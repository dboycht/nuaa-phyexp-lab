"""余量监控：低频轮询目标时段的剩余名额，发现空位后交给抢课引擎。

**当前状态：未实现（阶段 0）** —— 依赖 `api.PhyExpClient` 的接口逆向结果。

已确定的设计约束（来自需求和既有经验，实现时不要违背）
----------------------------------------------------
1. **低频**：轮询间隔不得低于 `min_interval_seconds`；命中限速时**指数退避**而不是硬顶。
2. **可中断**：监控是长跑进程，必须响应 Ctrl+C 且退出时不吞掉已收集的数据。
3. **先观察再动作**：放课规律（`docs/排课与放课规律.md`）研究清楚之前，监控只记录不提交。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .api import PhyExpClient
from .models import Slot


@dataclass
class MonitorConfig:
    """监控参数。默认值刻意保守，优先保证「不被限速/不打扰系统」。"""

    #: 两次轮询的最小间隔（秒）
    min_interval_seconds: float = 3.0
    #: 命中限速后的退避起点（秒）与倍率
    backoff_base_seconds: float = 5.0
    backoff_factor: float = 2.0
    #: 退避上限（秒）
    backoff_max_seconds: float = 120.0
    #: 关注的时段 ID（空 = 监控全部时段，仅做数据采集）
    watch_slot_ids: list[str] = field(default_factory=list)


class SlotMonitor:
    """按固定节奏采集时段余量，输出「余量变化事件」。"""

    def __init__(self, client: PhyExpClient, config: MonitorConfig | None = None) -> None:
        self.client = client
        self.config = config or MonitorConfig()

    def poll_once(self, slot_ids: list[str]) -> list[Slot]:
        """采集一轮，返回本轮读到的时段快照（余量未知的时段原样返回，不做臆测）。"""
        raise NotImplementedError(
            "监控依赖尚未逆向的接口（PhyExpClient.get_remaining）；"
            "先完成 `python run.py recon` 采集并填写 docs/接口逆向.md。"
        )

    def run(self, on_slot_change=None, max_rounds: int | None = None) -> None:
        """持续监控主循环（含限速退避）。

        `on_slot_change(prev, now)` 在每个时段余量发生变化时被调用；
        `max_rounds` 便于研究与测试时限定轮数。
        """
        raise NotImplementedError("同上：待接口逆向完成后实现。")
