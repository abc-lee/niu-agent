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
- **何时主动询问主 Agent**（异步模式下必须在正文写明 ask_main_agent 的使用时机，如"遇到用户意图不明确时调 ask_main_agent 询问，不要自行假设"——否则子 Agent 不会主动询问）
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
