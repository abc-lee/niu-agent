# 用户配置扩展设计 — 增加 nickname/occupation/organization 字段

## 背景

`~/.niu/memory.json` 的 `user` 字段目前只有 `name`（称呼），缺少职业和工作单位信息。主Agent、内容提取Agent（entity-extractor）、梦境进化Agent（dream-evolver）、日志Agent（journal-agent）在处理任务时无法了解用户的专业背景，导致：
- 内容提取Agent难以判断哪些信息对用户有价值
- 日志Agent无法从专业视角编写报告
- 梦境进化Agent无法优先关联用户专业领域的知识

## 设计

### 1. memory.json 结构变更

**当前**：
```json
{
  "user": {
    "name": "老板"
  }
}
```

**新结构**：
```json
{
  "user": {
    "name": "李磊",
    "nickname": "老板",
    "occupation": "软件工程师",
    "organization": "某科技公司"
  }
}
```

- `name`：用户真实姓名（原字段，语义从"称呼"变为"真名"）
- `nickname`：用户称呼/昵称（新增）
- `occupation`：用户职业（新增）
- `organization`：用户工作单位（新增）

**首次配置默认值**（占位提示文本）：
```json
{
  "user": {
    "name": "请询问用户真实名字",
    "nickname": "请询问用户真实称呼",
    "occupation": "请询问用户职业",
    "organization": "请询问用户工作单位"
  }
}
```

**迁移策略**：已有 `user.name` 的用户，其他三个字段填入占位提示文本。`name` 保持原值不动。

### 2. 主Agent系统提示词注入

**改动文件**：`agent/runner.py` 的 `_load_memory_for_prompt()` 函数（第186-190行）

**当前逻辑**：
```python
user = memory.get("user", {})
if user.get("name"):
    user_str = f"## 用户信息\n\n用户称呼：{user['name']}"
    parts.append(user_str)
```

**新逻辑**：
```python
user = memory.get("user", {})
user_lines = []
if user.get("name"):
    user_lines.append(f"真实姓名：{user['name']}")
if user.get("nickname"):
    user_lines.append(f"称呼：{user['nickname']}")
if user.get("occupation"):
    user_lines.append(f"职业：{user['occupation']}")
if user.get("organization"):
    user_lines.append(f"工作单位：{user['organization']}")
if user_lines:
    user_str = "## 用户信息\n\n" + "\n".join(user_lines)
    parts.append(user_str)
```

主Agent原样注入，不做过滤。占位提示文本（如"请询问用户真实名字"）直接出现在系统提示词中，主Agent读到后自然询问用户，然后通过 `read`/`edit` 工具修改 memory.json。

### 3. 子Agent动态注入

**改动文件**：`agent/subagent.py`

**现状**：子Agent的系统提示词是静态 `.md` 文件，没有动态注入用户信息的能力。

**实现位置**：`call_subagent()` 函数中，第284行（追加当前时间）之后、创建 LLM 客户端之前，插入读取 `memory.json` 并追加用户信息的逻辑。

**新增辅助函数**：`_build_user_info_section() -> str`
- 读取 `~/.niu/memory.json`
- 过滤占位文本和 task 类型条目
- 格式化为 `## 用户信息` + `## 用户偏好` 段落
- 返回格式化字符串（为空则返回空串）

**新增逻辑**：构建子Agent系统提示词时，从 `memory.json` 读取 `user` 和 `permanent` 字段，过滤后追加到系统提示词末尾。

**注入格式**：
```
## 用户信息

真实姓名：李磊
称呼：老板
职业：软件工程师
工作单位：某科技公司

## 用户偏好

1. 执行操作必须实际调用工具，不能只做口头确认
2. 座右铭：先做后说，不做不说
```

**过滤规则**：
- `user` 字段：如果值以"请询问"开头，跳过该行。四个字段全部是占位文本时，不注入 `## 用户信息` 段
- `permanent` 字段：只注入 `type: "memory"` 的条目，过滤掉 `type: "task"`。没有 memory 类型条目时不注入 `## 用户偏好` 段

> **边界说明**：占位文本判断使用 `value.startswith("请询问")`。极罕见情况下用户真实职业恰好以"请询问"开头会被误判，但实际场景中几乎不会发生，保持简单方案。

### 4. config-manager complete_setup 扩展

**改动文件**：`mcp-servers/config-manager/src/niu_config_manager/__init__.py`

`complete_setup` 工具增加三个参数：
- `user_nickname`（称呼）
- `user_occupation`（职业）
- `user_organization`（工作单位）

函数逻辑扩展：将新参数写入 `memory["user"]` 对应字段。

**需同步更新的位置**：
1. `TOOL_SCHEMAS["complete_setup"]["input_schema"]["properties"]` — 增加 `user_nickname`、`user_occupation`、`user_organization` 的 schema 定义
2. `list_tools()` 函数中 `complete_setup` 工具的 `inputSchema["properties"]` — 同步增加三个参数的 schema
3. `call_tool()` 函数中 `complete_setup` 的参数提取逻辑 — 增加 `arguments.get("user_nickname")` 等

主Agent也可通过虚拟磁盘的 config-manager 工具或直接 `read`/`edit` 操作 memory.json 来更新这些字段。

### 5. 子Agent提示词静态引导语

在各子Agent的 `.md` 文件中增加引导语，与动态注入的标题 `## 用户信息`、`## 用户偏好` 上下对应：

- **entity-extractor.md**：增加"根据用户的职业、工作性质和偏好，重点提取与用户专业领域相关的有价值信息"
- **dream-evolver.md**：增加"精加工知识图谱时，优先关联用户的专业领域和工作背景"
- **journal-agent.md**：增加"根据用户的职业、工作性质和偏好编写日志和报告，体现专业视角"

### 6. 系统手册更新

**改动文件**：`docs/manual-user-guide.md`

**改动位置**：1.5 记忆管理 + 1.6 首次使用流程

**1.5 记忆管理新增内容**：

在"用户长期记忆"部分，补充 `user` 字段的详细说明：

```
用户信息（memory.json 的 user 字段）：

| 字段 | 说明 | 示例 |
|------|------|------|
| name | 用户真实姓名 | 李磊 |
| nickname | 用户称呼/昵称，主Agent用此称呼用户 | 老板 |
| occupation | 用户职业，影响内容提取和日志编写的专业视角 | 软件工程师 |
| organization | 用户工作单位，影响内容提取和日志编写的专业视角 | 某科技公司 |

- 这些信息会自动注入到主Agent和子Agent的系统提示词中
- 缺失时主Agent会主动询问用户并写入
- 修改方式：告诉主Agent"我的职业是XXX"或"我在XXX工作"，主Agent会自动更新 memory.json
```

**1.6 首次使用流程新增内容**：

在首次使用流程中增加用户信息收集步骤：

```
首次使用时，如果 user 字段中的 name/nickname/occupation/organization 仍为占位提示文本
（以"请询问"开头），主Agent会在系统提示词中看到这些提示，并主动询问用户：

1. 询问用户的真实姓名
2. 询问用户的称呼/昵称
3. 询问用户的职业
4. 询问用户的工作单位

用户回答后，主Agent通过 read/edit 工具将真实信息写入 memory.json 的 user 字段。
```

**改动文件**：`config/agents/niu.md`

在 memory.json 字段说明表格中，扩展 `user` 字段的文档：

| 字段 | 用途 | 谁写入 |
|------|------|--------|
| `user.name` | 用户真实姓名 | 用户要求时由主 Agent 修改 |
| `user.nickname` | 用户称呼/昵称 | 用户要求时由主 Agent 修改 |
| `user.occupation` | 用户职业 | 用户要求时由主 Agent 修改 |
| `user.organization` | 用户工作单位 | 用户要求时由主 Agent 修改 |

## 改动清单

| 文件 | 改动类型 | 说明 |
|------|---------|------|
| `agent/runner.py` | 修改 | `_load_memory_for_prompt()` 扩展 user 字段注入 |
| `agent/subagent.py` | 修改 | 新增子Agent动态注入 user + permanent 逻辑 |
| `mcp-servers/config-manager/src/niu_config_manager/__init__.py` | 修改 | `complete_setup` 增加三个参数 |
| `config/agents/entity-extractor.md` | 修改 | 增加用户背景引导语 |
| `config/agents/dream-evolver.md` | 修改 | 增加用户背景引导语 |
| `config/agents/journal-agent.md` | 修改 | 增加用户背景引导语 |
| `config/agents/niu.md` | 修改 | 更新 memory.json 字段说明文档 |
| `docs/manual-user-guide.md` | 修改 | 1.5节补充 user 字段详细说明，1.6节补充首次使用用户信息收集流程 |
