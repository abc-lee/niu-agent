# 子 Agent tab 复用堆叠修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复同步子 Agent tab 的**复用堆叠**问题——同一子 Agent（unique_name = agent_name 固定值）连续多次任务时，第二次触发的 `createSubagentTab` 因同名 tab 已存在而直接 return（`chat.html L2793`），导致新任务的消息追加进上一个任务的旧 tab（可能呈 `.completed` 半透明态），新旧任务消息混叠、无分隔。

**Architecture:** 修改 `ui/main/windows/assistant/chat.html` 的 `createSubagentTab`（L2792-2828）：当同 unique_name 的 tab 已存在时，**不再无条件 return**，而是区分两种情况——若旧 tab 已结束（带 `.completed` 类）或已出错（带 `.error` 类）或仍在 `_pendingCloseTabs` 待关闭列表里，则**清空旧 tab 重新用于"新一轮"**（移除 completed/waiting/error 状态、清空消息容器、取消待关闭 timer、重置 loading 空态）；若旧 tab 仍在活跃运行（无 completed/error、不在待关闭列表）则维持原 return（避免同一次任务重复建 tab）。

**Tech Stack:** 原生 JS（Electron 渲染进程 chat.html，单文件前端）

---

## 关键技术约束

1. **前端代码修改铁律**：只改 `ui/main/windows/assistant/chat.html` 的 `createSubagentTab` 函数（L2792-2828）及其所需的小段辅助逻辑，不触碰其他现有函数（switchTab/_closeSubagentTab/markTabCompleted/subagent_closed 处理等）。
2. **无自动化测试**：前端单文件 HTML 内嵌 JS，用 python 正则验证 + 用户端到端验证。
3. **不引入新 DOM/CSS 类**：复用现有的 `completed`/`waiting`/`error`/`loading`/`tab-empty-state` 类。
4. **向后兼容**：异步子 Agent（unique_name 每次不同）不受影响；活跃 tab 的 return 行为保持不变。

## 文件结构

- **Modify**: `ui/main/windows/assistant/chat.html` — 仅 `createSubagentTab`（L2792-2828）：
  - 把 L2793 的 `if (document.getElementById('tab-' + uniqueName)) return;` 改为条件复用逻辑
  - 新增小段：已存在时判断是否可重用（`.completed` 或 `_pendingCloseTabs.has(uniqueName)`），可重用则清空重置；不可重用（活跃）则 return

## 根因分析（已核实）

### 触发链
1. 同步子 Agent unique_name = agent_name（固定，`agent/subagent.py` L927-936 `force_unique_name=agent_name`）
2. 第一次任务结束 → `subagent_closed` 事件 → `markTabCompleted`（L2897-2900，加 `.completed` 半透明）+ tab 进入待关闭逻辑（L3012-3026）
   - 用户在该 tab：`_pendingCloseTabs.add(unique_name)`（L3014），等切走才关
   - 用户不在：3 秒后 `_closeSubagentTab`（L3022），除非期间用户切回
3. 第二次触发同 agent → `subagent_started` → `onSubagentStarted`（L2964-2972）→ `createSubagentTab(unique_name,...)`
4. L2793 `if (document.getElementById('tab-' + uniqueName)) return;` → **直接返回，不重建**
5. 新任务事件（instruction/reply/tool_status 等）经 `addSubagentMessageToTab` 追加进**旧容器的旧消息之后** → 新旧任务混叠

### 关键状态量
- `_pendingCloseTabs`: `Set`（L2656），含待关闭但未关的 tab unique_name
- `_endCloseTimers`: `Map`（L2657），tab → 3 秒关闭的 timer id
- `.completed` 类：`markTabCompleted` 添加（L2899），CSS 半透明（L785）
- 清空一个 tab 需要：清 `_pendingCloseTabs`、清 `_endCloseTimers` timer、移除 `.completed`/`.waiting`/`.error` 类、清空 `messages-<uniqueName>` 容器子节点、重置 loading 空态

## 修复方案

在 `createSubagentTab` 开头，把无条件 return 改为条件复用：

```javascript
function createSubagentTab(uniqueName, displayName, isSync, skipLoading) {
  const existingTab = document.getElementById('tab-' + uniqueName);
  if (existingTab) {
    // 判断旧 tab 是否"已结束/待关闭/已出错"——可清空重用于新一轮
    const isReusable = existingTab.classList.contains('completed') || existingTab.classList.contains('error') || _pendingCloseTabs.has(uniqueName);
    if (!isReusable) return;  // 活跃 tab（仍在运行）——维持原行为，不重复创建
    // 复用旧 tab：清空状态 + 消息，作为"新一轮"重新开始
    // 取消 @end 延迟关闭 timer
    const timer = _endCloseTimers.get(uniqueName);
    if (timer) { clearTimeout(timer); _endCloseTimers.delete(uniqueName); }
    _pendingCloseTabs.delete(uniqueName);
    // 移除结束/等待/错误状态
    existingTab.classList.remove('completed', 'waiting', 'error');
    // 重设 loading 态（首条消息到达后由 addSubagentMessageToTab 移除）
    if (!skipLoading) existingTab.classList.add('loading');
    // 清空消息容器，重建空状态占位
    const container = document.getElementById('messages-' + uniqueName);
    if (container) {
      container.innerHTML = '';
      const empty = document.createElement('div');
      empty.className = 'tab-empty-state';
      empty.textContent = isSync ? `⏳ 子 Agent ${displayName} 工作中...` : `⏳ 子 Agent ${displayName} 正在启动...`;
      container.appendChild(empty);
    }
    tabBar.scrollIntoView({ behavior: 'smooth', inline: 'nearest', block: 'nearest' });
    return;
  }
  // ...原有创建新 tab 的代码（L2795-2827）保持不变
```

**注意**：
- `existingTab.dataset.sync` 已存在（首次创建时设过 L2802），复用时不需重设——若新任务 model 变更（isSync 不同），可更新 `existingTab.dataset.sync = isSync ? 'true' : 'false'`（防御，一般同步子 Agent 固定）
- 复用时不重复 appendChild tab（tab 已在 tabBar）——只清状态和容器
- `skipLoading` 参数：L3070（窗口恢复）传 true，复用时应保持一致

### 补充：switchTab 500ms 待关闭定时器竞态守卫（review 发现）

**竞态**：`switchTab`（L2742-2746）在用户切走待关闭 tab 时调度 500ms 延迟关闭，该 setTimeout **不入 `_endCloseTimers`**，复用分支无法取消。场景：用户停留 completed tab → 切走 → 500ms 关闭被调度 → 500ms 内同 agent 任务再触发 → 复用清空 tab → 500ms 后 `_closeSubagentTab` 照样触发 → 删除刚复用的新 tab + 断掉新任务 SSE。

**方案 (b)：在 `_closeSubagentTab` 入口加活跃守卫**（两个独立 reviewer 均推荐，最健壮）——复用分支已删 `.completed` 和 `_pendingCloseTabs`，复用后的 tab 既无 completed 也不在待关闭列表，因此 `_closeSubagentTab` 可据此识别"活跃新一轮"并跳过。

```javascript
    function _closeSubagentTab(uniqueName) {
      // 守卫：若 tab 已被复用为活跃新一轮（无 completed、不在待关闭列表），不关闭
      const closeTab = document.getElementById('tab-' + uniqueName);
      if (closeTab && !closeTab.classList.contains('completed') && !_pendingCloseTabs.has(uniqueName)) {
        return;  // 活跃 tab，禁止误杀（迟到的 switchTab 500ms / 其他 close 路径可能抵达）
      }
      _pendingCloseTabs.delete(uniqueName);
      ...
    }
```

**守卫不误伤正常关闭**：
- 3 秒未激活关闭：tab 仍 `.completed` → 不 return，正常关 ✅
- switchTab 切走关闭：tab `.completed` + 在 `_pendingCloseTabs` → 不 return，正常关 ✅
- 复用后新任务又自然结束：subagent_closed 重新加回 `.completed` → 后续 close 正常关 ✅
- 只有"复用后活跃"的 tab 被守卫跳过 ✅

---

## Task 1: 修改 createSubagentTab 支持 tab 复用

**Files:**
- Modify: `ui/main/windows/assistant/chat.html:2792-2795`（把 L2793 的无条件 return 改为条件复用逻辑，新增复用块）

- [ ] **Step 1: 读取当前代码确认锚点**

读 `ui/main/windows/assistant/chat.html` 的 `createSubagentTab`（L2792-2828）。当前开头：

```javascript
    function createSubagentTab(uniqueName, displayName, isSync, skipLoading) {
      if (document.getElementById('tab-' + uniqueName)) return;  // 已存在则不重复创建

      // tab 元素（无 × 按钮，不可手动关闭）
      const tab = document.createElement('div');
      ...
```

- [ ] **Step 2: 替换 L2793 并插入复用逻辑**

把 L2793 一行替换为「判断是否可复用 + 复用块 + return」；若不可复用（活跃 tab）则 return；可复用则清空重置后 return；都不满足则落到原有创建逻辑。

完整替换后的 `createSubagentTab` 开头（L2792 到 L2794 之间）：

```javascript
    function createSubagentTab(uniqueName, displayName, isSync, skipLoading) {
      const existingTab = document.getElementById('tab-' + uniqueName);
      if (existingTab) {
        // 旧 tab 已结束（completed）或已出错（error）或待关闭（_pendingCloseTabs）→ 清空重用于新一轮
        const isReusable = existingTab.classList.contains('completed') || existingTab.classList.contains('error') || _pendingCloseTabs.has(uniqueName);
        // 取消 @end 延迟关闭 timer
        const timer = _endCloseTimers.get(uniqueName);
        if (timer) { clearTimeout(timer); _endCloseTimers.delete(uniqueName); }
        _pendingCloseTabs.delete(uniqueName);
        // 移除结束/等待/错误状态 + 同步标志，重设 loading
        existingTab.classList.remove('completed', 'waiting', 'error');
        existingTab.dataset.sync = isSync ? 'true' : 'false';
        if (!skipLoading) existingTab.classList.add('loading');
        // 清空消息容器，重建空状态占位
        const container = document.getElementById('messages-' + uniqueName);
        if (container) {
          container.innerHTML = '';
          const empty = document.createElement('div');
          empty.className = 'tab-empty-state';
          empty.textContent = isSync ? `⏳ 子 Agent ${displayName} 工作中...` : `⏳ 子 Agent ${displayName} 正在启动...`;
          container.appendChild(empty);
        }
        tabBar.scrollIntoView({ behavior: 'smooth', inline: 'nearest', block: 'nearest' });
        return;
      }

      // tab 元素（无 × 按钮，不可手动关闭）
      const tab = document.createElement('div');
      ...  // 原有 L2795-2827 创建新 tab 的代码保持不变
```

**注意**：
- `existingTab.dataset.sync` 更新：防御新任务 isSync 与旧值不同（同步子 Agent 固定为 true，一般不变）
- `_endCloseTimers`/`_pendingCloseTabs` 需在作用域可用（都是模块级 `let`，L2656-2657，同一 script 内，createSubagentTab 可见）
- 不清 `_subagentWaitingTab`（若旧 tab 在 @user 等待态被复用——但复用前提是 completed/待关闭，不会仍在 waiting）——若有顾虑可加 `if (_subagentWaitingTab === uniqueName) _setSubagentInputWaiting(false)`

- [ ] **Step 3: 在 `_closeSubagentTab` 加活跃 tab 守卫（防 switchTab 500ms 竞态）**

在 `_closeSubagentTab`（L2940）函数体**最开头**、`_pendingCloseTabs.delete(uniqueName)` 之前，插入守卫：

```javascript
    function _closeSubagentTab(uniqueName) {
      // 守卫：若 tab 已被复用为活跃新一轮（无 completed、不在待关闭列表），不关闭
      // 防 "用户停留 completed tab → 切走 → 500ms 内同 agent 任务复用 → 迟到的 switchTab 500ms 关闭误杀新 tab"
      const closeTab = document.getElementById('tab-' + uniqueName);
      if (closeTab && !closeTab.classList.contains('completed') && !_pendingCloseTabs.has(uniqueName)) {
        return;  // 活跃 tab，禁止误杀
      }
      _pendingCloseTabs.delete(uniqueName);
      ...  // 原有 L2941-2958 代码保持不变
    }
```

**守卫不误伤正常关闭**（见"补充：switchTab 500ms 待关闭定时器竞态守卫"节）：
- 3 秒未激活 / switchTab 切走关闭：tab 仍 `.completed`（或待关闭列表）→ 不 return，正常关
- 复用后新任务自然结束再关闭：subagent_closed 重新加回 `.completed` → 正常关


- [ ] **Step 4: 验证（python 正则 + 结构核对）**

Run:
```bash
python/bin/python -c "
import re
src = open('ui/main/windows/assistant/chat.html').read()
# 确认新的复用逻辑存在
print('isReusable check:', 'isReusable' in src)
print('_pendingCloseTabs.has(uniqueName):', '_pendingCloseTabs.has(uniqueName)' in src)
print('existingTab.classList.remove(completed):', \"existingTab.classList.remove('completed'\" in src)
# 确认 _closeSubagentTab 守卫存在
print('closeTab guard:', '!closeTab.classList.contains(\'completed\')' in src)
# 确认函数括号平衡（粗略：createSubagentTab 函数体大括号匹配）
m = re.search(r'function createSubagentTab.*?\n    }', src, re.DOTALL)
print('createSubagentTab block balanced:', bool(m))
mc = re.search(r'function _closeSubagentTab.*?\n    }', src, re.DOTALL)
print('_closeSubagentTab block balanced:', bool(mc))
"
```
预期：前四项 True，后两项 True

同时人工核对：
- `existingTab` 定义后，若 `!isReusable` return，落到原有 `createElement('tab')` 创建逻辑——确认原有代码未改
- 复用分支 `return` 后不重复创建 tab/container
- `_pendingCloseTabs`/`_endCloseTimers` 引用不出错（未定义会 ReferenceError）
- `_closeSubagentTab` 入口守卫在 `_pendingCloseTabs.delete` 之前（return 分支先判断）
- 守卫不破坏正常关闭：3 秒未激活 / switchTab 切走（tab 仍 .completed 或在 _pendingCloseTabs）不会触发守卫 return

- [ ] **Step 5: 提交**



```bash
git add ui/main/windows/assistant/chat.html
git commit -m "fix: reuse completed subagent tab for new task instead of stacking

createSubagentTab previously returned when a tab with the same unique_name
existed, causing sync subagents (unique_name = agent_name) to append new
task events into the old completed/半透明 tab. Now detects reusable tabs
(completed or pending-close) and clears them for a fresh round."
```

---

## Task 2: 端到端手工验证（需用户参与）

- [ ] **Step 1: 启动 Niu Agent**（`./niu`）
- [ ] **Step 2: 触发同步子 Agent 连续两次同 agent 任务**
  - 主对话输入触发 `chat-with-file-processor`（或任意同步子 Agent）
  - 第一次任务完成（tab 半透明）
  - **用户停留该 tab 不切走**，立即再次触发同 agent 任务
  - **验收**：旧 tab 被清空重用于新一轮，首条显示新任务的 instruction/回复，**不堆叠**旧任务消息
- [ ] **Step 3: 验证 switchTab 500ms 竞态守卫（P1 修复针对性验收）**
  - 完成一次同步子 Agent 任务（tab 半透明）
  - **用户停留该 tab → 切走**（触发 switchTab 500ms 待关闭调度）
  - **500ms 内**再次触发同 agent 任务（复用刚发生）
  - **验收**：新任务 tab 不被迟到的 500ms 关闭删掉（`_closeSubagentTab` 守卫跳过）；新任务正常跑、消息正常显示
- [ ] **Step 4: 验证 errored tab 复用（P2 修复针对性验收）**
  - 触发一个会报错的同步子 Agent（如给无效参数/触发异常，tab 变红 `.error`）
  - **立即再次触发同 agent 任务**
  - **验收**：红色 error tab 被清空重用于新一轮（移除 `.error`、重建空态），新任务消息正常显示，**不堆叠**旧 error 消息

- [ ] **Step 5: 验证活跃 tab 不受影响**（同一次任务内不重复建 tab）
  - 触发一个异步子 Agent，正常运行——确认 tab 单一、消息正常顺序
- [ ] **Step 6: 验证主↔子对话显示仍正常**（回归）
  - 异步/同步子 Agent 的 instruction/回答仍显示在 tab（前一个功能的成果不受影响）


### 1. Spec coverage
- 同 unique_name 重复任务清空复用 → Step 2 ✅
- switchTab 500ms 竞态守卫 → Step 3 + `_closeSubagentTab` 入口守卫 ✅
- 活跃 tab 不重复创建（维持原行为）→ Step 2 `!isReusable` return ✅
- 端到端验证 → Task 2 ✅



### 2. Placeholder scan
- 无 TBD/TODO；代码块完整；无省略号含糊

### 3. Type consistency
- 复用时 `existingTab`/`existingTab.dataset.sync`/`_pendingCloseTabs`/`_endCloseTimers` 引用名与现有代码一致（L2656/2657、L2802）
- 空态占位 `tab-empty-state`/`loading` 类与现有完全一致（L2797/2820-2823）

### 4. 风险点
- **复用分支 ref 作用域**：`_endCloseTimers`/`_pendingCloseTabs` 是模块级 `let`（L2656-2657），`createSubagentTab` 在同一 script 内可见。不会 ReferenceError。
- **活跃 tab 的判断**：`isReusable = .completed || .error || _pendingCloseTabs.has()`。`.error` 是终结态（subagent_error 后子 Agent 结束，不会"活跃但出错"），含 `.error` 复用电安全；若旧 tab 既非 completed/error 也不在待关闭（异常残留未终结态），会走 return 维持旧行为——保守，不误清活跃 tab。
- **dataset.sync 更新**：防御性，防 isSync 变更导致 hideTyping 判断错乱。
- **不清 _subagentWaitingTab**：复用前提是 completed（已结束）或待关闭（也是结束后），不会在 @user 等待态——安全。但为稳健可加一行（见 Step 2 备注，可选）。

---

## 计划审查交付条件
按项目流程：本计划需经过计划审查（连续两轮零 bug）后方可实施。
