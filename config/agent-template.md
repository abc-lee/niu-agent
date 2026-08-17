---
# 子 Agent 配置模板
# 复制此文件到 ~/.niu/agents/{name}.md 并填写以下字段
# name 用 kebab-case（如 photo-organizer、doc-summarizer）
# 完整字段说明见本文档下方"frontmatter 字段说明"

name: ""                 # 必填：子 Agent 名字（与文件名一致，kebab-case）
description: ""          # 必填：一句话描述职责（主 Agent 据此判断何时调用你）
mode: subagent           # 固定值，标识为子 Agent
temperature: 0.7         # 可选：LLM 温度（0.0-0.3 严谨 / 0.7 创意），不写用默认
mcpServers: []           # 该子 Agent 可用的 MCP 服务器列表（白名单，见下方"可用 MCP 服务器"）
mcpToolFilter: {}        # 可选：对 mcpServers 再做工具级白名单（见下方说明；{} 表示不过滤）
allowBaseTools: []       # 可选：基础工具白名单（见下方"基础工具白名单"，不写=一个都没有）
allowAsync: false        # 可选：true=允许异步调用（长时任务用）
---

# 提示词正文

在此编写子 Agent 的系统提示词。要说清楚：
- 子 Agent 的角色和职责边界
- 工作流程（先做什么、再做什么）
- **输出格式要求（必须明确给出标准格式，禁止子Agent只输出 @end）
- 你是一个独立运行的子Agent，你所有的对话输出都会被程序拦截，没有人能够看到。所以你不需要说话，只需要调用工具工作。如果希望你说的话被主Agent看到，必须使用 @niu-agent 或者在 @end 同时输出你的工作汇报。
- **任务完成必须返回结果**：完成任务后，必须将最终结果（报告、总结、答案等）完整输出出来，再结束会话。不能只在内部处理完就直接结束，主 Agent 需要看到你的输出。系统会自动处理超长内容（超过2000字符会写入临时文件，主Agent会收到文件路径提示），你只需要正常输出完整内容即可，不用担心长度限制。
- **标准结束格式（必须严格遵守，创建子Agent时必须写入提示词正文）**：
  ```
  ## 一、工作总结
  （本次做了什么，完成了哪些操作，1-3条简洁列出）

  ## 二、任务结果
  （主Agent交办任务的完整结果，报告/分析/清单等详细内容，该写多长写多长）

  @end
  ```
  ⚠️ 严禁只输出 `@end` 而不汇报结果！主Agent必须看到工作成果才能转达给用户。
  ⚠️ 严禁工具调用和 `@end` 放在同一轮！最后一轮只能是纯文本汇报 + @end，不能有任何工具调用。
- **何时主动询问主 Agent**：所有子 Agent（同步 + 异步）都被程序注入 @niu-agent/@end 守则。子 Agent 用 `@niu-agent ` 前缀询问主 Agent，用 `@end ` 前缀结束会话。子 Agent 不需要在输出里包含自己的标识符，程序会自动在你的问题前加上唯一标识，主 Agent 据此回复你。
- **与用户交互**：子 Agent 可以用 `@user 问题内容` 向用户提问（阻塞等待回答，10 分钟超时）。如果你建的子 Agent 需要与用户多轮交互（如需要用户确认、选择方案、澄清需求），在正文提示词中说明交互流程即可——前端会自动为子 Agent 创建独立标签页，用户可在其中看到子 Agent 的工作过程并与它交流。注意：没有用户与子 Agent 对话时不要使用 `@user`，因为它会阻塞工作进度。
- 何时该终止自己
- 由于子Agent是没有历史记忆的，每次调用它都是新的会话。如果你需要一个有长期记忆的子Agent，那么你就要在提示词当中，给他指定好只属于他自己的工作目录和记录工作事件的文档名以及记录方法，并要求每次启动后第一件事是先读取这个文档。每次结束时必须整理内容，记录到这个文档后才可以@end。

## 工具白名单规则（重要，先读懂再配）

子 Agent 的工具分两类，**两类都是白名单制：写了才有，没写就没有**。

### 1. MCP 工具（mcpServers + mcpToolFilter）

- `mcpServers`：填 **MCP 服务器名**（如下所列，如 `browser-server`、`photo-server`），**不是虚拟磁盘工具前缀**（如 `browser`、`photo`）。主 Agent 日常用 disk 命令看到的是工具前缀，与服务器名不同，不要混淆。
- 写了某个服务器，该服务器的**全部**工具可用。
- `mcpToolFilter`：可选，在 mcpServers 基础上再收窄到指定工具。格式：
  ```yaml
  mcpServers:
    - lightrag-server
  mcpToolFilter:
    lightrag-server:
      - lightrag_insert
      - lightrag_search_entities
  ```

可用 MCP 服务器（必需服务器，启动时加载）：

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

### 2. 基础工具（allowBaseTools）

基础工具是系统自带的 6 个，**缺省一个都没有**，`allowBaseTools` 声明哪个有哪个：

| 工具 | 用途 | 什么时候给 |
|---|---|---|
| `read` | 读文件内容 | 需要读本地文件（对话记录、文档、配置） |
| `write` | 写文件 | 需要产出文件（报告、日志、skill 文件） |
| `edit` | 编辑已有文件 | 需要修改文件而非重写 |
| `grep` | 搜索文件内容 | 需要在文件中查找内容 |
| `bash` | 执行 shell 命令 | 需要 curl 网络请求、系统命令、管道处理 |
| `code_run` | 运行 Python 代码 | 需要复杂数据处理、计算 |

**常见错误**：正文里写"使用 bash 工具做 XXX"，但 frontmatter 没把 `bash` 写进 `allowBaseTools`——子 Agent 运行时**根本没有 bash 这个工具**，会直接失败。正文提到的每个基础工具，都必须出现在 `allowBaseTools` 里。

**笔误防护**：`allowBaseTools` 里写了不存在的工具名（如 `reed`），启动日志会打 warning（`unknown tool names`），该名字被忽略。

**判断原则**：能用 MCP 服务器完成的操作（如知识图谱、照片、定时任务）就不要给基础工具；基础工具只给正文确实需要的。

## frontmatter 字段说明

- `name`（必填）：子 Agent 名字（与文件名一致，kebab-case）
- `description`（必填）：主 Agent 据此判断何时调用此子 Agent，写清楚"什么任务该找你"
- `mode`（惯例）：固定 `subagent`，代码不校验，保留用于文档标识
- `temperature`（可选）：LLM 温度。0.0-0.3 适合严谨任务（数据处理、入库），0.7 适合创意任务（写作、摘要），不写则用系统 LLM 配置默认温度
- `mcpServers`（可选）：MCP 服务器名列表，见上方。不写=没有 MCP 工具
- `mcpToolFilter`（可选）：按 server 分组的工具级白名单 map，见上方
- `allowBaseTools`（可选）：基础工具白名单，见上方。不写=没有基础工具
- `allowAsync`（可选）：true 时支持异步调用（主 Agent 调用后立即返回，子 Agent 后台跑；异步子 Agent 自动启用 @前缀拦截层，必须用 @niu-agent/@end 表达意图）。长时任务（几十秒以上）设 true

## 完整示例

### 示例 1：纯 MCP 子 Agent（不需要基础工具）

```yaml
---
name: kg-query
description: "知识图谱查询：按实体/关系/时间线检索知识库，返回结构化结果"
mode: subagent
temperature: 0.2
mcpServers:
  - lightrag-server
mcpToolFilter:
  lightrag-server:
    - lightrag_search_entities
    - lightrag_get_entity_info
    - lightrag_timeline_query
allowAsync: false
---

你是知识图谱查询子 Agent。用 lightrag 工具检索，把结果整理成表格返回。（可在此补充输出格式、交互流程等指令）
```

### 示例 2：需要 bash 的网络抓取子 Agent

```yaml
---
name: web-fetcher
description: "网页抓取：用 curl 抓取指定 URL 内容并整理成摘要"
mode: subagent
temperature: 0.3
mcpServers: []
allowBaseTools:
  - bash
  - write
allowAsync: true
---

你是网页抓取子 Agent。用 bash 工具执行 curl 抓取网页，抓到的原文用 write 存到 /tmp/，最后整理成摘要返回。（可在此补充输出格式等指令）
```

注意示例 2：正文用了 `bash` 和 `write`，`allowBaseTools` 里就声明了这两个——**正文提到的基础工具必须全部声明**。
