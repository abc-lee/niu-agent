"""
测试 Page-Agent MCP 工具集成

验证：
1. ToolRegistry 是否成功注册 page-agent-mcp 的 3 个工具
2. 工具函数是否可以正常调用（需要先启动 page-agent-mcp Node.js 服务）
"""

import sys
from pathlib import Path

# 添加项目根目录到 sys.path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from agent.mcp_loader import load_mcp_tools


def test_tool_registration():
    """测试工具注册"""
    print("=== 测试 Page-Agent MCP 工具注册 ===\n")

    # 加载所有 MCP 工具
    registry = load_mcp_tools()

    # 检查 page-agent-mcp 工具是否存在
    expected_tools = [
        "page-agent-mcp/execute_task",
        "page-agent-mcp/get_status",
        "page-agent-mcp/stop_task",
    ]

    print("预期工具:")
    for tool in expected_tools:
        print(f"  - {tool}")

    print("\n实际注册的工具:")
    for tool_name in expected_tools:
        try:
            tool_fn = registry.get(tool_name)
            print(f"  ✓ {tool_name}")
        except KeyError:
            print(f"  ✗ {tool_name} (未注册)")

    # 获取所有工具 schema
    schemas = registry.get_schemas()
    page_agent_tools = [s for s in schemas if s["name"].startswith("page-agent-mcp/")]

    print(f"\n总计注册的 page-agent-mcp 工具: {len(page_agent_tools)}")
    print(f"总计注册的 MCP 工具: {len(schemas)}")

    if len(page_agent_tools) == 3:
        print("\n✓ Page-Agent MCP 工具注册成功！")
    else:
        print("\n✗ Page-Agent MCP 工具注册失败！")

    print("\n" + "=" * 50)


def test_tool_call():
    """测试工具调用（需要先启动 Node.js 服务）"""
    print("\n=== 测试 Page-Agent MCP 工具调用 ===\n")

    registry = load_mcp_tools()

    try:
        # 测试 get_status（不需要浏览器连接）
        get_status = registry.get("page-agent-mcp/get_status")
        print("调用 get_status...")
        result = get_status()
        print(f"结果: {result}")

        if "connected" in result:
            print("\n✓ 工具调用成功！")
        else:
            print("\n⚠ 工具调用返回异常结果")

    except Exception as e:
        print(f"\n✗ 工具调用失败: {e}")
        print("\n提示: 请先启动 page-agent-mcp Node.js 服务:")
        print("  node mcp-servers/page-agent-mcp/src/index.js")

    print("\n" + "=" * 50)


if __name__ == "__main__":
    test_tool_registration()

    # 如果要测试工具调用，取消下面的注释
    # test_tool_call()
