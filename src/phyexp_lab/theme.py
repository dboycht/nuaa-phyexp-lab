"""界面主题：统一配色、字体、图标与样式表（PySide6）。

为什么要有这个模块
------------------
1. **单一来源**：两个窗口（只读工作台 `gui.py`、抢课面板 `gui_grab.py`）共用一份配色与 QSS，
   避免"各写一套、改一处漏一处"。
2. **图标零依赖**：用 Windows 自带的 **Segoe Fluent Icons** 字体（本机实测存在），
   不打包字体文件、不引入第三方图标库；缺字体时**自动退回纯文字**（不显示方框乱码）。
3. **可测量**：所有颜色/间距都是具名常量，改主题只改这里。

设计取舍（Qt 的能力边界）
------------------------
- Qt 的 QSS **不支持** `box-shadow` / `transition` / 伪元素 ⇒ "卡片感"只能靠
  **圆角 + 1px 边框 + 留白 + 分层底色**来做，别指望阴影。
- 深色/浅色两套主题一开始就分开定义（`LIGHT` 是当前默认），以免以后加暗色时又改一遍结构。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtGui import QColor, QFont, QFontDatabase


@dataclass(frozen=True)
class Palette:
    """语义化配色（**按用途命名**，不按颜色命名 —— 换配色时不用改调用点）。"""

    bg: str                 #: 窗口底色
    surface: str            #: 卡片/输入框底色
    surface_alt: str        #: 次级底色（表格斑马纹、日志区）
    border: str             #: 常规分隔线
    border_strong: str      #: 卡片边框
    text: str               #: 主文字
    text_muted: str         #: 次要文字
    primary: str            #: 主色（主按钮、强调）
    primary_hover: str
    primary_pressed: str
    on_primary: str         #: 主色上的文字
    ok: str                 #: 可约/成功
    ok_soft: str            #: 可约单元格底色
    warn: str               #: 已满/注意
    warn_soft: str
    danger: str             #: 失败/危险
    danger_soft: str
    taken: str              #: 已选/已做过（中性偏紫，与可约区分）
    taken_soft: str
    disabled: str           #: 不可用文字
    disabled_soft: str      #: 不可用底色


LIGHT = Palette(
    bg="#F4F6F8",
    surface="#FFFFFF",
    surface_alt="#F8FAFC",
    border="#E4E7EB",
    border_strong="#D5DAE1",
    text="#1F2933",
    text_muted="#6B7280",
    primary="#0F766E",
    primary_hover="#0D6A62",
    primary_pressed="#0B5A53",
    on_primary="#FFFFFF",
    ok="#15803D",
    ok_soft="#E7F6EC",
    warn="#B45309",
    warn_soft="#FFF7E6",
    danger="#B91C1C",
    danger_soft="#FDECEC",
    taken="#7E22CE",
    taken_soft="#F4EBFB",
    disabled="#9AA5B1",
    disabled_soft="#EEF1F4",
)

#: 当前使用的配色（将来要做暗色主题，加一份 DARK 并切换这里即可）
ACTIVE = LIGHT

#: 中文界面字体（本机实测有 Microsoft YaHei UI；退回顺序保证任何 Windows 都能看）
UI_FONTS = ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", "sans-serif")
#: 等宽（日志/数字对齐用）
MONO_FONTS = ("Cascadia Mono", "Consolas", "Courier New", "monospace")
#: Windows 自带图标字体（实测存在）；缺失时图标退化为空串
ICON_FONT_CANDIDATES = ("Segoe Fluent Icons", "Segoe MDL2 Assets")

#: 图标码位（Segoe Fluent Icons / MDL2 通用区间）
ICONS = {
    "refresh": "\uE72C",
    "calendar": "\uE787",
    "play": "\uE768",
    "stop": "\uE71A",
    "check": "\uE73E",
    "warning": "\uE7BA",
    "error": "\uEA39",
    "delete": "\uE74D",
    "info": "\uE946",
    "clock": "\uE823",
    "settings": "\uE713",
    "bell": "\uE7E7",
    "grid": "\uE80A",
    "list": "\uE8FD",
    "search": "\uE721",
    "undo": "\uE7A7",
    "target": "\uE1D3",
    "shield": "\uEA18",
    "lab": "\uE9D9",
}

_icon_family: str | None = None
_icon_checked = False


def icon_family() -> str | None:
    """返回可用的图标字体名；没有则 None（并只探测一次）。"""
    global _icon_family, _icon_checked
    if _icon_checked:
        return _icon_family
    _icon_checked = True
    try:
        available = set(QFontDatabase.families())
    except Exception:  # noqa: BLE001 - 没有 QApplication 时也不要炸
        available = set()
    for name in ICON_FONT_CANDIDATES:
        if name in available:
            _icon_family = name
            break
    return _icon_family


def icon(name: str) -> str:
    """图标字形（用于纯图标控件）；字体缺失或无此码位时返回空串。

    ⚠️ **实测教训（2026-10-07）**：把图标字符塞进"中文按钮文本"里**不会显示** ——
    QSS 的 `font-family` 会把字体族锁成中文字体，而中文字体没有这些码位；
    且 `QRawFont.supportsCharacter()` 会**误报 False**（实测 26 个码位全部能渲染出墨迹）。
    ⇒ 需要图标时用 `icon_pixmap()` / `icon_label()` **自己光栅化**，不要指望字体回退。
    """
    glyph = ICONS.get(name, "")
    return glyph if (glyph and icon_family()) else ""


def icon_pixmap(name: str, size: int = 16, color: str | None = None):
    """把图标字形渲染成 QPixmap（透明底）。

    为什么要光栅化：见 `icon()` 的实测教训 —— 这样图标**不受任何 QSS 字体设置影响**，
    放进 `QPushButton.setIcon()` 或 `QLabel.setPixmap()` 都能稳定显示。
    """
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QPainter, QPixmap

    glyph = ICONS.get(name, "")
    family = icon_family()
    pixmap = QPixmap(size + 4, size + 4)
    pixmap.fill(Qt.transparent)
    if not glyph or not family:
        return pixmap
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.Antialiasing, True)
        font = QFont(family, size)
        painter.setFont(font)
        painter.setPen(QColor(color or ACTIVE.text))
        painter.drawText(pixmap.rect(), Qt.AlignCenter, glyph)
    finally:
        painter.end()
    return pixmap


def icon_label(name: str, size: int = 16, color: str | None = None):
    """返回一个只显示图标的 QLabel（已设中心对齐）。"""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLabel

    label = QLabel()
    label.setPixmap(icon_pixmap(name, size, color))
    label.setAlignment(Qt.AlignCenter)
    return label


def qicon(name: str, size: int = 16, color: str | None = None):
    """图标 → QIcon（给 `setIcon()` 用）。"""
    from PySide6.QtGui import QIcon

    return QIcon(icon_pixmap(name, size, color))


def icon_font(size: int = 11) -> QFont:
    family = icon_family() or UI_FONTS[0]
    font = QFont(family, size)
    return font


def ui_font(size: int = 10, weight: QFont.Weight = QFont.Normal) -> QFont:
    font = QFont(UI_FONTS[0], size)
    font.setWeight(weight)
    return font


def monospace(size: int = 9) -> QFont:
    return QFont(MONO_FONTS[0], size)


# ── 单元格状态 → 配色（抢课网格用；集中在这里，避免界面里散落魔法色值）──

@dataclass(frozen=True)
class CellStyle:
    background: str
    border: str
    color: str
    hover: str = ""
    radius: int = 6


CELL_STYLES: dict[str, CellStyle] = {
    "available": CellStyle(background=ACTIVE.ok_soft, border="#A7D8B8", color=ACTIVE.ok,
                           hover="#D6EFE0"),
    "selected": CellStyle(background=ACTIVE.primary, border=ACTIVE.primary_pressed,
                          color=ACTIVE.on_primary, hover=ACTIVE.primary_hover),
    "full": CellStyle(background=ACTIVE.surface, border=ACTIVE.border, color=ACTIVE.disabled),
    "none": CellStyle(background=ACTIVE.disabled_soft, border=ACTIVE.border,
                      color="#B6BEC8"),
    "taken": CellStyle(background=ACTIVE.taken_soft, border="#DCC8F0", color=ACTIVE.taken),
    "all_taken": CellStyle(background=ACTIVE.taken_soft, border="#DCC8F0", color=ACTIVE.taken),
}


def cell_qss(state: str) -> str:
    """按状态生成单元格按钮的 QSS。"""
    style = CELL_STYLES.get(state, CELL_STYLES["none"])
    hover = style.hover or style.background
    return f"""
    QPushButton {{
        background: {style.background};
        border: 1px solid {style.border};
        border-radius: {style.radius}px;
        color: {style.color};
        padding: 2px;
        font-size: 8pt;
    }}
    QPushButton:hover {{ background: {hover}; }}
    QPushButton:disabled {{ color: {style.color}; background: {style.background}; }}
    """


# ── 全局样式表 ──

def stylesheet() -> str:
    """整窗 QSS（Qt 支持的子集：无阴影/无过渡）。"""
    c = ACTIVE
    return f"""
    QWidget {{
        background: {c.bg};
        color: {c.text};
    }}
    /*
     * ⚠️ 这里**故意不写 `font-family` / `font-size`**：QSS 一旦指定字体族，就会覆盖
     * `widget.setFont(...)`，于是"图标字形"永远渲染不出来（实测：图标全空）。
     * 基础字体统一由 `apply_theme()` 里的 `app.setFont(ui_font(10))` 提供。
     */
    QDialog, QMainWindow {{ background: {c.bg}; }}

    /* 标签默认透明（否则会盖住卡片底色） */
    QLabel {{ background: transparent; }}
    QCheckBox {{ background: transparent; }}

    /* 顶部标题卡 */
    QFrame#header {{
        background: {c.surface};
        border: 1px solid {c.border_strong};
        border-radius: 12px;
    }}
    QLabel#h1 {{ font-size: 15pt; font-weight: bold; color: {c.primary}; }}
    QLabel#sub {{ color: {c.text_muted}; font-size: 9pt; }}
    QLabel#step {{ color: {c.text_muted}; font-size: 9pt; }}

    /* 状态小标签（chip） */
    QLabel#chip {{
        background: {c.surface_alt}; border: 1px solid {c.border}; border-radius: 9px;
        padding: 2px 9px; color: {c.text_muted}; font-size: 9pt;
    }}
    QLabel#chipOk {{
        background: {c.ok_soft}; border: 1px solid #A7D8B8; border-radius: 9px;
        padding: 2px 9px; color: {c.ok}; font-size: 9pt;
    }}
    QLabel#chipWarn {{
        background: {c.warn_soft}; border: 1px solid #F0D9A8; border-radius: 9px;
        padding: 2px 9px; color: {c.warn}; font-size: 9pt;
    }}
    QLabel#chipDanger {{
        background: {c.danger_soft}; border: 1px solid #E9B7B7; border-radius: 9px;
        padding: 2px 9px; color: {c.danger}; font-size: 9pt;
    }}

    /* 卡片式分组 */
    QGroupBox {{
        background: {c.surface};
        border: 1px solid {c.border_strong};
        border-radius: 10px;
        margin-top: 14px;
        padding: 12px 12px 10px 12px;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        subcontrol-position: top left;
        left: 12px;
        padding: 0 6px;
        color: {c.primary};
        font-weight: bold;
    }}

    /* 按钮 */
    QPushButton {{
        background: {c.surface};
        border: 1px solid {c.border_strong};
        border-radius: 8px;
        padding: 6px 14px;
        color: {c.text};
    }}
    QPushButton:hover {{ background: {c.surface_alt}; border-color: {c.primary}; }}
    QPushButton:pressed {{ background: #ECF1F0; }}
    QPushButton:disabled {{ color: {c.disabled}; background: {c.disabled_soft};
                            border-color: {c.border}; }}
    QPushButton#primary {{
        background: {c.primary}; color: {c.on_primary}; border: 1px solid {c.primary_pressed};
        font-weight: bold;
    }}
    QPushButton#primary:hover {{ background: {c.primary_hover}; }}
    QPushButton#primary:pressed {{ background: {c.primary_pressed}; }}
    QPushButton#primary:disabled {{ background: {c.disabled_soft}; color: {c.disabled};
                                    border-color: {c.border}; }}
    QPushButton#danger {{
        background: {c.surface}; color: {c.danger}; border: 1px solid #E9B7B7;
    }}
    QPushButton#danger:hover {{ background: {c.danger_soft}; }}
    QPushButton#danger:disabled {{ color: {c.disabled}; border-color: {c.border};
                                   background: {c.disabled_soft}; }}

    /* 输入类 */
    QSpinBox, QTimeEdit, QLineEdit, QComboBox, QDateEdit {{
        background: {c.surface};
        border: 1px solid {c.border_strong};
        border-radius: 6px;
        padding: 4px 8px;
        min-height: 22px;
        selection-background-color: {c.primary};
    }}
    QSpinBox:focus, QTimeEdit:focus, QLineEdit:focus {{ border-color: {c.primary}; }}
    QCheckBox {{ spacing: 6px; }}
    QCheckBox::indicator {{ width: 15px; height: 15px; border-radius: 4px;
                            border: 1px solid {c.border_strong}; background: {c.surface}; }}
    QCheckBox::indicator:checked {{ background: {c.primary}; border-color: {c.primary_pressed}; }}

    /* 文本区 */
    QPlainTextEdit, QTextEdit {{
        background: {c.surface_alt};
        border: 1px solid {c.border};
        border-radius: 8px;
        padding: 6px;
        color: {c.text};
        selection-background-color: {c.primary};
        selection-color: {c.on_primary};
    }}

    /* 表格 */
    QTableWidget, QTreeWidget {{
        background: {c.surface};
        alternate-background-color: {c.surface_alt};
        border: 1px solid {c.border};
        border-radius: 8px;
        gridline-color: {c.border};
    }}
    QHeaderView::section {{
        background: {c.surface_alt};
        color: {c.text_muted};
        border: none;
        border-bottom: 1px solid {c.border_strong};
        padding: 6px;
        font-weight: bold;
    }}
    QTableWidget::item:selected, QTreeWidget::item:selected {{
        background: {c.ok_soft}; color: {c.text};
    }}

    /* 滚动条（细一点，别抢视线） */
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: #C7CED6; border-radius: 5px; min-height: 30px; }}
    QScrollBar::handle:vertical:hover {{ background: #AEB7C2; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
    QScrollBar::handle:horizontal {{ background: #C7CED6; border-radius: 5px; min-width: 30px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
    QScrollArea {{ border: none; background: transparent; }}

    QSplitter::handle {{ background: {c.border}; }}
    QToolTip {{
        background: #24313D; color: white; border: none; border-radius: 4px; padding: 6px;
    }}
    """


def apply_theme(app) -> None:
    """把主题应用到 QApplication（字体 + QSS）。幂等，可重复调用。"""
    try:
        app.setFont(ui_font(10))
        app.setStyleSheet(stylesheet())
    except Exception as exc:  # noqa: BLE001 - 主题失败不该让程序起不来
        print(f"[warn] 应用主题失败（{type(exc).__name__}）：{exc}")


def muted(text: str) -> str:
    """次要文字的富文本包装（QLabel 用）。"""
    return f'<span style="color:{ACTIVE.text_muted};">{text}</span>'


def color(name: str) -> QColor:
    return QColor(getattr(ACTIVE, name))
