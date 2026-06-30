# IM Gateway 接入文档

本文档面向第三方开发者，说明如何为 Niu AI Bot 对接新的 IM 平台（钉钉、Telegram、企业微信等）。

---

## 1. 架构概览

IM 通信层采用 **Gateway + Adapter** 分离架构：

```
┌─────────────────────────────────────────────────┐
│                 niu_api 进程                      │
│                                                   │
│  ┌───────────┐    ┌──────────┐    ┌───────────┐  │
│  │ ChatQueue │◄───│  IM      │    │  Agent    │  │
│  │ (消息队列) │    │  Gateway │◄───│  Core     │  │
│  └───────────┘    │ (TCP Srv)│    └───────────┘  │
│                   └────┬─────┘                    │
│                        │ TCP 长连接                │
│                   127.0.0.1:19877                  │
└────────────────────────┼──────────────────────────┘
                         │
┌────────────────────────┼──────────────────────────┐
│   Adapter 子进程       │                          │
│                        ▼                          │
│  ┌─────────────────────────┐                      │
│  │   IM Adapter (TCP Clt)  │                      │
│  │   ┌───────────────────┐ │                      │
│  │   │  IM 平台 SDK      │ │                      │
│  │   │  (飞书/钉钉/...)  │ │                      │
│  │   └───────────────────┘ │                      │
│  └─────────────────────────┘                      │
└───────────────────────────────────────────────────┘
```

**关键设计**：

- **Gateway** 是内嵌在 niu_api 进程中的 TCP Server，监听 `127.0.0.1:19877`
- **Adapter** 是独立子进程，由 Gateway 自动拉起（`subprocess.Popen`），不需要手动启动
- 两者通过 TCP 长连接通信，协议为 **4 字节大端长度前缀 + UTF-8 JSON**
- Gateway 不知道任何具体 IM 平台的存在，所有平台逻辑封装在 Adapter 中
- Gateway 负责 Adapter 子进程的健康检查和自动重启（最多 3 次）
- **Gateway 自动维护 reply_to_id 映射**：内部维护 `_reply_to_ids` 字典（channel_id → reply_to_id），收到 Adapter 的 MSG 指令时记录映射关系；当 Agent 发送 SEND/STREAM 指令时，Gateway 自动注入对应的 `reply_to_id` 字段并消费该映射，使出方向消息能定位到入方向消息（群聊回复场景）。Adapter 无需自行管理回复关系。

---

## 2. 通信协议

### 2.1 帧格式

每条消息由 **长度头 + 正文** 两部分组成：

```
┌──────────────┬────────────────────────────┐
│  4 bytes     │  N bytes                   │
│  (big-endian)│  (UTF-8 JSON)              │
│  消息体长度   │  消息正文                   │
└──────────────┴────────────────────────────┘
```

- 长度头：4 字节无符号整数，大端序（`int.to_bytes(4, "big")`）
- 正文：UTF-8 编码的 JSON 字符串
- 最大消息大小：**10 MB**（超出此限制连接会被断开）

发送示例（Python）：

```python
payload = json.dumps(cmd, ensure_ascii=False).encode("utf-8")
writer.write(len(payload).to_bytes(4, "big") + payload)
await writer.drain()
```

接收示例（Python）：

```python
header = await reader.readexactly(4)
length = int.from_bytes(header, "big")
data = await reader.readexactly(length)
msg = json.loads(data.decode("utf-8"))
```

### 2.2 Adapter → Gateway 指令

#### MSG — 入方向消息

IM 平台收到用户消息后，Adapter 构造此指令发送给 Gateway。

```json
{
  "type": "MSG",
  "content": "用户发送的文本内容",
  "channel_id": "会话ID（飞书为 chat_id）",
  "sender_id": "发送者ID（飞书为 open_id）",
  "session_id": "会话标识，格式: {channel}:{sender_id} 或 {channel}:group:{chat_id}",
  "is_group": false,
  "reply_to_id": "被回复的消息ID（群聊时设置，可选）"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `content` | string | 是 | 消息文本，图片/文件以 Markdown 格式嵌入 |
| `channel_id` | string | 是 | IM 平台的会话标识 |
| `sender_id` | string | 是 | 消息发送者在 IM 平台的标识 |
| `session_id` | string | 是 | Agent 会话路由标识，决定消息进入哪个会话上下文 |
| `is_group` | boolean | 是 | 是否群聊消息 |
| `reply_to_id` | string | 否 | 群聊时设置，用于出方向消息的回复定位 |

**session_id 规则**：
- 私聊：`{adapter_name}:{sender_id}`（如 `feishu:ou_xxx`）
- 群聊：`{adapter_name}:group:{chat_id}`（如 `feishu:group:oc_xxx`）

#### READY — Adapter 就绪通知

Adapter 连接 Gateway 后，必须发送此指令声明身份。

```json
{
  "type": "READY",
  "adapter": "feishu",
  "push_target": "oc_default_chat"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `adapter` | string | 是 | 适配器名称，与配置中 `im.adapter` 一致 |
| `push_target` | string | 否 | 默认推送目标（私聊 chat_id），用于 PUSH 的兜底目标 |

Gateway 收到 READY 后会重放缓冲期间积压的 SEND/PUSH 消息。

#### PING — 心跳

```json
{"type": "PING"}
```

Gateway watchdog 每 10 秒向 Adapter 发送 PING，Adapter 应回复 PONG（`{"type": "PONG"}`）。

> ⚠️ **当前实现隐患**：飞书 Adapter 的 `_dispatch` 仅处理 STREAM/SEND/PUSH，**未实现 PING 的接收与 PONG 回复**。新 Adapter 必须在 `_dispatch` 中处理 PING 并回 PONG，否则在长时间空闲后可能被 Gateway 判定为连接异常。

### 2.3 Gateway → Adapter 指令

#### SEND — 最终回复

Agent 完成回复后，Gateway 发送最终完整内容。

```json
{
  "type": "SEND",
  "channel_id": "oc_xxx",
  "content": "完整的回复文本（含 Markdown 图片/文件标记）",
  "reply_to_id": "om_xxx"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `channel_id` | string | 目标会话 ID |
| `content` | string | 完整回复文本 |
| `reply_to_id` | string | 群聊时设置，回复目标消息 ID |

#### STREAM — 流式增量内容

Agent 流式输出过程中，Gateway 逐步推送增量内容。

```json
{
  "type": "STREAM",
  "channel_id": "oc_xxx",
  "content": "本次新增的文本片段",
  "is_final": false,
  "reply_to_id": "om_xxx"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `channel_id` | string | 目标会话 ID |
| `content` | string | 增量文本（空字符串表示保活信号） |
| `is_final` | boolean | 是否为最后一片段 |
| `reply_to_id` | string | 群聊时设置 |

Adapter 应在 STREAM 阶段创建可更新的消息卡片（或等效机制），在 SEND 阶段终结卡片。

#### PUSH — 主动推送

非对话触发的主动推送（定时提醒、系统通知等）。

```json
{
  "type": "PUSH",
  "channel_id": "",
  "content": "推送文本内容"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `channel_id` | string | 推送目标 ID（空则使用 READY 中声明的 push_target） |
| `content` | string | 推送文本 |

#### PONG — 心跳回复

```json
{"type": "PONG"}
```

---

## 3. 配置格式

配置统一存放在 `~/.niu/preferences.json` 中。

### 3.1 IM 通用配置

```json
{
  "im": {
    "enabled": true,
    "gateway_port": 19877,
    "adapter": "feishu"
  }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `im.enabled` | boolean | 是否启用 IM 功能 |
| `im.gateway_port` | int | Gateway TCP 端口，默认 19877 |
| `im.adapter` | string | 要启动的适配器名称，对应 `im-adapters/` 下的目录名 |

### 3.2 适配器专属配置

每个适配器在 preferences.json 中拥有一个同名的顶级配置段，存放该平台的凭证和参数：

```json
{
  "im": {
    "enabled": true,
    "gateway_port": 19877,
    "adapter": "feishu"
  },
  "feishu": {
    "app_id": "cli_xxx",
    "app_secret": "xxx",
    "enabled": true,
    "user_p2p_chat_id": "oc_xxx",
    "user_open_id": "ou_xxx"
  }
}
```

可选字段说明：

| 字段 | 类型 | 说明 |
|------|------|------|
| `user_p2p_chat_id` | string | 用户私聊 chat_id（运行时自动写回，首次配置无需填写） |
| `user_open_id` | string | 用户 open_id（运行时自动写回，首次配置无需填写） |

> 说明：飞书 Adapter 会在运行时将首个发消息用户的 `user_p2p_chat_id` 和 `user_open_id` 写回到 `preferences.json`，用于后续主动推送。Gateway 通过环境变量 `NIU_{ADAPTER_NAME}_USER_P2P_CHAT_ID` / `NIU_{ADAPTER_NAME}_USER_OPEN_ID` 传给 Adapter。
```

配置关系说明：

- `im.adapter` 的值（如 `"feishu"`）决定了启动哪个 Adapter 子进程
- Gateway 启动时读取 `{adapter_name}` 段（如 `feishu`）中的凭证，通过环境变量传给 Adapter
- `im.adapter` 的值必须与 `im-adapters/` 目录下的子目录名一致

### 3.3 钉钉配置示例

```json
{
  "im": {
    "enabled": true,
    "gateway_port": 19877,
    "adapter": "dingtalk"
  },
  "dingtalk": {
    "app_id": "dingxxx",
    "app_secret": "xxx",
    "enabled": true
  }
}
```

> 注意：钉钉官方 SDK 使用 `client_id` / `client_secret` 命名，但 **Gateway 当前仅自动传递 `app_id` 和 `app_secret` 两个字段**（硬编码在 `_launch_adapter` 中）。请在 `preferences.json` 中使用 `app_id` / `app_secret` 作为键名存放钉钉的 Client ID / Client Secret，否则 Gateway 不会将凭证传给 Adapter。

---

## 4. Adapter 目录规范

```
im-adapters/
└── {adapter_name}/              # 如 feishu、dingtalk、telegram
    ├── src/
    │   └── niu_{adapter_name}_adapter/
    │       ├── __init__.py       # 包初始化
    │       ├── __main__.py       # 入口点：python -m niu_{adapter_name}_adapter
    │       ├── adapter.py        # 主逻辑（连接 Gateway、消息转发）
    │       └── {adapter_name}_api.py  # IM 平台 API 封装（可选）
    └── pyproject.toml            # 包配置
```

**命名约定**：

- 目录名：小写，如 `feishu`、`dingtalk`、`telegram`
- Python 包名：`niu_{adapter_name}_adapter`，如 `niu_feishu_adapter`
- 入口模块：`__main__.py`，支持 `python -m niu_{adapter_name}_adapter` 启动

**pyproject.toml 模板**：

```toml
[project]
name = "niu-{adapter_name}-adapter"
version = "0.1.0"
description = "{Adapter名称} IM Adapter for Niu AI Bot"
requires-python = ">=3.11"
dependencies = [
    "loguru>=0.7.0",
    # IM 平台 SDK
]

[build-system]
requires = ["setuptools>=68.0"]
build-backend = "setuptools.build_meta"
```

---

## 5. 环境变量

Gateway 启动 Adapter 子进程时，自动设置以下环境变量：

| 环境变量 | 示例值 | 说明 |
|----------|--------|------|
| `NIU_IM_ADAPTER` | `feishu` | 适配器名称，与 `im.adapter` 配置一致 |
| `NIU_GATEWAY_PORT` | `19877` | Gateway TCP 端口 |
| `NIU_{ADAPTER_NAME}_APP_ID` | `cli_xxx` | 从 `preferences.json` 中 `{adapter_name}.app_id` 读取 |
| `NIU_{ADAPTER_NAME}_APP_SECRET` | `xxx` | 从 `preferences.json` 中 `{adapter_name}.app_secret` 读取 |
| `NIU_{ADAPTER_NAME}_USER_P2P_CHAT_ID` | `oc_xxx` | 私聊 chat_id（可选，首次连接后由 Adapter 写回） |
| `NIU_{ADAPTER_NAME}_USER_OPEN_ID` | `ou_xxx` | 用户 open_id（可选，首次连接后由 Adapter 写回） |
| `PYTHONPATH` | `/path/to/im-adapters/feishu/src` | 包含 Adapter 的 `src/` 目录 |

**环境变量命名规则**：

- 适配器名称转为大写后拼接：`NIU_{ADAPTER_NAME_UPPER}_xxx`
- 例如 `feishu` 的 app_id → `NIU_FEISHU_APP_ID`
- 例如 `dingtalk` 的 client_id → 如果配置键名为 `client_id`，当前 Gateway 仅自动传递 `app_id` 和 `app_secret`，其他自定义字段需要扩展 Gateway 的 `_launch_adapter` 方法

---

## 6. 开发新 Adapter 的步骤

以对接钉钉（DingTalk）为例。

### 步骤 1：创建目录结构

```bash
mkdir -p im-adapters/dingtalk/src/niu_dingtalk_adapter
```

创建以下文件：

- `im-adapters/dingtalk/src/niu_dingtalk_adapter/__init__.py`
- `im-adapters/dingtalk/src/niu_dingtalk_adapter/__main__.py`
- `im-adapters/dingtalk/src/niu_dingtalk_adapter/adapter.py`
- `im-adapters/dingtalk/src/niu_dingtalk_adapter/dingtalk_api.py`（可选）
- `im-adapters/dingtalk/pyproject.toml`

### 步骤 2：实现 Adapter 类

`adapter.py` 需要实现以下核心功能：

1. **连接 Gateway**：TCP 连接到 `127.0.0.1:{gateway_port}`，最多重试 30 次
2. **发送 READY**：连接成功后声明身份
3. **启动 IM 平台监听**：注册消息回调，收到消息后构造 MSG 指令
4. **处理出方向指令**：分发 SEND/STREAM/PUSH，调用 IM 平台 API 发送

核心结构：

```python
class DingtalkAdapter:
    def __init__(self, gateway_port, client_id, client_secret):
        self._gateway_port = gateway_port
        self._client_id = client_id
        self._client_secret = client_secret
        self._reader = None
        self._writer = None
        self._write_lock = asyncio.Lock()

    async def run(self):
        self._init_sdk()           # 初始化钉钉 SDK
        await self._connect_gateway()
        await self._send_ready()
        self._start_listener()     # 启动钉钉消息监听
        await self._read_loop()    # 进入 Gateway 指令读取循环
```

### 步骤 3：编写入口点

`__main__.py` 从环境变量读取配置，创建 Adapter 实例并运行：

```python
def main():
    adapter_type = os.environ.get("NIU_IM_ADAPTER", "")
    if adapter_type != "dingtalk":
        logger.error(f"NIU_IM_ADAPTER={adapter_type}, expected 'dingtalk'")
        sys.exit(2)  # 永久错误，Gateway 不会重启

    port = int(os.environ.get("NIU_GATEWAY_PORT", ""))
    client_id = os.environ.get("NIU_DINGTALK_APP_ID", "")
    client_secret = os.environ.get("NIU_DINGTALK_APP_SECRET", "")

    if not client_id or not client_secret:
        logger.error("Missing credentials")
        sys.exit(2)

    adapter = DingtalkAdapter(port, client_id, client_secret)
    asyncio.run(adapter.run())

if __name__ == "__main__":
    main()
```

**退出码约定**：

| 退出码 | 含义 | Gateway 行为 |
|--------|------|-------------|
| 0 | 正常退出 | 不重启 |
| 1 | 瞬时错误 | 自动重启（最多 3 次） |
| 2 | 永久错误 | 不重启 |

### 步骤 4：配置 preferences.json

```json
{
  "im": {
    "enabled": true,
    "gateway_port": 19877,
    "adapter": "dingtalk"
  },
  "dingtalk": {
    "app_id": "你的 Client ID",
    "app_secret": "你的 Client Secret",
    "enabled": true
  }
}
```

> 注意：Gateway 当前仅自动传递 `app_id` 和 `app_secret` 两个字段。如果你的平台使用不同的凭证名称（如 `client_id` / `client_secret`），有两种处理方式：
> 1. 在 `preferences.json` 中仍使用 `app_id` / `app_secret` 作为键名（推荐，无需改 Gateway）
> 2. 扩展 Gateway 的 `_launch_adapter` 方法，增加自定义字段的传递

### 步骤 5：启动测试

启动 Niu 主程序（`./niu`），观察日志确认：

1. Gateway TCP Server 启动成功（`IMGateway TCP Server listening on 127.0.0.1:19877`）
2. Adapter 子进程被拉起（`Adapter launched: dingtalk, PID=xxx`）
3. Adapter 连接 Gateway（`Adapter connected from 127.0.0.1:xxx`）
4. Adapter 发送 READY（`Adapter ready: dingtalk`）

---

## 7. 消息流转说明

### 入方向（IM 消息 → Agent）

```
用户在 IM 发送消息
       │
       ▼
Adapter 收到 IM 平台回调
       │
       ├── 解析消息文本
       ├── 下载图片/文件到本地（~/.niu/tmp/）
       ├── 构造 Markdown 格式内容（图片用 ![alt](path)，文件用 [name](path)）
       │
       ▼
构造 MSG 指令，通过 TCP 发送给 Gateway
       │
       ▼
Gateway._on_msg() 解析 MSG
       │
       ▼
ChannelRouter.route_in_sync() 入队到 ChatQueue
       │
       ▼
ChatQueue Worker 调用 Agent 处理
```

### 出方向（Agent → IM 消息）

```
Agent 生成回复
       │
       ├── 流式输出 → Gateway.notify_stream() → STREAM 指令 → Adapter 更新卡片
       │
       ▼
Agent 完成回复
       │
       ▼
ChannelRouter.route_out() → Gateway.send() → SEND 指令
       │
       ▼
Adapter 收到 SEND
       │
       ├── 有 CardState（经过 STREAM 阶段）：
       │     ├── 解析 Markdown 图片标记：![alt](path)
       │     ├── 上传图片到 IM 平台获取 img_key
       │     ├── 上传文件到 IM 平台获取 file_key
       │     ├── 替换 Markdown 标记为 IM 平台格式
       │     └── 终结卡片（将图片嵌入卡片 body）
       │
       └── 无 CardState（如推送通知、非流式回复、跳过 STREAM）：
             ├── 直接走 send_markdown / send_markdown_reply 发送纯文本
             └── 清理内部图片标记（无卡片可终结）
```

### 主动推送（定时提醒等）

```
定时任务 / 系统事件触发
       │
       ▼
ChannelRouter.push() → Gateway.push() → PUSH 指令
       │
       ▼
Adapter 收到 PUSH
       │
       ├── channel_id 非空（override_id） → 直接发送到指定会话
       │
       └── channel_id 为空 → 使用 push_target：
             ├── 优先尝试 push_target 作为 open_id 发送 P2P 消息
             ├── open_id 发送失败 → 自动回退用 chat_id 发送
             └── P2P 消息发送成功后，更新 push_target 到 preferences.json
```

**push 目标优先级**：`override_id`（PUSH 指令中的 channel_id）→ `open_id` → `chat_id`。open_id 发送失败时自动回退到 chat_id，确保推送可达。当用户首次与 Adapter 交互后，Adapter 会将有效的 open_id 写回 `preferences.json` 的 `push_target` 字段，供后续 PUSH 使用。

---

## 8. 图片和文件处理

### 约定

图片和文件统一通过 Markdown 格式在文本中传递：

- **图片**：`![描述文字](本地文件路径)` — 如 `![照片](/Users/xxx/.niu/tmp/image_001.jpg)`
- **文件**：`[文件名](本地文件路径)` — 如 `![报告.pdf](/Users/xxx/.niu/tmp/doc_001.pdf)`

Adapter 负责将本地文件上传到 IM 平台并替换标记。

### 入方向（Adapter → Gateway）

Adapter 收到 IM 平台的图片/文件消息后：

1. 调用 IM 平台 API 下载资源到本地（`~/.niu/tmp/`）
2. 将本地路径嵌入 Markdown 格式：
   - 图片：`入库照片：![图片](/path/to/local/file.jpg)`
   - 文件：`入库文件：[文件名.pdf](/path/to/local/file.pdf)`
3. 将完整文本放入 MSG 的 `content` 字段

### 出方向（Gateway → Adapter）

STREAM 阶段：

- Adapter 收到增量文本，直接显示（图片/文件标记原样展示）
- 不在流式阶段上传图片（原因：增量 chunk 中标记可能不完整，且会造成重复上传）

SEND 阶段（终结）：

- Adapter 收到完整回复文本
- 解析所有 Markdown 图片/文件标记
- 上传本地文件到 IM 平台获取平台标识（如飞书的 `img_key` / `file_key`）
- 替换 Markdown 标记为 IM 平台格式
- 终结卡片（将图片嵌入卡片 body）或发送独立消息
- 文件单独发送为文件消息

### 处理建议

- 图片上传失败的，建议在终结后重试发送独立图片消息（参考飞书 Adapter 的 `failed_images` 逻辑）
- 文本过长时需要截断（飞书卡片限制约 18000 字符，其他平台参考各自文档）
- 文件发送应为独立消息，不嵌入文本卡片

### 图片自动压缩

飞书 Adapter 在上传图片前会自动检查文件大小：

- 文件 ≤ 10MB：直接上传原图
- 文件 > 10MB：使用 PIL 压缩为 JPEG 格式，quality 从 85 起逐级递降（85 → 75 → 65 → ... → 25），直到压缩后体积 ≤ 10MB 或 quality 降至 25
- 压缩产生的临时文件上传完成后自动删除，不残留

> 该机制仅在飞书 Adapter 中实现，新 Adapter 如需支持大图上传，应参考 `feishu_api.py:compress_image` 自行实现类似逻辑。

---

## 附录：Gateway 自动管理机制

| 机制 | 说明 |
|------|------|
| 子进程拉起 | Gateway 启动时根据 `im.adapter` 配置自动 `subprocess.Popen` |
| 健康检查 | 每 10 秒 PING + 检查子进程状态 |
| 自动重启 | 子进程异常退出（退出码=1）最多重启 3 次 |
| 永久错误 | 退出码=2 不重启（凭证缺失、配置错误等） |
| 缓冲重放 | Adapter 未连接时，SEND/PUSH 缓冲在 deque(maxlen=10)，READY 后重放 |
| 稳定重置 | 连接稳定 60 秒后重置重启计数器 |
