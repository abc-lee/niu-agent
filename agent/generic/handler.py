"""
GenericAgent Handler - 完整移植自 E:\tools\GenericAgent\ga.py
包含 7 个原子工具 + 2 个记忆工具
"""

import sys, os, re, json, time, threading, signal
from datetime import datetime
from pathlib import Path
import tempfile, traceback, subprocess, itertools, collections

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

from .agent_loop import BaseHandler, StepOutcome, json_default


def code_run(code, code_type="python", timeout=60, cwd=None, code_cwd=None, stop_signal=[]):
    """代码执行器
    python: 运行复杂的 .py 脚本（文件模式）
    powershell/bash: 运行单行指令（命令模式）
    优先使用python，仅在必要系统操作时使用powershell。
    """
    preview = (code[:60].replace("\n", " ") + "...") if len(code) > 60 else code.strip()
    yield f"[Action] Running {code_type} in {os.path.basename(cwd)}: {preview}\n"
    script_dir = os.path.dirname(os.path.abspath(__file__))
    cwd = cwd or os.path.join(script_dir, "temp")
    tmp_path = None
    if code_type == "python":
        tmp_file = tempfile.NamedTemporaryFile(
            suffix=".ai.py", delete=False, mode="w", encoding="utf-8", dir=code_cwd
        )
        cr_header = os.path.join(script_dir, "assets", "code_run_header.py")
        if os.path.exists(cr_header):
            tmp_file.write(open(cr_header, encoding="utf-8").read())
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
        return {"status": "error", "msg": f"不支持的类型: {code_type}"}
    print("code run output:")
    startupinfo = None
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0  # SW_HIDE
    full_stdout = []

    def stream_reader(proc, logs):
        for line_bytes in iter(proc.stdout.readline, b""):
            try:
                line = line_bytes.decode("utf-8")
            except UnicodeDecodeError:
                line = line_bytes.decode("latin-1", errors="ignore")
            logs.append(line)
            try:
                print(line, end="")
            except:
                pass

    def _kill_tree(proc):
        """杀死整个进程树（包括子进程的子进程）"""
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                    capture_output=True, timeout=5,
                )
            else:
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
            istimeout = time.time() - start_t > timeout
            if istimeout or len(stop_signal) > 0:
                _kill_tree(process)
                print("[Debug] Process tree killed due to timeout or stop signal.")
                if istimeout:
                    full_stdout.append("\n[Timeout Error] 超时强制终止")
                else:
                    full_stdout.append("\n[Stopped] 用户强制终止")
                break
            time.sleep(1)

        t.join(timeout=1)
        # 确保进程已退出并回收资源
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _kill_tree(process)
            process.wait(timeout=3)
        # 关闭 stdout 管道
        try:
            if process.stdout:
                process.stdout.close()
        except Exception:
            pass
        exit_code = process.poll()

        stdout_str = "".join(full_stdout)
        status = "success" if exit_code == 0 else "error"
        status_icon = "✅" if exit_code == 0 else "❌"
        if exit_code is None:
            status_icon = "⏳"
        output_snippet = smart_format(
            stdout_str, max_str_len=600, omit_str="\n\n[omitted long output]\n\n"
        )
        yield f"[Status] {status_icon} Exit Code: {exit_code}\n[Stdout]\n{output_snippet}\n"
        return {
            "status": status,
            "stdout": smart_format(
                stdout_str, max_str_len=10000, omit_str="\n\n[omitted long output]\n\n"
            ),
            "exit_code": exit_code,
        }
    except Exception as e:
        if "process" in locals():
            _kill_tree(process)
        return {"status": "error", "msg": str(e)}
    finally:
        if code_type == "python" and tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


def ask_user(question: str, candidates: list = None):
    """question: 向用户提出的问题。candidates: 可选的候选项列表。需要保证should_exit为True"""
    return {
        "status": "INTERRUPT",
        "intent": "HUMAN_INTERVENTION",
        "data": {"question": question, "candidates": candidates or []},
    }


def format_error(e):
    exc_type, exc_value, exc_traceback = sys.exc_info()
    tb = traceback.extract_tb(exc_traceback)
    if tb:
        f = tb[-1]
        fname = os.path.basename(f.filename)
        return f"{exc_type.__name__}: {str(e)} @ {fname}:{f.lineno}, {f.name} -> `{f.line}`"
    return f"{exc_type.__name__}: {str(e)}"


def log_memory_access(path):
    if "memory" not in path:
        return
    script_dir = os.path.dirname(os.path.abspath(__file__))
    stats_file = os.path.join(script_dir, "memory/file_access_stats.json")
    try:
        with open(stats_file, "r", encoding="utf-8") as f:
            stats = json.load(f)
    except:
        stats = {}
    fname = os.path.basename(path)
    stats[fname] = {
        "count": stats.get(fname, {}).get("count", 0) + 1,
        "last": datetime.now().strftime("%Y-%m-%d"),
    }
    with open(stats_file, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2, ensure_ascii=False)


def expand_file_refs(text, base_dir=None):
    """展开文本中的 {{file:路径:起始行:结束行}} 引用为实际文件内容。
    可与普通文本混排。展开失败抛 ValueError。
    base_dir: 相对路径的基准目录，默认为进程 cwd。"""
    pattern = r"\{\{file:(.+?):(\d+):(\d+)\}\}"

    def replacer(match):
        path, start, end = match.group(1), int(match.group(2)), int(match.group(3))
        path = os.path.abspath(os.path.join(base_dir or ".", path))
        if not os.path.isfile(path):
            raise ValueError(f"引用文件不存在: {path}")
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if start < 1 or end > len(lines) or start > end:
            raise ValueError(f"行号越界: {path} 共{len(lines)}行, 请求{start}-{end}")
        return "".join(lines[start - 1 : end])

    return re.sub(pattern, replacer, text)


def file_patch(path: str, old_content: str, new_content: str):
    """在文件中寻找唯一的 old_content 块并替换为 new_content。"""
    path = str(Path(path).resolve())
    try:
        if not os.path.exists(path):
            return {"status": "error", "msg": "文件不存在"}
        with open(path, "r", encoding="utf-8") as f:
            full_text = f.read()
        if not old_content:
            return {"status": "error", "msg": "old_content 为空，请确认 arguments"}
        count = full_text.count(old_content)
        if count == 0:
            return {
                "status": "error",
                "msg": "未找到匹配的旧文本块，建议：先用 file_read 确认当前内容，再分小段进行 patch。若多次失败则询问用户，严禁自行使用 overwrite 或代码替换。",
            }
        if count > 1:
            return {
                "status": "error",
                "msg": f"找到 {count} 处匹配，无法确定唯一位置。请提供更长、更具体的旧文本块以确保唯一性。建议：包含上下文行来增强特征，或分小段逐个修改。",
            }
        updated_text = full_text.replace(old_content, new_content)
        with open(path, "w", encoding="utf-8") as f:
            f.write(updated_text)
        return {"status": "success", "msg": "文件局部修改成功"}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


def file_read(path, start=1, keyword=None, count=200, show_linenos=True):
    try:
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
                    return (
                        f"Keyword '{keyword}' not found after line {start}. Falling back to content from line {start}:\n\n"
                        + file_read(path, start, None, count, show_linenos)
                    )
            else:
                res = list(itertools.islice(stream, count))
            realcnt = len(res)
            L_MAX = max(100, 512000 // realcnt)
            TAG = " ... [TRUNCATED]"
            remaining = sum(1 for _ in itertools.islice(stream, 5000))
            total_lines = (start - 1) + realcnt + remaining
            total_tag = (
                "[FILE] Total "
                + (f"{total_lines}+" if remaining >= 5000 else str(total_lines))
                + " lines\n"
            )
            res = [(i, l if len(l) <= L_MAX else l[:L_MAX] + TAG) for i, l in res]
            result = "\n".join(f"{i}|{l}" if show_linenos else l for i, l in res)
            if show_linenos:
                result = total_tag + result
            return result
    except Exception as e:
        return f"Error: {str(e)}"


def smart_format(data, max_depth=2, max_str_len=100, omit_str=" ... "):
    def truncate(obj, depth):
        if isinstance(obj, str):
            if len(obj) < max_str_len + len(omit_str) * 2:
                return obj
            return f"{obj[: max_str_len // 2]}{omit_str}{obj[-max_str_len // 2 :]}"
        if depth >= max_depth:
            return truncate(str(obj), depth + 1)
        if isinstance(obj, dict):
            return {k: truncate(v, depth + 1) for k, v in obj.items()}
        if isinstance(obj, list):
            return [truncate(i, depth + 1) for i in obj]
        return obj

    if isinstance(data, (str, bytes)):
        return truncate(data, 0)
    return json.dumps(truncate(data, 0), indent=2, ensure_ascii=False, default=str)


def get_global_memory():
    """获取全局记忆 - L1 索引层"""
    prompt = "\n"
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        with open(
            os.path.join(script_dir, "memory/global_mem_insight.txt"), "r", encoding="utf-8"
        ) as f:
            insight = f.read()
        with open(
            os.path.join(script_dir, "assets/insight_fixed_structure.txt"), "r", encoding="utf-8"
        ) as f:
            structure = f.read()
        prompt += f"cwd = {os.path.abspath('./temp')} （用./引用）\n"
        prompt += f"\n[Memory] (../memory)\n"
        prompt += structure + "\n../memory/global_mem_insight.txt:\n"
        prompt += insight + "\n"
    except FileNotFoundError:
        pass
    return prompt


class GenericAgentHandler(BaseHandler):
    """Generic Agent 工具库，包含多种工具的实现。工具函数自动加上了 do_ 前缀。实际工具名没有前缀。"""

    def __init__(self, parent, last_history=None, cwd="./"):
        self.parent = parent
        self.working = {}
        self.cwd = cwd
        self.current_turn = 0
        self.history_info = last_history if last_history else []
        self.code_stop_signal = []

    def _get_abs_path(self, path):
        if not path:
            return ""
        return os.path.abspath(os.path.join(self.cwd, path))

    def tool_after_callback(self, tool_name, args, response, ret):
        if args.get("_index", 0) > 0:
            return

        # 提取 <summary> 标签（可选优化）
        rsumm = re.search(r"<summary>(.*?)</summary>", response.content, re.DOTALL)
        if rsumm:
            # LLM 提供了高质量摘要
            summary = rsumm.group(1).strip()[:200]
        else:
            # 自动生成结构化摘要（不依赖 <summary> 标签）
            summary = self._auto_generate_summary(tool_name, args, ret)

        self.history_info.append("[Agent] " + smart_format(summary, max_str_len=100))

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
        if tool_name == "file_read":
            path = clean_args.get("path", "")
            filename = os.path.basename(path) if path else "未知文件"
            return f"读取文件: {filename}"

        elif tool_name == "code_run":
            code_type = clean_args.get("type", "python")
            code = clean_args.get("code", clean_args.get("script", ""))
            preview = code[:30] + "..." if len(code) > 30 else code
            return f"执行{code_type}代码: {preview}"

        elif tool_name == "file_patch":
            path = clean_args.get("path", "")
            filename = os.path.basename(path) if path else "未知文件"
            return f"修改文件: {filename}"

        elif tool_name == "start_long_term_update":
            return "保存长期记忆"

        elif tool_name.startswith("chat-with-"):
            agent = tool_name.replace("chat-with-", "")
            return f"调用子Agent: {agent}"

        elif "/" in tool_name:  # MCP 工具（格式：server/name）
            server, name = tool_name.split("/", 1) if "/" in tool_name else ("", tool_name)
            return f"调用MCP工具: {name}"

        elif tool_name == "no_tool":
            # 直接回答用户
            return "直接回答用户问题"

        else:
            # 默认：工具名 + 参数预览
            args_preview = str(clean_args)[:50]
            return f"调用工具: {tool_name}({args_preview})"

    def _extract_code_block(self, response, code_type):
        matches = re.findall(rf"```{code_type}\n(.*?)\n```", response.content, re.DOTALL)
        return matches[-1].strip() if matches else None

    def do_code_run(self, args, response):
        """执行代码片段，有长度限制，不允许代码中放大量数据，如有需要应当通过文件读取进行。"""
        if (
            response.tool_calls
            and sum(
                1
                for tc in response.tool_calls[: args.get("_index", 0)]
                if tc.function.name == "code_run"
            )
            > 0
        ):
            return StepOutcome("[ERROR] no multi code_run in one round!", next_prompt="\n")
        code_type = args.get("type", "python")
        code = args.get("code") or args.get("script")
        if not code:
            code = self._extract_code_block(response, code_type)
            if not code:
                return StepOutcome(
                    "[Error] Code missing. Use ```{code_type} block or 'script' arg.",
                    next_prompt="\n",
                )
        timeout = args.get("timeout", 60)
        raw_path = os.path.join(self.cwd, args.get("cwd", "./"))
        cwd = os.path.normpath(os.path.abspath(raw_path))
        code_cwd = os.path.normpath(self.cwd)
        if args.get("_inline_eval"):
            ns = {"handler": self, "parent": self.parent}
            try:
                result = repr(eval(code, ns))
            except SyntaxError:
                exec(code, ns)
                result = ns.get("_r", "OK")
            except Exception as e:
                result = f"Error: {e}"
        else:
            result = yield from code_run(
                code, code_type, timeout, cwd, code_cwd=code_cwd, stop_signal=self.code_stop_signal
            )
        next_prompt = self._get_anchor_prompt(skip=args.get("_index", 0) > 0)
        return StepOutcome(result, next_prompt=next_prompt)

    def do_ask_user(self, args, response):
        question = args.get("question", "请提供输入：")
        candidates = args.get("candidates", [])
        result = ask_user(question, candidates)
        yield f"Waiting for your answer ...\n"
        return StepOutcome(result, next_prompt="", should_exit=True)

    def do_file_patch(self, args, response):
        path = self._get_abs_path(args.get("path", ""))
        yield f"[Action] Patching file: {path}\n"
        old_content = args.get("old_content", "")
        new_content = args.get("new_content", "")
        try:
            new_content = expand_file_refs(new_content, base_dir=self.cwd)
        except ValueError as e:
            yield f"[Status] ❌ 引用展开失败: {e}\n"
            return StepOutcome({"status": "error", "msg": str(e)}, next_prompt="\n")
        result = file_patch(path, old_content, new_content)
        yield f"\n{smart_format(result)}\n"
        next_prompt = self._get_anchor_prompt(skip=args.get("_index", 0) > 0)
        return StepOutcome(result, next_prompt=next_prompt)

    def do_file_write(self, args, response):
        """用于对整个文件的大量处理，精细修改要用file_patch。
        需要将要写入的内容放在<file_content>标签内，或者放在代码块中。
        """
        path = self._get_abs_path(args.get("path", ""))
        mode = args.get("mode", "overwrite")  # overwrite/append/prepend
        action_str = {"prepend": "Prepending to", "append": "Appending to"}.get(mode, "Overwriting")
        yield f"[Action] {action_str} file: {os.path.basename(path)}\n"

        def extract_robust_content(text):
            tag = re.search(r"<file_content>(.*)</file_content>", text, re.DOTALL)
            if tag:
                return tag.group(1).strip()
            s, e = text.find("```"), text.rfind("```")
            if -1 < s < e:
                return text[text.find("\n", s) + 1 : e].strip()
            return None

        blocks = extract_robust_content(response.content)
        if not blocks:
            yield f"[Status] ❌ 失败: 未在回复中找到<file_content>代码块内容\n"
            return StepOutcome(
                {
                    "status": "error",
                    "msg": "No content found, if you want a blank, you should use code_run",
                },
                next_prompt="\n",
            )
        try:
            new_content = expand_file_refs(blocks, base_dir=self.cwd)
            if mode == "prepend":
                old = open(path, "r", encoding="utf-8").read() if os.path.exists(path) else ""
                open(path, "w", encoding="utf-8").write(new_content + old)
            else:
                with open(path, "a" if mode == "append" else "w", encoding="utf-8") as f:
                    f.write(new_content)
            yield f"[Status] ✅ {mode.capitalize()} 成功 ({len(new_content)} bytes)\n"
            next_prompt = self._get_anchor_prompt(skip=args.get("_index", 0) > 0)
            return StepOutcome(
                {"status": "success", "writed_bytes": len(new_content)}, next_prompt=next_prompt
            )
        except Exception as e:
            yield f"[Status] ❌ 写入异常: {str(e)}\n"
            return StepOutcome({"status": "error", "msg": str(e)}, next_prompt="\n")

    def do_file_read(self, args, response):
        """读取文件内容。从第start行开始读取。如有keyword则返回第一个keyword(忽略大小写)周边内容"""
        path = self._get_abs_path(args.get("path", ""))
        yield f"\n[Action] Reading file: {path}\n"
        start = args.get("start", 1)
        count = args.get("count", 200)
        keyword = args.get("keyword")
        show_linenos = args.get("show_linenos", True)
        result = file_read(
            path, start=start, keyword=keyword, count=count, show_linenos=show_linenos
        )
        if show_linenos:
            tips = "由于设置了show_linenos，以下返回信息为：(行号|)内容 。\n"
            result = tips + result
        if " ... [TRUNCATED]" in result:
            result += "\n\n（某些行被截断，如需完整内容可改用 code_run 读取）"
        next_prompt = self._get_anchor_prompt()
        log_memory_access(path)
        if "memory" in path or "sop" in path:
            next_prompt += "\n[SYSTEM TIPS] 正在读取记忆或SOP文件，若决定按sop执行请提取sop中的关键点（特别是靠后的）update working memory."
        return StepOutcome(result, next_prompt=next_prompt)

    def do_update_working_checkpoint(self, args, response):
        """为整个任务设定后续需要临时记忆的重点。"""
        key_info = args.get("key_info", "")
        related_sop = args.get("related_sop", "")
        if "key_info" in args:
            self.working["key_info"] = key_info
        if "related_sop" in args:
            self.working["related_sop"] = related_sop
        self.working["passed_sessions"] = 0
        yield f"[Info] Updated key_info and related_sop.\n"
        next_prompt = self._get_anchor_prompt(skip=args.get("_index", 0) > 0)
        return StepOutcome({"status": "success"}, next_prompt=next_prompt)

    def do_no_tool(self, args, response):
        """这是一个特殊工具，由引擎自主调用，不要包含在TOOLS_SCHEMA里。
        当模型在一轮中未显式调用任何工具时，由引擎自动触发。
        二次确认仅在回复几乎只包含<thinking>/<summary>和一段大代码块时触发。"""
        # 提取 content（支持 MockResponse 对象和普通字符串）
        content = getattr(response, "content", "") or ""
        # 提取 thinking（如果有）
        thinking = getattr(response, "thinking", None)

        if not response or not content.strip():
            yield "[Warn] LLM returned an empty response. Retrying...\n"
            return StepOutcome({}, next_prompt="[System] Blank response, regenerate and tooluse")
        if "流异常中断，未收到完整响应 !!!]" in content:
            return StepOutcome(
                {}, next_prompt="[System] Incomplete response. Regenerate and tooluse."
            )
        if "max_tokens !!!]" in content:
            return StepOutcome(
                {}, next_prompt="[System] max_tokens limit reached. Use multi small steps to do it."
            )
        # 2. 检测"包含较大代码块但未调用工具"的情况
        code_block_pattern = r"```[a-zA-Z0-9_]*\n[\s\S]{300,}?```"
        m = re.search(code_block_pattern, content)
        if m:
            residual = content
            residual = residual.replace(m.group(0), "")
            residual = re.sub(r"<thinking>[\s\S]*?</thinking>", "", residual, flags=re.IGNORECASE)
            residual = re.sub(r"<summary>[\s\S]*?</summary>", "", residual, flags=re.IGNORECASE)
            clean_residual = re.sub(r"\s+", "", residual)
            if len(clean_residual) <= 20:
                yield "[Info] Detected large code block without tool call and no extra natural language. Requesting clarification.\n"
                next_prompt = (
                    "[System] 检测到你在上一轮回复中主要内容是较大代码块（仅配有<thinking>/<summary>），且本轮未调用任何工具。\n"
                    "如果这些代码需要执行、写入文件或进一步分析，请重新组织回复并显式调用相应工具"
                    "（例如：code_run、file_write、file_patch 等）；\n"
                    "如果只是向用户展示或讲解代码片段，请在回复中补充自然语言说明，"
                    "并明确是否还需要额外的实际操作。"
                )
                return StepOutcome({}, next_prompt=next_prompt)
        # 3. 正常情况：直接将回复返回给用户并结束循环
        yield "[Info] Final response to user.\n"
        # 构造最终响应：如果有 thinking，添加到 content 前面
        final_content = content
        if thinking and thinking.strip():
            final_content = f"<thinking>{thinking}</thinking>\n\n{content}"
        return StepOutcome(final_content, next_prompt=None)

    def do_start_long_term_update(self, args, response):
        """Agent觉得当前任务完成后有重要信息需要记忆时调用此工具。"""
        prompt = """### [总结提炼经验] 既然你觉得当前任务有重要信息需要记忆，请提取最近一次任务中【事实验证成功且长期有效】的环境事实、用户偏好、重要步骤，更新记忆。
本工具是标记开启结算过程，若已在更新记忆过程或没有值得记忆的点，忽略本次调用。
**提取行动验证成功的信息**：
- **环境事实**（路径/凭证/配置）→ `file_patch` 更新 L2，同步 L1
- **复杂任务经验**（关键坑点/前置条件/重要步骤）→ L3 精简 SOP（只记你被坑得多次重试的核心要点）
**禁止**：临时变量、具体推理过程、未验证信息、通用常识、你可以轻松复现的细节。
**操作**：严格遵循提供的L0的记忆更新SOP。先 `file_read` 看现有 → 判断类型 → 最小化更新 → 无新内容跳过，保证对记忆库最小局部修改。\n
""" + get_global_memory()
        yield "[Info] Start distilling good memory for long-term storage.\n"
        path = "./memory/memory_management_sop.md"
        if os.path.exists(path):
            result = file_read(path, show_linenos=False)
        else:
            result = "Memory Management SOP not found. Do not update memory."
        return StepOutcome(result, next_prompt=prompt)

    def _get_anchor_prompt(self, skip=False):
        if skip:
            return "\n"
        h_str = "\n".join(self.history_info[-20:])
        prompt = f"\n### [WORKING MEMORY]\n<history>\n{h_str}\n</history>"
        prompt += f"\nCurrent turn: {self.current_turn}\n"
        if self.working.get("key_info"):
            prompt += f"\n<key_info>{self.working.get('key_info')}</key_info>"
        if self.working.get("related_sop"):
            prompt += f"\n有不清晰的地方请再次读取{self.working.get('related_sop')}"
        try:
            print(prompt)
        except:
            pass
        return prompt

    def next_prompt_patcher(self, next_prompt, outcome, turn):
        if turn % 35 == 0 and "plan" not in str(self.working.get("related_sop")):
            next_prompt += (
                f"\n\n[DANGER] 已连续执行第 {turn} 轮。你必须总结情况并直接向用户提问，不允许继续重试。"
            )
        elif turn % 7 == 0:
            next_prompt += f"\n\n[DANGER] 已连续执行第 {turn} 轮。禁止无效重试。若无有效进展，必须切换策略：1. 探测物理边界 2. 请求用户协助。如有需要，可调用 update_working_checkpoint 保存关键上下文。"
        elif turn % 10 == 0:
            next_prompt += get_global_memory()
        return next_prompt
