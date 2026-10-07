"""抢课执行引擎：把 `planner` 的候选清单真正提交掉，并逐条**回读核实**。

对应 `docs/抢课规格.md` 的规则：

- **R2** 同一「日期+节次」只占 1 个（成功后立刻记入 `occupied`，后续候选重新过滤）
- **R6** 每条提交后回读 `rest/user2projects` 确认 `schedule_id` 已 `elected` —— **不拿 HTTP 200 当成功**
- **R7** 失败（已满/限流/被拒）**降级到下一个候选**，不中断整轮
- **A4** `dry_run=True` 时**绝不发写请求**（且候选与排序与真实运行完全一致）
- **A5** 结束时如实报告"没能覆盖的空闲时段"及其原因
- **A7** 已选过的实验项目跳过（含本轮内已占用的节次）

写操作全部经由 `api.PhyExpClient.submit_booking`（返回 `WriteResult`，不抛异常），
所以这里的"失败"是**业务结果**，会被记录下来继续决策。
"""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import notify as notify_mod
from . import planner
from .api import ApiError, PhyExpClient
from .grabconfig import GrabPlan
from .planner import Candidate, Plan, Uncovered, build_plan

LogFn = Callable[[str], None]

#: token 快过期（剩余秒数）时先提醒；小于此值就认为"随时可能失效"
TOKEN_WARN_SECONDS = 300


def _log(msg: str) -> None:
    print(msg, flush=True)


@dataclass
class Attempt:
    """一次提交的结果（成功 / 失败 + 原因）。"""

    candidate: Candidate
    ok: bool
    outcome: str
    message: str
    http_status: int | None = None
    elapsed_ms: int | None = None
    verified: bool = False
    #: 回读核实拿到的选课记录 id（`rest/user2projects.id`）—— 退课要用它
    record_id: Any | None = None

    def describe(self) -> str:
        status = "-" if self.http_status is None else str(self.http_status)
        return (f"{'成功' if self.ok else '失败'} slot={self.candidate.slot_id} "
                f"[{self.outcome}] HTTP {status} {self.elapsed_ms if self.elapsed_ms is not None else '-'}ms "
                f"{self.candidate.date} {self.candidate.period} "
                f"[{self.candidate.project_name[:20]}]：{self.message[:80]}")


@dataclass
class RunReport:
    """一轮抢课的完整结果（终端输出、通知与日志都从它生成）。"""

    dry_run: bool
    attempts: list[Attempt] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    uncovered: list[Uncovered] = field(default_factory=list)
    planned: list[Candidate] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: 演练模式下"本应提交"的候选（**不是成功**；单独记，避免把彩排当战果）
    would_submit: list[Candidate] = field(default_factory=list)
    #: 实际用掉的重试轮数（1 = 只跑了一轮）
    rounds_used: int = 1
    started_at: dt.datetime = field(default_factory=dt.datetime.now)
    finished_at: dt.datetime | None = None
    aborted_reason: str | None = None

    @property
    def succeeded(self) -> list[Attempt]:
        return [a for a in self.attempts if a.ok]

    @property
    def failed(self) -> list[Attempt]:
        return [a for a in self.attempts if not a.ok]

    def summary_lines(self) -> list[str]:
        """给终端与通知用的摘要（**如实**：失败与未覆盖都要列出来）。"""
        lines: list[str] = []
        if self.aborted_reason:
            lines.append(f"已中止：{self.aborted_reason}")
        if self.dry_run:
            # 演练必须说"本应提交"，不能说"成功 0 个" —— 后者会被读成"全失败了"
            lines.append(f"演练模式（**未发任何写请求**）：本应提交 {len(self.would_submit)} 个，"
                         f"候选共 {len(self.planned)} 个")
            for item in self.would_submit:
                lines.append(f"  → {item.date} {item.period} [{item.project_name[:18]}] slot={item.slot_id}")
        else:
            lines.append(f"已实际提交：计划 {len(self.planned)} 个，成功 {len(self.succeeded)} 个，"
                         f"失败 {len(self.failed)} 个")
            for attempt in self.succeeded:
                lines.append(f"  ✓ {attempt.candidate.date} {attempt.candidate.period} "
                             f"[{attempt.candidate.project_name[:18]}] slot={attempt.candidate.slot_id}")
            for attempt in self.failed:
                lines.append(f"  ✗ slot={attempt.candidate.slot_id} {attempt.outcome}：{attempt.message[:40]}")
        if self.uncovered:
            lines.append(f"未覆盖的空闲时段 {len(self.uncovered)} 个：")
            for item in self.uncovered:
                lines.append(f"  - {item.free.describe()}：{item.reason}")
        else:
            lines.append("未覆盖的空闲时段：无（全覆盖）")
        for note in self.notes:
            lines.append(f"注：{note}")
        return lines


class Runner:
    """按配置执行"自动填空闲时段"的抢课。"""

    def __init__(self, plan_cfg: GrabPlan, *, client: PhyExpClient | None = None,
                 log: LogFn = _log, on_progress: LogFn | None = None) -> None:
        self.cfg = plan_cfg
        self.log = log
        self.on_progress = on_progress or log
        self._client = client
        self._owns_client = client is None

    # ── 客户端 ──

    def _client_or_create(self) -> PhyExpClient:
        if self._client is None:
            self._client = PhyExpClient(timeout=15.0)
        return self._client

    def close(self) -> None:
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

    # ── token 检查 ──

    def token_seconds_left(self, client: PhyExpClient) -> float | None:
        """token 剩余有效秒数（解析不了返回 None = **未知**，不假装"还有很久"）。"""
        exp = (client.claims or {}).get("exp")
        if not isinstance(exp, (int, float)):
            return None
        return float(exp) - dt.datetime.now().timestamp()

    # ── 主流程 ──

    def run(self, *, limit_override: int | None = None) -> RunReport:
        cfg = self.cfg
        cfg.validate()
        client = self._client_or_create()
        report = RunReport(dry_run=cfg.dry_run)

        # 1) 构建计划（只读）
        plan, semester_id, course_id = planner.fetch_plan(client, cfg)
        report.planned = list(plan.candidates)
        self.log(f"[计划] 候选 {len(plan.candidates)} 个；已占时段 {len(plan.occupied)} 个；"
                 f"已选实验 {len(plan.taken_projects)} 个")
        for index, item in enumerate(plan.candidates, 1):
            self.log(f"  {index:>2}. {item.describe()}")

        # 2) token 检查（过期或快过期先提醒，不静默往下走）
        left = self.token_seconds_left(client)
        if left is not None:
            if left <= 0:
                self._relogin_or_abort(client, report, "token 已过期")
                return self._finish(report)
            if left < TOKEN_WARN_SECONDS:
                note = f"token 仅剩约 {int(left)} 秒，可能中途失效"
                report.notes.append(note)
                self.log(f"[注意] {note}")
                if cfg.notify:
                    notify_mod.notify("抢课提醒：登录态快过期",
                                      f"token 仅剩约 {int(left)} 秒；若本轮失败请重新 login。", log=self.log)

        max_total = limit_override if limit_override is not None else cfg.max_total
        if max_total and max_total > 0:
            note = f"本轮上限 {max_total} 个"
            report.notes.append(note)
            self.log(f"[设置] {note}")

        # 3) 按候选顺序提交（失败降级到下一个；每个空闲时段最多占 1 个）
        occupied = dict(plan.occupied)
        taken_projects = dict(plan.taken_projects)
        taken_slots: list[str] = []
        done = 0

        def secured() -> set[tuple[str, str]]:
            """"真正拿到手"的空闲时段。

            ⚠️ **口径要点（实测踩到的假成功）**：不能拿"有候选"当"已覆盖" ——
            候选只说明"当时有机会"，提交可能失败（已满/限流）。只有
            **真实回读确认过**（`occupied`）或**演练中假设成功**（`would_submit`）才算拿到。
            早期版本用候选算覆盖，导致：① 全部失败也打印"全部覆盖、收工"；
            ② 重试轮永远只跑一轮。两处都是"看起来正常"的静默错误。
            """
            keys = set(occupied)
            if cfg.dry_run:
                keys.update(item.free_key for item in report.would_submit)
            return keys

        def update_uncovered() -> int:
            """重算"还没拿到的空闲时段"并写明原因，返回还差几个（A5：必须如实）。"""
            got = secured()
            rows_now, names_now = planner.load_course_slots(client, course_id)
            fresh = build_plan(cfg, rows=rows_now, course_id=course_id, occupied=occupied,
                               taken_projects=taken_projects, project_names=names_now,
                               occupied_as_covered=False)
            reason_of = {item.free.key: item.reason for item in fresh.uncovered}
            pending: list[Uncovered] = []
            for free in cfg.free_slots:
                if free.key in got:
                    continue
                reason = reason_of.get(free.key) or "该时段当时有候选，但提交未成功（见上方失败明细）"
                pending.append(Uncovered(free=free, reason=reason))
            report.uncovered = pending
            return len(pending)

        def run_round(round_index: int) -> bool:
            """跑一轮（重新规划 → 逐个提交/演练）。返回 True = 该收工了。"""
            nonlocal done
            if round_index > 0:
                rows_now, names_now = planner.load_course_slots(client, course_id)
                plan_now = build_plan(cfg, rows=rows_now, course_id=course_id,
                                      occupied=occupied, taken_projects=taken_projects,
                                      project_names=names_now)
                candidates = [c for c in plan_now.candidates if c.free_key not in secured()]
                self.log(f"[第 {round_index + 1}/{rounds} 轮] 重试剩余未覆盖时段，"
                         f"本轮候选 {len(candidates)} 个")
            else:
                candidates = [c for c in plan.candidates if c.free_key not in secured()]

            for index, candidate in enumerate(candidates, 1):
                if candidate.free_key in secured():
                    report.skipped.append(f"{candidate.describe()}（该时段已被占）")
                    continue
                if max_total and max_total > 0 and done >= max_total:
                    report.notes.append(f"达到上限 {max_total}，停止")
                    return True

                prefix = f"[{index}/{len(candidates)}]" if round_index else f"[{index}/{len(plan.candidates)}]"
                if cfg.dry_run:
                    self.log(f"{prefix} [演练] 本应提交 {candidate.describe()}")
                    report.would_submit.append(candidate)
                    done += 1
                    taken_slots.append(candidate.slot_id)
                    continue

                self.log(f"{prefix} 提交 {candidate.describe()}")
                result = client.submit_booking(candidate.slot_id, course_id)
                attempt = Attempt(candidate=candidate, ok=result.ok, outcome=result.outcome.value,
                                  message=result.message, http_status=result.http_status,
                                  elapsed_ms=result.elapsed_ms)
                if not result.ok:
                    self.log(f"       └ 失败：{result.message[:100]}（继续下一个候选）")
                    report.attempts.append(attempt)
                    continue

                # 回读核实（R6）：不拿 HTTP 200 当成功
                verified, detail, record_id = self._verify(client, semester_id, course_id, candidate)
                attempt.verified = verified
                attempt.record_id = record_id
                if verified:
                    self.log(f"       └ 服务端已确认：{detail}")
                    occupied[candidate.free_key] = f"本次提交 slot={candidate.slot_id}"
                    taken_projects[candidate.project_id] = candidate.project_name
                    taken_slots.append(candidate.slot_id)
                    done += 1
                else:
                    attempt.ok = False
                    attempt.outcome = "unverified"
                    attempt.message = f"HTTP 200 但回读未确认（{detail}）"
                    self.log(f"       └ ⚠️ 回读未确认：{detail} —— 按**未成功**记录")
                report.attempts.append(attempt)

                # 每条之间保持最小间隔（合规：低频，沿用实测校准值）
                if index < len(candidates):
                    time.sleep(max(0.0, self._submit_interval_seconds()))

            remaining = update_uncovered()
            if remaining == 0:
                self.log(f"[满足] 所有空闲时段都已覆盖，收工。")
                return True
            if max_total and max_total > 0 and done >= max_total:
                return True
            return False

        rounds = max(1, int(cfg.retry_rounds))
        if rounds > 1 and not cfg.dry_run:
            self.log(f"[设置] 到点后最多重试 {rounds} 轮，每轮间隔 {cfg.retry_interval_seconds:.0f} 秒"
                     f"（用户确认：重试固定次数后停）")
        for round_index in range(rounds):
            if round_index > 0:
                if cfg.dry_run:
                    break     # 演练只跑一轮：候选与真实一致即可，没必要重复等待
                wait = max(0.0, float(cfg.retry_interval_seconds))
                self.log(f"[等待] {wait:.0f} 秒后开始第 {round_index + 1} 轮重试……")
                try:
                    time.sleep(wait)
                except KeyboardInterrupt:
                    self.log("[中断] 用户取消重试。")
                    break
            if run_round(round_index):
                break

        if taken_slots:
            report.notes.append(f"本次提交的场次：{', '.join(taken_slots)}")
        report.rounds_used = round_index + 1
        report.finished_at = dt.datetime.now()
        return self._finish(report)

    # ── 辅助 ──

    def _submit_interval_seconds(self) -> float:
        submit = self.cfg.submit or {}
        try:
            return float(submit.get("min_submit_interval_ms", 800)) / 1000.0
        except (TypeError, ValueError):
            return 0.8

    def _verify(self, client: PhyExpClient, semester_id: Any, course_id: Any,
                candidate: Candidate) -> tuple[bool, str, Any | None]:
        """回读 `rest/user2projects` 确认该场次已 `elected`（R6 的判据）。

        返回 `(是否确认, 说明, 选课记录 id)` —— 记录 id 正好是**退课**要用的那个 id，
        所以"顺手带回来"，避免用户想退课时再查一次（少一次请求、少一处口径）。
        """
        try:
            mine = client.my_electives(semester_id, course_id)
        except ApiError as exc:
            return False, f"回读失败：{exc}", None
        for record in mine:
            if str(record.get("schedule_id")) == candidate.slot_id:
                return True, (f"user2project_id={record.get('id')} "
                              f"status={record.get('schedule_status')}"), record.get("id")
        return False, "我的选课记录里没有该场次", None

    def _relogin_or_abort(self, client: PhyExpClient, report: RunReport, reason: str) -> None:
        """token 失效时：能免登录恢复就恢复，否则**中止并通知**（绝不静默继续）。"""
        from . import session as session_mod

        self.log(f"[中止] {reason}。")
        recovered = False
        try:
            if session_mod.has_state():
                self.log("[尝试] 用已保存的会话免登录恢复 token……")
                if session_mod.interactive_login(record_har=False, max_wait_seconds=180,
                                                use_saved_state=True) == session_mod.state_path():
                    self.log("[恢复] 已用保存的会话刷新 token。")
                    recovered = True
        except Exception as exc:  # noqa: BLE001 - 恢复失败按"未恢复"处理
            self.log(f"[恢复失败] {type(exc).__name__}：{exc}")

        if recovered:
            report.notes.append("token 已过期，但用保存的会话自动恢复了登录态；请重跑本轮。")
            report.aborted_reason = f"{reason}（已自动恢复登录态，请重跑）"
        else:
            report.notes.append("token 已过期且无法自动恢复：请先运行 python run.py login 再重跑。")
            report.aborted_reason = f"{reason}，请先 python run.py login"
        report.finished_at = dt.datetime.now()
        self._notify(report, title_prefix="抢课中止")

    def _finish(self, report: RunReport) -> RunReport:
        if report.finished_at is None:
            report.finished_at = dt.datetime.now()
        self._notify(report)
        return report

    def cancel_pick(self, attempt: Attempt, *, course_id: Any | None = None) -> tuple[bool, str]:
        """退掉刚抢到的一条（**写操作**）：先查 `user2projects.id`，再 `/cancel`，最后回读核实。"""
        from . import planner

        client = self._client_or_create()
        record_id = attempt.record_id
        try:
            if record_id is None:
                semesters = client.open_semesters()
                if not semesters:
                    return False, "没有开放学期，无法退课"
                semester_id = semesters[0].get("id")
                cid = course_id if course_id is not None else (self.cfg.course_id or None)
                if cid is None:
                    courses = client.my_courses(semester_id)
                    cid = courses[0].get("id") if courses else None
                if cid is None:
                    return False, "拿不到课程 id，无法定位选课记录"
                _rows, _names = planner.load_course_slots(client, cid)
                for record in client.my_electives(semester_id, cid):
                    if str(record.get("schedule_id")) == attempt.candidate.slot_id:
                        record_id = record.get("id")
                        break
            if record_id is None:
                return False, "在服务端找不到这条选课记录（可能已过期/已被退回）"

            result = client.cancel_booking(record_id)
            if not result.ok:
                return False, f"退课接口返回失败：{result.message[:80]}"

            # 回读核实：该场次必须从"我的选课记录"里消失
            semesters = client.open_semesters()
            if not semesters:
                return True, "退课已提交，但无开放学期可复核"
            semester_id = semesters[0].get("id")
            cid = course_id if course_id is not None else (self.cfg.course_id or None)
            if cid is None:
                courses = client.my_courses(semester_id)
                cid = courses[0].get("id") if courses else None
            for record in client.my_electives(semester_id, cid):
                if str(record.get("schedule_id")) == attempt.candidate.slot_id:
                    return False, "退课已提交，但回读仍能看到该场次（按未退成功记录）"
            return True, "服务端已确认该场次不在我的选课记录里"
        except ApiError as exc:
            return False, f"退课失败：{exc}"

    def _notify(self, report: RunReport, *, title_prefix: str = "抢课") -> None:
        """发桌面通知（A9：通知失败不影响结果）。"""
        if not self.cfg.notify:
            self.log("[通知] 已在配置里关闭（notify=false）")
            return
        state = "演练" if report.dry_run else ("成功" if report.succeeded else "未成功")
        if report.dry_run:
            title = f"{title_prefix}{state}：本应提交 {len(report.would_submit)} 个"
        else:
            title = f"{title_prefix}{state}：成功 {len(report.succeeded)} 个"
        if report.uncovered:
            title += f"，未覆盖 {len(report.uncovered)} 个时段"
        # 通知正文只放"结论级"信息：数量 + 成功项 + 未覆盖原因（细节看日志）
        lines: list[str] = []
        if report.dry_run:
            lines.append(f"候选 {len(report.planned)} 个，本应提交 {len(report.would_submit)} 个（未发写请求）")
            for item in report.would_submit[:3]:
                lines.append(f"→ {item.date} {item.period} {item.project_name[:16]}")
        else:
            lines.append(f"计划 {len(report.planned)} 个，成功 {len(report.succeeded)} 个，"
                         f"失败 {len(report.failed)} 个")
            for attempt in report.succeeded[:3]:
                lines.append(f"✓ {attempt.candidate.date} {attempt.candidate.period} "
                             f"{attempt.candidate.project_name[:16]}")
            for attempt in report.failed[:2]:
                lines.append(f"✗ {attempt.candidate.date} {attempt.candidate.period}：{attempt.message[:24]}")
        if report.uncovered:
            lines.append(f"未覆盖 {len(report.uncovered)} 个时段：{report.uncovered[0].reason[:28]}")
        if report.aborted_reason:
            lines.append(f"中止：{report.aborted_reason[:40]}")
        notify_mod.notify(title, notify_mod.summarize(lines), log=self.log)
