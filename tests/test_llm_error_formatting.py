"""E2 Task 1：LLM 错误友好文案纯函数测试（format_llm_error_for_user / extract_error_type / is_litellm_error_type）。

覆盖：映射表全类通道 1 翻译 / 真实 litellm str() 格式 / 三层识别通道 / 保底不变式 /
截断保尾 / 坏 __str__ 兜底 / 脱敏 / extract_error_type 二级提取 / is_litellm_error_type 双模块判定。
"""
import litellm
import pytest

from agent.generic.litellm_adapter import (
    _LLM_ERROR_FRIENDLY,
    extract_error_type,
    format_llm_error_for_user,
    is_litellm_error_type,
)

# 用户可见契约：10 键映射翻译完整文案（文案为产品文案，锁定精确值——不用截断前缀断言）
_MAPPING_EXPECTED = {
    "RateLimitError": "模型服务限流（429），请稍后重试",
    "ServiceUnavailableError": "模型服务暂不可用（503），请稍后重试",
    "AuthenticationError": "模型认证失败（401），请检查 API Key 配置",
    "NotFoundError": "模型或服务不存在（404），请检查模型配置",
    "BadRequestError": "模型请求被拒绝（400），请检查请求参数",
    "LiteLLMUnknownProvider": "模型服务商配置错误，请检查模型名/服务商设置",
    "APIConnectionError": "无法连接模型服务，请检查网络",
    "Timeout": "模型响应超时，请稍后重试",
    "BudgetExceededError": "模型配额已用完，请等待配额恢复或更换模型",
    "MidStreamFallbackError": "模型流式响应中断，请稍后重试",
}


# === 映射表契约 ===

def test_mapping_table_exact_keys_and_copy():
    """映射表键集合与文案与用户契约逐字一致（10 键，无多余无缺失）。"""
    assert set(_LLM_ERROR_FRIENDLY) == set(_MAPPING_EXPECTED)
    for key, copy in _MAPPING_EXPECTED.items():
        assert _LLM_ERROR_FRIENDLY[key] == copy


# === 通道 1：映射全类翻译（显式 error_type → 完整文案） ===

@pytest.mark.parametrize("error_type", sorted(_MAPPING_EXPECTED))
def test_channel1_mapping_translation(error_type):
    """显式 error_type 命中映射表 → 通道 1 中文翻译文案（与原文无关）。"""
    assert format_llm_error_for_user("任何原始错误", error_type) == _MAPPING_EXPECTED[error_type]


# === 真实 litellm 格式：子串②匹配 ===

def test_real_litellm_rate_limit_str_channel1():
    """真实 litellm 异常对象 str() 带 "litellm." 前缀——子串②匹配命中通道 1。

    构造签名按 litellm 1.88.1 实测：RateLimitError(message=..., llm_provider=..., model=...)
    → str() = 'litellm.RateLimitError: You exceeded your current quota'（与旧字面量一致）。
    """
    e = litellm.RateLimitError(message="You exceeded your current quota", llm_provider="openai", model="gpt-4o")
    assert format_llm_error_for_user(str(e), None) == _MAPPING_EXPECTED["RateLimitError"]


def test_real_litellm_timeout_str_channel1():
    """真实 litellm.Timeout 对象 str()（无 Error 后缀键名）——子串②命中 Timeout 键。

    构造签名按 litellm 1.88.1 实测：Timeout(message=..., model=..., llm_provider=...)
    → str() = 'litellm.Timeout: Request timed out'（与旧字面量一致）。
    """
    e = litellm.Timeout(message="Request timed out", model="gpt-4o", llm_provider="openai")
    assert format_llm_error_for_user(str(e), None) == _MAPPING_EXPECTED["Timeout"]


# === BudgetExceededError 真实 str()（构造签名特殊） ===

def test_budget_exceeded_real_str_channel3():
    """BudgetExceededError 真实 str() 无类名 → 无显式 error_type 时通道 3 裸原文（锁定非卡死语义）。"""
    e = litellm.BudgetExceededError(current_cost=10.0, max_budget=5.0, message="Budget has been exceeded!")
    assert format_llm_error_for_user(str(e), None) == "Budget has been exceeded!"


def test_budget_exceeded_explicit_type_channel1():
    """显式 error_type="BudgetExceededError" → 通道 1 翻译（映射条目依赖显式类型①）。"""
    e = litellm.BudgetExceededError(current_cost=10.0, max_budget=5.0, message="Budget has been exceeded!")
    assert format_llm_error_for_user(str(e), "BudgetExceededError") == _MAPPING_EXPECTED["BudgetExceededError"]


# === LiteLLMUnknownProvider（provider 名配置错误恰是 E2 目标场景） ===

def test_litellm_unknown_provider_explicit_channel1():
    """显式 error_type="LiteLLMUnknownProvider" → 通道 1 完整文案。"""
    assert format_llm_error_for_user("provider config broken", "LiteLLMUnknownProvider") == _MAPPING_EXPECTED["LiteLLMUnknownProvider"]


def test_bad_request_substring_tradeoff():
    """基类名取舍锁定：str() 含基类名 BadRequestError，子串②命中 → 通道 1（已知取舍，防御路径）。"""
    assert format_llm_error_for_user("litellm.BadRequestError: Unmapped LLM provider", None) == _MAPPING_EXPECTED["BadRequestError"]


# === 通道 2：非标准类型 → 类型名 + 原文 ===

def test_channel2_nonstandard_type():
    """通道 2：非映射类型 → "模型调用失败（类型名）：{原文}"。"""
    assert format_llm_error_for_user("some detail here", "SomeCustomError") == "模型调用失败（SomeCustomError）：some detail here"


def test_channel2_empty_message_no_dangling_colon():
    """通道 2 空原文：省略冒号——无悬空冒号。"""
    assert format_llm_error_for_user("", "SomeCustomError") == "模型调用失败（SomeCustomError）"


# === 通道 3：裸原文保底 ===

def test_channel3_raw_text_no_prefix():
    """通道 3：无类型可提取 → 裸原文（无前缀）。"""
    assert format_llm_error_for_user("some raw text", None) == "some raw text"


def test_empty_message_fallback():
    """空串 + error_type=None → 保底"模型调用失败"。"""
    assert format_llm_error_for_user("", None) == "模型调用失败"


def test_none_message_fallback():
    """None + error_type=None → 保底"模型调用失败"。"""
    assert format_llm_error_for_user(None, None) == "模型调用失败"


def test_explicit_type_beats_empty_input():
    """显式类型优先于空输入保底：format("", "RateLimitError") → 通道 1 翻译。"""
    assert format_llm_error_for_user("", "RateLimitError") == _MAPPING_EXPECTED["RateLimitError"]


def test_already_friendly_message_no_double_wrap():
    """裸原文自然无双包：输入"模型调用失败" → 原样输出（通道 3 输出再 format 幂等）。"""
    assert format_llm_error_for_user("模型调用失败", None) == "模型调用失败"


def test_empty_error_type_empty_msg_channel3():
    """空串 error_type 与 None 统一走通道 3 保底：format("", "") → "模型调用失败"（无悬空括号）。"""
    assert format_llm_error_for_user("", "") == "模型调用失败"


def test_channel2_output_reformat_double_wrap_locked():
    """锁定已知行为：通道 2 输出再 format 会双包（"模型调用失败（X）："中 X 被③正则提取 → 二次包装）。

    非期望修复——通道 3 输出再 format 幂等（裸原文原样返回），但通道 2 输出含类型名前缀，
    任何再 format 都有双包风险；设计上源头友好化后（full_reply）不重复 format。
    """
    first = format_llm_error_for_user("some detail", "SomeCustomError")
    assert first == "模型调用失败（SomeCustomError）：some detail"
    assert format_llm_error_for_user(first, None) == "模型调用失败（SomeCustomError）：模型调用失败（SomeCustomError）：some detail"


# === 截断保尾 / 坏输入 / 脱敏 ===

def test_long_message_truncated_tail_preserved():
    """超长原文截断保尾 ≤500：尾部内容保留。"""
    long_msg = "A" * 1000 + "TAIL_MARKER_9876543210"
    out = format_llm_error_for_user(long_msg, None)
    assert len(out) <= 500
    assert out.endswith("TAIL_MARKER_9876543210")
    assert "..." in out


class _BadStrError(Exception):
    """__str__ 抛异常的自定义异常（模拟坏输入）。"""

    def __str__(self):
        raise RuntimeError("broken __str__")


def test_bad_str_fallback_non_empty():
    """坏 __str__ 输入兜底：<unprintable> 或保底非空、不抛异常。"""
    out = format_llm_error_for_user(_BadStrError("x"), None)
    assert out  # 非空
    assert "<unprintable>" in out


def test_sanitize_sensitive_fields():
    """脱敏：key=xxx 敏感值不出现在输出中（含 ***）。"""
    out = format_llm_error_for_user("error: key=sk-abc123 detail", None)
    assert "sk-abc123" not in out
    assert "***" in out


# === extract_error_type（与 format 内部二级提取同源） ===

def test_extract_error_type_litellm_prefix():
    """带模块前缀 → 提取到映射键名。"""
    assert extract_error_type("litellm.RateLimitError: quota exceeded") == "RateLimitError"


def test_extract_error_type_timeout():
    """无 Error 后缀键名 Timeout 也能提取。"""
    assert extract_error_type("Timeout: request timed out") == "Timeout"


def test_extract_error_type_regex_fallback():
    """正则③兜底：非映射 *Error 类名也能提取 → 通道 2 展示。"""
    assert extract_error_type("CustomError: boom") == "CustomError"
    assert format_llm_error_for_user("CustomError: boom", None) == "模型调用失败（CustomError）：CustomError: boom"


def test_extract_error_type_none():
    """无类型可提取 → None（空串/None/裸文本）。"""
    assert extract_error_type("some raw text") is None
    assert extract_error_type("") is None
    assert extract_error_type(None) is None


# === is_litellm_error_type（hasattr 双模块动态判定） ===

def test_is_litellm_error_type_positive():
    """litellm 顶层/子模块导出的异常类 → True。"""
    assert is_litellm_error_type("RateLimitError") is True
    assert is_litellm_error_type("LiteLLMUnknownProvider") is True


def test_is_litellm_error_type_negative():
    """内部/非 litellm 异常（非 litellm 属性）→ False。"""
    assert is_litellm_error_type("ValueError") is False


def test_is_litellm_error_type_module_attr_false():
    """非类模块属性（hasattr 为 True 但不是异常类）→ False：防 "exceptions"/"model_cost" 误判。"""
    assert is_litellm_error_type("exceptions") is False
    assert is_litellm_error_type("model_cost") is False
