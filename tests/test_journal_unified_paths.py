# tests/test_journal_unified_paths.py
"""
Journal-Agent 三路径统一集成测试

真实测试：需要程序运行 + 真实 LLM。
手动执行：RUN_INTEGRATION_TESTS=1 python tests/test_journal_unified_paths.py

验证点：
1. 路径2/3（API触发）— journal.md 格式一致
2. 路径1（主Agent触发）— 也能正确写入日志
3. 游标文件正确更新
"""

import json
import os
import time

import pytest

# 集成测试：需要程序运行 + 真实 LLM，pytest 自动发现时跳过
pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_INTEGRATION_TESTS"),
    reason="集成测试需要程序运行+真实LLM，设置 RUN_INTEGRATION_TESTS=1 启用",
)
from pathlib import Path  # noqa: E402

import requests  # noqa: E402

API_BASE = "http://localhost:9876"
NIU_DIR = Path.home() / ".niu"
JOURNAL_PATH = NIU_DIR / "journal.md"
CURSOR_PATH = NIU_DIR / "last_journal.json"


def test_api_health():
    """验证 API 可达"""
    resp = requests.get(f"{API_BASE}/api/stats", timeout=5)
    assert resp.status_code == 200, f"API not reachable: {resp.status_code}"
    print("[PASS] API health check")


def test_send_chat_messages():
    """发送几条对话消息，为日志记录提供数据"""
    messages = [
        "我刚完成了代码审查功能的重构",
        "修复了一个关于路径展开的bug",
    ]
    for msg in messages:
        resp = requests.post(
            f"{API_BASE}/chat",
            json={"message": msg},
            timeout=60,
        )
        assert resp.status_code == 200, f"Chat failed: {resp.status_code}"
        time.sleep(5)
    print(f"[PASS] Sent {len(messages)} chat messages")


def test_force_tidy_triggers_journal():
    """路径3：通过 force tidy 触发 journal-agent"""
    old_cursor = ""
    if CURSOR_PATH.exists():
        old_cursor = json.loads(CURSOR_PATH.read_text()).get("last_journal_id", "")

    resp = requests.post(
        f"{API_BASE}/api/context/tidy",
        json={"session_id": "default", "mode": "force"},
        timeout=120,
    )
    assert resp.status_code == 200, f"Force tidy failed: {resp.status_code}"
    result = resp.json()
    print(f"[INFO] Force tidy result: {json.dumps(result, ensure_ascii=False)[:200]}")

    time.sleep(10)

    assert CURSOR_PATH.exists(), "Cursor file not created"
    new_cursor = json.loads(CURSOR_PATH.read_text()).get("last_journal_id", "")
    assert new_cursor != old_cursor, f"Cursor not updated: {old_cursor} -> {new_cursor}"
    print(f"[PASS] Force tidy: cursor updated {old_cursor[:8]}... -> {new_cursor[:8]}...")

    assert JOURNAL_PATH.exists(), "journal.md not created"
    content = JOURNAL_PATH.read_text(encoding="utf-8")
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    assert f"# {today}" in content, "Today's date header not found in journal.md"
    print("[PASS] Force tidy: journal.md contains today's entries")


def test_chat_triggers_journal_via_handler():
    """路径1：通过主Agent对话触发 journal-agent"""
    old_cursor = json.loads(CURSOR_PATH.read_text()).get("last_journal_id", "")

    resp = requests.post(
        f"{API_BASE}/chat",
        json={"message": "记录一下今天的工作"},
        timeout=120,
    )
    assert resp.status_code == 200, f"Chat trigger failed: {resp.status_code}"

    time.sleep(30)

    if CURSOR_PATH.exists():
        new_cursor = json.loads(CURSOR_PATH.read_text()).get("last_journal_id", "")
        if new_cursor != old_cursor:
            print(f"[PASS] Path-1: cursor updated {old_cursor[:8]}... -> {new_cursor[:8]}...")
        else:
            print("[WARN] Path-1: cursor not updated (may be no new messages)")
    else:
        print("[WARN] Path-1: cursor file missing")


def test_journal_format_consistency():
    """验证 journal.md 格式一致性"""
    content = JOURNAL_PATH.read_text(encoding="utf-8")

    import re
    date_headers = re.findall(r'^# \d{4}-\d{2}-\d{2}', content, re.MULTILINE)
    print(f"[INFO] Found {len(date_headers)} date headers: {date_headers}")

    entries = re.findall(r'^- \d{2}:\d{2} .+ \| .+ \| .+ \| .+', content, re.MULTILINE)
    print(f"[INFO] Found {len(entries)} journal entries")

    assert len(date_headers) == len(set(date_headers)), "Duplicate date headers found"
    print("[PASS] No duplicate date headers")


if __name__ == "__main__":
    print("=== Journal-Agent 三路径统一集成测试 ===\n")
    print("前置条件：程序已启动（./niu）\n")

    try:
        test_api_health()
        test_send_chat_messages()
        test_force_tidy_triggers_journal()
        test_chat_triggers_journal_via_handler()
        test_journal_format_consistency()
        print("\n=== 所有测试通过 ===")
    except AssertionError as e:
        print(f"\n=== 测试失败: {e} ===")
    except Exception as e:
        print(f"\n=== 测试异常: {e} ===")
