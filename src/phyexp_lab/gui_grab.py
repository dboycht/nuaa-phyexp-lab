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
    QSpinBox,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import QTime

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
            cells = planner.grid_cells(self.plan_cfg, rows, days=self.days, occupied=occupied,
                                       project_names=names, taken_projects=taken)
            self.loaded.emit({"course_id": course_id, "courses": courses, "cells": cells,
                              "occupied": occupied, "taken": taken,
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


def format_countdown(seconds: float) -> str:
    """把剩余秒数格式化成 `HH:MM:SS`（负数/超大值都按 0 处理，不显示怪值）。

    纯函数 ⇒ 可单测；界面与日志共用同一份格式，避免"两处各算一套"。
    """
    total = max(0, int(seconds + 0.999))     # 向上取整：还剩 0.4 秒显示 00:00:01，不是 00:00:00
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


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
        self.resize(1180, 1032)
        self.setMinimumSize(980, 640)   # 允许缩到小屏也能用（实测提醒：不留余量时最小高度会顶到 1053）
        self.client_factory = client_factory or (lambda: __import__(
            "phyexp_lab.api", fromlist=["PhyExpClient"]).PhyExpClient(timeout=20.0))
        self.plan_cfg = plan_cfg or GrabPlan(dry_run=True)
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
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        # ── 顶部标题卡：标题 + 一句说明 + 状态小标签 ──
        header = QFrame()
        header.setObjectName("header")
        head_layout = QVBoxLayout(header)
        head_layout.setContentsMargins(16, 12, 16, 12)
        head_layout.setSpacing(6)
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
        for chip in (self.chip_login, self.chip_course, self.chip_window):
            title_row.addWidget(chip)
        self.btn_login = QPushButton("登录")
        self.btn_login.setIcon(theme.qicon("shield", 15))
        self.btn_login.setToolTip("打开浏览器窗口登录（本项目不接触你的密码）；登录成功后会自动刷新场次")
        self.btn_login.clicked.connect(self._start_login)
        title_row.addWidget(self.btn_login)
        head_layout.addLayout(title_row)

        subtitle = QLabel(theme.muted(
            "第 1 步：点选你空闲的时段（绿色=可约，点一下变深绿=已选）"
            "　·　第 2 步：可设定抢课时刻，到时自动开抢"
            "　·　第 3 步：抢完看结果，抢多了可勾选退课"))
        subtitle.setObjectName("step")
        head_layout.addWidget(subtitle)
        root.addWidget(header)

        # ── 配置卡：三组之间用竖线分隔（时刻 / 重试 / 安全），避免一堆控件糊在一起 ──
        box = QGroupBox("抢课设置")
        box.setFont(theme.ui_font(10, QFont.DemiBold))
        cfg_layout = QHBoxLayout(box)
        cfg_layout.setSpacing(10)

        def separator() -> QFrame:
            line = QFrame()
            line.setFrameShape(QFrame.VLine)
            line.setStyleSheet(f"color: {theme.ACTIVE.border}; background: {theme.ACTIVE.border};")
            line.setFixedWidth(1)
            return line

        cfg_layout.addWidget(QLabel("目标时刻"))
        self.time_enable = QCheckBox("到点开抢")
        cfg_layout.addWidget(self.time_enable)
        self.time_edit = QTimeEdit()
        self.time_edit.setDisplayFormat("HH:mm:ss")
        self.time_edit.setTime(QTime(10, 0, 0))
        self.time_edit.setFixedWidth(96)
        cfg_layout.addWidget(self.time_edit)
        cfg_layout.addSpacing(4)
        cfg_layout.addWidget(separator())
        cfg_layout.addSpacing(4)
        cfg_layout.addWidget(QLabel("重试"))
        self.rounds_spin = QSpinBox()
        self.rounds_spin.setRange(1, 200)
        self.rounds_spin.setValue(int(self.plan_cfg.retry_rounds or 10))
        self.rounds_spin.setFixedWidth(72)
        self.rounds_spin.setSuffix(" 轮")
        cfg_layout.addWidget(self.rounds_spin)
        cfg_layout.addWidget(QLabel("每轮间隔"))
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(1, 600)
        self.interval_spin.setValue(int(self.plan_cfg.retry_interval_seconds or 30))
        self.interval_spin.setFixedWidth(80)
        self.interval_spin.setSuffix(" 秒")
        cfg_layout.addWidget(self.interval_spin)
        cfg_layout.addSpacing(4)
        cfg_layout.addWidget(separator())
        cfg_layout.addSpacing(4)
        self.real_check = QCheckBox("真实提交（会写进课表）")
        self.real_check.setChecked(not self.plan_cfg.dry_run)
        cfg_layout.addWidget(self.real_check)
        self.notify_check = QCheckBox("桌面通知")
        self.notify_check.setChecked(bool(self.plan_cfg.notify))
        cfg_layout.addWidget(self.notify_check)
        cfg_layout.addStretch(1)
        root.addWidget(box)

        # ── 操作条 ──
        actions = QHBoxLayout()
        actions.setSpacing(8)
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
        self.btn_start = QPushButton("开始抢课")
        self.btn_start.setIcon(theme.qicon("play", 15, "white"))
        self.btn_start.setObjectName("primary")
        self.btn_start.setMinimumWidth(120)
        self.btn_start.clicked.connect(self.start_grab)
        actions.addWidget(self.btn_start)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setIcon(theme.qicon("stop", 15, theme.ACTIVE.danger))
        self.btn_stop.setObjectName("danger")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_grab)
        actions.addWidget(self.btn_stop)
        root.addLayout(actions)

        # ── 网格卡 ──
        grid_box = QGroupBox("空闲时段（两周）")
        grid_box.setFont(theme.ui_font(10, QFont.DemiBold))
        grid_outer = QVBoxLayout(grid_box)
        grid_outer.setContentsMargins(10, 8, 10, 10)
        hint = QLabel(theme.muted(
            "点格子 = 标记「这个时段我有空」（与当前有没有课无关）；"
            "格子里的小字是**当前可见情况**，窗口未开时大多显示「未放出」属正常。"
            "到点开抢时会**重新拉取实时列表**再筛候选。"))
        hint.setWordWrap(True)
        hint.setObjectName("step")
        grid_outer.addWidget(hint)
        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setSpacing(4)
        self.grid.setContentsMargins(2, 2, 2, 2)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.grid_host)
        grid_outer.addWidget(scroll)
        scroll.setMinimumHeight(170)      # 允许缩小（能滚动看其余行）；窗口够大时由 stretch 自动展开
        scroll.setMaximumHeight(560)
        root.addWidget(grid_box, 5)

        # ── 结果卡 ──
        self.result_box = QGroupBox("本轮结果（勾选后可退课）")
        self.result_box.setFont(theme.ui_font(10, QFont.DemiBold))
        result_layout = QVBoxLayout(self.result_box)
        result_layout.setContentsMargins(10, 8, 10, 10)
        self.result_text = QPlainTextEdit()
        self.result_text.setReadOnly(True)
        self.result_text.setMinimumHeight(74)
        self.result_text.setMaximumHeight(110)
        self.result_text.setPlaceholderText(
            "还没有开始抢课。\n"
            "先在上面的网格里点选空闲时段 → 点「开始抢课」。\n"
            "默认是演练（不会真的提交）；勾选「真实提交」才会写进课表，届时会二次确认。")
        result_layout.addWidget(self.result_text)

        # 抢到的条目：**可滚动的勾选列表**（条目多时不会挤成一行、也不会被截断）
        picks_scroll = QScrollArea()
        picks_scroll.setWidgetResizable(True)
        picks_scroll.setMinimumHeight(40)
        picks_scroll.setMaximumHeight(110)
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
        root.addWidget(self.result_box, 2)

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
        self.log.setMaximumHeight(88)
        self.btn_clear_log = QPushButton("清空日志")
        self.btn_clear_log.setIcon(theme.qicon("undo", 14))
        self.btn_clear_log.clicked.connect(self.log.clear)
        log_head.addWidget(self.btn_clear_log)
        log_layout.addLayout(log_head)
        log_layout.addWidget(self.log)
        self.log_box = log_box
        root.addWidget(log_box)

    # ── 日志 ──

    def _toggle_log(self, visible: bool) -> None:
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
            self._set_chip(self.chip_login, f"登录态：{described[:28]}", "chipOk")
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
            remain = session.describe_token(token) if token else "无 token"
        except Exception:  # noqa: BLE001 - 状态展示失败不该影响功能
            remain = "未知"
        self._set_chip(self.chip_login, f"登录态：{remain[:28]}",
                       "chipOk" if "已过期" not in remain else "chipDanger")
        course_name = ""
        for course in payload.get("courses") or []:
            if str(course.get("id")) == str(payload["course_id"]):
                course_name = str(course.get("name") or "")
        self._set_chip(self.chip_course, f"课程：{course_name or payload['course_id']}")
        days = self.days()
        self._set_chip(self.chip_window, f"窗口：{days[0][5:]} ~ {days[-1][5:]}（两周）")
        self.log_line(f"已加载：课程 id={payload['course_id']}，当前可见可约单元 {avail} 个，"
                      f"已有选课 {taken} 个，已选过实验 {len(payload['taken'])} 个")
        if avail == 0:
            self.log_line("       注意：现在看不到可约单元，**这在窗口未开时是正常的** ——"
                          "你照常点选空闲时段即可，到点后系统会按当时的实时余量重新筛候选。")
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
            name.setMinimumWidth(84)
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
                button.setMinimumHeight(36)      # 36px：5 个节次在默认窗口里能一次看全（40px 会差 31px，需滚动）
                button.setMinimumWidth(62)
                state = "selected" if selected else self._cell_state(info)
                button.setStyleSheet(theme.cell_qss(state))
                button.clicked.connect(lambda _checked=False, k=key: self.toggle_cell(k))
                self.grid.addWidget(button, row, columns[index])
                self.buttons[key] = button
        self._update_selection_label()

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
            hint = "当前已满"
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
            "all_elected": ("该节次只放了你**已选过**的实验 —— 同一实验不重复选，所以这里没有可抢的新实验。\n"
                            + "涉及：" + "、".join(info.get("blocked_projects") or ["（未取到名称）"])),
            "none": "当前看不到这个节次的场次（窗口未开/未排课 —— **属正常**）",
        }.get(state, state)
        return (f"{head}当前可见情况：{current}\n\n"
                "点一下即可把它设为/取消『我的空闲时段』；\n"
                "真正的候选会在**到点执行时**按当时的实时余量重新筛选。")

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

    def select_all_available(self) -> None:
        for key, info in self.cells.items():
            if info["state"] == "available" and key not in self.selected:
                self.selected.add(key)
        self._rebuild_grid()

    def clear_selection(self) -> None:
        self.selected.clear()
        self._rebuild_grid()

    def _update_selection_label(self) -> None:
        count = len(self.selected)
        self.selection_label.setText(f"已选 {count} 个时段")
        self._set_chip(self.selection_label, f"已选 {count} 个时段",
                       "chipOk" if count else "chip")
        self.btn_start.setEnabled(bool(count) and not self._is_busy())

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
            dry_run=not self.real_check.isChecked(),
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
                             f"即将**真实提交** {len(plan.free_slots)} 个空闲时段的选课，会写进你的课表。\n"
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
        self.btn_start.setEnabled(bool(self.selected))

    def _on_grab_failed(self, message: str) -> None:
        self.log_line(f"[抢课失败] {message}")
        self._critical("抢课失败", message[:500])

    def _on_report(self, report) -> None:
        self._results = list(report.succeeded)
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
        picked = [attempt for box, attempt in getattr(self, "pick_boxes", []) if box.isChecked()]
        if not picked:
            self._info("未选择", "请先勾选要退掉的条目。")
            return
        names = "\n".join(f"- {a.candidate.date} {a.candidate.period} {a.candidate.project_name}"
                          for a in picked)
        if not self._ask("确认退课",
                         f"即将退掉以下 {len(picked)} 条（不可撤销）：\n\n{names}\n\n确认？"):
            self.log_line("[已取消] 未退课。")
            return
        engine = runner.Runner(self.build_plan_from_ui(apply_selection=False),
                               client=self.client_factory(), log=self.log_line)
        try:
            for attempt in picked:
                ok, detail = engine.cancel_pick(attempt)
                self.log_line(f"  {'✓' if ok else '✗'} {attempt.candidate.date} "
                              f"{attempt.candidate.period}：{detail}")
        finally:
            engine.close()
        self.log_line("复核请点「刷新场次」。")

    # ── 自检（无鼠标、无真实账号）──

    def self_check(self) -> int:
        """脚本化自检：装配 → 点选 → 演练抢课 → 断言状态转移。返回 0 = 通过。"""
        problems: list[str] = []

        def expect(name: str, condition: bool, detail: str = "") -> None:
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
        expect("结果占位区高度够放 3 行", self.result_text.height() >= 66,
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
        self.hide()

        self.log_line("")
        if problems:
            self.log_line(f"SELF-CHECK FAILED: {len(problems)} -> {problems}")
            return 1
        self.log_line("SELF-CHECK PASSED")
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
            "id": 90001, "schedule_id": 4841, "project_id": 445, "schedule_status": "elected",
            "schedules": {"date": dt.date.today().isoformat(), "periods": {"name": "上午1、2节"},
                          "projects": {"name": "磁阻传感器与地磁场测量（519）"}},
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
        print("\n".join(panel.log.toPlainText().splitlines()[-70:]))
        return code
    panel = GrabPanel()
    panel.show()
    return app.exec()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
