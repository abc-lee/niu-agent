// S5（AC-13 可编程半）：媒体横条「轮询永不停止」行为探针。
// 断言机制钉死（第四轮 P3：行为探针，不写文本级 grep 判据——grep clearInterval/stopMediaPolling
// 缺席恒假绿，别处新增一个有守卫的清除路径也测不出来）：把 chat.html 里 MEDIA_POLLING_JS_BEGIN/END
// 之间的横条 JS 块**逐字提取**进 node vm 上下文，桩 fetch（脚本指定的响应序列）/
// document.getElementById / window.electronAPI / setInterval+clearInterval（计数桩）——
// 脚本加载即执行末尾 startMediaPolling()，off/idle 响应处理后从同一 vm 上下文读回 mediaPollTimer，
// 断言其**非空**且 clearInterval 调用次数 = 0（off 分支只隐藏横条、轮询保持运行）。
const { test, describe, before } = require('node:test');
const assert = require('node:assert/strict');
const path = require('path');
const fs = require('fs');
const vm = require('node:vm');

const CHAT_HTML = path.join(__dirname, '..', 'ui', 'main', 'windows', 'assistant', 'chat.html');

function extractMediaPollingScript() {
  const html = fs.readFileSync(CHAT_HTML, 'utf-8');
  const beginMarker = '// ===== MEDIA_POLLING_JS_BEGIN =====';
  const endMarker = '// ===== MEDIA_POLLING_JS_END =====';
  const start = html.indexOf(beginMarker);
  const end = html.indexOf(endMarker);
  assert.notEqual(start, -1, 'chat.html 缺 MEDIA_POLLING_JS_BEGIN 标记（横条 JS 块被移走？）');
  assert.notEqual(end, -1, 'chat.html 缺 MEDIA_POLLING_JS_END 标记');
  assert.ok(end > start, 'MEDIA_POLLING_JS 标记顺序颠倒');
  return html.slice(start + beginMarker.length, end);
}

/**
 * 建一个 vm 上下文执行横条 JS 块，返回观测桩。
 * responses: fetch 按序返回的响应体序列（每次 fetch 弹出一个）。
 */
function runMediaBlock(responses) {
  const elements = new Map(); // id -> { classList, textContent }
  for (const id of ['media-progress-bar', 'media-progress-text', 'media-progress-fill']) {
    elements.set(id, {
      id,
      textContent: '',
      classList: {
        _set: new Set(),
        add(cls) { this._set.add(cls); },
        remove(cls) { this._set.delete(cls); },
        contains(cls) { return this._set.has(cls); },
      },
    });
  }
  const intervalIds = [];       // setInterval 返回的存活计时器 token（测试内永不真实触发）
  const clearCalls = [];        // clearInterval 收到的 token（期望恒为空 = 轮询永不停止）
  let intervalSeq = 0;
  const sseCallbacks = { started: null, completed: null };
  const fetchCalls = [];

  const context = {
    console: { log: () => {}, warn: () => {}, error: () => {}, debug: () => {} },
    document: { getElementById: (id) => elements.get(id) },
    window: {},
    fetch: async (url) => {
      fetchCalls.push(url);
      const body = responses.shift();
      assert.notEqual(body, undefined, 'fetch 被调用的次数多于提供的响应序列长度');
      return { json: async () => body };
    },
    // 桩计时器：返回真值 token（幂等守卫 if (mediaPollTimer) return 依赖它），测试内不真实调度
    setInterval: (fn, ms) => {
      const token = ++intervalSeq;
      intervalIds.push(token);
      return token;
    },
    clearInterval: (token) => { clearCalls.push(token); },
    setTimeout: () => 0,
    clearTimeout: () => {},
  };
  context.window.electronAPI = {
    onMediaIngestStarted: (cb) => { sseCallbacks.started = cb; },
    onMediaIngestCompleted: (cb) => { sseCallbacks.completed = cb; },
  };
  context.window.fetch = context.fetch; // 脚本里裸 fetch 走全局
  vm.createContext(context);
  vm.runInContext(extractMediaPollingScript(), context, { filename: 'media-progress-bar.js' });
  return { context, elements, clearCalls, intervalIds, sseCallbacks, fetchCalls };
}

/** 冲刷 vm 内 async pollMediaStatus 的微任务/setImmediate（外层与 vm 共享 node 事件循环）。 */
async function settle(context) {
  for (let i = 0; i < 5; i++) {
    await new Promise((r) => setImmediate(r));
  }
}

async function pollTimerAlive(context) {
  // let 声明不挂 context 对象，按脚本求值从同一 vm 上下文读回
  return vm.runInContext('mediaPollTimer', context);
}

describe('媒体横条轮询策略（行为探针）', () => {
  before(() => { extractMediaPollingScript(); }); // 标记缺失直接红

  test('off：横条隐藏，但轮询永不停止（mediaPollTimer 存活、clearInterval 零调用）', async () => {
    const rig = runMediaBlock([{ status: 'off', reason: '开关关', queue_depth: 0 }]);
    await settle(rig.context); // 初始 pollMediaStatus 完成（off 分支只 hideMediaBar）
    const bar = rig.elements.get('media-progress-bar');
    assert.ok(!bar.classList.contains('show'), 'off 时横条必须隐藏');
    assert.equal(rig.clearCalls.length, 0, 'off 分支不得清除任何轮询计时器');
    assert.ok(await pollTimerAlive(rig.context), 'off 后 mediaPollTimer 必须仍存活（轮询永不停止）');
    assert.equal(rig.fetchCalls.length, 1, '初始轮询恰好一次（测试内计时器不真实触发）');
  });

  test('idle：横条隐藏，轮询同样永不停止（调度器从未启动零事件形态靠它发现横条）', async () => {
    const rig = runMediaBlock([{ status: 'idle', reason: '', queue_depth: 0 }]);
    await settle(rig.context);
    assert.ok(!rig.elements.get('media-progress-bar').classList.contains('show'));
    assert.equal(rig.clearCalls.length, 0);
    assert.ok(await pollTimerAlive(rig.context));
  });

  test('encoding：横条显示在途条目与队列深度（异色横条的真实业务形态）', async () => {
    const rig = runMediaBlock([
      { status: 'encoding', entity: 'a/img-0001.jpg', queue_depth: 2 },
    ]);
    await settle(rig.context);
    const bar = rig.elements.get('media-progress-bar');
    assert.ok(bar.classList.contains('show'), 'encoding 时横条必须显示');
    assert.equal(rig.elements.get('media-progress-text').textContent, '媒体入库：a/img-0001.jpg（队列 2）');
    assert.equal(rig.clearCalls.length, 0);
  });

  test('SSE 事件是加速触发源不是生命周期管理者：事件后仍立即复查且轮询不重启不清除', async () => {
    const rig = runMediaBlock([
      { status: 'idle', reason: '', queue_depth: 0 },
      { status: 'stalled', reason: '实例未加载 mmproj', queue_depth: 1 }, // 事件触发的复查
      { status: 'idle', reason: '', queue_depth: 0 },
    ]);
    await settle(rig.context);
    assert.ok(rig.sseCallbacks.started, 'onMediaIngestStarted 订阅必须注册');
    assert.ok(rig.sseCallbacks.completed, 'onMediaIngestCompleted 订阅必须注册');
    rig.sseCallbacks.started(); // 模拟收到 media-ingest-started 事件
    await settle(rig.context);
    assert.equal(rig.fetchCalls.length, 2, '事件必须触发一次立即复查');
    assert.equal(rig.clearCalls.length, 0, '事件处理不得清除轮询');
    assert.equal(rig.intervalIds.length, 1, 'startMediaPolling 幂等：事件后不重复启动轮询');
    assert.equal(rig.elements.get('media-progress-text').textContent, '队列积压 1 行，未运行：实例未加载 mmproj');
    rig.sseCallbacks.completed({ entity: 'a/img-0001.jpg', ok: true });
    await settle(rig.context);
    assert.equal(rig.fetchCalls.length, 3, '完成事件同样触发一次复查');
    assert.equal(rig.clearCalls.length, 0);
    assert.ok(await pollTimerAlive(rig.context), '完成事件后轮询依然存活');
  });
});
