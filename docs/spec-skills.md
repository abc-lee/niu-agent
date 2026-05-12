# Skills 编写规范

> 版本：v1.0
> 日期：2026-04-15
> 更新：初始版本

---

## 概述

Skills 是 Agent 的可复用能力库，通过文件形式存储在 `memory/skills/` 目录下。系统启动时自动同步到向量库，推理时根据上下文自动注入相关 Skills。

---

## 文件命名规范

**格式**：`{功能名}.md`

**示例**：
- `browser-automation.md`
- `photo-processing.md`
- `document-ingestion.md`

---

## 文件结构

每个 Skill 文件必须包含以下部分：

### 1. 文件头（必须）

```markdown
# {技能名称}

**触发关键词**：关键词1、关键词2、关键词3

**L1 摘要**：{L1格式内容}
```

#### 触发关键词

- 逗号分隔，涵盖用户可能的表达方式
- 用于快速匹配用户意图
- 示例：`提醒、定时、闹钟、几点`

#### L1 摘要（必须，英文）

采用管道分隔格式：

```
{标题}|{关键词}|{摘要}|{实体}|{类型}|{指针}
```

| 字段 | 说明 | 示例 |
|------|------|------|
| 标题 | 简短英文标题 | `Browser automation` |
| 关键词 | 逗号分隔的关键词 | `browser,form filling,web operation` |
| 摘要 | 详细描述（英文） | `Use browser_navigate + code_run to...` |
| 实体 | 相关工具/实体 | `browser_navigate,Playwright,BrowserManager` |
| 类型 | 固定为 `skill` | `skill` |
| 指针 | 文件相对路径 | `memory/skills/browser-automation.md` |

**示例**：
```
Browser automation|browser,form filling,web operation|Use browser_navigate + browser_interact + browser_new_tab to automate browser tasks|browser_navigate,browser_interact,browser_new_tab,Chrome Extension|skill|memory/skills/browser-automation.md
```

### 2. 工具说明

列出 Skill 使用的所有工具：

```markdown
## 工具

| 工具 | 用途 |
|------|------|
| `tool_name(param)` | 功能说明 |
```

### 3. 工作流程

用文字或流程图描述标准操作流程：

```markdown
## 工作流程

```
步骤1 → 步骤2 → 步骤3
```
```

### 4. 核心规则

用表格或列表详细说明操作规则：

```markdown
## 规则

### 规则名称

**重要说明**

- 要点1
- 要点2
```

### 5. 示例（推荐）

提供典型场景的操作示例：

```markdown
## 示例

填写表单：
```
browser_navigate("https://example.com/login")
  → [0]<input name=username />
browser_interact(action="input", index=0, text="user")
  → 返回新状态
```
```

---

## Metadata 结构

| 字段 | 类型 | 必须 | 说明 |
|------|------|------|------|
| `level` | string | ✅ | 固定为 `"l1"` |
| `category` | string | ✅ | 固定为 `"skill"` |
| `language` | string | ✅ | 固定为 `"en"` |
| `name` | string | ✅ | Skill名称 |
| `description` | string | ✅ | 英文描述 |
| `source` | string | ✅ | 文件路径 |
| `priority` | int | ❌ | 优先级，默认50 |
| `tags` | array | ❌ | 标签列表 |
| `triggers` | array | ❌ | 触发条件 |

---

## 编写检查清单

创建新的 Skill 时，确保：

- [ ] 文件名符合命名规范
- [ ] 触发关键词覆盖常见表达
- [ ] L1 摘要使用英文、管道格式
- [ ] 工具列表完整准确
- [ ] 规则清晰、可执行
- [ ] 包含典型示例
- [ ] 使用相对路径作为指针

---

## 示例：完整的 Skill 文件

```markdown
# 照片处理 Skill

**触发关键词**：照片、入库、人脸识别、人物命名、处理照片、拖入照片

**L1 摘要**：Photo processing|photo,face,image,ingestion,recognition|Handle photo ingestion, face recognition, person naming and querying via chat-with-file-processor|photo,face,person,naming,chat-with-file-processor|skill|memory/skills/photo-processing.md

## 工具

| 工具 | 用途 |
|------|------|
| `chat-with-file-processor` | 统一入口，委托子Agent处理 |
| `photo-server/ingest_photos` | 照片入库（自动判断单张/目录） |
| `photo-server/name_person` | 为人物命名 |

## 规则

### 入库时机

用户拖入文件、说"入库"、"处理照片"时，必须调用 `chat-with-file-processor`。

❌ 不要自己读取文件
❌ 不要自己复制文件
✅ 调用专用工具

### 批量处理

多个文件分别调用，每次一个：
```
chat-with-file-processor({"task": "入库照片：E:/path/photo1.jpg"})
chat-with-file-processor({"task": "入库照片：E:/path/photo2.jpg"})
```

### 人物命名

1. 调用 `chat-with-file-processor` 显示照片和人脸框
2. 用户回答名字
3. 调用 `name_person` 完成命名
```

---

## 常见问题

### Q: 何时创建新的 Skill？

A: 当某个功能满足以下条件时：
1. 有多个工具需要组合使用
2. 有固定的执行流程
3. 有特定的规则和注意事项
4. 会被多次复用

### Q: Skill 和 SOP 的区别？

| 维度 | Skill | SOP |
|------|-------|-----|
| 粒度 | 原子能力 | 完整流程 |
| 结构 | 自由格式 | 步骤编号 |
| 用途 | 工具调用指引 | 复杂任务执行 |

### Q: 触发关键词越多越好吗？

A: 适度即可。覆盖 5-10 个核心表达即可，太多会导致误匹配。
