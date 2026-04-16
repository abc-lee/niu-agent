"""
工具生命周期管理

管理工具在对话单元中的生命周期，实现分数衰减机制。
支持持久化存储，程序重启后保留工具分数。

规则：
1. 每轮衰减：所有分数 -10，低于 25 移除
2. 向量检索：检索到工具 → 相似度×100 → 和衰减后分数取大值
3. 工具被调用：和衰减后分数比 → 高于55用自己的分 → 低于55补到55
"""

import json
import sys
from pathlib import Path
from typing import Dict, List


class ToolLifecycleManager:
    """管理工具在对话单元中的生命周期（带持久化）"""

    def __init__(self, decay_rate: int = 10, remove_threshold: int = 25):
        """
        Args:
            decay_rate: 每轮衰减分数（默认10分/轮）
            remove_threshold: 低于此分数移除工具（默认25分）
        """
        self.scores_path = Path.home() / ".niu" / "tool_scores.json"
        self.decay_rate = decay_rate
        self.remove_threshold = remove_threshold
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

    def hit_tool(self, tool_name: str, skip_coactivation: bool = False):
        """
        工具被 LLM 实际调用

        规则：和衰减后分数比 → 高于55用自己的分 → 低于55补到55
        设65：同轮衰减后55，用户看到的是55

        Args:
            tool_name: 工具名，格式为 "server-name/tool-name"
            skip_coactivation: 跳过同server工具激活和Skills检索
        """
        current = self.active_tools.get(tool_name, 0)
        if current < 65:
            self.active_tools[tool_name] = 65
        # 高于65则不动，用自己的分
        self._save_scores()

        # 检索相关Skills
        if not skip_coactivation:
            self._activate_related_skills(tool_name)

    def update_from_search(self, tool_name: str, search_score: int):
        """
        向量检索到工具，和衰减后分数取大值

        规则2：向量检索分和衰减分取大值

        Args:
            tool_name: 工具名
            search_score: 向量检索相似度×100
        """
        current = self.active_tools.get(tool_name, 0)
        new_score = max(current, search_score)
        if new_score != current:
            self.active_tools[tool_name] = new_score
            self._save_scores()

    def _activate_related_skills(self, tool_name: str):
        """
        用工具名去向量库检索相关Skills，并激活同server的其他工具

        Args:
            tool_name: 工具名
        """
        # 1. 激活同 server 的其他工具（低于65补到65）
        if "/" in tool_name:
            server = tool_name.split("/", 1)[0]
            try:
                from agent.runner import get_runner
                runner = get_runner()
                if runner and hasattr(runner, '_mcp_tools_schema'):
                    for schema in runner._mcp_tools_schema:
                        name = schema.get("function", {}).get("name", "")
                        if "/" in name:
                            s, _ = name.split("/", 1)
                        else:
                            continue
                        if s == server and name != tool_name:
                            current = self.active_tools.get(name, 0)
                            if current < 65:
                                self.active_tools[name] = 65
                                print(f"[ToolLifecycle] Co-activated: {name} (same server: {server})",
                                      file=sys.stderr, flush=True)
                    self._save_scores()
            except Exception as e:
                print(f"[ToolLifecycle] Failed to co-activate tools for {tool_name}: {e}",
                      file=sys.stderr, flush=True)

        # 2. 检索相关 Skills
        try:
            from agent.vector_search import get_vector_search

            vs = get_vector_search()
            skills = vs.search(
                query=tool_name,
                limit=2,
                min_score=0.3,
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
            print(f"[ToolLifecycle] Failed to find skills for {tool_name}: {e}",
                  file=sys.stderr, flush=True)

    def decay_tools(self):
        """
        每轮对话后衰减所有工具分数

        规则：
        - 所有工具分数 -decay_rate
        - 分数 < remove_threshold 的工具被移除
        - 保存到文件
        """
        to_remove = []
        for tool_name, score in self.active_tools.items():
            new_score = score - self.decay_rate
            self.active_tools[tool_name] = new_score

            if new_score < self.remove_threshold:
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
