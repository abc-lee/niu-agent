"""会话单元切割器——把消息序列切为会话单元的纯函数。

语义（spec §3.2 / 计划 Task 1）：user 开启 → 其派生 assistant/tool 链收口为一个单元。
边界规则：
  ① 首条若非 user，归入第一单元（切割点不早于 0）
  ② 连续多条 user 视为同一单元开启
  ③ 孤立 tool（无前导 assistant）归入前一单元
  ④ 空列表返回 []
  ⑤ 压缩三件套行（[系统提示] 上下文即将压缩/已完成 前缀的 user 行）透视：
     自身不开单元，且对非三件套 user 行的「前条判邻」透明——向前跳过连续
     三件套找最后一条非三件套消息判定 prev-role（嵌套在外层单元的工具循环
     内，归入外层单元；spec 2026-09-07 §3.1）

输入为 Message 对象序列（duck-typing：role 属性，dict 也兼容），不依赖 DB。
返回闭区间索引对列表 [(start_idx, end_idx), ...]，相邻区间无缝衔接、
并集覆盖全部消息；messages[start : end + 1] 即该单元的消息切片。
"""

from __future__ import annotations

from collections.abc import Sequence

# 压缩三件套（嵌套在外层单元的工具循环内）透视判据——精确匹配两条常量前缀
# 防扩大影响面（spec 2026-09-07：嵌套切割修复）。本地常量前缀——保持 slicer
# 零依赖轻量（agent_loop 常量不导入；两副本由 parity 契约测试绑定，见
# tests/test_compression_summary.py::test_triplet_prefix_parity）。
_COMPRESSION_TRIPLET_PREFIXES = (
    "[系统提示] 上下文即将压缩",   # agent_loop._SUMMARY_PROMPT（准备提问）
    "[系统提示] 上下文压缩已完成", # agent_loop._COMPRESSION_DONE_HINT（完成提示）
)


def _role(message) -> str:
    role = getattr(message, "role", None)
    if role is None and isinstance(message, dict):
        role = message.get("role")
    return role if isinstance(role, str) else ""


def _is_compression_triplet(message) -> bool:
    """压缩三件套行判定：role=user 且 content 以三件套前缀之一开头。"""
    if _role(message) != "user":
        return False
    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content")
    return isinstance(content, str) and content.startswith(
        _COMPRESSION_TRIPLET_PREFIXES
    )


def slice_units(messages: Sequence) -> list[tuple[int, int]]:
    """切割消息序列为会话单元，返回闭区间 [(start_idx, end_idx), ...]。"""
    n = len(messages)
    if n == 0:
        return []

    # 单元起点集合：首条恒为第一单元起点（规则①）；此后每遇
    # 「user 且前一条非 user」开启新单元——连续 user 不重复切割（规则②），
    # assistant/tool/system/subagent_msg 一律延续当前单元（规则③）。
    # 三件套透视（规则⑤）：三件套行自身不开单元，且对「前条判邻」透明——
    # 非三件套 user 行向前跳过连续三件套找最后一条非三件套消息再判定。
    # 摊还 O(n)：每个三件套行至多被回看扫一次（不同起点的扫描段互不相交）。
    starts = [0]
    for i in range(1, n):
        if _role(messages[i]) != "user" or _is_compression_triplet(messages[i]):
            continue  # 非 user 或三件套行：不作为单元开启者
        # 回看透视：向前跳过连续三件套找最后一条非三件套消息
        j = i - 1
        while j >= 0 and _is_compression_triplet(messages[j]):
            j -= 1
        if j < 0 or _role(messages[j]) != "user":
            starts.append(i)

    return [
        (s, starts[k + 1] - 1 if k + 1 < len(starts) else n - 1)
        for k, s in enumerate(starts)
    ]
