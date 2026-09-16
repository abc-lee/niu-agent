// mini-blur-observer.js —— 磨砂模糊观测层（T3，plan: docs/superpowers/plans/2026-09-16-winfx-integration.md §0 分层①）
// 红线：跨平台共用、纯只读观测（DOM 类名 / CSS 布局几何 / window 事件推导状态），
// 不调用也不修改任何既有函数（miniCurtainApply/enterMini/applyMiniDom/exitMini/miniPointerInside 全部原样）。
// isolation：默认不建层、不上报；仅当收到下行 chat-mini-blur-config 且
// {available:true, enabled:true}（启用态唯一来源=反向通道）才建 D9 罩层 / 加 body.blur-on / 开始上报。
// 全 try/catch：任何异常不得影响页面。
(function () {
  'use strict';
  if (typeof window === 'undefined' || typeof document === 'undefined') return;
  var API = window.electronAPI || {};
  if (typeof API.reportBlurState !== 'function' || typeof API.onBlurConfig !== 'function') return;

  var RADIUS_PILL = 23;    // 胶囊区域圆角 = --mini-pill-h(46px)/2（chat.html:916 + :1024 border-radius:calc(var(--mini-pill-h)/2)）
  var RADIUS_PANEL = 18;   // 面板（幕墙）顶角圆角（chat.html #mini-panel border-radius:18px 18px 0 0；底角直角）
  var LEAD = 40;           // T0 坑③：mask 前沿领先幕墙视觉前沿 ~40px（初段 1-2 帧瞬时滞后）
  var ANIM_TIMEOUT = 400;  // 幕墙 clip-path transition .2s；跟随循环 400ms 余量兜底结束
  var ARM_TIMEOUT = 2000;  // 扣留释放：首个 window resize 或 2s 超时（自愈路径无 resize，防永锁）
  var POLL_MS = 100;       // 稳态轮询（类名/几何变化检测）

  var enabled = false;
  var armed = false, released = false;   // 武装/释放（扣留）
  var open = false;
  var openSince = 0;                     // open 由假翻真的墙钟时刻（Date.now()）
  var seq = 0;
  var veil = null;        // D9 罩层容器（自身无背景、0×0；子块每 region 一个）
  var veilHop = 0.08;     // 子块背景浓度 = blur.hoverOpacity（默认 0.08）
  var pollTimer = null, armTimer = null;
  var rafId = 0, rafQueueLen = 0;
  var animDir = 0;                       // 0 空闲 / 1 展开中 / 2 收起中
  var animStart = 0;
  var lastRegionsJson = '';

  function $(id) { try { return document.getElementById(id); } catch (e) { return null; } }
  function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }

  // 区域 = offset* 布局几何（与命中盒 miniPointerInside 同源坐标系，与 client 系同原；
  // 禁对 transform 元素取 GBCR）
  function rectOf(el, radius) {
    var r = { x: el.offsetLeft, y: el.offsetTop, w: el.offsetWidth, h: el.offsetHeight, r: radius };
    return (r.w > 0 && r.h > 0) ? r : null;
  }

  // 幕墙前沿 inset（#mini-panel clip-path 首分量）：动画期 getComputedStyle 返回百分比（T0 坑①）→ 换算 px
  function clipTopInsetPx() {
    try {
      var panel = $('mini-panel');
      if (!panel) return 0;
      var m = String(window.getComputedStyle(panel).clipPath || '').match(/inset\(\s*([0-9]*\.?[0-9]+%?)/);
      var v = 0;
      if (m) {
        var t = m[1].trim();
        if (t.slice(-1) === '%') v = (parseFloat(t) / 100) * panel.offsetHeight;
        else { var n = parseFloat(t); v = isFinite(n) ? n : 0; }
      }
      return clamp(v, 0, panel.offsetHeight || 0);
    } catch (e) { return 0; }
  }

  // 区域集合：胶囊矩形 + 幕墙矩形（裁剪至幕前缘 − LEAD 余量）
  function computeRegions(includePill) {
    var regions = [];
    var pill = $('mini-pill');
    if (includePill && pill) { var pr = rectOf(pill, RADIUS_PILL); if (pr) regions.push(pr); }
    var panel = $('mini-panel');
    if (panel && panel.offsetHeight > 0) {
      var top = panel.offsetTop, h = panel.offsetHeight;
      var inset = clipTopInsetPx();
      var y = clamp(top + inset - LEAD, top, top + h - 1);
      var hh = top + h - y;
      if (hh > 0) {
        // 矩形分解（原生端每矩形仅支持单一 r，且 r 按轴缩放 min(r,w/2)×min(r,h/2)——带高必须 ≥2r 才得正圆角）：
        // 全开（inset=0）→ 顶部高 36px 圆角带（h=2×18，r=18，顶角为 18px 正圆弧，与 CSS 18px 18px 0 0 一致；
        // 带底缘圆弧凹口被下方方块补回）+ 下方方块（r=0，自 y+18 起，底角直角）；
        // 裁剪/动画中（inset>0）→ 可见幕墙顶缘是 clip 直线 → 单矩形 r=0。
        // band 标记仅供罩层子块用（子块高取 r、圆角只顶角 → 与下方方块零重叠补形，重叠半透明叠加会双浓度）；
        // 原生端 setRegions 只读 x/y/w/h/r，忽略该字段。
        var fullOpen = inset <= 0;
        if (fullOpen && hh > RADIUS_PANEL * 2) {
          regions.push({ x: panel.offsetLeft, y: y, w: panel.offsetWidth, h: RADIUS_PANEL * 2, r: RADIUS_PANEL, band: true });
          regions.push({ x: panel.offsetLeft, y: y + RADIUS_PANEL, w: panel.offsetWidth, h: Math.round(hh - RADIUS_PANEL), r: 0 });
        } else {
          regions.push({ x: panel.offsetLeft, y: y, w: panel.offsetWidth, h: Math.round(hh), r: fullOpen ? RADIUS_PANEL : 0 });
        }
      }
    }
    return regions;
  }

  function report(regions, isOpen) {
    try {
      seq += 1;
      API.reportBlurState({ open: !!isOpen, regions: regions, viewW: window.innerWidth, viewH: window.innerHeight, openSince: openSince, seq: seq });
      layoutVeil(regions);   // 罩层几何跟随每帧 regions（空 → 尺寸 0）
    } catch (e) { /* 观测层绝不影响页面 */ }
  }

  // D9 罩层：新增覆盖层（运行时注入的新 DOM，既有元素样式声明不动）；
  // 内联样式 fixed/pointer-events:none，z-index 低于既有构件（#mini-bar z-index:9999）；
  // 可见性由样式表规则驱动（chat.html 新增：#mini-blur-veil{display:none} 默认隐藏 +
  // body.blur-on #mini-blur-veil{display:block} 点亮，作用于容器、子块随容器整树显隐）——
  // 禁内联 display（内联优先级高于普通声明，会把 blur-on 规则顶掉 → 罩层永不显示）；
  // 结构 = 1 个容器（无背景、0×0，fixed 于 viewport 原点）+ 每 region 1 个子块
  // （absolute，背景 rgba 白纱，border-radius 随该 region 的 r；几何与原生 mask 同源、零重叠补形）：
  // D9 缺陷修正（用户真机反馈：并集包围盒 = 无圆角大矩形盖住胶囊∪幕墙最外缘）——
  // 容器不再按 regions 并集铺无圆角大矩形；
  // 顶带子块高取 r（只顶角圆弧）、自 y+r 起与下方方块子块首尾相接 → 覆盖与原生蒙版一致且不双浓度。
  function ensureVeil(hop) {
    veilHop = hop;
    if (veil) { try { applyVeilHop(); } catch (e) { } return; }
    try {
      var el = document.createElement('div');
      el.id = 'mini-blur-veil';
      el.style.position = 'fixed';
      el.style.left = '0px';
      el.style.top = '0px';
      el.style.width = '0px';    // 容器恒 0×0：仅作子块定位原点（子块 absolute 相对容器顶左 = viewport 原点）
      el.style.height = '0px';
      el.style.pointerEvents = 'none';
      el.style.zIndex = '9998';
      // 容器自身不设背景：背景在各子块上（每 region 一块）——消除无圆角大矩形
      document.body.appendChild(el);
      veil = el;
    } catch (e) { /* 罩层失败不影响上报主链 */ }
  }

  function applyVeilHop() {
    try {
      var bg = 'rgba(255,255,255,' + veilHop + ')';
      var kids = veil.children;
      for (var i = 0; i < kids.length; i++) kids[i].style.background = bg;
    } catch (e) { /* 罩层失败不影响上报主链 */ }
  }

  // 罩层子块 = 每帧 regions 1:1（与原生 mask 同源几何）；空 regions → 子块清零（容器恒 0×0 不变）。
  // 区域坐标（offset*，offsetParent=#mini-bar fixed inset:0 → 与 viewport 同原点）= absolute 定位坐标。
  // 子块池化：每次调用先回收多余子块，再复用既有的 / 补建缺失的——每帧不新建（无泄漏）。
  function layoutVeil(regions) {
    if (!veil) return;
    try {
      var kids = veil.children;
      while (kids.length > regions.length) {
        var last = kids[kids.length - 1];
        if (last.remove) last.remove(); else veil.removeChild(last);
      }
      for (var i = 0; i < regions.length; i++) {
        var g = regions[i];
        var kid = (i < veil.children.length) ? veil.children[i] : null;
        if (!kid) {
          kid = document.createElement('div');
          kid.style.position = 'absolute';
          kid.style.background = 'rgba(255,255,255,' + veilHop + ')';
          veil.appendChild(kid);
        }
        if (!g || !(g.w > 0) || !(g.h > 0)) {
          kid.style.left = '0px';   // 无效 region → 子块归零（不可见），保持与 regions 下标对齐
          kid.style.top = '0px';
          kid.style.width = '0px';
          kid.style.height = '0px';
          kid.style.borderRadius = '0';
          continue;
        }
        kid.style.left = g.x + 'px';
        kid.style.top = g.y + 'px';
        kid.style.width = g.w + 'px';
        // 顶带（band）：子块高取 r、圆角只顶角 r r 0 0 → 与下方方块零重叠且覆盖与原生蒙版一致；
        // 胶囊等独立圆角 region：全高 + 全角统一 r（如 r=23/h=46 胶囊）。
        kid.style.height = (g.r > 0 && g.band) ? g.r + 'px' : g.h + 'px';
        kid.style.borderRadius = g.r > 0 ? (g.band ? (g.r + 'px ' + g.r + 'px 0 0') : (g.r + 'px')) : '0';
      }
    } catch (e) { /* 罩层失败不影响上报主链 */ }
  }

  function setBlurOn(on) {
    try { var b = document.body; if (b) { if (on) b.classList.add('blur-on'); else b.classList.remove('blur-on'); } } catch (e) { }
  }

  function stopAnim() {
    animDir = 0;
    if (rafId) { try { if (window.cancelAnimationFrame) window.cancelAnimationFrame(rafId); } catch (e) { } rafId = 0; }
  }

  // 跟随模式：rAF 逐帧读 clip-path（百分比→px 换算 + 40px 前沿余量）；胶囊矩形首帧瞬开、末帧瞬闭（D10）
  function animFrame() {
    rafId = 0;
    if (!enabled || !armed) { stopAnim(); return; }
    try {
      var panel = $('mini-panel');
      var h = panel ? panel.offsetHeight : 0;
      var inset = clipTopInsetPx();
      var done = (animDir === 1) ? inset < 1 : (h > 0 ? inset >= h - 1 : true);
      if (done || Date.now() - animStart > ANIM_TIMEOUT) {
        var dir = animDir;
        stopAnim();
        if (dir === 1) { lastRegionsJson = ''; steadyReport(); }       // 展开结束 → 稳态全量上报
        else if (dir === 2) {                                            // 收起结束 → 末帧瞬闭
          open = false; setBlurOn(false); lastRegionsJson = ''; report([], false);
        }
        return;
      }
      report(computeRegions(true), true);   // 收起期胶囊矩形保留至末帧（D10）
      rafId = window.requestAnimationFrame(animFrame);
    } catch (e) { stopAnim(); }
  }

  function startAnim(dir) {
    stopAnim();
    animDir = dir;
    animStart = Date.now();
    lastRegionsJson = '';
    report(computeRegions(true), dir === 1 ? true : true);  // 首帧即报（胶囊矩形首帧瞬开）
    try { rafId = window.requestAnimationFrame(animFrame); } catch (e) { stopAnim(); }
  }

  // 稳态几何上报（新消息变高 / resize 落位后几何变化；JSON 去重）
  function steadyReport() {
    if (!enabled || !armed || !released) return;
    try {
      if (!document.body.classList.contains('mini')) return;
    } catch (e) { return; }
    var regions = computeRegions(true);
    var j = JSON.stringify(regions) + '|' + window.innerWidth + 'x' + window.innerHeight;
    if (j === lastRegionsJson) return;
    lastRegionsJson = j;
    report(regions, true);
  }

  function armNow() {
    armed = true; released = false; lastRegionsJson = '';
    try { clearTimeout(armTimer); } catch (e) { }
    try { armTimer = setTimeout(release, ARM_TIMEOUT); } catch (e) { }
  }

  function release() {
    try { if (armTimer) { clearTimeout(armTimer); armTimer = null; } } catch (e) { }
    if (!armed) return;
    released = true;
    try { tick(); } catch (e) { }   // 释放即判态：若已点亮（自愈/迟到配置）立即补上报
  }

  function disarm() {
    armed = false; released = false;
    try { if (armTimer) { clearTimeout(armTimer); armTimer = null; } } catch (e) { }
    stopAnim();
    if (open) { open = false; setBlurOn(false); report([], false); }  // 退出：body.mini 移除 → 上报 open:false
    lastRegionsJson = '';
  }

  // 主轮询：武装/释放 + open 翻转检测（open = #mini-bar 上 .mini-curtain-open，只读）
  function tick() {
    try {
      if (!enabled) return;
      var body = document.body;
      if (!body) return;
      var mini = body.classList.contains('mini');
      if (mini && !armed) armNow();
      if (!mini && armed) disarm();
      if (!armed || !released || !mini) return;
      if (animDir) return;   // 跟随循环驱动中
      var bar = $('mini-bar');
      var curtainOpen = !!(bar && bar.classList.contains('mini-curtain-open'));
      if (curtainOpen && !open) {
        open = true; openSince = Date.now(); setBlurOn(true);
        startAnim(1);
        return;
      }
      if (!curtainOpen && open) { startAnim(2); return; }
      if (open) steadyReport();
    } catch (e) { /* 观测层绝不影响页面 */ }
  }

  function onResize() {
    try {
      if (!enabled || !armed) return;
      if (!released) { release(); return; }   // 首个 resize → 释放扣留（落位信号）
      tick();                                  // 已释放：落位/变高后即时重测几何
    } catch (e) { }
  }

  function enable(hop) {
    if (enabled) { ensureVeil(hop); return; }
    enabled = true;
    ensureVeil(hop);
    try {
      window.addEventListener('resize', onResize);
      pollTimer = setInterval(tick, POLL_MS);
      tick();   // body.mini 已就位（自愈重载）→ 即刻武装
    } catch (e) { }
  }

  function disable() {
    if (!enabled) return;
    enabled = false;
    try { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } } catch (e) { }
    try { window.removeEventListener('resize', onResize); } catch (e) { }
    disarm();
    try { if (veil && veil.remove) veil.remove(); else if (veil && veil.parentNode) veil.parentNode.removeChild(veil); veil = null; } catch (e) { veil = null; }
  }

  // 下行接收：启用态唯一来源。收不到（后端缺失/加载失败/blur.enabled=false）→ 全程零副作用
  API.onBlurConfig(function (cfg) {
    try {
      if (cfg && cfg.available === true && cfg.enabled === true) {
        var hop = (typeof cfg.hoverOpacity === 'number') ? cfg.hoverOpacity : 0.08;
        enable(hop);
      } else {
        disable();
      }
    } catch (e) { }
  });
})();
