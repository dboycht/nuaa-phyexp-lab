"""连通性自检：脚本（requests）能不能直连目标 API？

为什么需要它
------------
抢课引擎的**形态完全取决于这个答案**：

- **能直连** ⇒ 用 `requests` 跑提交循环，单次往返毫秒级，预发射/重试都好做；
- **不能直连**（被前置防护挡、或缺浏览器上下文才有的东西）⇒ 只能在浏览器上下文里发请求
  （Playwright `page.evaluate(fetch)` / `context.request`），速度受浏览器限制，策略要重新设计。

所以本模块用**同一个最轻的只读接口**（`rest/time`）跑一组对照，把"缺哪一样就不通"定位出来，
而不是笼统地说"能/不能"。判据必须来自实测状态码，不来自推测。

安全：只发 **GET**（只读），不带任何写操作；不打印 token / Cookie 的值。
"""

from __future__ import annotations

from dataclasses import dataclass

from . import config, session


@dataclass
class ProbeCase:
    """一组对照条件。"""

    name: str
    with_ua: bool = True
    with_referer: bool = False
    with_token: bool = False
    with_cookies: bool = False


@dataclass
class ProbeResult:
    case: ProbeCase
    status: int | None = None
    elapsed_ms: int | None = None
    body_head: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300


DEFAULT_CASES = [
    ProbeCase("① 裸请求（无任何头）", with_ua=False),
    ProbeCase("② 带浏览器 UA", with_ua=True),
    ProbeCase("③ UA + Referer", with_ua=True, with_referer=True),
    ProbeCase("④ UA + Bearer token", with_ua=True, with_token=True),
    ProbeCase("⑤ UA + Referer + token + Cookie", with_ua=True, with_referer=True, with_token=True, with_cookies=True),
]


def probe_endpoint(path: str = "rest/time", timeout: float = 10.0,
                   cases: list[ProbeCase] | None = None) -> list[ProbeResult]:
    """对 `path`（相对 API 基址）逐个对照条件发只读 GET，返回全部结果。"""
    import requests  # 延迟导入

    url = f"{config.API_BASE}/{path.lstrip('/')}"
    token = session.load_token()
    try:
        cookies = session.load_cookies()
    except session.SessionError:
        cookies = {}

    results: list[ProbeResult] = []
    for case in (cases or DEFAULT_CASES):
        headers = {"Accept": "application/json, text/plain, */*"}
        if case.with_ua:
            headers["User-Agent"] = config.USER_AGENT
        if case.with_referer:
            headers["Referer"] = config.BOOKING_ENTRY
        if case.with_token:
            if not token:
                results.append(ProbeResult(case, error="缺少已保存的 token（请先 python run.py login）"))
                continue
            headers["Authorization"] = token

        jar = cookies if case.with_cookies else None
        try:
            resp = requests.get(url, headers=headers, cookies=jar, timeout=timeout, allow_redirects=False)
        except Exception as exc:  # noqa: BLE001 - 网络异常要如实报告
            results.append(ProbeResult(case, error=f"{type(exc).__name__}: {exc}"))
            continue

        head = ""
        try:
            head = (resp.text or "")[:120].replace("\n", " ")
        except Exception:
            pass
        results.append(
            ProbeResult(case, status=resp.status_code,
                        elapsed_ms=int(resp.elapsed.total_seconds() * 1000), body_head=head)
        )
    return results


def verdict(results: list[ProbeResult]) -> str:
    """把对照结果翻译成一句可执行的结论。"""
    token_case = next((r for r in results if r.case.with_token), None)
    if token_case is None:
        return "未测到带 token 的用例。"
    if token_case.error:
        return f"带 token 的用例没跑成：{token_case.error}"
    if token_case.ok:
        return ("✅ 脚本直连可行：带 token 的 requests 请求被正常受理 ⇒ 抢课引擎可以用 requests 跑高速循环"
                "（仍需遵守低频与限速退避）。")
    return (f"❌ 脚本直连不可行（带 token 仍返回 {token_case.status}）⇒ 请求必须放在浏览器上下文里发"
            "（Playwright page.evaluate / context.request）；抢课策略需按浏览器往返延迟重新设计。")


def measure_latency(path: str = "rest/time", repeat: int = 5, timeout: float = 10.0) -> list[int]:
    """在**同一条 keep-alive 会话**上连发 `repeat` 次只读 GET，返回各次耗时（毫秒）。

    为什么要单独测：首次请求含 TLS 握手，耗时明显偏高；抢课的预发射偏移必须按**稳态 RTT** 设计，
    拿首次耗时当依据会把提前量估大一个数量级。
    """
    import requests

    url = f"{config.API_BASE}/{path.lstrip('/')}"
    token = session.load_token()
    if not token:
        raise session.SessionError("缺少已保存的 token：请先运行 `python run.py login`。")

    headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": config.USER_AGENT,
        "Authorization": token,
        "Referer": config.BOOKING_ENTRY,
    }
    elapsed: list[int] = []
    with requests.Session() as http:
        http.headers.update(headers)
        for _ in range(max(1, repeat)):
            resp = http.get(url, timeout=timeout)
            elapsed.append(int(resp.elapsed.total_seconds() * 1000))
            if not (200 <= resp.status_code < 300):
                break
    return elapsed


@dataclass
class ClockOffset:
    """本地钟相对服务端的偏移测量结果（N 次往返取中位数）。"""

    #: 服务端时刻 - 本地时刻，单位秒。**正值 = 本地慢（需要往前补）**
    offset_seconds: float
    rtt_ms_min: int
    rtt_ms_median: int
    samples: int
    spread_seconds: float
    measured_at: str

    @property
    def summary(self) -> str:
        direction = "本地慢" if self.offset_seconds >= 0 else "本地快"
        return (f"服务端 − 本地 = {self.offset_seconds:+.3f}s（{direction} {abs(self.offset_seconds):.3f}s）；"
                f"RTT 最小 {self.rtt_ms_min}ms / 中位 {self.rtt_ms_median}ms；"
                f"{self.samples} 次采样，离散度 {self.spread_seconds * 1000:.0f}ms")


def measure_clock_offset(samples: int = 7, timeout: float = 10.0) -> ClockOffset:
    """用 `rest/time` 做 N 次往返，估算「服务端 − 本地」的时钟偏移。

    原理：第 i 次往返里，服务端时间戳对应本地时刻约 `t0 + rtt/2`，
    故 `offset_i = server_time_i - (t0_i + rtt_i/2)`；取**中位数**抗抖动。
    """
    import datetime as dt
    import statistics
    import time

    import requests

    token = session.load_token()
    if not token:
        raise session.SessionError("缺少已保存的 token：请先运行 `python run.py login`。")

    url = f"{config.API_BASE}/rest/time"
    headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": config.USER_AGENT,
        "Authorization": token,
        "Referer": config.BOOKING_ENTRY,
    }
    offsets: list[float] = []
    rtts: list[int] = []
    with requests.Session() as http:
        http.headers.update(headers)
        # 先预热：第一次请求含 TLS 握手，其 RTT 不代表稳态
        http.get(url, timeout=timeout)
        for _ in range(max(1, samples)):
            t0 = time.time()
            resp = http.get(url, timeout=timeout)
            t1 = time.time()
            if resp.status_code != 200:
                continue
            payload = resp.json()
            stamp = payload[0].get("time") if isinstance(payload, list) and payload else None
            if not stamp:
                continue
            server_epoch = dt.datetime.fromisoformat(stamp).timestamp()
            rtt = t1 - t0
            rtts.append(int(rtt * 1000))
            offsets.append(server_epoch - (t0 + rtt / 2.0))

    if not offsets:
        raise session.SessionError("对时失败：没有拿到可用的服务端时间戳。")

    return ClockOffset(
        offset_seconds=statistics.median(offsets),
        rtt_ms_min=min(rtts),
        rtt_ms_median=int(statistics.median(rtts)),
        samples=len(offsets),
        spread_seconds=(max(offsets) - min(offsets)),
        measured_at=dt.datetime.now().astimezone().isoformat(timespec="seconds"),
    )
