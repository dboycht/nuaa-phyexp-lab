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

from . import __version__, config, session
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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "version": _cmd_version,
        "status": _cmd_status,
        "login": _cmd_login,
        "recon": _cmd_recon,
        "logout": _cmd_logout,
    }
    return handlers[args.command](args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
