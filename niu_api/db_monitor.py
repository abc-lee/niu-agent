"""db 监测程序：轮询 messages db 中 role=subagent_msg 消息，按 @目标 路由。

后台 asyncio task，每 200ms 轮询。生命周期由 niu_api/__main__.py lifespan 管理。

实现说明：
- 直接用 sqlite3 查 messages.db（轻量，200ms 间隔足够），不依赖 MessageStore.get_messages
  （后者不支持 role/rowid 过滤，全表扫描浪费 CPU/IO）
- 用 _last_seen_rowid 游标只查 rowid > last_seen 的新消息，避免重复路由
- 不再用内存 set _routed_msg_ids（避免长期累积增长）
"""
import asyncio
import logging
import os
import re
import sqlite3


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


def parse_at_message(content: str) -> tuple[str, str, str]:
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

    - target==主Agent：推入 MainAgentRequestQueue（兼容防护）
    - target==子名：委托 route_to_subagent 公共函数处理
    """
    global _routed_count

    if target == "主Agent":
        # 阶段二：target==主Agent 的 subagent_msg 在新机制下不应出现
        # （ask_main_agent 和完成通知都改走 MainAgentRequestQueue 内存队列，不写 db）
        # 但为了兼容性（防止未来有人写 @主Agent 到 db），改为推入 MainAgentRequestQueue
        # push type="ask"——语义=有人 @主Agent 找主 Agent（需要主 Agent 处理/回复）
        msg_for_queue = f"[{sender}] {content}" if sender else f"[主Agent] {content}"
        try:
            from agent.main_agent_request_queue import get_main_agent_request_queue
            get_main_agent_request_queue().push(msg_for_queue, type="ask")
        except Exception as e:
            logger.error(f"db_monitor 推入 MainAgentRequestQueue 失败：{e}")
        _routed_count += 1
        logger.info(f"db_monitor 路由到主 Agent（推入 MainAgentRequestQueue）：{msg_for_queue[:50]}")
        return

    # 目标是子 Agent —— 调用公共函数 route_to_subagent
    from agent.route_to_subagent import route_to_subagent
    result = route_to_subagent(target, sender, content, source='db_monitor')
    _routed_count += 1
    logger.info(f"db_monitor route_to_subagent: {result}")


async def _drain_main_agent_request_queue() -> None:
    """链路 A：主 Agent 闲置时消费 MainAgentRequestQueue，推 SSE 触发前端。

    逻辑：
    1. 检查 _chat_lock.locked()——主 Agent 忙则跳过（消息留队列）
    2. 检查 MainAgentRequestQueue.peek()——空则跳过
    3. 主 Agent 闲 + 队列有消息 → 推 SSE（notify_new_message_sync，source="subagent"）
    4. 阶段二 C1：notify 返回 False 表示推送失败（主 loop 不可用/已关闭/无订阅者），
       不 pop 消息留队列下次重试；返回 True 才 pop 移除

    不写 db——写 db 由前端触发 /api/chat/session 后由 compat.py 完成

    阶段二说明：队列里 ask 请求和完成通知两类消息都由程序源头 push，type 字段
    （"ask"/"notify"）在队列层结构区分。db_monitor **不注入任何前缀**——T2.1 的
    【子Agent提问·需回复】文本标志由代码拼装（非 LLM 话术，无变异），链路 A 只做
    content 直通 + peek_type() 日志标注（ask/notify）。
    """
    from agent.main_agent_request_queue import get_main_agent_request_queue

    from niu_api.compat import _chat_lock

    if _chat_lock.locked():
        return

    q = get_main_agent_request_queue()
    content = q.peek()
    if content is None:
        return
    msg_type = q.peek_type()

    # 阶段二 C1：检查 notify 返回值，False 时不 pop 留队列下次重试
    try:
        from niu_api.chat import notify_new_message_sync
        ok = notify_new_message_sync("", "subagent_msg", content, source="subagent")
        if ok:
            q.pop()
            logger.info(f"db_monitor 链路 A 推 SSE 触发主 Agent（type={msg_type}）：{content[:50]}")
        else:
            # 推送失败（主 loop 不可用/已关闭/无订阅者），消息留队列下次重试
            logger.warning("db_monitor 链路 A 推 SSE 失败（loop 不可用或无订阅者），消息留队列重试")
    except Exception as e:
        logger.error(f"db_monitor 链路 A 推 SSE 异常，消息留队列：{e}")


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
