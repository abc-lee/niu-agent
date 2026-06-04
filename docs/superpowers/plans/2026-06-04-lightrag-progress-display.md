# LightRAG 入库进度显示 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在前端小女孩窗口中用图形化动画展示 LightRAG 后台异步入库进度，替换现有的"完成 ✨"文字气泡和不可见的"⏳"状态提示。

**Architecture:** 后端新增 `/api/kg/pipeline_status` 端点读取 LightRAG 共享存储中的 pipeline_status 数据；前端 spirit.html 通过 HTTP 轮询获取进度，用 SVG 圆弧进度指示器图形化展示百分比；完成后进度指示器自动消失。不涉及状态机变更，不使用文字气泡。

**分阶段策略：** 第一版先让进度指示器始终可见（显示 0% 或当前进度），方便测试位置、大小、动画效果。用户确认视觉效果后，再改为"仅入库时显示"。

**Tech Stack:** Python/FastAPI (后端), HTML/CSS/SVG (前端进度动画), Electron IPC (可选)

---

## 文件结构

| 操作 | 文件 | 职责 |
|------|------|------|
| 修改 | `niu_api/kg_api.py` | 新增 `/api/kg/pipeline_status` 端点 |
| 修改 | `ui/assistant/spirit.html` | 替换"完成 ✨"气泡为 SVG 进度指示器，删除不可见 statusHint，添加轮询逻辑 |
| 修改 | `ui/assistant/preload.js` | 无需修改（直接 HTTP 轮询，不走 IPC） |

---

### Task 1: 后端 — 新增 pipeline_status API 端点

**Files:**
- Modify: `niu_api/kg_api.py:219-224` (在 `/api/kg/stats` 端点附近)

- [ ] **Step 1: 在 kg_api.py 中添加 `/api/kg/pipeline_status` 端点**

在 `graph_stats()` 函数之后添加：

```python
@router.get("/pipeline_status")
def pipeline_status():
    """Get LightRAG ingestion pipeline progress.

    Returns busy flag, current batch / total batches, progress percentage,
    and the latest pipeline message. Frontend polls this endpoint to show
    a graphical progress indicator in the spirit window.
    """
    from niu_api.internal.lightrag_manager import get_lightrag

    rag = get_lightrag()
    if rag is None:
        return {"busy": False, "progress": 0, "message": "LightRAG not available"}

    try:
        from lightrag.kg.shared_storage import get_namespace_data

        # pipeline_status is async; run in LightRAG's event loop
        from niu_api.internal.lightrag_manager import call_async

        ps = call_async(
            get_namespace_data("pipeline_status", workspace=rag.workspace),
            timeout=5,
        )
    except Exception as e:
        return {"busy": False, "progress": 0, "message": f"Error: {e}"}

    busy = bool(ps.get("busy", False))
    cur_batch = int(ps.get("cur_batch", 0))
    batchs = int(ps.get("batchs", 0))
    job_name = str(ps.get("job_name", ""))
    latest_message = str(ps.get("latest_message", ""))

    progress = int(cur_batch / batchs * 100) if batchs > 0 else 0

    return {
        "busy": busy,
        "progress": progress,
        "cur_batch": cur_batch,
        "batchs": batchs,
        "job_name": job_name,
        "message": latest_message,
    }
```

- [ ] **Step 2: 验证端点可用**

启动 API 服务后，用 curl 测试：
```bash
curl http://127.0.0.1:9876/api/kg/pipeline_status
```
预期返回：`{"busy": false, "progress": 0, "cur_batch": 0, "batchs": 0, "job_name": "", "message": ""}`

- [ ] **Step 3: 提交**

```bash
git add niu_api/kg_api.py
git commit -m "feat: add /api/kg/pipeline_status endpoint for ingestion progress"
```

---

### Task 2: 前端 — 替换"完成 ✨"气泡为 SVG 进度指示器

**Files:**
- Modify: `ui/assistant/spirit.html`

这是核心任务。需要：
1. 删除 `#complete-bubble` HTML 元素和相关 CSS
2. 删除 `#status-hint` HTML 元素和相关 CSS（不可见，无价值）
3. 添加 SVG 圆弧进度指示器
4. 添加轮询逻辑
5. 修改 `showCompleteBubble()` 和 `showStatusHint()` 调用点

- [ ] **Step 2.1: 删除 #complete-bubble 和 #status-hint 的 HTML 元素**

删除第 168 行 `<div id="status-hint"></div>` 和第 170 行 `<div id="complete-bubble">完成 ✨</div>`。

替换为 SVG 进度指示器：

```html
<!-- 入库进度指示器（测试阶段始终可见，确认效果后改为入库时显示） -->
<svg id="progress-ring" width="36" height="36" viewBox="0 0 36 36"
     style="position:absolute; bottom:4px; left:50%; transform:translateX(-50%);
            pointer-events:none; z-index:100;">
  <circle cx="18" cy="18" r="15" fill="none" stroke="rgba(102,126,234,0.2)" stroke-width="3"/>
  <circle id="progress-arc" cx="18" cy="18" r="15" fill="none"
          stroke="rgba(102,126,234,0.9)" stroke-width="3" stroke-linecap="round"
          stroke-dasharray="94.25" stroke-dashoffset="94.25"
          transform="rotate(-90 18 18)"/>
  <text id="progress-text" x="18" y="19" text-anchor="middle" dominant-baseline="central"
        fill="white" font-size="9" font-family="system-ui, sans-serif" font-weight="bold">0%</text>
</svg>
```

- [ ] **Step 2.2: 删除 #complete-bubble 和 #status-hint 的 CSS**

删除第 71-81 行 `#status-hint` CSS 和第 84-105 行 `#complete-bubble` CSS + `@keyframes bubble-pop`。

替换为进度指示器动画 CSS：

```css
/* 入库进度指示器 */
#progress-ring {
  transition: opacity 0.3s ease;
}
/* 测试阶段：始终可见，不使用 .visible 控制显示隐藏 */
#progress-arc {
  transition: stroke-dashoffset 0.5s ease;
}
```

- [ ] **Step 2.3: 删除 JS 中对 statusHint 的引用**

删除第 213 行 `const statusHint = document.getElementById('status-hint');`。

在 `setState()` 函数中，删除所有 `statusHint.textContent = ...` 赋值（第 270, 275, 288, 302, 312, 319 行）。

- [ ] **Step 2.4: 替换 showCompleteBubble() 和 showStatusHint()**

删除 `showCompleteBubble()` 函数（第 608-614 行）和 `showStatusHint()` 函数（第 624-630 行）。

替换为进度指示器控制逻辑：

```javascript
// ========== 入库进度轮询 ==========
const PROGRESS_API = 'http://127.0.0.1:9876/api/kg/pipeline_status';
const PROGRESS_POLL_INTERVAL = 3000;  // 3秒轮询
let progressPollTimer = null;
let lastProgressBusy = false;

const progressRing = document.getElementById('progress-ring');
const progressArc = document.getElementById('progress-arc');
const progressText = document.getElementById('progress-text');
const CIRCUMFERENCE = 2 * Math.PI * 15;  // r=15, ≈94.25

function showProgressRing(percent) {
  if (!progressRing) return;
  const clamped = Math.max(0, Math.min(100, percent));
  const offset = CIRCUMFERENCE * (1 - clamped / 100);
  progressArc.setAttribute('stroke-dashoffset', offset);
  progressText.textContent = clamped + '%';
  // 测试阶段：始终可见，不需要 display 控制
  // 确认效果后改为：progressRing.style.display = 'block';
}

function hideProgressRing() {
  if (!progressRing) return;
  // 测试阶段：不隐藏，方便观察
  // 确认效果后改为：
  // progressRing.style.display = 'none';
}

async function pollPipelineStatus() {
  try {
    const resp = await fetch(PROGRESS_API);
    const data = await resp.json();

    if (data.busy) {
      showProgressRing(data.progress);
      lastProgressBusy = true;
    } else {
      // 测试阶段：空闲时也显示 0%，方便确认位置
      showProgressRing(0);
      if (lastProgressBusy) {
        hideProgressRing();
        lastProgressBusy = false;
      }
    }
  } catch (e) {
    console.debug('[Progress] poll failed:', e.message);
  }
}

function startProgressPolling() {
  if (progressPollTimer) return;
  // 测试阶段：立即显示进度指示器
  showProgressRing(0);
  pollPipelineStatus();
  progressPollTimer = setInterval(pollPipelineStatus, PROGRESS_POLL_INTERVAL);
}

function stopProgressPolling() {
  if (progressPollTimer) {
    clearInterval(progressPollTimer);
    progressPollTimer = null;
  }
}
```

- [ ] **Step 2.5: 修改 handleDroppedFiles() 中的完成/失败处理**

删除 `showCompleteBubble()` 调用（不再需要"完成 ✨"气泡）。但**保留错误反馈**：

```javascript
      // 收到响应后减少忙碌计数
      decrementBusy('file-drop-response');

      // 不再显示"完成"气泡 — 入库进度由轮询自动展示
      // 但保留错误反馈：失败时在进度环上显示红色感叹号
      if (result && result.error) {
        showProgressError();
      }
```

新增 `showProgressError()` 函数（在 Step 2.4 的轮询代码块末尾添加）：

```javascript
function showProgressError() {
  if (!progressText || !progressArc) return;
  progressText.textContent = '!';
  progressArc.setAttribute('stroke', 'rgba(231, 76, 60, 0.9)');  // 红色
  setTimeout(() => {
    progressArc.setAttribute('stroke', 'rgba(102,126,234,0.9)');  // 恢复原色
    progressText.textContent = '0%';
    showProgressRing(0);
  }, 5000);
}
```

删除原来 `if (result && result.reply) { showCompleteBubble(); } else ...` 中 `showCompleteBubble()` 的两个调用，替换为上面的逻辑。

- [ ] **Step 2.6: 在初始化部分启动轮询，窗口关闭时停止轮询**

在文件末尾 `window.electronAPI.onUserActivity(...)` 之后添加：

```javascript
// 启动入库进度轮询
startProgressPolling();

// 窗口关闭时停止轮询，避免后台空转
window.addEventListener('beforeunload', stopProgressPolling);
```

- [ ] **Step 2.7: 验证前端效果**

1. 启动应用，确认小女孩正常显示
2. 拖入文件，确认不再出现"完成 ✨"文字
3. 确认入库时底部出现圆弧进度指示器，显示百分比
4. 确认入库完成后进度指示器自动消失

- [ ] **Step 2.8: 提交**

```bash
git add ui/assistant/spirit.html
git commit -m "feat: replace complete bubble with SVG progress ring for ingestion"
```

---

### Task 3: 清理 — 删除废弃的 CSS 和 JS 引用

**Files:**
- Modify: `ui/assistant/spirit.html`

- [ ] **Step 3.1: 确认所有 statusHint 引用已删除**

搜索 spirit.html 中所有 `statusHint` 引用，确保全部删除：
- `const statusHint = ...` — 已在 Step 2.3 删除
- `statusHint.textContent = ...` — 已在 Step 2.3 删除
- `showStatusHint(...)` — 已在 Step 2.4 删除

- [ ] **Step 3.2: 确认所有 complete-bubble 引用已删除**

搜索 spirit.html 中所有 `complete-bubble` / `showCompleteBubble` 引用，确保全部删除。

- [ ] **Step 3.3: 提交（如有改动）**

```bash
git add ui/assistant/spirit.html
git commit -m "chore: clean up obsolete status-hint and complete-bubble references"
```

---

### Task 4: 切换到生产模式 — 进度指示器仅入库时显示

**Files:**
- Modify: `ui/assistant/spirit.html`

用户确认视觉效果后执行此 Task，将测试模式的"始终可见"切换为"仅入库时显示"。

- [ ] **Step 4.1: 修改 SVG 元素 — 默认隐藏**

将 SVG 的 style 中移除初始可见性，改为 `display:none`：

```html
<!-- 入库进度指示器 -->
<svg id="progress-ring" width="36" height="36" viewBox="0 0 36 36"
     style="position:absolute; bottom:4px; left:50%; transform:translateX(-50%);
            display:none; pointer-events:none; z-index:100;">
```

- [ ] **Step 4.2: 修改 CSS — 恢复动画类**

替换 CSS 为带 `.visible` 控制的版本：

```css
/* 入库进度指示器 */
#progress-ring {
  transition: opacity 0.3s ease;
}
#progress-ring.visible {
  display: block;
  animation: progress-fade-in 0.3s ease;
}
@keyframes progress-fade-in {
  from { opacity: 0; transform: translateX(-50%) scale(0.8); }
  to   { opacity: 1; transform: translateX(-50%) scale(1); }
}
#progress-arc {
  transition: stroke-dashoffset 0.5s ease;
}
```

- [ ] **Step 4.3: 修改 JS — 恢复显示/隐藏逻辑**

```javascript
function showProgressRing(percent) {
  if (!progressRing) return;
  const clamped = Math.max(0, Math.min(100, percent));
  const offset = CIRCUMFERENCE * (1 - clamped / 100);
  progressArc.setAttribute('stroke-dashoffset', offset);
  progressText.textContent = clamped + '%';
  progressRing.classList.add('visible');
}

function hideProgressRing() {
  if (!progressRing) return;
  progressRing.classList.remove('visible');
  setTimeout(() => {
    if (!progressRing.classList.contains('visible')) {
      progressRing.style.display = 'none';
    }
  }, 300);
}
```

修改 `pollPipelineStatus()` — 空闲时不显示进度，从忙碌变空闲时隐藏：

```javascript
async function pollPipelineStatus() {
  try {
    const resp = await fetch(PROGRESS_API);
    const data = await resp.json();

    if (data.busy) {
      showProgressRing(data.progress);
      lastProgressBusy = true;
    } else if (lastProgressBusy) {
      hideProgressRing();
      lastProgressBusy = false;
    }
  } catch (e) {
    console.debug('[Progress] poll failed:', e.message);
  }
}
```

修改 `startProgressPolling()` — 不再初始显示：

```javascript
function startProgressPolling() {
  if (progressPollTimer) return;
  pollPipelineStatus();
  progressPollTimer = setInterval(pollPipelineStatus, PROGRESS_POLL_INTERVAL);
}
```

- [ ] **Step 4.4: 提交**

```bash
git add ui/assistant/spirit.html
git commit -m "feat: progress ring only visible during ingestion (production mode)"
```

---

## 自查清单

1. **Spec 覆盖**：
   - 去掉"完成 ✨"气泡 → Task 2.1, 2.4, 2.5
   - 去掉不可见"⏳" → Task 2.2, 2.3
   - 图形化动画展示进度百分比 → Task 2.1 (SVG 圆弧), 2.4 (轮询+更新)
   - 完成后进度指示器消失 → Task 4 (生产模式 hideProgressRing)
   - 不涉及状态机变更 → 确认：未修改 State/BUSY 逻辑
   - 错误反馈保留 → Task 2.5 (showProgressError)

2. **Placeholder 扫描**：无 TBD/TODO/占位符

3. **类型一致性**：
   - 后端返回 `progress: int` (0-100)
   - 前端 `showProgressRing(percent)` 接收 int
   - SVG `stroke-dashoffset` 计算使用 `CIRCUMFERENCE * (1 - percent/100)` — 类型一致

4. **审查修复**：
   - 错误反馈丢失 → Task 2.5 增加 showProgressError()
   - 轮询未停止 → Task 2.6 增加 beforeunload 监听
   - 测试→生产过渡 → Task 4 记录完整代码改动
   - CORS 无问题 → __main__.py 已配置 allow_origins=["*"]
   - kg_router 已注册 → __main__.py 确认 include_router(kg_router)
   - call_async timeout 参数 → 签名 (coro, timeout: int = 120)，timeout=5 可用
