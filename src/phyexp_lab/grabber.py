"""抢课（提交）引擎：按目标时间点预发射提交，带间隔控制与限速退避。

**当前状态：未实现（阶段 0）** —— 依赖 `api.PhyExpClient` 的接口逆向结果。

移植要点（来自 NUAA-Snatcher 的实战经验，实现时逐条落实）
-------------------------------------------------------
1. **预发射补偿**：按 `pre_fire_offset_ms` 提前发请求，抵消网络与服务端处理延迟；
2. **提交间隔下限**：低于 `min_submit_interval_ms` 极易触发服务端限速，务必守住；
3. **多 URL 容错**：同一动作可能存在多个等价入口（参数名/路径差异），逐个尝试；
4. **结果四分类**：成功 / 已满 / 被拒 / 限速 —— 且**必须区分「失败」与「未知」**
   （超时、非预期响应一律记为 `Outcome.UNKNOWN` 并保留原始文本，不许当成功处理）。
"""

from __future__ import annotations

from dataclasses import dataclass

from .api import PhyExpClient
from .models import BookingAttempt


@dataclass
class GrabConfig:
    """提交策略参数。"""

    #: 提前多少毫秒发送请求（校园网经验值 100–300ms）
    pre_fire_offset_ms: int = 200
    #: 两次提交之间的最小间隔（毫秒）。**不要低于 800**：NUAA-Snatcher 实测会被限速
    min_submit_interval_ms: int = 800
    #: 单个目标的最大尝试次数
    max_attempts_per_target: int = 20
    #: 命中限速后的退避（秒）
    rate_limit_backoff_seconds: float = 3.0


class Grabber:
    """对一组目标时段执行「定时 + 重试」的提交动作。"""

    def __init__(self, client: PhyExpClient, config: GrabConfig | None = None) -> None:
        self.client = client
        self.config = config or GrabConfig()

    def submit_once(self, slot_id: str) -> BookingAttempt:
        """向单个时段提交一次预约，并把结果分类。"""
        raise NotImplementedError(
            "提交动作依赖尚未逆向的接口（PhyExpClient.submit_booking）；"
            "先完成 `python run.py recon` 采集并填写 docs/接口逆向.md。"
        )

    def run_until(self, slot_ids: list[str], target_epoch: float) -> list[BookingAttempt]:
        """等到 `target_epoch`（秒级时间戳）后按策略提交，返回全部尝试记录。"""
        raise NotImplementedError("同上：待接口逆向完成后实现。")
