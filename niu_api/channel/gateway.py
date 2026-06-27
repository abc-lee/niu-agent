"""IM 网关 — TCP Server，与 IM Adapter 通讯"""
import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from loguru import logger

from .base import ChannelAdapter


class IMGateway(ChannelAdapter):
    """IM 网关 — TCP Server。

    Gateway 不知道任何具体 IM 的存在。
    Adapter 通过 TCP 连接 Gateway，发送 MSG/READY/PING 指令。
    Gateway 通过 TCP 发送 SEND/PUSH/STREAM 指令给 Adapter。
    """

    def __init__(self, channel_router, port: int = 19877):
        self._port = port
        self._server = None
        self._writer = None
        self._adapter_name = None
        self._push_target = None
        self._adapter_proc = None
        self._channel_router = channel_router
        self._channel_name = "im"
        self._lock = threading.Lock()
        self._write_lock = asyncio.Lock()
        self._connected = threading.Event()
        self._loop = None
        self._stopping = False
        self._restart_count = 0
        self._MAX_RESTARTS = 3
        self._MAX_MESSAGE_SIZE = 10 * 1024 * 1024  # 10MB
        self._connected_since = 0.0
        self._send_buffer: deque = deque(maxlen=10)
        self._reply_to_ids: dict[str, str] = {}   # channel_id → reply_to_id 映射（群聊回复目标）
        self._finalized_channels: set[str] = set() # 已通过 STREAM(is_final) 终结的 channel_id

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
            # 稳定连接超过 60 秒 → 重置重启计数器
            if self._connected.is_set() and self._restart_count > 0:
                if time.monotonic() - self._connected_since > 60:
                    logger.info("[IMGateway] Adapter stable for 60s, resetting restart count")
                    self._restart_count = 0
            if self._adapter_proc is not None:
                retcode = self._adapter_proc.poll()
                if retcode is not None:
                    if retcode == 2:
                        logger.error("[IMGateway] Adapter permanent error (code=2), not restarting")
                        self._adapter_proc = None
                        break
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

    def _launch_adapter(self):
        """根据配置拉起 IM Adapter 子进程"""
        try:
            prefs_path = Path.home() / ".niu" / "preferences.json"
            if not prefs_path.exists():
                return
            prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
            im_config = prefs.get("im", {})
            adapter_type = im_config.get("adapter", "").strip()
            if not adapter_type:
                logger.info("[IMGateway] No adapter configured, skipping launch")
                return

            adapter_module = f"niu_{adapter_type}_adapter"
            adapter_workdir = Path(__file__).resolve().parent.parent.parent / "im-adapters" / adapter_type / "src"
            if not adapter_workdir.exists():
                logger.error(f"[IMGateway] Adapter not found: {adapter_workdir}")
                return

            env = dict(os.environ)
            env["NIU_IM_ADAPTER"] = adapter_type
            env["NIU_GATEWAY_PORT"] = str(self._port)
            python_path = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = f"{adapter_workdir}:{python_path}" if python_path else str(adapter_workdir)

            adapter_config = prefs.get(adapter_type, {})
            app_id = adapter_config.get("app_id", "")
            app_secret = adapter_config.get("app_secret", "")
            if not app_id or not app_secret:
                logger.error(f"[IMGateway] {adapter_type} credentials missing, skipping")
                return
            env[f"NIU_{adapter_type.upper()}_APP_ID"] = app_id
            env[f"NIU_{adapter_type.upper()}_APP_SECRET"] = app_secret
            for key in ("user_p2p_chat_id", "user_open_id"):
                val = adapter_config.get(key, "")
                if val:
                    env[f"NIU_{adapter_type.upper()}_{key.upper()}"] = val

            argv = [sys.executable, "-m", adapter_module]
            log_dir = Path(__file__).resolve().parent.parent.parent / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            adapter_stderr = open(log_dir / "im_adapter_stderr.log", "a")
            self._adapter_proc = subprocess.Popen(
                argv, stdout=subprocess.DEVNULL, stderr=adapter_stderr, env=env,
            )
            adapter_stderr.close()
            logger.info(f"[IMGateway] Adapter launched: {adapter_type}, PID={self._adapter_proc.pid}")
        except Exception as e:
            logger.error(f"[IMGateway] Launch failed: {e}")

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
            async with self._write_lock:
                with self._lock:
                    self._writer = None
                    self._connected.clear()
                    self._adapter_name = None
                    self._push_target = None
                    self._reply_to_ids.clear()
                    self._finalized_channels.clear()
                try:
                    writer.close()
                    await asyncio.wait_for(writer.wait_closed(), timeout=5.0)
                except Exception:
                    pass
            logger.info("[IMGateway] Adapter disconnected")

    async def _dispatch(self, msg: dict):
        """分发 Adapter 指令"""
        t = msg.get("type")
        if t == "MSG":
            self._on_msg(msg)
        elif t == "READY":
            await self._on_ready(msg)
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
        reply_to_id = msg.get("reply_to_id")
        if reply_to_id:
            with self._lock:
                self._reply_to_ids[msg.get("channel_id", "")] = reply_to_id
        self._channel_router.route_in_sync(
            unified,
            session_id=msg.get("session_id"),
            message_override=msg.get("content"),
        )

    async def _on_ready(self, msg: dict):
        """处理 READY — 记录信息 + 重放缓冲"""
        with self._lock:
            self._adapter_name = msg.get("adapter", "im")
            self._push_target = msg.get("push_target")
            logger.info(f"[IMGateway] Adapter ready: {self._adapter_name}, push_target={self._push_target}")
        if self._send_buffer:
            logger.info(f"[IMGateway] Replaying {len(self._send_buffer)} buffered messages")
            for cmd in list(self._send_buffer):
                try:
                    await self._async_send(cmd, _skip_buffer=True)
                except Exception as e:
                    logger.error(f"[IMGateway] Replay failed: {e}")
            self._send_buffer.clear()

    async def _async_send(self, cmd: dict, _skip_buffer=False):
        """发送指令给 Adapter（async，带 drain）

        _lock 保护 writer 引用的读取（跨线程安全）。
        _write_lock 序列化 write+drain（协程级安全，防止两个协程交错写入破坏协议）。
        _skip_buffer=True 时跳过缓冲（重放时避免循环）。
        """
        with self._lock:
            writer = self._writer
        if writer is None:
            return
        if not _skip_buffer and cmd.get("type") in ("SEND", "PUSH"):
            cid = cmd.get("channel_id", "__global__")
            existing = next((i for i, c in enumerate(self._send_buffer)
                             if c.get("channel_id") == cid and c.get("type") == cmd.get("type")), None)
            if existing is not None:
                self._send_buffer[existing] = cmd
            else:
                self._send_buffer.append(cmd)
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

    # ── ChannelAdapter 接口 ──

    async def send(self, channel_id: str, content: str) -> None:
        if not self._connected.is_set():
            logger.debug("[IMGateway] Adapter not connected, cannot send")
            return
        with self._lock:
            if channel_id in self._finalized_channels:
                self._finalized_channels.discard(channel_id)
                self._reply_to_ids.pop(channel_id, None)
                logger.debug(f"[IMGateway] Channel {channel_id} already finalized via STREAM, skipping SEND")
                return
            reply_to_id = self._reply_to_ids.get(channel_id, "")
        await self._async_send({"type": "SEND", "channel_id": channel_id, "content": content, "reply_to_id": reply_to_id})
        with self._lock:
            self._reply_to_ids.pop(channel_id, None)

    async def push(self, channel_id: str, content: str) -> None:
        with self._lock:
            target = channel_id or self._push_target or ""
            connected = self._connected.is_set()
        if not connected:
            logger.debug("[IMGateway] Adapter not connected, cannot push")
            return
        await self._async_send({"type": "PUSH", "channel_id": target, "content": content})

    def notify_stream(self, content: str, channel_id: str = "", is_final: bool = False):
        """通知 Adapter 有新增量内容"""
        with self._lock:
            reply_to_id = self._reply_to_ids.get(channel_id, "")
        self._send_command({
            "type": "STREAM",
            "channel_id": channel_id,
            "content": content,
            "is_final": is_final,
            "reply_to_id": reply_to_id,
        })
        if is_final:
            with self._lock:
                self._finalized_channels.add(channel_id)

    async def send_media(self, channel_id: str, msg) -> None:
        """IM 通道统一 Markdown 透传，不拆分媒体。如果 route_out 调用了 send_media，
        说明 resolve_outbound_content 返回了非 text kind — 记录但不发送。"""
        logger.debug(f"[IMGateway] send_media called with kind={getattr(msg, 'kind', '?')}, IM channel uses Markdown passthrough")

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    @property
    def push_target(self) -> str | None:
        with self._lock:
            return self._push_target

    @property
    def adapter_name(self) -> str | None:
        with self._lock:
            return self._adapter_name


_gateway: IMGateway | None = None

def get_im_gateway() -> IMGateway | None:
    return _gateway

def set_im_gateway(gw: IMGateway):
    global _gateway
    _gateway = gw
