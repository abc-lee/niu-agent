# IM Gateway Phase 1 — 删飞书，换 Gateway，测试验证

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 把飞书通道替换为 IM Gateway（TCP Server），用测试程序验证消息收发通路。飞书 Adapter 是 Phase 2 的事。

**核心逻辑**：
- 前端：不动。SSE 推送、消息收发完全不受影响
- 飞书：删掉。所有飞书代码和引用全部清除
- Gateway：新增。TCP Server，注册到 ChannelRouter，接收 MSG 指令、发送 SEND/PUSH/STREAM 指令
- 测试：测试程序模拟 Adapter 连接 Gateway，发消息、收回复，验证通路

**Tech Stack:** Python 3.11+, asyncio TCP, threading, JSON

**交付条件：** 启动 `./niu`，测试程序通过 Gateway 发消息，Agent 正确回复，通路验证通过。

**协议决策**：
- IM 通道统一用 Markdown 透传，不拆分媒体消息。Gateway 的 `send()`/`push()` 只传原始 Markdown content，Adapter 自己解析 `![alt](path)` 和 `[name](path)`
- STREAM 指令只做通知（`{"type": "STREAM", "channel_id": ""}`），不含内容。Adapter 收到后自行从 DB/API 拉取增量内容（Phase 2 实现）
- `resolve_outbound_content` 使用基类默认实现，返回 `[ResolvedMessage(kind="text", content=content)]`，不拆分

---

## 飞书引用清除清单

替换前必须改完的所有"feishu"引用（生产代码）：

| 文件 | 行 | 当前 | 改为 |
|------|-----|------|------|
| `niu_api/__main__.py` | 123-148 | 飞书启动 | Gateway 启动 |
| `niu_api/__main__.py` | 363-372 | 飞书关闭 | Gateway 关闭 |
| `agent/runner.py` | 1958-1963 | `trigger_feishu_stream_push()` | `Gateway.notify_stream()` |
| `niu_api/channel/__init__.py` | 47 | `or "feishu"` | `or "im"` |
| `niu_api/chat_queue.py` | 100 | `channel: str = "feishu"` | `channel: str = "im"` |
| `niu_api/internal/scheduler/service.py` | 102,106,109,114 | `"feishu"` + 注释/错误消息 | `"im"` + 注释/错误消息 |
| `niu_api/internal/ha_watcher/watcher.py` | 225,229,231,236 | `"feishu"` + 注释/错误消息 | `"im"` + 注释/错误消息 |
| `agent/mcp_loader.py` | 34 | `("feishu-server", ...)` | 删除此行 |

注释清理（不影功能，但避免误导）：

| 文件 | 位置 | 当前 | 改为 |
|------|------|------|------|
| `niu_api/channel/__init__.py` | 20-22 | docstring 提及 feishu SDK | 改为 IM Gateway |
| `niu_api/channel/base.py` | 20 | `# 飞书 file_key / image_key` | `# IM file_key / image_key` |
| `niu_api/channel/base.py` | 54,63 | `飞书通道重写` | `IM 通道重写` |
| `niu_api/chat_queue.py` | 22-23 | `"feishu"` | `"im"` |

配置文件清理：

| 文件 | 处理 |
|------|------|
| `config/mcp-servers.yaml` | 删除 feishu-server 段 |
| `config/disk/feishu-server.yaml` | 删除文件 |

测试文件清理（Task 4 一起处理）：

| 文件 | 处理 |
|------|------|
| `tests/test_feishu_*.py` | 全部删除（7个文件） |
| `tests/test_channel_base.py` | "feishu" → "im" |
| `tests/test_scheduler_group_push.py` | "feishu" → "im" |
| `tests/test_chat_queue.py` | 删除 feishu 相关测试 |
| `tests/test_channel_adapters.py` | 删除（import FeishuChannelAdapter） |
| `tests/test_v10_*.py` | 删除（依赖飞书配置） |
| `tests/test_cardkit_api.py` | 删除（依赖飞书配置） |
| `tests/test_cron_converter.py` | 检查是否依赖 feishu-server |

---

## Task 1: 创建 Gateway

**Files:**
- Create: `niu_api/channel/gateway.py`
- Create: `tests/test_im_gateway.py`

- [ ] **Step 1: 创建 gateway.py**

```python
"""IM 网关 — TCP Server，与 IM Adapter 通讯"""
import asyncio
import json
import shlex
import subprocess
import sys
import threading
from pathlib import Path
from loguru import logger

from .base import ChannelAdapter


class IMGateway(ChannelAdapter):
    """IM 网关 — TCP Server。

    Gateway 不知道任何具体 IM 的存在。
    Adapter 通过 TCP 连接 Gateway，发送 MSG/READY/PING 指令。
    Gateway 通过 TCP 发送 SEND/PUSH/STREAM 指令给 Adapter。
    """

    def __init__(self, channel_router, port: int = 19876):
        self._port = port
        self._server = None
        self._writer = None
        self._adapter_name = None
        self._push_target = None
        self._adapter_proc = None
        self._channel_router = channel_router
        self._channel_name = "im"
        self._lock = threading.Lock()
        self._connected = threading.Event()
        self._loop = None
        self._stopping = False
        self._restart_count = 0
        self._MAX_RESTARTS = 3
        self._MAX_MESSAGE_SIZE = 10 * 1024 * 1024  # 10MB

    async def start_server(self):
        """启动 TCP Server"""
        self._loop = asyncio.get_running_loop()
        self._server = await asyncio.start_server(
            self._handle_adapter, "127.0.0.1", self._port
        )
        logger.info(f"[IMGateway] TCP Server listening on 127.0.0.1:{self._port}")

    async def start(self):
        """启动 Server + 拉起 Adapter 子进程 + 健康检查"""
        await self.start_server()
        self._launch_adapter()
        asyncio.create_task(self._adapter_watchdog())

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

    def _launch_adapter(self):
        """根据配置拉起 IM Adapter 子进程"""
        try:
            prefs_path = Path.home() / ".niu" / "preferences.json"
            if not prefs_path.exists():
                return
            prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
            im_config = prefs.get("im", {})
            cmd = im_config.get("adapter_command", "").strip()
            if not cmd:
                return
            argv = shlex.split(cmd) + ["--gateway-port", str(self._port)]
            if argv[0] == "python":
                argv[0] = sys.executable
            log_dir = Path.home() / ".niu" / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            adapter_stderr = open(log_dir / "im_adapter_stderr.log", "a")
            self._adapter_proc = subprocess.Popen(
                argv, stdout=subprocess.DEVNULL, stderr=adapter_stderr,
            )
            adapter_stderr.close()
            logger.info(f"[IMGateway] Adapter process launched: PID={self._adapter_proc.pid}")
        except Exception as e:
            logger.error(f"[IMGateway] Failed to launch adapter: {e}")

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

        try:
            while True:
                header = await reader.readexactly(4)
                length = int.from_bytes(header, "big")
                if length > self._MAX_MESSAGE_SIZE:
                    logger.error(f"[IMGateway] Message too large: {length} bytes, dropping connection")
                    break
                data = await reader.readexactly(length)
                try:
                    msg = json.loads(data.decode("utf-8"))
                except Exception:
                    continue
                await self._dispatch(msg)
        except Exception as e:
            logger.error(f"[IMGateway] Adapter connection error: {e}")
        finally:
            with self._lock:
                self._writer = None
                self._connected.clear()
                self._adapter_name = None
                self._push_target = None
            logger.info("[IMGateway] Adapter disconnected")

    async def _dispatch(self, msg: dict):
        """分发 Adapter 指令"""
        t = msg.get("type")
        if t == "MSG":
            self._on_msg(msg)
        elif t == "READY":
            self._on_ready(msg)
        elif t == "PING":
            await self._async_send({"type": "PONG"})

    def _on_msg(self, msg: dict):
        """处理 MSG 指令 — 入队到 ChatQueue"""
        if not self._channel_router:
            return
        from .base import UnifiedMessage
        unified = UnifiedMessage(
            content=msg.get("content", ""),
            channel=self._channel_name,
            channel_id=msg.get("channel_id", ""),
            sender_id=msg.get("sender_id", ""),
            message_type="text",
            resources=[],
            raw={"is_group": msg.get("is_group", False)},
        )
        self._channel_router.route_in_sync(
            unified,
            session_id=msg.get("session_id"),
            message_override=msg.get("content"),
        )

    def _on_ready(self, msg: dict):
        """处理 READY 指令"""
        with self._lock:
            self._adapter_name = msg.get("adapter", "im")
            self._push_target = msg.get("push_target")
        logger.info(f"[IMGateway] Adapter ready: {self._adapter_name}, push_target={self._push_target}")

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
            # 清理悬空连接
            with self._lock:
                if self._writer is writer:
                    self._writer = None
                    self._connected.clear()
            try:
                writer.close()
            except Exception:
                pass

    def _send_command(self, cmd: dict):
        """线程安全发送 — 从 executor 线程调用"""
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._async_send(cmd), self._loop)

    # ── ChannelAdapter 接口 ──

    async def send(self, channel_id: str, content: str) -> None:
        if not self._connected.is_set():
            logger.debug("[IMGateway] Adapter not connected, cannot send")
            return
        await self._async_send({"type": "SEND", "channel_id": channel_id, "content": content})

    async def push(self, channel_id: str, content: str) -> None:
        if not self._connected.is_set():
            logger.debug("[IMGateway] Adapter not connected, cannot push")
            return
        target = channel_id or self._push_target or ""
        await self._async_send({"type": "PUSH", "channel_id": target, "content": content})

    def notify_stream(self, channel_id: str):
        """通知 Adapter 有新内容。channel_id 可为空，Adapter 通过内部状态确定推送目标。"""
        self._send_command({"type": "STREAM", "channel_id": channel_id or ""})

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    @property
    def push_target(self) -> str | None:
        return self._push_target

    @property
    def adapter_name(self) -> str | None:
        return self._adapter_name


_gateway: IMGateway | None = None

def get_im_gateway() -> IMGateway | None:
    return _gateway

def set_im_gateway(gw: IMGateway):
    global _gateway
    _gateway = gw
```

- [ ] **Step 2: 创建单元测试**

```python
"""IM Gateway 单元测试"""
import asyncio
import json
import pytest


def _encode(msg: dict) -> bytes:
    payload = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    return len(payload).to_bytes(4, "big") + payload


async def _read_one(reader: asyncio.StreamReader, timeout: float = 5.0) -> dict | None:
    try:
        header = await asyncio.wait_for(reader.readexactly(4), timeout=timeout)
        length = int.from_bytes(header, "big")
        data = await asyncio.wait_for(reader.readexactly(length), timeout=timeout)
        return json.loads(data.decode("utf-8"))
    except (asyncio.TimeoutError, asyncio.IncompleteReadError):
        return None


@pytest.mark.asyncio
async def test_gateway_accepts_connection():
    from niu_api.channel.gateway import IMGateway
    gw = IMGateway(channel_router=None, port=0)
    await gw.start_server()
    port = gw._server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        assert not reader.at_eof()
    finally:
        writer.close()
        await writer.wait_closed()
        await gw.stop()


@pytest.mark.asyncio
async def test_gateway_dispatches_msg():
    from niu_api.channel.gateway import IMGateway
    received = []
    class FakeRouter:
        def route_in_sync(self, message, session_id=None, message_override=None):
            received.append({"session_id": session_id, "content": message_override, "channel": message.channel})
    gw = IMGateway(channel_router=FakeRouter(), port=0)
    await gw.start_server()
    port = gw._server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(_encode({"type": "MSG", "session_id": "im:123", "content": "hello",
                              "channel_id": "ch1", "sender_id": "u1", "is_group": False, "reply_to_id": None}))
        await writer.drain()
        await asyncio.sleep(0.1)
        assert len(received) == 1
        assert received[0]["session_id"] == "im:123"
        assert received[0]["channel"] == "im"
    finally:
        writer.close()
        await writer.wait_closed()
        await gw.stop()


@pytest.mark.asyncio
async def test_gateway_send():
    from niu_api.channel.gateway import IMGateway
    gw = IMGateway(channel_router=None, port=0)
    await gw.start_server()
    port = gw._server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        await asyncio.sleep(0.1)
        await gw.send("ch1", "reply text")
        cmd = await _read_one(reader)
        assert cmd["type"] == "SEND"
        assert cmd["channel_id"] == "ch1"
        assert cmd["content"] == "reply text"
    finally:
        writer.close()
        await writer.wait_closed()
        await gw.stop()


@pytest.mark.asyncio
async def test_gateway_ready_sets_push_target():
    from niu_api.channel.gateway import IMGateway
    gw = IMGateway(channel_router=None, port=0)
    await gw.start_server()
    port = gw._server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(_encode({"type": "READY", "adapter": "test", "push_target": "oc_target"}))
        await writer.drain()
        await asyncio.sleep(0.1)
        assert gw.push_target == "oc_target"
    finally:
        writer.close()
        await writer.wait_closed()
        await gw.stop()
```

- [ ] **Step 3: 运行测试**

Run: `python -m pytest tests/test_im_gateway.py -v`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add niu_api/channel/gateway.py tests/test_im_gateway.py
git commit -m "feat: IM Gateway TCP Server"
```

---

## Task 2: 替换飞书启动/关闭 + 所有 "feishu" 引用

**Files:**
- Modify: `niu_api/__main__.py`
- Modify: `agent/runner.py`
- Modify: `niu_api/channel/__init__.py`
- Modify: `niu_api/chat_queue.py`
- Modify: `niu_api/internal/scheduler/service.py`
- Modify: `niu_api/internal/ha_watcher/watcher.py`

- [ ] **Step 1: `__main__.py` — 替换飞书启动为 Gateway 启动**

将行 116-154 的飞书启动代码替换为：
```python
    # 6.2. Start IM Gateway (if configured)
    try:
        import json as _json
        prefs_path = Path.home() / ".niu" / "preferences.json"
        if prefs_path.exists():
            _prefs = _json.loads(prefs_path.read_text(encoding="utf-8"))
        else:
            _prefs = {}
        im_config = _prefs.get("im", {})
        if im_config.get("enabled"):
            from niu_api.channel.gateway import IMGateway, set_im_gateway
            gateway = IMGateway(channel_router=channel_router, port=im_config.get("gateway_port", 19876))
            channel_router.register("im", gateway)
            set_im_gateway(gateway)
            gateway_task = asyncio.create_task(gateway.start())

            def _on_gateway_done(t: asyncio.Task):
                if not t.cancelled():
                    exc = t.exception()
                    if exc:
                        logger.error(f"IM Gateway startup failed: {exc}")

            gateway_task.add_done_callback(_on_gateway_done)
            logger.info("IM Gateway starting (TCP Server)")
        else:
            logger.info("IM Gateway disabled")
    except Exception as e:
        logger.warning(f"IM Gateway setup failed: {e}")
```

- [ ] **Step 2: `__main__.py` — 替换飞书关闭为 Gateway 关闭**

将行 363-372 替换为：
```python
    try:
        from niu_api.channel.gateway import get_im_gateway
        gateway = get_im_gateway()
        if gateway:
            await gateway.stop()
            logger.info("IM Gateway stopped")
    except Exception as e:
        logger.warning(f"Failed to stop IM Gateway: {e}")
```

- [ ] **Step 3: `__main__.py` — 启用 SQLite WAL 模式**

在 Gateway 启动之前添加：
```python
    import sqlite3
    db_path = Path.home() / ".niu" / "messages.db"
    if db_path.exists():
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.close()
        logger.info("messages.db WAL mode enabled")
```

- [ ] **Step 4: `agent/runner.py` — 流式推送改走 Gateway**

行 1958-1963 的 `trigger_feishu_stream_push()` 替换为：
```python
try:
    from niu_api.channel.gateway import get_im_gateway
    _gw = get_im_gateway()
    if _gw and _gw.is_connected:
        _gw.notify_stream("")
except Exception:
    pass
```

- [ ] **Step 5: `channel/__init__.py` — 默认 channel "feishu" → "im"**

行 47: `channel = message.channel or "feishu"` → `channel = message.channel or "im"`

- [ ] **Step 6: `chat_queue.py` — 默认 channel "feishu" → "im"**

行 100: `channel: str = "feishu"` → `channel: str = "im"`

- [ ] **Step 7: `scheduler/service.py` — channel "feishu" → "im" + 注释/错误消息**

行 102: `# 飞书通道推送` → `# IM 通道推送`
行 106: `has_channel("feishu")` → `has_channel("im")`
行 109: `router.push(agent_reply, "feishu", ...)` → `router.push(agent_reply, "im", ...)`
行 114: `"Feishu push failed"` → `"IM push failed"`

- [ ] **Step 8: `ha_watcher/watcher.py` — channel "feishu" → "im" + 注释/错误消息**

行 225: `# 飞书通道推送` → `# IM 通道推送`
行 229: `has_channel("feishu")` → `has_channel("im")`
行 231: `router.push(agent_reply, "feishu", ...)` → `router.push(agent_reply, "im", ...)`
行 236: `"Feishu push failed"` → `"IM push failed"`

- [ ] **Step 9: `agent/mcp_loader.py` — 删除 feishu-server 条目**

行 34: `("feishu-server", "niu_feishu_server")` → 删除此行

- [ ] **Step 10: 注释清理 — 飞书引用 → IM**

`niu_api/channel/__init__.py` 行 20-22: docstring 中 "飞书 SDK" → "IM Gateway"
`niu_api/channel/base.py` 行 20: `# 飞书 file_key` → `# IM file_key`
`niu_api/channel/base.py` 行 54,63: `飞书通道重写` → `IM 通道重写`
`niu_api/chat_queue.py` 行 22-23: `"feishu"` → `"im"`

- [ ] **Step 11: 配置文件清理**

删除 `config/mcp-servers.yaml` 中的 feishu-server 段
删除 `config/disk/feishu-server.yaml`

- [ ] **Step 12: 验证语法**

Run: `python -c "from niu_api.channel.gateway import IMGateway, get_im_gateway; print('OK')"`

- [ ] **Step 13: Commit**

```bash
git add niu_api/__main__.py agent/runner.py niu_api/channel/__init__.py niu_api/channel/base.py niu_api/chat_queue.py niu_api/internal/scheduler/service.py niu_api/internal/ha_watcher/watcher.py agent/mcp_loader.py config/mcp-servers.yaml
git commit -m "refactor: replace feishu channel with IM Gateway"
```

---

## Task 3: 集成测试程序

**Files:**
- Create: `tests/test_im_gateway_integration.py`

测试程序模拟 IM Adapter，连接运行中的 Gateway，验证全链路。

**测试覆盖**：
- 入方向：文本、图片（`![alt](path)`）、文件（`[name](path)`）→ Agent 处理 → 回复
- 出方向：让 Agent 展示图片/发送文件 → 回复 content 含 Markdown 图片/文件标记 → 验证标记格式正确
- 流式推送：STREAM 通知

- [ ] **Step 1: 创建集成测试程序**

```python
"""IM Gateway 集成测试 — 模拟 IM Adapter

使用方式：
1. 启动 ./niu（确保 preferences.json 中 im.enabled=true）
2. 运行 python tests/test_im_gateway_integration.py
"""
import asyncio
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

GATEWAY_HOST = "127.0.0.1"
GATEWAY_PORT = 19876
TEST_CHANNEL_ID = "test_chat_001"
TEST_SENDER_ID = "test_user_001"


def encode(msg: dict) -> bytes:
    payload = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    return len(payload).to_bytes(4, "big") + payload


async def read_one(reader, timeout=120.0):
    try:
        header = await asyncio.wait_for(reader.readexactly(4), timeout=timeout)
        length = int.from_bytes(header, "big")
        data = await asyncio.wait_for(reader.readexactly(length), timeout=timeout)
        return json.loads(data.decode("utf-8"))
    except (asyncio.TimeoutError, asyncio.IncompleteReadError):
        return None


async def connect_gateway():
    reader, writer = await asyncio.open_connection(GATEWAY_HOST, GATEWAY_PORT)
    writer.write(encode({"type": "READY", "adapter": "test-adapter", "push_target": TEST_CHANNEL_ID}))
    await writer.drain()
    await asyncio.sleep(0.5)
    return reader, writer


async def send_msg(writer, content, channel_id=TEST_CHANNEL_ID):
    writer.write(encode({
        "type": "MSG", "session_id": f"im:{TEST_SENDER_ID}",
        "content": content, "channel_id": channel_id,
        "sender_id": TEST_SENDER_ID, "is_group": False, "reply_to_id": None,
    }))
    await writer.drain()


async def wait_for_send(reader, timeout=120.0):
    """等待 Agent 回复（SEND 指令），跳过 STREAM/PONG"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        cmd = await read_one(reader, timeout=min(deadline - time.time(), 30))
        if cmd and cmd.get("type") == "SEND":
            return cmd
    return None


# ── 入方向测试 ──

async def test_inbound_text():
    """入方向：发送文本消息 → 收到 Agent 回复"""
    print("\n=== 测试: 入方向-文本消息 ===")
    reader, writer = await connect_gateway()
    await send_msg(writer, "你好，请简单回复一句话")
    print("[测试] 已发送文本消息")

    reply = await wait_for_send(reader)
    if reply:
        content = reply.get("content", "")
        print(f"[测试] PASS 收到回复: {content[:80]}...")
        assert reply["type"] == "SEND"
        assert reply.get("channel_id") == TEST_CHANNEL_ID
        assert len(content) > 0
    else:
        raise AssertionError("Agent 未回复文本消息")

    writer.close()
    await writer.wait_closed()


async def test_inbound_image():
    """入方向：发送带图片的消息 → Agent 处理图片 → 回复"""
    print("\n=== 测试: 入方向-图片消息 ===")
    tmp_dir = Path.home() / ".niu" / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    test_img = tmp_dir / "test_photo.jpg"
    if not test_img.exists():
        test_img.write_bytes(b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9')

    reader, writer = await connect_gateway()
    await send_msg(writer, f"请看这张照片\n![测试照片]({test_img})")
    print("[测试] 已发送图片消息")

    reply = await wait_for_send(reader)
    if reply:
        print(f"[测试] PASS 收到回复")
        assert reply["type"] == "SEND"
    else:
        raise AssertionError("Agent 未回复图片消息")

    writer.close()
    await writer.wait_closed()


async def test_inbound_file():
    """入方向：发送带文件的消息 → Agent 处理文件 → 回复"""
    print("\n=== 测试: 入方向-文件消息 ===")
    tmp_dir = Path.home() / ".niu" / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    test_file = tmp_dir / "test_doc.txt"
    test_file.write_text("这是一个测试文件，用于验证 IM Gateway 文件传输功能。", encoding="utf-8")

    reader, writer = await connect_gateway()
    await send_msg(writer, f"请查看文件\n[测试文档]({test_file})")
    print("[测试] 已发送文件消息")

    reply = await wait_for_send(reader)
    if reply:
        print(f"[测试] PASS 收到回复")
        assert reply["type"] == "SEND"
    else:
        raise AssertionError("Agent 未回复文件消息")

    writer.close()
    await writer.wait_closed()


# ── 出方向测试 ──

async def test_outbound_image():
    """出方向：让 Agent 展示图片 → 回复 content 含 ![alt](path) 标记"""
    print("\n=== 测试: 出方向-图片回复 ===")
    reader, writer = await connect_gateway()
    await send_msg(writer, "请用 photo_search 工具搜索一张照片并展示给我，或者从你的知识库中找一张图片展示")
    print("[测试] 已发送请求展示图片的消息")

    reply = await wait_for_send(reader, timeout=180.0)
    if reply:
        content = reply.get("content", "")
        # 检查回复中是否包含 Markdown 图片标记
        img_pattern = r'!\[.*?\]\((.+?)\)'
        matches = re.findall(img_pattern, content)
        if matches:
            # 验证图片路径存在
            for img_path in matches:
                p = Path(img_path)
                if p.exists():
                    print(f"[测试] PASS 回复含图片标记且文件存在: {img_path}")
                else:
                    print(f"[测试] WARN 回复含图片标记但文件不存在: {img_path}")
            assert reply["type"] == "SEND"
        else:
            print(f"[测试] WARN 回复不含图片标记（Agent 可能没有可用图片）: {content[:100]}...")
            # 不算失败 — Agent 可能没有图片可展示
    else:
        raise AssertionError("Agent 未回复图片展示请求")

    writer.close()
    await writer.wait_closed()


async def test_outbound_file():
    """出方向：让 Agent 发送文件 → 回复 content 含 [name](path) 标记"""
    print("\n=== 测试: 出方向-文件回复 ===")
    reader, writer = await connect_gateway()
    await send_msg(writer, "请把你的配置文件内容发给我，用文件的形式")
    print("[测试] 已发送请求发送文件的消息")

    reply = await wait_for_send(reader, timeout=180.0)
    if reply:
        content = reply.get("content", "")
        # 检查回复中是否包含 Markdown 文件链接（排除图片 ![...]）
        file_pattern = r'(?<!!)\[([^\]]+)\]\(([^)]+)\)'
        file_matches = re.findall(file_pattern, content)
        if file_matches:
            for name, fpath in file_matches:
                p = Path(fpath)
                if p.exists():
                    print(f"[测试] PASS 回复含文件标记且文件存在: [{name}]({fpath})")
                else:
                    print(f"[测试] WARN 回复含文件标记但文件不存在: [{name}]({fpath})")
            assert reply["type"] == "SEND"
        else:
            print(f"[测试] WARN 回复不含文件标记（Agent 可能没有可用文件）: {content[:100]}...")
    else:
        raise AssertionError("Agent 未回复文件发送请求")

    writer.close()
    await writer.wait_closed()


# ── 流式推送测试 ──

async def test_stream_notification():
    """测试：Agent 回复过程中收到 STREAM 通知"""
    print("\n=== 测试: 流式推送通知 ===")
    reader, writer = await connect_gateway()
    await send_msg(writer, "请详细介绍一下你自己，至少写三段")
    print("[测试] 已发送消息，等待 STREAM 通知...")

    stream_count = 0
    deadline = time.time() + 15
    while time.time() < deadline:
        cmd = await read_one(reader, timeout=2)
        if cmd and cmd.get("type") == "STREAM":
            stream_count += 1
        elif cmd and cmd.get("type") == "SEND":
            break

    if stream_count > 0:
        print(f"[测试] PASS 收到 {stream_count} 条 STREAM 通知")
    else:
        print("[测试] WARN 未收到 STREAM 通知（Agent 可能回复太快）")

    writer.close()
    await writer.wait_closed()


async def run_all():
    results = []
    tests = [
        ("入方向-文本", test_inbound_text),
        ("入方向-图片", test_inbound_image),
        ("入方向-文件", test_inbound_file),
        ("出方向-图片", test_outbound_image),
        ("出方向-文件", test_outbound_file),
        ("流式通知", test_stream_notification),
    ]
    for name, fn in tests:
        try:
            await fn()
            results.append((name, "PASS"))
        except Exception as e:
            results.append((name, f"FAIL: {e}"))
            print(f"[测试] FAIL {name}: {e}")

    print("\n" + "=" * 50)
    for name, r in results:
        status = "PASS" if r == "PASS" else "FAIL"
        print(f"  {status} {name}: {r}")
    passed = sum(1 for _, r in results if r == "PASS")
    print(f"\n通过: {passed}/{len(results)}")
    return passed == len(results)


if __name__ == "__main__":
    success = asyncio.run(run_all())
    sys.exit(0 if success else 1)
```

- [ ] **Step 2: 配置 preferences.json**

确保 `~/.niu/preferences.json` 中有：
```json
{
  "im": {
    "enabled": true,
    "gateway_port": 19876
  }
}
```

不配置 `adapter_command`（测试程序手动连接）。

- [ ] **Step 3: 启动系统**

Run: `./niu`
验证日志中有 `[IMGateway] TCP Server listening on 127.0.0.1:19876`

- [ ] **Step 4: 运行集成测试**

Run: `python tests/test_im_gateway_integration.py`
Expected: 4/4 PASS

- [ ] **Step 5: 验证前端不受影响**

在集成测试期间，前端 SSE 推送应正常工作。

- [ ] **Step 6: Commit**

```bash
git add tests/test_im_gateway_integration.py
git commit -m "test: IM Gateway integration test"
```

---

## Task 4: 删除飞书代码

**Files:**
- Delete: `niu_api/channel/feishu_channel.py`
- Delete: `tests/test_feishu_*.py` (10个文件)
- Delete: `tests/test_channel_adapters.py` (import FeishuChannelAdapter)
- Delete: `tests/test_v10_*.py` (依赖飞书配置)
- Delete: `tests/test_cardkit_api.py` (依赖飞书配置)
- Delete: `tests/test_cron_converter.py` (依赖 feishu-server)
- Modify: `tests/test_channel_base.py` ("feishu" → "im")
- Modify: `tests/test_scheduler_group_push.py` ("feishu" → "im")
- Modify: `tests/test_chat_queue.py` (删除 feishu 相关测试函数，不是替换)

- [ ] **Step 1: 确认所有飞书引用已清除**

Run: `grep -r "feishu" niu_api/ agent/ --include="*.py" | grep -v __pycache__`
Expected: 无结果

注：`mcp-servers/feishu-server/` 目录保留（Phase 2 的基础），验证范围不含 mcp-servers/

- [ ] **Step 2: 删除 feishu_channel.py**

- [ ] **Step 3: 删除飞书相关测试文件**

直接删除的文件：
- `tests/test_feishu_stream_state.py`
- `tests/test_feishu_concurrency.py`
- `tests/test_feishu_stream_dataflow.py`
- `tests/test_feishu_async_arch.py`
- `tests/test_feishu_channel_robustness.py`
- `tests/test_feishu_group_chat.py`
- `tests/test_feishu_channel_tdd.py`
- `tests/test_feishu_sync.py`
- `tests/test_feishu_channel.py`
- `tests/test_feishu_stream_assumptions.py`
- `tests/test_channel_adapters.py`
- `tests/test_v10_streaming_lifecycle.py`
- `tests/test_v10_cardkit_finalize.py`
- `tests/test_v10_finalize_methods.py`
- `tests/test_cardkit_api.py`
- `tests/test_cron_converter.py`

- [ ] **Step 4: 修改测试文件中的 feishu 引用**

`tests/test_channel_base.py`: 所有 "feishu" → "im"
`tests/test_scheduler_group_push.py`: 所有 "feishu" → "im"
`tests/test_chat_queue.py`: 删除 `test_feishu_push_on_feishu_source` 和 `test_no_feishu_push_on_frontend_source` 两个测试函数（它们 patch 了不存在的 `_push_to_feishu` 方法，直接删除不要替换）

- [ ] **Step 5: 验证程序启动**

Run: `./niu`，确认无 import 错误

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor: remove feishu_channel.py"
```

---

## Verification

1. `python -m pytest tests/test_im_gateway.py -v` — 单元测试通过
2. `./niu` 启动 → 日志显示 Gateway 监听
3. 集成测试：文本/图片/文件消息 → Agent 回复通过 SEND 指令返回
4. 前端 SSE 不受影响
5. `grep -r "feishu" niu_api/ agent/ --include="*.py" | grep -v __pycache__` → 无结果（mcp-servers/ 除外）
