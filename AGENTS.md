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
11. 开工前必须读项目流程 Skill（niu-plan-review-gate-and-sdd / niu-sdd-execution-flow）；Skill 必须真读真执行。记住本地Agent优先的约定，只要不需要过大上下文的操作，优先使用本地Agent。双审可以一个本地，一个远端。

**违反任何一条就停下来，不要继续。**

---

## ⚡ 项目管理纪律（2026-09-09 用户当面定稿——每次会话必读）

### 当前工作：可视化功能（重做版，用户大白话需求）
1. 目标：Agent 能截屏、能看图——截图工具抓屏图，丢给能看图的模型拿回文字结论（Phase 1 niu-natives 抓图底座已交付、用户真机验证通过，勿动）
2. 视觉能力探测**只问主模型**：在现有「探测能力」流程顺带测（纯红测试图问主色判定），结果记能力 `input: ["text","image"]`；**设置页不加任何 UI、不加按钮、一个字符不改**
3. 有能看图的模型才挂截图/看图工具；没有能看图的模型 → 工具不出现
4. 主模型不能看图时，用户自行配置第三方视觉模型：**测试方法与配置方法写进 SYSTEM_MANUAL**，主 Agent 读手册自己测通、自己配；**程序不自动探测第三方模型**
5. 用户手工配置的配置段**必须持久保留**——切换其他模型、设置页保存任何配置都不得覆盖丢
6. 禁止项（2026-09-09 已全部回退重做，勿再犯）：设置页视觉模型 UI/表单；程序自动探测第三方视觉模型；保存链自动触发探测
7. 动手前必须用大白话与用户对齐需求；方向做错必须立即承认并彻底回退（Phase 2 错 12+ 小时被全撤的教训）

### 任务派发硬门禁（本地模型优先——用户连续多次提醒）
- Niu 项目任务派发：**单 Agent = `tasks[].agent` 显式 `"local-vision"`**（自包含任务、128K 内）；**双 Agent（双审）= A 角 local-vision + B 角远端 reviewer**；禁双远端、禁双本地并行（local-vision 单槽位排队）
- 系统模板默认 scout 是远端——Niu 项目不得套模板默认；每份派发逐个 tasks[i].agent 显式写。**靠门禁不靠记忆**

### 双审方法（门禁标准）
- 每轮 2 个异角度审查并行（A=设计/逻辑链场景走查；B=技术可行性/事实核查）
- **连续两轮双 APPROVE 零阻断发现才通过门禁**；每轮修订版本号+修订记录+commit
- 审查员输出限行数（≤15-25 行）防流中断；prompt 要求审查员扩大范围按完整逻辑链分析，不只限一条语句

### 工程流程要求
- 中等以上改动（跨 MCP 工具签名/多调用点/子 Agent 提示词）：plan 文档（`docs/superpowers/plans/`）→ 双审门禁 → SDD（每 Task 新鲜子 Agent + spec/quality 两阶段审查 + 微修闭环）→ 实机验证；PM 复核后才 commit
- **方案变更绝不允许直接实施**（无论看起来多小——用户原话「每次这么做完了都会带来一堆的 bug」）；用户拍板方案细节 ≠ 授权实施，拍板只是 plan 输入
- 测试纪律：只跑点名文件、全 mock 禁真实 LLM/图谱写入；实施后立即验证不拖延
- 高频炸点（历轮实证，plan 必须核查）：MCP 工具参数改动=TOOL_SCHEMAS+直调实现+stdio Tool() 副本+dispatch 分派多处同步；函数级 import 块随删随收缩；运行时配置 copy-once 到不了存量装机；游标/水位线失效需 fail-loud 兜底

---

## 工作原则

1. 修改代码必须经过用户同意，说清楚修改的原因。
2. 未经同意，不得覆盖仓库内任何备份。
3. 从仓库恢复代码时，先回忆上次备份的内容；不确定就不能盲目恢复。
4. 遍历仓库历史测试原历史代码时，先把当前代码做临时提交。
5. 代码调试过程中验证无效后，必须马上撤销调试代码，恢复原始干净代码，再增加新的调试代码。
6. 项目代码量较大，为保护上下文窗口，无需长期记忆或大代码量的遍历工作交给子 Agent。
7. 代码质量优先，用户不在乎 token 消耗。
8. 版本号变更**只改根目录 `VERSION` 文件一处**（单一真相源，0.3.5 起）：`ui/main/windows/assistant/chat.html` 的版本 label 经 `preload-chat.js` 的 `APP_VERSION` 常量、`niu_api/compat.py` 的 `User-Agent: Niu/<版本号>` 经 `_read_version()`、`tests/test_list_models_endpoint.py` 的 UA 断言动态读 `VERSION`，全部自动同步。**未来新增版本号消费点一律读 `VERSION`，禁止新增硬编码副本**。Cargo.toml、package.json、pyproject.toml 等文件中的 version 字段是各子包的开发版本号，与产品版本号语义不同，**不要**强行统一。
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
- `agent/runner.py` — `_on_before_llm()` 每轮 LLM 调用前重读 memory.json + 刷新动态注入（轮次级）
- `agent/runner.py` — `_on_turn_end()` 每轮结束后脑区 decay_all + tools_schema 刷新（轮次级）

**轮次级刷新机制**：
- 每次 LLM 调用前，`agent_runner_loop()` 调用 `on_before_llm` 回调：重读 memory.json 重建 system 静态区，并经 `_inject_dynamic_resources` 向量检索刷新动态块。
- 每工具轮结果 persist 落库后（tool_results 非空）调用 `on_tool_round_refresh` 回调（主 Agent 专用）：从 DB 全量重建视图并原地替换 messages（`assemble_view_sync` + `transform_history`，与入口同一套组装流程——新输出编号/折叠态/仪表盘与 DB 同步；动态块由下轮 `_on_before_llm` 幂等重插）。子 Agent 不传 = None 跳过。
- 每轮循环末尾调用 `on_turn_end` 回调：脑区激活衰减（`decay_all`）+ tools_schema 刷新（`~/.niu/agents/` 有变化时重算 base 集）。

**工具生命周期（衰减-覆盖评分模式）——已退役，非迁移**：
- 旧 `agent/tool_lifecycle.py` 已删除；其工具分数衰减（-10/轮）、向量检索命中覆盖、`hit_tool()` 逻辑未迁入任何模块（生产代码零残留引用）。
- MCP 工具不再参与分数制动态注入/移除：visibility=static 的直接注入 tools_schema，hidden 的经虚拟磁盘 `disk()` 统一访问。

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
| `config/llm-configs.json` | LLM 命名配置合集（选择设置保存的命名配置，键=配置名=llm.presetId，条目=llm+lightrag_llm 两段快照） |

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

## 工程历史归档

日志区仅保留近期工程与仍在引用的终态（原样节）与压缩索引行。完整历史在 `docs/AGENTS-HISTORY.md`（压缩移出）与 git 历史——查旧工程/旧 commit 链 grep `docs/AGENTS-HISTORY.md` 或 `git log -- AGENTS.md`。

### 2026-09-10

#### 工程：LLM API 合规 + 工具消息顺序规整（k3-256k 400 根治；用户要求全面审计「别过几个月又告诉我别的地方还有不合规」→ 12 项全修；plan v0.1→v0.7 八轮双审门禁（R1-R6 阻断收敛 / **R7-R8 连续双 APPROVE 通过**）+ SDD T1/T2 每 Task 双审+微修复审闭环，main db745ff7/d48a9c21，docs 1d015b6 plan 冻结）

- **背景**：切 k3-256k（严格校验 OpenAI 消息序列）后主/子 Agent 400。病灶=ask_user 交互产生 `assistant(tool_calls)→user→tool` 顺序（DB rowid 3596/3597/3598 实证）——规范要求 tool 响应紧跟 assistant(tool_calls)。全面审计（远端，vendored SDK 类型+litellm 源码为权威）出 12 项：顺序不合规 + tool content 展开为 list（P0）/assistant 图片展开/tool 带 name 字段（三处）/压缩切片孤儿 tool/LightRAG schema 缺 additionalProperties（每次实体抽取白跑一次 400）/Claude 字段按模型名注入泄漏进 OpenAI 协议/extra_body 跨协议/MIME 按扩展名猜/stream_options 与 stream=false 同发/存量 list 污染链两链（T3 记录的"档膨胀"边界现为 400 根因）。
- **核心架构定案（R4 双审引出的根因修正）**：合规化落点=**发送层单点 `LiteLLMSession.chat()`**（全仓唯一 litellm.completion 出口，:1080/1145/1210 三调用点）——sanitize 返回副本不回流调用方 → 长度不变式（persist `[history_len+1:]` 切片）、落库污染链、子 Agent 档续跑累积三个 P1 同时根治；transform_history 去图片展开（4 调用点清零，全链路 content 恒 str，子 Agent 档体积回落）。
- **机制**：新模块 `agent/generic/message_sanitizer.py` 六步固定执行序（①subagent_msg 丢弃 ②孤儿 tool 丢弃（先于③防带图孤儿残留合成 user）③图片合规化 ④非 user list 降级（**system 保留**——cache_control 压平会静默废 prompt caching）⑤悬空按 tc 剥离（全悬空才整消息降级）⑥顺序规整（全局配对，合成 user 归位 tool 块后））；tool 图段→合成 user 消息承载（OpenAI 规范 tool content 仅文本；agno#7661 业界 fallback=synthetic user message）；`vision_enabled` 派发层判据经 llm_config 显式键透传（主 Agent `NiuRunner.__init__` 派生先于 create_client；子 Agent `_resolve_subagent_has_vision` 覆盖 llmPreset/vision_llm；续跑 `suspended_client.backend.vision_enabled` 赋值照 stop_check 先例）；chat() 入口重绑使 raw_http=真 wire 体（验收可核验）。
- **质量链亮点（双审价值实证）**：R3-B 抓出「D5 收窄会切断截图→模型唯一图通道」（Phase 2 刚交付的核心能力险些被我自己的合规修复废掉）→ 合成 user 消息重设计；R4 双审各抓一 P1（长度不变式/档累积）同指根因「发送专用变换放在持久消息层」→ 架构修正；R5-B P0「adapter 取不到 capabilities → 图永不展开」；R6 **双审同抓**「主 Agent vision_enabled 写入点晚于会话构造」→ 派生点钉死 __init__。双审同抓=缺陷真实性强信号再验证。
- **验证**：T1 95 + T2 224 + 全量回归 331 passed；ruff 零新增（stash 对比）；wire 级 test_llm_api_compliance 13 用例（构造级传递锁 spy create_client 防派发层接缝回归）；微修 2 轮（T1 QualityB P2 相邻 assistant 归位全局配对重写 / T2 SpecA P1 current_time fixture + P2 llmPreset 断言）均复审 CONFIRMED。
- **已知边界（接受）**：test_lightrag_manager 2 failed + test_response_format_probe 2 failed 为预存（非本工程引入，stash 实证）；camelCase apiBase 键分支保留（QualityB 实证 llm_proxy.py get_llm_config 生产 camelCase 路径真实存在，SpecA「死代码」结论被反证——**两角结论相反时以直接验证为准**）；`_derive_provider_prefix` 域名为子串匹配（api.anthropic.com.evil.com 理论误判，apiBase 用户自配，既有行为非本 diff 引入）。
- **实机验证清单（待用户重启 Niu）**：①k3 继续含 ask_user 历史会话不再 400 ②ask_user 交互后继续正常 ③子 Agent 派发正常 ④界面顺序不变 ⑤长会话压缩/手动 /compact 正常 ⑥截图→模型同轮见图 ⑦LightRAG 实体抽取无 400 白跑 ⑧raw_http 核验（无 name/OpenAI 协议无 cache_control·beta 头/stream=False 无 stream_options/无 subagent_msg）⑨MIME 与载荷一致 ⑩anthropic 路由 extra_body 无 reasoning_effort ⑪续跑旧档不 400、新档纯文本

#### 工程：可视化功能 Phase 1——niu-natives Rust crate（用户四问拍板+法律考察+三轮 spec 双审+三轮 plan 双审；main e3230788..b70dd995 7 commits，docs b7594b4 spec v0.4 冻结/7483554 plan v0.4 冻结/5e19ab1 勘误⑦⑧）

- **目标**：给 Agent 加可视化——屏幕/窗口/区域抓图+浏览器截图+视觉模型解析，第一期只「看」不「操作」但架构留备。**分 Phase**：P1=niu-natives Rust crate（本条目）；P2=vision-server MCP+动态挂载+vision_llm+探测（下阶段）；P3=browser_screenshot。
- **用户拍板（2026-09-09）**：①屏幕+窗口抓图（浏览器非文字 DOM 一期不解决）②操作能力后期要、一期不做但输入/坐标 API 随采集层一并移植暴露 ③MCP 工具 static 直挂主 Agent（不走 disk）+**解析模型=动态参数**（无视觉模型=工具不出现；有才把模型名挂 enum 主 Agent 自选）④采集方案=Rust+PyO3 复用 omp desktop（方案锁定后反推）⑤探测不加按钮（现有探测流程加一项）⑥requirements.txt 完整性+README 明谢 ⑦screenshot/analyze_image 分离 ⑧ax_lite（macos/ax.rs ~300 行聚焦子集保留——完整 ax 裁掉则 macOS 窗口聚焦输入断链，plan R1-A P1-2 实证后补拍板）。
- **法律考察**：OMP=MIT 三层一致；18 crate crates.io API 逐个实查全宽松（xcap Apache-2.0/enigo MIT/image MIT-Apache 等，零 GPL）；NOTICES 归集 297 crate 对账零遗漏（cargo vendor 机制）；**r-efi LGPL 选项核实不进产物**（getrandom UEFI-only 传递依赖，mac/win 编译树零命中）。
- **spec 三轮双审**（v0.4 冻结 b7594b4）：R1 双 CONDITIONAL（**双审同抓 enum 值语义矛盾**——模型名 vs 标识）；R2 双 CONDITIONAL（交互日志 base64 明文泄漏/static 注册绕过门控/nightly 构建链——R1 吸收双角验证通过）；R3 双 CONDITIONAL 零 P1/P2（§5.1 重复块自相矛盾双角同抓/vision_llm.model 空语义矛盾）。
- **plan 三轮双审**（v0.4 冻结 7483554）：R1 A REVISION 3P1（T1 验收不可达假拆分/macOS 输入与 ax 纠缠→用户拍板 ax_lite/region 是新增实现非移植）；R2 双 REVISION（**双审同抓 task.rs 依赖断裂**——T1 保真验证改 diff 不编译）；R3 双 CONDITIONAL 零 P1（**双审同抓 xcap 平台声明缺 Windows**）。
- **SDD**：T1 搬运（diff -r -x linux 零差异）→T2a 手术（ax_lite+PyO3——删 task::blocking 同步直调/py.detach/哨兵 build.rs/21 tests）→T2b（region 逐显示器求交混合 DPI+geometry wire+map_point 纯函数——21 passed）→T3（maturin wheel 装 python/+otool 干净）→T4（build.sh 构建步骤+Info.plist NSScreenCaptureUsageDescription+NOTICES 进 Resources）→T5（requirements.txt 注释段/README 致谢/NOTICES 297 对账/manual-dependencies）→T6 真机（map_point 4/4 PASS；TCC 权限拦截 permission_denied 正确分类——非静默黑图，印证设计）。
- **实施双审双 CONDITIONAL→修复闭环**（b70dd995）：A 零 P0/P1；B 抓 P1 **win32/input.rs 6 处 let-chain**（edition 2024 语法 vs crate 2021——mac 构建不触达漏网，Windows 首编必炸，cargo check windows target 验证修复）+**双审同抓 Windows 署名缺口**（pack.bat /xd 排除 niu-natives/LICENSE 悬空——NOTICES 补 omp 三版权行+MIT 全文双平台覆盖）；勘误队列补⑦detach⑧AxFailed 保留。
- **关键教训**：①**T1 保真验证用 diff 不用编译**——「删 napi 没接 PyO3」的中间态不存在（编译必然红），diff -r 零差异比 build 绿更直接证明没抄错；②**omp 的 #[napi] 绑定层是装饰不是骨架**——task::blocking 只是外层 Promise 包装，SessionCore.call 本就 flume 同步等待，删包装即同步语义（复用率 >90% 成立）；③**ax 裁切有纠缠成本**——macOS 输入的窗口前置聚焦链住在 ax 里（window_root/perform），「保留输入能力」=保留聚焦子集 ~300 行（用户拍板），不是删 3 个文件；④**双审同抓 5 次**（enum 语义/重复块/task.rs/xcap 平台/Windows 署名）——独立同抓=缺陷真实性强信号；⑤**crt-static 落点**：Cargo.toml [target.cfg.rustflags] 被 cargo 静默忽略（实测），唯一生效=niu-natives/.cargo/config.toml；⑥**派发纪律**：local-vision 用 eval agent(agent="local-vision") 代码接口，task JSON 我连续漏 agent 字段 8 次被用户抓——代码接口绕开。
- **已知边界**：TCC 未授权终端的真机抓图闭环待用户授权后复验；Windows .pyd 构建+真机验证留 Windows 机（用户实机清单）；omp 一次性 fork 无同步机制；输入 API 仅暴露不挂 MCP（hasattr 级验证）。
- **spec 勘误队列（Phase 2 收官随勘误 commit）**：①URL badlogiclabs→can1357 ②windows-sys 0.52→0.61 ③enigo(mac/win)→win-only ④「用户授权一次」ad-hoc 失真 ⑤ax ≈2200 行→ax_lite ~300 行 ⑥pointer 占位→四方法 ⑦detach 替代 allow_threads ⑧AxFailed 保留。

#### 工程：图谱例行事件清理 + dream-evolver 防复发（用户发现 entity/dream 行为分裂；spec v0.4 四轮双审收敛（R1 双 REVISION 7P1+4P2 / R2 双 REVISION 名单附件缺失 / R3 双 REVISION 命名精度双审同抓 / R4 A APPROVE B 1P2 大小写修正）+ plan v0.3 两轮双审（R1 双 REVISION T3 死步+重扫越权 / R2 B APPROVE A 1P2 vdb base64 判据）+ SDD T1-T3 + 实施双审 A APPROVE/B 1P2 KV 残留遗漏修复，main 827ae85e/e2dc992f，docs 23484fd/2d0e202）

- **背景**：用户发现 entity-extractor（000003）正确跳过例行消息（"全部定时任务触发的例行流程、无用户新增事实→跳过"）、dream-evolver（000005-000010）把同一批例行执行建成 event 实体+followed_by 时间链——行为分裂。DreamVerify 独立验证：诊断成立且污染规模 25+/天递增（HN 每日事件 17 个名称漂移 8 种+咖啡机 7 个+journal 8 个+门锁等）。
- **用户拍板**：时间链上例行任务事件全删（"咖啡机的提醒事件、每天的新闻，这些都是没有价值的东西"）+ 同步向量库；边界裁决：保留有真实上下文的门锁事件（回家取快递/购物回来/买蒜薹回家——B 审从 scout"generic 无上下文"误判中抓出）+ 安全告警（异常开锁）；journal 文档删。
- **机制**：删除经 MCP `lightrag_delete_entity`（唯一正确入口——graphml 级联删边+vdb_entities/vdb_relationships 同步删，直接改 graphml 留孤儿向量不可接受）；67 个实体逐字名单冻结附件（`docs/superpowers/specs/2026-09-08-routine-event-cleanup-list.md`）；删除经 HTTP DELETE /api/inject/resource/<name>（67/67 成功 61 秒）；KV 残留清理需停 Niu（JsonKVStorage 内存覆写）；11 项任务定义实体改 type=concept（关闭 dream 先例跟随通道）。
- **防复发**：dream-evolver.md 三处——实体提取规则新增判据（自动化例行执行/通知不构成事件，按性质：定时任务/后台子 Agent/设备例行通知前缀族，例外=用户新表达/参数变更/系统异常安全告警，按条消息生效）+ A1 指向句 + 阶段B步骤3 时间链限定（只连接真实事件）；通用化（无本地实例——用户指正 HN/咖啡机等是本机内容其他用户没有）。
- **验证**：graphml 3167→3100 ✓ / vdb_entities 3181→3114 ✓ / vdb_relationships 5144→4893（-251 精确吻合）✓ / 三 vdb consistent ✓ / 名单残留=0 ✓ / KV 四文件清零（entity_chunks 33+relation_chunks 136+full_entities 34+full_relations 133——实施双审 B 抓出后两个文件遗漏）✓ / 备份 ~/.niu/backups/lightrag_routine-delete-20260908/（83MB）。
- **关键教训**：①**scout 枚举清单不能直接信**——HN 39 复现不出（实际枚举 27 个 dated+~8 无日期≈35）、门锁漏 2 个、generic 定性错误（买蒜薹被误判）；删除不可恢复的操作必须名单逐字冻结+全量枚举对账。②**名称大小写陷阱**——graphml node id 小写「每日hn热点抓取」vs data 属性大写「每日HN热点抓取」，edit_entity 按 node id 匹配（A 审说改大写/B 审实证 node id 小写——两个审查结论相反时以直接验证为准）。③**执行顺序**：防复发提示词先行（截断流入），删除随后——倒置则清理后新污染继续产生。④**vdb matrix 是 base64 字符串**——一致性判据 len(matrix)==len(data)×4096，len(data)==len(matrix) 恒假。
- **已知边界**：约 40 个新闻话题实体失边孤立（非损坏，自然归宿）；vdb 孤儿 14 个不属本工程（MCP 不可达，如需清理走 lightrag-data-repair）；KV full_entities/full_relations 已清但未来同名 insert 时会重建（惰性）。
- **实机验证（待用户重启 Niu）**：图谱检索正常 + dream 下次运行不再建例行实体。

#### 工程：例行数据 daily 区域——轻提醒注入区（用户拍板备忘 5 条+问答 4 条；spec v0.2→v0.4 三轮双审收敛（R1 双 CONDITIONAL 3P2+8P3 / R2 双 CONDITIONAL 1P2+5P3 / R3 终核双 APPROVE）+ plan v0.2 R1 双 CONDITIONAL 6P2+7P3→R2 复核双 APPROVE + SDD Wave1 三 Task 并行 + 实施双审 A APPROVE/B CONDITIONAL 1P2，main 62aad058/84236b55）

- **需求（用户拍板）**：memory.json 新增 daily 区域（按数据源分 key，各槽独立）；每条带 expires_at（写入方自定保鲜期）；写时清理 lazy TTL；只动 daily 区域其余字段原样写回+原子写；动态块新增轻提醒行（只显示未过期，插在 [暂存事项] 上方）；主 Agent 提示词加一句指向 Skill。**Q1 写入通道**：新增 MCP 工具但必须隐藏，主 Agent 只能经 disk() 调用。**Q2 上限**：20 key+100 字符，Skill 讲清；大内容走指针模式（写 ~/.niu/tmp/ 临时文件、daily text 写路径指针、expires_at≤24h 因 tmp 每日清理）。**Q3 单行同构**照 [暂存事项] 行。**Q4（用户中途指正方向）**：例行数据写入由 **background_script 后台静默脚本**（主 Agent 编写，主 Agent 不能用 MCP 工具）完成——脚本经 sys.path 推导 + `from niu_memory_server import daily_set` **import 同一函数直调**（不走 MCP 协议，与 MCP 工具同一份实现），非子 Agent。
- **机制**：①`_write_daily_only`（锁内 read_text→**解析失败 raise 拒写**（对 _write_parked_only 静默 {} 回退的唯一偏离点——盲镜像会覆写销毁 memory.json，R1-A P2）→daily 非 dict 视为 {}（照 parked 先例）→清理判据（缺失/不可解析/<当前时刻字符串序）→mutator→mkstemp+os.replace）；入口 _read_memory_json()._raw_fallback 拒写（双闸）；ISO 带偏移转本地剥偏移存秒级裸串；text 单行化（\n/\r→空格）；upsert 豁免仅限清理后仍存在的 key ②**三处注册同步**（R2-B P2 抓出：主进程/disk 真实注册源是 **TOOL_SCHEMAS** 非 get_tool_definitions——漏则工具不注册 disk 调用失败）+call_tool dispatch+模块级别名 ③读侧 `_daily_reminder_line`（照 _park_reminder_line 先例同构，isinstance 判据，只过滤不清理——**读侧混写副作用会使纯读函数每轮触发写盘，清理只在写侧**（对备忘第 3 条的透明偏差））④disk memory-server.yaml 目录 description 列全功能（现状连话题暂存都没列——主 Agent ls / 只能看到 description）⑤Skill 讲清两条写入通道+上限+指针模式+background_script 机制引用 scheduled-tasks.md 不重复教学。
- **质量链**：spec R1（A P2 损坏守卫盲镜像风险/B P2 主进程写方失实（config-manager save_memory 无锁写）+text 单行化）→R2（**B P2 TOOL_SCHEMAS 注册锚点**/A 锁内 raise）→R3 双 APPROVE 冻结；plan R1（**双审同抓 grep 守卫缺失**/text 非空校验丢失/深比较断言/disk 集成测试）→R2 双 APPROVE；实施双审（A APPROVE 零 P0-P2 跨 Task 接缝三方一致——yaml parameters↔函数签名↔TOOL_SCHEMAS required/B CONDITIONAL 1P2：**Skill 脚本例子丢弃 daily_set 返回 error 字典**——设计内失败不抛异常，try/except 捕不到，区域写满/文件损坏时脚本静默失败无人知晓，违反 Skill 自写静默纪律→两例子补返回值检查）。
- **验证**：51 passed 9 skipped（T1 15+T2 10+T3 disk 8+回归 26，1 failed 为 test_disk_integration brain-region static **基线预存失败** git stash 实证与工程无关）+ ruff 零新增（stash 对比）+ A3 disk 链路同进程可调 + A4 registry.get_static_tools() 无 daily + grep mcp-servers.yaml 零条目。
- **关键教训**：①**用户中途纠正是最贵的需求信号**——v0.1 把写入通道设计成 MCP 工具+子 Agent 是方向错误（用户原本意图=主 Agent 写 background_script 脚本，脚本不能用 MCP 工具）；subagent 路线整套推翻改双层通道。②**写 memory.json 的镜像先例有坑**：_write_parked_only 读路径静默 {} 回退，盲镜像=损坏时覆写销毁全文件——镜像先例必须读实现非读注释。③**Skill 教"静默脚本"必须教错误可见性**：设计内失败返回字典不抛异常时，可抄例子若只 try/except，脚本静默失败永远无人知晓——例子必须检查返回值。
- **已知边界（接受）**：读侧不物理清理（过期条目滞留文件不可见无害，下次写入清）；跨进程锁不互斥 lost-update 窗口为既有现状（config-manager save_memory 无锁写，根治需 flock 覆盖全部写方另开工程）；带偏移手写条目读侧字符串序错排（工具路径归一化保证）。
- **实机验证 ✅ 已通过（2026-09-08 用户实测）**：①天气脚本 weather_daily.py 建好+cron 30 6 * * * ②动态块 `[例行数据] 1 项：weather〈石家庄 强阵雨 14.9-20.5°C…〉` 正确显示 ③`ls /` 目录描述含"例行数据轻提醒" ④Skill 已同步 ~/.niu/skills/ ⑤脚本按 Skill 规范编写（sys.path 推导+返回值检查+静默纪律+expires_at<24h）。

##### 方案变更：写时清理→读时清理（用户拍板 2026-09-08；plan v1.2 R1 双 REVISION→R2 双 CONDITIONAL→R3 双 APPROVE + 实施双审双 APPROVE，main 202ab070/16a95559，docs 8c26b40/419885d）

- **变更**：清理从写侧（daily_set/daily_delete 每次调用都清理）移到读侧（_daily_reminder_line 每轮读取时物理删除过期/畸形条目）；写侧 _clean_expired_daily 删除、返回值 cleaned 字段删除；disk yaml/TOOL_SCHEMAS/Skill/SYSTEM_MANUAL 措辞统一为「到期自动清理」
- **关键教训**：①**回写删除判据必须是三件套补集**（非 dict/缺失/不可解析/字符串序过期——只写字符串序会让畸形条目永久残留+每轮空回写，R1 双审同抓）②**清理必须先于空串返回**（`if not valid: return ""` 提前返回会导致全过期永不清理，R2-B P2 抓出）③**测试适配指令必须精确到断言形态**（复合断言删子句 vs 字典相等删键，「删该行」会误删 status/deleted 钉，R2 双审同抓）
- **已知边界（接受）**：_memory_file_lock 进程内锁不跨进程（background_script 子进程 lost-update 窗口，低频+TTL 自愈，与 spec 既有现状对齐）
- **验证**：28 passed（T4 测试适配后）+ A1-A5 验收全绿（cleaned/清理措辞零命中 + syntax 四文件 + 回归）

#### 工程：版本号单一真相源——四处硬编码消除（用户拍板；spec v0.2 R1 双审同抓 P0（preload 文件写错）→R2 复核双 APPROVE + plan v0.2 R1 双审同抓两处→R2 双 APPROVE + SDD Wave1（T1→T2 串行+T3a 并行）+ 实施双审双 APPROVE，main dbac2fe9/a298fcb1）

- **背景**：版本号手工同步四处（VERSION/chat.html/compat.py UA/测试断言）已连续两次漏同步（0.3.3→0.3.4 漏 compat.py+测试——测试钉同一旧值一直绿未暴露，0.3.5 升级才发现补）。用户拍板：改为读 VERSION（"是不是只需要改造两个地方去读 Version"→ 实际 5 个改动点已说明获批）。
- **机制**：①build.sh 资源复制段加 `cp VERSION → Resources/` + fail-fast（缺失即构建失败，永久结构保证）②compat.py `_read_version()`（`Path(__file__).parent.parent/"VERSION"` 两环境通吃：开发=仓库根、bundle=Resources；每次调用读文件不缓存；失败→'dev'）③preload-chat.js 暴露 APP_VERSION 常量（照 FONT_FACE_CSS 先例）④chat.html version-label 静态清空+body 尾部主脚本 JS 填充 ⑤测试动态化+防回潮守卫（chat.html 无硬编码版本/compat.py 无 Niu/x.y 硬编码/preload-chat.js 含 APP_VERSION 接线断言）。
- **双审同抓 P0（spec R1）**：v0.1 把 APP_VERSION 写进 preload-assistant.js——但 chat.html 由 **preload-chat.js** 服务（main.js L188 实证；assistant 只服务 spirit 窗口），按它实施 label 恒显 vdev；且 mock 注入 electronAPI 结构性抓不到接线错误 → 补 preload 接线静态断言（断言③）。
- **plan 双审同抓**：T1/T2"并行无文件交集"与测试依赖矛盾（守卫断言①③被测对象是 T1 产物——**文件无交集≠测试无依赖**）→ T1→T2 串行；T4 把 A3③"真启动目检"加"或文件核对"降级出口——**plan 零削弱验收权**（上轮零新增语义权的对偶）→ 删。
- **验证**：A1 grep 零命中 / A2 动态性（VERSION→9.9.9 测试绿→改回 git diff 干净）/ A3① mock（label=v9.9.9-mock）/ A4 变异必红 / A5 bundle Resources/VERSION=0.3.5+fail-fast 构建日志可见 / A7 JS 42+pytest 43 绿；实施双审双 APPROVE（零 P0-P2）。
- **已知边界**：A3③（打包后真启动 .app 目检 label）因开发版 Niu 运行中未做——**转用户实机清单**（重启 Niu 或重开 chat 窗口看 label=v0.3.5）；Cargo.toml/package.json/pyproject.toml 子包版本号不动。
- **skill 过时注明**：niu-version-bump-and-package 的"VERSION + chat.html 两处同步"已过时——现只改 VERSION 一处（skill 是用户文件未改，用户自行决定更新）。

#### 工程：命名配置合集替代厂商预设（选择设置）——LLM 命名配置双文件模型（用户拍板 Q1-Q5 + Agent 编辑坑规避；spec v0.3 R3 终核双 APPROVE + plan v0.2 R2 复核双 APPROVE + SDD Wave1-2 + 实施双审修复闭环复核双 APPROVE，main 5934c8e4/7534eb54）

- **需求（用户拍板）**：删 `config/llm-presets.json`（厂商预设用处不大）；设置页"选择预设"→"选择设置"（Q1=自绘下拉输入框同模型名称款：打字=新名/选中=加载）；测试保存时名字空→提醒必须输入，有名字→重写合集该名下配置；新建 `~/.niu/config/llm-configs.json` 记录全部命名配置（与 user-config.json 同目录）；选中调取旧配置测试保存后启用。**Agent 编辑坑（用户特别提醒）**：配置项可由主 Agent 自行编辑，文件内容比表单多——保存时多出的内容必须一并保存进合集；**手册必须强调主 Agent 修改配置要两个文件一起改**。Q2/Q3=保存所有字段（含 Agent 添加，llm+lightrag_llm 两段），以 user-config.json 为主合集为辅（合集有不同→改为与配置文档相同），前提=测试通过才写文档；Q4=presetId 就是配置名（一回事）；Q5=不做删除（文档开放主 Agent 可改，配置可覆盖）。
- **机制**：①合并纯函数 `ui/main/lib/config-merge.js`（UMD，main.js require/index.html script src 双通道）——**两层基底**（llm/lightrag_llm 段基底=loadedNamedEntry ?? 文件，storage/logging/context/Agent 顶级段恒取文件）+ 表单覆盖 + 空值语义四枚举（max_tokens 空=删键/thinking 空=删子键/RE 空=写""/temperature NaN→基底??0.2）+ probe 产物键覆写基底 + presetId=configName；②保存链路（spec §3.2）：名字 trim 空拦截（不发请求）→ 开头重读+前端合并→测试/probe（payload=合并值，read_timeout 逃生口特判被深合并全覆盖取代）→ 通过才写：后端保存时刻重读合并→写 user-config.json→读合集单条 upsert 两段快照（损坏不写+collectionWarning+reload 恒发+警告不关窗）；③加载流程（spec §3.3）：选中时重读条目→回填（含 llmMaxTokens）→跳过 clearCapabilityDropdowns（选中即可测试保存）→loadedNamedEntry 置位（**恒 null 直至选中——init 不置位**）；④config-manager 机制化双改（spec §3.6）：preset_id=整条两段加载+归一化+跳过同步 / 逐项修改写后同步（损坏→warning 不写）/ list_llm_configs / 畸形条目 isinstance 守卫 / 包装层类型校验。
- **质量链**：spec R1（A CONDITIONAL 3P1 / **B REJECT P0 深合并基底矛盾**——"以 user-config.json 为主"被误用为组装基底，切换场景 lightrag 永不切换+A 额外键渗入 B）→ v0.2（基底优先级=表单值>loadedNamedEntry>文件）→ R2 复核（**双 CONDITIONAL**：probe 产物归宿**双审同抓**；§3.6 段污染 A 抓+损坏保护 B 抓）→ v0.3 → R3 终核**双 APPROVE** 冻结（docs 891804a）；plan R1（A CONDITIONAL **P1 单基底签名丢文件层**/B REJECT **P0 C5 init 置位越权语义**）→ v0.2 → R2 **双 APPROVE** 冻结（docs dbbbe44）；SDD Wave1（T1 合并函数+T3 config-manager+T5 文档）→ Wave2（T2 IPC+T4 前端）→ **实施双审双审同抓 P0+P1** 修复闭环 → 复核**双 APPROVE**。
- **实施双审 P0（跨 Task 接缝缺陷）**：T1 Node 侧把 llm-configs.json 写成**扁平** name→entry 映射，T3 Python 侧按 spec §3.1 **{"configs": {...}} 包装**——两端测试各自自洽全绿，跨端零覆盖：Node 写的文件 Python 读=空→逐项 set 触发同步**整体覆写静默销毁全部条目**；Python 写的文件 Node 读→下拉幻影"configs"条目。修复=统一包装层 + **跨端 parity 双向测试**（Python 测试 subprocess 调 node 写真库读回 / Node 读 Python 写真库断顶层键，真执行非 fixture）。**P1**：loadNamedConfig 漏回填 llmMaxTokens→保存时旧值覆写/删除条目 max_tokens（违验收 A6）。
- **验证**：JS 39 绿（config-merge 16/named-configs 11/save-config 5/font 7 不回归）+ pytest 83 绿（named-configs-manager 16 含跨端 parity+畸形守卫+损坏四路径）；T4 browser mock 九场景（A2/A3/A6/A9/A10②/A12/A13 UI 层全过）；grep 六模式清零。
- **关键教训**：
  - **跨 Task 接缝缺陷只有全局审查能抓**——两端各自测试全绿 ≠ 系统正确；SDD 多 Task 并行时，跨 Task 共享的数据格式/文件结构必须在 plan 契约里钉死到字节级（本工程 plan C3 只写了 IPC 信封 {configs}，T1 误当文件格式），且必须配跨端 parity 测试。
  - **大工程按 Wave 提交中间检查点**（用户当面指正"中间过程完全不提交不利于审查"）——全堆收官提交=回退粒度全有或全无+审查 diff 巨大；Wave 级提交让 P0 类接缝缺陷在最小 diff 下更早暴露。本工程主体 5934c8e4 是用户指正后补的检查点。
  - **plan 层级也会越权新增语义**——C5"init 置位 loadedNamedEntry"（自以为的合理补全）被 B 角 P0 抓出：与 spec 两态矛盾+收敛方向反转+引入"打开页面直接保存丢 Agent 字段"路径（恰是立项要修的坑）。**spec 冻结后 plan/实现层零新增语义权**，拿不准回 spec 修订。
  - **"以 X 为主"类拍板必须问清是同步方向还是组装基底**（spec R1-B P0-1 正名：以 user-config.json 为主=合集向它对齐的同步方向，不是内容组装时的基底选择）。
  - **双审独立同抓=缺陷真实性强信号第四次验证**（spec R2 probe 产物归宿；实施双审 P0 文件格式+P1 max_tokens 同抓）。
- **已知边界（接受/记录）**：合集跨进程无锁 lost update 残余（低频写+毫秒窗口，JS tmp 唯一名防截断已修）；probe payload 新增 temperature 字段超 plan T4 字面（方向正确——compat.py L1159 注释明示对齐运行时默认 0.2，记录在案）；set_lightrag(model="",preset_id=X) 矛盾输入清空优先（docstring 已注明）。
- **实机验证清单（待用户）**：①设置页"选择设置"下拉（首次空）②填表单+起名→测试保存→llm-configs.json 出现条目 ③第二套配置→下拉两项→点选切换→测试保存→生效（重启对话用新配置）④主 Agent set_llm_config(preset_id=名)→切换生效；list_llm_configs 列出 ⑤名字空点保存→提醒 ⑥手工往 user-config.json llm 段加 custom_field→页面保存→字段仍在且进合集（坑规避回归）⑦手写坏 llm-configs.json→打开页面正常→保存→提示合集同步失败不关窗+坏文件保留+热更新生效。

#### 工程：slicer 嵌套切割修复——压缩三件套透视归入外层单元（用户拍板；spec v0.2 R1 双 CONDITIONAL（回看透视缺失独立同抓）→复核双 APPROVE + plan v0.2 R1 A CONDITIONAL APPROVE 2P2 吸收+B APPROVE + SDD T1 实施双审双 APPROVE，main cc5a9b3d）

- **背景（压缩修复工程 eeba705a 的 FinalReview B P3-3 引出）**：三件套落库位置可能在**进行中单元中段**（自动压缩工具循环内）——现行 slicer 规则「user 且前条非 user 开新单元」使 [准备]/[完成] user 行开新单元，[完成] 吸收原任务后续全部回复 → 未来归档该单元 first_user 跳过 [系统提示] 后为空串（历史索引"首问"退化）。**用户拍板（2026-09-07）**：压缩三件套是程序机制产物（嵌套在外层用户交付工作的工具循环里），**不应成为单元边界**——应透视归入外层单元；边界约束：不要因用"系统提示"区分而扩大影响面引发大范围 bug，判据**精确匹配两条常量**。
- **修复（回看透视算法，spec §3.1）**：slicer.slice_units 单元开启条件改——非三件套 user 行**向前跳过连续三件套**找最后非三件套 messages[j]，`j<0 or role(j)!="user"` 才开单元（与"删去三件套后应用原规则"严格等价）。判据 `_is_compression_triplet`（精确 startswith 两条常量前缀 `_COMPRESSION_TRIPLET_PREFIXES` 本地副本，保持 slicer 零依赖；parity 契约测试绑定 agent_loop 常量防漂移）。
  - **回看透视必要性（R1 双审独立同抓 P0/P1-1）**：仅判"三件套不开单元"不够——手动场景 `[U_N][准备][总结][完成][新提问]` 中，新提问前条=[完成](user) → 规则②"连续 user 不重切" → 新对话整体粘入旧单元成巨型单元（keep 把整个新会话当 1 轮、归档首问=旧指令、话题混装）。回看透视后新提问有效前条=总结（assistant）→ 正常开新单元。
- **消费点零改动**（A/B 双审实证）：compaction.build_compact_view/archive_excluded_units/integrity._rebuild 三处消费 slice_units 均通用逻辑无需改；context_manager 水位线模型不消费 slice_units（第四消费点不存在）。
- **测试**：回看透视 7 用例（手动 P0 主形态/嵌套/空总结两行/连续压缩两套/首条三件套/负向锁 skipped-at/零回归）+ parity 契约（agent_loop 常量 startswith slicer 前缀，漂移必红）+ 过渡收敛（旧规则块+新切割重叠→integrity 检测→重建收敛）+ first_user 集成断言（新切割×归档组合路径，区别既有 T4 手传 units）。126 passed（含既有 200 轮随机不变式 TestInvariants 零回归自然成立——无 content 形态新判据恒 False）。
- **已知边界（接受）**：①**过渡窗口**（spec §3.4/B P2-1）：旧规则已归档块 + 新切割 → 幂等键不命中 → 重叠块 → 下次启动 integrity._detect_issues 检出 + delete_all 整库重建自愈（无数据丢失，重建前索引重复行；部署后建议重启触发校验）②退化块（DB 以三件套开头 first_user=""）实际不可达（三件套前必有外层单元）③真实用户消息以精确压缩前缀开头会被误透视（startswith 选型接受，B P3-1 验证：落库 content 恒等常量原文、== 反而脆）④合并单元略大（一份总结量级）→ build_compact_view while 预算循环 + 95% 应急终态既有兜底正交。
- **关键教训**：①**R1 双审独立同抓回看透视缺失是缺陷真实性强信号**（A P0/B P1-1 殊途同归——手动场景新提问粘入旧单元）②**"精确匹配常量"判据是防扩大影响面的关键**（用户明确约束：宽泛 [系统提示] 前缀会造成大范围 bug——skipped-at 系列 3 条内存注入不落库、未来新增 [系统提示] 落库形态不受影响）③**parity 契约测试是双副本常量漂移的低成本守卫**（slicer 零依赖本地副本 + agent_loop 常量，照 2026-09-05 实体标签 TestCandidatePoolParity 先例）④**摊还 O(n) 论证**（回看 while：不同非三件套 user 的扫描段互不相交，三件套行至多扫一次——注释防后人误加标记数组）。

### 2026-09-07

#### 工程：压缩修复——总结请求上下文完整 + 压缩产物三件套落库（spec v1.1 R1 双 CONDITIONAL→R2 论据修正→双 APPROVE + plan v0.2 R1 双 CONDITIONAL→复核双 APPROVE + SDD T1-T6 每 Task 双审 + FinalReview A/B 双 CONDITIONAL 修复闭环，main eeba705a）

- **背景（Windows 实机测试 20260907-windows 实证，两缺陷）**：
  - **缺陷 A（000038）**：手动压缩（闲时 `_run_idle_compression`）总结请求 68 条消息**全程无 system**（首条=user 历史索引）——根因：组装视图（`assemble_view_sync`）= [索引]+[候选] 不含 system（system 由 runner 每轮 `_on_before_llm` 拼），手动路径把组装视图直接喂 `run_controlled_compression` → 总结模型无角色/时间/记忆/技能注入，承上启下总结质量崩。自动路径（发送前门）messages[0] 已是完整 system，无此问题。
  - **缺陷 B（000057）**：压缩完成提示 `[系统提示] 上下文压缩已完成。` **纯内存不落库**（spec 原拍板"纯内存注入"）——手动路径重组消息直接丢弃，提示从未发给任何模型轮、从未进 DB → 模型根本不知道"已压缩、上面有总结"，压缩后会话无法按用户拍板的承上启下语义衔接。
- **用户拍板（压缩后指示）**：压缩完成后把 [做准备提问]+[模型回答]+[压缩完成] **三件套一并落库**，落库序尾 = 压缩完成 → 自动路径下一轮组装最后一句 = 压缩完成、原指令在其后仍最后；手动路径下次用户提问其前最后一句 = 压缩完成。手动无下一轮 → 必须"提前把下一轮要说的这句话落库"。
- **修复**：
  - **T1（compat._run_idle_compression）**：前置 system 占位 → `runner._on_before_llm(history, turn=0)`（turn 必须 0——turn==1 消费清空 `_first_turn_extra_injection`，R2-A P2-2）；降级覆写单条 `base_system_prompt`（恒单条 system，双行违 OpenAI 兼容→400）；空 history 守卫。
  - **T2+T3（agent_loop）**：run_controlled_compression 时序重排（总结 LLM 仅内存 → 压实 → 三件套落库（`_triplet_messages` 共享构造，skip_mirror+bypass_at_extract，空总结跳 assistant 行）→ 重组 **Approach A**——`_last_compacted_view` 基底保占位符化、删 if summary_text 块、剥离待执行单元后追加三件套+尾引导+单元置末）；`_persist_summary_without_extract` 薄包装保留（import 点存活）；did=True 压实成功固定不翻转 + 重组异常 try/except 降级；done 两路径必推。
  - **T2T3 双审修复闭环（A/B 独立同抓 P1）**：降级分支剥 system 行 → 门后重跑 on_before_llm 遇 messages[0] 非 system 静默空转（`_assemble_system_message` 早退）→ 该次及同 run 后续轮全程无 system（000038 同因）→ 修复为保留 system 行（on_before_llm 只原地刷新 content、从不插新行，无双 system 风险）。
  - **T4（compaction.archive_excluded_units）**：first_user 提取跳过 `[系统提示]` 前缀 user（spec P3-4）——三件套落库后 slicer 切 [准备+总结]/[完成+续] 两单元，归档索引"首问"须跳过防污染；幂等性保持（已归档块 first_user 永不回改）。
  - **FinalReview A/B 修复闭环**：
    - B P2-1 **闩锁所有权透传**（B-P3-1 idle 漏网）：chat_session idle 分支 `try_acquire` 返回值曾丢弃、`_run_idle_compression` 硬编码 release_on_failure=True → 滞回闩锁期失败误清他轮闩锁 → 修复 acquired 透传 release_on_failure（未持锁则 False，与门内 manual 重试闩分支同构）。
    - A P2-1/P2-2 测试缺失补锁：重组基底负向锁（spy 禁 DB 重跑）+ if summary_text 块已删源码断言；B P2-2 条件解闩语义 2 条行为锁（release_on_failure=False 不解闩 + idle 已闩场景闩锁保留）。
    - B P3-1 注释漂移（`_COMPRESSION_DONE_HINT` 头注释"纯内存不落库"→"落库+内存追加双通道"）+ test_compression_compact.py 模块 docstring 同步。
  - **T6（SYSTEM_MANUAL）**：三处压缩产物形态更新（L28/L38/L479——"纯内存注入"表述失真 → 三件套落库语义）。
- **验证**：89 passed（compression 相关 11 文件全点名 + ruff 零新增——ruff 报错全部预存，stash 对比实证）+ py_compile；T5 MethodType 绑真实 `_on_before_llm` 策略直测 system 补全（策略 a）；变异检查证明新测试是真实行为锁（无 T1 补全则断言必失败）。
- **已知边界（接受/记录）**：P3-2/P3-3（spec）：turn==1 达线丢 resources 首轮注入、下轮检索上下文稀释一轮（非本工程引入）；B P3-2（剥离兜底注释不符+理论双份引导，生产几乎不可达）；B P3-3（三件套落库切入进行中单元中段致归档首问退化+内存重组序与 DB 序不一致可见重排——非数据损坏，记录为归档策略再议项）；B P3-4（压缩后 usage 圆环不刷新——`notify_compact_status_sync` 的 usage/reset_tokens 参数统一压缩路径未使用，SYSTEM_MANUAL L28 承诺无代码支撑，属统一入口工程遗留非本 diff 回归）；B P3-5（进度通知 mode 硬编码 auto）；B P3-6（full_flow mock 重组）；B P3-7（前端三件套 user 行按普通气泡渲染——spec P3-4a 已接受项）。
- **关键教训**：
  - **spec/plan 双审抓不出用户真实意图层面的设计错误**——上一轮统一压缩入口工程 spec 拍板"纯内存注入不落库"本身是错的（用户从没拍板过，是我设计带偏），审查 Agent 逐轮核对"实现 vs plan 对齐"都对，但没人验证"压缩完成提示模型必须看到"这个用户意图。本轮 spec 把"三件套落库"作为用户直接拍板的新语义写进设计，才堵住。**审查必须验证"用户拍板意图"而非仅"实现 vs plan"**。
  - **双审独立同发现是缺陷真实性的强信号**（降级分支 system 剥离：A 定 P2/B 定 P1；测试迁移遗漏：plan 双审同抓）——不同 Agent 独立抓到同一问题，基本可断定为真缺陷。
  - **重组基底选择（Approach A）**：用户关切"下一轮组装是否含三件套"由 DB 落库结构性保证（水位线后恒候选），本轮可见性由内存追加保证——双通道独立；`_last_compacted_view` 基底保留占位符化成果（内存操作不落库，DB 重建会复活 tool 原文），禁 assemble_view_sync/build_compact_view 重跑（exclude_last 剥完成行/二次归档/丢占位符化）。
  - **三件套落库时机**：压实**后**（三件套 rowid 最新 > 所有 blocks 覆盖 → 恒在候选窗口、不被本次压实归档）；压实**前**落总结会被压实当普通窗口内容处理。

### 2026-09-05

- 工程：get_messages 便利遍历裁剪 + 单条全量 + 服务端自管输出预算（027 事故根治——journal 水位错乱；spec v1.0-v1.4 R1-R5 五轮双审门禁（连续双 APPROVE 收敛）+ SDD T1-T2 每 Task 双审 + FinalReview 双 APPROVE，main d78e90db/3ebe8c72/61ae844）
  - **事故链（Windows raw_http 027 实证 + 用户逐层逼出根因）**：journal-agent 调 get_messages(after_time, limit=200) 过滤窗口 193 条消息序列化 **97275 字符** → agent_loop 工具结果通道 30000 截断 **保头丢尾**——09-05 全天 91 条（数组尾=最新消息）被静默砍掉 → 落款倒退 09-04 19:40:47、09-05 内容整段漏。根因两层：①便利遍历给全量 content（193 条整批 97KB）②服务端不知道输出会被通道砍（has_more=false 但数据实际不完整）
  - **方案（用户拍板三要素 + 落款修正前置）**：①便利遍历每条 content 折叠 前 1200 + `<已折叠>` + 后 800（原正文 ≤2000 字符、总长 2005，全 role；tool 先经 `<已精简>` 字节级折叠再裁剪——CJK 显示 <已精简>/ASCII 长正文显示 <已折叠>）②完整内容 = message_id 单查（超通道线 → error+reason=too_large 含 content_chars，绝不静默截断）③服务端自管输出预算（Read 模式：生产单形态 `len(json.dumps(result, ensure_ascii=False))` 自查 ≤29000，超预算尾部收缩 + has_more=true/next_after_id/output_budget_truncated/remaining_in_batch 显式告知续拉，base_index 页首不变严禁按收缩后条数重算——根治"服务端 has_more=false 但数据被砍"模式）；④落款语义修正（d78e90db 前置）：覆盖至=当前整理时间（用户指正 PM 误写"最后消息时间"，journal-daily 避让机制下无漏窗口）
  - **设计关键（五轮双审抓出）**：预算估算必须复刻生产链路单形态（stdio wrapper 双序列化仅外部模式存在——session-manager 同进程直调）；full_tool_output 参数退役（7 处清零，能力被统一裁剪覆盖）；裁剪产物 2005 超帽惯例（md_mirror 同族）；read 工具是正确范式（工具自己管理输出+显式续读告知）
  - **质量链**：R1 双 CONDITIONAL（outer 口径 P1 系列/单查体积/归因修正）→ v1.2 → R2（A 抓出 outer 公式复刻非生产链路 P1/B APPROVE）→ R3（必挂清单 2/5 实测误判/base_index 页首公式陷阱）→ R4（A APPROVE/B 抓 T1 行残留旧措辞+AC6 缺钉）→ **R5 双 APPROVE 冻结**；T1 双审双 APPROVE（48 smoke+5 挂恰为预测必挂项）；T2 双审（两角独立同发现 AC6 pin 缺失 P1 → 修复补双 md pin/schema 深比较/tool 单查用例，63 passed）
  - **验证**：63 passed（36 get_messages + 27 journal pin）+ ruff 零新增；判别力实证（HEAD 版重放 15 failed 恰为全部新行为测试）；e2e 排空不重不漏（两两不相交+union==窗口+末页 has_more=false+每页 ≤29000+关口恒等）
  - **已知边界（接受）**：打包副本 niu.app 需 launcher/build.sh 刷新才生效（旧 schema 无 message_id）；too_large 单查超 ~29.5K 字符消息无法完整返回（显式报错，agent 改 read 直读）；journal 弱模型对含折叠标记条目的单查触发靠提示词教学（机制教学在场、判定留 LLM，实机观察不佳再补触发句）
  - **实机验证（待用户）**：重新打包/源码运行后——journal 整理（Windows 机器下次 18:00 或手动）落款应为当前时间、09-05 内容不再漏、索引正常

- 工程：索引实体标签语义化——时间链候选池 + 首问向量排序（用户指出同日块标签全雷同→质问为何不语义检索→拍板先测试拿数据；实测 100%→15% 后定案；spec v1.0-v1.2 R1-R3 双审门禁 + SDD T1-T2 每 Task 双审 + FinalReview 双 APPROVE，main 6c6fbc08/aa79f138）
  - **问题（用户 Windows 实机抓出 + Mac 本机复现）**：归档历史索引行实体标签现行纯时间链查表（entity_tags.py 按块时间范围找 `YYYY-MM-DD会话` 节点一跳邻居按边权重 top3）——数据源粒度是天（entity-extractor 按天挂会话节点），同日块全部反查同节点 → 相邻同日块标签 100% 雷同（Mac 实测 75/75 对），区分度零+语义误导
  - **方案（零 LLM 保留，用户定案）**：时间链管候选范围 + 语义管池内挑选——`collect_tags(time_ranges, first_users=None)` 双参（None=纯时间链逐字节保持既有）；`_candidate_pool` 池构建（tags_for_range 零改动防测试面破坏）+ 首问 `batch_encode` + `call_async(_get_vdb_snapshot, timeout=5)` 协程内建 name→vector 映射（零 await 与写方串行原子）+ 池内 dot 相似度降序/名称升序 tie-break top3 + 时间链权重序剔除已选补齐；降级链 12 行全钉（语义段任何异常→全批时间链；空标签仅图快照 None/collect_entities=False）；vdb 向量 = entity_name+description embedding（float16 解码），实体矩阵 L2 归一化
  - **实测（入仓重放载体 docs/superpowers/experiments/entity_tag_baseline.py）**：AC3 雷同 基线 100%→语义 **15%≤30%**（残留=内容真实相近合理；饮食块→醋溜白菜配五花肉/rerank 块→rerank 配置任务等强相关）；AC5 warm batch_encode 80 首问 2.73s（i7-8850H CPU，常见归档批量远小成本线性）——R1 估算 0.5s 失准按实测重定 ≤3.5s
  - **质量链**：spec R1 双审 CONDITIONAL（A 4P2+5P3/B 5P2+3P3——签名默认值收敛测试面/call_async 超时 120s 未钉/is_ready TOCTOU/降级 catch-all 落点冲突/vdb 快照原子性/补齐去重/实验脚本口径差）→ v1.1 → R2（A CONDITIONAL 载体 P2/B APPROVE）→ 脚本按终态口径固化 + v1.2 → R3 双 APPROVE 冻结；T1 双审双 APPROVE（30 既有零断言改动= None 等价性自证 + QualityB 18/18 探针）；T2 双审（SpecA APPROVE + QualityB CONDITIONAL 1×P2 timeout=5 无行为锁 → 补断言收敛；46→49 passed）；FinalReview 双 APPROVE（P3：parity 契约测试已补——candidate_pool top3 与 tags_for_range 绑定防双副本静默分叉）
  - **已知边界（接受）**：归档路径 encode ≈2.7s/80 块（压实所在线程响应延迟，非事件循环冻结——FinalReview 披露口径修正）；tags_for_range/_candidate_pool 双副本靠 parity 测试绑定；存量块旧标签保留至块重建；换模型重启 dim assert→空标签（安全）

- 优化：折叠占位符去编号——`[输出#{rowid} 已折叠：...]` → `[已折叠：{name}({args})，原占约 X%。]`（用户分析：编号对 LLM 零功能纯诱饵——取回靠参数摘要重调原工具、归档取回靠 block_id 非 rowid、幂等靠 DB folded 标志，唯一"用途"是给弱模型重复折叠当目标把手，fold 必须传 output_ids 无编号物理不可达；头行 `[输出#N · 工具名 · 占比]` 保留编号不动）；_is_tool_placeholder 折叠族判定「单行 + [输出#」→「单行 + [已折叠：」双锚（旧带编号恢复会话经第二锚照认，最早「获取]」版经裁剪族尾缀通道照认——三代文案全兼容）；95 passed（StickyT3）
- 优化：fold 折叠占位符文案瘦身——删教学重复句（用户指出占位符尾部「如需原文请重新调用原工具获取」是 niu.md 已教内容的逐条重复征税 +「本条已移出上下文」半冗余）：新文案 `[输出#{rowid} 已折叠：{name}({args})，原占约 X%。]`（pct None 省略占比分句）——旧 45 字→新 22 字省一半，编号/工具名/参数摘要（重调原工具通道信息）零损失；配套 `_is_tool_placeholder` 折叠族判定从「获取]」尾缀锚（当初搭裁剪族判定便车）改为「单行 + [输出# 前缀」——更稳（未折叠渲染恒多行不误判），新文案与旧格式恢复会话均覆盖；点名 6 文件 95 passed（StickyT3）

#### 工程：LLM 会话亲和路由（sticky routing）——服务商 session id 动态注入（spec v1.5 R1-R6 双审门禁 + SDD T1-T3 每 Task 双审，main 7ac00ed2/59154330/5035dedd）

- **背景（OpenCode Go 09/06 起 x-opencode-session 强制邮件触发）**：调研实证 sticky 亲和路由机制（OpenCode Go 网关 createStickyTracker + header/部分模型强制 DeepSeek Flash 缺头 400；OpenRouter 官方 Provider Sticky Routing——session_id body 字段或 x-session-id header、10 分钟过期、agentic 场景 hash 回退不可靠须显式传）——**per-conversation 稳定 id → 同会话路由同槽位 → KV cache 命中；写死同值=反模式（负载倾斜+缓存互相驱逐）**
- **对原拍板的修正（调查推翻前提）**：Niu 单会话架构无对话 session_id（agent/session.py "No session concept"/compat 硬编码 "default"）→ 主 Agent 也用固定 id；per-conversation 动态 id 需贯穿 4 层调用链而 /new 后旧前缀缓存必然被逐出——KV cache 角度零额外收益，实现极简化
- **T1 注入机制（7ac00ed2）**：resolve_sticky_headers 模块级纯函数（lowercase+hostname 点边界匹配 h==d or endswith('.'+d)——evilopenrouter.ai 反例/scheme-less 补 https:///三态 sticky_session_headers auto|off|列表替换/非法值=off/anthropic 排除优先于列表态）+ LiteLLMSession sticky_session_id 构造键 + chat() 注入（真值守卫防 None 头/合并顺序 {**user, **headers} 程序值权威/每请求读 api_base）+ 控制键两处剔除（chat 展开 L1019 + model_probe _build_probe_params）+ create_client/create_litellm_client 白名单透传行（spec R2 抓出：固定键白名单会静默丢键）。双审修补：AC7 anthropic-beta 共存单测/尾点 FQDN rstrip('.')
- **T2 四通道接线（59154330）**：主 Agent "main"（NiuRunner.__init__ llm_config 加键）/子 Agent 同步=agent_name 异步=unique_name **无条件覆盖**继承的 main（判据与 _is_async 互斥；续答复用 suspended_client id 烘焙零接线）/LightRAG+脑区 "lightrag"（_get_litellm_session 内部无条件，id 不进 config_key）/MCP Sampling "mcp-sampling"（存量 bug：sampling_callback 传参与 call_llm_via_litellm 签名不匹配恒 TypeError，e2e 阻断——验收收窄单测级，bug 另案）
- **T3 测试+手册（5035dedd）**：37 passed（纯函数全表/mock 集成/接线断言/resume 身份断言+fresh 诱饵/脑区 label 覆盖与共享缓存构造次数判别）；手册 extra_headers 节改写（零配置域名表自动/静态头程序值权威/三态覆盖键/探测与 anthropic 不注入）
- **验证**：sticky 37 passed + 点名回归 114 passed + ruff 零新增；出网收敛点穷举（litellm.completion 直发仅 adapter chat 与 model_probe 两处）与 LiteLLMSession 构造点 5 处全部归位
- **实机探针 ✅ 已实证（2026-09-05，omp 同源 opencode-go 账号直发官方端点）**：交叉头无害性 200 全过（x-opencode-session+x-session-id 同发被接受）/Niu id 形态 'main'/'lightrag'（非 UUID）接受/域名表自证 opencode.ai（zen/go/v1）与常量一致/**sticky 端到端生效：大前缀 889 tokens 同 id 第 2 发 cached_tokens=832、异 id 对照 0**；UA 注意：Cloudflare 1010 拦 Python-urllib 默认 UA（同 Niu list_models 经验）；OpenRouter 侧待接入时顺带验证
- **设计修订（用户指正歧义，5460a439）**：auto 态域名分家——各家只发自家头键（openrouter.ai→x-session-id；opencode.ai→x-opencode-session），通用功能不绑死品牌键名；新服务商扩展 = _STICKY_DOMAIN_HEADERS 常量加一条 + 配它认的键名；反代仍走 sticky_session_headers 列表态
- **已知边界（接受）**：主 Agent "main" 跨 /new 共享（新对话首请求 cache miss 属预期）/脑区 label 走主配置时带 "lightrag" id（OpenRouter 同槽位混模型流量，无正确性影响）/OpenRouter Logs 固定 id 分组单条/OpenCode Go 反代域名需 sticky_session_headers 列表态兜底

### 2026-09-04

#### 工程：journal 游标改造——uuid 机器行退役 → 落款时间水位（计划 v1.4 R4+R5 双审门禁 + SDD T1-T4）

- **设计（用户拍板）**：旧「覆盖至」+uuid 机器行整链退役 → 整理条目末尾落款「覆盖至 YYYY-MM-DD HH:MM:SS」（空格分隔、「覆盖至」后无冒号，=本批最后消息 created_at 的 T 替换空格截断秒）即水位；下次整理读最近落款自判起点——程序零游标状态机，交互/夜间双 Agent 共享 journal.md 水位天然一致
- **T1 session-manager get_messages 新参数 after_time**：created_at 严格大于过滤（ISO 字符串比较=时间序），服务端 replace(' ','T') 分隔符归一后比较；与 after_id 可共存（分页第二页起两者都传）；分页基于过滤后序列（首页取最旧 n 条，防增量静默丢）；TOOL_SCHEMAS/MCPTool/dispatch/函数体四处同步
- **T2 提示词改写**：journal-agent/journal-daily-agent——无落款=首次整理 limit=200；首页起点为时间不会有 invalid_after_id（删旧因果句「→按首次整理兜底」）；分页中途遇 invalid_after_id（如 /new 并发清库）归类 transient → 本轮放弃不写条目不落款、下轮自然重试；/new 清库免疫（时间是值，新消息总更大）
- **T3 存量迁移**：journal.md 零 uuid 行，最近落款「覆盖至 2026-09-04 11:00:00」
- **T4 测试/文档同步**：协议钉补语义钉（落款格式/after_time/next_after_id/旧句 not-in-md）；unified_paths JOURNAL_PATH 错位修复（~/.niu/journal.md → memory.json workspace.path 解析，计数改落款正则）；SYSTEM_MANUAL journal-daily 段改写

### 2026-09-03

#### 新增：异步子 Agent 完成态存档续跑——工作结束后落盘，同名再次调用自动加载上轮上下文续跑（spec v0.1-v0.6 六轮双审 + 计划 v1.1-v1.2 双审门禁 + SDD T1-T4 PM 复核 + 收官 FinalReview 双审补位，main 2cc58291/66766307/66a88d03/6c3cc193）

- **需求（用户拍板）**：异步子 Agent 完工后上下文落盘 `~/.niu/tmp/<unique_name>.json`；主 Agent 用上次唯一名再次调用（async_mode）→ 程序查 tmp 同名档，有则加载组装跑、无则全新上下文。**D1** 存档 tmp 顶层（接受 24h//clear 清理语义）；**D2 同名即续跑不加参数**（主 Agent 有意调回已完工子 Agent=续跑意图，名字即意图载体）；**D3 仅异步**（unique_name=agent_type+hex 唯一；同步 force=agent_name 无唯一性不可二次调用）；**被叫停/中断态也落盘**（"结束早了补未完成"主场景）
- **机制**：①**T1 存档**——call_subagent 异步分支终态汇聚点（_run_agent_loop 返回后、early return 前，时序=写盘先于完成通知 push）：messages 门控（dict 非空才存；LLM_ERROR 无 messages/异常不达 → warning 跳过）、仅 is_sync=False、末轮补全三类终态（EXITED/TERMINATED_BY_SUPPLEMENT/CURRENT_TASK_DONE——纯文本轮产出从不入 messages，append last_reply 守卫非空非中断标记）、副本 append 不污染、原子写 mkstemp+chmod600+os.replace、registry `archive_written` 标志（None/True/False 通知话术门控）②**T2 续跑**——chat-with unique_name 透传 → 占名先于查档（单仲裁防并行双起跑）→ 运行中 ValueError 自定义文案 / 命中 `_prepare_resume_messages`（复用 transform_history 悬空 tool_calls 剥离——STOPPED mid-dispatch 档零配对 assistant 续跑必 400，剥离在加载侧档保持原样；+ system 头还原 + 尾部空 assistant 连剥 + 元素 dict 校验）→ effective_task=task or "继续上次未完成的工作"（空 task 撞 call_subagent L894 入口闸门）+ append user → resumed_messages 中继链（_run_subagent_async 签名/worker/call_subagent）→ 续跑完成再存档覆盖同名档（24h mtime 重置）③**T3 测试** 19 条行为锁 + 话术 cutover（test_incomplete_cursor 迁移 + overflow 3 锁）④**T4 教学**：niu.md 一句话 + schema description（限定 async_mode）+ 通知话术三处条件化（incomplete/overflow 未完成提示按 archive_written；同步删"已保留进度"）+ SYSTEM_MANUAL
- **质量链**：spec 六轮双审（R1 承诺域裂缝→用户拍板 @end+被叫停 / R2 messages 门控+时序不变式 / R3 末轮补全扩 CURRENT_TASK_DONE 双审同发现 / R4 空 task 撞入口闸门→effective_task / R5-R8 写失败抑制锚点闭环）+ 计划双轮（R1 中继透传遗漏 / R2 悬空剥离 spec 级回修）+ SDD T1-T4 PM diff 复核 + 收官 FinalReview（A APPROVE 1×P3 元素校验 / B CONDITIONAL P2 AGENTS 条目 + 2×P3 已修/披露）——**无每 Task 双审如实记录**
- **验证**：T3 19 + T4 cutover 25+2 + 涉改面 134 passed 全绿；fake HOME 零 ~/.niu 写实证
- **已知边界（接受）**：①@end 报告 >2000 字符档尾为 tmp 指针文本（悬空，同享 24h 清理）②Windows chmod 600 不实际生效（与 tmp 既有内容同暴露面）③档内旧 system 跨模型家族格式失配风险未修（24h 窗 + 换模型族概率低；同步挂起同机制窗口更短）④续跑仅 async_mode=true 生效（schema 已限定）
- **实机移交清单（待用户重启）**：①异步派发 file-processor → @end → 检查 ~/.niu/tmp/<名>.json 落盘（含末轮总结）②主 Agent 同名 async_mode 再调 → 确认文本"已加载上轮上下文续跑" + 子 Agent 记得上轮工作 ③被叫停 @名 /stop → 落盘 → 同名续跑成功 ④无档同名 → "[续跑回退]…已全新派发" + 实际名 ⑤24h 后档被 cleanup 清（续跑窗口过期语义）

#### 修复：续跑异步子 Agent 无标签页——createSubagentTab 活跃残留静默 no-op 改重置运行中（用户实测 5s 间隔失败/23min 成功 + 全链双审，main f353c6b7/482678ba）
- **根因**：子 Agent 完成后 subagent_closed 事件偶发丢失（per-name 连接瞬断/迟到）→ 旧 tab 停留活跃态（无 completed/error、不在 _pendingCloseTabs）→ 续跑 subagent_started 到达时 createSubagentTab `if (!isReusable) return` 静默放弃——tab 从未出现。全新派发无残留故正常。用户实测关键：5s 间隔（>3s 延迟关窗）仍失败 → 非"等 3s"时序而是 closed 丢失；23min 间隔成功=主路径无碍；用户在 tab 场景走 _pendingCloseTabs（等切走）本就复用正常——修复覆盖 closed 从未送达的残留重启（超集安全网）
- **修复**：活跃残留分支不再静默 return → 重置为运行中（remove completed/error/waiting + dataset.sync 对齐 + loading + 空占位刷新；不切 tab 不清容器防真重复清内容）
- **全链双审**（用户要求扩大范围证明无新 bug）：A 保留（P2 dataset.sync 遗漏）+ B 保留（3 P3）——双审独立同发现 dataset.sync/waiting 未同步（482678ba 修）+ 注释收窄（同步/程序触发 dup started 可达仅短暂视觉）+ 切走 500ms 匿名 timer 竞态有 _closeSubagentTab 守卫兜底
- **残余边界（披露）**：closed+started 双丢（主 SSE 断连窗口）纯前端无解——需后端 started 写入 per-name ring buffer 或主 SSE 补发；遇"重试仍无 tab"走此方案
- **验证**：node --check 通过；用户重启实测通过（2026-09-03 用户确认关闭工程）

#### 修复：@ 指令与工具调用同轮静默跳过——工具优先执行 + 跳过提示注入（方案 v1.0-v1.2 三轮双审 + SDD T1-T3，main b8fda761/49331860）
- **根因（raw_http 20260903/000043 实证）**：营养师子 Agent content=@niu-agent 提问全文 + 同轮 tool_calls=[grep] → 拦截层守卫 `if not response.tool_calls:`（agent_loop.py L1020）使整个 @ 拦截块不进入——提问被当旁白丢弃；子 Agent 以为问过了（000044 继续干活/000045 @end 收尾），缺两天记录被静默绕过。**提问丢失不可见**——LLM 动作确认记忆缺口（折叠工程同族教训）
- **方案（用户拍板 D1-D4）**：工具优先执行（@end+工具同轮="干完这票再收工"，先拦截=丢工作——用户纠正我原方案）+ next_prompts 注入跳过提示（截断免疫，不塞 tool 结果尾防 30000 截断连提示一起丢）+ 每轮单次幂等 + @end 不自动 EXIT（模型下轮自决）
- **机制**：模块级纯函数 `_detect_skipped_at_directive`（@end→@niu-agent→@user 优先级；@user 词边界复刻拦截层 L249 防 @username 误判）+ 接线在 next_prompts=set() reset 之后（**R1 双审 P0：锚在拦截区会被 L1229 重置吞掉/首轮 UnboundLocalError**）；文案要求完整内容重发（裸 @niu-agent 命中空问题守卫 FORMAT_ERROR；裸 @end 丢最终汇报——R1 B P2）
- **关键教训**：①用户语义直觉比我的机制方案更贴模型意图（@end+工具=工具完结束，不是先结束）②next_prompts 注入必须锚 reset 之后（L1229）——锚拦截区直接崩溃/静默失效 ③文案必须要求完整重发防裸指令（空问题守卫/exit_content 空回退）
- **验证**：46 passed（34 既有回归 + 9 纯函数 + 3 loop 级送达/负向）；messages.db 零新增
- **已知边界**：@end 习惯性同轮弱模型可能永不分离——既有 max_turns 兜底（接受不加强制 EXIT）；工具轮中途 STOPPED 时提示随 next_prompts 丢弃（与既有语义一致）

#### 优化：内容提炼子 Agent 提示词——入库机制认知 + 价值判断链 + 查重前置 + 职能边界（方案 v1.0-v1.2 三轮双审 + SDD T1-T3，main fe7d4ac4）
- **背景（用户怀疑内容提炼必要性，深度分析实证）**：entity-extractor（162 行）只教"筛选→提炼→入库"动作清单+枚举式禁止，缺机制认知 → 机械重复提炼：定时提醒消息每天 11:00 同一条被重复提炼 8+ 份平行文档（措辞漂移躲过内容 MD5 去重）+ 图谱平行实体 6+（咖啡机提醒定时任务/咖啡机定时提醒/…）；对照 dream-evolver（628 行）教完整写入→检索机制 → 行为克制先查重建
- **图谱侧归因（用户拍板不动）**：李磊描述 750 字符多段变体膨胀 = LightRAG fork merge 机制本身（operate.py already_description+sorted_descriptions 跨文档无条件 append、去重仅限单文档内、段数<8/token<1200 不压缩直接 <SEP> join）——到量自动触发 LLM 压缩清理，用户拍板图谱侧不调整
- **方案（用户拍板 D1-D5，纯提示词工程）**：D1 只改 entity-extractor.md 不动代码/机制；D2 入库机制认知（教 lightrag_insert 后果：文档永久入库+LLM 抽实体/同名 append 描述/异名=永久碎片/未来经自动注入被想起）；D3 价值判断链（新事实?→查→命中评估更新/跳过→未命中才入库；例行/程序消息自判跳过不枚举——枚举追不上新类型）；D4 查重前置（search_entities 必做）；D5 职能边界（entity 只做语义综合文档；纠错/建链/画像/精简归 dream——纯纠错消息不入库留给 dream B1，防重复 LLM 处理同一知识）
- **机制强化**：frontmatter 加 mcpToolFilter 白名单 6 工具（insert+读面），机械封死 edit/delete/merge（dream 同款 block 格式——R2 双审抓出单行嵌套 YAML ScannerError 先例）
- **质量链**：方案 R1 双审（A 机制事实核验全准+纠错 vs edit 不可执行指令 P2；B 五场景模拟 CONDITIONAL 2P2）→ v1.1 → R2（A 抓 mcpToolFilter 单行 YAML P1 + 不传 doc_id 保留 P3；B 模拟 APPROVE）→ v1.2 → R3 双 APPROVE 门禁 → T1 重写（+54/-6）+spec 对齐审查 APPROVE → T2 零背景 scout 模拟 entity 五场景+参数变更变体 8 标准全过
- **已知边界（接受）**：③步"更新"无单文档覆盖语义（LightRAG insert 是 append）——合法更新每次新增文档、高频更新主题仍缓慢累积，靠高门槛+保守 tie-breaker 压制；纯纠错 vs 偏好变更可提炼边界依赖模型判断（裸否定→dream/否定+新肯定→更新）
- **实机验证（待观察）**：下次睡眠 entity 运行时——定时提醒/门锁例行消息零入库；新偏好/计划正常入库一次；纠错消息不在 entity 产物中（留给 dream）；graphml 平行实体不再新增

### 2026-09-02

#### 工程：主 Agent 主动折叠工具输出——两列 + 头行/占位符 + fold_tool_output 软防线（spec 双审 → 计划 R1-R3 三轮双审冻结 → SDD T1-T6 每 Task 双审，main 75e4b739..6f0652a0 共 6 commits）

- **背景**：窗口内工具输出（文件内容/检索结果等）可再生（重调原工具即取回）却长期占上下文，此前只有 80% 硬压实能回收——加主 Agent 可自主触发的主动折叠软防线；DB 真相源 content 永不动，folded 是元数据标志列
- **机制三件套**：①存储=messages.db 两列 folded/output_pct（PRAGMA+ALTER 幂等迁移，失败置 `_fold_columns_available=False` 降级：无头行/无占位符/fold 工具返回明确错误文案不终止启动）②渲染=窗口 tool 消息加固化头行 `[输出#N · 工具名 · 占上下文 X%]`，folded=1 → 单行占位符以「获取]」收尾（兼容 _is_tool_placeholder 应急裁剪识别）；render_tool_content 共享 helper 常规组装与压实视图两路径同制式（R1 交叉 P1：渲染不下沉则压实当轮 folded 内容全文复活破缓存）③取回=重新调用原工具（不归档指针块库——块库是压实批次粒度）；fold_tool_output=session-manager 静态工具（7 处触点）
- **占比口径（用户拍板）**：落库时刻 calibration.estimate(本地计数) ÷ 总窗口 contextWindowSize 算一次永久固化永不重算——分母恒为总窗口非当前用量（重算=窗口区内容漂移破前缀缓存）；None（旧数据/估算失败）渲染省略占比分句
- **编号=rowid**：messages.db rowid（主键 id 是 TEXT uuid 不可用；messages 只增不删故 rowid 稳定）
- **幂等语义**：已折叠进 notes 不报错；全幂等 status:ok + folded:[]；部分成功 ok + errors + 「N 条未成功」
- **搭车纪律**：fold 必须捎在本来就要调用的工具同轮，绝不只为折叠单开一轮（全量上下文重发比省的更贵）——写入 niu.md 教学与工具 description
- **仪表盘+触发线配置化**：动态块 Current Time 前注入 `[上下文使用率 X% · 强制压缩线 Y% · 可折叠输出 N 条（合计 Z%)]`；新配置 context.compactionTriggerRatio 默认 0.80 clamp [0.50,0.94]（严格 < warningThreshold 追加提前窗口倒置警告），HARD_BUDGET=min(0.80,trigger) / RESET=trigger−0.02 / EMERGENCY 0.95 写死不动
- **教学**：niu.md 新节「并行工具调用与上下文折叠」（并行调用=历史欠账：agent_loop 机制层原生支持一轮多 tool_calls，niu.md 从未教过——dream-evolver 会并行因其提示词明确教过）+ SYSTEM_MANUAL 同步
- **质量链亮点**：R1-A P1 TOOL_SCHEMAS 计数硬断言 5→6（不列则回归必红）；T3 质量审 P2 fold_tool_output 的 **kwargs 注入通道非测试环境清空（生产 kwargs 只能来自 LLM 幻觉，DB 写路径不可被重定向；pytest 在场判定保留 tmp 注入）；收官 T2 P3 修 _is_tool_placeholder 加单行条件（头行使 startswith("[") 恒真，原文以「获取]」收尾的未折叠消息会被误判为占位符跳过应急裁剪——占位符形态恒单行，commit 629233b9）
- **验证**：104 点名回归全绿（9 skip=e2e 门 --run-e2e 未传，预存量）+ py_compile 六文件 + ruff 零新增
- **已知边界（接受）**：①压实轮统计高估一轮——组装出口 _fold_stats 压实轮沿用压实前缓存（仅指导语义，下轮自愈，R2-A P3）②kwargs 门控依赖 pytest 在场判定（sys.modules 含 "pytest"）

#### 修复：折叠视图刷新——DB 侧折叠同工具循环内不可见（深审全链 + 修复计划 R1-R5 双审连续双 APPROVE 门禁 + SDD T1-T3 双审，main 2be8c0d5/4ebbc644/47490513 + niu.md 教学去重 4e95f8bf）

- **bug（用户实机抓出，raw_http 实证）**：fold_tool_output 折叠只 UPDATE DB，而 LLM 视图只在入口组装一次、同一工具循环内纯内存累积 append——DB 侧状态变更对同循环不可见：下轮 LLM 仍见折叠前原文与旧使用率（raw_http 实证 76.9% vs 下入口组装后 62.3%）
- **修复三件套**：①context_manager 抽 `assemble_view_sync` 纯组装（入口/折叠刷新共用单一渲染源；**不含压实尾段**——rebuild 不得把刚折叠的目标行归档移出窗口；校准 usage 覆写移入）②agent_loop 抽模块级纯函数 `transform_history`（入口 history 变换全段：subagent_msg/空丢弃/孤儿校验/valid_tcs 剥离/30000 截断——rebuild 与入口逐字节同制式，否则悬空 tool_calls 注入会 400；去截断则回全量）+ fold 检测 hook（persist 循环后、next_prompts==0 前，每轮初始化）③runner._on_fold_applied 从 DB 重拉 → assemble_view_sync + transform_history → `messages[:]` 原地替换（贴压实回调先例——agent_loop yield persist 同步漏斗保证 yield 即落库，rebuild 从 DB 重拉必完整）
- **关键教训**：视图只在入口组装一次 + 工具循环内存累积 = 任何 DB 侧状态变更（折叠/未来同类 MCP 工具）都不会被同循环感知——**变更 DB 即需原地回写视图**；rebuild 必须走入口同一变换源（role 过滤 + 悬空剥离 + 截断），否则注入非法 role/悬空 tool_calls 破 API
- **质量链**：深审全链（P1 无压实 / P1 Message 契约 / P2 hook 挂载）+ 修复计划 R1-R5 双审（连续双 APPROVE 门禁）+ SDD T1-T3 双审 + 收官 READY；tests/test_fold_view_refresh.py 新增约 640 行
- **已知边界（接受）**：①should_exit 早退不刷新（下轮入口组装自愈）②未落库 supplement 同盲区（与压实回调同先例）③子 Agent 无 hook（on_fold_applied=None 跳过，陈旧至下入口）
- **实机验证（待用户重启）**：折叠后同轮下一条 LLM 请求即见占位符 + 折叠后使用率

#### 修复：循环折叠——不落库丢 LLM 动作确认记忆致反复折叠同一批编号（用户实机抓出 raw_http 实证，main 5f829ee9/b0aeec13）

- **bug（用户实机抓出，raw_http 实证）**：LLM 循环调用 fold_tool_output 折叠**同一批编号**（raw_http 15 个请求反复调同一 output_ids）。根因三叠加：①87cba0d1「成功结果不落库」优化使 LLM 在后续轮次看不到「我折过了」的记录——动作确认记忆丢失；②占位符缺完成态（不标明已由 fold_tool_output 折叠）；③freed 只算有快照的行，误导释放量（折 6 条显示仅释放 0.4% → LLM 判定折叠无效继续折）
- **修复四件套**：①**回滚不落库**——fold 成功结果照常落库（输出极小、提炼管道不提炼无语义工具文本、排障证据链完整；87cba0d1 的 _skip_persist 打标+persist 跳过全删）②占位符改完成态「[输出#N 已由 fold_tool_output 折叠：{tool}({参数摘要≤80字符，无配对 unknown})，本条已移出上下文（原占约 X%）。如需原文请重新调用原工具获取]」——保留工具名+参数摘要=LLM 重新调用原工具的通道（spec §4），仍以「获取]」收尾兼容 _is_tool_placeholder；pct None 省略占比分句 ③freed 无快照旧行按字符粗估计入（len(content)÷2÷window×100，约 2 字符/token）+ message 注明「旧输出按字符粗估」——LLM 见真实释放量不再误判无效 ④niu.md 补教学：已折叠占位符不重复折叠；不可再生一次性数据（子 Agent 交互结果、结论性回复）不折叠
- **关键教训**：**LLM 的动作确认记忆不能省**——不落库省了 DB 噪声却丢了 LLM 的「我做过了」证据，导致循环；自维护类工具的结果必须让 LLM 在后续轮次能看到（当轮 + 持久），否则 LLM 会重复执行
- **验证**：108 绿 + 回归绿

#### 工程：工具循环视图统一组装——每工具轮全量重建（fold hook 升级，浏览→同循环折叠场景根治；计划 v1.0→v1.2 R1-R2 双审门禁 + SDD T1-T2 双审，main 4f52e76c + tests/test_fold_view_refresh.py）

- **bug（用户实测，DB 2813-2831 实证）**：浏览京东大输出（占 8.4%）→ **同一工具循环内折叠失败**——LLM 猜编号 2812/2813（实际是 assistant/user 行）连续报错"不是工具输出"；用户新消息触发新一轮（入口重组装）→ LLM 看到 `[输出#2815]` → 一把折对。**根因**：视图只在入口组装一次 + fold hook 只在 fold 轮触发——工具循环内新产生 tool 输出在 LLM 视野**无 `[输出#N]` 头行**（头行只在组装渲染），LLM 只能猜 rowid
- **用户拍板方向**：循环内外上下文组装应**同一套流程**（全量非增量）——动态注入侧已每轮刷新（on_before_llm），缺的是消息窗口区每轮刷新
- **方案**：fold hook **升级为每工具轮 hook** `on_tool_round_refresh`（runner._on_fold_applied 改名/语义扩展，方法体零改动）：任何工具结果 persist 落库后从 DB 全量重建视图（`assemble_view_sync` + `transform_history` + system 保留含 cache_control + `messages[:]` 原地替换）——新输出编号/折叠态/仪表盘/索引与 DB 同步；`_fold_occurred` 检测退役
- **挂点论证（scout 双路时序实证——为什么 persist 后、而非 on_before_llm）**：persist 只写 assistant/tool 两 role；未落库引导（supplement drain 即毁不可再生/同步挂起警告 `_sync_suspend_warned` 置位不重置/截断重试对/拦截对）生命周期=N 轮 append → N+1 轮首消费。重建挂 on_before_llm 会把刚 append 的引导删掉 → supplement 丢失 + 挂起警告静默失效（回归）；重建挂 **persist 后、退出判定/引导注入前**零丢失面（不变式：重建点前本轮消息全 persist；重建点处未落库消息均已至少被 LLM 消费一次）。**tool_results 守卫必须**（R1 双审交叉 P1-1）：纯文本轮经 no_tool 占位路径也直落重建块（while 体级无 tool_calls 门控）——无守卫则每轮触发
- **组合级回归锁**：harness 消费 persist 落库（yield 即落库同步漏斗）+ 真 `_on_tool_round_refresh`（monkeypatch _sync_get_messages + 全局 ContextManager 指同 tmp store）→ 断言**普通工具轮后下轮 LLM 请求 messages 含 `[输出#{rowid} · 工具名]` 头行**——bug 直接行为锁（req1 无头行/req2 有）；另翻转两旧语义测试（fold 幂等/失败/非 dict 轮断言 [] → 恰触发 1 次；fold 后 read_file 轮 1 次 → 2 次）+ 多 tool_calls 单响应 hook==1 契约锁（锁"轮末一次"非"逐 tool_result"）
- **关键教训**：工具循环内新内容（新输出/折叠/仪表盘）的可见性 = 视图刷新频率问题——fold 专用 hook 治标（fold 轮才刷），每工具轮重建治本（任何 persist 都刷）；动态注入早就是每轮刷新，消息窗口区才是盲区
- **质量链**：scout 双路行号级实证调查 → 计划 R1 双 CONDITIONAL（P1-1 纯文本轮守卫/P1-2 旧语义测试翻转/P2 压实占位复原边界/P2 文档同步面）→ v1.1 修复 → R2 双 APPROVE 门禁 → SDD T1 生产（spec APPROVE + quality CONDITIONAL 2 处文案残留修复闭环）+ T2 测试（spec/quality 双 APPROVE + 2 P3 补强）→ fold 系列 69 passed
- **已知边界（接受）**：①**压实占位符化 view-only 复原**（R1-A P2-1）：build_compact_view 占位符化只改内存不写 DB，rebuild 复原全文 → 压实轮后工具轮膨胀再压实逐入口复发（_compress_cooldown 限每 loop ≤1 次有界；95% 应急线兜底；修=改压实写 DB 占位态违反最小改动铁律——接受）②should_exit 早退不刷新（下轮入口组装自愈）③子 Agent 无 hook（消息不入 DB）④每工具轮重建为 O(总历史) DB 读+渲染（毫秒级 vs LLM 秒级，用户拍板接受）
- **实机验证（待用户重启）**：浏览网页后同一工具循环内折叠成功（下轮即见编号不再猜）；折叠后使用率同轮可见；长程任务中途技能注入不退出（既有行为回归确认）

#### 修复：上下文使用率提取——校准倍率全量化 + 展示层真值化（用户指出擅自简化 + 页面 43% vs 动态块 36.5% 不一致；spec v0.1-v0.6 六轮双审门禁 + 计划 v1.1 双审 + SDD T1-T4 PM diff 复核 + 收官整体审查补位，main b7c5137a/ad84faff/b9f3d2e1/69a5b871/61b9b842）

- **用户报告**：页面显示 43%（真值）而 LLM 动态块 36.5%（估算）——不一致
- **根因一（校准倍率被污染——用户指出"你算的指数本身可能算错"）**：原始设计明确"真值÷**同消息集**本地估算"（2026-08-25 spec Task 3），实现**擅自简化为增量缓存**（入口算一次基线 + 只对尾部新增切片增量计数，注释"避免每响应全量重算"）——messages 被**原地改写且长度不变**的操作（每工具轮 rebuild/折叠占位符/截断/占位符化）不感知 → `_calib_est` 残留改写前内容 → ratio 漂移 → 下游一切用 ratio 的估算（逐条 output_pct、usage、压实预估）系统性错
- **根因二（展示层自算全量——用户指出"大模型已返回准确数据为什么重算"）**：动态块 usage 用 `(view+sys_est)×ratio` 另算（36.5%），服务端每轮返回的准确 `prompt_tokens`（42.7%）没用——估算的价值只在**逐条**（每条 tool 输出无服务端真值），全量展示应直接用真值
- **修复三件套（用户三原则：展示用真值/估算只逐条/ratio 全量同集）**：①**M1 ratio 全量化**：删增量缓存，每响应 `update_ratio(prompt_tokens, count_messages_tokens(messages))`（messages 在响应返回点=完整发送集含 system/动态块/索引；保留子 Agent 门控+try/except+>0 守卫）②**M2-F1 显示规则"真值优先、清零即失效、估算兜底"**：动态块 usage 改读 `handler._last_prompt_tokens ÷ window`（真值，与页面同源）；fold 成功**清 handler 真值双清**（agent_loop 层贴压实清零先例——fold 后旧真值失效落估算兜底=折叠后视图估算，修复①"折叠后同轮可见"自然成立；幂等 folded=[] 不清；整体 try/except 非 JSON 跳过）；`get_fold_dashboard_line(usage_override)` 接口（None 最高优先）③**M2-F2 兜底单源与回填**：页面 get_stats 0 分支三级取值链（真值→_fold_stats→compute fallback，仅主 Agent；子 Agent 0 语义）；/new reset_derived_state 清 `_fold_stats`；**压实回填四出口**（in-loop/手动 /compact/溢出压实/组装出口 AUTO_GATE——压实成功把 `_fold_stats["usage"]` 覆写为 compaction stats 值，与 done 推送同值；双守卫 `_fold_stats is not None and usage is not None`）
- **关键教训（用户批评）**：**禁止擅自变通既定要求**——"最早设计说得非常清楚要全量的数据去算尽可能贴近真实，结果你不按我所说的自己想个办法简化了这套逻辑"；"说好怎么做就是怎么做，为什么中间还自己有变通？你做的这些事我是发现了，有多少我没发现的？"——性能顾虑（每响应全量 count）不是简化正确性逻辑的理由（毫秒级 vs 秒级 LLM 差千倍）；实现偏离 spec 的"增量缓存优化"是擅自决定，埋下系统性漂移
- **质量链**：spec R1-R6 六轮双审收敛（stale 状态机删除改"清零即失效"/压实回填从错锚 _tidy 修正为四出口/fold 清零从跨模块标记收敛为 agent_loop 内实现/接口传真值结果非原始 token）→ 计划 R1-R2 门禁 → **SDD T1-T4 PM diff 复核（未派每 Task 双审——用户指出流程缺口）→ 收官整体审查补位（FinalReview：A 技术 CONDITIONAL 1 P2 + B 原则 APPROVE 2 P3——P2 质量链虚记修正；P3 采纳 fold 清零同步循环局部变量）**
- **验证**：127 passed（105 基线 + 22 新增：M1 全量回归锁（fold 原地改写不漂移）/显示规则/fold 清零变体//new/回填四出口含组装出口行为锁/页面同源）+ 相关回归绿
- **实机移交清单（待用户重启）**：①M1-P 性能实测（≥500 条发送集 count ≤200ms 或 <TTFT 1% 入册）②ratio 对账（raw_http N 请求真值÷该请求 messages 全量 count vs 文件 ratio 偏差 <±3%）③展示一致性（fold 前后动态块行：真值→估算→新真值；同刻 /api/stats 同值；手动 /compact 同刻同值；重启后首轮入口压实非触发线膨胀值）
- **已知边界（接受）**：fold/压实后至下轮响应为估算窗口（无真值唯一途径）；轮内真值滞后（上轮真值 vs 工具轮间视图增长，有界单轮增量）；压实回填后 n/p（可折叠条数）沿用压实前至下次 rebuild ≤1 轮；delete_messages 路径不清真值（既有行为）；compute fallback 口径差异（末级）

### 2026-09-01

- 新增：图谱详情面板关联实体右键进子图（用户实机验证通过）（详见 docs/AGENTS-HISTORY.md）
- 工程：定时任务第三种类型 task_kind='subagent'——子 Agent 静默执行 + @end report 反馈通道（详见 docs/AGENTS-HISTORY.md）

### 2026-08-31

- 工程：同步子 Agent 挂起丢失防护——退出前拦截警告 + cleanup 现场保留 + 4 端点清理（反转 2026-08-26「随主循环退出被回收」定案）（详见 docs/AGENTS-HISTORY.md）
- 工程：Browser elements 大小控制——精简逻辑全删，elements 原样输出 + 头尾截断保护 tabSummary（详见 docs/AGENTS-HISTORY.md）

### 2026-08-28

- 工程：测试债清算（T0-T7）——147 条失败全核销 + 版本 0.3.1（详见 docs/AGENTS-HISTORY.md）
- 工程：read 工具智能分页——29000 字符页预算按行截断取代行内均分截断（详见 docs/AGENTS-HISTORY.md）
- 修复：read 工具 tail 读预算方向——EOF 锚定窗口 + 反向累积（用户指出 + 四轮审查冻结）（详见 docs/AGENTS-HISTORY.md）
- 加固：子 Agent 压缩可见性两条微改造（归档机制经论证不建）（详见 docs/AGENTS-HISTORY.md）
- 修复：请求组装 thinking 双通道去冗余（用户看日志发现 + 双审通过）（详见 docs/AGENTS-HISTORY.md）

### 2026-08-27

- 工程：设置页模型列表在线探测 + 选中自动填档（详见 docs/AGENTS-HISTORY.md）
- 修复：测试隔离漏洞——test_clear_brain_state 真删生产指针块库（详见 docs/AGENTS-HISTORY.md）

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

- 工程：journal 子 Agent 直读 DB——日志即水位线（详见 docs/AGENTS-HISTORY.md）

### 2026-08-26（续二）

- 工程：mcp-servers.yaml 双目录化——copy-once 设计债清偿（详见 docs/AGENTS-HISTORY.md）

### 2026-08-25

- 工程：MD 中继工程五——force dream 保护链退役 + dream 游标终退 + 化石清理（详见 docs/AGENTS-HISTORY.md）

### 2026-08-24

- 工程：MD 中继工程四——睡眠管道重排 + 压缩前置门控清算 + 游标清算（详见 docs/AGENTS-HISTORY.md）

### 2026-08-22

- 修复：向量检索精确名短路——query 恰为实体名时图层精确命中置顶（精确名查询根治）（详见 docs/AGENTS-HISTORY.md）

### 2026-08-21

- 修复：entity-extractor 提炼入库 doc_id 撞车静默丢失（详见 docs/AGENTS-HISTORY.md）
