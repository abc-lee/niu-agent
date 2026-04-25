"""
Injector API endpoints

手动注册 MCP 工具到 LightRAG 知识图谱。
通过 entity_type="tool" 标签区分，供 LightRAGAdapter.search_tools() 检索。
"""

import json
from pydantic import BaseModel
from fastapi import APIRouter, HTTPException
from loguru import logger

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


# Mapping from injector category names to LightRAG entity_type values.
# "mcp_tool" category maps to "tool" entity_type in LightRAG (tools are
# registered via inject_entity with entity_type="tool").
# "l1" category has no direct LightRAG equivalent; listing it returns empty.
_CATEGORY_TO_ENTITY_TYPE: dict[str, str] = {
    "skill": "skill",
    "mcp_tool": "tool",
}


@router.post("/mcp-tool", response_model=RegisterMCPToolResponse)
async def register_mcp_tool(request: RegisterMCPToolRequest):
    """
    注册 MCP 工具到 LightRAG 知识图谱

    用法：新增 MCP 服务器后，调用此 API 注册其工具描述。
    工具以 entity_type="tool" 实体存入图谱，供 search_tools() 检索。
    """
    full_name = f"{request.server_name}/{request.tool_name}"
    content = f"{request.tool_name}: {request.description}"

    try:
        from niu_api.internal.lightrag_adapter import LightRAGIngester

        ingester = LightRAGIngester()
        result = ingester.inject_entity(
            name=f"tool:{full_name}",
            entity_type="tool",
            description=request.description,
            chunk_content=content,
            file_path=f"tool://{full_name}",
        )
        if result.get("status") == "ok":
            doc_id = f"mcp_tool:{request.server_name}:{request.tool_name}"
            return RegisterMCPToolResponse(status="success", resource_id=doc_id)
    except Exception as e:
        logger.error(f"Failed to register MCP tool {full_name} to LightRAG: {e}")

    raise HTTPException(status_code=500, detail="Failed to register MCP tool")


@router.post("/mcp-tools/batch")
async def register_mcp_tools_batch(tools: list[RegisterMCPToolRequest]):
    """批量注册 MCP 工具到 LightRAG"""
    results = []

    for tool in tools:
        full_name = f"{tool.server_name}/{tool.tool_name}"
        content = f"{tool.tool_name}: {tool.description}"

        try:
            from niu_api.internal.lightrag_adapter import LightRAGIngester

            ingester = LightRAGIngester()
            result = ingester.inject_entity(
                name=f"tool:{full_name}",
                entity_type="tool",
                description=tool.description,
                chunk_content=content,
                file_path=f"tool://{full_name}",
            )
            status = "success" if result.get("status") == "ok" else "failed"
        except Exception as e:
            logger.error(f"Failed to register MCP tool {full_name}: {e}")
            status = "failed"

        results.append({
            "tool_name": tool.tool_name,
            "status": status,
            "resource_id": f"mcp_tool:{tool.server_name}:{tool.tool_name}",
        })

    return {"results": results}


@router.get("/resources", response_model=ListResourcesResponse)
async def list_resources(resource_type: str = None):
    """列出已注册的资源（从 LightRAG 知识图谱查询）"""
    try:
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        adapter = LightRAGAdapter()
        resources: list[dict] = []

        categories = [resource_type] if resource_type else list(_CATEGORY_TO_ENTITY_TYPE.keys())

        for cat in categories:
            entity_type = _CATEGORY_TO_ENTITY_TYPE.get(cat)
            if entity_type is None:
                # Categories without a LightRAG mapping (e.g. "l1") yield nothing.
                continue

            result = adapter.list_entities(
                list_type="entities",
                entity_type=entity_type,
                limit=100,
            )
            if result.get("status") != "ok":
                logger.warning(f"list_entities returned error for {cat}: {result.get('message')}")
                continue

            for entity in result.get("data", []):
                name = entity.get("id", "") or entity.get("entity_name", "")
                resources.append({
                    "id": name,
                    "type": cat,
                    "name": name,
                    "description": (entity.get("description", "") or "")[:200],
                })

        return ListResourcesResponse(resources=resources)
    except Exception as e:
        logger.error(f"Failed to list resources from LightRAG: {e}")
        return ListResourcesResponse(resources=[])


@router.delete("/resource/{resource_id}")
async def delete_resource(resource_id: str):
    """删除资源（从 LightRAG 知识图谱删除）"""
    try:
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        adapter = LightRAGAdapter()
        result = adapter.delete_entity(resource_id)
        if result.get("status") == "ok":
            return {"status": "success", "resource_id": resource_id}
        else:
            raise HTTPException(
                status_code=500,
                detail=f"LightRAG deletion failed: {result.get('message', 'unknown error')}",
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete resource from LightRAG: {e}")
        raise HTTPException(status_code=500, detail=str(e))


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
