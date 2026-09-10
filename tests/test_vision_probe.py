"""vision 双色交叉子扫描单测（plan v0.5.2 §4-V1 / §6-T1）。

覆盖：
① 红蓝全对 → vision.supported=true + input:["text","image"] + probed_at ISO
② 单色错（红答绿）→ supported=false，蓝图不再发（短路）
③ 请求异常（5xx）→ supported=false 不毒化主探测项（probe_status=ok、
   reasoning_effort/thinking 结果照常落盘）
④ 超时 → 重试 1 次仍超时 → supported=false
⑤ max_tokens 传参断言：vision 请求 =500（R7），非 vision 请求 =256；timeout=45
⑥ 档案 |llm 键 vision 子对象结构；陈旧 true 被 false 覆盖落盘
⑦ lightrag 场景不跑 vision 扫描（|lightrag 键无 vision、无多模态请求）

禁真实 LLM：所有请求 patch litellm.completion。测试图 PIL 现场生成（32×32 纯色）。
"""

import base64
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from niu_api import model_probe  # noqa: E402
from niu_api.model_probe import (  # noqa: E402
    PROBE_MAX_TOKENS,
    VISION_MAX_TOKENS,
    VISION_TIMEOUT,
    _solid_png_data_uri,
    load_profile,
    probe,
    read_profile,
)


# ---------------------------------------------------------------------------
# mock 工具（镜像 tests/test_model_probe.py）
# ---------------------------------------------------------------------------


def _ok_response(content="OK", **message_attrs):
    """构造 200 响应 mock（choices[0].message.content）。"""
    msg = SimpleNamespace(content=content, **message_attrs)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def _timeout_error():
    from litellm import Timeout

    return Timeout("request timed out", model="probe-model", llm_provider="openai")


def _server_error():
    from litellm import APIError

    # litellm APIError(status_code, message, llm_provider, model)——status_code 首位位置参数
    return APIError(500, "internal server error", "openai", "probe-model")


def _patch_completion(*results):
    """patch model_probe.litellm.completion，按顺序返回/抛出 results（线程安全）。"""
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

LIGHTRAG_PROBE_ARGS = dict(PROBE_ARGS, lightrag=True)

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


# 主探测项请求数：7 值域全 200 → +1 无效值探针（ignores_unknown 判别）+ 2 thinking = 10
BASE_CALLS = 10
RED_URI = _solid_png_data_uri((255, 0, 0))
BLUE_URI = _solid_png_data_uri((0, 0, 255))


def _vision_call_params(mock_completion):
    """从 completion 调用序列里挑出多模态（含 image_url）请求参数。"""
    out = []
    for call in mock_completion.call_args_list:
        params = call.kwargs
        content = params["messages"][0]["content"]
        if isinstance(content, list):
            out.append(params)
    return out


def _image_url_of(params):
    for part in params["messages"][0]["content"]:
        if part.get("type") == "image_url":
            return part["image_url"]["url"]
    raise AssertionError("vision 请求缺 image_url part")


# ---------------------------------------------------------------------------
# ① 红蓝全对 → supported=true
# ---------------------------------------------------------------------------


def test_vision_both_colors_correct_marks_supported(profile_path):
    """红答红系词 + 蓝答蓝系词 → vision.supported=true，档案 |llm 键落盘。"""
    results = [_ok_response()] * BASE_CALLS
    results += [_ok_response("红色"), _ok_response("blue")]
    with _patch_completion(*results) as mock_completion:
        profile = probe(**PROBE_ARGS, user_config=USER_CONFIG)
    assert profile["probe_status"] == "ok"
    vision = profile["vision"]
    assert vision["supported"] is True
    assert vision["input"] == ["text", "image"]
    assert isinstance(vision["probed_at"], str) and "T" in vision["probed_at"]

    # 双色交叉请求形态：恰好 2 个多模态请求，红图在前蓝图在后，data URI 与 PIL 生成一致
    vision_calls = _vision_call_params(mock_completion)
    assert len(vision_calls) == 2
    assert _image_url_of(vision_calls[0]) == RED_URI
    assert _image_url_of(vision_calls[1]) == BLUE_URI
    for params in vision_calls:
        text_part = next(p for p in params["messages"][0]["content"] if p.get("type") == "text")
        assert "颜色" in text_part["text"]

    # 落盘档案 |llm 键含 vision 子对象
    saved = read_profile(PROBE_ARGS["api_base"], PROBE_ARGS["model"], profile_path=profile_path)
    assert saved["vision"]["supported"] is True
    assert mock_completion.call_count == BASE_CALLS + 2


def test_vision_english_and_cn_variants_all_match(profile_path):
    """色系命中词覆盖中英文变体：红答 crimson / 蓝答 青蓝 → supported=true。"""
    results = [_ok_response()] * BASE_CALLS
    results += [_ok_response("Crimson"), _ok_response("青蓝色")]
    with _patch_completion(*results):
        profile = probe(**PROBE_ARGS, user_config=USER_CONFIG)
    assert profile["vision"]["supported"] is True


# ---------------------------------------------------------------------------
# ② 单色错 → supported=false（短路——蓝图不再发）
# ---------------------------------------------------------------------------


def test_vision_red_wrong_marks_unsupported_and_short_circuits(profile_path):
    """红图答绿系词 → supported=false，蓝图请求不发（短路省一次调用）。"""
    results = [_ok_response()] * BASE_CALLS
    results += [_ok_response("绿色")]  # 红图错答；蓝图不应再被请求
    with _patch_completion(*results) as mock_completion:
        profile = probe(**PROBE_ARGS, user_config=USER_CONFIG)
    assert profile["vision"]["supported"] is False
    vision_calls = _vision_call_params(mock_completion)
    assert len(vision_calls) == 1  # 只发了红图
    assert mock_completion.call_count == BASE_CALLS + 1


def test_vision_blue_wrong_marks_unsupported(profile_path):
    """红对蓝错 → supported=false（两答全对才 true）。"""
    results = [_ok_response()] * BASE_CALLS
    results += [_ok_response("红色"), _ok_response("green")]
    with _patch_completion(*results) as mock_completion:
        profile = probe(**PROBE_ARGS, user_config=USER_CONFIG)
    assert profile["vision"]["supported"] is False
    assert len(_vision_call_params(mock_completion)) == 2


# ---------------------------------------------------------------------------
# ③ 请求异常 → false 不毒化主探测项
# ---------------------------------------------------------------------------


def test_vision_request_exception_false_without_poisoning(profile_path):
    """vision 请求 5xx → supported=false；probe_status=ok、reasoning_effort/thinking
    结果照常落盘（子扫描失败不得毒化主探测项）。"""
    results = [_ok_response()] * BASE_CALLS
    results += [_server_error()]  # 红图请求 500
    with _patch_completion(*results) as mock_completion:
        profile = probe(**PROBE_ARGS, user_config=USER_CONFIG)
    assert profile["probe_status"] == "ok"
    assert profile["vision"]["supported"] is False
    # 主探测项结果完整不受影响
    assert len(profile["reasoning_effort"]["supported"]) == 7
    assert profile["thinking"]["enabled"] is True

    saved = read_profile(PROBE_ARGS["api_base"], PROBE_ARGS["model"], profile_path=profile_path)
    assert saved["probe_status"] == "ok"
    assert saved["vision"]["supported"] is False


# ---------------------------------------------------------------------------
# ④ 超时 → 重试 1 次仍超时 → false
# ---------------------------------------------------------------------------


def test_vision_timeout_retried_once_then_unsupported(profile_path):
    """红图首次超时 → 重试 1 次仍超时 → supported=false（不抛、不毒化）。"""
    results = [_ok_response()] * BASE_CALLS
    results += [_timeout_error(), _timeout_error()]  # 首次 + 重试均超时
    with _patch_completion(*results) as mock_completion:
        profile = probe(**PROBE_ARGS, user_config=USER_CONFIG)
    assert profile["probe_status"] == "ok"
    assert profile["vision"]["supported"] is False
    vision_calls = _vision_call_params(mock_completion)
    assert len(vision_calls) == 2  # 同一红图两次 attempt（蓝图未发）
    assert mock_completion.call_count == BASE_CALLS + 2


def test_vision_timeout_then_ok_still_supported(profile_path):
    """红图首次超时 → 重试成功答对 + 蓝图答对 → supported=true（超时 ≠ 不支持）。"""
    results = [_ok_response()] * BASE_CALLS
    results += [_timeout_error(), _ok_response("红色"), _ok_response("蓝色")]
    with _patch_completion(*results):
        profile = probe(**PROBE_ARGS, user_config=USER_CONFIG)
    assert profile["vision"]["supported"] is True


# ---------------------------------------------------------------------------
# ⑤ max_tokens / timeout 传参断言（R7）
# ---------------------------------------------------------------------------


def test_vision_request_max_tokens_and_timeout(profile_path):
    """vision 请求 max_tokens=500（R7 reasoning 占预算陷阱）+ timeout=45；
    非 vision 探测请求保持 max_tokens=256、无显式 timeout。"""
    results = [_ok_response()] * BASE_CALLS
    results += [_ok_response("红色"), _ok_response("蓝色")]
    with _patch_completion(*results) as mock_completion:
        profile = probe(**PROBE_ARGS, user_config=USER_CONFIG)
    assert profile["vision"]["supported"] is True

    vision_calls = _vision_call_params(mock_completion)
    for params in vision_calls:
        assert params["max_tokens"] == VISION_MAX_TOKENS == 500
        assert params["timeout"] == VISION_TIMEOUT == 45

    non_vision = [
        c.kwargs for c in mock_completion.call_args_list
        if not isinstance(c.kwargs["messages"][0]["content"], list)
    ]
    assert len(non_vision) == BASE_CALLS
    for params in non_vision:
        assert params["max_tokens"] == PROBE_MAX_TOKENS == 256
        assert "timeout" not in params


# ---------------------------------------------------------------------------
# ⑥ 档案 vision 子对象结构 + 陈旧 true 覆盖
# ---------------------------------------------------------------------------


def test_profile_vision_subobject_shape(profile_path):
    """|llm 键落盘档案含 vision 子对象 {supported, input, probed_at}（ISO 秒级）。"""
    results = [_ok_response()] * BASE_CALLS
    results += [_ok_response("红色"), _ok_response("蓝色")]
    with _patch_completion(*results):
        probe(**PROBE_ARGS, user_config=USER_CONFIG)
    saved = read_profile(PROBE_ARGS["api_base"], PROBE_ARGS["model"], profile_path=profile_path)
    assert set(saved["vision"].keys()) == {"supported", "input", "probed_at"}
    assert saved["vision"]["supported"] is True
    assert saved["vision"]["input"] == ["text", "image"]
    from datetime import datetime

    datetime.fromisoformat(saved["vision"]["probed_at"])  # 合法 ISO


def test_vision_false_overwrites_stale_true(profile_path):
    """陈旧档案 vision.supported=true，本次探测失败 → false 覆盖落盘。"""
    stale = {
        "api_base": PROBE_ARGS["api_base"].rstrip("/"),
        "model": PROBE_ARGS["model"],
        "probed_at": "2026-01-01T00:00:00",
        "probe_status": "ok",
        "ignores_unknown": False,
        "reasoning_effort": {"supported": [], "unsupported": []},
        "thinking": {},
        "vision": {"supported": True, "input": ["text", "image"], "probed_at": "2026-01-01T00:00:00"},
    }
    profile_path.write_text(json.dumps({model_probe.build_profile_key(PROBE_ARGS["api_base"], PROBE_ARGS["model"]): stale}), encoding="utf-8")

    results = [_ok_response()] * BASE_CALLS
    results += [_server_error()]  # vision 失败
    with _patch_completion(*results):
        probe(**PROBE_ARGS, user_config=USER_CONFIG)
    saved = read_profile(PROBE_ARGS["api_base"], PROBE_ARGS["model"], profile_path=profile_path)
    assert saved["vision"]["supported"] is False


# ---------------------------------------------------------------------------
# ⑦ lightrag 场景不探 vision
# ---------------------------------------------------------------------------


def test_lightrag_scenario_has_no_vision(profile_path):
    """lightrag=True：不跑 vision 子扫描——|lightrag 键无 vision 键、
    全程无多模态请求（调用数 = 主探测项 10：7 值域 + 无效值探针 + 2 thinking）。"""
    results = [_ok_response()] * BASE_CALLS
    with _patch_completion(*results) as mock_completion:
        profile = probe(**LIGHTRAG_PROBE_ARGS, user_config=USER_CONFIG)
    assert "vision" not in profile

    saved = read_profile(PROBE_ARGS["api_base"], PROBE_ARGS["model"], lightrag=True, profile_path=profile_path)
    assert "vision" not in saved
    assert _vision_call_params(mock_completion) == []
    assert mock_completion.call_count == BASE_CALLS


def test_llm_and_lightrag_keys_vision_independence(profile_path):
    """双键并存：|llm 有 vision、|lightrag 无 vision（同模型两场景互不串扰）。"""
    # 先跑 llm 场景（红蓝全对）
    results = [_ok_response()] * BASE_CALLS
    results += [_ok_response("红色"), _ok_response("蓝色")]
    with _patch_completion(*results):
        probe(**PROBE_ARGS, user_config=USER_CONFIG)
    # 再跑 lightrag 场景（主探测项 10：7 值域 + 无效值探针 + 2 thinking）
    with _patch_completion(*[_ok_response()] * BASE_CALLS):
        probe(**LIGHTRAG_PROBE_ARGS, user_config=USER_CONFIG)

    data = load_profile(profile_path)
    llm_key = model_probe.build_profile_key(PROBE_ARGS["api_base"], PROBE_ARGS["model"])
    lightrag_key = model_probe.build_profile_key(PROBE_ARGS["api_base"], PROBE_ARGS["model"], lightrag=True)
    assert data[llm_key]["vision"]["supported"] is True
    assert "vision" not in data[lightrag_key]
