/**
 * Niu Browser Extension - Simulator Mask + AI Cursor
 *
 * Provides visual feedback for browser automation:
 * - Thin animated border around page edges (gradient flowing along 4 edges)
 * - AI cursor with SVG mouse icon and easing movement
 * - Click ripple animation
 *
 * Ported from page-agent SimulatorMask.ts, replaces WebGL2 ai-motion
 * with pure CSS border animation for broader compatibility.
 */

;(function () {
  'use strict'

  class SimulatorMask {
    shown = false
    wrapper = null
    cursor = null

    #currentX = 0
    #currentY = 0
    #targetX = 0
    #targetY = 0
    #rafId = null
    #disposed = false

    constructor () {
      // Create wrapper
      this.wrapper = document.createElement('div')
      this.wrapper.className = 'niu-mask-wrapper'
      this.wrapper.setAttribute('data-browser-use-ignore', 'true')
      this.wrapper.setAttribute('data-page-agent-ignore', 'true')

      // Animated border around page edges (top/bottom via ::before/::after)
      const border = document.createElement('div')
      border.className = 'niu-mask-border'
      this.wrapper.appendChild(border)

      // Left/right border edges (via separate element's ::before/::after)
      const borderSides = document.createElement('div')
      borderSides.className = 'niu-mask-border-sides'
      this.wrapper.appendChild(borderSides)

      // Semi-transparent overlay
      const overlay = document.createElement('div')
      overlay.className = 'niu-mask-overlay'
      this.wrapper.appendChild(overlay)

      // Create AI cursor
      this.#createCursor()

      // Block user interaction while mask is visible
      this.wrapper.addEventListener('click', (e) => { e.stopPropagation(); e.preventDefault() })
      this.wrapper.addEventListener('mousedown', (e) => { e.stopPropagation(); e.preventDefault() })
      this.wrapper.addEventListener('mouseup', (e) => { e.stopPropagation(); e.preventDefault() })
      this.wrapper.addEventListener('mousemove', (e) => { e.stopPropagation(); e.preventDefault() })
      this.wrapper.addEventListener('wheel', (e) => { e.stopPropagation(); e.preventDefault() })
      this.wrapper.addEventListener('keydown', (e) => { e.stopPropagation(); e.preventDefault() })
      this.wrapper.addEventListener('keyup', (e) => { e.stopPropagation(); e.preventDefault() })

      // Add to page
      document.body.appendChild(this.wrapper)

      // Start cursor easing loop
      this.#moveCursorToTarget()
    }

    #createCursor () {
      this.cursor = document.createElement('div')
      this.cursor.className = 'niu-cursor'

      // Ripple container
      const ripple = document.createElement('div')
      ripple.className = 'niu-cursor-ripple'
      this.cursor.appendChild(ripple)

      // Filling layer (white interior)
      const filling = document.createElement('div')
      filling.className = 'niu-cursor-filling'
      this.cursor.appendChild(filling)

      // Border layer (gradient outline)
      const borderLayer = document.createElement('div')
      borderLayer.className = 'niu-cursor-border'
      this.cursor.appendChild(borderLayer)

      this.wrapper.appendChild(this.cursor)
    }

    /**
     * Easing loop: smoothly move cursor toward target position.
     * Uses the same 0.2 interpolation factor as page-agent.
     */
    #moveCursorToTarget () {
      if (this.#disposed) return
      const newX = this.#currentX + (this.#targetX - this.#currentX) * 0.2
      const newY = this.#currentY + (this.#targetY - this.#currentY) * 0.2

      const xDist = Math.abs(newX - this.#targetX)
      if (xDist > 0) {
        this.#currentX = xDist < 2 ? this.#targetX : newX
        this.cursor.style.left = this.#currentX + 'px'
      }

      const yDist = Math.abs(newY - this.#targetY)
      if (yDist > 0) {
        this.#currentY = yDist < 2 ? this.#targetY : newY
        this.cursor.style.top = this.#currentY + 'px'
      }

      this.#rafId = requestAnimationFrame(() => this.#moveCursorToTarget())
    }

    /**
     * Show the mask overlay with spinning border and cursor.
     */
    show () {
      if (this.shown || this.#disposed) return
      this.shown = true
      this.wrapper.classList.add('visible')

      // Initialize cursor at center of viewport
      this.#currentX = window.innerWidth / 2
      this.#currentY = window.innerHeight / 2
      this.#targetX = this.#currentX
      this.#targetY = this.#currentY
      this.cursor.style.left = this.#currentX + 'px'
      this.cursor.style.top = this.#currentY + 'px'
    }

    /**
     * Hide the mask overlay with fade-out.
     */
    hide () {
      if (!this.shown || this.#disposed) return
      this.shown = false
      this.cursor.classList.remove('clicking')
      // Delay removal to allow fade-out (matches original 800ms)
      setTimeout(() => {
        this.wrapper.classList.remove('visible')
      }, 800)
    }

    /**
     * Set the target position for the cursor (it will ease toward it).
     */
    setCursorPosition (x, y) {
      if (this.#disposed) return
      this.#targetX = x
      this.#targetY = y
    }

    /**
     * Trigger click ripple animation on the cursor.
     */
    triggerClickAnimation () {
      if (this.#disposed) return
      this.cursor.classList.remove('clicking')
      // Force reflow to restart animation
      void this.cursor.offsetHeight
      this.cursor.classList.add('clicking')
    }

    /**
     * Allow events to pass through the mask (for actual DOM interaction).
     */
    enablePassThrough () {
      this.wrapper.style.pointerEvents = 'none'
    }

    /**
     * Block events again (restore mask interception).
     */
    disablePassThrough () {
      this.wrapper.style.pointerEvents = 'auto'
    }

    /**
     * Clean up and remove from DOM.
     */
    dispose () {
      this.#disposed = true
      if (this.#rafId) cancelAnimationFrame(this.#rafId)
      this.wrapper.remove()
    }
  }

  // Expose globally
  window.NiuSimulatorMask = SimulatorMask
})()
