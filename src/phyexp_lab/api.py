"""预约系统接口封装（**只读部分已实现**；写操作待选课窗口实测后再实现）。

实现依据
--------
全部来自**实测**（`docs/接口逆向.md` §3.1/§3.6）：

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

⚠️ 身份与隐私：`user_id` / `org_id` / 姓名 / 学号 / openid 都来自**使用者本人的 token 载荷**，
只在**内存与本地运行时目录**中使用（`%LOCALAPPDATA%\\PhyExpLab`）；**绝不写入本仓库任何文件**。
"""

from __future__ import annotations

import base64
import datetime as dt
import json
from typing import Any

from . import config, session
from .models import Experiment, Slot


class ApiError(RuntimeError):
    """接口层可预期失败（消息面向使用者，可直接打印）。"""


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
    """预约系统只读客户端：复用一条 keep-alive 会话，所有请求自动带 token。"""

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

    # ── 写操作：待实现（等选课窗口实测请求体后再写，绝不猜参数）──

    def submit_booking(self, slot_id: str) -> Any:
        """提交预约（抢课动作本体）。"""
        raise NotImplementedError(
            "写操作尚未实测：需在**选课窗口开放时**抓一次真实提交（POST report-api/electives）"
            "确认请求体与响应判据后再实现。see docs/接口逆向.md §3.4/§六。"
        )

    def cancel_booking(self, user2project_id: Any) -> Any:
        """退课（`PATCH rest/user2projects?id=eq.<id>`）。"""
        raise NotImplementedError(
            "退课尚未实测：需在可操作窗口内抓一次 PATCH rest/user2projects 的真实请求体后再实现。"
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
