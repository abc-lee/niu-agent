# 见缝插针 — 回车后停止按钮消失根因修复 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复见缝插针场景下"上一轮 Agent 未完成、用户回车发新消息导致停止按钮消失"的 bug，恢复 UI 状态完全由 SSE chat_busy/chat_idle 驱动的设计意图。

**Architecture:** 见缝插针原始计划 (`2026-06-07-interleave-messages.md` Task 4) 要求删除 `sendMessage` `finally` 块中的 UI 状态重置（`hideTyping()`、`notifyBusy(false)`、`stopBtn.style.display='none'`、`isProcessing=false`），让 SSE `chat_idle` 单独驱动状态机。实施时只删了一半（`isProcessing` 守卫、`busyTimeout`），`finally` 块的状态重置漏删。补充消息路径下后端立即返回 `ChatResponse(reply="已收到")`，前端 `finally` 抢在 SSE `chat_idle` 之前重置状态，导致停止按钮消失。本计划删除 `finally` 块的状态重置，让状态机完全由 SSE 驱动。

**Tech Stack:** Electron 33 renderer (chat.html), JavaScript

---

## 根因分析（基于 code-explorer Agent 全链路审计 + 主 Agent 复核）

### 现象

- 已显示停止按钮（`chat_busy` SSE 已推、`isProcessing=true`、`stopBtn.display='flex'`）
- Agent 第 N 轮未完
- 用户回车发新消息 → 停止按钮**消失**（但 Agent 还在跑）

### 时序图

```
T0  Agent 第 N 轮运行中
    后端 _chat_lock.locked() == True
    前端 isProcessing=true, stopBtn.display='flex'  ✓ 正确

T1  用户回车发补充消息
    chat.html:859 sendMessage() 被调用
    chat.html:916 addMessage('user', text)  → 本地渲染用户气泡
    chat.html:921 showTyping()
    chat.html:922 notifyBusy(true, 'chat')   ← 这是 typing 提示，无害
    chat.html:925 await sendMessageWithRetry(text)
        → HTTP POST /api/chat/session

T2  后端 compat.py:_chat_lock.locked() 命中见缝插针分支
    持久化 user 消息 + notify_new_message(role="user") SSE
    enqueue_supplement(request.message)
    return ChatResponse(reply="已收到", ...)   ← HTTP 立即返回
    注意：后端不推 chat_idle 也不推 chat_busy

T3  前端 await 返回，进入 chat.html:926 finally 块
    chat.html:928 hideTyping()         ← 隐藏 typing 指示（无害）
    chat.html:929 notifyBusy(false)    ← ⚠ 通知精灵 idle（错误：Agent 还在跑）
    chat.html:930 stopBtn.style.display = 'none'   ← 🔴 停止按钮消失（根因）
    chat.html:931 isProcessing = false              ← 🔴 状态机被破坏
    chat.html:932 sendBtn.disabled = false

T4  前端 SSE 收到 T2 推的 role="user" 事件
    chat.html:1502 else 分支 → 只 refreshFromDB()，不动 stopBtn

T5  Agent 第 N+1 轮继续在 backend 跑
    前端 isProcessing=false, stopBtn 已隐藏
    用户看到：Agent 还在跑（DB 持续有 assistant 消息），但停止按钮没了
    且 Escape 键停止守卫 (chat.html:1528 isProcessing && stopBtn.display==='flex') 失效
```

### 根因定位

`/Users/lilei/tools/ai-bot/ui/main/windows/assistant/chat.html:926-935` 的 `sendMessage` `finally` 块违背了见缝插针原始计划 Task 4 Step 1 修改 3 的要求，仍保留 UI 状态重置。在见缝插针"HTTP 立即返回"的场景下，`finally` 块抢在 SSE `chat_idle` 之前重置状态，触发 bug。

### 正常路径 vs 见缝插针路径对比

| 路径 | HTTP 返回时机 | finally 执行时 Agent 状态 | finally 重置 stopBtn 是否有害 |
|------|--------------|------------------------|---------------------------|
| 普通消息 | Agent 完成后返回 | 已空闲（SSE chat_idle 已处理） | 重复设置无害（值已为 'none'） |
| 见缝插针补充消息 | 立即返回 | 仍在忙（SSE chat_idle 未到） | **错误**：把 'flex' 改成 'none'，但 Agent 还在跑 |

`finally` 块的"防御兜底"语义只在普通路径下无害，在见缝插针路径下变成 bug。

### 次生影响

1. **Escape 键停止失效**：`isProcessing=false` 后，`chat.html:1528` 的 `Escape` 守卫失效，用户连 Escape 都停不了 Agent
2. **/clear 误走空闲分支**：`chat.html:898-901` 的 `if (isProcessing)` 判断为假，直接 `clearChat()`，可能和正在跑的 Agent 冲突
3. **subagent_msg 误触发**：`chat.html:1462-1476` 的 `if (!isProcessing)` 判断为真，收到 `subagent_msg` SSE 时会误触发新一轮主 Agent 对话，与正在运行的 Agent 抢 `_chat_lock`

### 设计意图确认

原始计划 Task 4 Step 1 修改 3 明确要求 `finally` 块改为：

```javascript
} finally {
  sendBtn.disabled = false;
  loadStats();
  userInput.focus();
}
```

UI 状态（`hideTyping`、`notifyBusy`、`stopBtn.display`、`isProcessing`）由 SSE `chat_idle` / `chat_busy` 单独驱动。

### 拖拽路径验证

主 Agent 已 grep 确认：`handleDroppedImage` / `handleDroppedFile` 这两个函数在 chat.html 中**不存在**（原始计划文档基于已过时的代码描述）。当前拖拽实现 `chat.html:1571-1592` 只把文件路径插入输入框（`insertTextToInput`），不直接触发 HTTP 请求。用户必须按回车，走的就是 `sendMessage` 同一路径。**不需要单独修拖拽代码**。

---

## 文件结构

| 文件 | 职责 | 操作 |
|------|------|------|
| `ui/main/windows/assistant/chat.html` | 删除 `sendMessage` `finally` 块中的 UI 状态重置；保留 `sendBtn.disabled`、`loadStats`、`userInput.focus` | 修改 |
| `tests/manual/test_supplement_stop_button.md` | 手工验证清单（前端无单元测试，记录回归验证步骤） | 新建 |

**不修改的文件**：
- `niu_api/compat.py`（见缝插针后端分支已正确，立即返回 + 不推 chat_idle）
- `niu_api/chat.py`（`notify_new_message` 推 role=user 不推 chat_idle，正确）
- `agent/generic/agent_loop.py`（状态机触发点正确）
- `agent/runner.py`（补充队列实现正确）

---

### Task 1: 删除 sendMessage finally 块的 UI 状态重置

**Files:**
- Modify: `ui/main/windows/assistant/chat.html:926-935`

- [ ] **Step 1: 临时提交备份当前状态**

按 CLAUDE.md 铁律 3（修改前必须先做临时提交备份）：

```bash
cd /Users/lilei/tools/ai-bot
git add -A
git commit -m "backup: 修复见缝插针停止按钮消失bug前临时备份 (基线: HEAD)"
```

验证：`git log -1 --oneline` 应显示刚创建的备份 commit。

- [ ] **Step 2: 修改 sendMessage finally 块**

用 Edit 工具，精确替换 `chat.html:926-935` 的 `finally` 块。

旧代码（第 926-935 行，需精确匹配）：

```javascript
      } finally {
        // 兜底状态重置（SSE chat_idle 为主驱动，此处为防御兜底）
        hideTyping();
        window.electronAPI.notifyBusy(false, 'chat');
        stopBtn.style.display = 'none';
        isProcessing = false;
        sendBtn.disabled = false;
        loadStats();
        userInput.focus();
      }
```

新代码：

```javascript
      } finally {
        // UI 状态（hideTyping/notifyBusy/stopBtn/isProcessing）由 SSE chat_idle 驱动
        // 见缝插针场景下 HTTP 立即返回但 Agent 仍在跑，此处不能重置状态
        // 否则会把停止按钮误隐藏、Escape 失效、/clear 误走空闲分支
        sendBtn.disabled = false;
        loadStats();
        userInput.focus();
      }
```

- [ ] **Step 3: 验证编辑结果**

```bash
cd /Users/lilei/tools/ai-bot
sed -n '920,940p' ui/main/windows/assistant/chat.html
```

预期：`finally` 块只剩 3 行非状态重置代码，且注释完整。

- [ ] **Step 4: 启动应用做手工回归验证**

按 CLAUDE.md 铁律 5（测试必须用真实数据+真实LLM）和 [[real-testing-only]] 记忆：

1. 启动应用：`./niu`
2. 普通消息路径（回归验证，确保正常对话不受影响）：
   - 发送 "你好" → 等待 Agent 回复
   - 验证：回复期间停止按钮显示，回复完成后停止按钮消失
   - 验证：typing 指示在回复期间显示，回复完成后消失
3. 见缝插针路径（核心 bug 验证）：
   - 发送一个需要长时间处理的任务，如 "分析一下文档" 或 "写一个长文档"（让 Agent 跑 30 秒以上）
   - 等 chat_busy 已推、停止按钮显示
   - 在 Agent 跑期间，输入 "补充一条信息" 按回车
   - **核心验证点**：
     - 停止按钮**仍然显示**（`stopBtn.display='flex'`）
     - `isProcessing` 仍为 true
     - 用户气泡正常渲染（addMessage 已执行）
     - typing 指示在 Agent 期间短暂出现，最终被 SSE chat_idle 清除
   - 等 Agent 完成 → 停止按钮消失，回复正常显示
4. Escape 键停止路径（次生影响验证）：
   - 见缝插针补充消息发送后、Agent 还在跑时
   - 按 Escape 键
   - 验证：能触发 `/stop`，Agent 停止
5. /clear 指令路径（次生影响验证）：
   - 见缝插针补充消息发送后、Agent 还在跑时
   - 输入 `/clear` 按回车
   - 验证：走"先 /stop 等 chat_idle 再清空"分支（不是"直接 clearChat"分支）

- [ ] **Step 5: 测试完彻底杀进程**

按 [[kill-processes-after-test]] 和 [[no-pkill-subprocess]] 记忆：

```bash
# 优雅退出，不能用 pkill -f niu（曾导致 LightRAG vdb 损坏）
# ./niu 启动的进程通过应用窗口关闭按钮正常退出即可
# 验证无残留进程：
ps aux | grep -E 'niu|electron' | grep -v grep
```

预期：无残留进程。如有残留，用 `kill -TERM <pid>` 优雅退出。

- [ ] **Step 6: Commit**

```bash
cd /Users/lilei/tools/ai-bot
git add ui/main/windows/assistant/chat.html
git commit -m "$(cat <<'EOF'
fix: 见缝插针回车后停止按钮消失根因修复

sendMessage finally 块违背原始计划 Task 4 设计，仍保留 UI 状态重置。
补充消息路径下后端立即返回 ChatResponse(reply="已收到")，前端 finally
抢在 SSE chat_idle 之前重置状态，导致停止按钮消失、isProcessing 清零。

修复：删除 finally 块的 hideTyping/notifyBusy(false)/stopBtn.display='none'/
isProcessing=false，只保留 sendBtn.disabled/loadStats/userInput.focus。
UI 状态完全由 SSE chat_busy/chat_idle 驱动。

次生影响修复：
- Escape 键停止守卫 (chat.html:1528) 恢复有效
- /clear 在 Agent 忙时正确走"先 /stop 再清空"分支
- subagent_msg SSE 不再误触发新一轮主 Agent 对话

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: 新建手工验证清单文档

**Files:**
- Create: `tests/manual/test_supplement_stop_button.md`

前端 chat.html 无单元测试框架（Electron renderer 进程），按 [[real-testing-only]] 记忆要求"真实程序+真实LLM"，记录手工验证清单作为回归测试基线。

- [ ] **Step 1: 创建 tests/manual 目录（如不存在）**

```bash
cd /Users/lilei/tools/ai-bot
ls tests/manual/ 2>/dev/null || mkdir -p tests/manual
```

- [ ] **Step 2: 写手工验证清单**

使用 Write 工具创建 `tests/manual/test_supplement_stop_button.md`，内容：

```markdown
# 见缝插针 — 停止按钮消失回归验证清单

> 前端 chat.html 无单元测试，按 [[real-testing-only]] 记忆要求用真实程序+真实 LLM 手工验证。

## 前置条件

- 清空会话数据库（避免历史对话污染）：
  ```bash
  # 实际布局（已 Glob 验证）：
  # - 会话 DB：~/.niu/messages.db（直接在根，非 databases/ 子目录）
  # - LightRAG：~/.niu/lightrag_storage/vdb_*.json（独立目录，非 *.db）
  # databases/ 目录不存在，原 rm -rf ~/.niu/databases/*.db 命令无效
  # 严格只删 messages.db 及其 -wal/-shm 辅助文件，绝不碰 lightrag_storage/
  if [ -f ~/.niu/messages.db ]; then
    mkdir -p ~/.niu/messages.db.bak.$(date +%s)
    cp ~/.niu/messages.db ~/.niu/messages.db-wal ~/.niu/messages.db-shm ~/.niu/messages.db.bak.$(date +%s)/ 2>/dev/null || true
    rm -f ~/.niu/messages.db ~/.niu/messages.db-wal ~/.niu/messages.db-shm
    echo "messages.db 已备份并清空"
  fi
  # 验证 lightrag_storage 完好
  ls ~/.niu/lightrag_storage/vdb_*.json | head -3
  ```
  **绝不能**用 `rm -rf ~/.niu/databases/` 或 `rm -rf ~/.niu/lightrag*`——前者无效，后者会永久丢失知识图谱（参见记忆 `lightrag-repair-history-failures.md`：mock测试/探针删数据曾导致 77 节点丢失）。
- 启动应用：`./niu`
- 配置 LLM API Key（如未配置）

## 测试用例

### TC-1: 普通消息路径回归

**步骤**：
1. 在聊天窗口输入 "你好" 按回车
2. 等 Agent 回复完成

**预期**：
- 回复期间停止按钮显示（`stopBtn.display='flex'`）
- 回复期间 typing 指示显示
- 回复完成后停止按钮消失
- 回复完成后 typing 指示消失
- Agent 回复正常出现在对话窗口

### TC-2: 见缝插针 — 核心修复验证

**步骤**：
1. 发送一个工具密集型任务确保 Agent 跑 30 秒以上，如 "用 file-parser 解析 ~/Documents 下所有 PDF 文档并总结"（触发子 Agent 处理多个文件），或 "写一篇 3000 字的散文，分章节输出"
2. 等 chat_busy 已推（停止按钮显示）
3. 在 Agent 跑期间（确认 chat_busy 到达后 5-10 秒内），输入 "补充：风格偏古典" 按回车

> **触发稳定性提示**：单条 LLM 调用可能 5-10 秒返回，难以稳定命中"Agent 跑期间"窗口。优先选工具密集型任务（file-parser 处理多个文件）保证有多个轮次和较长的工具执行期。

**预期**：
- 用户气泡 "补充：风格偏古典" 立即渲染
- 停止按钮**仍然显示**（关键验证点）
- HTTP 请求立即返回（后端入队补充消息）
- Agent 继续跑，回复中体现"古典风格"的影响
- Agent 完成后停止按钮消失

### TC-3: Escape 键停止（次生影响）

**步骤**：
1. 重复 TC-2 步骤 1-3（见缝插针补充消息已发送、Agent 还在跑）
2. 按键盘 Escape 键

**预期**：
- 能触发 `/stop`，Agent 停止
- 停止按钮消失
- Agent 输出"已停止"提示

### TC-4: /clear 在 Agent 忙时（次生影响）

**步骤**：
1. 重复 TC-2 步骤 1-3（见缝插针补充消息已发送、Agent 还在跑）
2. 输入 `/clear` 按回车

**预期**：
- 走"先 /stop 等 chat_idle 再清空"分支（不直接 clearChat）
- 收到 `/stop` 后等 chat_idle 事件
- chat_idle 到达后清空对话窗口

### TC-5: subagent_msg 不误触发（次生影响）

**步骤**：
1. 配置主 Agent 调用子 Agent 的场景（如让主 Agent 调 `file-processor`）
2. 子 Agent 完成后推 `subagent_msg` SSE 事件
3. 此时主 Agent 仍在忙（见缝插针补充消息已发送）

**预期**：
- `subagent_msg` SSE 走 `if (!isProcessing)` 判断为假
- 不触发新一轮主 Agent 对话
- 控制台日志："[Stage2] 收到 subagent_msg SSE 但主 Agent 忙，消息可能已从队列 pop"

## 测试后清理

```bash
# 优雅退出，不用 pkill -f niu
ps aux | grep -E 'niu|electron' | grep -v grep
# 如有残留用 kill -TERM <pid>
```

## 失败诊断

如 TC-2 失败（停止按钮仍消失）：
1. 检查 `chat.html:926-935` finally 块是否还有 `stopBtn.style.display='none'` 或 `isProcessing=false`
2. 检查后端 `niu_api/compat.py` 见缝插针分支是否立即返回（不推 chat_idle）
3. 检查后端 `niu_api/chat.py notify_new_message` 是否推 role=user（不推 chat_idle）
```

- [ ] **Step 3: Commit**

```bash
cd /Users/lilei/tools/ai-bot
git add tests/manual/test_supplement_stop_button.md
git commit -m "$(cat <<'EOF'
test: 新增见缝插针停止按钮回归验证清单

前端 chat.html 无单元测试框架，按 [[real-testing-only]] 记忆要求
用真实程序+真实 LLM 手工验证，记录 5 个测试用例作为回归基线：
- TC-1 普通消息路径回归
- TC-2 见缝插针核心修复验证
- TC-3 Escape 键停止（次生影响）
- TC-4 /clear 在 Agent 忙时（次生影响）
- TC-5 subagent_msg 不误触发（次生影响）

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## 自查清单

### 1. Spec 覆盖度

| 需求 | 对应 Task |
|------|-----------|
| 修复回车后停止按钮消失（核心 bug） | Task 1 Step 2（删除 finally 状态重置） |
| 验证普通消息路径不受影响 | Task 1 Step 4 用例 1 + Task 2 TC-1 |
| 验证见缝插针核心修复 | Task 1 Step 4 用例 2 + Task 2 TC-2 |
| 修复 Escape 键停止失效（次生） | Task 1 Step 2 删 isProcessing=false + Task 2 TC-3 |
| 修复 /clear 误走空闲分支（次生） | Task 1 Step 2 删 isProcessing=false + Task 2 TC-4 |
| 修复 subagent_msg 误触发（次生） | Task 1 Step 2 删 isProcessing=false + Task 2 TC-5 |
| 修改前临时提交备份（铁律 3） | Task 1 Step 1 |
| 测试用真实 LLM（铁律 5） | Task 1 Step 4 |
| 测试完彻底杀进程（记忆） | Task 1 Step 5 |
| 拖拽路径验证（原始计划提到但实际不存在） | 根因分析中已验证：handleDroppedImage/File 不存在，拖拽只 insertTextToInput，走 sendMessage 同路径 |

### Out-of-scope 声明（已分析但不处理）

- **sendMessageWithRetry 10 次连接错误后返回 null**（chat.html:837）：这是 pre-existing 边界行为，原 finally 块也只是兜底重置 UI，不报错。删除 finally 状态重置不会让此场景变得更糟（仍无 addSystemMessage 错误提示）。如需改进，应独立开 ticket。
- **subagent_msg 路径 typing 提示延迟到 chat_busy 到达**（chat.html:1469）：subagent_msg 触发的 sendMessageWithRetry 不经 sendMessage 函数（直接 .catch），不走 showTyping/notifyBusy(true)。删除 finally 状态重置不影响此路径——状态由 chat_busy/chat_idle SSE 正确驱动，仅 typing 提示在 LLM 启动前缺失。可接受退化。
- **SSE 断线重连期间发补充消息状态最终一致性**：onSyncState（chat.html:1509-1524）在窗口恢复时主动同步状态，可保证最终一致。本方案不引入此场景的新风险。


### 2. Placeholder 扫描

无 TBD、TODO、"add validation" 等占位符。所有步骤包含完整代码或具体命令。

### 3. 类型一致性

- `stopBtn` 是 DOM 元素，`style.display` 取 `'flex'` / `'none'`
- `isProcessing` 是 boolean
- `sendBtn.disabled` 是 boolean
- 函数名一致：`sendMessage`、`sendMessageWithRetry`、`clearChat`、`addMessage`、`addSystemMessage`、`showTyping`、`hideTyping`、`loadStats`、`refreshFromDB`、`insertTextToInput`
- SSE 事件名一致：`chat_busy`、`chat_idle`、`subagent_msg`、`role="user"`

### 4. 不变量检查

- **不修改后端**：`niu_api/compat.py`、`niu_api/chat.py`、`agent/` 全部不动
- **不修改其他前端文件**：只动 `chat.html` 一处 `finally` 块
- **不引入新依赖**：纯前端 JS 删除几行代码
- **不破坏正常路径**：正常消息路径下，SSE `chat_idle` 会在 Agent 完成时隐藏停止按钮，删除 `finally` 重置不影响
