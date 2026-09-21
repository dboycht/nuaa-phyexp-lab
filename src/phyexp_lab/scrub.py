"""HAR 脱敏：把抓包文件里的凭据与个人内容抹掉，只留下逆向需要的「形状」。

为什么必须做这一步
------------------
HAR 是**全量流量**：它会把 `POST rest/rpc/login` 的请求体（内含**密码的 MD5**，
可离线爆破）、每个 API 请求的 `Authorization: Bearer <jwt>`、
`Set-Cookie` 的值，以及你浏览过的页面响应体（报告、作答、个人信息）统统记下来。
这些内容对接口逆向**没有必要**，留着只会扩大泄露面。

脱敏规则（与 `recon.py` 共用同一份敏感判定，避免两处走偏）
--------------------------------------------------------
1. **敏感 URL**（登录/认证/验证码/改密…见 `recon.is_sensitive_url`）：
   - 删除 `request.postData`（登录体就在这里）；
   - 删除响应体 `content.text`；
2. **所有条目**：`Cookie` / `Set-Cookie` / `Authorization` 等敏感头，值一律替换为 `<redacted len=N>`；
3. 过程**幂等**：重复跑同一文件结果一致（已脱敏的值不会二次改写）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import recon


def _redact_header_list(headers: list[dict[str, Any]] | None) -> int:
    """就地脱敏一组 HAR 头对象，返回被改写的条数。"""
    if not headers:
        return 0
    changed = 0
    for header in headers:
        name = str(header.get("name", ""))
        if name.lower() in recon.SENSITIVE_HEADERS:
            value = str(header.get("value", ""))
            marker = f"<redacted len={len(value)}>"
            if header.get("value") != marker:
                header["value"] = marker
                changed += 1
    return changed


def scrub_har(source: Path, target: Path) -> dict[str, int]:
    """把 ``source`` 脱敏后写到 ``target``，返回统计。

    统计项：`entries`（总条目）、`post_data_removed`、`headers_redacted`、`bodies_removed`。
    """
    data = json.loads(Path(source).read_text(encoding="utf-8"))
    entries = ((data.get("log") or {}).get("entries")) or []

    stats = {"entries": len(entries), "post_data_removed": 0, "headers_redacted": 0, "bodies_removed": 0}

    for entry in entries:
        request = entry.get("request") or {}
        response = entry.get("response") or {}
        url = str(request.get("url", ""))
        sensitive = recon.is_sensitive_url(url)

        stats["headers_redacted"] += _redact_header_list(request.get("headers"))
        stats["headers_redacted"] += _redact_header_list(response.get("headers"))
        # HAR 的 cookies 数组与 headers 里是同一份值，单独再清一次（值只留标记）
        for jar in (request.get("cookies"), response.get("cookies")):
            for cookie in jar or []:
                if cookie.get("value"):
                    cookie["value"] = "<redacted>"
                    stats["headers_redacted"] += 1

        if sensitive:
            if "postData" in request:
                del request["postData"]
                stats["post_data_removed"] += 1
            content = response.get("content") or {}
            if content.get("text"):
                content["text"] = "<redacted: sensitive endpoint>"
                content["size"] = 0
                stats["bodies_removed"] += 1

    Path(target).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return stats


def default_target(source: Path) -> Path:
    """默认输出名：`capture-x.har` → `capture-x.scrubbed.har`。"""
    source = Path(source)
    return source.with_name(source.stem + ".scrubbed.har")


def verify_no_credentials(path: Path) -> list[str]:
    """复检：在脱敏后的 HAR 里搜「不该再出现」的痕迹，返回问题描述列表（空 = 通过）。

    检查项：敏感头的非空值、敏感接口残留的 postData、疑似 JWT（`eyJ` 开头的三段串）。
    """
    problems: list[str] = []
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    entries = ((data.get("log") or {}).get("entries")) or []

    for index, entry in enumerate(entries):
        request = entry.get("request") or {}
        response = entry.get("response") or {}
        url = str(request.get("url", ""))

        for header in list(request.get("headers") or []) + list(response.get("headers") or []):
            name = str(header.get("name", "")).lower()
            value = str(header.get("value", ""))
            if name in recon.SENSITIVE_HEADERS and value and not value.startswith("<redacted"):
                problems.append(f"entry#{index} {name} 仍含明文值（{len(value)} 字符）")

        if recon.is_sensitive_url(url) and "postData" in request:
            problems.append(f"entry#{index} 敏感接口仍残留 postData：{url[:80]}")

        blob = json.dumps(entry, ensure_ascii=False)
        if "eyJ" in blob and "Bearer eyJ" in blob:
            problems.append(f"entry#{index} 疑似残留 JWT：{url[:80]}")

    return problems
