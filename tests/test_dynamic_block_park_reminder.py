#!/usr/bin/env python3
"""Test runner 动态块 [暂存事项] 提醒行（T2：_park_reminder_line）。

覆盖场景：
1. 空/缺/损坏三态：parked 空 → 空串；memory.json 不存在 → 空串；损坏 JSON → warning + 空串降级
2. 字节稳定：同一 parked 数据两轮 _park_reminder_line() 返回值全等
   （R3-B：动态块尾部 Current Time 每秒变，整区块比全等跨秒必 flaky，只比提醒行）
3. 更新与排序：parked 变化后行更新；①=最新（数组头）
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp-servers" / "memory-server" / "src"))

import niu_memory_server as mod
from agent.runner import NiuRunner


def _capture_loguru(level="WARNING"):
    """loguru sink 捕获（runner 用 loguru 而非 stdlib logging，pytest caplog 捕获不到——项目既有模式）。"""
    from loguru import logger

    messages = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level=level)
    return messages, sink_id


@pytest.fixture
def memory_file(tmp_path, monkeypatch):
    """路径隔离：MEMORY_JSON_PATH + _reset 重定向到独立临时文件（防污染真实 ~/.niu）。"""
    mod._reset_memory_json_path()
    monkeypatch.setattr(mod, "MEMORY_JSON_PATH", tmp_path / "memory.json")
    return tmp_path / "memory.json"


def _make_runner():
    """NiuRunner.__init__ 有重副作用，测试用 __new__ 轻量构造（R4-B 钉死）。"""
    return NiuRunner.__new__(NiuRunner)


def test_reminder_empty_absent(memory_file):
    """三态全钉：文件不存在 → 空串；parked 空 → 空串；损坏 JSON → warning + 空串降级。"""
    runner = _make_runner()

    # 态1：memory.json 不存在 → 空串（R2 守卫：全新环境正常态，防 FileNotFoundError 刷屏）
    assert runner._park_reminder_line() == ""

    # 态2：文件存在但无 parked 键（parked 空）→ 空串
    memory_file.write_text(json.dumps({"identity": {"name": "妞妞"}}), encoding="utf-8")
    assert runner._park_reminder_line() == ""

    # 态3：损坏 JSON → warning + 空串降级（R1：禁止静默吞——架空常驻提醒语义）
    memory_file.write_text("{ not valid json !!!", encoding="utf-8")
    messages, sink_id = _capture_loguru()
    try:
        assert runner._park_reminder_line() == ""
    finally:
        from loguru import logger

        logger.remove(sink_id)
    assert any("[暂存提醒] 读取失败" in m for m in messages), "损坏 JSON 应记录 warning（不静默）"


def test_reminder_byte_stable(memory_file):
    """只比 _park_reminder_line() 返回值两轮全等（R3-B：不比含 Current Time 的整动态块）。"""
    runner = _make_runner()
    memory_file.write_text(
        json.dumps(
            {
                "parked": [
                    {"summary": "话题A", "detail": "细节", "anchor_msg_id": "m1", "parked_at": "2026-08-27T10:00:00"},
                    {"summary": "话题B", "detail": "细节", "anchor_msg_id": "m2", "parked_at": "2026-08-27T09:00:00"},
                ]
            }
        ),
        encoding="utf-8",
    )
    first = runner._park_reminder_line()
    second = runner._park_reminder_line()
    assert first == second
    assert first  # 非空
    assert "[暂存事项] 2 项" in first


def test_reminder_updates_and_order(memory_file):
    """parked 变化后行更新；①=最新（数组头，T1 insert(0) 契约）。"""
    runner = _make_runner()
    memory_file.write_text(
        json.dumps(
            {
                "parked": [
                    {"summary": "旧话题", "detail": "d", "anchor_msg_id": "m1", "parked_at": "2026-08-27T08:00:00"},
                ]
            }
        ),
        encoding="utf-8",
    )
    line1 = runner._park_reminder_line()
    assert "①〈旧话题〉" in line1

    # 新项插数组头 → 行更新且 ①=最新
    memory_file.write_text(
        json.dumps(
            {
                "parked": [
                    {"summary": "新话题", "detail": "d", "anchor_msg_id": "m2", "parked_at": "2026-08-27T11:00:00"},
                    {"summary": "旧话题", "detail": "d", "anchor_msg_id": "m1", "parked_at": "2026-08-27T08:00:00"},
                ]
            }
        ),
        encoding="utf-8",
    )
    line2 = runner._park_reminder_line()
    assert line2 != line1  # parked 变化后行更新
    assert "2 项" in line2
    assert line2.index("①〈新话题〉") < line2.index("②〈旧话题〉")
