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
        logger.warning(f"[MdMirror] 向 {os.path.basename(path)} 追加失败（不影响主流程）: {e}")
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
        with open(pref, encoding="utf-8") as f:
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


# ============== F4 媒体向量队列（v3.5：一行一条记录 + 完成标记，spec D-21/D-24） ==============
MEDIA_QUEUE_NAME = "media_vector_queue.md"
MEDIA_QUEUE_PATH = os.path.join(MD_DIR, MEDIA_QUEUE_NAME)

#: 完成标记键（v3.5，D-24/I-7）：工作者处置完成（成功或失败终局）时把该行改写为带
#: ``done: true`` 的行、**不删行**；无此键（或非 True）= 未完成。旧格式三键行
#: （entity_name/file_path/ts，无 done）天然未完成 ⇒ 兼容消费——消费侧只认「有无完成标记」，
#: 不依赖 ts 在不在（ts 仅诊断用）。手写行（主 Agent 存量补算 / 手工追加）**不得带 done**
#: （带上 = 被当完成、永不消费、完成比虚高——手册写入纪律已钉死）。
F4_DONE_KEY = "done"

#: 队列行记录的必填键（D-24：多键容忍、忽略；强制校验只卡前两键）。
F4_REQUIRED_KEYS = ("entity_name", "file_path")


class F4LineCorruptError(Exception):
    """队列坏行（非 JSON / 非 dict / 缺必填键）出现在文件**任意位置**（v3.5：不再限于队首——
    完成行留在文件里后，坏行可能夹在已完成行与未完成行之间）。消费者（tick）必须醒目告警 +
    ``f4_drop_line`` 按行号弃行：坏行留着会永久卡死「第一条未完成」扫描，弃掉才是安全行为
    （D-24）。携带行内容、原因与行号。"""

    def __init__(self, line: str, reason: str, line_index: int = 0):
        super().__init__(f"队列第 {line_index} 行坏行（{reason}）: {line[:200]!r}")
        self.line = line
        self.reason = reason
        self.line_index = line_index


def _f4_split_records(content: str) -> tuple[list[str], bool]:
    """行拆分（记录边界 = 一整行，空行不是记录，D-24）：返回 (行列表, 尾行是否撕裂)。
    尾行若无换行符 = O_APPEND 写了一半的撕裂行，不是完整记录（写入方可能正在写）⇒ 从
    行列表里摘掉、由调用方重写时原样拼回——撕裂尾行不许被分类、删除或标注。"""
    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # 末尾换行的伪影
        return lines, False
    lines.pop()  # 尾行无换行 = 写了一半，不是完整记录
    return lines, True


def _f4_scan_records(lines: list[str], skip_done: bool) -> int | None:
    """按序扫描，返回第一条目标记录的行号；无 ⇒ None。目标 = ``skip_done`` 时为未完成行，
    否则任意记录行。坏行 ⇒ 抛 F4LineCorruptError（带行号）。空行跳过（不是记录）。"""
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except (ValueError, TypeError):
            raise F4LineCorruptError(line, "非 JSON", i)
        if not isinstance(rec, dict) or any(not rec.get(k) for k in F4_REQUIRED_KEYS):
            raise F4LineCorruptError(line, f"缺必填键 {F4_REQUIRED_KEYS}", i)
        if skip_done and rec.get(F4_DONE_KEY) is True:
            continue  # 已完成行：跳过（文件里完成+未完成并存，v3.5 I-2）
        return i
    return None


def f4_take_first_pending(queue_path: str | None = None) -> tuple[int | None, dict | None]:
    """原子读「**第一条未完成**记录」（不删行、不改行；v3.5 取代 f4_take_head 的取件语义）。

    从队首起扫描，跳过空行与已完成行（``done is True``）；旧格式行（无 done 键）天然未完成
    ⇒ 兼容（I-7）。文件缺失 / 空文件 / 仅空行与已完成行 → ``(None, None)``（无活可干——
    完成行不是「活」，文件由 Chat 页面删，I-3/口径丁）。遇到的第一条坏行 ⇒ 抛
    F4LineCorruptError。非 UTF-8 字节 / 权限异常 ⇒ 异常向上抛，由 tick 侧按「读不出」处理
    （I-8 的 -1 哨兵同源）。锁纪律：排它锁贯穿读取全程（与 F2 同协议的姊妹原语）。
    """
    p = queue_path or MEDIA_QUEUE_PATH
    try:
        fd = os.open(p, os.O_RDONLY)
    except FileNotFoundError:
        return None, None
    try:
        _lock_fd(fd)
        with os.fdopen(os.dup(fd), "r", encoding="utf-8") as f:
            content = f.read()
    finally:
        _unlock_fd(fd)
        os.close(fd)
    if not content:
        return None, None
    lines, _torn = _f4_split_records(content)
    i = _f4_scan_records(lines, skip_done=True)
    if i is None:
        return None, None
    rec = json.loads(lines[i])  # 扫描已验证可解析、键齐全
    return i, rec


def _f4_rewrite_line(fd: int, content: str, line_index: int, new_line: str | None) -> str:
    """持锁整文件重写共享逻辑（标注/弃除同型；失败自动恢复原文）。
    ``new_line=None`` = 删除该行。返回 "ok" | "failed"（failed ⇒ 文件已恢复原样）。

    已知取舍（R1 A-P3-2，与 v3.0「撕裂尾行下次追加即拼坏」同危害类，非新增数据丢失）：目标行是
    **最后一条完整行**时，重写结果 ``rest[:-1] + torn`` 会把该行与撕裂尾行拼成一条坏行，下轮弃除
    路径将其弃掉并收敛——若它是 ``done`` 标注行 ⇒ 该完成行消失（意图不受影响：向量已入库、总数
    减 1、完成比相应移动）；若它是被弃除的坏行 ⇒ 拼合落到它前面一行（如有），该行下轮同成坏行
    被弃。目标行不是最后一行时，拼合落在撕裂尾行自身（尾行不是记录，下轮原样不动）。
    """
    lines, torn = _f4_split_records(content)
    if not isinstance(line_index, int) or line_index < 0 or line_index >= len(lines):
        logger.warning(f"[MdMirror] f4 行操作越界: {line_index} >= {len(lines)}")
        return "failed"
    if new_line is None and not lines[line_index].strip():
        logger.warning("[MdMirror] f4 弃除指向空行，拒动")
        return "failed"
    if new_line is None:
        lines.pop(line_index)
    else:
        lines[line_index] = new_line
    rest = "".join(ln + "\n" for ln in lines)
    if torn:
        # 撕裂尾行原样拼回（无尾换行），绝不吞掉写入方写了一半的行
        rest = rest[:-1] + content.split("\n")[-1]
    original = content.encode("utf-8")
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        view = memoryview(rest.encode("utf-8"))
        while view:
            n = os.write(fd, view)
            view = view[n:]
    except Exception:
        try:  # 重写失败自动恢复原文（与旧 f4_drop_prefix 的恢复语义同型）
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            view = memoryview(original)
            while view:
                n = os.write(fd, view)
                view = view[n:]
            logger.warning("[MdMirror] f4 队列重写失败，已恢复原队列文本")
        except Exception as e2:
            logger.error(f"[MdMirror] 队列恢复失败（数据可能丢失，需人工检查）: {e2}")
        return "failed"
    return "ok"


def f4_mark_line_done(line_index: int, queue_path: str | None = None,
                      expect: tuple[str, str] | None = None) -> str:
    """把 ``line_index`` 行改写为「原键保留 + done: true」（v3.5 完成标注，I-2/I-5）。

    开排它锁 → 读全文 → 该行 JSON 解析后**原键逐字保留**、仅追加 ``done: True`` 重序列化，
    其余行逐字节重写，撕裂尾行原样拼回 → 失败自动恢复原文。``expect = (entity_name,
    file_path)`` 是内容护栏：该行解析后的两键与 expect 不符 ⇒ 返回 "failed" 且文件不动。
    返回 "marked"（已标注）| "missing"（文件不存在 = Chat 页面已删文件，I-3/口径丁 的竞态
    形态）| "failed"（越界/空行/护栏不符/重写失败已恢复）。
    """
    p = queue_path or MEDIA_QUEUE_PATH
    try:
        fd = os.open(p, os.O_RDWR)
    except FileNotFoundError:
        return "missing"
    except Exception as e:
        logger.warning(f"[MdMirror] f4_mark_line_done 打开失败: {e}")
        return "failed"
    try:
        _lock_fd(fd)
        with os.fdopen(os.dup(fd), "r", encoding="utf-8") as f:
            content = f.read()
        if not content:
            return "failed"
        lines, _torn = _f4_split_records(content)
        if not isinstance(line_index, int) or line_index < 0 or line_index >= len(lines):
            logger.warning(f"[MdMirror] f4_mark_line_done 越界: {line_index} >= {len(lines)}")
            return "failed"
        try:
            rec = json.loads(lines[line_index])
        except (ValueError, TypeError):
            logger.warning("[MdMirror] f4_mark_line_done 指向坏行，拒标注（应走弃除路径）")
            return "failed"
        if not isinstance(rec, dict) or any(not rec.get(k) for k in F4_REQUIRED_KEYS):
            return "failed"
        if expect is not None and (rec.get("entity_name"), rec.get("file_path")) != tuple(expect):
            logger.warning("[MdMirror] f4_mark_line_done 内容护栏不符（行已被改动？），拒标注")
            return "failed"
        rec[F4_DONE_KEY] = True
        r = _f4_rewrite_line(fd, content, line_index, json.dumps(rec, ensure_ascii=False))
        return "marked" if r == "ok" else "failed"
    except Exception as e:
        logger.warning(f"[MdMirror] f4_mark_line_done 失败: {e}")
        return "failed"
    finally:
        _unlock_fd(fd)
        os.close(fd)


def f4_drop_line(line_index: int, queue_path: str | None = None) -> str:
    """弃除 ``line_index`` 行（**坏行**专用，文件任意位置；v3.5 取代 f4_drop_prefix 的弃行职责）。
    整行边界：撕裂尾行绝不被触及。重写协议与失败恢复同 ``f4_mark_line_done``。
    返回 "dropped" | "missing" | "failed"。"""
    p = queue_path or MEDIA_QUEUE_PATH
    try:
        fd = os.open(p, os.O_RDWR)
    except FileNotFoundError:
        return "missing"
    except Exception as e:
        logger.warning(f"[MdMirror] f4_drop_line 打开失败: {e}")
        return "failed"
    try:
        _lock_fd(fd)
        with os.fdopen(os.dup(fd), "r", encoding="utf-8") as f:
            content = f.read()
        if not content:
            return "failed"
        r = _f4_rewrite_line(fd, content, line_index, None)
        return "dropped" if r == "ok" else r
    except Exception as e:
        logger.warning(f"[MdMirror] f4_drop_line 失败: {e}")
        return "failed"
    finally:
        _unlock_fd(fd)
        os.close(fd)


def f4_dismiss_if_all_done(queue_path: str | None = None) -> bool:
    """Chat 页面删文件原语（v3.5，I-3/口径丁）：持排它锁，复核「**每条记录行都带完成标记，
    或文件没有任何记录行**」⇒ 删文件返 True；**只要存在未完成行 ⇒ False 且文件保持原样**
    （堵「页面观察到全部完成、删除前写入方新追加一行」的竞态：复核与删除在同一锁区间内完成，
    写入方的 append_record 排它锁会挡住并发追加）。文件缺失 ⇒ True（本来就没有，视为已清；
    「下次打开」形态里文件已被上一次删除/从未写过 ⇒ 同样视为已清）。坏行（非 JSON 等）
    **不算**完成 ⇒ 不删（坏行先由工作者的弃除路径处置）。
    """
    p = queue_path or MEDIA_QUEUE_PATH
    try:
        fd = os.open(p, os.O_RDWR)
    except FileNotFoundError:
        return True
    try:
        _lock_fd(fd)
        with os.fdopen(os.dup(fd), "r", encoding="utf-8") as f:
            content = f.read()
        if not content:
            os.remove(p)
            return True
        lines, _torn = _f4_split_records(content)
        for line in lines:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except (ValueError, TypeError):
                return False  # 坏行不是「已完成」⇒ 不删
            if not isinstance(rec, dict) or rec.get(F4_DONE_KEY) is not True:
                return False  # 未完成行 ⇒ 拒删
        os.remove(p)
        return True
    except Exception as e:
        logger.warning(f"[MdMirror] f4_dismiss_if_all_done 失败: {e}")
        return False
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
