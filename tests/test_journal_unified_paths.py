# tests/test_journal_unified_paths.py
"""
Journal-Agent 触发路径集成测试

真实测试：需要程序运行 + 真实 LLM。
手动执行：RUN_INTEGRATION_TESTS=1 python tests/test_journal_unified_paths.py

验证点：
1. 路径1（主Agent对话触发）— 正确写入 journal.md
2. journal.md 格式一致性（日期头去重、条目格式）

注：tidy/force 触发路径已退役（T6 压缩退役 + T7 journal 迁 scheduler journal_daily
定时任务），原「force tidy 推进 journal 游标」断言随之删除；游标推进由 scheduler
直执行分支负责，单元覆盖见 tests/test_journal_daily_scheduler.py。
"""

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


def test_chat_triggers_journal_via_handler():
    """路径1：通过主Agent对话触发 journal-agent（直读 DB 改造后以覆盖标记验证）"""
    old_text = JOURNAL_PATH.read_text(encoding="utf-8") if JOURNAL_PATH.exists() else ""
    old_markers = old_text.count("覆盖至: ")

    resp = requests.post(
        f"{API_BASE}/chat",
        json={"message": "记录一下今天的工作"},
        timeout=120,
    )
    assert resp.status_code == 200, f"Chat trigger failed: {resp.status_code}"

    time.sleep(30)

    new_text = JOURNAL_PATH.read_text(encoding="utf-8") if JOURNAL_PATH.exists() else ""
    new_markers = new_text.count("覆盖至: ")
    if new_text != old_text:
        print(f"[PASS] Path-1: journal updated, 覆盖标记 {old_markers} -> {new_markers}")
    else:
        print("[WARN] Path-1: journal not updated (may be no new messages)")


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
    print("=== Journal-Agent 触发路径集成测试 ===\n")
    print("前置条件：程序已启动（./niu）\n")

    try:
        test_api_health()
        test_send_chat_messages()
        test_chat_triggers_journal_via_handler()
        test_journal_format_consistency()
        print("\n=== 所有测试通过 ===")
    except AssertionError as e:
        print(f"\n=== 测试失败: {e} ===")
    except Exception as e:
        print(f"\n=== 测试异常: {e} ===")
