"""T2b-3：子 Agent 折叠禁门（装配层无条件排除 + 派发层入口拦截，双保险）。

用户定案：**视野集只属于主 Agent 视图**——子 Agent 拿不到 [输出#N …] 头行编号，
调 fold_tool_output 只会猜 rowid、写坏 DB。因此两层都堵：

1. 装配层（agent/subagent.py）：`get_subagent_mcp_tools_schema` 与
   `_build_subagent_tools_schema` 都**无条件**把折叠工具剔出子 Agent 工具表
   （caller 显式传入含该工具的 mcp_tools_schema 也照样剔）。
2. 派发层（agent/handler.py `_dispatch_impl`）：入口最早处按**实例标志**
   `_is_subagent` 拦截，裸名 `fold_tool_output` 与 `session-manager/fold_tool_output`
   两种形态都不调任何工具，直接返回错误结果（防绕过装配层 / 表被别处改写）。

覆盖：
- 装配层：caller 传入含折叠工具的 mcp_tools_schema → 返回表内无它
- 装配层：配置含 session-manager（mock 工具枚举，不初始化真 MCP）→ 返回表内无它
- 派发层：子 Agent + 两种工具名 → 拦截错误 + 底层工具零调用
- 反向锁：主 Agent（_is_subagent=False）同工具名不被拦截（底层工具确实被调用）

无 LLM 调用；不触碰真实 ~/.niu/messages.db。
"""

from unittest.mock import Mock

from agent.generic.agent_loop import StepOutcome, StreamEvent
from agent.handler import NiuHandler

# 派发层拦截文案（与实现逐字对齐）
FOLD_BLOCKED_MSG = "子 Agent 不可折叠输出：该能力仅主 Agent 可用"


# ============== 工具 schema 构造 ==============


def _fn_schema(name: str) -> dict:
    """OpenAI 形态工具 schema（子 Agent 工具表内的形态）。"""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"{name} desc",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _names(schema: list) -> list:
    return [t.get("function", {}).get("name", "") for t in schema]


# ============== ① 装配层：_build_subagent_tools_schema ==============


def test_build_schema_drops_fold_tool_from_caller_supplied_mcp_schema(monkeypatch):
    """caller 显式传入含折叠工具的 mcp_tools_schema（裸名 + server/ 前缀名）
    → 最终返回表内两种形态都无它，其余工具不动。

    锁的是"最终 return 前无条件过滤"——只过滤 `mcp_tools_schema is None` 分支
    的实现对本用例完全无效（必须真红）。
    """
    import agent.runner as runner_mod
    from agent import subagent

    monkeypatch.setattr(
        runner_mod, "get_tools_schema",
        lambda include_main_only=False: [_fn_schema("read")],
    )
    mcp_in = [
        _fn_schema("fold_tool_output"),
        _fn_schema("session-manager/fold_tool_output"),
        _fn_schema("get_messages"),
    ]
    out = subagent._build_subagent_tools_schema(
        "test-agent",
        agent_config={"allowBaseTools": ["read"], "mcpServers": ["session-manager"]},
        mcp_tools_schema=mcp_in,
    )
    assert "fold_tool_output" not in _names(out)
    assert "session-manager/fold_tool_output" not in _names(out)
    # 其余工具保留（只删折叠工具，不误伤）
    assert "read" in _names(out) and "get_messages" in _names(out)


def test_build_schema_drops_fold_tool_even_when_base_table_carries_it(monkeypatch):
    """基础工具表里混入折叠工具（server 前缀形态）→ 同样被剔。

    白名单制下 fold_tool_output 不在 _BASE_TOOL_NAMES，本就已进不来；
    此锁守的是"最后一层出口过滤"对任意来源一致生效。
    """
    import agent.runner as runner_mod
    from agent import subagent

    monkeypatch.setattr(
        runner_mod, "get_tools_schema",
        lambda include_main_only=False: [
            _fn_schema("read"),
            _fn_schema("session-manager/fold_tool_output"),
        ],
    )
    out = subagent._build_subagent_tools_schema(
        "test-agent",
        agent_config={"allowBaseTools": ["read"], "mcpServers": ["session-manager"]},
        mcp_tools_schema=[],
    )
    assert "session-manager/fold_tool_output" not in _names(out)


# ============== ② 装配层：get_subagent_mcp_tools_schema ==============


def test_get_subagent_mcp_tools_schema_excludes_fold_tool(monkeypatch):
    """配置含 session-manager → 返回表内无 fold_tool_output（mock 工具枚举，不初始化真 MCP）。"""
    from agent import subagent

    monkeypatch.setattr(
        subagent, "get_subagent_config",
        lambda name: {"mcpServers": ["session-manager"]},
    )
    registry = Mock()
    registry.get_schemas.return_value = [
        {
            "name": "session-manager/fold_tool_output",
            "description": "折叠可再生的工具输出",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "session-manager/get_messages",
            "description": "读消息",
            "input_schema": {"type": "object", "properties": {}},
        },
    ]
    monkeypatch.setattr("agent.tool_registry.get_registry", lambda: registry)

    schema = subagent.get_subagent_mcp_tools_schema("test-agent")
    assert "fold_tool_output" not in _names(schema)
    assert _names(schema) == ["get_messages"]  # 同 server 的其它工具不受影响


# ============== ③ 派发层拦截 ==============


class _FakeRegistry:
    """最小 ToolRegistry 桩：fold_tool_output 已注册，被调即炸（证明未被拦截）。"""

    def __init__(self):
        self._server_tools = {"session-manager": ["fold_tool_output"]}
        self._schemas = {"session-manager/fold_tool_output": {}}
        self.calls = []

    def get(self, tool_name):
        def _fold(**kwargs):
            self.calls.append((tool_name, kwargs))
            raise AssertionError(f"底层折叠工具被调用了（应被派发层拦截）: {tool_name} {kwargs}")

        return _fold if tool_name.endswith("fold_tool_output") else None

    def get_schemas(self):
        return []


def _make_handler() -> NiuHandler:
    return NiuHandler(cwd="/tmp", mcp_client=Mock(), disk_engine=None)


def _consume(gen):
    """消费 dispatch 生成器，返回 (事件列表, StepOutcome)。"""
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration as e:
        return events, e.value
    return events, None


def _install_registry(monkeypatch, registry):
    monkeypatch.setattr("agent.tool_registry.get_registry", lambda: registry)


def test_subagent_dispatch_blocks_bare_fold_tool_name(monkeypatch):
    """子 Agent 调裸名 fold_tool_output → 拦截错误，底层工具零调用。"""
    registry = _FakeRegistry()
    _install_registry(monkeypatch, registry)
    handler = _make_handler()
    handler._is_subagent = True

    events, outcome = _consume(handler.dispatch("fold_tool_output", {"output_ids": [1]}, None))

    assert isinstance(outcome, StepOutcome)
    data = outcome.data
    assert data.get("status") == "error"
    assert FOLD_BLOCKED_MSG in str(data.get("msg"))
    assert registry.calls == [], f"底层工具零调用，实际调用: {registry.calls}"
    # 不经工具表/回调路径：只允许（可选的）system 事件，不得有 tool_marker（真执行过）
    assert not [e for e in events if isinstance(e, StreamEvent) and e.type == "tool_marker"]


def test_subagent_dispatch_blocks_namespaced_fold_tool_name(monkeypatch):
    """子 Agent 调 session-manager/fold_tool_output → 同拦截，底层工具零调用。"""
    registry = _FakeRegistry()
    _install_registry(monkeypatch, registry)
    handler = _make_handler()
    handler._is_subagent = True

    events, outcome = _consume(
        handler.dispatch("session-manager/fold_tool_output", {"output_ids": [1]}, None)
    )

    data = outcome.data
    assert data.get("status") == "error"
    assert FOLD_BLOCKED_MSG in str(data.get("msg"))
    assert registry.calls == [], f"底层工具零调用，实际调用: {registry.calls}"
    assert not [e for e in events if isinstance(e, StreamEvent) and e.type == "tool_marker"]


# ============== ④ 反向锁：主 Agent 不被拦截 ==============


def test_main_agent_dispatch_not_blocked(monkeypatch):
    """_is_subagent=False 时同一工具名不被拦截——底层工具确实被调用。

    断言落到"底层工具是否真被调用"（不是只看返回值）：桩工具被调即抛
    AssertionError，经 dispatch 的 MCP 分支内层 except 变成 error msg，
    即可证明走到了工具调用点且没被入口拦下。
    """
    registry = _FakeRegistry()
    _install_registry(monkeypatch, registry)
    handler = _make_handler()
    handler._is_subagent = False

    _events, outcome = _consume(
        handler.dispatch("session-manager/fold_tool_output", {"output_ids": [1]}, None)
    )

    assert registry.calls == [("session-manager/fold_tool_output", {"output_ids": [1]})], (
        f"主 Agent 必须真的调到底层工具（未拦截），实际: {registry.calls}"
    )
    assert isinstance(outcome, StepOutcome)
    # 桩工具被真调用 → 其 AssertionError 文案原样进 MCP 分支错误返回
    # （证明派发层没在入口拦下，主 Agent 走通到工具调用点）
    assert "底层折叠工具被调用了" in str(outcome.data), outcome.data
    assert FOLD_BLOCKED_MSG not in str(outcome.data)
