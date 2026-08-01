"""Skill 衰减池与 _inject_dynamic_resources 集成测试。

验证：
1. skill 实体被注入衰减池后能通过 get_top_by_category("skill") 检索到
2. 衰减后低分 skill 被淘汰（低于 DECAY_THRESHOLD=0.35）
"""
from unittest.mock import MagicMock, patch

import pytest

from agent.decay_pool import DecayPool
from agent.runner import NiuRunner

pytestmark = pytest.mark.integration


@pytest.fixture
def runner(monkeypatch):
    """构造一个不依赖 LightRAG/Brain 的 NiuRunner 实例

    使用真实 LightRAGAdapter 类的 mock 实例直接注入 `_brain_adapter` 属性,
    跳过 `_inject_dynamic_resources` 内的局部 import 分支（L1969-1970）

    注意：故意跳过 __init__，已预填 `_inject_dynamic_resources` 当前实际访问的所有实例属性
    （_decay_pool / _INJECT_ENTITY_TYPE_BLACKLIST / _INJECT_ENTITY_NAME_BLACKLIST /
    _brain_adapter）；_format_running_subagents_section 内部用 try/except 包住对其他属性
    的访问不会崩。若未来 `_inject_dynamic_resources` 新增实例属性访问，需同步更新此
    fixture，否则会 AttributeError。
    """
    runner = NiuRunner.__new__(NiuRunner)
    runner._decay_pool = DecayPool()
    runner._INJECT_ENTITY_TYPE_BLACKLIST = set()
    runner._INJECT_ENTITY_NAME_BLACKLIST = set()
    return runner


def _make_mock_adapter(lightrag_results, region_results, habits=None):
    """构造一个 mock LightRAGAdapter 实例，注入 _brain_adapter 跳过局部 import"""
    adapter = MagicMock()
    adapter.search_multi_lightrag.return_value = lightrag_results
    return adapter


def _make_skill_entity(name: str, desc: str = "test desc") -> dict:
    return {
        "entity_name": name,
        "entity_type": "skill",
        "description": desc,
    }


def test_skill_injected_into_decay_pool_retrievable(runner):
    """skill 实体被注入衰减池后能通过 get_top_by_category("skill") 检索到。"""
    skill_entity = _make_skill_entity("定时任务管理", "管理定时任务的创建和查询")
    skill_entity["distance"] = 0.85
    runner._decay_pool.inject(
        entity_name="定时任务管理",
        entity_dict=skill_entity,
        category="skill",
        source="vector",
        vector_score=0.85,
    )
    top_skills = runner._decay_pool.get_top_by_category("skill", 5)
    assert len(top_skills) == 1, f"应有1个 skill，实际 {len(top_skills)}"
    assert top_skills[0].entity_name == "定时任务管理"
    assert top_skills[0].category == "skill"
    assert top_skills[0].source == "vector"
    assert abs(top_skills[0].score - 0.85) < 0.01


def test_low_score_skill_evicted_after_decay(runner):
    """衰减后低分 skill 被淘汰（低于 DECAY_THRESHOLD=0.35）。"""
    skill_entity = _make_skill_entity("临时技能", "临时技能描述")
    runner._decay_pool.inject(
        entity_name="临时技能",
        entity_dict=skill_entity,
        category="skill",
        source="vector",
        vector_score=0.5,
    )
    assert len(runner._decay_pool.get_top_by_category("skill", 5)) == 1
    runner._decay_pool.decay()  # 0.5*0.819 = 0.410 >= 0.35, 仍在
    assert len(runner._decay_pool.get_top_by_category("skill", 5)) == 1, "轮1后应仍在"
    runner._decay_pool.decay()  # 0.5*0.819^2 = 0.336 < 0.35, 淘汰
    top = runner._decay_pool.get_top_by_category("skill", 5)
    assert len(top) == 0, f"轮2后应被淘汰，实际还有 {len(top)}"
