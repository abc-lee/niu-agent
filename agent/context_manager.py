"""
上下文管理器 - 统一历史管理职责

职责：
1. 从 MessageStore 加载历史消息
2. 转换消息格式（Message → dict）
3. 上下文压缩（Token 计数 + 消息删除）
4. 上下文窗口监控

架构：
MessageStore (持久化) → ContextManager (管理) → agent_loop (使用)
"""

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.session import MessageStore
from agent.subagent import _read_context_window_tokens, _read_warning_threshold


class ContextManager:
    """上下文管理器 - 统一历史管理职责"""

    def __init__(self, message_store: MessageStore, max_messages: int = 0, max_tokens: int = 0):
        """
        初始化上下文管理器

        Args:
            message_store: 消息存储实例
            max_messages: 最大消息数量（默认0=不限制）
            max_tokens: 最大 token 数量（0 表示从配置读取）
        """
        if max_tokens <= 0:
            max_tokens = _read_context_window_tokens()
        self.store = message_store
        self.max_messages = max_messages
        self.max_tokens = max_tokens
        self._warning_threshold = _read_warning_threshold()

    async def load_history(self, limit: int | None = None) -> list[dict[str, Any]]:
        """
        加载历史消息并转换为 agent_loop 格式

        完整还原 tool 消息：保留 tool_calls、tool_call_id 等字段，
        不再过滤空 content 的 assistant(tool_calls) 消息。

        Args:
            limit: 加载消息数量，None 则使用 max_messages

        Returns:
            消息列表 [{"role": "user/assistant/tool", "content": str, ...}, ...]
        """
        if limit is None or limit <= 0:
            limit = None  # None = 不限制，返回全部消息

        # 从 MessageStore 加载
        messages = await self.store.get_messages(limit=limit)

        # 转换格式 — 完整还原 tool 消息
        history = []
        for msg in messages:
            entry = {"role": msg.role, "content": msg.content or ""}

            # 还原 tool_calls（assistant 消息可能携带工具调用）
            if msg.tool_calls:
                entry["tool_calls"] = msg.tool_calls

            # 还原 tool_call_id（tool 消息必须关联到对应的 tool_call）
            if msg.tool_call_id:
                entry["tool_call_id"] = msg.tool_call_id

            # 完全空的消息（无 content、无 tool_calls、无 tool_call_id）可以跳过
            if not msg.content and not msg.tool_calls and not msg.tool_call_id:
                continue

            history.append(entry)

        return history

    def count_tokens_simple(self, messages: list[dict[str, Any]]) -> int:
        """
        使用 TokenCalculator 计算 token 数量

        回退到字符数估算（约 2 字符/token，偏保守避免低估）。

        Args:
            messages: 消息列表

        Returns:
            token 数量
        """
        try:
            from agent.token_calculator import TokenCalculator
            return TokenCalculator.get().count_messages(messages)
        except Exception:
            # 回退：约 2 字符/token（偏保守，避免低估导致不触发压缩）
            total_tokens = 0
            for msg in messages:
                content = msg.get("content", "")
                total_tokens += max(1, len(content) // 2) + 4
            return total_tokens

    def should_compress(self, messages: list[dict[str, Any]]) -> bool:
        """已禁用：压缩只在 agent_loop 工具循环中同步触发。"""
        return False

    def compress_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        压缩消息列表（保留最近消息）

        策略：
        - 保留最近 80% 的消息
        - 删除早期的 20% 消息
        - assistant(tool_calls) + tool 消息必须成对删除，避免孤立

        Args:
            messages: 消息列表

        Returns:
            压缩后的消息列表
        """
        if not messages:
            return messages

        # 计算保留数量
        keep_count = int(len(messages) * 0.8)

        # 确保至少保留 10 条消息
        keep_count = max(10, keep_count)

        # 计算要删除的数量
        delete_count = len(messages) - keep_count
        if delete_count <= 0:
            return messages

        # 成对删除：如果删除 assistant(tool_calls)，必须同时删除对应的 tool 消息
        # 从前向后扫描，标记要删除的消息
        to_delete = set(range(delete_count))

        # 迭代收敛：删除消息可能产生新的孤立消息，需要反复检查直到稳定
        changed = True
        max_iterations = len(messages)  # 上限：最多迭代消息数量次
        iteration = 0
        while changed and iteration < max_iterations:
            changed = False
            iteration += 1
            changed = False

            # 正向：收集被删除的 assistant(tool_calls) 的 tool_call_id
            deleted_tool_call_ids = set()
            for idx in to_delete:
                msg = messages[idx]
                if msg.get("role") == "assistant" and msg.get("tool_calls"):
                    for tc in msg["tool_calls"]:
                        tc_id = tc.get("id", "")
                        if tc_id:
                            deleted_tool_call_ids.add(tc_id)

            # 正向：被删除的 tool_call_id 对应的 tool 消息也必须删除
            for idx in range(len(messages)):
                if idx in to_delete:
                    continue
                msg = messages[idx]
                if msg.get("role") == "tool" and msg.get("tool_call_id") in deleted_tool_call_ids:
                    if idx not in to_delete:
                        to_delete.add(idx)
                        changed = True

            # 反向：收集被删除的 tool 消息的 tool_call_id
            deleted_tool_call_ids_from_tool = set()
            for idx in to_delete:
                msg = messages[idx]
                if msg.get("role") == "tool" and msg.get("tool_call_id"):
                    deleted_tool_call_ids_from_tool.add(msg["tool_call_id"])

            # 反向：如果 assistant(tool_calls) 的任一 tool_call_id 被删除，该 assistant 也必须删除
            for idx in range(len(messages)):
                if idx in to_delete:
                    continue
                msg = messages[idx]
                if msg.get("role") == "assistant" and msg.get("tool_calls"):
                    tc_ids = {tc.get("id", "") for tc in msg["tool_calls"] if tc.get("id")}
                    if tc_ids and any(tc_id in deleted_tool_call_ids_from_tool for tc_id in tc_ids):
                        if idx not in to_delete:
                            to_delete.add(idx)
                            changed = True

        # 构建压缩后的消息列表
        compressed = [msg for idx, msg in enumerate(messages) if idx not in to_delete]

        # 如果删除了消息，添加压缩说明
        if len(compressed) < len(messages):
            deleted_count = len(messages) - len(compressed)
            compression_note = {
                "role": "user",
                "content": f"[系统] 为优化性能，已压缩早期 {deleted_count} 条消息。"
            }
            compressed.insert(0, compression_note)

        return compressed

    def estimate_context_usage(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """
        估算上下文使用情况

        Args:
            messages: 消息列表

        Returns:
            {
                "message_count": int,
                "estimated_tokens": int,
                "usage_percentage": float,
                "should_compress": bool
            }
        """
        message_count = len(messages)
        estimated_tokens = self.count_tokens_simple(messages)
        usage_percentage = (estimated_tokens / self.max_tokens) * 100
        should_compress = self.should_compress(messages)

        return {
            "message_count": message_count,
            "estimated_tokens": estimated_tokens,
            "usage_percentage": round(usage_percentage, 2),
            "should_compress": should_compress,
            "max_messages": self.max_messages,
            "max_tokens": self.max_tokens
        }

    async def get_context_for_chat(self, exclude_last: bool = True) -> list[dict[str, Any]]:
        """
        获取用于聊天的上下文（主入口）

        流程：
        1. 加载历史消息
        2. 检查是否需要压缩
        3. 如果需要，执行压缩
        4. 返回最终消息列表

        Args:
            exclude_last: 是否排除最后一条消息（当前用户输入）

        Returns:
            历史消息列表
        """
        # 加载历史
        history = await self.load_history()

        # 排除最后一条（如果需要）
        if exclude_last and history:
            history = history[:-1]

        # 检查是否需要压缩
        if self.should_compress(history):
            history = self.compress_messages(history)

        return history


# 全局实例管理
_context_manager: ContextManager | None = None


async def get_context_manager(message_store: MessageStore | None = None) -> ContextManager:
    """
    获取全局 ContextManager 实例

    Args:
        message_store: 消息存储实例（首次调用时需要）

    Returns:
        ContextManager 实例
    """
    global _context_manager

    if _context_manager is None:
        if message_store is None:
            raise ValueError("First call requires message_store parameter")
        _context_manager = ContextManager(message_store)

    return _context_manager


def reset_context_manager():
    """重置全局实例（用于测试）"""
    global _context_manager
    _context_manager = None
