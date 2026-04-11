"""
异步任务通知 API

完全照搬定时任务的 trigger_callback 实现：
1. 构建提示词
2. 调用 /chat/sync 激活主 Agent
3. 添加到 pending_alerts 队列
"""

from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional
import requests
from loguru import logger

from niu_api.alerts import add_pending_alert

router = APIRouter(prefix="/api/async-task", tags=["async-tasks"])


class TaskNotifyRequest(BaseModel):
    """异步任务完成通知请求"""
    type: str  # "task_complete" | "task_failed"
    result: Optional[str] = None
    error: Optional[str] = None


@router.post("/notify")
async def notify_async_task(request: TaskNotifyRequest):
    """
    异步任务完成通知（照搬 scheduler/service.py 的 trigger_callback）

    参考：niu_api/internal/scheduler/service.py:trigger_callback
    """
    logger.info(f"[ASYNC-TASK] Received notification: type={request.type}")

    # 1. 构建提示词（照搬定时任务）
    if request.type == "task_complete":
        prompt = f"🔔 异步任务完成：\n{request.result}\n\n请根据这个结果，给用户一个友好的回复。"
    else:
        prompt = f"⚠️ 异步任务失败：\n{request.error}\n\n请告知用户任务执行失败。"

    # 2. 调用 /chat/sync（激活主 Agent，照搬定时任务）
    try:
        response = requests.post(
            "http://localhost:9876/chat/sync",
            json={
                "session_id": "default",  # session_id 被 ignore
                "message": prompt
            },
            timeout=30
        )

        if response.status_code == 200:
            agent_reply = response.json().get("reply", "")
            logger.info(f"[ASYNC-TASK] Agent replied: {agent_reply[:100] if agent_reply else '(empty)'}")

            # 3. 添加到 pending_alerts（照搬定时任务）
            if agent_reply:
                add_pending_alert(agent_reply)
                logger.info(f"[ASYNC-TASK] Added to pending-alerts queue")

            return {"success": True, "reply": agent_reply}
        else:
            # 即使 API 出错，也发送基础提醒（照搬定时任务）
            fallback_msg = f"异步任务完成：{request.result[:200] if request.result else request.error}"
            add_pending_alert(fallback_msg)
            logger.error(f"[ASYNC-TASK] Chat API error: {response.status_code}")
            return {"success": False, "error": f"Chat API returned {response.status_code}"}

    except Exception as e:
        # 异常处理（照搬定时任务）
        fallback_msg = f"异步任务完成：{request.result[:200] if request.result else request.error}"
        add_pending_alert(fallback_msg)
        logger.error(f"[ASYNC-TASK] Failed to call chat API: {e}")
        return {"success": False, "error": str(e)}
