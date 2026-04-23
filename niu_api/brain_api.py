"""Brain Graph API — Memory brain graph endpoints.

Exposes BrainGraph operations as FastAPI endpoints for the agent
to store and recall memories via the LightRAG knowledge graph.
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import BaseModel, Field

from niu_api.internal.brain_graph import BrainGraph, format_memories_for_prompt

router = APIRouter(prefix="/api/brain", tags=["brain"])


# ============== Request Models ==============


class RememberRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=2000)
    level: str = Field(default="L0", pattern=r"^(L0|L1|L2)$")
    memory_type: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class RecallRequest(BaseModel):
    query: str = Field(..., min_length=1)
    top_k: int = Field(default=10, ge=1, le=50)
    min_weight: float = Field(default=0.3, ge=0.0, le=1.0)


# ============== Endpoints ==============


@router.post("/remember")
def remember_memory(req: RememberRequest) -> Dict[str, Any]:
    """Store a memory in the brain graph."""
    try:
        bg = BrainGraph()
        result = bg.store_memory(
            content=req.content,
            level=req.level,
            memory_type=req.memory_type,
            metadata=req.metadata,
        )
        if result.get("status") == "error":
            raise HTTPException(status_code=500, detail=result.get("message", "Store failed"))
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[BRAIN] remember failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/recall")
def recall_memories(req: RecallRequest) -> Dict[str, Any]:
    """Recall memories from the brain graph."""
    try:
        bg = BrainGraph()
        memories = bg.recall_memories(
            query=req.query,
            top_k=req.top_k,
            min_weight=req.min_weight,
        )
        return {"memories": memories}
    except Exception as e:
        logger.error(f"[BRAIN] recall failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status")
def brain_status() -> Dict[str, Any]:
    """Get brain graph status."""
    try:
        bg = BrainGraph()
        bg.ensure_niu_entity()
        return {
            "status": "ok",
            "message": "Brain graph is active. brain:Niu entity ensured.",
        }
    except Exception as e:
        logger.error(f"[BRAIN] status check failed: {e}")
        return {"status": "error", "message": str(e)}
