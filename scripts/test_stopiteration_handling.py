"""测试 runner.py 中处理 StopIteration.value 的逻辑完整性"""

import json
import sys
import os

# Set UTF-8 encoding for stdout/stderr
if sys.stdout.encoding != 'utf-8':
    sys.stdout = open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1)
if sys.stderr.encoding != 'utf-8':
    sys.stderr = open(sys.stderr.fileno(), mode='w', encoding='utf-8', buffering=1)

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from agent.generic.llmcore import MockResponse


def test_current_logic():
    """测试当前逻辑的覆盖情况"""

    # 情况 1: data 是 MockResponse 对象（有 content）
    print("=" * 60)
    print("测试 1: data 是 MockResponse 对象（有 content）")
    return_value = {
        'result': 'CURRENT_TASK_DONE',
        'data': MockResponse(thinking="思考过程", content="这是最终回复", tool_calls=None, raw=None)
    }

    if isinstance(return_value, dict) and "data" in return_value:
        data = return_value["data"]
        # 处理 MockResponse 对象
        if hasattr(data, 'content'):
            full_resp = str(data.content)
        elif isinstance(data, dict):
            full_resp = json.dumps(data, ensure_ascii=False)
        else:
            full_resp = ""

    print(f"  return_value: {return_value}")
    print(f"  full_resp: {full_resp}")
    print(f"  ✓ 结果: {full_resp}")
    assert full_resp == "这是最终回复", f"预期 '这是最终回复'，实际 '{full_resp}'"

    # 情况 2: data 是 MockResponse 对象（content 为 None）
    print("\n" + "=" * 60)
    print("测试 2: data 是 MockResponse 对象（content 为 None）")
    return_value = {
        'result': 'CURRENT_TASK_DONE',
        'data': MockResponse(thinking="思考过程", content=None, tool_calls=None, raw=None)
    }

    full_resp = ""
    if isinstance(return_value, dict) and "data" in return_value:
        data = return_value["data"]
        if hasattr(data, 'content'):
            full_resp = str(data.content)
        elif isinstance(data, dict):
            full_resp = json.dumps(data, ensure_ascii=False)
        else:
            full_resp = ""

    print(f"  return_value: {return_value}")
    print(f"  full_resp: {full_resp}")
    print(f"  ⚠️ 结果: {full_resp} (预期应该是空字符串，但实际是 'None')")

    # 情况 3: data 是字典
    print("\n" + "=" * 60)
    print("测试 3: data 是字典")
    return_value = {
        'result': 'CURRENT_TASK_DONE',
        'data': {"status": "success", "message": "操作完成"}
    }

    full_resp = ""
    if isinstance(return_value, dict) and "data" in return_value:
        data = return_value["data"]
        if hasattr(data, 'content'):
            full_resp = str(data.content)
        elif isinstance(data, dict):
            full_resp = json.dumps(data, ensure_ascii=False)
        else:
            full_resp = ""

    print(f"  return_value: {return_value}")
    print(f"  full_resp: {full_resp}")
    print(f"  ✓ 结果: {full_resp}")

    # 情况 4: data 是列表
    print("\n" + "=" * 60)
    print("测试 4: data 是列表")
    return_value = {
        'result': 'CURRENT_TASK_DONE',
        'data': ["item1", "item2", "item3"]
    }

    full_resp = ""
    if isinstance(return_value, dict) and "data" in return_value:
        data = return_value["data"]
        if hasattr(data, 'content'):
            full_resp = str(data.content)
        elif isinstance(data, dict):
            full_resp = json.dumps(data, ensure_ascii=False)
        else:
            full_resp = ""

    print(f"  return_value: {return_value}")
    print(f"  full_resp: {full_resp}")
    print(f"  ⚠️ 结果: {full_resp} (预期应该是 JSON 列表，但实际为空)")

    # 情况 5: data 是字符串
    print("\n" + "=" * 60)
    print("测试 5: data 是字符串")
    return_value = {
        'result': 'CURRENT_TASK_DONE',
        'data': "这是一条字符串结果"
    }

    full_resp = ""
    if isinstance(return_value, dict) and "data" in return_value:
        data = return_value["data"]
        if hasattr(data, 'content'):
            full_resp = str(data.content)
        elif isinstance(data, dict):
            full_resp = json.dumps(data, ensure_ascii=False)
        else:
            full_resp = ""

    print(f"  return_value: {return_value}")
    print(f"  full_resp: {full_resp}")
    print(f"  ⚠️ 结果: {full_resp} (预期应该是 '这是一条字符串结果'，但实际为空)")

    # 情况 6: data 是 None
    print("\n" + "=" * 60)
    print("测试 6: data 是 None")
    return_value = {
        'result': 'CURRENT_TASK_DONE',
        'data': None
    }

    full_resp = ""
    if isinstance(return_value, dict) and "data" in return_value:
        data = return_value["data"]
        if hasattr(data, 'content'):
            full_resp = str(data.content)
        elif isinstance(data, dict):
            full_resp = json.dumps(data, ensure_ascii=False)
        else:
            full_resp = ""

    print(f"  return_value: {return_value}")
    print(f"  full_resp: {full_resp}")
    print(f"  ✓ 结果: {full_resp} (空字符串是正确的)")

    # 情况 7: return_value 没有 data 字段
    print("\n" + "=" * 60)
    print("测试 7: return_value 没有 data 字段")
    return_value = {'result': 'MAX_TURNS_EXCEEDED'}

    full_resp = ""
    if isinstance(return_value, dict) and "data" in return_value:
        data = return_value["data"]
        if hasattr(data, 'content'):
            full_resp = str(data.content)
        elif isinstance(data, dict):
            full_resp = json.dumps(data, ensure_ascii=False)
        else:
            full_resp = ""

    print(f"  return_value: {return_value}")
    print(f"  full_resp: {full_resp}")
    print(f"  ✓ 结果: {full_resp} (空字符串是正确的)")


def test_improved_logic():
    """测试改进后的逻辑"""

    print("\n\n" + "=" * 60)
    print("测试改进后的逻辑")
    print("=" * 60)

    def process_return_value(return_value):
        """改进后的处理逻辑"""
        full_resp = ""

        if not return_value:
            return full_resp

        if isinstance(return_value, dict) and "data" in return_value:
            data = return_value["data"]

            # 优先处理有 content 属性的对象（如 MockResponse）
            if hasattr(data, 'content'):
                # 确保 content 不为 None
                content = data.content
                if content is not None:
                    full_resp = str(content)
                else:
                    full_resp = ""
            # 处理字典
            elif isinstance(data, dict):
                full_resp = json.dumps(data, ensure_ascii=False)
            # 处理列表
            elif isinstance(data, list):
                full_resp = json.dumps(data, ensure_ascii=False)
            # 处理字符串或其他类型
            elif data is not None:
                full_resp = str(data)

        return full_resp

    # 测试所有情况
    test_cases = [
        # (name, return_value, expected_output)
        ("MockResponse 有内容", {
            'result': 'CURRENT_TASK_DONE',
            'data': MockResponse(thinking="", content="测试内容", tool_calls=None, raw=None)
        }, "测试内容"),
        ("MockResponse content 为 None", {
            'result': 'CURRENT_TASK_DONE',
            'data': MockResponse(thinking="", content=None, tool_calls=None, raw=None)
        }, ""),
        ("字典", {
            'result': 'CURRENT_TASK_DONE',
            'data': {"status": "success"}
        }, '{"status": "success"}'),
        ("列表", {
            'result': 'CURRENT_TASK_DONE',
            'data': ["a", "b", "c"]
        }, '["a", "b", "c"]'),
        ("字符串", {
            'result': 'CURRENT_TASK_DONE',
            'data': "字符串结果"
        }, "字符串结果"),
        ("None", {
            'result': 'CURRENT_TASK_DONE',
            'data': None
        }, ""),
        ("无 data 字段", {
            'result': 'MAX_TURNS_EXCEEDED'
        }, ""),
    ]

    all_passed = True
    for name, return_value, expected in test_cases:
        result = process_return_value(return_value)
        passed = result == expected
        all_passed = all_passed and passed

        status = "✓" if passed else "✗"
        print(f"\n{status} {name}")
        print(f"  预期: {expected}")
        print(f"  实际: {result}")

        if not passed:
            print(f"  ❌ 测试失败！")

    print("\n" + "=" * 60)
    if all_passed:
        print("✅ 所有测试通过！")
    else:
        print("❌ 有测试失败！")
    print("=" * 60)


if __name__ == "__main__":
    test_current_logic()
    test_improved_logic()
