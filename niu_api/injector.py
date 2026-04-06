"""
Injector API endpoints

手动注册 MCP 工具到向量库。
复用 VectorSearchAdapter，通过 metadata.type="mcp_tool" 标签区分。
"""

import json
from typing import Optional
from pydantic import BaseModel
from fastapi import APIRouter, HTTPException
import numpy as np

from agent.vector_search import get_vector_search

router = APIRouter(prefix="/api/inject", tags=["injector"])


class RegisterMCPToolRequest(BaseModel):
    """注册 MCP 工具请求"""

    server_name: str
    tool_name: str
    description: str = ""
    input_schema: dict = {}


class RegisterMCPToolResponse(BaseModel):
    """注册 MCP 工具响应"""

    status: str
    resource_id: str


class ListResourcesResponse(BaseModel):
    """列出资源响应"""

    resources: list[dict]


def _register_to_vector_db(doc_id: str, content: str, metadata: dict) -> bool:
    """写入向量库"""
    vs = get_vector_search()
    conn = vs._get_connection()
    if conn is None:
        return False

    # 获取向量
    embedding = vs._get_embedding(content)
    if embedding is None:
        return False

    embedding_blob = np.array(embedding, dtype=np.float32).tobytes()

    # UPSERT
    conn.execute(
        """
        INSERT INTO documents (id, content, embedding, metadata)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            content = excluded.content,
            embedding = excluded.embedding,
            metadata = excluded.metadata
        """,
        (doc_id, content, embedding_blob, json.dumps(metadata, ensure_ascii=False)),
    )
    conn.commit()
    return True


def _list_by_type(resource_type: str) -> list[dict]:
    """列出指定类型的资源"""
    vs = get_vector_search()
    conn = vs._get_connection()
    if conn is None:
        return []

    cursor = conn.execute(
        "SELECT id, content, metadata FROM documents WHERE metadata LIKE ?",
        (f'%"type": "{resource_type}"%',),
    )

    results = []
    for row in cursor.fetchall():
        metadata = json.loads(row[2]) if row[2] else {}
        if metadata.get("type") == resource_type:
            results.append(
                {
                    "id": row[0],
                    "content": row[1][:500],
                    "metadata": metadata,
                }
            )
    return results


@router.post("/mcp-tool", response_model=RegisterMCPToolResponse)
async def register_mcp_tool(request: RegisterMCPToolRequest):
    """
    注册 MCP 工具到向量库

    用法：新增 MCP 服务器后，调用此 API 注册其工具描述。
    """
    doc_id = f"mcp_tool:{request.server_name}:{request.tool_name}"
    content = f"{request.tool_name}: {request.description}"
    metadata = {
        "level": "l1",  # 小写，符合规范
        "category": "mcp_tool",  # 内容分类
        "name": request.tool_name,
        "server": request.server_name,
        "description": request.description,
        "input_schema": request.input_schema,
    }

    success = _register_to_vector_db(doc_id, content, metadata)

    if success:
        return RegisterMCPToolResponse(status="success", resource_id=doc_id)
    else:
        raise HTTPException(status_code=500, detail="Failed to register MCP tool")


@router.post("/mcp-tools/batch")
async def register_mcp_tools_batch(tools: list[RegisterMCPToolRequest]):
    """批量注册 MCP 工具"""
    results = []

    for tool in tools:
        doc_id = f"mcp_tool:{tool.server_name}:{tool.tool_name}"
        content = f"{tool.tool_name}: {tool.description}"
        metadata = {
            "level": "l1",  # 小写，符合规范
            "category": "mcp_tool",  # 内容分类
            "name": tool.tool_name,
            "server": tool.server_name,
            "description": tool.description,
            "input_schema": tool.input_schema,
        }

        success = _register_to_vector_db(doc_id, content, metadata)
        results.append(
            {
                "tool_name": tool.tool_name,
                "status": "success" if success else "failed",
                "resource_id": doc_id,
            }
        )

    return {"results": results}


@router.get("/resources", response_model=ListResourcesResponse)
async def list_resources(resource_type: str = None):
    """列出已注册的资源"""
    if resource_type:
        resources = _list_by_type(resource_type)
    else:
        resources = []
        for t in ["skill", "mcp_tool", "l1"]:
            resources.extend(_list_by_type(t))

    return ListResourcesResponse(
        resources=[
            {
                "id": r["id"],
                "type": r["metadata"].get("type"),
                "name": r["metadata"].get("name"),
                "description": r["metadata"].get("description", "")[:200],
            }
            for r in resources
        ]
    )


@router.delete("/resource/{resource_id}")
async def delete_resource(resource_id: str):
    """删除资源"""
    vs = get_vector_search()
    conn = vs._get_connection()
    if conn is None:
        raise HTTPException(status_code=500, detail="Database not available")

    conn.execute("DELETE FROM documents WHERE id = ?", (resource_id,))
    conn.commit()

    return {"status": "success", "resource_id": resource_id}


@router.post("/skills/sync")
async def sync_skills():
    """手动触发 Skills 同步"""
    from agent.injector import get_skill_sync

    skill_sync = get_skill_sync(auto_start=False)
    added, updated, deleted = skill_sync.scan_and_sync()

    return {
        "status": "success",
        "added": added,
        "updated": updated,
        "deleted": deleted,
    }
