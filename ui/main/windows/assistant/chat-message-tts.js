// Chat 消息级朗读按钮（spec: docs/superpowers/specs/2026-10-21-chat-message-tts-button.md）
// UMD：浏览器挂 window.ChatMessageTts；Node 下 module.exports 供测试。
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.ChatMessageTts = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  function extractText(messageEl) {
    if (!messageEl) return '';
    var textEl = messageEl.querySelector('.message-text') ||
                 messageEl.querySelector('.subagent-content');
    if (textEl) return String(textEl.innerText || textEl.textContent || '').trim();
    var clone = messageEl.cloneNode(true);
    var bubble = clone.querySelector('.timestamp-bubble');
    if (bubble) bubble.remove();
    return String(clone.innerText || clone.textContent || '').trim();
  }

  function createButton(doc, onSpeak) {
    var btn = doc.createElement('button');
    btn.type = 'button';
    btn.className = 'message-tts-btn';
    btn.title = '朗读这条消息';
    btn.setAttribute('aria-label', '朗读这条消息');
    // inline SVG: speaker + waves, 12x12, fill currentColor
    btn.innerHTML = '<svg viewBox="0 0 12 12" width="12" height="12" fill="currentColor" aria-hidden="true">' +
      '<path d="M1.5 4.5h2L6 2.2v7.6L3.5 7.5h-2z"/>' +
      '<path d="M7.7 4.3a2.5 2.5 0 0 1 0 3.4" fill="none" stroke="currentColor" stroke-width="1.1" stroke-linecap="round"/>' +
      '<path d="M9.3 2.9a4.6 4.6 0 0 1 0 6.2" fill="none" stroke="currentColor" stroke-width="1.1" stroke-linecap="round"/>' +
      '</svg>';
    btn.addEventListener('click', function (e) {
      e.preventDefault(); e.stopPropagation();
      try { onSpeak(); } catch (_) { /* no-op */ }
    });
    return btn;
  }

  return { extractText: extractText, createButton: createButton };
});
