"""打包入口（PyInstaller 用）。

- **无参数**运行：直接打开抢课面板（`gui --grab`），双击即用。
- 带参数运行：原样交给 CLI，例如 `phyexp-gui login` / `phyexp-gui papers`
  ——「登录」按钮在打包版里就靠这条路径起浏览器（见 `gui_grab._start_login`）。

为什么还要自己兜异常：打包版是 `--windowed`（没有控制台），未捕获异常会被 PyInstaller
弹成一个**原始 traceback 对话框**（用户看到的是一屏栈，不是人话）。
这里改成：栈写进 `%LOCALAPPDATA%\\PhyExpLab\\logs\\crash-*.log` + 一个简短的中文提示框。
"""

from __future__ import annotations

import sys


def _report_crash(exc: BaseException) -> None:
    import datetime as dt
    import os
    import pathlib
    import traceback

    detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    base = pathlib.Path(os.environ.get("LOCALAPPDATA", pathlib.Path.home() / "AppData" / "Local"))
    log_dir = base / "PhyExpLab" / "logs"
    log_path = None
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"crash-{dt.datetime.now():%Y%m%d-%H%M%S}.log"
        log_path.write_text(detail, encoding="utf-8")
    except OSError:
        pass

    first = f"{type(exc).__name__}: {exc}".splitlines()[0][:300]
    tail = f"\n\n详细信息已写入：\n{log_path}" if log_path else ""
    try:  # 用 Win32 弹窗：打包版没有控制台，也不能假定 Qt 已经起好
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, f"{first}{tail}", "南航物理实验助手 · 出错了", 0x10)
    except Exception:  # noqa: BLE001 - 弹窗失败就算了，日志已经留下
        pass


def main() -> int:
    from phyexp_lab.cli import main as cli_main

    argv = sys.argv[1:] or ["gui", "--grab"]
    return cli_main(argv)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - 打包版必须自己兜，否则只剩原始栈
        _report_crash(exc)
        raise SystemExit(1) from None
