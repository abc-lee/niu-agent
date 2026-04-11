"""
测试 page-agent-mcp 完整功能

测试步骤：
1. 检查 Node.js 服务是否运行
2. 测试 HTTP 连接
3. 测试工具函数调用
"""

import sys
import json
from pathlib import Path

# 添加项目根目录到 sys.path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from agent.mcp_loader import load_mcp_tools


def test_nodejs_service():
    """测试 Node.js 服务是否运行"""
    print("=== 测试 1: Node.js 服务检查 ===\n")

    import urllib.request
    import urllib.error

    try:
        url = "http://localhost:38401/status"
        with urllib.request.urlopen(url, timeout=5) as response:
            data = response.read().decode("utf-8")
            print(f"✓ Node.js 服务正在运行")
            print(f"响应: {data}\n")
            return True
    except urllib.error.URLError as e:
        print(f"✗ Node.js 服务未运行")
        print(f"错误: {e}")
        print(f"\n请先启动服务:")
        print(f"  node mcp-servers/page-agent-mcp/src/index.js\n")
        return False
    except Exception as e:
        print(f"✗ 连接失败: {e}\n")
        return False


def test_tool_registry():
    """测试工具注册"""
    print("=== 测试 2: 工具注册检查 ===\n")

    registry = load_mcp_tools()

    tools = [
        "page-agent-mcp/execute_task",
        "page-agent-mcp/get_status",
        "page-agent-mcp/stop_task",
    ]

    for tool_name in tools:
        try:
            tool_fn = registry.get(tool_name)
            print(f"✓ {tool_name}")
        except KeyError:
            print(f"✗ {tool_name} (未注册)")

    print()


def test_get_status():
    """测试 get_status 工具"""
    print("=== 测试 3: get_status 工具调用 ===\n")

    registry = load_mcp_tools()

    try:
        get_status = registry.get("page-agent-mcp/get_status")
        result = get_status()

        print(f"调用结果: {result}\n")

        # 解析 JSON
        data = json.loads(result)
        if "connected" in data:
            print(f"✓ 工具调用成功")
            print(f"  - 扩展连接: {data.get('connected')}")
            print(f"  - 是否忙碌: {data.get('busy')}\n")
            return True
        else:
            print(f"✗ 返回格式异常\n")
            return False

    except Exception as e:
        print(f"✗ 调用失败: {e}\n")
        return False


def test_execute_task():
    """测试 execute_task 工具"""
    print("=== 测试 4: execute_task 工具调用 ===\n")

    registry = load_mcp_tools()

    try:
        execute_task = registry.get("page-agent-mcp/execute_task")

        # 简单的测试任务
        result = execute_task(task="打开百度首页")

        print(f"调用结果:\n{result}\n")

        if "Error" not in result:
            print(f"✓ 工具调用成功\n")
            return True
        else:
            print(f"⚠ 调用返回错误\n")
            return False

    except Exception as e:
        print(f"✗ 调用失败: {e}\n")
        return False


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("Page-Agent-MCP 完整功能测试")
    print("=" * 60 + "\n")

    # 测试 1: Node.js 服务
    if not test_nodejs_service():
        print("❌ 测试失败：Node.js 服务未启动")
        sys.exit(1)

    # 测试 2: 工具注册
    test_tool_registry()

    # 测试 3: get_status
    if not test_get_status():
        print("❌ 测试失败：get_status 调用失败")
        sys.exit(1)

    # 测试 4: execute_task（需要浏览器扩展连接）
    print("⚠ 跳过 execute_task 测试（需要浏览器扩展连接）")
    print("   如需测试，请确保 Chrome 扩展已安装并连接\n")

    print("=" * 60)
    print("✅ 所有测试通过！")
    print("=" * 60 + "\n")
