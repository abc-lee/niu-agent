# 聊天记录时间戳气泡 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 鼠标 hover 任意聊天消息时显示相对时间气泡（刚刚 / X分钟前 / HH:MM / 昨天 HH:MM / MM-DD HH:MM / YYYY-MM-DD HH:MM）。

**Architecture:** 后端 `/api/context/messages` 已返回 `created_at`（ISO 8601 字符串）。前端三层渲染函数（`addMessage` / `addMessageWithId` / `prependMessage`）透传 `createdAt` 参数，存入 `div.dataset.timestamp`。纯 CSS `:hover` 显示气泡，JS 定时器刷新"相对时间"文案。临时用户消息和系统消息无 `created_at` → 不显示气泡（契合 DB-替换设计）。

**Tech Stack:** 原生 JS + CSS（无新依赖），chat.html 单文件改动。

---

## 关键设计点

### 数据链路
```
后端 /api/context/messages
  → MessageResponse.created_at (ISO 8601 字符串, e.g. "2026-07-31T14:30:00")
  → main.js get-history IPC 原样透传 (已存在, 不改)
  → chat.html getHistory() 返回 msg.created_at (已存在, 不改)
  → addMessageWithId(role, content, id, createdAt) 新增参数
  → addMessage(role, text, images, skipAppend, createdAt) 新增参数
  → div.dataset.timestamp = createdAt
  → CSS :hover 显示 .timestamp-bubble
```

### 渲染入口覆盖（3 处调用点透传 createdAt）
| 路径 | 函数 | 改动 |
|------|------|------|
| `loadHistory` L1875 | `addMessageWithId(msg.role, msg.content, msg.id)` | 加 `msg.created_at` |
| `refreshFromDB` L1614, L1642 | `addMessageWithId(msg.role, msg.content, msg.id)` | 加 `msg.created_at` |
| 加载更多 L1841 | `prependMessage(msg.role, msg.content, msg.id, refNode)` | 加 `msg.created_at` |

### 不显示气泡的消息（无 created_at）
- `sendMessage` L956 临时用户消息：`addMessage('user', text)` — 不传 createdAt
- 系统消息：`addMessage('system', text)` — 不传 createdAt

注意：subagent_msg 的 DB 版本**会**显示气泡（Task 2 Step 2+4 透传 createdAt）。只有 subagent_msg 解析失败的降级路径 `addMessage('assistant', text, [], skipAppend, createdAt)` 会透传 createdAt（也显示气泡）。

这些消息 `dataset.timestamp` 为 undefined → CSS 气泡不显示。后端写 DB 后 SSE 触发 `refreshFromDB`，临时消息被 DB 版本（带 createdAt）替换，自动获得时间戳。

### 相对时间格式
```javascript
function formatRelativeTime(isoString) {
  const now = new Date();
  const t = new Date(isoString);
  const diffMs = now - t;
  const diffMin = Math.floor(diffMs / 60000);

  if (diffMin < 1) return '刚刚';
  if (diffMin < 60) return `${diffMin}分钟前`;

  const sameDay = now.toDateString() === t.toDateString();
  if (sameDay) {
    return t.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false });
  }

  const yesterday = new Date(now);
  yesterday.setDate(yesterday.getDate() - 1);
  const isYesterday = yesterday.toDateString() === t.toDateString();
  if (isYesterday) {
    return '昨天 ' + t.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false });
  }

  const sameYear = now.getFullYear() === t.getFullYear();
  if (sameYear) {
    const mm = String(t.getMonth() + 1).padStart(2, '0');
    const dd = String(t.getDate()).padStart(2, '0');
    const hh = String(t.getHours()).padStart(2, '0');
    const mi = String(t.getMinutes()).padStart(2, '0');
    return `${mm}-${dd} ${hh}:${mi}`;
  }

  const yyyy = t.getFullYear();
  const mm = String(t.getMonth() + 1).padStart(2, '0');
  const dd = String(t.getDate()).padStart(2, '0');
  const hh = String(t.getHours()).padStart(2, '0');
  const mi = String(t.getMinutes()).padStart(2, '0');
  return `${yyyy}-${mm}-${dd} ${hh}:${mi}`;
}
```

### 定时刷新
每 30 秒遍历 `.message[data-timestamp]` 更新气泡文案，保证"刚刚→X分钟前"实时性。

### CSS 气泡设计
- 气泡绝对定位于消息右上角（user 消息）或左上角（assistant 消息）
- 半透明深色背景，白色文字，小字号
- 默认 `opacity: 0`，`.message:hover` 时 `opacity: 1`
- `pointer-events: none` 避免干扰消息内链接/图片点击

---

## File Structure

**Modify:** `ui/main/windows/assistant/chat.html`（单文件，3 处函数 + CSS + 1 个定时器）

无新文件，无新依赖。

---

### Task 1: CSS 气泡样式 + JS 时间格式化函数

**Files:**
- Modify: `ui/main/windows/assistant/chat.html`（CSS 区块 + JS 区块）

- [ ] **Step 1: 添加 CSS 气泡样式**

在 `chat.html` 的 `<style>` 块中，找到 `.message` 样式定义（约 L140 附近，`.message` 选择器）。在 `.message` 规则之后插入：

```css
    /* 时间戳气泡：hover 消息时显示 */
    .message[data-timestamp] {
      position: relative;
    }
    .timestamp-bubble {
      position: absolute;
      top: 4px;
      right: 8px;
      background: rgba(0, 0, 0, 0.6);
      color: #fff;
      font-size: 11px;
      padding: 2px 6px;
      border-radius: 4px;
      opacity: 0;
      transition: opacity 0.15s;
      pointer-events: none;
      white-space: nowrap;
      z-index: 5;
    }
    .message:hover .timestamp-bubble {
      opacity: 1;
    }
    /* assistant 消息气泡放左上角（避免遮挡右侧操作） */
    .message.assistant .timestamp-bubble {
      right: auto;
      left: 8px;
    }
```

- [ ] **Step 2: 添加 formatRelativeTime 函数**

在 `chat.html` 的 `<script>` 块中，找到 `addMessage` 函数定义前（约 L1066，`// ========== 消息显示 ==========` 注释前）。插入：

```javascript
    // ========== 时间戳格式化 ==========
    function formatRelativeTime(isoString) {
      if (!isoString) return null;
      const now = new Date();
      const t = new Date(isoString);
      if (isNaN(t.getTime())) return null;

      const diffMs = now - t;
      const diffMin = Math.floor(diffMs / 60000);

      if (diffMin < 1) return '刚刚';
      if (diffMin < 60) return `${diffMin}分钟前`;

      const sameDay = now.toDateString() === t.toDateString();
      if (sameDay) {
        return t.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false });
      }

      const yesterday = new Date(now);
      yesterday.setDate(yesterday.getDate() - 1);
      const isYesterday = yesterday.toDateString() === t.toDateString();
      if (isYesterday) {
        return '昨天 ' + t.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false });
      }

      const sameYear = now.getFullYear() === t.getFullYear();
      const mm = String(t.getMonth() + 1).padStart(2, '0');
      const dd = String(t.getDate()).padStart(2, '0');
      const hh = String(t.getHours()).padStart(2, '0');
      const mi = String(t.getMinutes()).padStart(2, '0');
      if (sameYear) {
        return `${mm}-${dd} ${hh}:${mi}`;
      }
      return `${t.getFullYear()}-${mm}-${dd} ${hh}:${mi}`;
    }
```

- [ ] **Step 3: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add ui/main/windows/assistant/chat.html
git commit -m "feat(chat): 时间戳气泡 CSS 样式 + formatRelativeTime 函数"
```

---

### Task 2: addMessage + addSubagentMessage 加 createdAt 参数 + 气泡 DOM

**Files:**
- Modify: `ui/main/windows/assistant/chat.html`（`addMessage` 函数 L1067）

- [ ] **Step 1: 修改 addMessage 签名**

找到 `addMessage` 函数定义（约 L1067）：

```javascript
    function addMessage(role, text, images = [], skipAppend = false) {
```

改为：

```javascript
    function addMessage(role, text, images = [], skipAppend = false, createdAt = null) {
```

- [ ] **Step 2: 修改 subagent_msg 分流点透传 createdAt**

找到 `addMessage` 函数内 subagent_msg 分流点（约 L1071-1072）：

```javascript
      if (role === 'subagent_msg') {
        return addSubagentMessage(text, skipAppend);
      }
```

改为（透传 createdAt）：

```javascript
      if (role === 'subagent_msg') {
        return addSubagentMessage(text, skipAppend, createdAt);
      }
```

- [ ] **Step 3: 在 div 创建后设置 timestamp + 气泡 DOM**

找到 `addMessage` 函数内 `div.className` 行（约 L1075）：

```javascript
      const div = document.createElement('div');
      div.className = `message ${role}`;
```

改为：

```javascript
      const div = document.createElement('div');
      div.className = `message ${role}`;
      // 时间戳气泡：仅有 createdAt 的消息显示（DB 版本消息）
      if (createdAt) {
        div.dataset.timestamp = createdAt;
        const bubble = document.createElement('span');
        bubble.className = 'timestamp-bubble';
        bubble.textContent = formatRelativeTime(createdAt) || '';
        div.appendChild(bubble);
      }
```

- [ ] **Step 4: 修改 addSubagentMessage 接收并设置 createdAt**

找到 `addSubagentMessage` 函数定义（约 L1185）：

```javascript
    function addSubagentMessage(text, skipAppend = false) {
      // 解析格式：@目标 [发送者名] 内容
      const match = text.match(/^@(\S+)(?:\s+\[([^\]]+)\])?\s*(.*)$/s);
      if (!match) {
        // 解析失败，按普通 assistant 消息渲染降级
        return addMessage('assistant', text, [], skipAppend);
      }
      const target = match[1];
      const sender = match[2] || '';
      const content = match[3].trim();

      // 方向：sender → target（无 sender 时只显示 → target）
      const direction = sender ? `${sender} → ${target}` : `→ ${target}`;

      const div = document.createElement('div');
      div.className = 'message subagent-msg';
      div.innerHTML = `
        <div class="subagent-direction">${escapeHtml(direction)}</div>
        <div class="subagent-content">${escapeHtml(content)}</div>
      `;
      if (!skipAppend) {
        messages.appendChild(div);
        messages.scrollTop = messages.scrollHeight;
      }
      return div;
    }
```

改为（加 createdAt 参数 + 气泡 DOM + 降级路径透传）：

```javascript
    function addSubagentMessage(text, skipAppend = false, createdAt = null) {
      // 解析格式：@目标 [发送者名] 内容
      const match = text.match(/^@(\S+)(?:\s+\[([^\]]+)\])?\s*(.*)$/s);
      if (!match) {
        // 解析失败，按普通 assistant 消息渲染降级（透传 createdAt）
        return addMessage('assistant', text, [], skipAppend, createdAt);
      }
      const target = match[1];
      const sender = match[2] || '';
      const content = match[3].trim();

      // 方向：sender → target（无 sender 时只显示 → target）
      const direction = sender ? `${sender} → ${target}` : `→ ${target}`;

      const div = document.createElement('div');
      div.className = 'message subagent-msg';
      div.innerHTML = `
        <div class="subagent-direction">${escapeHtml(direction)}</div>
        <div class="subagent-content">${escapeHtml(content)}</div>
      `;
      // 时间戳气泡：subagent_msg 也是 DB 消息，需要显示时间戳
      if (createdAt) {
        div.dataset.timestamp = createdAt;
        const bubble = document.createElement('span');
        bubble.className = 'timestamp-bubble';
        bubble.textContent = formatRelativeTime(createdAt) || '';
        div.appendChild(bubble);
      }
      if (!skipAppend) {
        messages.appendChild(div);
        messages.scrollTop = messages.scrollHeight;
      }
      return div;
    }
```

- [ ] **Step 5: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add ui/main/windows/assistant/chat.html
git commit -m "feat(chat): addMessage + addSubagentMessage 支持 createdAt 生成时间戳气泡"
```


---

### Task 3: addMessageWithId + prependMessage 透传 createdAt

**Files:**
- Modify: `ui/main/windows/assistant/chat.html`（L1219, L1887）

- [ ] **Step 1: 修改 addMessageWithId 签名和调用**

找到 `addMessageWithId` 函数定义（约 L1219）：

```javascript
    function addMessageWithId(role, text, id, images = []) {
      // 检查是否已存在相同ID的消息，避免重复
      const existing = document.querySelector(`.message[data-id="${id}"]`);
      if (existing) {
        console.log('[Chat] 消息已存在，跳过重复:', id);
        return existing;
      }

      const div = addMessage(role, text, images);
      if (!div) return null;
      div.dataset.id = id;
      return div;
    }
```

改为：

```javascript
    function addMessageWithId(role, text, id, images = [], createdAt = null) {
      // 检查是否已存在相同ID的消息，避免重复
      const existing = document.querySelector(`.message[data-id="${id}"]`);
      if (existing) {
        console.log('[Chat] 消息已存在，跳过重复:', id);
        return existing;
      }

      const div = addMessage(role, text, images, false, createdAt);
      if (!div) return null;
      div.dataset.id = id;
      return div;
    }
```

- [ ] **Step 2: 修改 prependMessage 签名和调用**

找到 `prependMessage` 函数定义（约 L1887）：

```javascript
    // 在顶部插入消息（用于加载更多历史）
    function prependMessage(role, content, id, refNode = null) {
      // 去重：检查是否已存在相同ID的消息
      const existing = document.querySelector(`.message[data-id="${id}"]`);
      if (existing) return;

      // 复用 addMessage 的渲染逻辑（Markdown 图片由 marked.js 渲染）
      const div = addMessage(role, content, [], true);  // skipAppend=true: 不做appendChild/scrollTop
      if (!div) return;
      div.dataset.id = id;
      messages.insertBefore(div, refNode || messages.firstChild);
    }
```

改为：

```javascript
    // 在顶部插入消息（用于加载更多历史）
    function prependMessage(role, content, id, refNode = null, createdAt = null) {
      // 去重：检查是否已存在相同ID的消息
      const existing = document.querySelector(`.message[data-id="${id}"]`);
      if (existing) return;

      // 复用 addMessage 的渲染逻辑（Markdown 图片由 marked.js 渲染）
      const div = addMessage(role, content, [], true, createdAt);  // skipAppend=true: 不做appendChild/scrollTop
      if (!div) return;
      div.dataset.id = id;
      messages.insertBefore(div, refNode || messages.firstChild);
    }
```

- [ ] **Step 3: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add ui/main/windows/assistant/chat.html
git commit -m "feat(chat): addMessageWithId + prependMessage 透传 createdAt"
```

---

### Task 4: 调用点透传 msg.created_at

**Files:**
- Modify: `ui/main/windows/assistant/chat.html`（L1614, L1642, L1841, L1876）

- [ ] **Step 1: loadHistory 调用点**

找到 `loadHistory` 函数中的调用（约 L1875）：

```javascript
          history.forEach(msg => {
            addMessageWithId(msg.role, msg.content, msg.id);
          });
```

改为：

```javascript
          history.forEach(msg => {
            addMessageWithId(msg.role, msg.content, msg.id, [], msg.created_at);
          });
```

- [ ] **Step 2: refreshFromDB 第一个调用点（初始化重建）**

找到 `refreshFromDB` 函数中 `existingIds.size === 0` 分支的调用（约 L1613）：

```javascript
          msgs.forEach(msg => {
            addMessageWithId(msg.role, msg.content, msg.id);
          });
```

改为：

```javascript
          msgs.forEach(msg => {
            addMessageWithId(msg.role, msg.content, msg.id, [], msg.created_at);
          });
```

- [ ] **Step 3: refreshFromDB 第二个调用点（增量追加）**

找到 `refreshFromDB` 函数中增量追加的调用（约 L1642）：

```javascript
            addMessageWithId(msg.role, msg.content, msg.id);
```

改为：

```javascript
            addMessageWithId(msg.role, msg.content, msg.id, [], msg.created_at);
```

- [ ] **Step 4: 加载更多历史调用点**

找到滚动加载更多的调用（约 L1841）：

```javascript
            history.forEach(msg => {
              prependMessage(msg.role, msg.content, msg.id, refNode);
            });
```

改为：

```javascript
            history.forEach(msg => {
              prependMessage(msg.role, msg.content, msg.id, refNode, msg.created_at);
            });
```

- [ ] **Step 5: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add ui/main/windows/assistant/chat.html
git commit -m "feat(chat): 渲染调用点透传 msg.created_at 到气泡"
```

---

### Task 5: 定时刷新相对时间 + 运行环境验证

**Files:**
- Modify: `ui/main/windows/assistant/chat.html`（定时器 + 验证）

- [ ] **Step 1: 添加定时刷新器**

在 `loadHistory` 函数定义之后（约 L1884），插入：

```javascript
    // ========== 时间戳气泡定时刷新 ==========
    // 每 30 秒刷新已渲染消息的相对时间（刚刚 → X分钟前）
    setInterval(() => {
      document.querySelectorAll('.message[data-timestamp] .timestamp-bubble').forEach(bubble => {
        const msgDiv = bubble.parentElement;
        const ts = msgDiv.dataset.timestamp;
        if (ts) {
          const text = formatRelativeTime(ts);
          if (text) bubble.textContent = text;
        }
      });
    }, 30000);
```

- [ ] **Step 2: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add ui/main/windows/assistant/chat.html
git commit -m "feat(chat): 30秒定时刷新时间戳气泡相对时间"
```

- [ ] **Step 3: 运行环境验证**

```bash
cd /Users/lilei/tools/ai-bot && ./niu
```

验证项：
1. **历史消息气泡**：hover 历史消息 → 显示相对时间气泡
2. **临时消息无气泡**：发送消息瞬间 → hover 临时消息无气泡；DB 替换后 → 有气泡
3. **气泡位置**：user 消息气泡在右上，assistant 消息气泡在左上
4. **气泡不干扰交互**：hover 消息内链接/图片 → 正常点击
5. **定时刷新**：等待 1 分钟 → "刚刚"变为"1分钟前"

---

## Self-Review

### 1. Spec coverage
- ✅ 鼠标 hover 显示时间戳气泡 → Task 1 CSS + Task 2 DOM
- ✅ 相对时间格式（刚刚/X分钟前/HH:MM/昨天/MM-DD/YYYY-MM-DD）→ Task 1 formatRelativeTime
- ✅ 临时用户消息不显示气泡 → Task 2 `if (createdAt)` 守卫 + sendMessage 不传 createdAt
- ✅ 系统消息不显示气泡 → addSystemMessage → addMessage 不传 createdAt
- ✅ subagent_msg 显示气泡 → Task 2 Step 2 分流点透传 + Step 4 addSubagentMessage 设置 createdAt
- ✅ 定时刷新相对时间 → Task 5 setInterval

### 2. Placeholder scan
- 无 TBD/TODO
- 所有代码块完整
- 所有修改点有行号和上下文

- `createdAt` 参数名在 addMessage / addSubagentMessage / addMessageWithId / prependMessage 四处一致
- `msg.created_at` 字段名与后端 `MessageResponse.created_at` 一致
- `formatRelativeTime` 函数名在 Task 1 定义、Task 2 和 Task 5 使用
- `dataset.timestamp` 属性名在 Task 2 设置、Task 5 读取
- `timestamp-bubble` class 名在 Task 1 CSS、Task 2 DOM、Task 5 定时器一致

### 4. 风险点
- **Markdown 渲染不受影响**：气泡 DOM 是 `div` 的直接子节点，与 `.message-text` 同级，不干扰 marked.js 渲染
- **去重逻辑不受影响**：`addMessageWithId` 仍按 `data-id` 去重，`createdAt` 只是额外属性
- **临时消息替换正常**：L1648-1657 按文本匹配删除临时消息，DB 版本带 createdAt 正常渲染
- **CSS z-index**：气泡 `z-index: 5`，低于脑区面板（z-index 8/10），不冲突
