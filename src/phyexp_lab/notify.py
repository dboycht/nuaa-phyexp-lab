"""桌面通知（Windows）：让"人不在电脑前"也能知道抢课结果。

设计要点（对应 `docs/抢课规格.md` §7.1）
--------------------------------------
1. **通知失败不致命**：任何异常都只记日志，**绝不影响抢课本身**（A9）。
2. **成功与失败都通知**：只在成功时通知，恰好会漏掉最需要知道的情况。
3. **不阻塞**：通知投递放在独立子进程里并设超时，避免拖慢收尾。
4. **纯 ASCII 探针纪律**：标题/正文允许中文（走临时 JSON 文件传递，不拼进命令行）。

实现方式
--------
用 Windows 自带的 PowerShell 调 WinRT 的 `ToastNotificationManager`；
不用第三方库（无需安装依赖）。非 Windows 或调用失败时**如实返回失败原因**，不假装成功。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

LogFn = Callable[[str], None]

#: PowerShell 侧脚本（**纯 ASCII**，中文内容从 JSON 文件读，避免命令行编码问题）。
_TOAST_PS1 = r"""
param([string]$JsonPath)
$ErrorActionPreference = 'Stop'
$payload = Get-Content -Raw -LiteralPath $JsonPath -Encoding UTF8 | ConvertFrom-Json
[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime]
[void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime]
$template = [Windows.UI.Notifications.ToastTemplateType]::ToastText02
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent($template)
$nodes = $xml.GetElementsByTagName('text')
$nodes.Item(0).AppendChild($xml.CreateTextNode($payload.title)) | Out-Null
$nodes.Item(1).AppendChild($xml.CreateTextNode($payload.body)) | Out-Null
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
$appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
"""


def _write_temp(suffix: str, content: str, *, bom: bool = False) -> Path:
    """写临时文件并**关闭句柄**后再返回路径。

    ⚠️ **实测踩坑（2026-10-07）**：`tempfile.mkstemp` 返回的**文件描述符必须关掉**，
    否则在 Windows 上该文件仍被本进程占用 —— 再把路径交给 `powershell.exe` 就会得到
    `The process cannot access the file ... because it is being used by another process`。
    （本项目已有先例：通知/附属能力的失败绝不能拖垮主流程，见 `docs/抢课规格.md` A9。）
    """
    import os

    fd, name = tempfile.mkstemp(prefix="phyexp-toast-", suffix=suffix)
    path = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig" if bom else "utf-8", newline="") as handle:
            handle.write(content)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def notify(title: str, body: str, *, log: LogFn | None = None, timeout: float = 15.0) -> bool:
    """发一条 Windows 桌面通知。返回是否成功；失败原因写日志（不抛异常）。"""
    log = log or (lambda _msg: None)
    if sys.platform != "win32":
        log(f"[通知] 跳过：当前平台 {sys.platform} 不支持 Windows 通知")
        return False

    payload_path: Path | None = None
    script_path: Path | None = None
    try:
        payload_path = _write_temp(".json", json.dumps({"title": title, "body": body},
                                                       ensure_ascii=False))
        # 带 BOM：PS 5.1 才认得 UTF-8 脚本（纯 ASCII 内容，BOM 只为稳妥）
        script_path = _write_temp(".ps1", _TOAST_PS1, bom=True)
        try:
            proc = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", str(script_path), "-JsonPath", str(payload_path)],
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            log(f"[通知] 发送超时（>{timeout:.0f}s），已放弃（不影响抢课结果）")
            return False
        if proc.returncode == 0:
            log("[通知] 已发送桌面通知")
            return True
        detail = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")[:200]
        log(f"[通知] 发送失败（exit {proc.returncode}）：{detail or '无输出'}")
        return False
    except Exception as exc:  # noqa: BLE001 - 通知是附属能力，绝不向上抛
        log(f"[通知] 发送异常（{type(exc).__name__}）：{exc}")
        return False
    finally:
        for path in (payload_path, script_path):
            try:
                if path is not None:
                    path.unlink(missing_ok=True)
            except OSError:
                pass


def summarize(lines: list[str], *, limit: int = 6) -> str:
    """把多行结果压成通知正文（通知空间有限，只放关键行）。"""
    picked = [line.strip() for line in lines if line.strip()][:limit]
    return "\n".join(picked) if picked else "（无内容）"
