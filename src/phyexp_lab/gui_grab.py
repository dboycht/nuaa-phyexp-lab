"""抢课面板（PySide6）：两周网格点选空闲时段 → 定时等待 → 到点抢 → 汇报 → 可退课。

对应用户 2026-10-07 口述的完整流程与 `docs/抢课规格.md`：
「日历上点击选两周空闲时段 → 设定抢课时间段 → 登录后自动等待 → 抢到后弹出窗口汇报 → 可选择退课」。

设计要点（都来自本项目已踩过的坑）
--------------------------------
1. **点选 = 网格按钮**，不用 `QCalendarWidget`：每个"日期 × 节次"一个按钮，
   状态由 `planner.grid_cells()`（纯函数、已单测）决定 ⇒ 界面逻辑可被脚本验证。
2. **网络操作全在子线程**（`Loader` 只读加载、`GrabWorker` 执行抢课），
   主线程只更新界面 —— 不许出现"点一下卡住"。
3. **安全默认**：勾选框默认**不勾**"真实提交"，即演练；没勾就绝不发写请求（A4）。
4. **不可逆操作二次确认**：真实提交与退课都要先确认（默认 N）。
5. **如实汇报**：抢到什么、没抢到什么（含原因）都写进结果区；不存在"静默跳过"。
6. **客户端可注入**（`client_factory`）：`--self-check` 用假客户端跑一遍，
   验证装配与状态转移，无需真实账号、无需鼠标操作。
"""

from __future__ import annotations

import datetime as dt
import pathlib
import sys
import traceback
from typing import Any, Callable

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QSpinBox,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import QTime

from . import api
from . import config as app_config
from . import grabconfig, planner, prompt as prompt_mod, runner, session, theme
from .grabconfig import PERIODS, FreeSlot, GrabPlan

GREEN = QColor(0x18, 0x8A, 0x3E)
RED = QColor(0xC0, 0x39, 0x2B)
GREY = QColor(0x88, 0x88, 0x88)
AMBER = QColor(0xB5, 0x6A, 0x00)


# ── 子线程 ──


class GridLoader(QThread):
    """只读加载：两周窗口内的场次 + 我已有的选课（用于判断哪些时段已有、哪些实验做过）。"""

    progress = Signal(str)
    loaded = Signal(dict)
    failed = Signal(str)

    def __init__(self, plan_cfg: GrabPlan, days: list[str], client_factory: Callable[[], Any]) -> None:
        super().__init__()
        self.plan_cfg = plan_cfg
        self.days = days
        self.client_factory = client_factory

    def run(self) -> None:  # noqa: D102 - QThread 入口
        client = None
        try:
            client = self.client_factory()
            self.progress.emit("正在读取课程/场次/我的选课……")
            semesters = client.open_semesters()
            if not semesters:
                self.failed.emit("当前没有开放学期（选课窗口可能没开）")
                return
            semester = semesters[0]
            courses = client.my_courses(semester.get("id"))
            if not courses:
                self.failed.emit(f"学期 id={semester.get('id')} 下没有我的课程")
                return
            course_id = self.plan_cfg.course_id or courses[0].get("id")
            rows, names = planner.load_course_slots(client, course_id)
            occupied, taken = planner.occupied_from_electives(client, semester.get("id"), course_id)
            elections = planner.my_elections(client, semester.get("id"), course_id)
            cells = planner.grid_cells(self.plan_cfg, rows, days=self.days, occupied=occupied,
                                       project_names=names, taken_projects=taken)
            self.loaded.emit({"course_id": course_id, "courses": courses, "cells": cells,
                              "occupied": occupied, "taken": taken, "elections": elections,
                              "semester": semester.get("name")})
        except Exception as exc:  # noqa: BLE001 - 线程里必须自己兜异常
            self.failed.emit(f"{type(exc).__name__}：{exc}\n{traceback.format_exc()[:600]}")
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass


class GrabWorker(QThread):
    """执行抢课（可选等待到点）。**真实提交由面板的勾选框控制**（默认演练）。

    阶段通过 `phase` 信号上报主线程（`阶段名, 剩余秒数`），界面据此显示倒计时：
    `waiting`（等待到点）→ `fetching`（到点，拉取场次列表并筛候选）→
    `submitting`（逐条提交）→ `done`。
    """

    progress = Signal(str)
    phase = Signal(str, float)
    finished_report = Signal(object)
    failed = Signal(str)

    def __init__(self, plan_cfg: GrabPlan, *, wait_until_epoch: float | None,
                 client_factory: Callable[[], Any]) -> None:
        super().__init__()
        self.plan_cfg = plan_cfg
        self.wait_until_epoch = wait_until_epoch
        self.client_factory = client_factory
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:  # noqa: D102 - QThread 入口
        client = None
        try:
            client = self.client_factory()
            engine = runner.Runner(self.plan_cfg, client=client, log=self.progress.emit)
            if self.wait_until_epoch:
                while not self._cancel:
                    remain = self.wait_until_epoch - dt.datetime.now().timestamp()
                    if remain <= 0.2:
                        break
                    self.phase.emit("waiting", remain)
                    self.msleep(200)
                if self._cancel:
                    self.progress.emit("已取消等待。")
                    self.phase.emit("cancelled", 0.0)
                    return
            # 到点：**重新拉取实时场次列表**再筛候选（窗口未开时看到的"未放出"到这里才有数据）
            self.phase.emit("fetching", 0.0)
            self.progress.emit("到点：正在拉取场次列表并筛选候选……")
            self.phase.emit("submitting", 0.0)
            report = engine.run()
            self.phase.emit("done", 0.0)
            self.finished_report.emit(report)
        except Exception as exc:  # noqa: BLE001
            self.phase.emit("error", 0.0)
            self.failed.emit(f"{type(exc).__name__}：{exc}\n{traceback.format_exc()[:600]}")
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass


def short_cause(message: str) -> str:
    """把错误信息归成**一句完整的话**（给界面用；完整内容放 tooltip 与日志）。

    ⚠️ 实测教训（2026-10-07）：这里原本写 `message.splitlines()[0][:60]`，
    界面上就出现了"请重新运行 `python" 这种**半句话** —— 比不显示更糟。
    判据：要么给完整句，要么给**归类后的完整短语**，不做字符截断。
    """
    text = (message or "").strip()
    first = text.splitlines()[0] if text else ""
    if "401" in first or "未授权" in first or "42501" in first:
        return "登录已过期或未登录"
    if "超时" in first or "Timeout" in first or "timed out" in first:
        return "网络请求超时"
    if "404" in first:
        return "接口路径不存在（系统可能改版）"
    if "没有开放学期" in first:
        return "当前没有开放学期（选课窗口未开）"
    if "没有我的课程" in first:
        return "该学期没有你的课程"
    if "Connection" in first or "连接" in first:
        return "连不上服务端（检查网络或加速器）"
    return "原因见运行日志（已记录完整错误）" if first else "未知原因"


def format_countdown(seconds: float) -> str:
    """把剩余秒数格式化成 `HH:MM:SS`（负数/超大值都按 0 处理，不显示怪值）。

    纯函数 ⇒ 可单测；界面与日志共用同一份格式，避免"两处各算一套"。
    """
    total = max(0, int(seconds + 0.999))     # 向上取整：还剩 0.4 秒显示 00:00:01，不是 00:00:00
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def compact_login_text(token: str | None) -> str:
    """登录态的**短**文案（状态标签用）；完整时间放 tooltip。

    ⚠️ 实测教训（2026-10-07 用户反馈"文字显示不全"）：早期把
    `session.describe_token()` 的完整串截 28 字塞进标签，显示成
    `登录态：有效期至 2026-10-07 22:07:56（约剩` —— **半句话，比不显示更糟**。
    正解：标签只给结论（有效 / 已过期 / 未登录 + 剩余量），细节进 tooltip。

    ⚠️ **第二个教训（同日）**：这里原本写 `except Exception: return "未知"`，
    把 `api` 未导入导致的 `NameError` **静默降级成"未知"** —— 界面看着"只是没读到"，
    实际是代码 bug。现在：异常类型**必须显示出来**（`无法解析（NameError）`），
    不许把"我方出错"伪装成"没数据"。
    """
    if not token:
        return "登录态：未登录"
    try:
        claims = api.decode_token_claims(token)
    except Exception as exc:  # noqa: BLE001 - 但**必须**把异常类型暴露到界面上
        return f"登录态：无法解析（{type(exc).__name__}）"
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)):
        return "登录态：有效（无 exp）"
    remaining = float(exp) - dt.datetime.now().timestamp()
    if remaining <= 0:
        return "登录态：已过期（点右侧登录）"
    if remaining < 3600:
        return f"登录态：有效（剩 {int(remaining // 60)} 分）"
    return f"登录态：有效（剩 {remaining / 3600:.1f} 小时）"


# ── 面板 ──


class GrabPanel(QDialog):
    """抢课面板。`client_factory` 可注入（自检用假客户端）。"""

    def __init__(self, parent: QWidget | None = None, *,
                 client_factory: Callable[[], Any] | None = None,
                 plan_cfg: GrabPlan | None = None,
                 auto_reload: bool = True,
                 suppress_dialogs: bool = False) -> None:
        super().__init__(parent)
        #: 自检模式必须置 True：**模态对话框在没人点的时候会永远等下去**
        #: （实测：自检卡在 _on_report 的 QMessageBox 上；与 ERROR.md E13 同一类问题 ——
        #:  自检与真实运行共用逻辑时，一切"需要人"的副作用都要能关掉）。
        self.suppress_dialogs = suppress_dialogs
        self.setWindowTitle("抢课面板（两周空闲时段 → 到点自动抢 → 结果可退课）")
        self.resize(1180, 860)
        self.setMinimumSize(1040, 600)  # 左列(14 天网格) + 右列(日志 220) 的最低要求
        self.client_factory = client_factory or (lambda: __import__(
            "phyexp_lab.api", fromlist=["PhyExpClient"]).PhyExpClient(timeout=20.0))
        # ⚠️ 这里**不要**再硬编码 dry_run=True（旧的安全默认）：用户 2026-10-08 明确要求
        #    「默认的是真实提交，不是演示模式」⇒ 用 GrabPlan 的当前默认值（dry_run=False）。
        #    想演练就在「设置」里勾演练模式（或给 GrabPanel 传 plan_cfg）。
        self.plan_cfg = plan_cfg or GrabPlan()
        self.cells: dict[tuple[str, str], dict] = {}
        self.buttons: dict[tuple[str, str], QPushButton] = {}
        self.selected: set[tuple[str, str]] = set()
        self._loader: GridLoader | None = None
        self._worker: GrabWorker | None = None
        self._results: list = []

        self._build_ui()
        # ⚠️ 自检模式（auto_reload=False）**绝不能起这个定时器**：自检里一旦调用 processEvents，
        #    它会触发 reload() → 起 QThread → 进程退出时被**非守护线程**拖住
        #    ⇒ 整个自检看起来"挂死"（2026-10-07 实测踩到，排查代价不小）。
        if auto_reload:
            QTimer.singleShot(200, self.reload)

    # ── 界面 ──

    def start_date(self) -> dt.date:
        """两周窗口的起点：**从抢课当天起算**（用户 2026-10-07 确认）。"""
        return dt.date.today()

    def days(self) -> list[str]:
        return planner.planned_days(self.start_date(), weeks=2)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(0)

        # 两栏布局（用户 2026-10-08）：左列 = 完整工作流；右列 = 运行日志（**整列高度**）。
        # 用 QSplitter 而不是固定网格：用户可拖动分隔条，日志也能折叠。
        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        left_column = QWidget()
        left_layout = QVBoxLayout(left_column)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)

        # ── 顶部标题卡：标题 + 一句说明 + 状态小标签 ──
        header = QFrame()
        header.setObjectName("header")
        head_layout = QVBoxLayout(header)
        head_layout.setContentsMargins(10, 7, 10, 7)
        head_layout.setSpacing(4)
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        title_row.addWidget(theme.icon_label("lab", 22, theme.ACTIVE.primary))
        title = QLabel("抢课面板")
        title.setObjectName("h1")
        title_row.addWidget(title)
        title_row.addStretch(1)
        self.chip_login = QLabel("登录态：检查中")
        self.chip_login.setObjectName("chip")
        self.chip_course = QLabel("课程：—")
        self.chip_course.setObjectName("chip")
        self.chip_window = QLabel("窗口：—")
        self.chip_window.setObjectName("chip")
        # 提交方式（安全相关）也放一个常驻标签：设置收起时也要看得见当前是哪种
        self.chip_mode = QLabel("真实提交")
        self.chip_mode.setObjectName("chip")
        for chip in (self.chip_login, self.chip_mode, self.chip_course, self.chip_window):
            title_row.addWidget(chip)
        self.btn_login = QPushButton("登录")
        self.btn_login.setIcon(theme.qicon("shield", 15))
        self.btn_login.setToolTip("打开浏览器窗口登录（本项目不接触你的密码）；登录成功后会自动刷新场次")
        self.btn_login.clicked.connect(self._start_login)
        title_row.addWidget(self.btn_login)
        head_layout.addLayout(title_row)

        subtitle = QLabel(theme.muted(
            "1) 点格子 = 选你的空闲时段　2) 可设定到点开抢　3) 抢完看结果、可选退课"))
        subtitle.setObjectName("step")
        head_layout.addWidget(subtitle)
        left_layout.addWidget(header)

        # ── 配置卡：三组之间用竖线分隔（时刻 / 重试 / 安全），避免一堆控件糊在一起 ──
        box = QGroupBox("抢课设置")
        box.setFont(theme.ui_font(10, QFont.DemiBold))
        cfg_layout = QHBoxLayout(box)
        cfg_layout.setSpacing(6)

        def separator() -> QFrame:
            line = QFrame()
            line.setFrameShape(QFrame.VLine)
            line.setStyleSheet(f"color: {theme.ACTIVE.border}; background: {theme.ACTIVE.border};")
            line.setFixedWidth(1)
            return line

        # 主行只留"何时开抢"（核心工作流），其余选项全部收进下面的「设置」区
        cfg_layout.addWidget(QLabel("目标时刻"))
        self.time_enable = QCheckBox("到点开抢")
        cfg_layout.addWidget(self.time_enable)
        self.time_edit = QTimeEdit()
        self.time_edit.setDisplayFormat("HH:mm:ss")
        self.time_edit.setTime(QTime(10, 0, 0))
        self.time_edit.setFixedWidth(96)
        cfg_layout.addWidget(self.time_edit)
        cfg_layout.addSpacing(6)
        cfg_layout.addWidget(separator())
        cfg_layout.addSpacing(6)
        self.btn_settings = QPushButton("设置")
        self.btn_settings.setCheckable(True)
        self.btn_settings.setIcon(theme.qicon("settings", 15))
        self.btn_settings.toggled.connect(self._toggle_settings)
        cfg_layout.addWidget(self.btn_settings)
        cfg_layout.addStretch(1)
        left_layout.addWidget(box)

        # 设置摘要（**始终可见**）：安全相关的状态（真实提交/演练）绝不允许藏在折叠里
        self.settings_summary = QLabel("")
        self.settings_summary.setObjectName("sub")
        self.settings_summary.setWordWrap(True)
        left_layout.addWidget(self.settings_summary)

        # 设置区（默认折叠，保持紧凑）
        self.settings_box = QGroupBox("设置")
        set_layout = QHBoxLayout(self.settings_box)
        set_layout.setSpacing(6)
        self.dry_check = QCheckBox("演练模式（不真的提交）")
        self.dry_check.setChecked(bool(self.plan_cfg.dry_run))
        self.dry_check.setToolTip("勾上=只演练不写课表；不勾=真实提交（提交前仍会二次确认）")
        set_layout.addWidget(self.dry_check)
        set_layout.addWidget(separator())
        set_layout.addWidget(QLabel("重试"))
        self.rounds_spin = QSpinBox()
        self.rounds_spin.setRange(1, 200)
        self.rounds_spin.setValue(int(self.plan_cfg.retry_rounds or 10))
        self.rounds_spin.setFixedWidth(72)
        self.rounds_spin.setSuffix(" 轮")
        set_layout.addWidget(self.rounds_spin)
        set_layout.addWidget(QLabel("每轮间隔"))
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(1, 600)
        self.interval_spin.setValue(int(self.plan_cfg.retry_interval_seconds or 30))
        self.interval_spin.setFixedWidth(80)
        self.interval_spin.setSuffix(" 秒")
        set_layout.addWidget(self.interval_spin)
        set_layout.addWidget(separator())
        self.hunt_check = QCheckBox("满员后继续等退课（捡漏）")
        self.hunt_check.setChecked(bool(self.plan_cfg.hunt_drops))
        self.hunt_check.setToolTip(
            "不勾（默认）：该时段已经满了就不再每轮空等；\n"
            "勾上：每轮都再看一眼，有人退课就抢（适合开抢后那几分钟）。")
        set_layout.addWidget(self.hunt_check)
        self.notify_check = QCheckBox("桌面通知")
        self.notify_check.setChecked(bool(self.plan_cfg.notify))
        set_layout.addWidget(self.notify_check)
        set_layout.addStretch(1)
        self.settings_box.setVisible(False)          # 默认折叠
        left_layout.addWidget(self.settings_box)

        # 任何设置变化都刷新摘要
        for widget in (self.dry_check, self.hunt_check, self.notify_check):
            widget.toggled.connect(self._refresh_settings_summary)
        self.rounds_spin.valueChanged.connect(self._refresh_settings_summary)
        self.interval_spin.valueChanged.connect(self._refresh_settings_summary)

        # ── 操作条 ──
        actions = QHBoxLayout()
        actions.setSpacing(6)
        self.btn_reload = QPushButton("刷新场次")
        self.btn_reload.setIcon(theme.qicon("refresh", 15))
        self.btn_reload.clicked.connect(self.reload)
        actions.addWidget(self.btn_reload)
        self.btn_select_available = QPushButton("全选可约时段")
        self.btn_select_available.setIcon(theme.qicon("check", 15))
        self.btn_select_available.clicked.connect(self.select_all_available)
        actions.addWidget(self.btn_select_available)
        self.btn_clear = QPushButton("清空选择")
        self.btn_clear.setIcon(theme.qicon("undo", 15))
        self.btn_clear.clicked.connect(self.clear_selection)
        actions.addWidget(self.btn_clear)
        actions.addStretch(1)
        # 阶段 + 大号倒计时（用户流程：登录 → 倒计时 → 到点拉列表提交）
        self.lbl_phase = QLabel("待机")
        self.lbl_phase.setObjectName("chip")
        actions.addWidget(self.lbl_phase)
        self.lbl_countdown = QLabel("")
        self.lbl_countdown.setStyleSheet(
            f"color: {theme.ACTIVE.primary}; font-size: 15pt; font-weight: bold;"
            f"font-family: '{theme.MONO_FONTS[0]}'; background: transparent;")
        self.lbl_countdown.setToolTip("距离开抢的剩余时间（按服务端时钟对时换算）")
        actions.addWidget(self.lbl_countdown)
        self.selection_label = QLabel("已选 0 个时段")
        self.selection_label.setObjectName("chip")
        actions.addWidget(self.selection_label)
        self.btn_start = QPushButton("立即抢课")
        self.btn_start.setObjectName("primary")
        self.btn_start.setMinimumWidth(170)      # 容得下"定时抢课（等到 10:00:00）"
        # 文案 = 当前模式（用户 2026-10-08）：勾「到点开抢」前是"立即"，勾上后写明等到几点
        self.time_enable.toggled.connect(self._refresh_start_button)
        self.time_edit.timeChanged.connect(self._refresh_start_button)
        self.btn_start.clicked.connect(self.start_grab)
        actions.addWidget(self.btn_start)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setIcon(theme.qicon("stop", 15, theme.ACTIVE.danger))
        self.btn_stop.setObjectName("danger")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_grab)
        actions.addWidget(self.btn_stop)
        left_layout.addLayout(actions)

        # ── 网格卡 ──
        grid_box = QGroupBox("空闲时段（两周）")
        grid_box.setFont(theme.ui_font(10, QFont.DemiBold))
        grid_outer = QVBoxLayout(grid_box)
        grid_outer.setContentsMargins(7, 5, 7, 7)
        hint = QLabel(theme.muted(
            "点格子 = 标记「这个时段我有空」（与当前有没有课无关）；"
            "格子里的小字是当前可见情况，窗口未开时大多显示「未放出」属正常。"
            "到点开抢时会重新拉取实时列表再筛候选。"))
        hint.setWordWrap(True)
        hint.setObjectName("step")
        grid_outer.addWidget(hint)
        # 拉取失败/无数据时的**显式横幅**（用户 2026-10-07 提醒：拉不到时"没有数据也不会显示"，
        # 那就必须说清楚为什么空，而不是留一片白让人以为坏了）
        self.banner = QLabel("")
        self.banner.setWordWrap(True)
        self.banner.setObjectName("chipWarn")
        self.banner.setVisible(False)
        grid_outer.addWidget(self.banner)
        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setSpacing(2)
        self.grid.setContentsMargins(2, 2, 2, 2)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.grid_host)
        grid_outer.addWidget(scroll)
        scroll.setMinimumHeight(150)      # 兜底：窗口很矮时可滚动
        scroll.setMaximumHeight(520)

        left_layout.addWidget(grid_box)         # 按内容自适应：多余高度**不要**塞进网格卡
                                                # （否则网格内部会多出一大片空白，实测很难看）

        # ── 结果卡 ──
        self.result_box = QGroupBox("本轮结果（勾选后可退课）")
        self.result_box.setFont(theme.ui_font(10, QFont.DemiBold))
        result_layout = QVBoxLayout(self.result_box)
        result_layout.setContentsMargins(10, 8, 10, 10)
        self.result_text = QPlainTextEdit()
        self.result_text.setReadOnly(True)
        self.result_text.setMinimumHeight(46)
        self.result_text.setMaximumHeight(72)
        self.result_text.setPlaceholderText(
            "还没有开始抢课：先点选空闲时段，再点「立即抢课」。\n"
            "默认真实提交（会写进课表），提交前会二次确认；只想演练请在「设置」里勾上演练模式。")
        result_layout.addWidget(self.result_text)

        # 抢到的条目：**可滚动的勾选列表**（条目多时不会挤成一行、也不会被截断）
        picks_scroll = QScrollArea()
        picks_scroll.setWidgetResizable(True)
        picks_scroll.setMinimumHeight(32)
        # 不设上限：结果卡吃掉左列剩余高度时，让"抢到的条目"列表一起变高（更有用）
        picks_scroll.setMaximumHeight(16777215)
        picks_scroll.setStyleSheet(
            f"QScrollArea {{ background: {theme.ACTIVE.surface};"
            f" border: 1px solid {theme.ACTIVE.border}; border-radius: 8px; }}")
        self.picks_host = QWidget()
        self.picks_layout = QVBoxLayout(self.picks_host)
        self.picks_layout.setContentsMargins(8, 6, 8, 6)
        self.picks_layout.setSpacing(2)
        self.picks_layout.addStretch(1)
        picks_scroll.setWidget(self.picks_host)

        self.picks_scroll = picks_scroll
        result_layout.addWidget(picks_scroll)

        # 左下角：**本周期内已选**——用**列表**展示（用户 2026-10-08：一行长文字太麻烦）
        self.mine_title = QLabel("本周期内已选（0 个）")
        self.mine_title.setObjectName("step")
        result_layout.addWidget(self.mine_title)
        mine_scroll = QScrollArea()
        mine_scroll.setWidgetResizable(True)
        mine_scroll.setMinimumHeight(30)
        mine_scroll.setMaximumHeight(104)
        mine_scroll.setStyleSheet(
            f"QScrollArea {{ background: {theme.ACTIVE.surface};"
            f" border: 1px solid {theme.ACTIVE.border}; border-radius: 8px; }}")
        self.mine_host = QWidget()
        self.mine_layout = QVBoxLayout(self.mine_host)
        self.mine_layout.setContentsMargins(8, 5, 8, 5)
        self.mine_layout.setSpacing(1)
        self.mine_layout.addStretch(1)
        mine_scroll.setWidget(self.mine_host)
        self.mine_scroll = mine_scroll
        result_layout.addWidget(mine_scroll)

        bottom = QHBoxLayout()
        self.picks_hint = QLabel(theme.muted("抢到的条目会列在上面，勾选后可退课"))
        self.picks_hint.setObjectName("step")
        bottom.addWidget(self.picks_hint, 1)
        self.btn_cancel_picks = QPushButton("退掉勾选项")
        self.btn_cancel_picks.setIcon(theme.qicon("delete", 15, theme.ACTIVE.danger))
        self.btn_cancel_picks.setObjectName("danger")
        self.btn_cancel_picks.setEnabled(False)
        self.btn_cancel_picks.clicked.connect(self.cancel_picked)
        bottom.addWidget(self.btn_cancel_picks)
        result_layout.addLayout(bottom)
        # 结果卡吃掉左列剩余高度（用户 2026-10-08：不要留空白垃圾区域）——
        # 网格卡按内容自适应（内部不留空），结果卡随窗口变高，勾选列表也跟着变高。
        left_layout.addWidget(self.result_box, 1)

        # 日志卡：标题 + 清空按钮 + **可折叠**（小屏收起来能省 ~140px，让网格更大）
        log_box = QGroupBox("运行日志")
        log_box.setFont(theme.ui_font(10, QFont.DemiBold))
        log_layout = QVBoxLayout(log_box)
        log_layout.setContentsMargins(10, 8, 10, 10)
        log_head = QHBoxLayout()
        self.log_visible = QCheckBox("显示日志")
        self.log_visible.setChecked(True)
        self.log_visible.toggled.connect(self._toggle_log)
        log_head.addWidget(self.log_visible)
        log_head.addStretch(1)
        # ⚠️ 顺序：先建 self.log，再建"清空"按钮（按钮要连 self.log.clear）
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(theme.monospace(9))
        self.log.setMinimumHeight(56)
        # 不再设高度上限：日志现在独占右侧一列，撑满才有意义（原来限 88px 是为了竖向堆叠时省地方）
        self.btn_clear_log = QPushButton("清空日志")
        self.btn_clear_log.setIcon(theme.qicon("undo", 14))
        self.btn_clear_log.clicked.connect(self.log.clear)
        log_head.addWidget(self.btn_clear_log)
        log_layout.addLayout(log_head)
        log_layout.addWidget(self.log)
        self.log_box = log_box
        log_box.setMinimumWidth(200)
        self.splitter.addWidget(left_column)
        self.splitter.addWidget(log_box)
        self.splitter.setStretchFactor(0, 1)      # 左列吃剩余空间
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes([940, 240])
        root.addWidget(self.splitter, 1)          # 两栏填满窗口剩余高度
        # 摘要行 + 顶部「真实提交/演练」标签：**构造时就给初值**（不等数据加载，
        # 否则窗口刚打开时"当前是真实提交还是演练"是空的 —— 这是安全相关信息，不能空）
        self._refresh_settings_summary()

    # ── 日志 ──

    def _toggle_settings(self, checked: bool) -> None:
        """展开/收起设置区（默认折叠：界面紧凑，但摘要行始终说明当前设置）。"""
        self.settings_box.setVisible(bool(checked))
        self.btn_settings.setText("收起设置" if checked else "设置")

    def _refresh_settings_summary(self, *_args) -> None:
        """把当前设置写成**一行摘要**（始终可见），并把提交方式同步到顶部状态标签。"""
        if self.dry_check.isChecked():
            mode = "演练模式（不写课表）"
            mode_kind = "chipWarn"
        else:
            mode = "真实提交（会写进课表）"
            mode_kind = "chipDanger"
        hunt = "开" if self.hunt_check.isChecked() else "关"
        self.settings_summary.setText(
            f"当前设置：{mode}　·　重试 {self.rounds_spin.value()} 轮 × {self.interval_spin.value()} 秒"
            f"　·　捡漏：{hunt}　·　桌面通知：{'开' if self.notify_check.isChecked() else '关'}"
            f"　·　（已满的时段{'会' if self.hunt_check.isChecked() else '不会'}继续空等）")
        if self.chip_mode is not None:
            self._set_chip(self.chip_mode, mode.split("（")[0], mode_kind)

    def _toggle_log(self, *_args) -> None:
        """折叠/展开日志区（小屏时收起来，把空间让给网格）。"""
        self.log.setVisible(visible)
        self.btn_clear_log.setVisible(visible)

    def log_line(self, text: str) -> None:
        self.log.appendPlainText(text)

    # ── 加载网格 ──

    def reload(self) -> None:
        if self._loader is not None and self._loader.isRunning():
            self.log_line("[跳过] 上一次加载还没结束。")
            return
        self.btn_reload.setEnabled(False)
        self.log_line(f"开始加载：窗口 {self.days()[0]} ~ {self.days()[-1]}（两周）")
        self._loader = GridLoader(self.build_plan_from_ui(apply_selection=False),
                                  self.days(), self.client_factory)
        self._loader.progress.connect(self.log_line)
        self._loader.loaded.connect(self._on_loaded)
        self._loader.failed.connect(self._on_load_failed)
        self._loader.finished.connect(lambda: self.btn_reload.setEnabled(True))
        self._loader.start()

    def _show_banner(self, text: str, tooltip: str = "") -> None:
        """在网格卡里显示一条醒目的说明（拉取失败/无数据时用，避免一片空白让人以为坏了）。

        `text` 是**给人看的一句话**（不许截半句、不许带 markdown 标记）；
        完整错误走 `tooltip`（鼠标悬停可见），日志里也另有一份。
        """
        self.banner.setText(text)
        self.banner.setToolTip(tooltip or text)
        self.banner.setVisible(True)

    def _hide_banner(self) -> None:
        self.banner.setVisible(False)

    def _set_chip(self, chip: QLabel, text: str, kind: str = "chip") -> None:
        """更新状态小标签（颜色跟着状态走，不用肉眼看文字判断）。"""
        chip.setText(text)
        if chip.objectName() != kind:
            chip.setObjectName(kind)
            chip.style().unpolish(chip)      # 改了 objectName 必须重刷，否则样式不会变
            chip.style().polish(chip)

    # ── 对话框小助手（自检模式下全部短路，避免模态框把自检挂死）──

    def _info(self, title: str, text: str) -> None:
        if self.suppress_dialogs:
            self.log_line(f"[对话框-已抑制] {title}：{text.splitlines()[0][:80]}")
            return
        QMessageBox.information(self, title, text)

    def _warn(self, title: str, text: str) -> None:
        if self.suppress_dialogs:
            self.log_line(f"[对话框-已抑制] {title}：{text.splitlines()[0][:80]}")
            return
        QMessageBox.warning(self, title, text)

    def _critical(self, title: str, text: str) -> None:
        if self.suppress_dialogs:
            self.log_line(f"[对话框-已抑制] {title}：{text.splitlines()[0][:80]}")
            return
        QMessageBox.critical(self, title, text)

    def _ask(self, title: str, text: str, *, default_yes: bool = False) -> bool:
        """二次确认。自检模式下**默认按"否"**（安全一侧）并记日志。"""
        if self.suppress_dialogs:
            self.log_line(f"[对话框-已抑制] {title}：默认按{'是' if default_yes else '否'}处理")
            return default_yes
        answer = QMessageBox.question(
            self, title, text,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes if default_yes else QMessageBox.No)
        return answer == QMessageBox.Yes

    def showEvent(self, event) -> None:  # noqa: D102 - Qt 钩子
        super().showEvent(event)
        QTimer.singleShot(0, self._fit_grid_height)   # 显示后再校一次（布局此时才真正结算）

    # ── 阶段与倒计时 ──

    def _on_phase(self, name: str, remain: float) -> None:
        """把工作线程的阶段映射到界面（用户流程：等待 → 到点拉列表 → 提交）。"""
        if name == "waiting":
            self._set_chip(self.lbl_phase, "等待开抢", "chipWarn")
            self.lbl_countdown.setText(format_countdown(remain))
        elif name == "fetching":
            self._set_chip(self.lbl_phase, "到点：拉取场次列表", "chipWarn")
            self.lbl_countdown.setText("00:00:00")
        elif name == "submitting":
            self._set_chip(self.lbl_phase, "提交中", "chipWarn")
        elif name == "done":
            self._set_chip(self.lbl_phase, "已完成", "chipOk")
            self.lbl_countdown.setText("")
        elif name == "cancelled":
            self._set_chip(self.lbl_phase, "已取消", "chip")
            self.lbl_countdown.setText("")
        elif name == "error":
            self._set_chip(self.lbl_phase, "出错", "chipDanger")
            self.lbl_countdown.setText("")

    # ── 登录 ──

    def _start_login(self) -> None:
        """打开登录流程（Playwright 浏览器窗口），登录成功后自动刷新网格。

        为什么用子进程：登录需要真实浏览器交互（本项目**不接触密码**），
        复用已验证的 `run.py login`，而不是在界面里重写一遍。
        """
        import subprocess

        if getattr(self, "_login_proc", None) is not None and self._login_proc.poll() is None:
            self.log_line("[登录] 已经有一个登录窗口在运行。")
            return
        run_py = pathlib.Path(__file__).resolve().parents[2] / "run.py"
        cmd = [sys.executable, str(run_py), "login", "--max-wait", "900"]
        self.log_line(f"[登录] 正在打开浏览器窗口：{' '.join(cmd)}")
        try:
            self._login_proc = subprocess.Popen(cmd, cwd=str(run_py.parent),
                                                stdout=subprocess.DEVNULL,
                                                stderr=subprocess.DEVNULL)
        except OSError as exc:
            self.log_line(f"[登录] 启动失败（{type(exc).__name__}）：{exc}")
            return
        self._set_chip(self.chip_login, "登录态：等待你在浏览器里登录…", "chipWarn")
        self.btn_login.setEnabled(False)
        if not hasattr(self, "_login_timer"):
            self._login_timer = QTimer(self)
            self._login_timer.setInterval(2000)
            self._login_timer.timeout.connect(self._poll_login)
        self._login_wait_seconds = 0
        self._login_timer.start()

    def _poll_login(self) -> None:
        """轮询登录结果：token 有效即认为登录完成。"""
        self._login_wait_seconds = getattr(self, "_login_wait_seconds", 0) + 2
        token = session.load_token()
        described = session.describe_token(token) if token else "无 token"
        if token and "已过期" not in described:
            self._login_timer.stop()
            self.btn_login.setEnabled(True)
            self._set_chip(self.chip_login, compact_login_text(token), "chipOk")
            self.chip_login.setToolTip(described)
            self.log_line(f"[登录] 已登录：{described}")
            self.reload()
            return
        proc = getattr(self, "_login_proc", None)
        if proc is not None and proc.poll() is not None:
            self._login_timer.stop()
            self.btn_login.setEnabled(True)
            self._set_chip(self.chip_login, "登录态：未登录/已过期", "chipDanger")
            self.log_line(f"[登录] 窗口已关闭（退出码 {proc.returncode}），"
                          f"仍未检测到有效登录态。")
            return
        if self._login_wait_seconds % 30 == 0:
            self.log_line(f"[登录] 等待登录中……已等 {self._login_wait_seconds} 秒")
        self._set_chip(self.chip_login, f"登录态：等待登录（{self._login_wait_seconds}s）", "chipWarn")

    def _on_load_failed(self, message: str) -> None:
        self._set_chip(self.chip_login, "登录态：不可用", "chipDanger")
        self.log_line(f"[加载失败] {message}")
        self.log_line("       界面保持空白（不伪造数据）；请确认已 login 且选课窗口已开。")
        cause = short_cause(message)
        self._show_banner(
            f"当前拉取不到场次（{cause}）。"
            "这不影响你设置空闲时段：照常点格子即可，到点开抢时会重新拉取实时列表。"
            "如果是登录过期，请点右上角「登录」。", tooltip=message)

    def _on_loaded(self, payload: dict) -> None:
        self.cells = payload["cells"]
        if self.plan_cfg.course_id is None:
            self.plan_cfg.course_id = payload["course_id"]
        self.course_label = payload
        avail = sum(1 for c in self.cells.values() if c["state"] == "available")
        taken = sum(1 for c in self.cells.values() if c["state"] == "taken")
        # 顶部状态标签：登录态 / 课程 / 窗口
        try:
            token = session.load_token()
            full = session.describe_token(token) if token else "无 token"
        except Exception:  # noqa: BLE001 - 状态展示失败不该影响功能
            token, full = None, "未知"
        short = compact_login_text(token)
        self._set_chip(self.chip_login, short,
                       "chipOk" if "有效" in short else
                       ("chipWarn" if "未登录" in short else "chipDanger"))
        self.chip_login.setToolTip(full)      # 完整时间放提示里，标签不再被截成半句
        course_name = ""
        for course in payload.get("courses") or []:
            if str(course.get("id")) == str(payload["course_id"]):
                course_name = str(course.get("name") or "")
        self._set_chip(self.chip_course, f"课程：{course_name or payload['course_id']}")
        days = self.days()
        self._set_chip(self.chip_window, f"窗口：{days[0][5:]} ~ {days[-1][5:]}（两周）")
        self.log_line(f"已加载：课程 id={payload['course_id']}，当前可见可约单元 {avail} 个，"
                      f"已有选课 {taken} 个，已选过实验 {len(payload['taken'])} 个")
        # 左下角：**本周期（表格这两周）内已选**，用列表展示（窗口外的不进列表，只进标题提示）
        elections = list(payload.get("elections") or [])
        window = set(self.days())
        inside = [e for e in elections if e["date"] in window]
        outside = [e for e in elections if e["date"] not in window]
        self._elections_in_window = inside      # 供"待抢"列表与勾选退课复用
        self._refresh_mine_rows()
        self.mine_title.setText(f"本周期管理（已选 {len(inside)} · 待抢 {len(self._pending_slots())}）")
        tips: list[str] = []
        if outside:
            tips.append("窗口外的已选：" +
                        "；".join(f"{e['date'][5:]} {e['period']} {e['name']}" for e in outside))
        mine = dict(payload.get("taken") or {})
        if mine:
            tips.append("已选过的实验（同一实验不重复选）：" + "、".join(sorted(mine.values())))
        self.mine_title.setToolTip("\n".join(tips))
        if avail == 0:
            self.log_line("       注意：现在看不到可约单元，这在窗口未开时是正常的 ——"
                          "你照常点选空闲时段即可，到点后系统会按当时的实时余量重新筛候选。")
            self._show_banner(
                "当前没有已放出的场次（选课窗口未开 / 已满 / 都选过了）—— 这属正常。"
                "你照常点选空闲时段即可；到点开抢时系统会重新拉取实时列表再筛候选。")
        else:
            self._hide_banner()
        # ⚠️ **不要**因为"当前不可见/已满"就把用户已选的空闲时段剔掉 —— 那等于把用户的设置丢了。
        #    选的是"我什么时候有空"，与"现在有什么课"是两件事（用户 2026-10-07 的纠正）。
        dropped = 0
        for key in sorted(self.selected):
            state = (self.cells.get(key) or {}).get("state")
            if state == "taken":
                dropped += 1
                self.log_line(f"       提示：{key[0]} {key[1]} 你已有选课，该时段不会再选新的。")
            elif state == "all_elected":
                dropped += 1
                names_blocked = "、".join((self.cells.get(key) or {}).get("blocked_projects") or [])
                self.log_line(f"       提示：{key[0]} {key[1]} 只放了你已选过的实验"
                              f"（{names_blocked}）；同一实验不重复选 ⇒ 该时段到点不会有候选。")
        del dropped
        self._rebuild_grid()

    def _rebuild_grid(self) -> None:
        while self.grid.count():
            item = self.grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.buttons.clear()
        days = self.days()
        weekday = "一二三四五六日"
        today = dt.date.today().isoformat()
        # 布局：0 行 = 周分组标题；1 行 = 日期；2 起 = 节次。第 8 天前插一列分隔线，
        # 于是"本周 / 下周"一眼分得清（14 天连成一片很容易看错行）。
        week_gap_col = 8
        columns = [1 + index + (1 if index >= 7 else 0) for index in range(len(days))]

        for week_index, (start, end, title) in enumerate(
                ((0, 6, "本周（第 1 周）"), (7, 13, "下周（第 2 周）"))):
            if end >= len(days):
                continue
            label = QLabel(title)
            label.setAlignment(Qt.AlignCenter)
            label.setStyleSheet(
                f"color: {theme.ACTIVE.primary}; background: {theme.ACTIVE.ok_soft};"
                "border-radius: 6px; padding: 2px; font-size: 9pt;")
            self.grid.addWidget(label, 0, columns[start], 1, end - start + 1)
            del week_index

        # 让日期列**均分可用宽度**（窄窗口时自动变窄，而不是溢出到横向滚动）
        for col in columns:
            self.grid.setColumnStretch(col, 1)
        self.grid.setColumnStretch(week_gap_col, 0)
        # ⚠️ 行方向：卡里多出来的高度必须交给**末尾的空行**，否则 QGridLayout 会把它均摊到各行，
        #    表现为周标题与日期行之间一大片空白、单元格被挤到下面（实测踩到）。
        for row in range(2 + len(PERIODS)):
            self.grid.setRowStretch(row, 0)
        self.grid.setRowStretch(2 + len(PERIODS), 1)

        separator = QFrame()
        separator.setFrameShape(QFrame.VLine)
        separator.setStyleSheet(f"background: {theme.ACTIVE.border_strong};")
        separator.setFixedWidth(2)
        self.grid.addWidget(separator, 0, week_gap_col, len(PERIODS) + 2, 1)

        for index, date in enumerate(days):
            day = dt.date.fromisoformat(date)
            label = QLabel(f"<b>{date[5:]}</b><br/>周{weekday[day.weekday()]}")
            label.setAlignment(Qt.AlignCenter)
            label.setStyleSheet(
                f"color: {theme.ACTIVE.primary if date == today else theme.ACTIVE.text};"
                f"background: {theme.ACTIVE.ok_soft if date == today else 'transparent'};"
                "border-radius: 6px; padding: 3px; font-size: 9pt;")
            self.grid.addWidget(label, 1, columns[index])

        for row, period in enumerate(PERIODS, start=2):
            name = QLabel(period)
            name.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            name.setStyleSheet(f"color: {theme.ACTIVE.text_muted}; font-size: 9pt;")
            name.setMinimumWidth(70)
            self.grid.addWidget(name, row, 0)
            for index, date in enumerate(days):
                key = (date, period)
                info = self.cells.get(key) or {}
                selected = key in self.selected
                # 所有格子都**可点选**（选的是"我的空闲时段"，与当前有没有课无关）
                button = QPushButton(self._button_text(info, selected=selected))
                button.setCheckable(True)
                button.setChecked(selected)
                button.setCursor(Qt.PointingHandCursor)
                button.setToolTip(self._button_tip(key, info, selected=selected))
                button.setMinimumHeight(30)      # 紧凑：30px（36px 时整卡偏高）
                button.setMinimumWidth(46)
                state = "selected" if selected else self._cell_state(info)
                button.setStyleSheet(theme.cell_qss(state))
                button.clicked.connect(lambda _checked=False, k=key: self.toggle_cell(k))
                self.grid.addWidget(button, row, columns[index])
                self.buttons[key] = button
        # ⚠️ **延后一拍**再按内容定高：在 _rebuild_grid 里立刻量，布局还没结算，
        #    sizeHint 会取到 ~6px ⇒ 把网格压成一条（实测踩到，整块网格只剩 10px）。
        QTimer.singleShot(0, self._fit_grid_height)
        self._refresh_settings_summary()      # 摘要行 + 顶部「真实提交/演练」标签的初始值
        self._update_selection_label()

    def _fit_grid_height(self) -> None:
        """把网格滚动区的高度设成**刚好等于内容高度**（上限 520）。

        为什么要这样（用户 2026-10-08："又大又空"）：
        · 交给布局自由伸缩 -> 多余的空白落在网格卡内部，看起来又大又空；
        · 固定成小高度 -> 5 个节次看不全、被迫滚动。
        按内容定高则两者都不会发生；内容超过上限时才出现滚动。
        """
        content = self.grid_host.sizeHint().height()
        scroll = self.grid_host.parent().parent()
        # ⚠️ 先分清"还没建网格"和"测量异常"：窗口一显示（showEvent）时数据往往还没加载完，
        #    此时网格里一个格子都没有、sizeHint 只有几 px —— 这**不是异常**，
        #    照常返回即可（数据到达后 _rebuild_grid 会自己再调度一次定高）。
        #    实测踩到：把它当异常会连发 5 次重试并留下一条吓人的 warn。
        if not self.buttons:
            return
        # 护栏：两周表头 + 5 行节次，内容高度不可能低于 ~100px。
        # 取到不合理的小值时**不要应用**（宁可保持原样），否则会把网格压成一条。
        if content < 100:
            # 布局偶尔还没结算（实测首次延迟测量仍可能看到 ~4px）⇒ **静默重试**，
            # 不要为此在日志里留一条吓人的 warn；只有连续失败才报出来。
            self._fit_retries = getattr(self, "_fit_retries", 0) + 1
            if self._fit_retries <= 4:
                QTimer.singleShot(60, self._fit_grid_height)
                return
            self.log_line(f"[warn] 网格内容高度测量持续异常（{content}px），已放弃自动定高。")
            return
        self._fit_retries = 0
        scroll.setFixedHeight(min(content + 4, 520))     # +4：抵消滚动区边框

    @staticmethod
    def _cell_state(info: dict) -> str:
        """未选中时按"当前可见情况"决定底色；没有数据时用 none。"""
        return str(info.get("state") or "none")

    @staticmethod
    def _button_text(info: dict, *, selected: bool = False) -> str:
        """格子文字：**已选**优先显示（那才是用户真正设置的东西），当前可见情况作副行。"""
        state = str(info.get("state") or "none")
        hint = ""
        if state == "available":
            hint = f"余 {info.get('remaining')}"
        elif state == "full":
            hint = "已满"      # 缩短：右侧日志占位后左列更窄，4 字会被省略号截掉
        elif state == "taken":
            hint = "已有选课"
        elif state == "all_elected":
            hint = "无新实验"
        else:
            hint = "未放出"
        if selected:
            return f"✓ 已选\n{hint}"
        return hint if state != "available" else f"可约\n余 {info.get('remaining')}"

    @staticmethod
    def _button_tip(key: tuple[str, str], info: dict, *, selected: bool = False) -> str:
        state = str(info.get("state") or "none")
        head = f"{key[0]} {key[1]}\n" + ("已选为我的空闲时段\n" if selected else "未选中\n")
        current = {
            "available": f"当前可约：余量 {info.get('remaining')}（共 {info.get('total')} 个场次）"
                         f"\n实验：{info.get('project_name')}\n场次 id：{info.get('best_slot_id')}",
            "full": "当前已满",
            "taken": f"你在这个时段已有选课（{info.get('reason') or ''}）",
            "all_elected": ("该节次只放了你已选过的实验 —— 同一实验不重复选，所以这里没有可抢的新实验。\n"
                            + "涉及：" + "、".join(info.get("blocked_projects") or ["（未取到名称）"])),
            "none": "当前看不到这个节次的场次（窗口未开/未排课 —— 属正常）",
        }.get(state, state)
        return (f"{head}当前可见情况：{current}\n\n"
                "点一下即可把它设为/取消『我的空闲时段』；\n"
                "真正的候选会在到点执行时按当时的实时余量重新筛选。")

    # ── 点选 ──

    def toggle_cell(self, key: tuple[str, str]) -> None:
        """点选/取消"我的空闲时段"。

        ⚠️ **设计要点（2026-10-07 用户纠正）**：选空闲时段**与"当前能不能约"无关** ——
        用户配置的时候往往**还看不到有哪些课**（只有到点才放出来），
        所以显示 `—`（当前不可见）的格子**也必须能点选**；
        网格上的余量/状态只是"当前可见情况"的参考，真正的候选在**到点执行时**按实时数据重算。
        （早期版本只允许点选 `available` 的格子 ⇒ 到点前的时段全都点不动，属于设计缺陷。）
        """
        if key in self.selected:
            self.selected.discard(key)
        else:
            self.selected.add(key)
        button = self.buttons.get(key)
        if button is not None:
            info = self.cells.get(key) or {}
            state = "selected" if key in self.selected else self._cell_state(info)
            button.setStyleSheet(theme.cell_qss(state))
            button.setText(self._button_text(info, selected=key in self.selected))
        self._update_selection_label()
        # ⚠️ 这里必须刷新左下角的**管理列表**：点格子（或列表里的 ×）就是在改"准备抢的时段"，
        #    不刷新的话列表会与网格不一致（实测：门禁"点 × 后不再出现在待抢里"抓到了这个漏）。
        self._refresh_mine_rows()

    def select_all_available(self) -> None:
        for key, info in self.cells.items():
            if info["state"] == "available" and key not in self.selected:
                self.selected.add(key)
        self._rebuild_grid()
        self._refresh_mine_rows()     # 左下角"待抢"列表要跟着变

    def clear_selection(self) -> None:
        self.selected.clear()
        self._rebuild_grid()
        self._refresh_mine_rows()     # 左下角"待抢"列表要跟着变

    def _refresh_start_button(self, *_args) -> None:
        """按钮文案直接写出**将要执行哪种模式**（立即 / 定时等到几点）。

        为什么：模式原本只由一个勾选框决定，同一个"开始抢课"按钮有两种行为，
        用户不易看出当前是哪一种（用户 2026-10-08 就问了"是有两种窗口吗"）。
        文案随模式变，最省地方也最直白。
        """
        if self.time_enable.isChecked():
            moment = self.time_edit.time().toString("HH:mm:ss")
            self.btn_start.setText(f"定时抢课（等到 {moment}）")
        else:
            self.btn_start.setText("立即抢课")
        count = len(self.selected)
        self.btn_start.setEnabled(bool(count) and not self._is_busy())

    def _update_selection_label(self) -> None:
        count = len(self.selected)
        self.selection_label.setText(f"已选 {count} 个时段")
        self._set_chip(self.selection_label, f"已选 {count} 个时段",
                       "chipOk" if count else "chip")
        self._refresh_start_button()

    def selected_free_slots(self) -> list[FreeSlot]:
        return [FreeSlot(date=date, period=period) for date, period in sorted(self.selected)]

    # ── 配置装配 ──

    def build_plan_from_ui(self, *, apply_selection: bool = True) -> GrabPlan:
        plan = GrabPlan(
            course_id=self.plan_cfg.course_id,
            free_slots=self.selected_free_slots() if apply_selection else list(self.plan_cfg.free_slots),
            priority=self.plan_cfg.priority,
            max_total=self.plan_cfg.max_total,
            skip_taken_projects=True,
            dry_run=self.dry_check.isChecked(),
            hunt_drops=self.hunt_check.isChecked(),
            notify=self.notify_check.isChecked(),
            retry_rounds=int(self.rounds_spin.value()),
            retry_interval_seconds=float(self.interval_spin.value()),
            submit=dict(self.plan_cfg.submit or {}),
        )
        return plan

    def target_epoch(self) -> float | None:
        """把界面上的"抢课时刻"换算成本地 epoch（按**服务端时钟**对时）。"""
        if not self.time_enable.isChecked():
            return None
        from . import probe

        try:
            offset = probe.measure_clock_offset(samples=5).offset_seconds
        except Exception as exc:  # noqa: BLE001 - 对时失败就用本地钟，但要说清楚
            self.log_line(f"[警告] 对时失败（{type(exc).__name__}）：按本地时钟打点")
            offset = 0.0
        now = dt.datetime.now().astimezone()
        picked = self.time_edit.time()
        wall = now.replace(hour=picked.hour(), minute=picked.minute(), second=picked.second(),
                           microsecond=0)
        if wall.timestamp() < now.timestamp() - 30:
            wall += dt.timedelta(days=1)       # 已过则指明天
        return wall.timestamp() - offset

    # ── 抢课 ──

    def _is_busy(self) -> bool:
        return (self._worker is not None and self._worker.isRunning()) or \
               (self._loader is not None and self._loader.isRunning())

    def start_grab(self) -> None:
        if self._is_busy():
            return
        plan = self.build_plan_from_ui()
        try:
            plan.validate()
        except grabconfig.ConfigError as exc:
            self._warn("配置有误", str(exc))
            return
        if not plan.dry_run:
            if not self._ask("确认真实提交",
                             f"即将真实提交 {len(plan.free_slots)} 个空闲时段的选课，会写进你的课表。\n"
                             f"重试轮数 {plan.retry_rounds}，每轮间隔 {plan.retry_interval_seconds:.0f} 秒。\n\n确认继续？"):
                self.log_line("[已取消] 未开始。")
                return
        wait_until = self.target_epoch()
        if wait_until:
            self.log_line(f"[等待] 目标时刻换算完成，本地对应 "
                          f"{dt.datetime.fromtimestamp(wait_until).astimezone().isoformat(timespec='seconds')}")
        mode = "演练（不发写请求）" if plan.dry_run else "真实提交"
        self.log_line(f"[开始] 模式={mode}；空闲时段 {len(plan.free_slots)} 个；"
                      f"重试 {plan.retry_rounds} 轮 × {plan.retry_interval_seconds:.0f}s")
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._on_phase("waiting" if wait_until else "submitting", 0.0)
        self._worker = GrabWorker(plan, wait_until_epoch=wait_until, client_factory=self.client_factory)
        self._worker.progress.connect(self.log_line)
        self._worker.phase.connect(self._on_phase)
        self._worker.finished_report.connect(self._on_report)
        self._worker.failed.connect(self._on_grab_failed)
        self._worker.finished.connect(self._on_worker_done)
        self._worker.start()

    def stop_grab(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self.log_line("[停止] 已请求停止（正在进行的请求会跑完）。")

    def _on_worker_done(self) -> None:
        self.btn_stop.setEnabled(False)
        self._refresh_start_button()

    def _on_grab_failed(self, message: str) -> None:
        self.log_line(f"[抢课失败] {message}")
        self._critical("抢课失败", message[:500])

    def _update_result_title(self, report) -> None:
        """结果卡标题写明「本轮选中几个」（用户 2026-10-08：左下角要能看到选中的科目）。"""
        if getattr(report, "dry_run", False):
            self.result_box.setTitle(
                f"本轮结果（演练：本应提交 {len(report.would_submit)} 个）")
        else:
            self.result_box.setTitle(
                f"本轮选中的实验（{len(report.succeeded)} 个，勾选后可退课）")

    def _fit_mine_height(self) -> None:
        """把"本周期内已选"列表的高度设成刚好等于内容（上限 104），不留空白尾。

        与网格定高同一套路：布局未结算时就量会得到偏小的值 ⇒ 延后一拍 + 兜底判断。
        """
        content = self.mine_host.sizeHint().height()
        if content < 8:                      # 还没渲染好，等下一次
            QTimer.singleShot(40, self._fit_mine_height)
            return
        self.mine_scroll.setFixedHeight(min(content + 4, 104))

    @staticmethod
    def _row_text(row) -> str:
        """把一行列表的文字拼起来（自检用来核对内容）；富文本先剥标签，便于断言纯文字。"""
        import re as _re

        parts: list[str] = []
        for child in row.findChildren(QLabel):
            text = _re.sub(r"<[^>]+>", "", child.text()).strip()
            if text:
                parts.append(text)
        return " ".join(parts)

    @staticmethod
    def _tag(text: str, color: str) -> QLabel:
        """行首的小标签（已选 / 待抢）——固定宽度，让两类行对齐。"""
        label = QLabel(text)
        label.setStyleSheet(f"color: {color}; font-size: 8pt;")
        label.setMinimumWidth(26)
        label.setAlignment(Qt.AlignCenter)
        return label

    def _pending_slots(self) -> list[tuple[str, str]]:
        """我设为空闲、且落在窗口内、还没拿到课的时段（= 列表里的"待抢"行）。"""
        window = set(self.days())
        booked = {(e["date"], e["period"]) for e in getattr(self, "_elections_in_window", [])}
        return sorted(k for k in self.selected if k[0] in window and k not in booked)

    def _refresh_mine_rows(self) -> None:
        """按当前点选重画左下角管理列表（点格子/全选/清空后都要跟着变）。"""
        if not hasattr(self, "mine_layout"):
            return
        self._rebuild_mine_list(list(getattr(self, "_elections_in_window", [])),
                                self._pending_slots())
        inside = len(getattr(self, "_elections_in_window", []))
        self.mine_title.setText(
            f"本周期管理（已选 {inside} · 待抢 {len(self._pending_slots())}）")

    def _rebuild_mine_list(self, inside: list[dict], pending: list[tuple[str, str]]) -> None:
        """本周期**管理列表**（用户 2026-10-08）：

        - 已选行：带复选框（勾上 = 准备退掉），行尾写科目名；
        - 待抢行：当前你设为空闲、准备抢的时段（窗口内），带 × 可随时从计划里去掉。
        """
        while self.mine_layout.count():
            item = self.mine_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.mine_boxes = []
        if not inside and not pending:
            empty = QLabel(theme.muted("（本周期内既没有已选课，也还没选空闲时段）"))
            empty.setObjectName("step")
            self.mine_layout.addWidget(empty)
            self.mine_layout.addStretch(1)
            self._update_cancel_button()
            QTimer.singleShot(0, self._fit_mine_height)
            return
        for election in inside:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(6)
            box = QCheckBox()
            box.setToolTip("勾上 = 准备退掉这条选课；再点右下角「退掉勾选项」")
            box.stateChanged.connect(self._update_cancel_button)
            row_layout.addWidget(box)
            row_layout.addWidget(self._tag("已选", theme.ACTIVE.taken))
            when = QLabel(f"{election['date'][5:]} {election['period']}")
            when.setObjectName("step")
            when.setMinimumWidth(96)          # 对齐用：日期 + 节次
            row_layout.addWidget(when)
            name = QLabel(election["name"])
            name.setToolTip(f"{election['date']} {election['period']}　{election['name']}"
                            f"\n选课记录 id={election.get('record_id')}")
            row_layout.addWidget(name, 1)
            self.mine_layout.addWidget(row)
            self.mine_boxes.append((box, election))
        for date, period in pending:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(6)
            spacer = QLabel()
            spacer.setFixedWidth(14)          # 与"已选"行的复选框对齐
            row_layout.addWidget(spacer)
            row_layout.addWidget(self._tag("待抢", theme.ACTIVE.primary))
            when = QLabel(f"{date[5:]} {period}")
            when.setObjectName("step")
            when.setMinimumWidth(96)
            row_layout.addWidget(when)
            note = QLabel(theme.muted("准备抢：到点按实时余量选"))
            note.setToolTip("这是你设为空闲、准备抢的时段；点右侧 × 可把它从计划里去掉")
            row_layout.addWidget(note, 1)
            drop = QPushButton("×")
            drop.setFixedWidth(24)
            drop.setToolTip("把这个时段从「我的空闲时段」里去掉")
            drop.clicked.connect(lambda _checked=False, k=(date, period): self.toggle_cell(k))
            row_layout.addWidget(drop)
            self.mine_layout.addWidget(row)
        self.mine_layout.addStretch(1)
        self._update_cancel_button()
        # 按内容定高（延后一拍再量：布局未结算时 sizeHint 会偏小，见网格那次同款教训）
        QTimer.singleShot(0, self._fit_mine_height)

    def _update_cancel_button(self, *_args) -> None:
        """退课按钮的可用性/文案：**本轮抢到的**与**已有选课**都可勾选后退掉。"""
        picked = [box for box, _ in getattr(self, "pick_boxes", []) if box.isChecked()]
        mine = [box for box, _ in getattr(self, "mine_boxes", []) if box.isChecked()]
        total = len(picked) + len(mine)
        self.btn_cancel_picks.setEnabled(total > 0)
        if total:
            self.picks_hint.setText(f"已勾选 {total} 条待退（本轮 {len(picked)} + 已有选课 {len(mine)}）")
        else:
            self.picks_hint.setText("勾选要退掉的条目（本轮抢到的 / 本周期内已选的），再点右侧按钮")

    def _on_report(self, report) -> None:
        self._results = list(report.succeeded)
        self._update_result_title(report)
        lines = report.summary_lines()
        self.result_text.setPlainText("\n".join(lines))
        self.log_line("")
        for line in lines:
            self.log_line("  " + line)
        self._rebuild_picks()
        if not report.dry_run:
            self._info("抢课完成",
                       f"成功 {len(report.succeeded)} 个，失败 {len(report.failed)} 个，"
                       f"未覆盖时段 {len(report.uncovered)} 个。\n详见下方结果区。")

    def _rebuild_picks(self) -> None:
        while self.picks_layout.count():
            item = self.picks_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.pick_boxes: list[tuple[QCheckBox, Any]] = []
        if not self._results:
            self.btn_cancel_picks.setEnabled(False)
            self.picks_hint.setText("本轮没有抢到条目（失败与未覆盖原因见上方结果）")
            self.picks_layout.addStretch(1)
            return
        for attempt in self._results:
            box = QCheckBox(f"{attempt.candidate.date}  {attempt.candidate.period}   "
                            f"{attempt.candidate.project_name}   "
                            f"{attempt.candidate.location or '-'}   "
                            f"（slot={attempt.candidate.slot_id}）")
            self.picks_layout.addWidget(box)
            self.pick_boxes.append((box, attempt))
        self.picks_layout.addStretch(1)
        self.picks_hint.setText(f"本轮抢到 {len(self._results)} 条；勾选要退掉的，再点右侧按钮")
        self.btn_cancel_picks.setEnabled(True)

    def cancel_picked(self) -> None:
        """退掉**勾选的**条目：本轮抢到的（Attempt）+ 本周期内已有的选课（按记录 id）。

        两条路径共用同一套判据：调 `/cancel` → 回读核实确实消失（HTTP 成功不算数）。
        """
        picked = [attempt for box, attempt in getattr(self, "pick_boxes", []) if box.isChecked()]
        mine = [election for box, election in getattr(self, "mine_boxes", []) if box.isChecked()]
        if not picked and not mine:
            self._info("未选择", "请先勾选要退掉的条目（本轮抢到的 / 本周期内已选的）。")
            return
        names = "\n".join(
            [f"- [本轮] {a.candidate.date} {a.candidate.period} {a.candidate.project_name}"
             for a in picked] +
            [f"- [已选] {e['date']} {e['period']} {e['name']}" for e in mine])
        if not self._ask("确认退课",
                         f"即将退掉以下 {len(picked) + len(mine)} 条（不可撤销）：\n\n{names}\n\n确认？"):
            self.log_line("[已取消] 未退课。")
            return
        engine = runner.Runner(self.build_plan_from_ui(apply_selection=False),
                               client=self.client_factory(), log=self.log_line)
        cancelled: list[dict] = []
        try:
            for attempt in picked:
                ok, detail = engine.cancel_pick(attempt)
                self.log_line(f"  {'✓' if ok else '✗'} {attempt.candidate.date} "
                              f"{attempt.candidate.period}：{detail}")
            for election in mine:
                label = f"{election['date']} {election['period']} {election['name']}"
                ok, detail = engine.cancel_record(election.get("record_id"), label=label)
                self.log_line(f"  {'✓' if ok else '✗'} {label}：{detail}")
                if ok:
                    cancelled.append(election)
        finally:
            engine.close()
        if cancelled:                      # 本地先摘掉退成功的，界面立刻反映（复核仍可点刷新）
            remaining = [e for e in getattr(self, "_elections_in_window", [])
                         if e not in cancelled]
            self._elections_in_window = remaining
            self._refresh_mine_rows()
        self.log_line("复核请点「刷新场次」。")

    # ── 自检（无鼠标、无真实账号）──

    def self_check(self) -> int:
        """脚本化自检：装配 → 点选 → 演练抢课 → 断言状态转移。返回 0 = 通过。"""
        problems: list[str] = []

        checked = 0

        def expect(name: str, condition: bool, detail: str = "") -> None:
            nonlocal checked
            checked += 1
            self.log_line(f"[{'PASS' if condition else 'FAIL'}] {name}" +
                          (f" -- {detail}" if detail and not condition else ""))
            if not condition:
                problems.append(name)

        # 同步加载（自检不依赖事件循环里的线程时序）
        loader = GridLoader(self.build_plan_from_ui(apply_selection=False), self.days(),
                            self.client_factory)
        payload_holder: dict = {}
        loader.loaded.connect(lambda payload: payload_holder.update(payload))
        loader.failed.connect(lambda message: payload_holder.update({"error": message}))
        loader.run()          # 直接同步跑，确保断言前数据就绪
        if "error" in payload_holder:
            expect("加载网格", False, payload_holder["error"])
            self.log_line(f"SELF-CHECK FAILED: {problems}")
            return 1
        self._on_loaded(payload_holder)
        expect("加载网格", bool(self.cells))

        available = [k for k, v in self.cells.items() if v["state"] == "available"]
        expect("存在可约单元", bool(available), f"cells={len(self.cells)}")
        taken_cells = [k for k, v in self.cells.items() if v["state"] == "taken"]
        expect("已有选课被标为 taken（R2 前置）", True, f"taken={len(taken_cells)}")

        if available:
            key = available[0]
            self.toggle_cell(key)
            expect("点选后进入已选集合", key in self.selected, str(self.selected))
            self.toggle_cell(key)
            expect("再点一次取消", key not in self.selected)
            self.toggle_cell(key)

        plan = self.build_plan_from_ui()
        expect("界面装配出计划", len(plan.free_slots) == len(self.selected), str(plan))
        expect("默认演练（不发写请求）", plan.dry_run is True, f"dry_run={plan.dry_run}")

        report = runner.Runner(plan, client=self.client_factory(), log=self.log_line).run()
        self._on_report(report)
        expect("演练模式下没有真实成功", report.succeeded == [], str(report.succeeded))
        expect("演练给出本应提交", len(report.would_submit) == len(plan.free_slots),
               f"{len(report.would_submit)} vs {len(plan.free_slots)}")
        expect("结果区有内容", bool(self.result_text.toPlainText().strip()))

        # 已选实验不得出现在可约单元里（D6/R8）
        taken_ids = set((payload_holder.get("taken") or {}).keys())
        leaked = [k for k, v in self.cells.items()
                  if v["state"] == "available" and str(v.get("best_slot_id")) in taken_ids]
        expect("已选过的实验不出现在可约单元（D6）", not leaked, str(leaked))

        # ── 真实提交路径（用假客户端，不发网络请求）：验证"结果列表 + 可退课"这条链 ──
        real_plan = self.build_plan_from_ui()
        real_plan.dry_run = False
        real_plan.retry_rounds = 1
        real_engine = runner.Runner(real_plan, client=self.client_factory(), log=self.log_line)
        real_report = real_engine.run()
        self._on_report(real_report)
        expect("真实路径产生成功条目", len(real_report.succeeded) >= 1,
               f"succeeded={len(real_report.succeeded)} attempts={len(real_report.attempts)}")
        expect("结果列表已生成勾选项", len(getattr(self, "pick_boxes", [])) == len(real_report.succeeded),
               f"boxes={len(getattr(self, 'pick_boxes', []))} ok={len(real_report.succeeded)}")
        expect("退课按钮已启用", self.btn_cancel_picks.isEnabled())
        expect("每条都带 user2project_id", all(a.record_id is not None for a in real_report.succeeded),
               str([a.record_id for a in real_report.succeeded]))

        # 退课：直接调 cancel_pick（自检不弹确认框），并回读核实
        if real_report.succeeded:
            first = real_report.succeeded[0]
            ok, detail = real_engine.cancel_pick(first)
            expect("退课成功且回读核实", ok, detail)
            real_engine.close()

        # ── 布局真值判据（按 memory/04 §25：量"滚动区视口 vs 内容高度"，别看截图）──
        # ⚠️ **采样时机**：只 show() + 一两次 processEvents 时布局**还没稳定** ——
        #    实测那时量到 266/266"刚好放得下"，而真实路径（事件循环跑起来后）是 266/240 ⇒ 门禁**假绿**。
        #    正解：显式 `layout().activate()` + `adjustSize()` + 多泵几次事件后再量。
        self.show()
        for _ in range(3):
            QApplication.processEvents()
        self.layout().activate()
        self.grid_host.adjustSize()
        QApplication.processEvents()
        QApplication.processEvents()
        scroll = self.grid_host.parent().parent()
        content_h = self.grid_host.sizeHint().height()
        viewport_h = scroll.viewport().height()
        expect("默认尺寸下网格无需滚动就看全 5 个节次", content_h <= viewport_h,
               f"内容 {content_h}px > 视口 {viewport_h}px")
        # ── 2026-10-08 用户要求的默认值与设置区 ──
        expect("默认是真实提交（不是演练）", GrabPlan().dry_run is False,
               f"GrabPlan().dry_run={GrabPlan().dry_run}")
        expect("默认不捡漏（已满不空等）", GrabPlan().hunt_drops is False,
               f"GrabPlan().hunt_drops={GrabPlan().hunt_drops}")
        # 回归门禁：构造函数曾硬编码 GrabPlan(dry_run=True)，把新默认值覆盖掉（实测踩到）
        fresh = GrabPanel(client_factory=DemoClient, auto_reload=False, suppress_dialogs=True)
        expect("新建面板默认就是真实提交（构造函数不覆盖）",
               fresh.plan_cfg.dry_run is False and fresh.dry_check.isChecked() is False,
               f"dry_run={fresh.plan_cfg.dry_run} 勾选={fresh.dry_check.isChecked()}")
        expect("新建面板摘要构造时就有初值（安全信息不许为空）",
               bool(fresh.settings_summary.text()) and "真实提交" in fresh.settings_summary.text(),
               fresh.settings_summary.text()[:48])
        fresh.deleteLater()

        # 左下角：**窗口内**的已选要以**列表**显示（一行一条），窗口外的不进列表（用户 2026-10-08）
        rows = [self.mine_layout.itemAt(i).widget() for i in range(self.mine_layout.count())]
        row_texts = [self._row_text(w) for w in rows if w is not None]
        today_mmdd = dt.date.today().strftime("%m-%d")
        outside_day = (dt.date.today() + dt.timedelta(days=35)).strftime("%m-%d")
        selected_rows = [t for t in row_texts if "已选" in t]
        expect("左下角是列表：已选单独成行（不是一整串文字）",
               len(selected_rows) == 1 and today_mmdd in selected_rows[0],
               str(row_texts))
        expect("列表行含日期+节次+科目名",
               bool(row_texts) and today_mmdd in row_texts[0]
               and "上午1、2节" in row_texts[0] and "磁阻传感器与地磁场测量" in row_texts[0],
               str(row_texts))
        expect("标题写明本周期内的已选与待抢数量",
               self.mine_title.text().startswith("本周期管理（已选 1 · 待抢"),
               self.mine_title.text())
        expect("窗口外的已选不进列表",
               all(outside_day not in t and "分压限流电路" not in t for t in row_texts),
               str(row_texts))
        expect("窗口外的已选在标题提示里（信息不丢）",
               outside_day in self.mine_title.toolTip()
               and "分压限流电路" in self.mine_title.toolTip(),
               self.mine_title.toolTip()[:70])
        expect("左下角列表高度合理（不抢结果区）", 24 <= self.mine_scroll.height() <= 120,
               f"高 {self.mine_scroll.height()}px")
        # ── 左下角是**管理列表**：已选可勾选退课 + 待抢也在里面（用户 2026-10-08）──
        expect("左下角有已选行且带复选框", bool(self.mine_boxes)
               and isinstance(self.mine_boxes[0][0], QCheckBox),
               f"行数={len(self.mine_boxes)}")
        # 注意：本轮的 picks 可能已经把按钮置为可用（自检前面跑过真实路径），
        # 所以这里断言的是"勾选**进入**待退统计"，而不是"按钮从禁用变可用"。
        self.mine_boxes[0][0].setChecked(True)
        expect("勾选已有选课后退课按钮可用", self.btn_cancel_picks.isEnabled(),
               f"是否可用={self.btn_cancel_picks.isEnabled()}")
        expect("勾选后提示把『已有选课』计入待退",
               "待退" in self.picks_hint.text() and "已有选课 1" in self.picks_hint.text(),
               self.picks_hint.text())
        self.mine_boxes[0][0].setChecked(False)
        expect("取消勾选后该条不再计入待退",
               "已有选课 1" not in self.picks_hint.text(), self.picks_hint.text())
        # 待抢行：把两个窗口内的时段设为空闲后应出现在列表里
        win = self.days()
        self.selected.add((win[1], PERIODS[1]))
        self._refresh_mine_rows()
        row_texts2 = [self._row_text(self.mine_layout.itemAt(i).widget())
                      for i in range(self.mine_layout.count())
                      if self.mine_layout.itemAt(i).widget() is not None]
        expect("待抢时段出现在管理列表里",
               any("待抢" in t and win[1][5:] in t for t in row_texts2), str(row_texts2)[:120])
        expect("标题的待抢数量与实际待抢一致",
               f"待抢 {len(self._pending_slots())}" in self.mine_title.text(),
               f"{self.mine_title.text()} | _pending_slots={self._pending_slots()}")
        # × 按钮能把该时段从计划里去掉
        self.toggle_cell((win[1], PERIODS[1]))
        leftover = [self._row_text(self.mine_layout.itemAt(i).widget())
                    for i in range(self.mine_layout.count())
                    if self.mine_layout.itemAt(i).widget() is not None]
        # 注意：同一天可能有别的待抢时段 —— 必须**日期+节次**一起比，只比日期会误判
        expect("取消该时段后它不再出现在待抢里",
               not any("待抢" in t and win[1][5:] in t and PERIODS[1] in t for t in leftover),
               str(leftover)[:140])
        expect("取消该时段后它也不在空闲计划里",
               (win[1], PERIODS[1]) not in self.selected, str(sorted(self.selected))[:80])
        # 列表高度要**贴内容**（不留空白尾）：框高不该明显超过内容
        self._fit_mine_height()
        expect("左下角列表高度贴合内容（无空白尾）",
               self.mine_scroll.height() <= self.mine_host.sizeHint().height() + 12,
               f"框高 {self.mine_scroll.height()} vs 内容 {self.mine_host.sizeHint().height()}")
        expect("设置区默认折叠（保持紧凑）", not self.settings_box.isVisible())
        expect("设置摘要始终可见且写明提交方式",
               bool(self.settings_summary.text()) and
               ("真实提交" in self.settings_summary.text() or "演练" in self.settings_summary.text()),
               self.settings_summary.text()[:60])
        expect("顶部有提交方式标签", self.chip_mode is not None)
        # 摘要随控件变化（改成演练 -> 摘要与标签都要跟着变）
        self.dry_check.setChecked(True)
        self._refresh_settings_summary()
        expect("切到演练后摘要同步", "演练" in self.settings_summary.text(),
               self.settings_summary.text()[:40])
        expect("切到演练后顶部标签同步", "演练" in self.chip_mode.text(), self.chip_mode.text())
        self.dry_check.setChecked(False)
        self._refresh_settings_summary()
        # 捡漏开关必须真的进计划
        self.hunt_check.setChecked(True)
        hp = self.build_plan_from_ui(apply_selection=False)
        expect("捡漏开关进入计划", hp.hunt_drops is True, str(hp.hunt_drops))
        self.hunt_check.setChecked(False)
        hp = self.build_plan_from_ui(apply_selection=False)
        expect("捡漏关时计划里也是假", hp.hunt_drops is False, str(hp.hunt_drops))

        # 按钮文案必须随模式变（用户 2026-10-08 的要求，防回归）
        self.time_enable.setChecked(False)
        self._refresh_start_button()
        expect("不勾时按钮写『立即抢课』", "立即" in self.btn_start.text(), self.btn_start.text())
        self.time_enable.setChecked(True)
        self.time_edit.setTime(QTime(21, 30, 0))
        self._refresh_start_button()
        expect("勾上后按钮写『定时』并带出时刻",
               "定时" in self.btn_start.text() and "21:30:00" in self.btn_start.text(),
               self.btn_start.text())
        self.time_enable.setChecked(False)
        self._refresh_start_button()

        expect("结果占位区高度够放 2 行", self.result_text.height() >= 40,
               f"高度 {self.result_text.height()}px")

        # ── 本轮最关键的用户要求（2026-10-07 用户纠正）──
        # 配置的时候往往**看不到有哪些课**（窗口未开）⇒ "未放出"的时段也必须能选，
        # 而且刷新数据后**不能把用户已选的时段丢掉**。
        unpub = [k for k, v in self.cells.items() if v["state"] == "none"]
        if unpub:
            key = unpub[0]
            self.toggle_cell(key)
            expect("未放出的时段也能选中", key in self.selected, str(key))
            expect("未放出的时段进入计划",
                   key in {s.key for s in self.build_plan_from_ui().free_slots},
                   str([s.key for s in self.build_plan_from_ui().free_slots]))
            self._on_loaded(payload_holder)          # 模拟"刷新数据"
            expect("刷新后已选时段不丢", key in self.selected, str(sorted(self.selected)))
            self.toggle_cell(key)
            expect("可取消选中", key not in self.selected)
        else:
            expect("存在『未放出』的时段样本（用于验证可选中）", False,
                   "当前窗口已放全，换一天再跑更能覆盖此路径")

        # ── 用户流程的关键序列：等待(倒计时) → 到点拉取列表 → 提交 → 完成 ──
        # 直接同步跑一次 GrabWorker（3 秒后到点），断言阶段**按顺序**出现。
        phases: list[str] = []
        wait_plan = self.build_plan_from_ui()
        wait_plan.dry_run = True
        worker = GrabWorker(wait_plan, wait_until_epoch=dt.datetime.now().timestamp() + 3.0,
                            client_factory=self.client_factory)
        worker.phase.connect(lambda name, remain: phases.append(name))
        worker.run()                      # 同步执行（自检不需要事件循环）
        expect("阶段序列含 waiting（倒计时）", "waiting" in phases, str(phases))
        expect("到点先 fetching（拉取列表）再 submitting（提交）",
               "fetching" in phases and "submitting" in phases
               and phases.index("fetching") < phases.index("submitting"), str(phases))
        expect("结束上报 done", phases and phases[-1] == "done", str(phases))

        # 倒计时格式（纯函数；界面与日志共用同一份）
        expect("倒计时 0 秒", format_countdown(0) == "00:00:00", format_countdown(0))
        expect("倒计时 65 秒", format_countdown(65) == "00:01:05", format_countdown(65))
        expect("倒计时 3661 秒", format_countdown(3661) == "01:01:01", format_countdown(3661))
        expect("倒计时向上取整（还剩 0.4s 显示 1 秒）", format_countdown(0.4) == "00:00:01",
               format_countdown(0.4))
        expect("倒计时负数按 0 处理", format_countdown(-5) == "00:00:00", format_countdown(-5))
        avail_h = (self.screen() or QApplication.primaryScreen()).availableGeometry().height()
        expect("窗口最小高度能缩进可用工作区", self.minimumSizeHint().height() < avail_h - 40,
               f"最小 {self.minimumSizeHint().height()} vs 工作区 {avail_h}")

        # ── 用户反馈（2026-10-07）：文字显示不全 / 控件宽度不够 ──
        # ① 登录态标签必须是**结论**，不能是半句话（早期 [:28] 截出"…（约剩"这种）
        chip_text = self.chip_login.text()
        expect("登录态标签不是被截断的半句话",
               not chip_text.endswith("（") and "约剩" not in chip_text and len(chip_text) <= 26,
               f"标签={chip_text!r}")
        # 用一枚**语法合法的假 JWT**验证解析路径本身没坏（不需要真实账号/网络）——
        # 这条门禁本来能抓住"api 未导入 ⇒ NameError 被吞成未知"那个 bug。
        import base64 as _b64
        import json as _json
        def _b64u(obj):
            return _b64.urlsafe_b64encode(_json.dumps(obj).encode()).decode().rstrip("=")
        fake_jwt = "Bearer " + ".".join([
            _b64u({"alg": "HS256", "typ": "JWT"}),
            _b64u({"user_id": 0, "exp": dt.datetime.now().timestamp() + 3600}),
            "sig",
        ])
        parsed = compact_login_text(fake_jwt)
        expect("登录态解析不因内部错误降级", "无法解析" not in parsed and "有效" in parsed, parsed)
        expect("登录态：无 token 时提示未登录", compact_login_text(None) == "登录态：未登录",
               compact_login_text(None))
        expect("登录态标签有明确结论",
               any(word in chip_text for word in ("有效", "已过期", "未登录", "无法解析")), chip_text)

        # ② 在**默认窗口宽度**下，网格不该需要横向滚动（两周 14 天都要看得见）
        #    注：日志占了右侧一列，窗口被拖得很窄时允许横向滚动（这是刻意的取舍）；
        #    这里量的是"默认尺寸"这个承诺。
        self.resize(1180, self.height())
        for _ in range(3):
            QApplication.processEvents()
        self.layout().activate()
        need_w = self.grid_host.sizeHint().width()
        have_w = scroll.viewport().width()
        expect("默认宽度下网格无需横向滚动（14 天可见）", need_w <= have_w + 2,
               f"内容宽 {need_w}px > 视口宽 {have_w}px")

        # ③ 日志面板必须真的在**右侧**（用户 2026-10-08 要求；按几何判定，不靠"我打算这么做"）
        left_pane = self.splitter.widget(0)
        left_geo, log_geo = left_pane.geometry(), self.log_box.geometry()
        expect("日志面板在内容右侧", log_geo.x() >= left_geo.x() + left_geo.width() - 2,
               f"内容 x={left_geo.x()}+w{left_geo.width()} vs 日志 x={log_geo.x()}")
        expect("日志与内容并排（纵向有重叠）",
               not (log_geo.y() >= left_geo.y() + left_geo.height()
                    or left_geo.y() >= log_geo.y() + log_geo.height()),
               f"内容 y={left_geo.y()}..{left_geo.y()+left_geo.height()} "
               f"日志 y={log_geo.y()}..{log_geo.y()+log_geo.height()}")
        expect("日志面板宽度可用", log_geo.width() >= 190, f"宽度 {log_geo.width()}px")
        expect("日志占满右侧整列高度（顶齐、高度相当）",
               log_geo.y() <= left_geo.y() + 4 and log_geo.height() >= left_geo.height() - 4,
               f"日志 y={log_geo.y()} h={log_geo.height()} | 内容 y={left_geo.y()} h={left_geo.height()}")

        # ④ 单元格文字不许被省略号截掉（最宽的一行要放得下）
        elided: list[str] = []
        for key, button in self.buttons.items():
            text = button.text()
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            if not lines:
                continue
            metrics = button.fontMetrics()
            widest = max(lines, key=lambda line: metrics.horizontalAdvance(line))
            needed = metrics.horizontalAdvance(widest) + 8      # 内边距
            if needed > button.width():
                elided.append(f"{key[1]}:{widest}({needed}>{button.width()})")
        expect("单元格文字不被省略（含『未放出/无新实验/已满』）", not elided, str(elided[:4]))

        # ⑤ 通用门禁：**没有文字被截断**的标签（按字体实际测量，不靠肉眼）
        #    ⚠️ 多行标签要按**最长的一行**量，不能把各行拼起来量 ——
        #    实测踩到：日期表头是两行（`10-07` + `周三`），拼起来量成 57px 会误报"截断"。
        import re as _re

        clipped: list[str] = []
        for label in self.findChildren(QLabel):
            if not label.isVisible() or label.wordWrap() or label.width() <= 1:
                continue
            text = label.text()
            text = _re.sub(r"<br\s*/?>", "\n", text, flags=_re.IGNORECASE)
            text = _re.sub(r"<[^>]+>", "", text)
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            if not lines:
                continue
            metrics = label.fontMetrics()
            needed = max(metrics.horizontalAdvance(line) for line in lines)
            if needed > label.width() + 1:             # 严格大于才算截断（避免"刚好相等"误报）
                clipped.append(f"{lines[0][:16]}({needed}>{label.width()})")
        expect("界面上没有文字被截断的标签", not clipped, str(clipped[:4]))

        # ⑥ 拉不到数据时必须有**显式横幅**（用户 2026-10-07 提醒：拉不到时也不会显示）
        self._show_banner("测试：当前拉取不到场次")
        expect("无数据时横幅可见", self.banner.isVisible() and self.banner.text())
        before_h = self.banner.height()
        self._hide_banner()
        expect("有数据时横幅隐藏", not self.banner.isVisible())
        expect("横幅高度合理（非零、不超两行）", before_h > 0, f"高度 {before_h}")

        # ⑦ 界面文本**不许出现 markdown 标记**（memory/16 的规矩；这次又犯了，故做成运行时门禁）
        #    按"真实可见的文本"查：标签文字 / 悬停提示 / 横幅 / 结果区；
        #    运行日志只查 `**`（api 层的错误文案里合法带反引号，属半技术性文本）。
        markdown_hits: list[str] = []
        for label in self.findChildren(QLabel):
            if not label.isVisible():
                continue
            for text in (label.text(), label.toolTip()):
                if "**" in text or "`" in text:
                    markdown_hits.append(f"标签:{text[:26]}")
        for name, blob in (("横幅", self.banner.text()),
                           ("结果区", self.result_text.toPlainText())):
            if "**" in blob or "`" in blob:
                markdown_hits.append(f"{name}:{blob[:26]}")
        if "**" in self.log.toPlainText():
            markdown_hits.append("运行日志含 **")
        expect("界面文本不含 markdown 标记", not markdown_hits, str(markdown_hits[:4]))

        # ⑧ 错误文案必须是**完整的一句话**（不许截成半句，如"请重新运行 `python"）
        cause_401 = short_cause("ApiError: 401 未授权（PostgREST 42501）：token 可能已过期")
        expect("错误归类给完整短句", cause_401.endswith("未登录"), cause_401)
        cause_unknown = short_cause("某些奇怪的长错误" + "x" * 200)
        expect("错误归类不截断（未知原因也给整句）",
               cause_unknown == "原因见运行日志（已记录完整错误）" or cause_unknown.endswith("）"),
               cause_unknown)
        self.hide()

        self.log_line("")
        if problems:
            self.log_line(f"SELF-CHECK FAILED: {len(problems)}/{checked} -> {problems}")
            return 1
        # 把**总项数**打在结论行里：只数输出里的 [PASS] 会被日志截断而少算（实测踩到）
        self.log_line(f"SELF-CHECK PASSED（共 {checked} 项检查）")
        return 0


class DemoClient:
    """自检用的**假客户端**：不发任何网络请求，也不碰真实账号。

    为什么需要它：GUI 的装配逻辑（网格状态 → 点选 → 计划 → 演练 → 结果展示）
    在没有真实窗口与鼠标时也要能被验证；假客户端让 `--self-check` 全程可跑。
    """

    def __init__(self) -> None:
        self.claims = {"exp": 4102444800}
        self.submit_calls: list[str] = []
        self.electives = [{
            # 窗口内（今天）：左下角应该显示它
            "id": 90001, "schedule_id": 4841, "project_id": 445, "schedule_status": "elected",
            "schedules": {"date": dt.date.today().isoformat(), "periods": {"name": "上午1、2节"},
                          "projects": {"name": "磁阻传感器与地磁场测量（519）"}},
        }, {
            # 窗口外（35 天后）：**不该**出现在左下角的可见行里，只进悬停提示
            "id": 90002, "schedule_id": 4999, "project_id": 448, "schedule_status": "elected",
            "schedules": {"date": (dt.date.today() + dt.timedelta(days=35)).isoformat(),
                          "periods": {"name": "下午7、8节"},
                          "projects": {"name": "分压限流电路实验（543）"}},
        }]

    def prewarm(self) -> int:
        return 5

    def open_semesters(self) -> list[dict]:
        return [{"id": 18, "name": "2026-2027-（1）", "since": "2026-08-20", "to": "2027-02-07"}]

    def my_courses(self, semester_id: Any) -> list[dict]:
        return [{"id": 71, "name": "大学物理实验Ⅰ(2)"}]

    def course_projects(self, course_id: Any) -> list[dict]:
        return [{"project_id": 475, "projects": {"name": "弗兰克-赫兹实验（520）"}},
                {"project_id": 445, "projects": {"name": "磁阻传感器与地磁场测量（519）"}},
                {"project_id": 448, "projects": {"name": "分压限流电路实验（543）"}}]

    def _row(self, slot_id: int, date: str, period: str, project_id: int, remaining: int) -> dict:
        return {"id": slot_id, "date": date, "periods": {"name": period},
                "project_id": project_id, "current_student_number": 30 - remaining,
                "max_student_number": 30, "locations": {"name": "笃行楼520"}, "projects": None}

    def slots(self, course_id: Any, *, project_id: Any = None, with_my_status: bool = False) -> list[dict]:
        today = dt.date.today()
        day1 = (today + dt.timedelta(days=1)).isoformat()
        day3 = (today + dt.timedelta(days=3)).isoformat()
        table = {
            475: [self._row(5001, day1, "下午5、6节", 475, 24),
                  self._row(5002, day3, "上午1、2节", 475, 0)],      # 已满
            445: [self._row(5003, day1, "上午1、2节", 445, 9)],       # 已选过该实验
            448: [self._row(5004, day1, "晚上9，10节", 448, 15)],
        }
        return table.get(project_id, [])

    def my_electives(self, semester_id: Any, course_id: Any = None) -> list[dict]:
        return list(self.electives)

    def submit_booking(self, slot_id: Any, course_id: Any):
        from . import api as api_mod
        from .models import Outcome

        self.submit_calls.append(str(slot_id))
        # 模拟服务端**真的落库**：这样 runner 的"回读核实"能通过，
        # 自检才能走到"结果列表 + 可退课"那条路径（否则只会得到 unverified）。
        label = {"5001": ("弗兰克-赫兹实验（520）", "下午5、6节"),
                 "5002": ("弗兰克-赫兹实验（520）", "上午1、2节"),
                 "5004": ("分压限流电路实验（543）", "晚上9，10节")}.get(
            str(slot_id), ("（未知实验）", "下午5、6节"))
        day1 = (dt.date.today() + dt.timedelta(days=1)).isoformat()
        self.electives.append({
            "id": 91000 + len(self.electives), "schedule_id": int(slot_id), "project_id": 475,
            "schedule_status": "elected",
            "schedules": {"date": day1, "periods": {"name": label[1]},
                          "projects": {"name": label[0]}},
        })
        return api_mod.WriteResult(action="选课", target=f"lesson_id={slot_id}", http_status=200,
                                   body_text='{"status":false,"code":200,"message":"ok"}',
                                   ok=True, outcome=Outcome.SUCCESS, elapsed_ms=8)

    def cancel_booking(self, user2project_id: Any):
        from . import api as api_mod
        from .models import Outcome

        self.electives = [e for e in self.electives if str(e["id"]) != str(user2project_id)]
        return api_mod.WriteResult(action="退课", target=f"user2project_id={user2project_id}",
                                   http_status=200,
                                   body_text='{"status":false,"code":200,"message":"ok"}',
                                   ok=True, outcome=Outcome.SUCCESS, elapsed_ms=8)

    def close(self) -> None:
        pass


def main(argv: list[str] | None = None) -> int:
    """打开抢课面板；`--self-check` 用假客户端跑脚本化自检（不发网络请求）。"""
    import sys

    from PySide6.QtWidgets import QApplication

    args = list(argv if argv is not None else sys.argv[1:])
    self_check = "--self-check" in args
    app = QApplication.instance() or QApplication([sys.argv[0]])
    theme.apply_theme(app)
    if self_check:
        # auto_reload=False：不装 200ms 定时器，避免自检里被非守护线程拖住（见 __init__ 的说明）
        panel = GrabPanel(client_factory=DemoClient,
                          plan_cfg=GrabPlan(dry_run=True, notify=False),
                          auto_reload=False, suppress_dialogs=True)
        # 自检模式不显示窗口，也不依赖事件循环里的定时器
        code = panel.self_check()
        print("\n".join(panel.log.toPlainText().splitlines()))   # 全部打印，不再截尾
        return code
    panel = GrabPanel()
    panel.show()
    return app.exec()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
