"""MD 镜像层：把 MessageStore 持久化的消息同步镜像为 Markdown 文档（F1 提炼源）。

设计依据：docs/superpowers/specs/2026-08-24-md-relay-pipeline-redesign-design.md §3。
镜像为 best-effort 副本：任何失败只告警，绝不阻断对话持久化路径。
"""

import json
import os
import re

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

F3_NAME = "F3_dream_workset.md"
F3_PATH = os.path.join(MD_DIR, F3_NAME)
F3_MAX_BYTES_DEFAULT = 64 * 1024


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
        if cut and processed_line > cut:
            logger.warning(f"[MdMirror] relay 过报告吸附: line={processed_line} -> {cut}（未处理尾部留 F1）")
        if not cut:
            logger.warning(f"[MdMirror] relay 校验失败: line={processed_line}")
            return 0
        prefix = "".join(ln + "\n" for ln in lines[:cut])
        rest = "".join(ln + "\n" for ln in lines[cut:])
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


def _f3_max_bytes() -> int:
    """读 ~/.niu/preferences.json 的 context.dreamWorksetBytes（int 且 >0 才采用）；缺失/非法回退 64KB。"""
    try:
        pref = os.path.join(os.path.expanduser("~"), ".niu", "preferences.json")
        with open(pref, "r", encoding="utf-8") as f:
            data = json.load(f)
        v = data.get("context", {}).get("dreamWorksetBytes")
        if isinstance(v, int) and not isinstance(v, bool) and v > 0:
            return v
    except Exception:
        pass
    return F3_MAX_BYTES_DEFAULT


def build_f3_from_f2(max_bytes=None, f2_path=None, f3_path=None) -> int:
    """从 F2 头部切 ≤max_bytes 字节的前缀（按记录边界对齐）整体重建 F3；返回前缀行数。

    F3 必须是 F2 的逐字节相同前缀副本（否则游标映射断裂）；预算内无完整记录时
    软上限取首条记录整体并告警。F3 单写者，open 'w' 整体重写即可。
    """
    p2 = f2_path or F2_PATH
    p3 = f3_path or F3_PATH
    budget = _f3_max_bytes() if max_bytes is None else max_bytes
    try:
        parent = os.path.dirname(p3)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(p3, "w", encoding="utf-8"):
            pass  # 先写空 F3：所有失败路径上 F3 保持为空文件
    except Exception as e:
        logger.warning(f"[MdMirror] build_f3 初始化 F3 失败: {e}")
        return 0
    if not os.path.exists(p2):
        return 0
    try:
        fd = os.open(p2, os.O_RDONLY)
    except Exception as e:
        logger.warning(f"[MdMirror] build_f3 打开 F2 失败: {e}")
        return 0
    try:
        _lock_fd(fd)
        with os.fdopen(os.dup(fd), "r", encoding="utf-8") as f:
            content = f.read()
    finally:
        _unlock_fd(fd)
        os.close(fd)
    if not content:
        return 0
    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # 剥离尾部伪影（与 relay 一致，使边界值==read 显示行数）
    boundaries = record_end_boundaries(lines)
    if not boundaries:
        logger.warning("[MdMirror] build_f3 F2 无记录边界（畸形）")
        return 0
    line_bytes = [len(ln.encode("utf-8")) + 1 for ln in lines]
    picked, running, pos = None, 0, 0
    for b in boundaries:
        while pos < b:
            running += line_bytes[pos]
            pos += 1
        if running > budget:
            break
        picked = b
    if picked is None:
        logger.warning(f"[MdMirror] build_f3 首记录超预算 {budget}B，软上限取首条记录整体")
        picked = boundaries[0]
    prefix = "".join(ln + "\n" for ln in lines[:picked])
    try:
        with open(p3, "w", encoding="utf-8") as f:
            f.write(prefix)
    except Exception as e:
        logger.warning(f"[MdMirror] build_f3 写 F3 失败: {e}")
        return 0
    return picked


def drop_f2_prefix(n_lines, max_lines=None, f2_path=None) -> tuple[int, str]:
    """dream-evolver 报 processed_line 后删 F2 头部 n_lines 行（吸附记录边界，不多删）。

    返回 (删除行数, 被删前缀末条 msg_id)；任何校验失败返回 (0,"") 且不动文件。
    锁纪律：持 F2 锁体内不得再对同一文件取第二把锁。
    """
    p2 = f2_path or F2_PATH
    if not isinstance(n_lines, int) or n_lines <= 0:
        logger.warning(f"[MdMirror] drop_f2_prefix 无效行数: {n_lines!r}")
        return (0, "")
    try:
        fd = os.open(p2, os.O_RDWR)
    except Exception as e:
        logger.warning(f"[MdMirror] drop_f2_prefix 打开 F2 失败: {e}")
        return (0, "")
    try:
        _lock_fd(fd)
        with os.fdopen(os.dup(fd), "r", encoding="utf-8") as f:
            content = f.read()
        lines = content.split("\n")
        if lines and lines[-1] == "":
            lines.pop()  # 剥离尾部伪影（与 relay 一致，使边界值==read 显示行数）
        total = len(lines)
        if n_lines > total:
            logger.warning(f"[MdMirror] drop_f2_prefix 越界: {n_lines} > {total}")
            return (0, "")
        if max_lines is not None and n_lines > max_lines:
            logger.warning(f"[MdMirror] drop_f2_prefix 超上限: {n_lines} > max_lines={max_lines}")
            return (0, "")
        boundaries = record_end_boundaries(lines)
        cut = snap_to_boundary(n_lines, boundaries, min_progress=0)
        if cut is None or cut < boundaries[0]:
            logger.warning(f"[MdMirror] drop_f2_prefix 无有效边界: n={n_lines}")
            return (0, "")
        prefix_text = "".join(ln + "\n" for ln in lines[:cut])
        matches = re.findall(r'"msg_id":\s*"([^"]+)"', prefix_text)
        if not matches:
            logger.warning("[MdMirror] drop_f2_prefix 前缀无 msg_id（畸形）")
            return (0, "")
        msg_id = matches[-1]
        rest = "".join(ln + "\n" for ln in lines[cut:])
        original_bytes = content.encode("utf-8")
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            _write_all(fd, rest.encode("utf-8"))
        except Exception:
            # 重写失败 → 尽力恢复 F2 原文（镜像 relay_processed_prefix 的恢复模式）
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                os.ftruncate(fd, 0)
                _write_all(fd, original_bytes)
                logger.warning("[MdMirror] drop_f2_prefix 重写失败已恢复 F2 原文")
            except Exception as restore_err:
                logger.error(f"[MdMirror] F2 恢复失败（数据可能丢失，需人工核查）: {restore_err}")
            return (0, "")
        return (cut, msg_id)
    except Exception as e:
        logger.warning(f"[MdMirror] drop_f2_prefix 失败: {e}")
        return (0, "")
    finally:
        _unlock_fd(fd)
        os.close(fd)


def truncate_relay_files(f1_path: str | None = None, f2_path: str | None = None, f3_path: str | None = None) -> None:
    """/clear 配套：截断 F1、F2 与 F3（各自持锁，best-effort）。"""
    for p in (f1_path or F1_PATH, f2_path or F2_PATH, f3_path or F3_PATH):
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
