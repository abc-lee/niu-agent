"""config-manager 合集两段快照语义测试（vision_llm 不入合集，恒在 user-config.json 顶层）。

覆盖：逐项修改型写后同步（_sync_named_config）/ set_llm_config(preset_id) 整条加载不动 vision_llm 段。

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


# ============== config-manager 合集两段快照（vision_llm 不入合集） ==============


@pytest.fixture
def tmp_config(monkeypatch, tmp_path):
    """把模块级 CONFIG_DIR / USER_CONFIG_PATH / LLM_CONFIGS_PATH 重定向到 tmp_path。"""
    monkeypatch.setattr(ncm, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ncm, "USER_CONFIG_PATH", tmp_path / "user-config.json")
    monkeypatch.setattr(ncm, "LLM_CONFIGS_PATH", tmp_path / "llm-configs.json")
    return tmp_path


def _read_config(tmp_config):
    return json.loads((tmp_config / "user-config.json").read_text(encoding="utf-8"))


def test_llm_item_modify_syncs_two_section_snapshot(tmp_config):
    """逐项修改型写后同步：合集条目 = llm+lightrag_llm 两段快照（vision_llm 不入合集）。"""
    ncm.set_llm_config(api_key="k1", api_base="https://api.main/v1", model="main-model")
    # set_llm_config 不带 presetId → 无同步；手工写 presetId 触发同步路径
    config = _read_config(tmp_config)
    config["llm"]["presetId"] = "本地"
    config["vision_llm"] = {"model": "qwen38-xl", "apiBase": "http://192.168.3.88:8080/v1", "max_tokens": 8192}
    (tmp_config / "user-config.json").write_text(
        json.dumps(config, ensure_ascii=False), encoding="utf-8"
    )

    result = ncm.set_llm_config(model="main-model")
    assert result["status"] == "updated"
    assert "warning" not in result

    configs = json.loads(
        (tmp_config / "llm-configs.json").read_text(encoding="utf-8")
    )["configs"]
    entry = configs["本地"]
    assert set(entry.keys()) == {"llm", "lightrag_llm"}  # vision_llm 不入合集
    assert entry["llm"]["model"] == "main-model"
    # vision_llm 恒在 user-config.json 顶层：同步后顶层段原样保留（铺场写入）
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
    (tmp_config / "user-config.json").write_text(
        json.dumps({"vision_llm": {"model": "stale-vision"}}, ensure_ascii=False), encoding="utf-8")

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
