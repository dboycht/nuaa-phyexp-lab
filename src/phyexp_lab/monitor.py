"""余量监控：低频轮询关注场次的剩余名额，记录变化并（未来）交给抢课引擎。

设计约束（都来自本项目实测，不是照抄别的项目）
--------------------------------------------
1. **连接必须复用并预热**：实测首次请求 1250ms（TLS 握手），keep-alive 稳态 11ms
   ⇒ 监控主循环全程复用同一个 `PhyExpClient` 会话，并在首轮前 `prewarm()`。
2. **低频 + 指数退避**：默认最小间隔 3 秒（研究期建议 60–300 秒）；命中限速/超时则退避，
   而不是硬顶重试。
3. **只读**：监控阶段**只发 GET**，不调用任何写接口。
4. **失败轮次不得污染数据**：一轮里某个项目查询失败时，只记录失败原因，**不把该轮写进样本**
   （否则"查失败"会被误算成"余量为 0"）。
5. **可中断且不丢数据**：样本逐条 append + flush，Ctrl+C 后已采数据仍在盘上。
"""

from __future__ import annotations

import datetime as dt
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from . import config
from .api import ApiError, PhyExpClient
from .models import Slot

LogFn = Callable[[str], None]


def _log(msg: str) -> None:
    print(msg, flush=True)


class FileLogger:
    """长跑任务用的进度记录器：**写文件为主，stdout 为辅**。

    为什么必须这样（2026-09-21 实测教训）：把进度只 `print` 到 stdout 的长跑任务，
    在输出管道被断开后会死在一次写操作上 —— 那时连 traceback 都写不出去，
    外部只能看到一个"裸 exit code 1"，**排查时毫无线索**。
    改为：每条进度先落盘（一定成功），再尽力 print；print 失败就永久关掉 stdout，不抛异常。
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stdout_ok = True
        self.wrote = 0

    def __call__(self, msg: str) -> None:
        stamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(f"{stamp} {msg}\n")
            self.wrote += 1
        except OSError:
            pass
        if self.stdout_ok:
            try:
                print(msg, flush=True)
            except (BrokenPipeError, OSError, ValueError):
                self.stdout_ok = False


@dataclass
class MonitorConfig:
    """监控参数。默认值刻意保守，优先保证「不被限速/不打扰系统」。"""

    #: 两次轮询的最小间隔（秒）
    min_interval_seconds: float = 3.0
    #: 命中限速后的退避起点（秒）与倍率
    backoff_base_seconds: float = 5.0
    backoff_factor: float = 2.0
    #: 退避上限（秒）
    backoff_max_seconds: float = 300.0
    #: 关注的实验项目（project_id）；空 = 全部
    watch_project_ids: list[str] = field(default_factory=list)


@dataclass
class SampleWriter:
    """把每一轮样本按 JSONL 追加落盘（默认在 `%LOCALAPPDATA%\\PhyExpLab\\samples\\`）。"""

    path: Path

    @classmethod
    def default(cls) -> "SampleWriter":
        config.ensure_home()
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        directory = config.home_dir() / "samples"
        directory.mkdir(parents=True, exist_ok=True)
        return cls(directory / f"samples-{stamp}.jsonl")

    def append(self, record: dict) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()


class SlotMonitor:
    """按固定节奏采集时段余量，输出「余量变化」事件并落盘。"""

    def __init__(self, client: PhyExpClient, config: MonitorConfig | None = None,
                 writer: SampleWriter | None = None, log: LogFn = _log) -> None:
        self.client = client
        self.config = config or MonitorConfig()
        self.writer = writer
        self.log = log
        self.rounds = 0
        self.failures = 0

    # ── 单轮采集 ──

    def poll_once(self, course_id, project_ids: Iterable) -> dict[str, Slot]:
        """采集一轮，返回 `{slot_id: Slot}`；**单个项目失败只记录、不中断整轮**。"""
        slots: dict[str, Slot] = {}
        for project_id in project_ids:
            try:
                rows = self.client.slots(course_id, project_id=project_id, with_my_status=False)
            except ApiError as exc:
                self.failures += 1
                self.log(f"[warn] 项目 {project_id} 采集失败（本轮不计入样本）：{exc}")
                continue
            for row in rows:
                slot = self.client.to_slot(row)
                slots[slot.slot_id] = slot
        return slots

    # ── 主循环 ──

    def run(self, course_id, project_ids: Iterable, *,
            max_rounds: int | None = None,
            on_change: Callable[[Slot, Slot | None], None] | None = None) -> None:
        """轮询主循环。

        `on_change(now, prev)` 在余量发生变化时被调用（`prev=None` 表示首次见到该场次）。
        `max_rounds` 便于研究与测试时限定轮数（None = 一直跑）。
        """
        project_ids = list(project_ids)
        warm_ms = self.client.prewarm()
        self.log(f"[info] 连接已预热（{warm_ms}ms）；正在监控 {len(project_ids)} 个实验项目")

        previous: dict[str, Slot] = {}
        interval = self.config.min_interval_seconds
        try:
            while max_rounds is None or self.rounds < max_rounds:
                started = time.time()
                slots = self.poll_once(course_id, project_ids)
                self.rounds += 1

                changes = 0
                for slot_id, slot in slots.items():
                    old = previous.get(slot_id)
                    if old is None or old.remaining != slot.remaining:
                        changes += 1
                        if on_change:
                            on_change(slot, old)
                previous.update(slots)

                if self.writer:
                    self.writer.append({
                        "t": dt.datetime.now().astimezone().isoformat(timespec="milliseconds"),
                        "round": self.rounds,
                        "slots": {
                            sid: {"taken": s.taken, "capacity": s.capacity, "remaining": s.remaining}
                            for sid, s in slots.items()
                        },
                        "changes": changes,
                    })

                self.log(f"[{self.rounds:>4}] 场次 {len(slots):>3} 条，变化 {changes:>2} 处，"
                         f"失败项目累计 {self.failures}")

                # 退避：有失败就拉长间隔，全部成功则回到最小间隔
                if self.failures:
                    interval = min(interval * self.config.backoff_factor, self.config.backoff_max_seconds)
                else:
                    interval = self.config.min_interval_seconds

                elapsed = time.time() - started
                time.sleep(max(0.0, interval - elapsed))
        except KeyboardInterrupt:
            self.log(f"[info] 收到 Ctrl+C，监控停止（已完成 {self.rounds} 轮，样本已落盘）")
