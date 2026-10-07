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
    #: 机器可读的原因分类（决定「值不值得重试」）：
    #: `none`=该节次还没放课 / `full`=有场次但都满了 / `submitted_failed`=有余量但提交没成功 /
    #: `all_elected`=只放了你已选过的实验 / `unknown`=其他
    kind: str = "unknown"

    def describe(self) -> str:
        return f"{self.free.describe()} —— {self.reason}"


#: 这些原因**总是**值得重试（还没放课 → 到点后可能才放出来；提交失败 → 重试就可能成功）
RETRY_ALWAYS: tuple[str, ...] = ("none", "submitted_failed")
#: 这些原因**只有开启「捡漏」时**才重试（已满 → 只能等别人退课）
RETRY_IF_HUNT: tuple[str, ...] = ("full",)


def retryable(kind: str, *, hunt_drops: bool = False) -> bool:
    """该「未覆盖」原因是否值得再试一轮（纯函数，便于单测）。

    用户口径（2026-10-07）：「只有【这个时间段空闲且剩余还有课但是提交失败】的情况下继续重试提交」
    ⇒ 提交失败、还没放课都重试；**已满默认不空等**（要捡漏得显式打开开关）；
    「只放了你已选过的实验」永远不重试（再试也抢不到）。
    """
    if kind in RETRY_ALWAYS:
        return True
    return bool(hunt_drops) and kind in RETRY_IF_HUNT


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


def my_elections(client: PhyExpClient, semester_id: Any, course_id: Any) -> list[dict]:
    """我的选课**明细**（每条的日期/节次/科目名），界面显示与去重共用这一份解析。

    返回按 (日期, 节次) 排序的 `[{"date","period","name","project_id","record_id"}]`。

    为什么单独抽出来：左下角要显示「**本周期（表格窗口）内**已选的科目」，
    需要日期 + 科目名一起；而原来 `occupied_from_electives` 只留下"选课记录 id"，
    科目名又在另一个只按 project_id 归并的字典里，两边凑不出"某天某个节次是哪门实验"。
    """
    items: list[dict] = []
    for record in client.my_electives(semester_id, course_id):
        schedule = record.get("schedules") or {}
        date = str(schedule.get("date") or "")
        periods = schedule.get("periods") or {}
        period_raw = periods.get("name") or ""
        try:
            period = grabconfig.normalize_period(period_raw) if period_raw else ""
        except grabconfig.ConfigError:
            period = period_raw  # 名字不认得也照实记录，不阻断规划
        if not (date and period):
            continue
        project = schedule.get("projects")
        project_id = str(record.get("project_id") or "")
        name = (project.get("name") if isinstance(project, dict) else None) or project_id
        items.append({"date": date, "period": period, "name": str(name),
                      "project_id": project_id, "record_id": record.get("id")})
    items.sort(key=lambda item: (item["date"], item["period"]))
    return items


def occupied_from_electives(client: PhyExpClient, semester_id: Any,
                            course_id: Any) -> tuple[dict[tuple[str, str], str], dict[str, str]]:
    """从"我的选课记录"算出：已占用的「日期+节次」与已选过的实验项目（R2/R8 的前置）。

    解析统一走 `my_elections()`（单一来源，避免两处各解析一遍而口径不同）。
    """
    occupied: dict[tuple[str, str], str] = {}
    taken: dict[str, str] = {}
    for item in my_elections(client, semester_id, course_id):
        occupied[(item["date"], item["period"])] = f"选课记录 id={item['record_id']}"
        if item["project_id"]:
            taken[item["project_id"]] = item["name"]
    return occupied, taken


def build_plan(plan_cfg: GrabPlan, *, rows: list[dict], course_id: Any,
               occupied: dict[tuple[str, str], str] | None = None,
               taken_projects: dict[str, str] | None = None,
               project_names: dict[str, str] | None = None,
               occupied_as_covered: bool = True) -> Plan:
    """用**已拉取的场次行**构建候选（纯函数，便于单测与演练）。

    `occupied_as_covered` 决定 `uncovered` 的口径：

    - `True`（默认，用于**规划展示**）：已占用的时段不算"未覆盖"（因为那是"已经有了"）；
    - `False`（用于**执行后的核对**）：未覆盖 = **空闲时段 − 已占用的**，
      即"有候选"**不算**已覆盖 —— 因为候选随时可能提交失败（已满/限流）。
      ⚠️ 早期版本一律用候选算覆盖，导致"提交全失败也说全覆盖"与"重试轮只跑一轮"两个静默错误。
    """
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
        # （`taken_projects` 来自"我的选课记录"，语义是**已选过**，不是"已完成"）
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
        if occupied_as_covered and key in covered:
            continue
        published = [r for r in rows
                     if str(r.get("date") or "") == free.date
                     and _period_of(r) == free.period]
        kind = "unknown"
        if not published:
            kind = "none"
            reason = "系统在该日期没有这个节次的场次（可能未排课/未发布）"
        elif all((_slot_remaining(r) or 0) <= 0 for r in published):
            kind = "full"
            reason = f"该节次 {len(published)} 个场次都已满（余量 0）"
        elif key in covered:
            # ⚠️ **有候选但还没成功**（调用方用 occupied_as_covered=False 时会走到这里）——
            # 必须与「只放了你已选过的实验」区分开：这两种情况的重试价值完全相反。
            # 实测教训：原来只按"有没有非满场次"判断，把"提交失败"错判成"没得选"，导致不再重试。
            kind = "submitted_failed"
            reason = "该时段有可用候选，但这次没提交成功（见上方失败明细）"
        else:
            kind = "all_elected"
            names_blocked = sorted({project_names.get(str(r.get("project_id") or ""),
                                                       f"项目 {r.get('project_id')}")
                                    for r in published})
            reason = "该节次只放了这些实验，你都已选过：" + "、".join(names_blocked)
        plan.uncovered.append(Uncovered(free=free, reason=reason, kind=kind))

    return plan


def planned_days(start: dt.date, *, weeks: int = 2) -> list[str]:
    """返回从 `start` 起 `weeks` 周的日期串（含 start，共 weeks*7 天）。

    用户 2026-10-07 确认：日历上的"两周"**从抢课当天起算**
    （实验在周一上午 10:00 开抢，本周与下周的场次通常一起放出来）。
    """
    if weeks < 1:
        raise ValueError(f"weeks 至少为 1，实际是 {weeks}")
    return [(start + dt.timedelta(days=offset)).isoformat() for offset in range(weeks * 7)]


def grid_cells(plan_cfg: GrabPlan, rows: list[dict], *, days: list[str],
               occupied: dict[tuple[str, str], str] | None = None,
               project_names: dict[str, str] | None = None,
               taken_projects: dict[str, str] | None = None) -> dict[tuple[str, str], dict]:
    """把场次行整理成"网格单元"：`(日期, 节次) -> 该单元的状态`。

    每个单元给出 GUI 需要的最小信息（**纯函数，便于单测**）：

    - `state`: `available`（可约）/ `full`（已满）/ `none`（系统没有这个节次）/
      `taken`（该时段已有我的选课）/ `all_elected`（该节次只放了你**已选过**的实验 ⇒ 没有新实验可抢）
    - `remaining` / `total`：余量与场次数
    - `best_slot_id`：余量最多的那个场次 id（点选后提交它）
    - `project_name`：那个场次的实验名（界面提示用）

    ⚠️ **只统计未做过的实验项目**（D6/R8：已选过的实验不能再选）——
    否则界面会把"点了也会被跳过"的单元格显示成可点，属于误导。
    若某节次放的实验**你都已经选过**，状态给 `all_elected`，
    界面据此显示"无新实验"并说明涉及哪些实验（**不写"已做过"** —— 本项目不查考勤，
    只知"已选过"；写成"做过"会让用户以为"未来的实验也做完了"，实测引起过困惑）。
    """
    occupied = dict(occupied or {})
    project_names = dict(project_names or {})
    taken_ids = {str(pid) for pid in (taken_projects or {})}
    # ⚠️ 去重口径（用户 2026-10-07 明确确认"不改"）：**只要有 elected 记录就跳过**，
    #    不区分是否签到。虽然接口能读到考勤（`att_status`: att_y 签到 / att_n 未签到），
    #    但用户选择不做"漏做可补抢"的细分 ⇒ 不要擅自改成按考勤判断。
    #    （实测依据见 `docs/接口逆向.md` §3.4.2）
    cells: dict[tuple[str, str], dict] = {}

    for date in days:
        for period in grabconfig.PERIODS:
            key = (date, period)
            candidates = [r for r in rows
                          if str(r.get("date") or "") == date and _period_of(r) == period]
            if key in occupied:
                cells[key] = {"state": "taken", "remaining": 0, "total": len(candidates),
                              "best_slot_id": None, "project_name": "",
                              "reason": occupied[key]}
                continue
            if not candidates:
                cells[key] = {"state": "none", "remaining": 0, "total": 0,
                              "best_slot_id": None, "project_name": "",
                              "reason": "系统未排该节次"}
                continue
            # D6/R8：**已选过**的实验不再出现在可选项里
            #   ⚠️ 注意口径：这里只知道"已选过（elected）"，**不查考勤**，
            #   所以措辞一律用"已选过"，不要写成"已做过"（用户 2026-10-07 因此困惑过）。
            fresh = [r for r in candidates if str(r.get("project_id") or "") not in taken_ids]
            if not fresh:
                blocked = sorted({str(r.get("project_id") or "") for r in candidates})
                blocked_names = [project_names.get(pid, f"项目 {pid}") for pid in blocked]
                cells[key] = {"state": "all_elected", "remaining": 0, "total": len(candidates),
                              "best_slot_id": None, "project_name": "",
                              "blocked_projects": blocked_names,
                              "reason": "该节次只放了这些实验，你都已选过：" + "、".join(blocked_names)}
                continue
            usable = [r for r in fresh if (_slot_remaining(r) or 0) > 0]
            if not usable:
                cells[key] = {"state": "full", "remaining": 0, "total": len(candidates),
                              "best_slot_id": None, "project_name": "",
                              "reason": "已满"}
                continue
            usable.sort(key=lambda r: (-(_slot_remaining(r) or 0), str(r.get("id"))))
            best = usable[0]
            project_id = str(best.get("project_id") or "")
            cells[key] = {
                "state": "available",
                "remaining": max((_slot_remaining(r) or 0) for r in usable),
                "total": len(candidates),
                "best_slot_id": str(best.get("id") or ""),
                "project_name": project_names.get(project_id) or f"项目 {project_id}",
                "reason": "",
            }
    return cells


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
