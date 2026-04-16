"""
ToolLifecycleManager 单元测试

验证三条规则：
1. 每轮衰减：所有分数 -10，低于 25 移除
2. 向量检索：仅在工具不在 active_tools 中时初始化分数
3. 工具被调用（hit_tool）：低于65补到65（同轮衰减后为55）
"""

import json
import tempfile
from pathlib import Path

import pytest

from agent.tool_lifecycle import ToolLifecycleManager


@pytest.fixture
def tl():
    """创建使用临时文件的 ToolLifecycleManager"""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
        path = Path(f.name)
    tl = ToolLifecycleManager(decay_rate=10, remove_threshold=25)
    tl.scores_path = path
    tl.active_tools = {}
    tl._save_scores()
    yield tl
    path.unlink(missing_ok=True)


class TestDecay:
    """规则1：每轮衰减 -10，低于 25 移除"""

    def test_basic_decay(self, tl):
        tl.active_tools = {"kg-server/create_entity": 65}
        tl.decay_tools()
        assert tl.active_tools == {"kg-server/create_entity": 55}

    def test_decay_multiple_rounds(self, tl):
        """65 → 55 → 45 → 35 → 25 → 移除"""
        tl.active_tools = {"kg-server/create_entity": 65}
        expected = [55, 45, 35, 25]
        for exp in expected:
            tl.decay_tools()
            assert tl.active_tools.get("kg-server/create_entity") == exp
        # 第5轮：25-10=15 < 25，移除
        tl.decay_tools()
        assert "kg-server/create_entity" not in tl.active_tools

    def test_decay_removes_below_threshold(self, tl):
        tl.active_tools = {"a": 35, "b": 30, "c": 25}
        tl.decay_tools()
        # a: 35-10=25, 25 >= 25 保留
        # b: 30-10=20, 20 < 25 移除
        # c: 25-10=15, 15 < 25 移除
        assert tl.active_tools == {"a": 25}

    def test_decay_empty(self, tl):
        tl.active_tools = {}
        tl.decay_tools()
        assert tl.active_tools == {}

    def test_decay_persists_to_file(self, tl):
        tl.active_tools = {"kg-server/create_entity": 65}
        tl.decay_tools()
        # 从文件重新加载
        loaded = json.loads(tl.scores_path.read_text(encoding="utf-8"))
        assert loaded == {"kg-server/create_entity": 55}


class TestHitTool:
    """规则3：工具被调用 → 低于65补到65 → 同轮衰减后为55"""

    def test_hit_tool_boosts_to_65(self, tl):
        tl.active_tools = {}
        tl.hit_tool("kg-server/create_entity", skip_coactivation=True)
        assert tl.active_tools["kg-server/create_entity"] == 65

    def test_hit_tool_does_not_override_high_score(self, tl):
        """已有高分不动"""
        tl.active_tools = {"kg-server/create_entity": 80}
        tl.hit_tool("kg-server/create_entity", skip_coactivation=True)
        assert tl.active_tools["kg-server/create_entity"] == 80

    def test_hit_tool_boosts_low_score(self, tl):
        """低于65补到65"""
        tl.active_tools = {"kg-server/create_entity": 30}
        tl.hit_tool("kg-server/create_entity", skip_coactivation=True)
        assert tl.active_tools["kg-server/create_entity"] == 65

    def test_hit_tool_then_decay_gives_55(self, tl):
        """hit_tool 设 65，衰减后 55"""
        tl.active_tools = {}
        tl.hit_tool("kg-server/create_entity", skip_coactivation=True)
        tl.decay_tools()
        assert tl.active_tools["kg-server/create_entity"] == 55

    def test_hit_tool_preserves_3_rounds(self, tl):
        """hit_tool 后：65→55→45→35→25→移除"""
        tl.active_tools = {}
        tl.hit_tool("kg-server/create_entity", skip_coactivation=True)
        expected = [55, 45, 35, 25]
        for exp in expected:
            tl.decay_tools()
            assert tl.active_tools.get("kg-server/create_entity") == exp
        # 下一轮：25-10=15 < 25，移除
        tl.decay_tools()
        assert "kg-server/create_entity" not in tl.active_tools


class TestUpdateFromSearch:
    """规则2：向量检索仅在工具不在 active_tools 中时初始化"""

    def test_new_tool_initialized(self, tl):
        """新发现的工具，检索分高于55才写入"""
        tl.update_from_search("kg-server/create_entity", 70)
        assert tl.active_tools["kg-server/create_entity"] == 70

    def test_new_tool_low_score_accepted(self, tl):
        """新发现的工具，检索分低于55也写入（取大值，会自然衰减移除）"""
        tl.update_from_search("kg-server/create_entity", 45)
        assert tl.active_tools["kg-server/create_entity"] == 45

    def test_existing_tool_not_overridden_by_lower(self, tl):
        """已有工具不被更低的向量检索覆盖"""
        tl.active_tools = {"kg-server/create_entity": 55}
        tl.update_from_search("kg-server/create_entity", 35)
        assert tl.active_tools["kg-server/create_entity"] == 55

    def test_existing_tool_overridden_by_higher(self, tl):
        """已有工具被更高的向量检索覆盖（取大值）"""
        tl.active_tools = {"kg-server/create_entity": 55}
        tl.update_from_search("kg-server/create_entity", 70)
        assert tl.active_tools["kg-server/create_entity"] == 70

    def test_decay_not_blocked_by_search(self, tl):
        """衰减不会被向量检索阻止"""
        tl.active_tools = {"kg-server/create_entity": 55}
        tl.decay_tools()  # 55→45
        assert tl.active_tools["kg-server/create_entity"] == 45
        tl.update_from_search("kg-server/create_entity", 35)  # 不覆盖
        assert tl.active_tools["kg-server/create_entity"] == 45
        tl.decay_tools()  # 45→35
        assert tl.active_tools["kg-server/create_entity"] == 35

    def test_search_then_decay_removes(self, tl):
        """向量检索初始化的工具也能正常衰减移除"""
        tl.update_from_search("kg-server/create_entity", 30)
        # 30→20, 移除
        tl.decay_tools()
        assert "kg-server/create_entity" not in tl.active_tools


class TestEndToEnd:
    """端到端场景"""

    def test_drag_file_then_idle(self, tl):
        """拖文件触发工具 → 空聊衰减 → 向量检索停止后最终移除"""
        # 1. 拖文件：hit_tool
        tl.hit_tool("photo-server/ingest_document", skip_coactivation=True)
        assert tl.active_tools["photo-server/ingest_document"] == 65

        # 2. 同轮 _on_turn_end：衰减
        tl.decay_tools()
        assert tl.active_tools["photo-server/ingest_document"] == 55

        # 3. 向量检索（工具已存在，高分不被低分覆盖）
        tl.update_from_search("photo-server/ingest_document", 35)
        assert tl.active_tools["photo-server/ingest_document"] == 55

        # 4. 空聊多轮（向量检索不再返回此工具）
        for expected in [45, 35, 25]:
            tl.decay_tools()
            assert tl.active_tools["photo-server/ingest_document"] == expected

        # 5. 再衰减一轮：移除
        tl.decay_tools()
        assert "photo-server/ingest_document" not in tl.active_tools

    def test_multiple_tools_different_scores(self, tl):
        """多个工具不同分数，衰减互不影响"""
        tl.active_tools = {
            "kg-server/create_entity": 65,
            "photo-server/ingest_photo": 45,
            "vector-store/search_documents": 30,
        }
        tl.decay_tools()
        assert tl.active_tools == {
            "kg-server/create_entity": 55,
            "photo-server/ingest_photo": 35,
            # vector-store: 30-10=20 < 25, 移除
        }

    def test_hit_tool_during_decay_cycle(self, tl):
        """衰减过程中工具被调用，保分"""
        tl.active_tools = {"kg-server/create_entity": 35}
        tl.decay_tools()  # 35→25
        assert tl.active_tools["kg-server/create_entity"] == 25
        # 工具被调用
        tl.hit_tool("kg-server/create_entity", skip_coactivation=True)  # 25<65 → 65
        assert tl.active_tools["kg-server/create_entity"] == 65
        tl.decay_tools()  # 65→55
        assert tl.active_tools["kg-server/create_entity"] == 55

    def test_search_keeps_relevant_tool_alive(self, tl):
        """向量检索持续返回的工具保持活跃（不会衰减移除）"""
        tl.hit_tool("photo-server/ingest_document", skip_coactivation=True)
        # 模拟5轮：每轮衰减+向量检索持续返回(分35)
        for _ in range(5):
            tl.decay_tools()
            tl.update_from_search("photo-server/ingest_document", 35)
        # 工具仍在，分数不低于35
        assert "photo-server/ingest_document" in tl.active_tools
        assert tl.active_tools["photo-server/ingest_document"] >= 35

    def test_search_stops_then_tool_decays(self, tl):
        """向量检索停止后，工具正常衰减移除"""
        tl.update_from_search("photo-server/ingest_document", 35)
        # 向量检索停止，纯衰减
        tl.decay_tools()  # 35→25
        tl.decay_tools()  # 25→15, 移除
        assert "photo-server/ingest_document" not in tl.active_tools


class TestCoActivation:
    """同 server 工具 co-activation 测试"""

    def test_coactivation_uses_nested_schema_name(self, tl):
        """验证 co-activation 从嵌套 schema 结构中正确取 name"""
        from unittest.mock import MagicMock, patch

        # 模拟 runner._mcp_tools_schema 的嵌套结构
        mock_runner = MagicMock()
        mock_runner._mcp_tools_schema = [
            {"type": "function", "function": {"name": "photo-server/ingest_photo"}},
            {"type": "function", "function": {"name": "photo-server/ingest_document"}},
            {"type": "function", "function": {"name": "photo-server/face_recognize"}},
            {"type": "function", "function": {"name": "kg-server/create_entity"}},
        ]

        mock_runner_module = MagicMock()
        mock_runner_module.get_runner.return_value = mock_runner

        mock_vs_module = MagicMock()
        mock_vs_module.get_vector_search.return_value = MagicMock()

        with patch.dict("sys.modules", {"agent.runner": mock_runner_module, "agent.vector_search": mock_vs_module}):
            tl.hit_tool("photo-server/ingest_photo", skip_coactivation=False)

        # 主工具设 65
        assert tl.active_tools["photo-server/ingest_photo"] == 65
        # 同 server 工具 co-activate 到 65
        assert tl.active_tools["photo-server/ingest_document"] == 65
        assert tl.active_tools["photo-server/face_recognize"] == 65
        # 不同 server 的工具不受影响
        assert "kg-server/create_entity" not in tl.active_tools
