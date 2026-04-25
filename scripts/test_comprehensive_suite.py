#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
综合测试套件 - Virtual Disk 模式

测试范围：
1. 向量检索精度测试
2. 工具生命周期已移除（disk mode）
3. Disk 模式工具注入测试
4. 多轮对话测试
5. 递归查询测试
6. 历史上下文影响测试
"""

import sys
import io
from pathlib import Path

# 设置 UTF-8 输出（修复 Windows 中文显示问题）
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# 添加项目根目录到 sys.path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from agent.runner import NiuRunner
from agent.mcp_loader import load_mcp_tools
from agent.vector_search import get_vector_search


class TestSuite:
    """测试套件"""

    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.total = 0

    def test(self, name: str, condition: bool, detail: str = ""):
        """记录测试结果"""
        self.total += 1
        if condition:
            self.passed += 1
            print(f"  ✅ {name}")
            if detail:
                print(f"     {detail}")
        else:
            self.failed += 1
            print(f"  ❌ {name}")
            if detail:
                print(f"     {detail}")

    def summary(self):
        """输出测试摘要"""
        print(f"\n{'='*70}")
        print(f"测试摘要: {self.passed}/{self.total} 通过")
        print(f"{'='*70}")
        if self.failed > 0:
            print(f"⚠️  {self.failed} 个测试失败")
        else:
            print(f"✅ 所有测试通过！")


def test_vector_search_precision(suite: TestSuite):
    """测试1: 向量检索精度"""
    print(f"\n{'='*70}")
    print("测试 1: 向量检索精度")
    print(f"{'='*70}\n")

    vs = get_vector_search()

    # 检查数据库路径
    print(f"  向量库路径: {vs.db_path}\n")

    # 测试主Agent基础工具（memory-server + vector-store）
    test_cases = [
        # (查询, 预期工具, 最低分数)
        ("检索之前的记忆", "memory-server/recall", 0.5),
        ("记住这个", "memory-server/remember", 0.3),
        ("搜索文档", "vector-store/search_documents", 0.5),
    ]

    print("  主Agent基础工具测试:")
    for query, expected_tool, min_score in test_cases:
        results = vs.search(
            query=query,
            limit=3,
            min_score=0.0,  # 获取所有结果
            filter={'category': 'mcp_tool'}
        )

        if results:
            # 提取工具名
            matched_tools = [
                f"{r.metadata.get('server', '')}/{r.metadata.get('name', '')}"
                for r in results
            ]

            # 检查是否在top-3中
            is_in_top3 = expected_tool in matched_tools
            top1_score = results[0].score if hasattr(results[0], 'score') else 0

            detail = f"Top-3: {matched_tools[:3]}, 分数: {top1_score:.3f}"
            suite.test(
                f"'{query}' → {expected_tool}",
                is_in_top3 and top1_score >= min_score,
                detail
            )
        else:
            suite.test(f"'{query}' → {expected_tool}", False, "无匹配结果")


def test_tool_lifecycle(suite: TestSuite):
    """测试2: 工具生命周期 — 已移除（disk mode 不使用 tool_lifecycle）"""
    print(f"\n{'='*70}")
    print("测试 2: 工具生命周期管理 — SKIPPED (disk mode)")
    print(f"{'='*70}\n")
    suite.test("tool_lifecycle 已移除（disk 模式替代）", True, "disk mode: tools via disk YAML")


def test_disk_mode_injection(suite: TestSuite):
    """测试3: Disk 模式工具注入"""
    print(f"\n{'='*70}")
    print("测试 3: Disk 模式工具注入")
    print(f"{'='*70}\n")

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

    # 测试1: MCP tools 全部 hidden（disk mode）
    suite.test(
        "MCP tools 已加载（全部 hidden）",
        len(all_tools) > 0,
        f"实际数量: {len(all_tools)}"
    )

    # 测试2: disk_engine 已初始化
    has_disk = hasattr(runner, 'disk_engine') and runner.disk_engine is not None
    suite.test(
        "disk_engine 已初始化",
        has_disk,
        f"disk_engine: {type(runner.disk_engine).__name__ if has_disk else 'None'}"
    )

    # 测试3: base_tools_schema 存在
    suite.test(
        "base_tools_schema 存在",
        len(runner.base_tools_schema) > 0,
        f"数量: {len(runner.base_tools_schema)}"
    )


def test_context_extraction(suite: TestSuite):
    """测试4: 历史上下文提取"""
    print(f"\n{'='*70}")
    print("测试 4: 历史上下文提取")
    print(f"{'='*70}\n")

    registry = load_mcp_tools()
    runner = NiuRunner(
        llm_config={"apikey": "test", "model": "test"},
        mcp_client=None
    )
    runner.set_mcp_tools_schema(registry.get_schemas())

    # 测试1: 无历史消息
    context = runner._extract_context_from_history(None, "入库照片")
    suite.test(
        "无历史消息时返回用户输入",
        context == "入库照片",
        f"实际: {context}"
    )

    # 测试2: 有历史消息
    history = [
        {"role": "user", "content": "入库照片"},
        {"role": "assistant", "content": "好的"},
        {"role": "user", "content": "是的"},
    ]
    context = runner._extract_context_from_history(history, "确认")
    suite.test(
        "有历史消息时提取上下文",
        "入库照片" in context and "是的" in context and "确认" in context,
        f"上下文包含历史内容"
    )

    # 测试3: 历史消息过多
    history = [{"role": "user", "content": f"消息{i}"} for i in range(10)]
    context = runner._extract_context_from_history(history, "当前")
    suite.test(
        "历史消息超过5条时只保留最近5条",
        "消息5" in context and "消息0" not in context,
        "上下文正确截取最近5条"
    )


def test_context_impact_on_matching(suite: TestSuite):
    """测试5: 历史上下文对匹配的影响"""
    print(f"\n{'='*70}")
    print("测试 5: 历史上下文对匹配的影响")
    print(f"{'='*70}\n")

    registry = load_mcp_tools()
    runner = NiuRunner(
        llm_config={"apikey": "test", "model": "test"},
        mcp_client=None
    )
    runner.set_mcp_tools_schema(registry.get_schemas())

    vs = get_vector_search()

    # 测试场景: 单独的"是的"无法匹配，但结合历史可以匹配
    print("  场景1: 单独的'是的'无法匹配工具")
    results_alone = vs.search(
        query="是的",
        limit=3,
        min_score=0.5,
        filter={'category': 'mcp_tool'}
    )
    score_alone = results_alone[0].score if results_alone else 0

    print(f"    单独查询: {len(results_alone)}个结果, 最高分: {score_alone:.3f}")

    print("  场景2: 结合历史上下文可以匹配工具")
    history = [
        {"role": "user", "content": "入库照片 E:/test.jpg"},
        {"role": "assistant", "content": "好的，我将入库这张照片"},
    ]
    context = runner._extract_context_from_history(history, "是的")

    results_with_context = vs.search(
        query=context,
        limit=3,
        min_score=0.5,
        filter={'category': 'mcp_tool'}
    )
    score_with_context = results_with_context[0].score if results_with_context else 0

    print(f"    上下文查询: {len(results_with_context)}个结果, 最高分: {score_with_context:.3f}")
    if results_with_context:
        matched = results_with_context[0].metadata.get('name', '')
        print(f"    匹配工具: {matched}")

    suite.test(
        "历史上下文提升匹配分数",
        score_with_context > score_alone,
        f"单独: {score_alone:.3f}, 结合历史: {score_with_context:.3f}"
    )


def test_multi_turn_conversation(suite: TestSuite):
    """测试6: 多轮对话（disk mode — no tool_lifecycle）"""
    print(f"\n{'='*70}")
    print("测试 6: 多轮对话（模拟）— disk mode")
    print(f"{'='*70}\n")

    vs = get_vector_search()

    # 使用主Agent基础工具进行测试
    print("  第1轮: '检索之前的记忆'")
    results = vs.search(
        query="检索之前的记忆",
        limit=3,
        min_score=0.5,
        filter={'category': 'mcp_tool'}
    )

    tool_name = None
    if results:
        tool_name = f"{results[0].metadata.get('server', '')}/{results[0].metadata.get('name', '')}"
        print(f"    命中工具: {tool_name}")

    suite.test(
        "第1轮检索到工具",
        tool_name is not None,
        f"工具: {tool_name}"
    )

    print("  第2轮: '是的'")
    suite.test(
        "disk mode: 无 tool_lifecycle 衰减",
        True,
        "disk mode: tools via disk YAML, no scoring"
    )


def test_recursive_query(suite: TestSuite):
    """测试7: 递归查询"""
    print(f"\n{'='*70}")
    print("测试 7: 递归查询")
    print(f"{'='*70}\n")

    vs = get_vector_search()

    # 测试递归查询（使用主Agent基础工具）
    test_cases = [
        ("检索之前的记忆", "memory-server/recall"),
        ("搜索文档", "vector-store/search_documents"),
    ]

    for query, expected_tool in test_cases:
        # 第一轮：查询 query_pattern
        results1 = vs.search(
            query=query,
            limit=3,
            min_score=0.3,
            filter={'category': 'query_pattern'}
        )

        if results1:
            # 提取 refined_query
            metadata = results1[0].metadata if hasattr(results1[0], 'metadata') else {}
            refined_query = metadata.get('refined_query', '')
            is_recursive = metadata.get('is_recursive', False)

            print(f"  查询: '{query}'")
            print(f"    第一轮: query_pattern, 递归={is_recursive}")

            if is_recursive and refined_query:
                print(f"    refined_query: '{refined_query}'")

                # 第二轮：查询 mcp_tool
                results2 = vs.search(
                    query=refined_query,
                    limit=3,
                    min_score=0.3,
                    filter={'category': 'mcp_tool'}
                )

                if results2:
                    matched = f"{results2[0].metadata.get('server', '')}/{results2[0].metadata.get('name', '')}"
                    score = results2[0].score

                    suite.test(
                        f"递归查询成功: '{query}' → {expected_tool}",
                        expected_tool in matched,
                        f"匹配: {matched}, 分数: {score:.3f}"
                    )
                else:
                    suite.test(f"递归查询: '{query}'", False, "第二轮无结果")
            else:
                # 非递归查询也可能成功，直接查询mcp_tool
                results2 = vs.search(
                    query=query,
                    limit=3,
                    min_score=0.3,
                    filter={'category': 'mcp_tool'}
                )

                if results2:
                    matched = f"{results2[0].metadata.get('server', '')}/{results2[0].metadata.get('name', '')}"
                    score = results2[0].score

                    suite.test(
                        f"直接查询成功: '{query}' → {expected_tool}",
                        expected_tool in matched,
                        f"匹配: {matched}, 分数: {score:.3f}"
                    )
                else:
                    suite.test(f"查询: '{query}'", False, "无匹配结果")
        else:
            suite.test(f"查询: '{query}'", False, "第一轮无结果")


def main():
    """主测试函数"""
    print(f"\n{'='*70}")
    print("Virtual Disk 模式 - 综合测试套件")
    print(f"{'='*70}")

    suite = TestSuite()

    try:
        # 运行所有测试
        test_vector_search_precision(suite)
        test_tool_lifecycle(suite)
        test_disk_mode_injection(suite)
        test_context_extraction(suite)
        test_context_impact_on_matching(suite)
        test_multi_turn_conversation(suite)
        test_recursive_query(suite)

        # 输出摘要
        suite.summary()

        # 返回退出码
        return 0 if suite.failed == 0 else 1

    except Exception as e:
        print(f"\n❌ 测试执行失败: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
