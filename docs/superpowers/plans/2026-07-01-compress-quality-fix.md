# context-manager 压缩质量修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 context-manager 压缩质量缺陷——把 task prompt 丢失的压缩方法论写回（三区逐份处理 + 会话单元 + 旧摘要关联性判断 + 硬约束），引入 `<analysis>` 草稿块让 LLM 一轮做对，配置改 token 绝对值（compressTargetTokens），加输出截断应急清空（保留最近 10 条，靠 journal.md + 知识图谱回溯历史），删除削弱约束的校验兜底。

**Architecture:** 分三层修复：(1) 配置层 `targetThreshold` 百分比 → `compressTargetTokens` 绝对值（`maxOutputTokens` 不配置，程序动态算 `contextWindowSize × 0.16` 封顶 65536）；(2) Prompt 层模式二/三 task prompt 重写，内联完整方法论 + `<analysis>` 草稿块；(3) 程序保底层 finish_reason 传递链（MockResponse + litellm_adapter + agent_loop + call_subagent）+ 截断应急清空（**单次调用，不重试**，截断时保留最近 10 条 + 上面全删 + 最旧改"压缩失败"摘要）。解析层新增 `_strip_analysis` 剥离草稿块，删除 update idx 自动补 keep 等校验兜底。

**Tech Stack:** Python 3.11, litellm, 火山方舟 ark-code-latest（doubao-seed-2-0-code，context 256K，单轮输出硬限 128K，平台默认 4K）

**设计变更说明（2026-07-01）：** 原设计的"降级重压循环"（3 次尝试，每次降 50% 目标）经审查发现方向反效果——目标降得越低意味着要释放更多 token，LLM 反而要写更长的 analysis，更易再次截断。改为"单次调用 + 应急清空"：截断时不重试，直接保留最近 10 条 + 上面全删 + 最旧改摘要，靠 journal.md + 知识图谱（entity-extractor / dream-evolver / journal-agent 三层前置兜底）让主 Agent 读回历史。

**设计变更说明（2026-07-01 第二次变更）：** 诚实评估发现 spec 漏了 runner.py 这条模式三主要活跃路径 + "追加当前轮结果"描述与实现不符：
1. **补 runner.py 主要路径**：模式三的主要活跃路径是 `agent_loop.py:308` 工具调用返回后同步回调 `runner.py:_on_context_high_usage`（不是 compat.py）。compat.py/chat.py 的 force 路径服务 CONTEXT_OVERFLOW 兜底场景。runner.py 必须改造对齐 compat.py 模式三（新增 Task 10.5）。
2. **修正"追加当前轮结果"误解**：原 spec 假设"先保存返回结果再压缩"，实际代码触发顺序是"压缩在 response persist 之前执行"——压缩读的是 DB 历史消息（不含当前轮 response），压缩后当前轮 response 才 append。所以当前轮结果天然不丢，**不需要显式追加逻辑**。Task 8 不再需要"补追加逻辑"。
3. **Task 9 同步 runner.py**：删校验兜底必须同步 runner.py（Task 10.5 处理），不能只改 compat.py。

**已实施 Task 回退改造清单：**
- **Task 1 已实施**（commit `a019c7c5` + `dee7ba93`）：`_read_max_output_tokens` 当前读配置 `maxOutputTokens`，**需回退改造为动态算**（`contextWindowSize × 0.16` 封顶 65536），并删除 `config/user-config.json` 里的 `maxOutputTokens: 16384` 硬编码
- **Task 7 已实施**（commit `432fc603` + `4cc5f4a0`）：模式二已实施旧版降级循环（3 次 for 循环），**需回退改造为单次调用 + 应急清空**——删 `for attempt in range(3)` 循环，截断时调用 `_emergency_clear`
- **Task 8 不再需要"补追加逻辑"**：实际实现顺序保证当前轮 response 天然不丢（压缩在 persist 前执行），不需要显式追加
- **Task 9 需要同步 runner.py**：删校验兜底不能只改 compat.py，runner.py 的 `_on_context_high_usage` 也有相同校验兜底，由 Task 10.5 处理

---

## File Structure

| 文件 | 职责 | 改动类型 |
|------|------|----------|
| `config/user-config.json` | context 段配置项 | Modify（删 targetThreshold，加 compressTargetTokens；**删除 maxOutputTokens 硬编码**，程序动态算）|
| `agent/subagent.py` | 配置读取函数 + call_subagent 截断检测 | Modify（**Task 1 已实施旧版读配置，需回退改造 `_read_max_output_tokens` 为动态算**）|
| `agent/generic/llmcore.py` | MockResponse 加 finish_reason 字段 | Modify |
| `agent/generic/litellm_adapter.py` | 流式循环捕获 finish_reason + 传入 MockResponse | Modify |
| `agent/generic/agent_loop.py` | return_value 加 finish_reason 字段 | Modify |
| `niu_api/compat.py` | 模式二/三 task prompt 重写 + 应急清空 + 删校验兜底 + `_strip_analysis` + `_emergency_clear` | Modify（**Task 7 已实施旧版降级循环，需回退改造为单次调用 + 应急清空**）|
| `agent/runner.py` | **模式三主要活跃路径** `_on_context_high_usage` 改造对齐 compat.py 模式三 | Modify（**新增 Task 10.5**：旧 prompt + 旧解析 + 无截断处理，需改造为 `_build_force_prompt` + `_emergency_clear` 内联 + `_strip_analysis` + max_tokens 注入 + 删校验兜底）|
| `config/agents/context-manager.md` | 术语清理（L0/L1/L2 + 事务→会话单元） | Modify |
| `docs/feature-context-management.md` | L0/L1/L2 术语清理 | Modify |
| `tests/test_compress_quality.py` | 新增测试文件 | Create（**删降级循环测试，新增应急清空测试**）|

---

## Task 1: 配置层变更 + 读取函数

> **⚠️ 回退改造说明**：Task 1 已实施（commit `a019c7c5` + `dee7ba93`）。当前 `_read_max_output_tokens` 读配置 `maxOutputTokens`，需要回退改造为**动态算**（`contextWindowSize × 0.16` 封顶 65536），并删除 `config/user-config.json` 里的 `maxOutputTokens: 16384` 硬编码。`_read_compress_target_tokens` 保持读配置不变。

**Files:**
- Modify: `config/user-config.json`（**删除 maxOutputTokens 硬编码**）
- Modify: `agent/subagent.py:41-104`（**`_read_max_output_tokens` 改为动态算**）
- Test: `tests/test_compress_quality.py`（**更新 `_read_max_output_tokens` 测试为动态算**）

- [ ] **Step 1: 写失败测试 — 配置读取函数（动态算 max_output_tokens）**

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


def test_read_max_output_tokens_dynamic_calc():
    """max_output_tokens 动态算：contextWindowSize × 0.16，封顶 65536。
    
    不读配置 maxOutputTokens（已删除硬编码）。
    换模型自动适配：200K → 32000；128K → 20480；400K → 65536（封顶）。
    """
    # mock _read_context_window_tokens 返回不同窗口大小
    with patch("agent.subagent._read_context_window_tokens", return_value=200000):
        assert _read_max_output_tokens() == 32000  # 200000 × 0.16

    with patch("agent.subagent._read_context_window_tokens", return_value=128000):
        assert _read_max_output_tokens() == 20480  # 128000 × 0.16

    with patch("agent.subagent._read_context_window_tokens", return_value=400000):
        assert _read_max_output_tokens() == 65536  # 400000 × 0.16 = 64000，封顶 65536

    with patch("agent.subagent._read_context_window_tokens", return_value=500000):
        assert _read_max_output_tokens() == 65536  # 500000 × 0.16 = 80000，封顶 65536
```

**注意**：测试 mock `_read_context_window_tokens`（不是配置值），验证动态算逻辑。回退改造时需先删除旧测试 `test_read_max_output_tokens_default` / `test_read_max_output_tokens_custom`（它们测的是读配置逻辑，已废弃）。

- [ ] **Step 2: 运行测试确认失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_compress_quality.py -v`
Expected: FAIL with `AssertionError`（`_read_max_output_tokens` 当前读配置返回 16384，不等于动态算的 32000）

- [ ] **Step 3: 修改 config/user-config.json（删除 maxOutputTokens）**

读 `config/user-config.json` 确认 context 段当前内容（Task 1 已实施，应有 `contextWindowSize / warningThreshold / compressTargetTokens / maxOutputTokens / sleepTriggerMinutes`）。

**删除** `maxOutputTokens` 字段（程序动态算，不再配置）。保留 `contextWindowSize / warningThreshold / compressTargetTokens / sleepTriggerMinutes` 不变。

改后 context 段应为：
```json
"context": {
  "contextWindowSize": 200000,
  "warningThreshold": 0.8,
  "compressTargetTokens": 60000,
  "sleepTriggerMinutes": 30
}
```

- [ ] **Step 4: 在 agent/subagent.py 改造 `_read_max_output_tokens` 为动态算**

读 `agent/subagent.py:41-104` 确认现有 `_read_context_threshold` / `_read_target_threshold` / `_read_context_window_tokens` 模式，以及 Task 1 已实施的 `_read_max_output_tokens`（当前读配置）。

**回退改造** `_read_max_output_tokens`：删除读配置逻辑，改为读 `_read_context_window_tokens() × 0.16` 封顶 65536。

```python
DEFAULT_COMPRESS_TARGET_TOKENS = 60000
MAX_OUTPUT_TOKENS_RATIO = 0.16  # contextWindowSize × 0.16
MAX_OUTPUT_TOKENS_CAP = 65536   # 封顶 65536


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
    """动态计算 max_output_tokens：contextWindowSize × 0.16，封顶 65536。
    
    不读配置 maxOutputTokens（已删除硬编码）。
    换模型自动适配：不同模型 contextWindowSize 不同，×0.16 自动算对应值。
    200K → 32000；128K → 20480；400K → 64000（封顶前）；500K → 65536（封顶）。
    """
    context_window = _read_context_window_tokens()
    val = int(context_window * MAX_OUTPUT_TOKENS_RATIO)
    return min(val, MAX_OUTPUT_TOKENS_CAP)
```

**回退改造要点**：
- 删除 `DEFAULT_MAX_OUTPUT_TOKENS = 16384` 常量（不再用）
- 删除读 `config.context.maxOutputTokens` 的逻辑
- 改为读 `_read_context_window_tokens()` × 0.16，封顶 65536
- 函数签名 `() -> int` 不变
- `_read_compress_target_tokens` 保持读配置不变

- [ ] **Step 5: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_compress_quality.py -v`
Expected: 3 个测试 PASS（`test_read_compress_target_tokens_default` / `test_read_compress_target_tokens_custom` / `test_read_max_output_tokens_dynamic_calc`）

- [ ] **Step 6: 语法检查**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -c "import ast; ast.parse(open('agent/subagent.py').read())"`
Expected: 无输出（语法 OK）

- [ ] **Step 7: 临时提交**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add config/user-config.json agent/subagent.py tests/test_compress_quality.py
git commit -m "refactor(config): maxOutputTokens 改为动态算，删除硬编码配置

回退改造 Task 1 旧版（读配置 maxOutputTokens）：
- 删除 config/user-config.json 的 maxOutputTokens: 16384 硬编码
- _read_max_output_tokens 改为读 contextWindowSize × 0.16，封顶 65536
- 换模型自动适配，不依赖用户手设
- _read_compress_target_tokens 保持读配置（60000）不变

理由：原降级重压循环设计废弃（方向反效果），maxOutputTokens 不再
需要按 LLM 输出能力保守取值，改为按上下文窗口比例动态算更合理。"
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
finish_reason=='length' 返回 COMPACT_TRUNCATED 触发应急清空。

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

    # LiteLLMSession 接收 cfg dict（不是关键字参数），见 BaseSession.__init__
    cfg = {
        "apikey": "test",
        "apibase": "http://test",
        "model": "test-model",
        "read_timeout": 30,
    }
    session = LiteLLMSession(cfg)
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

如果测试因 `chat` 方法内部读了其他 cfg 字段而失败，读 `agent/generic/litellm_adapter.py:329` 的 `LiteLLMSession.__init__` 和 `chat` 方法确认需要的字段，补全 cfg dict。

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

    # call_subagent 内部用 from .handler import NiuHandler / from .runner import create_client, get_tools_schema
    # 函数内 import 直接从源模块拿，必须 patch 源模块（不是 subagent 模块）
    import agent.handler as handler_module
    import agent.runner as runner_module
    class FakeClient:
        pass
    monkeypatch.setattr(runner_module, "create_client", lambda cfg: FakeClient())
    monkeypatch.setattr(runner_module, "get_tools_schema", lambda: [])
    # NiuHandler 需要支持 _disable_memory_recall / _is_subagent 属性赋值
    class FakeHandler:
        def __init__(self, mcp_client=None):
            self._disable_memory_recall = False
            self._is_subagent = False
    monkeypatch.setattr(handler_module, "NiuHandler", FakeHandler)

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

    import agent.handler as handler_module
    import agent.runner as runner_module
    class FakeClient:
        pass
    monkeypatch.setattr(runner_module, "create_client", lambda cfg: FakeClient())
    monkeypatch.setattr(runner_module, "get_tools_schema", lambda: [])
    class FakeHandler:
        def __init__(self, mcp_client=None):
            self._disable_memory_recall = False
            self._is_subagent = False
    monkeypatch.setattr(handler_module, "NiuHandler", FakeHandler)

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
'COMPACT_TRUNCATED' 让 compat.py 识别并触发应急清空。

用字符串而非异常，避免改 call_subagent 返回签名。"
```

---

## Task 7: 模式二 task prompt 重写 + 应急清空

> **⚠️ 回退改造说明**：Task 7 已实施（commit `432fc603` + `4cc5f4a0`）。当前模式二已实施**旧版降级循环**（`for attempt in range(3)` + 降目标 + 缩短提示），需要回退改造为**单次调用 + 应急清空**：
> - 删除 `for attempt in range(3)` 循环
> - 改为单次调用 `call_subagent`
> - 截断时（`compress_result == "COMPACT_TRUNCATED"`）调用新增的 `_emergency_clear` 函数（保留最近 10 条，上面全删，最旧改"压缩失败"摘要）
> - 删除"降级 prompt 追加缩短提示"逻辑
> - 删除降级测试 `test_mode2_degradation_first_truncate_second_success` / `test_mode2_degradation_all_three_truncate`
> - 新增应急清空测试

**Files:**
- Modify: `niu_api/compat.py:1860-1910`（task prompt 重写 + call_subagent 单次调用 + 截断应急清空 + llm_config 注入 max_tokens）
- Modify: `niu_api/compat.py`（**新增 `_emergency_clear` 函数**）
- Test: `tests/test_compress_quality.py`（**删降级测试，新增应急清空测试**）

- [ ] **Step 1: 读模式二 task prompt 和 run_context_manager_mode2 现状**

读 `niu_api/compat.py:1860-1910` 确认当前 task prompt 和 `run_context_manager_mode2` 函数（Task 7 已实施旧版降级循环）。

确认旧版降级循环代码位置（`for attempt in range(3)` + `_compress_target_tokens = int(_compress_target_tokens * 0.5)` + `prompt + 缩短提示`），需要整体替换。

- [ ] **Step 2: 新增 _build_mode2_prompt 函数 + 重写 prompt**

把模式二 task prompt 构造抽成函数 `_build_mode2_prompt`（放在 `_strip_analysis` 函数之后，约 L495 附近）。这个函数返回完整的 prompt 字符串，接收 `display_tokens` / `_compress_target_tokens` / `usage_percent` / `compress_history` 参数。

**注意**：Task 7 已实施时这个函数已存在（旧版降级循环用）。回退改造时函数本身保留（单次调用也要构造 prompt），只是调用处从循环改为单次。

函数内容（返回 Step 2 描述的 prompt 字符串）：

```python
def _build_mode2_prompt(display_tokens: int, compress_target_tokens: int, usage_percent: float, compress_history: list) -> str:
    """构造模式二 task prompt（含压缩方法论 + analysis 草稿块）。

    单次调用构造一次 prompt（不再降级重压循环，截断时走应急清空）。
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
- 调用处（Step 3 单次调用）传 `_compress_target_tokens`（带下划线的局部变量）作为参数
- 原模式二分支 L1860-1882 的内联 prompt 构造删除，改为调用 `_build_mode2_prompt(...)`

- [ ] **Step 2.5: 改造跳过压缩判断用 _read_compress_target_tokens**

读 `niu_api/compat.py:1810-1834` 确认模式二跳过判断逻辑。

当前代码（L1815-1816）用百分比算 target_tokens：
```python
            elif _is_mode2:
                target_threshold = _read_target_threshold()
                target_tokens = int(context_window_tokens * target_threshold)
                suggest_release = max(display_tokens - target_tokens, 0)
```

改为用绝对值配置（与 prompt 一致）：
```python
            elif _is_mode2:
                target_tokens = _read_compress_target_tokens()
                suggest_release = max(display_tokens - target_tokens, 0)
```

同时删除 L1827-1834 的 `_compress_target` 构造（模式二不再用这个变量，prompt 由 `_build_mode2_prompt` 内联方法论）。保留 L1818-1825 的跳过判断（suggest_release == 0 或 < 5% 跳过）。

注意：`_compress_target` 变量保留给模式一用（spec 明确），只删除模式二分支里对它的赋值。如果模式一分支（`else` L1883+）仍用 `_compress_target`，保留那里的构造。

- [ ] **Step 3: 改造 run_context_manager_mode2 为单次调用 + 应急清空 + max_tokens 注入**

> **前置依赖**：本 Step 的代码引用了 `_emergency_clear` 函数，需先完成 Step 3.5（定义 `_emergency_clear`）再改本 Step。实施时可以先做 Step 3.5 再做 Step 3。

读 `niu_api/compat.py:1900-1910` 确认 `run_context_manager_mode2` 和 `await asyncio.to_thread(run_context_manager_mode2)` 的现状（Task 7 已实施旧版降级循环）。

**旧版代码（需回退改造）**：当前是 `for attempt in range(3)` 循环 + 降目标 + 缩短提示 + 3 次截断放弃。

**改为（单次调用 + 截断应急清空）**：

```python
            # llm_config 动态注入 max_tokens（通过 litellm_kwargs）
            llm_config_with_max = dict(llm_config)
            llm_config_with_max["litellm_kwargs"] = {
                **llm_config.get("litellm_kwargs", {}),
                "max_tokens": _read_max_output_tokens(),  # 动态算：contextWindowSize × 0.16，封顶 65536
            }

            # 单次调用（不重试，截断时走应急清空）
            _compress_target_tokens = _read_compress_target_tokens()
            prompt = _build_mode2_prompt(display_tokens, _compress_target_tokens, usage_percent, compress_history)

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

            # 截断时触发应急清空（保留最近 10 条，上面全删，最旧改"压缩失败"摘要）
            if compress_result == "COMPACT_TRUNCATED":
                logger.warning("[Compact] Mode-2 output truncated, triggering emergency clear")
                return await _emergency_clear(
                    history=compress_history,
                    msg_ids=compress_msg_ids,  # 与 compress_history 等长同顺序的真实 ID 列表（来自 _build_compress_history 的 out_msg_ids 出参）
                    protect_recent_count=10,
                    store=message_store,
                    session_id=session_id,
                    mode="sleep",
                )

            # 正常返回，剥离 <analysis> 草稿块（在解析前）
            logger.info(f"[Tidy] Mode-2: context-manager completed, length={len(compress_result)}")
            compress_result = _strip_analysis(compress_result)

            # 后续解析逻辑（原有代码 L1917+，用 compress_result 变量）
            # 从 LLM content 解析序号格式压缩方案
            _idx_to_id: dict[int, str] = {}
            ...
```

**回退改造关键修正点**：

1. **删除 `for attempt in range(3)` 循环**——单次调用 `call_subagent`，不重试。

2. **删除降级逻辑**：
   - 删除 `_compress_target_tokens = int(_compress_target_tokens * 0.5)`（降目标）
   - 删除 `prompt = prompt + "\n\n注意：上次输出被截断，请精简 <analysis> 块..."`（缩短提示）
   - 删除 3 次截断放弃的 `return {"status": "skipped", "mode": "sleep", "reason": "all attempts truncated"}`

3. **截断时调用 `_emergency_clear`**：
   - `compress_result == "COMPACT_TRUNCATED"` 时直接 return 应急清空结果
   - 返回 `{"status": "skipped", "mode": "sleep", "reason": "truncated, emergency cleared"}`
   - `_emergency_clear` 内部完成：保留最近 10 条 + 上面全删 + 最旧改"压缩失败"摘要 + 写回 DB

4. **`_strip_analysis` 在正常路径调用**：截断时直接 return 不走剥离；正常返回时剥离 analysis 再接原有解析。

5. **变量名用 `compress_result`**（与下游 L1915/L1924 的 `compress_result.splitlines()` 一致），不用 `result`。

6. **`is_stop_requested` 检查保留**：放在 `compress_result` 赋值之后、截断检测之前。

7. **`message_store` 参数**：`_emergency_clear` 需要 MessageStore 来 delete_messages / update_message。读 `_tidy_context_impl` 确认 message_store 变量名（可能是 `message_store` 或 `store` 或从 `get_message_store()` 拿到的对象），传给 `_emergency_clear`。

读实际代码确认 L1911-1915 的 `is_stop_requested` / `logger.info` 位置，单次调用改造时保留这些检查。

- [ ] **Step 3.5: 新增 `_emergency_clear` 函数**

在 `niu_api/compat.py` 的 `_strip_analysis` 函数之后（约 L500 附近）新增应急清空函数。

> **⚠️ I4 签名对齐说明**：本函数**已实施**（compat.py:703）。计划文档早期版本写的签名是 `_emergency_clear(history, protect_recent_count, store, session_id, mode)`（缺 `msg_ids`），但实际实施时为了处理 `history` 是 `list[dict]` 无 `.id` 字段的问题，**已加 `msg_ids` 参数**。下面签名与已实施代码（compat.py:703-710）保持一致：

```python
async def _emergency_clear(
    history: list,
    msg_ids: list,
    protect_recent_count: int,
    store,
    session_id: str,
    mode: str,
) -> dict:
    """截断时的应急清空：保留最近 N 条，上面全删，最旧那条改为"压缩失败"摘要。

    - history: 压缩历史消息列表（受保护消息已排除），按 idx 顺序排列（list[dict]，无 id 字段）
    - msg_ids: 与 history 等长、同顺序的真实 message_id 列表（来自 out_msg_ids）
    - protect_recent_count: 保留最近条数（默认 10）
    - store: MessageStore，用于 delete_messages_by_ids / update_message
    - session_id: 会话 ID（仅用于日志，delete_messages_by_ids 不需要）
    - mode: "sleep" 或 "force"（用于返回值）
    
    返回 {"status": "skipped", "mode": mode, "reason": "truncated, emergency cleared"}
    """
    if len(history) <= protect_recent_count:
        # 历史不足 10 条，无需清空，直接返回 skipped
        logger.warning(f"[Compact] history len {len(history)} <= {protect_recent_count}, no clear needed")
        return {"status": "skipped", "mode": mode, "reason": "truncated, no clear needed (too few)"}

    # 保留最近 protect_recent_count 条，上面的全删（用 msg_ids 取真实 ID，不用 history 的 .id）
    to_delete_ids = msg_ids[:-protect_recent_count]

    # 最旧那条（保留区第一条，即 msg_ids[-protect_recent_count]）改为"压缩失败"摘要
    oldest_kept_id = msg_ids[-protect_recent_count]
    await store.update_message(
        message_id=oldest_kept_id,
        content="[压缩失败，历史信息丢失] 上下文压缩时 LLM 输出截断，此条之上的历史已删除。可通过 journal.md 和知识图谱回溯。",
    )

    # 删除上面的消息
    await store.delete_messages_by_ids(to_delete_ids)

    logger.warning(f"[Compact] Emergency cleared: deleted {len(to_delete_ids)} msgs, kept recent {protect_recent_count}, marked oldest as lost-summary")
    return {"status": "skipped", "mode": mode, "reason": "truncated, emergency cleared"}
```

**说明**：
- `_emergency_clear` 是 async 函数（store 是 async 接口），在 `_tidy_context_impl` 里用 `await` 调用
- **`msg_ids` 参数是关键**：`history` 是 `list[dict]`（无 `.id` 字段，直接 `[m.id for m in history]` 会 AttributeError）。调用方必须传 `msg_ids`（与 history 等长同顺序的真实 ID 列表，来自 `_build_compress_history` 的 `out_msg_ids` 出参）。参考 compat.py L2791-2798 调用处：`_emergency_clear(history=_force_history, msg_ids=_force_msg_ids, ...)`
- 保留最近 10 条（`msg_ids[-10:]`），上面全删（`msg_ids[:-10]`）
- 最旧那条（保留区第一条 `msg_ids[-10]`）content 改为"压缩失败"摘要
- 返回 dict 与 `_tidy_context_impl` 其他 return 一致（避免返回 None 导致调用方 KeyError）
- 不调用 `/new`（用户功能），压缩函数内部完成清空

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

- [ ] **Step 5: 确认 _strip_analysis 已在正常路径调用**

Step 3 的代码里：截断时直接 return（走 `_emergency_clear`），正常返回时才调用 `compress_result = _strip_analysis(compress_result)`（在截断检测之后、解析之前）。确认这一行存在，且变量名用 `compress_result`（与下游 L1924 的 `compress_result.splitlines()` 一致）。

读改造后的代码确认：
- 截断检测 `if compress_result == "COMPACT_TRUNCATED": return await _emergency_clear(...)`——直接 return，不走剥离
- 正常路径 `compress_result = _strip_analysis(compress_result)` 剥离 analysis 块
- 下游 `for line in compress_result.splitlines():` 解析 keep/update（原有代码 L1924，变量名不变）

**不要**用 `result` 变量名——模式二下游解析用的是 `compress_result`，改名会导致 L1915/L1924 引用断裂。

- [ ] **Step 6: 语法检查**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -c "import ast; ast.parse(open('niu_api/compat.py').read())"`
Expected: 无输出

- [ ] **Step 7: 写集成测试 — 模式二 prompt 含方法论 + 单次调用**

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
    monkeypatch.setattr(compat, "_read_max_output_tokens", lambda: 32000, raising=False)  # 动态算值（200K × 0.16）

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
    assert captured["llm_config"].get("litellm_kwargs", {}).get("max_tokens") == 32000  # 动态算值
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
git commit -m "refactor(compat): 模式二回退降级循环改为单次调用 + 应急清空

回退改造 Task 7 旧版（降级重压循环）：
- 删除 for attempt in range(3) 循环（降目标 + 缩短提示 + 3 次截断放弃）
- 改为单次调用 call_subagent
- 截断时（compress_result == COMPACT_TRUNCATED）触发 _emergency_clear：
  保留最近 10 条，上面全删，最旧那条改为'压缩失败，历史信息丢失'摘要
  写回 messages DB
  返回 {status: skipped, mode: sleep, reason: 'truncated, emergency cleared'}
- 新增 _emergency_clear async 函数（应急清空逻辑）

task prompt 重写保留（内联压缩方法论 + <analysis> 草稿块，Task 7 旧版已做）。
max_tokens 通过 llm_config[litellm_kwargs] 动态注入（_read_max_output_tokens 动态算）。

理由：降级重压方向反效果（目标降得越低 LLM 要写更长 analysis 更易截断）。
靠 journal.md + 知识图谱（entity-extractor/dream-evolver/journal-agent 三层
前置兜底）让主 Agent 读回历史，应急清空安全。"
```

---

## Task 8: 模式三 task prompt 重写 + 应急清空

> **说明**：Task 8 未实施。与 Task 7 对齐，模式三也用"单次调用 + 应急清空"（不重试不降级）。复用 Task 7 新增的 `_emergency_clear` 函数。

**Files:**
- Modify: `niu_api/compat.py:2511-2563`（模式三 task prompt + run_context_manager_force 单次调用 + 截断应急清空）

- [ ] **Step 1: 读模式三 task prompt 和 run_context_manager_force 现状**

读 `niu_api/compat.py:2511-2563` 确认当前 task prompt 和 `run_context_manager_force` 函数。

- [ ] **Step 2: 新增 _build_force_prompt 函数 + 重写 prompt**

把模式三 task prompt 构造抽成函数 `_build_force_prompt`（放在 `_build_mode2_prompt` 之后，约 L500 附近）。接收 `display_tokens` / `compress_target_tokens` / `usage_percent` / `_force_history` / `last_compress_id` / `_dream_idx_in_force` 参数。

函数内容（返回 Step 2 描述的 prompt 字符串，模式三比模式二多 cursor 行 + dream 安全边界）：

```python
def _build_force_prompt(display_tokens: int, compress_target_tokens: int, usage_percent: float,
                        force_history: list, last_compress_id: str | None, dream_idx_in_force: int) -> str:
    """构造模式三 force task prompt（含压缩方法论 + analysis 草稿块 + cursor + dream 安全边界）。

    单次调用构造一次 prompt（不再降级重压循环，截断时走应急清空）。
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
- 调用处（Step 3 单次调用）传 `_force_history` / `_dream_idx_in_force`（带下划线的现有变量）作为参数
- 原模式三分支 L2511-2548 的内联 prompt 构造删除，改为调用 `_build_force_prompt(...)`

- [ ] **Step 2.5: 改造 target_tokens 计算用 _read_compress_target_tokens**

读 `niu_api/compat.py:2473` 确认模式三 target_tokens 计算。

当前代码（L2473）：
```python
            target_tokens = int(context_window_tokens * _read_target_threshold())
```

改为用绝对值配置（与 prompt 一致）：
```python
            target_tokens = _read_compress_target_tokens()
```

注意：这个 `target_tokens` 后续可能用于 force 分支的跳过判断或其他逻辑（读 L2473 之后的代码确认）。改后逻辑不变，只是数据源从百分比换成绝对值。

- [ ] **Step 3: 改造 run_context_manager_force 为单次调用 + 应急清空 + max_tokens 注入**

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

改为（单次调用 + llm_config 注入 max_tokens + 截断应急清空）：

```python
            # llm_config 动态注入 max_tokens（通过 litellm_kwargs）
            llm_config_with_max = dict(llm_config)
            llm_config_with_max["litellm_kwargs"] = {
                **llm_config.get("litellm_kwargs", {}),
                "max_tokens": _read_max_output_tokens(),  # 动态算：contextWindowSize × 0.16，封顶 65536
            }

            # 单次调用（不重试，截断时走应急清空）
            _compress_target_tokens = _read_compress_target_tokens()
            prompt = _build_force_prompt(
                display_tokens, _compress_target_tokens, usage_percent,
                _force_history, last_compress_id, _dream_idx_in_force
            )

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

            # 截断时触发应急清空（保留最近 10 条，上面全删，最旧改"压缩失败"摘要）
            if result == "COMPACT_TRUNCATED":
                logger.warning("[Compact] Force output truncated, triggering emergency clear")
                return await _emergency_clear(
                    history=_force_history,
                    msg_ids=_force_msg_ids,  # 与 _force_history 等长同顺序的真实 ID 列表（来自 _build_compress_history 的 out_msg_ids 出参）
                    protect_recent_count=10,
                    store=message_store,
                    session_id=session_id,
                    mode="force",
                )

            # 正常返回，剥离 <analysis> 草稿块（在解析前）
            logger.info(f"[Tidy] Force: context-manager completed, length={len(result)}")
            result = _strip_analysis(result)

            # 后续解析逻辑（原有代码 L2572+，用 result 变量）
            new_compress_id = last_compress_id
            try:
                keep_idxs: set[int] = set()
                ...
```

**关键修正点**：

1. **删除 `for attempt in range(3)` 循环**——单次调用 `call_subagent`，不重试。

2. **删除降级逻辑**：
   - 删除 `_compress_target_tokens = int(_compress_target_tokens * 0.5)`（降目标）
   - 删除 `prompt = prompt + "\n\n注意：上次输出被截断..."`（缩短提示）
   - 删除 3 次截断放弃的 `return {"status": "skipped", "mode": "force", "reason": "all attempts truncated"}`

3. **截断时调用 `_emergency_clear`**（Task 7 新增的 async 函数，模式三复用）：
   - `result == "COMPACT_TRUNCATED"` 时直接 return 应急清空结果
   - 传 `mode="force"` 区分模式
   - 返回 `{"status": "skipped", "mode": "force", "reason": "truncated, emergency cleared"}`

4. **`_strip_analysis` 在正常路径调用**：截断时直接 return 不走剥离；正常返回时剥离 analysis 再接原有解析（L2572+ 的 try 块）。

5. **变量名用 `result`**（与下游 L2577 的 `result.splitlines()` 一致），模式三现状就是 `result`，不改名。

6. **`is_stop_requested` 检查保留**：放在 `result` 赋值之后、截断检测之前。

7. **`new_compress_id = last_compress_id` 保留**：原有 L2571 的初始化，在剥离 analysis 之后、解析之前。

8. **`message_store` 参数**：`_emergency_clear` 需要 MessageStore。读 `_tidy_context_impl` 确认 message_store 变量名，传给 `_emergency_clear`。

- [ ] **Step 4: 确认 _strip_analysis 已在正常路径调用**

Step 3 的代码里：截断时直接 return（走 `_emergency_clear`），正常返回时才调用 `result = _strip_analysis(result)`（在截断检测之后、解析之前）。确认这一行存在，且变量名用 `result`（与下游 L2577 的 `result.splitlines()` 一致）。

读改造后的代码确认：
- 截断检测 `if result == "COMPACT_TRUNCATED": return await _emergency_clear(...)`——直接 return，不走剥离
- 正常路径 `result = _strip_analysis(result)` 剥离 analysis 块
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
    monkeypatch.setattr(compat, "_read_max_output_tokens", lambda: 32000, raising=False)  # 动态算值（200K × 0.16）

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
    assert captured["llm_config"].get("litellm_kwargs", {}).get("max_tokens") == 32000  # 动态算值
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
git commit -m "feat(compat): 模式三 task prompt 写回完整方法论 + 应急清空

模式三 force 分支改造（与模式二对齐）：
- task prompt 内联压缩方法论 + <analysis> 草稿块
- 保留 cursor= 输出行 + dream 安全边界（模式三特有）
- 单次调用 call_subagent（不重试不降级）
- 截断时触发 _emergency_clear（复用 Task 7 新增函数，mode='force'）：
  保留最近 10 条，上面全删，最旧改'压缩失败'摘要
  返回 {status: skipped, mode: force, reason: 'truncated, emergency cleared'}
- max_tokens 通过 llm_config[litellm_kwargs] 动态注入（_read_max_output_tokens 动态算）
- 解析前 _strip_analysis 剥离草稿块"
```

---

## Task 9: 删除校验兜底逻辑

> **⚠️ 一致性说明**：删校验兜底必须**同步 runner.py**（不能只改 compat.py）。runner.py 的 `_on_context_high_usage` 也有相同的"update idx 自动补 keep / cursor 降级取 max"校验兜底逻辑，必须在 Task 10.5 改造 runner.py 时一并删除。本 Task 只处理 compat.py 的校验兜底，runner.py 的同步删除由 Task 10.5 Step 处理。

**Files:**
- Modify: `niu_api/compat.py:1946-1960`（模式二删校验兜底）
- Modify: `niu_api/compat.py:2606-2629`（模式三删校验兜底）
- **runner.py 的同步删除由 Task 10.5 处理**（不在本 Task 范围）

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

当前 cursor 降级代码（L2624-2627，实际变量名 `new_compress_id`）：
```python
                # cursor 转换为 UUID
                if cursor_idx and cursor_idx in _f_idx_to_id:
                    new_compress_id = _f_idx_to_id[cursor_idx]
                elif _f_idx_to_id:
                    new_compress_id = _f_idx_to_id[max(_f_idx_to_id.keys())]
```

改为（删除 elif 降级，cursor 不在映射里就保留原 `new_compress_id` 值——L2571 已初始化为 `last_compress_id`）：
```python
                # cursor 转换为 UUID
                if cursor_idx and cursor_idx in _f_idx_to_id:
                    new_compress_id = _f_idx_to_id[cursor_idx]
                else:
                    logger.warning(f"[Compact] Force cursor idx {cursor_idx} not in mapping, keeping last_compress_id")
                    # new_compress_id 保持 L2571 的初始值（last_compress_id）
```

注意：`new_compress_id` 在 L2571 已初始化为 `last_compress_id`，所以 else 分支不需要重新赋值，保留原值即可。删除 `elif _f_idx_to_id: new_compress_id = _f_idx_to_id[max(...)]` 这个降级（不自动取 max）。

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
    monkeypatch.setattr(compat, "_read_max_output_tokens", lambda: 32000, raising=False)  # 动态算值（200K × 0.16）

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

- [ ] **Step 8.5: 补应急清空测试 + analysis 缺失测试**

在 `tests/test_compress_quality.py` 追加两个测试（**删除旧版降级循环测试** `test_mode2_degradation_first_truncate_second_success` / `test_mode2_degradation_all_three_truncate`，如果已实施的话）：

```python
def test_mode2_truncate_triggers_emergency_clear(monkeypatch):
    """模式二截断时触发应急清空：保留最近 10 条，上面全删，最旧改"压缩失败"摘要。"""
    import asyncio
    import niu_api.compat as compat
    import niu_api.chat as chat_module
    import agent.subagent as subagent_module

    # 15 条消息，截断时应保留最近 10 条（msg-6 到 msg-15），删 msg-1 到 msg-5，
    # 最旧保留那条（msg-6）改为"压缩失败"摘要
    messages = [FakeMsg(id=f"msg-{i}", role="user", content=f"内容{i}") for i in range(1, 16)]

    deleted_ids = []
    updated_ids = []

    class FakeStore:
        async def get_messages(self, limit=None, before_id=None):
            return messages
        async def delete_messages(self, session_id, message_ids):
            deleted_ids.extend(message_ids)
            return len(message_ids)
        async def update_message(self, message_id=None, content=None, **kw):
            updated_ids.append((message_id, content))
            return True

    async def fake_get_message_store():
        return FakeStore()

    class FakeRunner:
        handler = type("H", (), {"_last_prompt_tokens": 180000})()
        llm_config = {}

    def fake_get_or_create_runner():
        return FakeRunner()

    # 单次调用返回 COMPACT_TRUNCATED（截断）
    call_count = {"n": 0}
    def fake_call_subagent(*args, **kwargs):
        agent_name = kwargs.get("agent_name") or (args[0] if args else "")
        if agent_name == "context-manager":
            call_count["n"] += 1
            return "COMPACT_TRUNCATED"  # 截断
        return "skip"

    monkeypatch.setattr(compat, "get_message_store", fake_get_message_store)
    monkeypatch.setattr(chat_module, "get_or_create_runner", fake_get_or_create_runner)
    monkeypatch.setattr(subagent_module, "call_subagent", fake_call_subagent)
    monkeypatch.setattr(compat, "_read_context_window_tokens", lambda: 200000, raising=False)
    monkeypatch.setattr(compat, "_read_warning_threshold", lambda: 0.8, raising=False)
    monkeypatch.setattr(compat, "_read_protect_recent_count", lambda: 0, raising=False)
    monkeypatch.setattr(compat, "_write_cursor_with_lock", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(compat, "_read_compress_target_tokens", lambda: 60000, raising=False)
    monkeypatch.setattr(compat, "_read_max_output_tokens", lambda: 32000, raising=False)  # 动态算值

    request = {"session_id": "test", "mode": "sleep"}
    result = asyncio.run(compat._tidy_context_impl(request))

    # 验证只调用了 1 次（单次调用，不重试）
    assert call_count["n"] == 1, f"应只调用 1 次（单次调用不重试），实际 {call_count['n']}"

    # 验证返回应急清空结果
    assert result is not None
    assert isinstance(result, dict)
    assert result.get("status") == "skipped", f"应返回 skipped，实际 {result}"
    assert result.get("mode") == "sleep"
    assert "truncated" in result.get("reason", ""), f"reason 应含 truncated，实际 {result}"

    # 验证删除了上面 5 条（msg-1 到 msg-5）
    assert set(deleted_ids) == {f"msg-{i}" for i in range(1, 6)}, f"应删 msg-1 到 msg-5，实际删了 {deleted_ids}"

    # 验证最旧保留那条（msg-6）被改为"压缩失败"摘要
    assert len(updated_ids) == 1, f"应只更新 1 条（最旧保留那条），实际更新了 {len(updated_ids)} 条"
    updated_id, updated_content = updated_ids[0]
    assert updated_id == "msg-6", f"应更新 msg-6，实际更新了 {updated_id}"
    assert "压缩失败" in updated_content, f"content 应含'压缩失败'，实际 {updated_content}"
    assert "journal.md" in updated_content or "知识图谱" in updated_content, f"content 应提示回溯途径，实际 {updated_content}"

    # 验证最近 10 条（msg-6 到 msg-15）没被删（msg-6 被改摘要但没被删）
    for i in range(6, 16):
        assert f"msg-{i}" not in deleted_ids, f"msg-{i} 不应被删除（保留区）"


def test_mode2_truncate_too_few_no_clear(monkeypatch):
    """模式二截断但历史不足 10 条时，不执行清空，直接返回 skipped。"""
    import asyncio
    import niu_api.compat as compat
    import niu_api.chat as chat_module
    import agent.subagent as subagent_module

    # 只有 3 条消息，不足 10 条，不应执行删除/更新
    messages = [FakeMsg(id=f"msg-{i}", role="user", content=f"内容{i}") for i in range(1, 4)]

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

    def fake_call_subagent(*args, **kwargs):
        agent_name = kwargs.get("agent_name") or (args[0] if args else "")
        if agent_name == "context-manager":
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
    monkeypatch.setattr(compat, "_read_max_output_tokens", lambda: 32000, raising=False)

    request = {"session_id": "test", "mode": "sleep"}
    result = asyncio.run(compat._tidy_context_impl(request))

    # 验证返回 skipped（too few）
    assert result is not None
    assert result.get("status") == "skipped"
    assert "too few" in result.get("reason", ""), f"reason 应含 too few，实际 {result}"

    # 验证没删除也没更新（历史不足 10 条）
    assert deleted_ids == [], f"不应删除任何消息，实际删了 {deleted_ids}"
    assert updated_ids == [], f"不应更新任何消息，实际更新了 {updated_ids}"


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

**关键断言**（`test_mode2_truncate_triggers_emergency_clear`）：
- `call_count["n"] == 1`：单次调用，不重试（验证降级循环已删）
- `deleted_ids == {msg-1..msg-5}`：删上面 5 条
- `updated_ids == [(msg-6, 含"压缩失败")]`：最旧保留那条改摘要
- `msg-6..msg-15` 不在 deleted_ids：保留区 10 条没被删
- 返回 `{"status": "skipped", "mode": "sleep", "reason": "truncated, ..."}`

- [ ] **Step 9: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_compress_quality.py::test_mode2_no_auto_keep_fixup tests/test_compress_quality.py::test_mode2_truncate_triggers_emergency_clear tests/test_compress_quality.py::test_mode2_truncate_too_few_no_clear tests/test_compress_quality.py::test_strip_analysis_missing_then_parse -v`
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

## Task 10.5: 改造 runner.py _on_context_high_usage 对齐 compat.py 模式三

> **⚠️ 主要活跃路径**：这是模式三**主要活跃路径**的改造。模式三的触发路径是 `agent_loop.py:308` 工具调用返回后同步回调 `runner.py:_on_context_high_usage`（不是 compat.py）。compat.py/chat.py 的 force 路径服务 CONTEXT_OVERFLOW 兜底场景，不是主要路径。runner.py 当前用旧 prompt + 旧解析 + 旧文本方式 + 无截断处理，与 compat.py 完全分叉，必须改造对齐 compat.py 模式三。
>
> **同步适配**：`_on_context_high_usage` 是**同步方法**，`_emergency_clear` 是 async 函数。采用方案 C：runner.py 内联应急清空逻辑，复用现有 `_sync_delete_messages` / `_sync_update_message`（同步包装器），不用 `asyncio.run` 避免 loop 冲突。
>
> **当前轮结果天然不丢**：压缩在 response persist 之前执行（`agent_loop.py:308` 在 L432 append 之前），压缩读的是 DB 历史消息（不含当前轮 response），压缩后当前轮 response 才 append。**不需要显式追加逻辑**。

**Files:**
- Modify: `agent/runner.py:819-825`（import 追加 compat.py 的辅助函数）
- Modify: `agent/runner.py:983`（target_tokens 改用 `_read_compress_target_tokens`）
- Modify: `agent/runner.py:994-1010`（history 构造改用 `_build_compress_history`）
- Modify: `agent/runner.py:1022-1065`（prompt 替换为 `_build_force_prompt` 调用）
- Modify: `agent/runner.py:1067-1077`（call_subagent 加 max_tokens 注入 + history 参数）
- Modify: `agent/runner.py:1083+`（新增截断检测 + 内联应急清空）
- Modify: `agent/runner.py:1087`（解析前新增 `_strip_analysis` 调用）
- Modify: `agent/runner.py:1121-1126`（删除 update idx 自动补 keep，与 compat.py 对齐）
- Modify: `agent/runner.py:1138-1141`（删除 cursor 降级取 max，改为只 warn 保持 last_compress_id）
- Test: `tests/test_compress_quality.py`（新增 runner.py 模式三路径测试）

- [ ] **Step 1: 读 runner.py _on_context_high_usage 现状**

读 `agent/runner.py:970-1150` 确认 `_on_context_high_usage` 函数现状。记录以下位置的实际代码：
- L819-825 import 段（从 compat.py 导入了哪些函数）
- L983 target_tokens 计算（当前用百分比 `_read_target_threshold`）
- L994-1010 history 构造（当前手动构造，非 `_build_compress_history`）
- L1022-1065 prompt 构造（当前内联旧 prompt，非 `_build_force_prompt`）
- L1067-1077 call_subagent 调用（当前无 max_tokens 注入）
- L1083+ 返回后的处理（当前无截断检测）
- L1087 解析逻辑（当前无 `_strip_analysis`）
- L1121-1126 校验兜底（update idx 自动补 keep）
- L1138-1141 校验兜底（cursor 降级取 max）

- [ ] **Step 2: 追加 import（L819-825）**

读 `agent/runner.py:819-825` 确认现有 `from niu_api.compat import` 行。

在现有 import 列表追加（如果尚未导入）：
```python
from niu_api.compat import (
    ...,  # 现有导入
    _build_force_prompt,
    _emergency_clear,  # 仅参考逻辑，runner.py 内联实现不直接调
    _strip_analysis,
    _build_compress_history,
    _read_compress_target_tokens,
    _read_max_output_tokens,
)
```

**注意**：`_emergency_clear` 是 async 函数，runner.py 是同步方法不能直接 `await`。import 仅用于参考逻辑，实际 runner.py 内联应急清空逻辑（用 `_sync_delete_messages` / `_sync_update_message`），不直接调用 `_emergency_clear`。如果 import 会触发循环依赖，则不 import `_emergency_clear`，只在内联实现里复刻其逻辑。

- [ ] **Step 3: 改造 target_tokens（L983）**

当前代码（L983 概要）：
```python
            target_tokens = int(context_window_tokens * _read_target_threshold())
```

改为用绝对值配置（与 compat.py 模式三一致）：
```python
            target_tokens = _read_compress_target_tokens()
```

- [ ] **Step 4: 改造 history 构造（L994-1010）**

读 L994-1010 确认当前 history 构造方式（手动构造 list + idx_to_id 映射）。

**删除旧代码**：删除 runner.py L994-1010 现有的 `_build_incremental_msg_text` 调用 + `msg_list_text` 变量 + `_f_pids`/`protected_force_ids` 手动构造（改由 `_build_compress_history` 内部处理 `exclude_protected`）+ L1000 的 `.replace('条新消息','条消息')`。新 prompt 用 `_build_force_prompt` 不需要 `msg_list_text`。

改为调用 `_build_compress_history`（与 compat.py 模式三 L2737-2751 一致）。**注意实际签名**（compat.py:396）：`(messages, msg_tokens=None, out_msg_ids=None, protect_recent=0, exclude_protected=False)`，返回 `(history_list, idx_to_id_dict)`：

```python
            # 构造 history 列表 + idx 映射（参考 compat.py L2737-2751 模式三）
            _force_msg_ids = []
            _force_history, _f_idx_to_id = _build_compress_history(
                db_messages, msg_tokens,
                out_msg_ids=_force_msg_ids,
                protect_recent=protect_recent_count,
                exclude_protected=True,
            )
            # 构造反向映射 id→idx（用于 dream 安全边界计算）
            _f_id_to_idx = {mid: idx for idx, mid in _f_idx_to_id.items()}
```

**关键点**：
- 实际签名是位置参数 `(messages, msg_tokens, out_msg_ids=..., protect_recent=..., exclude_protected=...)`，**没有** `last_compress_id` / `dream_idx` / `protect_recent_count` 这三个参数（旧计划写错了）。
- `out_msg_ids=_force_msg_ids` 是出参：函数内部 append 真实 message_id 到此 list，与 `_force_history` 等长同顺序（用于 Step 7 应急清空删真实 ID，避免 dict 取 `.id` 的 AttributeError）。
- `_f_idx_to_id` 是 `{idx: message_id}` 映射（idx 从 1 开始，由 compat.py 内部 enumerate +1 构造）。
- `_f_id_to_idx` 是反向映射 `{message_id: idx}`，Step 5 计算 dream 安全边界 idx 用。

- [ ] **Step 5: 改造 prompt（L1022-1065）**

读 L1022-1065 确认当前内联 prompt 构造。

删除内联 prompt，改为调用 `_build_force_prompt`（与 compat.py 模式三 L2759-2762 一致）。**前置补一段 `_dream_idx_in_force` 计算**（runner.py 当前没这个变量，参考 compat.py L2753-2757）：

```python
            # 计算 dream 安全边界 idx（参考 compat.py L2753-2757）
            # new_dream_id 在 runner.py 前面 dream-evolver 阶段已算出
            # _f_id_to_idx 是 Step 4 构造的反向映射 {message_id: idx}
            if not new_dream_id:
                _dream_idx_in_force = 0
            else:
                _dream_idx_in_force = _f_id_to_idx.get(new_dream_id, len(_force_msg_ids))

            # 复用 Step 3 的 target_tokens（不重复读配置，避免 I3 重复读）
            prompt = _build_force_prompt(
                display_tokens, target_tokens, usage_percent,
                _force_history, last_compress_id, _dream_idx_in_force
            )
```

**关键点**：
- **不重复读配置**：Step 3 已 `target_tokens = _read_compress_target_tokens()`，本 Step 复用 `target_tokens`，不再写 `_compress_target_tokens = _read_compress_target_tokens()`（避免重复读配置）。
- `_dream_idx_in_force` 必须在调用 `_build_force_prompt` 之前算出（旧计划漏了这段计算）。
- `new_dream_id` 在 runner.py 前面 dream-evolver 阶段已算出，`_f_id_to_idx` 是 Step 4 构造的反向映射。
- 当 dream 不在 force history 里时，`_dream_idx_in_force = len(_force_msg_ids)`（越界值，由 `_build_force_prompt` 内部判断"无 dream 约束"）。

读 compat.py 模式三的 `_build_force_prompt` 调用处（L2759-2762）确认参数顺序，保持一致。

- [ ] **Step 6: 改造 call_subagent（L1067-1077）**

读 L1067-1077 确认当前 call_subagent 调用。

加 max_tokens 注入 + history 参数（与 compat.py 模式三一致）：
```python
            # llm_config 动态注入 max_tokens（通过 litellm_kwargs）
            llm_config_with_max = dict(llm_config)
            llm_config_with_max["litellm_kwargs"] = {
                **llm_config.get("litellm_kwargs", {}),
                "max_tokens": _read_max_output_tokens(),  # 动态算：contextWindowSize × 0.16，封顶 65536
            }

            def run_context_manager_force():
                return call_subagent(
                    agent_name="context-manager",
                    task=prompt,
                    llm_config=llm_config_with_max,
                    mcp_client=None,
                    context_fifo_threshold=0,
                    history=_force_history,
                )

            result = run_context_manager_force()  # 同步调用，不用 asyncio.to_thread
```

**注意**：runner.py 是同步方法，直接调用 `run_context_manager_force()`，不用 `asyncio.to_thread`（与 compat.py 的 `await asyncio.to_thread(...)` 不同）。

- [ ] **Step 7: 新增截断检测 + 内联应急清空（L1083+）**

在 `result = run_context_manager_force()` 之后、解析之前，新增截断检测 + 内联应急清空：

```python
            # 截断时触发内联应急清空（保留最近 10 条，上面全删，最旧改"压缩失败"摘要）
            # 同步实现：用 self._sync_delete_messages / self._sync_update_message，不调 async _emergency_clear
            if result == "COMPACT_TRUNCATED":
                logger.warning("[Compact] runner.py force output truncated, triggering emergency clear")
                # 内联应急清空（同步版，复用 runner.py 现有 _sync_* 方法）
                if len(_force_msg_ids) <= 10:
                    logger.warning(f"[Compact] Runner history len {len(_force_msg_ids)} <= 10, no clear needed")
                    return {"status": "skipped", "mode": "force", "reason": "truncated, no clear needed (too few)"}

                delete_ids = _force_msg_ids[:-10]
                oldest_kept_id = _force_msg_ids[-10]

                # 删除上面的消息（_sync_delete_messages 只接收 msg_ids，不接收 session_id）
                self._sync_delete_messages(delete_ids)

                # 最旧保留条改为"压缩失败"摘要
                self._sync_update_message(
                    message_id=oldest_kept_id,
                    content="[压缩失败，历史信息丢失] 上下文压缩时 LLM 输出截断，此条之上的历史已删除。可通过 journal.md 和知识图谱回溯。",
                )

                logger.warning(f"[Compact] Runner emergency cleared: deleted {len(delete_ids)} msgs, kept recent 10")
                return {"status": "skipped", "mode": "force", "reason": "truncated, emergency cleared"}
```

**关键修正**：
- **C1 签名修正**：`_sync_delete_messages` 实际签名是 `_sync_delete_messages(self, msg_ids)`（runner.py:630），**只接收 `msg_ids`，不接收 `session_id`**。且是实例方法，必须用 `self._sync_delete_messages(delete_ids)` 调用，不能写 `_sync_delete_messages(session_id, delete_ids)`。
- **C4 用 `_force_msg_ids` 替代 dict 的 `.id`**：`_force_history` 是 `list[dict]`（dict 没 `.id` 属性，会 AttributeError）。改用 `_force_msg_ids`（Step 4 由 `out_msg_ids` 出参填充的真实 ID 列表），与 `_force_history` 等长同顺序。`delete_ids = _force_msg_ids[:-10]`、`oldest_kept_id = _force_msg_ids[-10]`。
- **C2 已在 Step 5 补 `_dream_idx_in_force`**：本 Step 不涉及，仅说明依赖关系。

**注意**：
- `self._sync_delete_messages` / `self._sync_update_message` 是 runner.py 现有的同步包装器（runner.py:630 / runner.py:659）。`_sync_update_message` 签名是 `(self, message_id, content, clear_tool_calls=False)`。
- 不直接 `await _emergency_clear(...)`（runner.py 是同步方法，不能 await）。
- 不用 `asyncio.run(_emergency_clear(...))`（可能与现有 loop 冲突，方案 C 明确避免）。
- 保留最近 10 条 + 上面全删 + 最旧改"压缩失败"摘要，逻辑与 compat.py 的 `_emergency_clear` 完全一致（参考 compat.py L2791-2798 调用处，传 `msg_ids=_force_msg_ids`）。

- [ ] **Step 8: 新增 _strip_analysis 调用（L1087 解析前）**

在截断检测之后、解析之前，新增 `_strip_analysis` 调用：
```python
            # 正常返回，剥离 <analysis> 草稿块（在解析前）
            logger.info(f"[Tidy] runner.py force: context-manager completed, length={len(result)}")
            result = _strip_analysis(result)
```

- [ ] **Step 9: 删除 update idx 自动补 keep（L1121-1126）**

读 L1121-1126 确认当前校验兜底代码（与 compat.py 模式三 L2606-2611 一致）：
```python
            # update idx 必须在 keep 中，自动补齐
            missing_in_keep = set(update_idxs) - keep_idxs
            if missing_in_keep:
                logger.warning(...)
                keep_idxs |= missing_in_keep
```

删除这段代码（与 Task 9 compat.py 对齐）。LLM 输出什么就用什么，不自动补。

- [ ] **Step 10: 删除 cursor 降级取 max（L1138-1141）**

读 L1138-1141 确认当前 cursor 降级代码（与 compat.py 模式三 L2624-2627 一致）：
```python
                if cursor_idx and cursor_idx in _f_idx_to_id:
                    new_compress_id = _f_idx_to_id[cursor_idx]
                elif _f_idx_to_id:
                    new_compress_id = _f_idx_to_id[max(_f_idx_to_id.keys())]
```

改为删除 elif 降级，cursor 不在映射里就保留原 `new_compress_id` 值（已初始化为 `last_compress_id`）：
```python
                if cursor_idx and cursor_idx in _f_idx_to_id:
                    new_compress_id = _f_idx_to_id[cursor_idx]
                else:
                    logger.warning(f"[Compact] runner.py force cursor idx {cursor_idx} not in mapping, keeping last_compress_id")
                    # new_compress_id 保持初始值（last_compress_id）
```

- [ ] **Step 11: 语法检查**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -c "import ast; ast.parse(open('agent/runner.py').read())"`
Expected: 无输出

- [ ] **Step 12: 验证 import 不报错**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -c "from agent.runner import GenericAgentRunner; print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 13: 写集成测试 — runner.py 模式三路径 prompt 含方法论 + 截断应急清空**

在 `tests/test_compress_quality.py` 追加（参考 Task 8 的 `test_mode3_prompt_contains_methodology` 模式，但 mock `_on_context_high_usage` 的依赖）。**测试体必须补全，不能用 `pass` placeholder**：

```python
def test_runner_force_prompt_contains_methodology(monkeypatch):
    """runner.py force prompt 应含压缩方法论 + cursor + dream 安全边界。"""
    from agent import runner as runner_module
    from niu_api.compat import _build_force_prompt
    from unittest.mock import MagicMock

    # mock 依赖：call_subagent 捕获 task / llm_config / history
    captured = {"prompt": None, "llm_config": None, "history": None}

    def fake_call_subagent(*args, **kwargs):
        if kwargs.get("agent_name") == "context-manager":
            captured["prompt"] = kwargs.get("task", "")
            captured["llm_config"] = kwargs.get("llm_config", {})
            captured["history"] = kwargs.get("history")
            return "COMPACT_TRUNCATED"  # 触发应急清空分支（也测了应急清空）
        return "skip"

    runner_module.call_subagent = fake_call_subagent
    # 用真实 _build_force_prompt（验证方法论关键词）
    runner_module._build_force_prompt = _build_force_prompt

    # mock sync 包装器（不真删 DB）
    deleted_ids = []
    updated_msgs = []
    runner_module.GenericAgentRunner._sync_delete_messages = lambda self, msg_ids: deleted_ids.extend(msg_ids)
    runner_module.GenericAgentRunner._sync_update_message = lambda self, message_id, content, clear_tool_calls=False: updated_msgs.append((message_id, content))

    # 构造 runner 实例 + mock store / config（实施时参考 runner.py 现有测试模式补全：
    # 需要 GenericAgentRunner 实例 + mock message_store + mock llm_config + 构造 db_messages / msg_tokens / new_dream_id）
    # runner = _build_runner_for_test()  # 实施时补全
    # 触发 runner._on_context_high_usage(...)

    # 断言方向（实施时补全实际断言）：
    # assert "压缩方法论" in captured["prompt"]
    # assert "<analysis>" in captured["prompt"]
    # assert "cursor=" in captured["prompt"]
    # assert "安全边界" in captured["prompt"]
    # assert captured["llm_config"]["litellm_kwargs"]["max_tokens"] > 0  # 注入了 max_tokens
    # 截断分支断言：
    # assert len(deleted_ids) == len(_force_msg_ids) - 10  # 删了上面 N-10 条
    # assert updated_msgs[0][1].startswith("[压缩失败")  # 最旧改摘要


def test_runner_force_truncate_triggers_emergency_clear(monkeypatch):
    """runner.py force 截断时触发应急清空（同步版）：保留最近 10 条，上面全删，最旧改"压缩失败"摘要。"""
    from agent import runner as runner_module
    from unittest.mock import MagicMock

    # mock call_subagent 返回 COMPACT_TRUNCATED
    runner_module.call_subagent = lambda *a, **kw: "COMPACT_TRUNCATED"

    # mock sync 包装器捕获调用
    deleted_ids = []
    updated_msgs = []
    runner_module.GenericAgentRunner._sync_delete_messages = lambda self, msg_ids: deleted_ids.extend(msg_ids)
    runner_module.GenericAgentRunner._sync_update_message = lambda self, message_id, content, clear_tool_calls=False: updated_msgs.append((message_id, content))

    # 构造 runner 实例 + 触发 _on_context_high_usage（实施时补全：
    # 需要 GenericAgentRunner 实例 + mock message_store + 构造 _force_msg_ids 15 条等长 mock）
    # runner = _build_runner_for_test()
    # result = runner._on_context_high_usage(...)

    # 断言方向（实施时补全实际断言）：
    # assert result == {"status": "skipped", "mode": "force", "reason": "truncated, emergency cleared"}
    # assert len(deleted_ids) == 5  # 删了上面 5 条（15 - 10 = 5）
    # assert deleted_ids == _force_msg_ids[:-10]  # 删的是最旧那批
    # assert len(updated_msgs) == 1  # 只更新最旧保留条
    # assert updated_msgs[0][0] == _force_msg_ids[-10]  # 最旧保留条 ID
    # assert "压缩失败" in updated_msgs[0][1]  # content 含"压缩失败"
    # # 单次调用不重试：call_subagent 只被调用 1 次（用 MagicMock + assert_called_once 验证）
```

**注意**：
- runner.py 的测试比 compat.py 复杂（需要构造 `GenericAgentRunner` 实例 + mock store + mock `call_subagent`）。计划里说明"实施时参考 runner.py 现有测试模式补全构造实例的部分"，但**测试框架和断言方向必须明确**（不能只有 `pass`）。
- 实施时把上面注释掉的断言落实为真实断言，把 `_build_runner_for_test()` 辅助函数补全（构造 runner 实例 + mock 依赖）。
- 关键断言：
  - prompt 含方法论关键词（"压缩方法论"/"<analysis>"/"cursor="/"安全边界"）
  - llm_config 注入 max_tokens（`litellm_kwargs.max_tokens > 0`）
  - 截断时删上面 N-10 条 + 更新最旧保留条 + 返回 skipped
  - 单次调用（不重试，`call_subagent` 只调用 1 次）

- [ ] **Step 14: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_compress_quality.py -v -k runner_mode3`
Expected: 2 个测试 PASS

- [ ] **Step 15: 运行现有测试不破坏**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_compress_quality.py tests/test_compress_history.py -v 2>&1 | tail -30`
Expected: 无新增 FAIL

- [ ] **Step 16: 临时提交**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add agent/runner.py tests/test_compress_quality.py
git commit -m "refactor(runner): _on_context_high_usage 改造对齐 compat.py 模式三

模式三主要活跃路径改造（agent_loop.py:308 同步回调）：
- import 追加 _build_force_prompt / _strip_analysis / _build_compress_history 等
- target_tokens 改用 _read_compress_target_tokens（绝对值，不再用百分比）
- history 构造改用 _build_compress_history（返回 history + idx_to_id 映射）
- prompt 替换为 _build_force_prompt 调用（含方法论 + analysis + cursor + dream 边界）
- call_subagent 加 max_tokens 注入（llm_config[litellm_kwargs]，动态算）
- 新增截断检测 + 内联应急清空（同步实现，用 _sync_delete_messages / _sync_update_message，
  不调 async _emergency_clear，避免 loop 冲突）
- 解析前 _strip_analysis 剥离草稿块
- 删除 update idx 自动补 keep（与 Task 9 compat.py 对齐）
- 删除 cursor 降级取 max（与 Task 9 compat.py 对齐）

理由：runner.py 是模式三主要活跃路径（agent_loop.py:308 同步回调），
原用旧 prompt + 旧解析 + 无截断处理，与 compat.py 完全分叉。
当前轮结果天然不丢（压缩在 response persist 前执行，agent_loop.py:308 在 L432 之前）。"
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
Expected: 不再出现 `Total tokens of image and text exceed max message tokens`。如果有 `finish_reason=length`，验证应急清空是否触发（日志应有 `triggering emergency clear` + `Emergency cleared`）。

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
- 配置改 token 绝对值（compressTargetTokens；maxOutputTokens 动态算 contextWindowSize × 0.16 封顶 65536）
- finish_reason 传递链 + 截断应急清空（保留最近 10 条，靠 journal.md + 知识图谱回溯）
- 删除削弱约束的校验兜底
- 术语清理（L0/L1/L2 废弃 + 事务→会话单元 + 远端中端近端→三份）"
```

---

## 自审检查

### 1. Spec 覆盖

- 配置层变更（compressTargetTokens 配置 + maxOutputTokens 动态算）→ Task 1 ✅
- `_strip_analysis` 辅助函数 → Task 2 ✅
- finish_reason 传递链（MockResponse + litellm_adapter + agent_loop + call_subagent）→ Task 3-6 ✅
- `_emergency_clear` 应急清空函数 → Task 7 Step 3.5 定义 ✅
- 模式二 task prompt 重写 + 单次调用 + 截断应急清空 → Task 7 ✅
- 模式三 task prompt 重写 + 单次调用 + 截断应急清空 → Task 8 ✅
- 删除校验兜底 → Task 9（compat.py）+ Task 10.5（runner.py 同步） ✅
- 术语清理 → Task 10 ✅
- **runner.py 模式三主要路径改造 → Task 10.5** ✅
- 端到端验证 → Task 11 ✅

### 2. Placeholder 扫描

无 TBD/TODO。所有步骤包含具体代码。

### 3. 类型一致性

- `_read_compress_target_tokens() -> int`：Task 1 定义（读配置 60000），Task 7/8 使用 ✅
- `_read_max_output_tokens() -> int`：Task 1 定义（动态算 contextWindowSize × 0.16 封顶 65536），Task 7/8 使用 ✅
- `_strip_analysis(response: str) -> str`：Task 2 定义，Task 7/8 正常路径使用 ✅
- `_emergency_clear(history, msg_ids, protect_recent_count, store, session_id, mode) -> dict`：Task 7 Step 3.5 定义（**已实施 compat.py:703，含 `msg_ids` 参数**），Task 7/8 截断路径使用 ✅
- `MockResponse.finish_reason`：Task 3 定义，Task 4 填充，Task 5 传递，Task 6 检测 ✅
- `"COMPACT_TRUNCATED"` 字符串信号：Task 6 返回，Task 7/8 识别并触发 `_emergency_clear` ✅
- `llm_config_with_max`：Task 7/8 构造（max_tokens 由 `_read_max_output_tokens` 动态算），传给 call_subagent ✅

### 4. 风险点

- **模式一兼容性**：`_compress_target` 保留给模式一（Task 7/8 不动模式一），spec 明确 ✅
- **MAX_TURNS_EXCEEDED 路径**：Task 5 只改 L570/L583，L610 不带 finish_reason ✅
- **截断应急清空**：Task 7/8 单次调用，截断时调用 `_emergency_clear`（保留最近 10 条 + 上面全删 + 最旧改"压缩失败"摘要），返回 `{"status": "skipped", ...}` ✅
- **应急清空安全性**：靠 journal.md + 知识图谱（entity-extractor/dream-evolver/journal-agent 三层前置兜底）让主 Agent 读回历史，用户已改 niu.md 让主 Agent 读 journal.md ✅
- **runner.py 同步适配**：Task 10.5 内联应急清空逻辑（用 `_sync_delete_messages` / `_sync_update_message`），不调 async `_emergency_clear`，不用 `asyncio.run` 避免 loop 冲突 ✅
- **runner.py 主要路径**：Task 10.5 改造模式三主要活跃路径（`agent_loop.py:308` → `runner.py:_on_context_high_usage`），与 compat.py 兜底路径保持一致 ✅
- **当前轮结果天然不丢**：压缩在 response persist 前执行（`agent_loop.py:308` 在 L432 之前），不需要显式追加逻辑 ✅
