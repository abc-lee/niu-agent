# 飞书群聊完善设计

## 目标

完善飞书群聊处理，使机器人能在群聊中正常工作。所有功能仅在 `chat_type == "group"` 时激活，单聊逻辑零修改。

## 核心约束

1. **系统是单用户串行模型** — ChatQueue 串行处理，同一时刻只有一个 Agent 运行，新的 @bot 请求排队等待
2. **不影响单聊** — 所有修改在 `if chat_type == "group"` 分支内
3. **共享会话** — 群里所有人共享一个对话历史（`session_id = "feishu:group:{chat_id}"`）

## 功能清单

### F1: @Bot 过滤

**现状**：群里所有消息都触发 Agent
**改进**：只有 @bot 的消息才触发，其他群消息忽略

**实现**：
- `on_message` 中检查 `chat_type`
- `"p2p"` → 走现有逻辑，不变
- `"group"` → 检查 `msg.mentioned_bot`（SDK 已自动处理 bot_open_id 匹配）
- SDK 标准化后的 `InboundMessage` 结构：
  - `msg.mentions` — `List[Mention]`，每项有 `.open_id`、`.name`、`.is_bot`（不是 `id.open_id`）
  - `msg.mentioned_bot` — 布尔标志，SDK 的 `extract_mentions()` 已处理
- bot 的 `open_id` 从 `self.channel._bot_open_id` 获取（SDK 启动时已自动解析）

**不影响单聊**：检查在 `on_message` 入口处，`chat_type == "p2p"` 直接跳过

### F2: 群聊消息元信息注入

**现状**：群消息和单聊消息处理完全相同，Agent 不知道消息来自谁
**改进**：群聊时在用户消息前注入发送者信息

**实现**：
- 从 `InboundMessage` 中提取发送者 `msg.sender_name` 和 `msg.sender_id`
- 在构建用户消息时，如果是群聊，在 content 前加前缀：`[群聊] 发送者：{name}\n\n{原始内容}`
- sender_name 为空时兜底用 `sender_id[:8]` 作为标识
- @bot 文本清理：SDK 的 `resolve_mentions` 已将 `@_user_1` 替换为 `@机器人名字`。需在群聊分支中调 `extract_mentions` + `resolve_mentions` 清理：
  ```python
  from lark_oapi.channel.normalize.mentions import extract_mentions, resolve_mentions
  ext = extract_mentions(msg.raw.get("mentions", []), bot_open_id=self.channel._bot_open_id)
  cleaned_content = resolve_mentions(content_text, ext, strip_bot_mentions=True, bot_open_id=self.channel._bot_open_id)
  ```

**不影响单聊**：只有 `chat_type == "group"` 时才加前缀

**说明**：流式推送状态不需要改为 dict。ChatQueue 是串行处理模型，同一时刻只有一个活跃请求，当前实例级单值（`_stream_target`、`_stream_open_id` 等）已经正确工作。群聊和单聊不会并发，切换时自然覆盖。

### F3: 群聊回复使用 reply

**现状**：所有消息都用 `send_message` 发送新消息
**改进**：群聊中用 `reply_message` 回复特定消息

**实现**：
- 在 `_on_message` 入口状态重置块中添加 `self._stream_reply_to_id = None`
- 当 `chat_type == "group"` 时，设置 `self._stream_reply_to_id = msg.message_id`
- 在 `_create_stream_card` 中检测 `_stream_reply_to_id`，使用 reply API（与现有 `message.create` 同一调用模式）：
  ```python
  # 文件顶部 import（与 CreateMessageRequest 同一 import 块）
  from lark_oapi.api.im.v1 import (
      CreateMessageRequest, CreateMessageRequestBody,
      ReplyMessageRequest, ReplyMessageRequestBody,
  )

  # _create_stream_card 内部
  if self._stream_reply_to_id:
      reply_req = ReplyMessageRequest.builder() \
          .message_id(self._stream_reply_to_id) \
          .request_body(ReplyMessageRequestBody.builder()
              .msg_type("interactive")
              .content(card_ref)
              .build()) \
          .build()
      send_resp = self.channel.client.im.v1.message.reply(reply_req)
  else:
      # 现有 create 逻辑不变
      send_req = CreateMessageRequest.builder()...
  ```
- 回复卡片创建后，后续流式更新使用 `card_id` 正常更新（不重复 reply）
- 单聊时 `_stream_reply_to_id` 为 None，走现有 `CreateMessageRequest` 逻辑

**说明**：`_create_stream_card` 使用 lark_oapi Client 同步 API + Builder 模式（`im.v1.message.create`），reply 也使用同一模式（`im.v1.message.reply`），调用方式完全一致。不引入新的调用模式。

**不影响单聊**：只有 `chat_type == "group"` 时才设置 `_stream_reply_to_id`

### F4: 定时推送支持群目标

**现状**：定时推送只能推到私聊（通过 `open_id`）
**改进**：支持通过 `chat_id` 推送到群

**实现**：
- 定时推送通过 ChannelRouter 路由。当前 `scheduler/service.py:112` 触发时调 `router.push(agent_reply, "feishu", "")`，channel_id 硬编码空串
- 需要修改的链路：
  1. `task_store.py` — task 表增加 `chat_id` 列（ALTER TABLE 迁移，参照现有 `add_column_if_not_exists` 模式）
  2. `task_store.py` — `create_task()` 方法增加 `chat_id: str = None` 参数，写入数据库
  3. `scheduler/service.py:112` — trigger_callback 从 task 读取 `chat_id`：`chat_id = task.get("chat_id") or ""`，传给 `router.push(agent_reply, "feishu", chat_id)`
  4. `ChannelRouter.push()` — 已支持 `channel_id` 参数，传到 `FeishuChannelAdapter.push()`
  5. `FeishuChannelAdapter.push()` — 已支持 `chat_id` 参数，有 chat_id 时发群消息
  6. `scheduler-server/__init__.py` — `schedule_task` 工具 schema 增加 `chat_id` 属性，函数签名增加 `chat_id: str = None`，传给 `store.create_task()`

**不影响单聊**：`chat_id` 是可选参数，不传时走现有逻辑（空串 → 回退到私聊目标）

### F5: 群聊 session 持久化

**现状**：群聊用 `session_id = "feishu:group:{chat_id}"`，消息已写入数据库
**改进**：确保 session 创建和查询正确

**实现**：
- `session_adapter.py` 的 `add_message` 已支持 `session_id` 参数，写入时传入正确的 `session_id`
- `chat_queue.py:280` 的 `context_manager.get_context_for_chat()` 不按 session_id 过滤，加载全局历史 — 这在共享会话设计下是可接受的
- 群聊消息入库时必须带 `session_id = "feishu:group:{chat_id}"`，确保按群隔离存储
- 验证 `get_context_for_chat()` 的行为：确认群聊和单聊的历史不会互相污染

**不影响单聊**：单聊继续用 `session_id = "feishu:{open_id}"`，入库和查询逻辑不变

## 修改文件清单

| 文件 | 修改内容 | 风险 |
|------|----------|------|
| `niu_api/channel/feishu_channel.py` | F1 @bot过滤 + F2 元信息注入 + F3 reply API + @bot文本清理 | 中 |
| `niu_api/channel/router.py` | F4 push() 传递 chat_id（已支持，可能无需改） | 低 |
| `niu_api/internal/scheduler/service.py` | F4 trigger_callback 传递 chat_id 到 router.push() | 低 |
| `niu_api/internal/scheduler/task_store.py` | F4 task 表增加 chat_id 列 + create_task 增加 chat_id 参数 | 低 |
| `mcp-servers/scheduler-server/src/niu_scheduler_server/__init__.py` | F4 schedule_task 工具增加 chat_id 参数 | 低 |

## 不修改的文件

- `niu_api/chat.py` — SSE 推送基于 session_id，不需要改
- `niu_api/chat_queue.py` — 串行处理逻辑不变，session_id 在调用链上层传入
- `agent/` 目录 — Agent 核心逻辑不需要知道群聊/单聊差异
- `agent/session_adapter.py` — session_id 只是字符串 key，已支持任意前缀
- `mcp-servers/feishu-server/` — 该服务器没有 send_message 工具，消息发送在 feishu_channel.py 中完成
- 流式推送状态变量 — 串行模型下不需要改为 dict，现有单值即可

## 验证标准

1. 单聊行为完全不变（手动测试）
2. 群聊中非 @bot 消息不触发 Agent
3. 群聊中 @bot 消息触发 Agent，回复使用 reply
4. Agent 能识别发送者身份（消息前缀包含发送者名字）
5. 群聊对话历史正确持久化和加载（按 chat_id 隔离）
6. 定时推送能推到群
