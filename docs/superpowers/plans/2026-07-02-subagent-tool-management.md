# 子 Agent 工具管理与职责边界改造 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 防止子 Agent 接到越界任务后用 bash/grep 在文件系统乱翻死循环，建立"默认禁用危险基础工具 + 职责边界自动注入 + 主 Agent 委托约束"三层防线。

**Architecture:** 三层防线：①subagent.py 加默认黑名单 `{"bash","grep"}` + `allowBaseTools` 白名单解禁机制；②`build_subagent_system_segments` 自动注入通用"职责边界"段（子 Agent 正文没含"直接退出"语义就自动加）；③`niu.md` 加委托约束（主 Agent 委托前查看工具列表中该子 Agent 的描述，不在能力范围内的禁止委托）+ 3 个手动调用子 Agent 的 description 加"子 Agent —"前缀标识类型。改造影响面集中：subagent.py 改 3 处，6 个子 Agent .md 只改 frontmatter 不改正文（其中 3 个手动调用的子 Agent 顺带改 description 前缀），niu.md 改 1 处。

**Tech Stack:** Python 3.11 + YAML frontmatter + loguru + pytest

---

## 文件结构

### 修改文件清单

| 文件 | 改动内容 | Task |
|------|---------|------|
| `agent/subagent.py` | 加默认黑名单常量 + 重写 disableBaseTools 过滤逻辑（抽取为 `_filter_base_tools` 函数）+ 职责边界自动注入 | Task 1, 3 |
| `config/agents/event-manager.md` | frontmatter 加 `disableBaseTools` 全禁 + description 加"子 Agent —"前缀 | Task 4 |
| `config/agents/file-processor.md` | frontmatter 加 `disableBaseTools` 全禁 + description 加"子 Agent —"前缀 | Task 4 |
| `config/agents/journal-agent.md` | frontmatter 加 `allowBaseTools: [read, write, edit, grep]` + description 加"子 Agent —"前缀 | Task 4 |
| `config/agents/context-manager.md` | frontmatter 补 `grep` 到 disableBaseTools | Task 4 |
| `config/agents/entity-extractor.md` | frontmatter 补 `grep` 和 `read` 到 disableBaseTools | Task 4 |
| `config/agents/dream-evolver.md` | frontmatter 加 `allowBaseTools: [read, write, edit, bash]` | Task 4 |
| `config/agents/niu.md` | L106-123 子 Agent 委托段加"不在子 Agent 能力范围内禁止委托"规则（措辞改为"查看工具列表中该子 Agent 的描述"） | Task 5 |
| `tests/test_subagent_tool_filter.py` | 新建测试文件 | Task 2, 3 |

### 不改动文件

- `agent/handler.py` — `dispatch` 路由不检查 tools_schema（已知残留风险，见"风险与回滚"段第 1 条；Task 4 的 `allowBaseTools` 解禁 dream-evolver 的 bash 是为满足 dream-evolver 正文真实需求，不是为缓解此风险，本期不修 dispatch）
- `agent/runner.py` — `get_tools_schema` 不变（仍返回 6 个基础工具全集）
- `agent/generic/assets/tools_schema.json` — 基础工具 schema 不变
- 6 个子 Agent .md 的正文（职责描述不变）

---

## Task 1: subagent.py 加默认黑名单 + allowBaseTools 解禁机制

**Files:**
- Modify: `agent/subagent.py:14-16`（模块常量区）
- Modify: `agent/subagent.py:484-500`（disableBaseTools 应用逻辑）
- Test: `tests/test_subagent_tool_filter.py`（Task 2 创建）

- [ ] **Step 1: 在模块常量区加默认黑名单**

修改 `agent/subagent.py:14-16`，在 `MAX_CONTEXT_WINDOW_SIZE` 之后追加：

```python
DEFAULT_CONTEXT_WINDOW_SIZE = 200000
MIN_CONTEXT_WINDOW_SIZE = 32000    # 32K 最小合理值
MAX_CONTEXT_WINDOW_SIZE = 2000000  # 2M 上限

# 默认禁用的基础工具（子 Agent 默认不能调用，需要显式 allowBaseTools 解禁）
# bash 和 grep 是"文件系统乱翻"的元凶，默认禁用
DEFAULT_DISABLED_BASE_TOOLS = {"bash", "grep"}
```

- [ ] **Step 2: 抽取过滤逻辑为独立函数 `_filter_base_tools`**

把过滤逻辑从 `call_subagent` 内联代码抽取为模块级独立函数，便于 Task 2 测试直接调用真实函数而非复制逻辑。

**Step 2a: 在模块顶部定义 `_filter_base_tools` 函数**

在 `agent/subagent.py:18`（`DEFAULT_DISABLED_BASE_TOOLS` 常量之后，`count_tokens_for_text` 等函数之前）追加：

```python
def _filter_base_tools(agent_config: dict, tools_schema: list) -> tuple:
    """根据 agent_config 的 disableBaseTools/allowBaseTools 过滤基础工具。

    三层过滤逻辑：
    1. 默认黑名单（DEFAULT_DISABLED_BASE_TOOLS，bash/grep 默认禁用）
    2. disableBaseTools 追加禁用
    3. allowBaseTools 从黑名单中解禁（优先级最高）

    Args:
        agent_config: 子 Agent 配置字典（frontmatter 解析结果）
        tools_schema: 待过滤的工具 schema 列表

    Returns:
        (filtered_tools, disabled_set, custom_disabled, allowed_base) 元组：
        - filtered_tools: 过滤后的工具 schema 列表
        - disabled_set: 最终禁用的工具名集合
        - custom_disabled: 子 Agent 自定义 disableBaseTools 列表
        - allowed_base: 子 Agent 自定义 allowBaseTools 列表
    """
    disabled_set = set(DEFAULT_DISABLED_BASE_TOOLS)
    custom_disabled = agent_config.get("disableBaseTools", [])
    if custom_disabled:
        disabled_set |= set(custom_disabled)
    allowed_base = agent_config.get("allowBaseTools", [])
    if allowed_base:
        disabled_set -= set(allowed_base)

    if disabled_set:
        filtered = [
            t for t in tools_schema
            if t.get("function", {}).get("name", "") not in disabled_set
        ]
    else:
        filtered = list(tools_schema)

    return filtered, disabled_set, custom_disabled, allowed_base
```

**Step 2b: `call_subagent` 改为调用 `_filter_base_tools`**

修改 `agent/subagent.py:493-500`，把当前的 `if disabled_base:` 块替换为函数调用：

**当前代码**（L493-500）：
```python
    # 根据 disableBaseTools 配置移除基础工具
    disabled_base = agent_config.get("disableBaseTools", [])
    if disabled_base:
        tools_schema = [
            t for t in tools_schema
            if t.get("function", {}).get("name", "") not in disabled_base
        ]
        logger.info(f"[SubAgent] {agent_name}: Disabled base tools: {disabled_base}")
```

**改为**：
```python
    # 根据配置移除基础工具（三层过滤：默认黑名单 + disableBaseTools + allowBaseTools 解禁）
    # 过滤逻辑抽取到 _filter_base_tools 函数，Task 2 测试直接调用真实函数避免逻辑失同步
    tools_schema, disabled_set, custom_disabled, allowed_base = _filter_base_tools(agent_config, tools_schema)
    if disabled_set:
        logger.info(f"[SubAgent] {agent_name}: Disabled base tools: {sorted(disabled_set)}")
```

- [ ] **Step 3: 运行现有测试确认不破坏**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_subagent_overflow.py tests/test_subagent_migration.py -v 2>&1 | tail -20`
Expected: 现有测试 PASS（现有测试都 mock 掉了 get_tools_schema，不受过滤逻辑改动影响）

- [ ] **Step 4: 添加 logger.warning 提示未配置 disableBaseTools 的子 Agent**

在 Step 2 改造的代码块之后（L500 之后），追加一条 warning 检查（推动显式配置）：

```python
    # 配置完整性检查：未显式配置 disableBaseTools 且未配 allowBaseTools 的子 Agent
    if not custom_disabled and not allowed_base:
        logger.warning(
            f"[SubAgent] {agent_name}: No disableBaseTools/allowBaseTools configured, "
            f"using default blacklist only: {sorted(DEFAULT_DISABLED_BASE_TOOLS)}. "
            f"Recommend explicit config in config/agents/{agent_name}.md frontmatter."
        )
```

- [ ] **Step 5: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add agent/subagent.py
git commit -m "feat(subagent): 加默认黑名单 bash/grep + allowBaseTools 解禁机制

子 Agent 默认禁用 bash 和 grep（文件系统乱翻元凶），需要 bash/grep
的子 Agent 通过 frontmatter allowBaseTools 显式解禁。三层过滤逻辑：
默认黑名单 + disableBaseTools 追加 + allowBaseTools 解禁（优先级最高）。
过滤逻辑抽取为 _filter_base_tools 函数，供 Task 2 测试直接调用真实函数
避免复制逻辑导致测试与代码失同步。"
```

---

## Task 2: 新建测试覆盖工具过滤逻辑

**Files:**
- Create: `tests/test_subagent_tool_filter.py`

- [ ] **Step 1: 创建测试文件**

创建 `tests/test_subagent_tool_filter.py`，内容：

```python
"""测试 subagent.py 的基础工具过滤逻辑（disableBaseTools + allowBaseTools + 默认黑名单）。"""
import pytest
from unittest.mock import patch
from agent import subagent


def _make_base_tools():
    """构造 6 个基础工具的 schema（模拟 get_tools_schema 返回）。"""
    names = ["bash", "code_run", "read", "write", "edit", "grep"]
    return [{"type": "function", "function": {"name": n}} for n in names]


def _run_filter(agent_config):
    """跑一遍 subagent._filter_base_tools 真实函数。

    调用 Task 1 Step 2a 抽取的真实函数，避免复制逻辑导致测试与代码失同步。
    """
    from agent.subagent import _filter_base_tools

    tools_schema = _make_base_tools()
    filtered, disabled_set, _, _ = _filter_base_tools(agent_config, tools_schema)
    return [t["function"]["name"] for t in filtered]


def test_default_blacklist_disables_bash_and_grep():
    """未配置任何字段时，默认禁用 bash 和 grep。"""
    result = _run_filter({})
    assert "bash" not in result, f"bash should be disabled by default, got {result}"
    assert "grep" not in result, f"grep should be disabled by default, got {result}"
    assert "code_run" in result
    assert "read" in result
    assert "write" in result
    assert "edit" in result


def test_disableBaseTools_adds_to_blacklist():
    """disableBaseTools 追加禁用到默认黑名单。"""
    result = _run_filter({"disableBaseTools": ["read", "write"]})
    assert "bash" not in result  # 默认黑名单
    assert "grep" not in result  # 默认黑名单
    assert "read" not in result  # 追加禁用
    assert "write" not in result  # 追加禁用
    assert "code_run" in result
    assert "edit" in result


def test_allowBaseTools_unblocks_default_blacklist():
    """allowBaseTools 从默认黑名单中解禁 bash。"""
    result = _run_filter({"allowBaseTools": ["bash"]})
    assert "bash" in result, f"bash should be allowed, got {result}"
    assert "grep" not in result  # 默认黑名单仍禁用 grep
    assert "code_run" in result
    assert "read" in result


def test_allowBaseTools_priority_over_disableBaseTools():
    """allowBaseTools 优先级高于 disableBaseTools（同时配置时 allow 胜出）。"""
    result = _run_filter({
        "disableBaseTools": ["bash", "read"],
        "allowBaseTools": ["bash"],
    })
    assert "bash" in result, f"bash should be allowed (allow wins), got {result}"
    assert "read" not in result  # 被 disableBaseTools 禁用，不在 allow 里
    assert "grep" not in result  # 默认黑名单


def test_dream_evolver_config_unblocks_bash():
    """dream-evolver 的预期配置：allowBaseTools 解禁 bash（skill 删除需要 mv 命令）。"""
    result = _run_filter({"allowBaseTools": ["read", "write", "edit", "bash"]})
    assert "bash" in result
    assert "read" in result
    assert "write" in result
    assert "edit" in result
    assert "grep" not in result  # 默认黑名单仍禁用
    assert "code_run" not in result  # 默认黑名单（不在 allow 里）


def test_journal_agent_config():
    """journal-agent 的预期配置：allowBaseTools 解禁 read/write/edit/grep。"""
    result = _run_filter({"allowBaseTools": ["read", "write", "edit", "grep"]})
    assert "read" in result
    assert "write" in result
    assert "edit" in result
    assert "grep" in result
    assert "bash" not in result  # 默认黑名单仍禁用
    assert "code_run" not in result


def test_event_manager_config_all_disabled():
    """event-manager 的预期配置：disableBaseTools 全禁。"""
    result = _run_filter({"disableBaseTools": ["bash", "code_run", "read", "write", "edit", "grep"]})
    assert result == [], f"event-manager should have no base tools, got {result}"


def test_default_blacklist_constant_exists():
    """确认 DEFAULT_DISABLED_BASE_TOOLS 常量已定义。"""
    from agent.subagent import DEFAULT_DISABLED_BASE_TOOLS
    assert "bash" in DEFAULT_DISABLED_BASE_TOOLS
    assert "grep" in DEFAULT_DISABLED_BASE_TOOLS
```

- [ ] **Step 2: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_subagent_tool_filter.py -v 2>&1 | tail -15`
Expected: 7 个测试全部 PASS

- [ ] **Step 3: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add tests/test_subagent_tool_filter.py
git commit -m "test(subagent): 新增基础工具过滤逻辑测试覆盖 7 种场景

覆盖默认黑名单/disableBaseTools 追加/allowBaseTools 解禁/优先级
冲突/各子 Agent 预期配置等场景，填补原有零测试覆盖的空白。"
```

---

## Task 3: subagent.py 自动注入"职责边界"段

**Files:**
- Modify: `agent/subagent.py:299-320`（build_subagent_system_segments）
- Test: `tests/test_subagent_tool_filter.py`（Task 2 文件追加测试）

- [ ] **Step 1: 在模块顶部定义职责边界模板常量**

在 `agent/subagent.py` 的 `_filter_base_tools` 函数之后（Task 1 Step 2a 已新增该函数，位于常量区之后）追加：

```python
# 子 Agent 职责边界段模板（自动注入到正文未含"直接退出"语义的子 Agent）
_BOUNDARY_SECTION_TEMPLATE = """## 职责边界

你的职责范围由上方系统提示词界定的功能描述决定。
不要猜测含义，无法完全确认属于自己的职责范围的，就要直接退出，回复主 Agent。"""
```

注：检测条件用语义关键词"直接退出"而非标题"## 职责边界"——因为 dream-evolver.md 已有 `## 职责边界` 段但内容是职责声明，不含"直接退出"语义，需触发自动注入追加退出语义。按标题检测会误跳过。

- [ ] **Step 2: 在 build_subagent_system_segments 里加职责边界自动注入**

修改 `agent/subagent.py:311-313`，在 `_build_user_info_section()` 注入之后追加职责边界段。

**当前代码**（L307-313）：
```python
    # 1. 获取子 Agent 提示词（从配置文件）
    static_system = get_subagent_prompt(agent_name)

    # 2. 注入用户信息和偏好（静态段，子 Agent 需要了解用户背景）
    user_info_section = _build_user_info_section()
    if user_info_section:
        static_system += "\n\n" + user_info_section
```

**改为**：
```python
    # 1. 获取子 Agent 提示词（从配置文件）
    static_system = get_subagent_prompt(agent_name)

    # 2. 注入用户信息和偏好（静态段，子 Agent 需要了解用户背景）
    user_info_section = _build_user_info_section()
    if user_info_section:
        static_system += "\n\n" + user_info_section

    # 3. 注入职责边界段（如果子 Agent 正文未含"直接退出"语义，自动追加通用模板）
    #    按语义关键词检测而非标题，避免 dream-evolver 已有"## 职责边界"段（职责声明）
    #    但不含退出语义时被误跳过
    if "直接退出" not in static_system:
        static_system += "\n\n" + _BOUNDARY_SECTION_TEMPLATE
```

- [ ] **Step 3: 在测试文件里追加职责边界注入测试**

在 `tests/test_subagent_tool_filter.py` 末尾追加：

```python
def test_boundary_section_template_exists():
    """确认 _BOUNDARY_SECTION_TEMPLATE 常量已定义。"""
    from agent.subagent import _BOUNDARY_SECTION_TEMPLATE
    assert "## 职责边界" in _BOUNDARY_SECTION_TEMPLATE
    assert "不要猜测含义" in _BOUNDARY_SECTION_TEMPLATE
    assert "直接退出" in _BOUNDARY_SECTION_TEMPLATE


def test_build_subagent_system_segments_injects_boundary_when_missing(monkeypatch):
    """子 Agent 正文没有"直接退出"语义时，自动注入通用模板。"""
    from agent.subagent import build_subagent_system_segments
    # mock get_subagent_prompt 返回不含"直接退出"的正文
    monkeypatch.setattr(subagent, "get_subagent_prompt", lambda name: "你是测试子 Agent。")
    # mock _build_user_info_section 返回空
    monkeypatch.setattr(subagent, "_build_user_info_section", lambda: "")

    static_system, _ = build_subagent_system_segments("test-agent")
    assert "## 职责边界" in static_system
    assert "不要猜测含义" in static_system


def test_build_subagent_system_segments_skips_injection_when_present(monkeypatch):
    """子 Agent 正文已含"直接退出"语义时，不重复注入。"""
    from agent.subagent import build_subagent_system_segments
    # 模拟 dream-evolver 场景：正文已有"## 职责边界"段且含"直接退出"语义
    custom_boundary = "## 职责边界\n\n这是子 Agent 自定义的边界规则，无法确认职责范围就要直接退出，回复主 Agent。"
    monkeypatch.setattr(subagent, "get_subagent_prompt", lambda name: f"你是测试子 Agent。\n\n{custom_boundary}")
    monkeypatch.setattr(subagent, "_build_user_info_section", lambda: "")

    static_system, _ = build_subagent_system_segments("test-agent")
    # 自定义边界保留
    assert custom_boundary in static_system
    # 通用模板的"不要猜测含义"不应出现（因为已跳过自动注入）
    assert "不要猜测含义" not in static_system
    # "## 职责边界"标题只出现 1 次（不重复注入）
    assert static_system.count("## 职责边界") == 1, "should not inject twice"


def test_build_subagent_system_segments_injects_for_dream_evolver_existing_section(monkeypatch):
    """dream-evolver 场景：正文已有"## 职责边界"段但不含"直接退出"语义，应触发注入追加退出语义。"""
    from agent.subagent import build_subagent_system_segments
    # 模拟 dream-evolver.md:32 现状：有"## 职责边界"标题但内容是职责声明，无"直接退出"
    existing_section = "## 职责边界\n\n- 你负责精加工实体\n- 你不负责从零提取新实体"
    monkeypatch.setattr(subagent, "get_subagent_prompt", lambda name: f"你是 dream-evolver。\n\n{existing_section}")
    monkeypatch.setattr(subagent, "_build_user_info_section", lambda: "")

    static_system, _ = build_subagent_system_segments("dream-evolver")
    # 通用模板被追加（因为原文不含"直接退出"）
    assert "不要猜测含义" in static_system
    assert "直接退出" in static_system
    # 原"## 职责边界"段保留
    assert "你负责精加工实体" in static_system
    # 标题出现 2 次：原文 1 次 + 模板 1 次（这是预期行为，dream-evolver 的旧段不含退出语义需追加）
    assert static_system.count("## 职责边界") == 2
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_subagent_tool_filter.py -v 2>&1 | tail -15`
Expected: 11 个测试全部 PASS（7 个原 Task 2 + 4 个新追加）

- [ ] **Step 5: 运行 import 检查**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -c "import agent.subagent; print('IMPORT OK')"`
Expected: 输出 `IMPORT OK`

- [ ] **Step 6: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add agent/subagent.py tests/test_subagent_tool_filter.py
git commit -m "feat(subagent): 自动注入职责边界段到未含退出语义的子 Agent

子 Agent 正文没有'直接退出'语义时，build_subagent_system_segments
自动追加通用模板：'不要猜测含义，无法完全确认属于自己的职责范围的，
就要直接退出，回复主 Agent'。按语义关键词检测而非标题，避免
dream-evolver 已有'## 职责边界'段（职责声明）但不含退出语义时被误跳过。
已含'直接退出'语义的子 Agent 不重复注入。"
```

---

## Task 4: 6 个子 Agent .md frontmatter 配置 disableBaseTools/allowBaseTools

**Files:**
- Modify: `config/agents/event-manager.md:1-9`（frontmatter）
- Modify: `config/agents/file-processor.md:1-15`（frontmatter）
- Modify: `config/agents/journal-agent.md:1-10`（frontmatter）
- Modify: `config/agents/context-manager.md:1-14`（frontmatter）
- Modify: `config/agents/entity-extractor.md:1-15`（frontmatter）
- Modify: `config/agents/dream-evolver.md:1-30`（frontmatter）

**原则**：只改 frontmatter，不改正文（正文已正确描述职责）。

- [ ] **Step 1: event-manager.md 加 disableBaseTools 全禁 + description 加"子 Agent —"前缀**

`config/agents/event-manager.md:1-9` 当前 frontmatter：
```yaml
---
name: event-manager
description: "事件管理：日程/提醒/定时任务，写入scheduler数据库"
mode: subagent
temperature: 0.2
taskDescription: 任务描述，如：创建提醒：明天上午10点开会，或：查看本周日程
mcpServers:
  - scheduler-server
---
```

改为（在 `mcpServers` 之后追加 `disableBaseTools`，并把 description 加"子 Agent — "前缀）：
```yaml
---
name: event-manager
description: "子 Agent — 事件管理：日程/提醒/定时任务，写入scheduler数据库"
mode: subagent
temperature: 0.2
taskDescription: 任务描述，如：创建提醒：明天上午10点开会，或：查看本周日程
mcpServers:
  - scheduler-server
disableBaseTools:
  - bash
  - code_run
  - read
  - write
  - edit
  - grep
---
```

- [ ] **Step 2: file-processor.md 加 disableBaseTools 全禁 + description 加"子 Agent —"前缀**

`config/agents/file-processor.md:1-23` 当前完整 frontmatter：
```yaml
---
name: file-processor
description: "【必须调用】处理文件和照片：入库、人脸识别、文档解析。用户拖入文件/照片时必须调用此工具，不要自己处理文件。"
temperature: 0.2
mode: subagent
taskDescription: 任务描述，如：处理照片：E:/path/photo.jpg，或：入库文档：E:/path/doc.pdf
permissions:
  '*': allow
mcpServers:
  - photo-server
  - lightrag-server
mcpToolFilter:
  lightrag-server:
    - lightrag_insert
    - lightrag_insert_file
    - lightrag_insert_custom_kg
    - lightrag_insert_entity
    - lightrag_insert_relation
    - lightrag_document_status
    - lightrag_get_document
    - lightrag_search_entities
    - lightrag_list_entities
---
```

改为（在 `mcpToolFilter` 段之后追加 `disableBaseTools`，并把 description 前缀从"【必须调用】"改为"子 Agent —"）：
```yaml
---
name: file-processor
description: "子 Agent — 处理文件和照片：入库、人脸识别、文档解析。用户拖入文件/照片时必须调用此工具，不要自己处理文件。"
temperature: 0.2
mode: subagent
taskDescription: 任务描述，如：处理照片：E:/path/photo.jpg，或：入库文档：E:/path/doc.pdf
permissions:
  '*': allow
mcpServers:
  - photo-server
  - lightrag-server
mcpToolFilter:
  lightrag-server:
    - lightrag_insert
    - lightrag_insert_file
    - lightrag_insert_custom_kg
    - lightrag_insert_entity
    - lightrag_insert_relation
    - lightrag_document_status
    - lightrag_get_document
    - lightrag_search_entities
    - lightrag_list_entities
disableBaseTools:
  - bash
  - code_run
  - read
  - write
  - edit
  - grep
---
```

注：`permissions: {'*': allow}` 是 MCP 工具权限配置，不影响基础工具。基础工具由 `disableBaseTools` 管，两者互不影响。description 前缀去掉"【必须调用】"，改为"子 Agent —"——让主 Agent 从工具段就能看出调用此工具会启动一个独立 LLM 会话（参考 disk 工具 description 开头 "Virtual tool disk — ..." 的标识模式）。"必须调用"语义已体现在正文里（"用户拖入文件/照片时必须调用此工具"）。

- [ ] **Step 3: journal-agent.md 加 allowBaseTools 解禁 read/write/edit/grep + description 加"子 Agent —"前缀**

`config/agents/journal-agent.md` 当前 frontmatter `mcpServers: []`。当前 description：
```
description: "工作日志记录与报告生成 - 从对话中提取工作内容写入日志文件，生成周报/月报/季报/年报"
```

做两处改动：

**改动 a：description 加"子 Agent — "前缀，并把分隔符从" - "改为"："与前缀保持一致**：
```yaml
description: "子 Agent — 工作日志记录与报告生成：从对话中提取工作内容写入日志文件，生成周报/月报/季报/年报"
```

**改动 b：在 `mcpServers` 之后追加 `allowBaseTools`**：
```yaml
allowBaseTools:
  - read
  - write
  - edit
  - grep
```

注：journal-agent 正文 L49/L51/L63/L103/L104 显式依赖 read/write/edit/grep，必须解禁。bash 和 code_run 由默认黑名单禁用。description 前缀"子 Agent —"让主 Agent 从工具段识别此工具会启动独立 LLM 会话（与 file-processor/event-manager 保持一致）。

- [ ] **Step 4: context-manager.md 补 grep 到 disableBaseTools**

`config/agents/context-manager.md:8-13` 当前 frontmatter：
```yaml
disableBaseTools:
  - bash
  - write
  - edit
  - code_run
```

改为（追加 grep）：
```yaml
disableBaseTools:
  - bash
  - write
  - edit
  - code_run
  - grep
```

注：context-manager 正文未引用 grep，可禁用。read 保留（模式一可能需要）。

- [ ] **Step 5: entity-extractor.md 补 grep 和 read 到 disableBaseTools**

`config/agents/entity-extractor.md:8-12` 当前 frontmatter：
```yaml
disableBaseTools:
  - bash
  - write
  - edit
  - code_run
```

改为（追加 grep 和 read）：
```yaml
disableBaseTools:
  - bash
  - write
  - edit
  - code_run
  - grep
  - read
```

注：entity-extractor 正文只引用 lightrag_insert 等 MCP 工具，不需要 read/grep。

- [ ] **Step 6: dream-evolver.md 加 allowBaseTools 解禁 read/write/edit/bash**

`config/agents/dream-evolver.md:1-26` 当前完整 frontmatter：
```yaml
---
name: dream-evolver
description: "梦境进化 - 精加工知识图谱 + skill 编写与优化"
mode: subagent
temperature: 0.3
mcpServers:
  - lightrag-server
  - session-manager
mcpToolFilter:
  lightrag-server:
    - lightrag_search_entities
    - lightrag_get_graph
    - lightrag_timeline_query
    - lightrag_list_entities
    - lightrag_get_entity_info
    - lightrag_get_relation_info
    - lightrag_insert_entity
    - lightrag_insert_relation
    - lightrag_edit_entity
    - lightrag_edit_relation
    - lightrag_delete_entity
    - lightrag_delete_relation
    - lightrag_merge_entities
  session-manager:
    - get_messages
---
```

改为（在 `mcpToolFilter` 段之后追加 `allowBaseTools`）：
```yaml
---
name: dream-evolver
description: "梦境进化 - 精加工知识图谱 + skill 编写与优化"
mode: subagent
temperature: 0.3
mcpServers:
  - lightrag-server
  - session-manager
mcpToolFilter:
  lightrag-server:
    - lightrag_search_entities
    - lightrag_get_graph
    - lightrag_timeline_query
    - lightrag_list_entities
    - lightrag_get_entity_info
    - lightrag_get_relation_info
    - lightrag_insert_entity
    - lightrag_insert_relation
    - lightrag_edit_entity
    - lightrag_edit_relation
    - lightrag_delete_entity
    - lightrag_delete_relation
    - lightrag_merge_entities
  session-manager:
    - get_messages
allowBaseTools:
  - read
  - write
  - edit
  - bash
---
```

注：dream-evolver 正文 L236/L242/L273/L300-303 显式依赖 read/write/edit/bash（bash 用于 skill 删除的 mv 命令）。code_run 和 grep 由默认黑名单禁用。dream-evolver.md:32 已有 `## 职责边界` 段但内容是职责声明不含"直接退出"语义，Task 3 的自动注入会追加退出语义段（按"直接退出"关键词检测）。

- [ ] **Step 7: 运行验证脚本确认所有子 Agent 配置正确**

Run:
```bash
cd REDACTED_USER_PATH/tools/ai-bot
python -c "
from agent.subagent import get_subagent_config
for name in ['event-manager', 'file-processor', 'journal-agent', 'context-manager', 'entity-extractor', 'dream-evolver']:
    cfg = get_subagent_config(name)
    print(f'{name}: disableBaseTools={cfg.get(\"disableBaseTools\")}, allowBaseTools={cfg.get(\"allowBaseTools\")}')
"
```

Expected:
- event-manager: disableBaseTools=[bash, code_run, read, write, edit, grep], allowBaseTools=None
- file-processor: disableBaseTools=[bash, code_run, read, write, edit, grep], allowBaseTools=None
- journal-agent: disableBaseTools=None, allowBaseTools=[read, write, edit, grep]
- context-manager: disableBaseTools=[bash, write, edit, code_run, grep], allowBaseTools=None
- entity-extractor: disableBaseTools=[bash, write, edit, code_run, grep, read], allowBaseTools=None
- dream-evolver: disableBaseTools=None, allowBaseTools=[read, write, edit, bash]

- [ ] **Step 8: 跑过滤逻辑模拟确认最终工具列表**

Run:
```bash
cd REDACTED_USER_PATH/tools/ai-bot
python -c "
from agent.subagent import get_subagent_config, DEFAULT_DISABLED_BASE_TOOLS
for name in ['event-manager', 'file-processor', 'journal-agent', 'context-manager', 'entity-extractor', 'dream-evolver']:
    cfg = get_subagent_config(name)
    disabled = set(DEFAULT_DISABLED_BASE_TOOLS)
    custom = cfg.get('disableBaseTools', [])
    if custom:
        disabled |= set(custom)
    allowed = cfg.get('allowBaseTools', [])
    if allowed:
        disabled -= set(allowed)
    all_tools = {'bash', 'code_run', 'read', 'write', 'edit', 'grep'}
    kept = all_tools - disabled
    print(f'{name}: kept={sorted(kept)}, disabled={sorted(disabled)}')
"
```

Expected:
- event-manager: kept=[], disabled=[bash, code_run, read, write, edit, grep]
- file-processor: kept=[], disabled=[bash, code_run, read, write, edit, grep]
- journal-agent: kept=[edit, grep, read, write], disabled=[bash, code_run]
- context-manager: kept=[read], disabled=[bash, code_run, edit, grep, write]
- entity-extractor: kept=[], disabled=[bash, code_run, edit, grep, read, write]
- dream-evolver: kept=[bash, edit, read, write], disabled=[code_run, grep]

- [ ] **Step 9: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add config/agents/event-manager.md config/agents/file-processor.md config/agents/journal-agent.md config/agents/context-manager.md config/agents/entity-extractor.md config/agents/dream-evolver.md
git commit -m "feat(agents): 6 个子 Agent 显式配置 disableBaseTools/allowBaseTools + 3 个手动调用子 Agent 加'子 Agent —'前缀

- event-manager / file-processor: 全禁基础工具（职责全由 MCP 工具覆盖）
- journal-agent: allowBaseTools 解禁 read/write/edit/grep（正文显式依赖）
- context-manager: 补禁 grep（正文未引用）
- entity-extractor: 补禁 grep 和 read（正文未引用，只需 MCP 工具）
- dream-evolver: allowBaseTools 解禁 read/write/edit/bash（skill 维护需要，
  bash 用于 mv 命令删除 skill）
- event-manager / file-processor / journal-agent: description 加'子 Agent —'
  前缀，让主 Agent 从工具段识别调用此工具会启动独立 LLM 会话（参考 disk
  工具'Virtual tool disk — ...'标识模式）

防止子 Agent 接到越界任务后用 bash/grep 在文件系统乱翻死循环。"
```

---

## Task 5: niu.md 加委托约束规则

**Files:**
- Modify: `config/agents/niu.md:106-123`（子 Agent 委托段）

- [ ] **Step 1: 在子 Agent 委托段加约束规则**

读 `config/agents/niu.md:106-110`，在 L108（"**重要**"行）之前或之后追加一条规则。

建议在 L106 段标题 `# 子 Agent 委托` 之后、L108 之前追加：

```markdown
# 子 Agent 委托

**委托前检查**：委托子 Agent 前，查看工具列表中该子 Agent 的描述。不符合工具描述的，不要委托给该子 Agent。

**重要**：文件、照片入库等耗时任务必须使用子 Agent（`chat-with-file-processor`、`chat-with-event-manager`、`chat-with-journal-agent`）。
...
```

- [ ] **Step 2: 运行 import 验证主 Agent 配置无语法错误**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -c "from agent.subagent import get_subagent_config; cfg = get_subagent_config('niu'); print('OK' if cfg else 'FAIL')"`
Expected: 输出 `OK`（验证 niu.md frontmatter 解析正常）

- [ ] **Step 3: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add config/agents/niu.md
git commit -m "feat(niu): 主 Agent 加委托约束 - 不在子 Agent 能力范围内禁止委托

防止主 Agent 把不属于专用子 Agent 能力范围的任务误委托（如 HA 订阅
误委托给 event-manager）。主 Agent 通过查看工具列表中该子 Agent 的
description 判断能力范围，不凭名字猜测。规则只做禁止，不做指引——
主 Agent 自己判断该用 disk 直调、写通用子 Agent、还是其他办法。"
```

---

## Task 6: 端到端验证

**Files:**
- Test: 手动启动程序 + 触发子 Agent 调用

- [ ] **Step 1: 启动程序确认子 Agent 工具列表正确加载**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && ./niu &`
然后: `sleep 5 && ps aux | grep niu | grep -v grep | head -3`
Expected: niu 进程正常启动，无 import 错误

- [ ] **Step 2: 检查日志确认子 Agent 工具过滤生效**

触发一次子 Agent 调用（如问"创建提醒：明天上午 10 点开会"），然后:
Run: `tail -50 logs/api_stderr.log 2>/dev/null | grep -E "SubAgent|Disabled base tools"`
Expected: 看到 `[SubAgent] event-manager: Disabled base tools: [...]` 日志，列表应包含全部 6 个基础工具

- [ ] **Step 3: 检查职责边界段是否注入**

Run: `grep -A 3 "职责边界" logs/llm_interaction_$(date +%Y%m%d).log 2>/dev/null | head -20`
Expected: 看到子 Agent system prompt 里包含"## 职责边界"和"不要猜测含义，无法完全确认属于自己的职责范围的，就要直接退出，回复主 Agent"

- [ ] **Step 4: 端到端测试 - 触发越界任务确认子 Agent 拒绝**

跟主 Agent 说："让 event-manager 帮我查 HA 订阅状态"
Expected:
- 主 Agent 应该不再委托给 event-manager（因为 HA 订阅不在 event-manager 的工具描述范围内）——这条由 Task 5 的委托约束保证（主 Agent 查看工具列表中该子 Agent 的描述）
- 或者主 Agent 仍委托给 event-manager，但 event-manager 应该直接返回"超出我的职责范围"——这条由 Task 3 的职责边界段保证

- [ ] **Step 5: 端到端测试 - 正常子 Agent 任务仍工作**

跟主 Agent 说："创建提醒：明天上午 10 点开会"
Expected: event-manager 正常创建任务，返回 task_id

- [ ] **Step 6: 杀进程清理**

Run: `pkill -f "python.*niu" ; pkill -f "./niu"`
Expected: 所有 niu 进程被杀

- [ ] **Step 7: 最终 Commit（如果有验证中发现的修复）**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add -A
git commit -m "test: 端到端验证子 Agent 工具管理与职责边界改造

验证三层防线生效：
1. 默认黑名单 bash/grep + allowBaseTools 解禁（subagent.py）
2. 职责边界段自动注入（subagent.py）
3. 主 Agent 委托约束（niu.md：查看工具描述判断能力范围）

6 个子 Agent frontmatter 显式配置完成，越界任务被拒绝，正常任务正常执行。"
```

---

## 验证清单

完成所有 Task 后，确认：

- [ ] `python -m pytest tests/test_subagent_tool_filter.py -v` 全部 PASS（11 个测试）
- [ ] `python -c "import agent.subagent; print('OK')"` 输出 OK
- [ ] 6 个子 Agent frontmatter 都有显式 disableBaseTools 或 allowBaseTools 配置
- [ ] 3 个手动调用子 Agent（file-processor/event-manager/journal-agent）的 description 字段已加"子 Agent —"前缀
- [ ] niu.md 子 Agent 委托段有"不在子 Agent 能力范围内禁止委托"规则（措辞为"查看工具列表中该子 Agent 的描述"）
- [ ] 程序启动正常，日志里看到子 Agent 工具过滤生效
- [ ] 越界任务被子 Agent 拒绝（不再乱翻文件系统）
- [ ] 正常子 Agent 任务仍能正常执行

## 风险与回滚

### 已知风险

1. **dispatch 不检查 tools_schema**（handler.py:994-1049）：`do_bash`/`do_grep` 等方法始终存在于 NiuHandler 类上，即使 tools_schema 不含 bash，LLM 若无视 tools_schema 强行生成 bash 工具调用，dispatch 仍会执行。主流 LLM 通常遵守 tools_schema，但若 LLM 无视，dispatch 仍会执行。本期接受此残留风险，不修 dispatch。Task 4 的 allowBaseTools 解禁 dream-evolver 的 bash 是为了满足 dream-evolver 正文 L300-303 的真实需求，不是为缓解此风险。

2. **首次注入职责边界段会产生一次 cache miss**（Claude 模型）：Task 3 的职责边界段注入到子 Agent 的 `static_system`，内容变化导致 cache prefix 失效一次，之后固定文本保持字节稳定即可命中。长期无影响。

3. **通用子 Agent 不在本期范围**：用户未来会加一个"工具全量清单模板"的通用子 Agent，由主 Agent 挑工具写到新 .md 文件。本期改造对通用子 Agent 完全适用（subagent.py 现有逻辑支持任意 .md 配置），无需额外改造。

4. **职责边界段注入位置在末尾**：对长正文子 Agent（dream-evolver 547行、context-manager 355行），边界段在 system prompt 末尾，LLM 注意力可能衰减。但 dream-evolver 已有"## 职责边界"段但不含"直接退出"语义，会触发自动注入追加退出语义段（见 Task 3 测试 test_build_subagent_system_segments_injects_for_dream_evolver_existing_section），原文 + 模板同时存在；context-manager 模式二/三禁工具不会调 bash。其他子 Agent 正文都较短（<100行），影响可忽略。本期接受此设计，不优化注入位置。

### 回滚

如果改造出问题，回滚步骤：
1. `git revert <commit-sha>` 回滚到改造前
2. 临时方案：在 event-manager.md frontmatter 加 `disableBaseTools: [bash, code_run, read, write, edit, grep]`（手动全禁），防止死循环再次发生

## 不改动部分

- `agent/handler.py` 的 dispatch 逻辑（dispatch 不检查 tools_schema 的风险本期不修）
- `agent/runner.py` 的 `get_tools_schema`（仍返回 6 个基础工具全集）
- `agent/generic/assets/tools_schema.json`（基础工具 schema 不变）
- 6 个子 Agent .md 的正文（职责描述不变）
- `config/mcp-servers.yaml`（MCP 服务器配置不变）
