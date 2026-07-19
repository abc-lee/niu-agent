"""response_format 决策函数单元测试。

验证 _resolve_response_format 根据 litellm_kwargs.response_format_mode
决定构造哪种 response_format。这是纯函数，不调 LLM，仅检查配置。
"""
from niu_api.internal.lightrag_manager import (
    _resolve_response_format,
    _strip_response_format_mode,
)


def test_json_schema_mode_returns_json_schema_response_format():
    """response_format_mode=json_schema → 返回 json_schema strict 结构"""
    config = {"litellm_kwargs": {"response_format_mode": "json_schema"}}
    rf = _resolve_response_format(config)
    assert rf is not None
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["name"] == "keyword_extraction"


def test_json_object_mode_returns_json_object_response_format():
    """response_format_mode=json_object → 返回 {"type": "json_object"}"""
    config = {"litellm_kwargs": {"response_format_mode": "json_object"}}
    rf = _resolve_response_format(config)
    assert rf == {"type": "json_object"}


def test_prompt_only_mode_returns_none():
    """response_format_mode=prompt_only → 返回 None（不构造）"""
    config = {"litellm_kwargs": {"response_format_mode": "prompt_only"}}
    rf = _resolve_response_format(config)
    assert rf is None


def test_missing_mode_returns_none():
    """litellm_kwargs 无 response_format_mode 键 → 返回 None（保守降级）"""
    config = {"litellm_kwargs": {}}
    rf = _resolve_response_format(config)
    assert rf is None


def test_missing_litellm_kwargs_returns_none():
    """配置无 litellm_kwargs 键 → 返回 None"""
    config = {}
    rf = _resolve_response_format(config)
    assert rf is None


def test_unknown_mode_returns_none():
    """response_format_mode 是未知值 → 返回 None（保守降级）"""
    config = {"litellm_kwargs": {"response_format_mode": "unknown_mode"}}
    rf = _resolve_response_format(config)
    assert rf is None


def test_legacy_allowed_openai_params_still_supported():
    """旧配置只有 allowed_openai_params=["response_format"]（无 response_format_mode）
    → 兼容旧配置，返回 json_schema（默认最强档）

    Why: 旧版本用户配置文件没有 response_format_mode 字段，本次升级不应破坏。
    升级后首次启动后台探测会自动写入 response_format_mode。
    """
    config = {"litellm_kwargs": {"allowed_openai_params": ["response_format"]}}
    rf = _resolve_response_format(config)
    assert rf is not None
    assert rf["type"] == "json_schema"


def test_legacy_empty_allowed_openai_params_returns_none():
    """旧配置 allowed_openai_params=[]（无 response_format_mode）→ 返回 None"""
    config = {"litellm_kwargs": {"allowed_openai_params": []}}
    rf = _resolve_response_format(config)
    assert rf is None


def test_json_schema_mode_with_thinking_kwargs():
    """豆包配置：response_format_mode=json_schema + thinking={type:disabled} 共存

    验证 thinking 参数不影响 response_format 决策。
    """
    config = {"litellm_kwargs": {
        "response_format_mode": "json_schema",
        "thinking": {"type": "disabled"},
        "allowed_openai_params": ["response_format"],
    }}
    rf = _resolve_response_format(config)
    assert rf is not None
    assert rf["type"] == "json_schema"


def test_resolve_does_not_modify_config_no_side_effects():
    """_resolve_response_format 不修改 config（无副作用）

    Why: v4 曾用 pop 副作用修改 config，导致 keyword_extraction=True 与 False
    两种调用模式的 config_key 不一致，破坏 _get_litellm_session 缓存。
    v5 改为 get（无副作用），response_format_mode 字段在 _llm_model_func 内
    通过 _strip_response_format_mode 单独剔除。
    """
    config = {"litellm_kwargs": {
        "response_format_mode": "json_schema",
        "thinking": {"type": "disabled"},
        "allowed_openai_params": ["response_format"],
    }}
    original = {"litellm_kwargs": dict(config["litellm_kwargs"])}
    _resolve_response_format(config)
    # config 不应被修改
    assert config["litellm_kwargs"] == original["litellm_kwargs"]
    assert "response_format_mode" in config["litellm_kwargs"]


def test_strip_response_format_mode_returns_new_dict_without_mode():
    """_strip_response_format_mode 返回新 dict，剔除 response_format_mode"""
    config = {"litellm_kwargs": {
        "response_format_mode": "json_schema",
        "thinking": {"type": "disabled"},
        "allowed_openai_params": ["response_format"],
    }}
    stripped = _strip_response_format_mode(config)
    # 原config不修改
    assert "response_format_mode" in config["litellm_kwargs"]
    # 新config剔除 response_format_mode
    assert "response_format_mode" not in stripped["litellm_kwargs"]
    # 其他字段保留
    assert stripped["litellm_kwargs"]["thinking"] == {"type": "disabled"}
    assert stripped["litellm_kwargs"]["allowed_openai_params"] == ["response_format"]


def test_strip_response_format_mode_returns_same_dict_when_no_mode():
    """无 response_format_mode 键时，_strip 返回原 config（不复制）"""
    config = {"litellm_kwargs": {"thinking": {"type": "disabled"}}}
    stripped = _strip_response_format_mode(config)
    # 无需复制，返回原对象
    assert stripped is config


def test_strip_response_format_mode_handles_missing_litellm_kwargs():
    """config 无 litellm_kwargs 键时，_strip 返回原 config"""
    config = {}
    stripped = _strip_response_format_mode(config)
    assert stripped is config
