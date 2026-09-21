"""样本分析：把逐轮余量样本变成「退课/被抢事件」与可复核的结论。

为什么需要它
------------
`watch` 采到的样本是一堆 `{场次: (已选, 容量, 余量)}` 快照，本身说明不了规律。
真正的信号是**相邻两轮之间的变化**：

- **余量增加** ⇒ 有人退课（对使用者是"捡漏机会"）；
- **余量减少** ⇒ 有人选上（竞争速度的直接度量）；
- **空位存活时间** = 从"余量由 0 变正"到"再次被占满"的时长 ⇒ 决定监控频率要多快。

本模块只做**如实统计**：样本期内没发生的事不写、样本量太小就标注"不足以支撑结论"。
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SlotMeta:
    """场次的静态信息（来自 snapshot；样本行里只有数字）。"""

    slot_id: str
    time_text: str = ""
    location: str = ""
    project_id: str = ""
    project_name: str = ""


@dataclass
class Event:
    """一次余量变化。"""

    at: dt.datetime
    slot_id: str
    kind: str  # drop / taken
    before: int
    after: int
    meta: SlotMeta | None = None

    def describe(self) -> str:
        who = self.meta.project_name if self.meta and self.meta.project_name else "?"
        when = self.meta.time_text if self.meta and self.meta.time_text else "?"
        arrow = f"{self.before} → {self.after}"
        label = "退课（余量增加）" if self.kind == "drop" else "被抢（余量减少）"
        return f"{self.at:%m-%d %H:%M:%S}  场次 {self.slot_id}  {label}  {arrow}  [{who} / {when}]"


@dataclass
class Report:
    """分析结果。"""

    files: list[str] = field(default_factory=list)
    rounds: int = 0
    slots_seen: int = 0
    first_at: dt.datetime | None = None
    last_at: dt.datetime | None = None
    events: list[Event] = field(default_factory=list)
    per_slot: dict[str, dict] = field(default_factory=dict)
    gaps: list[dict] = field(default_factory=list)          # 空位存活时间
    hour_histogram: dict[int, int] = field(default_factory=dict)
    meta: dict[str, SlotMeta] = field(default_factory=dict)

    @property
    def span_minutes(self) -> float:
        if not self.first_at or not self.last_at:
            return 0.0
        return (self.last_at - self.first_at).total_seconds() / 60.0

    def summary_lines(self) -> list[str]:
        drops = [e for e in self.events if e.kind == "drop"]
        taken = [e for e in self.events if e.kind == "taken"]
        lines = [
            f"样本文件        : {len(self.files)} 个",
            f"轮数 / 场次数   : {self.rounds} 轮 / {self.slots_seen} 个场次",
            f"时间跨度        : {self.first_at} ~ {self.last_at}"
            f"（{self.span_minutes:.1f} 分钟）" if self.first_at else "时间跨度        : 无",
            f"退课事件        : {len(drops)} 次（余量增加）",
            f"被抢事件        : {len(taken)} 次（余量减少）",
        ]
        return [str(x) for x in lines]


def load_snapshot_meta(paths: list[Path]) -> dict[str, SlotMeta]:
    """从 snapshot JSON 里建立 场次 id → 静态信息 的映射（取第一个命中的）。"""
    meta: dict[str, SlotMeta] = {}
    for path in paths:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for course in data.get("courses", []):
            for project in course.get("projects", []):
                for slot in project.get("slots", []):
                    slot_id = str(slot.get("slot_id", ""))
                    if slot_id and slot_id not in meta:
                        meta[slot_id] = SlotMeta(
                            slot_id=slot_id,
                            time_text=str(slot.get("time", "")),
                            location=str(slot.get("location", "")),
                            project_id=str(project.get("project_id", "")),
                            project_name=str(project.get("name", "")),
                        )
    return meta


def load_samples(paths: list[Path]) -> list[dict]:
    """读入所有样本行，按时间排序。"""
    rows: list[dict] = []
    for path in paths:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    rows.sort(key=lambda r: r.get("t", ""))
    return rows


def build_report(sample_paths: list[Path], meta: dict[str, SlotMeta]) -> Report:
    """核心分析：相邻两轮比对，生成事件、空位存活时间与小时分布。"""
    rows = load_samples(sample_paths)
    report = Report(files=[p.name for p in sample_paths], meta=meta)
    if not rows:
        return report

    # 轮数按**实际样本行数**计：不同文件各自从 1 开始编号，取 max 会低估样本量
    report.rounds = len(rows)
    report.first_at = _parse_time(rows[0].get("t"))
    report.last_at = _parse_time(rows[-1].get("t"))

    previous: dict[str, dict] = {}
    open_gap_since: dict[str, dt.datetime] = {}
    seen_slots: set[str] = set()

    for row in rows:
        at = _parse_time(row.get("t"))
        slots = row.get("slots") or {}
        seen_slots.update(slots.keys())
        for slot_id, value in slots.items():
            remaining = value.get("remaining")
            taken = value.get("taken")
            capacity = value.get("capacity")
            stat = report.per_slot.setdefault(slot_id, {
                "samples": 0, "min_remaining": None, "max_remaining": None,
                "changes": 0, "taken": taken, "capacity": capacity,
            })
            stat["samples"] += 1
            stat["taken"] = taken
            stat["capacity"] = capacity
            if remaining is None:
                continue
            stat["min_remaining"] = remaining if stat["min_remaining"] is None else min(stat["min_remaining"], remaining)
            stat["max_remaining"] = remaining if stat["max_remaining"] is None else max(stat["max_remaining"], remaining)

            old = previous.get(slot_id)
            if old is not None and old.get("remaining") is not None:
                before = old["remaining"]
                if remaining != before:
                    stat["changes"] += 1
                    kind = "drop" if remaining > before else "taken"
                    report.events.append(Event(at, slot_id, kind, before, remaining, meta.get(slot_id)))
                    if at:
                        report.hour_histogram[at.hour] = report.hour_histogram.get(at.hour, 0) + 1

            # 空位存活时间：0 → 正 记开始；回到 0 记结束（样本期内没回到 0 则标注未闭合）
            if at:
                if old is not None and old.get("remaining") == 0 and remaining and remaining > 0:
                    open_gap_since[slot_id] = at
                elif slot_id in open_gap_since and remaining == 0:
                    start = open_gap_since.pop(slot_id)
                    report.gaps.append({
                        "slot_id": slot_id,
                        "start": start,
                        "end": at,
                        "minutes": (at - start).total_seconds() / 60.0,
                        "meta": meta.get(slot_id),
                    })
            previous[slot_id] = value

    report.slots_seen = len(seen_slots)
    if report.last_at:
        for slot_id, start in open_gap_since.items():
            report.gaps.append({
                "slot_id": slot_id,
                "start": start,
                "end": None,
                "minutes": (report.last_at - start).total_seconds() / 60.0,
                "meta": meta.get(slot_id),
            })
    return report


def render_markdown(report: Report) -> str:
    """把报告渲染成 Markdown（用于回填 docs/排课与放课规律.md）。"""
    lines = ["# 余量采样分析报告", "",
             f"生成时间：{dt.datetime.now().astimezone().isoformat(timespec='seconds')}", ""]
    lines += [f"- {line}" for line in report.summary_lines()]
    lines.append("")

    drops = [e for e in report.events if e.kind == "drop"]
    taken = [e for e in report.events if e.kind == "taken"]

    lines.append("## 一、退课事件（余量增加 = 捡漏机会）")
    if drops:
        lines += [f"- {e.describe()}" for e in drops]
    else:
        lines.append("- 样本期内**没有**观测到退课事件。")
    lines.append("")

    lines.append("## 二、被抢事件（余量减少）")
    if taken:
        lines += [f"- {e.describe()}" for e in taken]
    else:
        lines.append("- 样本期内**没有**观测到余量减少事件。")
    lines.append("")

    lines.append("## 三、空位存活时间（余量由 0 变正 → 再被占满）")
    if report.gaps:
        for gap in report.gaps:
            who = gap["meta"].project_name if gap["meta"] else "?"
            closed = "已闭合" if gap["end"] else "样本期结束时仍空着"
            lines.append(f"- 场次 {gap['slot_id']} [{who}]：{gap['minutes']:.1f} 分钟（{closed}）")
    else:
        lines.append("- 样本期内未观测到「从满到有空位」的转变。")
    lines.append("")

    lines.append("## 四、变动最多 / 始终为空 / 始终满员")
    per = report.per_slot
    changed = sorted(((sid, s) for sid, s in per.items() if s["changes"] > 0),
                     key=lambda kv: -kv[1]["changes"])[:10]
    if changed:
        lines.append("| 场次 | 变动次数 | 余量区间 | 项目 |")
        lines.append("| --- | --- | --- | --- |")
        for slot_id, stat in changed:
            item = report.meta.get(slot_id)
            name = item.project_name if item and item.project_name else "（未采到项目信息）"
            lines.append(f"| {slot_id} | {stat['changes']} | {stat['min_remaining']}~{stat['max_remaining']} | {name} |")
    else:
        lines.append("- 所有场次在样本期内都**没有任何变化**。")
    lines.append("")

    free_now = [sid for sid, s in per.items() if (s["max_remaining"] or 0) > 0]
    lines.append(f"- 样本期内出现过空位的场次：{len(free_now)} 个")
    lines.append("")

    lines.append("## 五、事件的小时分布")
    if report.hour_histogram:
        for hour in sorted(report.hour_histogram):
            lines.append(f"- {hour:02d} 时：{report.hour_histogram[hour]} 次变动")
    else:
        lines.append("- 无事件，故无分布。")
    lines.append("")
    return "\n".join(lines)


def _parse_time(value) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value))
    except ValueError:
        return None
