/**
 * Niu Browser Extension - Background Service Worker
 * Message routing: hub <-> content_script
 * Auto-opens hub tab on startup for WebSocket connection
 */

const HUB_URL = chrome.runtime.getURL('hub.html');
let hubTabId = null;

/**
 * Ensure hub tab is open. Called on service worker startup and extension icon click.
 * This is the most reliable way to keep hub alive across service worker restarts.
 */
function ensureHubTab() {
  chrome.tabs.query({ url: HUB_URL }, (tabs) => {
    if (tabs.length > 0) {
      hubTabId = tabs[0].id;
      console.log('[NiuBG] Hub tab already exists:', hubTabId);
    } else {
      chrome.tabs.create({ url: HUB_URL, active: false }, (tab) => {
        hubTabId = tab.id;
        console.log('[NiuBG] Hub tab opened:', hubTabId);
      });
    }
  });
}

// Open hub on service worker startup (fires when SW wakes up from idle)
ensureHubTab();

// Also open hub on extension install/update
chrome.runtime.onInstalled.addListener(() => {
  ensureHubTab();
});

// Open hub when extension icon is clicked (user can manually trigger)
chrome.action.onClicked.addListener(() => {
  ensureHubTab();
});

// Track hub tab lifecycle - reopen if closed
chrome.tabs.onRemoved.addListener((tabId) => {
  if (tabId === hubTabId) {
    hubTabId = null;
    console.log('[NiuBG] Hub tab closed, reopening in 1s...');
    setTimeout(() => {
      chrome.tabs.create({ url: HUB_URL, active: false }, (tab) => {
        hubTabId = tab.id;
        console.log('[NiuBG] Hub tab reopened:', hubTabId);
      });
    }, 1000);
  }
});

// Listen for messages from hub, forward to content_script
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

// Notify hub when tab finishes loading
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === 'complete' && tab.url && !tab.url.startsWith('chrome://') && !tab.url.startsWith('edge://')) {
    chrome.runtime.sendMessage({
      type: 'tab_updated',
      tabId: tabId,
      url: tab.url,
      title: tab.title,
    }).catch(() => {});
  }
});

// Notify hub when new tab is created
chrome.tabs.onCreated.addListener((tab) => {
  chrome.runtime.sendMessage({
    type: 'tab_created',
    tabId: tab.id,
    url: tab.url,
  }).catch(() => {});
});
