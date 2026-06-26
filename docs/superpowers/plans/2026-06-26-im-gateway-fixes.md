# IM Gateway 修复计划 — 代码审查发现的问题

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 修复代码审查发现的问题，使 gateway.py 达到生产质量。

**Architecture:** 保留现有 IMGateway 类结构，修复并发模型、连接生命周期、子进程管理、测试质量、注释一致性。

**Tech Stack:** Python 3.11+, asyncio.Lock, threading.Lock, asyncio.StreamWriter, time.monotonic

---

## 设计决策（每个决策必须写清 why）

### D1: 并发写入序列化 — asyncio.Lock

**问题**：`_async_send` 在锁外执行 `writer.write() + await writer.drain()`。两个协程（如 send + watchdog PING）可交错 write+drain，破坏长度前缀协议。

**Why asyncio.Lock 而非 threading.Lock**：threading.Lock 阻塞线程，在 asyncio 协程中不能用（会阻塞事件循环）。asyncio.Lock 是协程级锁，`async with` 在 await 点让出控制权，不会阻塞事件循环。

**Why 不用 threading.Lock 保护 write**：threading.Lock 保护的是跨线程的共享状态（_writer 引用本身）。但 write+drain 是纯 asyncio 操作，两个协程在同一个事件循环中，需要 asyncio.Lock 序列化。

**设计**：
- `self._lock`（threading.Lock）：保护 `_writer`、`_push_target`、`_adapter_name` 的引用赋值（跨线程安全）
- `self._write_lock`（asyncio.Lock）：序列化 `writer.write() + await writer.drain()`（协程级安全）

**两把锁的分工**：
1. 获取 writer 引用：`with self._lock: writer = self._writer`（防止读到半写的引用）
2. 写入+排空：`async with self._write_lock: writer.write() + await writer.drain()`（防止两个协程交错写入）

**不会死锁**：嵌套顺序固定：_write_lock 始终包含 _lock。_lock 可能短暂地在 _write_lock 之前获取（如 _async_send 中取 writer 引用），但在 _write_lock 被获取之前释放。在 _write_lock 内部获取 _lock 时，不存在循环等待。

**关键：关闭 writer 也必须获取 _write_lock**。_handle_adapter finally 和 stop() 关闭 writer 前，必须先获取 _write_lock，确保正在进行的写入完成后再关闭。否则 writer.close() 会打断正在进行的 write+drain，破坏协议。_handle_adapter 入口关闭旧 writer 时同理。

### D2: 连接生命周期 — writer 关闭

**问题**：`_handle_adapter` 的 finally 块只清 `_writer = None`，不 close writer，导致 FD 泄漏。`stop()` 读取 `_writer` 未加锁。

**设计**：
- `_handle_adapter` finally：先获取 _write_lock（等正在进行的写入完成），再在 _lock 内清 `_writer = None`，再 close writer + wait_closed（带 5s 超时）
- `_handle_adapter` 入口（关闭旧 writer）：同样先获取 _write_lock，再 close
- `stop()`：先获取 _write_lock，再在 _lock 内取 writer 引用并清 `_writer = None`，再 close（带 5s 超时）
- `_async_send` 异常时：已在 _write_lock 内，在 _lock 内清 `_writer = None`，再 close

**wait_closed 超时**：事件循环关闭期间 wait_closed() 可能挂起，用 `asyncio.wait_for(writer.wait_closed(), timeout=5.0)` 保护。

### D3: 子进程管理 — 重启计数器基于时间重置

**问题**：`_restart_count` 只增不减，3 次短暂故障后永久放弃。但如果连接后立即重置，Adapter 连上就断会绕过 _MAX_RESTARTS，导致无限重启循环。

**设计**：记录 `_connected_since` 时间戳。watchdog 检查时，如果 Adapter 已稳定连接超过 60 秒，才重置 `_restart_count = 0`。理由：60 秒稳定连接说明配置正确、网络正常，之前的崩溃是瞬时问题。

### D4: _send_command Future 处理

**问题**：`run_coroutine_threadsafe` 返回的 Future 被丢弃，异常静默丢失。

**设计**：添加命名 done_callback 函数记录异常。不用 lambda（复杂条件表达式不可读且脆弱）。不阻塞调用线程。

### D5: 属性加锁

**问题**：`push_target` 和 `adapter_name` 属性在锁外读取。

**设计**：加锁。虽然 CPython GIL 保证引用读取原子性，但代码自身的不变量是"所有 _push_target/_adapter_name 访问都通过 _lock"，属性访问器不应违反这个不变量。

### D6: route_in_sync fallback 保持 "im"

**问题**：`route_in_sync` fallback "im" vs `route_in` fallback "electron"。

**设计**：保持 "im" 不变。理由：route_in_sync 只被 IM 通道调用（IMGateway._on_msg），fallback "im" 是正确的语义。如果改为 "electron"，当 message.channel 为空时 session_id 会变成 "electron:xxx"，这是错误的。route_in 和 route_in_sync 的 fallback 不同是合理的——它们服务不同的通道。

### D7: 流式通知测试质量

**问题**：test_stream_notification 无论是否收到 STREAM 都报 PASS。

**设计**：改为 assert。如果 Agent 回复太快导致 STREAM 不可靠，这个测试应该标记为可选或用更长的消息触发流式推送。但当前阶段，assert 至少能检测到流式推送完全损坏的情况。

### D8: base.py 注释修正

**问题**：注释说"IM 通道重写 resolve_outbound_content"，但 IMGateway 不重写。

**设计**：更新注释反映 Gateway 的 Markdown 透传设计。

---

## 修改范围

**仅修改 3 个文件**：
1. `niu_api/channel/gateway.py` — 核心修复（D1-D5）
2. `tests/test_im_gateway_integration.py` — 测试质量（D7）
3. `niu_api/channel/base.py` — 注释修正（D8）

注：route_in_sync fallback 保持 "im" 不变（D6），不需要修改 `__init__.py`。

---

## Task 1: 修复 gateway.py — 并发写入序列化 + 连接生命周期 + 子进程管理 + Future 处理 + 属性加锁

**Files:**
- Modify: `niu_api/channel/gateway.py`

**修改清单（按行号顺序）：**

- [ ] **Step 1: 顶层添加 import time + __init__ 添加 _write_lock 和 _connected_since**

在文件顶部 import 区域添加 `import time`（在 `import threading` 之后）。

当前代码（行 31-37）：
```python
        self._lock = threading.Lock()
        self._connected = threading.Event()
        self._loop = None
        self._stopping = False
        self._restart_count = 0
        self._MAX_RESTARTS = 3
        self._MAX_MESSAGE_SIZE = 10 * 1024 * 1024  # 10MB
```

替换为：
```python
        self._lock = threading.Lock()
        self._write_lock = asyncio.Lock()
        self._connected = threading.Event()
        self._loop = None
        self._stopping = False
        self._restart_count = 0
        self._MAX_RESTARTS = 3
        self._MAX_MESSAGE_SIZE = 10 * 1024 * 1024  # 10MB
        self._connected_since = 0.0
```

- [ ] **Step 2: _adapter_watchdog — 基于时间的重启计数器重置**

当前代码（行 53-74）：
```python
    async def _adapter_watchdog(self):
        """定期检查 Adapter 子进程 + TCP 连接健康"""
        while not self._stopping:
            await asyncio.sleep(10)
            if self._stopping:
                break
            if self._adapter_proc is not None:
                retcode = self._adapter_proc.poll()
                if retcode is not None:
                    self._restart_count += 1
                    if self._restart_count > self._MAX_RESTARTS:
                        logger.error(f"[IMGateway] Adapter exited {self._restart_count} times, giving up restart")
                        self._adapter_proc = None
                        break
                    logger.warning(f"[IMGateway] Adapter process exited (code={retcode}), restarting ({self._restart_count}/{self._MAX_RESTARTS})...")
                    self._adapter_proc = None
                    self._launch_adapter()
            if self._connected.is_set():
                try:
                    await self._async_send({"type": "PING"})
                except Exception:
                    logger.warning("[IMGateway] Adapter health check failed")
```

替换为：
```python
    async def _adapter_watchdog(self):
        """定期检查 Adapter 子进程 + TCP 连接健康"""
        while not self._stopping:
            await asyncio.sleep(10)
            if self._stopping:
                break
            # 稳定连接超过 60 秒 → 重置重启计数器
            if self._connected.is_set() and self._restart_count > 0:
                if time.monotonic() - self._connected_since > 60:
                    logger.info("[IMGateway] Adapter stable for 60s, resetting restart count")
                    self._restart_count = 0
            if self._adapter_proc is not None:
                retcode = self._adapter_proc.poll()
                if retcode is not None:
                    self._restart_count += 1
                    if self._restart_count > self._MAX_RESTARTS:
                        logger.error(f"[IMGateway] Adapter exited {self._restart_count} times, giving up restart")
                        self._adapter_proc = None
                        break
                    logger.warning(f"[IMGateway] Adapter process exited (code={retcode}), restarting ({self._restart_count}/{self._MAX_RESTARTS})...")
                    self._adapter_proc = None
                    self._launch_adapter()
            if self._connected.is_set():
                try:
                    await self._async_send({"type": "PING"})
                except Exception:
                    logger.warning("[IMGateway] Adapter health check failed")
```

- [ ] **Step 3: stop() — 加锁读取 writer + _write_lock + 超时 wait_closed**

当前代码（行 76-94）：
```python
    async def stop(self):
        """停止 Server + 终止 Adapter"""
        self._stopping = True
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if self._adapter_proc:
            self._adapter_proc.terminate()
            try:
                self._adapter_proc.wait(timeout=5)
            except Exception:
                self._adapter_proc.kill()
        logger.info("[IMGateway] Stopped")
```

替换为：
```python
    async def stop(self):
        """停止 Server + 终止 Adapter"""
        self._stopping = True
        async with self._write_lock:
            with self._lock:
                writer = self._writer
                self._writer = None
        if writer:
            try:
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), timeout=5.0)
            except Exception:
                pass
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if self._adapter_proc:
            self._adapter_proc.terminate()
            try:
                self._adapter_proc.wait(timeout=5)
            except Exception:
                self._adapter_proc.kill()
        logger.info("[IMGateway] Stopped")
```

- [ ] **Step 4: _handle_adapter — 入口关闭旧 writer 加 _write_lock + 记录 _connected_since**

当前代码（行 121-134）：
```python
    async def _handle_adapter(self, reader, writer):
        """处理 Adapter 连接"""
        with self._lock:
            if self._writer is not None:
                logger.warning("[IMGateway] Previous adapter disconnected, accepting new connection")
                try:
                    self._writer.close()
                except Exception:
                    pass
            self._writer = writer
            self._connected.set()

        addr = writer.get_extra_info("peername")
        logger.info(f"[IMGateway] Adapter connected from {addr}")
```

替换为：
```python
    async def _handle_adapter(self, reader, writer):
        """处理 Adapter 连接"""
        async with self._write_lock:
            with self._lock:
                old_writer = self._writer
                if old_writer is not None:
                    logger.warning("[IMGateway] Previous adapter disconnected, accepting new connection")
                self._writer = writer
                self._connected.set()
                self._connected_since = time.monotonic()
            if old_writer is not None:
                try:
                    old_writer.close()
                    await asyncio.wait_for(old_writer.wait_closed(), timeout=5.0)
                except Exception:
                    pass

        addr = writer.get_extra_info("peername")
        logger.info(f"[IMGateway] Adapter connected from {addr}")
```

- [ ] **Step 5: _handle_adapter finally — 加 _write_lock + 关闭 writer + 超时 wait_closed**

当前代码（行 151-157）：
```python
        finally:
            with self._lock:
                self._writer = None
                self._connected.clear()
                self._adapter_name = None
                self._push_target = None
            logger.info("[IMGateway] Adapter disconnected")
```

替换为：
```python
        finally:
            async with self._write_lock:
                with self._lock:
                    self._writer = None
                    self._connected.clear()
                    self._adapter_name = None
                    self._push_target = None
                try:
                    writer.close()
                    await asyncio.wait_for(writer.wait_closed(), timeout=5.0)
                except Exception:
                    pass
            logger.info("[IMGateway] Adapter disconnected")
```

- [ ] **Step 6: _async_send — 添加 _write_lock 序列化写入**

当前代码（行 196-216）：
```python
    async def _async_send(self, cmd: dict):
        """发送指令给 Adapter（async，带 drain）"""
        with self._lock:
            writer = self._writer
        if writer is None:
            return
        try:
            payload = json.dumps(cmd, ensure_ascii=False).encode("utf-8")
            header = len(payload).to_bytes(4, "big")
            writer.write(header + payload)
            await writer.drain()
        except Exception as e:
            logger.error(f"[IMGateway] Send command failed: {e}")
            with self._lock:
                if self._writer is writer:
                    self._writer = None
                    self._connected.clear()
            try:
                writer.close()
            except Exception:
                pass
```

替换为：
```python
    async def _async_send(self, cmd: dict):
        """发送指令给 Adapter（async，带 drain）

        _lock 保护 writer 引用的读取（跨线程安全）。
        _write_lock 序列化 write+drain（协程级安全，防止两个协程交错写入破坏协议）。
        """
        with self._lock:
            writer = self._writer
        if writer is None:
            return
        async with self._write_lock:
            try:
                payload = json.dumps(cmd, ensure_ascii=False).encode("utf-8")
                header = len(payload).to_bytes(4, "big")
                writer.write(header + payload)
                await writer.drain()
            except Exception as e:
                logger.error(f"[IMGateway] Send command failed: {e}")
                with self._lock:
                    if self._writer is writer:
                        self._writer = None
                        self._connected.clear()
                try:
                    writer.close()
                except Exception:
                    pass
```

- [ ] **Step 7: _send_command — 添加命名回调函数**

当前代码（行 218-221）：
```python
    def _send_command(self, cmd: dict):
        """线程安全发送 — 从 executor 线程调用"""
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._async_send(cmd), self._loop)
```

替换为：
```python
    @staticmethod
    def _on_send_done(f):
        """run_coroutine_threadsafe 的 done callback — 记录异常"""
        if f.cancelled():
            return
        exc = f.exception()
        if exc:
            logger.error(f"[IMGateway] Async send failed: {exc}")

    def _send_command(self, cmd: dict):
        """线程安全发送 — 从 executor 线程调用"""
        if self._loop and self._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self._async_send(cmd), self._loop)
            future.add_done_callback(self._on_send_done)
```

- [ ] **Step 8: push_target 和 adapter_name 属性加锁**

当前代码（行 253-259）：
```python
    @property
    def push_target(self) -> str | None:
        return self._push_target

    @property
    def adapter_name(self) -> str | None:
        return self._adapter_name
```

替换为：
```python
    @property
    def push_target(self) -> str | None:
        with self._lock:
            return self._push_target

    @property
    def adapter_name(self) -> str | None:
        with self._lock:
            return self._adapter_name
```

- [ ] **Step 9: 语法验证**

```bash
python -c "from niu_api.channel.gateway import IMGateway; print('OK')"
```

---

## Task 2: 修复流式通知测试 — 不再虚假 PASS

**Files:**
- Modify: `tests/test_im_gateway_integration.py`

- [ ] **Step 1: 修改 test_stream_notification**

当前代码（行 213-216 附近）：
```python
if stream_count > 0:
    print(f"[测试] PASS 收到 {stream_count} 条 STREAM 通知")
else:
    print("[测试] WARN 未收到 STREAM 通知（Agent 可能回复太快）")
```

替换为：
```python
if stream_count > 0:
    print(f"[测试] PASS 收到 {stream_count} 条 STREAM 通知")
else:
    raise AssertionError("未收到任何 STREAM 通知 — 流式推送可能已损坏")
```

- [ ] **Step 2: 语法验证**

```bash
python -c "import tests.test_im_gateway_integration; print('OK')"
```

---

## Task 3: 修正 base.py 注释

**Files:**
- Modify: `niu_api/channel/base.py:54`

- [ ] **Step 1: 更新注释**

当前代码：
```python
        IM 通道重写：提取 Markdown 图片 ![alt](path) / 文件链接 [name](path) 标记 → 返回多条消息
```

替换为：
```python
        IM 通道 (Gateway)：使用默认实现（Markdown 透传给 Adapter，由 Adapter 解释图片/文件标记）
```

- [ ] **Step 2: 语法验证**

```bash
python -c "from niu_api.channel.base import ChannelAdapter; print('OK')"
```

---

## 验证清单

每个 Task 完成后：
1. 语法验证命令通过
2. `python -m pytest tests/test_im_gateway.py -v` — 单元测试通过

所有 Task 完成后：
3. 启动 `./niu`，检查日志确认 Gateway 启动成功（无端口冲突）
4. `python tests/test_im_gateway_integration.py` — 集成测试通过
5. 杀掉所有 niu 进程

---

## 审查检查项（审查 Agent 必须逐项检查）

- [ ] C1: _write_lock 是 asyncio.Lock（不是 threading.Lock），在 _async_send 中用 `async with` 获取
- [ ] C2: _lock 和 _write_lock 获取顺序固定（先 _lock 取引用，再 _write_lock 写入），无死锁风险
- [ ] C3: _handle_adapter finally 中先获取 _write_lock，再在 _lock 内清 _writer=None，再 close writer + wait_closed（带超时）
- [ ] C4: _handle_adapter 入口关闭旧 writer 时也获取 _write_lock
- [ ] C5: stop() 中先获取 _write_lock，再在 _lock 内取 writer 并清 _writer=None，再 close + wait_closed（带超时）
- [ ] C6: _restart_count 基于时间重置（稳定 60 秒后），不是连接时立即重置
- [ ] C7: _connected_since 在 _lock 内设置（与 _connected.set() 同步）
- [ ] C8: _send_command 使用命名回调函数 _on_send_done，不用 lambda
- [ ] C9: push_target/adapter_name 属性在 _lock 内返回值
- [ ] C10: route_in_sync fallback 保持 "im" 不变（不修改 __init__.py）
- [ ] C11: test_stream_notification 在 stream_count==0 时 raise AssertionError
- [ ] C12: base.py 注释反映 Gateway Markdown 透传设计
- [ ] C13: 无新增端口冲突（19877 不与 WSBridge 19876 冲突）
- [ ] C14: 无遗漏的 "feishu" 引用（grep 验证）
- [ ] C15: 所有 writer.close() 路径都获取了 _write_lock（_async_send 异常路径除外——已在 _write_lock 内）
- [ ] C16: wait_closed() 都有 5s 超时保护
- [ ] C17: _on_send_done 是 @staticmethod，不依赖 self（Future callback 在任意线程执行）
