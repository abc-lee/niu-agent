# 脑区鬼火动画 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Chat 页面消息区右上角，用"精灵鬼火"气泡动画展示脑区点亮/熄灭状态变化——点亮时鬼火从右侧弹入→上飘淡出，熄灭时鬼火从右侧弹入→下坠消散，绿→黄不播动画。

**Architecture:** 纯前端方案。后端 SSE `brain_region_updated` 事件已有完整管道（后端 → main.js 转发 → chat.html `onBrainRegionsChanged` → `fetchBrainRegions()`）。在 `fetchBrainRegions()` 内部对比新旧脑区状态，检测"关闭→点亮"和"点亮/调暗→关闭"的变化，分别调用 `showLight(label)` / `showExtinguish(label)` 播放动画。动画容器放在 `.messages` **外部**（`.container` 内），用动态定位对齐 `.messages` 顶部，避免 `innerHTML=''` 销毁和滚动消失。

**Tech Stack:** 纯 CSS @keyframes 动画 + 原生 JS DOM 操作，无外部依赖。Electron renderer 进程内运行。

---

## 现有架构（不要改这些）

```
后端 RegionActivationManager
  ├─ activate_regions()     → notify_brain_region_sync('auto', [labels])   // 点亮
  ├─ reinforce_by_tool_use()→ notify_brain_region_sync('auto', [label])    // 点亮
  ├─ manual_activate()      → notify_brain_region_sync('manual', [labels]) // 点亮
  ├─ manual_dim()           → notify_brain_region_sync('manual', [labels]) // 熄灭
  └─ decay_all()            → notify_brain_region_sync('auto')             // 衰减（不传 labels）
         ↓ SSE event: {type: 'brain_region_updated', source, changed_labels}
  main.js L1832-1836 → chatWindow.webContents.send('brain-regions-changed', event)
         ↓ IPC
  chat.html L1876-1880 → onBrainRegionsChanged(() => fetchBrainRegions())
         ↓ IPC getBrainRegions → GET /api/brain/regions?include_dark=true
  chat.html L1682-1708 → _brainRegions = data.regions; renderBrainList()
```

**关键约束**：
- SSE 事件只带 `changed_labels`（字符串列表），**不区分点亮/熄灭方向**
- `decay_all()` 不传 `changed_labels`（空列表），需要全量对比
- 脑区状态三态：`activation > 0.7` = 点亮(🟢)，`0.3 < activation ≤ 0.7` = 调暗(🟡)，`≤ 0.3` = 关闭(⚫)
- **动画规则**：
  - `off → light`：播点亮动画 ✅
  - `light → off`：播熄灭动画 ✅
  - `dim → off`：播熄灭动画 ✅（dim 是 light 衰减的中间态，用户看到的"亮→灭"需要熄灭动画）
  - `light → dim`：不播动画（绿→黄，用户明确要求）
  - `dim → light`：不播动画（黄→绿，用户明确要求）
  - `off → dim`：不播动画（灭→黄，无点亮动画）
  - `dim → off` 以外的 dim 变化：不播动画
- **首次加载不播动画**：页面加载/刷新时 `_prevBrainState` 为空，首次 `fetchBrainRegions` 只初始化状态，不触发动画
- 动画容器必须放在 `.messages` **外部**——`.messages` 有三处 `innerHTML = ''`（L1235 clearChat、L1905 refreshFromDB、L2172 loadHistory）会销毁内部容器，且 `overflow-y: auto` 会使内部容器随滚动消失

## chat.html 关键源码位置（已验证）

| 位置 | 行号 | 说明 |
|---|---|---|
| `.container` CSS | L31-41 | 有 `position: relative`（L38），外部绝对定位容器的定位上下文 |
| `.messages` CSS | L113-118 | 有 `overflow-y: auto`（L115），L533-534 追加了 `position: relative` |
| `messages.innerHTML = ''` | L1235, L1905, L2172 | 三处清空，动画容器必须在 `.messages` 外部 |
| `escapeHtml()` | L1507 | 函数声明，同一 script 块内可用 |
| `clearChat()` | L1231 | async function，L1235 清空 messages |
| `fetchBrainRegions()` | L1682-1708 | 有防并发守卫 `_fetchingBrainRegions` |
| `onBrainRegionsChanged` | L1876-1880 | SSE 事件监听，调 fetchBrainRegions |
| `positionBrainElements()` | L1619-1636 | 动态计算 .messages 相对 .container 的偏移，用于脑区面板定位 |
| `_brainRegions` 声明 | L1548 | 脑区状态缓存 |

## File Structure

| 文件 | 操作 | 职责 |
|---|---|---|
| `ui/main/windows/assistant/chat.html` | 修改 | 唯一改动文件：加 CSS 动画规则、加 DOM 容器（外部）、改 fetchBrainRegions 增加变化检测、加 showLight/showExtinguish 函数、加 SSE debounce、加 positionBrainElements 定位 |

### chat.html 内部改动位置

| 区域 | 行号 | 改动 |
|---|---|---|
| `<style>` 块 | L628（`</style>` 前） | 插入脑区鬼火动画 CSS |
| DOM `.container` 内 | L663（`brainPanel` 之后） | 追加 `.brain-spark-container`（在 `.messages` 外部） |
| JS 脑区区块 | L1548（`_brainRegions` 声明处） | 新增 `_prevBrainState` Map + `_brainStateInitialized` 标志 |
| JS `onBrainRegionsChanged` | L1876-1880 | 加 debounce（100ms） |
| JS `fetchBrainRegions()` | L1682-1708 | 改造：加变化检测 + 首次加载守卫 |
| JS 脑区区块末尾 | L1708 后 | 新增 `showLight()` / `showExtinguish()` / `clearSparks()` 函数（动态获取 DOM） |
| JS `positionBrainElements()` | L1619-1636 | 追加 `.brain-spark-container` 动态定位 |
| JS `clearChat()` | L1235 后 | 追加 `clearSparks()` + `_prevBrainState.clear()` + `_brainStateInitialized = false` |

---

### Task 1: 添加鬼火动画 CSS

**Files:**
- Modify: `ui/main/windows/assistant/chat.html:627-628`（在 `.brain-empty` 规则后、`</style>` 前插入）

- [ ] **Step 1: 在 `</style>` 前插入鬼火动画 CSS**

在 `chat.html` 的 `.brain-empty { ... }` 规则之后、`</style>` 之前，插入以下 CSS。注意：`.brain-spark-container` 不设 `top` 值（由 JS `positionBrainElements()` 动态设置），只设 `right`。

```css
    /* ========== 脑区鬼火动画（点亮/熄灭） ========== */
    .brain-spark-container {
      position: absolute;
      /* top 由 JS positionBrainElements() 动态设置 */
      right: 12px;
      z-index: 250;  /* 高于 brain-panel z-index:200，防止面板打开时遮挡动画 */
      pointer-events: none;
      display: flex;
      flex-direction: column;
      align-items: flex-end;
      gap: 10px;
    }
    .spark-light-zone {
      display: flex;
      flex-direction: column;
      align-items: flex-end;
      gap: 6px;
    }
    .spark-extinguish-zone {
      display: flex;
      flex-direction: column;
      align-items: flex-end;
      gap: 6px;
    }
    .brain-spark {
      position: relative;
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 5px 12px 5px 8px;
      background: rgba(10, 30, 40, 0.75);
      border: 1px solid rgba(64, 224, 208, 0.35);
      border-radius: 16px;
      box-shadow: 0 0 6px 1px rgba(64, 224, 208, 0.2), 1px 1px 4px rgba(0,0,0,0.15);
      transform: translateZ(0);
    }
    .brain-spark .spark-core {
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: radial-gradient(circle at 30% 30%, #e0fffe, #40e0d0 40%, #1a9b8a);
      box-shadow: 0 0 4px 1px rgba(64, 224, 208, 0.5);
      transform: translateZ(0);
    }
    .brain-spark .spark-label {
      font-size: 12px;
      font-weight: 600;
      color: #7ff0e0;
      text-shadow: 0 0 4px rgba(64, 224, 208, 0.3);
    }
    /* 点亮动画：从右侧弹出（过冲回弹）→ 停留 → 上飘淡出 */
    .spark-light {
      animation: spark-light-rise 2.5s linear forwards;
    }
    .spark-light::before {
      content: '';
      position: absolute;
      top: 50%; left: 7px;
      width: 10px; height: 10px;
      border-radius: 50%;
      background: radial-gradient(circle, rgba(64, 224, 208, 0.25), transparent 70%);
      transform: translateZ(0);
      animation: spark-light-halo 2.5s linear forwards;
    }
    @keyframes spark-light-rise {
      0%   { opacity: 0; transform: translateZ(0) translateX(60px) scale(0.7); }
      8%   { opacity: 0.5; transform: translateZ(0) translateX(30px) scale(0.85); }
      16%  { opacity: 1; transform: translateZ(0) translateX(-4px) scale(1.05); }
      24%  { transform: translateZ(0) translateX(0) scale(1); }
      68%  { opacity: 1; transform: translateZ(0) translateX(0) scale(1); }
      80%  { opacity: 0.5; transform: translateZ(0) translateY(-3px) scale(0.97); }
      100% { opacity: 0; transform: translateZ(0) translateY(-10px) scale(0.9); }
    }
    @keyframes spark-light-halo {
      0%   { opacity: 0; transform: translateZ(0) translate(0, -50%) scale(0.5); }
      15%  { opacity: 0.5; transform: translateZ(0) translate(0, -50%) scale(1.5); }
      40%  { opacity: 0.2; transform: translateZ(0) translate(0, -50%) scale(2.5); }
      100% { opacity: 0; transform: translateZ(0) translate(0, -50%) scale(3.5); }
    }
    /* 熄灭动画：从右侧弹出（过冲回弹）→ 下坠消散 */
    .spark-extinguish {
      animation: spark-extinguish-fall 2.5s linear forwards;
    }
    .spark-extinguish::before {
      content: '';
      position: absolute;
      top: 50%; left: 7px;
      width: 10px; height: 10px;
      border-radius: 50%;
      background: radial-gradient(circle, rgba(64, 224, 208, 0.25), transparent 70%);
      transform: translateZ(0);
      animation: spark-extinguish-halo 2.5s linear forwards;
    }
    @keyframes spark-extinguish-fall {
      0%   { opacity: 0; transform: translateZ(0) translateX(60px) scale(0.7); }
      8%   { opacity: 0.5; transform: translateZ(0) translateX(30px) scale(0.85); }
      16%  { opacity: 1; transform: translateZ(0) translateX(-4px) scale(1.05); }
      24%  { transform: translateZ(0) translateX(0) scale(1); }
      30%  { opacity: 0.8; transform: translateZ(0) translateY(5px) scale(0.98); }
      50%  { opacity: 0.35; transform: translateZ(0) translateY(22px) scale(0.92); }
      70%  { opacity: 0.15; transform: translateZ(0) translateY(36px) scale(0.87); }
      100% { opacity: 0; transform: translateZ(0) translateY(60px) scale(0.8); }
    }
    @keyframes spark-extinguish-halo {
      0%   { opacity: 0.5; transform: translateZ(0) translate(0, -50%) scale(1); }
      30%  { opacity: 0.2; transform: translateZ(0) translate(0, -50%) scale(0.5); }
      100% { opacity: 0; transform: translateZ(0) translate(0, -50%) scale(0); }
    }
```

- [ ] **Step 2: 验证 CSS 插入正确**

```bash
grep -n 'spark-light-rise\|spark-extinguish-fall\|brain-spark-container' ui/main/windows/assistant/chat.html | head -10
```
Expected: 看到 3+ 行匹配，行号在 628 附近。

- [ ] **Step 3: Commit**

```bash
git add ui/main/windows/assistant/chat.html
git commit -m "feat(brain-spark): add ghost-fire animation CSS for brain region light/dim"
```

---

### Task 2: 添加鬼火动画 DOM 容器（外部方案）

**Files:**
- Modify: `ui/main/windows/assistant/chat.html:663`（`brainPanel` 之后追加）

动画容器必须放在 `.messages` **外部**，与 `brainPanel` 同级（在 `.container` 内）。原因：
1. `.messages` 有三处 `innerHTML = ''`（L1235 clearChat、L1905 refreshFromDB、L2172 loadHistory）会销毁内部容器
2. `.messages` 有 `overflow-y: auto`，内部容器会随消息滚动消失

- [ ] **Step 1: 在 `brainPanel` 之后追加动画容器**

当前 L660-663 是：
```html
    <div class="brain-panel" id="brainPanel">
      <div class="brain-title">脑区状态</div>
      <div id="brainList"></div>
    </div>
```

在 `brainPanel` 的 `</div>` 之后（L663 后）追加：
```html
    
    <!-- 脑区鬼火动画容器（在 .messages 外部，避免 innerHTML 清空和滚动影响） -->
    <div class="brain-spark-container" id="brainSparkContainer">
      <div class="spark-light-zone" id="sparkLightZone"></div>
      <div class="spark-extinguish-zone" id="sparkExtinguishZone"></div>
    </div>
```

- [ ] **Step 2: 在 positionBrainElements() 中追加动画容器动态定位**

在 `positionBrainElements()` 函数（L1619-1636）末尾（`brainPanel.style.bottom = 'auto';` 之后、函数 `}` 之前）追加：

```javascript
      // 鬼火动画容器：对齐 .messages 顶部，偏移 16px
      const sparkContainer = document.getElementById('brainSparkContainer');
      if (sparkContainer) {
        sparkContainer.style.top = (top + 16) + 'px';
        sparkContainer.style.bottom = 'auto';
      }
```

这里 `top` 变量已在 L1624 计算（`const top = rect.top - containerRect.top;`），是 `.messages` 相对于 `.container` 的顶部偏移。`+ 16` 是在 `.messages` 顶部再向下偏移 16px，与预览原型的 `top: 16px` 效果一致。

- [ ] **Step 3: 验证 DOM 和定位代码插入正确**

```bash
grep -n 'brainSparkContainer\|sparkLightZone\|sparkExtinguishZone' ui/main/windows/assistant/chat.html
```
Expected: 多行匹配，包括 HTML 声明和 JS 引用。

- [ ] **Step 4: Commit**

```bash
git add ui/main/windows/assistant/chat.html
git commit -m "feat(brain-spark): add external spark container with dynamic positioning"
```

---

### Task 3: 添加 showLight / showExtinguish / clearSparks 函数

**Files:**
- Modify: `ui/main/windows/assistant/chat.html:1708`（`fetchBrainRegions` 函数结束后插入）

函数内动态获取 DOM 引用（不使用模块级 const），确保容器被重建后仍可正常工作。

- [ ] **Step 1: 在 `fetchBrainRegions()` 之后插入动画控制函数**

在 `fetchBrainRegions()` 函数的 `}` 之后（约 L1708），`submitBrainChanges()` 之前（约 L1710），插入以下代码：

```javascript
    // ========== 脑区鬼火动画控制 ==========
    /**
     * 点亮一个脑区——创建鬼火元素追加到点亮区。
     * 动画结束后 remove 元素（与 showExtinguish 保持一致，防止长会话累积不可见元素）。
     * @param {string} label — 脑区名称
     */
    function showLight(label) {
      const zone = document.getElementById('sparkLightZone');
      if (!zone) return;
      const spark = document.createElement('div');
      spark.className = 'brain-spark spark-light';
      spark.innerHTML = '<div class="spark-core"></div><div class="spark-label">' + escapeHtml(label) + '</div>';
      zone.appendChild(spark);
      setTimeout(function() { spark.remove(); }, 2600);
    }

    /**
     * 熄灭一个脑区——创建鬼火元素追加到熄灭区。
     * 动画结束后 remove 元素（不留占位）。
     * @param {string} label — 脑区名称
     */
    function showExtinguish(label) {
      const zone = document.getElementById('sparkExtinguishZone');
      if (!zone) return;
      const spark = document.createElement('div');
      spark.className = 'brain-spark spark-extinguish';
      spark.innerHTML = '<div class="spark-core"></div><div class="spark-label">' + escapeHtml(label) + '</div>';
      zone.appendChild(spark);
      setTimeout(function() { spark.remove(); }, 2600);
    }

    /** 清空所有鬼火元素（聊天清空时调用） */
    function clearSparks() {
      const lz = document.getElementById('sparkLightZone');
      const ez = document.getElementById('sparkExtinguishZone');
      if (lz) lz.innerHTML = '';
      if (ez) ez.innerHTML = '';
    }
```

- [ ] **Step 2: 验证函数插入正确**

```bash
grep -n 'function showLight\|function showExtinguish\|function clearSparks' ui/main/windows/assistant/chat.html
```
Expected: 3 行匹配。

- [ ] **Step 3: Commit**

```bash
git add ui/main/windows/assistant/chat.html
git commit -m "feat(brain-spark): add showLight/showExtinguish/clearSparks functions"
```

---

### Task 4: 在 fetchBrainRegions 中添加状态变化检测

**Files:**
- Modify: `ui/main/windows/assistant/chat.html:1548`（新增 `_prevBrainState` + `_brainStateInitialized`）
- Modify: `ui/main/windows/assistant/chat.html:1682-1708`（改造 `fetchBrainRegions`）

这是核心任务：在 `fetchBrainRegions()` 拉到新数据后，对比旧状态检测变化，触发对应动画。

**动画规则（完整）**：
- `off → light`：播点亮动画
- `light → off`：播熄灭动画
- `dim → off`：播熄灭动画（dim 是 light 衰减的中间态，用户看到的"亮→灭"）
- `light → dim`、`dim → light`、`off → dim`：不播动画
- 首次加载（`_brainStateInitialized === false`）：只初始化状态，不播动画

- [ ] **Step 1: 新增 _prevBrainState 和 _brainStateInitialized 变量**

在 L1548 `let _brainRegions = [];` 之后新增两行：

```javascript
    let _brainRegions = [];           // 缓存后端返回的脑区状态
    let _prevBrainState = new Map();  // label → 'light'|'dim'|'off'，用于变化检测
    let _brainStateInitialized = false;  // 首次加载标志，true 后才触发动画
    let _brainRegionFetchPending = false;  // SSE 事件 pending 标志（Task 5 使用，必须在顶层声明供 fetchBrainRegions finally 块访问）
```
- [ ] **Step 2: 改造 fetchBrainRegions 增加变化检测**

将 `fetchBrainRegions` 函数中 `if (data && data.status === 'ok' && data.regions)` 块改为：

```javascript
        if (data && data.status === 'ok' && data.regions) {
          // 变化检测：对比新旧状态，触发鬼火动画
          const newRegions = data.regions;
          for (const r of newRegions) {
            const label = r.label || r.name;
            // 计算新状态：light(>0.7) / dim(>0.3) / off(≤0.3)
            let newState;
            if (r.activation > 0.7) newState = 'light';
            else if (r.activation > 0.3) newState = 'dim';
            else newState = 'off';

            const oldState = _prevBrainState.get(label);

            // 首次加载只记录状态，不触发动画
            if (_brainStateInitialized) {
              // off→light：点亮动画
              if ((!oldState || oldState === 'off') && newState === 'light') {
                showLight(label);
              }
              // light→off 或 dim→off：熄灭动画
              // （dim 是 light 衰减的中间态，从 dim 到 off 用户看到的也是"熄灭"）
              else if ((oldState === 'light' || oldState === 'dim') && newState === 'off') {
                showExtinguish(label);
              }
              // light→dim、dim→light、off→dim：不播动画（用户要求绿→黄不需要动画）
            }

            // 更新状态记录
            _prevBrainState.set(label, newState);
          }

          // 首次加载完成，标记已初始化
          _brainStateInitialized = true;

          _brainRegions = newRegions;
          // F006 修复：清理 _pendingBrainChanges 中已不存在的脑区 label
          const validLabels = new Set(newRegions.map(r => r.label || r.name));
          for (const label of Object.keys(_pendingBrainChanges)) {
            if (!validLabels.has(label)) {
              delete _pendingBrainChanges[label];
            }
          }
          // 面板打开时刷新显示
          if (brainPanel.classList.contains('visible')) {
            renderBrainList();
          }
        }
```

- [ ] **Step 3: 在 clearChat 中调用 clearSparks 并重置状态**

在 `clearChat` 函数（L1231）中，`messages.innerHTML = ''`（L1235）之后追加：

```javascript
          clearSparks();
          _prevBrainState.clear();
          _brainStateInitialized = false;
```

这确保清空聊天后，下次脑区变化从干净状态开始（首次加载模式，不误播动画）。

- [ ] **Step 4: 验证改造正确**

```bash
grep -n '_prevBrainState\|_brainStateInitialized\|showLight\|showExtinguish\|clearSparks' ui/main/windows/assistant/chat.html
```
Expected: 多行匹配，包括声明、赋值、调用处。

- [ ] **Step 5: Commit**

```bash
git add ui/main/windows/assistant/chat.html
git commit -m "feat(brain-spark): detect brain region state changes with first-load guard"
```

---

### Task 5: SSE 事件 debounce

**Files:**
- Modify: `ui/main/windows/assistant/chat.html:1876-1880`（改造 `onBrainRegionsChanged` 回调）

后端一次对话轮次中可能发送多个 SSE 事件（activate + reinforce×N + decay），`fetchBrainRegions` 的防并发守卫会直接丢弃正在进行中的后续调用。用 100ms debounce 确保最后一次 SSE 事件一定触发 fetch。

- [ ] **Step 1: 改造 onBrainRegionsChanged 回调为 debounce 模式**

当前 L1876-1880 是：
```javascript
    if (window.electronAPI && window.electronAPI.onBrainRegionsChanged) {
      window.electronAPI.onBrainRegionsChanged(() => {
        fetchBrainRegions();
      });
    }
```

改为：
```javascript
    if (window.electronAPI && window.electronAPI.onBrainRegionsChanged) {
      let _brainRegionChangeTimer = null;
      // 注意：_brainRegionFetchPending 在 Task 4 Step 1 中顶层声明（与 _fetchingBrainRegions 同级），
      // 不能在此 if 块内用 let 声明——fetchBrainRegions 的 finally 块在 if 块外部，无法访问块级作用域变量
      window.electronAPI.onBrainRegionsChanged(() => {
        // debounce 100ms：多个快速 SSE 事件合并为一次 fetch
        // 确保最后一次事件一定触发 fetch，不被防并发守卫丢弃
        if (_brainRegionChangeTimer) clearTimeout(_brainRegionChangeTimer);
        _brainRegionChangeTimer = setTimeout(() => {
          _brainRegionChangeTimer = null;
          if (_fetchingBrainRegions) {
            // 上一次 fetch 还在进行中，标记 pending，fetch 完成后会重跑
            _brainRegionFetchPending = true;
          } else {
            fetchBrainRegions();
          }
        }, 100);
      });
    }
```

同时需要改造 `fetchBrainRegions` 的 `finally` 块，在 fetch 完成后检查 pending 标志。当前 finally 块（L1705-1707）是：
```javascript
      } finally {
        _fetchingBrainRegions = false;
      }
```

改为：
```javascript
      } finally {
        _fetchingBrainRegions = false;
        // 如果 fetch 期间有新的 SSE 事件到达，重跑一次确保最终状态被检测
        if (_brainRegionFetchPending) {
          _brainRegionFetchPending = false;
          setTimeout(() => fetchBrainRegions(), 0);
        }
      }
```

**原理**：`fetchBrainRegions` 拉的是全量快照（`include_dark=true`），中间状态丢失不影响最终状态对比。debounce 100ms 合并快速 SSE 事件。如果 debounce 触发时上一次 fetch 还在进行中（IPC 延迟 >100ms），标记 pending，fetch 完成后重跑一次确保最终状态被检测。这与 `refreshFromDB` 的 `_refreshPending` 机制（L1884-1888）模式一致。

- [ ] **Step 2: 验证 debounce 代码插入正确**

```bash
grep -n '_brainRegionChangeTimer\|_brainRegionFetchPending' ui/main/windows/assistant/chat.html
```
Expected: 5+ 行匹配（timer 声明/clear/赋 null + pending 声明/赋 true/赋 false/检查）。

- [ ] **Step 3: Commit**

```bash
git add ui/main/windows/assistant/chat.html
git commit -m "feat(brain-spark): debounce SSE brain region events to prevent fetch skipping"
```

---

### Task 6: 端到端验证

**Files:**
- 无文件修改，纯验证

- [ ] **Step 1: 启动应用**

```bash
./niu
```

- [ ] **Step 2: 验证首次加载不播动画**

页面加载后观察消息区右上角——应该没有鬼火动画出现（即使后端有残留 activation 值）。

Expected：无动画。`_brainStateInitialized` 首次为 false，只初始化状态。

- [ ] **Step 3: 发送消息触发脑区激活（点亮动画）**

在聊天窗口发送一条会触发知识检索的消息（如"帮我分析一下 Python 异步编程"），观察消息区右上角是否出现鬼火弹出动画。

Expected：
- Agent 调用工具检索知识 → 后端 `activate_regions` → SSE → debounce 100ms → `fetchBrainRegions` → 检测到 off→light → `showLight` → 鬼火从右侧弹入→上飘淡出

- [ ] **Step 4: 验证熄灭动画（手动 dim 快速测试）**

通过脑区面板手动将一个点亮的脑区（🟢）切换到关闭（⚫），触发 `manual_dim` SSE → `fetchBrainRegions` 检测到 light→off → `showExtinguish` → 鬼火从右侧弹入→下坠消散。

Expected：熄灭动画正常播放。

- [ ] **Step 5: 验证衰减路径的熄灭动画（dim→off）**

等待多轮对话后，脑区从 light 衰减经过 dim 最终到 off。当 activation 从 dim(0.3-0.7) 衰减到 off(≤0.3) 时，应播放熄灭动画。

Expected：dim→off 触发 `showExtinguish`（这是 P0 修复的核心验证点）。

注意：衰减需要约 14 轮（0.92^14 ≈ 0.31），如果等待时间太长，可以跳过此步骤，手动 dim 测试（Step 4）已覆盖 light→off 路径。

- [ ] **Step 6: 验证绿→黄不播动画**

通过脑区面板手动将一个点亮的脑区（🟢）切换为调暗（🟡），观察是否**没有**动画播放。

Expected：无鬼火动画（light→dim 不触发 showLight/showExtinguish）。

- [ ] **Step 7: 验证 clearChat 清空动画**

输入 `/new` 或 `/clear` 清空聊天，观察鬼火元素是否被清除。

Expected：所有鬼火元素消失，`_prevBrainState` 清空，`_brainStateInitialized` 重置为 false，下次脑区变化从干净状态开始（不误播动画）。

- [ ] **Step 8: 验证动画不随消息滚动消失**

发送多条消息使消息列表可滚动，滚动消息列表，观察鬼火动画容器是否固定在消息区右上角。

Expected：动画容器固定不动，不随消息滚动（因为容器在 `.messages` 外部）。

- [ ] **Step 9: 验证并发场景**

快速连续发送多条消息，观察多个鬼火是否正确错峰显示（不重叠、不闪烁）。

Expected：多个鬼火在点亮区纵向排列，各自独立播放动画。SSE debounce 合并快速事件，不会因防并发守卫丢失动画。

---

## Self-Review

### 1. Spec coverage

| 需求 | 覆盖 Task |
|---|---|
| 点亮脑区动画（从右侧弹出→停留→上飘淡出） | Task 1 (CSS) + Task 3 (showLight) + Task 4 (off→light 触发) |
| 熄灭脑区动画（从右侧弹出→下坠消散） | Task 1 (CSS) + Task 3 (showExtinguish) + Task 4 (light→off + dim→off 触发) |
| 绿→黄不需要动画 | Task 4 (light→dim、dim→light 不触发) |
| 集成到 chat.html | Task 1-5 全部在 chat.html 内 |
| 容器在消息区右上角，不随滚动消失 | Task 1 (CSS 无 top) + Task 2 (外部 DOM + positionBrainElements 动态定位) |
| 首次加载不误播动画 | Task 4 (_brainStateInitialized 标志) |
| SSE 事件不丢失 | Task 5 (100ms debounce) |

### 2. R1 审查问题修复对照

| R1 问题 | 级别 | 修复 |
|---|---|---|
| dim→off 熄灭动画永不播放 | P0 | Task 4：`(oldState === 'light' \|\| oldState === 'dim') && newState === 'off'` 都触发 showExtinguish |
| 容器在 .messages 内部被三处 innerHTML='' 销毁 | P0 | Task 2：容器放在 .messages 外部（brainPanel 之后） |
| 外部方案缺少动态定位 | P0 | Task 2 Step 2：在 positionBrainElements() 中追加动态 top 计算 |
| .messages overflow-y 导致内部容器随滚动消失 | P1 | Task 2：外部容器不受 .messages 滚动影响 |
| _fetchingBrainRegions 防并发守卫丢弃 SSE 事件 | P1 | Task 5：100ms debounce 确保最后一次事件一定触发 fetch |
| 首次加载/页面刷新误播点亮动画 | P1 | Task 4：_brainStateInitialized 标志，首次只初始化不播动画 |
| Task 3 const vs Task 5 动态获取矛盾 | P2 | Task 3：统一使用函数内动态获取 `document.getElementById` |
| Task 2 条件分支未给确定结论 | P2 | Task 2：删除内部方案，明确外部方案为唯一方案 |
| escapeHtml 行号引用错误（L1106 → L1507） | P3 | 计划中已修正行号引用 |

### 2b. R2 审查问题修复对照

| R2 问题 | 级别 | 修复 |
|---|---|---|
| showLight 元素永不移除导致容器无限增长 | P1 | Task 3：showLight 加 `setTimeout(() => spark.remove(), 2600)`，与 showExtinguish 保持一致 |
| brain-panel z-index:200 遮挡 spark z-index:50 | P2 | Task 1：.brain-spark-container z-index 提高到 250 |
| debounce + 防并发守卫 fetch>100ms 事件丢失窗口 | P2 | Task 5：增加 _brainRegionFetchPending 标志（Task 4 Step 1 顶层声明），fetch finally 块检查 pending 并重跑 |

### 2c. R3 审查问题修复对照

| R3 问题 | 级别 | 修复 |
|---|---|---|
| _brainRegionFetchPending 作用域错误导致 finally 块抛 ReferenceError | P0 | Task 4 Step 1：_brainRegionFetchPending 移到顶层声明（与 _fetchingBrainRegions 同级）；Task 5：if 块内删除 let 声明，添加注释说明作用域 |

### 3. Type consistency

- `showLight(label)` / `showExtinguish(label)` 签名在 Task 3 和 Task 4 调用处一致。
- `_prevBrainState` 在 Task 4 Step 1 声明，Step 2 使用，Step 3 清空——名称一致。
- `_brainStateInitialized` 在 Task 4 Step 1 声明，Step 2 使用，Step 3 重置——名称一致。
- `_brainRegionFetchPending` 在 Task 4 Step 1 顶层声明，Task 5 if 块内赋值，Task 5 finally 块检查——名称一致，作用域正确（顶层声明，所有函数可访问）。
- `clearSparks()` 在 Task 3 定义，Task 4 Step 3 调用——名称一致。
- `sparkLightZone` / `sparkExtinguishZone` DOM ID 在 Task 2 创建、Task 3 动态获取——ID 一致。
- `brainSparkContainer` ID 在 Task 2 HTML 创建、Task 2 Step 2 JS 引用——ID 一致。
