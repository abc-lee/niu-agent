// mini-blur-backend-win32.js —— 磨砂模糊后端（win32）：占位后端（全 no-op + 一条日志）。
// 平台差异唯一收口在驱动层的后端选择表；Windows 元件级亚克力路线（SystemBackdropElement
// + SetWindowRgn 药丸窗桥）另路另行实施时补真实现（docs/superpowers/2026-09-16-windows-w1plus-acrylic-report.md）。
'use strict';

try { console.log('[mini-blur-win32] 占位后端：全 no-op（Windows 亚克力另路实施时补真实现）'); } catch (e) { /* 无控制台时忽略 */ }

module.exports = {
  attach() {},
  setRegions() {},
  setVisible() {},
  setProgress() {},
  dispose() {},
};
