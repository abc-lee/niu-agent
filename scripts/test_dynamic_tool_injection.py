"""
测试动态工具注入
验证主Agent基础工具数量是否正确
"""

import sys
from pathlib import Path

# 添加项目根目录到 sys.path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from agent.runner import BASE_MCP_TOOLS, get_tools_schema


def test_base_mcp_tools_list():
    """测试基础MCP工具列表"""
    print("=== 测试基础MCP工具列表 ===")
    print(f"预期工具数量: 11")
    print(f"实际工具数量: {len(BASE_MCP_TOOLS)}")

    # 验证工具列表
    expected_tools = [
        "memory-server/remember",
        "memory-server/recall",
        "memory-server/update_memory",
        "memory-server/get_memory_stats",
        "memory-server/cleanup_memories",
        "memory-server/link_memories",
        "vector-store/add_document",
        "vector-store/search_documents",
        "vector-store/get_document",
        "vector-store/delete_document",
        "vector-store/list_documents",
    ]

    if BASE_MCP_TOOLS == expected_tools:
        print("✓ 工具列表匹配")
    else:
        print("✗ 工具列表不匹配")
        print(f"预期: {expected_tools}")
        print(f"实际: {BASE_MCP_TOOLS}")

    print()


def test_base_tools_schema():
    """测试内置工具Schema"""
    print("=== 测试内置工具Schema ===")
    tools = get_tools_schema()
    print(f"内置工具数量: {len(tools)}")

    # 预期9个内置工具 + 3个子Agent工具 = 12个
    expected_builtin = [
        "code_run",
        "file_read", "file_patch", "file_write",
        "update_working_checkpoint", "start_long_term_update",
        "chat-with-file-processor",
        "chat-with-event-manager",
        "chat-with-context-manager",
    ]

    tool_names = [t.get("function", {}).get("name") for t in tools]
    print(f"工具列表: {tool_names}")

    if set(expected_builtin).issubset(set(tool_names)):
        print("✓ 内置工具匹配")
    else:
        missing = set(expected_builtin) - set(tool_names)
        print(f"✗ 缺少工具: {missing}")

    print()


def test_tool_injection():
    """测试工具注入逻辑"""
    print("=== 测试工具注入逻辑 ===")

    # 创建一个模拟的 NiuRunner
    from agent.mcp_loader import load_mcp_tools

    registry = load_mcp_tools()
    all_tools = registry.get_schemas()
    print(f"所有MCP工具数量: {len(all_tools)}")

    # 模拟 set_mcp_tools_schema
    schema = []
    for tool in all_tools:
        schema.append(
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get(
                        "input_schema", {"type": "object", "properties": {}}
                    ),
                },
            }
        )

    # 模拟 _get_tool_schema_by_name
    def get_tool_schema_by_name(tool_name: str):
        for tool in schema:
            if tool.get("function", {}).get("name") == tool_name:
                return tool
        return None

    # 模拟工具注入
    tools_schema = get_tools_schema()
    for tool_name in BASE_MCP_TOOLS:
        tool_schema = get_tool_schema_by_name(tool_name)
        if tool_schema:
            tools_schema.append(tool_schema)

    # 统计
    base_mcp_count = len([t for t in tools_schema if t.get("function", {}).get("name") in BASE_MCP_TOOLS])
    print(f"内置工具: {len(get_tools_schema())}")
    print(f"基础MCP工具: {base_mcp_count}")
    print(f"总工具数: {len(tools_schema)}")
    print(f"预期总工具数: 23 (9 内置 + 14 基础MCP)")

    if len(tools_schema) == 23:
        print("✓ 工具注入正确")
    else:
        print("✗ 工具注入不正确")

    print()


if __name__ == "__main__":
    test_base_mcp_tools_list()
    test_base_tools_schema()
    test_tool_injection()

    print("=== 所有测试完成 ===")
