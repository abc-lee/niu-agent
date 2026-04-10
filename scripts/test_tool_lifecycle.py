"""
测试工具生命周期管理
验证分数衰减机制和工具持久化
"""

import sys
from pathlib import Path

# 添加项目根目录到 sys.path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from agent.tool_lifecycle import ToolLifecycleManager


def test_tool_lifecycle():
    """测试工具生命周期管理"""
    print("=== 测试工具生命周期管理 ===\n")

    manager = ToolLifecycleManager(decay_rate=10, min_score=50)

    # 1. 测试工具命中
    print("1. 测试工具命中")
    manager.hit_tool("photo-server/ingest_photo")
    manager.hit_tool("scheduler-server/schedule_task")

    active_tools = manager.get_active_tools()
    print(f"   活跃工具数量: {len(active_tools)}")
    print(f"   活跃工具: {active_tools}")

    assert len(active_tools) == 2, "应该有2个活跃工具"
    assert manager.get_tool_score("photo-server/ingest_photo") == 100, "命中后分数应为100"
    assert manager.get_tool_score("scheduler-server/schedule_task") == 100, "命中后分数应为100"
    print("   ✓ 工具命中正确\n")

    # 2. 测试分数衰减
    print("2. 测试分数衰减")
    manager.decay_tools()

    score1 = manager.get_tool_score("photo-server/ingest_photo")
    score2 = manager.get_tool_score("scheduler-server/schedule_task")
    print(f"   photo-server/ingest_photo 分数: {score1}")
    print(f"   scheduler-server/schedule_task 分数: {score2}")

    assert score1 == 90, "衰减后分数应为90"
    assert score2 == 90, "衰减后分数应为90"
    print("   ✓ 分数衰减正确\n")

    # 3. 测试持续衰减
    print("3. 测试持续衰减")
    for i in range(3):
        manager.decay_tools()
        score = manager.get_tool_score("photo-server/ingest_photo")
        print(f"   第{i+2}轮衰减后分数: {score}")

    # 应该是 90 - 10*3 = 60
    assert manager.get_tool_score("photo-server/ingest_photo") == 60, "持续衰减后分数应为60"
    print("   ✓ 持续衰减正确\n")

    # 4. 测试工具移除
    print("4. 测试工具移除（分数 < 50）")
    # 再衰减2次，分数变为 40，应该被移除
    manager.decay_tools()
    manager.decay_tools()

    active_tools = manager.get_active_tools()
    print(f"   活跃工具数量: {len(active_tools)}")
    print(f"   活跃工具: {active_tools}")

    assert len(active_tools) == 0, "所有工具应该被移除"
    assert manager.get_tool_score("photo-server/ingest_photo") == 0, "工具不存在应返回0"
    print("   ✓ 工具移除正确\n")

    # 5. 测试工具重生
    print("5. 测试工具重生（重新命中）")
    manager.hit_tool("photo-server/ingest_photo")

    score = manager.get_tool_score("photo-server/ingest_photo")
    print(f"   重新命中后分数: {score}")

    assert score == 100, "重新命中后分数应重置为100"
    assert len(manager.get_active_tools()) == 1, "应该有1个活跃工具"
    print("   ✓ 工具重生正确\n")

    # 6. 测试清空
    print("6. 测试清空所有工具")
    manager.hit_tool("tool1")
    manager.hit_tool("tool2")
    manager.hit_tool("tool3")

    print(f"   清空前活跃工具数量: {len(manager.get_active_tools())}")
    manager.clear()
    print(f"   清空后活跃工具数量: {len(manager.get_active_tools())}")

    assert len(manager.get_active_tools()) == 0, "清空后应该没有活跃工具"
    print("   ✓ 清空功能正确\n")


def test_tool_persistence():
    """测试工具持久化（模拟多轮对话）"""
    print("=== 测试工具持久化（模拟多轮对话） ===\n")

    manager = ToolLifecycleManager(decay_rate=10, min_score=50)

    # 第1轮对话
    print("第1轮：用户说'入库照片'")
    manager.hit_tool("photo-server/ingest_photo")
    print(f"  命中工具: photo-server/ingest_photo, 分数: 100")
    print(f"  活跃工具: {manager.get_active_tools()}")
    assert len(manager.get_active_tools()) == 1

    manager.decay_tools()
    print(f"  衰减后分数: {manager.get_tool_score('photo-server/ingest_photo')}")
    print()

    # 第2轮对话
    print("第2轮：用户说'是的'")
    # 用户确认，但没有提到照片相关内容
    # 此时 ingest_photo 工具应该在列表中但分数降低
    print(f"  活跃工具: {manager.get_active_tools()}")
    print(f"  photo-server/ingest_photo 分数: {manager.get_tool_score('photo-server/ingest_photo')}")

    manager.decay_tools()
    print(f"  衰减后分数: {manager.get_tool_score('photo-server/ingest_photo')}")
    print()

    # 第3轮对话
    print("第3轮：用户再次提到'处理照片'")
    manager.hit_tool("photo-server/ingest_photo")  # 重新命中
    print(f"  重新命中，分数重置为: {manager.get_tool_score('photo-server/ingest_photo')}")
    print(f"  活跃工具: {manager.get_active_tools()}")

    manager.decay_tools()
    print(f"  衰减后分数: {manager.get_tool_score('photo-server/ingest_photo')}")
    print()

    # 第4-8轮对话（用户不再提照片）
    print("第4-8轮：用户讨论其他话题")
    for i in range(4, 9):
        manager.decay_tools()
        score = manager.get_tool_score('photo-server/ingest_photo')
        print(f"  第{i}轮衰减后分数: {score}")

    print(f"  最终活跃工具: {manager.get_active_tools()}")
    assert len(manager.get_active_tools()) == 0, "工具应该已被移除"
    print("  ✓ 工具正确移除\n")


if __name__ == "__main__":
    test_tool_lifecycle()
    test_tool_persistence()

    print("=== 所有测试完成 ===")
