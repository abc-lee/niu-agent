"""Ebbinghaus 遗忘曲线衰减池。

统一管理 Skill/Knowledge/InteractionHabit 的注入与衰减。
公式: R_i(t) = s_i × e^(-t/S)
  - s_i: 命中时的向量余弦相似度（0~1）
  - t: 经过轮数
  - S=5: 记忆稳定性参数
  - 阈值=0.35: 低于此值淘汰
脑区 activation（*0.92, 阈值0.3）完全独立，不由此池管理。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


# 衰减池常量（统一定义，避免硬编码散落多处）
DECAY_S = 5.0                   # 记忆稳定性参数
DECAY_THRESHOLD = 0.35           # 注入阈值
DECAY_FACTOR = math.exp(-1 / DECAY_S)  # ≈ 0.8187，每轮衰减因子


@dataclass
class DecayEntry:
    """衰减池中的单个实体条目。"""
    entity_name: str
    entity_dict: dict[str, Any]   # LightRAG entity dict（description, entity_type 等）
    category: str                  # skill / knowledge / interactionhabit
    source: str                    # "vector" / "graph_traversal"
    score: float                   # 当前 R 值


class DecayPool:
    """Ebbinghaus 衰减池，管理跨轮次的知识实体注入与淘汰。

    使用方法:
        pool = DecayPool()
        pool.inject("定时任务", entity_dict, "knowledge", "vector", 0.65)
        pool.decay()  # 每轮调用
        top = pool.get_top_by_category("knowledge", top_n=10)
    """

    def __init__(self) -> None:
        self._entries: dict[str, DecayEntry] = {}  # key = entity_name (lowercase)

    def decay(self) -> None:
        """每轮衰减：所有 entry score *= DECAY_FACTOR，清理低于阈值的。"""
        for entry in self._entries.values():
            entry.score *= DECAY_FACTOR
        self._entries = {
            k: v for k, v in self._entries.items()
            if v.score >= DECAY_THRESHOLD
        }

    def inject(
        self,
        entity_name: str,
        entity_dict: dict[str, Any],
        category: str,
        source: str,
        vector_score: float,
    ) -> None:
        """注入新命中：保留高分（不降分），低分时仅更新 entity_dict。

        如果实体已在池中且新分数低于现有分数，不覆盖（保留高分）。
        """
        key = entity_name.lower()
        existing = self._entries.get(key)
        if existing is not None and vector_score < existing.score:
            existing.entity_dict = entity_dict
            existing.category = category
            existing.source = source
            return
        self._entries[key] = DecayEntry(
            entity_name=entity_name,
            entity_dict=entity_dict,
            category=category,
            source=source,
            score=vector_score,
        )

    def get_top_by_category(self, category: str, top_n: int) -> list[DecayEntry]:
        """按 category 取 top N（按 score 降序）。"""
        qualified = [
            e for e in self._entries.values()
            if e.category == category and e.score >= DECAY_THRESHOLD
        ]
        qualified.sort(key=lambda e: e.score, reverse=True)
        return qualified[:top_n]

    def get_top_by_source(self, source: str, top_n: int) -> list[DecayEntry]:
        """按 source 取 top N（按 score 降序）。"""
        qualified = [
            e for e in self._entries.values()
            if e.source == source and e.score >= DECAY_THRESHOLD
        ]
        qualified.sort(key=lambda e: e.score, reverse=True)
        return qualified[:top_n]

    def get_entry(self, entity_name: str) -> DecayEntry | None:
        """获取实体的衰减池条目（不存在返回 None）。"""
        return self._entries.get(entity_name.lower())

    def clear(self) -> None:
        """清空衰减池（新会话时调用）。"""
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)
