/**
 * Niu Browser Extension - Content Script
 * Injected into every web page. Handles DOM state extraction and interaction operations.
 * Uses SimulatorMask for visual mouse cursor animation during clicks.
 */

// DOM state cache
let lastFlatTree = null;
let lastSelectorMap = null;
let lastSimplifiedHTML = '';

// SimulatorMask instance (lazy init)
let mask = null;

function getMask() {
  if (!mask && window.NiuSimulatorMask) {
    mask = new window.NiuSimulatorMask();
  }
  return mask;
}

// Helper: get DOM element from selectorMap entry.
// selectorMap stores node data objects (with .ref pointing to the real DOM element).
function getDomElement(index) {
  const entry = lastSelectorMap?.get(index);
  if (!entry) return null;
  if (entry.ref && entry.ref.nodeType === Node.ELEMENT_NODE) return entry.ref;
  if (entry.nodeType === Node.ELEMENT_NODE) return entry;
  return null;
}

// Listen for messages from background/hub
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  handleMessage(msg).then(sendResponse);
  return true;
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
    default:
      return { success: false, message: 'Unknown command: ' + msg.type };
  }
}

/**
 * Get structured browser state.
 * Highlights elements briefly to build selectorMap, then cleans up.
 */
function getBrowserState() {
  try {
    // Hide mask before extracting DOM (mask elements have data-browser-use-ignore)
    const m = getMask();
    if (m) m.hide();

    NiuDomTree.cleanUpHighlights();

    lastFlatTree = NiuDomTree.buildFlatTree({ doHighlightElements: true });
    lastSelectorMap = NiuDomTree.getSelectorMap(lastFlatTree);
    lastSimplifiedHTML = NiuDomTree.flatTreeToString(lastFlatTree);

    // Clean up highlights immediately - numbers only flash briefly
    // selectorMap keeps .ref pointing to real DOM elements, so cleanup is safe
    NiuDomTree.cleanUpHighlights();

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
 * Wait for specified seconds.
 */
function waitFor(seconds) {
  return new Promise(resolve => setTimeout(resolve, seconds * 1000));
}

/**
 * Click element by index with full visual mouse simulation.
 * Follows page-agent's action sequence:
 * 1. scrollIntoView
 * 2. show mask + move cursor to element
 * 3. click animation
 * 4. enable pass-through
 * 5. dispatch real DOM events (W3C pointer event order)
 * 6. disable pass-through
 * 7. hide mask
 */
async function clickElement(index) {
  try {
    const element = getDomElement(index);
    if (!element) {
      return { success: false, message: 'Element ' + index + ' not found. Call get_state first.' };
    }

    // Scroll into view
    if (typeof element.scrollIntoViewIfNeeded === 'function') {
      element.scrollIntoViewIfNeeded();
    } else {
      element.scrollIntoView({ behavior: 'auto', block: 'center', inline: 'nearest' });
    }

    const rect = element.getBoundingClientRect();
    const x = rect.left + rect.width / 2;
    const y = rect.top + rect.height / 2;

    // Show mask and move cursor to element
    const m = getMask();
    if (m) {
      m.show();
      m.setCursorPosition(x, y);
      await waitFor(0.3); // Wait for cursor easing animation
      m.triggerClickAnimation();
      m.enablePassThrough();
    }

    // Hit-test to find deepest element at click coordinates (matches real browser behavior)
    const doc = element.ownerDocument;
    const hitTarget = doc.elementFromPoint(x, y);
    const target = (hitTarget instanceof HTMLElement && element.contains(hitTarget)) ? hitTarget : element;

    // Dispatch W3C pointer event sequence:
    // pointerover/enter → mouseover/enter → pointerdown → mousedown →
    // [focus] → pointerup → mouseup → click
    const pointerOpts = {
      bubbles: true, cancelable: true,
      clientX: x, clientY: y,
      pointerType: 'mouse', pointerId: 1, isPrimary: true,
    };
    const mouseOpts = {
      bubbles: true, cancelable: true,
      clientX: x, clientY: y, button: 0,
    };

    // Hover
    target.dispatchEvent(new PointerEvent('pointerover', pointerOpts));
    target.dispatchEvent(new PointerEvent('pointerenter', { ...pointerOpts, bubbles: false }));
    target.dispatchEvent(new MouseEvent('mouseover', mouseOpts));
    target.dispatchEvent(new MouseEvent('mouseenter', { ...mouseOpts, bubbles: false }));

    // Press
    target.dispatchEvent(new PointerEvent('pointerdown', pointerOpts));
    target.dispatchEvent(new MouseEvent('mousedown', mouseOpts));

    // Focus
    element.focus({ preventScroll: true });

    // Release
    target.dispatchEvent(new PointerEvent('pointerup', pointerOpts));
    target.dispatchEvent(new MouseEvent('mouseup', mouseOpts));

    // Return success before triggering activation click.
    // Use setTimeout(0) to put click in macrotask queue - this guarantees
    // sendResponse (in microtask) runs first, so the channel won't close
    // if click causes navigation.
    const maskRef = m;
    setTimeout(() => {
      target.click();
      // Clean up mask after a brief delay
      setTimeout(() => {
        if (maskRef) {
          maskRef.disablePassThrough();
          maskRef.hide();
        }
      }, 100);
    }, 0);

    return { success: true, message: 'Clicked element ' + index };
  } catch (e) {
    const m = getMask();
    if (m) m.hide();
    return { success: false, message: 'Click failed: ' + e.message };
  }
}

/**
 * Input text (React/Vue compatible)
 * Focuses the element, then sets the value.
 */
async function inputText(index, text) {
  try {
    const element = getDomElement(index);
    if (!element) {
      return { success: false, message: 'Element ' + index + ' not found.' };
    }

    // Scroll into view and focus
    if (typeof element.scrollIntoViewIfNeeded === 'function') {
      element.scrollIntoViewIfNeeded();
    } else {
      element.scrollIntoView({ behavior: 'auto', block: 'center', inline: 'nearest' });
    }
    element.focus({ preventScroll: true });

    // Use native value setter (React compatible)
    const nativeSetter = Object.getOwnPropertyDescriptor(
      Object.getPrototypeOf(element), 'value'
    )?.set;
    if (nativeSetter) {
      nativeSetter.call(element, '');
      nativeSetter.call(element, text);
    } else {
      element.value = text;
    }

    // Trigger input and change events
    element.dispatchEvent(new Event('input', { bubbles: true }));
    element.dispatchEvent(new Event('change', { bubbles: true }));

    // Return fresh state so caller has updated element indices
    return getBrowserState();
  } catch (e) {
    const m = getMask();
    if (m) m.hide();
    return { success: false, message: 'Input failed: ' + e.message };
  }
}

/**
 * Select dropdown option
 */
function selectOption(index, optionText) {
  try {
    const element = getDomElement(index);
    if (!element || element.tagName !== 'SELECT') {
      return { success: false, message: 'Element ' + index + ' is not a select.' };
    }

    element.value = optionText;
    element.dispatchEvent(new Event('change', { bubbles: true }));

    return getBrowserState();
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
