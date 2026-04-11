"""
完整交互式测试：主Agent逐步控制插件完成MBTI测试

流程：
1. 打开页面，获取第一题
2. 主Agent分析题目并决策答案
3. 执行点击
4. 获取下一题
5. 重复直到完成
"""
import sys
import json
import time
sys.path.insert(0, 'E:\\tools\\ai-bot\\mcp-servers\\page-agent-mcp\\src')

from niu_page_agent import execute_task, get_status


def step1_open_and_get_first_question():
    """步骤1：打开页面并获取第一题"""
    print("=" * 60)
    print("Step 1: Open page and get first question")
    print("=" * 60)

    task = """
    Navigate to https://mbti-test.app/zh-cn/free-personality-test
    Wait for page to load completely
    Return the first question text and all answer options
    IMPORTANT: Do NOT click any options yet!
    Just return the question and options.
    """

    result = execute_task(task)
    # Handle encoding issues on Windows console
    try:
        if isinstance(result, dict):
            import json
            print(f"Result: {json.dumps(result, ensure_ascii=False, indent=2)}\n")
        else:
            print(f"Result: {result}\n")
    except UnicodeEncodeError:
        # Fallback: write to file instead of console
        with open("test_result.txt", "w", encoding="utf-8") as f:
            f.write(f"Result: {result}\n")
        print("Result: [Written to test_result.txt due to encoding]\n")
    return result


def step2_click_option(option_letter):
    """步骤2：点击指定选项"""
    print("=" * 60)
    print(f"Step 2: Click option {option_letter}")
    print("=" * 60)

    task = f"""
    On the current page, click option {option_letter}
    Wait for the next question to appear
    Return the next question text and options
    """

    result = execute_task(task)
    try:
        if isinstance(result, dict):
            import json
            print(f"Result: {json.dumps(result, ensure_ascii=False, indent=2)}\n")
        else:
            print(f"Result: {result}\n")
    except UnicodeEncodeError:
        with open("test_result.txt", "w", encoding="utf-8") as f:
            f.write(f"Result: {result}\n")
        print("Result: [Written to test_result.txt due to encoding]\n")
    return result


def step3_get_current_question():
    """步骤3：获取当前题目"""
    print("=" * 60)
    print("Step 3: Get current question")
    print("=" * 60)

    task = """
    Return the current question number, question text, and all options
    Do NOT click anything
    """

    result = execute_task(task)
    try:
        if isinstance(result, dict):
            import json
            print(f"Result: {json.dumps(result, ensure_ascii=False, indent=2)}\n")
        else:
            print(f"Result: {result}\n")
    except UnicodeEncodeError:
        with open("test_result.txt", "w", encoding="utf-8") as f:
            f.write(f"Result: {result}\n")
        print("Result: [Written to test_result.txt due to encoding]\n")
    return result


def step4_answer_multiple_questions(answers):
    """
    步骤4：连续回答多道题

    Args:
        answers: list of option letters, e.g. ['A', 'B', 'A', 'B']
    """
    print("=" * 60)
    print(f"Step 4: Answer {len(answers)} questions")
    print("=" * 60)

    for i, answer in enumerate(answers, 1):
        print(f"\nQuestion {i}: Clicking option {answer}")
        result = execute_task(f"Click option {answer} and return the next question")
        print(f"Result: {result[:200]}...")
        time.sleep(1)  # 短暂暂停避免过快


def test_interactive_workflow():
    """测试完整的交互工作流"""
    print("\n" + "=" * 60)
    print("Interactive MBTI Test Workflow")
    print("=" * 60)

    # 检查状态
    status = json.loads(get_status())
    print(f"\nStatus: {status}")
    if not status.get('connected'):
        print("ERROR: Extension not connected!")
        return

    # 测试1：打开并获取第一题
    print("\n--- Test 1: Get first question ---")
    result1 = step1_open_and_get_first_question()

    # 测试2：点击选项A
    print("\n--- Test 2: Click option A ---")
    result2 = step2_click_option('A')

    # 测试3：点击选项B
    print("\n--- Test 3: Click option B ---")
    result3 = step2_click_option('B')

    # 测试4：获取当前题目（应该是第4题）
    print("\n--- Test 4: Get current question ---")
    result4 = step3_get_current_question()

    # 测试5：连续回答5道题
    print("\n--- Test 5: Answer 5 questions quickly ---")
    step4_answer_multiple_questions(['A', 'B', 'A', 'B', 'A'])

    # 最终状态检查
    print("\n--- Final Status ---")
    final_status = json.loads(get_status())
    print(f"Status: {final_status}")

    # 获取当前题目（应该是第9题）
    print("\n--- Current question after 8 answers ---")
    step3_get_current_question()


if __name__ == "__main__":
    test_interactive_workflow()
