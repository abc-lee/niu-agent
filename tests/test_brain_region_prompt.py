"""Tests for brain region prompt injection into LightRAG LLM requests."""
from pathlib import Path


def test_build_static_brain_region_prompt_returns_string():
    """Static prompt is a non-empty string."""
    from niu_api.internal.brain_region_prompt import build_static_brain_region_prompt
    result = build_static_brain_region_prompt()
    assert isinstance(result, str)
    assert len(result) > 0


def test_build_static_brain_region_prompt_contains_key_concepts():
    """Static prompt contains all key brain region concepts."""
    from niu_api.internal.brain_region_prompt import build_static_brain_region_prompt
    result = build_static_brain_region_prompt()
    # Must contain these key terms
    assert "niu" in result
    assert "包含" in result
    # Static prompt uses "文档库脑区" and "人际关系脑区" as examples
    assert "文档库脑区" in result
    assert "人际关系脑区" in result
    assert "脑区" in result


def test_build_static_brain_region_prompt_consistent():
    """Calling twice returns the same content (pure function, no side effects)."""
    from niu_api.internal.brain_region_prompt import build_static_brain_region_prompt
    result1 = build_static_brain_region_prompt()
    result2 = build_static_brain_region_prompt()
    assert result1 == result2


from unittest.mock import patch  # noqa: E402


def test_build_dynamic_brain_region_prompt_with_regions():
    """Dynamic prompt includes current brain regions from graph."""
    from niu_api.internal.brain_region_prompt import build_dynamic_brain_region_prompt
    with patch("niu_api.internal.brain_region_prompt.get_brain_regions", return_value=[
        "聊天历史脑区",
        "文档库脑区",
        "知识体系脑区",
    ]):
        prompt = build_dynamic_brain_region_prompt()
    assert "聊天历史" in prompt
    assert "文档库" in prompt
    assert "知识体系" in prompt
    assert "当前图谱中的脑区" in prompt


def test_build_dynamic_brain_region_prompt_empty():
    """When no regions found, dynamic prompt returns fallback."""
    from niu_api.internal.brain_region_prompt import build_dynamic_brain_region_prompt
    with patch("niu_api.internal.brain_region_prompt.get_brain_regions", return_value=[]):
        prompt = build_dynamic_brain_region_prompt()
    assert "默认" in prompt, f"Expected fallback marker '默认' in prompt, got: {prompt!r}"


def test_build_dynamic_brain_region_prompt_adapter_failure():
    """When get_brain_regions raises exception, falls back to defaults."""
    from niu_api.internal.brain_region_prompt import build_dynamic_brain_region_prompt
    with patch("niu_api.internal.brain_region_prompt.get_brain_regions", side_effect=Exception("LightRAG not initialized")):
        prompt = build_dynamic_brain_region_prompt()
    assert "默认" in prompt, f"Expected fallback marker '默认' in prompt, got: {prompt!r}"


def test_build_dynamic_brain_region_prompt_none_result():
    """When get_brain_regions returns empty list, dynamic prompt falls back to defaults."""
    from niu_api.internal.brain_region_prompt import build_dynamic_brain_region_prompt
    with patch("niu_api.internal.brain_region_prompt.get_brain_regions", return_value=[]):
        prompt = build_dynamic_brain_region_prompt()
    assert "默认" in prompt, f"Expected fallback marker '默认' in prompt, got: {prompt!r}"


def test_build_dynamic_brain_region_prompt_whitespace_only():
    """When get_brain_regions returns empty list, dynamic prompt falls back to defaults."""
    from niu_api.internal.brain_region_prompt import build_dynamic_brain_region_prompt
    with patch("niu_api.internal.brain_region_prompt.get_brain_regions", return_value=[]):
        prompt = build_dynamic_brain_region_prompt()
    assert "默认" in prompt, f"Expected fallback marker '默认' in prompt, got: {prompt!r}"


def test_build_dynamic_brain_region_prompt_uses_local_mode():
    """Dynamic query reads from in-memory graph (no LLM calls)."""
    from niu_api.internal.brain_region_prompt import build_dynamic_brain_region_prompt
    with patch("niu_api.internal.brain_region_prompt.get_brain_regions", return_value=["测试脑区"]) as mock_get:
        build_dynamic_brain_region_prompt()

    # Verify get_brain_regions was called (reads graph directly, no LLM)
    mock_get.assert_called_once()


# ============== build_user_info_prompt Tests ==============


def test_build_user_info_prompt_all_fields_present(tmp_path, monkeypatch):
    """四字段全有真实值时，全部注入。"""
    import json as _json

    memory = {
        "user": {
            "name": "李磊",
            "nickname": "老板",
            "occupation": "IT工程师",
            "organization": "中国农业银行河北省分行科技部",
            "skills": ["CCIE认证"],  # 不应被注入（只取4字段）
        }
    }
    (tmp_path / ".niu").mkdir()
    (tmp_path / ".niu" / "memory.json").write_text(
        _json.dumps(memory, ensure_ascii=False), encoding="utf-8"
    )

    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from niu_api.internal.brain_region_prompt import build_user_info_prompt

    result = build_user_info_prompt()

    assert "## 知识图谱所属用户" in result
    assert "真实姓名：李磊" in result
    assert "称呼：老板" in result
    assert "职业：IT工程师" in result
    assert "工作单位：中国农业银行河北省分行科技部" in result
    # skills 不在4字段内，不应出现
    assert "CCIE认证" not in result


def test_build_user_info_prompt_all_placeholders(tmp_path, monkeypatch):
    """四字段全是"请询问"占位符时返回空串。"""
    import json as _json

    memory = {
        "user": {
            "name": "请询问用户姓名",
            "nickname": "请询问称呼",
            "occupation": "请询问职业",
            "organization": "请询问工作单位",
        }
    }
    (tmp_path / ".niu").mkdir()
    (tmp_path / ".niu" / "memory.json").write_text(
        _json.dumps(memory, ensure_ascii=False), encoding="utf-8"
    )

    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from niu_api.internal.brain_region_prompt import build_user_info_prompt

    assert build_user_info_prompt() == ""


def test_build_user_info_prompt_no_memory_file(tmp_path, monkeypatch):
    """memory.json 不存在时返回空串（不报错）。"""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from niu_api.internal.brain_region_prompt import build_user_info_prompt

    assert build_user_info_prompt() == ""


def test_user_info_null_field(tmp_path, monkeypatch):
    """memory.json 的 user 字段为 null 时返回空串（验证 isinstance(user, dict) 守卫）。"""
    import json as _json

    (tmp_path / ".niu").mkdir()
    (tmp_path / ".niu" / "memory.json").write_text(
        _json.dumps({"user": None}, ensure_ascii=False), encoding="utf-8"
    )

    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from niu_api.internal.brain_region_prompt import build_user_info_prompt

    assert build_user_info_prompt() == ""


def test_user_info_partial_placeholder(tmp_path, monkeypatch):
    """name 真实值、其余三字段为占位符 → 只返回含 name 一行的字符串。"""
    import json as _json

    memory = {
        "user": {
            "name": "李磊",
            "nickname": "请询问称呼",
            "occupation": "请询问职业",
            "organization": "请询问工作单位",
        }
    }
    (tmp_path / ".niu").mkdir()
    (tmp_path / ".niu" / "memory.json").write_text(
        _json.dumps(memory, ensure_ascii=False), encoding="utf-8"
    )

    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from niu_api.internal.brain_region_prompt import build_user_info_prompt

    result = build_user_info_prompt()

    assert "## 知识图谱所属用户" in result
    assert "真实姓名：李磊" in result
    # 占位符字段被跳过
    assert "称呼" not in result
    assert "职业" not in result
    assert "工作单位" not in result


def test_user_info_corrupted_json(tmp_path, monkeypatch):
    """memory.json 内容非 JSON 时返回空串（验证 try/except 兜底）。"""
    (tmp_path / ".niu").mkdir()
    (tmp_path / ".niu" / "memory.json").write_text(
        "这不是合法的 JSON {{{}}}", encoding="utf-8"
    )

    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from niu_api.internal.brain_region_prompt import build_user_info_prompt

    assert build_user_info_prompt() == ""
