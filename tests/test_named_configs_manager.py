"""命名配置合集（llm-configs.json）改造测试——plan T3 / 契约 C4。

monkeypatch 配置路径到 tmp_path，不触碰真实 ~/.niu/config/。
"""
import json
import shutil
import subprocess
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


@pytest.fixture
def tmp_config(monkeypatch, tmp_path):
    """把模块级 CONFIG_DIR / USER_CONFIG_PATH / LLM_CONFIGS_PATH 重定向到 tmp_path。"""
    monkeypatch.setattr(ncm, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ncm, "USER_CONFIG_PATH", tmp_path / "user-config.json")
    monkeypatch.setattr(ncm, "LLM_CONFIGS_PATH", tmp_path / "llm-configs.json")
    return tmp_path


def _write_user_config(tmp_config, llm=None, lightrag_llm=None):
    config = {
        "llm": llm
        or {
            "presetId": "",
            "apiKey": "",
            "apiBase": "",
            "model": "",
            "type": "openai",
        },
        "context": {"contextWindowSize": 200000},
    }
    if lightrag_llm is not None:
        config["lightrag_llm"] = lightrag_llm
    (tmp_config / "user-config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _read_user_config(tmp_config):
    return json.loads((tmp_config / "user-config.json").read_text(encoding="utf-8"))


def _write_configs(tmp_config, configs):
    (tmp_config / "llm-configs.json").write_text(
        json.dumps({"configs": configs}, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _read_configs(tmp_config):
    return json.loads((tmp_config / "llm-configs.json").read_text(encoding="utf-8"))[
        "configs"
    ]


def _write_broken_configs(tmp_config):
    (tmp_config / "llm-configs.json").write_text("{ not valid json", encoding="utf-8")


ENTRY_DOUBAO = {
    "llm": {
        "presetId": "畸形",
        "apiKey": "k1",
        "apiBase": "https://api.doubao",
        "model": "doubao-pro",
        "type": "openai",
        "custom_agent_field": "keep-me",
    },
    "lightrag_llm": {
        "apiBase": "https://api.doubao",
        "model": "doubao-lite",
        "type": "openai",
        "max_tokens": 8192,
        "custom_field": "keep-me-too",
    },
}


def test_load_named_configs_missing_returns_empty(tmp_config):
    """合集文件不存在 = 空合集。"""
    assert not (tmp_config / "llm-configs.json").exists()
    assert ncm.load_named_configs() == {}


def test_set_llm_config_preset_loads_both_sections(tmp_config):
    """整条加载：llm+lightrag_llm 两段整体替换 + presetId 归一化为键值 + 跳过同步（合集条目不变）。"""
    _write_configs(tmp_config, {"豆包": ENTRY_DOUBAO})
    _write_user_config(
        tmp_config,
        llm={
            "presetId": "旧配置",
            "apiKey": "old-key",
            "apiBase": "https://old",
            "model": "old-model",
            "type": "openai",
            "reasoning_effort": "high",
        },
        lightrag_llm={"model": "old-lr", "apiBase": "https://old"},
    )

    result = ncm.set_llm_config(preset_id="豆包")
    assert result["status"] == "updated"

    config = _read_user_config(tmp_config)
    # 段级整体替换：旧段的 reasoning_effort 消失，条目的 Agent 额外键保留
    expected_llm = dict(ENTRY_DOUBAO["llm"])
    expected_llm["presetId"] = "豆包"  # 归一化为键值（畸形条目不带偏）
    assert config["llm"] == expected_llm
    assert config["lightrag_llm"] == ENTRY_DOUBAO["lightrag_llm"]

    # 加载型跳过写后同步：合集条目原样（畸形 presetId 不被刷正）
    assert _read_configs(tmp_config)["豆包"] == ENTRY_DOUBAO


def test_set_lightrag_preset_loads_only_lightrag_section(tmp_config):
    """分节加载：set_lightrag_llm_config(preset_id) 只替换 lightrag_llm 段。"""
    _write_configs(tmp_config, {"豆包": ENTRY_DOUBAO})
    _write_user_config(
        tmp_config,
        llm={
            "presetId": "主配置",
            "apiKey": "main-key",
            "apiBase": "https://main",
            "model": "main-model",
            "type": "openai",
        },
        lightrag_llm={"model": "old-lr"},
    )

    result = ncm.set_lightrag_llm_config(preset_id="豆包")
    assert result["status"] == "updated"

    config = _read_user_config(tmp_config)
    assert config["lightrag_llm"] == ENTRY_DOUBAO["lightrag_llm"]
    assert config["llm"]["model"] == "main-model"  # llm 段不动
    assert config["llm"]["presetId"] == "主配置"

    # 跳过同步
    assert _read_configs(tmp_config)["豆包"] == ENTRY_DOUBAO


def test_preset_mixed_with_params_ignores_rest(tmp_config):
    """混用：带 preset_id 又有其余参数 = 加载型，其余忽略并在结果说明。"""
    _write_configs(tmp_config, {"豆包": ENTRY_DOUBAO})
    _write_user_config(tmp_config)

    result = ncm.set_llm_config(preset_id="豆包", model="other-model", api_key="k2")
    assert result["status"] == "updated"
    assert "已忽略其余参数" in result["message"]
    assert "model" in result["message"] and "api_key" in result["message"]

    config = _read_user_config(tmp_config)
    assert config["llm"]["model"] == "doubao-pro"  # 条目值，非传入值
    assert config["llm"]["apiKey"] == "k1"


def test_item_modify_syncs_three_section_snapshot(tmp_config):
    """逐项修改型：llm.presetId 非空时写后同步，单条 upsert 三段快照（llm+lightrag_llm+vision_llm），其它条目不动。"""
    _write_user_config(
        tmp_config,
        llm={
            "presetId": "豆包",
            "apiKey": "k1",
            "apiBase": "https://api.doubao",
            "model": "doubao-pro",
            "type": "openai",
            "custom_agent_field": "keep-me",
        },
        lightrag_llm={"model": "doubao-lite", "max_tokens": 8192},
    )
    # user-config.json 手工补 vision_llm 段（主 Agent 手工配置形态）——须入快照
    user = _read_user_config(tmp_config)
    user["vision_llm"] = {"apiKey": "vk", "apiBase": "http://192.168.3.88:8080/v1", "model": "qwen38-xl"}
    (tmp_config / "user-config.json").write_text(
        json.dumps(user, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _write_configs(
        tmp_config,
        {
            "豆包": {"llm": {"model": "stale"}, "lightrag_llm": {"model": "stale-lr"}},
            "其它": {"llm": {"model": "other"}, "lightrag_llm": {}},
        },
    )

    result = ncm.set_llm_config(model="doubao-pro-max")
    assert result["status"] == "updated"
    assert "warning" not in result

    configs = _read_configs(tmp_config)
    user = _read_user_config(tmp_config)
    assert configs["豆包"] == {
        "llm": user["llm"],
        "lightrag_llm": user["lightrag_llm"],
        "vision_llm": user["vision_llm"],  # vision_llm 段入三段快照（手工配置持久化）
    }
    assert configs["豆包"]["llm"]["model"] == "doubao-pro-max"
    assert configs["豆包"]["llm"]["custom_agent_field"] == "keep-me"  # Agent 额外键入快照
    # 其它条目不动（同步只 upsert 当前 presetId 条目；旧两段条目保持原样向后兼容）
    assert configs["其它"] == {"llm": {"model": "other"}, "lightrag_llm": {}}


def test_item_modify_without_preset_id_no_sync(tmp_config):
    """逐项修改型：llm.presetId 为空 → 不创建/不写合集。"""
    _write_user_config(tmp_config)

    result = ncm.set_llm_config(model="m1")
    assert result["status"] == "updated"
    assert not (tmp_config / "llm-configs.json").exists()


def test_clear_model_syncs_empty_lightrag_snapshot(tmp_config):
    """model=="" 清空分支保留且适用写后同步：合集条目 lightrag_llm 同步为 {}。"""
    _write_user_config(
        tmp_config,
        llm={
            "presetId": "豆包",
            "apiKey": "k1",
            "apiBase": "https://api.doubao",
            "model": "doubao-pro",
            "type": "openai",
        },
        lightrag_llm={
            "presetId": "豆包",
            "apiBase": "https://api.doubao",
            "model": "doubao-lite",
            "type": "openai",
        },
    )
    _write_configs(
        tmp_config,
        {"豆包": {"llm": {"model": "stale"}, "lightrag_llm": {"model": "doubao-lite"}}},
    )

    result = ncm.set_lightrag_llm_config(model="")
    assert result["status"] == "cleared"

    user = _read_user_config(tmp_config)
    assert "lightrag_llm" not in user  # 清空后段移除（回落主 llm）

    configs = _read_configs(tmp_config)
    assert configs["豆包"]["lightrag_llm"] == {}  # 清空同样照快照同步
    assert configs["豆包"]["llm"] == user["llm"]


def test_preset_name_not_found(tmp_config):
    """名字不存在 → 明确错误，user-config.json 不被改动。"""
    _write_configs(tmp_config, {"豆包": ENTRY_DOUBAO})
    _write_user_config(tmp_config, llm={"presetId": "", "model": "keep", "type": "openai"})

    result = ncm.set_llm_config(preset_id="不存在")
    assert result["status"] == "error"
    assert result["message"] == "配置 '不存在' 不存在"

    result = ncm.set_lightrag_llm_config(preset_id="不存在")
    assert result["status"] == "error"
    assert result["message"] == "配置 '不存在' 不存在"

    assert _read_user_config(tmp_config)["llm"]["model"] == "keep"


def test_corrupted_collection_load_error(tmp_config):
    """损坏路径 ①：加载型调用 → error 配置合集文件损坏，user-config.json 不被改动。"""
    _write_broken_configs(tmp_config)
    _write_user_config(tmp_config, llm={"presetId": "", "model": "keep", "type": "openai"})

    result = ncm.set_llm_config(preset_id="豆包")
    assert result["status"] == "error"
    assert result["message"] == "配置合集文件损坏，无法加载"

    result = ncm.set_lightrag_llm_config(preset_id="豆包")
    assert result["status"] == "error"
    assert result["message"] == "配置合集文件损坏，无法加载"

    assert _read_user_config(tmp_config)["llm"]["model"] == "keep"


def test_corrupted_collection_sync_warning(tmp_config):
    """损坏路径 ②：逐项修改型写后同步 → 跳过合集写 + warning，坏文件不被覆写。"""
    _write_user_config(
        tmp_config,
        llm={"presetId": "豆包", "apiBase": "https://a", "model": "m1", "type": "openai"},
    )
    _write_broken_configs(tmp_config)
    broken_bytes = (tmp_config / "llm-configs.json").read_bytes()

    result = ncm.set_llm_config(model="m2")
    assert result["status"] == "updated"  # user-config.json 已写不受影响
    assert result["warning"] == "配置合集文件损坏未同步"

    assert _read_user_config(tmp_config)["llm"]["model"] == "m2"
    # 损坏文件内容保留（禁"损坏=空合集"整体覆写销毁全部条目）
    assert (tmp_config / "llm-configs.json").read_bytes() == broken_bytes


def test_corrupted_collection_list_degrades(tmp_config):
    """损坏路径 ③：list_llm_configs → 空列表 + warning 字段。"""
    _write_broken_configs(tmp_config)

    result = ncm.list_llm_configs()
    assert result["configs"] == []
    assert "warning" in result


def test_non_dict_configs_treated_as_corrupted(tmp_config):
    """损坏路径 ④：configs 值为非对象（列表）→ 与 JSON 损坏同判，走两条保护路径。"""
    _write_user_config(
        tmp_config,
        llm={"presetId": "豆包", "apiBase": "https://a", "model": "m1", "type": "openai"},
    )
    _write_configs(tmp_config, ["豆包"])  # {"configs": [...]}：合法 JSON 但类型错误
    bad_bytes = (tmp_config / "llm-configs.json").read_bytes()

    # 读侧降级：list_llm_configs → 空列表 + warning（防 .items() AttributeError）
    result = ncm.list_llm_configs()
    assert result["configs"] == []
    assert "warning" in result

    # 写侧保护：逐项修改型 → user-config 写入成功 + warning，坏文件不被覆写
    result = ncm.set_llm_config(model="m2")
    assert result["status"] == "updated"
    assert result["warning"] == "配置合集文件损坏未同步"
    assert _read_user_config(tmp_config)["llm"]["model"] == "m2"
    # 类型错误文件内容保留（禁"损坏=空合集"整体覆写销毁全部条目）
    assert (tmp_config / "llm-configs.json").read_bytes() == bad_bytes


def test_list_llm_configs_normal(tmp_config):
    """list 正常路径：返回 [{name, model, apiBase}] 摘要。"""
    _write_configs(
        tmp_config,
        {
            "豆包": ENTRY_DOUBAO,
            "DeepSeek": {
                "llm": {"apiBase": "https://api.deepseek", "model": "deepseek-v3"},
                "lightrag_llm": {},
            },
        },
    )

    result = ncm.list_llm_configs()
    assert "warning" not in result
    assert result["configs"] == [
        {"name": "豆包", "model": "doubao-pro", "apiBase": "https://api.doubao"},
        {"name": "DeepSeek", "model": "deepseek-v3", "apiBase": "https://api.deepseek"},
    ]


def test_malformed_entry_guards(tmp_config):
    """畸形条目（手工编辑成非对象）三处守卫：list 跳过 / 两个加载型 → error 格式损坏。"""
    _write_configs(
        tmp_config,
        {"好": ENTRY_DOUBAO, "坏": "not-a-dict"},
    )
    _write_user_config(tmp_config, llm={"presetId": "", "model": "keep", "type": "openai"})

    # list_llm_configs：非 dict 条目跳过，不抛 AttributeError
    result = ncm.list_llm_configs()
    assert result["configs"] == [
        {"name": "好", "model": "doubao-pro", "apiBase": "https://api.doubao"}
    ]

    # set_llm_config 加载型：畸形条目 → error
    result = ncm.set_llm_config(preset_id="坏")
    assert result["status"] == "error"
    assert result["message"] == "配置 '坏' 格式损坏"

    # set_lightrag_llm_config 加载型：畸形条目 → error
    result = ncm.set_lightrag_llm_config(preset_id="坏")
    assert result["status"] == "error"
    assert result["message"] == "配置 '坏' 格式损坏"

    # 两处 error 均不改 user-config.json
    assert _read_user_config(tmp_config)["llm"]["model"] == "keep"


# ============== 跨端格式 parity 锁（Node 写 ↔ Python 读）==============

_JS_LIB = (
    Path(__file__).resolve().parent.parent / "ui" / "main" / "lib" / "named-configs.js"
)


def _require_node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node 不可用（项目开发环境要求 Node.js）")
    return node


def test_cross_end_node_write_python_read(tmp_config):
    """parity ①：Node upsertNamedConfig 写两条 → Python load_named_configs 读回完整。"""
    node = _require_node()
    script = (
        f"const {{upsertNamedConfig}} = require({json.dumps(str(_JS_LIB))});"
        f"const dir = {json.dumps(str(tmp_config))};"
        "upsertNamedConfig(dir, '豆包', {"
        "  llm: {presetId: '豆包', apiKey: 'k1', apiBase: 'https://api.doubao',"
        "        model: 'doubao-pro', type: 'openai'},"
        "  lightrag_llm: {model: 'doubao-lite', max_tokens: 8192}"
        "});"
        "upsertNamedConfig(dir, 'DeepSeek', {"
        "  llm: {apiBase: 'https://api.deepseek', model: 'deepseek-v3', type: 'openai'},"
        "  lightrag_llm: {}"
        "});"
    )
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)

    configs = ncm.load_named_configs()
    assert configs == {
        "豆包": {
            "llm": {
                "presetId": "豆包",
                "apiKey": "k1",
                "apiBase": "https://api.doubao",
                "model": "doubao-pro",
                "type": "openai",
            },
            "lightrag_llm": {"model": "doubao-lite", "max_tokens": 8192},
        },
        "DeepSeek": {
            "llm": {
                "apiBase": "https://api.deepseek",
                "model": "deepseek-v3",
                "type": "openai",
            },
            "lightrag_llm": {},
        },
    }


def test_cross_end_python_write_node_read(tmp_config):
    """parity ②：Python 写后同步写出 → Node 读回，顶层键=='configs' 且条目完整。"""
    node = _require_node()
    _write_user_config(
        tmp_config,
        llm={
            "presetId": "豆包",
            "apiKey": "k1",
            "apiBase": "https://api.doubao",
            "model": "doubao-pro",
            "type": "openai",
        },
        lightrag_llm={"model": "doubao-lite", "max_tokens": 8192},
    )
    result = ncm.set_llm_config(model="doubao-pro-max")  # 触发写后同步
    assert result["status"] == "updated"

    script = (
        "const fs = require('fs');"
        f"const data = JSON.parse(fs.readFileSync({json.dumps(str(tmp_config / 'llm-configs.json'))}, 'utf-8'));"
        "const keys = Object.keys(data);"
        "if (keys.length !== 1 || keys[0] !== 'configs') {"
        "  console.error('top-level keys mismatch: ' + JSON.stringify(keys)); process.exit(1);"
        "}"
        "const e = data.configs['豆包'];"
        "if (!e || e.llm.model !== 'doubao-pro-max' || e.llm.presetId !== '豆包'"
        "    || e.lightrag_llm.model !== 'doubao-lite' || e.lightrag_llm.max_tokens !== 8192) {"
        "  console.error('entry mismatch: ' + JSON.stringify(e)); process.exit(1);"
        "}"
    )
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)
