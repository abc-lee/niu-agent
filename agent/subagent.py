"""
SubAgent Module

子 Agent 调用机制。
"""

import os
import json
import yaml
from typing import Optional, Dict, Any, List, Tuple
from loguru import logger


def count_tokens_for_text(text: str) -> int:
    """
    计算文本的 token 数量（用于子 Agent prompt 分片判断）

    使用 litellm.token_counter，回退到字符数估算。

    Args:
        text: 纯文本字符串

    Returns:
        token 数量
    """
    if not text:
        return 0
    try:
        from litellm import token_counter
        return token_counter(model="gpt-4o", messages=[{"role": "user", "content": text}])
    except Exception:
        # 回退：约 2 字符/token（偏保守）
        return max(1, len(text) // 2)


def _read_context_window_tokens() -> int:
    """
    从 ~/.niu/preferences.json 读取上下文窗口大小

    Returns:
        上下文窗口 token 数（默认 200000）
    """
    try:
        import json as _json
        from pathlib import Path
        prefs_path = Path.home() / ".niu" / "preferences.json"
        if prefs_path.exists():
            prefs = _json.loads(prefs_path.read_text(encoding="utf-8"))
            return prefs.get("context", {}).get("contextWindowSize", 200000)
    except Exception as e:
        logger.warning(f"[SubAgent] Failed to read context window size from preferences: {e}")
    return 200000


def _run_agent_loop(
    agent_name: str,
    client,
    system_prompt: str,
    user_input: str,
    handler,
    tools_schema: list,
    max_turns: int = 20,
    initial_user_content: Optional[str] = None,
    context_window_tokens: int = 0,
    context_fifo_threshold: int = 0,
) -> Tuple[str, Any]:
    """
    执行 agent_runner_loop 并收集结果（提取自 call_subagent）

    Args:
        agent_name: 子 Agent 名称（用于日志）
        client: LLM 客户端
        system_prompt: 系统提示词
        user_input: 用户输入
        handler: NiuHandler 实例
        tools_schema: 工具 schema 列表
        max_turns: 最大轮次
        initial_user_content: 初始用户内容（如果不提供则使用 user_input）
        context_window_tokens: 上下文窗口 token 数（0 表示不检查）

    Returns:
        (result_text, return_value) 元组
    """
    from .generic.agent_loop import agent_runner_loop

    if initial_user_content is None:
        initial_user_content = user_input

    gen = agent_runner_loop(
        client=client,
        system_prompt=system_prompt,
        user_input=user_input,
        handler=handler,
        tools_schema=tools_schema,
        max_turns=max_turns,
        verbose=False,
        initial_user_content=initial_user_content,
        context_window_tokens=context_window_tokens,
        context_fifo_threshold=context_fifo_threshold,
    )

    result = ""
    return_value = None

    while True:
        try:
            chunk = next(gen)
            if isinstance(chunk, str):
                result += chunk
            else:
                content = getattr(chunk, "content", None)
                if content and isinstance(content, str):
                    result += content
                else:
                    logger.warning(f"[SubAgent] Non-string chunk from agent_runner_loop: {type(chunk).__name__}")
                    result += str(chunk)
        except StopIteration as e:
            return_value = e.value
            break

    return result, return_value


def _extract_result_from_return_value(return_value: Any) -> Optional[str]:
    """
    从 agent_runner_loop 的 return 值中提取结构化结果文本

    控制流 dict（如 CONTEXT_OVERFLOW, EXITED, MAX_TURNS_EXCEEDED, CURRENT_TASK_DONE）
    不应被序列化为结果文本，应返回 None 让调用者回退到 result_text。

    Args:
        return_value: agent_runner_loop 的 StopIteration.value

    Returns:
        提取的结果字符串，如果无法提取则返回 None
    """
    if return_value and isinstance(return_value, dict):
        # 控制流 dict 不应被序列化为结果 — 返回 None
        control_flow_results = {"CONTEXT_OVERFLOW", "EXITED", "MAX_TURNS_EXCEEDED", "CURRENT_TASK_DONE"}
        if return_value.get("result") in control_flow_results:
            return None

        if "data" in return_value and return_value["data"] is not None:
            data = return_value["data"]
            if isinstance(data, dict):
                return json.dumps(data, ensure_ascii=False)
            return json.dumps(data, ensure_ascii=False, default=str)
        return json.dumps(return_value, ensure_ascii=False)
    return None


def get_subagent_config(agent_name: str) -> Dict[str, Any]:
    """
    获取子 Agent 配置

    Args:
        agent_name: 子 Agent 名称（如 file-processor）

    Returns:
        配置字典，包含 mcpServers 等字段
    """
    prompt_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "config", "agents", f"{agent_name}.md"
    )

    if os.path.exists(prompt_path):
        with open(prompt_path, "r", encoding="utf-8") as f:
            content = f.read()
            # 解析 YAML front matter
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    try:
                        config = yaml.safe_load(parts[1])
                        if config:
                            return config
                    except Exception:
                        pass

    return {}


def get_subagent_prompt(agent_name: str) -> str:
    """获取子 Agent 提示词"""
    prompt_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "config", "agents", f"{agent_name}.md"
    )

    if os.path.exists(prompt_path):
        with open(prompt_path, "r", encoding="utf-8") as f:
            content = f.read()
            # 提取 body（--- 后面的内容）
            if "---" in content:
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    return parts[2].strip()
            return content

    return f"You are {agent_name} sub-agent. Complete the task efficiently."


def get_subagent_mcp_tools_schema(agent_name: str) -> List[Dict]:
    """
    获取子 Agent 的 MCP 工具 schema

    根据子 Agent 配置中的 mcpServers 过滤工具

    Args:
        agent_name: 子 Agent 名称

    Returns:
        MCP 工具 schema 列表（OpenAI格式）
    """
    from .tool_registry import get_registry

    config = get_subagent_config(agent_name)
    mcp_servers = config.get("mcpServers", [])

    if not mcp_servers:
        return []

    # 从 ToolRegistry 获取所有工具
    registry = get_registry()
    all_tools = registry.get_schemas()

    # 过滤出指定服务器的工具，并转换为OpenAI格式
    schema = []
    for tool in all_tools:
        tool_name = tool.get("name", "")
        # 工具名格式：server_name/tool_name
        if "/" in tool_name:
            server = tool_name.split("/")[0]
            if server in mcp_servers:
                # hidden 只对主 Agent 生效；子 Agent 由 mcpServers 白名单控制工具范围
                # 转换为OpenAI工具格式
                schema.append({
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "description": tool.get("description", ""),
                        "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
                    }
                })

    logger.info(f"[SubAgent] {agent_name}: Found {len(schema)} MCP tools for servers {mcp_servers}")
    return schema


def call_subagent(
    agent_name: str,
    task: str,
    llm_config: Dict[str, Any],
    mcp_client=None,
) -> str:
    """
    调用子 Agent

    子 Agent 是独立的临时 session，干完就消失。
    不需要动态注入，直接使用配置文件中的提示词和工具。

    Args:
        agent_name: 子 Agent 名称（如 file-processor）
        task: 任务描述
        llm_config: LLM 配置
        mcp_client: MCP 客户端

    Returns:
        子 Agent 执行结果
    """
    from .handler import NiuHandler

    # 1. 获取子 Agent 提示词（从配置文件）
    system_prompt = get_subagent_prompt(agent_name)

    # 1.5 从子 Agent 配置读取 temperature，覆盖到 llm_config
    agent_config = get_subagent_config(agent_name)
    if agent_config.get("temperature") is not None:
        llm_config = {**llm_config, "temperature": agent_config["temperature"]}

    # 2. 注入当前时间（重要！）
    from datetime import datetime
    now = datetime.now()
    system_prompt += f"\n\nCurrent Time: {now.strftime('%Y-%m-%d %H:%M:%S')}"

    # 3. 创建 LLM 客户端（统一使用 LiteLLM）
    from .runner import create_client
    client = create_client(llm_config)

    # 4. 创建 handler（禁用记忆检索，子 Agent 不需要）
    handler = NiuHandler(mcp_client=mcp_client)
    handler._disable_memory_recall = True
    # 重要约定：子 Agent 必须标记 _is_subagent = True
    # 否则子 Agent 的工具调用会触发 brain region reinforcement（应只由主 Agent 触发）
    # 新增子 Agent 时必须遵守此约定
    handler._is_subagent = True

    # 5. 获取基础工具 schema（排除子Agent调用工具，避免递归）
    from .runner import get_tools_schema

    tools_schema = get_tools_schema()
    # 移除 chat-with-* 工具，子Agent不能再调用子Agent
    tools_schema = [
        t for t in tools_schema
        if not t.get("function", {}).get("name", "").startswith("chat-with-")
    ]

    # 6. 获取子 Agent 的 MCP 工具 schema
    mcp_tools_schema = get_subagent_mcp_tools_schema(agent_name)
    if mcp_tools_schema:
        tools_schema = tools_schema + mcp_tools_schema
        logger.info(f"[SubAgent] {agent_name}: {len(tools_schema)} tools ({len(mcp_tools_schema)} MCP)")
    else:
        logger.warning(f"[SubAgent] {agent_name}: {len(tools_schema)} tools (0 MCP - WARNING: No MCP tools loaded!)")

    # 列出关键工具（调试）
    tool_names = [t.get("function", {}).get("name", "") for t in tools_schema]
    logger.debug(f"[SubAgent] {agent_name}: Tools = {tool_names}")

    # 7. 执行（单次，不分片）
    context_window_tokens = _read_context_window_tokens()
    # FIFO 截断阈值：75% 的上下文窗口，比溢出检测(85%)低，留出缓冲空间
    context_fifo_threshold = int(context_window_tokens * 0.75)

    result_text, return_value = _run_agent_loop(
        agent_name=agent_name,
        client=client,
        system_prompt=system_prompt,
        user_input=task,
        handler=handler,
        tools_schema=tools_schema,
        max_turns=20,
        initial_user_content=task,
        context_window_tokens=context_window_tokens,
        context_fifo_threshold=context_fifo_threshold,
    )

    # CONTEXT_OVERFLOW：返回结构化进度报告
    if return_value and isinstance(return_value, dict) and return_value.get("result") == "CONTEXT_OVERFLOW":
        data = return_value.get("data", {})
        overflow_report = {
            "overflow": True,
            "agent": agent_name,
            "turns_completed": data.get("turns_completed", 0),
            "tokens_used": data.get("tokens_used", 0),
            "tokens_limit": data.get("tokens_limit", 0),
            "partial_result": result_text[-8000:] if result_text else "",
        }
        logger.warning(f"[SubAgent] {agent_name}: Context overflow at {data.get('tokens_used', 0)} tokens")
        return json.dumps(overflow_report, ensure_ascii=False)

    # 优先从 return 值提取结构化结果
    extracted = _extract_result_from_return_value(return_value)
    if extracted is not None:
        return extracted

    return result_text
