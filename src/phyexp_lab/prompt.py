"""抢课结束后的交互：显示本轮结果，并让用户**当场选择要不要退掉某些**。

设计约束（都来自本项目的实测教训）
--------------------------------
1. **纯解析与交互分离**：`parse_selection()` 是纯函数（可单测、无 IO），
   交互循环才做 IO —— 这样"用户输入理解错"这类 bug 能被单元测试钉住。
2. **只认数字**（`1,3` / `1 3` / `1、3` 都行；空回车 = 全部保留）：
   中文输入在 Windows 终端里受代码页影响（本项目踩过 GBK/UTF-8 的坑），
   **让用户敲数字**比敲"退"字稳得多。
3. **不可逆操作要二次确认**：扣掉输入后**再问一次** y/N，默认 N（保留）。
4. **非交互环境自动跳过**：重定向/无 stdin/TTY 时**绝不**进入提问（否则会挂住或误判），
   并明确打印"已跳过，可用 python run.py cancel --id <id> 手动退"。
"""

from __future__ import annotations

import sys
from typing import Callable

LogFn = Callable[[str], None]


class SelectionError(ValueError):
    """用户输入无法解析（消息面向使用者，可直接打印）。"""


def parse_selection(raw: str, count: int) -> list[int]:
    """把用户输入解析成 **0 基下标**列表。

    合法写法：`1,3` / `1 3` / `1、3` / `1，3` / `1-3`；
    空串或 `0` = **不选任何一条**（全部保留）。
    非法输入抛 `SelectionError`（消息里说明合法范围），**不猜**用户想选什么。
    """
    text = (raw or "").strip()
    if not text or text in {"0", "无", "none", "n"}:
        return []
    picked: list[int] = []
    normalized = text.replace("，", ",").replace("、", ",").replace(" ", ",")
    for chunk in normalized.split(","):
        item = chunk.strip()
        if not item:
            continue
        if "-" in item:
            parts = item.split("-", 1)
            if not all(p.strip().isdigit() for p in parts):
                raise SelectionError(f"无法解析区间：{item!r}（应形如 2-4）")
            start, end = (int(p) for p in parts)
            if start < 1 or end < start or end > count:
                raise SelectionError(f"区间超出范围：{item!r}（可选 1..{count}）")
            picked.extend(range(start - 1, end))
            continue
        if not item.isdigit():
            raise SelectionError(
                f"无法解析：{item!r}（请输入序号，如 1,3 或 1-2；直接回车 = 全部保留）"
            )
        index = int(item)
        if index < 1 or index > count:
            raise SelectionError(f"序号超出范围：{index}（可选 1..{count}）")
        picked.append(index - 1)
    # 去重且保持原顺序
    seen: list[int] = []
    for index in picked:
        if index not in seen:
            seen.append(index)
    return seen


def interactive_available() -> bool:
    """能否与用户交互（有 stdout 且 stdin 是终端）。"""
    try:
        return sys.stdout is not None and sys.stdin is not None and sys.stdin.isatty()
    except Exception:  # noqa: BLE001
        return False


def ask_selection(count: int, *, prompt: str = "请输入要退课的序号") -> list[int] | None:
    """反复询问直到拿到合法输入；EOF/Ctrl+C 返回 None（= 放弃退课，保留全部）。"""
    while True:
        try:
            raw = input(f"{prompt}（如 1,3；直接回车 = 全部保留）：")
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        try:
            return parse_selection(raw, count)
        except SelectionError as exc:
            print(f"  [输入有误] {exc}")


def confirm(text: str, *, default: bool = False) -> bool:
    """二次确认。默认值必须是**安全的那一侧**（这里默认 N = 不执行退课）。"""
    suffix = " [y/N] " if not default else " [Y/n] "
    try:
        raw = input(text + suffix).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    if not raw:
        return default
    return raw in {"y", "yes", "是", "1"}
