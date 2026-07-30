# Chat 页面脑区状态侧滑面板 — 设计文档

> 日期：2026-07-29
> 状态：待实现

## 1. 目标

在 Chat 窗口（`ui/main/windows/assistant/chat.html`）的消息区右侧增加一个脑区状态侧滑面板，让用户：
- 鼠标靠近消息区右侧边缘时，面板从右向左滑出
- 查看当前所有脑区的激活状态（🟢点亮 / 🟡调暗 / ⚫关闭），按状态排序（绿→黄→黑），支持滚动
- 单击脑区项循环切换状态（⚫→🟢→🟡→⚫）
- 发送消息时，将手动修改的状态提交到后端，**永久覆盖**后端计算出的 activation 值，影响下一轮向量检索

## 2. 交互设计

### 2.1 触发与收起

| 行为 | 触发条件 |
|------|---------|
| 滑出 | 鼠标进入消息区（`.messages`）右侧 24px 边缘热区 |
| 收起 | 鼠标离开面板+触发区（200ms 延迟）；或点击半透明遮罩 |
| 滑出动画 | `transform: translateX(100%) → translateX(0)`，0.3s ease |
| 收起动画 | 反向，同样 0.3s ease |

**触发区域限定在消息区**，不包含输入栏和状态栏。

### 2.2 面板布局

- 宽度：175px
- 定位：DOM 在 `.container` 内 `.messages` 之后（不在 `.messages` 内，避免 `innerHTML=''` 销毁），JS 动态计算 `top`/`height` 匹配 `.messages` 边界
- 背景：`#fff8e1` 暖黄底 + 棉纸纹理 SVG（与 chat.html 便签风格一致）
- 边框：左侧 2px 虚线 `rgba(180,150,50,0.3)`
- 字号：11px（比对话文字 13px 小）
- 字体：继承页面 `FONT_FAMILY` 变量（由 preload-chat.js 动态注入，不硬编码）
- 滚动：`overflow-y: auto`
- z-index：200（高于 resize-handle 的 100）

### 2.3 脑区列表渲染

- 按 activation 值分三组排序：
  - 🟢 点亮组（activation > 0.7）
  - 🟡 调暗组（0.3 < activation ≤ 0.7）
  - ⚫ 关闭组（activation ≤ 0.3）
- 每组上方显示分组标签（小字 9px，大写）
- 每个脑区项：状态圆点（emoji）+ 脑区名称，名称超长用 `text-overflow: ellipsis` 截断
- 关闭组项 `opacity: 0.45` 降低视觉权重
- hover 高亮：`rgba(231,202,74,0.15)`
- 脑区名称通过 `escapeHtml()` 转义（复用 chat.html 已有函数）

### 2.4 遮罩

- 遮罩覆盖消息区除面板外的部分：`left: 0; right: 175px`
- 颜色：`rgba(120,80,20,0.15)`（暖色调，与便签风格一致，非纯黑）
- 点击遮罩收起面板

### 2.5 状态切换

单击脑区项循环切换：

```
⚫(0.0) → 🟢(1.0) → 🟡(0.5) → ⚫(0.0) → ...
```

- 切换在前端即时完成（视觉反馈 + 本地状态缓存）
- **不立即提交后端**，只在用户发送消息时批量提交
- 发送前用户可反复调整

## 3. 数据流

### 3.1 总体架构

复用现有 SSE 通道实现脑区状态实时同步，不新增通信端口。数据流分两条路径：

**初次拉取 + 手动提交**（IPC → main.js → HTTP loopback → FastAPI）：

chat.html 通过 `file://` 协议加载，不能直接 `fetch('/api/...')`（相对路径无 host，会 `ERR_FILE_NOT_FOUND`）。所有前端到 Python 的调用走 IPC → main.js `apiRequest` → HTTP loopback → FastAPI，与 `getStats`/`sendMessage`/`getHistory` 等 ~15 个现有 handler 完全一致。

```
chat.html → electronAPI.getBrainRegions() → ipcRenderer.invoke('brain-regions')
→ main.js apiRequest('GET', '/api/brain/regions?include_dark=true')
→ FastAPI brain_region_api.get_brain_regions()
→ RegionActivationManager.get_region_map()（进程内单例）
```

**实时更新**（SSE 推送，复用现有 `_sync_broadcast` + `/api/events/stream`）：

```
RegionActivationManager 状态变更（activate_regions/decay_all/set_activation 等）
→ notify_brain_region_sync('auto'|'manual', [changed_labels])
→ _sync_broadcast(event) → SSE /api/events/stream
→ main.js startMessageEventStream 解析 brain_region_updated 事件
→ chatWindow.webContents.send('brain-regions-changed')
→ preload onBrainRegionsChanged 回调
→ chat.html fetchBrainRegions()（仅面板打开时刷新 DOM）
```

### 3.2 脑区状态刷新时机

**SSE 实时推送**（新增）：RegionActivationManager 的 6 个状态变更方法末尾调用 `notify_brain_region_sync()`：
- `activate_regions()` — 向量检索命中实体时（auto）
- `reinforce_by_tool_use()` — 工具调用强化时（auto）
- `decay_all()` — 每轮对话结束衰减时（auto，全量刷新）
- `manual_activate()` — Agent 调 brain_region_activate 工具时（manual）
- `manual_dim()` — Agent 调 brain_region_dim 工具时（manual）
- `set_activation()` — UI 面板手动提交时（manual）

事件格式：`{"type": "brain_region_updated", "source": "auto|manual", "changed_labels": [...]}`

轻量通知事件（不携带完整数据），与 `ingest-completed` 事件模式一致——前端收到后自行拉取完整快照。

**前端拉取**（复用现有）：
- `loadStats()` 中的 `fetchBrainRegions()` 仍保留（通过 IPC 走 `brain-regions` handler），作为兜底
- `showBrainPanel()` 中新增 `fetchBrainRegions()` 调用，确保面板打开时拉取最新快照
- SSE 事件触发的 `fetchBrainRegions()` 有防并发守卫（`_fetchingBrainRegions`）和面板可见性守卫（`brainPanel.classList.contains('visible')`）

### 3.3 手动改动提交

- 用户在面板中的状态改动缓存到前端 `pendingBrainChanges` 对象
- 点击"发送"时，在 `sendMessage()` 流程中先调 `electronAPI.updateBrainRegions(regions)` 提交改动（走 IPC → main.js → POST /api/brain/regions/update）
- 提交成功后清空 `pendingBrainChanges`
- `set_activation()` 末尾推 SSE 事件，实现"提交后自动刷新"闭环
- 如果提交失败，前端不阻塞消息发送，记录 console.error

## 4. 后端接口

### 4.1 已有接口（直接复用）

| 接口 | 用途 |
|------|------|
| `GET /api/brain/regions?include_dark=true` | 获取所有脑区状态（含关闭的），返回 name/label/activation/status_light/manually_dimmed |

### 4.2 新增接口

**`POST /api/brain/regions/update`**

批量更新脑区 activation 值。

```json
// Request
{
  "regions": [
    {"label": "聊天历史", "activation": 1.0},
    {"label": "文档库", "activation": 0.5},
    {"label": "知识体系", "activation": 0.0}
  ]
}

// Response
{
  "status": "ok",
  "updated": 3
}
```

- 调用 `RegionActivationManager.set_activation(label, value)` 逐个更新
- `manually_dimmed` 标记：activation=0.0 时设为 True，其他设为 False
- activation 字段有 `Field(ge=0.0, le=1.0)` 范围验证

### 4.3 RegionActivationManager 新增方法

```python
def set_activation(self, region_label: str, activation: float) -> bool:
    """Set region activation to an arbitrary value.
    
    Unlike manual_activate (1.0) and manual_dim (0.0), this supports
    the 'dimming' state (0.5) for the three-state UI toggle.
    """
    with self._lock:
        state = self.find_region_by_label(region_label)
        if state is None:
            logger.warning("set_activation: 未找到区域 '%s'", region_label)
            return False
        state.activation = activation
        state.manually_dimmed = (activation == 0.0)
        if activation > 0:
            state.last_activated_at = time.time()
            state.activation_count += 1
        logger.info("手动设置脑区 activation: %s = %.2f", region_label, activation)
    # 推送 SSE 事件
    try:
        from niu_api.chat import notify_brain_region_sync
        notify_brain_region_sync('manual', [region_label])
    except Exception:
        pass
    return True
```

### 4.4 SSE 推送函数（新增）

```python
# niu_api/chat.py — 复用 _sync_broadcast + _main_loop.call_soon_threadsafe 模式
def notify_brain_region_sync(source: str = 'auto', changed_labels: list[str] | None = None) -> None:
    """广播脑区状态变更事件到 /api/events/stream。"""
    if _main_loop is None:
        return
    event = {
        "type": "brain_region_updated",
        "source": source,
        "changed_labels": changed_labels or [],
    }
    try:
        _main_loop.call_soon_threadsafe(_sync_broadcast, event)
    except RuntimeError:
        pass
```

### 4.5 状态变更方法的锁外推送模式

所有 6 个方法中 SSE 推送必须在 `with self._lock` 块**外部**调用，避免在持有锁时执行 I/O。对于在锁内 return 的方法，需重构为先用局部变量捕获返回值、退出锁块后再推送。统一模式：

```python
# 通用模式：锁内修改状态 → 锁外推送 SSE
def some_method(self, ...) -> ReturnType:
    with self._lock:
        # ... 修改状态 ...
        result = ...  # 捕获返回值
    # 锁外推送（不在 with self._lock 块内）
    try:
        from niu_api.chat import notify_brain_region_sync
        notify_brain_region_sync('auto' or 'manual', [labels])
    except Exception:
        pass  # SSE 推送失败不影响核心逻辑
    return result
```

**需重构的 3 个方法**（当前在锁内 return，需改为先捕获返回值再锁外推送）：

- `activate_regions()` — `return activated_regions` 在锁内 → 改为 `result = activated_regions`，锁外推送 `('auto', [labels])`，return result
- `reinforce_by_tool_use()` — `return region_id` 在锁内 → 改为 `result = region_id`，锁外推送 `('auto', [state.label])`（仅 region_id 非 None 时），return result
- `manual_activate()` — `return activated` 在锁内 → 改为 `result = activated`，锁外推送 `('manual', region_labels)`，return result

**不需重构的 3 个方法**（锁块后已有自然出口）：

- `set_activation()` — 参见 §4.3 代码示例，锁外推送+return True
- `manual_dim()` — `with self._lock` 块结束后推送 `('manual', region_labels)`
- `decay_all()` — `with self._lock` 块结束后推送 `('auto')`（不传 labels，全量刷新）

## 5. 前端实现

### 5.1 chat.html — CSS

新增样式类：
- `.brain-trigger-zone` — 右侧 24px 不可见热区（默认 `pointer-events: none`，JS 启用）
- `.brain-overlay` — 半透明遮罩
- `.brain-panel` — 侧滑面板容器（z-index: 200）
- `.brain-title` — 面板标题
- `.brain-group-label` — 分组标签
- `.brain-item` — 脑区列表项
- `.brain-empty` — 空状态提示

### 5.2 chat.html — DOM

DOM 节点放在 `.container` 内 `.messages` 之后（不在 `.messages` 内，避免 `innerHTML=''` 销毁）：

```html
<div class="brain-trigger-zone" id="brainTriggerZone"></div>
<div class="brain-overlay" id="brainOverlay"></div>
<div class="brain-panel" id="brainPanel">
  <div class="brain-title">脑区状态</div>
  <div id="brainList"></div>
</div>
```

### 5.3 chat.html — JS

核心函数：
- `renderBrainList()` — 排序 + 渲染脑区列表（复用已有 `escapeHtml()`）
- `positionBrainElements()` — 动态计算 `.messages` 边界设置面板定位
- `showBrainPanel()` / `hideBrainPanel()` — 滑出/收起
- `fetchBrainRegions()` — 通过 `electronAPI.getBrainRegions()` IPC 拉取（防并发守卫）
- `submitBrainChanges()` — 通过 `electronAPI.updateBrainRegions()` IPC 提交

集成点（每个都是独立修改步骤，不可遗漏）：
- `showBrainPanel()` 函数体中 `renderBrainList()` 之前加 `fetchBrainRegions()` 调用，确保面板打开时拉取最新快照
- `loadStats()` 末尾追加 `fetchBrainRegions()`（兜底）
- `sendMessage()` 中 `sendMessageWithRetry` 之前追加 `await submitBrainChanges()`
- 注册 `onBrainRegionsChanged` 回调，收到 SSE 事件后调 `fetchBrainRegions()`

### 5.4 preload-chat.js

新增 IPC 桥接：
- `getBrainRegions` → `ipcRenderer.invoke('brain-regions')`
- `updateBrainRegions` → `ipcRenderer.invoke('brain-update', regions)`
- `onBrainRegionsChanged` → `ipcRenderer.on('brain-regions-changed', callback)`

### 5.5 main.js

新增 IPC handler（复用 `apiRequest` 模式）：
- `brain-regions` → `apiRequest('GET', '/api/brain/regions?include_dark=true')`
- `brain-update` → `apiRequest('POST', '/api/brain/regions/update', { regions })`

SSE 解析器新增 `brain_region_updated` 事件分支（与 `tool_status`/`compact_status` 并列）：
```javascript
} else if (event.type === 'brain_region_updated') {
  if (chatWindow && !chatWindow.isDestroyed()) {
    chatWindow.webContents.send('brain-regions-changed', event);
  }
}
```

## 6. 状态映射

| UI 状态 | activation 值 | 后端 status_light | manually_dimmed |
|---------|--------------|-------------------|-----------------|
| 🟢 点亮 | 1.0 | 🟢 | False |
| 🟡 调暗 | 0.5 | 🟡 | False |
| ⚫ 关闭 | 0.0 | ⚫ | True |

注意：`get_status_light()` 根据 activation 区间返回符号，但 UI 侧直接用前端缓存的 activation 值判断分组，不依赖后端的 status_light 字段。

## 7. 边界情况

| 场景 | 处理 |
|------|------|
| 面板打开时 Agent 正在运行 | SSE 推送实时刷新面板 |
| 用户修改后不发送直接关闭面板 | 改动丢失（`pendingBrainChanges` 不持久化），下次打开显示后端真实状态 |
| 脑区列表为空 | 显示"暂无脑区数据" |
| `GET /api/brain/regions` 失败 | 面板显示"暂无脑区数据"，不阻塞聊天功能 |
| `POST /api/brain/regions/update` 失败 | console.error 记录，不阻塞消息发送 |
| SSE 事件丢失 | `loadStats()` 兜底拉取，`showBrainPanel()` 打开时拉取最新 |
| 鼠标快速进出触发区 | mouseleave 200ms 延迟 + 取消机制 |

## 8. 不涉及的范围

- 不修改脑区 MCP 工具（`brain_region_activate` / `brain_region_dim` / `brain_region_status`）——这些是给 LLM Agent 用的
- 不修改脑区 activation 的衰减逻辑（`decay_factor=0.92`）——手动设的值与自动激活的值一样受衰减
- 不修改向量检索逻辑——脑区状态通过现有的 `search_within_region()` 机制影响检索，本功能只改变 activation 值
- 不新增 SSE 端点——复用现有 `/api/events/stream` + `_sync_broadcast`
- 不修改图谱窗口的脑区显示
