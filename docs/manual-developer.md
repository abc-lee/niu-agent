# 开发者参考手册

> 本文档从 SYSTEM_MANUAL.md 拆分而来，包含开发者指南、附录和更新日志。
> 如需系统概述和架构信息，请参阅 [SYSTEM_MANUAL.md](SYSTEM_MANUAL.md)。

## 一、开发者指南

### 1.1 本地开发

**环境要求：**
```
- Python 3.11+
- Rust toolchain（见 launcher/Cargo.toml）
- SQLite
```

> 前端已集成在 Rust 启动器的 GUI 中，无需单独安装 Node.js / Electron 环境。

**主要目录结构：**
```
agent/              # Agent 核心（agent_loop / handler / llmcore 等）
niu_api/            # Python API 服务（FastAPI）
launcher/           # Rust 启动器（clap + GUI 集成）
mcp-servers/        # MCP 服务器集群（同进程架构，ToolRegistry 加载）
im-adapters/        # IM Gateway 适配器（飞书等）
ui/                 # 前端界面（assistant / settings / graph）
config/             # 配置文件（user-config / llm-presets / agents / mcp-servers.yaml）
models/             # 本地模型（bge-base-zh-v1.5 / buffalo_l）
```

**启动开发环境：**

```bash
# 1. 安装依赖
pip install -r requirements.txt
cd agent && pip install -e .
cd ../mcp-servers/photo-server && pip install -e .
cd ../mcp-servers/lightrag-server && pip install -e .
# ... 安装其他 MCP 服务器（config-manager, memory-server, file-parser 等）
# 注意：kg-server 和 vector-store 已废弃，由 lightrag-server 替代

# 2. 启动 API
python -m niu_api

# 3. 使用 Rust 启动器（自动启动 API + GUI）
cd launcher && cargo run
# 或直接运行编译好的二进制
./niu
```

**Rust 启动器启动流程：**
1. 检测 Python 路径（Windows: `python/Scripts/python.exe` 等；Mac/Linux: `~/.niu-venv/bin/python3` 等）
2. 从 `~/.niu/memory.json` 加载 workspace.path，设为 `WORKSPACE_PATH` 环境变量
3. 启动 Python API（`python -m niu_api`），传入 `NIU_API_PORT`、`PYTHONUNBUFFERED`、`LITELLM_LOCAL_MODEL_COST_MAP`、`LITELLM_NO_AIOHTTP_TRANSPORT`、`WORKSPACE_PATH`
4. 等待 `/health` 返回 200（最多 30 秒）
5. 等待 `/api/preload-status` 返回 `ready=true`（最多 60 秒）
6. 根据 `--settings` 或 `--graph` 标志启动对应窗口，否则启动 assistant 窗口
7. 监控 GUI 窗口退出，触发关闭流程：POST `/api/shutdown` → Kill API 进程

### 1.2 调试技巧

**查看日志：**
```bash
# LLM 交互日志（每日轮转）
tail -f logs/llm_interaction_*.log

# raw_http 两层日志架构（由 /llm/v1/* HTTP 端点暴露）
# - transport 层：logs/raw_http/{YYYYMMDD}/{seq:06d}.json（记录 HTTP 请求）
# - 应用层：{seq:06d}_request.json / {seq:06d}_response.json（记录 LLM 流式响应）
ls logs/raw_http/

# API 日志（Rust 启动器通过 Pipe 捕获 stdout/stderr，无独立文件）
# 直接运行 python -m niu_api 可在控制台看到输出
```

**测试 MCP 工具（同进程架构）：**
```python
# 列出所有 MCP 工具 schema（通过 ToolRegistry）
from agent.tool_registry import get_registry
registry = get_registry()
for name in registry.list_tools():
    print(name)
```

```bash
# 列出注入的 skill 资源（注意：disk mode 下不返回 MCP 工具，只返回 skill）
curl http://127.0.0.1:9876/api/inject/resources

# 同进程调用 MCP 工具（不通过 HTTP，使用 ToolRegistry 直接调用）
# 代码示例：
from agent.tool_registry import get_registry
registry = get_registry()
tool_fn = registry.get("photo-server/ingest_photo")
result = tool_fn(photo_path="test.jpg")
```

**数据库调试：**
```bash
# 查看消息历史（路径：~/.niu/messages.db）
sqlite3 ~/.niu/messages.db "SELECT id, role, content, created_at FROM messages ORDER BY created_at DESC LIMIT 10;"

# 查看定时任务（路径：~/.niu/scheduled_tasks.db 或 <workspace>/scheduled_tasks.db）
sqlite3 ~/.niu/scheduled_tasks.db "SELECT * FROM scheduled_tasks;"

# 查看 LightRAG 存储状态
ls ~/.niu/lightrag_storage/
```

### 1.3 贡献代码

**代码风格：**
```
Python: ruff format + ruff check
Rust: cargo fmt + cargo clippy
```

**提交规范：**
```
feat: 新功能
fix: 修复 bug
docs: 文档更新
refactor: 重构
test: 测试
```

**Pull Request 流程：**
```
1. Fork 仓库
2. 创建分支：git checkout -b feature/xxx
3. 提交代码：git commit -m "feat: xxx"
4. 推送分支：git push origin feature/xxx
5. 创建 Pull Request
```

---

## 二、附录

### 2.1 命令行参数

```bash
niu [选项]

选项：
  --port=9876       API 端口（默认 9876）
  --settings        打开设置窗口（ui/main/windows/settings）
  --graph           打开知识图谱窗口（ui/main/windows/graph）
  --config=path     配置目录路径（默认 ./config，保留兼容）
```

注意：Rust 启动器使用 `clap` derive 宏解析参数，默认支持 `--help`。参数定义见 `launcher/src/main.rs` 中的 `Args` 结构体。

### 2.2 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `NIU_API_PORT` | Python API 端口 | `9876` |
| `NIU_MODELS_PATH` | 模型文件目录（覆盖默认 `{项目根}/models`） | 项目根目录下 `models/` |
| `WORKSPACE_PATH` | 工作空间根目录（定时任务等数据存储位置） | 从 `~/.niu/memory.json` 的 `workspace.path` 读取 |
| `LITELLM_LOCAL_MODEL_COST_MAP` | LiteLLM 本地模型费用映射开关 | Rust 启动器设为 `True` |
| `LITELLM_NO_AIOHTTP_TRANSPORT` | 禁用 LiteLLM 的 aiohttp transport（避免异步兼容问题） | Rust 启动器设为 `True` |
| `CUDA_VISIBLE_DEVICES` | GPU 设备选择 | 所有可用 GPU |
| `PYTHONUNBUFFERED` | Python 输出无缓冲 | Rust 启动器设为 `1` |

### 2.3 API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/chat` | POST | 主对话接口（SSE 流式） |
| `/chat/sync` | POST | 同步对话端点（遗留——无生产调用方，仅测试脚本使用；Electron 入口语义：_chat_lock 内清 IM 通道标志——set_im_channel("") + set_im_force(False)，与 /chat 一致） |
| `/chat/session/{session_id}` | DELETE | 删除指定会话 |
| `/api/events/stream` | GET | SSE 事件流（新消息推送） |
| `/api/context/messages` | GET | 获取上下文消息（含分页） |
| `/api/context/messages/delete` | POST | 按 ID 删除消息 |
| `/api/context/messages/update` | POST | 更新单条消息内容 |
| `/api/context/messages/add` | POST | 添加消息 |
| `/api/context/tidy` | POST | 上下文整理（sleep/force 模式）。整理管道全局一次一个排队（单 worker 队列）：`mode:'sleep'` → 投递 + 立即返回 `{"status":"queued"}`（后端继续排队执行）；`mode:'force'` → 投递 + await 队列执行完成（前端直接 await，无整体超时——解锁真源是后端 try/finally 必释放 `_tidy_lock`，子 Agent 有 LLM read_timeout 保底必收敛）。原 force 压缩前置游标追平门控（`_cursors_caught_up`）已于 2026-08-24 工程四移除——睡眠重排为压缩在前、提炼在后且提炼文件驱动，门控失去意义，不再返回 skipped |
| `/api/spirit-state` | POST | 精灵状态同步。body `{"state": str}`（如 `"sleep"`/`"idle"`，小写归一）；后端据此刻 `is_sleeping()` 判定 sleep 管道 CP0-CP3 状态机（睡眠整理可被唤醒打断，force 不检查） |
| `/api/chat/clear` | POST | 即时清空当前会话。流程：无条件唤醒睡眠整理管道（`set_spirit_state("idle")`）+ `request_stop()` 停主 Agent → 无限心跳排队拿 `_chat_lock`（60s 一跳，永不超时拒绝）→ `clear_messages()` 清空 + `cleanup_all_tmp()` + 复位全部游标 + 清理挂起同步子 Agent（`cleanup_suspended_sync_subagents`，STOPPED 语义）。**已移除 force_tidy 提炼通道**（用户拍板"取消清空前提炼"）：请求 body 的 `force_tidy` 字段被忽略，/clear 不再先跑整理 |
| `/api/chat/session` | POST | 同步对话（兼容旧 UI） |
| `/api/shutdown` | POST | 关闭服务 |
| `/api/preload-status` | GET | 预加载状态（Rust 启动器用） |
| `/api/llm-status` | GET | LLM 配置可用状态 |
| `/api/test-llm` | POST | 测试 LLM 配置连通性 |
| `/api/stats` | GET | 系统统计（消息数、运行时间） |
| `/api/pending-alerts` | GET | 获取待处理提醒 |
| `/api/alerts` | POST | 提交提醒（写入 pending 队列） |
| `/api/vector/stats` | GET | LightRAG 知识库统计 |
| `/api/vector/cleanup` | POST | 清理向量库失效条目 |
| `/api/inject/mcp-tool` | POST | 注册单个 MCP 工具 |
| `/api/inject/mcp-tools/batch` | POST | 批量注册 MCP 工具 |
| `/api/inject/resources` | GET | 列出注入资源 |
| `/api/inject/skills/sync` | POST | 触发 Skills 同步 |
| `/api/kg/*` | 多种 | 知识图谱端点 |
| `/api/brain/*` | 多种 | 脑图端点（remember/recall/status） |
| `/api/brain/regions/*` | 多种 | 脑区端点 |
| `/api/notes/*` | 多种 | 笔记端点 |
| `/api/llm-log/*` | GET | raw_http 日志查询（http_log_router） |
| `/llm/v1/models` | GET | 可用模型列表 |
| `/llm/v1/health` | GET | LLM 配置检查 |
| `/llm/v1/status` | GET | LightRAG 和模型状态 |
| `/scheduler/tasks` | GET/POST/PUT/DELETE | 定时任务管理 |
| `/health` | GET | 健康检查 |
| `/api/subagents/{unique_name}/stream` | GET | 子 Agent 独立 SSE 端点 |
| `/api/subagents/running` | GET | 在跑子 Agent 列表（窗口恢复时用） |
| `/api/subagents/{unique_name}/message` | POST | 用户向子 Agent 发消息/回答 @user 提问 |
| `/api/stop_all` | POST | 停止所有**用户对话派生的**子 Agent（source=user；程序触发/定时任务派生的跳过），置 terminate_event 可穿透 LLM 阻塞，立即返回 |

**SSE 事件类型清单**（`/api/events/stream` 推送，定义于 `niu_api/chat.py` 与 `agent/generic/agent_loop.py`）：

| 事件 type | 触发位置 | 说明 |
|-----------|----------|------|
| `new_message` | `chat.py` notify_new_message | 新消息入库通知（role/message_id/content） |
| `tool_status` | `chat.py` notify_tool_status_sync | 工具调用开始/结束状态（tool_name/status/summary） |
| `ingest` | `chat.py` push_ingest_result | 文件入库异常通知（role=system） |
| `chat_busy` | `agent_loop.py` StreamEvent("system", "chat_busy") | Agent 开始处理，进入忙碌状态 |
| `chat_idle` | `agent_loop.py` StreamEvent("system", "chat_idle") | Agent 处理完成，进入空闲状态 |
| `persist` | `agent_loop.py` StreamEvent("persist", ...) | V4 逐轮持久化推送（assistant/tool 消息逐条 yield）；chat.py persist_agent_reply 兜底路径带前缀去重（停止/异常场景 rv=None 时与已入库 assistant 内容比对，防重复写入 messages.db） |
| `subagent_started` | `handler.py` subagent_started 推送 | 子 Agent 启动通知（unique_name/agent_name/agent_type），前端创建 tab + 建立独立 SSE。子 Agent 详细事件（reply/tool_status/thinking_chain/question/subagent_suspended/subagent_error/subagent_closed）走独立 SSE 端点 `/api/subagents/{unique_name}/stream`，详见子 Agent 分册 |

### 2.4 许可证

```
Niu 个人知识助理
Copyright (c) 2026

本软件供个人学习和研究使用。
商业使用请联系开发者获取授权。

第三方库许可：
- InsightFace: MIT License (非商业)
- Sentence Transformers: Apache 2.0
- ONNX Runtime: MIT License
- FastAPI: MIT License
- LightRAG: MIT License
```

---

## 三、更新日志

### v0.6.0 (2026-06-30)

**重大变更：**
- MCP 同进程架构（ToolRegistry）：MCP 工具由 stdio 通信改为同进程直接调用，性能提升约 40000x
- Go → Rust 启动器迁移：launcher/ 改为 Rust + clap 实现，前端 GUI 集成在 Rust 启动器中
- Electron → Iced 迁移（部分）：splash/启动画面迁移至 Iced GPU GUI，主交互界面仍基于 Electron + Rust 启动器
- skill 三级降级机制：active → deprecated → `.trash/`，自动归档失效 skill
- 睡眠触发修复：preload.js 注入 `IDLE_TIMEOUT`，spirit.html 通过 electronAPI 读取
- requirements 清理：删除 14 个冗余依赖包

### v0.5.0 (2026-04-30)

**重大变更：**
- KG 实体架构重构：人物实体只存名字，文档入库全自动
- LightRAG 替代向量库作为主要知识检索引擎
- 脑图系统（Brain Graph）：记忆存取（store/recall via LightRAG）
- 脑区（Brain Region）：社区检测 + 区域节点刷新
- 上下文整理管道升级：entity-extractor + dream-evolver + context-manager + journal-agent 四游标机制 + 小憩模式主动触发（entity-extractor → dream-evolver）
- 子 Agent 新增：entity-extractor、dream-evolver

### v0.4.0 (2026-04-09)

**重大变更：**
- 新增交互习惯库（Interaction Habits）

**交互习惯库（Interaction Habits）：**
- 三类内容：工具方言、用户状态、用户画像
- 置信度机制：success_count/fail_count，自动删除低置信度记录（fail_count >= 3）
- context-manager 梦境整理时学习个性化内容
- 主 Agent 可读取和应用 Interaction Habits
- 工具调用成功后自动更新对应 dialect 的置信度

### v0.3.0 (2026-04-09)

**重大变更：**
- 新增向量库系统文档（第三章）（已废弃，由 LightRAG 替代）
- L1规范统一（spec-L1-summary.md）
- 递归查询机制文档（design-vector-recursive-query.md）（已废弃）
- 新增向量库故障排查（5.4节）（已废弃，由 LightRAG 故障排查替代）

**向量库系统：**（已废弃，由 LightRAG 替代）
- 4类文档：mcp_tool, query_pattern, skill, document
- 统一metadata结构：level, category, language
- L2归一化（标准行为）
- 递归查询机制（is_recursive标志）

**辅助脚本：**（服务于已废弃的向量库系统）
- `export_all_mcp_tools.py` - 导出工具到JSON
- `register_all_mcp_tools_from_json.py` - 从JSON注册
- `check_mcp_tools_in_db.py` - 检查向量库状态（已删除）

### v0.2.0 (2026-04-06)

**重大变更：**
- 单进程架构：整合 embedding 和 scheduler 到主进程
- GPU 自动检测：自动选择 CUDA/CPU
- 依赖打包：所有依赖预下载，无网络要求

**新增功能：**
- 动态技能系统（watchdog 监控）
- 定时任务优化（延迟启动避免时序问题）
- 完整的系统说明书

**修复问题：**
- 移除重复日志
- 修复依赖声明缺失
- 优化启动速度

**已知问题：**
- macOS/Linux 版本未测试
- 多用户支持未实现