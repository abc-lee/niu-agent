# 上下文去重精简 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复程序逻辑错误（子Agent task 参数 description 硬编码、动态注入来源后缀浪费 token），消除 system prompt 与 tools description 的真正重复，过滤不该出现的动态注入实体——不动核心功能提示词。

**Architecture:** 最小改动原则。修复程序硬编码错误 + 删除真正重复的内容 + 添加黑名单过滤。核心提示词（知识检索指南、照片显示语法、主动深挖策略等）不动——它们是不同模型能力下必须教给 Agent 的基本工作方法。

**Tech Stack:** Python, YAML

---

## 设计决策

### 1. 只消灭真正的重复，不动互补信息

**真正的重复**：同一段语义信息在两个位置都完整出现了，删一处不影响 Agent 能获取该信息。

| 重复类型 | system prompt 内容 | tools description 内容 | 处理 |
|----------|-------------------|----------------------|------|
| 子Agent用途描述 | 表格：3个工具名+用途 | 每个工具的 description 字段 | 删除 system prompt 表格，保留 tools description + 行为指导 |
| 脑区工具用途描述 | 3个工具名+用途列表 | 每个工具的 description 字段 | 删除 system prompt 工具列表，保留 tools description + 脑区概念解释 |
| disk 用法+目录列表 | 大段详细说明 | 精简版命令格式+目录列表 | 精简 tools description，保留 system prompt 详细版 |

**不动的内容**：
- 知识检索指南、主动深挖策略 — 不同模型能力不同，必须靠提示词教它怎么用
- 照片显示语法 — 每次显示照片都要用，不是"人脸识别时才看"的skill
- 人物命名约束 — 必须写在提示词里防止 Agent 犯错
- 子Agent行为指导（返回后直接转述、日志触发规则等）— tools description 不包含这些

### 2. 动态注入黑名单

过滤不该出现在主Agent上下文中的实体：
- `entity_type = "mcp_tool"` 或 `"tool"` — 主Agent通过 disk 发现工具，不需要在 system prompt 看到工具描述
- 实体名黑名单：源码文件名（agent_loop.py等）、内部架构概念（主Agent、context-manager、chat_idle事件等）、子Agent工具名（chat-with-file-processor等）

### 3. 子Agent工具白名单

file-processor 从 39 个工具精简到约 18 个，只保留职责所需的工具。向后兼容：没有 mcpToolFilter 配置的子Agent保持全量注入。

---

## 文件结构

| 文件 | 职责 | 操作 |
|------|------|------|
| `config/agents/niu.md` | 删除与 tools description 重复的段落 | 修改 |
| `config/agents/file-processor.md` | 添加 taskDescription + mcpToolFilter | 修改 |
| `config/agents/event-manager.md` | 添加 taskDescription | 修改 |
| `config/agents/journal-agent.md` | 添加 taskDescription | 修改 |
| `agent/runner.py` | 修复 task 参数硬编码；删除"(来源:知识图谱)"后缀；添加黑名单过滤 | 修改 |
| `agent/subagent.py` | 子Agent工具注入支持白名单过滤 | 修改 |

**不修改的文件**：lightrag 服务器、brain-region 服务器、tools_schema.json、region_injector.py、chat.html、compat.py、agent_loop.py、其他子Agent定义文件

---

### Task 1: 精简 niu.md — 只删除与 tools description 完全重复的段落

**Files:**
- Modify: `config/agents/niu.md`

**原则**：只删除与 tools description 语义完全重复的内容。保留所有核心功能提示词（知识检索指南、照片语法、行为约束等）——它们是 tools description 无法替代的。

- [ ] **Step 1: 精简"子 Agent 委托"段落 — 删除表格，保留行为指导**

当前代码（niu.md 第 98-119 行）：
```markdown
# 子 Agent 委托

**重要**：文件、照片入库等耗时任务必须使用子 Agent。

| 工具                           | 用途                         |
| ---------------------------- | -------------------------- |
| `chat-with-file-processor`   | 文档入库、照片处理、人脸-人物命名          |
| `chat-with-event-manager`    | 日程、提醒、定时任务                 |
| `chat-with-journal-agent`   | 工作日志记录、报告生成（周报/月报等） |

**流程**：调用工具 → 等待返回 → 直接转述结果给用户

**⚠️ 子 Agent 返回后**：直接把子 Agent 的返回结果转述给用户，不要自己编造或省略内容。子 Agent 的结果已包含原始文件信息，直接展示即可。

**日志触发**：
- 用户说"记录一下"、"记一下" → `chat-with-journal-agent`
- 用户说"写周报"、"写月报"、"生成报告" → `chat-with-journal-agent`

**报告偏好持久化**：用户对自动生成的日报/周报有任何要求或偏好时，必须用 `edit` 工具写入 `~/.niu/skills/report-skill.md`，不要只在对话中记住。例如：
- 用户说"周报不要写会议记录" → 修改 report-skill.md 的周报模板
- 用户说"我是软件工程师" → 填入 report-skill.md 的"用户职业"字段
```

修改为（删除表格的"用途"列——这与 tools description 完全重复；保留所有行为指导——这些是 tools description 中没有的）：
```markdown
# 子 Agent 委托

**重要**：文件、照片入库等耗时任务必须使用子 Agent（`chat-with-file-processor`、`chat-with-event-manager`、`chat-with-journal-agent`）。

**流程**：调用工具 → 等待返回 → 直接转述结果给用户

**⚠️ 子 Agent 返回后**：直接把子 Agent 的返回结果转述给用户，不要自己编造或省略内容。子 Agent 的结果已包含原始文件信息，直接展示即可。

**日志触发**：
- 用户说"记录一下"、"记一下" → `chat-with-journal-agent`
- 用户说"写周报"、"写月报"、"生成报告" → `chat-with-journal-agent`

**报告偏好持久化**：用户对自动生成的日报/周报有任何要求或偏好时，必须用 `edit` 工具写入 `~/.niu/skills/report-skill.md`，不要只在对话中记住。例如：
- 用户说"周报不要写会议记录" → 修改 report-skill.md 的周报模板
- 用户说"我是软件工程师" → 填入 report-skill.md 的"用户职业"字段
```

**说明**：表格的"用途"列（"文档入库、照片处理..."等）与 tools description 完全重复——LLM 选工具时看的是 description，表格是冗余的。但行为指导（返回后直接转述、日志触发规则、报告偏好持久化）是 tools description 中没有的，必须保留。

- [ ] **Step 2: 精简"脑区系统"的"主动控制"段落 — 删除工具名列表，保留行为说明**

当前代码（niu.md 第 90-96 行）：
```markdown
## 主动控制

如果自动调整的脑区状态不符合你的判断，你可以主动干预：
- `brain-region-server/brain_region_activate` — 点亮一个脑区（比如聊到朋友时主动点亮"人际关系"）
- `brain-region-server/brain_region_dim` — 关闭某脑区（比如某脑区注入了干扰信息，主动关闭它）
- `brain-region-server/brain_region_status` — 查看所有脑区当前状态
```

修改为（删除逐条列出的工具名+用途——与 tools description 完全重复；保留一句话行为说明）：
```markdown
## 主动控制

如果自动调整的脑区状态不符合你的判断，你可以主动干预：使用 `brain_region_activate`、`brain_region_dim`、`brain_region_status` 工具来点亮、关闭或查看脑区状态。
```

**说明**：三个工具名+用途与 tools description 完全重复，删除。但"主动干预"这个行为引导是 tools description 中没有的——它告诉 Agent 在什么情况下该用这些工具，保留。

- [ ] **Step 3: 验证语法**

Run: `cd <repo_root> && python -c "from agent.runner import NiuRunner; r = NiuRunner.__new__(NiuRunner); r.base_system_prompt = ''; print('OK')"` 

- [ ] **Step 4: Commit**

```bash
git add config/agents/niu.md
git commit -m "refactor: deduplicate niu.md — remove content duplicated by tools description"
```

---

### Task 2: 修复子Agent task 参数 description 硬编码

**Files:**
- Modify: `agent/runner.py:298-325`（`get_tools_schema` 中 chat-with-* 工具生成）

**问题**：三个子Agent的 `task` 参数 description 全都硬编码为 `"任务描述，如：处理照片：E:/path/photo.jpg"`。这对 event-manager 和 journal-agent 是误导——它们跟照片无关。

**根因**：`runner.py:316` 写死了示例文本，没有根据子Agent类型动态生成。

- [ ] **Step 1: 修复 task 参数 description**

当前代码（runner.py 第 298-325 行）：
```python
    for agent_name in sub_agents:
        try:
            agent_config = get_subagent_config(agent_name)
            desc = agent_config.get("description", f"子 Agent: {agent_name}")
        except Exception as e:
            logger.warning(f"Failed to load sub-agent '{agent_name}' config: {e}")
            desc = f"子 Agent: {agent_name}"
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": f"chat-with-{agent_name}",
                    "description": desc,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task": {
                                "type": "string",
                                "description": "任务描述，如：处理照片：E:/path/photo.jpg",
                            },
                        },
                        "required": ["task"],
                    },
                },
            }
        )

    return tools
```

修改为（从子Agent配置读取 task 描述，无配置时用通用描述；task_desc 必须在 try 块内定义）：
```python
    for agent_name in sub_agents:
        task_desc = "描述要委托给子Agent执行的任务"  # 默认值
        try:
            agent_config = get_subagent_config(agent_name)
            desc = agent_config.get("description", f"子 Agent: {agent_name}")
            task_desc = agent_config.get("taskDescription", task_desc)
        except Exception as e:
            logger.warning(f"Failed to load sub-agent '{agent_name}' config: {e}")
            desc = f"子 Agent: {agent_name}"
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": f"chat-with-{agent_name}",
                    "description": desc,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task": {
                                "type": "string",
                                "description": task_desc,
                            },
                        },
                        "required": ["task"],
                    },
                },
            }
        )
```

- [ ] **Step 2: 在子Agent配置文件中添加 taskDescription 字段**

**file-processor.md** 前置元数据修改（在 `mode: subagent` 后添加 taskDescription）：

old_string:
```yaml
mode: subagent
permissions:
```

new_string:
```yaml
mode: subagent
taskDescription: 任务描述，如：处理照片：E:/path/photo.jpg，或：入库文档：E:/path/doc.pdf
permissions:
```

**event-manager.md** 前置元数据修改（在 `temperature: 0.2` 后添加 taskDescription）：

old_string:
```yaml
temperature: 0.2
mcpServers:
```

new_string:
```yaml
temperature: 0.2
taskDescription: 任务描述，如：创建提醒：明天上午10点开会，或：查看本周日程
mcpServers:
```

**journal-agent.md** 前置元数据修改（在 `temperature: 0.3` 后添加 taskDescription）：

old_string:
```yaml
temperature: 0.3
mcpServers: []
```

new_string:
```yaml
temperature: 0.3
taskDescription: 任务描述，如：记录工作日志：完成了XXX功能的开发，或：生成本周工作周报
mcpServers: []
```

- [ ] **Step 3: 验证语法**

Run: `cd <repo_root> && python -c "from agent.runner import NiuRunner; r = NiuRunner.__new__(NiuRunner); r.base_system_prompt = ''; r._sub_agents = ['file-processor', 'event-manager', 'journal-agent']; tools = r.get_tools_schema(); [print(f'{t[\"function\"][\"name\"]}: task={t[\"function\"][\"parameters\"][\"properties\"][\"task\"][\"description\"]}') for t in tools if 'chat-with' in t['function']['name']]"`

预期：三个子Agent的 task description 各不相同。

- [ ] **Step 4: Commit**

```bash
git add agent/runner.py config/agents/file-processor.md config/agents/event-manager.md config/agents/journal-agent.md
git commit -m "fix: dynamic task description for sub-agent tools — no longer hardcoded to photo example"
```

---

### Task 3: 删除动态注入中浪费 token 的"(来源: 知识图谱)"后缀

**Files:**
- Modify: `agent/runner.py:703-747`（`_format_lightrag_entities_for_prompt` 函数）

**问题**：每条动态注入的实体都带 `(来源: 知识图谱)` 后缀，但所有实体都来自知识图谱，这个后缀毫无区分价值，纯粹浪费 token（每条约8个token）。

- [ ] **Step 1: 删除"(来源: 知识图谱)"后缀**

在 `_format_lightrag_entities_for_prompt` 函数中，找到所有 `"(来源: 知识图谱)"` 的出现位置并删除。

当前代码（两处）：
```python
            lines.append(f"{added + 1}. **{display_name}** (来源: 知识图谱)")
```
```python
                lines.append(f"{added + 1}. **{display_name}** (来源: 知识图谱)")
```

修改为：
```python
            lines.append(f"{added + 1}. **{display_name}**")
```
```python
                lines.append(f"{added + 1}. **{display_name}**")
```

- [ ] **Step 2: 更新测试文件中断言"(来源: 知识图谱)"的用例**

当前代码（tests/test_lightrag_retrieval_migration.py 第 153-164 行）：
```python
    def test_formats_skill_entities(self, runner):
        """Skill entities are formatted with (来源: 知识图谱) marker and file path."""
        entities = [
            {"entity_name": "skill:python", "description": "Python programming"},
        ]
        text, seen = runner._format_lightrag_entities_for_prompt(
            entities, "相关技能", set(),
        )
        assert "python" in text
        assert "来源: 知识图谱" in text
        assert "Python programming" in text
        assert "memory/skills/python.md" in text  # skill path annotation
```

修改为（删除对已移除后缀的断言，修正不真实的测试实体名和过期路径断言）：
```python
    def test_formats_skill_entities(self, runner):
        """Skill entities are formatted with file path annotation."""
        entities = [
            {"entity_name": "python", "description": "Python programming"},
        ]
        text, seen = runner._format_lightrag_entities_for_prompt(
            entities, "相关技能", set(),
        )
        assert "python" in text
        assert "Python programming" in text
        assert "~/.niu/skills/python.md" in text
```

**说明**：生产环境中技能实体名是文件名（如 `python`），不带 `skill:` 前缀（sync.py 中 `entity_name = skill_name`）。原测试 `"skill:python"` 不真实。

同时修正同文件中其他不真实的测试实体名：

**test_strips_type_prefix**（第 166-175 行）— 当前代码不做前缀剥离，此测试前提错误，应删除：

old_string:
```python
    def test_strips_type_prefix(self, runner):
        """Type prefixes (skill:, tool:, knowledge:) are stripped for display."""
        entities = [
            {"entity_name": "skill:git", "description": "Git version control"},
        ]
        text, seen = runner._format_lightrag_entities_for_prompt(
            entities, "相关技能", set(),
        )
        assert "skill:git" not in text  # prefix stripped
        assert "git" in text

    def test_dedup_against_seen_names(self, runner):
```

new_string（删除 test_strips_type_prefix，修正 test_dedup_against_seen_names 实体名）：
```python
    def test_dedup_against_seen_names(self, runner):
```

**test_dedup_against_seen_names**（第 177-185 行）— 修正实体名：

old_string:
```python
        entities = [
            {"entity_name": "skill:python", "description": "Python"},
        ]
        text, seen = runner._format_lightrag_entities_for_prompt(
            entities, "相关技能", {"python"},
```

new_string:
```python
        entities = [
            {"entity_name": "python", "description": "Python"},
        ]
        text, seen = runner._format_lightrag_entities_for_prompt(
            entities, "相关技能", {"python"},
```

- [ ] **Step 3: Commit**

```bash
git add agent/runner.py tests/test_lightrag_retrieval_migration.py
git commit -m "refactor: remove redundant '(来源: 知识图谱)' suffix from injected entities"
```

---

### Task 4: 动态注入添加黑名单过滤

**Files:**
- Modify: `agent/runner.py`（`_format_lightrag_entities_for_prompt` 函数，与 Task 3 同文件）

**目标**：过滤两类不该出现在主Agent system prompt 中的实体：(1) entity_type = "mcp_tool" 的实体，(2) 实体名在黑名单中的内部概念实体。

- [ ] **Step 1: 在 `_format_lightrag_entities_for_prompt` 中添加双重过滤**

当前代码（Task 3 已删除"(来源: 知识图谱)"后缀，此处为 Task 3 执行后的状态）：
```python
    def _format_lightrag_entities_for_prompt(
        self, entities: list[dict], title: str, seen_names: set[str],
    ) -> tuple[str, set[str]]:
        if not entities:
            return "", seen_names

        is_skill_section = title == "相关技能"
        lines = [f"\n\n### [{title}]"]
        added = 0
        for entity in entities:
            entity_name = entity.get("entity_name", "")
            display_name = entity_name
            if display_name in seen_names:
                continue
            seen_names.add(display_name)
            description = entity.get("description", "").replace("<SEP>", "\n")
            if description:
                lines.append(f"{added + 1}. **{display_name}**")
                lines.append(f"   {description}")
            else:
                lines.append(f"{added + 1}. **{display_name}**")
            if is_skill_section:
                lines.append(f"   路径: ~/.niu/skills/{display_name}.md")
            added += 1

        if added == 0:
            return "", seen_names
        return "\n".join(lines), seen_names
```

修改为：
```python
    # 黑名单：这些实体类型/名称不应注入到主Agent system prompt
    _INJECT_ENTITY_TYPE_BLACKLIST = {"mcp_tool", "tool"}
    _INJECT_ENTITY_NAME_BLACKLIST = {
        # 源码文件名 — 内部实现细节，对Agent对话无帮助
        "agent_loop.py", "handler.py", "tool_registry.py",
        # 内部架构概念 — system prompt 硬编码已覆盖
        "主Agent", "context-manager", "chat_idle事件",
        # 子Agent工具名 — tools description 已覆盖
        "chat-with-file-processor", "chat-with-event-manager", "chat-with-journal-agent",
    }

    def _format_lightrag_entities_for_prompt(
        self, entities: list[dict], title: str, seen_names: set[str],
    ) -> tuple[str, set[str]]:
        """Format LightRAG entity dicts for prompt injection with blacklist filtering."""
        if not entities:
            return "", seen_names

        is_skill_section = title == "相关技能"
        lines = [f"\n\n### [{title}]"]
        added = 0
        for entity in entities:
            entity_name = entity.get("entity_name", "")
            display_name = entity_name

            # 过滤黑名单实体类型（如 mcp_tool/tool — 主Agent通过 disk 发现工具）
            # 注意：LightRAG 返回的 entity_type 可能是 title case（如 "Tool"），需 .lower()
            entity_type = entity.get("entity_type", "").lower()
            if entity_type in self._INJECT_ENTITY_TYPE_BLACKLIST:
                logger.debug(f"[Inject] Skipping blacklisted type '{entity_type}': {display_name}")
                continue

            # 过滤黑名单实体名（源码文件名、硬编码已覆盖的架构概念）
            if display_name in self._INJECT_ENTITY_NAME_BLACKLIST:
                logger.debug(f"[Inject] Skipping blacklisted name: {display_name}")
                continue

            if display_name in seen_names:
                continue
            seen_names.add(display_name)
            description = entity.get("description", "").replace("<SEP>", "\n")
            if description:
                lines.append(f"{added + 1}. **{display_name}**")
                lines.append(f"   {description}")
            else:
                lines.append(f"{added + 1}. **{display_name}**")
            if is_skill_section:
                lines.append(f"   路径: ~/.niu/skills/{display_name}.md")
            added += 1

        if added == 0:
            return "", seen_names
        return "\n".join(lines), seen_names
```

**说明**：
- `_INJECT_ENTITY_TYPE_BLACKLIST` 过滤 entity_type=mcp_tool 和 entity_type=tool 的实体（file-parser、browser-automation 等工具描述）——主Agent通过 disk 发现工具，不需要在 system prompt 中看到。注意：生产数据使用 `"tool"` 而非 `"mcp_tool"`（`niu_api/injector.py` 注释 "mcp_tool removed"，LightRAG `_ENTITY_TYPE_TO_CATEGORY` 映射中 `"tool"` → `"knowledge"`），两种都过滤以确保安全
- `_INJECT_ENTITY_NAME_BLACKLIST` 过滤源码文件名和硬编码已覆盖的架构概念——这些对Agent对话无帮助或已在 system prompt 中出现
- `浏览器辅助` 实体不在黑名单中——它与 browser-automation 角度不同（Chrome Extension vs 浏览器自动化），且 browser_interact 的"action不支持type"是关键经验性知识，保留有助于防止 Agent 犯错
- 黑名单定义为类属性，便于后续扩展，避免每次调用创建集合

- [ ] **Step 2: 添加黑名单过滤测试**

在 `tests/test_lightrag_retrieval_migration.py` 的 `TestFormatLightragEntities` 类中添加：

```python
    def test_filters_mcp_tool_entity_type(self, runner):
        """Entities with entity_type=mcp_tool or tool are filtered out (case-insensitive)."""
        entities = [
            {"entity_name": "file-parser", "entity_type": "mcp_tool", "description": "Parses files"},
            {"entity_name": "browser-automation", "entity_type": "Tool", "description": "Browser tool"},
            {"entity_name": "python", "description": "Python programming"},
        ]
        text, seen = runner._format_lightrag_entities_for_prompt(
            entities, "参考知识", set(),
        )
        assert "file-parser" not in text
        assert "browser-automation" not in text
        assert "python" in text

    def test_filters_blacklisted_entity_name(self, runner):
        """Entities with blacklisted names are filtered out."""
        entities = [
            {"entity_name": "agent_loop.py", "description": "Main loop"},
            {"entity_name": "主Agent", "description": "Main agent"},
            {"entity_name": "python", "description": "Python programming"},
        ]
        text, seen = runner._format_lightrag_entities_for_prompt(
            entities, "参考知识", set(),
        )
        assert "agent_loop.py" not in text
        assert "主Agent" not in text
        assert "python" in text
```

- [ ] **Step 3: 验证语法**

Run: `cd <repo_root> && python -c "from agent.runner import NiuRunner; print('runner OK')"`

- [ ] **Step 4: Commit**

```bash
git add agent/runner.py tests/test_lightrag_retrieval_migration.py
git commit -m "refactor: add entity_type and name blacklists to dynamic injection"
```

---

### Task 5: 子Agent工具注入支持白名单过滤

**Files:**
- Modify: `config/agents/file-processor.md`（添加 `mcpToolFilter` 配置）
- Modify: `agent/subagent.py:209-253`（`get_subagent_mcp_tools_schema` 函数）

**目标**：file-processor 子Agent从39个工具精简到约18个（6基础+8photo+9lightrag白名单），只保留其职责所需的工具。

- [ ] **Step 1: 在 file-processor.md 前置元数据中添加 mcpToolFilter**

当前前置元数据（Task 2 已添加 taskDescription，此处为 Task 2 执行后的状态）：
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
---
```

修改为（添加 mcpToolFilter）：
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

**说明**：保留写入类（insert*）和状态查询类工具，删除删除/合并/编辑/查询类工具（delete_entity、delete_document、merge_entities、edit_entity、edit_relation、delete_relation、get_entity_info、get_relation_info、create_entity、create_relation、query、query_data、get_graph、timeline_query）。file-processor 的职责是入库和管理人物，不需要这些工具。

- [ ] **Step 2: 修改 `get_subagent_mcp_tools_schema` 支持白名单过滤**

当前代码（subagent.py 第 209-253 行）：
```python
def get_subagent_mcp_tools_schema(agent_name: str) -> List[Dict]:
    """
    获取子 Agent 的 MCP 工具 schema

    根据子 Agent 配置中的 mcpServers 过滤工具

    Args:
        agent_name: 子 Agent 名称

    Returns:
        MCP 工具 schema 列表（OpenAI格式）
    """
    from .tool_registry import get_registry

    config = get_subagent_config(agent_name)
    mcp_servers = config.get("mcpServers", [])

    if not mcp_servers:
        return []

    # 从 ToolRegistry 获取所有工具
    registry = get_registry()
    all_tools = registry.get_schemas()

    # 过滤出指定服务器的工具，并转换为OpenAI格式
    schema = []
    for tool in all_tools:
        tool_name = tool.get("name", "")
        # 工具名格式：server_name/tool_name
        if "/" in tool_name:
            server = tool_name.split("/")[0]
            if server in mcp_servers:
                # hidden 只对主 Agent 生效；子 Agent 由 mcpServers 白名单控制工具范围
                # 转换为OpenAI工具格式
                schema.append({
                    "type": "function",
                    "function": {
                        "name": tool_name.split("/", 1)[1],  # LLM sees bare name; handler auto-resolves to full name
                        "description": tool.get("description", ""),
                        "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
                    }
                })

    logger.info(f"[SubAgent] {agent_name}: Found {len(schema)} MCP tools for servers {mcp_servers}")
    return schema
```

修改为：
```python
def get_subagent_mcp_tools_schema(agent_name: str) -> List[Dict]:
    """
    获取子 Agent 的 MCP 工具 schema

    根据子 Agent 配置中的 mcpServers 过滤工具，支持 mcpToolFilter 白名单

    Args:
        agent_name: 子 Agent 名称

    Returns:
        MCP 工具 schema 列表（OpenAI格式）
    """
    from .tool_registry import get_registry

    config = get_subagent_config(agent_name)
    mcp_servers = config.get("mcpServers", [])
    mcp_tool_filter = config.get("mcpToolFilter", {})

    if not mcp_servers:
        return []

    # 从 ToolRegistry 获取所有工具
    registry = get_registry()
    all_tools = registry.get_schemas()

    # 过滤出指定服务器的工具，并转换为OpenAI格式
    schema = []
    for tool in all_tools:
        tool_name = tool.get("name", "")
        # 工具名格式：server_name/tool_name
        if "/" in tool_name:
            server = tool_name.split("/")[0]
            bare_name = tool_name.split("/", 1)[1]
            if server in mcp_servers:
                # 如果该服务器有白名单，只注入白名单中的工具
                server_filter = mcp_tool_filter.get(server)
                if server_filter is not None and bare_name not in server_filter:
                    continue
                # hidden 只对主 Agent 生效；子 Agent 由 mcpServers 白名单控制工具范围
                # 转换为OpenAI工具格式
                schema.append({
                    "type": "function",
                    "function": {
                        "name": bare_name,  # LLM sees bare name; handler auto-resolves to full name
                        "description": tool.get("description", ""),
                        "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
                    }
                })

    logger.info(f"[SubAgent] {agent_name}: Found {len(schema)} MCP tools for servers {mcp_servers}")
    return schema
```

**向后兼容**：`mcp_tool_filter.get(server)` 返回 None 时（没有配置白名单），`server_filter is not None` 为 False，不做过滤，全量注入。

- [ ] **Step 3: 验证语法**

Run: `cd <repo_root> && python -c "from agent.subagent import get_subagent_mcp_tools_schema; schema = get_subagent_mcp_tools_schema('file-processor'); print(f'file-processor tools: {len(schema)}'); [print(f'  {t[\"function\"][\"name\"]}') for t in schema]"`

预期：约 17 个工具（6基础 + 8photo-server + 9lightrag白名单），不再是 39 个。

- [ ] **Step 4: 验证其他子Agent不受影响**

Run: `cd <repo_root> && python -c "from agent.subagent import get_subagent_mcp_tools_schema; print('event-manager:', len(get_subagent_mcp_tools_schema('event-manager'))); print('journal-agent:', len(get_subagent_mcp_tools_schema('journal-agent')))"`

预期：event-manager 和 journal-agent 没有 mcpToolFilter，工具数量不变。

- [ ] **Step 5: Commit**

```bash
git add config/agents/file-processor.md agent/subagent.py
git commit -m "refactor: add mcpToolFilter whitelist for file-processor — reduce tools from 39 to ~17"
```

---

### Task 6: 子Agent system prompt 精简用户信息

**Files:**
- Modify: `agent/subagent.py`（`_build_user_info_section` 函数）

**目标**：子Agent需要知道工作目录（文件存放路径），但不需要完整的用户身份信息。用户偏好中的行为约束（"先做后说"）对子Agent有用，保留。

- [ ] **Step 1: 读取 `_build_user_info_section` 函数**

先读取 subagent.py 中的 `_build_user_info_section` 函数完整代码。

- [ ] **Step 2: 精简子Agent的用户信息注入**

在 `_build_user_info_section` 中，只保留工作目录和用户偏好（行为约束），跳过用户身份信息（姓名、称呼、职业、工作单位）。

当前代码（subagent.py 第 276-295 行，用户信息构建部分）：
```python
    # 用户信息
    user = memory.get("user", {})
    user_lines = []
    if user.get("name") and not str(user["name"]).startswith("请询问"):
        user_lines.append(f"真实姓名：{user['name']}")
    if user.get("nickname") and not str(user["nickname"]).startswith("请询问"):
        user_lines.append(f"称呼：{user['nickname']}")
    if user.get("occupation") and not str(user["occupation"]).startswith("请询问"):
        user_lines.append(f"职业：{user['occupation']}")
    if user.get("organization") and not str(user["organization"]).startswith("请询问"):
        user_lines.append(f"工作单位：{user['organization']}")
    if user_lines:
        sections.append("## 用户信息\n\n" + "\n".join(user_lines))
```

修改为（删除整个"用户信息"段落——子Agent不需要用户身份信息，只需工作目录和偏好）：
```python
    # 用户信息：子Agent不需要用户身份（姓名/称呼/职业/工作单位），只需工作目录和偏好
```

- [ ] **Step 3: 验证语法**

Run: `cd <repo_root> && python -c "from agent.subagent import _build_user_info_section; section = _build_user_info_section(); print(section[:200] if section else 'empty')"`

- [ ] **Step 4: Commit**

```bash
git add agent/subagent.py
git commit -m "refactor: slim user info in sub-agent prompts — keep workspace and preferences, skip identity"
```

---

### Task 7: 验证

- [ ] **Step 1: 运行见缝插针测试**

Run: `cd <repo_root> && python -m pytest tests/test_supplement_queue.py -v`

确认见缝插针功能不受影响。

- [ ] **Step 2: 启动应用验证主Agent上下文**

启动应用后，发送一条消息，检查 `logs/raw_http/` 中最新请求日志：
1. system prompt 中不应有子Agent用途表格（只有一句话列举工具名）
2. system prompt 中不应有脑区工具逐条列表（只有一句话）
3. `[参考知识]` 中不应出现 mcp_tool 类型实体、源码文件名、内部架构概念
4. 子Agent tools 数量应为约 17 个而非 39 个

- [ ] **Step 3: 更新 SYSTEM_MANUAL.md**

在"工具注入机制"段落之后添加：

```markdown
**上下文去重原则**：
- 工具用途描述只在 tools description 中出现，system prompt 不再重复列出
- 动态注入过滤 `mcp_tool` 类型实体和内部架构概念，防止工具描述和硬编码内容重复注入
- 子Agent工具按 `mcpToolFilter` 白名单过滤，只注入职责所需工具（向后兼容：无配置时全量注入）
```

- [ ] **Step 4: Commit**

```bash
git add docs/SYSTEM_MANUAL.md
git commit -m "docs: add context dedup principle to SYSTEM_MANUAL"
```

---

## 自查清单

### 1. Spec 覆盖度

| 问题 | 对应 Task | 处理方式 |
|------|-----------|---------|
| 子Agent用途描述双写 | Task 1 | 删除 system prompt 表格，保留 tools description + 行为指导 |
| 脑区工具用途描述双写 | Task 1 | 删除 system prompt 工具列表，保留 tools description + 行为说明 |
| 子Agent task 参数 description 硬编码 | Task 2 | 从子Agent配置动态读取 taskDescription |
| "(来源: 知识图谱)" 后缀浪费token | Task 3 | 删除后缀 |
| 动态注入泄露 mcp_tool | Task 4 | entity_type 黑名单过滤 |
| 动态注入泄露源码文件名 | Task 4 | 实体名黑名单过滤 |
| 动态注入泄露内部架构概念 | Task 4 | 实体名黑名单过滤 |
| 子Agent工具列表膨胀(39) | Task 5 | mcpToolFilter 白名单 |
| 子Agent用户信息冗余 | Task 6 | 精简为 workspace + preferences |

**不动的内容**（确认不改动）：
- 知识检索指南和主动深挖策略 — 不同模型能力下必须教 Agent 的核心方法
- 照片显示语法 — 每次显示照片都要用，删了就不会显示照片
- 人物命名约束 — 必须写在提示词防止犯错
- disk 详细用法 — system prompt 的详细版比 tools description 的精简版更有指导价值

### 2. Placeholder 扫描

Task 4 的 Step 1-2 需要先读取 `_build_user_info_section` 函数。这是唯一需要运行时读取的步骤。

### 3. 类型一致性

- `mcpToolFilter` 在 YAML 中是 `dict[str, list[str]]`
- `mcp_tool_filter.get(server)` 返回 `list[str] | None`，`None` 表示无白名单（全量注入）
- `entity_type` 和 `display_name` 从 lightrag 返回的 entity dict 中获取，是 `str`
- `_INJECT_ENTITY_TYPE_BLACKLIST` 和 `_INJECT_ENTITY_NAME_BLACKLIST` 是 `set[str]` 类属性