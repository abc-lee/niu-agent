# 脑区状态侧滑面板 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Chat 窗口消息区右侧增加脑区状态侧滑面板，支持查看/手动切换脑区激活状态，发送消息时提交到后端影响向量检索。

**Architecture:** 前端面板（chat.html 内联 CSS/DOM/JS）→ fetch 调用 Python API → RegionActivationManager 更新 activation 值。复用现有 `loadStats()` 拉取链获取脑区状态，不新增 SSE 事件。后端新增 `POST /api/brain/regions/update` 端点 + `set_activation()` 方法。

**Tech Stack:** Python FastAPI（后端）、vanilla JS/CSS（前端，Electron 渲染进程）、RegionActivationManager（内存单例）

**Spec:** `docs/superpowers/specs/2026-07-29-brain-region-panel-design.md`

---

## File Structure

| 文件 | 职责 | 改动类型 |
|------|------|---------|
| `niu_api/internal/region_activation.py` | 新增 `set_activation()` 方法 | Modify |
| `niu_api/brain_region_api.py` | 新增 `POST /api/brain/regions/update` 端点 + Request Model | Modify |
| `ui/main/windows/assistant/chat.html` | CSS 样式 + DOM 节点 + JS 逻辑（面板渲染、交互、提交） | Modify |
| `tests/test_brain_region_api.py` | 后端接口测试 | Create |

---

### Task 1: 后端 — RegionActivationManager.set_activation() 方法

**Files:**
- Modify: `niu_api/internal/region_activation.py:447-476`（在 `manual_activate` 方法之后插入）
- Test: `tests/test_brain_region_set_activation.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for RegionActivationManager.set_activation()."""
import pytest
from niu_api.internal.region_activation import RegionActivationManager, BrainRegionState


@pytest.fixture
def mgr():
    """Create a manager with 3 test regions."""
    m = RegionActivationManager()
    m._regions = {
        "region_a": BrainRegionState(
            region_id="region_a", community_id="c1", label="区域A",
            activation=0.0, last_activated_at=0, activation_count=0, manually_dimmed=False,
        ),
        "region_b": BrainRegionState(
            region_id="region_b", community_id="c2", label="区域B",
            activation=0.5, last_activated_at=0, activation_count=0, manually_dimmed=False,
        ),
    }
    m._label_index = {"区域A": "region_a", "区域B": "region_b"}
    return m


def test_set_activation_green(mgr):
    """Set activation to 1.0 (green)."""
    mgr.set_activation("区域A", 1.0)
    state = mgr.find_region_by_label("区域A")
    assert state.activation == 1.0
    assert state.manually_dimmed is False


def test_set_activation_yellow(mgr):
    """Set activation to 0.5 (yellow/dimming)."""
    mgr.set_activation("区域A", 0.5)
    state = mgr.find_region_by_label("区域A")
    assert state.activation == 0.5
    assert state.manually_dimmed is False


def test_set_activation_black(mgr):
    """Set activation to 0.0 (black/off)."""
    mgr.set_activation("区域B", 0.0)
    state = mgr.find_region_by_label("区域B")
    assert state.activation == 0.0
    assert state.manually_dimmed is True


def test_set_activation_updates_last_activated_at(mgr):
    """Setting activation updates last_activated_at timestamp."""
    mgr.set_activation("区域A", 1.0)
    state = mgr.find_region_by_label("区域A")
    assert state.last_activated_at > 0


def test_set_activation_unknown_label(mgr):
    """Unknown label is silently ignored (no exception)."""
    mgr.set_activation("不存在的区域", 1.0)
    # No exception raised, no state changed
    assert mgr.find_region_by_label("区域A").activation == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/lilei/tools/ai-bot && python -m pytest tests/test_brain_region_set_activation.py -v`
Expected: FAIL with `AttributeError: 'RegionActivationManager' object has no attribute 'set_activation'`

- [ ] **Step 3: Write minimal implementation**

在 `niu_api/internal/region_activation.py` 的 `manual_activate` 方法之后（约 L476 之后），`manual_dim` 方法之前，插入：

```python
    def set_activation(self, region_label: str, activation: float) -> None:
        """Set region activation to an arbitrary value.

        Unlike manual_activate (1.0) and manual_dim (0.0), this supports
        the 'dimming' state (0.5) for the three-state UI toggle.

        Args:
            region_label: Human-readable region label.
            activation: Target activation value (0.0-1.0).
        """
        with self._lock:
            state = self.find_region_by_label(region_label)
            if state is None:
                logger.warning("set_activation: 未找到区域 '%s'", region_label)
                return

            state.activation = activation
            state.manually_dimmed = (activation == 0.0)
            state.last_activated_at = time.time()
            state.activation_count += 1

            logger.info("手动设置脑区 activation: %s = %.2f", region_label, activation)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/lilei/tools/ai-bot && python -m pytest tests/test_brain_region_set_activation.py -v`
Expected: 5 PASSED

- [ ] **Step 5: Commit**

```bash
cd /Users/lilei/tools/ai-bot
git add niu_api/internal/region_activation.py tests/test_brain_region_set_activation.py
git commit -m "feat(brain): RegionActivationManager 新增 set_activation() 支持任意 activation 值"
```

---

### Task 2: 后端 — POST /api/brain/regions/update 端点

**Files:**
- Modify: `niu_api/brain_region_api.py:41-43`（新增 Request Model）和 `niu_api/brain_region_api.py:140-141`（在 `get_brain_regions` 之后插入新端点）
- Test: `tests/test_brain_region_api.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for POST /api/brain/regions/update endpoint."""
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """Create test client with mocked RegionManager."""
    with patch("niu_api.brain_region_api._get_region_mgr") as mock_mgr, \
         patch("niu_api.brain_region_api._get_activation_mgr") as mock_act:
        mock_mgr.return_value = MagicMock()
        mock_mgr.return_value.get_all_regions.return_value = []
        mock_act_mgr = MagicMock()
        mock_act_mgr.get_region_map.return_value = []
        mock_act_mgr.get_status_light.return_value = "⚫"
        mock_act_mgr.set_activation = MagicMock()
        mock_act.return_value = mock_act_mgr
        from niu_api.app import app
        yield TestClient(app)


def test_update_regions_success(client):
    """POST /api/brain/regions/update with valid data returns ok."""
    response = client.post("/api/brain/regions/update", json={
        "regions": [
            {"label": "聊天历史", "activation": 1.0},
            {"label": "文档库", "activation": 0.5},
            {"label": "知识体系", "activation": 0.0},
        ]
    })
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["updated"] == 3


def test_update_regions_empty_list(client):
    """POST /api/brain/regions/update with empty list returns updated=0."""
    response = client.post("/api/brain/regions/update", json={
        "regions": []
    })
    assert response.status_code == 200
    assert response.json()["updated"] == 0


def test_update_regions_calls_set_activation(client):
    """Verify set_activation is called with correct args."""
    with patch("niu_api.brain_region_api._get_activation_mgr") as mock_act:
        mock_act_mgr = MagicMock()
        mock_act_mgr.set_activation = MagicMock()
        mock_act.return_value = mock_act_mgr
        response = client.post("/api/brain/regions/update", json={
            "regions": [{"label": "聊天历史", "activation": 0.5}]
        })
        assert response.status_code == 200
        mock_act_mgr.set_activation.assert_called_once_with("聊天历史", 0.5)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/lilei/tools/ai-bot && python -m pytest tests/test_brain_region_api.py -v`
Expected: FAIL with 404 or `AttributeError`

- [ ] **Step 3: Add Request Model**

在 `niu_api/brain_region_api.py` 的 `ConsolidateRequest` 类之后（约 L43），添加：

```python
class RegionActivationUpdate(BaseModel):
    """Request body for batch updating region activation values."""
    label: str
    activation: float


class RegionUpdateRequest(BaseModel):
    """Request body for POST /api/brain/regions/update."""
    regions: list[RegionActivationUpdate]
```

- [ ] **Step 4: Add endpoint**

在 `get_brain_regions` 端点之后（约 L140，`consolidate_brain_regions` 之前），添加：

```python
@router.post("/regions/update")
def update_region_activations(req: RegionUpdateRequest) -> dict[str, Any]:
    """Batch update brain region activation values.

    Called by the Chat UI brain panel when user manually toggles region states
    and sends a message. Values permanently overwrite the backend-computed
    activation (subject to subsequent natural decay).
    """
    try:
        activation_mgr = _get_activation_mgr()
        if activation_mgr is None:
            raise HTTPException(status_code=503, detail="Activation manager not initialized")

        updated = 0
        for item in req.regions:
            activation_mgr.set_activation(item.label, item.activation)
            updated += 1

        logger.info("[Brain Region API] Updated %d region activations", updated)

        return {
            "status": "ok",
            "updated": updated,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("[Brain Region API] update_region_activations failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd /Users/lilei/tools/ai-bot && python -m pytest tests/test_brain_region_api.py -v`
Expected: 3 PASSED

- [ ] **Step 6: Commit**

```bash
cd /Users/lilei/tools/ai-bot
git add niu_api/brain_region_api.py tests/test_brain_region_api.py
git commit -m "feat(brain): POST /api/brain/regions/update 批量更新脑区 activation"
```

---

### Task 3: 前端 — chat.html CSS 样式

**Files:**
- Modify: `ui/main/windows/assistant/chat.html`（`<style>` 块内，约 L460 `</style>` 之前插入）

- [ ] **Step 1: Add CSS styles**

在 `ui/main/windows/assistant/chat.html` 的 `<style>` 块末尾（约 L460，`</style>` 之前），追加以下样式：

```css
    /* ===== 脑区状态侧滑面板 ===== */
    .brain-trigger-zone {
      position: absolute;
      right: 0;
      top: 0;
      bottom: 0;
      width: 12px;
      z-index: 5;
    }
    .brain-overlay {
      position: absolute;
      left: 0;
      top: 0;
      bottom: 0;
      right: 175px;
      background: rgba(120, 80, 20, 0);
      transition: background 0.3s;
      z-index: 8;
      pointer-events: none;
    }
    .brain-overlay.visible {
      background: rgba(120, 80, 20, 0.15);
      pointer-events: auto;
    }
    .brain-panel {
      position: absolute;
      right: 0;
      top: 0;
      bottom: 0;
      width: 175px;
      background: #fff8e1;
      overflow-y: auto;
      transform: translateX(100%);
      transition: transform 0.3s ease;
      z-index: 10;
      box-shadow: -3px 0 10px rgba(180, 150, 50, 0.2);
      border-left: 2px dashed rgba(180, 150, 50, 0.3);
      font-size: 11px;
      color: #000;
      background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 200 200' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noiseB'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noiseB)' opacity='0.06'/%3E%3C/svg%3E");
    }
    .brain-panel.visible {
      transform: translateX(0);
    }
    .brain-title {
      padding: 10px 12px 6px;
      border-bottom: 1px dashed rgba(180, 150, 50, 0.3);
      font-weight: bold;
      font-size: 11px;
      color: #8B6914;
      position: sticky;
      top: 0;
      background: #fff8e1;
      z-index: 1;
    }
    .brain-group-label {
      padding: 6px 12px 3px;
      font-size: 9px;
      color: #999;
      letter-spacing: 0.5px;
    }
    .brain-item {
      padding: 5px 12px;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 6px;
      transition: background 0.15s;
    }
    .brain-item:hover {
      background: rgba(231, 202, 74, 0.15);
    }
    .brain-item .dot {
      font-size: 13px;
      flex-shrink: 0;
      line-height: 1;
    }
    .brain-item .name {
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .brain-item.off {
      opacity: 0.45;
    }
    .brain-empty {
      padding: 20px 12px;
      text-align: center;
      color: #999;
      font-size: 11px;
    }
```

- [ ] **Step 2: Verify CSS doesn't break existing layout**

Run: `cd /Users/lilei/tools/ai-bot/ui/main && npm start`
Expected: Chat 窗口正常启动，布局不变（CSS 只是新增类，没有 DOM 节点引用它们）

- [ ] **Step 3: Commit**

```bash
cd /Users/lilei/tools/ai-bot
git add ui/main/windows/assistant/chat.html
git commit -m "style(chat): 脑区侧滑面板 CSS 样式（便签风格）"
```

---

### Task 4: 前端 — chat.html DOM 节点

**Files:**
- Modify: `ui/main/windows/assistant/chat.html`（`.messages` 容器内，约 L528 附近）

- [ ] **Step 1: Locate the messages container end**

找到 `.messages` 容器的闭合 `</div>`。搜索 chat.html 中 `id="messages"` 的 div，在其闭合标签之前插入脑区面板 DOM。

- [ ] **Step 2: Add DOM nodes**

在 `.messages` 容器内部末尾（在所有消息 div 之后、闭合 `</div>` 之前），插入：

```html
      <!-- 脑区状态侧滑面板 -->
      <div class="brain-trigger-zone" id="brainTriggerZone"></div>
      <div class="brain-overlay" id="brainOverlay"></div>
      <div class="brain-panel" id="brainPanel">
        <div class="brain-title">脑区状态</div>
        <div id="brainList"></div>
      </div>
```

- [ ] **Step 3: Verify DOM nodes exist**

Run: `cd /Users/lilei/tools/ai-bot/ui/main && npm start`
Expected: Chat 窗口正常，面板不可见（没有 `visible` class），不影响布局

- [ ] **Step 4: Commit**

```bash
cd /Users/lilei/tools/ai-bot
git add ui/main/windows/assistant/chat.html
git commit -m "feat(chat): 脑区侧滑面板 DOM 节点"
```

---

### Task 5: 前端 — chat.html JS 逻辑（面板渲染 + 交互）

**Files:**
- Modify: `ui/main/windows/assistant/chat.html`（`<script>` 块内，`loadStats()` 函数附近）

- [ ] **Step 1: Add brain region state variables**

在 `ui/main/windows/assistant/chat.html` 的 `<script>` 块内，`loadStats()` 函数之前（约 L1142），添加全局变量和常量：

```javascript
    // ========== 脑区状态侧滑面板 ==========
    const BRAIN_STATES = [
      { activation: 0.0, dot: '⚫', group: 2 },
      { activation: 1.0, dot: '🟢', group: 0 },
      { activation: 0.5, dot: '🟡', group: 1 },
    ];
    let _brainRegions = [];           // 缓存后端返回的脑区状态
    let _pendingBrainChanges = {};   // { label: stateIdx } 用户待提交的改动

    const brainPanel = document.getElementById('brainPanel');
    const brainOverlay = document.getElementById('brainOverlay');
    const brainTriggerZone = document.getElementById('brainTriggerZone');
    const brainList = document.getElementById('brainList');
```

- [ ] **Step 2: Add renderBrainList() function**

在上述变量之后，添加：

```javascript
    function renderBrainList() {
      if (!_brainRegions || _brainRegions.length === 0) {
        brainList.innerHTML = '<div class="brain-empty">暂无脑区数据</div>';
        return;
      }

      // Build display list: use pending changes if any, else use cached state
      const displayList = _brainRegions.map(r => {
        const label = r.label || r.name;
        let stateIdx;
        if (label in _pendingBrainChanges) {
          stateIdx = _pendingBrainChanges[label];
        } else {
          // Map activation to state index
          if (r.activation > 0.7) stateIdx = 1;       // green
          else if (r.activation > 0.3) stateIdx = 2;   // yellow
          else stateIdx = 0;                            // black
        }
        return { label, stateIdx };
      });

      // Sort by group: green(0) → yellow(1) → black(2)
      displayList.sort((a, b) => BRAIN_STATES[a.stateIdx].group - BRAIN_STATES[b.stateIdx].group);

      // Render
      let html = '';
      let lastGroup = -1;
      const groupLabels = ['🟢 点亮', '🟡 调暗', '⚫ 关闭'];
      for (const item of displayList) {
        const s = BRAIN_STATES[item.stateIdx];
        if (s.group !== lastGroup) {
          html += `<div class="brain-group-label">${groupLabels[s.group]}</div>`;
          lastGroup = s.group;
        }
        const cls = s.group === 2 ? 'brain-item off' : 'brain-item';
        html += `<div class="${cls}" data-label="${item.label}">
          <span class="dot">${s.dot}</span>
          <span class="name">${item.label}</span>
        </div>`;
      }
      brainList.innerHTML = html;

      // Attach click handlers
      brainList.querySelectorAll('.brain-item').forEach(el => {
        el.addEventListener('click', (e) => {
          e.stopPropagation();
          const label = el.dataset.label;
          // Find current state in displayList
          const item = displayList.find(d => d.label === label);
          if (!item) return;
          // Cycle: 0(black) → 1(green) → 2(yellow) → 0(black)
          const nextIdx = (item.stateIdx + 1) % 3;
          _pendingBrainChanges[label] = nextIdx;
          renderBrainList();
        });
      });
    }
```

- [ ] **Step 3: Add show/hide panel functions**

```javascript
    function showBrainPanel() {
      brainPanel.classList.add('visible');
      brainOverlay.classList.add('visible');
      renderBrainList();
    }

    function hideBrainPanel() {
      brainPanel.classList.remove('visible');
      brainOverlay.classList.remove('visible');
    }

    // Trigger: mouse enters right edge of messages area
    brainTriggerZone.addEventListener('mouseenter', showBrainPanel);
    // Hide: mouse leaves messages area
    document.getElementById('messages').addEventListener('mouseleave', hideBrainPanel);
    // Click overlay to hide
    brainOverlay.addEventListener('click', hideBrainPanel);
```

- [ ] **Step 4: Add fetchBrainRegions() function**

```javascript
    async function fetchBrainRegions() {
      try {
        const resp = await fetch('http://127.0.0.1:9876/api/brain/regions?include_dark=true');
        if (!resp.ok) return;
        const data = await resp.json();
        if (data.status === 'ok' && data.regions) {
          _brainRegions = data.regions;
        }
      } catch (e) {
        console.error('加载脑区状态失败:', e);
      }
    }
```

- [ ] **Step 5: Integrate fetchBrainRegions into loadStats()**

在 `loadStats()` 函数末尾（`catch` 块之前），追加脑区数据拉取：

```javascript
      // 并行拉取脑区状态（不阻塞 stats 渲染）
      fetchBrainRegions();
```

精确位置：在 `loadStats()` 的 `if (result)` 块结束后、`} catch (e)` 之前。

- [ ] **Step 6: Add submitBrainChanges() function**

```javascript
    async function submitBrainChanges() {
      const labels = Object.keys(_pendingBrainChanges);
      if (labels.length === 0) return;

      const regions = labels.map(label => ({
        label,
        activation: BRAIN_STATES[_pendingBrainChanges[label]].activation,
      }));

      try {
        const resp = await fetch('http://127.0.0.1:9876/api/brain/regions/update', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ regions }),
        });
        if (resp.ok) {
          _pendingBrainChanges = {};
        } else {
          console.error('提交脑区状态失败:', resp.status);
        }
      } catch (e) {
        console.error('提交脑区状态失败:', e);
      }
    }
```

- [ ] **Step 7: Integrate submitBrainChanges into sendMessage()**

在 `sendMessage()` 函数中，在 `const result = await sendMessageWithRetry(text);`（约 L860）之前，插入：

```javascript
      // 提交用户手动修改的脑区状态（在发送消息前）
      await submitBrainChanges();
```

精确位置：在 `showTyping();` 和 `window.electronAPI.notifyBusy(true, 'chat');` 之后、`try {` 块内 `const result = await sendMessageWithRetry(text);` 之前。

- [ ] **Step 8: Verify panel works**

Run: `cd /Users/lilei/tools/ai-bot/ui/main && npm start`
Expected:
1. 鼠标移到消息区右侧边缘 → 面板滑出
2. 脑区列表按 🟢→🟡→⚫ 排序
3. 单击脑区项 → 状态循环切换，列表重排序
4. 鼠标移出 → 面板收起
5. 发送消息时控制台无报错

- [ ] **Step 9: Commit**

```bash
cd /Users/lilei/tools/ai-bot
git add ui/main/windows/assistant/chat.html
git commit -m "feat(chat): 脑区侧滑面板 JS 逻辑（渲染/交互/拉取/提交）"
```

---

### Task 6: 验收测试

**Files:**
- 无新文件

- [ ] **Step 1: Run backend tests**

Run: `cd /Users/lilei/tools/ai-bot && python -m pytest tests/test_brain_region_set_activation.py tests/test_brain_region_api.py -v`
Expected: 8 PASSED

- [ ] **Step 2: Run ruff on modified Python files**

Run: `cd /Users/lilei/tools/ai-bot && ruff check niu_api/internal/region_activation.py niu_api/brain_region_api.py`
Expected: All checks passed

- [ ] **Step 3: Full integration smoke test**

Run: `cd /Users/lilei/tools/ai-bot/ui/main && npm start`

手动验证：
1. 启动 Agent，进行一轮对话
2. 对话返回后（`chat_idle`），鼠标移到消息区右侧 → 面板滑出，显示脑区列表
3. 点击一个 🟢 脑区 → 变为 🟡，列表重排序
4. 再点击 → 变为 ⚫，移到关闭组
5. 发送一条消息 → 控制台无 `提交脑区状态失败` 报错
6. 下一轮对话 → Agent 向量检索使用更新后的脑区状态

- [ ] **Step 4: Final commit if any fixes needed**

```bash
cd /Users/lilei/tools/ai-bot
git add -A
git commit -m "test: 脑区侧滑面板验收通过"
```

---

## Self-Review Checklist

**Spec coverage:**
- [x] 触发方式（消息区右侧边缘）→ Task 5 Step 3
- [x] 覆盖浮层面板（不改变窗口尺寸）→ Task 3 CSS + Task 4 DOM
- [x] 三态排序（绿→黄→黑）→ Task 5 Step 2 `renderBrainList()`
- [x] 单击循环切换（⚫→🟢→🟡→⚫）→ Task 5 Step 2 click handler
- [x] 发送时提交 → Task 5 Step 7
- [x] 永久覆盖后端值 → Task 1 `set_activation()` + Task 2 POST 端点
- [x] 复用 loadStats 拉取链 → Task 5 Step 5
- [x] 便签风格 → Task 3 CSS（`#fff8e1` + 棉纸纹理）
- [x] 小字号（11px）→ Task 3 CSS
- [x] 继承页面字体 → CSS 不硬编码 font-family，继承 `html, body` 的动态注入
- [x] 边界情况（提交失败不阻塞）→ Task 5 Step 6 try/catch
- [x] 不涉及 MCP 工具/向量检索/图谱窗口修改 → Plan scope 限定

**Placeholder scan:** 无 TBD/TODO，所有步骤都有完整代码。

**Type consistency:** `set_activation(region_label, activation)` 签名在 Task 1 定义、Task 2 调用、Task 5 Step 6 通过 HTTP 间接触发，一致。`BRAIN_STATES` 数组在 Task 5 定义并使用，索引映射一致。
