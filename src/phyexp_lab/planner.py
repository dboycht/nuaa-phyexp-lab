"""抢课规划：把"用户的空闲时段"变成"有序的候选场次清单"（**纯只读**）。

依据 `docs/抢课规格.md` 的规则（2026-10-07 用户逐项确认）：

- **R1** 空闲时段 = 具体日期 + 节次（`grabconfig.FreeSlot`）
- **R2** 同一「日期+节次」最多占 1 个实验（已占用的来自"我的选课记录"）
- **R3** 候选 = 落在空闲时段内 且 `余量 > 0`
- **R4** 优先级 = 余量多先抢；并列时 **日期早先**（再并列按节次顺序 → 场次 id，保证稳定）
- **R5** 不设总量上限（可选 `max_total`）
- **R8/D6** 同一实验项目不重复选（已选过的跳过）

本模块**只发 GET**：不做任何写操作（提交与回读核实由 `grabber`/CLI 负责）。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from . import grabconfig
from .api import PhyExpClient
from .grabconfig import FreeSlot, GrabPlan, PERIOD_ORDER


@dataclass(frozen=True)
class Candidate:
    """一个可提交的候选场次。"""

    slot_id: str
    date: str
    period: str
    project_id: str
    project_name: str
    location: str | None
    remaining: int
    capacity: int | None
    taken: int | None

    @property
    def free_key(self) -> tuple[str, str]:
        return (self.date, self.period)

    def describe(self) -> str:
        return (f"slot={self.slot_id:>6} 余{self.remaining:>3} {self.date} {self.period} "
                f"{self.location or '-':12s} [{self.project_name[:22]}]")


@dataclass
class Uncovered:
    """没能覆盖的空闲时段 + **原因**（A5 要求如实说明，不许粉饰）。"""

    free: FreeSlot
    reason: str

    def describe(self) -> str:
        return f"{self.free.describe()} —— {self.reason}"


@dataclass
class Plan:
    """一次抢课的候选计划。"""

    candidates: list[Candidate] = field(default_factory=list)
    uncovered: list[Uncovered] = field(default_factory=list)
    occupied: dict[tuple[str, str], str] = field(default_factory=dict)   # 已占时段 → 说明
    taken_projects: dict[str, str] = field(default_factory=dict)         # 项目 id → 名称

    @property
    def covered_keys(self) -> set[tuple[str, str]]:
        return {c.free_key for c in self.candidates}


def _slot_remaining(row: dict) -> int | None:
    try:
        return int(row.get("max_student_number")) - int(row.get("current_student_number"))
    except (TypeError, ValueError):
        return None


def load_course_slots(client: PhyExpClient, course_id: Any) -> tuple[list[dict], dict[str, str]]:
    """拉取课程下**全部已发布场次**与「项目 id → 项目名」映射。

    ⚠️ **实测（2026-10-07）**：`rest/schedules` 响应里内嵌的 `projects` 字段是 **null**
    （前端也是另外查项目表拿名字的）。所以项目名必须从 `course2projects` 的
    `projects(*)` 里取，**不能**指望场次行自带 —— 否则界面/日志里只会看到"项目 475"。
    """
    rows: list[dict] = []
    names: dict[str, str] = {}
    for project_row in client.course_projects(course_id):
        project_id = str(project_row.get("project_id") or "")
        embedded = project_row.get("projects") or {}
        name = str(embedded.get("name") or "").strip()
        if project_id and name:
            names[project_id] = name
        for row in client.slots(course_id, project_id=project_row.get("project_id"),
                               with_my_status=False):
            rows.append(row)
    return rows, names


def occupied_from_electives(client: PhyExpClient, semester_id: Any,
                            course_id: Any) -> tuple[dict[tuple[str, str], str], dict[str, str]]:
    """从"我的选课记录"算出：已占用的「日期+节次」与已选过的实验项目（R2/R8 的前置）。"""
    occupied: dict[tuple[str, str], str] = {}
    taken: dict[str, str] = {}
    for record in client.my_electives(semester_id, course_id):
        schedule = record.get("schedules") or {}
        date = str(schedule.get("date") or "")
        periods = schedule.get("periods") or {}
        period_raw = periods.get("name") or ""
        try:
            period = grabconfig.normalize_period(period_raw) if period_raw else ""
        except grabconfig.ConfigError:
            period = period_raw  # 名字不认得也照实记录，不阻断规划
        if date and period:
            occupied[(date, period)] = f"选课记录 id={record.get('id')}"
        project = schedule.get("projects")
        project_id = str(record.get("project_id") or "")
        if project_id:
            name = project.get("name") if isinstance(project, dict) else None
            taken[project_id] = str(name or project_id)
    return occupied, taken


def build_plan(plan_cfg: GrabPlan, *, rows: list[dict], course_id: Any,
               occupied: dict[tuple[str, str], str] | None = None,
               taken_projects: dict[str, str] | None = None,
               project_names: dict[str, str] | None = None) -> Plan:
    """用**已拉取的场次行**构建候选（纯函数，便于单测与演练）。"""
    occupied = dict(occupied or {})
    taken_projects = dict(taken_projects or {})
    project_names = dict(project_names or {})
    plan = Plan(occupied=occupied, taken_projects=taken_projects)

    wanted = {slot.key: slot for slot in plan_cfg.free_slots}
    per_free: dict[tuple[str, str], list[Candidate]] = {key: [] for key in wanted}
    seen_slot_ids: set[str] = set()

    for row in rows:
        date = str(row.get("date") or "")
        periods = row.get("periods") or {}
        period_raw = periods.get("name") or ""
        if not period_raw:
            continue
        try:
            period = grabconfig.normalize_period(period_raw)
        except grabconfig.ConfigError:
            period = period_raw
        key = (date, period)
        if key not in wanted or key in occupied:
            continue
        remaining = _slot_remaining(row)
        if remaining is None or remaining <= 0:
            continue
        project_id = str(row.get("project_id") or "")
        if plan_cfg.skip_taken_projects and project_id in taken_projects:
            continue
        locations = row.get("locations") or {}
        projects = row.get("projects") or {}
        slot_id = str(row.get("id") or "")
        if not slot_id or slot_id in seen_slot_ids:
            continue
        seen_slot_ids.add(slot_id)
        # 项目名优先取"场次行内嵌"，为空则回退到 course2projects 的项目表映射（见 load_course_slots）
        name = str(projects.get("name") or "").strip() or project_names.get(project_id, "")
        per_free[key].append(Candidate(
            slot_id=slot_id,
            date=date,
            period=period,
            project_id=project_id,
            project_name=name or f"项目 {project_id}",
            location=locations.get("name"),
            remaining=remaining,
            capacity=_int_or_none(row.get("max_student_number")),
            taken=_int_or_none(row.get("current_student_number")),
        ))

    # 排序（R4/D4）：余量多的先；并列时日期早的；再并列按节次顺序、最后按场次 id（稳定）
    def sort_key(item: Candidate) -> tuple:
        if plan_cfg.priority == "date_asc":
            return (item.date, PERIOD_ORDER.get(item.period, 99), -item.remaining, item.slot_id)
        return (-item.remaining, item.date, PERIOD_ORDER.get(item.period, 99), item.slot_id)

    # **同一时段最多占 1 个**（R2）：每个空闲时段只挑排序最靠前的那一个
    for key, candidates in per_free.items():
        if not candidates:
            continue
        candidates.sort(key=sort_key)
        plan.candidates.append(candidates[0])

    plan.candidates.sort(key=sort_key)
    if plan_cfg.max_total and plan_cfg.max_total > 0:
        plan.candidates = plan.candidates[: plan_cfg.max_total]

    # 未覆盖的空闲时段（A5：必须给出原因）
    # ⚠️ 顺序很重要：**先判"已占"再判"未覆盖"** —— 否则"已占用的时段"会同时出现在
    #    "已选"与"未覆盖"两处，自相矛盾（实测踩到：演练模拟占用时被报成未覆盖）。
    covered = plan.covered_keys
    for key, free in wanted.items():
        if key in occupied:
            continue                      # 已占用的时段不算"未覆盖"，也不需要在候选里
        if key in covered:
            continue
        published = [r for r in rows
                     if str(r.get("date") or "") == free.date
                     and _period_of(r) == free.period]
        if not published:
            reason = "系统在该日期没有这个节次的场次（可能未排课/未发布）"
        elif all((_slot_remaining(r) or 0) <= 0 for r in published):
            reason = f"该节次 {len(published)} 个场次都已满（余量 0）"
        else:
            reason = "候选都被过滤掉了（可能都是已选过的实验项目）"
        plan.uncovered.append(Uncovered(free=free, reason=reason))

    return plan


def _period_of(row: dict) -> str:
    periods = row.get("periods") or {}
    raw = periods.get("name") or ""
    if not raw:
        return ""
    try:
        return grabconfig.normalize_period(raw)
    except grabconfig.ConfigError:
        return raw


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def fetch_plan(client: PhyExpClient, plan_cfg: GrabPlan) -> tuple[Plan, Any, Any]:
    """联网构建计划；返回 `(Plan, semester_id, course_id)`。"""
    semesters = client.open_semesters()
    if not semesters:
        raise RuntimeError("当前没有开放学期（选课窗口可能没开）")
    semester = semesters[0]
    courses = client.my_courses(semester.get("id"))
    if not courses:
        raise RuntimeError(f"学期 id={semester.get('id')} 下没有我的课程")
    course_id = plan_cfg.course_id
    if course_id is None:
        course_id = courses[0].get("id")
    course_ids = [str(c.get("id")) for c in courses]
    if str(course_id) not in course_ids:
        raise RuntimeError(f"配置里的 course_id={course_id} 不在我的课程里：{', '.join(course_ids)}")

    rows, project_names = load_course_slots(client, course_id)
    occupied, taken = occupied_from_electives(client, semester.get("id"), course_id)
    return build_plan(plan_cfg, rows=rows, course_id=course_id,
                      occupied=occupied, taken_projects=taken,
                      project_names=project_names), semester.get("id"), course_id


def render_plan(plan: Plan, plan_cfg: GrabPlan | None = None, *, now: dt.datetime | None = None) -> list[str]:
    """把计划渲染成给使用者看的行（纯文本，不写 markdown 标记）。"""
    now = now or dt.datetime.now()
    plan_cfg = plan_cfg or GrabPlan(free_slots=[])
    lines: list[str] = []
    lines.append(f"空闲时段 {len(plan_cfg.free_slots)} 个；已占 {len(plan.occupied)} 个；"
                 f"已选实验 {len(plan.taken_projects)} 个")
    lines.append(f"排序策略：{'余量多的先抢' if plan_cfg.priority == 'remaining_desc' else '日期早的先抢'}"
                 f"；上限：{'不设' if not plan_cfg.max_total else plan_cfg.max_total}")
    lines.append("")
    lines.append(f"候选场次（{len(plan.candidates)} 个，按顺序提交）：")
    if plan.candidates:
        for index, item in enumerate(plan.candidates, 1):
            lines.append(f"  {index:>2}. {item.describe()}")
    else:
        lines.append("  （没有可用候选）")
    if plan.uncovered:
        lines.append("")
        lines.append(f"未能覆盖的空闲时段（{len(plan.uncovered)} 个）：")
        for item in plan.uncovered:
            lines.append(f"  - {item.describe()}")
    else:
        lines.append("")
        lines.append("未能覆盖的空闲时段：无（全覆盖）")
    if plan.taken_projects:
        lines.append("")
        lines.append(f"已选实验（本轮跳过）：{', '.join(sorted(plan.taken_projects.values()))}")
    lines.append("")
    lines.append(f"（生成于 {now.astimezone().isoformat(timespec='seconds')}）")
    return lines
