# context-manager 压缩质量修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 context-manager 压缩质量缺陷——把 task prompt 丢失的压缩方法论写回（三区逐份处理 + 会话单元 + 旧摘要关联性判断 + 硬约束），引入 `<analysis>` 草稿块让 LLM 一轮做对，配置改 token 绝对值，加输出截断降级保底，删除削弱约束的校验兜底。

**Architecture:** 分三层修复：(1) 配置层 `targetThreshold` 百分比 → `compressTargetTokens` + `maxOutputTokens` 绝对值；(2) Prompt 层模式二/三 task prompt 重写，内联完整方法论 + `<analysis>` 草稿块；(3) 程序保底层 finish_reason 传递链（MockResponse + litellm_adapter + agent_loop + call_subagent）+ 截断降级重压循环（3 次尝试，每次目标降 50%）。解析层新增 `_strip_analysis` 剥离草稿块，删除 update idx 自动补 keep 等校验兜底。

**Tech Stack:** Python 3.11, litellm, 火山方舟 ark-code-latest（doubao-seed-2-0-code，context 256K，单轮输出硬限 128K，平台默认 4K）

---

## File Structure

| 文件 | 职责 | 改动类型 |
|------|------|----------|
| `config/user-config.json` | context 段配置项 | Modify（删 targetThreshold，加 compressTargetTokens + maxOutputTokens）|
| `agent/subagent.py` | 配置读取函数 + call_subagent 截断检测 | Modify |
| `agent/generic/llmcore.py` | MockResponse 加 finish_reason 字段 | Modify |
| `agent/generic/litellm_adapter.py` | 流式循环捕获 finish_reason + 传入 MockResponse | Modify |
| `agent/generic/agent_loop.py` | return_value 加 finish_reason 字段 | Modify |
| `niu_api/compat.py` | 模式二/三 task prompt 重写 + 降级循环 + 删校验兜底 + `_strip_analysis` | Modify |
| `config/agents/context-manager.md` | 术语清理（L0/L1/L2 + 事务→会话单元） | Modify |
| `docs/feature-context-management.md` | L0/L1/L2 术语清理 | Modify |
| `tests/test_compress_quality.py` | 新增测试文件 | Create |

---

## Task 1: 配置层变更 + 读取函数

**Files:**
- Modify: `config/user-config.json`
- Modify: `agent/subagent.py:41-104`（新增两个读取函数）
- Test: `tests/test_compress_quality.py`（新建）

- [ ] **Step 1: 写失败测试 — 配置读取函数**

创建 `tests/test_compress_quality.py`：

```python
"""context-manager 压缩质量修复测试。"""
import json
from pathlib import Path
from unittest.mock import patch

from agent.subagent import (
    _read_compress_target_tokens,
    _read_max_output_tokens,
)


def test_read_compress_target_tokens_default():
    """配置无 compressTargetTokens 时返回默认 60000。"""
    with patch("agent.subagent._get_user_config_path") as mock_path:
        # 指向空配置文件
        tmp = Path("/tmp/test_niu_config_empty.json")
        tmp.write_text(json.dumps({"context": {}}))
        mock_path.return_value = tmp
        assert _read_compress_target_tokens() == 60000
    tmp.unlink()


def test_read_compress_target_tokens_custom():
    """配置有 compressTargetTokens 时返回自定义值。"""
    with patch("agent.subagent._get_user_config_path") as mock_path:
        tmp = Path("/tmp/test_niu_config_custom.json")
        tmp.write_text(json.dumps({"context": {"compressTargetTokens": 80000}}))
        mock_path.return_value = tmp
        assert _read_compress_target_tokens() == 80000
    tmp.unlink()


def test_read_max_output_tokens_default():
    """配置无 maxOutputTokens 时返回默认 16384。"""
    with patch("agent.subagent._get_user_config_path") as mock_path:
        tmp = Path("/tmp/test_niu_config_empty2.json")
        tmp.write_text(json.dumps({"context": {}}))
        mock_path.return_value = tmp
        assert _read_max_output_tokens() == 16384
    tmp.unlink()


def test_read_max_output_tokens_custom():
    """配置有 maxOutputTokens 时返回自定义值。"""
    with patch("agent.subagent._get_user_config_path") as mock_path:
        tmp = Path("/tmp/test_niu_config_custom2.json")
        tmp.write_text(json.dumps({"context": {"maxOutputTokens": 32768}}))
        mock_path.return_value = tmp
        assert _read_max_output_tokens() == 32768
    tmp.unlink()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_compress_quality.py -v`
Expected: FAIL with `ImportError: cannot import name '_read_compress_target_tokens'`

- [ ] **Step 3: 修改 config/user-config.json**

读 `config/user-config.json` 确认 context 段当前内容（应有 `contextWindowSize / warningThreshold / targetThreshold / sleepTriggerMinutes`）。

把 context 段的 `targetThreshold` 删除，新增 `compressTargetTokens` 和 `maxOutputTokens`。保留 `contextWindowSize / warningThreshold / sleepTriggerMinutes` 不变。

改后 context 段应为：
```json
"context": {
  "contextWindowSize": 200000,
  "warningThreshold": 0.8,
  "compressTargetTokens": 60000,
  "maxOutputTokens": 16384,
  "sleepTriggerMinutes": 30
}
```

- [ ] **Step 4: 在 agent/subagent.py 新增读取函数**

读 `agent/subagent.py:41-104` 确认现有 `_read_context_threshold` / `_read_target_threshold` / `_read_context_window_tokens` 模式。

在 `_read_protect_recent_count` 函数之后（约 L104）新增两个函数：

```python
DEFAULT_COMPRESS_TARGET_TOKENS = 60000
DEFAULT_MAX_OUTPUT_TOKENS = 16384


def _read_compress_target_tokens() -> int:
    """Read compressTargetTokens from config/user-config.json. Default 60000."""
    try:
        config_path = _get_user_config_path()
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        val = config.get("context", {}).get("compressTargetTokens", DEFAULT_COMPRESS_TARGET_TOKENS)
        if isinstance(val, (int, float)) and val > 0:
            return int(val)
        logger.warning(f"Invalid compressTargetTokens {val}, using default {DEFAULT_COMPRESS_TARGET_TOKENS}")
    except Exception:
        pass
    return DEFAULT_COMPRESS_TARGET_TOKENS


def _read_max_output_tokens() -> int:
    """Read maxOutputTokens from config/user-config.json. Default 16384."""
    try:
        config_path = _get_user_config_path()
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        val = config.get("context", {}).get("maxOutputTokens", DEFAULT_MAX_OUTPUT_TOKENS)
        if isinstance(val, (int, float)) and val > 0:
            return int(val)
        logger.warning(f"Invalid maxOutputTokens {val}, using default {DEFAULT_MAX_OUTPUT_TOKENS}")
    except Exception:
        pass
    return DEFAULT_MAX_OUTPUT_TOKENS
```

注意：这两个函数读绝对值（不是比例），不能用现有的 `_read_context_threshold`（它校验 0.0<val<1.0）。仿照 `_read_context_window_tokens`（L46-58）的绝对值读取模式。

- [ ] **Step 5: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_compress_quality.py -v`
Expected: 4 个测试 PASS

- [ ] **Step 6: 语法检查**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -c "import ast; ast.parse(open('agent/subagent.py').read())"`
Expected: 无输出（语法 OK）

- [ ] **Step 7: 临时提交**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add config/user-config.json agent/subagent.py tests/test_compress_quality.py
git commit -m "feat(config): add compressTargetTokens + maxOutputTokens config

替换 targetThreshold 百分比为 token 绝对值：
- compressTargetTokens: 压缩后目标 token 数（默认 60000）
- maxOutputTokens: LLM 单轮输出上限（默认 16384，ark-code-latest 硬限 128K）

新增 _read_compress_target_tokens / _read_max_output_tokens 读取函数，
仿 _read_context_window_tokens 绝对值模式（非比例）。"
```

---

## Task 2: `_strip_analysis` 辅助函数

**Files:**
- Modify: `niu_api/compat.py`（新增辅助函数）
- Test: `tests/test_compress_quality.py`

- [ ] **Step 1: 写失败测试 — `_strip_analysis` 各种格式**

在 `tests/test_compress_quality.py` 追加：

```python
from niu_api.compat import _strip_analysis


def test_strip_analysis_closed():
    """闭合的 <analysis>...</analysis> 块被剥离。"""
    raw = "<analysis>\n第一份 idx 1-100\n</analysis>\n\nkeep=1,2,3\nupdate=1|摘要"
    result = _strip_analysis(raw)
    assert "<analysis>" not in result
    assert "keep=1,2,3" in result
    assert "update=1|摘要" in result
    assert "第一份" not in result


def test_strip_analysis_unclosed():
    """未闭合的 <analysis>（有开始无结束）被剥离到字符串末尾。"""
    raw = "<analysis>\n第一份 idx 1-100\nkeep=1,2,3"
    result = _strip_analysis(raw)
    assert "<analysis>" not in result
    assert "keep=1,2,3" not in result  # 未闭合时 keep= 在 analysis 块里被一起剥离


def test_strip_analysis_case_insensitive():
    """大小写不敏感：<ANALYSIS> 也能剥离。"""
    raw = "<ANALYSIS>\n分析内容\n</ANALYSIS>\n\nkeep=1,2,3"
    result = _strip_analysis(raw)
    assert "<ANALYSIS>" not in result.lower()
    assert "keep=1,2,3" in result


def test_strip_analysis_missing():
    """没有 <analysis> 块时原样返回。"""
    raw = "keep=1,2,3\nupdate=1|摘要"
    result = _strip_analysis(raw)
    assert result == raw


def test_strip_analysis_multiline():
    """analysis 块跨多行（含换行）被完整剥离。"""
    raw = """<analysis>
第一份 idx 1-100：含 3 个会话单元
第二份 idx 101-200：估算释放 3K
累计 11K，已达目标
</analysis>

keep=1,5,15,30
update=1|[摘要] 智能家居;5|[摘要] 知识图谱
cursor=30"""
    result = _strip_analysis(raw)
    assert "<analysis>" not in result
    assert "keep=1,5,15,30" in result
    assert "cursor=30" in result
    assert "会话单元" not in result
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_compress_quality.py::test_strip_analysis_closed -v`
Expected: FAIL with `ImportError: cannot import name '_strip_analysis'`

- [ ] **Step 3: 在 niu_api/compat.py 新增 `_strip_analysis`**

读 `niu_api/compat.py` 顶部 import 段确认 `import re` 是否已存在（如果没有，在顶部加 `import re`）。

在 `_build_compress_history` 函数之后（约 L492 附近）新增：

```python
def _strip_analysis(response: str) -> str:
    """剥离 <analysis>...</analysis> 块，只保留 keep/update/cursor 部分。

    处理三种情况：
    1. 闭合的 <analysis>...</analysis>（含跨行）
    2. 未闭合的 <analysis>（有开始无结束，剥离到字符串末尾）
    3. 大小写不敏感（<ANALYSIS> 也识别）
    4. 无 analysis 块时原样返回
    """
    # 先匹配闭合的 <analysis>...</analysis>
    cleaned = re.sub(r'<analysis>.*?</analysis>\s*', '', response, flags=re.DOTALL | re.IGNORECASE)
    # 再处理未闭合的 <analysis>（LLM 写了开始标签但没写结束）
    cleaned = re.sub(r'<analysis>.*$', '', cleaned, flags=re.DOTALL | re.IGNORECASE)
    return cleaned.strip()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_compress_quality.py -v -k strip_analysis`
Expected: 5 个 strip_analysis 测试 PASS

- [ ] **Step 5: 语法检查**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -c "import ast; ast.parse(open('niu_api/compat.py').read())"`
Expected: 无输出

- [ ] **Step 6: 临时提交**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add niu_api/compat.py tests/test_compress_quality.py
git commit -m "feat(compat): add _strip_analysis to剥离 <analysis> 草稿块

为引入 <analysis> 草稿块机制做准备：LLM 单轮内先写 analysis 块（逐区分析
+ 估算释放量 + 判断会话单元），再输出 keep=/update=[/cursor=] 行。
程序解析前用 _strip_analysis 剥离草稿块，只取三行结果。

正则处理闭合/未闭合/大小写三种情况。"
```

---

## Task 3: finish_reason 传递链 — MockResponse 加字段

**Files:**
- Modify: `agent/generic/llmcore.py:27-35`
- Test: `tests/test_compress_quality.py`

- [ ] **Step 1: 写失败测试 — MockResponse 含 finish_reason 字段**

在 `tests/test_compress_quality.py` 追加：

```python
from agent.generic.llmcore import MockResponse


def test_mock_response_has_finish_reason_default():
    """MockResponse 不传 finish_reason 时默认 None。"""
    resp = MockResponse(thinking="", content="hello", tool_calls=[], raw={}, stop_reason="end_turn")
    assert resp.finish_reason is None


def test_mock_response_has_finish_reason_set():
    """MockResponse 传 finish_reason 时能设置。"""
    resp = MockResponse(
        thinking="", content="hello", tool_calls=[], raw={}, stop_reason="end_turn",
        finish_reason="length"
    )
    assert resp.finish_reason == "length"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_compress_quality.py::test_mock_response_has_finish_reason_default -v`
Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'finish_reason'`

- [ ] **Step 3: 修改 MockResponse.__init__ 加 finish_reason 参数**

读 `agent/generic/llmcore.py:27-35` 确认当前 MockResponse 定义。

当前代码（L27-35）：
```python
class MockResponse:
    def __init__(self, thinking, content, tool_calls, raw, stop_reason="end_turn", context_overflow=False, usage=None):
        self.thinking = thinking
        self.content = content
        self.tool_calls = tool_calls
        self.raw = raw
        self.stop_reason = stop_reason
        self.context_overflow = context_overflow
        self.usage = usage
```

改为（在 `usage=None` 后加 `finish_reason=None` 参数，在 `self.usage = usage` 后加 `self.finish_reason = finish_reason`）：
```python
class MockResponse:
    def __init__(self, thinking, content, tool_calls, raw, stop_reason="end_turn", context_overflow=False, usage=None, finish_reason=None):
        self.thinking = thinking
        self.content = content
        self.tool_calls = tool_calls
        self.raw = raw
        self.stop_reason = stop_reason
        self.context_overflow = context_overflow
        self.usage = usage
        self.finish_reason = finish_reason
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_compress_quality.py -v -k mock_response`
Expected: 2 个 mock_response 测试 PASS

- [ ] **Step 5: 语法检查**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -c "import ast; ast.parse(open('agent/generic/llmcore.py').read())"`
Expected: 无输出

- [ ] **Step 6: 临时提交**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add agent/generic/llmcore.py tests/test_compress_quality.py
git commit -m "feat(llmcore): MockResponse 加 finish_reason 字段

为输出截断检测做准备：litellm_adapter 流式循环捕获 chunk 的 finish_reason，
传入 MockResponse，agent_loop 再放进 return_value，call_subagent 检测
finish_reason=='length' 触发降级重压。

默认 None，不影响现有调用。"
```

---

## Task 4: finish_reason 传递链 — litellm_adapter 流式捕获

**Files:**
- Modify: `agent/generic/litellm_adapter.py:427-481`（流式循环加捕获）
- Modify: `agent/generic/litellm_adapter.py:578-583`（MockResponse 构造传 finish_reason）

- [ ] **Step 1: 读 litellm_adapter.py 确认流式循环和 MockResponse 构造**

读 `agent/generic/litellm_adapter.py:425-485` 确认流式循环结构。
读 `agent/generic/litellm_adapter.py:575-590` 确认 MockResponse 构造代码。

- [ ] **Step 2: 在流式循环前初始化 last_finish_reason**

在 `for chunk in response:` 之前（约 L433，`usage = None` 同位置）新增：

```python
        usage = None
        last_finish_reason = None  # 新增：捕获流式最后一个非空 finish_reason
```

- [ ] **Step 3: 在流式循环里捕获 finish_reason**

在 `for chunk in response:` 循环内（L434-481），读完 `delta.content` / `delta.reasoning_content` / `delta.tool_calls` / `chunk.usage` 之后，循环末尾新增：

```python
            # 捕获 finish_reason（最后一个非空的覆盖）
            try:
                if chunk.choices and chunk.choices[0].finish_reason:
                    last_finish_reason = chunk.choices[0].finish_reason
            except (AttributeError, IndexError):
                pass
```

放置位置：在 `chunk.usage` 读取之后（L480-481 附近），循环体的最后。

- [ ] **Step 4: 在 MockResponse 构造时传入 finish_reason**

读 L578-583 的 MockResponse 构造代码（正常路径主构造点）。

当前代码（L578-583，只传 4 个参数，usage 在 L585-590 后赋值）：
```python
        mock_resp = MockResponse(
            thinking=reasoning_content,
            content=full_content,
            tool_calls=tool_calls,
            raw=full_content,
        )

        if usage:
            mock_resp.usage = {...}
```

改为（加 `finish_reason=last_finish_reason or "stop"` 参数）：
```python
        mock_resp = MockResponse(
            thinking=reasoning_content,
            content=full_content,
            tool_calls=tool_calls,
            raw=full_content,
            finish_reason=last_finish_reason or "stop",
        )

        if usage:
            mock_resp.usage = {...}
```

注意：保留原有 4 个参数不变，只新增 `finish_reason` 关键字参数。usage 仍在构造后赋值（L585-590 不动）。

另外读 L529-536 的 context_overflow 分支（流中检测到 overflow 时提前构造 MockResponse），也要加 `finish_reason` 参数（否则 overflow 时无 finish_reason）。读实际代码确认该分支的 MockResponse 构造，加 `finish_reason=last_finish_reason or "stop"`。

- [ ] **Step 5: 写行为测试 — finish_reason 从流式 chunk 流到 MockResponse**

在 `tests/test_compress_quality.py` 追加（用 fake streamable response 驱动 LiteLLMSession，验证 finish_reason 真实流通）：

```python
def test_litellm_adapter_finish_reason_from_stream(monkeypatch):
    """litellm_adapter 流式循环应捕获最后一个 chunk 的 finish_reason 传入 MockResponse。"""
    from agent.generic.litellm_adapter import LiteLLMSession
    from agent.generic.llmcore import MockResponse
    from types import SimpleNamespace

    # 构造 fake chunk 流：3 个 chunk，最后一个 finish_reason='length'
    def make_chunk(content=None, finish_reason=None):
        delta = SimpleNamespace(content=content, reasoning_content=None, tool_calls=None)
        return SimpleNamespace(
            choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)],
            usage=None,
        )

    fake_chunks = [
        make_chunk(content="hello"),
        make_chunk(content=" world"),
        make_chunk(finish_reason="length"),  # 最后一个 chunk 带 finish_reason
    ]

    # mock litellm.completion 返回 fake_chunks 迭代器
    import litellm
    monkeypatch.setattr(litellm, "completion", lambda **kwargs: iter(fake_chunks))

    session = LiteLLMSession(
        default_model="test-model",
        api_base="http://test",
        api_key="test",
        read_timeout=30,
    )
    messages = [{"role": "user", "content": "test"}]
    gen = session.chat(messages=messages, tools=None)
    # 消费生成器拿 MockResponse（通过 StopIteration.value）
    result = None
    try:
        while True:
            next(gen)
    except StopIteration as e:
        result = e.value

    assert result is not None
    assert isinstance(result, MockResponse)
    assert result.finish_reason == "length"
    assert result.content == "hello world"
```

- [ ] **Step 6: 运行行为测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_compress_quality.py::test_litellm_adapter_finish_reason_from_stream -v`
Expected: PASS（finish_reason='length' 被正确捕获）

如果测试因 LiteLLMSession 初始化参数不对而失败，读 `agent/generic/litellm_adapter.py` 的 `LiteLLMSession.__init__` 确认必需参数，调整测试。

- [ ] **Step 7: 语法检查**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -c "import ast; ast.parse(open('agent/generic/litellm_adapter.py').read())"`
Expected: 无输出

- [ ] **Step 8: 验证 MockResponse 构造不报错**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -c "from agent.generic.litellm_adapter import LiteLLMSession; print('OK')"`
Expected: 输出 `OK`（import 不报错）

- [ ] **Step 9: 运行现有测试不破坏**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_compress_quality.py tests/test_compress_history.py -v 2>&1 | tail -20`
Expected: 无新增 FAIL

- [ ] **Step 10: 临时提交**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add agent/generic/litellm_adapter.py
git commit -m "feat(litellm_adapter): 流式循环捕获 finish_reason 传入 MockResponse

在 for chunk in response 循环里捕获 chunk.choices[0].finish_reason
（最后一个非空覆盖），构造 MockResponse 时传入。

为输出截断检测提供数据：finish_reason=='length' 表示 LLM 输出被截断。"
```

---

## Task 5: finish_reason 传递链 — agent_loop return_value

**Files:**
- Modify: `agent/generic/agent_loop.py:570`（CURRENT_TASK_DONE 纯文本路径 1）
- Modify: `agent/generic/agent_loop.py:583`（CURRENT_TASK_DONE 纯文本路径 2）

- [ ] **Step 1: 读 agent_loop.py 确认 return_value 构造点**

读 `agent/generic/agent_loop.py:565-590` 确认 L570 和 L583 两处 CURRENT_TASK_DONE 的 return dict。

- [ ] **Step 2: 在 L570 return dict 加 finish_reason**

当前 L570 附近（纯文本回复退出，next_prompts 为空）：
```python
        if not next_prompts:
            return {
                "result": "CURRENT_TASK_DONE",
                "data": {
                    "messages": [...],
                },
            }
```

改为（加 `"finish_reason": response.finish_reason if response else None`）：
```python
        if not next_prompts:
            return {
                "result": "CURRENT_TASK_DONE",
                "data": {
                    "messages": [...],
                },
                "finish_reason": response.finish_reason if response else None,
            }
```

注意：保留原有 `"data"` 结构不变，只加 `finish_reason` 键。读实际代码确认 `response` 变量在该路径可访问（L331/L336 赋值后作用域内可见）。

- [ ] **Step 3: 在 L583 return dict 加 finish_reason**

当前 L583 附近（纯文本回复退出，not response.tool_calls）：
```python
        if not response.tool_calls:
            return {
                "result": "CURRENT_TASK_DONE",
                "data": {
                    "messages": [...],
                },
            }
```

改为：
```python
        if not response.tool_calls:
            return {
                "result": "CURRENT_TASK_DONE",
                "data": {
                    "messages": [...],
                },
                "finish_reason": response.finish_reason,
            }
```

- [ ] **Step 4: 写行为测试 — agent_runner_loop return_value 含 finish_reason**

在 `tests/test_compress_quality.py` 追加（mock LLM 返回带 finish_reason 的 MockResponse，验证 return_value 真实携带）：

```python
def test_agent_loop_return_value_contains_finish_reason(monkeypatch):
    """agent_runner_loop 正常完成时 return_value 应含 response 的 finish_reason。"""
    from agent.generic import agent_loop
    from agent.generic.llmcore import MockResponse

    # mock LLM 客户端：返回 finish_reason='length' 的 MockResponse
    def fake_chat(self, messages, tools=None, response_format=None):
        resp = MockResponse(
            thinking="",
            content="keep=1,2,3\nupdate=",
            tool_calls=[],
            raw="keep=1,2,3",
            finish_reason="length",
        )
        # chat 是 generator，yield content chunks 然后 StopIteration 返回 resp
        yield "keep=1,2,3\nupdate="
        return resp

    # mock 依赖避免真实初始化
    monkeypatch.setattr(agent_loop, "is_stop_requested", lambda: False)

    # 构造最小 client mock
    class FakeClient:
        def chat(self, messages, tools=None, response_format=None):
            return fake_chat(self, messages, tools, response_format)

    gen = agent_loop.agent_runner_loop(
        client=FakeClient(),
        system_prompt="test",
        user_input="test",
        handler=None,
        tools_schema=[],
        max_turns=1,
        initial_user_content="test",
    )

    result_text = ""
    return_value = None
    try:
        while True:
            chunk = next(gen)
            if isinstance(chunk, str):
                result_text += chunk
    except StopIteration as e:
        return_value = e.value

    assert return_value is not None
    assert isinstance(return_value, dict)
    assert return_value.get("result") == "CURRENT_TASK_DONE"
    assert return_value.get("finish_reason") == "length"
```

注意：如果 `agent_runner_loop` 的实际签名或 `client.chat` 调用方式跟测试不匹配，读 `agent/generic/agent_loop.py` 确认参数名和调用约定，调整测试。关键断言是 `return_value.get("finish_reason") == "length"`。

- [ ] **Step 5: 运行行为测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_compress_quality.py::test_agent_loop_return_value_contains_finish_reason -v`
Expected: PASS（return_value 含 finish_reason='length'）

如果测试因 agent_runner_loop 签名复杂而难以构造，可以简化：直接 mock `_run_agent_loop` 内部的 LLM 调用，或读现有测试文件找 agent_runner_loop 的测试模式参考。

- [ ] **Step 6: 语法检查**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -c "import ast; ast.parse(open('agent/generic/agent_loop.py').read())"`
Expected: 无输出

- [ ] **Step 7: 验证 import 不报错**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -c "from agent.generic.agent_loop import agent_runner_loop; print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 6: 运行现有测试不破坏**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_compress_quality.py tests/test_compress_history.py -v 2>&1 | tail -20`
Expected: 无新增 FAIL

- [ ] **Step 7: 临时提交**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add agent/generic/agent_loop.py
git commit -m "feat(agent_loop): return_value 加 finish_reason 字段

CURRENT_TASK_DONE 路径（L570 next_prompts 为空 / L583 无 tool_calls）
的 return dict 加 finish_reason 字段，从 response.finish_reason 取值。

context-manager 禁工具模式实际触发的就是这两条路径。call_subagent
将读取 finish_reason 检测输出截断。

MAX_TURNS_EXCEEDED / STOPPED 路径不带 finish_reason（非正常完成）。"
```

---

## Task 6: finish_reason 传递链 — call_subagent 检测截断

**Files:**
- Modify: `agent/subagent.py:501-535`（call_subagent 加截断检测）

- [ ] **Step 1: 写失败测试 — call_subagent 检测截断返回 COMPACT_TRUNCATED**

在 `tests/test_compress_quality.py` 追加。

**注意 mock 边界**：Task 6 的核心改动是"call_subagent 检测 return_value 的 finish_reason"。单元测试 mock `_run_agent_loop` 是合理的（finish_reason 从 litellm→MockResponse→return_value 的真实流通已由 Task 4/5 的行为测试覆盖）。但 `create_client` / `get_tools_schema` / `NiuHandler` 不能 mock 成 None（会导致 AttributeError），需要 mock 成能被 call_subagent 安全调用的 fake 对象。

```python
def test_call_subagent_detects_truncation(monkeypatch):
    """call_subagent 检测 finish_reason=='length' 时返回 'COMPACT_TRUNCATED'。"""
    from agent import subagent

    # mock _run_agent_loop 返回 finish_reason='length' 的 return_value
    def fake_run_agent_loop(**kwargs):
        return "部分输出...", {"result": "CURRENT_TASK_DONE", "data": {}, "finish_reason": "length"}

    monkeypatch.setattr(subagent, "_run_agent_loop", fake_run_agent_loop)

    # mock create_client / get_tools_schema / NiuHandler 为安全 fake（非 None）
    class FakeClient:
        pass
    monkeypatch.setattr(subagent, "create_client", lambda cfg: FakeClient())
    monkeypatch.setattr(subagent, "get_tools_schema", lambda: [])
    # NiuHandler 需要支持 _disable_memory_recall / _is_subagent 属性赋值
    class FakeHandler:
        def __init__(self, mcp_client=None):
            self._disable_memory_recall = False
            self._is_subagent = False
    monkeypatch.setattr(subagent, "NiuHandler", FakeHandler)

    result = subagent.call_subagent(
        agent_name="context-manager",
        task="test",
        llm_config={"model": "test"},
    )
    assert result == "COMPACT_TRUNCATED"


def test_call_subagent_normal_return(monkeypatch):
    """call_subagent 正常完成时返回 result_text。"""
    from agent import subagent

    def fake_run_agent_loop(**kwargs):
        return "keep=1,2,3\nupdate=", {"result": "CURRENT_TASK_DONE", "data": {}, "finish_reason": "stop"}

    monkeypatch.setattr(subagent, "_run_agent_loop", fake_run_agent_loop)

    class FakeClient:
        pass
    monkeypatch.setattr(subagent, "create_client", lambda cfg: FakeClient())
    monkeypatch.setattr(subagent, "get_tools_schema", lambda: [])
    class FakeHandler:
        def __init__(self, mcp_client=None):
            self._disable_memory_recall = False
            self._is_subagent = False
    monkeypatch.setattr(subagent, "NiuHandler", FakeHandler)

    result = subagent.call_subagent(
        agent_name="context-manager",
        task="test",
        llm_config={"model": "test"},
    )
    assert "keep=1,2,3" in result
```

**说明**：这两个测试是单元测试，验证 call_subagent 的 if 检测逻辑。finish_reason 从 litellm chunk 到 return_value 的真实传递由 Task 4（litellm_adapter 行为测试）和 Task 5（agent_loop return_value 行为测试）覆盖。Task 6 在此基础上验证"检测到 finish_reason='length' 时返回 COMPACT_TRUNCATED 字符串"。

- [ ] **Step 2: 运行测试确认失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_compress_quality.py::test_call_subagent_detects_truncation -v`
Expected: FAIL（call_subagent 当前不检测 finish_reason，返回的是 result_text 而非 "COMPACT_TRUNCATED"）

- [ ] **Step 3: 在 call_subagent 加截断检测**

读 `agent/subagent.py:501-535` 确认 `_run_agent_loop` 调用后的逻辑。

当前代码（L501-535 概要）：
```python
    result_text, return_value = _run_agent_loop(
        client=client,
        ...
    )

    # CONTEXT_OVERFLOW：返回结构化进度报告
    if return_value and isinstance(return_value, dict) and return_value.get("result") == "CONTEXT_OVERFLOW":
        ...
        return json.dumps(overflow_report, ensure_ascii=False)

    # 优先从 return 值提取结构化结果
    extracted = _extract_result_from_return_value(return_value)
    if extracted is not None:
        return extracted

    return result_text
```

在 `_run_agent_loop` 调用之后、`CONTEXT_OVERFLOW` 检测之前（约 L516）新增截断检测：

```python
    result_text, return_value = _run_agent_loop(
        client=client,
        ...
    )

    # 检测输出截断（finish_reason == "length"）
    if return_value and isinstance(return_value, dict):
        if return_value.get("finish_reason") == "length":
            logger.warning(f"[SubAgent] {agent_name}: Output truncated (finish_reason=length)")
            return "COMPACT_TRUNCATED"

    # CONTEXT_OVERFLOW：返回结构化进度报告
    if return_value and isinstance(return_value, dict) and return_value.get("result") == "CONTEXT_OVERFLOW":
        ...
```

注意：保留原有 `CONTEXT_OVERFLOW` 检测和后续逻辑不变，只插入截断检测块。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_compress_quality.py -v -k call_subagent`
Expected: 2 个 call_subagent 测试 PASS

- [ ] **Step 5: 语法检查**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -c "import ast; ast.parse(open('agent/subagent.py').read())"`
Expected: 无输出

- [ ] **Step 6: 运行现有测试不破坏**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_compress_quality.py tests/test_compress_history.py -v 2>&1 | tail -20`
Expected: 无新增 FAIL

- [ ] **Step 7: 临时提交**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add agent/subagent.py tests/test_compress_quality.py
git commit -m "feat(subagent): call_subagent 检测 finish_reason=='length' 返回 COMPACT_TRUNCATED

在 _run_agent_loop 返回后、CONTEXT_OVERFLOW 检测前，检测 return_value
的 finish_reason。=='length' 表示 LLM 输出被截断，返回字符串
'COMPACT_TRUNCATED' 让 compat.py 降级循环识别。

用字符串而非异常，避免改 call_subagent 返回签名。"
```

---

## Task 7: 模式二 task prompt 重写 + 降级重压循环

**Files:**
- Modify: `niu_api/compat.py:1860-1910`（task prompt 重写 + call_subagent 加降级循环 + llm_config 注入 max_tokens）

- [ ] **Step 1: 读模式二 task prompt 和 run_context_manager_mode2 现状**

读 `niu_api/compat.py:1860-1910` 确认当前 task prompt 和 `run_context_manager_mode2` 函数。

- [ ] **Step 2: 新增 _build_mode2_prompt 函数 + 重写 prompt**

把模式二 task prompt 构造抽成函数 `_build_mode2_prompt`（放在 `_strip_analysis` 函数之后，约 L495 附近）。这个函数返回完整的 prompt 字符串，接收 `display_tokens` / `_compress_target_tokens` / `usage_percent` / `compress_history` 参数。

函数内容（返回 Step 2 描述的 prompt 字符串）：

```python
def _build_mode2_prompt(display_tokens: int, compress_target_tokens: int, usage_percent: float, compress_history: list) -> str:
    """构造模式二 task prompt（含压缩方法论 + analysis 草稿块）。

    抽成函数是因为降级重压循环每次 _compress_target_tokens 变了要重新构造 prompt
    （f-string 含 {_compress_target_tokens}，需要重新求值）。
    """
    return f"""CRITICAL: 你只有一轮机会完成压缩决策。禁止调用任何工具。
- 不调用 write、delete_messages、update_message、bash 等
- 你的回复必须包含 <analysis> 块和 keep=/update= 两行
- 调用工具会被拒绝，浪费唯一一轮，任务失败

先在 <analysis> 块里写分析过程，然后输出 keep=/update= 两行。

<analysis> 块内容：
- 列出三份的 idx 范围
- 估算每份删工具输出 + 合并会话单元能释放多少 token
- 判断第一份的旧摘要与近期工作的关联性
- 决定每份的处理强度

输出格式：
keep=1,3,5-10,15
update=2|[摘要] 摘要内容;11|[摘要] 摘要内容

说明：
- keep= 保留的消息 idx（逗号分隔，连续用短横线如 5-10）
- update= 需压缩为摘要的消息（idx|摘要内容，多条用分号分隔）
- update 的 idx 必须在 keep 中（update 的消息保留但 content 改为摘要）
- 未列在 keep 中的消息将被删除

示例：
<analysis>
第一份 idx 1-100：含 3 个会话单元（智能家居调试/知识图谱/周报），旧摘要 5 条
其中 2 条与近期无关可删，估算释放 8K tokens
第二份 idx 101-200：估算释放 3K tokens
累计 11K，已达目标 10K，第三份轻度处理
</analysis>

keep=1,5,15,30,50,75,100,105,115,150,180,200
update=1|[摘要] 智能家居调试 → 完成 | 微波炉/空调测试;5|[摘要] ...

压缩方法论（必须在一轮内完成，禁止多轮）：

1. 估算：当前 {display_tokens} tokens，目标 {compress_target_tokens} tokens，
   需释放 {display_tokens - compress_target_tokens} tokens。

2. 划分优先级（按 idx 范围，粗粒度）：
   - 第一份（最早）：idx 最小的约 1/3 范围
   - 第二份（中间）：中间约 1/3 范围
   - 第三份（最近）：idx 最大的约 1/3 范围
   注：划分是优先级提示，实际处理按会话单元边界，
   不得切断一个完整的会话单元（单元跨越划分边界时，
   整个单元归入更早的那份）。

3. 逐份处理（在 analysis 块里思考，一次输出结果）：
   a. 第一份（最早）最激进：
      - role=tool 的工具输出：全删（不进 keep）
      - 原始对话：按会话单元（2-15 条一个话题）合并，
        每个会话单元保留 1 条（锚 idx），content 改为摘要，其余删除
      - 旧摘要（已是 [摘要] 开头）：判断与近期工作的关联性，
        无关的直接删除，相关的保留
   b. 估算累计释放量。若已达目标，第二份/第三份按"轻度处理"
      （仅删工具输出、保留原文）即可。
   c. 若未达目标，处理第二份（中间）：
      - role=tool 工具输出：全删
      - 对话：按会话单元合并为摘要
      - 已有摘要：保留不动（禁止二次压缩）
   d. 再估算。若仍未达目标，处理第三份（最近）：
      - role=tool 工具输出：全删
      - 对话：仅精简超长内容，优先保留原文
   e. 若三份处理完仍未达目标，接受当前结果（受保护消息已排除）

4. 硬约束：
   - 每个会话单元至少保留 1 条（不得把多个会话单元合并成 1 条）
   - 摘要长度 ≤ 150 字符，不得低于 50 字符
   - 已是摘要（≤50 字符且信息密度高）不再二次压缩
   - update 的 idx 必须在 keep 中
   - 摘要格式：[摘要] <用户意图> → <执行结果> | <关键细节>

当前上下文状态：
- 参与压缩的消息数：{len(compress_history)}（受保护消息已排除）
- 当前 token 总数：{display_tokens}（{usage_percent:.1f}%）
- 目标 token 总数：{compress_target_tokens}
- 需释放至少 {display_tokens - compress_target_tokens} tokens

上方历史消息每条开头带 [idx:N] Ntokens 前缀，共 {len(compress_history)} 条。
role=tool 的工具输出会被程序自动删除，不需要放入 keep。

REMINDER: 禁止调用任何工具，直接在回复中输出 <analysis> 块和 keep=/update= 两行。"""
```

注意：
- 函数参数名用 `compress_target_tokens`（不带下划线前缀，函数内局部变量）
- 调用处（Step 3 降级循环）传 `_compress_target_tokens`（带下划线的循环变量）作为参数
- 原模式二分支 L1860-1882 的内联 prompt 构造删除，改为调用 `_build_mode2_prompt(...)`

- [ ] **Step 3: 改造 run_context_manager_mode2 加降级循环 + max_tokens 注入**

读 `niu_api/compat.py:1900-1910` 确认 `run_context_manager_mode2` 和 `await asyncio.to_thread(run_context_manager_mode2)` 的现状。

当前代码大致：
```python
            def run_context_manager_mode2():
                return call_subagent(
                    agent_name="context-manager",
                    task=prompt,
                    llm_config=llm_config,
                    mcp_client=None,
                    context_fifo_threshold=0,
                    history=compress_history,
                )
```

改为（加降级循环 + llm_config 注入 max_tokens + 截断检测）：

```python
            # llm_config 动态注入 max_tokens（通过 litellm_kwargs）
            llm_config_with_max = dict(llm_config)
            llm_config_with_max["litellm_kwargs"] = {
                **llm_config.get("litellm_kwargs", {}),
                "max_tokens": _read_max_output_tokens(),
            }

            # 降级重压循环：最多 3 次尝试，每次压缩目标降 50%
            _compress_target_tokens = _read_compress_target_tokens()
            compress_result = None
            for attempt in range(3):
                if attempt > 0:
                    _compress_target_tokens = int(_compress_target_tokens * 0.5)
                    logger.warning(f"[Compact] Mode-2 truncated, lowering target to {_compress_target_tokens}, attempt {attempt+1}")

                # 每次循环重新构造 prompt（_compress_target_tokens 变了，f-string 要重新求值）
                # base_prompt 是不含缩短提示的原始 prompt，attempt>0 时追加缩短提示
                prompt = _build_mode2_prompt(display_tokens, _compress_target_tokens, usage_percent, compress_history)
                if attempt > 0:
                    prompt = prompt + "\n\n注意：上次输出被截断，请精简 <analysis> 块，只保留关键决策依据，不要逐条分析每条消息。"

                def run_context_manager_mode2():
                    return call_subagent(
                        agent_name="context-manager",
                        task=prompt,
                        llm_config=llm_config_with_max,
                        mcp_client=None,
                        context_fifo_threshold=0,
                        history=compress_history,
                    )

                compress_result = await asyncio.to_thread(run_context_manager_mode2)

                if is_stop_requested():
                    logger.warning("[Tidy] Stop requested, aborting tidy pipeline")
                    clear_stop()
                    return {"status": "aborted", "message": "Stopped by user"}

                if compress_result == "COMPACT_TRUNCATED":
                    if attempt < 2:
                        continue  # 降级重试
                    else:
                        logger.error("[Compact] Mode-2 all 3 attempts truncated, giving up")
                        return {"status": "skipped", "mode": "sleep", "reason": "all attempts truncated"}

                # 正常返回，跳出循环进入解析
                logger.info(f"[Tidy] Mode-2: context-manager completed, length={len(compress_result)}")
                break

            # 剥离 <analysis> 草稿块（在解析前）
            compress_result = _strip_analysis(compress_result)

            # 后续解析逻辑（原有代码 L1917+，用 compress_result 变量）
            # 从 LLM content 解析序号格式压缩方案
            _idx_to_id: dict[int, str] = {}
            ...
```

**关键修正点**：

1. **变量名用 `compress_result`**（与下游 L1915/L1924 的 `compress_result.splitlines()` 一致），不用 `result`。降级循环给 `compress_result` 赋值。

2. **prompt 在循环内重新构造**：因为 prompt 是 f-string 含 `{_compress_target_tokens}`，降级时值变了必须重新构造。把 prompt 构造抽成函数 `_build_mode2_prompt(display_tokens, _compress_target_tokens, usage_percent, compress_history)`，Step 2 的 prompt 内容移到这个函数里返回。这样循环内每次调用都重新求值 f-string。

3. **缩短提示只追加一次**：`if attempt > 0: prompt = prompt + 提示`——因为 prompt 每次从 `_build_mode2_prompt` 重新构造（不含提示），所以追加不会累积。

4. **3 次都截断返回 dict**：`return {"status": "skipped", "mode": "sleep", "reason": "all attempts truncated"}`——与函数其他 return（如 L1914 `{"status": "aborted", ...}` 和末尾 `{"status": "ok", ...}`）一致，避免返回 None 导致调用方 KeyError。

5. **`is_stop_requested` 检查**：保留原有的停止检查（L1911-1914），放在每次 `compress_result` 赋值之后。

6. **`_strip_analysis` 在循环外调用**：循环 break 后 `compress_result` 是正常返回值，剥离 analysis 再接原有解析。

读实际代码确认 L1911-1915 的 `is_stop_requested` / `logger.info` 位置，把降级循环嵌入时保留这些检查。

- [ ] **Step 4: 在 compat.py 顶部 import 新增读取函数**

读 `niu_api/compat.py:1-20` 确认现有 `from agent.subagent import` 行。

在现有的 `from agent.subagent import ...` 行加两个新函数：

```python
from agent.subagent import (
    ...,
    _read_compress_target_tokens,
    _read_max_output_tokens,
)
```

- [ ] **Step 5: 确认 _strip_analysis 已在降级循环后调用**

Step 3 的降级循环代码里已经包含 `compress_result = _strip_analysis(compress_result)`（在 break 之后、解析之前）。确认这一行存在，且变量名用 `compress_result`（与下游 L1924 的 `compress_result.splitlines()` 一致）。

读改造后的代码确认：
- 降级循环 `break` 后 `compress_result` 是正常返回值（非 "COMPACT_TRUNCATED"）
- `compress_result = _strip_analysis(compress_result)` 剥离 analysis 块
- 下游 `for line in compress_result.splitlines():` 解析 keep/update（原有代码 L1924，变量名不变）

**不要**用 `result` 变量名——模式二下游解析用的是 `compress_result`，改名会导致 L1915/L1924 引用断裂。

- [ ] **Step 6: 语法检查**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -c "import ast; ast.parse(open('niu_api/compat.py').read())"`
Expected: 无输出

- [ ] **Step 7: 写集成测试 — 模式二 prompt 含方法论 + 降级循环**

在 `tests/test_compress_quality.py` 追加（参考现有 `test_mode2_passes_history_to_call_subagent` 的 monkeypatch 模式）：

```python
def test_mode2_prompt_contains_methodology(monkeypatch):
    """模式二 task prompt 应含压缩方法论（三份/会话单元/硬约束）。"""
    import asyncio
    import niu_api.compat as compat
    import niu_api.chat as chat_module
    import agent.subagent as subagent_module

    messages = [
        FakeMsg(id="msg-1", role="user", content="你好"),
        FakeMsg(id="msg-2", role="assistant", content="你好，我是 Niu"),
    ]

    class FakeStore:
        async def get_messages(self, limit=None, before_id=None):
            return messages

    async def fake_get_message_store():
        return FakeStore()

    class FakeRunner:
        handler = type("H", (), {"_last_prompt_tokens": 180000})()
        llm_config = {}

    def fake_get_or_create_runner():
        return FakeRunner()

    captured = {}
    def fake_call_subagent(*args, **kwargs):
        agent_name = kwargs.get("agent_name") or (args[0] if args else "")
        if agent_name == "context-manager":
            captured["task"] = kwargs.get("task", "")
            captured["history"] = kwargs.get("history")
            captured["llm_config"] = kwargs.get("llm_config", {})
            return "<analysis>分析</analysis>\nkeep=1,2\nupdate="
        return "skip"

    monkeypatch.setattr(compat, "get_message_store", fake_get_message_store)
    monkeypatch.setattr(chat_module, "get_or_create_runner", fake_get_or_create_runner)
    monkeypatch.setattr(subagent_module, "call_subagent", fake_call_subagent)
    monkeypatch.setattr(compat, "_read_context_window_tokens", lambda: 200000, raising=False)
    monkeypatch.setattr(compat, "_read_warning_threshold", lambda: 0.8, raising=False)
    monkeypatch.setattr(compat, "_read_protect_recent_count", lambda: 0, raising=False)
    monkeypatch.setattr(compat, "_write_cursor_with_lock", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(compat, "_read_compress_target_tokens", lambda: 60000, raising=False)
    monkeypatch.setattr(compat, "_read_max_output_tokens", lambda: 16384, raising=False)

    request = {"session_id": "test", "mode": "sleep"}
    # 不用 try/except 吞异常——让真实错误透出，便于发现 prompt 构造或解析的问题
    asyncio.run(compat._tidy_context_impl(request))

    # 验证 call_subagent 被调用且捕获了参数
    assert "task" in captured, "call_subagent 未被调用，可能 _tidy_context_impl 提前返回或抛错"
    # prompt 含方法论关键词
    assert "压缩方法论" in captured["task"]
    assert "第一份" in captured["task"]
    assert "会话单元" in captured["task"]
    assert "<analysis>" in captured["task"]
    # llm_config 注入了 max_tokens
    assert captured["llm_config"].get("litellm_kwargs", {}).get("max_tokens") == 16384
```

**注意**：
- `FakeMsg` 从 `tests/test_compress_history.py` 导入（`from tests.test_compress_history import FakeMsg`）或在测试文件顶部定义（参考 test_compress_history.py L13-20 的定义）
- 不用 `try/except: pass` 吞异常——如果 `_tidy_context_impl` 抛错，测试应失败而非静默通过
- 不 mock `_read_target_threshold`（Task 1 已删除该配置，模式二不再用百分比阈值）

- [ ] **Step 8: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_compress_quality.py::test_mode2_prompt_contains_methodology -v`
Expected: PASS

- [ ] **Step 9: 运行现有测试不破坏**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_compress_quality.py tests/test_compress_history.py -v 2>&1 | tail -30`
Expected: 无新增 FAIL

- [ ] **Step 10: 临时提交**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add niu_api/compat.py tests/test_compress_quality.py
git commit -m "feat(compat): 模式二 task prompt 写回完整方法论 + 降级重压循环

task prompt 重写：内联压缩方法论（三份逐份处理 + 会话单元 + 旧摘要关联性
判断 + 硬约束），引入 <analysis> 草稿块让 LLM 单轮内先分析再输出。

降级重压循环：最多 3 次尝试，每次压缩目标降 50%。截断时 prompt 追加
'缩短 analysis' 提示。3 次都截断放弃压缩。

max_tokens 通过 llm_config[litellm_kwargs] 动态注入，不改 call_subagent
签名。解析前用 _strip_analysis 剥离草稿块。"
```

---

## Task 8: 模式三 task prompt 重写 + 降级重压循环

**Files:**
- Modify: `niu_api/compat.py:2511-2563`（模式三 task prompt + run_context_manager_force）

- [ ] **Step 1: 读模式三 task prompt 和 run_context_manager_force 现状**

读 `niu_api/compat.py:2511-2563` 确认当前 task prompt 和 `run_context_manager_force` 函数。

- [ ] **Step 2: 新增 _build_force_prompt 函数 + 重写 prompt**

把模式三 task prompt 构造抽成函数 `_build_force_prompt`（放在 `_build_mode2_prompt` 之后，约 L500 附近）。接收 `display_tokens` / `compress_target_tokens` / `usage_percent` / `_force_history` / `last_compress_id` / `_dream_idx_in_force` 参数。

函数内容（返回 Step 2 描述的 prompt 字符串，模式三比模式二多 cursor 行 + dream 安全边界）：

```python
def _build_force_prompt(display_tokens: int, compress_target_tokens: int, usage_percent: float,
                        force_history: list, last_compress_id: str | None, dream_idx_in_force: int) -> str:
    """构造模式三 force task prompt（含压缩方法论 + analysis 草稿块 + cursor + dream 安全边界）。

    抽成函数是因为降级重压循环每次 compress_target_tokens 变了要重新构造 prompt。
    """
    return f"""CRITICAL: 你只有一轮机会完成压缩决策。禁止调用任何工具。
- 不调用 write、delete_messages、update_message、bash 等
- 你的回复必须包含 <analysis> 块和 keep=/update=/cursor= 三行
- 调用工具会被拒绝，浪费唯一一轮，任务失败

先在 <analysis> 块里写分析过程，然后输出 keep=/update=/cursor= 三行。

<analysis> 块内容：
- 列出三份的 idx 范围
- 估算每份删工具输出 + 合并会话单元能释放多少 token
- 判断第一份的旧摘要与近期工作的关联性
- 决定每份的处理强度

输出格式：
keep=1,3,5-10,15
update=2|[摘要] 摘要内容;11|[摘要] 摘要内容
cursor=15

说明：
- keep= 保留的消息 idx（逗号分隔，连续用短横线如 5-10）
- update= 需压缩为摘要的消息（idx|摘要内容，多条用分号分隔）
- update 的 idx 必须在 keep 中（update 的消息保留但 content 改为摘要）
- cursor= 操作范围内 idx 最大且仍存在的消息 idx
- 未列在 keep 中的消息将被删除

示例：
<analysis>
第一份 idx 1-100：含 3 个会话单元（智能家居调试/知识图谱/周报），旧摘要 5 条
其中 2 条与近期无关可删，估算释放 8K tokens
第二份 idx 101-200：估算释放 3K tokens
累计 11K，已达目标 10K，第三份轻度处理
</analysis>

keep=1,5,15,30,50,75,100,105,115,150,180,200
update=1|[摘要] 智能家居调试 → 完成 | 微波炉/空调测试;5|[摘要] ...
cursor=200

压缩方法论（必须在一轮内完成，禁止多轮）：

1. 估算：当前 {display_tokens} tokens，目标 {compress_target_tokens} tokens，
   需释放 {display_tokens - compress_target_tokens} tokens。

2. 划分优先级（按 idx 范围，粗粒度）：
   - 第一份（最早）：idx 最小的约 1/3 范围
   - 第二份（中间）：中间约 1/3 范围
   - 第三份（最近）：idx 最大的约 1/3 范围
   注：划分是优先级提示，实际处理按会话单元边界，
   不得切断一个完整的会话单元（单元跨越划分边界时，
   整个单元归入更早的那份）。

3. 逐份处理（在 analysis 块里思考，一次输出结果）：
   a. 第一份（最早）最激进：
      - role=tool 的工具输出：全删（不进 keep）
      - 原始对话：按会话单元（2-15 条一个话题）合并，
        每个会话单元保留 1 条（锚 idx），content 改为摘要，其余删除
      - 旧摘要（已是 [摘要] 开头）：判断与近期工作的关联性，
        无关的直接删除，相关的保留
   b. 估算累计释放量。若已达目标，第二份/第三份按"轻度处理"
      （仅删工具输出、保留原文）即可。
   c. 若未达目标，处理第二份（中间）：
      - role=tool 工具输出：全删
      - 对话：按会话单元合并为摘要
      - 已有摘要：保留不动（禁止二次压缩）
   d. 再估算。若仍未达目标，处理第三份（最近）：
      - role=tool 工具输出：全删
      - 对话：仅精简超长内容，优先保留原文
   e. 若三份处理完仍未达目标，接受当前结果（受保护消息已排除）

4. 硬约束：
   - 每个会话单元至少保留 1 条（不得把多个会话单元合并成 1 条）
   - 摘要长度 ≤ 150 字符，不得低于 50 字符
   - 已是摘要（≤50 字符且信息密度高）不再二次压缩
   - update 的 idx 必须在 keep 中
   - 摘要格式：[摘要] <用户意图> → <执行结果> | <关键细节>

当前上下文状态：
- 参与压缩的消息数：{len(force_history)}（受保护消息已排除）
- 当前 token 总数：{display_tokens}（{usage_percent:.1f}%）
- 目标 token 总数：{compress_target_tokens}
- 需释放至少 {display_tokens - compress_target_tokens} tokens
- 上次压缩游标：{last_compress_id or '（无，从最早消息开始）'}

上方历史消息每条开头带 [idx:N] Ntokens 前缀，共 {len(force_history)} 条。
role=tool 的工具输出会被程序自动删除，不需要放入 keep。

安全边界：idx > {dream_idx_in_force} 的消息（dream-evolver 未提取知识），
不得直接删除，必须用 update 压缩为[摘要]格式后保留（不删除）。
注：受保护消息已从列表中排除，无需处理。

请按照【模式三】执行压缩决策，安全边界优先于模式三决策流程。
REMINDER: 禁止调用任何工具，直接在回复中输出 <analysis> 块和 keep=/update=/cursor= 三行。"""
```

注意：
- 函数参数名用 `force_history` / `dream_idx_in_force`（不带下划线前缀，函数内局部变量）
- 调用处（Step 3 降级循环）传 `_force_history` / `_dream_idx_in_force`（带下划线的现有变量）作为参数
- 原模式三分支 L2511-2548 的内联 prompt 构造删除，改为调用 `_build_force_prompt(...)`

- [ ] **Step 3: 改造 run_context_manager_force 加降级循环 + max_tokens 注入**

读 `niu_api/compat.py:2553-2563` 确认 `run_context_manager_force` 和调用现状。

当前代码（L2553-2563）：
```python
            def run_context_manager_force():
                return call_subagent(
                    agent_name="context-manager",
                    task=prompt,
                    llm_config=llm_config,
                    mcp_client=None,
                    context_fifo_threshold=0,
                    history=_force_history,
                )

            result = await asyncio.to_thread(run_context_manager_force)
```

改为（加降级循环 + llm_config 注入 max_tokens + 截断检测）：

```python
            # llm_config 动态注入 max_tokens（通过 litellm_kwargs）
            llm_config_with_max = dict(llm_config)
            llm_config_with_max["litellm_kwargs"] = {
                **llm_config.get("litellm_kwargs", {}),
                "max_tokens": _read_max_output_tokens(),
            }

            # 降级重压循环：最多 3 次尝试，每次压缩目标降 50%
            _compress_target_tokens = _read_compress_target_tokens()
            result = None
            for attempt in range(3):
                if attempt > 0:
                    _compress_target_tokens = int(_compress_target_tokens * 0.5)
                    logger.warning(f"[Compact] Force truncated, lowering target to {_compress_target_tokens}, attempt {attempt+1}")

                # 每次循环重新构造 prompt（_compress_target_tokens 变了，f-string 要重新求值）
                prompt = _build_force_prompt(
                    display_tokens, _compress_target_tokens, usage_percent,
                    _force_history, last_compress_id, _dream_idx_in_force
                )
                if attempt > 0:
                    prompt = prompt + "\n\n注意：上次输出被截断，请精简 <analysis> 块，只保留关键决策依据，不要逐条分析每条消息。"

                def run_context_manager_force():
                    return call_subagent(
                        agent_name="context-manager",
                        task=prompt,
                        llm_config=llm_config_with_max,
                        mcp_client=None,
                        context_fifo_threshold=0,
                        history=_force_history,
                    )

                result = await asyncio.to_thread(run_context_manager_force)

                if is_stop_requested():
                    logger.warning("[Tidy] Stop requested, aborting tidy pipeline")
                    clear_stop()
                    return {"status": "aborted", "message": "Stopped by user"}

                if result == "COMPACT_TRUNCATED":
                    if attempt < 2:
                        continue  # 降级重试
                    else:
                        logger.error("[Compact] Force all 3 attempts truncated, giving up")
                        return {"status": "skipped", "mode": "force", "reason": "all attempts truncated"}

                # 正常返回
                logger.info(f"[Tidy] Force: context-manager completed, length={len(result)}")
                break

            # 剥离 <analysis> 草稿块（在解析前）
            result = _strip_analysis(result)

            # 后续解析逻辑（原有代码 L2572+，用 result 变量）
            new_compress_id = last_compress_id
            try:
                keep_idxs: set[int] = set()
                ...
```

**关键修正点**：

1. **变量名用 `result`**（与下游 L2577 的 `result.splitlines()` 一致），模式三现状就是 `result`，不改名。

2. **prompt 在循环内重新构造**：调用 `_build_force_prompt(...)` 每次重新求值 f-string。`_compress_target_tokens` 降级后值变了，prompt 里的 `{compress_target_tokens}` 会反映新值。

3. **缩短提示只追加一次**：`if attempt > 0: prompt = prompt + 提示`——prompt 每次从 `_build_force_prompt` 重新构造（不含提示），追加不会累积。

4. **3 次都截断返回 dict**：`return {"status": "skipped", "mode": "force", "reason": "all attempts truncated"}`——与 force 分支末尾 L2827 的 `return {"status": "ok", ...}` 一致，避免返回 None。注意：这个 return 会跳过 L2810-2827 的 `tokens_after` 计算和 compress 游标写入——这是合理的（压缩没执行，不需要更新游标和 tokens_after）。

5. **`is_stop_requested` 检查**：保留原有的停止检查（L2564-2567），放在每次 `result` 赋值之后。

6. **`_strip_analysis` 在循环外调用**：循环 break 后 `result` 是正常返回值，剥离 analysis 再接原有解析（L2572+ 的 try 块）。

7. **`new_compress_id = last_compress_id` 保留**：原有 L2571 的初始化，在剥离 analysis 之后、解析之前。

- [ ] **Step 4: 确认 _strip_analysis 已在降级循环后调用**

Step 3 的降级循环代码里已经包含 `result = _strip_analysis(result)`（在 break 之后、解析之前）。确认这一行存在，且变量名用 `result`（与下游 L2577 的 `result.splitlines()` 一致）。

读改造后的代码确认：
- 降级循环 `break` 后 `result` 是正常返回值（非 "COMPACT_TRUNCATED"）
- `result = _strip_analysis(result)` 剥离 analysis 块
- 下游 `for line in result.splitlines():` 解析 keep/update/cursor（原有代码 L2577，变量名不变）

- [ ] **Step 5: 语法检查**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -c "import ast; ast.parse(open('niu_api/compat.py').read())"`
Expected: 无输出

- [ ] **Step 6: 写集成测试 — 模式三 prompt 含方法论 + cursor + dream 边界**

在 `tests/test_compress_quality.py` 追加（参考现有 `test_mode3_passes_history_to_call_subagent`）：

```python
def test_mode3_prompt_contains_methodology(monkeypatch):
    """模式三 task prompt 应含压缩方法论 + cursor + dream 安全边界。"""
    import asyncio
    import niu_api.compat as compat
    import niu_api.chat as chat_module
    import agent.subagent as subagent_module

    messages = [
        FakeMsg(id="msg-1", role="user", content="你好"),
        FakeMsg(id="msg-2", role="assistant", content="你好，我是 Niu"),
    ]

    class FakeStore:
        async def get_messages(self, limit=None, before_id=None):
            return messages

    async def fake_get_message_store():
        return FakeStore()

    class FakeRunner:
        handler = type("H", (), {"_last_prompt_tokens": 180000})()
        llm_config = {}

    def fake_get_or_create_runner():
        return FakeRunner()

    captured = {}
    def fake_call_subagent(*args, **kwargs):
        agent_name = kwargs.get("agent_name") or (args[0] if args else "")
        if agent_name == "context-manager":
            captured["task"] = kwargs.get("task", "")
            captured["llm_config"] = kwargs.get("llm_config", {})
            return "<analysis>分析</analysis>\nkeep=1,2\ncursor=2\nupdate="
        return "skip"

    monkeypatch.setattr(compat, "get_message_store", fake_get_message_store)
    monkeypatch.setattr(chat_module, "get_or_create_runner", fake_get_or_create_runner)
    monkeypatch.setattr(subagent_module, "call_subagent", fake_call_subagent)
    monkeypatch.setattr(compat, "_read_context_window_tokens", lambda: 200000, raising=False)
    monkeypatch.setattr(compat, "_read_warning_threshold", lambda: 0.8, raising=False)
    monkeypatch.setattr(compat, "_read_protect_recent_count", lambda: 0, raising=False)
    monkeypatch.setattr(compat, "_write_cursor_with_lock", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(compat, "_read_compress_target_tokens", lambda: 60000, raising=False)
    monkeypatch.setattr(compat, "_read_max_output_tokens", lambda: 16384, raising=False)

    request = {"session_id": "test", "mode": "force"}
    # 不用 try/except 吞异常——让真实错误透出
    asyncio.run(compat._tidy_context_impl(request))

    assert "task" in captured, "call_subagent 未被调用"
    assert "压缩方法论" in captured["task"]
    assert "第一份" in captured["task"]
    assert "会话单元" in captured["task"]
    assert "<analysis>" in captured["task"]
    assert "cursor=" in captured["task"]
    assert "安全边界" in captured["task"]
    assert captured["llm_config"].get("litellm_kwargs", {}).get("max_tokens") == 16384
```

**注意**：
- `FakeMsg` 从 `tests/test_compress_history.py` 导入或在测试文件顶部定义
- 不 mock `_read_target_threshold`（Task 1 已删除该配置，模式三不再用百分比阈值）
- 不用 `try/except: pass` 吞异常

- [ ] **Step 7: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_compress_quality.py::test_mode3_prompt_contains_methodology -v`
Expected: PASS

- [ ] **Step 8: 运行现有测试不破坏**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_compress_quality.py tests/test_compress_history.py -v 2>&1 | tail -30`
Expected: 无新增 FAIL

- [ ] **Step 9: 临时提交**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add niu_api/compat.py tests/test_compress_quality.py
git commit -m "feat(compat): 模式三 task prompt 写回完整方法论 + 降级重压循环

模式三 force 分支改造（与模式二对齐）：
- task prompt 内联压缩方法论 + <analysis> 草稿块
- 保留 cursor= 输出行 + dream 安全边界（模式三特有）
- 降级重压循环（3 次尝试，目标降 50%）
- max_tokens 通过 llm_config[litellm_kwargs] 注入
- 解析前 _strip_analysis 剥离草稿块"
```

---

## Task 9: 删除校验兜底逻辑

**Files:**
- Modify: `niu_api/compat.py:1946-1960`（模式二删校验兜底）
- Modify: `niu_api/compat.py:2606-2629`（模式三删校验兜底）

- [ ] **Step 1: 读模式二校验兜底现状**

读 `niu_api/compat.py:1946-1960` 确认当前校验逻辑：
- L1946-1951：update idx 不在 keep 时自动补进 keep（`keep_idxs |= missing_in_keep`）
- L1956-1958：update idx 越界 `if idx in _idx_to_id` 过滤（保留，加 warning）

- [ ] **Step 2: 删除模式二 update idx 自动补 keep 逻辑**

当前代码（L1946-1951 概要）：
```python
            # update idx 必须在 keep 中，自动补齐
            missing_in_keep = set(update_idxs) - keep_idxs
            if missing_in_keep:
                logger.warning(f"update idxs not in keep, auto-added: {missing_in_keep}")
                keep_idxs |= missing_in_keep
```

删除这段代码（L1946-1951 整段删掉）。LLM 输出什么就用什么，不自动补。

- [ ] **Step 3: 模式二 update 越界 idx 加 warning 日志**

当前代码（L1956-1958 概要）：
```python
            updates = [{"message_id": _idx_to_id[idx], "content": content} for idx, content in update_list if idx in _idx_to_id]
```

改为（保留过滤 + 加 warning）：
```python
            for idx, _ in update_list:
                if idx not in _idx_to_id:
                    logger.warning(f"[Compact] Mode-2 LLM returned out-of-range update idx {idx}, silently dropped")
            updates = [{"message_id": _idx_to_id[idx], "content": content} for idx, content in update_list if idx in _idx_to_id]
```

- [ ] **Step 4: 读模式三校验兜底现状**

读 `niu_api/compat.py:2606-2629` 确认当前校验逻辑：
- L2606-2611：update idx 不在 keep 时自动补进 keep
- cursor idx 不在 keep 降级取 max（L2624-2627 附近）

- [ ] **Step 5: 删除模式三 update idx 自动补 keep + cursor 降级逻辑**

当前代码（L2606-2611 概要）：
```python
            # update idx 必须在 keep 中，自动补齐
            missing_in_keep = set(update_idxs) - keep_idxs
            if missing_in_keep:
                logger.warning(...)
                keep_idxs |= missing_in_keep
```

删除这段代码。

当前 cursor 降级代码（L2624-2627 概要）：
```python
            if cursor_idx and cursor_idx in _f_idx_to_id:
                ...
            else:
                cursor_idx = max(keep_idxs)  # 降级取 max
```

改为（删除 else 降级，cursor 不在映射里就置 None）：
```python
            if cursor_idx and cursor_idx in _f_idx_to_id:
                cursor_uuid = _f_idx_to_id[cursor_idx]
            else:
                logger.warning(f"[Compact] Force cursor idx {cursor_idx} not in mapping, skipping cursor update")
                cursor_uuid = None
```

注意：读实际代码确认 cursor_uuid 后续怎么用，None 时跳过 cursor 写入。

- [ ] **Step 6: 模式三 update 越界 idx 加 warning**

仿 Step 3，在模式三 update 解析处加 warning：
```python
            for idx, _ in update_list:
                if idx not in _f_idx_to_id:
                    logger.warning(f"[Compact] Force LLM returned out-of-range update idx {idx}, silently dropped")
            updates = [{"message_id": _f_idx_to_id[idx], "content": content} for idx, content in update_list if idx in _f_idx_to_id]
```

- [ ] **Step 7: 语法检查**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -c "import ast; ast.parse(open('niu_api/compat.py').read())"`
Expected: 无输出

- [ ] **Step 8: 写测试 — 校验兜底已删除**

在 `tests/test_compress_quality.py` 追加。

**测试设计说明**：删除 auto-fixup 后，LLM 回 `keep=1, update=3|摘要`（idx 3 不在 keep）时：
- msg-3 不在 keep → 进 deletes
- msg-3 在 update → 进 updates
- L2034-2038 的 overlap 处理：update_ids & deletes 的重叠从 deletes 移除
- 最终 msg-3 被 update 保留改摘要（不被删除）

这是已有 overlap 逻辑的合理兜底（避免 LLM 笔误丢消息）。Task 9 删的是"auto-fixup 把 3 补进 keep"——让 keep 列表保持 LLM 原样，不偷偷加 idx。

```python
def test_mode2_no_auto_keep_fixup(monkeypatch):
    """模式二不再自动把 update idx 补进 keep（keep 列表保持 LLM 原样）。"""
    import asyncio
    import niu_api.compat as compat
    import niu_api.chat as chat_module
    import agent.subagent as subagent_module

    messages = [
        FakeMsg(id="msg-1", role="user", content="你好"),
        FakeMsg(id="msg-2", role="assistant", content="你好"),
        FakeMsg(id="msg-3", role="user", content="测试"),
    ]

    deleted_ids = []
    updated_ids = []

    class FakeStore:
        async def get_messages(self, limit=None, before_id=None):
            return messages
        async def delete_messages(self, session_id, message_ids):
            deleted_ids.extend(message_ids)
            return len(message_ids)
        async def update_message(self, message_id=None, content=None, **kw):
            updated_ids.append(message_id)
            return True

    async def fake_get_message_store():
        return FakeStore()

    class FakeRunner:
        handler = type("H", (), {"_last_prompt_tokens": 180000})()
        llm_config = {}

    def fake_get_or_create_runner():
        return FakeRunner()

    # LLM 回 keep=1（不含 update 的 idx 3），update=3|摘要（idx 3 不在 keep）
    # 如果 auto-fixup 还在，3 会被补进 keep；删了 auto-fixup 后 keep 只有 1
    captured = {}
    def fake_call_subagent(*args, **kwargs):
        agent_name = kwargs.get("agent_name") or (args[0] if args else "")
        if agent_name == "context-manager":
            captured["task"] = kwargs.get("task", "")
            return "<analysis>分析</analysis>\nkeep=1\nupdate=3|摘要内容"
        return "skip"

    monkeypatch.setattr(compat, "get_message_store", fake_get_message_store)
    monkeypatch.setattr(chat_module, "get_or_create_runner", fake_get_or_create_runner)
    monkeypatch.setattr(subagent_module, "call_subagent", fake_call_subagent)
    monkeypatch.setattr(compat, "_read_context_window_tokens", lambda: 200000, raising=False)
    monkeypatch.setattr(compat, "_read_warning_threshold", lambda: 0.8, raising=False)
    monkeypatch.setattr(compat, "_read_protect_recent_count", lambda: 0, raising=False)
    monkeypatch.setattr(compat, "_write_cursor_with_lock", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(compat, "_read_compress_target_tokens", lambda: 60000, raising=False)
    monkeypatch.setattr(compat, "_read_max_output_tokens", lambda: 16384, raising=False)

    request = {"session_id": "test", "mode": "sleep"}
    # 不用 try/except 吞异常
    asyncio.run(compat._tidy_context_impl(request))

    # 验证 auto-fixup 已删除：
    # - msg-3 不在 keep（LLM 只回 keep=1），不被 auto-fixup 补进 keep
    # - msg-3 进 update（LLM 回 update=3），被 update 保留改摘要
    # - msg-3 不进 delete（overlap 从 deletes 移除）
    assert "msg-3" in updated_ids, "msg-3 应被 update 保留改摘要"
    assert "msg-3" not in deleted_ids, "msg-3 不应被删除（overlap 从 deletes 移除）"
    # msg-2 既不在 keep 也不在 update，应被删除
    assert "msg-2" in deleted_ids, "msg-2 应被删除（不在 keep 也不在 update）"
```

**关键断言**：
- `msg-3 in updated_ids`：update idx 3 不在 keep，但 overlap 处理后从 deletes 移除，最终被 update 保留改摘要
- `msg-3 not in deleted_ids`：overlap 兜底，不删除
- `msg-2 in deleted_ids`：msg-2 不在 keep 也不在 update，正常删除

这验证了"auto-fixup 已删除"——keep 列表保持 LLM 原样（只有 1），没有偷偷把 3 补进 keep。同时验证了 overlap 兜底逻辑仍生效（避免 LLM 笔误丢消息）。

- [ ] **Step 8.5: 补降级重压循环测试 + analysis 缺失测试**

在 `tests/test_compress_quality.py` 追加三个测试：

```python
def test_mode2_degradation_first_truncate_second_success(monkeypatch):
    """模式二降级循环：第 1 次截断 → 第 2 次成功。"""
    import asyncio
    import niu_api.compat as compat
    import niu_api.chat as chat_module
    import agent.subagent as subagent_module

    messages = [
        FakeMsg(id="msg-1", role="user", content="你好"),
        FakeMsg(id="msg-2", role="assistant", content="你好"),
    ]

    class FakeStore:
        async def get_messages(self, limit=None, before_id=None):
            return messages
        async def delete_messages(self, *a, **kw):
            return 0
        async def update_message(self, *a, **kw):
            return True

    async def fake_get_message_store():
        return FakeStore()

    class FakeRunner:
        handler = type("H", (), {"_last_prompt_tokens": 180000})()
        llm_config = {}

    def fake_get_or_create_runner():
        return FakeRunner()

    # 第 1 次返回 COMPACT_TRUNCATED，第 2 次返回正常结果
    call_count = {"n": 0}
    captured_targets = []
    def fake_call_subagent(*args, **kwargs):
        agent_name = kwargs.get("agent_name") or (args[0] if args else "")
        if agent_name == "context-manager":
            call_count["n"] += 1
            captured_targets.append(kwargs.get("task", ""))
            if call_count["n"] == 1:
                return "COMPACT_TRUNCATED"  # 第 1 次截断
            return "<analysis>分析</analysis>\nkeep=1,2\nupdate="  # 第 2 次成功
        return "skip"

    monkeypatch.setattr(compat, "get_message_store", fake_get_message_store)
    monkeypatch.setattr(chat_module, "get_or_create_runner", fake_get_or_create_runner)
    monkeypatch.setattr(subagent_module, "call_subagent", fake_call_subagent)
    monkeypatch.setattr(compat, "_read_context_window_tokens", lambda: 200000, raising=False)
    monkeypatch.setattr(compat, "_read_warning_threshold", lambda: 0.8, raising=False)
    monkeypatch.setattr(compat, "_read_protect_recent_count", lambda: 0, raising=False)
    monkeypatch.setattr(compat, "_write_cursor_with_lock", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(compat, "_read_compress_target_tokens", lambda: 60000, raising=False)
    monkeypatch.setattr(compat, "_read_max_output_tokens", lambda: 16384, raising=False)

    request = {"session_id": "test", "mode": "sleep"}
    asyncio.run(compat._tidy_context_impl(request))

    # 验证调用了 2 次
    assert call_count["n"] == 2, f"应调用 2 次（第 1 次截断 + 第 2 次成功），实际 {call_count['n']}"
    # 第 2 次 prompt 含缩短提示
    assert "精简" in captured_targets[1] or "缩短" in captured_targets[1], "第 2 次 prompt 应含缩短 analysis 提示"


def test_mode2_degradation_all_three_truncate(monkeypatch):
    """模式二降级循环：3 次都截断 → 放弃压缩，返回 skipped。"""
    import asyncio
    import niu_api.compat as compat
    import niu_api.chat as chat_module
    import agent.subagent as subagent_module

    messages = [
        FakeMsg(id="msg-1", role="user", content="你好"),
        FakeMsg(id="msg-2", role="assistant", content="你好"),
    ]

    class FakeStore:
        async def get_messages(self, limit=None, before_id=None):
            return messages

    async def fake_get_message_store():
        return FakeStore()

    class FakeRunner:
        handler = type("H", (), {"_last_prompt_tokens": 180000})()
        llm_config = {}

    def fake_get_or_create_runner():
        return FakeRunner()

    # 每次都返回 COMPACT_TRUNCATED
    call_count = {"n": 0}
    def fake_call_subagent(*args, **kwargs):
        agent_name = kwargs.get("agent_name") or (args[0] if args else "")
        if agent_name == "context-manager":
            call_count["n"] += 1
            return "COMPACT_TRUNCATED"
        return "skip"

    monkeypatch.setattr(compat, "get_message_store", fake_get_message_store)
    monkeypatch.setattr(chat_module, "get_or_create_runner", fake_get_or_create_runner)
    monkeypatch.setattr(subagent_module, "call_subagent", fake_call_subagent)
    monkeypatch.setattr(compat, "_read_context_window_tokens", lambda: 200000, raising=False)
    monkeypatch.setattr(compat, "_read_warning_threshold", lambda: 0.8, raising=False)
    monkeypatch.setattr(compat, "_read_protect_recent_count", lambda: 0, raising=False)
    monkeypatch.setattr(compat, "_write_cursor_with_lock", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(compat, "_read_compress_target_tokens", lambda: 60000, raising=False)
    monkeypatch.setattr(compat, "_read_max_output_tokens", lambda: 16384, raising=False)

    request = {"session_id": "test", "mode": "sleep"}
    result = asyncio.run(compat._tidy_context_impl(request))

    # 验证调用了 3 次
    assert call_count["n"] == 3, f"应调用 3 次，实际 {call_count['n']}"
    # 验证返回 skipped（不返回 None）
    assert result is not None, "3 次截断应返回 dict，不应是 None"
    assert isinstance(result, dict)
    assert result.get("status") == "skipped", f"应返回 skipped，实际 {result}"


def test_strip_analysis_missing_then_parse():
    """LLM 没写 <analysis> 块时，_strip_analysis 原样返回，解析正常。"""
    from niu_api.compat import _strip_analysis

    # LLM 直接输出 keep/update，无 analysis 块
    raw = "keep=1,2,3\nupdate=1|摘要"
    result = _strip_analysis(raw)
    # 原样返回
    assert result == raw
    # 解析 keep/update 仍可用
    lines = result.strip().splitlines()
    keep_line = [l for l in lines if l.lower().startswith("keep=")]
    assert len(keep_line) == 1
    assert "1,2,3" in keep_line[0]
```

- [ ] **Step 9: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_compress_quality.py::test_mode2_no_auto_keep_fixup tests/test_compress_quality.py::test_mode2_degradation_first_truncate_second_success tests/test_compress_quality.py::test_mode2_degradation_all_three_truncate tests/test_compress_quality.py::test_strip_analysis_missing_then_parse -v`
Expected: 4 个测试 PASS

- [ ] **Step 10: 运行现有测试不破坏**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_compress_quality.py tests/test_compress_history.py -v 2>&1 | tail -30`
Expected: 无新增 FAIL

- [ ] **Step 11: 临时提交**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add niu_api/compat.py tests/test_compress_quality.py
git commit -m "refactor(compat): 删除压缩校验兜底逻辑，避免削弱 prompt 约束

删除：
- update idx 不在 keep 时自动补进 keep（模式二 L1946-1951 / 模式三 L2606-2611）
- cursor idx 不在 keep 降级取 max（模式三 L2624-2627）

保留：
- idx in _idx_to_id 映射检查（防 KeyError）
- 越界 idx 静默丢弃 + warning 日志（便于排查）

设计取舍：LLM 输出什么用什么，靠 prompt 硬约束让 LLM 一次做对，
不靠程序补救（程序补救会削弱 prompt 约束力）。"
```

---

## Task 10: 术语清理（L0/L1/L2 + 事务→会话单元 + 远端中端近端→三份）

**Files:**
- Modify: `config/agents/context-manager.md`
- Modify: `docs/feature-context-management.md`
- Modify: `niu_api/compat.py`（task prompt 里的术语，Task 7/8 已改，这里查漏）
- Modify: 其他 docs（grep 发现的残留）

- [ ] **Step 1: grep 全项目找 L0/L1/L2 残留**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && grep -rn "L0\|L1\|L2" config/agents/context-manager.md docs/feature-context-management.md docs/SYSTEM_MANUAL.md AGENTS.md 2>/dev/null | grep -i "摘要\|原文\|压缩\|存储" | head -30`

记录所有命中位置。

注意：只清理"压缩相关的 L0/L1/L2"（如"L0 摘要""L1 摘要""L2 原文""L2→L1→L0 删除优先级"）。**不动** LightRAG 的 `l1`/`l2` 标签（那是知识图谱层级标签，与压缩无关）。

- [ ] **Step 2: 清理 config/agents/context-manager.md**

读 `config/agents/context-manager.md` 找 L0/L1/L2 残留。

逐处替换或删除：
- "L0 摘要" → "摘要"
- "L1 摘要" → "摘要"
- "L2 原文" → "原文"
- "L2→L1→L0 删除优先级" → 按现行三份方法论重写（或删除如果已过时）
- "事务"/"事务块" → "会话单元"
- "远端/中端/近端" → "第一份（最早）/第二份（中间）/第三份（最近）"

注意：system prompt 的有效规则（会话单元边界、摘要格式、禁止无限衰减、压缩强度量化）保留，只改术语。

- [ ] **Step 3: 清理 docs/feature-context-management.md**

读 `docs/feature-context-management.md` 找 L0/L1/L2 残留（审查报告说有 45 处）。

逐处替换或删除（同 Step 2 的术语映射）。如果某些段落整体过时（如"L2→L1→L0 删除优先级"已被三份方法论取代），整段重写或标注"已被三份方法论取代，见 context-manager.md"。

- [ ] **Step 4: 清理其他 docs 和 AGENTS.md**

grep `AGENTS.md` 和 `docs/SYSTEM_MANUAL.md` 及子文档的 L0/L1/L2 残留，逐处清理。

- [ ] **Step 5: 验证术语统一**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && grep -rn "L0 摘要\|L1 摘要\|L2 原文\|事务块\|远端.*中端.*近端" config/agents/ docs/ 2>/dev/null | head -20`
Expected: 无命中（或只剩 LightRAG 的 l1/l2 标签，那不是压缩相关）

- [ ] **Step 6: 验证程序启动不破坏**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -c "from niu_api.compat import _tidy_context_impl; print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 7: 临时提交**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add config/agents/context-manager.md docs/feature-context-management.md AGENTS.md docs/SYSTEM_MANUAL.md
git commit -m "docs(cleanup): 术语清理 - L0/L1/L2 废弃 + 事务→会话单元 + 远端中端近端→三份

- 删除 L0/L1/L2 废弃说法（向量库时代术语，知识图谱已取代）
- 统一用'会话单元'（替代'事务'/'事务块'）
- 统一用'第一份/第二份/第三份'（替代'远端/中端/近端'，避免百分数歧义）
- 不动 LightRAG 的 l1/l2 标签（知识图谱层级标签，与压缩无关）"
```

---

## Task 11: 端到端验证

**Files:**
- 无文件修改，纯验证

- [ ] **Step 1: 启动程序，真实触发模式二压缩**

用户执行：
1. `./niu` 启动程序
2. 持续对话，直到上下文使用率 ≥ 80%（日志显示 `usage=X.X%`）
3. 睡眠触发或 force 触发压缩
4. 观察日志 `[Tidy]` / `[Compact]` 相关行

- [ ] **Step 2: 检查压缩请求日志结构**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python3 -c "
import json, glob, os, datetime
files = sorted(glob.glob('logs/raw_http/' + datetime.date.today().strftime('%Y%m%d') + '/*_request.json'))
for f in reversed(files[-20:]):
    with open(f) as fh:
        req = json.load(fh)
    sys_content = req['messages'][0].get('content','')
    if isinstance(sys_content, str) and '记忆压缩器' in sys_content:
        msgs = req['messages']
        # 找 task prompt（最后一个 user message）
        task = ''
        for m in reversed(msgs):
            if m.get('role') == 'user':
                c = m.get('content','')
                if isinstance(c, str):
                    task = c
                    break
        if '压缩方法论' not in task:
            continue
        print(f'=== 找到新方法论压缩请求: {f} ===')
        print(f'消息数: {len(msgs)}')
        print(f'task 含压缩方法论: {\"压缩方法论\" in task}')
        print(f'task 含 analysis 草稿块: {\"<analysis>\" in task}')
        print(f'task 含会话单元: {\"会话单元\" in task}')
        print(f'task 含三份划分: {\"第一份\" in task}')
        break
"
```

Expected:
- task 含"压缩方法论"
- task 含"<analysis>"
- task 含"会话单元"
- task 含"第一份"

- [ ] **Step 3: 检查 LLM 回复含 analysis 块**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python3 -c "
import json, glob, datetime
files = sorted(glob.glob('logs/raw_http/' + datetime.date.today().strftime('%Y%m%d') + '/*_response.json'))
for f in reversed(files[-20:]):
    with open(f) as fh:
        resp = json.load(fh)
    content = resp.get('choices',[{}])[0].get('message',{}).get('content','')
    if '<analysis>' in content and 'keep=' in content:
        print(f'=== 找到含 analysis 的压缩回复: {f} ===')
        print(f'content 前 500 字符: {content[:500]}')
        print(f'含 analysis: {\"<analysis>\" in content}')
        print(f'含 keep=: {\"keep=\" in content}')
        print(f'含 update=: {\"update=\" in content}')
        break
"
```

Expected:
- LLM 回复含 `<analysis>` 块
- 含 `keep=` / `update=` 行
- analysis 块有实际分析内容（不是空标签）

- [ ] **Step 4: 验证压缩质量改善**

检查 LLM 实际回的 keep/update：
- keep 条数是否合理（不再是 3-4 条）
- update 摘要是否按会话单元组织（不是 1 条塞 290 条）
- 摘要格式是否含 `[摘要]` 前缀

Run: `cd REDACTED_USER_PATH/tools/ai-bot && grep "Mode-2.*Parsed\|Force.*Parsed\|keep.*update" logs/api_stderr.log 2>/dev/null | tail -10`
Expected: 看到 keep/update 计数合理（如 keep 20+ 条、update 10+ 条摘要）

- [ ] **Step 5: 验证无单消息超限错误**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && grep "exceed max message tokens\|finish_reason.*length" logs/api_stderr.log 2>/dev/null | tail -5 || echo "无超限错误"`
Expected: 不再出现 `Total tokens of image and text exceed max message tokens`。如果有 `finish_reason=length`，验证降级重压是否触发（日志应有 `lowering target`）。

- [ ] **Step 6: 最终提交（清理调试代码，如有）**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git status
# 如有调试代码清理后
git add -A
git commit -m "feat(compress): context-manager 压缩质量修复完成

修复 390 条压成 1 条的严重缺陷：
- task prompt 写回完整压缩方法论（三份逐份处理 + 会话单元 + 旧摘要关联性判断）
- 引入 <analysis> 草稿块让 LLM 单轮内先分析再输出
- 配置改 token 绝对值（compressTargetTokens + maxOutputTokens）
- finish_reason 传递链 + 截断降级重压（3 次尝试）
- 删除削弱约束的校验兜底
- 术语清理（L0/L1/L2 废弃 + 事务→会话单元 + 远端中端近端→三份）"
```

---

## 自审检查

### 1. Spec 覆盖

- 配置层变更 → Task 1 ✅
- `_strip_analysis` 辅助函数 → Task 2 ✅
- finish_reason 传递链（MockResponse + litellm_adapter + agent_loop + call_subagent）→ Task 3-6 ✅
- 模式二 task prompt 重写 + 降级循环 → Task 7 ✅
- 模式三 task prompt 重写 + 降级循环 → Task 8 ✅
- 删除校验兜底 → Task 9 ✅
- 术语清理 → Task 10 ✅
- 端到端验证 → Task 11 ✅

### 2. Placeholder 扫描

无 TBD/TODO。所有步骤包含具体代码。

### 3. 类型一致性

- `_read_compress_target_tokens() -> int`：Task 1 定义，Task 7/8 使用 ✅
- `_read_max_output_tokens() -> int`：Task 1 定义，Task 7/8 使用 ✅
- `_strip_analysis(response: str) -> str`：Task 2 定义，Task 7/8 使用 ✅
- `MockResponse.finish_reason`：Task 3 定义，Task 4 填充，Task 5 传递，Task 6 检测 ✅
- `"COMPACT_TRUNCATED"` 字符串信号：Task 6 返回，Task 7/8 识别 ✅
- `llm_config_with_max`：Task 7/8 构造，传给 call_subagent ✅

### 4. 风险点

- **模式一兼容性**：`_compress_target` 保留给模式一（Task 7/8 不动模式一），spec 明确 ✅
- **MAX_TURNS_EXCEEDED 路径**：Task 5 只改 L570/L583，L610 不带 finish_reason ✅
- **降级循环 3 次都失败**：Task 7/8 用 `return` 或 `result=None` 放弃，记 error 日志 ✅
- **analysis 块超长**：Task 7/8 降级时追加"缩短 analysis"提示 ✅
