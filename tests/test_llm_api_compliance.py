"""LLM API 合规 wire 级断言（T2，plan 2026-09-10-tool-message-order-fix §4）。

mock litellm.completion 捕获**实际发出的请求体**，断言：
- 构造级传递锁：NiuRunner(llm_config) 把 vision_enabled 交付到 client.backend
  （写入点必须早于 create_client——缺失则图永不展开，R6 P0）
- 派发级：call_subagent 经 llm_config 显式键透传 vision_enabled（真实判定函数）
- 续跑路径：suspended_client.backend.vision_enabled 与重算值同步（T1 R3）
- 发送级：vision_enabled=True → tool 图标记展开为紧随合成 user 图消息；
  缺失/False → fail-closed 不展开
- 消息 wire 形态：tool 键集={role,content,tool_call_id}（无 name）；
  tool/assistant content 恒 str；图在 user 段；ask_user 形态顺序合规；
  无 subagent_msg role

全 mock，禁真实 LLM。
"""
import json
from unittest.mock import Mock, patch

MAIN_CFG = {"apikey": "sk-main", "apibase": "http://main/v1", "model": "main-model"}

BASE_CONFIG = {
    "llm": {"model": "main-model", "apiKey": "sk-main", "apiBase": "http://main/v1", "type": "openai"},
}


def _write_config(tmp_path, data):
    p = tmp_path / "user-config.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return str(p)


def _png_bytes():
    """最小 1x1 PNG（魔数有效，_detect_image_mime 认 image/png）。"""
    import struct
    import zlib

    def _chunk(tag, payload):
        c = struct.pack(">I", len(payload)) + tag + payload
        return c + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw = zlib.compress(b"\x00\xff\x00\x00")
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", raw) + _chunk(b"IEND", b""))


def _make_png(path):
    path.write_bytes(_png_bytes())
    return str(path)


# ---------------------------------------------------------------------------
# ① 构造级传递锁（R6 P0：写入点必须早于 create_client）
# ---------------------------------------------------------------------------

def _build_runner(llm_config):
    """全 mock 构造 NiuRunner（镜像 test_sticky_session_routing 范式）。"""
    from agent.tool_registry import reset_registry

    try:
        with patch("agent.runner.get_system_prompt", return_value="sys"), \
             patch("agent.runner.get_tools_schema", return_value=[]), \
             patch("agent.runner.get_skill_sync"), \
             patch("agent.runner.NiuHandler"), \
             patch("niu_api.internal.disk_engine.DiskEngine") as mock_disk_cls:
            mock_disk_instance = Mock()
            mock_disk_instance.get_schema.return_value = {
                "type": "function", "function": {"name": "disk"},
            }
            mock_disk_instance.config.servers = {}
            mock_disk_cls.return_value = mock_disk_instance

            from agent.runner import NiuRunner
            return NiuRunner(llm_config=llm_config, mcp_client=None)
    finally:
        reset_registry()


def test_runner_init_delivers_vision_enabled_true():
    """capabilities 匹配（model 绑定 + input 含 image）→ client.backend.vision_enabled=True。"""
    runner = _build_runner({
        **MAIN_CFG,
        "capabilities": {"model": "main-model", "input": ["text", "image"]},
    })
    assert runner.client.backend.vision_enabled is True


def test_runner_init_vision_enabled_false_without_caps():
    """无 capabilities 键 → fail-closed False（图永不展开）。"""
    runner = _build_runner(dict(MAIN_CFG))
    assert runner.client.backend.vision_enabled is False


def test_runner_init_vision_enabled_false_model_mismatch():
    """capabilities.model ≠ 当前 model（换模型后旧能力）→ False（防误判）。"""
    runner = _build_runner({
        **MAIN_CFG,
        "capabilities": {"model": "other-model", "input": ["text", "image"]},
    })
    assert runner.client.backend.vision_enabled is False


# ---------------------------------------------------------------------------
# ② 派发级：call_subagent 经 llm_config 显式键透传 vision_enabled
# ---------------------------------------------------------------------------

def _run_subagent(monkeypatch, tmp_path, call_kwargs, agent_cfg=None, base_config=None):
    """全 mock 调 call_subagent，返回 create_client 收到的 llm_config。

    agent_cfg：get_subagent_config 返回值（默认 {} = 无 frontmatter 字段）；
    base_config：写入 CONFIG_PATH 的 user-config.json（默认 BASE_CONFIG，llmPreset 用例传含 vision_llm 段的配置）。
    """
    import agent.runner as runner_mod
    from agent import subagent

    captured = {}

    def cap_client(cfg):
        captured["client_cfg"] = cfg
        return Mock()

    monkeypatch.setattr(subagent, "_run_agent_loop", lambda *a, **k: ("done", {"result": "ok"}, ""))
    monkeypatch.setattr(subagent, "get_subagent_mcp_tools_schema", lambda name: [])
    monkeypatch.setattr(runner_mod, "create_client", cap_client)
    monkeypatch.setattr(runner_mod, "get_tools_schema", lambda include_main_only=False: [])
    monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_config(tmp_path, base_config or BASE_CONFIG))
    monkeypatch.setattr(subagent, "get_subagent_config", lambda name: agent_cfg if agent_cfg is not None else {})

    subagent.call_subagent(**call_kwargs)
    assert captured.get("client_cfg") is not None, "create_client 未被调用"
    return captured["client_cfg"]


def test_subagent_dispatch_delivers_vision_enabled(monkeypatch, tmp_path):
    """主 llm_config capabilities 匹配 → create_client 收到的 cfg.vision_enabled=True（真实判定）。"""
    cfg = _run_subagent(monkeypatch, tmp_path, {
        "agent_name": "screenshot-analyst",
        "task": "test",
        "llm_config": {
            **MAIN_CFG,
            "capabilities": {"model": "main-model", "input": ["text", "image"]},
        },
    })
    assert cfg.get("vision_enabled") is True


def test_subagent_dispatch_vision_enabled_false_without_caps(monkeypatch, tmp_path):
    """无 capabilities → 显式 False（fail-closed，键存在供会话构造读取）。"""
    cfg = _run_subagent(monkeypatch, tmp_path, {
        "agent_name": "screenshot-analyst",
        "task": "test",
        "llm_config": dict(MAIN_CFG),
    })
    assert cfg.get("vision_enabled") is False


def test_subagent_dispatch_llmpreset_vision_enabled(monkeypatch, tmp_path):
    """判据来源②（plan §4）：frontmatter llmPreset=vision_llm 且段 model 非空 →
    vision_enabled=True——主配置无 capabilities，True 只能来自 llmPreset 段判定（真实判定函数）。"""
    config = dict(BASE_CONFIG)
    config["vision_llm"] = {"model": "vision-model"}  # apiKey/apiBase 继承 llm 段（get_llm_config 语义）
    cfg = _run_subagent(monkeypatch, tmp_path, {
        "agent_name": "screenshot-analyst",
        "task": "test",
        "llm_config": dict(MAIN_CFG),  # 主配置无 capabilities——排除判据来源①
    }, agent_cfg={"llmPreset": "vision_llm"}, base_config=config)
    assert cfg.get("vision_enabled") is True


# ---------------------------------------------------------------------------
# ③ 续跑路径：suspended_client.backend.vision_enabled 与重算值同步
# ---------------------------------------------------------------------------

def test_subagent_resume_syncs_suspended_vision(monkeypatch, tmp_path):
    """挂起档 backend.vision_enabled=旧值 False；续答时主配置 capabilities 匹配 → 覆盖为 True。"""
    import agent.runner as runner_mod
    from agent import subagent
    from agent.subagent_registry import SubagentRegistry

    class _FakeBackend:
        vision_enabled = False  # 派发时刻旧值（当时模型无视觉）

    suspended_client = Mock()
    suspended_client.backend = _FakeBackend()

    monkeypatch.setattr(subagent, "_run_agent_loop", lambda *a, **k: ("done", {"result": "ok"}, ""))
    monkeypatch.setattr(subagent, "get_subagent_mcp_tools_schema", lambda name: [])
    monkeypatch.setattr(runner_mod, "create_client", lambda cfg: Mock())
    monkeypatch.setattr(runner_mod, "get_tools_schema", lambda include_main_only=False: [])
    monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_config(tmp_path, BASE_CONFIG))
    monkeypatch.setattr(subagent, "get_subagent_config", lambda name: {})
    monkeypatch.setattr(subagent, "_maybe_push_subagent_instruction", lambda *a, **k: None)
    monkeypatch.setattr(subagent, "_maybe_suspend_session", lambda *a, **k: None)

    unique_name = SubagentRegistry.register(
        "screenshot-analyst", supplement_queue=Mock(), force_unique_name="sa-resume-test")
    try:
        inst = SubagentRegistry.get(unique_name)
        assert inst is not None  # register 后必存在（类型收窄）
        inst.state = "waiting_for_answer"
        inst.suspended_messages = [{"role": "user", "content": "task"}]
        inst.suspended_handler = Mock()
        inst.suspended_client = suspended_client
        inst.suspended_tools_schema = []
        inst.suspended_system_message = {"role": "system", "content": "sys"}

        subagent.call_subagent(
            agent_name="screenshot-analyst",
            task="",
            answer="[回答] 继续",
            answer_unique_name=unique_name,
            llm_config={
                **MAIN_CFG,
                "capabilities": {"model": "main-model", "input": ["text", "image"]},
            },
        )
        assert suspended_client.backend.vision_enabled is True, \
            "续跑必须同步重算的 has_vision 到 suspended_client（防换模型沿用旧值）"
    finally:
        SubagentRegistry.unregister(unique_name)


# ---------------------------------------------------------------------------
# ④ 发送级：chat() wire 体图片展开 / fail-closed
# ---------------------------------------------------------------------------

def _wire_messages(cfg, messages):
    """调用 LiteLLMSession.chat（mock litellm.completion 抛异常），返回 wire messages。"""
    from agent.generic.litellm_adapter import LiteLLMSession

    session = LiteLLMSession(cfg=cfg)
    with patch("agent.generic.litellm_adapter.litellm.completion") as mock_completion:
        mock_completion.side_effect = Exception("stop-test")
        try:
            next(session.chat(messages=messages))
        except Exception:
            pass
        return mock_completion.call_args[1]["messages"]


def _base_cfg(**overrides):
    cfg = {
        "api_type": "openai",
        "apikey": "test-key",
        "apibase": "https://api.openai.com/v1",
        "model": "gpt-4o",
    }
    cfg.update(overrides)
    return cfg


def test_chat_wire_expands_tool_image_when_vision_enabled(tmp_path):
    """vision_enabled=True：tool 图标记 → tool(str) + 紧随合成 user(image_url data URI)。"""
    p = _make_png(tmp_path / "shot.png")
    msgs = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "screenshot", "arguments": "{}"}},
        ]},
        {"role": "tool", "content": f"截图完成 ![截图]({p})", "tool_call_id": "call_1"},
    ]
    out = _wire_messages(_base_cfg(vision_enabled=True), msgs)
    assert [m["role"] for m in out] == ["assistant", "tool", "user"]
    # tool content 恒 str（标记保留，UI/DB 不受影响）
    assert isinstance(out[1]["content"], str)
    assert f"![截图]({p})" in out[1]["content"]
    # 合成 user：text 说明段 + image_url data URI 段（图只在 user 段——OpenAI 规范位）
    synth = out[2]["content"]
    assert isinstance(synth, list)
    assert synth[0]["type"] == "text"
    blocks = [s for s in synth if s.get("type") == "image_url"]
    assert len(blocks) == 1
    assert blocks[0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_chat_wire_no_expand_without_vision(tmp_path):
    """vision_enabled 缺失（fail-closed）：文件存在也不展开、无合成 user、无图段。"""
    p = _make_png(tmp_path / "shot.png")
    msgs = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "screenshot", "arguments": "{}"}},
        ]},
        {"role": "tool", "content": f"截图完成 ![截图]({p})", "tool_call_id": "call_1"},
    ]
    out = _wire_messages(_base_cfg(), msgs)  # 无 vision_enabled 键
    assert [m["role"] for m in out] == ["assistant", "tool"]
    assert isinstance(out[1]["content"], str)
    flat = json.dumps(out, ensure_ascii=False)
    assert "data:image/" not in flat, "fail-closed：不得产出任何 image data URI"


def test_chat_wire_tool_list_content_downgraded_to_str():
    """存量 list content（tool）→ wire 恒 str（image_url 段 → [图片已省略]）。"""
    msgs = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "t", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": [
            {"type": "text", "text": "ok"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA="}},
        ]},
    ]
    out = _wire_messages(_base_cfg(vision_enabled=True), msgs)
    tool_msg = next(m for m in out if m["role"] == "tool")
    assert isinstance(tool_msg["content"], str)
    assert "ok" in tool_msg["content"]


# ---------------------------------------------------------------------------
# ⑤ 消息 wire 形态：ask_user 顺序 / subagent_msg / 键集 / content 类型
# ---------------------------------------------------------------------------

def test_chat_wire_ask_user_shape_compliance():
    """DB ask_user 形态 assistant(tc)→user(回答)→tool(结果) + subagent_msg →
    wire：assistant→tool 紧邻（k3-256k 400 根因形态）、subagent_msg 丢弃、
    tool 键集无 name、content 恒 str。"""
    msgs = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "ask_user", "arguments": "{}"}},
        ]},
        {"role": "user", "content": "[主 Agent 回答] 继续"},
        {"role": "tool", "content": "[用户回答] 继续", "tool_call_id": "call_1"},
        {"role": "subagent_msg", "content": "@子名 消息（仅前端展示）"},
    ]
    out = _wire_messages(_base_cfg(), msgs)
    # 顺序合规：assistant(tool_calls) 后紧跟其 tool 响应；user 回答顺延到 tool 块之后
    assert [m["role"] for m in out] == ["assistant", "tool", "user"], (
        f"ask_user 形态必须规整为 assistant→tool→user，got: {[m['role'] for m in out]}"
    )
    # subagent_msg 不发出（@ 消息仅供前端展示）
    assert all(m["role"] != "subagent_msg" for m in out)
    # tool 键集 = {role, content, tool_call_id}（无 name——OpenAI 规范）
    tool_msg = next(m for m in out if m["role"] == "tool")
    assert set(tool_msg.keys()) == {"role", "content", "tool_call_id"}
    # tool/assistant content 恒 str；图只允许出现在 user 段
    for m in out:
        if m["role"] in ("tool", "assistant"):
            assert isinstance(m["content"], str), f"{m['role']} content 必须为 str"


def test_chat_wire_assistant_list_content_downgraded():
    """存量 assistant list content → wire str（OpenAI 规范 assistant 仅 text/refusal 段，
    且 Niu 出站统一 str——UI/DB 不受影响）。"""
    msgs = [
        {"role": "assistant", "content": [
            {"type": "text", "text": "已完成"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA="}},
        ]},
        {"role": "user", "content": "ok"},
    ]
    out = _wire_messages(_base_cfg(vision_enabled=True), msgs)
    assert isinstance(out[0]["content"], str)
    assert "已完成" in out[0]["content"]


# ---------------------------------------------------------------------------
# ⑥ LightRAG 关键词 schema strict 合规
# ---------------------------------------------------------------------------

def test_lightrag_keyword_schema_additional_properties_false():
    """OpenAI strict 模式硬要求：object schema 声明 additionalProperties:false。"""
    from niu_api.internal.lightrag_manager import _build_keyword_extraction_response_format

    rf = _build_keyword_extraction_response_format()
    assert rf["type"] == "json_schema"
    schema = rf["json_schema"]["schema"]
    assert schema.get("additionalProperties") is False, \
        "strict 模式缺 additionalProperties:false → OpenAI 路由必 400 后降级重发（每次白跑）"
