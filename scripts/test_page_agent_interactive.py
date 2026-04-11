"""
Test Interactive Browser Automation

测试交互式浏览器操作：主Agent能否逐步控制插件？

假设：
1. 插件支持"暂停-等待"模式
2. 通过特殊的提示词可以触发交互式返回
3. 或者插件本身就会在需要决策时返回

测试步骤：
1. 测试普通模式（一次性完成）
2. 测试交互式提示词
3. 验证返回的数据格式
"""
import sys
import json
import time
sys.path.insert(0, 'E:\\tools\\ai-bot\\mcp-servers\\page-agent-mcp\\src')

from niu_page_agent import execute_task, get_status


def test_normal_mode():
    """测试1：普通模式（一次性完成整个测试）"""
    print("=" * 60)
    print("测试1：普通模式")
    print("=" * 60)

    task = """
    打开 https://mbti-test.app/zh-cn/free-personality-test
    完成整个MBTI人格测试
    返回测试结果（MBTI类型和描述）
    """

    print(f"任务: {task}")
    print("执行中...\n")

    start = time.time()
    result = execute_task(task)
    elapsed = time.time() - start

    print(f"耗时: {elapsed:.1f}秒")
    print(f"结果: {result[:500]}...\n")

    return result


def test_interactive_mode_v1():
    """测试2：交互式模式 v1（提示词要求每题暂停）"""
    print("=" * 60)
    print("测试2：交互式模式 v1 - 提示词要求每题暂停")
    print("=" * 60)

    task = """
    打开 https://mbti-test.app/zh-cn/free-personality-test

    重要：这不是一次性任务！

    步骤：
    1. 打开第一题
    2. 立即停止并返回题目内容
    3. 等待我的指令
    4. 不要自己答题！

    返回格式：
    {
      "status": "waiting_for_input",
      "current_question": "题目内容",
      "options": ["A. xxx", "B. xxx", ...]
    }
    """

    print(f"任务: {task}")
    print("执行中...\n")

    start = time.time()
    result = execute_task(task)
    elapsed = time.time() - start

    print(f"耗时: {elapsed:.1f}秒")
    print(f"结果: {result[:500]}...\n")

    return result


def test_interactive_mode_v2():
    """测试3：交互式模式 v2（只打开页面，不操作）"""
    print("=" * 60)
    print("测试3：交互式模式 v2 - 只打开页面")
    print("=" * 60)

    task = """
    任务：打开页面并截图

    1. 导航到 https://mbti-test.app/zh-cn/free-personality-test
    2. 等待页面完全加载
    3. 返回页面上看到的所有题目和选项
    4. 不要点击任何选项
    5. 立即返回
    """

    print(f"任务: {task}")
    print("执行中...\n")

    start = time.time()
    result = execute_task(task)
    elapsed = time.time() - start

    print(f"耗时: {elapsed:.1f}秒")
    print(f"结果: {result[:500]}...\n")

    return result


def test_step_by_step():
    """测试4：逐步操作（获取当前状态）"""
    print("=" * 60)
    print("测试4：逐步操作 - 先看看当前浏览器状态")
    print("=" * 60)

    task = """
    描述当前浏览器窗口的内容：
    - 当前URL是什么？
    - 页面上有什么内容？
    - 如果在测试页面，当前是第几题？题目是什么？
    """

    print(f"任务: {task}")
    print("执行中...\n")

    start = time.time()
    result = execute_task(task)
    elapsed = time.time() - start

    print(f"耗时: {elapsed:.1f}秒")
    print(f"结果: {result[:500]}...\n")

    return result


def main():
    """运行所有测试"""
    print("Page Agent Interactive Test")
    print("=" * 60)

    # 检查服务状态
    print("\nChecking service status...")
    status = get_status()
    print(f"Status: {status}\n")

    if not json.loads(status).get('connected'):
        print("ERROR: Extension not connected!")
        return

    # 依次运行测试
    tests = [
        # ("Normal Mode", test_normal_mode),  # Will take long time
        ("Interactive v1", test_interactive_mode_v1),
        ("Interactive v2", test_interactive_mode_v2),
        ("Current State", test_step_by_step),
    ]

    results = {}
    for name, test_fn in tests:
        try:
            results[name] = test_fn()
        except Exception as e:
            print(f"Error: {e}\n")
            results[name] = f"Error: {e}"

        # 每次测试后暂停，避免过载
        time.sleep(2)

    # 总结
    print("\n" + "=" * 60)
    print("Test Summary")
    print("=" * 60)
    for name, result in results.items():
        print(f"\n{name}:")
        print(f"  {result[:200]}...")


if __name__ == "__main__":
    main()
