# 拖入文件转文字插入对话框 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把主对话框拖入文件的行为从"立即触发入库"改为"插入纯绝对路径到输入框，等用户补充文字后回车发送"。

**Architecture:** 只改 `ui/main/windows/assistant/chat.html` 一个文件——重写 drop 事件处理器，把所有拖入文件的绝对路径用 `", "` 拼成一行纯路径文本，通过新增的辅助函数 `insertTextToInput()` 插入到 `userInput` 输入框，并聚焦+光标末尾。删除 `handleDroppedImage` 和 `handleDroppedFile` 两个旧函数及它们对 `processImage` / `addMessage` / `showTyping` / `notifyBusy` / `getImageUrl` / `openWithSystemViewer` 的调用。精灵窗口 `spirit.html` 不动。后端不动。Agent 提示词不动（主 Agent 通过 MCP schema 调用子 Agent，用户补充"入库"等描述时主 Agent 自动委托 file-processor）。

**Tech Stack:** HTML + 原生 JavaScript（Electron 渲染进程）+ DataTransfer API + HTMLInputElement API。

**Spec:** `docs/superpowers/specs/2026-07-20-drag-to-insert-path-design.md`

---

## 文件结构

| 文件 | 责任 | 改动 |
|------|------|------|
| `ui/main/windows/assistant/chat.html` | 主对话框页面：消息渲染、消息发送、拖入处理 | 修改：重写 drop handler，新增 `insertTextToInput()`，删除两个旧 handler |

不改动的相关文件（仅作上下文参考）：
- `ui/main/preload-chat.js` — `processImage` 等 IPC 暴露保留（精灵窗口可能复用，未来可清理）
- `ui/main/main.js` — `ipcMain.handle('process-image', ...)` 保留
- `ui/main/windows/assistant/spirit.html` — 精灵窗口拖入逻辑，完全不动
- `config/agents/niu.md` / `config/agents/file-processor.md` — Agent 提示词不动
- `niu_api/compat.py` — 后端 `/api/chat/session` 路由不动

---

## Task 1：备份当前 chat.html

**Files:**
- 修改：无（仅 git 操作）

- [ ] **Step 1: 确认工作区干净**

Run: `git status`
Expected: `nothing to commit, working tree clean`（如果有未提交改动，先和用户确认）

- [ ] **Step 2: 记录当前 chat.html 的 commit hash**

Run: `git log -1 --format='%H' ui/main/windows/assistant/chat.html`
Expected: 输出一个 40 字符的 commit hash，记下来用于回滚参考

---

## Task 2：阅读 chat.html 现有 drop 相关代码

**Files:**
- 阅读：`ui/main/windows/assistant/chat.html:1519-1664`

- [ ] **Step 1: 阅读拖入视觉态切换逻辑（L1519-1538）**

确认 `dragover` / `dragleave` / `dragenter` 事件监听器和 `.drag-over` class 切换逻辑位置。**这部分不动**，改造后保留。

- [ ] **Step 2: 阅读现有 drop handler（L1540-1568）**

确认现有 handler 把 `files` 分成 `imageFiles` / `otherFiles` 两组，分别调 `handleDroppedImage` / `handleDroppedFile`。这段要整体重写。

- [ ] **Step 3: 阅读 handleDroppedImage（L1571-1630）**

确认现有逻辑：取 `file.path` → 创建预览 div → `getImageUrl` → `addMessage('user', null)` → `showTyping` / `notifyBusy` → `processImage(filePath)`。整段删除。

- [ ] **Step 4: 阅读 handleDroppedFile（L1633-1664）**

确认现有逻辑：取 `file.path` → `addMessage('user', '📄 ' + file.name)` → `showTyping` / `notifyBusy` → `processImage(filePath)`。整段删除。

- [ ] **Step 5: 确认 userInput 元素引用位置**

在 chat.html 顶部脚本部分找到 `const userInput = document.getElementById('userInput')` 或类似语句，记录行号。后续 `insertTextToInput` 函数依赖这个引用。

Run: `grep -n "userInput" ui/main/windows/assistant/chat.html | head -20`
Expected: 看到 userInput 的引用、事件监听、`sendMessage` 等相关行号

---

## Task 3：新增 insertTextToInput 辅助函数

**Files:**
- 修改：`ui/main/windows/assistant/chat.html`（在 `sendMessage` 函数附近或 `userInput` 引用处之后插入）

- [ ] **Step 1: 找到合适的插入位置**

Run: `grep -n "async function sendMessage\|function sendMessage\|const userInput" ui/main/windows/assistant/chat.html`
Expected: 找到 `sendMessage` 函数定义行号或 `userInput` 引用行号，把新函数插在这些代码附近（同属消息发送区域）

- [ ] **Step 2: 在 chat.html 中插入 insertTextToInput 函数**

在 `sendMessage` 函数定义之前或之后（同一作用域），插入：

```javascript
// 把文本插入到输入框末尾，已有内容时补换行；末尾是空格则直接追加；最后聚焦+光标末尾+触发 input 事件
function insertTextToInput(text) {
  const current = userInput.value;
  if (current && !current.endsWith('\n') && !current.endsWith(' ')) {
    userInput.value = current + '\n' + text;
  } else {
    userInput.value = current + text;
  }
  userInput.focus();
  userInput.setSelectionRange(userInput.value.length, userInput.value.length);
  userInput.dispatchEvent(new Event('input', { bubbles: true }));
}
```

- [ ] **Step 3: 验证函数已加入**

Run: `grep -n "insertTextToInput" ui/main/windows/assistant/chat.html`
Expected: 至少看到函数定义那一行

---

## Task 4：重写 drop 事件处理器

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
messages.addEventListener('drop', async (e) => {
  messages.classList.remove('drag-over');

  const files = e.dataTransfer.files;
  if (files.length === 0) return;

  const paths = Array.from(files)
    .map(f => f.path)
    .filter(Boolean);

  if (paths.length === 0) return;

  insertTextToInput(paths.join(', '));
});
```

- [ ] **Step 2: 验证新 handler 已就位**

Run: `grep -n "messages.addEventListener('drop'" ui/main/windows/assistant/chat.html`
Expected: 只有一行，且后面紧跟 `messages.classList.remove('drag-over');`

- [ ] **Step 3: 验证旧的分类逻辑已删除**

Run: `grep -n "imageFiles\|otherFiles\|handleDroppedImage\|handleDroppedFile" ui/main/windows/assistant/chat.html`
Expected: 在 drop handler 区域不应再有这些变量；后续 Task 5 删除函数定义后应全部消失

---

## Task 5：删除 handleDroppedImage 和 handleDroppedFile 函数

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
Expected: 这些函数可能还在其他地方（如发送消息时）调用，但 drop handler 区域不应再调用。逐一确认剩余调用点都是合理的（如 sendMessage 流程中的 showTyping），不是 drop 流程遗留。

---

## Task 6：语法检查 + 启动验证

**Files:**
- 检查：`ui/main/windows/assistant/chat.html`

- [ ] **Step 1: 检查 HTML/JS 语法**

由于 chat.html 是 HTML 内嵌 `<script>`，无独立 linter。用 node 做基本语法检查：

Run: `node -e "const fs = require('fs'); const html = fs.readFileSync('ui/main/windows/assistant/chat.html', 'utf8'); const m = html.match(/<script>([\s\S]*?)<\/script>/g); if (!m) { console.error('no script found'); process.exit(1); } m.forEach((s, i) => { const code = s.replace(/^<script>/, '').replace(/<\/script>$/, ''); try { new Function(code); console.log('script #' + i + ' syntax OK'); } catch (e) { console.error('script #' + i + ' syntax error:', e.message); process.exit(1); } });"`
Expected: 输出 `script #0 syntax OK` 等若干行，无 `syntax error`

如果有语法错误，根据报错行号（注意：node 报的行号是相对 script 内部的）定位修复。

- [ ] **Step 2: 启动程序验证**

Run: `./niu`
Expected: 程序正常启动，主对话框能打开

**注意**：不要用 `cargo build`，必须用 `./niu`（已编译好的二进制）。如果改了 Rust 代码才需要 `launcher/build.sh`，本次没改 Rust，直接 `./niu` 即可。

- [ ] **Step 3: 程序启动后无控制台报错**

在主对话框打开后，检查控制台（开发者工具）是否有 JS 报错（特别是 `insertTextToInput is not defined` 之类）。
Expected: 无报错。如果有 `insertTextToInput is not defined`，说明 Task 3 函数插入位置作用域不对——需要把函数移到全局或更外层。

---

## Task 7：手动功能验证

**Files:**
- 无代码改动，纯功能测试

- [ ] **Step 1: 拖入单张图片**

操作：从 Finder 拖一张 `.jpg` 到主对话框
Expected: 输入框出现 `/absolute/path/to/file.jpg`（纯路径），输入框聚焦，光标在末尾

- [ ] **Step 2: 拖入单个文档**

操作：从 Finder 拖一个 `.pdf` 到主对话框
Expected: 输入框出现 `/absolute/path/to/file.pdf`

- [ ] **Step 3: 拖入多文件**

操作：从 Finder 选中 3 个文件（任意类型混合）拖入
Expected: 输入框出现 `/path1, /path2, /path3`（逗号+空格分隔，一行）

- [ ] **Step 4: 拖入到已有内容**

操作：在输入框先输入 `帮我处理`，然后拖入一个文件
Expected: 输入框变为 `帮我处理\n/absolute/path/to/file`（已有非空格非换行内容时补换行）

- [ ] **Step 5: 拖入到末尾带空格的内容**

操作：在输入框输入 `帮我处理 ` （末尾带空格），然后拖入一个文件
Expected: 输入框变为 `帮我处理 /absolute/path/to/file`（末尾是空格时直接追加，不补换行）

- [ ] **Step 6: 补充文字后回车发送**

操作：拖入一个文件后，在路径后输入 `，请入库`，按回车
Expected: 消息成功发送，主 Agent 收到完整消息如 `/absolute/path/to/file，请入库`，主 Agent 委托 file-processor 子 Agent 处理入库

- [ ] **Step 7: 精灵窗口未受影响**

操作：打开精灵窗口，拖入一个文件
Expected: 精灵窗口仍走原 `send-to-agent` 路径，立即触发入库流程（精灵窗口行为不变）

- [ ] **Step 8: 检查后端日志确认消息格式**

Run: `ls logs/raw_http/ | tail -1` 拿到当天目录，然后看最新一次 LLM 交互的 request.json 中 user message
Expected: 用户消息为纯路径格式（如 `/path/to/file，请入库`），**不含** `入库照片：` / `入库文件：` 前缀

---

## Task 8：提交

**Files:**
- 修改：`ui/main/windows/assistant/chat.html`

- [ ] **Step 1: 查看改动**

Run: `git diff ui/main/windows/assistant/chat.html`
Expected: 删除两个旧函数（约 90 行），新增一个辅助函数（约 9 行），重写一个 drop handler（约 13 行，原 28 行）

- [ ] **Step 2: 添加并提交**

```bash
git add ui/main/windows/assistant/chat.html
git commit -m "feat(chat): 拖入文件改为插入路径到输入框

- 删除 handleDroppedImage / handleDroppedFile 两个函数
- 重写 drop 事件处理器：收集所有文件绝对路径，用 ', ' 拼成一行
- 新增 insertTextToInput 辅助函数：插入文本+聚焦+光标末尾+触发 input 事件
- 精灵窗口 spirit.html 不动，后端 /api/chat/session 不动，Agent 提示词不动
- 用户拖入后需补充描述（如\"请入库\"）再回车发送，主 Agent 通过 MCP schema 调用 file-processor

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

- [ ] **Step 3: 验证提交成功**

Run: `git log -1 --oneline`
Expected: 看到刚才的 commit，message 以 `feat(chat):` 开头

- [ ] **Step 4: 杀进程清理**

测试完必须彻底杀所有 niu 进程（按 CLAUDE.md 铁律）：

```bash
ps aux | grep -E "niu|launcher" | grep -v grep
```

找到 PID 后用 `kill -TERM <pid>` 优雅退出，**禁止 `pkill -f niu`**（会损坏 LightRAG vdb 文件）。
Expected: 无残留进程

---

## 回滚方案

如果验证失败需要回退：

```bash
git revert HEAD  # 创建反向 commit，保留历史
# 或者：
git checkout HEAD~1 -- ui/main/windows/assistant/chat.html  # 仅恢复 chat.html
```

**禁止 `git reset --hard`**（曾导致有效代码全部丢失）。

---

## Self-Review

### Spec coverage 检查

- spec 二.1 格式：纯绝对路径无前缀 → Task 4 新 drop handler 用 `paths.join(', ')`，不加前缀 ✓
- spec 二.2 多文件逗号+空格 → Task 4 `paths.join(', ')` ✓
- spec 二.3 混合拖入一行 → Task 4 不再分流 imageFiles/otherFiles，所有路径统一收集 ✓
- spec 二.4 光标末尾+聚焦 → Task 3 `insertTextToInput` 中 `focus()` + `setSelectionRange` ✓
- spec 二.5 不发送不触发 IPC → Task 4 新 handler 只调 `insertTextToInput`，无 IPC 调用 ✓
- spec 二.6 精灵窗口不动 → Task 7 Step 7 验证 ✓
- spec 二.7 不改渲染 → 整个计划无 `addMessage` / `encodeLocalPaths` 改动 ✓
- spec 二.8 不改 Agent 提示词 → 整个计划无 `config/agents/` 改动 ✓

### Placeholder 扫描

无 TBD / TODO / "implement later" / "add error handling" 等占位符。所有代码步骤都给了完整代码。

### Type consistency 检查

- `insertTextToInput(text)` 函数名在 Task 3 定义、Task 4 调用，一致 ✓
- `userInput` 变量名在 Task 3、Task 4、Task 7 中使用，与 chat.html 现有定义一致（Task 2 Step 5 验证） ✓
- `paths` 变量名在 Task 4 中定义和使用，一致 ✓

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-20-drag-to-insert-path.md`. Two execution options:

**1. Subagent-Driven (recommended)** - 我每个 Task 派一个新子 Agent，任务间我做 review，迭代快

**2. Inline Execution** - 在当前会话里直接执行，批量执行 + 关键节点 checkpoint

哪个？
