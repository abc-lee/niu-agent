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
4. **修改前必须用 先 分析影响范围** — 评估 blast radius 后再动手。
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
     command: ${PYTHON_PATH}  # 装饰性字段：同进程架构下内置服务器经 ToolRegistry 直调不执行 command；仅外部 stdio 服务器消费，需写真实命令
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
| `config/mcp-servers.yaml` | MCP 服务器配置（bundle 权威层，随版本升级直读）；用户自定义放 `~/.niu/config/mcp-servers-user.yaml`（deep merge 用户赢，0.3.0 双目录模型） |
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
> 以下为历史记录，反映彼时状态。部分条目中的架构（Go 后端、Nanobot、MCP stdio、`pkg/` 目录）已被后续重构推翻，当前架构以本文件为准。2026-08-13 前条目已压缩为摘要，完整细节见 git 历史。

### 2026-08-28

#### 工程：read 工具智能分页——29000 字符页预算按行截断取代行内均分截断（计划 v1.0→v1.3 四轮双审门禁 + SDD T1-T3）

- **背景（用户提出）**：dream-evolver/entity-extractor 提示词"每次读取不超过 150 行"是过度保守浪费轮次——核查发现 150 行是绕开行内截断缺陷的提示词补丁（500 行/页时每行被均分预算砍到 1000 字符，静默破坏 F1/F3 tool 记录），非根治
- **方案（用户拍板）**：500 行硬上限不变；工具自动计量内容大小，贴 29000 字符页预算**按行截断**（行边界停，永不在行中切；单行超预算才走行内截断兜底）；页长自适应省轮次
- **交付**（main `0c29cd4e`/`4d9978d8`/`92291d0d`）：
  - T1 read_file 重写：预算累积（每行成本=len(f"{i}|{line}")+1）+ 单行兜底（line[:预算−len(tag)−len(前缀)−1]+tag）+ 续读标记 `[Truncated at line {N}. Use offset={N+1} to read more.]` 精确行号且**移至输出末尾**（recency 利于弱模型；旧标记在 header 第 2 行且预算截断时跳行）+ 删行内均分截断四行 + schema 描述补三句；测试 45 绿（新增 6 用例：14 行/页精确断言/单行兜底/页中超长行跨页/预算不变式固定种子/tail 回归）
  - T2 提示词配套：两子 Agent 撤 150 行指导改"工具自动分页、见末尾标记按 offset 续读"；零背景推演验收（续读链/processed_line 语义零歧义）
  - T3 收尾：死函数 file_read 删除（46 行化石，512000 公式；工具名别名映射保留）+ session-manager 两处"对齐 read_file"失实注释自述化；回归 54 绿
- **设计要点**：预算口径=字符（对齐下游 agent_loop 30000 字符截断，返回整体 ≤30000 永不二次截断）；标记不归因截断原因（预算/limit/500 硬顶同文案，offset 恒正确）；READ_PAGE_BUDGET_CHARS=29000 常量注释锚定 agent_loop.py:598 耦合
- **质量链**：计划四轮双审（R1 双 CONDITIONAL 9 发现→R2 1P1+6P2→R3 双 APPROVE 3P2 文本级→R4 双 APPROVE 零阻断，连续两轮达成门禁；R2A 抓出标记位置 P1——D-8 "ends with" 与旧 header 位置矛盾）+ T1 实施后 spec/quality 双 PASS
- **实机验证（待用户观察）**：下次睡眠管道 dream-evolver/entity-extractor 读 F3/F1 时页长自适应（短行文件一次读满 500 行），raw 日志可见 read 调用轮次减少

#### 修复：请求组装 thinking 双通道去冗余（用户看日志发现 + 双审通过，commit 1225a1a9）

- **现象（用户报告）**：raw_http 日志查看器的应用层 Request Params 里 `thinking` 出现两份（顶层 + extra_body），主 Agent 与知识图谱入库路径同现；另质疑知识图谱入库请求无 response_format
- **实证结论**：
  - **thinking 重复**：真但无害——`chat()` 先把 `litellm_kwargs` 全量透传顶层（旧通道），`assemble_request_params` 又注入 `extra_body.thinking`（新通道）；litellm 发送时 drop_params 丢弃顶层、合并 extra_body，上线 HTTP body 只有一份（transport 层日志 16 条全核对）。远端审查对照安装版 litellm 源码实证：volcengine 路由原生就把顶层 thinking pop 进 extra_body（map_openai_params）、extra_body 合并 `data.update(extra_body)` 是全路由通用行为——修复前后 wire 完全等价
  - **response_format 缺失**：设计行为非 bug——`_llm_model_func` 只在 `keyword_extraction=True` 调用挂 response_format（json_schema 是 high/low_level_keywords 专用结构），实体抽取（分隔符文本输出）本就不挂；用户配置的 `response_format_mode: "json_schema"` 未丢
- **修复**：`chat()` 顶层透传剔除 thinking 单键（`{k: v for k, v in self.litellm_kwargs.items() if k != "thinking"}`），extra_body 为唯一送达通道——与 reasoning_effort 同策略、与探测路径 `model_probe._strip_thinking_key`（R13 单一来源纪律）同款；`allowed_openai_params` 等其余键保留顶层（litellm 当 kwarg 消费）；drop_params 置位逻辑不变
- **通用性**（用户拍板：Anthropic 不管，其余厂商要通用）：openai 兼容网关（deepseek/GLM/qwen/豆包）顶层 thinking 本就被 drop_params 丢弃，extra_body 本就是唯一实际通道——任意厂商 wire 行为不变
- **验证**：旧行为锁定测试断言反转 + 新不变式测试（顶层无 thinking/extra_body 送达/其余键透传保留），tests/test_llm_extra_body.py 16 绿；本地实施 + 远端 scout 源码级双审 APPROVE
- **实机验证（待用户重启后观察）**：raw_http 查看器应用层 Request Params 中 thinking 只出现在 extra_body 内

### 2026-08-27

#### 工程：设置页模型列表在线探测 + 选中自动填档（计划 v1.0→v1.15 共 16 轮双审门禁 + SDD T1-T3 双审全 PASS）

- **背景**：用户质疑上下文窗口靠手填——实测三条「便宜拿」路径全败（litellm 静态表对豆包 Coding Plan 给 256000 但实际窗口实测 ~229K 二分撞出、网关 /models 404、max_tokens 边界只探出输出上限 131072）；用户拍板：在线标准方法（`GET {apiBase}/models`）探测，探到就预填，探不到维持手填；litellm 离线表禁用（打包即冻结/口径是标准产品线非 Plan 线/程序无法判断表项对错）
- **交付**（main 00f09ae3/64ff0794/335841e6）：
  - T1 `POST /api/list-models`（compat.py）：openai/anthropic 双类型 URL 组装、窗口字段四键提取（context_length/max_input_tokens/context_window/top_provider.context_length）、三态返回（ok/unsupported/error）永不 500、本地免 key、非字符串字段守卫；18 测试
  - T2 IPC 通道：preload listModels + main.js list-models 转发（timeout 15s，**4 个失败出口全归一 {status, reason}**——get-config 直读文件不经 API server，server 未起时设置页可用，ECONNREFUSED 是可达主路径）
  - T3 前端 combobox：datalist+hint（四路径终态全钉死：成功/unsupported 缓存/error 静默重试/change 复位）+ 选中自动探测（D4 datalist options 单容器判定）+ **probeCapability 快照三元组 (apiBase|model|type) 全完成分支防陈旧复写**（手动按钮路径同治）+ probeInFlight 模块级单标志（置位点钉死三条校验后侧效代码前+check-and-set 二次检查）+ 失效三件套（清缓存+复位 hint+清 datalist，preset 路径同补）+ 窗口预填 clamp 32000-2000000；13/13 mock 场景 PASS
- **审查亮点**：16 轮双审抓出两个双审交叉级发现——探测陈旧复写竞态（探测在途 1 分钟窗口换模型，旧结果覆盖新配置）与 IPC 失败形状缝隙；R10 双审独立同发现快照维度不全（model→三元组）
- **申报偏差**：T3 为保一屏放下做纯留白 CSS -20px（内容高 743px ≤750）；手输恰等于列表项会触发自动探测（datalist 不可区分，无害披露）
- **实机验证（待用户执行）**：计划 §7 清单 6 条（前置纪律：设置页改动必须关窗重开）——豆包 404 降级手输/标准网关下拉+自动探测+窗口预填/手输不自动探/探测中改模型与改网关双竞态回归/保存全流程

#### 修复：测试隔离漏洞——test_clear_brain_state 真删生产指针块库（commit 887b533f）

- **现象**：context_blocks.db 消失仅剩 .lock，聊天历史无块号——用户质疑压缩工程失效
- **根因**：tests/test_clear_brain_state.py 调真实 compat.clear_chat() 未 patch reset_derived_state（T8 给 clear_chat 加派生状态复位后该老测试变炸弹），08-26 14:32 测试运行真删 ~/.niu/context_blocks.db + token_calibration.json；姊妹测试（test_remove_outer_timeouts/test_pipeline_queue_t4）都有防删 patch，此文件漏网。生产端点全部排除（4 个删除点均与 clear_messages 成对，messages.db 738 条完整）
- **修复**：补 patch（两处调用点）+ 全仓审计无其他漏网；token_calibration.json 已由校准回写自动重建
- **教训**：给既有函数加副作用时必须穷举该函数的全部测试调用方——姊妹文件补了不代表全仓补了
- **机制认知**：块号只在压实后出现（D16 水位线），块库误删后用量 63%<80% 触发线故视图纯原文窗口——工程是生效的，非失效；[摘要]/[合并] 行是旧压缩体系物理改写 DB 的历史遗留，组装器视 DB 为真相源原样呈现

### 2026-08-26

#### 工程：上下文组装器——压缩体系退役 + 存储/视图分离的确定性组装（spec v1.3.1 + 计划 v1.5 双审 R1-R6 收敛）

- **背景**：Windows 测试机实证压缩体系死穴——35 条消息短对话被「保护 N 轮」全量吞掉 → compress_msg_ids 空 → 65% 使用率静默跳过压缩；模式一/二/三逐条裁决对弱模型必然脆弱。用户逐轮纠偏后确立新架构方向：压缩概念整体退役，改为存储/视图分离的确定性组装器。
- **架构定案**：
  - **存储/视图分离**：messages.db 是真相源永不动；LLM 每轮只见组装视图 = [历史索引前导] + [近期原文窗口]；索引区职责边界 = 模拟全量上下文的目录页，纯时间线 FIFO 无语义注入
  - **分区预算**：原文窗口 ≤50% 窗口（完整会话单元装填，tool_calls 配对完整）；历史索引 ≤30%（每块一行机械行，超预算最老相邻块合并）
  - **指针块**：窗口外单元归档为 SQLite 单表指针块（`~/.niu/context_blocks.db`，flock 排它锁），不向量化——模型看索引报块号，`read_history_block(block_id)` 取回逐字原文（MCP 静态工具直接进主 Agent 工具列表，Schema 描述自带块语义；不对子 Agent 开放）
  - **批量压实**：校准后估算 ≥80% 触发（AUTO_GATE 滞回 ≥80% 触发/<78% 复位，组装出口与 runner 真值回调双触发去重）、95% 应急线；保留最近 N 轮（`context.keepRecentTurns` 默认 3 可配置）；D15 三轮硬约束（工具输出占位符化→减轮）；纯机械零 LLM 秒级
  - **token 校准倍率**：每次主 Agent 响应后真值 prompt_tokens ÷ 本地估算覆盖更新倍率（`~/.niu/token_calibration.json`，默认 1.15），桥接本地估算与服务端真值
  - **journal 迁出睡眠管道**：scheduler 内置 `journal-daily` 定时任务每日 18 点直执行（导出 DB 增量为工作集文件让 journal-agent 自读；严禁经 ChatQueue enqueue 防反污染；backend-busy 避让活跃对话；游标自管）
  - **§8 拍板落地**：journal.md 本体 /new 时保留；指针块 SQLite 单表；read_history_block 不对子 Agent 开放；保留轮数默认 3 可配置
- **交付链**（SDD 每 Task 新鲜子 Agent + spec/quality 双审，main `1af5ffab`→`5be2c087` 共 8 commits + 本条目 T9）：
  - `1af5ffab` T1 指针块存储层 + 会话单元切割器（纯函数零接线）
  - `4fa0132b` T2 get_context_for_chat 重写为索引+窗口新视图（压缩调用路径退役）
  - `8da1bc97` T3 校准倍率闭环 + 80%/95% 触发 + 五入口溢出收编（回写 `messages[:] = [system]+new_view` 原地生效）
  - `ce61f6c4` T4 read_history_block 工具 + 实体标签会话展开 + 解码说明书
  - `6e0a7221` T5 摘要增强可选层（裸调 lightrag_llm 一次一 call + 空闲调度 + 默认禁用）
  - `23362b12` T6 压缩体系退役大清理——cm/模式一二三/compress 游标/保护 N 轮/force 投递面全链清零（compat 净删 5687 行）
  - `f1f6fbe8` T7 journal → journal_daily 定时任务直执行
  - `5be2c087` T8 一致性校验挂 lifespan（不一致整库重切重建）+ /new 清理面四端点接线
  - 本条目 T9 文档收官（SYSTEM_MANUAL 上下文管理章节重写/manual-performance 双路并发升格+KV cache 踩踏机理/manual-user-guide 用户视角/niu.md journal 自读语义修正）
- **验证**：各 Task 点名回归全绿（T1 34/T2 17/T3 35/T4 17/T5 15/T6 322/T8 15 passed）；主链路确定性零 LLM 承重，LLM 仅做可选异步增强且失败无害

### 2026-08-26（续）

#### 工程：journal 子 Agent 直读 DB——日志即水位线（计划 v1.0→v1.5 双审 R1-R6 门禁 + T1/T2 SDD 双审收敛）

- **背景与病灶**：T7 后 journal 链路=程序把 DB 增量导出到 `~/.niu/md/journal_workset.md` 让子 Agent 自读+last_journal.json 程序侧游标。用户实测「日志子 Agent 无法工作」实证三病灶：①导出文件是动态中间产物（覆盖写/unlink/并发窗口）非准确历史②零增量或导出失败时任务文本无文件路径、子 Agent 无从获取消息（直接断链）③游标仅夜间推进、程序监听不到交互路径结果。组装器新架构使 messages.db 只增不改，直读 DB 的历史障碍消失。
- **设计定案（用户逐条拍板）**：D-A 数据源唯一=messages.db；D-B 日志即水位线——每条整理条目尾带机器可读标记「覆盖至: <message_id>」，单一工件自描述，交互记录条目不带标记；D-C 分支判据=是否提取 DB 内容（记录单件事不动标记/整理类完整流程/报告类默认纯聚合不足再整理）；D-G error 归因分级（invalid_after_id→首次兜底/transient→轮空不写标记防覆盖空洞）；D-H mcpToolFilter 嵌套 dict 钉死只暴露 get_messages（平铺列表会使 subagent.py L656 AttributeError——R3 抓出）。
- **交付链**（main 61d37700→80fcbdd8 共 2 commits，24 文件净删 539 行）：
  - T1 `61d37700`：get_messages 四处 schema 同步扩展 after_id/limit/full_tool_output+created_at/has_more/next_after_id+reason 分级错误；折叠直接 import 复用 agent/md_mirror.truncate_tool_output（<已精简> 2000B 头60%尾40%）；stdio dispatch get_messages 分支改直调消除双实现（申报偏差，对齐 read_history_block 先例）；14 单测
  - T2 `80fcbdd8`：journal-agent.md 重写（三分支判据+七步整理流程）；handler _build_journal_task_for_handler 整删薄层化；scheduler 任务文本自理化+import 收缩（R4 抓出漏改则夜间静默 ImportError）；compat 游标链整链退役（_export/_parse_processed_up_to/JOURNAL_*/_read_write_cursor_with_lock/_ALL_CURSOR_FILES+_reset_all_cursors 四调用点）；SYSTEM_MANUAL/niu.md 同步；测试处置（grep 穷举+create=True 保零写退役反向钉）
- **质量链**：计划 R1-R6 六轮双审（R1 P0×1 /new 清库后标记失效无恢复→D-G；R3 P1×1 mcpToolFilter 格式错误照抄即崩；R4 P2×1 import 块收缩漏点名；R5+R6 连续两轮双 APPROVE 达成门禁）+ 每 Task spec/quality 双审（T1 双 PASS、T2 双 PASS+微修闭环：dispatcher docstring 残留/SYSTEM_MANUAL「复位全部游标」虚假陈述）
- **验证**：点名回归 150 passed；真实 load_mcp_tools 断言 journal-agent 工具面恰为 [get_messages] 且 Schema 含三新参；DiskEngine.get_schema() 零泄漏；py_compile/ruff 零新增

### 2026-08-26（续二）

#### 工程：mcp-servers.yaml 双目录化——copy-once 设计债清偿（计划 v1.5 双审 R1-R5 门禁 + T1-T3 SDD 收官，版本 0.3.0）

- **背景与病灶**：mcp-servers.yaml 是三配置面中唯一 copy-once 例外（launcher 首启复制到 `~/.niu/config/` 后仓库侧变更永不达存量装机）——b248c8b6 式手工修复即此类脱节；任何内置服务器的新增/参数/tools visibility 变更都卡在同一死点。关键实证：`${PYTHON_PATH}` 是装饰性字段——全仓无替换/执行代码（MCP 同进程化后内置 server 全经 ToolRegistry 直调，command 仅外部 stdio 消费），bundle 配置零机器相关值可直读。
- **方案定案（docs/superpowers/plans/2026-08-26-mcp-servers-dual-dir.md v1.5）**：
  - D1 bundle 权威层直读 + 用户层 `~/.niu/config/mcp-servers-user.yaml` deep merge（dict 递归合并 / 标量 list 用户赢），用户只写差异段即可给内置 server 补 tool visibility
  - D2 同名冲突用户赢；D3 迁移=0.3.0 升级说明删旧文件、零自动迁移（范围仅此一文件，user-config.json 不碰）
  - D4 任一层解析失败 error 降级空基座继续启动（config 解析失败从不终止启动）；D5 用户层缺失=正常态
  - D7 删除语义：用户层 `server名: null` = 禁用该内置 server——deleted_names 集合在 REQUIRED/OPTIONAL 两条加载循环兑现 skip（不计失败不触发严格终止），嵌套 null 只删键不入集合；两调用点均解包 `(merged, deleted)` tuple
  - D8 旧文件弃用 warning 模块级去重（双调用点共享至多一条）+ 测试重置口；D9 跨平台零新增分支（os.path.expanduser 同款先例）
- **实施**（T1/T2 改动在工作树待提交 + 本条目 T3）：
  - T1 Python 双源加载：_load_mcp_config 返回 (merged, deleted_names)；deep merge/null 删除/bundle 缺失降级/load_external_servers 非 dict 条目守卫；niu_api/config.py 删 _get_mcp_servers_path 惰性兜底复制；16 例合并矩阵测试 tests/test_mcp_config_dual_source.py；test_p0/test_mcp_loader.py 三处 patch 契约改 tuple
  - T2 Rust launcher：init_niu_dir 删 mcp-servers.yaml 复制段（53 行纯删除；user-config.json 复制段保留不动）
  - T3 文档收官：manual-mcp-disk.md L102 与 AGENTS.md L259 两处 `${PYTHON_PATH}` 失实修正 + manual-mcp-disk.md 新增 2.7 用户层配置节 + SYSTEM_MANUAL.md 新增 2.1.1 双目录加载节（含 0.3.0 升级说明，两文档交叉引用）+ 版本 bump 0.3.0（VERSION/chat.html version-label 两处同步）
- **验证**：点名回归 test_mcp_config_dual_source.py + test_p0/test_mcp_loader.py 共 32 passed；grep 全仓 `${PYTHON_PATH}` 失实描述零残留（config/mcp-servers.yaml 内为字面量数据不受影响）
- **实机验证清单（待用户执行）**：①删除 `~/.niu/config/mcp-servers.yaml`（先备份）后启动 → 内置 10 server 照常加载②建 mcp-servers-user.yaml 写测试 server → registry 中并存③用户层覆盖 preload/tool visibility 生效④`server名: null` 禁用 REQUIRED server → 启动 warning skip 不终止⑤旧文件残留时弃用 warning 至多一条且不影响加载⑥重打包后 launcher 启动日志无 mcp-servers.yaml 复制行

### 2026-08-25

#### 工程：MD 中继工程五——force dream 保护链退役 + dream 游标终退 + 化石清理（方案 v1.0→v2.5 共九版，R1-R7 双审+全局架构审计收敛）

- **背景**：用户质询「force 保护链在文件驱动下不存在」——工程四完成提炼文件驱动化后，F1/F2 不受 DB 压缩影响，基于 `~/.niu/last_dream_evolve.json` 的 force dream 哨兵保护链成为旧范式自洽残留（提示词层引用已消失的游标 UUID、机制层哨兵计算与砍半互斥空转）；全局架构审计清查提示词/机制/文档三层残留后重构计划。
- **方案**（docs/superpowers/plans/2026-08-24-md-relay-project5-cursor-retirement.md v2.5；门禁=同文本连续两轮零发现）：
  - **T1 提示词层对齐**：context-manager.md 三处 dream 边界描述改写（模式一=last_compress_id 之后全量无上界）；dream-evolver.md frontmatter mcpServers+mcpToolFilter 双删 session-manager（单删过滤项会因缺省 filter 全放行而扩权），get_messages 禁止理由改「对话记录在 F3 文件中自读」
  - **T2 机制层七件套整链退役**：force/runner-force 哨兵与边界防护、睡眠 cm 锚点排除+cascade cursor 分量、dream 循环游标回写与 fresh_ids 校验、`_build_force_prompt` 安全边界行、砍半互斥、`_ALL_CURSOR_FILES` 收缩两键（journal+compress）、入口共享读取删除、`_f_id_to_idx` 反向映射整删；磁盘清算 `~/.niu/last_dream_evolve.json`(+.lock)；新建 tests/test_cursor_retirement.py 六组退役钉
  - **T3 化石清理与回归收尾**：context-manager.md 三处「安全边界」死文本块删除+「未提取知识」悬空引用改写为现行语义+决策流程列表缩进修复；步骤编号化石（compat force 分支 2.5/3·3/3、runner 2.5/4·3/4 → 1/2·2/2）；CP3 注释改文件驱动措辞；SYSTEM_MANUAL 睡眠管道段澄清 force 只跑压缩对；AGENTS.md 增量游标存量化石标注退役；md_alignment docstring 补 [摘要] 补写边缘态标注
- **拆链后终态**：模式三=对全部消息 keep/update/delete（无 dream 边界；PROTECTED 近期消息排除照常保留）；dream-evolver 只删 F2 前缀、无任何游标读写；journal/compress 两游标语义不变
- **验证**：点名回归 10 文件全绿（test_cursor_retirement/test_sleep_reorder/test_md_f3/test_dream_segment_v2/test_entity_segment_v2/test_journal_agent_tidy/test_compress_prompt_lean/test_compress_degradation/test_compress_history/test_compress_quality）；py_compile+ruff 零新增
- **commit**：`7dd61379`（T1）+ `8aaba576`（T2）+ 本条目（T3）
- **实机验证（待用户重启）**：①/compact 正常且模式三方案覆盖全部消息（无边界截断）②睡眠 dream 多轮循环正常、F2 前缀按 processed_line 删除③/new 后仅复位 journal/compress 两键④`~/.niu/last_dream_evolve.json` 不复活

### 2026-08-24

#### 工程：MD 中继工程四——睡眠管道重排 + 压缩前置门控清算 + 游标清算（方案 v1.0→v1.9 共十一轮双审收敛，连续两轮零发现）

- **背景**：工程二/三完成提炼文件驱动化（F1/F2/F3 中继）后，睡眠管道仍保留旧物理顺序 entity→dream→journal→compress；spec §5/D3 定稿顺序为压缩在前、提炼在后——用户实机验证时发现该盲区，重排补进工程四走完整计划→双审→SDD 流程。
- **方案**（docs/superpowers/plans/2026-08-24-md-relay-project4-reorder.md v1.9，R1-R11 双审全远端派审）：
  - **睡眠管道重排**：entity→dream→journal→compress 改为 **journal（仅模式2及以上 usage≥50%）→ context-manager → entity-extractor → dream-evolver 多轮循环**。安全性根基=提炼文件驱动化：DB 压缩只动 Message DB 不触 F1/F2 文件（镜像仅挂 add_message，压缩的 [摘要] 替换不回写），entity/dream 读到的永远是完整原文，压缩先行零丢失
  - **CP 重排**：CP0 排队唤醒非睡眠 → cancelled；CP1 journal+压缩段完成后 / CP2 entity 段完成后 / CP3 dream 循环完成后 → interrupted（已推进不回滚，下次续跑）
  - **门控清算**：删压缩前置游标追平校验 `_cursors_caught_up` 调用及三孤儿函数（`_cursors_caught_up`/`_dream_only`/`_read_cursor_value`，含 7 处 monkeypatch 缝）；/compact 不再出现 skipped 状态，不被梦境积压阻塞
  - **游标收缩与哨兵保护**：模式一 end_cursor 上界移除；post-dream 范围守卫收窄但**保留锚点排除+cascade**（数据源改入口读取 last_dream_evolve_id——force 哨兵承重墙）；dream 游标继续回写（force 边界唯一数据源，停写=永久全保护退化）；复位表三键方案（journal/compress/dream 全留，防 /new 后陈旧游标+F2 truncate 致 force 哨兵 0↔len 翻转静默关闭边界）；`last_entity_extract.json` 死键清算，scripts/backfill_f1_from_db.py DEFAULT_CURSOR 默认空串防误跑
  - **已知边界**：cm 失败 mode-2 早退中止整个 sleep（entity/dream 延迟一轮自愈）、mode-1 三类失败吸收续跑；压缩对内部无检查点，唤醒最早 CP1 被感知（既有段落原子性非新退化）；force 保护边界滞后为保守方向（多保护不少删）
- **实施**（main 2 commits，SDD 每 Task 新鲜子 Agent）：`23b8c4c1` T1 重排+CP 检查点迁移+12 文件约 30 例测试适配（含 test_subagent_overflow getsource 源码序断言——三轮被删符号 grep 盲区，R4-B 抓出）；`9eb1d36a` T2 门控三孤儿删除+复位表三键+backfill 防误跑+残留游标档清理
- **验证**：T3 点名回归 20 文件 307 passed；剩余 7 failed 全部基线既有豁免（test_tidy_cursor 4 例 PROTECTED 类 + test_subagent_overflow 3 例 client.backend None 环境）+ 本次顺带修复 6 例历史存量测试腐化（test_compress_quality FakeClient.backend×2 与 _read_cursor_locked patch×2、test_compress_history 用户轮边界语义×1、test_stop_interruptible _ensure_session_chain 桩缺失×1——worktree 对照 b3bb7cb7 实证全部 pre-existing）；py_compile 三文件通过，ruff 17 条告警与基线逐码一致零新增
- **实机验证（待用户重启）**：①睡眠日志顺序 journal→cm→entity→dream 多轮循环 ②手动 /compact 不再被积压阻塞且无 dream-evolver tab ③/new 后 F1/F2/F3 清空且游标复位正常 ④force 边界行为正常（[Tidy] 游标跳过告警频度观测）

### 2026-08-22

#### 修复：向量检索精确名短路——query 恰为实体名时图层精确命中置顶（精确名查询根治）

- **现象**：睡眠后向量检索某高频人物实体检索不到（rank 49 掉出 top_k）。
- **根因链（全实证）**：① dream-evolver 触发式精简（`lightrag_edit_entity`）把该实体描述归纳覆盖为**无主语属性堆叠**（正文零次实体名、丢称呼锚点）→ ② 向量 = embed(entity_name + "\n" + description)，bge pooling 全文加权——开头 1 次名字被 130 字正文稀释（量化：无主语堆叠 sim 0.4409 rank 49；名字首句（"XX是…"）sim 0.5780 ≈历史 #1）→ ③ **检索侧真实缺陷**：`search_entities`→`query_data(mode=local)` 纯向量语义排序，query 恰好等于实体名时无图层精确索引短路——向量排序决定精确名查询命运。
- **机制认知（bge 稀释）**：向量化构造 name+\n+description 本身没错，但隐含假设"description 是自然语言描述（主语会反复出现）"——精简成无主语电报体后假设破裂。name 在开头出现 1 次被正文稀释，正文再出现名字信号加倍。
- **方案（用户拍板）**：向量检索与图层精确名检索**并行**，精确命中实体分数置最高。落地为 query_data 返回前后置修正（**不动 LightRAG fork**——成熟产品不改原则）；**事实更正**：返回实体项无 rank 分数字段（rank 是 operate.py 内部图度数中间值，convert_to_user_format 已丢弃）——"分数放到最高分"落地为"置顶到首位"。
- **实施（main 2 commits）**：
  - `26a37871`：query_data 短路块（45 行）——守卫 G1 status==success（failure 零命中不短路）/ G2 filter_lambda is None（search_by_file_path 技能通道契约不绕过）/ G3 mode∈(local,global,hybrid,mix)（naive/bypass 实体恒空）/ G4 data dict+entities list 双形态；query.strip() 非空+≤50 字符+has_entity(q) lowercase 精确命中 → get_entity_info 下钻 data.graph_data 构造同构项（无 rank 无 distance；fields 在场同过滤）→ 在列重排首位/不在列插入首位截断生效 top_k；全块 try/except 异常返回原结果
  - `89ac2cdd`：tests/test_query_data_exact_match.py 10 用例（命中不在列截断/命中在列重排/未命中/组合 query/异常防御/failure/filter_lambda/info error/naive 门控/51 字符门控）
- **一个入口全覆盖**（调用点核对一致）：MCP search_entities/lightrag_query_data、kg_api 前端搜索、search_multi_lightrag 动态注入、region_injector 脑区激活、timeline_query、photo-server 全过 query_data；filter_lambda 通道（search_by_file_path/search_within_region）G2 豁免
- **下游效应（有利）**：置顶实体 distance 缺失 → runner 衰减池 fallback i=0 → 1.0 = 池内最高分（decay_pool 降序）——与置顶意图一致
- **质量链**：方案 v1.0→v1.2（R1 双审交叉抓出 rank 事实错误+failure 门控 2 P1 + filter_lambda/mode 契约 2 P2；R2 双审仅剩 P3 级行号/清单/测试增补，修正后确认可交付）→ 实施 2 commits → 实施后双审 APPROVE（Quality 零缺陷）
- **验证**：新测试 10 passed；adapter 回归 51 passed/9 failed 与 pre-existing 基线精确一致零新增
- **实机验证（待用户重启）**：search_entities("<实体名>") 或对话查该实体 → 该实体在结果首位（永久免疫描述形态漂移）
- **已知边界**：多词 query（"<实体名>是谁"）不短路走向量（保守语义）；>50 字符实体名不短路（DEFAULT_ENTITY_NAME_MAX_LENGTH=256 的取舍）；向量异常/failure 路径短路不可达（门控优先）；语义通道（模糊查询）质量交由 dream-evolver 自然演进（精简锚点规则不改——用户拍板）；受损描述不手动修（短路后精确名查询已免疫）

### 2026-08-21

#### 修复：entity-extractor 提炼入库 doc_id 撞车静默丢失（方案 R1+R2 双轮审查 + T1-T4 实施 + 实机验证）

- **现象（用户报告）**：内容提炼调 `lightrag_insert` 入库后，知识图谱**多数情况无动作**、少数才有——当天第二次提炼起全部静默丢失（2026-08-21 实证：10:42 首次入库成功，11:08 第二次同 doc_id 被吞）。
- **根因三层**：
  1. **提示词层**：entity-extractor.md L44/L66 教 LLM 自编 `doc_id="refined:{date}:{seq:03d}"`——LLM 不知道当天已用几号 seq → 恒写 `001` → 当天第二次提炼撞车
  2. **去重层**：LightRAG `apipeline_enqueue_documents`（lightrag.py L1452-1513）`doc_status.filter_keys` 检出 doc_id 已存在 → 过滤出处理队列（early-return L1508-1510）→ 仅 warning → **不做实体抽取**；`ainsert` 仍正常返回 track_id（L1237-1270）→ 工具/LLM/程序三层无感知
  3. **清洗层**（dup- 记录不可见之谜）：撞车时 upsert 的 `dup-` FAILED 记录**立即落盘**（json_doc_status_impl.py L222 upsert 自带 index_done_callback），但 `GET /api/kg/pipeline_status`（kg_api.py L569-620）被 chat.html:2390/spirit.html:636 每 3s 轮询，管道完成后 `_cleanup_failed_docs`（kg_api.py L23-104）删除全部 dup- 条目——**dup 记录活不过一个轮询周期**，事后排查永远看不到撞车痕迹
- **方案 A（删 doc_id 走内容 MD5）**：root cause 修复——去重键从"LLM 瞎编的序号"变"内容本身"（不同内容永不撞车、相同内容合理去重）。`lightrag_insert` schema 的 doc_id 本就 Optional（auto-generated if omitted）
- **实施（main 4 commits + AGENTS.md 本条）**：
  - `c13c28a1`：entity-extractor.md L44/L66 删 doc_id 指导 + 显式"不要传 doc_id"（提示词每次调用现读，无需重启）
  - `9730a4c9`：inject_document changelog 空 id 修复——`"id": doc_id or track_id or ""`（doc_id=None 时用 track_id，防图谱前端空 id 伪节点；对齐既有先例 L2040）
  - `5283838f`：删除 message_injector.py 死代码（4 函数生产零调用——generate_doc_id 生成的正是 `refined:{date}:{seq:03d}`，同一错误抽象的化石）+ 其唯一测试
  - `0104fdeb`：新测试 tests/test_inject_document_changelog.py（3 用例：doc_id=None→id==track_id / 显式 doc_id→id==doc_id / rag None→不 record_change）
- **验证**：新测试 3 passed；adapter 套件 9 failed 与基线完全一致（pre-existing 陈旧测试）；实施后双审 APPROVE（唯一 P3 = 本条日志补录）
- **已知边界**：11:08 被吞内容不补救（游标已推进，内容价值低且部分过时）；同内容提炼仍会被合理去重（MD5 撞车=设计语义）；file-processor/主 Agent 经 disk 仍可传 doc_id（未教学、行为=修复前，接受）；撞车保持不可观测（程序调用方无法解释错误——用户拍板）
- **实机验证（待用户）**：新对话产生游标后新消息 → 自然睡眠或 /sleep → doc_status 出现 `doc-xxxx` 新条目（content_summary 含"记忆提炼"）且 status=processed；图谱前端 changelog 无空 id 伪节点

### 2026-08-20

#### 工程：整理管道全局排队 + 睡眠状态机打断 + 压缩前置校验 + 游标假推进修复（方案 7 轮双审 + T1-T8 分批实施）

- **用户现象**：小憩（nap）正在做梦境进化（dream-evolver），睡眠（sleep）同时触发 → nap 的 dream-evolver 前端 tab "突然退出"，sleep 的 dream-evolver 开始。期望：整理类子 Agent **全局一次一个、排队执行**。
- **实证根因**：
  - **nap 与 sleep 无互斥**：`_nap_running`（runner.py，threading.Event）只防 nap-vs-nap；`_tidy_lock`（compat.py，asyncio.Lock）只防 sleep-vs-force——两套锁互不感知
  - **无"顶掉"机制**：SubagentRegistry.register 同名冲突抛 ValueError——杀的是**后到者**（用户观感 = nap 的 dream-evolver 独立退出 + 前端同名 tab 清空复用）
  - **真 bug（数据丢失级）**：后到者注册失败返回 `"[错误]"` 前缀字符串（subagent.py L1138-1139），游标推进逻辑只识别 overflow/incomplete 两种 JSON → 落 else 兜底被当成功 → **游标假推进**；`SUBAGENT_ERROR:`（subagent.py L1183-1187）同类洞
  - **后端对睡眠状态完全无感知**：进入 SLEEP 时 POST /api/context/tidy，唤醒时无通知后端机制
- **方案**（docs/superpowers/plans/2026-08-20-tidy-pipeline-queue.md v1.6，R1-R7 七轮双审、连续两轮零 bug）：
  - **单 worker 全局队列**：`_pipeline_queue`（asyncio.Queue）+ `_pipeline_worker` 单协程串行消费——队列模型无锁嵌套即无环（锁方案在 chat.py/compat 持 `_chat_lock` 调 impl 必成环，R1 实证）
  - **九入口全接入**：tidy 端点 sleep（投递+立即返回 queued）/force（投递+await）、chat.py 溢出 ×2（fire-and-forget）、chat_session（fire-and-forget）、clear_chat（await 600s 超时+held=True）、chat_queue 降级重试（await+参数透传）、runner 80% 水位（call_soon_threadsafe+300s 超时+转换块留回调）、runner nap 触发（call_soon_threadsafe+失败清 `_nap_running`）——各按语义阻塞/await/fire-and-forget；future 统一 `concurrent.futures.Future`（asyncio wrap_future / runner 线程 result(timeout)）；**None 窗口防御**（队列未创建 → 同步执行 Option A）
  - **队列去重**：键 = (kind, skip_compress, force_protect_recent)；压缩类在队 ≤3（force/runner-force/clear skip_compress 键不同可并存）；跨线程 check-then-set 竞态后果有界
  - **CP0-CP3 睡眠状态检查**（仅 sleep）：CP0 排队唤醒非睡眠 → cancelled/woke_up；CP1 实体段后 / CP2 梦境段后 / CP3 压缩段前 → interrupted/woke_up（已推进游标不回滚，下次续跑）；**nap/force/runner-force 零插入**（需求 4/5 用户拍板矩阵）。**【已被 2026-08-24 MD 中继工程四取代】**睡眠管道重排后 CP 位次变为 CP0 排队/CP1 journal+压缩段后/CP2 entity 段后/CP3 dream 循环后——现状以 2026-08-24 工程四条目为准
  - **压缩前置校验** `_cursors_caught_up`（sleep+force+runner-force 三处）：提炼+进化游标全追平才允许压缩；protect 同源 `effective_protect`（force 降级提前到分支顶部）；protect=0 特判真实尾部（Quality P1-1 吸收：继续查 dream 游标不 early-return）；protect_start==0 全保护放行；未追平 → `{"status":"skipped","reason":"还有消息未提炼完，本次不压缩"}`（中文 reason 前端直接展示）。**【已被 2026-08-24 MD 中继工程三取代】**force/runner-force 两处门控随 force 梦境腿摘除而删除（手动 /compact 不再被梦境积压阻塞），仅保留睡眠 CP3 门控——现状以 2026-08-24 条目为准
  - **`_is_subagent_failure` 修游标假推进**：`[错误]` / `SUBAGENT_ERROR:` 前缀识别为失败；11 决策点（compat 7 + runner 4）分支顺序 failure/incomplete 先判、再判 overflow、else 才推进
- **关键设计教训**：
  - ① 锁模型在持锁方多入口调用场景（chat.py/compat 持 `_chat_lock` 调 impl）必然成环 → 队列单 worker 是绕开死锁的正确选择
  - ② 校验视图不能简单剔除保护尾部（剔除后游标落在视图外 → `_build_incremental_msg_text` 降级全量 → 恒判未追平 → 压缩死代码）——改 `_find_protected_range` 索引比较
  - ③ agent_loop 的 `messages[:]` 回写契约是 **dict 列表**（非 Message dataclass）——入口 8 转换块（L2417-2529：dict 构建/孤立 tool 清理/system 保留/cache_control 重注入）整体保留在回调内执行，不能简化为"自加载"
  - ④ 跨线程投递统一 `call_soon_threadsafe` 桥（先例 chat.py L145-151 / runner L1566-1585）；fire-and-forget 只是不 await，item 一律带 future（去重依赖 done()）
- **验证**：T1-T7 配套测试 **79 passed**（全 mock：禁真实 LLM、禁图谱写入、messages.db 零新增）+ T8 回归 **19 文件 252 passed / 11 failed**（11 failed 全部 pre-existing——基线 34575f3a 同文件同失败复现豁免：test_compress_quality 2（FakeClient.backend 缺失）+ test_compress_history 1 + test_tidy_cursor 4 + test_subagent_overflow 4）
- **文档同步**：SYSTEM_MANUAL 指令机制（/clear 排队+600s 预算、/compact 排队+await+压缩前置校验+skipped 中文、/sleep 与空闲自动睡眠同路径+CP0-CP3，compat.py 行号锚点重写 3828→4398 / 3428→3988 / 2432→2909）；manual-developer 端点表（/api/context/tidy queued/skipped 语义、新增 /api/spirit-state 行、/api/chat/clear 排队+await）
- **commit**：main 10 个（T1 eb869b5e → T7 f9aae026 → T2 1060715b → T7 journal 修复 577cf045 → T3 abd4d60c → T5 16d53c54 → T4 66feb58d → 批次2 Quality 733c48b5 → T6 0e736db0 → T6 Quality 14efef64），HEAD=14efef64

### 2026-08-18

#### 新增：模型能力探测器 + 配置页动态档位（方案 19 轮审查 + Task 1-5 实施 + 真实实测）

- **背景**：zen 接入暴露 `reasoning_effort`/`thinking` 参数被 litellm 白名单静默丢弃（utils.py L4107-4112，`drop_params=True` 时静默 pop 无告警）——配置意图与实际载荷脱节；LightRAG 入库"关思考链"指令（thinking disabled）从未送达，图谱在 zen/nemotron 下瘫痪。
- **实证链**：① `extra_body` 全路由送达（litellm 白名单不拦 extra_body 内键）——参数上线逃生通道；② 顶层通道 `drop_params` 丢弃产生**假 200**——豆包"全值域 200"是 reasoning_effort 被丢的假象（服务端从未收到该参数）；③ 豆包 `disabled` 场景真实值域 [minimal, none]——minimal/none 200、其余 400（`high` + `disabled` 400 Invalid combination），经 extra_body 通道实测。
- **关键机制**：
  - `assemble_request_params` / `build_base_params`（agent/generic/litellm_adapter.py）——extra_body 注入 + none 排除（none 不注入，语义由 thinking disabled 表达——豆包/zen none 400 实测）+ 用户已有 extra_body 键优先；**生产 chat 与探测直发共用一份基础参数组装**，杜绝两份漂移
  - 探测器（niu_api/model_probe.py）：值域扫描 [minimal, low, medium, high, xhigh, none, max] 按序探测、请求携带**场景配置的 thinking**（lightrag 场景恒 disabled、llm 场景按用户配置——值域结论只对当前场景 thinking 成立，不得外推）、候选超时重试 1 次（豆包响应 10s 边界波动，超时 ≠ 不支持）、400 body=None 分类健壮性（volcengine 路由实测 400 响应 body=None——litellm 未解析 body；body 缺失不改变 400 语义，错误体必须从 e.body 取，e.response.text 实证为空）
  - 档案 `~/.niu/model_capabilities.json`：键 = `apiBase|model|llm` / `apiBase|model|lightrag`（api_base rstrip("/") 规范化——settings 保存值与 CLI 读取值尾部斜杠差异不致档案不命中）+ **原子写（临时文件 + os.replace）+ fcntl.flock 非阻塞写锁**（读-改-写整体持锁；锁被占用 → 跳过写入返回 False，失败不写坏旧档）
  - settings 动态下拉：llm 段与 lightrag 段各一个"探测能力"按钮（POST /api/model-capability-probe，与 CLI 共用 probe 核心），探测成功后用档案 supported 值刷新推理深度下拉（**厂商原生档位名原样**），选择即写入配置
  - 探测生产同参（组件 3）：探测请求 reasoning_effort/thinking 从配置透传（compat.py 三处 None 硬编码清除——_probe_llm / probe-response-format 等），经 assemble_request_params 注入 extra_body 送达，探测"服务端认不认原始值"与生产"配置值归一后直发"无矛盾
  - high 五源头清理 + 存量迁移：lightrag_llm.reasoning_effort 兜底统一改 `""`（模型默认/配置页驱动，不强制档位——llm_proxy.py get_llm_config），旧配置存量迁移
  - CLI 入口：`python/bin/python3 scripts/model_capability_probe.py --api-base URL --model MODEL [--api-type anthropic] [--lightrag] [--api-key KEY]`（退出码 0=档案已更新 / 1=探测失败未覆盖旧档；单场景预算 ≈11 次×10s，值域超时重试最坏 ≈140s 建议 timeout=150，双场景 ≈280s 建议 timeout=300 或分两次调用）
- **服务端行为变化记录**：豆包 2026-08-18 更新接受 reasoning_effort 全值（早前 none/xhigh/max 400 过期）；但 disabled 场景仍只接受 minimal/none——值域结论与场景 thinking 强耦合，探测必须按生产场景 thinking 配置测值域（enabled 下测出的全 supported 不能外推到 disabled 生产场景）。
- **教训**：① **顶层参数 vs extra_body 两通道行为差异**——顶层被 drop_params 静默丢弃产生假 200，验证参数是否上线必须抓 HTTP 发送层请求体（extra_body 送达检查法，raw_http 传输层 `NNNNNN.json`）；② 400 body=None 分类健壮性（body 含 token 是充分条件非必要条件）；③ 探测必须按生产场景 thinking 配置测值域。

#### 修复：配置热更新四层缓存 + 设置页状态机 + 探测提速（用户实机验收 2026-08-18）

- **配置热更新（定时任务用旧模型 → ChatQueue 卡死回归）**：
  - 根因：`get_chat_queue()` 初始化时缓存 runner（`_queue._runner`）——/api/config/reload 三层缓存（Config 单例/Runner 单例/LightRAG session）**漏 ChatQueue** → 定时任务（scheduler→ChatQueue）用旧模型
  - D2 回归：补清时 `_queue = None` 导致**新队列 worker 永不启动**（start() 仅应用启动时调用一次）→ 定时任务/IM/HA 消息入队后静默卡死（比"用旧模型"更糟，双审查抓出）
  - 修复：`ChatQueue.reload_runner()` 替换 `self._runner` 引用（不清队列不重启 worker——`_worker_loop` 每次处理读 self._runner，待处理消息自动用新配置；正在处理持旧 runner 完成）
  - `get_or_create_runner` 惰性配置比对补 apiBase/type/read_timeout（reload POST 失败时的兜底重载；**temperature 不加**——NiuRunner.__init__ 覆盖其值致恒不等、每次重建 runner）
- **设置页状态机（用户拍板语义）**：
  - 换模型检测：apiBase/model 任一变化（含 preset 选择）→ 立即清空四下拉框（思考链+推理深度）+ 不可选 + 测试保存灰 + 能力提示重置（D6）
  - 探测按钮**第一个动作** = 清空四下拉框 + 不可选（探测中档位未知）；探测成功 → 复写档位 + 可选 + 测试保存亮；失败 → 保持灰
  - init 未探测 → 只显示被选中项（不注入历史档案档位）；有旧配置 → 可点（D3：判定用"model 有值"非"档案存在"）
  - llm/lightrag 推理深度下拉框**档位统一**（lightrag 改用 llm 档案——两框动态注入内容一致；已知边界：lightrag 生产恒 disabled 下选 high/xhigh/max 档入库 400）
  - 探测按钮加载动画（"探测中"+循环动点）；设置页全部字体统一宋体（删 Ma Shan Zheng/Caveat + Google Fonts link）；Chat 斜杠命令下拉框 max-height 200→300px（/setup 不再被截断滚动）
- **探测提速（222s → ~40s）**：
  - **PROBE_MAX_TOKENS=8 对 thinking 模型太小**（08-13 压缩工程改 test-llm 5→256 时 model_probe.py 漏改）——豆包 thinking enabled + high/max 深度思考档连思考链都放不下，响应 9.5s+ 贴超时边界 → 8→256
  - **显式短 timeout 在模型深度思考档返回前主动放弃**（用户拍板：临时脚本不传 timeout 能测通、程序传 10s 测不通的差异根源）——探测不传 timeout（litellm 默认大超时，等模型真实响应 1.9-12s）
  - 值域 7 值并行（max_workers=**3**——服务端并发限制，用户拍板防限流）；结果按候选顺序排序（并行完成顺序不定）
  - **删除 rf/tools 探测项**（无档案消费点 + 用户未要求；response_format 探测归属"测试连接并保存"按钮的 testAndSave 流程）——探测项 12→10
  - 失败原因可读化：`probe_fail_reason` 透传前端（429 限流/401 认证/404 不存在/5xx 服务端明确提示，不再笼统"探测失败"）
- **ignores_unknown 误判修复**：豆包 enabled 场景"7 值全 200"可能是**真全支持**而非"忽略未知参数"——加**无效值探针**（INVALID_EFFORT_VALUE）判别：无效值 400 → 真支持（false）；无效值也 200 → 忽略未知参数（true）；探针无法判别 → 保守 true
- **http_logger**：stream=True 请求的响应即使 content-type 为 json 也是流式模式（未 read），访问 `.content` 抛 "Attempted to access streaming response content"——try/except 降级记 streaming note，不再刷错误日志（也是"只有请求无响应日志"的根因）
- **实测结论（Open Code）**：免费层 zen/v1 **429 限流频繁**（探测 10 请求全 429 + SDK Retrying；mimo/hy3 均限流）——失败提示明确"稍后重试"；**zen/go 包月端点模型名全小写**（用户输入 `MiMo-V2.5` 大写 → 401 "Model not supported"；`mimo-v2.5` 全小写 → 200）——**模型名大小写敏感教训**（26 模型：minimax-m3/m2.7/m2.5、kimi-k3/k2.7-code/k2.6/k2.5、glm-5.2/5.3/5.1/5、deepseek-v4-pro/flash、qwen3.7-max/3.8-max/3.7-plus/3.6-plus/3.5-plus、mimo-v2-pro/v2-omni/v2.5-pro/v2.5、hy3/hy3-preview、gpt-5.6-luna、grok-4.5）

#### 重设计：设置页面横屏三栏布局 + 0.2.1 发布（用户实机验收 2026-08-18）

- **背景**：原设置页 500×650 竖条固定窗口 + "高级选项"折叠，用户拍板重设计——横屏标准窗口、不要折叠全部铺开、三栏布局（第一栏内容多拐弯到第二栏顶上）、status 长文案挪第三栏下方空白区
- **布局（commits 67dd92f5 + b88f1916 + 23a59ea7 + 88e05443）**：
  - 三栏：左=对话模型基础（预设/Key/地址/模型/类型），中上=对话模型能力（思考链/推理深度/探测按钮），中下=知识图谱模型，右=上下文与压缩；窗口 1020×770 resizable（minWidth 960/minHeight 600）
  - 删除高级折叠（advanced-toggle/advancedFields/toggleAdvanced 零残留）+ logo 大图区 + 底部装饰 footer；探测按钮改低调虚线样式；提示文案缩短单行
  - **status 浮层**：`position: absolute` 浮在右栏下方空白区（按钮上方），长错误文案多行换行 + 超高内部滚动，**不占文档流**——出现时按钮位置不动
  - **坑**：absolute 定位的 right/width 百分比基准是包含块的 **padding box**（含 padding）——`right:0 + width:(100%-28px)/3` 右偏 20px + 宽度多算 13px = 出屏 33px（用户实机抓出）；修 `right:20px` + 宽度减 40px 双 padding
  - JS 逻辑零改动（全部 DOM id 保留）；继承场景入库探测按钮隐藏时 hint 文案指向对话模型探测（不再误导点击）
- **验证方法（UI 改动新流程）**：browser 工具 + `evaluateOnNewDocument` 注入 mock electronAPI（getPresets/getConfig 等）+ reload + 截图 + `getBoundingClientRect` 对齐断言——继承/非继承两场景一屏放下（scrollHeight ≤ viewport）、状态机（下拉框 disabled/enabled/testBtn 选齐）逐项断言——**main.js 主进程改动需重启应用生效，index.html 重开窗口即生效**（不重启会看到"新 HTML 塞进旧竖窗"的错乱布局）
- **0.2.1 发布**：VERSION + chat.html version-label 两处同步（d6325bd8）→ `rm -rf niu.app && ./launcher/build.sh --dmg`（hub start 进程跑无超时，12m45s）→ `dist/Niu-0.2.1-mac-intel.dmg`（941M）；bundle 内容验证（版本号/三栏/窗口尺寸全进包）
- **敏感信息核查（用户要求）**：本次 5 commit 仅 4 文件（VERSION/main.js/chat.html/settings index.html）零测试文件；测试文件全文扫描零真实 key（全假值占位 `"k"`/`"test-key"`/`"fake-key"`/环境变量读取）；`config/user-config.json` 在 .gitignore 从未追踪；browser mock 数据仅内存注入不写文件
- **挂起项关闭（用户拍板）**：①文件入库"Content already exists" = doc_status 残留 FAILED 记录未清理干净（非程序缺陷）②zen 模型问题已解决

### 2026-08-17

#### 修复：主 Agent 工具名带斜杠致 OpenAI 严格校验服务 400（zen 实测根因 + 剥前缀修复 + DB 清理脚本）

- **问题（用户实证，Windows 测试机）**：配置 opencode.ai/zen + nemotron-3.5-lightning-free，启动探测"hi"成功进程序，Chat 对话**立即 400**（"模型请求被拒绝，请检查请求参数"）；睡眠管道子 Agent（entity-extractor/dream-evolver/context-manager）全部成功；raw_http 只有请求无响应日志。
- **根因（二分实证链）**：① zen 上游严格校验 function name 规范（OpenAI：`^[a-zA-Z0-9_-]{1,64}$`，**禁 `/`**）——doubao/火山方舟宽松不校验所以历史无恙；② **ToolRegistry 内部注册键 = `server/tool` 带斜杠**（内部约定），子 Agent 路径（subagent.py L686）剥前缀发裸名（"LLM sees bare name"），**主 Agent 路径（runner.py `_assemble_tools_schema` L1159）漏剥**直接发带斜杠全名——不对称实现缺陷；③ 主 Agent 15 个静态工具中 `brain-region-server/brain_region_activate/dim/status` 三个带斜杠 → 单工具即 400；④ 次要触发点：messages.db 历史 assistant tool_calls 存带斜杠全名 + agent_loop L749-790 重发时给 tool 消息补 name 字段——旧会话历史原样外发同样 400。
- **诊断方法（可复用）**：本机换错误配置起 API 真实复现（curl /api/chat/session 重现 400）→ raw_http 提取失败请求体裸重放拿响应 → 单变量二分（messages 无罪 → tools 触发 → 逐工具单测定位 3 个 brain-region 工具 → 改名验证 `/`→`_` 全 15 工具 200）+ 6 连发判定确定性（6/6 400）；zen 免费层同时存在间歇 503（"服务忙"，独立现象）。
- **修复（main aed4e664，用户拍板范围：只修 schema 剥前缀 + DB 一次性清理，不做发送链路永久规范化）**：
  1. **runner.py**：`_assemble_tools_schema` name 剥 `server/` 前缀发裸名（`split("/", 1)[1]`，对齐 subagent.py 既有模式；dispatch 裸名自动解析 handler.py L1508-1517 既有零改动；该函数仅 2 调用点共用 = 单点全覆盖）
  2. **防回归测试**：test_schema_refresh_in_turn.py 新增 test_assemble_tools_schema_strips_server_prefix（假 registry 带斜杠名 → 断言无斜杠 + 裸名存在）
  3. **~~scripts/cleanup_tool_name_prefix.py~~**（一次性，用后已删）：messages.db 清理（--db/--dry-run + sqlite3 在线备份 + **JSON 解析判定禁 LIKE**（arguments 文件路径斜杠假阳性 31 条实证）+ 仅改 function.name + 验证不符即停）——本机 DB 实测 **0 污染**（105 条 assistant 扫描 0 命中——斜杠仅 Windows 测试机 doubao 时代产生）；Windows 测试机用户直接弃库无需清理
  4. **context-manager.md**：L83/L328 斜杠教学文本改裸名（防 LLM 学斜杠名再污染——子 Agent schema 本就裸名，提示词对齐）
- **双审查（修复前）**：AuditCodePaths + AuditDataCompat 双 scout——确认 L1159 唯一同类问题点、裸名零重名冲突、dispatch 双兼容、无测试锁定斜杠发送、其余持久化点全干净；**无阻断风险**。
- **验证**：双审 PASS（Spec S1-S5 + Quality APPROVE 2 Minor 已修权限）+ 测试 3 passed + **本机判定性实测：修复前同请求 400 → 修复后同配置同路径正常回复**（你好！老板有什么我可以帮您的吗）+ 测试消息 3 条已精确清理（DB 回 208 条，备份留存）。
- **Windows 机待办**：测试机直接弃库重配即可（用户拍板不清理）。
- **已知边界**：zen 免费层间歇 503/400 波动（上游不稳定，与 Niu 无关）；跨 server 裸名重名时 dispatch first-match-wins（当前注册表零重名，未来注册新 server 留意）。

#### 修复：IM 流式卡片终结统一化——chat_session 补 SEND 终结（定时任务异步子 Agent 回填触发飞书卡片"思考中"不终结）

- **问题（用户实证）**：定时任务（HN 新闻 10:00 触发）→ 主 Agent 异步调用子 Agent（chat-with-* async_mode）→ 第一轮正常终结（ChatQueue 分支 2 send_sync）→ **子 Agent 完成回填触发第二轮（前端 source='' → chat_session 路径）**→ 流式建卡 B（"思考中"）→ 会话末 `route_out(full_reply, "im", "")` 空 channel_id 退化为 **PUSH（send_markdown 独立消息，不终结卡片）** → 卡 B 永久"思考中"；同日 10:01:50 HA 门锁通知恰好走 ChatQueue 分支 2 send_sync 终结了当时的卡片——用户误以为"HA 修复了状态"。
- **根因**：08-12 只在 P1 ChatQueue 分支 2 补了 scheduler 特判 `send_sync` 终结；**P2 chat_session 路径对 force-only 会话（`_im_force` 粘性 True + `_im_channel_id` 空）无等价特判**——route_out 空 channel_id → PUSH 不终结。异步子 Agent 回填（source='' 触发 chat_session）首次系统性踩中。**点式修复教训实证**：08-12 修分支 2、08-15 收口 IM 推送，两次都没覆盖 chat_session 的 force-only 分支。
- **修复（main 3 commits：c3e78302 + 80a6a19e + 80b27198）**：
  1. **gateway.py 新增 `async def push_im_reply(runner, reply) -> bool`**（统一投递入口）：should_push_im 早退 → has_channel("im") 早退 → im_cid 非空 `route_out`（SEND，既有行为）→ **force-only `send_sync("", reply, pop_reply_to=False)`（SEND 终结，与分支 2 逐字对齐——条件仅 `_gw and _gw.is_connected`，不查 `_push_target`：READY 期快照与 adapter 实时 `_push_chat_id` 不同步，检查会导致 fresh-P2P 配置修复失效——R3/R4 双审交叉 P1-1）** → gateway 未连接回退 `router.push`（独立消息）
  2. **compat.py chat_session 投递段**（L2359-2371）：`if chat_error is None and runner.should_push_im(): route_out(...)` → **无条件 `await push_im_reply(runner, full_reply)`**（chat_error 也终结——错误文案流式期已进卡必须终结，对齐分支 2；非 LLM 内部异常 F3 失配以 accumulated 终结、无卡时错误文案独立消息发出）
  3. **ChatQueue 分支 2 零改动**（现状正确 + reply_to 串联语义 + `_im_finalized` 置位，改不得）
  4. **测试三文件**：test_push_im_reply.py 新建（5 路径 mock 单测，send_sync 参数级断言）+ test_chat_session_push_runtime.py 新建（rows 6/7 runtime mock：chat_error 非 None/None 均调用且传 full_reply）+ test_chat_session_im_push.py AST 改造（_extract_push_block 定位 push_im_reply；闸门收敛断言；新增 test_push_block_is_unconditional 父链无 If）
- **全场景矩阵（方案核心交付物）**：13 场景 A-M 逐链路分析——缺口集合 {E, G, K}（scheduler/ha-watcher + 异步子 Agent 回填 = force 粘性 + channel 空 + chat_session + PUSH 不终结）；A/B/L（无标志早退）、C（IM 走 P1 分支 1）、D/F（分支 2 零改动）、H/I/J（不新建会话/ask_finalize/程序消息不推）、M（channel 非空 route_out SEND）均正常。接受边界：`_push_chat_id` 会话期竞态（入站 P2P 消息改写——分支 2 同款既有）、gateway 断线窗口（临时悬挂非永久）、前端 isProcessing 竞态丢回填（既有数据面边界）。
- **质量链**：方案 v1.0→v1.3 四版 + **六轮双审**（R1/R2 CONDITIONAL 4P1+5P2 → v1.1 分支 2 零改动；R3/R4 CONDITIONAL **双审交叉同一 P1-1**（_push_target 守卫错误）→ v1.2 删守卫；**R5/R6 连续两轮 APPROVE 零阻断**）+ subagent-driven 实施（4 Task 每 Task spec+quality 双审：Task 1 gateway 双审 PASS / Task 2 compat 双审 PASS / Task 3 spec FAIL→复审 PASS（rows 6/7 缺失补齐）+quality PASS / Task 4 回归 PASS）+ 最终整体审查 **READY FOR DELIVERY**；**关键教训：①点式修复恶性循环的根治 = 全场景矩阵先穷举再动手（用户拍板"分析透彻了再动手"）②`_push_target` 是 READY 快照、adapter `_push_chat_id` 实时更新——两字段不同步，守卫必须用实时源 ③AST 测试锁结构（无条件投递父链无 If）与 runtime mock 锁行为（rows 6/7）互补，缺一不可**。
- **验证**：回归 10 文件 **93 passed 零新增失败零污染**（messages.db 181→181）；**实机验证待用户执行**（清单：①HN 定时任务+异步子 Agent → 第二轮卡片终结 ②HA 门锁通知回归 ③Electron 对话中回填不推 IM ④双端 IM 会话后回填行为不变）。

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
- **质量链**：计划 v1.0→v1.3 三轮双审查（R1 双 CONDITIONAL：P1 Document 守卫字段错 + P2 主进程改动验证需重启应用 + P3 行号；R2 A=CONDITIONAL 仅 P3（行号引用）B=APPROVE；R3 双 APPROVE——连续两轮零阻断达成）+ subagent-driven 实施（Task 1/2 并行，各 commit 独立可回退）；**关键教训：①force-graph 回调节点字段在顶层 vs _originalData 的层级差异——守卫类代码引用字段必须核对对象构造处（buildGraphData L220-228 实证）②main.js 主进程改动验证必须重启应用、renderer.js 关窗重开即可——验证前置条件按代码层区分 ③用户"建议回退"时不要立刻回退——先确认修复是否已被验证过；回退会移除代码，用户此后重启验证的是无修复版本 = 假阴性（实证：22:57 提交 → 23:02 回退 → 用户 23:11 重启验证无效——处理器从未被运行过；重新应用 2a1524f1 后用户重启验证通过）④Chat 复制粘贴有效 = before-input-event 处理器 + Chromium macOS 原生编辑快捷键双通道（chat 输入框 9+ 处自动 focus 使粘贴目标常聚焦；graph 搜索框从不聚焦是体验差异非根因）**。
- **验证**：两文件 `node --check` 通过 + `grep expandNode` 零残留 + 实施 diff 与计划逐字核对；**实机验证通过（用户 2026-08-16 确认）**：Task 1 重启应用后图谱搜索框 Cmd+V/Cmd+C 正常（2a1524f1 恢复后验证）；Task 2 关窗重开图谱后主图右键实体节点进子图（depth=1）+ 加减层级 + 返回总览正常；右键 Document 无操作；子图内右键保持原行为。

### 2026-08-15

#### 修复：strip_at_messages 删除回复空行（飞书卡片块不闭合——子 Agent 转述与主 Agent 话直接连接）

- **现象**：飞书 IM 中主 Agent 回复里子 Agent 转述块后主 Agent 的话直接连接（文本无分隔/表格吞并"简单说："入表）；Chat 页面正常（另起一行）。实证：Chat 显示 DB 持久化文本、飞书显示流式卡片累计，两条路径都过 strip_at_messages。
- **根因（实证链）**：LLM 原始输出（raw_http 20260815/000007）`...日志\n\n您看...`（空行存在）→ `strip_at_messages` 的 `if line.strip()` 过滤空行 + 单 \n 重连 → DB（messages.db a01d63e5）与飞书卡片均 `...日志\n您看...`（空行被删）→ Chat（marked）单 \n 显示换行（正常）；飞书 CardKit 列表/表格项内单 \n 折叠/吞并（连接）——同一文本两端渲染差异。
- **修复**：`strip_at_messages` 只做 `_AT_PATTERN.sub('', reply_text).strip()`——@ 消息段剥离 + 两端清理，原文换行/空行结构原样保留。@ 剥离残留空行保留（无害，段落间距）。函数签名/调用点零改动（12 处调用点核验：persist 去重双方同版本一致、纯 @ 回复判定 .strip() 保留不变、ask_user 问题文本无空行结构）。
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

#### 修复：LLM 不可用启动门控——模型不通时不启动依赖 LLM 的后台组件，配置成功重启后生效
main 7 commits：新建 niu_api/llm_ready.py（check_llm_ready 探测预算 read_timeout=120/wait_for=150，user-config llm.read_timeout≤190s 逃生口）+ lifespan 在 scheduler 前门控（llm_ready=False 跳过 scheduler/HAWatcher/IM gateway/db_monitor/脑区背景同步）+ 启动器三处对齐（test-llm 230s、preload 轮询 360→3600 防 180s 无限重启循环、settings socket 230s）。实机验证待用户执行。

#### 修复：定时任务小时级 cron 当天只执行一次——last_executed_trigger 触发点级记账取代日期粒度去重
新增 last_executed_trigger 列：同触发点再到期=崩溃重跑跳过，不同触发点照常执行（23:10 最后触发点不丢）；取舍：崩溃恢复从"漏执行"转"可能重复"（at-least-once）。

#### 修复：强制压缩链路四联问题（提示词瘦身/拒绝报告/首次停思考链/截断报错）
commits 172146da→dde63a28：压缩 task prompt 瘦身为严格输出契约、_build_compress_llm_config 统一 4 处压缩调用点、_probe_llm 检测 finish_reason=length 显式报截断、降级链门控只判 thinking type。

#### 修复：压缩后空壳 assistant 消息残留——_cleanup_orphan_tool_messages 扩展四条件空壳判定
commit 354581f6：content/tool_calls/tool_call_id 全空才清；原始形态（content 空但 tool_calls 非空=工具调用锚点）严格保留勿删。

#### 修复：主/子 Agent 提示词 Current Time 每轮实时刷新（原启动固定值跨午夜漂移）
commits 12f2674b（主 Agent 每轮 datetime.now()）+ 50a57196（子 Agent _refresh_subagent_current_time 回调正则替换）。

#### 修复：LLM 启动探测去重——单一真相源，正常启动两次真实探测降为一次
commits e63d38ea（llm-status 三态 ready/probe_failed/not_ready）+ a83c9868（启动器三态决策，ready 直接跳过 test-llm）。

#### 修复：脑区注入分级——点亮数感知熄灭加速 + 🟢5/🟡3/⚫0 分级注入 + 图遍历移除 + 会话边界清理
R1-R34 三十四轮双审（commits 65fd2108…adbcec81）：噪声放大器 _traverse_from_hits 整体删除（动态注入四处变三处）、cap 26 全局上限、clear_recent_region_entities 接线会话清理。

### 2026-08-12

#### 修复：定时任务飞书流式卡片永不终结——chat_queue 仅终结不投递 + adapter 死卡 pop 重建 + do_ask_user 来源闸门兜底
commits 79952acb+dcec016b+3e551f79；死路由是 pre-existing（scheduler 通道未注册路由，回复永不 SEND）。教训："原来没问题"≠机制不存在，先 diff 实证再下结论。

#### 修复：IM 推送判定收口 should_push_im() 单一入口 + 程序消息不推 IM
runner.should_push_im() 五消费点唯一判定入口；service.py reminder/background_script 不再 route_out 推 IM 只写 DB；用户语义三规则置位。

#### 修复：压缩后主动重算前端模型使用率（sleep 强制压缩后圆环不刷新 → 下次睡眠重复强制压缩）
8 commits 1a846726→4292cb54：compute_context_usage_estimate 同源估算 + compact done 事件带 usage/reset_tokens + chat.html 渲染代际守卫；mode-1 纯游标推进使用率不变是正确语义。

### 2026-08-11

#### 修复：主 Agent ask_user 工具（暂停问话）+ 轮中 schema 刷新 + @ 通道反馈闭环 + 通配路由存在性检查 + cleanup 注销通知（nutritionist 事故五层修复收尾）
教训：与用户交流必须有显式暂停工具；工具调用任何失败不得静默（无反馈=任务终止）。

#### 修复：飞书流式卡片打字机失效 + ask_user 问题不即时显示——IM 抽象层内容形态错配
main 9 commits 86d6c7a2→ca6473dc + 实测反馈 f2df81ac→8ff12548：CardState accumulated 累计全文适配飞书流式 API（传增量打字机永不触发）；双端场景 electron_pushed/im_pushed 独立判定。教训（铁律）：飞书功能必须先学官方手册再动手。

### 2026-08-10

#### 修复：主 Agent 停止立即返回——统一可中断执行层 run_interruptibly 覆盖注入检索/TTFT/工具执行盲区
commits c7493e4c…c4c9f740；教训：Python 线程无法 OS 级强杀，最优实现=后台 daemon 执行+前台轮询放弃等待；注入检索超时 120s→15s 参数化。

#### 新增：macOS Cmd+Q 拦截（assistant 模式精灵/Chat/图谱 3 窗口全拒绝）
commits b9943419+cd11d191；菜单 accelerator 条件化 + before-quit 守卫 + powerMonitor 关机放行；教训：dev 清理 SIGTERM 会被守卫拦下需 pkill -9，osascript 目标是库存二进制名 "Electron"。

#### 修复：定时任务重复发送——trigger_callback 改 fire-and-forget 消除等待超时重试窗口
commits 51485133+edd42231+7c165499；教训：通知类任务"送达即完成"，等 Agent 回复判成功必引入重复窗口；enqueue_sync 必须显式 channel="scheduler" 防双 IM 消息。

#### 修复：子 Agent 上下文压缩两级策略（tool 输出占位符化 → FIFO 兜底）+ 删除 targetThreshold 配置
commits 5cc3f890+a57bef47+2550d937+a0d1f671；占位符化幂等、达标即停、10 轮保护；压缩目标写死窗口 50%。

#### 修复：子 Agent 去掉轮数上限（max_turns=None）+ 未完成结果游标不推进
commits d48e2d9a/13b306e9/292268dd/3948eec2；call_subagent 后处理新增 incomplete JSON 契约，全库 11 处游标决策点不推进。教训：程序化终止必须有显式标记；PM 复核审查员行号类反馈必须 grep 实证。

### 2026-08-09

#### 新增：知识图谱时间链（会话日期链补全 + 主 Agent 认知 + dream-evolver 减负）
nap/sleep/force 三管道收尾 _ensure_session_chain() 补 followed_by 日期链，只补边不建实体。

#### 新增：LightRAG 关系方向语义说明（图本质无向，方向只在关系 description 文本）
查询工具 Schema/disk yaml 补输出契约；教训：disk_navigator readme 才渲染 tool.long，改工具描述须确认 readme 呈现。

#### 新增：程序触发子 Agent 显示标签页（nap/sleep/force 全程可见）
call_subagent_with_auto_answer 补 pre_register + subagent_started 推送 + 异常清理三件套；fa59f3ad 加固归属守卫。

#### 修复：dream-evolver 自建日期节点——insert_relation 工具契约修复
描述补"建链自动创建不存在实体"；教训：工具描述必须说明副作用语义；提示词对系统固定节点直接陈述机制、不用类比。

### 2026-04-15

#### 新增：KG 数据流入 5 条渠道全部实现（KuzuDB 时代：文档/照片/聊天/便利贴/批量整理）
#### 新增：梦境进化子 Agent dream-evolver 从 context-manager 拆出（增量游标 last_dream_evolve.json 已随 2026-08-25 MD 中继工程五退役）
#### 新增：便利贴后端 API + SQLite 持久化（notes.py/notes_api.py，~/.niu/notes.db）
#### 重构：context-manager 精简为压缩专用

### 2026-04-04

#### 修复：NiuHandler 缺少工作记忆机制——补 tool_after_callback/_get_anchor_prompt/next_prompt_patcher（agent/handler.py）
#### 修复：子 Agent 缺少 MCP 工具——get_mcp_tools_for_servers 按 server 过滤
#### 修复：空代码块显示——runner.py 正则清理
#### 新增：动态注入架构——Skills/MCP 工具描述按语义检索注入
#### 修复：同步/异步架构冲突——MCPSyncBridge 后台事件循环（已被 MCP 同进程化取代，保留为历史）
#### 修复：历史对话丢失（session_id 误作 limit）
#### 修复：MCP 工具未挂载——改用 get_or_create_runner()
#### 修复：人脸识别卡死——恢复 preload_face_model()

### 2026-04-03

#### 重构：GenericAgent 整合——GenericAgent 替换 Nanobot（约 53 万行 Go），pkg/ 目录清空；commits 06792e8/6c256a5/f388f58/b3c3ffd/9452d51
#### 新增：/new 清空聊天记录（历史 Go 后端实现，架构已迁移）
#### 新增：输入框支持多行输入（textarea；Enter 发送、Shift+Enter 换行）
#### 修复：主 Agent 工具丢失 + 缺少系统工具（Nanobot 时代历史）
#### 优化：Agent 提示词改进——抽象规则改具体操作指令
#### 删除：10 轮对话自动整理遗留代码

### 2026-04-02

#### 修复：照片拖入卡死（历史 stdio 时代）→ 已被 MCP 同进程化取代
#### 新增：人脸识别模型空闲卸载（MODEL_IDLE_TIMEOUT_SECONDS=300）；教训：卸载时不调 gc.collect()，让 Python 自然回收（否则可能释放使用中的对象致崩溃）
#### 修复：Electron 关闭时后端不退出（历史 Go 后端时代，现为 Rust 启动器 + /api/shutdown）
#### 新增：聊天历史加载（getHistory IPC + loadHistory 滚动加载；消息顺序最旧在上、最新在下）

**消息顺序**：最旧在上，最新在下，滚动到顶部加载更多。
