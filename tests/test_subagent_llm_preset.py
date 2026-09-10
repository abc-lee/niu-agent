"""T4（plan §4-V4）：子 Agent llmPreset 捆绑模型机制测试。

frontmatter llmPreset 指向 user-config.json 顶层段（如 vision_llm）→ call_subagent 用
get_llm_config(use_vision_config=True) 覆盖 llm_config；覆盖点在 temperature merge 之前
（agent 预设 temperature 不丢）；段 model 空 → 回落主配置 + loguru warning 留痕；
无字段 → 零影响（缺省走主配置，不调 get_llm_config）；bundled（config/agents/*.md）
与用户定义（~/.niu/agents/*.md）双目录同生效。

全 mock，禁真实 LLM。call_subagent mock 范式镜像 tests/test_subagent_current_time.py。
"""
import json
from unittest.mock import Mock

MAIN_CFG = {"apikey": "sk-main", "apibase": "http://main/v1", "model": "main-model"}

BASE_CONFIG = {
    "llm": {"model": "main-model", "apiKey": "sk-main", "apiBase": "http://main/v1", "type": "openai"},
}


def _write_config(tmp_path, data):
    p = tmp_path / "user-config.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return str(p)


def _run(monkeypatch, tmp_path, agent_config, config_data):
    """全 mock 调 call_subagent，返回 (result, captured)。captured["client_cfg"] = create_client 收到的配置。"""
    import agent.runner as runner_mod
    from agent import subagent

    captured = {}

    def mock_run(client, system_prompt, user_input, handler, tools_schema, **kwargs):
        return ("done", {"result": "T4_DONE", "data": "ok"}, "")

    def cap_client(cfg):
        captured["client_cfg"] = cfg
        return Mock()

    monkeypatch.setattr(subagent, "_run_agent_loop", mock_run)
    monkeypatch.setattr(subagent, "get_subagent_mcp_tools_schema", lambda name: [])
    # T3 判定非 T4 范围——钉死 False 隔离（避免触碰真实 ~/.niu/model_capabilities.json）
    monkeypatch.setattr(subagent, "_resolve_subagent_has_vision", lambda name, cfg, user_cfg=None: False)
    monkeypatch.setattr(runner_mod, "create_client", cap_client)
    monkeypatch.setattr(runner_mod, "get_tools_schema", lambda include_main_only=False: [])
    monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_config(tmp_path, config_data))
    if agent_config is not None:
        monkeypatch.setattr(subagent, "get_subagent_config", lambda name: agent_config)

    result = subagent.call_subagent(
        agent_name="t4-agent",
        task="test",
        llm_config=dict(MAIN_CFG),
    )
    assert captured.get("client_cfg") is not None, "create_client 未被调用"
    return result, captured


def test_llmpreset_overrides_model_and_apibase(monkeypatch, tmp_path):
    """llmPreset=vision_llm → model/apiBase 被段配置替换。"""
    config = dict(BASE_CONFIG)
    config["vision_llm"] = {"model": "vision-model", "apiBase": "http://vision/v1"}
    _, captured = _run(monkeypatch, tmp_path, {"llmPreset": "vision_llm"}, config)
    cfg = captured["client_cfg"]
    assert cfg["model"] == "vision-model"
    assert cfg["apibase"] == "http://vision/v1"


def test_llmpreset_empty_keys_inherit_from_llm_section(monkeypatch, tmp_path):
    """段只配 model：apiKey/apiBase 继承 llm 段（T2 get_llm_config 语义，T4 消费）。"""
    config = dict(BASE_CONFIG)
    config["vision_llm"] = {"model": "vision-model"}
    _, captured = _run(monkeypatch, tmp_path, {"llmPreset": "vision_llm"}, config)
    cfg = captured["client_cfg"]
    assert cfg["model"] == "vision-model"
    assert cfg["apikey"] == "sk-main"
    assert cfg["apibase"] == "http://main/v1"


def test_llmpreset_temperature_merge_not_lost(monkeypatch, tmp_path):
    """覆盖点在 temperature merge 之前：agent 预设 temperature 不随原 llm_config 被丢。"""
    config = dict(BASE_CONFIG)
    config["vision_llm"] = {"model": "vision-model", "apiBase": "http://vision/v1"}
    _, captured = _run(monkeypatch, tmp_path, {"llmPreset": "vision_llm", "temperature": 0.7}, config)
    cfg = captured["client_cfg"]
    assert cfg["model"] == "vision-model"
    assert cfg.get("temperature") == 0.7


def test_llmpreset_empty_section_fallback_with_warning(monkeypatch, tmp_path):
    """段 model 空 → 回落主配置 + loguru warning 留痕。"""
    config = dict(BASE_CONFIG)
    config["vision_llm"] = {}
    from agent import subagent

    mock_logger = Mock()
    monkeypatch.setattr(subagent, "logger", mock_logger)
    _, captured = _run(monkeypatch, tmp_path, {"llmPreset": "vision_llm"}, config)
    cfg = captured["client_cfg"]
    assert cfg["model"] == "main-model"  # 原 llm_config 未被替换
    assert cfg["apikey"] == "sk-main"
    warned = [str(c.args[0]) for c in mock_logger.warning.call_args_list if c.args]
    assert any("llmPreset" in w and "model 为空" in w for w in warned), f"无回落警示: {warned}"


def test_no_llmpreset_zero_impact(monkeypatch, tmp_path):
    """无 llmPreset 字段 → 零影响：主配置原样，且不调 get_llm_config。"""
    config = dict(BASE_CONFIG)
    config["vision_llm"] = {"model": "vision-model", "apiBase": "http://vision/v1"}

    def boom(**kwargs):
        raise AssertionError("无 llmPreset 时不得调用 get_llm_config")

    monkeypatch.setattr("niu_api.llm_proxy.get_llm_config", boom)
    _, captured = _run(monkeypatch, tmp_path, {}, config)
    cfg = captured["client_cfg"]
    assert cfg["model"] == "main-model"
    assert cfg["apibase"] == "http://main/v1"


def test_unknown_preset_falls_back_with_warning(monkeypatch, tmp_path):
    """不支持的 preset 名 → 回落主配置 + warning（get_llm_config 仅支持 vision_llm 段）。"""
    config = dict(BASE_CONFIG)
    from agent import subagent

    mock_logger = Mock()
    monkeypatch.setattr(subagent, "logger", mock_logger)
    _, captured = _run(monkeypatch, tmp_path, {"llmPreset": "some_other"}, config)
    cfg = captured["client_cfg"]
    assert cfg["model"] == "main-model"
    warned = [str(c.args[0]) for c in mock_logger.warning.call_args_list if c.args]
    assert any("llmPreset" in w for w in warned), f"无警示: {warned}"


def test_unknown_preset_falls_back_main_config_and_main_vision_profile(monkeypatch, tmp_path):
    """P2-1 行为锁：llmPreset=lightrag_llm（不支持段，SUPPORTED_PRESETS 外）→
    llm_config 回落主配置 AND has_vision 按主档案判定（不查段 model——旧判定会误判 True）。"""
    import agent.image_channel as ic
    import agent.runner as runner_mod
    from agent import subagent

    captured = {}
    seen_main_cfgs = []

    def mock_run(client, system_prompt, user_input, handler, tools_schema, **kwargs):
        captured["has_vision"] = kwargs.get("has_vision")
        return ("done", {"result": "T4_DONE", "data": "ok"}, "")

    def cap_client(cfg):
        captured["client_cfg"] = cfg
        return Mock()

    monkeypatch.setattr(subagent, "_run_agent_loop", mock_run)
    monkeypatch.setattr(subagent, "get_subagent_mcp_tools_schema", lambda name: [])
    # 判定侧不 mock——真实 _resolve_subagent_has_vision；主档案钉 False（段 model 非空下旧判定会误判 True）
    monkeypatch.setattr(ic, "main_has_vision", lambda cfg: seen_main_cfgs.append(cfg) or False)
    monkeypatch.setattr(runner_mod, "create_client", cap_client)
    monkeypatch.setattr(runner_mod, "get_tools_schema", lambda include_main_only=False: [])

    config = dict(BASE_CONFIG)
    # lightrag_llm 段 model 非空——若判定误查段（不支持段）将得 True，锁死新行为
    config["lightrag_llm"] = {"model": "lr-model", "apiBase": "http://lr/v1"}
    monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_config(tmp_path, config))
    monkeypatch.setattr(subagent, "get_subagent_config", lambda name: {"llmPreset": "lightrag_llm"})

    subagent.call_subagent(agent_name="t4-agent", task="test", llm_config=dict(MAIN_CFG))

    cfg = captured["client_cfg"]
    assert cfg["model"] == "main-model"  # llm_config 未被 lightrag_llm 段覆盖
    assert cfg["apibase"] == "http://main/v1"
    assert captured["has_vision"] is False  # has_vision 按主档案（False），非段 model 非空
    assert len(seen_main_cfgs) == 1 and seen_main_cfgs[0]["model"] == "main-model"  # 判定入参=主配置


# ---- 双目录同生效（真实 get_subagent_config 读 frontmatter，tmp 目录）----

_MD = """---
llmPreset: vision_llm
temperature: 0.4
description: t4 test agent
---
You are the t4 test agent.
"""


def _run_real_md(monkeypatch, tmp_path, dir_attr, agent_name):
    """不 mock get_subagent_config——真实 frontmatter 解析路径。"""
    import agent.runner as runner_mod
    from agent import subagent

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / f"{agent_name}.md").write_text(_MD, encoding="utf-8")
    monkeypatch.setattr(subagent, dir_attr, str(agents_dir))

    captured = {}

    def cap_client(cfg):
        captured["client_cfg"] = cfg
        return Mock()

    monkeypatch.setattr(subagent, "_run_agent_loop", lambda *a, **k: ("done", {"result": "T4_DONE", "data": "ok"}, ""))
    monkeypatch.setattr(subagent, "get_subagent_mcp_tools_schema", lambda name: [])
    monkeypatch.setattr(subagent, "_resolve_subagent_has_vision", lambda name, cfg, user_cfg=None: False)
    monkeypatch.setattr(runner_mod, "create_client", cap_client)
    monkeypatch.setattr(runner_mod, "get_tools_schema", lambda include_main_only=False: [])
    config = dict(BASE_CONFIG)
    config["vision_llm"] = {"model": "vision-model", "apiBase": "http://vision/v1"}
    monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_config(tmp_path, config))

    subagent.call_subagent(agent_name=agent_name, task="test", llm_config=dict(MAIN_CFG))
    assert captured.get("client_cfg") is not None
    return captured["client_cfg"]


def test_dual_directory_user_agents_dir(monkeypatch, tmp_path):
    """用户目录（~/.niu/agents 场景，tmp 替代）：llmPreset 生效。"""
    cfg = _run_real_md(monkeypatch, tmp_path, "_USER_AGENTS_DIR", "t4-user-agent")
    assert cfg["model"] == "vision-model"
    assert cfg["apibase"] == "http://vision/v1"
    assert cfg.get("temperature") == 0.4


def test_dual_directory_project_agents_dir(monkeypatch, tmp_path):
    """项目目录（config/agents 场景，tmp 替代）：llmPreset 同生效。"""
    cfg = _run_real_md(monkeypatch, tmp_path, "_PROJECT_AGENTS_DIR", "t4-proj-agent")
    assert cfg["model"] == "vision-model"
    assert cfg["apibase"] == "http://vision/v1"
    assert cfg.get("temperature") == 0.4
