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
        # 本轮命中的工具名（统一注入时取出并清空）
        self._recent_hits: List[str] = []

    def _load_scores(self) -> Dict[str, int]:
        """从 JSON 文件加载工具分数

        过滤掉 visibility=hidden 的工具（防御性，清理持久化文件残留）
        """
        if not self.scores_path.exists():
            return {}

        try:
            scores = json.loads(self.scores_path.read_text(encoding="utf-8"))
            # 过滤掉 hidden 工具的残留分数
            try:
                from agent.tool_registry import get_registry
                registry = get_registry()
                return {k: v for k, v in scores.items()
                        if registry.get_visibility(k) != "hidden"}
            except Exception:
                # ToolRegistry 未初始化时，返回全部（向后兼容）
                return scores
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
            skip_coactivation: 跳过同server工具激活
        """
        current = self.active_tools.get(tool_name, 0)
        if current < 65:
            self.active_tools[tool_name] = 65
        # 高于65则不动，用自己的分
        self._save_scores()

        # 记录本轮命中（统一注入时用于 skill 检索）
        self._recent_hits.append(tool_name)

        # 激活同 server 的其他工具
        if not skip_coactivation:
            self._coactivate_same_server_tools(tool_name)

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

    def _coactivate_same_server_tools(self, tool_name: str):
        """
        激活同 server 的其他工具（低于65补到65）

        Args:
            tool_name: 工具名
        """
        # 1. 激活同 server 的其他工具（低于65补到65）
        if "/" in tool_name:
            server = tool_name.split("/", 1)[0]
            try:
                from agent.runner import get_runner
                from agent.tool_registry import get_registry
                runner = get_runner()
                registry = get_registry()
                if runner and hasattr(runner, '_mcp_tools_schema'):
                    for schema in runner._mcp_tools_schema:
                        name = schema.get("function", {}).get("name", "")
                        if "/" in name:
                            s, _ = name.split("/", 1)
                        else:
                            continue
                        if s == server and name != tool_name:
                            # 跳过 visibility=hidden 的工具（主 Agent 不可见）
                            if registry.get_visibility(name) == "hidden":
                                continue
                            current = self.active_tools.get(name, 0)
                            if current < 65:
                                self.active_tools[name] = 65
                                print(f"[ToolLifecycle] Co-activated: {name} (same server: {server})",
                                      file=sys.stderr, flush=True)
                    self._save_scores()
            except Exception as e:
                print(f"[ToolLifecycle] Failed to co-activate tools for {tool_name}: {e}",
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

        过滤掉 visibility=hidden 的工具（防御性，防止持久化文件残留）

        Returns:
            活跃工具名列表
        """
        try:
            from agent.tool_registry import get_registry
            registry = get_registry()
            return [name for name in self.active_tools.keys()
                    if registry.get_visibility(name) != "hidden"]
        except Exception:
            # ToolRegistry 未初始化时，返回全部（向后兼容）
            return list(self.active_tools.keys())

    def reset_session(self):
        """重置会话级状态（新 chat 开始时调用）"""
        self._recent_hits.clear()

    def consume_recent_hits(self) -> List[str]:
        """获取并清空本轮命中的工具名列表（一次性，调用后清空）"""
        hits = self._recent_hits.copy()
        self._recent_hits.clear()
        return hits

    def clear(self):
        """清空所有活跃工具"""
        self.active_tools.clear()
        self._recent_hits.clear()
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
