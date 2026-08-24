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
F1_PATH = os.path.join(MD_DIR, F1_NAME)


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


def append_record(block: str, md_path: str | None = None) -> bool:
    """向 F1 追加一个记录块。O_APPEND 持锁写循环（保证全量）+ 排它锁；失败告警不抛。"""
    path = md_path or F1_PATH
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        data = block.encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            _lock_fd(fd)
            written = 0
            while written < len(data):
                n = os.write(fd, data[written:])
                if n == 0:
                    raise OSError("os.write returned 0")
                written += n
        finally:
            try:
                _unlock_fd(fd)
            except Exception:
                pass
            os.close(fd)
        return True
    except Exception as e:  # best-effort：镜像故障绝不影响对话
        logger.warning(f"[MdMirror] F1 追加失败（不影响对话）: {e}")
        return False


F2_NAME = "F2_dream_queue.md"
F2_PATH = os.path.join(MD_DIR, F2_NAME)
_META_PREFIX = '{"msg_id":'


def record_end_boundaries(lines: list[str]) -> list[int]:
    """记录独占终点列表（0-based 行数==read 显示行数）。输入不得含 split 尾部伪影。"""
    starts = [i for i, ln in enumerate(lines) if ln.startswith(_META_PREFIX)]
    if not starts:
        return []
    return starts[1:] + [len(lines)]


def snap_to_boundary(n: int, boundaries: list[int], min_progress: int = 0) -> int | None:
    candidates = [b for b in boundaries if b <= n and b >= min_progress]
    return max(candidates) if candidates else None


def _write_all(fd: int, data: bytes) -> None:
    written = 0
    while written < len(data):
        n = os.write(fd, data[written:])
        if n == 0:
            raise OSError("os.write returned 0")
        written += n


def relay_processed_prefix(processed_line, f1_path=None, f2_path=None, min_progress=0) -> int:
    """校验 processed_line（吸附记录边界）→ F1 前缀剪切追加 F2 → F1 原地重写剩余。

    原地重写（同 inode）保护 O_APPEND 追加 fd；锁序先 F1 后 F2。
    返回剪切行数；校验失败返回 0。
    """
    p1 = f1_path or F1_PATH
    p2 = f2_path or F2_PATH
    if not isinstance(processed_line, int) or processed_line < 0 or not os.path.exists(p1):
        logger.warning(f"[MdMirror] relay 无效参数: {processed_line}")
        return 0
    try:
        fd = os.open(p1, os.O_RDWR)
    except Exception as e:
        logger.warning(f"[MdMirror] relay 打开 F1 失败: {e}")
        return 0
    try:
        _lock_fd(fd)
        with os.fdopen(os.dup(fd), "r", encoding="utf-8") as f:
            content = f.read()
        lines = content.split("\n")
        if lines and lines[-1] == "":
            lines.pop()  # 剥离尾部伪影：使边界值==read 显示行数（P0-1 修复核心）
        cut = snap_to_boundary(processed_line, record_end_boundaries(lines), min_progress)
        if not cut:
            logger.warning(f"[MdMirror] relay 校验失败: line={processed_line}")
            return 0
        prefix = "".join(l + "\n" for l in lines[:cut])
        rest = "".join(l + "\n" for l in lines[cut:])
        original_bytes = content.encode("utf-8")
        _append_under_lock(p2, prefix)
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            _write_all(fd, rest.encode("utf-8"))
        except Exception:
            # 重写失败 → 尽力恢复 F1 原文（防未提炼尾部永久丢失，审查 B-P2）
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                os.ftruncate(fd, 0)
                _write_all(fd, original_bytes)
                logger.warning("[MdMirror] relay 重写失败已恢复 F1 原文")
            except Exception as restore_err:
                logger.error(f"[MdMirror] F1 恢复失败（数据可能丢失，需人工核查）: {restore_err}")
            raise
        return cut
    except Exception as e:
        logger.warning(f"[MdMirror] relay 失败（尽力而为，异常窗口由 LightRAG MD5 幂等吸收）: {e}")
        return 0
    finally:
        _unlock_fd(fd)
        os.close(fd)


def _append_under_lock(path: str, text: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        _lock_fd(fd)
        _write_all(fd, text.encode("utf-8"))
    finally:
        _unlock_fd(fd)
        os.close(fd)


def truncate_relay_files(f1_path: str | None = None, f2_path: str | None = None) -> None:
    """/clear 配套：截断 F1 与 F2（各自持锁，best-effort）。"""
    for p in (f1_path or F1_PATH, f2_path or F2_PATH):
        try:
            if not os.path.exists(p):
                continue
            fd = os.open(p, os.O_WRONLY)
            try:
                _lock_fd(fd)
                os.ftruncate(fd, 0)
            finally:
                _unlock_fd(fd)
                os.close(fd)
        except Exception as e:
            logger.warning(f"[MdMirror] truncate {p} 失败: {e}")
