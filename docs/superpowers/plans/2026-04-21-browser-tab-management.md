# 浏览器标签页管理实现方案

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标：** 为 browser-server 添加多标签页管理能力，使 LLM 能够跟踪、切换和关闭标签页——对齐 page-agent 的 TabsController 能力。

**架构：** Hub.js 维护 `tabs[]` 数组和 `currentTabId`，监听 Chrome 标签页生命周期事件，通过 WebSocket 暴露标签页操作。Python MCP 工具（`browser_switch_tab`、`browser_close_tab`）通过同一 WS bridge 调用。每次操作响应都包含标签页摘要表格（page-agent 的做法：`summarizeTabs()` 附加到响应中）。

**技术栈：** Chrome Extension (Manifest V3) + WebSocket + Python MCP Server (ToolRegistry 同进程)

---

## 文件结构

| 文件 | 职责 | 变更类型 |
|------|------|----------|
| `extensions/niu-browser-ext/hub.js` | 标签页状态管理、标签页命令、标签页事件监听、summarizeTabs | **重大修改** |
| `extensions/niu-browser-ext/background.js` | 通过 Port 转发标签页生命周期事件（onCreated, onRemoved, onUpdated）到 hub | **重大修改** |
| `mcp-servers/browser-server/src/niu_browser_server/__init__.py` | 新增 MCP 工具（switch_tab, close_tab），更新 schema，所有响应包含标签页摘要 | **重大修改** |
| `mcp-servers/browser-server/src/niu_browser_server/ws_bridge.py` | 无需修改 — 现有 send_command 已能处理新命令 | **无** |
| `config/mcp-servers.yaml` | 新增工具可见性配置 | **小改** |

---

## 设计参考：page-agent TabsController

page-agent 的方案（根据我们的场景简化）：

```
TabsController {
  tabs: TabMeta[]          // 跟踪的标签页
  currentTabId: number     // 当前活跃标签页
  windowId: number         // 浏览器窗口

  openNewTab(url)          // 创建 + 加入 tabs + switchToTab
  switchToTab(tabId)       // 激活标签页 + 更新 currentTabId
  closeTab(tabId)          // 移除标签页 + 切换到另一个
  summarizeTabs()          // 返回 markdown 表格给 LLM

  connectTabEvents()       // 通过 Port 监听 created/removed/updated
}
```

关键洞察：`summarizeTabs()` 附加到每次 `getBrowserState()` 响应中，LLM 始终能看到标签页列表。

---

### 任务 1：在 background.js 中添加标签页事件转发

**文件：**
- 修改：`extensions/niu-browser-ext/background.js`

当前 background.js 只通过 `chrome.runtime.onMessage` 转发 `tab_updated` 和 `tab_created`。这不可靠，因为：
1. Hub.js 使用 `chrome.runtime.onMessage`，SW 重启期间可能丢失消息
2. 没有转发 `tab_removed` 事件

切换为 **Port-based** 事件转发（与 page-agent v1.7.1+ 相同），更可靠且能经受 SW 重启。

- [ ] **步骤 1：将 onMessage 转发替换为 Port-based 标签页事件**

替换整个 `background.js` 内容：

```javascript
/**
 * Niu Browser Extension - Background Service Worker
 * - 标签页生命周期事件通过 Port 转发（跨 SW 重启可靠）
 * - 消息路由：hub <-> content_script
 * - 启动时自动打开 hub 标签页
 */

const HUB_URL = chrome.runtime.getURL('hub.html');
let hubTabId = null;

// ============== Hub 标签页生命周期 ==============

function ensureHubTab() {
  chrome.tabs.query({ url: HUB_URL }, (tabs) => {
    if (tabs.length > 0) {
      hubTabId = tabs[0].id;
    } else {
      chrome.tabs.create({ url: HUB_URL, active: false }, (tab) => {
        hubTabId = tab.id;
      });
    }
  });
}

ensureHubTab();
chrome.runtime.onInstalled.addListener(() => ensureHubTab());
chrome.action.onClicked.addListener(() => ensureHubTab());

chrome.tabs.onRemoved.addListener((tabId) => {
  if (tabId === hubTabId) {
    hubTabId = null;
    setTimeout(() => {
      chrome.tabs.create({ url: HUB_URL, active: false }, (tab) => {
        hubTabId = tab.id;
      });
    }, 1000);
  }
});

// ============== 标签页事件 Port ==============
// Hub 通过 chrome.runtime.connect({ name: 'tab-events' }) 连接
// 可靠地接收标签页生命周期事件（经受 SW 重启）

const tabEventPorts = new Set();

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== 'tab-events') return;
  tabEventPorts.add(port);
  port.onDisconnect.addListener(() => tabEventPorts.delete(port));
});

function broadcastTabEvent(message) {
  for (const port of tabEventPorts) {
    try { port.postMessage(message); } catch (e) { /* port 已关闭 */ }
  }
}

chrome.tabs.onCreated.addListener((tab) => {
  broadcastTabEvent({ action: 'created', payload: { tab } });
});

chrome.tabs.onRemoved.addListener((tabId, removeInfo) => {
  broadcastTabEvent({ action: 'removed', payload: { tabId, removeInfo } });
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  // 只广播有意义的更新（url、title、status 变化）
  if (changeInfo.url || changeInfo.title || changeInfo.status) {
    broadcastTabEvent({ action: 'updated', payload: { tabId, changeInfo, tab } });
  }
});

// ============== 消息路由 ==============

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.target === 'content') {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      if (tabs[0]) {
        chrome.tabs.sendMessage(tabs[0].id, msg, (response) => {
          sendResponse(response);
        });
      } else {
        sendResponse({ success: false, message: 'No active tab' });
      }
    });
    return true;
  }
});
```

- [ ] **步骤 2：提交**

```bash
git add extensions/niu-browser-ext/background.js
git commit -m "refactor: background.js 改用 Port 转发标签页事件（替代 onMessage 转发）"
```

---

### 任务 2：在 hub.js 中添加标签页状态管理

**文件：**
- 修改：`extensions/niu-browser-ext/hub.js`

这是核心变更。添加：
1. `tabs[]` 数组和 `currentTabId` 状态
2. `connectTabEvents()` — 监听来自 background.js 的 Port 事件
3. `summarizeTabs()` — 生成 markdown 表格给 LLM
4. `handleSwitchTab()`、`handleCloseTab()` 命令处理器
5. 修改 `handleNavigate()` 和 `handleClick()` 使用 `currentTabId` 而非 `getActiveTabId()`
6. 修改 `create_tab` 将新标签页加入 `tabs[]` 并自动切换
7. 在所有响应中包含标签页摘要

- [ ] **步骤 1：在 hub.js 顶部添加标签页状态变量和辅助函数**

在现有 `let ws = null; let reconnectTimer = null;` 之后添加：

```javascript
// ============== 标签页状态管理 ==============
// 参照 page-agent TabsController

let tabs = [];          // Array of { id, url, title, status, isInitial }
let currentTabId = null; // 当前活跃标签页 ID
let windowId = null;     // 浏览器窗口 ID
let tabEventPort = null; // 接收标签页生命周期事件的 Port

function isInternalUrl(url) {
  return !url || url.startsWith('chrome://') || url.startsWith('edge://') ||
    url.startsWith('chrome-extension://') || url === 'about:blank';
}

function isContentScriptAllowed(url) {
  if (!url) return false;
  return !isInternalUrl(url) && !url.startsWith('about:') &&
    !url.startsWith('file://') && !url.startsWith('view-source:') &&
    !url.startsWith('devtools://');
}

function addTab(meta) {
  if (tabs.find(t => t.id === meta.id)) return;
  tabs.push(meta);
}

function removeTab(tabId) {
  tabs = tabs.filter(t => t.id !== tabId);
}

function updateTab(tabId, updates) {
  const tab = tabs.find(t => t.id === tabId);
  if (tab) Object.assign(tab, updates);
}

async function switchToTab(tabId) {
  const target = tabs.find(t => t.id === tabId);
  if (!target) throw new Error('Tab ' + tabId + ' not found in tab list.');

  currentTabId = tabId;
  await chrome.tabs.update(tabId, { active: true });
}

/**
 * 生成标签页摘要表格给 LLM（page-agent summarizeTabs 模式）。
 * 附加到每次响应，LLM 始终知道可用的标签页。
 */
function summarizeTabs() {
  if (!tabs.length) return 'No tabs open.';

  const lines = ['| TabID | Title | URL |', '|-------|-------|-----|'];
  for (const tab of tabs) {
    const marker = tab.id === currentTabId ? ' *' : '';
    const title = (tab.title || '').substring(0, 40);
    const url = (tab.url || '').substring(0, 60);
    lines.push('| ' + tab.id + marker + ' | ' + title + ' | ' + url + ' |');
  }
  return lines.join('\n');
}
```

- [ ] **步骤 2：添加 connectTabEvents() 函数**

在 `summarizeTabs()` 之后添加：

```javascript
/**
 * 通过 Port 连接到 background SW 接收标签页生命周期事件。
 * 断开时自动重连（SW 可能重启）。
 */
function connectTabEvents() {
  if (tabEventPort) {
    try { tabEventPort.disconnect(); } catch (e) {}
  }

  tabEventPort = chrome.runtime.connect({ name: 'tab-events' });

  tabEventPort.onMessage.addListener((message) => {
    if (message.action === 'created') {
      const tab = message.payload.tab;
      if (tab.id && tab.windowId === windowId && isContentScriptAllowed(tab.url)) {
        addTab({ id: tab.id, url: tab.url, title: tab.title, status: tab.status, isInitial: false });
        // 自动切换到新创建的标签页
        currentTabId = tab.id;
      }
    } else if (message.action === 'removed') {
      const { tabId } = message.payload;
      const existed = tabs.find(t => t.id === tabId);
      if (existed) {
        removeTab(tabId);
        if (currentTabId === tabId) {
          // 切换到最后一个剩余标签页
          const newCurrent = tabs[tabs.length - 1] || null;
          currentTabId = newCurrent ? newCurrent.id : null;
        }
      }
    } else if (message.action === 'updated') {
      const { tabId, changeInfo } = message.payload;
      const updates = {};
      if (changeInfo.url) updates.url = changeInfo.url;
      if (changeInfo.title) updates.title = changeInfo.title;
      if (changeInfo.status) updates.status = changeInfo.status;
      updateTab(tabId, updates);
    }
  });

  tabEventPort.onDisconnect.addListener(() => {
    tabEventPort = null;
    // 短暂延迟后重连（SW 可能已重启）
    setTimeout(connectTabEvents, 1000);
  });
}
```

- [ ] **步骤 3：在首次 WS 连接成功时初始化标签页跟踪**

在 `ws.onopen` 处理器中，`ws.send(JSON.stringify({ type: 'ready' }))` 之后添加：

```javascript
    // 首次连接时初始化标签页跟踪
    if (tabs.length === 0) {
      initTabTracking();
    }
```

添加 `initTabTracking()` 函数：

```javascript
async function initTabTracking() {
  try {
    const activeTabs = await chrome.tabs.query({ active: true, currentWindow: true });
    if (activeTabs[0]) {
      windowId = activeTabs[0].windowId;
      currentTabId = activeTabs[0].id;

      // 如果活跃标签页是真实页面，加入跟踪
      if (isContentScriptAllowed(activeTabs[0].url)) {
        addTab({
          id: activeTabs[0].id,
          url: activeTabs[0].url,
          title: activeTabs[0].title,
          status: activeTabs[0].status,
          isInitial: true,
        });
      }
    }

    // 连接标签页生命周期事件
    connectTabEvents();
  } catch (e) {
    console.error('[NiuHub] Tab tracking init failed:', e);
  }
}
```

- [ ] **步骤 4：添加 switch_tab 和 close_tab 命令处理器**

在 `handleCommand()` 中，在 `else`（转发到 content_script）分支之前添加新 case：

```javascript
    } else if (type === 'switch_tab') {
      const tabId = msg.tabId;
      if (!tabId) {
        sendResult(id, false, 'tabId is required');
        return;
      }
      try {
        await switchToTab(tabId);
        // 获取切换后标签页的状态
        const stateResult = await sendToContentScriptWithRetry(tabId, { type: 'get_state', id: id });
        sendResult(id, true, 'Switched to tab ' + tabId, stateResult?.data);
      } catch (e) {
        sendResult(id, false, e.message);
      }
    } else if (type === 'close_tab') {
      const tabId = msg.tabId;
      if (!tabId) {
        sendResult(id, false, 'tabId is required');
        return;
      }
      const target = tabs.find(t => t.id === tabId);
      if (!target) {
        sendResult(id, false, 'Tab ' + tabId + ' not found in tab list');
        return;
      }
      if (target.isInitial) {
        sendResult(id, false, 'Cannot close the initial tab');
        return;
      }
      await chrome.tabs.remove(tabId);
      removeTab(tabId);
      // 自动切换到最后一个剩余标签页
      if (currentTabId === tabId) {
        const newCurrent = tabs[tabs.length - 1] || null;
        if (newCurrent) {
          await switchToTab(newCurrent.id);
        } else {
          currentTabId = null;
        }
      }
      sendResult(id, true, 'Closed tab ' + tabId);
    } else if (type === 'list_tabs') {
      // 返回完整标签页列表
      const tabDetails = [];
      for (const tab of tabs) {
        tabDetails.push({
          id: tab.id,
          url: tab.url || '',
          title: tab.title || '',
          status: tab.status || '',
          isCurrent: tab.id === currentTabId,
        });
      }
      sendResult(id, true, 'Tab list', { tabs: tabDetails, summary: summarizeTabs() });
```

- [ ] **步骤 5：修改 create_tab 处理器以跟踪新标签页**

在现有 `create_tab` 处理器中，`const newTab = await chrome.tabs.create(...)` 之后添加：

```javascript
      // 跟踪新标签页
      addTab({ id: newTab.id, url: url, title: '', status: 'loading', isInitial: false });
      currentTabId = newTab.id;
```

- [ ] **步骤 6：修改 navigate 处理器以跟踪标签页**

在 `handleNavigate()` 中，当创建新标签页时（内部 URL 的 `else` 分支），添加标签页跟踪：

```javascript
        // 跟踪新标签页
        addTab({ id: newTab.id, url: url, title: '', status: 'loading', isInitial: false });
        currentTabId = newTab.id;
```

- [ ] **步骤 7：在所有响应中包含标签页摘要**

修改 `sendResult()` 自动包含标签页摘要：

```javascript
function sendResult(id, success, message, data) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    // 每次响应都包含标签页摘要（page-agent 模式）
    const enrichedData = data || {};
    if (typeof enrichedData === 'object' && success) {
      enrichedData.tabSummary = summarizeTabs();
      enrichedData.currentTabId = currentTabId;
    }
    ws.send(JSON.stringify({ type: 'result', id: id, success: success, message: message, data: enrichedData }));
  }
}
```

- [ ] **步骤 8：移除旧的 onMessage 标签页事件监听器**

移除 hub.js 底部监听 `tab_updated` / `tab_created` 的旧 `chrome.runtime.onMessage.addListener` 代码块，因为现在使用 Port-based 事件。

- [ ] **步骤 9：移除旧的 `isInternalUrl` 函数**

新的 `isInternalUrl` 已在顶部添加。移除原来约第 61 行定义的旧版本。

- [ ] **步骤 10：提交**

```bash
git add extensions/niu-browser-ext/hub.js
git commit -m "feat: hub.js 标签页状态管理（tabs[], switchToTab, closeTab, summarizeTabs）"
```

---

### 任务 3：在 Python 中添加新 MCP 工具

**文件：**
- 修改：`mcp-servers/browser-server/src/niu_browser_server/__init__.py`

添加 `browser_switch_tab` 和 `browser_close_tab` 工具。修改所有现有工具的返回值以包含 `tabSummary` 和 `currentTabId`。

- [ ] **步骤 1：添加 browser_switch_tab 函数**

在 `browser_new_tab()` 之后添加：

```python
def browser_switch_tab(
    tab_id: int,
) -> dict:
    """
    切换到指定标签页。

    Args:
        tab_id: 要切换到的标签页 ID（来自之前响应中的 tabSummary）

    Returns:
        切换后标签页的页面状态
    """
    try:
        bridge = _ensure_connection()
        result = bridge.send_command("switch_tab", tabId=tab_id, timeout=30)

        if result.get("success"):
            data = result.get("data") or {}
            return {
                "status": "success",
                "message": f"Switched to tab {tab_id}",
                "url": data.get("url", ""),
                "title": data.get("title", ""),
                "elements": data.get("elements", ""),
                "pageInfo": data.get("pageInfo", {}),
                "tabSummary": data.get("tabSummary", ""),
                "currentTabId": data.get("currentTabId"),
            }
        else:
            return {"status": "error", "message": result.get("message", "Unknown error")}

    except Exception as e:
        logger.error(f"browser_switch_tab failed: {e}")
        return {"status": "error", "message": str(e)}
```

- [ ] **步骤 2：添加 browser_close_tab 函数**

```python
def browser_close_tab(
    tab_id: int,
) -> dict:
    """
    关闭指定标签页。不能关闭初始标签页。

    Args:
        tab_id: 要关闭的标签页 ID（来自之前响应中的 tabSummary）

    Returns:
        关闭结果和更新后的标签页摘要
    """
    try:
        bridge = _ensure_connection()
        result = bridge.send_command("close_tab", tabId=tab_id, timeout=30)

        if result.get("success"):
            data = result.get("data") or {}
            return {
                "status": "success",
                "message": f"Closed tab {tab_id}",
                "tabSummary": data.get("tabSummary", ""),
                "currentTabId": data.get("currentTabId"),
            }
        else:
            return {"status": "error", "message": result.get("message", "Unknown error")}

    except Exception as e:
        logger.error(f"browser_close_tab failed: {e}")
        return {"status": "error", "message": str(e)}
```

- [ ] **步骤 3：更新现有工具返回值以包含 tabSummary**

修改 `browser_navigate`、`browser_interact` 和 `browser_new_tab` 的返回字典，从 Extension 响应中包含 `tabSummary` 和 `currentTabId`。在每个成功返回中添加：

```python
"tabSummary": data.get("tabSummary", ""),
"currentTabId": data.get("currentTabId"),
```

- [ ] **步骤 4：添加新工具的 schema**

在 `TOOL_SCHEMAS` 字典中添加：

```python
    "browser_switch_tab": {
        "name": "browser_switch_tab",
        "description": "切换到指定标签页。当需要操作非当前标签页时使用。tabId 来自之前响应中的 tabSummary 表格。",
        "input_schema": {
            "type": "object",
            "properties": {
                "tab_id": {"type": "integer", "description": "要切换到的标签页 ID（来自 tabSummary）"}
            },
            "required": ["tab_id"]
        }
    },
    "browser_close_tab": {
        "name": "browser_close_tab",
        "description": "关闭指定标签页。不能关闭初始标签页。关闭后自动切换到最后一个剩余标签页。",
        "input_schema": {
            "type": "object",
            "properties": {
                "tab_id": {"type": "integer", "description": "要关闭的标签页 ID（来自 tabSummary）"}
            },
            "required": ["tab_id"]
        }
    },
```

- [ ] **步骤 5：提交**

```bash
git add mcp-servers/browser-server/src/niu_browser_server/__init__.py
git commit -m "feat: 添加 browser_switch_tab 和 browser_close_tab MCP 工具"
```

---

### 任务 4：更新 MCP 服务器配置

**文件：**
- 修改：`config/mcp-servers.yaml`

- [ ] **步骤 1：添加新工具的可见性配置**

更新 browser-server 的 tools 部分：

```yaml
browser-server:
  command: ${PYTHON_PATH}
  args:
    - "-m"
    - "niu_browser_server"
  workdir: mcp-servers/browser-server/src
  preload: false  # 按需启动，首次使用 ~2 秒启动浏览器
  tools:
    browser_navigate: {visibility: static}
    browser_switch_tab: {visibility: dynamic}
    browser_close_tab: {visibility: dynamic}
    # browser_interact, browser_new_tab 默认 dynamic
```

- [ ] **步骤 2：提交**

```bash
git add config/mcp-servers.yaml
git commit -m "feat: 添加 browser_switch_tab/close_tab 可见性配置"
```

---

### 任务 5：更新 browser_navigate 描述以提及标签页摘要

**文件：**
- 修改：`mcp-servers/browser-server/src/niu_browser_server/__init__.py`

- [ ] **步骤 1：更新 browser_navigate 工具描述**

修改描述以告知 LLM 响应中包含标签页摘要：

```python
"description": "启动浏览器并导航到 URL，自动返回页面结构化状态和标签页列表。LLM 根据返回的元素编号决策下一步操作，根据 tabSummary 管理多个标签页。**使用场景**：用户要求'打开网页'、'访问网站'、'浏览页面'时使用。**返回**：url、title、elements（编号的交互元素）、tabSummary（标签页列表）、currentTabId。",
```

- [ ] **步骤 2：更新 browser_interact 工具描述**

```python
"description": "与页面元素交互（按索引）：点击、输入文本、选择下拉项、滚动、获取当前状态。每次操作返回更新后的页面状态（含重新编号的元素和标签页摘要）。操作是串行的——始终使用上一次结果的最新索引。",
```

- [ ] **步骤 3：提交**

```bash
git add mcp-servers/browser-server/src/niu_browser_server/__init__.py
git commit -m "docs: 更新浏览器工具描述，提及 tabSummary"
```

---

## 自检

### 需求覆盖检查

| 需求 | 任务 |
|------|------|
| 标签页状态管理（tabs[], currentTabId） | 任务 2 |
| 标签页生命周期事件（created/removed/updated） | 任务 1 + 任务 2 |
| switchToTab 操作 | 任务 2（hub.js）+ 任务 3（Python） |
| closeTab 操作 | 任务 2（hub.js）+ 任务 3（Python） |
| 每次响应包含 summarizeTabs | 任务 2（sendResult 增强） |
| MCP 工具 schema | 任务 3 |
| MCP 配置可见性 | 任务 4 |
| 工具描述提及标签页 | 任务 5 |

### 占位符扫描

未发现 TBD、TODO 或占位符模式。所有代码都是具体的。

### 类型一致性检查

- `tabId` 在 hub.js 命令和 Python kwargs 中一致使用
- `tabSummary` 字符串在所有工具响应中一致返回
- `currentTabId` 整数在所有响应中一致返回
- `tabs[]` 数组结构：`{ id, url, title, status, isInitial }` — 在 addTab 调用和 summarizeTabs 迭代之间一致

### 与 page-agent 的关键差异

| 特性 | page-agent | 我们的实现 | 原因 |
|------|-----------|-----------|------|
| 标签页分组 | 有（`createTabGroup`） | 无 | 单 agent 使用不需要此复杂度 |
| `experimentalIncludeAllTabs` | 有 | 无 | 我们只跟踪自己创建的标签页和初始标签页 |
| `isInitial` 标签页保护 | 有 | 有 | 防止关闭第一个标签页 |
| Port-based 事件 | 有 | 有 | 相同方案，保证可靠性 |
| `summarizeTabs()` 在 header 中 | 有 | 有（在 `tabSummary` 字段中） | 相同模式，字段名略有不同以适配我们的 JSON 响应结构 |
