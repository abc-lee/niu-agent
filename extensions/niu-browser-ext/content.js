/**
 * Niu Browser Extension - Content Script
 * Injected into every web page. Handles DOM state extraction and interaction operations.
 */

// DOM state cache
let lastFlatTree = null;
let lastSelectorMap = null;
let lastSimplifiedHTML = '';

// Listen for messages from background
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  handleMessage(msg).then(sendResponse);
  return true; // Keep channel open for async response
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
    default:
      return { success: false, message: 'Unknown command: ' + msg.type };
  }
}

/**
 * Get structured browser state
 * Returns url, title, elements (indexed interactive element list), pageInfo
 */
function getBrowserState() {
  try {
    // Clean up old highlights
    NiuDomTree.cleanUpHighlights();

    // Build new DOM tree
    lastFlatTree = NiuDomTree.buildFlatTree({ doHighlightElements: true });
    lastSelectorMap = NiuDomTree.getSelectorMap(lastFlatTree);
    lastSimplifiedHTML = NiuDomTree.flatTreeToString(lastFlatTree);

    // Get page info
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
    return { success: false, message: 'Failed to get state: ' + e.message };
  }
}

/**
 * Click element by index (simulate real mouse events)
 */
function clickElement(index) {
  try {
    const element = lastSelectorMap?.get(index);
    if (!element) {
      return { success: false, message: 'Element ' + index + ' not found. Call get_state first.' };
    }

    // Scroll into view
    element.scrollIntoView({ behavior: 'smooth', block: 'center' });

    // Simulate mouse event sequence (more realistic than .click())
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

    // Wait for page response, then return new state
    return new Promise(resolve => {
      setTimeout(() => resolve(getBrowserState()), 500);
    });
  } catch (e) {
    return { success: false, message: 'Click failed: ' + e.message };
  }
}

/**
 * Input text (React/Vue compatible)
 */
function inputText(index, text) {
  try {
    const element = lastSelectorMap?.get(index);
    if (!element) {
      return { success: false, message: 'Element ' + index + ' not found.' };
    }

    element.scrollIntoView({ behavior: 'smooth', block: 'center' });
    element.focus();

    // Use native value setter (React compatible)
    const nativeSetter = Object.getOwnPropertyDescriptor(
      Object.getPrototypeOf(element), 'value'
    )?.set;
    if (nativeSetter) {
      nativeSetter.call(element, text);
    } else {
      element.value = text;
    }

    // Trigger input and change events
    element.dispatchEvent(new Event('input', { bubbles: true }));
    element.dispatchEvent(new Event('change', { bubbles: true }));

    return { success: true, message: 'Input "' + text + '" into element ' + index };
  } catch (e) {
    return { success: false, message: 'Input failed: ' + e.message };
  }
}

/**
 * Select dropdown option
 */
function selectOption(index, optionText) {
  try {
    const element = lastSelectorMap?.get(index);
    if (!element || element.tagName !== 'SELECT') {
      return { success: false, message: 'Element ' + index + ' is not a select.' };
    }

    element.value = optionText;
    element.dispatchEvent(new Event('change', { bubbles: true }));

    return { success: true, message: 'Selected "' + optionText + '" in element ' + index };
  } catch (e) {
    return { success: false, message: 'Select failed: ' + e.message };
  }
}

/**
 * Scroll page
 */
function scroll(direction, amount) {
  amount = amount || 1;
  const pixels = amount * window.innerHeight;
  if (direction === 'down') window.scrollBy(0, pixels);
  else if (direction === 'up') window.scrollBy(0, -pixels);

  return new Promise(resolve => {
    setTimeout(() => resolve(getBrowserState()), 300);
  });
}

/**
 * Navigate to URL
 */
function navigate(url) {
  window.location.href = url;
  // After navigation, page reloads and content_script re-injects
  // Return value will be provided by new page's get_state
  return { success: true, message: 'Navigating to ' + url };
}

/**
 * Get page geometry info
 */
function getPageInfo() {
  return {
    viewportWidth: window.innerWidth,
    viewportHeight: window.innerHeight,
    pageWidth: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth || 0),
    pageHeight: Math.max(document.documentElement.scrollHeight, document.body.scrollHeight || 0),
    scrollX: window.scrollX,
    scrollY: window.scrollY,
    pixelsBelow: Math.max(0, document.documentElement.scrollHeight - (window.innerHeight + window.scrollY)),
    pixelsAbove: window.scrollY,
  };
}
