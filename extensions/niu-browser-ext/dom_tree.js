/**
 * @file NiuDomTree - DOM tree extraction engine for Niu Browser Extension
 * @origin Ported from page-agent page-controller/dom/dom_tree/index.js + dom/index.ts
 *
 * Exposes window.NiuDomTree with:
 *   - buildFlatTree(options)    : build flattened DOM tree with indexed interactive elements
 *   - flatTreeToString(flatTree): serialize FlatDomTree to LLM-readable text
 *   - getSelectorMap(flatTree)  : get interactive element map: index -> DOM element reference
 *   - cleanUpHighlights()       : clean up visual highlights
 */

;(function () {
  'use strict'

  // ---------------------------------------------------------------------------
  // newElementsCache: tracks which interactive elements are new (first-seen)
  // ---------------------------------------------------------------------------
  const newElementsCache = new WeakMap()

  // ---------------------------------------------------------------------------
  // Semantic landmark tags (preserved in dehydrated output even if not interactive)
  // ---------------------------------------------------------------------------
  const SEMANTIC_TAGS = new Set([
    'nav',
    'menu',
    'header',
    'footer',
    'aside',
    'dialog',
  ])

  // ---------------------------------------------------------------------------
  // Interactive ARIA attribute names
  // ---------------------------------------------------------------------------
  const INTERACTIVE_ARIA_ATTRS = [
    'aria-expanded',
    'aria-checked',
    'aria-selected',
    'aria-pressed',
    'aria-haspopup',
    'aria-controls',
    'aria-owns',
    'aria-activedescendant',
    'aria-valuenow',
    'aria-valuetext',
    'aria-valuemax',
    'aria-valuemin',
    'aria-autocomplete',
  ]

  function hasInteractiveAria(el) {
    for (let i = 0; i < INTERACTIVE_ARIA_ATTRS.length; i++) {
      if (el.hasAttribute(INTERACTIVE_ARIA_ATTRS[i])) return true
    }
    return false
  }

  // ---------------------------------------------------------------------------
  // Constants for distinct interaction check
  // ---------------------------------------------------------------------------
  const DISTINCT_INTERACTIVE_TAGS = new Set([
    'a',
    'button',
    'input',
    'select',
    'textarea',
    'summary',
    'details',
    'label',
    'option',
    'li',
  ])

  const DISTINCT_INTERACTIVE_ROLES = new Set([
    'button',
    'link',
    'menuitem',
    'menuitemradio',
    'menuitemcheckbox',
    'radio',
    'checkbox',
    'tab',
    'switch',
    'slider',
    'spinbutton',
    'combobox',
    'searchbox',
    'textbox',
    'listbox',
    'listitem',
    'treeitem',
    'row',
    'option',
    'scrollbar',
  ])

  // ---------------------------------------------------------------------------
  // Highlight container ID
  // ---------------------------------------------------------------------------
  const HIGHLIGHT_CONTAINER_ID = 'playwright-highlight-container'

  // ---------------------------------------------------------------------------
  // Glob-to-regex helper (used by flatTreeToString for attribute matching)
  // ---------------------------------------------------------------------------
  const globRegexCache = new Map()

  function globToRegex(pattern) {
    let regex = globRegexCache.get(pattern)
    if (!regex) {
      const escaped = pattern.replace(/[.+^${}()|[\]\\]/g, '\\$&')
      regex = new RegExp('^' + escaped.replace(/\*/g, '.*') + '$')
      globRegexCache.set(pattern, regex)
    }
    return regex
  }

  function matchAttributes(attrs, patterns) {
    const result = {}

    for (const pattern of patterns) {
      if (pattern.includes('*')) {
        const regex = globToRegex(pattern)
        for (const key of Object.keys(attrs)) {
          if (regex.test(key) && attrs[key].trim()) {
            result[key] = attrs[key].trim()
          }
        }
      } else {
        const value = attrs[pattern]
        if (value && value.trim()) {
          result[pattern] = value.trim()
        }
      }
    }

    return result
  }

  // ===========================================================================
  // buildFlatTree - core DOM tree builder
  // ===========================================================================

  function buildFlatTree(options) {
    const opts = options || {}

    const doHighlightElements = opts.doHighlightElements !== undefined ? opts.doHighlightElements : true
    const focusHighlightIndex = opts.focusHighlightIndex !== undefined ? opts.focusHighlightIndex : -1
    const viewportExpansion = opts.viewportExpansion !== undefined ? opts.viewportExpansion : -1
    const debugMode = opts.debugMode !== undefined ? opts.debugMode : false
    const highlightOpacity = opts.highlightOpacity !== undefined ? opts.highlightOpacity : 0.0
    const highlightLabelOpacity = opts.highlightLabelOpacity !== undefined ? opts.highlightLabelOpacity : 0.1

    let highlightIndex = 0

    // extraData WeakMap for scrollable element info
    const extraData = new WeakMap()

    function addExtraData(element, data) {
      if (!element || element.nodeType !== Node.ELEMENT_NODE) return
      extraData.set(element, Object.assign({}, extraData.get(element), data))
    }

    // -----------------------------------------------------------------------
    // DOM_CACHE - caches bounding rects, client rects, computed styles
    // -----------------------------------------------------------------------
    const DOM_CACHE = {
      boundingRects: new WeakMap(),
      clientRects: new WeakMap(),
      computedStyles: new WeakMap(),
      clearCache: function () {
        DOM_CACHE.boundingRects = new WeakMap()
        DOM_CACHE.clientRects = new WeakMap()
        DOM_CACHE.computedStyles = new WeakMap()
      },
    }

    function getCachedBoundingRect(element) {
      if (!element) return null
      if (DOM_CACHE.boundingRects.has(element)) {
        return DOM_CACHE.boundingRects.get(element)
      }
      const rect = element.getBoundingClientRect()
      if (rect) {
        DOM_CACHE.boundingRects.set(element, rect)
      }
      return rect
    }

    function getCachedComputedStyle(element) {
      if (!element) return null
      if (DOM_CACHE.computedStyles.has(element)) {
        return DOM_CACHE.computedStyles.get(element)
      }
      const style = window.getComputedStyle(element)
      if (style) {
        DOM_CACHE.computedStyles.set(element, style)
      }
      return style
    }

    function getCachedClientRects(element) {
      if (!element) return null
      if (DOM_CACHE.clientRects.has(element)) {
        return DOM_CACHE.clientRects.get(element)
      }
      const rects = element.getClientRects()
      if (rects) {
        DOM_CACHE.clientRects.set(element, rects)
      }
      return rects
    }

    // -----------------------------------------------------------------------
    // DOM_HASH_MAP - flat map of all nodes indexed by ID
    // -----------------------------------------------------------------------
    const DOM_HASH_MAP = {}
    const ID = { current: 0 }

    // xpathCache (kept for potential future use, not used in output)
    const xpathCache = new WeakMap()

    // -----------------------------------------------------------------------
    // highlightElement
    // -----------------------------------------------------------------------
    function highlightElement(element, index, parentIframe) {
      if (!element) return index

      const overlays = []
      let label = null
      let labelWidth = 20
      let labelHeight = 16
      let cleanupFn = null

      try {
        // Create or get highlight container
        let container = document.getElementById(HIGHLIGHT_CONTAINER_ID)
        if (!container) {
          container = document.createElement('div')
          container.id = HIGHLIGHT_CONTAINER_ID
          container.style.position = 'fixed'
          container.style.pointerEvents = 'none'
          container.style.top = '0'
          container.style.left = '0'
          container.style.width = '100%'
          container.style.height = '100%'
          container.style.zIndex = '2147483640'
          container.style.backgroundColor = 'transparent'
          document.body.appendChild(container)
        }

        // Get element client rects
        const rects = element.getClientRects()
        if (!rects || rects.length === 0) return index

        // Generate a color based on the index
        const colors = [
          '#FF0000',
          '#00FF00',
          '#0000FF',
          '#FFA500',
          '#800080',
          '#008080',
          '#FF69B4',
          '#4B0082',
          '#FF4500',
          '#2E8B57',
          '#DC143C',
          '#4682B4',
        ]
        const colorIndex = index % colors.length
        let baseColor = colors[colorIndex]

        const backgroundColor =
          baseColor +
          Math.floor(highlightOpacity * 255)
            .toString(16)
            .padStart(2, '0')
        baseColor =
          baseColor +
          Math.floor(highlightLabelOpacity * 255)
            .toString(16)
            .padStart(2, '0')

        // Get iframe offset if necessary
        let iframeOffset = { x: 0, y: 0 }
        if (parentIframe) {
          const iframeRect = parentIframe.getBoundingClientRect()
          iframeOffset.x = iframeRect.left
          iframeOffset.y = iframeRect.top
        }

        // Create fragment to hold overlay elements
        const fragment = document.createDocumentFragment()

        // Create highlight overlays for each client rect
        for (let ri = 0; ri < rects.length; ri++) {
          const rect = rects[ri]
          if (rect.width === 0 || rect.height === 0) continue

          const overlay = document.createElement('div')
          overlay.style.position = 'fixed'
          overlay.style.border = '2px solid ' + baseColor
          overlay.style.backgroundColor = backgroundColor
          overlay.style.pointerEvents = 'none'
          overlay.style.boxSizing = 'border-box'

          const top = rect.top + iframeOffset.y
          const left = rect.left + iframeOffset.x

          overlay.style.top = top + 'px'
          overlay.style.left = left + 'px'
          overlay.style.width = rect.width + 'px'
          overlay.style.height = rect.height + 'px'

          fragment.appendChild(overlay)
          overlays.push({ element: overlay, initialRect: rect })
        }

        // Create and position a single label relative to the first rect
        const firstRect = rects[0]
        label = document.createElement('div')
        label.className = 'playwright-highlight-label'
        label.style.position = 'fixed'
        label.style.background = baseColor
        label.style.color = 'white'
        label.style.padding = '1px 4px'
        label.style.borderRadius = '4px'
        label.style.fontSize = Math.min(12, Math.max(8, firstRect.height / 2)) + 'px'
        label.textContent = index.toString()

        labelWidth = label.offsetWidth > 0 ? label.offsetWidth : labelWidth
        labelHeight = label.offsetHeight > 0 ? label.offsetHeight : labelHeight

        const firstRectTop = firstRect.top + iframeOffset.y
        const firstRectLeft = firstRect.left + iframeOffset.x

        let labelTop = firstRectTop + 2
        let labelLeft = firstRectLeft + firstRect.width - labelWidth - 2

        // Adjust label position if first rect is too small
        if (firstRect.width < labelWidth + 4 || firstRect.height < labelHeight + 4) {
          labelTop = firstRectTop - labelHeight - 2
          labelLeft = firstRectLeft + firstRect.width - labelWidth
          if (labelLeft < iframeOffset.x) labelLeft = firstRectLeft
        }

        // Ensure label stays within viewport bounds
        labelTop = Math.max(0, Math.min(labelTop, window.innerHeight - labelHeight))
        labelLeft = Math.max(0, Math.min(labelLeft, window.innerWidth - labelWidth))

        label.style.top = labelTop + 'px'
        label.style.left = labelLeft + 'px'

        fragment.appendChild(label)

        // Update positions on scroll/resize
        var updatePositions = function () {
          const newRects = element.getClientRects()
          var newIframeOffset = { x: 0, y: 0 }

          if (parentIframe) {
            const iframeRect = parentIframe.getBoundingClientRect()
            newIframeOffset.x = iframeRect.left
            newIframeOffset.y = iframeRect.top
          }

          // Update each overlay
          overlays.forEach(function (overlayData, i) {
            if (i < newRects.length) {
              const newRect = newRects[i]
              const newTop = newRect.top + newIframeOffset.y
              const newLeft = newRect.left + newIframeOffset.x

              overlayData.element.style.top = newTop + 'px'
              overlayData.element.style.left = newLeft + 'px'
              overlayData.element.style.width = newRect.width + 'px'
              overlayData.element.style.height = newRect.height + 'px'
              overlayData.element.style.display =
                newRect.width === 0 || newRect.height === 0 ? 'none' : 'block'
            } else {
              overlayData.element.style.display = 'none'
            }
          })

          // If there are fewer new rects than overlays, hide the extras
          if (newRects.length < overlays.length) {
            for (var i = newRects.length; i < overlays.length; i++) {
              overlays[i].element.style.display = 'none'
            }
          }

          // Update label position based on the first new rect
          if (label && newRects.length > 0) {
            const firstNewRect = newRects[0]
            const firstNewRectTop = firstNewRect.top + newIframeOffset.y
            const firstNewRectLeft = firstNewRect.left + newIframeOffset.x

            var newLabelTop = firstNewRectTop + 2
            var newLabelLeft = firstNewRectLeft + firstNewRect.width - labelWidth - 2

            if (firstNewRect.width < labelWidth + 4 || firstNewRect.height < labelHeight + 4) {
              newLabelTop = firstNewRectTop - labelHeight - 2
              newLabelLeft = firstNewRectLeft + firstNewRect.width - labelWidth
              if (newLabelLeft < newIframeOffset.x) newLabelLeft = firstNewRectLeft
            }

            newLabelTop = Math.max(0, Math.min(newLabelTop, window.innerHeight - labelHeight))
            newLabelLeft = Math.max(0, Math.min(newLabelLeft, window.innerWidth - labelWidth))

            label.style.top = newLabelTop + 'px'
            label.style.left = newLabelLeft + 'px'
            label.style.display = 'block'
          } else if (label) {
            label.style.display = 'none'
          }
        }

        var throttleFunction = function (func, delay) {
          var lastCall = 0
          return function () {
            var now = performance.now()
            if (now - lastCall < delay) return
            lastCall = now
            return func.apply(this, arguments)
          }
        }

        var throttledUpdatePositions = throttleFunction(updatePositions, 16) // ~60fps
        window.addEventListener('scroll', throttledUpdatePositions, true)
        window.addEventListener('resize', throttledUpdatePositions)

        // Add cleanup function
        cleanupFn = function () {
          window.removeEventListener('scroll', throttledUpdatePositions, true)
          window.removeEventListener('resize', throttledUpdatePositions)
          overlays.forEach(function (overlay) {
            overlay.element.remove()
          })
          if (label) label.remove()
        }

        // Then add fragment to container in one operation
        container.appendChild(fragment)

        return index + 1
      } finally {
        // Store cleanup function for later use
        if (cleanupFn) {
          if (!window._highlightCleanupFunctions) window._highlightCleanupFunctions = []
          window._highlightCleanupFunctions.push(cleanupFn)
        }
      }
    }

    // -----------------------------------------------------------------------
    // getElementPosition
    // -----------------------------------------------------------------------
    function getElementPosition(currentElement) {
      if (!currentElement.parentElement) {
        return 0
      }

      var tagName = currentElement.nodeName.toLowerCase()

      var siblings = Array.from(currentElement.parentElement.children).filter(function (sib) {
        return sib.nodeName.toLowerCase() === tagName
      })

      if (siblings.length === 1) {
        return 0
      }

      var idx = siblings.indexOf(currentElement) + 1
      return idx
    }

    // -----------------------------------------------------------------------
    // getXPathTree (kept for completeness, not used in output)
    // -----------------------------------------------------------------------
    function getXPathTree(element, stopAtBoundary) {
      if (stopAtBoundary === undefined) stopAtBoundary = true
      if (xpathCache.has(element)) return xpathCache.get(element)

      var segments = []
      var currentElement = element

      while (currentElement && currentElement.nodeType === Node.ELEMENT_NODE) {
        if (
          stopAtBoundary &&
          (currentElement.parentNode instanceof ShadowRoot ||
            currentElement.parentNode instanceof HTMLIFrameElement)
        ) {
          break
        }

        var position = getElementPosition(currentElement)
        var tagName = currentElement.nodeName.toLowerCase()
        var xpathIndex = position > 0 ? '[' + position + ']' : ''
        segments.unshift(tagName + xpathIndex)

        currentElement = currentElement.parentNode
      }

      var result = segments.join('/')
      xpathCache.set(element, result)
      return result
    }

    // -----------------------------------------------------------------------
    // isScrollableElement
    // -----------------------------------------------------------------------
    function isScrollableElement(element) {
      if (!element || element.nodeType !== Node.ELEMENT_NODE) {
        return null
      }

      var style = getCachedComputedStyle(element)
      if (!style) return null

      // Check if the element is a block-level element
      var display = style.display
      if (display === 'inline' || display === 'inline-block') {
        return null
      }

      // Check overflow properties
      var overflowX = style.overflowX
      var overflowY = style.overflowY

      var hasScrollbarSignal =
        (style.scrollbarWidth && style.scrollbarWidth !== 'auto') ||
        (style.scrollbarGutter && style.scrollbarGutter !== 'auto')

      var scrollableX = overflowX === 'auto' || overflowX === 'scroll'
      var scrollableY = overflowY === 'auto' || overflowY === 'scroll'

      if (!scrollableX && !scrollableY && !hasScrollbarSignal) {
        return null
      }

      var scrollWidth = element.scrollWidth - element.clientWidth
      var scrollHeight = element.scrollHeight - element.clientHeight

      // Consider small distances as not scrollable
      var threshold = 4

      if (scrollWidth < threshold && scrollHeight < threshold) {
        return null
      }

      if (!scrollableY && !hasScrollbarSignal && scrollWidth < threshold) {
        return null
      }

      if (!scrollableX && !hasScrollbarSignal && scrollHeight < threshold) {
        return null
      }

      var distanceToTop = element.scrollTop
      var distanceToLeft = element.scrollLeft
      var distanceToRight = element.scrollWidth - element.clientWidth - element.scrollLeft
      var distanceToBottom = element.scrollHeight - element.clientHeight - element.scrollTop

      var scrollData = {
        top: distanceToTop,
        right: distanceToRight,
        bottom: distanceToBottom,
        left: distanceToLeft,
      }

      // Store extra data for the element
      addExtraData(element, {
        scrollable: true,
        scrollData: scrollData,
      })

      return scrollData
    }

    // -----------------------------------------------------------------------
    // isTextNodeVisible
    // -----------------------------------------------------------------------
    function isTextNodeVisible(textNode) {
      try {
        // Special case: when viewportExpansion is -1, consider all text nodes as visible
        if (viewportExpansion === -1) {
          var parentElement = textNode.parentElement
          if (!parentElement) return false

          try {
            return parentElement.checkVisibility({
              checkOpacity: true,
              checkVisibilityCSS: true,
            })
          } catch (e) {
            var style = window.getComputedStyle(parentElement)
            return style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0'
          }
        }

        var range = document.createRange()
        range.selectNodeContents(textNode)
        var rects = range.getClientRects()

        if (!rects || rects.length === 0) {
          return false
        }

        var isAnyRectVisible = false
        var isAnyRectInViewport = false

        for (var i = 0; i < rects.length; i++) {
          var rect = rects[i]
          if (rect.width > 0 && rect.height > 0) {
            isAnyRectVisible = true

            if (
              !(
                rect.bottom < -viewportExpansion ||
                rect.top > window.innerHeight + viewportExpansion ||
                rect.right < -viewportExpansion ||
                rect.left > window.innerWidth + viewportExpansion
              )
            ) {
              isAnyRectInViewport = true
              break
            }
          }
        }

        if (!isAnyRectVisible || !isAnyRectInViewport) {
          return false
        }

        // Check parent visibility
        var parentElement = textNode.parentElement
        if (!parentElement) return false

        try {
          return parentElement.checkVisibility({
            checkOpacity: true,
            checkVisibilityCSS: true,
          })
        } catch (e) {
          var style = window.getComputedStyle(parentElement)
          return style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0'
        }
      } catch (e) {
        return false
      }
    }

    // -----------------------------------------------------------------------
    // isElementAccepted
    // -----------------------------------------------------------------------
    function isElementAccepted(element) {
      if (!element || !element.tagName) return false

      var alwaysAccept = new Set([
        'body',
        'div',
        'main',
        'article',
        'section',
        'nav',
        'header',
        'footer',
      ])
      var tagName = element.tagName.toLowerCase()

      if (alwaysAccept.has(tagName)) return true

      var leafElementDenyList = new Set([
        'svg',
        'script',
        'style',
        'link',
        'meta',
        'noscript',
        'template',
      ])

      return !leafElementDenyList.has(tagName)
    }

    // -----------------------------------------------------------------------
    // isElementVisible
    // -----------------------------------------------------------------------
    function isElementVisible(element) {
      var style = getCachedComputedStyle(element)
      return (
        element.offsetWidth > 0 &&
        element.offsetHeight > 0 &&
        style &&
        style.visibility !== 'hidden' &&
        style.display !== 'none'
      )
    }

    // -----------------------------------------------------------------------
    // isInteractiveElement
    // -----------------------------------------------------------------------
    function isInteractiveElement(element) {
      if (!element || element.nodeType !== Node.ELEMENT_NODE) {
        return false
      }

      // Cache the tagName and style lookups
      var tagName = element.tagName.toLowerCase()
      var style = getCachedComputedStyle(element)

      // Define interactive cursors
      var interactiveCursors = new Set([
        'pointer',
        'move',
        'text',
        'grab',
        'grabbing',
        'cell',
        'copy',
        'alias',
        'all-scroll',
        'col-resize',
        'context-menu',
        'crosshair',
        'e-resize',
        'ew-resize',
        'help',
        'n-resize',
        'ne-resize',
        'nesw-resize',
        'ns-resize',
        'nw-resize',
        'nwse-resize',
        'row-resize',
        's-resize',
        'se-resize',
        'sw-resize',
        'vertical-text',
        'w-resize',
        'zoom-in',
        'zoom-out',
      ])

      // Define non-interactive cursors
      var nonInteractiveCursors = new Set([
        'not-allowed',
        'no-drop',
        'wait',
        'progress',
        'initial',
        'inherit',
      ])

      function doesElementHaveInteractivePointer(element) {
        if (element.tagName.toLowerCase() === 'html') return false
        if (style && style.cursor && interactiveCursors.has(style.cursor)) return true
        return false
      }

      var isInteractiveCursor = doesElementHaveInteractivePointer(element)

      // Genius fix for almost all interactive elements
      if (isInteractiveCursor) {
        return true
      }

      var interactiveElements = new Set([
        'a',
        'button',
        'input',
        'select',
        'textarea',
        'details',
        'summary',
        'label',
        'option',
        'optgroup',
        'fieldset',
        'legend',
      ])

      // Define explicit disable attributes and properties
      var explicitDisableTags = new Set([
        'disabled',
        'readonly',
      ])

      // handle inputs, select, checkbox, radio, textarea, button
      if (interactiveElements.has(tagName)) {
        // Check for non-interactive cursor
        if (style && style.cursor && nonInteractiveCursors.has(style.cursor)) {
          return false
        }

        // Check for explicit disable attributes
        for (var _i = 0, _arr = Array.from(explicitDisableTags); _i < _arr.length; _i++) {
          var disableTag = _arr[_i]
          if (
            element.hasAttribute(disableTag) ||
            element.getAttribute(disableTag) === 'true' ||
            element.getAttribute(disableTag) === ''
          ) {
            return false
          }
        }

        // Check for disabled property on form elements
        if (element.disabled) {
          return false
        }

        // Check for readonly property on form elements
        if (element.readOnly) {
          return false
        }

        // Check for inert property
        if (element.inert) {
          return false
        }

        return true
      }

      var role = element.getAttribute('role')
      var ariaRole = element.getAttribute('aria-role')

      // Check for contenteditable attribute
      if (element.getAttribute('contenteditable') === 'true' || element.isContentEditable) {
        return true
      }

      // Added enhancement to capture dropdown interactive elements
      if (
        element.classList &&
        (element.classList.contains('button') ||
          element.classList.contains('dropdown-toggle') ||
          element.getAttribute('data-index') ||
          element.getAttribute('data-toggle') === 'dropdown' ||
          element.getAttribute('aria-haspopup') === 'true')
      ) {
        return true
      }

      var interactiveRoles = new Set([
        'button',
        'menu',
        'menubar',
        'menuitem',
        'menuitemradio',
        'menuitemcheckbox',
        'radio',
        'checkbox',
        'tab',
        'switch',
        'slider',
        'spinbutton',
        'combobox',
        'searchbox',
        'textbox',
        'listbox',
        'option',
        'scrollbar',
      ])

      // Basic role/attribute checks
      var hasInteractiveRole =
        interactiveElements.has(tagName) ||
        (role && interactiveRoles.has(role)) ||
        (ariaRole && interactiveRoles.has(ariaRole))

      if (hasInteractiveRole) return true

      // check whether element has event listeners by window.getEventListeners
      try {
        if (typeof getEventListeners === 'function') {
          var listeners = getEventListeners(element)
          var mouseEvents = ['click', 'mousedown', 'mouseup', 'dblclick']
          for (var ei = 0; ei < mouseEvents.length; ei++) {
            var eventType = mouseEvents[ei]
            if (listeners[eventType] && listeners[eventType].length > 0) {
              return true
            }
          }
        }

        var getEventListenersForNode =
          (element && element.ownerDocument && element.ownerDocument.defaultView
            ? element.ownerDocument.defaultView.getEventListenersForNode
            : undefined) ||
          window.getEventListenersForNode
        if (typeof getEventListenersForNode === 'function') {
          var listeners = getEventListenersForNode(element)
          var interactionEvents = [
            'click',
            'mousedown',
            'mouseup',
            'keydown',
            'keyup',
            'submit',
            'change',
            'input',
            'focus',
            'blur',
          ]
          for (var ei = 0; ei < interactionEvents.length; ei++) {
            for (var li = 0; li < listeners.length; li++) {
              if (listeners[li].type === interactionEvents[ei]) {
                return true
              }
            }
          }
        }
        // Fallback: Check common event attributes if getEventListeners is not available
        var commonMouseAttrs = ['onclick', 'onmousedown', 'onmouseup', 'ondblclick']
        for (var ai = 0; ai < commonMouseAttrs.length; ai++) {
          var attr = commonMouseAttrs[ai]
          if (element.hasAttribute(attr) || typeof element[attr] === 'function') {
            return true
          }
        }
      } catch (e) {
        // If checking listeners fails, rely on other checks
      }

      // Scrollable element detection
      if (isScrollableElement(element)) {
        return true
      }

      return false
    }

    // -----------------------------------------------------------------------
    // isTopElement
    // -----------------------------------------------------------------------
    function isTopElement(element) {
      // Special case: when viewportExpansion is -1, consider all elements as "top" elements
      if (viewportExpansion === -1) {
        return true
      }

      var rects = getCachedClientRects(element)

      if (!rects || rects.length === 0) {
        return false
      }

      var isAnyRectInViewport = false
      for (var i = 0; i < rects.length; i++) {
        var rect = rects[i]
        if (
          rect.width > 0 &&
          rect.height > 0 &&
          !(
            rect.bottom < -viewportExpansion ||
            rect.top > window.innerHeight + viewportExpansion ||
            rect.right < -viewportExpansion ||
            rect.left > window.innerWidth + viewportExpansion
          )
        ) {
          isAnyRectInViewport = true
          break
        }
      }

      if (!isAnyRectInViewport) {
        return false
      }

      // Find the correct document context and root element
      var doc = element.ownerDocument

      // If we're in an iframe, elements are considered top by default
      if (doc !== window.document) {
        return true
      }

      // find a rect that has width and height as sample
      var rect = Array.from(rects).find(function (r) {
        return r.width > 0 && r.height > 0
      })
      if (!rect) {
        return false
      }

      // For shadow DOM, we need to check within its own root context
      var shadowRoot = element.getRootNode()
      if (shadowRoot instanceof ShadowRoot) {
        var centerX = rect.left + rect.width / 2
        var centerY = rect.top + rect.height / 2

        try {
          var topEl = shadowRoot.elementFromPoint(centerX, centerY)
          if (!topEl) return false

          var current = topEl
          while (current && current !== shadowRoot) {
            if (current === element) return true
            current = current.parentElement
          }
          return false
        } catch (e) {
          return true
        }
      }

      var margin = 5

      var checkPoints = [
        { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 },
        { x: rect.left + margin, y: rect.top + margin },
        { x: rect.right - margin, y: rect.bottom - margin },
      ]

      return checkPoints.some(function (point) {
        try {
          var topEl = document.elementFromPoint(point.x, point.y)
          if (!topEl) return false

          var current = topEl
          while (current && current !== document.documentElement) {
            if (current === element) return true
            current = current.parentElement
          }
          return false
        } catch (e) {
          return true
        }
      })
    }

    // -----------------------------------------------------------------------
    // isInExpandedViewport
    // -----------------------------------------------------------------------
    function isInExpandedViewport(element, viewportExpansion) {
      if (viewportExpansion === -1) {
        return true
      }

      var rects = element.getClientRects()

      if (!rects || rects.length === 0) {
        var boundingRect = getCachedBoundingRect(element)
        if (!boundingRect || boundingRect.width === 0 || boundingRect.height === 0) {
          return false
        }
        return !(
          boundingRect.bottom < -viewportExpansion ||
          boundingRect.top > window.innerHeight + viewportExpansion ||
          boundingRect.right < -viewportExpansion ||
          boundingRect.left > window.innerWidth + viewportExpansion
        )
      }

      for (var i = 0; i < rects.length; i++) {
        var rect = rects[i]
        if (rect.width === 0 || rect.height === 0) continue

        if (
          !(
            rect.bottom < -viewportExpansion ||
            rect.top > window.innerHeight + viewportExpansion ||
            rect.right < -viewportExpansion ||
            rect.left > window.innerWidth + viewportExpansion
          )
        ) {
          return true
        }
      }

      return false
    }

    // -----------------------------------------------------------------------
    // isInteractiveCandidate
    // -----------------------------------------------------------------------
    function isInteractiveCandidate(element) {
      if (!element || element.nodeType !== Node.ELEMENT_NODE) return false

      var tagName = element.tagName.toLowerCase()

      // Fast-path for common interactive elements
      var interactiveElements = new Set([
        'a',
        'button',
        'input',
        'select',
        'textarea',
        'details',
        'summary',
        'label',
      ])

      if (interactiveElements.has(tagName)) return true

      // Quick attribute checks without getting full lists
      var hasQuickInteractiveAttr =
        element.hasAttribute('onclick') ||
        element.hasAttribute('role') ||
        element.hasAttribute('tabindex') ||
        hasInteractiveAria(element) ||
        element.hasAttribute('data-action') ||
        element.getAttribute('contenteditable') === 'true'

      return hasQuickInteractiveAttr
    }

    // -----------------------------------------------------------------------
    // isHeuristicallyInteractive
    // -----------------------------------------------------------------------
    function isHeuristicallyInteractive(element) {
      if (!element || element.nodeType !== Node.ELEMENT_NODE) return false

      // Skip non-visible elements early for performance
      if (!isElementVisible(element)) return false

      // Check for common attributes that often indicate interactivity
      var hasInteractiveAttributes =
        element.hasAttribute('role') ||
        element.hasAttribute('tabindex') ||
        element.hasAttribute('onclick') ||
        typeof element.onclick === 'function'

      // Check for semantic class names suggesting interactivity
      var hasInteractiveClass = /\b(btn|clickable|menu|item|entry|link)\b/i.test(
        element.className || ''
      )

      // Determine whether the element is inside a known interactive container
      var isInKnownContainer = Boolean(
        element.closest('button,a,[role="button"],.menu,.dropdown,.list,.toolbar')
      )

      // Ensure the element has at least one visible child (to avoid marking empty wrappers)
      var hasVisibleChildren = Array.from(element.children).some(isElementVisible)

      // Avoid highlighting elements whose parent is <body> (top-level wrappers)
      var isParentBody = element.parentElement && element.parentElement.isSameNode(document.body)

      return (
        (isInteractiveElement(element) || hasInteractiveAttributes || hasInteractiveClass) &&
        hasVisibleChildren &&
        isInKnownContainer &&
        !isParentBody
      )
    }

    // -----------------------------------------------------------------------
    // isElementDistinctInteraction
    // -----------------------------------------------------------------------
    function isElementDistinctInteraction(element) {
      if (!element || element.nodeType !== Node.ELEMENT_NODE) {
        return false
      }

      var tagName = element.tagName.toLowerCase()
      var role = element.getAttribute('role')

      // Check if it's an iframe - always distinct boundary
      if (tagName === 'iframe') {
        return true
      }

      // Check tag name
      if (DISTINCT_INTERACTIVE_TAGS.has(tagName)) {
        return true
      }
      // Check interactive roles
      if (role && DISTINCT_INTERACTIVE_ROLES.has(role)) {
        return true
      }
      // Check contenteditable
      if (element.isContentEditable || element.getAttribute('contenteditable') === 'true') {
        return true
      }
      // Check for common testing/automation attributes
      if (
        element.hasAttribute('data-testid') ||
        element.hasAttribute('data-cy') ||
        element.hasAttribute('data-test')
      ) {
        return true
      }
      // Check for explicit onclick handler (attribute or property)
      if (element.hasAttribute('onclick') || typeof element.onclick === 'function') {
        return true
      }
      // ARIA state attributes imply the element manages its own interaction state
      if (hasInteractiveAria(element)) {
        return true
      }

      // Check for other common interaction event listeners
      try {
        var getEventListenersForNode =
          (element && element.ownerDocument && element.ownerDocument.defaultView
            ? element.ownerDocument.defaultView.getEventListenersForNode
            : undefined) ||
          window.getEventListenersForNode
        if (typeof getEventListenersForNode === 'function') {
          var listeners = getEventListenersForNode(element)
          var interactionEvents = [
            'click',
            'mousedown',
            'mouseup',
            'keydown',
            'keyup',
            'submit',
            'change',
            'input',
            'focus',
            'blur',
          ]
          for (var ei = 0; ei < interactionEvents.length; ei++) {
            for (var li = 0; li < listeners.length; li++) {
              if (listeners[li].type === interactionEvents[ei]) {
                return true
              }
            }
          }
        }
        // Fallback: Check common event attributes if getEventListeners is not available
        var commonEventAttrs = [
          'onmousedown',
          'onmouseup',
          'onkeydown',
          'onkeyup',
          'onsubmit',
          'onchange',
          'oninput',
          'onfocus',
          'onblur',
        ]
        if (commonEventAttrs.some(function (attr) { return element.hasAttribute(attr) })) {
          return true
        }
      } catch (e) {
        // If checking listeners fails, rely on other checks
      }

      // if the element is not strictly interactive but appears clickable based on heuristic signals
      if (isHeuristicallyInteractive(element)) {
        return true
      }

      // Scrollable containers are always distinct
      if (extraData.get(element) && extraData.get(element).scrollable) {
        return true
      }

      return false
    }

    // -----------------------------------------------------------------------
    // handleHighlighting
    // -----------------------------------------------------------------------
    function handleHighlighting(nodeData, node, parentIframe, isParentHighlighted) {
      if (!nodeData.isInteractive) return false

      var shouldHighlight = false
      if (!isParentHighlighted) {
        shouldHighlight = true
      } else {
        if (isElementDistinctInteraction(node)) {
          shouldHighlight = true
        } else {
          shouldHighlight = false
        }
      }

      if (shouldHighlight) {
        nodeData.isInViewport = isInExpandedViewport(node, viewportExpansion)

        if (nodeData.isInViewport || viewportExpansion === -1) {
          nodeData.highlightIndex = highlightIndex++

          if (doHighlightElements) {
            if (focusHighlightIndex >= 0) {
              if (focusHighlightIndex === nodeData.highlightIndex) {
                highlightElement(node, nodeData.highlightIndex, parentIframe)
              }
            } else {
              highlightElement(node, nodeData.highlightIndex, parentIframe)
            }
            return true
          }
        }
      }

      return false
    }

    // -----------------------------------------------------------------------
    // buildDomTree - the core recursive builder
    // -----------------------------------------------------------------------
    function buildDomTree(node, parentIframe, isParentHighlighted) {
      if (isParentHighlighted === undefined) isParentHighlighted = false

      // Fast rejection checks first
      if (
        !node ||
        node.id === HIGHLIGHT_CONTAINER_ID ||
        (node.nodeType !== Node.ELEMENT_NODE && node.nodeType !== Node.TEXT_NODE)
      ) {
        return null
      }

      if (!node || node.id === HIGHLIGHT_CONTAINER_ID) {
        return null
      }

      // Skip elements with data-browser-use-ignore or data-page-agent-ignore
      if (
        (node.dataset &&
          (node.dataset.browserUseIgnore === 'true' || node.dataset.pageAgentIgnore === 'true'))
      ) {
        return null
      }

      // Exclude aria-hidden elements
      if (node.getAttribute && node.getAttribute('aria-hidden') === 'true') {
        return null
      }

      // Special handling for root node (body)
      if (node === document.body) {
        var nodeData = {
          tagName: 'body',
          attributes: {},
          xpath: '/body',
          children: [],
        }

        for (var ci = 0; ci < node.childNodes.length; ci++) {
          var child = node.childNodes[ci]
          var domElement = buildDomTree(child, parentIframe, false)
          if (domElement) nodeData.children.push(domElement)
        }

        var id = '' + ID.current++
        DOM_HASH_MAP[id] = nodeData
        return id
      }

      // Early bailout for non-element nodes except text
      if (node.nodeType !== Node.ELEMENT_NODE && node.nodeType !== Node.TEXT_NODE) {
        return null
      }

      // Process text nodes
      if (node.nodeType === Node.TEXT_NODE) {
        var textContent = node.textContent ? node.textContent.trim() : ''
        if (!textContent) {
          return null
        }

        var parentElement = node.parentElement
        if (!parentElement || parentElement.tagName.toLowerCase() === 'script') {
          return null
        }

        var id = '' + ID.current++
        DOM_HASH_MAP[id] = {
          type: 'TEXT_NODE',
          text: textContent,
          isVisible: isTextNodeVisible(node),
        }
        return id
      }

      // Quick checks for element nodes
      if (node.nodeType === Node.ELEMENT_NODE && !isElementAccepted(node)) {
        return null
      }

      // Early viewport check
      if (viewportExpansion !== -1 && !node.shadowRoot) {
        var rect = getCachedBoundingRect(node)
        var style = getCachedComputedStyle(node)

        var isFixedOrSticky = style && (style.position === 'fixed' || style.position === 'sticky')
        var hasSize = node.offsetWidth > 0 || node.offsetHeight > 0

        if (
          !rect ||
          (!isFixedOrSticky &&
            !hasSize &&
            (rect.bottom < -viewportExpansion ||
              rect.top > window.innerHeight + viewportExpansion ||
              rect.right < -viewportExpansion ||
              rect.left > window.innerWidth + viewportExpansion))
        ) {
          return null
        }
      }

      var nodeData = {
        tagName: node.tagName.toLowerCase(),
        attributes: {},
        children: [],
      }

      // Get attributes for interactive elements or potential text containers
      if (
        isInteractiveCandidate(node) ||
        node.tagName.toLowerCase() === 'iframe' ||
        node.tagName.toLowerCase() === 'body'
      ) {
        var attributeNames = node.getAttributeNames ? node.getAttributeNames() : []
        for (var ai = 0; ai < attributeNames.length; ai++) {
          var name = attributeNames[ai]
          var value = node.getAttribute(name)
          nodeData.attributes[name] = value
        }

        // Workaround for input.checked
        if (
          node.tagName.toLowerCase() === 'input' &&
          (node.type === 'checkbox' || node.type === 'radio')
        ) {
          nodeData.attributes.checked = node.checked ? 'true' : 'false'
        }
      }

      var nodeWasHighlighted = false
      // Perform visibility, interactivity, and highlighting checks
      if (node.nodeType === Node.ELEMENT_NODE) {
        nodeData.isVisible = isElementVisible(node)
        if (nodeData.isVisible) {
          nodeData.isTopElement = isTopElement(node)

          // Special handling for ARIA menu containers
          var role = node.getAttribute('role')
          var isMenuContainer = role === 'menu' || role === 'menubar' || role === 'listbox'

          if (nodeData.isTopElement || isMenuContainer) {
            nodeData.isInteractive = isInteractiveElement(node)
            nodeWasHighlighted = handleHighlighting(nodeData, node, parentIframe, isParentHighlighted)

            // Direct dom ref
            nodeData.ref = node

            // Make sure attributes exist for interactive candidates
            if (nodeData.isInteractive && Object.keys(nodeData.attributes).length === 0) {
              var attributeNames = node.getAttributeNames ? node.getAttributeNames() : []
              for (var ai = 0; ai < attributeNames.length; ai++) {
                var name = attributeNames[ai]
                var value = node.getAttribute(name)
                nodeData.attributes[name] = value
              }
            }
          }
        }
      }

      // Process children, with special handling for iframes and rich text editors
      if (node.tagName) {
        var tagName = node.tagName.toLowerCase()

        // Handle iframes
        if (tagName === 'iframe') {
          try {
            var iframeDoc = node.contentDocument || (node.contentWindow ? node.contentWindow.document : null)
            if (iframeDoc) {
              for (var ci = 0; ci < iframeDoc.childNodes.length; ci++) {
                var child = iframeDoc.childNodes[ci]
                var domElement = buildDomTree(child, node, false)
                if (domElement) nodeData.children.push(domElement)
              }
            }
          } catch (e) {
            // Unable to access iframe
          }
        }
        // Handle rich text editors and contenteditable elements
        else if (
          node.isContentEditable ||
          node.getAttribute('contenteditable') === 'true' ||
          node.id === 'tinymce' ||
          (node.classList && node.classList.contains('mce-content-body')) ||
          (tagName === 'body' && node.getAttribute('data-id') && node.getAttribute('data-id').startsWith('mce_'))
        ) {
          for (var ci = 0; ci < node.childNodes.length; ci++) {
            var child = node.childNodes[ci]
            var domElement = buildDomTree(child, parentIframe, nodeWasHighlighted)
            if (domElement) nodeData.children.push(domElement)
          }
        } else {
          // Handle shadow DOM
          if (node.shadowRoot) {
            nodeData.shadowRoot = true
            for (var ci = 0; ci < node.shadowRoot.childNodes.length; ci++) {
              var child = node.shadowRoot.childNodes[ci]
              var domElement = buildDomTree(child, parentIframe, nodeWasHighlighted)
              if (domElement) nodeData.children.push(domElement)
            }
          }
          // Handle regular elements
          for (var ci = 0; ci < node.childNodes.length; ci++) {
            var child = node.childNodes[ci]
            var passHighlightStatusToChild = nodeWasHighlighted || isParentHighlighted
            var domElement = buildDomTree(child, parentIframe, passHighlightStatusToChild)
            if (domElement) nodeData.children.push(domElement)
          }
        }
      }

      // Skip empty anchor tags only if they have no dimensions and no children
      if (nodeData.tagName === 'a' && nodeData.children.length === 0 && !nodeData.attributes.href) {
        var rect = getCachedBoundingRect(node)
        var hasSize =
          (rect && rect.width > 0 && rect.height > 0) || node.offsetWidth > 0 || node.offsetHeight > 0

        if (!hasSize) {
          return null
        }
      }

      // Add extra data field
      nodeData.extra = extraData.get(node) || null

      var id = '' + ID.current++
      DOM_HASH_MAP[id] = nodeData
      return id
    }

    // -----------------------------------------------------------------------
    // Execute: build the tree from document.body
    // -----------------------------------------------------------------------
    var rootId = buildDomTree(document.body)

    // Clear the cache before returning
    DOM_CACHE.clearCache()

    // Mark new elements
    var currentUrl = window.location.href
    for (var nodeId in DOM_HASH_MAP) {
      var node = DOM_HASH_MAP[nodeId]
      if (node.isInteractive && node.ref) {
        var ref = node.ref
        if (!newElementsCache.has(ref)) {
          newElementsCache.set(ref, currentUrl)
          node.isNew = true
        }
      }
    }

    return { rootId: rootId, map: DOM_HASH_MAP }
  }

  // ===========================================================================
  // flatTreeToString - serialize FlatDomTree to LLM-readable text
  // ===========================================================================

  function flatTreeToString(flatTree, includeAttributes, keepSemanticTags) {
    if (includeAttributes === undefined) includeAttributes = []
    if (keepSemanticTags === undefined) keepSemanticTags = false

    var DEFAULT_INCLUDE_ATTRIBUTES = [
      'title',
      'type',
      'checked',
      'name',
      'role',
      'value',
      'placeholder',
      'data-date-format',
      'alt',
      'aria-label',
      'aria-expanded',
      'data-state',
      'aria-checked',
      'id',
      'for',
      'target',
      'aria-haspopup',
      'aria-controls',
      'aria-owns',
      'contenteditable',
    ]

    var includeAttrs = includeAttributes.concat(DEFAULT_INCLUDE_ATTRIBUTES)

    // Helper function to cap text length
    function capTextLength(text, maxLength) {
      if (text.length > maxLength) {
        return text.substring(0, maxLength) + '...'
      }
      return text
    }

    // Build tree structure from flat map
    function buildTreeNode(nodeId) {
      var node = flatTree.map[nodeId]
      if (!node) return null

      if (node.type === 'TEXT_NODE') {
        return {
          type: 'text',
          text: node.text,
          isVisible: node.isVisible,
          parent: null,
          children: [],
        }
      } else {
        var children = []

        if (node.children) {
          for (var i = 0; i < node.children.length; i++) {
            var childId = node.children[i]
            var child = buildTreeNode(childId)
            if (child) {
              child.parent = null
              children.push(child)
            }
          }
        }

        return {
          type: 'element',
          tagName: node.tagName,
          attributes: node.attributes || {},
          isVisible: node.isVisible || false,
          isInteractive: node.isInteractive || false,
          isTopElement: node.isTopElement || false,
          isNew: node.isNew || false,
          highlightIndex: node.highlightIndex,
          parent: null,
          children: children,
          extra: node.extra || null,
        }
      }
    }

    // Set parent references
    function setParentReferences(node, parent) {
      node.parent = parent || null
      for (var i = 0; i < node.children.length; i++) {
        setParentReferences(node.children[i], node)
      }
    }

    // Build root node
    var rootNode = buildTreeNode(flatTree.rootId)
    if (!rootNode) return ''

    setParentReferences(rootNode)

    // Helper to check if text node has parent with highlight index
    function hasParentWithHighlightIndex(node) {
      var current = node.parent
      while (current) {
        if (current.type === 'element' && current.highlightIndex !== undefined) {
          return true
        }
        current = current.parent
      }
      return false
    }

    // Get all text until next clickable element
    function getAllTextTillNextClickableElement(node, maxDepth) {
      if (maxDepth === undefined) maxDepth = -1
      var textParts = []

      function collectText(currentNode, currentDepth) {
        if (maxDepth !== -1 && currentDepth > maxDepth) {
          return
        }

        if (
          currentNode.type === 'element' &&
          currentNode !== node &&
          currentNode.highlightIndex !== undefined
        ) {
          return
        }

        if (currentNode.type === 'text' && currentNode.text) {
          textParts.push(currentNode.text)
        } else if (currentNode.type === 'element') {
          for (var i = 0; i < currentNode.children.length; i++) {
            collectText(currentNode.children[i], currentDepth + 1)
          }
        }
      }

      collectText(node, 0)
      return textParts.join('\n').trim()
    }

    // Main processing function
    function processNode(node, depth, result) {
      var nextDepth = depth
      var depthStr = ''
      for (var d = 0; d < depth; d++) depthStr += '\t'

      if (node.type === 'element') {
        var isSemantic = keepSemanticTags && node.tagName && SEMANTIC_TAGS.has(node.tagName)

        // Add element with highlight_index
        if (node.highlightIndex !== undefined) {
          nextDepth += 1

          var text = getAllTextTillNextClickableElement(node)
          var attributesHtmlStr = ''

          if (includeAttrs.length > 0 && node.attributes) {
            var attributesToInclude = matchAttributes(node.attributes, includeAttrs)

            // Remove duplicate values (for attributes longer than 5 chars)
            var keys = Object.keys(attributesToInclude)
            if (keys.length > 1) {
              var keysToRemove = new Set()
              var seenValues = {}

              for (var ki = 0; ki < keys.length; ki++) {
                var key = keys[ki]
                var value = attributesToInclude[key]
                if (value.length > 5) {
                  if (value in seenValues) {
                    keysToRemove.add(key)
                  } else {
                    seenValues[value] = key
                  }
                }
              }

              keysToRemove.forEach(function (k) {
                delete attributesToInclude[k]
              })
            }

            // Remove role if it matches tagName
            if (attributesToInclude.role === node.tagName) {
              delete attributesToInclude.role
            }

            // Remove attributes that duplicate text content
            var attrsToRemoveIfTextMatches = ['aria-label', 'placeholder', 'title']
            for (var ai = 0; ai < attrsToRemoveIfTextMatches.length; ai++) {
              var attr = attrsToRemoveIfTextMatches[ai]
              if (
                attributesToInclude[attr] &&
                attributesToInclude[attr].toLowerCase().trim() === text.toLowerCase().trim()
              ) {
                delete attributesToInclude[attr]
              }
            }

            if (Object.keys(attributesToInclude).length > 0) {
              var attrParts = []
              var attrKeys = Object.keys(attributesToInclude)
              for (var aki = 0; aki < attrKeys.length; aki++) {
                var aKey = attrKeys[aki]
                attrParts.push(aKey + '=' + capTextLength(attributesToInclude[aKey], 20))
              }
              attributesHtmlStr = attrParts.join(' ')
            }
          }

          // Build the line
          var highlightIndicator = node.isNew
            ? '*[' + node.highlightIndex + ']'
            : '[' + node.highlightIndex + ']'
          var line = depthStr + highlightIndicator + '<' + (node.tagName || '')

          if (attributesHtmlStr) {
            line += ' ' + attributesHtmlStr
          }

          // Scrollable data
          if (node.extra) {
            if (node.extra.scrollable) {
              var scrollDataText = ''
              if (node.extra.scrollData && node.extra.scrollData.left)
                scrollDataText += 'left=' + node.extra.scrollData.left + ', '
              if (node.extra.scrollData && node.extra.scrollData.top)
                scrollDataText += 'top=' + node.extra.scrollData.top + ', '
              if (node.extra.scrollData && node.extra.scrollData.right)
                scrollDataText += 'right=' + node.extra.scrollData.right + ', '
              if (node.extra.scrollData && node.extra.scrollData.bottom)
                scrollDataText += 'bottom=' + node.extra.scrollData.bottom

              line += ' data-scrollable="' + scrollDataText + '"'
            }
          }

          if (text) {
            var trimmedText = text.trim()
            if (!attributesHtmlStr) {
              line += ' '
            }
            line += '>' + trimmedText
          } else if (!attributesHtmlStr) {
            line += ' '
          }

          line += ' />'
          result.push(line)
        }

        // Special treatment for semantic tags
        var emitSemantic = isSemantic && node.highlightIndex === undefined
        var mark = emitSemantic ? result.length : -1

        if (emitSemantic) {
          result.push(depthStr + '<' + node.tagName + '>')
          nextDepth += 1
        }

        for (var ci = 0; ci < node.children.length; ci++) {
          processNode(node.children[ci], nextDepth, result)
        }

        if (emitSemantic) {
          // empty tag should be removed
          if (result.length === mark + 1) {
            result.pop()
          } else {
            result.push(depthStr + '</' + node.tagName + '>')
          }
        }
      } else if (node.type === 'text') {
        // Add text only if it doesn't have a highlighted parent
        if (hasParentWithHighlightIndex(node)) {
          return
        }

        if (
          node.parent &&
          node.parent.type === 'element' &&
          node.parent.isVisible &&
          node.parent.isTopElement
        ) {
          result.push(depthStr + (node.text || ''))
        }
      }
    }

    var result = []
    processNode(rootNode, 0, result)
    return result.join('\n')
  }

  // ===========================================================================
  // getSelectorMap - get interactive element map: index -> DOM element reference
  // ===========================================================================

  function getSelectorMap(flatTree) {
    var selectorMap = new Map()

    var keys = Object.keys(flatTree.map)
    for (var i = 0; i < keys.length; i++) {
      var key = keys[i]
      var node = flatTree.map[key]
      if (node.isInteractive && typeof node.highlightIndex === 'number') {
        selectorMap.set(node.highlightIndex, node)
      }
    }

    return selectorMap
  }

  // ===========================================================================
  // cleanUpHighlights - clean up visual highlights
  // ===========================================================================

  function cleanUpHighlights() {
    var cleanupFunctions = window._highlightCleanupFunctions || []
    for (var i = 0; i < cleanupFunctions.length; i++) {
      if (typeof cleanupFunctions[i] === 'function') {
        cleanupFunctions[i]()
      }
    }
    window._highlightCleanupFunctions = []

    // Also remove the container
    var container = document.getElementById(HIGHLIGHT_CONTAINER_ID)
    if (container) container.remove()
  }

  // ===========================================================================
  // URL change listeners for automatic highlight cleanup
  // ===========================================================================

  window.addEventListener('popstate', function () {
    cleanUpHighlights()
  })
  window.addEventListener('hashchange', function () {
    cleanUpHighlights()
  })
  window.addEventListener('beforeunload', function () {
    cleanUpHighlights()
  })

  var navigation = window.navigation
  if (navigation && typeof navigation.addEventListener === 'function') {
    navigation.addEventListener('navigate', function () {
      cleanUpHighlights()
    })
  } else {
    var currentUrl = window.location.href
    setInterval(function () {
      if (window.location.href !== currentUrl) {
        currentUrl = window.location.href
        cleanUpHighlights()
      }
    }, 500)
  }

  // ===========================================================================
  // Expose global NiuDomTree API
  // ===========================================================================

  window.NiuDomTree = {
    /**
     * Build flattened DOM tree with indexed interactive elements
     * @param {Object} options - { doHighlightElements: true, viewportExpansion: -1, highlightOpacity: 0.0, highlightLabelOpacity: 0.1, focusHighlightIndex: -1, debugMode: false }
     * @returns {Object} { rootId, map } - FlatDomTree
     */
    buildFlatTree: buildFlatTree,

    /**
     * Serialize FlatDomTree to LLM-readable text with [index] prefixed elements
     * @param {Object} flatTree - buildFlatTree() result
     * @param {string[]} [includeAttributes=[]] - Additional attribute names to include
     * @param {boolean} [keepSemanticTags=false] - Whether to preserve semantic landmark tags
     * @returns {string} indexed interactive element list
     */
    flatTreeToString: flatTreeToString,

    /**
     * Get interactive element map: index -> DOM element reference
     * @param {Object} flatTree - buildFlatTree() result
     * @returns {Map<number, Object>} map of highlightIndex -> node data (with .ref property)
     */
    getSelectorMap: getSelectorMap,

    /**
     * Clean up visual highlights
     */
    cleanUpHighlights: cleanUpHighlights,
  }
})()
