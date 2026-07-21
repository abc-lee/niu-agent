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


# 探测辅助函数单元测试：
# _classify_probe_response_tier1/tier2 根据响应文本判定 supported/gateway_blocked。
# _build_probe_messages 构造探测 prompt。
# _build_probe_response_format_json_schema / json_object 构造探测 response_format。
# 纯函数，不调真实 LLM。
from niu_api.compat import (
    _classify_probe_response_tier1,
    _classify_probe_response_tier2,
    _build_probe_messages,
    _build_probe_response_format_json_schema,
    _build_probe_response_format_json_object,
)


def test_build_probe_response_format_json_schema_structure():
    """json_schema 档构造冲突式 schema（verdict 枚举单值），用于区分真假支持"""
    rf = _build_probe_response_format_json_schema()
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["name"] == "probe_response_format"
    assert rf["json_schema"]["strict"] is True
    schema = rf["json_schema"]["schema"]
    assert schema["properties"]["verdict"]["enum"] == ["SCHEMA_ENFORCED"]
    assert schema["required"] == ["verdict"]
    assert schema["additionalProperties"] is False


def test_build_probe_response_format_json_object_structure():
    """json_object 档构造 {"type": "json_object"}"""
    rf = _build_probe_response_format_json_object()
    assert rf == {"type": "json_object"}


def test_build_probe_messages_returns_single_user_message_with_json_keyword():
    """探测消息：单条 user 消息；含 "json" 字样（OpenAI json_object 硬性要求 prompt 含 json 字符串）"""
    msgs = _build_probe_messages()
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"
    assert "json" in msgs[0]["content"].lower()


def test_build_probe_messages_conflicts_with_schema():
    """探测消息与 schema 故意矛盾（冲突式设计）：要求普通句子且禁止 JSON 输出"""
    msgs = _build_probe_messages()
    content = msgs[0]["content"].lower()
    assert "ocean" in content
    assert "do not output json" in content


# Tier 1 (json_schema strict) 冲突式设计：只有 verdict == "SCHEMA_ENFORCED" 才算 supported
def test_classify_tier1_supported_when_verdict_schema_enforced():
    """响应是 {"verdict": "SCHEMA_ENFORCED"} → supported（schema 战胜 prompt）"""
    assert _classify_probe_response_tier1('{"verdict": "SCHEMA_ENFORCED"}') == "supported"


def test_classify_tier1_supported_when_verdict_with_extra_fields():
    """响应含 verdict + 额外字段 → supported（容忍额外字段，部分厂商宽松处理 additionalProperties）"""
    assert _classify_probe_response_tier1('{"verdict": "SCHEMA_ENFORCED", "extra": "ignored"}') == "supported"


def test_classify_tier1_gateway_blocked_when_prompt_following_ok_json():
    """关键回归：响应是 {"ok": true}（旧探测设计的"成功"响应）→ gateway_blocked

    旧设计 prompt 与 schema 都要 {"ok": true}，模型跟随 prompt 即假阳性。
    新设计下这只是"模型跟随 prompt 的普通 JSON"，无 verdict → gateway_blocked。
    """
    assert _classify_probe_response_tier1('{"ok": true}') == "gateway_blocked"


def test_classify_tier1_gateway_blocked_when_ocean_sentence():
    """响应是普通英文句子（flaky 网关静默忽略时模型跟随 prompt）→ gateway_blocked"""
    assert _classify_probe_response_tier1(
        'Beneath the sun-dappled surface of the ocean, vibrant coral reefs teem with life.'
    ) == "gateway_blocked"


def test_classify_tier1_gateway_blocked_when_wrong_verdict_value():
    """响应是 {"verdict": "WRONG"}（JSON 合法但 verdict 值不匹配枚举）→ gateway_blocked"""
    assert _classify_probe_response_tier1('{"verdict": "WRONG"}') == "gateway_blocked"


def test_classify_tier1_gateway_blocked_when_plain_text():
    """响应是纯文本 → gateway_blocked"""
    assert _classify_probe_response_tier1('I am doing fine.') == "gateway_blocked"


def test_classify_tier1_gateway_blocked_when_truncated_json():
    """响应是截断的非合法 JSON → gateway_blocked"""
    assert _classify_probe_response_tier1('{"verdict":') == "gateway_blocked"


def test_classify_tier1_gateway_blocked_when_empty():
    """响应空 → gateway_blocked"""
    assert _classify_probe_response_tier1('') == "gateway_blocked"


def test_classify_tier1_gateway_blocked_when_markdown_wrapped():
    """响应是 ```json 包裹的 verdict JSON → gateway_blocked（非纯 JSON，schema strict 不会产 markdown）"""
    assert _classify_probe_response_tier1('```json\n{"verdict": "SCHEMA_ENFORCED"}\n```') == "gateway_blocked"


# Tier 2 (json_object) 不要求含 ok 字段，只要求合法 JSON dict
def test_classify_tier2_supported_when_valid_json_dict():
    """响应是 {"foo": "bar"}（合法 JSON dict，无 ok）→ supported
    json_object 不约束字段名，只要合法 JSON dict 即可"""
    assert _classify_probe_response_tier2('{"foo": "bar"}') == "supported"


def test_classify_tier2_supported_when_json_with_ok_field():
    """响应是 {"ok": true} → supported"""
    assert _classify_probe_response_tier2('{"ok": true}') == "supported"


def test_classify_tier2_gateway_blocked_when_plain_text():
    """响应是纯文本 → gateway_blocked"""
    assert _classify_probe_response_tier2('I am doing fine.') == "gateway_blocked"


def test_classify_tier2_gateway_blocked_when_truncated_json():
    """响应是 {"ok": true}\\n} （GLM json_object 实测含额外字符）→ gateway_blocked"""
    assert _classify_probe_response_tier2('{"ok": true}\n}') == "gateway_blocked"


def test_classify_tier2_gateway_blocked_when_empty():
    """响应空 → gateway_blocked"""
    assert _classify_probe_response_tier2('') == "gateway_blocked"


"""升级后自动探测决策函数测试。

_should_auto_probe_after_upgrade 判断 lightrag_llm.litellm_kwargs 是否需要自动探测：
- 无 response_format_mode 键 → True（旧版本配置，需探测）
- 有 response_format_mode 键 → False（已探测过）

v6 修正：同时检查 lightrag_llm 和 llm 两段 litellm_kwargs，因为
lightrag_llm.model 为空时 get_llm_config 走 fallback 用 llm 段，
response_format_mode 可能写在 llm 段（场景二/三：LightRAG 用主 Agent 同一模型）。
"""
from niu_api.internal.lightrag_manager import _should_auto_probe_after_upgrade


def test_returns_true_when_no_response_format_mode():
    """lightrag_llm.litellm_kwargs 无 response_format_mode 键 → True（旧版本）"""
    config = {"lightrag_llm": {"litellm_kwargs": {"thinking": {"type": "disabled"}}}}
    assert _should_auto_probe_after_upgrade(config) is True


def test_returns_true_when_no_litellm_kwargs():
    """lightrag_llm 无 litellm_kwargs 键 → True"""
    config = {"lightrag_llm": {"reasoning_effort": "none"}}
    assert _should_auto_probe_after_upgrade(config) is True


def test_returns_true_when_no_lightrag_llm():
    """配置无 lightrag_llm 段 → True"""
    config = {}
    assert _should_auto_probe_after_upgrade(config) is True


def test_returns_false_when_response_format_mode_exists():
    """lightrag_llm.litellm_kwargs 含 response_format_mode 键 → False（已探测过）"""
    config = {"lightrag_llm": {"litellm_kwargs": {"response_format_mode": "prompt_only"}}}
    assert _should_auto_probe_after_upgrade(config) is False


def test_returns_false_when_response_format_mode_is_any_value():
    """response_format_mode 是任意值（含 prompt_only）都算已探测过 → False"""
    config = {"lightrag_llm": {"litellm_kwargs": {"response_format_mode": "json_schema"}}}
    assert _should_auto_probe_after_upgrade(config) is False


def test_returns_false_when_llm_has_response_format_mode():
    """llm.litellm_kwargs 含 response_format_mode（lightrag_llm 为空场景）→ False

    场景二/三：LightRAG 用主 Agent 同一模型，response_format_mode 写在 llm 段。
    """
    config = {"llm": {"litellm_kwargs": {"response_format_mode": "prompt_only"}}}
    assert _should_auto_probe_after_upgrade(config) is False


def test_returns_true_when_only_llm_has_litellm_kwargs_without_mode():
    """llm.litellm_kwargs 有内容但无 response_format_mode 键 → True（需探测）"""
    config = {"llm": {"litellm_kwargs": {"thinking": {"type": "enabled"}}}}
    assert _should_auto_probe_after_upgrade(config) is True


"""端到端集成测试：调用 /api/probe-response-format 端点。

需启动 niu_api 服务（端口 9876）。验证三种探测档位路径 + 两个真实配置。
"""
import json
import os
import pytest
import httpx


@pytest.fixture
def api_base():
    return "http://127.0.0.1:9876"


def _load_user_llm_config() -> dict | None:
    """加载 user-config.json 的 lightrag_llm 配置（fallback 到 llm 段，近似 get_llm_config 语义）

    近似运行时 get_llm_config（llm_proxy.py L209-241）语义，仅覆盖当前豆包/GLM
    实际配置形态（Branch 2：lightrag_llm.model 为空）。Branch 1（lightrag_llm.model
    非空）场景下的完整继承逻辑未复刻，未来如需支持需按 llm_proxy.py L213-222
    补五个继承块。
    """
    config_path = os.path.join(os.path.dirname(__file__), "..", "config", "user-config.json")
    if not os.path.exists(config_path):
        return None
    with open(config_path) as f:
        cfg = json.load(f)
    lightrag_llm = cfg.get("lightrag_llm", {})
    llm = cfg.get("llm", {})

    # Branch 2：lightrag_llm.model 为空，fallback 到 llm 段
    # apiKey/apiBase/model/type 只用 llm 段（lightrag_llm 的这些字段被忽略）
    # provider/temperature/litellm_kwargs 优先 lightrag_llm、空则 llm
    return {
        "apikey": llm.get("apiKey", ""),
        "apibase": llm.get("apiBase", ""),
        "model": llm.get("model", ""),
        "type": llm.get("type", "openai"),
        "provider": lightrag_llm.get("provider") or llm.get("provider", ""),
        "temperature": lightrag_llm["temperature"] if lightrag_llm.get("temperature") is not None else llm.get("temperature", 0.2),
        "litellm_kwargs": lightrag_llm.get("litellm_kwargs") or llm.get("litellm_kwargs") or {},
    }


def _load_glm_llm_config() -> dict | None:
    """加载 GLM 配置（从独立文件 config/user-config - glm.json，与前端发送逻辑一致）

    litellm_kwargs 优先 lightrag_llm 段、空则 llm 段（与前端 settings/index.html L410
    实际发送逻辑一致：lightrag_llm?.litellm_kwargs || llm?.litellm_kwargs || {}）
    provider 优先 lightrag_llm 段、空则 llm 段（与 _load_user_llm_config 统一）
    """
    config_path = os.path.join(os.path.dirname(__file__), "..", "config", "user-config - glm.json")
    if not os.path.exists(config_path):
        return None
    with open(config_path) as f:
        cfg = json.load(f)
    lightrag_llm = cfg.get("lightrag_llm", {})
    llm = cfg.get("llm", {})
    return {
        "apikey": llm.get("apiKey", ""),
        "apibase": llm.get("apiBase", ""),
        "model": llm.get("model", ""),
        "type": llm.get("type", "openai"),
        "provider": lightrag_llm.get("provider") or llm.get("provider", ""),
        "temperature": llm.get("temperature", 0.2),
        "litellm_kwargs": lightrag_llm.get("litellm_kwargs") or llm.get("litellm_kwargs") or {},
    }


@pytest.mark.timeout(600)  # 突破 pytest.ini 全局 timeout=30，新探测最坏 ~500s
def test_probe_endpoint_returns_prompt_only_for_doubao_coding(api_base):
    """豆包 Coding Plan 网关行为非确定性（flaky），三次采样必然 ≥1 次
    静默忽略 → 稳定降级 prompt_only

    已知抖动率：flaky 网关执行率约 2/5，P(Tier1 三样本全过)≈6.4%，Tier 2 同理。
    本测试断言 prompt_only 有 ~6% 偶发失败率，偶发失败可重跑。
    """
    config = _load_user_llm_config()
    if not config:
        pytest.skip("无 user-config.json")
    if "coding" not in config.get("apibase", ""):  # 全小写键，与 helper 返回一致
        pytest.skip("非豆包 Coding Plan 端点")

    # 三次采样 + 限流/超时重试最坏耗时 ~500s（两档），设 600s 余量
    client = httpx.Client(timeout=600.0)
    with client:
        resp = client.post(f"{api_base}/api/probe-response-format", json=config)
    assert resp.status_code == 200
    data = resp.json()
    # flaky 网关三次采样必然 ≥1 次静默忽略 → 稳定降级 prompt_only（~94% 概率）
    assert data["mode"] == "prompt_only", f"豆包 Coding Plan flaky 网关应稳定降级 prompt_only，实际: {data}"
    # reason 应含 Tier 1 失败信息（gateway_blocked 或 model_rejected）
    reason = data.get("reason", "")
    assert "Tier 1" in reason, f"reason 应含 Tier 1 失败信息，实际: {reason}"


@pytest.mark.timeout(600)  # 突破 pytest.ini 全局 timeout=30
def test_probe_endpoint_returns_prompt_only_for_glm(api_base):
    """GLM 网关接受但模型输出漂移，三次采样必然 ≥1 次漂移 → 稳定降级 prompt_only

    已知抖动率：GLM 漂移率较高，P(Tier1 三样本全过) 极低，但理论上非零。
    偶发失败可重跑。
    """
    config = _load_glm_llm_config()
    if not config:
        pytest.skip("无 GLM 配置")

    # 三次采样 + 限流/超时重试最坏耗时 ~500s（两档），设 600s 余量
    client = httpx.Client(timeout=600.0)
    with client:
        resp = client.post(f"{api_base}/api/probe-response-format", json=config)
    assert resp.status_code == 200
    data = resp.json()
    # GLM 输出漂移，三次采样必然 ≥1 次非合法 JSON → 稳定降级 prompt_only
    assert data["mode"] == "prompt_only", f"GLM 应稳定降级 prompt_only（输出漂移），实际: {data}"
    reason = data.get("reason", "")
    assert "Tier 1" in reason, f"reason 应含 Tier 1 失败信息，实际: {reason}"


@pytest.mark.timeout(600)  # 突破 pytest.ini 全局 timeout=30
def test_probe_endpoint_returns_json_schema_for_openai(api_base):
    """用 OpenAI 真实 API Key 测试（需环境变量 OPENAI_API_KEY）"""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        pytest.skip("OPENAI_API_KEY 未设置，跳过真实 OpenAI 探测测试")
    config = {
        "apiKey": api_key,
        "apiBase": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "type": "openai",
        "provider": "",
    }
    # 三次采样最坏耗时 ~90s（3 次 × 30s 超时），设 600s 余量
    client = httpx.Client(timeout=600.0)
    with client:
        resp = client.post(f"{api_base}/api/probe-response-format", json=config)
    assert resp.status_code == 200
    data = resp.json()
    assert data["result"] in {"supported", "probe_failed"}
    # OpenAI 应支持 json_schema strict（三次采样全过）
    assert data["mode"] == "json_schema", f"OpenAI 应支持 json_schema，实际: {data}"


def test_probe_endpoint_returns_probe_failed_for_invalid_config(api_base):
    """无效配置（缺 apikey）应返回 probe_failed"""
    config = {"apiKey": "", "apiBase": "", "model": ""}
    with httpx.Client(timeout=10) as client:
        resp = client.post(f"{api_base}/api/probe-response-format", json=config)
    assert resp.status_code == 200
    data = resp.json()
    assert data["result"] == "probe_failed"
    assert data["mode"] is None


def test_probe_endpoint_does_not_affect_test_llm_endpoint(api_base):
    """验证 /api/test-llm 响应结构未被探测逻辑污染（启动器依赖 {success, message, error}）"""
    config = {"apiKey": "fake-key", "apiBase": "https://api.openai.com/v1", "model": "gpt-4o-mini"}
    # timeout=30：/api/test-llm 内部 asyncio.wait_for(timeout=20) + 网络往返余量
    with httpx.Client(timeout=30) as client:
        resp = client.post(f"{api_base}/api/test-llm", json=config)
    assert resp.status_code == 200
    data = resp.json()
    # 必须只有 success/message/error 三字段（启动器 TestLlmResult 结构体依赖）
    assert "success" in data
    # 不能有 result/mode/raw_response 等探测字段
    assert "result" not in data
    assert "mode" not in data


# ===== 三次采样逻辑测试 =====

@pytest.mark.asyncio
async def test_probe_tier_three_samples_all_pass_returns_supported():
    """三次采样全 supported → 该档 supported"""
    from niu_api.compat import _probe_tier_three_samples_async
    from unittest.mock import AsyncMock

    mock_try = AsyncMock(return_value=("supported", '{"verdict": "SCHEMA_ENFORCED"}'))
    result, raw = await _probe_tier_three_samples_async(mock_try, {"type": "json_schema"})
    assert result == "supported"
    assert raw == ""
    assert mock_try.call_count == 3


@pytest.mark.asyncio
async def test_probe_tier_one_gateway_blocked_returns_failed():
    """三次采样中任何一次 gateway_blocked → 该档失败"""
    from niu_api.compat import _probe_tier_three_samples_async
    from unittest.mock import AsyncMock

    mock_try = AsyncMock(side_effect=[
        ("supported", '{"verdict": "SCHEMA_ENFORCED"}'),
        ("gateway_blocked", "ocean sentence"),
        ("supported", '{"verdict": "SCHEMA_ENFORCED"}'),
    ])
    result, raw = await _probe_tier_three_samples_async(mock_try, {"type": "json_schema"})
    assert result == "gateway_blocked"
    assert raw == "ocean sentence"
    assert mock_try.call_count == 2


@pytest.mark.asyncio
async def test_probe_tier_one_model_rejected_returns_failed():
    """三次采样中任何一次 model_rejected → 该档失败"""
    from niu_api.compat import _probe_tier_three_samples_async
    from unittest.mock import AsyncMock

    mock_try = AsyncMock(side_effect=[
        ("supported", '{"verdict": "SCHEMA_ENFORCED"}'),
        ("model_rejected", "BadRequestError: 400"),
    ])
    result, raw = await _probe_tier_three_samples_async(mock_try, {"type": "json_schema"})
    assert result == "model_rejected"
    assert raw == "BadRequestError: 400"
    assert mock_try.call_count == 2


@pytest.mark.asyncio
async def test_probe_tier_rate_limit_retries_without_counting():
    """限流只重试不计失败，直到返回非限流结果"""
    from niu_api.compat import _probe_tier_three_samples_async
    from unittest.mock import AsyncMock, patch

    mock_try = AsyncMock(side_effect=[
        ("rate_limited", "RateLimitError: 429"),
        ("supported", '{"verdict": "SCHEMA_ENFORCED"}'),
        ("supported", '{"verdict": "SCHEMA_ENFORCED"}'),
        ("supported", '{"verdict": "SCHEMA_ENFORCED"}'),
    ])
    with patch("niu_api.compat._asyncio_sleep", new_callable=AsyncMock):
        result, raw = await _probe_tier_three_samples_async(mock_try, {"type": "json_schema"})
    assert result == "supported"
    assert raw == ""
    assert mock_try.call_count == 4


@pytest.mark.asyncio
async def test_probe_tier_timeout_retries_without_counting():
    """超时同限流处理：只重试不计失败（asyncio.TimeoutError + litellm.Timeout）"""
    from niu_api.compat import _probe_tier_three_samples_async
    from unittest.mock import AsyncMock, patch
    import asyncio

    mock_try = AsyncMock(side_effect=[
        asyncio.TimeoutError(),
        ("supported", '{"verdict": "SCHEMA_ENFORCED"}'),
        ("supported", '{"verdict": "SCHEMA_ENFORCED"}'),
        ("supported", '{"verdict": "SCHEMA_ENFORCED"}'),
    ])
    with patch("niu_api.compat._asyncio_sleep", new_callable=AsyncMock):
        result, raw = await _probe_tier_three_samples_async(mock_try, {"type": "json_schema"})
    assert result == "supported"
    assert raw == ""
    assert mock_try.call_count == 4

    mock_try2 = AsyncMock(side_effect=[
        ("timeout", "litellm.Timeout: APITimeoutError"),
        ("supported", '{"verdict": "SCHEMA_ENFORCED"}'),
        ("supported", '{"verdict": "SCHEMA_ENFORCED"}'),
        ("supported", '{"verdict": "SCHEMA_ENFORCED"}'),
    ])
    with patch("niu_api.compat._asyncio_sleep", new_callable=AsyncMock):
        result2, raw2 = await _probe_tier_three_samples_async(mock_try2, {"type": "json_schema"})
    assert result2 == "supported"
    assert raw2 == ""
    assert mock_try2.call_count == 4


@pytest.mark.asyncio
async def test_probe_tier_rate_limit_exhausted_returns_error():
    """限流/超时重试超过上限（整档共享 5 次）仍未成功 → 返回 rate_limited"""
    from niu_api.compat import _probe_tier_three_samples_async
    from unittest.mock import AsyncMock, patch

    mock_try = AsyncMock(return_value=("rate_limited", "RateLimitError: 429"))
    with patch("niu_api.compat._asyncio_sleep", new_callable=AsyncMock):
        result, raw = await _probe_tier_three_samples_async(mock_try, {"type": "json_schema"})
    assert result == "rate_limited"
    assert raw == "RateLimitError: 429"
    assert mock_try.call_count == 6


@pytest.mark.asyncio
async def test_probe_tier_transient_retries_shared_across_samples():
    """限流/超时重试预算整档共享：采样 1 限流 3 次 + 采样 2 限流 3 次 → 第 6 次返回 rate_limited"""
    from niu_api.compat import _probe_tier_three_samples_async
    from unittest.mock import AsyncMock, patch

    mock_try = AsyncMock(side_effect=[
        ("rate_limited", "RateLimitError: 429"),
        ("rate_limited", "RateLimitError: 429"),
        ("rate_limited", "RateLimitError: 429"),
        ("supported", '{"verdict": "SCHEMA_ENFORCED"}'),
        ("rate_limited", "RateLimitError: 429"),
        ("rate_limited", "RateLimitError: 429"),
        ("rate_limited", "RateLimitError: 429"),
    ])
    with patch("niu_api.compat._asyncio_sleep", new_callable=AsyncMock):
        result, raw = await _probe_tier_three_samples_async(mock_try, {"type": "json_schema"})
    assert result == "rate_limited"
    assert raw == "RateLimitError: 429"
    assert mock_try.call_count == 7


@pytest.mark.asyncio
async def test_probe_tier_infra_error_returns_immediately():
    """任何一次基础设施错误（401/网络断/500）→ 立即返回 infra_error，不写配置"""
    from niu_api.compat import _probe_tier_three_samples_async
    from unittest.mock import AsyncMock

    mock_try = AsyncMock(side_effect=[
        ("supported", '{"verdict": "SCHEMA_ENFORCED"}'),
        ("infra_error", "AuthenticationError: 401"),
    ])
    result, raw = await _probe_tier_three_samples_async(mock_try, {"type": "json_schema"})
    assert result == "infra_error"
    assert raw == "AuthenticationError: 401"
    assert mock_try.call_count == 2


@pytest.mark.asyncio
async def test_probe_returns_probe_failed_when_rate_limited():
    """端点探测限流/超时重试耗尽 → 返回 probe_failed + rate_limited reason"""
    from niu_api.compat import probe_response_format
    from unittest.mock import AsyncMock, patch
    from fastapi import Request

    with patch("niu_api.compat._probe_tier_three_samples_async", new_callable=AsyncMock) as mock_sampler:
        mock_sampler.return_value = ("rate_limited", "RateLimitError: 429")

        mock_request = AsyncMock(spec=Request)
        mock_request.json = AsyncMock(return_value={})

        with patch("niu_api.llm_proxy.get_llm_config") as mock_get_config:
            mock_get_config.return_value = {
                "apikey": "test-key",
                "apibase": "https://test.example.com",
                "model": "test-model",
                "type": "openai",
                "litellm_kwargs": {},
            }

            result = await probe_response_format(mock_request)

    assert result["result"] == "probe_failed"
    assert "限流" in result["reason"]
    assert result["mode"] is None


@pytest.mark.asyncio
async def test_probe_returns_probe_failed_when_infra_error():
    """端点探测遇基础设施错误（401/网络断/500）→ 返回 probe_failed + infra_error reason，不写配置"""
    from niu_api.compat import probe_response_format
    from unittest.mock import AsyncMock, patch
    from fastapi import Request

    with patch("niu_api.compat._probe_tier_three_samples_async", new_callable=AsyncMock) as mock_sampler:
        mock_sampler.return_value = ("infra_error", "AuthenticationError: 401")

        mock_request = AsyncMock(spec=Request)
        mock_request.json = AsyncMock(return_value={})

        with patch("niu_api.llm_proxy.get_llm_config") as mock_get_config:
            mock_get_config.return_value = {
                "apikey": "test-key",
                "apibase": "https://test.example.com",
                "model": "test-model",
                "type": "openai",
                "litellm_kwargs": {},
            }

            result = await probe_response_format(mock_request)

    assert result["result"] == "probe_failed"
    assert "基础设施错误" in result["reason"]
    assert result["mode"] is None


# ===== _try_tier 异常分类端点级测试 =====
# 验证 _try_tier 捕获各类 litellm 异常时正确分类返回值。mock LiteLLMSession.chat
# 抛异常，绕过真实 LLM 调用，端点级覆盖从异常到 result 的完整路径。

@pytest.mark.asyncio
async def test_try_tier_classifies_rate_limit_error():
    """_try_tier 捕获 RateLimitError → rate_limited"""
    from niu_api.compat import probe_response_format
    from unittest.mock import AsyncMock, patch, MagicMock
    from fastapi import Request
    from litellm import RateLimitError

    mock_request = AsyncMock(spec=Request)
    mock_request.json = AsyncMock(return_value={})

    with patch("niu_api.llm_proxy.get_llm_config") as mock_get_config:
        mock_get_config.return_value = {
            "apikey": "test-key",
            "apibase": "https://test.example.com",
            "model": "test-model",
            "type": "openai",
            "litellm_kwargs": {},
        }

        # mock LiteLLMSession.chat 抛 RateLimitError（litellm 异常需要 model + llm_provider 必填 kwarg）
        with patch("agent.generic.litellm_adapter.LiteLLMSession") as mock_session_class:
            mock_session = MagicMock()
            mock_session.chat.side_effect = RateLimitError(
                "429 rate limit", model="test-model", llm_provider="openai"
            )
            mock_session_class.return_value = mock_session

            # mock _asyncio_sleep 避免真实等待
            with patch("niu_api.compat._asyncio_sleep", new_callable=AsyncMock):
                result = await probe_response_format(mock_request)

    # 限流重试 5 次后返回 probe_failed
    assert result["result"] == "probe_failed"
    assert "限流" in result["reason"]


@pytest.mark.asyncio
async def test_try_tier_classifies_litellm_timeout():
    """_try_tier 捕获 litellm.Timeout → timeout（与 rate_limited 同等待遇）"""
    from niu_api.compat import probe_response_format
    from unittest.mock import AsyncMock, patch, MagicMock
    from fastapi import Request
    import litellm

    mock_request = AsyncMock(spec=Request)
    mock_request.json = AsyncMock(return_value={})

    with patch("niu_api.llm_proxy.get_llm_config") as mock_get_config:
        mock_get_config.return_value = {
            "apikey": "test-key",
            "apibase": "https://test.example.com",
            "model": "test-model",
            "type": "openai",
            "litellm_kwargs": {},
        }

        with patch("agent.generic.litellm_adapter.LiteLLMSession") as mock_session_class:
            mock_session = MagicMock()
            mock_session.chat.side_effect = litellm.Timeout(
                "APITimeoutError", model="test-model", llm_provider="openai"
            )
            mock_session_class.return_value = mock_session

            with patch("niu_api.compat._asyncio_sleep", new_callable=AsyncMock):
                result = await probe_response_format(mock_request)

    # 超时重试 5 次后返回 probe_failed
    assert result["result"] == "probe_failed"
    assert "限流" in result["reason"] or "超时" in result["reason"]


@pytest.mark.asyncio
async def test_try_tier_classifies_authentication_error():
    """_try_tier 捕获 AuthenticationError → infra_error → probe_failed 不写配置"""
    from niu_api.compat import probe_response_format
    from unittest.mock import AsyncMock, patch, MagicMock
    from fastapi import Request
    from litellm import AuthenticationError

    mock_request = AsyncMock(spec=Request)
    mock_request.json = AsyncMock(return_value={})

    with patch("niu_api.llm_proxy.get_llm_config") as mock_get_config:
        mock_get_config.return_value = {
            "apikey": "test-key",
            "apibase": "https://test.example.com",
            "model": "test-model",
            "type": "openai",
            "litellm_kwargs": {},
        }

        with patch("agent.generic.litellm_adapter.LiteLLMSession") as mock_session_class:
            mock_session = MagicMock()
            mock_session.chat.side_effect = AuthenticationError(
                "401 invalid api key", model="test-model", llm_provider="openai"
            )
            mock_session_class.return_value = mock_session

            result = await probe_response_format(mock_request)

    # 基础设施错误立即返回 probe_failed（不重试）
    assert result["result"] == "probe_failed"
    assert "基础设施错误" in result["reason"]


@pytest.mark.asyncio
async def test_try_tier_classifies_bad_request_error():
    """_try_tier 捕获 BadRequestError → model_rejected → 降级 prompt_only"""
    from niu_api.compat import probe_response_format
    from unittest.mock import AsyncMock, patch, MagicMock
    from fastapi import Request
    from litellm import BadRequestError

    mock_request = AsyncMock(spec=Request)
    mock_request.json = AsyncMock(return_value={})

    with patch("niu_api.llm_proxy.get_llm_config") as mock_get_config:
        mock_get_config.return_value = {
            "apikey": "test-key",
            "apibase": "https://test.example.com",
            "model": "test-model",
            "type": "openai",
            "litellm_kwargs": {},
        }

        with patch("agent.generic.litellm_adapter.LiteLLMSession") as mock_session_class:
            mock_session = MagicMock()
            mock_session.chat.side_effect = BadRequestError(
                "400 response_format not supported", model="test-model", llm_provider="openai"
            )
            mock_session_class.return_value = mock_session

            result = await probe_response_format(mock_request)

    # model_rejected 降级 prompt_only
    assert result["result"] == "supported"
    assert result["mode"] == "prompt_only"
