"""预约系统接口封装（只读 + **写操作**）。

实现依据
--------
只读部分来自**实测**（`docs/接口逆向.md` §3.1/§3.6）：

- 脚本直连可行：带 `Authorization: Bearer <jwt>` 的 `requests` GET 返回 200；
- 稳态 RTT 中位 11ms（首次含 TLS 1250ms）⇒ 客户端**复用一个 keep-alive 会话**；
- 查询参数由 token 自带的 `user_id` / `org_id` 拼出（实测 `class_list.cs.{<org_id>}`）：
  ```
  GET {API}/rest/schedules?select=*,periods(*),locations(*),teacher:users!schedule_teacher_id_fkey(*),
        user2projects!user2project_schedule_id_fkey(*)
      &is_publish=eq.true
      &or=(course_id.is.null,course_id.eq.<course_id>)
      &or=(is_reserved.eq.false,class_list.cs.{<org_id>},class_list.cs.{null})
      &date=gte.<起>&date=lte.<止>
      &user2projects.user_id=eq.<user_id>&user2projects.schedule_status=in.(elected,scheduled)
  ```

写操作依据（**2026-10-07 从线上前端 bundle 读出的调用点**，见 `docs/接口逆向.md` §3.4）：

- 选课：`POST report-api/electives`，**表单**（`qs.stringify`）`lesson_id=<schedules.id>` + `course_id=<课程 id>`；
- 退课：`POST report-api/electives/<user2projects.id>/cancel`；
- 选实验项目：`POST report-api/electives/project`，表单 `course_id` + `project_id`；
- 前端错误语义：**401** = token 失效（前端清 localStorage 跳登录）；**400** = 取 `data.message` 展示给用户；
- 前端 axios `timeout = 5000`、`withCredentials = true` ⇒ 本项目同样带 Cookie、并按同一量级设超时。

⚠️ 身份与隐私：`user_id` / `org_id` / 姓名 / 学号 / openid 都来自**使用者本人的 token 载荷**，
只在**内存与本地运行时目录**中使用（`%LOCALAPPDATA%\\PhyExpLab`）；**绝不写入本仓库任何文件**。
"""

from __future__ import annotations

import base64
import datetime as dt
import json
from dataclasses import dataclass
from typing import Any

from . import config, session
from .models import Experiment, Outcome, Slot


class ApiError(RuntimeError):
    """接口层可预期失败（消息面向使用者，可直接打印）。"""


#: 写操作里**服务端明确回话**的失败特征（命中即分类，不靠猜）。
RATE_LIMIT_HINTS = ("频繁", "太快", "稍后", "限流", "rate limit", "too many")
FULL_HINTS = ("已满", "人数已满", "满员", "名额已满", "no more", "full")


def parse_write_body(body_text: str) -> dict[str, Any]:
    """解析写接口响应体（成功/失败都是 JSON）；解析不了就返回 `{}`（如实表示"没解析出结构化信息"）。"""
    text = (body_text or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def friendly_message(body_text: str) -> str:
    """给使用者看的提示语：能从 JSON 里取到 `message` 就只显示它，否则退回原文。

    为什么：服务端**选课成功**时回的是 `{"status":false,"code":200,"message":"ok"}` ——
    直接把整段 JSON 打印给人看既噪音大，又**容易被误读成失败**（`status:false`！）。
    """
    data = parse_write_body(body_text)
    message = data.get("message")
    if isinstance(message, str) and message.strip():
        code = data.get("code")
        return f"{message.strip()}（服务端 code={code}）" if code is not None else message.strip()
    return (body_text or "").strip()


@dataclass(frozen=True)
class WriteResult:
    """一次**写操作**的结果（选课/退课）。

    设计要点：把「HTTP 是否成功」「服务端文案」「分类」三者**分开存**，
    这样复盘时能区分「真的成功了」和「HTTP 200 但服务端说没成」——
    这正是 `BookingAttempt` 里 `Outcome.UNKNOWN` 存在的理由。
    """

    action: str
    target: str
    http_status: int | None
    body_text: str
    ok: bool
    outcome: Outcome
    elapsed_ms: int | None = None
    error: str | None = None

    @property
    def message(self) -> str:
        """给使用者看的一句话（服务端 `message` 优先，其次本地错误，最后原始响应）。"""
        if self.error:
            return self.error
        if self.body_text:
            return friendly_message(self.body_text)
        return f"HTTP {self.http_status}（空响应体）"

    def describe(self) -> str:
        status = "-" if self.http_status is None else str(self.http_status)
        elapsed = "-" if self.elapsed_ms is None else f"{self.elapsed_ms}ms"
        return (f"{self.action} {self.target} → {'成功' if self.ok else '未成功'} "
                f"[{self.outcome.value}] HTTP {status} {elapsed}：{self.message[:200]}")


def classify_write(status: int | None, body_text: str) -> tuple[bool, Outcome]:
    """把一次写操作的**状态码 + 响应体**分类。

    ⚠️ **2026-10-07 实测（务必先读）**：真实选课成功的响应是
    `HTTP 200 {"status":false,"code":200,"message":"ok"}` ——
    **成功时 `status` 反而是 `false`**。因此：
    - **不要**用 `status` 字段判断成败；
    - 真正的判据是 **HTTP 状态码 + `message` 文案**；
    - 本函数因此只在 `message` 文案里找「满/失败/…”等特征词，绝不看 `status`。

    判据（如实、不美化）：
    - 无状态码（网络层失败）→ `UNKNOWN`；
    - 401 → `AUTH_EXPIRED`（前端也是这个语义：清登录态跳登录页）；
    - 429 → `RATE_LIMITED`；
    - 其余 4xx/5xx → 文案命中「满」→ `FULL`，命中「频繁」→ `RATE_LIMITED`，否则 `REJECTED`；
    - 2xx → 文案命中「满/失败/错误/不存在/无权」→ `REJECTED`（HTTP 装成功但服务端说没成），
      否则 `SUCCESS`。
    """
    if status is None:
        return False, Outcome.UNKNOWN
    if status == 401:
        return False, Outcome.AUTH_EXPIRED
    if status == 429:
        return False, Outcome.RATE_LIMITED
    # 只取 message 文案做特征匹配（避免把 JSON 键名本身当成文案）
    data = parse_write_body(body_text)
    message = data.get("message") if isinstance(data.get("message"), str) else ""
    text = message or (body_text or "")
    lowered = text.lower()
    if status >= 400:
        if any(hint in text for hint in FULL_HINTS) or "full" in lowered:
            return False, Outcome.FULL
        if any(hint in text for hint in RATE_LIMIT_HINTS) or "rate limit" in lowered:
            return False, Outcome.RATE_LIMITED
        return False, Outcome.REJECTED
    # 2xx
    if any(hint in text for hint in FULL_HINTS):
        return False, Outcome.FULL
    if any(word in text for word in ("失败", "错误", "不存在", "无权", "已结束", "不允许")):
        return False, Outcome.REJECTED
    return True, Outcome.SUCCESS


def decode_token_claims(token: str | None = None) -> dict[str, Any]:
    """解码 JWT 载荷（**不校验签名**），取出 `user_id` / `org_id` / `role` 等。

    只解析、不使用任何个人字段做展示；调用方也不应把它落盘到仓库里。
    """
    raw = token or session.load_token()
    if not raw:
        raise ApiError("缺少已保存的 token：请先运行 `python run.py login` 完成登录。")
    raw = raw[7:] if raw.lower().startswith("bearer ") else raw
    parts = raw.split(".")
    if len(parts) != 3:
        raise ApiError("token 不是标准 JWT，无法解析身份字段；请重新登录。")
    payload = parts[1]
    try:
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ApiError(f"token 载荷解码失败（{type(exc).__name__}）：请重新登录。") from exc


def _pg_date(value: dt.date) -> str:
    """PostgREST/PG 的日期参数写法：`2026-9-21`（不补零，与前端一致）。"""
    return f"{value.year}-{value.month}-{value.day}"


class PhyExpClient:
    """预约系统客户端：复用一条 keep-alive 会话，所有请求自动带 token。

    只读方法失败时抛 `ApiError`（可预期、消息面向使用者）；
    **写方法（`submit_booking` / `cancel_booking` / `select_project`）不抛异常**，
    统一返回 `WriteResult` —— 失败是业务结果，需要被记录与重试决策。
    """

    def __init__(self, base_url: str | None = None, timeout: float = 10.0) -> None:
        self.base_url = (base_url or config.API_BASE).rstrip("/")
        self.timeout = timeout
        self._http = None
        self.claims: dict[str, Any] = {}
        self._prepare()

    # ── 会话准备 ──

    def _prepare(self) -> None:
        import requests  # 延迟导入

        token = session.load_token()
        if not token:
            raise ApiError("缺少已保存的 token：请先运行 `python run.py login` 完成登录。")
        self.claims = decode_token_claims(token)

        http = requests.Session()
        http.headers.update({
            "User-Agent": config.USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Authorization": token,
            "Referer": config.BOOKING_ENTRY,
        })
        # 前端 `axios.defaults.withCredentials = true`：写接口走网关，带上会话 Cookie 更贴近真实前端。
        try:
            for name, value in session.load_cookies().items():
                http.cookies.set(name, value)
        except Exception:  # noqa: BLE001 - Cookie 缺失不应阻断只读能力
            pass
        # 连接预热：实测不预热首次要付 ~1250ms(TLS)，预热后稳态 ~11ms
        self._http = http
        self._prewarmed = False

    def prewarm(self) -> int:
        """预热连接，返回耗时（毫秒）。抢课/批量采集前先调一次。"""
        started = dt.datetime.now()
        self._get("rest/time")
        self._prewarmed = True
        return int((dt.datetime.now() - started).total_seconds() * 1000)

    @property
    def user_id(self) -> Any:
        return self.claims.get("user_id")

    @property
    def org_id(self) -> Any:
        return self.claims.get("org_id")

    # ── 底层请求 ──

    def _get(self, path: str, params: list[tuple[str, str]] | None = None,
             accept_single: bool = False) -> Any:
        """只读 GET。`params` 用**列表**传，以支持重复键（`or=`/`date=` 都要出现两次）。"""
        url = f"{self.base_url}/{path.lstrip('/')}"
        headers = {}
        if accept_single:
            headers["Accept"] = "application/vnd.pgrst.object+json"
        try:
            resp = self._http.get(url, params=params, headers=headers, timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001
            raise ApiError(f"请求失败（{type(exc).__name__}）：{exc}") from exc

        if resp.status_code == 401:
            raise ApiError("401 未授权（PostgREST 42501）：token 可能已过期，请重新运行 `python run.py login`。")
        if resp.status_code >= 400:
            raise ApiError(f"HTTP {resp.status_code}：{(resp.text or '')[:160]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise ApiError(f"响应不是 JSON（{exc}）：{(resp.text or '')[:160]}") from exc

    def _post_form(self, path: str, data: dict[str, Any] | None = None, *,
                   action: str = "post", target: str = "") -> WriteResult:
        """**写操作**底层：表单编码 POST，把结果如实分类（**不抛异常，返回结果对象**）。

        为什么写操作不抛异常：一次写请求的"失败"是**业务结果**（已满/限流/重复），
        调用方需要把它记录下来继续决策（退避重试等），而不是让流程中断。
        只有网络层异常才在结果里以 `error` 字段体现。
        """
        url = f"{self.base_url}/{path.lstrip('/')}"
        started = dt.datetime.now()
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        try:
            # 注意：requests 的 data=dict 会自动表单编码（charset utf-8），与前端 `qs.stringify` 一致
            resp = self._http.post(url, data=(data or {}), headers=headers, timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001
            elapsed = int((dt.datetime.now() - started).total_seconds() * 1000)
            return WriteResult(
                action=action, target=target, http_status=None, body_text="",
                ok=False, outcome=Outcome.UNKNOWN, elapsed_ms=elapsed,
                error=f"请求失败（{type(exc).__name__}）：{exc}",
            )
        elapsed = int((dt.datetime.now() - started).total_seconds() * 1000)
        text = (resp.text or "").strip()
        ok, outcome = classify_write(resp.status_code, text)
        result = WriteResult(
            action=action, target=target, http_status=resp.status_code,
            body_text=text[:2000], ok=ok, outcome=outcome, elapsed_ms=elapsed,
        )
        self._log_write(result)
        return result

    def _log_write(self, result: WriteResult) -> None:
        """把每次写操作追加到本地日志（**当天事后复盘的唯一依据**）。

        只写本地运行时目录；不写仓库。用于区分「自以为成功」与「服务端说成功」。
        """
        try:
            logs = config.logs_dir()
            logs.mkdir(parents=True, exist_ok=True)
            path = logs / f"write-{dt.datetime.now().strftime('%Y%m%d')}.jsonl"
            payload = {
                "at": dt.datetime.now().astimezone().isoformat(timespec="milliseconds"),
                "action": result.action,
                "target": result.target,
                "ok": result.ok,
                "outcome": result.outcome.value,
                "http_status": result.http_status,
                "elapsed_ms": result.elapsed_ms,
                "message": (result.error or result.body_text)[:500],
            }
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001 - 日志失败不能影响写操作本身的结论
            pass

    # ── 只读接口 ──

    def server_time(self) -> str:
        """服务端当前时间（用于对时；预发射的 T0 应以服务端时钟为准）。"""
        data = self._get("rest/time")
        if isinstance(data, list) and data:
            return str(data[0].get("time", ""))
        return ""

    def open_semesters(self) -> list[dict]:
        """当前开放的学期。"""
        data = self._get("rest/semesters", [("is_open", "eq.true")])
        return data if isinstance(data, list) else [data]

    def my_courses(self, semester_id: Any) -> list[dict]:
        """我在某学期选的课程（`rest/rpc/user_courses`）。

        ⚠️ **RPC 的参数是裸值**，不能像表查询那样加 `eq.` 前缀
        （实测：带 `eq.` 会得到 404 `Could not find api.user_courses...`）。
        """
        data = self._get("rest/rpc/user_courses", [
            ("user_id", str(self.user_id)),
            ("organization_id", str(self.org_id)),
            ("semester_id", str(semester_id)),
        ])
        return data if isinstance(data, list) else [data]

    def course_projects(self, course_id: Any) -> list[dict]:
        """课程下的实验项目（`course2projects` + 内嵌 `projects(*)`）。"""
        data = self._get("rest/course2projects", [
            ("select", "course_id,project_id,optional,group,free_schedule,projects(*)"),
            ("course_id", f"eq.{course_id}"),
        ])
        return data if isinstance(data, list) else [data]

    def slots(self, course_id: Any, *, project_id: Any | None = None,
              date_from: dt.date | None = None, date_to: dt.date | None = None,
              with_my_status: bool = True) -> list[dict]:
        """场次列表（**含容量与已选人数**，抢课监控的主查询）。

        `with_my_status=True` 时只返回「我已选/已排」的场次（与前端首页一致）；
        研究余量时通常传 `False`，以拿到该项目的全部可约场次。
        """
        today = dt.date.today()
        start = date_from or today
        end = date_to or (today + dt.timedelta(days=180))
        params: list[tuple[str, str]] = [
            ("select", "*,periods(*),locations(*),teacher:users!schedule_teacher_id_fkey(*),"
                       "user2projects!user2project_schedule_id_fkey(*)"),
            ("is_publish", "eq.true"),
            ("or", f"(course_id.is.null,course_id.eq.{course_id})"),
            ("or", f"(is_reserved.eq.false,class_list.cs.{{{self.org_id}}},class_list.cs.{{null}})"),
            ("date", f"gte.{_pg_date(start)}"),
            ("date", f"lte.{_pg_date(end)}"),
        ]
        if project_id is not None:
            params.append(("project_id", f"eq.{project_id}"))
        if with_my_status:
            params.append(("user2projects.user_id", f"eq.{self.user_id}"))
            params.append(("user2projects.schedule_status", "in.(elected,scheduled)"))
        data = self._get("rest/schedules", params)
        return data if isinstance(data, list) else [data]

    # ── 模型转换 ──

    @staticmethod
    def to_slot(row: dict) -> Slot:
        """把 `schedules` 行转成 `Slot`（字段缺失一律保持 None，**不猜**）。"""
        periods = row.get("periods") or {}
        locations = row.get("locations") or {}
        time_text = ""
        if row.get("date"):
            start = periods.get("start_time") or ""
            end = periods.get("end_time") or ""
            time_text = f"{row['date']} {start}-{end}".strip(" -")
        def _int(value: Any) -> int | None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
        return Slot(
            slot_id=str(row.get("id", "")),
            experiment_id=str(row.get("project_id", "")),
            time_text=time_text,
            taken=_int(row.get("current_student_number")),
            capacity=_int(row.get("max_student_number")),
            location=locations.get("name"),
            raw=row,
        )

    @staticmethod
    def to_experiment(row: dict) -> Experiment:
        """把 `course2projects` 行（内嵌 projects）转成 `Experiment`。"""
        project = row.get("projects") or {}
        return Experiment(
            experiment_id=str(row.get("project_id", "")),
            name=str(project.get("name", "")),
            category=str(project.get("code") or "") or None,
            capacity=None,
            raw=row,
        )

    # ── 写操作（2026-10-07 依线上前端调用点实现）──

    def my_electives(self, semester_id: Any, course_id: Any | None = None) -> list[dict]:
        """我选上的**选课记录**（`rest/user2projects`），含 `id`（退课要用的 id）。

        证据：前端「我的实验」页用的正是这条查询
        （`user2projects?select=...&user_id=eq.&semester_id=eq.&schedule_status=in.(elected,scheduled)`）。
        """
        params: list[tuple[str, str]] = [
            ("select", "id,schedule_id,project_id,course_id,schedule_status,created_at,"
                       "ordernumber_of_schedule,schedules!user2project_schedule_id_fkey"
                       "(date,periods(name,start_time,end_time),locations(name),"
                       "projects:projects!schedule_project_id_fkey(name))"),
            ("user_id", f"eq.{self.user_id}"),
            ("semester_id", f"eq.{semester_id}"),
            ("schedule_status", "in.(elected,scheduled,free_schedule)"),
        ]
        if course_id is not None:
            params.append(("course_id", f"eq.{course_id}"))
        data = self._get("rest/user2projects", params)
        return data if isinstance(data, list) else [data]

    def schedule(self, slot_id: Any) -> dict:
        """按 id 读**单个场次**（提交前后核对余量与状态用）。"""
        data = self._get("rest/schedules", [
            ("select", "id,date,current_student_number,max_student_number,is_publish,"
                       "project_id,course_id,periods(name,start_time,end_time),locations(name)"),
            ("id", f"eq.{slot_id}"),
        ])
        if isinstance(data, list):
            return data[0] if data else {}
        return data if isinstance(data, dict) else {}

    def submit_booking(self, slot_id: str, course_id: Any) -> WriteResult:
        """**选课**：`POST report-api/electives`，表单 `lesson_id` + `course_id`。

        `slot_id` 即 `schedules.id`（前端 `lesson.id`；前端把 `rest/schedules` 的行直接当 lesson 用，
        且同一处既取 `lesson.id` 又取 `lesson.schedule_id` ⇒ 两者同为 `schedules.id`）。
        """
        return self._post_form(
            config.ELECT_ENDPOINT,
            {"lesson_id": str(slot_id), "course_id": str(course_id)},
            action="选课", target=f"lesson_id={slot_id} course_id={course_id}",
        )

    def cancel_booking(self, user2project_id: Any) -> WriteResult:
        """**退课**：`POST report-api/electives/<user2projects.id>/cancel`。

        `user2project_id` 来自「我的选课记录」（`rest/user2projects.id`），**不是** `schedules.id`。
        """
        path = f"{config.ELECT_ENDPOINT}/{user2project_id}/cancel"
        return self._post_form(path, None, action="退课", target=f"user2project_id={user2project_id}")

    def select_project(self, project_id: Any, course_id: Any) -> WriteResult:
        """**选实验项目**：`POST report-api/electives/project`，表单 `course_id` + `project_id`。

        部分课程要求先在「选实验项目」里挑项目，才能约该项目的场次。
        """
        return self._post_form(
            f"{config.ELECT_ENDPOINT}/project",
            {"course_id": str(course_id), "project_id": str(project_id)},
            action="选实验项目", target=f"project_id={project_id} course_id={course_id}",
        )

    def close(self) -> None:
        if self._http is not None:
            try:
                self._http.close()
            finally:
                self._http = None

    def __enter__(self) -> "PhyExpClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
