"""DecayPool 衰减单元测试。

2026-08-09 用户需求：保留 Ebbinghaus 遗忘曲线（乘性衰减），参数对齐
脑区 activation（×0.92/轮，阈值 0.3，region_activation.py）——
skill/知识注入与脑区注入衰减语义统一。
"""
from agent.decay_pool import DecayPool


def _inject(pool, name, score, category="skill"):
    pool.inject(
        entity_name=name,
        entity_dict={"entity_name": name},
        category=category,
        source="vector",
        vector_score=score,
    )


def test_decay_factor_aligns_with_brain_region():
    """衰减因子 ≈0.92（与脑区 activation 一致），而非旧的 0.8187。"""
    import math
    from agent.decay_pool import DECAY_S
    factor = math.exp(-1 / DECAY_S)
    assert abs(factor - 0.92) < 0.005


def test_decay_is_multiplicative_and_threshold_applied():
    """乘性衰减保留（用户认可的遗忘曲线方向）；低于阈值被淘汰。"""
    pool = DecayPool()
    _inject(pool, "s1", 1.0)
    _inject(pool, "s2", 0.5)

    pool.decay()  # 1.0→0.92, 0.5→0.46
    entries = pool.get_top_by_category("skill", 5)
    scores = {e.entity_name: e.score for e in entries}
    assert abs(scores["s1"] - 0.92) < 0.01
    assert abs(scores["s2"] - 0.46) < 0.01


def test_decay_until_threshold_eliminates():
    """连续衰减：分数降到阈值以下被淘汰（不再出现在注入结果）。"""
    pool = DecayPool()
    _inject(pool, "low", 0.4)

    for _ in range(20):
        pool.decay()

    entries = pool.get_top_by_category("skill", 5)
    assert all(e.entity_name != "low" for e in entries)


def test_inject_keeps_higher_score():
    """inject 保留高分（低分命中不降分，只更新 entity_dict/category）。"""
    pool = DecayPool()
    _inject(pool, "s1", 0.6)
    pool.inject(
        entity_name="s1",
        entity_dict={"entity_name": "s1", "updated": True},
        category="knowledge",
        source="vector",
        vector_score=0.3,
    )
    entry = pool.get_entry("s1")
    assert abs(entry.score - 0.6) < 1e-9  # 高分保留
    assert entry.category == "knowledge"  # category 更新
    assert entry.entity_dict.get("updated") is True
