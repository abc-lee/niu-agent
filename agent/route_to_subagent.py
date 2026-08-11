"""公共函数：路由消息到子 Agent。

提取自 db_monitor.route_message 的子 Agent 路由逻辑。
POST API 和 db_monitor 都调用此函数。

source='db_monitor': 保留原有全部行为（孤儿 sender=主Agent 推 MainAgentRequestQueue 反馈主 Agent；其他 sender 推回主 Agent via enqueue_supplement）
source='post_api': 孤儿回答返回 not_found，不推回主 Agent
"""
import logging

from agent.runner import enqueue_supplement
from agent.subagent_registry import SubagentRegistry

logger = logging.getLogger(__name__)


def route_to_subagent(target: str, sender: str, content: str, source: str = 'db_monitor') -> dict:
    """路由消息到子 Agent。

    Returns:
        {"status": "ok"|"error"|"not_found", "message": str}
    """
    # trailing 标点 strip（先 strip 再查找，简化原 db_monitor 的两步查找）
    target = target.rstrip('。，！？；：、.,!?;:')
    instance = SubagentRegistry.get(target)

    if instance is None:
        # 孤儿回答
        if source == 'post_api':
            return {"status": "not_found", "message": f"子 Agent {target} 不存在或已结束"}
        # db_monitor 场景：sender=='主Agent' 反馈主 Agent（不静默丢弃），其他推回主 Agent
        if sender == '主Agent':
            # 用户拍板（2026-08-11）：@ 不到必须反馈主 Agent，不静默丢弃。
            # R3 定稿：MainAgentRequestQueue 直推 → db_monitor 链路 A 主 Agent 闲置时推 SSE 读到
            # （不经 supplement 防 drain；不写 DB 防上下文污染）。
            # 循环有界：内容带 [system] 前缀且主 Agent 收到后（Task 5 存在性检查）对不存在子 Agent
            # 返回 error → 主 Agent 不再重复 @——不会死循环。
            try:
                from agent.main_agent_request_queue import get_main_agent_request_queue
                get_main_agent_request_queue().push(
                    f"@主Agent [system] 目标子 Agent {target} 已不存在（可能已结束或被清理），无法接收消息：{content[:200]}"
                )
                logger.info(f"[route] 主 Agent 回复孤儿 {target}，已推入主 Agent 请求队列")
            except Exception as e:
                logger.error(f"[route] 推孤儿通知失败: {e}")
            return {"status": "error", "message": "orphan reported to main agent (queue)"}
        # 推回主 Agent（与原 db_monitor 一致，用 enqueue_supplement）
        fallback = f"@主Agent [system] 目标子 Agent {target} 已不存在：{content}"
        enqueue_supplement(fallback)
        return {"status": "error", "message": "orphan forwarded to main agent"}

    sq = instance.supplement_queue

    # /stop 终止命令（注意 strip，与 db_monitor L126 一致）
    if content.strip() == '/stop':
        from agent.ask_main_agent import get_pending_ask_registry
        pending_ask = get_pending_ask_registry()
        pending_ask.cancel_pending_ask(target)
        # 也 cancel pending ask_user（如果有）
        try:
            from agent.ask_user import get_user_ask_registry
            get_user_ask_registry().cancel_pending_ask(target)
        except ImportError:
            pass
        sq.push('/stop', is_terminate=True, sender=sender)
        logger.info(f"[route] /stop → {target}")
        return {"status": "ok", "message": f"已发送 /stop 到 {target}"}

    # 主 Agent 回复 @niu-agent 挂起的子 Agent
    if sender == '主Agent':
        from agent.ask_main_agent import get_pending_ask_registry
        pending_ask = get_pending_ask_registry()
        if pending_ask.set_answer(target, content):
            logger.info(f"[route] 主 Agent 回答 → {target}")
            # 推送主 Agent 回答到子 Agent tab（异步路径）
            from agent.subagent import _maybe_push_subagent_instruction
            _maybe_push_subagent_instruction(target, content)
            return {"status": "ok", "message": f"已回答 {target}"}
        # set_answer 失败（无 pending future），降级推 supplement_queue
        logger.warning(f"[route] {target} 无 pending ask，降级推 supplement_queue")
        sq.push(content, is_terminate=False, sender=sender)
        return {"status": "ok", "message": f"已推送补充信息到 {target}"}

    # 用户回答 @user 挂起的子 Agent
    if sender == 'user':
        try:
            from agent.ask_user import get_user_ask_registry
            user_ask = get_user_ask_registry()
            if user_ask.set_answer(target, content):
                logger.info(f"[route] 用户回答 → {target}")
                return {"status": "ok", "message": f"已回答 {target}"}
        except ImportError:
            pass
        # 无 pending ask_user 或模块未就绪，降级推 supplement_queue
        sq.push(content, is_terminate=False, sender='user')
        return {"status": "ok", "message": f"已推送补充信息到 {target}"}

    # 其他 sender：直接推 supplement_queue
    sq.push(content, is_terminate=False, sender=sender)
    return {"status": "ok", "message": f"已推送消息到 {target}"}
