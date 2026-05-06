"""
Notes API - Sticky notes CRUD endpoints (JSON storage + LightRAGIngester sync)
"""

import asyncio
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, HTTPException
from loguru import logger
from pydantic import BaseModel

from niu_api.notes import create_note, delete_note, get_note, list_notes, update_note

_INVALID_ID_MSG = "Invalid note ID: use 1-128 alphanumeric, hyphen or underscore characters"

router = APIRouter(prefix="/api", tags=["notes"])


class NoteCreateRequest(BaseModel):
    id: str
    content: str
    tags: list[str] = []
    createdAt: float  # 前端传 ms 时间戳


class NoteUpdateRequest(BaseModel):
    id: str
    content: str
    tags: list[str] = []
    updatedAt: float  # 前端传 ms 时间戳


@router.post("/notes")
async def api_create_note(request: NoteCreateRequest, background_tasks: BackgroundTasks):
    """Create a new sticky note"""
    try:
        created_at = datetime.fromtimestamp(request.createdAt / 1000).isoformat()

        result = create_note(
            note_id=request.id,
            content=request.content,
            tags=request.tags,
            created_at=created_at,
        )

        if result["status"] == "invalid_id":
            raise HTTPException(status_code=400, detail=_INVALID_ID_MSG)

        # LightRAG 写入（后台任务，不阻塞响应）— 仅在创建成功时
        if result["status"] == "created":
            background_tasks.add_task(
                asyncio.to_thread, sync_note_to_lightrag, request.id, request.content, request.tags
            )

        return {"status": "ok", "result": result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Notes] Create failed: {e}")
        raise HTTPException(status_code=500, detail="Internal error")


@router.get("/notes")
async def api_list_notes():
    """List all sticky notes"""
    try:
        notes = await asyncio.to_thread(list_notes)
        return {"status": "ok", "notes": notes}
    except Exception as e:
        logger.error(f"[Notes] List failed: {e}")
        raise HTTPException(status_code=500, detail="Internal error")


@router.get("/notes/{note_id}")
async def api_get_note(note_id: str):
    """Get a single note"""
    note = await asyncio.to_thread(get_note, note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="Note not found")
    return {"status": "ok", "note": note}


@router.put("/notes/{note_id}")
async def api_update_note(note_id: str, request: NoteUpdateRequest, background_tasks: BackgroundTasks):
    """Update a sticky note"""
    try:
        result = update_note(note_id=note_id, content=request.content, tags=request.tags)

        if result["status"] == "invalid_id":
            raise HTTPException(status_code=400, detail=_INVALID_ID_MSG)
        if result["status"] == "not_found":
            raise HTTPException(status_code=404, detail="Note not found")

        # LightRAG 写入（后台任务，不阻塞响应）
        background_tasks.add_task(
            asyncio.to_thread, sync_note_to_lightrag, note_id, request.content, request.tags
        )

        return {"status": "ok", "result": result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Notes] Update failed: {e}")
        raise HTTPException(status_code=500, detail="Internal error")


@router.delete("/notes/{note_id}")
async def api_delete_note(note_id: str):
    """Delete a sticky note"""
    try:
        result = await asyncio.to_thread(delete_note, note_id=note_id)

        if result["status"] == "invalid_id":
            raise HTTPException(status_code=400, detail=_INVALID_ID_MSG)
        if result["status"] == "not_found":
            raise HTTPException(status_code=404, detail="Note not found")

        return {"status": "ok", "result": result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Notes] Delete failed: {e}")
        raise HTTPException(status_code=500, detail="Internal error")


def sync_note_to_lightrag(note_id: str, content: str, tags: list[str]):
    """便利贴写入 LightRAG 知识图谱。

    使用 LightRAGIngester.inject_entity() 注入实体，
    替代旧的 ainsert() 非结构化注入方式。
    """
    try:
        from niu_api.internal.lightrag_adapter import LightRAGIngester

        description = content + (" | 标签: " + ", ".join(tags) if tags else "")

        entity_name = f"note:{note_id}"

        ingester = LightRAGIngester()
        result = ingester.inject_custom_kg(
            entities=[{
                "entity_name": entity_name,
                "entity_type": "Note",
                "description": description,
            }],
            relationships=[{
                "src_id": "brain:Niu",
                "tgt_id": entity_name,
                "relation": "remembers",
                "description": "brain:Niu 记住了这条便签",
                "source_id": f"note:{note_id}",
                "file_path": f"note://{note_id}",
            }],
            chunks=[{
                "content": description,
                "source_id": f"note:{note_id}",
                "file_path": f"note://{note_id}",
            }],
            source_id=f"note:{note_id}",
        )
        if result.get("status") == "ok":
            logger.info(f"[Notes] LightRAG sync: note:{note_id}")
        else:
            logger.warning(f"[Notes] LightRAG sync failed for {note_id}: {result.get('message', '')}")
    except Exception as e:
        logger.warning(f"[Notes] LightRAG sync failed for {note_id}: {e}")
