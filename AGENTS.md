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
9. **方案/计划等私有文档（`docs/superpowers/` 整个目录）必须在本地 `plans` 分支编写与提交，禁止在 main 上提交** — 写方案前 `git checkout plans`（私有文档只在 plans 分支存在），完成后 `git checkout main`；**`plans` 分支永远不推送**；main 的 `.gitignore` 排除 `docs/superpowers/`，push 天然干净、pull 不影响本地私有文档。审查 Agent 读计划文件时若在 main 上文件不可见，先 `git checkout plans`。此铁律必须传达给派出去的子 Agent。

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
9. 私有文档（`docs/superpowers/` 整个目录）遵循铁律 9：在本地 `plans` 分支编写与提交（有 git 历史供多轮审查），`plans` 分支永不推送；main 通过 `.gitignore` 排除该目录，push 天然干净。

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

### 2026-08-11

#### 修复：主 Agent ask_user 工具（暂停问话）+ 轮中 schema 刷新 + @ 通道反馈闭环 + 通配路由存在性检查 + cleanup 注销通知（nutritionist 事故五层修复收尾）

- **根因**：① 主 Agent 没有 ask_user 工具——想与用户交流只能输出纯文本 → 无 tool_calls → 程序判定 CURRENT_TASK_DONE → **退出工具循环**（"停下来问话"），工作流中断；② schema 刷新只在 chat() 入口执行，工具循环内新建 agent md 当轮冻结 → 主 Agent 幻觉调用不在 schema 的 chat-with-*；③ @ 通道三层静默丢失（提取正则只认 @类型-4hex 不匹配同步名、回复防护只查 content 开头、orphan 静默丢弃）；④ 通配路由容忍不存在 agent 致幻影执行
- **修复**：
  1. **ask_user 工具**：复用子 Agent 的 UserAskRegistry（key="main-agent"），阻塞等待用户回答期间 chat() 生成器不结束 → 不触发 cleanup；挂起子 Agent 存活，工作流不中断（暂停而非停止）
  2. **schema 轮中刷新**：_on_turn_end 每轮 LLM 前刷新工具 schema 并返回新 schema，工具循环内新建的 agent 当轮即出现 chat-with-*
  3. **@ 通道反馈闭环**：提取正则兼容同步名（无 hex 后缀）、无匹配打日志、回复防护查任意位置、orphan 推回主 Agent 显式报错
  4. **通配路由存在性检查**：get_subagent_config 对不存在 agent 返回空 dict → chat-with-* 未知 agent 显式错误反馈，不再容忍执行
  5. **cleanup 注销通知**：轮末清理挂起同步子 Agent 时通知主 Agent，注销不再静默
- **验证**：T1-T5 单测全过（test_main_agent_ask_user 11 / test_schema_refresh_in_turn 2 / test_at_sync_name 10 / test_at_prefix_interception 27 / test_at_message_parser 9 / test_ask_user_cleanup_protection 2 / test_chat_with_unknown 4 / test_dynamic_injection / test_truncation_marker）+ 全量回归与基线一致（db_monitor 9 陈旧 patch / agent_loop 13 为 pre-existing 豁免，零新增）
- **教训**：需要与用户交流必须给显式暂停工具（业界 ask_user / LangGraph interrupt 模式）；"停下来问话"vs"暂停问话"是工作流中断与挂起的本质区别；工具调用任何失败都不得静默（无反馈 = 任务终止）

#### 修复：飞书流式卡片（打字机失效 + ask_user 问题不即时显示）——IM 抽象层内容形态错配，R1-R11 方案审查 + 6 轮实施审查收敛

- **根因（官方文档定案）**：飞书 CardKit 流式更新文本 API（`PUT card-element/content`）要求**每次传元素累计全文**，平台自动算增量做打字机（新文本以旧文本为前缀 → 续打；前缀不同 → 整体替换直接显示）。本项目 IM 抽象层全链路（runner → gateway.notify_stream → feishu adapter `update_card_element`）传的是 **chunk 增量**——相邻 chunk 前缀不同 → 每次整体替换 → 卡片只显示最后一个 chunk，**打字机永不触发**；只有 route_out SEND 终结（`full_reply` 全文）才显示完整 → 用户"内容在流式里但只有结束才显示全貌、ask_user 问题看不到"。
- **修复（main 分支 9 commits：86d6c7a2 → ca6473dc）**：  1. **adapter 累积全文**：`CardState` 加 `accumulated`（首 chunk raw 种子，后续追加）→ `update_card_element` 每次传 `_truncate_card_text(accumulated)`（字节守卫 CUT_BYTES=29500，官方卡片 ≤30KB error 200860；CJK 3B/字，17900 字符≈53KB 超限）
  2. **ask_user 终结 + 问题独立消息**：`handler.do_ask_user` 改 `send_sync(_cid, "", pop_reply_to=False, ask_finalize=True)` 终结当前卡片（adapter 用 accumulated 定稿）→ `send_sync(_cid, f"❓ {question}", ask_finalize=True)` 问题作独立消息即时显示；`gateway` 加 `send_sync`（同步线程安全桥，透传 ask_finalize）
  3. **重复防护 `_ask_finalized` + `_ask_finalized_content`**：ask_user 终结记标记 + 拼接记录终结内容；无 state + 标记在 + 非 ask_finalize 的 route_out SEND 仅当 `content == 记录` 才跳过（真重复，防 ask_user 终结后无新 chunk 整轮重发）；return_value 兜底文本（CONTEXT_OVERFLOW/STOPPED/错误）≠ 记录 → 正常 send_markdown（不丢）；标记清除三处（建新卡 mark 门控 pop、route_out 跳过/兜底双清、重连 clear）
  4. **stream_error 进流式**：runner else 分支（普通 str）也 notify_stream（进 accumulated，终结含错误文本不丢）
  5. **uuid 防跨卡冲突**：`update_card_element`/`finalize_card` 的 uuid 带 card_id 前缀（`niu-{card_id[-6:]}-...`，防 ask_user 多卡 seq 重置 uuid 复用触发 200770）
  6. **`_build_final_body` 总预算**：有图多段终结按总 JSON ≤30KB 分摊（实测常数 wrapper 220 / md 55 / img 135 / 后缀 26 / 转义 1.08）
- **质量链**：方案 v3→v15 十一轮双审查（R1-R11，每轮 2 审查员异角度、**必须先学飞书官方手册**——未学=审查无效；R10+R11 连续两轮零 bug 通过）；实施 9 commits + 6 轮实施审查（Spec 合规 + Quality 2 P2 + 判重生命周期 4 轮修复收敛，最终 APPROVE 92%）；测试：tests/test_feishu_adapter.py 新建（21 用例，sys.path 需加 im-adapters/feishu/src）+ ask_main_agent/gateway 系列 33 passed
- **实测反馈修复链（2026-08-12 用户飞书实测，4 commits：f2df81ac → 8ff12548）**：
  1. **主 Agent ask_user 双通道（ba46b949）**：`do_ask_user` 的 `if not pushed:` 门控导致 **Electron 窗口开 + 飞书 IM 双端场景 IM 推送被跳过**（Electron SSE 推成功即 pushed=True → IM 不推）→ 飞书只看到主 Agent 流式回复"现在用 ask_user 问您"，看不到 ❓ 问题内容。改：`electron_pushed` + `im_pushed` 独立，IM 无条件推（_cid 存在时），`pushed = or`
  2. **子 Agent @user 提问补 IM 推送（f2df81ac）**：`_ask_user_impl`（subagent.py）只推 Electron（SubagentEventBus）无 IM 推送 → 双端场景子 Agent @user 提问飞书同样收不到。补：`send_sync` 终结 + 问题独立消息（同 do_ask_user 模式）
  3. **去 ❓ 装饰前缀（2f769b25）**：ask_user 问题消息的 `❓` 红色问号纯展示（回答拦截/判重都不解析），用户认为多余——三处（handler/subagent/chat.html）同步去掉
  4. **niu.md 同步子 Agent 反问补 ask_user 强制警告（8ff12548）**：主 Agent 提示词原无 ask_user 指导——补"反问需要用户参与时**必须用 ask_user** 转述，否则子 Agent 阻塞 → 被迫结束 → 任务失败"（含 nutritionist 示例）
  - **验证**：用户飞书实测全链路通过（messages.db + llm_interaction 确认：子 Agent 提问 → ask_user 显示 → 飞书回答 → set_answer 注入 → 主 Agent 转述 → 子 Agent 完成；测试 44 passed）
- **排查教训**：① **飞书功能必须先学官方机制再动手**——本工程初版靠半途子 Agent 的猜测闭门造车，v1/v2 全错被回退（`git reset --hard e77fe352`）；官方文档一句话点破："Pass the **full text content**... prefix → typewriter, different prefix → replace"；② **IM 抽象层是"内容形态"约定层**——流式推增量 vs 平台要全文，语义在层间流转必须一致（runner 推 chunk 是增量抽象，adapter 负责翻译成飞书全文契约）；③ 审查 Agent 必须先学官方手册（用户铁律），否则凭印象审查全部无效；④ 方案多轮修订会引入代码块截断/残留/块间不一致——修订后必须验证代码块配对（偶数）+ 引用一致；⑤ **双端场景（Electron+飞书同开）是 ask_user IM 推送的盲区**——`if not pushed` 门控假设"纯 IM 会话"，实际 Electron 有订阅者时 IM 被跳过；排查须看 messages.db 会话还原 + 完整读 llm_interaction（勿凭单条日志下结论）；⑥ 内容判重（accumulated vs full_reply）受 strip_at_messages 逐 chunk 归一化失配影响不可靠——状态判重（标记+记录）更稳

### 2026-08-10

#### 修复：主 Agent 停止立即返回（统一可中断执行层 run_interruptibly 覆盖注入检索/TTFT/工具执行盲区）

- **根因**：2026-08-08 停止改造只覆盖 LLM 流式读取（`_interruptible_iter`）；主 Agent 每轮 LLM 前动态注入 LightRAG 检索（skill/knowledge/脑区激活 3 处 call_async，各 120s 超时）+ LLM 调用建立（TTFT，openai SDK `send(stream=True)` 同步等响应头，上界 read_timeout 300s）+ 工具执行（exhaust 同步消费）均无停止检查 → 卡住时停止无效、只能等超时（实测"三五分钟"= 120s×3 或 300s）
- **修复（统一可中断执行层，机制级而非逐工具补丁）**：
  1. **新增 `agent/generic/interruptible.py`**：`run_interruptibly(fn, stop_check)`——后台 daemon 线程执行 fn + 前台 0.2s 轮询 stop_check + **启动前预检**（stop 前置到达零线程启动，端到端收敛单轮询 ~0.2s），同 `_interruptible_iter` 模式
  2. **动态注入四处包可中断**：search_by_file_path / search_multi_lightrag / _traverse_from_hits / 脑区激活 activate_for_query（runner.py，含 block 间 stop 短路）——检索超时 120s→15s（lightrag_adapter 两方法 + query_data 加 timeout 参数透传，慢则降级空注入）
  3. **agent_loop LLM 前新增停止检查**（agent_loop.py L828/L834）：注入放弃后立即 STOPPED，不发起 LLM 调用
  4. **LLM 调用建立（TTFT）三处 completion 包可中断**（litellm_adapter.py L710 初始 / L773 socket fallback / L837 重试）：放弃返回空或已积累内容 MockResponse（stream_error=False）→ after-LLM 检查 STOPPED，不再等 read_timeout 300s
  5. **工具执行 exhaust 包可中断**（agent_loop.py L1157）：stop 放弃等待，后台线程继续跑、结果丢弃——用户拍板；chat-with-* 同步子 Agent 放弃时 terminate 实例（防 clear_stop 后逃逸单击停止）
- **验证**：T1-T4 单测 16 用例（test_interruptible_runner 7 / test_inject_interruptible 3 / test_ttft_interruptible 3 / test_agent_loop_tool_interruptible 3）+ 既有 agent_loop 测试回归
- **教训**：① **Python 线程模型无法 OS 级强杀**（pthread_kill 信号 handler 固定主线程 / PyThreadState_SetAsyncExc 不打断阻塞等待 / 杀 API 进程 launcher 不自动重启）——"停止立即返回"的最优实现 = 统一可中断执行层（后台执行 + 前台轮询放弃等待）② **用户语义**：不要求杀后台，只要主 Agent 前台立即回——放弃等待后 daemon 线程继续跑完可接受 ③ **TTFT 同步阻塞实证**：stream=True 仅"body 惰性读取"，请求发送+响应头等待同步阻塞（openai SDK `_base_client.py` request→send(stream=True) 实证），`_interruptible_iter` 在 response 返回后才启动、覆盖不到建立窗口 ④ 动态注入检索是主 Agent 每轮 LLM 前的隐藏长阻塞（120s×3 实测超时源），超时参数化是配套；锁核查无 self-deadlock（graph_read_lock=RLock copy-only、LightRAG coro 恒在单例 loop，asyncio 锁 coroutine-bound）
- **质量链**：计划 11 轮审查（R1-R11，R10+R11 连续两轮零 bug；R5 跨 5 轮抓出 TTFT 盲区、R6 补重试/fallback、R7 测试缺陷双审交叉、R1/R2 补 chat-with 逃逸与脑区激活第 4 处）；每 Task spec+quality 双审；提交 c7493e4c（执行器）+ 25bc4b72（注入）+ 47b357d4（脑区）+ 1b6c3258（TTFT）+ e8805557（LLM 前检查）+ c4c9f740（工具执行）

#### 新增：macOS Cmd+Q 拦截（阻止误退出，assistant 模式精灵/Chat/图谱 3 窗口全拒绝）

- **问题**：macOS 按 Cmd+Q 直接退出应用（自定义菜单"退出"项 accelerator 触发 `app.quit()`），精灵/Chat/图谱 3 窗口（同进程）都被误退出；Windows 无此问题（无强制退出快捷键），零改动
- **修复（ui/main/main.js 单文件，三重机制）**：
  1. **根源拦截（按模式条件化）**：macMenu（顶层 darwin 块、全模式共享）"退出"项 accelerator 改 `WINDOW_MODE === 'assistant' ? undefined : 'Cmd+Q'`——assistant 模式移除 Cmd+Q 绑定（快捷键不再触发 quit），settings/graph 保留原行为
  2. **防御网（before-quit 守卫）**：`process.platform === 'darwin' && WINDOW_MODE === 'assistant' && !allowQuit` → `e.preventDefault()` + 日志（darwin 门控保证 Windows/Linux 零语义）；拦截一切未闩锁的 quit（AppleScript `quit app`、系统 quit、未来 `app.quit()` 路径）
  3. **系统关机放行（powerMonitor）**：`powerMonitor.on('shutdown')`（whenReady assistant 分支，Electron 文档标注 Linux/macOS）→ `e.preventDefault()` + `allowQuit = true` + `app.quit()`——避免守卫 preventDefault 打断 macOS 关机/注销（会弹强制退出对话框）
  4. **allowQuit 闩锁**：macMenu 退出项鼠标点击、close-all IPC 先置位再 quit；托盘"⛔ 关闭妞妞"用 `app.exit(0)`（Electron 文档：不触发 before-quit）天然绕过；闩锁不复位语义已在守卫注释声明（当前无窗口 close 拦截中止路径）
- **验证**：自动（osascript `quit app "Electron"` → 守卫 preventDefault + `[main] quit blocked (unlatched quit: AppleScript/system?)...` 日志；settings/graph 独立模式回归不拦）+ 用户实机（3 窗口按 Cmd+Q 均不退出、菜单点击/托盘确认正常退出）
- **排查教训**：① **SIGTERM 给 Electron 会被 Chromium 路由进退出序列 → 触发 before-quit → 被守卫拦下**（dev 清理需 SIGKILL `pkill -9`；普通 pkill 的 SIGTERM 会让进程存活 ~1 分半；Rust launcher 从不向 Electron 发信号，生产无影响）② 守卫日志文案必须准确描述真实触发面——assistant 模式真实 Cmd+Q 被菜单层消费、永不触发 before-quit，守卫只在未闩锁的非 Cmd+Q quit（AppleScript/系统）触发，文案用 "unlatched quit" 防误导 ③ 实机验证的应用名：UI 进程是库存 Electron 二进制（bundle 名 "Electron"），`niu.app`（CFBundleName="Niu"）是 Rust launcher 监督进程（无 AppleEvent handler）——osascript 必须 `quit app "Electron"`
- **质量链**：计划 5 轮审查（R1-R5，R4+R5 连续两轮零 bug；R3 抓 osascript 应用名 P1 双审交叉）；实施 + spec 审查 ✅ + code quality ✅（1 Minor 日志文案修复）；提交 b9943419 + cd11d191

#### 修复：定时任务重复发送（trigger_callback 改 fire-and-forget，消除等待超时重试窗口）

- **根因**：`trigger_callback` 用 `ChatQueue.enqueue_and_wait`（120s 超时）等 Agent 回复——周报类长任务（>120s，实证 121.8s）超时后 `future.cancel()` 只取消等待、Agent 仍在处理 → 10s 重试再入队 → 同一任务内容入队两次（2026-08-10 09:00 weekly-report-reminder 实证：09:00:00 与 09:02:10 两条周报入队）
- **修复**：① reminder + background_script 两分支改 fire-and-forget——`enqueue_sync(channel="scheduler")` 入队即完成返回 "ok"，scheduler 立即 reschedule/删除；② 蹦高 + IM 推送提前到入队后（内容 = 任务内容 prompt，不再等 agent_reply）；③ bg recurring 报错保留 3-strike DLQ（返回 None）、one-time 报错 "ok"+永久删除（防 retry_failed 无限重置）；④ **enqueue_sync 必须显式传 `channel="scheduler"`**（默认 "im" 会让 ChatQueue worker 把 Agent 回复自动 push 到 IM——叠加手动 route_out = 同一任务两条 IM 消息；"scheduler" 通道未注册 → 自动回路由 no-op）；⑤ 删除 `enqueue_and_wait`/`_try_once`/10s 重试/`import time`；⑥ 删 `test_trigger_callback_retry.py`（3 个重试测试作废）
- **保留**：scheduler.py 零改动（in_progress/CAS 跨进程防双触发、8h 超时重置、失败计数、retry_failed）；`ChatQueue.enqueue_and_wait` 方法本身（ha_watcher 仍用，120s 等待无重试）
- **完成语义**：入队成功 = 通知已送达 = 任务完成；Agent 处理失败由 ChatQueue 降级回复机制兜底，不再需要 scheduler 等回复判成功
- **交付**：commits 51485133（reminder）+ edd42231（bg）+ 7c165499（删重试测试+ruff 清理）；计划 4 轮审查（R1-R4，连续两轮零 bug）；3 Task 每 Task spec+quality 双审；终审 APPROVE（0 Critical/0 Important，5 Minor 文档级取舍：scheduler.py 过期注释/_CALLBACK_TIMEOUT 注释、bg 分支 4 个测试小缺口、两分支推送代码重复——均为计划已接受项）
- **排查教训**："等 Agent 回复判成功"的等待协议脆弱（Agent 生成耗时不可控）——通知类任务应"送达即完成"，等待+超时重试必然引入重复窗口；跨通道自动回路由（ChatQueue worker 按 channel 回推回复）是防双消息的隐性耦合点，改 channel 语义必须先查 worker 路由行为

#### 修复：子 Agent 上下文压缩两级策略（tool 占位符化 → FIFO 兜底）+ 删除 targetThreshold 配置

- **问题**：① 子 Agent 上下文超 80%（warningThreshold）后直接 `_fifo_prune` 整组删除——一轮的 assistant 推理文本 + 全部 tool 输出一起消失，LLM 推理链断裂，有用 tool 信息丢失导致重调工具；② `targetThreshold` 是子 Agent 专用压缩目标参数（百分比），但 `docs/manual-user-guide.md` 描述像主 Agent 的"强制压缩目标"，主 Agent 实际用 `compressTargetTokens`（绝对值 60000），参数对用户隐形且冗余。
- **修复（两级压缩，scheduler/主 Agent 零改动）**：
  1. **阶段 1（新增，温和）** `_placeholderize_tool_outputs`（agent/generic/agent_loop.py）：80% 触发时从最早 tool 输出开始，content 替换为 `[name 输出已裁剪]`（tool 消息自带 name，缺失回退 assistant.tool_calls 的 OpenAI 嵌套 `function.name` 匹配）；**达标即停**（token ≤ target 即停，保留尽量多上下文，防重调工具）；**10 轮保护**（最近 10 轮对话内 tool 不动，从尾部数 user 消息）；**幂等**（已占位符跳过，二次压缩不重复替换——用户拍板）
  2. **阶段 2（保留）** 仍超 target 才 `_fifo_prune` 整组删（保护 system+初始 user 不变）
  3. **删除 targetThreshold 配置**：`_read_target_threshold()` 删、压缩目标写死 `int(contextWindowSize × 0.50)`（用户拍板：80% 触发 → 压到 50%，参数无必要）；config-manager 模板 2 处、manual-user-guide、AGENTS.md 示例、3 个测试 patch 全链清理；settings UI 本无此字段
  4. **保留**：warningThreshold（主/子共用触发线，配置+UI 可调）；`context_target_threshold` 内部参数（subagent.py 写死 50% 传入，runner.py 主 Agent 传 0）；主 Agent 压缩（compressTargetTokens/on_context_high_usage）零改动；首轮回退路径（context_fifo_threshold）零改动
- **10 轮保护只约束阶段 1**：FIFO 兜底仍可删最近 10 轮内整组消息（两级设计必然，用户"10 轮内不动"仅适用于阶段 1）
- **重复触发无害**：子 Agent 分支不设压缩冷却/不重置 last_prompt_tokens → 下轮可能重复触发占位符化，因幂等 + 达标即停为 no-op
- **交付**：commits 5cc3f890（占位符化纯函数 TDD，10 单测）+ a57bef47（两处触发点两级串联，兜底 0.30→0.50）+ 2550d937（删 targetThreshold 全链路）+ a0d1f671（回归适配）；计划 R1-R5 审查（R4+R5 连续两轮零 bug）；每 Task spec+quality 双审；终审 APPROVE（0 Critical/0 Important）
- **用户可见变化**：子 Agent 压缩目标从 30%（配置缺失兜底）→ 50% 窗口（更温和）；旧轮次 tool 输出可能显示为 `[工具名 输出已裁剪]`
- **真实场景验证待触发后补（2026-08-10 用户拍板"暂时算完成"）**：占位符化触发条件苛刻（子 Agent 上下文 > 80% 且旧轮次含大量 tool 输出，需真实 LLM 长会话），当前验证止于单测（test_tool_placeholderize.py 10 例）+ 集成测试（mock 层）——真实场景端到端效果（占位符化后 Agent 推理连贯性、达标即停是否如期、是否减少 FIFO 整组删）未实测；**以后实际触发该场景时（日志见 `[ToolCrop] placeholderized N tool outputs`）需确认效果，必要时再调优**

#### 修复：子 Agent 去掉轮数上限（max_turns=None 无上限）+ 未完成结果游标不推进

- **事故（2026-08-10 实机）**：睡眠整理 context-manager 逐条精简 103 条消息，第 20 次 LLM 调用（raw_http 20260810/000031，finish_reason=tool_calls 要求再精简 idx:33/67/71）后撞线 `call_subagent` 硬编码 `max_turns=20`（初始化代码带入，从未有人拍板）→ `agent_runner_loop` 返回 MAX_TURNS_EXCEEDED → `call_subagent` 后处理只有 LLM_ERROR/length/CONTEXT_OVERFLOW 三分支 → 落 `return last_reply`（中间文本"再精简几个小工具输出..."）→ `_tidy_context_impl` 判非 overflow → **游标自动推进到范围末尾** → "压缩没结束但完成"；未处理消息被游标越过、下次整理不再覆盖（日志 `[Tidy] context-manager result: 再精简几个` + `Compress cursor auto-advanced` 特征）
- **用户拍板**：**子 Agent 是智能体，不需要轮数上限**（工具循环已有重复工具调用检测等多级保护，无上限后防失控依赖 stop_predicate 三检查点 + 上下文溢出保护 + 重复检测注入）；游标误判一并修
- **修复**：
  1. **max_turns=None = 无上限**：`agent_runner_loop` 循环条件 `while handler.max_turns is None or turn < handler.max_turns`（agent/generic/agent_loop.py L642/L750）；`_run_agent_loop` 默认 `max_turns: int | None = None`；`call_subagent` **新增 max_turns 参数（默认 None）**，resume/异步/同步三路径透传（agent/subagent.py）；主 Agent 默认 40 轮零改动（runner.py chat 入口）；显式传小值（测试用 1/2/5）仍触发 MAX_TURNS_EXCEEDED
  2. **incomplete JSON 契约**：call_subagent 后处理在 LLM_ERROR 之后、finish_reason=length **之前**插入分支——result in (MAX_TURNS_EXCEEDED/STOPPED/TERMINATED_BY_SUPPLEMENT) → 返回 `{"incomplete": true, "agent", "reason", "partial_result"(≤2000)}`（分支前置防 TERMINATED_BY_SUPPLEMENT+length 双重命中被 COMPACT_TRUNCATED 抢先——/stop drain 时序会走 TERMINATED_BY_SUPPLEMENT 而非 STOPPED）
  3. **全库 11 处游标决策点**（compat 7 + runner 3 + handler 1）`or _is_subagent_incomplete(x)` → 游标不推进 + reason 日志：compat L2697/L2780/L2862/L3279/L3453/L3535/L3617、runner L1297（Nap entity）/L1378（Nap dream，**三分支重构**：overflow 1/3 fallback 专属 / incomplete 不动 / else 全量保留 processed_up_to+range-end 兜底）/L1765（_run_subagent_step）、handler L963（_update_journal_cursor）
  4. **handler 顺序钉死**：`_update_journal_cursor` 用原始 result；incomplete JSON → 自然语言"子Agent未完成任务（reason）"只作用于返回 LLM 的显示副本（L1163/L1164）
  5. `_run_subagent_async` 通知基于 result 判 incomplete（"未完成（被停止/轮次耗尽）"）；mode2 入口短路；`_is_subagent_incomplete` 严格 `is True` 判定
- **存量游标修复（一次性数据操作）**：`~/.niu/last_compress.json` 回退 `6327de4d`(idx:103) → `12ba93d6`(idx:32)（未处理 idx:33/67/71 重新进入下次整理）；备份 `last_compress.json.bak-20260810-1520`；**注意 15:18 实况：代码修复前每次整理都会重演 bug 推进（回退被覆盖一次）——修复完成后才回退才有效**
- **交付**：commits d48e2d9a（agent_loop None）+ 13b306e9（call_subagent 参数透传）+ 292268dd（incomplete JSON 分支）+ 3948eec2（11 游标点 + _is_subagent_incomplete + 测试 22 新）；计划审查 R1-R6（R5+R6 连续两轮零 bug，R2 曾抓到"Task 2 改错函数"P0、R3 抓到"PM 采纳错误行号修正"、R4 抓到规格内部矛盾）；每 Task spec+quality 双审（Spec 符合规格可交付、Quality correct 0 Critical/0 Important，5 P3 非阻塞：journal 重写时间戳 cosmetic/force-cm fail-loud 日志误导（计划已接受）/JSON 误判面低概率/无上限逃逸风险（用户拍板，建议后续加轮数看门狗）/3 处覆盖缺口）
- **实机验证（2026-08-10 用户确认）**：修复后首次睡眠整理**压缩 5 轮自然完成**（对比事故时 20 轮撞线未完成）——修复生效的基本确认；长程大范围压缩场景待将来自然触发后验证（**下次触发时确认：无上限后长任务不被掐断、/stop 打断后游标不推进**）
- **排查教训**：①"压缩没结束但完成"先查**子 Agent 终止路径**（max_turns/STOPPED/TERMINATED_BY_SUPPLEMENT 三 result 是否在 call_subagent 后处理全有分支），不是先怀疑超时——最后一次 LLM 调用 10 秒正常返回，超时假设不成立；②游标推进逻辑"非 overflow 即成功"会把一切未完成结果当成功，程序化终止（非正常完成）必须有显式标记；③**PM 复核审查员行号类反馈必须 grep/sed 实证**——R2-A 的"L987 实为 L980"错误信息曾被采纳（R2-8），R3-A 实证纠正
- **排查教训**：子 Agent 触发分支（on_context_high_usage None）不设压缩冷却——与主 Agent 分支（回调后冷却）行为不同，跨轮重复触发依赖幂等兜底；测试断言"达标即停"必须用与实现同一 count 函数量 target（probe 法），不能猜字符数
- **回归豁免清单（12 个 pre-existing 测试失败，与本工程无关，勿当新失败）**：
  - `tests/test_context_overflow.py`：3× TestLiteLLMAdapterContextOverflow（断言查 chat 方法源码字面量 'context window'/'prompt is too long'/'maximum context length'——源码已不含）+ 3× TestSubagentFIFOThreshold（call_subagent 测试 mock 的 client.backend AttributeError）
  - `tests/test_on_before_llm_callback.py`：3×（_make_response 未设 stream_error=False → MagicMock 自动真值 → LLM_ERROR 短路）
  - `tests/test_llm_error_handling.py`：1× test_call_subagent_returns_subagent_error_prefix（FakeClient 无 backend）
  - `tests/test_compress_history.py`：1× test_build_compress_history_protected_assistant_excludes_its_tool（0==3）
  - `tests/test_sync_subagent_interaction.py`：1×（suspended_handler None）
  - 既有已知：test_compress_quality 的 REDACTED_USER_PATH、test_tidy_cursor 4 个 PROTECTED 断言

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

#### 新增：程序触发子 Agent 显示标签页（nap/sleep/force 全程可见）

- **根因**：`subagent_started` 事件只有 `handler.py _call_subagent_gen`（主 Agent 工具循环 chat-with-* 路径）一个发射点；系统触发的子 Agent（nap 小憩 / sleep 睡眠整理 / force 压缩管道的 entity-extractor、dream-evolver、journal-agent、context-manager）走 `call_subagent_with_auto_answer` → 低层 `call_subagent`，从不发该事件 → 前端收不到启动通知，不建 tab、不连 SSE（详细事件堆积在 ring buffer 无人订阅）——不是有意隐藏，是事件缺失遗漏
- **修复**：`call_subagent_with_auto_answer` 首次调用（answer is None）补三件套——① pre_register（防 SSE 404 竞态，幂等）② `subagent_started` 推送（is_sync=False，程序触发不阻塞主对话；`call_soon_threadsafe` 线程安全，调用方在 to_thread 后台线程）③ 异常/值错误清理（call_subagent 抛异常时无条件 close、register 失败返回 `[错误]` 前缀时 close——close 幂等 _closed 防双关，防 ring buffer 泄漏 + tab 卡死，同 handler 问题2c/2e 模式）
- **前端零改动**：main.js/chat.html 对 subagent_started 零过滤，收到即建 tab；同名复用（entity-extractor 每次整理同名复用旧 tab 不堆积）；窗口关闭守卫已有
- **不受影响**：blocked_subagents 不动；subagent_started 是顶级事件不进 LLM 上下文；skill-sync 非子 Agent；调度器 cron 任务走主 Agent 工具路径本就显示（发射点互不重叠无双推）
- **后续状态**：① 回归豁免（既有失败与本工程无关，按计划豁免不修）——`test_tidy_cursor.py` 4 个（PROTECTED 计数断言与 `_find_protected_range` user-turn-aware 语义不符，niu_api/compat.py 既有行为）+ `test_runner_stream_events.py` 1 个（`REDACTED_USER_PATH` 字面量路径未替换，纯测试工件）；② 实机验证已通过（2026-08-09）——重启后 sleep 整理触发，entity-extractor/dream-evolver/journal-agent/context-manager 四子 Agent tab 实时显示工具调用与回复、结束后自动关闭、同名复用不堆积；③ 手册分册不专门更新——tab 渲染为既有机制（manual-general-subagent §十三 泛化描述已覆盖），本次仅补事件入口，SYSTEM_MANUAL L30"子 Agent 运行时自动创建独立标签页"修复后更准确无需改；④ 全量质量审查加固（commit fa59f3ad）——`[错误]` close 加归属守卫（`SubagentRegistry.get(agent_name) is None`，并发同名触发时不误关活跃实例 tab/ring buffer）+ 两处 close 异常吞噬（`except Exception: pass`，防 TOCTOU 掩盖原始异常/破坏契约）

#### 修复：dream-evolver 自建日期节点（三层根因 + 工具契约修复）

- **根因**：① 提示词日期节点行只说"天生存在"，无免查/免建指令——通用"先查再建"流程（阶段A A1 去重、实体提取规则 3）对日期节点同样生效；② 查询一致性——`search_entities` 是向量检索，日期节点不在 vdb_entities（数字+汉字日期名嵌入不可靠）必 miss，而 `insert_entity` 的精确名查重能命中——"查不到→准备建→发现又有了"三轮反复；③ **insert_relation 工具描述未说明"建链自动创建不存在实体"**（LightRAG fork 功能：insert_relation 对不存在端点自动创建占位节点 description=UNKNOWN/entity_type=unknown——图谱 08-03/08-04/08-07 会话占位节点即实证），Agent 无从得知可直接建链
- **修复**：① MCP Schema + disk yaml 的 `insert_relation` 描述补"源/目标实体不存在时自动创建（含 `YYYY-MM-DD会话` 日期节点），无需预先查询存在性，直接建链即可"；② 提示词日期节点行改"当天日期节点由系统自动维护，建链时自动存在——不需要查询、不需要手动创建" + 连接优先原则第 3 条改"直接建链连接即可，节点自动存在"（**不提脑区**——禁止类比：脑区有注入列表而日期节点没有，类比会反向误导"没注入=不存在=要创建"）；③ manual-vector-store 工具表同步
- **排查教训**：① 工具描述必须说明副作用类输入语义（建链自动创建）——大模型读工具描述判断行为，不写它就按最保守流程执行；② 提示词对系统固定节点要**直接陈述机制**（"建链即自动存在"），不要用类比——类比可被模型反向解读；③ 排查流程：日志还原 Agent 工具调用序列（search 的 query/top_k/返回 → 结论 → 补救动作）比读提示词更能定位真实触发点

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
