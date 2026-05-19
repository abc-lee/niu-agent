# 飞书 SDK SafetyPipeline 及配置类验证报告

> 日期：2026-05-19
> 目的：验证重设计方案中使用的 SDK SafetyPipeline 和配置类是否真实存在

---

## 1. SafetyPipeline 类是否存在？

**存在。** 完整定义在 `lark_oapi/channel/safety/pipeline.py`。

SafetyPipeline 是一个门面类，组合了 stale 检测、dedup 缓存、策略门控、处理锁、批量聚合和串行队列。

构造函数签名：
```python
class SafetyPipeline:
    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        on_message: MessageDispatch,
        on_reject: Optional[OnReject] = None,
        policy: Optional[PolicyConfig] = None,
        cache: Optional[ICache] = None,
        dedup_config: Optional[DedupConfig] = None,
        batch_config: Optional[TextBatchConfig] = None,
        media_batch_config: Optional[MediaBatchConfig] = None,
        queue_config: Optional[ChatQueueConfig] = None,
        stale_window_ms: int = 1800000,
        processing_lock_ttl_ms: int = 300000,
        drop_self_sent: bool = True,
    ) -> None
```

公开方法：
- `push_message(msg)` — 三层完整流水线
- `push_action(event_id, queue_scope, handler)` — 二层流水线（dedup + lock + serial queue）
- `push_light(event_id, handler)` — 一层流水线（dedup only）
- `dispose()` — 清理所有缓冲区和任务
- `set_bot_open_id(open_id)` — 设置机器人身份
- `update_policy(**changes)` — 运行时部分更新策略
- `get_policy()` — 获取当前策略配置

---

## 2. 配置类验证

**全部存在。** 定义在 `lark_oapi/channel/config.py`，并通过 `channel/__init__.py` 公开导出。

### SafetyConfig

| 字段 | 类型 | 默认值 |
|------|------|--------|
| `dedup` | DedupConfig | DedupConfig() |
| `text_batch` | TextBatchConfig | TextBatchConfig() |
| `media_batch` | MediaBatchConfig | MediaBatchConfig() |
| `chat_queue` | ChatQueueConfig | ChatQueueConfig() |
| `stale_message_window_ms` | int | 1800000 (30 分钟) |

### DedupConfig

| 字段 | 类型 | 默认值 |
|------|------|--------|
| `enabled` | bool | True |
| `ttl_seconds` | int | 43200 (12 小时) |
| `max_entries` | int | 5000 |
| `sweep_seconds` | int | 300 (5 分钟) |

### TextBatchConfig

| 字段 | 类型 | 默认值 |
|------|------|--------|
| `delay_ms` | int | 600 |
| `long_threshold_chars` | int | 1000 |
| `long_delay_ms` | int | 2000 |
| `max_messages` | int | 8 |
| `max_chars` | int | 4000 |

### ChatQueueConfig

| 字段 | 类型 | 默认值 |
|------|------|--------|
| `enabled` | bool | True |

### MediaBatchConfig

| 字段 | 类型 | 默认值 |
|------|------|--------|
| `enabled` | bool | False |
| `delay_ms` | int | 800 |
| `max_items` | int | 9 |
| `compatible_kinds` | frozenset | {"image", "file", "audio", "video"} |

---

## 3. `channel.on("error", handler)` 是否支持？

**支持。** `"error"` 是 `ChannelEventName` Literal 类型中的合法事件名。

error 事件在两种场景下触发：

**场景 A：handler 内部异常。** `_invoke` 方法捕获异常后，将原始异常对象传给所有 `"error"` handlers：
```python
for err in self._handlers.get("error", []):
    res = err(e)  # 参数是原始 Exception 对象
```

**场景 B：outbound 发送失败。** `send()` 和 `stream()` 方法在遇到发送错误时，调用 `_forward_outbound_error(err)` 将错误转发给 `"error"` handlers。参数类型是 `OutboundSendError`（如果原始错误是 `SendError`，会被包装）。

---

## 4. `channel.schedule()` 完整签名和实现

```python
def schedule(self, coro) -> concurrent.futures.Future
```

实现（channel.py line 879-911）：
- 调用 `_ensure_bg_loop()` 确保后台循环已启动
- 使用 `asyncio.run_coroutine_threadsafe(coro, self._bg_loop)` 将协程提交到后台循环
- 返回的 Future 被加入 `self._bg_tasks` 集合进行跟踪
- Future 完成时自动从集合中移除
- 如果 Future 产生异常，通过 done callback 记录日志
- `stop()` 方法会取消所有跟踪中的 Future

**关键特性**：可以从任何线程调用（线程安全），返回 `concurrent.futures.Future`。

---

## 5. `channel.is_ready` 完整实现

```python
@property
def is_ready(self) -> bool:
    """True after start() has fully initialized."""
    return self._ready_flag
```

`_ready_flag` 在以下时机被设为 True：
- WS 模式：WS 连接成功后
- Webhook 模式：dispatcher 构建完成后
- `start_background()` 的轮询检测到 WS client 有活跃连接时

`stop()` 会将 `_ready_flag` 重置为 False。

---

## 6. `channel.stream()` 完整签名和实现

```python
async def stream(self, to, spec: Dict[str, Any], opts=None) -> SendResult
```

支持两种模式：

**Markdown 流式**：`spec = {"markdown": producer}`
- 使用 CardKit 预分配流程
- 返回 `SendResult.ok(message_id=mid)`

**Card 流式**：`spec = {"card": {"initial": ..., "producer": ...}}`
- 使用 `CardStreamController` 管理
- 返回 `SendResult.ok(message_id=mid)`

---

## 7. FeishuChannel 构造函数参数

**safety 参数完全支持**。构造函数接受 `safety: Optional[SafetyConfig] = None`，传入后覆盖到 `cfg.safety` 字段。

**safety_cache 参数也支持**。这是 ICache（通常是 Redis-backed）实例，直接传递给 SafetyPipeline 的 SeenCache，用于跨进程去重一致性。

**典型用法**：
```python
channel = FeishuChannel(
    app_id="cli_xxx",
    app_secret="***",
    safety=SafetyConfig(
        dedup=DedupConfig(ttl_seconds=12 * 3600, max_entries=10_000),
        text_batch=TextBatchConfig(delay_ms=800),
    ),
    outbound=OutboundConfig(
        text_chunk_limit=2000,
        retry=RetryConfig(max_attempts=5, base_delay_ms=250),
    ),
)
```

---

## 8. SDK 默认行为

| 功能 | 默认行为 |
|------|----------|
| **Dedup** | enabled=True，12 小时 TTL，5000 条上限，内存存储 |
| **Text Batch** | 600ms 延迟，长消息 2000ms，最多 8 条合并，4000 字符上限 |
| **Media Batch** | enabled=False（默认关闭），需要显式启用 |
| **Chat Queue** | enabled=True，每个 chat_id 串行处理 handler |
| **Stale 检测** | 30 分钟窗口，超过窗口的消息静默丢弃 |
| **Policy** | dm_policy="open"，group_policy="open"，require_mention=True |
| **Self-sent 过滤** | drop_self_sent=True |
| **Retry** | max_attempts=3，base_delay_ms=500，指数退避 |
| **Chunking** | text_chunk_limit=3500，chunk_mode="newline" |

---

## 结论

所有被验证的类和方法都真实存在于飞书 SDK 中，且功能完整、参数齐全。SDK 的设计高度对齐 node-sdk，提供了完整的安全流水线、事件系统、流式输出和后台任务调度。默认配置覆盖了生产环境的基本需求，同时通过 dataclass 参数提供了细粒度调优能力。