# Page-Agent Server 集成实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Page-Agent 的 AI-native 浏览器自动化能力集成到项目的 MCP 工具体系，提供双层工具架构（细粒度工具供主Agent交互式操作，粗粒度工具供子Agent批量任务）。

**Architecture:** Python 端启动 Chrome 并加载扩展，通过 WebSocket 调用扩展的 DOM 操作能力。扩展保留 Page-Agent 的 AI-native DOM 理解，Python 端负责工具注册和生命周期管理。复用项目的 LiteLLM，移除 Page-Agent 的 LLM 层。

**Tech Stack:** Python + WebSocket + Chrome Extension + MCP Protocol

---

## 文件结构

### 新建文件

```
mcp-servers/page-agent-server/
├── src/
│   └── niu_page_agent/
│       ├── __init__.py              # MCP 工具注册 + TOOL_SCHEMAS（~150行）
│       ├── browser_launcher.py      # Chrome 启动器（~150行）
│       ├── ws_client.py             # WebSocket 客户端（~200行）
│       ├── browser_tools.py         # 工具函数实现（~400行）
│       └── protocol.py              # 消息协议定义（~50行）
├── pyproject.toml                   # 包配置
└── extension/                       # Chrome 扩展（从 page-agent 复制并修改）
    ├── manifest.json
    ├── background.js
    ├── content.js
    └── hub/
        ├── hub.html
        └── hub-ws.js
```

### 修改文件

```
agent/mcp_loader.py                  # 添加 page-agent-server 到 REQUIRED_SERVERS
config/mcp-servers.yaml              # 添加 page-agent-server 配置
config/agents/niu.md                 # 添加 browser-agent 子 Agent 定义
agent/runner.py                      # 添加 browser-agent 到 sub_agent_descriptions
agent/handler.py                     # 添加 do_chat_with_browser_agent 方法
```

---

## Task 1: 项目初始化和目录结构

**Files:**
- Create: `mcp-servers/page-agent-server/pyproject.toml`
- Create: `mcp-servers/page-agent-server/src/niu_page_agent/__init__.py`

- [ ] **Step 1: 创建项目目录结构**

```bash
mkdir -p mcp-servers/page-agent-server/src/niu_page_agent
mkdir -p mcp-servers/page-agent-server/extension/hub
```

- [ ] **Step 2: 创建 pyproject.toml**

```toml
[build-system]
requires = ["setuptools>=61.0"]
build-backend = "setuptools.build_meta"

[project]
name = "niu-page-agent"
version = "0.1.0"
description = "Page-Agent MCP Server - AI-native browser automation"
requires-python = ">=3.11"
dependencies = [
    "websocket-client>=1.6.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=7.0.0",
    "pytest-asyncio>=0.21.0",
]

[project.scripts]
niu-page-agent = "niu_page_agent.__main__:main"

[tool.setuptools.packages.find]
where = ["src"]
```

- [ ] **Step 3: 创建空的 __init__.py**

```python
"""
Page-Agent MCP Server - AI-native Browser Automation

提供双层工具架构：
- 细粒度工具：browser_navigate, browser_click, browser_input, browser_screenshot
- 粗粒度工具：execute_browser_task
"""
```

- [ ] **Step 4: 提交初始化**

```bash
git add mcp-servers/page-agent-server/
git commit -m "feat: initialize page-agent-server project structure"
```

---

## Task 2: 消息协议定义

**Files:**
- Create: `mcp-servers/page-agent-server/src/niu_page_agent/protocol.py`
- Test: `mcp-servers/page-agent-server/tests/test_protocol.py`

- [ ] **Step 1: 写消息协议测试**

```python
# tests/test_protocol.py
import pytest
from niu_page_agent.protocol import (
    BrowserCommand,
    NavigateCommand,
    ClickCommand,
    InputCommand,
    ScreenshotCommand,
    ExecuteTaskCommand,
    BrowserResponse,
)


def test_navigate_command_serialization():
    cmd = NavigateCommand(url="https://example.com")
    data = cmd.to_dict()
    assert data == {"type": "navigate", "url": "https://example.com"}


def test_click_command_serialization():
    cmd = ClickCommand(selector="#login-button")
    data = cmd.to_dict()
    assert data == {"type": "click", "selector": "#login-button"}


def test_input_command_serialization():
    cmd = InputCommand(selector="#username", text="user123")
    data = cmd.to_dict()
    assert data == {"type": "input", "selector": "#username", "text": "user123"}


def test_execute_task_command_serialization():
    cmd = ExecuteTaskCommand(
        task="填写登录表单",
        data={"username": "user123", "password": "pass456"}
    )
    data = cmd.to_dict()
    assert data == {
        "type": "execute_task",
        "task": "填写登录表单",
        "data": {"username": "user123", "password": "pass456"}
    }


def test_browser_response_success():
    resp = BrowserResponse(success=True, data="操作成功")
    assert resp.success is True
    assert resp.data == "操作成功"
    assert resp.error is None


def test_browser_response_error():
    resp = BrowserResponse(success=False, error="元素未找到")
    assert resp.success is False
    assert resp.data is None
    assert resp.error == "元素未找到"


def test_browser_response_from_dict():
    data = {"success": True, "data": "操作成功"}
    resp = BrowserResponse.from_dict(data)
    assert resp.success is True
    assert resp.data == "操作成功"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd mcp-servers/page-agent-server
pytest tests/test_protocol.py -v
```

Expected: FAIL with "ModuleNotFoundError: No module named 'niu_page_agent.protocol'"

- [ ] **Step 3: 实现消息协议类**

```python
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
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd mcp-servers/page-agent-server
pytest tests/test_protocol.py -v
```

Expected: PASS (7 tests)

- [ ] **Step 5: 提交协议实现**

```bash
git add mcp-servers/page-agent-server/
git commit -m "feat: add message protocol definitions"
```

---

## Task 3: 浏览器启动器

**Files:**
- Create: `mcp-servers/page-agent-server/src/niu_page_agent/browser_launcher.py`
- Test: `mcp-servers/page-agent-server/tests/test_browser_launcher.py`

- [ ] **Step 1: 写浏览器启动器测试**

```python
# tests/test_browser_launcher.py
import pytest
import subprocess
from unittest.mock import Mock, patch, MagicMock
from niu_page_agent.browser_launcher import BrowserLauncher


def test_launcher_initialization():
    launcher = BrowserLauncher(
        extension_path="/path/to/extension",
        port=38401
    )
    assert launcher.extension_path == "/path/to/extension"
    assert launcher.port == 38401
    assert launcher.process is None


@patch('subprocess.Popen')
def test_launch_creates_process(mock_popen):
    mock_process = MagicMock()
    mock_popen.return_value = mock_process

    launcher = BrowserLauncher(
        extension_path="/path/to/extension",
        port=38401
    )
    result = launcher.launch()

    assert result == mock_process
    assert launcher.process == mock_process
    mock_popen.assert_called_once()

    # 验证命令参数包含扩展路径
    call_args = mock_popen.call_args[0][0]
    assert "--load-extension=/path/to/extension" in call_args


@patch('subprocess.Popen')
def test_launch_with_custom_binary(mock_popen):
    launcher = BrowserLauncher(
        extension_path="/path/to/extension",
        chrome_binary="/custom/chrome"
    )
    launcher.launch()

    call_args = mock_popen.call_args[0][0]
    assert "/custom/chrome" in call_args


def test_shutdown_kills_process():
    launcher = BrowserLauncher(extension_path="/path/to/extension")
    launcher.process = MagicMock()
    launcher.shutdown()

    launcher.process.kill.assert_called_once()
    assert launcher.process is None


def test_shutdown_without_process():
    launcher = BrowserLauncher(extension_path="/path/to/extension")
    # 不应该抛异常
    launcher.shutdown()
    assert launcher.process is None
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd mcp-servers/page-agent-server
pytest tests/test_browser_launcher.py -v
```

Expected: FAIL with "ModuleNotFoundError"

- [ ] **Step 3: 实现浏览器启动器**

```python
# src/niu_page_agent/browser_launcher.py
"""Chrome 浏览器启动器"""
import subprocess
import sys
from pathlib import Path
from typing import Optional


class BrowserLauncher:
    """启动 Chrome 浏览器并加载扩展"""

    def __init__(
        self,
        extension_path: str,
        port: int = 38401,
        chrome_binary: Optional[str] = None
    ):
        self.extension_path = extension_path
        self.port = port
        self.chrome_binary = chrome_binary
        self.process: Optional[subprocess.Popen] = None

    def launch(self) -> subprocess.Popen:
        """启动浏览器"""
        chrome_path = self.chrome_binary or self._find_chrome_binary()

        cmd = [
            chrome_path,
            f"--load-extension={self.extension_path}",
            f"--disable-extensions-except={self.extension_path}",
            f"--app=http://localhost:{self.port}",  # 打开 hub 页面
            "--no-first-run",
            "--no-default-browser-check",
        ]

        # Windows 下需要特殊处理
        if sys.platform == "win32":
            # 使用 CREATE_NEW_PROCESS_GROUP 避免父进程退出时子进程被杀
            self.process = subprocess.Popen(
                cmd,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            self.process = subprocess.Popen(cmd)

        return self.process

    def shutdown(self):
        """关闭浏览器"""
        if self.process:
            try:
                self.process.kill()
            except Exception:
                pass
            finally:
                self.process = None

    def _find_chrome_binary(self) -> str:
        """自动检测 Chrome 可执行文件路径"""
        if sys.platform == "win32":
            candidates = [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                Path.home() / "AppData/Local/Google/Chrome/Application/chrome.exe",
            ]
        elif sys.platform == "darwin":
            candidates = [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            ]
        else:  # Linux
            candidates = [
                "/usr/bin/google-chrome",
                "/usr/bin/google-chrome-stable",
                "/usr/bin/chromium-browser",
            ]

        for path in candidates:
            if isinstance(path, str):
                if Path(path).exists():
                    return path
            else:
                if path.exists():
                    return str(path)

        raise FileNotFoundError("Chrome not found. Please specify chrome_binary parameter.")
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd mcp-servers/page-agent-server
pytest tests/test_browser_launcher.py -v
```

Expected: PASS (5 tests)

- [ ] **Step 5: 提交浏览器启动器**

```bash
git add mcp-servers/page-agent-server/
git commit -m "feat: add Chrome browser launcher"
```

---

## Task 4: WebSocket 客户端

**Files:**
- Create: `mcp-servers/page-agent-server/src/niu_page_agent/ws_client.py`
- Test: `mcp-servers/page-agent-server/tests/test_ws_client.py`

- [ ] **Step 1: 写 WebSocket 客户端测试**

```python
# tests/test_ws_client.py
import pytest
from unittest.mock import Mock, patch, MagicMock
import json
from niu_page_agent.ws_client import BrowserWSClient
from niu_page_agent.protocol import NavigateCommand, BrowserResponse


@patch('websocket.create_connection')
def test_client_initialization(mock_create):
    client = BrowserWSClient(port=38401)
    assert client.port == 38401
    assert client.url == "ws://localhost:38401"


@patch('websocket.create_connection')
def test_connect_creates_websocket(mock_create):
    mock_ws = MagicMock()
    mock_create.return_value = mock_ws

    client = BrowserWSClient(port=38401)
    client.connect()

    mock_create.assert_called_once_with("ws://localhost:38401")
    assert client.ws == mock_ws


@patch('websocket.create_connection')
def test_send_command_and_receive_response(mock_create):
    mock_ws = MagicMock()
    mock_create.return_value = mock_ws

    # 模拟响应
    mock_ws.recv.return_value = json.dumps({
        "success": True,
        "data": "导航成功"
    })

    client = BrowserWSClient(port=38401)
    client.connect()

    cmd = NavigateCommand(url="https://example.com")
    response = client.send_command(cmd)

    assert response.success is True
    assert response.data == "导航成功"

    # 验证发送的消息
    sent_data = json.loads(mock_ws.send.call_args[0][0])
    assert sent_data["type"] == "navigate"
    assert sent_data["url"] == "https://example.com"


@patch('websocket.create_connection')
def test_close_connection(mock_create):
    mock_ws = MagicMock()
    mock_create.return_value = mock_ws

    client = BrowserWSClient(port=38401)
    client.connect()
    client.close()

    mock_ws.close.assert_called_once()
    assert client.ws is None
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd mcp-servers/page-agent-server
pytest tests/test_ws_client.py -v
```

Expected: FAIL with "ModuleNotFoundError"

- [ ] **Step 3: 实现 WebSocket 客户端**

```python
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
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd mcp-servers/page-agent-server
pytest tests/test_ws_client.py -v
```

Expected: PASS (4 tests)

- [ ] **Step 5: 提交 WebSocket 客户端**

```bash
git add mcp-servers/page-agent-server/
git commit -m "feat: add WebSocket client for extension communication"
```

---

## Task 5: 浏览器工具函数实现

**Files:**
- Create: `mcp-servers/page-agent-server/src/niu_page_agent/browser_tools.py`
- Test: `mcp-servers/page-agent-server/tests/test_browser_tools.py`

- [ ] **Step 1: 写工具函数测试**

```python
# tests/test_browser_tools.py
import pytest
from unittest.mock import Mock, patch, MagicMock
from niu_page_agent.browser_tools import (
    browser_navigate,
    browser_click,
    browser_input,
    browser_screenshot,
    execute_browser_task,
)


@patch('niu_page_agent.browser_tools.get_ws_client')
def test_browser_navigate(mock_get_client):
    mock_client = MagicMock()
    mock_client.send_command.return_value = Mock(success=True, data="导航成功")
    mock_get_client.return_value = mock_client

    result = browser_navigate("https://example.com")

    assert result == "导航成功"
    mock_client.send_command.assert_called_once()


@patch('niu_page_agent.browser_tools.get_ws_client')
def test_browser_click(mock_get_client):
    mock_client = MagicMock()
    mock_client.send_command.return_value = Mock(success=True, data="点击成功")
    mock_get_client.return_value = mock_client

    result = browser_click("#login-button")

    assert result == "点击成功"


@patch('niu_page_agent.browser_tools.get_ws_client')
def test_browser_input(mock_get_client):
    mock_client = MagicMock()
    mock_client.send_command.return_value = Mock(success=True, data="输入成功")
    mock_get_client.return_value = mock_client

    result = browser_input("#username", "user123")

    assert result == "输入成功"


@patch('niu_page_agent.browser_tools.get_ws_client')
def test_browser_screenshot(mock_get_client):
    mock_client = MagicMock()
    mock_client.send_command.return_value = Mock(
        success=True,
        data="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
    )
    mock_get_client.return_value = mock_client

    result = browser_screenshot()

    assert result.startswith("data:image/png;base64,")


@patch('niu_page_agent.browser_tools.get_ws_client')
def test_execute_browser_task(mock_get_client):
    mock_client = MagicMock()
    mock_client.send_command.return_value = Mock(
        success=True,
        data="任务完成：已填写登录表单"
    )
    mock_get_client.return_value = mock_client

    result = execute_browser_task(
        task="填写登录表单",
        data={"username": "user123", "password": "pass456"}
    )

    assert "任务完成" in result


@patch('niu_page_agent.browser_tools.get_ws_client')
def test_browser_tool_error_handling(mock_get_client):
    mock_client = MagicMock()
    mock_client.send_command.return_value = Mock(
        success=False,
        error="元素未找到"
    )
    mock_get_client.return_value = mock_client

    result = browser_click("#nonexistent")

    assert "错误" in result
    assert "元素未找到" in result
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd mcp-servers/page-agent-server
pytest tests/test_browser_tools.py -v
```

Expected: FAIL with "ModuleNotFoundError"

- [ ] **Step 3: 实现工具函数**

```python
# src/niu_page_agent/browser_tools.py
"""浏览器工具函数实现"""
from typing import Any, Dict
from .ws_client import BrowserWSClient
from .protocol import (
    NavigateCommand,
    ClickCommand,
    InputCommand,
    ScreenshotCommand,
    ExecuteTaskCommand,
)


# 全局 WebSocket 客户端实例
_ws_client: BrowserWSClient = None


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
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd mcp-servers/page-agent-server
pytest tests/test_browser_tools.py -v
```

Expected: PASS (6 tests)

- [ ] **Step 5: 提交工具函数**

```bash
git add mcp-servers/page-agent-server/
git commit -m "feat: add browser tool functions"
```

---

## Task 6: MCP 工具注册

**Files:**
- Modify: `mcp-servers/page-agent-server/src/niu_page_agent/__init__.py`

- [ ] **Step 1: 定义 TOOL_SCHEMAS**

```python
# src/niu_page_agent/__init__.py
"""
Page-Agent MCP Server - AI-native Browser Automation

提供双层工具架构：
- 细粒度工具：browser_navigate, browser_click, browser_input, browser_screenshot
- 粗粒度工具：execute_browser_task
"""
from typing import Any, Dict, List

from .browser_tools import (
    browser_navigate,
    browser_click,
    browser_input,
    browser_screenshot,
    execute_browser_task,
)


TOOL_SCHEMAS = {
    "browser_navigate": {
        "description": "导航到指定 URL",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "目标 URL"
                }
            },
            "required": ["url"]
        }
    },
    "browser_click": {
        "description": "点击页面上的元素",
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": "CSS 选择器或元素描述"
                }
            },
            "required": ["selector"]
        }
    },
    "browser_input": {
        "description": "在页面元素中输入文本",
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": "CSS 选择器或元素描述"
                },
                "text": {
                    "type": "string",
                    "description": "要输入的文本"
                }
            },
            "required": ["selector", "text"]
        }
    },
    "browser_screenshot": {
        "description": "截取当前页面截图，返回 base64 编码的图片",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    "execute_browser_task": {
        "description": "执行浏览器自动化任务（粗粒度工具），适合批量操作和表单填写",
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "任务描述，例如：填写登录表单、批量注册账号"
                },
                "data": {
                    "type": "object",
                    "description": "任务数据，例如表单字段和值"
                }
            },
            "required": ["task"]
        }
    }
}


def get_tool_schemas() -> List[Dict[str, Any]]:
    """返回工具 schema 列表"""
    schemas = []
    for tool_name, tool_def in TOOL_SCHEMAS.items():
        schemas.append({
            "name": tool_name,
            "description": tool_def["description"],
            "input_schema": tool_def["input_schema"]
        })
    return schemas


__all__ = [
    "get_tool_schemas",
    "browser_navigate",
    "browser_click",
    "browser_input",
    "browser_screenshot",
    "execute_browser_task",
]
```

- [ ] **Step 2: 提交 MCP 工具注册**

```bash
git add mcp-servers/page-agent-server/
git commit -m "feat: add MCP tool schemas and registration"
```

---

## Task 7: 集成到项目 MCP 加载器

**Files:**
- Modify: `agent/mcp_loader.py`

- [ ] **Step 1: 添加 page-agent-server 到 REQUIRED_SERVERS**

在 `agent/mcp_loader.py` 的 `REQUIRED_SERVERS` 列表中添加：

```python
REQUIRED_SERVERS: List[Tuple[str, str]] = [
    ("photo-server", "niu_photo_server"),
    ("config-manager", "niu_config_manager"),
    ("memory-server", "niu_memory_server"),
    ("vector-store", "niu_vector_store"),
    ("kg-server", "niu_kg_server"),
    ("file-parser", "niu_file_parser"),
    ("session-manager", "niu_session_manager"),
    ("scheduler-server", "niu_scheduler_server"),
    ("page-agent-server", "niu_page_agent"),  # 新增
]
```

- [ ] **Step 2: 提交集成修改**

```bash
git add agent/mcp_loader.py
git commit -m "feat: add page-agent-server to MCP loader"
```

---

## Task 8: 配置文件更新

**Files:**
- Modify: `config/mcp-servers.yaml`
- Modify: `config/agents/niu.md`

- [ ] **Step 1: 添加 MCP 服务器配置**

在 `config/mcp-servers.yaml` 中添加：

```yaml
page-agent-server:
  command: ${PYTHON_PATH}
  args:
    - "-m"
    - "niu_page_agent"
  workdir: ../mcp-servers/page-agent-server/src
  preload: true
```

- [ ] **Step 2: 添加 browser-agent 子 Agent 定义**

在 `config/agents/` 目录创建 `browser-agent.md`：

```markdown
# Browser Agent

浏览器自动化子 Agent，负责网页浏览、表单填写、信息提取等任务。

## 能力
- 网页导航和元素操作
- 表单自动填写
- 批量任务执行
- 截图和数据提取

## 适用场景
- 简单表单填写（一次性提供所有数据）
- 批量操作（照表执行）
- 独立任务（给定明确目标）

## MCP 工具
- browser_navigate
- browser_click
- browser_input
- browser_screenshot
- execute_browser_task
```

- [ ] **Step 3: 更新主 Agent 配置**

在 `config/agents/niu.md` 的 `mcpServers` 列表中添加：

```yaml
mcpServers:
  - photo-server
  - config-manager
  - memory-server
  - vector-store
  - kg-server
  - file-parser
  - session-manager
  - scheduler-server
  - page-agent-server  # 新增
```

- [ ] **Step 4: 提交配置更新**

```bash
git add config/
git commit -m "feat: add page-agent-server configuration"
```

---

## Task 9: 子 Agent 工具生成

**Files:**
- Modify: `agent/runner.py`
- Modify: `agent/handler.py`

- [ ] **Step 1: 添加 browser-agent 到 sub_agent_descriptions**

在 `agent/runner.py` 的 `sub_agent_descriptions` 字典中添加：

```python
sub_agent_descriptions = {
    "file-processor": "文件处理（PDF/Word/Excel 解析、文档转换）。",
    "event-manager": "事件管理（日程安排、提醒设置）。",
    "context-manager": "上下文管理（记忆检索、知识查询）。",
    "browser-agent": "浏览器自动化（网页浏览、表单填写、信息提取）。",  # 新增
}
```

- [ ] **Step 2: 添加 do_chat_with_browser_agent 方法**

在 `agent/handler.py` 的子 Agent 方法区域添加：

```python
def do_chat_with_browser_agent(self, args: dict, response) -> StepOutcome:
    """调用 browser-agent 子 Agent"""
    return (yield from self._call_subagent_gen("browser-agent", args))
```

- [ ] **Step 3: 提交子 Agent 集成**

```bash
git add agent/
git commit -m "feat: add browser-agent subagent support"
```

---

## Task 10: Chrome 扩展协议修改（简化版）

**注意**：由于扩展代码在 `E:\tools\page-agent\`，本任务只修改协议适配部分。

**Files:**
- Copy from `E:\tools\page-agent\packages\extension\` to `mcp-servers/page-agent-server/extension/`
- Modify: `mcp-servers/page-agent-server/extension/hub/hub-ws.js`

- [ ] **Step 1: 复制扩展代码**

```bash
cp -r E:\tools\page-agent\packages\extension\* mcp-servers/page-agent-server/extension/
```

- [ ] **Step 2: 修改 WebSocket 消息处理**

在 `extension/hub/hub-ws.js` 中修改消息处理逻辑（具体修改点需要查看实际代码，此处为示意）：

```javascript
// 原协议只支持 execute
// 新协议支持细粒度命令：navigate, click, input, screenshot
ws.on('message', (data) => {
  const msg = JSON.parse(data.toString())

  switch(msg.type) {
    case 'navigate':
      // 处理导航
      handleNavigate(msg.url)
      break
    case 'click':
      // 处理点击
      handleClick(msg.selector)
      break
    case 'input':
      // 处理输入
      handleInput(msg.selector, msg.text)
      break
    case 'screenshot':
      // 处理截图
      handleScreenshot()
      break
    case 'execute_task':
      // 原有的任务执行逻辑
      handleExecuteTask(msg.task, msg.data)
      break
  }
})
```

- [ ] **Step 3: 提交扩展修改**

```bash
git add mcp-servers/page-agent-server/extension/
git commit -m "feat: modify extension WebSocket protocol for granular commands"
```

---

## Task 11: 集成测试

**Files:**
- Create: `scripts/test_page_agent_integration.py`

- [ ] **Step 1: 写集成测试脚本**

```python
# scripts/test_page_agent_integration.py
"""Page-Agent Server 集成测试"""
import sys
import time
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.tool_registry import get_registry


def test_tool_registration():
    """测试工具注册"""
    registry = get_registry()

    # 检查工具是否注册
    tools = [
        "page-agent-server/browser_navigate",
        "page-agent-server/browser_click",
        "page-agent-server/browser_input",
        "page-agent-server/browser_screenshot",
        "page-agent-server/execute_browser_task",
    ]

    for tool_name in tools:
        assert registry.get(tool_name) is not None, f"Tool {tool_name} not registered"
        print(f"✓ {tool_name} registered")


def test_browser_launch():
    """测试浏览器启动（需要手动确认）"""
    from niu_page_agent.browser_launcher import BrowserLauncher
    from niu_page_agent.ws_client import BrowserWSClient

    extension_path = Path(__file__).parent.parent / "mcp-servers/page-agent-server/extension"

    launcher = BrowserLauncher(extension_path=str(extension_path))
    launcher.launch()

    print("浏览器已启动，等待扩展连接...")
    time.sleep(5)

    # 尝试连接
    client = BrowserWSClient()
    client.connect(timeout=30)
    print("✓ WebSocket 连接成功")

    # 测试导航
    response = client.send_command(NavigateCommand(url="https://example.com"))
    print(f"✓ 导航测试: {response.data}")

    # 清理
    launcher.shutdown()
    print("✓ 测试完成")


if __name__ == "__main__":
    test_tool_registration()
    # test_browser_launch()  # 需要手动取消注释才能运行
```

- [ ] **Step 2: 运行集成测试**

```bash
python scripts/test_page_agent_integration.py
```

Expected: 工具注册成功，浏览器启动测试需要手动确认

- [ ] **Step 3: 提交集成测试**

```bash
git add scripts/test_page_agent_integration.py
git commit -m "test: add page-agent integration test"
```

---

## Task 12: 文档更新

**Files:**
- Create: `docs/feature-browser-automation.md`
- Update: `CLAUDE.md`

- [ ] **Step 1: 写功能文档**

```markdown
# 浏览器自动化功能

## 概述

Page-Agent Server 提供基于 AI-native DOM 理解的浏览器自动化能力，支持双层工具架构。

## 架构

### 双层工具设计

1. **细粒度工具（主 Agent）**
   - `browser_navigate`: 导航到 URL
   - `browser_click`: 点击元素
   - `browser_input`: 输入文本
   - `browser_screenshot`: 截图

2. **粗粒度工具（子 Agent）**
   - `execute_browser_task`: 执行完整任务

### 适用场景

**细粒度工具适合**：
- 需要逐步与用户交互
- 复杂流程需要人工确认
- 动态决策的操作

**粗粒度工具适合**：
- 简单表单填写（一次性提供所有数据）
- 批量操作（照表执行）
- 独立任务（给定明确目标）

## 使用示例

### 主 Agent 交互式操作

```python
# 用户: "帮我登录GitHub"
# 主Agent逐步执行：
browser_navigate("https://github.com")
browser_screenshot()  # 查看页面
# 询问用户使用哪种登录方式...
browser_click("#login-field")
browser_input("#login-field", "username")
```

### 子 Agent 批量任务

```python
# 主Agent调用子Agent
execute_browser_task(
    task="填写注册表单",
    data={
        "username": "user123",
        "email": "user@example.com",
        "password": "pass456"
    }
)
```

## 技术实现

- Python 端：WebSocket 客户端 + Chrome 启动器
- 扩展端：AI-native DOM 理解 + 操作执行
- 通信：WebSocket 协议
```

- [ ] **Step 2: 更新 CLAUDE.md**

在 `CLAUDE.md` 的 "已实现的 MCP 服务器" 表格中添加：

```markdown
| `page-agent-server` | 浏览器自动化（AI-native DOM 理解） | ✅ |
```

- [ ] **Step 3: 提交文档**

```bash
git add docs/ CLAUDE.md
git commit -m "docs: add browser automation feature documentation"
```

---

## 验收标准

完成所有任务后，应满足：

1. ✅ Python 端工具注册成功（Task 6-8）
2. ✅ Chrome 扩展协议适配完成（Task 10）
3. ✅ 主 Agent 可调用细粒度工具（Task 11 测试）
4. ✅ 子 Agent 可调用粗粒度工具（Task 11 测试）
5. ✅ 集成测试通过（Task 11）
6. ✅ 文档完整（Task 12）

---

**实现完成后，可以进行功能测试：**

```bash
# 1. 启动项目
python -m niu_api

# 2. 在聊天中测试
用户: "帮我打开GitHub主页"
主Agent: 调用 browser_navigate("https://github.com")

用户: "测试下批量表单填写"
主Agent: 调用 chat-with-browser-agent 子Agent
```
