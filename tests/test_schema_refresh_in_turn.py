"""Schema 刷新在工具循环内生效测试。"""
import threading
import pytest


@pytest.fixture
def fresh_agents_dir(tmp_path, monkeypatch):
    """临时 ~/.niu/agents/ 目录，含一个初始 agent。

    注意：get_tools_schema/_refresh_base_tools_schema_if_dirty 内部都是
    `from .subagent import _USER_AGENTS_DIR`——真实属性在 agent.subagent，
    **必须 patch agent.subagent._USER_AGENTS_DIR**（patch agent.runner 同名属性无效，AttributeError）。
    """
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "browser-operator.md").write_text(
        "---\ndescription: 浏览器操作\n---\n# browser-operator\n", encoding="utf-8")
    monkeypatch.setattr("agent.subagent._USER_AGENTS_DIR", str(agents))
    return agents


def _make_runner(monkeypatch):
    """构造最小 runner（不执行完整 __init__）。"""
    from agent.runner import NiuRunner
    r = NiuRunner.__new__(NiuRunner)
    # _on_turn_end 无条件调 _maybe_trigger_nap（runner.py L1119）——__new__ 实例无 _nap_running，
    # 必须设置并 set() 短路（避免读真实 cursor/db）
    r._nap_running = threading.Event()
    r._nap_running.set()
    # 改造后 _on_turn_end 返回 self._assemble_tools_schema()——内部访问 self.disk_engine.get_schema()
    # （__new__ 实例无 disk_engine，必须 stub，否则 AttributeError）
    r.disk_engine = type("_FakeDisk", (), {
        "get_schema": staticmethod(lambda: {"type": "function", "function": {"name": "disk", "description": "", "parameters": {"type": "object", "properties": {}}}})
    })()
    return r


def test_on_turn_end_refreshes_schema(fresh_agents_dir, monkeypatch):
    """工具循环内新建 MD → _on_turn_end 刷新 base_tools_schema + 返回含新工具的 schema。"""
    from agent.runner import NiuRunner
    r = _make_runner(monkeypatch)
    r._known_user_subagents = {"browser-operator.md"}
    # 新建 nutritionist.md（模拟工具循环内 write）
    (fresh_agents_dir / "nutritionist.md").write_text(
        "---\ndescription: 家庭营养师\n---\n# nutritionist\n", encoding="utf-8")
    # 调用 _on_turn_end（内部 _refresh + _assemble；返回新 schema）
    new_schema = r._on_turn_end([], [], 1)
    assert r._known_user_subagents == {"browser-operator.md", "nutritionist.md"}
    names = [t["function"]["name"] for t in r.base_tools_schema]
    assert "chat-with-nutritionist" in names
    # 返回值 = 组装后 schema（含 base 全部 + disk；断言含新工具而非与 base 相等——
    # _assemble_tools_schema 追加 static MCP + disk，base_tools_schema 本身不含 disk）
    new_names = [t["function"]["name"] for t in new_schema]
    assert "chat-with-nutritionist" in new_names
    assert "disk" in new_names


def test_on_turn_end_no_change_no_rebuild(fresh_agents_dir, monkeypatch):
    """无变化时不重算（对象引用稳定）。"""
    from agent.runner import NiuRunner
    r = _make_runner(monkeypatch)
    r._known_user_subagents = {"browser-operator.md"}
    old_schema = [{"type": "function", "function": {"name": "keep"}}]
    r.base_tools_schema = old_schema
    # 无变化：_refresh 短路不重算 → base_tools_schema 引用不变
    r._on_turn_end([], [], 1)
    assert r.base_tools_schema is old_schema
