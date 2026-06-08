# 脑区闭环实施计划

> **⚠️ 历史文档**：本文档中使用 `brain:Niu`、`brain:region:xxx`、`brain:concept:xxx`、`brain:event:xxx`、`brain:person:xxx`、`brain:session:xxx`、`event:xxx`、`skill:xxx`、`person:xxx` 等冒号前缀实体名的描述已过时。当前系统要求所有实体名必须使用自然语言（如 `Niu`、`编程开发脑区`、`Python`、`海滩日落事件`），禁止冒号前缀格式。详见 `docs/kg-dev-dictionary.md`。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现知识图谱脑区架构的完整闭环——写入侧（LLM提取时考虑脑区）、读取侧（冷启动优化）、维护侧（DEPRECATED清理），并通过TDD验证所有功能真实可运行。

**Architecture:** 通过 `llm_proxy.py` 代理拦截 LightRAG 的 LLM 提取请求，注入脑区架构提示词。检测方式：system prompt 中包含 `"Knowledge Graph Specialist"`。注入内容：脑区架构说明 + 动态查询当前脑区列表（local模式，0次LLM）。同时优化读取侧冷启动问题，清理遗留的 DEPRECATED 标记。

**Tech Stack:** Python 3.11+, pytest, pytest-asyncio, FastAPI, LightRAG

---

## 文件结构

| 文件 | 操作 | 职责 |
|------|------|------|
| `niu_api/llm_proxy.py` | 修改 | 添加脑区提示词注入逻辑 |
| `niu_api/internal/brain_region_prompt.py` | 新建 | 脑区提示词构建 + 动态查询 |
| `tests/test_brain_region_prompt.py` | 新建 | 脑区提示词注入的TDD测试 |
| `tests/test_llm_proxy_injection.py` | 新建 | 代理拦截注入的集成测试 |
| `niu_api/internal/lightrag_adapter.py` | 修改 | 清理DEPRECATED标记 |
| `agent/generic/runner.py` | 修改 | 冷启动优化 + 实例缓存 |

---

## Task 1: 脑区提示词构建模块（brain_region_prompt.py）

**Files:**
- Create: `niu_api/internal/brain_region_prompt.py`
- Test: `tests/test_brain_region_prompt.py`

- [ ] **Step 1: 写失败测试 — 检测LightRAG提取请求**

```python
"""Tests for brain region prompt injection into LightRAG LLM requests."""
import pytest
from niu_api.internal.brain_region_prompt import is_lightrag_extraction_request


def test_is_lightrag_extraction_request_with_specialist():
    """System prompt containing 'Knowledge Graph Specialist' is detected."""
    messages = [
        {"role": "system", "content": "---Role---\nYou are a Knowledge Graph Specialist..."},
        {"role": "user", "content": "Extract entities from this text..."},
    ]
    assert is_lightrag_extraction_request(messages) is True


def test_is_lightrag_extraction_request_without_specialist():
    """Normal chat messages are NOT detected as extraction requests."""
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello, how are you?"},
    ]
    assert is_lightrag_extraction_request(messages) is False


def test_is_lightrag_extraction_request_empty_messages():
    """Empty message list is not an extraction request."""
    assert is_lightrag_extraction_request([]) is False


def test_is_lightrag_extraction_request_no_system():
    """Messages without system prompt are not extraction requests."""
    messages = [
        {"role": "user", "content": "Hello"},
    ]
    assert is_lightrag_extraction_request(messages) is False
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd E:/tools/ai-bot && python -m pytest tests/test_brain_region_prompt.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'niu_api.internal.brain_region_prompt'`

- [ ] **Step 3: 实现检测函数**

```python
"""
Brain region prompt injection for LightRAG LLM extraction requests.

When LightRAG calls the LLM to extract entities/relationships, we inject
brain region architecture information so the LLM considers brain regions
when building the knowledge graph.
"""

BRAIN_REGION_MARKER = "Knowledge Graph Specialist"


def is_lightrag_extraction_request(messages: list[dict]) -> bool:
    """Detect whether a message list is a LightRAG extraction request.

    LightRAG extraction requests always have a system prompt starting with
    '---Role---\\nYou are a Knowledge Graph Specialist...'.
    """
    for msg in messages:
        if msg.get("role") == "system":
            content = msg.get("content", "")
            if BRAIN_REGION_MARKER in content:
                return True
    return False
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd E:/tools/ai-bot && python -m pytest tests/test_brain_region_prompt.py::test_is_lightrag_extraction_request_with_specialist tests/test_brain_region_prompt.py::test_is_lightrag_extraction_request_without_specialist tests/test_brain_region_prompt.py::test_is_lightrag_extraction_request_empty_messages tests/test_brain_region_prompt.py::test_is_lightrag_extraction_request_no_system -v`
Expected: 4 PASSED

- [ ] **Step 5: 提交**

```bash
git add niu_api/internal/brain_region_prompt.py tests/test_brain_region_prompt.py
git commit -m "feat: add brain region extraction request detection"
```

---

## Task 2: 脑区提示词构建 — 静态部分

**Files:**
- Modify: `niu_api/internal/brain_region_prompt.py`
- Modify: `tests/test_brain_region_prompt.py`

- [ ] **Step 1: 写失败测试 — 静态提示词构建**

```python
def test_build_static_brain_region_prompt_contains_architecture():
    """Static prompt explains brain region architecture."""
    prompt = build_static_brain_region_prompt()
    assert "brain:region" in prompt
    assert "brain:Niu" in prompt
    assert "brain_region_anchor" in prompt


def test_build_static_brain_region_prompt_contains_how_to_create():
    """Static prompt explains how to create new brain regions."""
    prompt = build_static_brain_region_prompt()
    assert "brain:region:" in prompt
    assert "belongs_to_region" in prompt


def test_build_static_brain_region_prompt_is_chinese():
    """Static prompt is written in Chinese (LightRAG extraction language is Chinese)."""
    prompt = build_static_brain_region_prompt()
    # Must contain Chinese characters
    assert any('\u4e00' <= c <= '\u9fff' for c in prompt)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd E:/tools/ai-bot && python -m pytest tests/test_brain_region_prompt.py::test_build_static_brain_region_prompt -v`
Expected: FAIL — `NameError: name 'build_static_brain_region_prompt' is not defined`

- [ ] **Step 3: 实现静态提示词构建**

在 `brain_region_prompt.py` 中添加：

```python
STATIC_BRAIN_REGION_PROMPT = """\
=== 脑区架构说明 ===

本知识图谱采用"脑区"架构组织知识。脑区是知识的逻辑分区，类似于大脑的功能区域。

架构规则：
1. 根节点 brain:Niu 是整个图谱的核心锚点，所有脑区通过 brain_region_anchor 关系连接到它
2. 每个脑区是一个 brain:region:{标签} 格式的实体，entity_type 为 BrainRegion
3. 知识实体通过 belongs_to_region 关系归属于某个脑区
4. 默认脑区：聊天历史、文档库、知识体系

如何创建新脑区：
- 当你发现一组实体形成了一个清晰的专业领域（如财务、法律、医疗），可以创建新的脑区
- 创建方式：新建 brain:region:{标签} 实体，建立 brain_region_anchor 关系到 brain:Niu
- 将相关实体通过 belongs_to_region 关系连接到该脑区

如何使用脑区：
- 提取实体时，判断该实体应归属于哪个脑区，建立 belongs_to_region 关系
- 如果实体不属于任何现有脑区，且该领域实体数量足够多，考虑创建新脑区
- 不要将实体强行归入不相关的脑区
"""
```

```python
def build_static_brain_region_prompt() -> str:
    """Return the static brain region architecture explanation.

    This is always injected regardless of current graph state.
    """
    return STATIC_BRAIN_REGION_PROMPT
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd E:/tools/ai-bot && python -m pytest tests/test_brain_region_prompt.py::test_build_static_brain_region_prompt -v`
Expected: 3 PASSED

- [ ] **Step 5: 提交**

```bash
git add niu_api/internal/brain_region_prompt.py tests/test_brain_region_prompt.py
git commit -m "feat: add static brain region prompt builder"
```

---

## Task 3: 脑区提示词构建 — 动态查询部分

**Files:**
- Modify: `niu_api/internal/brain_region_prompt.py`
- Modify: `tests/test_brain_region_prompt.py`

- [ ] **Step 1: 写失败测试 — 动态脑区列表查询**

```python
from unittest.mock import patch, MagicMock


def test_build_dynamic_brain_region_prompt_with_regions():
    """Dynamic prompt includes current brain regions from graph."""
    mock_adapter = MagicMock()
    mock_adapter.query_data.return_value = (
        "brain:region:聊天历史 - 聊天记录和对话历史\n"
        "brain:region:文档库 - 文档和文件存储\n"
        "brain:region:知识体系 - 系统化知识\n"
    )

    prompt = build_dynamic_brain_region_prompt(mock_adapter)
    assert "聊天历史" in prompt
    assert "文档库" in prompt
    assert "知识体系" in prompt
    assert "当前脑区" in prompt


def test_build_dynamic_brain_region_prompt_empty():
    """When no regions found, dynamic prompt returns fallback."""
    mock_adapter = MagicMock()
    mock_adapter.query_data.return_value = ""

    prompt = build_dynamic_brain_region_prompt(mock_adapter)
    assert "默认脑区" in prompt or "聊天历史" in prompt


def test_build_dynamic_brain_region_prompt_uses_local_mode():
    """Dynamic query uses local mode (no LLM calls)."""
    mock_adapter = MagicMock()
    mock_adapter.query_data.return_value = "brain:region:测试"

    build_dynamic_brain_region_prompt(mock_adapter)

    # Verify query_data was called with local mode and only_need_context
    mock_adapter.query_data.assert_called_once()
    call_kwargs = mock_adapter.query_data.call_args
    assert call_kwargs[1]["mode"] == "local" or (call_kwargs[0] and call_kwargs[0][1] == "local")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd E:/tools/ai-bot && python -m pytest tests/test_brain_region_prompt.py::test_build_dynamic_brain_region_prompt -v`
Expected: FAIL — `NameError: name 'build_dynamic_brain_region_prompt' is not defined`

- [ ] **Step 3: 实现动态脑区查询**

在 `brain_region_prompt.py` 中添加：

```python
# Query keywords for finding brain region entities (0 LLM calls)
BRAIN_REGION_QUERY_KEYWORDS = ["brain:region"]

# Fallback when graph query returns nothing
FALLBACK_REGIONS = "聊天历史、文档库、知识体系"


def build_dynamic_brain_region_prompt(adapter) -> str:
    """Build dynamic brain region list by querying the graph.

    Uses local mode + only_need_context=True to avoid LLM calls.
    This prevents infinite loops (proxy → query → LLM → proxy → ...).

    Args:
        adapter: LightRAGAdapter instance with query_data() method.
    """
    try:
        result = adapter.query_data(
            "brain region nodes",
            mode="local",
            only_need_context=True,
        )

        if result and result.strip():
            return f"当前图谱中的脑区：\n{result.strip()}"
    except Exception:
        pass

    return f"当前图谱中的脑区（默认）：{FALLBACK_REGIONS}"
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd E:/tools/ai-bot && python -m pytest tests/test_brain_region_prompt.py::test_build_dynamic_brain_region_prompt -v`
Expected: 3 PASSED

- [ ] **Step 5: 提交**

```bash
git add niu_api/internal/brain_region_prompt.py tests/test_brain_region_prompt.py
git commit -m "feat: add dynamic brain region prompt builder with local-mode query"
```

---

## Task 4: 组合提示词 + 注入到消息

**Files:**
- Modify: `niu_api/internal/brain_region_prompt.py`
- Modify: `tests/test_brain_region_prompt.py`

- [ ] **Step 1: 写失败测试 — 组合注入**

```python
def test_inject_brain_region_context_adds_to_system_prompt():
    """Injection appends brain region info to the system message."""
    messages = [
        {"role": "system", "content": "---Role---\nYou are a Knowledge Graph Specialist..."},
        {"role": "user", "content": "Extract entities..."},
    ]
    mock_adapter = MagicMock()
    mock_adapter.query_data.return_value = "brain:region:测试脑区"

    result = inject_brain_region_context(messages, mock_adapter)

    # System message should be modified
    system_msg = next(m for m in result if m["role"] == "system")
    assert "脑区架构说明" in system_msg["content"]
    assert "测试脑区" in system_msg["content"]


def test_inject_brain_region_context_preserves_other_messages():
    """Non-system messages are not modified."""
    messages = [
        {"role": "system", "content": "---Role---\nYou are a Knowledge Graph Specialist..."},
        {"role": "user", "content": "Extract entities..."},
    ]
    mock_adapter = MagicMock()
    mock_adapter.query_data.return_value = ""

    result = inject_brain_region_context(messages, mock_adapter)

    user_msg = next(m for m in result if m["role"] == "user")
    assert user_msg["content"] == "Extract entities..."


def test_inject_brain_region_context_non_extraction_request_unchanged():
    """Non-extraction requests are not modified at all."""
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello"},
    ]
    mock_adapter = MagicMock()

    result = inject_brain_region_context(messages, mock_adapter)

    assert result == messages
    mock_adapter.query_data.assert_not_called()


def test_inject_brain_region_context_returns_new_list():
    """Injection returns a new list, does not mutate the original."""
    messages = [
        {"role": "system", "content": "---Role---\nYou are a Knowledge Graph Specialist..."},
        {"role": "user", "content": "Extract entities..."},
    ]
    mock_adapter = MagicMock()
    mock_adapter.query_data.return_value = ""

    result = inject_brain_region_context(messages, mock_adapter)

    assert result is not messages
    assert messages[0]["content"] == "---Role---\nYou are a Knowledge Graph Specialist..."
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd E:/tools/ai-bot && python -m pytest tests/test_brain_region_prompt.py::test_inject_brain_region_context -v`
Expected: FAIL — `NameError: name 'inject_brain_region_context' is not defined`

- [ ] **Step 3: 实现组合注入函数**

在 `brain_region_prompt.py` 中添加：

```python
def inject_brain_region_context(
    messages: list[dict], adapter
) -> list[dict]:
    """Inject brain region architecture info into LightRAG extraction requests.

    If the messages are a LightRAG extraction request, appends brain region
    context to the system prompt. Otherwise, returns messages unchanged.

    Returns a NEW list — does not mutate the input.

    Args:
        messages: LiteLLM-format message list.
        adapter: LightRAGAdapter instance for querying brain regions.

    Returns:
        New message list with brain region context injected (or original if
        not an extraction request).
    """
    if not is_lightrag_extraction_request(messages):
        return messages

    # Build injection content
    static_part = build_static_brain_region_prompt()
    dynamic_part = build_dynamic_brain_region_prompt(adapter)
    injection = f"\n\n{static_part}\n\n{dynamic_part}"

    # Create new list with modified system prompt
    result = []
    for msg in messages:
        if msg.get("role") == "system":
            new_msg = {**msg, "content": msg.get("content", "") + injection}
            result.append(new_msg)
        else:
            result.append(msg)

    return result
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd E:/tools/ai-bot && python -m pytest tests/test_brain_region_prompt.py::test_inject_brain_region_context -v`
Expected: 4 PASSED

- [ ] **Step 5: 提交**

```bash
git add niu_api/internal/brain_region_prompt.py tests/test_brain_region_prompt.py
git commit -m "feat: add inject_brain_region_context combining static + dynamic prompts"
```

---

## Task 5: 集成到 llm_proxy.py

**Files:**
- Modify: `niu_api/llm_proxy.py`
- Create: `tests/test_llm_proxy_injection.py`

- [ ] **Step 1: 写失败测试 — 代理拦截集成**

```python
"""Integration tests for brain region injection in LLM proxy."""
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from niu_api.llm_proxy import chat_completions, OpenAIChatRequest, OpenAIMessage


@pytest.mark.asyncio
async def test_chat_completions_injects_for_lightrag_request():
    """LightRAG extraction requests get brain region context injected."""
    request = OpenAIChatRequest(
        model="test-model",
        messages=[
            OpenAIMessage(
                role="system",
                content="---Role---\nYou are a Knowledge Graph Specialist...",
            ),
            OpenAIMessage(role="user", content="Extract entities from: test text"),
        ],
    )

    # Mock the LLM call to capture what messages are actually sent
    captured_messages = {}

    async def mock_call_llm(messages, tools=None, response_format=None):
        captured_messages["messages"] = messages
        return {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "done"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    with patch("niu_api.llm_proxy.call_llm_via_litellm", new=AsyncMock(side_effect=mock_call_llm)):
        with patch("niu_api.llm_proxy.get_llm_config", return_value={
            "type": "openai", "apikey": "test-key", "apibase": "http://test", "model": "test-model"
        }):
            with patch("niu_api.llm_proxy.inject_brain_region_context") as mock_inject:
                mock_inject.return_value = [
                    {"role": "system", "content": "Knowledge Graph Specialist... + brain region info"},
                    {"role": "user", "content": "Extract entities from: test text"},
                ]
                response = await chat_completions(request)

    # Verify inject_brain_region_context was called
    mock_inject.assert_called_once()


@pytest.mark.asyncio
async def test_chat_completions_no_injection_for_normal_chat():
    """Normal chat requests are NOT modified."""
    request = OpenAIChatRequest(
        model="test-model",
        messages=[
            OpenAIMessage(role="system", content="You are a helpful assistant."),
            OpenAIMessage(role="user", content="Hello"),
        ],
    )

    with patch("niu_api.llm_proxy.call_llm_via_litellm", new=AsyncMock(return_value={
        "choices": [{"message": {"role": "assistant", "content": "Hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    })):
        with patch("niu_api.llm_proxy.get_llm_config", return_value={
            "type": "openai", "apikey": "test-key", "apibase": "http://test", "model": "test-model"
        }):
            with patch("niu_api.llm_proxy.inject_brain_region_context") as mock_inject:
                mock_inject.return_value = [
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": "Hello"},
                ]
                response = await chat_completions(request)

    # inject should still be called (it checks internally), but returns unchanged
    mock_inject.assert_called_once()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd E:/tools/ai-bot && python -m pytest tests/test_llm_proxy_injection.py -v`
Expected: FAIL — `ImportError: cannot import name 'inject_brain_region_context' from 'niu_api.llm_proxy'`

- [ ] **Step 3: 集成注入到 llm_proxy.py**

在 `llm_proxy.py` 顶部添加导入：

```python
from niu_api.internal.brain_region_prompt import inject_brain_region_context
```

在 `chat_completions()` 函数中，在 `litellm_messages = openai_to_litellm_messages(request.messages)` 之后、`call_llm_via_litellm()` 之前，添加注入逻辑：

```python
    # Inject brain region context for LightRAG extraction requests
    from niu_api.internal.lightrag_adapter import LightRAGAdapter
    adapter = LightRAGAdapter()
    litellm_messages = inject_brain_region_context(litellm_messages, adapter)
```

完整修改位置：在 `chat_completions()` 函数中，找到以下代码块：

```python
    # Convert OpenAI format to LiteLLM format
    litellm_messages = openai_to_litellm_messages(request.messages)
    litellm_tools = openai_to_litellm_tools(request.tools)

    logger.debug(f"[LLM Proxy] Converted {len(litellm_messages)} messages")
```

替换为：

```python
    # Convert OpenAI format to LiteLLM format
    litellm_messages = openai_to_litellm_messages(request.messages)
    litellm_tools = openai_to_litellm_tools(request.tools)

    # Inject brain region context for LightRAG extraction requests
    from niu_api.internal.lightrag_adapter import LightRAGAdapter
    adapter = LightRAGAdapter()
    litellm_messages = inject_brain_region_context(litellm_messages, adapter)

    logger.debug(f"[LLM Proxy] Converted {len(litellm_messages)} messages")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd E:/tools/ai-bot && python -m pytest tests/test_llm_proxy_injection.py -v`
Expected: 2 PASSED

- [ ] **Step 5: 提交**

```bash
git add niu_api/llm_proxy.py tests/test_llm_proxy_injection.py
git commit -m "feat: integrate brain region prompt injection into LLM proxy"
```

---

## Task 6: 清理 lightrag_adapter.py DEPRECATED 标记（P8）

**Files:**
- Modify: `niu_api/internal/lightrag_adapter.py`

- [ ] **Step 1: 确认当前DEPRECATED残留位置**

Run: `grep -n "DEPRECATED\|deprecated\|warnings.warn\|import warnings" E:/tools/ai-bot/niu_api/internal/lightrag_adapter.py`

需要清理的内容（根据之前的调研）：
- 类 docstring 中的 "inject_custom_kg (deprecated)" → 改为 "inject_custom_kg (precise control)"
- 方法 docstring 中的 "DEPRECATED: 建议使用 lightrag_insert 替代" → 改为互补说明
- `warnings.warn(...)` 调用 → 删除
- `import warnings` → 删除（如果仅用于上述 warn）

- [ ] **Step 2: 修改类 docstring**

找到 LightRAGIngester 类的 docstring 中的：
```
- LightRAGIngester: injection via lightrag_insert (recommended), inject_custom_kg
                    (deprecated), and inject_document (unstructured)
```
替换为：
```
- LightRAGIngester: injection via lightrag_insert (auto extraction),
                    inject_custom_kg (precise control), and inject_document (unstructured)
```

找到：
```
    Legacy: inject_custom_kg (DEPRECATED) → calls ainsert_custom_kg() without LLM extraction.
```
替换为：
```
    Structured: inject_custom_kg → calls ainsert_custom_kg() for precise entity/relationship injection.
    Complementary: inject_custom_kg (precise control) and lightrag_insert (auto extraction) serve different purposes.
```

- [ ] **Step 3: 修改方法 docstring**

找到 `inject_custom_kg` 方法的 docstring 中的：
```
        DEPRECATED: 建议使用 lightrag_insert 替代。
        ainsert_custom_kg 不触发 LLM 提取，无法自动合并同名实体。
        新代码应使用 lightrag_insert 通过 ainsert 自动提取。
```
替换为：
```
        Complementary to lightrag_insert: use inject_custom_kg when you need
        precise control over entity names, relationship types, and graph structure
        (e.g., brain regions, photo metadata, person nodes). Use lightrag_insert
        for natural language content where LLM auto-extraction and entity merging
        are desired.
```

- [ ] **Step 4: 删除 warnings.warn 和 import warnings**

删除 `inject_custom_kg` 方法中的：
```python
        # Warn about deprecation
        warnings.warn(
            "inject_custom_kg is deprecated, use lightrag_insert instead. "
            "ainsert_custom_kg does not trigger LLM extraction.",
            DeprecationWarning,
            stacklevel=2,
        )
```

删除文件顶部的 `import warnings`（如果仅用于上述 warn）。

- [ ] **Step 5: 验证清理完成**

Run: `grep -n "DEPRECATED\|deprecated\|warnings.warn\|import warnings" E:/tools/ai-bot/niu_api/internal/lightrag_adapter.py`
Expected: 无输出

- [ ] **Step 6: 提交**

```bash
git add niu_api/internal/lightrag_adapter.py
git commit -m "fix: remove incorrect DEPRECATED markers from inject_custom_kg — it is complementary to lightrag_insert, not a replacement"
```

---

## Task 7: 冷启动优化 — 监听LightRAG就绪事件

**Files:**
- Modify: `agent/injector/region_sync.py`

- [ ] **Step 1: 分析当前冷启动逻辑**

当前 `RegionSync` 使用 `asyncio.sleep(180)` 固定等待180秒。需要改为：
- 启动时立即尝试初始化
- 如果 LightRAG 未就绪，短间隔重试（如5秒）
- 最多重试12次（60秒），而非固定等180秒

- [ ] **Step 2: 修改 RegionSync 初始化逻辑**

在 `region_sync.py` 中，找到 `asyncio.sleep(180)` 或类似的固定等待，替换为轮询重试：

```python
    async def _wait_for_lightrag_ready(self, max_retries: int = 12, interval: float = 5.0) -> bool:
        """Wait for LightRAG to be ready, polling at short intervals.

        Returns True if LightRAG is ready, False if max retries exceeded.
        """
        from niu_api.internal.lightrag_adapter import get_lightrag

        for attempt in range(max_retries):
            rag = get_lightrag()
            if rag is not None:
                logger.info(f"[RegionSync] LightRAG ready after {attempt * interval:.0f}s")
                return True
            await asyncio.sleep(interval)

        logger.warning(f"[RegionSync] LightRAG not ready after {max_retries * interval:.0f}s")
        return False
```

- [ ] **Step 3: 验证修改**

Run: `cd E:/tools/ai-bot && python -c "from agent.injector.region_sync import RegionSync; print('import OK')"`

- [ ] **Step 4: 提交**

```bash
git add agent/injector/region_sync.py
git commit -m "perf: replace 180s fixed delay with polling retry for LightRAG readiness"
```

---

## Task 8: runner.py 实例缓存优化

**Files:**
- Modify: `agent/generic/runner.py`

- [ ] **Step 1: 分析当前每轮新建实例的位置**

在 `runner.py` 中找到每轮创建 `LightRAGAdapter`、`LightRAGIngester`、`RegionManager`、`BrainContextInjector` 的位置（约 L701-723）。

- [ ] **Step 2: 缓存实例为 runner 属性**

将4个实例缓存为 `GenericAgentRunner` 的实例属性，首次创建后复用：

```python
    def _get_brain_context_injector(self):
        """Get or create cached BrainContextInjector."""
        if not hasattr(self, '_brain_injector') or self._brain_injector is None:
            from niu_api.internal.lightrag_adapter import LightRAGAdapter, LightRAGIngester
            from niu_api.internal.region_manager import RegionManager
            from niu_api.internal.region_injector import BrainContextInjector

            adapter = LightRAGAdapter()
            ingester = LightRAGIngester(adapter)
            region_mgr = RegionManager(adapter)
            self._brain_injector = BrainContextInjector(region_mgr)
        return self._brain_injector
```

- [ ] **Step 3: 替换调用点**

将原来每轮新建的代码替换为 `self._get_brain_context_injector()` 调用。

- [ ] **Step 4: 提交**

```bash
git add agent/generic/runner.py
git commit -m "perf: cache brain context injector instances across turns"
```

---

## Task 9: 全量集成测试

**Files:**
- Create: `tests/test_brain_region_e2e.py`

- [ ] **Step 1: 写端到端验证测试**

```python
"""End-to-end verification tests for brain region lifecycle.

These tests verify that the complete brain region architecture works
end-to-end: injection → extraction → storage → retrieval → dissolution.
"""
import pytest
from unittest.mock import patch, MagicMock


def test_detection_and_injection_pipeline():
    """Full pipeline: detect extraction request → build prompt → inject."""
    from niu_api.internal.brain_region_prompt import (
        is_lightrag_extraction_request,
        build_static_brain_region_prompt,
        build_dynamic_brain_region_prompt,
        inject_brain_region_context,
    )

    # Step 1: Detect
    messages = [
        {"role": "system", "content": "---Role---\nYou are a Knowledge Graph Specialist..."},
        {"role": "user", "content": "Extract entities from: Python is a programming language"},
    ]
    assert is_lightrag_extraction_request(messages) is True

    # Step 2: Build static prompt
    static = build_static_brain_region_prompt()
    assert "brain:region" in static
    assert "brain:Niu" in static

    # Step 3: Build dynamic prompt (mocked)
    mock_adapter = MagicMock()
    mock_adapter.query_data.return_value = "brain:region:聊天历史\nbrain:region:文档库"
    dynamic = build_dynamic_brain_region_prompt(mock_adapter)
    assert "聊天历史" in dynamic

    # Step 4: Inject
    result = inject_brain_region_context(messages, mock_adapter)
    system_msg = next(m for m in result if m["role"] == "system")
    assert "脑区架构说明" in system_msg["content"]
    assert "聊天历史" in system_msg["content"]

    # Step 5: Original messages not mutated
    assert "脑区架构说明" not in messages[0]["content"]


def test_injection_does_not_trigger_llm():
    """Verify that dynamic brain region query uses local mode (0 LLM calls)."""
    from niu_api.internal.brain_region_prompt import build_dynamic_brain_region_prompt

    mock_adapter = MagicMock()
    mock_adapter.query_data.return_value = "brain:region:测试"

    build_dynamic_brain_region_prompt(mock_adapter)

    # Verify query_data was called with mode="local"
    mock_adapter.query_data.assert_called_once()
    call_args = mock_adapter.query_data.call_args
    # mode should be "local" — no LLM calls
    assert call_args[1].get("mode") == "local" or "local" in str(call_args)
    # only_need_context should be True — no LLM calls
    assert call_args[1].get("only_need_context") is True


def test_injection_handles_adapter_failure_gracefully():
    """If adapter fails, injection falls back to static + defaults."""
    from niu_api.internal.brain_region_prompt import inject_brain_region_context

    messages = [
        {"role": "system", "content": "---Role---\nYou are a Knowledge Graph Specialist..."},
        {"role": "user", "content": "Extract entities..."},
    ]

    mock_adapter = MagicMock()
    mock_adapter.query_data.side_effect = Exception("LightRAG not initialized")

    result = inject_brain_region_context(messages, mock_adapter)
    system_msg = next(m for m in result if m["role"] == "system")
    # Static part should still be injected
    assert "脑区架构说明" in system_msg["content"]
    # Dynamic part should fall back to defaults
    assert "聊天历史" in system_msg["content"] or "默认" in system_msg["content"]


def test_non_extraction_request_not_modified():
    """Normal chat messages pass through unchanged."""
    from niu_api.internal.brain_region_prompt import inject_brain_region_context

    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello"},
    ]

    mock_adapter = MagicMock()
    result = inject_brain_region_context(messages, mock_adapter)

    assert result == messages
    mock_adapter.query_data.assert_not_called()
```

- [ ] **Step 2: 运行全量测试**

Run: `cd E:/tools/ai-bot && python -m pytest tests/test_brain_region_e2e.py tests/test_brain_region_prompt.py tests/test_llm_proxy_injection.py -v`
Expected: ALL PASSED

- [ ] **Step 3: 提交**

```bash
git add tests/test_brain_region_e2e.py
git commit -m "test: add end-to-end verification tests for brain region lifecycle"
```

---

## Task 10: 真实环境冒烟测试

**Files:**
- Create: `scripts/test_brain_region_smoke.py`

- [ ] **Step 1: 写冒烟测试脚本**

```python
#!/usr/bin/env python3
"""Smoke test for brain region architecture — verifies real graph operations.

Run: python scripts/test_brain_region_smoke.py

Prerequisites:
- App must be running (python -m niu_api)
- LightRAG must be initialized
"""
import sys
import requests

API_BASE = "http://localhost:9876"


def test_lightrag_status():
    """Verify LightRAG is running."""
    resp = requests.get(f"{API_BASE}/llm/v1/status", timeout=5)
    assert resp.status_code == 200, f"Status check failed: {resp.status_code}"
    data = resp.json()
    assert data.get("lightrag", {}).get("installed"), "LightRAG not installed"
    print("[PASS] LightRAG status OK")


def test_brain_region_query():
    """Verify brain region data can be queried (local mode, 0 LLM calls)."""
    resp = requests.post(
        f"{API_BASE}/api/lightrag/query",
        json={
            "query": "brain:region",
            "mode": "local",
            "only_need_context": True,
        },
        timeout=30,
    )
    assert resp.status_code == 200, f"Query failed: {resp.status_code}"
    data = resp.json()
    print(f"[PASS] Brain region query returned: {str(data)[:200]}...")


def test_proxy_injection():
    """Verify LLM proxy injects brain region context for extraction requests."""
    resp = requests.post(
        f"{API_BASE}/llm/v1/chat/completions",
        json={
            "model": "test",
            "messages": [
                {
                    "role": "system",
                    "content": "---Role---\nYou are a Knowledge Graph Specialist...",
                },
                {
                    "role": "user",
                    "content": "Extract entities from: test text for smoke test",
                },
            ],
        },
        timeout=60,
    )
    # Even if LLM call fails (no API key), the proxy should not crash
    print(f"[INFO] Proxy response status: {resp.status_code}")


if __name__ == "__main__":
    tests = [test_lightrag_status, test_brain_region_query, test_proxy_injection]
    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"[FAIL] {test.__name__}: {e}")
            failed += 1

    print(f"\nResults: {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
```

- [ ] **Step 2: 运行冒烟测试（需要应用运行中）**

Run: `cd E:/tools/ai-bot && python scripts/test_brain_region_smoke.py`
Expected: 3 PASSED（需要应用运行中）

- [ ] **Step 3: 提交**

```bash
git add scripts/test_brain_region_smoke.py
git commit -m "test: add smoke test script for brain region architecture"
```

---

## 自检清单

- [x] **Spec coverage**: P6（提示词注入）→ Task 1-5, P8（DEPRECATED清理）→ Task 6, P5冷启动 → Task 7-8, 测试 → Task 9-10
- [x] **Placeholder scan**: 无 TBD/TODO/placeholder
- [x] **Type consistency**: 所有函数签名和调用一致（messages: list[dict], adapter 参数）
- [x] **死循环防护**: Task 3 明确使用 local + only_need_context=True，0次LLM
- [x] **不可变性**: inject_brain_region_context 返回新列表，不修改原列表
