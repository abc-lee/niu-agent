---
# 子 Agent 配置模板
# 复制此文件到 ~/.niu/agents/{name}.md 并填写以下字段
# name 用 kebab-case（如 photo-organizer、doc-summarizer）
# 字段格式参照 config/agents/file-processor.md

name: ""                 # 子 Agent 名字（与文件名一致，kebab-case）
description: ""          # 一句话描述子 Agent 的职责（必填，主 Agent 据此判断何时调用）
mode: subagent           # 标识为子 Agent（固定值）
temperature: 0.7         # 可选：覆盖 LLM 温度（0.0 严谨 / 0.7 创意）
taskDescription: ""      # 任务描述模板（主 Agent 调用时填的入参说明）
permissions:
  '*': allow             # 权限声明，默认全部允许
mcpServers: []           # 该子 Agent 可用的 MCP 服务器列表（见下方"可用 MCP 服务器"）
mcpToolFilter: null      # 可选：按 server 分组的白名单 map（见下方说明）
disableBaseTools: []     # 可选：禁用基础工具列表（如 [bash, code_run]）
allowBaseTools: []       # 可选：从 disableBaseTools 解禁的工具列表
allowAsync: false        # 是否允许异步调用（长时任务设为 true）
---

# 提示词正文

在此编写子 Agent 的系统提示词。要说清楚：
- 子 Agent 的角色和职责边界
- 工作流程（先做什么、再做什么）
- 输出格式要求
- **何时主动询问主 Agent**（仅异步模式 allowAsync: true 时才会注入 @前缀拦截层；同步子 Agent 不拦截。异步子 Agent **必须**用 `@niu ` 前缀询问主 Agent，禁止把问题写在 content 里直接返回——直接返回会被程序拒绝并要求重新输出。结束会话必须用 `@end ` 前缀）
- 何时该终止自己

## 可用 MCP 服务器

**重要**：`mcpServers` 字段填的是 **MCP 服务器名**（如下所列，如 `browser-server`、`photo-server`），**不是虚拟磁盘工具前缀**（如 `browser`、`photo`）。主 Agent 日常用 disk 命令看到的是工具前缀，与服务器名不同，不要混淆。

主 Agent 创建子 Agent 时，从以下服务器中选择 `mcpServers` 字段（必需服务器，启动时加载）：

- `file-parser` — 文档解析（PDF/Word/PPT/Excel/MD/HTML）
- `lightrag-server` — 知识图谱 + 向量检索
- `photo-server` — 照片管理 + 人脸识别
- `config-manager` — 配置管理
- `memory-server` — 用户长期记忆
- `session-manager` — 会话管理
- `browser-server` — 浏览器自动化
- `brain-region-server` — 脑区状态管理
- `scheduler-server` — 定时任务调度

可选服务器（见 `agent/mcp_loader.py` 的 `OPTIONAL_SERVERS`，按需启用）：

- `ha-server` — Home Assistant 智能家居

**维护提示**：MCP 服务器清单随项目演进更新，以 `agent/mcp_loader.py` 的 `REQUIRED_SERVERS` + `OPTIONAL_SERVERS` 为准。

## frontmatter 字段说明

- `name`：子 Agent 名字（与文件名一致，kebab-case）
- `description`（必填）：主 Agent 据此判断何时调用此子 Agent
- `mode`：固定 `subagent`，标识为子 Agent
- `temperature`：覆盖 LLM 温度（0.0 严谨 / 0.7 创意）
- `taskDescription`：主 Agent 调 `chat-with-{name}` 时 task 参数的描述
- `permissions`：权限 map，格式 `{ '*': allow }` 默认全部允许
- `mcpServers`：MCP 服务器名字列表，子 Agent 只能用这些服务器的工具
- `mcpToolFilter`：可选，按 server 分组的白名单 map。格式示例：
  ```yaml
  mcpToolFilter:
    lightrag-server:
      - lightrag_insert
      - lightrag_search_entities
  ```
- `disableBaseTools`：可选，禁用基础工具列表（如 `[bash, code_run, read, write, edit, grep]`）
- `allowBaseTools`：可选，从 disableBaseTools 解禁的工具列表（黑名单中的例外）
- `allowAsync`：true 时支持异步调用（主 Agent 调用后立即返回，子 Agent 后台跑；异步子 Agent 自动启用 @前缀拦截层，必须用 @niu/@end 表达意图）
