# src/niu_page_agent/protocol.py
"""WebSocket 消息协议定义"""
from dataclasses import dataclass
from typing import Any, Dict, Optional
from abc import ABC, abstractmethod


@dataclass
class BrowserCommand(ABC):
    """浏览器命令基类"""

    @abstractmethod
    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典"""
        pass


@dataclass
class NavigateCommand(BrowserCommand):
    """导航命令"""
    url: str

    def to_dict(self) -> Dict[str, Any]:
        return {"type": "navigate", "url": self.url}


@dataclass
class ClickCommand(BrowserCommand):
    """点击命令"""
    selector: str

    def to_dict(self) -> Dict[str, Any]:
        return {"type": "click", "selector": self.selector}


@dataclass
class InputCommand(BrowserCommand):
    """输入命令"""
    selector: str
    text: str

    def to_dict(self) -> Dict[str, Any]:
        return {"type": "input", "selector": self.selector, "text": self.text}


@dataclass
class ScreenshotCommand(BrowserCommand):
    """截图命令"""

    def to_dict(self) -> Dict[str, Any]:
        return {"type": "screenshot"}


@dataclass
class ExecuteTaskCommand(BrowserCommand):
    """执行任务命令（粗粒度）"""
    task: str
    data: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {"type": "execute_task", "task": self.task, "data": self.data}


@dataclass
class BrowserResponse:
    """浏览器响应"""
    success: bool
    data: Optional[Any] = None
    error: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BrowserResponse":
        return cls(
            success=data.get("success", False),
            data=data.get("data"),
            error=data.get("error")
        )
