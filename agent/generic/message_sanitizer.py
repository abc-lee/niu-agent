"""发送层 LLM API 合规化（plan 2026-09-10-tool-message-order-fix §2 D1/D2）。

单一职责：LiteLLMSession.chat() 入口把调用方 messages 规整为 OpenAI 规范合规的
**副本**并在 chat() 局部重绑——调用方列表零变化（长度不变式 / 不回流 DB/档/UI），
raw_http 日志 = 真 wire 体。

固定执行序（丢弃在前、展开居中、规整最后）：
  ① subagent_msg 丢弃（@ 消息仅供前端展示）
  ② 孤儿 tool 丢弃（无对应 assistant tool_calls）——先于图片合规化，防带图孤儿
     先生成合成 user、再被丢弃，留下引用不存在工具结果的合成消息
  ③ 图片合规化：user 展开 / assistant 不展开（content 恒 str）/ tool 图段 →
     紧随其后合成 user 消息（零图段不发）
  ④ 非 user list content 降级：tool/assistant list → str（text 段拼接、
     image_url → [图片已省略]）；system 原样保留（其 list 承载 cache_control，
     压平会静默废掉 prompt caching）
  ⑤ 悬空 tool_calls 按 tc 剥离（镜像 transform_history valid_tcs 先例）；
     全部悬空才整消息降级为纯文本（不设 tool_calls 键、不删消息）——避免部分
     悬空整消息降级在步骤②之后制造新孤儿
  ⑥ 顺序规整（最后 pass）：每个 assistant(tool_calls) 后紧跟其全部 tool 响应；
     夹入消息（user 回答 / 合成 user）顺延到 tool 块之后

幂等语义：无图输入恒等幂等（二次净化逐字节相同）；含图输入为每请求确定性补全
（同输入同输出 → 前缀缓存友好）。纯函数 + 只读文件访问（经 expand_image_markers），
叶子层模块防循环 import。
"""

from __future__ import annotations

from agent.image_channel import expand_image_markers

# tool 图段合成 user 消息的前置说明文本（业界 fallback = synthetic user message，agno#7661）
_SYNTH_IMAGE_USER_TEXT = "（以下是上一条工具结果中的图片）"


def _image_blocks_of(content) -> list[dict]:
    """提取 content 中的 image_url 段（str 先经 expand_image_markers 展开；list 直读）。

    文件缺失/不可读 → 展开产物无图段（仅文本警示）→ 返回 []（零图段不发合成消息）。
    """
    if isinstance(content, str):
        parts = expand_image_markers(content, has_vision=True)
        if not isinstance(parts, list):
            return []
    elif isinstance(content, list):
        parts = content
    else:
        return []
    return [seg for seg in parts if isinstance(seg, dict) and seg.get("type") == "image_url"]


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


def sanitize_llm_messages(messages, has_vision: bool = False):
    """发送层合规化（纯函数）：返回 OpenAI 规范合规的消息**副本**。

    - 不就地改调用方列表/消息对象（长度不变式保持；产物不回流 DB/档/UI）
    - 消息集变更：丢弃「孤儿 tool」「subagent_msg」；新增「合成 user 图消息」
      （零图段不新增）；其余一律保留（重排/降级）
    - has_vision=False → 不做任何图片展开（fail-closed）

    Args:
        messages: LLM 上下文消息列表（transform_history / agent_runner_loop 产物形态）
        has_vision: 会话视觉能力（派发层经 llm_config.vision_enabled 传入的 bool）

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

    # ② 孤儿 tool 丢弃（无对应 assistant tool_calls；缺 tool_call_id 同判孤儿）——先于图片合规化
    kept = [m for m in kept
            if not (m.get("role") == "tool" and m.get("tool_call_id") not in valid_tc_ids)]

    # ③ 图片合规化：user 展开 / assistant 不展开 / tool 图段 → 紧随合成 user（零图段不发）
    out: list[dict] = []
    for msg in kept:
        role = msg.get("role", "user")
        entry = dict(msg)  # 消息级副本——后续只整体替换键值，不就地改调用方对象
        if role == "user" and isinstance(entry.get("content"), str):
            # user：展开图标记（规范允许位）；非 str（存量 list）原样保留
            entry["content"] = expand_image_markers(entry["content"], has_vision)
        elif role == "tool":
            # tool：不展开、content 恒 str；图段由紧随其后的合成 user 消息承载
            out.append(entry)
            if has_vision:
                blocks = _image_blocks_of(entry.get("content"))
                if blocks:
                    out.append({
                        "role": "user",
                        "content": [
                            {"type": "text", "text": _SYNTH_IMAGE_USER_TEXT},
                            *blocks,
                        ],
                    })
            continue
        # assistant / system / 其他角色：不展开（assistant 保留 str 文本标记）
        out.append(entry)

    # ④ 非 user list content 降级：tool/assistant list → str；system 原样保留（cache_control）
    for entry in out:
        if entry.get("role") in ("tool", "assistant") and isinstance(entry.get("content"), list):
            entry["content"] = _list_to_str(entry["content"])

    # ⑤ 悬空 tool_calls 按 tc 剥离（镜像 transform_history valid_tcs 先例）；全悬空 → 整消息降级纯文本
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

    # ⑥ 顺序规整（最后 pass）：assistant(tool_calls) 后紧跟其全部 tool 响应
    return _reorder_tool_responses(out)
