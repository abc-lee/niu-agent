"""
Niu Agent Handler

继承 GenericAgent 的 BaseHandler，实现自定义工具处理。
"""

import os
import sys
import json
import re
import signal
import tempfile
import subprocess
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Optional

from loguru import logger

# 导入 GenericAgent 基类
from .generic.agent_loop import BaseHandler, StepOutcome, StreamEvent, try_call_generator

# 导入经验总结器
from .experience_summarizer import ExperienceSummarizer, ToolExecution, ExperienceContext


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
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            total_lines = sum(1 for _ in f)
            f.seek(0)
            stream = ((i, l.rstrip("\r\n")) for i, l in enumerate(f, 1))
            stream = itertools.dropwhile(lambda x: x[0] < offset, stream)
            res = list(itertools.islice(stream, limit))

            if not res:
                if offset > total_lines:
                    return f"[FILE] offset={offset} exceeds total lines ({total_lines}). Use offset=1 to read from the beginning."
                return f"[FILE] No content to display (offset={offset}, total={total_lines} lines)"

            realcnt = len(res)
            L_MAX = max(100, 512000 // realcnt) if realcnt > 0 else 100
            TAG = " ... [TRUNCATED]"

            res = [(i, l if len(l) <= L_MAX else l[:L_MAX] + TAG) for i, l in res]
            result = "\n".join(f"{i}|{l}" for i, l in res)

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
        file_path = str(Path(file_path).resolve())
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
        file_path = str(Path(file_path).resolve())
        if not os.path.exists(file_path):
            return {"status": "error", "msg": "File not found"}

        with open(file_path, "r", encoding="utf-8") as f:
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
    import re as re_mod
    import glob as glob_mod

    MAX_LINES = 50

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
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
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
    import itertools
    import collections
    import os

    try:
        if os.path.isdir(path):
            return f"Error: '{path}' is a directory, not a file. Please provide a file path, e.g. 'memory/skills/photo-face-display.md'"
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            stream = ((i, l.rstrip("\r\n")) for i, l in enumerate(f, 1))
            stream = itertools.dropwhile(lambda x: x[0] < start, stream)

            if keyword:
                before = collections.deque(maxlen=count // 3)
                for i, l in stream:
                    if keyword.lower() in l.lower():
                        res = (
                            list(before)
                            + [(i, l)]
                            + list(itertools.islice(stream, count - len(before) - 1))
                        )
                        break
                    before.append((i, l))
                else:
                    return f"Keyword '{keyword}' not found after line {start}.\n"
            else:
                res = list(itertools.islice(stream, count))

            realcnt = len(res)
            L_MAX = max(100, 512000 // realcnt) if realcnt > 0 else 100
            TAG = " ... [TRUNCATED]"

            res = [(i, l if len(l) <= L_MAX else l[:L_MAX] + TAG) for i, l in res]
            result = "\n".join(f"{i}|{l}" if show_linenos else l for i, l in res)

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
        path = str(Path(path).resolve())
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
        path = str(Path(path).resolve())
        if not os.path.exists(path):
            return {"status": "error", "msg": "File not found"}

        with open(path, "r", encoding="utf-8") as f:
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

        # 经验总结相关
        self._experience_context: Optional[ExperienceContext] = None
        self._experience_summarizer = ExperienceSummarizer()

        # P2-1: 工具调用历史追踪（用于重复检测）
        self._recent_tool_calls: list[str] = []

    # ========== 工作记忆机制 ==========

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
        # Note: memory dirty flag is set in MCP dispatch path (see dispatch() method)

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
            f"[WorkingMemory] Recorded: {tool_name} -> {summary[:50]}...",
            file=sys.stderr,
            flush=True,
        )

        # 追踪工具调用（用于重复检测）
        self._track_tool_call_for_repeat_detection(tool_name, args)

        # 追踪工具执行以供经验总结
        self._track_tool_execution(tool_name, args, ret)

        # 更新 Interaction Habits 置信度（LightRAG）
        try:
            from niu_api.internal.lightrag_adapter import LightRAGAdapter, LightRAGIngester

            adapter = LightRAGAdapter()

            # 工具调用成功，更新相关 dialect 的置信度
            if isinstance(ret, dict) and ret.get("status") == "success":
                habit_entities = adapter.search_interaction_habits(
                    query=str(args), top_k=10,
                )
                for entity in habit_entities:
                    entity_name = entity.get("entity_name", "")
                    # Match by target_tool extracted from entity_name "habit:{type}:{tool}"
                    parts = entity_name.split(":", 2)
                    target_tool = parts[2] if len(parts) >= 3 else ""
                    if target_tool == tool_name:
                        ingester = LightRAGIngester()
                        ingester.update_habit_confidence(entity_name, "success")

            # 工具调用失败
            elif isinstance(ret, dict) and ret.get("status") == "error":
                habit_entities = adapter.search_interaction_habits(
                    query=str(args), top_k=5,
                )
                for entity in habit_entities:
                    entity_name = entity.get("entity_name", "")
                    parts = entity_name.split(":", 2)
                    target_tool = parts[2] if len(parts) >= 3 else ""
                    if target_tool == tool_name:
                        ingester = LightRAGIngester()
                        ingester.update_habit_confidence(entity_name, "fail")

        except Exception:
            # 置信度更新失败不影响主流程
            logger.debug("Interaction habit update failed (non-blocking)", exc_info=True)

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

    def _track_tool_execution(self, tool_name: str, args: dict, ret):
        """追踪工具执行以供经验总结"""
        # 初始化 experience context（如果需要）
        if self._experience_context is None:
            # 从 history_info 提取用户输入（不完美但够用）
            user_input = ""
            for h in reversed(self.history_info):
                if "[Agent]" in h and "调用工具" not in h:
                    user_input = h.replace("[Agent]", "").strip()
                    break
            self._experience_context = ExperienceContext(
                user_input=user_input,
                turn_count=0,
                start_time=time.time()
            )

        # 更新轮数
        self._experience_context.turn_count = self.current_turn

        # 记录工具执行
        result_str = str(ret) if ret else ""
        success = (isinstance(ret, dict) and ret.get("status") == "success") if ret is not None else False

        tool_exec = ToolExecution(
            tool_name=tool_name,
            args={k: v for k, v in args.items() if not k.startswith("_")},
            result=result_str[:500],  # 限制结果长度
            success=success
        )
        self._experience_context.tool_executions.append(tool_exec)

        # 定期检查是否需要总结（每 10 轮）
        if self.current_turn > 0 and self.current_turn % 10 == 0:
            self._check_and_summarize_experience()

    def _check_and_summarize_experience(self):
        """检查并总结经验"""
        if self._experience_context is None:
            return

        context = self._experience_context
        should, reason = self._experience_summarizer.should_summarize(context)

        if should:
            print(
                f"[ExperienceSummarizer] Triggered: {reason}",
                file=sys.stderr,
                flush=True,
            )
            path = self._experience_summarizer.summarize_and_write(context)
            if path:
                print(
                    f"[ExperienceSummarizer] Wrote skill: {path.name}",
                    file=sys.stderr,
                    flush=True,
                )
            # 重置 context
            self._experience_context = None

    def _get_anchor_prompt(self, skip=False):
        """生成工作记忆提示词（仅工具调用摘要）"""
        if skip:
            return "\n"

        # 限制历史信息长度，避免过长导致 LLM 困惑
        history_items = self.history_info[-10:]  # 减少到最近10条
        h_str = "\n".join(history_items)
        if len(h_str) > 500:  # 限制总长度
            h_str = h_str[:500] + "..."

        prompt = f"\n### [WORKING MEMORY]\n<history>\n{h_str}\n</history>"
        prompt += f"\nCurrent turn: {self.current_turn}\n"

        return prompt

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

        # 每 35 轮强制询问用户
        if turn % 35 == 0:
            next_prompt += (
                f"\n\n[DANGER] 已连续执行第 {turn} 轮。你必须总结情况并直接向用户提问，"
                "不允许继续重试。"
            )
        # 每 7 轮警告禁止无效重试
        elif turn % 7 == 0:
            next_prompt += (
                f"\n\n[DANGER] 已连续执行第 {turn} 轮。禁止无效重试。"
                "若无有效进展，必须切换策略或请求用户协助。"
            )

        return next_prompt

    def reset_working_memory(self):
        """重置工作记忆（新会话开始时调用）"""
        self.history_info = []
        self.current_turn = 0

    def _get_abs_path(self, path: str) -> str:
        """获取绝对路径"""
        if not path:
            return ""
        return os.path.abspath(os.path.join(self.cwd, path))

    # ========== 文件操作 ==========

    def do_read(self, args: dict, response) -> StepOutcome:
        """读取文件（新 API，兼容旧参数名 path/start/count）"""
        raw_path = args.get("file_path") if "file_path" in args else args.get("path", "")
        file_path = self._get_abs_path(raw_path) if hasattr(self, "cwd") and self.cwd else raw_path
        offset = args.get("offset") if "offset" in args else args.get("start", 1)
        limit = args.get("limit") if "limit" in args else args.get("count", 500)

        result = read_file(file_path, offset=offset, limit=limit)
        anchor = self._get_anchor_prompt() if hasattr(self, "history_info") else None
        return StepOutcome(result, next_prompt=anchor)

    def do_write(self, args: dict, response) -> StepOutcome:
        """Write content to file."""
        file_path = self._get_abs_path(args.get("file_path") if "file_path" in args else args.get("path", ""))
        content = args.get("content", "")
        mode = args.get("mode", "overwrite")

        if not file_path:
            return StepOutcome({"status": "error", "msg": "file_path is required"}, next_prompt=self._get_anchor_prompt())

        result = write_file(file_path, content, mode=mode)
        return StepOutcome(result, next_prompt=self._get_anchor_prompt())

    def do_edit(self, args: dict, response) -> StepOutcome:
        """Edit file by replacing old_string with new_string."""
        file_path = self._get_abs_path(args.get("file_path") if "file_path" in args else args.get("path", ""))
        old_string = args.get("old_string") if "old_string" in args else args.get("old_content", "")
        new_string = args.get("new_string") if "new_string" in args else args.get("new_content", "")
        replace_all = args.get("replace_all", False)

        if not file_path:
            return StepOutcome({"status": "error", "msg": "file_path is required"}, next_prompt=self._get_anchor_prompt())

        result = edit_file(file_path, old_string, new_string, replace_all=replace_all)
        return StepOutcome(result, next_prompt=self._get_anchor_prompt())

    def do_grep(self, args: dict, response) -> StepOutcome:
        """Search for pattern in files."""
        pattern = args.get("pattern", "")
        path = self._get_abs_path(args.get("path", "."))
        include = args.get("include", "")

        if not pattern:
            return StepOutcome("[GREP] Error: pattern is required", next_prompt=self._get_anchor_prompt())

        result = grep_search(pattern, path, include)
        return StepOutcome(result, next_prompt=self._get_anchor_prompt())

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
            return StepOutcome("[Error] Command missing.", next_prompt="\n")

        code_type = "bash" if os.name != "nt" else "powershell"
        result = code_run(command, code_type=code_type, timeout=timeout, cwd=self.cwd)
        return StepOutcome(result, next_prompt=self._get_anchor_prompt())

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
                    next_prompt="\n",
                )

        result = code_run(code, code_type=code_type, timeout=timeout, cwd=self.cwd)
        return StepOutcome(result, next_prompt=self._get_anchor_prompt())

    # ========== 无工具调用 ==========

    def do_no_tool(self, args: dict, response) -> StepOutcome:
        """未调用工具时的处理"""
        content = getattr(response, "content", "") if response else ""

        if not content.strip():
            return StepOutcome({}, next_prompt="[System] Blank response, regenerate")

        # 检测只有反引号的响应（LLM 输出异常）
        clean_content = re.sub(r"`+", "", content).strip()
        if not clean_content:
            return StepOutcome(
                {},
                next_prompt="[System] 你只输出了反引号，没有实际内容。请重新组织回复，直接输出文本内容，不要使用空的代码块。"
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
                    next_prompt=(
                        "[System] 检测到你在上一轮回复中主要内容是较大代码块，且本轮未调用任何工具。\n"
                        "如果这些代码需要执行、写入文件或进一步分析，请重新组织回复并显式调用相应工具"
                        "（例如：code_run、write、edit 等）；\n"
                        "如果只是向用户展示或讲解代码片段，请在回复中补充自然语言说明，"
                        "并明确是否还需要额外的实际操作。"
                    ),
                )

        # 正常情况：返回给用户，使用空字符串作为 next_prompt 而不是 None
        return StepOutcome(response, next_prompt="")

    def _call_subagent_gen(self, agent_name: str, args: dict):
        """调用子 Agent（生成器版本）"""
        from .subagent import call_subagent

        task = args.get("task", "")

        # 获取完整的 LLM 配置（从全局 runner）
        from .runner import get_runner

        runner = get_runner()
        if runner is None:
            yield StreamEvent("system", "[System] Runner not initialized\n")
            return StepOutcome(
                {"status": "error", "msg": "Runner not initialized"},
                next_prompt="\n[System] Runner not initialized\n",
            )

        # 直接传递完整配置（而不是挑选字段）
        llm_config = runner.llm_config.copy()  # 复制一份，避免修改原始配置

        try:
            yield StreamEvent("tool_marker", f"[SubAgent] Calling {agent_name}...\n")
            # 过滤 WORKING MEMORY 虚拟消息，不让子Agent看到
            # 只有 entity-extractor 需要看到主Agent的tool消息，其他子Agent保持独立上下文
            if agent_name == "entity-extractor":
                _history = getattr(self, '_current_messages', None)
            else:
                _history = None
            if _history:
                _wm_ids = set()
                for m in _history:
                    if m.get("role") == "assistant" and m.get("tool_calls"):
                        for tc in m["tool_calls"]:
                            if tc.get("function", {}).get("name") == "working_memory":
                                _wm_ids.add(tc.get("id", ""))
                _history = [m for m in _history if not (
                    (m.get("role") == "assistant" and m.get("tool_calls") and
                     any(tc.get("function", {}).get("name") == "working_memory" for tc in m["tool_calls"]))
                    or (m.get("role") == "tool" and m.get("tool_call_id", "") in _wm_ids)
                )]

            # 移除末尾孤立的 assistant(tool_calls)（没有对应 tool 结果）
            while _history and _history[-1].get("role") == "assistant" and _history[-1].get("tool_calls"):
                _history.pop()

            result = call_subagent(
                agent_name=agent_name,
                task=task,
                llm_config=llm_config,
                mcp_client=self.mcp_client,
                history=_history,
            )

            # 验证结果：检查 event-manager 是否真正创建了任务
            if agent_name == "event-manager" and ("提醒" in task or "定时" in task or "提醒我" in task):
                try:
                    from pathlib import Path
                    import json
                    import sqlite3

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
                                    yield StreamEvent("system", f"[SubAgent] ⚠ Warning: No task found in database\n")
                except Exception as e:
                    yield StreamEvent("system", f"[SubAgent] Warning: Failed to verify task: {e}\n")

            # entity-extractor 调用完成后，从输出提取游标并写入文件
            # 这样下次 sleep 模式触发时，游标已推进，不会重复处理
            if agent_name == "entity-extractor" and result:
                try:
                    import re as _re
                    _pattern = r'\{\s*"last_entity_extract_id"\s*:\s*"([^"]+)"\s*'
                    _match = _re.search(_pattern, result, _re.DOTALL)
                    if _match:
                        _new_cursor = _match.group(1)
                        from pathlib import Path as _Path
                        from datetime import datetime as _dt
                        _cursor_path = _Path.home() / ".niu" / "last_entity_extract.json"
                        _cursor_path.parent.mkdir(parents=True, exist_ok=True)
                        _cursor_path.write_text(json.dumps({
                            "last_entity_extract_id": _new_cursor,
                            "last_entity_extract_at": _dt.now().isoformat(),
                        }, ensure_ascii=False, indent=2), encoding="utf-8")
                        logger.info(f"[SubAgent] Entity cursor updated: {_new_cursor}")
                except Exception as e:
                    logger.warning(f"[SubAgent] Failed to update entity cursor: {e}")

            yield StreamEvent("tool_marker", f"[SubAgent] {agent_name} completed: {result[:200] if len(result) > 200 else result}\n")
            # 返回结果给 LLM，让它向用户汇报
            return StepOutcome(
                {"status": "success", "result": result},
                next_prompt=self._get_anchor_prompt()
            )
        except Exception as e:
            yield StreamEvent("system", f"[SubAgent] Error: {e}\n")
            return StepOutcome(
                {"status": "error", "msg": str(e)}, next_prompt=f"\n[System] Sub-agent error: {e}\n"
            )

    # ========== 记忆管理 ==========

    def do_save_memory(self, args: dict, response) -> StepOutcome:
        """
        手动保存记忆（LLM 直接调用）

        参数:
            content: 记忆内容
            memory_type: 记忆类型
            title: 可选标题
            importance: 可选重要性（0-1）
        """
        try:
            content = args.get("content", "")
            memory_type = args.get("memory_type", "facts")
            title = args.get("title")
            importance = args.get("importance")

            if not content:
                return StepOutcome(
                    {"status": "error", "msg": "content is required"},
                    next_prompt="[System] 记忆内容不能为空\n",
                )

            # 调用 memory-server/remember
            from agent.tool_registry import get_registry

            tool_fn = get_registry().get("memory-server/remember")
            if tool_fn:
                result = tool_fn(
                    content=content,
                    memory_type=memory_type,
                    title=title,
                    importance=importance or self._calculate_importance(memory_type),
                )

                # Also store in brain graph (secondary write, don't block on failure)
                try:
                    from niu_api.internal.brain_graph import get_brain_graph
                    bg = get_brain_graph()
                    bg.store_memory(
                        content=content,
                        memory_type=memory_type,
                    )
                except Exception as e:
                    logger.debug(f"Brain graph write failed (non-blocking): {e}")

                return StepOutcome(
                    {"status": "success", "memory_id": result.get("memory_id")},
                    next_prompt=self._get_anchor_prompt(),
                )
            else:
                return StepOutcome(
                    {"status": "error", "msg": "memory-server/remember tool not available"},
                    next_prompt="[System] 记忆工具不可用\n",
                )

        except Exception as e:
            return StepOutcome(
                {"status": "error", "msg": str(e)},
                next_prompt=f"[System] 保存记忆失败: {e}\n",
            )

    def _calculate_importance(self, memory_type: str) -> float:
        """根据记忆类型计算重要性"""
        importance_map = {
            "environment": 0.9,
            "preferences": 0.85,
            "skills": 0.8,
            "experiences": 0.7,
            "facts": 0.75,
        }
        return importance_map.get(memory_type, 0.75)

    # ========== MCP 工具（动态） ==========

    # Backward compatibility aliases: old tool names → new lightrag-server tools
    _TOOL_ALIASES = {
        "file_read": "read",
        "file_write": "write",
        "file_patch": "edit",
    }

    def dispatch(self, tool_name: str, args, response, index=0):
        """分发工具调用（支持 MCP 工具）- 必须是生成器"""
        # Apply backward compatibility aliases
        resolved_name = self._TOOL_ALIASES.get(tool_name, tool_name)
        if resolved_name != tool_name:
            logger.debug(f"Tool alias: {tool_name} → {resolved_name}")
            tool_name = resolved_name
        # 先检查 chat-with-* 子 Agent 调用（通配路由）
        if tool_name.startswith("chat-with-"):
            agent_name = tool_name[len("chat-with-"):]
            args = {**args, "_index": index}
            prer = yield from try_call_generator(
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
            prer = yield from try_call_generator(
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
            prer = yield from try_call_generator(
                self.tool_before_callback, tool_name, args, response
            )
            from niu_api.internal.disk_engine import DiskResult
            disk_result = self.disk_engine.execute(command)
            if disk_result.action == "EXECUTE":
                # 返回原始 MCP 结果，保留 status 检查和 memory dirty flag
                result = disk_result.raw_result
                # Map /dir/tool → server-name/tool using DiskConfig
                real_tool_name = tool_name
                parts = disk_result.tool_path.strip("/").split("/", 1)
                if len(parts) == 2:
                    dir_name, tool = parts
                    server = self.disk_engine.config.get_server_by_dir(dir_name)
                    if server is not None:
                        real_tool_name = f"{server.server_name}/{tool}"
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
                    # Set memory dirty flag for user memory tools on success
                    if real_tool_name in ("memory-server/user_memory_remember", "memory-server/user_memory_forget"):
                        try:
                            from agent.runner import get_runner
                            runner = get_runner()
                            if runner and hasattr(runner, '_memory_dirty'):
                                runner._memory_dirty.set()
                        except Exception as e:
                            logger.debug(f"Memory dirty flag set failed: {e}")
                    return StepOutcome(result, next_prompt=f"工具调用成功。请向用户简洁汇报结果。")
                else:
                    return StepOutcome(result, next_prompt="Tool execution returned an error. Read the error message above and adjust accordingly.")
            elif disk_result.action == "ERROR":
                # 参数/执行错误 → 提示修正
                result = disk_result.text
                _ = yield from try_call_generator(
                    self.tool_after_callback, tool_name, args, response, result
                )
                return StepOutcome(result, next_prompt="Disk command returned an error. Read the error message above and fix the command accordingly.")
            else:
                # 导航命令 (LIST/READ/HELP/EMPTY) → 继续工作
                result = disk_result.text
                _ = yield from try_call_generator(
                    self.tool_after_callback, tool_name, args, response, result
                )
                return StepOutcome(result, next_prompt=self._get_anchor_prompt())

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
                        next_prompt=self._get_anchor_prompt()
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

                # 调用 tool_after_callback（工作记忆、重复检测、习惯追踪）
                try:
                    _ = yield from try_call_generator(
                        self.tool_after_callback, tool_name, args, response, result
                    )
                except Exception:
                    pass  # callback 失败不影响主流程

                # 判断任务是否完成：
                # - 成功后让LLM向用户汇报结果
                if isinstance(result, dict) and result.get("status") not in ("error", None):
                    # Set memory dirty flag for user memory tools
                    if tool_name in ("memory-server/user_memory_remember", "memory-server/user_memory_forget"):
                        try:
                            from agent.runner import get_runner
                            runner = get_runner()
                            if runner and hasattr(runner, '_memory_dirty'):
                                runner._memory_dirty.set()
                        except Exception as e:
                            logger.debug(f"Memory dirty flag set failed: {e}")
                    # 成功执行，提示LLM向用户汇报
                    result_summary = json.dumps(result, ensure_ascii=False)[:500]
                    return StepOutcome(result, next_prompt=f"工具调用成功。请向用户简洁汇报结果：{result_summary}")
                else:
                    # 需要进一步处理，返回anchor prompt
                    return StepOutcome(result, next_prompt=self._get_anchor_prompt())
            except Exception as e:
                yield StreamEvent("system", f"[MCP Error] {tool_name}: {e}\n")
                return StepOutcome(
                    {"status": "error", "msg": str(e)}, next_prompt=self._get_anchor_prompt()
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

                try:
                    _ = yield from try_call_generator(
                        self.tool_after_callback, tool_name, args, response, result
                    )
                except Exception:
                    pass

                if isinstance(result, dict) and result.get("status") not in ("error", None):
                    result_summary = json.dumps(result, ensure_ascii=False)[:500]
                    return StepOutcome(result, next_prompt=f"工具调用成功。请向用户简洁汇报结果：{result_summary}")
                else:
                    return StepOutcome(result, next_prompt=self._get_anchor_prompt())
            except Exception as e:
                yield StreamEvent("system", f"[MCP Error] {tool_name}: {e}\n")
                return StepOutcome(
                    {"status": "error", "msg": str(e)}, next_prompt=self._get_anchor_prompt()
                )

        # 未知工具
        yield StreamEvent("system", f"Unknown tool: {tool_name}\n")
        return StepOutcome(None, next_prompt=f"Unknown tool: {tool_name}")


# Backward-compatible alias
Handler = NiuHandler
