"""
SubAgent Module

子 Agent 调用机制。
"""

import os
import json
import yaml
from typing import Optional, Dict, Any, List


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
        MCP 工具 schema 列表
    """
    from .mcp_client import get_mcp_tools_for_servers

    config = get_subagent_config(agent_name)
    mcp_servers = config.get("mcpServers", [])

    if not mcp_servers:
        return []

    # 获取指定服务器的工具
    tools = get_mcp_tools_for_servers(mcp_servers)

    # 转换为 OpenAI 工具格式
    schema = []
    for tool in tools:
        schema.append(
            {
                "type": "function",
                "function": {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
                },
            }
        )

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
    from .generic.agent_loop import agent_runner_loop
    from .handler import NiuHandler

    # 1. 获取子 Agent 提示词（从配置文件）
    system_prompt = get_subagent_prompt(agent_name)

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
        print(f"[SubAgent] {agent_name}: {len(tools_schema)} tools ({len(mcp_tools_schema)} MCP)")
    else:
        print(f"[SubAgent] {agent_name}: {len(tools_schema)} tools (0 MCP - WARNING: No MCP tools loaded!)")

    # 列出关键工具（调试）
    tool_names = [t.get("function", {}).get("name", "") for t in tools_schema]
    print(f"[SubAgent] {agent_name}: Tools = {tool_names}")

    # 7. 执行
    gen = agent_runner_loop(
        client=client,
        system_prompt=system_prompt,
        user_input=task,
        handler=handler,
        tools_schema=tools_schema,
        max_turns=20,
        verbose=False,  # 改为 False，避免输出调试信息干扰结果
        initial_user_content=task,
    )

    # 8. 收集结果（包括 return 值）
    result = ""
    return_value = None

    # 改用 while + next 消费生成器，以捕获 StopIteration
    # 重要：for 循环会自动捕获 StopIteration，导致无法获取生成器的 return 值
    while True:
        try:
            chunk = next(gen)
            result += chunk
        except StopIteration as e:
            # 生成器的 return 值在 StopIteration.value 中
            return_value = e.value
            break

    # 优先使用 return 值（包含结构化数据）
    if return_value and isinstance(return_value, dict):
        # 如果 return_value 中有 data 字段，提取它作为结果
        if "data" in return_value and return_value["data"] is not None:
            data = return_value["data"]
            if isinstance(data, dict):
                return json.dumps(data, ensure_ascii=False)
            return str(data)
        return json.dumps(return_value, ensure_ascii=False)

    return result
