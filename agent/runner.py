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
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from agent.subagent_registry import SubagentRegistry

# kebab-case 校验正则（小写字母/数字/连字符，且不以连字符开头/结尾）
# runner.py 顶部已有 `import re`，直接复用
_KEBAB_CASE_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# 主 Agent 专用工具集合（子 Agent 不可见）
# check_subagent_progress 是主 Agent 查子 Agent 进度的工具，子 Agent 不该有
MAIN_AGENT_ONLY_TOOLS = {"check_subagent_progress"}


# --- Stop flag mechanism ---
_stop_requested = threading.Event()


def request_stop():
    """Set the stop flag — Agent loops will check and exit."""
    _stop_requested.set()


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
    """给所有在跑的子 Agent 推 /stop（双击停止按钮触发）。

    遍历 SubagentRegistry，给每个子 Agent：
    1. 调 cancel_pending_ask 解除 ask_main_agent 阻塞（避免死锁）
    2. 推 /stop 到 supplement queue（让子 Agent 下一轮 drain 走终止总结流程）

    主 Agent 不受影响（主 Agent 用 _stop_requested 信号灯单独控制）。
    """
    from agent.ask_main_agent import get_pending_ask_registry
    pending_ask = get_pending_ask_registry()

    for instance in SubagentRegistry.list_running():
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
        except Exception as e:
            logger.error(f"给子 Agent {instance.unique_name} 推 /stop 失败：{e}")


def cleanup_suspended_sync_subagents():
    """主 Agent 工具循环退出时清理所有挂起的同步子 Agent session。

    场景：主 Agent 调用 chat-with-xxx 后，LLM 不再调用第二次 chat-with-xxx
    而是直接回应用户，导致同步子 Agent session 残留在 waiting_for_answer 状态。
    主 Agent 工具循环 finally 块调用此函数清理。
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
    cfg["provider"] = config.get("provider", "")
    cfg["litellm_kwargs"] = config.get("litellm_kwargs", {})

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

        # 动态前缀段：Current Time + disk_desc（启动时固定，不每轮更新）
        now = datetime.now()
        dynamic_prefix = f"\n\nCurrent Time: {now.strftime('%Y-%m-%d %H:%M:%S')}"
        disk_desc = self._build_disk_description()
        if disk_desc:
            dynamic_prefix += disk_desc
        self.dynamic_system_prefix = dynamic_prefix

        # 向后兼容：base_system_prompt = 静态段 + 动态前缀段（不含 injection，不含 memory 段）
        self.base_system_prompt = self.static_system_prompt + self.dynamic_system_prefix

        self._current_channel_id = ""
        # 首轮 resources 注入（拖入文件模式要求），_on_before_llm turn==1 时合并进 injection 后清空
        self._first_turn_extra_injection: str = ""

        # Brain context injector chain (lazy-cached, created once per runner)
        self._brain_adapter = None      # LightRAGAdapter
        self._brain_ingester = None     # LightRAGIngester
        self._brain_region_mgr = None   # RegionManager
        self._brain_injector = None     # BrainContextInjector
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

        策略：最近2条消息，≤80字符全量放入，>80字符从80位置往后找自然断点
        （优先句末标点→段落换行→逗号），最大200字符。assistant 附带最多5个工具名。
        """
        context_parts = []
        recent = messages[-2:] if len(messages) > 2 else messages

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

        # 动态段 = memory_section + Current Time + disk_desc + injection
        dynamic_text = ""
        if memory_section:
            dynamic_text += "\n\n" + memory_section
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

    def _on_turn_end(self, messages: list, tools_schema: list, turn: int) -> list:
        """每轮循环结束后的清理工作（动态注入已移到 _on_before_llm）。

        保留：
        - 脑区衰减 decay_all：每轮降低脑区激活级别

        已移除（移到 _on_before_llm）：
        - _inject_dynamic_resources + _assemble_system_message
          原因：原在 LLM 调用后注入，注入的 system message 下一轮才被读到，滞后一轮
        """
        # Decay brain region activation levels
        try:
            from agent.brain_tools import get_activation_mgr
            mgr = get_activation_mgr()
            if mgr is not None:
                mgr.decay_all()
        except Exception as e:
            logger.debug(f"Brain region decay failed: {e}")

        # No schema refresh — tools_schema stays base + disk
        return tools_schema

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
            Sub-agent name passed to call_subagent (e.g. "entity-extractor").
        cursor_path : Path
            JSON file that persists the cursor.
        cursor_field : str
            Key name inside the cursor JSON (e.g. "last_entity_extract_id").
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
            (e.g. "last_entity_extract_at").

        Returns
        -------
        (result_text, new_cursor_id) : tuple[str, str]
            result_text is the raw sub-agent output (empty on failure).
            new_cursor_id is the validated cursor after the step.
        """
        import concurrent.futures as _cf

        from niu_api.compat import (
            _extract_overflow_info,
            _is_subagent_overflow,
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

        # --- cursor advance: overflow→don't move; else parse processed_up_to=N + lookup idx_to_id, fallback fallback_ids[-1] ---
        new_cursor_id = last_cursor_id
        if _is_subagent_overflow(result):
            overflow_info = _extract_overflow_info(result)
            logger.warning(f"[{step_name}] overflow: {overflow_info.get('turns_completed', 0)} turns")
            # overflow 时游标不动
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

    def _on_context_high_usage(self, messages, tokens_used, tokens_limit):
        """主 Agent 上下文超阈值回调 — 执行完整 force 压缩流程

        回调完成后原地修改 messages 列表（从 DB 重新加载压缩后的消息）。
        agent_loop 不需要知道 DB、不需要导入 niu_api 的任何东西。

        实现参考：niu_api/compat.py _tidy_context_impl(mode="force") L1429-1907
        关键差异：compat.py 是 async，这里是同步线程中运行，
        子 Agent 调用用 concurrent.futures.ThreadPoolExecutor，无总超时限制。
        """
        from pathlib import Path as _Path

        from niu_api.compat import (
            _build_compress_history,
            _build_force_prompt,
            _build_incremental_msg_text,
            _build_journal_task,
            _build_plain_history,
            _is_subagent_overflow,      # 新增：检测 overflow
            _parse_idx_list,
            _split_dream_first_batch,   # 新增：dream-evolver 第一批拆分
            _strip_analysis,
            _write_cursor_with_lock,
        )

        from agent.subagent import (
            _read_compress_target_tokens,
            _read_context_window_tokens,
            _read_max_output_tokens,
            _read_protect_recent_count,
            call_subagent_with_auto_answer,
        )

        logger.info(f"[Runner] Context high usage: {tokens_used}/{tokens_limit} tokens "
                     f"({tokens_used/tokens_limit:.1%})")

        # 广播压缩状态 started 事件（前端圆环动画启动，模式1 auto）
        try:
            from niu_api.chat import notify_compact_status_sync
            notify_compact_status_sync("started", mode="auto")
        except Exception:
            pass

        try:
            # === 读取游标 ===
            niu_dir = _Path.home() / ".niu"
            entity_cursor_path = niu_dir / "last_entity_extract.json"
            dream_cursor_path = niu_dir / "last_dream_evolve.json"
            compress_cursor_path = niu_dir / "last_compress.json"
            journal_cursor_path = niu_dir / "last_journal.json"

            last_entity_extract_id = self._read_cursor(entity_cursor_path, "last_entity_extract_id")
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

            # === 步骤 1/4: entity-extractor（全量，cursor 传空 = 全量）===
            logger.info("[Runner] Force: starting entity-extractor (full processing)")
            new_entity_id = last_entity_extract_id
            # 计算 PROTECTED 消息 ID 集合（最近 N 条 user/assistant，与 context-manager 对齐）
            # 用于 entity force 方案 A：排除最近 PROTECTED 条防止 overflow 死循环（详见 Architecture §6）
            _force_protect_recent_count = _read_protect_recent_count()
            _force_protected_ids: set[str] = set()
            if _force_protect_recent_count > 0 and db_messages:
                from niu_api.compat import _find_protected_range
                _protect_start = _find_protected_range(db_messages, _force_protect_recent_count)
                _force_protected_ids = {getattr(db_messages[i], "id", "") or "" for i in range(_protect_start, len(db_messages))}

            if is_stop_requested():
                logger.warning("[Runner] Stop requested, aborting force compress")
                return

            entity_force_msg_ids = []
            _ = _build_incremental_msg_text(
                db_messages, "", entity_force_msg_ids, msg_tokens
            )
            if entity_force_msg_ids:
                entity_force_prompt = """以上是最近的对话消息（以 history 形式逐条传入，每条 content 前缀 [N] 极简编号，1-based）。请从中提取有价值的内容，形成精炼文档提交给 LightRAG 入库。

注意：对话历史中包含工具调用结果（role=tool），这些是程序化操作的结果。照片入库、人物命名等操作已经自动完成了知识图谱写入，不要重复创建这些实体。如果需要关联已有实体，请使用入库后的实体名称。

处理完成后，在最终回复中包含 `@end`，最后一行输出 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果最后一段不是完整的对话单元（如 assistant 回复未完成、tool 调用缺少对应结果），请将 `processed_up_to` 设为你最后完整处理到的那条消息的编号，不要设到不完整的位置。如果未输出该行，程序会回退到区间末尾作为游标（兜底）。"""
                # 构造全量 history + idx_to_id 映射（force 模式 cursor 为空 = 全量）
                # 方案 A：排除 PROTECTED 消息（最近 N 条 user/assistant）防止 overflow 死循环（详见 Architecture §6）
                entity_force_msgs_filtered = [m for m in db_messages if (getattr(m, "id", "") or "") not in _force_protected_ids]
                entity_force_history, entity_force_idx_to_id = _build_plain_history(entity_force_msgs_filtered)
                # 同步过滤 entity_force_msg_ids（游标推进兜底用，与 history 保持一致）
                entity_force_msg_ids = [getattr(m, "id", "") or "" for m in entity_force_msgs_filtered]

                _, new_entity_id = self._run_subagent_step(
                    "entity-extractor", entity_cursor_path, "last_entity_extract_id",
                    entity_force_prompt, llm_config, last_entity_extract_id,
                    entity_force_msg_ids, "last_entity_extract_at",
                    history=entity_force_history, context_fifo_threshold=-1,  # FIFO 保底
                    idx_to_id=entity_force_idx_to_id,
                )

                if is_stop_requested():
                    logger.warning("[Runner] Stop requested, aborting force compress")
                    return
            else:
                logger.info("[Runner] Force: entity-extractor skipped, no messages")

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

消息以 history 形式逐条传入，每条 content 前缀 [N] 极简编号（1-based）。处理完成后，在最终回复中包含 `@end`，最后一行输出 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果最后一段不是完整的对话单元（如 assistant 回复未完成、tool 调用缺少对应结果），请将 `processed_up_to` 设为你最后完整处理到的那条消息的编号，不要设到不完整的位置。如果未输出该行，程序会回退到区间末尾作为游标（兜底）。"""

                # 计算第一批：增量消息 token 量过大时在 user 消息边界处拆分
                _dream_context_window = _read_context_window_tokens()
                _first_batch_ids = _split_dream_first_batch(
                    db_messages, dream_force_msg_ids, msg_tokens, _dream_context_window
                )

                if _first_batch_ids is None:
                    # 不拆分，正常单次调用（保持原有逻辑）
                    _id_set = set(dream_force_msg_ids)
                    dream_force_incremental_msgs = [m for m in db_messages if (getattr(m, "id", "") or "") in _id_set]
                    dream_force_history, dream_force_idx_to_id = _build_plain_history(dream_force_incremental_msgs)

                    _, new_dream_id = self._run_subagent_step(
                        "dream-evolver", dream_cursor_path, "last_dream_evolve_id",
                        dream_force_prompt, llm_config, last_dream_evolve_id,
                        dream_force_msg_ids, "last_evolve_at",
                        history=dream_force_history, context_fifo_threshold=-1,
                        idx_to_id=dream_force_idx_to_id,
                    )

                    if is_stop_requested():
                        logger.warning("[Runner] Stop requested, aborting force compress")
                        return
                else:
                    # ===== 第一批 =====
                    logger.info(f"[Runner] Force: dream-evolver splitting into 2 batches "
                                f"(incremental tokens exceed 50% of {_dream_context_window})")
                    _first_id_set = set(_first_batch_ids)
                    _first_msgs = [m for m in db_messages if (getattr(m, "id", "") or "") in _first_id_set]
                    _first_history, _first_idx_to_id = _build_plain_history(_first_msgs)

                    logger.info(f"[Runner] Force: dream-evolver batch 1/2: {len(_first_batch_ids)} messages")
                    _batch_result, new_dream_id = self._run_subagent_step(
                        "dream-evolver", dream_cursor_path, "last_dream_evolve_id",
                        dream_force_prompt, llm_config, last_dream_evolve_id,
                        _first_batch_ids, "last_evolve_at",
                        history=_first_history, context_fifo_threshold=-1,
                        idx_to_id=_first_idx_to_id,
                    )

                    if is_stop_requested():
                        logger.warning("[Runner] Stop requested, aborting force compress")
                        return

                    # overflow 检测：跳过第二批（游标不动，_run_subagent_step 已处理）
                    if not _is_subagent_overflow(_batch_result):
                        # Clamp: ensure cursor is at least at first_batch end
                        # to prevent batch2 from re-processing batch1 messages
                        if new_dream_id in _first_batch_ids:
                            _first_batch_last = _first_batch_ids[-1]
                            if _first_batch_ids.index(new_dream_id) < len(_first_batch_ids) - 1:
                                new_dream_id = _first_batch_last
                                logger.info(f"[Runner] Force: Dream cursor clamped to first batch end: {new_dream_id}")
                                # _run_subagent_step 已写入未 clamp 的游标，需重新写 clamp 后的值
                                _write_cursor_with_lock(dream_cursor_path, {
                                    "last_dream_evolve_id": new_dream_id,
                                    "last_evolve_at": datetime.now().isoformat(),
                                })
                        last_dream_evolve_id = new_dream_id  # 更新基准（clamp 后），使第二批回退到此游标

                        # ===== 第二批：从 new_dream_id 之后到末尾，动态计算 =====
                        _second_batch_ids = []
                        _found_cursor = False
                        for mid in dream_force_msg_ids:
                            if mid == new_dream_id:
                                _found_cursor = True
                                continue
                            if _found_cursor:
                                _second_batch_ids.append(mid)

                        if _second_batch_ids:
                            _second_id_set = set(_second_batch_ids)
                            _second_msgs = [m for m in db_messages if (getattr(m, "id", "") or "") in _second_id_set]
                            _second_history, _second_idx_to_id = _build_plain_history(_second_msgs)

                            logger.info(f"[Runner] Force: dream-evolver batch 2/2: {len(_second_batch_ids)} messages")
                            _batch_result, new_dream_id = self._run_subagent_step(
                                "dream-evolver", dream_cursor_path, "last_dream_evolve_id",
                                dream_force_prompt, llm_config, last_dream_evolve_id,
                                _second_batch_ids, "last_evolve_at",
                                history=_second_history, context_fifo_threshold=-1,
                                idx_to_id=_second_idx_to_id,
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

            protect_recent_count = _read_protect_recent_count()

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
            # 当 dream 不在 force history 里时，用 len(_force_msg_ids)（越界值，由 _build_force_prompt 内部判断"无 dream 约束"）
            if not new_dream_id:
                _dream_idx_in_force = 0
            else:
                _dream_idx_in_force = _f_id_to_idx.get(new_dream_id, len(_force_msg_ids))

            # 复用上文的 target_tokens（不重复读配置）
            prompt = _build_force_prompt(
                display_tokens, target_tokens, usage_percent,
                _force_history, last_compress_id, _dream_idx_in_force,
            )

            # llm_config 动态注入 max_tokens（通过 litellm_kwargs）
            # _read_max_output_tokens 动态算：contextWindowSize × 0.16，封顶 65536
            llm_config_with_max = dict(llm_config)
            llm_config_with_max["litellm_kwargs"] = {
                **llm_config.get("litellm_kwargs", {}),
                "max_tokens": _read_max_output_tokens(),
            }

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

            if is_stop_requested():
                logger.warning("[Runner] Stop requested, aborting force compress")
                return
            # 截断时触发内联应急清空（保留最近完整用户会话段落，上面全删，最旧改"压缩失败"摘要）
            # 同步实现：用 self._sync_delete_messages / self._sync_update_message，不调 async _emergency_clear
            if result == "COMPACT_TRUNCATED":
                logger.warning("[Compact] runner.py force output truncated, triggering emergency clear")
                from niu_api.compat import _find_protected_range
                _force_id_set = set(_force_msg_ids)
                _force_msgs = [m for m in db_messages if (getattr(m, "id", "") or "") in _force_id_set]
                _protect_n = _read_protect_recent_count()
                _protect_start = _find_protected_range(_force_msgs, _protect_n)
                if _protect_start <= 0 or _protect_start >= len(_force_msg_ids):
                    logger.warning(f"[Compact] Runner history all protected ({len(_force_msg_ids)} msgs), no clear needed")
                    return {"status": "skipped", "mode": "force", "reason": "truncated, no clear needed (all protected)"}

                delete_ids = _force_msg_ids[:_protect_start]
                oldest_kept_id = _force_msg_ids[_protect_start]

                # _sync_delete_messages 只接收 msg_ids（不接收 session_id）
                self._sync_delete_messages(delete_ids)

                # 最旧保留条改为"压缩失败"摘要
                self._sync_update_message(
                    message_id=oldest_kept_id,
                    content="[压缩失败，历史信息丢失] 上下文压缩时 LLM 输出截断，此条之上的历史已删除。可通过 journal.md 和知识图谱回溯。",
                )

                logger.warning(f"[Compact] Runner emergency cleared: deleted {len(delete_ids)} msgs, kept recent {len(_force_msg_ids) - _protect_start}")
                return {"status": "skipped", "mode": "force", "reason": "truncated, emergency cleared"}

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
                cursor_ids_set = {cid for cid in [new_compress_id, new_entity_id, new_dream_id] if cid}
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
                lines.append(f"   路径: ~/.niu/skills/{display_name}.md")
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

    def _traverse_from_hits(self, hits: list[str]) -> dict[str, dict]:
        """从 hit entities 沿知识边1跳图遍历，收集邻居实体。

        Args:
            hits: 向量检索命中的 entity_name 列表

        Returns:
            {entity_name_lower: {description, entity_type, source, ...}}
            source 为 "hit" 或 "neighbor:{关系关键词}"
        """
        from niu_api.internal.lightrag_manager import get_lightrag, graph_read_lock

        rag = get_lightrag()
        if rag is None:
            return {}

        graph_obj = getattr(rag, "chunk_entity_relation_graph", None)
        if graph_obj is None:
            return {}

        nx_graph = graph_obj._graph if hasattr(graph_obj, "_graph") else graph_obj
        if nx_graph is None or nx_graph.number_of_nodes() == 0:
            return {}

        result: dict[str, dict] = {}

        with graph_read_lock():
            snapshot = nx_graph.copy()

        for hit in hits:
            hit_lower = hit.lower() if isinstance(hit, str) else hit
            if hit_lower not in snapshot:
                continue

            node = snapshot.nodes.get(hit_lower, {})
            if hit_lower not in result:
                result[hit_lower] = {**node, "entity_name": hit_lower, "source": "hit"}

            for neighbor in snapshot.neighbors(hit_lower):
                if neighbor.endswith("脑区"):
                    continue

                edge_data = snapshot.get_edge_data(hit_lower, neighbor)
                if not edge_data:
                    continue

                if isinstance(list(edge_data.values())[0], dict):
                    edge_list = list(edge_data.values())
                else:
                    edge_list = [edge_data]

                for ed in edge_list:
                    kw = ed.get("keywords", "") if isinstance(ed, dict) else ""
                    if kw and kw != "包含" and not kw.startswith("_session"):
                        node2 = snapshot.nodes.get(neighbor, {})
                        neighbor_lower = neighbor.lower()
                        if neighbor_lower not in result:
                            result[neighbor_lower] = {
                                **node2,
                                "entity_name": neighbor_lower,
                                "source": f"neighbor:{kw}",
                            }

        return result

    def _inject_dynamic_resources(self, context: str) -> tuple[str, dict[str, int]]:
        """动态注入相关资源 — 向量检索 + 图遍历 + Ebbinghaus 衰减池。

        流程:
        1. 脑区激活 (不变)
        2. 全局向量检索
        3. 衰减池维护 (先衰减旧实体，再注入新命中)
        4. 衰减池注入：全局检索命中 (score=distance)
        5. 图遍历 (从 hit entities 沿知识边1跳) → 衰减池注入 (score=hit×0.8)
        6. 格式化注入 (脑区状态图 + skill + knowledge + 活跃脑区知识 + 习惯)
        """
        # 0. Brain region activation (不变)
        _brain_injector = None
        try:
            _brain_injector = self._get_brain_injector()
            if _brain_injector is not None:
                _brain_injector.activate_for_query(context)
        except Exception as e:
            logger.warning(f"Brain activation failed: {e}")

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
            skill_results = adapter.search_by_file_path(
                context, file_path_contains="skill_sync", top_k=10, keywords=[context],
            )
            lightrag_results["skill"] = skill_results
        except Exception as e:
            logger.warning(f"LightRAG skill retrieval failed: {e}")
        # 1b. Knowledge 全量检索
        try:
            knowledge_results = adapter.search_multi_lightrag(
                context, mode="local", top_k=10, keywords=[context],
            )
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

        # 2. 衰减池维护（先衰减旧实体，再注入新命中）
        self._decay_pool.decay()

        # 3. 衰减池注入：全局检索命中的实体
        all_hits = []  # 用于图遍历
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
                all_hits.append(name)

        # 4. 图遍历：从 hit entities 沿知识边1跳
        traversed: dict[str, dict] = {}
        try:
            if all_hits:
                traversed = self._traverse_from_hits(all_hits)
        except Exception as e:
            logger.warning(f"Graph traversal failed: {e}")

        # 图遍历结果注入衰减池
        # 建立 entity_name -> distance 映射（从全局检索结果）
        hit_distance_map: dict[str, float] = {}
        for category, entities in lightrag_results.items():
            for i, entity in enumerate(entities):
                name = entity.get("entity_name", "")
                if name:
                    distance = entity.get("distance")
                    if distance is None:
                        distance = 1.0 - (i / max(len(entities), 1)) * 0.5
                    hit_distance_map[name.lower()] = distance

        for entity_name, node_data in traversed.items():
            source = node_data.get("source", "")
            if source == "hit":
                # hit 实体已在 step 3 注入，跳过
                continue
            # 如果实体已通过向量检索注入（source="vector"），不覆盖
            existing_entry = self._decay_pool.get_entry(entity_name)
            if existing_entry is not None and existing_entry.source == "vector":
                continue
            # 黑名单预过滤（与向量路径一致）
            entity_type = (node_data.get("entity_type") or "").lower()
            if entity_type in self._INJECT_ENTITY_TYPE_BLACKLIST:
                continue
            if entity_name in self._INJECT_ENTITY_NAME_BLACKLIST:
                continue
            # 邻居实体：用平均 hit 分数 × 0.8
            neighbor_score = 0.8 * (
                sum(hit_distance_map.values()) / max(len(hit_distance_map), 1)
                if hit_distance_map else 0.5
            )

            from niu_api.internal.lightrag_adapter import LightRAGAdapter
            category = LightRAGAdapter._ENTITY_TYPE_TO_CATEGORY.get(entity_type, "knowledge")

            # 真实 skill 检查：file_path 含 "skill_sync" 段（与向量检索路径一致）
            if category == "skill":
                entity_file_path = node_data.get("file_path", "")
                is_real_skill = any(
                    seg.strip() == "skill_sync"
                    for seg in entity_file_path.split("<SEP>")
                )
                if not is_real_skill:
                    skill_path = Path.home() / ".niu" / "skills" / f"{entity_name}.md"
                    category = "knowledge" if not skill_path.exists() else "skill"

            self._decay_pool.inject(
                entity_name=entity_name,
                entity_dict=node_data,
                category=category,
                source="graph_traversal",
                vector_score=neighbor_score,
            )

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

        # 活跃脑区知识 (从衰减池取 source=graph_traversal)
        region_entries = self._decay_pool.get_top_by_source("graph_traversal", 5)
        if region_entries:
            region_lines: list[str] = []
            for entry in region_entries:
                name = entry.entity_dict.get("entity_name", entry.entity_name)
                if name in seen_names:
                    continue
                # 黑名单过滤（与 _format_lightrag_entities_for_prompt 一致，case-sensitive）
                entity_type = (entry.entity_dict.get("entity_type") or "").lower()
                if entity_type in self._INJECT_ENTITY_TYPE_BLACKLIST:
                    continue
                if name in self._INJECT_ENTITY_NAME_BLACKLIST:
                    continue
                seen_names.add(name)
                desc = (entry.entity_dict.get("description") or "").replace("<SEP>", "\n")
                source = entry.entity_dict.get("source", "")
                if source == "hit":
                    source_label = "(hit)"
                elif source.startswith("neighbor:"):
                    relation = source.split(":", 1)[1]
                    source_label = f"(邻居: {relation})"
                else:
                    source_label = ""
                desc_line = f"   {desc[:500]}" if desc else ""
                region_lines.append(f"{len(region_lines)+1}. **{name}** {source_label}\n{desc_line}")
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
        self._current_channel_id = channel_id
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
        tools_schema = self.base_tools_schema.copy()

        # Inject static-visibility MCP tools (e.g. brain_region/*)
        # These are always visible to the LLM, unlike hidden/dynamic tools
        # which are accessed via disk().
        try:
            from agent.tool_registry import get_registry
            registry = get_registry()
            for tool_name in registry.get_static_tools():
                schema = registry._schemas.get(tool_name)
                if schema:
                    tools_schema.append({
                        "type": "function",
                        "function": {
                            "name": schema["name"],
                            "description": schema.get("description", ""),
                            "parameters": schema.get("input_schema", {"type": "object", "properties": {}}),
                        }
                    })
        except Exception as e:
            logger.debug(f"Static MCP tools injection skipped: {e}")

        # Add disk tool
        disk_schema = self.disk_engine.get_schema()
        tools_schema.append(disk_schema)

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
                                try:
                                    from niu_api.channel.gateway import get_im_gateway
                                    _gw = get_im_gateway()
                                    if _gw and _gw.is_connected and chunk.content and self._current_channel_id:
                                        _gw.notify_stream(chunk.content, channel_id=self._current_channel_id)
                                except Exception:
                                    pass
                        elif chunk.type == "persist":
                            # V4: 逐条持久化消息到 DB + 通知 SSE
                            try:
                                msg_dict = json.loads(chunk.content)
                                msg_id = self._persist_one_msg(msg_dict)
                                if msg_id is not None:
                                    msg_dict["_persisted_id"] = msg_id  # 记录写入后的消息ID
                                    persisted_msgs.append(msg_dict)
                            except Exception as e:
                                logger.warning(f"[Runner] Failed to persist msg: {e}")
                        elif chunk.type == "system":
                            # V4: chat_busy/chat_idle 状态机事件，通过SSE推送给前端
                            if chunk.content in ("chat_busy", "chat_idle"):
                                from niu_api.chat import notify_new_message_sync
                                notify_new_message_sync("", chunk.content, "", source="electron")
                                if chunk.content == "chat_idle":
                                    chat_idle_pushed = True
                        # type="tool_marker" 不进入 SSE 和 full_resp
                    else:
                        # 向后兼容：普通 str
                        full_resp += chunk
                        if chunk:
                            yield chunk
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
                if _gw and _gw.is_connected and self._current_channel_id:
                    _gw.notify_stream("", channel_id=self._current_channel_id, is_final=True)
            except Exception:
                pass
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
