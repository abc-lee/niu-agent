# GenericAgent 自我进化机制深度分析

> 分析日期：2026-04-06
> 目标：评估 `update_working_checkpoint` 和 `start_long_term_update` 是否为核心能力

---

## 一、GenericAgent 自我进化机制分析

### 1.1 记忆分层架构

GenericAgent 采用 L0/L1/L2/L3 四层记忆体系：

```
L0: META-SOP (memory_management_sop.md)
    └─ 记忆管理规则（核心公理 + 决策树）

L1: global_mem_insight.txt (极简索引层，≤30行)
    └─ 场景关键词 → 记忆定位
    └─ RULES（红线规则 + 高频犯错点）

L2: global_mem.txt (事实库层)
    └─ 环境事实（路径/凭证/配置）
    └─ 按 ## [SECTION] 组织

L3: memory/*.md (任务级SOP)
    └─ 特定任务的关键前置 + 典型坑
    └─ 工具脚本（*.py）
```

**核心公理**：
1. **行动验证原则**：No Execution, No Memory（无行动，不记忆）
2. **神圣不可删改性**：验证过的数据严禁丢弃
3. **禁止存储易变状态**：不存时间戳、PID、临时变量
4. **最小充分指针**：上层只留定位下层的最短标识

### 1.2 两个工具的作用

#### `update_working_checkpoint`

```python
def do_update_working_checkpoint(self, args, response):
    """为整个任务设定后续需要临时记忆的重点。"""
    key_info = args.get("key_info", "")
    related_sop = args.get("related_sop", "")
    if "key_info" in args:
        self.working["key_info"] = key_info
    if "related_sop" in args:
        self.working["related_sop"] = related_sop
    self.working["passed_sessions"] = 0  # 重置计数器
    return StepOutcome({"status": "success"}, next_prompt=...)
```

**作用**：
- 更新 `self.working` 字典（任务级工作记忆）
- 设置 `key_info`：关键信息（如"用户偏好简洁回答"）
- 设置 `related_sop`：相关SOP文件路径
- 重置 `passed_sessions` 计数器（用于周期性警告）

**注入点**：
```python
def _get_anchor_prompt(self, skip=False):
    prompt = f"\n### [WORKING MEMORY]\n<history>\n{h_str}\n</history>"
    prompt += f"\nCurrent turn: {self.current_turn}\n"
    if self.working.get("key_info"):
        prompt += f"\n<key_info>{self.working.get('key_info')}</key_info>"
    if self.working.get("related_sop"):
        prompt += f"\n有不清晰的地方请再次读取{self.working.get('related_sop')}"
    return prompt
```

**效果**：每轮都会注入 `key_info` 和 `related_sop`，避免 LLM 遗忘关键上下文。

#### `start_long_term_update`

```python
def do_start_long_term_update(self, args, response):
    """Agent觉得当前任务完成后有重要信息需要记忆时调用此工具。"""
    prompt = """### [总结提炼经验] 既然你觉得当前任务有重要信息需要记忆，
请提取最近一次任务中【事实验证成功且长期有效】的环境事实、用户偏好、重要步骤，更新记忆。
本工具是标记开启结算过程，若已在更新记忆过程或没有值得记忆的点，忽略本次调用。

**提取行动验证成功的信息**：
- **环境事实**（路径/凭证/配置）→ `file_patch` 更新 L2，同步 L1
- **复杂任务经验**（关键坑点/前置条件/重要步骤）→ L3 精简 SOP（只记你被坑得多次重试的核心要点）
**禁止**：临时变量、具体推理过程、未验证信息、通用常识、你可以轻松复现的细节。
**操作**：严格遵循提供的L0的记忆更新SOP。先 `file_read` 看现有 → 判断类型 → 最小化更新 → 无新内容跳过，保证对记忆库最小局部修改。
"""
    return StepOutcome(result, next_prompt=prompt + get_global_memory())
```

**作用**：
- 触发长期记忆提炼流程
- 返回提示词 + 全局记忆（L1索引）
- 引导 LLM：
  1. 读取 META-SOP（L0规则）
  2. 读取现有记忆（L1/L2/L3）
  3. 判断信息类型（环境事实/任务经验）
  4. 最小化更新（file_patch）
  5. 同步 L1 索引

**效果**：LLM 主动调用此工具后，会进入"记忆提炼模式"，读取 SOP 规则，判断哪些信息值得记忆，然后写入文件。

### 1.3 自我进化闭环

```
┌─────────────────────────────────────────────────────────────────┐
│                        任务执行                                  │
│  用户："帮我处理这 100 个文档"                                   │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│              update_working_checkpoint (可选)                    │
│  LLM 发现关键信息："这些文档都是 PDF，需要特殊解析器"           │
│  设置 key_info = "文档格式：PDF，需要 pdfplumber"               │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                      任务执行中...                               │
│  _get_anchor_prompt 每轮注入 key_info                           │
│  LLM 记得使用 pdfplumber，不会遗忘                              │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│               start_long_term_update (任务完成后)                │
│  LLM 判断："PDF 解析经验值得记忆"                               │
│  调用工具 → 进入记忆提炼模式                                    │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                      记忆提炼流程                                │
│  1. 读取 memory_management_sop.md (L0)                          │
│  2. 读取 global_mem_insight.txt (L1)                            │
│  3. 读取 global_mem.txt (L2)                                    │
│  4. 判断：这是"环境事实"还是"任务经验"？                        │
│  5. 写入：file_patch 更新 L2 或创建 L3 SOP                      │
│  6. 同步：更新 L1 索引                                          │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                       下次任务                                  │
│  用户："帮我处理这 200 个文档"                                  │
│  get_global_memory() 注入 L1 索引                               │
│  LLM 看到："PDF处理 → memory/pdf_sop.md"                        │
│  读取 SOP → 直接使用最佳实践                                    │
└─────────────────────────────────────────────────────────────────┘
```

**关键点**：
- **显式工具调用**：LLM 主动决定何时更新工作记忆、何时提炼长期记忆
- **SOP 指导**：有明确的规则（L0）指导如何更新记忆
- **闭环**：任务执行 → 工作记忆 → 长期记忆 → 下次注入

---

## 二、NiuHandler 能力对比

### 2.1 已有能力

| 能力 | 实现方式 | 作用 |
|------|---------|------|
| 短期工作记忆 | `tool_after_callback` + `_get_anchor_prompt` | 记录最近 10 条工具调用摘要 |
| 周期性警告 | `next_prompt_patcher` | 每 7 轮警告，每 35 轮强制 ask_user |
| 全局记忆注入 | `next_prompt_patcher` 调用 `get_global_memory()` | 每 10 轮注入（但文件缺失） |
| 用户偏好存储 | config-manager MCP | `add_user_preference` 工具 |
| 记忆存储 | memory-server MCP | `store_memory` 工具（但未集成到 handler） |

### 2.2 缺失能力

| 能力 | GenericAgent | NiuHandler | 影响 |
|------|--------------|------------|------|
| 显式更新工作记忆 | `update_working_checkpoint` | ❌ 无 | LLM 无法主动设置 key_info |
| 长期记忆提炼触发 | `start_long_term_update` | ❌ 无 | LLM 无法主动保存经验 |
| L0/L1/L2/L3 文件体系 | memory/*.md | ❌ 无 | 无 SOP 指导记忆更新 |
| 环境事实记忆 | global_mem.txt | ❌ 无向量库对应 | 硬件配置、路径等易遗忘 |
| 任务经验 SOP | memory/*_sop.md | ❌ 无向量库对应 | 复杂任务无法积累经验 |

### 2.3 关键差异

#### GenericAgent 的记忆注入

```python
# 每 10 轮注入全局记忆
if turn % 10 == 0:
    next_prompt += get_global_memory()

# get_global_memory() 返回：
Facts(L2): ../memory/global_mem.txt | Code: ../ | SOPs(L3): ../memory/*.md
Insight是极简索引，L2/L3变更时同步Insight，索引必须极简。

[CONSTITUTION]
1. 改自身源码先请示；./内可自主实验
2. 决策前查记忆库；未查证不断言
3. 分步执行，控制粒度，限制失败半径
...
```

**效果**：LLM 定期看到记忆库结构，知道有哪些 L2/L3 可用。

#### NiuHandler 的记忆注入

```python
# 每 10 轮注入全局记忆
if turn % 10 == 0 and turn > 0:
    from .generic.handler import get_global_memory
    global_mem = get_global_memory()
    if global_mem:
        next_prompt += f"\n\n### [GLOBAL MEMORY]\n{global_mem}"
```

**问题**：
1. 调用 `generic.handler.get_global_memory()`，读取的是 GenericAgent 目录下的文件
2. Niu 项目没有对应的 `global_mem_insight.txt` 和 `global_mem.txt`
3. 返回空字符串或占位符，无实际作用

#### 向量库替代方案

Niu 项目的向量库有：
- Skills（L1）
- MCP 工具描述（L1）
- 知识文档（L1/L2）

**问题**：
- 缺少 L0（META-SOP）
- 缺少"环境事实"分类（如硬件配置、路径）
- 缺少"任务经验"SOP 分类
- 缺少统一的"新记忆注入"流程

---

## 三、具体场景分析

### 场景1：Agent 发现用户偏好

**用户**："我比较喜欢简洁的回答"

#### GenericAgent 处理

```
1. LLM 调用 update_working_checkpoint({
     "key_info": "用户偏好：简洁回答"
   })
2. self.working["key_info"] = "用户偏好：简洁回答"
3. 后续每轮 _get_anchor_prompt 注入：<key_info>用户偏好：简洁回答</key_info>
4. 任务完成后，LLM 调用 start_long_term_update()
5. 进入记忆提炼模式：
   - 读取 META-SOP：判断这是"用户偏好"
   - 读取 global_mem.txt：查看是否已记录
   - file_patch 更新 L2：添加用户偏好
   - 同步 L1：在索引中添加"用户偏好→L2#user"
6. 下次对话：get_global_memory() 返回索引，LLM 看到用户偏好
```

#### NiuHandler 处理

```
1. LLM 想更新工作记忆，但缺少 update_working_checkpoint 工具
2. 只能通过 tool_after_callback 自动记录摘要（但这是工具调用摘要，不是偏好）
3. LLM 可以调用 config-manager 的 add_user_preference()
4. 写入 ~/.niu/memory.json 的 user.preferences 数组
5. 下次对话：
   - config-manager 的 get_full_memory() 可以读取
   - 但没有统一的注入机制（没有每轮注入 memory.json）
```

**对比**：
- GenericAgent：显式工具 → 工作记忆 → 长期记忆 → 自动注入
- NiuHandler：缺少工作记忆工具 → 可以保存到 memory.json → 但不会自动注入

### 场景2：Agent 学到环境事实

**Agent 探测**：总内存 63.8GB，GPU RTX 4090 + CUDA

#### GenericAgent 处理

```
1. LLM 调用 update_working_checkpoint({
     "key_info": "硬件：63.8GB 内存，RTX 4090，CUDA 可用"
   })
2. 后续每轮注入硬件信息，避免重复探测
3. 任务完成后，LLM 调用 start_long_term_update()
4. 判断：这是"环境事实"，写入 L2 global_mem.txt
   [HARDWARE]
   - Total Memory: 63.8GB
   - GPU: RTX 4090
   - CUDA: Available
5. 同步 L1：在索引中添加"硬件→L2#hardware"
```

#### NiuHandler 处理

```
1. LLM 想更新工作记忆，但缺少工具
2. 没有统一的"环境事实"存储位置：
   - memory.json 只有 identity/workspace/user
   - 向量库的 document 表是知识文档，不是配置
   - memory-server 的 store_memory 可以存，但属于"交互记忆"
3. 下次对话：需要重新探测
```

**对比**：
- GenericAgent：有专门的 L2 环境事实层，自动注入
- NiuHandler：无专门存储位置，无法自动注入

### 场景3：长任务中的关键信息

**任务**："帮我整理这 100 个文档"

**执行 50 轮后发现**：某些文件格式需要特殊处理

#### GenericAgent 处理

```
1. LLM 发现规律，调用 update_working_checkpoint({
     "key_info": "文档格式：部分是 PDF，需要 pdfplumber",
     "related_sop": "memory/pdf_processing_sop.md"
   })
2. self.working["passed_sessions"] = 0  # 重置计数器
3. 后续每轮注入 key_info + related_sop
4. LLM 记得使用 pdfplumber，读取 SOP
5. 避免 50 轮后遗忘（LLM 上下文窗口限制）
```

#### NiuHandler 处理

```
1. LLM 想更新工作记忆，但缺少工具
2. tool_after_callback 只记录工具调用摘要
3. history_info 只保留最近 10 条
4. 如果任务超过 50 轮，早期的发现会被挤出历史记录
5. LLM 可能遗忘，重新踩坑
```

**对比**：
- GenericAgent：显式设置 key_info，永久保留（直到任务结束）
- NiuHandler：自动记录摘要，有限长度（最近 10 条）

---

## 四、能力对比矩阵

| 能力维度 | GenericAgent | NiuHandler | 状态 |
|---------|--------------|------------|------|
| **短期工作记忆** |
| 记录方式 | 显式工具 `update_working_checkpoint` | 自动记录 `tool_after_callback` | ⚠️ 部分缺失 |
| 保留时长 | 任务级（直到任务结束） | 最近 10 条工具调用 | ⚠️ 较弱 |
| 主动控制 | LLM 可主动设置 key_info | 无法主动设置 | ❌ 缺失 |
| **长期记忆保存** |
| 触发机制 | 显式工具 `start_long_term_update` | ❌ 无工具 | ❌ 完全缺失 |
| SOP 指导 | L0 META-SOP 规则 | ❌ 无向量库对应 | ❌ 缺失 |
| 记忆分类 | L1 索引 / L2 事实 / L3 SOP | 向量库无分类 | ⚠️ 架构不同 |
| **记忆注入** |
| 全局记忆注入 | 每 10 轮注入 L1 索引 | 调用但文件缺失 | ❌ 无效 |
| 工作记忆注入 | 每轮注入 key_info | 每轮注入历史摘要 | ✅ 已有 |
| 动态资源注入 | ❌ 无向量库 | ✅ Skills/MCP Tools/知识 | ✅ 更优 |
| **记忆内容** |
| 用户偏好 | L2 global_mem.txt | memory.json | ✅ 有替代 |
| 环境事实 | L2 global_mem.txt | ❌ 无专门位置 | ❌ 缺失 |
| 任务经验 SOP | L3 memory/*.md | ❌ 无向量库对应 | ❌ 缺失 |
| 技能 SOP | L3 memory/skills/*.md | 向量库 Skills (L1) | ✅ 有替代 |
| **自我进化** |
| 闭环完整性 | 执行 → 工作记忆 → 长期记忆 → 注入 | 执行 → 自动摘要 → （断裂） | ❌ 不完整 |
| 经验积累 | 可以积累任务经验 SOP | 无法积累任务经验 | ❌ 缺失 |
| 知识沉淀 | 文件系统 + 手动注入 | 向量库自动注入 | ⚠️ 架构不同 |

**符号说明**：
- ✅ 已有：能力完整
- ⚠️ 部分缺失：能力部分实现
- ❌ 缺失：能力完全缺失
- ⚠️ 架构不同：实现方式不同，但功能可替代

---

## 五、移除影响评估

### 5.1 移除 `update_working_checkpoint`

**丧失能力**：
1. ❌ LLM 无法主动设置 `key_info`
2. ❌ LLM 无法主动设置 `related_sop`
3. ⚠️ 工作记忆完全依赖自动记录（最近 10 条）

**影响程度**：**中等**

**替代方案**：
- `tool_after_callback` 已经记录工具调用摘要
- 可以调整 `history_info` 的长度（从 10 条增加到 20 条）
- 可以在提示词中强调："关键信息请使用 ask_user 确认"

**是否核心**：**否**（有替代方案）

### 5.2 移除 `start_long_term_update`

**丧失能力**：
1. ❌ LLM 无法主动触发长期记忆提炼
2. ❌ 无法将任务经验写入向量库
3. ❌ 无法创建 SOP 文件
4. ❌ 自我进化闭环断裂

**影响程度**：**严重**

**替代方案**：
- 目前无替代方案
- memory-server 有 `store_memory` 工具，但：
  - LLM 不会主动调用（缺少提示词引导）
  - 属于"交互记忆"，不是"任务经验 SOP"
  - 没有统一的 L0/L1/L2 分层

**是否核心**：**是**（无替代方案，核心能力丧失）

### 5.3 综合评估

**如果移除这两个工具**：

| 场景 | GenericAgent | NiuHandler（移除后） | 后果 |
|------|--------------|---------------------|------|
| 用户偏好记忆 | 自动注入到每轮 | 需手动调用 MCP | 体验退化 |
| 环境事实记忆 | 自动注入到每轮 | 无存储位置 | 每次重新探测 |
| 任务经验积累 | 自动写入 SOP | 无法积累 | 无法进化 |
| 长任务上下文 | 显式保留关键信息 | 自动记录有限历史 | 可能遗忘 |

**核心问题**：
1. **自我进化能力丧失**：无法从任务中学习，无法积累经验
2. **工作记忆能力退化**：从"显式控制"降级为"自动记录"
3. **记忆注入机制断裂**：有存储能力（memory-server），但缺少统一的注入流程

---

## 六、建议方案

### 方案对比

#### 方案A：移除并补全缺失能力

**操作**：
1. 移除 `update_working_checkpoint`（用 `tool_after_callback` 替代）
2. 保留 `start_long_term_update`，但改为调用 MCP

**补全内容**：
1. 向量库添加 L0/L1/L2 分层：
   - L0：META-SOP（记忆管理规则）
   - L1：索引层（场景关键词 → 向量库 ID）
   - L2：事实层（环境事实、用户偏好）

2. 创建向量库注入工具：
   ```python
   # 新建 MCP 工具：vector-store/inject_memory
   def inject_memory(content, memory_type, metadata):
       """
       保存新记忆到向量库

       memory_type:
       - "environment_fact"：环境事实（硬件、路径）
       - "user_preference"：用户偏好
       - "task_experience"：任务经验 SOP
       """
       doc_id = f"memory:{memory_type}:{uuid.uuid4()}"
       vector_db.upsert(doc_id, content, metadata={
           "level": "l2",
           "category": "memory",
           "type": memory_type,
           ...
       })
   ```

3. 修改 `start_long_term_update`：
   ```python
   def do_start_long_term_update(self, args, response):
       # 不再返回文件操作提示词
       # 改为调用 MCP 工具：
       # 1. 调用 config-manager/add_user_preference（用户偏好）
       # 2. 调用 vector-store/inject_memory（环境事实）
       # 3. 调用 vector-store/add_document（任务经验）
       ...
   ```

4. 统一记忆注入流程：
   ```python
   def _inject_dynamic_resources(self, user_input):
       # 搜索 Skills
       # 搜索 MCP 工具
       # 搜索知识文档
       # 搜索记忆（新增）
       memories = self.vector_search.search(
           query=user_input, limit=3,
           filter={"level": "l2", "category": "memory"}
       )
       ...
   ```

**优点**：
- ✅ 保留自我进化能力
- ✅ 统一使用向量库（架构一致性）
- ✅ 不依赖文件系统

**缺点**：
- ⚠️ 需要修改向量库 Schema
- ⚠️ 需要创建新的 MCP 工具
- ⚠️ 需要修改提示词模板

**工作量**：中等（约 2 天）

---

#### 方案B：保留并补全实现

**操作**：
1. 保留 `update_working_checkpoint` 和 `start_long_term_update`
2. 创建对应的向量库版本

**补全内容**：
1. 创建 GenericAgent 风格的记忆文件：
   ```
   ~/.niu/
   ├── memory.json          # 已有
   ├── global_mem_insight.txt  # 新建（L1 索引）
   ├── global_mem.txt          # 新建（L2 事实）
   └── memory/
       └── *.md              # 新建（L3 SOP）
   ```

2. 修改 `get_global_memory()`：
   ```python
   def get_global_memory():
       # 读取 ~/.niu/global_mem_insight.txt
       # 返回 L1 索引
       ...
   ```

3. 在 NiuHandler 中实现工具：
   ```python
   def do_update_working_checkpoint(self, args, response):
       # 同 GenericAgent
       ...

   def do_start_long_term_update(self, args, response):
       # 同 GenericAgent，但：
       # 1. 读取 ~/.niu/memory/memory_management_sop.md
       # 2. 更新 ~/.niu/global_mem.txt
       # 3. 同步 ~/.niu/global_mem_insight.txt
       # 4. 写入向量库（可选）
       ...
   ```

**优点**：
- ✅ 完全复用 GenericAgent 架构
- ✅ 无需修改向量库
- ✅ 文件易查看和编辑

**缺点**：
- ⚠️ 文件系统与向量库并存（架构分裂）
- ⚠️ 记忆分散在两个系统
- ⚠️ 需要维护两套注入机制

**工作量**：中等（约 1.5 天）

---

#### 方案C：重新设计自我进化机制

**操作**：
1. 移除这两个工具
2. 设计全新的"记忆管理器"

**新架构**：
```python
# 创建独立的 MemoryManager
class MemoryManager:
    """统一记忆管理"""

    def inject_working_memory(self, key_info, related_sop):
        """注入工作记忆（任务级）"""
        ...

    def save_long_term_memory(self, content, memory_type):
        """保存长期记忆（持久化）"""
        # 调用 vector-store/inject_memory
        # 或调用 config-manager/add_user_preference
        ...

    def retrieve_relevant_memories(self, query):
        """检索相关记忆（动态注入）"""
        # 从向量库搜索
        # 从 memory.json 读取
        ...

# 在 NiuHandler 中集成
class NiuHandler(BaseHandler):
    def __init__(self):
        self.memory_manager = MemoryManager()
        ...

    def do_update_memory(self, args, response):
        """统一的记忆更新工具"""
        memory_type = args.get("type")  # working/long_term
        if memory_type == "working":
            return self.memory_manager.inject_working_memory(...)
        else:
            return self.memory_manager.save_long_term_memory(...)
```

**优点**：
- ✅ 架构清晰
- ✅ 可扩展性强
- ✅ 统一管理所有记忆

**缺点**：
- ⚠️ 需要重新设计
- ⚠️ 工作量大

**工作量**：较大（约 3 天）

---

### 推荐方案：方案A（移除并补全缺失能力）

**理由**：
1. **架构一致性**：统一使用向量库，符合 Niu 项目设计
2. **保留核心能力**：自我进化能力完整保留
3. **工作量适中**：约 2 天，可接受

**实施步骤**：

| 步骤 | 任务 | 预估时间 |
|------|------|---------|
| 1 | 向量库 Schema 扩展：添加 L0/L1/L2 分层 | 2h |
| 2 | 创建 `vector-store/inject_memory` MCP 工具 | 2h |
| 3 | 修改 `start_long_term_update`：调用 MCP 而非文件操作 | 2h |
| 4 | 修改 `_inject_dynamic_resources`：搜索记忆类型 | 1h |
| 5 | 测试：用户偏好、环境事实、任务经验 | 2h |
| 6 | 文档更新 | 1h |

**总计**：约 10 小时（1.5 天）

---

## 七、结论

### 7.1 核心问题回答

**问题**：`update_working_checkpoint` 和 `start_long_term_update` 这两个工具，是否是 Agent 自我进化的核心？

**回答**：

1. **`update_working_checkpoint`**：**不是核心能力**
   - 作用：显式更新工作记忆
   - 替代：`tool_after_callback` 自动记录
   - 影响：中等（可接受降级）

2. **`start_long_term_update`**：**是核心能力**
   - 作用：触发长期记忆提炼，实现自我进化
   - 替代：无
   - 影响：严重（丧失自我进化能力）

### 7.2 移除后果

**如果完全移除这两个工具**：

- ❌ Agent 无法自我进化
- ❌ 无法从任务中学习
- ❌ 无法积累任务经验
- ⚠️ 用户偏好需要手动管理
- ⚠️ 环境事实每次重新探测

**结论**：不能完全移除，必须保留自我进化能力。

### 7.3 最终建议

**推荐方案**：方案A（移除并补全缺失能力）

**核心修改**：
1. 移除 `update_working_checkpoint`（用自动记录替代）
2. 保留 `start_long_term_update`，改为调用 MCP 工具
3. 向量库添加记忆分层（L0/L1/L2）
4. 创建 `inject_memory` MCP 工具
5. 统一记忆注入流程

**效果**：
- ✅ 保留自我进化能力
- ✅ 架构统一（全向量库）
- ✅ 可扩展性强
- ✅ 工作量适中（1.5 天）

---

## 附录：关键代码片段

### A.1 GenericAgent 工作记忆注入

```python
# agent/generic/handler.py

def _get_anchor_prompt(self, skip=False):
    if skip:
        return "\n"
    h_str = "\n".join(self.history_info[-20:])
    prompt = f"\n### [WORKING MEMORY]\n<history>\n{h_str}\n</history>"
    prompt += f"\nCurrent turn: {self.current_turn}\n"

    # 注入 key_info
    if self.working.get("key_info"):
        prompt += f"\n<key_info>{self.working.get('key_info')}</key_info>"

    # 注入 related_sop
    if self.working.get("related_sop"):
        prompt += f"\n有不清晰的地方请再次读取{self.working.get('related_sop')}"

    return prompt

def next_prompt_patcher(self, next_prompt, outcome, turn):
    # 每 35 轮强制 ask_user
    if turn % 35 == 0 and "plan" not in str(self.working.get("related_sop")):
        next_prompt += f"\n\n[DANGER] 已连续执行第 {turn} 轮。你必须总结情况进行ask_user，不允许继续重试。"

    # 每 7 轮警告
    elif turn % 7 == 0:
        next_prompt += f"\n\n[DANGER] 已连续执行第 {turn} 轮。禁止无效重试。如有需要，可调用 update_working_checkpoint 保存关键上下文。"

    # 每 10 轮注入全局记忆
    elif turn % 10 == 0:
        next_prompt += get_global_memory()

    return next_prompt
```

### A.2 NiuHandler 工作记忆注入

```python
# agent/handler.py

def tool_after_callback(self, tool_name, args, response, ret):
    """工具调用后记录摘要到 history_info"""
    if args.get("_index", 0) > 0:
        return

    # 提取 <summary> 标签或自动生成摘要
    content = getattr(response, "content", "") if response else ""
    rsumm = re.search(r"<summary>(.*?)</summary>", content, re.DOTALL)
    if rsumm:
        summary = rsumm.group(1).strip()[:200]
    else:
        clean_args = {k: v for k, v in args.items() if not k.startswith("_")}
        summary = f"调用工具{tool_name}, args: {clean_args}"
        if tool_name == "no_tool":
            summary = "直接回答了用户问题"

    self.history_info.append("[Agent] " + summary[:100])

def _get_anchor_prompt(self, skip=False):
    """生成工作记忆提示词"""
    if skip:
        return "\n"

    # 限制历史信息长度
    history_items = self.history_info[-10:]  # 最近10条
    h_str = "\n".join(history_items)
    if len(h_str) > 500:
        h_str = h_str[:500] + "..."

    prompt = f"\n### [WORKING MEMORY]\n<history>\n{h_str}\n</history>"
    prompt += f"\nCurrent turn: {self.current_turn}\n"

    if self.working.get("key_info"):
        key_info = self.working.get("key_info")[:200]
        prompt += f"\n<key_info>{key_info}</key_info>"
    if self.working.get("related_sop"):
        prompt += f"\n有不清晰的地方请再次读取{self.working.get('related_sop')}"

    return prompt

def next_prompt_patcher(self, next_prompt, outcome, turn):
    """周期性警告和全局记忆注入"""
    # 每 35 轮强制 ask_user
    if turn % 35 == 0 and "plan" not in str(self.working.get("related_sop")):
        next_prompt += f"\n\n[DANGER] 已连续执行第 {turn} 轮。你必须总结情况进行 ask_user，不允许继续重试。"

    # 每 7 轮警告
    elif turn % 7 == 0:
        next_prompt += f"\n\n[DANGER] 已连续执行第 {turn} 轮。禁止无效重试。若无有效进展，必须切换策略或请求用户协助。"

    # 每 10 轮注入全局记忆
    if turn % 10 == 0 and turn > 0:
        from .generic.handler import get_global_memory
        try:
            global_mem = get_global_memory()
            if global_mem:
                next_prompt += f"\n\n### [GLOBAL MEMORY]\n{global_mem}"
        except Exception:
            pass

    return next_prompt
```

### A.3 GenericAgent 长期记忆提炼

```python
# agent/generic/handler.py

def do_start_long_term_update(self, args, response):
    """Agent觉得当前任务完成后有重要信息需要记忆时调用此工具。"""
    prompt = """### [总结提炼经验] 既然你觉得当前任务有重要信息需要记忆，请提取最近一次任务中【事实验证成功且长期有效】的环境事实、用户偏好、重要步骤，更新记忆。
本工具是标记开启结算过程，若已在更新记忆过程或没有值得记忆的点，忽略本次调用。

**提取行动验证成功的信息**：
- **环境事实**（路径/凭证/配置）→ `file_patch` 更新 L2，同步 L1
- **复杂任务经验**（关键坑点/前置条件/重要步骤）→ L3 精简 SOP（只记你被坑得多次重试的核心要点）

**禁止**：临时变量、具体推理过程、未验证信息、通用常识、你可以轻松复现的细节。

**操作**：严格遵循提供的L0的记忆更新SOP。先 `file_read` 看现有 → 判断类型 → 最小化更新 → 无新内容跳过，保证对记忆库最小局部修改。
"""
    yield "[Info] Start distilling good memory for long-term storage.\n"
    path = "./memory/memory_management_sop.md"
    if os.path.exists(path):
        result = file_read(path, show_linenos=False)
    else:
        result = "Memory Management SOP not found. Do not update memory."
    return StepOutcome(result, next_prompt=prompt + get_global_memory())
```

### A.4 Niu 项目向量库注入 API

```python
# niu_api/injector.py

@router.post("/mcp-tool", response_model=RegisterMCPToolResponse)
async def register_mcp_tool(request: RegisterMCPToolRequest):
    """注册 MCP 工具到向量库"""
    doc_id = f"mcp_tool:{request.server_name}:{request.tool_name}"
    content = f"{request.tool_name}: {request.description}"
    metadata = {
        "level": "l1",  # L1 层
        "category": "mcp_tool",
        "name": request.tool_name,
        "server": request.server_name,
        "description": request.description,
        "input_schema": request.input_schema,
    }

    success = _register_to_vector_db(doc_id, content, metadata)

    if success:
        return RegisterMCPToolResponse(status="success", resource_id=doc_id)
    else:
        raise HTTPException(status_code=500, detail="Failed to register MCP tool")
```

### A.5 memory-server 记忆存储

```python
# mcp-servers/memory-server/src/niu_memory_server/storage.py

def store_memory(self, content: str, memory_type: str, metadata: dict = None) -> str:
    """存储记忆"""
    import uuid

    memory_id = str(uuid.uuid4())

    # 调用统一的 embedding-service
    embedding = np.array(get_embedding_sync(content), dtype=np.float32)

    full_metadata = {
        "type": memory_type,  # 但缺少 L0/L1/L2 分层
        "created_at": self._get_timestamp(),
        **(metadata or {}),
    }

    conn = sqlite3.connect(self.db_path)
    cursor = conn.cursor()

    cursor.execute(
        "INSERT INTO documents (id, content, embedding, metadata) VALUES (?, ?, ?, ?)",
        (memory_id, content, embedding.tobytes(), json.dumps(full_metadata)),
    )

    conn.commit()
    conn.close()

    logger.info(f"存储记忆: {memory_id[:8]}... ({memory_type})")
    return memory_id
```

---

## 参考资料

1. `agent/generic/handler.py` - GenericAgent 工具实现
2. `agent/generic/memory/memory_management_sop.md` - L0 记忆管理 SOP
3. `agent/handler.py` - NiuHandler 实现
4. `agent/runner.py` - Niu Runner 动态注入
5. `mcp-servers/config-manager/src/niu_config_manager/__init__.py` - 配置管理 MCP
6. `mcp-servers/memory-server/src/niu_memory_server/__init__.py` - 记忆管理 MCP
7. `niu_api/injector.py` - 向量库注入 API
8. `docs/design-dynamic-injection.md` - 动态注入架构设计
