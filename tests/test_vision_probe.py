"""vision 双色交叉子扫描单测（plan v0.5.2 §4-V1 / §6-T1）。

覆盖：
① 红蓝全对 → user-config.json llm 段 capabilities.input=["text","image"] + model/probed_at
   + 档案不落 vision + 其他段/字段不触碰
② 单色错（红答绿）→ input=["text"]，蓝图不再发（短路）
③ 请求异常（5xx）→ 三态 None：不写 capabilities（保持旧值）不毒化主探测项
   （probe_status=ok、reasoning_effort/thinking 结果照常落盘）
④ 超时 → 重试 1 次仍超时 → 三态 None：不写（防网络抖动降级已知视觉模型）
⑤ max_tokens 传参断言：vision 请求 =500（R7），非 vision 请求 =256；timeout=45
⑥ capabilities 子对象结构 {model, input, probed_at}；陈旧 ["text","image"] 被 ["text"] 覆盖
⑦ lightrag 场景不跑 vision 扫描（无多模态请求、user-config llm 段不被触碰）
⑧ 命名配置同步：presetId 非空 → llm-configs.json upsert 三段快照（含 capabilities）；
   presetId 空 → 不建文件；合集损坏 → 跳过同步不写坏
⑨ V8c 串模型防护：配置 llm.model ≠ 本次探测 model → 跳过写入保持旧值
⑩ P2-1 空回答语义：200 但 content None/空（reasoning 截断形态）→ 三态 None 不写，
   旧 ["text","image"] 保持（空串恒未命中色系不得误判 False 降级）

禁真实 LLM：所有请求 patch litellm.completion。测试图 PIL 现场生成（32×32 纯色）。
user-config.json / llm-configs.json 路径 monkeypatch 到 tmp（不碰真实 ~/.niu/config/）。
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


# 其他段哨兵值（断言探测写入不触碰 llm/lightrag_llm 之外的顶级段）
EXTRA_SECTIONS = {
    "storage": {"dir": "/tmp/niu-store"},
    "logging": {"level": "info"},
    "context": {"keepRecentTurns": 5},
}


@pytest.fixture(autouse=True)
def probe_env(tmp_path, monkeypatch):
    """隔离 vision 结果落点（user-config.json / llm-configs.json → tmp，不碰真实 ~/.niu/config/）。

    user-config.json 预置 USER_CONFIG + 其他段哨兵值；llm-configs.json 不预建
    （命名同步测试按需自建/预置损坏态）。返回 {"user_config": Path, "named_configs": Path}。
    """
    uc_path = tmp_path / "user-config.json"
    uc_path.write_text(
        json.dumps({**USER_CONFIG, **EXTRA_SECTIONS}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    nc_path = tmp_path / "llm-configs.json"
    monkeypatch.setattr(model_probe, "USER_CONFIG_PATH", str(uc_path))
    monkeypatch.setattr(model_probe, "NAMED_CONFIGS_PATH", str(nc_path))
    return {"user_config": uc_path, "named_configs": nc_path}


def _read_user_config(env) -> dict:
    return json.loads(env["user_config"].read_text(encoding="utf-8"))


def _caps_of(env):
    """探测后 user-config.json llm 段 capabilities（未写入 → None）。"""
    return (_read_user_config(env).get("llm") or {}).get("capabilities")


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


def test_vision_both_colors_correct_writes_capabilities(profile_path, probe_env):
    """红答红系词 + 蓝答蓝系词 → user-config.json llm 段 capabilities.input=["text","image"]，
    档案不落 vision，其他段/字段不触碰。"""
    results = [_ok_response()] * BASE_CALLS
    results += [_ok_response("红色"), _ok_response("blue")]
    with _patch_completion(*results) as mock_completion:
        profile = probe(**PROBE_ARGS, user_config=USER_CONFIG)
    assert profile["probe_status"] == "ok"

    # 双色交叉请求形态：恰好 2 个多模态请求，红图在前蓝图在后，data URI 与 PIL 生成一致
    vision_calls = _vision_call_params(mock_completion)
    assert len(vision_calls) == 2
    assert _image_url_of(vision_calls[0]) == RED_URI
    assert _image_url_of(vision_calls[1]) == BLUE_URI
    for params in vision_calls:
        text_part = next(p for p in params["messages"][0]["content"] if p.get("type") == "text")
        assert "颜色" in text_part["text"]

    # 落点 = user-config.json llm 段 capabilities（model 记探测时模型名，供读侧比对）
    caps = _caps_of(probe_env)
    assert caps["model"] == PROBE_ARGS["model"]
    assert caps["input"] == ["text", "image"]
    from datetime import datetime

    datetime.fromisoformat(caps["probed_at"])  # 合法 ISO

    # 档案不落 vision（能力档案只留既有探测项）
    saved = read_profile(PROBE_ARGS["api_base"], PROBE_ARGS["model"], profile_path=profile_path)
    assert "vision" not in saved
    assert "vision" not in profile

    # 其他段/字段不触碰：llm 段其余键 + lightrag_llm 段 + 顶级哨兵段原样
    cfg = _read_user_config(probe_env)
    assert {k: v for k, v in cfg["llm"].items() if k != "capabilities"} == USER_CONFIG["llm"]
    assert cfg["lightrag_llm"] == USER_CONFIG["lightrag_llm"]
    for section, sentinel in EXTRA_SECTIONS.items():
        assert cfg[section] == sentinel

    assert mock_completion.call_count == BASE_CALLS + 2


def test_vision_english_and_cn_variants_all_match(profile_path, probe_env):
    """色系命中词覆盖中英文变体：红答 crimson / 蓝答 青蓝 → input=["text","image"]。"""
    results = [_ok_response()] * BASE_CALLS
    results += [_ok_response("Crimson"), _ok_response("青蓝色")]
    with _patch_completion(*results):
        probe(**PROBE_ARGS, user_config=USER_CONFIG)
    assert _caps_of(probe_env)["input"] == ["text", "image"]


# ---------------------------------------------------------------------------
# ② 单色错 → supported=false（短路——蓝图不再发）
# ---------------------------------------------------------------------------


def test_vision_red_wrong_writes_text_only_and_short_circuits(profile_path, probe_env):
    """红图答绿系词 → input=["text"]，蓝图请求不发（短路省一次调用）。"""
    results = [_ok_response()] * BASE_CALLS
    results += [_ok_response("绿色")]  # 红图错答；蓝图不应再被请求
    with _patch_completion(*results) as mock_completion:
        profile = probe(**PROBE_ARGS, user_config=USER_CONFIG)
    assert _caps_of(probe_env)["input"] == ["text"]
    assert "vision" not in profile
    vision_calls = _vision_call_params(mock_completion)
    assert len(vision_calls) == 1  # 只发了红图
    assert mock_completion.call_count == BASE_CALLS + 1


def test_vision_blue_wrong_writes_text_only(profile_path, probe_env):
    """红对蓝错 → input=["text"]（两答全对才 ["text","image"]）。"""
    results = [_ok_response()] * BASE_CALLS
    results += [_ok_response("红色"), _ok_response("green")]
    with _patch_completion(*results) as mock_completion:
        probe(**PROBE_ARGS, user_config=USER_CONFIG)
    assert _caps_of(probe_env)["input"] == ["text"]
    assert len(_vision_call_params(mock_completion)) == 2


# ---------------------------------------------------------------------------
# ③ 请求异常 → false 不毒化主探测项
# ---------------------------------------------------------------------------


def test_vision_request_exception_no_write_without_poisoning(profile_path, probe_env):
    """vision 请求 5xx → 三态 None：不写 capabilities（保持旧值）；probe_status=ok、
    reasoning_effort/thinking 结果照常落盘（子扫描失败不得毒化主探测项）。"""
    results = [_ok_response()] * BASE_CALLS
    results += [_server_error()]  # 红图请求 500
    with _patch_completion(*results) as mock_completion:
        profile = probe(**PROBE_ARGS, user_config=USER_CONFIG)
    assert profile["probe_status"] == "ok"
    assert _caps_of(probe_env) is None  # 请求失败 → 不写（无旧值可保持）
    # 主探测项结果完整不受影响
    assert len(profile["reasoning_effort"]["supported"]) == 7
    assert profile["thinking"]["enabled"] is True

    saved = read_profile(PROBE_ARGS["api_base"], PROBE_ARGS["model"], profile_path=profile_path)
    assert saved["probe_status"] == "ok"
    assert "vision" not in saved


def test_vision_request_failure_keeps_stale_capabilities(profile_path, probe_env):
    """V8d 核心不变量：陈旧 capabilities.input=["text","image"]，本次 vision 请求失败
    （5xx）→ 三态 None 不写——旧值原样保留（防网络抖动把已知视觉模型降级 text-only）。"""
    cfg = _read_user_config(probe_env)
    cfg["llm"]["capabilities"] = {
        "model": PROBE_ARGS["model"],
        "input": ["text", "image"],
        "probed_at": "2026-01-01T00:00:00",
    }
    probe_env["user_config"].write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    results = [_ok_response()] * BASE_CALLS
    results += [_server_error()]  # 红图请求 500
    with _patch_completion(*results):
        profile = probe(**PROBE_ARGS, user_config=USER_CONFIG)
    assert profile["probe_status"] == "ok"
    caps = _caps_of(probe_env)
    assert caps["input"] == ["text", "image"]  # 旧值未降级
    assert caps["probed_at"] == "2026-01-01T00:00:00"  # 无新写入（未被刷新）


def test_vision_empty_answer_keeps_stale_capabilities(profile_path, probe_env):
    """P2-1：200 但空回答（content None——reasoning 预算耗尽截断形态，重试后仍空）→
    三态 None 不写：旧 capabilities.input=["text","image"] 原样保留（空串恒未命中色系
    会被误判 False，把已探测视觉模型降级 text-only），蓝图不再发（短路）。"""
    cfg = _read_user_config(probe_env)
    cfg["llm"]["capabilities"] = {
        "model": PROBE_ARGS["model"],
        "input": ["text", "image"],
        "probed_at": "2026-01-01T00:00:00",
    }
    probe_env["user_config"].write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    results = [_ok_response()] * BASE_CALLS
    results += [_ok_response(None)]  # 红图请求 200 但 content=None（空回答形态）
    with _patch_completion(*results) as mock_completion:
        profile = probe(**PROBE_ARGS, user_config=USER_CONFIG)
    assert profile["probe_status"] == "ok"
    caps = _caps_of(probe_env)
    assert caps["input"] == ["text", "image"]  # 旧值未降级
    assert caps["probed_at"] == "2026-01-01T00:00:00"  # 无新写入（未被刷新）
    assert len(_vision_call_params(mock_completion)) == 1  # 空回答 → 短路，蓝图不发


# ---------------------------------------------------------------------------
# ④ 超时 → 重试 1 次仍超时 → None（不写）
# ---------------------------------------------------------------------------


def test_vision_timeout_retried_once_then_no_write(profile_path, probe_env):
    """红图首次超时 → 重试 1 次仍超时 → 三态 None：不写 capabilities（不抛、不毒化）。"""
    results = [_ok_response()] * BASE_CALLS
    results += [_timeout_error(), _timeout_error()]  # 首次 + 重试均超时
    with _patch_completion(*results) as mock_completion:
        profile = probe(**PROBE_ARGS, user_config=USER_CONFIG)
    assert profile["probe_status"] == "ok"
    assert _caps_of(probe_env) is None  # 请求失败 → 不写
    vision_calls = _vision_call_params(mock_completion)
    assert len(vision_calls) == 2  # 同一红图两次 attempt（蓝图未发）
    assert mock_completion.call_count == BASE_CALLS + 2


def test_vision_timeout_then_ok_still_supported(profile_path, probe_env):
    """红图首次超时 → 重试成功答对 + 蓝图答对 → input=["text","image"]（超时 ≠ 不支持）。"""
    results = [_ok_response()] * BASE_CALLS
    results += [_timeout_error(), _ok_response("红色"), _ok_response("蓝色")]
    with _patch_completion(*results):
        probe(**PROBE_ARGS, user_config=USER_CONFIG)
    assert _caps_of(probe_env)["input"] == ["text", "image"]


# ---------------------------------------------------------------------------
# ⑤ max_tokens / timeout 传参断言（R7）
# ---------------------------------------------------------------------------


def test_vision_request_max_tokens_and_timeout(profile_path, probe_env):
    """vision 请求 max_tokens=500（R7 reasoning 占预算陷阱）+ timeout=45；
    非 vision 探测请求保持 max_tokens=256、无显式 timeout。"""
    results = [_ok_response()] * BASE_CALLS
    results += [_ok_response("红色"), _ok_response("蓝色")]
    with _patch_completion(*results) as mock_completion:
        probe(**PROBE_ARGS, user_config=USER_CONFIG)
    assert _caps_of(probe_env)["input"] == ["text", "image"]

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
# ⑥ capabilities 子对象结构 + 陈旧 ["text","image"] 覆盖
# ---------------------------------------------------------------------------


def test_capabilities_subobject_shape(profile_path, probe_env):
    """user-config.json llm 段 capabilities = {model, input, probed_at}（ISO 秒级）；档案不落 vision。"""
    results = [_ok_response()] * BASE_CALLS
    results += [_ok_response("红色"), _ok_response("蓝色")]
    with _patch_completion(*results):
        probe(**PROBE_ARGS, user_config=USER_CONFIG)
    caps = _caps_of(probe_env)
    assert set(caps.keys()) == {"model", "input", "probed_at"}
    assert caps["model"] == PROBE_ARGS["model"]
    assert caps["input"] == ["text", "image"]
    from datetime import datetime

    datetime.fromisoformat(caps["probed_at"])  # 合法 ISO

    saved = read_profile(PROBE_ARGS["api_base"], PROBE_ARGS["model"], profile_path=profile_path)
    assert "vision" not in saved


def test_vision_false_overwrites_stale_image_input(profile_path, probe_env):
    """陈旧 capabilities.input=["text","image"]，本次探测完成且无视觉（红图答绿系词，
    三态 False）→ ["text"] 覆盖落盘（区别于请求失败 None 不写）。"""
    cfg = _read_user_config(probe_env)
    cfg["llm"]["capabilities"] = {
        "model": PROBE_ARGS["model"],
        "input": ["text", "image"],
        "probed_at": "2026-01-01T00:00:00",
    }
    probe_env["user_config"].write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    results = [_ok_response()] * BASE_CALLS
    results += [_ok_response("绿色")]  # 红图答绿系词 → 未命中（探测完成，非请求失败）
    with _patch_completion(*results):
        probe(**PROBE_ARGS, user_config=USER_CONFIG)
    assert _caps_of(probe_env)["input"] == ["text"]


# ---------------------------------------------------------------------------
# ⑦ lightrag 场景不探 vision
# ---------------------------------------------------------------------------


def test_lightrag_scenario_has_no_vision(profile_path, probe_env):
    """lightrag=True：不跑 vision 子扫描——档案 |lightrag 键无 vision、user-config llm 段
    不被触碰（无 capabilities 键）、全程无多模态请求（调用数 = 主探测项 10）。"""
    results = [_ok_response()] * BASE_CALLS
    with _patch_completion(*results) as mock_completion:
        profile = probe(**LIGHTRAG_PROBE_ARGS, user_config=USER_CONFIG)
    assert "vision" not in profile

    saved = read_profile(PROBE_ARGS["api_base"], PROBE_ARGS["model"], lightrag=True, profile_path=profile_path)
    assert "vision" not in saved
    assert _caps_of(probe_env) is None  # llm 段未被触碰（不写 capabilities）
    assert _vision_call_params(mock_completion) == []
    assert mock_completion.call_count == BASE_CALLS


def test_llm_and_lightrag_scenario_vision_independence(profile_path, probe_env):
    """双场景并存：llm 探测写 user-config capabilities、lightrag 探测不触碰
    （同模型两场景互不串扰；档案双键均无 vision）。"""
    # 先跑 llm 场景（红蓝全对）
    results = [_ok_response()] * BASE_CALLS
    results += [_ok_response("红色"), _ok_response("蓝色")]
    with _patch_completion(*results):
        probe(**PROBE_ARGS, user_config=USER_CONFIG)
    caps_after_llm = _caps_of(probe_env)
    assert caps_after_llm["input"] == ["text", "image"]
    # 再跑 lightrag 场景（主探测项 10：7 值域 + 无效值探针 + 2 thinking）
    with _patch_completion(*[_ok_response()] * BASE_CALLS):
        probe(**LIGHTRAG_PROBE_ARGS, user_config=USER_CONFIG)

    # lightrag 探测不覆盖 llm 场景写入的 capabilities（probed_at 不变 = 未被重写）
    assert _caps_of(probe_env) == caps_after_llm

    data = load_profile(profile_path)
    llm_key = model_probe.build_profile_key(PROBE_ARGS["api_base"], PROBE_ARGS["model"])
    lightrag_key = model_probe.build_profile_key(PROBE_ARGS["api_base"], PROBE_ARGS["model"], lightrag=True)
    assert "vision" not in data[llm_key]
    assert "vision" not in data[lightrag_key]


# ---------------------------------------------------------------------------
# ⑧ 命名配置同步（llm.presetId → llm-configs.json upsert）
# ---------------------------------------------------------------------------


def _probe_with_preset(probe_env, preset_id):
    """user-config.json llm 段注入 presetId 后跑一次全对探测，返回 (profile, user_cfg)。"""
    cfg = _read_user_config(probe_env)
    if preset_id is None:
        cfg["llm"].pop("presetId", None)
    else:
        cfg["llm"]["presetId"] = preset_id
    probe_env["user_config"].write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    results = [_ok_response()] * BASE_CALLS
    results += [_ok_response("红色"), _ok_response("蓝色")]
    with _patch_completion(*results):
        profile = probe(**PROBE_ARGS, user_config=USER_CONFIG)
    return profile, _read_user_config(probe_env)


def test_named_config_upserted_with_capabilities_snapshot(profile_path, probe_env):
    """presetId 非空 → llm-configs.json upsert 该条目三段快照（llm 段含新 capabilities，
    lightrag_llm/vision_llm 段一并入快照），其他条目不动。"""
    other_entry = {"llm": {"model": "other"}, "lightrag_llm": {}, "vision_llm": {}}
    probe_env["named_configs"].write_text(
        json.dumps({"configs": {"其他配置": other_entry}}, ensure_ascii=False, indent=2), encoding="utf-8")

    _probe_with_preset(probe_env, "豆包")

    data = json.loads(probe_env["named_configs"].read_text(encoding="utf-8"))
    entry = data["configs"]["豆包"]
    assert set(entry.keys()) == {"llm", "lightrag_llm", "vision_llm"}
    caps = entry["llm"]["capabilities"]
    assert caps["model"] == PROBE_ARGS["model"]
    assert caps["input"] == ["text", "image"]
    # 三段快照与主配置一致（含 capabilities 随条目走）
    user_cfg = _read_user_config(probe_env)
    assert entry["llm"] == user_cfg["llm"]
    assert entry["lightrag_llm"] == user_cfg["lightrag_llm"]
    # 其他条目不动
    assert data["configs"]["其他配置"] == other_entry


def test_no_preset_id_does_not_touch_named_configs(profile_path, probe_env):
    """presetId 空/缺省 → llm-configs.json 不创建（capabilities 仍写主配置）。"""
    _probe_with_preset(probe_env, None)

    assert not probe_env["named_configs"].exists()
    assert _caps_of(probe_env)["input"] == ["text", "image"]


def test_corrupt_named_configs_skips_sync_without_clobbering(profile_path, probe_env):
    """llm-configs.json 损坏 → 跳过同步（原坏文件保留不写），主配置 capabilities 照写。"""
    probe_env["named_configs"].write_text("{not valid json", encoding="utf-8")

    _probe_with_preset(probe_env, "豆包")

    assert probe_env["named_configs"].read_text(encoding="utf-8") == "{not valid json"
    assert _caps_of(probe_env)["input"] == ["text", "image"]


# ---------------------------------------------------------------------------
# ⑨ V8c 串模型防护（配置 llm.model ≠ 本次探测 model → 跳过写入）
# ---------------------------------------------------------------------------


def test_model_mismatch_keeps_current_capabilities(profile_path, probe_env):
    """V8c：探测期间配置已切到 other-model（设置页测候选/CLI 测第三方场景）→
    探测全对也不写——当前模型旧 capabilities 原样保留，不被 m1 结果顶掉。"""
    cfg = _read_user_config(probe_env)
    cfg["llm"]["model"] = "other-model"
    cfg["llm"]["capabilities"] = {
        "model": "other-model",
        "input": ["text"],
        "probed_at": "2026-01-01T00:00:00",
    }
    probe_env["user_config"].write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    results = [_ok_response()] * BASE_CALLS
    results += [_ok_response("红色"), _ok_response("蓝色")]  # m1 探测全对
    with _patch_completion(*results):
        profile = probe(**PROBE_ARGS, user_config=USER_CONFIG)
    assert profile["probe_status"] == "ok"
    caps = _caps_of(probe_env)
    assert caps["model"] == "other-model"  # 未被 m1 顶掉
    assert caps["input"] == ["text"]
    assert caps["probed_at"] == "2026-01-01T00:00:00"


def test_model_mismatch_creates_no_capabilities(profile_path, probe_env):
    """V8c：配置模型 ≠ 探测模型且无旧 capabilities → 不创建键（防给非当前模型落能力）。"""
    cfg = _read_user_config(probe_env)
    cfg["llm"]["model"] = "other-model"
    probe_env["user_config"].write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    results = [_ok_response()] * BASE_CALLS
    results += [_ok_response("红色"), _ok_response("蓝色")]
    with _patch_completion(*results):
        probe(**PROBE_ARGS, user_config=USER_CONFIG)
    assert _caps_of(probe_env) is None
