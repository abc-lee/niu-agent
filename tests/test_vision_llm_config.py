"""vision_llm 配置段测试（plan 2026-09-09-vision-retry.md §4-V2 / T2）。

覆盖：
① llm_proxy.get_llm_config(use_vision_config=True) 三形态——
   model 非空独立继承 / model 空回落主 llm + overrides / 与 use_lightrag_config 互斥 ValueError。
② config-manager get/set_vision_llm_config 行为（镜像 lightrag 工具模式）+
   _sync_named_config 两段快照（vision_llm 不入合集，恒在 user-config.json 顶层）+
   函数体开头拒绝 preset_id（已移除；dispatch 与同进程直调两路径均生效）。

全 mock：配置路径 monkeypatch 到 tmp_path，不触碰真实 ~/.niu/config/，禁真实 LLM。
"""
import json
import sys
from pathlib import Path

import pytest

# config-manager 是独立包（mcp-servers/config-manager/src 布局），加入 sys.path
sys.path.insert(
    0,
    str(
        Path(__file__).resolve().parent.parent
        / "mcp-servers"
        / "config-manager"
        / "src"
    ),
)

import niu_config_manager as ncm


# ============== llm_proxy.get_llm_config(use_vision_config) ==============


@pytest.fixture
def vision_config_file(tmp_path, monkeypatch):
    """把 CONFIG_PATH 指向 tmp 文件，返回写配置的辅助函数。"""
    import niu_api.config as niu_cfg

    path = tmp_path / "user-config.json"
    monkeypatch.setattr(niu_cfg, "CONFIG_PATH", str(path))

    def _write(data):
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    return _write


MAIN_LLM = {
    "apiKey": "main-key",
    "apiBase": "https://api.main/v1",
    "model": "main-model",
    "type": "openai",
    "provider": "openai",
    "litellm_kwargs": {"thinking": {"type": "enabled"}},
    "max_tokens": 4096,
}


def test_vision_independent_model_inherits_missing_fields(vision_config_file):
    """model 非空 = 独立模型：apiKey/apiBase/type/provider/litellm_kwargs/max_tokens 全继承，reasoning_effort 默认 ""。"""
    from niu_api.llm_proxy import get_llm_config

    vision_config_file({
        "llm": dict(MAIN_LLM),
        "vision_llm": {"model": "qwen38-xl"},
    })
    cfg = get_llm_config(use_vision_config=True)
    assert cfg["model"] == "qwen38-xl"
    assert cfg["apikey"] == "main-key"
    assert cfg["apibase"] == "https://api.main/v1"
    assert cfg["type"] == "openai"
    assert cfg["provider"] == "openai"
    assert cfg["litellm_kwargs"] == {"thinking": {"type": "enabled"}}
    assert cfg["max_tokens"] == 4096
    assert cfg["reasoning_effort"] == ""


def test_vision_independent_model_own_values_win(vision_config_file):
    """独立模型已配字段不被继承覆盖（只填空键）。"""
    from niu_api.llm_proxy import get_llm_config

    vision_config_file({
        "llm": dict(MAIN_LLM),
        "vision_llm": {
            "model": "qwen38-xl",
            "apiKey": "vision-key",
            "apiBase": "http://192.168.3.88:8080/v1",
            "max_tokens": 8192,
        },
    })
    cfg = get_llm_config(use_vision_config=True)
    assert cfg["apikey"] == "vision-key"
    assert cfg["apibase"] == "http://192.168.3.88:8080/v1"
    assert cfg["max_tokens"] == 8192


def test_vision_empty_model_falls_back_to_main_llm(vision_config_file):
    """model 空 = 回落主 llm 同一模型 + vision_llm 段独立 overrides（reasoning_effort/temperature/max_tokens）。"""
    from niu_api.llm_proxy import get_llm_config

    vision_config_file({
        "llm": dict(MAIN_LLM),
        "vision_llm": {
            "reasoning_effort": "high",
            "temperature": 0.5,
            "max_tokens": 8192,
        },
    })
    cfg = get_llm_config(use_vision_config=True)
    assert cfg["model"] == "main-model"
    assert cfg["apikey"] == "main-key"
    assert cfg["reasoning_effort"] == "high"
    assert cfg["temperature"] == 0.5
    assert cfg["max_tokens"] == 8192


def test_vision_empty_model_no_section_falls_back_plain(vision_config_file):
    """vision_llm 段整体缺失 = 纯主 llm 回落（reasoning_effort 置 ""）。"""
    from niu_api.llm_proxy import get_llm_config

    vision_config_file({"llm": dict(MAIN_LLM)})
    cfg = get_llm_config(use_vision_config=True)
    assert cfg["model"] == "main-model"
    assert cfg["reasoning_effort"] == ""


def test_vision_and_lightrag_mutually_exclusive(vision_config_file):
    """use_lightrag_config 与 use_vision_config 同传 → ValueError。"""
    from niu_api.llm_proxy import get_llm_config

    vision_config_file({"llm": dict(MAIN_LLM), "vision_llm": {"model": "m"}})
    with pytest.raises(ValueError):
        get_llm_config(use_lightrag_config=True, use_vision_config=True)


def test_vision_flag_does_not_affect_default_call(vision_config_file):
    """默认调用（两 flag 均 False）不受 vision_llm 段影响。"""
    from niu_api.llm_proxy import get_llm_config

    vision_config_file({
        "llm": dict(MAIN_LLM),
        "vision_llm": {"model": "qwen38-xl", "apiKey": "vk"},
    })
    cfg = get_llm_config()
    assert cfg["model"] == "main-model"
    assert cfg["apikey"] == "main-key"


# ============== config-manager get/set_vision_llm_config ==============


@pytest.fixture
def tmp_config(monkeypatch, tmp_path):
    """把模块级 CONFIG_DIR / USER_CONFIG_PATH / LLM_CONFIGS_PATH 重定向到 tmp_path。"""
    monkeypatch.setattr(ncm, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ncm, "USER_CONFIG_PATH", tmp_path / "user-config.json")
    monkeypatch.setattr(ncm, "LLM_CONFIGS_PATH", tmp_path / "llm-configs.json")
    return tmp_path


def _read_config(tmp_config):
    return json.loads((tmp_config / "user-config.json").read_text(encoding="utf-8"))


def test_set_vision_llm_config_writes_section(tmp_config):
    """逐项修改型：写入 vision_llm 段，get 读回 configured=True。"""
    result = ncm.set_vision_llm_config(
        model="qwen38-xl",
        api_base="http://192.168.3.88:8080/v1",
        max_tokens=8192,
    )
    assert result["status"] == "updated"

    config = _read_config(tmp_config)
    assert config["vision_llm"]["model"] == "qwen38-xl"
    assert config["vision_llm"]["apiBase"] == "http://192.168.3.88:8080/v1"
    assert config["vision_llm"]["max_tokens"] == 8192

    got = ncm.get_vision_llm_config()
    assert got["configured"] is True
    assert got["model"] == "qwen38-xl"
    assert got["hasApiKey"] is False
    assert got["max_tokens"] == 8192


def test_get_vision_llm_config_unconfigured(tmp_config):
    """未配置 → configured=False、model 空。"""
    ncm.set_vision_llm_config(model="qwen38-xl")
    ncm.set_vision_llm_config(model="")  # 清空
    got = ncm.get_vision_llm_config()
    assert got["configured"] is False
    assert got["model"] == ""


def test_set_vision_llm_config_clear_model(tmp_config):
    """model='' 清空分支：移除模型键、保留 reasoning_effort/max_tokens 独立维度。"""
    ncm.set_vision_llm_config(model="qwen38-xl", max_tokens=8192, reasoning_effort="high")
    result = ncm.set_vision_llm_config(model="")
    assert result["status"] == "cleared"

    config = _read_config(tmp_config)
    section = config.get("vision_llm", {})
    assert "model" not in section
    assert section["reasoning_effort"] == "high"
    assert section["max_tokens"] == 8192


def test_set_vision_llm_config_max_tokens_clear(tmp_config):
    """max_tokens=0 清除该键（回退不传）。"""
    ncm.set_vision_llm_config(model="m", max_tokens=8192)
    assert _read_config(tmp_config)["vision_llm"]["max_tokens"] == 8192

    ncm.set_vision_llm_config(max_tokens=0)
    assert "max_tokens" not in _read_config(tmp_config)["vision_llm"]


def test_set_vision_llm_config_dispatch_rejects_preset_id(tmp_config):
    """preset_id 已移除：dispatch 显式拒绝（status=error + 「preset_id 已移除」文案），防旧调用静默 no-op。"""
    import asyncio

    out = asyncio.run(ncm.call_tool("set_vision_llm_config", {"preset_id": "本地视觉"}))
    payload = json.loads(out[0].text)
    assert payload["status"] == "error"
    assert "preset_id 已移除" in payload["message"]


def test_set_vision_llm_config_direct_call_rejects_preset_id(tmp_config):
    """同进程 ToolRegistry 路径（按签名过滤实参后直调实现函数）：preset_id 同样拒绝且 user-config.json 不变。"""
    ncm.set_vision_llm_config(model="stale")
    before = (tmp_config / "user-config.json").read_bytes()

    result = ncm.set_vision_llm_config(preset_id="本地视觉")
    assert result["status"] == "error"
    assert "preset_id 已移除" in result["message"]
    assert (tmp_config / "user-config.json").read_bytes() == before


def test_vision_item_modify_syncs_two_section_snapshot(tmp_config):
    """逐项修改型写后同步：合集条目 = llm+lightrag_llm 两段快照（vision_llm 不入合集）。"""
    ncm.set_llm_config(api_key="k1", api_base="https://api.main/v1", model="main-model")
    # set_llm_config 不带 presetId → 无同步；手工写 presetId 触发同步路径
    config = _read_config(tmp_config)
    config["llm"]["presetId"] = "本地"
    config["vision_llm"] = {"model": "qwen38-xl", "apiBase": "http://192.168.3.88:8080/v1"}
    (tmp_config / "user-config.json").write_text(
        json.dumps(config, ensure_ascii=False), encoding="utf-8"
    )

    result = ncm.set_vision_llm_config(max_tokens=8192)
    assert result["status"] == "updated"
    assert "warning" not in result

    configs = json.loads(
        (tmp_config / "llm-configs.json").read_text(encoding="utf-8")
    )["configs"]
    entry = configs["本地"]
    assert set(entry.keys()) == {"llm", "lightrag_llm"}  # vision_llm 不入合集
    assert entry["llm"]["model"] == "main-model"
    # vision_llm 恒在 user-config.json 顶层：同步后顶层段原样保留（含本次写入的 max_tokens）
    assert _read_config(tmp_config)["vision_llm"] == {
        "model": "qwen38-xl",
        "apiBase": "http://192.168.3.88:8080/v1",
        "max_tokens": 8192,
    }


def test_set_llm_config_preset_load_keeps_vision_section(tmp_config):
    """set_llm_config(preset_id) 整条加载：llm/lightrag_llm 替换，vision_llm 段不动（条目里的 vision_llm 键被忽略）。"""
    (tmp_config / "llm-configs.json").write_text(
        json.dumps({"configs": {
            "视觉套": {
                "llm": {"model": "entry-main"},
                "lightrag_llm": {},
                "vision_llm": {"model": "qwen38-xl"},
            },
            "旧两段": {
                "llm": {"model": "legacy-main"},
                "lightrag_llm": {"model": "legacy-lr"},
            },
        }}, ensure_ascii=False), encoding="utf-8")
    ncm.set_vision_llm_config(model="stale-vision")

    result = ncm.set_llm_config(preset_id="视觉套")
    assert result["status"] == "updated"
    config = _read_config(tmp_config)
    assert config["llm"]["model"] == "entry-main"
    # vision_llm 段不动：条目里的旧快照不覆盖顶层段
    assert config["vision_llm"] == {"model": "stale-vision"}

    # 旧两段条目（无 vision_llm 键）→ 正常加载，vision_llm 同样不动
    result = ncm.set_llm_config(preset_id="旧两段")
    assert result["status"] == "updated"
    config = _read_config(tmp_config)
    assert config["llm"]["model"] == "legacy-main"
    assert config["vision_llm"] == {"model": "stale-vision"}


def test_vision_tools_registered_in_schemas_and_dispatch(tmp_config):
    """四处注册 parity：TOOL_SCHEMAS 含两工具 + call_tool dispatch 可达（async）。"""
    import asyncio

    assert "get_vision_llm_config" in ncm.TOOL_SCHEMAS
    assert "set_vision_llm_config" in ncm.TOOL_SCHEMAS
    schemas = ncm.get_tool_schemas()
    names = {s["name"] for s in schemas}
    assert {"get_vision_llm_config", "set_vision_llm_config"} <= names

    # dispatch 层：call_tool 走通（结果 JSON 含 vision_llm 键）
    out = asyncio.run(ncm.call_tool("get_vision_llm_config", {}))
    payload = json.loads(out[0].text)
    assert "model" in payload and "configured" in payload
