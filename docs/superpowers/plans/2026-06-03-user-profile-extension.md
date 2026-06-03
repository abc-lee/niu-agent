# 用户配置扩展 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 扩展 memory.json 的 user 字段，增加 nickname/occupation/organization，并实现主Agent注入、子Agent动态注入、config-manager扩展和文档更新。

**Architecture:** memory.json 的 user 字段从单字段扩展为四字段结构。主Agent通过 runner.py 原样注入（含占位提示文本），子Agent通过 subagent.py 动态注入（过滤占位文本和 task 类型）。config-manager 的 complete_setup 工具同步扩展。

**Tech Stack:** Python (agent/runner.py, agent/subagent.py, mcp-servers/config-manager), Markdown (Agent定义文件, 系统手册)

---

### Task 1: 主Agent系统提示词注入扩展

**Files:**
- Modify: `agent/runner.py:186-190`

- [ ] **Step 1: 修改 `_load_memory_for_prompt()` 中的用户信息注入逻辑**

将第186-190行的：
```python
    # 用户信息
    user = memory.get("user", {})
    if user.get("name"):
        user_str = f"## 用户信息\n\n用户称呼：{user['name']}"
        parts.append(user_str)
```

替换为：
```python
    # 用户信息
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

- [ ] **Step 2: 验证语法**

Run: `python -c "import ast; ast.parse(open('agent/runner.py').read()); print('OK')"`
Expected: OK

- [ ] **Step 3: 提交**

```bash
git add agent/runner.py
git commit -m "feat: extend user info injection — name/nickname/occupation/organization"
```

---

### Task 2: 子Agent动态注入 — 新增辅助函数

**Files:**
- Modify: `agent/subagent.py`

- [ ] **Step 1: 在 `call_subagent()` 函数之前新增 `_build_user_info_section()` 辅助函数**

在 `call_subagent()` 函数定义之前（约第248行），插入：

```python
def _build_user_info_section() -> str:
    """从 memory.json 构建 ## 用户信息 + ## 用户偏好 段落，供子Agent注入。

    过滤规则：
    - user 字段：值以"请询问"开头则跳过
    - permanent 字段：只注入 type="memory"，过滤 type="task"
    """
    from pathlib import Path

    memory_path = Path.home() / ".niu" / "memory.json"
    if not memory_path.exists():
        return ""

    try:
        import json
        memory = json.loads(memory_path.read_text(encoding="utf-8"))
    except Exception:
        return ""

    sections = []

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

    # 用户偏好（仅 type="memory"）
    permanent = memory.get("permanent", [])
    memory_items = [item for item in permanent if item.get("type") == "memory" and item.get("content")]
    if memory_items:
        pref_lines = [f"{i}. {item['content']}" for i, item in enumerate(memory_items, 1)]
        sections.append("## 用户偏好\n\n" + "\n".join(pref_lines))

    return "\n\n".join(sections)
```

- [ ] **Step 2: 在 `call_subagent()` 中调用辅助函数**

在第284行（`system_prompt += f"\n\nCurrent Time: ..."`）之后，第286行（`# 3. 创建 LLM 客户端`）之前，插入：

```python
    # 2.5 注入用户信息和偏好（子Agent需要了解用户背景）
    user_info_section = _build_user_info_section()
    if user_info_section:
        system_prompt += "\n\n" + user_info_section
```

- [ ] **Step 3: 验证语法**

Run: `python -c "import ast; ast.parse(open('agent/subagent.py').read()); print('OK')"`
Expected: OK

- [ ] **Step 4: 提交**

```bash
git add agent/subagent.py
git commit -m "feat: add user info + preferences injection for sub-agents"
```

---

### Task 3: config-manager complete_setup 扩展

**Files:**
- Modify: `mcp-servers/config-manager/src/niu_config_manager/__init__.py`

- [ ] **Step 1: 扩展 `complete_setup` 函数签名和逻辑**

将第631-632行的：
```python
def complete_setup(
    workspace_path: str = None, user_name: str = None, assistant_name: str = None
) -> dict[str, Any]:
```

替换为：
```python
def complete_setup(
    workspace_path: str = None,
    user_name: str = None,
    assistant_name: str = None,
    user_nickname: str = None,
    user_occupation: str = None,
    user_organization: str = None,
) -> dict[str, Any]:
```

在第659行（`memory["user"]["name"] = user_name`）之后，第661行（`# Set assistant name`）之前，插入：

```python
    if user_nickname:
        memory["user"]["nickname"] = user_nickname
    if user_occupation:
        memory["user"]["occupation"] = user_occupation
    if user_organization:
        memory["user"]["organization"] = user_organization
```

- [ ] **Step 2: 扩展 `TOOL_SCHEMAS` 中 `complete_setup` 的 schema**

在第209行（`"assistant_name"` 属性块）之后，`}` 闭合 `properties` 之前，插入：

```python
                "user_nickname": {
                    "type": "string",
                    "description": "User's nickname or preferred form of address",
                },
                "user_occupation": {
                    "type": "string",
                    "description": "User's occupation or profession",
                },
                "user_organization": {
                    "type": "string",
                    "description": "User's workplace or organization",
                },
```

- [ ] **Step 3: 扩展 `list_tools()` 中 `complete_setup` 的 schema**

在第968行（`"assistant_name"` 属性块）之后，`}` 闭合 `properties` 之前，插入与 Step 2 相同的三个属性定义。

- [ ] **Step 4: 扩展 `call_tool()` 中 `complete_setup` 的参数提取**

将第1093-1098行的：
```python
        elif name == "complete_setup":
            result = complete_setup(
                workspace_path=arguments.get("workspace_path"),
                user_name=arguments.get("user_name"),
                assistant_name=arguments.get("assistant_name"),
            )
```

替换为：
```python
        elif name == "complete_setup":
            result = complete_setup(
                workspace_path=arguments.get("workspace_path"),
                user_name=arguments.get("user_name"),
                assistant_name=arguments.get("assistant_name"),
                user_nickname=arguments.get("user_nickname"),
                user_occupation=arguments.get("user_occupation"),
                user_organization=arguments.get("user_organization"),
            )
```

- [ ] **Step 5: 验证语法**

Run: `python -c "import ast; ast.parse(open('mcp-servers/config-manager/src/niu_config_manager/__init__.py').read()); print('OK')"`
Expected: OK

- [ ] **Step 6: 更新 virtual disk 配置文件**

在 `config/disk/config-manager.yaml` 第146行（`assistant_name` 参数）之后，增加三个新参数：

```yaml
      - name: user_nickname
        type: string
      - name: user_occupation
        type: string
      - name: user_organization
        type: string
```

- [ ] **Step 7: 提交**

```bash
git add mcp-servers/config-manager/src/niu_config_manager/__init__.py config/disk/config-manager.yaml
git commit -m "feat: extend complete_setup with nickname/occupation/organization"
```

---

### Task 4: 子Agent提示词静态引导语

**Files:**
- Modify: `config/agents/entity-extractor.md`
- Modify: `config/agents/dream-evolver.md`
- Modify: `config/agents/journal-agent.md`

- [ ] **Step 1: 在 entity-extractor.md 末尾添加引导语**

在文件末尾（第140行之后）追加：

```markdown

## 用户背景

根据用户的职业、工作性质和偏好，重点提取与用户专业领域相关的有价值信息。
```

- [ ] **Step 2: 在 dream-evolver.md 末尾添加引导语**

在文件末尾（第291行之后）追加：

```markdown

## 用户背景

精加工知识图谱时，优先关联用户的专业领域和工作背景。
```

- [ ] **Step 3: 在 journal-agent.md 末尾添加引导语**

在文件末尾（第92行之后）追加：

```markdown

## 用户背景

根据用户的职业、工作性质和偏好编写日志和报告，体现专业视角。
```

- [ ] **Step 4: 提交**

```bash
git add config/agents/entity-extractor.md config/agents/dream-evolver.md config/agents/journal-agent.md
git commit -m "feat: add user background guidance to sub-agent prompts"
```

---

### Task 5: 系统手册更新

**Files:**
- Modify: `docs/manual-user-guide.md`
- Modify: `config/agents/niu.md`

- [ ] **Step 1: 在 manual-user-guide.md 的 1.5 节补充用户信息字段说明**

在第163行（`- 特点：每轮对话自动注入系统提示词，大模型始终可见`）之后，`**2. 语义记忆` 之前，插入：

```markdown

**用户信息配置（memory.json 的 user 字段）**：

| 字段 | 说明 | 示例 |
|------|------|------|
| `name` | 用户真实姓名 | 李磊 |
| `nickname` | 用户称呼/昵称，主Agent用此称呼用户 | 老板 |
| `occupation` | 用户职业，影响内容提取和日志编写的专业视角 | 软件工程师 |
| `organization` | 用户工作单位，影响内容提取和日志编写的专业视角 | 某科技公司 |

- 这些信息会自动注入到主Agent和子Agent的系统提示词中
- 缺失时主Agent会主动询问用户并写入
- 修改方式：告诉主Agent"我的职业是XXX"或"我在XXX工作"，主Agent会自动更新 memory.json
```

- [ ] **Step 2: 在 manual-user-guide.md 的 1.6 节补充用户信息收集流程**

在第188行（`> 代码中实际将 `firstRun` 设为 `false`，而非删除该字段。`）之后，第190行（`5. 完成后...`）之前，插入：

```markdown

4. 大模型询问用户基本信息（真实姓名、称呼、职业、工作单位），用户回答后写入 memory.json 的 user 字段
```

- [ ] **Step 3: 更新 niu.md 中的 memory.json 格式示例**

将第227-229行的：
```json
  "user": {
    "name": "老板"
  },
```

替换为：
```json
  "user": {
    "name": "李磊",
    "nickname": "老板",
    "occupation": "软件工程师",
    "organization": "某科技公司"
  },
```

- [ ] **Step 4: 更新 niu.md 中的字段说明表格**

将第247行的：
```markdown
| `user.name`      | 用户称呼                                            | 用户要求时由主 Agent 修改                    |
```

替换为：
```markdown
| `user.name`      | 用户真实姓名                                        | 用户要求时由主 Agent 修改                    |
| `user.nickname`  | 用户称呼/昵称                                        | 用户要求时由主 Agent 修改                    |
| `user.occupation` | 用户职业，影响子Agent内容提取和日志编写              | 用户要求时由主 Agent 修改                    |
| `user.organization` | 用户工作单位，影响子Agent内容提取和日志编写        | 用户要求时由主 Agent 修改                    |
```

- [ ] **Step 5: 提交**

```bash
git add docs/manual-user-guide.md config/agents/niu.md
git commit -m "docs: update manual and niu.md with extended user fields"
```

---

### Task 6: memory.json 迁移 — 为现有用户补充新字段

**Files:**
- Modify: `~/.niu/memory.json`（运行时数据，非代码仓库文件）

- [ ] **Step 1: 读取当前 memory.json**

Run: `cat ~/.niu/memory.json`
Expected: 包含 `"user": {"name": "老板"}` 等现有内容

- [ ] **Step 2: 为 user 字段补充新字段**

使用 `edit` 工具将：
```json
  "user": {
    "name": "老板"
  },
```

替换为：
```json
  "user": {
    "name": "老板",
    "nickname": "请询问用户真实称呼",
    "occupation": "请询问用户职业",
    "organization": "请询问用户工作单位"
  },
```

注意：`name` 保持原值"老板"不动，三个新字段填入占位提示文本。主Agent下次对话时会看到占位文本并主动询问用户。

- [ ] **Step 3: 验证 JSON 格式**

Run: `python -c "import json; json.loads(open('$HOME/.niu/memory.json').read()); print('OK')"`
Expected: OK
