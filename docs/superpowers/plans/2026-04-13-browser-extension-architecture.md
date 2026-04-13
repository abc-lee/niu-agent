# Chrome Extension 浏览器自动化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 Chrome Extension 替代 Playwright 控制浏览器，实现结构化网页状态提取和交互操作，让 LLM 能直接看到编号的交互元素并决策操作。

**Architecture:** 基于 page-agent 的 page-controller 核心代码，改造为我们自己的 Chrome Extension。Extension 的 content_script 在页面内遍历 DOM、给交互元素编号、模拟鼠标操作；通过 WebSocket 与 Python 后端通讯，接收操作指令、返回结构化页面状态。Python 端只负责启动浏览器和转发指令，不再需要 Playwright/BrowserManager/CDP。

**Tech Stack:** Chrome Extension (Manifest V3), WebSocket, Python (websockets/aiohttp), page-controller DOM 引擎 (JS)

---

## 问题分析

### 当前方案的根因问题

当前 browser-server 用 Playwright 控制浏览器，`browser_navigate` 只返回 `{"status": "success"}`，LLM 看不到页面内容。LLM 需要写 code_run 通过 CDP 连接浏览器、写 Playwright 代码操作页面，但这经常失败（选择器不对、print 不够、新标签页处理不了）。

### 新方案的核心思路

借鉴 page-agent 的 page-controller：**Extension 常驻浏览器页面，自动提取 DOM 状态并编号交互元素，通过 WebSocket 返回给系统**。LLM 拿到的不再是空返回，而是：

```
[0]<a aria-label=首页 />
[1]<input type=text name=username placeholder=请输入用户名 />
[2]<input type=password name=password />
[3]<button type=submit>登录 />
```

LLM 直接决策"点击 index=3"，系统通过 WebSocket 告诉 Extension 执行 `selectorMap.get(3).click()`。

---

## 文件结构

```
extensions/
└── niu-browser-ext/                    # 我们的 Chrome Extension
    ├── manifest.json                   # Extension 配置
    ├── background.js                   # Service Worker：消息路由
    ├── content.js                      # Content Script：DOM 提取 + 操作执行
    ├── dom_tree.js                     # page-controller 的 DOM 引擎（移植自 page-agent）
    ├── hub.html                        # Extension 页面：WebSocket 连接
    ├── hub.js                          # hub.html 的脚本：WS 通讯
    └── icons/                          # Extension 图标

mcp-servers/browser-server/src/niu_browser_server/
├── __init__.py                         # MCP 工具：browser_navigate（重写）
├── ws_bridge.py                        # WebSocket 服务端：与 Extension 通讯
└── launcher.py                         # 浏览器启动器：找默认浏览器 + 启动

docs/
└── SYSTEM_MANUAL.md                   # 系统管理手册（添加浏览器插件章节）

memory/skills/
└── browser-automation.md              # 更新 Skill 文档
```

---

## 任务 1：移植 page-controller DOM 引擎为独立 JS 文件

**文件：**
- 创建：`extensions/niu-browser-ext/dom_tree.js`

**说明：** 从 page-agent 的 `packages/page-controller/src/dom/dom_tree/index.js`（1753行）移植核心 DOM 遍历和交互元素检测代码。需要修改：
1. 移除 `export default` 包裹，改为全局函数 `buildDomTree()`
2. 移除 `extraData` WeakMap 和 `addExtraData`（我们不需要 extra 字段）
3. 移除 `interactiveBlacklist`/`interactiveWhitelist`（简化）
4. 保留核心：`isInteractiveCandidate()`, `isInteractiveElement()`, `isElementDistinctInteraction()`, `buildDomTree()`, highlightIndex 分配
5. 保留视觉高亮（可选，通过参数控制）

- [ ] **步骤 1：读取 page-agent 源码**

读取 `E:\tools\page-agent\packages\page-controller\src\dom\dom_tree\index.js`，理解完整结构。

- [ ] **步骤 2：创建 dom_tree.js**

将核心代码移植到 `extensions/niu-browser-ext/dom_tree.js`，暴露全局函数：

```javascript
// 全局函数，供 content.js 调用
window.NiuDomTree = {
  /**
   * 构建扁平化 DOM 树，给交互元素编号
   * @returns {Object} { rootId, map } - FlatDomTree
   */
  buildFlatTree(options = {}) {
    // 调用移植的 buildDomTree 核心逻辑
    // options.doHighlightElements: 是否显示视觉高亮（默认 true）
    // options.viewportExpansion: 视口扩展（默认 -1，全页面）
  },

  /**
   * 将 FlatDomTree 序列化为 LLM 可读的文本
   * @param {Object} flatTree - buildFlatTree() 的返回值
   * @returns {string} 编号的交互元素列表
   */
  flatTreeToString(flatTree) {
    // 移植自 page-controller/src/dom/index.ts 的 flatTreeToString()
  },

  /**
   * 获取交互元素映射：index → DOM 元素引用
   * @param {Object} flatTree - buildFlatTree() 的返回值
   * @returns {Map<number, HTMLElement>}
   */
  getSelectorMap(flatTree) {
    // 移植自 page-controller/src/dom/index.ts 的 getSelectorMap()
  },

  /**
   * 清理视觉高亮
   */
  cleanUpHighlights() {
    // 移植自 page-controller/src/dom/index.ts 的 cleanUpHighlights()
  }
};
```

- [ ] **步骤 3：验证 dom_tree.js 在浏览器中可用**

在 Chrome DevTools Console 中测试：打开任意网页，粘贴 dom_tree.js 代码，执行 `NiuDomTree.buildFlatTree()`，确认返回带编号的交互元素。

- [ ] **步骤 4：提交**

```bash
git add extensions/niu-browser-ext/dom_tree.js
git commit -m "feat: 移植 page-controller DOM 引擎为独立 JS 文件"
```

---

## 任务 2：创建 content_script（DOM 提取 + 操作执行）

**文件：**
- 创建：`extensions/niu-browser-ext/content.js`

**说明：** Content Script 注入到每个网页，负责：
1. 监听来自 background.js 的消息
2. 执行 DOM 状态提取（调用 dom_tree.js）
3. 执行交互操作（点击、填充、选择、滚动、导航）
4. 模拟鼠标移动（dispatch PointerEvent）
5. 返回结果给 background.js

- [ ] **步骤 1：创建 content.js**

```javascript
/**
 * Niu Browser Extension - Content Script
 * 注入到每个网页，负责 DOM 状态提取和交互操作执行。
 */

// DOM 状态缓存
let lastFlatTree = null;
let lastSelectorMap = null;
let lastSimplifiedHTML = '';

// 监听来自 background 的消息
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  handleMessage(msg).then(sendResponse);
  return true; // 保持消息通道开放（异步响应）
});

async function handleMessage(msg) {
  switch (msg.type) {
    case 'get_state':
      return getBrowserState();
    case 'click':
      return clickElement(msg.index);
    case 'input_text':
      return inputText(msg.index, msg.text);
    case 'select_option':
      return selectOption(msg.index, msg.option);
    case 'scroll':
      return scroll(msg.direction, msg.amount);
    case 'navigate':
      return navigate(msg.url);
    case 'screenshot':
      return takeScreenshot();
    default:
      return { success: false, message: `Unknown command: ${msg.type}` };
  }
}

/**
 * 获取结构化浏览器状态
 * 返回 url, title, elements（编号的交互元素列表）, pageText（页面文本摘要）
 */
function getBrowserState() {
  try {
    // 清理旧高亮
    NiuDomTree.cleanUpHighlights();

    // 构建新的 DOM 树
    lastFlatTree = NiuDomTree.buildFlatTree({ doHighlightElements: true });
    lastSelectorMap = NiuDomTree.getSelectorMap(lastFlatTree);
    lastSimplifiedHTML = NiuDomTree.flatTreeToString(lastFlatTree);

    // 获取页面信息
    const pageInfo = getPageInfo();

    return {
      success: true,
      data: {
        url: window.location.href,
        title: document.title,
        elements: lastSimplifiedHTML,
        pageInfo: pageInfo,
      }
    };
  } catch (e) {
    return { success: false, message: `Failed to get state: ${e.message}` };
  }
}

/**
 * 点击元素（模拟真实鼠标事件）
 */
function clickElement(index) {
  try {
    const element = lastSelectorMap?.get(index);
    if (!element) {
      return { success: false, message: `Element ${index} not found. Call get_state first.` };
    }

    // 滚动到可见区域
    element.scrollIntoView({ behavior: 'smooth', block: 'center' });

    // 模拟鼠标事件序列（比 .click() 更真实）
    const rect = element.getBoundingClientRect();
    const x = rect.x + rect.width / 2;
    const y = rect.y + rect.height / 2;

    const events = ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'];
    for (const eventType of events) {
      element.dispatchEvent(new PointerEvent(eventType, {
        bubbles: true,
        cancelable: true,
        clientX: x,
        clientY: y,
        pointerId: 1,
        pointerType: 'mouse',
        isPrimary: true,
      }));
    }

    // 等待页面响应后返回新状态
    return new Promise(resolve => {
      setTimeout(() => resolve(getBrowserState()), 500);
    });
  } catch (e) {
    return { success: false, message: `Click failed: ${e.message}` };
  }
}

/**
 * 输入文本（React/Vue 兼容）
 */
function inputText(index, text) {
  try {
    const element = lastSelectorMap?.get(index);
    if (!element) {
      return { success: false, message: `Element ${index} not found.` };
    }

    element.scrollIntoView({ behavior: 'smooth', block: 'center' });
    element.focus();

    // 使用原生 value setter（React 兼容）
    const nativeSetter = Object.getOwnPropertyDescriptor(
      Object.getPrototypeOf(element), 'value'
    )?.set;
    if (nativeSetter) {
      nativeSetter.call(element, text);
    } else {
      element.value = text;
    }

    // 触发 input 和 change 事件
    element.dispatchEvent(new Event('input', { bubbles: true }));
    element.dispatchEvent(new Event('change', { bubbles: true }));

    return { success: true, message: `Input "${text}" into element ${index}` };
  } catch (e) {
    return { success: false, message: `Input failed: ${e.message}` };
  }
}

/**
 * 选择下拉选项
 */
function selectOption(index, optionText) {
  try {
    const element = lastSelectorMap?.get(index);
    if (!element || element.tagName !== 'SELECT') {
      return { success: false, message: `Element ${index} is not a select.` };
    }

    element.value = optionText;
    element.dispatchEvent(new Event('change', { bubbles: true }));

    return { success: true, message: `Selected "${optionText}" in element ${index}` };
  } catch (e) {
    return { success: false, message: `Select failed: ${e.message}` };
  }
}

/**
 * 滚动页面
 */
function scroll(direction, amount = 1) {
  const pixels = amount * window.innerHeight;
  if (direction === 'down') window.scrollBy(0, pixels);
  else if (direction === 'up') window.scrollBy(0, -pixels);

  return new Promise(resolve => {
    setTimeout(() => resolve(getBrowserState()), 300);
  });
}

/**
 * 导航到 URL
 */
function navigate(url) {
  window.location.href = url;
  // 导航后页面会重新加载，content_script 会重新注入
  // 返回值由新页面的 get_state 提供
  return { success: true, message: `Navigating to ${url}` };
}

/**
 * 获取页面几何信息
 */
function getPageInfo() {
  return {
    viewportWidth: window.innerWidth,
    viewportHeight: window.innerHeight,
    pageWidth: document.documentElement.scrollWidth,
    pageHeight: document.documentElement.scrollHeight,
    scrollX: window.scrollX,
    scrollY: window.scrollY,
    pixelsBelow: Math.max(0, document.documentElement.scrollHeight - (window.innerHeight + window.scrollY)),
    pixelsAbove: window.scrollY,
  };
}
```

- [ ] **步骤 2：提交**

```bash
git add extensions/niu-browser-ext/content.js
git commit -m "feat: 创建 content_script 实现 DOM 提取和交互操作"
```

---

## 任务 3：创建 background.js 和 hub 通讯层

**文件：**
- 创建：`extensions/niu-browser-ext/background.js`
- 创建：`extensions/niu-browser-ext/hub.html`
- 创建：`extensions/niu-browser-ext/hub.js`

**说明：** background.js 是 Service Worker，负责消息路由。hub.html 是 Extension 页面，维持 WebSocket 连接（Service Worker 会被 Chrome 杀掉，不适合维持长连接）。

- [ ] **步骤 1：创建 background.js**

```javascript
/**
 * Niu Browser Extension - Background Service Worker
 * 消息路由：hub ↔ content_script
 */

// 监听来自 hub 的消息，转发到 content_script
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.target === 'content') {
    // 获取当前活动标签页
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      if (tabs[0]) {
        chrome.tabs.sendMessage(tabs[0].id, msg, (response) => {
          sendResponse(response);
        });
      } else {
        sendResponse({ success: false, message: 'No active tab' });
      }
    });
    return true; // 异步响应
  }
});

// 监听标签页更新（页面加载完成时自动获取状态）
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === 'complete' && tab.url && !tab.url.startsWith('chrome://')) {
    // 通知 hub 页面加载完成
    chrome.runtime.sendMessage({
      type: 'tab_updated',
      tabId: tabId,
      url: tab.url,
      title: tab.title,
    }).catch(() => {}); // 忽略没有监听者的错误
  }
});

// 监听新标签页创建
chrome.tabs.onCreated.addListener((tab) => {
  chrome.runtime.sendMessage({
    type: 'tab_created',
    tabId: tab.id,
    url: tab.url,
  }).catch(() => {});
});
```

- [ ] **步骤 2：创建 hub.html**

```html
<!DOCTYPE html>
<html>
<head>
  <title>Niu Browser Hub</title>
</head>
<body>
  <div id="status">Connecting...</div>
  <script src="hub.js"></script>
</body>
</html>
```

- [ ] **步骤 3：创建 hub.js**

```javascript
/**
 * Niu Browser Extension - Hub Page
 * 维持与 Python 后端的 WebSocket 连接。
 * 使用 Extension 页面而非 Service Worker，因为 Service Worker 会被 Chrome 杀掉。
 */

const WS_PORT = 19876; // 与 Python 端约定
const WS_URL = `ws://localhost:${WS_PORT}`;
const statusEl = document.getElementById('status');

let ws = null;
let reconnectTimer = null;

function connect() {
  statusEl.textContent = 'Connecting...';

  ws = new WebSocket(WS_URL);

  ws.onopen = () => {
    statusEl.textContent = 'Connected';
    console.log('[NiuHub] WebSocket connected');
    // 通知 Python 端已就绪
    ws.send(JSON.stringify({ type: 'ready' }));
    // 取消重连定时器
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
  };

  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    handleCommand(msg);
  };

  ws.onclose = () => {
    statusEl.textContent = 'Disconnected - reconnecting...';
    console.log('[NiuHub] WebSocket closed, reconnecting in 2s');
    reconnectTimer = setTimeout(connect, 2000);
  };

  ws.onerror = (err) => {
    console.error('[NiuHub] WebSocket error:', err);
  };
}

/**
 * 处理来自 Python 的命令
 */
async function handleCommand(msg) {
  const { type, id, tabId } = msg;

  try {
    // 确定目标标签页
    let targetTabId = tabId;
    if (!targetTabId) {
      // 默认：当前活动标签页
      const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
      if (tabs[0]) targetTabId = tabs[0].id;
    }

    if (!targetTabId) {
      sendResult(id, false, 'No active tab');
      return;
    }

    // 转发命令到 content_script
    const response = await chrome.tabs.sendMessage(targetTabId, msg);
    // 将结果返回给 Python
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        type: 'result',
        id: id,
        success: response?.success ?? false,
        data: response?.data,
        message: response?.message,
      }));
    }
  } catch (e) {
    sendResult(id, false, e.message);
  }
}

function sendResult(id, success, message, data = null) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'result', id, success, message, data }));
  }
}

// 监听来自 content_script 的主动消息（如 tab_updated）
chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type === 'tab_updated' || msg.type === 'tab_created') {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(msg));
    }
  }
});

// 启动连接
connect();
```

- [ ] **步骤 4：创建 manifest.json**

```json
{
  "manifest_version": 3,
  "name": "Niu Browser Assistant",
  "description": "AI 驱动的浏览器自动化助手 - 结构化网页状态提取与交互操作",
  "version": "1.0.0",
  "permissions": ["tabs", "activeTab", "sidePanel"],
  "host_permissions": ["<all_urls>"],
  "action": {
    "default_title": "Niu Browser Assistant"
  },
  "side_panel": {
    "default_path": "hub.html"
  },
  "background": {
    "service_worker": "background.js"
  },
  "content_scripts": [
    {
      "matches": ["<all_urls>"],
      "run_at": "document_end",
      "js": ["dom_tree.js", "content.js"]
    }
  ],
  "externally_connectable": {
    "matches": ["http://localhost/*"]
  }
}
```

- [ ] **步骤 5：提交**

```bash
git add extensions/niu-browser-ext/
git commit -m "feat: 创建 Chrome Extension 通讯层（background + hub + manifest）"
```

---

## 任务 4：创建 Python WebSocket Bridge

**文件：**
- 创建：`mcp-servers/browser-server/src/niu_browser_server/ws_bridge.py`

**说明：** Python 端的 WebSocket 服务端，与 Extension 的 hub 页面通讯。接收 MCP 工具的调用请求，通过 WS 转发给 Extension，等待结果返回。

- [ ] **步骤 1：创建 ws_bridge.py**

```python
"""
WebSocket Bridge: Python 后端 ↔ Chrome Extension

在独立线程中运行 WebSocket 服务端，接收 MCP 工具调用请求，
通过 WS 转发给 Extension，等待结果返回。
"""

import asyncio
import json
import queue
import threading
import time
import uuid
from typing import Any, Optional

try:
    import websockets
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False

from loguru import logger

WS_PORT = 19876  # 与 Extension hub.js 约定


class WSBridge:
    """
    WebSocket 服务端，与 Chrome Extension 通讯。

    架构：
    - 独立线程运行 asyncio 事件循环
    - MCP 工具调用 → send_command() → WS → Extension
    - Extension → WS → on_message() → 返回结果给等待的调用者
    """

    _instance: Optional['WSBridge'] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws_server = None
        self._hub_ws = None  # Extension hub 的 WS 连接
        self._pending: dict[str, queue.Queue] = {}  # command_id → result queue
        self._connected = False

        # 启动 WS 服务线程
        self._thread = threading.Thread(
            target=self._run_server,
            daemon=True,
            name="WSBridge-Server"
        )
        self._thread.start()

        self._initialized = True
        logger.info(f"WSBridge initialized (port: {WS_PORT})")

    def _run_server(self):
        """在独立线程中运行 WebSocket 服务端。"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        if not HAS_WEBSOCKETS:
            logger.warning("websockets package not installed, WS bridge unavailable")
            return

        async def start():
            self._ws_server = await websockets.serve(
                self._handle_connection,
                "localhost",
                WS_PORT,
            )
            logger.info(f"WSBridge server listening on ws://localhost:{WS_PORT}")

        self._loop.run_until_complete(start())
        self._loop.run_forever()

    async def _handle_connection(self, ws, path=None):
        """处理 Extension hub 的 WS 连接。"""
        logger.info("Extension hub connected")
        self._hub_ws = ws
        self._connected = True

        try:
            async for message in ws:
                msg = json.loads(message)
                await self._on_message(msg)
        except websockets.exceptions.ConnectionClosed:
            logger.info("Extension hub disconnected")
        finally:
            self._hub_ws = None
            self._connected = False

    async def _on_message(self, msg: dict):
        """处理来自 Extension 的消息。"""
        msg_type = msg.get("type")

        if msg_type == "ready":
            logger.info("Extension hub ready")
            return

        if msg_type in ("result", "error"):
            # 命令结果返回
            cmd_id = msg.get("id")
            if cmd_id and cmd_id in self._pending:
                self._pending[cmd_id].put(msg)

        elif msg_type == "tab_updated":
            # 标签页更新通知
            logger.debug(f"Tab updated: {msg.get('url')}")

        elif msg_type == "tab_created":
            # 新标签页创建通知
            logger.debug(f"Tab created: {msg.get('url')}")

    def send_command(self, action: str, **kwargs) -> dict:
        """
        发送命令给 Extension，等待结果返回（同步）。

        Args:
            action: 命令类型（get_state, click, input_text, select_option, scroll, navigate）
            **kwargs: 命令参数

        Returns:
            Extension 返回的结果字典
        """
        if not self._connected or not self._hub_ws:
            return {"success": False, "message": "Extension not connected. Is the browser running with the extension?"}

        cmd_id = str(uuid.uuid4())
        result_queue = queue.Queue()
        self._pending[cmd_id] = result_queue

        # 构建命令
        command = {
            "type": action,
            "id": cmd_id,
            **kwargs,
        }

        # 发送到 Extension
        future = asyncio.run_coroutine_threadsafe(
            self._hub_ws.send(json.dumps(command)),
            self._loop,
        )
        try:
            future.result(timeout=5)
        except Exception as e:
            del self._pending[cmd_id]
            return {"success": False, "message": f"Failed to send command: {e}"}

        # 等待结果（超时 30 秒）
        try:
            result = result_queue.get(timeout=30)
        except queue.Empty:
            del self._pending[cmd_id]
            return {"success": False, "message": f"Command {action} timed out (30s)"}

        del self._pending[cmd_id]
        return result

    @property
    def connected(self) -> bool:
        """Extension 是否已连接。"""
        return self._connected
```

- [ ] **步骤 2：安装 websockets 依赖**

```bash
pip install websockets
```

- [ ] **步骤 3：提交**

```bash
git add mcp-servers/browser-server/src/niu_browser_server/ws_bridge.py
git commit -m "feat: 创建 WebSocket Bridge 与 Chrome Extension 通讯"
```

---

## 任务 5：创建浏览器启动器

**文件：**
- 创建：`mcp-servers/browser-server/src/niu_browser_server/launcher.py`

- [ ] **步骤 1：创建 launcher.py**

```python
"""
浏览器启动器：查找系统默认浏览器并启动，加载我们的 Chrome Extension。
"""

import subprocess
import sys
from pathlib import Path
from typing import Optional
from loguru import logger


# Extension 路径（相对于项目根目录）
EXTENSION_DIR = Path(__file__).parent.parent.parent.parent.parent / "extensions" / "niu-browser-ext"

# 用户数据目录（独立 profile，避免与用户日常浏览器冲突）
USER_DATA_DIR = Path.home() / ".niu" / "browser_ext_profile"


def _find_default_browser() -> Optional[str]:
    """查找 Windows 系统默认浏览器路径。"""
    if sys.platform != "win32":
        return None

    import winreg

    # 1. 从注册表获取默认浏览器的 ProgId
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice",
        ) as key:
            prog_id, _ = winreg.QueryValueEx(key, "ProgId")
    except (FileNotFoundError, OSError):
        prog_id = None

    # 2. ProgId → App Paths 注册表键
    progid_to_appkey = {
        "MSEdgeHTM": r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe",
        "ChromeHTML": r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
    }

    if prog_id in progid_to_appkey:
        app_key = progid_to_appkey[prog_id]
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, app_key) as key:
                    exe_path, _ = winreg.QueryValueEx(key, None)
                    if exe_path and Path(exe_path).is_file():
                        return exe_path
            except (FileNotFoundError, OSError):
                continue

    # 3. 从 ProgId 的 shell\open\command 解析
    if prog_id:
        try:
            with winreg.OpenKey(
                winreg.HKEY_CLASSES_ROOT, f"{prog_id}\\shell\\open\\command"
            ) as key:
                command, _ = winreg.QueryValueEx(key, None)
                if command.startswith('"'):
                    end = command.index('"', 1)
                    exe_path = command[1:end]
                    if Path(exe_path).is_file():
                        return exe_path
        except (FileNotFoundError, OSError, ValueError):
            pass

    return None


def launch_browser(url: Optional[str] = None) -> subprocess.Popen:
    """
    启动系统默认浏览器，加载 Niu Browser Extension。

    Args:
        url: 可选的初始 URL。如果 None，打开 about:blank。

    Returns:
        浏览器进程句柄
    """
    exe_path = _find_default_browser()
    if not exe_path:
        raise RuntimeError(
            "无法找到系统默认浏览器。请确保已安装 Chrome 或 Edge。"
        )

    extension_path = str(EXTENSION_DIR.resolve())
    if not Path(extension_path, "manifest.json").is_file():
        raise FileNotFoundError(
            f"Extension 未找到: {extension_path}\n"
            "请参考系统管理手册安装 Niu Browser Extension。"
        )

    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)

    args = [
        exe_path,
        f"--load-extension={extension_path}",
        f"--user-data-dir={USER_DATA_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        url or "about:blank",
    ]

    # Windows: 创建独立进程组，浏览器不随 Python 退出
    DETACHED = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200

    proc = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=DETACHED | CREATE_NEW_PROCESS_GROUP,
    )

    logger.info(f"Browser launched (PID: {proc.pid}, exe: {exe_path})")
    return proc
```

- [ ] **步骤 2：提交**

```bash
git add mcp-servers/browser-server/src/niu_browser_server/launcher.py
git commit -m "feat: 创建浏览器启动器（查找默认浏览器 + 加载 Extension）"
```

---

## 任务 6：重写 browser_navigate MCP 工具

**文件：**
- 修改：`mcp-servers/browser-server/src/niu_browser_server/__init__.py`

**说明：** 完全重写 browser-server。移除 Playwright/BrowserManager/SyncPageProxy/CDP，改为通过 WSBridge 与 Extension 通讯。

- [ ] **步骤 1：重写 __init__.py**

```python
"""
Niu Browser MCP Server

通过 Chrome Extension 控制浏览器，替代 Playwright 方案。

架构：
- browser_navigate MCP 工具 → 启动浏览器（如果未启动）→ 通过 WSBridge 发送命令给 Extension
- Extension content_script → 在页面内提取 DOM 状态、执行操作
- 结果通过 WebSocket 返回给 Python → 返回给 LLM

优势：
- LLM 直接看到编号的交互元素，无需写 code_run
- Extension 常驻页面，自动处理新标签页
- 模拟真实鼠标事件，比 Playwright 更不容易被检测
"""

from loguru import logger
from .ws_bridge import WSBridge
from .launcher import launch_browser

import subprocess
import time


# 全局状态
_browser_proc: subprocess.Popen | None = None
_ws_bridge: WSBridge | None = None


def _ensure_browser_and_connection() -> WSBridge:
    """确保浏览器已启动且 Extension 已连接。"""
    global _browser_proc, _ws_bridge

    if _ws_bridge is None:
        _ws_bridge = WSBridge()

    # 如果 Extension 未连接，尝试启动浏览器
    if not _ws_bridge.connected:
        if _browser_proc is None or _browser_proc.poll() is not None:
            logger.info("Starting browser with extension...")
            _browser_proc = launch_browser()
            # 等待 Extension 连接（最多 10 秒）
            for _ in range(20):
                if _ws_bridge.connected:
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError(
                    "Extension 未连接。请确保 Niu Browser Extension 已安装。\n"
                    "参考系统管理手册安装插件。"
                )

    return _ws_bridge


def browser_navigate(
    url: str,
    wait_until: str = "domcontentloaded"
) -> dict:
    """
    启动浏览器并导航到 URL。自动返回页面结构化状态。

    Args:
        url: 目标 URL
        wait_until: 等待策略（保留参数兼容性，实际由 Extension 处理）

    Returns:
        包含页面状态的字典：url, title, elements（编号的交互元素）, pageInfo
    """
    try:
        bridge = _ensure_browser_and_connection()

        # 发送导航命令
        nav_result = bridge.send_command("navigate", url=url)

        # 等待页面加载（Extension content_script 会重新注入）
        time.sleep(2)

        # 获取页面状态
        state_result = bridge.send_command("get_state")

        if state_result.get("success"):
            data = state_result.get("data", {})
            return {
                "status": "success",
                "url": data.get("url", url),
                "title": data.get("title", ""),
                "elements": data.get("elements", ""),
                "pageInfo": data.get("pageInfo", {}),
            }
        else:
            return {
                "status": "success",
                "message": f"Navigated to {url}, but failed to get page state: {state_result.get('message', '')}",
            }

    except Exception as e:
        logger.error(f"browser_navigate failed: {e}")
        return {"status": "error", "message": str(e)}


def browser_interact(
    action: str,
    index: int = 0,
    text: str = "",
    option: str = "",
    direction: str = "down",
    amount: float = 1.0,
) -> dict:
    """
    与页面交互：点击、输入、选择、滚动。

    Args:
        action: 操作类型 - click, input, select, scroll, get_state
        index: 元素编号（从 browser_navigate 返回的 elements 中获取）
        text: 输入文本（action=input 时使用）
        option: 选择选项（action=select 时使用）
        direction: 滚动方向（action=scroll 时使用）
        amount: 滚动量（页数，action=scroll 时使用）

    Returns:
        操作结果 + 更新后的页面状态
    """
    try:
        bridge = _ensure_browser_and_connection()

        action_map = {
            "click": lambda: bridge.send_command("click", index=index),
            "input": lambda: bridge.send_command("input_text", index=index, text=text),
            "select": lambda: bridge.send_command("select_option", index=index, option=option),
            "scroll": lambda: bridge.send_command("scroll", direction=direction, amount=amount),
            "get_state": lambda: bridge.send_command("get_state"),
        }

        if action not in action_map:
            return {"status": "error", "message": f"Unknown action: {action}. Supported: {list(action_map.keys())}"}

        result = action_map[action]()

        if result.get("success"):
            data = result.get("data", {})
            return {
                "status": "success",
                "message": result.get("message", "OK"),
                "url": data.get("url", ""),
                "title": data.get("title", ""),
                "elements": data.get("elements", ""),
                "pageInfo": data.get("pageInfo", {}),
            }
        else:
            return {"status": "error", "message": result.get("message", "Unknown error")}

    except Exception as e:
        logger.error(f"browser_interact failed: {e}")
        return {"status": "error", "message": str(e)}


# ============== Tool Schemas ==============

TOOL_SCHEMAS = {
    "browser_navigate": {
        "name": "browser_navigate",
        "description": "启动浏览器并导航到 URL，自动返回页面结构化状态（编号的交互元素列表）。LLM 根据返回的元素编号决策下一步操作。**使用场景**：用户要求'打开网页'、'访问网站'、'浏览页面'时使用。**返回**：url、title、elements（编号的交互元素，如 [0]<button>登录 />）、pageInfo。",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "目标 URL"},
                "wait_until": {
                    "type": "string",
                    "enum": ["load", "domcontentloaded", "networkidle", "commit"],
                    "default": "domcontentloaded"
                }
            },
            "required": ["url"]
        }
    },
    "browser_interact": {
        "name": "browser_interact",
        "description": "与页面交互：点击元素、输入文本、选择下拉选项、滚动页面、获取当前页面状态。**使用场景**：在 browser_navigate 之后，根据返回的元素编号执行操作。**参数**：action（click/input/select/scroll/get_state）、index（元素编号，从 elements 列表中获取）、text（输入文本）、option（选择选项）、direction（滚动方向）、amount（滚动量）。**返回**：操作结果 + 更新后的页面状态。",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["click", "input", "select", "scroll", "get_state"],
                    "description": "操作类型"
                },
                "index": {"type": "integer", "description": "元素编号（从 elements 列表中获取）"},
                "text": {"type": "string", "description": "输入文本（action=input 时使用）"},
                "option": {"type": "string", "description": "选择选项（action=select 时使用）"},
                "direction": {
                    "type": "string",
                    "enum": ["up", "down"],
                    "default": "down",
                    "description": "滚动方向（action=scroll 时使用）"
                },
                "amount": {
                    "type": "number",
                    "default": 1.0,
                    "description": "滚动量（页数，action=scroll 时使用）"
                }
            },
            "required": ["action"]
        }
    }
}


def get_tool_schemas() -> list[dict]:
    """Return tool schemas for ToolRegistry"""
    return list(TOOL_SCHEMAS.values())


def main():
    """Entry point for standalone testing"""
    print("Niu Browser Server - Chrome Extension Architecture")
    print(f"Available tools: {len(TOOL_SCHEMAS)}")
    for name in TOOL_SCHEMAS:
        print(f"  - {name}")
```

- [ ] **步骤 2：提交**

```bash
git add mcp-servers/browser-server/src/niu_browser_server/__init__.py
git commit -m "feat: 重写 browser-server 为 Chrome Extension 架构"
```

---

## 任务 7：往系统管理手册添加浏览器插件章节

**文件：**
- 修改：`docs/SYSTEM_MANUAL.md`

**说明：** 系统管理手册已存在，往其中添加"浏览器自动化插件"章节。主 Agent 读取后，能自动安装插件或指导用户安装。

- [ ] **步骤 1：在 SYSTEM_MANUAL.md 的故障排查章节（六）之前，添加新章节"5.7 浏览器自动化插件"**

在 `### 5.6 数据问题` 之后，`---` 分隔线之前，添加：

```markdown
### 5.7 浏览器自动化插件

#### 插件概述

Niu Browser Assistant 是一个 Chrome Extension，提供结构化网页状态提取和交互操作能力。
安装后，AI 助手可以：自动读取网页内容、点击按钮、填写表单、滚动页面。

插件随软件包分发，位于 `extensions/niu-browser-ext/` 目录。

#### 安装方法

**方法 1：自动安装（推荐）**

如果系统默认浏览器已关闭，AI 助手会自动启动浏览器并加载插件（通过 `--load-extension` 参数）。
无需手动操作。

**方法 2：手动安装（浏览器已打开时）**

1. 打开 Chrome/Edge 浏览器
2. 地址栏输入：`chrome://extensions/`（Chrome）或 `edge://extensions/`（Edge）
3. 开启"开发者模式"（右上角开关）
4. 点击"加载已解压的扩展程序"
5. 选择目录：`[安装目录]/extensions/niu-browser-ext`
6. 插件安装完成，浏览器右上角出现 Niu 图标

**方法 3：权限不足时**

如果无法写入浏览器扩展目录，请用户执行以下操作：

1. 以管理员身份打开命令提示符
2. 运行：`start chrome --load-extension="[安装目录]\extensions\niu-browser-ext" --user-data-dir="%USERPROFILE%\.niu\browser_ext_profile"`
3. 或指导用户按方法 2 手动安装

#### 验证安装

安装成功后，打开任意网页，按 F12 打开开发者工具，在 Console 中输入：
```javascript
typeof NiuDomTree !== 'undefined'
```
返回 `true` 表示插件工作正常。

#### 故障排查

| 问题 | 原因 | 解决 |
|------|------|------|
| browser_navigate 返回 "Extension not connected" | 插件未安装或浏览器未启动 | 按上述方法安装插件 |
| 页面无交互元素 | 页面是纯图片/Canvas | 使用截图方式判断 |
| 新标签页无法操作 | content_script 未注入 | 刷新页面或等待自动注入 |
| WebSocket 连接失败 | Python 服务未启动 | 重启 AI 助手服务 |
```

- [ ] **步骤 2：提交**

```bash
git add docs/SYSTEM_MANUAL.md
git commit -m "docs: 系统管理手册添加浏览器自动化插件章节"
```

---

## 任务 8：更新 Skill 文档和 CLAUDE.md

**文件：**
- 修改：`memory/skills/browser-automation.md`
- 修改：`CLAUDE.md`

**说明：** 更新 Skill 文档，反映新的 Chrome Extension 架构。LLM 不再需要写 code_run，直接用 `browser_navigate` + `browser_interact` 即可。

- [ ] **步骤 1：重写 browser-automation.md**

核心变化：
- 移除 code_run + CDP + Playwright 的所有内容
- 新的工作循环：`browser_navigate` → 看到编号元素 → `browser_interact` → 看到新状态 → 决策下一步
- 示例改为 `browser_interact(action="click", index=3)` 这种形式

- [ ] **步骤 2：更新 CLAUDE.md 的 Browser-Server 架构部分**

- 移除 Playwright/BrowserManager/SyncPageProxy/CDP 相关描述
- 添加 Chrome Extension 架构描述
- 更新 MCP 工具列表：browser_navigate + browser_interact

- [ ] **步骤 3：提交**

```bash
git add memory/skills/browser-automation.md CLAUDE.md
git commit -m "docs: 更新 Skill 和 CLAUDE.md 为 Chrome Extension 架构"
```

---

## 任务 9：更新 ToolRegistry 和 runner.py

**文件：**
- 修改：`agent/runner.py`（BASE_MCP_TOOLS 更新）
- 修改：`config/agents/niu.md`（mcpServers 确认 browser-server 在列表中）

**说明：** BASE_MCP_TOOLS 从 `["browser-server/browser_navigate"]` 改为 `["browser-server/browser_navigate", "browser-server/browser_interact"]`。

- [ ] **步骤 1：更新 BASE_MCP_TOOLS**

在 `agent/runner.py` 中找到 `BASE_MCP_TOOLS`，更新为：

```python
BASE_MCP_TOOLS = [
    "browser-server/browser_navigate",
    "browser-server/browser_interact",
]
```

- [ ] **步骤 2：提交**

```bash
git add agent/runner.py
git commit -m "feat: 更新 BASE_MCP_TOOLS 添加 browser_interact"
```

---

## 任务 10：端到端测试

**文件：**
- 无新文件

- [ ] **步骤 1：构建 Extension**

确认 `extensions/niu-browser-ext/` 目录结构完整：
- manifest.json
- background.js
- content.js
- dom_tree.js
- hub.html
- hub.js

- [ ] **步骤 2：启动服务**

```bash
# 安装 websockets
pip install websockets

# 重启服务
go run main.go
```

- [ ] **步骤 3：测试"上网查新闻"场景**

在对话中输入"上网查一下今日热点新闻"，检查：

预期：
1. LLM 调用 `browser_navigate(url="https://news.baidu.com")`
2. 返回包含编号的交互元素：`[0]<a >新闻 />`, `[1]<a >国内 />` 等
3. LLM 根据元素编号调用 `browser_interact(action="click", index=N)`
4. 返回新的页面状态
5. LLM 提取新闻内容并汇报给用户

- [ ] **步骤 4：测试表单填写场景**

1. `browser_navigate(url="https://example.com/login")`
2. 看到 `[1]<input type=text name=username />`, `[2]<input type=password />`, `[3]<button>登录 />`
3. `browser_interact(action="input", index=1, text="张三")`
4. `browser_interact(action="input", index=2, text="password123")`
5. `browser_interact(action="click", index=3)`

---

## 验证清单

- [ ] Extension 能在 Chrome/Edge 中加载
- [ ] WebSocket 连接建立成功
- [ ] browser_navigate 返回结构化页面状态（编号的交互元素）
- [ ] browser_interact 能点击、输入、选择、滚动
- [ ] 新标签页自动被 Extension 覆盖
- [ ] 系统管理手册可被主 Agent 读取
- [ ] Skill 文档准确反映新架构
- [ ] 旧 Playwright 代码已移除（BrowserManager, SyncPageProxy, CDP）

---

## 解决方案覆盖矩阵

| 问题 | 严重程度 | 解决方案 | 任务 |
|------|---------|---------|------|
| LLM 看不到页面内容 | HIGH | browser_navigate 自动返回结构化状态 | 2+6 |
| code_run 经常失败 | HIGH | 不再需要 code_run，用 browser_interact | 6 |
| 新标签页无法处理 | HIGH | Extension content_script 自动注入 | 3 |
| 选择器猜测不准 | HIGH | 编号的交互元素，用 index 操作 | 1+2 |
| 无鼠标模拟 | MEDIUM | dispatchEvent PointerEvent | 2 |
| Playwright 依赖重 | MEDIUM | 完全移除 Playwright | 6 |
| 插件安装复杂 | LOW | 系统管理手册 + 主 Agent 自动处理 | 7 |
| 旧架构残留代码 | LOW | 移除 BrowserManager/SyncPageProxy | 6 |
