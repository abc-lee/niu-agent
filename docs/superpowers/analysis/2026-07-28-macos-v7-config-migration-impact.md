# macOS v7 打包方案：文件移动影响面分析报告

> 仅分析，未改任何代码。所有结论基于代码证据（文件:行号）。

---

## 1. 睡眠触发根因：sleepTriggerMinutes 失效的真实原因

### 1.1 结论：不是"文件移动后读错路径"，而是"后端根本不读这个字段"

`sleepTriggerMinutes` 这个字段的**唯一读取方**是前端 `ui/main/preload-assistant.js:11`，而且**该读取路径在 commit `d90d4fe1` 中已经修对**（从项目根 `config/user-config.json` 改为 `~/.niu/config/user-config.json`）。后端 Python/Rust 代码里**零处读取**该字段——这不是 v7 文件移动导致的回归，而是该字段从一开始就只服务于前端小女孩窗口的 UI 空闲计时器。

### 1.2 睡眠触发的完整链路（代码证据）

实际触发"睡眠"的有**三条独立链路**，需区分清楚：

| 链路 | 触发源 | 读什么配置 | 路径正确性 | 后果 |
|------|--------|-----------|-----------|------|
| **A. 小女孩 UI 睡眠动画 + tidy** | `spirit.html:165` `IDLE_TIMEOUT` | `preload-assistant.js:11` 读 `~/.niu/config/user-config.json` 的 `context.sleepTriggerMinutes` | ✅ 正确（已修） | 小女孩空闲 N 分钟后播放睡眠动画并调 `/api/context/tidy`(mode=sleep) |
| **B. Agent 对话结束 chat_idle** | `agent/generic/agent_loop.py` 多处 yield `chat_idle`（:650/:738/:752/:815 等） | **不读任何配置**，纯控制流 | N/A（硬编码控制流） | 只发 SSE 事件让前端切 idle 状态，不触发整理 |
| **C. 后端 auto-tidy（已废弃）** | `niu_api/compat.py:1087` `_check_and_trigger_auto_tidy` | **函数首行 `return False`，已禁用**（:1082-1083） | N/A（死代码） | 不触发 |

**链路 A 才是真正受 sleepTriggerMinutes 控制的**，且它已经读对路径。它做的事情是：

1. 小女孩窗口空闲 `IDLE_TIMEOUT` 毫秒后 → `spirit.html:336` `setState(State.SLEEP)`
2. `setState(SLEEP)` 分支（:271-274）调用 `triggerTidy()`
3. `triggerTidy()`（:363-388）调 `fetch('http://127.0.0.1:9876/api/context/tidy', {mode:'sleep'})`
4. 后端 `_tidy_context_impl(mode=sleep)`（`compat.py:2235`）跑整理流水线

### 1.3 为什么用户感觉"5-6 分钟睡眠"——三个可能根因（需用户进一步确认）

我**实测了用户当前的 `~/.niu/config/user-config.json`**，发现：

```json
"context": {
    "contextWindowSize": 200000,
    "warningThreshold": 0.8,
    "compressTargetTokens": 60000,
    "sleepTriggerMinutes": 5   ← 文件里实际是 5，不是用户以为的 30
}
```

**文件里就是 5**，不是 30。所以"系统按 5-6 分钟睡眠"与文件内容**完全一致**——配置没有失效，是用户以为改成了 30 但文件里仍是 5。可能原因：

- **R1（最可能）**：用户在设置窗口改了 30，但点的是关闭/取消而非"测试连接并保存"。根据 `2026-07-27-first-run-config-probe-fix.md` 的 R5 原则，**只有测试通过才 saveConfig**，直接关窗不保存。设置窗口的 `sleepTriggerMinutes` 字段在 `testAndSave()` 流程里（`index.html:452`），不在独立保存按钮下。
- **R2**：用户改过但后来被某次"首次启动兜底"覆盖回 5（但当前 `firstRun:false`，且兜底只在文件不存在时触发，可能性低）。
- **R3**：用户改的是另一个文件（如旧路径项目根 `config/user-config.json`，但该文件已不存在）。

### 1.4 字段语义澄清（重要）

`sleepTriggerMinutes` **不是**"Agent 后端什么时候进入睡眠模式整理上下文"的配置——后端**从不读它**。它只是"小女孩悬浮窗空闲多久后播放睡眠动画并顺手调一次 tidy API"的 UI 计时器。真正的上下文整理触发在 `agent_loop.py` 工具循环内同步进行（`compat.py:1082` 的 auto-tidy 已 `return False` 废弃，见注释"压缩只在 agent_loop 工具循环中同步触发"）。

**所以即使把 sleepTriggerMinutes 改成 300，后端压缩行为也不会变——它只影响小女孩多久播放睡眠动画。**

---

## 2. v7 方案文件移动清单 + 读取路径核对表

v7 方案（`2026-07-24-macos-app-bundle-self-contained-v7.md`）涉及的文件迁移分两类：**运行时写数据迁移到 `~/.niu/`**，和**模板源保留在 bundle 内 `config/`**。逐项核对：

### 2.1 迁移到 `~/.niu/` 的文件（运行时可写数据）

| # | 文件 | 迁移到 | 代码读取点 | 读取路径 | 正确? | 后果/说明 |
|---|------|--------|-----------|---------|-------|----------|
| 1 | `user-config.json` | `~/.niu/config/` | `niu_api/config.py:49-62` `_get_config_path()` → `CONFIG_PATH` | `~/.niu/config/user-config.json` | ✅ | 被全仓正确引用：`agent/subagent.py:121`、`niu_api/internal/region_manager.py:38`、`niu_api/internal/lightrag_manager.py:221`、`niu_api/llm_proxy.py`、`niu_api/chat.py:333`、`niu_api/compat.py:1211`、`launcher/src/main.rs:1405`。**注意：该文件无模板，由设置窗口 saveConfig 创建（R16 决策）** |
| 1b | 同上 | 同上 | `ui/main/preload-assistant.js:8` | `~/.niu/config/user-config.json` | ✅ | commit `d90d4fe1` 已修（原读项目根不存在的文件） |
| 1c | 同上 | 同上 | `ui/main/main.js:1126-1133` `get-config` handler | `~/.niu/config/user-config.json`（写）+ bundle 内 `llm-presets.json`（只读） | ✅ | v7 Task 9 改造，userConfigPath 指向 niuConfigDir |
| 2 | `mcp-servers.yaml` | `~/.niu/config/` | `niu_api/config.py:66-79` `_get_mcp_servers_path()` | `~/.niu/config/mcp-servers.yaml` | ✅ | 懒复制兜底；`agent/mcp_loader.py:46` 调此函数 |
| 2b | 同上 | 同上 | `launcher/src/main.rs:957-994` `init_niu_dir` | 源 `project_root/config/mcp-servers.yaml` → 目标 `~/.niu/config/mcp-servers.yaml` | ✅ | Rust 启动时复制（不覆盖已存在） |
| 3 | `memory.json` | `~/.niu/` | `agent/runner.py:210`、`agent/subagent.py:584`、`agent/handler.py:1045`、`agent/autonomous_explorer.py:180`、`niu_api/internal/scheduler/service.py:37`、`launcher/src/main.rs:1124` `load_memory` | 全部 `Path.home() / ".niu" / "memory.json"` | ✅ | 全仓一致读 `~/.niu/memory.json` |
| 3b | 同上（模板源） | bundle `memory/` | `launcher/src/main.rs:928-952` `init_niu_dir` | 源 `project_root/memory/memory.json` → 目标 `~/.niu/memory.json` | ✅ | 模板从 `memory/` 目录复制（不是 `config/`） |
| 4 | `preferences.json` | `~/.niu/` | `niu_api/internal/reranker.py:44/211`、`niu_api/internal/embedding.py:50/257`、`niu_api/internal/lightrag_manager.py:455/1176` | 全部 `Path.home() / ".niu" / "preferences.json"` | ✅ | 全仓一致读 `~/.niu/` |
| 4b | 同上（模板源） | bundle `memory/` | `launcher/src/main.rs:928-952` | 源 `project_root/memory/preferences.json` → 目标 `~/.niu/preferences.json` | ✅ | 同 memory.json |
| 5 | `skills/*.md` | `~/.niu/skills/` | `agent/injector/sync.py:180-182` `_default_skills_dir()` | `Path.home() / ".niu" / "skills"` | ✅ | SkillSync 扫描 `~/.niu/skills/` |
| 5b | 同上（模板源） | bundle `memory/skills/` | `launcher/src/main.rs:1009-1124` `init_niu_dir` | 源 `project_root/memory/skills/*.md` → 目标 `~/.niu/skills/` | ✅ | 逐个复制 .md，不覆盖已存在 |
| 6 | `window-config.json` | `~/.niu/` | `ui/main/main.js:36` 附近 | `~/.niu/window-config.json` | ✅ | v7 Task 9 Step 1，含旧路径迁移逻辑 |
| 7 | `agents/*.md`（用户创建） | `~/.niu/agents/` | `agent/subagent.py:403` `_USER_AGENTS_DIR` | `~/.niu/agents/` | ✅ | 通用子 Agent 查找路径 |
| 8 | 日志目录 | `~/.niu/logs/` | `launcher/src/main.rs:1421` `log_fatal_error`、`niu_api/http_log_api.py`、`niu_api/channel/gateway.py`、`agent/generic/http_logger.py`、`agent/generic/litellm_adapter.py` | `~/.niu/logs/` | ✅ | v7 Task 5/6 改造 |
| 9 | `disk/*.yaml`（用户可写覆盖） | `~/.niu/disk/` | `agent/runner.py:605-607` `DiskEngine([bundle_disk_dir, user_disk_dir])` | bundle 内 `config/disk/`（只读）+ `~/.niu/disk/`（可写覆盖） | ✅ | 双层设计，`~/.niu/disk/` 当前不存在但代码容错（`disk_config.py:301` skip 不存在目录） |

### 2.2 保留在 bundle 内 `config/` 的文件（只读模板）

| # | 文件 | 位置 | 代码读取点 | 正确? | 说明 |
|---|------|------|-----------|-------|------|
| 10 | `config/agents/niu.md` | bundle `config/agents/` | `agent/runner.py:544` `niu_md_path = os.path.join(script_dir, "..", "config", "agents", "niu.md")` | ✅ | `__file__`=bundle 内 `agent/runner.py`，拼出 `Contents/Resources/config/agents/niu.md`，**实测 bundle 内存在**（17714 字节）。主 Agent 提示词，只读 |
| 10b | 同上 | dev 模式 | 同上 | ✅ | dev 模式 `__file__`=项目根 `agent/runner.py`，拼出项目根 `config/agents/niu.md`，存在 |
| 11 | `config/agents/{其他}.md` | bundle `config/agents/` | `agent/subagent.py:400-402` `_PROJECT_AGENTS_DIR` + `:424` | ✅ | `os.path.join(dirname(dirname(__file__)), "config", "agents")`，dev/bundle 都指向 `config/agents/`。专用子 Agent 提示词，只读 |
| 12 | `config/llm-presets.json` | bundle `config/` | `ui/main/main.js:1133` `presetsPath = path.join(bundleConfigDir, 'llm-presets.json')` | ✅ | 只读模板（v7 Task 9 Step 2 明确"只读，仍从 bundle 内读"） |
| 12b | 同上 | bundle | `mcp-servers/config-manager/src/niu_config_manager/__init__.py:340` `PRESETS_PATH` | ✅ | `Path(__file__).parent.parent.parent.parent.parent / "config" / "llm-presets.json"`，从 bundle 内模块算出 bundle 内 config，只读 |
| 13 | `config/disk/*.yaml` | bundle `config/disk/` | `agent/runner.py:605` `bundle_disk_dir` | ✅ | 见 #9，作为只读层 |
| 14 | `config/agent-template.md` | bundle `config/` | **找不到读取点** | ⚠️ | grep 全仓零引用。可能未使用或已废弃（历史遗留）。不影响功能 |

### 2.3 Rust 侧 `should_enable_logging` 路径核对

`launcher/src/main.rs:1403-1419` `should_enable_logging()` 读 `~/.niu/config/user-config.json` 的 `logging.enabled`，路径正确（v7 Task 5 改造）。实测 `~/.niu/config/user-config.json` 里 `logging.enabled=true`，与 Rust 侧读取路径一致。

---

## 3. 影响面总结

### 3.1 因 v7 文件移动而失效的配置：**零个**

所有被移动的文件，代码读取路径都**已正确指向新位置**（`~/.niu/...` 或 bundle 内 `config/...`）。v7 方案的 14 个 Task 执行完整，路径迁移无遗漏。具体：

- `user-config.json`：6 处 Python + 1 处 JS + 1 处 Rust 读取，全指向 `~/.niu/config/`
- `mcp-servers.yaml`：Python `_get_mcp_servers_path` + Rust `init_niu_dir`，全指向 `~/.niu/config/`
- `memory.json`/`preferences.json`：全仓 8+ 处，全指向 `~/.niu/`
- `skills/`：`injector/sync.py` 指向 `~/.niu/skills/`，Rust `init_niu_dir` 从 `memory/skills/` 复制
- `agents/*.md`：`_PROJECT_AGENTS_DIR` 指 bundle 内 `config/agents/`（只读），`_USER_AGENTS_DIR` 指 `~/.niu/agents/`（可写）
- `llm-presets.json`：JS + Python 两处都指 bundle 内 `config/`（只读）
- `disk/*.yaml`：双层 `config/disk/`（只读）+ `~/.niu/disk/`（可写）

### 3.2 虚惊一场：sleepTriggerMinutes

用户的疑虑"文件移动后读错路径导致失效"**不成立**：

- 读取路径在 `d90d4fe1` 已修对（早于 v7 或 v7 修复的一部分，bundle 内也已同步——实测 bundle 内 `preload-assistant.js` 第 8 行就是正确路径）
- 实测用户当前 `~/.niu/config/user-config.json` 里 `sleepTriggerMinutes: 5`，系统按 5 分钟睡眠**与文件一致**，配置没失效
- 用户以为改成了 30 但文件里是 5，最可能是 R1（设置窗口改了但没走"测试连接并保存"流程，直接关窗不保存——这是 `2026-07-27-first-run-config-probe-fix.md` R5 设计的副作用：只有测试通过才写文件）

### 3.3 真正的潜在隐患（非 v7 回归，是历史设计特征）

| 隐患 | 证据 | 影响 |
|------|------|------|
| `sleepTriggerMinutes` 字段语义误导 | 后端零读取，只前端小女孩 UI 用 | 用户/开发者误以为它能控制后端整理触发时机。实际后端整理在 `agent_loop` 工具循环内同步触发，与此字段无关 |
| `config/agent-template.md` 无引用 | grep 全仓零读取 | 死文件，建议清理或确认用途 |
| `compat.py:1082-1133` auto-tidy 死代码 | `_should_auto_tidy` 首行 `return False`，`_check_and_trigger_auto_tidy`/`_run_auto_tidy` 注释"DEPRECATED: no callers" | 维护负担，建议删除 |
| `AutonomousExplorer` 硬编码 30 分钟 | `agent/autonomous_explorer.py:37` `DEFAULT_IDLE_THRESHOLD = 30*60`，不读配置 | 另一个空闲触发器，与 sleepTriggerMinutes 无关，但同样不可配置 |

---

## 4. 修复方向建议（只建议，不改代码）

### 4.1 针对 sleepTriggerMinutes"失效"

**不需要改代码**——代码路径已正确。需要的是：

1. **确认用户实际操作**：让用户在设置窗口填 30 后，**必须点"测试连接并保存"**并等测试通过（probe 探测可能要 1-5 分钟），才会写入文件。直接关窗不保存。可让用户改完后 `cat ~/.niu/config/user-config.json` 确认 `sleepTriggerMinutes` 实际值。
2. **语义澄清**：在用户文档/设置窗口 tooltip 说明 `sleepTriggerMinutes` 只控制小女孩悬浮窗空闲多久播放睡眠动画+触发 tidy，**不控制后端压缩时机**。若用户想控制后端整理，应调 `contextWindowSize`/`warningThreshold`/`compressTargetTokens`。

### 4.2 针对 v7 文件移动

**无需修复**——所有路径已正确。可选的清理：

1. 删除 `config/agent-template.md`（零引用死文件）——或先确认是否有构建脚本/其他用途引用。
2. 删除 `compat.py:1082-1133` 的 auto-tidy 死代码（`_should_auto_tidy`/`_check_and_trigger_auto_tidy`/`_run_auto_tidy`），降低维护负担。
3. `agent/subagent.py:124/139/171/190` 的 docstring 仍写"from config/user-config.json"（过时注释，实际读 `~/.niu/config/`），可更新注释避免误导——纯注释，无功能影响。

### 4.3 若想让后端真正可配置"睡眠触发时机"

当前后端压缩触发时机由 `agent_loop.py` 内部逻辑 + `warningThreshold`/`compressTargetTokens` 控制，不读 `sleepTriggerMinutes`。若产品上需要"空闲 N 分钟后整理"，需新增后端读取逻辑（如 `chat_queue.py` worker 空闲计时），但这属于**新功能**，不是 v7 回归修复。

---

## 附：关键代码证据索引

| 证据 | 文件:行号 |
|------|----------|
| sleepTriggerMinutes 唯一读取点（已修对路径） | `ui/main/preload-assistant.js:8-14` |
| 修复 commit | `d90d4fe1`（`git log -- ui/main/preload-assistant.js`） |
| bundle 内 preload 已同步 | `niu.app/Contents/Resources/ui/main/preload-assistant.js:8` |
| sleepTriggerMinutes 后端零读取 | grep 全仓 `agent/` `niu_api/` `launcher/` 无匹配 |
| user-config.json 主读取路径 | `niu_api/config.py:49-62` `_get_config_path()` |
| user-config.json 全仓引用点 | `agent/subagent.py:121`、`region_manager.py:38`、`lightrag_manager.py:221`、`launcher/src/main.rs:1405` |
| mcp-servers.yaml 读取 | `niu_api/config.py:66-79`、`agent/mcp_loader.py:46`、`launcher/src/main.rs:957-994` |
| memory.json 全仓读取 | `agent/runner.py:210`、`agent/subagent.py:584`、`launcher/src/main.rs:1124` 等（全 `~/.niu/memory.json`） |
| preferences.json 全仓读取 | `niu_api/internal/reranker.py:44`、`embedding.py:50`、`lightrag_manager.py:455`（全 `~/.niu/`） |
| niu.md 主提示词读取 | `agent/runner.py:544`（`../config/agents/niu.md`，bundle 内存在） |
| bundle 内 config/ 完整性 | `niu.app/Contents/Resources/config/`（含 agents/disk/llm-presets.json/mcp-servers.yaml） |
| auto-tidy 已废弃 | `niu_api/compat.py:1082-1083`（`return False`） |
| AutonomousExplorer 硬编码阈值 | `agent/autonomous_explorer.py:37`（30*60，不读配置） |
| init_niu_dir 模板复制清单 | `launcher/src/main.rs:909-1124`（memory.json/preferences.json/mcp-servers.yaml/skills/，**不含 user-config.json**——R16 无模板设计） |
