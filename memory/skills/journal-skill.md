---
name: journal-skill
description: Use when user mentions work progress, project updates, meetings, decisions, or asks to record/view/edit daily work logs, or when generating reports from work logs
---

# 工作日志技能

## 日志存储

- 目录：`{workspace}/journals/`
- 文件命名：`YYYY-MM-DD.md`（如 `2026-05-17.md`）
- workspace 路径从 `~/.niu/memory.json` 的 `workspace.path` 字段获取

## 日志条目格式

每条日志是一个三级标题，包含时间戳、任务描述和元数据：

```markdown
## 2026-05-17

### 14:30 完成用户认证模块重构
- 项目：后端服务
- 类型：开发
- 状态：完成
- 关键词：JWT, 认证, 重构

### 16:00 与产品团队讨论需求优先级
- 项目：产品规划
- 类型：会议
- 状态：进行中
- 关键词：需求, 优先级, Q3规划
```

**字段说明**：
- 时间：HH:MM 格式，取对话发生的时间
- 标题：一句话概括工作内容
- 项目：所属项目或工作领域
- 类型：开发/会议/决策/修复/调研/其他
- 状态：完成/进行中/搁置
- 关键词：3-5个关键标签，逗号分隔

## 职业上下文

- 写日志前，先读取 `~/.niu/memory.json` 获取用户职业信息
- 如果 `user.profession` 非空，日志提取时优先关注与该职业相关的工作内容
- 例如：profession="软件工程师" → 重点关注代码开发、技术决策、Bug修复等；profession="产品经理" → 重点关注需求分析、项目进度、用户反馈等
- 职业信息仅作为提取优先级参考，不排除其他类型的工作内容
- 如果 `user.profession` 为空，Agent 应主动询问用户的职业，并通过 `set_user_info(profession="...")` 更新到 memory.json

## 写入规则

1. 识别到工作相关内容时，追加写入当日日志文件
2. 写入流程：
   - `read` 当日文件（如不存在则创建）
   - 在文件末尾追加新条目
   - `write` 写回完整文件内容
3. 同一条工作内容不重复写入（基于对话消息ID追踪）
4. 每日文件以 `## YYYY-MM-DD` 开头作为日期标记

## 知识图谱同步

1. 首次写入日志文件后，调用 `lightrag_insert_file` 入库
2. 日志文件被追加内容后，先 `lightrag_delete_document` 删除旧版本，再 `lightrag_insert_file` 重新入库
3. doc_id 使用文件绝对路径作为固定标识，确保更新时可精确删除

## 去重机制

- 维护已提取消息ID集合，避免同一对话内容重复写入日志
- 如果同一条消息中包含多个工作事项，分别写成独立条目

## Skill 自更新

- 如果用户在交互中对日志格式、分类方式、关注重点等提出明确要求或意见，Agent 应及时用 `edit` 工具更新本 Skill 文件，确保后续日志记录符合用户最新偏好
- 例如：用户说"日志里不需要写关键词了" → 删除关键词字段；用户说"把类型改成：编码/沟通/管理/学习" → 更新类型枚举
- 如果用户给出具体的日志模板，必须用 `edit` 工具将模板写入本 Skill 文件，后续日志严格按模板格式输出

## 主 Agent 交互规则

- 用户说"记录一下今天做了XXX" → 直接写入当日日志
- 用户问"我今天做了什么" → `read` 当日日志文件展示
- 用户说"修改今天的日志" → 修改后重新入库知识图谱
- 用户说"查看本周日志" → 依次 `read` 本周所有日志文件