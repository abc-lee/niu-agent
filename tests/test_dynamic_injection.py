"""Tests for dynamic injection — per-type retrieval with filter_lambda."""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch
from niu_api.internal.lightrag_adapter import LightRAGAdapter


class TestSearchByFilePath:
    """Test search_by_file_path — pre-filter by file_path then top-k."""

    @patch.object(LightRAGAdapter, 'query_data')
    def test_filter_lambda_passed_to_query_data(self, mock_query):
        """filter_lambda must be passed to query_data for pre-filtering."""
        adapter = LightRAGAdapter.__new__(LightRAGAdapter)
        mock_query.return_value = {"data": {"entities": [], "relationships": [], "chunks": []}}

        adapter.search_by_file_path("test query", file_path_contains="skill_sync", top_k=10)

        call_kwargs = mock_query.call_args.kwargs
        assert "filter_lambda" in call_kwargs
        filter_fn = call_kwargs["filter_lambda"]
        assert callable(filter_fn)
        # Verify the filter function checks file_path
        assert filter_fn({"file_path": "skill_sync"}) is True
        assert filter_fn({"file_path": "some_doc.md"}) is False
        assert filter_fn({"file_path": None}) is False

    @patch.object(LightRAGAdapter, 'query_data')
    def test_returns_list_of_entities(self, mock_query):
        """Should return list of entity dicts, not categorized dict."""
        adapter = LightRAGAdapter.__new__(LightRAGAdapter)
        mock_query.return_value = {
            "data": {
                "entities": [
                    {"entity_name": "report-skill", "entity_type": "Skill", "file_path": "skill_sync"},
                    {"entity_name": "note-management", "entity_type": "Skill", "file_path": "skill_sync"},
                ],
                "relationships": [],
                "chunks": [],
            }
        }

        result = adapter.search_by_file_path("日志", file_path_contains="skill_sync", top_k=10)

        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["entity_name"] == "report-skill"


class TestDecayPoolCategoryCorrection:
    """Test that inject() updates category when entity is re-injected with correct category."""

    def test_category_updated_when_lower_score(self):
        """Even with lower score, category must be updated if different."""
        from agent.decay_pool import DecayPool

        pool = DecayPool()
        # First inject as "knowledge" (wrong category)
        pool.inject(
            entity_name="report-skill",
            entity_dict={"entity_name": "report-skill", "entity_type": "Skill", "description": "test"},
            category="knowledge",
            source="vector",
            vector_score=0.8,
        )
        # Re-inject with correct category "skill" but lower score
        pool.inject(
            entity_name="report-skill",
            entity_dict={"entity_name": "report-skill", "entity_type": "Skill", "description": "test"},
            category="skill",
            source="vector",
            vector_score=0.5,
        )
        # Category should be "skill" now
        entry = pool._entries["report-skill"]
        assert entry.category == "skill"


class TestInjectDynamicResourcesSkillRetrieval:
    """Test that _inject_dynamic_resources retrieves skills independently."""

    def test_skill_retrieval_uses_search_by_file_path(self):
        """Skill retrieval must use search_by_file_path, not search_multi_lightrag."""
        from agent.runner import NiuRunner

        runner = NiuRunner.__new__(NiuRunner)
        runner._decay_pool = MagicMock()
        runner._decay_pool.decay = MagicMock()
        runner._decay_pool.inject = MagicMock()
        runner._decay_pool.get_top_by_category = MagicMock(return_value=[])
        runner._brain_adapter = MagicMock()
        runner._brain_adapter.activate_for_query = MagicMock()
        runner._brain_adapter.format_region_map_only = MagicMock(return_value="")
        runner._format_running_subagents_section = MagicMock(return_value="")
        runner._get_brain_injector = MagicMock(return_value=None)
        runner._format_lightrag_entities_for_prompt = MagicMock(return_value=("", set()))
        runner._INJECT_ENTITY_TYPE_BLACKLIST = set()
        runner._INJECT_ENTITY_NAME_BLACKLIST = set()

        call_log = []

        def mock_search_multi(query, mode="local", top_k=20, keywords=None, timeout=None):
            call_log.append(("search_multi_lightrag", query, top_k))
            return {"skill": [], "knowledge": [], "other": []}

        def mock_search_by_fp(query, file_path_contains, top_k=10, keywords=None, timeout=None):
            call_log.append(("search_by_file_path", query, file_path_contains, top_k))
            return [{"entity_name": "report-skill", "entity_type": "Skill", "file_path": "skill_sync", "description": "test", "distance": 0.55}]

        runner._brain_adapter.search_multi_lightrag = mock_search_multi
        runner._brain_adapter.search_by_file_path = mock_search_by_fp

        runner._inject_dynamic_resources("test context")

        # Verify search_by_file_path was called for skills
        skill_calls = [c for c in call_log if c[0] == "search_by_file_path"]
        assert len(skill_calls) == 1
        assert "skill_sync" in skill_calls[0][2]

        # Verify search_multi_lightrag was NOT called with the old all-in-one approach
        # (it should only be used for knowledge, not skills)
        multi_calls = [c for c in call_log if c[0] == "search_multi_lightrag"]
        # knowledge still uses search_multi_lightrag
        assert len(multi_calls) >= 1


# ============== E3 D4：注入失败标注（injection_notes 累加器） ==============

_ANNOTATIONS = (
    "[脑区激活失败，本轮无脑区注入]",
    "[技能检索失败，本轮无技能注入]",
    "[知识检索失败，本轮无参考知识注入]",
    "[脑区状态图生成失败]",
    "[脑区知识格式化失败]",
    "[脑区上下文不可用]",
)


def _assert_siblings_absent(injection, own_marker):
    """兄弟标注隔离：自身标注之外其余 5 种标注必须缺席（防兄弟路径误标回归，P3-2）。"""
    for marker in _ANNOTATIONS:
        if marker != own_marker:
            assert marker not in injection


def _make_runner():
    """最小 NiuRunner 装配（NiuRunner.__new__ 绕过 __init__——E3-07 getattr 守卫验证面）。"""
    from agent.runner import NiuRunner

    runner = NiuRunner.__new__(NiuRunner)
    runner._decay_pool = MagicMock()
    runner._decay_pool.decay = MagicMock()
    runner._decay_pool.inject = MagicMock()
    runner._decay_pool.get_top_by_category = MagicMock(return_value=[])
    runner._brain_adapter = MagicMock()
    runner._format_running_subagents_section = MagicMock(return_value="")
    runner._format_lightrag_entities_for_prompt = MagicMock(return_value=("", set()))
    runner._INJECT_ENTITY_TYPE_BLACKLIST = set()
    runner._INJECT_ENTITY_NAME_BLACKLIST = set()
    return runner


def _make_real_injector_runner():
    """真实 _get_brain_injector 生命周期测试装配（E3-07 标记置位/清除经真实 re-check 路径）。"""
    from agent.runner import NiuRunner

    runner = NiuRunner.__new__(NiuRunner)
    runner._decay_pool = MagicMock()
    runner._decay_pool.decay = MagicMock()
    runner._decay_pool.inject = MagicMock()
    runner._decay_pool.get_top_by_category = MagicMock(return_value=[])
    runner._format_running_subagents_section = MagicMock(return_value="")
    runner._format_lightrag_entities_for_prompt = MagicMock(return_value=("", set()))
    runner._INJECT_ENTITY_TYPE_BLACKLIST = set()
    runner._INJECT_ENTITY_NAME_BLACKLIST = set()
    runner._brain_adapter = None
    runner._brain_ingester = None
    runner._brain_region_mgr = None
    runner._brain_injector = None
    runner._cached_activation_mgr = None
    runner._last_forced_sync_fail_time = 0.0
    runner._forced_sync_running = MagicMock()
    return runner


@contextmanager
def _brain_patches():
    """真实 _get_brain_injector 依赖链 patch（E3-07 标记生命周期测试专用）。

    B6 P3：须同时 mock _get_rag() → None + get_activation_mgr() → None——
    re-check 仅 rag None 可达；activation None + rag 正常被 forced-sync 分支截获——标记永不置位。
    """
    with patch("niu_api.internal.lightrag_adapter.LightRAGAdapter") as adapter_cls, \
         patch("niu_api.internal.lightrag_adapter.LightRAGIngester"), \
         patch("agent.brain_tools.get_activation_mgr") as get_mgr, \
         patch("niu_api.internal.region_manager.RegionManager"), \
         patch("niu_api.internal.region_injector.BrainContextInjector") as injector_cls:
        injector = MagicMock()
        injector.activate_for_query.return_value = ({}, None, None)
        injector.format_region_map_only.return_value = ""
        injector.format_region_knowledge.return_value = []
        injector_cls.return_value = injector
        yield adapter_cls, get_mgr, injector_cls


class TestInjectionNotes:
    """E3 D4：5 处 except 各追加固定标注文本，组装前并入 parts（LLM 可感知检索失败）。"""

    def test_brain_activation_failure_adds_annotation(self):
        runner = _make_runner()
        runner._get_brain_injector = MagicMock(side_effect=RuntimeError("brain down"))
        injection, _ = runner._inject_dynamic_resources("test")
        assert "[脑区激活失败，本轮无脑区注入]" in injection
        _assert_siblings_absent(injection, "[脑区激活失败，本轮无脑区注入]")

    def test_skill_retrieval_failure_adds_annotation(self):
        runner = _make_runner()
        runner._get_brain_injector = MagicMock(return_value=None)
        runner._brain_adapter.search_by_file_path = MagicMock(side_effect=RuntimeError("skill down"))
        injection, _ = runner._inject_dynamic_resources("test")
        assert "[技能检索失败，本轮无技能注入]" in injection
        _assert_siblings_absent(injection, "[技能检索失败，本轮无技能注入]")

    def test_knowledge_retrieval_failure_adds_annotation(self):
        runner = _make_runner()
        runner._get_brain_injector = MagicMock(return_value=None)
        runner._brain_adapter.search_multi_lightrag = MagicMock(side_effect=RuntimeError("kg down"))
        injection, _ = runner._inject_dynamic_resources("test")
        assert "[知识检索失败，本轮无参考知识注入]" in injection
        _assert_siblings_absent(injection, "[知识检索失败，本轮无参考知识注入]")

    def test_region_map_failure_adds_annotation(self):
        runner = _make_runner()
        injector = MagicMock()
        injector.activate_for_query.return_value = ({}, None, None)
        injector.format_region_map_only = MagicMock(side_effect=RuntimeError("map down"))
        runner._get_brain_injector = MagicMock(return_value=injector)
        injection, _ = runner._inject_dynamic_resources("test")
        assert "[脑区状态图生成失败]" in injection
        _assert_siblings_absent(injection, "[脑区状态图生成失败]")

    def test_region_knowledge_format_failure_adds_annotation(self):
        runner = _make_runner()
        injector = MagicMock()
        injector.activate_for_query.return_value = ({}, None, None)
        injector.format_region_map_only.return_value = ""
        injector.format_region_knowledge = MagicMock(side_effect=RuntimeError("fmt down"))
        runner._get_brain_injector = MagicMock(return_value=injector)
        injection, _ = runner._inject_dynamic_resources("test")
        assert "[脑区知识格式化失败]" in injection
        _assert_siblings_absent(injection, "[脑区知识格式化失败]")

    def test_normal_path_has_no_annotations(self):
        """正常路径（检索成功）无任何标注。"""
        runner = _make_runner()
        runner._get_brain_injector = MagicMock(return_value=None)
        runner._brain_adapter.search_by_file_path.return_value = [
            {"entity_name": "report-skill", "entity_type": "Skill", "file_path": "skill_sync",
             "description": "t", "distance": 0.5},
        ]
        runner._brain_adapter.search_multi_lightrag.return_value = {"skill": [], "knowledge": [], "other": []}
        runner._decay_pool.get_top_by_category.side_effect = [
            [MagicMock(entity_dict={"entity_name": "report-skill", "entity_type": "Skill",
                                    "file_path": "skill_sync", "description": "t", "distance": 0.5})],
            [MagicMock(entity_dict={"entity_name": "note", "entity_type": "Note",
                                    "description": "d", "distance": 0.6})],
        ]
        runner._format_lightrag_entities_for_prompt.side_effect = [
            ("skills text", {"report-skill"}),
            ("knowledge text", {"note"}),
        ]
        injection, _ = runner._inject_dynamic_resources("test")
        assert injection  # 非空——正常注入
        for marker in _ANNOTATIONS:
            assert marker not in injection

    def test_brain_injector_failed_flag_adds_annotation(self):
        """标记置位 → [脑区上下文不可用] 标注。

        B6 P3：须同时 mock _get_rag() → None + get_activation_mgr() → None——
        re-check 仅 rag None 可达；activation None + rag 正常被 forced-sync 分支截获——标记永不置位测试必失败。
        """
        runner = _make_real_injector_runner()
        with _brain_patches() as (adapter_cls, get_mgr, _):
            adapter = MagicMock()
            adapter._get_rag.return_value = None
            adapter_cls.return_value = adapter
            get_mgr.return_value = None
            injection, _ = runner._inject_dynamic_resources("test")
        assert "[脑区上下文不可用]" in injection
        assert runner._brain_injector_failed is True

    def test_flag_cleared_after_recovery_removes_annotation(self):
        """恢复（清除）后标注消失（A4 P1）——成功创建路径 L2629 前置清除。"""
        runner = _make_real_injector_runner()
        with _brain_patches() as (adapter_cls, get_mgr, _):
            adapter = MagicMock()
            adapter._get_rag.return_value = None
            adapter_cls.return_value = adapter
            get_mgr.return_value = None
            injection1, _ = runner._inject_dynamic_resources("test")
            assert "[脑区上下文不可用]" in injection1
            # 恢复：rag + activation_mgr 均可用 → _get_brain_injector 成功创建 → 标记清除
            adapter._get_rag.return_value = MagicMock()
            get_mgr.return_value = MagicMock()
            injection2, _ = runner._inject_dynamic_resources("test")
        assert "[脑区上下文不可用]" not in injection2
        assert runner._brain_injector_failed is False

    def test_rag_none_with_activation_mgr_no_annotation(self):
        """rag None + activation_mgr 非 None（无图谱正常态）不标注（B4 P2）。"""
        runner = _make_real_injector_runner()
        with _brain_patches() as (adapter_cls, get_mgr, _):
            adapter = MagicMock()
            adapter._get_rag.return_value = None
            adapter_cls.return_value = adapter
            get_mgr.return_value = MagicMock()
            injection, _ = runner._inject_dynamic_resources("test")
        assert "[脑区上下文不可用]" not in injection
        assert runner._brain_injector_failed is False

    def test_case_a_transition_clears_flag(self):
        """置位 → Case A 过渡（activation 恢复 rag 仍门控）→ 标注消失（B5 P2 互斥清除）。

        re-check 块级互斥置位/清除：activation 恢复但 rag 仍 None 时标记清除——修复期不误标。
        """
        runner = _make_real_injector_runner()
        with _brain_patches() as (adapter_cls, get_mgr, _):
            adapter = MagicMock()
            adapter._get_rag.return_value = None
            adapter_cls.return_value = adapter
            get_mgr.return_value = None
            injection1, _ = runner._inject_dynamic_resources("test")
            assert "[脑区上下文不可用]" in injection1
            # Case A：activation 恢复，rag 仍门控（None）——forced-sync 分支不截获，re-check 块级清除
            get_mgr.return_value = MagicMock()
            injection2, _ = runner._inject_dynamic_resources("test")
        assert "[脑区上下文不可用]" not in injection2
        assert runner._brain_injector_failed is False
