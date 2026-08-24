"""MD 镜像层：把 MessageStore 持久化的消息同步镜像为 Markdown 文档（F1 提炼源）。

设计依据：docs/superpowers/specs/2026-08-24-md-relay-pipeline-redesign-design.md §3。
镜像为 best-effort 副本：任何失败只告警，绝不阻断对话持久化路径。
"""

import json
import os

from loguru import logger

try:  # Unix
    import fcntl
except ImportError:  # Windows
    fcntl = None
try:  # Windows
    import msvcrt
except ImportError:
    msvcrt = None

TOOL_OUTPUT_MAX_BYTES = 2000
TOOL_OUTPUT_HEAD_BYTES = 1200
TOOL_OUTPUT_TAIL_BYTES = 800
TOOL_OUTPUT_MARKER = "<已精简>"

MD_DIR = os.path.join(os.path.expanduser("~"), ".niu", "md")
F1_NAME = "F1_extract_source.md"


def _lock_fd(fd) -> None:
    """跨平台排它锁（对齐 niu_api/compat.py _flock 先例）。"""
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_EX)
    elif msvcrt is not None:
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)


def _unlock_fd(fd) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
    elif msvcrt is not None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


def _safe_decode_head(raw: bytes, limit: int) -> str:
    """取前 limit 字节并解码，末尾不完整字节序列向前回退。"""
    chunk = raw[:limit]
    while chunk:
        try:
            return chunk.decode("utf-8")
        except UnicodeDecodeError:
            chunk = chunk[:-1]
    return ""


def _safe_decode_tail(raw: bytes, limit: int) -> str:
    """取后 limit 字节并解码，开头不完整字节序列向后丢弃。"""
    chunk = raw[-limit:] if limit else b""
    while chunk:
        try:
            return chunk.decode("utf-8")
        except UnicodeDecodeError:
            chunk = chunk[1:]
    return ""


def truncate_tool_output(text: str) -> str:
    """>2000 字节的工具输出取头 60% + 占位符 + 尾 40%（UTF-8 边界安全）。"""
    raw = (text or "").encode("utf-8")
    if len(raw) <= TOOL_OUTPUT_MAX_BYTES:
        return text or ""
    head = _safe_decode_head(raw, TOOL_OUTPUT_HEAD_BYTES)
    tail = _safe_decode_tail(raw, TOOL_OUTPUT_TAIL_BYTES)
    return f"{head}{TOOL_OUTPUT_MARKER}{tail}"

F1_PATH = os.path.join(MD_DIR, F1_NAME)


def format_message_record(
    *,
    msg_id: str,
    created_at: str,
    role: str,
    content: str,
    tool_calls: list | None = None,
    tool_call_id: str = "",
    degraded_reason: str = "",
) -> str | None:
    """格式化单条消息为模拟 JSON 结构的 MD 记录块。system 角色返回 None（不镜像）。"""
    if role == "system":
        return None
    meta: dict = {"msg_id": msg_id, "ts": created_at, "role": role}
    if tool_calls:
        meta["tool_calls"] = tool_calls
    if tool_call_id:
        meta["tool_call_id"] = tool_call_id
    if degraded_reason:
        meta["degraded_reason"] = degraded_reason
    lines = [json.dumps(meta, ensure_ascii=False)]
    if role == "tool":
        lines += ["```output", truncate_tool_output(content), "```"]
    else:
        lines.append(content or "")
    return "\n".join(lines) + "\n\n"
