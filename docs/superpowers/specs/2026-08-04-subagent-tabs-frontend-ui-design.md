# 动态子 Agent 标签页 — 前端 UI/UE 设计需求

> 日期：2026-08-04
> 状态：需求定义，待 UI/UE 设计实现
> 设计参考：`docs/superpowers/specs/2026-08-03-dynamic-subagent-tabs-design.md` §5

## 1. 总体目标

在现有 Chat 页面（`chat.html`）框架不变的前提下，新增动态子 Agent 标签页。用户可以观察子 Agent 的工作过程（工具调用、thinking chain、回复文本），并在子 Agent 遇到问题 `@user` 提问时与子 Agent 交互。

## 2. 现有框架（不变）

```
.container（flex column）
  .header（头像 + 标题 + 关闭按钮）
  .messages（#messages，消息区，flex:1，overflow-y:auto）
  .progress-bar
  .input-area（textarea + 发送按钮 + 停止按钮 + 斜杠命令下拉）
  .status-bar（上下文使用率 + 文件/人物/笔记计数 + 图谱按钮）
  .resize-handle
```

脑区面板（`.brain-trigger-zone` / `.brain-overlay` / `.brain-panel` / `.brain-spark-container`）在主对话 tab 下正常工作，在子 Agent tab 下隐藏。

## 3. Tab 栏

### 3.1 位置

在 `.header` 和 `.messages` 之间插入一行 tab 栏。`flex-shrink: 0`，不占主消息区空间。

### 3.2 默认状态

只有一个 `[主对话]` tab，就是现有全部内容。tab 高亮（active）样式：白底 + 底部 2px 青色边框。

### 3.3 动态创建

主 Agent 调用子 Agent 时，动态新增 tab，标题为子 Agent 名称（如 `file-processor`、`photo-server`）。新 tab 创建后自动切换过去。

### 3.4 Tab 视觉状态

| 状态 | 视觉表现 |
|---|---|
| 活跃（active） | 白底 + 底部青色边框 + 深色文字 |
| 非活跃 | 灰色文字 + 透明背景 |
| 有未读消息 | 右上角红点（6px 圆形 badge） |
| 子 Agent 运行中 | 文字正常颜色 |
| 子 Agent 等待用户回答（@user） | **文字闪烁/脉动动画 + 红点**，吸引用户注意 |
| 子 Agent 已完成 | 文字半透明（opacity:0.5） |
| 子 Agent 异常终止 | 文字红色 |
| 鼠标悬停 | 浅灰背景 |

### 3.5 Tab 关闭规则

- tab 上**无 × 按钮**，用户不可手动关闭
- 唯一关闭方式：子 Agent 输出 `@end`，tab 标记为已完成（半透明）
- 用户发 `/stop` 终止子 Agent，但 tab 保留
- 用户点击已完成的 tab 仍可查看历史消息

### 3.6 Tab 切换交互

- 点击 tab 切换：只替换 messages 区域内容，页面其他部分不变
- 切到子 Agent tab 时：隐藏脑区面板（`display:none`）
- 切回主对话 tab 时：恢复脑区面板
- 主对话 tab 有 `onclick="switchTab('main')"` 绑定

## 4. 子 Agent Tab 消息区

### 4.1 消息类型与渲染

| 事件类型 | 渲染方式 | 说明 |
|---|---|---|
| `tool_status` (start) | 🔧 工具名 | 系统消息样式（灰色小字） |
| `tool_status` (end) | ✅ 工具名 — 摘要 | 系统消息样式，带摘要 |
| `thinking_chain` | 灰色折叠块，左侧 2px 灰色边框 | 12px 灰色字，`pre-wrap`，可考虑加折叠/展开 |
| `reply` | 白色消息卡片（同主 Agent assistant 消息） | Markdown 渲染 + DOMPurify 净化 |
| `question` (@user) | **黄色高亮卡片** | 背景 rgba(255,193,7,0.15)，边框 rgba(255,193,7,0.4)，圆角 8px |
| `persist` / `system` / `tool_marker` | 系统消息样式 | 灰色小字 |
| `subagent_suspended` | 系统消息："子 Agent 等待主 Agent 回答中..." | 等待主 Agent 回答状态 |
| `subagent_closed` | 系统消息："子 Agent 已结束" + tab 标记完成 | |

### 4.2 消息容器

每个子 Agent tab 有独立的 `#messages-{unique_name}` 容器，与主 `#messages` 同级，`flex:1`，`overflow-y:auto`。切换 tab 时 `display:none/block` 切换。

### 4.3 自动滚动

新消息到达时自动滚动到底部。如果用户手动向上滚动查看历史，不自动滚动（检测 `scrollTop + clientHeight < scrollHeight - threshold`）。

### 4.4 消息去重

子 Agent tab 的消息不走数据库，走 SSE 事件流。不需要 `data-id` 去重（SSE 不会重复推送）。但断线重连时 ring buffer 补发可能重复——补发时前端需检查最后一条消息内容做简单去重。

## 5. 输入栏设计

### 5.1 位置

输入栏位置不变（`.input-area`），但在子 Agent tab 下，发送目标切换为子 Agent。

### 5.2 主对话 Tab 输入

与现有完全一致：`window.electronAPI.sendMessage(text)` → POST /api/chat/session。

### 5.3 子 Agent Tab 输入

调 `POST /api/subagents/{unique_name}/message`。用户消息作为**补充信息**（supplement）插入子 Agent 上下文，不是让子 Agent 回答。

### 5.4 特殊状态：子 Agent @user 提问等待

当子 Agent `@user` 提问时：

| 元素 | 视觉表现 |
|---|---|
| Tab 标题 | **闪烁/脉动动画**（CSS animation），红点 badge |
| 输入栏 | **高亮边框**（黄色 2px），提示用户回答 |
| 输入栏 placeholder | 改为 `"子 Agent 正在等待你的回答..."` |
| 发送按钮 | 正常可用 |
| 停止按钮 | 可用（发送 /stop 终止子 Agent） |

用户回答后：
- Tab 闪烁停止，红点消除
- 输入栏恢复正常边框
- placeholder 恢复默认

### 5.5 指令处理

| 指令 | 主对话 Tab | 子 Agent Tab |
|---|---|---|
| `/stop` | 停止主 Agent | 发给子 Agent（POST API），终止子 Agent |
| `/clear` | 清空主对话 | 提示"请切回主对话使用此命令" |
| `/new` | 新建会话 | 提示"请切回主对话使用此命令" |

### 5.6 sendMessage 函数关键

子 Agent tab 的消息发送逻辑必须在 `addMessage('user', text)` 和 `showTyping()` **之前** early return，否则用户消息会同时渲染到主 #messages（隐藏）和子 Agent 容器，导致消息重复。

## 6. SSE 连接管理

### 6.1 连接生命周期

| 事件 | SSE 操作 |
|---|---|
| 收到 `subagent_started` | 建立独立 SSE 连接 `/api/subagents/{unique_name}/stream` |
| SSE 断开（res.on('end')） | 3 秒自动重连 |
| SSE 错误（req.on('error')） | 3 秒自动重连 |
| SSE 返回 404 | 不重连（子 Agent 不存在） |
| 子 Agent `@end`（subagent_closed 事件） | 断开 SSE 连接 |
| 用户关闭窗口 | 断开所有子 Agent SSE 连接 |
| 窗口重开 | 从 `/api/subagents/running` 获取列表，重建 tab + SSE 连接 |

### 6.2 编码

SSE 连接必须 `res.setEncoding('utf8')`，防止中文多字节字符跨 TCP 块损坏。

### 6.3 断线重连

重连时从 ring buffer（最近 100 条事件）补发历史事件。前端标注"连接已恢复"。

## 7. IPC 通道

### 7.1 新增 IPC 接口

| 接口 | 方向 | Channel | 说明 |
|---|---|---|---|
| `onSubagentStarted(callback)` | main.js → chat.html | `subagent-started` | 子 Agent 启动通知 |
| `onSubagentEvent(callback)` | main.js → chat.html | `subagent-event` | 子 Agent 事件流（含 unique_name + event） |
| `connectSubagentSSE(unique_name)` | chat.html → main.js | `connect-subagent-sse` | 建立 SSE 连接（窗口恢复时） |
| `disconnectSubagentSSE(unique_name)` | chat.html → main.js | `disconnect-subagent-sse` | 断开 SSE 连接（tab 关闭时） |

### 7.2 事件路由

`subagent_started` 是顶级 `event.type`（不是 `new_message` 的 `role` 字段），main.js 事件路由中需要新增 `else if (event.type === 'subagent_started')` 顶级分支。

## 8. 窗口恢复

### 8.1 恢复时机

窗口获得焦点 / 显示时（现有 `onSyncState` 回调），追加 `restoreSubagentTabs()` 调用。

### 8.2 恢复逻辑

1. 调 `GET /api/subagents/running` 获取运行中子 Agent 列表
2. 对每个子 Agent：创建 tab（不自动切换，不显示"工作中"消息）
3. 建立 SSE 连接
4. 用户停留在主对话 tab

### 8.3 历史消息

窗口重开后，tab 标题和状态可恢复，历史消息仅限 ring buffer 内的最近 100 条。完整历史持久化到 db 是已知限制，一期不做。

## 9. 前端关键约束

1. **XSS 防护**：所有 LLM 输出（reply、question）必须经 `DOMPurify.sanitize(marked.parse(text))` 渲染，与主 Agent 消息一致。
2. **脑区面板隐藏**：子 Agent tab 下隐藏 `.brain-trigger-zone` / `.brain-overlay` / `.brain-panel` / `.brain-spark-container`，切回主对话恢复。
3. **clearChat 不影响子 Agent**：`clearChat()` 只清空主 `#messages`，子 Agent tab 的消息容器不在 `#messages` 内部，不受影响。
4. **SubagentSSEManager 位置**：必须在 main.js 模块级变量区域定义（`startMessageEventStream` 之前），确保 `chatWindow.on('closed')` 能引用到。
5. **cancelled 标志**：SSE 连接断开时设 `cancelled=true`，防止 `req.destroy()` 触发的 error/end 回调重连。
6. **sendMessage early return**：子 Agent tab 分支在 `addMessage('user', text)` 之前 early return。

## 10. UI 动效需求

| 场景 | 动效 |
|---|---|
| 新 tab 创建 | 从右滑入（translateX 20px → 0，200ms） |
| Tab 切换 | 消息区淡入淡出（200ms） |
| @user 提问等待 | Tab 标题闪烁动画（opacity 1→0.5→1，1s 循环）+ 红点 |
| @user 提问等待 | 输入栏边框脉冲（box-shadow 黄色，1s 循环） |
| 子 Agent 完成 | Tab 标题渐变半透明（300ms） |
| 新消息未读 | 红点淡入（200ms） |

以上动效用 CSS @keyframes 实现，不用 JS 动画。

## 11. 同步子 Agent 主对话 tab 状态

收到 `is_sync=true` 的 `subagent_started` 事件时，主对话 tab 主动设置"子 Agent 工作中"状态：
- showTyping 气泡文案改为 `"子 Agent 工作中..."`（而非默认的 `"正在思考..."`）
- 这是现有 `showTyping()` 函数的文案参数化改造（`showTyping(text?)`），不是新增 UI 元素
- 恢复时机：子 Agent `@end`（subagent_closed 事件）或主 Agent 收到 `chat_idle`

## 12. 停止按钮行为定义

现有停止按钮有双击批量停止语义（chat.html L1035-1064）：
- 双击：POST `/api/stop_all` 停止所有子 Agent + 停主 Agent
- 单击：只停主 Agent + `checkRunningSubagents()`

子 Agent tab 下停止按钮行为：
- **单击**：发送 `/stop` 到当前子 Agent（POST `/api/subagents/{id}/message` 发 `/stop`）
- **双击**：沿用现有批量停止语义（POST `/api/stop_all` + 停主 Agent），让用户能快速终止所有
- 与主对话 tab 的区别：单击目标从主 Agent 变为当前子 Agent，双击行为不变

## 13. Tab 栏溢出处理

- 多个子 Agent 同时运行时 tab 栏横向滚动（`overflow-x: auto`）
- 新 tab 创建时自动 `scrollIntoView` 到可见区域
- 切换 tab 时自动滚动 active tab 到可见区域
- 可选：左右箭头按钮快速导航（一期可不做）

## 14. 子 Agent tab 空状态

子 Agent tab 刚创建、SSE 尚未推送任何事件时：
- 显示居中占位文案：`"⏳ 子 Agent {name} 正在启动..."`
- 同步子 Agent 可显示：`"⏳ 子 Agent {name} 工作中..."`
- 首条事件到达后移除占位元素

## 15. thinking chain 折叠/展开

- **默认展开**，但限制最大高度（`max-height: 200px` + `overflow-y: auto`）防撑爆消息区
- 超过最大高度时底部显示渐变遮罩 + `"展开全部"` 按钮
- 点击按钮移除 `max-height` 限制，按钮变为 `"收起"`
- 去掉"可考虑"模糊措辞，这是确定方案

## 16. 子 Agent 名称截断

- Tab 标题最大宽度限制：`max-width: 120px`
- 截断样式：`text-overflow: ellipsis` + `white-space: nowrap` + `overflow: hidden`
- hover 时显示 `title` tooltip 全名
- 与现有 brain-item `.name` 截断模式一致

## 17. 键盘导航

- 一期**不支持**键盘左右箭头切换 tab（仅鼠标点击）
- 明确记录为已知限制，二期可加 `tabindex=0` + 左右箭头切换

## 18. 多个子 Agent 同时 @user

- 所有对应 tab 同时闪烁 + 红点（简单方案）
- 一期不增加全局提示条
- 多个闪烁 tab 时，最近闪烁的 tab 闪烁频率/亮度更高（通过 CSS animation-delay 差异化）

## 19. 加载/过渡状态

| 状态 | 视觉表现 |
|---|---|
| SSE 建立中 | tab 标题后加 loading spinner（⏳ 或 CSS spin） |
| 断线重连中 | messages 区顶部系统消息 `"连接已恢复，正在补发历史事件..."` |
| 补发完成 | 正常追加消息，不额外提示 |
| 连接失败（404） | tab 标记为错误状态（红色文字） |

## 20. ring buffer 历史不足提示

- 当 ring buffer 补发的事件恰好是 100 条（满）时，messages 区顶部显示提示：`"仅显示最近 100 条事件，更早历史暂不可查"`
- 不满 100 条则不提示（说明是完整历史）

## 21. @end 关闭后焦点管理与通知

- `@end` 时若用户正在该子 Agent tab：自动切回主对话 tab + 主对话显示系统消息 `"子 Agent {name} 已完成"`（可点击回到该 tab 查看历史）
- `@end` 时若用户在其他 tab：仅该 tab 标记完成 + 红点，不强制切换
- 已完成的 tab 仍可点击查看历史消息

## 22. 发送消息后等待态

子 Agent tab 发送 supplement 消息后：
- 输入栏 placeholder 改为 `"子 Agent 处理补充信息中..."`
- 发送按钮 disabled，直到下一条事件到达（tool_status/reply/thinking_chain 等）
- 子 Agent tab 需独立的 typing 指示：在 `#messages-{id}` 内创建状态气泡（不能复用主 #messages 的 status-bubble）
- 下一条事件到达后恢复：placeholder 默认、发送按钮 enabled、移除 typing 指示

## 23. 视觉一致性规格

子 Agent tab 的 `#messages-{id}` 容器复用主 `#messages` 的全部 `.message` CSS（字号、间距、圆角、颜色方案），不重新定义。仅新增两个子类型样式：

```css
/* thinking chain 折叠块 */
.message.thinking {
  font-size: 12px;
  color: #888;
  border-left: 2px solid #ccc;
  padding-left: 8px;
  margin: 4px 0;
  white-space: pre-wrap;
  max-height: 200px;
  overflow-y: auto;
  position: relative;
}

/* @user 提问高亮卡片 */
.message.question {
  background: rgba(255, 193, 7, 0.15);
  border: 1px solid rgba(255, 193, 7, 0.4);
  border-radius: 8px;
  padding: 8px 12px;
  margin: 4px 0;
}
```

## 24. 响应式布局

- 沿用现有 `.container` flex column 响应式行为，不额外处理
- tab 栏 `flex-shrink: 0` 固定高度，横向滚动处理溢出
- 消息区 `flex: 1` 自适应，保证最小可读高度
- 一期不定义最小窗口尺寸约束
