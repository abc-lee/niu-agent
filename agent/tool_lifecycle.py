"""
工具生命周期管理

管理工具在对话单元中的生命周期，实现分数衰减机制。
支持持久化存储，程序重启后保留工具分数。
"""

import json
import sys
from pathlib import Path
from typing import Dict, List


class ToolLifecycleManager:
    """管理工具在对话单元中的生命周期（带持久化）"""

    def __init__(self, decay_rate: int = 10, min_score: int = 50):
        """
        Args:
            decay_rate: 每轮衰减分数（默认10分/轮）
            min_score: 低于此分数移除工具（默认50分）
        """
        self.scores_path = Path.home() / ".niu" / "tool_scores.json"
        self.decay_rate = decay_rate
        self.min_score = min_score
        self.active_tools: Dict[str, int] = self._load_scores()
        # 临时存储：工具命中后激活的Skills（不持久化，不衰减）
        self._pending_skills: List[str] = []

    def _load_scores(self) -> Dict[str, int]:
        """从 JSON 文件加载工具分数"""
        if not self.scores_path.exists():
            return {}

        try:
            return json.loads(self.scores_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_scores(self):
        """保存工具分数到 JSON 文件"""
        self.scores_path.parent.mkdir(parents=True, exist_ok=True)
        self.scores_path.write_text(
            json.dumps(self.active_tools, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )

    def hit_tool(self, tool_name: str, score: int = 0):
        """
        工具被命中，记录激活并检索相关Skills

        不再强制设100分。如果提供了 score（来自向量检索），使用该分数；
        否则保持现有分数不变（仅记录命中）。

        衰减-覆盖模式：
        - 每轮开始：所有工具 -10（decay）
        - 向量检索命中：覆盖为新分数（净效果 ≈ +10）
        - 未被检索到的工具：持续 -10/轮

        Args:
            tool_name: 工具名，格式为 "server-name/tool-name"
            score: 向量检索分数（0-100），0表示仅记录命中不更新分数
        """
        if score > 0:
            self.active_tools[tool_name] = score
        elif tool_name not in self.active_tools:
            # 新工具首次命中，给一个初始分数
            self.active_tools[tool_name] = self.min_score
        self._save_scores()  # 立即保存，保证持久化语义

        # 检索相关Skills（临时存储，不持久化）
        self._activate_related_skills(tool_name)

    def _activate_related_skills(self, tool_name: str):
        """
        用工具名去向量库检索相关Skills（临时存储，不持久化）

        Args:
            tool_name: 工具名
        """
        try:
            from agent.vector_search import get_vector_search

            vs = get_vector_search()
            # 用工具名检索Skills
            skills = vs.search(
                query=tool_name,
                limit=2,
                min_score=0.3,  # 降低阈值，因为工具名可能只匹配部分关键词
                filter={"category": "skill"}
            )

            for skill in skills:
                skill_name = skill.metadata.get("name", "")
                if skill_name and skill_name not in self._pending_skills:
                    self._pending_skills.append(skill_name)

            if skills:
                print(f"[ToolLifecycle] Found skills for {tool_name}: {[s.metadata.get('name') for s in skills]}",
                      file=sys.stderr, flush=True)

        except Exception as e:
            # Skills检索失败不影响主流程
            print(f"[ToolLifecycle] Failed to find skills for {tool_name}: {e}", file=sys.stderr, flush=True)

    def decay_tools(self):
        """
        每轮对话后衰减所有工具分数

        规则：
        - 所有工具分数 -decay_rate
        - 分数 < min_score 的工具被移除
        - 保存到文件
        """
        to_remove = []
        for tool_name, score in self.active_tools.items():
            new_score = score - self.decay_rate
            self.active_tools[tool_name] = new_score

            if new_score < self.min_score:
                to_remove.append(tool_name)

        for tool_name in to_remove:
            del self.active_tools[tool_name]

        self._save_scores()

    def get_active_tools(self) -> List[str]:
        """
        获取当前应该注入的工具列表

        Returns:
            活跃工具名列表
        """
        return list(self.active_tools.keys())

    def get_pending_skills(self) -> List[str]:
        """
        获取待注入的Skills列表（临时，不持久化）

        Returns:
            Skills名称列表
        """
        return self._pending_skills.copy()

    def clear_pending_skills(self):
        """清空待注入的Skills列表（对话结束后调用）"""
        self._pending_skills.clear()

    def clear(self):
        """清空所有活跃工具和待注入Skills"""
        self.active_tools.clear()
        self._pending_skills.clear()
        self._save_scores()

    def get_tool_score(self, tool_name: str) -> int:
        """
        获取指定工具的当前分数

        Args:
            tool_name: 工具名

        Returns:
            当前分数，如果工具不存在返回0
        """
        return self.active_tools.get(tool_name, 0)

    def debug_print(self):
        """调试：打印所有活跃工具及其分数"""
        if not self.active_tools:
            print("[ToolLifecycle] No active tools")
            return

        print("[ToolLifecycle] Active tools:")
        for tool_name, score in sorted(self.active_tools.items(), key=lambda x: -x[1]):
            print(f"  {tool_name}: {score}")
