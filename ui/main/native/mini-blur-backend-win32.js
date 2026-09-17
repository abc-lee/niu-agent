// mini-blur-backend-win32.js —— 磨砂模糊后端（win32）：真实现（路线 A：零内容 WinUI 3 材质窗 + 区域裁剪，独立进程）。
// 宿主 = T1 产物 ui/main/native/winfx/WinFx.exe：材质铺满窗 + SetWindowRgn 区域裁剪 + 贴宿主窗正下方；
// spawn（cwd=产物目录、进程常驻、stdin/stdout NDJSON 命令通道：attach/geom/rects/show/hide/quit）。
// 预热（§5.2）：load 时（驱动探针通过后 attach 触发）spawn → 握手协议无 'hello' 命令，以 'attach'
// （off-screen 哨兵 hwnd=0）兼做握手 → rects[]（区域归零）→ hide（回驻留隐藏态）= 就绪；
// 超时 5s 未就绪 → 本会话全 no-op + 一条日志（不重试）。WinFx.exe 不存在 = no-op，绝不抛异常/崩溃。
// 就绪前队列 = 最新一次完整快照 {visible,fadeMs,progress,regions}（深度 1），
// 就绪后按 regions → progress → visible 顺序重放（先 setProgress 定位、再 setVisible 启动画，顺序不得反）。
// 几何（§5.5）：每次 move/resize/DPI 变化现取 resolveWin()+getBounds()（禁止缓存），
// DIP × 窗 scaleFactor 换算在 WinFx 进程内完成（attach 时下发 scaleFactor）；拖动期合帧（每帧至多一次）。
// 降级（§5.7）：握手失败/超时/进程中途退出 → 本会话全 no-op + 一条日志，不重启；
// dispose = 区域归零 + hide（宿主常驻不销毁）；app will-quit → quit + 回收进程。
'use strict';

const { app, screen } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const os = require('os');

const WINFX_DIR = path.join(__dirname, 'winfx');
const WINFX_EXE = path.join(WINFX_DIR, 'WinFx.exe');
const HANDSHAKE_MS = 5000;   // 预热握手超时（§5.2：超时 = 本会话全 no-op + 一条日志）
const GEOM_COALESCE_MS = 16; // 拖动期合帧：每帧至多一次 geom（§5.5）

const warned = new Set();
function warnOnce(key, msg) {
  if (warned.has(key)) return;
  warned.add(key);
  try { console.error('[mini-blur-win32]', key, msg); } catch (e) { /* 无控制台时忽略 */ }
}

// ── isAvailable() 预检（同步、不 spawn）：
// ① Windows build ≥ 22621（Win11 22H2，DesktopAcrylicController 官方下限）
// ② WinFx.exe 产物存在（缺失 = no-op，绝不 spawn 炸掉）
// ③ 自包含产物：WinFx.exe 自带 .NET 8 + Windows App SDK，无需机器级 App Runtime；
//    真实可用性由 5s 预热握手裁决（WinFx 起不来 = 超时 = 本会话全 no-op，行为一致）。
function winBuild() {
  const m = String(os.release()).match(/(\d{3,})/);
  return m ? Number(m[1]) : 0;
}

function isAvailable() {
  if (process.platform !== 'win32') return false;
  if (winBuild() < 22621) return false;
  try { if (!fs.existsSync(WINFX_EXE)) return false; } catch (e) { return false; }
  return true;
}

// ── 状态
let resolveWin = () => null;   // 驱动层 attach 注入「实时解析当前 chat 窗」
let proc = null;               // WinFx.exe 常驻进程
let ready = false;             // 预热握手完成
let dead = false;              // 握手失败/超时/进程退出 → 本会话全 no-op（不重启，§5.7）
let attached = false;          // 已发真实 attach（hwnd + 几何 + scaleFactor）
let scaleSent = 0;             // 真实 attach 所用 scaleFactor（DPI 变化 → 重 attach）
let subWin = null;             // 已订阅的窗口实例（订阅随实例走，§5.3）
let closedFn = null;           // 绑定在 subWin 上的 'closed' 监听闭包（Electron 的 'closed' 事件零参数，
                                // 实例只能靠闭包捕获）；绑定/退订/重订阅复用同一引用，防 removeListener 拿不到
let handshakeTimer = null;
let hideTimer = null;
let geomTimer = null;
let geomPending = false;
let preheatStage = 0;          // 0=attach已发 1=rects已发 2=hide已发 → 就绪
let attachReplyPending = false; // 真实 attach 已发、回包未裁决（fail-loud：仅 'attach'/'attach-fail:'/'error:' 裁决，见 onStdout）
let outBuf = '';
let stderrTail = '';

// 就绪前队列 = 最新一次完整快照（深度 1，覆盖驱动三轴 + dispose）
function newSnap(disposed) {
  return { visible: false, fadeMs: 0, progress: 0, regions: null, radius: undefined, disposed: !!disposed };
}
let snap = newSnap(false);

// ── 命令通道
function cmd(obj) {
  if (!proc || !proc.stdin || !proc.stdin.writable) return;
  try { proc.stdin.write(JSON.stringify(obj) + '\n'); } catch (e) { warnOnce('stdin', e && e.message); }
}

// ── 窗口解析 + 实例订阅（不缓存窗口引用；实例变了重新订阅并重 attach，§5.3）
function onGeo() { scheduleGeom(); }

// 实例销毁（Alt+F4/点关闭；渲染端不发 chat-mini-exit）→ 与 dispose 同一套效果：
// 区域归零 + hide + attached=false，材质窗不留在旧位置/可见态。
function onClosed(win) {
  try {
    if (subWin !== win) return;   // 旧实例迟到的 closed：已被新实例接管，忽略
    subWin = null;
    attached = false;
    // 窗已销毁：复位在途标记（在途回包可能尚未被裁决），使旧 attach 的陈旧回包通常不会算到
    // 后续新 attach 头上：若它在下次 attach 之前被读到，落非在途的独立键 'attachfail-late'
    // （onStdout），槽位由 late 分支独立记账、互不消耗。边界（不改行为）：若陈旧回包拖到
    // 下次 attach 已下发（pending 重新为 true）之后才被读到，陈旧 'attach-fail:' 仍走「在途」分支、
    // 烧掉 'attachfail' 槽位，真实在途失败则落 'attachfail-late'——两键语义互换（不静默，但归属误导）。
    attachReplyPending = false;
    if (!ready || dead) return;
    if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
    cmd({ cmd: 'rects', rects: [], viewW: 0, viewH: 0 });
    cmd({ cmd: 'hide' });
  } catch (e) { warnOnce('closed', e && e.message); }
}
function liveWin() {
  let win = null;
  try { win = resolveWin(); } catch (e) { return null; }
  if (!win) return null;
  try { if (win.isDestroyed()) return null; } catch (e) { return null; }
  if (win !== subWin) {
    try {
      if (subWin) {
        try {
          if (!subWin.isDestroyed()) {
            subWin.removeListener('move', onGeo);
            subWin.removeListener('resize', onGeo);
            subWin.removeListener('show', onGeo);
            subWin.removeListener('hide', onGeo);
            subWin.removeListener('minimize', onGeo);
            if (closedFn) subWin.removeListener('closed', closedFn);   // 退订用绑定时的同一引用
          }
        } catch (e) { /* 旧实例已销毁：监听随其失效 */ }
      }
    } catch (e) { /* ignore */ }
    subWin = win;
    attached = false;   // 新实例 → hwnd 变化，必须重 attach
    try {
      // 订阅随实例走（§5.3）：move/resize/show/hide/minimize → 几何现取重同步；closed → dispose 同效
      win.on('move', onGeo);
      win.on('resize', onGeo);
      win.on('show', onGeo);
      win.on('hide', onGeo);
      win.on('minimize', onGeo);
      // 'closed' 事件零参数（Electron API），闭包捕获实例后存入 closedFn；换实例时闭包随之重建
      closedFn = () => onClosed(win);
      win.on('closed', closedFn);
    } catch (e) { warnOnce('sub', e && e.message); }
  }
  return win;
}

// ── 几何现取 + 合帧（§5.5：禁缓存几何）
// 窗所在屏缩放 = screen.getDisplayMatching(窗 bounds).scaleFactor（本 Electron 版本 BrowserWindow
// 无 getScaleFactor；getDisplayMatching 恒可用，跨屏拖动/DPI 变化自动跟随）
function winScale(win) {
  try { return screen.getDisplayMatching(win.getBounds()).scaleFactor; } catch (e) { return 1; }
}
function scheduleGeom() {
  if (geomPending) return;
  geomPending = true;
  try { geomTimer = setTimeout(flushGeom, GEOM_COALESCE_MS); } catch (e) { geomPending = false; geomTimer = null; }
}
function flushGeom() {
  geomTimer = null;
  geomPending = false;
  if (dead || !ready) return;
  try {
    const win = liveWin();
    if (!win) return;
    pushGeom(win);
  } catch (e) { warnOnce('geom', e && e.message); }
}
function attachNow(win) {
  const b = win.getBounds();
  // getNativeWindowHandle() 返回 Buffer：Number(buffer) 会走 toString()（UTF-8）→ NaN →
  // JSON.stringify 写成 null，原生 DoAttach 只接受 JSON 数字/十进制字符串（long.Parse），
  // 遇 null 抛 InvalidOperationException → _attachHwnd 恒为 Zero（实测：材质窗压到迷你窗之上）。
  // 必须 readBigUInt64LE 转十进制字符串下发。
  cmd({
    cmd: 'attach',
    hwnd: win.getNativeWindowHandle().readBigUInt64LE(0).toString(),
    rect: { x: b.x, y: b.y, w: b.width, h: b.height },
    scaleFactor: winScale(win),
  });
  attached = true;
  scaleSent = winScale(win);
  attachReplyPending = true;   // 真实 attach 的回包 fail-loud 检查（onStdout）
}
function pushGeom(win) {
  const scale = winScale(win);
  if (!attached || scale !== scaleSent) { attachNow(win); return; }
  const b = win.getBounds();
  cmd({ cmd: 'geom', x: b.x, y: b.y, w: b.width, h: b.height });
}
function ensureAttached(win) {
  if (win && (!attached || winScale(win) !== scaleSent)) attachNow(win);
}

// ── 预热：spawn → attach(off-screen 哨兵) → rects[] → hide = 就绪
function startPrewarm() {
  if (proc || dead) return;
  try {
    if (!fs.existsSync(WINFX_EXE)) {
      dead = true;
      warnOnce('preheat', `WinFx.exe 缺失（${WINFX_EXE}）→ 本会话全 no-op`);
      return;
    }
    proc = spawn(WINFX_EXE, [], {
      cwd: WINFX_DIR,
      windowsHide: true,
      stdio: ['pipe', 'pipe', 'pipe'],
    });
  } catch (e) {
    proc = null;
    dead = true;
    warnOnce('preheat', `材质窗 spawn 失败：${e && e.message} → 本会话全 no-op`);
    return;
  }
  try {
    proc.stdout.on('data', (d) => onStdout(String(d)));
    proc.stderr.on('data', (d) => { stderrTail = (stderrTail + d).slice(-400); });
  } catch (e) { /* ignore */ }
  proc.on('error', (e) => onGone(`进程异常：${e && e.message}`));
  proc.on('exit', () => onGone('进程退出'));
  handshakeTimer = setTimeout(() => {
    handshakeTimer = null;
    const tail = stderrTail.trim().split('\n').pop();
    onGone(`预热握手超时（${HANDSHAKE_MS}ms）` + (tail ? `（stderr 末行：${tail}）` : ''));
  }, HANDSHAKE_MS);
  // 握手 = off-screen attach（hwnd=0 哨兵：hWndInsertAfter=NULL 即 HWND_TOP((HWND)0)，是合法值
  // → SetWindowPos 必成功；真实效果 = 把材质窗抬到 topmost 带顶；宿主窗存在时由 onReady 的真实 attach
  // （after=宿主窗）将其贴回宿主窗正下方；窗尚不存在时延后到驱动下一帧的点亮路径
  // （点亮帧到达时窗必已存在，由常规 setRegions/show 路径接管）；停靠位在窗外（-3000,-3000）故零像素）；其后归零区域 + 回隐藏态
  cmd({ cmd: 'attach', hwnd: 0, rect: { x: -3000, y: -3000, w: 488, h: 216 } });
}

function onStdout(data) {
  outBuf += data;
  let i;
  while ((i = outBuf.indexOf('\n')) >= 0) {
    const line = outBuf.slice(0, i);
    outBuf = outBuf.slice(i + 1);
    const t = line.trim();
    if (!t) continue;
    try {
      const o = JSON.parse(t);
      if (!o || typeof o.msg !== 'string') continue;
      if (o.ok && !ready && !dead) {
        onPreheatMsg(o.msg);
      } else if (ready) {
        // 就绪后 fail-loud（覆盖两条轴）：
        // ① attach 的 z 序轴：真实 attach 回包必须是 'attach'（原生 DoAttach 的 z 序失败编码为
        //    'attach-fail:<err>'，无独立 error 行）→ warnOnce('attachfail')。
        //    在途分支先校验行身份：先前命令（geom/rects/show/hide）的回包可能在同一 stdout 管道里
        //    晚到，不校验会被当成 attach 回包 → 误报失败并烧掉唯一的 'attachfail' 槽位；
        //    attach 回包只可能是 'attach' / 'attach-fail:' / 'error:' 三种形态（判定见下方分支）。
        //    两键分工：'attachfail' = 真实在途 attach 的 z 序/几何失败（一次性）；
        //    'attachfail-late' = 非在途时期到达的 'attach-fail:'/'error:'（陈旧/迟到/其它命令回包，
        //    一次性但独立，不消耗在途 'attachfail' 槽位——否则一次良性陈旧失败吃掉唯一告警后，
        //    后续真实 z 序失败全静默）。
        // ② rects 的区域构建轴：'create-rgn-fail' / 'combine-fail' → 独立键 warnOnce('rectsfail')
        //    （不与 ① 共用一次性槽位）。'SetWindowRgn=0' 不判：未点亮稳态时材质窗处于隐藏（hide 后），
        //    原生 SetWindowRgn 恒返回 0 且区域更新丢失（由 show 时 DoShow 重放缓存区域补偿）= 预期行为；
        //    而驱动点亮顺序为「先 setRegions 后 setVisible(true)」，未点亮稳态（已 attach 且处于 hide 之后）时
        //    点亮后首个 rects 回包命中该串 → 误报（若该次点亮前刚发生重 attach（新实例/onClosed/DPI 变化），
        //    DoAttach 的 ShowWindow(SW_SHOWNA) 已使其可见，紧随 rects 包回包为 SetWindowRgn=1、不含该串）。
        //    可见（点亮）态同样不判：若区域更新真丢失，DoShow 只在 show 时刻重放补偿，点亮态失败仍静默
        //    （有意收窄的代价；触发需 GDI 耗尽/句柄失效，罕见）。
        // msg 非字符串的行在上游 continue 丢弃、不会被记；本分支只判 'error:'/'attach-fail:' 两个前缀并记一条（late 键），绝不再静默。
        // 注：非在途回包到达时在途标记可能已清（回包已被裁决，或被 onClosed 复位），本条属重复/陈旧失败，记独立键便于定位。
        if (attachReplyPending) {
          // 只把 'attach' / 'attach-fail:' / 'error:' 三种形态当作 attach 回包（裁决：清在途标记、
          // 非 'attach' 即告警）；其余形态（先前命令 geom/rects/show/hide 的晚到回包等）不清标记、
          // 不告警、直接放过——保持 in-flight 等真正的 attach 回包（理由见上方 ① 说明）。
          if (o.msg === 'attach' || o.msg.startsWith('attach-fail:') || o.msg.startsWith('error:')) {
            attachReplyPending = false;
            if (o.msg !== 'attach') {
              warnOnce('attachfail', `真实 attach 回包异常："${o.msg}" → 材质窗未贴宿主窗下方（z 序/几何失效）`);
            }
          }
        } else if (o.msg.startsWith('error:') || o.msg.startsWith('attach-fail:')) {
          warnOnce('attachfail-late', `材质窗回包 error/attach-fail（非在途）："${o.msg}"`);
        }
        // ② rects 轴：原生 DoRects 回包 "SetWindowRgn=… GetWindowRgn=…"，功能性失败 =
        // 区域创建失败 / 区域并集失败（旧原型曾静默刷 336 行 combine-fail，全展开态幕墙无材质而无人发现）。
        // 'SetWindowRgn=0' 不判——隐藏态区域更新丢失由 show 重放补偿属预期行为；点亮态同样不判
        // （仅 DoShow 在 show 时刻重放补偿，见上方 ② 说明）。未点亮稳态（已 attach 且处于 hide 之后）时
        // 点亮的首个 rects 回包命中该串，纳入判据会误报并烧掉一次性槽位。
        if (o.msg.includes('create-rgn-fail') || o.msg.includes('combine-fail')) {
          warnOnce('rectsfail', `材质窗 rects 回包功能性失败："${o.msg}" → 区域裁剪未生效（材质窗未按区域显隐）`);
        }
      }
    } catch (e) { /* stdout 只承载协议行，非协议行忽略 */ }
  }
}

function onPreheatMsg(msg) {
  try {
    if (preheatStage === 0 && msg === 'attach') {
      preheatStage = 1;
      cmd({ cmd: 'rects', rects: [], viewW: 0, viewH: 0 });
    } else if (preheatStage === 1 && typeof msg === 'string' && msg.indexOf('rects') === 0) {
      preheatStage = 2;
      cmd({ cmd: 'hide' });
    } else if (preheatStage === 2 && msg === 'hide') {
      onReady();
    }
  } catch (e) { warnOnce('preheat', e && e.message); }
}

function onReady() {
  if (dead) return;
  ready = true;
  if (handshakeTimer) { clearTimeout(handshakeTimer); handshakeTimer = null; }
  // 真实 attach（窗已存在时；也补齐 z 序/几何）→ 按 regions → progress → visible 重放快照
  try {
    const win = liveWin();
    if (win) ensureAttached(win);
    if (snap.regions) sendRects(snap.regions, snap.radius, win);
    // progress：T2 揭示为瞬时（无 alpha 通道、无前沿动画）→ 仅记录，不得置终态（§5.3）
    if (snap.disposed) {
      cmd({ cmd: 'rects', rects: [], viewW: 0, viewH: 0 });
      cmd({ cmd: 'hide' });
    } else if (snap.visible && win) {
      // win==null（chat 窗尚不存在）→ 不 show（会 show 到 off-screen 预热停靠位）；
      // 点亮帧到达时窗必已存在，由常规路径 setRegions/setVisible 接管
      cmd({ cmd: 'show' });
    } else if (snap.regions && snap.fadeMs > 0) {
      scheduleHide(snap.fadeMs);
    }
  } catch (e) { warnOnce('ready', e && e.message); }
}

function onGone(why) {
  if (dead) return;
  dead = true;
  ready = false;
  if (handshakeTimer) { clearTimeout(handshakeTimer); handshakeTimer = null; }
  if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
  if (geomTimer) { clearTimeout(geomTimer); geomTimer = null; }
  geomPending = false;
  const p = proc;
  proc = null;
  if (p) {
    try { if (p.exitCode === null && !p.killed) p.kill(); } catch (e) { /* 已退出 */ }
  }
  warnOnce('gone', `材质窗${why} → 本会话全 no-op（不重启，§5.7）`);
}

function sendRects(rects, radius, win) {
  // DIP 坐标下发，× scaleFactor 换算在 WinFx 进程内完成（attach 时下发）
  const list = (Array.isArray(rects) ? rects : [])
    .filter((r) => r && Number.isFinite(r.w) && Number.isFinite(r.h) && r.w > 0 && r.h > 0)
    .map((r) => ({
      x: Number.isFinite(r.x) ? r.x : 0,
      y: Number.isFinite(r.y) ? r.y : 0,
      w: r.w,
      h: r.h,
      r: Number.isFinite(r.r) ? r.r : (Number.isFinite(radius) ? radius : 0),
    }));
  let vw = 0, vh = 0;
  try { if (win) [vw, vh] = win.getSize(); } catch (e) { /* ignore */ }
  cmd({ cmd: 'rects', rects: list, viewW: vw, viewH: vh });
}

function scheduleHide(ms) {
  if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
  if (ms <= 0) { cmd({ cmd: 'hide' }); return; }
  hideTimer = setTimeout(() => {
    hideTimer = null;
    if (!dead && ready) cmd({ cmd: 'hide' });
  }, ms);
}

// 应用退出统一回收（进程常驻，真正回收只在这里，§5.3 dispose 语义）
try {
  app.on('will-quit', () => {
    try {
      if (proc && proc.exitCode === null) {
        try { proc.stdin.write(JSON.stringify({ cmd: 'quit' }) + '\n'); } catch (e) { /* ignore */ }
        setTimeout(() => {
          try { if (proc && proc.exitCode === null) proc.kill(); } catch (e) { /* ignore */ }
        }, 300).unref();
      }
    } catch (e) { /* ignore */ }
  });
} catch (e) { /* ignore */ }
try {
  screen.on('display-metrics-changed', () => scheduleGeom());   // DPI 变化 → 重 attach（新 scaleFactor）
} catch (e) { /* ignore */ }

module.exports = {
  // 驱动层探针（§5.2）：同步、不 spawn；false = 本会话全 no-op
  isAvailable,

  // 驱动层注入「实时解析当前 chat 窗」；仅探针通过后驱动才会调用 → 此处启动预热（唯一 spawn 点）
  attach(fn) {
    resolveWin = typeof fn === 'function' ? fn : () => null;
    startPrewarm();
  },

  // rects: [{x,y,w,h,r}] CSS/DIP 顶左原点；radius 为缺省圆角。空列表 = 区域归零。
  setRegions(rects, radius) {
    const list = Array.isArray(rects) ? rects : null;
    snap.regions = list;
    snap.radius = radius;
    snap.disposed = false;
    if (!ready || dead) return;
    try {
      const win = liveWin();
      if (!win) return;
      ensureAttached(win);
      sendRects(list, radius, win);
    } catch (e) { warnOnce('rects', e && e.message); }
  },

  // visible=true → show（当帧活显，区域随 show 重放）；visible=false → ms 后 hide（宿主常驻）。
  setVisible(visible, fadeMs) {
    const ms = Math.max(0, Number.isFinite(fadeMs) ? fadeMs : 0);
    snap.visible = !!visible;
    snap.fadeMs = ms;
    snap.disposed = false;
    if (!ready || dead) return;
    try {
      if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
      const win = liveWin();
      if (!win) return;
      if (visible) {
        ensureAttached(win);
        cmd({ cmd: 'show' });
      } else {
        scheduleHide(ms);
      }
    } catch (e) { warnOnce('vis', e && e.message); }
  },

  // 续跑对齐：T2 揭示为瞬时 → 仅记录进度（不得置终态）；观测层帧上报接管后由 regions 轴跟随。
  setProgress(t) {
    snap.progress = Math.min(1, Math.max(0, Number.isFinite(t) ? t : 0));
  },

  // 回驻留隐藏态：区域归零 + hide；宿主常驻不销毁（再点亮免冷启动），app 退出时统一回收。
  dispose() {
    if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
    snap = newSnap(true);
    if (!ready || dead) return;
    try {
      const win = liveWin();
      if (win) ensureAttached(win);
      cmd({ cmd: 'rects', rects: [], viewW: 0, viewH: 0 });
      cmd({ cmd: 'hide' });
    } catch (e) { warnOnce('dispose', e && e.message); }
  },
};
