"""命令行入口：`python run.py <子命令>`。

子命令
------
- `version`  查看版本
- `status`   查看数据目录、会话状态与最近的采集文件
- `login`    打开浏览器登录并保存会话
- `recon`    侦察：登录 + 记录请求（HAR + 脱敏 JSONL）
- `logout`   删除本地会话文件
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

from . import __version__, config, probe, scrub, session
from .session import PlaywrightMissingError, SessionError

REPO_URL = "https://github.com/dboycht/nuaa-phyexp-lab"


def _human_age(seconds: float | None) -> str:
    if seconds is None:
        return "无会话"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} 秒前"
    if seconds < 3600:
        return f"{seconds // 60} 分 {seconds % 60} 秒前"
    if seconds < 86400:
        return f"{seconds // 3600} 小时 {(seconds % 3600) // 60} 分前"
    return f"{seconds // 86400} 天前"


def _session_cookie_count() -> str:
    """会话里的 Cookie 数量（只数个数，不展示任何值）。"""
    try:
        return str(len(session.load_state().get("cookies") or []))
    except SessionError:
        return "-"


def _token_summary() -> str:
    """token 的存在性与有效期（只显示长度与 exp，绝不打印 token 本身）。"""
    token = session.load_token()
    if not token:
        return "无 token（需登录）"
    return f"已保存（{len(token)} 字符）；{session.describe_token(token)}"


# ── 子命令 ──


def _cmd_version(_args: argparse.Namespace) -> int:
    print(f"nuaa-phyexp-lab {__version__}")
    print(f"仓库：{REPO_URL}")
    print(f"目标系统：{config.BOOKING_BASE}（物理实验预约选课）")
    return 0


def _cmd_status(_args: argparse.Namespace) -> int:
    home = config.home_dir()
    state = session.state_path()
    print(f"版本            : {__version__}")
    print(f"目标入口        : {config.BOOKING_ENTRY}")
    print(f"API 基址        : {config.API_BASE}")
    print(f"数据目录        : {home}  （存在：{home.is_dir()}）")
    print(f"会话文件        : {state}")
    if state.is_file():
        size = state.stat().st_size
        print(f"会话状态        : 已有会话（{_human_age(session.state_age_seconds())}保存，"
              f"{size} 字节，Cookie {_session_cookie_count()} 条）")
    else:
        print("会话状态        : 无（请先运行 `python run.py login`）")
    print(f"登录 token      : {_token_summary()}")

    recon_dir = config.recon_dir()
    captures = sorted(recon_dir.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True) if recon_dir.is_dir() else []
    if captures:
        print(f"采集文件（{recon_dir}）：")
        for path in captures[:5]:
            mtime = dt.datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            print(f"  - {path.name}  {path.stat().st_size} 字节  {mtime}")
    else:
        print(f"采集文件        : 无（{recon_dir}）")
    return 0


def _run_login(record_har: bool, max_wait: int, use_saved_state: bool = True) -> int:
    try:
        state = session.interactive_login(
            record_har=record_har,
            max_wait_seconds=max_wait,
            use_saved_state=use_saved_state,
        )
    except PlaywrightMissingError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2
    except SessionError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n[中断] 已取消。", file=sys.stderr)
        return 130
    print(f"[完成] 会话文件：{state}")
    return 0


def _cmd_login(args: argparse.Namespace) -> int:
    return _run_login(record_har=False, max_wait=args.max_wait,
                      use_saved_state=not args.no_saved_state)


def _cmd_recon(args: argparse.Namespace) -> int:
    print("[提示] 侦察模式会记录所有请求（含响应体）到本地 recon 目录；登录类请求已脱敏。")
    return _run_login(record_har=True, max_wait=args.max_wait,
                      use_saved_state=not args.no_saved_state)


def _cmd_probe(args: argparse.Namespace) -> int:
    """只读连通性自检：脚本直连 API 到底行不行（决定抢课引擎形态）。"""
    print(f"目标：GET {config.API_BASE}/{args.path.lstrip('/')}   （只读，无写操作）")
    results = probe.probe_endpoint(path=args.path, timeout=args.timeout)
    print(f"{'用例':34s} {'状态':>6s} {'耗时':>7s}  说明")
    print("-" * 92)
    for r in results:
        status = "-" if r.status is None else str(r.status)
        elapsed = "-" if r.elapsed_ms is None else f"{r.elapsed_ms}ms"
        note = r.error or r.body_head[:44]
        print(f"{r.case.name:34s} {status:>6s} {elapsed:>7s}  {note}")
    print("-" * 92)
    print(probe.verdict(results))

    if args.repeat > 0:
        print()
        print(f"稳态延迟测量：同一条 keep-alive 会话连发 {args.repeat} 次 GET（首次含 TLS 握手，不计入结论）")
        try:
            samples = probe.measure_latency(path=args.path, repeat=args.repeat, timeout=args.timeout)
        except SessionError as exc:
            print(f"[跳过] {exc}")
            return 0
        print(f"  各次耗时(ms)：{', '.join(str(s) for s in samples)}")
        steady = samples[1:] or samples
        if steady:
            ordered = sorted(steady)
            median = ordered[len(ordered) // 2]
            print(f"  稳态（去掉首次）：最小 {min(steady)}ms / 中位 {median}ms / 最大 {max(steady)}ms  "
                  f"（样本 {len(steady)}）")
            print(f"  ⇒ 预发射提前量的量级参考：约 {median}ms（抢课参数别用首次那 {samples[0]}ms）")
    return 0


def _cmd_scrub(args: argparse.Namespace) -> int:
    source = Path(args.har)
    if not source.is_file():
        print(f"[错误] HAR 文件不存在：{source}", file=sys.stderr)
        return 2

    target = source if args.in_place else scrub.default_target(source)
    if args.in_place:
        backup = source.with_name(source.name + ".bak")
        backup.write_bytes(source.read_bytes())
        print(f"[info] 原文件已备份 → {backup}")

    stats = scrub.scrub_har(source, target)
    print(f"[完成] 脱敏输出 → {target}")
    print(f"  条目 {stats['entries']} 条：删除 postData {stats['post_data_removed']} 处、"
          f"脱敏敏感头 {stats['headers_redacted']} 处、删除敏感响应体 {stats['bodies_removed']} 条")

    problems = scrub.verify_no_credentials(target)
    if problems:
        print("[警告] 复检发现残留，请人工检查：")
        for item in problems[:20]:
            print(f"  - {item}")
        return 1
    print("[复检] 未发现明文凭据 / 敏感接口残留 postData / 残留 JWT ✅")
    return 0


def _cmd_stop(_args: argparse.Namespace) -> int:
    config.ensure_home()
    flag = config.stop_flag_path()
    flag.write_text("stop\n", encoding="utf-8")
    print(f"[完成] 已请求优雅收尾：{flag}")
    print("       正在运行的 login/recon 会在下一次轮询（≤1 秒）时保存会话与 HAR 后退出。")
    return 0


def _cmd_logout(_args: argparse.Namespace) -> int:
    removed = session.clear_state()
    if removed:
        for path in removed:
            print(f"[完成] 已删除 {path}")
    else:
        print("[跳过] 本来就没有会话文件。")
    return 0


# ── 解析器 ──


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="phyexp",
        description="南航物理实验中心数据研究 / 物理实验预约辅助工具",
    )
    parser.add_argument("--version", action="version", version=f"nuaa-phyexp-lab {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("version", help="查看版本与目标系统")
    sub.add_parser("status", help="查看数据目录 / 会话 / 采集文件状态")
    sub.add_parser("logout", help="删除本地会话文件")
    sub.add_parser("stop", help="让正在运行的 login/recon 优雅收尾（保存 HAR 后退出）")

    p_login = sub.add_parser("login", help="打开浏览器登录并保存会话")
    p_login.add_argument("--max-wait", type=int, default=1800,
                         help="最长等待秒数（默认 1800，即 30 分钟）")
    p_login.add_argument("--no-saved-state", action="store_true",
                         help="不载入上次会话，强制重新登录")

    p_recon = sub.add_parser("recon", help="侦察：登录并记录请求（HAR + 脱敏 JSONL）")
    p_recon.add_argument("--max-wait", type=int, default=1800,
                         help="最长等待秒数（默认 1800，即 30 分钟）")
    p_recon.add_argument("--no-saved-state", action="store_true",
                         help="不载入上次会话，强制重新登录")

    p_scrub = sub.add_parser("scrub", help="HAR 脱敏：抹掉凭据与敏感响应体后再做分析")
    p_scrub.add_argument("har", help="要脱敏的 .har 文件路径")
    p_scrub.add_argument("--in-place", action="store_true",
                         help="就地覆盖（会先生成 .bak 备份）；默认输出 <名字>.scrubbed.har")

    p_probe = sub.add_parser("probe", help="只读自检：脚本直连 API 是否可行（决定抢课引擎形态）")
    p_probe.add_argument("--path", default="rest/time",
                         help="要探测的接口路径（相对 API 基址，默认 rest/time；只发 GET）")
    p_probe.add_argument("--timeout", type=float, default=10.0, help="单次请求超时秒数（默认 10）")
    p_probe.add_argument("--repeat", type=int, default=5,
                         help="额外做 N 次 keep-alive 连发以测稳态 RTT（默认 5；填 0 跳过）")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "version": _cmd_version,
        "status": _cmd_status,
        "login": _cmd_login,
        "recon": _cmd_recon,
        "scrub": _cmd_scrub,
        "probe": _cmd_probe,
        "stop": _cmd_stop,
        "logout": _cmd_logout,
    }
    return handlers[args.command](args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
