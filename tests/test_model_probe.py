"""模型能力探测器核心单测（组件 1，Task 2）。

覆盖：
① 值域判定（200→supported / 400+e.body 含 reasoning_effort→unsupported 继续 /
   401→failed 终止不覆盖旧档）
② partial 例外（response_format 超时→partial + 值域照写 + timeout 形状；tools
   失败→partial + tools 段形状；rf 400→unsupported 形状）
③ thinking 剔除行为（config 含 litellm_kwargs.thinking 时探测请求 thinking 仅
   来自 raw 候选——传入 assemble_request_params 的 config 副本不含 thinking 键）
④ thinking 状态聚合枚举（双 true→ok / 一 false→partial / 双 false→partial）
⑤ ignores_unknown 置位（7×200→true；6×200+1×400→false）
⑥ 档案读写/键控覆盖（llm/lightrag 双键）/键规范化（rstrip("/")）/flock 锁竞争跳过
⑦ mock 按 litellm 异常构造（BadRequestError(body={...})——e.response.text 空、
   分类用 e.body）；探测请求传输形态（前缀推导 model + build_base_params +
   extra_body raw 注入 + 本地模型免 apiKey）

禁真实 LLM：所有请求 patch litellm.completion。
"""

import fcntl
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from niu_api import model_probe  # noqa: E402
from niu_api.model_probe import (  # noqa: E402
    REASONING_EFFORT_CANDIDATES,
    build_profile_key,
    load_profile,
    probe,
    read_profile,
    write_profile,
)


# ---------------------------------------------------------------------------
# mock 工具
# ---------------------------------------------------------------------------


def _ok_response(**message_attrs):
    """构造 200 响应 mock（choices[0].message）。"""
    msg = SimpleNamespace(content="OK", **message_attrs)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def _bad_request(body, message="bad request"):
    """按 litellm 异常构造：BadRequestError(body={...})——e.response.text 空，分类用 e.body。"""
    from litellm import BadRequestError

    return BadRequestError(message, model="probe-model", llm_provider="openai", body=body)


def _auth_error():
    from litellm import AuthenticationError

    return AuthenticationError("401 invalid api key", llm_provider="openai", model="probe-model")


def _timeout_error():
    from litellm import Timeout

    return Timeout("request timed out", model="probe-model", llm_provider="openai")


def _patch_completion(*results):
    """patch model_probe.litellm.completion，按顺序返回/抛出 results（耗尽即断言失败）。"""
    return patch("niu_api.model_probe.litellm.completion", side_effect=list(results))


PROBE_ARGS = dict(
    api_base="https://api.example.com/v1/",
    api_key="k-llm",
    model="m1",
    api_type="openai",
)

USER_CONFIG = {
    "llm": {
        "apiKey": "k-llm",
        "apiBase": "https://api.example.com/v1/",
        "model": "m1",
        "type": "openai",
        "reasoning_effort": "high",
        "litellm_kwargs": {"thinking": {"type": "enabled"}},
    },
    "lightrag_llm": {
        "apiKey": "k-lightrag",
        "apiBase": "https://api.example.com/v1/",
        "model": "m1",
        "type": "openai",
        "reasoning_effort": "",
        "litellm_kwargs": {"thinking": {"type": "disabled"}},
    },
}


@pytest.fixture
def profile_path(tmp_path):
    """隔离档案路径（patch PROFILE_PATH——不碰真实 ~/.niu）。"""
    path = tmp_path / "model_capabilities.json"
    with patch.object(model_probe, "PROFILE_PATH", path):
        yield path


# ---------------------------------------------------------------------------
# ① 值域判定
# ---------------------------------------------------------------------------


def test_value_domain_200_marks_supported(profile_path):
    """值域 200 → supported（7 值全收）。"""
    with _patch_completion(*[_ok_response()] * 11) as mock_completion:
        profile = probe(**PROBE_ARGS, user_config=USER_CONFIG)
    assert profile["probe_status"] == "ok"
    assert profile["reasoning_effort"]["supported"] == REASONING_EFFORT_CANDIDATES
    assert profile["reasoning_effort"]["unsupported"] == []
    assert mock_completion.call_count == 11  # 7 值域 + 2 thinking + rf + tools


def test_value_domain_400_with_reasoning_effort_token_marks_unsupported(profile_path):
    """400 + e.body 含 "reasoning_effort" → unsupported，继续探测（值域不连续）。"""
    results = [_ok_response()] * 2  # minimal/low
    results.append(_bad_request({"error": {"message": "reasoning_effort: invalid value 'medium'"}}))
    results += [_ok_response()] * 4  # 剩余 4 个值
    results += [_ok_response()] * 2  # thinking
    results += [_ok_response()] * 2  # rf + tools
    with _patch_completion(*results):
        profile = probe(**PROBE_ARGS, user_config=USER_CONFIG)
    assert profile["probe_status"] == "ok"  # 值域探测完成（结果是不支持部分值）
    assert profile["reasoning_effort"]["supported"] == ["minimal", "low", "high", "xhigh", "none", "max"]
    assert profile["reasoning_effort"]["unsupported"] == ["medium"]
    assert profile["ignores_unknown"] is False


def test_value_domain_other_400_fails_and_terminates(profile_path):
    """400 但 body 不含 reasoning_effort（max_tokens 过小/模型名错）→ failed 终止。"""
    results = [_ok_response()]
    results.append(_bad_request({"error": {"message": "max_tokens must be at least 16"}}))
    with _patch_completion(*results) as mock_completion:
        profile = probe(**PROBE_ARGS, user_config=USER_CONFIG)
    assert profile["probe_status"] == "failed"
    assert mock_completion.call_count == 2  # 立即终止，不再探测
    assert profile["reasoning_effort"]["supported"] == ["minimal"]


def test_value_domain_401_fails_and_does_not_overwrite_old_profile(profile_path):
    """401 → failed 终止，不覆盖旧档案。"""
    old = {"api_base": "https://api.example.com/v1", "model": "m1",
           "probe_status": "ok", "probed_at": "2026-01-01T00:00:00"}
    write_profile(dict(old))
    results = [_ok_response(), _auth_error()]
    with _patch_completion(*results) as mock_completion:
        profile = probe(**PROBE_ARGS, user_config=USER_CONFIG)
    assert profile["probe_status"] == "failed"
    assert mock_completion.call_count == 2
    # 旧档保留（含 api_base 尾部斜杠差异也能命中——键规范化）
    saved = read_profile("https://api.example.com/v1/", "m1")
    assert saved is not None
    assert saved["probe_status"] == "ok"
    assert saved["probed_at"] == "2026-01-01T00:00:00"


# ---------------------------------------------------------------------------
# ② partial 例外（response_format / tools 子项失败仅降级 partial，值域照写）
# ---------------------------------------------------------------------------


def test_response_format_timeout_is_partial_with_value_domain_written(profile_path):
    """response_format 超时 → partial + 值域照写 + response_format 段 timeout 形状。"""
    results = [_ok_response()] * 7
    results += [_ok_response()] * 2  # thinking 双 true
    results.append(_timeout_error())  # response_format 挂起
    results.append(_ok_response())    # tools ok
    with _patch_completion(*results):
        profile = probe(**PROBE_ARGS, user_config=USER_CONFIG)
    assert profile["probe_status"] == "partial"
    assert profile["reasoning_effort"]["supported"] == REASONING_EFFORT_CANDIDATES
    assert profile["thinking"] == {"enabled": True, "disabled": True, "returns_reasoning_content": False}
    assert profile["response_format"] == {"status": "timeout", "supported": []}
    assert profile["tools"] == {"status": "ok", "supported": ["probe_tool"]}
    # 值域照写档案（partial 也落盘）
    saved = read_profile("https://api.example.com/v1/", "m1")
    assert saved is not None
    assert saved["probe_status"] == "partial"
    assert saved["response_format"] == {"status": "timeout", "supported": []}
    assert saved["reasoning_effort"]["supported"] == REASONING_EFFORT_CANDIDATES


def test_response_format_400_is_unsupported_shape(profile_path):
    """response_format 400 → unsupported（区别于 timeout）。"""
    results = [_ok_response()] * 7
    results += [_ok_response()] * 2
    results.append(_bad_request({"error": {"message": "response_format not supported"}}))
    results.append(_ok_response())
    with _patch_completion(*results):
        profile = probe(**PROBE_ARGS, user_config=USER_CONFIG)
    assert profile["probe_status"] == "partial"
    assert profile["response_format"] == {"status": "unsupported", "supported": []}


def test_tools_failure_is_partial_with_tools_section_shape(profile_path):
    """tools 400 → partial + tools 段 {"status":"unsupported","supported":[]} 形状。"""
    results = [_ok_response()] * 7
    results += [_ok_response()] * 2
    results.append(_ok_response())  # rf ok
    results.append(_bad_request({"error": {"message": "tools not supported"}}))
    with _patch_completion(*results):
        profile = probe(**PROBE_ARGS, user_config=USER_CONFIG)
    assert profile["probe_status"] == "partial"
    assert profile["tools"] == {"status": "unsupported", "supported": []}
    assert profile["response_format"] == {"status": "ok", "supported": ["json_object"]}
    saved = read_profile("https://api.example.com/v1/", "m1")
    assert saved["probe_status"] == "partial"
    assert saved["tools"] == {"status": "unsupported", "supported": []}


def test_tools_timeout_is_timeout_shape(profile_path):
    """tools 超时 → timeout 形状。"""
    results = [_ok_response()] * 7
    results += [_ok_response()] * 2
    results.append(_ok_response())
    results.append(_timeout_error())
    with _patch_completion(*results):
        profile = probe(**PROBE_ARGS, user_config=USER_CONFIG)
    assert profile["probe_status"] == "partial"
    assert profile["tools"] == {"status": "timeout", "supported": []}


# ---------------------------------------------------------------------------
# ③ thinking 剔除行为（R13/R14）
# ---------------------------------------------------------------------------


def test_thinking_probe_strips_config_thinking_key(profile_path):
    """config 含 litellm_kwargs.thinking 时，thinking 探测请求 thinking 仅来自 raw 候选。

    断言传入 assemble_request_params 的 config 副本不含 thinking 键——volcengine
    transformation 顶层与注入双源冲突的静默回归只能靠单测防。
    """
    captured = []
    real_assemble = model_probe.assemble_request_params

    def _spy(config, raw_reasoning_effort=None, raw_thinking=None):
        captured.append((config, raw_reasoning_effort, raw_thinking))
        return real_assemble(config, raw_reasoning_effort=raw_reasoning_effort, raw_thinking=raw_thinking)

    results = [_ok_response()] * 7
    results += [_ok_response()] * 2
    results += [_ok_response()] * 2
    with patch.object(model_probe, "assemble_request_params", side_effect=_spy), \
         _patch_completion(*results):
        probe(**PROBE_ARGS, user_config=USER_CONFIG)

    thinking_calls = [c for c in captured if c[2] is not None]
    assert len(thinking_calls) == 2, "应有 2 次 thinking 探测（enabled/disabled）"
    for config, _raw_effort, raw_thinking in thinking_calls:
        litellm_kwargs = config.get("litellm_kwargs") or {}
        assert "thinking" not in litellm_kwargs, (
            "thinking 探测传入 assemble_request_params 的 config 副本必须不含 thinking 键"
        )
        assert raw_thinking in ({"type": "enabled"}, {"type": "disabled"})
    # 值域扫描（raw_thinking=None）的 config 仍保留生产 thinking（与 chat() 顶层通道一致）
    reasoning_calls = [c for c in captured if c[2] is None and c[1] is not None]
    assert len(reasoning_calls) == 7
    assert all(("thinking" in (c[0].get("litellm_kwargs") or {})) for c in reasoning_calls)


def test_thinking_probe_sends_raw_candidate_via_extra_body(profile_path):
    """thinking 探测请求 extra_body.thinking 来自 raw 候选（enabled/disabled 各一）。"""
    with _patch_completion(*[_ok_response()] * 11) as mock_completion:
        probe(**PROBE_ARGS, user_config=USER_CONFIG)
    calls = mock_completion.call_args_list
    # 第 8、9 次（index 7/8）是 thinking 探测
    assert calls[7].kwargs["extra_body"]["thinking"] == {"type": "enabled"}
    assert calls[8].kwargs["extra_body"]["thinking"] == {"type": "disabled"}


# ---------------------------------------------------------------------------
# ④ thinking 状态聚合枚举（R9/R10/R15/R17）
# ---------------------------------------------------------------------------


def test_thinking_aggregation_both_true_ok(profile_path):
    """双 true → probe_status=ok（R17）。"""
    results = [_ok_response()] * 7
    results += [_ok_response()] * 2  # enabled 200 / disabled 200
    results += [_ok_response()] * 2
    with _patch_completion(*results):
        profile = probe(**PROBE_ARGS, user_config=USER_CONFIG)
    assert profile["probe_status"] == "ok"
    assert profile["thinking"] == {"enabled": True, "disabled": True, "returns_reasoning_content": False}


def test_thinking_aggregation_one_false_partial(profile_path):
    """一 false（enabled 400 / disabled 200）→ partial + thinking 段如实记录。"""
    results = [_ok_response()] * 7
    results.append(_bad_request({"error": {"message": "thinking param not allowed"}}))  # enabled 400
    results.append(_ok_response())  # disabled 200
    results += [_ok_response()] * 2
    with _patch_completion(*results):
        profile = probe(**PROBE_ARGS, user_config=USER_CONFIG)
    assert profile["probe_status"] == "partial"
    assert profile["thinking"] == {"enabled": False, "disabled": True, "returns_reasoning_content": False}


def test_thinking_aggregation_both_false_partial(profile_path):
    """双 false（enabled 400 + disabled 400）→ partial（值域结果照写）。"""
    results = [_ok_response()] * 7
    results.append(_bad_request({"error": {"message": "thinking param not allowed"}}))
    results.append(_bad_request({"error": {"message": "thinking disabled invalid"}}))
    results += [_ok_response()] * 2
    with _patch_completion(*results):
        profile = probe(**PROBE_ARGS, user_config=USER_CONFIG)
    assert profile["probe_status"] == "partial"
    assert profile["thinking"] == {"enabled": False, "disabled": False, "returns_reasoning_content": False}
    saved = read_profile("https://api.example.com/v1/", "m1")
    assert saved["probe_status"] == "partial"
    assert saved["thinking"]["enabled"] is False


def test_thinking_records_reasoning_content(profile_path):
    """thinking 响应含 reasoning_content → returns_reasoning_content=true。"""
    results = [_ok_response()] * 7
    results.append(_ok_response(reasoning_content="thinking..."))  # enabled 带思考链返回
    results.append(_ok_response())
    results += [_ok_response()] * 2
    with _patch_completion(*results):
        profile = probe(**PROBE_ARGS, user_config=USER_CONFIG)
    assert profile["thinking"]["returns_reasoning_content"] is True


# ---------------------------------------------------------------------------
# ⑤ ignores_unknown 置位（R11）
# ---------------------------------------------------------------------------


def test_ignores_unknown_true_when_all_7_values_200(profile_path):
    """7×200 且无一个 400 → ignores_unknown=true（服务端静默忽略未知参数）。"""
    with _patch_completion(*[_ok_response()] * 11):
        profile = probe(**PROBE_ARGS, user_config=USER_CONFIG)
    assert profile["ignores_unknown"] is True


def test_ignores_unknown_false_when_partial_400(profile_path):
    """6×200 + 1×400 → ignores_unknown=false。"""
    results = [_ok_response()] * 6
    results.append(_bad_request({"error": {"message": "reasoning_effort: bad value"}}))
    results += [_ok_response()] * 4
    with _patch_completion(*results):
        profile = probe(**PROBE_ARGS, user_config=USER_CONFIG)
    assert profile["ignores_unknown"] is False


# ---------------------------------------------------------------------------
# ⑥ 档案读写 / 键控覆盖 / 键规范化 / flock 锁竞争
# ---------------------------------------------------------------------------


def test_profile_dual_keys_and_normalization(tmp_path):
    """llm/lightrag 双键并存；api_base rstrip('/') 规范化写读一致。"""
    path = tmp_path / "cap.json"
    with patch.object(model_probe, "PROFILE_PATH", path):
        write_profile({"api_base": "https://api.example.com/v1", "model": "m1", "probe_status": "ok"}, lightrag=False)
        write_profile({"api_base": "https://api.example.com/v1/", "model": "m1", "probe_status": "partial"}, lightrag=True)
        data = load_profile()
        assert set(data.keys()) == {
            "https://api.example.com/v1|m1|llm",
            "https://api.example.com/v1|m1|lightrag",
        }
        # 读端尾部斜杠差异不导致档案不命中
        assert read_profile("https://api.example.com/v1/", "m1", lightrag=False)["probe_status"] == "ok"
        assert read_profile("https://api.example.com/v1", "m1", lightrag=True)["probe_status"] == "partial"
        assert read_profile("https://other.example.com", "m1") is None


def test_profile_keyed_override(tmp_path):
    """同键覆盖（键控覆盖）；不同模型独立键（换模型自动失效）。"""
    path = tmp_path / "cap.json"
    with patch.object(model_probe, "PROFILE_PATH", path):
        write_profile({"api_base": "https://x.com", "model": "m1", "probe_status": "ok"})
        write_profile({"api_base": "https://x.com", "model": "m1", "probe_status": "partial"})
        data = load_profile()
        assert len(data) == 1
        assert data["https://x.com|m1|llm"]["probe_status"] == "partial"
        write_profile({"api_base": "https://x.com", "model": "m2", "probe_status": "ok"})
        assert len(load_profile()) == 2
        assert read_profile("https://x.com", "m2")["probe_status"] == "ok"


def test_profile_atomic_write_valid_json(tmp_path):
    """原子写：写后文件为合法 JSON（os.replace 保证不半写）。"""
    path = tmp_path / "cap.json"
    with patch.object(model_probe, "PROFILE_PATH", path):
        write_profile({"api_base": "https://x.com", "model": "m1", "probe_status": "ok"})
        raw = path.read_text(encoding="utf-8")
        assert json.loads(raw)["https://x.com|m1|llm"]["probe_status"] == "ok"


def test_write_profile_lock_contention_skips_write(tmp_path):
    """flock 非阻塞：另一 fd 持锁时跳过写入返回 False（不写坏旧档）。"""
    path = tmp_path / "cap.json"
    lock_path = tmp_path / "cap.json.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with patch.object(model_probe, "PROFILE_PATH", path):
            assert write_profile({"api_base": "https://x.com", "model": "m1", "probe_status": "ok"}) is False
        assert not path.exists()  # 未写入
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# ---------------------------------------------------------------------------
# ⑦ mock 按 litellm 异常构造 + 探测请求传输形态
# ---------------------------------------------------------------------------


def test_error_classification_uses_e_body_not_response_text():
    """分类读 e.body：BadRequestError(body={...}) 即使无 response（e.response.text 空）
    也按 body token 归类 unsupported；token 不匹配的 400 与 401 归 failed。"""
    from niu_api.model_probe import _classify_value_domain_error

    e = _bad_request({"error": {"message": "reasoning_effort not supported"}})
    assert getattr(e, "status_code", None) == 400
    assert _classify_value_domain_error(e, "reasoning_effort") == "unsupported"
    assert _classify_value_domain_error(e, "thinking") == "failed"  # token 不匹配

    e2 = _bad_request({"error": {"message": "max_tokens too small"}})
    assert _classify_value_domain_error(e2, "reasoning_effort") == "failed"

    e3 = _auth_error()
    assert getattr(e3, "status_code", None) == 401
    assert _classify_value_domain_error(e3, "reasoning_effort") == "failed"


def test_probe_request_transport_shape(profile_path):
    """探测请求直发 litellm.completion：前缀推导 model + build_base_params
    (stream=False, max_tokens=8, timeout=10) + 固定消息 + extra_body raw 注入。"""
    with _patch_completion(*[_ok_response()] * 11) as mock_completion:
        probe(**PROBE_ARGS, user_config=USER_CONFIG)
    calls = mock_completion.call_args_list
    assert len(calls) == 11

    first = calls[0].kwargs
    assert first["model"] == "openai/m1"  # 前缀推导（openai 兼容路由）
    assert first["stream"] is False
    assert first["max_tokens"] == 8
    assert first["timeout"] == 10
    assert first["messages"] == [{"role": "user", "content": "OK"}]
    assert first["api_base"] == "https://api.example.com/v1/"
    assert first["api_key"] == "k-llm"
    # raw 候选无条件注入 extra_body（绕过 none 排除与 llmcore 归一化）
    assert first["extra_body"]["reasoning_effort"] == "minimal"
    assert first["drop_params"] is True

    # 第 10 次（index 9）= response_format 探测：顶层 response_format + 逃生口
    rf_call = calls[9].kwargs
    assert rf_call["response_format"] == {"type": "json_object"}
    assert rf_call["allowed_openai_params"] == ["response_format"]  # 顶层送达 litellm
    assert rf_call["drop_params"] is True

    # 第 11 次（index 10）= tools 探测：顶层 tools
    tools_call = calls[10].kwargs
    assert tools_call["tools"][0]["function"]["name"] == "probe_tool"


def test_probe_volces_domain_derives_volcengine_prefix(profile_path):
    """volces.com 域名 → volcengine/ 前缀（否则豆包 response_format 探测挂起）。"""
    with _patch_completion(*[_ok_response()] * 11) as mock_completion:
        probe(
            api_base="https://ark.cn-beijing.volces.com/api/plan/v3",
            api_key="k",
            model="doubao-seed-2.1-turbo",
            user_config=None,
        )
    assert mock_completion.call_args_list[0].kwargs["model"] == "volcengine/doubao-seed-2.1-turbo"


def test_local_model_omits_api_key(profile_path):
    """本地模型（localhost）免 apiKey：api_key="" → 请求不含 api_key 键。"""
    with _patch_completion(*[_ok_response()] * 11) as mock_completion:
        probe(api_base="http://localhost:11434/v1", api_key="", model="llama3", user_config=None)
    first = mock_completion.call_args_list[0].kwargs
    assert "api_key" not in first
    assert first["model"] == "openai/llama3"


def test_lightrag_scenario_probes_with_lightrag_section(profile_path):
    """lightrag 场景：config 取 lightrag_llm 段（thinking disabled 随生产注入），
    档案键 api_base|model|lightrag。"""
    results = [_ok_response()] * 7
    results += [_ok_response()] * 2
    results += [_ok_response()] * 2
    with _patch_completion(*results) as mock_completion:
        profile = probe(**PROBE_ARGS, user_config=USER_CONFIG, lightrag=True)
    # 生产 thinking（lightrag_llm 段 disabled）随值域探测注入（与生产同参数）
    first = mock_completion.call_args_list[0].kwargs
    assert first["extra_body"]["thinking"] == {"type": "disabled"}
    assert profile["probe_status"] == "ok"
    assert build_profile_key(profile["api_base"], profile["model"], lightrag=True) == \
        "https://api.example.com/v1|m1|lightrag"
    saved = read_profile("https://api.example.com/v1/", "m1", lightrag=True)
    assert saved is not None
    assert saved["probe_status"] == "ok"
