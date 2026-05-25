# v3 实时逐轮SSE推送修复方案（最终版）

## 问题

### 问题1: 消息倒序
**根因**: `created_at` 是 DB 写入时刻而非消息产生时刻。异步协程积压导致时序错乱。

### 问题2: 没有逐条推送
**根因**: 前端 `_refreshFromDBRunning` 并发保护丢弃中间的 SSE 通知。

## 核心架构变更

将 `_on_turn_result` 从异步管道改为全同步管道：
- **旧路径**: `_on_turn_result → run_coroutine_threadsafe → _async_persist_increment (fire-and-forget)`
- **新路径**: `_on_turn_result → add_message_sync + notify_new_message_sync (executor线程直接执行)`

## 修改清单

### 1. agent/session.py — add_message_sync 增加 created_at 参数 + 新增 get_message_store_sync

**改动1**: `add_message_sync` 签名增加 `created_at=None`

```python
# 修改前
def add_message_sync(
    self, role, content, tool_calls=None, tool_results=None, tool_call_id="",
) -> str:

# 修改后
def add_message_sync(
    self, role, content, tool_calls=None, tool_results=None, tool_call_id="",
    created_at=None,
) -> str:
```

**改动2**: `add_message_sync` 方法体，created_at 生成改为可选

```python
# 修改前
msg_id = str(uuid4())
created_at = datetime.now().isoformat()

# 修改后
msg_id = str(uuid4())
created_at = created_at or datetime.now().isoformat()
```

**改动3**: `add_message` async 方法也增加 `created_at=None`（向后兼容）

```python
# 修改前
async def add_message(self, role, content, tool_calls=None, tool_results=None, tool_call_id="") -> str:

# 修改后
async def add_message(self, role, content, tool_calls=None, tool_results=None, tool_call_id="", created_at=None) -> str:
```

方法体中 `created_at = datetime.now().isoformat()` 改为 `created_at = created_at or datetime.now().isoformat()`

**改动4**: 新增 `init_db_sync` 方法（在 async `init_db` 之后）

```python
def init_db_sync(self):
    """同步版本 init_db — 使用 sqlite3 + WAL 模式"""
    import sqlite3
    conn = sqlite3.connect(self.db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY, role TEXT NOT NULL, content TEXT,
                tool_calls TEXT, tool_results TEXT, tool_call_id TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages(created_at ASC)")
        cursor = conn.execute("PRAGMA table_info(messages)")
        columns = [row[1] for row in cursor.fetchall()]
        if "tool_call_id" not in columns:
            conn.execute("ALTER TABLE messages ADD COLUMN tool_call_id TEXT DEFAULT ''")
        conn.commit()
    finally:
        conn.close()
```

**改动5**: 新增 `get_message_store_sync()` 全局函数

```python
def get_message_store_sync() -> MessageStore:
    """同步版本 get_message_store — 返回已初始化的单例"""
    global _message_store
    if _message_store is None:
        _message_store = MessageStore()
        _message_store.init_db_sync()
    return _message_store
```

### 2. agent/runner.py — _on_turn_result 改为全同步 + 清理异步管道残留

**改动1**: 删除 `_persisted_ids` 属性（__init__ 中）
```python
# 删除
self._persisted_ids: set[str] = set()
```

**改动2**: 删除 `_main_event_loop` 属性（__init__ 中）
```python
# 删除
self._main_event_loop = None
```

**改动3**: 删除 `self._persisted_ids.clear()`（chat() 方法中）
```python
# 删除此行
self._persisted_ids.clear()
```

**改动4**: 重写 `_on_turn_result` 方法为全同步管道（替换原方法 + 删除 `_async_persist_increment` + `set_main_event_loop`）

```python
def _on_turn_result(self, messages: list, turn_increment: list, turn: int):
    """每轮消息追加完成后，增量持久化到 DB + SSE 推送给前端。

    全同步管道：DB写入用add_message_sync，SSE推送用notify_new_message_sync。
    无需asyncio事件循环桥接。
    """
    if not turn_increment:
        return

    # 过滤 working_memory 虚拟消息
    _wm_tool_call_ids = set()
    for msg in turn_increment:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                if tc.get("function", {}).get("name") == "working_memory":
                    _wm_tool_call_ids.add(tc.get("id", ""))

    filtered = []
    for msg in turn_increment:
        role = msg.get("role", "")
        if role == "system" or role == "user":
            continue
        if role == "assistant" and msg.get("tool_calls"):
            if any(tc.get("function", {}).get("name") == "working_memory" for tc in msg["tool_calls"]):
                continue
        if role == "tool" and msg.get("tool_call_id", "") in _wm_tool_call_ids:
            continue
        filtered.append(msg)

    if not filtered:
        return

    # 指纹去重
    new_msgs = []
    with self._persisted_lock:
        for msg in filtered:
            fp = self._msg_fingerprint(msg)
            if fp not in self._persisted_fingerprints:
                self._persisted_fingerprints.add(fp)
                new_msgs.append(msg)

    if not new_msgs:
        return

    # 全同步管道：DB写入 + SSE推送
    now_iso = datetime.now().isoformat()

    from agent.session import get_message_store_sync
    store = get_message_store_sync()
    channel = self._current_channel

    last_assistant_id = None
    last_assistant_content = ""

    for msg in new_msgs:
        role = msg.get("role", "")
        content = msg.get("content", "") or ""
        tool_calls = msg.get("tool_calls")
        tool_call_id = msg.get("tool_call_id", "")

        try:
            if role == "tool" and tool_call_id:
                msg_id = store.add_message_sync(
                    role="tool", content=content, tool_call_id=tool_call_id,
                    created_at=now_iso
                )
            elif role == "assistant":
                msg_id = store.add_message_sync(
                    role="assistant", content=content, tool_calls=tool_calls,
                    created_at=now_iso
                )
                last_assistant_id = msg_id
                last_assistant_content = content
            else:
                continue
        except Exception as e:
            logger.error(f"[TurnResult] DB write failed for {role}: {e}")
            # 移除指纹使兜底 persist_agent_reply 可以重试
            with self._persisted_lock:
                self._persisted_fingerprints.discard(self._msg_fingerprint(msg))

    # SSE 推送最后一条 assistant 消息
    if last_assistant_id and last_assistant_content.strip():
        from niu_api.chat import notify_new_message_sync
        notify_new_message_sync(
            last_assistant_id, "assistant", last_assistant_content,
            source=channel
        )
```

**删除**: `_async_persist_increment` 方法（整个方法）
**删除**: `set_main_event_loop` 方法

### 3. niu_api/__main__.py — 删除 runner.set_main_event_loop 调用

删除 __main__.py 中的以下代码块（约第98-103行）：
```python
# 5.1. Pass main event loop to NiuRunner for _on_turn_result bridging
from agent.runner import get_runner
_runner = get_runner()
if _runner is not None:
    _runner.set_main_event_loop(asyncio.get_running_loop())
```

**保留**: 第159-160行的 `niu_api/chat.py` 的 `set_main_event_loop` 调用（SSE模块需要 _main_loop）

### 4. niu_api/chat.py — 移除 source 过滤器

**改动1**: `notify_new_message` 删除 `source != "electron"` 过滤（约第40-41行）
```python
# 删除这两行
if source != "electron":
    return  # 非electron通道不走SSE，前端零感知
```

**改动2**: `notify_new_message_sync` 删除同样的过滤（约第60-61行）
```python
# 删除这两行
if source != "electron":
    return
```

**原因**: 所有通道的 assistant 消息都应推送到 SSE（Electron前端需要看到飞书/Scheduler的回复）。tool 消息由 `role == "tool"` 过滤，不受影响。

### 5. ui/assistant/chat.html — refreshFromDB 改为排队机制

**改动1**: 新增变量（约第1016行）
```javascript
// 修改前
let _refreshFromDBRunning = false;

// 修改后
let _refreshFromDBRunning = false;
let _pendingRefresh = false;
```

**改动2**: refreshFromDB 入口改为排队而非丢弃（约第1017-1018行）
```javascript
// 修改前
async function refreshFromDB() {
  if (_refreshFromDBRunning) return;

// 修改后
async function refreshFromDB() {
  if (_refreshFromDBRunning) {
    _pendingRefresh = true;
    return;
  }
```

**改动3**: finally 块增加排队检查（约第1111行）
```javascript
// 修改前
  } finally {
    _refreshFromDBRunning = false;
  }

// 修改后
  } finally {
    _refreshFromDBRunning = false;
    if (_pendingRefresh) {
      _pendingRefresh = false;
      refreshFromDB();
    }
  }
```

**竞态安全说明**: JavaScript 是单线程事件循环模型，所有回调串行执行。`_pendingRefresh` 和 `_refreshFromDBRunning` 的读写不存在竞态条件。

## 修复后数据流

### Electron 通道
```
用户输入 → runner.chat(channel="electron")
→ agent_loop → _on_turn_result (executor线程, 同步)
  → created_at=datetime.now().isoformat()
  → add_message_sync(created_at=now_iso) → DB写入
  → notify_new_message_sync(source="electron") → SSE推送
  → chat.html refreshFromDB() (排队) → 渲染
```

### 飞书通道
```
飞书消息 → ChatQueue → runner.chat(channel="feishu")
→ _on_turn_result (executor线程, 同步)
  → add_message_sync(created_at=now_iso) → DB写入
  → notify_new_message_sync(source="feishu") → SSE推送 → 前端显示
  → router.route_out → FeishuChannelAdapter.send → 飞书回复
```

### Scheduler 通道
```
定时任务 → ChatQueue → runner.chat(channel="scheduler")
→ _on_turn_result (executor线程, 同步)
  → add_message_sync(created_at=now_iso) → DB写入
  → notify_new_message_sync(source="scheduler") → SSE推送 → 前端显示
  → router.push → FeishuChannelAdapter.push → 飞书推送
```

### 兜底路径 (persist_agent_reply)
```
chat()完成 → persist_agent_reply
  → 读 _persisted_fingerprints 去重 → 跳过已持久化消息
  → 写入未覆盖的消息 (async add_message)
  → notify_new_message 推送
```

## 不需要修改的文件

| 文件 | 原因 |
|------|------|
| agent/generic/agent_loop.py | on_turn_result回调机制不变 |
| niu_api/chat_queue.py | _process_single已通过channel参数调用runner.chat() |
| niu_api/compat.py | persist_agent_reply兜底逻辑不变 |
| ui/assistant/main.js | SSE监听器触发new-message IPC不变 |

## 验证方法

1. **消息顺序**: 发送多轮工具调用的消息，验证DB中created_at递增，前端显示正序
2. **逐条推送**: 验证每轮完成后前端立即显示，不再等全部完成
3. **created_at一致性**: 同一轮次的assistant/tool消息共享相同created_at
4. **并发刷新**: 多个SSE通知快速到达时，排队机制保证不丢失
5. **兜底逻辑**: 逐轮写入成功时，persist_agent_reply跳过已持久化消息
6. **飞书隔离**: 飞书消息的SSE推送到达前端（设计意图），飞书通过route_out独立回复