"""
Message API endpoints - 简化版（无 Session 概念）

所有消息属于用户，不区分 session。
"""

from typing import Optional, List
from pydantic import BaseModel
from fastapi import APIRouter

import sys

sys.path.insert(0, "..")
from agent.session import MessageStore, Message, get_message_store

router = APIRouter(tags=["session"])


class MessageResponse(BaseModel):
    """Message response model"""

    id: str
    role: str
    content: str
    created_at: str


class MessagesResponse(BaseModel):
    """Messages list response"""

    messages: List[MessageResponse]
    total_count: int
    total_in_db: int


@router.get("/{session_id}/messages")
async def get_messages(
    session_id: str, limit: int = 50, before_id: Optional[str] = None
) -> MessagesResponse:
    """Get messages (session_id is ignored - all messages belong to user)"""
    store = await get_message_store()
    msg_before_id = before_id if before_id else None
    messages = await store.get_messages(limit, msg_before_id)
    total_count = await store.count_messages()

    return MessagesResponse(
        messages=[
            MessageResponse(
                id=msg.id, role=msg.role, content=msg.content, created_at=msg.created_at
            )
            for msg in messages
        ],
        total_count=len(messages),
        total_in_db=total_count,
    )


@router.delete("/{session_id}/messages")
async def delete_messages(session_id: str) -> dict:
    """Clear all messages (session_id is ignored)"""
    store = await get_message_store()
    count = await store.clear_messages()
    return {"deleted_count": count}


# 以下端点保留兼容性，但不做实际操作


@router.post("/")
async def create_session() -> dict:
    """Create a new session (deprecated - returns fixed ID)"""
    return {"session_id": "default"}


@router.get("/")
async def list_sessions(limit: int = 20) -> List[dict]:
    """List recent sessions (deprecated - returns single default)"""
    return [{"session_id": "default", "message_count": 0}]


@router.delete("/{session_id}")
async def delete_session(session_id: str) -> dict:
    """Delete a session (deprecated - clears all messages)"""
    store = await get_message_store()
    await store.clear_messages()
    from niu_api.chat import get_or_create_runner
    runner = get_or_create_runner()
    if runner and runner.handler:
        runner.handler._last_prompt_tokens = 0
    return {"deleted": True}
