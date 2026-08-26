#!/usr/bin/env python3
"""Test conversation park/recall tools (parked array in memory.json)"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp-servers" / "memory-server" / "src"))

import niu_memory_server as mod


def _setup_module(memory_path):
    """Patch MEMORY_JSON_PATH for test isolation"""
    mod._reset_memory_json_path()
    mod.MEMORY_JSON_PATH = memory_path
    return mod


def _seed(tmp_path, parked):
    memory_path = tmp_path / ".niu" / "memory.json"
    memory_path.parent.mkdir(parents=True, exist_ok=True)
    memory_path.write_text(
        json.dumps({"permanent": [], "parked": parked}, ensure_ascii=False),
        encoding="utf-8",
    )
    _setup_module(memory_path)
    return memory_path


def _mock_store(monkeypatch, messages=None, exists=None):
    """Mock _get_store：anchor 侧必须 AsyncMock（普通 Mock 返回非协程 → _run_async TypeError）"""
    store = MagicMock()
    store.get_messages = AsyncMock(return_value=list(messages or []))
    if exists is not None:
        store.message_exists = AsyncMock(return_value=exists)
    monkeypatch.setattr(mod, "_get_store", lambda: store)
    return store


def test_park_writes_and_caps(tmp_path, monkeypatch):
    """park 成功写入+旧文件无 parked 键兼容+summary 换行压单行+长度超限拒绝+非 list 守卫+满10拒绝"""
    memory_path = tmp_path / ".niu" / "memory.json"
    memory_path.parent.mkdir(parents=True, exist_ok=True)
    memory_path.write_text('{"permanent": [{"type": "memory", "content": "旧记忆"}]}', encoding="utf-8")
    _setup_module(memory_path)
    # 统一 monkeypatch anchor 捕获（不 mock 会真开 ~/.niu DB，且 _capture 吞异常致 anchor='' 静默假绿）
    monkeypatch.setattr(mod, "_capture_anchor_msg_id", lambda: "anchor-fixed")

    # 成功写入（旧文件无 parked 键兼容），permanent 键被 _write_parked_only 保留
    result = mod.conversation_park_handler(
        summary="定时任务清理方案讨论",
        detail="发现15个cancelled任务；只有cancel无delete；待决定是否补delete工具",
    )
    assert result["status"] == "success", result
    assert result["message"] == "已暂存：「定时任务清理方案讨论」"
    data = json.loads(memory_path.read_text(encoding="utf-8"))
    assert [p["summary"] for p in data["parked"]] == ["定时任务清理方案讨论"]
    assert data["parked"][0]["detail"].startswith("发现15个cancelled")
    assert data["parked"][0]["anchor_msg_id"] == "anchor-fixed"
    assert "parked_at" in data["parked"][0]
    assert data["permanent"][0]["content"] == "旧记忆"

    # 空串/空白拒绝（对齐 remember 先例）
    assert mod.conversation_park_handler(summary="  ", detail="d")["status"] == "error"
    assert mod.conversation_park_handler(summary="s", detail="")["status"] == "error"

    # summary 换行被压成单行
    result = mod.conversation_park_handler(summary="第一行\n第二行", detail="d")
    assert result["status"] == "success", result
    data = json.loads(memory_path.read_text(encoding="utf-8"))
    assert data["parked"][0]["summary"] == "第一行 第二行"

    # 长度超限拒绝
    result = mod.conversation_park_handler(summary="长" * 200, detail="d")
    assert result["status"] == "error" and "summary 过长" in result["message"]
    result = mod.conversation_park_handler(summary="s", detail="长" * 500)
    assert result["status"] == "error" and "detail 过长" in result["message"]

    # 非 list parked 守卫：不 crash，按空处理
    memory_path.write_text('{"parked": "not-a-list"}', encoding="utf-8")
    assert mod.conversation_park_handler(summary="守卫后", detail="d")["status"] == "success"

    # 满 10 拒绝（「守卫后」已占 1 条，只剩 9 个空位）
    for i in range(mod.MAX_PARKED_ITEMS - 1):
        assert mod.conversation_park_handler(summary=f"事项{i}", detail="d")["status"] == "success"
    result = mod.conversation_park_handler(summary="第11条", detail="d")
    assert result["status"] == "error" and "已满" in result["message"]
    data = json.loads(memory_path.read_text(encoding="utf-8"))
    assert len(data["parked"]) == mod.MAX_PARKED_ITEMS

    mod._reset_memory_json_path()

def test_park_alias_returns_dict(tmp_path, monkeypatch):
    """模块级别名 conversation_park() 必须返回 dict（钉死别名接线，防 call_tool 包装静默劣化）"""
    memory_path = _seed(tmp_path, [])
    # anchor 侧 mock 用 AsyncMock：普通 Mock 返回非协程 → _run_async TypeError
    _mock_store(monkeypatch, messages=[SimpleNamespace(id="msg-1")])

    result = mod.conversation_park(summary="别名调用", detail="经模块级别名调用")
    assert isinstance(result, dict), f"别名接线失效，返回 {type(result)}"
    assert result["status"] == "success", result
    data = json.loads(memory_path.read_text(encoding="utf-8"))
    assert data["parked"][0]["anchor_msg_id"] == "msg-1"

    mod._reset_memory_json_path()


def test_park_anchor_capture(tmp_path, monkeypatch):
    """anchor 捕获两路全钉：有消息 → 最新消息 id；msgs=[] → 空串（规格 §8 空 anchor 分支）"""
    memory_path = _seed(tmp_path, [])
    # 有消息：mock 返回 Message dataclass 形态（带 .id 属性，不可下标；mock 成 dict 会假绿）
    _mock_store(monkeypatch, messages=[SimpleNamespace(id="latest-msg-id")])
    assert mod.conversation_park_handler(summary="有消息", detail="d")["status"] == "success"
    data = json.loads(memory_path.read_text(encoding="utf-8"))
    assert data["parked"][0]["anchor_msg_id"] == "latest-msg-id"

    # 空列表：anchor 为空串
    _mock_store(monkeypatch, messages=[])
    assert mod.conversation_park_handler(summary="无消息", detail="d")["status"] == "success"
    data = json.loads(memory_path.read_text(encoding="utf-8"))
    assert data["parked"][0]["anchor_msg_id"] == ""

    mod._reset_memory_json_path()


def test_recall_removes_and_alive(tmp_path, monkeypatch):
    """recall 返回完整字段+数组移除+anchor_alive 真假两路+空 anchor 不查 DB+越界 error+损坏文件守卫"""
    memory_path = _seed(tmp_path, [
        {"summary": "新话题", "detail": "新细节", "anchor_msg_id": "anchor-new", "parked_at": "2026-08-26T11:00:00"},
        {"summary": "旧话题", "detail": "旧细节", "anchor_msg_id": "anchor-old", "parked_at": "2026-08-26T10:00:00"},
    ])

    # anchor_alive=True 路：返回完整字段
    store = _mock_store(monkeypatch, exists=True)
    result = mod.conversation_recall(index=1)
    assert result["status"] == "success", result
    assert result["summary"] == "新话题"
    assert result["detail"] == "新细节"
    assert result["anchor_msg_id"] == "anchor-new"
    assert result["anchor_alive"] is True
    assert "锚点有效" in result["message"]
    store.message_exists.assert_awaited_once_with("anchor-new")
    # 召回即关：数组移除该项
    data = json.loads(memory_path.read_text(encoding="utf-8"))
    assert [p["summary"] for p in data["parked"]] == ["旧话题"]

    # anchor_alive=False 路
    _mock_store(monkeypatch, exists=False)
    result = mod.conversation_recall(index=1)
    assert result["status"] == "success", result
    assert result["summary"] == "旧话题"
    assert result["anchor_alive"] is False
    assert "锚点有效" not in result["message"]

    # 越界 error：1-based，0 与超上限都拒绝且不移除
    memory_path.write_text(json.dumps({"parked": [{"summary": "仅一条", "detail": "d", "anchor_msg_id": "", "parked_at": "2026-08-26T12:00:00"}]}, ensure_ascii=False), encoding="utf-8")
    result = mod.conversation_recall(index=0)
    assert result["status"] == "error" and "超出范围" in result["message"]
    result = mod.conversation_recall(index=2)
    assert result["status"] == "error" and "超出范围" in result["message"]
    data = json.loads(memory_path.read_text(encoding="utf-8"))
    assert len(data["parked"]) == 1  # 越界不触发移除

    # 空 anchor → False 且不查 DB
    store = _mock_store(monkeypatch, exists=False)
    result = mod.conversation_recall(index=1)
    assert result["status"] == "success", result
    assert result["anchor_alive"] is False
    store.message_exists.assert_not_awaited()

    # 空列表 error
    memory_path.write_text('{"permanent": []}', encoding="utf-8")
    result = mod.conversation_recall(index=1)
    assert result["status"] == "error" and "暂存列表为空" in result["message"]

    # 损坏文件守卫（R4-B：报文件损坏而非序号越界）
    memory_path.write_text("{broken json", encoding="utf-8")
    result = mod.conversation_recall(index=1)
    assert result["status"] == "error" and "文件损坏" in result["message"]

    mod._reset_memory_json_path()
