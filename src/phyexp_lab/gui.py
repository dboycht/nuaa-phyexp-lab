"""PySide6 主界面：只读工作台（实验项目 + 场次余量 + 日志）。

设计原则
--------
1. **界面文本不写 markdown 标记**：QTableWidget / QLabel / 日志都按纯文本渲染，
   星号与反引号会原样露出来（见 `memory/16`）。
2. **网络操作进子线程**：所有接口调用都在 `Loader(QThread)` 里跑，主线程只更新界面，
   避免"点一下卡住"。
3. **只读**：界面上不提供任何写操作（选课/退课要等窗口开放并实测请求体后再加）。
4. **不伪造数据**：拿不到数据就在日志区如实说明原因（未登录 / token 过期 / 无开放学期）。
5. **低频**：自动刷新默认 60 秒且可关闭，不做高频轮询。
"""

from __future__ import annotations

import datetime as dt

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import __version__, api, session

GREEN = QColor(0x18, 0x8A, 0x3E)
RED = QColor(0xC0, 0x39, 0x2B)
GREY = QColor(0x77, 0x77, 0x77)


class Loader(QThread):
    """后台加载：课程 → 实验项目 → 场次（全部只读 GET）。"""

    progress = Signal(str)
    loaded = Signal(dict)
    failed = Signal(str)

    def __init__(self, timeout: float = 20.0) -> None:
        super().__init__()
        self.timeout = timeout

    def run(self) -> None:  # noqa: D102 - QThread 入口
        client = None
        try:
            client = api.PhyExpClient(timeout=self.timeout)
            warm_ms = client.prewarm()
            self.progress.emit(f"连接已预热（{warm_ms} ms）")

            semesters = client.open_semesters()
            if not semesters:
                self.failed.emit("当前没有开放学期（接口返回空），无法加载数据。")
                return
            semester = semesters[0]
            self.progress.emit(f"开放学期：{semester.get('name')}（id={semester.get('id')}）")

            courses_raw = client.my_courses(semester.get("id"))
            self.progress.emit(f"我的课程：{len(courses_raw)} 门")

            payload: dict = {
                "server_time": client.server_time(),
                "local_time": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                "semester": {k: semester.get(k) for k in ("id", "name", "since", "to")},
                "courses": [],
            }

            total_slots = total_free = 0
            for course in courses_raw:
                course_id = course.get("id")
                entry = {"course_id": course_id, "name": course.get("name"), "projects": []}
                rows = client.course_projects(course_id)
                self.progress.emit(f"课程「{course.get('name')}」下有 {len(rows)} 个实验项目")
                for row in rows:
                    experiment = client.to_experiment(row)
                    slots = [client.to_slot(r) for r in client.slots(
                        course_id, project_id=experiment.experiment_id, with_my_status=False)]
                    free = sum(1 for s in slots if (s.remaining or 0) > 0)
                    total_slots += len(slots)
                    total_free += free
                    entry["projects"].append({
                        "project_id": experiment.experiment_id,
                        "name": experiment.name,
                        "slots": [
                            {
                                "slot_id": s.slot_id,
                                "time": s.time_text,
                                "location": s.location or "",
                                "taken": s.taken,
                                "capacity": s.capacity,
                                "remaining": s.remaining,
                            }
                            for s in slots
                        ],
                    })
                payload["courses"].append(entry)

            payload["totals"] = {"slots": total_slots, "free": total_free}
            self.loaded.emit(payload)
        except Exception as exc:  # noqa: BLE001 - 任何失败都在界面上如实说明
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        finally:
            if client is not None:
                client.close()


class MainWindow(QMainWindow):
    """只读工作台主窗口。"""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"nuaa-phyexp-lab 实验助手 · 只读工作台  v{__version__}")
        self.resize(1240, 780)
        self._payload: dict | None = None

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        # ── 顶部状态条 ──
        top = QHBoxLayout()
        self.lbl_session = QLabel()
        self.lbl_time = QLabel()
        for label in (self.lbl_session, self.lbl_time):
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        top.addWidget(self.lbl_session)
        top.addStretch(1)
        top.addWidget(self.lbl_time)
        layout.addLayout(top)

        # ── 操作条 ──
        bar = QHBoxLayout()
        self.btn_refresh = QPushButton("刷新数据")
        self.btn_refresh.clicked.connect(self.reload)
        self.btn_auto = QPushButton("自动刷新：关")
        self.btn_auto.setCheckable(True)
        self.btn_auto.toggled.connect(self._toggle_auto)
        self.lbl_summary = QLabel("就绪")
        bar.addWidget(self.btn_refresh)
        bar.addWidget(self.btn_auto)
        bar.addSpacing(12)
        bar.addWidget(self.lbl_summary)
        bar.addStretch(1)
        layout.addLayout(bar)

        # ── 主体：左项目树 / 右场次表 ──
        splitter = QSplitter(Qt.Horizontal)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["实验项目", "场次", "余量"])
        self.tree.setColumnWidth(0, 300)
        self.tree.currentItemChanged.connect(self._on_project_selected)
        splitter.addWidget(self.tree)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["场次 ID", "时间", "地点", "已选", "容量", "余量"])
        self.table.setSortingEnabled(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        splitter.addWidget(self.table)
        splitter.setSizes([430, 790])
        layout.addWidget(splitter, 3)

        # ── 日志区 ──
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.log.setFont(QFont("Consolas", 9))
        self.log.setFixedHeight(160)
        layout.addWidget(self.log, 1)

        self._timer = QTimer(self)
        self._timer.setInterval(60_000)
        self._timer.timeout.connect(self.reload)

        self._refresh_session_label()
        self.log_line("界面已启动。点击「刷新数据」从接口拉取真实数据（只读 GET）。")

    # ── 界面辅助 ──

    def log_line(self, text: str) -> None:
        stamp = dt.datetime.now().strftime("%H:%M:%S")
        self.log.appendPlainText(f"[{stamp}] {text}")

    def _refresh_session_label(self) -> None:
        token = session.load_token()
        if not token:
            self.lbl_session.setText("会话：无（请先关闭本窗口，运行 python run.py login）")
            self.lbl_session.setStyleSheet("color:#C0392B;")
            return
        age = session.state_age_seconds()
        age_text = "刚刚" if age is None or age < 5 else f"{int(age)} 秒前"
        self.lbl_session.setText(
            f"会话：已保存（{age_text}） · {session.describe_token(token)}")
        self.lbl_session.setStyleSheet("color:#188A3E;")

    def _toggle_auto(self, checked: bool) -> None:
        self.btn_auto.setText("自动刷新：开（60s）" if checked else "自动刷新：关")
        if checked:
            self._timer.start()
            self.log_line("已开启自动刷新（每 60 秒，只读、低频）。")
        else:
            self._timer.stop()
            self.log_line("已关闭自动刷新。")

    # ── 数据加载 ──

    def reload(self) -> None:
        if getattr(self, "_loader", None) is not None and self._loader.isRunning():
            self.log_line("上一轮加载还在进行，忽略本次刷新。")
            return
        self.btn_refresh.setEnabled(False)
        self.lbl_summary.setText("加载中…")
        self._refresh_session_label()
        self._loader = Loader()
        self._loader.progress.connect(self.log_line)
        self._loader.loaded.connect(self._on_loaded)
        self._loader.failed.connect(self._on_failed)
        self._loader.finished.connect(lambda: self.btn_refresh.setEnabled(True))
        self._loader.start()

    def _on_failed(self, message: str) -> None:
        self.lbl_summary.setText("加载失败")
        self.log_line(f"加载失败：{message}")
        if "401" in message or "token" in message:
            self.log_line("提示：token 有效期约 2 小时；请运行 python run.py login 重新登录。")

    def _on_loaded(self, payload: dict) -> None:
        self._payload = payload
        course = payload["courses"][0] if payload["courses"] else None
        self.tree.clear()

        for entry in payload["courses"]:
            course_item = QTreeWidgetItem([str(entry.get("name") or ""), "", ""])
            course_item.setFirstColumnSpanned(False)
            course_item.setExpanded(True)
            for project in entry["projects"]:
                slots = project["slots"]
                free = sum(1 for s in slots if (s["remaining"] or 0) > 0)
                item = QTreeWidgetItem([project["name"], str(len(slots)), str(free)])
                item.setData(0, Qt.UserRole, (entry["course_id"], project["project_id"]))
                item.setForeground(2, GREEN if free else GREY)
                course_item.addChild(item)
            self.tree.addTopLevelItem(course_item)

        totals = payload.get("totals", {})
        self.lbl_summary.setText(
            f"场次 {totals.get('slots', 0)} 条，其中有余量 {totals.get('free', 0)} 条")
        server_time = payload.get("server_time", "")
        local_time = payload.get("local_time", "")
        self.lbl_time.setText(f"服务端 {server_time[11:19]} · 本地 {local_time[11:19]}")
        self.log_line(f"加载完成：场次 {totals.get('slots', 0)} 条，有余额 {totals.get('free', 0)} 条")

        if course and self.tree.topLevelItemCount():
            # 自动选中有场次的第一个项目；都没有场次时退回第一个（并显示空状态说明）
            first_course = self.tree.topLevelItem(0)
            preferred = None
            for index in range(first_course.childCount()):
                child = first_course.child(index)
                if int(child.text(1) or 0) > 0:
                    preferred = child
                    break
            if preferred is None and first_course.childCount():
                preferred = first_course.child(0)
            if preferred is not None:
                self.tree.setCurrentItem(preferred)

    def _on_project_selected(self, current: QTreeWidgetItem | None, _previous=None) -> None:
        self.table.setRowCount(0)
        if current is None or current.parent() is None or self._payload is None:
            return
        data = current.data(0, Qt.UserRole)
        if not data:
            return
        course_id, project_id = data
        for entry in self._payload["courses"]:
            if entry["course_id"] != course_id:
                continue
            for project in entry["projects"]:
                if project["project_id"] != project_id:
                    continue
                self._fill_table(project["slots"])
                self.log_line(f"查看项目「{project['name']}」：{len(project['slots'])} 个场次")
                return

    def _fill_table(self, slots: list[dict]) -> None:
        self.table.setSortingEnabled(False)
        self.table.clearSpans()
        if not slots:
            # 空状态要讲清楚：0 场次是接口的真实返回，不是程序出错
            self.table.setRowCount(1)
            item = QTableWidgetItem("该项目当前没有已发布场次（这是接口的真实返回，不是程序出错）")
            item.setForeground(GREY)
            self.table.setItem(0, 0, item)
            self.table.setSpan(0, 0, 1, 6)
            return

        self.table.setRowCount(len(slots))
        for row, slot in enumerate(slots):
            remaining = slot["remaining"]
            values = [
                str(slot["slot_id"]),
                str(slot["time"]),
                str(slot["location"]),
                "-" if slot["taken"] is None else str(slot["taken"]),
                "-" if slot["capacity"] is None else str(slot["capacity"]),
                "未知" if remaining is None else str(remaining),
            ]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                if col >= 3:
                    item.setTextAlignment(Qt.AlignCenter)
                if col == 5:
                    item.setForeground(GREEN if (remaining or 0) > 0 else RED)
                self.table.setItem(row, col, item)
        self.table.setSortingEnabled(True)


def main() -> int:
    """启动 GUI。"""
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.show()
    # 启动后自动加载一次真实数据
    QTimer.singleShot(200, window.reload)
    return app.exec()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
