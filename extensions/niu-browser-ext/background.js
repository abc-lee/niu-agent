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
