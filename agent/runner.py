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


def cleanup_suspended_sync_subagents():
    """主 Agent 工具循环退出时清理所有挂起的同步子 Agent session。

    场景：主 Agent 调用 chat-with-xxx 后，LLM 不再调用第二次 chat-with-xxx
    而是直接回应用户，导致同步子 Agent session 残留在 waiting_for_answer 状态。
    主 Agent 工具循环 finally 块调用此函数清理。
    注（2026-08-11 用户拍板）：不再推送清理通知（工具错误/orphan 反馈已告知主 Agent；
    通知以 user 消息进对话流会被主 Agent 误认为用户话，造成转述混乱）。
    """
    for instance in SubagentRegistry.list_running():
        state = getattr(instance, "state", "running")
        is_sync = getattr(instance, "is_sync", False)
        if state == "waiting_for_answer" and is_sync:
            try:
                SubagentRegistry.unregister(instance.unique_name)
                logger.info(f"[CleanupSuspendedSync] 已清理挂起同步子 Agent: {instance.unique_name}")
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
    - description 字段缺失（视为无效子 Agent）

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

        # 5d. frontmatter 非空 + description 存在
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

        # 5e. MCP 服务器未加载 warning（不阻塞）
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
                "description": "子 Agent 唯一名。同步调用（chat-with-xxx）时可省略，默认用 agent 名（如 browser-operator）；异步调用时为 agent 名+4位 hex 后缀（如 file-processor-a1b2，来自派单确认）",
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
                    "向用户提问并等待回答。当你需要用户确认、提供信息、做决策时使用。"
                    "调用后暂停等待用户输入（不退出当前工作流），收到回答后继续原任务。"
                    "回答以 [user 回答] 形式返回。"
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



# --- Threshold EMA 张力模型常量 ---
_THRESHOLD_MIN = 10.0
_THRESHOLD_MAX = 50.0
_THRESHOLD_ALPHA_UP = 0.1    # 上升慢：threshold += (50 - threshold) * 0.1
_THRESHOLD_ALPHA_DOWN = 0.4  # 下降快：threshold -= (threshold - 10) * 0.4
_THRESHOLD_MIN_SAMPLES = 5   # 冷启动保护：sample_count < 5 时保持 10
_REF_ALPHA = 0.2   # 参考线 EMA 遗忘因子：ref = 0.2*current + 0.8*old（等效近期 ~10 轮，用户 2026-08-09 确认）


def _calc_dream_trigger_threshold_dynamic(
    context_window: int,
    ema_path: Path,
) -> int:
    """根据持久化 threshold EMA 值返回触发阈值。

    threshold 自身做 EMA（对数渐近张力模型）：
    - 冷启动（sample_count < 5）：返回 10
    - 否则返回持久化的 threshold 值（int 截断）

    context_window 参数保留（调用方传入），但新模型不依赖它。
    """
    threshold, sample_count, _ref_ema = NiuRunner._read_ema(ema_path)

    if sample_count < _THRESHOLD_MIN_SAMPLES:
        return int(_THRESHOLD_MIN)

    return max(int(_THRESHOLD_MIN), min(int(_THRESHOLD_MAX), int(threshold)))

def _compute_threshold_update(
    threshold_old: float,
    sample_count: int,
    current_turn_tokens: int,
    ref_old: float,
) -> tuple[float, int, float]:
    """计算 threshold EMA 更新。返回 (new_threshold, new_sample_count, new_ref)。

    对数渐近张力模型 + EMA 参考线：
    - 冷启动（sample_count < 5）：threshold 不变，保持 10；参考线仍更新
    - 参考线 EMA：ref = ALPHA * current + (1 - ALPHA) * old（等效近期 ~10 轮主导，
      参考线随近期负载双向快速响应——重活抬升、轻活回落，避免被早期历史定型；
      轻活历史后参考线低于全历史累积平均，门槛更低 → 中等轮更容易判重量）
    - 轻量（本轮 token <= ref）：threshold 上升
      threshold += (THRESHOLD_MAX - threshold) * ALPHA_UP
    - 重量（本轮 token > ref）：threshold 下降
      threshold -= (threshold - THRESHOLD_MIN) * ALPHA_DOWN
    """
    new_sample_count = sample_count + 1
    new_ref = _REF_ALPHA * current_turn_tokens + (1 - _REF_ALPHA) * ref_old

    if sample_count < _THRESHOLD_MIN_SAMPLES:
        return threshold_old, new_sample_count, new_ref

    if current_turn_tokens <= new_ref:
        # 轻量 → 上升（对数渐近，越接近 50 越慢）
        new_threshold = threshold_old + (_THRESHOLD_MAX - threshold_old) * _THRESHOLD_ALPHA_UP
    else:
        # 重量 → 下降（快速回 10）
        new_threshold = threshold_old - (threshold_old - _THRESHOLD_MIN) * _THRESHOLD_ALPHA_DOWN

    # clamp
    new_threshold = max(_THRESHOLD_MIN, min(_THRESHOLD_MAX, new_threshold))

    return new_threshold, new_sample_count, new_ref


def _slice_after_cursor(db_messages: list, cursor_id: str) -> list:
    """截取游标之后的消息。游标为空或找不到时返回全量消息。"""
    if not cursor_id:
        return db_messages
    cursor_idx = -1
    for i, msg in enumerate(db_messages):
        if (getattr(msg, "id", "") or "") == cursor_id:
            cursor_idx = i
            break
    return db_messages[cursor_idx + 1:] if cursor_idx >= 0 else db_messages


def _cursors_caught_up(messages, protect_recent) -> bool:
    """进化（dream）游标追平（runner 版，读游标用 _read_cursor_locked）。

    与 niu_api/compat._cursors_caught_up_dream_only 同逻辑（§4.3 v2）——D3：模式三管道无提炼腿，
    只查进化游标；判定与即将执行的压缩使用同一 protect 有效值。journal 游标不查。
    """
    from niu_api.compat import _find_protected_range

    if not messages:
        return True  # 空库无可压缩内容
    protect_start = _find_protected_range(messages, protect_recent)
    msg_ids = [getattr(m, "id", "") or "" for m in messages]
    cursor_path = Path.home() / ".niu" / "last_dream_evolve.json"
    cursor = NiuRunner._read_cursor_locked(cursor_path, "last_dream_evolve_id")
    if not cursor:
        return False  # 空游标=从未处理——保守不压
    try:
        idx = msg_ids.index(cursor)
    except ValueError:
        return False  # 游标指向已删消息——保守不压
    if protect_start >= len(messages):
        if idx != len(messages) - 1:
            return False  # protect=0：游标未到真实尾部——未追平
        return True  # 已追平
    if idx < protect_start - 1:
        return False  # 游标在最后一条未保护消息之前——有未处理
    return True


def _extract_prev_complete_turn_msgs(post_compress_msgs: list) -> list:
    """取上一完整轮的 messages（倒数第二条 user 消息含，到倒数第一条 user 消息不含）。

    延迟结算语义：
    - 最新 user 消息后的消息属于"本轮"（进行中，不确定是否完成），不参与计算
    - 上一轮 = 上一条 user（含）到最新 user（不含）之间的所有消息（含全部 assistant/tool 输出）
    - 不足两轮（无完整上一轮，如压缩游标刚越过、重启后仅 1 条 user）返回 []
    - 上一轮被压缩（compress 游标切割后 user 不在切片内）时自然不出现，返回 []
    """
    user_indices = [
        i for i, m in enumerate(post_compress_msgs)
        if getattr(m, "role", "") == "user"
    ]
    if len(user_indices) < 2:
        return []
    return post_compress_msgs[user_indices[-2]:user_indices[-1]]


def _ema_marker_step(last_user_id: str, prev_marker: str) -> tuple[str, str]:
    """EMA marker 状态机：决定本次 _on_turn_end 回调是初始化、结算还是跳过。

    Returns: (action, new_marker)
    - "skip"：无 user 消息（last_user_id 空）或同轮重复回调（id 未变）→ 不结算
    - "init"：启动后首次（prev_marker 空）→ 只设 marker 不结算
      （避免重启重复结算重启前已结算的轮）
    - "settle"：新 user 消息到来 → 结算上一完整轮
    """
    if not last_user_id:
        return "skip", prev_marker
    if not prev_marker:
        return "init", last_user_id
    if last_user_id == prev_marker:
        return "skip", prev_marker
    return "settle", last_user_id


def _prev_turn_is_complete(prev_turn_msgs: list) -> bool:
    """上一轮是否完整可结算：以 assistant/user 回复结束。

    - 尾部是 assistant：轮完整（含最终回复）→ 可结算
    - 尾部是 user（连续 user 消息）：该轮=纯 user 消息 → 可结算
    - 尾部是 tool：工具循环进行中（快照可能缺尾部）或轮被截断 → 不可结算
      （defer：marker 已在 settle 分支推进，下轮重新采样；若该轮最终完成，
      其尾部 assistant 会在下一次 _on_turn_end 的新快照中出现并被结算）
    """
    if not prev_turn_msgs:
        return False
    return getattr(prev_turn_msgs[-1], "role", "") in ("assistant", "user")


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
        _on_before_llm 每轮从 memory.json 重读生成，作为动态段拼接。
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

        self.llm_config = llm_config
        self.mcp_client = mcp_client
        self.client = create_client(llm_config)
        # 当前模型名（用于 _assemble_system_message 判断是否 Claude 走 cache_control）
        self.default_model = llm_config.get("model", "")
        project_root = os.path.dirname(os.path.dirname(__file__))
        self.handler = NiuHandler(cwd=project_root, mcp_client=mcp_client)
        # 静态段：niu.md + memory（cache 友好，字节稳定）
        # memory 段由 _on_before_llm 每轮重读，不在此缓存
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

        # 动态前缀段：disk_desc（磁盘结构启动时固定，缓存）；Current Time 由
        # _assemble_system_message 每轮实时生成（disk_desc 自带 \n\n 开头，空时为空串）
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

        # Brain context injector chain (lazy-cached, created once per runner)
        self._brain_adapter = None      # LightRAGAdapter
        self._brain_ingester = None     # LightRAGIngester
        self._brain_region_mgr = None   # RegionManager
        self._brain_injector = None     # BrainContextInjector
        self._brain_injector_failed = False  # E3-07：re-check 脑区上下文不可用标记（_inject_dynamic_resources getattr 守卫消费）
        self._cached_activation_mgr = None  # RegionActivationManager (for cache invalidation)
        self._last_forced_sync_fail_time: float = 0.0  # forced sync 失败冷却时间戳
        self._forced_sync_running = threading.Event()  # forced sync 后台线程运行标志，避免并发启动多个 daemon
        self._nap_running = threading.Event()  # 小憩模式后台运行标志，避免并发启动
        self._last_ema_user_id = ""  # 去重：记录上次结算 EMA 时最后一个 user 消息 id
        self._ema_lock = threading.Lock()  # EMA read-modify-write 进程内原子性（不与 _read_ema/_write_ema 的文件锁嵌套）

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
    ) -> None:
        """组装 system message，根据 model 决定是否用 cache_control。

        原地修改 messages[0]["content"]。

        - Claude 模型：content 改为 list 格式，静态段末尾打 cache_control breakpoint。
          静态段（仅 niu.md）被 cache，命中后 input token 计费降至 10%。
          动态段（memory + Current Time + disk_desc + injection）每轮重新发送。
        - 其他模型（火山方舟/DeepSeek/Qwen 等）：content 保持字符串格式。
          静态段在开头且字节稳定，靠服务端自动 prefix cache 命中。

        Args:
            messages: 消息列表，messages[0] 必须是 role=system
            memory_section: 本轮从 memory.json 重读的 memory 段（identity/workspace/user/permanent/firstRun）
            injection: 动态注入内容（skills/knowledge/brain region）
            model: 当前模型名，用于判断是否 Claude
        """
        if not messages or messages[0].get("role") != "system":
            return

        # 动态段 = memory_section + Current Time（每轮实时）+ disk_desc + injection
        dynamic_text = ""
        if memory_section:
            dynamic_text += "\n\n" + memory_section
        dynamic_text += f"\n\nCurrent Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        dynamic_text += self.dynamic_system_prefix
        if injection:
            dynamic_text += injection

        model_lower = (model or "").lower()
        if "claude" in model_lower:
            # Claude：list 格式 + cache_control breakpoint
            messages[0]["content"] = [
                {
                    "type": "text",
                    "text": self.static_system_prompt,
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": dynamic_text,
                },
            ]
        else:
            # 其他模型：字符串格式，静态段在开头
            messages[0]["content"] = self.static_system_prompt + dynamic_text

    def _on_before_llm(self, messages: list, turn: int) -> None:
        """每轮 LLM 调用前重读 memory.json + 刷新动态注入。

        每轮从 memory.json 重新构建 memory_section（identity/workspace/user/permanent/firstRun），
        保证 Agent 写入 memory.json 后下一轮 system prompt 立即感知。
        关键：在 client.chat 之前调，让本轮 LLM 立即读到新 system message。
        原地修改 messages[0]，无返回值。

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

        # 3. 原地修改 messages[0]，本轮 LLM 立即读到
        self._assemble_system_message(messages, memory_section, injection, self.default_model)

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
        - 小憩模式触发检查：增量消息达阈值则后台启动 entity-extractor → dream-evolver
        """
        # Decay brain region activation levels
        try:
            from agent.brain_tools import get_activation_mgr
            mgr = get_activation_mgr()
            if mgr is not None:
                mgr.decay_all()
        except Exception as e:
            logger.debug(f"Brain region decay failed: {e}")

        # 小憩模式触发检查：增量消息达阈值则后台启动 entity-extractor → dream-evolver
        self._maybe_trigger_nap()

        # Schema 刷新：失败退回原 tools_schema（不击穿工具循环）
        try:
            self._refresh_base_tools_schema_if_dirty()
            return self._assemble_tools_schema()
        except Exception as e:
            logger.warning(f"[Runner] schema refresh failed, keeping existing tools: {e}")
            return tools_schema

    def _maybe_trigger_nap(self):
        """检查增量对话轮数，达阈值则后台启动小憩模式（entity-extractor → dream-evolver）。"""
        # 防止并发启动
        if self._nap_running.is_set():
            return

        try:
            from pathlib import Path
            niu_dir = Path.home() / ".niu"
            dream_cursor_path = niu_dir / "last_dream_evolve.json"
            last_dream_evolve_id = self._read_cursor_locked(dream_cursor_path, "last_dream_evolve_id")
            compress_cursor_path = niu_dir / "last_compress.json"
            last_compress_id = self._read_cursor_locked(compress_cursor_path, "last_compress_id")

            # 从 DB 获取消息
            db_messages = self._sync_get_messages()
            if not db_messages:
                logger.debug('[Nap] No messages in DB, skipping trigger check')
                return

            # 数游标后的增量对话轮数（一轮 = 两条 user 消息之间的所有消息）
            incremental_msgs = _slice_after_cursor(db_messages, last_dream_evolve_id)

            # 计算轮数：每遇到一条 role=user 消息算一轮开始
            turn_count = sum(1 for msg in incremental_msgs if getattr(msg, "role", "") == "user")

            # 截取压缩游标后的消息（用于 EMA 更新）
            post_compress_msgs = _slice_after_cursor(db_messages, last_compress_id)
            post_compress_turns = sum(1 for m in post_compress_msgs if getattr(m, "role", "") == "user")

            ema_path = niu_dir / "threshold_ema.json"

            # 找最后一个 user 消息 id（去重 marker：仅当新 user 到来时结算）
            last_user_id = ""
            for m in reversed(post_compress_msgs):
                if getattr(m, "role", "") == "user":
                    last_user_id = getattr(m, "id", "") or ""
                    break

            # 去重 + 更新 threshold EMA（延迟结算上一完整轮）
            action, new_marker = _ema_marker_step(last_user_id, self._last_ema_user_id)
            if action == "settle":
                self._last_ema_user_id = new_marker
                # 延迟结算：新 user 消息到来时上一轮已完整。
                # 算上一轮 = 倒数第二条 user（含）到倒数第一条 user（不含）的所有消息
                # （含全部 assistant 与 tool 输出）；本轮（最新 user 之后）不确定是否
                # 完成，不参与计算；上一轮被压缩（游标切割）或无完整上一轮时跳过。
                prev_turn_msgs = _extract_prev_complete_turn_msgs(post_compress_msgs)

                if prev_turn_msgs and _prev_turn_is_complete(prev_turn_msgs):
                    from agent.token_calculator import TokenCalculator
                    calc = TokenCalculator.get()
                    prev_turn_dicts = [
                        {
                            "role": getattr(m, "role", ""),
                            # CQ-05: 统一 content 为字符串，与 count_message_single 一致
                            "content": (lambda c: (
                                " ".join(p.get("text", "") for p in c
                                         if isinstance(p, dict) and p.get("type") == "text")
                                if isinstance(c, list) else c
                            ))(getattr(m, "content", "") or ""),
                            "tool_calls": getattr(m, "tool_calls", []) or [],
                        }
                        for m in prev_turn_msgs
                    ]
                    prev_turn_tokens = calc.count_messages(prev_turn_dicts)

                    # CQ-01 修复：用 threading.Lock 保证 read-modify-write 原子性
                    with self._ema_lock:
                        threshold_old, sample_count, ref_old = self._read_ema(ema_path)
                        new_threshold, new_sample_count, new_ref = _compute_threshold_update(
                            threshold_old, sample_count, prev_turn_tokens, ref_old
                        )

                        self._write_ema(ema_path, new_threshold, new_sample_count, new_ref)
                        logger.debug(f"[Nap] threshold EMA: old={threshold_old:.1f}, new={new_threshold:.1f}, "
                                    f"samples={new_sample_count}, prev_turn_tokens={prev_turn_tokens}, "
                                    f"ref={ref_old:.0f}->{new_ref:.0f}")
                else:
                    logger.debug(f"[Nap] No complete previous turn to settle (post_compress_turns={post_compress_turns})")
            elif action == "init":
                self._last_ema_user_id = new_marker
                logger.debug("[Nap] EMA marker initialized, skip first settlement")
            # "skip"：同轮重复回调或无 user，不结算

            # 计算阈值
            from agent.subagent import _read_context_window_tokens
            context_window = _read_context_window_tokens()
            threshold = _calc_dream_trigger_threshold_dynamic(context_window, ema_path)

            logger.info(f"[Nap] turn_count={turn_count}, threshold={threshold}, post_compress_turns={post_compress_turns}")

            if turn_count < threshold:
                return

            logger.info(f"[Nap] Triggering nap: {turn_count} turns >= threshold {threshold} (post_compress={len(post_compress_msgs)} msgs)")

            # 后台启动小憩模式（投递到全局整理队列，§3.1 入口 9）
            self._nap_running.set()
            try:
                fut = self._dispatch_to_pipeline("nap")
                if fut is None:
                    # None 窗口（队列未创建/主 loop 不可用）：同步执行兜底（§3.0 Option A）
                    try:
                        self._run_nap_background()
                    finally:
                        self._nap_running.clear()
            except Exception:
                self._nap_running.clear()
                raise
        except Exception as e:
            logger.warning(f'[Nap] Trigger check failed: {e}', exc_info=True)

    def _run_nap_background(self):
        """小憩模式：后台串行执行 entity-extractor → dream-evolver。

        简化版的睡眠模式——只做内容提炼和梦境进化，不压缩、不提取日志。
        entity-extractor 先入库精炼文档（LightRAG LLM 自动提取实体），
        dream-evolver 再精加工这些已入库的实体，避免实体碎片化。
        """
        try:
            from pathlib import Path

            from niu_api.compat import (
                _build_incremental_msg_text,
                _build_plain_history,
                _call_entity_extractor_on_f1,
                _extract_overflow_info,
                _incomplete_reason,
                _is_subagent_failure,
                _is_subagent_incomplete,
                _is_subagent_overflow,
                _parse_and_relay_f1,
                _parse_processed_up_to,
                _write_cursor_with_lock,
            )

            from agent.subagent import call_subagent_with_auto_answer

            niu_dir = Path.home() / ".niu"
            llm_config = self.llm_config
            db_messages = self._sync_get_messages()
            if not db_messages:
                return
            msg_tokens = self._recalc_msg_stats(db_messages)

            # ============================================================
            # Step 1 v2: entity-extractor 自读 F1（nap 为同步上下文，对齐扫描只在异步睡眠侧）
            # ============================================================
            from agent.md_mirror import F1_PATH

            f1_path = F1_PATH
            if os.path.exists(f1_path) and os.path.getsize(f1_path) > 0:
                try:
                    entity_result = _call_entity_extractor_on_f1(llm_config, f1_path)
                    logger.info(f"[Nap] entity-extractor completed, length={len(entity_result)}")

                    if _is_subagent_overflow(entity_result) or _is_subagent_incomplete(entity_result) or _is_subagent_failure(entity_result):
                        if _is_subagent_incomplete(entity_result):
                            logger.warning(f"[Nap] entity-extractor incomplete ({_incomplete_reason(entity_result)}) — F1 不剪切")
                        else:
                            overflow_info = _extract_overflow_info(entity_result)
                            logger.warning(f"[Nap] entity-extractor overflow: {overflow_info.get('turns_completed', 0)} turns, {overflow_info.get('tokens_used', 0)} tokens")
                        # overflow/incomplete/failure 时 F1 不剪切，下次重跑
                    else:
                        cut = _parse_and_relay_f1(entity_result, f1_path)
                        logger.info(f"[Nap] relay cut {cut} lines" if cut else "[Nap] relay skipped (invalid line number)")
                except Exception as e:
                    logger.error(f"[Nap] entity-extractor failed: {e}")
                    # entity-extractor 失败不阻断 dream-evolver
            else:
                logger.info("[Nap] entity-extractor: F1 空/不存在，跳过提炼")

            # ============================================================
            # Step 2: dream-evolver（梦境进化）
            # ============================================================
            dream_cursor_path = niu_dir / "last_dream_evolve.json"
            last_dream_id = self._read_cursor_locked(dream_cursor_path, "last_dream_evolve_id")

            # 重新获取消息（entity-extractor 可能已修改 LightRAG 知识图谱，重新读取确保 DB 一致）
            db_messages = self._sync_get_messages()
            if not db_messages:
                return
            msg_tokens = self._recalc_msg_stats(db_messages)

            dream_msg_ids = []
            _ = _build_incremental_msg_text(
                db_messages, last_dream_id, dream_msg_ids, msg_tokens
            )

            if not dream_msg_ids:
                logger.info("[Nap] dream-evolver: no new messages since cursor")
                return

            logger.info(f"[Nap] dream-evolver: {len(dream_msg_ids)} new messages since cursor")
            _id_set = set(dream_msg_ids)
            dream_msgs = [m for m in db_messages if (getattr(m, "id", "") or "") in _id_set]
            dream_history, dream_idx_to_id = _build_plain_history(dream_msgs)

            dream_prompt = """对以上消息中涉及的实体进行精加工（打标签、建关系、关联脑区、更新画像），并维护 skill 文件。

消息以 history 形式逐条传入，每条 content 前缀 [N] 极简编号（1-based）。处理完成后，在最终回复中包含 `@end`，最后一行输出 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果最后一段不是完整的对话单元（如 assistant 回复未完成、tool 调用缺少对应结果），请将 `processed_up_to` 设为你最后完整处理到的那个消息的编号，不要设到不完整的位置。如果未输出该行，程序会回退到区间末尾作为游标（兜底）。"""

            dream_result = call_subagent_with_auto_answer(
                agent_name="dream-evolver",
                task=dream_prompt,
                llm_config=llm_config,
                mcp_client=None,
                history=dream_history,
                context_fifo_threshold=-1,  # FIFO 保底
            )

            # 补全会话日期链（只补边/断边，不建实体；失败不阻塞游标推进）
            # 在 dream-evolver 完成后、游标推进前——当天会话实体（dream 挂边自动补 placeholder）
            # 已入图，链边与断跨越边当轮生效
            self._ensure_session_chain()

            # 游标推进：failure（[错误]/SUBAGENT_ERROR:）或 incomplete→不动；overflow→1/3 兜底；否则解析推进
            new_dream_id = last_dream_id
            if _is_subagent_failure(dream_result) or _is_subagent_incomplete(dream_result):
                # 失败前缀（注册冲突 [错误] / LLM 错误 SUBAGENT_ERROR:）与 incomplete（/stop、轮次耗尽等打断场景）：游标不动，下次续做
                if _is_subagent_failure(dream_result):
                    logger.warning(f"[Nap] dream-evolver failure: {dream_result[:200]} — cursor not advanced")
                else:
                    logger.warning(f"[Nap] dream-evolver incomplete ({_incomplete_reason(dream_result)}) — cursor not advanced")
            elif _is_subagent_overflow(dream_result):
                logger.warning(f"[Nap] dream-evolver overflow")
                if len(dream_msg_ids) > 10:
                    _fallback_idx = len(dream_msg_ids) // 3
                    new_dream_id = dream_msg_ids[_fallback_idx]
                    logger.info(f"[Nap] Overflow fallback: advancing cursor to 1/3 ({_fallback_idx}/{len(dream_msg_ids)})")
            else:
                _processed_idx = _parse_processed_up_to(dream_result)
                if _processed_idx is not None and _processed_idx in dream_idx_to_id:
                    new_dream_id = dream_idx_to_id[_processed_idx]
                    logger.info(f"[Nap] Dream cursor advanced: {new_dream_id}")
                elif dream_msg_ids:
                    new_dream_id = dream_msg_ids[-1]
                    logger.info(f"[Nap] Dream cursor fallback to range end: {new_dream_id}")

            # 游标校验
            if new_dream_id:
                fresh_msgs = self._sync_get_messages()
                fresh_ids = {getattr(m, "id", "") for m in fresh_msgs}
                if new_dream_id not in fresh_ids:
                    new_dream_id = last_dream_id
                    if new_dream_id and new_dream_id not in fresh_ids:
                        new_dream_id = ""

            if new_dream_id:
                from datetime import datetime
                _write_cursor_with_lock(dream_cursor_path, {
                    "last_dream_evolve_id": new_dream_id,
                    "last_evolve_at": datetime.now().isoformat(),
                })
                logger.info(f"[Nap] Dream cursor written: {new_dream_id}")

        except Exception as e:
            logger.error(f"[Nap] Background nap failed: {e}")
        finally:
            self._nap_running.clear()

    def _ensure_session_chain(self, max_days: int = 10) -> None:
        """小憩收尾：补全会话日期链（只补边/断边，不建实体）。

        从已有 YYYY-MM-DD会话 实体取最近 max_days 日历天窗口：
        断开跳过中间实体的跨越边（安全前提：两实体间仅 followed_by），
        补全相邻日期的 followed_by 边。失败不抛出（nap 收尾容错）。
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
                        "source_id": "nap_session_chain",
                        "file_path": "nap_session_chain",
                    }
                    for src, tgt in creates
                ]
                r = ingester.inject_custom_kg(
                    entities=[], relationships=rels, chunks=[], source_id="nap_session_chain"
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

    # --- Helper methods for _on_context_high_usage ---

    @staticmethod
    def _read_cursor(cursor_path, cursor_field):
        """Read a cursor ID from a JSON file.

        Returns the cursor value (str) or empty string if file missing / parse error.
        """
        if not cursor_path.exists():
            return ""
        try:
            data = json.loads(cursor_path.read_text(encoding="utf-8"))
            return data.get(cursor_field, "")
        except Exception as e:
            logger.warning(f"[Runner] Failed to read cursor {cursor_path.name}: {e}")
            return ""

    @staticmethod
    def _read_cursor_locked(cursor_path, cursor_field):
        """Read a cursor ID from a JSON file with file locking.

        Uses _flock/_funlock (same as _write_cursor_with_lock) to prevent
        read-write races between the main thread and background daemon thread.
        """
        if not cursor_path.exists():
            return ""
        try:
            import json
            from niu_api.compat import _flock, _funlock
            lock_path = cursor_path.with_suffix(".lock")
            with open(lock_path, "w") as lock_f:
                _flock(lock_f)
                try:
                    data = json.loads(cursor_path.read_text(encoding="utf-8"))
                    return data.get(cursor_field, "")
                finally:
                    _funlock(lock_f)
        except Exception as e:
            logger.warning(f"[Runner] Failed to read cursor {cursor_path.name}: {e}")
            return ""

    @staticmethod
    def _read_ema(ema_path):
        """读取持久化的 threshold EMA 值、样本数和参考线 EMA。

        Returns:
            (threshold: float, sample_count: int, ref_ema: float)
            文件不存在或损坏时返回 (_THRESHOLD_MIN, 0, 0)
        """
        # CQ-04: exists() 短路保证后续 open(lock_path) 时父目录已存在
        if not ema_path.exists():
            # 检测旧版 avg_tokens_per_turn.json 是否存在，提醒用户数据不会自动迁移
            old_path = ema_path.parent / "avg_tokens_per_turn.json"
            if old_path.exists():
                logger.warning(f"[Nap] Old EMA file {old_path.name} found but no longer used; "
                               f"threshold EMA starts fresh from {_THRESHOLD_MIN}")
            return _THRESHOLD_MIN, 0, 0
        try:
            import json
            from niu_api.compat import _flock, _funlock
            lock_path = ema_path.with_suffix(".lock")
            with open(lock_path, "w") as lock_f:
                _flock(lock_f)
                try:
                    data = json.loads(ema_path.read_text(encoding="utf-8"))
                    threshold = float(data.get("threshold", _THRESHOLD_MIN))
                    sc = int(data.get("sample_count", 0))
                    ref = data.get("ref_ema")
                    if ref is None:
                        # 旧文件迁移：用累积平均热启动参考线（旧分类参考线 = ct/sc）
                        ct = int(data.get("cumulative_tokens", 0))
                        ref = (ct / sc) if sc > 0 else 0.0
                    ref = float(ref)
                    # CQ-02: NaN/负值校验
                    if threshold != threshold or threshold < 0:  # NaN check: NaN != NaN
                        threshold = _THRESHOLD_MIN
                    if sc < 0:
                        sc = 0
                    if ref != ref or ref < 0:
                        ref = 0.0
                    return threshold, sc, ref
                finally:
                    _funlock(lock_f)
        # CQ-03: 收窄异常范围
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, OSError) as e:
            logger.warning(f"[Nap] Failed to read threshold EMA {ema_path.name}: {e}")
            return _THRESHOLD_MIN, 0, 0

    @staticmethod
    def _write_ema(ema_path, threshold: float, sample_count: int, ref_ema: float):
        """写入持久化的 threshold EMA 值（加文件锁）。"""
        try:
            import json
            from datetime import datetime
            from niu_api.compat import _flock, _funlock
            ema_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = ema_path.with_suffix(".lock")
            with open(lock_path, "w") as lock_f:
                _flock(lock_f)
                try:
                    ema_path.write_text(json.dumps({
                        "threshold": threshold,
                        "sample_count": sample_count,
                        "ref_ema": ref_ema,
                        "last_updated_at": datetime.now().isoformat(),
                    }, ensure_ascii=False), encoding="utf-8")
                finally:
                    _funlock(lock_f)
        except (OSError, TypeError) as e:
            logger.warning(f"[Nap] Failed to write threshold EMA {ema_path.name}: {e}")

    @staticmethod
    def _recalc_msg_stats(db_messages):
        """Recalculate per-message token counts.

        Returns list[int] of token counts per message.
        """
        msg_tokens = []
        try:
            from agent.token_calculator import TokenCalculator
            calc = TokenCalculator.get()
            for msg in db_messages:
                try:
                    t = calc.count_message_single(msg.role, msg.content or "", tool_calls=msg.tool_calls)
                except Exception:
                    t = max(1, len(msg.content or "") // 2) + 4
                msg_tokens.append(t)
        except ImportError:
            msg_tokens = [max(1, len(msg.content or "") // 2) + 4 for msg in db_messages]
        return msg_tokens

    def _run_subagent_step(self, step_name, cursor_path, cursor_field,
                           prompt, llm_config, last_cursor_id,
                           fallback_ids, timestamp_field,
                           history=None, context_fifo_threshold=None,
                           idx_to_id=None):
        """Run a sub-agent step with timeout, cursor extraction, validation and write-back.

        Parameters
        ----------
        step_name : str
            Sub-agent name passed to call_subagent (e.g. "dream-evolver").
        cursor_path : Path
            JSON file that persists the cursor.
        cursor_field : str
            Key name inside the cursor JSON (e.g. "last_dream_evolve_id").
        prompt : str
            The task prompt for the sub-agent (already truncated).
        llm_config : dict
            LLM configuration forwarded to call_subagent.
        last_cursor_id : str
            Previous cursor value — used as revert target on validation failure.
        fallback_ids : list[str]
            Message IDs from the incremental batch; last element is the
            fallback cursor when extraction fails.
        timestamp_field : str
            Key name for the timestamp written into the cursor JSON
            (e.g. "last_dream_evolve_at").

        Returns
        -------
        (result_text, new_cursor_id) : tuple[str, str]
            result_text is the raw sub-agent output (empty on failure).
            new_cursor_id is the validated cursor after the step.
        """
        import concurrent.futures as _cf

        from niu_api.compat import (
            _extract_overflow_info,
            _incomplete_reason,
            _is_subagent_incomplete,
            _is_subagent_overflow,
            _is_subagent_failure,
            _write_cursor_with_lock,
        )

        from agent.subagent import call_subagent_with_auto_answer

        # --- call sub-agent ---
        with _cf.ThreadPoolExecutor(max_workers=1) as executor:
            _kwargs = {"llm_config": llm_config, "mcp_client": None}
            if history is not None:
                _kwargs["history"] = history
            if context_fifo_threshold is not None:
                _kwargs["context_fifo_threshold"] = context_fifo_threshold
            future = executor.submit(call_subagent_with_auto_answer, step_name, prompt, **_kwargs)
            try:
                result = future.result()
            except Exception as e:
                logger.warning(f"[Runner] Force: {step_name} failed: {e}")
                result = ""

        logger.info(f"[Runner] Force: {step_name} completed, length={len(result)}")

        # --- cursor advance: overflow/incomplete→don't move; else parse processed_up_to=N + lookup idx_to_id, fallback fallback_ids[-1] ---
        new_cursor_id = last_cursor_id
        if _is_subagent_overflow(result) or _is_subagent_incomplete(result) or _is_subagent_failure(result):
            if _is_subagent_incomplete(result):
                logger.warning(f"[{step_name}] incomplete ({_incomplete_reason(result)}) — cursor not advanced")
            else:
                overflow_info = _extract_overflow_info(result)
                logger.warning(f"[{step_name}] overflow: {overflow_info.get('turns_completed', 0)} turns")
            # overflow/incomplete 时游标不动
            new_cursor_id = last_cursor_id
        else:
            from niu_api.compat import _parse_processed_up_to
            _processed_idx = _parse_processed_up_to(result)
            if _processed_idx is not None and idx_to_id and _processed_idx in idx_to_id:
                new_cursor_id = idx_to_id[_processed_idx]
                logger.info(f"[{step_name}] Cursor advanced per processed_up_to={_processed_idx} -> {new_cursor_id}")
            elif fallback_ids:
                new_cursor_id = fallback_ids[-1]  # 兜底
                logger.info(f"[{step_name}] Cursor fallback to range end: {new_cursor_id}")
            else:
                new_cursor_id = last_cursor_id

        # --- cursor validation ---
        if new_cursor_id:
            try:
                fresh_msgs = self._sync_get_messages()
                fresh_ids = {getattr(m, "id", "") for m in fresh_msgs}
                if new_cursor_id not in fresh_ids:
                    logger.warning(f"[{step_name}] Cursor {new_cursor_id} deleted, reverting to {last_cursor_id}")
                    new_cursor_id = last_cursor_id
                    if new_cursor_id and new_cursor_id not in fresh_ids:
                        new_cursor_id = ""
            except Exception:
                logger.warning(f"[{step_name}] Could not verify cursor, keeping {new_cursor_id}")

        # --- cursor write-back ---
        if new_cursor_id:
            _write_cursor_with_lock(cursor_path, {
                cursor_field: new_cursor_id,
                timestamp_field: datetime.now().isoformat(),
            })

        return result, new_cursor_id

    def _dispatch_to_pipeline(self, kind: str, request: dict | None = None, held: bool = False):
        """跨线程投递整理任务到全局队列（§3.1 跨线程投递桥：call_soon_threadsafe）。

        返回 concurrent.futures.Future（调用方可 .result(timeout) 等待）；
        队列未创建 / 主 loop 不可用（None 窗口，§3.0）→ 返回 None，调用方按 Option A
        同步执行兜底。压缩类（force/runner-force）去重键与 _pipeline_enqueue 一致（§3.2）。
        """
        from concurrent.futures import Future

        from niu_api import chat as _chat_module
        from niu_api.compat import (
            _pipeline_queue, _active_compress_futs, _drop_active_compress,
        )

        if _pipeline_queue is None:
            return None
        loop = _chat_module._main_loop
        if loop is None or loop.is_closed():
            return None
        request = request or {}
        fut: Future = Future()
        if kind in ("force", "runner-force"):
            key = (kind, bool(request.get("skip_compress")), request.get("force_protect_recent"))
            active = _active_compress_futs.get(key)
            if active is not None and not active.done():
                return active
            _active_compress_futs[key] = fut
            fut.add_done_callback(_drop_active_compress)
        item = {"kind": kind, "request": request, "held": held, "result": fut}
        try:
            loop.call_soon_threadsafe(_pipeline_queue.put_nowait, item)
        except Exception:
            # 投递失败：回滚去重登记（如有）→ 返回 None 让调用方同步兜底
            if kind in ("force", "runner-force"):
                _drop_active_compress(fut)
            return None
        return fut

    def _execute_force_pipeline(self) -> dict | None:
        """运行完整 force 压缩管道（entity → dream → journal → context-manager → DB 写删）。

        由全局整理队列 worker（§3.1 入口 8，kind="runner-force"）在后台线程调用。
        不含转换块（dict 转换/孤立 tool 清理/system 保留/messages[:] 回写）——转换块由
        调用方 _on_context_high_usage 在回调内执行，保证 agent_loop 的 messages 为 dict 列表契约。
        返回 {"status": "ok"} / {"status": "skipped", "reason": ...} / {"status": "error", ...}。
        """
        from pathlib import Path as _Path

        from niu_api.compat import (
            _build_compress_llm_config,
            _build_compress_history,
            _build_force_prompt,
            _build_incremental_msg_text,
            _build_journal_task,
            _build_plain_history,
            _parse_idx_list,
            _strip_analysis,
            _write_cursor_with_lock,
        )

        from agent.subagent import (
            _read_compress_target_tokens,
            _read_context_window_tokens,
            _read_protect_recent_count,
            call_subagent_with_auto_answer,
        )

        try:
            # === 读取游标 ===
            niu_dir = _Path.home() / ".niu"
            dream_cursor_path = niu_dir / "last_dream_evolve.json"
            compress_cursor_path = niu_dir / "last_compress.json"
            journal_cursor_path = niu_dir / "last_journal.json"

            last_dream_evolve_id = self._read_cursor(dream_cursor_path, "last_dream_evolve_id")
            last_compress_id = self._read_cursor(compress_cursor_path, "last_compress_id")
            last_journal_id = self._read_cursor(journal_cursor_path, "last_journal_id")

            # === 从 DB 读取消息 ===
            db_messages = self._sync_get_messages()
            if not db_messages:
                logger.info("[Runner] No messages in DB, skipping compress")
                return

            msg_tokens = self._recalc_msg_stats(db_messages)
            estimated_tokens = sum(msg_tokens)
            message_count = len(db_messages)
            context_window_tokens = _read_context_window_tokens()

            # 优先用真实 prompt_tokens 计算 usage_percent
            real_prompt_tokens = self.handler._last_prompt_tokens if hasattr(self.handler, '_last_prompt_tokens') else 0
            if real_prompt_tokens > 0:
                usage_percent = (real_prompt_tokens / context_window_tokens) * 100 if context_window_tokens > 0 else 0
                display_tokens = real_prompt_tokens
                logger.info(f"[Runner] Force compress: {message_count} messages, real_tokens={real_prompt_tokens}, est_tokens={estimated_tokens}, {usage_percent:.1f}%")
            else:
                usage_percent = (estimated_tokens / context_window_tokens) * 100 if context_window_tokens > 0 else 0
                display_tokens = estimated_tokens
                logger.info(f"[Runner] Force compress: {message_count} messages, {estimated_tokens} tokens, {usage_percent:.1f}%")


            llm_config = self.llm_config

            # === 步骤 2/4: dream-evolver（增量 task 方式）===
            if is_stop_requested():
                logger.warning("[Runner] Stop requested, aborting force compress")
                return

            # 重新获取消息列表（entity 可能已修改 DB）
            db_messages = self._sync_get_messages()
            msg_tokens = self._recalc_msg_stats(db_messages)

            new_dream_id = last_dream_evolve_id
            dream_force_msg_ids = []
            _ = _build_incremental_msg_text(
                db_messages, last_dream_evolve_id, dream_force_msg_ids, msg_tokens
            )
            logger.info(f"[Runner] Force: starting dream-evolver ({len(dream_force_msg_ids)} incremental messages)")

            if dream_force_msg_ids:
                dream_force_prompt = """对以上消息中涉及的实体进行精加工（打标签、建关系、关联脑区、更新画像），并维护 skill 文件。

消息以 history 形式逐条传入，每条 content 前缀 [N] 极简编号（1-based）。处理完成后，在最终回复中包含 `@end`，最后一行输出 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果最后一段不是完整的对话单元（如 assistant 回复未完成、tool 调用缺少对应结果），请将 `processed_up_to` 设为你最后完整处理到的那个消息的编号，不要设到不完整的位置。如果未输出该行，程序会回退到区间末尾作为游标（兜底）。"""
                # 构造增量 history + idx_to_id 映射
                _id_set = set(dream_force_msg_ids)
                dream_force_incremental_msgs = [m for m in db_messages if (getattr(m, "id", "") or "") in _id_set]
                dream_force_history, dream_force_idx_to_id = _build_plain_history(dream_force_incremental_msgs)

                _, new_dream_id = self._run_subagent_step(
                    "dream-evolver", dream_cursor_path, "last_dream_evolve_id",
                    dream_force_prompt, llm_config, last_dream_evolve_id,
                    dream_force_msg_ids, "last_evolve_at",
                    history=dream_force_history, context_fifo_threshold=-1,  # FIFO 保底
                    idx_to_id=dream_force_idx_to_id,
                )

                if is_stop_requested():
                    logger.warning("[Runner] Stop requested, aborting force compress")
                    return
            else:
                logger.info("[Runner] Force: dream-evolver no incremental messages")

            # === 步骤 2.5/4: journal-agent（force 模式，始终调用）===
            if is_stop_requested():
                logger.warning("[Runner] Stop requested, aborting force compress")
                return

            db_messages = self._sync_get_messages()
            msg_tokens = self._recalc_msg_stats(db_messages)

            new_journal_id = last_journal_id
            journal_force_msg_ids = []
            _ = _build_incremental_msg_text(
                db_messages, last_journal_id, journal_force_msg_ids, msg_tokens
            )
            logger.info(f"[Runner] Force: starting journal-agent ({len(journal_force_msg_ids)} incremental messages)")

            if journal_force_msg_ids:
                journal_force_prompt = _build_journal_task()  # 纯指令，无参（含 processed_up_to 说明）
                # 构造增量 history + idx_to_id 映射
                _id_set = set(journal_force_msg_ids)
                journal_force_incremental_msgs = [m for m in db_messages if (getattr(m, "id", "") or "") in _id_set]
                journal_force_history, journal_force_idx_to_id = _build_plain_history(journal_force_incremental_msgs)

                _, new_journal_id = self._run_subagent_step(
                    "journal-agent", journal_cursor_path, "last_journal_id",
                    journal_force_prompt, llm_config, last_journal_id,
                    journal_force_msg_ids, "last_journal_at",
                    history=journal_force_history, context_fifo_threshold=-1,  # FIFO 保底
                    idx_to_id=journal_force_idx_to_id,
                )
                logger.info(f"[Runner] Force: Journal cursor updated: {new_journal_id}")

                if is_stop_requested():
                    logger.warning("[Runner] Stop requested, aborting force compress")
                    return
            else:
                logger.info("[Runner] Force: journal-agent no incremental messages")

            # === 步骤 3/4: context-manager force prompt — 一轮 JSON 文件方案 ===
            if is_stop_requested():
                logger.warning("[Runner] Stop requested, aborting force compress")
                return

            # 压缩前置游标追平校验（§4.3）：提炼/进化游标未追平则本次不压缩（runner 侧 protect 同源）
            protect_recent_count = _read_protect_recent_count()
            if not _cursors_caught_up(db_messages, protect_recent_count):
                logger.warning("[Runner] Force: 还有消息未提炼完，本次不压缩")
                return {"status": "skipped", "reason": "还有消息未提炼完，本次不压缩"}

            # 重新读取 compress 游标
            last_compress_id = self._read_cursor(compress_cursor_path, "last_compress_id")

            target_tokens = _read_compress_target_tokens()
            compress_plan_path = os.path.expanduser("~/.niu/compress_plan.json")
            # 清理上次的残留计划文件
            if os.path.exists(compress_plan_path):
                try:
                    os.remove(compress_plan_path)
                except OSError:
                    pass  # Windows 文件锁，忽略

            # 构造 history 列表 + idx 映射（参考 compat.py 模式三）
            # _build_compress_history 内部处理 exclude_protected（PROTECTED 消息不进 history、不分配 idx）
            # out_msg_ids 是出参：函数内部 append 真实 message_id，与 history 等长同顺序
            _force_msg_ids = []
            _force_history, _f_idx_to_id = _build_compress_history(
                db_messages, msg_tokens,
                out_msg_ids=_force_msg_ids,
                protect_recent=protect_recent_count,
                exclude_protected=True,
            )
            # 构造反向映射 id→idx（用于 dream 安全边界计算）
            _f_id_to_idx = {mid: idx for idx, mid in _f_idx_to_id.items()}

            # 计算 dream 安全边界 idx（参考 compat.py 模式三）
            # new_dream_id 在 runner.py 前面 dream-evolver 阶段已算出
            # dream 哨兵：0（无 new_dream_id——首次 force 或 dream 失败）→ prompt 渲染 idx > 0 → 所有消息受保护（全保护：只 update 不删）
            # len(_force_msg_ids)（new_dream_id 不在映射）→ 渲染 idx > 全量 → 实际不限制删除。
            # 两种哨兵均为纯数字插值，_build_force_prompt 内部无判断分支。
            if not new_dream_id:
                _dream_idx_in_force = 0
            else:
                _dream_idx_in_force = _f_id_to_idx.get(new_dream_id, len(_force_msg_ids))

            # 复用上文的 target_tokens（不重复读配置）
            prompt = _build_force_prompt(
                display_tokens, target_tokens, usage_percent,
                _force_history, last_compress_id, _dream_idx_in_force,
            )

            # 压缩 LLM 配置：按知识图谱（lightrag 段）用户配置 + max_tokens（输出预算）
            llm_config_with_max = _build_compress_llm_config()

            def run_context_manager_force():
                return call_subagent_with_auto_answer(
                    agent_name="context-manager",
                    task=prompt,
                    llm_config=llm_config_with_max,
                    mcp_client=None,
                    context_fifo_threshold=-1,  # FIFO 保底
                    history=_force_history,
                    bypass_at_prefix=True,  # 一轮出方案：绕过@前缀拦截，禁止追问第二轮（防上下文溢出）
                )

            try:
                result = run_context_manager_force()  # 同步调用，不用 asyncio.to_thread
            except Exception as e:
                logger.warning(f"[Runner] Force: context-manager failed: {e}")
                result = ""

            _force_halved_msg_ids = None  # 降级砍半的前半段 msg_ids
            if is_stop_requested():
                logger.warning("[Runner] Stop requested, aborting force compress")
                return
            # SUBAGENT_ERROR: context-manager LLM 错误，跳过不删消息
            if result and result.startswith("SUBAGENT_ERROR:"):
                error_msg = result[len("SUBAGENT_ERROR:"):]
                logger.warning(f"[Compact] Runner: context-manager LLM error: {error_msg}")
                return {"status": "skipped", "reason": f"LLM error: {error_msg}"}

            # 截断时触发三级降级（关思考链→砍半消息→报失败）
            if result and result.startswith("COMPACT_TRUNCATED:"):
                logger.warning("[Compact] runner.py force output truncated, starting degradation")
                from niu_api.compat import _compact_with_degradation_sync, _build_force_prompt as _bfp
                result_str, actual_msg_ids, halved_msg_ids = _compact_with_degradation_sync(
                    agent_name="context-manager",
                    prompt=prompt,
                    compress_history=_force_history,
                    compress_msg_ids=_force_msg_ids,
                    llm_config=llm_config_with_max,
                    prompt_builder=_bfp,
                    prompt_builder_kwargs={
                        "display_tokens": display_tokens,
                        "compress_target_tokens": target_tokens,
                        "usage_percent": usage_percent,
                        "force_history": _force_history,
                        "last_compress_id": last_compress_id,
                        "dream_idx_in_force": _dream_idx_in_force,
                    },
                    stop_aware=True,
                    call_fn=call_subagent_with_auto_answer,
                )
                if result_str is None:
                    return {"status": "skipped", "reason": "compress failed: output truncated after all degradation steps"}
                # 降级成功，用返回值替代 result
                result = result_str
                _force_msg_ids = actual_msg_ids
                _force_halved_msg_ids = halved_msg_ids
                # 重建 idx→UUID 映射（砍半后 msg_ids 变化，旧映射失效）
                _f_idx_to_id = {}
                for _i, _mid in enumerate(_force_msg_ids):
                    _f_idx_to_id[_i + 1] = _mid

            # 正常返回，剥离 <analysis> 草稿块（在解析前）
            logger.info(f"[Runner] Force: context-manager completed, length={len(result)}")
            result = _strip_analysis(result)

            # === 从 sub-agent 回复中解析压缩计划（idx 格式） ===
            new_compress_id = last_compress_id
            try:
                keep_idxs: set[int] = set()
                update_list: list[tuple[int, str]] = []
                cursor_idx: int | None = None

                for line in result.splitlines():
                    line = line.strip()
                    if line.lower().startswith("keep="):
                        keep_idxs = _parse_idx_list(line.split("=", 1)[1].strip())
                    elif line.lower().startswith("update="):
                        update_str = line.split("=", 1)[1].strip()
                        if update_str:
                            for part in update_str.split(";"):
                                part = part.strip()
                                if "|" in part:
                                    idx_str, content = part.split("|", 1)
                                    try:
                                        idx = int(idx_str.strip())
                                        _c = content.strip()
                                        if not _c.startswith('[摘要]') and not _c.startswith('[合并]'):
                                            _c = f'[摘要] {_c}'
                                        update_list.append((idx, _c))
                                    except ValueError:
                                        pass
                    elif line.lower().startswith("cursor="):
                        cursor_str = line.split("=", 1)[1].strip()
                        try:
                            cursor_idx = int(cursor_str)
                        except ValueError:
                            pass

                if not keep_idxs:
                    raise ValueError("No keep= line found in sub-agent reply")

                # 计算删除列表
                all_force_idxs = set(_f_idx_to_id.keys())
                delete_idxs = all_force_idxs - keep_idxs

                # 转换为 UUID
                deletes = [_f_idx_to_id[i] for i in sorted(delete_idxs) if i in _f_idx_to_id]
                # 砍半掉的前半段 msg_ids 加入删除列表
                if _force_halved_msg_ids:
                    deletes.extend(_force_halved_msg_ids)
                updates = [
                    {"message_id": _f_idx_to_id[idx], "content": content}
                    for idx, content in update_list if idx in _f_idx_to_id
                ]
                if cursor_idx and cursor_idx in _f_idx_to_id:
                    new_compress_id = _f_idx_to_id[cursor_idx]
                else:
                    logger.warning(f"[Compact] runner.py force cursor idx {cursor_idx} not in mapping, keeping last_compress_id")
                    # new_compress_id 保持初始值（last_compress_id）

                logger.info(f"[Runner] Force: Parsed from content: keep={len(keep_idxs)}, delete={len(deletes)}, update={len(updates)}, cursor_idx={cursor_idx}")

                # 重新获取消息列表
                fresh_messages = self._sync_get_messages()
                existing_ids = {getattr(m, "id", "") for m in fresh_messages}
                valid_deletes = [mid for mid in deletes if mid in existing_ids]
                valid_deletes = list(dict.fromkeys(valid_deletes))
                # 校验游标有效性
                if new_compress_id and new_compress_id not in existing_ids:
                    logger.warning(f"[Runner] Force: last_compress_id {new_compress_id} not in messages, reverting to {last_compress_id}")
                    new_compress_id = last_compress_id
                if new_compress_id and new_compress_id not in existing_ids:
                    logger.warning(f"[Runner] Force: Fallback last_compress_id {new_compress_id} also invalid, clearing cursor")
                    new_compress_id = ""

                # 保护游标
                cursor_ids_set = {cid for cid in [new_compress_id, new_dream_id] if cid}
                for cursor_id in cursor_ids_set:
                    if cursor_id in valid_deletes:
                        valid_deletes.remove(cursor_id)
                        logger.warning(f"[Runner] Force: Protected cursor message {cursor_id} from deletion")

                valid_updates = [u for u in updates if isinstance(u, dict) and u.get("message_id") and u["message_id"] in existing_ids]
                cursor_updates = [u for u in valid_updates if u.get("message_id", "") in cursor_ids_set]
                if cursor_updates:
                    logger.warning(f"[Runner] Force: Removing cursor messages from updates: {[u.get('message_id') for u in cursor_updates]}")
                    valid_updates = [u for u in valid_updates if u.get("message_id", "") not in cursor_ids_set]

                # dream 安全边界
                if new_dream_id:
                    dream_boundary_idx = -1
                    for i, m in enumerate(fresh_messages):
                        if (getattr(m, "id", "") or "") == new_dream_id:
                            dream_boundary_idx = i
                            break
                    if dream_boundary_idx >= 0:
                        post_dream_ids = {getattr(m, "id", "") for m in fresh_messages[dream_boundary_idx + 1:]}
                        unsafe_deletes = [mid for mid in valid_deletes if mid in post_dream_ids]
                        if unsafe_deletes:
                            logger.warning(f"[Runner] Force: Protecting {len(unsafe_deletes)} messages after dream cursor from deletion")
                            valid_deletes = [mid for mid in valid_deletes if mid not in post_dream_ids]
                        unsafe_updates = [u for u in valid_updates if u.get("message_id", "") in post_dream_ids]
                        if unsafe_updates:
                            logger.warning(f"[Runner] Force: Protecting {len(unsafe_updates)} messages after dream cursor from content replacement")
                            valid_updates = [u for u in valid_updates if u.get("message_id", "") not in post_dream_ids]

                # 保护最近完整用户会话段落（从最近 user 消息开始）
                protect_recent_count = _read_protect_recent_count()
                protected_force_ids: set[str] = set()
                if protect_recent_count > 0:
                    from niu_api.compat import _find_protected_range
                    _protect_start = _find_protected_range(fresh_messages, protect_recent_count)
                    protected_force_ids = {getattr(fresh_messages[i], "id", "") or "" for i in range(_protect_start, len(fresh_messages))}
                    removed_deletes = [mid for mid in valid_deletes if mid in protected_force_ids]
                    if removed_deletes:
                        logger.warning(f"[Runner] Force: Protecting {len(removed_deletes)} recent messages from deletion: {removed_deletes}")
                        valid_deletes = [mid for mid in valid_deletes if mid not in protected_force_ids]
                    removed_updates = [u for u in valid_updates if u.get("message_id", "") in protected_force_ids]
                    if removed_updates:
                        logger.warning(f"[Runner] Force: Protecting {len(removed_updates)} recent messages from update")
                        valid_updates = [u for u in valid_updates if u.get("message_id", "") not in protected_force_ids]

                # 防止 delete/update 重叠
                update_ids = {u.get("message_id", "") for u in valid_updates}
                overlap_ids = update_ids & set(valid_deletes)
                if overlap_ids:
                    logger.warning(f"[Runner] Force: Removing {len(overlap_ids)} IDs from deletes that also appear in updates: {overlap_ids}")
                    valid_deletes = [mid for mid in valid_deletes if mid not in overlap_ids]

                if len(valid_deletes) < len(deletes):
                    logger.warning(f"[Runner] Force: Filtered {len(deletes) - len(valid_deletes)} invalid delete IDs")
                if len(valid_updates) < len(updates):
                    logger.warning(f"[Runner] Force: Filtered {len(updates) - len(valid_updates)} invalid update IDs")

                # 级联删除
                from niu_api.compat import _cascade_tool_chain_deletes, _cascade_tool_chain_updates
                _cascade_protected = cursor_ids_set | (protected_force_ids if protect_recent_count > 0 else set())
                cascade_del = _cascade_tool_chain_deletes(fresh_messages, valid_deletes, protected_ids=_cascade_protected)
                valid_deletes = cascade_del.delete_ids
                dangling_tc_cleanups = cascade_del.dangling_cleanups
                cascade_upd = _cascade_tool_chain_updates(fresh_messages, valid_updates)
                valid_updates = cascade_upd.updates
                cascade_delete_ids = cascade_upd.cascade_delete_ids
                if cascade_delete_ids:
                    existing = set(valid_deletes)
                    for cid in cascade_delete_ids:
                        if cid not in existing:
                            valid_deletes.append(cid)
                            existing.add(cid)

                _post_update_ids = {u.get("message_id", "") for u in valid_updates}
                _post_overlap = _post_update_ids & set(valid_deletes)
                if _post_overlap:
                    logger.warning(f"[Runner] Force: Cascade created delete/update overlap: {_post_overlap}")
                    valid_deletes = [mid for mid in valid_deletes if mid not in _post_overlap]

                # 清理受保护 assistant 的悬空 tool_calls（同步 DB 操作）
                if dangling_tc_cleanups:
                    import sqlite3
                    _db_path = os.path.join(os.path.expanduser("~"), ".niu", "messages.db")
                    for cleanup in dangling_tc_cleanups:
                        mid = cleanup["message_id"]
                        dangling_ids = cleanup["dangling_tc_ids"]
                        m = next((m for m in fresh_messages if getattr(m, "id", "") == mid), None)
                        if m and getattr(m, "tool_calls", None):
                            tcs = m.tool_calls
                            if isinstance(tcs, str):
                                tcs = json.loads(tcs)
                            valid_tcs = [tc for tc in tcs if tc.get("id", "") not in dangling_ids]
                            with sqlite3.connect(_db_path) as conn:
                                if valid_tcs:
                                    conn.execute("UPDATE messages SET tool_calls = ? WHERE id = ?",
                                               (json.dumps(valid_tcs, ensure_ascii=False), mid))
                                else:
                                    conn.execute("UPDATE messages SET tool_calls = '[]' WHERE id = ?", (mid,))
                                conn.commit()
                            logger.info(f"[Runner] Force: Cleaned {len(dangling_ids)} dangling tool_calls from protected assistant {mid}")

                # 执行删除
                if valid_deletes:
                    del_result = self._sync_delete_messages(valid_deletes)
                    if del_result:
                        logger.info(f"[Runner] Force: Deleted {del_result.get('deleted_count', 0)} messages, freed {del_result.get('freed_tokens', 0)} tokens")

                # 执行更新
                for upd in valid_updates:
                    mid = upd.get("message_id", "")
                    content = upd.get("content", "")
                    if mid and content:
                        clear_tc = upd.get("clear_tool_calls", False)
                        ok = self._sync_update_message(mid, content, clear_tool_calls=clear_tc)
                        if ok:
                            logger.info(f"[Runner] Force: Updated message {mid}")
                        else:
                            logger.warning(f"[Runner] Force: Failed to update message {mid}")

                logger.info(f"[Runner] Force: Compression plan executed: {len(valid_deletes)} deletes, {len(valid_updates)} updates")
            except ValueError as e:
                logger.error(f"[Runner] Force: Failed to parse compression plan: {e}")
            except Exception as e:
                logger.error(f"[Runner] Force: Failed to execute compress plan: {e}")

            # 写入 compress 游标
            if new_compress_id:
                _write_cursor_with_lock(compress_cursor_path, {
                    "last_compress_id": new_compress_id,
                    "last_compress_at": datetime.now().isoformat(),
                })
                logger.info(f"[Runner] Force: Compress cursor updated: {new_compress_id}")

            return {"status": "ok"}
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            logger.error(f"[Runner] Proactive compress failed: {e}\n{tb}")
            return {"status": "error", "message": str(e)}

    def _on_context_high_usage(self, messages, tokens_used, tokens_limit):
        """主 Agent 上下文超阈值回调 — 内联执行压缩管道，随后执行转换块

        压缩管道（_execute_force_pipeline）直接同步调用（Case 2 内联化：阻塞的是
        对话请求自身，不经全局整理队列、无外层等待上限——仅 Stop 可断，管道阶段间
        十余处 is_stop_requested 检查点）。然后从 DB 重载消息并原地修改 messages 列表
        （dict 转换/孤立 tool 清理/system 保留/cache_control 重注入/messages[:] 回写——
        agent_loop L845-848 契约：messages 为 dict 列表）。
        agent_loop 不需要知道 DB、不需要导入 niu_api 的任何东西。
        """

        logger.info(f"[Runner] Context high usage: {tokens_used}/{tokens_limit} tokens "
                     f"({tokens_used/tokens_limit:.1%})")

        # 广播压缩状态 started 事件（前端圆环动画启动，模式1 auto）
        try:
            from niu_api.chat import notify_compact_status_sync
            notify_compact_status_sync("started", mode="auto")
        except Exception:
            pass

        try:
            # === 入口 8：内联执行压缩管道（Case 2 直调；None 返回 = Stop 中断/skip 天然覆盖）===
            _result = self._execute_force_pipeline()

            # === 重新加载消息，原地修改 agent_loop 的 messages 列表 ===
            fresh_db_msgs = self._sync_get_messages()
            if fresh_db_msgs:
                # 从 assistant 消息的 tool_calls 构建 tool_call_id → tool_name 映射
                # 同时收集所有有效的 tool_call_id（压缩可能留下孤立的 tool 消息）
                # （与 agent_loop.py 历史还原路径相同逻辑）
                _tc_id_to_name: dict[str, str] = {}
                _valid_tc_ids: set[str] = set()
                for m in fresh_db_msgs:
                    if m.role == "assistant" and m.tool_calls:
                        for tc in m.tool_calls:
                            tc_id = tc.get("id", "")
                            tc_name = tc.get("function", {}).get("name", "")
                            if tc_id and tc_name:
                                _tc_id_to_name[tc_id] = tc_name
                            if tc_id:
                                _valid_tc_ids.add(tc_id)

                # 收集所有 tool 消息的 tool_call_id，用于验证 assistant tool_calls 完整性
                _tool_response_ids: set[str] = set()
                for m in fresh_db_msgs:
                    if m.role == "tool" and m.tool_call_id:
                        _tool_response_ids.add(m.tool_call_id)

                # 收集需要清理的消息
                _orphan_tool_mids = []  # 孤立 tool 消息 ID（需从 DB 删除）
                _dangling_tc_updates = []  # 悬空 tool_calls 更新（需更新 DB）

                fresh_msgs = []
                for msg in fresh_db_msgs:
                    d = {
                        "role": msg.role,
                        "content": msg.content or "",
                    }
                    if msg.tool_calls:
                        valid_tcs = [tc for tc in msg.tool_calls if tc.get("id") in _tool_response_ids]
                        if valid_tcs and len(valid_tcs) < len(msg.tool_calls):
                            d["tool_calls"] = valid_tcs
                            # 收集悬空 tool_calls 清理（循环后统一执行）
                            _dangling_tc_updates.append({
                                "message_id": msg.id,
                                "valid_tcs": valid_tcs,
                                "original_count": len(msg.tool_calls),
                            })
                        elif valid_tcs:
                            d["tool_calls"] = valid_tcs
                        # 如果所有 tool_calls 都没有响应，不设置 tool_calls（变成纯文本消息）
                        elif msg.tool_calls:
                            # 收集清空 tool_calls（保留原始 content）
                            _dangling_tc_updates.append({
                                "message_id": msg.id,
                                "valid_tcs": [],
                                "original_count": len(msg.tool_calls),
                            })
                    if msg.tool_call_id:
                        if msg.tool_call_id not in _valid_tc_ids:
                            logger.warning(f"[Runner] Force: Skipping orphan tool message: tool_call_id={msg.tool_call_id}")
                            _orphan_tool_mids.append(msg.id)
                            continue
                        d["tool_call_id"] = msg.tool_call_id
                        _tn = _tc_id_to_name.get(msg.tool_call_id, "")
                        if _tn:
                            d["name"] = _tn
                    fresh_msgs.append(d)

                # 统一执行 DB 清理（遍历后执行，避免遍历中修改）
                if _orphan_tool_mids or _dangling_tc_updates:
                    try:
                        import sqlite3
                        _cleanup_db_path = os.path.join(os.path.expanduser("~"), ".niu", "messages.db")
                        with sqlite3.connect(_cleanup_db_path) as _c:
                            _c.execute("PRAGMA busy_timeout=5000")
                            for mid in _orphan_tool_mids:
                                _c.execute("DELETE FROM messages WHERE id = ?", (mid,))
                                logger.info(f"[Force-reload] Deleted orphan tool message {mid}")
                            for upd in _dangling_tc_updates:
                                mid = upd["message_id"]
                                if upd["valid_tcs"]:
                                    _c.execute(
                                        "UPDATE messages SET tool_calls = ? WHERE id = ?",
                                        (json.dumps(upd["valid_tcs"], ensure_ascii=False), mid),
                                    )
                                    logger.info(f"[Force-reload] Cleaned dangling tool_calls for assistant {mid}: {upd['original_count']} -> {len(upd['valid_tcs'])}")
                                else:
                                    # 清空 tool_calls 但保留原始 content
                                    _c.execute("UPDATE messages SET tool_calls = '[]' WHERE id = ?", (mid,))
                                    logger.info(f"[Force-reload] Cleared all tool_calls for assistant {mid}")
                            _c.commit()
                    except Exception as e:
                        logger.warning(f"[Force-reload] DB cleanup failed: {e}")
                # 保留 system prompt（messages[0]），替换其余消息
                system_msg = messages[0] if messages and messages[0].get("role") == "system" else None
                if system_msg:
                    # 压缩后重建 system message（确保 Claude cache_control 不丢失）
                    # injection="" 和 memory_section=""：本轮 _on_before_llm 会重新读 memory + 重新注入
                    # （动态注入已从 _on_turn_end 移到 LLM 调用前，memory 也已每轮重读）
                    self._assemble_system_message([system_msg], "", "", self.default_model)
                    messages[:] = [system_msg] + fresh_msgs
                else:
                    messages[:] = fresh_msgs
                logger.info(f"[Runner] Force: Reloaded {len(fresh_msgs)} messages from DB after compress")

            return _result
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            logger.error(f"[Runner] Proactive compress failed: {e}\n{tb}")
        finally:
            # 无论成功/失败/异常都必须广播 done，避免前端圆环卡死
            try:
                from niu_api.chat import notify_compact_status_sync
                notify_compact_status_sync("done", mode="auto")
            except Exception:
                pass

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

        # 组装 system message（首轮就按 model 决定格式，Claude 走 cache_control）
        # injection="" 因为动态注入移到 _on_before_llm 首轮
        # memory_section="" 因为 _on_before_llm 首轮会重读 memory.json 覆盖（入口先放空骨架）
        # resources 文本在实例属性里，_on_before_llm 首轮会合并进 injection
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
            # 清理残留挂起的同步子 Agent session（主 Agent 不再调 chat-with-xxx 时）
            cleanup_suspended_sync_subagents()
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
                                         tool_calls=tool_calls, tool_call_id=tool_call_id)
        if msg_id is None:
            return None

        # 通知 SSE（仅 assistant 消息推送给前端）
        if role == "assistant" and content.strip():
            notify_new_message_sync(msg_id, "assistant", content, source="electron")

            # IM Gateway 流式推送已通过 reply chunk 路径（行 1849）发送完整内容，
            # 此处不再发空信号（避免冗余 CardKit API 调用）

        return msg_id

    def _sync_add_message(self, role: str, content: str,
                           tool_calls: list | None = None, tool_call_id: str = "") -> str | None:
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
                tool_calls=tool_calls, tool_call_id=tool_call_id
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
