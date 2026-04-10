# src/niu_page_agent/ws_client.py
"""WebSocket 客户端，与 Chrome 扩展通信"""
import json
import time
import websocket
from typing import Optional
from .protocol import BrowserCommand, BrowserResponse


class BrowserWSClient:
    """WebSocket 客户端，连接到 Chrome 扩展"""

    def __init__(self, port: int = 38401):
        self.port = port
        self.url = f"ws://localhost:{port}"
        self.ws: Optional[websocket.WebSocket] = None

    def connect(self, timeout: int = 30) -> bool:
        """连接到 WebSocket 服务器"""
        start_time = time.time()

        while time.time() - start_time < timeout:
            try:
                self.ws = websocket.create_connection(self.url)
                return True
            except Exception:
                time.sleep(0.5)

        raise ConnectionError(f"Failed to connect to {self.url} within {timeout}s")

    def send_command(self, command: BrowserCommand, timeout: int = 120) -> BrowserResponse:
        """发送命令并等待响应"""
        if not self.ws:
            raise ConnectionError("WebSocket not connected")

        # 发送命令
        self.ws.send(json.dumps(command.to_dict()))

        # 等待响应
        self.ws.settimeout(timeout)
        response_data = self.ws.recv()

        # 解析响应
        response_dict = json.loads(response_data)
        return BrowserResponse.from_dict(response_dict)

    def close(self):
        """关闭连接"""
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
            finally:
                self.ws = None
