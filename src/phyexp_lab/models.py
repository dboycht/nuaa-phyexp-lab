"""数据模型：物理实验项目、可选时段与预约结果。

⚠️ 字段为**基于需求的设计草案**，尚未与真实接口对齐：等 `docs/接口逆向.md`
里的接口清单填好后，按真实响应字段回填/改名（见该项目「接口逆向」待办清单）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


@dataclass(frozen=True)
class Experiment:
    """一个物理实验项目（如「迈克尔逊干涉仪」「示波器的使用」）。"""

    experiment_id: str
    name: str
    #: 实验类别/章节，用于分组展示（如果系统提供）
    category: str | None = None
    #: 单个时段容量上限（如果系统在项目层级就给出）
    capacity: int | None = None
    #: 原始响应片段，便于逆向时回溯未建模字段
    raw: dict = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class Slot:
    """一个「实验项目 × 时间」的可预约时段。"""

    slot_id: str
    experiment_id: str
    #: 形如 "2026-10-12 08:00-10:00" 的可读串，便于日志展示
    time_text: str = ""
    #: 已选人数 / 容量上限；系统未提供时保持 None（不要用 0 伪装「没人选」）
    taken: int | None = None
    capacity: int | None = None
    #: 地点/实验室房间号
    location: str | None = None
    raw: dict = field(default_factory=dict, compare=False)

    @property
    def remaining(self) -> int | None:
        """剩余名额。任一字段缺失时返回 None —— 未知就是未知，不猜。"""
        if self.taken is None or self.capacity is None:
            return None
        return self.capacity - self.taken

    @property
    def is_full(self) -> bool | None:
        remaining = self.remaining
        return None if remaining is None else remaining <= 0


class Outcome(str, Enum):
    """一次提交的结果分类（判据要点：**必须能区分「失败」与「未知」**）。"""

    SUCCESS = "success"
    FULL = "full"
    REJECTED = "rejected"
    RATE_LIMITED = "rate_limited"
    AUTH_EXPIRED = "auth_expired"
    UNKNOWN = "unknown"
    #: 演练（dry-run）——**绝不等同于成功**，单独一类，避免把彩排结果当成战果
    DRY_RUN = "dry_run"


@dataclass
class BookingAttempt:
    """一次预约提交的记录，用于日志与统计。"""

    slot_id: str
    #: 本地发起时刻（秒级浮点时间戳）
    started_at: float
    #: 服务端返回的原始文本（截断后保存）
    message: str = ""
    outcome: Outcome = Outcome.UNKNOWN
    http_status: int | None = None
    elapsed_ms: int | None = None
    #: 实际发射时刻（本地时间戳）
    attempted_at: float | None = None
    #: 与计划发射时刻的偏差（毫秒，正 = 晚于计划）——**调预发射偏移的唯一依据**
    deviation_ms: float | None = None
