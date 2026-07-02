"""db 监测程序：轮询 messages db 中 role=subagent_msg 消息，按 @目标 路由。

后台 asyncio task，每 200ms 轮询。生命周期由 niu_api/__main__.py lifespan 管理。
"""
import re
import asyncio
import logging
from typing import Tuple

from agent.runner import enqueue_supplement
from agent.subagent_registry import SubagentRegistry
from agent.session import get_message_store

logger = logging.getLogger(__name__)

# @消息格式：@目标 [发送者名] 内容（发送者名可选）
_AT_MSG_PATTERN = re.compile(r'^@(\S+)(?:\s+\[([^\]]+)\])?\s*(.*)$', re.DOTALL)

# 已路由的消息 id 集合（内存，程序启动时从 db 灌入基线）
_routed_msg_ids: set = set()

# 心跳计数
_routed_count = 0


async def _init_routed_baseline(message_store) -> None:
    """启动时拿当前所有 subagent_msg 消息 id 灌入基线，避免重启后重复路由历史消息。"""
    global _routed_msg_ids
    try:
        msgs = await message_store.get_messages()
        for msg in msgs:
            if msg.role == "subagent_msg":
                _routed_msg_ids.add(msg.id)
        logger.info(f"db_monitor 基线初始化：{len(_routed_msg_ids)} 条历史 subagent_msg 消息标记为已路由")
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


async def _poll_messages(message_store) -> None:
    """轮询 db 中未路由的 subagent_msg 消息。"""
    global _routed_msg_ids
    try:
        msgs = await message_store.get_messages()
    except Exception as e:
        logger.error(f"db_monitor 查询消息失败：{e}")
        return

    for msg in msgs:
        if msg.id in _routed_msg_ids:
            continue
        if msg.role != "subagent_msg":
            continue
        target, sender, content = parse_at_message(msg.content)
        if not target:
            logger.warning(f"db_monitor 无法解析 @消息：{msg.content[:50]}")
            _routed_msg_ids.add(msg.id)
            continue
        try:
            route_message(target, sender, content)
        except Exception as e:
            logger.error(f"db_monitor 路由失败：{e}")
        _routed_msg_ids.add(msg.id)


async def run_db_monitor(interval: float = 0.2) -> None:
    """db 监测程序主循环。崩溃自动重启。

    启动时先初始化基线（当前所有 subagent_msg 标记为已路由），
    然后只路由启动后的新消息。
    """
    logger.info("db_monitor 启动")
    # 初始化基线
    message_store = await get_message_store()
    await _init_routed_baseline(message_store)

    while True:
        try:
            await _poll_messages(message_store)
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
