"""
Memory Server - 智能记忆管理 MCP 服务器

提供用户长期记忆和工作便签管理（permanent array）。
"""

from mcp.server import Server
from mcp.types import Tool, TextContent
from loguru import logger
import json
import asyncio
from datetime import datetime

# 创建 MCP 服务器
server = Server("memory-server")

# ============================================================================
# Tool Schemas
# ============================================================================

TOOL_SCHEMAS = {
    "user_memory_remember": {
        "name": "user_memory_remember",
        "description": "添加用户长期记忆或工作便签。type='task'为当前工作便签(最多1条,新任务自动覆盖旧任务,用于保存复杂任务的进度/关键参数/下一步); type='memory'为用户长期记忆(最多9条,仅在用户明确要求记住时添加)。记忆永久驻留系统提示词,异常退出后下次继续。",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "记忆内容（≤200 token，约300中文字符）",
                },
                "type": {
                    "type": "string",
                    "enum": ["task", "memory"],
                    "description": "task=当前工作便签(1条,自动覆盖), memory=用户长期记忆(9条,需手动删)",
                    "default": "memory",
                },
            },
            "required": ["content"],
        },
    },
    "user_memory_forget": {
        "name": "user_memory_forget",
        "description": "删除用户长期记忆或工作便签。按序号(index)或关键词(keyword)匹配删除。task和memory类型均可删除。",
        "input_schema": {
            "type": "object",
            "properties": {
                "index": {
                    "type": "integer",
                    "description": "记忆序号（1-10），优先于 keyword",
                },
                "keyword": {
                    "type": "string",
                    "description": "不区分大小写的子串匹配",
                },
            },
        },
    },
    "user_memory_list": {
        "name": "user_memory_list",
        "description": "查看当前所有用户长期记忆",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    "conversation_park": {
        "name": "conversation_park",
        "description": "暂存告一段落的话题(回头再处理)。用户明确说「这事先告一段落/回头再说/先放着」时调用，把该话题存入暂存列表。summary=一句话说清是什么事；detail=3-5句关键上下文(讨论什么/进展到哪/卡点/下一步)。不要主动替用户决定暂存；闲聊随口「回头再聊」不要调用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "一句话摘要（≤50 token）",
                },
                "detail": {
                    "type": "string",
                    "description": "3-5句关键上下文：讨论什么/进展到哪/卡点/下一步（≤200 token）",
                },
            },
            "required": ["summary", "detail"],
        },
    },
    "conversation_recall": {
        "name": "conversation_recall",
        "description": "召回暂存的话题(召回即从列表移除)。用户提起之前暂存的话题时调用，参数为提醒行里的序号(①=最新)。无法确定指哪一项时按 summary 语义匹配，仍不确定先向用户列候选确认，禁止猜序号。返回 summary+detail+锚点；锚点有效时可用 get_messages/read_history_block 回放原文。召错项时立即用返回的 detail 重新 park。",
        "input_schema": {
            "type": "object",
            "properties": {
                "index": {
                    "type": "integer",
                    "description": "暂存序号（1 起始，①=最新）",
                },
            },
            "required": ["index"],
        },
    },
}


def get_tool_schemas() -> list[dict]:
    """返回所有工具的 schema 列表（用于 MCP Loader 注册）"""
    return list(TOOL_SCHEMAS.values())


# ============================================================================
# Tool definitions
# ============================================================================


def get_tool_definitions() -> list[Tool]:
    """返回所有工具定义"""
    return [
        Tool(
            name="user_memory_remember",
            description="添加用户长期记忆或工作便签。type='task'为当前工作便签(最多1条,新任务自动覆盖旧任务,用于保存复杂任务的进度/关键参数/下一步); type='memory'为用户长期记忆(最多9条,仅在用户明确要求记住时添加)。记忆永久驻留系统提示词,异常退出后下次继续。",
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "记忆内容（≤200 token，约300中文字符）"},
                    "type": {
                        "type": "string",
                        "enum": ["task", "memory"],
                        "description": "task=当前工作便签(1条), memory=用户长期记忆(9条)",
                        "default": "memory",
                    },
                },
                "required": ["content"],
            },
        ),
        Tool(
            name="user_memory_forget",
            description="删除用户长期记忆或工作便签。按序号(index)或关键词(keyword)匹配删除。task和memory类型均可删除。",
            inputSchema={
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "记忆序号（1-10），优先于 keyword"},
                    "keyword": {"type": "string", "description": "不区分大小写的子串匹配"},
                },
            },
        ),
        Tool(
            name="user_memory_list",
            description="查看当前所有用户长期记忆",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="conversation_park",
            description="暂存告一段落的话题(回头再处理)。用户明确说「这事先告一段落/回头再说/先放着」时调用，把该话题存入暂存列表。summary=一句话说清是什么事；detail=3-5句关键上下文(讨论什么/进展到哪/卡点/下一步)。不要主动替用户决定暂存；闲聊随口「回头再聊」不要调用。",
            inputSchema={
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "一句话摘要（≤50 token）"},
                    "detail": {"type": "string", "description": "3-5句关键上下文：讨论什么/进展到哪/卡点/下一步（≤200 token）"},
                },
                "required": ["summary", "detail"],
            },
        ),
        Tool(
            name="conversation_recall",
            description="召回暂存的话题(召回即从列表移除)。用户提起之前暂存的话题时调用，参数为提醒行里的序号(①=最新)。无法确定指哪一项时按 summary 语义匹配，仍不确定先向用户列候选确认，禁止猜序号。返回 summary+detail+锚点；锚点有效时可用 get_messages/read_history_block 回放原文。召错项时立即用返回的 detail 重新 park。",
            inputSchema={
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "暂存序号（1 起始，①=最新）"},
                },
                "required": ["index"],
            },
        ),
    ]


# ============================================================================
# User memory tools (memory.json permanent array)
# ============================================================================

MEMORY_JSON_PATH = None  # Set at first call
import threading
_memory_file_lock = threading.Lock()

MAX_PERMANENT_ITEMS = 10
MAX_TASK_ITEMS = 1
MAX_MEMORY_ITEMS = 9  # MAX_TASK_ITEMS + MAX_MEMORY_ITEMS = MAX_PERMANENT_ITEMS
MAX_TOKEN_PER_ITEM = 200  # ~300 Chinese chars

MAX_PARKED_ITEMS = 10
MAX_PARKED_SUMMARY_TOKENS = 50
MAX_PARKED_DETAIL_TOKENS = 200


def _count_tokens(text: str) -> int:
    """Count tokens using TokenCalculator.
    Falls back to conservative CJK-aware char-based estimate if unavailable.
    """
    try:
        # Ensure agent package is importable in stdio mode (project root may not be in sys.path)
        import sys as _sys
        from pathlib import Path as _Path
        _project_root = str(_Path(__file__).resolve().parent.parent.parent.parent.parent)
        if _project_root not in _sys.path:
            _sys.path.insert(0, _project_root)
        from agent.token_calculator import TokenCalculator
        return TokenCalculator.get().count_text(text)
    except Exception:
        # Fallback: CJK ~1.5 tokens, ASCII ~0.25 tokens (conservative)
        cjk = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
        return max(1, int(cjk * 1.5 + (len(text) - cjk) * 0.25))


def _get_memory_json_path():
    """Get path to ~/.niu/memory.json"""
    global MEMORY_JSON_PATH
    if MEMORY_JSON_PATH is None:
        from pathlib import Path
        MEMORY_JSON_PATH = Path.home() / ".niu" / "memory.json"
    return MEMORY_JSON_PATH


def _reset_memory_json_path():
    """Reset cached path (for testing)"""
    global MEMORY_JSON_PATH
    MEMORY_JSON_PATH = None


def _normalize_permanent(permanent: list) -> list:
    """Migrate old format (list[str]) to new format (list[dict with type field]).
    Old strings become {"type": "memory", "content": str}.
    """
    normalized = []
    for item in permanent:
        if isinstance(item, str):
            normalized.append({"type": "memory", "content": item})
        elif isinstance(item, dict) and "content" in item:
            normalized.append({
                "type": item.get("type", "memory"),
                "content": item["content"],
            })
        # skip invalid items
    return normalized


def _read_memory_json() -> dict:
    """Read memory.json, return dict with at least {permanent: []}.

    On parse error, returns the raw file content preserved under a _raw_fallback
    key so _write_permanent can recover it. Only returns {"permanent": []} if
    the file doesn't exist at all.

    This is a pure read — no write side effects. Truncation is handled
    by mutation handlers (remember/forget) if needed.
    """
    path = _get_memory_json_path()
    if not path.exists():
        return {"permanent": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            logger.warning(f"memory.json root is not a dict, resetting permanent only")
            return {"permanent": [], "_raw_fallback": True}
        if "permanent" not in data:
            data["permanent"] = []
        if not isinstance(data["permanent"], list):
            logger.warning(f"memory.json permanent is not a list, treating as empty")
            data["permanent"] = []
        # Migrate old string format to new dict format
        data["permanent"] = _normalize_permanent(data["permanent"])
        # Truncate in-memory only (no write side effect), but flag for handlers
        if len(data["permanent"]) > MAX_PERMANENT_ITEMS:
            logger.warning(f"memory.json has {len(data['permanent'])} permanent items (max {MAX_PERMANENT_ITEMS}), truncating in-memory")
            data["permanent"] = data["permanent"][:MAX_PERMANENT_ITEMS]
            data["_truncated"] = True
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to read memory.json: {e}, preserving file")
        return {"permanent": [], "_raw_fallback": True}


def _write_permanent_only(permanent: list):
    """Read-modify-write: update only the permanent field, preserve all others.
    Thread-safe via module-level lock.
    """
    import os
    import tempfile

    with _memory_file_lock:
        path = _get_memory_json_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        # Read existing file to preserve other fields (identity, workspace, user, etc.)
        existing = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(existing, dict):
                    existing = {}
            except (json.JSONDecodeError, OSError):
                existing = {}

        existing["permanent"] = permanent
        # 原子写：先写 tmp，再 replace（保证 reader 永远看到完整文件）
        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=path.name + ".",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


def _write_parked_only(parked: list):
    """Read-modify-write: update only the parked field, preserve all others.
    Thread-safe via module-level lock (镜像 _write_permanent_only 形态).
    """
    import os
    import tempfile

    with _memory_file_lock:
        path = _get_memory_json_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        # Read existing file to preserve other fields (identity, workspace, user, permanent, etc.)
        existing = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(existing, dict):
                    existing = {}
            except (json.JSONDecodeError, OSError):
                existing = {}

        existing["parked"] = parked
        # 原子写：先写 tmp，再 replace（保证 reader 永远看到完整文件）
        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=path.name + ".",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


def user_memory_remember_handler(content: str, type: str = "memory") -> dict:
    """添加用户长期记忆到 memory.json permanent 数组

    type="task": 当前工作便签（最多1条，新任务覆盖旧的）
    type="memory": 用户长期记忆（最多9条）
    """
    if type not in ("task", "memory"):
        return {"status": "error", "message": f"type 必须是 'task' 或 'memory'，收到 '{type}'"}

    # Reject empty or whitespace-only content
    if not content or not content.strip():
        return {"status": "error", "message": "记忆内容不能为空"}

    data = _read_memory_json()
    if data.get("_raw_fallback"):
        return {"status": "error", "message": "memory.json 文件损坏，请手动修复后重试"}
    if data.get("_truncated"):
        return {"status": "error", "message": f"memory.json 超过{MAX_PERMANENT_ITEMS}条限制，请先调用 user_memory_forget 删除多余记忆后再添加。"}
    permanent = data["permanent"]

    # Count by type
    task_count = sum(1 for item in permanent if item.get("type") == "task")
    memory_count = sum(1 for item in permanent if item.get("type") == "memory")

    # Dedup: reject case-insensitive duplicate content (strip whitespace, skip empty)
    content_stripped_lower = content.strip().lower()
    for i, existing in enumerate(permanent):
        existing_content = existing.get("content", "")
        if existing_content and existing_content.strip().lower() == content_stripped_lower:
            return {
                "status": "error",
                "message": f"该内容已存在(第{i+1}条)，无需重复添加。",
                "current_memories": permanent,
            }

    # Token count using litellm (tiktoken-based)
    estimated_tokens = _count_tokens(content)
    if estimated_tokens > MAX_TOKEN_PER_ITEM:
        return {
            "status": "error",
            "message": f"内容过长（约{int(estimated_tokens)} token，上限{MAX_TOKEN_PER_ITEM}），请精简后重试。",
        }

    # Check capacity by type
    if type == "task" and task_count >= MAX_TASK_ITEMS:
        # Remove ALL existing task items (handles manual edits with multiple tasks)
        permanent = [item for item in permanent if item.get("type") != "task"]
        permanent.insert(0, {"type": "task", "content": content})
        _write_permanent_only(permanent)
        return {
            "status": "success",
            "message": f"✅ 已更新工作便签（覆盖旧任务）",
            "current_memories": permanent,
        }
    if type == "memory" and memory_count >= MAX_MEMORY_ITEMS:
        return {
            "status": "error",
            "message": f"记忆已满({memory_count}/{MAX_MEMORY_ITEMS})，请先调用 user_memory_forget 删除旧记忆。",
            "current_memories": permanent,
        }

    permanent.append({"type": type, "content": content})
    _write_permanent_only(permanent)

    type_label = "工作便签" if type == "task" else "记忆"
    msg = f"✅ 已添加{type_label}({len(permanent)}/{MAX_PERMANENT_ITEMS})"

    return {
        "status": "success",
        "message": msg,
        "current_memories": permanent,
    }


def user_memory_forget_handler(index: int = None, keyword: str = None) -> dict:
    """删除用户长期记忆"""
    data = _read_memory_json()
    if data.get("_raw_fallback"):
        return {"status": "error", "message": "memory.json 文件损坏，请手动修复后重试"}
    if data.get("_truncated"):
        # Allow forget when truncated — deleting is the fix for over-limit
        pass
    permanent = data["permanent"]

    if not permanent:
        return {"status": "error", "message": "没有可删除的记忆"}

    if index is not None:
        # Index is 1-based
        if index < 1 or index > len(permanent):
            return {"status": "error", "message": f"序号超出范围(1-{len(permanent)})"}
        item = permanent[index - 1]
        if item.get("type") == "task":
            # Task slot: clear content instead of removing, to keep slot position stable
            old_content = item.get("content", "")
            permanent[index - 1] = {"type": "task", "content": ""}
            _write_permanent_only(permanent)
            msg = f"✅ 已清空工作便签(第{index}条): {old_content}"
        else:
            removed = permanent.pop(index - 1)
            _write_permanent_only(permanent)
            msg = f"✅ 已删除第{index}条记忆: {removed}"
        return {
            "status": "success",
            "message": msg,
            "current_memories": permanent,
        }

    if keyword:
        keyword_lower = keyword.lower()
        matches = [(i, item) for i, item in enumerate(permanent) if keyword_lower in item.get("content", "").lower()]
        if not matches:
            return {"status": "error", "message": f"未找到包含'{keyword}'的记忆", "current_memories": permanent}
        # Handle first match
        i, item = matches[0]
        if item.get("type") == "task":
            # Task slot: clear content instead of removing
            old_content = item.get("content", "")
            permanent[i] = {"type": "task", "content": ""}
            _write_permanent_only(permanent)
            msg = f"✅ 已清空工作便签: {old_content}"
        else:
            removed = permanent.pop(i)
            _write_permanent_only(permanent)
            msg = f"✅ 已删除匹配'{keyword}'的记忆: {removed}"
        if len(matches) > 1:
            msg += f"（注意：还有{len(matches)-1}条记忆也匹配该关键词）"
        return {
            "status": "success",
            "message": msg,
            "current_memories": permanent,
        }

    return {"status": "error", "message": "请提供 index 或 keyword 参数"}


def user_memory_list_handler() -> dict:
    """查看当前所有用户长期记忆和工作便签"""
    data = _read_memory_json()
    if data.get("_raw_fallback"):
        return {"status": "error", "message": "memory.json 文件损坏，请手动修复后重试"}
    permanent = data["permanent"]

    task_count = sum(1 for item in permanent if item.get("type") == "task")
    memory_count = sum(1 for item in permanent if item.get("type") == "memory")

    return {
        "status": "success",
        "count": len(permanent),
        "task_count": task_count,
        "memory_count": memory_count,
        "max_task": MAX_TASK_ITEMS,
        "max_memory": MAX_MEMORY_ITEMS,
        "memories": permanent,
    }


# ============================================================================
# Conversation park tools (memory.json parked array)
# 复制自 session-manager 的 _run_async/_get_store（两包间无交叉 import 先例，复制不引用）
# ============================================================================


def _get_store():
    """Get or create MessageStore instance (sync wrapper)."""
    from agent.session import get_message_store
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # Already in async context — create a new event loop in a thread
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, get_message_store()).result()
    else:
        return asyncio.run(get_message_store())


def _run_async(coro):
    """Run an async coroutine synchronously."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result()
    else:
        return asyncio.run(coro)


def _capture_anchor_msg_id() -> str:
    """捕获当前会话最新消息 id 作为 anchor（无消息时返回空串；失败降级不阻塞 park）"""
    try:
        store = _get_store()  # 内部 _run_async(get_message_store()) —— async 函数返回协程，必须 _run_async 包一层
        msgs = _run_async(store.get_messages(limit=1))  # list[Message] dataclass，取 .id 属性不可下标
        return msgs[0].id if msgs else ""
    except Exception as e:
        logger.warning(f"anchor 捕获失败: {e}")
        return ""


def conversation_park_handler(summary: str, detail: str) -> dict:
    """暂存告一段落的话题到 memory.json parked 数组（新项插头部，上限 10 条）"""
    if not summary or not summary.strip():
        return {"status": "error", "message": "summary 不能为空"}
    if not detail or not detail.strip():
        return {"status": "error", "message": "detail 不能为空"}
    # summary 单行约束：换行会破坏「常驻一行」与字节稳定语义
    summary = summary.replace("\n", " ").strip()

    data = _read_memory_json()
    if data.get("_raw_fallback"):
        return {"status": "error", "message": "memory.json 文件损坏，请手动修复后重试"}
    parked = data.get("parked") or []  # 旧 memory.json 无 parked 键兼容
    if not isinstance(parked, list):  # 非 list 守卫（对齐 permanent 的 not-a-list 守卫）
        logger.warning("memory.json parked is not a list, treating as empty")
        parked = []

    if _count_tokens(summary) > MAX_PARKED_SUMMARY_TOKENS:
        return {"status": "error", "message": f"summary 过长（约{_count_tokens(summary)} token，上限{MAX_PARKED_SUMMARY_TOKENS}），请精简后重试。"}
    if _count_tokens(detail) > MAX_PARKED_DETAIL_TOKENS:
        return {"status": "error", "message": f"detail 过长（约{_count_tokens(detail)} token，上限{MAX_PARKED_DETAIL_TOKENS}），请精简后重试。"}
    if len(parked) >= MAX_PARKED_ITEMS:
        return {"status": "error", "message": f"暂存列表已满（{MAX_PARKED_ITEMS} 条），请先召回或确认关闭某项后再暂存"}

    parked.insert(0, {
        "summary": summary,
        "detail": detail,
        "anchor_msg_id": _capture_anchor_msg_id(),
        "parked_at": datetime.now().isoformat(timespec="seconds"),
    })
    _write_parked_only(parked)

    return {"status": "success", "message": f"已暂存：「{summary}」"}


def conversation_recall_handler(index: int) -> dict:
    """召回暂存话题（召回即从 parked 数组移除）"""
    data = _read_memory_json()
    if data.get("_raw_fallback"):
        return {"status": "error", "message": "memory.json 文件损坏，请手动修复后重试"}
    parked = data.get("parked") or []
    if not isinstance(parked, list):
        logger.warning("memory.json parked is not a list, treating as empty")
        parked = []
    if not parked:
        return {"status": "error", "message": "暂存列表为空"}

    if index < 1 or index > len(parked):
        return {"status": "error", "message": f"序号超出范围(1-{len(parked)})"}

    item = parked.pop(index - 1)
    _write_parked_only(parked)
    if not isinstance(item, dict):
        item = {}

    summary = item.get("summary", "")
    detail = item.get("detail", "")
    anchor_msg_id = item.get("anchor_msg_id", "")

    # anchor 存活判定：空 anchor 或 DB 无该消息 → False（不报错）
    anchor_alive = False
    if anchor_msg_id:
        try:
            store = _get_store()
            anchor_alive = bool(_run_async(store.message_exists(anchor_msg_id)))
        except Exception as e:
            logger.warning(f"anchor 存活判定失败: {e}")

    message = f"已召回：「{summary}」"
    if anchor_alive:
        message += "；锚点有效，可用 get_messages/read_history_block 回放原文获取全量细节"
    return {
        "status": "success",
        "summary": summary,
        "detail": detail,
        "anchor_msg_id": anchor_msg_id,
        "anchor_alive": anchor_alive,
        "message": message,
    }


# ============================================================================
# Module-level aliases for ToolRegistry direct function lookup
# (without these, ToolRegistry falls back to call_tool wrapper which returns
#  [TextContent] instead of dict, breaking isinstance(result, dict) checks)
# ============================================================================

def user_memory_remember(content: str, type: str = "memory", **kwargs):
    return user_memory_remember_handler(content=content, type=type)

def user_memory_forget(**kwargs):
    return user_memory_forget_handler(**kwargs)

def user_memory_list(**kwargs):
    return user_memory_list_handler(**kwargs)

def conversation_park(summary: str, detail: str, **kwargs):
    return conversation_park_handler(summary=summary, detail=detail)

def conversation_recall(**kwargs):
    return conversation_recall_handler(**kwargs)


# ============================================================================
# MCP handlers
# ============================================================================


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List available tools."""
    return get_tool_definitions()


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Handle tool calls."""
    try:
        if name == "user_memory_remember":
            result = user_memory_remember_handler(
                content=arguments["content"],
                type=arguments.get("type", "memory"),
            )
        elif name == "user_memory_forget":
            result = user_memory_forget_handler(
                index=arguments.get("index"),
                keyword=arguments.get("keyword"),
            )
        elif name == "user_memory_list":
            result = user_memory_list_handler()
        elif name == "conversation_park":
            result = conversation_park_handler(
                summary=arguments["summary"],
                detail=arguments["detail"],
            )
        elif name == "conversation_recall":
            result = conversation_recall_handler(
                index=arguments["index"],
            )
        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

    except Exception as e:
        logger.exception(f"Error executing tool {name}: {e}")
        return [TextContent(type="text", text=f"Error: {e}")]


# ============================================================================
# Main
# ============================================================================


def main():
    """MCP 服务器入口"""
    import mcp.server.stdio

    logger.info("Memory Server 启动中...")

    async def run():
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream, write_stream, server.create_initialization_options()
            )

    asyncio.run(run())


if __name__ == "__main__":
    main()
