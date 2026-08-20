"""
Niu Agent Handler

继承 GenericAgent 的 BaseHandler，实现自定义工具处理。
"""

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

from loguru import logger

# 导入 GenericAgent 基类
from .generic.agent_loop import BaseHandler, StepOutcome, StreamEvent, try_call_generator

# 导入经验总结器
# ExperienceSummarizer disabled

# E1 统一异常兜底（2026-08-15）：错误文本截断上限（防上下文爆炸——总体方案风险 6）
_TOOL_ERROR_MAX = 500
# 截断时尾部保留长度——保尾 (fname:lineno) 位置信息（供区分 harness 伪装与真实工具错误）
_TOOL_ERROR_TAIL = 100


def format_error(e: Exception) -> str:
    """格式化错误信息"""
    exc_type, exc_value, exc_traceback = sys.exc_info()
    tb = traceback.extract_tb(exc_traceback)
    if tb:
        f = tb[-1]
        fname = os.path.basename(f.filename)
        return f"{type(e).__name__}: {e} ({fname}:{f.lineno})"
    return f"{type(e).__name__}: {e}"


def _run_coroutine(coro):
    """在同步上下文中执行 coroutine（集中桥接点）"""
    import asyncio
    import inspect
    if not inspect.iscoroutine(coro):
        return coro
    try:
        asyncio.get_running_loop()
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result()
    except RuntimeError:
        return asyncio.run(coro)


def _check_chat_with_agent_exists(agent_name: str) -> tuple[bool, str]:
    """检查 chat-with-{name} 目标子 Agent 是否存在（config/agents/ + ~/.niu/agents/）。

    get_subagent_config 返回空 dict = 配置不存在（未创建 MD 或拼写错误）。
    """
    from agent.subagent import get_subagent_config
    config = get_subagent_config(agent_name)
    if not config:
        return False, (
            f"子Agent {agent_name} 不存在（未在 config/agents/ 或 ~/.niu/agents/ 找到配置）。"
            f"请先用 write 创建 ~/.niu/agents/{agent_name}.md（含 description frontmatter），"
            f"或检查名称拼写。"
        )
    return True, ""


def read_file(file_path: str, offset: int = 1, limit: int = 500) -> str:
    """读取文件内容（支持 offset/limit 分页，limit 最大 500；offset 为负数时读取文件末尾 |offset| 行）"""
    import itertools

    max_limit = 500
    # offset < 0 → tail 语义：读取文件末尾 |offset| 行（需先数总行数再换算起始行）
    tail_lines = -offset if offset < 0 else 0
    if offset < 1:
        offset = 1
    if limit < 1:
        limit = max_limit
    if limit > max_limit:
        limit = max_limit

    try:
        if os.path.isdir(file_path):
            return f"Error: '{file_path}' is a directory, not a file."
        with open(file_path, encoding="utf-8", errors="replace") as f:
            total_lines = sum(1 for _ in f)
            f.seek(0)
            if tail_lines > 0:
                if total_lines == 0:
                    return "[FILE] No content to display (total=0 lines)"
                # 末尾 N 行起始行 = total - N + 1；文件不足 N 行则从第 1 行开始（返回全部，不报错）
                offset = max(1, total_lines - tail_lines + 1)
            stream = ((i, line.rstrip("\r\n")) for i, line in enumerate(f, 1))
            stream = itertools.dropwhile(lambda x: x[0] < offset, stream)
            res = list(itertools.islice(stream, limit))

            if not res:
                if offset > total_lines:
                    return f"[FILE] offset={offset} exceeds total lines ({total_lines}). Use offset=1 to read from the beginning."
                return f"[FILE] No content to display (offset={offset}, total={total_lines} lines)"

            realcnt = len(res)
            l_max = min(10000, max(100, 500000 // max(realcnt, 1)))
            tag = " ... [TRUNCATED]"

            res = [(i, line if len(line) <= l_max else line[:l_max] + tag) for i, line in res]
            result = "\n".join(f"{i}|{line}" for i, line in res)

            header = f"[FILE] Showing {len(res)} lines from line {offset} (total {total_lines} lines)"
            if offset + limit - 1 < total_lines:
                header += f"\n[Use offset={offset + limit} to read more]"

            return header + "\n" + result
    except FileNotFoundError:
        return f"Error: File not found: '{file_path}'"
    except Exception as e:
        return f"Error: {str(e)}"


def write_file(file_path: str, content: str, mode: str = "overwrite") -> dict:
    """写入文件（支持 overwrite/append 模式）"""
    try:
        if mode not in ("overwrite", "append"):
            return {"status": "error", "msg": f"Invalid mode '{mode}'. Use 'overwrite' or 'append'."}
        file_path = str(Path(os.path.expanduser(file_path)).resolve())
        dir_path = os.path.dirname(file_path)
        if dir_path and not os.path.exists(dir_path):
            os.makedirs(dir_path, exist_ok=True)
        existed = os.path.exists(file_path)
        write_mode = "a" if mode == "append" else "w"
        with open(file_path, write_mode, encoding="utf-8") as f:
            f.write(content)
        action = "Appended" if mode == "append" and existed else "Written"
        return {"status": "success", "msg": f"{action} {len(content)} bytes to {file_path}"}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


def edit_file(file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> dict:
    """局部修改文件（精确字符串替换）"""
    try:
        file_path = str(Path(os.path.expanduser(file_path)).resolve())
        if not os.path.exists(file_path):
            return {"status": "error", "msg": "File not found"}

        with open(file_path, encoding="utf-8") as f:
            full_text = f.read()

        if not old_string:
            return {"status": "error", "msg": "old_string is empty"}

        if old_string == new_string:
            return {"status": "error", "msg": "old_string and new_string are identical. No change needed."}

        count = full_text.count(old_string)
        if count == 0:
            return {"status": "error", "msg": "old_string not found"}
        if count > 1 and not replace_all:
            return {"status": "error", "msg": f"Found {count} matches. Use replace_all=true or provide more context to make old_string unique."}

        if replace_all:
            updated_text = full_text.replace(old_string, new_string)
        else:
            updated_text = full_text.replace(old_string, new_string, 1)

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(updated_text)

        replaced = count if replace_all else 1
        return {"status": "success", "msg": f"Replaced {replaced} occurrence(s)"}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


def grep_search(pattern: str, path: str = ".", include: str = "") -> str:
    """搜索文件内容（支持正则，最多返回50个匹配）"""
    import glob as glob_mod
    import re as re_mod

    max_lines = 50

    if not pattern:
        return "[GREP] Error: pattern is required"

    if not os.path.exists(path):
        return f"[GREP] Error: Path does not exist: '{path}'"

    try:
        regex = re_mod.compile(pattern)
    except re_mod.error:
        regex = re_mod.compile(re_mod.escape(pattern))

    matches = []

    if os.path.isfile(path):
        files = [path]
    else:
        if include:
            files = glob_mod.glob(os.path.join(path, "**", include), recursive=True)
        else:
            files = glob_mod.glob(os.path.join(path, "**", "*"), recursive=True)
        # 过滤目录和二进制文件
        binary_exts = ('.pyc', '.so', '.dylib', '.exe', '.png', '.jpg', '.jpeg', '.gif', '.ico',
                        '.woff', '.woff2', '.ttf', '.eot', '.db', '.sqlite', '.graphml', '.jsonl',
                        '.zip', '.gz', '.tar', '.rar', '.pdf', '.doc', '.docx', '.ppt', '.pptx',
                        '.xls', '.xlsx', '.class', '.o', '.obj', '.bin', '.dat',
                        '.wav', '.mp3', '.mp4', '.avi', '.mov', '.svg')
        files = [f for f in files if os.path.isfile(f) and not f.endswith(binary_exts)]

    searched_count = 0
    failed_files = []
    for filepath in files[:200]:
        searched_count += 1
        try:
            with open(filepath, encoding="utf-8", errors="replace") as f:
                for line_no, line in enumerate(f, 1):
                    if regex.search(line):
                        matches.append(f"{filepath}:{line_no}:{line.rstrip()}")
                        if len(matches) >= max_lines:
                            break
        except (OSError, UnicodeDecodeError):
            failed_files.append(filepath)
            continue
        if len(matches) >= max_lines:
            break

    failed_count = len(failed_files)
    if not matches:
        if failed_count > 0 and failed_count == searched_count:
            return f"[GREP] 读取失败 {failed_count} 个文件——无法确认匹配"
        failure_note = ""
        if failed_count:
            shown = failed_files[:5]
            shown_repr = ", ".join(shown)
            if failed_count > len(shown):
                shown_repr += ", ..."
            failure_note = f", {failed_count} failed to read: [{shown_repr}]"
        return f"[GREP] No matches for '{pattern}' in {path} (searched {searched_count} files{failure_note})"

    result = "\n".join(matches)
    if len(matches) >= max_lines:
        result += f"\n... (showing first {max_lines} matches)"
    if failed_count:
        shown = failed_files[:5]
        shown_repr = ", ".join(shown)
        if failed_count > len(shown):
            shown_repr += ", ..."
        result += f"\n（另有 {failed_count} 个文件读取失败：{shown_repr}）"
    return result


def file_read(
    path: str, start: int = 1, keyword: str = None, count: int = 200, show_linenos: bool = True
) -> str:
    """读取文件内容"""
    import collections
    import itertools
    import os

    try:
        if os.path.isdir(path):
            return f"Error: '{path}' is a directory, not a file. Please provide a file path, e.g. '~/.niu/skills/photo-face-display.md'"
        with open(path, encoding="utf-8", errors="replace") as f:
            stream = ((i, line.rstrip("\r\n")) for i, line in enumerate(f, 1))
            stream = itertools.dropwhile(lambda x: x[0] < start, stream)

            if keyword:
                before = collections.deque(maxlen=count // 3)
                for i, line in stream:
                    if keyword.lower() in line.lower():
                        res = (
                            list(before)
                            + [(i, line)]
                            + list(itertools.islice(stream, count - len(before) - 1))
                        )
                        break
                    before.append((i, line))
                else:
                    return f"Keyword '{keyword}' not found after line {start}.\n"
            else:
                res = list(itertools.islice(stream, count))

            realcnt = len(res)
            l_max = max(100, 512000 // realcnt) if realcnt > 0 else 100
            tag = " ... [TRUNCATED]"

            res = [(i, line if len(line) <= l_max else line[:l_max] + tag) for i, line in res]
            result = "\n".join(f"{i}|{line}" if show_linenos else line for i, line in res)

            if show_linenos:
                result = f"[FILE] Showing {len(res)} lines from line {start}\n" + result

            return result
    except FileNotFoundError:
        return f"Error: File not found: '{path}'. Please check the file path."
    except Exception as e:
        return f"Error: {str(e)}"


def file_write(path: str, content: str, mode: str = "write") -> dict:
    """写入文件"""
    try:
        path = str(Path(os.path.expanduser(path)).resolve())
        os.makedirs(os.path.dirname(path), exist_ok=True)

        if mode == "append":
            with open(path, "a", encoding="utf-8") as f:
                f.write(content)
        else:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)

        return {"status": "success", "msg": f"Written {len(content)} bytes to {path}"}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


def file_patch(path: str, old_content: str, new_content: str) -> dict:
    """局部修改文件"""
    try:
        path = str(Path(os.path.expanduser(path)).resolve())
        if not os.path.exists(path):
            return {"status": "error", "msg": "File not found"}

        with open(path, encoding="utf-8") as f:
            full_text = f.read()

        if not old_content:
            return {"status": "error", "msg": "old_content is empty"}

        count = full_text.count(old_content)
        if count == 0:
            return {"status": "error", "msg": "old_content not found"}
        if count > 1:
            return {"status": "error", "msg": f"Found {count} matches, need unique match"}

        updated_text = full_text.replace(old_content, new_content)
        with open(path, "w", encoding="utf-8") as f:
            f.write(updated_text)

        return {"status": "success", "msg": "File patched successfully"}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


def code_run(code: str, code_type: str = "python", timeout: int = 60, cwd: str = None) -> dict:
    """执行代码"""
    preview = (code[:60].replace("\n", " ") + "...") if len(code) > 60 else code.strip()
    print(f"[Action] Running {code_type}: {preview}", file=sys.stderr, flush=True)

    cwd = cwd or tempfile.gettempdir()
    tmp_path = None

    if code_type == "python":
        tmp_file = tempfile.NamedTemporaryFile(
            suffix=".ai.py", delete=False, mode="w", encoding="utf-8"
        )
        tmp_file.write(code)
        tmp_path = tmp_file.name
        tmp_file.close()
        cmd = [sys.executable, "-X", "utf8", "-u", tmp_path]
    elif code_type in ["powershell", "bash"]:
        if os.name == "nt":
            cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", code]
        else:
            cmd = ["bash", "-c", code]
    else:
        return {"status": "error", "msg": f"Unsupported type: {code_type}"}

    startupinfo = None
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0

    full_stdout = []

    def stream_reader(proc, logs):
        for line_bytes in iter(proc.stdout.readline, b""):
            try:
                line = line_bytes.decode("utf-8")
            except UnicodeDecodeError:
                line = line_bytes.decode("latin-1", errors="ignore")
            logs.append(line)

    def _kill_tree(proc):
        """杀死整个进程树（包括子进程的子进程）"""
        try:
            if os.name == "nt":
                # Windows: taskkill /T 杀进程树 /F 强制
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                    capture_output=True, timeout=5,
                )
            else:
                # Linux/Mac: 用进程组杀
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    try:
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,  # 隔离 stdin，防止 input() 阻塞
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            cwd=cwd,
            startupinfo=startupinfo,
        )
        start_t = time.time()
        t = threading.Thread(target=stream_reader, args=(process, full_stdout), daemon=True)
        t.start()

        while t.is_alive():
            if time.time() - start_t > timeout:
                _kill_tree(process)
                full_stdout.append(f"\n[Timeout Error] Process killed after {timeout}s")
                break
            time.sleep(0.5)

        t.join(timeout=1)
        # 确保进程已退出并回收资源
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _kill_tree(process)
            process.wait(timeout=3)
        # 关闭 stdout 管道，防止 I/O 线程挂住
        try:
            if process.stdout:
                process.stdout.close()
        except Exception:
            pass
        exit_code = process.poll()
        if exit_code is None:
            exit_code = -1
        stdout_str = "".join(full_stdout)

        if len(stdout_str) > 10000:
            stdout_str = stdout_str[:10000] + f"\n\n[输出已截断：原始输出共 {len(stdout_str)} 字符，已截断至 10000 字符。如需完整输出，请调整程序输出或分页获取。]"

        return {
            "status": "success" if exit_code == 0 else "error",
            "stdout": stdout_str,
            "exit_code": exit_code,
        }
    except Exception as e:
        return {"status": "error", "msg": str(e)}
    finally:
        if code_type == "python" and tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


# 统一路径参数展开 — 像 Shell 处理 ~ 一样
_PATH_ARG_NAMES = frozenset({
    "file_path", "path", "cwd", "output_path",
    "source_path", "dest_path", "workspace_path",
    "document_root", "database_path",
})


def expand_path_args(args: dict) -> None:
    """原地展开 args 中已知路径参数名的 ~/ 前缀。

    像 Shell 处理 ~ 一样，在工具调用入口统一展开。
    只展开以 ~/ 开头的值，不影响其他路径格式。
    """
    for key in _PATH_ARG_NAMES:
        val = args.get(key)
        if isinstance(val, str) and val.startswith("~/"):
            args[key] = os.path.expanduser(val)


def _push_subagent_event(unique_name: str, event_type: str, data: dict):
    """推送子 Agent 事件到 SubagentEventBus，安全降级。

    import 失败（niu_api 未启动）或推送异常均静默降级，
    不影响子 Agent 的工具调用循环。
    """
    try:
        from niu_api.internal.subagent_event_bus import notify_subagent_event_sync
    except ImportError:
        return
    try:
        notify_subagent_event_sync(unique_name, event_type, data)
    except Exception:
        pass


class NiuHandler(BaseHandler):
    """
    Niu Agent 工具处理器

    继承 GenericAgent 的 BaseHandler，实现：
    - 文件操作（read, write, patch）
    - 代码执行（code_run）
    - MCP 工具调用（动态加载）
    """

    def __init__(self, cwd: str = None, mcp_client=None, disk_engine=None):
        self.cwd = cwd or os.getcwd()
        self.mcp_client = mcp_client
        self.disk_engine = disk_engine
        self.current_turn = 0
        self.history_info = []
        self._done_hooks = []
        self._disable_memory_recall = False  # 禁用长期记忆检索（子 Agent 使用）
        self._is_subagent = False

        # ExperienceSummarizer disabled
        # self._experience_context: Optional[ExperienceContext] = None
        # self._experience_summarizer = ExperienceSummarizer()

        # P2-1: 工具调用历史追踪（用于重复检测）
        self._recent_tool_calls: list[tuple] = []
        self._last_prompt_tokens = 0

    # ========== 工具回调机制 ==========

    def tool_before_callback(self, tool_name, args, response):
        """工具调用前：推送状态到前端"""
        # 子 Agent 的工具调用不推送前端状态，走 SubagentEventBus
        if getattr(self, '_is_subagent', False):
            unique_name = getattr(self, '_subagent_unique_name', None)
            if unique_name:
                short_name = tool_name.split('/')[-1] if '/' in tool_name else tool_name
                _push_subagent_event(unique_name, 'tool_status', {'tool_name': short_name, 'status': 'start'})
            return
        try:
            from niu_api.chat import notify_tool_status_sync
            short_name = tool_name.split("/")[-1] if "/" in tool_name else tool_name
            notify_tool_status_sync(short_name, "start")
        except Exception:
            pass  # 推送失败不影响工具调用

    def tool_after_callback(self, tool_name, args, response, ret):
        """工具调用后记录摘要到 history_info"""
        # 子 Agent：推送工具状态到 SubagentEventBus，然后 return（不走主 Agent history tracking）
        if getattr(self, '_is_subagent', False):
            unique_name = getattr(self, '_subagent_unique_name', None)
            if unique_name:
                short_name = tool_name.split('/')[-1] if '/' in tool_name else tool_name
                try:
                    summary = self._auto_generate_summary(tool_name, args, ret)
                except Exception:
                    summary = ''
                _push_subagent_event(unique_name, 'tool_status', {'tool_name': short_name, 'status': 'end', 'summary': summary})
            # 保留重复调用检测（子 Agent 也有死循环风险）
            self._track_tool_call_for_repeat_detection(tool_name, args)
            return
        # 主 Agent：推送工具完成状态到前端
        try:
            from niu_api.chat import notify_tool_status_sync
            short_name = tool_name.split("/")[-1] if "/" in tool_name else tool_name
            notify_tool_status_sync(short_name, "end")
        except Exception:
            pass
        # 跳过同一轮内的多个工具调用（只记录第一个）
        if args.get("_index", 0) > 0:
            return

        # 提取 <summary> 标签（可选优化）
        content = getattr(response, "content", "") if response else ""
        rsumm = re.search(r"<summary>(.*?)</summary>", content, re.DOTALL)

        if rsumm:
            # LLM 提供了高质量摘要
            summary = rsumm.group(1).strip()[:200]
        else:
            # 自动生成结构化摘要（不依赖 <summary> 标签）
            summary = self._auto_generate_summary(tool_name, args, ret)

        self.history_info.append("[Agent] " + summary[:100])
        print(
            f"[ToolSummary] Recorded: {tool_name} -> {summary[:50]}...",
            file=sys.stderr,
            flush=True,
        )

        # 追踪工具调用（用于重复检测）
        self._track_tool_call_for_repeat_detection(tool_name, args)


    def _track_tool_call_for_repeat_detection(self, tool_name: str, args: dict):
        """追踪工具调用用于重复检测。

        存储格式：(tool_name, args_hash) 元组。
        args_hash 是参数的完整哈希，不受截断影响。
        只有工具名和参数完全相同才算重复调用。
        """
        if not hasattr(self, '_recent_tool_calls'):
            self._recent_tool_calls = []

        clean_args = {k: v for k, v in args.items() if not k.startswith("_")}
        args_hash = hash(str(sorted(clean_args.items())))
        self._recent_tool_calls.append((tool_name, args_hash))

        # 保留最近 10 次
        if len(self._recent_tool_calls) > 10:
            self._recent_tool_calls = self._recent_tool_calls[-10:]

    def _auto_generate_summary(self, tool_name: str, args: dict, ret) -> str:
        """自动生成结构化摘要（不依赖 <summary> 标签）"""

        import os
        clean_args = {k: v for k, v in args.items() if not k.startswith("_")}

        # 基于工具类型生成不同格式的摘要
        if tool_name in ("read", "file_read"):
            path = clean_args.get("file_path") if "file_path" in clean_args else clean_args.get("path", "")
            filename = os.path.basename(path) if path else "未知文件"
            return f"读取文件: {filename}"

        elif tool_name == "code_run":
            code_type = clean_args.get("type", "python")
            code = clean_args.get("code", clean_args.get("script", ""))
            preview = code[:30] + "..." if len(code) > 30 else code
            exit_code = ret.get("exit_code", "?") if isinstance(ret, dict) else "?"
            return f"执行{code_type}代码: {preview} (退出码: {exit_code})"

        elif tool_name in ("write", "file_write"):
            path = clean_args.get("file_path") if "file_path" in clean_args else clean_args.get("path", "")
            filename = os.path.basename(path) if path else "未知文件"
            return f"写入文件: {filename}"

        elif tool_name in ("edit", "file_patch"):
            path = clean_args.get("file_path") if "file_path" in clean_args else clean_args.get("path", "")
            filename = os.path.basename(path) if path else "未知文件"
            return f"修改文件: {filename}"

        elif tool_name == "grep":
            pattern = clean_args.get("pattern", "")
            return f"搜索文件: {pattern}"

        elif tool_name.startswith("chat-with-"):
            agent = tool_name.replace("chat-with-", "")
            return f"调用子Agent: {agent}"

        elif tool_name == "disk":
            command = clean_args.get("command", "")
            return f"磁盘: {command[:40]}"

        elif "/" in tool_name:  # MCP 工具（格式：server/name）
            server, name = tool_name.split("/", 1) if "/" in tool_name else ("", tool_name)
            return f"调用MCP工具: {name}"

        elif tool_name == "no_tool":
            # 直接回答用户，从内容提取第一句
            content = getattr(ret, "content", "") if ret else ""
            first_sentence = content.split('。')[0].split('\n')[0]
            return first_sentence[:100] if first_sentence else "直接回答用户问题"

        else:
            # 默认：工具名 + 参数预览
            args_preview = str(clean_args)[:50]
            return f"调用工具: {tool_name}({args_preview})"

    def next_prompt_patcher(self, next_prompt, outcome, turn):
        """周期性警告、全局记忆注入和重复调用检测"""
        # P2-1: 工具重复调用检测
        # 只检测连续 3 次完全相同的调用（工具名 + 参数都相同）
        if turn > 3 and hasattr(self, '_recent_tool_calls'):
            recent_tools = self._recent_tool_calls[-3:]
            if len(recent_tools) == 3:
                # 检查是否连续 3 次调用相同工具且参数完全相同
                if (recent_tools[0] == recent_tools[1] == recent_tools[2]):
                    tool_name = recent_tools[0][0]  # 元组第一个元素是工具名
                    import sys
                    print(
                        f"[P2-1] Detected repeated tool calls: {recent_tools}",
                        file=sys.stderr,
                        flush=True
                    )
                    next_prompt = (
                        f"⚠️ **警告：检测到重复工具调用**\n\n"
                        f"你已连续 3 次以相同参数调用相同工具（{tool_name}）。这通常表示：\n"
                        f"1. 参数可能不正确，工具无法正常执行\n"
                        f"2. 当前方法可能无法解决问题\n"
                        f"3. 需要用户澄清需求\n\n"
                        f"**建议行动：**\n"
                        f"- 检查工具参数是否正确\n"
                        f"- 尝试不同的方法或工具\n"
                        f"- 总结当前进展，说明遇到的困难并直接向用户提问\n\n"
                        f"---\n\n原始提示：{next_prompt}"
                    )

        return next_prompt

    def reset_working_memory(self):
        """重置工作记忆（新会话开始时调用）"""
        self.current_turn = 0
        self._recent_tool_calls = []

    def _get_abs_path(self, path: str) -> str:
        """获取绝对路径（支持 ~ 展开和相对路径解析）"""
        if not path:
            return ""
        expanded = os.path.expanduser(path)
        if os.path.isabs(expanded):
            return expanded
        return os.path.abspath(os.path.join(self.cwd, expanded))

    # ========== 文件操作 ==========

    def do_read(self, args: dict, response) -> StepOutcome:
        """读取文件（新 API，兼容旧参数名 path/start/count；offset 负数 = 读取文件末尾 |offset| 行）"""
        raw_path = args.get("file_path") if "file_path" in args else args.get("path", "")
        file_path = self._get_abs_path(raw_path) if hasattr(self, "cwd") and self.cwd else raw_path
        raw_offset = args.get("offset") if "offset" in args else args.get("start", 1)
        raw_limit = args.get("limit") if "limit" in args else args.get("count", 500)
        offset = int(raw_offset if raw_offset is not None else 1)
        limit = int(raw_limit if raw_limit is not None else 500)

        result = read_file(file_path, offset=offset, limit=limit)
        return StepOutcome(result, next_prompt="")

    def do_write(self, args: dict, response) -> StepOutcome:
        """Write content to file."""
        file_path = self._get_abs_path(args.get("file_path") if "file_path" in args else args.get("path", ""))
        content = args.get("content", "")
        mode = args.get("mode", "overwrite")

        if not file_path:
            return StepOutcome({"status": "error", "msg": "file_path is required"}, next_prompt="")

        result = write_file(file_path, content, mode=mode)
        return StepOutcome(result, next_prompt="")

    def do_edit(self, args: dict, response) -> StepOutcome:
        """Edit file by replacing old_string with new_string."""
        file_path = self._get_abs_path(args.get("file_path") if "file_path" in args else args.get("path", ""))
        old_string = args.get("old_string") if "old_string" in args else args.get("old_content", "")
        new_string = args.get("new_string") if "new_string" in args else args.get("new_content", "")
        replace_all_raw = args.get("replace_all", False)
        replace_all = replace_all_raw if isinstance(replace_all_raw, bool) else str(replace_all_raw).lower() == "true"

        if not file_path:
            return StepOutcome({"status": "error", "msg": "file_path is required"}, next_prompt="")

        result = edit_file(file_path, old_string, new_string, replace_all=replace_all)
        return StepOutcome(result, next_prompt="")

    def do_grep(self, args: dict, response) -> StepOutcome:
        """Search for pattern in files."""
        pattern = args.get("pattern", "")
        path = self._get_abs_path(args.get("path", "."))
        include = args.get("include", "")

        if not pattern:
            return StepOutcome("[GREP] Error: pattern is required", next_prompt="")

        result = grep_search(pattern, path, include)
        return StepOutcome(result, next_prompt="")

    # 向后兼容
    do_file_read = do_read
    do_file_write = do_write
    do_file_patch = do_edit

    # ========== 代码执行 ==========

    def _extract_code_block(self, response, code_type):
        """从回复中提取代码块"""
        content = getattr(response, "content", "") if response else ""
        matches = re.findall(rf"```{code_type}\n(.*?)\n```", content, re.DOTALL)
        return matches[-1].strip() if matches else None

    def do_bash(self, args: dict, response) -> StepOutcome:
        """Execute a shell command."""
        command = args.get("command", "")
        timeout = max(1, min(args.get("timeout", 30), 300))

        if not command:
            return StepOutcome("[Error] Command missing.", next_prompt="")

        code_type = "bash" if os.name != "nt" else "powershell"
        result = code_run(command, code_type=code_type, timeout=timeout, cwd=self.cwd)
        return StepOutcome(result, next_prompt="")

    def do_code_run(self, args: dict, response) -> StepOutcome:
        """执行代码"""
        # 兼容两种参数名
        code = args.get("code") or args.get("script", "")
        code_type = args.get("code_type") or args.get("type", "python")
        timeout = args.get("timeout", 60)

        # 如果参数中没有代码，从回复中提取代码块
        if not code:
            code = self._extract_code_block(response, code_type)
            if not code:
                return StepOutcome(
                    "[Error] Code missing. Use ```{code_type} block or 'script' arg.",
                    next_prompt="",
                )

        result = code_run(code, code_type=code_type, timeout=timeout, cwd=self.cwd)
        return StepOutcome(result, next_prompt="")

    # ========== 无工具调用 ==========

    def do_no_tool(self, args: dict, response) -> StepOutcome:
        """未调用工具时的处理"""
        content = getattr(response, "content", "") if response else ""

        if not content.strip():
            return StepOutcome({}, next_prompt="")

        # 检测只有反引号的响应（LLM 输出异常）
        clean_content = re.sub(r"`+", "", content).strip()
        if not clean_content:
            return StepOutcome(
                {},
                next_prompt=""
            )

        # 检测大段代码但没有工具调用
        code_block_pattern = r"```[a-zA-Z0-9_]*\n[\s\S]{300,}?```"
        m = re.search(code_block_pattern, content)

        if m:
            residual = content.replace(m.group(0), "")
            residual = re.sub(r"<thinking>[\s\S]*?</thinking>", "", residual, flags=re.IGNORECASE)
            residual = re.sub(r"<summary>[\s\S]*?</summary>", "", residual, flags=re.IGNORECASE)
            clean_residual = re.sub(r"\s+", "", residual)

            if len(clean_residual) <= 20:
                return StepOutcome(
                    {},
                    next_prompt="",
                )

        # 正常情况：返回给用户，使用空字符串作为 next_prompt 而不是 None
        return StepOutcome(response, next_prompt="")

    def do_check_subagent_progress(self, args: dict, response) -> StepOutcome:
        """查看异步子 Agent 的进度（最近一轮 LLM 对话）。

        Args:
            args: {"subagent_name": "file-processor-a1b2"}

        Returns:
            StepOutcome(data=进度文本, next_prompt="")
        """
        from .subagent_registry import SubagentRegistry

        subagent_name = args.get("subagent_name", "")
        if not subagent_name:
            return StepOutcome(
                {"status": "error", "msg": "subagent_name is required"},
                next_prompt="",
            )

        instance = SubagentRegistry.get(subagent_name)
        if instance is None:
            return StepOutcome(
                f"子 Agent {subagent_name} 不在运行中（可能已完成或不存在）。",
                next_prompt="",
            )

        if instance.memory_context is None:
            return StepOutcome(
                f"子 Agent {subagent_name} 是同步调用，无进度数据。",
                next_prompt="",
            )

        snap = instance.memory_context.snapshot()

        # 格式化输出
        lines = [
            f"子 Agent: {subagent_name}",
            f"类型: {instance.agent_type}",
            f"当前轮次: {snap['current_turn']}",
            f"最近工具调用: {snap['last_tool_name'] or '（无）'}",
            "最近一轮 LLM 请求（摘要）:",
            f"  {snap['last_llm_request'] or '（无）'}",
            "最近一轮 LLM 回复:",
            f"  {snap['last_llm_response'] or '（无）'}",
        ]

        return StepOutcome("\n".join(lines), next_prompt="")

    def do_ask_user(self, args: dict, response) -> StepOutcome:
        """向用户提问并等待回答（主 Agent 专用——暂停而非退出工具循环）。

        复用子 Agent 的 UserAskRegistry（key="main-agent"），前端主对话流消息式显示提问
        （assistant 消息），用户用主输入框回答——经 /api/chat/session 见缝插针分支
        直接注入 set_answer("main-agent", answer)（不走补充队列）。
        停止按钮：request_stop_all_subagents 补 set_answer("main-agent", TERMINATED_SIGNAL)
        （见 Task 1 Step 8 停止接线）。
        """
        from agent.ask_user import TERMINATED_SIGNAL, UNAVAILABLE_SIGNAL, _ASK_TIMEOUT, get_user_ask_registry

        # P2-1：子 Agent 运行时守卫——ask_user 仅主 Agent 可用
        # （防子 Agent 幻觉调用劫持 "main-agent" future——chat-with 幻觉先例存在）
        if getattr(self, '_is_subagent', False):
            return StepOutcome(
                {"status": "error", "msg": "ask_user 仅主 Agent 可用"},
                next_prompt="",
            )

        question = args.get("question", "")
        if not question:
            return StepOutcome(
                {"status": "error", "msg": "question is required"},
                next_prompt="",
            )

        # R6-A P1/P2：/stop 落在推送/register 前窗口——推送之前检查全局停止标志
        # （轮末 clear_stop() 清除，天然轮次边界，无粘滞残留）
        from agent.runner import is_stop_requested
        if is_stop_requested():
            return StepOutcome(
                "[ask_user 已终止] 停止指令已在提问前到达。请基于现有信息继续推进，或用 @end 结束当前任务。",
                next_prompt="",
            )

        # 1. Electron SSE 推送（主对话流消息式显示提问）；失败/无订阅者不静默——置 electron_pushed=False
        #    （R2-P1-4：_sync_broadcast 只把事件放队列，无订阅者（窗口关闭/SSE 断连）时
        #    提问永不显示——必须检查 _event_subscribers，否则静默阻塞 600s）
        electron_pushed = False
        try:
            from niu_api.chat import _main_loop, _sync_broadcast, _event_subscribers
            if _main_loop and not _main_loop.is_closed() and _event_subscribers:
                event = {"type": "ask_user", "content": question}
                try:
                    _main_loop.call_soon_threadsafe(_sync_broadcast, event)
                    electron_pushed = True
                except RuntimeError:
                    # R3-B P3：is_closed() 检查后 loop 恰好关闭（竞态）——置 electron_pushed=False 走错误分支
                    electron_pushed = False
        except ImportError:
            pass
        # 2. IM 抽象通道：独立推送（不依赖 electron_pushed——双端场景（Electron 窗口开 + 飞书 IM）
        #    飞书也要看到问题；_cid 存在即推）。终结当前回复卡片 → 问题作独立消息
        #    （同步线程安全，gateway 为 executor 线程设计）。
        #    终结用 send_sync 而非 notify_stream：流式问题会被回复卡 accumulated 吞掉且不即时显示；
        #    独立 SEND 消息即时显示问题（用户立即看到），且不复写/污染回复卡。
        im_pushed = False
        try:
            from agent.runner import get_runner
            from niu_api.channel.gateway import get_im_gateway
            from agent.at_message_parser import strip_at_messages
            _runner = get_runner()
            _cid = getattr(_runner, "_current_channel_id", "")
            _gw = get_im_gateway()
            if _cid and _gw and _gw.is_connected:
                # 1) 终结当前回复卡片（content 空 → adapter 用 state.accumulated 终结）→ 记 ask_finalized 标记
                #    ask_finalize=True（v11：与 route_out 重复 SEND 区分）
                #    pop_reply_to=False：保留群聊回复目标（R2-B-P2 + R8-A-P3：问题 send 也 False，防卡 B 群聊不串联）
                _gw.send_sync(_cid, "", pop_reply_to=False, ask_finalize=True)
                # 2) 问题作独立消息发（无卡片 state + ask_finalize → send_markdown，不清标记供 route_out 判重）
                _gw.send_sync(_cid, f"{strip_at_messages(question)}", pop_reply_to=False, ask_finalize=True)
                im_pushed = True  # 必须置位——无 Electron 订阅者时保证 pushed=True → 注册 future（漏置 = 提前 return + 回答无法注入）
            elif _gw and _gw.is_connected and getattr(_runner, "_request_source", "") in ("scheduler", "ha-watcher"):
                # elif 守卫含 _gw and _gw.is_connected（防 _gw=None 时 push_target AttributeError）
                # 注："ha-watcher" 是死值——_process_single 归一化把 scheduler/ha-watcher 都映射为
                #   "scheduler"，_request_source 永不等于 "ha-watcher"——保留元组写法无害（闸门功能正确）
                # 兜底：定时任务场景无 IM 继承 → 推最近会话 + 临时设 channel（回答可注入）
                # target 仅依赖 push_target——get_im_channel() 回退在 elif 内逻辑不可达
                #   （elif 成立 ⇔ _current_channel_id 空 ⇔ _im_channel_id 空，chat() 继承逻辑）——不写防御性死代码
                target = _gw.push_target  # 公开线程安全属性（带 _lock）
                if target:
                    # 设置 channel 使 gateway._on_msg 注入守卫（_ask_cid and channel_id==_ask_cid）命中
                    _runner._current_channel_id = target
                    _runner._im_channel_id = target  # 同步——chat_queue 的 get_im_channel() 终结目标与建卡渠道一致
                    _gw.send_sync(target, "", pop_reply_to=False, ask_finalize=True)
                    _gw.send_sync(target, f"{strip_at_messages(question)}", pop_reply_to=False, ask_finalize=True)
                    im_pushed = True  # 必须置位——无 Electron 时保证 pushed=True → 注册 future
                    # chat() 末尾仍会重置 _current_channel_id——不需手动恢复
                else:
                    logger.warning("[AskUser] IM fallback skipped: no push_target and no im_channel for scheduler ask")  # 可观测性
        except Exception:
            im_pushed = False   # v10（R7-B-P3）：保留 try/except 优雅降级（send_sync 异常 → 仅 IM 失败，不影响 electron_pushed）
        # 双通道任一成功即视为可显示——都失败才走无法显示分支（去掉 if not pushed 门控：
        # 双端场景 Electron 推成功不再跳过 IM，飞书也能看到问题）
        pushed = electron_pushed or im_pushed
        if not pushed:
            return StepOutcome(
                "[ask_user 无法显示] 前端事件通道不可用或无订阅者，无法向用户提问。请基于现有信息继续推进，或用 @end 结束。",
                next_prompt="",
            )

        # 2. 注册 future 等待用户回答（复用子 Agent 注册表，key="main-agent"）
        registry = get_user_ask_registry()
        future = registry.register("main-agent")
        try:
            # R7-A/B P2：register 后 wait 前复查——/stop 落在"预检→register"毫秒窗口时，
            # set_answer 对未注册 future 是 no-op（返回 False），复查捕获（finally unregister 覆盖早退）
            if is_stop_requested():
                return StepOutcome(
                    "[ask_user 已终止] 停止指令已在提问建立前到达。请基于现有信息继续推进，或用 @end 结束当前任务。",
                    next_prompt="",
                )
            answer = future.wait(timeout=_ASK_TIMEOUT)
            # 3. 按结果返回（终止/不可用/超时/回答）
            if answer == TERMINATED_SIGNAL:
                # P3-9：非 verbose 主路径（工具循环）中 /stop 经 run_interruptibly 放弃等待、
                # 结果被调用方丢弃（agent_loop 走 STOPPED 退出），不会到达本分支；
                # 本分支仅在 verbose 工具调用路径可达（停止信号经 set_answer 注入时）
                return StepOutcome(
                    "[ask_user 已终止] 用户未回答（停止指令）。请基于现有信息继续推进，或用 @end 结束当前任务。",
                    next_prompt="",
                )
            if answer == UNAVAILABLE_SIGNAL:
                # R4-A P1-4：前端无可渲染窗口时 main.js 回执此标记——不静默阻塞 600s
                return StepOutcome(
                    "[ask_user 无法显示] 前端无可用窗口显示提问（可能已关闭）。请基于现有信息继续推进，或用 @end 结束当前任务。",
                    next_prompt="",
                )
            if answer is None:
                return StepOutcome(
                    "[ask_user 超时] 用户长时间未回答。你可以继续推进、用 @end 结束，或稍后再问。",
                    next_prompt="",
                )
            return StepOutcome(f"[user 回答] {answer}", next_prompt="")
        finally:
            # 防 is_waiting 脏：超时/终止/回答后都移除 future（镜像子 Agent _ask_user_impl L1354-1356）
            # P3-4：unregister 前校验 future 身份——仅当注册表中仍是本次注册的 future 才移除
            # （防并发/重复 register 把新 future 误删；set_answer 已 pop 时 get_future 返回 None，跳过无害）
            if registry.get_future("main-agent") is future:
                registry.unregister("main-agent")

    def _sync_get_messages(self):
        """同步获取消息列表 — 复用 runner 的桥接方法"""
        from .runner import get_runner
        runner = get_runner()
        if runner is None:
            return []
        return runner._sync_get_messages()

    def _build_journal_task_for_handler(self, original_task: str) -> tuple:
        """为主Agent调用 journal-agent 构建增量消息 task。"""
        from niu_api.compat import (
            _build_incremental_msg_text,
            _build_journal_task,
            _build_plain_history,
        )

        # 报告生成指令不替换为增量消息 task — journal-agent 自己读 journal.md 聚合
        # 返回四元组 (task, history=[], idx_to_id={}, msg_ids=[])，与主返回路径结构一致
        report_keywords = ("周报", "月报", "季报", "年报")
        if any(kw in original_task for kw in report_keywords):
            return original_task, [], {}, []

        # 1. 读取游标
        journal_cursor_path = Path.home() / ".niu" / "last_journal.json"
        last_journal_id = ""
        if journal_cursor_path.exists():
            try:
                cursor_data = json.loads(journal_cursor_path.read_text(encoding="utf-8"))
                last_journal_id = cursor_data.get("last_journal_id", "")
            except Exception:
                pass

        # 2. 获取消息列表
        messages = self._sync_get_messages()
        if not messages:
            return original_task, [], {}, []

        # 3. 游标为空且消息过多时，限制为最近200条（防止全量嵌入超限）
        if not last_journal_id and len(messages) > 200:
            from loguru import logger
            logger.warning(f"[Handler] Journal cursor empty, {len(messages)} messages total, limiting to last 200")
            messages = messages[-200:]

        # 4. 计算 token
        msg_tokens = []
        try:
            from agent.token_calculator import TokenCalculator
            calc = TokenCalculator.get()
            for msg in messages:
                try:
                    t = calc.count_message_single(msg.role, msg.content or "", tool_calls=msg.tool_calls)
                except Exception:
                    t = max(1, len(msg.content or "") // 2) + 4
                msg_tokens.append(t)
        except ImportError:
            msg_tokens = [max(1, len(msg.content or "") // 2) + 4 for msg in messages]

        # 5. 收集增量消息 ID（不再用 journal_msg_text 构造 task）
        journal_msg_ids = []
        _ = _build_incremental_msg_text(
            messages, last_journal_id, journal_msg_ids, msg_tokens
        )

        # 无增量消息早返回：四元组（history 空 + idx_to_id 空字典 + msg_ids 空列表）
        if not journal_msg_ids:
            return original_task, [], {}, []

        # 6. 构造增量 history + idx_to_id 映射（按 journal_msg_ids 过滤，保留双游标区间内的消息）
        _id_set = set(journal_msg_ids)
        journal_incremental_msgs = [m for m in messages if (getattr(m, "id", "") or "") in _id_set]
        journal_history, journal_idx_to_id = _build_plain_history(journal_incremental_msgs)

        # 7. 返回 (task 纯指令, history 逐条, idx_to_id 映射, journal_msg_ids)
        return _build_journal_task(), journal_history, journal_idx_to_id, journal_msg_ids

    def _update_journal_cursor(self, journal_result: str, journal_msg_ids: list, journal_idx_to_id: dict | None = None):
        """从 journal-agent 结果中提取游标并更新 last_journal.json

        仿 context-manager 简易 ID 映射：解析子 Agent 输出的 processed_up_to=N，
        查 journal_idx_to_id[N] 得到真实 UUID 更新游标；未找到则回退到 msg_ids[-1]（兜底）。
        """
        from datetime import datetime

        from niu_api.compat import (
            _extract_overflow_info,
            _flock,
            _funlock,
            _incomplete_reason,
            _is_subagent_failure,
            _is_subagent_incomplete,
            _is_subagent_overflow,
            _parse_processed_up_to,
        )

        # 在获取文件锁之前读取消息列表 — 避免在锁内调用 _sync_get_messages() 导致死锁
        messages = self._sync_get_messages()
        msg_id_set = {getattr(m, "id", "") for m in messages}

        journal_cursor_path = Path.home() / ".niu" / "last_journal.json"
        lock_path = journal_cursor_path.with_suffix(".lock")
        # 文件锁保护 — 防止与 tidy 管道并发读写
        with open(lock_path, 'w') as lock_f:
            _flock(lock_f)
            try:
                # 读取当前游标（在锁内读取，保证原子性）
                last_journal_id = ""
                if journal_cursor_path.exists():
                    try:
                        cursor_data = json.loads(journal_cursor_path.read_text(encoding="utf-8"))
                        last_journal_id = cursor_data.get("last_journal_id", "")
                    except Exception:
                        pass

                new_journal_id = last_journal_id

                # 游标推进：failure/overflow/incomplete→不动；否则解析 processed_up_to=N 查映射，兜底 msg_ids[-1]
                if _is_subagent_overflow(journal_result) or _is_subagent_incomplete(journal_result) or _is_subagent_failure(journal_result):
                    if _is_subagent_failure(journal_result):
                        logger.warning(f"[Journal] failure: {journal_result[:200]} — cursor not advanced")
                    elif _is_subagent_incomplete(journal_result):
                        logger.warning(f"[Journal] incomplete ({_incomplete_reason(journal_result)}) — cursor not advanced")
                    else:
                        overflow_info = _extract_overflow_info(journal_result)
                        logger.warning(f"[Journal] overflow: {overflow_info.get('turns_completed', 0)} turns")
                    # failure/overflow/incomplete 时游标不动
                    new_journal_id = last_journal_id
                else:
                    _processed_idx = _parse_processed_up_to(journal_result)
                    if _processed_idx is not None and journal_idx_to_id and _processed_idx in journal_idx_to_id:
                        new_journal_id = journal_idx_to_id[_processed_idx]
                        logger.info(f"[Journal] Cursor advanced per processed_up_to={_processed_idx} -> {new_journal_id}")
                    elif journal_msg_ids:
                        new_journal_id = journal_msg_ids[-1]  # 兜底
                        logger.info(f"[Journal] Cursor fallback to range end: {new_journal_id}")
                    else:
                        new_journal_id = last_journal_id

                # 校验游标（二次校验，与 compat.py 一致）
                if new_journal_id and new_journal_id not in msg_id_set:
                    new_journal_id = last_journal_id
                    if new_journal_id and new_journal_id not in msg_id_set:
                        new_journal_id = ""

                # 写入
                if new_journal_id:
                    journal_cursor_path.parent.mkdir(parents=True, exist_ok=True)
                    journal_cursor_path.write_text(json.dumps({
                        "last_journal_id": new_journal_id,
                        "last_journal_at": datetime.now().isoformat(),
                    }, ensure_ascii=False, indent=2), encoding="utf-8")
            finally:
                _funlock(lock_f)

    def _call_subagent_gen(self, agent_name: str, args: dict):
        """调用子 Agent（生成器版本）— 同步/异步分流"""
        import json  # E4-14：函数顶部绑定——事件块内 import json 使 json 成为函数局部名，先于其执行引用会 UnboundLocalError

        from niu_api.compat import _incomplete_reason, _is_subagent_incomplete

        from .subagent import _dispatch_async_subagent, call_subagent, get_subagent_config

        task = args.get("task", "")
        async_mode = args.get("async_mode", False)
        answer = args.get("answer")
        unique_name_arg = args.get("unique_name")

        # journal-agent 特殊处理：构建增量消息 task + history + idx_to_id，与 tidy 管道一致
        journal_msg_ids_for_cursor = []  # 默认空列表，仅 journal-agent 时填充
        _journal_history = []  # 默认空 history，仅 journal-agent 时填充
        _journal_idx_to_id = {}  # 默认空映射，仅 journal-agent 时填充
        if agent_name == "journal-agent":
            task, _journal_history, _journal_idx_to_id, journal_msg_ids_for_cursor = self._build_journal_task_for_handler(task)

        # 获取完整的 LLM 配置（从全局 runner）
        from .runner import get_runner

        runner = get_runner()
        if runner is None:
            yield StreamEvent("system", "[System] Runner not initialized\n")
            return StepOutcome(
                {"status": "error", "msg": "Runner not initialized"},
                next_prompt="",
            )

        # 直接传递完整配置（而不是挑选字段）
        llm_config = runner.llm_config.copy()  # 复制一份，避免修改原始配置

        # 阶段二：异步分流
        if async_mode:
            # 检查该子 Agent 是否支持异步
            agent_config = get_subagent_config(agent_name)
            if not agent_config.get("allowAsync", False):
                return StepOutcome(
                    {"status": "error", "msg": f"子 Agent {agent_name} 不支持异步调用（allowAsync 未启用）"},
                    next_prompt="",
                )

            # 硬阻止：event-manager 不允许异步调用（异步路径跳过定时任务入库验证，
            # 会导致定时任务可能未真正入库主 Agent 不知情）
            if agent_name == "event-manager":
                return StepOutcome(
                    {"status": "error", "msg": "event-manager 不支持异步调用（定时任务入库验证需要在同步路径执行）"},
                    next_prompt="",
                )

            unique_name, confirmation = _dispatch_async_subagent(
                agent_name=agent_name,
                task=task,
                llm_config=llm_config,
                mcp_client=self.mcp_client,
            )
            if unique_name is not None:
                # 推送 subagent_started 事件到主 Agent SSE 流
                # 不走 notify_new_message_sync（它需要 message_id），直接调 _sync_broadcast
                try:
                    from niu_api.chat import _main_loop, _sync_broadcast
                    if _main_loop and not _main_loop.is_closed():
                        event = {
                            'type': 'subagent_started',
                            'unique_name': unique_name,
                            'agent_name': agent_name,
                            'is_sync': False,
                        }
                        _main_loop.call_soon_threadsafe(_sync_broadcast, event)
                except ImportError:
                    pass
                yield StreamEvent("tool_marker", f"[SubAgent] 异步派出：{confirmation[:100]}\n")
                return StepOutcome({"status": "success", "result": confirmation}, next_prompt="")
            else:
                yield StreamEvent("system", confirmation)
                return StepOutcome({"status": "error", "msg": confirmation}, next_prompt="")

        # 同步子 Agent：先 pre_register 创建 ring buffer，再推送 subagent_started
        # 仅首次调用（新任务）执行；恢复路径（answer is not None）跳过：ring buffer 已存在、tab 已创建
        if not answer:
            # 【问题一修复】Early pre_register: creates ring buffer BEFORE subagent_started is queued,
            # so has_subagent() returns True when frontend connects SSE.
            # register() inside call_subagent will call pre_register again (no-op, idempotency guard).
            try:
                from niu_api.internal.subagent_event_bus import pre_register
                pre_register(agent_name)
            except ImportError:
                pass
            # 推送 subagent_started 事件到主 Agent SSE 流
            try:
                from niu_api.chat import _main_loop, _sync_broadcast
                if _main_loop and not _main_loop.is_closed():
                    event = {
                        'type': 'subagent_started',
                        'unique_name': agent_name,
                        'agent_name': agent_name,
                        'is_sync': True,
                    }
                    _main_loop.call_soon_threadsafe(_sync_broadcast, event)
            except ImportError:
                pass

        # 同步路径
        try:
            yield StreamEvent("tool_marker", f"[SubAgent] Calling {agent_name}...\n")
            _history = None
            if agent_name == "journal-agent" and _journal_history:
                _history = _journal_history

            result = call_subagent(
                agent_name=agent_name,
                task=task,
                llm_config=llm_config,
                mcp_client=self.mcp_client,
                history=_history,
                **({"context_fifo_threshold": 0} if (agent_name == "journal-agent" and _journal_history) else {}),
                answer=answer,
                answer_unique_name=(unique_name_arg or agent_name) if answer else None,
            )
            # SUBAGENT_ERROR: 子 Agent LLM 错误，返回 error 让主 Agent 知道失败
            if result and result.startswith("SUBAGENT_ERROR:"):
                error_msg = result[len("SUBAGENT_ERROR:"):]
                return StepOutcome(
                    {"status": "error", "msg": error_msg},
                    next_prompt=f"子Agent调用失败：{error_msg}",
                )

            # 剥除 COMPACT_TRUNCATED: 前缀（截断信号已被 context-manager 路径处理，主 Agent 只需内容）
            if result and result.startswith("COMPACT_TRUNCATED:"):
                result = result[len("COMPACT_TRUNCATED:"):]

            # journal-agent 特殊处理：更新游标（必须用原始 result——incomplete/overflow
            # 判定基于原始 JSON；转换只作用于返回 LLM 的副本，见下方 display_result）
            if agent_name == "journal-agent" and journal_msg_ids_for_cursor:
                self._update_journal_cursor(result, journal_msg_ids_for_cursor, _journal_idx_to_id)

            # incomplete JSON → 自然语言提示（只作用于返回 LLM 的副本，游标判定已用原始 result）
            display_result = result
            if result and result.strip().startswith("{") and _is_subagent_incomplete(result):
                display_result = (
                    f"子Agent未完成任务（{_incomplete_reason(result)}），已保留进度；"
                    f"请决定是否让子Agent继续处理。"
                )

            # E4-14：提示词降级标注（展示层注入）——call_subagent 内已在非 JSON 结果拼接
            # [子 Agent 提示词降级: ...]；JSON 结构化结果保持原样（游标/JSON 消费不受影响），
            # 降级事实经 thread-local 标记旁路在此补注到 display_result（返回 LLM 的展示副本，不解析）。
            # E4 T3 P1：同步链 handler 与 call_subagent 同一执行线程——threading.local 可读；
            # 异步 worker 线程的标记不在此读取（防并发串扰）。
            from . import subagent as _subagent_mod
            _degraded_reason = _subagent_mod._get_subagent_prompt_degraded_reason()
            if _degraded_reason and result and result.strip().startswith("{"):
                try:
                    json.loads(result.strip())
                except json.JSONDecodeError:
                    pass  # 非可解析 JSON——call_subagent 内已拼接，不重复补注
                else:
                    display_result = f"{display_result}\n[子 Agent 提示词降级: {_degraded_reason}]"

            # 验证结果：检查 event-manager 是否真正创建了任务
            if agent_name == "event-manager" and ("提醒" in task or "定时" in task or "提醒我" in task):
                # E4-11：验证失败原因（None=验证成功——display_result 保持原样不注入；
                # 非 None=失败——注入 chat-with 结果流，主 Agent 下一轮可见验证失败）
                verify_fail_reason = None
                try:
                    import json
                    import sqlite3
                    from pathlib import Path

                    memory_path = Path.home() / ".niu" / "memory.json"
                    if memory_path.exists():
                        memory = json.loads(memory_path.read_text(encoding="utf-8"))
                        workspace = memory.get("workspace", {}).get("path")
                        if workspace:
                            db_path = str(Path(workspace) / "scheduled_tasks.db")
                            if Path(db_path).exists():
                                try:
                                    with sqlite3.connect(db_path) as conn:
                                        cursor = conn.cursor()
                                        cursor.execute("""
                                            SELECT id, content, status, scheduled_at
                                            FROM scheduled_tasks
                                            ORDER BY created_at DESC
                                            LIMIT 1
                                        """)
                                        latest_task = cursor.fetchone()
                                except sqlite3.Error as e:
                                    yield StreamEvent("system", f"[SubAgent] ⚠ Database error: {e}\n")
                                    latest_task = None
                                    verify_fail_reason = f"数据库错误: {e}"

                                if latest_task:
                                    yield StreamEvent("tool_marker", f"[SubAgent] ✓ Verified task in database: {latest_task[1]} at {latest_task[3]}\n")
                                else:
                                    yield StreamEvent("system", "[SubAgent] ⚠ Warning: No task found in database\n")
                                    verify_fail_reason = verify_fail_reason or "数据库中无任务记录"
                            else:
                                # P3-2：第四分支——数据库文件缺失（原静默通过 → 验证失败可见化）
                                yield StreamEvent("system", "[SubAgent] ⚠ Warning: 数据库不存在，无法验证任务\n")
                                verify_fail_reason = "数据库不存在，无法验证任务"
                except Exception as e:
                    yield StreamEvent("system", f"[SubAgent] Warning: Failed to verify task: {e}\n")
                    verify_fail_reason = f"验证异常: {e}"

                # E4-11：验证失败 → 注入 chat-with 结果流（display_result——主 Agent 下一轮可见
                # 验证失败；不走 next_prompt——防 test_working_memory_removal 白名单回归）；
                # 成功分支保持丢弃（display_result 原样）
                if verify_fail_reason:
                    # P3-1：验证失败原因长度上限——保尾截断（200 字符），防异常文本
                    # （如超长数据库错误消息）挤占 tool_marker 200 截断窗口
                    if len(verify_fail_reason) > 200:
                        verify_fail_reason = "..." + verify_fail_reason[-197:]
                    display_result = f"[event-manager 任务验证失败：{verify_fail_reason}]\n{display_result}"

            yield StreamEvent("tool_marker", f"[SubAgent] {agent_name} completed: {display_result[:200] if len(display_result) > 200 else display_result}\n")
            return StepOutcome(
                {"status": "success", "result": display_result},
                next_prompt=""
            )
        except Exception as e:
            yield StreamEvent("system", f"[SubAgent] Error: {e}\n")
            # 【问题 2d 修复】推送 subagent_error 事件到 SubagentEventBus（前端 tab 显示错误状态）
            _push_subagent_event(agent_name, 'subagent_error', {'content': str(e)[:2000]})
            return StepOutcome(
                {"status": "error", "msg": str(e)}, next_prompt=""
            )
        finally:
            # 【问题 2a/2b/2c 修复】仅首次调用时清理 pre_register 创建的 ring buffer
            # 恢复路径（answer is not None）的清理由 call_subagent 内部 finally 负责
            if not answer:
                from .subagent_registry import SubagentRegistry
                instance = SubagentRegistry.get(agent_name)
                if instance is not None:
                    # 【问题 2a 修复】挂起时不清理（ring buffer 保留供恢复使用）
                    state = getattr(instance, 'state', None)
                    if state != 'waiting_for_answer':
                        try:
                            from niu_api.internal.subagent_event_bus import close
                            close(agent_name)
                        except ImportError:
                            pass
                else:
                    # 【问题 2c 修复】instance is None：call_subagent 未 register 或在 register 前异常
                    # 但 pre_register 可能已创建 ring buffer + subagent_started 已广播
                    # 必须清理，否则 ring buffer 泄漏 + 前端 tab 卡死
                    # 【问题 2e 修复】场景 1/8（正常完成/@end 退出）call_subagent 内部已 close，
                    # _closed set 标记后 has_subagent() 返回 False（已排除已 close 的），自动去重。
                    try:
                        from niu_api.internal.subagent_event_bus import has_subagent, close
                        if has_subagent(agent_name):
                            close(agent_name)
                    except ImportError:
                        pass

    # ========== MCP 工具（动态） ==========

    # Backward compatibility aliases: old tool names → new lightrag-server tools
    _TOOL_ALIASES = {
        "file_read": "read",
        "file_write": "write",
        "file_patch": "edit",
    }

    def dispatch(self, tool_name: str, args, response, index=0):
        """分发工具调用（支持 MCP 工具）- 必须是生成器

        E1 统一异常兜底（2026-08-15）：整体包裹——do_* 参数畸形 / disk_engine.execute
        裸调用 / chat-with 异步分支 / 回调穿透抛出的 Exception 转 TOOL_ERROR error dict
        （进 StepOutcome.data → tool 消息 → LLM 可见可自纠），工具循环不死亡、会话不中止。
        BaseException（KeyboardInterrupt/SystemExit/CancelledError）保留穿透（停止语义兼容）。
        """
        try:
            return (yield from self._dispatch_impl(tool_name, args, response, index))
        except Exception as e:
            # 错误文本构造——format_error 调用处内层 try/except 兜底坏 __str__（防二次抛异常逃逸 except 块）
            try:
                err_detail = format_error(e)  # f"{type(e).__name__}: {e} ({fname}:{f.lineno})"
            except Exception:
                err_detail = f"{type(e).__name__}: <unprintable>"
            err_msg = f"Tool {tool_name} failed: {err_detail}"
            # 截断保尾：前 _TOOL_ERROR_MAX-(len('...')+_TOOL_ERROR_TAIL) + '...' + 尾 _TOOL_ERROR_TAIL = 总 ≤500，
            # 保留 (fname:lineno) 位置信息（供区分 harness 伪装与真实工具错误）
            if len(err_msg) > _TOOL_ERROR_MAX:
                err_msg = err_msg[: _TOOL_ERROR_MAX - (len("...") + _TOOL_ERROR_TAIL)] + "..." + err_msg[-_TOOL_ERROR_TAIL:]

            # 错误路径镜像 tool_after_callback 职责（复制 L520-566 段结构）：
            # ① 重复调用检测——失败工具同参重试时重复检测警告仍触发（防 LLM 自旋）。
            # args 非 dict（如字符串参数）时静默跳过——可接受降级（重复检测仅对 dict 参数有意义）
            try:
                self._track_tool_call_for_repeat_detection(tool_name, args)
            except Exception:
                pass
            # ② 工具状态 end 推送——失败工具不滞留前端状态（status 只用 'start'/'end'）
            try:
                short_name = tool_name.split('/')[-1]
                if getattr(self, "_is_subagent", False):
                    unique_name = getattr(self, "_subagent_unique_name", None)
                    if unique_name:
                        _push_subagent_event(unique_name, 'tool_status',
                                             {'tool_name': short_name, 'status': 'end', 'summary': 'tool error'})
                else:
                    from niu_api.chat import notify_tool_status_sync
                    notify_tool_status_sync(short_name, 'end')
            except Exception:
                pass

            # system 事件与 msg 均复用 err_detail（已兜底）——不再直接调 str(e)。
            # 截断策略：msg 保尾为权威信息源（LLM 消费，含位置信息）；system 事件仅前端展示，截头即可
            yield StreamEvent("system", f"[Tool Error] {tool_name}: {err_detail[:_TOOL_ERROR_MAX]}\n")
            return StepOutcome(
                {"status": "error", "error_code": "TOOL_ERROR", "msg": err_msg},
                next_prompt="",
            )

    def _dispatch_impl(self, tool_name: str, args, response, index=0):
        """dispatch 原始函数体（被外层统一异常包裹兜底）。"""

        # 统一路径参数展开（~/ → 实际 home 目录）
        expand_path_args(args)

        # Apply backward compatibility aliases
        resolved_name = self._TOOL_ALIASES.get(tool_name, tool_name)
        if resolved_name != tool_name:
            logger.debug(f"Tool alias: {tool_name} → {resolved_name}")
            tool_name = resolved_name

        # Auto-resolve bare tool names: if no "/" prefix, try MCP server prefixes
        if "/" not in tool_name:
            from agent.tool_registry import get_registry
            registry = get_registry()
            for server_name in registry._server_tools:
                full_name = f"{server_name}/{tool_name}"
                if full_name in registry._schemas:
                    logger.debug(f"Auto-resolved bare tool: {tool_name} → {full_name}")
                    tool_name = full_name
                    break

        # 先检查 chat-with-* 子 Agent 调用（通配路由）
        if tool_name.startswith("chat-with-"):
            agent_name = tool_name[len("chat-with-"):]
            # 系统自动管理的子Agent，禁止手动调用
            blocked_subagents = {"context-manager", "entity-extractor", "dream-evolver"}  # 由 auto-tidy 管道自动调用，禁止主Agent手动触发
            if agent_name in blocked_subagents:
                return StepOutcome(
                    {"status": "error", "message": f"子Agent {agent_name} 已由系统自动管理，不可手动调用"},
                    next_prompt=""
                )
            # 存在性检查：不存在的子 Agent 给主 Agent 明确反馈（不静默/不幻觉执行）
            _ok, _err = _check_chat_with_agent_exists(agent_name)
            if not _ok:
                return StepOutcome(
                    {"status": "error", "msg": _err},
                    next_prompt=f"子Agent {agent_name} 不存在，无法调用。{_err}",
                )
            args = {**args, "_index": index}
            yield from try_call_generator(
                self.tool_before_callback, tool_name, args, response
            )
            ret = yield from try_call_generator(self._call_subagent_gen, agent_name, args)
            try:
                _ = yield from try_call_generator(
                    self.tool_after_callback, tool_name, args, response, ret
                )
            except Exception:
                pass  # callback 失败不影响主流程（工具已成功，防 wrapper 误标 TOOL_ERROR）
            return ret

        # 再检查内置工具（工具名中的 - 转换为 _）
        method_name = f"do_{tool_name.replace('-', '_')}"
        if hasattr(self, method_name):
            # 直接调用方法，不委托给 super（因为 super 会用原始 tool_name 查找）
            args = {**args, "_index": index}
            yield from try_call_generator(
                self.tool_before_callback, tool_name, args, response
            )
            ret = yield from try_call_generator(getattr(self, method_name), args, response)
            try:
                _ = yield from try_call_generator(
                    self.tool_after_callback, tool_name, args, response, ret
                )
            except Exception:
                pass  # callback 失败不影响主流程（工具已成功，防 wrapper 误标 TOOL_ERROR）
            return ret

        # 检查 disk 虚拟磁盘命令
        if tool_name == "disk" and self.disk_engine is not None:
            command = args.get("command", "")
            yield from try_call_generator(
                self.tool_before_callback, tool_name, args, response
            )
            disk_result = self.disk_engine.execute(command)
            if disk_result.action == "EXECUTE":
                # 返回原始 MCP 结果，保留 status 检查
                result = disk_result.raw_result
                # Map /dir/tool → server-name/tool using DiskConfig
                real_tool_name = tool_name
                parts = disk_result.tool_path.strip("/").split("/", 1)
                if len(parts) == 2:
                    dir_name, tool = parts
                    server = self.disk_engine.config.get_server_by_dir(dir_name)
                    if server is not None:
                        real_tool_name = f"{server.server_name}/{tool}"
                # 截断由 agent_loop 统一关口处理（dispatch 返回后）
                try:
                    _ = yield from try_call_generator(
                        self.tool_after_callback, real_tool_name,
                        args, response, result
                    )
                except Exception:
                    pass  # callback 失败不影响主流程（工具已成功，防 wrapper 误标 TOOL_ERROR）
                # Determine success: dict with status != error, or any non-dict result (str/list)
                is_success = (
                    isinstance(result, dict) and result.get("status") not in ("error", None)
                ) or not isinstance(result, dict)
                if is_success:
                    # status 为 ok/success 表示任务完成，提示汇报；其他非 error 状态（need_category 等）让 LLM 自行判断
                    if isinstance(result, dict) and result.get("status") in ("ok", "success"):
                        return StepOutcome(result, next_prompt="")
                    else:
                        return StepOutcome(result, next_prompt="")
                else:
                    return StepOutcome(result, next_prompt="")
            elif disk_result.action == "ERROR":
                # 参数/执行错误 → 提示修正
                result = disk_result.text
                try:
                    _ = yield from try_call_generator(
                        self.tool_after_callback, tool_name, args, response, result
                    )
                except Exception:
                    pass  # callback 失败不影响主流程（工具已成功，防 wrapper 误标 TOOL_ERROR）
                return StepOutcome(result, next_prompt="")
            else:
                # 导航命令 (LIST/READ/HELP/EMPTY) → 继续工作
                result = disk_result.text
                try:
                    _ = yield from try_call_generator(
                        self.tool_after_callback, tool_name, args, response, result
                    )
                except Exception:
                    pass  # callback 失败不影响主流程（工具已成功，防 wrapper 误标 TOOL_ERROR）
                return StepOutcome(result, next_prompt="")

        # 检查 MCP 工具（工具名格式：server/tool）
        if "/" in tool_name:
            try:
                from agent.tool_registry import get_registry

                # 从 ToolRegistry 获取工具函数
                func = get_registry().get(tool_name)

                if func is None:
                    yield StreamEvent("system", f"[MCP Error] Tool not found: {tool_name}\n")
                    return StepOutcome(
                        {"status": "error", "error_code": "TOOL_NOT_FOUND", "msg": f"Tool {tool_name} not found in registry"},
                        next_prompt=""
                    )

                # 直接调用工具函数
                result = func(**args)

                result = _run_coroutine(result)

                yield StreamEvent("tool_marker", f"[MCP] {tool_name} executed\n")

                # 截断由 agent_loop 统一关口处理（dispatch 返回后）

                # 调用 tool_after_callback（工作记忆、重复检测、习惯追踪）
                try:
                    _ = yield from try_call_generator(
                        self.tool_after_callback, tool_name, args, response, result
                    )
                except Exception:
                    pass  # callback 失败不影响主流程

                # Determine success: dict with status != error, or any non-dict result (str/list)
                is_success = (
                    isinstance(result, dict) and result.get("status") not in ("error", None)
                ) or not isinstance(result, dict)
                if is_success:
                    # status 为 ok/success 表示任务完成，提示汇报；其他非 error 状态（need_category 等）让 LLM 自行判断
                    if isinstance(result, dict) and result.get("status") in ("ok", "success"):
                        return StepOutcome(result, next_prompt="")
                    else:
                        return StepOutcome(result, next_prompt="")
                else:
                    # 需要进一步处理，返回anchor prompt
                    return StepOutcome(result, next_prompt="")
            except Exception as e:
                yield StreamEvent("system", f"[MCP Error] {tool_name}: {e}\n")
                return StepOutcome(
                    {"status": "error", "msg": str(e)}, next_prompt=""
                )

        # Check ToolRegistry for bare tool names (no "/" prefix, e.g. "lightrag-query")
        from agent.tool_registry import get_registry

        func = get_registry().get(tool_name)
        if func is not None:
            try:
                result = func(**args)

                result = _run_coroutine(result)

                yield StreamEvent("tool_marker", f"[MCP] {tool_name} executed\n")

                # 截断由 agent_loop 统一关口处理（dispatch 返回后）

                try:
                    _ = yield from try_call_generator(
                        self.tool_after_callback, tool_name, args, response, result
                    )
                except Exception:
                    pass

                # Determine success: dict with status != error, or any non-dict result (str/list)
                is_success = (
                    isinstance(result, dict) and result.get("status") not in ("error", None)
                ) or not isinstance(result, dict)
                if is_success:
                    # status 为 ok/success 表示任务完成，提示汇报；其他非 error 状态（need_category 等）让 LLM 自行判断
                    if isinstance(result, dict) and result.get("status") in ("ok", "success"):
                        return StepOutcome(result, next_prompt="")
                    else:
                        return StepOutcome(result, next_prompt="")
                else:
                    return StepOutcome(result, next_prompt="")
            except Exception as e:
                yield StreamEvent("system", f"[MCP Error] {tool_name}: {e}\n")
                return StepOutcome(
                    {"status": "error", "msg": str(e)}, next_prompt=""
                )

        # 未知工具
        yield StreamEvent("system", f"Unknown tool: {tool_name}\n")
        return StepOutcome(None, next_prompt=f"Unknown tool: {tool_name}")


# Backward-compatible alias
Handler = NiuHandler
