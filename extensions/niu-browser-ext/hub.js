/**
 * Niu Browser Extension - Hub Page
 * Maintains WebSocket connection to Python backend.
 * Uses extension page (not service worker) because service worker gets killed by Chrome after 5min.
 */

const WS_PORT = 19876;
const WS_URL = 'ws://localhost:' + WS_PORT;
const statusEl = document.getElementById('status');

let ws = null;
let reconnectTimer = null;

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

  await chrome.tabs.update(tabId, { active: true });
  currentTabId = tabId;
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
      if (tab.id && isContentScriptAllowed(tab.url) && (windowId === null || tab.windowId === windowId)) {
        addTab({ id: tab.id, url: tab.url, title: tab.title, status: tab.status, isInitial: false });
        // 自动切换到新创建的标签页
        currentTabId = tab.id;
        // 首次收到标签页事件时确定 windowId
        if (windowId === null) {
          windowId = tab.windowId;
        }
      }
    } else if (message.action === 'removed') {
      const { tabId } = message.payload;
      const existed = tabs.find(t => t.id === tabId);
      if (existed) {
        removeTab(tabId);
        if (currentTabId === tabId) {
          const newCurrent = tabs[tabs.length - 1] || null;
          currentTabId = newCurrent ? newCurrent.id : null;
        }
      }
      // 如果 tabId 不在 tabs[] 中，说明已被 close_tab 处理器移除，跳过
    } else if (message.action === 'updated') {
      const { tabId, changeInfo } = message.payload;
      const updates = {};
      if (changeInfo.url) updates.url = changeInfo.url;
      if (changeInfo.title) updates.title = changeInfo.title;
      if (changeInfo.status) updates.status = changeInfo.status;
      updateTab(tabId, updates);
    } else if (message.action === 'activated') {
      // 用户手动切换标签页，同步 currentTabId
      const { tabId, windowId: evtWindowId } = message.payload;
      if (evtWindowId === windowId && tabs.find(t => t.id === tabId)) {
        currentTabId = tabId;
      }
    }
  });

  tabEventPort.onDisconnect.addListener(() => {
    tabEventPort = null;
    // 短暂延迟后重连（SW 可能已重启）
    setTimeout(connectTabEvents, 1000);
  });
}

async function initTabTracking() {
  try {
    const activeTabs = await chrome.tabs.query({ active: true, currentWindow: true });
    if (activeTabs[0]) {
      windowId = activeTabs[0].windowId;
      // 只在活动标签页支持 content script 时设置 currentTabId
      // 否则 currentTabId 将在遍历 tabs[] 后设置
      if (isContentScriptAllowed(activeTabs[0].url)) {
        currentTabId = activeTabs[0].id;
      }
    }

    // 查询窗口中所有标签页（不仅仅是活跃的）
    const allTabs = await chrome.tabs.query({ currentWindow: true });
    for (const tab of allTabs) {
      if (isContentScriptAllowed(tab.url)) {
        addTab({
          id: tab.id,
          url: tab.url,
          title: tab.title,
          status: tab.status,
          isInitial: tab.id === currentTabId,
        });
      }
    }

    // 如果 currentTabId 未设置（活动标签页是内部页面），选择 tabs[] 中第一个
    if (currentTabId === null && tabs.length > 0) {
      currentTabId = tabs[0].id;
    }

    // 确保至少有一个 isInitial 标签页（防止所有标签页都被关闭）
    if (tabs.length > 0 && !tabs.find(t => t.isInitial)) {
      tabs[0].isInitial = true;
    }

    // 连接标签页生命周期事件
    connectTabEvents();
  } catch (e) {
    console.error('[NiuHub] Tab tracking init failed:', e);
  }
}

function scheduleReconnect(delay) {
  if (reconnectTimer) return; // Already scheduled
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, delay);
}

function connect() {
  statusEl.textContent = 'Connecting...';
  statusEl.className = '';

  ws = new WebSocket(WS_URL);

  ws.onopen = () => {
    statusEl.textContent = 'Connected to Niu Assistant';
    statusEl.className = 'connected';
    console.log('[NiuHub] WebSocket connected');
    ws.send(JSON.stringify({ type: 'ready' }));
    // 首次连接时初始化标签页跟踪
    if (tabs.length === 0) {
      initTabTracking();
    }
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
  };

  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    console.log('[NiuHub] Received command:', msg.type, msg.id);
    handleCommand(msg);
  };

  ws.onclose = () => {
    statusEl.textContent = 'Disconnected - reconnecting...';
    statusEl.className = 'error';
    console.log('[NiuHub] WebSocket closed, reconnecting in 3s');
    scheduleReconnect(3000);
  };

  ws.onerror = (err) => {
    console.error('[NiuHub] WebSocket error:', err);
    // onerror is usually followed by onclose, but ensure we reconnect
    if (!reconnectTimer) {
      scheduleReconnect(3000);
    }
  };
}

async function handleCommand(msg) {
  const { type, id, tabId } = msg;

  try {
    if (type === 'navigate') {
      await handleNavigate(msg, id);
    } else if (type === 'click') {
      await handleClick(msg, id);
    } else if (type === 'create_tab') {
      // 创建新标签页（url 必填，不能打开 about:blank）
      const url = msg.url;
      if (!url || url === 'about:blank') {
        sendResult(id, false, 'url is required for new tab');
        return;
      }
      const newTab = await chrome.tabs.create({ url: url, active: msg.active !== false });
      // 跟踪新标签页
      addTab({ id: newTab.id, url: url, title: '', status: 'loading', isInitial: false });
      currentTabId = newTab.id;
      await waitForTabLoad(newTab.id, 15000);
      // 仅对支持 content script 的页面获取状态
      if (isContentScriptAllowed(url)) {
        const stateResult = await sendToContentScriptWithRetry(newTab.id, { type: 'get_state', id: id });
        sendResult(id, stateResult?.success ?? true, 'Tab created: ' + url, stateResult?.data);
      } else {
        sendResult(id, true, 'Tab created: ' + url, { url: url, title: '' });
      }
    } else if (type === 'switch_tab') {
      const tabId = msg.tabId;
      if (!tabId) {
        sendResult(id, false, 'tabId is required');
        return;
      }
      try {
        await switchToTab(tabId);
        // 获取切换后标签页的状态（仅对支持 content script 的页面）
        const target = tabs.find(t => t.id === tabId);
        if (target && isContentScriptAllowed(target.url)) {
          const stateResult = await sendToContentScriptWithRetry(tabId, { type: 'get_state', id: id });
          sendResult(id, true, 'Switched to tab ' + tabId, stateResult?.data);
        } else {
          sendResult(id, true, 'Switched to tab ' + tabId, { url: target?.url || '', title: target?.title || '' });
        }
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
      // 标记待关闭，防止 onRemoved 事件重复处理
      const wasCurrent = currentTabId === tabId;
      removeTab(tabId);
      try {
        await chrome.tabs.remove(tabId);
      } catch (e) {
        // chrome.tabs.remove 可能失败（标签页已关闭），忽略
      }
      // 如果关闭的是当前标签页，切换到最后一个剩余标签页
      if (wasCurrent) {
        const newCurrent = tabs[tabs.length - 1] || null;
        if (newCurrent) {
          currentTabId = newCurrent.id;
          try { await chrome.tabs.update(newCurrent.id, { active: true }); } catch (e) {}
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
    } else {
      // get_state, input_text, select_option, scroll - forward to content_script
      const targetTabId = tabId || await getActiveTabId();
      if (!targetTabId) {
        sendResult(id, false, 'No active tab');
        return;
      }
      // 检查目标标签页是否支持 content script
      try {
        const targetTab = await chrome.tabs.get(targetTabId);
        if (!isContentScriptAllowed(targetTab.url)) {
          sendResult(id, false, 'Target tab does not support content scripts: ' + (targetTab.url || 'unknown') + '\n\n可切换的标签页：\n' + summarizeTabs());
          return;
        }
      } catch (e) {
        sendResult(id, false, 'Tab not found: ' + targetTabId);
        return;
      }
      const response = await sendToContentScript(targetTabId, msg);
      sendResult(id, response?.success ?? false, response?.message, response?.data);
    }
  } catch (e) {
    sendResult(id, false, e.message);
  }
}

/**
 * Navigate: use chrome.tabs.update, wait for load, then get state from new page.
 */
async function handleNavigate(msg, id) {
  const url = msg.url;
  let targetTabId = msg.tabId;

  if (!targetTabId) {
    const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
    if (tabs[0] && !isInternalUrl(tabs[0].url)) {
      targetTabId = tabs[0].id;
    } else {
      // Create a new tab
      const newTab = await chrome.tabs.create({ url: url });
      // 跟踪新标签页
      addTab({ id: newTab.id, url: url, title: '', status: 'loading', isInitial: false });
      currentTabId = newTab.id;
      await waitForTabLoad(newTab.id, 15000);
      // 仅对支持 content script 的页面获取状态
      if (isContentScriptAllowed(url)) {
        const stateResult = await sendToContentScriptWithRetry(newTab.id, { type: 'get_state', id: id });
        sendResult(id, stateResult?.success ?? true, stateResult?.message || 'Navigated to ' + url, stateResult?.data);
      } else {
        sendResult(id, true, 'Navigated to ' + url, { url: url, title: '' });
      }
      return;
    }
  }

  // Navigate existing tab
  await chrome.tabs.update(targetTabId, { url: url });
  await waitForTabLoad(targetTabId, 15000);

  // Get page state from the new page's content_script (with retry for injection delay)
  // 仅对支持 content script 的页面获取状态
  if (isContentScriptAllowed(url)) {
    const stateResult = await sendToContentScriptWithRetry(targetTabId, { type: 'get_state', id: id });
    sendResult(id, stateResult?.success ?? true, stateResult?.message || 'Navigated to ' + url, stateResult?.data);
  } else {
    sendResult(id, true, 'Navigated to ' + url, { url: url, title: '' });
  }
}

/**
 * Click: send click to content_script, then detect if navigation happened.
 * If navigation: wait for new page load, get fresh state.
 * If no navigation: get state from current page.
 */
async function handleClick(msg, id) {
  const targetTabId = msg.tabId || await getActiveTabId();
  if (!targetTabId) {
    sendResult(id, false, 'No active tab');
    return;
  }

  // 检查目标标签页是否支持 content script（同时记录点击前 URL）
  let urlBefore;
  try {
    const targetTab = await chrome.tabs.get(targetTabId);
    if (!isContentScriptAllowed(targetTab.url)) {
      sendResult(id, false, 'Target tab does not support content scripts: ' + (targetTab.url || 'unknown') + '\n\n可切换的标签页：\n' + summarizeTabs());
      return;
    }
    urlBefore = targetTab.url;
  } catch (e) {
    sendResult(id, false, 'Tab not found: ' + targetTabId);
    return;
  }

  // Send click to content_script
  const clickResult = await sendToContentScript(targetTabId, { type: 'click', index: msg.index, id: id });

  if (!clickResult?.success) {
    sendResult(id, false, clickResult?.message || 'Click failed');
    return;
  }

  // Wait a moment for navigation to start
  await sleep(800);

  // Check if navigation happened
  let urlAfter;
  try {
    const tab = await chrome.tabs.get(targetTabId);
    urlAfter = tab.url;
  } catch (e) {
    // Tab may have been closed or replaced
    urlAfter = '';
  }

  const navigated = urlAfter !== urlBefore && !isInternalUrl(urlAfter);

  if (navigated) {
    // Page navigated - wait for new page to finish loading
    console.log('[NiuHub] Click caused navigation:', urlBefore, '->', urlAfter);
    await waitForTabLoad(targetTabId, 15000);

    // Get state from new page's content_script (with retry, as injection may be delayed)
    // 仅对支持 content script 的页面获取状态
    if (isContentScriptAllowed(urlAfter)) {
      const stateResult = await sendToContentScriptWithRetry(targetTabId, { type: 'get_state', id: id });
      sendResult(id, stateResult?.success ?? true, stateResult?.message || 'Click caused navigation', stateResult?.data);
    } else {
      sendResult(id, true, 'Click caused navigation', { url: urlAfter, title: '' });
    }
  } else {
    // No navigation - get state from current page
    // 仅对支持 content script 的页面获取状态
    if (isContentScriptAllowed(urlAfter)) {
      const stateResult = await sendToContentScript(targetTabId, { type: 'get_state', id: id });
      sendResult(id, stateResult?.success ?? true, stateResult?.message || 'Clicked', stateResult?.data);
    } else {
      sendResult(id, true, 'Clicked', { url: urlAfter, title: '' });
    }
  }
}

// ============== Utility functions ==============

function getActiveTabId() {
  return chrome.tabs.query({ active: true, currentWindow: true }).then((tabs) => {
    const tab = tabs[0];
    return tab ? tab.id : null;
  });
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function waitForTabLoad(tabId, timeout) {
  return new Promise((resolve) => {
    // Check if tab is already complete
    chrome.tabs.get(tabId, (tab) => {
      if (tab && tab.status === 'complete' && !isInternalUrl(tab.url)) {
        // Already loaded, but give content_script time to initialize
        setTimeout(resolve, 800);
        return;
      }

      const listener = (updatedTabId, changeInfo) => {
        if (updatedTabId === tabId && changeInfo.status === 'complete') {
          chrome.tabs.onUpdated.removeListener(listener);
          // Give content_script time to initialize after page load
          setTimeout(resolve, 800);
        }
      };
      chrome.tabs.onUpdated.addListener(listener);

      // Timeout fallback
      setTimeout(() => {
        chrome.tabs.onUpdated.removeListener(listener);
        resolve();
      }, timeout);
    });
  });
}

function sendToContentScript(tabId, msg, timeoutMs = 30000) {
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      resolve({ success: false, message: 'Content script timeout' });
    }, timeoutMs);

    chrome.tabs.sendMessage(tabId, msg, (response) => {
      clearTimeout(timer);
      if (chrome.runtime.lastError) {
        resolve({ success: false, message: chrome.runtime.lastError.message });
      } else {
        resolve(response);
      }
    });
  });
}

/**
 * Send message to content_script with retries.
 * After navigation, content_script may not be injected yet.
 */
async function sendToContentScriptWithRetry(tabId, msg, maxRetries = 5, delayMs = 1000) {
  for (let i = 0; i < maxRetries; i++) {
    const result = await sendToContentScript(tabId, msg);
    if (result?.success) return result;
    // Content script not ready yet, wait and retry
    if (i < maxRetries - 1) await sleep(delayMs);
  }
  return { success: false, message: 'Content script not responding after ' + maxRetries + ' retries' };
}

function sendResult(id, success, message, data) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    // 每次响应都包含标签页摘要（page-agent 模式）
    let enrichedData = data;
    if (success) {
      if (!data || typeof data !== 'object') {
        enrichedData = {};
      }
      enrichedData.tabSummary = summarizeTabs();
      enrichedData.currentTabId = currentTabId;
    }
    ws.send(JSON.stringify({ type: 'result', id: id, success: success, message: message, data: enrichedData }));
  }
}

// Start connection
connect();
