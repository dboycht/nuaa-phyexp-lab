"""抢课配置（G1）：运行时目录存真实配置，仓库里只放示例。

落点（2026-10-07 用户确认）
--------------------------
- 真实配置：``%LOCALAPPDATA%\\PhyExpLab\\config.json``（可用 ``PHYEXP_HOME`` 覆盖）——
  **绝不进仓库**（里面有使用者的目标日期/场次）；
- 仓库内：``config.example.json`` 示例 + 本文档的字段说明；
- 缺配置时：**自动生成一份默认配置**（含注释性说明字段）并**明确提示路径**，
  绝不静默使用内置默认值就跑（否则使用者以为"已经在按我的偏好抢了"）。

安全默认
--------
``dry_run`` 默认 **true**：不加显式开关绝不发写请求。这是刻意的 —— 抢课是**有副作用**的写操作。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config as app_config

#: 系统固定 5 个节次（**实测**，2026-10-07；名字里的标点必须与系统一致）。
PERIODS: tuple[str, ...] = (
    "上午1、2节",
    "上午3、4节",
    "下午5、6节",
    "下午7、8节",
    "晚上9，10节",
)

#: 节次的顺序（用于"按节次顺序"排序与展示）。
PERIOD_ORDER: dict[str, int] = {name: index for index, name in enumerate(PERIODS)}

#: 常见的等价写法 → 规范名。用户手写配置时不必精确复制系统里的标点。
PERIOD_ALIASES: dict[str, str] = {
    "上午1、2节": "上午1、2节", "上午1,2节": "上午1、2节", "上午12节": "上午1、2节",
    "上午3、4节": "上午3、4节", "上午3,4节": "上午3、4节", "上午34节": "上午3、4节",
    "下午5、6节": "下午5、6节", "下午5,6节": "下午5、6节", "下午56节": "下午5、6节",
    "下午7、8节": "下午7、8节", "下午7,8节": "下午7、8节", "下午78节": "下午7、8节",
    "晚上9，10节": "晚上9，10节", "晚上9、10节": "晚上9，10节", "晚上9,10节": "晚上9，10节",
    "晚上910节": "晚上9，10节",
}


class ConfigError(RuntimeError):
    """配置可预期错误（消息面向使用者，可直接打印）。"""


def normalize_period(name: str) -> str:
    """把用户写的节次名归一成系统名；无法识别时抛 `ConfigError` 并列出合法取值。

    为什么不"返回 None 当没有候选"：静默把"配置写错"变成"今天没课可抢"，
    会让人以为是系统没放课 —— 必须**当场报错**。
    """
    raw = (name or "").strip()
    if raw in PERIODS:
        return raw
    compact = raw.replace(" ", "").replace("，", ",").replace("、", ",")
    for alias, canonical in PERIOD_ALIASES.items():
        if compact == alias.replace("，", ",").replace("、", ","):
            return canonical
    # 再退一步：只留关键数字与"上午/下午/晚上"
    digits = "".join(ch for ch in compact if ch.isdigit())
    phase = next((p for p in ("上午", "下午", "晚上") if p in compact), "")
    for canonical in PERIODS:
        c_digits = "".join(ch for ch in canonical if ch.isdigit())
        if phase and phase in canonical and digits and digits == c_digits:
            return canonical
    raise ConfigError(
        f"无法识别的节次名：{name!r}；合法取值（必须与系统一致）：{' / '.join(PERIODS)}"
    )


@dataclass(frozen=True)
class FreeSlot:
    """一个空闲时段 = 具体日期 + 节次（用户按日历勾选）。

    ⚠️ **构造即校验节次**：节次名必须能被 `normalize_period` 识别成系统名，
    否则配置里把节次写错就会**静默变成"没有候选"**（看起来像系统没放课）——
    这是本项目明确要避免的失败模式，所以校验放在构造函数里，**任何构造路径都绕不过**。
    """

    date: str          #: 形如 2026-10-14
    period: str        #: 规范节次名（见 PERIODS）

    def __post_init__(self) -> None:
        normalize_period(self.period)   # 不合法时抛 ConfigError（消息里列出合法取值）

    @property
    def key(self) -> tuple[str, str]:
        return (self.date, self.period)

    def describe(self) -> str:
        return f"{self.date} {self.period}"

    @staticmethod
    def parse(raw: Any) -> "FreeSlot":
        if not isinstance(raw, dict):
            raise ConfigError(f"free_slots 里每一项应为对象（如 {{\"date\":\"2026-10-14\",\"period\":\"下午5、6节\"}}），实际是 {raw!r}")
        date = str(raw.get("date", "")).strip()
        if not date or len(date.split("-")) != 3:
            raise ConfigError(f"free_slots 里的 date 必须是 YYYY-MM-DD，实际是 {raw.get('date')!r}")
        year, month, day = date.split("-")
        if not (year.isdigit() and month.isdigit() and day.isdigit()):
            raise ConfigError(f"free_slots 里的 date 必须是 YYYY-MM-DD，实际是 {date!r}")
        return FreeSlot(date=f"{int(year):04d}-{int(month):02d}-{int(day):02d}",
                        period=normalize_period(str(raw.get("period", ""))))


@dataclass
class GrabPlan:
    """抢课配置（对应 config.json）。"""

    #: 课程 id；None = 取"我的第一门课"
    course_id: Any | None = None
    #: 空闲时段（R1）
    free_slots: list[FreeSlot] = field(default_factory=list)
    #: 排序策略：remaining_desc（默认）/ date_asc
    priority: str = "remaining_desc"
    #: 总选课上限；0 = 不设上限（R5）
    max_total: int = 0
    #: 同一实验不重复选（R8）
    skip_taken_projects: bool = True
    #: 安全默认：演练
    dry_run: bool = True
    #: 桌面通知（D7）
    notify: bool = True
    #: 到点自动开抢的目标时刻（本地 HH:MM:SS，配合 --at 使用；None = 手动即时执行）
    target_at: str | None = None
    #: 到点后的**重试轮数**（2026-10-07 用户确认："重试固定次数后停"）；1 = 只抢一轮
    retry_rounds: int = 10
    #: 每一轮之间的间隔秒数（默认 30s：抢课窗口内足够快，又不至于高频打扰系统）
    retry_interval_seconds: float = 30.0
    #: 提交参数（沿用已按实测校准的 grabber.GrabConfig 默认值）
    submit: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.free_slots:
            raise ConfigError(
                "free_slots 为空：请先告诉我要填哪些空闲时段（具体日期 + 节次）。\n"
                f"       合法节次：{' / '.join(PERIODS)}\n"
                "       示例：\"free_slots\": [{\"date\": \"2026-10-14\", \"period\": \"下午5、6节\"}]"
            )
        if self.priority not in ("remaining_desc", "date_asc"):
            raise ConfigError(f"priority 只能是 remaining_desc / date_asc，实际是 {self.priority!r}")
        if self.max_total < 0:
            raise ConfigError(f"max_total 不能为负（0 = 不设上限），实际是 {self.max_total}")
        if self.retry_rounds < 1:
            raise ConfigError(f"retry_rounds 至少为 1（只抢一轮），实际是 {self.retry_rounds}")
        if self.retry_interval_seconds < 0:
            raise ConfigError(f"retry_interval_seconds 不能为负，实际是 {self.retry_interval_seconds}")
        seen: set[tuple[str, str]] = set()
        for slot in self.free_slots:
            if slot.key in seen:
                raise ConfigError(f"free_slots 里有重复项：{slot.describe()}")
            seen.add(slot.key)
        if self.target_at is not None:
            parts = str(self.target_at).split(":")
            if len(parts) != 3 or not all(p.isdigit() for p in parts):
                raise ConfigError(f"target_at 必须是 HH:MM:SS，实际是 {self.target_at!r}")

    # ── 读写 ──

    def to_dict(self) -> dict[str, Any]:
        return {
            "course_id": self.course_id,
            "free_slots": [{"date": s.date, "period": s.period} for s in self.free_slots],
            "priority": self.priority,
            "max_total": self.max_total,
            "skip_taken_projects": self.skip_taken_projects,
            "dry_run": self.dry_run,
            "notify": self.notify,
            "target_at": self.target_at,
            "retry_rounds": self.retry_rounds,
            "retry_interval_seconds": self.retry_interval_seconds,
            "submit": self.submit or {
                "prewarm": True,
                "pre_fire_offset_ms": 50,
                "min_submit_interval_ms": 800,
                "max_attempts_per_target": 3,
            },
        }

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "GrabPlan":
        if not isinstance(raw, dict):
            raise ConfigError("config.json 顶层必须是对象")
        plan = GrabPlan(
            course_id=raw.get("course_id"),
            free_slots=[FreeSlot.parse(item) for item in (raw.get("free_slots") or [])],
            priority=str(raw.get("priority") or "remaining_desc"),
            max_total=int(raw.get("max_total") or 0),
            skip_taken_projects=bool(raw.get("skip_taken_projects", True)),
            dry_run=bool(raw.get("dry_run", True)),
            notify=bool(raw.get("notify", True)),
            target_at=raw.get("target_at"),
            retry_rounds=int(raw.get("retry_rounds") or 10),
            retry_interval_seconds=float(raw.get("retry_interval_seconds") or 30.0),
            submit=dict(raw.get("submit") or {}),
        )
        return plan


def config_path() -> Path:
    """真实配置文件路径（运行时目录，绝不进仓库）。"""
    return app_config.home_dir() / "config.json"


def example_path() -> Path:
    """仓库内示例配置路径（源码树里的 config.example.json）。"""
    return Path(__file__).resolve().parents[2] / "config.example.json"


def load_config(path: Path | str | None = None) -> GrabPlan:
    """读取配置；文件不存在时**生成默认配置**并抛错提示（不静默使用内置默认值）。"""
    target = Path(path) if path else config_path()
    if not target.is_file():
        plan = GrabPlan()
        write_config(plan, target)
        raise ConfigError(
            f"没有找到配置文件，已生成默认配置 → {target}\n"
            "       请先填写 free_slots（空闲的具体日期 + 节次），然后再跑一次。\n"
            f"       合法节次：{' / '.join(PERIODS)}"
        )
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ConfigError(f"配置文件不是合法 JSON：{target}（{exc}）") from exc
    plan = GrabPlan.from_dict(raw)
    return plan


def write_config(plan: GrabPlan, path: Path | str | None = None) -> Path:
    """写配置（UTF-8 无 BOM；保持人类可读的缩进）。"""
    target = Path(path) if path else config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    return target
