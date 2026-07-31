# Spirit 报警状态修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用户发消息（本地或飞书）时自动取消小女孩报警状态，同时修复 spirit 状态机的其他 bug（transitionTo 未定义、ALERT 期间 busy 打断、多条 alert 丢失、alert 内容传递、SSE 重连不同步）。

**Architecture:** 在 main.js SSE 分发层加 spiritWindow 的 cancel-alert 转发（单一修改点覆盖所有消息来源）；spirit.html 加 onCancelAlert 监听 + 修复 transitionTo + ALERT 期间 busy 守卫；preload-assistant.js 暴露 onCancelAlert；alerts_api 遍历多条 alert；scheduler 传递实际任务内容；main.js SSE 重连同步 spirit。

**Tech Stack:** Electron 33 (IPC + preload), JavaScript, Python (FastAPI)

---

## 已验证的关键事实

1. **ALERT 触发链路**：scheduler → `add_pending_alert("⏰")` → 前端 HTTP 轮询（10s）→ `main.js:1687` `spiritWindow.send('alert', '⏰')` → `spirit.html:704` `onAlert` → `setState(State.ALERT)`
2. **ALERT 退出只有 2 条路径**：鼠标 `mouseenter`（spirit.html:441-442）和点击（spirit.html:465-466），都调 `endAlert()`
3. **BUSY 能从飞书触发的原因**：BUSY 走 `chat_busy`/`chat_idle` SSE 事件 → chat.html → `notifyBusy` IPC → spirit，与消息来源无关
4. **ALERT 无法被消息取消的根因**：`main.js:1805-1813` 的 SSE `new_message` 分支只转发 chatWindow，不转发 spiritWindow；spirit 也没有 `onNewMessage` 回调
5. **`transitionTo` 未定义**：spirit.html:720 调用 `transitionTo(State.IDLE)` 但该函数未定义。但影响有限——飞书消息通过 `onBusyState`→`setState(BUSY)` 唤醒 SLEEP（不走 transitionTo），本地聊天活动通过 `onUserActivity`→`transitionTo` 会抛 ReferenceError，但发消息后 Agent busy 会间接唤醒

6. **ALERT 期间 busy 打断**：spirit.html:544-549 的 `onBusyState` 没有检查 `currentState === State.ALERT`，busy 变化会 `setState(BUSY/IDLE)` 打断 ALERT
7. **pending_alerts 多条丢失**：`main.js:1687` 只发一次 `alert` IPC，不遍历 alerts 数组
8. **alert 内容固定 `⏰`**：scheduler L132 `add_pending_alert("⏰")` 不传实际任务内容
9. **SSE 重连不同步 spirit**：`main.js:1786` 重连后只通知 chatWindow `sync-state`，不重置 spirit 的 busyCount

## File Structure

| 文件 | 职责 | 操作 |
|------|------|------|
| `ui/main/main.js` | SSE 分发层加 cancel-alert 转发 + SSE 重连同步 spirit + 多条 alert 遍历 | 修改 |
| `ui/main/preload-assistant.js` | 暴露 onCancelAlert IPC 回调 | 修改 |
| `ui/main/windows/assistant/spirit.html` | 注册 onCancelAlert + 修复 transitionTo + ALERT 期间 busy 守卫 | 修改 |
| `niu_api/internal/scheduler/service.py` | add_pending_alert 传实际任务内容 | 修改 |

---

### Task 1: main.js — SSE 分发加 cancel-alert + 多条 alert 遍历

**Files:**
- Modify: `ui/main/main.js`

- [ ] **Step 1: 在 SSE new_message 分支加 cancel-alert 转发**

在 `ui/main/main.js` 中，找到 SSE 分发的 `new_message` 分支（L1805-1813）。

当前代码：
```javascript
            if (event.type === 'new_message') {
              // 通知 chat 有新消息（传递 role/content/source 字段，用于 chat_busy/chat_idle 状态机控制 + ask_main_agent 跨进程转发）
              if (chatWindow && !chatWindow.isDestroyed()) {
                chatWindow.webContents.send('new-message', {
                  role: event.role,
                  content: event.content,
                  source: event.source
                });
              }
            } else if (event.type === 'tool_status') {
```

改为（在 chatWindow 块之后、`}` 之前加 spiritWindow 转发）：
```javascript
            if (event.type === 'new_message') {
              // 通知 chat 有新消息（传递 role/content/source 字段，用于 chat_busy/chat_idle 状态机控制 + ask_main_agent 跨进程转发）
              if (chatWindow && !chatWindow.isDestroyed()) {
                chatWindow.webContents.send('new-message', {
                  role: event.role,
                  content: event.content,
                  source: event.source
                });
              }
              // 用户发消息时取消 spirit 的 ALERT 状态
              // 用户发消息代表已看到报警内容，无论本地还是飞书都应取消
              if (event.role === 'user' && spiritWindow && !spiritWindow.isDestroyed()) {
                spiritWindow.webContents.send('cancel-alert');
              }
            } else if (event.type === 'tool_status') {
```

- [ ] **Step 2: 多条 alert 遍历发送**


在 `ui/main/main.js` 中，找到 pending alerts 轮询的发送逻辑（约 L1685-1689）。

当前代码：
```javascript
      if (alerts && alerts.length > 0) {
        if (spiritWindow && !spiritWindow.isDestroyed()) {
          spiritWindow.webContents.send('alert', '⏰');
        }
      }
```

改为（遍历每条 alert，传实际内容）：
```javascript
      if (alerts && alerts.length > 0) {
        if (spiritWindow && !spiritWindow.isDestroyed()) {
          // 每条 alert 都发送，spirit 端 setState 有守卫（已 ALERT 不重复）
          alerts.forEach(a => {
            const content = (a && a.content) ? a.content : '⏰';
            spiritWindow.webContents.send('alert', content);
          });
        }
      }
```

注意：确认 `alerts` 是数组（`get_and_clear_pending_alerts` 返回 `list[dict]`，每个元素有 `content` 字段）。

- [ ] **Step 3: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add ui/main/main.js
git commit -m "feat(spirit): SSE new_message 转发 cancel-alert 到 spirit + 多条 alert 遍历"
```

---

### Task 2: preload-assistant.js — 暴露 onCancelAlert

**Files:**
- Modify: `ui/main/preload-assistant.js`

- [ ] **Step 1: 在 preload-assistant.js 加 onCancelAlert 回调**

在 `ui/main/preload-assistant.js` 中，找到 `onAlert` 定义（L85）。

当前代码：
```javascript
  // 接收蹦高通知（有新消息但窗口不在焦点）
  onAlert: (callback) => ipcRenderer.on('alert', (event, message) => callback(message)),

  // 接收用户活动通知（重置空闲计时器）
  onUserActivity: (callback) => ipcRenderer.on('user-activity', callback),
```

改为（在 onAlert 和 onUserActivity 之间加 onCancelAlert）：
```javascript
  // 接收蹦高通知（有新消息但窗口不在焦点）
  onAlert: (callback) => ipcRenderer.on('alert', (event, message) => callback(message)),

  // 接收取消报警通知（用户发消息时触发，代表已看到报警内容）
  onCancelAlert: (callback) => ipcRenderer.on('cancel-alert', () => callback()),

  // 接收用户活动通知（重置空闲计时器）
  onUserActivity: (callback) => ipcRenderer.on('user-activity', callback),
```

- [ ] **Step 2: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add ui/main/preload-assistant.js
git commit -m "feat(preload): 暴露 onCancelAlert IPC 回调给 spirit 窗口"
```

---

### Task 3: spirit.html — 注册 onCancelAlert + 修复 transitionTo + ALERT 期间 busy 守卫

**Files:**
- Modify: `ui/main/windows/assistant/spirit.html`

- [ ] **Step 1: 注册 onCancelAlert 监听**

在 `ui/main/windows/assistant/spirit.html` 中，找到 `onAlert` 监听注册处（L704-708）。

当前代码：
```javascript
    // 监听蹦高通知（有新消息但窗口不在焦点）
    window.electronAPI.onAlert((message) => {
      console.log('[Spirit] 收到蹦高通知:', message);
      alertMessage = message;
      setState(State.ALERT);
    });
```

在它之后加 onCancelAlert 监听：
```javascript
    // 监听蹦高通知（有新消息但窗口不在焦点）
    window.electronAPI.onAlert((message) => {
      console.log('[Spirit] 收到蹦高通知:', message);
      alertMessage = message;
      setState(State.ALERT);
    });

    // 监听取消报警通知（用户发消息时触发，代表已看到报警内容）
    window.electronAPI.onCancelAlert(() => {
      if (currentState === State.ALERT) {
        console.log('[Spirit] 用户发消息，取消 ALERT');
        endAlert();
      }
    });
```

- [ ] **Step 2: 修复 transitionTo 未定义 bug**

在 `ui/main/windows/assistant/spirit.html` 中，找到 `onUserActivity` 回调（L717-724）。

当前代码：
```javascript
    window.electronAPI.onUserActivity(() => {
      if (currentState === State.SLEEP) {
        console.log('[Spirit] 用户活动，从睡眠中唤醒');
        transitionTo(State.IDLE);
      } else if (currentState === State.IDLE) {
        console.log('[Spirit] 用户活动，重置空闲计时器');
        startIdleTimer();
      }
    });
```

改为（`transitionTo` → `setState`）：
```javascript
    window.electronAPI.onUserActivity(() => {
      if (currentState === State.SLEEP) {
        // 用户活动唤醒直接到 IDLE，不走 WAKE 过渡（与鼠标 hover 不同）
        setState(State.IDLE);
      } else if (currentState === State.IDLE) {
        console.log('[Spirit] 用户活动，重置空闲计时器');
        startIdleTimer();
      }
    });
```

- [ ] **Step 3: ALERT 期间 busy 状态守卫**

在 `ui/main/windows/assistant/spirit.html` 中，找到 `onBusyState` 回调（L544-549）。

当前代码：
```javascript
    window.electronAPI.onBusyState((isBusy, reason) => {
      if (isBusy) {
        incrementBusy(reason || 'backend');
      } else {
        decrementBusy(reason || 'backend');
      }
    });
```

改为（加 ALERT 守卫，更新 busyCount 但不切状态）：
```javascript
    window.electronAPI.onBusyState((isBusy, reason) => {
      if (currentState === State.ALERT) {
        // ALERT 期间只更新 busyCount，不调 setState 避免打断报警动画
        // endAlert 恢复后会根据 busyCount 正确反映状态
        if (isBusy) {
          busyCount++;
          console.log(`[BusyCount] +1 (${reason || 'backend'}) → ${busyCount} [ALERT期间]`);
        } else if (busyCount > 0) {
          busyCount--;
          console.log(`[BusyCount] -1 (${reason || 'backend'}) → ${busyCount} [ALERT期间]`);
        }
        console.log('[Spirit] ALERT 期间仅更新 busyCount，不切换状态:', isBusy, reason);
        return;
      }
      if (isBusy) {
        incrementBusy(reason || 'backend');
      } else {
        decrementBusy(reason || 'backend');
      }
    });
```

- [ ] **Step 4: 修改 endAlert 根据 busyCount 恢复状态**

在 `ui/main/windows/assistant/spirit.html` 中，找到 `endAlert` 函数（L323-335）。

当前代码：
```javascript
    function endAlert() {
      if (currentState !== State.ALERT) return;

      console.log('[State] 结束 ALERT，恢复到:', previousStateBeforeAlert);

      if (previousStateBeforeAlert) {
        setState(previousStateBeforeAlert);
      } else {
        // 默认恢复到 IDLE
        setState(State.IDLE);
      }
      previousStateBeforeAlert = null;
    }
```

改为（根据 busyCount 决定恢复到 BUSY 还是 IDLE）：
```javascript
    function endAlert() {
      if (currentState !== State.ALERT) return;

      console.log('[State] 结束 ALERT，恢复到:', previousStateBeforeAlert);

      // 根据 busyCount 决定恢复状态，避免 ALERT 期间 busyCount 变化导致状态不一致
      if (busyCount > 0) {
        setState(State.BUSY);
      } else {
        setState(State.IDLE);
      }
      previousStateBeforeAlert = null;
    }
```

- [ ] **Step 5: 修改 resetBusy 加 ALERT 守卫**

在 `ui/main/windows/assistant/spirit.html` 中，找到 `resetBusy` 函数（L229-237）。

当前代码：
```javascript
    function resetBusy() {
      // 强制归零：用于 SSE 断连后 chat_idle 丢失、busyCount 无法配对归零的修复场景
      // 由 chat.html onSyncState（查后端 getChatStatus 返回 !busy）触发
      if (busyCount > 0) {
        console.log(`[BusyCount] reset ${busyCount} → 0 (sync from backend)`);
        busyCount = 0;
        setState(State.IDLE);
      }
    }
```

改为（ALERT 期间只归零 busyCount，不调 setState）：
```javascript
    function resetBusy() {
      // 强制归零：用于 SSE 断连后 chat_idle 丢失、busyCount 无法配对归零的修复场景
      if (busyCount > 0) {
        console.log(`[BusyCount] reset ${busyCount} → 0 (sync from backend)`);
        busyCount = 0;
        // ALERT 期间不调 setState，避免打断报警动画
        // endAlert 恢复时会根据 busyCount 正确反映状态
        if (currentState !== State.ALERT) {
          setState(State.IDLE);
        }
      }
    }
```

- [ ] **Step 6: 提交**

---

### Task 4: scheduler — add_pending_alert 传实际任务内容

**Files:**
- Modify: `niu_api/internal/scheduler/service.py`

- [ ] **Step 1: add_pending_alert 传实际任务内容**

在 `niu_api/internal/scheduler/service.py` 中，找到 L132。

当前代码：
```python
    # 触发小女孩蹦高提醒（仅用于视觉提示，不传递消息内容）
    add_pending_alert("⏰")
```

改为（传任务内容前 50 字符）：
```python
    # 触发小女孩蹦高提醒，传递任务内容摘要让用户知道是什么事
    task_content = task.get("content", "⏰")
    alert_text = (task_content[:47] + "...") if len(task_content) > 50 else task_content
    add_pending_alert(alert_text)
```

- [ ] **Step 2: ruff 检查**

Run: `cd /Users/lilei/tools/ai-bot && ruff check niu_api/internal/scheduler/service.py`
Expected: OK

- [ ] **Step 3: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add niu_api/internal/scheduler/service.py
git commit -m "feat(scheduler): add_pending_alert 传实际任务内容而非固定 emoji"
```

---

### Task 5: 运行环境验证

**Files:** 无（手动验证）

- [ ] **Step 1: 重启应用**

```bash
cd /Users/lilei/tools/ai-bot && ./niu
```

- [ ] **Step 2: 测试报警取消（本地）**

1. 等待一个定时任务触发（或手动调 `POST /api/alert` 触发报警）
2. 确认小女孩进入 ALERT 状态（蹦高 + 红色气泡）
3. 在聊天框发一条消息
4. 预期：小女孩立即取消 ALERT，恢复到之前状态

- [ ] **Step 3: 测试报警取消（飞书）**

1. 触发报警
2. 在飞书给 Agent 发一条消息
3. 预期：小女孩立即取消 ALERT

- [ ] **Step 4: 测试 SLEEP 唤醒**

1. 不操作应用 5 分钟，等小女孩进入 SLEEP
2. 在聊天框输入文字（触发 user-activity）
3. 预期：小女孩从 SLEEP 唤醒到 IDLE（不报 ReferenceError）

- [ ] **Step 5: 测试 ALERT 期间本地消息的 busy 守卫**

注意：飞书消息的 cancel-alert（role=user SSE）在 chat_busy 之前到达，会先取消 ALERT。
要测试 busy 守卫，必须用本地消息（chat.html 的 notifyBusy 在 SSE 之前同步触发）。

1. 触发报警（ALERT）
2. 在本地聊天框输入消息并发送
3. 预期：notifyBusy(true) 先到达 spirit（被 ALERT 守卫忽略，不切状态），然后 cancel-alert 到达取消 ALERT

- [ ] **Step 6: 测试飞书消息取消 ALERT**

1. 触发报警（ALERT）
2. 在飞书发消息
3. 预期：小女孩取消 ALERT（role=user SSE 的 cancel-alert 先于 chat_busy 到达）

---

## Self-Review

### 1. Spec coverage
- ✅ 用户发消息取消报警（本地+飞书） → Task 1 Step 1 + Task 2 + Task 3 Step 1
- ✅ transitionTo 未定义修复 → Task 3 Step 2
- ✅ ALERT 期间 busy 打断修复 → Task 3 Step 3 + Step 4 + Step 5
- ✅ 多条 alert 丢失修复 → Task 1 Step 2
- ✅ alert 内容传递 → Task 4
- ⚠️ SSE 重连同步 spirit → 已删除（现有 chat.html onSyncState 路径已处理，直接 reset-busy 会在 Agent busy 时误重置）

### 2. Placeholder scan
- 无 TBD/TODO
- 所有代码块完整
- 所有修改点都有行号和上下文

### 3. Type consistency
- `cancel-alert` IPC 事件名在 main.js（send）、preload-assistant.js（on）、spirit.html（监听）三处一致
- `onCancelAlert` 回调签名一致：`() => callback()`
- `add_pending_alert(content: str)` 签名不变，只改传入值
- `setState` 在 spirit.html 中已定义（L252），替代 `transitionTo`
- `endAlert` 在 spirit.html 中已定义（L323）
