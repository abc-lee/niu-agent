"""模型能力探测器核心单测（组件 1，Task 2）。

覆盖：
① 值域判定（200→supported / 400→unsupported 继续——R19：400 本身表明该值不被
   接受，body 含 token 是充分条件非必要条件，body 缺失/None 不改变 400 语义
   （volcengine 路由实测 400 响应 body=None）/ 401→failed 终止不覆盖旧档 /
   P1-1：值域扫描携带场景 thinking——disabled 下
   high+disabled 400→high unsupported、enabled vs disabled supported 差异断言；
   **值域超时重试（R18）**：首次超时→重试成功→supported；连续两次超时→
   unsupported 继续且 probe_status 非 failed；重试遇 401→failed 终止；
   请求总数恒 11 = 7 值域 + 2 thinking + 1 response_format + 1 tools，
   含重试最坏 14 次）
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
import threading
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
    """按 litellm 异常构造：BadRequestError(body={...})——e.response.text 空；
    R19 后分类只看 status_code（body 可 None——volcengine 实测 400 响应 body=None）。"""
    from litellm import BadRequestError

    return BadRequestError(message, model="probe-model", llm_provider="openai", body=body)


def _auth_error():
    from litellm import AuthenticationError

    return AuthenticationError("401 invalid api key", llm_provider="openai", model="probe-model")


def _timeout_error():
    from litellm import Timeout

    return Timeout("request timed out", model="probe-model", llm_provider="openai")


def _patch_completion(*results):
    """patch model_probe.litellm.completion，按顺序返回/抛出 results（耗尽即断言失败）。

    线程安全（值域扫描并行后 completion 被多线程并发调用——unittest.mock 的
    side_effect 列表索引遍历非线程安全，竞态会丢响应/重复取）。
    """
    from unittest.mock import patch
    _lock = threading.Lock()
    _queue = list(results)

    def _side_effect(*a, **kw):
        with _lock:
            if not _queue:
                raise StopIteration
            r = _queue.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    return patch("niu_api.model_probe.litellm.completion", side_effect=_side_effect)


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
    with _patch_completion(*[_ok_response()] * 12) as mock_completion:
        profile = probe(**PROBE_ARGS, user_config=USER_CONFIG)
    assert profile["probe_status"] == "ok"
    assert profile["reasoning_effort"]["supported"] == REASONING_EFFORT_CANDIDATES
    assert profile["reasoning_effort"]["unsupported"] == []
    assert mock_completion.call_count == 12  # 7 值域 + 无效值探针 + 2 thinking + rf + tools


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


def test_value_domain_disabled_thinking_rejects_high_as_unsupported(profile_path):
    """场景 thinking=disabled（lightrag 生产恒 disabled）值域扫描：high 候选 400
    （body 含 reasoning_effort/combination——豆包实测 "Invalid combination of
    reasoning_effort and thinking type: high + disabled"）→ high 记 unsupported，
    值域结论与生产场景一致（P1-1）。"""
    results = [_ok_response()] * 3  # minimal/low/medium（disabled 下 200）
    results.append(_bad_request({
        "error": {"message": "Invalid combination of reasoning_effort and thinking type: high + disabled"}
    }))  # high 400（与 t5_lightrag Variant A 实测同型）
    results += [_ok_response()] * 3  # xhigh/none/max
    results += [_ok_response()] * 2  # thinking
    results += [_ok_response()] * 2  # rf + tools
    with _patch_completion(*results) as mock_completion:
        profile = probe(**PROBE_ARGS, user_config=USER_CONFIG, lightrag=True)
    assert profile["probe_status"] == "ok"
    assert profile["reasoning_effort"]["supported"] == ["minimal", "low", "medium", "xhigh", "none", "max"]
    assert profile["reasoning_effort"]["unsupported"] == ["high"]
    assert profile["ignores_unknown"] is False  # 任一值被拒即 false（R11 同步）
    # wire 验证：值域扫描 7 次请求均携带场景 thinking=disabled，且无顶层 thinking 键（单一来源）
    for call in mock_completion.call_args_list[:7]:
        assert call.kwargs.get("extra_body", {}).get("thinking") == {"type": "disabled"}
        assert "thinking" not in call.kwargs


def test_value_domain_supported_differs_between_thinking_enabled_and_disabled(profile_path):
    """场景 thinking=enabled vs disabled 值域结论差异断言（P1-1）：同服务端下
    enabled（llm 场景）→ 7 值全 supported + 无效值探针 200 → ignores_unknown=true；
    disabled（lightrag 场景）→ high 被拒 unsupported + ignores_unknown=false。"""
    # llm 场景（thinking=enabled）：7 值全 200 + 探针 200（mock 全 _ok_response）
    with _patch_completion(*[_ok_response()] * 12) as mock_completion:
        profile_enabled = probe(**PROBE_ARGS, user_config=USER_CONFIG)
    assert profile_enabled["reasoning_effort"]["supported"] == REASONING_EFFORT_CANDIDATES
    assert profile_enabled["reasoning_effort"]["unsupported"] == []
    assert profile_enabled["ignores_unknown"] is True
    for call in mock_completion.call_args_list[:7]:
        assert call.kwargs.get("extra_body", {}).get("thinking") == {"type": "enabled"}

    # lightrag 场景（thinking=disabled）：high 400 → unsupported；其余 6 值 200
    results = [_ok_response()] * 3
    results.append(_bad_request({
        "error": {"message": "Invalid combination of reasoning_effort and thinking type: high + disabled"}
    }))
    results += [_ok_response()] * 3
    results += [_ok_response()] * 2
    results += [_ok_response()] * 2
    with _patch_completion(*results) as mock_completion:
        profile_disabled = probe(**PROBE_ARGS, user_config=USER_CONFIG, lightrag=True)
    assert profile_disabled["reasoning_effort"]["supported"] == ["minimal", "low", "medium", "xhigh", "none", "max"]
    assert profile_disabled["reasoning_effort"]["unsupported"] == ["high"]
    assert profile_disabled["ignores_unknown"] is False
    for call in mock_completion.call_args_list[:7]:
        assert call.kwargs.get("extra_body", {}).get("thinking") == {"type": "disabled"}

    # supported 差异断言：enabled 的 supported 真包含 disabled 的（high 仅 enabled 下可用）
    assert set(profile_enabled["reasoning_effort"]["supported"]) - \
        set(profile_disabled["reasoning_effort"]["supported"]) == {"high"}


def test_value_domain_400_without_token_marks_unsupported_and_continues(profile_path):
    """400 但 body 不含 reasoning_effort（max_tokens 过小/模型名错/限流）→ 仍
    unsupported 继续探测（R19：400 本身表明该值不被接受，body 含 token 是充分
    条件非必要条件，body 缺失不改变 400 语义）。"""
    results = [_ok_response()]  # minimal
    results.append(_bad_request({"error": {"message": "max_tokens must be at least 16"}}))  # low 400 无 token
    results += [_ok_response()] * 5  # medium..max
    results += [_ok_response()] * 2  # thinking
    results += [_ok_response()] * 2  # rf + tools
    with _patch_completion(*results) as mock_completion:
        profile = probe(**PROBE_ARGS, user_config=USER_CONFIG)
    assert profile["probe_status"] == "ok"  # 探测完成（结果是不支持部分值），不 failed 终止
    assert profile["reasoning_effort"]["supported"] == ["minimal", "medium", "high", "xhigh", "none", "max"]
    assert profile["reasoning_effort"]["unsupported"] == ["low"]
    assert profile["ignores_unknown"] is False  # low 未确认 200 → 不置位
    assert mock_completion.call_count == 11  # 全部 7 值域 + 2 thinking + rf + tools 测完


def test_value_domain_400_with_none_body_marks_unsupported_and_continues(profile_path):
    """400 + body=None（volcengine 路由实测——litellm 未解析 body，token 无法匹配）→
    仍 unsupported 继续探测（R19 核心场景：不再因 body 缺失误分类 failed 导致探测
    中断、只记录 minimal）。"""
    results = [_ok_response()] * 2  # minimal/low
    results.append(_bad_request(None))  # medium 400 body=None
    results += [_ok_response()] * 4  # high..max
    results += [_ok_response()] * 2  # thinking
    results += [_ok_response()] * 2  # rf + tools
    with _patch_completion(*results) as mock_completion:
        profile = probe(**PROBE_ARGS, user_config=USER_CONFIG)
    assert profile["probe_status"] == "ok"
    assert profile["reasoning_effort"]["supported"] == ["minimal", "low", "high", "xhigh", "none", "max"]
    assert profile["reasoning_effort"]["unsupported"] == ["medium"]
    assert profile["ignores_unknown"] is False
    assert mock_completion.call_count == 11


def test_value_domain_401_fails_and_does_not_overwrite_old_profile(profile_path):
    """401 → failed 终止，不覆盖旧档案。"""
    old = {"api_base": "https://api.example.com/v1", "model": "m1",
           "probe_status": "ok", "probed_at": "2026-01-01T00:00:00"}
    write_profile(dict(old))
    results = [_ok_response(), _auth_error()]
    with _patch_completion(*results) as mock_completion:
        profile = probe(**PROBE_ARGS, user_config=USER_CONFIG)
    assert profile["probe_status"] == "failed"
    # 并行值域扫描：7 个候选已全部提交（其中 2 个用 mock 响应，其余 StopIteration 也走 failed）
    assert mock_completion.call_count == 7
    # 旧档保留（含 api_base 尾部斜杠差异也能命中——键规范化）
    saved = read_profile("https://api.example.com/v1/", "m1")
    assert saved is not None
    assert saved["probe_status"] == "ok"
    assert saved["probed_at"] == "2026-01-01T00:00:00"


def test_value_domain_timeout_retried_then_supported(profile_path):
    """值域候选首次超时 → 重试该候选一次 → 重试 200 → supported（超时 ≠ 值不支持，
    R18——豆包响应在 10s 边界波动，Task 5 实测 minimal 成功/low 超时即 failed 终止）。"""
    results = [_ok_response()]  # minimal
    results.append(_timeout_error())  # low 首次超时
    results.append(_ok_response())    # low 重试成功
    results += [_ok_response()] * 5   # medium..max
    results.append(_ok_response())    # 无效值探针（7 值全 200 判别）
    results += [_ok_response()] * 2   # thinking
    results += [_ok_response()] * 2   # rf + tools
    with _patch_completion(*results) as mock_completion:
        profile = probe(**PROBE_ARGS, user_config=USER_CONFIG)
    assert profile["probe_status"] == "ok"
    assert profile["reasoning_effort"]["supported"] == REASONING_EFFORT_CANDIDATES
    assert profile["reasoning_effort"]["unsupported"] == []
    assert profile["ignores_unknown"] is True  # 探针 200 → 忽略未知参数
    assert mock_completion.call_count == 13  # 7 值域（含 1 次重试）+ 探针 + 2 thinking + rf + tools


def test_value_domain_double_timeout_marks_unsupported_and_continues(profile_path):
    """连续两次超时（重试仍超时）→ 记 unsupported（保守——无法确认支持）并继续探测，
    不 failed 终止；超时 unsupported 不参与 ignores_unknown 置位。"""
    results = [_ok_response()] * 2  # minimal/low
    results.append(_timeout_error())  # medium 首次超时
    results.append(_timeout_error())  # medium 重试仍超时
    results += [_ok_response()] * 4   # high..max
    results += [_ok_response()] * 2   # thinking
    results += [_ok_response()] * 2   # rf + tools
    with _patch_completion(*results) as mock_completion:
        profile = probe(**PROBE_ARGS, user_config=USER_CONFIG)
    assert profile["probe_status"] == "ok"  # 非 failed——值域探测完成（结果是不支持）
    assert profile["reasoning_effort"]["supported"] == ["minimal", "low", "high", "xhigh", "none", "max"]
    assert profile["reasoning_effort"]["unsupported"] == ["medium"]
    assert profile["ignores_unknown"] is False  # medium 未确认 200（超时 unsupported）→ 不置位
    assert mock_completion.call_count == 12  # 7 值域（含 1 次重试）+ 2 thinking + rf + tools


def test_value_domain_timeout_retry_then_401_fails(profile_path):
    """超时重试仅 1 次；重试遇非值域错误（401）→ failed 终止（服务端拒绝 ≠ 慢，
    不继续重试——R18 只对 Timeout 类重试）。并行语义：7 候选全部提交，任一线程
    遇非值域错误（401/StopIteration）→ failed；supported 完成顺序不定不再断言具体值。"""
    results = [_ok_response()]  # minimal
    results.append(_timeout_error())  # low 首次超时
    results.append(_auth_error())     # low 重试遇 401
    with _patch_completion(*results) as mock_completion:
        profile = probe(**PROBE_ARGS, user_config=USER_CONFIG)
    assert profile["probe_status"] == "failed"
    assert mock_completion.call_count >= 3  # 7 候选已提交 + 至少 1 次超时重试


# ---------------------------------------------------------------------------
# ② partial 例外（response_format / tools 子项失败仅降级 partial，值域照写）
# ---------------------------------------------------------------------------


def test_response_format_timeout_is_partial_with_value_domain_written(profile_path):
    """response_format 超时 → partial + 值域照写 + response_format 段 timeout 形状。"""
    results = [_ok_response()] * 7
    results.append(_ok_response())  # 无效值探针（7 值全 200 判别）
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
    results.append(_ok_response())  # 无效值探针
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
    results.append(_ok_response())  # 无效值探针
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
    results.append(_ok_response())  # 无效值探针
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
    """config 含 litellm_kwargs.thinking 时，探测请求 thinking 单一来源（R13 + P1-1）。

    thinking 探测：config 副本剔除 thinking 键，raw 候选经 raw_thinking 注入——
    volcengine transformation 顶层与注入双源冲突的静默回归只能靠单测防。
    P1-1 扩展：值域扫描同样单一来源——config 副本剔除 thinking 键（无顶层通道），
    场景 thinking 经 raw_thinking 显式传入（值域结论与场景 thinking 耦合）。
    """
    captured = []
    real_assemble = model_probe.assemble_request_params

    def _spy(config, raw_reasoning_effort=None, raw_thinking=None):
        captured.append((config, raw_reasoning_effort, raw_thinking))
        return real_assemble(config, raw_reasoning_effort=raw_reasoning_effort, raw_thinking=raw_thinking)

    results = [_ok_response()] * 7
    results.append(_ok_response())  # 无效值探针
    results += [_ok_response()] * 2
    results += [_ok_response()] * 2
    with patch.object(model_probe, "assemble_request_params", side_effect=_spy), \
         _patch_completion(*results):
        probe(**PROBE_ARGS, user_config=USER_CONFIG)

    thinking_calls = [c for c in captured if c[2] is not None and c[1] is None]
    assert len(thinking_calls) == 2, "应有 2 次 thinking 探测（enabled/disabled）"
    for config, _raw_effort, raw_thinking in thinking_calls:
        litellm_kwargs = config.get("litellm_kwargs") or {}
        assert "thinking" not in litellm_kwargs, (
            "thinking 探测传入 assemble_request_params 的 config 副本必须不含 thinking 键"
        )
        assert raw_thinking in ({"type": "enabled"}, {"type": "disabled"})
    # 值域扫描（raw_reasoning_effort 非 None）：config 副本剔除 thinking 键，
    # 场景 thinking 经 raw_thinking 显式传入（P1-1——不得默认 enabled）
    # 7 值 + 无效值探针（探针同走 raw_reasoning_effort 注入）
    reasoning_calls = [c for c in captured if c[1] is not None]
    assert len(reasoning_calls) == 8
    for config, _raw_effort, raw_thinking in reasoning_calls:
        litellm_kwargs = config.get("litellm_kwargs") or {}
        assert "thinking" not in litellm_kwargs, (
            "值域扫描 config 副本必须剔除 thinking 键——thinking 单一来源走 raw_thinking"
        )
        assert raw_thinking == {"type": "enabled"}  # llm 场景配置 thinking=enabled 随值域扫描注入


def test_thinking_probe_sends_raw_candidate_via_extra_body(profile_path):
    """thinking 探测请求 extra_body.thinking 来自 raw 候选（enabled/disabled 各一）。"""
    with _patch_completion(*[_ok_response()] * 12) as mock_completion:
        probe(**PROBE_ARGS, user_config=USER_CONFIG)
    calls = mock_completion.call_args_list
    # 第 9、10 次（index 8/9）是 thinking 探测（index 7 = 无效值探针）
    assert calls[8].kwargs["extra_body"]["thinking"] == {"type": "enabled"}
    assert calls[9].kwargs["extra_body"]["thinking"] == {"type": "disabled"}


# ---------------------------------------------------------------------------
# ④ thinking 状态聚合枚举（R9/R10/R15/R17）
# ---------------------------------------------------------------------------


def test_thinking_aggregation_both_true_ok(profile_path):
    """双 true → probe_status=ok（R17）。"""
    results = [_ok_response()] * 7
    results.append(_ok_response())  # 无效值探针
    results += [_ok_response()] * 2  # enabled 200 / disabled 200
    results += [_ok_response()] * 2
    with _patch_completion(*results):
        profile = probe(**PROBE_ARGS, user_config=USER_CONFIG)
    assert profile["probe_status"] == "ok"
    assert profile["thinking"] == {"enabled": True, "disabled": True, "returns_reasoning_content": False}


def test_thinking_aggregation_one_false_partial(profile_path):
    """一 false（enabled 400 / disabled 200）→ partial + thinking 段如实记录。"""
    results = [_ok_response()] * 7
    results.append(_ok_response())  # 无效值探针
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
    results.append(_ok_response())  # 无效值探针
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
    results.append(_ok_response())  # 无效值探针
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
    with _patch_completion(*[_ok_response()] * 12):
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


def test_error_classification_r19_400_always_unsupported():
    """R19 分类：status==400 → "unsupported"（body 含 token 与否、body 缺失/None
    均不改判——volcengine 路由实测 400 响应 body=None，litellm 未解析 body）；
    Timeout 类 → "timeout"；其他状态码（401 等）→ "failed"。"""
    from niu_api.model_probe import _classify_value_domain_error

    e = _bad_request({"error": {"message": "reasoning_effort not supported"}})
    assert getattr(e, "status_code", None) == 400
    assert _classify_value_domain_error(e, "reasoning_effort") == "unsupported"
    assert _classify_value_domain_error(e, "thinking") == "unsupported"  # 400 即 unsupported，与 token 无关

    e2 = _bad_request({"error": {"message": "max_tokens too small"}})
    assert _classify_value_domain_error(e2, "reasoning_effort") == "unsupported"  # 无 token 亦 unsupported

    e3 = _bad_request(None)  # body 缺失（volcengine 实测形态）
    assert getattr(e3, "status_code", None) == 400
    assert _classify_value_domain_error(e3, "reasoning_effort") == "unsupported"

    e4 = _auth_error()
    assert getattr(e4, "status_code", None) == 401
    assert _classify_value_domain_error(e4, "reasoning_effort") == "failed"

    e5 = _timeout_error()
    assert _classify_value_domain_error(e5, "reasoning_effort") == "timeout"


def test_probe_request_transport_shape(profile_path):
    """探测请求直发 litellm.completion：前缀推导 model + build_base_params
    (stream=False, max_tokens=256, timeout=60) + 固定消息 + extra_body raw 注入。"""
    with _patch_completion(*[_ok_response()] * 12) as mock_completion:
        probe(**PROBE_ARGS, user_config=USER_CONFIG)
    calls = mock_completion.call_args_list
    assert len(calls) == 12

    first = calls[0].kwargs
    assert first["model"] == "openai/m1"  # 前缀推导（openai 兼容路由）
    assert first["stream"] is False
    assert first["max_tokens"] == 256
    assert first["timeout"] == 60
    assert first["messages"] == [{"role": "user", "content": "OK"}]
    assert first["api_base"] == "https://api.example.com/v1/"
    assert first["api_key"] == "k-llm"
    # raw 候选无条件注入 extra_body（绕过 none 排除与 llmcore 归一化）
    assert first["extra_body"]["reasoning_effort"] == "minimal"
    assert first["drop_params"] is True

    # 第 11 次（index 10）= response_format 探测：顶层 response_format + 逃生口
    rf_call = calls[10].kwargs
    assert rf_call["response_format"] == {"type": "json_object"}
    assert rf_call["allowed_openai_params"] == ["response_format"]  # 顶层送达 litellm
    assert rf_call["drop_params"] is True

    # 第 12 次（index 11）= tools 探测：顶层 tools
    tools_call = calls[11].kwargs
    assert tools_call["tools"][0]["function"]["name"] == "probe_tool"


def test_probe_volces_domain_derives_volcengine_prefix(profile_path):
    """volces.com 域名 → volcengine/ 前缀（否则豆包 response_format 探测挂起）。"""
    with _patch_completion(*[_ok_response()] * 12) as mock_completion:
        probe(
            api_base="https://ark.cn-beijing.volces.com/api/plan/v3",
            api_key="k",
            model="doubao-seed-2.1-turbo",
            user_config=None,
        )
    assert mock_completion.call_args_list[0].kwargs["model"] == "volcengine/doubao-seed-2.1-turbo"


def test_local_model_omits_api_key(profile_path):
    """本地模型（localhost）免 apiKey：api_key="" → 请求不含 api_key 键。"""
    with _patch_completion(*[_ok_response()] * 12) as mock_completion:
        probe(api_base="http://localhost:11434/v1", api_key="", model="llama3", user_config=None)
    first = mock_completion.call_args_list[0].kwargs
    assert "api_key" not in first
    assert first["model"] == "openai/llama3"


def test_lightrag_scenario_probes_with_lightrag_section(profile_path):
    """lightrag 场景：config 取 lightrag_llm 段（thinking disabled 随生产注入），
    档案键 api_base|model|lightrag。"""
    results = [_ok_response()] * 7
    results.append(_ok_response())  # 无效值探针
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
