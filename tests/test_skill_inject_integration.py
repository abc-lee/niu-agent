"""Skill 计数器与 _inject_dynamic_resources 集成测试。

验证：
1. _inject_dynamic_resources 调用后计数器被正确更新
2. 第二阶段排序后的 skill 列表被注入 prompt
3. 计数器跨轮维持状态（多次调用累积）
"""
from unittest.mock import MagicMock, patch

import pytest

from agent.runner import NiuRunner


@pytest.fixture
def runner(monkeypatch):
    """构造一个不依赖 LightRAG/Brain 的 NiuRunner 实例

    使用真实 LightRAGAdapter 类的 mock 实例直接注入 `_brain_adapter` 属性,
    跳过 `_inject_dynamic_resources` 内的局部 import 分支（L1969-1970）

    注意：故意跳过 __init__，已预填 `_inject_dynamic_resources` 当前实际访问的所有实例属性
    （_skill_score_counter / _skill_entity_cache / _INJECT_ENTITY_TYPE_BLACKLIST /
    _INJECT_ENTITY_NAME_BLACKLIST / _brain_adapter）；_format_running_subagents_section
    内部用 try/except 包住对其他属性的访问不会崩。若未来 `_inject_dynamic_resources` 新增
    实例属性访问，需同步更新此 fixture，否则会 AttributeError。
    """
    runner = NiuRunner.__new__(NiuRunner)
    runner._skill_score_counter = {}
    runner._skill_entity_cache = {}  # entity dict 跨轮缓存（关键：未命中时仍可注入）
    runner._INJECT_ENTITY_TYPE_BLACKLIST = set()
    runner._INJECT_ENTITY_NAME_BLACKLIST = set()
    return runner


def _make_mock_adapter(lightrag_results, region_results, habits=None):
    """构造一个 mock LightRAGAdapter 实例，注入 _brain_adapter 跳过局部 import"""
    adapter = MagicMock()
    adapter.search_multi_lightrag.return_value = lightrag_results
    adapter.search_within_region.return_value = region_results
    adapter.search_interaction_habits.return_value = habits or []
    return adapter


def _make_skill_entity(name: str, desc: str = "test desc") -> dict:
    return {
        "entity_name": name,
        "entity_type": "skill",
        "description": desc,
    }


def test_inject_updates_counter_on_first_hit(runner):
    """首次向量检索命中 skill → 计数器记 7"""
    lightrag_results = {"skill": [_make_skill_entity("skillA")], "knowledge": [], "other": []}
    region_results = {"skill": [], "knowledge": [], "other": []}
    runner._brain_adapter = _make_mock_adapter(lightrag_results, region_results)

    with patch.object(runner, "_get_brain_injector", return_value=None):
        runner._inject_dynamic_resources("test context")

    assert runner._skill_score_counter.get("skillA") == 7


def test_inject_accumulates_counter_across_rounds(runner):
    """两轮检索命中同一 skill → 计数器 7 → 8"""
    lightrag_results = {"skill": [_make_skill_entity("skillA")], "knowledge": [], "other": []}
    region_results = {"skill": [], "knowledge": [], "other": []}
    runner._brain_adapter = _make_mock_adapter(lightrag_results, region_results)

    with patch.object(runner, "_get_brain_injector", return_value=None):
        runner._inject_dynamic_resources("ctx1")
        runner._inject_dynamic_resources("ctx2")

    assert runner._skill_score_counter["skillA"] == 8


def test_inject_decays_non_hit_skills(runner):
    """第二轮没命中 skillA → 计数器 -1"""
    # 第一轮命中 skillA → 7
    runner._skill_score_counter = {"skillA": 7}
    # 第二轮没命中任何 skill
    lightrag_results = {"skill": [], "knowledge": [], "other": []}
    region_results = {"skill": [], "knowledge": [], "other": []}
    runner._brain_adapter = _make_mock_adapter(lightrag_results, region_results)

    with patch.object(runner, "_get_brain_injector", return_value=None):
        runner._inject_dynamic_resources("ctx2")

    assert runner._skill_score_counter["skillA"] == 6  # 7 - 1


def test_inject_second_stage_filters_below_3(runner):
    """计数器 <3 分的 skill 不进 prompt（cache 里有 entity dict 但 counter 不够 3 分被筛掉）"""
    # skillA=2 分（被淘汰出 prompt），skillB=10 分（保留）
    runner._skill_score_counter = {"skillA": 2, "skillB": 10}
    # cache 里两个 skill 都有 entity dict（模拟上一轮命中过）
    runner._skill_entity_cache = {
        "skillA": _make_skill_entity("skillA"),
        "skillB": _make_skill_entity("skillB"),
    }
    # 本轮没命中，衰减后 skillA=1(<3 被筛出 prompt), skillB=9
    lightrag_results = {"skill": [], "knowledge": [], "other": []}
    region_results = {"skill": [], "knowledge": [], "other": []}
    runner._brain_adapter = _make_mock_adapter(lightrag_results, region_results)

    with patch.object(runner, "_get_brain_injector", return_value=None):
        injection, _ = runner._inject_dynamic_resources("ctx")

    # 精确断言：检查 prompt 里的 skill 文件路径标志（注入格式为 路径: ~/.niu/skills/{name}.md）
    assert "路径: ~/.niu/skills/skillB.md" in injection
    assert "路径: ~/.niu/skills/skillA.md" not in injection


def test_inject_second_stage_sorts_by_score_desc(runner):
    """注入 prompt 时按分数倒序（cache 里有 entity dict，本轮未命中仍能注入）"""
    runner._skill_score_counter = {"low": 3, "high": 10, "mid": 5}
    # cache 里有三个 skill 的 entity dict
    runner._skill_entity_cache = {
        "low": _make_skill_entity("low"),
        "high": _make_skill_entity("high"),
        "mid": _make_skill_entity("mid"),
    }
    # 本轮都没命中（衰减后 low=2(<3 被筛出 prompt), high=9, mid=4）
    lightrag_results = {"skill": [], "knowledge": [], "other": []}
    region_results = {"skill": [], "knowledge": [], "other": []}
    runner._brain_adapter = _make_mock_adapter(lightrag_results, region_results)

    with patch.object(runner, "_get_brain_injector", return_value=None):
        injection, _ = runner._inject_dynamic_resources("ctx")

    # high 应在 mid 之前（按计数器分数 9 > 4 倒序）
    high_pos = injection.find("路径: ~/.niu/skills/high.md")
    mid_pos = injection.find("路径: ~/.niu/skills/mid.md")
    assert high_pos != -1, f"high 应进 prompt: {injection}"
    assert mid_pos != -1, f"mid 应进 prompt: {injection}"
    assert high_pos < mid_pos, "high 应在 mid 之前"


def test_inject_uses_cache_when_not_hit_this_round(runner):
    """核心：本轮没命中某 skill，但 cache 有 entity dict + counter 仍 ≥3 → 仍进 prompt

    这是"缓慢淘汰"机制的关键测试：被命中过的 skill 不会因下一轮没命中就被立即丢弃。
    """
    # 上一轮命中过 skillA → counter=8, cache 里有 entity dict
    runner._skill_score_counter = {"skillA": 8}
    runner._skill_entity_cache = {"skillA": _make_skill_entity("skillA", "cached desc")}
    # 本轮没命中 skillA（lightrag_results.skill 为空）
    lightrag_results = {"skill": [], "knowledge": [], "other": []}
    region_results = {"skill": [], "knowledge": [], "other": []}
    runner._brain_adapter = _make_mock_adapter(lightrag_results, region_results)

    with patch.object(runner, "_get_brain_injector", return_value=None):
        injection, _ = runner._inject_dynamic_resources("ctx")

    # skillA 仍应进 prompt（counter 8→7，从 cache 取 entity dict）
    assert "路径: ~/.niu/skills/skillA.md" in injection
    assert runner._skill_score_counter["skillA"] == 7  # 8 - 1
