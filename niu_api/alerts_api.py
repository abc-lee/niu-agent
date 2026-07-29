"""Alerts API Router"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from niu_api.alerts import add_pending_alert, get_and_clear_pending_alerts

router = APIRouter(prefix="/api", tags=["alerts"])


class AddAlertRequest(BaseModel):
    """添加提醒请求"""
    content: str
    task_id: str | None = None


@router.get("/pending-alerts")
async def pending_alerts():
    """获取待推送提醒（供前端轮询）"""
    alerts = get_and_clear_pending_alerts()
    return alerts


@router.post("/alerts")
async def add_alert(request: AddAlertRequest):
    """
    添加提醒到队列

    供 scheduler-service 调用，任务触发时添加提醒通知。
    注意：按照审核意见，/chat/sync 返回的 reply 就是提醒内容，
    不需要额外调用此接口，此接口作为备用降级方案。
    """
    try:
        add_pending_alert(request.content)
        return {"status": "ok", "message": "Alert added"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
