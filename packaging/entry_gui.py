"""打包入口（PyInstaller 用）。

- **无参数**运行：直接打开抢课面板（`gui --grab`），双击即用。
- 带参数运行：原样交给 CLI，例如 `phyexp-gui login` / `phyexp-gui papers`
  ——「登录」按钮在打包版里就靠这条路径起浏览器（见 `gui_grab._start_login`）。

放在打包目录而不是 `src/` 里，是因为它是**发布流程**的一部分，不属于库代码。
"""

from __future__ import annotations

import sys


def main() -> int:
    from phyexp_lab.cli import main as cli_main

    argv = sys.argv[1:] or ["gui", "--grab"]
    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
