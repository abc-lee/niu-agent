#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
测试 Skills 注入是否正常工作

运行方式：python test_skills_injection.py
"""

import sys
from pathlib import Path

# 添加项目根目录到 sys.path
sys.path.insert(0, str(Path(__file__).parent))

from agent.runner import NiuRunner
import json

def test_skills_injection():
    """测试 Skills 注入流程"""
    print("=" * 70)
    print("Skills 注入测试")
    print("=" * 70)

    # 加载配置
    with open('config/user-config.json', 'r', encoding='utf-8') as f:
        config = json.load(f)

    # 创建 Runner
    print("\n[1] 初始化 Runner...")
    runner = NiuRunner(config['llm'])

    # 模拟第一轮对话
    print("\n[2] 模拟第一轮对话...")
    print("    用户: '上网查一下今日热点'")
    print("    LLM 调用 browser_navigate 工具")

    # 工具命中
    runner.tool_lifecycle.hit_tool('browser-server/browser_navigate')
    pending = runner.tool_lifecycle.get_pending_skills()
    print(f"\n[3] Pending Skills（第一轮工具命中后）: {pending}")

    # 模拟第二轮对话
    print("\n[4] 模拟第二轮对话...")
    history = [
        {'role': 'user', 'content': '上网查一下今日热点'},
        {'role': 'assistant', 'content': '我帮你打开百度热搜'},
    ]
    user_input = '帮我导航到百度热搜'

    # 提取上下文
    context = runner._extract_context_from_history(history, user_input)
    print(f"    提取的上下文:\n{context}")

    # 动态注入
    print("\n[5] 执行动态注入...")
    injection = runner._inject_dynamic_resources(context)

    if injection:
        print(f"\n✅ 注入成功！")
        print(f"    注入长度: {len(injection)} 字符")

        # 统计注入内容
        if '### [相关技能]' in injection:
            print("    ✅ 包含 Skills 注入")
            # 提取 Skills 部分
            lines = injection.split('\n')
            skill_lines = []
            in_skills = False
            for line in lines:
                if '### [相关技能]' in line:
                    in_skills = True
                elif '### [' in line and in_skills:
                    break
                elif in_skills:
                    skill_lines.append(line)
            print("    Skills 内容:")
            for line in skill_lines[:10]:
                print(f"      {line}")
        else:
            print("    ❌ 未包含 Skills 注入")

        if '### [可用工具]' in injection:
            print("    ✅ 包含 MCP 工具注入")
        else:
            print("    ❌ 未包含 MCP 工具注入")
    else:
        print("\n❌ 注入失败！无任何内容注入")

    # 检查最终状态
    print("\n[6] 最终状态:")
    print(f"    Active tools: {runner.tool_lifecycle.get_active_tools()}")
    print(f"    Pending skills: {runner.tool_lifecycle.get_pending_skills()}")

    print("\n" + "=" * 70)
    print("测试完成")
    print("=" * 70)

if __name__ == "__main__":
    test_skills_injection()
