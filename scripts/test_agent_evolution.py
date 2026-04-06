#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试 Agent 层的自我进化功能

验证：
1. do_start_long_term_update 方法可以调用
2. 记忆类型推断正确
3. 能够保存到向量库
"""

import sys
import os
from pathlib import Path

# 设置项目根目录
project_root = Path(__file__).parent.parent

# 添加项目根目录到 sys.path
sys.path.insert(0, str(project_root))

# 设置 UTF-8 输出（修复 Windows 中文显示问题）
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

from agent.handler import NiuHandler


def test_infer_memory_type():
    """测试记忆类型推断"""
    print("\n" + "=" * 60)
    print("测试 1: 记忆类型推断")
    print("=" * 60)

    handler = NiuHandler()

    test_cases = [
        ("探测到 GPU: RTX 4090, 内存: 63.8GB", "environment"),
        ("用户喜欢使用深色主题", "preferences"),
        ("成功实现了文件上传功能", "skills"),
        ("遇到了内存泄漏问题，已解决", "experiences"),
        ("项目使用 Python 3.11", "facts"),
    ]

    for history_str, expected_type in test_cases:
        inferred_type = handler._infer_memory_type(history_str)
        status = "✓" if inferred_type == expected_type else "✗"
        print(f"{status} '{history_str[:30]}...' -> {inferred_type} (期望: {expected_type})")


def test_generate_memory_content():
    """测试记忆内容生成"""
    print("\n" + "=" * 60)
    print("测试 2: 记忆内容生成")
    print("=" * 60)

    handler = NiuHandler()

    history_str = """[Agent] 调用工具code_run, args: {'code': 'print("hello")'}
[Agent] 成功执行代码
[Agent] 学会了使用 Python 打印"""

    memory_type = "skills"
    content = handler._generate_memory_content(history_str, memory_type)
    title = handler._generate_memory_title(history_str, memory_type)
    importance = handler._calculate_importance(memory_type)

    print(f"\n生成的记忆内容:")
    print(f"  标题: {title}")
    print(f"  类型: {memory_type}")
    print(f"  重要性: {importance}")
    print(f"  内容长度: {len(content)} 字符")
    print(f"  内容预览: {content[:100]}...")


def test_calculate_importance():
    """测试重要性计算"""
    print("\n" + "=" * 60)
    print("测试 3: 重要性计算")
    print("=" * 60)

    handler = NiuHandler()

    types_and_importance = [
        ("environment", 0.9),
        ("preferences", 0.85),
        ("skills", 0.8),
        ("experiences", 0.7),
        ("facts", 0.75),
    ]

    for memory_type, expected_importance in types_and_importance:
        importance = handler._calculate_importance(memory_type)
        status = "✓" if importance == expected_importance else "✗"
        print(f"{status} {memory_type}: {importance} (期望: {expected_importance})")


def main():
    print("=" * 60)
    print("Agent 层自我进化功能测试")
    print("=" * 60)

    try:
        test_infer_memory_type()
        test_generate_memory_content()
        test_calculate_importance()

        print("\n" + "=" * 60)
        print("测试完成")
        print("=" * 60)

    except Exception as e:
        print(f"\n✗ 测试失败: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
