"""会话单元切割器——把消息序列切为会话单元的纯函数。

语义（spec §3.2 / 计划 Task 1）：user 开启 → 其派生 assistant/tool 链收口为一个单元。
边界规则：
  ① 首条若非 user，归入第一单元（切割点不早于 0）
  ② 连续多条 user 视为同一单元开启
  ③ 孤立 tool（无前导 assistant）归入前一单元
  ④ 空列表返回 []

输入为 Message 对象序列（duck-typing：role 属性，dict 也兼容），不依赖 DB。
返回闭区间索引对列表 [(start_idx, end_idx), ...]，相邻区间无缝衔接、
并集覆盖全部消息；messages[start : end + 1] 即该单元的消息切片。
"""

from __future__ import annotations

from collections.abc import Sequence


def _role(message) -> str:
    role = getattr(message, "role", None)
    if role is None and isinstance(message, dict):
        role = message.get("role")
    return role if isinstance(role, str) else ""


def slice_units(messages: Sequence) -> list[tuple[int, int]]:
    """切割消息序列为会话单元，返回闭区间 [(start_idx, end_idx), ...]。"""
    n = len(messages)
    if n == 0:
        return []

    # 单元起点集合：首条恒为第一单元起点（规则①）；此后每遇
    # 「user 且前一条非 user」开启新单元——连续 user 不重复切割（规则②），
    # assistant/tool/system/subagent_msg 一律延续当前单元（规则③）。
    starts = [0]
    for i in range(1, n):
        if _role(messages[i]) == "user" and _role(messages[i - 1]) != "user":
            starts.append(i)

    return [
        (s, starts[k + 1] - 1 if k + 1 < len(starts) else n - 1)
        for k, s in enumerate(starts)
    ]
