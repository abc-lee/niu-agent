// mini-blur-backend-darwin.js —— 磨砂模糊后端（darwin）：唯一平台分叉点。
// 包装原生模块 niu-winfx.<platform>-<arch>.node（node-gyp ObjC++，NSVisualEffectView +
// maskImage 子区域材质，T0 真机验证；材质 hud，state 钉 .active）。
// 句柄 = 当前 chat 窗 getNativeWindowHandle()（NSView*，模块内部 [view window] 换 NSWindow），
// 每次调用实时经驱动注入的 resolveWin 取窗，不缓存窗口引用（关窗重开/自愈重载后自动跟上新实例）。
'use strict';

let mod = null;
try {
  const m = require(`./niu-winfx.${process.platform}-${process.arch}.node`);
  if (m && m.isAvailable && m.isAvailable() === true) mod = m;
} catch (e) { mod = null; }

const warned = new Set();
function warnOnce(key, msg) {
  if (warned.has(key)) return;
  warned.add(key);
  try { console.error('[mini-blur-darwin]', key, msg); } catch (e) { /* 无控制台时忽略 */ }
}

let resolveWin = () => null;  // 驱动层 attach 注入
let installed = false;        // 本进程内是否已 installVibrancy（同窗重装会重复插 NSView）
let lastRects = null;         // 最近一次 setRegions 的矩形（CSS 顶左原点，含 r 圆角）
let lastSize = null;          // 对应窗口尺寸 [w,h]（DIP）

function getWindow() {
  try {
    const win = resolveWin();
    if (!win || win.isDestroyed()) return null;
    return win;
  } catch (e) { return null; }
}

function withWindow(fn) {
  if (!mod) return;
  try {
    const win = getWindow();
    if (!win) return;
    const handle = win.getNativeWindowHandle();
    fn(win, handle);
  } catch (e) { warnOnce('call', e && e.message); }
}

module.exports = {
  // 驱动层探针（§5.2 迁移）：= 原驱动双段探针（require .node + m.isAvailable()===true）逐条等价——
  // 本模块加载期已执行同两段（失败 → mod=null），故探针通过 ⇔ mod 非空。行为零变化。
  isAvailable() { return mod !== null; },

  // 驱动层注入"实时解析当前 chat 窗"函数（驱动不缓存窗口，窗口重建后自动跟随）
  attach(fn) { resolveWin = typeof fn === 'function' ? fn : () => null; },

  // rects: [{x,y,w,h,r}] CSS 顶左原点；radius 为缺省圆角（矩形自带 r 优先）。
  // 底左原点换算（y_cocoa = viewH − y − h）在模块内部完成，调用方无需处理。
  setRegions(rects, radius) {
    if (!mod) return;
    withWindow((win, handle) => {
      const [vw, vh] = win.getSize();
      const list = (Array.isArray(rects) ? rects : [])
        .filter(r => r && Number.isFinite(r.w) && Number.isFinite(r.h) && r.w > 0 && r.h > 0)
        .map(r => ({
          x: Number.isFinite(r.x) ? r.x : 0,
          y: Number.isFinite(r.y) ? r.y : 0,
          w: r.w,
          h: r.h,
          r: Number.isFinite(r.r) ? r.r : (Number.isFinite(radius) ? radius : 0),
        }));
      if (!list.length) return;
      lastRects = list;
      lastSize = [vw, vh];
      mod.setMask(handle, JSON.stringify(list), vw, vh);
    });
  },

  // visible=true：未装则 installVibrancy(handle,'hud',true)（材质 hud、state 钉 active），
  // 重铺最近矩形后 fadeTo(1,ms)；visible=false：fadeTo(0,ms)（材质视图保留，再点亮免重装）。
  setVisible(visible, fadeMs) {
    if (!mod) return;
    withWindow((win, handle) => {
      const ms = Math.max(0, Number.isFinite(fadeMs) ? fadeMs : 0);
      if (visible) {
        if (!installed) {
          mod.installVibrancy(handle, 'hud', true);
          installed = true;
        }
        if (lastRects && lastSize) {
          mod.setMask(handle, JSON.stringify(lastRects), lastSize[0], lastSize[1]);
        }
        mod.fadeTo(handle, 1, ms);
      } else {
        mod.fadeTo(handle, 0, ms);
      }
    });
  },

  // 续跑对齐：把 alpha 瞬时钉到 t（cubic-bezier 曲线值由驱动层算好），并重铺 mask 前沿，
  // 使驱动层在 open 已过半程才接入时（自愈重载/晚启动）模糊进度与幕墙动画同步。
  setProgress(t) {
    if (!mod) return;
    withWindow((win, handle) => {
      const a = Math.min(1, Math.max(0, Number.isFinite(t) ? t : 0));
      if (lastRects && lastSize) {
        mod.setMask(handle, JSON.stringify(lastRects), lastSize[0], lastSize[1]);
      }
      mod.setAlpha(handle, a);
    });
  },

  // 摘除材质视图（退出迷你/关窗/后端清除）；未装则无动作。
  dispose() {
    if (!mod) return;
    try {
      const win = getWindow();
      if (win) mod.removeVibrancy(win.getNativeWindowHandle());
    } catch (e) { warnOnce('dispose', e && e.message); }
    installed = false;
    lastRects = null;
    lastSize = null;
  },
};
