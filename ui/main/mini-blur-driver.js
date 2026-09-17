// mini-blur-driver.js —— 磨砂模糊驱动层（T2，②层：跨平台共用，零平台分支）。
// 职责边界：只做「观测层上报 → 后端调用」的状态收敛（去重 / 换装合并 / openSince 续跑 /
// 失败降级 / blur.enabled 总开关）+ 反向通道生产端。不做 pending 判断（扣留在观测层），
// 不读既有状态机内部变量（窗口自己经 BrowserWindow.getAllWindows 按 chat 页 URL 实时解析，
// 这是 main.js 只加一行 require 能成立的前提）。
// 红线（方案 §0）：本文件与两个后端文件全部为新增；平台差异只收口在下方后端选择表；
// 所有异常 try/catch 包住，每类错误只记一条日志，绝不抛到主进程。
'use strict';

const { app, BrowserWindow, ipcMain } = require('electron');
const path = require('path');
const fs = require('fs');
const os = require('os');

const warned = new Set();
function warnOnce(key, msg) {
  if (warned.has(key)) return;
  warned.add(key);
  try { console.error('[mini-blur-driver]', key, msg); } catch (e) { /* 无控制台时忽略 */ }
}

// ── 配置：驱动自读 ~/.niu/window-config.json 顶层 blur 段（新读取路径，不复用既有 getChatMiniConfig）
// enabled 默认 true（false = 完全回到现状观感）；hoverOpacity 默认 0.08（用户真机拍板的 8% 白纱）。改后需重启。
let blurEnabled = true;
let blurHoverOpacity = 0.08;
try {
  const cfg = JSON.parse(fs.readFileSync(path.join(os.homedir(), '.niu', 'window-config.json'), 'utf8'));
  const blur = cfg && typeof cfg === 'object' ? (cfg.blur || {}) : {};
  if (blur.enabled !== undefined) blurEnabled = !!blur.enabled;
  if (typeof blur.hoverOpacity === 'number') blurHoverOpacity = blur.hoverOpacity;
} catch (e) { /* 文件缺失/损坏 → 全默认 */ }

// ── 启动探针（§5.2 迁移）：require + isAvailable 双段已下移到后端——darwin 后端 isAvailable()
// = 原探针两段（require .node + m.isAvailable()===true）逐条等价；win32 后端 isAvailable() =
// 同步预检（build ≥ 22621 + WinFx.exe 存在 + App Runtime 2.x 可见性）。
// 门禁一：blurEnabled=false → 不加载后端（零进程零副作用）；门禁二：探针不过 → 后端置空（全 no-op，
// 反向通道除外，仍下发 {available:false}）。probeOk 消费点（反向通道 payload、IPC 守卫）不变。
let probeOk = false;

// ── 后端选择表：唯一平台分叉点（darwin = NSVisualEffectView 原生件；win32 = WinFx.exe 材质窗，真实现）
const BACKENDS = {
  darwin: () => require('./native/mini-blur-backend-darwin.js'),
  win32: () => require('./native/mini-blur-backend-win32.js'),
};
let backend = null;
if (blurEnabled) {
  const load = BACKENDS[process.platform];
  if (load) {
    try { backend = load(); } catch (e) { warnOnce('backend', `后端加载失败：${e && e.message}`); }
  } else {
    warnOnce('backend', `平台 ${process.platform} 无后端，全 no-op`);
  }
  if (backend && typeof backend.isAvailable === 'function') {
    try { probeOk = backend.isAvailable() === true; } catch (e) { probeOk = false; }
    if (!probeOk) backend = null;         // 探针不过 → 后端置空（全 no-op）
  }
}
if (blurEnabled && !probeOk) {
  warnOnce('probe', `原生探针失败（后端 isAvailable 未通过，平台 ${process.platform}）→ 全 no-op（反向通道除外）`);
}

// ── 窗口解析：实时取当前 chat 窗（file:// …/windows/assistant/chat.html），不缓存引用
function resolveChatWindow() {
  try {
    const wins = BrowserWindow.getAllWindows();
    for (const w of wins) {
      if (w.isDestroyed()) continue;
      let u = '';
      try { u = w.getURL(); } catch (e) { /* 加载中取 URL 可能抛 */ }
      if (u.includes('chat.html')) return w;
    }
  } catch (e) { /* 全 no-op */ }
  return null;
}
if (backend) {
  try { backend.attach(resolveChatWindow); } catch (e) { warnOnce('attach', e && e.message); }
}

// ── 幕墙曲线：cubic-bezier(.22,.61,.36,1)（与 chat.html #mini-panel clip-path transition 同款），
// 入参 x∈[0,1]（时间进度），返回 y（效果进度）。牛顿迭代 + 区间钳位，确定性无随机。
function cubicBezier(p1x, p1y, p2x, p2y) {
  const cx = 3 * p1x, bx = 3 * (p2x - p1x) - cx, ax = 1 - cx - bx;
  const cy = 3 * p1y, by = 3 * (p2y - p1y) - cy, ay = 1 - cy - by;
  const sx = t => ((ax * t + bx) * t + cx) * t;
  const sy = t => ((ay * t + by) * t + cy) * t;
  const dx = t => (3 * ax * t + 2 * bx) * t + cx;
  return function (x) {
    if (x <= 0) return 0;
    if (x >= 1) return 1;
    let t = x;
    for (let i = 0; i < 8; i++) {
      const e = sx(t) - x;
      if (Math.abs(e) < 1e-5) break;
      const d = dx(t);
      if (Math.abs(d) < 1e-6) break;
      t -= e / d;
    }
    t = Math.min(1, Math.max(0, t));
    return sy(t);
  };
}
const EASE = cubicBezier(0.22, 0.61, 0.36, 1);
const FADE_MS = 200;  // 点亮/熄灭淡入淡出时长（与 clip-path .2s 同步）

// ── 反向通道（生产端）：向当前 chat 窗下发 {available, enabled, hoverOpacity}——观测层启用态唯一来源；
// 探针失败/enabled=false → 下发 {available:false}（观测层收不到启用信号 = 全程零副作用）
function sendBlurConfig() {
  try {
    const win = resolveChatWindow();
    if (!win || win.isDestroyed()) return;
    const payload = (probeOk && blurEnabled)
      ? { available: true, enabled: true, hoverOpacity: blurHoverOpacity }
      : { available: false };
    win.webContents.send('chat-mini-blur-config', payload);
  } catch (e) { warnOnce('cfgsend', e && e.message); }
}

// ── 驱动状态 + 清除（exit/自愈重载时调）：可见性瞬时关 + 摘除材质 + 重置状态
const state = { open: false, regionsJson: null, disposeTimer: null };
function clearBlur() {
  if (state.disposeTimer) { clearTimeout(state.disposeTimer); state.disposeTimer = null; }
  state.open = false;
  state.regionsJson = null;
  if (backend) {
    try { backend.setVisible(false, 0); backend.dispose(); } catch (e) { warnOnce('clear', e && e.message); }
  }
}

// ── IPC 接收：观测层上报 {open, regions:[{x,y,w,h,r}], viewW, viewH, openSince, seq}
ipcMain.on('chat-mini-blur-state', (e, payload) => {
  try {
    if (!backend || !probeOk || !blurEnabled) return;   // 总回退/后端缺失：驱动层全 no-op
    if (!payload || typeof payload.open !== 'boolean') return;
    if (payload.open) {
      const regions = Array.isArray(payload.regions) ? payload.regions : [];
      const rjson = JSON.stringify(regions);
      const radius = Number.isFinite(payload.radius) ? payload.radius : undefined;
      if (!state.open) {
        // 假翻真：setRegions + setVisible(true,200)；openSince 续跑——open 已过半程才接入
        // （自愈重载/晚启动）时把 fade 时长按剩余时间缩短，并把 alpha 钉到曲线当前进度
        // P1 修复：先摘掉待执行的 dispose 定时器，否则快速翻转（关→200ms 内又开）会
        // 让晚到的 dispose 摘掉材质，且此后上报只走 setRegions 路径 → 模糊静默消失
        if (state.disposeTimer) { clearTimeout(state.disposeTimer); state.disposeTimer = null; }
        state.open = true;
        state.regionsJson = rjson;
        backend.setRegions(regions, radius);
        const age = (typeof payload.openSince === 'number')
          ? Date.now() - payload.openSince
          : FADE_MS;
        if (age < FADE_MS) {
          // T2 微修：必须先 setProgress 定位 alpha，再 setVisible 启动剩余时长淡入——
          // 反序时淡入动画已起，setAlpha 直写会把动画 model/presentation 一并钉在残值
          // （实测 t+600ms 仍为 0），模糊浓度卡在曲线残值不归满
          backend.setProgress(EASE(Math.max(0, age) / FADE_MS));
          backend.setVisible(true, FADE_MS - Math.max(0, age));
        } else {
          // P2 修复：openSince 已过 fade 全程（迟到上报）→ 直达终态，零时长点亮；
          // 不得再起 FADE_MS 淡入（darwin 后端 install 时 alphaValue=0，会重新走 200ms）
          backend.setVisible(true, 0);
        }
      } else if (rjson !== state.regionsJson) {
        // 同 open 仅 regions 变（跟随帧/新消息变高）：即时换装，不动可见性
        state.regionsJson = rjson;
        backend.setRegions(regions, radius);
      }
    } else if (state.open) {
      // 真翻假：setVisible(false,200)，动画结束后 dispose
      state.open = false;
      state.regionsJson = null;
      if (state.disposeTimer) { clearTimeout(state.disposeTimer); state.disposeTimer = null; }
      backend.setVisible(false, FADE_MS);
      state.disposeTimer = setTimeout(() => {
        state.disposeTimer = null;
        if (backend) { try { backend.dispose(); } catch (e2) { warnOnce('dispose', e2 && e2.message); } }
      }, FADE_MS);
    }
  } catch (e) { warnOnce('ipcstate', e && e.message); }
});

// ── 既有通道旁路监听（新增监听，不改原 handler）：chat-mini-exit → 强制摘除
ipcMain.on('chat-mini-exit', () => {
  try { clearBlur(); } catch (e) { warnOnce('ipcexit', e && e.message); }
});

// ── 反向通道全生命周期：逐 webContents 实例挂 did-finish-load（覆盖三态：首次加载 /
// 自愈重载 / 关窗后托盘重开的新实例），清旧模糊 + 重发启用态
app.on('web-contents-created', (e, wc) => {
  try {
    wc.on('did-finish-load', () => {
      try {
        let u = '';
        try { u = wc.getURL(); } catch (err) { /* 忽略 */ }
        if (!u.includes('chat.html')) return;
        clearBlur();      // 自愈重载/重开：清残留材质（与重发同一时机）
        sendBlurConfig();
      } catch (err) { warnOnce('didfinish', err && err.message); }
    });
  } catch (e) { warnOnce('wccreated', e && e.message); }
});

// 启动时若 chat 窗已存在（极少见）也下发一次；正常路径由 did-finish-load 覆盖
sendBlurConfig();
