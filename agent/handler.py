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


def read_file(file_path: str, offset: int = 1, limit: int = 500) -> str:
    """读取文件内容（支持 offset/limit 分页，limit 最大 500）"""
    import itertools

    MAX_LIMIT = 500
    if offset < 1:
        offset = 1
    if limit < 1:
        limit = MAX_LIMIT
    if limit > MAX_LIMIT:
        limit = MAX_LIMIT

    try:
        if os.path.isdir(file_path):
            return f"Error: '{file_path}' is a directory, not a file."
        with open(file_path, encoding="utf-8", errors="replace") as f:
            total_lines = sum(1 for _ in f)
            f.seek(0)
            stream = ((i, line.rstrip("\r\n")) for i, line in enumerate(f, 1))
            stream = itertools.dropwhile(lambda x: x[0] < offset, stream)
            res = list(itertools.islice(stream, limit))

            if not res:
                if offset > total_lines:
                    return f"[FILE] offset={offset} exceeds total lines ({total_lines}). Use offset=1 to read from the beginning."
                return f"[FILE] No content to display (offset={offset}, total={total_lines} lines)"

            realcnt = len(res)
            L_MAX = min(10000, max(100, 500000 // max(realcnt, 1)))
            TAG = " ... [TRUNCATED]"

            res = [(i, line if len(line) <= L_MAX else line[:L_MAX] + TAG) for i, line in res]
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

    MAX_LINES = 50

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
    for filepath in files[:200]:
        searched_count += 1
        try:
            with open(filepath, encoding="utf-8", errors="replace") as f:
                for line_no, line in enumerate(f, 1):
                    if regex.search(line):
                        matches.append(f"{filepath}:{line_no}:{line.rstrip()}")
                        if len(matches) >= MAX_LINES:
                            break
        except (OSError, UnicodeDecodeError):
            continue
        if len(matches) >= MAX_LINES:
            break

    if not matches:
        return f"[GREP] No matches for '{pattern}' in {path} (searched {searched_count} files)"

    result = "\n".join(matches)
    if len(matches) >= MAX_LINES:
        result += f"\n... (showing first {MAX_LINES} matches)"
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
            L_MAX = max(100, 512000 // realcnt) if realcnt > 0 else 100
            TAG = " ... [TRUNCATED]"

            res = [(i, line if len(line) <= L_MAX else line[:L_MAX] + TAG) for i, line in res]
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

        return {
            "status": "success" if exit_code == 0 else "error",
            "stdout": stdout_str[:10000] if len(stdout_str) > 10000 else stdout_str,
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
        self._recent_tool_calls: list[str] = []
        self._last_prompt_tokens = 0

    # ========== 工具回调机制 ==========

    def tool_before_callback(self, tool_name, args, response):
        """工具调用前：推送状态到前端"""
        # 子 Agent 的工具调用不推送前端状态
        if getattr(self, '_is_subagent', False):
            return
        try:
            from niu_api.chat import notify_tool_status_sync
            short_name = tool_name.split("/")[-1] if "/" in tool_name else tool_name
            notify_tool_status_sync(short_name, "start")
        except Exception:
            pass  # 推送失败不影响工具调用

    def tool_after_callback(self, tool_name, args, response, ret):
        """工具调用后记录摘要到 history_info"""
        # 推送工具完成状态到前端（子 Agent 除外）
        if not getattr(self, '_is_subagent', False):
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
        """追踪工具调用用于重复检测"""
        if not hasattr(self, '_recent_tool_calls'):
            self._recent_tool_calls = []

        # 构建工具调用字符串表示
        clean_args = {k: v for k, v in args.items() if not k.startswith("_")}
        args_preview = str(clean_args)[:50]
        tool_call_str = f"{tool_name}({args_preview})"

        self._recent_tool_calls.append(tool_call_str)

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
        if turn > 3 and hasattr(self, '_recent_tool_calls'):
            recent_tools = self._recent_tool_calls[-3:]
            if len(recent_tools) == 3:
                # 检查是否连续 3 次调用相同工具
                if (recent_tools[0] == recent_tools[1] == recent_tools[2]):
                    tool_name = recent_tools[0].split('(')[0]  # 提取工具名
                    import sys
                    print(
                        f"[P2-1] Detected repeated tool calls: {recent_tools}",
                        file=sys.stderr,
                        flush=True
                    )
                    next_prompt = (
                        f"⚠️ **警告：检测到重复工具调用**\n\n"
                        f"你已连续 3 次调用相同工具（{tool_name}）。这通常表示：\n"
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
        """读取文件（新 API，兼容旧参数名 path/start/count）"""
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
        import fcntl
        from datetime import datetime

        from niu_api.compat import (
            _extract_overflow_info,
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
            fcntl.flock(lock_f, fcntl.LOCK_EX)
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

                # 游标推进：overflow→不动；否则解析 processed_up_to=N 查映射，兜底 msg_ids[-1]
                if _is_subagent_overflow(journal_result):
                    overflow_info = _extract_overflow_info(journal_result)
                    logger.warning(f"[Journal] overflow: {overflow_info.get('turns_completed', 0)} turns")
                    # overflow 时游标不动
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
                fcntl.flock(lock_f, fcntl.LOCK_UN)

    def _call_subagent_gen(self, agent_name: str, args: dict):
        """调用子 Agent（生成器版本）— 同步/异步分流"""
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

            confirmation = _dispatch_async_subagent(
                agent_name=agent_name,
                task=task,
                llm_config=llm_config,
                mcp_client=self.mcp_client,
            )
            yield StreamEvent("tool_marker", f"[SubAgent] 异步派出：{confirmation[:100]}\n")
            return StepOutcome({"status": "success", "result": confirmation}, next_prompt="")

        # 同步路径（现有逻辑不变）
        try:
            yield StreamEvent("tool_marker", f"[SubAgent] Calling {agent_name}...\n")
            # 子Agent保持独立上下文，不传递主Agent历史
            _history = None
            # journal-agent 的 history 来自 _build_journal_task_for_handler，覆盖默认 _history
            if agent_name == "journal-agent" and _journal_history:
                _history = _journal_history

            result = call_subagent(
                agent_name=agent_name,
                task=task,
                llm_config=llm_config,
                mcp_client=self.mcp_client,
                history=_history,
                # journal-agent 改 history 逐条传消息后，禁用子 Agent 内部 FIFO 截断（防止砍末尾最新内容）
                # 非 journal-agent 走默认 -1（75%）
                **({"context_fifo_threshold": 0} if (agent_name == "journal-agent" and _journal_history) else {}),
                answer=answer,
                # 阶段四修复 B2：LLM 不传 unique_name 时 fallback 到 agent_name
                # 同步路径 unique_name=agent_name（方案 B），主 Agent 不需要记随机后缀
                answer_unique_name=(unique_name_arg or agent_name) if answer else None,
            )

            # journal-agent 特殊处理：更新游标（仅当有增量消息时才更新，透传 idx_to_id 映射）
            if agent_name == "journal-agent" and journal_msg_ids_for_cursor:
                self._update_journal_cursor(result, journal_msg_ids_for_cursor, _journal_idx_to_id)

            # 验证结果：检查 event-manager 是否真正创建了任务
            if agent_name == "event-manager" and ("提醒" in task or "定时" in task or "提醒我" in task):
                try:
                    import json
                    import sqlite3
                    from pathlib import Path

                    # 读取数据库路径
                    memory_path = Path.home() / ".niu" / "memory.json"
                    if memory_path.exists():
                        memory = json.loads(memory_path.read_text(encoding="utf-8"))
                        workspace = memory.get("workspace", {}).get("path")
                        if workspace:
                            db_path = str(Path(workspace) / "scheduled_tasks.db")
                            if Path(db_path).exists():
                                # 检查最新的任务
                                # P0-7: 使用 with 管理数据库连接
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

                                if latest_task:
                                    yield StreamEvent("tool_marker", f"[SubAgent] ✓ Verified task in database: {latest_task[1]} at {latest_task[3]}\n")
                                else:
                                    yield StreamEvent("system", "[SubAgent] ⚠ Warning: No task found in database\n")
                except Exception as e:
                    yield StreamEvent("system", f"[SubAgent] Warning: Failed to verify task: {e}\n")


            yield StreamEvent("tool_marker", f"[SubAgent] {agent_name} completed: {result[:200] if len(result) > 200 else result}\n")
            # 返回结果给 LLM，让它向用户汇报
            return StepOutcome(
                {"status": "success", "result": result},
                next_prompt=""
            )
        except Exception as e:
            yield StreamEvent("system", f"[SubAgent] Error: {e}\n")
            return StepOutcome(
                {"status": "error", "msg": str(e)}, next_prompt=""
            )

    # ========== MCP 工具（动态） ==========

    # Backward compatibility aliases: old tool names → new lightrag-server tools
    _TOOL_ALIASES = {
        "file_read": "read",
        "file_write": "write",
        "file_patch": "edit",
    }

    def dispatch(self, tool_name: str, args, response, index=0):
        """分发工具调用（支持 MCP 工具）- 必须是生成器"""

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
            args = {**args, "_index": index}
            yield from try_call_generator(
                self.tool_before_callback, tool_name, args, response
            )
            ret = yield from try_call_generator(self._call_subagent_gen, agent_name, args)
            _ = yield from try_call_generator(
                self.tool_after_callback, tool_name, args, response, ret
            )
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
            _ = yield from try_call_generator(
                self.tool_after_callback, tool_name, args, response, ret
            )
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
                _ = yield from try_call_generator(
                    self.tool_after_callback, real_tool_name,
                    args, response, result
                )
                # Reinforce brain region on tool use via disk
                if not getattr(self, '_is_subagent', False):
                    try:
                        from agent.brain_tools import reinforce_on_tool_use
                        reinforce_on_tool_use(real_tool_name)
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
            elif disk_result.action == "ERROR":
                # 参数/执行错误 → 提示修正
                result = disk_result.text
                _ = yield from try_call_generator(
                    self.tool_after_callback, tool_name, args, response, result
                )
                return StepOutcome(result, next_prompt="")
            else:
                # 导航命令 (LIST/READ/HELP/EMPTY) → 继续工作
                result = disk_result.text
                _ = yield from try_call_generator(
                    self.tool_after_callback, tool_name, args, response, result
                )
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

                # Reinforce brain region on tool use
                if not getattr(self, '_is_subagent', False):
                    try:
                        from agent.brain_tools import reinforce_on_tool_use
                        reinforce_on_tool_use(tool_name)
                    except Exception:
                        pass

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
                # Reinforce brain region on tool use
                if not getattr(self, '_is_subagent', False):
                    try:
                        from agent.brain_tools import reinforce_on_tool_use
                        reinforce_on_tool_use(tool_name)
                    except Exception:
                        pass

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
