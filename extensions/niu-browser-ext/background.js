/**
 * Niu Browser Extension - Background Service Worker
 * Message routing: hub <-> content_script
 */

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
  if (changeInfo.status === 'complete' && tab.url && !tab.url.startsWith('chrome://')) {
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
