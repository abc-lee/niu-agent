"""
Niu Agent Handler

继承 GenericAgent 的 BaseHandler，实现自定义工具处理。
"""

import os
import sys
import json
import re
import tempfile
import subprocess
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Optional

# 导入 GenericAgent 基类
from .generic.agent_loop import BaseHandler, StepOutcome, try_call_generator

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


def file_read(
    path: str, start: int = 1, keyword: str = None, count: int = 200, show_linenos: bool = True
) -> str:
    """读取文件内容"""
    import itertools
    import collections

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

    try:
        process = subprocess.Popen(
            cmd,
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
                process.kill()
                full_stdout.append(f"\n[Timeout Error] Process killed after {timeout}s")
                break
            time.sleep(0.5)

        t.join(timeout=1)
        exit_code = process.poll()
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


def ask_user(question: str, candidates: list = None) -> dict:
    """向用户提问"""
    return {
        "status": "INTERRUPT",
        "intent": "HUMAN_INTERVENTION",
        "data": {"question": question, "candidates": candidates or []},
    }


class NiuHandler(BaseHandler):
    """
    Niu Agent 工具处理器

    继承 GenericAgent 的 BaseHandler，实现：
    - 文件操作（read, write, patch）
    - 代码执行（code_run）
    - 用户交互（ask_user）
    - MCP 工具调用（动态加载）
    """

    def __init__(self, cwd: str = None, mcp_client=None):
        self.cwd = cwd or os.getcwd()
        self.mcp_client = mcp_client
        self.working = {}
        self.current_turn = 0
        self.history_info = []
        self._done_hooks = []
        self._disable_memory_recall = False  # 禁用长期记忆检索（子 Agent 使用）

        # 经验总结相关
        self._experience_context: Optional[ExperienceContext] = None
        self._experience_summarizer = ExperienceSummarizer()

        # P2-1: 工具调用历史追踪（用于重复检测）
        self._recent_tool_calls: list[str] = []

    # ========== 工作记忆机制 ==========

    def tool_after_callback(self, tool_name, args, response, ret):
        """工具调用后记录摘要到 history_info"""
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

        # 增强：判断是否值得长期记忆
        if self._should_remember(tool_name, args, ret):
            self.working["suggest_remember"] = True
            self.working["remember_reason"] = self._get_remember_reason(tool_name, args, ret)

        # 追踪工具执行以供经验总结
        self._track_tool_execution(tool_name, args, ret)

        # 更新 Interaction Habits 置信度
        try:
            from agent.vector_search import VectorSearchAdapter

            vs = VectorSearchAdapter()

            # 工具调用成功，更新相关 dialect 的置信度
            if hasattr(ret, 'status') and ret.status == "success":
                dialect_results = vs.search_interaction_habits(
                    query=str(args), habit_type="tool_dialect", limit=10, min_score=0.3
                )
                for r in dialect_results:
                    if r.metadata.get("target_tool") == tool_name:
                        vs.update_habit_confidence(r.id, "success")

            # 工具调用失败
            elif hasattr(ret, 'status') and ret.status == "error":
                dialect_results = vs.search_interaction_habits(
                    query=str(args), habit_type="tool_dialect", limit=5, min_score=0.3
                )
                for r in dialect_results:
                    if r.metadata.get("target_tool") == tool_name:
                        vs.update_habit_confidence(r.id, "fail")

        except Exception:
            # 置信度更新失败不影响主流程
            pass

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
            exit_code = ret.get("exit_code", "?") if isinstance(ret, dict) else "?"
            return f"执行{code_type}代码: {preview} (退出码: {exit_code})"

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

        # P2-1: 记录工具调用（用于重复检测）
        tool_call_str = f"{tool_name}({str(args)[:50]})"  # 限制长度
        self._recent_tool_calls.append(tool_call_str)
        # 只保留最近 10 次调用
        if len(self._recent_tool_calls) > 10:
            self._recent_tool_calls = self._recent_tool_calls[-10:]

        # 记录工具执行
        result_str = str(ret) if ret else ""
        success = isinstance(ret, dict) and ret.get("status") == "success" if ret else False

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

    def _should_remember(self, tool_name: str, args: dict, ret) -> bool:
        """判断是否值得长期记忆"""
        # 用户明确要求记住
        if args.get("explicit_remember"):
            return True

        # 成功的复杂操作
        if tool_name in ["code_run", "file_write", "file_patch"]:
            if isinstance(ret, dict) and ret.get("status") == "success":
                # 代码执行成功
                if tool_name == "code_run":
                    code = args.get("code") or args.get("script", "")
                    if len(code) > 200:  # 复杂代码
                        return True
                # 文件操作
                elif tool_name in ["file_write", "file_patch"]:
                    return True

        # MCP 工具调用成功
        if "/" in tool_name and isinstance(ret, dict) and ret.get("status") == "success":
            return True

        return False

    def _get_remember_reason(self, tool_name: str, args: dict, ret) -> str:
        """获取建议记忆的原因"""
        if args.get("explicit_remember"):
            return "用户明确要求记住"

        if tool_name == "code_run":
            return "成功执行了复杂代码"

        if tool_name in ["file_write", "file_patch"]:
            return f"完成了文件操作: {tool_name}"

        if "/" in tool_name:
            return f"成功调用了 MCP 工具: {tool_name}"

        return "检测到重要操作"

    def _get_anchor_prompt(self, skip=False):
        """生成工作记忆提示词"""
        if skip:
            return "\n"

        # 限制历史信息长度，避免过长导致 LLM 困惑
        history_items = self.history_info[-10:]  # 减少到最近10条
        h_str = "\n".join(history_items)
        if len(h_str) > 500:  # 限制总长度
            h_str = h_str[:500] + "..."

        prompt = f"\n### [WORKING MEMORY]\n<history>\n{h_str}\n</history>"
        prompt += f"\nCurrent turn: {self.current_turn}\n"

        if self.working.get("key_info"):
            key_info = self.working.get("key_info")[:200]  # 限制长度
            prompt += f"\n<key_info>{key_info}</key_info>"
        if self.working.get("related_sop"):
            prompt += f"\n有不清晰的地方请再次读取{self.working.get('related_sop')}"

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
        if turn % 35 == 0 and "plan" not in str(self.working.get("related_sop")):
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

        # 增强：每 5 轮注入相关长期记忆
        if turn % 5 == 0 and turn > 0:
            memories = self._recall_relevant_memories(next_prompt)
            if memories:
                next_prompt += f"\n\n### [相关长期记忆]\n{memories}"

        # 增强：如果有建议记忆的标记，提示 LLM
        if self.working.get("suggest_remember"):
            reason = self.working.get("remember_reason", "")
            next_prompt += (
                f"\n\n[SYSTEM TIP] 检测到值得长期记忆的信息: {reason}。"
                "建议调用 start_long_term_update 提炼记忆。"
            )
            # 清除标记
            self.working.pop("suggest_remember", None)
            self.working.pop("remember_reason", None)

        # 每 10 轮注入全局记忆
        if turn % 10 == 0 and turn > 0:
            from .generic.handler import get_global_memory

            try:
                global_mem = get_global_memory()
                if global_mem:
                    next_prompt += f"\n\n### [GLOBAL MEMORY]\n{global_mem}"
            except Exception:
                pass

        return next_prompt

    def _recall_relevant_memories(self, context: str) -> Optional[str]:
        """检索相关长期记忆"""
        # 子 Agent 禁用记忆检索
        if getattr(self, "_disable_memory_recall", False):
            return None

        try:
            if not self.mcp_client:
                return None

            from agent.tool_registry import get_registry

            # 从上下文中提取关键词
            keywords = self._extract_keywords(context)
            if not keywords:
                return None

            # 调用 memory-server/recall
            tool_fn = get_registry().get("memory-server/recall")
            if tool_fn:
                result = tool_fn(
                    query=keywords,
                    limit=3,
                    level="l1",
                )
            else:
                return None

            if isinstance(result, list) and result:
                memories = []
                for mem in result[:3]:
                    content = mem.get("content", "")
                    memory_type = mem.get("metadata", {}).get("memory_type", "")
                    memories.append(f"- [{memory_type}] {content[:100]}")

                return "\n".join(memories)

            return None

        except Exception:
            return None

    def _extract_keywords(self, text: str) -> str:
        """从文本中提取关键词"""
        # 简单实现：提取重要词汇
        import re

        # 提取中文词汇（2-4 字）
        chinese_words = re.findall(r"[\u4e00-\u9fa5]{2,4}", text)

        # 提取英文单词
        english_words = re.findall(r"\b[A-Z][a-z]+\b", text)

        # 合并并去重
        keywords = list(set(chinese_words + english_words))[:5]

        return " ".join(keywords) if keywords else ""

        return next_prompt

    def reset_working_memory(self):
        """重置工作记忆（新会话开始时调用）"""
        self.history_info = []
        self.working = {}
        self.current_turn = 0

    def _get_abs_path(self, path: str) -> str:
        """获取绝对路径"""
        if not path:
            return ""
        return os.path.abspath(os.path.join(self.cwd, path))

    # ========== 文件操作 ==========

    def do_file_read(self, args: dict, response) -> StepOutcome:
        """读取文件"""
        path = self._get_abs_path(args.get("path", ""))
        start = args.get("start", 1)
        count = args.get("count", 200)
        keyword = args.get("keyword")
        show_linenos = args.get("show_linenos", True)

        result = file_read(
            path, start=start, keyword=keyword, count=count, show_linenos=show_linenos
        )
        return StepOutcome(result, next_prompt=self._get_anchor_prompt())

    def do_file_write(self, args: dict, response) -> StepOutcome:
        """写入文件"""
        path = self._get_abs_path(args.get("path", ""))
        content = args.get("content", "")
        mode = args.get("mode", "write")

        result = file_write(path, content, mode=mode)
        return StepOutcome(result, next_prompt=self._get_anchor_prompt())

    def do_file_patch(self, args: dict, response) -> StepOutcome:
        """局部修改文件"""
        path = self._get_abs_path(args.get("path", ""))
        old_content = args.get("old_content", "")
        new_content = args.get("new_content", "")

        result = file_patch(path, old_content, new_content)
        return StepOutcome(result, next_prompt=self._get_anchor_prompt())

    # ========== 代码执行 ==========

    def _extract_code_block(self, response, code_type):
        """从回复中提取代码块"""
        content = getattr(response, "content", "") if response else ""
        matches = re.findall(rf"```{code_type}\n(.*?)\n```", content, re.DOTALL)
        return matches[-1].strip() if matches else None

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

    # ========== 用户交互 ==========

    def do_ask_user(self, args: dict, response) -> StepOutcome:
        """向用户提问"""
        question = args.get("question", "")
        candidates = args.get("candidates")

        result = ask_user(question, candidates)
        return StepOutcome(result, next_prompt=None, should_exit=True)

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
                        "（例如：code_run、file_write、file_patch 等）；\n"
                        "如果只是向用户展示或讲解代码片段，请在回复中补充自然语言说明，"
                        "并明确是否还需要额外的实际操作。"
                    ),
                )

        # 正常情况：返回给用户，使用空字符串作为 next_prompt 而不是 None
        return StepOutcome(response, next_prompt="")

    # ========== 子 Agent 调用 ==========

    def do_chat_with_file_processor(self, args: dict, response) -> StepOutcome:
        """调用 file-processor 子 Agent"""
        return (yield from self._call_subagent_gen("file-processor", args))

    def do_chat_with_event_manager(self, args: dict, response) -> StepOutcome:
        """调用 event-manager 子 Agent"""
        return (yield from self._call_subagent_gen("event-manager", args))

    def do_chat_with_context_manager(self, args: dict, response) -> StepOutcome:
        """调用 context-manager 子 Agent"""
        return (yield from self._call_subagent_gen("context-manager", args))

    def _call_subagent_gen(self, agent_name: str, args: dict):
        """调用子 Agent（生成器版本）"""
        from .subagent import call_subagent

        task = args.get("task", "")

        # 获取完整的 LLM 配置（从全局 runner）
        from .runner import get_runner

        runner = get_runner()
        if runner is None:
            yield "[System] Runner not initialized\n"
            return StepOutcome(
                {"status": "error", "msg": "Runner not initialized"},
                next_prompt="\n[System] Runner not initialized\n",
            )

        # 直接传递完整配置（而不是挑选字段）
        llm_config = runner.llm_config.copy()  # 复制一份，避免修改原始配置

        try:
            yield f"[SubAgent] Calling {agent_name}...\n"
            result = call_subagent(
                agent_name=agent_name,
                task=task,
                llm_config=llm_config,
                mcp_client=self.mcp_client,
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
                                    yield f"[SubAgent] ⚠ Database error: {e}\n"
                                    latest_task = None

                                if latest_task:
                                    yield f"[SubAgent] ✓ Verified task in database: {latest_task[1]} at {latest_task[3]}\n"
                                else:
                                    yield f"[SubAgent] ⚠ Warning: No task found in database\n"
                except Exception as e:
                    yield f"[SubAgent] Warning: Failed to verify task: {e}\n"

            yield f"[SubAgent] {agent_name} completed: {result[:200] if len(result) > 200 else result}\n"
            # 返回结果给 LLM，让它向用户汇报
            return StepOutcome(
                {"status": "success", "result": result},
                next_prompt=f"[SubAgent Result] {agent_name} 已完成任务。请根据以下结果向用户汇报：\n{result}\n"
            )
        except Exception as e:
            yield f"[SubAgent] Error: {e}\n"
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

    def do_update_working_checkpoint(self, args: dict, response) -> StepOutcome:
        """更新工作记忆检查点"""
        key_info = args.get("key_info", "")
        related_sop = args.get("related_sop", "")

        if key_info:
            self.working["key_info"] = key_info[:500]  # 限制长度
        if related_sop:
            self.working["related_sop"] = related_sop

        return StepOutcome(
            {"status": "success", "msg": "Working memory updated"},
            next_prompt=self._get_anchor_prompt(),
        )

    def do_start_long_term_update(self, args: dict, response) -> StepOutcome:
        """
        提炼长期记忆

        触发条件：
        1. 用户明确要求'记住这个'
        2. 发现重要环境事实（第一次探测硬件）
        3. 学到重要用户偏好
        4. 完成复杂任务（15+轮）后提炼经验教训

        实现：
        - 从 history_info 提取关键信息
        - 调用 memory-server/remember 保存到向量库
        """
        try:
            # 分析 history_info，提取记忆类型和内容
            history_str = "\n".join(self.history_info[-20:])  # 最近 20 条

            # 推断记忆类型
            memory_type = self._infer_memory_type(history_str)

            # 生成记忆内容
            memory_content = self._generate_memory_content(history_str, memory_type)

            # 生成标题
            title = self._generate_memory_title(history_str, memory_type)

            # 调用 memory-server/remember
            from agent.tool_registry import get_registry

            tool_fn = get_registry().get("memory-server/remember")
            if tool_fn:
                result = tool_fn(
                    content=memory_content,
                    memory_type=memory_type,
                    title=title,
                    importance=self._calculate_importance(memory_type),
                )

                yield f"[Memory] 已保存长期记忆: {memory_type} - {title}\n"
                return StepOutcome(
                    {"status": "success", "memory_id": result.get("memory_id")},
                    next_prompt=self._get_anchor_prompt(),
                )
            else:
                yield "[Memory] memory-server/remember tool not available\n"
                return StepOutcome(
                    {"status": "error", "msg": "memory-server/remember tool not available"},
                    next_prompt=self._get_anchor_prompt(),
                )

        except Exception as e:
            yield f"[Memory] Failed to save: {e}\n"
            return StepOutcome(
                {"status": "error", "msg": str(e)},
                next_prompt=self._get_anchor_prompt(),
            )

    def _infer_memory_type(self, history_str: str) -> str:
        """从历史信息推断记忆类型"""
        history_lower = history_str.lower()

        # 用户偏好（优先级最高）
        pref_keywords = ["偏好", "喜欢", "习惯", "设置", "主题", "字体"]
        if any(kw in history_lower for kw in pref_keywords):
            return "preferences"

        # 经验教训（优先级次高，因为"问题"可能与其他关键词重叠）
        exp_keywords = ["失败", "错误", "解决", "教训", "遇到", "修复"]
        if any(kw in history_lower for kw in exp_keywords):
            return "experiences"

        # 环境相关
        env_keywords = ["硬件", "gpu", "cpu", "内存", "cuda", "系统", "配置", "路径"]
        if any(kw in history_lower for kw in env_keywords):
            return "environment"

        # 技能
        skill_keywords = ["学会", "掌握", "实现", "完成", "成功"]
        if any(kw in history_lower for kw in skill_keywords):
            return "skills"

        # 默认为事实
        return "facts"

    def _generate_memory_content(self, history_str: str, memory_type: str) -> str:
        """生成记忆内容"""
        # 清理历史信息
        lines = history_str.split("\n")
        clean_lines = [line.replace("[Agent] ", "").strip() for line in lines if line.strip()]

        # 拼接为完整内容
        content = "\n".join(clean_lines[:10])  # 最多 10 条

        # 添加上下文信息
        content = f"[{memory_type.upper()}] {content}"

        return content

    def _generate_memory_title(self, history_str: str, memory_type: str) -> str:
        """生成记忆标题"""
        # 提取第一个非空行
        lines = [line.strip() for line in history_str.split("\n") if line.strip()]
        if lines:
            first_line = lines[0].replace("[Agent] ", "")
            # 截取前 20 字符
            return first_line[:20]
        return f"{memory_type}记录"

    def _calculate_importance(self, memory_type: str) -> float:
        """根据记忆类型计算重要性（符合设计文档）"""
        importance_map = {
            "environment": 0.9,
            "preferences": 0.85,
            "skills": 0.8,
            "experiences": 0.7,
            "facts": 0.75,
        }
        return importance_map.get(memory_type, 0.75)

    # ========== MCP 工具（动态） ==========

    def dispatch(self, tool_name, args, response, index=0):
        """分发工具调用（支持 MCP 工具）- 必须是生成器"""
        # 先检查内置工具（工具名中的 - 转换为 _）
        method_name = f"do_{tool_name.replace('-', '_')}"
        if hasattr(self, method_name):
            # 直接调用方法，不委托给 super（因为 super 会用原始 tool_name 查找）
            args["_index"] = index
            prer = yield from try_call_generator(
                self.tool_before_callback, tool_name, args, response
            )
            ret = yield from try_call_generator(getattr(self, method_name), args, response)
            _ = yield from try_call_generator(
                self.tool_after_callback, tool_name, args, response, ret
            )
            return ret

        # 检查 MCP 工具（工具名格式：server/tool）
        if "/" in tool_name:
            try:
                from agent.tool_registry import get_registry

                # 从 ToolRegistry 获取工具函数
                func = get_registry().get(tool_name)

                if func is None:
                    yield f"[MCP Error] Tool not found: {tool_name}\n"
                    return StepOutcome(
                        {"status": "error", "error_code": "TOOL_NOT_FOUND", "msg": f"Tool {tool_name} not found in registry"},
                        next_prompt=self._get_anchor_prompt()
                    )

                # 直接调用工具函数
                result = func(**args)

                yield f"[MCP] {tool_name} executed\n"

                # 判断任务是否完成：
                # - 如果结果是success且没有要求进一步操作，返回空字符串表示任务完成
                # - 否则返回anchor prompt继续对话
                if isinstance(result, dict) and result.get("status") == "success":
                    # 成功执行，任务完成，返回空字符串
                    return StepOutcome(result, next_prompt="")
                else:
                    # 需要进一步处理，返回anchor prompt
                    return StepOutcome(result, next_prompt=self._get_anchor_prompt())
            except Exception as e:
                yield f"[MCP Error] {tool_name}: {e}\n"
                return StepOutcome(
                    {"status": "error", "msg": str(e)}, next_prompt=self._get_anchor_prompt()
                )

        # 未知工具
        yield f"Unknown tool: {tool_name}\n"
        return StepOutcome(None, next_prompt=f"Unknown tool: {tool_name}")
