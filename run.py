#!/usr/bin/env python
"""开发期启动入口：把 `src/` 加进 sys.path 后转交 `phyexp_lab.cli.main()`。

为什么需要它：本仓库是 `src/` 布局且未安装成包，直接 `python -m phyexp_lab.cli` 会因为
模块不在 sys.path 上而失败。这里显式兜底，保证**克隆下来直接能跑**：

    python run.py login
    python run.py recon

（装成包之后也可以用控制台脚本 `phyexp`，见 pyproject.toml 的 `[project.scripts]`。）
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from phyexp_lab.cli import main  # noqa: E402  （必须在 sys.path 调整之后导入）

if __name__ == "__main__":
    sys.exit(main())
