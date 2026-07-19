# tests/test_skill_score_counter.py
"""Skill 计数器衰减注入单元测试。

流程分三个阶段：前置 → _update_skill_counter（函数内 5 步）→ 后置。

前置（每轮调用前，外部已完成）：
- 向量库检索得候选 skill 集合（candidate_entities）

_update_skill_counter 函数内 5 步（与 runner.py docstring 一致）：
- Step 1: 未命中衰减 — 所有计数器 > 0 且不在候选集合里的 skill，各 -1 分
- Step 2: 命中加分（已熟悉）— 候选集合里计数器 ≥7 且 <10 的，+1 分（封顶 10，7 分走这条分支到 8）
- Step 3: 命中置位（新命中或低分）— 候选集合里计数器 <7 的，直接置为 7（7 分不走这条分支）
- Step 4: entity dict 缓存更新 — 候选集合里的 skill 用本轮 entity dict 覆盖 cache
- Step 5: 清理 0 分项 — counter 和 entity_cache 同步删除 ≤0 分项

后置（每轮调用后，外部继续）：
- 第二阶段 Top_N 注入 — 所有计数器 ≥3 的 skill 按分数倒序取前 N 个注入 prompt
"""
from agent.runner import NiuRunner


def _make_entity(name: str, desc: str = "test") -> dict:
    """构造测试用 entity dict"""
    return {"entity_name": name, "entity_type": "skill", "description": desc}


def test_first_hit_sets_score_to_7():
    """首次命中：0 → 7（第 4 步置位）"""
    counter: dict[str, int] = {}
    cache: dict[str, dict] = {}
    candidates = {"skillA": _make_entity("skillA")}
    NiuRunner._update_skill_counter(counter, cache, candidates)
    assert counter["skillA"] == 7


def test_second_hit_increments_to_8():
    """连续第二次命中：7 → 8（第 3 步加分）"""
    counter: dict[str, int] = {"skillA": 7}
    cache: dict[str, dict] = {}
    candidates = {"skillA": _make_entity("skillA")}
    NiuRunner._update_skill_counter(counter, cache, candidates)
    assert counter["skillA"] == 8


def test_consecutive_hits_cap_at_10():
    """连续命中封顶 10：10 → 10（第 3 步加分但不超 10）"""
    counter: dict[str, int] = {"skillA": 10}
    cache: dict[str, dict] = {}
    candidates = {"skillA": _make_entity("skillA")}
    NiuRunner._update_skill_counter(counter, cache, candidates)
    assert counter["skillA"] == 10


def test_low_score_hit_resets_to_7():
    """计数器低于 7 的命中直接置 7：5 → 7（第 4 步置位，不是 +2）"""
    counter: dict[str, int] = {"skillA": 5}
    cache: dict[str, dict] = {}
    candidates = {"skillA": _make_entity("skillA")}
    NiuRunner._update_skill_counter(counter, cache, candidates)
    assert counter["skillA"] == 7


def test_non_hit_decrements_when_score_above_zero():
    """未命中且分数 > 0：-1（第 2 步衰减）"""
    counter: dict[str, int] = {"skillA": 8}
    cache: dict[str, dict] = {"skillA": _make_entity("skillA")}
    NiuRunner._update_skill_counter(counter, cache, {})  # 没命中
    assert counter["skillA"] == 7


def test_zero_score_cleaned_when_not_hit():
    """未命中且分数已为 0：被 Step 6 清理（不衰减但被清理出 dict）"""
    counter: dict[str, int] = {"skillA": 0}
    cache: dict[str, dict] = {"skillA": _make_entity("skillA")}
    NiuRunner._update_skill_counter(counter, cache, {})
    # 0 分不衰减，但 Step 6 清理 ≤0 分项
    assert "skillA" not in counter
    assert "skillA" not in cache  # cache 同步清理


def test_decay_trajectory_10_to_4_six_rounds():
    """完整衰减轨迹：连续 6 轮不命中，10 → 9 → 8 → 7 → 6 → 5 → 4

    第 6 轮后还剩 4 分（仍 ≥3 进 prompt），第 7 轮才降到 3，第 8 轮才被淘汰出 prompt。
    """
    counter: dict[str, int] = {"skillA": 10}
    cache: dict[str, dict] = {"skillA": _make_entity("skillA")}
    expected = [9, 8, 7, 6, 5, 4]
    for expected_score in expected:
        NiuRunner._update_skill_counter(counter, cache, {})
        assert counter["skillA"] == expected_score, f"轮次期望 {expected_score}，实际 {counter['skillA']}"


def test_decay_drops_below_3_after_8_rounds():
    """10 分连续不命中 8 轮后降到 2（被淘汰出 prompt 门槛）"""
    counter: dict[str, int] = {"skillA": 10}
    cache: dict[str, dict] = {"skillA": _make_entity("skillA")}
    for _ in range(8):
        NiuRunner._update_skill_counter(counter, cache, {})
    assert counter["skillA"] == 2


def test_mixed_hit_and_non_hit():
    """混合场景：skillA 命中加分，skillB 未命中衰减"""
    counter: dict[str, int] = {"skillA": 8, "skillB": 5}
    cache: dict[str, dict] = {"skillA": _make_entity("skillA"), "skillB": _make_entity("skillB")}
    candidates = {"skillA": _make_entity("skillA")}  # 只 skillA 命中
    NiuRunner._update_skill_counter(counter, cache, candidates)
    assert counter["skillA"] == 9  # 8 + 1
    assert counter["skillB"] == 4  # 5 - 1


def test_new_skill_in_candidate_sets_to_7():
    """候选集合含新 skill（counter 里没有）：直接置 7"""
    counter: dict[str, int] = {"skillA": 8}
    cache: dict[str, dict] = {"skillA": _make_entity("skillA")}
    candidates = {"skillA": _make_entity("skillA"), "skillB": _make_entity("skillB")}  # skillB 新
    NiuRunner._update_skill_counter(counter, cache, candidates)
    assert counter["skillA"] == 9
    assert counter["skillB"] == 7


def test_candidate_set_empty_all_decay():
    """候选集合空，所有 skill 衰减 1 分（0 分项被清理）"""
    counter: dict[str, int] = {"skillA": 5, "skillB": 0, "skillC": 10}
    cache: dict[str, dict] = {
        "skillA": _make_entity("skillA"),
        "skillB": _make_entity("skillB"),
        "skillC": _make_entity("skillC"),
    }
    NiuRunner._update_skill_counter(counter, cache, {})
    assert counter["skillA"] == 4  # 5 - 1
    assert "skillB" not in counter  # 0 分被清理（不衰减但被 Step 6 清理）
    assert "skillB" not in cache  # cache 同步清理
    assert counter["skillC"] == 9  # 10 - 1


def test_counter_does_not_grow_unbounded():
    """连续命中不会无限增长，封顶 10"""
    counter: dict[str, int] = {}
    cache: dict[str, dict] = {}
    candidates = {"skillA": _make_entity("skillA")}
    for _ in range(20):
        NiuRunner._update_skill_counter(counter, cache, candidates)
    assert counter["skillA"] == 10


def test_select_top_skills_filters_below_3():
    """第二阶段筛选：只保留 ≥3 分的 skill"""
    counter: dict[str, int] = {"skillA": 10, "skillB": 5, "skillC": 2, "skillD": 3}
    result = NiuRunner._select_top_skills(counter, top_n=10)
    result_names = [name for name, _ in result]
    assert "skillA" in result_names
    assert "skillB" in result_names
    assert "skillD" in result_names
    assert "skillC" not in result_names  # 2 分被筛掉


def test_select_top_skills_sorted_descending():
    """第二阶段排序：分数倒序"""
    counter: dict[str, int] = {"low": 3, "mid": 5, "high": 10}
    result = NiuRunner._select_top_skills(counter, top_n=10)
    names = [name for name, _ in result]
    assert names == ["high", "mid", "low"]


def test_select_top_skills_limits_to_n():
    """第二阶段 Top_N：分数相同时按 name 字典序兜底"""
    counter: dict[str, int] = {"a": 5, "b": 5, "c": 5, "d": 5, "d2": 5, "e": 5}
    result = NiuRunner._select_top_skills(counter, top_n=3)
    assert len(result) == 3
    names = [name for name, _ in result]
    assert names == ["a", "b", "c"]


def test_select_top_skills_empty_counter():
    """空计数器返回空列表"""
    result = NiuRunner._select_top_skills({}, top_n=5)
    assert result == []


def test_select_top_skills_top_n_zero_returns_empty():
    """top_n=0 返回空列表（防御边界）"""
    counter: dict[str, int] = {"skillA": 5, "skillB": 10}
    result = NiuRunner._select_top_skills(counter, top_n=0)
    assert result == []


def test_select_top_skills_top_n_negative_returns_empty():
    """top_n 负数返回空列表（防御边界）"""
    counter: dict[str, int] = {"skillA": 5, "skillB": 10}
    result = NiuRunner._select_top_skills(counter, top_n=-3)
    assert result == []


def test_update_counter_does_not_modify_candidate_dict():
    """算法不应修改入参 candidate_entities dict"""
    counter: dict[str, int] = {}
    cache: dict[str, dict] = {}
    candidates = {"skillA": _make_entity("skillA")}
    original = dict(candidates)
    NiuRunner._update_skill_counter(counter, cache, candidates)
    assert candidates == original


def test_zero_score_entries_are_cleaned_up():
    """Step 6 清理：0 分项从 counter 和 cache 同步移除（防止无界增长）"""
    counter: dict[str, int] = {"skillA": 1, "skillB": 7, "skillC": 0}
    cache: dict[str, dict] = {
        "skillA": _make_entity("skillA"),
        "skillB": _make_entity("skillB"),
        "skillC": _make_entity("skillC"),
    }
    # 本轮没命中 → skillA: 1→0(被清理), skillB: 7→6, skillC: 0(被清理)
    NiuRunner._update_skill_counter(counter, cache, {})
    assert "skillA" not in counter  # 衰减到 0 被清理
    assert "skillA" not in cache  # cache 同步清理
    assert "skillC" not in counter  # 原本 0 分被清理
    assert "skillC" not in cache  # cache 同步清理
    assert counter["skillB"] == 6  # 7 - 1
    assert "skillB" in cache  # 6 分仍保留


def test_zero_score_cleaned_after_decay_below_zero():
    """连续不命中 10 轮后从 10 降到 0 被清理（含 cache）"""
    counter: dict[str, int] = {"skillA": 10}
    cache: dict[str, dict] = {"skillA": _make_entity("skillA")}
    # 10→9→8→7→6→5→4→3→2（8 轮）→1（9 轮）→0（10 轮，被清理）
    for _ in range(10):
        NiuRunner._update_skill_counter(counter, cache, {})
    # 10 轮后 counter 和 cache 都应被清理
    assert "skillA" not in counter
    assert "skillA" not in cache


def test_empty_string_key_in_candidate_ignored():
    """空字符串 candidate name 不应进入 counter（防御）"""
    counter: dict[str, int] = {}
    cache: dict[str, dict] = {}
    candidates = {"": _make_entity(""), "skillA": _make_entity("skillA")}
    NiuRunner._update_skill_counter(counter, cache, candidates)
    assert "" not in counter
    assert "" not in cache
    assert counter["skillA"] == 7


def test_empty_string_existing_key_cleaned_up():
    """counter 里历史遗留的空 key 应被清理（cache 同步）"""
    counter: dict[str, int] = {"": 5, "skillA": 7}
    cache: dict[str, dict] = {"": _make_entity(""), "skillA": _make_entity("skillA")}
    candidates = {"skillA": _make_entity("skillA")}
    NiuRunner._update_skill_counter(counter, cache, candidates)
    assert "" not in counter  # 空 key 被清理
    assert "" not in cache
    assert counter["skillA"] == 8  # 7 + 1


def test_entity_cache_updated_on_hit():
    """命中时 entity dict 写入 cache（Step 5）"""
    counter: dict[str, int] = {}
    cache: dict[str, dict] = {}
    new_entity = _make_entity("skillA", "new description")
    candidates = {"skillA": new_entity}
    NiuRunner._update_skill_counter(counter, cache, candidates)
    assert cache["skillA"] == new_entity  # cache 被写入


def test_entity_cache_refreshed_on_repeated_hit():
    """重复命中时 cache 被最新 entity dict 覆盖"""
    counter: dict[str, int] = {"skillA": 7}
    old_entity = _make_entity("skillA", "old")
    cache: dict[str, dict] = {"skillA": old_entity}
    new_entity = _make_entity("skillA", "new description")
    candidates = {"skillA": new_entity}
    NiuRunner._update_skill_counter(counter, cache, candidates)
    assert cache["skillA"] == new_entity  # 被覆盖
    assert cache["skillA"]["description"] == "new description"


def test_entity_cache_preserved_when_not_hit():
    """未命中时 cache 保留旧 entity dict（关键：跨轮注入能力）"""
    counter: dict[str, int] = {"skillA": 8}
    old_entity = _make_entity("skillA", "old")
    cache: dict[str, dict] = {"skillA": old_entity}
    # 本轮没命中 skillA
    NiuRunner._update_skill_counter(counter, cache, {})
    assert counter["skillA"] == 7  # 8 - 1
    assert cache["skillA"] == old_entity  # cache 保留（关键：下一轮可从这里取注入）


def test_default_top_n_constant_is_5():
    """默认注入 Top_N 常量为 5（防止误改）"""
    assert NiuRunner._SKILL_INJECT_TOP_N == 5


def test_default_inject_threshold_is_3():
    """默认注入门槛常量为 3"""
    assert NiuRunner._SKILL_SCORE_INJECT_THRESHOLD == 3
