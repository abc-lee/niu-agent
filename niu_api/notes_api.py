"""
Notes API - Sticky notes CRUD endpoints (JSON storage)
"""

import asyncio
from datetime import datetime

from fastapi import APIRouter, HTTPException
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
async def api_create_note(request: NoteCreateRequest):
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
async def api_update_note(note_id: str, request: NoteUpdateRequest):
    """Update a sticky note"""
    try:
        result = update_note(note_id=note_id, content=request.content, tags=request.tags)

        if result["status"] == "invalid_id":
            raise HTTPException(status_code=400, detail=_INVALID_ID_MSG)
        if result["status"] == "not_found":
            raise HTTPException(status_code=404, detail="Note not found")

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
