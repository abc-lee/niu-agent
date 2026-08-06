# 知识图谱搜索聚焦修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 搜索选中实体后，自动聚焦到该实体（居中+放大），而非仅 zoomToFit 全部节点导致目标实体太小看不到。

**Architecture:** 修改 `renderer.js` 的 `enterSubgraph` 函数：在 d3-force 引擎稳定后（`onEngineStop` 回调），用 `centerAt(node.x, node.y)` + `zoom(N)` 聚焦中心实体，替代当前的 `zoomToFit` 全图适配。同时修复 `focusNodeBtn` 按钮逻辑使其在子图模式下也能正确聚焦。

**Tech Stack:** force-graph v1.51.4 (vasturiano/force-graph), vanilla JS, Electron

---

## 深度分析

### 使用的库

**force-graph v1.51.4** — 2D force-directed graph，基于 d3-force 引擎，HTML5 canvas 渲染。

关键 API（来自 README）：
- `graph.graphData(data)` — 设置图数据，触发 d3-force 重新模拟
- `graph.zoomToFit(ms, px, nodeFilterFn)` — 自动缩放/平移使所有节点适配画布
- `graph.centerAt(x, y, ms)` — 设置视口中心坐标，可选动画时长
- `graph.zoom(num, ms)` — 设置缩放级别（1=原始，>1放大，<1缩小），可选动画时长
- `graph.onEngineStop(fn)` — d3-force 引擎停止时回调（布局冻结）
- `graph.graphData()` — 无参数时获取当前图数据（含节点 x/y 坐标）
- `graph.graph2ScreenCoords(x, y)` — 图坐标→屏幕坐标
- `graph.d3ReheatSimulation()` — 重新加热模拟（alpha=1）

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
`setTimeout(() => flashNodes([entityId]), 600)` — 600ms 时 d3-force 还在跑（cooldownTime=15000ms），节点位置还在漂移。闪烁的节点可能已移到画布边缘。且 `flashNodes` 内部用 `graph.centerAt(c.x, c.y)` 触发重绘，保持当前视口中心不变——不会追踪到闪烁节点。

**问题 3：`onEngineStop` 回调互相覆盖。**
`loadGraphSnapshot`（L342）、`reLayout`（L612）、`enterSubgraph`（L986）、`exitSubgraph`（L1047）都调用 `graph.onEngineStop()` 注册回调。每次调用覆盖前一个。快速操作时回调可能被覆盖导致 `zoomToFit` 被跳过。

### 官方推荐做法

force-graph 官方 `click-to-focus` 示例：
```js
.onNodeClick(node => {
  Graph.centerAt(node.x, node.y, 1000);  // 居中到节点
  Graph.zoom(8, 2000);                    // 放大
});
```

本项目 `focusNodeBtn`（"在图谱中聚焦"按钮，L810-819）已经实现了正确逻辑：
```js
graph.centerAt(node.x, node.y, 800);
graph.zoom(3, 800);
```
但需要用户手动点击——搜索后应该自动执行。

### 解决方案

修改 `enterSubgraph` 的 `onLayoutStop` 回调：
1. 先 `zoomToFit(400, 40)` 让 d3-force 稳定后的布局适配画布（快速概览）
2. 立即用 `centerAt(entityNode.x, entityNode.y, 800)` + `zoom(5, 800)` 聚焦中心实体

`flashNodes` 改为在 `onLayoutStop` 回调中执行（引擎稳定后位置不再漂移），而非 600ms 定时器。

修改 `exitSubgraph` 的 `onLayoutStop` 回调保持 `zoomToFit`（退出子图返回总览需要看到全图）。

`focusNodeBtn` 按钮保持现有逻辑（已正确），但增大 zoom 值从 3 到 5 以与搜索聚焦一致。

---

## File Structure

| 文件 | 职责 | 操作 |
|---|---|---|
| `ui/main/windows/graph/renderer.js` | force-graph 渲染器，搜索/子图/聚焦逻辑 | 修改 |

只修改一个文件。所有修改集中在 `renderer.js` 的 `enterSubgraph` 函数和 `focusNodeBtn` 事件监听器。

---

### Task 1: 修改 `enterSubgraph` 聚焦逻辑

**Files:**
- Modify: `ui/main/windows/graph/renderer.js:979-992`

**当前代码（L979-992）：**
```js
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
```

**问题分析：**
1. `onLayoutStop` 只做 `zoomToFit` — 适配全部节点，中心实体太小
2. `flashNodes` 在 600ms 定时器中执行 — 引擎还在跑（15s cooldown），节点位置漂移
3. `_justReplacedData = false` 在 `onEngineStop` 注册后立即执行 — pollChangelog 可能在引擎稳定前就触发增量更新，覆盖回调

- [ ] **Step 1: 修改 `enterSubgraph` 的 `onLayoutStop` 回调**

将 `renderer.js` L979-992 替换为：

```js
    _reLayoutPending = true;
    const onLayoutStop = () => {
      if (!_reLayoutPending) return;
      _reLayoutPending = false;
      // 先 zoomToFit 让布局适配画布，再聚焦到中心实体
      graph.zoomToFit(400, 40);
      // 在 zoomToFit 完成后聚焦中心实体（zoomToFit 动画 400ms，延迟 450ms 确保完成）
      setTimeout(() => {
        const fgData = graph.graphData();
        const targetNode = fgData.nodes.find(n => n.id === entityId);
        if (targetNode && Number.isFinite(targetNode.x) && Number.isFinite(targetNode.y)) {
          graph.centerAt(targetNode.x, targetNode.y, 800);
          graph.zoom(5, 800);
          // 引擎已稳定，节点位置不再漂移，此时闪烁
          setTimeout(() => flashNodes([entityId]), 850);
        }
      }, 450);
      graph.onEngineStop(() => {});
    };
    graph.onEngineStop(onLayoutStop);
    updateStats();
    _justReplacedData = false;

    currentSelectedNode = entityId;
    showDetail(entityId);
    return true;
```

关键改动：
- `onLayoutStop` 回调中：先 `zoomToFit(400, 40)` 适配全图，450ms 后（zoomToFit 动画完成）`centerAt` + `zoom(5)` 聚焦中心实体
- `flashNodes` 移到 `onLayoutStop` 回调内部，在 `centerAt`+`zoom` 动画完成后（850ms）执行 — 引擎已稳定，位置不再漂移
- 删除外层 `setTimeout(() => flashNodes([entityId]), 600)` — 不再需要，闪烁已在回调中处理

- [ ] **Step 2: 验证修改无语法错误**

Run: `node -c ui/main/windows/graph/renderer.js`
Expected: 无输出（语法正确）

- [ ] **Step 3: 提交**

```bash
git add ui/main/windows/graph/renderer.js
git commit -m "fix: enterSubgraph now focuses on center entity after layout settles

Instead of only zoomToFit (which fits ALL nodes, making the target entity
too small to see), now zoomToFit first, then centerAt + zoom(5) on the
target entity. flashNodes moved into onLayoutStop callback (after engine
stabilizes) instead of 600ms timer (when positions were still drifting)."
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

将 `renderer.js` L817 的 `graph.zoom(3, 800)` 改为 `graph.zoom(5, 800)`：

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

### Task 3: 修复 `exitSubgraph` 同样使用聚焦逻辑

**Files:**
- Modify: `ui/main/windows/graph/renderer.js:1040-1047`

**当前代码（L1040-1047）：**
```js
    _reLayoutPending = true;
    const onLayoutStop = () => {
      if (!_reLayoutPending) return;
      _reLayoutPending = false;
      graph.zoomToFit(400, 40);
      graph.onEngineStop(() => {});
    };
    graph.onEngineStop(onLayoutStop);
    updateStats();
```

**分析：** `exitSubgraph` 返回总览模式，`zoomToFit` 是正确的行为（需要看到全图）。不需要聚焦特定节点。**此 Task 不修改代码**，仅确认 `exitSubgraph` 的 `zoomToFit` 行为正确。

但有一个问题：如果用户之前选中了一个节点（`currentSelectedNode`），退出子图后 `currentSelectedNode` 仍然指向子图中的实体 ID，但该实体可能不在总览数据中。`exitSubgraph` 中调用了 `hideDetail()`（L1052），但没有清除 `currentSelectedNode`。

- [ ] **Step 1: 在 `exitSubgraph` 中清除 `currentSelectedNode`**

在 `renderer.js` 的 `exitSubgraph` 函数中，找到 `} finally {` 块（L1049-1053）：

```js
  } finally {
    _justReplacedData = false;
    hideLoading();
    hideDetail();
  }
```

改为：

```js
  } finally {
    _justReplacedData = false;
    hideLoading();
    hideDetail();
    currentSelectedNode = null;
  }
```

这样退出子图后，`focusNodeBtn` 不会尝试聚焦一个已不存在的节点。

- [ ] **Step 2: 验证语法**

Run: `node -c ui/main/windows/graph/renderer.js`
Expected: 无输出（语法正确）

- [ ] **Step 3: 提交**

```bash
git add ui/main/windows/graph/renderer.js
git commit -m "fix: clear currentSelectedNode on exitSubgraph to prevent stale focus"
```

---

### Task 4: 修复深度切换（depth-up/depth-down）的聚焦逻辑

**Files:**
- Modify: `ui/main/windows/graph/renderer.js:1071-1089`

**当前代码（L1071-1089）：**
```js
document.getElementById('depth-up').addEventListener('click', async () => {
  if (!_subgraphMode) return;
  const newDepth = Math.min(5, _subgraphDepth + 1);
  const success = await enterSubgraph(_subgraphCenterId, newDepth);
  if (success) {
    _subgraphDepth = newDepth;
    document.getElementById('depth-display').textContent = newDepth;
  }
});

document.getElementById('depth-down').addEventListener('click', async () => {
  if (!_subgraphMode) return;
  const newDepth = Math.max(1, _subgraphDepth - 1);
  const success = await enterSubgraph(_subgraphCenterId, newDepth);
  if (success) {
    _subgraphDepth = newDepth;
    document.getElementById('depth-display').textContent = newDepth;
  }
});
```

**分析：** 深度切换调用 `enterSubgraph(_subgraphCenterId, newDepth)`，而 Task 1 修改后的 `enterSubgraph` 已经会聚焦到 `entityId` 参数（即 `_subgraphCenterId`）。所以深度切换**自动继承**了 Task 1 的聚焦逻辑——切换深度后也会自动聚焦中心实体。

**此 Task 不需要修改代码**，仅验证深度切换后聚焦逻辑正确工作。

- [ ] **Step 1: 确认深度切换路径不需要额外修改**

`enterSubgraph` 的 `entityId` 参数在深度切换时是 `_subgraphCenterId`（L1074, L1084），Task 1 修改的 `onLayoutStop` 回调用 `entityId` 查找目标节点并聚焦。深度切换时同一个中心实体会被聚焦，行为正确。

无需修改。跳到下一个 Task。

---

### Task 5: 端到端验证

**Files:**
- 无文件修改，仅验证

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

- [ ] **Step 3: 验证深度切换聚焦**

1. 在子图模式下点击深度 "+" 按钮
2. 验证：子图扩展后，中心实体仍然居中放大并闪烁
3. 点击深度 "−" 按钮
4. 验证：子图缩小后，中心实体仍然居中放大并闪烁

- [ ] **Step 4: 验证"在图谱中聚焦"按钮**

1. 在子图模式下，点击其他节点（非中心实体）
2. 点击详情面板中的"在图谱中聚焦"按钮
3. 验证：视图居中放大到该节点（zoom=5）

- [ ] **Step 5: 验证退出子图**

1. 点击"返回总览"按钮
2. 验证：
   - 图谱返回总览模式
   - `zoomToFit` 适配全图
   - 详情面板关闭
   - "在图谱中聚焦"按钮不会聚焦到已不存在的节点

- [ ] **Step 6: 验证右键扩散聚焦**

1. 在子图模式下，右键点击一个节点
2. 验证：以该节点为中心扩散，新中心实体居中放大并闪烁

---

## Self-Review

### 1. Spec coverage

| 用户需求 | 对应 Task |
|---|---|
| 搜索点击后重新聚焦被选择的实体 | Task 1: `enterSubgraph` 的 `onLayoutStop` 改为 `centerAt`+`zoom` |
| 重新缩放页面以适配当前图结构 | Task 1: 先 `zoomToFit` 适配全图，再 `centerAt`+`zoom(5)` 聚焦 |
| 节点多的时候能看到搜索聚焦的点 | Task 1: `zoom(5)` 放大到 5 倍，中心实体占据画布中心 |
| 手工能找到它在屏幕什么位置 | Task 1: `centerAt` 把实体移到画布中心 + `flashNodes` 闪烁 |
| 深度切换后也聚焦 | Task 4 验证：深度切换调用 `enterSubgraph` 自动继承聚焦逻辑 |

### 2. Placeholder scan

✅ 无占位符。所有代码块都是完整可执行的代码。所有步骤都有具体命令和预期输出。

### 3. Type consistency

✅ `enterSubgraph` 的 `entityId` 参数在所有调用方一致（`selectSearchEntity`、`depth-up`、`depth-down`、`onNodeRightClick`）。
✅ `currentSelectedNode` 在 `showDetail`、`hideDetail`、`focusNodeBtn`、`exitSubgraph` 中使用一致。
✅ `zoom(5)` 在 Task 1（`enterSubgraph`）和 Task 2（`focusNodeBtn`）中一致。

### 关键设计决策

1. **先 `zoomToFit` 再 `centerAt`+`zoom`**：`zoomToFit` 让 d3-force 稳定后的布局先适配画布（避免极端坐标），然后聚焦到中心实体。如果跳过 `zoomToFit` 直接 `centerAt`+`zoom`，可能因为节点坐标范围过大导致 `centerAt` 到一个偏离的位置。

2. **450ms 延迟**：`zoomToFit(400, 40)` 动画时长 400ms，延迟 450ms 确保动画完成后再 `centerAt`+`zoom`。如果两者同时执行，`zoomToFit` 的平移和 `centerAt` 的平移会冲突。

3. **850ms 延迟闪烁**：`centerAt(x, y, 800)` 动画 800ms，延迟 850ms 确保聚焦动画完成后再闪烁。引擎已稳定，节点位置不再漂移，闪烁位置准确。

4. **`zoom(5)` 而非 `zoom(8)`**：官方示例用 `zoom(8)`，但本项目子图节点更密集（有描述/媒体节点），`zoom(8)` 可能太近看不到邻居。`zoom(5)` 平衡——中心实体足够大，也能看到直接邻居。

5. **不修改 `exitSubgraph` 的 `zoomToFit`**：退出子图返回总览需要看到全图，`zoomToFit` 是正确行为。只清除 `currentSelectedNode` 防止 stale focus。

6. **不修改 `reLayout` 和 `loadGraphSnapshot`**：这两个路径的 `zoomToFit` 是正确的——初始加载和透视切换需要看到全图。
