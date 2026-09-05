"""
Niu Agent Runner

简化的 Agent 入口，直接使用 GenericAgent 组件。
Disk mode: MCP 工具通过虚拟磁盘 disk() 发现和调用，
Skills/知识通过 LightRAG 动态注入提示词。
"""

import io
import json
import os
import queue as _queue_module
import re
import sys
import threading
import time
from collections.abc import Generator
from datetime import date, datetime
from pathlib import Path
from typing import Any

from loguru import logger

from agent.subagent_registry import SubagentRegistry

# kebab-case 校验正则（小写字母/数字/连字符，且不以连字符开头/结尾）
# runner.py 顶部已有 `import re`，直接复用
_KEBAB_CASE_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def _skill_display_path(display_name: str, skills_dir: Path | None = None) -> str:
    """构造 skill 注入时的可读路径行（LLM 按此路径读取 skill 全文）。

    平铺: ~/.niu/skills/<name>.md
    目录式: ~/.niu/skills/<name>/SKILL.md
    同名冲突与两处都不存在（旧数据/幽灵实体）: 平铺形式（与 _skill_file_for_name
    平铺优先一致，保证 LLM 读取的路径与注入内容同源）

    注意：平铺分支用 is_file() 而非 exists()——目录名以 .md 结尾
    （~/.niu/skills/foo.md/）时 exists() 会把目录误判为平铺文件
    （T1 同类缺陷 R12-P3 修复的一致性要求）。
    """
    root = skills_dir or (Path.home() / ".niu" / "skills")
    flat = root / f"{display_name}.md"
    sub = root / display_name / "SKILL.md"
    if sub.exists() and not flat.is_file():
        return f"~/.niu/skills/{display_name}/SKILL.md"
    return f"~/.niu/skills/{display_name}.md"

# 主 Agent 专用工具集合（子 Agent 不可见）
# check_subagent_progress 是主 Agent 查子 Agent 进度的工具，子 Agent 不该有
MAIN_AGENT_ONLY_TOOLS = {"check_subagent_progress", "ask_user"}


# --- Stop flag mechanism ---
_stop_requested = threading.Event()


def request_stop():
    """Set the stop flag — Agent loops will check and exit."""
    _stop_requested.set()
    # R2-P2-1：主 Agent ask_user 等待也一并终止（"main-agent" 不在注册表，单独接线）
    # R6-A P1：不做粘滞标记——do_ask_user register 前直接检查 is_stop_requested()
    # （全局标志轮末 clear_stop() 清除，天然轮次边界；set_answer 覆盖"wait 中"停止）
    try:
        from agent.ask_user import get_user_ask_registry, TERMINATED_SIGNAL
        get_user_ask_registry().set_answer("main-agent", TERMINATED_SIGNAL)
    except Exception:
        pass


def clear_stop():
    """Clear the stop flag — called when Agent loop exits and at conversation start."""
    _stop_requested.clear()



# 截断断点字符集（按优先级）
_SENTENCE_END_CHARS = set("。！？!?")
_PARA_BREAK_CHARS = set("\n\r")
_COMMA_CHARS = set("，,")


def _smart_truncate(content: str, min_len: int = 80, max_len: int = 200) -> str:
    """智能截断：≤min_len 全量返回，>min_len 从 min_len 往后找自然断点。

    优先级：句末标点 > 段落换行 > 逗号 > 硬截断到 max_len。
    """
    if len(content) <= min_len:
        return content

    para_break_pos = -1
    comma_pos = -1
    search_end = min(len(content), max_len)

    for i in range(min_len, search_end):
        ch = content[i]
        if ch in _SENTENCE_END_CHARS:
            return content[: i + 1]
        if para_break_pos < 0 and ch in _PARA_BREAK_CHARS:
            para_break_pos = i + 1
        if comma_pos < 0 and ch in _COMMA_CHARS:
            comma_pos = i + 1

    if para_break_pos > 0:
        return content[:para_break_pos]
    if comma_pos > 0:
        return content[:comma_pos]
    # 硬截断：截断到 max_len，加省略号标记
    truncated = content[:search_end]
    return truncated + "..." if search_end < len(content) else truncated

def is_stop_requested() -> bool:
    """Check if stop has been requested."""
    return _stop_requested.is_set()


def request_stop_all_subagents() -> None:
    """给所有用户对话派生的子 Agent 推 /stop（双击停止按钮触发；program/scheduler 来源实例跳过）。

    机制（保留既有描述）：挂起同步 session 直接 unregister（无活跃 loop 消费 supplement）；
    活跃 session 推 /stop 到 supplement queue（is_terminate=True）+ cancel pending ask。
    注（R6 接线后）：主 Agent 的 ask_user 等待也一并终止（"main-agent" 不在注册表，
    set_answer 单独接线；与 request_stop 幂等）。
    """
    from agent.ask_main_agent import get_pending_ask_registry
    pending_ask = get_pending_ask_registry()

    # R2-P2-1：主 Agent ask_user 等待一并终止（与 request_stop 幂等；set_answer 无 future 返回 False 无害）
    try:
        from agent.ask_user import get_user_ask_registry, TERMINATED_SIGNAL
        get_user_ask_registry().set_answer("main-agent", TERMINATED_SIGNAL)
    except Exception as e:
        logger.error(f"停止主 Agent ask_user 失败：{e}")
    for instance in SubagentRegistry.list_running():
        if getattr(instance, "source", "user") != "user":
            # 程序触发（睡眠整理管道）或 scheduler 派生的子 Agent：不受停止按钮影响
            continue
        try:
            state = getattr(instance, "state", "running")
            if state == "waiting_for_answer":
                # 同步挂起 session：agent_runner_loop 已退出，supplement 推了无人消费
                # 直接 unregister 释放资源
                SubagentRegistry.unregister(instance.unique_name)
            else:
                # 活跃 session（同步 running 或异步）：推 /stop 终止
                # cancel_pending_ask 对 sync 是 no-op，安全
                pending_ask.cancel_pending_ask(instance.unique_name)
                instance.supplement_queue.push("/stop", is_terminate=True, sender="主Agent")
                ev = getattr(instance, "terminate_event", None)
                if ev is not None:
                    ev.set()  # 让卡在 LLM 流式上的子 Agent ≤0.2s 内收到终止
        except Exception as e:
            logger.error(f"给子 Agent {instance.unique_name} 推 /stop 失败：{e}")


def cleanup_suspended_sync_subagents(return_value=None):
    """主 Agent 工具循环结束时清理挂起的同步子 Agent session。

    语义（2026-08-31 用户拍板）：同步子 Agent 挂起 = 主 Agent 未完成的工具调用，
    主 Agent 正常结束工具循环时挂起现场必须保留（跨轮可 answer 接续）——
    清理只发生在用户显式停止（STOPPED / TERMINATED_BY_SUPPLEMENT / 全局停止标志）。
    注（2026-08-11 用户拍板保留）：不推送清理通知（工具错误/orphan 反馈已告知主 Agent；
    通知以 user 消息进对话流会被主 Agent 误认为用户话，造成转述混乱）。
    """
    # 用户显式停止判定（修正自 R1-B P2）：除 return_value.result 外，还要覆盖
    # runner 级 gen.close() 路径——用户 /stop 只置全局标志（compat.py L1709-1710），
    # runner L2007-2010 检查 is_stop_requested() 后 gen.close() → agent_loop GeneratorExit
    # → return_value 保持 None（L1999 初始化）。finally 时全局标志仍 True
    # （L2082-2083 才 clear_stop），故 is_stop_requested() 在此可作兜底判定；
    # 顺序安全：本函数在 clear_stop() 之前执行。
    _force_exit = (
        (return_value is not None and isinstance(return_value, dict)
         and return_value.get("result") in ("STOPPED", "TERMINATED_BY_SUPPLEMENT"))
        or is_stop_requested()
    )
    if not _force_exit:
        pending = [
            i for i in SubagentRegistry.list_running()
            if getattr(i, "is_sync", False) and getattr(i, "state", None) == "waiting_for_answer"
        ]
        if pending:
            logger.info(f"[CleanupSuspendedSync] 主 Agent 结束但挂起同步子 Agent 保留（跨轮可接续）: "
                        f"{[p.unique_name for p in pending]}")
        return
    for instance in SubagentRegistry.list_running():
        state = getattr(instance, "state", "running")
        is_sync = getattr(instance, "is_sync", False)
        if state == "waiting_for_answer" and is_sync:
            try:
                SubagentRegistry.unregister(instance.unique_name)
                logger.info(f"[CleanupSuspendedSync] 用户显式停止，已清理挂起同步子 Agent: {instance.unique_name}")
            except Exception as e:
                logger.error(f"[CleanupSuspendedSync] 清理 {instance.unique_name} 失败：{e}")


# --- Supplement queue (见缝插针) ---
_supplement_queue = _queue_module.Queue()  # 无限长度，永不阻塞


def enqueue_supplement(content: str):
    """将用户在 Agent 运行期间发送的补充消息放入队列。"""
    _supplement_queue.put(content)


def drain_supplements() -> list[str]:
    """取出所有补充消息（非阻塞，无竞态）。"""
    msgs = []
    while True:
        try:
            msgs.append(_supplement_queue.get_nowait())
        except _queue_module.Empty:
            break
    return msgs


def drain_supplement() -> str | None:
    """取出所有补充消息，格式化为单条字符串。

    - 无消息返回 None
    - 单条返回原文
    - 多条合并为 "[补充] 消息1\\n[补充] 消息2"
    """
    msgs = drain_supplements()
    if not msgs:
        return None
    if len(msgs) == 1:
        return msgs[0]
    return "\n".join(f"[补充] {m}" for m in msgs)


def _sanitize_memory_content(content: str) -> str:
    """Sanitize user memory content before injecting into system prompt.
    Prevents prompt injection by removing newlines and sentinel markers."""
    if content is None:
        return ""
    if not isinstance(content, str):
        content = str(content)
    # Remove newlines to prevent multi-line injection
    content = content.replace("\n", " ").replace("\r", " ")
    # Remove sentinel markers to prevent section boundary spoofing
    content = content.replace("<!--USER_MEMORY_START-->", "").replace("<!--USER_MEMORY_END-->", "")
    # Remove markdown headers to prevent section injection
    content = re.sub(r"^#{1,6}\s*", "", content, flags=re.MULTILINE)
    # Truncate to 300 chars as hard limit
    if len(content) > 300:
        content = content[:300] + "..."
    return content.strip()


def _render_permanent_section(permanent: list) -> str:
    """Render permanent memory items into a system prompt section.
    Shared by _load_memory_for_prompt."""
    if not permanent:
        return ""
    lines = ["### [用户长期记忆]"]
    # Normalize old string format to dict, default missing type to "memory"
    normalized = []
    for item in permanent:
        if isinstance(item, str):
            normalized.append({"type": "memory", "content": item})
        elif isinstance(item, dict):
            normalized.append({**item, "type": item.get("type", "memory")})
    # Task items first (skip empty content — cleared task slot)
    task_items = [item for item in normalized if item.get("type") == "task" and item.get("content")]
    memory_items = [item for item in normalized if item.get("type") == "memory"]
    if task_items:
        lines.append(f"📋 当前任务：{_sanitize_memory_content(task_items[0].get('content', ''))}")
    if memory_items:
        lines.append("以下内容用户特别强调，必须始终遵守：")
        for i, item in enumerate(memory_items, 1):
            lines.append(f"{i}. {_sanitize_memory_content(item.get('content', str(item)))}")
    lines.append(f"（共{len(normalized)}/10条，使用 disk 添加/删除）")
    return "<!--USER_MEMORY_START-->\n" + "\n".join(lines) + "\n<!--USER_MEMORY_END-->"

# 修复Windows控制台编码问题
if sys.platform == 'win32':
    # 确保stderr使用UTF-8编码
    if not isinstance(sys.stderr, io.TextIOWrapper) or sys.stderr.encoding != 'utf-8':
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    if not isinstance(sys.stdout, io.TextIOWrapper) or sys.stdout.encoding != 'utf-8':
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from agent.tool_registry import get_registry  # noqa: E402

from .generic.agent_loop import StreamEvent, agent_runner_loop  # noqa: E402
from .handler import NiuHandler  # noqa: E402
from .injector.sync import get_skill_sync  # noqa: E402

from .decay_pool import DecayPool  # noqa: E402

# 动态块框架标记头（D19）：动态注入 + Current Time 以 role=user 消息承载，
# 全 provider 统一（中途 system 角色在国产网关无官方背书，user 载体零兼容风险）。
# 同时作为幂等移除的识别锚——配合与上一轮注入文本全等判定，用户输入原文
# 含此字样也不会被误删。
_DYNAMIC_BLOCK_HEADER = "[系统动态信息]"


def get_system_prompt() -> str:
    """获取系统提示词（向后兼容：静态段 + Current Time）。

    注意：此函数保留向后兼容。新的 cache 逻辑应直接用
    NiuRunner._build_static_system_prompt() 获取静态段，
    Current Time 由调用方在动态段开头拼接。
    """
    sys_prompt = NiuRunner._build_static_system_prompt()
    now = datetime.now()
    sys_prompt += f"\n\nCurrent Time: {now.strftime('%Y-%m-%d %H:%M:%S')}"
    return sys_prompt


def _load_memory_for_prompt() -> str:
    """从 memory.json 加载身份设定和用户偏好，格式化为提示词"""
    memory_path = Path.home() / ".niu" / "memory.json"
    if not memory_path.exists():
        return ""

    try:
        # 加锁读，避免与 memory-server 写并发时拿到半写文件
        # memory-server 可能未加载（如单元测试环境），失败则降级为 nullcontext
        try:
            from niu_memory_server import _memory_file_lock
            lock_ctx = _memory_file_lock
        except ImportError:
            import contextlib
            lock_ctx = contextlib.nullcontext()

        with lock_ctx:
            memory = json.loads(memory_path.read_text(encoding="utf-8"))
    except Exception:
        return ""

    parts = []

    # 身份设定
    identity = memory.get("identity", {})
    if identity:
        name = identity.get("name", "妞妞")
        personality = identity.get("personality", [])
        greeting_style = identity.get("greetingStyle", "")

        identity_str = f"## 身份设定\n\n你的名字是 **{name}**。"
        if personality:
            identity_str += f"\n性格特质：{'、'.join(personality)}。"
        if greeting_style:
            identity_str += f"\n问候风格：{greeting_style}。"
        parts.append(identity_str)

    # 工作目录
    workspace = memory.get("workspace", {})
    ws_path = workspace.get("path", "")
    if ws_path and not str(ws_path).startswith("请询问"):
        parts.append(f"## 工作目录\n\n知识库目录：{ws_path}")

    # 用户信息
    user = memory.get("user", {})
    user_lines = []
    if user.get("name") and not str(user["name"]).startswith("请询问"):
        user_lines.append(f"真实姓名：{user['name']}")
    if user.get("nickname") and not str(user["nickname"]).startswith("请询问"):
        user_lines.append(f"称呼：{user['nickname']}")
    if user.get("occupation") and not str(user["occupation"]).startswith("请询问"):
        user_lines.append(f"职业：{user['occupation']}")
    if user.get("organization") and not str(user["organization"]).startswith("请询问"):
        user_lines.append(f"工作单位：{user['organization']}")
    if user_lines:
        user_str = "## 用户信息\n\n" + "\n".join(user_lines)
        parts.append(user_str)

    # 用户长期记忆（驻留在 system prompt，最多10条(1 task + 9 memory)，每条≤200 token）
    permanent = memory.get("permanent", [])
    perm_str = _render_permanent_section(permanent)
    if perm_str:
        parts.append(perm_str)

    # 首次使用引导（firstRun）
    # 如果 memory.json 中存在 firstRun 字段，说明用户尚未完成初始设置
    # AI 应主动询问用户工作目录，并帮助用户完成 memory.json 的写入
    if memory.get("firstRun"):
        parts.append(
            "## 首次使用\n\n"
            "用户尚未完成初始设置。你需要主动询问用户工作目录路径。\n"
            "请说：\"嗨！我是妞妞。为了帮你管理知识，请告诉我你的工作目录想放在哪里？\"\n\n"
            "同时告诉用户：如果愿意提供真实姓名、称呼、职业、工作单位，"
            "你能更专业地帮助整理知识库和记录日志，也能根据真实姓名维护人际关系网。"
            "这些信息可以现在提供，也可以以后慢慢告诉我。\n\n"
            "用户回答工作目录路径后，你需要用 bash 工具完成以下操作：\n"
            "1. 创建目录（如果不存在）\n"
            "2. 写入 ~/.niu/memory.json：设置 workspace.path，将 firstRun 设为 false\n\n"
            "工作目录是必填项——没有工作目录系统无法正常工作，只有写入了真实的 "
            "workspace.path 才能把 firstRun 关闭。\n\n"
            "如果用户同时提供了姓名/称呼/职业/工作单位，一并更新到 memory.json 的 "
            "user.name / user.nickname / user.occupation / user.organization 字段；"
            "如果用户没提供，保留这 4 个字段的占位符不变，不要主动修改。\n\n"
            "完成后，下次对话不再出现此提示。"
        )

    return "\n\n".join(parts)


def get_tools_schema(include_main_only: bool = True) -> list:
    """获取工具 Schema（从 JSON 文件加载 + 注册子 Agent 工具）

    子 Agent 名单来源：
    1. config/agents/niu.md 的 sub agents 字段（专用子 Agent）
    2. ~/.niu/agents/*.md 扫描（通用子 Agent，主 Agent 运行时创建）

    跳过条件（方式 B：不允许坏工具让主 Agent 看到）：
    - 文件名非 kebab-case（避免工具名含空格/大写）
    - MD 文件不存在
    - frontmatter 为空或解析失败（YAML 错误）
    - frontmatter 非 dict（纯字符串/列表等非标量值，视为无效 MD）
    - description 字段缺失（视为无效子 Agent）
    - visibility: hidden（后台专用子 Agent，不注册 chat-with 工具；程序按名直调不受影响）

    重算返回完整 base 集（基础工具 + MCP 工具 + 所有 chat-with-* + check_subagent_progress）。

    Args:
        include_main_only: 是否包含主 Agent 专用工具（如 check_subagent_progress）。
            主 Agent 路径传 True（默认），子 Agent 路径传 False。
    """
    from .subagent import _USER_AGENTS_DIR, _resolve_agent_md_path, get_subagent_config
    from .tool_registry import get_registry

    script_dir = os.path.dirname(os.path.abspath(__file__))
    schema_path = os.path.join(script_dir, "generic", "assets", "tools_schema.json")

    tools = []
    if os.path.exists(schema_path):
        with open(schema_path, encoding="utf-8") as f:
            tools = json.load(f)

    # 1. 从 niu.md 读专用子 Agent 名单
    try:
        niu_config = get_subagent_config("niu")
        sub_agents = list(niu_config.get("sub agents", []))
    except Exception as e:
        logger.warning(f"Failed to load niu.md sub agents config: {e}")
        sub_agents = []

    # 2. 扫描 ~/.niu/agents/*.md 加通用子 Agent 名单
    user_agent_names = []
    if os.path.isdir(_USER_AGENTS_DIR):
        for fname in os.listdir(_USER_AGENTS_DIR):
            if not fname.endswith(".md") or fname.startswith("_"):
                continue
            # 跳过子目录（仅处理文件）
            fpath = os.path.join(_USER_AGENTS_DIR, fname)
            if not os.path.isfile(fpath):
                continue
            user_agent_names.append(os.path.splitext(fname)[0])

    # 3. 合并去重（保序：专用在前，通用在后）
    all_subagents = list(dict.fromkeys(sub_agents + user_agent_names))

    # 4. 收集已加载的 MCP 服务器名（用于 warning 提示未加载的服务器）
    try:
        registry = get_registry()
        loaded_servers = set(registry._server_tools.keys())
    except Exception:
        loaded_servers = None  # 不做 warning

    # 5. 为每个名字生成 chat-with-{name} schema
    for agent_name in all_subagents:
        # 5a. 文件名 kebab-case 校验
        if not _KEBAB_CASE_RE.match(agent_name):
            logger.warning(
                f"Sub-agent '{agent_name}' name not kebab-case, skipping "
                f"(use lowercase + hyphens like 'photo-organizer')"
            )
            continue

        # 5b. MD 文件存在性
        md_path = _resolve_agent_md_path(agent_name)
        if md_path is None:
            logger.warning(f"Sub-agent '{agent_name}' MD file not found, skipping")
            continue

        # 5c. frontmatter 解析
        try:
            agent_config = get_subagent_config(agent_name)
        except Exception as e:
            logger.warning(f"Sub-agent '{agent_name}' config parse error: {e}, skipping")
            continue
        # 5d. frontmatter 必须是 dict（纯字符串/列表等非标量值视为无效 MD，warn+skip——
        #     缺此守卫下方 .get() 抛 AttributeError 逃逸出循环 → get_tools_schema 整体崩溃）
        if not isinstance(agent_config, dict):
            logger.warning(
                f"Sub-agent '{agent_name}' has non-dict frontmatter "
                f"({type(agent_config).__name__}), skipping (bad MD)"
            )
            continue

        # 5e. visibility: hidden → 不注册 chat-with 工具（后台专用子 Agent，仅程序按名直调）
        if agent_config.get("visibility") == "hidden":
            continue

        # 5f. frontmatter 非空 + description 存在
        if not agent_config:
            logger.warning(
                f"Sub-agent '{agent_name}' has empty/invalid frontmatter, skipping (bad MD)"
            )
            continue
        if "description" not in agent_config or not agent_config["description"]:
            logger.warning(
                f"Sub-agent '{agent_name}' missing description field, skipping"
            )
            continue

        # 5g. MCP 服务器未加载 warning（不阻塞）
        mcp_servers = agent_config.get("mcpServers", []) or []
        if loaded_servers is not None:
            for s in mcp_servers:
                if s not in loaded_servers:
                    logger.warning(
                        f"Sub-agent '{agent_name}' references unloaded MCP server '{s}', "
                        f"its tools will be unavailable"
                    )

        desc = agent_config.get("description")

        # 阶段二：根据 allowAsync 决定是否暴露 async_mode
        allow_async = bool(agent_config.get("allowAsync", False))

        properties = {
            "task": {
                "type": "string",
                "description": "任务描述（回复路径可传空字符串）",
            },
            "answer": {
                "type": "string",
                "description": "回复子 Agent 的 @niu-agent 问题（含 @子名 前缀）",
            },
            "unique_name": {
                "type": "string",
                "description": "子 Agent 唯一名。同步调用（chat-with-xxx）时可省略，默认用 agent 名（如 browser-operator）；异步调用时为 agent 名+4位 hex 后缀（如 file-processor-a1b2，来自派单确认）。异步续跑（仅 async_mode=true 生效；同步调用忽略 unique_name）：传上次派发确认中的唯一名，若 24h 内有同名完成存档则加载其上轮上下文续跑旧工作（任务中应声明与上次要求的差异）；无同名存档则全新派发",
            },
        }
        if allow_async:
            properties["async_mode"] = {
                "type": "boolean",
                "description": (
                    "是否异步调用。true=后台运行，立即返回派单确认（含子 Agent 唯一名）；"
                    "false（默认）=同步阻塞等结果。"
                    "异步调用后可用 check_subagent_progress 查进度、@子名 消息补充上下文、@子名 /stop 停止。"
                ),
                "default": False,
            }

        tools.append(
            {
                "type": "function",
                "function": {
                    "name": f"chat-with-{agent_name}",
                    "description": desc,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": [],
                    },
                },
            }
        )

    # 阶段二：主 Agent 的 check_subagent_progress 工具（主 Agent 专用，子 Agent 不可见）
    if include_main_only:
        tools.append({
            "type": "function",
            "function": {
                "name": "check_subagent_progress",
                "description": (
                    "查看异步子 Agent 的进度。返回子 Agent 最近一轮 LLM 对话（请求摘要、回复、当前轮次、最近工具）。"
                    "用于监控后台运行的子 Agent。同步子 Agent 无进度数据。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "subagent_name": {
                            "type": "string",
                            "description": "子 Agent 唯一名（同步：browser-operator；异步：file-processor-a1b2，来自派单确认或动态注入区）",
                        },
                    },
                    "required": ["subagent_name"],
                },
            },
        })

    # 阶段二b：主 Agent 的 ask_user 工具（显式暂停问话，工作流不中断；子 Agent 不可见）
    if include_main_only:
        tools.append({
            "type": "function",
            "function": {
                "name": "ask_user",
                "description": (
                    "在工具循环执行过程中向用户提问并等待回答。仅当你正在执行工具调用链"
                    "（含同步子 Agent 挂起期间需征求用户意见）、且必须拿到用户确认/信息/决策才能继续时使用——"
                    "调用后暂停等待用户输入（不退出当前工作流），收到回答后继续原任务，"
                    "回答以 [user 回答] 形式返回。普通对话中想向用户提问时，直接在回复文本中提问即可，不要调用本工具。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "要向用户提出的问题",
                        },
                    },
                    "required": ["question"],
                },
            },
        })

    return tools


def create_client(config: dict[str, Any]):
    """创建 LLM 客户端（统一使用LiteLLM）"""
    cfg = {
        "apikey": config.get("apikey") or config.get("api_key", ""),
        "apibase": config.get("apibase") or config.get("api_base", ""),
        "model": config.get("model", ""),
        "api_type": config.get("type", "openai"),
    }
    if "temperature" in config and config["temperature"] is not None:
        cfg["temperature"] = config["temperature"]
    if "reasoning_effort" in config and config["reasoning_effort"] is not None:
        cfg["reasoning_effort"] = config["reasoning_effort"]
    if config.get("max_tokens") is not None:
        cfg["max_tokens"] = config["max_tokens"]
    cfg["provider"] = config.get("provider", "")
    cfg["litellm_kwargs"] = config.get("litellm_kwargs", {})
    # sticky routing id 纯管道透传（spec §3.1——白名单构造转发，零 sticky 逻辑）
    cfg["sticky_session_id"] = config.get("sticky_session_id")
    cfg["read_timeout"] = config.get("read_timeout") or 300

    from .generic.litellm_adapter import create_litellm_client
    logger.info(f"Using LiteLLM adapter for model: {cfg['model']}")
    return create_litellm_client(cfg)


def format_resources_for_prompt(results: list, title: str = "相关资源") -> str:
    """
    格式化资源为提示词注入格式

    格式：
    ### [相关资源]
    1. **name** (分数: 87)
       完整内容...
    """
    if not results:
        return ""

    lines = [f"\n\n### [{title}]"]
    for i, r in enumerate(results, 1):
        score_pct = int(r.score * 100)
        name = r.metadata.get("name", "")

        if name:
            lines.append(f"{i}. **{name}** (分数: {score_pct})")
            # Skills 等其他类型，注入 L1 摘要 + 文件路径（指针）
            lines.append(f"   {r.content}")
            source = r.metadata.get("source", "")
            if source:
                lines.append(f"   文件路径: {source}")
        else:
            lines.append(f"{i}. {r.content} (分数: {score_pct})")

        return "\n".join(lines)


def _build_session_chain_ops(
    dates: list[str],
    existing_edges: dict[tuple[str, str], set[str]],
    max_days: int = 10,
    today: date | None = None,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """纯函数：计算会话日期链补链操作（断跨越边 + 补相邻边）。

    Args:
        dates: 排序后的会话实体名列表（YYYY-MM-DD会话，字典序=时间序）。
        existing_edges: {(src, tgt): set(keywords)} 两实体间已存在边（keywords 集合）。
        max_days: 日历天窗口（含今天）。
        today: 测试注入日期；缺省用系统日期。

    Returns:
        (deletes, creates)：要断开的跨越边对、要补的相邻边对（src=先, tgt=后）。
    """
    from datetime import timedelta

    today = today or date.today()
    cutoff = (today - timedelta(days=max_days - 1)).isoformat()
    in_window = [d for d in dates if d >= cutoff]
    if len(in_window) < 2:
        return [], []

    deletes: list[tuple[str, str]] = []
    creates: list[tuple[str, str]] = []
    # 断开跨越边：i、j 之间还有中间日期实体，且长边仅 followed_by（无其他语义边）
    for i in range(len(in_window)):
        for j in range(i + 2, len(in_window)):
            pair = (in_window[i], in_window[j])
            kws = existing_edges.get(pair)
            if kws and kws <= {"followed_by"}:
                deletes.append(pair)
    # 补相邻边：相邻存在日之间缺 followed_by 则补
    for i in range(len(in_window) - 1):
        pair = (in_window[i], in_window[i + 1])
        if pair not in existing_edges:
            creates.append(pair)
    return deletes, creates


class NiuRunner:
    """
    Niu Agent Runner

    简化的 Agent 运行器，直接使用 GenericAgent 组件。
    集成动态注入：Skills 按语义注入提示词，MCP 工具按分数动态注入 tools_schema。
    """

    @staticmethod
    def _build_static_system_prompt() -> str:
        """构建静态系统提示词段（cache 友好）。


        只包含 niu.md 正文，是 prompt cache 的前缀，字节稳定。
        memory 派生段（identity/workspace/user/permanent/firstRun）由
        _on_before_llm 每轮从 memory.json 重读生成，拼入 system 静态区（D17：
        字节级几乎恒定，变化仅发生在记忆编辑时）；injection+Current Time 走
        role=user 动态块（D19），不进 system。
        """
        script_dir = os.path.dirname(os.path.abspath(__file__))

        # 1. 读取 niu.md
        sys_prompt = ""
        niu_md_path = os.path.join(script_dir, "..", "config", "agents", "niu.md")
        if os.path.exists(niu_md_path):
            with open(niu_md_path, encoding="utf-8") as f:
                content = f.read()
                if content.startswith("---"):
                    parts = content.split("---", 2)
                    if len(parts) >= 3:
                        try:
                            import yaml as _yaml
                            config = _yaml.safe_load(parts[1])
                            if config and config.get("description"):
                                sys_prompt = config["description"].strip() + "\n\n"
                        except Exception:
                            pass
                        sys_prompt += parts[2].strip()
                else:
                    sys_prompt = content

        if not sys_prompt:
            sys_prompt = "# Role: Niu Agent\nYou are a helpful assistant with file and code access."

        return sys_prompt

    def __init__(self, llm_config: dict[str, Any], mcp_client=None):
        # 从 niu.md front matter 读取 temperature，覆盖到 llm_config
        from .subagent import get_subagent_config
        niu_config = get_subagent_config("niu")
        if niu_config.get("temperature") is not None:
            llm_config = {**llm_config, "temperature": niu_config["temperature"]}
        # sticky routing id（spec §3.1 唯一接线点）：主 Agent 固定 "main"（单用户单活跃对话）。
        # 常量键不参与 get_or_create_runner 配置比对——无重建循环风险。
        llm_config = {**llm_config, "sticky_session_id": "main"}

        self.llm_config = llm_config
        self.mcp_client = mcp_client
        self.client = create_client(llm_config)
        # 当前模型名（用于 _assemble_system_message 判断是否 Claude 走 cache_control）
        self.default_model = llm_config.get("model", "")
        project_root = os.path.dirname(os.path.dirname(__file__))
        self.handler = NiuHandler(cwd=project_root, mcp_client=mcp_client)

        # 静态段：niu.md（cache 友好，字节稳定）；memory 段由 _on_before_llm
        # 每轮重读后拼入 system 静态区（D17）
        self.static_system_prompt = self._build_static_system_prompt()
        # base_system_prompt 将在 disk_desc 拼接完成后组装（向后兼容）
        # 阶段三：跟踪 ~/.niu/agents/ 已知子 Agent 文件集合
        # chat() 入口用此集合判断是否需要重算 base_tools_schema
        from .subagent import _USER_AGENTS_DIR
        if os.path.isdir(_USER_AGENTS_DIR):
            self._known_user_subagents = {
                f for f in os.listdir(_USER_AGENTS_DIR)
                if f.endswith(".md") and not f.startswith("_")
            }
        else:
            self._known_user_subagents = set()
        self.base_tools_schema = get_tools_schema()

        # 启动 Skills 后台同步
        get_skill_sync(auto_start=True)

        # MCP 工具列表（启动时加载，缓存）
        self._mcp_tools_schema: list = []

        # DiskEngine（虚拟磁盘命令引擎）
        from niu_api.internal.disk_engine import DiskEngine
        bundle_disk_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "disk")
        user_disk_dir = os.path.expanduser("~/.niu/disk")
        self.disk_engine = DiskEngine([bundle_disk_dir, user_disk_dir], registry=None)
        self.handler = NiuHandler(cwd=project_root, mcp_client=mcp_client, disk_engine=self.disk_engine)


        # 动态前缀段：disk_desc（磁盘结构启动时固定；disk_desc 自带 \n\n 开头，
        # 空时为空串）。D17 起并入 system 静态区；Current Time 不再进 system，
        # 由 _build_dynamic_block 每轮实时生成于 role=user 动态块末尾
        disk_desc = self._build_disk_description()
        self.dynamic_system_prefix = disk_desc

        # 向后兼容：base_system_prompt = 静态段 + 动态前缀段（不含 injection，不含 memory 段）
        self.base_system_prompt = self.static_system_prompt + self.dynamic_system_prefix

        self._current_channel_id = ""
        self._im_channel_id = ""  # IM 通道继承：记录最近真实用户消息/定时任务的 channel_id
        self._im_force = False  # IM 强制标志：定时任务/后台触发置 True；仅 Electron 用户消息置 False
        self._request_source = "user"  # 当前请求来源（scheduler/ha-watcher 触发时 chat_queue 置 "scheduler"；停止隔离用）

        # 首轮 resources 注入（拖入文件模式要求），_on_before_llm turn==1 时合并进 injection 后清空
        self._first_turn_extra_injection: str = ""
        # 当前对话在途的动态块文本（D19 role=user 载体），_refresh_dynamic_user_block 幂等移除锚
        self._active_dynamic_block: str = ""

        # Brain context injector chain (lazy-cached, created once per runner)
        self._brain_adapter = None      # LightRAGAdapter
        self._brain_ingester = None     # LightRAGIngester
        self._brain_region_mgr = None   # RegionManager
        self._brain_injector = None     # BrainContextInjector
        self._brain_injector_failed = False  # E3-07：re-check 脑区上下文不可用标记（_inject_dynamic_resources getattr 守卫消费）
        self._cached_activation_mgr = None  # RegionActivationManager (for cache invalidation)
        self._last_forced_sync_fail_time: float = 0.0  # forced sync 失败冷却时间戳
        self._forced_sync_running = threading.Event()  # forced sync 后台线程运行标志，避免并发启动多个 daemon

        # Decay pool (Ebbinghaus forgetting curve)
        self._decay_pool = DecayPool()

        # 注入 ask_agent callback（供内部 MCP Server 调用 LLM）
        _registry = get_registry()
        _registry.set_ask_agent(self._make_ask_agent_callback())

        # 初始化 MCPClientManager 并连接外部 MCP 服务器
        from agent.mcp_client import MCPClientManager, make_sampling_callback
        self._ext_mcp_client = MCPClientManager(sampling_callback=make_sampling_callback())
        _registry.set_mcp_client(self._ext_mcp_client)
        # 注意：_connect_external_servers 是 async，需要在 async 上下文中调用
        # 这里暂时不调用，由 lifespan 的 startup 事件触发
    def set_im_channel(self, channel_id: str) -> None:
        """设置/清除 IM 通道。必须在 _chat_lock 持有时调用。"""
        self._im_channel_id = channel_id

    def get_im_channel(self) -> str:
        return self._im_channel_id

    def set_im_force(self, value: bool) -> None:
        """设置/清除 IM 强制标志。定时任务/后台触发置 True；Electron 用户消息置 False。"""
        self._im_force = value

    def get_im_force(self) -> bool:
        return self._im_force

    def should_push_im(self) -> bool:
        """IM 推送统一判定（全局唯一入口——用户拍板：所有消费点统一调用，禁止内联复制判定式）。

        True = IM 用户消息置的 channel_id 或定时任务/后台触发置的 force 任一在。
        消费点：compat.py chat_session 推送闸门 / chat_queue.py scheduler 特判 / runner 流式三处。
        注意读 _im_channel_id（粘性，回合外仍有效）而非 _current_channel_id（回合结束清空）。"""
        return bool(self._im_channel_id or self._im_force)

    def _refresh_base_tools_schema_if_dirty(self):
        """每次对话开始时扫 ~/.niu/agents/，发现新 MD 就重算 base_tools_schema。

        重算返回完整 base 集（基础工具 + MCP 工具 + 所有 chat-with-* + check_subagent_progress），
        不是差量重算。无变化时不重算（保持对象引用稳定，避免无谓拷贝）。
        """
        from .subagent import _USER_AGENTS_DIR
        if not os.path.isdir(_USER_AGENTS_DIR):
            return

        current_files = {
            f for f in os.listdir(_USER_AGENTS_DIR)
            if f.endswith(".md") and not f.startswith("_")
        }

        if current_files != self._known_user_subagents:
            self._known_user_subagents = current_files
            self.base_tools_schema = get_tools_schema()
            logger.info(
                f"Refreshed base_tools_schema: {len(self.base_tools_schema)} tools "
                f"(~/.niu/agents/ changed)"
            )

    def _make_ask_agent_callback(self):
        """创建 ask_agent 回调，调用当前 Agent 的 LLM"""
        def ask_agent(prompt: str, system_prompt: str = "", max_tokens: int = 500) -> str | None:
            try:
                messages = []
                if system_prompt:
                    messages.append({"role": "system", "content": system_prompt})
                messages.append({"role": "user", "content": prompt})
                response = self.client.chat.completions.create(
                    model=self.llm_config["model"],
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=0.2,
                )
                return response.choices[0].message.content
            except Exception as e:
                logger.error(f"ask_agent failed: {e}")
                return None
        return ask_agent

    async def _connect_external_servers(self):
        """连接外部 MCP 服务器（stdio/HTTP 模式）"""
        try:
            from agent.mcp_loader import load_external_servers
            await load_external_servers(self._ext_mcp_client)
        except Exception as e:
            logger.warning(f"Failed to connect external MCP servers: {e}")

    async def startup(self):
        """异步启动，连接外部 MCP 服务器。由 lifespan startup 事件调用。"""
        await self._connect_external_servers()

    def set_mcp_tools_schema(self, tools: list):
        """Set MCP tool schemas — in disk mode, only inject disk() schema.

        All MCP tools are visibility=hidden and accessed via disk().
        The tools list is stored for potential future lookups.
        Also passes registry to DiskEngine for cross-validation and tool lookup.
        """
        self._mcp_tools_schema = tools  # Store for schema lookups
        # Pass registry to DiskEngine for cross-validation and tool lookup
        try:
            from agent.tool_registry import get_registry
            registry = get_registry()
            self.disk_engine.executor.registry = registry
            self.disk_engine._registry = registry
            self.disk_engine.config._cross_validate_registry(registry)
        except Exception as e:
            logger.warning(f"DiskEngine registry binding failed: {e}")
        logger.info(f"Loaded {len(tools)} MCP tools (all hidden, accessed via disk)")

    def _extract_context_from_messages(self, messages: list) -> str:
        """从 messages 列表提取上下文用于向量检索。

        策略：最近 2 条**对话**消息（user/assistant 各至多 1 条，跳过 tool），
        ≤80字符全量放入，>80字符从80位置往后找自然断点
        （优先句末标点→段落换行→逗号），最大200字符。assistant 附带最多5个工具名。
        """
        context_parts = []
        # 从尾部向前取最近 1 条 user + 1 条 assistant（各至多一次，跳过 tool）。
        # tool 消息只是 assistant 的副产物；工具循环可能连续多条 assistant
        # （asst→tool→asst→tool），按"取 2 条"会挤掉 user 意图——各角色
        # 至多 1 条保证 user 意图恒在场 + 最近 assistant 的工具名
        # （2026-08-09 修复：Minimax H3 skill 第二轮丢失根因）。
        recent: list[dict] = []
        for msg in reversed(messages):
            role = msg.get("role")
            if role not in ("user", "assistant"):
                continue
            if any(existing.get("role") == role for existing in recent):
                continue  # 该角色已收集，跳过（各至多一次）
            recent.append(msg)
            if len(recent) == 2:
                break
        recent.reverse()

        for msg in recent:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "user" and content:
                if content.startswith("工具调用成功") or content.startswith("Tool call succeeded"):
                    # 取首行（摘要行），再用 _smart_truncate 断点截断
                    line = content.split("\n")[0]
                    context_parts.append(f"{role}: {_smart_truncate(line)}")
                else:
                    context_parts.append(f"{role}: {_smart_truncate(content)}")
            elif role == "assistant" and content:
                context_parts.append(f"{role}: {_smart_truncate(content)}")

            if role == "assistant":
                for tc in msg.get("tool_calls", [])[:5]:
                    name = tc.get("function", {}).get("name", "")
                    if name:
                        context_parts.append(f"tool: {name}")

        return "\n".join(context_parts) if context_parts else ""


    def _assemble_system_message(
        self,
        messages: list,
        memory_section: str,
        injection: str,
        model: str,
    ) -> str:
        """组装 system message 静态区并产出动态块文本（D17 缓存友好排布）。

        原地修改 messages[0]["content"] 为**静态区**：static_system_prompt + disk_desc
        + memory_section——三者均低频变化（disk 启动固定；memory 每轮重读但字节级几乎
        恒定，变化仅发生在记忆编辑时，一次重建属可接受代价），命中段可延伸至索引区末尾。

        - Claude 模型：content 为单 text 块 list 格式，末尾打 cache_control breakpoint。
        - 其他模型（火山方舟/DeepSeek/Qwen 等）：content 保持字符串格式，
          静态区在开头且字节稳定，靠服务端自动 prefix cache 命中。

        易变内容（injection + Current Time）不再进 system：由本方法以动态块文本返回，
        调用方经 _refresh_dynamic_user_block 以 role=user 载体插入（D19 全 provider 统一，
        复用 supplement 注入先例；时间在块内最后）。

        Args:
            messages: 消息列表，messages[0] 必须是 role=system
            memory_section: 本轮从 memory.json 重读的 memory 段（identity/workspace/user/permanent/firstRun）
            injection: 动态注入内容（skills/knowledge/brain region），仅用于生成动态块文本
            model: 当前模型名，用于判断是否 Claude

        Returns:
            动态块文本（「[系统动态信息]」头 + injection + Current Time）；messages[0]
            非 system 时返回 ""。插入位置与幂等移除由调用方决定。
        """
        if not messages or messages[0].get("role") != "system":
            return ""

        # 静态区 = 静态指令 + disk_desc（dynamic_system_prefix 自带 \n\n 开头，空时为空串）
        #          + memory_section
        static_text = self.static_system_prompt + self.dynamic_system_prefix
        if memory_section:
            static_text += "\n\n" + memory_section

        model_lower = (model or "").lower()
        if "claude" in model_lower:
            # Claude：单 text 块 list 格式 + cache_control breakpoint
            messages[0]["content"] = [
                {
                    "type": "text",
                    "text": static_text,
                    "cache_control": {"type": "ephemeral"},
                },
            ]
        else:
            # 其他模型：字符串格式，静态区在开头
            messages[0]["content"] = static_text

        return self._build_dynamic_block(injection)

    def _build_dynamic_block(self, injection: str) -> str:
        """构建动态块文本：框架标记头 + injection + 暂存提醒 + 使用率仪表盘 + Current Time（时间最后）。"""
        text = _DYNAMIC_BLOCK_HEADER
        if injection:
            text += "\n" + injection.strip()
        text += self._park_reminder_line()
        # 上下文使用率仪表盘（fold spec §5）：M2-F1 真值化——优先 LLM API 真值
        # prompt_tokens（每轮响应后更新；0/缺失 → usage=None 落估算兜底），None/空行跳过。
        # 动态块在缓存前缀之外，允许每轮刷新；失败不影响本轮对话
        try:
            from agent import context_manager as _cm_mod
            from agent.subagent import _read_context_window_tokens
            _cm = _cm_mod.peek_context_manager()
            if _cm is not None:
                truth = getattr(getattr(self, "handler", None), "_last_prompt_tokens", 0) or 0
                window = _read_context_window_tokens()
                usage = (truth / window) if (truth > 0 and window and window > 0) else None
                line = _cm.get_fold_dashboard_line(usage_override=usage)
            else:
                line = ""
        except Exception:
            line = ""
        if line:
            text += "\n" + line
        text += f"\n\nCurrent Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        return text

    def _park_reminder_line(self) -> str:
        """读取 memory.json 的 parked 数组，生成常驻暂存提醒行（无暂存返回空串）。

        锁与路径形态 R1-P1 修正（裸写 _memory_file_lock 在 runner 作用域必 NameError 且被 except 吞）：
        镜像 _load_memory_for_prompt（runner.py L292-306）的函数内 try-import 回退
        """
        try:
            # R2-A P1：一并导入路径函数，保测试隔离（test_user_memory.py L54-58 经
            # monkeypatch MEMORY_JSON_PATH + _reset_memory_json_path 重定向）；ImportError 才回退
            from niu_memory_server import _memory_file_lock, _get_memory_json_path
            memory_path = _get_memory_json_path()
        except ImportError:
            from contextlib import nullcontext
            _memory_file_lock = nullcontext()
            memory_path = Path.home() / ".niu" / "memory.json"
        if not memory_path.exists():
            return ""  # 全新环境无 memory.json 是正常态（R2-A：缺此守卫会每轮 FileNotFoundError→warning 刷屏）
        try:
            with _memory_file_lock:
                data = json.loads(memory_path.read_text(encoding="utf-8"))  # R3-A：照 runner L307 先例 read_text，避免 open().read() 不关句柄每轮 fd 泄漏
            parked = data.get("parked") or []
            if not parked:
                return ""
            items = "".join(f" {chr(0x2460+i)}〈{p.get('summary','')}〉" for i, p in enumerate(parked))
            return f"\n[暂存事项] {len(parked)} 项：{items.strip()}——用户提起时调 disk(\"/memory/conversation_recall 序号\") 召回处理"
        except Exception as e:
            logger.warning(f"[暂存提醒] 读取失败（降级不显示）: {e}")  # R1：禁止静默吞——架空常驻提醒语义
            return ""

    def _refresh_dynamic_user_block(self, messages: list, dynamic_text: str) -> None:
        """移除上一轮动态块并以 role=user 插入新块（最后一个 user 消息之前）。

        插入位取「最后一个 role=user 消息之前」：轮首当前输入在尾部 → 动态块紧贴其前
        （[-2]=动态块 / [-1]=输入）；轮中工具循环时尾部是 tool 结果，动态块仍锚定在
        本轮 user 输入的语义位之前。supplement/next_prompt 已在本回调之前的上一轮迭代末
        由 agent_loop 合并追加为 user 消息（轮末统一 append），因此本回调执行时动态块
        恰好插在它们之前。

        幂等识别锚：消息 content 与上一轮注入文本**全等**且以框架标记头开头——用户输入
        原文即使以「[系统动态信息]」开头也不含实时时间戳、不可能全等，绝不误删。

        Args:
            messages: agent_runner_loop 的消息列表引用（原地修改）
            dynamic_text: _assemble_system_message 返回的动态块文本；空串只做移除不插入
        """
        prev = getattr(self, "_active_dynamic_block", "")
        if prev:
            for idx, m in enumerate(messages):
                if (
                    m.get("role") == "user"
                    and isinstance(m.get("content"), str)
                    and m["content"].startswith(_DYNAMIC_BLOCK_HEADER)
                    and m["content"] == prev
                ):
                    del messages[idx]
                    break
        self._active_dynamic_block = dynamic_text
        if not dynamic_text:
            return
        insert_at = len(messages)
        for idx in range(len(messages) - 1, -1, -1):
            if messages[idx].get("role") == "user":
                insert_at = idx
                break
        messages.insert(insert_at, {"role": "user", "content": dynamic_text})


    def _on_before_llm(self, messages: list, turn: int) -> None:
        """每轮 LLM 调用前重读 memory.json + 刷新动态注入。

        每轮从 memory.json 重新构建 memory_section（identity/workspace/user/permanent/firstRun），
        保证 Agent 写入 memory.json 后下一轮 system prompt 立即感知。
        关键：在 client.chat 之前调，让本轮 LLM 立即读到新内容。
        原地修改 messages[0]（静态区）并刷新 role=user 动态块，无返回值。

        Args:
            messages: agent_runner_loop 的消息列表引用
            turn: 当前轮次（从 1 开始）
        """
        # 1. 每轮重读 memory.json（关键：解决 Agent 写入后下轮 system prompt 不更新的 bug）
        memory_section = _load_memory_for_prompt()

        # 2. 提取最近 2 条消息作为 context（保持原样，按用户原始设计）
        context = self._extract_context_from_messages(messages)
        injection, _ = self._inject_dynamic_resources(context)

        # C4 修复：首轮合并拖入文件的 resources 模式要求（chat() 存入实例属性）
        # chat() 把 resources 模式文本存入 self._first_turn_extra_injection，
        # 这里 turn==1 时合并进 injection，让首轮 LLM 能读到 mode=reference/move 指令
        if turn == 1 and getattr(self, "_first_turn_extra_injection", ""):
            injection += self._first_turn_extra_injection
            self._first_turn_extra_injection = ""  # 清空，防跨对话泄漏


        # 3. 组装静态区（messages[0] = 静态指令+disk_desc+memory，D17 缓存友好排布）
        #    并刷新动态块（injection+Current Time 以 role=user 插在最后一个 user 前，
        #    D19 全 provider 统一；先移除上一轮旧块防叠加），本轮 LLM 立即读到
        dynamic_text = self._assemble_system_message(messages, memory_section, injection, self.default_model)
        self._refresh_dynamic_user_block(messages, dynamic_text)
        # 4. 回填 system token 估算（Task 3：80% 触发判定的系统侧输入）。
        # 挂点选在组装出口：此时静态区已写好、动态块文本已知，
        # 计 [system]+[动态块] 尺寸即本轮注入面最终值。计数失败不阻塞对话
        # （缺省 0=未知，首轮回退偏保守）。
        try:
            from agent import context_manager as _cm_mod
            _cm = _cm_mod.peek_context_manager()
            if _cm is not None and messages and messages[0].get("role") == "system":
                counted = [messages[0]]
                if dynamic_text:
                    counted.append({"role": "user", "content": dynamic_text})
                _cm.set_system_token_estimate(
                    _cm_mod.ContextManager.count_tokens_simple(counted)
                )
        except Exception as e:
            logger.debug(f"[Runner] system token estimate backfill skipped: {e}")

    def _assemble_tools_schema(self) -> list:
        """组装 tools_schema = base tools + static MCP tools + disk（chat 与轮中刷新共用）。"""
        tools_schema = self.base_tools_schema.copy()
        try:
            from agent.tool_registry import get_registry
            registry = get_registry()
            for tool_name in registry.get_static_tools():
                schema = registry._schemas.get(tool_name)
                if schema:
                    tools_schema.append({
                        "type": "function",
                        "function": {
                            # 剥 server/ 前缀发裸名（对齐 subagent.py 模式）——OpenAI 规范
                            # function name 不允许 /，严格校验的服务（如 opencode zen）直接 400；
                            # dispatch 侧 handler.py 裸名自动解析全名，无需反向映射
                            "name": schema["name"].split("/", 1)[1] if "/" in schema["name"] else schema["name"],
                            "description": schema.get("description", ""),
                            "parameters": schema.get("input_schema", {"type": "object", "properties": {}}),
                        }
                    })
        except Exception as e:
            logger.debug(f"Static MCP tools injection skipped: {e}")
        disk_schema = self.disk_engine.get_schema()
        tools_schema.append(disk_schema)
        return tools_schema

    def _on_turn_end(self, messages: list, tools_schema: list, turn: int) -> list:
        """每轮循环结束后的清理工作（动态注入已移到 _on_before_llm）。

        保留：
        - 脑区衰减 decay_all：每轮降低脑区激活级别
        """
        # Decay brain region activation levels
        try:
            from agent.brain_tools import get_activation_mgr
            mgr = get_activation_mgr()
            if mgr is not None:
                mgr.decay_all()
        except Exception as e:
            logger.debug(f"Brain region decay failed: {e}")

        # Schema 刷新：失败退回原 tools_schema（不击穿工具循环）
        try:
            self._refresh_base_tools_schema_if_dirty()
            return self._assemble_tools_schema()
        except Exception as e:
            logger.warning(f"[Runner] schema refresh failed, keeping existing tools: {e}")
            return tools_schema

    def _ensure_session_chain(self, max_days: int = 10) -> None:
        """睡眠/dream 段收尾：补全会话日期链（只补边/断边，不建实体）。

        从已有 YYYY-MM-DD会话 实体取最近 max_days 日历天窗口：
        断开跳过中间实体的跨越边（安全前提：两实体间仅 followed_by），
        补全相邻日期的 followed_by 边。失败不抛出（收尾容错）。
        """
        try:
            from niu_api.internal.lightrag_adapter import LightRAGAdapter, LightRAGIngester

            adapter = LightRAGAdapter()
            result = adapter.list_entities_by_name_regex(r"^\d{4}-\d{2}-\d{2}会话$")
            if result.get("status") != "ok":
                logger.warning(f"[SessionChain]: list failed: {result.get('message')}")
                return
            names = sorted(e["entity_name"] for e in result.get("data", []))
            if not names:
                return

            # 收集窗口内所有 pair 的已有边（keywords 集合）
            from datetime import date, timedelta

            today = date.today()
            cutoff = (today - timedelta(days=max_days - 1)).isoformat()
            in_window = [n for n in names if n >= cutoff]
            if len(in_window) < 2:
                return
            existing_edges: dict[tuple[str, str], set[str]] = {}
            for i in range(len(in_window)):
                for j in range(i + 1, len(in_window)):
                    src, tgt = in_window[i], in_window[j]
                    if adapter.has_edge(src, tgt):  # 任意边（非仅 followed_by）
                        kws = adapter.get_edge_keywords_between(src, tgt)
                        existing_edges[(src, tgt)] = set(kws)

            deletes, creates = _build_session_chain_ops(
                in_window, existing_edges, max_days=max_days, today=today
            )
            for src, tgt in deletes:
                r = adapter.delete_relation(src, tgt)
                if r.get("status") != "ok":
                    logger.warning(f"[SessionChain]: break {src}->{tgt} failed: {r.get('message')}")
            if creates:
                ingester = LightRAGIngester()
                rels = [
                    {
                        "src_id": src,
                        "tgt_id": tgt,
                        "keywords": "followed_by",
                        "description": f"{src} 之后是 {tgt}",
                        "source_id": "dream_session_chain",
                        "file_path": "dream_session_chain",
                    }
                    for src, tgt in creates
                ]
                r = ingester.inject_custom_kg(
                    entities=[], relationships=rels, chunks=[], source_id="dream_session_chain"
                )
                if r.get("status") != "ok":
                    logger.warning(f"[SessionChain]: inject {len(rels)} edges failed: {r.get('message')}")
                else:
                    logger.info(f"[SessionChain]: broke {len(deletes)}, created {len(rels)} followed_by edges")
        except Exception as e:
            logger.warning(f"[SessionChain] failed: {e}")

    def _sync_get_messages(self, limit=None):
        """同步从 DB 读取消息（桥接 async MessageStore）

        Returns:
            Message 对象列表，或空列表（读取失败）
        """
        import asyncio

        from niu_api.chat import _main_loop

        from agent.session import get_message_store

        loop = _main_loop
        if loop is None or loop.is_closed():
            logger.warning("[Runner] No event loop available for sync DB read")
            return []

        async def _do():
            store = await get_message_store()
            return await store.get_messages(limit=limit)

        try:
            future = asyncio.run_coroutine_threadsafe(_do(), loop)
            return future.result(timeout=30.0)
        except Exception as e:
            logger.warning(f"[Runner] sync_get_messages failed: {e}")
            return []

    def _sync_delete_messages(self, msg_ids):
        """同步从 DB 删除消息（桥接 async MessageStore）

        Args:
            msg_ids: 要删除的消息 ID 列表

        Returns:
            删除结果 dict，或 None（失败）
        """
        import asyncio

        from niu_api.chat import _main_loop

        from agent.session import get_message_store

        loop = _main_loop
        if loop is None or loop.is_closed():
            logger.warning("[Runner] No event loop available for sync DB delete")
            return None

        async def _do():
            store = await get_message_store()
            return await store.delete_messages_by_ids(msg_ids)

        try:
            future = asyncio.run_coroutine_threadsafe(_do(), loop)
            return future.result(timeout=30.0)
        except Exception as e:
            logger.warning(f"[Runner] sync_delete_messages failed: {e}")
            return None

    def _sync_update_message(self, message_id, content, clear_tool_calls=False):
        """同步更新 DB 中的消息内容（桥接 async MessageStore）

        Args:
            message_id: 消息 UUID
            content: 新内容
            clear_tool_calls: 是否同时清空 tool_calls 字段

        Returns:
            bool 更新是否成功
        """
        import asyncio

        from niu_api.chat import _main_loop

        from agent.session import get_message_store

        loop = _main_loop
        if loop is None or loop.is_closed():
            logger.warning("[Runner] No event loop available for sync DB update")
            return False

        async def _do():
            store = await get_message_store()
            return await store.update_message(message_id=message_id, content=content, clear_tool_calls=clear_tool_calls)

        try:
            future = asyncio.run_coroutine_threadsafe(_do(), loop)
            return future.result(timeout=30.0)
        except Exception as e:
            logger.warning(f"[Runner] sync_update_message failed: {e}")
            return False

    def _on_context_high_usage(self, messages, tokens_used, tokens_limit) -> bool:
        """主 Agent 上下文超阈值回调 — 机械压实 + 原地回写新视图

        Task 3 定案：调 context_assembler.compaction 压实（纯机械、零 LLM、DB 不动），
        产出 [system 原样（含 cache_control）]+[索引消息]+[窗口 dict] 新视图后
        `messages[:] = new_view` 原地回写——当轮对话立即生效；不做 DB 重载
        （机械压实不改 DB，重载是 no-op 假动作）。与组装出口触发共用 AUTO_GATE
        滞回闸门（≥trigger 触发/<trigger−0.02 复位，线随配置 compactionTriggerRatio），同一轮次双触发去重不双压。
        agent_loop 不需要知道 DB、不需要导入 niu_api 的任何东西。

        Returns:
            True=闸门放行且压实完成（调用方据此进入本 loop 冷却）；
            False=闸门未放行（真值低于 80% 触发线——warningThreshold 与触发线
            的耦合区间，或同轮已被组装出口压实）或压实异常。False 时调用方
            不得置冷却，保留后续轮次检测（P2）。
        """

        logger.info(f"[Runner] Context high usage: {tokens_used}/{tokens_limit} tokens "
                     f"({tokens_used/tokens_limit:.1%})")

        # 广播压缩状态 started 事件（前端圆环动画启动）
        try:
            from niu_api.chat import notify_compact_status_sync
            notify_compact_status_sync("started", mode="auto")
        except Exception:
            pass

        usage_after = None
        compacted = False
        try:
            from agent.context_assembler import compaction

            # 真值比率过滞回闸门：与组装出口共用同一闸门，同轮去重
            ratio_now = tokens_used / tokens_limit if tokens_limit else 1.0
            if not compaction.AUTO_GATE.try_acquire(ratio_now):
                trigger = compaction.trigger_ratio()
                if ratio_now < trigger:
                    # warningThreshold(70%) < 触发线(80%)：真值未达线，无需压实。
                    # 返回 False 让 agent_loop 不置冷却、保留检测（P2：真值落在
                    # [warning, 80%) 区间时置冷却会导致本 loop 内检测停摆）
                    logger.info(f"[Runner] Compaction deferred: truth {ratio_now:.1%} below "
                                f"trigger line {trigger:.0%}")
                else:
                    logger.info("[Runner] Compaction skipped: gate latched by another trigger this round")
                return False
            db_messages = self._sync_get_messages()
            if not db_messages:
                logger.warning("[Runner] Compaction skipped: no DB messages available")
                try:
                    compaction.AUTO_GATE.release()  # 早退也须解除闩锁，避免此后永不再自动触发
                except Exception:
                    pass
                return False
            system_msg = messages[0] if messages and messages[0].get("role") == "system" else None
            new_view, stats = compaction.build_compact_view(db_messages, system_msg=system_msg)
            messages[:] = new_view  # 原地回写（agent_loop 契约：messages 为 dict 列表）
            # 压实成功即复位闩锁：压实后视图常落 [复位线, 触发线) 滞回带内，
            # 不复位则自动压实进程级失效（P1 修复）
            compaction.AUTO_GATE.release()
            compacted = True
            usage_after = stats.get("usage")
            # 压实后真值回填仪表盘缓存（M2-F2）：页面三级链/动态块改读它，不再用压实前旧估算
            if usage_after is not None:
                try:
                    from agent.context_manager import peek_context_manager
                    cm = peek_context_manager()
                    fs = getattr(cm, "_fold_stats", None) if cm is not None else None
                    if fs is not None:
                        fs["usage"] = usage_after
                except Exception:
                    pass
            logger.info(f"[Runner] Compacted in-flight view: {len(messages)} entries, "
                        f"keep_turns={stats['keep_turns']}, blocks_archived={stats['blocks_archived']}, "
                        f"tools_placeholderized={stats['tools_placeholderized']}, "
                        f"est_usage={usage_after}")
        except Exception as e:
            import traceback
            logger.error(f"[Runner] Compaction failed: {e}\n{traceback.format_exc()}")
            try:
                from agent.context_assembler.compaction import AUTO_GATE
                AUTO_GATE.release()  # 失败解除闩锁，避免此后永不再自动触发
            except Exception:
                pass
        finally:
            # 无论成功/失败/异常都必须广播 done，避免前端圆环卡死；
            # reset_tokens 仅在实际压实路径为 True（未压实路径保留旧真实 token 数，
            # 使下次判定准确——notify 协议语义不变）
            try:
                from niu_api.chat import notify_compact_status_sync
                notify_compact_status_sync("done", mode="auto", usage=usage_after,
                                           reset_tokens=compacted)
            except Exception:
                pass
        return compacted

    def _on_tool_round_refresh(self, messages):
        """每工具轮视图重建（2026-09-02）：任何工具结果 persist 落库后从 DB 全量重建——
        新输出编号/折叠态/仪表盘与 DB 同步（fold 只 UPDATE DB，内存视图不感知；
        不刷新则同工具循环下轮仍见折叠前原文与旧使用率）。循环内外同一组装流程：从 DB 重拉 → assemble_view_sync
        （无压实——rebuild 不得把刚折叠的目标行归档移出窗口）→ transform_history
        （与入口同制式：subagent_msg 过滤/悬空 tool_calls 剥离/30000 截断）
        → messages[:] 原地替换；system 保留（含 cache_control，不重建）；动态块由下轮
        _on_before_llm 幂等重插。失败只记日志——下轮入口组装自然自愈。
        """
        try:
            db_messages = self._sync_get_messages()
            if not db_messages:
                return
            from agent.context_manager import peek_context_manager
            cm = peek_context_manager()
            if cm is None:
                return
            from agent.generic.agent_loop import transform_history
            view = cm.assemble_view_sync(db_messages, exclude_last=False)
            transformed = transform_history(view)
            system = messages[0] if messages and messages[0].get("role") == "system" else None
            if system is not None:
                messages[:] = [system] + transformed
            else:
                messages[:] = transformed
        except Exception as e:
            logger.error(f"[Runner] Tool-round view refresh failed (self-heals at next entry assembly): {e}")

    def _get_brain_injector(self):
        """Get or create the cached brain context injector chain.

        All four instances (LightRAGAdapter, LightRAGIngester,
        RegionManager, BrainContextInjector) are lightweight wrappers
        with no expensive initialization, but creating them every turn
        is unnecessary. Cached as instance variables on the runner.

        Includes cache invalidation: if the adapter's underlying LightRAG
        instance has been reset (e.g. after re-initialization), or if
        the activation_mgr singleton was replaced by region_sync, the
        cached wrappers are stale and must be recreated.

        Returns None if activation_mgr is not available (brain tools
        not initialized), matching the original guard condition.
        """
        from agent.brain_tools import get_activation_mgr

        # Invalidate cache if the adapter's LightRAG instance is gone
        # OR if the activation_mgr singleton was replaced by region_sync
        if self._brain_adapter is not None:
            try:
                rag = self._brain_adapter._get_rag()
                current_mgr = get_activation_mgr()
                if rag is None or current_mgr is not self._cached_activation_mgr:
                    self._brain_adapter = None
                    self._brain_ingester = None
                    self._brain_region_mgr = None
                    self._brain_injector = None
                    self._cached_activation_mgr = None
            except Exception:
                self._brain_adapter = None
                self._brain_ingester = None
                self._brain_region_mgr = None
                self._brain_injector = None
                self._cached_activation_mgr = None

        if self._brain_injector is None:
            from niu_api.internal.lightrag_adapter import LightRAGAdapter, LightRAGIngester
            from niu_api.internal.region_injector import BrainContextInjector
            from niu_api.internal.region_manager import RegionManager

            self._brain_adapter = LightRAGAdapter()
            self._brain_ingester = LightRAGIngester()
            _activation_mgr = get_activation_mgr()
            if self._brain_adapter._get_rag() is None or _activation_mgr is None:
                # If activation_mgr is None, try forcing a RegionSync once
                if _activation_mgr is None and self._brain_adapter._get_rag() is not None:
                    # 冷却检查：forced sync 失败后 5 分钟内不再重试，避免死循环
                    forced_sync_cooldown_seconds = 300
                    if time.time() - self._last_forced_sync_fail_time < forced_sync_cooldown_seconds:
                        logger.debug("[BrainInjector] forced sync in cooldown, skip")
                        return None
                    # 防并发：已有 daemon 线程在跑，跳过避免启动多个
                    if self._forced_sync_running.is_set():
                        logger.debug("[BrainInjector] forced sync already running in background, skip")
                        return None
                    # 异步触发：启动 daemon 线程跑 run_sync，主线程立即返回 None 不阻塞
                    # （同步调用阻塞主线程 43 秒导致程序启动卡死）
                    self._forced_sync_running.set()

                    def _run_forced_sync():
                        try:
                            from agent.injector.region_sync import get_region_sync
                            logger.info("[BrainInjector] activation_mgr is None, forcing RegionSync.run_sync() (async)")
                            get_region_sync().run_sync()
                            _mgr = get_activation_mgr()
                            if _mgr is not None:
                                # 成功后刷新缓存的 activation_mgr
                                self._cached_activation_mgr = _mgr
                                # 重置冷却时间
                                self._last_forced_sync_fail_time = 0.0
                                logger.info("[BrainInjector] forced sync succeeded, activation_mgr ready")
                            else:
                                logger.error("[BrainInjector] forced sync completed but activation_mgr still None")
                                self._last_forced_sync_fail_time = time.time()
                        except Exception as e:
                            logger.error("[BrainInjector] Forced RegionSync failed: %s", e)
                            # 记录失败时间，启动 5 分钟冷却
                            self._last_forced_sync_fail_time = time.time()
                        finally:
                            self._forced_sync_running.clear()

                    threading.Thread(target=_run_forced_sync, daemon=True, name="forced-sync").start()
                    # 主线程立即返回 None，下次调用 _get_brain_injector 时后台 sync 可能已完成
                    return None
                # Re-check after forced sync attempt
                # E3-07：块级互斥置位/清除（L2613 子分支之外——子分支内恒置 True、Case A 过渡清除失效）
                self._brain_injector_failed = (_activation_mgr is None)
                if self._brain_adapter._get_rag() is None or _activation_mgr is None:
                    if _activation_mgr is None:
                        logger.error("[BrainInjector] activation_mgr still None after forced sync, brain context disabled")
                    # LightRAG instance not available or activation mgr missing — invalidate cache
                    self._brain_adapter = None
                    self._brain_ingester = None
                    self._brain_region_mgr = None
                    self._brain_injector = None
                    self._cached_activation_mgr = None
                    return None
            self._cached_activation_mgr = _activation_mgr
            self._brain_region_mgr = RegionManager(self._brain_adapter, self._brain_ingester)
            self._brain_injector = BrainContextInjector(
                adapter=self._brain_adapter,
                activation_mgr=_activation_mgr,
                region_mgr=self._brain_region_mgr,
            )
        # E3-07：成功创建/缓存命中共用返回路径——标记清除（恢复后标注消失）
        self._brain_injector_failed = False
        return self._brain_injector

    # ============== LightRAG Helper Methods ==============

    # 黑名单：这些实体类型/名称不应注入到主Agent system prompt
    _INJECT_ENTITY_TYPE_BLACKLIST = {"mcp_tool", "tool", "brainregion"}
    _INJECT_ENTITY_NAME_BLACKLIST = {
        # 源码文件名 — 内部实现细节，对Agent对话无帮助
        "agent_loop.py", "handler.py", "tool_registry.py",
        # 内部架构概念 — system prompt 硬编码已覆盖
        "主Agent", "context-manager", "chat_idle事件",
        # 子Agent工具名 — tools description 已覆盖
        "chat-with-file-processor", "chat-with-event-manager", "chat-with-journal-agent",
    }

    def _format_lightrag_entities_for_prompt(
        self, entities: list[dict], title: str, seen_names: set[str],
    ) -> tuple[str, set[str]]:
        """Format LightRAG entity dicts for prompt injection with blacklist filtering."""
        if not entities:
            return "", seen_names

        is_skill_section = title == "相关技能"
        lines = [f"\n\n### [{title}]"]
        added = 0
        for entity in entities:
            entity_name = entity.get("entity_name", "")
            display_name = entity_name

            # 过滤黑名单实体类型（如 mcp_tool/tool — 主Agent通过 disk 发现工具）
            # 注意：LightRAG 返回的 entity_type 可能是 title case（如 "Tool"），需 .lower()
            entity_type = entity.get("entity_type", "").lower()
            if entity_type in self._INJECT_ENTITY_TYPE_BLACKLIST:
                logger.debug(f"[Inject] Skipping blacklisted type '{entity_type}': {display_name}")
                continue

            # 过滤黑名单实体名（源码文件名、硬编码已覆盖的架构概念）
            if display_name in self._INJECT_ENTITY_NAME_BLACKLIST:
                logger.debug(f"[Inject] Skipping blacklisted name: {display_name}")
                continue

            if display_name in seen_names:
                continue
            seen_names.add(display_name)
            description = (entity.get("description") or "").replace("<SEP>", "\n")
            if len(description) > 500:
                description = description[:500] + "..."
            if description:
                lines.append(f"{added + 1}. **{display_name}**")
                lines.append(f"   {description}")
            else:
                lines.append(f"{added + 1}. **{display_name}**")
            if is_skill_section:
                lines.append(f"   路径: {_skill_display_path(display_name)}")
                if description.startswith("[草稿]"):
                    lines.append("   ⚠️ 草稿skill — 使用后反馈效果")
                elif description.startswith("[待观察]"):
                    lines.append("   ⚠️ 待观察skill — 此skill有历史问题，使用后必须反馈效果（成功或失败）")
            added += 1

        if added == 0:
            return "", seen_names
        return "\n".join(lines), seen_names

    # ============== Disk Description ==============

    def _build_disk_description(self) -> str:
        """Build disk tool description with dynamic directory listing for system prompt."""
        try:
            servers = self.disk_engine.config.servers
        except Exception:
            return ""

        dir_lines = []
        for server in servers.values():
            dir_lines.append(f"  /{server.directory:<10} — {server.description}")

        return (
            "\n\n### [虚拟磁盘工具]\n"
            "你有一个虚拟磁盘工具 disk(command)，可以用 Unix 命令探索和调用所有 MCP 工具。这是对 MCP 工具的虚拟化封装，不是系统磁盘，不能用于访问本地文件系统。\n\n"
            "命令:\n"
            "  ls /                列出所有目录\n"
            "  ls /<dir>           列出目录下的工具\n"
            "  cat /<dir>/readme.txt  查看目录说明（推荐先看这个再执行）\n"
            "  cat /<dir>/<tool>   查看工具详细用法\n"
            "  /<dir>/<tool> <args>  执行工具（位置参数直接写，不加 key=）\n\n"
            "示例:\n"
            "  cat /browser/readme.txt           先看 browser 目录说明\n"
            "  /browser/browser_navigate https://example.com  打开网页\n"
            "  /lightrag/lightrag_query 什么是知识图谱 --mode hybrid\n\n"
            "当前磁盘目录:\n"
            + "\n".join(dir_lines)
        )

    # ============== Dynamic Resource Injection ==============

    def _inject_dynamic_resources(self, context: str) -> tuple[str, dict[str, int]]:
        """动态注入相关资源 — 向量检索 + 分级脑区知识 + Ebbinghaus 衰减池。

        流程:
        1. 脑区激活（保留返回值——step 6 分级注入消费；图遍历已删除，见方案 2.3）
        2. 全局向量检索
        3. 衰减池维护 (先衰减旧实体，再注入新命中)
        4. 衰减池注入：全局检索命中 (score=distance)
        5. 格式化注入 (脑区状态图 + skill + knowledge + 活跃脑区知识 + 习惯)
        """
        from agent.generic.interruptible import run_interruptibly

        # E3 D4：注入失败标注累加器——5 处 except 分支追加固定标注，组装前并入 parts
        injection_notes: list[str] = []

        # 0. Brain region activation——保留 activate_for_query 返回值（step 6 分级注入消费）
        _brain_region_entities: dict[str, list[dict]] = {}
        _brain_injector = None
        try:
            _ok, _brain_injector = run_interruptibly(
                lambda: self._get_brain_injector(), is_stop_requested,
            )
            # R3-R1：_get_brain_injector 首次初始化持 _rag_lock（秒级）——一并包可中断（防御性）
            if _ok and _brain_injector is not None:
                _ok2, _brain_result = run_interruptibly(
                    lambda: _brain_injector.activate_for_query(context, timeout=15),
                    is_stop_requested,
                )
                if _ok2 and _brain_result:
                    # (region_entities, entity_to_region, hit_entities)——分级注入消费第一项
                    _brain_region_entities, _, _ = _brain_result
                # 放弃等待但后台 daemon 线程仍可能完成激活副作用（置 1.0 + SSE 推送 + 缓存合并更新）
        except Exception as e:
            logger.warning(f"Brain activation failed: {e}")
            injection_notes.append("[脑区激活失败，本轮无脑区注入]")

        # 1. LightRAG 检索 — 按类型独立检索，避免 skill 被 knowledge 淹没
        lightrag_results: dict[str, list[dict]] = {
            "skill": [], "knowledge": [], "other": [],
        }
        adapter = None
        if self._brain_adapter is not None:
            adapter = self._brain_adapter
        else:
            from niu_api.internal.lightrag_adapter import LightRAGAdapter
            adapter = LightRAGAdapter()
        # 1a. Skill 专属检索：用 filter_lambda 按 file_path 预过滤，确保 skill 不被 knowledge 淹没
        #     独立 try 块：skill 检索失败不影响 knowledge 检索
        try:
            _ok, _res = run_interruptibly(
                lambda: adapter.search_by_file_path(
                    context, file_path_contains="skill_sync", top_k=10,
                    keywords=[context], timeout=15,
                ),
                is_stop_requested,
            )
            skill_results = _res if _ok else []
            lightrag_results["skill"] = skill_results
        except Exception as e:
            logger.warning(f"LightRAG skill retrieval failed: {e}")
            injection_notes.append("[技能检索失败，本轮无技能注入]")
        if is_stop_requested():
            return "", {}
        # 1b. Knowledge 全量检索
        try:
            _ok, _res = run_interruptibly(
                lambda: adapter.search_multi_lightrag(
                    context, mode="local", top_k=10, keywords=[context], timeout=15,
                ),
                is_stop_requested,
            )
            knowledge_results = _res if _ok else {}
            # 从 knowledge 结果中移除已由 skill 检索获取的实体（按 entity_name 去重）
            skill_names = {e.get("entity_name", "") for e in lightrag_results["skill"]}
            for cat, entities in knowledge_results.items():
                if cat == "skill":
                    # search_multi_lightrag 的 skill 桶含非 SkillSync 来源的 skill（文档提取）
                    # 合并到 lightrag_results["skill"]，由衰减池注入阶段的降级逻辑统一处理
                    for e in entities:
                        if e.get("entity_name", "") not in skill_names:
                            lightrag_results["skill"].append(e)
                    continue
                lightrag_results[cat] = [e for e in entities if e.get("entity_name", "") not in skill_names]
        except Exception as e:
            logger.warning(f"LightRAG knowledge retrieval failed: {e}")
            injection_notes.append("[知识检索失败，本轮无参考知识注入]")
        if is_stop_requested():
            return "", {}

        # 2. 衰减池维护（先衰减旧实体，再注入新命中）
        self._decay_pool.decay()

        # 3. 衰减池注入：全局检索命中的实体
        for category, entities in lightrag_results.items():
            for i, entity in enumerate(entities):
                name = entity.get("entity_name", "")
                if not name:
                    continue
                # 黑名单预过滤：不在注入后才过滤，节省衰减池 slot
                entity_type = (entity.get("entity_type") or "").lower()
                if entity_type in self._INJECT_ENTITY_TYPE_BLACKLIST:
                    continue
                if name in self._INJECT_ENTITY_NAME_BLACKLIST:
                    continue
                # distance fallback: 旧版 lightrag-hku 没有 distance 字段
                distance = entity.get("distance")
                if distance is None:
                    distance = 1.0 - (i / max(len(entities), 1)) * 0.5
                # 真实 skill 检查：file_path 含 "skill_sync" 段（由 sync.py 标记）
                # 非 SkillSync 来源的 skill（文档入库 LLM 提取）降级为 knowledge
                # fallback: file_path 不含 skill_sync 时，检查磁盘文件是否存在（兼容旧 skill）
                inject_category = category
                if category == "skill":
                    entity_file_path = entity.get("file_path", "")
                    is_real_skill = any(
                        seg.strip() == "skill_sync"
                        for seg in entity_file_path.split("<SEP>")
                    )
                    if not is_real_skill:
                        skill_path = Path.home() / ".niu" / "skills" / f"{name}.md"
                        inject_category = "knowledge" if not skill_path.exists() else "skill"
                self._decay_pool.inject(
                    entity_name=name,
                    entity_dict=entity,
                    category=inject_category,
                    source="vector",
                    vector_score=distance,
                )

        # 4. 图遍历已删除（2026-08-13 实验裁决，见方案 2.3：命中实体已够好，
        #    图邻居是噪声放大器——applescript 混入实证；保留块间停止守卫与 1a/1b 后一致）
        if is_stop_requested():
            return "", {}

        # ============== Format & Inject ==============
        parts: list[str] = []
        seen_names: set[str] = set()

        # Brain region status map (不变)
        try:
            if _brain_injector is not None:
                brain_context = _brain_injector.format_region_map_only()
                if brain_context:
                    parts.append(f"\n{brain_context}")
        except Exception as e:
            logger.warning(f"Brain region map injection failed: {e}")
            injection_notes.append("[脑区状态图生成失败]")

        # Skills (从衰减池取 category=skill)
        skill_entries = self._decay_pool.get_top_by_category("skill", 5)
        if skill_entries:
            skill_entities = [e.entity_dict for e in skill_entries]
            skills_text, seen_names = self._format_lightrag_entities_for_prompt(
                skill_entities, "相关技能", seen_names,
            )
            if skills_text:
                parts.append(skills_text)

        # Knowledge (从衰减池取 category=knowledge)
        knowledge_entries = self._decay_pool.get_top_by_category("knowledge", 10)
        if knowledge_entries:
            knowledge_entities = [e.entity_dict for e in knowledge_entries]
            knowledge_text, seen_names = self._format_lightrag_entities_for_prompt(
                knowledge_entities, "参考知识", seen_names,
            )
            if knowledge_text:
                parts.append(knowledge_text)

        # 活跃脑区知识（点亮脑区分级：🟢 5 / 🟡 3 / ⚫ 0——本轮命中优先、缓存回退）
        # region_entries 块外初始化——尾部 debug 行 len(region_entries) 无条件引用（R4-A P1-1）
        region_entries: list[tuple] = []
        if _brain_injector is not None:
            try:  # injector 交互惯例——失败降级空段，不传播至 LLM 轮次（R7-A P2-1）
                region_entries = _brain_injector.format_region_knowledge(_brain_region_entities)
            except Exception as e:
                logger.warning(f"Brain region knowledge formatting failed: {e}")
                injection_notes.append("[脑区知识格式化失败]")
            region_lines: list[str] = []
            for label, name, etype, desc in region_entries:
                if name in seen_names:
                    continue
                seen_names.add(name)
                # `or ""` 守卫：query_data 实体可无 description 字段（R8-A P2-1）
                desc_clean = (desc or "").replace("<SEP>", "\n")
                desc_line = f"   {desc_clean[:500]}" if desc_clean else ""
                region_lines.append(f"{label} **{name}** [{etype}]\n{desc_line}")
            if region_lines:
                parts.append("### [活跃脑区知识]\n" + "\n".join(region_lines))
                parts.append(
                    "\n\n### [知识探索指引]\n"
                    "优先参考上述活跃脑区知识回答用户问题，脑区内容与你当前关注领域最相关。"
                )

        logger.debug(
            f"Dynamic injection | pool_size={len(self._decay_pool)}, "
            f"skills={len(skill_entries)}, knowledge={len(knowledge_entries)}, "
            f"region={len(region_entries)}"
        )

        # E3 D4：脑区上下文不可用标注（getattr 守卫消费——__new__ 装配测试不崩）——组装前并入
        if getattr(self, "_brain_injector_failed", False):
            injection_notes.append("[脑区上下文不可用]")
        parts.extend(injection_notes)

        injection = "\n".join(parts)
        if injection:
            logger.debug(f"Dynamic injection - Total length: {len(injection)} chars")
        else:
            logger.debug("Dynamic injection - Skipped (no relevant results)")

        # 阶段二：注入后台子 Agent 清单
        subagent_section = self._format_running_subagents_section()
        if subagent_section:
            injection = (injection + "\n\n" + subagent_section) if injection else subagent_section

        return injection, {}

    def _format_running_subagents_section(self) -> str:
        """格式化后台子 Agent 清单段（动态注入用）。

        软上限 5 个，超出只显示前 5 + "还有 N 个"。
        只列异步子 Agent（同步子 Agent 主 Agent 阻塞中，无法 @）。
        """
        from agent.subagent_registry import SubagentRegistry

        try:
            async_subagents = [r for r in SubagentRegistry.list_running() if not r.is_sync]
        except Exception as e:
            logger.warning(f"List running subagents failed: {e}")
            return ""

        if not async_subagents:
            return ""

        # 按启动时间排序（started_at 字段，Task 3 已加）
        async_subagents.sort(key=lambda r: r.started_at)

        # 软上限 5 个
        shown = async_subagents[:5]
        remaining = len(async_subagents) - len(shown)

        lines = ["[当前后台运行的子 Agent]"]
        for r in shown:
            status = "running"
            try:
                if r.memory_context is not None:
                    snap = r.memory_context.snapshot()
                    turn = snap.get("current_turn", 0)
                    if turn > 0:
                        status = f"running（第 {turn} 轮）"
            except Exception:
                pass
            lines.append(f"- {r.unique_name}（类型：{r.agent_type}，状态：{status}）")

        if remaining > 0:
            lines.append(f"- 还有 {remaining} 个子 Agent 运行中")

        lines.append("")
        lines.append("如需查看某子 Agent 进度，调用 check_subagent_progress 工具。")
        lines.append("如需给某子 Agent 补充上下文，写消息到对话（@子名 补充内容）。")
        lines.append("如需停止某子 Agent，写消息到对话（@子名 /stop）。")

        return "\n".join(lines)

    def chat(
        self, session_id: str, user_input: str, stream: bool = True, max_turns: int = 40, history: list = None, resources: list | None = None, channel_id: str = ""
    ) -> Generator[str, None, None]:
        """执行对话 — disk mode: base tools + disk only.

        Args:
            session_id: 会话ID
            user_input: 用户输入
            stream: 是否流式输出
            max_turns: 最大轮次
            history: 可选的历史消息列表
        """
        logger.info(f"[Runner] chat() called, session_id={session_id}, input={user_input[:50]}")
        if not channel_id and self._im_channel_id:
            channel_id = self._im_channel_id
        self._current_channel_id = channel_id
        if channel_id:
            self._im_channel_id = channel_id
        # 重置首轮 resources 注入，防跨对话泄漏
        self._first_turn_extra_injection = ""

        # I1 修复：首轮动态注入由 _on_before_llm 统一负责（在 agent_runner_loop 内 turn=1 时调）
        # 这里不调用 _inject_dynamic_resources，动态注入段留给 _on_before_llm 首轮覆盖

        # 注入 resources（拖入文件的模式信息）到实例属性
        # C4 修复：存 self._first_turn_extra_injection 而非 injection 变量，
        # 让 _on_before_llm 首轮合并进 injection（否则被 _assemble_system_message 整体替换覆盖）
        if resources:
            # 防御性过滤：只处理格式正确的资源条目
            valid_resources = [r for r in resources if isinstance(r, dict) and "path" in r and "mode" in r]
            if valid_resources:
                resource_lines = []
                for r in valid_resources:
                    path = r.get("path", "")
                    mode = r.get("mode", "copy")
                    if mode == "reference":
                        resource_lines.append(f"- 文件 {path}：必须使用引用模式（mode=reference），不要拷贝文件，使用原路径引用")
                    elif mode == "move":
                        resource_lines.append(f"- 文件 {path}：必须使用移动模式（mode=move），将文件移动到存储目录")
                    # mode="copy" 不需要额外提示，这是默认行为
                if resource_lines:
                    self._first_turn_extra_injection = "\n\n【文件操作模式要求】\n以下文件的操作模式由用户指定，调用 ingest 工具时必须传递对应的 mode 参数：\n" + "\n".join(resource_lines)


        # 组装 system message 静态区（首轮就按 model 决定格式，Claude 单块 cache_control）。
        # D17：injection="" / memory_section="" → 空骨架只含静态指令+disk_desc，
        # 不含任何动态文本（无 Current Time）；首轮动态块由 _on_before_llm turn=1
        # 经 role=user 载体注入（此处忽略返回的动态块文本）。
        # resources 文本在实例属性里，_on_before_llm 首轮会合并进 injection。
        system_message = {"role": "system", "content": ""}
        self._assemble_system_message([system_message], "", "", self.default_model)

        # 阶段三：每次对话开始时检查 ~/.niu/agents/ 是否有新 MD
        self._refresh_base_tools_schema_if_dirty()

        # 组装 tools_schema = base tools + static MCP tools + disk
        # （chat 与轮中刷新共用 _assemble_tools_schema）
        tools_schema = self._assemble_tools_schema()

        logger.debug(
            f"tools_schema: {len(self.base_tools_schema)} base + {len(tools_schema) - len(self.base_tools_schema) - 1} static + 1 disk = {len(tools_schema)} total"
        )

        # 读取上下文窗口大小，用于主 Agent warningThreshold 溢出检测
        from agent.subagent import _read_context_window_tokens
        context_window_tokens = _read_context_window_tokens()

        gen = agent_runner_loop(
            client=self.client,
            system_prompt="",  # 向后兼容（system_message 非 None 时分支选择生效）
            system_message=system_message,
            user_input=user_input,
            handler=self.handler,
            tools_schema=tools_schema,
            max_turns=max_turns,
            verbose=False,
            initial_user_content=user_input,
            history=history,  # Pass history to agent_loop
            on_turn_end=self._on_turn_end,  # 每轮结束后清理（用户记忆 + 脑区衰减）
            on_before_llm=self._on_before_llm,  # 每轮 LLM 调用前刷新动态注入
            context_window_tokens=context_window_tokens,  # 主 Agent 溢出检测
            on_context_high_usage=self._on_context_high_usage,  # 主 Agent 超阈值回调
            on_tool_round_refresh=self._on_tool_round_refresh,  # 每工具轮 persist 后视图重建（子 Agent 不传 = None 跳过）
            context_target_threshold=0,  # 主 Agent 不需要 FIFO 目标阈值
        )

        # 累加输出（双管道：full_resp 只含 reply 内容，用于 DB 存储）
        full_resp = ""
        return_value = None
        self.last_return_value = None  # 重置，避免复用残留
        persisted_msgs = []  # V4: 已通过persist事件持久化的消息列表
        self._extracted_at_msgs = []  # 修正版方案：本轮流中提取的 subagent_msg 内容（persist_agent_reply 去重用）
        chat_idle_pushed = False  # 跟踪是否已推送 chat_idle，避免重复推送
        try:
            while True:
                # 协作式停止：每次迭代检查，发现停止立即停止消费生成器
                if is_stop_requested():
                    logger.info("[Runner] Stop requested, stopping generator consumption")
                    gen.close()  # 关闭生成器，触发 GeneratorExit
                    break
                try:
                    chunk = next(gen)
                    if isinstance(chunk, StreamEvent):
                        if chunk.type == "reply":
                            full_resp += chunk.content
                            if chunk.content:  # SSE 管道：只推送非空 reply
                                yield chunk.content
                                # IM Gateway 流式推送（完整内容，非增量）
                                # R1：推送前 strip @ 段——IM 用户流式不见主↔子内容
                                # （Electron 渲染走 DB new-message SSE 已 strip；IM 最终
                                # 消息由 route_out(full_reply) 发，full_reply 在
                                # persist_agent_reply 已 strip——此处 chunk 级 strip 为
                                # 流式预览兜底，@ 段跨 chunk 拆分时尽力而为）
                                # 闸门统一 should_push_im() 单一入口——force-only 时 channel_id 空，
                                # adapter 回退 _push_chat_id 广播建卡
                                try:
                                    from agent.at_message_parser import strip_at_messages
                                    from niu_api.channel.gateway import get_im_gateway
                                    _gw = get_im_gateway()
                                    if _gw and _gw.is_connected and chunk.content and self.should_push_im():
                                        _gw.notify_stream(strip_at_messages(chunk.content), channel_id=self._current_channel_id)
                                except Exception as e:
                                    # E4-10：IM 通道故障可监控——静默 pass → error（闸门结构零变化）
                                    logger.error(f"[Runner] IM notify_stream (reply) failed: {e}")
                        elif chunk.type == "persist":
                            # V4: 逐条持久化消息到 DB + 通知 SSE
                            try:
                                msg_dict = json.loads(chunk.content)
                                msg_id = self._persist_one_msg(msg_dict)
                                if msg_id is not None:
                                    msg_dict["_persisted_id"] = msg_id  # 记录写入后的消息ID
                                    persisted_msgs.append(msg_dict)
                            except Exception as e:
                                # E4-10：DB 写失败是数据完整性事件——warning→error 提升（监控可见）
                                logger.error(f"[Runner] Failed to persist msg: {e}")
                        elif chunk.type == "system":
                            # V4: chat_busy/chat_idle 状态机事件，通过SSE推送给前端
                            if chunk.content in ("chat_busy", "chat_idle"):
                                from niu_api.chat import notify_new_message_sync
                                notify_new_message_sync("", chunk.content, "", source="electron")
                                if chunk.content == "chat_idle":
                                    chat_idle_pushed = True
                            elif "已强制退出" in chunk.content:
                                # E4-02：强制退出事件（agent_loop 截断重试耗尽/工具参数连续解析失败——
                                # "已强制退出"特征文本）→ 专用 SSE 事件 system_notice（E2 llm_error 模式）。
                                # 不落库、不进 LLM 上下文——只推前端 ⚠️ 提示；chat_busy/chat_idle 之上独立分支。
                                from niu_api.chat import notify_system_notice_sync
                                notify_system_notice_sync(chunk.content.strip(), source="runner")
                        # type="tool_marker" 不进入 SSE 和 full_resp
                    else:
                        # 向后兼容：普通 str（stream_error/兼容文本）
                        full_resp += chunk
                        if chunk:
                            yield chunk
                            # IM 流式也推（错误文本进 accumulated，finalize 不丢）；与 reply 分支同款守卫
                            try:
                                from agent.at_message_parser import strip_at_messages
                                from niu_api.channel.gateway import get_im_gateway
                                _gw = get_im_gateway()
                                if _gw and _gw.is_connected and chunk and self.should_push_im():
                                    _gw.notify_stream(strip_at_messages(chunk), channel_id=self._current_channel_id)
                            except Exception as e:
                                # E4-10：IM 通道故障可监控——静默 pass → error（闸门结构零变化）
                                logger.error(f"[Runner] IM notify_stream (str chunk) failed: {e}")
                except StopIteration as e:
                    return_value = e.value
                    break
        finally:
            # 工具循环结束时处理挂起同步子 Agent：仅用户显式停止清理，否则保留现场（见函数 docstring）
            cleanup_suspended_sync_subagents(return_value)
            # 确保停止标志被清除（无论正常退出、停止退出还是异常退出）
            if is_stop_requested():
                clear_stop()
            # IM Gateway 流式结束通知
            try:
                from niu_api.channel.gateway import get_im_gateway
                _gw = get_im_gateway()
                if _gw and _gw.is_connected and self.should_push_im():
                    _gw.notify_stream("", channel_id=self._current_channel_id, is_final=True)
            except Exception as e:
                # E4-10：IM 通道故障可监控——静默 pass → error（闸门结构零变化）
                logger.error(f"[Runner] IM notify_stream (final) failed: {e}")
            self._current_channel_id = ""
            # 防御性推送 chat_idle：gen.close() 可能中断 agent_loop 的正常退出路径
            # 只在未推送过时才推送，避免重复
            if not chat_idle_pushed:
                from niu_api.chat import notify_new_message_sync
                notify_new_message_sync("", "chat_idle", "", source="electron")

        # 暴露 return_value 给调用方（用于检测 CONTEXT_OVERFLOW 等控制流）
        self.last_return_value = return_value
        self._persisted_msgs = persisted_msgs  # V4: 已逐条持久化的消息列表

        # 如果 full_resp 为空但有返回值数据，使用返回值
        if not full_resp.strip() and return_value:
            if isinstance(return_value, dict) and "data" in return_value:
                data = return_value["data"]

                try:
                    # 处理有 content 属性的对象（如 MockResponse）
                    if hasattr(data, 'content'):
                        parts = []

                        # 提取思考链（如果有）
                        if hasattr(data, 'thinking') and data.thinking:
                            parts.append(f"<thinking>\n{data.thinking}\n</thinking>")

                        # 提取内容（确保不为 None）
                        if data.content is not None:
                            parts.append(str(data.content))

                        full_resp = "\n\n".join(parts) if parts else ""

                    # 处理字典
                    elif isinstance(data, dict):
                        full_resp = json.dumps(data, ensure_ascii=False)

                    # 处理列表
                    elif isinstance(data, list):
                        full_resp = json.dumps(data, ensure_ascii=False)

                    # 处理字符串
                    elif isinstance(data, str):
                        full_resp = data

                    # 处理其他类型
                    elif data is not None:
                        full_resp = str(data)
                    else:
                        full_resp = ""

                except Exception as e:
                    # 异常保护：记录错误但不崩溃
                    logger.error(f"Failed to extract return_value data: {e}")
                    full_resp = ""

            # 回退：return_value 中提取的内容作为最后一个 chunk yield
            if full_resp.strip():
                yield full_resp.strip()

        # 对话结束后工具衰减已由 _on_turn_end 每轮执行，此处不再重复

    def _persist_one_msg(self, msg_dict: dict) -> str | None:
        """逐条持久化消息到 DB + 通知 SSE（同步，从 executor 线程调用）

        Args:
            msg_dict: 完整的消息 dict，包含 role, content, tool_calls, tool_call_id 等

        Returns:
            消息 ID，或 None（写入失败或消息被过滤）
        """
        from niu_api.chat import notify_new_message_sync


        role = msg_dict.get("role", "")
        content = msg_dict.get("content", "") or ""
        tool_calls = msg_dict.get("tool_calls")
        tool_call_id = msg_dict.get("tool_call_id", "")
        # fold 存储层：tool 消息落库时算一次占比固化（spec §3——分母恒为总窗口）
        output_pct = None
        if role == "tool" and content:
            try:
                from agent.context_assembler import calibration
                from agent.context_manager import ContextManager
                from agent.subagent import _read_context_window_tokens
                est = calibration.estimate(
                    ContextManager.count_tokens_simple([{"role": "tool", "content": content}])
                )
                window = _read_context_window_tokens()
                output_pct = round(est / window * 100, 1) if window else None
            except Exception:
                output_pct = None  # 估算失败不阻断落库

        # 修正版方案 1（来源处理）：主 Agent 对话流的 @ 消息在此源头剥离/提取，
        # 避免主↔子对话中间过程原样写入 DB 泄露到用户对话（000006 实证：
        # tool_calls assistant 的 content 含 @nutritionist 段被原样持久化 + 轮末
        # persist_agent_reply 对 full_reply 整轮拼接懒匹配跨消息提取）。
        # - 有 tool_calls：工具就是回复通道——仅 strip content 的 @ 段，不提取
        # - 无 tool_calls：提取 @ 为 subagent_msg（轮中落库 → db_monitor 实时路由，
        #   子 Agent 挂起时即收到，不 orphan）→ strip content 的 @ 段再写 assistant
        if role == "assistant":
            from agent.at_message_parser import extract_at_messages, format_for_db, strip_at_messages
            if tool_calls:
                content = strip_at_messages(content)
            else:
                for msg in extract_at_messages(content):
                    db_content = format_for_db(msg)
                    sub_msg_id = self._sync_add_message(role="subagent_msg", content=db_content)
                    if sub_msg_id is not None:
                        # P3-1：写成功才记录去重（subagent_msg 写失败时不记——
                        # 否则 rv=None 兜底会误判去重跳过，导致 @ 丢失）
                        self._extracted_at_msgs.append(db_content)
                content = strip_at_messages(content)
            # P2-2：纯 @ 回复（strip 后为空）不写空 assistant 行（subagent_msg 已存），
            # 对齐 persist_agent_reply rv 路径 `if not content.strip(): continue` 惯例。
            # Review P1：assistant(tool_calls) 排除——即使 content 为空也是锚点行
            # （agent_loop L720-723 还原工具调用锚点，tool 消息靠 _valid_tc_ids 归属），
            # 不入库会导致多轮工具对话丢失工具结果上下文。
            if not content.strip() and not tool_calls:
                return None

        # 同步写入 DB
        msg_id = self._sync_add_message(role=role, content=content,
                                         tool_calls=tool_calls, tool_call_id=tool_call_id,
                                         output_pct=output_pct)
        if msg_id is None:
            return None

        # 通知 SSE（仅 assistant 消息推送给前端）
        if role == "assistant" and content.strip():
            notify_new_message_sync(msg_id, "assistant", content, source="electron")

            # IM Gateway 流式推送已通过 reply chunk 路径（行 1849）发送完整内容，
            # 此处不再发空信号（避免冗余 CardKit API 调用）

        return msg_id

    def _sync_add_message(self, role: str, content: str,
                           tool_calls: list | None = None, tool_call_id: str = "",
                           output_pct: float | None = None) -> str | None:
        """从同步线程写入消息到 DB（桥接 aiosqlite）

        使用 asyncio.run_coroutine_threadsafe 在 FastAPI 事件循环中执行 DB 写入，
        然后阻塞等待结果。这保证了消息按 yield 顺序写入 DB（不会倒序）。

        超时设为30秒：DB写入正常情况下<100ms，30秒足够覆盖极端情况。
        如果30秒仍超时，说明DB严重故障，此时重复写入是可接受的。

        Returns:
            消息 ID，或 None（写入失败）
        """
        import asyncio

        from niu_api.chat import _main_loop

        from agent.session import get_message_store

        loop = _main_loop
        if loop is None or loop.is_closed():
            logger.warning("[Runner] No event loop available for sync DB write")
            return None

        async def _do_add():
            store = await get_message_store()
            return await store.add_message(
                role=role, content=content,
                tool_calls=tool_calls, tool_call_id=tool_call_id,
                output_pct=output_pct
            )

        try:
            future = asyncio.run_coroutine_threadsafe(_do_add(), loop)
            msg_id = future.result(timeout=30.0)  # 阻塞等待，保证顺序
            return msg_id
        except Exception as e:
            logger.warning(f"[Runner] sync_add_message failed: {e}")
            return None


# 全局实例
_runner: NiuRunner | None = None
_runner_lock = threading.Lock()


def get_runner(llm_config: dict[str, Any] | None = None, mcp_client=None) -> NiuRunner | None:
    """获取全局 Runner 实例（线程安全）"""
    global _runner
    if _runner is None and llm_config:
        with _runner_lock:
            # 双重检查
            if _runner is None:
                _runner = NiuRunner(llm_config, mcp_client)
    return _runner


def chat(session_id: str, user_input: str, **kwargs) -> Generator[str, None, None]:
    """便捷函数：执行对话"""
    runner = get_runner()
    if runner is None:
        raise RuntimeError("Runner not initialized. Call get_runner() with config first.")
    return runner.chat(session_id, user_input, **kwargs)
