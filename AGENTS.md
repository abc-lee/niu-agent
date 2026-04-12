# AI-Bot 项目知识库

> 核心架构：Electron 前端 + Python Agent (GenericAgent) + 多种 LLM

## 项目结构

```
ai-bot/
├── main.go              # 入口，Electron 窗口管理（瘦身后）
├── agent/               # Python Agent 核心
│   ├── generic/         # GenericAgent 原始代码（~1700行）
│   │   ├── agent_loop.py    # 核心循环（99行）
│   │   ├── handler.py       # 工具实现（526行）
│   │   ├── llmcore.py       # LLM 抽象层（835行）
│   │   └── assets/          # 工具描述、sys_prompt
│   ├── session_adapter.py   # Session 管理 + SQLite 持久化
│   ├── runner.py            # GenericAgentRunner 整合层
│   ├── vector_search.py     # 向量检索注入
│   └── thinking_chain.py    # 思考链处理器
├── niu_api/             # FastAPI 服务层
├── ui/
│   ├── assistant/       # 悬浮窗 UI
│   │   ├── chat.html    # 聊天对话框
│   │   ├── spirit.html  # 小女孩悬浮窗
│   │   ├── sticky.html  # 便签窗口
│   │   └── fonts/       # 本地字体
│   ├── graph/           # 知识图谱 UI
│   └── settings/        # 设置页面
├── config/
│   └── agents/          # Agent 提示词配置
│       └── niu.md       # 主 Agent 提示词
├── mcp-servers/         # MCP 服务器（独立进程）
│   ├── photo-server/    # 照片处理 + 人脸识别
│   ├── kg-server/       # 知识图谱
│   ├── vector-store/    # 向量存储
│   └── ...
└── docs/                # 设计文档
```

## 核心原则

### GenericAgent 整合原则

**架构**：
```
GenericAgent 原始代码（agent/generic/）
    ↓ 不修改
适配层（session_adapter.py, runner.py）
    ↓ 扩展功能
向量检索注入（vector_search.py）
思考链处理（thinking_chain.py）
```

### 文件处理必须由子 Agent 处理

文件处理（文档入库、照片人脸识别）非常耗时，必须委托给 `file-processor` 子 Agent。主 Agent 只负责接收用户请求、调用子 Agent、返回结果。

---

## 当前功能

| 功能 | 状态 | 说明 |
|------|------|------|
| 聊天对话 | ✅ | **会话持久化**：消息存储到数据库，重启后可恢复上下文 |
| 聊天历史 | ✅ | 打开窗口加载历史，滚动到顶部加载更多 |
| 文件归档 | ✅ | 拖入文件自动处理入库 |
| 知识图谱 | ✅ | 实体提取、关联建立 |
| 向量搜索 | ✅ | 语义搜索 + 每轮对话自动注入 |
| 照片处理 | ✅ | 人脸识别、人物管理、L0摘要 |
| 悬浮窗 | ✅ | 引用计数状态机 |
| 便签 | ✅ | 滚轮翻页，自动缩放 |
| 字体 | ✅ | 阿朱泡泡体 + Caveat fallback |
| 定时任务 | ✅ | 调度器 + Agent 处理 + 小女孩报警 |
| 思考链处理 | ✅ | 支持 DeepSeek/MiniMax/Qwen 等多厂商 |
| Token 统计 | ✅ | MockResponse.usage 返回 input/output/total_tokens |

---

## GenericAgent 整合状态

| Step | 状态 | 说明 |
|------|------|------|
| **1. 核心搬运** | ✅ | agent/generic/ (~1700行) |
| **2. Session隔离** | ✅ | session_adapter.py + SQLite持久化 |
| **3. 向量检索** | ✅ | 每轮注入，>50分，最多10条 |
| **4. Token+思考链** | ✅ | usage属性 + 多厂商思考链处理 |
| **5. SubAgent** | ⏳ | 待实现 |

---

## 技术细节

### 字体方案

```css
font-family: 'AZhuPaoPaoTi', 'Caveat', system-ui, sans-serif;
```

- 中文：阿朱泡泡体（本地打包）
- 英文：Caveat（Google Fonts）
- 铅笔效果：`color: #000; -webkit-text-stroke: 0.2px rgba(0,0,0,0.35);`

### 悬浮窗状态机

引用计数模式：
```javascript
let busyCount = 0;
incrementBusy(reason)  // busyCount++
decrementBusy(reason)  // busyCount--, 0 则 IDLE
```

### 聊天消息发送

1. 显示 3 点动画
2. 自动重试连接（10 次，每次 2 秒）
3. 保留换行符（`white-space: pre-wrap`）

---

## 已完成功能

### 照片处理

- 设计文档：`docs/feature-photo-processing.md`
- 服务位置：`mcp-servers/photo-server/`
- 已实现工具：
  - `ingest_document` - 文档入库
  - `ingest_documents` - 批量文档入库
  - `ingest_photo` - 照片入库+人脸识别
  - `ingest_photos` - 智能入库（单张/目录）
  - `name_person` - 人物命名（自动生成名字向量）
  - `merge_persons` - 人物合并
  - `search_persons` - 按名字搜索人物
  - `get_unnamed_persons` - 获取未命名人物列表
  - `unload_face_model` - 卸载人脸识别模型（释放 ~326MB 内存）

**数据存储**：
- 人物名向量：persons.name_embedding
- 人脸向量：faces.embedding
- 同框关系：co_occurrences 表

**模型管理**：
- InsightFace buffalo_l 模型懒加载（第一次处理照片时加载）
- 空闲 5 分钟自动卸载
- 预加载机制：MCP stdio 启动前加载 cv2 和 InsightFace 模块代码

---

## 开发规范

### MCP Server 注册规范

**必须步骤**：

| 步骤 | 说明 | 示例 |
|------|------|------|
| 1. 目录结构 | `mcp-servers/<name>/src/niu_<name>/` | `mcp-servers/photo-server/src/niu_photo_server/` |
| 2. 必需文件 | `__init__.py` + `__main__.py` + `pyproject.toml` | 见下方模板 |
| 3. 配置 | `config/mcp-servers.yaml` | 添加 server 配置 |

**关键点**：
- `workdir` 指向 `src/` 目录，Python 会把它加到 `sys.path`
- **不需要 `pip install`**，通过 `workdir` 即可找到模块
- `python -m niu_xxx` 需要模块目录下有 `__main__.py`

**pyproject.toml 模板**：
```toml
[project]
name = "niu-<server-name>"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["mcp>=1.0.0", "loguru>=0.7.0"]

[project.scripts]
niu-<server-name> = "niu_<server_name>:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

**mcp-servers.yaml 配置**：
```yaml
server-name:
  command: ${PYTHON_PATH}
  args:
    - "-m"
    - "niu_server_name"
  workdir: ../mcp-servers/server-name/src
  # preload: true  # 可选，启动时预加载
```

**PYTHON_PATH 解析顺序**（由 `main.go` 自动检测）：
1. 打包的 Python：`E:/tools/ai-bot/python/`
2. venv 虚拟环境
3. 系统 Python
4. PATH 环境变量

---

## 配置文件架构

### 程序目录 `config/`

| 文件 | 用途 | 说明 |
|------|------|------|
| `config/user-config.json` | LLM API Key、模型选择 | 用户可修改 |
| `config/llm-presets.json` | LLM 预设列表 | 可添加新预设 |
| `config/agents/niu.md` | 主 Agent 定义 | 提示词、权限、MCP服务器 |
| `config/agents/file-processor.md` | 子 Agent 定义 | 文件处理专用 |
| `config/mcp-servers.yaml` | MCP 服务器配置 | 高级用户 |

### 模型目录 `models/`

| 目录 | 大小 | 用途 |
|------|------|------|
| `models/all-MiniLM-L6-v2/` | ~90 MB | SentenceTransformer 文本向量 |
| `models/models/buffalo_l/` | ~326 MB | InsightFace 人脸识别 |

**加载逻辑**：优先从本地加载，本地没有才下载。

### 用户目录 `~/.niu/`

| 文件 | 用途 |
|------|------|
| `memory.json` | 用户记忆（身份、偏好、工作目录） |
| `preferences.json` | 存储配置（分类、路径结构、冲突阈值） |

**详细配置说明在 `config/agents/niu.md`，给 Agent 看的。**

---

## Agent 定义 vs Skill

### 文件位置

| 类型 | 位置 | 示例 |
|------|------|------|
| Agent 定义 | `config/agents/*.md` | `file-processor.md` |
| Skill | `pkg/servers/system/skills/*.md` | `file-processing.md` |

### 本质区别

| 项目 | Agent 定义 | Skill |
|------|-----------|-------|
| 类型 | 独立子 Agent 进程 | 知识模块（嵌入主 Agent） |
| 调用方式 | `chat-with-xxx` | `skill(name="xxx")` |
| MCP 服务器 | 有独立配置 | 共享主 Agent 的 |
| 上下文 | 独立隔离 | 共享主 Agent 上下文 |

### 配合关系

```
主 Agent (niu.md)
    ↓ 加载 skill
[file-processing.md] → 告诉主 Agent："文件处理要委托给子 Agent"
    ↓ 调用
chat-with-file-processor
    ↓ 启动
子 Agent (file-processor.md) → 执行具体文件处理
```

**Skill** = 说明书（告诉主 Agent 怎么做）
**Agent 定义** = 执行者（定义被委托者的行为）

---

## 文档相似度计算

### 改造记录

| 项目 | 改动前 | 改动后 |
|------|--------|--------|
| 算法 | TF-IDF (sklearn) | 语义向量 (sentence-transformers) |
| 依赖大小 | ~180 MB | ~90 MB |
| 预导入问题 | 必须预导入，否则卡死 | 无问题 |

### 卡死原因

sklearn 是 C 扩展，在 Windows + stdio 通信 + asyncio 事件循环中动态导入会死锁。解决方案：预导入或在模块顶层导入。

### 阈值说明

语义相似度判断的是"是不是同一个文档"，不是"内容改了多少"。

| 相似度 | 含义 |
|--------|------|
| 0.95+ | 几乎相同（小修改、版本更新） |
| 0.50-0.95 | 同一文档的不同版本 |
| 0.00-0.50 | 同名但完全不同的东西 |

当前阈值：`0.5`（在 `~/.niu/preferences.json` 中配置）

---

## 相关文档

- `docs/feature-file-management.md` - 文件管理设计
- `docs/feature-document-processing.md` - 文档处理设计
- `docs/feature-scheduled-tasks.md` - 定时任务设计
- `docs/note-agent-communication.md` - Agent 通讯技术笔记

---

## 更新日志

### 2026-04-02

#### 修复：照片拖入卡死问题

**问题**：文件拖入正常，照片拖入卡死，日志显示 `photo-server stdin is closed`

**根因**：`asyncio.to_thread` + InsightFace/ONNX Runtime 在 MCP stdio 环境中存在兼容性问题

**解决方案**：
- 将 `mcp-servers/photo-server/__init__.py` 的工具调用从 `asyncio.to_thread` 改为同步调用
- 添加 `preload_face_model()` 在 MCP stdio 启动前预加载 cv2 和 InsightFace 模块

**教训**：
- **MCP 工具调用优先用同步**：`asyncio.to_thread` 在 MCP stdio 环境中可能有问题，特别是涉及 ONNX Runtime 等原生库时
- 不同机器表现不同，慢电脑更容易出问题

#### 新增：人脸识别模型空闲卸载

**问题**：InsightFace 模型加载后占用 ~326MB 内存，永不释放

**解决方案**：
- 后台定时器线程每 60 秒检查
- 空闲超过 5 分钟自动卸载模型
- 配置：`MODEL_IDLE_TIMEOUT_SECONDS = 300`

**教训**：
- **不要在卸载时调用 `gc.collect()`**：可能在其他线程使用对象时释放，导致崩溃
- 让 Python 垃圾回收器自然回收

#### ⚠️ 关键经验：避免 gc.collect() 导致的崩溃

```python
# 错误：可能在 detect_faces() 使用模型时释放
def unload_face_model():
    global _face_model
    _face_model = None
    gc.collect()  # 危险！

# 正确：让 Python 自然回收
def unload_face_model():
    global _face_model
    _face_model = None
    # 不调用 gc.collect()
```

**原因**：如果 `detect_faces()` 正在执行 `face_model.get(img)`，此时 `_face_model = None` 只是移除全局引用，但 `detect_faces()` 仍有局部引用。然而 `gc.collect()` 会立即回收没有任何引用的对象，可能导致崩溃。

---

### 2026-04-03

#### 新增：/new 清空聊天记录

**功能**：在聊天框输入 `/new` 清空当前会话的所有聊天记录。

**实现**：
- `main.go` 添加 `/api/chat/clear` 端点，调用 `sessionManager.DB.DeleteMessages()`
- `preload-chat.js` 添加 `clearChat()` API
- `main.js` 添加 `clear-chat` IPC handler
- `chat.html` 的 `sendMessage()` 检测 `/new` 指令

#### 新增：输入框支持多行输入

**问题**：粘贴带换行的文本时换行符丢失。

**原因**：使用 `<input type="text">` 单行输入框。

**解决方案**：
- 改用 `<textarea>` 支持多行输入
- Enter 发送，Shift+Enter 换行
- 自动调整高度（最大 120px）

**修改文件**：`ui/assistant/chat.html`

#### 修复：主 Agent 工具丢失

**问题**：主 Agent 只显示 3 个 `chat-with-*` 子 Agent 工具，看不到 MCP Server 工具。

**原因**：`pkg/toolloop/toolloop.go` 第 172 行缺少 `agent.MCPServers`：
```go
// 错误
toolMappings, err := registry.BuildToolMappings(ctx, append(agent.Tools, agent.Agents...))
// 正确
toolMappings, err := registry.BuildToolMappings(ctx, append(agent.Tools, append(agent.Agents, agent.MCPServers...)...))
```

**修改文件**：`pkg/toolloop/toolloop.go`

#### 修复：主 Agent 缺少系统工具

**问题**：bash、read、write、edit、glob、grep 等系统工具不可用。

**原因**：`niu.md` 的 `mcpServers` 列表缺少 `nanobot.system`。

**解决方案**：添加 `nanobot.system` 到 mcpServers 列表。

**修改文件**：`config/agents/niu.md`

#### 优化：Agent 提示词改进

**问题**：Agent 收到指令后返回"执行中..."但不调用工具。

**原因**：LLM 把说话当作"执行"，没有理解"执行 = 调用工具"。

**解决方案**：将抽象规则改为具体操作指令，添加错误/正确示例。

**修改文件**：`config/agents/niu.md`

#### 删除：10 轮对话自动整理

**问题**：正常对话过程中触发睡眠模式整理。

**原因**：遗留的 10 轮对话整理代码。

**解决方案**：删除 `main.go` 中的自动整理逻辑。

---

### 2026-04-03

#### 重构：GenericAgent 整合

**目标**：用 GenericAgent（~1700行）替换 Nanobot（~53万行），实现更简洁的 Agent 架构。

**完成的工作**：

| Step | 内容 | 提交 |
|------|------|------|
| **1** | 全量搬运 GenericAgent 核心代码到 `agent/generic/` | `06792e8` |
| **2** | Session 隔离 + SQLite 持久化适配层 | `6c256a5` |
| **3** | 向量检索注入（每轮对话注入，>50分，最多10条） | `f388f58` |
| **4** | Token 返回 + 思考链处理 | `b3c3ffd`, `9452d51` |

**新增文件**：
- `agent/generic/` — GenericAgent 原始代码（不修改）
- `agent/session_adapter.py` — Session/SessionManager 类
- `agent/runner.py` — GenericAgentRunner 整合层
- `agent/vector_search.py` — 向量检索适配器
- `agent/thinking_chain.py` — 思考链处理器（支持 DeepSeek/MiniMax/Qwen 等）

**设计决策**：
- 研究了 Strands Agents SDK (5.5K星)，决定继续用 GenericAgent（小而精、可控性高）
- GenericAgent 作为主 Agent 循环，SubAgent 作为临时专业工人（待实现）

**思考链处理**：
统一处理不同厂商的思考链格式：
- DeepSeek: `菏...SaveChanges` 或 `<thinking>...</thinking>`
- MiniMax M2.5: `<FLUX>...</FLUX>`
- Claude: API 原生 thinking block
- OpenAI o1: `reasoning_content` 字段

**Token 返回**：
- `MockResponse` 新增 `usage` 属性
- `_parse_claude_sse` 返回 `(content_blocks, usage_info)`

**删除的代码**：
- `pkg/` 目录已清空（Nanobot Go 代码已移除）

---

### 2026-04-04

#### 新增：动态注入架构

**目标**：MCP 工具描述和 Skills 内容按语义动态注入提示词，减少基础提示词长度。

**实现**：
- `agent/injector/sync.py` — Skills 定时扫描同步到向量库（`metadata.type="skill"`）
- `niu_api/injector.py` — API 端点手动注册 MCP 工具描述（`metadata.type="mcp_tool"`）
- `agent/runner.py` — `_inject_dynamic_resources()` 按语义搜索并注入

**向量库标签**：
| 标签 | 用途 |
|------|------|
| `l1` | L1 摘要（现有） |
| `l2` | L2 原文（现有） |
| `skill` | Skills 文件（新增） |
| `mcp_tool` | MCP 工具描述（新增） |

#### 修复：同步/异步架构冲突

**问题**：GenericAgent 纯同步，MCP 客户端异步，FastAPI 异步端点，导致事件循环冲突。

**解决方案**：
- 新增 `agent/mcp_sync_bridge.py` — 后台事件循环 + `run_coroutine_threadsafe`
- 修改 `agent/handler.py` 的 `dispatch()` 使用 `MCPSyncBridge` 调用 MCP 工具
- 修改 `niu_api/compat.py` 使用 `asyncio.to_thread` 运行同步 chat

#### 修复：历史对话丢失

**问题**：`niu_api/session.py` 将 `session_id` 当作 `limit` 参数传入。

**解决方案**：修复 API 调用，移除多余的 `session_id` 参数。

#### 修复：MCP 工具未挂载

**问题**：`compat.py` 调用 `get_runner()` 创建新 Runner，没有 MCP 工具。

**解决方案**：改用 `get_or_create_runner()` 使用预初始化的 Runner。

#### 修复：人脸识别卡死

**问题**：`preload_face_model()` 被注释掉，导致 InsightFace 模块在 MCP stdio 环境中动态导入卡死。

**解决方案**：恢复 `preload_face_model()` 调用（在 MCP stdio 启动前预加载 cv2 和 InsightFace 模块代码）。

**修改文件**：
- `agent/mcp_sync_bridge.py` — 新增
- `agent/injector/sync.py` — 新增
- `agent/injector/__init__.py` — 新增
- `niu_api/injector.py` — 新增
- `agent/runner.py` — 动态注入 + MCP 工具挂载
- `agent/handler.py` — MCP 同步桥接
- `niu_api/compat.py` — asyncio.to_thread
- `niu_api/session.py` — API 修复
- `mcp-servers/photo-server/src/niu_photo_server/__init__.py` — 恢复预加载

---

### 2026-04-04

#### 修复：NiuHandler 缺少工作记忆机制

**问题**：Agent 无法"自我进化"，工具循环表现异常，代码直接显示给用户而非执行。

**根因分析**：通过多个 Agent 并行深度分析，发现 NiuHandler 缺少原始 GenericAgent 的核心机制：

| 机制 | 作用 | NiuHandler 状态 |
|------|------|----------------|
| `tool_after_callback` | 每次工具调用后记录摘要到 `history_info` | ❌ 缺失 |
| `_get_anchor_prompt` | 生成工作记忆提示词注入 `next_prompt` | ❌ 缺失 |
| `next_prompt_patcher` | 周期性警告防止死循环 | ❌ 缺失 |

**解决方案**：

1. **添加 `tool_after_callback`**：工具调用后提取 `<summary>` 或自动生成摘要，追加到 `history_info`

2. **添加 `_get_anchor_prompt`**：生成包含 `history_info[-20:]`、`current_turn`、`key_info` 的工作记忆提示词

3. **添加 `next_prompt_patcher`**：
   - 每 35 轮强制 `ask_user`
   - 每 7 轮警告禁止无效重试
   - 每 10 轮注入全局记忆

4. **修改各 `do_XXX` 方法**：使用 `_get_anchor_prompt()` 替代硬编码的 `"\n"`

5. **添加状态重置**：`/new` 命令时调用 `reset_working_memory()`

**修改文件**：
- `agent/handler.py` — 添加工作记忆机制
- `niu_api/compat.py` — 添加状态重置调用

#### 修复：子 Agent 缺少 MCP 工具

**问题**：子 Agent（file-processor 等）调用 MCP 工具失败。

**原因**：`subagent.py` 只获取基础工具 schema，没有 MCP 工具。

**解决方案**：
- `mcp_client.py` 添加 `get_mcp_tools_for_servers()` 按 server 名称过滤工具
- `subagent.py` 添加 `get_subagent_mcp_tools_schema()` 根据 `mcpServers` 配置获取工具

#### 修复：空代码块显示问题

**问题**：LLM 响应中的空代码块原样输出，显示为多个 ` `````` `。

**解决方案**：在 `runner.py` 添加清理空代码块的正则表达式。

**修改文件**：
- `agent/runner.py` — 添加空代码块清理
- `agent/mcp_client.py` — 添加 MCP 工具过滤函数
- `agent/subagent.py` — 添加子 Agent MCP 工具获取

---

### 2026-04-02

#### 修复：Electron 关闭时 Go 后端不退出

**问题**：关闭 Electron 窗口后，Go 后端和 embedding 服务进程残留。

**原因**：Electron 退出时没有通知 Go 后端关闭。

**解决方案**：
- `main.go` 添加 `/api/shutdown` 端点，调用 `cancel()` 取消 context
- `ui/assistant/main.js` 在 `close-all`、托盘关闭、`before-quit` 中调用该端点

**修改文件**：
- `main.go` — 添加 shutdown 端点
- `ui/assistant/main.js` — 3 处关闭入口调用 `/api/shutdown`

#### 新增：聊天历史加载功能

**问题**：打开聊天窗口没有历史消息，刷新也不显示。

**原因**：
- `preload-chat.js` 缺少 `getHistory` API
- `main.js` 缺少 `get-history` IPC handler
- `chat.html` 没有加载历史的代码

**解决方案**：
- `preload-chat.js` 添加 `getHistory`, `getSessionId`, `getPendingMessages`
- `main.js` 添加 `get-history` IPC handler
- `chat.html` 添加 `loadHistory()` 和滚动加载更多逻辑
- `pkg/session/store.go` 的 `GetRecentMessages` 返回正序（最旧在前）
- 新增 `GetMessagesBefore` 支持加载更早的消息

**消息顺序**：最旧在上，最新在下，滚动到顶部加载更多。

**修改文件**：
- `pkg/session/store.go`
- `main.go`
- `ui/assistant/preload-chat.js`
- `ui/assistant/main.js`
- `ui/assistant/chat.html`

---

## 相关文档

| 文档 | 说明 |
|------|------|
| `docs/implementation-L0L1L2.md` | L0/L1/L2 三级存储实现分析 |
| `docs/feature-file-management.md` | 文件管理设计 |
| `docs/feature-document-processing.md` | 文档处理设计 |
| `docs/feature-scheduled-tasks.md` | 定时任务设计 |
| `docs/feature-photo-processing.md` | 照片处理设计 |
| `docs/note-agent-communication.md` | Agent 通讯技术笔记 |
| `docs/spec-L1-summary.md` | L1 摘要层规范 |

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **ai-bot** (5742 symbols, 10058 relationships, 300 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> If any GitNexus tool warns the index is stale, run `npx gitnexus analyze` in terminal first.

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `gitnexus_impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `gitnexus_detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `gitnexus_query({query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `gitnexus_context({name: "symbolName"})`.

## When Debugging

1. `gitnexus_query({query: "<error or symptom>"})` — find execution flows related to the issue
2. `gitnexus_context({name: "<suspect function>"})` — see all callers, callees, and process participation
3. `READ gitnexus://repo/ai-bot/process/{processName}` — trace the full execution flow step by step
4. For regressions: `gitnexus_detect_changes({scope: "compare", base_ref: "main"})` — see what your branch changed

## When Refactoring

- **Renaming**: MUST use `gitnexus_rename({symbol_name: "old", new_name: "new", dry_run: true})` first. Review the preview — graph edits are safe, text_search edits need manual review. Then run with `dry_run: false`.
- **Extracting/Splitting**: MUST run `gitnexus_context({name: "target"})` to see all incoming/outgoing refs, then `gitnexus_impact({target: "target", direction: "upstream"})` to find all external callers before moving code.
- After any refactor: run `gitnexus_detect_changes({scope: "all"})` to verify only expected files changed.

## Never Do

- NEVER edit a function, class, or method without first running `gitnexus_impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `gitnexus_rename` which understands the call graph.
- NEVER commit changes without running `gitnexus_detect_changes()` to check affected scope.

## Tools Quick Reference

| Tool | When to use | Command |
|------|-------------|---------|
| `query` | Find code by concept | `gitnexus_query({query: "auth validation"})` |
| `context` | 360-degree view of one symbol | `gitnexus_context({name: "validateUser"})` |
| `impact` | Blast radius before editing | `gitnexus_impact({target: "X", direction: "upstream"})` |
| `detect_changes` | Pre-commit scope check | `gitnexus_detect_changes({scope: "staged"})` |
| `rename` | Safe multi-file rename | `gitnexus_rename({symbol_name: "old", new_name: "new", dry_run: true})` |
| `cypher` | Custom graph queries | `gitnexus_cypher({query: "MATCH ..."})` |

## Impact Risk Levels

| Depth | Meaning | Action |
|-------|---------|--------|
| d=1 | WILL BREAK — direct callers/importers | MUST update these |
| d=2 | LIKELY AFFECTED — indirect deps | Should test |
| d=3 | MAY NEED TESTING — transitive | Test if critical path |

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/ai-bot/context` | Codebase overview, check index freshness |
| `gitnexus://repo/ai-bot/clusters` | All functional areas |
| `gitnexus://repo/ai-bot/processes` | All execution flows |
| `gitnexus://repo/ai-bot/process/{name}` | Step-by-step execution trace |

## Self-Check Before Finishing

Before completing any code modification task, verify:
1. `gitnexus_impact` was run for all modified symbols
2. No HIGH/CRITICAL risk warnings were ignored
3. `gitnexus_detect_changes()` confirms changes match expected scope
4. All d=1 (WILL BREAK) dependents were updated

## Keeping the Index Fresh

After committing code changes, the GitNexus index becomes stale. Re-run analyze to update it:

```bash
npx gitnexus analyze
```

If the index previously included embeddings, preserve them by adding `--embeddings`:

```bash
npx gitnexus analyze --embeddings
```

To check whether embeddings exist, inspect `.gitnexus/meta.json` — the `stats.embeddings` field shows the count (0 means no embeddings). **Running analyze without `--embeddings` will delete any previously generated embeddings.**

> Claude Code users: A PostToolUse hook handles this automatically after `git commit` and `git merge`.

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
