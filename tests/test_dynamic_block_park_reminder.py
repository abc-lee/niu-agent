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
from datetime import datetime, timedelta
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


# ===== daily 区域 [例行数据] 提醒行（_daily_reminder_line，spec §3.4 / plan §3）=====


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _daily_entry(text, expires_at):
    """daily 条目结构（plan §0 契约）：text/expires_at/updated_at 三字段。"""
    return {"text": text, "expires_at": expires_at, "updated_at": "2026-09-08T08:00:00"}


def test_daily_reminder_shows_unexpired(memory_file):
    """未过期条目显示：格式 \\n[例行数据] N 项：key〈text〉，text 原样内嵌。"""
    runner = _make_runner()
    future = _iso(datetime.now() + timedelta(hours=12))
    memory_file.write_text(
        json.dumps({"daily": {"weather": _daily_entry("北京 晴 22-32°C 午后雷阵雨", future)}}),
        encoding="utf-8",
    )
    assert runner._daily_reminder_line() == "\n[例行数据] 1 项：weather〈北京 晴 22-32°C 午后雷阵雨〉"


def test_daily_reminder_filters_expired(memory_file):
    """过期过滤（读侧只过滤不清理）：已过期/缺失/不可解析 expires_at 剔除，未过期保留。"""
    runner = _make_runner()
    past = _iso(datetime.now() - timedelta(hours=1))
    future = _iso(datetime.now() + timedelta(hours=12))
    memory_file.write_text(
        json.dumps(
            {
                "daily": {
                    "weather": _daily_entry("晴", future),
                    "stale": _daily_entry("旧数据", past),
                    "no_expiry": {"text": "无 expires_at", "updated_at": past},
                    "bad_expiry": _daily_entry("坏日期", "not-a-date"),
                }
            }
        ),
        encoding="utf-8",
    )
    line = runner._daily_reminder_line()
    assert line == "\n[例行数据] 1 项：weather〈晴〉"
    # 只过滤不清理：文件内容不变
    data = json.loads(memory_file.read_text(encoding="utf-8"))
    assert set(data["daily"]) == {"weather", "stale", "no_expiry", "bad_expiry"}


def test_daily_reminder_empty_region(memory_file):
    """空区域空串三态：daily 键缺失 / daily 空 dict / daily 非 dict（isinstance 判据视为 {}）。"""
    runner = _make_runner()
    memory_file.write_text(json.dumps({"identity": {"name": "妞妞"}}), encoding="utf-8")
    assert runner._daily_reminder_line() == ""

    memory_file.write_text(json.dumps({"daily": {}}), encoding="utf-8")
    assert runner._daily_reminder_line() == ""

    memory_file.write_text(json.dumps({"daily": "not-a-dict"}), encoding="utf-8")
    assert runner._daily_reminder_line() == ""

    # 文件不存在 → 空串（全新环境正常态）
    memory_file.unlink()
    assert runner._daily_reminder_line() == ""


def test_daily_reminder_drops_malformed(memory_file):
    """畸形条目剔除：非 dict 条目跳过，其余有效条目照常显示（不拖垮整行）。"""
    runner = _make_runner()
    future = _iso(datetime.now() + timedelta(hours=12))
    memory_file.write_text(
        json.dumps(
            {
                "daily": {
                    "weather": _daily_entry("晴", future),
                    "junk": "i am not a dict",
                    "also_junk": 42,
                }
            }
        ),
        encoding="utf-8",
    )
    assert runner._daily_reminder_line() == "\n[例行数据] 1 项：weather〈晴〉"


def test_daily_reminder_key_sorted_stable(memory_file):
    """key 字典序稳定：写入顺序打乱，输出按字典序；两轮调用返回值全等（前缀缓存友好）。"""
    runner = _make_runner()
    future = _iso(datetime.now() + timedelta(hours=12))
    memory_file.write_text(
        json.dumps(
            {
                "daily": {
                    "weather": _daily_entry("晴", future),
                    "hn_hot": _daily_entry("热榜", future),
                    "a_stock": _daily_entry("股票", future),
                }
            }
        ),
        encoding="utf-8",
    )
    first = runner._daily_reminder_line()
    second = runner._daily_reminder_line()
    assert first == second
    assert first == "\n[例行数据] 3 项：a_stock〈股票〉 hn_hot〈热榜〉 weather〈晴〉"


def test_daily_reminder_corrupt_json(memory_file):
    """损坏 JSON → warning + 空串降级（禁止静默吞）。"""
    runner = _make_runner()
    memory_file.write_text("{ not valid json !!!", encoding="utf-8")
    messages, sink_id = _capture_loguru()
    try:
        assert runner._daily_reminder_line() == ""
    finally:
        from loguru import logger

        logger.remove(sink_id)
    assert any("[例行数据] 读取失败" in m for m in messages), "损坏 JSON 应记录 warning（不静默）"


def test_dynamic_block_daily_above_park(memory_file):
    """集成：daily 行在暂存行上方；两区均空两行皆无（index/存在性断言，非整块全等）。"""
    runner = _make_runner()
    future = _iso(datetime.now() + timedelta(hours=12))
    memory_file.write_text(
        json.dumps(
            {
                "daily": {"weather": _daily_entry("晴", future)},
                "parked": [
                    {"summary": "话题A", "detail": "d", "anchor_msg_id": "m1", "parked_at": "2026-09-08T08:00:00"},
                ],
            }
        ),
        encoding="utf-8",
    )
    block = runner._build_dynamic_block("")
    assert block.index("[例行数据]") < block.index("[暂存事项]"), "daily 行应在暂存行上方"

    # 两区均空 → 两行皆无
    memory_file.write_text(json.dumps({"identity": {"name": "妞妞"}}), encoding="utf-8")
    block2 = runner._build_dynamic_block("")
    assert "[例行数据]" not in block2
    assert "[暂存事项]" not in block2
