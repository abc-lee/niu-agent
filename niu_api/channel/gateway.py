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


def _get_gateway_log_dir() -> Path:
    """返回 ~/.niu/logs/。"""
    import os
    home = os.path.expanduser("~")
    return Path(home) / ".niu" / "logs"


def _log_gateway_error(msg: str) -> None:
    """记录 gateway 致命错误到 logs/gateway_error.log，不受 logging flag 控制。

    飞书 adapter 启动失败是关键诊断（app_id 配错、端口占用、credentials 缺失），
    即使 logging.enabled=false 也必须写，确保用户能诊断。
    """
    try:
        log_dir = _get_gateway_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "gateway_error.log"
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] ERROR: {msg}\n")
    except Exception:
        pass  # 日志写入失败不影响主流程


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
                _log_gateway_error(f"Adapter not found: {adapter_workdir}")
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
                _log_gateway_error(f"{adapter_type} credentials missing, skipping")
                return
            env[f"NIU_{adapter_type.upper()}_APP_ID"] = app_id
            env[f"NIU_{adapter_type.upper()}_APP_SECRET"] = app_secret
            for key in ("user_p2p_chat_id", "user_open_id"):
                val = adapter_config.get(key, "")
                if val:
                    env[f"NIU_{adapter_type.upper()}_{key.upper()}"] = val

            argv = [sys.executable, "-m", adapter_module]
            log_dir = _get_gateway_log_dir()
            log_dir.mkdir(parents=True, exist_ok=True)
            from niu_api.config import get_logging_config
            if get_logging_config().enabled:
                adapter_stderr = open(log_dir / "im_adapter_stderr.log", "a")
            else:
                adapter_stderr = subprocess.DEVNULL  # logging 关闭时不写文件
            self._adapter_proc = subprocess.Popen(
                argv, stdout=subprocess.DEVNULL, stderr=adapter_stderr, env=env,
            )
            if adapter_stderr is not subprocess.DEVNULL:
                adapter_stderr.close()
            logger.info(f"[IMGateway] Adapter launched: {adapter_type}, PID={self._adapter_proc.pid}")
        except Exception as e:
            logger.error(f"[IMGateway] Launch failed: {e}")
            _log_gateway_error(f"Launch failed: {e}")

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
                    # 只清除当前 writer，避免杀死新适配器的连接
                    if self._writer is writer:
                        self._writer = None
                        self._connected.clear()
                        self._adapter_name = None
                        self._push_target = None
                        self._reply_to_ids.clear()
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
            reply_to_id = self._reply_to_ids.get(channel_id, "")
        content = self._rewrite_unsupported_images(content)
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
        content = self._rewrite_unsupported_images(content)
        await self._async_send({"type": "PUSH", "channel_id": target, "content": content})

    def notify_stream(self, content: str, channel_id: str = "", is_final: bool = False):
        """通知 Adapter 有新增量内容"""
        with self._lock:
            reply_to_id = self._reply_to_ids.get(channel_id, "")
        content = self._rewrite_unsupported_images(content)
        self._send_command({
            "type": "STREAM",
            "channel_id": channel_id,
            "content": content,
            "is_final": is_final,
            "reply_to_id": reply_to_id,
        })

    async def send_media(self, channel_id: str, msg) -> None:
        """IM 通道统一 Markdown 透传，不拆分媒体。如果 route_out 调用了 send_media，
        说明 resolve_outbound_content 返回了非 text kind — 记录但不发送。"""
        logger.debug(f"[IMGateway] send_media called with kind={getattr(msg, 'kind', '?')}, IM channel uses Markdown passthrough")


    # 飞书等 IM 图片 API 支持的格式
    _SUPPORTED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

    @classmethod
    def _rewrite_unsupported_images(cls, content: str) -> str:
        """把不支持图片格式的 ![alt](path) 改写为 [文件名](path)。

        IM adapter 用 ![] 语法判断图片上传、[] 语法判断文件上传。
        SVG 等不支持格式如果走图片路径，飞书 API 会拒绝。
        改写为文件链接语法后，adapter 自然走文件上传。

        使用括号平衡解析（与飞书 adapter 的 extract_md_refs 一致），
        正确处理路径中含括号的情况。仅改写本地文件路径，不影响 URL 和 data URI。
        """
        import os
        result = []
        last_end = 0
        i = 0
        while i < len(content):
            # 检测 ![
            if content[i] == '!' and i + 1 < len(content) and content[i + 1] == '[':
                start = i
                i += 2
            else:
                i += 1
                continue

            # 找 alt_text（到第一个 ]）
            alt_start = i
            while i < len(content) and content[i] != ']':
                i += 1
            if i >= len(content):
                continue
            alt_text = content[alt_start:i]
            i += 1  # 跳过 ]

            # 必须紧跟 (
            if i >= len(content) or content[i] != '(':
                continue
            i += 1  # 跳过 (

            # 括号平衡找 path
            path_start = i
            depth = 1
            while i < len(content) and depth > 0:
                if content[i] == '(':
                    depth += 1
                elif content[i] == ')':
                    depth -= 1
                i += 1
            if depth != 0:
                continue
            path = content[path_start:i - 1]

            # URL / data URI 不改写
            if path.startswith(("http://", "https://", "ftp://", "data:", "mailto:")):
                continue

            # 检查扩展名
            ext = os.path.splitext(path)[1].lower()
            if ext in cls._SUPPORTED_IMAGE_EXTS:
                continue  # 支持的格式，不改写

            # 不支持的格式，改写为文件链接
            filename = os.path.basename(path) or alt_text or "文件"
            # 追加改写前的内容
            result.append(content[last_end:start])
            result.append(f"[{filename}]({path})")
            last_end = i

        result.append(content[last_end:])
        return "".join(result)

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
