import json, re, sys
from dataclasses import dataclass
from typing import Any, Optional


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
            yield f"未知工具: {tool_name}\n"
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
):
    # Build messages: system + history + current user
    messages = [{"role": "system", "content": system_prompt}]

    # Add conversation history if provided
    if history:
        for msg in history:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})

    # Add current user message
    messages.append({
        "role": "user",
        "content": initial_user_content if initial_user_content is not None else user_input,
    })

    # Debug info only - logging is done in ToolClient.chat where the real prompt is built
    print(f"[Debug] agent_runner_loop: {len(messages)} messages (history: {len(history) if history else 0})", file=sys.stderr, flush=True)

    turn = 0
    handler._done_hooks = []
    handler.max_turns = max_turns
    while turn < handler.max_turns:
        turn += 1
        if verbose:
            yield f"**LLM Running (Turn {turn}) ...**\n\n"
        if turn % 10 == 0:
            client.last_tools = ""  # 每10轮重置一次工具描述，避免上下文过大导致的模型性能下降
        response_gen = client.chat(messages=messages, tools=tools_schema)
        if verbose:
            response = yield from response_gen
            yield "\n\n"
        else:
            response = exhaust(response_gen)
            # 过滤掉 <tool_use> 标签，只返回纯文本
            content = response.content or ""
            content = re.sub(r"<tool_use>.*?</tool_use>", "", content, flags=re.DOTALL)
            yield content

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
                    print(f"[ERROR] Failed to parse tool arguments for {tc.function.name}: {e}", file=sys.stderr, flush=True)
                    print(f"[ERROR] Raw arguments: {tc.function.arguments}", file=sys.stderr, flush=True)
                    tool_calls.append({
                        "tool_name": tc.function.name,
                        "args": {},  # 回退为空参数
                        "id": tc.id,
                        "error": str(e),  # 记录错误信息
                    })

        # 添加assistant消息（如果有工具调用）
        if response.tool_calls:
            assistant_msg = {"role": "assistant", "tool_calls": []}
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

        tool_results = []
        next_prompts = set()
        should_exit = None
        for ii, tc in enumerate(tool_calls):
            tool_name, args, tid = tc["tool_name"], tc["args"], tc.get("id", "")
            if tool_name == "no_tool":
                pass
            else:
                showarg = get_pretty_json(args)
                if not verbose and len(showarg) > 200:
                    showarg = showarg[:200] + " ..."
                yield f"🛠️ **正在调用工具:** `{tool_name}`  📥**参数:**\n````text\n{showarg}\n````\n"
            handler.current_turn = turn
            gen = handler.dispatch(tool_name, args, response, index=ii)
            if verbose:
                yield "`````\n"
                outcome = yield from gen
                yield "`````\n"
            else:
                outcome = exhaust(gen)

            if outcome.should_exit:
                return {
                    "result": "EXITED",
                    "data": outcome.data,
                }  # should_exit is only used for immediate exit
            if not outcome.next_prompt:
                should_exit = {"result": "CURRENT_TASK_DONE", "data": outcome.data}
                break
            if outcome.next_prompt.startswith("未知工具"):
                client.last_tools = ""

            # 关键：Anthropic API 要求每个 tool_call 都有 tool_result
            # 即使 outcome.data 为 None，也必须添加空的 tool_result
            if outcome.data is not None:
                datastr = (
                    json.dumps(outcome.data, ensure_ascii=False, default=json_default)
                    if type(outcome.data) in [dict, list]
                    else str(outcome.data)
                )
                tool_results.append({"tool_use_id": tid, "content": datastr})
            else:
                # 添加空的 tool_result 以满足 Anthropic API 要求
                tool_results.append({"tool_use_id": tid, "content": ""})

            next_prompts.add(outcome.next_prompt)

        # 添加tool消息（工具结果）
        for tool_result in tool_results:
            messages.append({
                "role": "tool",
                "tool_call_id": tool_result["tool_use_id"],
                "content": tool_result["content"]
            })

        if len(next_prompts) == 0:
            if len(handler._done_hooks) == 0:
                return should_exit
            next_prompts.add(handler._done_hooks.pop(0))
        next_prompt = handler.next_prompt_patcher("\n".join(next_prompts), None, turn)

        # 如果 next_prompt 为空，说明任务完成，应该退出
        if not next_prompt or not next_prompt.strip():
            # 确保最后一轮的 decay 和保存执行
            if on_turn_end is not None:
                on_turn_end(messages, tools_schema, turn)
            return should_exit

        # 添加下一个user消息
        messages.append({"role": "user", "content": next_prompt})

        # 轮次级刷新回调：允许调用方在每轮结束后更新 system_prompt 和 tools_schema
        if on_turn_end is not None:
            tools_schema = on_turn_end(messages, tools_schema, turn)

    return {"result": "MAX_TURNS_EXCEEDED"}
