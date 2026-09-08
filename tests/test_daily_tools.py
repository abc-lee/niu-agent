#!/usr/bin/env python3
"""Test daily routine data tools (daily region in memory.json, lazy TTL)

spec: docs/superpowers/specs/2026-09-08-daily-routine-data-design.md v0.4 §3.2/§4
plan: docs/superpowers/plans/2026-09-08-daily-routine-data.md v0.2 §2 (T1)
"""

import asyncio
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp-servers" / "memory-server" / "src"))

import niu_memory_server as mod
import pytest

BARE_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")


@pytest.fixture()
def memory_path(tmp_path):
    """MEMORY_JSON_PATH 重定向隔离（照 _reset_memory_json_path 先例，防污染生产 ~/.niu/memory.json）"""
    path = tmp_path / ".niu" / "memory.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    mod._reset_memory_json_path()
    mod.MEMORY_JSON_PATH = path
    yield path
    mod._reset_memory_json_path()


def _seed(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _future(hours=24) -> str:
    return (datetime.now() + timedelta(hours=hours)).isoformat(timespec="seconds")


def _past(hours=1) -> str:
    return (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")


def test_daily_set_basic_write_preserves_other_fields(memory_path):
    """基本写入：返回形状 {status,key,expires_at} + entry 三字段 + 其余字段逐键深比较保留"""
    seed = {
        "permanent": [{"type": "memory", "content": "旧记忆"}],
        "identity": {"name": "lilei"},
        "parked": [],
        "user": {"tz": "Asia/Shanghai"},
    }
    _seed(memory_path, seed)
    snapshot = {k: v for k, v in seed.items() if k != "daily"}

    expires = _future()
    result = mod.daily_set("weather", "北京 晴 22-32°C", expires)
    assert result == {"status": "ok", "key": "weather", "expires_at": expires}, result

    data = json.loads(memory_path.read_text(encoding="utf-8"))
    entry = data["daily"]["weather"]
    assert entry["text"] == "北京 晴 22-32°C"
    assert entry["expires_at"] == expires
    assert BARE_ISO_RE.match(entry["updated_at"]), entry["updated_at"]
    # 其余字段原样保留（写路径全量重序列化，解析后逐键深比较）
    for k, v in snapshot.items():
        assert data[k] == v, f"字段 {k} 被改写"


def test_daily_set_upsert_same_key(memory_path):
    """同 key 重复调用=更新：不新增条目、text 覆盖"""
    _seed(memory_path, {"permanent": []})
    assert mod.daily_set("weather", "旧文本", _future())["status"] == "ok"
    result = mod.daily_set("weather", "新文本", _future(hours=48))
    assert result["status"] == "ok", result

    data = json.loads(memory_path.read_text(encoding="utf-8"))
    assert list(data["daily"].keys()) == ["weather"]
    assert data["daily"]["weather"]["text"] == "新文本"


def test_daily_set_no_write_time_cleanup(memory_path):
    """写侧不再物理清理：过期条目滞留文件；上限计数只算未过期（19 有效+1 过期时新 key 写入成功）"""
    _seed(memory_path, {"permanent": [], "daily": {
        **{f"k{i:02d}": {"text": "t", "expires_at": _future(), "updated_at": _past()} for i in range(19)},
        "stale": {"text": "已过期", "expires_at": _past(), "updated_at": _past(hours=2)},
    }})

    result = mod.daily_set("new_key", "新条目", _future())
    assert result["status"] == "ok", result  # 未过期 19 < 20：过期 stale 不占槽，写入成功

    data = json.loads(memory_path.read_text(encoding="utf-8"))
    assert "stale" in data["daily"], "写侧不再物理清理：过期条目应滞留文件（读时清理职责）"
    assert len(data["daily"]) == 21


def test_daily_set_validation_rejects_without_write(memory_path):
    """校验：key 非法/text 空或纯空白/text>100/expires_at 不可解析 → 报错不写入"""
    _seed(memory_path, {"permanent": [], "daily": {}})
    raw_before = memory_path.read_text(encoding="utf-8")

    for bad_key in ["Weather", "1abc", "a" * 31, "", "has-dash"]:
        result = mod.daily_set(bad_key, "文本", _future())
        assert result["status"] == "error" and "key" in result["message"], (bad_key, result)

    for bad_text in ["", "   ", None]:
        result = mod.daily_set("weather", bad_text, _future())
        assert result["status"] == "error" and "text" in result["message"], (bad_text, result)

    result = mod.daily_set("weather", "x" * 101, _future())
    assert result["status"] == "error" and "过长" in result["message"], result

    for bad_exp in ["not-a-date", "", 12345]:
        result = mod.daily_set("weather", "文本", bad_exp)
        assert result["status"] == "error" and "expires_at" in result["message"], (bad_exp, result)

    # 全部拒绝：文件未被写入
    assert memory_path.read_text(encoding="utf-8") == raw_before


def test_daily_set_text_single_line(memory_path):
    """text 含换行 → \\n/\\r 替换为空格后单行写入"""
    _seed(memory_path, {"permanent": []})
    result = mod.daily_set("weather", "第一行\n第二行\r第三行", _future())
    assert result["status"] == "ok", result

    data = json.loads(memory_path.read_text(encoding="utf-8"))
    text = data["daily"]["weather"]["text"]
    assert text == "第一行 第二行 第三行", text
    assert "\n" not in text and "\r" not in text


def test_daily_set_timezone_normalization(memory_path):
    """带时区偏移 expires_at → 转本地时间剥偏移存秒级裸串（与读侧字符串序比较同形态）"""
    _seed(memory_path, {"permanent": []})
    for raw in ["2026-09-09T15:00:00+08:00", "2026-09-09T07:00:00Z"]:
        key = "offset" if "+08:00" in raw else "zulu"
        expected = (
            datetime.fromisoformat(raw).astimezone().replace(tzinfo=None)
            .isoformat(timespec="seconds")
        )
        result = mod.daily_set(key, "文本", raw)
        assert result["status"] == "ok", result
        assert result["expires_at"] == expected, (raw, result["expires_at"], expected)
        assert BARE_ISO_RE.match(result["expires_at"]), result["expires_at"]

    data = json.loads(memory_path.read_text(encoding="utf-8"))
    for key in ("offset", "zulu"):
        assert BARE_ISO_RE.match(data["daily"][key]["expires_at"])


def test_daily_key_cap_and_upsert_exemption(memory_path):
    """上限 20（内存过滤未过期后计数）：满时报错；upsert 豁免仅限未过期 key"""
    full = {f"k{i:02d}": {"text": "t", "expires_at": _future(), "updated_at": _past()} for i in range(20)}
    _seed(memory_path, {"permanent": [], "daily": full})

    # 满 20 写新 key → 报错不写入
    result = mod.daily_set("new_key", "文本", _future())
    assert result["status"] == "error" and "已满" in result["message"], result
    data = json.loads(memory_path.read_text(encoding="utf-8"))
    assert len(data["daily"]) == 20 and "new_key" not in data["daily"]

    # upsert 已有 key（未过期条目中已存在）→ 豁免，成功
    result = mod.daily_set("k05", "更新文本", _future())
    assert result["status"] == "ok", result
    data = json.loads(memory_path.read_text(encoding="utf-8"))
    assert data["daily"]["k05"]["text"] == "更新文本" and len(data["daily"]) == 20

    # upsert 豁免边界：已过期的 key 不在未过期集合中，按新 key 计（写侧不物理清理）
    _seed(memory_path, {"permanent": [], "daily": {
        **{f"k{i:02d}": {"text": "t", "expires_at": _future(), "updated_at": _past()} for i in range(19)},
        "gone": {"text": "已过期", "expires_at": _past(), "updated_at": _past(hours=2)},
    }})
    result = mod.daily_set("gone", "复活?", _future())
    assert result["status"] == "ok", result  # 未过期 19 < 20（过期 gone 不计入上限），按新 key 写入
    data = json.loads(memory_path.read_text(encoding="utf-8"))
    assert len(data["daily"]) == 20

    _seed(memory_path, {"permanent": [], "daily": {
        **{f"k{i:02d}": {"text": "t", "expires_at": _future(), "updated_at": _past()} for i in range(20)},
        "gone": {"text": "已过期", "expires_at": _past(), "updated_at": _past(hours=2)},
    }})
    result = mod.daily_set("gone", "复活?", _future())
    assert result["status"] == "error" and "已满" in result["message"], result  # 未过期 20（过期 gone 不计入豁免）→ 新 key 超限


def test_daily_delete(memory_path):
    """daily_delete：存在删除/不存在幂等 deleted=false；写侧不再物理清理，过期 stale 滞留文件"""
    _seed(memory_path, {"permanent": [], "daily": {
        "weather": {"text": "晴", "expires_at": _future(), "updated_at": _past()},
        "stale": {"text": "旧", "expires_at": _past(), "updated_at": _past(hours=2)},
    }})

    result = mod.daily_delete("weather")
    assert result == {"status": "ok", "deleted": True}, result
    data = json.loads(memory_path.read_text(encoding="utf-8"))
    assert set(data["daily"].keys()) == {"stale"}  # 写侧不再物理清理：过期 stale 滞留文件（读时清理职责）

    result = mod.daily_delete("ghost")
    assert result == {"status": "ok", "deleted": False}, result


def test_daily_corrupt_file_rejects_write(memory_path):
    """损坏守卫双闸①：_raw_fallback → 拒写报错，文件不被覆写"""
    memory_path.write_text("{broken json", encoding="utf-8")

    for fn in (lambda: mod.daily_set("weather", "文本", _future()),
               lambda: mod.daily_delete("weather")):
        result = fn()
        assert result["status"] == "error" and "文件损坏" in result["message"], result
        assert memory_path.read_text(encoding="utf-8") == "{broken json"


def test_daily_in_lock_reread_corrupt_raises(memory_path, monkeypatch):
    """损坏守卫双闸②：入口检查通过后锁内重读损坏 → raise 拒写（TOCTOU 兜底），不覆写文件"""
    _seed(memory_path, {"permanent": [{"type": "memory", "content": "旧记忆"}]})
    monkeypatch.setattr(mod, "_read_memory_json", lambda: {"permanent": []})  # 闸①放行
    memory_path.write_text("{broken json", encoding="utf-8")

    result = mod.daily_set("weather", "文本", _future())
    assert result["status"] == "error" and "文件损坏" in result["message"], result
    assert memory_path.read_text(encoding="utf-8") == "{broken json"  # 未被覆写成只剩 daily 键


def test_daily_non_dict_guard(memory_path):
    """daily 键非 dict → warning + 视为 {}（不卡死），写侧顺带修复，其余字段保留"""
    _seed(memory_path, {"permanent": [{"type": "memory", "content": "旧记忆"}], "daily": ["not", "a", "dict"]})

    result = mod.daily_set("weather", "文本", _future())
    assert result["status"] == "ok", result
    data = json.loads(memory_path.read_text(encoding="utf-8"))
    assert set(data["daily"].keys()) == {"weather"}
    assert data["permanent"][0]["content"] == "旧记忆"

    # daily_delete 对非 dict 同样不卡死（幂等 deleted=false）
    memory_path.write_text(json.dumps({"daily": "still-not-dict"}, ensure_ascii=False), encoding="utf-8")
    result = mod.daily_delete("weather")
    assert result == {"status": "ok", "deleted": False}, result


def test_daily_alias_returns_dict(memory_path):
    """模块级别名 daily_set/daily_delete 必须返回 dict（钉死 ToolRegistry 直查接线）"""
    _seed(memory_path, {"permanent": []})
    result = mod.daily_set("weather", "别名写入", _future())
    assert isinstance(result, dict), f"别名接线失效，返回 {type(result)}"
    assert result["status"] == "ok", result

    result = mod.daily_delete(key="weather")
    assert isinstance(result, dict) and result["deleted"] is True, result


def test_daily_registration_sync(memory_path):
    """三处注册同步守卫：TOOL_SCHEMAS（主进程/disk 真实注册源）+ get_tool_definitions + call_tool dispatch"""
    _seed(memory_path, {"permanent": []})
    # TOOL_SCHEMAS 漏登记 → mcp_loader 不注册 → disk 调用失败（R2-B P2）
    assert "daily_set" in mod.TOOL_SCHEMAS
    assert mod.TOOL_SCHEMAS["daily_set"]["input_schema"]["required"] == ["key", "text", "expires_at"]
    assert "daily_delete" in mod.TOOL_SCHEMAS
    assert mod.TOOL_SCHEMAS["daily_delete"]["input_schema"]["required"] == ["key"]

    names = [t.name for t in mod.get_tool_definitions()]
    assert "daily_set" in names and "daily_delete" in names, names

    # call_tool dispatch 分支（装饰器返回原函数，可直接 await）
    out = asyncio.run(mod.call_tool("daily_set", {
        "key": "weather", "text": "晴 22°C", "expires_at": _future()}))
    assert json.loads(out[0].text)["status"] == "ok"
    out = asyncio.run(mod.call_tool("daily_delete", {"key": "weather"}))
    assert json.loads(out[0].text)["deleted"] is True


def test_mcp_servers_yaml_has_no_daily_entries():
    """grep 守卫：mcp-servers.yaml 不含 daily_set/daily_delete 条目（保 hidden 语义，防误加 visibility: static）"""
    yaml_path = Path(__file__).parent.parent / "config" / "mcp-servers.yaml"
    content = yaml_path.read_text(encoding="utf-8")
    assert "daily_set" not in content, "mcp-servers.yaml 不应登记 daily_set（默认 hidden，spec §3.2）"
    assert "daily_delete" not in content, "mcp-servers.yaml 不应登记 daily_delete（默认 hidden，spec §3.2）"
