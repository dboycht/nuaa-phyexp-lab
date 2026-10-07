"""全局配置：本地数据目录、目标系统 URL、浏览器指纹与反检测脚本。

本文件里的 URL / 接口常量**全部来自 2026-09-21 的实测与前端口令包分析**，
不是猜的；来源与证据见 `docs/接口逆向.md`。

设计要点
--------
1. **运行时数据一律落在仓库之外**（默认 `%LOCALAPPDATA%\\PhyExpLab`），
   从根上避免「Cookie / token / HAR / 日志误提交进 GitHub」这一类事故。
2. 目标系统按**天目湖校区**入口走：`/tianmuhu/wechat/login`（实测 200，页面标题「实验助手」）。
   注意同域的 `/landing`（另一套/新版入口）对脚本客户端返回 **412 Precondition Failed**，
   因此统一以真实浏览器上下文访问。
"""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "PhyExpLab"

# ── 目标系统 ──
CENTER_SITE = "http://phylab.nuaa.edu.cn/"
BOOKING_BASE = "https://phyexp.nuaa.edu.cn"

#: 天目湖校区登录/入口页（2026-09-21 实测 HTTP 200，标题「实验助手」，Vue SPA）
BOOKING_ENTRY = f"{BOOKING_BASE}/tianmuhu/wechat/login"

#: 后端 API 基址（实测证据：前端 `var BASE_URL = '/tianmuhu/api'`）
API_BASE = f"{BOOKING_BASE}/tianmuhu/api"

#: 另一套入口：实测对脚本客户端返回 412，仅作记录/排查用
LEGACY_LANDING = f"{BOOKING_BASE}/landing"

#: 登录接口（相对 API_BASE）：载荷 `{code: 用户名, password: MD5(密码)}`，表单编码
LOGIN_RPC = "rest/rpc/login"
#: 选课/退课写操作（相对 API_BASE）
ELECT_ENDPOINT = "report-api/electives"

#: 前端把登录态存在 localStorage 的这个键里，值为 `"Bearer <jwt>"`（实测）
TOKEN_STORAGE_KEY = "token"

# ── 浏览器指纹（与 NUAA-Snatcher 保持一致，避免两套系统各踩一次坑）──
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
VIEWPORT = {"width": 1600, "height": 900}
LOCALE = "zh-CN"
TIMEZONE = "Asia/Shanghai"

CHROMIUM_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--no-sandbox",
    "--disable-infobars",
    "--disable-dev-shm-usage",
]

#: 反检测注入脚本。来源：同作者项目 NUAA-Snatcher（MIT）`LoginWorker.STEALTH_JS`
STEALTH_JS = """
// 1. 隐藏 webdriver 标记
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
// 2. 伪造 chrome 对象
window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}};
// 3. 伪造 plugins / languages
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN','zh','en']});
// 4. 移除 PhantomJS 痕迹
delete window.callPhantom;
// 5. 覆盖权限查询
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications' ?
    Promise.resolve({state: Notification.permission}) :
    originalQuery(parameters)
);
"""

# ── 本地数据目录 ──


def home_dir() -> Path:
    """运行时数据根目录：可用环境变量 ``PHYEXP_HOME`` 覆盖。"""
    override = os.environ.get("PHYEXP_HOME")
    if override:
        return Path(override).expanduser()
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        return Path(local_appdata) / APP_NAME
    # 非 Windows 或环境变量缺失时的兜底（仍然在仓库之外）
    return Path.home() / f".{APP_NAME.lower()}"


def ensure_home() -> Path:
    """确保运行时目录存在并返回根目录。"""
    root = home_dir()
    for sub in (root, session_dir(), recon_dir(), logs_dir()):
        sub.mkdir(parents=True, exist_ok=True)
    return root


def session_dir() -> Path:
    return home_dir() / "session"


def session_state_path() -> Path:
    """Playwright ``storage_state`` 文件（含 Cookie 与 localStorage token，敏感）。"""
    return session_dir() / "state.json"


def token_path() -> Path:
    """从会话里单独抽出的 Bearer token（便于阶段 2 脚本直接复用）。"""
    return session_dir() / "token.json"


def recon_dir() -> Path:
    return home_dir() / "recon"


def stop_flag_path() -> Path:
    """删除这个文件即请求登录/侦察流程优雅收尾（不杀进程，保证 HAR 落盘）。"""
    return home_dir() / "STOP.flag"


def logs_dir() -> Path:
    return home_dir() / "logs"


def downloads_dir() -> Path:
    """系统的「下载」文件夹。

    用 Windows **已知文件夹** API 取，而不是拼 `%USERPROFILE%\\Downloads` ——
    很多人把下载目录挪到了别的盘（C 盘小），拼字符串会指到不存在的路径。
    取不到时退回 `%USERPROFILE%\\Downloads`，再退回运行目录（保证总有地方可写）。
    """
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_byte * 8)]

        # FOLDERID_Downloads = {374DE290-123F-4565-9164-39C4925E467B}
        folder_id = GUID(0x374DE290, 0x123F, 0x4565,
                         (ctypes.c_byte * 8)(0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B))
        path_ptr = ctypes.c_wchar_p()
        result = ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(folder_id), 0, None, ctypes.byref(path_ptr))
        if result == 0 and path_ptr.value:
            resolved = Path(path_ptr.value)
            ctypes.windll.ole32.CoTaskMemFree(path_ptr)
            if resolved.is_dir():
                return resolved
    except Exception:  # noqa: BLE001 - 取不到就退回下面的备选，不让它影响下载
        pass
    fallback = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Downloads"
    return fallback if fallback.is_dir() else home_dir()


def notes_dir() -> Path:
    """实验笔记/报告纸 PDF 的存放目录 = **「下载」文件夹下的「实验笔记」子目录**。

    用户 2026-10-07："不应该下载到下载文件夹吗" ⇒ 默认改到下载文件夹；
    放一个子目录是为了不跟其它下载文件混在一起（要平铺在 Downloads 根目录就改这里）。
    """
    return downloads_dir() / "实验笔记"
