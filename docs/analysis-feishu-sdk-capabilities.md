# lark_oapi SDK 能力分析报告

> 分析版本：lark_oapi channel 模块（与 node-sdk FeishuChannel 1:1 对齐）
> 分析日期：2026-05-19
> 涉及文件：
> - `lark_oapi/channel/channel.py` — FeishuChannel 主类（2046行）
> - `lark_oapi/ws/client.py` — WS Client（430行）
> - `lark_oapi/channel/config.py` — 配置体系（368行）
> - `lark_oapi/channel/outbound/` — 出站发送/重试/流式/媒体
> - `lark_oapi/channel/safety/` — 安全管线（去重/策略/批处理/串行队列）

---

## 1. FeishuChannel 类 — 所有公开方法

### 1.1 构造函数

```python
FeishuChannel(
    *,
    app_id: Optional[str] = None,
    app_secret: Optional[str] = None,
    encrypt_key: Optional[str] = None,
    verification_token: Optional[str] = None,
    domain: Optional[str] = None,
    log_level: Optional[LogLevel] = None,
    transport: Optional[Union[str, TransportConfig]] = None,
    policy: Optional[PolicyConfig] = None,
    safety: Optional[SafetyConfig] = None,
    inbound: Optional[InboundConfig] = None,
    outbound: Optional[OutboundConfig] = None,
    uat: Optional[UATConfig] = None,
    token_store: Optional[TokenStore] = None,
    dedup_store: Any = None,
    safety_cache: Optional[ICache] = None,
    name_lookup: Optional[Callable[[List[str]], Any]] = None,
    config: Optional[ChannelConfig] = None,
)
```

**关键设计**：
- `dedup_store` 和 `safety_cache` 是**两层不同的去重**：前者用于 pipeline 层 Deduper（webhook 重试 + WS 重连回填），后者用于 SafetyPipeline 的 SeenCache（跨进程一致性，可接 Redis）
- `transport` 接受字符串 `"ws"` / `"webhook"` 或 `TransportConfig` 对象
- `name_lookup` 可注入自定义名称解析函数（默认调用飞书 API）
- `config` 参数可一次性传入完整配置，per-area kwargs 会覆盖对应字段

### 1.2 事件注册

```python
def on(
    self,
    name_or_map: Union[ChannelEventName, Dict[ChannelEventName, EventHandler]],
    handler: Optional[EventHandler] = None,
) -> Unsubscribe
```

- 支持两种调用形式：`on("message", handler)` 或 `on({"message": handler1, "cardAction": handler2})`
- 返回 `Unsubscribe` 可调用对象，调用后从内部列表移除该 handler
- 同一事件可注册多个 handler，按注册顺序执行
- 未知事件名产生 warning 日志但不抛异常（历史别名通过 `_coerce` 模块归一化）

### 1.3 生命周期方法

| 方法 | 签名 | 功能 |
|------|------|------|
| `start()` | `def start(self) -> None` | 同步启动 WS（阻塞）或初始化 Webhook dispatcher |
| `connect()` | `async def connect(self) -> None` | 异步幂等，内部调用 `run_in_executor(None, self.start)` |
| `start_background()` | `async def start_background(self, *, timeout: Optional[float] = 30.0) -> None` | 后台启动传输，等待就绪后返回 |
| `connect_until_ready()` | `async def connect_until_ready(self, *, timeout: Optional[float] = 30.0) -> None` | `start_background` 的别名，语义更明确 |
| `stop()` | `def stop(self, *, join_timeout: float = 5.0) -> None` | 完整关闭：WS + 取消 futures + 关闭 device_flow + 停止 bg loop + join 线程 |
| `disconnect()` | `async def disconnect(self) -> None` | 先 drain safety pipeline 批处理，再调用 `stop()` |
| `stop_background()` | `async def stop_background(self) -> None` | `disconnect()` 的别名 |

### 1.4 就绪状态

| 属性/方法 | 签名 | 功能 |
|-----------|------|------|
| `is_ready` | `@property -> bool` | WS 连接成功 + bot identity 解析 + safety pipeline 初始化后为 True |
| `wait_ready()` | `async def wait_ready(self, *, timeout: Optional[float] = None) -> None` | 阻塞等待 is_ready，超时抛 `asyncio.TimeoutError` |

### 1.5 出站消息方法

| 方法 | 签名 | 功能 |
|------|------|------|
| `send()` | `async def send(self, to, message, opts=None) -> SendResult` | 发送消息（text/markdown/image/card/file/audio/video/sticker/share_chat/share_user） |
| `stream()` | `async def stream(self, to, spec: Dict, opts=None) -> SendResult` | 流式发送：`{"markdown": producer}` 或 `{"card": {"initial": ..., "producer": ...}}` |
| `update_card()` | `async def update_card(self, message_id: str, card: Dict) -> SendResult` | 更新卡片消息 |
| `edit_message()` | `async def edit_message(self, message_id: str, message) -> SendResult` | 编辑已发送的 text/post 消息 |
| `recall_message()` | `async def recall_message(self, message_id: str) -> SendResult` | 撤回消息 |
| `add_reaction()` | `async def add_reaction(self, message_id: str, emoji_type: str) -> SendResult` | 添加表情回应 |
| `remove_reaction()` | `async def remove_reaction(self, message_id: str, reaction_id: str) -> SendResult` | 移除表情回应 |

### 1.6 媒体方法

| 方法 | 签名 | 功能 |
|------|------|------|
| `upload_media()` | `async def upload_media(self, source: MediaSource, *, kind: Literal["image","file"], file_name, file_type) -> str` | 上传媒体资源，返回 image_key/file_key |
| `download_resource()` | `async def download_resource(self, file_key: str, resource_type: str = "image", message_id: Optional[str] = None) -> Optional[bytes]` | 下载资源到内存 |
| `download_resource_to_file()` | `async def download_resource_to_file(self, file_key: str, *, resource_type, message_id, dest_dir: Path, file_name) -> Path` | 下载资源到磁盘（原子写入：先写临时文件再 rename） |

### 1.7 CardKit 预分配 API

| 方法 | 签名 | 功能 |
|------|------|------|
| `create_card_instance()` | `async def create_card_instance(self, spec: Dict) -> str` | 创建预分配卡片，返回 card_id |
| `send_card_by_reference()` | `async def send_card_by_reference(self, to: str, card_id: str, *, receive_id_type, reply_to, reply_in_thread, reply_target_gone) -> SendResult` | 按引用发送预分配卡片 |
| `update_card_element_content()` | `async def update_card_element_content(self, card_id: str, element_id: str, content: str, sequence: int) -> None` | 打字机式更新卡片元素（sequence 必须严格递增） |
| `finish_streaming_card()` | `async def finish_streaming_card(self, card_id: str, sequence: int) -> None` | 关闭卡片的 streaming_mode |

### 1.8 其他方法

| 方法 | 签名 | 功能 |
|------|------|------|
| `fetch_message()` | `async def fetch_message(self, message_id: str) -> Dict` | 按 ID 获取消息原始响应 |
| `get_chat_info()` | `async def get_chat_info(self, chat_id: str) -> Optional[ChatInfo]` | 获取聊天元数据 |
| `handle_webhook_request()` | `async def handle_webhook_request(self, headers: Mapping, body: bytes) -> tuple[int, bytes]` | 处理 webhook 请求（框架无关入口） |
| `require_user_auth()` | `async def require_user_auth(self, user_open_id: str, scopes: list, *, prompt_context) -> UAT` | 解析用户访问令牌（设备流） |
| `schedule()` | `def schedule(self, coro) -> concurrent.futures.Future` | 提交协程到后台循环（线程安全） |
| `resolve_bot_identity()` | `async def resolve_bot_identity(self) -> Optional[BotIdentity]` | 获取机器人身份 |
| `get_policy()` | `def get_policy(self) -> PolicyConfig` | 获取当前策略配置 |
| `update_policy()` | `def update_policy(self, **changes) -> None` | 运行时部分更新策略 |

### 1.9 公开属性

| 属性 | 类型 | 功能 |
|------|------|------|
| `client` | `Client` | 底层 OpenAPI Client |
| `ws_client` | `Optional[WSClient]` | WebSocket 客户端（仅 ws 模式） |
| `bot_identity` | `Optional[BotIdentity]` | 当前机器人身份 |
| `config` | `ChannelConfig` | 完整配置对象 |
| `safety` | `Optional[SafetyPipeline]` | 安全管线 |
| `sender` | `OutboundSender` | 出站发送器 |
| `driver` | `LarkClientDriver` | Lark API 驱动 |
| `dispatcher` | `EventDispatcherHandler` | 事件分发器 |

---

## 2. WS Client 类 — 所有公开方法

```python
class Client(object):
    def __init__(
        self,
        app_id: str,
        app_secret,
        log_level: LogLevel = LogLevel.INFO,
        event_handler: EventDispatcherHandler = None,
        domain: str = FEISHU_DOMAIN,
        auto_reconnect: bool = True,
        source: Optional[str] = None,
        extra_ua_tags: Optional[list] = None,
    )
```

### 2.1 公开方法

| 方法 | 签名 | 功能 |
|------|------|------|
| `start()` | `def start(self) -> None` | 阻塞启动：连接 WS → 启动 ping 循环 → 进入 `_select()` 永久等待 |

### 2.2 观察者钩子（可被外部赋值覆盖）

| 属性 | 签名 | 触发时机 |
|------|------|----------|
| `on_reconnecting` | `Callable[[], None]` | WS 连接丢失，开始重连前 |
| `on_reconnected` | `Callable[[], None]` | 重连成功后 |

**注意**：这两个钩子默认是空 lambda，FeishuChannel 在创建 WSClient 后会将其覆盖为 `_notify_reconnecting` / `_notify_reconnected`，从而桥接到 `channel.on("reconnecting", ...)` / `channel.on("reconnected", ...)` 事件总线。

### 2.3 内部状态

| 属性 | 类型 | 说明 |
|------|------|------|
| `_conn` | `Optional[websockets.WebSocketClientProtocol]` | 当前 WS 连接 |
| `_conn_url` | `str` | 连接 URL |
| `_conn_id` | `str` | 连接 ID（从 URL query 参数提取） |
| `_service_id` | `str` | 服务 ID |
| `_auto_reconnect` | `bool` | 是否自动重连 |
| `_lock` | `asyncio.Lock` | 连接操作互斥锁 |
| `_cache` | `ExpiringCache` | 合包缓存（多帧消息重组） |
| `_reconnect_count` | `int` | 最大重连次数（-1 = 无限） |
| `_reconnect_interval` | `int` | 重连间隔（秒） |
| `_reconnect_nonce` | `int` | 首次重连随机抖动上限（秒） |
| `_ping_interval` | `int` | Ping 间隔（秒） |

---

## 3. 事件回调机制

### 3.1 支持的事件名（ChannelEventName Literal）

| 事件名 | 回调签名 | 触发条件 |
|--------|----------|----------|
| `"message"` | `handler(inbound: InboundMessage)` | 收到消息（经过 inbound pipeline + safety pipeline 处理后） |
| `"cardAction"` | `handler(event: CardActionEvent)` | 用户点击卡片按钮 |
| `"reaction"` | `handler(event: ReactionEvent)` | 表情回应添加/移除 |
| `"botAdded"` | `handler(event: BotAddedEvent)` | Bot 被加入群聊 |
| `"botLeave"` | `handler(event: BotLeaveEvent)` | Bot 被移出群聊 |
| `"messageRead"` | `handler(event: MessageReadEvent)` | 消息已读回执 |
| `"comment"` | `handler(event: CommentEvent)` | 云文档评论 |
| `"reject"` | `handler(event: RejectEvent)` | 消息被安全管线拒绝（stale/duplicate/policy/lock_contention/self_sent） |
| `"raw"` | （通过 `_coerce` 归一化，保留扩展性） | 原始事件 |
| `"reconnecting"` | `handler()` | WS 开始重连 |
| `"reconnected"` | `handler()` | WS 重连成功 |
| `"error"` | `handler(error: OutboundSendError)` | 出站发送/流式失败 |

### 3.2 事件常量类

```python
class Events:
    MESSAGE = "message"
    CARD_ACTION = "cardAction"
    REACTION = "reaction"
    BOT_ADDED = "botAdded"
    BOT_LEAVE = "botLeave"
    MESSAGE_READ = "messageRead"
    REJECT = "reject"
    COMMENT = "comment"
    RAW = "raw"
    RECONNECTING = "reconnecting"
    RECONNECTED = "reconnected"
    ERROR = "error"
```

### 3.3 安全管线分层

事件经过 SafetyPipeline 时按类型走不同层级：

| 层级 | 事件类型 | 管线步骤 |
|------|----------|----------|
| Tier 1（完整管线） | message | stale → dedup → self_sent → policy → lock → media_batch → text_batch + serial_queue |
| Tier 2（去重+锁+串行） | cardAction, comment | dedup → lock → per-scope serial queue |
| Tier 3（仅去重） | reaction | dedup only |

---

## 4. 连接生命周期管理

### 4.1 start() 流程

```
start()
  ├── 检查 _started（幂等）
  ├── _ensure_bg_loop()  → 创建后台 asyncio 循环 + 守护线程
  ├── _fetch_bot_identity_sync()  → 在 bg loop 上获取 bot identity（10s 超时）
  ├── _build_dispatcher()  → 注册所有事件处理器
  ├── if webhook:
  │     └── _mark_ready() → 返回
  └── if ws:
        ├── 创建 WSClient
        ├── 覆盖 on_reconnecting / on_reconnected 钩子
        ├── ws_client.start()  → 阻塞连接 WS
        └── _mark_ready()
```

### 4.2 connect_until_ready() / start_background() 流程

```
start_background(timeout=30.0)
  ├── 检查 _ready_flag（已就绪则直接返回）
  ├── run_in_executor(None, self.start)  → 在线程池中启动
  └── _wait_background_start_ready(timeout, generation)
        ├── 循环检查 _ready_flag
        ├── 检查 _start_future 完成状态
        ├── 检查 ws_client._conn 是否已建立
        ├── 超时则 stop() + 抛 FeishuChannelError(NOT_CONNECTED)
        └── 每 50ms 轮询一次
```

### 4.3 stop() 流程

```
stop(join_timeout=5.0)
  ├── 设置 _shutdown 事件
  ├── 取消 _start_future
  ├── 停止 WS 客户端（尝试 stop/close/disconnect 方法）
  ├── 取消所有 _bg_tasks 中的 futures
  ├── 在 bg loop 上关闭 device_flow 的 httpx 客户端
  ├── 停止 bg loop（loop.call_soon_threadsafe(loop.stop)）
  ├── join bg 线程（超时 join_timeout）
  ├── 清理状态：_shutdown.clear(), _started=False, _ready_flag=False
  └── 清空 _bg_tasks
```

### 4.4 关键设计点

- **幂等性**：`start()` / `connect()` 多次调用安全
- **生命周期代际**：`_lifecycle_generation` 和 `_background_generation` 防止并发启动/停止导致的状态混乱
- **stop 后可重连**：`stop()` 清除 `_shutdown` 和 `_started` 标志，允许后续再次调用 `connect()`

---

## 5. schedule() 方法实现细节

```python
def schedule(self, coro) -> concurrent.futures.Future:
```

### 5.1 实现机制

1. 调用 `self._ensure_bg_loop()` 确保后台循环存在
2. 使用 `asyncio.run_coroutine_threadsafe(coro, self._bg_loop)` 将协程提交到后台循环
3. 将返回的 Future 加入 `self._bg_tasks` 集合（受 `_bg_tasks_lock` 保护）
4. 添加 done-callback：从 `_bg_tasks` 移除 + 记录异常日志

### 5.2 线程安全性

- **线程安全**：`schedule()` 可从任何线程调用
- `run_coroutine_threadsafe` 本身是线程安全的
- `_bg_tasks_lock`（`threading.Lock`）保护 `_bg_tasks` 集合的并发读写
- `_ensure_bg_loop()` 使用双重检查锁定（`_bg_lock`）防止两个线程同时创建循环

### 5.3 提交目标

- 协程被提交到 `self._bg_loop`（FeishuChannel 私有的 asyncio 事件循环）
- 该循环运行在 `self._bg_thread`（名为 `"lark-channel-bg"` 的守护线程）中
- **不是**调用者当前的事件循环，也不是 WS Client 的模块级 `loop`

### 5.4 生命周期管理

- 返回的 Future 被 `_bg_tasks` 持有，确保异常不会静默丢失
- `stop()` 时会取消所有 `_bg_tasks` 中的 pending futures
- done-callback 自动从 `_bg_tasks` 中移除已完成的 future

---

## 6. _invoke() 的 handler 调用机制

```python
async def _invoke(self, name: str, *args) -> None:
```

### 6.1 执行流程

1. 从 `self._handlers` 获取归一化事件名对应的 handler 列表
2. **快照迭代**：`for handler in list(handlers)` — 防止 handler 在执行中取消注册导致列表变异
3. 调用 `handler(*args)`
4. **同步/异步自动检测**：使用 `inspect.isawaitable(result)` 检查返回值
   - 如果是 awaitable，则 `await result`
   - 如果是普通同步返回值，直接忽略
5. **异常处理**：handler 抛异常时
   - 记录完整异常日志（`logger.exception`）
   - 将异常转发给所有 `"error"` 事件的 handler
   - 不中断其他 handler 的执行（继续迭代快照列表）

### 6.2 同步 vs 异步 handler 的区别

| 特性 | 同步 handler | 异步 handler |
|------|-------------|-------------|
| 定义 | `def on_msg(msg): ...` | `async def on_msg(msg): ...` |
| 调用方式 | 直接调用，返回非 awaitable | 返回 awaitable，被 await |
| 阻塞风险 | **会阻塞事件循环** | 不会阻塞 |
| 适用场景 | 轻量级处理（日志、计数） | I/O 操作（API 调用、数据库） |

**重要**：同步 handler 如果执行耗时操作，会阻塞 `_bg_loop` 上所有其他任务。SDK 不做 `run_in_executor` 包装，完全依赖调用者自行保证。

---

## 7. 自动重连机制

### 7.1 触发条件

WS Client 的 `_receive_message_loop()` 中，任何异常（包括 `ConnectionClosedException`）都会触发重连流程：

```python
async def _receive_message_loop(self):
    try:
        while True:
            msg = await self._conn.recv()
            loop.create_task(self._handle_message(msg))
    except Exception as e:
        await self._disconnect()
        if self._auto_reconnect:
            await self._reconnect()
        else:
            raise e
```

### 7.2 重连策略

```python
async def _reconnect(self):
    # 1. 通知 on_reconnecting 回调
    self.on_reconnecting()
    
    # 2. 首次重连随机抖动
    nonce = random.random() * self._reconnect_nonce  # 默认 0~30 秒
    await asyncio.sleep(nonce)
    
    # 3. 重连循环
    if self._reconnect_count >= 0:  # 有限次数
        for i in range(self._reconnect_count):
            if await self._try_connect(i):  # 成功
                self._fire_on_reconnected()
                return
            await asyncio.sleep(self._reconnect_interval)  # 默认 120 秒
        raise ServerUnreachableException(...)  # 超过次数
    else:  # 无限重连
        while True:
            if await self._try_connect(i):
                self._fire_on_reconnected()
                return
            await asyncio.sleep(self._reconnect_interval)
            i += 1
```

### 7.3 服务端权威配置

重连参数**不是客户端配置的**，而是由飞书 WS 端点在每次握手时通过 `ClientConfig` 下发：

```python
def _configure(self, conf: ClientConfig) -> None:
    self._reconnect_count = conf.ReconnectCount      # 最大重连次数
    self._reconnect_interval = conf.ReconnectInterval  # 重连间隔（秒）
    self._reconnect_nonce = conf.ReconnectNonce        # 首次抖动上限（秒）
    self._ping_interval = conf.PingInterval            # Ping 间隔（秒）
```

- 首次握手时从 endpoint 响应中获取
- 运行时可通过 PONG 帧的 payload 推送更新（`_handle_control_frame` 中处理）

### 7.4 回调时机

| 回调 | 触发时机 | 用途 |
|------|----------|------|
| `on_reconnecting` | 决定重连后、首次抖动等待前 | 通知上层暂停出站操作 |
| `on_reconnected` | `_try_connect` 成功后 | 通知上层恢复出站操作 |

### 7.5 _try_connect 行为

- 成功：返回 `True`
- `ClientException`（如认证失败）：**直接抛出**，不重试
- 其他异常（网络错误等）：返回 `False`，继续重试

---

## 8. SafetyPipeline 实现

### 8.1 三层入口

```python
class SafetyPipeline:
    async def push_message(self, msg: InboundMessage) -> None   # Tier 1: 完整管线
    async def push_action(self, event_id, queue_scope, handler) -> None  # Tier 2: 去重+锁+串行
    async def push_light(self, event_id, handler) -> None       # Tier 3: 仅去重
```

### 8.2 Tier 1 完整管线（push_message）

按顺序执行：

1. **Stale 检测**：消息创建时间超过 `stale_window_ms`（默认 30 分钟）则丢弃
   - 原因：WS 重连后飞书会回放断连期间的事件，重启后的进程不应回复旧消息
2. **去重（SeenCache）**：检查消息 ID 是否已处理
   - 两层缓存：内存 LRU + 可选外部 ICache（Redis）
   - TTL 默认 12 小时
3. **自发自过滤**：如果 `drop_self_sent=True` 且 bot identity 已知，丢弃 bot 自己发送的消息
4. **策略门（PolicyGate）**：根据 dm_policy / group_policy / require_mention 等规则判断是否允许
5. **处理锁（ProcessingLock）**：短 TTL（5 分钟）内存锁，防止同一消息在长时间处理期间被 WS 重连重新投递导致重复处理
6. **媒体批处理**：如果消息是兼容的媒体类型（image/file/audio/video），按 `(chat_id, kind, reply_to, thread_id)` 分桶，在 `delay_ms` 内合并为单次 dispatch
7. **文本批处理 + 串行队列**：
   - 同一 chat_id 的连续文本消息在 `delay_ms`（默认 600ms）内合并
   - 长文本（>= `long_threshold_chars` 默认 1000 字符）使用更长的 `long_delay_ms`（默认 2000ms）
   - `max_messages`（默认 8 条）和 `max_chars`（默认 4000 字符）是并行触发上限
   - 同一 chat_id 的 handler 严格串行执行（FIFO 链式 await）

### 8.3 Tier 2 去重+锁+串行（push_action）

用于 cardAction 和 comment 事件：
- SeenCache 去重（基于稳定的 event_id，如 `card:{message_id}:{operator_open_id}:{tag}:{value_repr}`）
- ProcessingLock 防并发
- 按 queue_scope（chat_id 或 file_token）串行执行

### 8.4 Tier 3 仅去重（push_light）

用于 reaction 事件：
- 仅 SeenCache 去重
- 无锁、无串行队列（reaction 是幂等状态变更，重复处理无害但增加延迟）

### 8.5 超时与重试

SafetyPipeline 本身**没有超时和重试机制**。它只负责准入控制（去重/策略/锁/批处理），不负责 handler 执行。Handler 执行异常被捕获并记录日志，但不重试。

### 8.6 错误处理

- 每个拒绝步骤都会调用 `_emit_reject()` 发出 `RejectEvent`
- RejectEvent 包含 `message_id`, `chat_id`, `sender_id`, `reason`
- RejectReason 共 11 种：`stale`, `duplicate`, `lock_contention`, `self_sent`, `policy_dm_disabled`, `policy_group_disabled`, `policy_dm_not_in_allowlist`, `policy_group_not_in_allowlist`, `policy_blocklist`, `policy_admin_only`, `policy_no_mention`, `policy_mention_all_blocked`, `policy_sender_not_allowed`
- Handler 执行异常被 `try/except` 捕获，记录日志但不传播

### 8.7 dispose()

```python
async def dispose(self) -> None:
    await self._manager.dispose()  # drain 所有 ChatPipeline 的缓冲和待执行任务
    await self._media.dispose()    # flush 所有媒体桶
```

---

## 9. OutboundConfig 和 MarkdownConverter 配置选项

### 9.1 OutboundConfig

```python
@dataclass
class OutboundConfig:
    reply_mode: Union[ReplyModeValue, PerChatReplyMode] = "auto"
    text_chunk_limit: int = 3500
    chunk_mode: ChunkMode = "newline"           # "newline" | "paragraph" | "none"
    stream_initial_text: str = ""
    stream_throttle: StreamThrottleConfig = ...
    footer: FooterConfig = ...
    markdown_converter: MarkdownConverter = ...
    retry: RetryConfig = ...
    ssrf_allowlist: Optional[List[str]] = None
    on_oversize: Optional[Callable[[OversizeContext], Awaitable[Optional[str]]]] = None
```

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `reply_mode` | `"auto"` | 回复模式：`"auto"` / `"static"` / `"streaming"`，或 `PerChatReplyMode` 按 DM/群聊区分 |
| `text_chunk_limit` | 3500 | 单条消息最大字符数，超出后分片发送 |
| `chunk_mode` | `"newline"` | 分片策略：`"newline"` 优先在换行处切分，`"paragraph"` 优先在空行处切分，`"none"` 硬切 |
| `stream_initial_text` | `""` | 流式发送的初始占位文本 |
| `stream_throttle` | `StreamThrottleConfig(min_chars=20, max_chars=200, idle_ms=300)` | 流式节流配置 |
| `footer` | `FooterConfig(status=False, elapsed=False, tokens=False, model=False, cache=False, context=False)` | 消息脚注配置 |
| `markdown_converter` | `MarkdownConverter(enabled=True, table_mode="off", tag_md_mode="structured")` | Markdown 转换器配置 |
| `retry` | `RetryConfig(max_attempts=3, base_delay_ms=500)` | 重试配置 |
| `ssrf_allowlist` | `None` | URL 下载的域名白名单（无白名单则禁止 URL 下载） |
| `on_oversize` | `None` | 超大消息自定义处理钩子 |

### 9.2 MarkdownConverter

```python
@dataclass
class MarkdownConverter:
    enabled: bool = True
    table_mode: TableMode = "off"       # "table" | "bullets" | "code" | "off"
    tag_md_mode: TagMdMode = "structured"  # "structured" | "native"
```

| 字段 | 说明 |
|------|------|
| `enabled` | 是否启用 Markdown → Post AST 转换。`False` 时 `table_mode` 强制为 `"off"` |
| `table_mode` | 表格渲染模式：`"off"` 原样输出，`"bullets"` 转为列表，`"code"` 放入代码块，`"table"` 用 `|` 分隔 |
| `tag_md_mode` | Markdown 渲染模式：`"structured"` 解析为显式 post 节点（跨客户端一致），`"native"` 用 `tag:md` 让飞书客户端原生渲染（依赖客户端版本） |

### 9.3 RetryConfig

```python
@dataclass
class RetryConfig:
    max_attempts: int = 3       # 总尝试次数（1 初始 + 2 重试）
    base_delay_ms: int = 500    # 基础延迟，实际延迟 = base_delay_ms * 3^attempt
```

重试延迟计算：
- 指数退避：`delay_ms = min(base_delay_ms * 3^attempt, max_delay_ms)`，`max_delay_ms` 默认 30 秒
- 抖动：`delay *= 0.7 + 0.6 * random()`
- 服务端 `Retry-After` 优先：如果 `SendError.retry_after_seconds` 有值，使用该值（上限 60 秒）
- 仅重试可重试错误（`RATE_LIMITED` 和服务端 5xx）

### 9.4 StreamThrottleConfig

```python
@dataclass
class StreamThrottleConfig:
    min_chars: int = 20     # 最少累积字符数
    max_chars: int = 200    # 最多累积字符数（达到立即触发）
    idle_ms: int = 300      # 最长空闲时间（毫秒）
```

双阈值节流：`(elapsed >= min_ms) OR (pending >= max_chars)` 任一条件满足即触发更新。

### 9.5 OversizeContext 钩子

```python
@dataclass
class OversizeContext:
    text: str
    chat_id: str
    receive_id_type: str
    estimated_chunks: int
```

`on_oversize` 钩子合约：
- 返回 `None` 或空字符串 → SDK 回退到默认分片
- 返回非空字符串 → SDK 发送该字符串作为单条替换消息，原长文本被丢弃
- 抛异常 → 异常传播到 `channel.send()` 调用者，无静默回退

---

## 10. _bg_loop 和 _bg_thread 的创建和管理方式

### 10.1 创建（_ensure_bg_loop）

```python
def _ensure_bg_loop(self) -> None:
    # 双重检查锁定
    if self._bg_loop is not None:
        return
    with self._bg_lock:
        if self._bg_loop is not None:
            return
        if self._shutdown.is_set():
            raise RuntimeError("channel is shutting down")
        
        loop = asyncio.new_event_loop()
        
        def _runner() -> None:
            asyncio.set_event_loop(loop)
            try:
                loop.run_forever()
            finally:
                loop.close()
        
        t = threading.Thread(target=_runner, name="lark-channel-bg", daemon=True)
        t.start()
        self._bg_loop = loop
        self._bg_thread = t
```

**关键特性**：
- 使用 `asyncio.new_event_loop()` 创建独立循环，**不依附于任何现有循环**
- 线程名为 `"lark-channel-bg"`，设为 `daemon=True`（主线程退出时自动终止）
- `_runner` 中调用 `asyncio.set_event_loop(loop)` 使该线程的默认循环一致
- `loop.run_forever()` 退出后自动 `loop.close()`
- 双重检查锁定防止两个并发调用者各自创建循环

### 10.2 SafetyPipeline 初始化

在 `_ensure_bg_loop` 末尾，同步等待 SafetyPipeline 构建完成：

```python
fut = asyncio.run_coroutine_threadsafe(_build_safety(), self._bg_loop)
self._safety = fut.result(timeout=5)
```

### 10.3 销毁（stop 中的处理）

```python
# 停止循环
loop.call_soon_threadsafe(loop.stop)

# 等待线程退出
thread.join(timeout=join_timeout)

# 清理引用
self._bg_loop = None
self._bg_thread = None
```

### 10.4 与 WS Client 的循环关系

**重要**：WS Client 使用**模块级** `loop`（在 `ws/client.py` 顶层创建）：

```python
try:
    loop = asyncio.get_event_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
```

FeishuChannel 的 `_bg_loop` 和 WS Client 的 `loop` 是**两个不同的 asyncio 循环**，运行在**不同的线程**中。FeishuChannel 通过 `schedule()` 将协程提交到自己的 `_bg_loop`，而 WS Client 的事件处理在其自己的 `loop` 上运行。

---

## 11. 所有可被上层利用但当前未被利用的 SDK 能力

### 11.1 未利用的事件类型

| 事件 | 当前状态 | 潜在用途 |
|------|----------|----------|
| `"reaction"` | 可能已部分使用 | 表情回应可做"点赞确认"、"快捷指令"（如 thumbs up = 确认执行） |
| `"messageRead"` | 可能未使用 | 已读回执可用于"等待用户阅读后再执行"或"消息到达率统计" |
| `"comment"` | 可能未使用 | 云文档评论触发 Agent 回复或知识库更新 |
| `"reject"` | 可能未使用 | 被策略拒绝的消息可做审计日志、通知管理员、或给用户友好提示 |
| `"botAdded"` / `"botLeave"` | 可能未使用 | Bot 被加入/移出群聊时自动发送欢迎/告别消息，或更新群聊缓存 |
| `"error"` | 可能未使用 | 集中式错误监控、Sentry 上报、自动降级 |

### 11.2 未利用的 SafetyPipeline 能力

| 能力 | 当前状态 | 潜在用途 |
|------|----------|----------|
| `MediaBatchConfig` | 默认 `enabled=False` | 启用后可将连续图片/文件消息合并为单次 dispatch，减少 handler 调用次数 |
| `SeenCache` + 外部 ICache（Redis） | 默认内存 | 多进程部署时共享去重状态，防止跨进程重复处理 |
| `PolicyGate` 运行时更新 | `update_policy()` 已暴露 | 可根据运行时条件动态调整 DM/群聊策略（如临时关闭某个群） |
| `GroupOverride` | 配置已定义 | 按群聊 ID 设置不同策略（如某个群允许所有人触发，其他群需要 @Bot） |
| `stale_message_window_ms` | 默认 30 分钟 | 可调整窗口大小，重启后更短窗口 = 更少回填消息处理 |
| `ChatQueueConfig.enabled` | 默认 True | 可按需禁用串行队列（如低并发场景减少延迟） |

### 11.3 未利用的 Outbound 能力

| 能力 | 当前状态 | 潜在用途 |
|------|----------|----------|
| `MarkdownConverter.tag_md_mode = "native"` | 默认 `"structured"` | 使用飞书原生 Markdown 渲染，支持标题/引用/列表原生样式（但依赖客户端版本） |
| `MarkdownConverter.table_mode` | 默认 `"off"` | 启用后表格可渲染为列表/代码块/分隔线格式，而非原样输出 |
| `OutboundConfig.on_oversize` 钩子 | 默认 None | 超长消息自定义处理（如截断+追加"查看完整内容"链接，而非分片） |
| `OutboundConfig.footer` | 默认全 False | 启用后可在消息末尾追加状态/耗时/token数/模型名等元信息 |
| `OutboundConfig.reply_mode = "streaming"` | 默认 `"auto"` | 强制流式回复模式 |
| `PerChatReplyMode` | 未使用 | 按 DM/群聊设置不同回复模式（如 DM 流式、群聊静态） |
| `edit_message()` | 已暴露 | 编辑已发送消息（如修正错误、更新进度） |
| `recall_message()` | 已暴露 | 撤回消息（如发送错误后立即撤回） |
| `add_reaction()` / `remove_reaction()` | 已暴露 | 程序化添加/移除表情回应（如处理完成后加 checkmark） |
| `upload_media()` | 已暴露 | 预上传媒体获取 key，用于跨聊天复用或构建自定义 post AST |
| `download_resource_to_file()` | 已暴露 | 下载到磁盘（原子写入），比 `download_resource()` 更适合大文件 |
| `forward_message()` (driver 层) | 已暴露 | 转发消息到其他聊天 |
| `stream()` 的 card 模式 | 已暴露 | 流式更新完整卡片 JSON（进度条、动态元素等） |

### 11.4 未利用的连接生命周期能力

| 能力 | 当前状态 | 潜在用途 |
|------|----------|----------|
| `connect_until_ready()` | 已暴露 | 异步启动后继续执行其他初始化，而非阻塞等待 |
| `wait_ready(timeout=...)` | 已暴露 | 在异步代码中等待 channel 就绪，超时可控 |
| `is_ready` 属性 | 已暴露 | 非阻塞检查就绪状态，用于条件逻辑 |
| `schedule()` | 已暴露 | 从任何线程安全提交协程到 channel 的后台循环 |
| `resolve_bot_identity()` | 已暴露 | 手动触发 bot identity 刷新（如 token 更新后） |
| `handle_webhook_request()` | 已暴露 | webhook 模式下的框架无关入口，可接入 aiohttp/starlette/fastapi |

### 11.5 未利用的 UAT 能力

| 能力 | 当前状态 | 潜在用途 |
|------|----------|----------|
| `require_user_auth()` | 已暴露 | 获取用户访问令牌，调用需要用户权限的 API（如读取用户日历、发送用户消息） |
| `UATConfig` | 已定义 | 配置 UAT 的 scope 控制、刷新策略、设备流轮询间隔 |
| `TokenStore` / `FileTokenStore` | 已暴露 | 持久化用户令牌，跨重启复用 |

### 11.6 未利用的 SSRF 防护能力

| 能力 | 当前状态 | 潜在用途 |
|------|----------|----------|
| `assert_public_url()` | 已暴露 | 独立使用 SSRF 检查（不依赖 channel 实例） |
| `ssrf_allowlist` | 需显式配置 | 配置可信域名白名单后才能使用 URL 媒体源下载 |

### 11.7 未利用的 InboundPipeline 能力

| 能力 | 当前状态 | 潜在用途 |
|------|----------|----------|
| `InboundConfig.expand_merge_forward` | 默认 True | 合并转发消息展开为子消息列表 |
| `InboundConfig.merge_forward_max_depth` | 默认 3 | 控制展开深度 |
| `InboundConfig.merge_forward_max_items` | 默认 50 | 控制展开条目数 |
| `InboundConfig.fetch_interactive_card` | 默认 True | 获取交互式卡片的完整内容 |
| `InboundConfig.reaction_notifications` | 默认 `"own"` | `"off"` = 关闭，`"own"` = 仅自己发送的消息的 reaction，`"all"` = 所有 |
| `InboundConfig.media_capabilities` | 默认全开 | 按需关闭不支持的媒体类型 |
| `InboundConfig.name_cache` | 默认启用 | 名称解析缓存（max_size=2000, ttl=24h） |

### 11.8 未利用的流式能力细节

| 能力 | 当前状态 | 潜在用途 |
|------|----------|----------|
| `MarkdownStreamController.append()` | 内部使用 | 流式追加文本块（delta 模式） |
| `MarkdownStreamController.set_content()` | 内部使用 | 流式设置完整内容（accumulated 模式） |
| `CardStreamController.update()` | 内部使用 | 流式更新完整卡片快照 |
| `UpdateQueue` 合并语义 | 内部使用 | 最多 1 running + 1 pending，新 pending 替换旧 pending（比 node-sdk 的 FIFO 更高效） |
| `merge_streaming_text()` | 内部使用 | 智能合并流式文本（自动检测 delta/accumulated/mixed 模式） |

---

## 12. WS Client 模块级 loop 的隐患

### 12.1 问题

`ws/client.py` 在模块顶层创建事件循环：

```python
try:
    loop = asyncio.get_event_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
```

这意味着：
- 所有 WS Client 实例共享同一个事件循环
- 该循环在模块导入时创建，可能早于 FeishuChannel 的 `_bg_loop`
- FeishuChannel 的 `stop()` 需要特殊处理这个外部循环（`_stop_private_ws_client` 方法）

### 12.2 FeishuChannel 的适配

`_stop_private_ws_client()` 方法处理了三种情况：
1. 当前正在 WS 循环中 → `ws_loop.create_task(disconnect())`
2. 当前不在 WS 循环中 → `run_coroutine_threadsafe(disconnect(), ws_loop)`
3. WS 循环未运行但未关闭 → `ws_loop.run_until_complete(disconnect())`

---

## 13. 错误分类体系

### 13.1 FeishuChannelErrorCode（10 种）

| 错误码 | 含义 | 可重试 |
|--------|------|--------|
| `FORMAT_ERROR` | 消息格式错误（230001/230099/230021/230022） | 否 |
| `TARGET_REVOKED` | 回复目标已撤回（230002/230005/230020/230017） | 否 |
| `RATE_LIMITED` | 限流（99991402/11020/11021） | 是 |
| `PERMISSION_DENIED` | 权限不足（99991400/99991401/99991672/...） | 部分（token 过期可重试） |
| `UPLOAD_FAILED` | 上传失败 | 否 |
| `DOWNLOAD_FAILED` | 下载失败 | 否 |
| `SSRF_BLOCKED` | SSRF 防护拦截 | 否 |
| `SEND_TIMEOUT` | 发送超时 | 否 |
| `NOT_CONNECTED` | 未连接 | 否 |
| `UNKNOWN` | 未知错误（5xx 可重试） | 部分 |

### 13.2 出站降级策略

OutboundSender 实现了两级优雅降级：

1. **回复目标已撤回 → 新建消息**：如果 `reply_target_gone="fresh"`（默认），回复失败后自动降级为新建消息发送
2. **Post 格式错误 → 纯文本**：如果 post 消息被服务端拒绝（230001），自动降级为纯文本发送

---

## 14. 关键架构总结

### 14.1 线程模型

```
主线程
  └── FeishuChannel 实例
        ├── _bg_thread ("lark-channel-bg", daemon)
        │     └── _bg_loop (asyncio.new_event_loop)
        │           ├── SafetyPipeline 定时器
        │           ├── ChatPipeline 批处理定时器
        │           ├── MediaPipeline 批处理定时器
        │           └── schedule() 提交的协程
        └── WS Client (模块级 loop)
              └── ws/client.py:loop (模块顶层创建)
                    ├── _ping_loop
                    ├── _receive_message_loop
                    └── _handle_message → EventDispatcherHandler._do_without_validation
```

### 14.2 数据流

```
入站:
  WS/Webhook → EventDispatcherHandler → _on_p2_xxx (同步) → schedule(_handle_xxx)
    → InboundPipeline.process() → SafetyPipeline.push_message()
      → stale → dedup → self_sent → policy → lock → batch → queue
        → _dispatch_inbound_to_user() → _invoke("message", inbound)
          → 用户 handler(inbound: InboundMessage)

出站:
  channel.send(to, message) → _coerce.coerce_outbound(message)
    → OutboundSender.send() → _materialize() → _send_one_with_fallback()
      → with_retry() → _create() / _reply()
        → LarkClientDriver → lark_oapi.Client → HTTP API
```

### 14.3 配置层次

```
ChannelConfig (顶层)
  ├── TransportConfig (kind, auto_reconnect)
  ├── PolicyConfig (dm_policy, group_policy, require_mention, allow/deny lists, group_overrides)
  ├── SafetyConfig
  │     ├── DedupConfig (enabled, ttl_seconds, max_entries, sweep_seconds)
  │     ├── TextBatchConfig (delay_ms, long_threshold_chars, long_delay_ms, max_messages, max_chars)
  │     ├── MediaBatchConfig (enabled, delay_ms, max_items, compatible_kinds)
  │     ├── ChatQueueConfig (enabled)
  │     └── stale_message_window_ms
  ├── InboundConfig
  │     ├── MediaCapabilities (image, audio, video, file, sticker)
  │     ├── NameCacheConfig (enabled, max_size, ttl_seconds)
  │     ├── expand_merge_forward, fetch_interactive_card, reaction_notifications
  │     ├── merge_forward_max_depth, merge_forward_max_items
  │     └── drop_self_sent
  ├── OutboundConfig
  │     ├── reply_mode, text_chunk_limit, chunk_mode
  │     ├── StreamThrottleConfig (min_chars, max_chars, idle_ms)
  │     ├── FooterConfig (status, elapsed, tokens, model, cache, context)
  │     ├── MarkdownConverter (enabled, table_mode, tag_md_mode)
  │     ├── RetryConfig (max_attempts, base_delay_ms)
  │     ├── ssrf_allowlist
  │     └── on_oversize
  └── UATConfig (allowed_scopes, blocked_scopes, refresh_before_expiry_seconds, device_poll_interval_seconds)
```
