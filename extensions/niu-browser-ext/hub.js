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

function isInternalUrl(url) {
  return !url || url.startsWith('chrome://') || url.startsWith('edge://') || url.startsWith('chrome-extension://') || url === 'about:blank';
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
      await waitForTabLoad(newTab.id, 15000);
      const stateResult = await sendToContentScriptWithRetry(newTab.id, { type: 'get_state', id: id });
      sendResult(id, stateResult?.success ?? true, 'Tab created: ' + url, stateResult?.data);
    } else {
      // get_state, input_text, select_option, scroll - forward to content_script
      const targetTabId = tabId || await getActiveTabId();
      if (!targetTabId) {
        sendResult(id, false, 'No active tab');
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
      await waitForTabLoad(newTab.id, 15000);
      const stateResult = await sendToContentScriptWithRetry(newTab.id, { type: 'get_state', id: id });
      sendResult(id, stateResult?.success ?? true, stateResult?.message || 'Navigated to ' + url, stateResult?.data);
      return;
    }
  }

  // Navigate existing tab
  await chrome.tabs.update(targetTabId, { url: url });
  await waitForTabLoad(targetTabId, 15000);

  // Get page state from the new page's content_script (with retry for injection delay)
  const stateResult = await sendToContentScriptWithRetry(targetTabId, { type: 'get_state', id: id });
  sendResult(id, stateResult?.success ?? true, stateResult?.message || 'Navigated to ' + url, stateResult?.data);
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

  // Record URL before click
  let urlBefore;
  try {
    const tab = await chrome.tabs.get(targetTabId);
    urlBefore = tab.url;
  } catch (e) {
    urlBefore = '';
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
    const stateResult = await sendToContentScriptWithRetry(targetTabId, { type: 'get_state', id: id });
    sendResult(id, stateResult?.success ?? true, stateResult?.message || 'Click caused navigation', stateResult?.data);
  } else {
    // No navigation - get state from current page
    const stateResult = await sendToContentScript(targetTabId, { type: 'get_state', id: id });
    sendResult(id, stateResult?.success ?? true, stateResult?.message || 'Clicked', stateResult?.data);
  }
}

// ============== Utility functions ==============

function getActiveTabId() {
  return chrome.tabs.query({ active: true, currentWindow: true }).then(async (tabs) => {
    const tab = tabs[0];
    if (!tab) return null;

    // Browser internal pages (edge://, chrome://, about:) don't support content scripts.
    // Navigate to a real page so content_script can be injected.
    if (tab.url && isInternalUrl(tab.url)) {
      console.log('[NiuHub] Active tab is internal page, navigating to bing.com');
      await chrome.tabs.update(tab.id, { url: 'https://www.bing.com' });
      await waitForTabLoad(tab.id, 10000);
    }

    return tab.id;
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
    ws.send(JSON.stringify({ type: 'result', id: id, success: success, message: message, data: data || null }));
  }
}

// Listen for proactive messages from content_script/background (tab_updated, tab_created)
chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type === 'tab_updated' || msg.type === 'tab_created') {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(msg));
    }
  }
});

// Start connection
connect();
