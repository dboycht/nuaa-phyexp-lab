# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把抢课面板打成 Windows 可执行文件。

用法（在仓库根目录）：
    pyinstaller --noconfirm --clean packaging\\phyexp-gui.spec

产物：`dist/phyexp-gui/`（onedir）+ `dist/phyexp-gui.zip`（用 packaging\\build_exe.ps1 生成）

为什么 onedir 而不是 onefile：
  PySide6 解包到临时目录很慢（onefile 每次启动都要解包 ~150 MB），且更容易被杀软误报；
  onedir 启动快、体积可见、便于排查。要单文件可改 `COLLECT` 为 `EXE(..., onefile)`。

Playwright：登录要用它起浏览器，故显式收集（`collect_all`）；它自带的 node 驱动比较大，
但少了它「登录」按钮在打包版里就没法用（本机已装的会另有一份在 %LOCALAPPDATA%\\ms-playwright）。
"""

from PyInstaller.utils.hooks import collect_all
import os

# ⚠️ spec 里的相对路径是**相对 spec 文件所在目录**解析的（实测：写 "packaging/entry_gui.py"
# 会被拼成 packaging/packaging/entry_gui.py 而报 not found）⇒ 一律用绝对路径。
HERE = os.path.abspath(SPECPATH)          # ...\nuaa-phyexp-lab\packaging
ROOT = os.path.dirname(HERE)              # ...\nuaa-phyexp-lab

datas, binaries, hiddenimports = [], [], []
for package in ("playwright",):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

a = Analysis(
    [os.path.join(HERE, "entry_gui.py")],
    pathex=[os.path.join(ROOT, "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "PySide6.QtWebEngineCore"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="phyexp-gui",
    icon=os.path.join(HERE, "phyexp.ico"),   # 与界面同一枚图标（_scratch/make_ico.py 从 theme 生成）
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # GUI 程序：不弹黑窗（登录子进程也照样工作）
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="phyexp-gui",
)
