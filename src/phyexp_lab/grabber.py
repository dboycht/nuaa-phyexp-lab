"""抢课（提交）引擎骨架：对时 → 预热 → 精确定时 → 预发射 → 限速退避。

**当前状态：定时与演练部分已实现；真实提交仍显式不可用。**

为什么真实提交还不能用：写接口 `POST report-api/electives` 的**请求体与响应判据尚未实测**
（本季选课窗口已关闭，见 `docs/接口逆向.md` §3.4/§六）。因此：

- `run_until(..., dry_run=True)`（**默认**）只做定时与演练：走到点、打印"本应在这一毫秒发出"、
  记录实际发射偏差，但**不发任何写请求**；
- `run_until(..., dry_run=False)` 会走到真实提交那一步并抛 `NotImplementedError` ——
  **宁可明确报错，也不猜参数**。

已按本项目实测校准的要点
------------------------
1. **预热连接**：首次请求 1250ms（TLS 握手），稳态中位 11ms ⇒ 提交前必须预热。
2. **对时优先**：本地钟比服务端**慢约 1–2 秒**（实测）⇒ 打点必须换算到**服务端时刻**，不能直接用本地钟。
3. **发射偏差要可观测**：每次发射都记录"计划时刻 vs 实际时刻"的毫秒差，
   否则预发射偏移永远调不准（这也是抢课脚本最容易"自我感觉良好"的地方）。
4. **限速退避**：默认两次提交最小间隔 800ms（沿用姊妹项目教务系统的实测下界），命中限速指数退避。
"""

from __future__ import annotations

import datetime as dt
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .api import PhyExpClient
from .models import BookingAttempt, Outcome
from .probe import ClockOffset, measure_clock_offset

LogFn = Callable[[str], None]
#: 真实提交函数的签名：吃一个场次 id，返回一次尝试记录。
#: **写接口实测完成后，只需要实现这样一个函数并注入**，引擎本身不用改。
SubmitFn = Callable[[str], BookingAttempt]


def _log(msg: str) -> None:
    print(msg, flush=True)


@dataclass
class GrabConfig:
    """提交策略参数。**默认值按本系统实测校准，不是照抄别的项目**。"""

    #: 是否在提交前先发一次预热请求建立 TLS/keep-alive 连接。
    #: ⚠️ 实测：不预热的话首次请求要付 ~1250ms（TLS 握手），抢课等于开局先落后 1.2 秒。
    prewarm: bool = True

    #: 提前多少毫秒发送请求。默认 50ms —— 依据：本系统稳态 RTT 中位 **11ms**（见 docs/接口逆向.md §3.6）。
    #: ⚠️ 不要沿用教务系统那套 100–300ms：那是另一套系统的实测值，会提前过多。
    pre_fire_offset_ms: int = 50
    #: 两次提交之间的最小间隔（毫秒）。**不要低于 800**：姊妹项目实测会被限速
    min_submit_interval_ms: int = 800
    #: 单个目标的最大尝试次数
    max_attempts_per_target: int = 20
    #: 命中限速后的退避（秒）
    rate_limit_backoff_seconds: float = 3.0
    #: 对时采样次数（每次一个 `rest/time` 往返）
    clock_samples: int = 7


@dataclass
class TimingPlan:
    """一次抢课的**发射计划**（全部换算清楚，便于人工核对）。"""

    target_server: dt.datetime
    offset_seconds: float
    pre_fire_offset_ms: int
    fire_local: dt.datetime
    now_local: dt.datetime
    lead_seconds: float
    clock: ClockOffset | None = None
    slot_ids: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            f"目标时刻（服务端）  : {self.target_server.isoformat(timespec='milliseconds')}",
            f"时钟偏移            : {self.offset_seconds:+.3f}s"
            f"（{'本地慢' if self.offset_seconds >= 0 else '本地快'} {abs(self.offset_seconds):.3f}s）",
            f"本地发射时刻        : {self.fire_local.isoformat(timespec='milliseconds')}"
            f"（提前 {self.pre_fire_offset_ms}ms + 偏移补偿）",
            f"当前本地时刻        : {self.now_local.isoformat(timespec='milliseconds')}",
            f"距发射              : {self.lead_seconds:+.2f}s",
            f"目标场次            : {', '.join(self.slot_ids) if self.slot_ids else '(未指定)'}",
        ]
        return "\n".join(lines)


class GrabError(RuntimeError):
    """抢课流程的可预期失败。"""


class Grabber:
    """按服务端时刻精确定时，对一组场次执行"预发射 + 间隔重试"的提交。"""

    def __init__(self, client: PhyExpClient, config: GrabConfig | None = None,
                 log: LogFn = _log, submit_func: SubmitFn | None = None) -> None:
        self.client = client
        self.config = config or GrabConfig()
        self.log = log
        #: 真实提交流程的注入点（写接口实测后从这里接进去，引擎无需改动）
        self.submit_func = submit_func
        self.clock: ClockOffset | None = None
        self.attempts: list[BookingAttempt] = []
        self.log_path: Path | None = None

    # ── 准备：对时 + 预热 ──

    def prepare(self, measure_clock: bool = True) -> None:
        """预热连接并（可选）测量时钟偏移。**抢课之前必须调用**。"""
        if self.config.prewarm:
            warm_ms = self.client.prewarm()
            self.log(f"[准备] 连接已预热（{warm_ms}ms）")
        if measure_clock:
            self.clock = measure_clock_offset(samples=self.config.clock_samples)
            self.log(f"[准备] 对时完成：{self.clock.summary}")

    @property
    def offset_seconds(self) -> float:
        return self.clock.offset_seconds if self.clock else 0.0

    # ── 计划 ──

    def plan(self, slot_ids: list[str], target_server_epoch: float) -> TimingPlan:
        """把"服务端的目标时刻"换算成本地的发射时刻。"""
        offset = self.offset_seconds
        now_local = dt.datetime.now().astimezone()
        # 本地钟 = 服务端钟 - offset ⇒ 服务端 T 对应本地 T - offset
        fire_local_epoch = target_server_epoch - offset - self.config.pre_fire_offset_ms / 1000.0
        return TimingPlan(
            target_server=dt.datetime.fromtimestamp(target_server_epoch).astimezone(),
            offset_seconds=offset,
            pre_fire_offset_ms=self.config.pre_fire_offset_ms,
            fire_local=dt.datetime.fromtimestamp(fire_local_epoch).astimezone(),
            now_local=now_local,
            lead_seconds=fire_local_epoch - now_local.timestamp(),
            clock=self.clock,
            slot_ids=list(slot_ids),
        )

    # ── 提交 ──

    def submit_once(self, slot_id: str, *, dry_run: bool = True) -> BookingAttempt:
        """向单个时段提交一次预约，并把结果分类。

        `dry_run=True` 时**不发任何写请求**，只记录一次演练（`Outcome.DRY_RUN`）；
        `dry_run=False` 时需要外部注入 `submit_func`（写接口实测后实现），否则抛 `NotImplementedError`。
        """
        started = time.time()
        if dry_run:
            self.log(f"[演练] 本应提交场次 {slot_id}（未发送任何写请求）")
            return BookingAttempt(slot_id=slot_id, started_at=started,
                                  message="dry-run：未发送请求", outcome=Outcome.DRY_RUN)

        if self.submit_func is None:
            raise NotImplementedError(
                "真实提交尚未接入：需在**选课窗口开放时**抓一次真实提交"
                "（POST report-api/electives）确认请求体与响应判据，"
                "然后实现 `api.PhyExpClient.submit_booking` 并通过 Grabber(submit_func=...) 注入。"
                "见 docs/接口逆向.md §3.4 与 docs/选课窗口操作手册.md。"
            )
        return self.submit_func(slot_id)

    # ── 发射日志（当天事后复盘的唯一依据）──

    def _open_log(self) -> None:
        from . import config

        logs = config.logs_dir()
        logs.mkdir(parents=True, exist_ok=True)
        self.log_path = logs / f"grab-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl"

    def _record(self, attempt: BookingAttempt) -> None:
        if self.log_path is None:
            return
        payload = {
            "slot_id": attempt.slot_id,
            "outcome": attempt.outcome.value,
            "message": attempt.message[:500],
            "http_status": attempt.http_status,
            "elapsed_ms": attempt.elapsed_ms,
            "deviation_ms": attempt.deviation_ms,
            "started_at": dt.datetime.fromtimestamp(attempt.started_at).astimezone().isoformat(timespec="milliseconds"),
        }
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            handle.flush()

    # ── 主流程 ──

    def run_until(self, slot_ids: list[str], target_server_epoch: float, *,
                  dry_run: bool = True,
                  max_attempts: int | None = None,
                  plan_only: bool = False) -> list[BookingAttempt]:
        """等到目标时刻（服务端时钟）后按策略提交，返回全部尝试记录。

        ⚠️ **默认 dry_run=True**：这是刻意的 —— 写接口没实测之前，
        真实提交只能是显式的 `dry_run=False`（并且会立刻报错），不能"不小心就发出去了"。
        `plan_only=True` 只打印计划就返回（窗口当天先核对计划用）。
        """
        if not slot_ids:
            raise GrabError("没有指定目标场次。")
        if not dry_run and self.submit_func is None:
            self.submit_once(slot_ids[0], dry_run=False)  # 立刻抛出明确的未接入错误

        plan = self.plan(slot_ids, target_server_epoch)
        self.log("[计划]")
        for line in plan.render().splitlines():
            self.log(f"    {line}")

        if plan_only:
            self.log("[plan-only] 只打印计划，未做任何等待或提交。")
            return []

        if plan.lead_seconds <= 0:
            self.log("[警告] 目标时刻已过或不足以准备，仍按当前时刻立即执行（演练）。")

        self._open_log()
        if self.log_path:
            self.log(f"[日志] 每次发射都会写入 → {self.log_path}")

        # 粗等到发射时刻前 0.2 秒，再用短睡精调（避免长 sleep 的系统调度误差）
        while True:
            remain = plan.fire_local.timestamp() - time.time()
            if remain <= 0.2:
                break
            time.sleep(min(remain - 0.1, 5.0))
        while True:
            remain = plan.fire_local.timestamp() - time.time()
            if remain <= 0:
                break
            time.sleep(min(remain, 0.05))

        limit = max_attempts or self.config.max_attempts_per_target
        for index in range(limit):
            for slot_id in slot_ids:
                fired_at = time.time()
                deviation_ms = (fired_at - plan.fire_local.timestamp()) * 1000.0
                self.log(f"[发射 {index + 1}/{limit}] 场次 {slot_id} "
                         f"计划偏差 {deviation_ms:+.1f}ms（正=晚于计划）")
                attempt = self.submit_once(slot_id, dry_run=dry_run)
                attempt.attempted_at = fired_at
                attempt.deviation_ms = deviation_ms
                self.attempts.append(attempt)
                self._record(attempt)

                if attempt.outcome in (Outcome.SUCCESS, Outcome.FULL, Outcome.REJECTED):
                    self.log(f"[结束] 场次 {slot_id} 结果为 {attempt.outcome.value}，停止重试。")
                    return self.attempts

            if index < limit - 1:
                time.sleep(self.config.min_submit_interval_ms / 1000.0)

        self.log(f"[结束] 达到最大尝试次数（{limit}），最后结果："
                 f"{self.attempts[-1].outcome.value if self.attempts else '无'}")
        return self.attempts
