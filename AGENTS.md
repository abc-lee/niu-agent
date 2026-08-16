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
9. **方案/计划等私有文档（`docs/superpowers/` 整个目录）在独立 git 仓库管理（2026-08-16 起，替代原 plans 分支机制——分支切换会导致运行中 Niu 服务的延迟导入读到旧代码，事故实证 2026-08-16）**：
   - `docs/superpowers/` 是**独立 git 仓库**（内层 `.git/`）——外层 main 仓库的 `.gitignore` 排除该目录（push 天然干净、pull 不删除、`git status` 零干扰）
   - **写方案/计划直接在 `docs/superpowers/` 内提交**：`cd docs/superpowers && git add -A && git commit`——不切分支、无 plans 分支、Niu 服务运行中写文档零影响
   - 读 main 代码用 `git show main:<path>`；审查 Agent 直接读工作区计划文件（禁 pytest、禁改代码、禁跑测试）
   - 实施：在 main 分支执行，与文档仓库无关；实施计划提交只进 main
   - 文档仓库**永远不推送**（本地版本比对用）
   此铁律必须传达给派出去的子 Agent。
10. **禁止用 Python/脚本直接修改任何代码或文档** — 所有文件修改（代码、配置、计划、手册）必须用 **Edit 工具**（先读后改：old_string 不匹配会显式报错，不会静默失败）。**Python 仅限只读分析**（读文件、算数据、grep 统计），禁止 `open(p,'w').write()` 写文件。`python -c 's=open(f).read().replace(...); open(f,"w").write(s)'` 一类批量静默替换**一律禁止**——`str.replace` 的 old_string 不匹配时静默跳过不报错，是"改了但没改对"的根源（2026-08-14 脑区 assign 计划 19 轮审查教训：行号漂移连续三轮、fake 结构修错、改一处漏同步，全因静默 replace）。此铁律必须传达给派出去的子 Agent。

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
9. 私有文档（`docs/superpowers/` 整个目录）遵循铁律 9：在**独立 git 仓库**（`docs/superpowers/` 内层 `.git`）编写与提交（有 git 历史供多轮审查），该仓库永不推送；main 通过 `.gitignore` 排除该目录，push 天然干净。

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

### 2026-08-16

#### 新增：残余静默点清理收尾（E4——E1/E2/E3 覆盖之外 17 条全处置：工具链错误可见化 + 事件转发 + 生命周期日志 + MCP 状态槽 + 降级可追溯 + do_grep 读失败清单——验收标准 1 全清单归零）

- **背景**：四路审计 116 处失败位置经 E1（工具兜底）/E2（LLM 通讯）/E3（图谱）覆盖后仍余 17 条（E4-01~17）——按"进 Agent 上下文 / 用户可见通道 + 日志 / 日志 + 显式接受"三选一处置，实现总体方案验收标准 1（116 处全清单归零）。重盘点补 3 行：E4-15（E1-06 重归属落点——序列化崩溃防核销悬空）、E4-16（register_server 失败）、E4-17（do_grep 误导性 No matches——用户拍板补修）。
- **核心机制（T1-T6）**：
  1. **T1 工具链**（agent_loop.py）：**E4-01** 参数解析失败 → 错误工具结果 `[工具参数解析失败: <err>]` 进 tool 消息 + 同文本 next_prompts 注入（循环续行 LLM 可自纠——防全失败轮 CURRENT_TASK_DONE 退出）+ 函数级连续失败计数（解析成功清零）+ 同一轮连续 3 次失败 → yield chat_idle + 显式退出（对齐截断强制退出模式）；**E4-03** data=None → content 中性占位 `（工具已执行，无返回值）`（全角括号无错误前缀语义——防 LLM 误读重试副作用工具；成功路径唯一文本变化显式声明）；**E4-15** 序列化三层兜底（json_default 内 str(o) try/except → `[无法序列化: <type>]` / _truncate_dict_result 外层 except Exception → error dict / list 分支 json.dumps 与裸对象 str() 直调两处包 try——自引用 list 结构性失败覆盖——无逃逸路径）
  2. **T2 事件消费链**：**E4-02** 强制退出分支（"⚠️ 输出多次超长截断，已强制退出"/"⚠️ 工具参数连续 3 次解析失败"——无 messages.append 未注入）→ **system_notice 专用 SSE 事件四跳链转发**（runner.chat system 分支文本匹配 → niu_api/chat.py → main.js → preload-chat.js onSystemNotice → chat.html ⚠️ system 提示渲染——E2 llm_error 模式同族；截断重试提示已注入 LLM → 显式接受；runner.chat 纯 str yield 契约零改动——不走 yield 新类型）；**E4-11** event-manager 验证失败 → 保留 system yield（verbose 调试通道——3 既有测试锁定）+ 失败文本注入 chat-with 结果流（display_result——主 Agent 下一轮可见；成功分支保持丢弃——LLM 已见自报成功）
  3. **T3 子 Agent 生命周期**：**E4-04** _ask_user_impl IM 推送 pass → logger.error（吞异常保持——AskUserFuture 超时兜底）；**E4-05** 异步通知 push pass×4 + 1 日志 → 统一 logger.error（异常路径 push/notify + CancelledError 路径 push/notify——db_monitor 轮询兜底保持）；**E4-06** instruction 双 pass → 区分 ImportError→warning（环境预期态）/Exception→error（真异常）+ return True 保持；**E4-07** registry ImportError pass → warning（不扩大捕获——运行时异常传播=可见）；**E4-09** get_subagent_mcp_tools_schema 异常返回 [] + logger.error（非裸字符串防 list+str TypeError）+ call_subagent 判定标注：mcpServers 非空 + 0 工具 → `⚠ 配置的 MCP 工具加载失败——0 工具可用（预期能力不完整）` 进 task/system 文本（绝不进 tools_schema 防伪工具）；**E4-13** allowBaseTools 笔误 → 保持 warning 显式接受（warning 含完整有效清单可自查）；**E4-14** system segments 降级 → 标注 `[子 Agent 提示词降级: <原因>]` 仅对非 JSON 结构化结果附加（JSON 结果经展示层 display_result 注入——journal 游标 json.loads 零破坏——threading.local 防并发串扰）
  4. **T4 MCP 注册管线**：**E4-08/E4-16** 失败收集进可查询状态槽（load_mcp_tools 可选段/load_external_servers/register_server 三处——服务端保留至下次加载周期**不显示后清除**）+ SSE 连接建立后随连接响应返回 mcp_load_failures 每连接一次（每连接已显示标记——防第二窗口/重连静默丢失——非启动完成推送）+ chat.html 首屏一次简单提示（E3 节流模式同族）+ 既有轮询路径 /api/chat/status 补拉；_fold_and_cap_reason 统一（换行折叠 + 200 字符保尾截断）；runner._startup 外层吞错保持（显式接受——双层日志防御性）
  5. **T5 对话降级链**：**E4-10** persist 失败 warning→error + IM notify_stream pass×3→error（闸门数量/位置零变化——4 结构断言保持）+ return_value 提取失败**无需修**（既有 logger.error 含异常对象——零控制流改动）；**E4-12** 降级回复 DB 附加 degraded_reason（timeout|internal——列扩展复用 tool_call_id 迁移模式 PRAGMA+ALTER ADD COLUMN DEFAULT '' + 读取端 .get 容错）+ 非 LLM 异常日志 warning→error（用户侧中性占位符保持——E2 定案不泄露内部细节）
  6. **T6 do_grep + 收尾**：**E4-17** grep_search 读失败收集 → 结果附加失败清单（截断 ≤5 + 计数——`(searched N files, M failed to read: [路径×5...])`）；**全失败** → 明确 `[GREP] 读取失败 M 个文件——无法确认匹配`（不再误导 No matches）；部分失败保留 no match 文案附失败计数；**有匹配时结果末尾附失败计数（`（另有 M 个文件读取失败：路径×5...）`——防匹配存在时失败静默丢弃）**（LLM 区分）；路径脱敏显式接受（grep 匹配行本身已含完整路径——同曝光类——≤5 截断不新增曝光面）
- **核销**（error-inventory.md E4 归属 17 条）：**已实施 15**（E4-01/03/04/05/06/07/08/09/11/12/14/15/16/17 + E4-10 主体——persist/IM notify）+ **E4-02 部分显式接受 + 部分已实施**（截断重试提示已注入 → 显式接受；强制退出事件 → system_notice SSE 转发已实施）+ **E4-10 内 return_value 子项无需修**（既有 error 日志含异常对象）+ **E4-13 显式接受**（配置笔误——warning 含有效清单自纠）——**三态词汇：已实施/显式接受/无需修**（E3 引入正式化）
- **待定位项核出**（handler.py 第 3 真静默点——positions A1）：盘点 handler.py 全部 system 事件——纯 system 单通道仅 E4-11 验证块（L1363/1370/1374/1377——已实施 display_result 注入）；其余 system 事件全双通道可见（L1199 Runner not initialized + error dict / L1396 SubAgent Error + tool result / L1484 Tool Error + TOOL_ERROR error dict——E1-02 实证）——**第 3 真静默点 = 审计期（E1 前）dispatch/disk_engine 未捕获异常崩溃路径（E1-02/E1-03 已实施 dispatch 外壳包裹 L1448 → TOOL_ERROR error dict）——核销"已覆盖"**
- **质量链**：详设 v1.0→v1.11 十一轮双审（A 技术 + B 原则——R10+R11 连续两轮零 bug）+ subagent-driven 实施（T1-T6 每 Task spec+quality 双审 + 修复闭环——T1 Fix 轮起点重置/T2 quality 四分支补全/T3 并发串扰 threading.local/T4 防御一致化/T5 degraded_reason 迁移 DEFAULT）；**关键教训：①next_prompts 注入是循环续行的前提**（仅错误工具结果时全失败轮 len(next_prompts)==0 走纯文本退出——自纠落空）；②**display_result 注入避开 next_prompt 结构测试白名单**（test_working_memory_removal.py TestAllNextPromptsEmptyAfterRemoval）；③**状态槽不显示后清除**（SSE 无重放缓冲——第二窗口/重连拉取恒空=静默丢失）；④**标注绝不进 tools_schema/JSON 结构化结果**（伪工具污染/journal 游标 json.loads 破坏）
- **验证（T6 收尾）**：回归 **36 文件 union（E3 17 文件基线 + E4 T1-T6 各验证集 20 文件——test_tool_truncation 双侧重合）** **632 passed / 13 failed / 12 skipped——零新增失败**；13 失败 = 12 pre-existing 豁免（①test_lightrag_adapter.py 9 个——TestIngester* 6（inject_entity 等写方法 85034d6d 移除后测试未更新）+ TestSearch* 3（大写实体类型 vs 小写期望）②test_lightrag_repair_unit.py 3 个真实数据用例（~/.niu 数据增长——chunk 数 116→152））+ 1 历史遗留（test_runner_stream_events.py::test_no_clean_stream_output——REDACTED_USER_PATH 字面量自 849564e3（2026-05-14）泄漏进文件——E4 T2 已记录豁免）；12 skipped = 7 integration marker（test_lightrag_resilience_integration——需 --run-e2e 真实数据）+ 5 e2e marker（test_e2e_message_persist——T1 已单独 --run-e2e 验证）；**116 行位置级对账表**（docs/superpowers/plans/2026-08-16-E4-positions-reconciliation.md——36 聚合条目展开 116 行逐行标注归属/无需修/接受——验收标准 1 唯一映射交付物）；~/.niu/messages.db **78→78 零新增**。
- **实机验证（待用户重启）**：①do_grep 读失败 → 结果含失败清单/全失败明确文案 ②工具参数坏 JSON → Chat 显示解析失败（LLM 可自纠）+ 连续 3 次强制退出 → 前端 ⚠️ system 提示（system_notice SSE）③E4-11 验证失败 → 主 Agent 下一轮可见 ④MCP 可选服务器失败 → 状态槽连接后拉取一次显示（每连接一次）⑤子 Agent 降级 → 结果标注 ⑥IM 降级 → DB degraded_reason 可追溯。

#### 新增：知识图谱错误分类与可见化（E3——错误不再静默吞掉：adapter 分类返回 + MCP 透传 + 前端简单提示 + 注入段标注 + 门控不伪装）

- **背景**：四路审计定位 D 类知识图谱 28 处失败位置（~20 静默/3 伪 no_results/5 可见）——查询异常返回 None/空图/空列表，LLM 与用户把"检索失败"误解为"没有知识"。
- **核心机制**：
  1. **adapter 7 读方法错误分类返回**（lightrag_adapter.py）：query 真异常 → 错误文本 `[图谱查询失败: <err>]`（fail_response `""` 真空保持）；query_data/explore_node/get_graph_snapshot → `{"status":"error","message":err}` dict（空壳字段 nodes/edges/center/stats 保持——消费点缺键安全依赖）；timeline_query 内部 error → raise RuntimeError（MCP except 转 error dict）；**has_entity/has_edge 保持 bool 不动**（dict 化会静默破坏去重/前置校验）；`_sanitize_graph_error` E3 专用脱敏（绝对路径剥离 + key/Bearer 复用——内联实现（不模块级导入 litellm_adapter）防循环依赖）；rag None（门控拒绝）→ 通用文案 `知识图谱不可用（初始化门控拒绝）`
  2. **raise 传导链**：adapter 内部 self-callers（search_multi_lightrag/search_by_file_path/**activate_for_query**）遇 error dict → raise RuntimeError → runner 既有 except 捕获 → 标注注入；**activate_for_query 内部 except 对 RuntimeError 重抛**（`except RuntimeError: raise`——error dict 传导至 runner，其余异常保持降级日志）
  3. **MCP 工具层收紧**（lightrag-server）：query_data/search_entities error dict 透传（LLM 可见 message，不再伪装 no_results）；lightrag_query 三分支显式判定（错误前缀 → error dict / 空串 → no_results / 门控通用文案 → error dict——不再裸文本直达 LLM）；TOOL_SCHEMAS 计数断言核正 16→23（陈旧断言）
  4. **kg_api 三端点转错误响应 + 前端简单提示**（graph_snapshot/explore_node/search_entities）：error dict → HTTP 错误响应；renderer 各消费点 `status=="error"` 显式分支 → **简单文案**（"知识图谱不可用/检索失败"——不显示复杂详情）+ 首屏提示一次节流（后续失败仅 console + 状态角标，不重复打扰）
  5. **runner 注入标注**：`_inject_dynamic_resources` 新增 `injection_notes` 累加器（函数起始初始化——parts 在 L2854 才定义），5 处 except 各追加固定标注（`[脑区激活失败，本轮无脑区注入]`/`[技能检索失败，本轮无技能注入]`/`[知识检索失败，本轮无参考知识注入]`/`[脑区状态图生成失败]`/`[脑区知识格式化失败]`）组装前并入 parts——**不泄露 err 详情**（LLM 只需感知"检索失败"；err 进日志）；`_brain_injector_failed` 实例标记生命周期（`__init__` 初始化 False + re-check 块块级互斥置位 `(_activation_mgr is None)`（L2613 子分支之外）+ 成功创建/缓存命中返回路径前置清除 + 消费端 getattr 守卫）——仅置位时追加 `[脑区上下文不可用]` 标注——冷却/防并发/异步/无图谱正常态不标注，恢复后标注消失
  6. **门控层不伪装**（lightrag_manager）：`run_resilience_phase1` check_all 异常 → **ok=True（"无损坏"语义——不触发 launcher 修复弹窗闩锁）+ check_failed=True + error=str(e)**——检测失败 ≠ 数据损坏；`get_lightrag_status` integrity dict 四子路径统一输出 check_failed/error；`need_repair` 公式改写为 critical/major > 0（显式 .get 默认 0 防缺键 KeyError——check_failed 不参与）；region_activation `initialize_from_regions` except pass → 补 warning
- **用户拍板**：①前端简单提示——用户原话"前端对于用户来讲，无需显示那么复杂的，用户看不懂的内容"（不显示错误详情）②**错误与修复方法写入系统管理手册 Troubleshooting 分册**（docs/manual-troubleshooting.md 新增 1.7.2 知识图谱错误分类与修复方法——"把一些可能的错误和修复方法记录到我们的系统管理手册里边"）③E3-09（sync.py skill 同步失败仅日志）显式接受——后台进程有下轮重试机制（skill_sync_state），失败仅日志是既有设计 ④门控原因通用文案（"知识图谱不可用"——不细分修复中/损坏/冷却——门控 warning 日志已含具体原因；最小改动）
- **核销**（error-inventory.md E3 归属 11 条）：**E3-01~08/10 修**（adapter 分类/工具层收紧/get_graph+timeline/门控可见化/resilience 不伪装 ok/注入标注/_get_brain_injector 标记/activate_for_query raise/region_activation 补日志）；**E3-09 显式接受**（用户拍板）；**E3-11 无需修（已覆盖）**——启动期 import 失败响亮（RuntimeError 终止启动），运行时失败在工具层统一 except 可见
- **质量链**：详设 v1.0→v1.13 十四轮双审（A 技术 + B 原则——R13+R14 连续两轮零 bug）+ subagent-driven 实施（T1-T6 每 Task spec+quality 双审——E2 教训：quality 审查不能省）；**关键教训：①explore_node 真空 vs error 区分**——实体不存在（真空 no_results 保持）≠ 查询失败（error dict）——D1/D3/D7 真空保持语义；**②check_failed ok=True 语义**——检测失败 ≠ 损坏——launcher 闩锁防回归（ok:False 会弹修复窗 + 跳过全部初始化 + repair_all 手术作用于可能健康的图谱）
- **验证（Task 6 收尾）**：回归 17 文件 **395 passed 0 新增失败**；12 个 pre-existing 豁免记录：①test_lightrag_adapter.py 9 个（TestIngester* 6 个——`inject_entity` 等写方法 85034d6d 重构移除后测试未更新，2026-05 起就红；TestSearchSkills/Tools/Knowledge 3 个——`filter_by_entity_type(..., "Skill"/"Tool")` 大写实体类型与测试期望小写不符，代码 650690f1 起字节级未变）②test_lightrag_repair_unit.py 3 个真实数据用例（~/.niu 数据增长——2026-08-14 VDB 并发写工程已记录豁免）；E3 契约反转测试全绿（query_data/query rag None → error dict、explore_node/get_graph_snapshot 异常 → error dict + 空壳、lightrag_query 三分支、check_failed 四路径、注入标注断言）；~/.niu/messages.db **65→65 零新增**。

#### 修复：知识图谱界面两问题（macOS 剪贴板快捷键 + 主图右键进子图）

- **问题一（剪贴板）**：macOS 上图谱窗口 Cmd+C/Cmd+V 不生效（搜索框无法粘贴/复制）。
  - **根因（scout 实证）**：darwin 菜单模板（main.js L1729-1751）仅「妞妞」一个顶层菜单、无 Edit 菜单，`Menu.setApplicationMenu` 替换默认菜单后渲染进程编辑快捷键全被吞；**项目既有解法 = per-window `before-input-event` 手动分发**（chat L197-214 / sticky L338-360 / settings L403-419 三处先例），唯独 graph 窗口（createGraphWindow）漏挂。Windows 保留默认菜单 + Chromium 原生支持故正常（用户未报）。
  - **修复**：createGraphWindow 内 loadFile/show 后补挂同款处理器（F12 devtools + `input.meta` → Cmd+V/C/X/A 分发，无 preventDefault——对齐先例）。**不做全局 Edit 菜单**（第二条并行约定 + 菜单栏外观变化 + 三处现有处理器变死代码）。
- **问题二（主图右键进子图）**：右键"以此节点为中心扩散"只在子图态生效；主图态右键是就地展开邻居（expandNode）。
  - **修复**：主图态右键改为 `enterSubgraph(node.id, 1)` + 成功后 `updateSubgraphControls()`——与搜索框进子图（selectSearchEntity）完全同路径（depth=1、聚焦动画、showDetail、加减层级/返回控件自动可用）；**Document 节点守卫保留**（`node._originalData && node._originalData.nodeType !== 'Document'`——force-graph 节点顶层无 nodeType，只在 _originalData 上；R1 双审查员交叉抓出计划初版 `node.nodeType` 守卫恒真失效）；删除 expandNode 死代码（唯一调用点即右键）+ 主图态 tooltip 补"右键点击：以此为中心进入子图"提示（Document 不显示）。
- **质量链**：计划 v1.0→v1.3 三轮双审查（R1 双 CONDITIONAL：P1 Document 守卫字段错 + P2 主进程改动验证需重启应用 + P3 行号；R2 A=CONDITIONAL 仅 P3（行号引用）B=APPROVE；R3 双 APPROVE——连续两轮零阻断达成）+ subagent-driven 实施（Task 1/2 并行，各 commit 独立可回退）；**关键教训：①force-graph 回调节点字段在顶层 vs _originalData 的层级差异——守卫类代码引用字段必须核对对象构造处（buildGraphData L220-228 实证）②main.js 主进程改动验证必须重启应用、renderer.js 关窗重开即可——验证前置条件按代码层区分**。
- **验证**：两文件 `node --check` 通过 + `grep expandNode` 零残留 + 实施 diff 与计划逐字核对；**实机验证待用户执行**：Task 1 重启应用后搜索框 Cmd+V/Cmd+A/Cmd+C/Cmd+X + 详情面板文本 Cmd+C；Task 2 关窗重开图谱后主图右键实体节点 → 进子图（depth=1）→ +/− 层级 → 返回总览；右键 Document 无操作；子图内右键保持原行为。

### 2026-08-15

#### 修复：strip_at_messages 删除回复空行（飞书卡片块不闭合——子 Agent 转述与主 Agent 话直接连接）

- **现象**：飞书 IM 中主 Agent 回复里子 Agent 转述块后主 Agent 的话直接连接（文本无分隔/表格吞并"简单说："入表）；Chat 页面正常（另起一行）。实证：Chat 显示 DB 持久化文本、飞书显示流式卡片累计，两条路径都过 strip_at_messages。
- **根因（实证链）**：LLM 原始输出（raw_http 20260815/000007）`...日志\n\n您看...`（空行存在）→ `strip_at_messages` 的 `if line.strip()` 过滤空行 + 单 \n 重连 → DB（messages.db a01d63e5）与飞书卡片均 `...日志\n您看...`（空行被删）→ Chat（marked）单 \n 显示换行（正常）；飞书 CardKit 列表/表格项内单 \n 折叠/吞并（连接）——同一文本两端渲染差异。
- **修复**：`strip_at_messages` 只做 `_AT_PATTERN.sub('', reply_text).strip()`——@ 消息段剥离 + 两端清理，原文换行/空行结构原样保留。@ 剥离残留空行保留（无害，段落间距）。函数签名/调用点零改动（gitnexus CRITICAL 12 处核验：persist 去重双方同版本一致、纯 @ 回复判定 .strip() 保留不变、ask_user 问题文本无空行结构）。
- **验证**：TDD（空行保留核心断言 2 新）+ 5 回归文件 41 passed 零新增失败（test_at_message_parser 11 / full_text 8 / at_sync_name 10 / persist_dedup 9 / scheduler_sse 3）+ 实机验证待用户重启（飞书卡片块闭合）。

#### 修复：工具调用统一异常兜底（E1——dispatch 整体包裹，循环不死亡/错误 LLM 可见）

- **问题**：主/子 Agent 工具调用抛未捕获异常（do_* 参数畸形如 offset='abc'、disk_engine.execute 裸调用链、chat-with 异步分支、回调穿透）时——子 Agent 循环直接死亡（错误穿透 call_subagent 转报主 Agent，子 Agent 自身盲）、主 Agent 会话中止（chat_queue/compat 降级回复），Agent 看不到报错。
- **根因**：handler.dispatch（agent/handler.py）无统一异常包裹——MCP 两分支有内层 try/except，但内置 do_*、disk、chat-with 路径裸调用；异常穿透 interruptible re-raise → agent_loop 无 catch → 消费端中止。
- **修复（main 2 commits：74bec278 + 3c5fc4a8）**：dispatch 外壳 `try: return (yield from _dispatch_impl(...)) except Exception`（非 BaseException——KeyboardInterrupt/CancelledError 保留穿透兼容停止语义）→ 任何未捕获 Exception 转 `TOOL_ERROR` error dict（error_code + format_error 类型/消息/位置 + 截断保尾 ≤500）进 StepOutcome.data → tool 消息 → LLM 下一轮可见可自纠；错误路径镜像 tool_after_callback 职责（重复调用检测防 LLM 自旋 + 工具状态 end 推送防前端滞留——主 Agent notify_tool_status_sync/子 Agent _push_subagent_event，status 仅 start|end）；format_error 内层兜底坏 __str__（<unprintable> 防二次抛异常）；E1-08 ask_agent 吞异常加 logger.error。
- **核销**：E1 归属 10 条——修 2（E1-02/08）/ 覆盖 5（E1-01/03/04/05/10）/ 无需修 2（E1-07 get() 五调用方可见、E1-09 interruptible re-raise 仅剩 BaseException）/ 重归属 E4 1（E1-06 三序列化点在 dispatch 之后）。
- **验证**：7 新测试（TOOL_ERROR 格式/循环存活/BaseException 穿透/MCP 内层优先/坏 __str__/状态 end 双路径）+ 回归 10 文件 95 passed（6 个 pre-existing 失败基线复现确认非本工程引入）+ 实机验证待用户重启。

#### 新增：LLM 通讯失败错误可见化（E2——错误推前端可见，SSE 只推不落库刷新自然消失）

- **方向定案**（用户原话可引）：**不做弹窗**（"这个应用未必在电脑上操作，可能远端（飞书），你弹窗没有用啊"——远端场景无效）；**逻辑不变**（现有错误处理零改动）；**错误推前端可见**（SSE 事件只推不写 DB——"你写 Message 点 DB 是没有意义的，因为它也自我修复不了呀"）；**不进 LLM 上下文**（"你推消息的这个事还不占用上下文"）；**自愈靠下次调用自然重试**（"临时网络/服务端故障不代表模型坏了，过一段时间自愈了，你再发消息它又好了"）；**刷新 Chat 错误消失**（不落库 → 刷新从 DB 加载历史时自然消失）；**文案三通道**（标准翻译/非标准原文展示/保底不卡死——"绝不能出现识别不了就卡死的路径"）。
- **核心机制**：
  1. **format_llm_error_for_user 三层识别通道**（litellm_adapter.py 纯函数）：① 显式 error_type ② 映射表键名子串匹配（真实 str() 带 "litellm." 前缀）③ 通用正则兜底；映射表 10 键翻译（RateLimitError 429/ServiceUnavailableError 503/AuthenticationError 401/NotFoundError 404/BadRequestError 400/LiteLLMUnknownProvider/APIConnectionError/Timeout/BudgetExceededError/MidStreamFallbackError）；非映射类型 → 类型名+原文（通道 2）；裸原文保底（通道 3）；**保底不变式**（str 强转防坏 __str__ + 任何输入非空不抛异常 + 截断保尾 ≤500）；extract_error_type 同源导出 + is_litellm_error_type 动态判定（hasattr 双模块 + 异常类校验）
  2. **error_type 透传链**：MockResponse 加 error_type_name 字段（llmcore 签名加参默认 None）→ adapter 流中段 except/覆盖点记录 → agent_loop LLM_ERROR 双分支**源头友好化 yield**（函数内绝对导入防 agent_loop→litellm_adapter→runner→agent_loop 循环依赖）→ IM 流式卡 accumulated 友好 → route_out → 飞书 F3 startswith 命中 → 终稿友好
  3. **三入口 LLM_ERROR skip persist**（compat chat_session / chat_queue / chat/sync）：`rv["result"] == "LLM_ERROR"` 显式守卫 → 错误文本零落库（E2-03/04 真实 bug：此前错误文本经 persist_agent_reply 兜底入库）+ message_id=None 初始化（返回处无条件读，防 NameError 500）+ notify error_type 优先 rv 透传
  4. **chat_queue 降级分离**：中性占位符 [系统繁忙，请重试] 落库（错误细节不进 DB）+ 友好文案投递 + **notify 与 persist 解耦**（persist 成败均推）；chat_error 保留异常对象（str() 化后 type() 判定恒 'str'——is_litellm_error_type 失效）；timeout 路径 chat_error="timeout" 字符串 → isinstance BaseException 守卫 False → 中性占位符 + 不 notify
  5. **notify_llm_error_sync → SSE llm_error 事件 → main.js 分发（chatWindow 存在性守卫）→ chat.html ⚠️ system 提示**（不落 DB 刷新消失；_main_loop None 早退 + RuntimeError 守卫，照抄 notify_brain_region_sync）
- **核销**：E2 归属 13 条——E2-02 保留（错误不进 LLM 上下文）/ E2-03/04 改修（三入口 skip persist）/ E2-05 修（降级友好投递）；接受项 E2-01/06-13 逐条理由（详设 §5：弹窗不做/定时任务入 DB/触发矩阵等）。
- **质量链**：详设 v1.1→v1.15 十四轮双审（R13+R14 连续两轮零 bug）+ subagent-driven 实施（Task 1-6 每 Task spec+quality 双审）；**关键教训：测试 patch 目标必须消费方命名空间**（compat 模块级绑定——patch 源模块无效直写真实 DB，15 条幽灵 hello 消息污染 messages.db 事故 + 备份清理恢复；E2 Task2 起测试全部 patch 消费方）。
- **验证（Task 6 收尾）**：回归 10 文件 134 passed（规格列 11——tests/test_tool_registry.py 顶层不存在，实为 tests/test_p0/test_tool_registry.py）+ 2 个 pre-existing mock 形状修复：① test_chat_sse_persist 补 `resp.stream_error = False`（aa38d208 引入 stream_error 检查后 getattr 命中 truthy Mock 误走 LLM_ERROR）② test_llm_error_handling FakeClient 补 `backend = MagicMock()`（53cbc6f9 stop 特性加 `client.backend.stop_check` 后即失败，2026-08-10 豁免清单项）——**本回归文件集内 pre-existing 集合归零**（设计基线另一组豁免——test_agent_loop_tool_results 1 + test_agent_loop_stream_events 5——不在本次文件集内，仍保持豁免状态）；新增 _chat_lock 超时路径测试（chat_error="timeout" 字符串 → 中性占位符 + 不 notify，isinstance 守卫分支锁定）；~/.niu/messages.db 523→523 零新增。

### 2026-08-14

#### 修复：脑区 assign 每次启动全量重注入抵消图边衰减（遗忘曲线失效）——assign 删除 + update_default_region_sizes 提取 + decay 从 detection 解耦

- **问题（实证）**：① 每次启动重注入 1606 条实体→默认脑区归属边（weight=1.0 幂等 upsert 恢复满权）→ 同次同步 decay 只削到 ≈0.996 → 边权重恒水平线永不跌穿 FLOOR_WEIGHT=0.1 → **图边遗忘永不发生**（净效果≈0）；② 定时衰减被社区检测失败门控（detection 异常分支早退跳过 decay）；③ **description 源既有缺陷（R3-P0-1 实证）**——list_entities 经 `_clean_description` 清洗剥掉 brain_meta_* 字段抹平 priority（assign 自 08-02 起已在污染存量 description；同源影响 update_region_summaries region_id 抹空 / dissolve shrink_count 失效）——差异化半衰期（90-360 天）被抹平
- **根因**：`assign_entities_to_default_regions` docstring 自述 "one-time operation"（05-12 引入时设计=一次性填充、零调用点）——06-09 df870087 接线进 region_sync Step 3.5 后即每次同步全量重注入，**"一次性"实现成"每次启动全量重注入"**（run_sync_once_for_startup 只查进程内 Event、绕过 21.6h 跨重启门控 + 每 24h 后台 + consolidate API 三条触发链无条件调用）；归属建边实为 **LLM 设计路径**——dream-evolver 动态注入脑区列表+提示词引导建边、entity-extractor/文档入库提示词注入（brain_region_prompt.py）、技能同步（sync.py L663-671 知识体系包含边）——assign 是多余旧机制
- **修复（main 3 commits）**：
  1. **Task 1（f89a2e5a）**：region_manager.py 删除 assign_entities_to_default_regions（clean cutover——无调用者死代码不留，git 历史可取回）+ 新增 `update_default_region_sizes`（get_all_region_members 单次读——7 次全图拷贝→1 次；**图快照直读原始描述**绕开 _clean_description 清洗 + priority 固定映射自愈（实证用户配置 core/category 旧值——合法值尊重、旧值归一化））**【注意：priority 固定映射自愈为错误做法——已被 2026-08-14 脑区 priority 配置权威工程废止（见下条）——配置原样透传】**
  2. **Task 2（b3a5156a）**：region_sync Step 4.5 替换 update_default_region_sizes + decay 提取 `_run_decay` 从 detection 解耦（**检测异常仍衰减**）+ 早退分支 decay + consolidate 同构替换（Step 4.5 assign→size 更新；无分区早退 L218 仍早退——接受边界）
  3. **Task 3（本条目）**：回归全清单零新增失败 + brain-region-management.md assign 引用清理（归属建边由 LLM 提示词引导自然完成）+ grep 零残留
- **用户拍板**：删除不保留 force 逃生口（LLM 建连接是设计路径）；**启动器提示保留验证**——splash "正在同步脑区状态" + launcher 社区检测警告均不依赖 assign（两处提示与 assign 解耦）
- **质量链**：计划 v1→v19 十九版演进 + **R1-R18 十八轮双审查**（R18 双 CONDITIONAL 同 P2-1 明示补后 APPROVE——实质达成双 APPROVE 终止进入实施；关键教训：description 源改图快照直读（R3 P0-1）/priority 固定映射自愈（R5/R6 实证配置旧值落空）【**该自愈方案本身为错误做法——已被配置权威工程废止（见下条）**】/fake graph 必须显式真实 networkx.Graph（R18 P3-6））+ 每 Task spec+quality 双审
- **实机验证（用户 2026-08-14 19:03 执行——通过）**：① 重启日志**无** `批量注入实体-脑区关系`——改为 `[RegionSync] Updated 7 default region sizes`（新 size 更新路径）② `[Decay] brain region edges: decayed=3185, deleted=0, protected=546` + `[RegionSync] 衰减结果: {...}`（`_run_decay` 新提取路径生效）③ splash `[STAGE] 正在同步脑区状态` + `[StartupGate] Running brain region first sync` 保留 ④ 完整启动无回归（LightRAG Phase 1 全过 + VDB (3242,768) matrix/data 一致 + 85 MCP 工具 + Scheduler/IM/Electron）⑤ 边数 6314（上次 6282——assign 移除后归属边仍增长——**LLM 建连接路径真实生效**）⑥ `距上次同步 0 秒，不足 77760 秒，等待 77760 秒后再首次同步`——21.6h 门控 + 24h 后台循环就位；新 stats 键 `regions_size_updated: 7`/`edges_disconnected: 0` 写入。**长运行 >24h 第 2 次 `[Decay]` 待用户后续确认**（24h 循环——衰减持续执行、无检测门控阻断）

#### 修复：脑区 priority 配置权威——删 `_DEFAULT_REGION_PRIORITY` 写死映射 + 迁移用户配置文件（用户指出严重错误）

- **问题（用户指出）**：`update_default_region_sizes` 用 `_DEFAULT_REGION_PRIORITY`（7 脑区→priority 固定映射**写死在代码里**）自愈旧值——但默认脑区配置的权威来源是 `~/.niu/preferences.json` 的 `brain_regions.defaults`（label/description/priority/keywords 全量定义）——代码写死导致配置与代码不一致、用户改配置不跟随、两处维护漂移——低级错误
- **根因链（实证）**：06-19 优先级体系变更（core/category → permanent/long/medium/short，差异化半衰期 90-360 天）→ `memory/preferences.json` 模板已更新新值 → **用户实际 `~/.niu/preferences.json` 从未迁移**（仍 core/category——launcher 不覆盖已有配置）→ 衰减算法只认新值（`parse_priority_from_description` 旧值回退 medium）→ 差异化半衰期从未生效 → 上一工程用代码写死映射补救（**错误做法——应迁移配置文件而非写死代码**）
- **修复（main 3 commits：4c2d4b0c + f11458ee + T4 文档）**：
  1. **T2（配置迁移，先于 T1 原子落地）**：Edit 工具迁移 `~/.niu/preferences.json` 7 脑区 priority 旧值→新值（与 memory/preferences.json 模板 7/7 一致：聊天历史 medium/文档库 permanent/知识体系 long/人际关系 permanent/工作事务 medium/生活事务 short/组织机构 permanent）——即时生效（`get_default_regions_config` 每次调用重读文件）；验证：json.load 合法 + 模板比对 + label/description/keywords 未触碰
  2. **T1（删写死）**：region_manager.py 删 `_DEFAULT_REGION_PRIORITY` 常量（含注释）+ `update_default_region_sizes` priority 改**配置原样透传** `config_map.get(region_name, {}).get("priority", DEFAULT_PRIORITY)`（删 cfg_priority 合法值分支）+ docstring 更新——**T1/T2 原子性**：live 图 description 已差异化（上工程已改写存量）——T1 单独生效会把差异化降级回旧值（R1 双审查实证）——顺序 T2→T1→重启规避
  3. **T3（测试）**：test_default_region_sizes.py 断言更新（T1-1 改透传形态 + T1-10 permanent→medium + 新增旧值透传判别 + 删自愈断言）——3 相关文件 95 passed 全绿零污染；T4：brain-region-management.md priority 枚举同步新值（permanent/long/medium/short + 半衰期）
- **存量自愈**：live 图 description 已差异化（上工程已改写）——配置迁移后与图一致——无需手动改图
- **质量链**：计划 R1 双审查（A-P2 原子性 + B-P2 断言/豁免/验证步骤）→ v1.1 修正 → R2 双 APPROVE（连续两轮零 bug）→ T2+T1 实施 + PM 复核 diff → T3 断言更新 + 3 文件回归
- **测试污染事故（记录）**：T3 首轮被指示跑全量 pytest → photo 测试真实写图（`person:test-uuid-003` + `test_photo_for_depicts.jpg` 写入 vdb_entities/relationships/chunks/kv_store/graphml 5 文件——19:45 实证）→ 按 lightrag-data-repair 流程修复（备份 `~/.niu/lightrag_storage/fix-bak-20260814-1948/` + 内容匹配精确删除 2/1/2/2/2节点1边 + XML 验证 + 与 19:04 基线核对一致）→ **教训：Niu 测试任务必须写死"只跑指定文件、禁全量"（全量含写图测试）——改动面小就只跑相关文件（用户质疑"为什么全量"正确）**
- **实机验证（待用户执行）**：重启 Niu → 日志 `[RegionSync] Updated 7 default region sizes` 后 description priority 写入新值（配置透传）→ 长运行 24h 后 `[Decay]` 按差异化半衰期衰减（permanent 360 天/medium 180 天/short 90 天）

#### 修复：VDB 并发写混写根治——单写者保证三层防线（launcher 无条件清理等进程消失 + niu_api 启动单实例自检）

- **根因修正（用户纠正后定稿——不是上游 bug）**：nano_vectordb `save()` 非原子（open('w')+json.dump）+ LightRAG 文档明示 "Only one process should updating the storage at a time"——是**单进程前提设计**，单进程内 `save()` 顺序执行完全正常。LightRAG 生产写路径实证无并发写：所有写路径经 `call_async` 同步桥（run_coroutine_threadsafe）收敛到唯一 lightrag-loop 事件循环线程——asyncio.Lock 同 loop 有效、单 client 内存单调、save 串行——单进程内不可能产生两个独立内存快照并发 save。**混写 = 08-11 前的一次双进程并存事故**（数据实证：副本快照 08-11 14:11 matrix 3213 vs data 3211 已差 2；08-11 → 08-14 两文件同步 +14、差 2 保持——单进程正常写不产生新错位）；06:51 越界 = **历史孤儿概率命中**（非新写损坏）。现象链：矩阵错位 → 检索排序错误（李磊 Top 15）→ 孤儿行累积 → 越界崩溃。**双进程来源（我们部署的问题）**：launcher `kill_stale_api_process` 只查端口占用（半死进程不触发）+ pkill 后不等待退出；崩溃残留 + 重启窗口；手动双起——重启窗口/双开窗口期新旧实例并存双双跑整理管线。
- **方案（用户拍板）：不 fork nano-vectordb**（成熟产品不改）——遵守其单进程前提，Niu 侧单写者保证三层防线：
  1. **防线一（Task 1 launcher kill_stale 重写——主防线）**：① 保留 best-effort notify_shutdown 前置（`/api/shutdown` 的 shutdown_pending_futures(1.5s) 是**唯一在途写排空点**——删则 SIGTERM 截断非原子 save；进程最迟 ~4.5s 自灭；等 2s 仅在 notify 成功后执行——端口空闲全新启动 fast path ~100ms）；端口探测**仅作观测日志**——旧代码两处 early-return（初始 health 失败 / notify 后端口释放）**全部移除** ② **无条件 pkill**（唯一清理手段——覆盖半死/不监听端口者）；Windows 用 `Get-CimInstance Win32_Process` 按 CommandLine 匹配（修复破损代码：PS5.1 Get-Process 无 CommandLine 属性 → 旧 filter 恒空不杀任何进程）③ `wait_for_process_exit`（**pgrep 主信号——端口释放 ≠ 进程死亡**：uvicorn graceful 先关 LISTEN socket 再无限等 SSE 在途流）：轮询 pgrep -f pattern 300ms，exit 1 = 消失 → true；exit 0 = 存活继续；**spawn 失败/其他退出码 = 保守（视为存活走超时升级——防误判消失提前 spawn 双写）**；Windows 分支 PowerShell 显式输出退出码（管道空结果退出码仍为 0 会恒判存活）+ try/catch exit 2（查询错误与无匹配区分）④ 仍活着 → **pkill -9 升级**（镜像 cleanup 阶梯）→ 5s 再等 → 仍活着 warn **proceed-anyway**（不阻塞启动）⑤ 端口辅助 `wait_for_port_free`（spawn 前最终确认——进程消失 + 端口空闲双保险）：connect_timeout(300ms) 探测，ConnectionRefused = 空闲；Ok/Timeout/其他 connect 错误一律视为占用继续轮询（保守，500ms 重探间隔）⑥ **调用位置移入 bg 线程闭包顶部**（spawn 之前）——splash 立即出现覆盖清理期（原 main() 同步调用最坏 ~20s 无窗口静默冻结）
  2. **防线二（Task 2 niu_api 启动单实例自检——最后防线）**：2a. `_check_single_instance(port)`——SO_REUSEADDR=1（Windows 不加——setsockopt 语义不同）+ bind 127.0.0.1:port 探测：能 bind = 无实例 → True；OSError = 占用 → logger.error + **sys.exit(1)**；端口读取上移 main() 开头（仅保留一处读取）2b. launcher **health（30×1s）/preload（3600×0.5s）循环每迭代开头 `api_server_child.try_wait()`**——进程早退（sys.exit(1) 场景：spawn 后 2-4s 退出）→ 显式 `cancelled_bg.store(true)` + `phase_tx.send(SplashPhase::CleanupDone)` + return——**绝不能裸 return**（跳过 bg 线程闭包末尾 wait-loop 与 CleanupDone 发送 → splash 永不关闭 + 主线程 while !cancelled 双重挂死，比修复前 30.5min 空转弹配置页更糟）——防空转弹 LLM 配置页
  3. **防线三（既有启动自检兜底保留）**：vdb_matrix_mismatch 检测 + 自动修复（matrix/data 行数比对，从 data.vector 重建 matrix）——**仅修可解析文件**；截断文件（不可解析）走 rfd 手动全量重建
- **实施偏差 D1（记录）**：T1.2 测试的 **bound-not-listening 机制在 macOS 不可行**（Linux bound-not-listening 对新 SYN 回 RST；macOS/BSD 静默丢弃 SYN → connect 超时 → 端口恒判"占用" → 测试必红）——改为 **TIME_WAIT 持口方案**（主动关闭使端口进 TIME_WAIT ~60s：不被并行 bind-0 测试重分配 + 新连接探测得 ECONNREFUSED 读作"空闲"）——实测 **30,000 次 bind-0 零碰撞**（macOS 实证）
- **已知边界（10 条）**：① 不 fork nano-vectordb（用户拍板）——上游 save 非原子在单进程下正常；单写者保证被绕过（手动双起）最坏 = 混写复发——由启动自检兜底修复 ② 双 launcher 竞态：A 的 pkill 可能杀 B 刚 spawn 的进程——B 监控到进程死 → cleanup → 退出（无重启）——收敛单进程 ③ 端口自定义：NIU_API_PORT 改端口时 kill_stale/自检按实际端口（已实证一致）④ 手动绕过（直接 python -m niu_api 双起）→ Task 2 自检兜底（bind 失败退出）⑤ 启动自检兜底仅修可解析文件（matrix/data 行数不一致）；截断文件（不可解析）走 rfd 手动重建 ⑥ 单进程内写串行已实证（call_async 收敛单 loop）——不引入新抽象（无需队列）⑦ **repair 临时 loop 窗口**（单进程内唯一理论双快照窗口：asyncio.run 临时 loop + 自建独立 client）——被 `_repairing` 门控 + 事故时间窗无 repair——本次不修；未来可选：repair 改走 call_async 桥回同一 loop ⑧ **wait 超时 proceed-anyway 兜底链**：wait_for_process_exit 10s + pkill -9 + 5s 仍活着 → proceed（不阻塞启动）——残留进程仍持有端口时被 Task 2 bind 自检兜底（新进程 bind 失败退出）；已释放端口的极端场景（SIGKILL 后 D-state）无兜底 → 由启动自检（vdb_matrix_mismatch）最终兜底 ⑨ Timeout 分支不可测（wait_for_port_free Err(Timeout) 继续轮询——loopback 不可达——防御性代码无测试）⑩ **LLM 门控段 child 死亡残窗**：早退检测仅加在 health/preload 循环——子进程在 preload 完成后、LLM 门控（test-llm 230s）期间死亡仍会走 need_settings 弹窗——目标场景（sys.exit(1) 早退 2-4s）必落在 health 循环内，此残窗为既有行为、超范围——记录；可选低成本补强：LLM 门控首个 HTTP 探测失败时顺带 try_wait 一次
- **质量链**：计划 **v1→v2.12 十二版演进 + R1-R13 十三轮双审查**（产品逻辑链 A + 测试有效性 B，**R12+R13 连续两轮双 APPROVE 零 bug 达成**）——关键 REJECT 教训：**fork 原子写方案**被用户质疑否决（R1 双 REJECT；成熟产品不改）+ 纯 bin crate 无 [lib]（tests/ 无法 import 内部函数 → 内嵌 mod tests 钉死）；**notify 在途写排空前置**（R3 P1-1——唯一排空点，删则截断）；**端口释放 ≠ 进程死亡**（R3 P1-2——以端口判死 → 10s 超时 proceed 重建双写）；**2b 裸 return P0**（R4 双审同时抓出——挂死比修复前更糟）；Windows **PowerShell 无匹配退出码 0 恒判存活** + **PS5.1 Get-Process 无 CommandLine**（R6/R7/R8——CIM + 显式退出码 + try/catch exit 2 + 旧 kill 代码破损实证）；**pgrep pattern 与 spawn cmdline 必须真实匹配**（R5/R6——随机时长方案 + 子串匹配勿锚定 argv[0] 全路径）；**T1.6 随机时长下限 1600ms**（R8 P1-2——600ms < 观察窗口 ~0.9s → flake ~3.3%）；**T1.4 双向握手确定性序列 + 删 elapsed 断言**（R8 双审——send 与 drop 无 happens-before → 首探竞态）；实施 subagent-driven 每 Task spec+quality 双审
- **验证**：`cd launcher && cargo test` **17 全绿**（内嵌 mod tests 9 = 4 既有 + T1.1/T1.2/T1.3/T1.4/T1.6 五新 + 既有集成 4+4）+ `./launcher/build.sh` 成功（产物 ./niu 更新）+ `python/bin/pytest tests/test_single_instance_check.py` **8 全绿**（T2.1-T2.8）+ `tests/test_lightrag_integrity_check.py` **9 全绿** + __main__ import 回归（test_brain_region_api + test_http_log_router_conditional 9 全绿——Task 2a 模块级零破坏）；回归备注：计划清单中 test_lightrag_repair_unit.py 的 5 个真实数据用例因 ~/.niu 数据增长（硬编码期望与实际不符）预存在失败——本工程零 diff 该文件、不 import __main__，非本工程引入
- **实机验证（2026-08-14 12:38 用户执行——通过）**：重启 Niu 日志逐项对照——① `pkill: no matching stale API process (fresh start)`（info 级——P3 打磨生效，fresh start 不再误导 warn）+ `Stale API process(es) exited after pkill` + `Port 9876 is free, safe to start`——**防线一 fresh start 路径正常**；② `[PROCESS-START-MAIN] PID=87998 entered main()` → 直接 `Starting Niu API Server on port 9876`——**防线二单实例自检 bind 探测通过**（无已有实例，sys.exit(1) 未触发）；③ `Phase 1 完成: check_ok=True, critical=0, major=0, minor=0` + `vdb_internal` 检查通过——**防线三兜底健康**；④ LightRAG 加载 `(3230, 768)/(6279, 768)/(1122, 768)`——**matrix 行数与 data 条数一致（无孤儿）**；⑤ 完整启动流程无回归（85 MCP 工具/LightRAG eager/脑区首同步 1606 实体/preload complete/LLM ready/Electron/IM gateway/scheduler started）——启动总耗时 ~68s 正常。已知 P3 观察项：`Stale API process(es) exited after pkill` 在 fresh start 也打印（措辞略误导——pkill 无匹配时的确认日志）——可后续条件化，无功能影响

#### 修复：启动自检检不出 VDB matrix/data 不一致（孤儿向量越界崩溃）——检测 + 自动修复

- **现象**：`query_data("李磊")` 报 `LightRAG query_data failed: index 3225 is out of bounds for axis 0 with size 3225`（2026-08-14 06:51 实证）——脑区点亮/注入全部依赖 query_data，图谱损坏直接瘫痪。
- **根因链（实证）**：三个 vdb 文件全部内部不一致（2026-08-14 实测）——`vdb_entities.json` matrix 3227 行 vs data 3225 条、`vdb_relationships.json` 6266 vs 6265、`vdb_chunks.json` 1097 vs 1095（各差 1-2 个孤儿向量）+ entities 尾部 ~7 条错位——LightRAG fork 注释警告 "Only one process should updating the storage at a time"——跨进程并发 upsert 各持 data 快照、各自 append matrix → 保存互相覆盖；nano-vectordb `_cosine_query`（site-packages dbs.py L169-182）`filter_index = arange(len(data))` → `filter_index[sort_index]`（sort_index 索引 matrix 行）孤儿行号 ≥ len(data) → 越界崩溃。
- **检测盲区（本工程修复对象）**：`lightrag_integrity._check_vdb_missing` 只做**单向**检查（GraphML 节点 ⊆ vdb 向量）——不读 matrix——matrix/data 行数不一致完全不可见——启动检测"健康"但查询必崩。
- **修复（main 5 commits）**：
  1. **检测**：`_check_vdb_internal` + `_load_vdb_full`——vdb_entities/vdb_relationships/**vdb_chunks** 的 matrix 行数（base64(float32) ÷ 4×embedding_dim）vs data 条数，不一致 → major `vdb_matrix_mismatch`（check_all 第 4 步 + checks.vdb_internal 键）；matrix 键缺失（旧格式）跳过不误报、空 matrix（0 行）+ 非空 data 也报 mismatch
  2. **外科修复**：`_decode_vdb_vector`（base64(zlib(float16)) → float32 L2 归一化——matrix 存归一化行，NaN/Inf 非有限范数拒绝解码防 NaN 行写回）+ `_repair_vdb_matrix_inplace`（data 是权威，逐条解码 → vstack 重建 matrix → `_atomic_write_json` 原子写回；任一条解码失败不写回 status=error 走全量重建）+ `auto_repair_vdb_matrices` 编排（mismatch 门控——只修真不一致文件）
  3. **启动接线**：`run_resilience_phase1` 检测到 `vdb_matrix_mismatch` → 自动修复 → 重跑 check_all → 门控用修复后结果（**唯一自动修复路径**——真相源 corrupt / vdb_missing 仍走 rfd 弹窗）
- **验证**：Task 1-3 测试 23 passed（9 检测 + 11 修复/编排/格式 + 3 接线）+ 回归零新增 + Task 4 真实损坏副本端到端（2026-08-14 实执：三文件副本检测 data=3225/6265/1095 vs matrix_rows=3227/6266/1097 + 3 个 `vdb_matrix_mismatch` → `_repair_vdb_matrix_inplace` 三文件 status=ok、修复后 matrix_rows==data（3225/6265/1095）→ 模拟 `_cosine_query` 三文件 `query OK（无越界）`——真实 ~/.niu 数据零改动，副本验证后清理）
- **已知边界**：① 并发写根治（LightRAG 跨进程 upsert 串行化）超出本工程范围——自检修复后再次并发写仍会复发，检测+自动修复为兜底 ② nano-vectordb `_cosine_query` 越界保护是依赖层（site-packages 安装产物，pip 覆盖）——不做——修复后 matrix/data 一致即不越界 ③ float16 量化误差 ~1e-3（data.vector 实测已归一化（范数 0.9999~1.0001）→ 重建误差更小；测试断言容差 <1e-3）④ data 条目本身损坏（vector 解码失败）→ 修复拒绝写回，走全量重建弹窗 ⑤ matrix/data 同数但内容错位（无行数差）检测不到——本次实机形态是行数不一致（检测可触发），重建天然按 data 顺序对齐也修正尾部错位；同数错位无实证，接受 ⑥ `vdb_matrix_format`（matrix 字节不可整除——罕见，截断写通常表现为 JSON 解析失败）不在自动修复触发条件内，走 rfd 弹窗 ⑦ 顶层 JSON 非 dict/解析失败的损坏 vdb 文件会被 `_load_vdb`（_check_vdb_missing）与 `_load_vdb_full`（_check_vdb_internal）重复报错（major 计数虚增一倍）——均为 major、不触发自动修复、布尔语义不变，真实三文件均正常解析——接受（R8-P3 记录）
- **实机验证（待用户执行）**：重启 Niu → 启动日志见 `[LightRAG] 检测到 vdb 内部不一致（3 个文件 matrix/data 行数不匹配），自动修复...` → 三个 vdb 文件 matrix 行数变 3225/6265/1095 → `query_data("李磊")` 正常返回 → 脑区点亮恢复

### 2026-08-13

#### 修复：LLM 不可用启动门控（模型不通时不启动依赖 LLM 的后台组件，只等配置成功后重启）

- **现象**：启动检测到大模型不通（配置缺失或连通性失败）→ 启动器弹配置页 → 但后端不阻塞：scheduler 在第 3 步无条件启动 → `_delayed_start` 三阶段（ready 180s → frontend_ready 60s 超时 proceed → sleep 2s → start）→ 10s 后扫描过期任务 → trigger_callback 入队 ChatQueue → runner.chat → LLM 不通 → **定时任务全部失败**
- **根因（实证）**：配置页与后端启动进程**零耦合**——① `signal_scheduler_ready` 只被 LightRAG/脑区门控，从不被 LLM 配置门控；② scheduler Phase2 `frontend_ready_event.wait(60)` 超时 proceed-anyway 是配置页期间启动的直接使能点（settings 窗口不连 SSE、不调 /api/frontend-ready）；③ 启动器"阻塞"只发生在配置流程结束后（need_settings → 配置页 → test-llm 通过 → notify_shutdown → 退出进程），期间后端已完整启动；④ 用户记忆的"之前修过"= 启动器 LLM settings flow，只挡流程结束后的进程，不挡流程期间的后端后台组件
- **修复（main 7 commits：5 功能 + 2 清理）**：
  1. **compat.py 提取 `_probe_llm`**（test-llm 端点内部逻辑 → 模块级函数，read_timeout/wait_timeout 参数化，入口键名归一化）——端点与启动检测共用同一探测核心
  2. **新建 `niu_api/llm_ready.py`**：`resolve_probe_budget`（预算解析 + **逃生口：user-config llm.read_timeout 覆盖（≤190s——wait ≤220 < 三方客户端 230s；float/bool/isfinite/非正/超上限全防护）**，check_llm_ready 与 test-llm 端点共用，>120s 慢模型全链路生效）+ `check_llm_ready`（存在性 + 真实连通性探测，**预算 read_timeout=120 / wait_for=150**——覆盖代码库显式支持的 20-120s 首响应推理模型；短预算会误杀慢首响模型 → 配置页保存被测试闸门挡 → 永久降级循环 = 回归）
  3. **lifespan 门控**（__main__.py + lightrag_manager.py）：embedding 后、start_scheduler 前插检测——**flag 前置时序**：check_llm_ready **之前**先 `set_llm_gate_ready(False)`（慢探测窗口内 daemon 触发的 probe 全被跳过）、探测结束后置 `llm_ready`；`llm_ready=False` 时跳过 scheduler/HAWatcher/IM gateway/db_monitor/脑区 gate/signal/region 背景同步 + lightrag_sync（`auto_start=llm_ready`，保留既有 try/except）+ response_format 后台探测（lightrag_manager 模块级 `_llm_gate_ready` flag + `set_llm_gate_ready`）；**ChatQueue 照常启动不 pause**（配置页无消息源；瞬态分歧场景消息走 LLM 失败降级自愈）；**preload_complete 无条件置位**；shutdown 段零改动即安全
  4. **启动器三处对齐**（main.rs）：test-llm 客户端超时 25s→230s（LLM 验证 + 配置页轮询）+ **preload-status 轮询迭代上限 360→3600**（实证：lifespan 阻塞时 uvicorn 不接连接、每轮 ≈0.5s，原 360 轮 ≈180s 墙钟——慢模型探测 150s+初始化会耗尽致无限重启循环；3600 轮 ≈30min）+ **settings 轮询 sleep 后 POST 前二次窗口关闭检查**（v2.6.4：关窗在 sleep 期间立即退出，消除确定性 2-3.5 分钟退出延迟；残余在途 POST 竞态 ≤230s 接受）
  5. **settings 前端**（main.js + windows/settings/index.html）：socket 超时 100s→230s（三处含 fallback 直连）+ 过时注释更新 + **testAndSave body 合并文件 llm.read_timeout + saveConfig 写回保留**（index.html——testAndSave/saveConfig 在此文件非 main.js；existingConfig 变量；UI 保存不再抹掉逃生口覆盖）；**启动器配置页流程零改动**（既有 need_settings → settings 窗口 → 轮询 test-llm → 通过 → 退出重启已满足"配置成功后重启"）
- **质量链**：scout 全量启动链探索 + 细节 scout → **计划审查 R1-R10 十轮双审查**（R1 双 REJECT：预算分歧 P1 + LiteLLMSession patch 目标 P1；R3 一 APPROVE 一 REJECT：read_timeout 60s 封顶误杀 60-120s 首响模型 P1 + ChatQueue 单向闩锁 P2；R4 双 REJECT：>120s 模型回归定性 + 逃生口 P2 + 瞬态分歧会话级副作用 P2；R5 双 REJECT：逃生口未闭环到端点 P1 + preload 墙钟 180s 无限重启 P1 + probe 保存闸门 P2 + llm_ready 传参 NameError P2；R6 双 REJECT：逃生口 wait 超出三方客户端封顶 P1/P2 + 端点接线零单测 P2；R7 双 REJECT：Step 4.5 改错文件 P2 + 保存腿声明与 probe 闸门矛盾 P2；R8 一 APPROVE 一 REJECT（Task 4 commit 缺 index.html P2）；**R9+R10 连续两轮零 bug 通过**）→ subagent-driven 实施（T1-T4 每 Task spec+quality 双审；实施中修复：_hang 签名（计划测试缺陷）、T2 ruff F401 + 日志常量 + 负数断言、T3 ruff F401 + global 死语法、T4 settings 轮询 P2）→ 35 新测试全绿 + 回归无新增失败 → **实机验证待用户执行**
- **实机验证（待执行）**：配置无效（改坏 key）→ 启动 → 后端日志 `[LLMGate] LLM 连通性检测失败` + 无 `Internal scheduler started` → 配置页 → 配好 → 重启 → `Internal scheduler started` 正常；正常配置回归
- **已知边界**：① **>120s 首字节模型**：patch 前可用、patch 后两端一致判定失败弹配置页——**逃生口：user-config llm.read_timeout 覆盖（≤190s，wait ≤220 < 三方客户端 230s）**，对门控 + 启动器验证生效；**配置页保存仍受 probe 保存闸门（⑨）限制——>10s 首字节模型经 UI testAndSave 保存必败，需手改 user-config.json**；settings UI 保存保留覆盖（v2.6）② llm_ready=False 时 `/api/scheduler` 端点用 get_store()（返回 200、任务可见但永不触发——非 500）③ 启动器 `/api/llm-status` 判空无 is_local 豁免（pre-existing——Ollama 空 apiKey 场景启动器仍弹配置页）④ lifespan 门控决策无单测（对齐 LightRAG v7 先例，靠双审查 + 实机验证）⑤ ChatQueue 不 pause 后，llm_ready=False 期间若有消息入队（理论无源）走 LLM 失败降级回复 ⑥ **瞬态分歧会话级副作用**（启动瞬间 LLM 短暂不可达 + 启动器验证前恢复）：后台组件整会话跳过、无用户可见通知——需**手动重启**恢复全量 ⑦ **挂起 provider** 弹配置页前总等待 ≈455s（默认）/ ≈445s（逃生口）——预算覆盖 20-120s 首响模型的设计取舍 ⑧ 配置页轮询 sleep 后二次窗口检查（残余在途 POST 竞态 ≤230s）⑨ **settings 保存双闸门**：testAndSave 还须 probe-response-format 返回 supported——probe 端点 read_timeout=10 → 首 chunk >10-20s 模型 probe_failed → "配置未保存"——pre-existing，遇真实慢模型场景单独扩展

#### 修复：定时任务小时级 cron 当天只执行一次（last_executed_date 日期粒度缺陷 → 触发点级记账）

- **症状**：定时任务 `10 9-23 * * *`（每整点后 10 分钟检查邮件）9:10 执行一次后当天再不执行——`last_executed_date` 每日去重（为日级任务防崩溃重跑设计）把"当天已执行过"当成"当天不再需要执行"，吞掉当天全部后续触发点（DB 实证：triggered_at=11:10:04 被扫描但跳过、scheduled_at 被 reschedule 到 12:10）
- **根因**：日期粒度无法区分场景 A（日级崩溃重跑，应跳过）与场景 B（小时级当天最后触发点 23:10 到期，应执行）——两者数据形态相同（last_executed==today + 到期 + next=明天）
- **修复**：新增 `last_executed_trigger` 列（触发点粒度，存上次执行的触发点 scheduled_at）+ try/except ALTER 迁移；跳过判断改 `scheduled_at <= last_executed_trigger`（同触发点再到期=崩溃重跑→跳过；不同触发点=当天多次→执行）——23:10 最后触发点也执行（15/15），判断不依赖当前时刻（无墙钟问题），旧数据 NULL 部署当天即自愈。**取舍**：回调在途崩溃场景从"漏执行"转向"可能重复"（at-least-once，窗口=回调期+落库间隙，对幂等任务良性）
- **质量链**：计划 R1-R6 六轮双审查（v1 next_time.date() 方案 R1/R2 REJECT：23:10 丢失 P2 + 测试墙钟 P1×2；v2 触发点级 R3/R4 REJECT：CREATE TABLE 示例缺列 P1 + 测试 mock 缺复现形态 P1 + 索引映射 P2×2 + 验证命令问题；v3 R5/R6 双 APPROVE：R5 补 at-least-once 取舍文档 P2 + 行号/论证 P3×2，R6 补断言强化/格式不变式 P3×4——v3.1 修订后实施）；与 2026-07-31 cron 高级修饰符改造（#/L/LW，仅 cron_parser.py）修改面不相交

#### 修复：强制压缩链路四联问题（提示词瘦身/拒绝报告/首次停思考链/截断报错）

- **问题**：①压缩 task prompt（_build_mode2_prompt/_build_force_prompt）内联重复 system 提示词（context-manager.md）已有方法论约 120 行（三份划分/会话单元/工具级联/摘要规范/转义），指令性不足；②prompt 强制"先写 <analysis> 再输出三行"→ 模型写长报告占掉输出预算（min(contextWindow×0.16, 65536)=32000）→ keep=/update=/cursor= 在末尾被截断 → finish_reason=length；③B1 三连重试不改参数（thinking 仍开、max_tokens 不变）→ 每次重试重复截断，3 次耗尽转 COMPACT_TRUNCATED；④降级链 step1 才关思考链（"第一轮失败第二轮才停"），思考链占输出 token（doubao 实证：max_tokens=5 探测时思考链跑 ~172 token 挤压 content 至截断）
- **修复**：①task prompt 瘦身为"任务参数 + 严格输出契约 + 禁止报告"（删方法论重复，方法论信任 system；保留 CRITICAL 门控短语（context-manager.md L194-199 用它判定模式二"一轮方案"分支）+ force 专属参数（上次压缩游标/dream 安全边界））；②新增 _build_compress_llm_config helper（max_tokens + thinking disabled）统一 4 处压缩调用点（compat 模式二/模式一/force + runner force）——首次调用即停思考链，B1 重试复用同一 config 自动继承；③_probe_llm 检测 finish_reason=length → 报"输出被截断"（曾静默判"模型测试通过"——raw_http 实证），探测 max_tokens 5→256（thinking 模型 content 有空间，防标准 thinking 模型 50 截断误杀启动门控）；④降级链 thinking_enabled 判定自然跳过已关思考链的 step1 空转
- **已知边界**：①流被中断吞掉 finish_reason chunk → 截断仍静默（无法可靠检测，未修）；②B1 重试对非压缩子 Agent 保持原行为（通用路径 client 重建成本高，压缩路径已由首次注入覆盖）；③context-manager.md system 提示词本身不瘦身（358 行完整方法论文档保留）；④模式一（睡眠整理非破坏性）注入 thinking disabled 但不走三行输出契约（工具化操作，不受影响）
- **质量链**：计划 R1-R7 七轮双审查（v1-v6.1，连续两轮双 APPROVE 达成——R6+R7；测试定义缺陷 15+ 个经审查抓出：PEP 479/子串断言/MagicMock 自动真值/锚点漂移/魔法短语门控/第 4 调用点等）；实施 subagent-driven 每 Task spec+quality 双审（Task 1 quality 2 P2 修复、Task 4 quality 2 P2 修复）；commits 172146da→dde63a28

#### 修复：压缩后空壳 assistant 消息残留（悬空清理扩展 + 原始形态严格保留）

- **问题**：用户发现 messages.db 存在空记录（如 08-12 23:40 两条 content='' + tool_calls='[]' 的 assistant）。根因机制（代码实证）：压缩对 content 空的消息 clear_tool_calls（模式二/force 对受保护锚点、模式一对空 content 消息）会产生空壳（content 空 + tool_calls 空 + tool_call_id 空）——`_cleanup_orphan_tool_messages` 只清理孤立 tool 消息，不处理空壳 assistant。具体空壳的产生路径（压缩 vs 模型空响应）未完全证实——清理是条件式删除，无论来源都安全
- **修复**：`_cleanup_orphan_tool_messages` 扩展——新增 `_is_empty_shell_assistant`（四条件严格判断：role=assistant + content 空 + tool_calls 空 + tool_call_id 空；JSON 解析失败保守保留）；三处压缩路径全覆盖（模式二/force 既有调用 + 模式一完整性检查后补调）；**原始形态（content 空但 tool_calls 非空）严格保留**（工具调用锚点——agent_loop 还原锚点、tool 消息靠 tool_call_id 归属，删除会丢失工具结果上下文）；存量 2 条空壳一次性清理（WAL 安全备份 + 精确条件删除 + 验证 4 条锚点完好——含今日活跃会话新锚点）
- **已知边界**：原始形态不迁移（把 tool_calls 移到上一条有内容消息的方案因多消息一致性风险高不做——YAGNI，空壳清理已解决残留问题）；非压缩路径（模型空响应等）产生的空壳需等下次压缩清理
- **质量链**：计划 R1-R4 四轮双审查（R1/R2 REJECT 含 WAL 备份/模式一缺口/根因归因/mock 形态等；R3+R4 连续两轮双 APPROVE）；实施 subagent-driven 每 Task spec/quality 双审；commits 354581f6（Task 1）+ Task 2 纯 DB 无 commit + 本条目

#### 修复：主/子 Agent 提示词 Current Time 每轮实时（启动固定 → 每轮 LLM 前刷新）

- **问题**：主 Agent 提示词 Current Time 永远不变——`NiuRunner.__init__` 里 `datetime.now()` 只算一次存 `self.dynamic_system_prefix`（注释明文"启动时固定，不每轮更新"），`_assemble_system_message`（每轮 LLM 前由 `_on_before_llm` 调用）复用固定值——所有轮次 Current Time = 进程启动时刻；子 Agent 的 `system_message` 在 `call_subagent` 启动时构建一次（build_subagent_system_segments 实时取启动时刻），长任务（context-manager 压缩 20+ 轮、dream-evolver 跨午夜）会话内时间漂移——dream-evolver 用日期建 `YYYY-MM-DD会话` 节点，跨午夜有真实风险
- **修复**：主 Agent——`dynamic_system_prefix` 拆出 Current Time（只留 disk_desc 启动缓存——磁盘结构运行期不变），`_assemble_system_message` 每轮 `datetime.now()` 实时生成；子 Agent——新增 `_refresh_subagent_current_time`（on_before_llm 回调，正则替换 system 的 Current Time 行，兼容 str/Claude list 两种格式），`call_subagent` 三处 `_run_agent_loop`（新任务/恢复/异步）统一传入——会话内每轮 LLM 前刷新
- **不受影响**：prompt cache 设计（动态段本就不 cache、静态段在前字节稳定，后部变化不影响前缀命中——Claude cache_control/字符串 prefix cache 均无破坏）；`build_subagent_system_segments` 启动实时语义保留（首轮即正确）
- **质量链**：计划 R1-R4 四轮双审查（R1 双 REJECT：_run_agent_loop 缺 on_before_llm 参数+转发；R2 双 REJECT：异步接线缺锁/红相预期；R3+R4 连续两轮双 APPROVE）+ 每 Task spec/quality 双审；commits 12f2674b（Task 1 主 Agent）+ 50a57196（Task 2 子 Agent）+ 本条目

#### 修复：LLM 启动探测去重（单一真相源——正常启动从两次真实探测降为一次）

- **问题**：每次正常启动触发两次真实 LLM 探测（2026-08-13 20:13/20:14 实证：lifespan check_llm_ready + 启动器 test-llm 各一次，均 max_tokens=256 "hi" 探测）——上午启动门控工程新增后端 lifespan 探测时未盘点既有启动器 test-llm 验证（6/23 d1cbb953 引入），两进程并行互不知晓、无共享状态（LLM 就绪状态无单一真相源）→ 每次启动多一次 LLM 调用 + 4-6s 延迟。十轮门控审查全部聚焦"LLM 不通时怎么办"，无人从正常启动路径审视"探测几次"——点式审查盲区实证
- **修复**：llm-status 从"仅配置存在性"升级三态——ready（配置存在 AND lifespan 探测通过）/ probe_failed（配置存在 AND 探测失败）/ not_ready（配置缺失）——读 lightrag_manager._llm_gate_ready（新增对称 get_llm_gate_ready）；启动器决策改三态：ready 直接启动（不再 test-llm）、probe_failed 保留 test-llm 兜底（230s——配逃生口（wait≤220<230s）后慢模型可被放行；未配逃生口时 >120s 首字节慢模型后端 150s 强杀失败 → 配置页，与修复前一致）、not_ready 配置页（现状）
- **探测次数全景**：正常 1（原 2）/ 配置缺失 0 / 探测失败（快速失败/慢模型 >120s 未配逃生口/挂起 wait_for 生效）3→配置页（原 3）/ 挂起 wait_for 失效 >230s 2→proceed-anyway（原 2）/ 逃生口 1（原 2）——唯一行为变化 = 正常启动跳过重复 test-llm，异常路径行为等价（无死循环）
- **已知取舍（R1-A5 记录）**：正常路径从"2 次探测都有机会抓启动瞬间瞬态故障"降为"1 次"——若 lifespan 探测通过后、启动器决策前 LLM 恰好不可用（极窄窗口），修复后直接 proceed（运行期走 LLM 失败降级）——与边界⑥同族但检测窗口缩短——低概率、可接受（去重目标固有取舍）；升级兼容：新启动器+旧后端（无门控）probe_failed 缺省 false → 跳过 test-llm 失去唯一真实验证——混窗仅手工混用，文档化接受
- **质量链**：计划 R1-R4 四轮双审查（R1 双 REJECT：patch 目标错误 P0 + 探测次数表预算语义 P2×2 + 计数方法 P1；R2 双 APPROVE 后清零：Task 3 mtime 限定 P2；R3+R4 连续两轮双 APPROVE）+ 每 Task spec/quality 双审（Task 1/2 PASS）；commits e63d38ea（Task 1 llm-status 三态）+ a83c9868（Task 2 启动器三态决策）+ 本条目
- **实机验证（2026-08-13）**：重启后正常启动仅 1 次探测（raw_http max_tokens=256+"hi" 特征请求恰 1 个——修复前 20:13:45/20:14:37 两次实证）；异常路径未实测（改坏 key 场景可选后续抽验）

#### 修复：脑区注入分级（点亮数感知熄灭加速 + 分级注入 🟢5/🟡3/⚫0 + 图遍历移除 + 会话边界清理）

- **问题**：活跃脑区注入的内容"总是不太满意"（2026-08-13 21:42:46 日志实证：4 🟢 点亮但 `### [活跃脑区知识]` 5 条含 applescript——与雄安工作主题无关，对照参考知识段明显更贴题）。三层根因：① **名实脱节**——活跃脑区知识段（runner.py L3006-3013）= 衰减池 `get_top_by_source("graph_traversal", 5)` 即"向量命中实体的 1 跳图遍历邻居"全局混排——非每脑区 N 条、不检查脑区归属、无相关性排序（所有邻居同分 = 平均 hit × 0.8，top 5 实质=图遍历顺序前 5）；`activate_for_query` 构建的命中实体数据被 runner L2810 丢弃（注释"返回值丢弃（激活副作用已发生）"）——脑区激活只贡献状态图、不贡献一行知识内容 ② **图遍历 `_traverse_from_hits`（L2717）是噪声放大器**——applescript 来自组合 query 中"版本升级/技能授权"语义命中实体的邻居（实验实证：命中实体本身无噪声）③ **警告口径与 🟢 不一致**——`format_region_map` lit_count 用 >0.3（含黄灯）——警告数的是"活跃"不是"点亮"
- **修复**：
  1. **点亮数感知熄灭加速**（region_activation.py decay_all）：🟢 点亮数 >6 时衰减指数加速——`factor = 0.92 ** (1 + 0.15 × (lit − 6))`（lit=8 → 0.897）——亮的越多熄得越快，程序层自动收敛（警告 LLM 不关注）；`activate_regions`（命中 → 1.0）不变——当前话题脑区每轮拉回满值，加速只压"无命中持续衰减"的冗余脑区
  2. **分级注入 🟢5/🟡3/⚫0**（region_injector.py）：`activate_for_query` 返回值扩展——region_knowledge（每脑区首条 desc）→ region_entities（region → 命中实体 dict 列表，保持相似度序）；新增 `_recent_region_entities` 最近命中缓存（**合并更新**——只覆盖本轮命中脑区、保留未命中脑区旧条目——🟡 档数据源；覆盖更新会使 🟡 恒空）；`format_region_knowledge` 按激活度分级取数（🟢 本轮命中优先、缓存回退 top 5；🟡 缓存 top 3；⚫ 跳过）——激活度 ↔ 注入量严格非减；全局上限 `_REGION_ENTRY_CAP = 26` 条逐条准入截断（防最坏 10🟢×5=50 条）；黑名单过滤（entity_type `.lower()` + entity_name case-sensitive 精确，与 runner 常量同步维护）+ seen_names 去重（与参考知识段同源）
  3. **图遍历移除**（runner.py clean cutover）：`_traverse_from_hits` 方法 + step 4 整块（hit_distance_map + graph_traversal 衰减池注入）+ step 3 all_hits 收集删除（保留块间 stop 守卫）；decay_pool 删 `get_top_by_source`（无生产调用者）、source 注释更新；旧进程残留 graph_traversal 条目经 category 消费 ~7-13 轮出池自愈——**历史条目"动态注入四处可中断"（2026-08-10）→ 四处变三处（_traverse_from_hits 已删）**
  4. **口径统一**：警告阈值 >0.3 → >0.7 与 🟢 统一（6 个黄灯不再虚警"点亮"）；format_region_map lit_count 与加速阈值 >6 同口径
  5. **会话边界清理**：/api/chat/clear 原只清衰减池（`_decay_pool.clear()`）不清 injector 缓存——激活管理器为跨会话单例 → 新会话前 ~11-15 轮持续注入上一会话缓存实体（🟢 top5 + 🟡 top3 可占满 cap 26，直接复现"注入不满意"场景）——新增 `clear_recent_region_entities()` 接线 `_decay_pool.clear()` 同处（compat.py L2549-2550）
- **实验证据**（2026-08-13 两次只读实验 /tmp/brain_inject_test.py + test2.py，真实图谱 ~/.niu/lightrag_storage，query=雄安分行/筹备组/揭牌/征迁补偿/千年秀林/智慧民生/数字人民币/指挥室）：
  - `query_data(mode=local, top_k=5/10/20/30)` 命中实体质量**全部相关**（银企直连平台/千年秀林工程/智慧民生平台/河北分行…）——检索方法没问题
  - 命中 20 实体 **17/20 属于脑区成员**（图谱"包含"边构建）；图遍历邻居 40 个 **36/40 属于脑区成员**且集中点亮脑区——脑区数据健康
  - 命中实体按脑区分组：知识体系 8 / 工作事务 4 / 组织机构 2 / 文档库 2——每脑区取 top N 充足
  - applescript 只出现在组合 query（版本升级/技能授权语义）命中实体的**邻居**——命中实体本身无噪声——图遍历邻居是噪声放大器；组合 query 本身正确（用户拍板不动——程序不做语义判断）
  - **实验裁决**：脑区归属门控收益小（36/40 已自然属于点亮脑区，且会误杀"三农/企业网银"等无归属好内容）——不引入门控；改做图遍历删除 + 分级注入 + 熄灭加速
- **已知取舍**：① 图遍历删除——命中实体已够好、邻居是噪声源、省一次图快照+遍历；若未来需"相关实体扩展"可重加（黑名单/排序完善后）② 加速指数系数 0.15 保守（lit=8 → 0.897 不激进），按实机效果可调参；1.0 满值 3 轮无命中掉出 🟢 属合理边界 ③ 黄灯用最近命中缓存（命中即 🟢 使"本轮命中黄灯"不可达）——缓存条目无龄期衰减，同 🟡 脑区持续注入相同 top 3 ~8-11 轮（🟡 生命周期）——接受（语义="最近相关"；掉 ⚫ 后不再服务）④ 上下文成本上限 26 条 ≈ 6.5K tokens ≈ prompt 的 12%——可接受；🟢 档与参考知识段同源，seen_names 去重后常剩排名 11-20 尾部实体——活跃脑区知识段成为参考知识段的补充视角 ⑤ 手动激活/工具强化（无本轮命中）脑区 🟢 档 0 条——接受（无命中即无相关内容可注入，不构成噪声）⑥ cap 触发时第 6+ 个 🟢 脑区截断、随后掉 🟡 时缓存可输出 3 条 > 🟢 时 1 条（短暂反转）——仅最坏场景可达，已接受
- **质量链**：计划 R1-R34 **三十四轮双审查**（每轮 2 审查员——A 产品逻辑链 + B 测试有效性；**R33+R34 连续两轮双 APPROVE**）——关键设计缺陷演进：🟡 档生产不可达 → 最近命中缓存（R1）；覆盖更新时序恒空 → 合并更新（R2）；🟢 只读本轮命中非单调 → 缓存回退（R3）；step6 region_entries 作用域 NameError（R4）/无 try/except 惯例（R7）/desc None 逃逸（R8）；上下文预算最坏 50 条 → cap 26 全局上限（R15）+ 准入粒度与 26 恰好边界（R18/R20/R22）；**缓存无会话边界生命周期 → clear_recent_region_entities 接线（R24）+ getattr 读属性防懒初始化副作用（R25）**；测试夹具假绿窗口 12 类逐轮清零（🟡 命中置 1.0、FakePool 硬编码 []、边界值避开、断言强度不足等——R6/R16/R17/R28/R30/R31/R32）；每 Task spec/quality 双审 PASS；commits 65fd2108 + 0181aa84（Task 1 熄灭加速红相+实现）+ f7095f7a + a0709b6f（Task 2 分级注入红相+实现）+ 156d7cf2 + 485ce0d8 + 77fcf1cd + adbcec81（Task 3 接线+清理+T3-5 会话边界）+ 本条目

### 2026-08-12

#### 修复：定时任务飞书流式卡片永不终结（死路由 pre-existing + 86d6c7a2 显性化）——chat_queue 仅终结不投递 + adapter 死卡 pop 重建 + do_ask_user 来源闸门兜底

- **现象**：① 定时任务提醒用户不回答 → 飞书流式卡片一直"思考中"（streaming_mode 永不终结）② 下次用户说话 → 新会话流式内容追加到旧卡片（顺序混乱）③ 后续 update 报 300309 "streaming mode is closed"（im_adapter_stderr.log 11:10:33 实证）→ 新内容丢失且永不建新卡
- **根因（改造前后 diff 实证——"动错了哪"）**：
  1. **死路由是 pre-existing**：`git diff e77fe352 3fd648aa`——chat_queue.py/scheduler service.py/channel/__init__.py **零改动**；scheduler 通道从未注册进 ChannelRouter（__main__.py 只注册 electron/im）→ 定时任务回复路由 `router.push(reply, "scheduler", "")` no-op → **最终回复永不产生 SEND** → adapter 终结卡片唯一途径（_on_send pop state）永不触发 → streaming_mode 永不关闭
  2. **"原来没问题"的解释**：改造前同一机制同样存在——当时无 IM 历史会话（_im_channel_id 空 → 不建卡）或旧版 chunk 替换显示下"卡死"不可见
  3. **本次改造的直接贡献是 86d6c7a2（accumulated 累计全文）**：让"下次会话内容追加旧卡片"以字面"追加"形式显性化（改造前是替换显示）——用户症状精确对应此改动
  4. **t0 SEND 提前消费**：trigger_callback 入队瞬间以任务文本 fire-and-forget 发 SEND → 卡片未创建 → send_markdown 普通消息；之后回复流式建卡 → 再无 SEND
  5. **刻意设计冲突**：service.py L99-105 注释——channel="scheduler" 是刻意选择（"回复只走 SSE 前端，避免同一任务两条 IM 消息"）——修复不得把回复 push 成独立 IM 消息
- **修复（main 3 commits：79952acb + dcec016b + 3e551f79）**：
  1. **2.1 chat_queue 仅终结卡片不投递**：_process_with_merge 回复路由 elif 分支 scheduler 特判——`get_im_channel()` 非空时 `get_im_gateway().send_sync(im_cid, "", pop_reply_to=False)`（空 content → adapter _on_send 用 accumulated 定稿终结卡片、不新增独立消息——兼容刻意设计）；新增 `enqueue_and_wait_with_future`（返回 (result, reply_future) 元组，三返回点全元组）；watcher.py 自推条件化（读 reply_future._im_finalized 标志，遍历合并批次置位——无 TOCTOU）；无 IM 继承维持 no-op（回复只 SSE）
  2. **2.2 adapter 死卡 pop 重建**：update_card_element 返回错误码（成功/异常 None、业务失败 resp.code）；_on_stream 死卡 pop 集 {300309, 200850, 200740, 200750} → 先 `finalize_card` 纯终结旧卡（4 参 + 完整卡片 JSON + subtitle 清空——旧卡不再永久"思考中"；跳过 _filter_media 防媒体双上传）→ 重建新卡（种子 = 旧 accumulated 已含当前 chunk 无 double-append、CardState 构造序/message_id/失败检查/ask 门控/last_content/seq=1）；瞬时错误保留 state
  3. **2.3 do_ask_user 来源闸门兜底**：_cid 空且来源 scheduler/ha-watcher → `_gw.push_target` 兜底推问题（临时设 _current_channel_id/_im_channel_id 使注入守卫命中——回答可注入）；Electron 会话（source=user）永不触发；主路径 im_pushed=True 保留
- **质量链**：改造前后 diff 实证（DiffAnalyzer 独立分析）→ 计划 R1-R14 十四轮双审查（每轮 2 审查员异角度、先学飞书官方手册、完整逻辑链；R13+R14 连续两轮零 bug）→ 实施 3 commits + 55 测试全绿 → 实施后重审 R15+R16 连续两轮零 bug → 轻量方案对齐审查通过（无实施遗漏、无越界改动、6 项已知偏差全部记录）
- **已知接受边界（已记录）**：ha-watcher 降级/零 chunk 时回复仅 SSE（低频）；死卡重建 finalize 成功时两张同内容卡片（= 验收 #4 接受）；_im_channel_id 兜底残留至下条用户消息；push_target 群聊过期；跨来源合并（user IM + watcher supplement）双投递为 pre-existing 出范围
- **实机验证待确认项**（对齐审查标注）：验收 #1 真实飞书 streaming_mode 关闭、验收 #3 下次会话开新卡——需实机确认
- **排查教训**：①"原来没问题"≠"机制不存在"——同机制改造前后都存在，改造（86d6c7a2）让症状从不可见变可见；先 diff 实证再下结论，勿凭空想象新问题 ② 定时任务回复"死路由"是刻意设计（避免双消息）的副作用——修复必须兼容设计意图（仅终结不投递），不能简单改为投递 ③ watcher 的 future（concurrent.futures.Future）与 chat_queue 的 reply_future（asyncio.Future）是**不同对象**——跨线程共享信号必须同一对象（enqueue_and_wait_with_future 元组返回）④ "照抄可运行"伪代码标准：函数签名（finalize_card 4 参/update_card_element 返回码）、字段名（message_id 非 msg_id）、变量作用域（else 分支无 card_id 绑定）、future 对象链——每个都是实施炸点，必须审查到

#### 修复：IM 推送判定收口 should_push_im() 单一入口 + scheduler 回复投递内容 + 程序消息不推 IM（工程修复记录，非用户原话）

- **收口**：`runner.should_push_im()`（= bool(_im_channel_id or _im_force)）成为"是否推 IM"唯一判定入口——五消费点统一调用（compat.py chat_session 闸门 / chat_queue.py scheduler 特判 / runner.py 流式三处 L3092/L3124/L3141）；禁止内联 get_im_channel() or get_im_force() 自写判定
- **chat_queue scheduler 特判投递回复内容**：定时任务主 Agent 回复经 should_push_im 闸门 `send_sync(im_cid, reply, pop_reply_to=False)` 投递完整回复内容（替代 08-12"仅终结不投递"）——卡片生命周期由 adapter _on_send 保证（有流式卡 → state 分支用 reply 终结；无卡 → send_markdown 独立消息，receive_id 空时 adapter 回退 _push_chat_id 广播）
- **service.py 删除程序消息推 IM**：reminder/background_script 两分支不再 route_out 推 IM——定时提醒程序消息只写 DB（enqueue_sync 入队即写入 Message.DB）唤醒主 Agent，Chat 前端由 DB 变更 SSE 刷新显示
- **/chat 与 /chat/sync Electron 入口锁内双清 force**：两个会话入口在 _chat_lock 内 set_im_channel("") + set_im_force(False)（排队等锁期间 scheduler 可能重臂 force，锁内清除才生效）
- **用户语义**：定时提醒置 IM 标志为真（规则 3）、主 Agent 的话发 IM、程序消息不推 IM

#### 修复：压缩后主动重算前端模型使用率（sleep 强制压缩后圆环不刷新 → 下次睡眠重复强制压缩）

- **现象**：压缩前 60% → sleep 强制压缩 → 前端圆环仍显示 60% → 用户唤醒不说话（无 LLM 交互）→ 下次 sleep 判定仍见 60% → 再做一轮强制压缩
- **根因（实证）**：① sleep/force 压缩（`_tidy_context_impl`）后 `_last_prompt_tokens` **不置 0**（旧真实 token 数残留——只有主 Agent 超阈值路径 agent_loop.py:777/1006 置 0）；② `notify_compact_status_sync` 只广播 `{type,status,mode}`，done 不带 usage；③ 前端 chat.html compact_status 'done' 分支只清 `_compacting` 标志、不刷新使用率；④ 无交互 → real_tokens 永不更新 → 下次 sleep 判定（compat.py:2631-2648 real_tokens 优先）仍见旧高值
- **修复（8 commits：1a846726→4292cb54）**：
  1. **compat.py `compute_context_usage_estimate(store, context_window, messages)`**——启动时 get_stats fallback 同源全量估算抽取（返回 `float|None`，失败不伪装 0%）；get_stats fallback 改调（DRY + store/窗口注入免重复读取）
  2. **chat.py `notify_compact_status_sync`** 加 `usage`/`reset_tokens` 参数——done+reset_tokens 才置 0 旧 `_last_prompt_tokens`（用 `get_runner` 无创建副作用）；SSE 事件带 usage（6 处既有调用零改动，向后兼容）
  3. **compat.py `_tidy_context_impl` finally**——**先无条件保底 done（无 await——CancelledError 继承 BaseException 不入 except Exception，任务取消（clear_chat wait_for 600s 超时兜底）时保底广播必须在 await 前）**→ 再 `_compute_post_compress_usage`（消息数前后对比确认实际压缩）→ 条件二次 done（usage+reset_tokens）；**skip/abort/error 路径不置 0**（保留旧值 → 70% warningThreshold-0.1 冲突避让设计保持）
  4. **chat.html**——抽 `renderContextUsage` + **渲染代际守卫**（loadStats 用本请求局部 `fetchTs` vs `_compactUsageTs` 最近压缩 usage 渲染时间戳——主 Agent 目标受守卫丢弃"保底 done 触发、reset 前被服务端处理"的旧值响应；子 Agent 目标直渲）；done 分支 `typeof data.usage === 'number'` → 标记代际 + 渲染，else → `loadStats('')` 固定刷新主 Agent
- **质量链**：计划 R1-R5 五轮双审查（R4+R5 连续两轮零 bug；R3 抓 CancelledError 丢 done、R4 抓保底 done 竞态旧值覆盖、T4 quality 抓守卫条件反转）→ subagent-driven 实施（T1-T4 每 Task spec+quality 双审）+ 14 新测试全绿 + 全量回归 93 passed（4 个 test_tidy_cursor PROTECTED 断言为 pre-existing 豁免）
- **实机验证（2026-08-13）与工程结束决策**：
  - **mode-1（低使用率 <50%）场景实测**：23% → sleep 全套整理 → 前端 done 推送正常（红色→绿色）但值不变（仍 23%）——**这是正确语义**：mode-1（非破坏性）只推进增量游标（`Compress cursor auto-advanced`），context-manager 方案文本**不落地执行**；主 LLM prompt 由 `ContextManager.load_history → store.get_messages()` **全量加载、无游标过滤**（R6 全量逻辑链双审查实证：last_compress_id 只被 nap-EMA/force prompt/tidy 增量范围消费，无一处裁剪主 prompt）→ mode-1 后 messages.db 与下次 prompt 均不变 → 使用率不变是真实行为
  - **已知限制（待遇到再修）**：mode-2/force 的**纯 update 压缩**（0 删 N 精简——如"8 条 0 删 1 精简"）消息数对比判定漏判 → 不置 0 不推 usage；v2.5 执行段标志方案已备（plans 分支 1aadf698：mode-2/force 最终级联过滤后 `valid_deletes|valid_updates` 非空置 `_compressed`；mode-1 纯游标推进**不置**——R6 双审查 REJECT 修正；force 嵌套函数需 nonlocal）——当前无法实机验证高使用率场景，工程结束，**遇到 mode-2/force 真实压缩（删除/更新落地）时再实施验证**
  - **前端刷新机制本体有效**：done 分支 usage 渲染/loadStats 兜底 + 渲染代际守卫（保底 done 竞态已解决）——mode-2/force 删除场景下使用率会正确刷新
- **质量链补充（R6 全量逻辑链审查教训）**：用户要求审查必须**从用户场景出发追踪完整逻辑链 + 主动找计划外受影响位置**（不是只核对计划语句）——R6 抓出"mode-1 游标推进不构成压缩"（prompt 全量加载无游标过滤）与"测试默认 protectRecentCount=10 走不到游标推进"两个此前漏掉的真实问题——点式审查（R4/R5 漏守卫反转）已被证明无效，后续审查一律全量逻辑链

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

This project is indexed by GitNexus as **niu-agent** (19826 symbols, 37650 relationships, 300 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

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
