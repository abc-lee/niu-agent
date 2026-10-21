// 消息级朗读按钮模块测试：纯 Node，fake DOM（不引 jsdom）。Node 无 innerText，实现走 textContent 兜底。
const { test } = require('node:test');
const assert = require('node:assert/strict');
const M = require('../ui/main/windows/assistant/chat-message-tts.js');

test('extractText prefers .message-text', () => {
  const el = {
    querySelector(sel) {
      if (sel === '.message-text') return { textContent: ' 你好，世界 ' };
      return null;
    },
    cloneNode() { throw new Error('must not clone when .message-text found'); },
  };
  assert.equal(M.extractText(el), '你好，世界');
});

test('extractText falls back to .subagent-content', () => {
  const el = {
    querySelector(sel) {
      if (sel === '.message-text') return null;
      if (sel === '.subagent-content') return { textContent: '子 Agent 正文\n第二行' };
      return null;
    },
    cloneNode() { throw new Error('must not clone when .subagent-content found'); },
  };
  assert.equal(M.extractText(el), '子 Agent 正文\n第二行');
});

test('extractText fallback clone removes .timestamp-bubble before reading', () => {
  const clone = {
    textContent: '🔊3分钟前正文内容',
    querySelector(sel) { return sel === '.timestamp-bubble' ? bubble : null; },
  };
  const bubble = {
    removed: false,
    remove() { this.removed = true; clone.textContent = '正文内容'; }, // 模拟 remove 后剩余文本
  };
  const el = {
    querySelector() { return null; },
    cloneNode() { return clone; },
  };
  assert.equal(M.extractText(el), '正文内容');
  assert.ok(bubble.removed, 'timestamp-bubble must be removed before reading');
});

test('extractText returns empty for null/empty messages', () => {
  assert.equal(M.extractText(null), '');
  assert.equal(M.extractText(undefined), '');
  const empty = {
    querySelector() { return null; },
    cloneNode() { return { textContent: '   ', querySelector() { return null; } }; },
  };
  assert.equal(M.extractText(empty), '');
  const blankTextEl = {
    querySelector(sel) { return sel === '.message-text' ? { textContent: '' } : null; },
    cloneNode() { throw new Error('empty .message-text short-circuits to empty'); },
  };
  assert.equal(M.extractText(blankTextEl), '');
});

test('createButton wires click with stopPropagation and swallows handler errors', () => {
  const doc = {
    createElement() {
      return {
        setAttribute() {},
        addEventListener(name, fn) { this._click = fn; },
      };
    },
  };
  let spoke = 0;
  const btn = M.createButton(doc, () => { spoke++; });
  assert.equal(btn.className, 'message-tts-btn');
  assert.equal(btn.type, 'button');
  assert.equal(btn.title, '朗读这条消息');
  let stopped = false, defaulted = false;
  btn._click({ preventDefault() { defaulted = true; }, stopPropagation() { stopped = true; } });
  assert.ok(stopped && defaulted, 'click must preventDefault + stopPropagation');
  assert.equal(spoke, 1);
  const bad = M.createButton(doc, () => { throw new Error('boom'); });
  bad._click({ preventDefault() {}, stopPropagation() {} }); // 不抛出
});
