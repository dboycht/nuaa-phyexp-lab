"""Playwright 弹窗登录 + 会话持久化。

职责边界（重要）
----------------
- **只负责登录与保存会话**：账号密码由使用者在弹出的真实 Chromium 窗口里输入，
  本模块既不接收也不保存任何账号密码（验证码同理，人工输入）。
- 会话保存在**仓库之外**的 `%LOCALAPPDATA%\\PhyExpLab\\session\\`：
  `state.json`（Playwright storage_state：Cookie + localStorage token）与 `token.json`（抽出的 Bearer token）。
- 为了经得起「用户随手关掉窗口」，会话在轮询循环里**增量保存**，而不是只在退出时保存一次。

登录成功判据（实测得出，不靠 URL 猜）
------------------------------------
前端登录成功后执行 `localStorage.setItem("token", "Bearer ".concat(user.token))`，
因此**判据 = 预约系统 origin 下的 `localStorage.token` 非空且以 `Bearer ` 开头**。
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from . import config

LogFn = Callable[[str], None]


class SessionError(RuntimeError):
    """登录/会话相关的可预期失败，消息面向使用者，可直接打印。"""


class PlaywrightMissingError(SessionError):
    """未安装 Playwright 或未安装 Chromium。"""


def _log(msg: str) -> None:
    print(msg, flush=True)


# ── 会话文件 ──


def state_path() -> Path:
    return config.session_state_path()


def has_state() -> bool:
    return state_path().is_file()


def state_age_seconds() -> float | None:
    """会话文件距上次保存的秒数；文件不存在时返回 None。"""
    path = state_path()
    if not path.is_file():
        return None
    return max(0.0, time.time() - path.stat().st_mtime)


def load_state() -> dict[str, Any]:
    path = state_path()
    if not path.is_file():
        raise SessionError(
            f"尚未保存会话：{path} 不存在。请先运行 `python run.py login` 完成一次登录。"
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SessionError(f"会话文件无法解析（{path}）：{exc}。请重新运行 `python run.py login`。") from exc


def load_cookies() -> dict[str, str]:
    """读取会话中的 Cookie 键值对。"""
    cookies = load_state().get("cookies") or []
    return {c["name"]: c["value"] for c in cookies if c.get("name")}


def load_token() -> str | None:
    """读取保存的 Bearer token（形如 ``Bearer eyJ...``）；没有则返回 None。"""
    path = config.token_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    token = data.get("token")
    return token if isinstance(token, str) and token else None


def auth_headers() -> dict[str, str]:
    """阶段 1/2 直接可用的请求头（token + 常规头）。"""
    headers = {
        "User-Agent": config.USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": config.BOOKING_ENTRY,
    }
    token = load_token()
    if token:
        headers["Authorization"] = token
    return headers


def clear_state() -> list[Path]:
    """删除本地会话文件；返回实际删除的文件列表。"""
    removed: list[Path] = []
    for path in (state_path(), config.token_path()):
        if path.is_file():
            path.unlink()
            removed.append(path)
    return removed


def build_http_session(base_url: str | None = None):
    """用已保存的 token / Cookie 构造 `requests.Session`（阶段 1 数据研究用）。

    注意：目标站有前置防护，**纯脚本请求可能仍被 412 拒绝**；
    遇到就以浏览器上下文（Playwright）为准，不要在这里硬绕。
    """
    import requests  # 延迟导入：只有真正需要 HTTP 会话时才要求装了 requests

    session = requests.Session()
    session.headers.update(auth_headers())
    for name, value in load_cookies().items():
        session.cookies.set(name, value)
    return session


# ── token 解析（仅用于提示有效期，不校验签名）──


def describe_token(token: str) -> str:
    """尽力解出 JWT 的签发/过期时间，返回一句人类可读的描述。

    **只做 base64 解码、不校验签名**；解析失败时如实说明「无法解析」，不编造。
    """
    raw = token[7:] if token.lower().startswith("bearer ") else token
    parts = raw.split(".")
    if len(parts) != 3:
        return f"非标准 JWT（长度 {len(raw)}），未解析有效期"
    payload_b64 = parts[1]
    padding = "=" * (-len(payload_b64) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding).decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - 解析失败只提示，不影响主流程
        return f"JWT 载荷解码失败（{type(exc).__name__}），未解析有效期"
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        return "JWT 中没有 exp 字段，有效期未知"
    expire_at = dt.datetime.fromtimestamp(exp).astimezone()
    remain = expire_at - dt.datetime.now().astimezone()
    remain_text = "已过期" if remain.total_seconds() <= 0 else f"约剩 {remain}"
    return f"有效期至 {expire_at:%Y-%m-%d %H:%M:%S}（{remain_text}）"


# ── 登录流程 ──


def _read_token(context: Any) -> str | None:
    """从预约系统 origin 的 localStorage 里读 token（登录成功判据）。"""
    try:
        pages = list(context.pages)
    except Exception:
        return None
    for page in pages:
        try:
            if not page.url.startswith(config.BOOKING_BASE):
                continue
            value = page.evaluate("() => window.localStorage.getItem('token')")
        except Exception:
            continue
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _describe_pages(context: Any) -> str:
    try:
        urls = [page.url[:100] for page in context.pages]
    except Exception:
        return "(无法读取标签页)"
    return " | ".join(urls) if urls else "(无标签页)"


def _save_session(context: Any, token: str | None, log: LogFn) -> bool:
    """保存 storage_state（含 Cookie 与 localStorage）与抽出的 token。返回是否有内容可存。"""
    saved = False
    try:
        context.storage_state(path=str(state_path()))
        saved = True
    except Exception as exc:
        log(f"[warn] storage_state 保存失败（浏览器可能已关闭）：{exc}")
    if token:
        payload = {
            "token": token,
            "saved_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "expiry_hint": describe_token(token),
        }
        try:
            config.token_path().write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            log(f"[warn] token 写入失败：{exc}")
    return saved


# ── Playwright 浏览器目录（打包版的关键）──


def browsers_dir() -> Path:
    """Playwright 浏览器的**用户级**目录（源码版与打包版共用同一份，不各装一份）。"""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "ms-playwright"


def pin_browsers_path() -> Path:
    """把 `PLAYWRIGHT_BROWSERS_PATH` 钉到用户级目录 —— **必须在起 driver 之前调用**。

    为什么必须显式钉（2026-10-08 实测踩坑）：
    `playwright/_impl/_transport.py` 里有这么一段：

        # For pyinstaller and Nuitka
        if getattr(sys, "frozen", False) or globals().get("__compiled__"):
            env.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")

    —— 它**假定打包者把浏览器一起打进了包内**（`0` = "浏览器就在 playwright 包旁边"）。
    本项目把浏览器装在用户级目录，冻结后 `0` 会指向 `_internal\\playwright\\driver\\package\\.local-browsers`
    （里面没有 chrome.exe）⇒ 打包版点「登录」直接报
    `BrowserType.launch: Executable doesn't exist at ...`。
    因为用的是 `setdefault`，**我们的代码先设好这个变量就能覆盖它**。
    """
    target = browsers_dir()
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(target)
    return target


def chromium_ready() -> bool:
    """用户级目录里是否已有可用的 Chromium（决定"要不要先下载"）。"""
    root = browsers_dir()
    if not root.is_dir():
        return False
    return any(root.glob("chromium-*/chrome-win*/chrome.exe")) or \
        any(root.glob("chromium-*/chrome-win/chrome.exe"))


def install_chromium(log: LogFn = _log) -> tuple[bool, str]:
    """用**自带的 node 驱动**把 Chromium 下载到用户级目录。

    为什么要自带：打包版的目标机器**没有 Python、也没有 `playwright` 这个命令**，
    但驱动（node.exe + cli.js）就在我们包里 ⇒ 直接调它，等价于 `playwright install chromium`。
    返回 `(是否成功, 说明)`。
    """
    try:
        from playwright._impl._driver import compute_driver_executable
    except ImportError:
        return False, "本机没有 Playwright（源码版请先 pip install playwright）"
    node, cli = compute_driver_executable()
    env = dict(os.environ)
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers_dir())   # 装到用户级目录
    log(f"[浏览器] 正在下载 Chromium（约 130 MB，仅一次）→ {browsers_dir()}")
    try:
        proc = subprocess.run([str(node), str(cli), "install", "chromium"],
                              env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=1800)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"下载失败（{type(exc).__name__}）：{exc}"
    for line in (proc.stdout or "").splitlines()[-6:]:
        if line.strip():
            log(f"[浏览器] {line.strip()}")
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        return False, f"下载失败（退出码 {proc.returncode}）：{' | '.join(tail)[:200]}"
    if not chromium_ready():
        return False, "下载命令成功，但仍找不到 chrome.exe（请重试或手动执行 playwright install chromium）"
    log("[浏览器] Chromium 就绪。")
    return True, "Chromium 已就绪"


#: 系统自带/常见浏览器安装位置（Windows）。优先 Edge —— Win10/11 自带，用户**零下载**。
#: ⚠️ 用 (环境变量名, 相对路径) 显式拼接：**不要**写 `{ProgramFiles(x86)}` 那种花括号占位符 ——
#: `os.path.expandvars` 只认 `$VAR` / `${VAR}` / `%VAR%`，花括号会被当成字面量（实测踩过，
#: 结果"本机明明装了 Edge 却检测为未安装"）。
SYSTEM_BROWSER_CANDIDATES: tuple[tuple[str, tuple[tuple[str, tuple[str, ...]], ...]], ...] = (
    ("msedge", (
        ("ProgramFiles(x86)", ("Microsoft", "Edge", "Application", "msedge.exe")),
        ("ProgramFiles", ("Microsoft", "Edge", "Application", "msedge.exe")),
        ("LOCALAPPDATA", ("Microsoft", "Edge", "Application", "msedge.exe")),
    )),
    ("chrome", (
        ("ProgramFiles", ("Google", "Chrome", "Application", "chrome.exe")),
        ("ProgramFiles(x86)", ("Google", "Chrome", "Application", "chrome.exe")),
        ("LOCALAPPDATA", ("Google", "Chrome", "Application", "chrome.exe")),
    )),
)


def system_browser_channel() -> str | None:
    """本机已安装的系统浏览器对应的 Playwright channel（`msedge` / `chrome`），没有则 None。

    用**文件存在**判断而不是"启动一次试试"：界面要在起浏览器**之前**决定要不要提示下载，
    试启动会有窗口闪现、也慢。
    """
    for channel, candidates in SYSTEM_BROWSER_CANDIDATES:
        for env_var, parts in candidates:
            base = os.environ.get(env_var)
            if base and os.path.isfile(os.path.join(base, *parts)):
                return channel
    return None


def browser_attempts() -> list[str | None]:
    """起浏览器的**尝试顺序**（越靠前越"省事"）。

    1. 环境变量 `PHYEXP_BROWSER_CHANNEL` 指定的（高级用户可强制，如 `msedge`/`chrome`/`chromium`）；
    2. 系统已装的 Edge / Chrome —— **别人电脑不用下载任何东西**；
    3. `None` = Playwright 自带的 Chromium（需要先下载约 130 MB，用户级目录）。
    """
    override = (os.environ.get("PHYEXP_BROWSER_CHANNEL") or "").strip().lower()
    if override:
        return [None if override in ("chromium", "bundled") else override]
    plan: list[str | None] = []
    channel = system_browser_channel()
    if channel:
        plan.append(channel)
    plan.append(None)          # 兜底：自带 Chromium
    return plan


def _launch_with_best_browser(pw: Any, *, log: LogFn = _log, headless: bool = False) -> Any:
    """按 `browser_attempts()` 依次尝试，返回第一个成功的 Browser。

    为什么要这么做：Playwright 自带的 Chromium 需要先下载（~130 MB），而
    Windows 10/11 **自带 Edge** ⇒ 优先驱动系统 Edge 就能让"别人电脑"零下载直接登录。
    """
    pin_browsers_path()        # 自带 Chromium 那条路要用到（见 pin_browsers_path 的注释）
    errors: list[str] = []
    for channel in browser_attempts():
        label = channel or "自带的 Chromium"
        if channel is None and not chromium_ready():
            errors.append("自带 Chromium 未下载")
            continue
        try:
            log(f"[浏览器] 使用{label}…")
            if channel:
                return pw.chromium.launch(headless=headless, channel=channel,
                                          args=config.CHROMIUM_ARGS)
            return pw.chromium.launch(headless=headless, args=config.CHROMIUM_ARGS)
        except Exception as exc:  # noqa: BLE001 - 换下一个内核继续试
            first = str(exc).splitlines()[0][:140]
            errors.append(f"{label}：{first}")
            log(f"[浏览器] {label} 不可用 —— {first}")
    raise PlaywrightMissingError(
        "找不到可用的浏览器内核。\n"
        "  · Windows 自带的 Edge 通常可直接使用；若已卸载，可执行：\n"
        "      python -m playwright install chromium\n"
        "    （打包版会在下次点「登录」时询问是否下载，约 130 MB）\n"
        "  细节：" + " ｜ ".join(errors)
    )


def interactive_login(
    *,
    record_har: bool = False,
    max_wait_seconds: int = 1800,
    use_saved_state: bool = True,
    log: LogFn = _log,
) -> Path:
    """打开可见的 Chromium，由使用者本人完成登录；返回保存后的会话文件路径。

    参数
    ----
    record_har:
        True 时录制 HAR（含响应体，供接口逆向）+ 脱敏 JSONL；登录后浏览过的页面请求都会被记录。
    max_wait_seconds:
        最长等待时间，默认 30 分钟；到点、关闭窗口或 Ctrl+C 都会结束并保存会话。
    use_saved_state:
        默认 True，把上次保存的会话带进本次浏览器（「记住我」场景下可免登录）。
    """
    # ⚠️ 必须在起 driver **之前**钉住浏览器目录：playwright 在冻结/编译环境下会
    #    setdefault(PLAYWRIGHT_BROWSERS_PATH, "0")（= "浏览器就在包旁边"），打包版会因此找不到
    #    可执行文件（见 pin_browsers_path 的注释与 ERROR.md E18）。
    pin_browsers_path()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - 环境相关
        raise PlaywrightMissingError(
            "未安装 Playwright。请先执行：\n"
            "  pip install playwright\n"
            "  playwright install chromium"
        ) from exc

    config.ensure_home()

    har_path: Path | None = None
    jsonl_path: Path | None = None
    if record_har:
        from . import recon

        jsonl_path, har_path = recon.new_capture_paths()
        log(f"[recon] 请求摘要 → {jsonl_path}")
        log(f"[recon] 完整 HAR → {har_path}")

    deadline = time.time() + max_wait_seconds
    last_token = ""
    last_heartbeat = 0.0
    announced = False
    no_page_since: float | None = None

    with sync_playwright() as pw:
        browser = _launch_with_best_browser(pw, log=log)

        context_args: dict[str, Any] = {
            "user_agent": config.USER_AGENT,
            "viewport": config.VIEWPORT,
            "locale": config.LOCALE,
            "timezone_id": config.TIMEZONE,
        }
        if use_saved_state and has_state():
            context_args["storage_state"] = str(state_path())
            log("[info] 已载入上次保存的会话（若仍有效则无需重新登录）")
        if har_path is not None:
            context_args.update(
                record_har_path=str(har_path),
                record_har_mode="full",
                record_har_content="embed",  # 逆向要看响应体，必须内嵌
            )

        context = browser.new_context(**context_args)
        context.add_init_script(config.STEALTH_JS)

        if jsonl_path is not None:
            from . import recon

            recon.RequestRecorder(jsonl_path, log=log).attach(context)

        page = context.new_page()
        try:
            page.goto(config.BOOKING_ENTRY, wait_until="domcontentloaded", timeout=30_000)
        except Exception as exc:
            log(f"[warn] 打开入口页失败（可稍后在窗口里手动访问）：{exc}")

        log("")
        log("=" * 70)
        log("  请在刚弹出的浏览器窗口中操作：")
        log("    1) 用「用户名（学号）+ 密码」登录（页面上的登录表单）；")
        log("    2) 登录后点进「我的课程 / 实验 / 选课」相关页面浏览一圈。")
        log("")
        log("  说明：程序不接触你的密码；只要检测到登录态就自动保存会话。")
        log("  完成后直接关闭浏览器窗口即可（或到此终端按 Ctrl+C）。")
        log(f"  想让它主动收尾（更稳妥，HAR 一定会落盘）：另开终端跑 `python run.py stop`。")
        log("=" * 70)
        log("")

        try:
            while time.time() < deadline:
                if not browser.is_connected():
                    log("[info] 浏览器窗口已关闭，准备收尾。")
                    break

                # 优雅停止开关：外部删掉/创建 STOP.flag 即可收尾（不必杀进程，HAR 才能落盘）
                if config.stop_flag_path().is_file():
                    log(f"[info] 检测到停止标志 {config.stop_flag_path()}，准备收尾。")
                    try:
                        config.stop_flag_path().unlink()
                    except OSError:
                        pass
                    break

                # 标签页全被关掉（但 Chromium 进程仍在）时不能傻等：连续 15 秒无标签页即收尾
                try:
                    page_count = len(context.pages)
                except Exception:
                    page_count = 0
                if page_count == 0:
                    if no_page_since is None:
                        no_page_since = time.time()
                        log("[info] 所有标签页已关闭；若 15 秒内没有新标签页就收尾。")
                    elif time.time() - no_page_since >= 15:
                        log("[info] 已确认无标签页，准备收尾。")
                        break
                else:
                    no_page_since = None

                token = _read_token(context)
                if token and token != last_token:
                    last_token = token
                    _save_session(context, token, log)
                    log(f"[ok] 检测到登录态，会话已保存（{describe_token(token)}）")
                    log(f"     会话文件 → {state_path()}")
                    if not announced:
                        announced = True
                        log("     （建议再点几个页面，让接口请求都被记录下来）")

                now = time.time()
                if now - last_heartbeat >= 20:
                    last_heartbeat = now
                    if token:
                        log(f"[..] 已登录，等待你浏览/收尾（剩余 {int(deadline - now)}s）")
                    else:
                        remain = int(deadline - now)
                        log(f"[..] 等待登录中（剩余 {remain}s）：{_describe_pages(context)}")

                time.sleep(1.0)
            else:
                log(f"[warn] 已到最长等待时间（{max_wait_seconds}s），自动收尾。")
        except KeyboardInterrupt:
            log("[info] 收到 Ctrl+C，准备收尾。")

        # 收尾：先尽力存一次会话，再关上下文（关上下文才会把 HAR 落盘）
        final_token = last_token or _read_token(context)
        if _save_session(context, final_token, log):
            log(f"[ok] 最终会话已保存 → {state_path()}")
        try:
            context.close()
        except Exception:
            pass
        try:
            browser.close()
        except Exception:
            pass

    if not state_path().is_file():
        raise SessionError(
            "没有拿到任何会话：看起来登录尚未完成。"
            "请重新运行 `python run.py login`，并在弹出的窗口里完成登录。"
        )
    if not load_token():
        raise SessionError(
            "会话已保存，但没读到登录 token（localStorage 里没有 token）。\n"
            "通常意味着**登录没有真正成功**，而不是程序出错。\n"
            f"排查：重跑 `python run.py login`，确认页面显示已登录、能打开「我的课程」。会话文件：{state_path()}"
        )
    if record_har and har_path is not None:
        log(f"[done] HAR 已写入 → {har_path}")
    return state_path()
