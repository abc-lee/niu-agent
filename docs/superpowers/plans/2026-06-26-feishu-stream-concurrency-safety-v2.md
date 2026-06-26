# 飞书流式推送并发安全 — 修正实施计划 v2

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 修复飞书流式推送的并发安全问题，同时不破坏已有的正常功能。

**Architecture:** 保留 StreamState + generation 守卫的核心架构（这个方向是正确的），但修复 v1 计划中的设计缺陷。

**Tech Stack:** Python 3.11+, threading.Lock, asyncio, lark-oapi CardKit API

---

## v1 计划的错误根因分析

v1 计划做了正确的架构决策（StreamState 快照 + generation 守卫），但在实现细节上犯了三个根本性错误：

### 错误 1：把 StreamState 当成"不可变快照"来用，但业务逻辑需要"可变状态"

`_get_stream()` 返回只读快照，但 `_finalize_stream_card` 中 Settings API 成功后需要更新 seq，然后 UpdateCard 基于新 seq 计算。v1 没有在 Settings API 成功后写回 seq，导致 UpdateCard 用了和 Settings 相同的 seq。

**重构前**的代码用 `self._stream_seq += 1` 原地修改，每次递增都是独立的，不存在这个问题。

**修正原则**：每次 API 调用成功后，必须立即将新 seq 写回 StreamState（通过 `_update_stream`），然后再读取新快照计算下一个 seq。

### 错误 2：send() 存在两条通道（流式卡片 + markdown），导致双重发送

`send()` 里有一条"普通 markdown 发送"的路径（行 555-565），在终结失败或卡片未创建时执行。但实际场景中：

- `send()` 只在**回复用户消息**时被调用（`route_out` → `adapter.send()`）
- 回复用户消息时，`_on_message` 已经通过 `_new_generation(waiting=True)` 设置了流式状态
- `_push_incremental` 已经将内容推到了流式卡片上
- 终结失败后，再发一条 markdown 就是**重复发送**——用户看到两条消息

**正确做法**：`send()` 只走流式卡片一条通道。终结失败就重试终结，不降级到 markdown。

`push()` 是定时任务用的，没有流式上下文，走 markdown 没问题。两条通道各走各的，不要混在一起。

### 错误 3：`_update_stream` 返回值未检查导致静默失败

多处调用 `_update_stream(expected_generation=...)` 但不检查返回值。generation 不匹配时返回 None，代码继续执行，相当于"假装操作成功了"。

**修正原则**：所有带 `expected_generation` 的 `_update_stream` 调用必须检查返回值。返回 None 时必须中止当前操作（raise 或 return）。

---

## 修改范围

**仅修改一个文件**：`niu_api/channel/feishu_channel.py`

测试文件不需要修改（它们测试的是 StreamState 基础机制和 generation 守卫，这些是正确的）。

---

## Task 1: 修复 `_finalize_stream_card` 的 seq 号管理

**Files:**
- Modify: `niu_api/channel/feishu_channel.py:1002-1093`

**问题**：Settings API 成功后没有将新 seq 写回 StreamState，导致 UpdateCard 使用了与 Settings 相同的 seq 号。

**修复**：Settings API 成功后，立即通过 `_update_stream` 写回新 seq 并刷新快照。UpdateCard 基于新快照计算下一个 seq。同时，所有 `_update_stream` 调用必须检查返回值。

- [ ] **Step 1: 修复 Settings API 成功后的 seq 写回**

当前代码（行 1022-1041）：
```python
new_seq = state.seq + 1
settings_json = json.dumps({"config": {"streaming_mode": False}})
settings_req = SettingsCardRequest.builder() \
    .card_id(state.card_id) \
    .request_body(SettingsCardRequestBody.builder()
        .settings(settings_json)
        .sequence(new_seq)
        .uuid(f"niu-finalize-settings")
        .build()) \
    .build()
settings_resp = self.channel.client.cardkit.v1.card.settings(settings_req)
if not settings_resp.success():
    logger.error(f"[FeishuStream] Settings API failed: {settings_resp.code} {settings_resp.msg}")
    raise RuntimeError(f"Settings API failed: {settings_resp.code} {settings_resp.msg}")

# Settings API 成功后再次检查 generation
current = self._get_stream()
if current.generation != entry_gen:
    raise RuntimeError(f"Generation changed between Settings and UpdateCard ({entry_gen}→{current.generation})")
state = current
```

替换为：
```python
settings_seq = state.seq + 1
settings_json = json.dumps({"config": {"streaming_mode": False}})
settings_req = SettingsCardRequest.builder() \
    .card_id(state.card_id) \
    .request_body(SettingsCardRequestBody.builder()
        .settings(settings_json)
        .sequence(settings_seq)
        .uuid(f"niu-finalize-settings")
        .build()) \
    .build()
settings_resp = self.channel.client.cardkit.v1.card.settings(settings_req)
if not settings_resp.success():
    logger.error(f"[FeishuStream] Settings API failed: {settings_resp.code} {settings_resp.msg}")
    raise RuntimeError(f"Settings API failed: {settings_resp.code} {settings_resp.msg}")

# Settings API 成功后，立即将新 seq 写回 StreamState
if self._update_stream(expected_generation=entry_gen, seq=settings_seq) is None:
    raise RuntimeError(f"Generation changed after Settings API ({entry_gen})")
# 刷新快照，让 UpdateCard 基于新 seq 计算
state = self._get_stream()
if state.generation != entry_gen:
    raise RuntimeError(f"Generation changed after Settings API ({entry_gen}→{state.generation})")
```

- [ ] **Step 2: 修复 UpdateCard 的 seq 计算**

当前代码（行 1044）：
```python
new_seq = state.seq + 1
```

替换为：
```python
update_seq = state.seq + 1
```

后续所有引用 `new_seq` 的地方改为 `update_seq`（行 1074 和 1088）。

- [ ] **Step 3: 检查行 1019 的 `_update_stream` 返回值**

当前代码：
```python
self._update_stream(expected_generation=entry_gen, accumulated_text=filtered_content)
```

替换为：
```python
if self._update_stream(expected_generation=entry_gen, accumulated_text=filtered_content) is None:
    raise RuntimeError(f"Generation changed before Settings API ({entry_gen})")
```

- [ ] **Step 4: 检查行 1092 的 `_update_stream` 返回值**

当前代码：
```python
self._update_stream(expected_generation=entry_gen, fallback_used=True)
```

替换为：
```python
if self._update_stream(expected_generation=entry_gen, fallback_used=True) is None:
    logger.debug(f"[FeishuStream] Generation changed during finalize exception, skipping fallback_used")
```

---

## Task 2: 简化 `send()` — 只走流式卡片一条通道

**Files:**
- Modify: `niu_api/channel/feishu_channel.py:473-572`

**核心改动**：`send()` 只负责终结流式卡片，不再有 markdown fallback。终结失败就重试，不降级。

**设计原则**：
- `send()` = 回复用户消息 = 流式卡片的终结阶段
- `push()` = 定时任务主动推送 = markdown（无流式上下文）
- 两条通道各走各的，不要混在一起

- [ ] **Step 1: 重写 `send()` 方法**

当前代码（行 473-572）有复杂的 fallback 逻辑：终结失败 → 发 markdown → 发两遍。

替换为：
```python
async def send(self, channel_id: str, content: str) -> None:
    """发送消息到飞书 — 终结流式卡片（只走流式卡片一条通道）。

    send() 只在回复用户消息时被调用（route_out → adapter.send()）。
    此时 _on_message 已经通过 _new_generation(waiting=True) 设置了流式状态，
    _push_incremental 已经将内容推到了流式卡片上。
    send() 只需要终结卡片 + 发送待处理文件，不再有 markdown fallback。
    """
    entry_state = self._get_stream()
    entry_gen = entry_state.generation

    try:
        # 确保流式推送已完成（消除竞态：协程可能还没执行完）
        state = self._get_stream()
        if state.waiting and not state.card_created:
            try:
                await self._push_incremental()
            except Exception as e:
                logger.warning(f"[FeishuStream] Pre-send push failed: {e}")
            # await 后检查 generation
            if self._get_stream().generation != entry_gen:
                logger.debug("[FeishuStream] Generation changed after pre-send push, aborting send")
                return

        state = self._get_stream()
        if state.card_created:
            # 流式卡片存在 → 终结卡片
            try:
                await self._finalize_stream_card(content)
            except Exception as e:
                logger.error(f"[FeishuStream] Finalize failed on first attempt: {e}")
                # 终结失败 → 重试一次（可能是瞬时 seq 错误）
                try:
                    await asyncio.sleep(1)
                    if self._get_stream().generation != entry_gen:
                        logger.debug("[FeishuStream] Generation changed, aborting finalize retry")
                        return
                    await self._finalize_stream_card(content)
                except Exception as e2:
                    logger.error(f"[FeishuStream] Finalize failed on retry: {e2}")
                    # 两次终结都失败 → 不降级到 markdown（卡片已展示内容）
                    # 图片嵌入失败时，通过 _send_pending_media 独立发送
                    current = self._get_stream()
                    if current.generation == entry_gen:
                        # 将未嵌入卡片的图片转为独立消息发送
                        for img_info in current.pending_images:
                            local_path = img_info.get("local_path")
                            if local_path:
                                self._append_stream_list("pending_files", {
                                    "local_path": local_path,
                                    "filename": img_info.get("alt", Path(local_path).name),
                                    "kind": "image",
                                }, expected_generation=entry_gen)
                        self._clear_stream_list("pending_images", expected_generation=entry_gen)
                        self._clear_stream_set("sent_media_paths", expected_generation=entry_gen)

        # 发送待处理的文件（独立于卡片的附件）
        if self._get_stream().generation == entry_gen:
            await self._send_pending_media(channel_id)

    finally:
        # 仅擦除同代状态——新消息已通过 _new_generation 创建了自己的状态
        current = self._get_stream()
        if current.generation == entry_gen:
            self._new_generation(waiting=False)
        else:
            logger.debug(f"[FeishuStream] send() finally: generation changed ({entry_gen}→{current.generation}), preserving new state")
```

**关键变化**：
1. 删除了行 555-565 的 `channel.send(target, {"markdown": content})` — 不再有 markdown fallback
2. 终结失败后重试一次，而不是降级到 markdown
3. 两次终结都失败时，只通过 `_send_pending_media` 发送图片/文件，不再发文本
4. 删除了行 537-545 的媒体标记剥离逻辑（不再需要，因为不发 markdown 了）

- [ ] **Step 2: 简化 `_send_pending_media` — 从 state 读取，不再传参**

当前签名：
```python
async def _send_pending_media(self, channel_id: str, pending_images: list):
```

替换为：
```python
async def _send_pending_media(self, channel_id: str):
    """终结后发送待处理的文件。从 StreamState 读取 pending_files。"""
    state = self._get_stream()
    if not state.pending_files:
        return
    entry_gen = state.generation
    pending_files = list(state.pending_files)
```

方法体中删除行 1098-1106 的转换逻辑（pending_images → pending_files 格式转换），因为 `send()` 中已经负责了转换。方法体直接遍历 `pending_files` 发送。

**所有调用点更新**：
- 行 506: `await self._send_pending_media(channel_id, state.pending_images)` → `await self._send_pending_media(channel_id)`
- 行 532: `await self._send_pending_media(channel_id, state.pending_images)` → `await self._send_pending_media(channel_id)`
- 行 552: `await self._send_pending_media(channel_id, state.pending_images)` → `await self._send_pending_media(channel_id)`

---

## Task 3: 检查所有 `_update_stream` 调用的返回值

**Files:**
- Modify: `niu_api/channel/feishu_channel.py`

- [ ] **Step 1: 检查 `_push_incremental` 中的返回值**

行 706：
```python
self._update_stream(expected_generation=state.generation, fallback_used=True)
```
改为：
```python
if self._update_stream(expected_generation=state.generation, fallback_used=True) is None:
    logger.debug("[FeishuStream] Generation changed, skipping fallback_used")
```

行 718 同理。

- [ ] **Step 2: 检查 `_on_message` 中行 327 的 `_update_stream`**

当前代码：
```python
self._update_stream(waiting=False, target=None)
```

在 `_on_message` 的 `_new_generation` 调用之后（行 304 之后），记录：
```python
entry_gen = self._get_stream().generation
```

然后行 327 改为：
```python
if self._update_stream(expected_generation=entry_gen, waiting=False, target=None) is None:
    logger.debug("[FeishuStream] Generation changed, not resetting waiting state")
```

---

## 实施顺序

1. **Task 1** — 修复 seq 号管理（最关键，直接影响功能可用性）
2. **Task 2** — 简化 send() + _send_pending_media（消除双重发送的根源）
3. **Task 3** — 检查所有 `_update_stream` 返回值（防御性修复）

---

## 验证清单

每个 Task 完成后，运行以下验证：

1. `python -c "from niu_api.channel.feishu_channel import FeishuChannelAdapter, StreamState"` — 确认语法正确
2. `python -m pytest tests/test_feishu_concurrency.py -v` — 确认并发测试通过
3. `python -m pytest tests/test_feishu_stream_state.py -v` — 确认 StreamState 测试通过

所有 Task 完成后：
4. 启动程序 `./niu`，发送一条普通文本消息 — 确认流式推送正常、不重复发送
5. 发送一条带照片的消息 — 确认照片正常展示、seq 号不再重复
6. 快速连续发送两条消息 — 确认并发安全（第二条消息不被擦除）

---

## 与 v1 计划的区别

| 方面 | v1 计划 | v2 计划 |
|------|---------|---------|
| 核心架构 | StreamState + generation 守卫 | **相同**（架构方向正确） |
| seq 管理 | 未考虑 Settings→UpdateCard 的 seq 传递 | **Settings 成功后立即写回 seq** |
| send() 通道 | 流式卡片 + markdown fallback（两条通道） | **只有流式卡片一条通道** |
| 终结失败处理 | 降级到 markdown（导致双重发送） | **重试终结，不降级** |
| _update_stream 返回值 | 多处未检查 | **全部检查，返回 None 时中止** |
| _send_pending_media | 接受列表参数（参数语义混乱） | **从 state 读取，不传参** |
| 修改范围 | 8 个 Task，大量代码改动 | **3 个 Task，精确修复** |
