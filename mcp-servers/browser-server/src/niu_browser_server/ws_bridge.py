"""
WebSocket Bridge: Python backend <-> Chrome Extension

Runs a WebSocket server in a dedicated thread. Receives MCP tool call requests,
forwards them to the Extension via WS, waits for results.
"""

import asyncio
import json
import queue
import threading
import time
import uuid
from typing import Any, Optional

from loguru import logger

WS_PORT = 19876  # Must match Extension hub.js


class WSBridge:
    """
    WebSocket server for communication with Chrome Extension.

    Architecture:
    - Dedicated thread runs asyncio event loop
    - MCP tool call -> send_command() -> WS -> Extension
    - Extension -> WS -> on_message() -> return result to waiting caller
    """

    _instance: Optional['WSBridge'] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws_server = None
        self._hub_ws = None
        self._pending: dict[str, queue.Queue] = {}
        self._connected = False
        self._started = False

        self._initialized = True
        logger.info(f"WSBridge initialized (port: {WS_PORT})")

    def start(self):
        """Start the WebSocket server thread."""
        if self._started:
            return

        try:
            import websockets
        except ImportError:
            logger.warning("websockets package not installed, WS bridge unavailable. Run: pip install websockets")
            return

        self._started = True
        self._thread = threading.Thread(
            target=self._run_server,
            daemon=True,
            name="WSBridge-Server"
        )
        self._thread.start()
        logger.info("WSBridge server thread started")

    def _run_server(self):
        """Run WebSocket server in dedicated thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        async def start():
            import websockets
            self._ws_server = await websockets.serve(
                self._handle_connection,
                "localhost",
                WS_PORT,
            )
            logger.info(f"WSBridge server listening on ws://localhost:{WS_PORT}")

        self._loop.run_until_complete(start())
        self._loop.run_forever()

    async def _handle_connection(self, ws, path=None):
        """Handle Extension hub WebSocket connection."""
        logger.info("Extension hub connected")
        self._hub_ws = ws
        self._connected = True

        try:
            async for message in ws:
                msg = json.loads(message)
                await self._on_message(msg)
        except Exception as e:
            logger.debug(f"Extension hub connection ended: {e}")
        finally:
            self._hub_ws = None
            self._connected = False
            logger.info("Extension hub disconnected")

    async def _on_message(self, msg: dict):
        """Handle message from Extension."""
        msg_type = msg.get("type")

        if msg_type == "ready":
            logger.info("Extension hub ready")
            return

        if msg_type in ("result", "error"):
            cmd_id = msg.get("id")
            if cmd_id and cmd_id in self._pending:
                self._pending[cmd_id].put(msg)

        elif msg_type == "tab_updated":
            logger.debug(f"Tab updated: {msg.get('url')}")

        elif msg_type == "tab_created":
            logger.debug(f"Tab created: {msg.get('url')}")

    def send_command(self, action: str, **kwargs) -> dict:
        """
        Send command to Extension, wait for result (synchronous).

        Args:
            action: Command type (get_state, click, input_text, select_option, scroll, navigate)
            **kwargs: Command parameters

        Returns:
            Result dict from Extension
        """
        if not self._connected or not self._hub_ws:
            return {"success": False, "message": "Extension not connected. Is the browser running with the extension installed?"}

        cmd_id = str(uuid.uuid4())
        result_queue = queue.Queue()
        self._pending[cmd_id] = result_queue

        command = {
            "type": action,
            "id": cmd_id,
            **kwargs,
        }

        future = asyncio.run_coroutine_threadsafe(
            self._hub_ws.send(json.dumps(command)),
            self._loop,
        )
        try:
            future.result(timeout=5)
        except Exception as e:
            del self._pending[cmd_id]
            return {"success": False, "message": f"Failed to send command: {e}"}

        try:
            result = result_queue.get(timeout=30)
        except queue.Empty:
            del self._pending[cmd_id]
            return {"success": False, "message": f"Command {action} timed out (30s)"}

        del self._pending[cmd_id]
        return result

    @property
    def connected(self) -> bool:
        """Whether Extension is connected."""
        return self._connected
