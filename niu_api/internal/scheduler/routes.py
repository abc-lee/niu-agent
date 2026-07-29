"""
Scheduler FastAPI Router

HTTP 接口用于 MCP 适配器和其他程序化调用。
"""


from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import BaseModel

from .service import get_store

# ============== 路由器 ==============

router = APIRouter(prefix="/scheduler", tags=["scheduler"])


# ============== 请求模型 ==============

class CreateTaskRequest(BaseModel):
    content: str
    scheduled_at: str
    event_type: str = "reminder"
    is_recurring: bool = False
    cron_expr: str | None = None
    name: str | None = None


class UpdateTaskRequest(BaseModel):
    content: str | None = None
    scheduled_at: str | None = None
    cron_expr: str | None = None


# ============== HTTP 接口 ==============

@router.get("/health")
async def health():
    """健康检查"""
    return {"status": "ok", "service": "scheduler-internal"}


@router.post("/tasks")
async def create_task(request: CreateTaskRequest):
    """创建定时任务"""
    try:
        store = get_store()
        task_id = store.create_task(
            content=request.content,
            scheduled_at=request.scheduled_at,
            event_type=request.event_type,
            is_recurring=request.is_recurring,
            cron_expr=request.cron_expr,
            name=request.name
        )

        logger.info(f"[SCHEDULER] Task created: {task_id} - {request.content}")

        return {
            "status": "success",
            "task_id": task_id,
            "message": f"✅ 已创建定时任务：{request.content}"
        }
    except Exception as e:
        logger.error(f"[SCHEDULER] Create task error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/tasks")
async def list_tasks(status: str | None = None):
    """查询任务列表"""
    try:
        store = get_store()
        tasks = store.list_tasks(status)
        return {
            "status": "success",
            "tasks": tasks,
            "count": len(tasks)
        }
    except Exception as e:
        logger.error(f"[SCHEDULER] List tasks error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/tasks/{task_id}")
async def get_task(task_id: str):
    """获取单个任务"""
    try:
        store = get_store()
        task = store.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        return {"status": "success", "task": task}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[SCHEDULER] Get task error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.delete("/tasks/{task_id}")
async def cancel_task(task_id: str):
    """取消任务"""
    try:
        store = get_store()
        success = store.cancel_task(task_id)
        return {
            "status": "success" if success else "error",
            "message": "✅ 任务已取消" if success else "❌ 任务不存在或已完成"
        }
    except Exception as e:
        logger.error(f"[SCHEDULER] Cancel task error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.put("/tasks/{task_id}")
async def update_task(task_id: str, request: UpdateTaskRequest):
    """更新任务"""
    try:
        store = get_store()
        success = store.update_task(
            task_id=task_id,
            content=request.content,
            scheduled_at=request.scheduled_at,
            cron_expr=request.cron_expr
        )
        return {
            "status": "success" if success else "error",
            "message": "✅ 任务已更新" if success else "❌ 任务不存在或已完成"
        }
    except Exception as e:
        logger.error(f"[SCHEDULER] Update task error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e
