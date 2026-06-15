"""
LiteLLM兼容层 - LLM核心抽象

核心组件：
- BaseSession: LiteLLMSession 的基类
- ToolClient: runner.py 使用的客户端，直接转发 messages + tools
- MockFunction/MockToolCall/MockResponse: 数据结构
"""

import json


# ===== 数据结构 =====

class MockFunction:
    def __init__(self, name, arguments):
        self.name, self.arguments = name, arguments


class MockToolCall:
    def __init__(self, name, args, id=""):
        arg_str = json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else args
        self.function = MockFunction(name, arg_str)
        self.id = id


class MockResponse:
    def __init__(self, thinking, content, tool_calls, raw, stop_reason="end_turn", context_overflow=False, usage=None):
        self.thinking = thinking
        self.content = content
        self.tool_calls = tool_calls
        self.raw = raw
        self.stop_reason = "tool_use" if tool_calls else stop_reason
        self.context_overflow = context_overflow
        self.usage = usage

    def __repr__(self):
        return f"<MockResponse thinking={bool(self.thinking)}, content='{self.content}', tools={bool(self.tool_calls)}>"


# ===== BaseSession =====

class BaseSession:
    def __init__(self, cfg):
        self.api_key = cfg["apikey"]
        self.api_base = cfg["apibase"].rstrip("/")
        self.default_model = cfg.get("model", "")
        self.context_win = cfg.get("context_win", 24000)
        self.system = ""
        self.name = cfg.get("name", self.default_model)
        proxy = cfg.get("proxy")
        self.proxies = {"http": proxy, "https": proxy} if proxy else None
        self.max_retries = max(0, int(cfg.get("max_retries", 2)))
        self.connect_timeout = max(1, int(cfg.get("connect_timeout", 10)))
        self.read_timeout = max(5, int(cfg.get("read_timeout", 300)))
        effort = cfg.get("reasoning_effort")
        effort = None if effort is None else str(effort).strip().lower()
        self.reasoning_effort = (
            effort if effort in ("none", "minimal", "low", "medium", "high", "xhigh") else None
        )
        if effort and not self.reasoning_effort:
            print(f"[WARN] Invalid reasoning_effort {effort!r}, ignored.")
        mode = str(cfg.get("api_mode", "chat_completions")).strip().lower().replace("-", "_")
        self.api_mode = "responses" if mode in ("responses", "response") else "chat_completions"
        self.temperature = cfg.get("temperature")
        self.litellm_kwargs = cfg.get("litellm_kwargs") or {}


# ===== ToolClient =====

class ToolClient:
    def __init__(self, backend, auto_save_tokens=True):
        self.backend = backend
        self.auto_save_tokens = auto_save_tokens
        self.last_tools = ""
        self.name = self.backend.name
        self.total_cd_tokens = 0

    def chat(self, messages, tools=None):
        """
        直接返回 backend 的 generator，不做二次 yield。
        这样 StopIteration.value（MockResponse）能正确传播给 exhaust()。
        """
        # LiteLLMSession.chat() 内部已设置 stream=True，无需传递
        return self.backend.chat(messages, tools=tools)
