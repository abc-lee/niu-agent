"""
MCP HTTP Bridge

通过 HTTP 调用 MCP 服务器，保持 MCP 独立。

用法：
    from agent.mcp_bridge import call_mcp

    result = call_mcp("photo-server", "ingest_photo", {"path": "/path/to/photo.jpg"})
"""

import json
import urllib.request
import urllib.error
from typing import Dict, Any, Optional


# MCP 服务器端口配置
MCP_PORTS = {
    "photo-server": 9871,
    "kg-server": 9872,
    "vector-store": 9873,
    "config-manager": 9874,
    "file-parser": 9875,
    "embedding-service": 9877,
    "memory-server": 9878,
    "session-manager": 9879,
}


def call_mcp(
    server: str, tool: str, arguments: Dict[str, Any], timeout: int = 30
) -> Dict[str, Any]:
    """
    调用 MCP 服务器的工具

    Args:
        server: MCP 服务器名称
        tool: 工具名称
        arguments: 工具参数
        timeout: 超时时间（秒）

    Returns:
        工具执行结果
    """
    port = MCP_PORTS.get(server)
    if not port:
        return {"error": f"Unknown MCP server: {server}"}

    url = f"http://127.0.0.1:{port}/tools/{tool}"

    try:
        body = json.dumps(arguments).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
            return result

    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.reason}"}
    except urllib.error.URLError as e:
        return {"error": f"URL Error: {e.reason}"}
    except Exception as e:
        return {"error": str(e)}


# 便捷函数
def ingest_photo(path: str, **kwargs) -> Dict[str, Any]:
    """入库照片"""
    args = {"path": path, **kwargs}
    return call_mcp("photo-server", "ingest_photo", args)


def ingest_document(path: str, **kwargs) -> Dict[str, Any]:
    """入库文档"""
    args = {"path": path, **kwargs}
    return call_mcp("file-parser", "ingest_document", args)


def search_vectors(query: str, limit: int = 10, **kwargs) -> Dict[str, Any]:
    """向量搜索"""
    args = {"query": query, "limit": limit, **kwargs}
    return call_mcp("vector-store", "search", args)


def get_config(key: str) -> Dict[str, Any]:
    """获取配置"""
    return call_mcp("config-manager", "get", {"key": key})


def set_config(key: str, value: Any) -> Dict[str, Any]:
    """设置配置"""
    return call_mcp("config-manager", "set", {"key": key, "value": value})
