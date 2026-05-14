# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概览

这是一个个人知识管理助手项目，采用 **Electron 前端 + Go 后端启动器 + Python Agent 核心 + 多个 MCP 服务器** 的混合架构。

**核心架构**：
```
用户界面 (Electron)
    ↓ HTTP/SSE
Go 启动器 (main.go)
    ↓ 启动 + 监控
Python API 服务 (niu_api/)
    ↓ 调用
Agent 核心 (agent/generic/)
    ↓ MCP 协议
MCP 服务器集群 (mcp-servers/)
```

## ⛔ 不可违反的铁律（每次对话必读）

1. **禁止自己改代码** — 所有代码修改必须委托给子Agent执行，主对话只做分析和决策
2. **修改前必须先做临时提交备份** — `git add -A && git commit`，恢复前也必须先备份当前状态，不能直接 `git checkout` 覆盖
3. **修改前必须用 gitnexus 分析影响范围** — 评估 blast radius 后再动手
4. **测试必须用真实数据+真实LLM** — 绕过LLM的测试是假测试

**违反任何一条就停下来，不要继续。**

## 工作原则：
```
1、修改代码必须经过用户同意，说清楚修改的原因
2、未经同意，不得覆盖仓库内任何备份
3、从仓库恢复代码时，先回忆上次备份的内容。不确定就不能盲目恢复
4、遍历仓库历史需要测试原历史代码时，先把当前代码做临时提交
5、代码调试过程中验证无效后，必须马上撤销调试代码，恢复原始干净代码，再增加新的调试代码
6、目前项目代码量比较大，为了保护自己的上下文窗口，无需长期记忆或大代码量的遍历工作交给子Agent完成
7、代码质量优先，用户不在乎token消耗
```

## 开发环境设置

### 前置要求

- **Go**: 1.26+ (用于启动器)
- **Python**: 3.11+ (用于 Agent 和 MCP 服务器)
- **Node.js**: 18+ (用于 Electron 前端)
- **SQLite**: 用于会话持久化

### 安装依赖

```bash
# Go 依赖
go mod download

# Python 依赖（Agent 核心）
cd agent
pip install -e .

# Python 依赖（各个 MCP 服务器）
cd mcp-servers/photo-server && pip install -e .
cd mcp-servers/lightrag-server && pip install -e .
cd mcp-servers/file-parser && pip install -e .
cd mcp-servers/config-manager && pip install -e .
cd mcp-servers/memory-server && pip install -e .
cd mcp-servers/session-manager && pip install -e .

# Electron 前端依赖
cd ui/assistant
npm install
```

### 运行项目

**完整启动**：
```bash
# 构建并运行（推荐）
go build -o niu.exe && ./niu.exe

# 或者直接运行
go run main.go
```

**单独启动前端**：
```bash
cd ui/assistant
npm start
```

**单独启动 Python API**：
```bash
python -m niu_api
# API 端口默认 9876，可通过环境变量 NIU_API_PORT 修改
```

### 测试

```bash
# Python Agent 测试
cd agent
pytest

# Go 测试
go test ./...
```

### 代码检查

```bash
# Python 代码检查（使用 ruff）
cd agent
ruff check .

# Python 自动格式化
ruff format .

# Go 代码检查
go fmt ./...
```

## 核心架构

### Agent 核心（agent/generic/）

**核心文件**：
- `agent_loop.py` — 主循环（99行）
- `handler.py` — 工具实现 + 工作记忆机制（526行）
- `llmcore.py` — LLM 抽象层，支持多厂商（835行）

**重要机制**：
1. **工作记忆**：`tool_after_callback` + `_get_anchor_prompt` + `next_prompt_patcher`
   - 每 35 轮强制询问用户
   - 每 7 轮警告避免无效重试
   - 保留最近 20 条工具调用摘要
2. **思考链处理**：统一处理 DeepSeek/MiniMax/Qwen/Claude/OpenAI o1 的思考链格式
3. **Token 统计**：`MockResponse.usage` 返回 input/output/total_tokens

**适配层**：
- `session_adapter.py` — Session 隔离 + SQLite 持久化
- `runner.py` — 整合层（GenericAgentRunner）+ 动态注入架构
- `vector_search.py` — 向量检索适配器
- `thinking_chain.py` — 思考链处理器
- `tool_registry.py` — MCP 工具注册中心（新架构核心）
- `mcp_loader.py` — MCP 模块加载器（新架构核心）
- `mcp_sync_bridge.py` — 同步/异步桥接（已废弃，保留向后兼容）

### MCP 服务器架构

#### MCP 同进程架构（In-Process Architecture）

**架构升级（2026-04）**：
- **旧架构**：MCP stdio 通信（进程隔离，性能低）
- **新架构**：同进程直接调用（无进程通信，性能提升 ~40000x）

**核心组件**：
1. **ToolRegistry** (`agent/tool_registry.py`):
   - 全局工具注册中心
   - 管理所有 MCP 工具的注册、获取和 schema 返回
   - 支持 `get_registry().get("server-name/tool-name")` 直接调用

2. **MCP Loader** (`agent/mcp_loader.py`):
   - 启动时加载所有必需的 MCP 模块
   - 严格验证：任何加载失败将终止应用
   - 支持自定义服务器列表

3. **TOOL_SCHEMAS 模式**：
   - 每个 MCP 服务器模块定义 `TOOL_SCHEMAS` 字典
   - 提供 `get_tool_schemas()` 函数返回 schema 列表
   - 工具函数直接在模块中实现

**性能对比**：
```
10 次工具调用：
- stdio 模式：~40 秒（进程启动 + JSON-RPC 序列化）
- 同进程模式：~0 秒（直接 Python 函数调用）
- 性能提升：~40000x
```

**使用示例**：
```python
from agent.tool_registry import get_registry

# 获取工具函数
registry = get_registry()
tool_fn = registry.get("memory-server/remember")

# 直接调用（无需 stdio 通信）
result = tool_fn(content="用户喜欢 Python", metadata={"type": "preference"})

# 获取 schema 列表（用于 LLM）
schemas = registry.get_schemas()
```

**迁移状态**（已完成）：
- ✅ 所有 8 个 MCP 服务器改造完成（photo-server + 7 个其他服务器）
- ✅ 52 个工具 schema 已添加
- ✅ Handler 使用 ToolRegistry 进行工具调用
- ✅ API 启动流程使用 `load_mcp_tools()`
- ✅ 集成测试通过

**废弃组件**（保留向后兼容）：
- `MCPSyncBridge` (`agent/mcp_sync_bridge.py`)：保留但不再使用
- `mcp_client.py` 的 stdio 通信函数：标记为废弃，建议使用 ToolRegistry

**注册规范**（所有 MCP 服务器必须遵守）：

1. **目录结构**：
   ```
   mcp-servers/<name>/
   ├── src/
   │   └── niu_<name>/
   │       ├── __init__.py      # MCP 工具定义
   │       └── __main__.py      # 入口点
   └── pyproject.toml
   ```

2. **配置要求**：
   - `workdir` 必须指向 `src/` 目录（自动加入 sys.path）
   - **不需要 `pip install`**，通过 workdir 即可找到模块
   - `python -m niu_xxx` 需要模块目录下有 `__main__.py`

3. **配置示例**（config/mcp-servers.yaml）：
   ```yaml
   server-name:
     command: ${PYTHON_PATH}  # 由 main.go 自动检测
     args:
       - "-m"
       - "niu_server_name"
     workdir: ../mcp-servers/server-name/src
     preload: true  # 可选，启动时预加载
   ```

**已实现的 MCP 服务器**：

| 服务器 | 功能 | 预加载 |
|--------|------|--------|
| `file-parser` | 文档解析（PDF/Word/PPT/Excel/MD/HTML） | ✅ |
| `lightrag-server` | 知识图谱 + 向量检索（LightRAG 统一管理） | ✅ |
| `photo-server` | 照片管理 + 人脸识别（InsightFace） | ✅ |
| `config-manager` | 配置管理（读/写用户配置和记忆） | ✅ |
| `memory-server` | 智能记忆提取和检索 | ✅ |
| `session-manager` | 会话管理（消息压缩） | ❌ |
| `browser-server` | 浏览器自动化（Playwright async_api + 守护线程） | ✅ |

**Browser-Server 架构**：
- `playwright.async_api` 在独立守护线程中运行（自有 asyncio loop）
- 主进程通过 `call_async()` 桥接同步调用与异步 Playwright
- `get_page()` 返回 `SyncPageProxy`：包装 async Page 方法为同步，code_run 代码无需改动
- `launch_persistent_context()` 返回 BrowserContext，用 `_context` 做状态判断（非 `_browser`）
- 不修改 pip 安装目录源码，Playwright 升级安全

### 子 Agent 架构

**定义位置**：`config/agents/*.md`

**调用方式**：主 Agent 通过 `chat-with-xxx` 工具调用子 Agent

**关键实现**：
- `agent/subagent.py` — 子 Agent 工具生成
- `agent/mcp_client.py` — `get_mcp_tools_for_servers()` 按 server 名称过滤工具

**委托规则**：文件处理等耗时任务必须委托给子 Agent（`file-processor`）

### 动态注入架构

**实现**：
- `agent/injector/sync.py` — Skills 定时扫描同步到向量库
- `niu_api/injector.py` — API 端点手动注册 MCP 工具描述
- `agent/runner.py` — `_inject_dynamic_resources()` 按语义搜索并注入
- `agent/runner.py` — `_on_turn_end()` 每轮结束后刷新动态注入（轮次级）
- `agent/tool_lifecycle.py` — 工具生命周期管理（衰减-覆盖评分模式）

**轮次级刷新机制**：
- `agent_runner_loop()` 每轮循环末尾调用 `on_turn_end` 回调
- 回调执行顺序：先衰减(`decay_tools` -10) → 再注入(`_inject_dynamic_resources` 向量检索覆盖分数)
- 向量检索到的 MCP 工具分数覆盖到 `tool_lifecycle`，实现"衰减-覆盖"模式

**衰减-覆盖评分模式**：
- 每轮开始：所有活跃工具 -10 分
- 向量检索命中：覆盖为新分数（`max(min_score, int(similarity * 100))`）
- 命中工具净效果 ≈ 0（-10 + 新分数），保持稳定
- 非命中工具持续 -10/轮，低于 min_score(50) 自动移除
- `hit_tool()` 不再强制设 100 分，改为接受可选 `score` 参数

**知识库标签**（LightRAG 统一管理）：
- `l1` — L1 摘要
- `l2` — L2 原文
- `skill` — Skills 文件
- `mcp_tool` — MCP 工具描述

### 同步/异步桥接（已废弃）

**问题**：GenericAgent 纯同步，MCP 客户端异步，FastAPI 异步端点，导致事件循环冲突。

**旧解决方案**（已废弃）：
1. `agent/mcp_sync_bridge.py` — 后台事件循环 + `run_coroutine_threadsafe`
2. `agent/handler.py` 的 `dispatch()` 使用 `MCPSyncBridge` 调用 MCP 工具
3. `niu_api/compat.py` 使用 `asyncio.to_thread` 运行同步 chat

**新解决方案**（推荐）：
- 使用 ToolRegistry 进行同进程直接调用
- 无需事件循环桥接，纯同步架构
- 性能提升 ~40000x

**迁移状态**：
- ✅ Handler 已改用 ToolRegistry
- ✅ API 启动流程已使用 `load_mcp_tools()`
- ⚠️ MCPSyncBridge 保留向后兼容，但不推荐使用

## 配置文件架构

### 程序目录 `config/`

| 文件 | 用途 |
|------|------|
| `config/user-config.json` | LLM API Key、模型选择 |
| `config/llm-presets.json` | LLM 预设列表 |
| `config/agents/niu.md` | 主 Agent 定义（提示词、权限、MCP服务器） |
| `config/agents/file-processor.md` | 子 Agent 定义（文件处理专用） |
| `config/mcp-servers.yaml` | MCP 服务器配置 |

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

## 关键技术点

### MCP 工具调用规范

**推荐：使用 ToolRegistry 同进程调用（新架构）**：
```python
# 推荐：通过 ToolRegistry 直接调用
from agent.tool_registry import get_registry

registry = get_registry()
tool_fn = registry.get("server-name/tool-name")
result = tool_fn(param1="value1", param2="value2")
```

**废弃：使用 stdio 通信（旧架构）**：
```python
# 已废弃：stdio 通信（性能低，不推荐）
result = await call_mcp_tool("server-name/tool-name", {"param1": "value1"})
```

**注意事项**：
- InsightFace/ONNX Runtime 在异步环境中可能导致问题，建议使用同步调用
- ToolRegistry 已经是同步架构，无需 `asyncio.to_thread`
- 所有新代码应使用 ToolRegistry，避免 stdio 通信

### 人脸识别模型管理

**内存管理**：
- InsightFace 模型加载后占用 ~326MB 内存
- 空闲 5 分钟自动卸载（`MODEL_IDLE_TIMEOUT_SECONDS = 300`）
- **不要在卸载时调用 `gc.collect()`**：可能导致崩溃

**预加载机制**：
```python
# 在 MCP stdio 启动前预加载 cv2 和 InsightFace 模块代码
preload_face_model()
```

### 历史对话管理

**消息顺序**：最旧在上，最新在下，滚动到顶部加载更多。

**API**：
- `getHistory(limit, before_id)` — 获取历史消息
- `getMessagesBefore(message_id, limit)` — 加载更早的消息

### 上下文窗口管理

**配置**（`~/.niu/preferences.json`）：
```json
{
  "context": {
    "warningThreshold": 0.80,
    "targetThreshold": 0.50,
    "sleepTriggerMinutes": 5,
    "contextWindowSize": 200000
  }
}
```

### Electron 窗口管理

**关闭流程**：
1. Electron 窗口关闭 → 触发 `close-all` 事件
2. 调用 `/api/shutdown` 通知 Python API
3. Python API 清理资源
4. Go 启动器终止所有子进程

**修改文件**：`main.go` + `ui/assistant/main.js`

## 常见问题

### 照片拖入卡死

**原因**：`asyncio.to_thread` + InsightFace/ONNX Runtime 在 MCP stdio 环境中存在兼容性问题。

**解决方案**：
1. 将 MCP 工具调用改为同步
2. 添加 `preload_face_model()` 在 MCP stdio 启动前预加载

### 主 Agent 工具丢失

**检查点**：
- `config/agents/niu.md` 的 `mcpServers` 列表是否完整
- MCP 服务器配置是否正确（`workdir` 指向 `src/`）

### 子 Agent 缺少 MCP 工具

**检查**：`agent/subagent.py` 的 `get_subagent_mcp_tools_schema()` 是否根据 `mcpServers` 配置获取工具。

### 历史对话丢失

**检查**：`niu_api/session.py` 的 API 调用参数是否正确，避免将 `session_id` 当作 `limit` 参数传入。

### 记忆无法保存或检索

**检查**：
1. LightRAG 是否初始化：检查日志中是否有 "LightRAG initialized" 
2. Memory Server 是否正常：`python scripts/test_memory_server.py`
3. 日志中是否有错误：`tail -f logs/api_stderr.log | grep "记忆|MEMORY|LightRAG"`

**解决**：
- 检查 LightRAG 工作目录：`~/.niu/lightrag/`
- 检查数据库路径：`~/.niu/memory.json` 中的 `workspace.path`

### MCP stdio 通信错误（旧架构问题）

**症状**：日志中出现大量 "Failed to parse JSONRPC message"

**原因**：ONNX Runtime 将调试信息输出到 stdout，污染了 MCP 协议

**解决方案**（已废弃，建议升级到新架构）：
- ~~已在 `mcp-servers/photo-server/src/niu_photo_server/__init__.py` 中修复，临时抑制 stdout~~
- **推荐**：升级到 MCP 同进程架构，无需 stdio 通信

**新架构优势**：
- 无 stdio 通信，无 JSON-RPC 序列化开销
- 无 stdout 污染问题
- 性能提升 ~40000x

## 相关文档

- `AGENTS.md` — 项目知识库（包含详细更新日志）
- `docs/feature-photo-processing.md` — 照片处理设计
- `docs/feature-file-management.md` — 文件管理设计
- `docs/feature-document-processing.md` — 文档处理设计
- `docs/feature-scheduled-tasks.md` — 定时任务设计
- `docs/note-agent-communication.md` — Agent 通讯技术笔记
- `docs/implementation-L0L1L2.md` — L0/L1/L2 三级存储实现分析
- **`docs/design-self-evolution-system.md` — 自我进化系统设计规范**
- **`docs/USAGE-self-evolution.md` — 自我进化系统使用指南**
- `docs/analysis-genericagent-evolution.md` — GenericAgent 进化机制分析

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **niu-agent** (16017 symbols, 25662 relationships, 300 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> If any GitNexus tool warns the index is stale, run `npx gitnexus analyze` in terminal first.

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `gitnexus_impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `gitnexus_detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `gitnexus_query({query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `gitnexus_context({name: "symbolName"})`.

## Never Do

- NEVER edit a function, class, or method without first running `gitnexus_impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `gitnexus_rename` which understands the call graph.
- NEVER commit changes without running `gitnexus_detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/niu-agent/context` | Codebase overview, check index freshness |
| `gitnexus://repo/niu-agent/clusters` | All functional areas |
| `gitnexus://repo/niu-agent/processes` | All execution flows |
| `gitnexus://repo/niu-agent/process/{name}` | Step-by-step execution trace |

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
