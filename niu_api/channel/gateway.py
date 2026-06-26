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
        with self._lock:
            target = channel_id or self._push_target or ""
            connected = self._connected.is_set()
        if not connected:
            logger.debug("[IMGateway] Adapter not connected, cannot push")
            return
        await self._async_send({"type": "PUSH", "channel_id": target, "content": content})

    def notify_stream(self, channel_id: str):
        """通知 Adapter 有新内容。channel_id 可为空，Adapter 通过内部状态确定推送目标。"""
        self._send_command({"type": "STREAM", "channel_id": channel_id or ""})

    async def send_media(self, channel_id: str, msg) -> None:
        """IM 通道统一 Markdown 透传，不拆分媒体。如果 route_out 调用了 send_media，
        说明 resolve_outbound_content 返回了非 text kind — 记录但不发送。"""
        logger.debug(f"[IMGateway] send_media called with kind={getattr(msg, 'kind', '?')}, IM channel uses Markdown passthrough")

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
