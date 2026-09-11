"""发送层 LLM API 合规化（plan 2026-09-10-tool-message-order-fix §2 D1/D2）。

单一职责：LiteLLMSession.chat() 入口把调用方 messages 规整为 OpenAI 规范合规的
**副本**并在 chat() 局部重绑——调用方列表零变化（长度不变式 / 不回流 DB/档/UI），
raw_http 日志 = 真 wire 体。

固定执行序（丢弃在前、规整最后）：
  ① subagent_msg 丢弃（@ 消息仅供前端展示）
  ② 孤儿 tool 丢弃（无对应 assistant tool_calls；缺 tool_call_id 同判孤儿）
  ③ 非 user list content 降级：tool/assistant list → str（text 段拼接、
     image_url → [图片已省略]）；system 原样保留（其 list 承载 cache_control，
     压平会静默废掉 prompt caching）
  ④ 悬空 tool_calls 按 tc 剥离（镜像 transform_history valid_tcs 先例）；
     全部悬空才整消息降级为纯文本（不设 tool_calls 键、不删消息）——避免部分
     悬空整消息降级在步骤②之后制造新孤儿
  ⑤ 顺序规整（最后 pass）：每个 assistant(tool_calls) 后紧跟其全部 tool 响应；
     夹入消息（user 回答等）顺延到 tool 块之后

幂等语义：输入恒等幂等（二次净化逐字节相同）。纯函数，叶子层模块防循环 import。
"""

from __future__ import annotations


def _list_to_str(content) -> str:
    """非 user list content 降级为 str：text 段拼接、image_url → [图片已省略]。"""
    parts: list[str] = []
    for seg in (content if isinstance(content, list) else [content]):
        if isinstance(seg, dict):
            t = seg.get("type")
            if t == "text":
                parts.append(str(seg.get("text", "")))
            elif t == "image_url":
                parts.append("[图片已省略]")
        else:
            parts.append(str(seg))
    return "".join(parts)


def _reorder_tool_responses(messages: list[dict]) -> list[dict]:
    """顺序规整（最后 pass）：assistant(tool_calls) 后紧跟其全部 tool 响应。

    全局配对：先建 tool_call_id → tool 消息的全局索引，再左→右遍历；遇
    assistant(tool_calls) 即把其全部 tool 响应（无论当前在哪）紧跟其后输出，
    其间夹入的 user/assistant 保持相对次序、顺延到 tool 块之后。相邻 assistant
    （中断/压缩重建形态 A(tc)→B(纯文本)→tool）同样归位——旧「扫描止于下一个
    assistant」在此漏网。已规整输入原样返回（幂等）；未配对 tool（孤儿已在②
    丢弃，此处防御性保留原位）。
    """
    tools_by_id: dict[str, list[dict]] = {}
    for m in messages:
        if m.get("role") == "tool":
            tcid = m.get("tool_call_id")
            if tcid is not None:
                tools_by_id.setdefault(tcid, []).append(m)

    result: list[dict] = []
    emitted: set[int] = set()  # id() of tool messages already placed next to owner
    i, n = 0, len(messages)
    while i < n:
        m = messages[i]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            result.append(m)
            for tc in m["tool_calls"]:
                if not (isinstance(tc, dict) and tc.get("id")):
                    continue
                for t in tools_by_id.get(tc["id"], []):
                    if id(t) not in emitted:
                        result.append(t)
                        emitted.add(id(t))
        else:
            # 已归位到其 assistant 之后的 tool 跳过原位置；其余（含未配对 tool）原位保留
            if m.get("role") == "tool" and id(m) in emitted:
                i += 1
                continue
            result.append(m)
        i += 1
    return result


def sanitize_llm_messages(messages):
    """发送层合规化（纯函数）：返回 OpenAI 规范合规的消息**副本**。

    - 不就地改调用方列表/消息对象（长度不变式保持；产物不回流 DB/档/UI）
    - 消息集变更：丢弃「孤儿 tool」「subagent_msg」；其余一律保留（重排/降级）

    Args:
        messages: LLM 上下文消息列表（transform_history / agent_runner_loop 产物形态）

    Returns:
        新的消息列表（合规副本）；输入为空 → []
    """
    if not messages:
        return []

    # ① subagent_msg 丢弃 + 收集 assistant tool_call_id（供 ② 孤儿判定）
    kept: list[dict] = []
    valid_tc_ids: set[str] = set()
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "user")
        if role == "subagent_msg":
            continue
        if role == "assistant":
            for tc in (msg.get("tool_calls") or []):
                tc_id = tc.get("id") if isinstance(tc, dict) else None
                if tc_id:
                    valid_tc_ids.add(tc_id)
        kept.append(msg)

    # ② 孤儿 tool 丢弃（无对应 assistant tool_calls；缺 tool_call_id 同判孤儿）
    kept = [m for m in kept
            if not (m.get("role") == "tool" and m.get("tool_call_id") not in valid_tc_ids)]

    # 消息级副本——③/④ 会变异 entry（content 降级 / tool_calls 剥离），不就地改调用方对象
    out: list[dict] = [dict(m) for m in kept]

    # ③ 非 user list content 降级：tool/assistant list → str；system 原样保留（cache_control）
    for entry in out:
        if entry.get("role") in ("tool", "assistant") and isinstance(entry.get("content"), list):
            entry["content"] = _list_to_str(entry["content"])

    # ④ 悬空 tool_calls 按 tc 剥离（镜像 transform_history valid_tcs 先例）；全悬空 → 整消息降级纯文本
    tool_response_ids = {m.get("tool_call_id") for m in out if m.get("role") == "tool"}
    for entry in out:
        if entry.get("role") != "assistant" or not entry.get("tool_calls"):
            continue
        tcs = entry["tool_calls"]
        valid = [tc for tc in tcs
                 if isinstance(tc, dict) and tc.get("id") in tool_response_ids]
        if len(valid) == len(tcs):
            continue  # 无悬空，原样保留
        if valid:
            entry["tool_calls"] = valid
        else:
            del entry["tool_calls"]  # 全悬空 → 不设 tool_calls 键（不删消息）

    # ⑤ 顺序规整（最后 pass）：assistant(tool_calls) 后紧跟其全部 tool 响应
    return _reorder_tool_responses(out)
