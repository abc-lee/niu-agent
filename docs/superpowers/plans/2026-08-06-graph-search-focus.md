# 知识图谱搜索聚焦修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 搜索选中实体后，自动聚焦到该实体（居中+放大），而非仅 zoomToFit 全部节点导致目标实体太小看不到。

**Architecture:** 修改 `renderer.js` 的 `enterSubgraph` 函数：在 d3-force 引擎稳定后（`onEngineStop` 回调），用 `zoomToFit(0, 40)` 即时适配画布（无 Tween 动画，避免与后续 centerAt+zoom Tween 冲突），然后立即 `centerAt(node.x, node.y, 800)` + `zoom(5, 800)` 聚焦中心实体。所有 setTimeout 回调内部检查 `myRequestId !== _subgraphRequestId` 防止快速操作时旧定时器在错误图数据上执行。`flashNodes` 移入 onEngineStop 回调，在聚焦动画完成后执行。

**Tech Stack:** force-graph v1.51.4 (vasturiano/force-graph), vanilla JS, Electron

---

## 深度分析

### 使用的库

**force-graph v1.51.4** — 2D force-directed graph，基于 d3-force 引擎，HTML5 canvas 渲染。

关键 API（来自 README + 源码验证）：
- `graph.graphData(data)` — 设置图数据，触发 d3-force 重新模拟
- `graph.zoomToFit(ms, px, nodeFilterFn)` — 自动缩放/平移使所有节点适配画布。**源码验证**：内部调用 `this.centerAt(center.x, center.y, transitionDuration)` + `this.zoom(zoomK, transitionDuration)`，各创建一个 Tween（持续 ms 毫秒）。ms=0 时无 Tween，立即完成。
- `graph.centerAt(x, y, ms)` — 设置视口中心坐标，ms>0 时创建 Tween 动画
- `graph.zoom(num, ms)` — 设置缩放级别（1=原始，>1放大，<1缩小），ms>0 时创建 Tween 动画
- `graph.onEngineStop(fn)` — d3-force 引擎停止时回调。**覆盖式**：每次调用替换之前的回调。引擎停止后只触发一次。
- `graph.graphData()` — 无参数时获取当前图数据（含节点 x/y 坐标）。引擎停止后坐标稳定不变。
- `graph.centerAt()` — 无参数时返回当前中心坐标 `{x, y}`

### 官方示例验证

**click-to-focus**（`node_modules/force-graph/example/click-to-focus/index.html`）：
```js
.onNodeClick(node => {
  Graph.centerAt(node.x, node.y, 1000);
  Graph.zoom(8, 2000);
});
```
官方做法：直接 `centerAt` + `zoom`，不需要先 `zoomToFit`。

**fit-to-canvas**（`node_modules/force-graph/example/fit-to-canvas/index.html`）：
```js
Graph.onEngineStop(() => Graph.zoomToFit(400));
```
官方做法：在 `onEngineStop` 回调中调 `zoomToFit`。

### 问题根因

**搜索→选中实体→`enterSubgraph()` 路径（renderer.js L959-1000）：**

```
selectSearchEntity (L946)
  → enterSubgraph(entity.id, 1) (L959)
    → exploreNode API 获取子图数据
    → currentData = { nodes, edges }
    → buildGraphData() + graph.graphData(freshData)  // 重绘，d3-force 重新模拟
    → graph.onEngineStop(onLayoutStop)  // 注册引擎停止回调
      → onLayoutStop: graph.zoomToFit(400, 40)  // 适配【全部】节点
    → showDetail(entityId)  // 显示详情面板
    → setTimeout(() => flashNodes([entityId]), 600)  // 600ms 后闪烁
```

**问题 1：`zoomToFit` 适配全部节点，不聚焦中心实体。**
`zoomToFit(400, 40)` 把子图所有节点缩放到画布内。当子图有 20+ 节点时，中心实体只占几个像素——用户看不到搜索的实体在哪。

**问题 2：`flashNodes` 时序错误。**
`setTimeout(() => flashNodes([entityId]), 600)` — 600ms 时 d3-force 还在跑（cooldownTime=15000ms），节点位置还在漂移。闪烁的节点可能已移到画布边缘。

**问题 3：`_subgraphMode` 设置时序。**
`selectSearchEntity` 在 `enterSubgraph` 返回后才设 `_subgraphMode = true`（L953）。虽然 enterSubgraph return → L955 是同步代码，pollChangelog 是宏任务不会插入，但将 `_subgraphMode = true` 移入 `enterSubgraph` 内部更内聚。

### 解决方案

修改 `enterSubgraph` 的 `onLayoutStop` 回调：
1. `zoomToFit(0, 40)` — **ms=0 无 Tween**，立即适配画布（避免与后续 centerAt+zoom Tween 冲突）
2. 立即 `centerAt(targetNode.x, targetNode.y, 800)` + `zoom(5, 800)` — 聚焦中心实体，800ms Tween 动画
3. 850ms 后（centerAt+zoom 动画完成）`flashNodes([entityId])` — 引擎已稳定，位置不再漂移

所有 setTimeout 回调内部检查 `myRequestId !== _subgraphRequestId`，防止快速操作（深度切换/退出子图）时旧定时器在错误图数据上执行。

将 `_subgraphMode = true` 和 `_subgraphCenterId = entityId` 移入 `enterSubgraph` 内部（在 `_justReplacedData = false` 之前），使守卫逻辑更内聚。

修改 `focusNodeBtn` 的 zoom 值从 3 到 5，与搜索聚焦一致。

`exitSubgraph` 的 `zoomToFit` 保持不变（返回总览需要看到全图）。`hideDetail()` 内部已清除 `currentSelectedNode`（L782），无需额外处理。

---

## File Structure

| 文件 | 职责 | 操作 |
|---|---|---|
| `ui/main/windows/graph/renderer.js` | force-graph 渲染器，搜索/子图/聚焦逻辑 | 修改 |

只修改一个文件。修改集中在 `enterSubgraph` 函数和 `focusNodeBtn` 事件监听器。

---

### Task 1: 修改 `enterSubgraph` 聚焦逻辑

**Files:**
- Modify: `ui/main/windows/graph/renderer.js:959-1000`

**当前代码（L959-1000）：**
```js
async function enterSubgraph(entityId, depth) {
  _subgraphRequestId++;
  const myRequestId = _subgraphRequestId;
  _justReplacedData = true;
  try {
    const result = await window.electronAPI.exploreNode(entityId, depth, 0, 'both');
    if (myRequestId !== _subgraphRequestId) return false;
    if (!result.nodes || result.nodes.length === 0) {
      _justReplacedData = false;
      if (_subgraphMode) await exitSubgraph();
      return false;
    }
    currentData = { nodes: result.nodes, edges: result.edges || [] };
    _prevNodePositions = {};
    currentPerspective = null;
    currentMatchIds = null;
    buildEdgeCountCache();
    applyForceConfig();
    const freshData = buildGraphData();
    graph.graphData(freshData);
    _reLayoutPending = true;
    const onLayoutStop = () => {
      if (!_reLayoutPending) return;
      _reLayoutPending = false;
      graph.zoomToFit(400, 40);
      graph.onEngineStop(() => {});
    };
    graph.onEngineStop(onLayoutStop);
    updateStats();
    _justReplacedData = false;

    currentSelectedNode = entityId;
    showDetail(entityId);
    setTimeout(() => flashNodes([entityId]), 600);
    return true;
  } catch (err) {
    console.error('Failed to enter subgraph:', err);
    if (myRequestId !== _subgraphRequestId) return false;
    _justReplacedData = false;
    return false;
  }
}
```

- [ ] **Step 1: 修改 `enterSubgraph` 的 `onLayoutStop` 回调和子图状态设置**

将 `renderer.js` L959-1000 整个 `enterSubgraph` 函数替换为：

```js
async function enterSubgraph(entityId, depth) {
  _subgraphRequestId++;
  const myRequestId = _subgraphRequestId;
  _justReplacedData = true;
  try {
    const result = await window.electronAPI.exploreNode(entityId, depth, 0, 'both');
    if (myRequestId !== _subgraphRequestId) return false;
    if (!result.nodes || result.nodes.length === 0) {
      _justReplacedData = false;
      if (_subgraphMode) await exitSubgraph();
      return false;
    }
    currentData = { nodes: result.nodes, edges: result.edges || [] };
    _prevNodePositions = {};
    currentPerspective = null;
    currentMatchIds = null;
    buildEdgeCountCache();
    applyForceConfig();
    const freshData = buildGraphData();
    graph.graphData(freshData);
    _reLayoutPending = true;
    const onLayoutStop = () => {
      if (!_reLayoutPending) return;
      _reLayoutPending = false;
      // zoomToFit(0, 40): ms=0 无 Tween，立即适配画布
      // 避免与后续 centerAt+zoom 的 Tween 冲突（两组 Tween 同时操作 zoom transform 会抖动）
      graph.zoomToFit(0, 40);
      // 立即聚焦中心实体（引擎已稳定，节点坐标不再漂移）
      const fgData = graph.graphData();
      const targetNode = fgData.nodes.find(n => n.id === entityId);
      if (targetNode && Number.isFinite(targetNode.x) && Number.isFinite(targetNode.y)) {
        graph.centerAt(targetNode.x, targetNode.y, 800);
        graph.zoom(5, 800);
        // 聚焦动画 800ms，延迟 850ms 确保动画完成后闪烁
        setTimeout(() => {
          if (myRequestId !== _subgraphRequestId) return;
          flashNodes([entityId]);
        }, 850);
      }
      graph.onEngineStop(() => {});
    };
    graph.onEngineStop(onLayoutStop);
    updateStats();
    // 子图状态在 _justReplacedData=false 之前设置，使 pollChangelog 守卫更内聚
    _subgraphMode = true;
    _subgraphCenterId = entityId;
    _subgraphDepth = depth;
    _justReplacedData = false;

    currentSelectedNode = entityId;
    showDetail(entityId);
    return true;
  } catch (err) {
    console.error('Failed to enter subgraph:', err);
    if (myRequestId !== _subgraphRequestId) return false;
    _justReplacedData = false;
    return false;
  }
}
```

关键改动：
1. `onLayoutStop` 回调：`zoomToFit(0, 40)` 无 Tween 立即适配 → `centerAt`+`zoom(5, 800)` 聚焦中心实体 → 850ms 后 `flashNodes`（检查 `myRequestId`）
2. `flashNodes` 的 setTimeout 内部检查 `myRequestId !== _subgraphRequestId` — 快速操作时旧定时器自动跳过
3. 删除外层 `setTimeout(() => flashNodes([entityId]), 600)` — 闪烁已在 onLayoutStop 回调内处理
4. `_subgraphMode = true` / `_subgraphCenterId` / `_subgraphDepth` 移入 enterSubgraph 内部（在 `_justReplacedData = false` 之前）

- [ ] **Step 2: 修改 `selectSearchEntity` 移除冗余的子图状态设置**

`selectSearchEntity`（L946-957）当前在 `enterSubgraph` 返回后设置 `_subgraphMode`、`_subgraphCenterId`、调用 `updateSubgraphControls`。由于 Task 1 已将这些状态移入 `enterSubgraph`，`selectSearchEntity` 只需调用 `updateSubgraphControls`。

将 `renderer.js` L946-957 替换为：

```js
async function selectSearchEntity(entity) {
  closeSearchDropdown();
  _justReplacedData = true;  // Block pollChangelog BEFORE the await
  const success = await enterSubgraph(entity.id, 1);
  if (success) {
    updateSubgraphControls();
  }
}
```

删除的代码：`_subgraphDepth = 1`（已在 enterSubgraph 中通过 depth 参数设置）、`_subgraphMode = true`（已移入）、`_subgraphCenterId = entity.id`（已移入）。

- [ ] **Step 3: 修改 `onNodeRightClick` 中的子图中心设置**

`onNodeRightClick`（L286-293）当前在 `enterSubgraph` 返回后设置 `_subgraphCenterId`。由于 Task 1 已将此移入 `enterSubgraph`，需要删除冗余设置。

将 `renderer.js` L286-293 替换为：

```js
  .onNodeRightClick(async (node) => {
    if (_subgraphMode) {
      await enterSubgraph(node.id, _subgraphDepth);
    } else {
      expandNode(node.id);
    }
  })
```

删除的代码：`const success = await enterSubgraph(...)` + `if (success) _subgraphCenterId = node.id`。`_subgraphCenterId` 已在 `enterSubgraph` 内部设置。

- [ ] **Step 4: 修改 `depth-up` 事件监听器移除冗余的深度设置**

`depth-up`（L1071-1079）当前在 `enterSubgraph` 返回后设置 `_subgraphDepth`。由于 Task 1 已将此移入 `enterSubgraph`，需要简化。

将 `renderer.js` L1071-1079 替换为：

```js
document.getElementById('depth-up').addEventListener('click', async () => {
  if (!_subgraphMode) return;
  const newDepth = Math.min(5, _subgraphDepth + 1);
  await enterSubgraph(_subgraphCenterId, newDepth);
  if (_subgraphDepth === newDepth) {
    document.getElementById('depth-display').textContent = newDepth;
  }
});
```

改动：删除 `const success = ...` + `if (success) { _subgraphDepth = newDepth; ... }`。`_subgraphDepth` 已在 `enterSubgraph` 内部设置（`_subgraphDepth = depth`）。检查 `_subgraphDepth === newDepth` 确认 enterSubgraph 成功（失败时不会更新深度）。

- [ ] **Step 5: 修改 `depth-down` 事件监听器移除冗余的深度设置**

将 `renderer.js` L1081-1089 替换为：

```js
document.getElementById('depth-down').addEventListener('click', async () => {
  if (!_subgraphMode) return;
  const newDepth = Math.max(1, _subgraphDepth - 1);
  await enterSubgraph(_subgraphCenterId, newDepth);
  if (_subgraphDepth === newDepth) {
    document.getElementById('depth-display').textContent = newDepth;
  }
});
```

- [ ] **Step 6: 验证修改无语法错误**

Run: `node -c ui/main/windows/graph/renderer.js`
Expected: 无输出（语法正确）

- [ ] **Step 7: 提交**

```bash
git add ui/main/windows/graph/renderer.js
git commit -m "fix: enterSubgraph now focuses on center entity after layout settles

- zoomToFit(0, 40) instant (no Tween) then centerAt+zoom(5, 800) focus
- flashNodes moved into onLayoutStop (after engine stabilizes)
- setTimeout callbacks guard with myRequestId against stale execution
- _subgraphMode/_subgraphCenterId/_subgraphDepth moved into enterSubgraph
- selectSearchEntity/onNodeRightClick/depth-up/depth-down simplified"
```

---

### Task 2: 修改 `focusNodeBtn` 缩放级别与子图模式一致

**Files:**
- Modify: `ui/main/windows/graph/renderer.js:810-819`

**当前代码（L810-819）：**
```js
focusNodeBtn.addEventListener('click', () => {
  if (!currentSelectedNode) return;
  // Find node in force-graph data and center on it
  const fgData = graph.graphData();
  const node = fgData.nodes.find(n => n.id === currentSelectedNode);
  if (node && node.x != null) {
    graph.centerAt(node.x, node.y, 800);
    graph.zoom(3, 800);
  }
});
```

**问题：** `zoom(3)` 与 Task 1 中 `enterSubgraph` 的 `zoom(5)` 不一致。用户搜索后看到 zoom=5 的视图，点"在图谱中聚焦"按钮却变成 zoom=3，体验不一致。

- [ ] **Step 1: 修改 zoom 值从 3 到 5**

将 `renderer.js` L810-819 替换为：

```js
focusNodeBtn.addEventListener('click', () => {
  if (!currentSelectedNode) return;
  // Find node in force-graph data and center on it
  const fgData = graph.graphData();
  const node = fgData.nodes.find(n => n.id === currentSelectedNode);
  if (node && node.x != null) {
    graph.centerAt(node.x, node.y, 800);
    graph.zoom(5, 800);
  }
});
```

- [ ] **Step 2: 验证语法**

Run: `node -c ui/main/windows/graph/renderer.js`
Expected: 无输出（语法正确）

- [ ] **Step 3: 提交**

```bash
git add ui/main/windows/graph/renderer.js
git commit -m "fix: focusNodeBtn zoom level 3→5 to match enterSubgraph focus"
```

---

### Task 3: 端到端验证

**Files:**
- 无文件修改，仅验证

**说明：** `exitSubgraph` 的 `zoomToFit` 保持不变（返回总览需要看到全图）。`hideDetail()` 内部已清除 `currentSelectedNode`（renderer.js L782），无需额外处理。

- [ ] **Step 1: 启动应用**

Run: `./niu`
Expected: 应用正常启动，知识图谱窗口可用

- [ ] **Step 2: 验证搜索聚焦**

1. 打开知识图谱窗口
2. 在搜索框输入关键词，回车搜索
3. 点击下拉列表中的一个实体
4. 验证：
   - 图谱重绘为该实体的子图
   - **中心实体自动居中并放大（zoom=5）**
   - 中心实体闪烁 3 次
   - 详情面板显示该实体信息
   - 深度控制按钮显示 "1"

- [ ] **Step 3: 验证深度切换聚焦**

1. 在子图模式下点击深度 "+" 按钮
2. 验证：子图扩展后，中心实体仍然居中放大并闪烁
3. 点击深度 "−" 按钮
4. 验证：子图缩小后，中心实体仍然居中放大并闪烁

- [ ] **Step 4: 验证快速操作不冲突**

1. 搜索选中一个实体，进入子图模式
2. 在聚焦动画进行中（800ms 内）立即点击深度 "+" 按钮
3. 验证：不会在错误图数据上执行旧定时器的 centerAt+zoom，新子图正确聚焦
4. 在聚焦动画进行中立即点击"返回总览"
5. 验证：总览图不会被执行 zoom(5)，正确显示全图

- [ ] **Step 5: 验证"在图谱中聚焦"按钮**

1. 在子图模式下，点击其他节点（非中心实体）
2. 点击详情面板中的"在图谱中聚焦"按钮
3. 验证：视图居中放大到该节点（zoom=5）

- [ ] **Step 6: 验证退出子图**

1. 点击"返回总览"按钮
2. 验证：
   - 图谱返回总览模式
   - `zoomToFit` 适配全图
   - 详情面板关闭
   - "在图谱中聚焦"按钮不会聚焦到已不存在的节点

- [ ] **Step 7: 验证右键扩散聚焦**

1. 在子图模式下，右键点击一个节点
2. 验证：以该节点为中心扩散，新中心实体居中放大并闪烁

---

## Self-Review

### 1. Spec coverage

| 用户需求 | 对应 Task |
|---|---|
| 搜索点击后重新聚焦被选择的实体 | Task 1: `enterSubgraph` 的 `onLayoutStop` 改为 `centerAt`+`zoom(5)` |
| 重新缩放页面以适配当前图结构 | Task 1: `zoomToFit(0, 40)` 即时适配全图，再 `centerAt`+`zoom(5)` 聚焦 |
| 节点多的时候能看到搜索聚焦的点 | Task 1: `zoom(5)` 放大到 5 倍，中心实体占据画布中心 |
| 手工能找到它在屏幕什么位置 | Task 1: `centerAt` 把实体移到画布中心 + `flashNodes` 闪烁 |
| 深度切换后也聚焦 | Task 1: 深度切换调用 `enterSubgraph` 自动继承聚焦逻辑 |

### 2. Placeholder scan

✅ 无占位符。所有代码块都是完整可执行的代码。所有步骤都有具体命令和预期输出。

### 3. Type consistency

✅ `enterSubgraph` 的 `entityId` 参数在所有调用方一致（`selectSearchEntity`、`depth-up`、`depth-down`、`onNodeRightClick`）。
✅ `enterSubgraph` 的 `depth` 参数在所有调用方一致。
✅ `_subgraphMode` / `_subgraphCenterId` / `_subgraphDepth` 在 `enterSubgraph` 内部统一设置，调用方不再重复设置。
✅ `zoom(5)` 在 Task 1（`enterSubgraph`）和 Task 2（`focusNodeBtn`）中一致。
✅ `myRequestId` 守卫在 `enterSubgraph` 函数体内所有异步路径（exploreNode 返回、onLayoutStop 回调、setTimeout 回调、catch 块）中一致使用。

### 4. 审查问题修复对照

| 审查问题 | 严重度 | 修复方式 |
|---|---|---|
| P1: setTimeout 链无取消机制 | P1 | setTimeout 回调内部检查 `myRequestId !== _subgraphRequestId`，快速操作时旧定时器自动跳过 |
| P2: zoomToFit Tween 与 centerAt+zoom Tween 冲突 | P2 | `zoomToFit(0, 40)` ms=0 无 Tween，立即完成，不与后续 centerAt+zoom Tween 冲突 |
| P3: Task 3 currentSelectedNode=null 冗余 | P3 | 删除原 Task 3。`hideDetail()` 内部已清除 `currentSelectedNode` |
| P2: _subgraphMode 设置时序 | P2 | `_subgraphMode = true` 移入 `enterSubgraph` 内部（在 `_justReplacedData = false` 之前） |
| P3: pollChangelog NaN 检测交互 | P3 | 当前安全（_subgraphMode 阻止 pollChangelog），记录在分析中 |
| P3: onEngineStop 回调覆盖 | P3 | 当前安全（子图模式下 perspective 按钮被禁用），记录在分析中 |

### 关键设计决策

1. **`zoomToFit(0, 40)` 无 Tween**：force-graph 源码验证 `zoomToFit(ms, px)` 内部调用 `centerAt(x, y, ms)` + `zoom(k, ms)`，ms>0 时各创建 Tween。ms=0 时无 Tween，立即完成布局适配。这避免了旧 zoomToFit Tween 与新 centerAt+zoom Tween 同时操作 zoom transform 导致的视口抖动。

2. **不使用 setTimeout 延迟分离 zoomToFit 和 centerAt**：原计划用 450ms 延迟等 zoomToFit(400ms) Tween 完成，但 Tween 完成时间不保证（requestAnimationFrame 帧率波动、标签页后台时 Tween 延迟）。改用 `zoomToFit(0, 40)` 无 Tween 方案，彻底消除时序依赖。

3. **`myRequestId` 守卫**：`enterSubgraph` 已有 `_subgraphRequestId` 机制防止快速点击竞态。所有 setTimeout 回调内部检查 `myRequestId !== _subgraphRequestId`，确保旧定时器在新请求到来时自动跳过。

4. **`zoom(5)` 而非 `zoom(8)`**：官方 click-to-focus 示例用 `zoom(8)`，但本项目子图节点更密集（有描述/媒体节点），`zoom(8)` 可能太近看不到邻居。`zoom(5)` 平衡——中心实体足够大，也能看到直接邻居。

5. **子图状态内聚**：`_subgraphMode` / `_subgraphCenterId` / `_subgraphDepth` 移入 `enterSubgraph` 内部设置，调用方（`selectSearchEntity`、`onNodeRightClick`、`depth-up`、`depth-down`）不再重复设置。这消除了状态设置分散导致的时序依赖。

6. **不修改 `exitSubgraph`**：退出子图返回总览需要看到全图，`zoomToFit(400, 40)` 是正确行为。`hideDetail()` 内部已清除 `currentSelectedNode`，无需额外处理。

7. **不修改 `reLayout` 和 `loadGraphSnapshot`**：这两个路径的 `zoomToFit` 是正确的——初始加载和透视切换需要看到全图。
