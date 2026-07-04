# 通用子 Agent（阶段三）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现通用子 Agent——主 Agent 通过参考模板自定义新子 Agent 配置（MD 文件），动态加载，由主 Agent 同步或异步调用完成长时复杂任务。

**Architecture:** 模板放 `config/agent-template.md`（参考用，不加载）；主 Agent 用基础工具写新 MD 到 `~/.niu/agents/{name}.md`；程序在 `chat()` 入口扫 `~/.niu/agents/`，发现新 MD 就重算 `base_tools_schema`；MCP 工具由 frontmatter `mcpServers` 字段从已加载的 ToolRegistry 过滤，无需额外加载。

**Tech Stack:** Python 3.11+ / asyncio / SQLite / LightRAG / 现有 agent 框架

**Spec:** `docs/superpowers/specs/2026-07-04-general-subagent-stage3-design.md`

---

## File Structure

| 文件 | 责任 | 操作 |
|------|------|------|
| `config/agent-template.md` | 子 Agent 配置模板 + 编写规则 + 可用 MCP 服务器清单 | Create |
| `agent/subagent.py` | `_resolve_agent_md_path` 多目录查找 + `get_subagent_config` / `get_subagent_prompt` 改造 + `get_subagent_mcp_tools_schema` 失败时返回 `None` 标记坏工具 | Modify |
| `agent/runner.py` | `get_tools_schema` 扫描 `~/.niu/agents/` + 跳过坏 MD + `NiuRunner.__init__` 初始化 `_known_user_subagents` + `chat()` 入口加 `_refresh_base_tools_schema_if_dirty` | Modify |
| `config/agents/niu.md` | 主 Agent 提示词加通用子 Agent 说明段 | Modify |
| `AGENTS.md` | 系统管理手册加通用子 Agent 体系章节 | Modify |
| `tests/test_general_subagent.py` | 单元测试 | Create |

---

## Task 1: 创建模板文件 `config/agent-template.md`

**Files:**
- Create: `config/agent-template.md`

- [ ] **Step 1: 写模板文件**

创建 `config/agent-template.md`，内容：

```markdown
---
# 子 Agent 配置模板
# 复制此文件到 ~/.niu/agents/{name}.md 并填写以下字段
# name 用 kebab-case（如 photo-organizer、doc-summarizer）

description: ""        # 一句话描述子 Agent 的职责（必填，主 Agent 据此判断何时调用）
mcpServers: []         # 该子 Agent 可用的 MCP 服务器列表（见下方"可用 MCP 服务器"）
mcpToolFilter: null    # 可选：白名单过滤特定工具（如只用 photo-server 的 search_photos）
allowAsync: false      # 是否允许异步调用（长时任务设为 true）
permissions: []        # 权限声明（保留字段）
taskDescription: ""    # 任务描述模板（主 Agent 调用时填的入参说明）
disableBaseTools: false # 是否禁用基础工具
temperature: null      # 可选：覆盖 LLM 温度
---

# 提示词正文

在此编写子 Agent 的系统提示词。要说清楚：
- 子 Agent 的角色和职责边界
- 工作流程（先做什么、再做什么）
- 输出格式要求
- 何时主动询问主 Agent（异步模式下用 ask_main_agent）
- 何时该终止自己

## 可用 MCP 服务器

主 Agent 创建子 Agent 时，从以下服务器中选择 `mcpServers` 字段：

- `file-parser` — 文档解析（PDF/Word/PPT/Excel/MD/HTML）
- `lightrag-server` — 知识图谱 + 向量检索
- `photo-server` — 照片管理 + 人脸识别
- `config-manager` — 配置管理
- `memory-server` — 用户长期记忆
- `session-manager` — 会话管理
- `browser-server` — 浏览器自动化
- `brain-region-server` — 脑区状态管理
- `scheduler-server` — 定时任务调度

## frontmatter 字段说明

- `description`（必填）：主 Agent 据此判断何时调用此子 Agent
- `mcpServers`：MCP 服务器名字列表，子 Agent 只能用这些服务器的工具
- `mcpToolFilter`：可选，进一步限制具体工具（如 `["photo-server/search_photos"]`）
- `allowAsync`：true 时支持异步调用（主 Agent 调用后立即返回，子 Agent 后台跑）
- `taskDescription`：主 Agent 调 `chat-with-{name}` 时 task 参数的描述
- `disableBaseTools`：true 时禁用基础工具（如 disk 命令）
- `temperature`：覆盖 LLM 温度（0.0 严谨 / 0.7 创意）
```

- [ ] **Step 2: 验证文件创建**

Run: `cat REDACTED_USER_PATH/tools/ai-bot/config/agent-template.md | head -5`
Expected: 显示 frontmatter 开头 `---`

- [ ] **Step 3: Commit**

```bash
git add config/agent-template.md
git commit -m "feat(template): 通用子 Agent 配置模板

列出所有可用 MCP 服务器 + frontmatter 字段说明 + 提示词编写规则。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2: `agent/subagent.py` 加 `_resolve_agent_md_path` 多目录查找

**Files:**
- Modify: `agent/subagent.py:297-345`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_general_subagent.py`：

```python
"""通用子 Agent（阶段三）单元测试"""
import os
import tempfile
from unittest import mock
from pathlib import Path


def test_resolve_agent_md_path_project_priority(tmp_path, monkeypatch):
    """项目目录的 MD 优先于用户目录"""
    from agent import subagent

    project_dir = tmp_path / "project" / "config" / "agents"
    user_dir = tmp_path / "user" / "agents"
    project_dir.mkdir(parents=True)
    user_dir.mkdir(parents=True)

    # 两个目录都有同名文件
    (project_dir / "foo.md").write_text("---\ndescription: project\n---\nproject body")
    (user_dir / "foo.md").write_text("---\ndescription: user\n---\nuser body")

    monkeypatch.setattr(
        subagent, "__file__",
        str(tmp_path / "project" / "agent" / "subagent.py")
    )
    monkeypatch.setattr("os.path.expanduser", lambda p: str(tmp_path / "user") if p == "~/.niu/agents" else p)

    path = subagent._resolve_agent_md_path("foo")
    assert path == str(project_dir / "foo.md")


def test_resolve_agent_md_path_user_fallback(tmp_path, monkeypatch):
    """项目目录没有时回退到用户目录"""
    from agent import subagent

    project_dir = tmp_path / "project" / "config" / "agents"
    user_dir = tmp_path / "user" / "agents"
    project_dir.mkdir(parents=True)
    user_dir.mkdir(parents=True)

    (user_dir / "bar.md").write_text("---\ndescription: user\n---\nuser body")

    monkeypatch.setattr(
        subagent, "__file__",
        str(tmp_path / "project" / "agent" / "subagent.py")
    )
    monkeypatch.setattr("os.path.expanduser", lambda p: str(tmp_path / "user") if p == "~/.niu/agents" else p)

    path = subagent._resolve_agent_md_path("bar")
    assert path == str(user_dir / "bar.md")


def test_resolve_agent_md_path_not_found(tmp_path, monkeypatch):
    """都找不到返回 None"""
    from agent import subagent

    project_dir = tmp_path / "project" / "config" / "agents"
    user_dir = tmp_path / "user" / "agents"
    project_dir.mkdir(parents=True)
    user_dir.mkdir(parents=True)

    monkeypatch.setattr(
        subagent, "__file__",
        str(tmp_path / "project" / "agent" / "subagent.py")
    )
    monkeypatch.setattr("os.path.expanduser", lambda p: str(tmp_path / "user") if p == "~/.niu/agents" else p)

    path = subagent._resolve_agent_md_path("missing")
    assert path is None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_general_subagent.py -v`
Expected: FAIL with `_resolve_agent_md_path not found` 或 `AttributeError`

- [ ] **Step 3: 实现 `_resolve_agent_md_path`**

在 `agent/subagent.py` 的 `get_subagent_config` 函数之前（约 L295）加：

```python
def _resolve_agent_md_path(agent_name: str) -> Optional[str]:
    """查找子 Agent MD 文件路径。

    先查项目目录 config/agents/{name}.md（专用子 Agent 优先），
    再查用户目录 ~/.niu/agents/{name}.md（通用子 Agent）。

    Args:
        agent_name: 子 Agent 名称（如 file-processor、photo-organizer）

    Returns:
        找到则返回绝对路径，找不到返回 None
    """
    # 项目目录（专用子 Agent）
    project_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "config", "agents", f"{agent_name}.md"
    )
    if os.path.exists(project_path):
        return project_path

    # 用户目录（通用子 Agent，主 Agent 运行时创建）
    user_path = os.path.join(os.path.expanduser("~/.niu/agents"), f"{agent_name}.md")
    if os.path.exists(user_path):
        return user_path

    return None
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_general_subagent.py -v`
Expected: PASS（3 个测试全过）

- [ ] **Step 5: Commit**

```bash
git add agent/subagent.py tests/test_general_subagent.py
git commit -m "feat(subagent): _resolve_agent_md_path 多目录查找

先查 config/agents/（专用子 Agent 优先），再查 ~/.niu/agents/（通用子 Agent）。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 3: 改造 `get_subagent_config` / `get_subagent_prompt` 用 helper

**Files:**
- Modify: `agent/subagent.py:297-345`

- [ ] **Step 1: 写失败测试**

在 `tests/test_general_subagent.py` 追加：

```python
def test_get_subagent_config_from_user_dir(tmp_path, monkeypatch):
    """从用户目录读取通用子 Agent 配置"""
    from agent import subagent

    user_dir = tmp_path / "user" / "agents"
    user_dir.mkdir(parents=True)
    (user_dir / "my-agent.md").write_text(
        "---\ndescription: my agent\nmcpServers: [photo-server]\n---\nbody"
    )

    monkeypatch.setattr(
        subagent, "__file__",
        str(tmp_path / "project" / "agent" / "subagent.py")
    )
    # 让项目目录不存在
    monkeypatch.setattr("os.path.expanduser", lambda p: str(tmp_path / "user") if p == "~/.niu/agents" else p)

    config = subagent.get_subagent_config("my-agent")
    assert config["description"] == "my agent"
    assert config["mcpServers"] == ["photo-server"]


def test_get_subagent_prompt_from_user_dir(tmp_path, monkeypatch):
    """从用户目录读取通用子 Agent 提示词"""
    from agent import subagent

    user_dir = tmp_path / "user" / "agents"
    user_dir.mkdir(parents=True)
    (user_dir / "my-agent.md").write_text(
        "---\ndescription: my agent\n---\nYou are my agent."
    )

    monkeypatch.setattr(
        subagent, "__file__",
        str(tmp_path / "project" / "agent" / "subagent.py")
    )
    monkeypatch.setattr("os.path.expanduser", lambda p: str(tmp_path / "user") if p == "~/.niu/agents" else p)

    prompt = subagent.get_subagent_prompt("my-agent")
    assert prompt == "You are my agent."


def test_get_subagent_config_missing_returns_empty(tmp_path, monkeypatch):
    """MD 文件不存在时返回空 dict（保持现有行为）"""
    from agent import subagent

    monkeypatch.setattr(
        subagent, "__file__",
        str(tmp_path / "project" / "agent" / "subagent.py")
    )
    monkeypatch.setattr("os.path.expanduser", lambda p: str(tmp_path / "user") if p == "~/.niu/agents" else p)

    config = subagent.get_subagent_config("nonexistent")
    assert config == {}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_general_subagent.py -v`
Expected: 3 个新测试 FAIL（因为现有 `get_subagent_config` 硬编码 `config/agents/`）

- [ ] **Step 3: 改造 `get_subagent_config`**

把 `agent/subagent.py:297-325` 的 `get_subagent_config` 整体替换为：

```python
def get_subagent_config(agent_name: str) -> Dict[str, Any]:
    """
    获取子 Agent 配置

    Args:
        agent_name: 子 Agent 名称（如 file-processor、photo-organizer）

    Returns:
        配置字典，包含 mcpServers 等字段。MD 文件不存在时返回空 dict。
    """
    prompt_path = _resolve_agent_md_path(agent_name)

    if prompt_path and os.path.exists(prompt_path):
        with open(prompt_path, "r", encoding="utf-8") as f:
            content = f.read()
            # 解析 YAML front matter
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    try:
                        config = yaml.safe_load(parts[1])
                        if config:
                            return config
                    except Exception:
                        pass

    return {}
```

- [ ] **Step 4: 改造 `get_subagent_prompt`**

把 `agent/subagent.py:328-344`（原行号，改造后可能偏移）的 `get_subagent_prompt` 整体替换为：

```python
def get_subagent_prompt(agent_name: str) -> str:
    """获取子 Agent 提示词"""
    prompt_path = _resolve_agent_md_path(agent_name)

    if prompt_path and os.path.exists(prompt_path):
        with open(prompt_path, "r", encoding="utf-8") as f:
            content = f.read()
            # 提取 body（--- 后面的内容）
            if "---" in content:
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    return parts[2].strip()
            return content

    return f"You are {agent_name} sub-agent. Complete the task efficiently."
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_general_subagent.py -v`
Expected: PASS（所有测试全过）

- [ ] **Step 6: 验证现有功能未回归**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -c "from agent.subagent import get_subagent_config, get_subagent_prompt; print(get_subagent_config('file-processor').get('description')); print(get_subagent_prompt('file-processor')[:50])"`
Expected: 打印 file-processor 的 description 和提示词前 50 字符，不报错

- [ ] **Step 7: Commit**

```bash
git add agent/subagent.py tests/test_general_subagent.py
git commit -m "refactor(subagent): get_subagent_config/prompt 用 _resolve_agent_md_path

支持从 ~/.niu/agents/ 读取通用子 Agent 配置，专用子 Agent（config/agents/）优先。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 4: `get_tools_schema` 扫描 `~/.niu/agents/` + 跳过坏 MD

**Files:**
- Modify: `agent/runner.py:243-330`

- [ ] **Step 1: 写失败测试**

在 `tests/test_general_subagent.py` 追加：

```python
def test_get_tools_schema_includes_user_agents(tmp_path, monkeypatch):
    """get_tools_schema 扫描 ~/.niu/agents/ 把通用子 Agent 加入工具列表"""
    from agent import runner, subagent

    user_dir = tmp_path / "user" / "agents"
    user_dir.mkdir(parents=True)
    (user_dir / "photo-organizer.md").write_text(
        "---\ndescription: 整理照片\nmcpServers: [photo-server]\nallowAsync: true\n---\nbody"
    )
    (user_dir / "doc-summarizer.md").write_text(
        "---\ndescription: 总结文档\nmcpServers: [file-parser]\n---\nbody"
    )

    # 让 niu.md 加载走项目目录（避免找不到 niu.md）
    project_dir = tmp_path / "project"
    (project_dir / "config" / "agents").mkdir(parents=True)
    (project_dir / "config" / "agents" / "niu.md").write_text(
        "---\nsub agents: []\n---\nniu prompt"
    )

    monkeypatch.setattr(runner, "__file__", str(project_dir / "agent" / "runner.py"))
    monkeypatch.setattr(subagent, "__file__", str(project_dir / "agent" / "subagent.py"))
    monkeypatch.setattr("os.path.expanduser", lambda p: str(tmp_path / "user") if p == "~/.niu/agents" else p)

    tools = runner.get_tools_schema()
    tool_names = [t["function"]["name"] for t in tools]
    assert "chat-with-photo-organizer" in tool_names
    assert "chat-with-doc-summarizer" in tool_names


def test_get_tools_schema_skips_bad_md(tmp_path, monkeypatch):
    """YAML 解析失败的 MD 被跳过，不生成对应工具（方式 B：不允许坏工具）"""
    from agent import runner, subagent

    user_dir = tmp_path / "user" / "agents"
    user_dir.mkdir(parents=True)
    # 好 MD
    (user_dir / "good.md").write_text(
        "---\ndescription: good\nmcpServers: []\n---\nbody"
    )
    # 坏 MD（YAML 格式错误，safe_load 返回 None 或抛异常）
    (user_dir / "bad.md").write_text(
        "---\ndescription: : invalid yaml\n---\nbody"
    )

    project_dir = tmp_path / "project"
    (project_dir / "config" / "agents").mkdir(parents=True)
    (project_dir / "config" / "agents" / "niu.md").write_text(
        "---\nsub agents: []\n---\nniu prompt"
    )

    monkeypatch.setattr(runner, "__file__", str(project_dir / "agent" / "runner.py"))
    monkeypatch.setattr(subagent, "__file__", str(project_dir / "agent" / "subagent.py"))
    monkeypatch.setattr("os.path.expanduser", lambda p: str(tmp_path / "user") if p == "~/.niu/agents" else p)

    tools = runner.get_tools_schema()
    tool_names = [t["function"]["name"] for t in tools]
    assert "chat-with-good" in tool_names
    assert "chat-with-bad" not in tool_names  # 坏 MD 被跳过


def test_get_tools_schema_dedup(tmp_path, monkeypatch):
    """同名时专用子 Agent 优先，不重复生成工具"""
    from agent import runner, subagent

    project_dir = tmp_path / "project"
    project_agents = project_dir / "config" / "agents"
    project_agents.mkdir(parents=True)
    # 专用子 Agent
    (project_agents / "shared.md").write_text(
        "---\ndescription: project shared\n---\nproject body"
    )
    (project_agents / "niu.md").write_text(
        "---\nsub agents: [shared]\n---\nniu prompt"
    )
    # 用户目录同名
    user_dir = tmp_path / "user" / "agents"
    user_dir.mkdir(parents=True)
    (user_dir / "shared.md").write_text(
        "---\ndescription: user shared\n---\nuser body"
    )

    monkeypatch.setattr(runner, "__file__", str(project_dir / "agent" / "runner.py"))
    monkeypatch.setattr(subagent, "__file__", str(project_dir / "agent" / "subagent.py"))
    monkeypatch.setattr("os.path.expanduser", lambda p: str(tmp_path / "user") if p == "~/.niu/agents" else p)

    tools = runner.get_tools_schema()
    shared_tools = [t for t in tools if t["function"]["name"] == "chat-with-shared"]
    assert len(shared_tools) == 1  # 不重复
    # 描述来自专用子 Agent（项目目录优先）
    assert shared_tools[0]["function"]["description"] == "project shared"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_general_subagent.py -v`
Expected: 3 个新测试 FAIL（现有 `get_tools_schema` 不扫 `~/.niu/agents/`）

- [ ] **Step 3: 改造 `get_tools_schema`**

把 `agent/runner.py:243-330` 的 `get_tools_schema` 整体替换为：

```python
def get_tools_schema() -> list:
    """获取工具 Schema（从 JSON 文件加载 + 注册子 Agent 工具）

    子 Agent 名单来源：
    1. config/agents/niu.md 的 sub agents 字段（专用子 Agent）
    2. ~/.niu/agents/*.md 扫描（通用子 Agent，主 Agent 运行时创建）

    YAML 解析失败的 MD 被跳过（方式 B：不允许坏工具让主 Agent 看到）。
    重算返回完整 base 集（含基础工具 + MCP 工具 + 所有 chat-with-* + check_subagent_progress）。
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    schema_path = os.path.join(script_dir, "generic", "assets", "tools_schema.json")

    tools = []
    if os.path.exists(schema_path):
        with open(schema_path, "r", encoding="utf-8") as f:
            tools = json.load(f)

    # 1. 从 niu.md 读专用子 Agent 名单
    from .subagent import get_subagent_config
    try:
        niu_config = get_subagent_config("niu")
        sub_agents = list(niu_config.get("sub agents", []))
    except Exception as e:
        logger.warning(f"Failed to load niu.md sub agents config: {e}")
        sub_agents = []

    # 2. 扫描 ~/.niu/agents/*.md 加通用子 Agent 名单
    user_agents_dir = os.path.expanduser("~/.niu/agents")
    user_agent_names = []
    if os.path.isdir(user_agents_dir):
        for fname in os.listdir(user_agents_dir):
            if fname.endswith(".md") and not fname.startswith("_"):
                user_agent_names.append(os.path.splitext(fname)[0])

    # 3. 合并去重（保序：专用在前，通用在后）
    all_subagents = list(dict.fromkeys(sub_agents + user_agent_names))

    # 4. 为每个名字生成 chat-with-{name} schema
    for agent_name in all_subagents:
        try:
            agent_config = get_subagent_config(agent_name)
        except Exception as e:
            logger.warning(f"Failed to load sub-agent '{agent_name}' config: {e}")
            agent_config = {}

        # 方式 B：YAML 解析失败时 get_subagent_config 返回 {}，
        # 但我们要更严格——检查 MD 文件是否存在且 frontmatter 有效
        from .subagent import _resolve_agent_md_path
        md_path = _resolve_agent_md_path(agent_name)
        if md_path is None:
            logger.warning(f"Sub-agent '{agent_name}' MD file not found, skipping")
            continue

        # 验证 frontmatter 有效（非空 dict）
        if not agent_config:
            logger.warning(f"Sub-agent '{agent_name}' has empty/invalid frontmatter, skipping (bad MD)")
            continue

        desc = agent_config.get("description", f"子 Agent: {agent_name}")
        task_desc = agent_config.get("taskDescription", "描述要委托给子Agent执行的任务")

        # 阶段二：根据 allowAsync 决定是否暴露 async_mode
        allow_async = bool(agent_config.get("allowAsync", False))

        properties = {
            "task": {
                "type": "string",
                "description": task_desc,
            },
        }
        if allow_async:
            properties["async_mode"] = {
                "type": "boolean",
                "description": (
                    "是否异步调用。true=后台运行，立即返回派单确认（含子 Agent 唯一名）；"
                    "false（默认）=同步阻塞等结果。"
                    "异步调用后可用 check_subagent_progress 查进度、@子名 消息补充上下文、@子名 /stop 停止。"
                ),
                "default": False,
            }

        tools.append(
            {
                "type": "function",
                "function": {
                    "name": f"chat-with-{agent_name}",
                    "description": desc,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": ["task"],
                    },
                },
            }
        )

    # 阶段二：主 Agent 的 check_subagent_progress 工具
    tools.append({
        "type": "function",
        "function": {
            "name": "check_subagent_progress",
            "description": (
                "查看异步子 Agent 的进度。返回子 Agent 最近一轮 LLM 对话（请求摘要、回复、当前轮次、最近工具）。"
                "用于监控后台运行的子 Agent。同步子 Agent 无进度数据。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "subagent_name": {
                        "type": "string",
                        "description": "子 Agent 唯一名（如 file-processor-a1b2，来自派单确认或动态注入区）",
                    },
                },
                "required": ["subagent_name"],
            },
        },
    })

    return tools
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_general_subagent.py -v`
Expected: PASS（所有测试全过）

- [ ] **Step 5: 验证现有功能未回归**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -c "from agent.runner import get_tools_schema; tools = get_tools_schema(); names = [t['function']['name'] for t in tools]; print('chat-with-file-processor' in names); print('check_subagent_progress' in names)"`
Expected: 打印两行 `True`（现有子 Agent 工具和 check_subagent_progress 都在）

- [ ] **Step 6: Commit**

```bash
git add agent/runner.py tests/test_general_subagent.py
git commit -m "feat(runner): get_tools_schema 扫描 ~/.niu/agents/ 加载通用子 Agent

YAML 解析失败的 MD 被跳过（方式 B），专用子 Agent 优先，重算返回完整 base 集。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 5: `NiuRunner.__init__` 初始化 `_known_user_subagents`

**Files:**
- Modify: `agent/runner.py:433-489`

- [ ] **Step 1: 写失败测试**

在 `tests/test_general_subagent.py` 追加：

```python
def test_niu_runner_init_known_user_subagents(tmp_path, monkeypatch):
    """NiuRunner.__init__ 初始化 _known_user_subagents 集合"""
    from agent import runner, subagent

    user_dir = tmp_path / "user" / "agents"
    user_dir.mkdir(parents=True)
    (user_dir / "foo.md").write_text("---\ndescription: foo\n---\nbody")
    (user_dir / "bar.md").write_text("---\ndescription: bar\n---\nbody")

    project_dir = tmp_path / "project"
    (project_dir / "config" / "agents").mkdir(parents=True)
    (project_dir / "config" / "agents" / "niu.md").write_text("---\n---\nniu prompt")

    monkeypatch.setattr(runner, "__file__", str(project_dir / "agent" / "runner.py"))
    monkeypatch.setattr(subagent, "__file__", str(project_dir / "agent" / "subagent.py"))
    monkeypatch.setattr("os.path.expanduser", lambda p: str(tmp_path / "user") if p == "~/.niu/agents" else p)

    # mock LLM config 避免实际初始化 client
    llm_config = {"model": "test", "api_key": "test", "base_url": "http://localhost"}
    with mock.patch.object(runner.NiuRunner, "_build_static_system_prompt", return_value=""), \
         mock.patch.object(runner.NiuRunner, "_build_disk_description", return_value=""), \
         mock.patch.object(runner, "create_client", return_value=None), \
         mock.patch.object(runner, "get_skill_sync"), \
         mock.patch.object(runner, "get_registry"):
        r = runner.NiuRunner(llm_config)
    assert r._known_user_subagents == {"foo.md", "bar.md"}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_general_subagent.py::test_niu_runner_init_known_user_subagents -v`
Expected: FAIL with `AttributeError: _known_user_subagents`

- [ ] **Step 3: 在 `NiuRunner.__init__` 加初始化**

在 `agent/runner.py:451` `self.base_tools_schema = get_tools_schema()` **之前**加：

```python
        # 阶段三：跟踪 ~/.niu/agents/ 已知子 Agent 文件集合
        # chat() 入口用此集合判断是否需要重算 base_tools_schema
        user_agents_dir = os.path.expanduser("~/.niu/agents")
        if os.path.isdir(user_agents_dir):
            self._known_user_subagents = {
                f for f in os.listdir(user_agents_dir)
                if f.endswith(".md") and not f.startswith("_")
            }
        else:
            self._known_user_subagents = set()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_general_subagent.py::test_niu_runner_init_known_user_subagents -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/runner.py tests/test_general_subagent.py
git commit -m "feat(runner): NiuRunner 初始化 _known_user_subagents 集合

跟踪 ~/.niu/agents/ 已知 MD 文件，供 chat() 入口判断是否需重算 schema。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 6: `chat()` 入口加 `_refresh_base_tools_schema_if_dirty`

**Files:**
- Modify: `agent/runner.py:1955` 之前 + `NiuRunner` 类内新增方法

- [ ] **Step 1: 写失败测试**

在 `tests/test_general_subagent.py` 追加：

```python
def test_refresh_base_tools_schema_if_dirty_no_change(tmp_path, monkeypatch):
    """无新文件时不重算 base_tools_schema"""
    from agent import runner, subagent

    user_dir = tmp_path / "user" / "agents"
    user_dir.mkdir(parents=True)
    (user_dir / "foo.md").write_text("---\ndescription: foo\n---\nbody")

    project_dir = tmp_path / "project"
    (project_dir / "config" / "agents").mkdir(parents=True)
    (project_dir / "config" / "agents" / "niu.md").write_text("---\n---\nniu prompt")

    monkeypatch.setattr(runner, "__file__", str(project_dir / "agent" / "runner.py"))
    monkeypatch.setattr(subagent, "__file__", str(project_dir / "agent" / "subagent.py"))
    monkeypatch.setattr("os.path.expanduser", lambda p: str(tmp_path / "user") if p == "~/.niu/agents" else p)

    llm_config = {"model": "test", "api_key": "test", "base_url": "http://localhost"}
    with mock.patch.object(runner.NiuRunner, "_build_static_system_prompt", return_value=""), \
         mock.patch.object(runner.NiuRunner, "_build_disk_description", return_value=""), \
         mock.patch.object(runner, "create_client", return_value=None), \
         mock.patch.object(runner, "get_skill_sync"), \
         mock.patch.object(runner, "get_registry"):
        r = runner.NiuRunner(llm_config)

    original_schema = r.base_tools_schema
    r._refresh_base_tools_schema_if_dirty()
    assert r.base_tools_schema is original_schema  # 同一对象，未重算


def test_refresh_base_tools_schema_if_dirty_new_file(tmp_path, monkeypatch):
    """有新 MD 文件时重算 base_tools_schema"""
    from agent import runner, subagent

    user_dir = tmp_path / "user" / "agents"
    user_dir.mkdir(parents=True)
    (user_dir / "foo.md").write_text("---\ndescription: foo\n---\nbody")

    project_dir = tmp_path / "project"
    (project_dir / "config" / "agents").mkdir(parents=True)
    (project_dir / "config" / "agents" / "niu.md").write_text("---\n---\nniu prompt")

    monkeypatch.setattr(runner, "__file__", str(project_dir / "agent" / "runner.py"))
    monkeypatch.setattr(subagent, "__file__", str(project_dir / "agent" / "subagent.py"))
    monkeypatch.setattr("os.path.expanduser", lambda p: str(tmp_path / "user") if p == "~/.niu/agents" else p)

    llm_config = {"model": "test", "api_key": "test", "base_url": "http://localhost"}
    with mock.patch.object(runner.NiuRunner, "_build_static_system_prompt", return_value=""), \
         mock.patch.object(runner.NiuRunner, "_build_disk_description", return_value=""), \
         mock.patch.object(runner, "create_client", return_value=None), \
         mock.patch.object(runner, "get_skill_sync"), \
         mock.patch.object(runner, "get_registry"):
        r = runner.NiuRunner(llm_config)

    # 新建一个 MD 文件
    (user_dir / "bar.md").write_text("---\ndescription: bar\n---\nbody")

    r._refresh_base_tools_schema_if_dirty()
    tool_names = [t["function"]["name"] for t in r.base_tools_schema]
    assert "chat-with-bar" in tool_names  # 新子 Agent 已加入


def test_refresh_base_tools_schema_if_dirty_no_dir(tmp_path, monkeypatch):
    """~/.niu/agents/ 不存在时跳过"""
    from agent import runner, subagent

    project_dir = tmp_path / "project"
    (project_dir / "config" / "agents").mkdir(parents=True)
    (project_dir / "config" / "agents" / "niu.md").write_text("---\n---\nniu prompt")

    monkeypatch.setattr(runner, "__file__", str(project_dir / "agent" / "runner.py"))
    monkeypatch.setattr(subagent, "__file__", str(project_dir / "agent" / "subagent.py"))
    # 用户目录指向不存在的路径
    monkeypatch.setattr("os.path.expanduser", lambda p: str(tmp_path / "nonexistent") if p == "~/.niu/agents" else p)

    llm_config = {"model": "test", "api_key": "test", "base_url": "http://localhost"}
    with mock.patch.object(runner.NiuRunner, "_build_static_system_prompt", return_value=""), \
         mock.patch.object(runner.NiuRunner, "_build_disk_description", return_value=""), \
         mock.patch.object(runner, "create_client", return_value=None), \
         mock.patch.object(runner, "get_skill_sync"), \
         mock.patch.object(runner, "get_registry"):
        r = runner.NiuRunner(llm_config)

    original_schema = r.base_tools_schema
    r._refresh_base_tools_schema_if_dirty()  # 不应抛异常
    assert r.base_tools_schema is original_schema  # 未重算
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_general_subagent.py -v -k refresh`
Expected: 3 个测试 FAIL（`_refresh_base_tools_schema_if_dirty` 方法不存在）

- [ ] **Step 3: 实现 `_refresh_base_tools_schema_if_dirty` 方法**

在 `NiuRunner` 类内（`__init__` 之后，约 L490 附近）加：

```python
    def _refresh_base_tools_schema_if_dirty(self):
        """每次对话开始时扫 ~/.niu/agents/，发现新 MD 就重算 base_tools_schema。

        重算返回完整 base 集（基础工具 + MCP 工具 + 所有 chat-with-* + check_subagent_progress），
        不是差量重算。无变化时不重算（保持对象引用稳定，避免无谓拷贝）。
        """
        user_agents_dir = os.path.expanduser("~/.niu/agents")
        if not os.path.isdir(user_agents_dir):
            return

        current_files = {
            f for f in os.listdir(user_agents_dir)
            if f.endswith(".md") and not f.startswith("_")
        }

        if current_files != self._known_user_subagents:
            self._known_user_subagents = current_files
            self.base_tools_schema = get_tools_schema()
            logger.info(
                f"Refreshed base_tools_schema: {len(self.base_tools_schema)} tools "
                f"(~/.niu/agents/ changed)"
            )
```

- [ ] **Step 4: 在 `chat()` 入口调用**

找到 `agent/runner.py:1955` `tools_schema = self.base_tools_schema.copy()`，在它**之前**加一行：

```python
        # 阶段三：每次对话开始时检查 ~/.niu/agents/ 是否有新 MD
        self._refresh_base_tools_schema_if_dirty()

        # 组装 tools_schema = base tools + static MCP tools + disk
        tools_schema = self.base_tools_schema.copy()
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_general_subagent.py -v -k refresh`
Expected: PASS（3 个测试全过）

- [ ] **Step 6: 验证模块导入正常**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -c "from agent.runner import NiuRunner; print(hasattr(NiuRunner, '_refresh_base_tools_schema_if_dirty'))"`
Expected: 打印 `True`

- [ ] **Step 7: Commit**

```bash
git add agent/runner.py tests/test_general_subagent.py
git commit -m "feat(runner): chat() 入口加 _refresh_base_tools_schema_if_dirty

每次对话开始扫 ~/.niu/agents/，发现新 MD 就重算 base_tools_schema（完整 base 集）。
无变化时不重算保持引用稳定。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 7: 更新主 Agent 提示词 `config/agents/niu.md`

**Files:**
- Modify: `config/agents/niu.md`

- [ ] **Step 1: 读现有 niu.md 找插入位置**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -c "
with open('config/agents/niu.md') as f:
    content = f.read()
# 找现有子 Agent 相关段落
import re
for m in re.finditer(r'^## .*', content, re.MULTILINE):
    print(m.start(), m.group())
"`
Expected: 列出现有所有 `##` 标题，找到合适插入位置（如"子 Agent"相关段之后）

- [ ] **Step 2: 在 niu.md 加通用子 Agent 说明段**

用 Edit 工具在 `config/agents/niu.md` 适当位置（建议在现有子 Agent 说明段之后）插入：

```markdown

## 通用子 Agent

对于复杂、长时、耗时或专业性的任务，你可以创建专用的子 Agent 来处理，以减少自己上下文的占用。

### 模板位置

子 Agent 配置模板在 `config/agent-template.md`，包含所有可用 MCP 服务器清单和 frontmatter 字段说明。

### 何时创建子 Agent

- **复杂任务**：多步骤、需要长期跟踪的任务（如"整理我所有照片里的人物"）
- **耗时任务**：单个操作很慢（如批量处理几百个文件），异步调用避免阻塞你
- **专业性任务**：用户提供专业提示词或专业文档，交给专门的子 Agent 处理
- **减少上下文占用**：大段工作丢给子 Agent，你的上下文留给决策和协调

### 如何创建子 Agent

1. 读 `config/agent-template.md` 了解字段和可用 MCP 服务器
2. 用基础工具（读写文档）写新 MD 到 `~/.niu/agents/{name}.md`：
   - name 用 kebab-case（如 `photo-organizer`、`doc-summarizer`）
   - frontmatter 填 description / mcpServers / allowAsync 等
   - 正文写系统提示词
3. 当前任务结束。下一轮对话开始时，`chat-with-{name}` 工具自动出现
4. 调用 `chat-with-{name}`（同步或异步）执行任务

### 异步子 Agent

allowAsync: true 的子 Agent 支持异步调用：
- 调用后立即返回"已开始异步工作"，你不阻塞
- 子 Agent 在另一个线程跑，可主动询问你（ask_main_agent）
- 你可随时查询进度（check_subagent_progress）
- 子 Agent 完成后自动汇报，你拿结果判断下一步

### 子 Agent 交互

- 你通过 @子名 给子 Agent 发消息
- 子 Agent 可主动问你（异步模式下）
- 双击停止按钮或 /stop 可终止子 Agent
```

- [ ] **Step 3: 验证 niu.md 解析正常**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -c "from agent.subagent import get_subagent_config; c = get_subagent_config('niu'); print('sub agents' in c); print(len(c.get('sub agents', [])))"`
Expected: 打印 `True` 和现有子 Agent 数量，不报错

- [ ] **Step 4: Commit**

```bash
git add config/agents/niu.md
git commit -m "docs(prompt): niu.md 加通用子 Agent 说明段

讲清模板位置、何时创建、如何创建、同步异步选择、交互方式。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 8: 更新系统管理手册 `AGENTS.md`

**Files:**
- Modify: `AGENTS.md`

- [ ] **Step 1: 读现有 AGENTS.md 找插入位置**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && grep -n "^## " AGENTS.md | head -30`
Expected: 列出所有章节标题

- [ ] **Step 2: 在 AGENTS.md 加"通用子 Agent 体系"章节**

用 Edit 工具在 `AGENTS.md` 适当位置加：

```markdown

## 通用子 Agent 体系（阶段三）

### 设计目标

- 减少主 Agent 上下文占用（大段工作丢给子 Agent）
- 支持长时任务（异步调用不阻塞主 Agent）
- 支持专业性任务（用户提供专业提示词或文档）

### 模板位置

`config/agent-template.md`——子 Agent 配置模板，含所有可用 MCP 服务器清单和 frontmatter 字段说明。模板本身不被加载，仅供主 Agent 参考编写。

### 配置目录

- `config/agents/`——专用子 Agent（项目内置，启动加载），如 `file-processor.md`、`niu.md`
- `~/.niu/agents/`——通用子 Agent（主 Agent 运行时创建，动态加载）

同名时专用子 Agent 优先（`config/agents/` 先查）。

### 动态加载机制

程序在 `chat()` 入口（每次对话开始时）扫描 `~/.niu/agents/`，与 `NiuRunner._known_user_subagents` 集合对比，发现新 MD 文件就重算 `base_tools_schema`，新子 Agent 的 `chat-with-{name}` 工具自动出现。

- 不用 watchdog / 定时器，复用现有动态组装机制
- 主 Agent 写完 MD 后下一轮对话开始时工具才出现（自然时序）
- YAML 解析失败的 MD 被跳过（不允许坏工具让主 Agent 看到）

### MCP 工具映射

子 Agent 的 MCP 工具由 frontmatter `mcpServers` 字段指定（如 `mcpServers: [photo-server, lightrag-server]`）。加载时从已加载的全局 ToolRegistry 过滤，无需额外加载逻辑。如果 `mcpServers` 含未加载的服务器，对应工具缺失但不阻塞。

### 主 Agent 创建子 Agent 流程

1. 主 Agent 读 `config/agent-template.md`
2. 主 Agent 用基础工具（读写文档）写新 MD 到 `~/.niu/agents/{name}.md`
3. 主 Agent 当前任务结束
4. 下一轮 `chat()` 入口扫描发现新 MD → 重算 schema → `chat-with-{name}` 工具出现
5. 主 Agent 调用 `chat-with-{name}`（同步或异步）

### 同步 vs 异步调用

- **同步**：主 Agent 阻塞等子 Agent 跑完拿结果。适合短时任务。
- **异步**（`allowAsync: true` + `async_mode: true`）：立即返回"已开始异步工作"，子 Agent 后台跑。适合长时任务。异步子 Agent 完成后自动 push 完成汇报，触发主 Agent 新一轮 LLM 处理（拿结果判断下一步）。

### 与阶段一+二的衔接

- 阶段一：主子 Agent 通信通道（@消息路由、/stop 终止、双击停止）
- 阶段二：异步调用 + ask_main_agent + check_subagent_progress + 内存队列 + 5 死锁约束
- 阶段三：通用子 Agent 动态创建 + 加载

通用子 Agent 完整复用阶段一+二的全部交互能力。

### 维护注意事项

- MCP 服务器清单变化时（新增/移除 MCP 服务器），同步更新 `config/agent-template.md` 的"可用 MCP 服务器"段
- `mcp_loader.REQUIRED_SERVERS` 改动会影响子 Agent 可用工具，需检查现有通用子 Agent 的 `mcpServers` 字段是否仍有效
- 用户清理 `~/.niu/agents/` 时，下一轮 `chat()` 入口扫描会自动移除对应工具
```

- [ ] **Step 3: Commit**

```bash
git add AGENTS.md
git commit -m "docs(manual): AGENTS.md 加通用子 Agent 体系章节

覆盖设计目标、模板位置、动态加载机制、MCP 映射、创建流程、维护注意事项。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 9: 端到端真实 LLM 验证

**Files:**
- 无代码改动，纯验证

- [ ] **Step 1: 清空测试环境**

```bash
# 备份现有 ~/.niu/agents/（如有）
if [ -d ~/.niu/agents ]; then
    mv ~/.niu/agents ~/.niu/agents.backup.$(date +%s)
fi
mkdir -p ~/.niu/agents
# 清空 message db（按真实测试规范）
# （具体清空逻辑按项目既有规范执行）
```

- [ ] **Step 2: 启动程序**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && ./niu`
Expected: 程序正常启动，无 import 错误，Adapter 子进程存在

- [ ] **Step 3: 验证阶段三能力——主 Agent 创建子 Agent**

在前端发送消息：

```
我有一批照片需要整理归类。请创建一个专门的子 Agent 来处理，要求：
- 能用 photo-server 搜索照片
- 能用 lightrag-server 查询知识图谱
- 支持异步调用
```

Expected:
- 主 Agent 读 `config/agent-template.md`
- 主 Agent 写新 MD 到 `~/.niu/agents/`（如 `photo-organizer.md`）
- 主 Agent 回复用户"已创建子 Agent"
- 用 `ls ~/.niu/agents/` 确认 MD 文件存在

- [ ] **Step 4: 验证阶段三能力——新子 Agent 工具出现**

继续发送消息：

```
开始整理照片
```

Expected:
- `chat()` 入口扫描发现新 MD
- 主 Agent LLM 看到 `chat-with-photo-organizer` 工具
- 主 Agent 调用 `chat-with-photo-organizer(async_mode=true)`

- [ ] **Step 5: 验证阶段二能力——ask_main_agent**

子 Agent 跑期间，预期子 Agent 会调 `ask_main_agent` 询问主 Agent（如"照片按日期还是按人物归类？"）：

Expected:
- 主 Agent 收到询问
- 主 Agent LLM 回应
- 子 Agent 收到回答继续工作

- [ ] **Step 6: 验证阶段二能力——check_subagent_progress**

继续发送消息：

```
查一下子 Agent 进度
```

Expected:
- 主 Agent 调 `check_subagent_progress`
- 返回子 Agent 最近一轮 LLM 对话摘要

- [ ] **Step 7: 验证阶段一能力——/stop 终止**

双击停止按钮或发送 `/stop`：

Expected:
- 子 Agent 收到 /stop
- 子 Agent LLM 生成总结后退出
- 不死锁（5 死锁约束生效）

- [ ] **Step 8: 验证阶段二能力——完成汇报**

重新启动子 Agent 让它跑完：

Expected:
- 子 Agent 完成 → push 完成汇报到 MainAgentRequestQueue
- db_monitor 链路 A 检测主 Agent 空闲 → 推 SSE
- 前端调 /api/chat/session → 主 Agent 新一轮 LLM
- 主 Agent 拿结果判断下一步（继续 / 向用户汇报）

- [ ] **Step 9: 验证坏 MD 被跳过**

手动写一个坏 MD：

```bash
echo "---\ndescription: : invalid yaml\n---\nbody" > ~/.niu/agents/bad.md
```

发送新消息触发 `chat()` 入口扫描：

Expected:
- 日志记录 `Sub-agent 'bad' has empty/invalid frontmatter, skipping (bad MD)`
- `chat-with-bad` 工具不出现在主 Agent 工具列表
- 其他正常子 Agent 不受影响

- [ ] **Step 10: 清理验证环境**

```bash
# 恢复 ~/.niu/agents/ 备份（如有）
rm -rf ~/.niu/agents
if [ -d ~/.niu/agents.backup.* ]; then
    mv ~/.niu/agents.backup.* ~/.niu/agents
fi
```

- [ ] **Step 11: 记录验证结果**

在 `docs/superpowers/plans/2026-07-04-general-subagent-stage3.md` 末尾追加"端到端验证结果"段，记录每步实际表现 + 截图/日志关键片段。

- [ ] **Step 12: Commit 验证记录**

```bash
git add docs/superpowers/plans/2026-07-04-general-subagent-stage3.md
git commit -m "test(e2e): 阶段三端到端验证通过

串联阶段一+二+三全部能力：创建子 Agent、动态加载、ask_main_agent、
check_subagent_progress、/stop 不死锁、完成汇报触发新一轮。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage**：
- 模板文件 → Task 1 ✓
- 加载层改造（`_resolve_agent_md_path` / `get_tools_schema` 扫描 / `_refresh_base_tools_schema_if_dirty`）→ Task 2/3/4/5/6 ✓
- 主 Agent 提示词 → Task 7 ✓
- 系统管理手册 → Task 8 ✓
- 验证（阶段一+二+三能力）→ Task 9 ✓
- 方式 B（坏 MD 跳过）→ Task 4 Step 3 + Task 9 Step 9 ✓
- 重算语义（完整 base 集）→ Task 6 Step 3 注释 ✓

**2. Placeholder scan**：无 TBD/TODO，所有代码片段完整。

**3. Type consistency**：
- `_resolve_agent_md_path(agent_name: str) -> Optional[str]` 在 Task 2 定义，Task 4 调用 ✓
- `_known_user_subagents: set` 在 Task 5 初始化，Task 6 使用 ✓
- `_refresh_base_tools_schema_if_dirty()` 无参数，Task 6 定义 + 调用 ✓
- `get_tools_schema() -> list` 返回完整 base 集，Task 4 + Task 6 一致 ✓

---

## 执行选择

Plan complete and saved to `docs/superpowers/plans/2026-07-04-general-subagent-stage3.md`. Two execution options:

**1. Subagent-Driven (recommended)** - 我派新子 Agent 逐 Task 实施，每 Task 后做 spec + 代码质量两轮审查，快速迭代。

**2. Inline Execution** - 在本会话内逐 Task 实施，批量执行带检查点。

哪种？
