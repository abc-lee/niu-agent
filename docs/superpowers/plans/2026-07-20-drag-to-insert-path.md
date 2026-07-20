# 拖入文件转文字插入对话框 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **审查历史**：
> - v1：第一轮审查发现 3 个阻断问题（C1: Electron 33 废弃 `File.path` / C2: `files` null 防御 / L2: 主 Agent 触发 file-processor 依赖未验证）+ 4 个中等问题（M4/M5/M6）+ 1 个轻微（L1 gitnexus）。v2 已全部修复。
> - v2：第二轮审查发现 2 个 High 问题（H1: grep 命令把 `user-input` 写成 `userInput` / H2: 主 Agent 委托验证缺真实测试文件和通过标准）+ 5 个 Medium/Low 优化项（M1/M2/M3/L1/L2）。v3 已全部修复。
> - v3：第三轮审查发现 1 个 High 问题（H1: 含空格路径与用户补充文字的解析歧义）+ 4 个 Medium/Low（M1 空文件测试标准 / M2 空格+换行边界 / L1 可选链防御 / L2 Expected 表述）。v4 已全部修复。
> - v4：第四轮审查发现 1 个 Medium（M1: Task 1 Step 5 Expected 与 v4 多行格式矛盾）+ 2 个 Low（L1 gitnexus detect_changes 兜底说明 / L2 Task 4 注释分支描述）。v5 已全部修复。
> - v5：第五轮审查无 Critical/High 阻断问题，2 个 Medium 建议项（M1 纯路径无补充文字场景 / M2 isProcessing 期间拖入场景）。v6 已全部修复。
> - v6：第六轮审查无 Critical/High 阻断问题，连续两轮通过，可交付。2 个非阻断优化项（M1 约束传递 / L1 isProcessing Enter 盲点）已在 v7 修复。

**Goal:** 把主对话框拖入文件的行为从"立即触发入库"改为"插入纯绝对路径到输入框，等用户补充文字后回车发送"。

**Architecture:** 改两个文件——
1. `ui/main/preload-chat.js`：新增 `getFilePath` IPC 暴露（参照 `preload-assistant.js:50-58`，使用 `webUtils.getPathForFile`，绕开 Electron 33 废弃的 `File.path`）。
2. `ui/main/windows/assistant/chat.html`：重写 drop 事件处理器，把所有拖入文件的绝对路径用 `", "` 拼成一行纯路径文本，通过新增的辅助函数 `insertTextToInput()` 插入到 `userInput` 输入框，并聚焦+光标末尾。删除 `handleDroppedImage` 和 `handleDroppedFile` 两个旧函数及它们对 `processImage` / `addMessage` / `showTyping` / `notifyBusy` / `getImageUrl` / `openWithSystemViewer` 的调用。

精灵窗口 `spirit.html` 不动。后端不动。Agent 提示词不动（主 Agent 通过 MCP schema 调用子 Agent，用户补充"入库"等描述时主 Agent 自动委托 file-processor——前置验证步骤见 Task 1）。

**Tech Stack:** HTML + 原生 JavaScript（Electron 渲染进程）+ DataTransfer API + `webUtils.getPathForFile` + HTMLInputElement API。

**Spec:** `docs/superpowers/specs/2026-07-20-drag-to-insert-path-design.md`

---

## 文件结构

| 文件 | 责任 | 改动 |
|------|------|------|
| `ui/main/preload-chat.js` | 主对话框的 preload 脚本，暴露 IPC | 修改：新增 `getFilePath` 暴露 |
| `ui/main/windows/assistant/chat.html` | 主对话框页面：消息渲染、消息发送、拖入处理 | 修改：重写 drop handler，新增 `insertTextToInput()`，删除两个旧 handler |

不改动的相关文件（仅作上下文参考）：
- `ui/main/preload-assistant.js` — 精灵窗口 preload，已有 `getFilePath` 样板（L50-58），不动
- `ui/main/main.js` — `ipcMain.handle('process-image', ...)` 保留（精灵窗口可能复用，未来可清理）
- `ui/main/windows/assistant/spirit.html` — 精灵窗口拖入逻辑，完全不动
- `config/agents/niu.md` / `config/agents/file-processor.md` — Agent 提示词不动
- `niu_api/compat.py` — 后端 `/api/chat/session` 路由不动

---

## Task 1：前置依赖验证 + 备份

**目的**：验证"主 Agent 能从纯路径 + 用户补充文字的消息触发 file-processor 委托"这一关键假设；记录 chat.html 和 preload-chat.js 当前 commit hash 备份回滚。

**Files:**
- 修改：无（仅 git 操作 + 真实程序验证）

- [ ] **Step 1: 确认工作区干净**

Run: `git status`
Expected: `nothing to commit, working tree clean`

**如果工作区不干净**：让用户先处理或确认这些改动与本任务无关后再继续。**不要自动 stash**——用户可能正在调试其他东西。按 MEMORY "No Empty Backup Commits"，工作区干净时不做 empty commit 备份。

- [ ] **Step 2: 记录当前 chat.html 和 preload-chat.js 的 commit hash**

Run:
```bash
git log -1 --format='%H' ui/main/windows/assistant/chat.html
git log -1 --format='%H' ui/main/preload-chat.js
```
Expected: 输出两个 40 字符的 commit hash，**记下来**用于回滚参考（不要用 `HEAD~1` 回滚，因为期间可能有其他 commit）。

- [ ] **Step 3: 用 gitnexus 分析影响范围**

按 CLAUDE.md 铁律 4 "修改前必须用 gitnexus 分析影响范围"，对要删的两个函数跑 impact 分析：

```
gitnexus_impact({target: "handleDroppedImage", direction: "upstream"})
gitnexus_impact({target: "handleDroppedFile", direction: "upstream"})
```

Expected: 返回的 d=1（直接调用者）应该只有 `chat.html` 的 drop handler。如果有其他调用点，必须停下来分析，可能不能删。

**如果 gitnexus 返回空**：gitnexus 主要索引 Python/Rust/TS，HTML 内嵌 JS 可能未索引，返回空不等于安全。改用 grep 兜底验证：
```bash
grep -rn "handleDroppedImage\|handleDroppedFile" ui/main/
```
Expected: 只在 `chat.html` 的 drop handler（L1540-1568）和函数定义本身（L1571-1664）出现，无其他文件引用。如果 grep 发现其他文件调用，必须停下来分析。

如果 gitnexus 索引过期（提示 stale），先跑 `npx gitnexus analyze` 更新。

- [ ] **Step 4: 启动程序验证主 Agent 委托能力**

按 CLAUDE.md 铁律 5 "测试必须用真实数据+真实 LLM"：

Run: `./niu`（不能用 `cargo build`，已编译好的二进制）

**必须用含空格的真实路径测试**（v4 修正 v3 H1）——这是关键验证点：

```bash
# 用 macOS 系统自带图片（路径含空格，一定能复现路径边界歧义问题）
ls /System/Library/Desktop\ Pictures/*.jpg | head -1
```

操作：在主对话框直接手动输入一条测试消息，**必须用含空格路径**，例如：
```
/System/Library/Desktop Pictures/Sonoma Horizon.jpg 请入库
```

**通过标准**：
- ✅ 通过：后端日志（`logs/raw_http/<当天>/`）中最新一次 LLM 交互的 tool_call 字段包含 `chat-with-file-processor`，证明主 Agent 正确委托子 Agent 处理（即使路径含空格）。
- ❌ 失败：主 Agent 没委托而是自己尝试处理（如自己调用 `ingest` 工具），或回复"无法处理"等，或把"Desktop Pictures"当成两个东西。
- 失败时**必须停下来和用户重新讨论方案**——可能需要在 `config/agents/niu.md` 加规则或调整设计（如路径加引号定界符），不能继续后续 Task。

**注意**：v4 计划已通过 `insertTextToInput` 自动补换行让路径独占一行，所以实际拖入时路径和用户补充文字会分行，不会出现上述歧义。本步手动输入测试是模拟最坏情况（用户自己手打路径和补充文字在一行），确认主 Agent 即使面对含空格的路径也能识别。如果手动输入测试失败但拖入测试通过，方案仍可执行——因为拖入路径会自动补换行。

**额外子测试（v6 修正 v5 M1）**：再发一条**纯路径**消息（无任何补充文字），如：
```
/System/Library/Desktop Pictures/Sonoma Horizon.jpg
```
观察主 Agent 行为：
- 如果主 Agent 仍委托 file-processor，说明纯路径也能触发入库——用户拖入后直接回车也能工作。
- 如果主 Agent 不委托（如回复"你想要我做什么？"），说明**用户拖入后必须补充文字才能入库**——这是可接受的设计约束，但需要在 Task 8 测试用例中明确：用户拖入后必须补充文字（如"请入库"）才能触发入库，直接回车发送纯路径不会入库。

验证完成后**优雅杀进程**（按 CLAUDE.md 铁律 + MEMORY "Test Process Kill Corruption"）：
```bash
ps aux | grep -E "niu|launcher" | grep -v grep
# 找到 PID 后用 kill -TERM 优雅退出，禁止 pkill -f niu
kill -TERM <pid>
```

- [ ] **Step 5: 检查后端日志确认消息格式**

Run:
```bash
ls logs/raw_http/ | tail -1
# 拿到当天目录，找最新一次 LLM 交互的 request.json
ls logs/raw_http/<当天目录>/ | tail -3
```

Expected: 用户消息含路径和补充文字（手动输入测试时是一行 `/path/to/test.jpg 请入库`，实际拖入时是多行 `/path/to/test.jpg\n请入库`），主 Agent 工具调用日志中有 `chat-with-file-processor`。

---

## Task 2：阅读 chat.html 现有 drop 相关代码 + preload-chat.js

**Files:**
- 阅读：`ui/main/windows/assistant/chat.html:1519-1664`
- 阅读：`ui/main/preload-chat.js`（全文，仅 79 行）
- 阅读：`ui/main/preload-assistant.js:50-58`（参考 `getFilePath` 样板）

- [ ] **Step 1: 阅读拖入视觉态切换逻辑（chat.html L1519-1538）**

确认 `dragover` / `dragleave` / `dragenter` 事件监听器和 `.drag-over` class 切换逻辑位置。**这部分不动**，改造后保留。同时确认 L1521-1526 的 `['dragenter','dragover','dragleave','drop'].forEach` 已对 drop 调 `preventDefault()` 和 `stopPropagation()`，所以新 drop handler 不需要再调 `e.preventDefault()`。

- [ ] **Step 2: 阅读现有 drop handler（chat.html L1540-1568）**

确认现有 handler 把 `files` 分成 `imageFiles` / `otherFiles` 两组，分别调 `handleDroppedImage` / `handleDroppedFile`。这段要整体重写。

- [ ] **Step 3: 阅读 handleDroppedImage（chat.html L1571-1630）**

确认现有逻辑：取 `file.path` → 创建预览 div → `getImageUrl` → `addMessage('user', null)` → `showTyping` / `notifyBusy` → `processImage(filePath)`。整段删除。

- [ ] **Step 4: 阅读 handleDroppedFile（chat.html L1633-1664）**

确认现有逻辑：取 `file.path` → `addMessage('user', '📄 ' + file.name)` → `showTyping` / `notifyBusy` → `processImage(filePath)`。整段删除。

- [ ] **Step 5: 确认 userInput 元素引用位置和类型**

HTML 中元素 id 是 `user-input`（带连字符），JS 中变量名是 `userInput`（驼峰）。

Run: `grep -n 'id="user-input"' ui/main/windows/assistant/chat.html`
Expected: 看到 `<textarea id="user-input" ...>`（约 L556）——确认是 textarea，`setSelectionRange` 可用。

Run: `grep -n "userInput" ui/main/windows/assistant/chat.html | head -20`
Expected:
- 看到 `const userInput = document.getElementById('user-input')` 引用（id 字符串带连字符，变量名是驼峰）
- 看到 `userInput.addEventListener('input', ...)` 监听器（用于自适应高度）
- 看到 `userInput.addEventListener('keydown', ...)` 监听器（Enter 发送）

- [ ] **Step 6: 阅读现有 input 事件监听器**

Run: `grep -n "userInput.addEventListener('input'" ui/main/windows/assistant/chat.html`
Expected: 找到监听器位置。记录这个监听器做什么（通常是自适应高度）。`insertTextToInput` 中的 `dispatchEvent(new Event('input'))` 会触发它，让 textarea 高度自动调整。

- [ ] **Step 7: 阅读 preload-chat.js 确认缺少 getFilePath**

Read 整个 `ui/main/preload-chat.js`。
Expected: 确认 `contextBridge.exposeInMainWorld('electronAPI', {...})` 内**没有** `getFilePath`。这是 v1 审查发现的 C1 问题的根因——Electron 33 已废弃 `File.path`，必须用 `webUtils.getPathForFile`。

- [ ] **Step 8: 阅读 preload-assistant.js L50-58 作为 getFilePath 样板**

Read `ui/main/preload-assistant.js:1` 和 `:50-58`。
Expected:
- L1: `const { contextBridge, ipcRenderer, webUtils } = require('electron');` —— 需要在 preload-chat.js 顶部也 `require` `webUtils`
- L50-58: 完整的 `getFilePath` 实现，直接复用

---

## Task 3：在 preload-chat.js 新增 getFilePath 暴露

**Files:**
- 修改：`ui/main/preload-chat.js:1` 和 `:46` 附近

**目的**：修复 v1 审查 C1 问题——Electron 33 废弃 `File.path`，必须用 `webUtils.getPathForFile`。

- [ ] **Step 1: 修改 preload-chat.js L1 的 require 语句**

把原 L1：
```javascript
const { contextBridge, ipcRenderer } = require('electron');
```

改为：
```javascript
const { contextBridge, ipcRenderer, webUtils } = require('electron');
```

- [ ] **Step 2: 在 preload-chat.js 的 exposeInMainWorld 对象内新增 getFilePath**

在 `openWithSystemViewer` 行之后（原 L47 后）或 `processImage` 行之后（原 L44 后）插入。建议放在 `processImage` 后、`openWithSystemViewer` 前，逻辑相近：

```javascript
  // 获取 File 对象的真实路径（Electron 33 后 file.path 被废弃，必须用 webUtils.getPathForFile）
  getFilePath: (file) => {
    try {
      return webUtils.getPathForFile(file);
    } catch (e) {
      console.error('[preload-chat] getFilePath failed:', e.message);
      return '';
    }
  },
```

- [ ] **Step 3: 验证 preload-chat.js 已加入 getFilePath**

Run: `grep -n "getFilePath\|webUtils" ui/main/preload-chat.js`
Expected:
- L1: `const { contextBridge, ipcRenderer, webUtils } = require('electron');`
- 新增的 `getFilePath:` 定义行
- `webUtils.getPathForFile(file)` 调用行

- [ ] **Step 4: 暂不提交 preload-chat.js，等 Task 4-7 完成后一起提交**

preload-chat.js 的改动和 chat.html 的改动作为同一个功能一起提交。

---

## Task 4：新增 insertTextToInput 辅助函数到 chat.html

**Files:**
- 修改：`ui/main/windows/assistant/chat.html`（在 `sendMessage` 函数附近或 `userInput` 引用处之后插入）

- [ ] **Step 1: 找到合适的插入位置**

Run: `grep -n "async function sendMessage\|function sendMessage\|const userInput" ui/main/windows/assistant/chat.html`
Expected: 找到 `sendMessage` 函数定义行号或 `userInput` 引用行号，把新函数插在这些代码附近（同属消息发送区域，作用域一致）

- [ ] **Step 2: 在 chat.html 中插入 insertTextToInput 函数**

在 `sendMessage` 函数定义之前或之后（同一 `<script>` 作用域内），插入：

```javascript
// 把文本插入到输入框末尾，自动补换行让路径独占一行：
// - 已有内容为空或末尾是换行 → 直接追加路径
// - 已有内容末尾不是换行 → 补换行再追加路径
// - 路径末尾自动补换行，光标停在新行行首，用户补充文字在新行输入
// 这样主 Agent 能识别"行首是绝对路径"，避免路径含空格时与用户补充文字混淆
// 最后聚焦+触发 input 事件让 textarea 自适应高度
function insertTextToInput(text) {
  const current = userInput.value;
  if (current && !current.endsWith('\n')) {
    userInput.value = current + '\n' + text + '\n';
  } else {
    userInput.value = current + text + '\n';
  }
  userInput.focus();
  // 光标定位到末尾（新行行首）
  userInput.setSelectionRange(userInput.value.length, userInput.value.length);
  userInput.dispatchEvent(new Event('input', { bubbles: true }));
}
```

**关键变更说明**（v4 修正 v3 H1）：
- 路径末尾自动补换行，让用户补充文字在新行输入
- 光标停在新行行首，用户直接打字
- 主 Agent 收到的消息格式：路径独占一行，用户补充文字在下一行，避免路径含空格时的解析歧义

- [ ] **Step 3: 验证函数已加入**

Run: `grep -n "insertTextToInput" ui/main/windows/assistant/chat.html`
Expected: 至少看到函数定义那一行

---

## Task 5：重写 drop 事件处理器

**Files:**
- 修改：`ui/main/windows/assistant/chat.html:1540-1568`

- [ ] **Step 1: 用新 handler 替换 L1540-1568 整段**

把原 L1540-1568 的：

```javascript
messages.addEventListener('drop', async (e) => {
  messages.classList.remove('drag-over');
  
  const files = e.dataTransfer.files;
  if (files.length === 0) return;
  
  // 分离图片和文件
  const imageFiles = [];
  const otherFiles = [];
  Array.from(files).forEach(f => {
    const isImageByMime = f.type.startsWith('image/');
    const isImageByExt = /\.(jpg|jpeg|png|gif|bmp|webp|tiff?|heic|heif)$/i.test(f.name || f.path || '');
    if (isImageByMime || isImageByExt) {
      imageFiles.push(f);
    } else {
      otherFiles.push(f);
    }
  });
  
  // 处理图片
  for (const file of imageFiles) {
    await handleDroppedImage(file);
  }
  
  // 处理其他文件
  for (const file of otherFiles) {
    await handleDroppedFile(file);
  }
});
```

替换为：

```javascript
messages.addEventListener('drop', (e) => {
  messages.classList.remove('drag-over');

  // e.dataTransfer.files 在某些场景（拖入纯文本/URL）可能为 null，需防御
  const files = e.dataTransfer && e.dataTransfer.files;
  if (!files || files.length === 0) return;

  // Electron 33 废弃 File.path，必须用 webUtils.getPathForFile 经 preload 暴露的 getFilePath
  // 可选链防御：如果 preload 未加载（window.electronAPI 未定义），不会抛 TypeError 而是 map 出 undefined
  const paths = Array.from(files)
    .map(f => window.electronAPI?.getFilePath?.(f))
    .filter(Boolean);

  if (paths.length === 0) {
    // 有文件但拿不到路径——通常是 preload 未生效或 webUtils 异常，便于排查
    console.warn('[Chat] drop: 未获取到任何文件路径，可能 preload 未生效或 webUtils 异常');
    return;
  }

  insertTextToInput(paths.join(', '));
});
```

**关键变更说明**（对比 v1 计划）：
1. **去掉 `async`**：handler 内无 await，去掉避免误导（v1 审查 M4）
2. **`files` null 防御**：`e.dataTransfer && e.dataTransfer.files` + `!files || files.length === 0`（v1 审查 C2）
3. **改用 `getFilePath`**：`window.electronAPI.getFilePath(f)` 替代 `f.path`（v1 审查 C1）
4. **`paths.length === 0` 加 console.warn**：有文件但拿不到路径时给反馈，便于排查 preload 异常（v2 审查 M2）
5. **可选链防御**：`window.electronAPI?.getFilePath?.(f)`，preload 未加载时不抛 TypeError（v3 审查 L1）

- [ ] **Step 2: 验证新 handler 已就位**

Run: `grep -n "messages.addEventListener('drop'" ui/main/windows/assistant/chat.html`
Expected: 只有一行，且后面紧跟 `messages.classList.remove('drag-over');`

- [ ] **Step 3: 验证旧的分类逻辑已删除**

Run: `grep -n "imageFiles\|otherFiles\|handleDroppedImage\|handleDroppedFile" ui/main/windows/assistant/chat.html | head -10`
Expected: 在 drop handler 区域不应再有这些变量。后续 Task 6 删除函数定义后应全部消失。

---

## Task 6：删除 handleDroppedImage 和 handleDroppedFile 函数

**Files:**
- 修改：`ui/main/windows/assistant/chat.html:1571-1630`（删除 `handleDroppedImage`）和 `L1633-1664`（删除 `handleDroppedFile`）

- [ ] **Step 1: 删除 handleDroppedImage 整个函数**

删除从 `// 处理拖入的图片` 注释行到 `handleDroppedImage` 函数闭合 `}` 的整段（原 L1570-1630）。

具体起点：`// 处理拖入的图片` 注释行（约 L1570）
具体终点：`handleDroppedImage` 函数最后的 `}`（约 L1630）

- [ ] **Step 2: 删除 handleDroppedFile 整个函数**

删除从 `// 处理拖入的文件（非图片）` 注释行到 `handleDroppedFile` 函数闭合 `}` 的整段（原 L1632-1664）。

具体起点：`// 处理拖入的文件（非图片）` 注释行（约 L1632）
具体终点：`handleDroppedFile` 函数最后的 `}`（约 L1664）

- [ ] **Step 3: 验证两个函数已删除**

Run: `grep -n "handleDroppedImage\|handleDroppedFile" ui/main/windows/assistant/chat.html`
Expected: 无输出（两个函数完全删除）

- [ ] **Step 4: 验证没有遗留对旧函数的引用**

Run: `grep -n "processImage\|getImageUrl\|showTyping\|notifyBusy" ui/main/windows/assistant/chat.html | head -20`

Expected（v1 审查 C3 修复——明确说明残留调用是合理的）：
- `showTyping` / `notifyBusy` 在 sendMessage 流程（约 L890-898）仍被调用——**正常**，这是发送消息时的状态指示，与 drop 无关。
- `processImage` / `getImageUrl` 在 chat.html 内**删除两个函数后应无调用点**。若 grep 后仍有残留，说明 Task 6 Step 1-2 的删除不彻底，需要回去检查。IPC 暴露保留在 preload-chat.js 但前端不再用——这是预期行为，未来可清理。
- `openWithSystemViewer` 在 chat.html 内可能仍有调用（如双击图片消息打开系统查看器）——**正常**，保留。

逐一确认剩余调用点都是合理的，不是 drop 流程遗留。

**⚠️ 不要删 main.js 的 `ipcMain.handle('process-image', ...)`**（约 L895-934）。IPC 链保留以减少改动面——精灵窗口或未来功能可能复用。仅前端 chat.html 不再调用，main.js 的 handler 和 preload-chat.js 的 `processImage` 暴露都保留。

---

## Task 7：语法检查 + 启动验证

**Files:**
- 检查：`ui/main/windows/assistant/chat.html` + `ui/main/preload-chat.js`

- [ ] **Step 1: 检查 chat.html 的 JS 语法**

由于 chat.html 是 HTML 内嵌 `<script>`，无独立 linter。用 node 做基本语法检查：

Run: `node -e "const fs = require('fs'); const html = fs.readFileSync('ui/main/windows/assistant/chat.html', 'utf8'); const m = html.match(/<script>([\s\S]*?)<\/script>/g); if (!m) { console.error('no script found'); process.exit(1); } m.forEach((s, i) => { const code = s.replace(/^<script>/, '').replace(/<\/script>$/, ''); try { new Function(code); console.log('script #' + i + ' syntax OK'); } catch (e) { console.error('script #' + i + ' syntax error:', e.message); process.exit(1); } });"`
Expected: 输出 `script #0 syntax OK` 等若干行，无 `syntax error`

如果有语法错误，根据报错行号（注意：node 报的行号是相对 script 内部的）定位修复。

- [ ] **Step 2: 检查 preload-chat.js 的 JS 语法**

Run: `node --check ui/main/preload-chat.js`
Expected: 无输出（node --check 通过时静默）。如果有错误会输出 `SyntaxError`。

- [ ] **Step 3: 启动程序验证**

Run: `./niu`
Expected: 程序正常启动，主对话框能打开

**注意**：不要用 `cargo build`，必须用 `./niu`（已编译好的二进制）。如果改了 Rust 代码才需要 `launcher/build.sh`，本次没改 Rust，直接 `./niu` 即可。

- [ ] **Step 4: 程序启动后无控制台报错**

在主对话框打开后，检查控制台（开发者工具）是否有 JS 报错。
Expected: 无报错。常见的潜在报错：
- `insertTextToInput is not defined`：Task 4 函数插入位置作用域不对——需要把函数移到更外层
- `window.electronAPI.getFilePath is not a function`：Task 3 preload 改动未生效，检查 preload-chat.js 是否保存、是否 require 了 webUtils
- `Cannot read property 'files' of null`：Task 5 的 null 防御失效，检查 `e.dataTransfer && e.dataTransfer.files` 写法

---

## Task 8：手动功能验证

**Files:**
- 无代码改动，纯功能测试

- [ ] **Step 1: 拖入单张图片**

操作：从 Finder 拖一张 `.jpg` 到主对话框
Expected: 输入框出现 `/absolute/path/to/file.jpg\n`（纯路径 + 末尾换行），输入框聚焦，光标在第二行行首

- [ ] **Step 2: 拖入单个文档**

操作：从 Finder 拖一个 `.pdf` 到主对话框
Expected: 输入框出现 `/absolute/path/to/file.pdf\n`

- [ ] **Step 3: 拖入多文件**

操作：从 Finder 选中 3 个文件（任意类型混合）拖入
Expected: 输入框出现 `/path1, /path2, /path3\n`（同一行多路径 + 末尾换行）

- [ ] **Step 4: 拖入到已有内容**

操作：在输入框先输入 `帮我处理`，然后拖入一个文件
Expected: 输入框变为 `帮我处理\n/absolute/path/to/file\n`（已有非换行内容补换行，路径末尾再补换行，光标在第三行行首）

- [ ] **Step 5: 拖入到末尾带换行的内容**

操作：在输入框输入 `帮我处理` 然后按 Shift+Enter（末尾是换行），然后拖入一个文件
Expected: 输入框变为 `帮我处理\n/absolute/path/to/file\n`（已有末尾换行则直接追加路径+换行）

- [ ] **Step 6: 多次拖入叠加**

操作：第一次拖入一个文件（路径为 `/p1`），第二次再拖入另一个文件
Expected: 输入框变为 `/p1\n/p2\n`（每次拖入路径独占一行，末尾换行让下次拖入直接追加）

- [ ] **Step 7: 拖入到只含换行的输入框（v2 审查 M1 边界测试）**

操作：在输入框按 Shift+Enter 输入一个空换行（`current === '\n'`），然后拖入一个文件
Expected: 输入框变为 `\n/p1\n`（已有末尾换行，直接追加路径+换行）

- [ ] **Step 8: 拖入到末尾是空格的内容（v4 边界测试）**

操作：在输入框输入 `帮我处理 ` （末尾带空格），然后拖入一个文件
Expected: 输入框变为 `帮我处理 \n/absolute/path/to/file\n`（末尾是空格不是换行，走"补换行"分支，结果是空格+换行+路径+换行——空格保留是用户主动输入的）

- [ ] **Step 9: 拖入含空格路径的文件 + 补充文字后回车发送（v4 关键测试）**

操作：从 Finder 拖一个路径含空格的文件（如 `/System/Library/Desktop Pictures/Sonoma Horizon.jpg`），然后在路径后输入"请入库"（在新行输入），按回车
Expected: 消息成功发送，主 Agent 收到：
```
/System/Library/Desktop Pictures/Sonoma Horizon.jpg
请入库
```
主 Agent 委托 file-processor 子 Agent 处理入库。**这是 v3 H1 修复后的关键验证**——路径独占一行，用户补充文字在下一行，主 Agent 能正确识别路径边界。

**如果 Task 1 Step 4 子测试发现纯路径不触发入库**（即主 Agent 不委托 file-processor 处理纯路径），本 Step 必须验证"用户补充文字（如'请入库'）是触发入库的必要条件"——拖入后不补充文字直接回车不会入库，用户必须在路径后补充描述。

- [ ] **Step 10: 精灵窗口未受影响**

操作：打开精灵窗口，拖入一个文件
Expected: 精灵窗口仍走原 `send-to-agent` 路径，立即触发入库流程（精灵窗口行为不变）

- [ ] **Step 11: 检查后端日志确认消息格式**

Run:
```bash
ls logs/raw_http/ | tail -1
# 拿到当天目录，找最新一次 LLM 交互的 request.json
ls logs/raw_http/<当天目录>/ | tail -3
```

打开最新 `*_request.json`，检查 user message 内容。
Expected: 用户消息为多行格式（路径独占一行 + 用户补充文字在下一行），**不含** `入库照片：` / `入库文件：` 前缀。如：
```
/System/Library/Desktop Pictures/Sonoma Horizon.jpg
请入库
```

- [ ] **Step 12: 拖入文件夹（v1 审查 M5 补充测试）**

操作：从 Finder 拖一个**文件夹**到主对话框
Expected: 输入框出现文件夹的绝对路径（如 `REDACTED_USER_PATH/Pictures/某文件夹\n`）。后续由用户决定如何处理——主 Agent 收到文件夹路径后行为由 file-processor 决定（可能触发目录入库，也可能询问用户）。

- [ ] **Step 13: 拖入纯文本/URL（v1 审查 M5 补充测试）**

操作：从浏览器地址栏拖一个 URL 到主对话框
Expected: 输入框无反应（`e.dataTransfer.files` 为空或 null，新 handler 的 null 防御 + `files.length === 0 return` 生效）。不应抛 TypeError。

- [ ] **Step 14: 拖入时按住修饰键（v1 审查 M5 补充测试）**

操作：按住 Shift 或 Ctrl 拖入一个文件到主对话框
Expected: 与不按修饰键行为一致——输入框插入纯路径+末尾换行。chat.html 新 handler 不区分修饰键模式（与精灵窗口 spirit.html 区分 copy/move/reference 模式的行为不同，但这是设计意图——主对话框改造后简化为纯插入路径）。

- [ ] **Step 15: 拖入时主对话框正在处理消息（v6 修正 v5 M2 边界测试）**

操作：发送一条需要较长时间处理的消息（如"帮我整理一下最近的笔记"），在主 Agent 处理期间（isProcessing=true、sendBtn 已 disabled）拖入一个文件到主对话框
Expected: drop handler 仍会执行 `insertTextToInput` 把路径插入到输入框（这是合理行为——用户可以在等待回复时继续输入下一句话）。但此时 sendBtn 已 disabled，用户按 Enter 调 sendMessage 时，sendMessage 应该按现有逻辑处理（如果 isProcessing 期间允许发送则发送，如果阻止则不发送）——这与本次改造无关，是 chat.html 现有 sendMessage 的行为。本测试只验证 drop handler 在 isProcessing 期间不会崩溃、不会错误触发 IPC、不会破坏 isProcessing 状态机。

**注意（v7 修正 v6 L1）**：keydown 监听器不检查 isProcessing 也不检查 sendBtn.disabled，Enter 时直接调用 sendMessage()——这是既有行为，不在本次改造范围内。本次只改 drop handler，不改 keydown 监听器。如果 isProcessing 期间按 Enter 重复发送消息是已知 bug，应单独立项处理，不在本次拖入改造范围内。

---

## Task 9：提交 + 清理

**Files:**
- 修改：`ui/main/windows/assistant/chat.html` + `ui/main/preload-chat.js`

- [ ] **Step 1: 查看改动**

Run:
```bash
git diff ui/main/windows/assistant/chat.html
git diff ui/main/preload-chat.js
```
Expected:
- `chat.html`：删除两个旧函数（约 90 行），新增一个辅助函数（约 12 行），重写一个 drop handler（约 14 行，原 28 行）
- `preload-chat.js`：L1 加 `webUtils`，新增 `getFilePath` 函数（约 8 行）

- [ ] **Step 2: 用 gitnexus 检测改动范围**

按 CLAUDE.md "MUST run gitnexus_detect_changes() before committing"：

```
gitnexus_detect_changes({scope: "unstaged"})
```

Expected: 改动符号仅限 `chat.html` 和 `preload-chat.js` 内的预期函数。如果检测到其他文件符号受影响，停下来分析。

**如果 gitnexus 未索引 JS 文件返回空**：依赖 Step 1 的 `git diff` 结果验证范围即可。gitnexus 主要索引 Python/Rust/TS，HTML 内嵌 JS 和 preload JS 可能未索引，返回空不等于安全。

- [ ] **Step 3: 添加并提交**

```bash
git add ui/main/windows/assistant/chat.html ui/main/preload-chat.js
git commit -m "feat(chat): 拖入文件改为插入路径到输入框

- preload-chat.js: 新增 getFilePath 暴露（用 webUtils.getPathForFile 绕开 Electron 33 废弃的 File.path）
- chat.html: 重写 drop 事件处理器，收集所有文件绝对路径用 ', ' 拼成一行
- chat.html: 新增 insertTextToInput 辅助函数（插入文本+聚焦+光标末尾+触发 input 事件让 textarea 自适应高度）
- chat.html: 删除 handleDroppedImage / handleDroppedFile 两个函数
- 精灵窗口 spirit.html 不动，后端 /api/chat/session 不动，Agent 提示词不动
- 用户拖入后需补充描述（如\"请入库\"）再回车发送，主 Agent 通过 MCP schema 调用 file-processor

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

- [ ] **Step 4: 验证提交成功**

Run: `git log -1 --oneline`
Expected: 看到刚才的 commit，message 以 `feat(chat):` 开头，影响文件包含 `chat.html` 和 `preload-chat.js`

- [ ] **Step 5: 杀进程清理**

测试完必须彻底杀所有 niu 进程（按 CLAUDE.md 铁律 + MEMORY "Test Process Kill Corruption" / "Kill Processes After Test"）：

```bash
ps aux | grep -E "niu|launcher" | grep -v grep
```

找到 PID 后用 `kill -TERM <pid>` 优雅退出，**禁止 `pkill -f niu`**（曾损坏 LightRAG vdb 文件）。
Expected: 无残留进程

---

## 回滚方案

如果验证失败需要回退：

```bash
# 推荐方案：创建反向 commit，保留历史
git revert HEAD

# 或者：仅恢复两个改动文件到 Task 1 Step 2 记录的 hash（不用 HEAD~1，避免期间有其他 commit）
git checkout <Task1Step2记录的chat.html的hash> -- ui/main/windows/assistant/chat.html
git checkout <Task1Step2记录的preload-chat.js的hash> -- ui/main/preload-chat.js
```

回滚前先确认未提交改动内容（MEMORY "Git Checkout Safety"），混合改动用 Edit 精确回退而非 checkout。

**禁止 `git reset --hard` 和 `git push --force`**（曾导致有效代码全部丢失，MEMORY "Git Reset Strictly Prohibited"）。

---

## Self-Review

### Spec coverage 检查

- spec 二.1 格式：纯绝对路径无前缀 → Task 5 新 drop handler 用 `paths.join(', ')`，不加前缀 ✓
- spec 二.2 多文件逗号+空格 → Task 5 `paths.join(', ')` ✓
- spec 二.3 混合拖入一行 → Task 5 不再分流 imageFiles/otherFiles，所有路径统一收集 ✓
- spec 二.4 光标末尾+聚焦 → Task 4 `insertTextToInput` 中 `focus()` + `setSelectionRange` ✓
- spec 二.5 不发送不触发 IPC → Task 5 新 handler 只调 `insertTextToInput`，无 IPC 调用 ✓
- spec 二.6 精灵窗口不动 → Task 8 Step 8 验证 ✓
- spec 二.7 不改渲染 → 整个计划无 `addMessage` / `encodeLocalPaths` 改动 ✓
- spec 二.8 不改 Agent 提示词 → 整个计划无 `config/agents/` 改动 ✓

### v1 审查问题修复检查

- **C1（Electron 33 废弃 File.path）**：Task 3 新增 `getFilePath` 暴露，Task 5 改用 `window.electronAPI.getFilePath(f)` ✓
- **C2（files null 防御）**：Task 5 加 `e.dataTransfer && e.dataTransfer.files` + `!files || files.length === 0` ✓
- **C3（grep 残留调用误导）**：Task 6 Step 4 明确说明 `showTyping`/`notifyBusy` 在 sendMessage 流程仍调用是正常的 ✓
- **M1（多次拖入边界）**：Task 4 注释说明三种情况（空/已有非空格非换行/已有末尾空格或换行），Task 8 Step 6 加多次拖入叠加测试 ✓
- **M2（dispatchEvent input 目的）**：Task 4 注释明确"让 textarea 自适应高度"，Task 2 Step 6 验证 input 监听器存在 ✓
- **M3（userInput 类型）**：Task 2 Step 5 加 grep 确认是 textarea 还是 input ✓
- **M4（多余 async）**：Task 5 新 handler 去掉 `async` ✓
- **M5（测试用例缺失）**：Task 8 补 Step 10 文件夹、Step 11 URL、Step 12 修饰键 ✓
- **M6（回滚 HEAD~1 不安全）**：回滚方案改用 Task 1 Step 2 记录的 hash ✓
- **L1（未用 gitnexus）**：Task 1 Step 3 加 impact 分析 + Task 9 Step 2 加 detect_changes ✓
- **L2（主 Agent 委托能力未验证）**：Task 1 Step 4 加前置真实程序验证 ✓
- **L3（注释误导）**：Task 5 注释改为"通过 webUtils.getPathForFile 经 preload 暴露的 getFilePath" ✓

### v2 审查问题修复检查

- **H1（grep 命令把 `user-input` 写成 `userInput`）**：Task 2 Step 5 修正 grep 命令为 `grep -n 'id="user-input"'`，Expected 改为 `<textarea id="user-input">` ✓
- **H2（主 Agent 委托验证缺真实测试文件和通过标准）**：Task 1 Step 4 改用 macOS 系统自带图片或 `/tmp/niu-drag-test.txt`，明确通过标准是日志中出现 `chat-with-file-processor` 工具调用 ✓
- **M1（多次拖入只含换行边界）**：Task 8 补 Step 7 测试"拖入到只含换行的输入框"，后续步骤顺延（v3 又加了 Step 8 Step 9，现在总共 Step 1-14）✓
- **M2（drop handler 静默退出难排查）**：Task 5 Step 1 在 `paths.length === 0` 分支加 `console.warn` 反馈 ✓
- **M3（grep 残留可能误导删 main.js）**：Task 6 Step 4 显式标注"不要删 main.js 的 `ipcMain.handle('process-image')`" ✓
- **L1（gitnexus 是否索引 HTML 内嵌 JS）**：Task 1 Step 3 加 grep 兜底验证路径，明确"返回空不等于安全" ✓
- **L2（工作区不干净缺指引）**：Task 1 Step 1 明确"不要自动 stash，让用户先处理或确认" ✓

### v3 审查问题修复检查

- **H1（含空格路径与用户补充文字的解析歧义）**：`insertTextToInput` 改为路径末尾自动补换行，让路径独占一行；用户补充文字在新行输入；主 Agent 收到"路径行 + 文字行"格式，能正确识别路径边界。Task 1 Step 4 强制用含空格路径测试。Task 8 补 Step 9 关键测试用例 ✓
- **M1（空文件测试通过标准细化）**：Task 1 Step 4 移除 `/tmp/niu-drag-test.txt` 空文件选项，统一用 macOS 系统图片（确定有内容）测试 ✓
- **M2（空格+换行边界测试）**：Task 8 补 Step 8 测试"拖入到末尾是空格的内容"（空格不是换行，走补换行分支）✓
- **L1（可选链防御）**：Task 5 drop handler 用 `window.electronAPI?.getFilePath?.(f)` 替代 `window.electronAPI.getFilePath(f)`，preload 未加载时不抛 TypeError ✓
- **L2（Expected 表述）**：Task 6 Step 4 Expected 改为"删除两个函数后应无调用点。若仍有残留，说明删除不彻底"✓

### v4 审查问题修复检查

- **M1（Task 1 Step 5 Expected 与 v4 多行格式矛盾）**：Step 5 Expected 改为"用户消息含路径和补充文字（手动输入测试时是一行，实际拖入时是多行）"，与 v4 路径末尾补换行设计一致 ✓
- **L1（Task 9 Step 2 gitnexus detect_changes 兜底说明）**：Step 2 加"如果 gitnexus 未索引 JS 文件返回空，依赖 Step 1 的 git diff 结果验证范围即可" ✓
- **L2（Task 4 Step 2 注释分支描述不完整）**：注释改为"已有内容为空或末尾是换行 → 直接追加路径"+"已有内容末尾不是换行 → 补换行再追加路径"，与代码分支对应 ✓

### v5 审查问题修复检查

- **M1（Task 1 Step 4 纯路径无补充文字场景未验证）**：Step 4 加额外子测试——发纯路径消息（无任何补充文字）观察主 Agent 行为。如果主 Agent 仍委托 file-processor，说明纯路径也能触发入库；如果不委托，说明用户必须补充文字，需在 Task 8 明确此约束 ✓
- **M2（Task 8 缺 isProcessing 期间拖入测试）**：Step 15 加测试用例——主 Agent 处理期间拖入文件，验证 drop handler 不崩溃、不误触发 IPC、不破坏 isProcessing 状态机 ✓

### v6 审查问题修复检查

- **M1（v5→v6 约束未传递到 Task 8）**：Task 8 Step 9 Expected 末尾加"如果 Task 1 Step 4 子测试发现纯路径不触发入库，本 Step 必须验证补充文字是必要的"，约束传递到位 ✓
- **L1（isProcessing 期间 Enter 键盲点说明）**：Task 8 Step 15 Expected 加"用户按 Enter 时 sendMessage 会被调用（既有行为，不在本次改造范围内）"，明确这是既有 bug 不在本次范围 ✓

### Placeholder 扫描

无 TBD / TODO / "implement later" / "add error handling" 等占位符。所有代码步骤都给了完整代码。

### Type consistency 检查

- `insertTextToInput(text)` 函数名在 Task 4 定义、Task 5 调用，一致 ✓
- `getFilePath(file)` 函数名在 Task 3 定义、Task 5 调用，与 `preload-assistant.js:51` 样板一致 ✓
- `webUtils.getPathForFile(file)` API 名在 Task 3、Task 5 使用，与 `preload-assistant.js:53` 一致 ✓
- `userInput` 变量名在 Task 4、Task 5、Task 8 中使用，与 chat.html 现有定义一致（Task 2 Step 5 验证） ✓
- `paths` 变量名在 Task 5 中定义和使用，一致 ✓

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-20-drag-to-insert-path.md`. Two execution options:

**1. Subagent-Driven (recommended)** - 我每个 Task 派一个新子 Agent，任务间我做 review，迭代快

**2. Inline Execution** - 在当前会话里直接执行，批量执行 + 关键节点 checkpoint

哪个？
