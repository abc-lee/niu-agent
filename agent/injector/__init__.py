"""
Injector Module

动态注入架构 - Skills 同步到向量库。
"""

from .sync import SkillSync, get_skill_sync

__all__ = ["SkillSync", "get_skill_sync"]
