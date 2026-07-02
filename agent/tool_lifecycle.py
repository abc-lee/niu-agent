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
        content = json.dumps(self.active_tools, indent=2, ensure_ascii=False)
        self.scores_path.write_text(content, encoding="utf-8")

    def hit_tool(self, tool_name: str, score: int = 0, skip_coactivation: bool = False):
        """
        工具被 LLM 实际调用，记录激活并检索相关Skills

        只在 LLM 真正调用工具时触发（handler.dispatch 中调用）。
        向量检索命中的工具不再通过此方法保分，避免衰减-覆盖死循环。

        Args:
            tool_name: 工具名，格式为 "server-name/tool-name"
            score: 保留参数（兼容），0表示LLM实际调用
            skip_coactivation: 跳过同server工具激活和Skills检索
        """
        # LLM 实际调用：确认需要，给高分（能扛 3 轮衰减）
        current = self.active_tools.get(tool_name, 0)
        self.active_tools[tool_name] = max(current, 80)
        print(f"[ToolLifecycle] hit_tool: {tool_name} ({current} -> {self.active_tools[tool_name]})",
              file=sys.stderr, flush=True)
        self._save_scores()  # 立即保存，保证持久化语义

        # 检索相关Skills（仅 LLM 实际调用时触发，向量检索命中时跳过）
        if not skip_coactivation:
            self._activate_related_skills(tool_name)

    def mark_used(self, tool_name: str):
        """
        子 Agent 间接使用工具，标记低分（仅防止被移除）

        子 Agent 调用工具时不应给高分（避免泄露），但需要标记"近期用过"，
        否则主 Agent 后续对话无法注入这些工具。

        Args:
            tool_name: 工具名，格式为 "server-name/tool-name"
        """
        current = self.active_tools.get(tool_name, 0)
        # 只在分数低于60时设60（不覆盖主 Agent 的高分）
        if current < 60:
            self.active_tools[tool_name] = 60
            self._save_scores()
            print(f"[ToolLifecycle] mark_used: {tool_name} ({current} -> 60, sub-agent)",
                  file=sys.stderr, flush=True)

    def _activate_related_skills(self, tool_name: str):
        """
        用工具名去向量库检索相关Skills，并激活同server的其他工具

        当 LLM 调用一个工具时，同 server 的其他工具也应该被激活，
        因为它们通常需要配合使用（如 browser_navigate → browser_interact）。

        Args:
            tool_name: 工具名
        """
        # 1. 激活同 server 的其他工具（给 80 分，与 LLM 调用同等待遇）
        if "/" in tool_name:
            server = tool_name.split("/", 1)[0]
            try:
                from agent.runner import get_runner
                runner = get_runner()
                if runner and hasattr(runner, '_mcp_tools_schema'):
                    for schema in runner._mcp_tools_schema:
                        name = schema.get("name", "")
                        if "/" in name:
                            s, _ = name.split("/", 1)
                        else:
                            continue
                        if s == server and name != tool_name and name not in self.active_tools:
                            self.active_tools[name] = 80
                            print(f"[ToolLifecycle] Co-activated: {name} (same server: {server})",
                                  file=sys.stderr, flush=True)
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
            print(f"[ToolLifecycle] Removed: {tool_name} (score fell below {self.min_score})",
                  file=sys.stderr, flush=True)

        if self.active_tools:
            print(f"[ToolLifecycle] After decay: {dict(sorted(self.active_tools.items(), key=lambda x: -x[1]))}",
                  file=sys.stderr, flush=True)
        else:
            print(f"[ToolLifecycle] After decay: (empty)", file=sys.stderr, flush=True)

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
