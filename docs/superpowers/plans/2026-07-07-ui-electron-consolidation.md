# UI 三套 Electron 合并为单一应用 Implementation Plan (v2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `ui/assistant/`、`ui/settings/`、`ui/graph/` 三个独立 Electron 应用合并为 `ui/main/` 单一应用，共用一套 node_modules + 一套 electron 二进制，按 `NIU_WINDOW` 环境变量决定开哪个 BrowserWindow，**跨平台兼容（macOS/Windows）**。

**Architecture:** 在 `ui/main/` 下新建统一 Electron 应用，一个 `main.js` 入口按 `process.env.NIU_WINDOW`（`assistant` / `settings` / `graph`）分支创建对应 BrowserWindow。三个原 main.js 的 IPC handler 命名零冲突（assistant 33 个、settings 6 个、graph 12 个，已核对），直接合并到同一 `ipcMain`。前端资源（HTML/CSS/JS/assets）从三个原目录移动到 `ui/main/windows/{assistant,settings,graph}/`。Rust 启动器 `launch_window` 改为固定 spawn `ui/main/`，传 `NIU_WINDOW` 环境变量区分（Rust `Command::env` 跨平台一致）。assistant 的 `open-graph`（托盘菜单）改为同进程 `createGraphWindow()`；命令行 `niu --graph` 仍走独立进程（保持原架构）。**重构期间禁启动 `./niu`**——Task 1-7 全部完成后才允许 Task 8 启动验证。

**Tech Stack:** Electron 33、Node.js、Rust（launcher）、npm、cross-env（跨平台环境变量）

---

## Context

### 当前架构问题

1. **三套 node_modules 共 1.5GB**：`ui/assistant/`（436M）、`ui/settings/`（562M）、`ui/graph/`（542M）各装一套 electron + 依赖
2. **settings/graph 装错 platform**：`ui/settings/node_modules/electron/dist/` 和 `ui/graph/node_modules/electron/dist/` 只有 Windows 版 `.exe`/`.dll`，没有 macOS `Electron.app`
3. **node_modules 被强制 add 进 git**：`.gitignore` 规则 `ui/*/node_modules/` 失效，`ui/settings/node_modules/`（4914 文件）和 `ui/graph/node_modules/`（4657 文件）已 tracked
4. **assistant → graph 跨进程通信复杂**：`open-graph` IPC 通过 spawn `niu --graph` 起独立 Rust 进程
5. **graph/index.html 第 91 行引用 `./node_modules/force-graph/dist/force-graph.min.js`**：移动到 `ui/main/windows/graph/` 后会断链（`ui/main/windows/graph/node_modules/` 不存在，真安装在 `ui/main/node_modules/`）
6. **大量文档/代码引用旧路径**：全局 grep `ui/assistant|ui/settings|ui/graph` 共 208 处引用（README/AGENTS/docs/Rust main.rs 等），合并后全部失真

### 关键约束（用户铁律）

- **禁止直接修改安装包**（包括 npm 安装的 `node_modules`）——只能改源码 + 让用户/构建重新 `npm install`
- **Rust 启动器编译必须用 `./launcher/build.sh`**，禁止 `cargo build`（铁律 #8）
- **修改前必须先做临时提交备份**（铁律 #3，**每个 Task 第一步都要做**）
- **测试必须用真实数据+真实 LLM**（铁律 #5）——Task 8 验证前先配真实可用 LLM
- **python/ 目录必须是完整自包含 Python 安装**（铁律 #6）——`ui/main/node_modules/` 分发策略见"分发策略"节
- **git 操作后必须修复文件权限**（铁律 #7）：`find python/bin/ -type f -exec grep -l '^#!' {} \; | xargs chmod +x` 和 `find ui/*/node_modules/.bin/ -type f ! -perm -u+x -exec chmod +x {} \;`
- **删除文件前必须比对内容、说清原因、得到用户同意**（铁律，Task 7 删 node_modules 前要确认）
- **重构期间禁止启动 `./niu`**：Task 1-7 期间用户随时启动都会崩（HTML 移走了但 main.js 还在原位），Task 8 npm install 完成前都不允许启动

### 跨平台要求（用户明确要求）

1. **npm script 用 `cross-env`**：Windows cmd/PowerShell 不认 `NIU_WINDOW=X electron .` 这种 Unix 语法，必须用 `cross-env` 包
2. **路径全用 `path.join`**：不硬编码 `/` 或 `\\`
3. **环境变量传递**：Rust `Command::env("NIU_WINDOW", name)` 跨平台一致，但 Windows 上 `cmd /C npm start` 是否透传 env 需在 Task 8 验证
4. **electron-builder 双平台打包**：macOS（.app + dmg）+ Windows（.exe + nsis），配置在 `ui/main/package.json` 的 `build` 字段
5. **Dock 行为差异**：macOS 的 `app.dock.hide()` 仅在 assistant 模式调，settings/graph 模式不调（否则窗口不显示）；Windows 无 Dock 概念，跳过
6. **electron 二进制按平台解压**：`npm install` 在 macOS 装 `Electron.app`，在 Windows 装 `electron.exe`——这是 electron npm 包的正常行为，不需要特殊处理

### 分发策略

**当前阶段（开发）**：`ui/main/node_modules/` 不提交 git（.gitignore 规则生效），开发者本地 `npm install` 装对应平台二进制。

**未来打包分发**：用 `electron-builder` 打成平台包（macOS .app / Windows .exe），electron 二进制打进包里，用户不需要装 Node.js。这一步不在本计划范围（属于打包流水线建设），但 `ui/main/package.json` 预留 `build` 配置字段。

**打包路径注意**：graph/index.html 第 91 行的 `../../node_modules/force-graph/dist/force-graph.min.js` 是 **dev 模式相对路径**（`ui/main/windows/graph/` → `ui/main/node_modules/`）。打包后 electron-builder 把 node_modules 平铺到 app resources 目录，这个相对路径会失效。**未来打包时需改为 `node_modules/force-graph/...`（去掉 `../../`）**，或用 `process.env.NODE_ENV` 分支判断 dev/prod。本计划只解决 dev 模式，打包路径调整留给打包流水线任务。

### Task 失败回退策略

**禁止 `git reset --hard`**（铁律，会删本地 node_modules）。回退用以下方式：

1. **撤销部分文件**：`git checkout <commit>~1 -- <specific paths>`（恢复指定文件到某个 commit 之前的状态，不影响其他文件）。用 `git log --oneline -10` 定位每个 Task 的 commit SHA
2. **node_modules 回退**：Task 7 Step 4 备份了 `ui/assistant_node_modules_backup`，Task 8 验证失败时可 `mv assistant_node_modules_backup assistant/node_modules` 恢复旧 electron（macOS 版能跑）
3. **不要用 `git revert HEAD`** 撤销整个 Task——如果当前 HEAD 是 Task 8 Step 1 的空 backup commit，revert HEAD 撤不掉 Task 4/5/6 的实际改动

每个 Task Step 1 都有 `git commit` 备份，用 `git checkout <commit>~1 -- <paths>` 精确回退到任意 Task 之前的状态。

### IPC handler 命名零冲突（已核对）

- **assistant 33 个**：`set-spirit-position`、`resize-spirit-window`、`save-spirit-position`、`close-chat`、`open-chat`、`show-sticky`、`hide-sticky`、`sticky-mouse-enter`、`sticky-mouse-leave`、`spirit-mouse-enter`、`spirit-mouse-leave`、`create-note`、`update-note`、`delete-note`、`get-sticky-size`、`get-stats`、`save-sticky-size`、`close-all`、`spirit-state`、`notify-busy`、`notify-activity`、`open-external`、`open-with-system-viewer`、`get-image-url`、`process-image`、`send-message`、`send-to-agent`、`get-chat-session-id`、`get-history`、`clear-chat`、`open-graph`、`get-pending-messages`、`get-chat-status`
- **settings 6 个**：`get-presets`、`get-config`、`save-config`、`test-connection`、`close-window`、`minimize-window`
- **graph 12 个**：`kg-snapshot`、`kg-stats`、`kg-hubs`、`kg-explore`、`kg-find-path`、`kg-entities`、`kg-search-entities`、`kg-concepts`、`kg-surprising`、`kg-changelog`、`open-path`、`show-item-in-folder`

**已知 orphan IPC**（pre-existing bug，不是本计划引入）：`preload-chat.js` 第 5/8 行调用 `set-chat-position` / `resize-chat-window`，但 main.js 未注册——合并时保留现状（不补注册，避免扩大范围），记录在 Task 4 Step 8。

### 关键代码位置（用于改造时定位）

| 文件 | 行数 | 关键函数 |
|------|------|---------|
| `ui/assistant/main.js` | 1264 | `createSpiritWindow`(L59)、`createChatWindow`(L120)、`createStickyWindow`(L851)、`createTray`(L955)、`open-graph` IPC(L835)、托盘"打开图谱"(L984)、`window-all-closed`(L1037)、`before-quit`(L1042)、`app.dock.hide`(L945)、SSE 轮询(L1163-1263)、alerts 轮询(L1093-1107) |
| `ui/settings/main.js` | 261 | BrowserWindow(L13)、`window-all-closed`(L258) |
| `ui/graph/main.js` | 154 | BrowserWindow(L35)、`window-all-closed`(L144)、`activate`(L144) |
| `ui/graph/index.html` | L91 | `<script src="./node_modules/force-graph/dist/force-graph.min.js">` — 移动后断链，Task 5 修 |
| `launcher/src/main.rs` | ~1490 | `launch_window`(L836-868)、`--graph`/`--settings` 模式(L948-955)、LLM 失败启 settings(L1312-1350)、正常启 assistant(L1356)、settings 失败路径注释(L1317)、错误提示(L1360) |

---

## File Structure

### 新建文件

```
ui/main/
├── package.json                          # 合并三个 package.json，electron ^33，cross-env，build 配置
├── main.js                               # 总入口：按 NIU_WINDOW 分支创建 BrowserWindow
├── preload-assistant.js                  # 从 ui/assistant/preload.js 移动
├── preload-chat.js                       # 从 ui/assistant/preload-chat.js 移动
├── preload-sticky.js                     # 从 ui/assistant/preload-sticky.js 移动
├── preload-settings.js                   # 从 ui/settings/preload.js 移动+重命名
├── preload-graph.js                      # 从 ui/graph/preload.js 移动+重命名
└── windows/
    ├── assistant/                        # 从 ui/assistant/ 移动前端资源
    │   ├── spirit.html
    │   ├── chat.html
    │   ├── sticky.html
    │   ├── icons/                        # 9 个尺寸 PNG + icon.ico
    │   ├── fonts/AZhuPaoPaoTi.ttf
    │   ├── alert.gif, alert1.gif, busy.gif, idle1.gif, idle2.gif, sleep.gif, to-busy.gif, to-sleep.gif, wake.gif  # 9 个英文 GIF
    │   └── window-config.json
    ├── settings/                         # 从 ui/settings/ 移动前端资源
    │   └── index.html
    └── graph/                            # 从 ui/graph/ 移动前端资源
        ├── index.html                    # 引用路径在 Task 5 改为 ../../node_modules/force-graph/...
        ├── renderer.js
        ├── styles.css
        ├── demo.html
        └── test-api.html
```

### 修改文件

- `launcher/src/main.rs`（L836-868 `launch_window` 函数 + L948-955 / L1312-1350 / L1356 三处调用点 + L1317/L1360 注释/提示）
- 全局 208 处文档/代码引用旧路径（Task 8 Step 8 全局 grep 更新）

### 删除文件

- `ui/assistant/{main.js, preload*.js, package.json, package-lock.json}`（保留 HTML/assets，移动到 `ui/main/windows/assistant/`）
- `ui/settings/{main.js, preload.js, package.json, package-lock.json}`（保留 index.html，移动到 `ui/main/windows/settings/`）
- `ui/graph/{main.js, preload.js, package.json, package-lock.json}`（保留 index.html 等，移动到 `ui/main/windows/graph/`）
- `ui/settings/node_modules/`（4914 文件，Task 1 git rm --cached + Task 7 本地删）
- `ui/graph/node_modules/`（4657 文件，同上）
- `ui/assistant/node_modules/`（Task 7 先 mv 备份，Task 8 验证通过后删）

### 保留文件

- `ui/` 根目录的中文名 GIF（`唤醒.gif`/`忙碌.gif`/`默认-忙碌.gif`/`默认-睡觉.gif`/`默认1.gif`/`默认2.gif`/`睡觉.gif`）和 `默认1.png`——这些**不是** spirit.html 引用的，是其他用途（可能是文档/README 引用），**不动**

---

## Task 1: 止血 — 清理 git 中的 node_modules

**目标：** 立即从 git 移除 `ui/settings/node_modules/` 和 `ui/graph/node_modules/`（共 9571 文件），让 `.gitignore` 规则真正生效。本地文件系统保留 node_modules（用户当前还能用 `npm start` 启动 assistant）。

**Files:**
- Verify: `.gitignore`（确认规则）
- Delete (git only): `ui/settings/node_modules/`、`ui/graph/node_modules/`

- [ ] **Step 1: 临时备份提交（铁律 #3）**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add -A && git commit -m "backup: UI 三套 electron 合并前临时备份

基线：8df40b47（LLM 检测阻塞+权限修复已就位）
待改：合并 ui/assistant|settings|graph 为 ui/main 单一应用

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>" || echo "nothing to commit, working tree clean"
```

- [ ] **Step 2: 确认 .gitignore 规则**

```bash
grep -n "node_modules" .gitignore
```

Expected: 看到 `node_modules/` 和 `ui/*/node_modules/` 两行。如果规则缺失，先补上。

- [ ] **Step 3: 确认 node_modules 当前 git 状态**

```bash
git ls-files ui/settings/node_modules/ | wc -l
git ls-files ui/graph/node_modules/ | wc -l
git ls-files ui/assistant/node_modules/ | wc -l
```

Expected: settings=4914, graph=4657, assistant=0

- [ ] **Step 4: 从 git index 移除（保留本地文件）**

```bash
git rm -r --cached ui/settings/node_modules ui/graph/node_modules
```

注意：用 `--cached` 只从 git index 移除，本地文件系统保留。**风险提示**：Task 1 后到 Task 7 之间**禁止 `git reset --hard` / `git checkout .` / `git clean -fd`**，否则本地 node_modules 会按 index 被删（铁律：禁止 git reset --hard）。

- [ ] **Step 5: 验证 git status**

```bash
git status | head -20
```

Expected: 看到 `deleted: ui/settings/node_modules/...` 和 `deleted: ui/graph/node_modules/...` 大量文件，但本地文件还在（`ls ui/settings/node_modules/` 仍有内容）。

- [ ] **Step 6: 提交清理**

```bash
git commit -m "chore(ui): 从 git 移除 settings/graph 的 node_modules（9571 文件）

.gitignore 规则 ui/*/node_modules/ 之前失效（文件在规则添加前已 tracked）。
用 git rm --cached 只从 index 移除，本地文件保留，规则现在生效。

风险提示：Task 1 后到 Task 7 之间禁止 git reset --hard / git checkout .。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

- [ ] **Step 7: 验证 .gitignore 规则生效**

```bash
git check-ignore -v ui/settings/node_modules/electron
git check-ignore -v ui/graph/node_modules/electron
```

Expected: 输出 `.gitignore:NN:ui/*/node_modules/ ui/settings/node_modules/electron`（规则匹配）

- [ ] **Step 8: 权限检查（铁律 #7）**

```bash
find ui/*/node_modules/.bin/ -type f ! -perm -u+x -exec chmod +x {} \; 2>/dev/null || true
```

`git rm --cached` 不改本地文件权限，但铁律要求"git 操作后修权限"，跑一遍兜底。

---

## Task 2: 创建 ui/main 骨架 + package.json（跨平台）

**目标：** 新建 `ui/main/` 目录，写 `package.json` 合并三个原 package.json 的所有依赖，electron 统一到 ^33.0.0，**用 `cross-env` 解决 Windows npm script 环境变量问题**，预留 `electron-builder` 双平台 build 配置。

**Files:**
- Create: `ui/main/package.json`

- [ ] **Step 1: 临时备份（铁律 #3）**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add -A && git commit -m "backup: 创建 ui/main 骨架前临时备份" || echo "nothing to commit"
```

- [ ] **Step 2: 创建目录**

```bash
mkdir -p ui/main/windows/assistant ui/main/windows/settings ui/main/windows/graph
```

- [ ] **Step 3: 写 ui/main/package.json**

创建 `ui/main/package.json`：

```json
{
  "name": "niu-ui-main",
  "version": "1.0.0",
  "description": "Niu unified Electron UI (assistant + settings + graph)",
  "main": "main.js",
  "scripts": {
    "start": "electron .",
    "start:assistant": "cross-env NIU_WINDOW=assistant electron .",
    "start:settings": "cross-env NIU_WINDOW=settings electron .",
    "start:graph": "cross-env NIU_WINDOW=graph electron .",
    "build": "electron-builder",
    "build:mac": "electron-builder --mac",
    "build:win": "electron-builder --win"
  },
  "keywords": ["electron", "niu", "assistant", "settings", "graph"],
  "author": "",
  "license": "MIT",
  "dependencies": {
    "dompurify": "^3.4.2",
    "eventsource": "^4.1.0",
    "marked": "^18.0.0",
    "@antv/g6": "^5.1.0",
    "force-graph": "^1.51.3"
  },
  "devDependencies": {
    "electron": "^33.0.0",
    "electron-builder": "^25.0.0",
    "cross-env": "^7.0.3"
  },
  "build": {
    "appId": "com.niu.ui",
    "directories": {
      "output": "dist"
    },
    "mac": {
      "target": ["dmg", "zip"],
      "category": "public.app-category.productivity"
    },
    "win": {
      "target": ["nsis"],
      "icon": "windows/assistant/icons/icon.ico"
    },
    "files": [
      "main.js",
      "preload-*.js",
      "windows/**/*"
    ]
  }
}
```

**跨平台说明**：
- `cross-env` 解决 Windows cmd/PowerShell 不认 `NIU_WINDOW=X electron .` 的问题
- `build.mac.target` 和 `build.win.target` 分别配置双平台打包
- `build.files` 显式列出要打进包的文件（不含 node_modules，electron-builder 自动注入 electron 二进制；运行时依赖 dompurify/marked 等由 electron-builder 默认规则打入）

**版本号 pinning 理由**：
- `electron: ^33.0.0`——与原 `ui/settings/package.json` 一致（取三个原目录最高），不升到最新 43.x 避免引入新 API 变更风险
- `electron-builder: ^25.0.0`——与原 `ui/settings/package.json` 一致，不升到 26.x
- `cross-env: ^7.0.3`——稳定版，不升到 10.x（10.x 主要改 ESM 支持，对本场景无收益）

如果未来要升级版本，单独开任务做（不在本计划范围）。

- [ ] **Step 4: 验证 JSON 合法**

```bash
python -c "import json; json.load(open('ui/main/package.json'))" && echo "JSON OK"
```

Expected: `JSON OK`

- [ ] **Step 5: 提交**

```bash
git add ui/main/package.json
git commit -m "feat(ui): 新建 ui/main 骨架，合并三个 package.json 依赖

- electron 统一 ^33.0.0
- cross-env 解决 Windows npm script 环境变量问题
- electron-builder 预留 mac/win 双平台 build 配置
- dompurify/marked/eventsource/@antv/g6/force-graph 合并

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 3: 移动前端资源到 ui/main/windows/

**目标：** 把三个原目录的 HTML/CSS/JS/assets 移动到 `ui/main/windows/{assistant,settings,graph}/`，preload 脚本移动到 `ui/main/` 根目录并重命名（避免歧义）。**注意：`ui/` 根目录的中文名 GIF 不动（不是 spirit.html 引用的）**。

**Files:**
- Move: `ui/assistant/{spirit,chat,sticky}.html` → `ui/main/windows/assistant/`
- Move: `ui/assistant/icons/` → `ui/main/windows/assistant/icons/`
- Move: `ui/assistant/fonts/` → `ui/main/windows/assistant/fonts/`
- Move: `ui/assistant/*.gif`（9 个英文文件名）→ `ui/main/windows/assistant/`
- Move: `ui/assistant/window-config.json` → `ui/main/windows/assistant/`
- Move: `ui/settings/index.html` → `ui/main/windows/settings/`
- Move: `ui/graph/{index.html,renderer.js,styles.css,demo.html,test-api.html}` → `ui/main/windows/graph/`
- Move: `ui/assistant/preload.js` → `ui/main/preload-assistant.js`
- Move: `ui/assistant/preload-chat.js` → `ui/main/preload-chat.js`
- Move: `ui/assistant/preload-sticky.js` → `ui/main/preload-sticky.js`
- Move: `ui/settings/preload.js` → `ui/main/preload-settings.js`
- Move: `ui/graph/preload.js` → `ui/main/preload-graph.js`

- [ ] **Step 1: 临时备份（铁律 #3）**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add -A && git commit -m "backup: 移动前端资源前临时备份" || echo "nothing to commit"
```

- [ ] **Step 2: 清理 icons 目录的脏文件（核对发现 .DS_Store/.mnemo/.ruff_cache）**

```bash
cd REDACTED_USER_PATH/tools/ai-bot/ui
# 清理 Python 工具误生成的缓存和系统脏文件，避免 mv 带到 ui/main/
rm -rf assistant/icons/.mnemo assistant/icons/.ruff_cache assistant/icons/.DS_Store
rm -rf settings/.DS_Store graph/.DS_Store 2>/dev/null || true
ls assistant/icons/  # 应该只剩 9 PNG + 1 ico
```

- [ ] **Step 3: 移动 assistant 前端资源**

```bash
cd REDACTED_USER_PATH/tools/ai-bot/ui

# HTML
mv assistant/spirit.html assistant/chat.html assistant/sticky.html main/windows/assistant/

# GIF（9 个英文文件名，从 ui/assistant/ 移动，不动 ui/ 根目录的中文名 GIF）
mv assistant/alert.gif assistant/alert1.gif assistant/busy.gif assistant/idle1.gif assistant/idle2.gif assistant/sleep.gif assistant/to-busy.gif assistant/to-sleep.gif assistant/wake.gif main/windows/assistant/

# icons 和 fonts
mv assistant/icons main/windows/assistant/
mv assistant/fonts main/windows/assistant/

# window-config.json
mv assistant/window-config.json main/windows/assistant/
```

**注意**：`ui/` 根目录的中文名 GIF（`唤醒.gif`/`忙碌.gif` 等）**不动**——这些不是 spirit.html 引用的，是其他用途。

- [ ] **Step 4: 移动 settings 前端资源**

```bash
cd REDACTED_USER_PATH/tools/ai-bot/ui
mv settings/index.html main/windows/settings/
```

- [ ] **Step 5: 移动 graph 前端资源**

```bash
cd REDACTED_USER_PATH/tools/ai-bot/ui
mv graph/index.html graph/renderer.js graph/styles.css graph/demo.html graph/test-api.html main/windows/graph/
```

**注意**：`graph/index.html` 第 91 行引用 `./node_modules/force-graph/dist/force-graph.min.js`，移动后路径会断（`ui/main/windows/graph/node_modules/` 不存在）。Task 5 Step 2 会改为 `../../node_modules/force-graph/dist/force-graph.min.js`。

- [ ] **Step 6: 移动 preload 脚本（重命名）**

```bash
cd REDACTED_USER_PATH/tools/ai-bot/ui
mv assistant/preload.js main/preload-assistant.js
mv assistant/preload-chat.js main/preload-chat.js
mv assistant/preload-sticky.js main/preload-sticky.js
mv settings/preload.js main/preload-settings.js
mv graph/preload.js main/preload-graph.js
```

- [ ] **Step 7: 验证文件结构**

```bash
find ui/main -type f -not -path "*/node_modules/*" | sort
```

Expected: 看到所有 HTML/CSS/JS/assets/preload 文件都在 `ui/main/` 下正确位置。重点核对：
- `ui/main/windows/assistant/{spirit,chat,sticky}.html` 存在
- `ui/main/windows/assistant/icons/` 有 10 个 PNG/ico 文件（`ls icons/*.png icons/*.ico | wc -l` = 10）
- `ui/main/windows/assistant/fonts/AZhuPaoPaoTi.ttf` 存在
- `ui/main/windows/assistant/*.gif` 有 9 个
- `ui/main/preload-{assistant,chat,sticky,settings,graph}.js` 5 个都存在

- [ ] **Step 8: 提交**

```bash
git add ui/main
git commit -m "refactor(ui): 移动三个窗口前端资源到 ui/main/windows/

- assistant HTML/GIF(9个英文)/icons/fonts/window-config → ui/main/windows/assistant/
- settings index.html → ui/main/windows/settings/
- graph HTML/CSS/JS → ui/main/windows/graph/
- 5 个 preload 移到 ui/main/ 根目录并重命名

ui/ 根目录的中文名 GIF 不动（不是 spirit.html 引用的）。
graph/index.html L91 的 force-graph 引用 Task 5 修。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 4: 写 ui/main/main.js（核心入口）

**目标：** 写 `ui/main/main.js`，按 `process.env.NIU_WINDOW` 分支创建对应 BrowserWindow。合并三个 main.js 的所有 `ipcMain.handle/on`、`app.on`、Tray、Dock 逻辑。**Dock.hide 仅在 assistant 模式调，settings/graph 模式不调（否则窗口不显示）**。

**Files:**
- Create: `ui/main/main.js`

**关键设计：**
- 入口读 `process.env.NIU_WINDOW`（`assistant` / `settings` / `graph`），默认 `assistant`
- `assistant` 模式：创建 spirit + chat + sticky + Tray + SSE 轮询 + Dock.hide（仅 macOS）
- `settings` 模式：只创建 settings 窗口，**不调 Dock.hide**
- `graph` 模式：只创建 graph 窗口，**不调 Dock.hide**
- `open-graph` IPC（assistant 内托盘菜单）改为同进程 `createGraphWindow()`（不再 spawn `niu --graph`）
- `niu --graph` 命令行仍走独立进程（Rust 启动器 spawn 第二个 `niu` + `NIU_WINDOW=graph`）
- `window-all-closed`：assistant 模式空实现保活；settings/graph 模式 `app.quit()`
- IPC handler 命名零冲突，直接合并

- [ ] **Step 1: 临时备份（铁律 #3）**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add -A && git commit -m "backup: 写 ui/main/main.js 前临时备份" || echo "nothing to commit"
```

- [ ] **Step 2: 写 main.js 入口 + 模式分支**

创建 `ui/main/main.js`，开头：

```javascript
const { app, BrowserWindow, ipcMain, screen, Tray, Menu, nativeImage, dialog, shell } = require('electron');
const path = require('path');
const fs = require('fs');
const url = require('url');
const http = require('http');
const { exec, spawn } = require('child_process');

const WINDOW_MODE = process.env.NIU_WINDOW || 'assistant';

// 窗口引用
let spiritWindow = null, chatWindow = null, stickyWindow = null;
let settingsWindow = null, graphWindow = null;
let tray = null;

// 根据 mode 决定是否创建 Tray
function shouldCreateTray() { return WINDOW_MODE === 'assistant'; }
```

**注意**：preload 脚本内 `__dirname` 相对路径（如 `ui/assistant/preload.js` 第 8 行 `path.join(__dirname, '..', '..', 'config', 'user-config.json')`）移到 `ui/main/preload-assistant.js` 后 `__dirname=ui/main`，`../../config` 仍是仓库根 `config/`，**路径不变**。Task 5 会逐一核对。

- [ ] **Step 3: 移植 assistant 的 createSpiritWindow**

从 `ui/assistant/main.js` L59-117 复制 `createSpiritWindow` 函数，调整 preload 路径和 HTML 路径：

```javascript
function createSpiritWindow() {
  const spiritPath = path.join(__dirname, 'windows', 'assistant', 'spirit.html');
  const preloadPath = path.join(__dirname, 'preload-assistant.js');
  spiritWindow = new BrowserWindow({
    width: 96, height: 144,
    frame: false, transparent: true, resizable: false,
    alwaysOnTop: true, hasShadow: false,
    webPreferences: { preload: preloadPath, contextIsolation: true, nodeIntegration: false }
  });
  spiritWindow.loadFile(spiritPath);
  // ... 保留原 L59-117 的所有逻辑（setPosition、setAlwaysOnTop 等）
}
```

**注意：** 完整复制原 L59-117 的所有代码，只改两处：
- `preload: path.join(__dirname, 'preload.js')` → `preload: path.join(__dirname, 'preload-assistant.js')`
- `spiritWindow.loadFile('spirit.html')` → `spiritWindow.loadFile(path.join(__dirname, 'windows', 'assistant', 'spirit.html'))`

GIF 引用路径如果在原 main.js 里是 `path.join(__dirname, 'alert.gif')`（指向 `ui/assistant/alert.gif`），改为 `path.join(__dirname, 'windows', 'assistant', 'alert.gif')`。Task 5 会 grep 全部确认。

- [ ] **Step 4: 移植 createChatWindow**

从 `ui/assistant/main.js` L120-253 复制 `createChatWindow`，调整 preload 和 HTML 路径：

```javascript
function createChatWindow() {
  const chatPath = path.join(__dirname, 'windows', 'assistant', 'chat.html');
  const preloadPath = path.join(__dirname, 'preload-chat.js');
  chatWindow = new BrowserWindow({
    width: 400, height: 500,
    minWidth: 300, minHeight: 400,
    frame: false, transparent: true,
    webPreferences: { preload: preloadPath, contextIsolation: true, nodeIntegration: false }
  });
  chatWindow.loadFile(chatPath);
  // ... 保留原 L120-253 的所有逻辑
}
```

- [ ] **Step 5: 移植 createStickyWindow**

从 `ui/assistant/main.js` L851-939 复制 `createStickyWindow`，调整路径：

```javascript
function createStickyWindow() {
  const stickyPath = path.join(__dirname, 'windows', 'assistant', 'sticky.html');
  const preloadPath = path.join(__dirname, 'preload-sticky.js');
  // ... 保留原 L851-939 的所有逻辑
}
```

- [ ] **Step 6: 移植 createSettingsWindow**

从 `ui/settings/main.js` L13-24 复制，调整路径：

```javascript
function createSettingsWindow() {
  const settingsPath = path.join(__dirname, 'windows', 'settings', 'index.html');
  const preloadPath = path.join(__dirname, 'preload-settings.js');
  settingsWindow = new BrowserWindow({
    width: 500, height: 650,
    resizable: false, frame: false, transparent: true,
    webPreferences: { preload: preloadPath, contextIsolation: true, nodeIntegration: false }
  });
  settingsWindow.loadFile(settingsPath);
}
```

- [ ] **Step 7: 移植 createGraphWindow**

从 `ui/graph/main.js` L35-46 复制，调整路径。**注意：assistant 模式下 Dock 已 hide，graph 窗口需 `graphWindow.show()` 显式激活**：

```javascript
function createGraphWindow() {
  const graphPath = path.join(__dirname, 'windows', 'graph', 'index.html');
  const preloadPath = path.join(__dirname, 'preload-graph.js');
  graphWindow = new BrowserWindow({
    width: 1280, height: 800,
    minWidth: 800, minHeight: 600,
    autoHideMenuBar: true,
    webPreferences: { preload: preloadPath, contextIsolation: true, nodeIntegration: false }
  });
  graphWindow.loadFile(graphPath);
  // assistant 模式下 Dock 已 hide，需显式 show 激活
  graphWindow.show();
}
```

- [ ] **Step 8: 移植 createTray + 改 open-graph 为同进程**

从 `ui/assistant/main.js` L955-1035 复制 `createTray`。**关键改动**：托盘菜单"打开图谱"和 `open-graph` IPC 都改为同进程 `createGraphWindow()`：

```javascript
function createTray() {
  // ... 保留原 L955-1035 的所有逻辑，但 L984-996 的 spawn niu --graph 改为：
  // 菜单项"打开图谱"click: () => createGraphWindow()
}

// open-graph IPC 改为同进程（不再 spawn niu --graph）
ipcMain.on('open-graph', () => {
  if (graphWindow && !graphWindow.isDestroyed()) {
    graphWindow.focus();
    return;
  }
  createGraphWindow();
});
```

**注意**：`exec/spawn` 在合并后仅用于 graph 模式的 `open-path`/`show-item-in-folder`（如果用到），`open-graph` 不再用 spawn。

- [ ] **Step 9: 移植所有 IPC handler**

按原文件顺序，把三个 main.js 的所有 `ipcMain.handle/on` 复制到 `ui/main/main.js`：

- 从 `ui/assistant/main.js` 复制 33 个 IPC handler（L835 起）
- 从 `ui/settings/main.js` 复制 6 个 IPC handler（`get-presets`、`get-config`、`save-config`、`test-connection`、`close-window`、`minimize-window`）
- 从 `ui/graph/main.js` 复制 12 个 IPC handler（`kg-*`、`open-path`、`show-item-in-folder`）

**核对步**：grep 所有 preload 的 `ipcRenderer.send/invoke`，逐一确认 main.js 有对应 handler：

```bash
grep -rn "ipcRenderer\.\(send\|invoke\)" ui/main/preload-*.js | sort
```

记录 orphan IPC（preload 调用但 main.js 未注册的），保留现状不补注册（避免扩大范围）。已知 orphan：`preload-chat.js` 的 `set-chat-position` / `resize-chat-window`。

- [ ] **Step 10: 写 app.whenReady + 模式分支**

```javascript
app.whenReady().then(() => {
  if (WINDOW_MODE === 'assistant') {
    createSpiritWindow();
    createChatWindow();
    createStickyWindow();
    if (shouldCreateTray()) createTray();
    // Dock.hide 仅 macOS 且仅 assistant 模式
    if (process.platform === 'darwin' && app.dock) {
      app.dock.hide();
    }
    // 启动 alerts 轮询 + SSE 轮询
    // 从原 assistant main.js 复制 L1093-1107 (alerts polling)
    // + L1163-1167 (startPendingAlertsPolling setTimeout)
    // + L1174-1263 (SSE)
  } else if (WINDOW_MODE === 'settings') {
    createSettingsWindow();
    // 不调 Dock.hide（否则 settings 窗口不显示）
  } else if (WINDOW_MODE === 'graph') {
    createGraphWindow();
    // 不调 Dock.hide
  } else {
    console.error('Unknown NIU_WINDOW:', WINDOW_MODE);
    app.quit();
  }
});
```

**关键**：Dock.hide 仅在 assistant 模式 + macOS 调。settings/graph 模式调了会导致窗口不显示。

- [ ] **Step 11: 移植 app.on 事件**

```javascript
app.on('window-all-closed', () => {
  // assistant 模式：空实现保活（托盘维持）
  // settings/graph 模式：退出
  if (WINDOW_MODE !== 'assistant') {
    app.quit();
  }
});

app.on('before-quit', (e) => {
  // 从原 assistant main.js L1042-1062 复制：停轮询 + POST /api/shutdown + destroy 全部窗口 + tray
  // 注意：settings/graph 模式不需要 POST /api/shutdown（它们不管理 Python API 生命周期）
  if (WINDOW_MODE === 'assistant') {
    // ... 原 L1042-1062 逻辑
  }
});

app.on('activate', () => {
  // 从原 graph main.js L144-149 复制：macOS reinitialize
  if (WINDOW_MODE === 'graph' && !graphWindow) createGraphWindow();
});
```

**注意**：settings 模式下 electron `app.quit()` 后，npm 父进程不会自动退出（npm 是 electron 的父进程）。Rust 启动器 L1318 `launch_window("settings")` spawn 的是 `npm start`，npm 等待 electron 退出后会自己退出。Rust 的 `try_wait` 检测的是 npm 子进程（不是 electron），npm 退出后 `try_wait` 返回 Some → Rust break → 退出。**需在 Task 8 验证 npm 是否真的随 electron 退出**——如果不退出，Rust 会一直等。

- [ ] **Step 12: 验证 main.js 语法**

```bash
cd REDACTED_USER_PATH/tools/ai-bot/ui/main
node -c main.js && echo "Syntax OK"
```

Expected: `Syntax OK`

- [ ] **Step 13: 静态路径检查（防运行时断链）**

```bash
cd REDACTED_USER_PATH/tools/ai-bot/ui/main
# grep 所有 path.join(__dirname, ...) 和 loadFile(...)
grep -n "path\.join(__dirname\|loadFile(" main.js | head -40
```

逐一核对每个路径目标文件存在于 `ui/main/` 下。如有断链，Task 5 修。

- [ ] **Step 14: 提交**

```bash
git add ui/main/main.js
git commit -m "feat(ui): 写 ui/main/main.js 单一入口

按 NIU_WINDOW 环境变量分支创建对应 BrowserWindow：
- assistant: spirit + chat + sticky + tray + SSE 轮询 + Dock.hide(macOS)
- settings: 只创 settings 窗口（不调 Dock.hide）
- graph: 只创 graph 窗口（不调 Dock.hide）

合并三个 main.js 的所有 IPC handler（零命名冲突）。
open-graph IPC（托盘菜单）改为同进程 createGraphWindow()。
niu --graph 命令行仍走独立进程（Rust spawn 第二个 niu + NIU_WINDOW=graph）。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 5: 修正前端资源的引用路径

**目标：** 三个原 main.js 里的相对路径（preload/HTML/GIF/icons）在移动到 `ui/main/` 后需要调整。**重点修 graph/index.html 第 91 行的 force-graph 引用**（移动后断链）。

**Files:**
- Modify: `ui/main/main.js`（路径修正）
- Modify: `ui/main/windows/graph/index.html`（force-graph 引用路径）

- [ ] **Step 1: 临时备份（铁律 #3）**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add -A && git commit -m "backup: 修正引用路径前临时备份" || echo "nothing to commit"
```

- [ ] **Step 2: 修 graph/index.html 的 force-graph 引用**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
# 当前 L91: <script src="./node_modules/force-graph/dist/force-graph.min.js"></script>
# 移动后路径: ui/main/windows/graph/index.html
# 真安装位置: ui/main/node_modules/force-graph/dist/force-graph.min.js
# 相对路径: ../../node_modules/force-graph/dist/force-graph.min.js
sed -i.bak 's|./node_modules/force-graph|../../node_modules/force-graph|' ui/main/windows/graph/index.html
rm ui/main/windows/graph/index.html.bak
```

验证：

```bash
grep "force-graph" ui/main/windows/graph/index.html
```

Expected: `../../node_modules/force-graph/dist/force-graph.min.js`

- [ ] **Step 3: 检查 ui/main/main.js 里的所有路径引用**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
grep -n "path\.join(__dirname\|loadFile(\|\.gif\|\.png\|\.ttf\|icons/\|fonts/" ui/main/main.js | head -40
```

记录所有引用路径，逐一核对目标文件存在。

- [ ] **Step 4: 检查 HTML 里的资源引用**

```bash
grep -rn "src=\|href=" ui/main/windows/assistant/*.html ui/main/windows/settings/*.html ui/main/windows/graph/*.html | grep -v "node_modules" | head -40
```

- [ ] **Step 5: 修正 ui/main/main.js 里的路径引用**

根据 Step 3-4 的检查结果，逐个修正。常见模式：

- `path.join(__dirname, 'spirit.html')` → `path.join(__dirname, 'windows', 'assistant', 'spirit.html')`
- `path.join(__dirname, 'preload.js')` → `path.join(__dirname, 'preload-assistant.js')`
- `path.join(__dirname, 'alert.gif')` → `path.join(__dirname, 'windows', 'assistant', 'alert.gif')`
- `path.join(__dirname, 'icons', 'icon-16.png')` → `path.join(__dirname, 'windows', 'assistant', 'icons', 'icon-16.png')`
- `path.join(__dirname, 'fonts', 'AZhuPaoPaoTi.ttf')` → `path.join(__dirname, 'windows', 'assistant', 'fonts', 'AZhuPaoPaoTi.ttf')`

**注意**：Tray 函数的 icons 引用有 4 处（核对 assistant/main.js 实际位置）：
- L71（createSpiritWindow 内 `icon-64.png`）
- L140（createChatWindow 内 `icon-64.png`）
- L866（createStickyWindow 内 `icon-32.png`）
- L957（createTray 内 `icon-16.png`）

全部从 `path.join(__dirname, 'icons', 'icon-XX.png')` 改为 `path.join(__dirname, 'windows', 'assistant', 'icons', 'icon-XX.png')`。grep `icon-` 全文搜索确保不漏。

- [ ] **Step 6: 检查 preload 脚本内的 __dirname 相对路径**

```bash
grep -rn "path\.join(__dirname\|require(" ui/main/preload-*.js | head -20
```

preload 脚本内 `__dirname` 从 `ui/assistant/` 变为 `ui/main/`，相对路径 `../../config` 仍是仓库根 `config/`，**路径不变**。但如果 preload 引用了 `../xxx`（指向 `ui/`），需要改为 `../xxx` 仍指向 `ui/`（因为 `ui/main/../` = `ui/`），**路径不变**。逐一核对确认。

- [ ] **Step 7: 验证 main.js 语法**

```bash
cd REDACTED_USER_PATH/tools/ai-bot/ui/main
node -c main.js && echo "Syntax OK"
```

- [ ] **Step 8: 提交**

```bash
git add ui/main
git commit -m "fix(ui): 修正移动后的资源引用路径

- graph/index.html force-graph 引用改为 ../../node_modules/...
- HTML 路径加 windows/{assistant,settings,graph}/ 前缀
- preload 路径用重命名后的名字
- GIF/icons/fonts 路径加 windows/assistant/ 前缀

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 6: 改 Rust 启动器 launch_window

**目标：** 改 `launcher/src/main.rs` 的 `launch_window` 函数，从 spawn `ui/<name>/npm start` 改为 spawn `ui/main/npm start` + 传 `NIU_WINDOW=<name>` 环境变量。Rust `Command::env` 跨平台一致。

**Files:**
- Modify: `launcher/src/main.rs`（L836-868 launch_window 函数 + L1317/L1360 注释/提示）

- [ ] **Step 1: 临时备份（铁律 #3）**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add -A && git commit -m "backup: 改 launch_window 前临时备份" || echo "nothing to commit"
```

- [ ] **Step 2: 读当前 launch_window 完整实现**

```bash
sed -n '836,868p' launcher/src/main.rs
```

记录当前实现的 stdout/stderr 重定向、pipe、错误处理等所有逻辑，准备改造时保留。

- [ ] **Step 3: 改 launch_window 函数**

把 `launcher/src/main.rs` 的 `launch_window` 函数（约 L836-868）改为。**必须保留原函数的 3 行 `Stdio::inherit()`**（stdout/stderr/stdin 继承），否则 electron 子进程输出无法回传到 Rust 启动器日志，端到端验证日志全无：

```rust
fn launch_window(name: &str) -> Result<std::process::Child, Box<dyn std::error::Error>> {
    let exe_path = env::current_exe()?;
    let exe_dir = exe_path.parent().unwrap_or_else(|| {
        eprintln!("Cannot find parent dir of executable");
        std::process::exit(1);
    });
    // 改为固定 ui/main/ 单目录
    let window_dir = exe_dir.join("ui").join("main");

    #[cfg(windows)]
    {
        let mut cmd = Command::new("cmd");
        cmd.args(["/C", "npm", "start"])
            .env("NIU_WINDOW", name)
            .current_dir(&window_dir);
        // 必须保留：stdout/stderr/stdin 继承到 Rust 启动器日志
        cmd.stdout(std::process::Stdio::inherit());
        cmd.stderr(std::process::Stdio::inherit());
        cmd.stdin(std::process::Stdio::inherit());
        let child = cmd.spawn()?;
        Ok(child)
    }

    #[cfg(not(windows))]
    {
        let mut cmd = Command::new("npm");
        cmd.arg("start")
            .env("NIU_WINDOW", name)
            .current_dir(&window_dir);
        // 必须保留：stdout/stderr/stdin 继承到 Rust 启动器日志
        cmd.stdout(std::process::Stdio::inherit());
        cmd.stderr(std::process::Stdio::inherit());
        cmd.stdin(std::process::Stdio::inherit());
        let child = cmd.spawn()?;
        Ok(child)
    }
}
```

**注意：** 只改两点：
1. `let window_dir = exe_dir.join("ui").join(name);` → `let window_dir = exe_dir.join("ui").join("main");`
2. 在两个分支的 `cmd` builder 里加 `.env("NIU_WINDOW", name)`

3 行 `Stdio::inherit()` 必须原样保留，不要删。

- [ ] **Step 4: 改 L1317 注释和 L1360 错误提示**

```bash
# L1317 注释 "npm start in ui/settings/" 改为 "npm start in ui/main/ with NIU_WINDOW=settings"
sed -i.bak 's|// 启动 settings 窗口（npm start in ui/settings/）|// 启动 settings 窗口（npm start in ui/main/ with NIU_WINDOW=settings）|' launcher/src/main.rs

# L1360 错误提示 "cd ui/assistant && npm start" 改为 "cd ui/main && NIU_WINDOW=assistant npm start"
sed -i.bak 's|cd ui/assistant && npm start|cd ui/main \&\& NIU_WINDOW=assistant npm start|' launcher/src/main.rs

rm launcher/src/main.rs.bak
```

验证：

```bash
grep -n "ui/assistant\|ui/settings\|ui/graph" launcher/src/main.rs
```

Expected: 无旧路径引用（除了 `let window_dir = exe_dir.join("ui").join("main")` 这种新路径）。

- [ ] **Step 5: 检查三个调用点不需要改**

```bash
grep -n "launch_window" launcher/src/main.rs
```

Expected: 三处调用 `launch_window("assistant")`、`launch_window("settings")`、`launch_window("graph")` 都不需要改。

- [ ] **Step 6: 核对 Rust settings 失败路径（L1312-1350）**

```bash
sed -n '1312,1350p' launcher/src/main.rs
```

确认逻辑：settings electron 退出 → npm 子进程退出 → `try_wait` 返回 Some → break → `cancelled.store(true) + notify_shutdown` → 进程退出。**注意 npm 是否随 electron 退出**——npm start 启动 electron，electron 退出后 npm 默认会自己退出（npm 把 electron 当前台进程）。Task 8 验证。

- [ ] **Step 7: 编译验证（铁律 #8）**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
./launcher/build.sh
```

**禁止用 `cargo build`**。编译必须通过，有 warning 也修掉。

- [ ] **Step 8: 权限修复（铁律 #7）**

```bash
find python/bin/ -type f -exec grep -l '^#!' {} \; | xargs chmod +x 2>/dev/null || true
find ui/*/node_modules/.bin/ -type f ! -perm -u+x -exec chmod +x {} \; 2>/dev/null || true
```

- [ ] **Step 9: 提交**

```bash
git add launcher/src/main.rs
git commit -m "refactor(launcher): launch_window 改为单目录 ui/main + NIU_WINDOW 环境变量

不再 spawn 三个独立 npm start，改为 spawn ui/main/npm start 传 NIU_WINDOW=assistant|settings|graph。
Rust Command::env 跨平台一致，Windows cmd 子进程继承环境变量。
L1317 注释和 L1360 错误提示同步更新。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 7: 清理旧目录 + 修复 .gitignore

**目标：** 删除三个原目录的 main.js/preload/package.json（前端资源已移动到 `ui/main/`），**先备份 assistant/node_modules**（Task 8 npm install 失败时回退），清理 settings/graph 的 node_modules。

**Files:**
- Delete: `ui/assistant/{main.js, preload*.js, package.json, package-lock.json}`
- Delete: `ui/settings/{main.js, preload.js, package.json, package-lock.json}`
- Delete: `ui/graph/{main.js, preload.js, package.json, package-lock.json}`
- Backup + Delete: `ui/assistant/node_modules/`（先 mv 备份，Task 8 验证通过后删）
- Delete: `ui/settings/node_modules/`、`ui/graph/node_modules/`（本地文件系统）

- [ ] **Step 1: 临时备份（铁律 #3）**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add -A && git commit -m "backup: 清理旧目录前临时备份" || echo "nothing to commit"
```

- [ ] **Step 2: 确认前端资源已移动**

```bash
ls ui/assistant/ ui/settings/ ui/graph/ 2>/dev/null
```

Expected: 三个目录里应该只剩 `node_modules/` 和（可能残留的）`package.json`/`main.js`/`preload*.js`。HTML/assets/preload 应该都不在了（已移到 `ui/main/`）。

- [ ] **Step 3: 删除三个原目录的 main.js/preload/package.json**

```bash
cd REDACTED_USER_PATH/tools/ai-bot/ui
rm -f assistant/main.js assistant/preload.js assistant/preload-chat.js assistant/preload-sticky.js assistant/package.json assistant/package-lock.json
rm -f settings/main.js settings/preload.js settings/package.json settings/package-lock.json
rm -f graph/main.js graph/preload.js graph/package.json graph/package-lock.json
```

**注意：** 这一步会删除文件，已得到用户同意（用户骂"有病吧"要求合并）。删除前确认 `ui/main/` 下已有对应文件（Task 3 已移动）。

- [ ] **Step 4: 备份 assistant/node_modules（fallback 用）**

```bash
cd REDACTED_USER_PATH/tools/ai-bot/ui
# 先 mv 备份，Task 8 npm install 成功后再删
mv assistant/node_modules assistant_node_modules_backup
```

**为什么备份**：assistant 的 node_modules 当前是 macOS 版且能跑。如果 Task 8 `npm install` 失败（网络/registry），用户**完全无法启动**——三个目录全空。备份后可以 `mv assistant_node_modules_backup assistant/node_modules` 回退。

- [ ] **Step 5: 删除 settings/graph 的 node_modules（本地文件系统）**

```bash
cd REDACTED_USER_PATH/tools/ai-bot/ui
rm -rf settings/node_modules graph/node_modules
```

settings/graph 的 node_modules 是 Windows 版（跑不起来），且 Task 1 已从 git index 移除，直接删本地。

- [ ] **Step 6: 安全删除空的三个原目录（避免扫雷式删除）**

```bash
cd REDACTED_USER_PATH/tools/ai-bot/ui
# 先列出残留文件，人工核对确认都是可删的（.DS_Store 等）
echo "=== assistant 残留 ==="
find assistant -maxdepth 1 -type f 2>/dev/null
echo "=== settings 残留 ==="
find settings -maxdepth 1 -type f 2>/dev/null
echo "=== graph 残留 ==="
find graph -maxdepth 1 -type f 2>/dev/null
```

**人工核对**：残留文件应该只有 `.DS_Store` 之类系统脏文件。如果看到任何 `.html`/`.js`/`.json`/`.gif`/`.png` 等前端资源，说明 Task 3 漏移了——**停下来回 Task 3 补移**，不要直接删。

确认只有脏文件后，逐个清理：

```bash
cd REDACTED_USER_PATH/tools/ai-bot/ui
find assistant settings graph -maxdepth 1 -name ".DS_Store" -delete 2>/dev/null || true
rmdir assistant settings graph 2>/dev/null || ls -la assistant settings graph
```

如果 rmdir 失败（目录非空），说明有未清理的文件，`ls -la` 查看后人工决定。

- [ ] **Step 7: 确认 .gitignore 规则覆盖 ui/main/node_modules/**

```bash
grep -n "node_modules" .gitignore
git check-ignore -v ui/main/node_modules/electron 2>/dev/null || echo "ui/main/node_modules not exist yet, will be ignored by ui/*/node_modules/ rule"
```

Expected: 规则 `ui/*/node_modules/` 已覆盖 `ui/main/node_modules/`，不需要新增。

- [ ] **Step 8: 提交清理**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add -A
git commit -m "chore(ui): 删除三个原目录，所有 UI 资源已合并到 ui/main/

- 删除 ui/assistant/、ui/settings/、ui/graph/ 三个原目录
- assistant/node_modules 先 mv 备份为 ui/assistant_node_modules_backup（Task 8 验证通过后删）
- settings/graph 的 node_modules 直接删本地（Windows 版跑不起来）

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 8: 安装 ui/main 依赖 + 端到端验证 + 全局路径更新

**目标：** 在 `ui/main/` 下 `npm install`（macOS 装 macOS 版 electron），端到端验证三个窗口都能起来。**先配真实可用 LLM**（铁律 #5）。**全局更新 208 处旧路径引用**。

**Files:**
- Create: `ui/main/node_modules/`（npm install 生成，不提交 git）
- Modify: 全局 208 处文档/代码引用 `ui/assistant|ui/settings|ui/graph` → `ui/main`

- [ ] **Step 1: 临时备份（铁律 #3）**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add -A && git commit -m "backup: 安装依赖+验证+全局路径更新前临时备份" || echo "nothing to commit"
```

- [ ] **Step 2: 配真实可用 LLM（铁律 #5）+ 核对端点存在**

先核对 `/api/test-llm` 端点确实存在（避免假设错误）：

```bash
grep -rn "test-llm\|test_llm" niu_api/ 2>/dev/null | head -3
```

Expected: 至少看到 `niu_api/compat.py:NNN:@router.post("/api/test-llm")`。如果没找到，说明端点不存在或路径变了——停下来重新核对端点路径。

在 `config/user-config.json` 配一个真实可用的 LLM（如本地 ollama、真实 OpenAI API key、或其他可用模型）。**不要用 ark-code-latest**（已确认不存在）。

```bash
cat config/user-config.json | python -c "import json,sys; d=json.load(sys.stdin); print('model:', d.get('model', 'N/A'))"
```

确认配置的 LLM 真的能调通（用 `/api/test-llm` 端点测）：

```bash
curl -X POST http://127.0.0.1:9876/api/test-llm -H "Content-Type: application/json" -d '{}' 2>/dev/null | python -m json.tool
```

如果 `success: true` 才能继续。如果 false，先配好再往下走。如果 Python API 没启动（端口 9876 不通），先手动 `python -m niu_api &` 起来再测，测完再 kill。

- [ ] **Step 3: 安装依赖**

```bash
cd REDACTED_USER_PATH/tools/ai-bot/ui/main
npm install
```

Expected: 安装成功，`node_modules/electron/dist/Electron.app` 存在（macOS 版）。

- [ ] **Step 4: 验证 electron 二进制是 macOS 版**

```bash
ls ui/main/node_modules/electron/dist/
```

Expected: 看到 `Electron.app`（不是 `electron.exe`）。

- [ ] **Step 5: 修复文件权限（铁律 #7）**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
find python/bin/ -type f -exec grep -l '^#!' {} \; | xargs chmod +x 2>/dev/null || true
find ui/*/node_modules/.bin/ -type f ! -perm -u+x -exec chmod +x {} \; 2>/dev/null || true
```

- [ ] **Step 6: 全局更新旧路径引用（208 处）**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
# 全局 grep 找出所有引用
grep -rln "ui/assistant\|ui/settings\|ui/graph" --include="*.md" --include="*.rs" --include="*.py" --include="*.yaml" --include="*.json" --include="*.js" --include="*.html" . 2>/dev/null | grep -v node_modules | grep -v ".git/" > /tmp/old_path_refs.txt
wc -l /tmp/old_path_refs.txt
```

逐文件检查引用上下文，决定怎么改：
- **文档（.md）**：`ui/assistant/main.js` → `ui/main/main.js`；`ui/assistant/spirit.html` → `ui/main/windows/assistant/spirit.html`；`ui/settings/index.html` → `ui/main/windows/settings/index.html`；`ui/graph/index.html` → `ui/main/windows/graph/index.html`
- **Rust（.rs）**：Task 6 已改 L1317/L1360，其他引用按文档规则改
- **Python（.py）**：如果有引用，按文档规则改
- **配置（.yaml/.json）**：`config/mcp-servers.yaml` 或其他配置，按文档规则改

**注意**：历史文档（`docs/plans/2026-04-19-*.md` 等已归档计划）可以不改（它们记录的是当时的状态），但要加 deprecated 注释或不动。

逐文件改完后验证：

```bash
grep -rln "ui/assistant\|ui/settings\|ui/graph" --include="*.md" --include="*.rs" --include="*.py" --include="*.yaml" --include="*.json" --include="*.js" --include="*.html" . 2>/dev/null | grep -v node_modules | grep -v ".git/" | grep -v "docs/plans/202[0-9]" | wc -l
```

Expected: 0（除历史归档计划外，全部更新）

- [ ] **Step 7: 端到端验证 — assistant 模式**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
./niu
```

Expected:
- splash 启动 → Python API ready → LLM 测试通过（Step 2 已配真实 LLM）→ splash 关闭
- spirit 窗口（小女孩）出现
- chat 窗口（对话页面）出现
- 托盘菜单出现

监控日志：

```bash
tail -f logs/launcher_stdout.log
```

应该看到 `NIU_WINDOW=assistant` 模式启动，无错误。

- [ ] **Step 8: 端到端验证 — settings 模式（LLM 失败路径）**

改坏 `config/user-config.json`（如清空 apiKey），重启 `./niu`：

```bash
./niu
```

Expected:
- splash 启动 → LLM 检测失败 → 启 settings 窗口（不再是 Permission denied）
- 主 UI（assistant）不启动
- settings 窗口起来 → 用户改好配置 + 点测试 → 测试通过 → 程序退出

**验证 npm 随 electron 退出**：settings electron 退出后，Rust 日志应该出现 `Settings window closed (exit_status=...)` 然后 `LLM settings flow complete, exiting process for user restart`。如果 Rust 卡住不退出，说明 npm 没随 electron 退出，需要在 `launch_window` 的 npm 命令上加 `--kill-others-on-fail` 或改用 `electron .` 直接 spawn（跳过 npm 中间层）。

- [ ] **Step 9: 端到端验证 — graph 模式（托盘菜单 + 命令行）**

**托盘菜单路径**（同进程 createGraphWindow）：在 assistant 托盘菜单点"打开图谱"，graph 窗口应在同进程内起来（1280x800）。

**命令行路径**（独立进程）：

```bash
./niu --graph
```

Expected: 第二个 `niu` 进程启动，spawn `ui/main/npm start` with `NIU_WINDOW=graph`，graph 窗口起来。第一个 assistant 进程不受影响。

- [ ] **Step 10: 验证 graph/index.html 的 force-graph 引用没断**

在 graph 窗口打开后，按 F12 看 console，确认 `force-graph.min.js` 加载成功（无 404）。

- [ ] **Step 11: 删除 assistant/node_modules 备份（或回退）**

Task 7 Step 4 的 `ui/assistant_node_modules_backup`，在 Task 8 Step 7-10 全部验证通过后删除：

```bash
cd REDACTED_USER_PATH/tools/ai-bot/ui
rm -rf assistant_node_modules_backup
```

**只有 Step 7-10 全部通过才删**。任何一步失败，按以下映射回退：

| 失败的 Step | 失败原因 | 回退动作 |
|------------|---------|---------|
| Step 7（assistant 起不来） | main.js 逻辑错/路径断 | `git log --oneline -10` 找到 Task 4 的 commit SHA，`git checkout <Task4-commit>~1 -- ui/main/main.js` 恢复 Task 4 之前的 main.js，回 Task 4 重做。node_modules 备份保留，`mv assistant_node_modules_backup assistant/node_modules` 恢复旧 electron（macOS 版能跑） |
| Step 8（settings 起不来/卡死） | npm 不随 electron 退出 | `git checkout <Task4-commit>~1 -- ui/main/main.js` + `git checkout <Task6-commit>~1 -- launcher/src/main.rs`，回 Task 4/6 查 settings 模式 `app.quit()` 逻辑。备份保留 |
| Step 9（graph 起不来） | graph/index.html 路径断/force-graph 404 | `git checkout <Task5-commit>~1 -- ui/main/windows/graph/index.html`，回 Task 5 Step 2 核对 `../../node_modules/force-graph/...` 路径。备份保留 |
| Step 10（F12 看 force-graph 404） | 同 Step 9 | 同上 |

**注意**：
- **禁止 `git reset --hard`**（铁律，会删本地 node_modules）
- **不要用 `git revert HEAD`**——Step 11 执行时 HEAD 是 Task 8 Step 1 的空 backup commit，revert HEAD 撤不掉 Task 4/5/6 的实际改动
- 用 `git checkout <commit>~1 -- <specific paths>` 精确恢复指定文件到某个 Task 之前的状态
- 用 `git log --oneline -10` 定位每个 Task 的 commit SHA（每个 Task Step 1 都有 backup commit，Step 最后有正式 commit）

回退后报告失败，等用户决定下一步。

- [ ] **Step 12: 提交最终状态**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add -A
git commit -m "test(ui): 端到端验证三窗口在 ui/main 单一应用下正常启动

- assistant 模式：spirit + chat + sticky + tray 正常
- settings 模式：LLM 失败时弹 settings 窗口正常
- graph 模式：托盘菜单（同进程）+ niu --graph（独立进程）都能起
- graph/index.html force-graph 引用没断（F12 验证）

全局更新 208 处旧路径引用为 ui/main。
ui/main/node_modules/ 不提交（.gitignore 规则生效）。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

- [ ] **Step 13: 更新 CLAUDE.md（如有需要）**

```bash
grep -n "ui/assistant\|ui/settings\|ui/graph" CLAUDE.md
```

如果有引用，更新为 `ui/main`。Step 6 已包含，但单独核对一遍。

---

## Self-Review

### 1. Spec coverage 检查

- ✅ 三套 node_modules 共 1.5GB → Task 1 (git 清理) + Task 7 (本地清理)
- ✅ settings/graph 装错 platform → Task 8 (npm install 在 macOS 装 macOS 版)
- ✅ node_modules 被强制 add 进 git → Task 1 (git rm --cached)
- ✅ assistant → graph 跨进程通信复杂 → Task 4 Step 8 (open-graph 托盘菜单改同进程)
- ✅ graph/index.html force-graph 引用断链 → Task 5 Step 2 (改 ../../node_modules/)
- ✅ 大量文档/代码引用旧路径 → Task 8 Step 6 (全局 grep 更新 208 处)
- ✅ 跨平台 Windows 兼容 → Task 2 (cross-env) + Task 6 (Rust Command::env 跨平台)
- ✅ 重构期间禁启动 → Context 节 + Task 1 Step 4 风险提示
- ✅ Task 7 删 node_modules 无 fallback → Task 7 Step 4 (mv 备份)
- ✅ Dock.hide 与 graph 窗口冲突 → Task 4 Step 7 (graphWindow.show()) + Step 10 (Dock.hide 仅 assistant)
- ✅ 分发策略 → Context 节"分发策略"
- ✅ IPC handler orphan 核对 → Task 4 Step 9 (grep preload ipcRenderer)
- ✅ Rust settings 失败路径核对 → Task 6 Step 6 (核对 L1312-1350)
- ✅ 铁律 #3 每 Task 备份 → 每个 Task Step 1
- ✅ 铁律 #7 git 操作后修权限 → Task 1 Step 8 + Task 6 Step 8 + Task 8 Step 5
- ✅ 铁律 #8 Rust 编译用 build.sh → Task 6 Step 7
- ✅ 铁律 #5 真实 LLM → Task 8 Step 2 (先配真实可用 LLM)

### 2. Placeholder 检查

- 无 "TBD"、"TODO"、"implement later"
- Task 4 Step 3-9 的 main.js 移植步骤是"指针式指令"（从原文件 LXX-YY 复制 + 调整路径），不是占位符。执行者需要读原文件复制代码。
- Task 5 Step 5 的路径修正依赖 Step 3-4 的 grep 结果，是条件性的，不是占位符。
- Task 8 Step 6 的全局路径更新依赖 grep 结果，是条件性的，不是占位符。

### 3. Type consistency 检查

- `NIU_WINDOW` 环境变量名全程一致
- `createSpiritWindow` / `createChatWindow` / `createStickyWindow` / `createSettingsWindow` / `createGraphWindow` / `createTray` 函数名一致
- preload 文件名 `preload-assistant.js` / `preload-chat.js` / `preload-sticky.js` / `preload-settings.js` / `preload-graph.js` 一致
- `ui/main/windows/{assistant,settings,graph}/` 目录结构一致
- `WINDOW_MODE` 变量名一致

### 4. 跨平台检查

- ✅ npm script 用 `cross-env`（Task 2）
- ✅ 路径全用 `path.join`（Task 4/5）
- ✅ Rust `Command::env` 跨平台一致（Task 6）
- ✅ electron-builder 双平台 build 配置（Task 2 package.json）
- ✅ Dock.hide 仅 macOS + 仅 assistant 模式（Task 4 Step 10）
- ⚠️ Windows 上 `cmd /C npm start` 透传 env 给 npm → electron 需 Task 8 验证（macOS 测不了 Windows，计划已注明）
- ⚠️ Windows 上 npm 是否随 electron 退出需 Task 8 验证

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-07-ui-electron-consolidation.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
