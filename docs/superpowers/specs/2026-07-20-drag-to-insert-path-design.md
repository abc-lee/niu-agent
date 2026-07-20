# 拖入文件转文字插入对话框 设计

**日期**：2026-07-20
**作者**：Claude Code（brainstorming）
**状态**：设计待批准

## 一、问题背景

当前主对话框（`ui/main/windows/assistant/chat.html`）的拖入行为是模仿精灵窗口做的——拖入后立即触发入库流程：
- 图片：`handleDroppedImage` → 显示预览 → `window.electronAPI.processImage(filePath)` → 后端构造 `"入库照片：/path"` → POST `/api/chat/session`
- 文件：`handleDroppedFile` → 显示文件名 → `processImage(filePath)` → 后端构造 `"入库文件：/path"` → POST `/api/chat/session`

用户希望改成：拖入后不直接执行，把文件路径以文字形式插入到对话框输入框，等用户补充描述文字后回车发送。

精灵窗口（`spirit.html`）的拖入行为不动。

## 二、需求确认（与用户对话得出）

1. **格式**：沿用现有 `"入库照片：/path"` / `"入库文件：/path"` 纯文本前缀格式。Agent 提示词不改，仍能识别。
2. **多文件**：同类型多文件拼成一行，路径间用 `", "`（逗号+空格）分隔。例如：`入库照片：/path1, /path2`。
3. **混合拖入**：图片和文档分别构造两行文本插入输入框（先图片后文档），各自走各自的前缀。
4. **光标**：插入后输入框获得焦点，光标定位到末尾，用户可直接打字补充描述。
5. **不发送**：拖入只插入文字，不触发任何 IPC，不调用后端，不显示 typing 指示器。
6. **精灵窗口不动**：`spirit.html` 的 `handleDroppedFiles` 保持原样。
7. **不改渲染**：user 消息仍走纯文本渲染（`textContent`），不改 `addMessage`，不改 markdown 渲染逻辑。
8. **不改 Agent 提示词**：`niu.md` 中对 `"入库照片："` / `"入库文件："` 前缀的识别能力保留。

## 三、实现方案

### 改动文件

只改一个文件：`REDACTED_USER_PATH/tools/ai-bot/ui/main/windows/assistant/chat.html`

### 改动位置

`chat.html` 的 drop 事件处理器（L1540-1568）和两个处理函数（L1571-1630 `handleDroppedImage`、L1633-1664 `handleDroppedFile`）。

### 改动内容

**1. drop 事件处理器（L1540-1568）重写**

不再分别调用 `handleDroppedImage` / `handleDroppedFile`，改为：
- 收集所有图片路径和所有文档路径
- 构造插入文本：
  - 图片部分：`入库照片：${imagePaths.join(', ')}`
  - 文档部分：`入库文件：${otherPaths.join(', ')}`
  - 两部分都有时用换行 `\n` 连接
- 调用新函数 `insertTextToInput(text)` 插入到输入框

**2. 新增辅助函数 `insertTextToInput(text)`**

```javascript
function insertTextToInput(text) {
  const current = userInput.value;
  if (current && !current.endsWith('\n')) {
    userInput.value = current + '\n' + text;
  } else {
    userInput.value = current + text;
  }
  userInput.focus();
  userInput.setSelectionRange(userInput.value.length, userInput.value.length);
  // 触发 input 事件，确保 sendBtn 状态等逻辑更新
  userInput.dispatchEvent(new Event('input', { bubbles: true }));
}
```

**3. 删除 `handleDroppedImage` 和 `handleDroppedFile` 函数**

这两个函数及其调用的 `addMessage`、`showTyping`、`processImage`、`notifyBusy` 等都不再需要。

**4. 保留 `dragover`/`dragleave` 视觉态切换**

`drag-over` class 的添加/移除逻辑不动，保留拖入时的视觉反馈。

### 不改动的部分

- `preload-chat.js` 的 `processImage` IPC 暴露：保留，可能其他地方用（后续清理可考虑）。
- `main.js` 的 `ipcMain.handle('process-image', ...)`：保留，精灵窗口或未来功能可能复用。
- `addMessage` 函数：不改。
- `sendMessage` / `sendMessageWithRetry`：不改。
- `encodeLocalPaths` / markdown 渲染：不改。
- `spirit.html` 整个文件：不改。
- 后端 `niu_api/compat.py` 的 `/api/chat/session` 路由：不改。
- Agent 提示词 `config/agents/niu.md`：不改。

## 四、数据流（改动后）

```
用户拖入文件
  ↓
chat.html drop handler
  ↓ 收集 imageFiles / otherFiles 路径
  ↓ 构造 "入库照片：/p1, /p2\n入库文件：/p3"
  ↓ insertTextToInput(text)
  ↓ userInput.value = text + focus + 光标末尾
  ↓
用户补充描述文字，按 Enter
  ↓
sendMessage() → sendMessageWithRetry() → window.electronAPI.sendMessage(text)
  ↓
main.js ipcMain.handle('send-message') → POST /api/chat/session { message: text }
  ↓
niu_api/compat.py chat_session(request) → runner.chat(message=request.message)
  ↓
Agent 识别 "入库照片：" 前缀 → 触发入库流程
```

## 五、测试方案

### 手动测试用例

1. **拖入单张图片**：输入框出现 `入库照片：/path/to.jpg`，光标在末尾，输入框聚焦。
2. **拖入单个文档**：输入框出现 `入库文件：/path/to.pdf`，光标在末尾，输入框聚焦。
3. **拖入多张图片**：输入框出现 `入库照片：/path1, /path2, /path3`。
4. **拖入多个文档**：输入框出现 `入库文件：/path1, /path2`。
5. **混合拖入（图片+文档）**：输入框出现两行，第一行图片，第二行文档。
6. **拖入后补充文字**：在路径后输入 "，请帮我描述一下"，回车，Agent 收到完整消息 `"入库照片：/path，请帮我描述一下"`。
7. **拖入到已有内容**：输入框已有文字 "看看这个"，拖入图片，应为 `看看这个\n入库照片：/path`（已有文字后补换行）。
8. **精灵窗口未受影响**：打开精灵窗口，拖入文件，仍走原 `send-to-agent` 路径，立即触发入库。
9. **HEIC 等特殊格式**：拖入 HEIC 图片，路径仍正常插入（不再尝试预览）。
10. **拖入无路径的项**（罕见，理论保护）：跳过该项，其他项继续。

### 不需要的测试

- 不写自动化测试：UI 拖入行为难以在自动化中模拟，且改动范围小、逻辑直观，手动测试足够。
- 不改后端测试：后端行为完全不变。

## 六、风险评估

### 低风险

- **改动范围小**：只改一个文件，删除两个函数，新增一个辅助函数。
- **后端无改动**：Agent 看到的消息格式与之前完全一致（`"入库照片：/path"`）。
- **不影响精灵窗口**：完全独立代码路径。
- **可回滚**：git revert 即可。

### 需注意

- **预览功能丢失**：原 `handleDroppedImage` 会显示图片预览，改造后不再显示。用户在发送前看不到缩略图。**接受**——用户明确要求"转换成文字"，预览属于原"直接执行"流程的一部分，应一并移除。
- **`processImage` IPC 残留**：`preload-chat.js` 的 `processImage` 暴露保留，但 `chat.html` 不再调用。后续可清理，本次不动以减少改动面。
- **`addMessage('user', null)` 的空消息行为**：原代码会创建空 user 消息挂预览，改造后不再创建，无副作用。

## 七、未来可能的优化（不在本次范围）

- 清理 `preload-chat.js` 的 `processImage` 和 `main.js` 的 `ipcMain.handle('process-image')`。
- user 消息也走 markdown 渲染，让路径在用户气泡里显示为可点击链接。
- 拖入时显示缩略图缩略名提示（非预览，仅提示）。

## 八、验证清单

实施完成后逐项确认：

- [ ] 主对话框拖入单文件：路径插入输入框
- [ ] 主对话框拖入多文件：路径按规则拼接
- [ ] 主对话框拖入混合：两行文本
- [ ] 输入框聚焦 + 光标末尾
- [ ] 已有内容时正确换行
- [ ] 精灵窗口拖入行为未变
- [ ] 后端日志能看到 `"入库照片：/path"` 格式消息
- [ ] Agent 正常触发入库流程
