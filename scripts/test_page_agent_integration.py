#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Page-Agent Server 集成测试"""
import sys
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

import time
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.tool_registry import get_registry
from agent.mcp_loader import load_mcp_tools


def test_tool_registration():
    """测试工具注册"""
    # 加载 MCP 工具到 registry
    print("Loading MCP tools...")
    load_mcp_tools()

    registry = get_registry()

    # 检查工具是否注册
    tools = [
        "page-agent-server/browser_navigate",
        "page-agent-server/browser_click",
        "page-agent-server/browser_input",
        "page-agent-server/browser_screenshot",
        "page-agent-server/execute_browser_task",
    ]

    for tool_name in tools:
        assert registry.get(tool_name) is not None, f"Tool {tool_name} not registered"
        print(f"✓ {tool_name} registered")


def test_browser_launch():
    """测试浏览器启动（需要手动确认）"""
    from niu_page_agent.browser_launcher import BrowserLauncher
    from niu_page_agent.ws_client import BrowserWSClient
    from niu_page_agent.protocol import NavigateCommand

    extension_path = Path(__file__).parent.parent / "mcp-servers/page-agent-server/extension"

    launcher = BrowserLauncher(extension_path=str(extension_path))
    launcher.launch()

    print("浏览器已启动，等待扩展连接...")
    time.sleep(5)

    # 尝试连接
    client = BrowserWSClient()
    client.connect(timeout=30)
    print("✓ WebSocket 连接成功")

    # 测试导航
    response = client.send_command(NavigateCommand(url="https://example.com"))
    print(f"✓ 导航测试: {response.data}")

    # 清理
    launcher.shutdown()
    print("✓ 测试完成")


if __name__ == "__main__":
    test_tool_registration()
    # test_browser_launch()  # 需要手动取消注释才能运行
