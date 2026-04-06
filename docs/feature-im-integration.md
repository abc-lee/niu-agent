# IM 消息接入 - 详细设计

> 版本：v1.0
> 日期：2026-03-22
> 状态：详细设计完成

---

## 一、设计理念

### 1.1 核心价值

**助理随时可用，不仅限于桌面。**

| 场景 | 传统方式 | IM 接入后 |
|------|----------|-----------|
| 外出开会 | 没办法用助理 | 手机拍照 → 飞书发送 → 自动处理 |
| 通勤路上 | 无法访问电脑 | 手机提问 → 飞书回复 |
| 紧急查询 | 等回到电脑 | 随时随地问助理 |

### 1.2 支持的平台

| 平台 | 状态 | 方式 |
|------|------|------|
| **飞书** | ✅ 优先支持 | WebSocket 长连接 |
| **钉钉** | ✅ 优先支持 | Stream Mode |
| 微信 | ❌ 不支持 | 封号风险 |
| Telegram | 🔜 可选支持 | Bot API |

---

## 二、架构设计

### 2.1 整体架构

```
┌─────────────────────────────────────────────────────────────────┐
│                        IM 平台                                   │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐              │
│  │ 飞书机器人   │  │ 钉钉机器人   │  │ Telegram Bot │              │
│  │ (WebSocket) │  │ (Stream)    │  │ (Webhook)   │              │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘              │
│         │                │                │                      │
└─────────┼────────────────┼────────────────┼──────────────────────┘
          │                │                │
          └────────────────┼────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Channel Plugin 层                             │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  ChannelPlugin 接口（统一消息处理）                        │   │
│  │  ├── start()      启动监听                                │   │
│  │  ├── stop()       停止监听                                │   │
│  │  ├── send()       发送消息                                │   │
│  │  └── on_message() 消息回调                                │   │
│  └─────────────────────────────────────────────────────────┘   │
│  ┌────────────┐   ┌────────────┐   ┌────────────┐              │
│  │ Feishu     │   │ DingTalk   │   │ Telegram   │              │
│  │ Channel    │   │ Channel    │   │ Channel    │              │
│  └────────────┘   └────────────┘   └────────────┘              │
└─────────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                    消息路由层                                    │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  MessageRouter                                           │   │
│  │  ├── 文字消息 → Agent 对话处理                            │   │
│  │  ├── 图片消息 → 人脸识别 Pipeline                         │   │
│  │  ├── 文件消息 → 文档解析 Pipeline                         │   │
│  │  └── 其他消息 → 提示不支持                                │   │
│  └─────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Agent 核心 (Go)                               │
│  - 处理消息                                                     │
│  - 调用工具                                                     │
│  - 返回结果                                                     │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 Channel Plugin 接口

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Awaitable

@dataclass
class IncomingMessage:
    """入站消息"""
    message_id: str
    sender_id: str
    chat_id: str
    content: str
    msg_type: str  # text/image/file
    media: list[str] | None  # 文件路径列表
    metadata: dict[str, Any]  # 平台特定信息

@dataclass
class OutgoingMessage:
    """出站消息"""
    content: str
    reply_to: str  # 回复目标（chat_id 或 user_id）
    msg_type: str = "text"
    media: list[str] | None = None
    metadata: dict[str, Any] | None = None

class ChannelPlugin(ABC):
    """频道插件基类"""
    
    name: str  # 频道名称
    
    @abstractmethod
    async def start(self) -> None:
        """启动频道（开始监听消息）"""
        pass
    
    @abstractmethod
    async def stop(self) -> None:
        """停止频道"""
        pass
    
    @abstractmethod
    async def send(self, message: OutgoingMessage) -> None:
        """发送消息"""
        pass
    
    def set_message_handler(
        self, 
        handler: Callable[[IncomingMessage], Awaitable[None]]
    ) -> None:
        """设置消息处理器"""
        self._message_handler = handler
    
    async def _on_message(self, message: IncomingMessage) -> None:
        """收到消息时调用"""
        if hasattr(self, '_message_handler'):
            await self._message_handler(message)
```

---

## 三、飞书集成

### 3.1 接入方式

**WebSocket 长连接** — 无需公网 IP，无需配置 Webhook。

```
┌───────────┐     WebSocket      ┌───────────┐
│ 飞书服务器 │◄──────────────────►│ 本地助理   │
└───────────┘                    └───────────┘
     │                                 │
     │ 推送消息事件                    │
     │ ──────────────────────────────►│
     │                                 │
     │                                 │ 处理消息
     │                                 │
     │◄────────────────────────────── │ 回复消息
     │  HTTP API                      │
     └───────────┘                    
```

### 3.2 配置要求

```yaml
# config/channels.yaml
channels:
  feishu:
    enabled: true
    app_id: "cli_xxx"           # 飞书应用 App ID
    app_secret: "xxx"           # 飞书应用 App Secret
    encrypt_key: ""             # 加密密钥（可选）
    verification_token: ""      # 验证令牌（可选）
```

### 3.3 飞书应用配置

**在飞书开放平台创建应用：**

1. 创建企业自建应用
2. 开启机器人能力
3. 配置事件订阅：`im.message.receive_v1`
4. 获取 App ID 和 App Secret

### 3.4 消息类型处理

| 消息类型 | 处理方式 |
|----------|----------|
| **text** | 直接转发给 Agent |
| **image** | 下载图片 → 人脸识别 Pipeline |
| **post** | 提取文本和图片 → 分别处理 |
| **file** | 下载文件 → 文档解析 Pipeline |
| **audio** | 语音转文字 → Agent 处理 |

### 3.5 飞书 Channel 实现

```python
from lark_oapi import Client as LarkClient
import lark_oapi as lark

class FeishuChannel(ChannelPlugin):
    """飞书频道"""
    
    name = "feishu"
    
    def __init__(self, config: dict):
        self.config = config
        self._client: LarkClient | None = None
        self._ws_client: Any = None
        self._running = False
    
    async def start(self) -> None:
        """启动飞书机器人"""
        self._running = True
        
        # 创建 Lark 客户端
        self._client = (
            lark.Client.builder()
            .app_id(self.config['app_id'])
            .app_secret(self.config['app_secret'])
            .build()
        )
        
        # 创建事件处理器
        event_handler = (
            lark.EventDispatcherHandler.builder(
                self.config.get('encrypt_key', ''),
                self.config.get('verification_token', '')
            )
            .register_p2_im_message_receive_v1(self._on_message_sync)
            .build()
        )
        
        # 创建 WebSocket 客户端
        self._ws_client = lark.ws.Client(
            self.config['app_id'],
            self.config['app_secret'],
            event_handler=event_handler
        )
        
        # 启动 WebSocket（在后台线程）
        import threading
        def run_ws():
            while self._running:
                try:
                    self._ws_client.start()
                except Exception as e:
                    logger.error(f"WebSocket error: {e}")
                if self._running:
                    time.sleep(5)
        
        thread = threading.Thread(target=run_ws, daemon=True)
        thread.start()
        
        logger.info("Feishu channel started")
    
    async def stop(self) -> None:
        """停止飞书机器人"""
        self._running = False
        logger.info("Feishu channel stopped")
    
    def _on_message_sync(self, data):
        """同步消息处理器（在 WebSocket 线程中调用）"""
        import asyncio
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._handle_message(data), 
                self._loop
            )
    
    async def _handle_message(self, data):
        """处理飞书消息"""
        event = data.event
        message = event.message
        sender = event.sender
        
        # 跳过机器人消息
        if sender.sender_type == "bot":
            return
        
        # 解析消息内容
        msg_type = message.message_type
        content = ""
        media = []
        
        if msg_type == "text":
            import json
            content = json.loads(message.content).get("text", "")
        
        elif msg_type == "image":
            # 下载图片
            image_key = json.loads(message.content).get("image_key")
            image_bytes = await self._download_image(image_key, message.message_id)
            image_path = await self._save_image(image_bytes)
            media.append(image_path)
            content = "[图片]"
        
        # 构建消息
        incoming = IncomingMessage(
            message_id=message.message_id,
            sender_id=sender.sender_id.open_id,
            chat_id=message.chat_id,
            content=content,
            msg_type=msg_type,
            media=media if media else None,
            metadata={
                "chat_type": message.chat_type,
                "platform": "feishu"
            }
        )
        
        # 调用消息处理器
        await self._on_message(incoming)
    
    async def send(self, message: OutgoingMessage) -> None:
        """发送飞书消息"""
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest,
            CreateMessageRequestBody
        )
        
        # 构建消息内容
        content = json.dumps({
            "text": message.content
        }, ensure_ascii=False)
        
        # 发送
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id" if message.reply_to.startswith("oc_") else "open_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(message.reply_to)
                .msg_type("text")
                .content(content)
                .build()
            )
            .build()
        )
        
        response = self._client.im.v1.message.create(request)
        if not response.success():
            logger.error(f"Failed to send message: {response.msg}")
```

### 3.6 飞书富消息支持

飞书支持发送富文本消息（卡片消息）：

```python
async def send_card(self, reply_to: str, title: str, content: str):
    """发送飞书卡片消息"""
    card = {
        "config": {"wide_screen_mode": True},
        "elements": [
            {"tag": "div", "text": {"content": content, "tag": "lark_md"}}
        ]
    }
    
    if title:
        card["header"] = {
            "title": {"tag": "plain_text", "content": title},
            "template": "blue"
        }
    
    content_json = json.dumps(card, ensure_ascii=False)
    
    request = (
        CreateMessageRequest.builder()
        .receive_id_type("chat_id")
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(reply_to)
            .msg_type("interactive")
            .content(content_json)
            .build()
        )
        .build()
    )
    
    self._client.im.v1.message.create(request)
```

---

## 四、钉钉集成

### 4.1 接入方式

**Stream Mode** — 无需公网 IP，通过 WebSocket 接收消息。

```
┌───────────┐     WebSocket      ┌───────────┐
│ 钉钉服务器 │◄──────────────────►│ 本地助理   │
└───────────┘                    └───────────┘
     │                                 │
     │ 推送消息事件                    │
     │ ──────────────────────────────►│
     │                                 │
     │                                 │ 处理消息
     │                                 │
     │◄────────────────────────────── │ 回复消息
     │  HTTP API                      │
     └───────────┘                    
```

### 4.2 配置要求

```yaml
# config/channels.yaml
channels:
  dingtalk:
    enabled: true
    client_id: "dingxxx"         # 钉钉应用 Client ID
    client_secret: "xxx"         # 钉钉应用 Client Secret
```

### 4.3 钉钉应用配置

**在钉钉开放平台创建应用：**

1. 创建企业内部应用
2. 开启机器人能力
3. 配置 Stream Mode
4. 获取 Client ID 和 Client Secret

### 4.4 钉钉 Channel 实现

```python
from dingtalk_stream import (
    Credential,
    DingTalkStreamClient,
    CallbackHandler,
    CallbackMessage,
    AckMessage
)
from dingtalk_stream.chatbot import ChatbotMessage

class DingTalkHandler(CallbackHandler):
    """钉钉消息处理器"""
    
    def __init__(self, channel: "DingTalkChannel"):
        super().__init__()
        self.channel = channel
    
    async def process(self, message: CallbackMessage):
        """处理钉钉消息"""
        try:
            chatbot_msg = ChatbotMessage.from_dict(message.data)
            
            # 提取文本
            content = ""
            if chatbot_msg.text:
                content = chatbot_msg.text.content.strip()
            
            if not content:
                return AckMessage.STATUS_OK, "OK"
            
            sender_id = chatbot_msg.sender_staff_id or chatbot_msg.sender_id
            sender_name = chatbot_msg.sender_nick or "Unknown"
            
            # 构建消息
            incoming = IncomingMessage(
                message_id=chatbot_msg.message_id,
                sender_id=sender_id,
                chat_id=sender_id,  # 私聊时 chat_id = sender_id
                content=content,
                msg_type="text",
                metadata={
                    "sender_name": sender_name,
                    "platform": "dingtalk"
                }
            )
            
            # 异步处理
            asyncio.create_task(self.channel._on_message(incoming))
            
            return AckMessage.STATUS_OK, "OK"
        
        except Exception as e:
            logger.exception(f"Error processing DingTalk message: {e}")
            return AckMessage.STATUS_OK, "Error"

class DingTalkChannel(ChannelPlugin):
    """钉钉频道"""
    
    name = "dingtalk"
    
    def __init__(self, config: dict):
        self.config = config
        self._client: DingTalkStreamClient | None = None
        self._access_token: str | None = None
        self._running = False
    
    async def start(self) -> None:
        """启动钉钉机器人"""
        self._running = True
        
        credential = Credential(
            self.config['client_id'],
            self.config['client_secret']
        )
        self._client = DingTalkStreamClient(credential)
        
        # 注册消息处理器
        handler = DingTalkHandler(self)
        self._client.register_callback_handler(
            ChatbotMessage.TOPIC, 
            handler
        )
        
        logger.info("DingTalk channel started")
        
        # 启动 Stream
        while self._running:
            try:
                await self._client.start()
            except Exception as e:
                logger.error(f"DingTalk stream error: {e}")
            if self._running:
                await asyncio.sleep(5)
    
    async def stop(self) -> None:
        """停止钉钉机器人"""
        self._running = False
        logger.info("DingTalk channel stopped")
    
    async def _get_access_token(self) -> str | None:
        """获取 Access Token"""
        if self._access_token and time.time() < self._token_expiry:
            return self._access_token
        
        url = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
        data = {
            "appKey": self.config['client_id'],
            "appSecret": self.config['client_secret']
        }
        
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=data)
            result = resp.json()
            self._access_token = result.get("accessToken")
            self._token_expiry = time.time() + result.get("expireIn", 7200) - 60
        
        return self._access_token
    
    async def send(self, message: OutgoingMessage) -> None:
        """发送钉钉消息"""
        token = await self._get_access_token()
        if not token:
            return
        
        url = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
        headers = {"x-acs-dingtalk-access-token": token}
        
        data = {
            "robotCode": self.config['client_id'],
            "userIds": [message.reply_to],
            "msgKey": "sampleMarkdown",
            "msgParam": json.dumps({
                "text": message.content,
                "title": "助理回复"
            })
        }
        
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=data, headers=headers)
            if resp.status_code != 200:
                logger.error(f"DingTalk send failed: {resp.text}")
```

---

## 五、消息路由

### 5.1 路由逻辑

```python
class MessageRouter:
    """消息路由器"""
    
    def __init__(self, agent, pipelines: dict):
        self.agent = agent
        self.pipelines = pipelines
    
    async def route(self, message: IncomingMessage) -> None:
        """路由消息到对应处理器"""
        
        msg_type = message.msg_type
        
        if msg_type == "text":
            # 文字消息 → Agent 对话
            response = await self.agent.chat(
                message.content,
                context={
                    "sender_id": message.sender_id,
                    "platform": message.metadata.get("platform")
                }
            )
            
            # 发送回复
            await self._reply(message, response)
        
        elif msg_type == "image":
            # 图片消息 → 人脸识别 Pipeline
            if message.media:
                result = await self.pipelines['face'].process(message.media[0])
                
                # 返回处理结果
                if result.get('persons'):
                    names = [p['name'] for p in result['persons']]
                    response = f"识别到 {len(names)} 人：{', '.join(names)}"
                else:
                    response = "照片已入库"
                
                await self._reply(message, response)
        
        elif msg_type == "file":
            # 文件消息 → 文档解析 Pipeline
            if message.media:
                result = await self.pipelines['document'].process(message.media[0])
                response = f"文档已入库：{result.get('title', '未知文档')}"
                await self._reply(message, response)
        
        else:
            await self._reply(message, "暂不支持此类型消息")
    
    async def _reply(self, message: IncomingMessage, content: str):
        """发送回复"""
        channel = self._get_channel(message.metadata.get("platform"))
        if channel:
            await channel.send(OutgoingMessage(
                content=content,
                reply_to=message.chat_id
            ))
```

### 5.2 异步处理

对于耗时操作（如人脸识别、文档解析），使用异步处理：

```python
async def route(self, message: IncomingMessage) -> None:
    """路由消息"""
    
    # 立即回复"收到"
    await self._reply(message, "收到，正在处理...")
    
    # 异步处理
    if message.msg_type == "image":
        asyncio.create_task(self._process_image(message))
    elif message.msg_type == "file":
        asyncio.create_task(self._process_file(message))
    else:
        # 文字消息同步处理
        response = await self.agent.chat(message.content)
        await self._reply(message, response)

async def _process_image(self, message: IncomingMessage):
    """异步处理图片"""
    try:
        result = await self.pipelines['face'].process(message.media[0])
        
        if result.get('persons'):
            names = [p.get('name', '未命名') for p in result['persons']]
            response = f"识别完成：{', '.join(names)}"
        else:
            response = "照片已入库"
        
        await self._reply(message, response)
    
    except Exception as e:
        await self._reply(message, f"处理失败：{str(e)}")
```

---

## 六、安全与权限

### 6.1 权限控制

```python
# config/channels.yaml
channels:
  feishu:
    enabled: true
    app_id: "cli_xxx"
    app_secret: "xxx"
    
    # 权限控制
    allow_from:
      - "ou_xxx"     # 允许的用户 ID
      - "@all"       # 允许所有人（谨慎使用）
    
    # 黑名单
    deny_from:
      - "ou_yyy"     # 禁止的用户 ID
```

### 6.2 消息验证

```python
async def _on_message(self, message: IncomingMessage):
    """消息验证"""
    
    # 检查允许列表
    allow_list = self.config.get('allow_from', [])
    if allow_list and '@all' not in allow_list:
        if message.sender_id not in allow_list:
            logger.warning(f"Message from unauthorized user: {message.sender_id}")
            return
    
    # 检查黑名单
    deny_list = self.config.get('deny_from', [])
    if message.sender_id in deny_list:
        logger.warning(f"Message from denied user: {message.sender_id}")
        return
    
    # 验证通过，处理消息
    await self._message_handler(message)
```

### 6.3 敏感信息处理

```python
def sanitize_for_im(text: str) -> str:
    """为 IM 消息脱敏"""
    import re
    
    # 隐藏电话中间 4 位
    text = re.sub(r'(\d{3})\d{4}(\d{4})', r'\1****\2', text)
    
    # 隐藏邮箱用户名部分
    text = re.sub(r'([a-zA-Z0-9])[a-zA-Z0-9._%+-]+@', r'\1***@', text)
    
    return text
```

---

## 七、状态同步

### 7.1 会话状态

用户在 IM 和桌面端的会话状态共享：

```python
class SessionManager:
    """会话管理"""
    
    def __init__(self, db):
        self.db = db
    
    def get_session(self, sender_id: str, platform: str) -> str:
        """获取会话 ID（同一用户跨平台共享）"""
        # 查找用户绑定的会话
        result = self.db.execute("""
            SELECT session_id FROM user_bindings
            WHERE sender_id = ? AND platform = ?
        """, sender_id, platform)
        
        if result:
            return result['session_id']
        
        # 创建新会话
        session_id = str(uuid.uuid4())
        self.db.execute("""
            INSERT INTO user_bindings (sender_id, platform, session_id)
            VALUES (?, ?, ?)
        """, sender_id, platform, session_id)
        
        return session_id
    
    def get_history(self, session_id: str, limit: int = 10) -> list:
        """获取对话历史"""
        return self.db.execute("""
            SELECT role, content FROM messages
            WHERE session_id = ?
            ORDER BY created_at DESC
            LIMIT ?
        """, session_id, limit)
```

### 7.2 数据同步

```
┌─────────────┐                    ┌─────────────┐
│ 桌面端      │                    │ IM 端       │
│             │                    │             │
│  发送消息   │                    │  发送消息   │
│      │      │                    │      │      │
│      ▼      │                    │      ▼      │
│  本地存储   │                    │  远程存储   │
│      │      │                    │      │      │
│      └──────┼────────────────────┼──────┘      │
│             │     会话 ID 绑定    │             │
│             │                    │             │
└─────────────┘                    └─────────────┘
```

---

## 八、错误处理

### 8.1 错误类型

| 错误类型 | 处理方式 |
|----------|----------|
| 网络断开 | 自动重连 |
| Token 过期 | 自动刷新 |
| 消息发送失败 | 重试 3 次 |
| 处理超时 | 返回错误提示 |

### 8.2 重连机制

```python
async def start_with_reconnect(self):
    """带重连的启动"""
    while self._running:
        try:
            await self.start()
        except Exception as e:
            logger.error(f"Channel error: {e}")
        
        if self._running:
            logger.info("Reconnecting in 5 seconds...")
            await asyncio.sleep(5)
```

### 8.3 错误响应

```python
ERROR_RESPONSES = {
    "rate_limit": "消息太频繁，请稍后再试",
    "processing_error": "处理出错，请稍后重试",
    "unsupported_type": "暂不支持此类型消息",
    "unauthorized": "您没有权限使用此功能"
}
```

---

## 九、监控与日志

### 9.1 日志格式

```
[INFO] Feishu channel started
[INFO] Received message from ou_xxx: "帮我找张三的合同"
[INFO] Agent response: "找到 2 个文件..."
[INFO] Message sent to ou_xxx
[ERROR] Failed to send message: rate limited
[WARN] Reconnecting Feishu channel...
```

### 9.2 统计指标

```python
class ChannelMetrics:
    """频道统计"""
    
    def __init__(self):
        self.messages_received = 0
        self.messages_sent = 0
        self.errors = 0
        self.start_time = None
    
    def record_received(self):
        self.messages_received += 1
    
    def record_sent(self):
        self.messages_sent += 1
    
    def record_error(self):
        self.errors += 1
    
    def get_stats(self) -> dict:
        return {
            "uptime": time.time() - self.start_time if self.start_time else 0,
            "received": self.messages_received,
            "sent": self.messages_sent,
            "errors": self.errors
        }
```

---

## 十、代码量估算

| 组件 | 代码量 |
|------|--------|
| Channel Plugin 基类 | ~150 行 |
| 飞书 Channel | ~400 行 |
| 钉钉 Channel | ~300 行 |
| 消息路由器 | ~200 行 |
| 会话管理 | ~150 行 |
| 安全验证 | ~100 行 |
| 错误处理 | ~100 行 |
| **总计** | **~1,400 行** |

---

## 十一、参考资料

### SDK 文档

- [飞书开放平台](https://open.feishu.cn/)
- [钉钉开放平台](https://open.dingtalk.com/)
- [lark-oapi SDK](https://github.com/larksuite/oapi-sdk-python)
- [dingtalk-stream SDK](https://github.com/open-dingtalk/dingtalk-stream-sdk)

### 实现参考

- OpenViking bot/channels/feishu.py
- OpenViking bot/channels/dingtalk.py

---

*文档结束*
