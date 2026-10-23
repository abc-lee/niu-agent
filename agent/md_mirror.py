"""MD 镜像层：把 MessageStore 持久化的消息同步镜像为 Markdown 文档（F1 提炼源）。

设计依据：docs/superpowers/specs/2026-08-24-md-relay-pipeline-redesign-design.md §3。
镜像为 best-effort 副本：任何失败只告警，绝不阻断对话持久化路径。
"""

import json
import os
import re
from typing import NamedTuple

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
#: 压缩轮里被省略的 tool 输出占位正文（该记录 meta 行逐字节保留，spec §3.5）
COMPRESSED_TOOL_BODY = ("```output", "（工具输出已省略）", "```")


def _record_meta(line: str) -> dict | None:
    """记录 meta 行 → dict；非 meta 行 / 不能 json.loads / 非 dict / 无 msg_id → None（该行按正文处理）。"""
    if not line.startswith(_META_PREFIX):
        return None
    try:
        obj = json.loads(line)
    except Exception:
        return None
    return obj if isinstance(obj, dict) and "msg_id" in obj else None


def _records(lines: list[str]) -> list[tuple[int, dict]]:
    """记录序列 [(起点下标, meta)]（plan §1：前缀匹配**且**能解析成含 msg_id 的 dict 才算记录）。"""
    out: list[tuple[int, dict]] = []
    for i, ln in enumerate(lines):
        meta = _record_meta(ln)
        if meta is not None:
            out.append((i, meta))
    return out


def _record_starts(lines: list[str]) -> list[int]:
    """记录起点下标（0-based）。前缀匹配但解析失败的行不进列表——它是工具回显/正文。"""
    return [i for i, _ in _records(lines)]


def record_end_boundaries(lines: list[str]) -> list[int]:
    """记录独占终点列表（0-based 行数==read 显示行数）= `_record_starts` 的终点投影，末条 = len(lines)。
    输入不得含 split 尾部伪影。"""
    starts = _record_starts(lines)
    if not starts:
        return []
    return starts[1:] + [len(lines)]


def _turn_end_lines(lines: list[str], include_final_turn: bool) -> list[int]:
    """完整轮末（独占终点，升序）。轮 = `role==user` 记录起、到下一条 user 记录之前
    （`role` 缺失按非 user）；首个 user 之前的记录是残片，不成轮。

    `include_final_turn=False`（F1→F2 relay 口径）：文件末尾没有后继 user 的段不算轮末；
    `True`（F2 口径，切 F3 / drop）：relay 只搬完整轮，故 F2 末段即完整轮，终点 = len(lines)。"""
    recs = _records(lines)
    users = [k for k, (_, meta) in enumerate(recs) if meta.get("role") == "user"]
    ends: list[int] = []
    for k in range(len(users)):
        if k + 1 < len(users):
            ends.append(recs[users[k + 1]][0])   # 后继 user 的起点 = 本轮独占终点
        elif include_final_turn:
            ends.append(len(lines))
    return ends


def strict_turn_ends(lines: list[str]) -> list[int]:
    """F1 口径轮末：只收「有后继 user」的轮——未结束的尾段整轮留 F1 等下次。"""
    return _turn_end_lines(lines, include_final_turn=False)


def all_turn_ends(lines: list[str]) -> list[int]:
    """F2 口径轮末：有后继 user 的轮末 + 末尾轮末（存在 user 记录时）。"""
    return _turn_end_lines(lines, include_final_turn=True)


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
    """校验 processed_line（吸附**完整轮末**，F1 口径 strict_turn_ends）→ F1 前缀剪切追加 F2 → F1 原地重写剩余。

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
        turn_ends = strict_turn_ends(lines)
        cut = snap_to_boundary(processed_line, turn_ends, min_progress)
        if cut and processed_line > cut:
            logger.info(f"[MdMirror] relay 过报告吸附: line={processed_line} -> {cut}（未处理尾部留 F1）")
        if not cut:
            if not _record_starts(lines):
                logger.warning(f"[MdMirror] relay 无记录可吸附（畸形）: line={processed_line}")
            else:
                logger.info(f"[MdMirror] relay 吸不到完整轮末: line={processed_line}（整轮留 F1 等下次）")
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


def _read_locked(path: str) -> str:
    """持排它锁读全文（os.open + dup 读，与 relay/drop 的 fd 纪律一致；失败原样抛出）。"""
    fd = os.open(path, os.O_RDONLY)
    try:
        _lock_fd(fd)
        with os.fdopen(os.dup(fd), "r", encoding="utf-8") as f:
            return f.read()
    finally:
        _unlock_fd(fd)
        os.close(fd)


def _record_local_tokens(text: str) -> int:
    """一条记录原文的本地 token 数（DeepSeek-V3 分词器，与主 Agent 上下文预算同口径）。"""
    from agent.token_calculator import TokenCalculator
    return TokenCalculator.get().count_text(text)


class BuildResult(NamedTuple):
    """`build_f3_from_f2` 返回值（plan §2 契约表）。非 ok 态：行数字段 0、三张表空。"""

    status: str              # ok / no_complete_turn / malformed
    f3_lines: int            # F3 行数
    f2_lines: int            # 选中轮末的 F2 行数（= drop 的 max_lines 上界）
    est_tokens: int          # 选中 span 的 est token（× ratio）
    turns: int               # 进 F3 的完整轮数
    compressed: bool         # 末轮是否省略过中间 tool 输出
    f3_est_tokens: int       # 写出后 F3 的 est token（压缩后口径）
    msg_id_to_turn_end: dict  # F3 记录 msg_id → 所属轮末的 F2 行数
    turn_last_msg_ids: list   # 每轮「自末条向前第一条非空 msg_id」（全空轮不贡献条目）
    f3_turn_ends: list        # [(F3 末行, F2 末行)]，按 F3 行升序


def _empty_build_result(status: str) -> BuildResult:
    return BuildResult(status, 0, 0, 0, 0, False, 0, {}, [], [])


def _msg_id_of(meta: dict) -> str:
    """meta.msg_id 归一化为字符串；None → ""（其余非 str 走 str()，便于诊断）。"""
    raw = meta.get("msg_id")
    if isinstance(raw, str):
        return raw
    return "" if raw is None else str(raw)


def _is_tool_calls_assistant(meta: dict) -> bool:
    tc = meta.get("tool_calls")
    return meta.get("role") == "assistant" and isinstance(tc, list) and bool(tc)


def build_f3_from_f2(token_budget, ratio, f2_path=None, f3_path=None) -> BuildResult:
    """按 **token 预算** 从 F2 头部切「完整轮」前缀整体重建 F3（plan §2/§3，spec §3.4/§3.5）。

    span = 首个 user 记录起、到选中轮末（残片不入 F3）；轮是处理单位，绝不切半轮。
    无残片且未压缩时 F3 是 F2 的逐字节前缀。选点：比「扩大下一轮」与「回缩上一轮」的
    est 代价，取小者，相等取 prev。末轮单轮 est 超预算且该轮有 assistant(tool_calls) →
    省略中间 tool 输出（meta 行原样、正文换成占位），尾组工具输出与该轮末条完整保留。
    所有失败路径先把 F3 写成空文件。F3 单写者，open 'w' 整体重写即可。
    """
    p2 = f2_path or F2_PATH
    p3 = f3_path or F3_PATH
    try:
        budget = int(token_budget)
    except (TypeError, ValueError):
        logger.warning(f"[MdMirror] build_f3 token_budget 非法: {token_budget!r}（本轮放弃）")
        return _empty_build_result("malformed")
    try:
        ratio = float(ratio)
    except (TypeError, ValueError):
        logger.warning(f"[MdMirror] build_f3 ratio 非法: {ratio!r}，按 1.0 计")
        ratio = 1.0
    try:
        parent = os.path.dirname(p3)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(p3, "w", encoding="utf-8"):
            pass  # 先写空 F3：所有失败路径上 F3 保持为空文件
    except Exception as e:
        logger.warning(f"[MdMirror] build_f3 初始化 F3 失败（IO 失败）: {e}")
        return _empty_build_result("malformed")
    if not os.path.exists(p2):
        return _empty_build_result("no_complete_turn")
    try:
        content = _read_locked(p2)
    except Exception as e:
        logger.warning(f"[MdMirror] build_f3 读 F2 失败（IO 失败）: {e}")
        return _empty_build_result("malformed")

    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # 剥离尾部伪影（与 relay 一致，使边界值==read 显示行数）
    if not any(ln.strip() for ln in lines):
        return _empty_build_result("no_complete_turn")            # 空/纯空行：无事可梦境
    recs = _records(lines)
    if not recs:
        logger.warning("[MdMirror] build_f3 F2 无一条可解析记录（畸形）")
        return _empty_build_result("malformed")
    starts = [i for i, _ in recs]
    users = [k for k, (_, meta) in enumerate(recs) if meta.get("role") == "user"]
    if not users:
        return _empty_build_result("no_complete_turn")            # 仅残片：没有完整轮

    def _rec_end(idx: int) -> int:
        """记录 idx 的独占终点（下一条记录起点，末条 = len(lines)）。"""
        return starts[idx + 1] if idx + 1 < len(starts) else len(lines)

    def _record_est(idx: int) -> float:
        return _record_local_tokens("\n".join(lines[starts[idx]:_rec_end(idx)]) + "\n") * ratio

    # 逐轮 est 与累计 est（轮已连续覆盖「首个 user 记录 → 文件末」，故累计即 span est）
    turns: list[tuple[int, int, int]] = []                        # (首记录序号, 末记录序号, F2 轮末行)
    turn_ests: list[float] = []
    cums: list[float] = []
    run = 0.0
    for k, u in enumerate(users):
        nxt = users[k + 1] if k + 1 < len(users) else None
        last_rec = (nxt - 1) if nxt is not None else len(recs) - 1
        f2_end = starts[nxt] if nxt is not None else len(lines)
        turns.append((u, last_rec, f2_end))
        est = sum(_record_est(i) for i in range(u, last_rec + 1))
        turn_ests.append(est)
        run += est
        cums.append(run)

    # 选点：prev = 累计 ≤budget 的最大轮，next = >budget 的最小轮；代价小者胜，相等取 prev
    prev_k: int | None = None
    for k, c in enumerate(cums):
        if c > budget:
            break
        prev_k = k
    if prev_k is None:
        chosen = 0                                                # 只有 next：整轮取之（不切半轮）
    elif prev_k + 1 >= len(cums):
        chosen = prev_k                                           # 只有 prev
    else:
        grow, shrink = cums[prev_k + 1] - budget, budget - cums[prev_k]
        chosen = prev_k + 1 if grow < shrink else prev_k
    sel_first, sel_last, sel_f2_end = turns[chosen]

    # 压缩判据：仅当选中的末轮单轮 est 超预算，且该轮存在 assistant(tool_calls)
    compressed = turn_ests[chosen] > budget and any(
        _is_tool_calls_assistant(recs[i][1]) for i in range(sel_first, sel_last + 1)
    )
    keep_tail: set[int] = set()
    if compressed:
        a_idx: int | None = None
        for i in range(sel_first, sel_last + 1):
            if _is_tool_calls_assistant(recs[i][1]):
                a_idx = i                                         # 该轮最后一条带 tool_calls 的 assistant
        if a_idx is not None:
            ids = {tc.get("id") for tc in recs[a_idx][1]["tool_calls"]
                   if isinstance(tc, dict) and tc.get("id")}
            for j in range(a_idx + 1, sel_last + 1):              # A 之后配对的 tool 输出完整保留
                m = recs[j][1]
                if m.get("role") == "tool" and m.get("tool_call_id") in ids:
                    keep_tail.add(j)
        keep_tail.add(sel_last)                                   # 该轮末条无条件原样

    def _emit_record(idx: int, shrink_body: bool) -> None:
        start, end = starts[idx], _rec_end(idx)
        if not shrink_body:
            out_lines.extend(lines[start:end])                    # 逐字节原样
            return
        body = lines[start + 1:end]
        blanks = 0
        while blanks < len(body) and body[len(body) - 1 - blanks] == "":
            blanks += 1                                           # 记录间空行结构原样保留
        out_lines.append(lines[start])                            # meta 行逐字节原样
        out_lines.extend(COMPRESSED_TOOL_BODY)
        out_lines.extend([""] * blanks)

    out_lines: list[str] = []
    msg_id_to_turn_end: dict[str, int] = {}
    turn_last_msg_ids: list[str] = []
    f3_turn_ends: list[tuple[int, int]] = []
    f3_tokens = 0
    for k in range(chosen + 1):
        _, b, f2_end = turns[k]
        turn_ids: list[str] = []
        for i in range(turns[k][0], b + 1):
            head = len(out_lines)
            _emit_record(i, compressed and k == chosen
                         and recs[i][1].get("role") == "tool" and i not in keep_tail)
            f3_tokens += _record_local_tokens("\n".join(out_lines[head:]) + "\n")
            mid = _msg_id_of(recs[i][1])
            if not mid:
                logger.warning(f"[MdMirror] build_f3 记录 msg_id 为空（F2 行 {starts[i] + 1}）：不建映射")
            else:
                turn_ids.append(mid)
                if mid in msg_id_to_turn_end:
                    logger.warning(f"[MdMirror] build_f3 msg_id 重复: {mid}"
                                   f"（取首个轮末 {msg_id_to_turn_end[mid]}，忽略 {f2_end}）")
                else:
                    msg_id_to_turn_end[mid] = f2_end
        f3_turn_ends.append((len(out_lines), f2_end))
        if turn_ids:
            turn_last_msg_ids.append(turn_ids[-1])                # 自末条向前第一条非空

    text = "".join(ln + "\n" for ln in out_lines)
    try:
        with open(p3, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception as e:
        logger.warning(f"[MdMirror] build_f3 写 F3 失败（IO 失败）: {e}")
        try:
            with open(p3, "w", encoding="utf-8"):                 # 尽力把 F3 恢复成空文件
                pass
        except Exception as restore_err:
            logger.error(f"[MdMirror] build_f3 清空 F3 失败（需人工核查）: {restore_err}")
        return _empty_build_result("malformed")

    est = int(round(cums[chosen]))
    logger.info(f"[MdMirror] build_f3: {chosen + 1} 轮 → F3 {len(out_lines)} 行"
                f"（F2 前 {sel_f2_end} 行，est={est}/{budget} tokens，压缩={compressed}）")
    return BuildResult(
        status="ok",
        f3_lines=len(out_lines),
        f2_lines=sel_f2_end,
        est_tokens=est,
        turns=chosen + 1,
        compressed=compressed,
        f3_est_tokens=int(round(f3_tokens * ratio)),
        msg_id_to_turn_end=msg_id_to_turn_end,
        turn_last_msg_ids=turn_last_msg_ids,
        f3_turn_ends=f3_turn_ends,
    )


def drop_f2_prefix(n_lines, max_lines=None, f2_path=None) -> tuple[int, str]:
    """dream-evolver 报 processed_line 后删 F2 头部 n_lines 行（吸附**完整轮末**，不多删）。

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
        cut = snap_to_boundary(n_lines, all_turn_ends(lines), min_progress=0)
        if cut is None:
            logger.warning(f"[MdMirror] drop_f2_prefix 吸不到完整轮末: n={n_lines}")
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
        except (ValueError, TypeError) as err:
            raise F4LineCorruptError(line, "非 JSON", i) from err
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
    """Chat 页面清空文件原语（v3.5，I-3/口径丁）：持排它锁，复核「**每条记录行都带完成标记，
    或文件没有任何记录行**」⇒ **清空文件（截断到 0），文件本身保留，返 True**；**只要存在未完成行 ⇒ False 且文件保持原样**
    （堵「页面观察到全部完成、清空前写入方新追加一行」的竞态：复核与清空在同一锁区间内完成，
    写入方的 append_record 排它锁会挡住并发追加）。文件缺失 ⇒ True（本来就没有，视为已清；
    「下次打开」形态里文件已被上一次清空/从未写过 ⇒ 同样视为已清）。坏行（非 JSON 等）
    **不算**完成 ⇒ 不清空（坏行先由工作者的弃除路径处置）。
    **清空而非删除**：Windows 不允许跨进程删除被其他进程持有的文件（本队列文件常被 worker 或入库
    MCP 工具持有）⇒ os.remove 抛 PermissionError 被吞 ⇒ 页面状态条永远关不掉；Chat 页面本就对
    「文件缺失/空/只有空行」一律按「无活可干」隐藏状态条，故清空语义在页面侧完全同型。截断复用
    既有 ``os.ftruncate(fd, 0)`` 原语（同 truncate_relay_files）；失败走下方 except 如实返 False
    并告警（不再静默吞）。
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
            os.ftruncate(fd, 0)  # 空文件：截断为无害 no-op，保持与主路径同一原语
            return True
        lines, _torn = _f4_split_records(content)
        for line in lines:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except (ValueError, TypeError):
                return False  # 坏行不是「已完成」⇒ 不清空
            if not isinstance(rec, dict) or rec.get(F4_DONE_KEY) is not True:
                return False  # 未完成行 ⇒ 拒清空
        os.ftruncate(fd, 0)  # 全部完成 ⇒ 清空文件，文件本身保留（Windows 形态：跨进程删除会被挡）
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
