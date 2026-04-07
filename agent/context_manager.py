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
from typing import List, Dict, Any, Optional
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.session import MessageStore, Message


class ContextManager:
    """上下文管理器 - 统一历史管理职责"""

    def __init__(self, message_store: MessageStore, max_messages: int = 50, max_tokens: int = 200000):
        """
        初始化上下文管理器

        Args:
            message_store: 消息存储实例
            max_messages: 最大消息数量（默认50条）
            max_tokens: 最大 token 数量（默认200K）
        """
        self.store = message_store
        self.max_messages = max_messages
        self.max_tokens = max_tokens

    async def load_history(self, limit: Optional[int] = None) -> List[Dict[str, str]]:
        """
        加载历史消息并转换为 agent_loop 格式

        Args:
            limit: 加载消息数量，None 则使用 max_messages

        Returns:
            消息列表 [{"role": "user/assistant", "content": str}, ...]
        """
        if limit is None:
            limit = self.max_messages

        # 从 MessageStore 加载
        messages = await self.store.get_messages(limit=limit)

        # 转换格式
        history = []
        for msg in messages:
            if msg.content:  # 跳过空消息
                history.append({
                    "role": msg.role,
                    "content": msg.content
                })

        return history

    def count_tokens_simple(self, messages: List[Dict[str, str]]) -> int:
        """
        简单估算 token 数量（粗略估算，每个词约 1.3 tokens）

        Args:
            messages: 消息列表

        Returns:
            估算的 token 数量
        """
        total_tokens = 0

        for msg in messages:
            content = msg.get("content", "")
            # 简单估算：按空格分词，每个词约 1.3 tokens
            words = len(content.split())
            tokens = int(words * 1.3)
            total_tokens += tokens

            # 每条消息的固定开销（role + 格式）
            total_tokens += 4

        return total_tokens

    def should_compress(self, messages: List[Dict[str, str]]) -> bool:
        """
        判断是否需要压缩上下文

        Args:
            messages: 消息列表

        Returns:
            是否需要压缩
        """
        # 条件1: 消息数量超过限制
        if len(messages) > self.max_messages:
            return True

        # 条件2: Token 数量超过阈值的 80%
        tokens = self.count_tokens_simple(messages)
        if tokens > self.max_tokens * 0.8:
            return True

        return False

    def compress_messages(self, messages: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """
        压缩消息列表（保留最近消息）

        策略：
        - 保留最近 80% 的消息
        - 删除早期的 20% 消息

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

        # 保留最近的消息
        compressed = messages[-keep_count:]

        # 如果删除了消息，添加压缩说明
        if len(compressed) < len(messages):
            deleted_count = len(messages) - len(compressed)
            compression_note = {
                "role": "user",
                "content": f"[系统] 为优化性能，已压缩早期 {deleted_count} 条消息。"
            }
            compressed.insert(0, compression_note)

        return compressed

    def estimate_context_usage(self, messages: List[Dict[str, str]]) -> Dict[str, Any]:
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

    async def get_context_for_chat(self, exclude_last: bool = True) -> List[Dict[str, str]]:
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
_context_manager: Optional[ContextManager] = None


async def get_context_manager(message_store: Optional[MessageStore] = None) -> ContextManager:
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
