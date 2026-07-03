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
    """路由一条 @ 消息到目标。"""
    global _routed_count

    if target == "主Agent":
        # 推入主 Agent supplement queue，格式含 @主Agent 标识让主 Agent 能识别
        full_msg = f"@主Agent [{sender}] {content}" if sender else f"@主Agent {content}"
        enqueue_supplement(full_msg)
        _routed_count += 1
        logger.info(f"db_monitor 路由到主 Agent：{full_msg[:50]}")
        return

    # 目标是子 Agent
    instance = SubagentRegistry.get(target)
    if instance is None:
        # 目标不在注册表，推回主 Agent
        fallback = f"@主Agent [system] 目标子 Agent {target} 已不存在：{content}"
        enqueue_supplement(fallback)
        logger.warning(f"db_monitor 目标子 Agent {target} 不在注册表，推回主 Agent")
        return

    # 推入子 Agent supplement queue
    is_terminate = content.strip() == "/stop"
    instance.supplement_queue.push(content, is_terminate=is_terminate, sender=sender)
    _routed_count += 1
    logger.info(f"db_monitor 路由到子 Agent {target}：{content[:50]} (terminate={is_terminate})")


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

    启动时先初始化基线（记当前 max(rowid) 作为游标），
    然后只路由启动后的新消息。
    """
    logger.info("db_monitor 启动")
    # 初始化基线
    await _init_routed_baseline()

    while True:
        try:
            await _poll_messages()
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
