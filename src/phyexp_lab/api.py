"""预约系统接口封装。

**当前状态：未实现（阶段 0）**。

为什么是空的：本项目的接口必须从真实请求样本逆向（`docs/接口逆向.md`），
在此之前任何「猜出来」的 URL/参数都是伪实现。因此下面每个方法都**显式抛出
`NotImplementedError` 并指明下一步**，而不是返回假数据或静默失败。

实现步骤
--------
1. `python run.py recon` → 在弹出窗口里登录，并点进「预约选课」各页面；
2. 打开 `%LOCALAPPDATA%\\PhyExpLab\\recon\\*.jsonl`（脱敏摘要）与 `*.har`（含响应体）；
3. 把接口填进 `docs/接口逆向.md` 的表格；
4. 回到本文件实现，并用 `requests.Session` 复用 `session.load_cookies()` 的会话。
"""

from __future__ import annotations

from typing import Any

from .models import Experiment, Slot

_PENDING = (
    "接口尚未逆向完成：请先运行 `python run.py recon` 采集真实请求样本，"
    "并填写 docs/接口逆向.md 的接口清单后再实现本方法。"
)


class PhyExpClient:
    """预约系统客户端（登录态复用 + 接口调用）。"""

    def __init__(self, base_url: str, cookies: dict[str, str] | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.cookies = cookies or {}

    @classmethod
    def from_saved_session(cls, base_url: str) -> "PhyExpClient":
        """从本地保存的会话构造客户端（会话由 `run.py login` 生成）。"""
        from .session import load_cookies

        return cls(base_url, load_cookies())

    # ── 待实现接口 ──

    def list_experiments(self) -> list[Experiment]:
        """拉取可选实验项目清单。"""
        raise NotImplementedError(_PENDING)

    def list_slots(self, experiment_id: str) -> list[Slot]:
        """拉取某实验项目的可选时段与余量。"""
        raise NotImplementedError(_PENDING)

    def get_remaining(self, slot_id: str) -> int | None:
        """查询单个时段剩余名额（监控主循环的最小请求）。"""
        raise NotImplementedError(_PENDING)

    def submit_booking(self, slot_id: str) -> Any:
        """提交预约（抢课动作本体）。"""
        raise NotImplementedError(_PENDING)
