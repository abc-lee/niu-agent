"""
Niu Knowledge Graph MCP Server

Provides tools for managing a knowledge graph using Kuzu database.
Supports creating documents, entities, relations and querying the graph.
"""

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import kuzu
from loguru import logger
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool


# Default database path - use environment variable or current directory
def get_db_path() -> Path:
    """Get database path (3-level priority, consistent with resolve_vector_db_path).

    Priority:
    1. NIU_DB_PATH env var (explicit override, replaces vectors.db with knowledge.db)
    2. WORKSPACE_PATH env var (set by Go launcher main.go)
    3. ~/.niu/memory.json workspace.path
    不降级、不创建流氓库。
    """
    # 1. NIU_DB_PATH — replace vectors.db suffix with knowledge.db
    if "NIU_DB_PATH" in os.environ:
        p = Path(os.environ["NIU_DB_PATH"])
        if not p.parent.exists():
            raise ValueError(f"NIU_DB_PATH 父目录不存在: {p.parent}。请检查配置。")
        return p.parent / "knowledge.db"

    # 2. WORKSPACE_PATH env var
    if "WORKSPACE_PATH" in os.environ:
        ws = Path(os.environ["WORKSPACE_PATH"])
        if not ws.exists():
            raise ValueError(f"WORKSPACE_PATH 指向不存在的目录: {ws}。请检查配置。")
        return ws / "knowledge.db"

    # 3. 从 ~/.niu/memory.json 读取 workspace.path
    memory_path = Path.home() / ".niu" / "memory.json"
    if memory_path.exists():
        try:
            with open(memory_path, "r", encoding="utf-8") as f:
                memory = json.load(f)
            workspace_path = memory.get("workspace", {}).get("path")
            if workspace_path and Path(workspace_path).exists():
                return Path(workspace_path) / "knowledge.db"
            if workspace_path:
                raise ValueError(f"workspace.path 指向不存在的目录: {workspace_path}。请检查 memory.json 配置。")
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"无法从 {memory_path} 解析 JSON: {e}。") from e

    raise ValueError(
        f"无法确定知识库路径：~/.niu/memory.json 不存在或缺少 workspace.path 配置。"
        f"请在 ~/.niu/memory.json 中设置 workspace.path，或设置 WORKSPACE_PATH 环境变量。"
    )


try:
    DEFAULT_DB_PATH = get_db_path()
except ValueError as e:
    logger.warning(f"知识库路径解析失败: {e}")
    DEFAULT_DB_PATH = None

# Initialize MCP server
server = Server("niu-kg-server")

# Global database connection
_db: kuzu.Database | None = None
_conn: kuzu.Connection | None = None

# ============== Tool Schemas ==============

TOOL_SCHEMAS = {
    "create_document": {
        "name": "create_document",
        "description": "Create a document node in the knowledge graph. Use file_path to avoid passing large content through JSON.",
        "input_schema": {
            "type": "object",
            "properties": {
                "uri": {
                    "type": "string",
                    "description": "Unique identifier for the document (e.g., file path)",
                },
                "title": {"type": "string", "description": "Document title"},
                "content": {
                    "type": "string",
                    "description": "Document content (optional if file_path provided)",
                },
                "source": {
                    "type": "string",
                    "description": "Source of the document (optional)",
                },
                "file_path": {
                    "type": "string",
                    "description": "Path to file to read content from (avoids JSON size limits)",
                },
                "entity_status": {
                    "type": "string",
                    "description": "Entity extraction status (default: pending)",
                },
            },
            "required": ["uri", "title"],
        },
    },
    "create_entity": {
        "name": "create_entity",
        "description": "Create an entity node (person, organization, etc.) in the knowledge graph.",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Unique identifier for the entity",
                },
                "name": {"type": "string", "description": "Entity name"},
                "type": {
                    "type": "string",
                    "description": "Entity type (person, organization, technology, location, concept, other)",
                },
                "description": {
                    "type": "string",
                    "description": "Entity description (optional)",
                },
            },
            "required": ["id", "name", "type"],
        },
    },
    "create_concept": {
        "name": "create_concept",
        "description": "Create a concept node in the knowledge graph.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Concept name"},
                "description": {
                    "type": "string",
                    "description": "Concept description (optional)",
                },
            },
            "required": ["name"],
        },
    },
    "link_document_entity": {
        "name": "link_document_entity",
        "description": "Link a document to an entity (MENTIONS relation).",
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_uri": {"type": "string", "description": "Document URI"},
                "entity_id": {"type": "string", "description": "Entity ID"},
                "confidence": {"type": "number", "description": "Confidence score (0.0-1.0), default 1.0"},
            },
            "required": ["doc_uri", "entity_id"],
        },
    },
    "link_document_concept": {
        "name": "link_document_concept",
        "description": "Link a document to a concept (CONTAINS relation).",
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_uri": {"type": "string", "description": "Document URI"},
                "concept_name": {"type": "string", "description": "Concept name"},
                "confidence": {"type": "number", "description": "Confidence score (0.0-1.0), default 1.0"},
            },
            "required": ["doc_uri", "concept_name"],
        },
    },
    "link_entities": {
        "name": "link_entities",
        "description": "Create a relation between two entities.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity1_id": {"type": "string", "description": "First entity ID"},
                "entity2_id": {"type": "string", "description": "Second entity ID"},
                "relation": {
                    "type": "string",
                    "description": "Relation type (e.g., works_for, knows)",
                },
                "confidence": {"type": "number", "description": "Confidence score (0.0-1.0), default 1.0"},
            },
            "required": ["entity1_id", "entity2_id", "relation"],
        },
    },
    "get_document": {
        "name": "get_document",
        "description": "Get a document by URI.",
        "input_schema": {
            "type": "object",
            "properties": {
                "uri": {"type": "string", "description": "Document URI"},
            },
            "required": ["uri"],
        },
    },
    "list_documents": {
        "name": "list_documents",
        "description": "List all documents in the knowledge graph.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of documents to return (default: 10)",
                },
            },
        },
    },
    "search_documents": {
        "name": "search_documents",
        "description": "Search documents by keyword.",
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "Search keyword"},
                "limit": {
                    "type": "integer",
                    "description": "Maximum results (default: 10)",
                },
            },
            "required": ["keyword"],
        },
    },
    "get_related_entities": {
        "name": "get_related_entities",
        "description": "Get entities mentioned in a document.",
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_uri": {"type": "string", "description": "Document URI"},
            },
            "required": ["doc_uri"],
        },
    },
    "get_related_concepts": {
        "name": "get_related_concepts",
        "description": "Get concepts contained in a document.",
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_uri": {"type": "string", "description": "Document URI"},
            },
            "required": ["doc_uri"],
        },
    },
    "query_graph": {
        "name": "query_graph",
        "description": "Execute a Cypher query on the knowledge graph.",
        "input_schema": {
            "type": "object",
            "properties": {
                "cypher": {"type": "string", "description": "Cypher query string"},
            },
            "required": ["cypher"],
        },
    },
    "explore_node": {
        "name": "explore_node",
        "description": "从指定实体出发探索N层邻居和边，支持置信度过滤",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string", "description": "实体ID或名称（支持模糊匹配）"},
                "depth": {"type": "integer", "default": 2, "description": "遍历深度（1-5）"},
                "min_confidence": {"type": "number", "default": 0.0, "description": "最小置信度过滤（0.0-1.0）"},
                "direction": {"type": "string", "default": "both", "enum": ["both", "outgoing", "incoming"], "description": "方向过滤"}
            },
            "required": ["entity_id"],
        },
    },
    "find_path": {
        "name": "find_path",
        "description": "查找两个实体之间的最短路径",
        "input_schema": {
            "type": "object",
            "properties": {
                "from_id": {"type": "string", "description": "起点实体ID或名称"},
                "to_id": {"type": "string", "description": "终点实体ID或名称"},
                "max_depth": {"type": "integer", "default": 5, "description": "最大跳数（1-10）"}
            },
            "required": ["from_id", "to_id"],
        },
    },
    "graph_stats": {
        "name": "graph_stats",
        "description": "获取知识图谱的统计信息，包括节点数、边数、置信度分布、密度等",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    "hub_entities": {
        "name": "hub_entities",
        "description": "查找图中的枢纽实体（按连接数排序）",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 10, "description": "返回数量上限（默认10）"},
                "min_confidence": {"type": "number", "default": 0.0, "description": "最小置信度过滤（0.0-1.0）"}
            },
        },
    },
    "surprising_connections": {
        "name": "surprising_connections",
        "description": "发现意外连接：两个实体之间没有直接边但共享很多共同邻居",
        "input_schema": {
            "type": "object",
            "properties": {
                "min_shared": {"type": "integer", "default": 2, "description": "最小共同邻居数（默认2）"},
                "min_confidence": {"type": "number", "default": 0.0, "description": "最小置信度过滤"},
                "max_entities": {"type": "integer", "default": 200, "description": "最大实体数（默认200，防止O(n²)爆炸）"}
            },
        },
    },
    "graph_changelog": {
        "name": "graph_changelog",
        "description": "获取知识图谱的最近变更日志（按时间倒序）",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 50, "description": "返回数量上限（默认50）"},
                "since": {"type": "string", "description": "ISO 8601 时间戳，仅返回此时间之后的变更"}
            },
        },
    },
    "list_entities": {
        "name": "list_entities",
        "description": "List all entities in the knowledge graph, optionally filtered by type.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Maximum results (default: 100, max: 500)"},
                "entity_type": {"type": "string", "description": "Optional entity type filter (e.g., person, organization)"},
            },
        },
    },
    "list_concepts": {
        "name": "list_concepts",
        "description": "List all concepts in the knowledge graph.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Maximum results (default: 100, max: 500)"},
            },
        },
    },
    "graph_snapshot": {
        "name": "graph_snapshot",
        "description": "Get a full graph snapshot for visualization. Returns nodes and edges across all types.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max entity nodes (default: 200)"},
                "min_confidence": {"type": "number", "description": "Minimum confidence filter (default: 0.0)"},
            },
        },
    },
    "update_entity_status": {
        "name": "update_entity_status",
        "description": "Update Document node's entity completion status (entity_status, processing_at, retry_count).",
        "input_schema": {
            "type": "object",
            "properties": {
                "uri": {"type": "string", "description": "Document URI"},
                "entity_status": {"type": "string", "description": "New status: pending/processing/completed/failed/failed_permanent"},
                "processing_at": {"type": "string", "description": "Processing timestamp (optional)"},
                "retry_count": {"type": "integer", "description": "Retry count (optional)"},
            },
            "required": ["uri", "entity_status"],
        },
    },
}


def get_tool_schemas() -> list[dict]:
    """返回所有工具的 schema 列表（用于 MCP Loader 注册）"""
    return list(TOOL_SCHEMAS.values())


_db_path_failed: bool = False


def get_connection() -> kuzu.Connection:
    """Get or create database connection."""
    global _db, _conn, _db_path_failed
    if _db_path_failed:
        raise RuntimeError("知识库路径解析失败，无法建立连接。请检查 memory.json 中 workspace.path 配置。")
    if _conn is None:
        try:
            db_path = get_db_path()
        except ValueError as e:
            _db_path_failed = True
            logger.error(f"知识库路径解析失败: {e}")
            raise RuntimeError(f"知识库路径解析失败: {e}") from e
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _db = kuzu.Database(str(db_path))
        _conn = kuzu.Connection(_db)
        _init_schema(_conn)
    return _conn


def _init_schema(conn: kuzu.Connection) -> None:
    """Initialize database schema with confidence and timestamps.

    Uses IF NOT EXISTS to preserve existing data on restart.
    Schema migration requires manual DB rebuild (KuzuDB lacks ALTER TABLE).
    """
    # Create node tables with timestamps
    conn.execute("""
        CREATE NODE TABLE IF NOT EXISTS Document (
            uri STRING,
            title STRING,
            content STRING,
            source STRING,
            entity_status STRING,
            processing_at STRING,
            retry_count INT64,
            created_at STRING,
            updated_at STRING,
            PRIMARY KEY (uri)
        )
    """)

    conn.execute("""
        CREATE NODE TABLE IF NOT EXISTS Entity (
            id STRING,
            name STRING,
            type STRING,
            description STRING,
            created_at STRING,
            updated_at STRING,
            PRIMARY KEY (id)
        )
    """)

    conn.execute("""
        CREATE NODE TABLE IF NOT EXISTS Concept (
            name STRING,
            description STRING,
            created_at STRING,
            updated_at STRING,
            PRIMARY KEY (name)
        )
    """)

    # Create relationship tables with confidence + timestamps
    conn.execute("""
        CREATE REL TABLE IF NOT EXISTS MENTIONS (
            FROM Document TO Entity,
            confidence FLOAT,
            created_at STRING
        )
    """)

    conn.execute("""
        CREATE REL TABLE IF NOT EXISTS CONTAINS (
            FROM Document TO Concept,
            confidence FLOAT,
            created_at STRING
        )
    """)

    conn.execute("""
        CREATE REL TABLE IF NOT EXISTS RELATED_TO (
            FROM Entity TO Entity,
            relation STRING,
            confidence FLOAT,
            created_at STRING
        )
    """)

    logger.info("Database schema initialized")


def _infer_confidence(confidence: float | None = None) -> float:
    """Clamp confidence to [0.0, 1.0], defaulting to 1.0 if not provided.

    Confidence levels (guideline for callers):
    - 1.0: User manually created (default for backward compatibility)
    - 0.7-0.9: LLM extracted from documents
    - 0.4-0.6: Agent inferred from context
    - 0.1-0.3: Algorithm discovered (clustering, co-occurrence)
    """
    if confidence is not None:
        return max(0.0, min(1.0, confidence))
    return 1.0  # Default: highest confidence for backward compatibility


def _get_timestamp() -> str:
    """Get current UTC timestamp in ISO 8601 format."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def create_document(
    uri: str, title: str, content: str = "", source: str = "", file_path: str = "", entity_status: str = "pending"
) -> dict[str, Any]:
    """Create a document node in the graph.

    Args:
        uri: Unique identifier for the document (e.g., file path)
        title: Document title
        content: Document content (optional if file_path provided)
        source: Source of the document (optional)
        file_path: Path to file to read content from (optional)
        entity_status: Entity extraction status (default: pending)

    If file_path is provided, content will be read from the file.
    This avoids passing large content through JSON parameters.
    """
    conn = get_connection()

    # If file_path provided, read content from file
    if file_path and not content:
        try:
            from pathlib import Path

            file = Path(file_path)
            if file.exists():
                # Check file extension - docx/pptx/xlsx are binary (ZIP) formats
                suffix = file.suffix.lower()
                if suffix in [".docx", ".pptx", ".xlsx", ".pdf"]:
                    # These are binary formats, don't try to read as text
                    # The content should be parsed by file-parser tool instead
                    logger.info(
                        f"Skipping binary file {file_path}, use file-parser for content"
                    )
                    content = f"[Binary file: {file.name}]"
                else:
                    # Text files can be read directly
                    content = file.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning(f"Failed to read file {file_path}: {e}")

    created_at = _get_timestamp()

    conn.execute(
        """MERGE (d:Document {uri: $uri})
           ON CREATE SET d.title = $title, d.content = $content, d.source = $source,
                         d.entity_status = $entity_status, d.retry_count = 0, d.created_at = $ts
           SET d.updated_at = $ts""",
        {"uri": uri, "title": title, "content": content, "source": source,
         "entity_status": entity_status, "ts": created_at},
    )

    return {"status": "created", "uri": uri, "title": title}


def update_entity_status(uri: str, entity_status: str, processing_at: str | None = None, retry_count: int | None = None) -> dict[str, Any]:
    """Update Document node's entity completion status.

    Args:
        uri: Document URI
        entity_status: Status (pending/processing/completed/failed/failed_permanent)
        processing_at: Processing timestamp (optional)
        retry_count: Retry count (optional)

    Returns:
        {"status": "updated", "uri": ..., "entity_status": ...}
    """
    VALID_STATUSES = {"pending", "processing", "completed", "failed", "failed_permanent"}
    if entity_status not in VALID_STATUSES:
        return {"status": "error", "message": f"Invalid entity_status '{entity_status}', must be one of: {sorted(VALID_STATUSES)}"}

    conn = get_connection()
    ts = _get_timestamp()

    try:
        set_clauses = ["d.entity_status = $status", "d.updated_at = $ts"]
        params: dict[str, Any] = {"uri": uri, "status": entity_status, "ts": ts}

        if processing_at is not None:
            set_clauses.append("d.processing_at = $pat")
            params["pat"] = processing_at

        if retry_count is not None:
            set_clauses.append("d.retry_count = $rc")
            params["rc"] = retry_count

        query = f"MATCH (d:Document {{uri: $uri}}) SET {', '.join(set_clauses)}"
        conn.execute(query, params)
        return {"status": "updated", "uri": uri, "entity_status": entity_status}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def create_entity(
    id: str, name: str, entity_type: str, description: str = ""
) -> dict[str, Any]:
    """Create an entity node in the graph."""
    conn = get_connection()
    ts = _get_timestamp()
    entity_type = entity_type.lower()

    conn.execute(
        "MERGE (e:Entity {id: $id}) ON CREATE SET e.name = $name, e.type = $type, e.description = $description, e.created_at = $ts SET e.type = $type, e.updated_at = $ts",
        {"id": id, "name": name, "type": entity_type, "description": description, "ts": ts},
    )

    return {"status": "created", "id": id, "name": name, "type": entity_type, "created_at": ts, "updated_at": ts}


def create_concept(name: str, description: str = "") -> dict[str, Any]:
    """Create a concept node in the graph."""
    conn = get_connection()
    ts = _get_timestamp()

    conn.execute(
        "MERGE (c:Concept {name: $name}) SET c.description = $description, c.created_at = $ts, c.updated_at = $ts",
        {"name": name, "description": description, "ts": ts},
    )

    return {"status": "created", "name": name, "created_at": ts, "updated_at": ts}


def link_document_entity(doc_uri: str, entity_id: str, confidence: float | None = None) -> dict[str, Any]:
    """Link a document to an entity (MENTIONS relation)."""
    conn = get_connection()
    conf = _infer_confidence(confidence)
    ts = _get_timestamp()

    conn.execute(
        "MATCH (d:Document {uri: $doc_uri}), (e:Entity {id: $entity_id}) MERGE (d)-[r:MENTIONS]->(e) ON CREATE SET r.confidence = $conf, r.created_at = $ts ON MATCH SET r.confidence = $conf",
        {"doc_uri": doc_uri, "entity_id": entity_id, "conf": conf, "ts": ts},
    )

    return {"status": "linked", "document": doc_uri, "entity": entity_id, "confidence": conf, "created_at": ts}


def link_document_concept(doc_uri: str, concept_name: str, confidence: float | None = None) -> dict[str, Any]:
    """Link a document to a concept (CONTAINS relation)."""
    conn = get_connection()
    conf = _infer_confidence(confidence)
    ts = _get_timestamp()

    conn.execute(
        "MATCH (d:Document {uri: $doc_uri}), (c:Concept {name: $concept_name}) MERGE (d)-[r:CONTAINS]->(c) ON CREATE SET r.confidence = $conf, r.created_at = $ts ON MATCH SET r.confidence = $conf",
        {"doc_uri": doc_uri, "concept_name": concept_name, "conf": conf, "ts": ts},
    )

    return {"status": "linked", "document": doc_uri, "concept": concept_name, "confidence": conf, "created_at": ts}


def link_entities(entity1_id: str, entity2_id: str, relation: str, confidence: float | None = None) -> dict[str, Any]:
    """Create a relation between two entities."""
    conn = get_connection()
    conf = _infer_confidence(confidence)
    ts = _get_timestamp()

    conn.execute(
        "MATCH (e1:Entity {id: $e1_id}), (e2:Entity {id: $e2_id}) MERGE (e1)-[r:RELATED_TO {relation: $relation}]->(e2) ON CREATE SET r.confidence = $conf, r.created_at = $ts ON MATCH SET r.confidence = $conf",
        {"e1_id": entity1_id, "e2_id": entity2_id, "relation": relation, "conf": conf, "ts": ts},
    )

    return {
        "status": "linked",
        "entity1": entity1_id,
        "entity2": entity2_id,
        "relation": relation,
        "confidence": conf,
        "created_at": ts,
    }


def _validate_cypher_readonly(query: str) -> bool:
    """Validate that Cypher query is read-only.

    Strips string literals and comments before checking for write keywords.
    Blocks: CREATE, DELETE, SET, REMOVE, MERGE, DROP, FOREACH, LOAD, COPY
    Allows: MATCH, RETURN, WITH, WHERE, ORDER BY, LIMIT, COUNT, SUM, etc.
    """
    import re
    # Strip single-quoted string literals (Cypher uses single quotes)
    cleaned = re.sub(r"'(?:[^'\\]|\\.)*'", "", query)
    # Strip double-quoted string literals
    cleaned = re.sub(r'"(?:[^"\\]|\\.)*"', "", cleaned)
    # Strip line comments (//)
    cleaned = re.sub(r"//[^\n]*", "", cleaned)
    # Strip block comments (/* */)
    cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL)

    blocked_keywords = [
        "CREATE", "DELETE", "REMOVE", "MERGE", "DROP", "FOREACH", "LOAD", "COPY", "SET",
    ]
    cleaned_upper = cleaned.upper()
    for keyword in blocked_keywords:
        if re.search(rf'\b{keyword}\b', cleaned_upper):
            return False
    return True


def query_graph(cypher: str) -> list[dict[str, Any]] | dict[str, Any]:
    """Execute a read-only Cypher query and return results.

    Only read-only queries (MATCH, RETURN, WITH, WHERE, ORDER BY, LIMIT) are allowed.
    Write operations (CREATE, DELETE, SET, REMOVE, MERGE, DROP) are blocked.
    """
    if not _validate_cypher_readonly(cypher):
        return {"error": "Only read-only queries are allowed (MATCH, RETURN, WITH, WHERE, ORDER BY, LIMIT)"}

    conn = get_connection()
    try:
        result = conn.execute(cypher)

        rows = []
        while result.has_next():
            row = result.get_next()
            rows.append(row)

        return rows
    except Exception as e:
        return {"error": str(e)}


def explore_node(entity_id: str, depth: int = 2, min_confidence: float = 0.0, direction: str = "both") -> dict[str, Any]:
    """Explore graph from entity, returning N-layer neighbors.

    Args:
        entity_id: Entity ID or name (supports fuzzy matching)
        depth: Traversal depth (1-5)
        min_confidence: Minimum confidence filter (0.0-1.0)
        direction: "both" | "outgoing" | "incoming"

    Returns:
        {
            "center": {"id": "...", "name": "...", "type": "..."},
            "nodes": [{"id": "...", "name": "...", "type": "...", "distance": 1}],
            "edges": [{"source": "...", "target": "...", "relation": "...", "confidence": 0.9}],
            "stats": {"nodes": N, "edges": M, "max_depth": D}
        }
    """
    conn = get_connection()
    depth = max(1, min(5, depth))

    if direction not in ("both", "outgoing", "incoming"):
        return {"error": f"Invalid direction '{direction}', must be one of: both, outgoing, incoming"}

    # Find center node (fuzzy match by name or exact ID)
    center_result = conn.execute(
        "MATCH (e:Entity) WHERE e.id = $id OR e.name CONTAINS $id RETURN e.id, e.name, e.type LIMIT 1",
        {"id": entity_id}
    )
    center_rows = list(center_result)
    if not center_rows:
        return {"error": f"Entity '{entity_id}' not found"}

    center = {
        "id": center_rows[0][0],
        "name": center_rows[0][1],
        "type": center_rows[0][2]
    }

    # BFS traversal with confidence filter
    # Max 500 nodes to prevent query explosion
    max_nodes = 500
    nodes = []
    edges = []
    visited = {center["id"]}
    frontier = [center["id"]]
    seen_edges = set()

    for d in range(1, depth + 1):
        next_frontier = []
        for node_id in frontier:
            if len(visited) >= max_nodes:
                break
            # Get outgoing neighbors
            if direction in ("both", "outgoing"):
                query = f"""
                    MATCH (n:Entity {{id: $node_id}})-[r]->(neighbor:Entity)
                    WHERE r.confidence >= $min_confidence
                    RETURN n.id, n.name, neighbor.id, neighbor.name, neighbor.type, r.relation, r.confidence
                """
                result = conn.execute(query, {"node_id": node_id, "min_confidence": min_confidence})
                for row in result:
                    if len(visited) >= max_nodes:
                        break
                    neighbor_id = row[2]
                    if neighbor_id not in visited:
                        visited.add(neighbor_id)
                        next_frontier.append(neighbor_id)
                        nodes.append({
                            "id": neighbor_id,
                            "name": row[3],
                            "type": row[4],
                            "distance": d
                        })
                    edge_key = (row[0], row[2], row[5])
                    if edge_key not in seen_edges:
                        seen_edges.add(edge_key)
                        edges.append({
                            "source": row[0],
                            "target": row[2],
                            "relation": row[5],
                            "confidence": row[6]
                        })

            # Get incoming neighbors
            if direction in ("both", "incoming"):
                query = f"""
                    MATCH (neighbor:Entity)-[r]->(n:Entity {{id: $node_id}})
                    WHERE r.confidence >= $min_confidence
                    RETURN neighbor.id, neighbor.name, n.id, n.name, neighbor.type, r.relation, r.confidence
                """
                result = conn.execute(query, {"node_id": node_id, "min_confidence": min_confidence})
                for row in result:
                    if len(visited) >= max_nodes:
                        break
                    neighbor_id = row[0]
                    if neighbor_id not in visited:
                        visited.add(neighbor_id)
                        next_frontier.append(neighbor_id)
                        nodes.append({
                            "id": neighbor_id,
                            "name": row[1],
                            "type": row[4],
                            "distance": d
                        })
                    edge_key = (row[0], row[2], row[5])
                    if edge_key not in seen_edges:
                        seen_edges.add(edge_key)
                        edges.append({
                            "source": row[0],
                            "target": row[2],
                            "relation": row[5],
                            "confidence": row[6]
                        })

        frontier = next_frontier

    return {
        "center": center,
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "nodes": len(nodes),
            "edges": len(edges),
            "max_depth": depth
        }
    }


def find_path(from_id: str, to_id: str, max_depth: int = 5) -> dict[str, Any]:
    """Find shortest path between two entities.

    Args:
        from_id: Source entity ID or name
        to_id: Target entity ID or name
        max_depth: Maximum hops to search (1-10)

    Returns:
        {
            "found": bool,
            "hops": int,
            "path": [{"id": "...", "name": "...", "relation": "...", "confidence": 0.9}]
        }
    """
    conn = get_connection()
    max_depth = max(1, min(10, max_depth))

    # Find source node
    source_result = conn.execute(
        "MATCH (e:Entity) WHERE e.id = $id OR e.name CONTAINS $id RETURN e.id, e.name LIMIT 1",
        {"id": from_id}
    )
    source_rows = list(source_result)
    if not source_rows:
        return {"found": False, "error": f"Source entity '{from_id}' not found"}

    # Find target node
    target_result = conn.execute(
        "MATCH (e:Entity) WHERE e.id = $id OR e.name CONTAINS $id RETURN e.id, e.name LIMIT 1",
        {"id": to_id}
    )
    target_rows = list(target_result)
    if not target_rows:
        return {"found": False, "error": f"Target entity '{to_id}' not found"}

    source_id = source_rows[0][0]
    target_id = target_rows[0][0]

    # Self-loop: path from node to itself is 0 hops
    if source_id == target_id:
        return {"found": True, "hops": 0, "path": [{"id": source_id, "name": source_rows[0][1]}]}

    # Use BFS to find shortest path (KuzuDB doesn't have SHORTESTPATH)
    max_visited = 1000
    visited = {source_id}
    frontier = [(source_id, [source_id])]  # (current_id, path_so_far)

    for _ in range(max_depth):
        next_frontier = []
        for current_id, path in frontier:
            if len(visited) >= max_visited:
                break
            # Find all neighbors
            result = conn.execute(
                """
                MATCH (n:Entity {id: $current_id})-[r]->(neighbor:Entity)
                RETURN neighbor.id, neighbor.name, r.relation, r.confidence
                """,
                {"current_id": current_id}
            )
            for row in result:
                if len(visited) >= max_visited:
                    break
                neighbor_id = row[0]
                if neighbor_id not in visited:
                    new_path = path + [neighbor_id]
                    if neighbor_id == target_id:
                        # Found path!
                        path_result = []
                        for i, node_id in enumerate(new_path):
                            if i == 0:
                                # Get node name
                                node_result = conn.execute(
                                    "MATCH (e {id: $id}) RETURN e.name LIMIT 1",
                                    {"id": node_id}
                                )
                                node_rows = list(node_result)
                                path_result.append({
                                    "id": node_id,
                                    "name": node_rows[0][0] if node_rows else node_id
                                })
                            else:
                                # Get edge info (try both directions)
                                edge_result = conn.execute(
                                    """
                                    MATCH (prev {id: $prev_id})-[r]->(next {id: $next_id})
                                    RETURN r.relation, r.confidence, next.name
                                    """,
                                    {"prev_id": path[i-1], "next_id": node_id}
                                )
                                edge_rows = list(edge_result)
                                if not edge_rows:
                                    edge_result = conn.execute(
                                        """
                                        MATCH (next {id: $next_id})-[r]->(prev {id: $prev_id})
                                        RETURN r.relation, r.confidence, prev.name
                                        """,
                                        {"next_id": node_id, "prev_id": path[i-1]}
                                    )
                                    edge_rows = list(edge_result)
                                if edge_rows:
                                    path_result.append({
                                        "id": node_id,
                                        "name": edge_rows[0][2],
                                        "relation": edge_rows[0][0],
                                        "confidence": edge_rows[0][1]
                                    })
                                else:
                                    # Edge query returned empty — still include node with name fallback
                                    node_result = conn.execute(
                                        "MATCH (e {id: $id}) RETURN e.name LIMIT 1",
                                        {"id": node_id}
                                    )
                                    node_rows = list(node_result)
                                    path_result.append({
                                        "id": node_id,
                                        "name": node_rows[0][0] if node_rows else node_id
                                    })
                        return {
                            "found": True,
                            "hops": len(new_path) - 1,
                            "path": path_result
                        }

                    visited.add(neighbor_id)
                    next_frontier.append((neighbor_id, new_path))

            # Also check incoming edges
            result = conn.execute(
                """
                MATCH (neighbor:Entity)-[r]->(n:Entity {id: $current_id})
                RETURN neighbor.id, neighbor.name, r.relation, r.confidence
                """,
                {"current_id": current_id}
            )
            for row in result:
                if len(visited) >= max_visited:
                    break
                neighbor_id = row[0]
                if neighbor_id not in visited:
                    new_path = path + [neighbor_id]
                    if neighbor_id == target_id:
                        # Found path!
                        path_result = []
                        for i, node_id in enumerate(new_path):
                            if i == 0:
                                node_result = conn.execute(
                                    "MATCH (e {id: $id}) RETURN e.name LIMIT 1",
                                    {"id": node_id}
                                )
                                node_rows = list(node_result)
                                path_result.append({
                                    "id": node_id,
                                    "name": node_rows[0][0] if node_rows else node_id
                                })
                            else:
                                # Try edge in either direction
                                edge_result = conn.execute(
                                    """
                                    MATCH (prev {id: $prev_id})-[r]->(next {id: $next_id})
                                    RETURN r.relation, r.confidence, next.name
                                    """,
                                    {"prev_id": new_path[i-1], "next_id": node_id}
                                )
                                edge_rows = list(edge_result)
                                if not edge_rows:
                                    # Try reverse direction (incoming edge)
                                    edge_result = conn.execute(
                                        """
                                        MATCH (next {id: $next_id})-[r]->(prev {id: $prev_id})
                                        RETURN r.relation, r.confidence, prev.name
                                        """,
                                        {"next_id": node_id, "prev_id": new_path[i-1]}
                                    )
                                    edge_rows = list(edge_result)
                                if edge_rows:
                                    path_result.append({
                                        "id": node_id,
                                        "name": edge_rows[0][2],
                                        "relation": edge_rows[0][0],
                                        "confidence": edge_rows[0][1]
                                    })
                                else:
                                    # Edge query returned empty — still include node with name fallback
                                    node_result = conn.execute(
                                        "MATCH (e {id: $id}) RETURN e.name LIMIT 1",
                                        {"id": node_id}
                                    )
                                    node_rows = list(node_result)
                                    path_result.append({
                                        "id": node_id,
                                        "name": node_rows[0][0] if node_rows else node_id
                                    })
                        return {
                            "found": True,
                            "hops": len(new_path) - 1,
                            "path": path_result
                        }

                    visited.add(neighbor_id)
                    next_frontier.append((neighbor_id, new_path))

        frontier = next_frontier

    return {"found": False, "hops": 0, "path": []}


def graph_stats() -> dict[str, Any]:
    """Get knowledge graph statistics.

    Returns:
        {
            "nodes": {
                "total": int,
                "by_type": {"Entity": N, "Document": N, "Concept": N}
            },
            "edges": {
                "total": int,
                "by_type": {"RELATED_TO": N, "MENTIONS": N, "CONTAINS": N},
                "by_confidence": {"high (0.7-1.0)": N, "medium (0.4-0.6)": N, "low (0.0-0.3)": N}
            },
            "density": float,
            "components": int
        }
    """
    conn = get_connection()

    # Count nodes by type (using entity type field, not table name)
    nodes = {"total": 0, "by_type": {}}

    # Count by table name
    for label in ("Entity", "Document", "Concept"):
        result = conn.execute(f"MATCH (n:{label}) RETURN count(n)")
        rows = list(result)
        count = rows[0][0] if rows else 0
        nodes["total"] += count

    # Count by Entity.type field
    entity_result = conn.execute("MATCH (e:Entity) RETURN e.type, count(e)")
    for row in entity_result:
        entity_type = row[0] or "unknown"
        nodes["by_type"][entity_type] = row[1]

    # Count edges by type and confidence
    edges = {
        "total": 0,
        "by_type": {"RELATED_TO": 0, "MENTIONS": 0, "CONTAINS": 0},
        "by_confidence": {"high (0.7-1.0)": 0, "medium (0.4-0.6)": 0, "low (0.0-0.3)": 0}
    }

    for rel_type in ("RELATED_TO", "MENTIONS", "CONTAINS"):
        result = conn.execute(f"MATCH ()-[r:{rel_type}]->() RETURN count(r)")
        rows = list(result)
        count = rows[0][0] if rows else 0
        edges["total"] += count
        edges["by_type"][rel_type] = count

    # Confidence distribution (separate queries to avoid consumption)
    # High bucket: >= 0.699 (slightly below 0.7 to absorb float precision from 0.7 -> 0.699999988)
    for rel_type in ("RELATED_TO", "MENTIONS", "CONTAINS"):
        # High: >= 0.699 (covers both 0.7 and 0.9 stored as floats)
        high_result = conn.execute(
            f"MATCH ()-[r:{rel_type}]->() WHERE r.confidence >= 0.699 RETURN count(r)"
        )
        high_rows = list(high_result)
        edges["by_confidence"]["high (0.7-1.0)"] += high_rows[0][0] if high_rows else 0

        # Medium: [0.4, 0.699)
        conf_result = conn.execute(
            f"MATCH ()-[r:{rel_type}]->() WHERE r.confidence >= 0.4 AND r.confidence < 0.699 RETURN count(r)"
        )
        conf_rows = list(conf_result)
        edges["by_confidence"]["medium (0.4-0.6)"] += conf_rows[0][0] if conf_rows else 0

        # Low: < 0.4
        low_result = conn.execute(
            f"MATCH ()-[r:{rel_type}]->() WHERE r.confidence < 0.4 RETURN count(r)"
        )
        low_rows = list(low_result)
        edges["by_confidence"]["low (0.0-0.3)"] += low_rows[0][0] if low_rows else 0

    # Graph density: actual_edges / (possible_edges)
    # For directed graph: possible = n * (n - 1)
    n = nodes["total"]
    density = edges["total"] / (n * (n - 1)) if n > 1 else 0.0

    # Connected components (simplified: Entity nodes via RELATED_TO edges)
    # Limited to 500 entities to prevent quadratic query explosion
    components = 0
    entity_result = conn.execute("MATCH (n:Entity) RETURN n.id LIMIT 500")
    entity_ids = [row[0] for row in entity_result]

    visited: set[str] = set()
    max_bfs_nodes = 1000
    for node_id in entity_ids:
        if node_id not in visited:
            # BFS via RELATED_TO edges
            frontier = [node_id]
            while frontier and len(visited) < max_bfs_nodes:
                current = frontier.pop()
                if current in visited:
                    continue
                visited.add(current)
                for neighbor_result in (
                    conn.execute(
                        "MATCH (n {id: $id})-[r:RELATED_TO]->(neighbor) RETURN neighbor.id",
                        {"id": current}
                    ),
                    conn.execute(
                        "MATCH (neighbor)-[r:RELATED_TO]->(n {id: $id}) RETURN neighbor.id",
                        {"id": current}
                    ),
                ):
                    for nrow in neighbor_result:
                        if nrow[0] not in visited:
                            frontier.append(nrow[0])
            components += 1

    return {
        "nodes": nodes,
        "edges": edges,
        "density": round(density, 6),
        "components": components
    }


def hub_entities(limit: int = 10, min_confidence: float = 0.0) -> dict[str, Any]:
    """Find hub entities by connection count (degree centrality).

    Args:
        limit: Maximum number of results (default: 10)
        min_confidence: Minimum confidence filter (0.0-1.0)

    Returns:
        {
            "entities": [
                {"id": "...", "name": "...", "type": "...", "connections": N, "outgoing": N, "incoming": N}
            ]
        }
    """
    conn = get_connection()
    limit = max(1, min(100, limit))

    # Count outgoing and incoming connections per entity
    entities: dict[str, dict] = {}

    # Outgoing
    result = conn.execute(
        """
        MATCH (n:Entity)-[r:RELATED_TO]->(m:Entity)
        WHERE r.confidence >= $min_conf
        RETURN n.id, n.name, n.type, count(r)
        """,
        {"min_conf": min_confidence}
    )
    for row in result:
        eid, name, etype, cnt = row[0], row[1], row[2], row[3]
        if eid not in entities:
            entities[eid] = {"id": eid, "name": name, "type": etype, "outgoing": 0, "incoming": 0, "connections": 0}
        entities[eid]["outgoing"] = cnt
        entities[eid]["connections"] += cnt

    # Incoming
    result = conn.execute(
        """
        MATCH (n:Entity)-[r:RELATED_TO]->(m:Entity)
        WHERE r.confidence >= $min_conf
        RETURN m.id, m.name, m.type, count(r)
        """,
        {"min_conf": min_confidence}
    )
    for row in result:
        eid, name, etype, cnt = row[0], row[1], row[2], row[3]
        if eid not in entities:
            entities[eid] = {"id": eid, "name": name, "type": etype, "outgoing": 0, "incoming": 0, "connections": 0}
        entities[eid]["incoming"] = cnt
        entities[eid]["connections"] += cnt

    # Sort by connections descending
    sorted_entities = sorted(entities.values(), key=lambda e: e["connections"], reverse=True)
    return {"entities": sorted_entities[:limit]}


def surprising_connections(min_shared: int = 2, min_confidence: float = 0.0, max_entities: int = 200) -> dict[str, Any]:
    """Find unexpected connections: two entities share many neighbors but aren't directly linked.

    Algorithm: for each pair of entities (A, B) not directly connected,
    compute shared neighbors and return pairs with |shared| >= min_shared.

    Args:
        min_shared: Minimum shared neighbor count (default: 2)
        min_confidence: Minimum confidence filter for edge filtering
        max_entities: Maximum entities to consider (default: 200, prevents O(n²) explosion)

    Returns:
        {
            "connections": [
                {
                    "entity1": {"id": "...", "name": "...", "type": "..."},
                    "entity2": {"id": "...", "name": "...", "type": "..."},
                    "shared_neighbors": N,
                    "neighbors": [{"id": "...", "name": "...", "relation_to_1": "...", "relation_to_2": "..."}]
                }
            ]
        }
    """
    conn = get_connection()
    min_shared = max(1, min_shared)

    # Get all entities (limited to prevent O(n²) explosion)
    max_entities = max(10, min(500, max_entities))
    entities_result = conn.execute(f"MATCH (e:Entity) RETURN e.id, e.name, e.type LIMIT {max_entities}")
    entities = {row[0]: {"id": row[0], "name": row[1], "type": row[2]} for row in entities_result}

    # Get all neighbors per entity (both directions) with edge relations
    neighbors: dict[str, list[tuple]] = {}
    for eid in entities:
        # Outgoing neighbors
        result = conn.execute(
            """
            MATCH (n {id: $eid})-[r:RELATED_TO]->(m:Entity)
            WHERE r.confidence >= $min_conf
            RETURN m.id, m.name, r.relation
            """,
            {"eid": eid, "min_conf": min_confidence}
        )
        neighbor_set: set[tuple] = {(row[0], row[1], row[2]) for row in result}
        # Incoming neighbors
        result = conn.execute(
            """
            MATCH (m:Entity)-[r:RELATED_TO]->(n {id: $eid})
            WHERE r.confidence >= $min_conf
            RETURN m.id, m.name, r.relation
            """,
            {"eid": eid, "min_conf": min_confidence}
        )
        neighbor_set |= {(row[0], row[1], row[2]) for row in result}
        neighbors[eid] = list(neighbor_set)

    # Find surprising connections
    results = []
    checked: set[tuple] = set()

    for e1_id, e1_neighbors in neighbors.items():
        e1_neighbor_ids = {n[0] for n in e1_neighbors}
        for e2_id, e2_neighbors in neighbors.items():
            if e1_id >= e2_id:
                continue  # Only check each pair once
            if e2_id in e1_neighbor_ids:
                continue  # Already directly connected

            e2_neighbor_ids = {n[0] for n in e2_neighbors}
            shared = e1_neighbor_ids & e2_neighbor_ids
            if len(shared) < min_shared:
                continue

            pair_key = (e1_id, e2_id)
            if pair_key in checked:
                continue
            checked.add(pair_key)

            # Build neighbor details
            neighbor_details = []
            for shared_id in shared:
                rel_to_1 = next((n[2] for n in e1_neighbors if n[0] == shared_id), "")
                rel_to_2 = next((n[2] for n in e2_neighbors if n[0] == shared_id), "")
                # Use name from neighbor tuples (n[1]) instead of entities dict fallback
                shared_name = next((n[1] for n in e1_neighbors if n[0] == shared_id), shared_id)
                neighbor_details.append({
                    "id": shared_id,
                    "name": shared_name,
                    "relation_to_1": rel_to_1,
                    "relation_to_2": rel_to_2
                })

            results.append({
                "entity1": entities[e1_id],
                "entity2": entities[e2_id],
                "shared_neighbors": len(shared),
                "neighbors": neighbor_details
            })

    # Sort by shared count descending
    results.sort(key=lambda x: x["shared_neighbors"], reverse=True)
    return {"connections": results}


def graph_changelog(limit: int = 50, since: str | None = None) -> dict[str, Any]:
    """Get recent graph changes sorted by timestamp.

    Args:
        limit: Maximum number of results (default: 50)
        since: ISO 8601 timestamp, only return changes after this time

    Returns:
        {
            "changes": [
                {
                    "type": "entity_created" | "edge_created",
                    "timestamp": "...",
                    "data": {...}
                }
            ]
        }
    """
    conn = get_connection()
    limit = max(1, min(500, limit))

    changes = []

    # Recent entities - fetch all, sort in Python
    entity_query = "MATCH (e:Entity) RETURN e.id, e.name, e.type, e.created_at"
    if since:
        entity_query = f"MATCH (e:Entity) WHERE e.created_at >= $since RETURN e.id, e.name, e.type, e.created_at"
    params: dict = {"since": since} if since else {}
    entity_result = conn.execute(entity_query, params)
    entity_rows = list(entity_result)
    for row in entity_rows:
        changes.append({
            "type": "entity_created",
            "timestamp": row[3],
            "data": {"id": row[0], "name": row[1], "type": row[2]}
        })

    # Recent edges (RELATED_TO)
    edge_query = "MATCH ()-[r:RELATED_TO]->() RETURN r.created_at, r.relation, r.confidence"
    if since:
        edge_query = f"MATCH ()-[r:RELATED_TO]->() WHERE r.created_at >= $since RETURN r.created_at, r.relation, r.confidence"
    edge_result = conn.execute(edge_query, params)
    for row in list(edge_result):
        changes.append({
            "type": "edge_created",
            "timestamp": row[0],
            "data": {"relation": row[1], "confidence": row[2]}
        })

    # Sort all changes by timestamp descending
    changes.sort(key=lambda c: c["timestamp"] or "", reverse=True)
    return {"changes": changes[:limit]}


def get_document(uri: str) -> dict[str, Any] | None:
    """Get a document by URI."""
    conn = get_connection()
    result = conn.execute(
        "MATCH (d:Document {uri: $uri}) RETURN d.uri, d.title, d.content, d.source, d.created_at",
        {"uri": uri},
    )

    if result.has_next():
        row = result.get_next()
        return {
            "uri": row[0],
            "title": row[1],
            "content": row[2],
            "source": row[3],
            "created_at": row[4],
        }
    return None


def list_documents(limit: int = 10) -> list[dict[str, Any]]:
    """List all documents."""
    conn = get_connection()
    limit = max(1, min(100, int(limit)))
    result = conn.execute(
        f"MATCH (d:Document) RETURN d.uri, d.title, d.source, d.created_at ORDER BY d.created_at DESC LIMIT {limit}"
    )

    docs = []
    while result.has_next():
        row = result.get_next()
        docs.append(
            {"uri": row[0], "title": row[1], "source": row[2], "created_at": row[3]}
        )
    return docs


def list_entities(limit: int = 100, entity_type: str | None = None) -> list[dict[str, Any]]:
    """List all entities, optionally filtered by type.

    Args:
        limit: Maximum results (default: 100, max: 500)
        entity_type: Optional entity type filter (e.g., "person", "organization")

    Returns:
        List of entity dicts with id, name, type, description, created_at
    """
    conn = get_connection()
    limit = max(1, min(500, int(limit)))

    if entity_type:
        result = conn.execute(
            f"MATCH (e:Entity) WHERE e.type = $etype RETURN e.id, e.name, e.type, e.description, e.created_at ORDER BY e.created_at DESC LIMIT {limit}",
            {"etype": entity_type},
        )
    else:
        result = conn.execute(
            f"MATCH (e:Entity) RETURN e.id, e.name, e.type, e.description, e.created_at ORDER BY e.created_at DESC LIMIT {limit}"
        )

    entities = []
    while result.has_next():
        row = result.get_next()
        entities.append({
            "id": row[0], "name": row[1], "type": row[2],
            "description": row[3], "created_at": row[4]
        })
    return entities


def list_concepts(limit: int = 100) -> list[dict[str, Any]]:
    """List all concepts.

    Args:
        limit: Maximum results (default: 100, max: 500)

    Returns:
        List of concept dicts with name, description, created_at
    """
    conn = get_connection()
    limit = max(1, min(500, int(limit)))
    result = conn.execute(
        f"MATCH (c:Concept) RETURN c.name, c.description, c.created_at ORDER BY c.created_at DESC LIMIT {limit}"
    )

    concepts = []
    while result.has_next():
        row = result.get_next()
        concepts.append({"name": row[0], "description": row[1], "created_at": row[2]})
    return concepts


def graph_snapshot(limit: int = 200, min_confidence: float = 0.0) -> dict[str, Any]:
    """Get a full graph snapshot for visualization.

    Returns Entity nodes with RELATED_TO edges, Document nodes connected
    via MENTIONS, and Concept nodes connected via CONTAINS.

    Args:
        limit: Max entity nodes to return (default: 200)
        min_confidence: Minimum confidence filter (0.0-1.0)

    Returns:
        {"nodes": [...], "edges": [...], "stats": {"nodes": N, "edges": M}}
    """
    conn = get_connection()
    min_confidence = max(0.0, min(1.0, float(min_confidence)))
    nodes = []
    edges = []
    node_ids = set()

    # 1. Entity nodes
    entity_result = conn.execute(
        f"MATCH (e:Entity) RETURN e.id, e.name, e.type, e.description LIMIT {limit}"
    )
    while entity_result.has_next():
        row = entity_result.get_next()
        node_id = f"entity:{row[0]}"
        nodes.append({
            "id": node_id, "label": row[1], "nodeType": "Entity",
            "entityType": (row[2] or "").lower(), "description": row[3] or ""
        })
        node_ids.add(node_id)

    # 2. RELATED_TO edges (Entity -> Entity)
    rel_result = conn.execute(
        "MATCH (e1:Entity)-[r:RELATED_TO]->(e2:Entity) "
        "WHERE r.confidence >= $min_conf "
        "RETURN e1.id, e2.id, r.relation, r.confidence",
        {"min_conf": min_confidence}
    )
    while rel_result.has_next():
        row = rel_result.get_next()
        src = f"entity:{row[0]}"
        tgt = f"entity:{row[1]}"
        if src in node_ids and tgt in node_ids:
            edges.append({
                "source": src, "target": tgt,
                "relation": row[2], "confidence": row[3],
                "edgeType": "RELATED_TO"
            })

    # 3. Document nodes (connected to entities via MENTIONS)
    doc_result = conn.execute(
        "MATCH (d:Document)-[:MENTIONS]->(e:Entity) "
        "RETURN DISTINCT d.uri, d.title, d.source, d.content"
    )
    while doc_result.has_next():
        row = doc_result.get_next()
        doc_id = f"doc:{row[0]}"
        # Truncate content for description (avoid sending full document text)
        content = row[3] or ""
        description = content[:200] + "..." if len(content) > 200 else content
        nodes.append({
            "id": doc_id, "label": row[1] or row[0], "nodeType": "Document",
            "uri": row[0], "source": row[2] or "", "description": description
        })
        node_ids.add(doc_id)

    # 4. MENTIONS edges (Document -> Entity)
    mentions_result = conn.execute(
        "MATCH (d:Document)-[r:MENTIONS]->(e:Entity) "
        "WHERE r.confidence >= $min_conf "
        "RETURN d.uri, e.id, r.confidence",
        {"min_conf": min_confidence}
    )
    while mentions_result.has_next():
        row = mentions_result.get_next()
        src = f"doc:{row[0]}"
        tgt = f"entity:{row[1]}"
        if src in node_ids and tgt in node_ids:
            edges.append({
                "source": src, "target": tgt,
                "confidence": row[2], "edgeType": "MENTIONS"
            })

    # 5. Concept nodes (connected to documents via CONTAINS)
    concept_result = conn.execute(
        "MATCH (d:Document)-[:CONTAINS]->(c:Concept) "
        "RETURN DISTINCT c.name, c.description"
    )
    while concept_result.has_next():
        row = concept_result.get_next()
        concept_id = f"concept:{row[0]}"
        nodes.append({
            "id": concept_id, "label": row[0], "nodeType": "Concept",
            "description": row[1] or ""
        })
        node_ids.add(concept_id)

    # 6. CONTAINS edges (Document -> Concept)
    contains_result = conn.execute(
        "MATCH (d:Document)-[r:CONTAINS]->(c:Concept) "
        "WHERE r.confidence >= $min_conf "
        "RETURN d.uri, c.name, r.confidence",
        {"min_conf": min_confidence}
    )
    while contains_result.has_next():
        row = contains_result.get_next()
        src = f"doc:{row[0]}"
        tgt = f"concept:{row[1]}"
        if src in node_ids and tgt in node_ids:
            edges.append({
                "source": src, "target": tgt,
                "confidence": row[2], "edgeType": "CONTAINS"
            })

    return {
        "nodes": nodes, "edges": edges,
        "stats": {"nodes": len(nodes), "edges": len(edges)}
    }


def search_documents(keyword: str, limit: int = 10) -> list[dict[str, Any]]:
    """Search documents by keyword in title or content."""
    conn = get_connection()
    limit = max(1, min(100, int(limit)))
    result = conn.execute(
        f"MATCH (d:Document) WHERE d.title CONTAINS $keyword OR d.content CONTAINS $keyword RETURN d.uri, d.title, d.source LIMIT {limit}",
        {"keyword": keyword},
    )

    docs = []
    while result.has_next():
        row = result.get_next()
        docs.append({"uri": row[0], "title": row[1], "source": row[2]})
    return docs


def get_related_entities(doc_uri: str) -> list[dict[str, Any]]:
    """Get entities mentioned in a document."""
    conn = get_connection()
    result = conn.execute(
        "MATCH (d:Document {uri: $uri})-[:MENTIONS]->(e:Entity) RETURN e.id, e.name, e.type, e.description",
        {"uri": doc_uri},
    )

    entities = []
    while result.has_next():
        row = result.get_next()
        entities.append(
            {"id": row[0], "name": row[1], "type": row[2], "description": row[3]}
        )
    return entities


def get_related_concepts(doc_uri: str) -> list[dict[str, Any]]:
    """Get concepts contained in a document."""
    conn = get_connection()
    result = conn.execute(
        "MATCH (d:Document {uri: $uri})-[:CONTAINS]->(c:Concept) RETURN c.name, c.description",
        {"uri": doc_uri},
    )

    concepts = []
    while result.has_next():
        row = result.get_next()
        concepts.append({"name": row[0], "description": row[1]})
    return concepts


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List available tools."""
    return [
        Tool(
            name="create_document",
            description="Create a document node in the knowledge graph. Use file_path to avoid passing large content through JSON.",
            inputSchema={
                "type": "object",
                "properties": {
                    "uri": {
                        "type": "string",
                        "description": "Unique identifier for the document (e.g., file path)",
                    },
                    "title": {"type": "string", "description": "Document title"},
                    "content": {
                        "type": "string",
                        "description": "Document content (optional if file_path provided)",
                    },
                    "source": {
                        "type": "string",
                        "description": "Source of the document (optional)",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "Path to file to read content from (avoids JSON size limits)",
                    },
                    "entity_status": {
                        "type": "string",
                        "description": "Entity extraction status (default: pending)",
                    },
                },
                "required": ["uri", "title"],
            },
        ),
        Tool(
            name="create_entity",
            description="Create an entity node (person, organization, etc.) in the knowledge graph.",
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Unique identifier for the entity",
                    },
                    "name": {"type": "string", "description": "Entity name"},
                    "type": {
                        "type": "string",
                        "description": "Entity type (person, organization, technology, location, concept, other)",
                    },
                    "description": {
                        "type": "string",
                        "description": "Entity description (optional)",
                    },
                },
                "required": ["id", "name", "type"],
            },
        ),
        Tool(
            name="create_concept",
            description="Create a concept node in the knowledge graph.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Concept name"},
                    "description": {
                        "type": "string",
                        "description": "Concept description (optional)",
                    },
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="link_document_entity",
            description="Link a document to an entity (MENTIONS relation).",
            inputSchema={
                "type": "object",
                "properties": {
                    "doc_uri": {"type": "string", "description": "Document URI"},
                    "entity_id": {"type": "string", "description": "Entity ID"},
                    "confidence": {"type": "number", "description": "Confidence score (0.0-1.0), default 1.0"},
                },
                "required": ["doc_uri", "entity_id"],
            },
        ),
        Tool(
            name="link_document_concept",
            description="Link a document to a concept (CONTAINS relation).",
            inputSchema={
                "type": "object",
                "properties": {
                    "doc_uri": {"type": "string", "description": "Document URI"},
                    "concept_name": {"type": "string", "description": "Concept name"},
                    "confidence": {"type": "number", "description": "Confidence score (0.0-1.0), default 1.0"},
                },
                "required": ["doc_uri", "concept_name"],
            },
        ),
        Tool(
            name="link_entities",
            description="Create a relation between two entities.",
            inputSchema={
                "type": "object",
                "properties": {
                    "entity1_id": {"type": "string", "description": "First entity ID"},
                    "entity2_id": {"type": "string", "description": "Second entity ID"},
                    "relation": {
                        "type": "string",
                        "description": "Relation type (e.g., works_for, knows)",
                    },
                    "confidence": {"type": "number", "description": "Confidence score (0.0-1.0), default 1.0"},
                },
                "required": ["entity1_id", "entity2_id", "relation"],
            },
        ),
        Tool(
            name="get_document",
            description="Get a document by URI.",
            inputSchema={
                "type": "object",
                "properties": {
                    "uri": {"type": "string", "description": "Document URI"},
                },
                "required": ["uri"],
            },
        ),
        Tool(
            name="list_documents",
            description="List all documents in the knowledge graph.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of documents to return (default: 10)",
                    },
                },
            },
        ),
        Tool(
            name="search_documents",
            description="Search documents by keyword.",
            inputSchema={
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "Search keyword"},
                    "limit": {
                        "type": "integer",
                        "description": "Maximum results (default: 10)",
                    },
                },
                "required": ["keyword"],
            },
        ),
        Tool(
            name="get_related_entities",
            description="Get entities mentioned in a document.",
            inputSchema={
                "type": "object",
                "properties": {
                    "doc_uri": {"type": "string", "description": "Document URI"},
                },
                "required": ["doc_uri"],
            },
        ),
        Tool(
            name="get_related_concepts",
            description="Get concepts contained in a document.",
            inputSchema={
                "type": "object",
                "properties": {
                    "doc_uri": {"type": "string", "description": "Document URI"},
                },
                "required": ["doc_uri"],
            },
        ),
        Tool(
            name="query_graph",
            description="Execute a Cypher query on the knowledge graph.",
            inputSchema={
                "type": "object",
                "properties": {
                    "cypher": {"type": "string", "description": "Cypher query string"},
                },
                "required": ["cypher"],
            },
        ),
        Tool(
            name="explore_node",
            description="Explore N-layer neighbors from an entity via BFS.",
            inputSchema={
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string", "description": "Entity ID or name (supports fuzzy match)"},
                    "depth": {"type": "integer", "description": "Traversal depth 1-5 (default: 2)"},
                    "min_confidence": {"type": "number", "description": "Minimum confidence filter (default: 0.0)"},
                    "direction": {"type": "string", "description": "Direction: both, outgoing, incoming (default: both)"},
                },
                "required": ["entity_id"],
            },
        ),
        Tool(
            name="find_path",
            description="Find shortest path between two entities.",
            inputSchema={
                "type": "object",
                "properties": {
                    "from_id": {"type": "string", "description": "Source entity ID or name"},
                    "to_id": {"type": "string", "description": "Target entity ID or name"},
                    "max_depth": {"type": "integer", "description": "Maximum hops 1-10 (default: 5)"},
                },
                "required": ["from_id", "to_id"],
            },
        ),
        Tool(
            name="graph_stats",
            description="Get knowledge graph statistics including node/edge counts, confidence distribution, density.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="hub_entities",
            description="Find hub entities by connection count (degree centrality).",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Maximum results (default: 10)"},
                    "min_confidence": {"type": "number", "description": "Minimum confidence filter (default: 0.0)"},
                },
            },
        ),
        Tool(
            name="surprising_connections",
            description="Find unexpected connections: two entities share many neighbors but aren't directly linked.",
            inputSchema={
                "type": "object",
                "properties": {
                    "min_shared": {"type": "integer", "description": "Minimum shared neighbor count (default: 2)"},
                    "min_confidence": {"type": "number", "description": "Minimum confidence filter (default: 0.0)"},
                    "max_entities": {"type": "integer", "description": "Maximum entities to consider (default: 200)"},
                },
            },
        ),
        Tool(
            name="graph_changelog",
            description="Get recent graph changes sorted by timestamp.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Maximum results (default: 50)"},
                    "since": {"type": "string", "description": "ISO 8601 timestamp to filter changes after this time"},
                },
            },
        ),
        Tool(
            name="list_entities",
            description="List all entities in the knowledge graph, optionally filtered by type.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Maximum results (default: 100, max: 500)"},
                    "entity_type": {"type": "string", "description": "Optional entity type filter"},
                },
            },
        ),
        Tool(
            name="list_concepts",
            description="List all concepts in the knowledge graph.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Maximum results (default: 100, max: 500)"},
                },
            },
        ),
        Tool(
            name="graph_snapshot",
            description="Get a full graph snapshot for visualization.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max entity nodes (default: 200)"},
                    "min_confidence": {"type": "number", "description": "Minimum confidence filter (default: 0.0)"},
                },
            },
        ),
        Tool(
            name="update_entity_status",
            description="Update Document node's entity completion status.",
            inputSchema={
                "type": "object",
                "properties": {
                    "uri": {"type": "string", "description": "Document URI"},
                    "entity_status": {"type": "string", "description": "Status: pending/processing/completed/failed/failed_permanent"},
                    "processing_at": {"type": "string", "description": "Processing timestamp (optional)"},
                    "retry_count": {"type": "integer", "description": "Retry count (optional)"},
                },
                "required": ["uri", "entity_status"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Handle tool calls."""
    try:
        result: Any = None

        if name == "create_document":
            result = create_document(
                uri=arguments["uri"],
                title=arguments["title"],
                content=arguments.get("content", ""),
                source=arguments.get("source", ""),
                file_path=arguments.get("file_path", ""),
                entity_status=arguments.get("entity_status", "pending"),
            )
        elif name == "create_entity":
            result = create_entity(
                id=arguments["id"],
                name=arguments["name"],
                entity_type=arguments["type"],
                description=arguments.get("description", ""),
            )
        elif name == "create_concept":
            result = create_concept(
                name=arguments["name"], description=arguments.get("description", "")
            )
        elif name == "link_document_entity":
            result = link_document_entity(
                doc_uri=arguments["doc_uri"], entity_id=arguments["entity_id"],
                confidence=arguments.get("confidence"),
            )
        elif name == "link_document_concept":
            result = link_document_concept(
                doc_uri=arguments["doc_uri"], concept_name=arguments["concept_name"],
                confidence=arguments.get("confidence"),
            )
        elif name == "link_entities":
            result = link_entities(
                entity1_id=arguments["entity1_id"],
                entity2_id=arguments["entity2_id"],
                relation=arguments["relation"],
                confidence=arguments.get("confidence"),
            )
        elif name == "get_document":
            result = get_document(uri=arguments["uri"])
        elif name == "list_documents":
            result = list_documents(limit=arguments.get("limit", 10))
        elif name == "search_documents":
            result = search_documents(
                keyword=arguments["keyword"], limit=arguments.get("limit", 10)
            )
        elif name == "get_related_entities":
            result = get_related_entities(doc_uri=arguments["doc_uri"])
        elif name == "get_related_concepts":
            result = get_related_concepts(doc_uri=arguments["doc_uri"])
        elif name == "query_graph":
            result = query_graph(cypher=arguments["cypher"])
        elif name == "explore_node":
            result = explore_node(
                entity_id=arguments["entity_id"],
                depth=arguments.get("depth", 2),
                min_confidence=arguments.get("min_confidence", 0.0),
                direction=arguments.get("direction", "both"),
            )
        elif name == "find_path":
            result = find_path(
                from_id=arguments["from_id"],
                to_id=arguments["to_id"],
                max_depth=arguments.get("max_depth", 5),
            )
        elif name == "graph_stats":
            result = graph_stats()
        elif name == "hub_entities":
            result = hub_entities(
                limit=arguments.get("limit", 10),
                min_confidence=arguments.get("min_confidence", 0.0)
            )
        elif name == "surprising_connections":
            result = surprising_connections(
                min_shared=arguments.get("min_shared", 2),
                min_confidence=arguments.get("min_confidence", 0.0),
                max_entities=arguments.get("max_entities", 200),
            )
        elif name == "graph_changelog":
            result = graph_changelog(
                limit=arguments.get("limit", 50),
                since=arguments.get("since")
            )
        elif name == "list_entities":
            result = list_entities(
                limit=arguments.get("limit", 100),
                entity_type=arguments.get("entity_type"),
            )
        elif name == "list_concepts":
            result = list_concepts(limit=arguments.get("limit", 100))
        elif name == "graph_snapshot":
            result = graph_snapshot(
                limit=arguments.get("limit", 200),
                min_confidence=arguments.get("min_confidence", 0.0),
            )
        elif name == "update_entity_status":
            result = update_entity_status(
                uri=arguments["uri"],
                entity_status=arguments["entity_status"],
                processing_at=arguments.get("processing_at"),
                retry_count=arguments.get("retry_count"),
            )
        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

    except Exception as e:
        logger.exception(f"Error executing tool {name}: {e}")
        return [TextContent(type="text", text=f"Error: {e}")]


async def run_server():
    """Run the MCP server."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


def main():
    """Main entry point."""
    asyncio.run(run_server())


if __name__ == "__main__":
    main()
