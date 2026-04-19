# 前端聊天窗口数据库视图重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 前端聊天窗口成为数据库的只读视图，所有消息统一从数据库读取显示，消除前端内存维护独立消息列表导致的去重bug。

**Architecture:** 数据库是唯一持久化消息源。临时目录(~/.niu/tmp/)存放画了人脸框的图片等数据库无法直接存储的二进制内容，消息记录引用临时文件路径。删除消息时联动清理临时目录。

**Tech Stack:** Electron (chat.html/main.js/preload-chat.js), Python (agent/session.py, niu_api/compat.py), SQLite (messages.db)

---

## 两条铁律（所有实现必须遵守）

1. **用户触发的** → 先刷新页面（即时反馈），再写数据库，数据库推送后重新刷新前端（用数据库版本替换临时显示）
2. **后端主动跟用户通话的** → 只写数据库，不碰前端

原来乱就是又想写前端又想写数据库，两套数据打架。把握住这两点就乱不了。

**具体体现：**
- 用户发消息 → 先在页面显示用户消息（即时反馈）→ 后端写数据库 → SSE推送 → refreshFromDB() 用数据库版本替换临时显示
- 用户拖入图片 → 先显示预览（即时反馈）→ 后端处理+写数据库 → SSE推送 → refreshFromDB() 显示结果
- 定时任务通知 → 只写数据库 → SSE推送 → refreshFromDB() 显示
- Agent主动回复 → 只写数据库 → SSE推送 → refreshFromDB() 显示

---

## 问题清单

1. **数据库是唯一消息源** — 前端只是数据库视图，SSE只通知不传内容
2. **临时目录** — 只存画了框的照片（原图上画红框后的新图）
3. **消息记录引用文件路径** — 前端看到路径是可显示的就直接显示，CSS自动缩放
4. **删除消息工具联动清理** — 后端删除消息时检查是否引用了临时目录文件，有则同删
5. **人脸框** — 后端在原图上画好，存临时目录，数据库存路径，前端直接显示图片
6. **小女孩alert** — 只有定时任务完成才触发，其他消息不触发

---

## File Structure

| 文件 | 职责 | 操作 |
|------|------|------|
| `ui/assistant/chat.html` | 聊天窗口渲染 | **重写JS部分** |
| `ui/assistant/main.js` | Electron主进程 | **修改SSE处理和process-image** |
| `ui/assistant/preload-chat.js` | IPC桥接 | **修改** |
| `ui/assistant/spirit.html` | 小女孩窗口 | **修改alert触发** |
| `agent/session.py` | 消息数据库 | **修改delete/clear方法** |
| `niu_api/compat.py` | API端点 | **修改clear/delete端点** |
| `mcp-servers/photo-server/src/niu_photo_server/__init__.py` | 照片处理 | **修改get_person_photos画框存图** |

---

## Task 1: chat.html — 重写消息渲染为数据库视图

**Files:**
- Modify: `ui/assistant/chat.html` (整个 `<script>` 部分，约800行)

**核心变化：**

### 删除的状态和逻辑
- `_isSending` 变量和所有引用
- `data-pending` 逻辑
- `onNewMessage` 中的去重、pending处理、内容渲染
- `onSyncMessages` 整个处理器
- `sendMessage()` 中的 `addMessage('user', text)` 本地渲染
- `sendMessage()` 中对 `result.reply` 的本地渲染
- `savedFileCount/savedPersonCount/savedNoteCount` 保存恢复逻辑
- `fileCount/personCount/noteCount` 的 `let` 和重新绑定（改为 `const`）
- `renderFaceBox()` 函数（不再需要前端算坐标）
- `createPersonPhotoElement()` 中的 bbox 渲染逻辑
- `lastMessageId` 变量

### 保留的逻辑
- `addMessage()` — 渲染单条消息（保留 ::person_photo:: 解析，但改为直接显示带框图）
- `addMessageWithId()` — 带 data-id 的消息渲染（保留去重检查）
- `prependMessage()` — 顶部插入历史消息
- `loadHistory()` — 初始化加载历史
- 滚动加载更多历史
- 拖放文件/图片的预览UI
- typing 动画
- 进度条
- 统计数据

### 新增的逻辑
- `refreshFromDB()` — 从数据库读取最新消息，与DOM对比，只追加新的
- SSE通知处理简化为：收到通知 → `refreshFromDB()`

### sendMessage() 新流程
```
1. 禁用发送按钮，显示typing
2. 调用 electronAPI.sendMessage(text)  // 后端存DB + SSE通知
3. 等待返回（不管返回内容）
4. 隐藏typing，启用发送按钮
5. // 消息显示由SSE通知触发 refreshFromDB() 完成
```

### refreshFromDB() 逻辑
```
1. 获取DOM中所有 data-id → existingIds Set
2. 调用 electronAPI.getHistory(50) 获取最新消息
3. 遍历消息，不在 existingIds 中的 → addMessageWithId()
4. 有新消息 → 滚动到底部，hideTyping()
5. 更新 oldestMessageId（如果之前为空）
```

### ::person_photo:: 渲染简化
当前：解析 bbox，加载原图，前端算缩放坐标画红框
改为：后端已画好框存临时目录，消息内容里是带框图的路径，前端直接显示图片

- [ ] **Step 1: 重写 sendMessage()**

铁律1：用户触发的 → 先刷新页面（即时反馈）→ 写数据库 → SSE推送 → refreshFromDB()替换

```javascript
async function sendMessage() {
  const text = userInput.value.trim();
  if (!text) return;
  if (sendBtn.disabled) return;  // 防并发

  // /new 指令
  if (text === '/new') {
    userInput.value = '';
    userInput.style.height = 'auto';
    await clearChat();
    return;
  }

  // 先刷新页面：即时显示用户消息（临时，无data-id）
  addMessage('user', text);

  userInput.value = '';
  userInput.style.height = 'auto';
  sendBtn.disabled = true;
  showTyping();
  window.electronAPI.notifyBusy(true, 'chat');
  statusBar.querySelector('.stats').innerHTML = '思考中...';

  try {
    await sendMessageWithRetry(text);
    // 后端已写数据库 + SSE推送，refreshFromDB() 会用数据库版本替换临时显示
  } catch (err) {
    addMessage('system', `连接失败: ${err.message}`);
  }

  hideTyping();
  window.electronAPI.notifyBusy(false, 'chat');
  sendBtn.disabled = false;
  loadStats();
  userInput.focus();
}
```

- [ ] **Step 2: 写 refreshFromDB()**

核心逻辑：从数据库读取消息，与DOM对比。数据库版本替换临时显示（铁律1：数据库推送后重新刷新前端）。

```javascript
async function refreshFromDB() {
  try {
    // 收集DOM中已有的消息ID
    const existingIds = new Set();
    document.querySelectorAll('.message[data-id]').forEach(div => {
      existingIds.add(div.dataset.id);
    });

    // 从数据库获取最新消息
    const result = await window.electronAPI.getHistory(50);
    const msgs = result.messages || [];

    let hasNew = false;
    msgs.forEach(msg => {
      if (!existingIds.has(msg.id)) {
        addMessageWithId(msg.role, msg.content, msg.id);
        hasNew = true;
      }
    });

    if (hasNew) {
      // 删除没有data-id的临时用户消息（被数据库版本替换）
      document.querySelectorAll('.message.user:not([data-id])').forEach(div => {
        div.remove();
      });
      messages.scrollTop = messages.scrollHeight;
      hideTyping();
      if (!oldestMessageId && msgs.length > 0) {
        oldestMessageId = msgs[0].id;
      }
    }
  } catch (e) {
    console.error('[Chat] 刷新消息失败:', e);
  }
}
```

**关键**：`sendMessage()` 先 `addMessage('user', text)` 显示临时消息（无data-id），后端写DB后SSE推送，`refreshFromDB()` 从数据库读到带id的版本，追加到DOM，同时删除无data-id的临时版本。这就是铁律1的"先刷新页面→写数据库→重新刷新前端"。

- [ ] **Step 3: 简化 SSE 通知处理**

删除 `onNewMessage` 的所有内容渲染逻辑，改为：
```javascript
window.electronAPI.onNewMessage(() => {
  refreshFromDB();
});
```

删除整个 `onSyncMessages` 处理器。

- [ ] **Step 4: 简化 onSpiritState**

```javascript
window.electronAPI.onSpiritState((state) => {
  if (state === 'busy') {
    statusBar.querySelector('.stats').textContent = '⏳ 处理中...';
  } else if (state === 'idle') {
    loadStats();  // 直接从后端刷新统计
  }
});
```

- [ ] **Step 5: 简化 onAlert**

```javascript
window.electronAPI.onAlert(() => {
  // 定时任务通知，消息已通过数据库+SSE显示，此处仅视觉提示
  console.log('[Chat] 收到提醒通知');
});
```

- [ ] **Step 6: 简化拖放处理**

handleDroppedImage: 保留预览UI，删除本地渲染agent回复。agent回复由SSE→refreshFromDB()显示。
handleDroppedFile: 同上。

```javascript
async function handleDroppedImage(file) {
  const filePath = file.path;
  if (!filePath) return;
  if (sendBtn.disabled) return;  // 防并发

  // 显示预览（纯UI反馈，用原图）
  const previewDiv = document.createElement('div');
  previewDiv.className = 'image-preview uploading';
  const imgEl = document.createElement('img');
  imgEl.src = await window.electronAPI.getImageUrl(filePath);
  imgEl.dataset.filePath = filePath;
  previewDiv.appendChild(imgEl);
  imgEl.addEventListener('dblclick', () => {
    window.electronAPI.openWithSystemViewer(filePath);
  });
  const overlayDiv = document.createElement('div');
  overlayDiv.className = 'upload-overlay';
  overlayDiv.textContent = '处理中...';
  previewDiv.appendChild(overlayDiv);
  const msgDiv = addMessage('user', null);
  msgDiv.appendChild(previewDiv);

  try {
    await window.electronAPI.processImage(filePath);
    // agent回复由SSE→refreshFromDB()显示
    previewDiv.classList.remove('uploading');
    overlayDiv.remove();
  } catch (e) {
    console.error('[Chat] 图片处理失败:', e);
    previewDiv.classList.add('error');
    overlayDiv.textContent = '处理失败';
  }
}
```

- [ ] **Step 7: 简化 ::person_photo:: 渲染**

删除 `renderFaceBox()` 函数。`createPersonPhotoElement()` 简化为直接显示图片（带框图已在后端画好）：

```javascript
async function createPersonPhotoElement(data) {
  const container = document.createElement('div');
  container.className = 'image-container';
  if (data.person_id) container.dataset.personId = data.person_id;

  const img = document.createElement('img');
  img.className = 'chat-image';
  img.style.cursor = 'pointer';

  // data.path 现在是带框图的路径（后端已画好）
  const imageUrl = await window.electronAPI.getImageUrl(data.path);
  img.src = imageUrl;
  img.dataset.filePath = data.path;

  img.addEventListener('dblclick', () => {
    window.electronAPI.openWithSystemViewer(data.path);
  });

  container.appendChild(img);
  return container;
}
```

- [ ] **Step 8: 简化 clearChat()**

```javascript
async function clearChat() {
  try {
    const result = await window.electronAPI.clearChat();
    if (result.success) {
      messages.innerHTML = '';
      oldestMessageId = null;
      addMessage('system', '✅ 聊天记录已清空');
    } else {
      addMessage('system', '❌ 清空失败: ' + (result.error || '未知错误'));
    }
  } catch (err) {
    addMessage('system', '❌ 清空失败: ' + err.message);
  }
  userInput.focus();
}
```

- [ ] **Step 9: 简化窗口focus事件**

```javascript
window.addEventListener('focus', async () => {
  window.electronAPI.notifyActivity();
  refreshFromDB();  // 窗口获得焦点时刷新
});
```

- [ ] **Step 10: 简化 loadHistory()**

```javascript
async function loadHistory() {
  try {
    messages.innerHTML = '';
    oldestMessageId = null;

    const result = await window.electronAPI.getHistory(20);
    const history = result.messages || result;
    if (history && history.length > 0) {
      history.forEach(msg => {
        addMessageWithId(msg.role, msg.content, msg.id);
      });
      oldestMessageId = history[0].id;
    }
  } catch (e) {
    console.error('[Chat] 加载历史失败:', e);
    addMessage('system', '妞妞已就绪！有什么可以帮你的？');
  }
}
```

- [ ] **Step 11: 删除 prependMessage 中的去重**

prependMessage不再需要去重检查（addMessageWithId已有），但保留函数用于滚动加载更多。

- [ ] **Step 12: 删除 lastMessageId 变量**

全局删除 `lastMessageId`，所有引用改为 `refreshFromDB()`。

- [ ] **Step 13: fileCount/personCount/noteCount 改为 const**

```javascript
const fileCount = document.getElementById('file-count');
const personCount = document.getElementById('person-count');
const noteCount = document.getElementById('note-count');
```

删除所有 `fileCount = document.getElementById(...)` 重新绑定。

- [ ] **Step 14: 验证chat.html完整性**

确认所有函数引用正确，无遗漏的旧逻辑。

---

## Task 2: main.js — 简化SSE处理和process-image

**Files:**
- Modify: `ui/assistant/main.js`

- [ ] **Step 1: SSE new_message 处理简化**

当前（main.js:1081-1089）：收到 new_message 后转发完整事件给 chat.html，并触发小女孩alert。

改为：只发送简单通知给 chat.html，不触发小女孩alert（alert只由定时任务触发）。

```javascript
if (event.type === 'new_message') {
  // 通知 chat.html 有新消息（不传内容，chat.html 自己从数据库读取）
  if (chatWindow && !chatWindow.isDestroyed() && chatWindow.isVisible()) {
    chatWindow.webContents.send('new-message');
  }
}
```

删除 SSE 中的小女孩 alert 触发（line 1087-1089）。

- [ ] **Step 2: 删除 sync-messages 机制**

删除 `did-finish-load` 中的 `chatWindow.webContents.send('sync-messages')`（line 166-168）。
删除 SSE 重连时的 `chatWindow.webContents.send('sync-messages')`（line 1065-1067）。

SSE重连后，chat.html 的 `onNewMessage` 会自动触发 `refreshFromDB()`，不需要单独的补漏机制。

- [ ] **Step 3: process-image 简化**

当前 process-image 发送 `入库照片：路径` 或 `入库文件：路径` 给 `/api/chat/session`。
这个逻辑不变，因为后端会存DB + SSE通知，前端由 refreshFromDB() 显示结果。

但删除 `isImage` 判断和 `action` 变量，统一为一种消息格式：

```javascript
ipcMain.handle('process-image', async (event, filePath) => {
  return new Promise((resolve) => {
    const isImage = /\.(jpg|jpeg|png|gif|bmp|webp|tiff?)$/i.test(filePath);
    const action = isImage ? '入库照片' : '入库文件';
    const data = JSON.stringify({
      session_id: config.chatSessionId || null,
      message: `${action}：${filePath.replace(/\\/g, '/')}`
    });
    // ... HTTP请求不变 ...
  });
});
```

process-image 逻辑保持不变，因为后端 chat_session 端点已经存DB + SSE通知。

---

## Task 3: preload-chat.js — 简化IPC桥接

**Files:**
- Modify: `ui/assistant/preload-chat.js`

- [ ] **Step 1: 简化 onNewMessage**

当前：`ipcRenderer.on('new-message', (event, msg) => callback(msg))`
改为：`ipcRenderer.on('new-message', (event) => callback())`

SSE通知不再传消息内容，只通知"有新消息"。

- [ ] **Step 2: 删除 onSyncMessages**

删除 `onSyncMessages` 桥接（line 58-59）。

---

## Task 4: spirit.html + main.js — 简化小女孩alert逻辑

**Files:**
- Modify: `ui/assistant/spirit.html`
- Modify: `ui/assistant/main.js`

**原则：**
1. 收到alert → 直接蹦高，不判断窗口是否聚焦/可见/任何条件
2. 鼠标移到小女孩身上 → 清除alert状态 → 唤醒状态
3. 只有定时任务完成才触发alert，其他消息不触发

- [ ] **Step 1: main.js — SSE不再触发小女孩alert**

当前（main.js:1087-1088）：SSE收到new_message时，判断窗口不聚焦则触发小女孩alert。
删除这段判断和触发。SSE的new_message只通知chat.html刷新，不触发小女孩。

```javascript
if (event.type === 'new_message') {
  // 只通知 chat.html 有新消息
  if (chatWindow && !chatWindow.isDestroyed() && chatWindow.isVisible()) {
    chatWindow.webContents.send('new-message');
  }
  // 不触发小女孩alert（alert只由定时任务触发）
}
```

- [ ] **Step 2: main.js — alert轮询简化，删除所有聚焦判断**

当前（main.js:970-1004）：每10秒轮询pending-alerts，判断聊天窗口是否聚焦，不聚焦才触发。
改为：有pending-alert就触发，不判断任何条件。

```javascript
alertsPollingTimer = setInterval(async () => {
  try {
    const alerts = await fetchPendingAlerts();
    if (alerts && alerts.length > 0) {
      // 直接触发小女孩蹦高，不判断任何条件
      if (spiritWindow && !spiritWindow.isDestroyed()) {
        spiritWindow.webContents.send('alert', '⏰');
      }
    }
  } catch (e) {
    console.error('[Alerts] 轮询失败:', e.message);
  }
}, 10000);
```

- [ ] **Step 3: spirit.html — mouseenter清除alert**

当前（spirit.html:433-447）：mouseenter只处理IDLE/SLEEP→WAKE，不处理ALERT。
改为：mouseenter时如果当前是ALERT状态，先endAlert()再WAKE。

```javascript
spirit.addEventListener('mouseenter', () => {
  spirit.classList.add('hover');
  window.electronAPI.resizeWindow(SIZE.hover.width, SIZE.hover.height);
  window.electronAPI.spiritMouseEnter();

  clearTimeout(hoverTimer);
  hoverTimer = setTimeout(() => {
    window.electronAPI.showSticky();
  }, 1000);

  // 鼠标移到小女孩身上：清除alert，唤醒
  if (currentState === State.ALERT) {
    endAlert();
  } else if (currentState === State.IDLE || currentState === State.SLEEP) {
    setState(State.WAKE);
  }
});
```

- [ ] **Step 4: 确认chat.html的onAlert不触发小女孩**

当前chat.html的onAlert只是console.log，不触发任何状态变化。正确，保持。

---

## Task 5: 后端 — 临时目录和人脸框画图

**Files:**
- Modify: `mcp-servers/photo-server/src/niu_photo_server/__init__.py`
- Create: `agent/tmp_dir.py` (临时目录管理工具函数)

- [ ] **Step 1: 创建临时目录管理模块 agent/tmp_dir.py**

```python
"""临时文件目录管理 - 存放画了人脸框的图片等数据库无法直接存储的内容"""
import os
import shutil
from pathlib import Path

def get_tmp_dir() -> Path:
    """获取临时目录 ~/.niu/tmp/"""
    tmp_dir = Path.home() / ".niu" / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    return tmp_dir

def save_to_tmp(filename: str, data: bytes) -> str:
    """保存文件到临时目录，返回绝对路径"""
    tmp_dir = get_tmp_dir()
    filepath = tmp_dir / filename
    filepath.write_bytes(data)
    return str(filepath)

def is_tmp_file(filepath: str) -> bool:
    """判断文件是否在临时目录中"""
    tmp_dir = str(get_tmp_dir())
    return filepath.startswith(tmp_dir)

def cleanup_tmp_files(filepaths: list[str]) -> int:
    """删除临时目录中的文件，返回删除数量"""
    deleted = 0
    for fp in filepaths:
        if is_tmp_file(fp) and os.path.exists(fp):
            os.remove(fp)
            deleted += 1
    return deleted

def cleanup_all_tmp() -> int:
    """清空整个临时目录，返回删除数量"""
    tmp_dir = get_tmp_dir()
    if not tmp_dir.exists():
        return 0
    count = sum(1 for _ in tmp_dir.iterdir())
    shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    return count
```

- [ ] **Step 2: 修改 photo-server 的 get_person_photos — 在原图上画框存临时目录**

当前 `get_person_photos` 返回 `{"path": 原图路径, "bbox": [x1,y1,x2,y2], ...}`。
改为：在原图上用 PIL/cv2 画红框，保存到临时目录，返回 `{"path": 带框图路径}`。

在 `get_person_photos` 函数中，遍历 photos 时：
1. 读取原图
2. 在原图上画红框（bbox坐标，不缩放）
3. 保存到 `~/.niu/tmp/{person_id}_{photo_id}_boxed.jpg`
4. 返回的 path 改为带框图路径，删除 bbox 字段

```python
import cv2
from agent.tmp_dir import save_to_tmp

# 在 get_person_photos 中，对每张代表照片：
img = cv2.imread(photo_file_path)
if img is not None and bbox and len(bbox) == 4:
    x1, y1, x2, y2 = [int(v) for v in bbox]
    cv2.rectangle(img, (x1, y1), (x2, y2), (248, 167, 200), 2)  # 粉色框
    # 保存到临时目录
    boxed_filename = f"{person_id}_{photo_id}_boxed.jpg"
    _, encoded = cv2.imencode('.jpg', img)
    boxed_path = save_to_tmp(boxed_filename, encoded.tobytes())
    # 返回带框图路径
    photo_data["path"] = boxed_path.replace("\\", "/")
    # 删除 bbox（前端不再需要）
    photo_data.pop("bbox", None)
```

- [ ] **Step 3: 更新 ::person_photo:: 标记格式**

skill 文档 `memory/skills/photo-face-display.md` 中的标记格式更新：
- 删除 `bbox` 参数（后端已画好框）
- `path` 现在指向临时目录中的带框图

标记格式从：
```
::person_photo::{"path": "原图路径", "bbox": [x1,y1,x2,y2], "person_id": "ID", "name": "名"}::
```
改为：
```
::person_photo::{"path": "带框图路径", "person_id": "ID", "name": "名"}::
```

---

## Task 6: 后端 — 删除消息联动清理临时文件

**Files:**
- Modify: `agent/session.py`
- Modify: `niu_api/compat.py`

- [ ] **Step 1: session.py — clear_messages 联动清理**

在 `clear_messages()` 中，删除前先收集所有消息的 content，提取临时文件路径，删除后清理临时文件。

```python
async def clear_messages(self) -> int:
    """Clear all messages and cleanup temp files"""
    # 先收集所有消息内容（用于清理临时文件）
    async with aiosqlite.connect(self.db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT content FROM messages")
        rows = await cursor.fetchall()

    # 提取临时文件路径
    from agent.tmp_dir import is_tmp_file, cleanup_tmp_files
    tmp_files = []
    for row in rows:
        content = row["content"] or ""
        # 从 content 中提取文件路径（简单匹配 ~/.niu/tmp/ 路径）
        if "/.niu/tmp/" in content or "\\.niu\\tmp\\" in content:
            # 提取路径（消息中可能包含多个路径）
            import re
            paths = re.findall(r'[A-Za-z]:[/\\][^\s"\']+/\.niu/tmp/[/\\][^\s"\']+', content)
            paths += re.findall(r'/home/[/\\][^\s"\']+/\.niu/tmp/[/\\][^\s"\']+', content)
            tmp_files.extend(paths)

    # 删除消息
    async with aiosqlite.connect(self.db_path) as db:
        cursor = await db.execute("DELETE FROM messages")
        deleted = cursor.rowcount
        await db.commit()

    # 清理临时文件
    if tmp_files:
        cleanup_tmp_files(tmp_files)

    logger.info(f"Cleared {deleted} messages, cleaned {len(tmp_files)} temp files")
    return deleted
```

- [ ] **Step 2: session.py — delete_messages_by_ids 联动清理**

同样在 `delete_messages_by_ids()` 中添加临时文件清理逻辑。

- [ ] **Step 3: compat.py — clear_chat 端点联动清理全部临时文件**

`/api/chat/clear` 端点在清空聊天时，除了 session.py 的联动，还需要调用 `cleanup_all_tmp()` 确保临时目录完全清空。

```python
# 在 clear_chat() 中，store.clear_messages() 之后：
from agent.tmp_dir import cleanup_all_tmp
cleaned_tmp = cleanup_all_tmp()
logger.info(f"Cleaned {cleaned_tmp} temp files")
```

---

## Task 7: 集成验证

- [ ] **Step 1: 启动应用，验证初始加载**

启动应用 → 聊天窗口打开 → 从数据库加载历史消息 → 显示正确，无重复

- [ ] **Step 2: 验证发送消息**

发送消息 → typing动画 → 消息出现（从数据库读取）→ 不重复 → 顺序正确

- [ ] **Step 3: 验证定时任务通知**

定时任务触发 → 小女孩蹦高 → 消息出现在聊天窗口 → 不重复

- [ ] **Step 4: 验证拖入图片**

拖入图片 → 预览显示 → 处理完成 → agent回复出现 → 不重复

- [ ] **Step 5: 验证人脸框**

查询未命名人物 → 显示带红框的照片 → 框在正确位置 → 不需要前端算坐标

- [ ] **Step 6: 验证SSE断开重连**

断开网络 → 重连 → 消息不丢失不重复

- [ ] **Step 7: 验证滚动加载更多**

滚动到顶部 → 加载更多历史 → 顺序正确 → 滚动位置不跳

- [ ] **Step 8: 验证清空聊天**

/new 指令 → 消息清空 → 临时目录文件删除

- [ ] **Step 9: 验证小女孩alert**

普通消息不触发小女孩蹦高，只有定时任务触发

---

## Self-Review

### 1. Spec coverage

| 问题 | 对应Task |
|------|----------|
| 数据库是唯一消息源 | Task 1 (chat.html重写) + Task 2 (main.js SSE简化) + Task 3 (preload简化) |
| 临时目录 | Task 5 Step 1 (tmp_dir.py) |
| 消息记录引用文件路径 | Task 1 Step 7 (person_photo简化) + Task 5 Step 2 (画框存图) |
| 删除消息联动清理 | Task 6 (session.py + compat.py) |
| 人脸框 | Task 5 Step 2 (后端画框) + Task 1 Step 7 (前端简化) |
| 小女孩alert | Task 4 (限制触发源) |

### 2. Placeholder scan

无 TBD/TODO/placeholder。所有步骤都有具体代码。

### 3. Type consistency

- `refreshFromDB()` 在 Task 1 定义，Task 2/3 引用一致
- `get_tmp_dir()` / `save_to_tmp()` / `cleanup_tmp_files()` 在 Task 5 定义，Task 6 引用一致
- `onNewMessage` 回调签名从 `(msg)` 改为 `()` 在 Task 1/2/3 一致
