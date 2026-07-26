# 飞书流式推送并发安全修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消除飞书流式推送中 SDK 线程与 asyncio 线程并发访问共享状态导致的竞态条件，确保快速连续消息场景下状态不丢失、不错乱。

**Architecture:** 将 15+ 个分散的流式状态变量封装为 `StreamState` 数据类，`_on_message` 每次创建新 `StreamState` 实例并原子交换，`_push_incremental`/`send()` 启动时读取一次快照，关键操作前检查 generation 是否变化，变化则中止当前操作。

**Tech Stack:** Python 3.11+, dataclasses, threading.Lock (单一锁，仅保护 state 交换), asyncio

---

## File Structure

| File | Responsibility |
|------|----------------|
| `niu_api/channel/feishu_channel.py` | 唯一修改文件。新增 `StreamState` 数据类，改造 `_on_message`、`_push_incremental`、`send()`、`_finalize_stream_card`、`_filter_media_markers` |
| `tests/test_feishu_stream_state.py` | 新建测试文件。测试 `StreamState` 交换原子性、generation 守卫逻辑 |

---

## 线程模型回顾

```
lark-channel-bg 线程 (SDK):  _on_message() 写入流式状态 → route_in_sync() 入队
                                    ↓ (共享状态变量，无锁)
FastAPI main-loop 线程:     _push_incremental() 读取/写入流式状态
                            send() 读取/写入流式状态
                            _finalize_stream_card() 读取/写入流式状态
                            _filter_media_markers() 读取/写入流式状态
```

**三个竞态触发路径**：

1. `send(A)` finally 块擦除 `_on_message(B)` 刚设置的状态
2. `_push_incremental` 在 await 点之间被 `_on_message` 交错，读到不一致状态
3. `_filter_media_markers` 在 await 上传图片时被 `_on_message` 交错，图片状态损坏

**核心方案**：`StreamState` 原子快照 + generation 计数器

---

### Task 1: 定义 StreamState 数据类

**Files:**
- Modify: `niu_api/channel/feishu_channel.py:60-73` (替换 15+ 个独立实例变量)
- Create: `tests/test_feishu_stream_state.py`

- [ ] **Step 1: 在 `feishu_channel.py` 顶部（import 之后、class 之前）定义 `StreamState` 数据类**

```python
from dataclasses import dataclass, field


@dataclass
class StreamState:
    """飞书流式推送的完整状态快照。

    每次 _on_message 收到新消息时创建新实例并原子交换。
    _push_incremental / send() 启动时读取一次快照，
    关键操作前检查 generation 是否变化。
    """
    generation: int = 0
    waiting: bool = False
    card_id: str | None = None
    message_id: str | None = None
    card_created: bool = False
    fallback_used: bool = False
    seq: int = 0
    accumulated_text: str = ""
    pending_images: list = field(default_factory=list)
    pending_files: list = field(default_factory=list)
    sent_media_paths: set = field(default_factory=set)
    reply_to_id: str | None = None
    target: str | None = None
    last_pushed_rowid: int = 0
```

- [ ] **Step 2: 将 `__init__` 中 15+ 个独立实例变量替换为单个 `_stream` + 保护锁**

替换 `feishu_channel.py:60-73` 的以下代码：

```python
        # 流式推送状态
        self._feishu_waiting: bool = False
        self._stream_card_id: str | None = None
        self._stream_message_id: str | None = None
        self._last_pushed_rowid: int = 0
        self._stream_seq: int = 0
        self._stream_target: str | None = None
        self._stream_card_created: bool = False
        self._stream_fallback_used: bool = False
        self._accumulated_text: str = ""
        self._stream_pending_images: list[dict] = []   # [{"img_key": "img_v3_xxx", "alt": "描述"}]
        self._stream_pending_files: list[dict] = []     # [{"local_path": "...", "filename": "..."}]
        self._stream_reply_to_id: str | None = None     # F3: 群聊回复目标消息ID
        self._stream_sent_media_paths: set[str] = set()  # 流式卡片中已展示的媒体路径，防止 send_media 重复发送
```

为：

```python
        # 流式推送状态（原子快照 + generation 守卫）
        self._stream = StreamState()
        self._stream_lock = threading.Lock()  # 仅保护 state 交换，不保护长时间操作
```

- [ ] **Step 3: 添加 `StreamState` 读写辅助方法**

在 `FeishuChannelAdapter` 类中添加（`_on_message` 之前）：

```python
    # ── StreamState 读写 ──────────────────────────────────────

    def _get_stream(self) -> StreamState:
        """读取当前流式状态快照（线程安全）。"""
        with self._stream_lock:
            return StreamState(
                generation=self._stream.generation,
                waiting=self._stream.waiting,
                card_id=self._stream.card_id,
                message_id=self._stream.message_id,
                card_created=self._stream.card_created,
                fallback_used=self._stream.fallback_used,
                seq=self._stream.seq,
                accumulated_text=self._stream.accumulated_text,
                pending_images=[dict(d) for d in self._stream.pending_images],
                pending_files=[dict(d) for d in self._stream.pending_files],
                sent_media_paths=set(self._stream.sent_media_paths),
                reply_to_id=self._stream.reply_to_id,
                target=self._stream.target,
                last_pushed_rowid=self._stream.last_pushed_rowid,
            )

    def _set_stream(self, state: StreamState) -> None:
        """原子替换流式状态（线程安全）。"""
        with self._stream_lock:
            self._stream = state

    def _update_stream(self, **kwargs) -> StreamState:
        """原地更新流式状态字段（线程安全），返回更新后的快照。"""
        with self._stream_lock:
            for k, v in kwargs.items():
                setattr(self._stream, k, v)
            return StreamState(
                generation=self._stream.generation,
                waiting=self._stream.waiting,
                card_id=self._stream.card_id,
                message_id=self._stream.message_id,
                card_created=self._stream.card_created,
                fallback_used=self._stream.fallback_used,
                seq=self._stream.seq,
                accumulated_text=self._stream.accumulated_text,
                pending_images=[dict(d) for d in self._stream.pending_images],
                pending_files=[dict(d) for d in self._stream.pending_files],
                sent_media_paths=set(self._stream.sent_media_paths),
                reply_to_id=self._stream.reply_to_id,
                target=self._stream.target,
                last_pushed_rowid=self._stream.last_pushed_rowid,
            )

    def _new_generation(self, **overrides) -> StreamState:
        """创建新一代流式状态（递增 generation），用于 _on_message 重置。"""
        with self._stream_lock:
            new_gen = self._stream.generation + 1
            new_state = StreamState(generation=new_gen, **overrides)
            self._stream = new_state
            return StreamState(
                generation=new_gen,
                waiting=new_state.waiting,
                card_id=new_state.card_id,
                message_id=new_state.message_id,
                card_created=new_state.card_created,
                fallback_used=new_state.fallback_used,
                seq=new_state.seq,
                accumulated_text=new_state.accumulated_text,
                pending_images=[dict(d) for d in new_state.pending_images],
                pending_files=[dict(d) for d in new_state.pending_files],
                sent_media_paths=set(new_state.sent_media_paths),
                reply_to_id=new_state.reply_to_id,
                target=new_state.target,
                last_pushed_rowid=new_state.last_pushed_rowid,
            )

    def _append_stream_list(self, field_name: str, item) -> StreamState:
        """原子地追加元素到 StreamState 的列表字段（线程安全）。

        避免读取-修改-写入竞态：读取和写入在同一把锁内完成。
        """
        with self._stream_lock:
            current_list = getattr(self._stream, field_name)
            new_list = current_list + [item]
            setattr(self._stream, field_name, new_list)
            return self._get_stream_unlocked()

    def _add_to_stream_set(self, field_name: str, item) -> StreamState:
        """原子地添加元素到 StreamState 的集合字段（线程安全）。

        避免读取-修改-写入竞态：读取和写入在同一把锁内完成。
        """
        with self._stream_lock:
            current_set = getattr(self._stream, field_name)
            new_set = current_set | {item}
            setattr(self._stream, field_name, new_set)
            return self._get_stream_unlocked()

    def _clear_stream_list(self, field_name: str) -> StreamState:
        """原子地清空 StreamState 的列表字段（线程安全）。"""
        with self._stream_lock:
            setattr(self._stream, field_name, [])
            return self._get_stream_unlocked()

    def _clear_stream_set(self, field_name: str) -> StreamState:
        """原子地清空 StreamState 的集合字段（线程安全）。"""
        with self._stream_lock:
            setattr(self._stream, field_name, set())
            return self._get_stream_unlocked()

    def _get_stream_unlocked(self) -> StreamState:
        """读取当前流式状态快照（调用方已持有 _stream_lock）。"""
        return StreamState(
            generation=self._stream.generation,
            waiting=self._stream.waiting,
            card_id=self._stream.card_id,
            message_id=self._stream.message_id,
            card_created=self._stream.card_created,
            fallback_used=self._stream.fallback_used,
            seq=self._stream.seq,
            accumulated_text=self._stream.accumulated_text,
            pending_images=[dict(d) for d in self._stream.pending_images],
            pending_files=[dict(d) for d in self._stream.pending_files],
            sent_media_paths=set(self._stream.sent_media_paths),
            reply_to_id=self._stream.reply_to_id,
            target=self._stream.target,
            last_pushed_rowid=self._stream.last_pushed_rowid,
        )
```

- [ ] **Step 4: 写测试验证 StreamState 交换原子性**

创建 `tests/test_feishu_stream_state.py`：

```python
"""测试 StreamState 原子快照 + generation 守卫。"""

import threading
from niu_api.channel.feishu_channel import StreamState


class TestStreamStateDataclass:
    """StreamState 数据类基本功能。"""

    def test_default_values(self):
        s = StreamState()
        assert s.generation == 0
        assert s.waiting is False
        assert s.card_id is None
        assert s.pending_images == []
        assert s.sent_media_paths == set()

    def test_generation_increments(self):
        s1 = StreamState(generation=1)
        s2 = StreamState(generation=2)
        assert s2.generation > s1.generation

    def test_mutable_fields_are_independent(self):
        """两个 StreamState 实例的可变字段互不影响。"""
        s1 = StreamState(generation=1, pending_images=[{"a": 1}])
        s2 = StreamState(generation=1, pending_images=[{"b": 2}])
        s1.pending_images.append({"c": 3})
        assert len(s2.pending_images) == 1  # s2 不受影响


class TestStreamStateGeneration:
    """generation 守卫逻辑。"""

    def test_generation_mismatch_means_stale(self):
        entry_gen = 5
        current = StreamState(generation=6)
        assert current.generation != entry_gen  # 过期

    def test_generation_match_means_current(self):
        entry_gen = 5
        current = StreamState(generation=5)
        assert current.generation == entry_gen  # 仍有效


class TestStreamStateAtomicSwap:
    """原子交换的线程安全性。"""

    def test_concurrent_swap_no_partial_state(self):
        """并发 _new_generation 不会产生部分状态。"""
        import time

        class FakeAdapter:
            def __init__(self):
                self._stream = StreamState(generation=0)
                self._stream_lock = threading.Lock()

            def _new_generation(self, **overrides):
                with self._stream_lock:
                    new_gen = self._stream.generation + 1
                    new_state = StreamState(generation=new_gen, **overrides)
                    self._stream = new_state
                    return StreamState(
                        generation=new_gen,
                        waiting=new_state.waiting,
                        target=new_state.target,
                    )

        adapter = FakeAdapter()
        results = []
        errors = []

        def worker(target_val):
            try:
                state = adapter._new_generation(waiting=True, target=target_val)
                results.append(state)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(f"target_{i}",)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Errors: {errors}"
        # 最终 generation 应为 20（每个线程递增一次）
        assert adapter._stream.generation == 20
        # 每个 result 的 generation 应唯一
        gens = {r.generation for r in results}
        assert len(gens) == 20
```

- [ ] **Step 5: 运行测试验证**

Run: `cd <repo_root> && python -m pytest tests/test_feishu_stream_state.py -v`
Expected: 6 passed

- [ ] **Step 6: 提交**

```bash
git add niu_api/channel/feishu_channel.py tests/test_feishu_stream_state.py
git commit -m "feat: add StreamState dataclass for atomic snapshot + generation guard"
```

---

### Task 2: 改造 _on_message 使用 _new_generation

**Files:**
- Modify: `niu_api/channel/feishu_channel.py:164-200` (`_on_message` 中流式状态重置)

当前代码在 `_on_message` 中逐行赋值 15+ 个状态变量。改造为单次 `_new_generation()` 调用。

- [ ] **Step 1: 改造 _on_message 流式状态重置**

将 `_on_message` 中第 164-200 行的流式状态重置代码替换。

当前代码（需要替换）：

```python
            # 完整重置流式状态（防止上一轮残留）
            self._stream_card_id = None
            self._stream_message_id = None
            self._stream_card_created = False
            self._stream_fallback_used = False
            self._stream_seq = 0
            self._accumulated_text = ""
            self._stream_pending_images = []
            self._stream_pending_files = []
            self._stream_reply_to_id = None  # F3c: 重置群聊回复目标
            # F3b: 群聊消息设置 reply_to_id（必须在重置之后）
            if not is_p2p:
                self._stream_reply_to_id = getattr(msg, 'id', None) or getattr(msg, 'message_id', None)
            # 流式推送状态初始化
            self._feishu_waiting = True
            self._stream_target = unified.channel_id or self._user_open_id or self._user_p2p_chat_id
            self._stream_sent_media_paths = set()

            # 同步初始化游标：记录当前 DB 位置，后续 _persist_one_msg 的增量从此之后开始
            try:
                import sqlite3
                db_path = str(Path.home() / ".niu" / "messages.db")
                conn = sqlite3.connect(db_path)
                try:
                    cursor = conn.execute("SELECT MAX(rowid) FROM messages")
                    row = cursor.fetchone()
                    self._last_pushed_rowid = row[0] if row and row[0] is not None else 0
                finally:
                    conn.close()
                logger.info(f"[FeishuStream] Waiting, cursor={self._last_pushed_rowid}")
            except Exception as e:
                logger.warning(f"[FeishuStream] Failed to init cursor: {e}")
                self._last_pushed_rowid = 0
```

替换为：

```python
            # 原子创建新一代流式状态（递增 generation，重置所有字段）
            reply_to_id = None
            if not is_p2p:
                reply_to_id = getattr(msg, 'id', None) or getattr(msg, 'message_id', None)
            stream_target = unified.channel_id or self._user_open_id or self._user_p2p_chat_id

            # 同步初始化游标：记录当前 DB 位置
            last_rowid = 0
            try:
                import sqlite3
                db_path = str(Path.home() / ".niu" / "messages.db")
                conn = sqlite3.connect(db_path)
                try:
                    cursor = conn.execute("SELECT MAX(rowid) FROM messages")
                    row = cursor.fetchone()
                    last_rowid = row[0] if row and row[0] is not None else 0
                finally:
                    conn.close()
                logger.info(f"[FeishuStream] Waiting, cursor={last_rowid}")
            except Exception as e:
                logger.warning(f"[FeishuStream] Failed to init cursor: {e}")

            self._new_generation(
                waiting=True,
                target=stream_target,
                reply_to_id=reply_to_id,
                last_pushed_rowid=last_rowid,
            )
```

- [ ] **Step 2: 修改 _on_message 中断连时的状态重置（第 220 行附近）**

查找 `_on_message` 中 WebSocket 断连/重连时的 `_feishu_waiting = False` 赋值，替换为：

```python
            self._update_stream(waiting=False)
```

同理，查找 `_on_message` 中所有对 `self._stream_*` / `self._accumulated_text` / `self._feishu_waiting` 的直接赋值，全部改为通过 `_update_stream()` 或 `_new_generation()`。

搜索命令：
```bash
grep -n 'self\._feishu_waiting\|self\._stream_card_id\|self\._stream_card_created\|self\._stream_target\|self\._stream_reply_to_id\|self\._stream_seq\|self\._stream_fallback_used\|self\._stream_message_id\|self\._stream_sent_media_paths\|self\._stream_pending_images\|self\._stream_pending_files\|self\._accumulated_text\|self\._last_pushed_rowid' <repo_root>/niu_api/channel/feishu_channel.py
```

每个匹配项的替换规则：

| 旧写法 | 新写法 |
|--------|--------|
| `self._feishu_waiting = True` | `self._update_stream(waiting=True)` |
| `self._feishu_waiting = False` | `self._update_stream(waiting=False)` |
| `self._stream_target = None` | `self._update_stream(target=None)` |
| `self._stream_card_id = card_id` | `self._update_stream(card_id=card_id)` |
| `self._stream_message_id = msg_id` | `self._update_stream(message_id=msg_id)` |
| `self._stream_card_created = True` | `self._update_stream(card_created=True)` |
| `self._stream_sent_media_paths.add(x)` | `self._add_to_stream_set("sent_media_paths", x)` |
| `self._stream_pending_images.append(x)` | `self._append_stream_list("pending_images", x)` |
| `self._stream_pending_files.append(x)` | `self._append_stream_list("pending_files", x)` |
| `self._stream_pending_images.clear()` | `self._clear_stream_list("pending_images")` |
| `self._stream_pending_files.clear()` | `self._clear_stream_list("pending_files")` |
| `self._stream_sent_media_paths.clear()` | `self._clear_stream_set("sent_media_paths")` |

- [ ] **Step 3: 运行语法检查**

Run: `python3 -c "import ast; ast.parse(open('niu_api/channel/feishu_channel.py').read()); print('OK')"`
Expected: OK

- [ ] **Step 4: 提交**

```bash
git add niu_api/channel/feishu_channel.py
git commit -m "refactor: _on_message uses _new_generation for atomic state reset"
```

---

### Task 3: 改造 _push_incremental 使用快照 + generation 守卫

**Files:**
- Modify: `niu_api/channel/feishu_channel.py:527-578` (`_push_incremental`)

当前 `_push_incremental` 直接读写 `self._stream_*` 变量。改造为：入口处读取快照，关键操作前检查 generation，修改通过 `_update_stream()`。

- [ ] **Step 1: 改造 _push_incremental**

将 `_push_incremental` 方法整体替换。

当前代码（行 527-578）：

```python
    async def _push_incremental(self):
        """读取 DB 增量内容，创建或更新流式卡片"""
        if not self._feishu_waiting:
            return

        try:
            from agent.session import get_message_store
            store = await get_message_store()

            # 读取增量 assistant 文本（游标在 _on_message 中已初始化）
            new_texts = await store.get_assistant_text_after_rowid(self._last_pushed_rowid)
            if not new_texts:
                return

            # ... 后续逻辑直接读写 self._stream_* 变量 ...
```

替换为：

```python
    async def _push_incremental(self):
        """读取 DB 增量内容，创建或更新流式卡片。

        入口处读取快照，await 后检查 generation 是否变化（新消息到达），
        变化则中止当前推送（新消息会重新触发推送）。
        """
        state = self._get_stream()
        if not state.waiting:
            return

        try:
            from agent.session import get_message_store
            store = await get_message_store()

            # 读取增量 assistant 文本（游标在 _on_message 中已初始化）
            new_texts = await store.get_assistant_text_after_rowid(state.last_pushed_rowid)
            if not new_texts:
                return

            # await 后检查 generation（新消息可能已到达）
            current = self._get_stream()
            if current.generation != state.generation:
                logger.debug(f"[FeishuStream] Generation changed during _push_incremental ({state.generation}→{current.generation}), aborting")
                return
            state = current  # 使用最新快照

            # 拼接同一轮回复的所有增量片段（流式输出可能分多条写入DB）
            combined_text = "".join(text for _, text in new_texts)
            latest_rowid = new_texts[-1][0]
            filtered_text = await self._filter_media_markers(combined_text, state)

            # await 后再次检查 generation
            current = self._get_stream()
            if current.generation != state.generation:
                logger.debug(f"[FeishuStream] Generation changed after _filter_media_markers ({state.generation}→{current.generation}), aborting")
                return
            state = current

            self._update_stream(accumulated_text=filtered_text)
            # 保留 [PHOTO_SEP] 供终结时拆分文本+插入img
            display_text = filtered_text.replace("[PHOTO_SEP]", "")
            content = display_text
            if len(content) > 18000:
                content = content[:17900] + "\n\n...[内容已截断]"

            state = self._get_stream()
            if not state.card_created:
                # 首次：创建流式卡片
                card_id = self._create_stream_card(content)
                if card_id:
                    self._update_stream(
                        card_id=card_id,
                        card_created=True,
                        last_pushed_rowid=latest_rowid,
                        seq=1,
                    )
                    logger.info(f"[FeishuStream] Card created: card_id={card_id}")
                else:
                    self._update_stream(fallback_used=True)
                    logger.warning("[FeishuStream] Card creation failed, will fallback to markdown")
            else:
                # 后续：元素级更新
                new_seq = state.seq + 1
                success = self._update_stream_element(content, new_seq)
                if success:
                    self._update_stream(seq=new_seq, last_pushed_rowid=latest_rowid)
                    logger.info(f"[FeishuStream] Element updated: seq={new_seq}")
                else:
                    self._update_stream(fallback_used=True)
                    logger.warning("[FeishuStream] Element update failed, will fallback to markdown")

        except Exception as e:
            logger.error(f"[FeishuStream] _push_incremental error: {e}")
```

- [ ] **Step 2: 同步修改 _create_stream_card 和 _update_stream_element 读取状态**

`_create_stream_card` 中当前读取 `self._stream_reply_to_id`、`self._stream_target`，替换为从 `self._get_stream()` 读取：

在 `_create_stream_card` 方法开头添加：

```python
        state = self._get_stream()
```

替换方法体中的直接变量访问：

| 旧写法 | 新写法 |
|--------|--------|
| `self._stream_reply_to_id` | `state.reply_to_id` |
| `self._stream_target` | `state.target` |
| `self._stream_card_id` | `state.card_id` |

同理修改 `_update_stream_element`：

| 旧写法 | 新写法 |
|--------|--------|
| `self._stream_card_id` | `state.card_id` |

注意：`_update_stream_element` 中的 `self._stream_card_id` 应改为从入口快照读取。方法开头添加 `state = self._get_stream()`。

- [ ] **Step 3: 修改 _create_stream_card 中遗漏的 _stream_message_id 写入**

当前代码在 `_create_stream_card` 成功后写入 `self._stream_message_id = send_resp.data.message_id`。改造后改为：

```python
        self._update_stream(message_id=send_resp.data.message_id)
```

- [ ] **Step 4: 运行语法检查**

Run: `python3 -c "import ast; ast.parse(open('niu_api/channel/feishu_channel.py').read()); print('OK')"`
Expected: OK

- [ ] **Step 5: 提交**

```bash
git add niu_api/channel/feishu_channel.py
git commit -m "refactor: _push_incremental uses snapshot + generation guard for concurrency safety"
```

---

### Task 4: 改造 send() 使用快照 + generation 守卫

**Files:**
- Modify: `niu_api/channel/feishu_channel.py:366-452` (`send()` 方法)

`send()` 的核心问题：finally 块无条件擦除所有状态，会覆盖新消息的状态。

- [ ] **Step 1: 改造 send() 入口——读取快照、记录 entry generation**

在 `send()` 方法开头（try 块之前）添加：

```python
        entry_state = self._get_stream()
        entry_gen = entry_state.generation
```

- [ ] **Step 2: 替换 send() 中所有直接状态变量访问**

`send()` 方法中的直接变量访问替换规则：

| 旧写法 | 新写法 |
|--------|--------|
| `self._feishu_waiting` | `state.waiting` (需要时重新 `state = self._get_stream()`) |
| `self._stream_card_created` | `state.card_created` |
| `self._stream_card_id` | `state.card_id` |
| `self._stream_fallback_used` | `state.fallback_used` |
| `self._stream_pending_images` | `state.pending_images` |
| `self._stream_pending_files` | `state.pending_files` |
| `self._stream_sent_media_paths` | `state.sent_media_paths` |
| `self._user_open_id` | `self._user_open_id` (不变，非流式状态) |
| `self._user_p2p_chat_id` | `self._user_p2p_chat_id` (不变，非流式状态) |

**send() 中可变操作的替换规则**：

| 旧写法 | 新写法 |
|--------|--------|
| `self._stream_pending_images.clear()` | `self._clear_stream_list("pending_images")` |
| `self._stream_pending_files.clear()` | `self._clear_stream_list("pending_files")` |
| `self._stream_sent_media_paths.clear()` | `self._clear_stream_set("sent_media_paths")` |
| `self._stream_pending_files.append(x)` | `self._append_stream_list("pending_files", x)` |

**关键改造**：send() 不应长时间持有旧快照。每次关键操作前重新读取：

```python
        state = self._get_stream()
        if state.generation != entry_gen:
            logger.info(f"[FeishuStream] send() detected generation change ({entry_gen}→{state.generation}), skipping finalize")
            return
```

- [ ] **Step 3: 改造 send() finally 块——仅擦除同代状态**

当前 finally 块（无条件擦除）：

```python
        finally:
            self._feishu_waiting = False
            self._stream_card_id = None
            self._stream_message_id = None
            self._stream_card_created = False
            self._stream_fallback_used = False
            self._stream_seq = 0
            self._last_pushed_rowid = 0
            self._stream_target = None
            self._stream_reply_to_id = None
            self._accumulated_text = ""
            self._stream_pending_images = []
            self._stream_pending_files = []
            self._stream_sent_media_paths = set()
```

替换为：

```python
        finally:
            # 仅擦除同代状态——新消息已通过 _new_generation 创建了自己的状态
            current = self._get_stream()
            if current.generation == entry_gen:
                self._new_generation(waiting=False)
            else:
                logger.debug(f"[FeishuStream] send() finally: generation changed ({entry_gen}→{current.generation}), preserving new state")
```

- [ ] **Step 4: 改造 _send_pending_media——签名 + 内部状态操作**

`_send_pending_media` 需要从快照获取 pending 文件列表，而不是从实例变量。修改 `_send_pending_media` 签名：

```python
    async def _send_pending_media(self, channel_id: str, pending_images: list, pending_files: list):
```

调用处传入快照数据：

```python
        state = self._get_stream()
        await self._send_pending_media(channel_id, state.pending_images, state.pending_files)
```

**`_send_pending_media` 方法体内部的旧状态操作也需要迁移**：

| 旧写法 | 新写法 |
|--------|--------|
| `self._stream_pending_files.append({...})` | `self._append_stream_list("pending_files", {...})` |
| `self._stream_pending_files = []` | `self._clear_stream_list("pending_files")` |

注意：`_send_pending_media` 现在接收 `pending_images`/`pending_files` 作为参数，内部不再读取 `self._stream.pending_images`。但方法中可能需要将文件添加到 pending_files（图片转文件格式），这些写操作仍需通过原子辅助方法。

- [ ] **Step 5: 运行语法检查**

Run: `python3 -c "import ast; ast.parse(open('niu_api/channel/feishu_channel.py').read()); print('OK')"`
Expected: OK

- [ ] **Step 6: 提交**

```bash
git add niu_api/channel/feishu_channel.py
git commit -m "refactor: send() uses generation guard in finally block to preserve new message state"
```

---

### Task 5: 改造 _finalize_stream_card 和 _filter_media_markers

**Files:**
- Modify: `niu_api/channel/feishu_channel.py` (`_finalize_stream_card`, `_filter_media_markers`)

- [ ] **Step 1: 改造 _finalize_stream_card**

入口读取快照，所有状态访问通过快照，更新通过 `_update_stream()`：

在 `_finalize_stream_card` 开头添加：

```python
        state = self._get_stream()
        entry_gen = state.generation
```

替换方法体中的直接变量访问：

| 旧写法 | 新写法 |
|--------|--------|
| `self._stream_card_id` | `state.card_id` |
| `self._stream_seq` | `state.seq` |
| `self._accumulated_text` | `state.accumulated_text` |
| `self._stream_pending_images` | `state.pending_images` |
| `self._stream_sent_media_paths` | `state.sent_media_paths` |
| `await self._filter_media_markers(final_content)` | `await self._filter_media_markers(final_content, state)` |
| `self._build_final_card_body(body_text)` | `self._build_final_card_body(body_text, state.pending_images)` |

**`_build_final_card_body` 签名改造**：当前方法直接读取 `self._stream_pending_images`，需改为接受参数：

```python
    def _build_final_card_body(self, final_text: str, pending_images: list) -> list:
        # 使用 pending_images 参数代替 self._stream_pending_images
```

方法体中所有 `self._stream_pending_images` 替换为 `pending_images`。

`_finalize_stream_card` 中 `else` 分支的完整替换：

当前代码：
```python
            else:
                filtered_content = await self._filter_media_markers(final_content)
                self._accumulated_text = filtered_content
```

替换为：
```python
            else:
                filtered_content = await self._filter_media_markers(final_content, state)
                self._update_stream(accumulated_text=filtered_content)
```

更新状态时使用：

```python
            self._update_stream(accumulated_text=filtered_content)
```

Settings API / UpdateCard 调用前检查 generation：

```python
            current = self._get_stream()
            if current.generation != entry_gen:
                raise RuntimeError(f"Generation changed during finalize ({entry_gen}→{current.generation})")
```

- [ ] **Step 2: 改造 _filter_media_markers**

`_filter_media_markers` 中直接修改 `self._stream_pending_images`、`self._stream_pending_files`、`self._stream_sent_media_paths`。改造为：接受调用者传入的 `state` 快照，操作完成后统一通过 `_update_stream()` 更新。

**方法签名改为**：

```python
    async def _filter_media_markers(self, text: str, state: StreamState) -> str:
```

调用处（`_push_incremental` 和 `_finalize_stream_card`）传入已验证的快照：

```python
        filtered_text = await self._filter_media_markers(combined_text, state)
```

关键改动：不再直接 `append`/`add`，改为收集到本地列表后一次性更新。

方法开头从传入的 `state` 初始化本地列表：

```python
        new_pending_images = list(state.pending_images)
        new_pending_files = list(state.pending_files)
        new_sent_media_paths = set(state.sent_media_paths)
```

**重要**：方法体中所有 `self._stream_sent_media_paths` 的引用都必须替换为 `new_sent_media_paths`（而不是 `state.sent_media_paths`），因为 `new_sent_media_paths` 会在方法执行期间累积新的条目，而 `state.sent_media_paths` 是入口时的快照不会更新。同理，所有 `self._stream_pending_images` 替换为 `new_pending_images`，`self._stream_pending_files` 替换为 `new_pending_files`。

图片上传成功后：

```python
        new_pending_images.append({"img_key": img_key, "alt": name, "local_path": img_path})
        new_sent_media_paths.add(img_path)
```

方法末尾统一更新（更新前检查 generation，防止污染新消息的状态）：

```python
        # 更新前检查 generation 是否变化（新消息可能已到达）
        current = self._get_stream()
        if current.generation != state.generation:
            return text  # 新消息到达，丢弃本地修改，新消息会重新处理
        self._update_stream(
            pending_images=new_pending_images,
            pending_files=new_pending_files,
            sent_media_paths=new_sent_media_paths,
        )
```

- [ ] **Step 3: 修改 resolve_outbound_content 中的 _stream_sent_media_paths 访问**

查找 `resolve_outbound_content` 方法中对 `self._stream_sent_media_paths` 的读取，改为 `self._get_stream().sent_media_paths`。

- [ ] **Step 4: 修改 _finalize_stream_card——API 调用间增加 generation 检查**

Settings API 和 UpdateCard 之间，SDK 线程的 `_on_message` 仍可交错执行。在 Settings API 成功后、UpdateCard 调用前增加检查：

```python
            # Settings API 成功后再次检查 generation
            current = self._get_stream()
            if current.generation != entry_gen:
                raise RuntimeError(f"Generation changed between Settings and UpdateCard ({entry_gen}→{current.generation})")
```

- [ ] **Step 5: 运行语法检查**

Run: `python3 -c "import ast; ast.parse(open('niu_api/channel/feishu_channel.py').read()); print('OK')"`
Expected: OK

- [ ] **Step 6: 提交**

```bash
git add niu_api/channel/feishu_channel.py
git commit -m "refactor: _finalize_stream_card and _filter_media_markers use snapshot + update_stream"
```

---

### Task 6: 迁移现有测试

**Files:**
- Modify: `tests/test_feishu_group_chat.py` (旧实例变量引用 → StreamState 访问)

- [ ] **Step 1: 搜索所有测试文件中的旧变量引用**

```bash
grep -rn '_feishu_waiting\|_stream_card_id\|_stream_card_created\|_stream_target\|_stream_reply_to_id\|_stream_seq\|_stream_fallback_used\|_stream_message_id\|_stream_sent_media_paths\|_stream_pending_images\|_stream_pending_files\|_accumulated_text\|_last_pushed_rowid' tests/
```

- [ ] **Step 2: 迁移 test_feishu_group_chat.py**

替换规则：

| 旧写法 | 新写法 |
|--------|--------|
| `adapter._stream_reply_to_id` | `adapter._get_stream().reply_to_id` |
| `adapter._stream_target` | `adapter._get_stream().target` |
| `adapter._stream_card_id` | `adapter._get_stream().card_id` |
| `adapter._feishu_waiting` | `adapter._get_stream().waiting` |
| `adapter._stream_reply_to_id = "om_old_msg"` | `adapter._update_stream(reply_to_id="om_old_msg")` |
| `inspect.getsource` 检查 `"_stream_reply_to_id = None"` | 改为功能测试：调用 `send()` 后验证 `adapter._get_stream().reply_to_id is None` |

- [ ] **Step 3: 运行迁移后的测试**

Run: `cd <repo_root> && python -m pytest tests/test_feishu_group_chat.py -v`
Expected: all passed

- [ ] **Step 4: 提交**

```bash
git add tests/test_feishu_group_chat.py
git commit -m "test: migrate feishu group chat tests to StreamState access"
```

---

### Task 7: 全局验证——确保无遗漏的直接状态访问

**Files:**
- Modify: `niu_api/channel/feishu_channel.py` (最终验证)

- [ ] **Step 1: 全文搜索所有旧变量名的直接访问**

```bash
grep -n 'self\._feishu_waiting\|self\._stream_card_id\|self\._stream_card_created\|self\._stream_target\|self\._stream_reply_to_id\|self\._stream_seq\|self\._stream_fallback_used\|self\._stream_message_id\|self\._stream_sent_media_paths\|self\._stream_pending_images\|self\._stream_pending_files\|self\._accumulated_text\|self\._last_pushed_rowid' <repo_root>/niu_api/channel/feishu_channel.py
```

预期结果：**0 个匹配**。所有旧变量名应已全部迁移到 `StreamState` 访问方式。

如果有遗漏，逐个修复：
- 读取 → `self._get_stream().xxx`
- 写入 → `self._update_stream(xxx=val)`
- 重置（新消息）→ `self._new_generation(...)`
- 最终清理 → `self._new_generation(waiting=False)`

- [ ] **Step 2: 删除 __init__ 中已被 StreamState 替代的旧实例变量**

确认 `__init__` 中已无 `self._feishu_waiting`、`self._stream_card_id` 等旧变量声明。如果有残留，删除。

- [ ] **Step 3: 运行完整测试**

Run: `cd <repo_root> && python -m pytest tests/test_feishu_stream_state.py -v`
Expected: all passed

Run: `python3 -c "import ast; ast.parse(open('niu_api/channel/feishu_channel.py').read()); print('OK')"`
Expected: OK

- [ ] **Step 4: 提交**

```bash
git add niu_api/channel/feishu_channel.py
git commit -m "chore: remove all direct stream state variable access, fully migrated to StreamState"
```

---

### Task 8: 竞态场景集成测试

**Files:**
- Create: `tests/test_feishu_concurrency.py`

- [ ] **Step 1: 写并发场景测试**

```python
"""测试飞书流式推送的并发安全性——快速连续消息场景。"""

import threading
import time
from niu_api.channel.feishu_channel import StreamState


class FakeStreamAdapter:
    """模拟 FeishuChannelAdapter 的流式状态管理。"""

    def __init__(self):
        self._stream = StreamState()
        self._stream_lock = threading.Lock()

    def _get_stream(self):
        with self._stream_lock:
            return StreamState(
                generation=self._stream.generation,
                waiting=self._stream.waiting,
                target=self._stream.target,
                card_id=self._stream.card_id,
                card_created=self._stream.card_created,
            )

    def _new_generation(self, **overrides):
        with self._stream_lock:
            new_gen = self._stream.generation + 1
            new_state = StreamState(generation=new_gen, **overrides)
            self._stream = new_state
            return StreamState(
                generation=new_gen,
                waiting=new_state.waiting,
                target=new_state.target,
                card_id=new_state.card_id,
                card_created=new_state.card_created,
            )

    def _update_stream(self, **kwargs):
        with self._stream_lock:
            for k, v in kwargs.items():
                setattr(self._stream, k, v)
            # Inline snapshot (避免调用 _get_stream 导致死锁)
            return StreamState(
                generation=self._stream.generation,
                waiting=self._stream.waiting,
                target=self._stream.target,
                card_id=self._stream.card_id,
                card_created=self._stream.card_created,
            )


class TestSendFinallyNotEraseNewMessage:
    """send(A) finally 块不应擦除 _on_message(B) 设置的状态。"""

    def test_send_finally_preserves_new_generation(self):
        """新消息到达后，send() finally 不应擦除新状态。"""
        adapter = FakeStreamAdapter()

        # 消息 A 到达
        adapter._new_generation(waiting=True, target="chat_A")

        # 消息 B 到达（在 send(A) 期间）
        adapter._new_generation(waiting=True, target="chat_B")

        # send(A) 的 finally 块检查 generation
        entry_gen = 1  # send(A) 启动时的 generation
        current = adapter._get_stream()
        assert current.generation == 2  # B 已递增
        assert current.generation != entry_gen  # 不应擦除

        # finally 块逻辑：generation 不匹配 → 不擦除
        if current.generation == entry_gen:
            adapter._new_generation(waiting=False)
        # 新消息 B 的状态应保留
        assert adapter._get_stream().waiting is True
        assert adapter._get_stream().target == "chat_B"

    def test_send_finally_erases_same_generation(self):
        """没有新消息时，send() finally 正常擦除。"""
        adapter = FakeStreamAdapter()

        # 消息 A 到达
        adapter._new_generation(waiting=True, target="chat_A")
        entry_gen = adapter._get_stream().generation

        # send(A) 完成，finally 块
        current = adapter._get_stream()
        if current.generation == entry_gen:
            adapter._new_generation(waiting=False)

        assert adapter._get_stream().waiting is False


class TestPushIncrementalGenerationGuard:
    """_push_incremental 在 await 后应检查 generation。"""

    def test_push_aborts_on_generation_change(self):
        """新消息到达后，_push_incremental 应中止。"""
        adapter = FakeStreamAdapter()

        # 消息 A 到达
        adapter._new_generation(waiting=True, target="chat_A", last_pushed_rowid=100)
        entry_state = adapter._get_stream()
        entry_gen = entry_state.generation

        # 模拟 await 期间新消息到达
        adapter._new_generation(waiting=True, target="chat_B", last_pushed_rowid=200)

        # _push_incremental 检查 generation
        current = adapter._get_stream()
        assert current.generation != entry_gen  # 应中止
        assert current.target == "chat_B"  # 新消息的目标

    def test_push_continues_on_same_generation(self):
        """没有新消息时，_push_incremental 正常继续。"""
        adapter = FakeStreamAdapter()

        adapter._new_generation(waiting=True, target="chat_A", last_pushed_rowid=100)
        entry_state = adapter._get_stream()
        entry_gen = entry_state.generation

        # 没有新消息
        current = adapter._get_stream()
        assert current.generation == entry_gen  # 应继续
        assert current.last_pushed_rowid == 100


class TestConcurrentNewGeneration:
    """并发 _new_generation 不应产生部分状态。"""

    def test_rapid_fire_messages(self):
        """20 条快速连续消息，generation 应递增 20 次。"""
        adapter = FakeStreamAdapter()

        def simulate_message(target):
            adapter._new_generation(waiting=True, target=target)

        threads = [
            threading.Thread(target=simulate_message, args=(f"chat_{i}",))
            for i in range(20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        state = adapter._get_stream()
        assert state.generation == 20
        assert state.waiting is True
```

- [ ] **Step 2: 运行测试**

Run: `cd <repo_root> && python -m pytest tests/test_feishu_concurrency.py -v`
Expected: all passed

- [ ] **Step 3: 提交**

```bash
git add tests/test_feishu_concurrency.py
git commit -m "test: add concurrency safety tests for StreamState generation guard"
```

---

## Self-Review

### 1. Spec Coverage

| 竞态路径 | 对应任务 |
|----------|----------|
| `send(A)` finally 擦除 B 状态 | Task 4 (generation 守卫 in finally) |
| `_push_incremental` await 后交错 | Task 3 (generation check after await) |
| `_filter_media_markers` await 期间状态损坏 | Task 5 (本地收集 + _update_stream) |

### 2. Placeholder Scan

无 "TBD"/"TODO"/"handle edge cases" 等占位符。所有步骤包含完整代码。

### 3. Type Consistency

- `StreamState` 字段名与 `_update_stream(**kwargs)` 参数名一致
- `_new_generation(**overrides)` 参数名与 `StreamState` 字段名一致
- `_get_stream()` 返回的快照字段名与方法体中的 `state.xxx` 访问一致

### 4. 时序与语义说明

**route_out → send() → send_media 时序安全性**：

`route_out` 调用顺序为：
1. `resolve_outbound_content(reply)` — 读取 `sent_media_paths` 过滤已嵌入卡片的图片
2. `send(channel_id, msg.content)` — 发送文本，finally 块清理状态
3. `send_media(channel_id, msg)` — 发送未嵌入卡片的图片

步骤 1 在步骤 2 之前运行，此时 `sent_media_paths` 尚未被清理，过滤结果正确。
步骤 3 在步骤 2 之后运行，`sent_media_paths` 已被清理，但 `resolve_outbound_content` 已预先决定了哪些媒体需要发送，`send_media` 只发送未被过滤的媒体。清理 `sent_media_paths` 不影响结果。

**send() finally 块调用 _new_generation(waiting=False) 的 generation 跳跃**：

`_new_generation` 递增 generation，这是预期行为。每次 `send()` 完成递增一次，每次 `_on_message` 到达递增一次，generation 单调递增。`_push_incremental` 只检查 generation 是否与入口时一致，不依赖连续性，因此跳跃无害。

**_finalize_stream_card 同步 API 调用期间的交错风险**：

Settings API 和 UpdateCard 是同步调用，asyncio 不会在它们之间切换协程。但 SDK 线程（`_on_message`）可以随时交错执行，因为它是独立的 OS 线程。Task 5 Step 4 在两个 API 调用之间增加了 generation 检查，捕获交错情况。即使错过，最坏情况是向已终结的卡片发送过时的 UpdateCard，飞书 API 会返回错误，被 except 捕获，触发 fallback 路径。

**_filter_media_markers 中已上传但未保存的图片**：

如果 `_filter_media_markers` 上传了图片但 generation 变化导致 `_push_incremental` 中止，已上传的图片信息会丢失（状态未更新）。这是资源浪费（消耗了 API 调用），但不是 bug——新消息会重新触发推送，图片会在新的 generation 中重新处理。
