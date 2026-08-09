# AGENTS.md — Niu (niu-agent) 权威项目指南

> **本文件是项目的权威指南，每次会话必读。** 记录架构、开发规范与历史更新日志。

核心架构：**Electron 33 前端 + Rust 启动器 + Python Agent 核心 + 多个 MCP 服务器** 的混合架构。

**核心特色**：MCP 虚拟磁盘 — 把 100+ MCP 工具的 Schema 映射为 Unix 风格虚拟文件系统，Agent 用单一 `disk()` 工具以 `ls`/`cat`/路径调用的方式使用所有工具，彻底解决 MCP 工具爆炸导致的上下文占用问题。

**核心架构**：
```
用户界面 (Electron 33)
    ↓ HTTP/SSE
Rust 启动器 (launcher/)  ← Iced 仅用于 Splash 启动画面
    ↓ 启动 + 监控
Python API 服务 (niu_api/)
    ↓ 调用
Agent 核心 (agent/generic/)
    ↓ MCP 协议
MCP 服务器集群 (mcp-servers/)
```

---

## ⛔ 不可违反的铁律（每次对话必读）

1. **你是项目经理** — 不要自己遍历代码，把控全局，减少无价值上下文占用。
2. **禁止自己改代码** — 所有代码修改必须委托给子 Agent 执行，主对话只做分析和决策。
3. **修改前必须先做临时提交备份** — `git add -A && git commit`，恢复前也必须先备份当前状态，不能直接 `git checkout` 覆盖；完整回退到过去的某点必须经过用户同意。
4. **修改前必须用 gitnexus 分析影响范围** — 评估 blast radius 后再动手。
5. **测试必须用真实数据 + 真实 LLM** — 绕过 LLM 的测试是假测试。
6. **`python/` 目录必须是完整的自包含 Python 安装** — 所有二进制、库、依赖必须真实存在于 `python/` 目录内，禁止符号链接指向外部路径（如 `/Library/Frameworks/Python.framework/`）。此目录最终要打包分发，客户无需自装 Python 环境和依赖。当前 `python/` 目录的 stdlib 仍指向系统 Python（自包含 stdlib 尚未复制进来），需另开会话重建。numpy<2 和 opencv<4.12 是隐性约束（torch 2.2.2 / insightface C 扩展用 numpy 1.x ABI；opencv 4.12+ 强制 numpy>=2）。
7. **git 操作后必须修复文件权限** — `git checkout/reset` 会丢失可执行权限，执行后必须运行：
   ```bash
   find python/bin/ -type f -exec grep -l '^#!' {} \; | xargs chmod +x
   find ui/*/node_modules/.bin/ -type f ! -perm -u+x -exec chmod +x {} \;
   ```
8. **Rust 启动器编译必须用 `launcher/build.sh`，禁止直接 `cargo build`** — `cargo build` 只输出到 `launcher/target/debug/`，不会复制到项目根目录的 `niu`，导致测试用旧二进制。`launcher/build.sh` 编译后自动 `cp target/release/niu-launcher ../niu`。每次改 Rust 代码（`launcher/src/`）后必须跑 `./launcher/build.sh`。此铁律必须传达给派出去的子 Agent。

**违反任何一条就停下来，不要继续。**

---

## 工作原则

1. 修改代码必须经过用户同意，说清楚修改的原因。
2. 未经同意，不得覆盖仓库内任何备份。
3. 从仓库恢复代码时，先回忆上次备份的内容；不确定就不能盲目恢复。
4. 遍历仓库历史测试原历史代码时，先把当前代码做临时提交。
5. 代码调试过程中验证无效后，必须马上撤销调试代码，恢复原始干净代码，再增加新的调试代码。
6. 项目代码量较大，为保护上下文窗口，无需长期记忆或大代码量的遍历工作交给子 Agent。
7. 代码质量优先，用户不在乎 token 消耗。
8. 版本号变更必须同步两处：根目录 `VERSION` 文件（单一真相源，对外发布用）、`ui/main/windows/assistant/chat.html` 中 `version-label` span 的文本（UI 展示用）。其他文件（Cargo.toml、package.json、Python `__version__`、pyproject.toml 等）的 version 字段是各子包的开发版本号，与产品版本号语义不同，**不要**强行统一。

---

## 开发环境设置

### 前置要求

- **Go**: 不再使用（历史遗留的 `main.go` / `pkg/` 已移除或废弃）。
- **Rust**: 用于启动器（`launcher/`，含 Iced Splash 启动画面）。
- **Node.js**: 用于 Electron 前端（`ui/main/`），建议 LTS。
- **Python**: 3.11+（Agent 和 MCP 服务器）。
- **SQLite**: 会话持久化。

### 安装依赖

两套依赖必须都装：
1. **Python 依赖**（Agent 核心 + MCP 服务器）→ `python/` 自包含环境
2. **Electron 前端依赖**（`ui/main/node_modules`）→ `npm install`

```bash
# 1. 创建自包含 Python 运行时（venv + 全量依赖）
# macOS
python3.11 -m venv --copies python
python/bin/pip install --upgrade pip
python/bin/pip install -r requirements.txt

# Windows（用完整路径指定 Python 3.11）
C:\Python311\python.exe -m venv --copies python
python\Scripts\pip.exe install --upgrade pip
python\Scripts\pip.exe install -r requirements.txt

# 2. 安装 Electron 前端依赖
cd ui/main && npm install && cd ../..

# 3. 开发/测试依赖（可选，不进入分发包）
# macOS: python/bin/pip install -r requirements-dev.txt
# Windows: python\Scripts\pip.exe install -r requirements-dev.txt
```

MCP 服务器不需要 `pip install`，通过 `config/mcp-servers.yaml` 的 `workdir` 配置即可加载模块。
Rust 启动器在 Windows 上通过 `cmd /C npm start` 拉起 Electron，`node_modules` 不存在会导致设置窗口无法弹出、启动器直接退出。

### 运行项目

**完整启动**：
```bash
./niu   # 直接运行编译好的二进制
```

**单独启动前端**：
```bash
cd ui/main && npm start   # 前端是独立 Electron 进程，由 Rust 启动器自动拉起
```

**单独启动 Python API**：
```bash
python -m niu_api
# API 端口默认 9876，可通过环境变量 NIU_API_PORT 修改
```

### 打包发布

**macOS .app + DMG 打包**（由 `launcher/build.sh` 自动完成）：
```bash
./launcher/build.sh          # 只打 .app bundle（开发调试用）
./launcher/build.sh --dmg    # 打 .app bundle + DMG 安装包（发布用）
```

`build.sh` 会：cargo build → 构造 `niu.app/`（复制资源 + 签名 + LaunchServices 注册 + quarantine）→ 可选生成 DMG（`dist/Niu-${VERSION}-mac-intel.dmg`）。

**关键约束**：
- 必须用 `launcher/build.sh`，禁止直接 `cargo build`（铁律 8）。
- 重打 DMG 前必须先 `rm -rf niu.app`——rsync `--delete --exclude` 会保护被 exclude 的旧文件不删除（许可证合规排除的 igraph/buffalo_l onnx/字体 ttf 等），删掉重打才干净。
- DMG 产物在 `dist/Niu-<VERSION>-mac-intel.dmg`，VERSION 从根目录 `VERSION` 文件读。
- M 系列 Mac 打包：必须在 arm64 host 上 `pip install` / `npm install`（不能 cross-compile），详见 `docs/manual-installation.md`。

**DMG 生成流程**（build.sh 内部）：
1. 准备临时目录 `/tmp/niu_dmg_stage_<pid>/`
2. 软链 `Applications`（支持拖拽安装）
3. 复制 `niu.app` 到临时目录
4. `hdiutil create -format UDZO -imagekey zlib-level=9` 生成 DMG（zlib 压缩，~3.3G bundle → ~1.2G DMG）
5. 清理临时目录

**许可证合规排除**（build.sh 的 rsync exclude）：
- `python/` 排除 igraph/leidenalg/texttable（GPL）
- `models/` 排除 `buffalo_l/*.onnx`（非商业许可，首次用自动下载到 `~/.insightface/`）
- `ui/main/` 排除阿朱泡泡体 ttf（许可证存疑）

**Windows 绿色包打包**（由 `pack.bat` 完成）：
```cmd
pack.bat
```
Windows 是绿色安装，用户解压 7z 即用，无需安装程序。前置：已安装 [7-Zip](https://7-zip.org/)（`C:\Program Files\7-Zip\7z.exe`）。打包前需已完成：Rust 编译（`launcher/build.sh` 或 `cargo build --release` + 复制 `niu-launcher.exe` 到根目录 `niu.exe`）、`npm install`、Python venv 创建。

`pack.bat` 会：
1. 自动清理 `launcher/target/`、`__pycache__/`、`*.pyc`（不进 7z，也不需要保留）
2. 用 robocopy 复制文件到临时目录，排除 `.git/`、`backup/`、缓存目录等
3. 用 7-Zip 压缩（LZMA2 -mx=9，压缩率高于 zip）
4. 产物在 `dist/Niu-<VERSION>-win-x64.7z`，VERSION 从根目录 `VERSION` 文件读

### 测试

```bash
cd agent && pytest
```

### 代码检查

```bash
cd agent
ruff check .      # Python 代码检查
ruff format .     # Python 自动格式化
```

---

## 核心架构

### Agent 核心（`agent/generic/`）

**核心文件**：
- `agent_loop.py` — 主循环 + V4 逐轮 persist 推送 + chat_busy/chat_idle 状态机
- `handler.py` — 工具实现 + 工作记忆机制
- `llmcore.py` — LLM 抽象层，支持多厂商

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
- **旧架构**：MCP stdio 通信（进程隔离，性能低）。
- **新架构**：同进程直接调用（无进程通信，性能提升 ~40000x）。

**核心组件**：
1. **ToolRegistry**（`agent/tool_registry.py`）：全局工具注册中心，管理所有 MCP 工具的注册、获取和 schema 返回，支持 `get_registry().get("server-name/tool-name")` 直接调用。
2. **MCP Loader**（`agent/mcp_loader.py`）：启动时加载所有必需的 MCP 模块，严格验证（任何加载失败将终止应用），支持自定义服务器列表。
3. **TOOL_SCHEMAS 模式**：每个 MCP 服务器模块定义 `TOOL_SCHEMAS` 字典，提供 `get_tool_schemas()` 函数返回 schema 列表，工具函数直接在模块中实现。

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

registry = get_registry()
tool_fn = registry.get("memory-server/user_memory_remember")
result = tool_fn(content="用户喜欢 Python", type="memory")  # 直接调用，无需 stdio
schemas = registry.get_schemas()
```

**废弃组件**（保留向后兼容）：
- `MCPSyncBridge`（`agent/mcp_sync_bridge.py`）：保留但不再使用。
- `mcp_client.py` 的 stdio 通信函数：标记为废弃，建议使用 ToolRegistry。

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
   - `workdir` 必须指向 `src/` 目录（自动加入 sys.path）。
   - **不需要 `pip install`**，通过 workdir 即可找到模块。
   - `python -m niu_xxx` 需要模块目录下有 `__main__.py`。

3. **配置示例**（`config/mcp-servers.yaml`）：
   ```yaml
   server-name:
     command: ${PYTHON_PATH}  # 由 launcher/src/main.rs 的 detect_python() 自动检测
     args:
       - "-m"
       - "niu_server_name"
     workdir: ../mcp-servers/server-name/src
     preload: true  # 可选，启动时预加载
   ```

4. **pyproject.toml 模板**：
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

**已实现的 MCP 服务器**：

| 服务器 | 功能 | 预加载 |
|--------|------|--------|
| `file-parser` | 文档解析（PDF/Word/PPT/Excel/MD/HTML） | ✅ |
| `lightrag-server` | 知识图谱 + 向量检索（LightRAG 统一管理） | ✅ |
| `photo-server` | 照片管理 + 人脸识别（InsightFace） | ✅ |
| `config-manager` | 配置管理（读/写用户配置和记忆） | ✅ |
| `memory-server` | 用户长期记忆和工作便签（permanent array 10 条） | ✅ |
| `session-manager` | 会话管理（消息压缩） | ❌ |
| `browser-server` | 浏览器自动化（WebSocket Bridge + 系统 Chrome，CDP 协议） | ✅ |
| `brain-region-server` | 脑区激活/调暗/状态管理 | ✅ |
| `scheduler-server` | 定时任务调度（增删改查） | ❌ |
| `feishu-server` | 飞书消息收发（可选） | ❌ |

**Browser-Server 架构**：
- 旧架构（已废弃）：`playwright.async_api` 守护线程模式，playwright 库已从依赖中移除。
- 新架构：WebSocket Bridge + 系统 Chrome（CDP 协议）。
- 核心文件：
  - `mcp-servers/browser-server/src/niu_browser_server/launcher.py` — 启动系统 Chrome（带 remote-debugging-port）
  - `mcp-servers/browser-server/src/niu_browser_server/ws_bridge.py` — WebSocket 桥接 CDP 命令
- 浏览器扩展：`extensions/niu-browser-ext/`（基于 alibaba/page-agent 二次开发），负责页面 DOM 提取和用户交互。
- 优势：不再需要内嵌浏览器，复用用户系统 Chrome（含登录态、插件）。

### 子 Agent 架构

**定义位置**：`config/agents/*.md`

**调用方式**：主 Agent 通过 `chat-with-xxx` 工具调用子 Agent。

**关键实现**：
- `agent/subagent.py` — 子 Agent 工具生成
- `agent/mcp_client.py` — `get_mcp_tools_for_servers()` 按 server 名称过滤工具

**委托规则**：文件处理等耗时任务必须委托给子 Agent（`file-processor`）。

### 动态注入架构

**实现**：
- `agent/injector/sync.py` — Skills 定时扫描同步到向量库
- `niu_api/injector.py` — API 端点手动注册 MCP 工具描述
- `agent/runner.py` — `_inject_dynamic_resources()` 按语义搜索并注入
- `agent/runner.py` — `_on_turn_end()` 每轮结束后刷新动态注入（轮次级）
- `agent/tool_lifecycle.py` — 工具生命周期管理（衰减-覆盖评分模式）

**轮次级刷新机制**：
- `agent_runner_loop()` 每轮循环末尾调用 `on_turn_end` 回调。
- 回调执行顺序：先衰减（`decay_tools` -10）→ 再注入（`_inject_dynamic_resources` 向量检索覆盖分数）。
- 向量检索到的 MCP 工具分数覆盖到 `tool_lifecycle`，实现"衰减-覆盖"模式。

**衰减-覆盖评分模式**：
- 每轮开始：所有活跃工具 -10 分。
- 向量检索命中：覆盖为新分数（`max(min_score, int(similarity * 100))`）。
- 命中工具净效果 ≈ 0（-10 + 新分数），保持稳定。
- 非命中工具持续 -10/轮，低于 min_score(50) 自动移除。
- `hit_tool()` 不再强制设 100 分，改为接受可选 `score` 参数。

**知识库标签**（LightRAG 统一管理）：
- `l1` — L1 摘要
- `l2` — L2 原文
- `skill` — Skills 文件
- `mcp_tool` — MCP 工具描述

---

## 配置文件架构

### 程序目录 `config/`

| 文件 | 用途 |
|------|------|
| `config/user-config.json` | LLM API Key、模型选择 |
| `config/llm-presets.json` | LLM 预设列表 |
| `config/agents/niu.md` | 主 Agent 定义（提示词、权限、MCP服务器） |
| `config/agents/file-processor.md` | 子 Agent 定义（文件处理专用） |
| `config/mcp-servers.yaml` | MCP 服务器配置 |
| `config/disk/*.yaml` | MCP 虚拟磁盘配置（把 100+ MCP 工具 Schema 映射为 Unix 风格路径，解决工具爆炸问题） |

**MCP 虚拟磁盘**（项目核心特色）：
- 所有 MCP 工具的 Schema 不直接注入 Agent 上下文（避免上下文爆炸）。
- 通过 `config/disk/*.yaml` 映射为 Unix 风格虚拟文件系统。
- Agent 用单一 `disk()` 工具以 `ls /`、`cat /memory/xxx`、`/memory/xxx(params)` 方式调用。
- 详细规范见 `docs/manual-mcp-disk.md`。

### 模型目录 `models/`

| 目录 | 大小 | 用途 |
|------|------|------|
| `models/bge-base-zh-v1.5/` | ~390 MB | BAAI/bge-base-zh-v1.5 中文向量模型（768d） |
| `models/models/buffalo_l/` | ~326 MB | InsightFace 人脸识别 |

**加载逻辑**：优先从本地加载，本地没有才下载。

### 用户目录 `~/.niu/`

| 文件 | 用途 |
|------|------|
| `memory.json` | 用户记忆（身份、偏好、工作目录） |
| `preferences.json` | 存储配置（分类、路径结构、冲突阈值） |

---

## 关键技术点

### MCP 工具调用规范

**推荐：使用 ToolRegistry 同进程调用（新架构）**：
```python
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
- InsightFace/ONNX Runtime 在异步环境中可能导致问题，建议使用同步调用。
- ToolRegistry 已经是同步架构，无需 `asyncio.to_thread`。
- 所有新代码应使用 ToolRegistry，避免 stdio 通信。

### 人脸识别模型管理

**内存管理**：
- InsightFace 模型加载后占用 ~326MB 内存。
- 空闲 5 分钟自动卸载（`MODEL_IDLE_TIMEOUT_SECONDS = 300`）。
- **不要在卸载时调用 `gc.collect()`**：可能导致崩溃。

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

前端是 Electron 33（`ui/main/`），含三套窗口：
- `assistant/` — 主对话窗口（精灵 + 聊天）
- `settings/` — 设置窗口
- `graph/` — 知识图谱可视化（force-graph 渲染）

**关闭流程**：
1. 前端窗口关闭 → 触发关闭事件
2. 调用 `/api/shutdown` 通知 Python API
3. Python API 清理资源
4. Rust 启动器终止所有子进程

---

## 常见问题

### 照片拖入卡死

**原因**：历史上 MCP 走 stdio 时存在此问题。当前 MCP 已同进程化（ToolRegistry 直接调用），此问题已不存在。保留此节作为历史参考。

**历史解决方案**：
1. 将 MCP 工具调用改为同步。
2. 添加 `preload_face_model()` 在 MCP stdio 启动前预加载。

### 主 Agent 工具丢失

**检查点**：
- `config/agents/niu.md` 的 `mcpServers` 列表是否完整。
- MCP 服务器配置是否正确（`workdir` 指向 `src/`）。

### 子 Agent 缺少 MCP 工具

**检查**：`agent/subagent.py` 的 `get_subagent_mcp_tools_schema()` 是否根据 `mcpServers` 配置获取工具。

### 历史对话丢失

**检查**：`niu_api/session.py` 的 API 调用参数是否正确，避免将 `session_id` 当作 `limit` 参数传入。

### 记忆无法保存或检索

**检查**：
1. LightRAG 是否初始化：检查日志中是否有 "LightRAG initialized"。
2. Memory Server 是否正常：`python -m niu_memory_server`。
3. 日志中是否有错误：`tail -f logs/api_stderr.log | grep "记忆|MEMORY|LightRAG"`。

**解决**：
- 检查 LightRAG 工作目录：`~/.niu/lightrag/`。
- 检查数据库路径：`~/.niu/memory.json` 中的 `workspace.path`。

---

## 相关文档

- `docs/SYSTEM_MANUAL.md` — 系统手册（功能列表、架构设计、分册索引）
- `docs/manual-mcp-disk.md` — MCP 虚拟磁盘手册
- `docs/manual-general-subagent.md` — 通用子 Agent 体系（阶段三）
- `docs/personal-assistant-architecture-v2.md` — 产品定位与核心亮点（架构 v2）
- `docs/feature-photo-processing.md` — 照片处理设计
- `docs/feature-file-management.md` — 文件管理设计
- `docs/feature-document-processing.md` — 文档处理设计
- `docs/feature-scheduled-tasks.md` — 定时任务设计
- `docs/note-agent-communication.md` — Agent 通讯技术笔记
- `docs/implementation-L0L1L2.md` — L0/L1/L2 三级存储实现分析
- `docs/design-self-evolution-system.md` — 自我进化系统设计规范
- `docs/USAGE-self-evolution.md` — 自我进化系统使用指南
- `docs/analysis-genericagent-evolution.md` — GenericAgent 进化机制分析

---

## 历史更新日志
> 以下为历史记录，反映彼时状态。部分条目中的架构（Go 后端、Nanobot、MCP stdio、`pkg/` 目录）已被后续重构推翻，当前架构以本文件为准。

### 2026-08-09

#### 新增：知识图谱时间链（会话日期链补全 + 主 Agent 认知 + dream-evolver 减负）

- **程序补链**：`_ensure_session_chain()` 在三条 dream 管道收尾补全会话实体 `followed_by` 日期链——nap（小憩）/ sleep（睡眠兜底）/ force（手动整理），小憩与睡眠互补（一天没事小憩不触发但睡眠必触发）；只补边/断边、不建实体（当天无内容=选择性记忆正常行为）；10 日历天窗口；中间日期实体出现后断开跨越边、重建逐日链（如"昨天的事"补挂）
- **dream-evolver 减负**：特殊节点表改为"日期实体天生存在"+ 连接示例（肯定式，无否定指令），消除其"先查再建"日期实体的动作
- **主 Agent 教学**：niu.md 主动深挖策略加"知识图谱时间链"小节（何时用/怎么用，含 timeline_query 参数）
- **disk 容错**：`--start-entities` 等 array 参数裸字符串自动包 JSON；get_entity_info 说明"关系用 get_graph"

#### 新增：LightRAG 关系方向语义说明（图无向，方向在 description）

- **事实**：LightRAG 图本质无向（nx.Graph + 读取排序 + vdb sorted 去重，上游 2025-03 起设计，fork 未改）——边的 source/target 顺序只是排序，不代表方向；**方向语义只在关系 description 文本里**（如"李磊 属于 人际关系脑区"），LLM 读描述可正确推断（红楼梦人物关系测试准的真相）
- **修复**：① lightrag 查询工具（query/query_data/get_graph/timeline_query/get_relation_info）的 MCP Schema + disk yaml long 描述加输出契约说明"source/target 顺序仅为排序、不代表方向；方向看 description"；② **disk_navigator 目录 readme 渲染 tool.long 描述**（此前只显示 short+参数，漏 long——前天优化的 browser/config-manager/file-parser 描述与方向说明主 Agent 都看不到；readme 应为最全面的总览）
- **排查教训**：虚拟磁盘工具说明主 Agent 实际看 `cat /<dir>/readme.txt`（动态生成，渲染 short+参数+examples，不渲染 long）——修改工具描述须确认 readme 呈现；LightRAG"方向乱"多为字典序字段与 description 混读的假象，先确认图存储/查询方向语义再下结论

### 2026-04-15

#### 新增：KG 数据流入 5 条渠道全部实现

知识图谱（KuzuDB）从空壳变为有真实数据流入：

| 渠道 | 实现方式 | 关键文件 |
|------|---------|---------|
| 1 文档→KG | `sync_to_kg()` 程序化调用 | `photo-server/__init__.py` |
| 2 照片→KG | `sync_photo_to_kg()` 程序化调用 | `photo-server/__init__.py` |
| 3 聊天→KG | dream-evolver 子Agent，睡眠时增量学习+KG写入 | `config/agents/dream-evolver.md` |
| 4 便利贴→KG | notes API + `sync_note_to_kg()` | `niu_api/notes_api.py` |
| 5 批量整理 | KGSync 服务，6小时周期 | `agent/injector/kg_sync.py` |

#### 新增：梦境进化子 Agent（dream-evolver）

从 context-manager 拆出学习/建模职责，新增 KG 实体/关系写入。

- **执行顺序**：sleep → dream-evolver（增量学习+KG写入）→ context-manager（压缩删除）
- **6 项工作**：错误经验、成功经验、工具方言、用户状态、用户画像、KG 实体/关系写入
- **增量游标**：`~/.niu/last_dream_evolve.json`，避免重复处理
- **metadata 对齐**：工具方言→query_pattern（递归检索），经验→document（参考知识桶），状态/画像→interaction_habit（交互习惯桶）
- **关键文件**：`config/agents/dream-evolver.md`、`niu_api/compat.py`、`config/agents/context-manager.md`

#### 新增：便利贴后端 API + SQLite 持久化

便利贴从纯 localStorage 迁移到后端存储：

- **新建**：`niu_api/notes.py`（SQLite 数据层）、`niu_api/notes_api.py`（FastAPI 路由）
- **端点**：POST/GET/PUT/DELETE `/api/notes`
- **数据库**：`~/.niu/notes.db`
- **前端迁移**：启动时从后端加载，更新时调 updateNote，批量同步实现
- **KG 写入**：便利贴创建/编辑时写入 KG Document 节点 + 实体提取（正则规则）

#### 重构：context-manager 精简为压缩专用

移除 5 个学习/建模章节，只保留压缩逻辑：l0/l1/l2 压缩、会话单元识别、消息删除规则、强制压缩模式。

---

### 2026-04-04

#### 修复：NiuHandler 缺少工作记忆机制

**问题**：Agent 无法"自我进化"，工具循环表现异常，代码直接显示给用户而非执行。

**根因**：NiuHandler 缺少原始 GenericAgent 的核心机制：

| 机制 | 作用 | 缺失状态 |
|------|------|---------|
| `tool_after_callback` | 每次工具调用后记录摘要到 `history_info` | ❌ |
| `_get_anchor_prompt` | 生成工作记忆提示词注入 `next_prompt` | ❌ |
| `next_prompt_patcher` | 周期性警告防止死循环 | ❌ |

**解决方案**：
1. 添加 `tool_after_callback`：工具调用后提取 `<summary>` 或自动生成摘要，追加到 `history_info`。
2. 添加 `_get_anchor_prompt`：生成包含 `history_info[-20:]`、`current_turn`、`key_info` 的工作记忆提示词。
3. 添加 `next_prompt_patcher`：每 35 轮强制 `ask_user`、每 7 轮警告禁止无效重试、每 10 轮注入全局记忆。
4. 修改各 `do_XXX` 方法：使用 `_get_anchor_prompt()` 替代硬编码的 `"\n"`。
5. 添加状态重置：`/new` 命令时调用 `reset_working_memory()`。

**修改文件**：`agent/handler.py`、`niu_api/compat.py`。

#### 修复：子 Agent 缺少 MCP 工具

**问题**：子 Agent（file-processor 等）调用 MCP 工具失败。

**原因**：`subagent.py` 只获取基础工具 schema，没有 MCP 工具。

**解决方案**：
- `mcp_client.py` 添加 `get_mcp_tools_for_servers()` 按 server 名称过滤工具。
- `subagent.py` 添加 `get_subagent_mcp_tools_schema()` 根据 `mcpServers` 配置获取工具。

#### 修复：空代码块显示问题

**问题**：LLM 响应中的空代码块原样输出，显示为多个 ` `````` `。

**解决方案**：在 `runner.py` 添加清理空代码块的正则表达式。

#### 新增：动态注入架构

**目标**：MCP 工具描述和 Skills 内容按语义动态注入提示词，减少基础提示词长度。

**实现**：
- `agent/injector/sync.py` — Skills 定时扫描同步到向量库（`metadata.type="skill"`）
- `niu_api/injector.py` — API 端点手动注册 MCP 工具描述（`metadata.type="mcp_tool"`）
- `agent/runner.py` — `_inject_dynamic_resources()` 按语义搜索并注入

#### 修复：同步/异步架构冲突

**问题**：GenericAgent 纯同步，MCP 客户端异步，FastAPI 异步端点，导致事件循环冲突。

**解决方案**（已被同进程架构取代，保留为历史）：
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

**解决方案**：恢复 `preload_face_model()` 调用。

---

### 2026-04-03

#### 重构：GenericAgent 整合

**目标**：用 GenericAgent（~1700 行）替换 Nanobot（~53 万行），实现更简洁的 Agent 架构。

| Step | 内容 | 提交 |
|------|------|------|
| **1** | 全量搬运 GenericAgent 核心代码到 `agent/generic/` | `06792e8` |
| **2** | Session 隔离 + SQLite 持久化适配层 | `6c256a5` |
| **3** | 向量检索注入（每轮对话注入，>50 分，最多 10 条） | `f388f58` |
| **4** | Token 返回 + 思考链处理 | `b3c3ffd`, `9452d51` |

**新增文件**：
- `agent/generic/` — GenericAgent 原始代码（不修改）
- `agent/session_adapter.py` — Session/SessionManager 类
- `agent/runner.py` — GenericAgentRunner 整合层
- `agent/vector_search.py` — 向量检索适配器
- `agent/thinking_chain.py` — 思考链处理器（支持 DeepSeek/MiniMax/Qwen 等）

**设计决策**：
- 研究了 Strands Agents SDK (5.5K 星)，决定继续用 GenericAgent（小而精、可控性高）。
- GenericAgent 作为主 Agent 循环，SubAgent 作为临时专业工人（待实现）。

**思考链处理**（统一处理不同厂商格式）：
- DeepSeek: `菏...SaveChanges` 或 `<thinking>...</thinking>`
- MiniMax M2.5: `<FLUX>...</FLUX>`
- Claude: API 原生 thinking block
- OpenAI o1: `reasoning_content` 字段

**Token 返回**：
- `MockResponse` 新增 `usage` 属性。
- `_parse_claude_sse` 返回 `(content_blocks, usage_info)`。

**删除的代码**：`pkg/` 目录已清空（Nanobot Go 代码已移除）。

#### 新增：/new 清空聊天记录

**功能**：在聊天框输入 `/new` 清空当前会话的所有聊天记录。

**实现**（历史 Go 后端实现，当前架构已迁移）：
- `main.go` 添加 `/api/chat/clear` 端点，调用 `sessionManager.DB.DeleteMessages()`
- `preload-chat.js` 添加 `clearChat()` API
- `main.js` 添加 `clear-chat` IPC handler
- `chat.html` 的 `sendMessage()` 检测 `/new` 指令

#### 新增：输入框支持多行输入

**问题**：粘贴带换行的文本时换行符丢失。

**原因**：使用 `<input type="text">` 单行输入框。

**解决方案**：改用 `<textarea>` 支持多行输入；Enter 发送，Shift+Enter 换行；自动调整高度（最大 120px）。

#### 修复：主 Agent 工具丢失（历史 — Nanobot 时代）

> 此条目为 Nanobot/`pkg/toolloop/toolloop.go` 时代的问题，当前架构已无此文件。

**问题**：主 Agent 只显示 3 个 `chat-with-*` 子 Agent 工具，看不到 MCP Server 工具。

**原因**：`pkg/toolloop/toolloop.go` 第 172 行缺少 `agent.MCPServers`。

#### 修复：主 Agent 缺少系统工具（历史）

> 此条目为 Nanobot 时代的问题。

**问题**：bash、read、write、edit、glob、grep 等系统工具不可用。

**原因**：`niu.md` 的 `mcpServers` 列表缺少 `nanobot.system`。

**解决方案**：添加 `nanobot.system` 到 mcpServers 列表。

#### 优化：Agent 提示词改进

**问题**：Agent 收到指令后返回"执行中..."但不调用工具。

**原因**：LLM 把说话当作"执行"，没有理解"执行 = 调用工具"。

**解决方案**：将抽象规则改为具体操作指令，添加错误/正确示例。

#### 删除：10 轮对话自动整理

**问题**：正常对话过程中触发睡眠模式整理。

**原因**：遗留的 10 轮对话整理代码。

**解决方案**：删除自动整理逻辑。

---

### 2026-04-02

#### 修复：照片拖入卡死问题（历史 — stdio 时代）

> 此条目为 MCP stdio 时代的问题，当前 MCP 已同进程化（ToolRegistry），此问题已不存在。

**问题**：文件拖入正常，照片拖入卡死，日志显示 `photo-server stdin is closed`。

**根因**：`asyncio.to_thread` + InsightFace/ONNX Runtime 在 MCP stdio 环境中存在兼容性问题。

**解决方案**：
- 将 `mcp-servers/photo-server/__init__.py` 的工具调用从 `asyncio.to_thread` 改为同步调用。
- 添加 `preload_face_model()` 在 MCP stdio 启动前预加载 cv2 和 InsightFace 模块。

**教训**：
- **MCP 工具调用优先用同步**：`asyncio.to_thread` 在 MCP stdio 环境中可能有问题，特别是涉及 ONNX Runtime 等原生库时。
- 不同机器表现不同，慢电脑更容易出问题。

#### 新增：人脸识别模型空闲卸载

**问题**：InsightFace 模型加载后占用 ~326MB 内存，永不释放。

**解决方案**：
- 后台定时器线程每 60 秒检查。
- 空闲超过 5 分钟自动卸载模型。
- 配置：`MODEL_IDLE_TIMEOUT_SECONDS = 300`。

**教训**：
- **不要在卸载时调用 `gc.collect()`**：可能在其他线程使用对象时释放，导致崩溃。
- 让 Python 垃圾回收器自然回收。

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

#### 修复：Electron 关闭时后端不退出（历史 — Go 后端时代）

> 此条目为 Go 后端时代的问题，当前后端已为 Rust 启动器 + Python API。

**问题**：关闭 Electron 窗口后，后端和 embedding 服务进程残留。

**原因**：Electron 退出时没有通知后端关闭。

**解决方案**：
- `main.go` 添加 `/api/shutdown` 端点，调用 `cancel()` 取消 context。
- `ui/main/main.js` 在 `close-all`、托盘关闭、`before-quit` 中调用该端点。

#### 新增：聊天历史加载功能

**问题**：打开聊天窗口没有历史消息，刷新也不显示。

**原因**：
- `preload-chat.js` 缺少 `getHistory` API。
- `main.js` 缺少 `get-history` IPC handler。
- `chat.html` 没有加载历史的代码。

**解决方案**：
- `preload-chat.js` 添加 `getHistory`、`getSessionId`、`getPendingMessages`。
- `main.js` 添加 `get-history` IPC handler。
- `chat.html` 添加 `loadHistory()` 和滚动加载更多逻辑。
- `pkg/session/store.go` 的 `GetRecentMessages` 返回正序（最旧在前）。
- 新增 `GetMessagesBefore` 支持加载更早的消息。

**消息顺序**：最旧在上，最新在下，滚动到顶部加载更多。

---

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **niu-agent** (18362 symbols, 34363 relationships, 300 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

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
