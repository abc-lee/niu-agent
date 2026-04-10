"""
测试触发机制改进
验证基于消息历史的上下文提取和工具检索
"""

import sys
from pathlib import Path

# 添加项目根目录到 sys.path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from agent.runner import NiuRunner


def test_context_extraction():
    """测试从消息历史中提取上下文"""
    print("=== 测试上下文提取 ===\n")

    # 创建一个临时的 NiuRunner 实例（不需要完整配置）
    from agent.mcp_loader import load_mcp_tools

    # 加载MCP工具
    registry = load_mcp_tools()

    # 创建 NiuRunner
    runner = NiuRunner(
        llm_config={"apikey": "test", "model": "test"},
        mcp_client=None
    )

    # 设置MCP工具Schema
    all_tools = registry.get_schemas()
    runner.set_mcp_tools_schema(all_tools)

    # 测试场景1：无历史消息
    print("场景1：无历史消息")
    context = runner._extract_context_from_history(None, "入库照片")
    print(f"  上下文: {context}")
    assert context == "入库照片", "无历史消息时应只返回用户输入（无前缀）"
    print("  ✓ 通过\n")

    # 测试场景2：有历史消息
    print("场景2：有历史消息")
    history = [
        {"role": "user", "content": "入库照片"},
        {"role": "assistant", "content": "好的，请提供照片路径"},
        {"role": "user", "content": "E:/test.jpg"},
    ]
    context = runner._extract_context_from_history(history, "是的")
    print(f"  上下文长度: {len(context)}")
    print(f"  上下文: {context}")
    assert "入库照片" in context, "上下文应包含历史消息"
    assert "E:/test.jpg" in context, "上下文应包含历史消息"
    assert "是的" in context, "上下文应包含当前用户输入"
    print("  ✓ 通过\n")

    # 测试场景3：历史消息过多（超过5条）
    print("场景3：历史消息过多（只提取最近5条）")
    history = [
        {"role": "user", "content": f"消息{i}"}
        for i in range(10)
    ]
    context = runner._extract_context_from_history(history, "当前输入")
    print(f"  上下文: {context}")
    # 应该只包含最近5条 + 当前输入
    assert "消息5" in context, "应包含最近5条消息"
    assert "消息6" in context, "应包含最近5条消息"
    assert "消息9" in context, "应包含最近5条消息"
    assert "消息0" not in context, "不应包含较早的消息"
    print("  ✓ 通过\n")

    # 测试场景4：历史消息过长（截断）
    print("场景4：历史消息过长（截断到200字符）")
    history = [
        {"role": "user", "content": "a" * 300},  # 300个字符
    ]
    context = runner._extract_context_from_history(history, "测试")
    print(f"  上下文包含'...'截断: {'...' in context}")
    assert "..." in context, "长消息应被截断"
    print("  ✓ 通过\n")


def test_tool_matching_with_context():
    """测试基于上下文的工具匹配"""
    print("=== 测试基于上下文的工具匹配 ===\n")

    from agent.mcp_loader import load_mcp_tools
    from agent.vector_search import get_vector_search

    # 加载MCP工具
    registry = load_mcp_tools()

    # 创建 NiuRunner
    runner = NiuRunner(
        llm_config={"apikey": "test", "model": "test"},
        mcp_client=None
    )

    # 设置MCP工具Schema
    all_tools = registry.get_schemas()
    runner.set_mcp_tools_schema(all_tools)

    # 测试场景1：单独的"是的"无法匹配工具
    print("场景1：单独的'是的'无法匹配工具")
    results = runner.vector_search.search(
        query="是的",
        limit=3,
        min_score=0.5,
        filter={'category': 'mcp_tool'}
    )
    print(f"  匹配工具数量: {len(results)}")
    print(f"  预期: 0个工具（'是的'语义不明确）")
    print()

    # 测试场景2：结合历史上下文可以匹配工具
    print("场景2：结合历史上下文可以匹配工具")
    history = [
        {"role": "user", "content": "入库照片 E:/test.jpg"},
        {"role": "assistant", "content": "好的，我将入库这张照片"},
    ]
    context = runner._extract_context_from_history(history, "是的")
    print(f"  上下文: {context[:100]}...")

    results = runner.vector_search.search(
        query=context,
        limit=3,
        min_score=0.5,
        filter={'category': 'mcp_tool'}
    )
    print(f"  匹配工具数量: {len(results)}")
    if results:
        for r in results:
            tool_name = r.metadata.get('name', '')
            server = r.metadata.get('server', '')
            score = r.score if hasattr(r, 'score') else 0
            print(f"    - {server}/{tool_name} (score: {score:.3f})")
    print()

    # 测试场景3：用户输入明确，无需历史
    print("场景3：用户输入明确，无需历史")
    results = runner.vector_search.search(
        query="检索之前的记忆",
        limit=3,
        min_score=0.5,
        filter={'category': 'mcp_tool'}
    )
    print(f"  匹配工具数量: {len(results)}")
    if results:
        for r in results:
            tool_name = r.metadata.get('name', '')
            server = r.metadata.get('server', '')
            score = r.score if hasattr(r, 'score') else 0
            print(f"    - {server}/{tool_name} (score: {score:.3f})")
    print()


if __name__ == "__main__":
    test_context_extraction()
    test_tool_matching_with_context()

    print("=== 所有测试完成 ===")
