import json, re, sys
from dataclasses import dataclass
from typing import Any, Optional

from loguru import logger

from agent.output_validator import validate_references
from agent.subagent import _read_warning_threshold

_VALID_STREAM_TYPES = ("reply", "tool_marker", "system", "persist")


@dataclass
class StreamEvent:
    type: str
    content: str

    def __post_init__(self):
        if self.type not in _VALID_STREAM_TYPES:
            raise ValueError(f"Invalid StreamEvent type: {self.type!r}, must be one of {_VALID_STREAM_TYPES}")

    def __str__(self):
        return self.content

    def __add__(self, other):
        if isinstance(other, str):
            return self.content + other
        if isinstance(other, StreamEvent):
            return self.content + other.content
        return NotImplemented

    def __radd__(self, other):
        if isinstance(other, str):
            return other + self.content
        return NotImplemented




def count_messages_tokens(messages: list) -> int:
    """
    估算消息列表的 token 数量

    使用 litellm.token_counter，回退到字符数估算。
    """
    try:
        from litellm import token_counter
        return token_counter(model="gpt-4o", messages=messages)
    except Exception:
        total = 0
        for msg in messages:
            content = msg.get("content", "") or ""
            total += max(1, len(content) // 2) + 4
        return total


@dataclass
class StepOutcome:
    data: Any
    next_prompt: Optional[str] = None
    should_exit: bool = False


def try_call_generator(func, *args, **kwargs):
    ret = func(*args, **kwargs)
    if hasattr(ret, "__iter__") and not isinstance(ret, (str, bytes, dict, list)):
        ret = yield from ret
    return ret


class BaseHandler:
    def tool_before_callback(self, tool_name, args, response):
        pass

    def tool_after_callback(self, tool_name, args, response, ret):
        pass

    def next_prompt_patcher(self, next_prompt, outcome, turn):
        return next_prompt

    def dispatch(self, tool_name, args, response, index=0):
        method_name = f"do_{tool_name}"
        if hasattr(self, method_name):
            args["_index"] = index
            prer = yield from try_call_generator(
                self.tool_before_callback, tool_name, args, response
            )
            ret = yield from try_call_generator(getattr(self, method_name), args, response)
            _ = yield from try_call_generator(
                self.tool_after_callback, tool_name, args, response, ret
            )
            return ret
        elif tool_name == "bad_json":
            return StepOutcome(None, next_prompt=args.get("msg", "bad_json"), should_exit=False)
        else:
            yield StreamEvent("system", f"未知工具: {tool_name}\n")
            return StepOutcome(None, next_prompt=f"未知工具 {tool_name}", should_exit=False)


def json_default(o):
    if isinstance(o, set):
        return list(o)
    return str(o)


def exhaust(g):
    try:
        while True:
            next(g)
    except StopIteration as e:
        return e.value


def get_pretty_json(data):
    if isinstance(data, dict) and "script" in data:
        data = data.copy()
        data["script"] = data["script"].replace("; ", ";\n  ")
    return json.dumps(data, indent=2, ensure_ascii=False).replace("\\n", "\n")


def _fifo_prune(messages, target_tokens):
    """FIFO 裁剪：从 messages[2] 开始删除，直到 token 数低于 target。
    assistant+tool_calls 消息会连带后续 tool 消息一起删除。
    返回删除的消息数。
    """
    if len(messages) <= 2:
        return 0
    removed = 0
    current_tokens = count_messages_tokens(messages)
    while len(messages) > 2 and current_tokens > target_tokens:
        first = messages[2]
        messages.pop(2)
        removed += 1
        if first.get("role") == "assistant" and first.get("tool_calls"):
            while len(messages) > 2 and messages[2].get("role") == "tool":
                messages.pop(2)
                removed += 1
        current_tokens = count_messages_tokens(messages)
    return removed


def agent_runner_loop(
    client,
    system_prompt,
    user_input,
    handler,
    tools_schema,
    max_turns=40,
    verbose=True,
    initial_user_content=None,
    history=None,  # Optional: list of {"role": "user/assistant", "content": str}
    on_turn_end=None,  # Optional: callback(messages, tools_schema, turn) -> tools_schema
    context_window_tokens=0,  # 0 means no limit check (backward compatible)
    context_fifo_threshold=0,  # 0 means no FIFO truncation; >0 means max token budget for sub-agents
    context_target_threshold=0,  # FIFO 裁剪目标 token 量
    on_context_high_usage=None,  # 主Agent超阈值回调；None=子Agent走FIFO
    enable_supplement=True,  # False for sub-agents to prevent stealing main agent's supplements
):
    from agent.runner import is_stop_requested, clear_stop, drain_supplement

    # Build messages: system + history + current user
    messages = [{"role": "system", "content": system_prompt}]

    # Add conversation history if provided
    if history:
        for msg in history:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role in ("user", "assistant") and (content or msg.get("tool_calls")):
                entry = {"role": role, "content": content}
                # 还原 tool_calls（assistant 消息可能携带工具调用）
                if msg.get("tool_calls"):
                    entry["tool_calls"] = msg["tool_calls"]
                messages.append(entry)
            elif role == "tool" and msg.get("tool_call_id") and content is not None:
                # tool 消息必须有 tool_call_id 和 content，否则 OpenAI API 返回 400
                entry = {"role": role, "content": content, "tool_call_id": msg["tool_call_id"]}
                messages.append(entry)

    # Add current user message
    messages.append({
        "role": "user",
        "content": initial_user_content if initial_user_content is not None else user_input,
    })

    # Debug info only - logging is done in ToolClient.chat where the real prompt is built
    logger.info(f"[Debug] agent_runner_loop: {len(messages)} messages (history: {len(history) if history else 0})")

    turn = 0
    last_prompt_tokens = 0
    _compress_cooldown = False  # 回调冷却：同一轮 agent_runner_loop 只触发一次压缩
    handler._done_hooks = []
    handler.max_turns = max_turns
    # V4: 通知前端进入忙碌状态
    yield StreamEvent("system", "chat_busy")

    _harness_fail_count = 0
    _MAX_HARNESS_RETRIES = 3
    warning_threshold = _read_warning_threshold()

    while turn < handler.max_turns:
        turn += 1
        # --- Stop flag check ---
        if is_stop_requested():
            logger.info("[AgentLoop] Stop requested, exiting loop")
            clear_stop()
            yield StreamEvent("system", "chat_idle")
            return {"result": "STOPPED", "messages": messages}
        # === 上下文使用率检测（prompt_tokens 驱动）===
        if last_prompt_tokens > 0 and context_window_tokens > 0 and not _compress_cooldown:
            usage_ratio = last_prompt_tokens / context_window_tokens
            if usage_ratio > warning_threshold:
                if on_context_high_usage:
                    # 主 Agent：调回调执行压缩，循环不退出
                    logger.info(f"[Context] Proactive compress: {last_prompt_tokens}/{context_window_tokens} tokens "
                                f"({usage_ratio:.1%} > {warning_threshold:.0%})")
                    on_context_high_usage(messages, last_prompt_tokens, context_window_tokens)
                    # 回调内部已完成压缩并原地修改 messages（messages[:] = ...）
                    last_prompt_tokens = 0  # 重置，下轮重新获取
                    _compress_cooldown = True  # 冷却：本次 agent_runner_loop 不再触发压缩
                else:
                    # 子 Agent：FIFO 裁剪到 target 阈值
                    target_tokens = context_target_threshold if context_target_threshold > 0 else int(context_window_tokens * 0.50)
                    removed = _fifo_prune(messages, target_tokens)
                    if removed > 0:
                        logger.info(f"[FIFO] Proactive pruning: {last_prompt_tokens}/{context_window_tokens} tokens "
                                    f"({usage_ratio:.1%} > {warning_threshold:.0%}), removed {removed} messages, "
                                    f"now ~{count_messages_tokens(messages)} tokens (target {target_tokens})")
            # 旧 FIFO 回退：只在首轮（last_prompt_tokens==0）时执行
        if context_fifo_threshold > 0 and len(messages) > 2 and last_prompt_tokens == 0 and not _compress_cooldown:
            removed = _fifo_prune(messages, context_fifo_threshold)
            if removed > 0:
                logger.info(f"[FIFO] Fallback truncation: removed {removed} oldest messages, "
                            f"tokens {count_messages_tokens(messages)}/{context_fifo_threshold}")
        if verbose:
            yield StreamEvent("system", f"**LLM Running (Turn {turn}) ...**\n\n")
        if turn % 10 == 0:
            client.last_tools = ""  # 每10轮重置一次工具描述，避免上下文过大导致的模型性能下降
        response_gen = client.chat(messages=messages, tools=tools_schema)
        if verbose:
            response = yield from response_gen
            yield StreamEvent("system", "\n\n")
        else:
            response = exhaust(response_gen)
            # 过滤掉 <tool_use> 标签，只返回纯文本
            content = response.content or ""
            content = re.sub(r"<tool_use>.*?</tool_use>", "", content, flags=re.DOTALL)
            # Harness 验证：仅在 LLM 不调工具直接回复用户时验证
            # 条件 not response.tool_calls 精确区分最终回复 vs 中间工具调用
            if not response.tool_calls:
                validation = validate_references(content)
                if not validation.is_valid and _harness_fail_count < _MAX_HARNESS_RETRIES:
                    _harness_fail_count += 1
                    feedback = validation.format_feedback()
                    messages.append({"role": "assistant", "content": content})
                    messages.append({"role": "user", "content": feedback})
                    continue  # 回到 while 循环，让 LLM 修正
                _harness_fail_count = 0

            yield StreamEvent("reply", content)

        # 统一提取 prompt_tokens（verbose/else 分支共用）
        if hasattr(response, 'usage') and response.usage:
            u = response.usage
            _pt = u.get('prompt_tokens', 0) if isinstance(u, dict) else getattr(u, 'prompt_tokens', 0)
            if isinstance(_pt, (int, float)):
                last_prompt_tokens = int(_pt)
                logger.info(f"[Context] prompt_tokens={last_prompt_tokens}, context_window={context_window_tokens}")
                # 提取后立即检测：如果超阈值，在当前轮就触发回调/FIFO
                # （无工具调用时循环会退出，下轮顶部检测不会执行，所以此处必须检测）
                if context_window_tokens > 0 and not _compress_cooldown:
                    usage_ratio = last_prompt_tokens / context_window_tokens
                    if usage_ratio > warning_threshold:
                        if on_context_high_usage:
                            logger.info(f"[Context] Proactive compress: {last_prompt_tokens}/{context_window_tokens} tokens "
                                        f"({usage_ratio:.1%} > {warning_threshold:.0%})")
                            on_context_high_usage(messages, last_prompt_tokens, context_window_tokens)
                            last_prompt_tokens = 0
                            _compress_cooldown = True
                        else:
                            target_tokens = context_target_threshold if context_target_threshold > 0 else int(context_window_tokens * 0.50)
                            removed = _fifo_prune(messages, target_tokens)
                            if removed > 0:
                                logger.info(f"[FIFO] Proactive pruning: removed {removed} messages")
        else:
            logger.debug(f"[Context] No usage in response: hasattr={hasattr(response, 'usage')}, usage={getattr(response, 'usage', 'N/A')}")

        # 检测 LLM 返回的 context_length_exceeded 标记（覆盖 verbose=True 和 verbose=False）
        if hasattr(response, 'context_overflow') and response.context_overflow:
            logger.warning(f"[Overflow] LLM API returned context_length_exceeded, triggering CONTEXT_OVERFLOW")
            if on_turn_end is not None:
                on_turn_end(messages, tools_schema, turn)
            clear_stop()
            yield StreamEvent("system", "chat_idle")
            return {
                "result": "CONTEXT_OVERFLOW",
                "data": {
                    "overflow": True,
                    "turns_completed": turn - 1,
                    "tokens_used": count_messages_tokens(messages),
                    "tokens_limit": context_window_tokens,
                },
                "messages": messages,
            }

        # 如果在 LLM 流式传输期间请求停止，跳过部分 tool_calls 处理
        if is_stop_requested():
            logger.info("[AgentLoop] Stop requested after LLM stream, skipping tool calls")
            clear_stop()
            yield StreamEvent("system", "chat_idle")
            return {"result": "STOPPED", "messages": messages}

        if not response.tool_calls:
            tool_calls = [{"tool_name": "no_tool", "args": {}}]
        else:
            # P0-6: 添加 JSON 解析异常处理
            tool_calls = []
            for tc in response.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                    tool_calls.append({
                        "tool_name": tc.function.name,
                        "args": args,
                        "id": tc.id,
                    })
                except json.JSONDecodeError as e:
                    # 记录错误并使用空参数继续执行
                    logger.error(f"[ERROR] Failed to parse tool arguments for {tc.function.name}: {e}")
                    logger.error(f"[ERROR] Raw arguments: {tc.function.arguments}")
                    tool_calls.append({
                        "tool_name": tc.function.name,
                        "args": {},  # 回退为空参数
                        "id": tc.id,
                        "error": str(e),  # 记录错误信息
                    })

        # 添加assistant消息（如果有工具调用）
        if response.tool_calls:
            assistant_msg = {"role": "assistant", "content": response.content or "", "tool_calls": []}
            for tc in response.tool_calls:
                assistant_msg["tool_calls"].append({
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments
                    }
                })
            messages.append(assistant_msg)
            # V4: yield persist事件，逐条持久化assistant(tool_calls)消息
            yield StreamEvent("persist", json.dumps(assistant_msg, ensure_ascii=False))

        tool_results = []
        next_prompts = set()
        should_exit = None
        # 注入当前消息列表到 handler，使子Agent能获取主Agent的对话历史
        # 注意：此时 messages 包含本轮的 assistant(tool_calls) 但不含 tool 结果
        handler._current_messages = messages
        for ii, tc in enumerate(tool_calls):
            tool_name, args, tid = tc["tool_name"], tc["args"], tc.get("id", "")
            if tool_name == "no_tool":
                continue
            elif verbose:
                showarg = get_pretty_json(args)
                yield StreamEvent("tool_marker", f"🛠️ **正在调用工具:** `{tool_name}`  📥**参数:**\n````text\n{showarg}\n````\n")
            handler.current_turn = turn
            # --- Stop flag check before tool dispatch ---
            if is_stop_requested():
                logger.info("[AgentLoop] Stop requested, skipping remaining tools")
                clear_stop()
                yield StreamEvent("system", "chat_idle")
                return {"result": "STOPPED", "messages": messages}
            gen = handler.dispatch(tool_name, args, response, index=ii)
            if verbose:
                yield StreamEvent("tool_marker", "`````\n")
                outcome = yield from gen
                yield StreamEvent("tool_marker", "`````\n")
            else:
                outcome = exhaust(gen)

            if outcome.should_exit:
                # should_exit路径：补齐当前tool_result到tool_results列表
                if tid:
                    if outcome.data is not None:
                        datastr = (
                            json.dumps(outcome.data, ensure_ascii=False, default=json_default)
                            if type(outcome.data) in [dict, list]
                            else str(outcome.data)
                        )
                        tool_results.append({"tool_use_id": tid, "content": datastr})
                    else:
                        tool_results.append({"tool_use_id": tid, "content": ""})
                # 添加tool消息到messages
                for tool_result in tool_results:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_result["tool_use_id"],
                        "content": tool_result["content"]
                    })
                # V4: yield每条tool结果的persist事件
                for tool_result in tool_results:
                    tool_msg = {
                        "role": "tool",
                        "tool_call_id": tool_result["tool_use_id"],
                        "content": tool_result["content"]
                    }
                    yield StreamEvent("persist", json.dumps(tool_msg, ensure_ascii=False))
                if on_turn_end is not None:
                    on_turn_end(messages, tools_schema, turn)
                clear_stop()
                yield StreamEvent("system", "chat_idle")
                return {
                    "result": "EXITED",
                    "data": outcome.data,
                    "messages": messages,
                }  # should_exit is only used for immediate exit
            if outcome.next_prompt.startswith("未知工具") or outcome.next_prompt.startswith("Unknown tool"):
                client.last_tools = ""

            # 关键：Anthropic API 要求每个 tool_call 都有 tool_result
            # 即使 outcome.data 为 None，也必须添加空的 tool_result
            # 但 no_tool 场景 tid 为空字符串，不应产生孤立的 tool 消息
            if tid:
                if outcome.data is not None:
                    datastr = (
                        json.dumps(outcome.data, ensure_ascii=False, default=json_default)
                        if type(outcome.data) in [dict, list]
                        else str(outcome.data)
                    )
                    tool_results.append({"tool_use_id": tid, "content": datastr})
                else:
                    tool_results.append({"tool_use_id": tid, "content": ""})

            next_prompts.add(outcome.next_prompt)

        # 添加tool消息（工具结果）
        for tool_result in tool_results:
            messages.append({
                "role": "tool",
                "tool_call_id": tool_result["tool_use_id"],
                "content": tool_result["content"]
            })
        # V4: yield每条tool结果的persist事件
        for tool_result in tool_results:
            tool_msg = {
                "role": "tool",
                "tool_call_id": tool_result["tool_use_id"],
                "content": tool_result["content"]
            }
            yield StreamEvent("persist", json.dumps(tool_msg, ensure_ascii=False))

        if len(next_prompts) == 0:
            if len(handler._done_hooks) == 0:
                # 纯文本回复：也要执行衰减
                if on_turn_end is not None:
                    on_turn_end(messages, tools_schema, turn)
                # V4: 纯文本回复yield persist事件（从response.content构造，不从messages[-1]获取）
                if response.content and not response.tool_calls:
                    pure_text_msg = {"role": "assistant", "content": response.content}
                    yield StreamEvent("persist", json.dumps(pure_text_msg, ensure_ascii=False))
                # V4: 通知前端进入空闲状态
                clear_stop()
                yield StreamEvent("system", "chat_idle")
                if isinstance(should_exit, dict):
                    should_exit["messages"] = messages
                    return should_exit
                # should_exit 为 None 时（无工具调用），返回标准格式
                return {"result": "CURRENT_TASK_DONE", "data": None, "messages": messages}
            next_prompts.add(handler._done_hooks.pop(0))
        next_prompt = handler.next_prompt_patcher("\n".join(next_prompts), None, turn)

        # 退出逻辑：LLM 无工具调用时退出（纯文本回复 = 任务完成或等待用户输入）
        if not response.tool_calls:
            if on_turn_end is not None:
                on_turn_end(messages, tools_schema, turn)
            clear_stop()
            yield StreamEvent("system", "chat_idle")
            if isinstance(should_exit, dict):
                should_exit["messages"] = messages
                return should_exit
            return {"result": "CURRENT_TASK_DONE", "data": None, "messages": messages}

        # --- 见缝插针：读取用户在 Agent 运行期间发送的补充消息 ---
        supplement = drain_supplement() if enable_supplement else None

        # 警告注入：只在有工具调用时才有意义（LLM 还在工作，可能需要调整策略）
        # 补充消息插在 next_prompt 前面，当前任务作为最后一条，LLM 优先处理
        if supplement or (next_prompt and next_prompt.strip()):
            combined = ""
            if supplement:
                combined = supplement
            if next_prompt and next_prompt.strip():
                combined = combined + "\n" + next_prompt if combined else next_prompt
            messages.append({"role": "user", "content": combined})
            if supplement:
                logger.info(f"[AgentLoop] Supplement inserted before next_prompt: {supplement[:80]}...")

        # 轮次级刷新回调：允许调用方在每轮结束后更新 system_prompt 和 tools_schema
        if on_turn_end is not None:
            tools_schema = on_turn_end(messages, tools_schema, turn)

    # MAX_TURNS_EXCEEDED 退出时也要执行衰减
    if on_turn_end is not None:
        on_turn_end(messages, tools_schema, turn)
    # V4: 通知前端进入空闲状态
    clear_stop()
    yield StreamEvent("system", "chat_idle")
    return {"result": "MAX_TURNS_EXCEEDED", "messages": messages}
