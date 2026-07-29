"""TokenCalculator 集成测试 — 验证 DeepSeek-V3 本地 tokenizer 替代 litellm o200k_base 后的准确性。

中文场景下 o200k_base 对中文高估约 1.3x，导致压缩过早触发。
本测试验证 TokenCalculator 计数更接近真实值。
"""

import sys
from pathlib import Path

import pytest

# 确保项目根目录在 sys.path 中
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent.token_calculator import (
    _MSG_OVERHEAD,
    _TOOL_CALL_ID_OVERHEAD,
    _TOOL_CALL_OVERHEAD,
    TokenCalculator,
    _cjk_aware_estimate,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_singleton():
    """每个测试前后重置单例，确保测试隔离。"""
    TokenCalculator.reset()
    yield
    TokenCalculator.reset()


@pytest.fixture
def calc() -> TokenCalculator:
    """提供干净的 TokenCalculator 实例。"""
    return TokenCalculator.get()


# ---------------------------------------------------------------------------
# 1. 基础计数测试
# ---------------------------------------------------------------------------

class TestCountText:
    """count_text 基础功能测试。"""

    def test_english_text(self, calc: TokenCalculator):
        """纯英文文本应返回合理的 token 计数。"""
        text = "Hello, this is a simple English sentence for testing."
        count = calc.count_text(text)
        assert isinstance(count, int)
        assert count > 0
        # 英文约 4 字符/token，这句话 54 字符，预期约 10-15 tokens
        assert 5 <= count <= 25

    def test_chinese_text(self, calc: TokenCalculator):
        """纯中文文本应返回合理的 token 计数。"""
        text = "这是一个用于测试的中文句子。"
        count = calc.count_text(text)
        assert isinstance(count, int)
        assert count > 0
        # 中文字符约 1.5 字符/token，15 个中文字符 + 标点，预期约 10-20 tokens
        assert 5 <= count <= 30

    def test_mixed_text(self, calc: TokenCalculator):
        """中英混合文本应返回合理的 token 计数。"""
        text = "这是Chinese and English混合的mixed文本text。"
        count = calc.count_text(text)
        assert isinstance(count, int)
        assert count > 0
        # 混合文本，预期在 10-30 之间
        assert 5 <= count <= 40

    def test_empty_string(self, calc: TokenCalculator):
        """空字符串应返回 0 或极小值。"""
        count = calc.count_text("")
        assert isinstance(count, int)
        assert count >= 0

    def test_whitespace_only(self, calc: TokenCalculator):
        """纯空白字符串应返回合理值。"""
        count = calc.count_text("   \n\t  ")
        assert isinstance(count, int)
        assert count >= 0

    def test_long_chinese_text(self, calc: TokenCalculator):
        """较长中文文本的计数应该随长度增长。"""
        short = "你好世界"
        long = "你好世界" * 50
        short_count = calc.count_text(short)
        long_count = calc.count_text(long)
        assert long_count > short_count
        # 长文本约为短文本的 50 倍（tokenizer 可能有微小偏差）
        assert long_count >= short_count * 40

    def test_special_characters(self, calc: TokenCalculator):
        """特殊字符和 emoji 应返回合理的 token 计数。"""
        text = "特殊字符：@#$%^&*() 和 emoji 🎉🚀💡"
        count = calc.count_text(text)
        assert isinstance(count, int)
        assert count > 0


# ---------------------------------------------------------------------------
# 2. o200k_base 对比测试
# ---------------------------------------------------------------------------

class TestO200kComparison:
    """对比 TokenCalculator 与 litellm o200k_base 的计数差异。"""

    CHINESE_SAMPLES = [
        "这是一个用于测试的中文句子，包含一些常见词汇。",
        "今天天气很好，适合出去散步。我们可以在公园里看看花和树。",
        "人工智能技术在近年来取得了显著的进步，尤其是在自然语言处理领域。",
        "机器学习模型的训练需要大量的数据和计算资源，同时还需要精心设计的算法。",
    ]

    @pytest.fixture
    def litellm_counter(self):
        """如果 litellm 可用，返回 token_counter 函数；否则 skip。"""
        pytest.importorskip("litellm")
        from litellm import token_counter
        return token_counter

    def test_chinese_o200k_overestimates(self, calc: TokenCalculator, litellm_counter):
        """o200k_base 对中文计数应高于 TokenCalculator（验证高估问题）。"""
        for text in self.CHINESE_SAMPLES:
            tc_count = calc.count_text(text)
            o200k_count = litellm_counter(model="gpt-4o", text=text)
            # o200k_base 对中文应高估
            assert o200k_count > tc_count, (
                f"o200k_base ({o200k_count}) should overestimate vs TokenCalculator ({tc_count}) "
                f"for Chinese text: {text[:30]}..."
            )

    def test_chinese_overestimation_ratio(self, calc: TokenCalculator, litellm_counter):
        """o200k_base 对中文的高估比率应在 1.1x-1.5x 范围内。"""
        for text in self.CHINESE_SAMPLES:
            tc_count = calc.count_text(text)
            o200k_count = litellm_counter(model="gpt-4o", text=text)
            if tc_count > 0:
                ratio = o200k_count / tc_count
                # o200k_base 高估约 1.3x，允许 1.1x-1.6x 范围
                assert 1.1 <= ratio <= 1.6, (
                    f"Overestimation ratio {ratio:.2f}x out of expected range [1.1, 1.6] "
                    f"for text: {text[:30]}... (tc={tc_count}, o200k={o200k_count})"
                )

    def test_english_counts_similar(self, calc: TokenCalculator, litellm_counter):
        """英文文本下两种计数应比较接近。"""
        english_samples = [
            "This is a simple English sentence for testing purposes.",
            "The quick brown fox jumps over the lazy dog.",
            "Machine learning models require large amounts of training data.",
        ]
        for text in english_samples:
            tc_count = calc.count_text(text)
            o200k_count = litellm_counter(model="gpt-4o", text=text)
            ratio = max(tc_count, o200k_count) / max(min(tc_count, o200k_count), 1)
            # 英文场景下差异应较小（ratio < 1.3）
            assert ratio < 1.3, (
                f"English text ratio {ratio:.2f}x too large: "
                f"tc={tc_count}, o200k={o200k_count} for: {text[:40]}..."
            )


# ---------------------------------------------------------------------------
# 3. count_messages 测试
# ---------------------------------------------------------------------------

class TestCountMessages:
    """count_messages 消息列表计数测试。"""

    def test_simple_user_assistant_pair(self, calc: TokenCalculator):
        """简单的 user/assistant 消息对。"""
        messages = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！有什么可以帮你的吗？"},
        ]
        count = calc.count_messages(messages)
        # 手动计算验证
        user_text_count = calc.count_text("你好")
        assistant_text_count = calc.count_text("你好！有什么可以帮你的吗？")
        expected = user_text_count + _MSG_OVERHEAD + assistant_text_count + _MSG_OVERHEAD
        assert count == expected

    def test_tool_calls_in_assistant(self, calc: TokenCalculator):
        """带 tool_calls 的 assistant 消息应包含额外开销。"""
        messages = [
            {"role": "user", "content": "查天气"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}},
                ],
            },
        ]
        count = calc.count_messages(messages)
        user_text = calc.count_text("查天气")
        # assistant content is None → treated as ""
        assistant_text = calc.count_text("")
        expected = user_text + _MSG_OVERHEAD + assistant_text + _MSG_OVERHEAD + 1 * _TOOL_CALL_OVERHEAD
        assert count == expected

    def test_multiple_tool_calls(self, calc: TokenCalculator):
        """多条 tool_calls 应按数量累加开销。"""
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "call_1", "type": "function", "function": {"name": "fn1", "arguments": "{}"}},
                    {"id": "call_2", "type": "function", "function": {"name": "fn2", "arguments": "{}"}},
                    {"id": "call_3", "type": "function", "function": {"name": "fn3", "arguments": "{}"}},
                ],
            },
        ]
        count = calc.count_messages(messages)
        expected = calc.count_text("") + _MSG_OVERHEAD + 3 * _TOOL_CALL_OVERHEAD
        assert count == expected

    def test_tool_role_message(self, calc: TokenCalculator):
        """tool 角色消息应包含 tool_call_id 额外开销。"""
        messages = [
            {
                "role": "tool",
                "content": "北京今天晴，25°C",
                "tool_call_id": "call_abc123",
            },
        ]
        count = calc.count_messages(messages)
        expected = calc.count_text("北京今天晴，25°C") + _MSG_OVERHEAD + _TOOL_CALL_ID_OVERHEAD
        assert count == expected

    def test_tool_role_without_call_id(self, calc: TokenCalculator):
        """tool 角色消息没有 tool_call_id 时不应加额外开销。"""
        messages = [
            {"role": "tool", "content": "结果"},
        ]
        count = calc.count_messages(messages)
        expected = calc.count_text("结果") + _MSG_OVERHEAD
        assert count == expected

    def test_list_content_format(self, calc: TokenCalculator):
        """content 为 list 格式的消息（multimodal content parts）应提取 text。"""
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "描述一下这张图片"},
                    {"type": "image_url", "image_url": {"url": "https://example.com/img.png"}},
                    {"type": "text", "text": "请详细描述"},
                ],
            },
        ]
        count = calc.count_messages(messages)
        # 只计算 text 类型的部分，用空格连接
        combined_text = "描述一下这张图片 请详细描述"
        expected = calc.count_text(combined_text) + _MSG_OVERHEAD
        assert count == expected

    def test_empty_content_list(self, calc: TokenCalculator):
        """content 为空 list 时应按空文本处理。"""
        messages = [
            {"role": "user", "content": []},
        ]
        count = calc.count_messages(messages)
        expected = calc.count_text("") + _MSG_OVERHEAD
        assert count == expected

    def test_none_content(self, calc: TokenCalculator):
        """content 为 None 时应按空文本处理。"""
        messages = [
            {"role": "assistant", "content": None},
        ]
        count = calc.count_messages(messages)
        expected = calc.count_text("") + _MSG_OVERHEAD
        assert count == expected

    def test_empty_messages_list(self, calc: TokenCalculator):
        """空消息列表应返回 0。"""
        count = calc.count_messages([])
        assert count == 0

    def test_missing_content_key(self, calc: TokenCalculator):
        """消息没有 content 字段时应按空文本处理。"""
        messages = [
            {"role": "user"},
        ]
        count = calc.count_messages(messages)
        expected = calc.count_text("") + _MSG_OVERHEAD
        assert count == expected

    def test_full_conversation_with_tool_use(self, calc: TokenCalculator):
        """完整对话流程：user → assistant(tool_call) → tool(result) → assistant(response)。"""
        messages = [
            {"role": "user", "content": "帮我查一下北京天气"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "北京"}'}},
                ],
            },
            {"role": "tool", "content": "北京：晴，25°C", "tool_call_id": "call_1"},
            {"role": "assistant", "content": "北京今天天气晴朗，气温25°C，适合外出。"},
        ]
        count = calc.count_messages(messages)
        # 逐条手动计算
        expected = (
            calc.count_text("帮我查一下北京天气") + _MSG_OVERHEAD
            + calc.count_text("") + _MSG_OVERHEAD + 1 * _TOOL_CALL_OVERHEAD
            + calc.count_text("北京：晴，25°C") + _MSG_OVERHEAD + _TOOL_CALL_ID_OVERHEAD
            + calc.count_text("北京今天天气晴朗，气温25°C，适合外出。") + _MSG_OVERHEAD
        )
        assert count == expected


# ---------------------------------------------------------------------------
# 4. count_message_single 测试
# ---------------------------------------------------------------------------

class TestCountMessageSingle:
    """count_message_single 单条消息计数测试。"""

    def test_user_message(self, calc: TokenCalculator):
        """user 角色消息应包含 _MSG_OVERHEAD。"""
        count = calc.count_message_single("user", "你好")
        expected = calc.count_text("你好") + _MSG_OVERHEAD
        assert count == expected

    def test_assistant_message(self, calc: TokenCalculator):
        """assistant 角色消息应包含 _MSG_OVERHEAD。"""
        count = calc.count_message_single("assistant", "你好！")
        expected = calc.count_text("你好！") + _MSG_OVERHEAD
        assert count == expected

    def test_tool_message(self, calc: TokenCalculator):
        """tool 角色消息应包含 _MSG_OVERHEAD + _TOOL_CALL_ID_OVERHEAD。"""
        count = calc.count_message_single("tool", "结果内容")
        expected = calc.count_text("结果内容") + _MSG_OVERHEAD + _TOOL_CALL_ID_OVERHEAD
        assert count == expected

    def test_empty_content(self, calc: TokenCalculator):
        """空内容的 user 消息。"""
        count = calc.count_message_single("user", "")
        expected = calc.count_text("") + _MSG_OVERHEAD
        assert count == expected

    def test_tool_overhead_greater_than_user(self, calc: TokenCalculator):
        """tool 角色的开销应大于 user 角色（相同内容时）。"""
        user_count = calc.count_message_single("user", "相同内容")
        tool_count = calc.count_message_single("tool", "相同内容")
        assert tool_count > user_count
        assert tool_count - user_count == _TOOL_CALL_ID_OVERHEAD

    def test_assistant_with_tool_calls(self, calc: TokenCalculator):
        """带 tool_calls 的 assistant 消息应累加 _TOOL_CALL_OVERHEAD。"""
        tool_calls = [{"id": "call_1", "type": "function", "function": {"name": "test", "arguments": "{}"}}]
        count = calc.count_message_single("assistant", "调用工具", tool_calls=tool_calls)
        expected = calc.count_text("调用工具") + _MSG_OVERHEAD + 1 * _TOOL_CALL_OVERHEAD
        assert count == expected

    def test_assistant_with_multiple_tool_calls(self, calc: TokenCalculator):
        """多条 tool_calls 应按数量累加开销。"""
        tool_calls = [
            {"id": "call_1", "type": "function", "function": {"name": "a", "arguments": "{}"}},
            {"id": "call_2", "type": "function", "function": {"name": "b", "arguments": "{}"}},
            {"id": "call_3", "type": "function", "function": {"name": "c", "arguments": "{}"}},
        ]
        count = calc.count_message_single("assistant", "三次调用", tool_calls=tool_calls)
        expected = calc.count_text("三次调用") + _MSG_OVERHEAD + 3 * _TOOL_CALL_OVERHEAD
        assert count == expected

    def test_tool_calls_none_default(self, calc: TokenCalculator):
        """不传 tool_calls 时行为与修改前一致。"""
        count = calc.count_message_single("assistant", "普通回复")
        expected = calc.count_text("普通回复") + _MSG_OVERHEAD
        assert count == expected

    def test_tool_calls_empty_list(self, calc: TokenCalculator):
        """空 tool_calls 列表不增加开销。"""
        count = calc.count_message_single("assistant", "无调用", tool_calls=[])
        expected = calc.count_text("无调用") + _MSG_OVERHEAD
        assert count == expected

    def test_none_content_with_tool_calls(self, calc: TokenCalculator):
        """content 为 None 时带 tool_calls 不崩溃。"""
        tool_calls = [{"id": "call_1", "type": "function", "function": {"name": "x", "arguments": "{}"}}]
        count = calc.count_message_single("assistant", None, tool_calls=tool_calls)
        expected = calc.count_text("") + _MSG_OVERHEAD + 1 * _TOOL_CALL_OVERHEAD
        assert count == expected


# ---------------------------------------------------------------------------
# 5. 单例测试
# ---------------------------------------------------------------------------

class TestSingleton:
    """TokenCalculator 单例行为测试。"""

    def test_get_returns_same_instance(self):
        """多次 get() 应返回同一实例。"""
        instance1 = TokenCalculator.get()
        instance2 = TokenCalculator.get()
        assert instance1 is instance2

    def test_reset_creates_new_instance(self):
        """reset() 后再 get() 应返回新实例。"""
        instance1 = TokenCalculator.get()
        TokenCalculator.reset()
        instance2 = TokenCalculator.get()
        assert instance1 is not instance2

    def test_reset_clears_instance(self):
        """reset() 后 _instance 应为 None。"""
        TokenCalculator.get()
        TokenCalculator.reset()
        assert TokenCalculator._instance is None

    def test_get_after_reset_functional(self):
        """reset() 后的新实例应正常工作。"""
        TokenCalculator.get()
        TokenCalculator.reset()
        new_instance = TokenCalculator.get()
        count = new_instance.count_text("测试文本")
        assert isinstance(count, int)
        assert count > 0


# ---------------------------------------------------------------------------
# 6. CJK 回退估算测试
# ---------------------------------------------------------------------------

class TestCJKAwareEstimate:
    """_cjk_aware_estimate 纯 CJK 估算函数测试。"""

    def test_pure_chinese(self):
        """纯中文文本估算。"""
        # 10 个中文字符 → 10 * 1.5 = 15
        count = _cjk_aware_estimate("这是一段中文文本测试")
        assert count == 15

    def test_pure_english(self):
        """纯英文文本估算。"""
        # 12 个英文字符 → 12 * 0.25 = 3
        count = _cjk_aware_estimate("Hello world!")
        assert count == 3

    def test_mixed_text(self):
        """中英混合文本估算。"""
        # "你好" 2 CJK → 2*1.5=3, "Hi" 2 other → 2*0.25=0.5→0 → total=3
        text = "你好Hi"
        cjk_count = sum(1 for c in text if '一' <= c <= '鿿' or '㐀' <= c <= '䶿')
        other_count = len(text) - cjk_count
        expected = max(1, int(cjk_count * 1.5 + other_count * 0.25))
        count = _cjk_aware_estimate(text)
        assert count == expected

    def test_empty_string(self):
        """空字符串应返回 1（max(1, 0)）。"""
        count = _cjk_aware_estimate("")
        assert count == 1

    def test_single_chinese_char(self):
        """单个中文字符。"""
        # 1 * 1.5 = 1.5 → int(1.5) = 1
        count = _cjk_aware_estimate("你")
        assert count == 1

    def test_long_chinese(self):
        """长中文文本的估算应与短文本呈线性关系。"""
        short = "你好"
        long = "你好" * 100
        short_count = _cjk_aware_estimate(short)
        long_count = _cjk_aware_estimate(long)
        assert long_count == short_count * 100

    def test_cjk_ext_a_range(self):
        """CJK 扩展 A 区字符（U+3400-U+4DBF）也应被识别。"""
        # U+3447 是 CJK 扩展 A 区字符
        text = "㑇"
        count = _cjk_aware_estimate(text)
        # 1 CJK 字符 → 1 * 1.5 = 1.5 → int(1.5) = 1
        assert count == 1


# ---------------------------------------------------------------------------
# 7. 属性测试
# ---------------------------------------------------------------------------

class TestProperties:
    """TokenCalculator 属性测试。"""

    def test_not_using_fallback(self, calc: TokenCalculator):
        """本地 tokenizer 可用时，using_fallback 应为 False。"""
        # 前提：tokenizer.json 文件存在
        tokenizer_path = _PROJECT_ROOT / "models" / "tokenizers" / "deepseek-v3" / "tokenizer.json"
        if tokenizer_path.exists():
            assert calc.using_fallback is False
        else:
            assert calc.using_fallback is True

    def test_tokenizer_name_with_local(self, calc: TokenCalculator):
        """本地 tokenizer 可用时，tokenizer_name 应为 deepseek-v3。"""
        tokenizer_path = _PROJECT_ROOT / "models" / "tokenizers" / "deepseek-v3" / "tokenizer.json"
        if tokenizer_path.exists():
            assert calc.tokenizer_name == "deepseek-v3"
        else:
            assert calc.tokenizer_name == "litellm-o200k-fallback"

    def test_overhead_constants(self):
        """验证开销常量值。"""
        assert _MSG_OVERHEAD == 5
        assert _TOOL_CALL_OVERHEAD == 6
        assert _TOOL_CALL_ID_OVERHEAD == 3
