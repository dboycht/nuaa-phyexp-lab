"""请求采集：把浏览器里真实发生的流量落成两份证据。

- **HAR（完整）**：由 Playwright `record_har_path` 直接产出，含响应体，是接口逆向的主依据；
- **JSONL（脱敏摘要）**：本模块自己写，一行一条请求，便于快速 `Select-String` / 阅读。

脱敏纪律
--------
Cookie / Authorization / Set-Cookie 的值**只记录长度**；统一身份认证、登录、验证码相关的
请求（URL 命中敏感词）**不记录请求体与响应体**，避免把凭据写进日志。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Callable

from . import config

#: 这些请求头的**值**一律不落盘
SENSITIVE_HEADERS = {
    "cookie",
    "set-cookie",
    "authorization",
    "proxy-authorization",
    "x-auth-token",
    "x-csrf-token",
}

#: URL 命中这些片段的请求，不记录 body（登录/认证/验证码链路）
SENSITIVE_URL_MARKERS = (
    "authserver",
    "login",
    "logon",
    "signin",
    "sso",
    "/cas",
    "passport",
    "captcha",
    "verify",
    "password",
)

#: 这些资源类型的请求只进 HAR，不进 JSONL（避免日志被静态资源刷屏）
NOISY_RESOURCE_TYPES = {"image", "font", "media", "stylesheet"}

_TEXT_CONTENT_HINTS = ("json", "javascript", "html", "xml", "text", "urlencoded")


def _now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


def new_capture_paths() -> tuple[Path, Path]:
    """生成本次采集的 (JSONL 路径, HAR 路径)，以本地时间戳命名。"""
    config.ensure_home()
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return (
        config.recon_dir() / f"capture-{stamp}.jsonl",
        config.recon_dir() / f"capture-{stamp}.har",
    )


def is_sensitive_url(url: str) -> bool:
    lowered = (url or "").lower()
    return any(marker in lowered for marker in SENSITIVE_URL_MARKERS)


def redact_headers(headers: dict[str, str] | None) -> dict[str, str]:
    """把敏感请求头的值替换成 `<redacted len=N>`。"""
    if not headers:
        return {}
    safe: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in SENSITIVE_HEADERS:
            safe[key] = f"<redacted len={len(value or '')}>"
        else:
            safe[key] = value
    return safe


def _is_textual(content_type: str | None) -> bool:
    if not content_type:
        return False
    lowered = content_type.lower()
    return any(hint in lowered for hint in _TEXT_CONTENT_HINTS)


class RequestRecorder:
    """把响应事件写成脱敏 JSONL。"""

    def __init__(
        self,
        jsonl_path: Path,
        *,
        capture_bodies: bool = True,
        max_body_chars: int = 4000,
        include_noisy: bool = False,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.jsonl_path = Path(jsonl_path)
        self.capture_bodies = capture_bodies
        self.max_body_chars = max_body_chars
        self.include_noisy = include_noisy
        self._log = log
        self.count = 0
        self._handle: Any = None

    # ── 事件挂载 ──

    def attach(self, context: Any) -> None:
        """把记录器挂到 Playwright BrowserContext 上。"""
        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.jsonl_path.open("a", encoding="utf-8")
        context.on("response", self._on_response)

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            finally:
                self._handle = None

    # ── 记录逻辑 ──

    def _on_response(self, response: Any) -> None:
        # 记录器自身的任何异常都不允许影响使用者浏览，故整体兜底
        try:
            request = response.request
            resource_type = request.resource_type
            if not self.include_noisy and resource_type in NOISY_RESOURCE_TYPES:
                return

            url = response.url
            sensitive = is_sensitive_url(url)
            record: dict[str, Any] = {
                "t": _now_iso(),
                "method": request.method,
                "status": response.status,
                "resource_type": resource_type,
                "url": url,
                "sensitive_url": sensitive,
                "req_headers": redact_headers(request.headers),
            }

            if not sensitive:
                post_data = None
                try:
                    post_data = request.post_data
                except Exception:
                    post_data = None
                if post_data:
                    record["post_data"] = post_data[: self.max_body_chars]

            try:
                record["duration_ms"] = request.timing.get("responseEnd")
            except Exception:
                pass

            response_headers = {}
            try:
                response_headers = response.headers
            except Exception:
                pass
            content_type = None
            for key, value in (response_headers or {}).items():
                if key.lower() == "content-type":
                    content_type = value
                    break
            if content_type:
                record["resp_content_type"] = content_type

            if self.capture_bodies and not sensitive and _is_textual(content_type):
                try:
                    body = response.text()
                except Exception as exc:
                    record["body_error"] = type(exc).__name__
                else:
                    if body:
                        record["body_preview"] = body[: self.max_body_chars]
                        record["body_length"] = len(body)

            self._write(record)
        except Exception as exc:  # pragma: no cover - 兜底，绝不影响浏览
            if self._log:
                self._log(f"[recon][warn] 记录请求失败（已忽略）：{type(exc).__name__}: {exc}")

    def _write(self, record: dict[str, Any]) -> None:
        if self._handle is None:
            return
        self._handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._handle.flush()
        self.count += 1
        if self._log and self.count % 25 == 0:
            self._log(f"[recon] 已记录 {self.count} 条请求…")
