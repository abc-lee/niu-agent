"""
Notes API - Sticky notes CRUD endpoints
"""

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel
from loguru import logger

from niu_api.notes import create_note, update_note, delete_note, list_notes, get_note

router = APIRouter(prefix="/api", tags=["notes"])


class NoteCreateRequest(BaseModel):
    id: str
    content: str
    createdAt: float  # 前端传 ms 时间戳


class NoteUpdateRequest(BaseModel):
    id: str
    content: str
    updatedAt: float  # 前端传 ms 时间戳


@router.post("/notes")
async def api_create_note(request: NoteCreateRequest, background_tasks: BackgroundTasks):
    """Create a new sticky note"""
    try:
        from datetime import datetime
        created_at = datetime.fromtimestamp(request.createdAt / 1000).isoformat()

        result = await create_note(
            note_id=request.id,
            content=request.content,
            created_at=created_at,
        )

        # KG 写入（后台任务，不阻塞响应）
        background_tasks.add_task(sync_note_to_kg, request.id, request.content)

        return {"status": "ok", "result": result}
    except Exception as e:
        logger.error(f"[Notes] Create failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/notes")
async def api_list_notes():
    """List all sticky notes"""
    try:
        notes = await list_notes()
        return {"status": "ok", "notes": notes}
    except Exception as e:
        logger.error(f"[Notes] List failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/notes/{note_id}")
async def api_get_note(note_id: str):
    """Get a single note"""
    note = await get_note(note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="Note not found")
    return {"status": "ok", "note": note}


@router.put("/notes/{note_id}")
async def api_update_note(note_id: str, request: NoteUpdateRequest, background_tasks: BackgroundTasks):
    """Update a sticky note"""
    try:
        result = await update_note(note_id=note_id, content=request.content)

        # KG 写入（后台任务，不阻塞响应）
        background_tasks.add_task(sync_note_to_kg, note_id, request.content)

        return {"status": "ok", "result": result}
    except Exception as e:
        logger.error(f"[Notes] Update failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/notes/{note_id}")
async def api_delete_note(note_id: str):
    """Delete a sticky note"""
    try:
        result = await delete_note(note_id=note_id)
        return {"status": "ok", "result": result}
    except Exception as e:
        logger.error(f"[Notes] Delete failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def sync_note_to_kg(note_id: str, content: str):
    """便利贴写入 LightRAG 知识图谱。

    使用 ainsert() 进行非结构化注入，LightRAG 内部自动提取实体。
    """
    try:
        from niu_api.internal.lightrag_manager import call_async, get_lightrag
        rag = get_lightrag()
        if rag is None:
            logger.warning(f"[Notes] LightRAG not available, skipping sync for {note_id}")
            return
        uri = f"note://{note_id}"
        prefixed = f"[Note: {note_id}]\n{content}"
        call_async(rag.ainsert(prefixed, file_paths=[uri]))
        logger.info(f"[Notes] LightRAG sync: {uri}")
    except Exception as e:
        logger.warning(f"[Notes] LightRAG sync failed for {note_id}: {e}")
