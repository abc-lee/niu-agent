"""统一 token 计数模块 — 使用本地 HuggingFace tokenizer 替代 o200k_base。

中文场景下 o200k_base 对中文高估约 1.3x，导致压缩过早触发。
本模块固定加载 DeepSeek-V3 本地 tokenizer.json，产出与实际 API 一致的 token 计数。
"""

import logging
import threading
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent.parent / "models" / "tokenizers"

# 固定使用 DeepSeek-V3 tokenizer（对中文场景最准确，无需根据模型名动态选择）
_DEFAULT_TOKENIZER = "deepseek-v3"

# messages 结构开销（ChatML 格式：每条消息的 role 标记 + 格式标记）
# <|im_start|>role\n 约 3 token + <|im_end|>\n 约 2 token = 5
_MSG_OVERHEAD = 5
# tool_calls 序列化额外开销（函数名 + 参数括号 + id）
_TOOL_CALL_OVERHEAD = 6
# tool 角色消息中 tool_call_id 序列化开销
_TOOL_CALL_ID_OVERHEAD = 3


class TokenCalculator:
    """统一 token 计数入口。启动时加载本地 tokenizer，所有计数调用走此类。"""

    _instance: Optional["TokenCalculator"] = None
    _instance_lock = threading.Lock()

    def __init__(self):
        self._tokenizer = self._load_tokenizer()
        self._using_fallback = self._tokenizer is None

    @classmethod
    def get(cls) -> "TokenCalculator":
        """获取全局单例。首次调用时自动初始化。线程安全。"""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
                    if cls._instance._using_fallback:
                        logger.warning("[TokenCalculator] Using litellm fallback — token counts may be overestimated for CJK text")
        return cls._instance

    @classmethod
    def reset(cls):
        """重置单例，用于模型切换后重新初始化。"""
        with cls._instance_lock:
            cls._instance = None

    @property
    def using_fallback(self) -> bool:
        """当前是否在使用回退模式（litellm o200k_base）。"""
        return self._using_fallback

    @property
    def tokenizer_name(self) -> str:
        """当前使用的 tokenizer 名称。"""
        if self._tokenizer is not None:
            return _DEFAULT_TOKENIZER
        return "litellm-o200k-fallback"

    def _load_tokenizer(self):
        """从本地文件加载 HuggingFace tokenizer。"""
        tokenizer_path = _BASE_DIR / _DEFAULT_TOKENIZER / "tokenizer.json"

        if not tokenizer_path.exists():
            logger.warning(f"[TokenCalculator] tokenizer not found: {tokenizer_path}, falling back to litellm")
            return None

        try:
            from tokenizers import Tokenizer
            tok = Tokenizer.from_file(str(tokenizer_path))
            logger.info(f"[TokenCalculator] loaded tokenizer: {tokenizer_path}")
            return tok
        except Exception as e:
            logger.warning(f"[TokenCalculator] failed to load tokenizer: {e}, falling back to litellm")
            return None

    def count_text(self, text: str) -> int:
        """计算纯文本的 token 数。"""
        if self._tokenizer is not None:
            return len(self._tokenizer.encode(text).ids)
        # 回退到 litellm o200k_base
        try:
            from litellm import token_counter
            return token_counter(model="gpt-4o", text=text)
        except Exception:
            return _cjk_aware_estimate(text)

    def count_messages(self, messages: List[Dict]) -> int:
        """计算消息列表的 token 数，逐条计算并包含结构开销。

        采用逐条计算而非一次性计算，与 compat.py 和 runner.py 中
        逐条 msg_tokens 求和的方式保持一致，避免两种路径产出不同结果。
        """
        total = 0
        for msg in messages:
            content = msg.get("content", "") or ""
            if isinstance(content, list):
                text_parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
                content = " ".join(text_parts)
            total += self.count_text(content)
            total += _MSG_OVERHEAD
            # tool_calls 额外开销（assistant 消息）
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                total += len(tool_calls) * _TOOL_CALL_OVERHEAD
            # tool 角色消息的 tool_call_id 开销
            if msg.get("role") == "tool":
                total += _TOOL_CALL_ID_OVERHEAD
        return total

    def count_message_single(self, role: str, content: str, tool_calls: list | None = None) -> int:
        """计算单条消息的 token 数（含结构开销）。"""
        content = content or ""
        overhead = _MSG_OVERHEAD
        if role == "tool":
            overhead += _TOOL_CALL_ID_OVERHEAD
        if tool_calls:
            overhead += len(tool_calls) * _TOOL_CALL_OVERHEAD
        return self.count_text(content) + overhead


def _cjk_aware_estimate(text: str) -> int:
    """CJK 感知的字符级 token 估算。

    中文字符约 1.5 字符/token（偏保守避免低估），
    英文/其他字符约 4 字符/token。
    """
    cjk_count = sum(1 for c in text if '\u4e00' <= c <= '\u9fff' or '\u3400' <= c <= '\u4dbf')
    other_count = len(text) - cjk_count
    return max(1, int(cjk_count * 1.5 + other_count * 0.25))
