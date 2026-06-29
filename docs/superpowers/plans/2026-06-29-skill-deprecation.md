# Skill 三级降级机制实施计划

**日期**: 2026-06-29
**作者**: 实施计划撰写员
**状态**: 待执行

## Goal

为 skill 系统新增"三级降级机制"，让主Agent的反馈驱动 skill 降级，最终删除垃圾 skill。解决当前 skill 只增不减、垃圾累积的问题。

## Architecture

### 三级状态机

```
                    失败反馈 ×3
   ┌──────────────────────────────────┐
   │                                  ▼
┌──┴────────┐   失败×1   ┌────────────┴──┐   失败×1   ┌──────────┐
│  active   │ ─────────► │  deprecated   │ ─────────► │ .trash/  │
│ (issue=N) │            │  (待观察)      │            │ (删除)    │
└────┬──────┘            └───────┬───────┘            └──────────┘
     │                           │ 成功×1
     │ 成功×1                    │
     │ issue-=1                  │ 复活
     ▼                           │
┌────────────┐ ◄─────────────────┘
│  active    │
│ issue=0    │
└────────────┘
```

- **active + issue_count=N**：正常状态，记录失败反馈次数
- **active 下累计 3 次失败反馈** → 降级到 deprecated
- **deprecated**：观察态，仍注入主 Agent 但加 `[待观察]` 前缀
- **deprecated 下 1 次失败反馈** → dream-evolver 用 bash 移到 `~/.niu/skills/.trash/` + SkillSync 自动删 LightRAG 实体
- **deprecated 下 1 次成功反馈** → 复活到 active，issue_count 清零
- **active 下成功使用** → issue_count 减 1（衰减，最低到 0）

### 数据流

```
主Agent使用skill → 反馈成功/失败
       ↓
dream-evolver读取反馈
       ↓
判断skill当前status
       ↓
┌─ active + 失败 ──► issue_count+=1，若≥3则改status=deprecated
├─ active + 成功 ──► issue_count-=1（最低0）
├─ deprecated + 失败 ──► bash mv到.trash/，SkillSync下次扫描自动删LightRAG
└─ deprecated + 成功 ──► 改status=active，issue_count=0
       ↓
frontmatter修改触发watchdog → SkillSync重注入 → LightRAG前缀更新
```

## Tech Stack

- **Python**: agent/injector/sync.py (SkillSync)、agent/runner.py (动态注入)
- **Markdown**: config/agents/dream-evolver.md、config/agents/niu.md (提示词)
- **LightRAG**: skill 实体注入/删除（通过 SkillSync 现有机制）
- **Bash**: dream-evolver 执行文件移动（已确认 dream-evolver 默认继承 bash 工具，无 disableBaseTools 配置）

## 前置确认

### dream-evolver 工具确认
- dream-evolver.md 无 `disableBaseTools` 配置
- subagent.py (415-430行)：子 Agent 默认继承所有基础工具，仅当配置 disableBaseTools 时才移除
- 结论：dream-evolver 默认拥有 bash、read、write、edit、code_run 工具
- 验证命令：`grep -n "disableBaseTools" config/agents/dream-evolver.md`（应无输出）

### SkillSync 现有机制确认
- watchdog 监控 `~/.niu/skills/*.md`（sync.py 760-770行）
- 文件删除事件触发 `_delete_skill_from_lightrag`（sync.py 83-89、125-129行）
- 文件移动到 `.trash/` 会被识别为原文件删除 → 自动触发 LightRAG 实体删除
- `.trash/` 目录在 `skills_dir.glob("*.md")` 之外（不递归），不会重新注入

### 关键文件行号定位
- `agent/injector/sync.py:496-498` — draft 前缀处理
- `agent/runner.py:1531-1532` — 草稿前缀检测分支
- `config/agents/dream-evolver.md:277-294` — frontmatter 规范
- `config/agents/dream-evolver.md:197-206` — 阶段C操作类型表
- `config/agents/dream-evolver.md:210-218` — C2判断逻辑
- `config/agents/dream-evolver.md:263-266` — 草稿转正逻辑
- `config/agents/dream-evolver.md:405-409` — 工具列表
- `config/agents/niu.md:141-144` — 反馈要求

---

## Task 1: sync.py 加 deprecated 前缀处理

**目标**：SkillSync 注入 LightRAG 时，识别 `status: deprecated` 并在 description 前加 `[待观察]` 前缀，让主 Agent 能看到状态标记。

**Files**:
- `agent/injector/sync.py`

### 步骤

- [ ] 1.1 搜索定位：在 `agent/injector/sync.py` 中搜索 `status") == "draft"`，确认当前代码在约 497 行：
  ```bash
  grep -n 'status") == "draft"' agent/injector/sync.py
  ```
- [ ] 1.2 读取上下文：读取 494-500 行确认精确文本：
  ```bash
  sed -n '494,500p' agent/injector/sync.py
  ```
- [ ] 1.3 用 Edit 替换。`old_string` 必须精确匹配当前文件内容：
  ```python
              # 标记草稿 skill（fm 已在上方由 parse_yaml_frontmatter 解析）
              if isinstance(fm, dict) and fm.get("status") == "draft":
                  description = f"[草稿] {description}"
  ```
  `new_string`:
  ```python
              # 标记草稿/待观察 skill（fm 已在上方由 parse_yaml_frontmatter 解析）
              status = fm.get("status") if isinstance(fm, dict) else None
              if status == "draft":
                  description = f"[草稿] {description}"
              elif status == "deprecated":
                  description = f"[待观察] {description}"
  ```
- [ ] 1.4 语法检查：
  ```bash
  python -c "import ast; ast.parse(open('agent/injector/sync.py').read()); print('OK')"
  ```

### 验证

- [ ] 1.5 手动构造 deprecated skill 文件测试注入：
  ```bash
  # 创建测试 skill
  mkdir -p ~/.niu/skills
  cat > ~/.niu/skills/_test_deprecated.md <<'EOF'
  ---
  name: _test_deprecated
  description: Use when testing deprecated prefix
  status: deprecated
  issue_count: 3
  created: 2026-06-29
  last_tested: 2026-06-29
  ---
  # Test Deprecated
  ## Overview
  Test skill.
  EOF
  ```
- [ ] 1.6 等待 SkillSync 扫描（最多 60 秒）或手动触发同步：
  ```bash
  python -c "
  from agent.injector.sync import get_skill_sync
  sync = get_skill_sync(auto_start=False)
  sync.scan_and_sync()
  "
  ```
- [ ] 1.7 在 LightRAG 中搜索实体，确认 description 以 `[待观察]` 开头：
  ```bash
  python -c "
  from niu_api.internal.lightrag_adapter import LightRAGAdapter
  adapter = LightRAGAdapter()
  result = adapter.search_entities(query='_test_deprecated', top_k=5, keywords=['_test_deprecated'])
  for e in result.get('data', []):
      if e.get('entity_name') == '_test_deprecated':
          print('description:', e.get('description'))
  "
  ```
  预期输出包含：`[待观察] Use when testing deprecated prefix`
- [ ] 1.8 清理测试文件：
  ```bash
  rm ~/.niu/skills/_test_deprecated.md
  ```

### 提交

- [ ] 1.9 临时提交：
  ```bash
  git add agent/injector/sync.py
  git commit -m "feat(skill): SkillSync adds [待观察] prefix for deprecated status — backup before Task 2

  Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
  ```

---

## Task 2: runner.py 加 [待观察] 检测分支

**目标**：主 Agent system prompt 注入 skill 时，识别 `[待观察]` 前缀并加提醒行，强制要求使用后反馈。

**Files**:
- `agent/runner.py`

### 步骤

- [ ] 2.1 搜索定位：
  ```bash
  grep -n '草稿skill — 使用后反馈效果' agent/runner.py
  ```
  确认在约 1532 行。
- [ ] 2.2 读取上下文（1529-1533行）确认精确文本：
  ```bash
  sed -n '1529,1533p' agent/runner.py
  ```
- [ ] 2.3 用 Edit 替换。`old_string`:
  ```python
              if is_skill_section:
                  lines.append(f"   路径: ~/.niu/skills/{display_name}.md")
                  if description.startswith("[草稿]"):
                      lines.append(f"   ⚠️ 草稿skill — 使用后反馈效果")
  ```
  `new_string`:
  ```python
              if is_skill_section:
                  lines.append(f"   路径: ~/.niu/skills/{display_name}.md")
                  if description.startswith("[草稿]"):
                      lines.append(f"   ⚠️ 草稿skill — 使用后反馈效果")
                  elif description.startswith("[待观察]"):
                      lines.append(f"   ⚠️ 待观察skill — 此skill有历史问题，使用后必须反馈效果（成功或失败）")
  ```
- [ ] 2.4 语法检查：
  ```bash
  python -c "import ast; ast.parse(open('agent/runner.py').read()); print('OK')"
  ```

### 验证

- [ ] 2.5 启动 API 服务，构造 deprecated skill，触发注入：
  ```bash
  # 创建 deprecated skill
  cat > ~/.niu/skills/_test_deprecated.md <<'EOF'
  ---
  name: _test_deprecated
  description: Use when testing deprecated injection
  status: deprecated
  issue_count: 3
  created: 2026-06-29
  last_tested: 2026-06-29
  ---
  # Test
  ## Overview
  Test.
  EOF
  ```
- [ ] 2.6 发送一条触发该 skill 的对话消息，检查日志中 system prompt 是否包含提醒：
  ```bash
  grep -r "待观察skill — 此skill有历史问题" logs/ | tail -5
  ```
  预期：日志中出现注入的提醒行。
- [ ] 2.7 清理测试文件：
  ```bash
  rm ~/.niu/skills/_test_deprecated.md
  ```

### 提交

- [ ] 2.8 临时提交：
  ```bash
  git add agent/runner.py
  git commit -m "feat(skill): runner injects [待观察] reminder for deprecated skills — backup before Task 3

  Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
  ```

---

## Task 3: dream-evolver.md 更新（frontmatter规范 + 阶段C操作 + 删除操作）

**目标**：dream-evolver 提示词增加三级降级逻辑：frontmatter 加 `issue_count` 字段，阶段 C 操作表增加降级/复活/删除分支，明确删除操作的具体 bash 命令。

**Files**:
- `config/agents/dream-evolver.md`

### 步骤

#### 3.0 更新阶段A信号识别（A2，约147-155行）

- [ ] 3.0.1 搜索定位：
  ```bash
  grep -n "明确的 skill 反馈\|skill 被使用且成功\|skill 被使用但失败" config/agents/dream-evolver.md
  ```
  确认在约 147-155 行的 A2 信号观察列表。

- [ ] 3.0.2 用 Edit 在 A2 信号列表末尾追加两条新信号。`old_string`:
  ```markdown
  - ✦ **有效规则没被遵守**：skill 中的规则是正确的，但对话中的 assistant 行为没有遵循

  不需要主动扫描 skill 目录，只关注消息中呈现的信号。
  ```
  `new_string`:
  ```markdown
  - ✦ **有效规则没被遵守**：skill 中的规则是正确的，但对话中的 assistant 行为没有遵循
  - ✦ **待观察 skill 的成功反馈**：assistant 消息中包含"待观察 skill [name] 本次使用成功"或类似表述——识别后走步骤 C4 成功路径（deprecated → active 复活）
  - ✦ **待观察 skill 的失败反馈**：assistant 消息中包含"待观察 skill [name] 本次使用失败"或类似表述——识别后走步骤 C5 删除路径

  不需要主动扫描 skill 目录，只关注消息中呈现的信号。
  ```

#### 3.1 更新 frontmatter 规范（277-294行）

- [ ] 3.1.1 搜索定位：
  ```bash
  grep -n 'status: draft | active' config/agents/dream-evolver.md
  ```
  确认在约 283 行。
- [ ] 3.1.2 用 Edit 替换 frontmatter 格式块。`old_string`:
  ```yaml
  ```yaml
  ---
  name: skill-name-with-hyphens
  description: Use when [触发条件，不写工作流]
  status: draft | active
  created: YYYY-MM-DD
  last_tested: YYYY-MM-DD
  ---
  ```
  ```
  `new_string`:
  ```yaml
  ```yaml
  ---
  name: skill-name-with-hyphens
  description: Use when [触发条件，不写工作流]
  status: draft | active | deprecated
  issue_count: 0
  created: YYYY-MM-DD
  last_tested: YYYY-MM-DD
  ---
  ```
  ```
- [ ] 3.1.3 用 Edit 替换字段说明。`old_string`:
  ```
  字段说明：
  - `name`：只含字母、数字、连字符，不用下划线、不用中文、不用空格
  - `description`：以 "Use when..." 开头，**只写触发条件，不写工作流**。包含具体症状和情境，500 字符以内
  - `status`：新建时写 `draft`，验证通过后改为 `active`
  - `created`：创建日期
  - `last_tested`：最近一次验证或修改日期
  ```
  `new_string`:
  ```
  字段说明：
  - `name`：只含字母、数字、连字符，不用下划线、不用中文、不用空格
  - `description`：以 "Use when..." 开头，**只写触发条件，不写工作流**。包含具体症状和情境，500 字符以内
  - `status`：三态生命周期
    - `draft`：新建草稿，使用后根据反馈转 `active` 或继续修改
    - `active`：正常使用，issue_count 跟踪失败次数
    - `deprecated`：待观察态，仍注入主 Agent 但加 `[待观察]` 前缀；下次成功反馈复活，下次失败反馈删除
  - `issue_count`：失败反馈累计计数（仅 active 状态有意义）
    - active 下每次失败反馈 +1；达到 3 时降级为 deprecated
    - active 下每次成功反馈 -1（最低 0，衰减机制，避免历史问题永久累积）
    - 降级为 deprecated 时保留当前值
    - 复活为 active 时清零为 0
    - 新建 skill 默认为 0
  - `created`：创建日期
  - `last_tested`：最近一次验证或修改日期
  ```

#### 3.2 更新阶段C操作类型表（197-206行）

- [ ] 3.2.1 搜索定位：
  ```bash
  grep -n 'assistant 消息中明确反馈 skill 成功' config/agents/dream-evolver.md
  ```
  确认在约 199 行。
- [ ] 3.2.2 用 Edit 替换整个操作类型表。`old_string`:
  ```markdown
  | 信号 | 操作 |
  |------|------|
  | assistant 消息中明确反馈 skill 成功 | 如果 skill 状态是 draft → 改为 active |
  | assistant 消息中明确反馈 skill 有问题 | 进入步骤 C2 判断 |
  | 重复模式（出现 2 次以上）且无对应 skill | 创建新 skill（草稿） |
  | 多轮失败后找到方案 | 创建新 skill（草稿），记录坑点 |
  | skill 被使用且任务成功（无明确反馈时） | 如果 skill 状态是 draft → 改为 active |
  | skill 被使用但任务失败（无明确反馈时） | 进入步骤 C2 判断 |
  | 有效规则没被遵守 | 不改正文，在"执行提醒"区域添加提醒 |
  | skill 被读取但未被引用 | 视为"未使用"，不触发任何操作 |
  ```
  `new_string`:
  ```markdown
  | 信号 | 操作 |
  |------|------|
  | assistant 消息中明确反馈 skill 成功 | 进入步骤 C4 成功路径（按当前 status 处理） |
  | assistant 消息中明确反馈 skill 有问题 | 进入步骤 C2 判断失败原因，再走 C4 失败路径 |
  | 重复模式（出现 2 次以上）且无对应 skill | 创建新 skill（草稿） |
  | 多轮失败后找到方案 | 创建新 skill（草稿），记录坑点 |
  | skill 被使用且任务成功（无明确反馈时） | 进入步骤 C4 成功路径（draft 状态转 active，active 状态 issue_count-=1） |
  | skill 被使用但任务失败（无明确反馈时） | 进入步骤 C2 判断，再走 C4 失败路径 |
  | 有效规则没被遵守 | 不改正文，在"执行提醒"区域添加提醒 |
  | skill 被读取但未被引用 | 视为"未使用"，不触发任何操作 |

  **降级机制速查**（步骤 C4 依据）：
  | 当前 status | 反馈类型 | 操作 |
  |-------------|---------|------|
  | draft | 成功 | 改 status=active，issue_count=0 |
  | draft | 失败 | 按 C2 修改正文（draft 阶段不计 issue_count） |
  | active | 成功 | issue_count-=1（最低 0） |
  | active | 失败 | issue_count+=1；若 ≥3 改 status=deprecated |
  | deprecated | 成功 | 改 status=active，issue_count=0（复活） |
  | deprecated | 失败 | 执行删除操作（步骤 C5） |
  ```

#### 3.3 更新 C3 执行操作（263-266行草稿转正逻辑）

- [ ] 3.3.1 搜索定位：
  ```bash
  grep -n '验证草稿 skill' config/agents/dream-evolver.md
  ```
  确认在约 263 行。
- [ ] 3.3.2 用 Edit 替换"验证草稿 skill"块。`old_string`:
  ```markdown
  **验证草稿 skill：**
  1. `read` 读取目标 skill 文件
  2. 如果信号表明该 skill 被使用且任务成功 → `edit` 把 `status: draft` 改为 `status: active`，同时删除 When to Use 区域下的 `> ⚠️ 此 skill 为草稿状态，使用后请反馈效果` 提示行
  3. 如果信号表明该 skill 被使用但任务失败 → 按步骤 C2 处理
  ```
  `new_string`:
  ```markdown
  **验证草稿 skill / 成功反馈路径（步骤 C4 成功）：**
  1. `read` 读取目标 skill 文件
  2. 根据 frontmatter 中的 `status` 决定操作：
     - **status=draft** → `edit` 把 `status: draft` 改为 `status: active`，确保 `issue_count: 0` 存在，同时删除 When to Use 区域下的 `> ⚠️ 此 skill 为草稿状态，使用后请反馈效果` 提示行
     - **status=active** → `edit` 把 `issue_count` 减 1（最低 0）。若当前值已是 0 则不改
     - **status=deprecated** → `edit` 把 `status: deprecated` 改为 `status: active`，`issue_count` 改为 `0`（复活）
  3. 更新 `last_tested` 日期

  **失败反馈路径（步骤 C4 失败）：**
  1. `read` 读取目标 skill 文件
  2. 先按步骤 C2 判断失败原因并修改正文（如需要）
  3. 根据 frontmatter 中的 `status` 决定后续：
     - **status=draft** → 不改 status（draft 阶段不计 issue_count，只改正文）
     - **status=active** → `edit` 把 `issue_count` 加 1。若加 1 后 ≥3，同时把 `status: active` 改为 `status: deprecated`
     - **status=deprecated** → 执行步骤 C5 删除操作
  4. 更新 `last_tested` 日期
  ```

#### 3.4 新增步骤 C5 删除操作（在"添加执行提醒"之后插入）

- [ ] 3.4.1 搜索定位"添加执行提醒"块结尾：
  ```bash
  grep -n '如果提醒已超过 5 条' config/agents/dream-evolver.md
  ```
  确认在约 271 行。
- [ ] 3.4.2 用 Edit 在"添加执行提醒"块之后、"## Skill 文件规范"之前插入 C5 块。`old_string`:
  ```markdown
  **添加执行提醒：**
  1. `read` 读取目标 skill 文件
  2. `edit` 在 `<!-- 执行提醒 -->` 下方添加一条简短提醒，重申已有规则（不引入新规则）
  3. 如果提醒已超过 5 条，合并去重

  ## Skill 文件规范（知识储备）
  ```
  `new_string`:
  ```markdown
  **添加执行提醒：**
  1. `read` 读取目标 skill 文件
  2. `edit` 在 `<!-- 执行提醒 -->` 下方添加一条简短提醒，重申已有规则（不引入新规则）
  3. 如果提醒已超过 5 条，合并去重

  **删除 skill（步骤 C5 — 仅 deprecated 状态 + 失败反馈时执行）：**
  1. `read` 读取目标 skill 文件，确认 `status: deprecated`（非 deprecated 禁止删除）
  2. 用 `bash` 工具执行以下命令（将 `<skill-name>` 替换为实际 skill 文件名，不含 .md 后缀）：
     ```bash
     mkdir -p ~/.niu/skills/.trash && mv ~/.niu/skills/<skill-name>.md ~/.niu/skills/.trash/<skill-name>.$(date +%Y%m%d%H%M%S).md && echo "已移动到 .trash/"
     ```
     说明：
     - `mkdir -p` 确保 .trash 目录存在
     - `mv` 直接移动文件，失败时 bash 会返回非零退出码，dream-evolver 在报告中说明失败原因
     - 文件名用 `$(date +%Y%m%d%H%M%S)` 精确到秒，避免同一天多次删除同名 skill 冲突
     - 不要用 `if/then/else/fi` 嵌套在 `&&` 链中（bash 语法不允许）
  3. **LightRAG 实体清理**：文件移动后，由 SkillSync 清理 LightRAG 实体。注意 watchdog 的 `on_deleted` 在 macOS 上对 `mv` 到子目录**不触发**（产生 FileMovedEvent 而非 FileDeletedEvent），所以清理依赖 `scan_and_sync` 的60秒定时扫描（检测到磁盘文件消失后调 `_delete_skill_from_lightrag`）。最长延迟约60秒，期间主Agent可能仍检索到该 skill 的残留实体——这是可接受的（文件已不在，主Agent即使检索到也读取不到内容）。
  4. 在回复报告中记录删除操作

  **安全约束**：
  - 只能删除 `status: deprecated` 的 skill，禁止删除 active/draft 状态的 skill
  - 移动而非 `rm`，保留备份在 `.trash/` 目录
  - 文件名加日期时间戳后缀（`.YYYYMMDDHHMMSS.md`，精确到秒），避免同名 skill 多次删除时冲突
  - 如果 `mv` 失败（如权限问题），不要重试，在报告中说明失败原因

  ## Skill 文件规范（知识储备）
  ```

#### 3.5 更新工具列表（405-409行）

- [ ] 3.5.1 搜索定位：
  ```bash
  grep -n 'edit(file_path, old_string, new_string)' config/agents/dream-evolver.md
  ```
  确认在约 407 行。
- [ ] 3.5.2 用 Edit 在工具列表中补充 bash 工具说明。`old_string`:
  ```markdown
  其他工具：
  - `get_messages(session_id)` — session_id 传 `"default"`（但消息已在 prompt 中提供，通常不需要调用）
  - `edit(file_path, old_string, new_string)` — Skill 修改
  - `write(file_path, content)` — Skill 创建
  - `read(file_path)` — Skill 读取
  ```
  `new_string`:
  ```markdown
  其他工具：
  - `get_messages(session_id)` — session_id 传 `"default"`（但消息已在 prompt 中提供，通常不需要调用）
  - `edit(file_path, old_string, new_string)` — Skill 修改（含 frontmatter status/issue_count 字段修改）
  - `write(file_path, content)` — Skill 创建
  - `read(file_path)` — Skill 读取
  - `bash(command)` — 执行 shell 命令，仅用于步骤 C5 删除 skill（mv 到 .trash/）
  ```

#### 3.6 更新回复格式（437-449行）

- [ ] 3.6.1 搜索定位：
  ```bash
  grep -n '草稿转正:' config/agents/dream-evolver.md
  ```
  确认在约 445 行。
- [ ] 3.6.2 用 Edit 替换报告格式块。`old_string`:
  ```markdown
  [梦境进化报告]
  处理范围：消息 idx {start_idx} ~ {end_idx}（共 {count} 条）
  实体精加工：{n} 个实体
    - 描述优化：{n1} 个
    - 时间链创建：{n2} 条关系
    - 脑区关联：{n3} 条关系
    - 脑区归入：{n4} 条关系
  Skill 操作：{n5} 个（新建: {n6}, 修改正文: {n7}, 添加提醒: {n8}, 草稿转正: {n9}）
    - 如果阶段C未执行（无信号），报告：Skill 操作：无信号，跳过
  ```
  `new_string`:
  ```markdown
  [梦境进化报告]
  处理范围：消息 idx {start_idx} ~ {end_idx}（共 {count} 条）
  实体精加工：{n} 个实体
    - 描述优化：{n1} 个
    - 时间链创建：{n2} 条关系
    - 脑区关联：{n3} 条关系
    - 脑区归入：{n4} 条关系
  Skill 操作：{n5} 个（新建: {n6}, 修改正文: {n7}, 添加提醒: {n8}, 草稿转正: {n9}, 降级: {n10}, 复活: {n11}, 删除: {n12}）
    - 如果阶段C未执行（无信号），报告：Skill 操作：无信号，跳过
    - 删除操作需列出 skill 名和 .trash/ 中的目标路径
  ```

### 验证

- [ ] 3.7 检查 markdown 格式完整性（表格列数对齐）：
  ```bash
  # 表格行应以 | 开头和结尾
  grep -n '^|' config/agents/dream-evolver.md | head -40
  ```
- [ ] 3.8 检查 frontmatter 块 yaml 格式（可选，如有 yamllint）：
  ```bash
  # 提取 frontmatter 块测试
  python -c "
  import yaml
  text = open('config/agents/dream-evolver.md').read()
  # 测试 frontmatter 规范示例
  sample = '''name: skill-name-with-hyphens
  description: Use when test
  status: deprecated
  issue_count: 3
  created: 2026-06-29
  last_tested: 2026-06-29'''
  print(yaml.safe_load(sample))
  "
  ```
- [ ] 3.9 dream-evolver 实际运行测试：触发一条会让 dream-evolver 处理 skill 反馈的对话，观察日志：
  ```bash
  # 等待 dream-evolver 执行后检查日志
  tail -f logs/api_stderr.log | grep -i "dream\|skill\|deprecated\|issue_count"
  ```

### 提交

- [ ] 3.10 临时提交：
  ```bash
  git add config/agents/dream-evolver.md
  git commit -m "feat(skill): dream-evolver three-tier deprecation logic — backup before Task 4

  Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
  ```

---

## Task 4: niu.md 更新反馈要求

**目标**：主 Agent 提示词增加对待观察 skill 的强制反馈要求，并明确软约束。

**Files**:
- `config/agents/niu.md`

### 步骤

- [ ] 4.1 搜索定位：
  ```bash
  grep -n '草稿 skill' config/agents/niu.md
  ```
  确认在约 143 行。
- [ ] 4.2 读取上下文（141-145行）确认精确文本：
  ```bash
  sed -n '141,145p' config/agents/niu.md
  ```
- [ ] 4.3 用 Edit 替换反馈要求块。`old_string`:
  ```markdown
  ### Skill 使用反馈

  - **草稿 skill**：使用后**必须**明确说明效果——草稿 skill 尚未验证，你的反馈决定它能否转正。例如："根据 [skill-name] 草稿的指导，成功完成了..."或"按照 [skill-name] 草稿的步骤操作，但在...处遇到问题"
  - **成熟 skill**：当使用已有的skill出现错误，或者发现skill中所说的内容完全没有必要时，要通知用户需要修改、或者需要删除。主要目的并不是让用户去做相应的操作，而是在子Agent进行梦境进化时，会分析你的建议。
  ```
  `new_string`:
  ```markdown
  ### Skill 使用反馈

  - **草稿 skill**（`[草稿]` 前缀）：使用后**必须**明确说明效果——草稿 skill 尚未验证，你的反馈决定它能否转正。例如："根据 [skill-name] 草稿的指导，成功完成了..."或"按照 [skill-name] 草稿的步骤操作，但在...处遇到问题"
  - **待观察 skill**（`[待观察]` 前缀）：使用后**必须**明确反馈成功或失败——此 skill 有历史问题，正处于观察态。你的反馈决定它复活还是删除。反馈格式：
    - 成功："待观察 skill [skill-name] 本次使用成功，解决了..."
    - 失败："待观察 skill [skill-name] 本次使用失败，问题是..."
  - **成熟 skill**（无前缀）：如果发现问题或发现 skill 内容完全没有必要，请明确反馈——失败反馈会累计 issue_count，3 次失败后降级为待观察；成功反馈会衰减 issue_count。反馈格式："skill [skill-name] 在...场景下失效，原因是..."
  - **软约束**：以上反馈要求是软约束。如果发现问题请反馈；若确实无法判断效果，可省略反馈，但待观察 skill 强烈建议反馈。
  ```

### 验证

- [ ] 4.4 启动 API 服务，检查 system prompt 中反馈要求段落是否正确渲染：
  ```bash
  grep -r "待观察 skill.*前缀.*必须" logs/ | tail -5
  ```
- [ ] 4.5 发送一条对话，确认主 Agent 能看到三种 skill 的反馈要求。

### 提交

- [ ] 4.6 临时提交：
  ```bash
  git add config/agents/niu.md
  git commit -m "feat(skill): niu.md three-tier feedback requirements — backup before Task 5

  Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
  ```

---

## Task 5: 端到端测试

**目标**：用真实 LLM + 真实 LightRAG 验证完整降级链路：active → 3次失败 → deprecated → 1次失败 → 删除；以及 deprecated → 1次成功 → 复活。

**Files**: 无代码改动，仅测试。

### 前置准备

- [ ] 5.1 清空测试环境（遵守 [Real Testing Only] 规则）：
  ```bash
  # 备份现有 skills
  cp -r ~/.niu/skills ~/.niu/skills.backup.$(date +%Y%m%d)
  # 清空 skills 目录（保留 .trash 子目录如有）
  rm -f ~/.niu/skills/*.md
  ```
- [ ] 5.2 杀掉所有 niu 进程（遵守 [Kill Processes After Test] 规则）：
  ```bash
  pkill -f "niu_api" 2>/dev/null
  pkill -f "python.*niu" 2>/dev/null
  sleep 2
  ps aux | grep -i niu | grep -v grep
  ```

### 测试场景 A：降级链路（active → deprecated → 删除）

- [ ] 5.3 创建一个 active skill（issue_count=0）：
  ```bash
  cat > ~/.niu/skills/_test_e2e.md <<'EOF'
  ---
  name: _test_e2e
  description: Use when testing skill deprecation end-to-end
  status: active
  issue_count: 0
  created: 2026-06-29
  last_tested: 2026-06-29
  ---
  # Test E2E

  ## Overview
  测试用 skill，验证降级链路。

  ## When to Use
  - 测试降级机制时使用

  ## Steps
  1. 执行测试操作

  <!-- 执行提醒 -->
  EOF
  ```
- [ ] 5.4 启动程序：`./niu`（在另一个终端或后台）
- [ ] 5.5 等 SkillSync 扫描注入完成（约 60 秒），确认 LightRAG 中有该实体：
  ```bash
  python -c "
  from niu_api.internal.lightrag_adapter import LightRAGAdapter
  adapter = LightRAGAdapter()
  result = adapter.search_entities(query='_test_e2e', top_k=5, keywords=['_test_e2e'])
  print([e.get('entity_name') for e in result.get('data', [])])
  "
  ```
  预期：包含 `_test_e2e`
- [ ] 5.6 模拟主 Agent 反馈 3 次失败（**必须用真实 LLM 路径**，不能用手动编辑 frontmatter 绕过——绕过 dream-evolver 无法验证提示词逻辑）：
  - 发送 3 条对话消息让主 Agent 使用该 skill 并反馈失败（如"按照 _test_e2e 的步骤操作，但在...处遇到问题"）
  - 每次反馈后等待 dream-evolver 增量处理（通过游标推进），确认 frontmatter 的 issue_count 递增
  - 如果 dream-evolver 未触发或未识别反馈，检查 dream-evolver 是否运行（看日志），不要用手动编辑替代
- [ ] 5.7 第 3 次失败后，确认 frontmatter：
  ```bash
  grep -E 'status:|issue_count:' ~/.niu/skills/_test_e2e.md
  ```
  预期：`status: deprecated`，`issue_count: 3`
- [ ] 5.8 等 SkillSync 重注入，确认 LightRAG 中 description 有 `[待观察]` 前缀：
  ```bash
  python -c "
  from niu_api.internal.lightrag_adapter import LightRAGAdapter
  adapter = LightRAGAdapter()
  result = adapter.search_entities(query='_test_e2e', top_k=5, keywords=['_test_e2e'])
  for e in result.get('data', []):
      if e.get('entity_name') == '_test_e2e':
          print('description:', e.get('description'))
  "
  ```
  预期：description 以 `[待观察]` 开头
- [ ] 5.9 触发第 4 次失败反馈（deprecated 状态下），等待 dream-evolver 执行删除：
  ```bash
  # 检查 .trash 目录
  ls -la ~/.niu/skills/.trash/ 2>/dev/null
  # 检查原文件是否已移走
  ls ~/.niu/skills/_test_e2e.md 2>/dev/null && echo "文件仍在" || echo "文件已移走"
  ```
  预期：原文件不存在，`.trash/_test_e2e.YYYYMMDDHHMMSS.md` 存在
- [ ] 5.10 等 SkillSync 定时扫描检测删除（最长约 60 秒，因 macOS 上 mv 到子目录不触发 on_deleted，依赖 scan_and_sync 兜底），确认 LightRAG 实体已删除：
  ```bash
  python -c "
  from niu_api.internal.lightrag_adapter import LightRAGAdapter
  adapter = LightRAGAdapter()
  result = adapter.search_entities(query='_test_e2e', top_k=5, keywords=['_test_e2e'])
  print([e.get('entity_name') for e in result.get('data', [])])
  "
  ```
  预期：返回空列表或不含 `_test_e2e`

### 测试场景 B：复活链路（deprecated → active）

- [ ] 5.11 创建一个 deprecated skill（issue_count=3）：
  ```bash
  cat > ~/.niu/skills/_test_revive.md <<'EOF'
  ---
  name: _test_revive
  description: Use when testing skill revival
  status: deprecated
  issue_count: 3
  created: 2026-06-29
  last_tested: 2026-06-29
  ---
  # Test Revive
  ## Overview
  测试复活链路。
  <!-- 执行提醒 -->
  EOF
  ```
- [ ] 5.12 等 SkillSync 注入，确认 `[待观察]` 前缀（同 5.8 验证方式）
- [ ] 5.13 触发 1 次成功反馈，等待 dream-evolver 执行复活：
  ```bash
  grep -E 'status:|issue_count:' ~/.niu/skills/_test_revive.md
  ```
  预期：`status: active`，`issue_count: 0`
- [ ] 5.14 等 SkillSync 重注入，确认 LightRAG 中 description 不再有 `[待观察]` 前缀

### 测试场景 C：active 成功衰减

- [ ] 5.15 创建一个 active skill，issue_count=2：
  ```bash
  cat > ~/.niu/skills/_test_decay.md <<'EOF'
  ---
  name: _test_decay
  description: Use when testing issue_count decay
  status: active
  issue_count: 2
  created: 2026-06-29
  last_tested: 2026-06-29
  ---
  # Test Decay
  ## Overview
  测试衰减。
  <!-- 执行提醒 -->
  EOF
  ```
- [ ] 5.16 触发 1 次成功反馈，等待 dream-evolver 执行衰减：
  ```bash
  grep 'issue_count:' ~/.niu/skills/_test_decay.md
  ```
  预期：`issue_count: 1`
- [ ] 5.17 再触发 3 次成功反馈，确认 issue_count 不会变成负数：
  ```bash
  grep 'issue_count:' ~/.niu/skills/_test_decay.md
  ```
  预期：`issue_count: 0`（最低 0，不会负数）

### 清理

- [ ] 5.18 删除所有测试 skill：
  ```bash
  rm -f ~/.niu/skills/_test_*.md
  rm -rf ~/.niu/skills/.trash/_test_*
  ```
- [ ] 5.19 恢复原 skills（如果有备份）：
  ```bash
  # 仅当 5.1 做了备份时执行
  cp ~/.niu/skills.backup.*/[a-z]*.md ~/.niu/skills/ 2>/dev/null
  rm -rf ~/.niu/skills.backup.*
  ```
- [ ] 5.20 杀掉所有 niu 进程：
  ```bash
  pkill -f "niu_api" 2>/dev/null
  pkill -f "python.*niu" 2>/dev/null
  sleep 2
  ps aux | grep -i niu | grep -v grep
  ```

### 提交

- [ ] 5.21 如果测试中发现 bug 并修复，提交修复：
  ```bash
  git add -A
  git commit -m "test(skill): e2e validation of three-tier deprecation mechanism

  Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
  ```

---

## 自审检查

### Spec 覆盖检查

逐条核对设计决策是否在计划中实现：

- [x] `status: active` + `issue_count: N` — Task 3.1.3 frontmatter 字段说明
- [x] active 下累计 3 次失败反馈 → 降级到 deprecated — Task 3.2.2 降级机制速查表 + Task 3.3.2 失败路径
- [x] `status: deprecated` — 观察态，仍注入主 Agent 但加 `[待观察]` 前缀 — Task 1.3 sync.py 前缀 + Task 2.3 runner.py 提醒
- [x] deprecated 下 1 次失败反馈 → dream-evolver 用 bash 移到 `.trash/` + 删 LightRAG 实体 — Task 3.4.2 步骤 C5
- [x] deprecated 下 1 次成功反馈 → 复活到 active，issue_count 清零 — Task 3.3.2 成功路径 + Task 3.2.2 速查表
- [x] active 下成功使用 → issue_count 减 1（衰减，最低到 0） — Task 3.3.2 成功路径 + Task 3.1.3 字段说明
- [x] frontmatter 加 `issue_count` 字段 — Task 3.1.2 + 3.1.3
- [x] 接受每次计数变化触发 LightRAG 重注入的副作用 — 隐式由 SkillSync watchdog 实现，Task 5 验证
- [x] 软约束（保留现有"如果发现问题请反馈"） — Task 4.3 软约束段
- [x] 待观察 skill（deprecated）使用后必须反馈成功/失败 — Task 4.3 待观察 skill 反馈格式
- [x] dream-evolver 有 bash 工具，自己执行 `mv` — Task 3.4.2 bash 命令 + Task 3.5.2 工具列表
- [x] 移动后 SkillSync 下次扫描时检测到文件消失，自动调 `_delete_skill_from_lightrag` 清理 LightRAG — Task 3.4.2 说明 + Task 5.10 验证

### Placeholder 扫描

检查计划中是否有占位符未替换：

- [x] 所有代码块均为完整代码，无 `TODO`、`FIXME`、`...` 占位
- [x] bash 命令中的 `SKILL_NAME` 是变量名（dream-evolver 执行时替换为实际 skill 名），非占位符
- [x] 所有行号引用都附带了 grep 搜索命令，可重新定位（防止行号漂移）
- [x] 验证步骤中的预期输出都是具体值

### 类型一致性检查

- [x] `status` 字段三态：`draft | active | deprecated`（Task 1、Task 3.1.2 一致）
- [x] `issue_count` 字段类型：整数（Task 3.1.3、Task 3.3.2 一致）
- [x] 前缀字符串：`[草稿]` 和 `[待观察]`（Task 1.3、Task 2.3 一致）
- [x] 降级阈值：`>=3`（Task 3.1.3 字段说明、Task 3.2.2 速查表、Task 3.3.2 失败路径一致）
- [x] 衰减下限：`0`（Task 3.1.3、Task 3.3.2 一致）
- [x] .trash 目录路径：`~/.niu/skills/.trash/`（Task 3.4.2、Task 5.9 一致）
- [x] 文件名格式：`{name}.{YYYYMMDDHHMMSS}.md`（Task 3.4.2 bash 命令一致）

### 风险评估

| 风险 | 等级 | 缓解 |
|------|------|------|
| watchdog 未检测到文件移动 | 中 | SkillSync 有定时扫描 fallback（60秒），Task 5.10 验证 |
| dream-evolver 误删 active skill | 高 | Task 3.4.2 安全约束：只删 deprecated，先 read 确认 status |
| issue_count 字段缺失导致解析失败 | 低 | Task 3.1.3 字段说明：新建默认 0；Task 3.3.2 草稿转正时确保 issue_count:0 存在 |
| LightRAG 重注入副作用（每次计数变化都重注入） | 低 | 设计已确认接受此副作用 |
| bash 命令注入风险 | 中 | SKILL_NAME 来自 skill 文件名（已受命名规范约束：只含字母数字连字符） |

### 依赖顺序

```
Task 1 (sync.py 前缀) ──┐
                         ├──► Task 5 (e2e 测试)
Task 2 (runner.py 提醒) ─┤
                         │
Task 3 (dream-evolver) ──┤
                         │
Task 4 (niu.md 反馈) ────┘
```

Task 1-4 之间无强依赖（可并行），但 Task 5 依赖全部完成。建议执行顺序：1 → 2 → 3 → 4 → 5。
