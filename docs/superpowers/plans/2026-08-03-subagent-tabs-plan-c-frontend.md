# 动态子 Agent 标签页 — 计划 C：前端 Tab 栏 + SSE 管理 + 异常恢复

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 前端动态 Tab 栏——主 Agent 调用子 Agent 时自动创建 tab，用户可查看子 Agent 工作过程并与其交互。

**Architecture:** chat.html 新增 tab 栏（header 和 messages 之间），每个子 Agent tab 有独立 messages 容器。main.js 新增 SubagentSSEManager 管理多个独立 SSE 连接，事件通过 IPC 转发到 chat.html。preload-chat.js 新增 onSubagentEvent 接口。窗口关闭/重开时从 /api/subagents/running 恢复 tab。

**Tech Stack:** JavaScript, Electron, Node.js http

**设计文档:** `docs/superpowers/specs/2026-08-03-dynamic-subagent-tabs-design.md` §5

**依赖:** 计划 A（后端事件通道）+ 计划 B（后端消息通道）已完成

---

### Task 1: Tab 栏 DOM + CSS + 切换逻辑

**Files:**
- Modify: `ui/main/windows/assistant/chat.html` L753-763 (header 和 messages 之间插入 tab 栏)
- Modify: `ui/main/windows/assistant/chat.html` `<style>` 区域 (新增 tab CSS)

**参考代码位置:**
- `chat.html` L753-833: DOM 结构 `.container > .header > .messages > .input-area > .status-bar`
- `chat.html` L837-843: 全局变量 `messages = document.getElementById('messages')`
- `chat.html` L1463-1598: `addMessage(role, text)` — 消息渲染核心函数
- `chat.html` L1255-1343: `sendMessage()` — 消息发送函数
- `chat.html` L1345-1361: `clearChat()` — 清空聊天

- [ ] **Step 1: 新增 tab 栏 DOM**

在 `chat.html` 的 `.header`(L761 `</div>`) 和 `.messages`(L763) 之间插入:

```html
<!-- ========== 子 Agent 标签栏 ========== -->
<div class="tab-bar" id="tab-bar">
  <div class="tab active" data-tab="main" id="tab-main">主对话</div>
</div>
```

- [ ] **Step 2: 新增 tab CSS**

在 `chat.html` 的 `<style>` 区域新增:

```css
/* ========== 子 Agent 标签栏 ========== */
.tab-bar {
  display: flex;
  align-items: center;
  gap: 2px;
  padding: 0 8px;
  background: rgba(250, 248, 240, 0.95);
  border-bottom: 1px solid rgba(0, 0, 0, 0.06);
  min-height: 32px;
  flex-shrink: 0;
  overflow-x: auto;
}
.tab {
  padding: 4px 12px;
  font-size: 12px;
  color: #888;
  cursor: pointer;
  border-radius: 6px 6px 0 0;
  white-space: nowrap;
  position: relative;
  user-select: none;
}
.tab:hover {
  background: rgba(0, 0, 0, 0.04);
}
.tab.active {
  color: #333;
  font-weight: 600;
  background: #fff;
  border-bottom: 2px solid #40e0d0;
}
.tab .tab-badge {
  position: absolute;
  top: 2px;
  right: 2px;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #ff6b6b;
  display: none;
}
.tab.has-update .tab-badge {
  display: block;
}
.tab.completed {
  opacity: 0.5;
}
.tab.error {
  color: #ff6b6b;
}
```

- [ ] **Step 3: 新增 tab 管理全局变量和函数**

在 `chat.html` 的 `<script>` 区域全局变量附近新增:

```javascript
// ========== 子 Agent Tab 管理 ==========
let _activeTab = 'main';  // 当前激活的 tab id（'main' 或 unique_name）
let _subagentTabs = {};   // {unique_name: {messagesDiv, title, isSync, completed}}

function createSubagentTab(unique_name, agent_name, is_sync) {
  // 如果 tab 已存在，不重复创建
  if (_subagentTabs[unique_name]) return;

  // 创建 tab 按钮
  const tabBar = document.getElementById('tab-bar');
  const tab = document.createElement('div');
  tab.className = 'tab';
  tab.dataset.tab = unique_name;
  tab.innerHTML = `${agent_name}<span class="tab-badge"></span>`;
  tab.addEventListener('click', () => switchTab(unique_name));
  tabBar.appendChild(tab);

  // 创建独立 messages 容器（隐藏）
  const messagesContainer = document.getElementById('messages');
  const subMessages = document.createElement('div');
  subMessages.className = 'messages';
  subMessages.id = `messages-${unique_name}`;
  subMessages.style.display = 'none';
  subMessages.style.flex = '1';
  subMessages.style.overflowY = 'auto';
  subMessages.style.padding = '16px';
  messagesContainer.parentNode.insertBefore(subMessages, messagesContainer.nextSibling);

  _subagentTabs[unique_name] = {
    tabEl: tab,
    messagesDiv: subMessages,
    title: agent_name,
    isSync: is_sync,
    completed: false,
  };

  // 自动切换到新 tab
  switchTab(unique_name);

  // 同步子 Agent：主对话 tab 显示"子 Agent 工作中"
  if (is_sync) {
    addSystemMessage(`子 Agent ${agent_name} 工作中...`);
  }
}

function switchTab(tabId) {
  _activeTab = tabId;
  // 更新 tab 按钮状态
  document.querySelectorAll('.tab').forEach(t => {
    t.classList.toggle('active', t.dataset.tab === tabId);
    if (t.dataset.tab === tabId) t.classList.remove('has-update');
  });
  // 切换 messages 容器
  const mainMessages = document.getElementById('messages');
  if (tabId === 'main') {
    mainMessages.style.display = '';
    Object.values(_subagentTabs).forEach(t => t.messagesDiv.style.display = 'none');
  } else {
    mainMessages.style.display = 'none';
    Object.values(_subagentTabs).forEach(t => {
      t.messagesDiv.style.display = (t.messagesDiv.id === `messages-${tabId}`) ? '' : 'none';
    });
  }
}

function getActiveMessagesContainer() {
  if (_activeTab === 'main') return document.getElementById('messages');
  const tab = _subagentTabs[_activeTab];
  return tab ? tab.messagesDiv : document.getElementById('messages');
}

function addSubagentMessageToTab(unique_name, role, text) {
  const tab = _subagentTabs[unique_name];
  if (!tab) return;
  const container = tab.messagesDiv;
  const div = document.createElement('div');
  div.className = `message ${role}`;
  if (role === 'assistant') {
    div.innerHTML = marked.parse(text);
  } else {
    div.textContent = text;
  }
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
  // 如果不是当前 tab，加红点
  if (_activeTab !== unique_name) {
    tab.tabEl.classList.add('has-update');
  }
}

function closeSubagentTab(unique_name) {
  const tab = _subagentTabs[unique_name];
  if (!tab) return;
  tab.tabEl.classList.add('completed');
  tab.completed = true;
  // 如果是当前 tab，切回主对话
  if (_activeTab === unique_name) {
    switchTab('main');
  }
}

function markSubagentError(unique_name) {
  const tab = _subagentTabs[unique_name];
  if (!tab) return;
  tab.tabEl.classList.add('error');
  tab.tabEl.classList.add('completed');
}
```

- [ ] **Step 4: 修改 sendMessage — 子 Agent tab 下调 POST API**

`chat.html` L1255-1343 的 `sendMessage()` 函数，在发送逻辑中判断当前 tab:

```javascript
// 在 sendMessage 函数中，原有 sendMessageWithRetry(text) 调用改为：
if (_activeTab === 'main') {
  sendMessageWithRetry(text);
} else {
  // 子 Agent tab：调 POST /api/subagents/{unique_name}/message
  fetch(`/api/subagents/${_activeTab}/message`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content: text })
  }).then(r => {
    if (r.ok) {
      addSubagentMessageToTab(_activeTab, 'user', text);
    } else if (r.status === 404) {
      addSubagentMessageToTab(_activeTab, 'system', '子 Agent 已结束，无法发送消息');
    }
  }).catch(e => {
    addSubagentMessageToTab(_activeTab, 'system', `发送失败: ${e.message}`);
  });
}
```

- [ ] **Step 5: 提交**

```bash
git add ui/main/windows/assistant/chat.html
git commit -m "feat: subagent tab bar DOM + CSS + switch logic + sendMessage routing"
```

---

### Task 2: main.js SubagentSSEManager + IPC 转发

**Files:**
- Modify: `ui/main/main.js` (新增 SubagentSSEManager 类 + subagent-event IPC)
- Modify: `ui/main/preload-chat.js` (新增 onSubagentEvent 接口)

**参考代码位置:**
- `main.js` L1758-1865: `startMessageEventStream()` — SSE 单连接实现（参考模板）
- `main.js` L1754-1756: `sseReconnectTimer` / `sseConnectedBefore` 全局变量
- `main.js` L1853-1864: SSE 重连机制（3s 重连）
- `main.js` L274-279: `chatWindow.on('closed')` — 窗口关闭清理
- `main.js` L1791-1844: `chatWindow.webContents.send` 事件转发模式
- `preload-chat.js` L60-61: `onNewMessage` 接口 — `ipcRenderer.on('new-message', ...)`

- [ ] **Step 1: main.js 新增 SubagentSSEManager**

在 `main.js` 的 `startMessageEventStream()` 函数之后新增:

```javascript
// ========== 子 Agent SSE 连接管理 ==========
const SubagentSSEManager = {
  connections: new Map(), // unique_name → {req, reconnectTimer}

  connect(unique_name) {
    if (this.connections.has(unique_name)) return;

    const port = NIU_API_PORT || 9876;
    const url = `http://127.0.0.1:${port}/api/subagents/${unique_name}/stream`;

    const connection = { req: null, reconnectTimer: null };

    const doConnect = () => {
      const req = http.request(url, (res) => {
        let buffer = '';
        res.on('data', (chunk) => {
          buffer += chunk.toString();
          const lines = buffer.split('\n');
          buffer = lines.pop();
          for (const line of lines) {
            if (line.startsWith('data: ')) {
              try {
                const event = JSON.parse(line.slice(6));
                if (chatWindow && !chatWindow.isDestroyed()) {
                  chatWindow.webContents.send('subagent-event', { unique_name, event });
                }
              } catch (e) { /* ignore parse errors */ }
            }
          }
        });
        res.on('end', () => {
          // 重连（3s）
          connection.reconnectTimer = setTimeout(doConnect, 3000);
        });
      });
      req.on('error', (e) => {
        connection.reconnectTimer = setTimeout(doConnect, 3000);
      });
      req.end();
      connection.req = req;
    };

    doConnect();
    this.connections.set(unique_name, connection);
  },

  disconnect(unique_name) {
    const conn = this.connections.get(unique_name);
    if (!conn) return;
    if (conn.reconnectTimer) clearTimeout(conn.reconnectTimer);
    if (conn.req) conn.req.destroy();
    this.connections.delete(unique_name);
  },

  disconnectAll() {
    for (const [name] of this.connections) {
      this.disconnect(name);
    }
  }
};
```

- [ ] **Step 2: 主 SSE 流新增 subagent_started 事件处理**

在 `main.js` 的 `startMessageEventStream()` 函数中，事件类型路由（L1791-1844 区域）新增:

```javascript
// 在 new_message 分支中检测 subagent_started role
if (data.role === 'subagent_started') {
  try {
    const info = JSON.parse(data.content);
    // 创建 tab
    if (chatWindow && !chatWindow.isDestroyed()) {
      chatWindow.webContents.send('subagent-started', info);
      // 建立子 Agent SSE 连接
      SubagentSSEManager.connect(info.unique_name);
    }
  } catch (e) { /* ignore */ }
}
```

- [ ] **Step 3: chatWindow.on('closed') 断开所有子 Agent SSE**

`main.js` L274-279，在 chatWindow.on('closed') 中追加:

```javascript
chatWindow.on('closed', () => {
  chatWindow = null;
  SubagentSSEManager.disconnectAll();
  if (spiritWindow && !spiritWindow.isDestroyed()) {
    spiritWindow.webContents.send('chat-closed');
  }
});
```

- [ ] **Step 4: preload-chat.js 新增 onSubagentEvent + onSubagentStarted 接口**

`preload-chat.js`，在 `electronAPI` 对象中新增:

```javascript
onSubagentEvent: (callback) => ipcRenderer.on('subagent-event', (_event, data) => callback(data)),
onSubagentStarted: (callback) => ipcRenderer.on('subagent-started', (_event, info) => callback(info)),
```

- [ ] **Step 5: 提交**

```bash
git add ui/main/main.js ui/main/preload-chat.js
git commit -m "feat: SubagentSSEManager + subagent-event IPC + onSubagentEvent preload API"
```

---

### Task 3: chat.html 接收子 Agent 事件 + 渲染

**Files:**
- Modify: `ui/main/windows/assistant/chat.html` (新增事件回调)

**参考代码位置:**
- `chat.html` L2001-2006: `onToolStatus` 回调 — `window.electronAPI.onToolStatus((data) => {...})`
- `chat.html` L2195-2244: `onNewMessage` 回调 — 主 Agent SSE 事件入口
- `chat.html` L1463-1598: `addMessage(role, text)` — 消息渲染

- [ ] **Step 1: 注册 onSubagentStarted 回调 — 创建 tab**

在 `chat.html` 的 `onNewMessage` 回调之后新增:

```javascript
// ========== 子 Agent 事件 ==========
window.electronAPI.onSubagentStarted((info) => {
  createSubagentTab(info.unique_name, info.agent_name, info.is_sync);
});

window.electronAPI.onSubagentEvent(({ unique_name, event }) => {
  const tab = _subagentTabs[unique_name];
  if (!tab) return;

  switch (event.type) {
    case 'tool_status':
      // 工具调用进度
      const status = event.status === 'start' ? '🔧' : '✅';
      addSubagentMessageToTab(unique_name, 'system', `${status} ${event.tool_name}${event.summary ? ' — ' + event.summary : ''}`);
      break;

    case 'thinking_chain':
      // thinking chain（折叠显示）
      const thinkDiv = document.createElement('div');
      thinkDiv.className = 'message thinking';
      thinkDiv.style.cssText = 'font-size:12px;color:#888;border-left:2px solid #ccc;padding-left:8px;margin:4px 0;white-space:pre-wrap;';
      thinkDiv.textContent = event.content;
      tab.messagesDiv.appendChild(thinkDiv);
      tab.messagesDiv.scrollTop = tab.messagesDiv.scrollHeight;
      break;

    case 'reply':
      addSubagentMessageToTab(unique_name, 'assistant', event.content);
      break;

    case 'question':
      // 子 Agent @user 提问 — 高亮显示
      const qDiv = document.createElement('div');
      qDiv.className = 'message question';
      qDiv.style.cssText = 'background:rgba(255,193,7,0.15);border:1px solid rgba(255,193,7,0.4);border-radius:8px;padding:8px 12px;margin:4px 0;';
      qDiv.innerHTML = `<strong>🤔 子 Agent 提问：</strong>${marked.parse(event.content)}`;
      tab.messagesDiv.appendChild(qDiv);
      tab.messagesDiv.scrollTop = tab.messagesDiv.scrollHeight;
      // 如果不是当前 tab，加红点
      if (_activeTab !== unique_name) {
        tab.tabEl.classList.add('has-update');
      }
      break;

    case 'persist':
    case 'system':
      addSubagentMessageToTab(unique_name, 'system', event.content);
      break;

    case 'subagent_closed':
      closeSubagentTab(unique_name);
      addSubagentMessageToTab(unique_name, 'system', '子 Agent 已结束');
      break;

    case 'error':
      markSubagentError(unique_name);
      addSubagentMessageToTab(unique_name, 'system', `子 Agent 异常终止: ${event.message || ''}`);
      break;
  }
});
```

- [ ] **Step 2: 提交**

```bash
git add ui/main/windows/assistant/chat.html
git commit -m "feat: chat.html subagent event rendering — tool status, thinking chain, question, close"
```

---

### Task 4: 窗口恢复 + 异常处理

**Files:**
- Modify: `ui/main/windows/assistant/chat.html` (窗口恢复逻辑)
- Modify: `ui/main/main.js` (chatWindow.on('show'/'focus') 触发恢复)

**参考代码位置:**
- `chat.html` L2247-2268: `onSyncState` 回调 — 窗口恢复时同步状态
- `chat.html` L1068-1080: `checkRunningSubagents()` — 调 `/api/subagents/running`
- `main.js` L262-272: `chatWindow.on('show'/'focus')` — 发送 sync-state

- [ ] **Step 1: 窗口恢复时重建子 Agent tab**

在 `chat.html` 的 `onSyncState` 回调中，或新增独立的恢复函数:

```javascript
async function restoreSubagentTabs() {
  try {
    const resp = await fetch('/api/subagents/running');
    const data = await resp.json();
    if (data.count === 0) return;

    for (const sub of data.subagents) {
      // tab 已存在则跳过
      if (_subagentTabs[sub.unique_name]) continue;

      // 创建 tab（不自动切换）
      createSubagentTab(sub.unique_name, sub.agent_type, sub.is_sync);

      // 如果 state 是已结束（不在 running 列表里但 ring buffer 还在），标记完成
      if (sub.state === 'completed' || sub.state === 'error') {
        closeSubagentTab(sub.unique_name);
      }

      // 重新建立 SSE 连接（main.js 负责）
      // 通过 IPC 通知 main.js 连接
      window.electronAPI.sendMessage && window.electronAPI.connectSubagentSSE(sub.unique_name);
    }
  } catch (e) {
    console.error('恢复子 Agent tab 失败:', e);
  }
}

// 在 onSyncState 回调中调用
window.electronAPI.onSyncState(() => {
  getChatStatus();
  restoreSubagentTabs();
});
```

- [ ] **Step 2: main.js 新增 connectSubagentSSE IPC**

`main.js` 新增 IPC handler:

```javascript
ipcMain.on('connect-subagent-sse', (_event, unique_name) => {
  SubagentSSEManager.connect(unique_name);
});
```

`preload-chat.js` 新增:
```javascript
connectSubagentSSE: (unique_name) => ipcRenderer.send('connect-subagent-sse', unique_name),
```

- [ ] **Step 3: clearChat 不清空子 Agent tab 消息**

`chat.html` L1345-1361 的 `clearChat()` 函数，确保只清空主 #messages:

```javascript
function clearChat() {
  window.electronAPI.clearChat();
  // 只清空主对话 messages，不清空子 Agent tab
  document.getElementById('messages').innerHTML = '';
}
```

- [ ] **Step 4: 提交**

```bash
git add ui/main/windows/assistant/chat.html ui/main/main.js ui/main/preload-chat.js
git commit -m "feat: window restore subagent tabs + connectSubagentSSE IPC + clearChat isolation"
```
