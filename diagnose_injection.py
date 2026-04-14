#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
诊断 Skills 和 MCP 工具注入问题

运行方式：python diagnose_injection.py
"""

import sys
from pathlib import Path

# 添加项目根目录到 sys.path
sys.path.insert(0, str(Path(__file__).parent))

def check_vector_db():
    """检查向量库"""
    print("\n" + "="*70)
    print("1. 检查向量库")
    print("="*70)

    from agent.vector_search import get_vector_search

    vs = get_vector_search()

    # 测试 Skills 搜索
    skills = vs.search(
        query="browser automation",
        limit=3,
        min_score=0.35,
        filter={'level': 'l1', 'category': 'skill'}
    )

    print(f"\nSkills 数量: {len(skills)}")
    for i, s in enumerate(skills, 1):
        print(f"  [{i}] {s.metadata.get('name')} (score: {s.score:.2f})")

    # 测试 MCP 工具搜索
    tools = vs.search(
        query="browser navigate",
        limit=5,
        min_score=0.15,
        filter={'level': 'l1', 'category': 'mcp_tool'}
    )

    print(f"\nMCP 工具数量: {len(tools)}")
    for i, t in enumerate(tools[:3], 1):
        print(f"  [{i}] {t.metadata.get('name')} (score: {t.score:.2f})")

    return len(skills) > 0 and len(tools) > 0

def check_injection_logic():
    """检查注入逻辑"""
    print("\n" + "="*70)
    print("2. 检查注入逻辑")
    print("="*70)

    import json
    from agent.runner import NiuRunner

    with open('config/user-config.json', 'r', encoding='utf-8') as f:
        config = json.load(f)

    runner = NiuRunner(config['llm'])

    # 测试注入
    context = "user: 打开网页\nuser: 帮我导航到百度"
    injection = runner._inject_dynamic_resources(context)

    print(f"\n注入长度: {len(injection) if injection else 0} 字符")

    if injection:
        has_skills = "### [相关技能]" in injection
        has_mcp = "### [可用工具]" in injection

        print(f"包含 Skills: {'✅' if has_skills else '❌'}")
        print(f"包含 MCP 工具: {'✅' if has_mcp else '❌'}")

        if has_skills:
            lines = injection.split('\n')
            skill_count = sum(1 for line in lines if line.strip().startswith('1.') or line.strip().startswith('2.') or line.strip().startswith('3.'))
            print(f"Skills 数量: ~{skill_count}")

        if has_mcp:
            lines = injection.split('\n')
            tool_count = sum(1 for line in lines if '**photo-server/' in line or '**config-manager/' in line)
            print(f"MCP 工具示例: ~{tool_count}")

        return has_skills or has_mcp
    else:
        print("❌ 无注入")
        return False

def check_runner_instance():
    """检查 Runner 实例"""
    print("\n" + "="*70)
    print("3. 检查 Runner 实例")
    print("="*70)

    import json
    from agent.runner import NiuRunner

    with open('config/user-config.json', 'r', encoding='utf-8') as f:
        config = json.load(f)

    runner = NiuRunner(config['llm'])

    print(f"\n✅ Runner 初始化成功")
    print(f"向量搜索: {'✅' if runner.vector_search else '❌'}")
    print(f"ToolLifecycle: {'✅' if runner.tool_lifecycle else '❌'}")

    return True

def main():
    """主诊断流程"""
    print("="*70)
    print("Skills 和 MCP 工具注入诊断")
    print("="*70)

    try:
        # 1. 检查向量库
        db_ok = check_vector_db()

        # 2. 检查注入逻辑
        injection_ok = check_injection_logic()

        # 3. 检查 Runner 实例
        runner_ok = check_runner_instance()

        # 总结
        print("\n" + "="*70)
        print("诊断结果")
        print("="*70)
        print(f"向量库: {'✅ 正常' if db_ok else '❌ 异常'}")
        print(f"注入逻辑: {'✅ 正常' if injection_ok else '❌ 异常'}")
        print(f"Runner: {'✅ 正常' if runner_ok else '❌ 异常'}")

        if db_ok and injection_ok and runner_ok:
            print("\n✅ 所有检查通过！")
            print("\n如果服务仍未注入，请检查：")
            print("1. 是否有 Python 缓存（运行: find . -name '*.pyc' -delete）")
            print("2. 是否重启了服务（运行: go run main.go）")
            print("3. 查看控制台是否有 [Debug] Dynamic injection 输出")
        else:
            print("\n❌ 发现问题，请根据上述提示修复")

    except Exception as e:
        print(f"\n❌ 诊断失败: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
