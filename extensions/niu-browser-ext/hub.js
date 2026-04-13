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
    handleCommand(msg);
  };

  ws.onclose = () => {
    statusEl.textContent = 'Disconnected - reconnecting...';
    statusEl.className = 'error';
    console.log('[NiuHub] WebSocket closed, reconnecting in 2s');
    reconnectTimer = setTimeout(connect, 2000);
  };

  ws.onerror = (err) => {
    console.error('[NiuHub] WebSocket error:', err);
  };
}

async function handleCommand(msg) {
  const { type, id, tabId } = msg;

  try {
    let targetTabId = tabId;
    if (!targetTabId) {
      const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
      if (tabs[0]) targetTabId = tabs[0].id;
    }

    if (!targetTabId) {
      sendResult(id, false, 'No active tab');
      return;
    }

    // Forward command to content_script
    const response = await chrome.tabs.sendMessage(targetTabId, msg);
    // Return result to Python
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

function sendResult(id, success, message, data) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'result', id: id, success: success, message: message, data: data || null }));
  }
}

// Listen for proactive messages from content_script (tab_updated, tab_created)
chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type === 'tab_updated' || msg.type === 'tab_created') {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(msg));
    }
  }
});

// Start connection
connect();
