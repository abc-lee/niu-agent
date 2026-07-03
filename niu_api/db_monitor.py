"""db 监测程序：轮询 messages db 中 role=subagent_msg 消息，按 @目标 路由。

后台 asyncio task，每 200ms 轮询。生命周期由 niu_api/__main__.py lifespan 管理。

实现说明：
- 直接用 sqlite3 查 messages.db（轻量，200ms 间隔足够），不依赖 MessageStore.get_messages
  （后者不支持 role/rowid 过滤，全表扫描浪费 CPU/IO）
- 用 _last_seen_rowid 游标只查 rowid > last_seen 的新消息，避免重复路由
- 不再用内存 set _routed_msg_ids（避免长期累积增长）
"""
import os
import re
import sqlite3
import asyncio
import logging
from typing import Tuple

from agent.runner import enqueue_supplement
from agent.subagent_registry import SubagentRegistry

logger = logging.getLogger(__name__)

# @消息格式：@目标 [发送者名] 内容（发送者名可选）
_AT_MSG_PATTERN = re.compile(r'^@(\S+)(?:\s+\[([^\]]+)\])?\s*(.*)$', re.DOTALL)

# messages.db 路径（与 agent/session.py MessageStore 默认路径一致）
_db_path = os.path.join(os.path.expanduser("~"), ".niu", "messages.db")

# 已路由的最后一个 rowid（内存游标，程序启动时从 db max(rowid) 初始化）
_last_seen_rowid: int = 0

# 心跳计数
_routed_count = 0


def _set_db_path(path: str) -> None:
    """测试用：覆盖 db 路径（避免污染真实 ~/.niu/messages.db）。"""
    global _db_path
    _db_path = path


async def _init_routed_baseline() -> None:
    """启动时记当前 max(rowid) 作为游标起点，避免重启后重复路由历史消息。

    只记一个 int 游标，不灌内存 set——长期运行无累积。
    """
    global _last_seen_rowid
    try:
        conn = sqlite3.connect(_db_path)
        try:
            cur = conn.execute(
                "SELECT COALESCE(MAX(rowid), 0) FROM messages WHERE role='subagent_msg'"
            )
            _last_seen_rowid = cur.fetchone()[0] or 0
        finally:
            conn.close()
        logger.info(f"db_monitor 基线初始化：last_seen_rowid={_last_seen_rowid}")
    except Exception as e:
        logger.error(f"db_monitor 基线初始化失败：{e}")


def parse_at_message(content: str) -> Tuple[str, str, str]:
    """解析 @消息格式，返回 (target, sender, content)。

    格式：@目标 [发送者名] 内容  或  @目标 内容
    """
    match = _AT_MSG_PATTERN.match(content.strip())
    if not match:
        return "", "", content
    target = match.group(1)
    sender = match.group(2) or ""
    body = match.group(3).strip()
    return target, sender, body


def route_message(target: str, sender: str, content: str) -> None:
    """路由一条 @ 消息到目标。

    阶段二改造：
    - target==主Agent：推入 MainAgentRequestQueue（新机制下不应出现，保留兼容防护）
    - target==子名 + is_terminate：cancel_pending_ask + 推 /stop 到 supplement queue
    - target==子名 + sender==主Agent（非 /stop）：set_answer 解除 ask_main_agent 阻塞；
      找不到 future 时降级推 supplement queue（主 Agent 在补充上下文）
    - target==子名 + 其他 sender：推 supplement queue
    - 孤儿回答（子 Agent 已退出）：sender==主Agent 时丢弃避免死循环，其他 sender 推回主 Agent
    """
    global _routed_count

    if target == "主Agent":
        # 阶段二：target==主Agent 的 subagent_msg 在新机制下不应出现
        # （ask_main_agent 和完成通知都改走 MainAgentRequestQueue 内存队列，不写 db）
        # 但为了兼容性（防止未来有人写 @主Agent 到 db），改为推入 MainAgentRequestQueue
        msg_for_queue = f"[{sender}] {content}" if sender else f"[主Agent] {content}"
        try:
            from agent.main_agent_request_queue import get_main_agent_request_queue
            get_main_agent_request_queue().push(msg_for_queue)
        except Exception as e:
            logger.error(f"db_monitor 推入 MainAgentRequestQueue 失败：{e}")
        _routed_count += 1
        logger.info(f"db_monitor 路由到主 Agent（推入 MainAgentRequestQueue）：{msg_for_queue[:50]}")
        return

    # 目标是子 Agent
    instance = SubagentRegistry.get(target)
    if instance is None:
        # 目标不在注册表（子 Agent 已退出/重启后残留消息）
        # sender==主Agent 时丢弃（孤儿回答，不推回主 Agent 避免死循环）
        # 其他 sender 推回主 Agent 让主 Agent 知道
        if sender == "主Agent":
            logger.warning(
                f"db_monitor 孤儿回答：主 Agent 回答到达但子 Agent {target} 已不在注册表，丢弃：{content[:50]}"
            )
        else:
            fallback = f"@主Agent [system] 目标子 Agent {target} 已不存在：{content}"
            enqueue_supplement(fallback)
            logger.warning(f"db_monitor 目标子 Agent {target} 不在注册表，推回主 Agent")
        return

    # /stop 优先分支：cancel_pending_ask + 推 /stop 到 supplement queue
    is_terminate = content.strip() == "/stop"

    if is_terminate:
        try:
            from agent.ask_main_agent import get_pending_ask_registry
            get_pending_ask_registry().cancel_pending_ask(target)
            logger.info(f"db_monitor /stop 同时 cancel ask_main_agent：{target}")
        except Exception as e:
            logger.error(f"db_monitor cancel_pending_ask 失败：{e}")

        instance.supplement_queue.push(content, is_terminate=True, sender=sender)
        _routed_count += 1
        logger.info(f"db_monitor 路由到子 Agent {target}：{content[:50]} (terminate=True)")
        return

    if sender == "主Agent":
        # 主 Agent 回答消息（非 /stop）→ 路由到 PendingAskRegistry.set_answer
        # 用有无 future 作为判据，不用 sender=="主Agent" 区分（主 Agent 也会补充上下文）
        try:
            from agent.ask_main_agent import get_pending_ask_registry
            found = get_pending_ask_registry().set_answer(target, content)
            if found:
                _routed_count += 1
                logger.info(
                    f"db_monitor 路由主 Agent 回答到 ask_main_agent：{target}：{content[:50]}"
                )
                return
            # 找不到 future：主 Agent 在补充上下文（不是回答 ask_main_agent），降级推 supplement queue
            instance.supplement_queue.push(content, is_terminate=False, sender=sender)
            _routed_count += 1
            logger.info(
                f"db_monitor 路由主 Agent 补充上下文到子 Agent：{target}：{content[:50]}"
            )
            return
        except Exception as e:
            logger.error(f"db_monitor set_answer 失败：{e}")
            instance.supplement_queue.push(content, is_terminate=False, sender=sender)
            _routed_count += 1
            return

    # 其他 sender 普通补充消息 → 推 supplement queue
    instance.supplement_queue.push(content, is_terminate=is_terminate, sender=sender)
    _routed_count += 1
    logger.info(f"db_monitor 路由到子 Agent {target}：{content[:50]} (terminate={is_terminate})")


async def _drain_main_agent_request_queue() -> None:
    """链路 A：主 Agent 闲置时消费 MainAgentRequestQueue，推 SSE 触发前端。

    逻辑：
    1. 检查 _chat_lock.locked()——主 Agent 忙则跳过（消息留队列）
    2. 检查 MainAgentRequestQueue.peek()——空则跳过
    3. 主 Agent 闲 + 队列有消息 → 推 SSE（notify_new_message_sync，source="subagent"）
    4. 推 SSE 成功后 pop 移除（避免推送失败丢消息）

    不写 db——写 db 由前端触发 /api/chat/session 后由 compat.py 完成
    """
    from agent.main_agent_request_queue import get_main_agent_request_queue
    from niu_api.compat import _chat_lock

    if _chat_lock.locked():
        return

    q = get_main_agent_request_queue()
    content = q.peek()
    if content is None:
        return

    try:
        from niu_api.chat import notify_new_message_sync
        notify_new_message_sync("", "subagent_msg", content, source="subagent")
        q.pop()
        logger.info(f"db_monitor 链路 A 推 SSE 触发主 Agent：{content[:50]}")
    except Exception as e:
        logger.error(f"db_monitor 链路 A 推 SSE 失败，消息留队列：{e}")


async def _poll_messages() -> None:
    """轮询 db 中 rowid > last_seen 的 subagent_msg 消息。

    SQL 直接过滤 role='subagent_msg' AND rowid > _last_seen_rowid，
    避免全表扫描和内存 set 累积。
    """
    global _last_seen_rowid
    try:
        conn = sqlite3.connect(_db_path)
        try:
            cur = conn.execute(
                "SELECT rowid, content FROM messages "
                "WHERE role='subagent_msg' AND rowid > ? "
                "ORDER BY rowid",
                (_last_seen_rowid,),
            )
            rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"db_monitor 查询消息失败：{e}")
        return

    for rowid, content in rows:
        target, sender, body = parse_at_message(content or "")
        if not target:
            logger.warning(f"db_monitor 无法解析 @消息：{(content or '')[:50]}")
            _last_seen_rowid = rowid
            continue
        try:
            route_message(target, sender, body)
        except Exception as e:
            logger.error(f"db_monitor 路由失败：{e}")
        _last_seen_rowid = rowid


async def run_db_monitor(interval: float = 0.2) -> None:
    """db 监测程序主循环。崩溃自动重启。

    两条职责独立的链路：
    - 链路 B（现有）：轮询 messages.db 中 role=subagent_msg 新消息，按 @目标路由
    - 链路 A（阶段二新增）：检测主 Agent 闲置 + 消费 MainAgentRequestQueue + 推 SSE 触发前端

    启动时先初始化基线（记当前 max(rowid) 作为游标），
    然后只路由启动后的新消息。
    """
    logger.info("db_monitor 启动")
    # 初始化基线
    await _init_routed_baseline()

    while True:
        try:
            await _poll_messages()
            await _drain_main_agent_request_queue()
            await asyncio.sleep(interval)
            # 每 100 条心跳日志
            if _routed_count > 0 and _routed_count % 100 == 0:
                logger.info(f"db_monitor 心跳：已路由 {_routed_count} 条消息")
        except asyncio.CancelledError:
            logger.info("db_monitor 收到取消信号，退出")
            break
        except Exception as e:
            logger.error(f"db_monitor 异常崩溃，1 秒后重启：{e}")
            await asyncio.sleep(1)
