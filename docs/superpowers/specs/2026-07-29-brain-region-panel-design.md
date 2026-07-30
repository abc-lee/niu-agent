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
| 滑出 | 鼠标进入消息区（`.messages`）右侧 12px 边缘热区 |
| 收起 | 鼠标离开消息区；或点击半透明遮罩 |
| 滑出动画 | `transform: translateX(100%) → translateX(0)`，0.3s ease |
| 收起动画 | 反向，同样 0.3s ease |

**触发区域限定在消息区**，不包含输入栏和状态栏。

### 2.2 面板布局

- 宽度：175px
- 定位：`position: absolute; right: 0; top: 0; bottom: 0`（相对于 `.messages` 容器）
- 背景：`#fff8e1` 暖黄底 + 棉纸纹理 SVG（与 chat.html 便签风格一致）
- 边框：左侧 2px 虚线 `rgba(180,150,50,0.3)`
- 字号：11px（比对话文字 13px 小）
- 字体：继承页面 `FONT_FAMILY` 变量（由 preload-chat.js 动态注入，不硬编码）
- 滚动：`overflow-y: auto`

### 2.3 脑区列表渲染

- 按 activation 值分三组排序：
  - 🟢 点亮组（activation > 0.7）
  - 🟡 调暗组（0.3 < activation ≤ 0.7）
  - ⚫ 关闭组（activation ≤ 0.3）
- 每组上方显示分组标签（小字 9px，大写）
- 每个脑区项：状态圆点（emoji）+ 脑区名称，名称超长用 `text-overflow: ellipsis` 截断
- 关闭组项 `opacity: 0.45` 降低视觉权重
- hover 高亮：`rgba(231,202,74,0.15)`

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

```
① 鼠标进入消息区右侧边缘 → 面板滑出
② 前端调 GET /api/brain/regions → 渲染排序后的脑区列表
③ 用户单击脑区 → 前端本地循环切换状态（即时视觉反馈，缓存改动）
④ 用户点"发送" → POST /api/brain/regions/update 批量提交改动
⑤ 后端 RegionActivationManager.set_activation() 更新值（永久覆盖）
⑥ Agent 本轮 _inject_dynamic_resources() 使用更新后的脑区状态做向量检索
⑦ 后续轮次：手动设的值受自然衰减（×0.92/轮）影响，与其他脑区一致
```

### 3.1 脑区状态刷新时机

脑区状态数据通过现有 `loadStats()` 拉取链获取，**不新增 SSE 事件类型**：

- `loadStats()` 在以下时机触发：
  - Agent 空闲时（`chat_idle` → `onSpiritState('idle')` → `loadStats()`）
  - 每次工具调用后（`onToolStatus` → `loadStats()`）
- 在 `loadStats()` 中**并行追加** `GET /api/brain/regions` 调用
  - 不合并到 `/api/stats` 响应中——统计数据和脑区数据解耦
  - 面板关闭时仍拉取但开销可控（脑区数量有限，JSON 响应小）
  - 缓存到前端变量，面板打开时直接渲染，无需等待网络

### 3.2 手动改动提交

- 用户在面板中的状态改动缓存到前端 `pendingBrainChanges` 对象
- 点击"发送"时，在 `sendMessage()` 流程中先调 `POST /api/brain/regions/update` 提交改动
- 提交成功后清空 `pendingBrainChanges`
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
- 更新 `updated_at` 时间戳

### 4.3 RegionActivationManager 新增方法

```python
def set_activation(self, region_label: str, activation: float) -> None:
    """Set region activation to an arbitrary value.
    
    Unlike manual_activate (1.0) and manual_dim (0.0), this supports
    the 'dimming' state (0.5) for the three-state UI toggle.
    """
    with self._lock:
        if region_label in self._regions:
            state = self._regions[region_label]
            state.activation = activation
            state.manually_dimmed = (activation == 0.0)
            state.updated_at = datetime.now()
```

## 5. 前端实现

### 5.1 chat.html — CSS（内联 `<style>` 块内追加）

新增样式类：
- `.brain-trigger-zone` — 右侧 12px 不可见热区
- `.brain-overlay` — 半透明遮罩
- `.brain-panel` — 侧滑面板容器
- `.brain-title` — 面板标题
- `.brain-group-label` — 分组标签
- `.brain-item` — 脑区列表项

### 5.2 chat.html — DOM（`.messages` 容器内追加）

```html
<div class="brain-trigger-zone" id="brainTriggerZone"></div>
<div class="brain-overlay" id="brainOverlay"></div>
<div class="brain-panel" id="brainPanel">
  <div class="brain-title">脑区状态</div>
  <div id="brainList"></div>
</div>
```

### 5.3 chat.html — JS（内联 `<script>` 块内追加）

核心函数：
- `renderBrainList(regions)` — 排序 + 渲染脑区列表
- `showBrainPanel()` / `hideBrainPanel()` — 滑出/收起
- `cycleBrainState(label)` — 单击循环切换，更新 `pendingBrainChanges`
- `submitBrainChanges()` — 发送时提交改动到后端

集成点：
- `loadStats()` 末尾追加 `fetch('/api/brain/regions?include_dark=true')` → 缓存到 `window._brainRegions`
- `sendMessage()` 开头追加 `await submitBrainChanges()`

### 5.4 preload-chat.js

不需要新增 IPC 桥接——脑区数据通过 `fetch('/api/brain/regions')` 直接调用（与 chat.html 中已有的 `fetch('/api/stop_all')` 等模式一致）。

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
| 面板打开时 Agent 正在运行 | 仍可查看和修改，改动在下次发送时提交 |
| 用户修改后不发送直接关闭面板 | 改动丢失（`pendingBrainChanges` 不持久化），下次打开显示后端真实状态 |
| 脑区列表为空 | 显示"暂无脑区数据" |
| `GET /api/brain/regions` 失败 | 面板显示"加载失败"，不阻塞聊天功能 |
| `POST /api/brain/regions/update` 失败 | console.error 记录，不阻塞消息发送 |
| 鼠标快速进出触发区 | CSS transition 自然处理，不做防抖 |

## 8. 不涉及的范围

- 不修改脑区 MCP 工具（`brain_region_activate` / `brain_region_dim` / `brain_region_status`）——这些是给 LLM Agent 用的
- 不修改脑区 activation 的衰减逻辑（`decay_factor=0.92`）——手动设的值与自动激活的值一样受衰减
- 不修改向量检索逻辑——脑区状态通过现有的 `search_within_region()` 机制影响检索，本功能只改变 activation 值
- 不新增 SSE 事件类型——复用 `loadStats()` 拉取链
- 不修改图谱窗口的脑区显示
