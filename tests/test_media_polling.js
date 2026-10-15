// S5（AC-13 可编程半）：媒体横条「轮询永不停止」行为探针。
//（v3.5：真进度 + 页面删文件责任，D-27 + 口径②③④⑤/丁——新增「全部完成 ⇒ dismiss + 关条、轮询仍存活」「dismiss 被拒 ⇒ 横条不关」断言；
// v3.6：非对称样本 done/total=25% 杀死比率取向变异 + 「未完成（done<total）⇒ 绝不 dismiss」负向断言 +「-1 哨兵 ⇒ 渲染未知、绝不误报行数」覆盖）
// 断言机制钉死（第四轮 P3：行为探针，不写文本级 grep 判据——grep clearInterval/stopMediaPolling
// 缺席恒假绿，别处新增一个有守卫的清除路径也测不出来）：把 chat.html 里 MEDIA_POLLING_JS_BEGIN/END
// 之间的横条 JS 块**逐字提取**进 node vm 上下文，桩 fetch（按 URL 分派：status 端点按序弹响应 /
// dismiss 端点返回指定结果）/ document.getElementById / window.electronAPI / setInterval+clearInterval（计数桩）——
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
 * responses: status 端点 fetch 按序返回的响应体序列（每次 status fetch 弹出一个）。
 * dismissResult: dismiss 端点 fetch 的固定响应体（默认 { dismissed: false }）。
 */
function runMediaBlock(responses, dismissResult = { dismissed: false }) {
  const elements = new Map(); // id -> { classList, textContent, style }
  for (const id of ['media-progress-bar', 'media-progress-text', 'media-progress-fill', 'media-progress-percent']) {
    elements.set(id, {
      id,
      textContent: '',
      style: {},
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
      let body;
      if (String(url).endsWith('/api/media/queue/dismiss')) {
        body = dismissResult; // dismiss 端点：固定返回指定结果
      } else {
        body = responses.shift();
        assert.notEqual(body, undefined, 'status 端点被调用的次数多于提供的响应序列长度');
      }
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

  test('encoding：真进度渲染（非对称样本 done/total=25%，杀死比率取向变异）', async () => {
    const rig = runMediaBlock([
      { status: 'encoding', entity: 'a/img-0001.jpg', queue_depth: 3, queue_total: 4, queue_done: 1 },
    ]);
    await settle(rig.context);
    const bar = rig.elements.get('media-progress-bar');
    assert.ok(bar.classList.contains('show'), 'encoding 时横条必须显示');
    assert.equal(rig.elements.get('media-progress-text').textContent, '媒体入库：a/img-0001.jpg');
    assert.equal(rig.elements.get('media-progress-fill').style.width, '25%', 'fill 宽度 = queue_done/queue_total = 25%');
    assert.equal(rig.elements.get('media-progress-percent').textContent, '25%', 'percent 元素 = 同一百分比文本');
    assert.equal(rig.fetchCalls.filter((u) => u.endsWith('/api/media/queue/dismiss')).length, 0, '未完成（done < total）时不得调用 dismiss');
    assert.equal(rig.clearCalls.length, 0);
  });

  test('全部完成且 dismiss 成功：横条关闭、dismiss 恰 1 次、轮询仍存活（页面删文件责任，口径③/丁）', async () => {
    const rig = runMediaBlock(
      [{ status: 'idle', reason: '', queue_total: 3, queue_done: 3, queue_depth: 0 }],
      { dismissed: true },
    );
    await settle(rig.context);
    assert.ok(!rig.elements.get('media-progress-bar').classList.contains('show'), 'dismiss 成功后横条必须关闭');
    assert.equal(
      rig.fetchCalls.filter((u) => u.endsWith('/api/media/queue/dismiss')).length,
      1,
      '全部完成后 dismiss 端点恰好调用 1 次',
    );
    assert.equal(rig.clearCalls.length, 0, 'dismiss 分支不得清除任何轮询计时器');
    assert.ok(await pollTimerAlive(rig.context), 'dismiss 关条后 mediaPollTimer 仍存活（轮询永不停止）');
  });

  test('全部完成但 dismiss 被拒：横条保持显示完成态文案、轮询仍存活（竞态：观察后新追加行，下一轮重试）', async () => {
    const rig = runMediaBlock(
      [{ status: 'idle', reason: '', queue_total: 2, queue_done: 2, queue_depth: 0 }],
      { dismissed: false },
    );
    await settle(rig.context);
    const bar = rig.elements.get('media-progress-bar');
    assert.ok(bar.classList.contains('show'), 'dismiss 被拒时横条不得关闭');
    // 不逐字钉文案（9d23299c 后队列文件语义为「清空、文件保留」，文案措辞可随时微调）：
    // 只断言稳定行为面——完成态文案非空且同时含「完成」「队列」。
    const doneText = rig.elements.get('media-progress-text').textContent;
    assert.ok(doneText.length > 0, '完成态文案非空');
    assert.ok(doneText.includes('完成') && doneText.includes('队列'), '完成态文案须同时表明队列与完成');
    assert.equal(rig.elements.get('media-progress-percent').textContent, '100%');
    assert.equal(rig.clearCalls.length, 0, 'dismiss 被拒不得清除任何轮询计时器');
    assert.ok(await pollTimerAlive(rig.context), 'dismiss 被拒后 mediaPollTimer 仍存活（下一轮轮询重试）');
  });

  test('未完成（done < total）时绝不 POST dismiss（I-3 负向面：只有全部完成才 dismiss）', async () => {
    const rig = runMediaBlock([
      { status: 'stalled', reason: '实例未加载 mmproj', queue_depth: 2, queue_total: 4, queue_done: 1 },
    ]);
    await settle(rig.context);
    assert.ok(rig.elements.get('media-progress-bar').classList.contains('show'), 'stalled 有积压时横条显示');
    assert.equal(rig.elements.get('media-progress-text').textContent, '队列积压 2 行，未运行：实例未加载 mmproj');
    assert.equal(
      rig.fetchCalls.filter((u) => u.endsWith('/api/media/queue/dismiss')).length,
      0,
      '未完成（done < total）时不得调用 dismiss',
    );
    assert.equal(rig.clearCalls.length, 0);
    assert.ok(await pollTimerAlive(rig.context), '轮询仍存活');
  });

  test('queue_depth=-1 哨兵（三计数全 -1）：渲染「未知」，绝不误报「队列积压 -1 行」', async () => {
    const rig = runMediaBlock([
      { status: 'scheduler-stopped', reason: '队列文件读不出（非 UTF-8 / 权限异常）', queue_depth: -1, queue_total: -1, queue_done: -1 },
    ]);
    await settle(rig.context);
    assert.equal(rig.elements.get('media-progress-text').textContent, '队列积压未知，未运行：队列文件读不出（非 UTF-8 / 权限异常）');
    assert.equal(rig.elements.get('media-progress-fill').style.width, '0%');
    assert.ok(!rig.elements.get('media-progress-text').textContent.includes('-1'), '哨兵分支不得把 -1 渲染进文案');
    assert.equal(rig.fetchCalls.filter((u) => u.endsWith('/api/media/queue/dismiss')).length, 0, '哨兵（未完成态）不得调用 dismiss');
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
