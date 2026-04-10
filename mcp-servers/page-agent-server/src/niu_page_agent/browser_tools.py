"""浏览器工具函数实现"""
from typing import Any, Dict, Optional
from .ws_client import BrowserWSClient
from .protocol import (
    NavigateCommand,
    ClickCommand,
    InputCommand,
    ScreenshotCommand,
    ExecuteTaskCommand,
)


# 全局 WebSocket 客户端实例
_ws_client: Optional[BrowserWSClient] = None


def get_ws_client() -> BrowserWSClient:
    """获取或创建 WebSocket 客户端"""
    global _ws_client

    if _ws_client is None:
        _ws_client = BrowserWSClient(port=38401)
        _ws_client.connect()

    return _ws_client


def browser_navigate(url: str) -> str:
    """导航到指定 URL"""
    client = get_ws_client()
    response = client.send_command(NavigateCommand(url=url))

    if response.success:
        return response.data or f"已导航到 {url}"
    else:
        return f"错误：{response.error}"


def browser_click(selector: str) -> str:
    """点击指定元素"""
    client = get_ws_client()
    response = client.send_command(ClickCommand(selector=selector))

    if response.success:
        return response.data or "点击成功"
    else:
        return f"错误：{response.error}"


def browser_input(selector: str, text: str) -> str:
    """在指定元素中输入文本"""
    client = get_ws_client()
    response = client.send_command(InputCommand(selector=selector, text=text))

    if response.success:
        return response.data or "输入成功"
    else:
        return f"错误：{response.error}"


def browser_screenshot() -> str:
    """截取当前页面截图"""
    client = get_ws_client()
    response = client.send_command(ScreenshotCommand())

    if response.success:
        return response.data  # base64 图片
    else:
        return f"错误：{response.error}"


def execute_browser_task(task: str, data: Dict[str, Any]) -> str:
    """执行浏览器任务（粗粒度工具）"""
    client = get_ws_client()
    response = client.send_command(
        ExecuteTaskCommand(task=task, data=data),
        timeout=120  # 任务可能需要较长时间
    )

    if response.success:
        return response.data or "任务完成"
    else:
        return f"错误：{response.error}"
