"""
Niu Vector Store MCP Server

Provides semantic search capabilities using vector embeddings.
Uses shared embedding service (http://127.0.0.1:9877) for embeddings.
"""

import asyncio
import json
import os
import sqlite3
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any

import numpy as np
from loguru import logger
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# Initialize MCP server
server = Server("niu-vector-store")

# Shared embedding service URL
EMBEDDING_SERVICE_URL = os.environ.get("EMBEDDING_SERVICE_URL", "http://127.0.0.1:9877")

# ============== Tool Schemas ==============

TOOL_SCHEMAS = {
    "add_document": {
        "name": "add_document",
        "description": "Add a document to the vector store for semantic search. Use file_path to avoid passing large content through JSON.",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Unique document ID"},
                "content": {
                    "type": "string",
                    "description": "Document content (optional if file_path provided)",
                },
                "metadata": {"type": "object", "description": "Optional metadata"},
                "file_path": {
                    "type": "string",
                    "description": "Path to file to read content from (avoids JSON size limits)",
                },
            },
            "required": ["id"],
        },
    },
    "search_documents": {
        "name": "search_documents",
        "description": 'Search for similar documents using semantic search. Use filter to narrow results by metadata, e.g. filter={"type": "event", "status": "pending"}.',
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "limit": {
                    "type": "integer",
                    "description": "Max results (default: 5)",
                },
                "filter": {
                    "type": "object",
                    "description": 'Optional metadata filter, e.g. {"type": "event", "status": "pending"}',
                },
            },
            "required": ["query"],
        },
    },
    "get_document": {
        "name": "get_document",
        "description": "Get a document by ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Document ID"},
            },
            "required": ["id"],
        },
    },
    "delete_document": {
        "name": "delete_document",
        "description": 'Delete documents by ID, query (semantic search), or metadata filter. Use filter to delete by type/status, e.g. filter={"type": "event", "status": "cancelled"}.',
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_id": {
                    "type": "string",
                    "description": "Document ID to delete (exact match)",
                },
                "query": {
                    "type": "string",
                    "description": "Delete documents matching content (semantic search, similarity > 0.7)",
                },
                "filter": {
                    "type": "object",
                    "description": 'Delete documents matching metadata filter, e.g. {"type": "event", "status": "cancelled"}',
                },
            },
        },
    },
    "list_documents": {
        "name": "list_documents",
        "description": "List all documents in the vector store, optionally filtered by metadata.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max results (default: 10)",
                },
                "offset": {
                    "type": "integer",
                    "description": "Offset for pagination (default: 0)",
                },
                "filter": {
                    "type": "object",
                    "description": 'Optional metadata filter, e.g. {"type": "l2"}',
                },
            },
        },
    },
    "count_documents": {
        "name": "count_documents",
        "description": "Count total documents in the vector store.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
}


def get_tool_schemas() -> list[dict]:
    """返回所有工具的 schema 列表（用于 MCP Loader 注册）"""
    return list(TOOL_SCHEMAS.values())


def call_embedding_service(endpoint: str, data: dict) -> dict | None:
    """Call the shared embedding service."""
    try:
        url = f"{EMBEDDING_SERVICE_URL}{endpoint}"
        body = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as e:
        logger.warning(f"Embedding service unavailable: {e}")
        return None
    except Exception as e:
        logger.warning(f"Failed to call embedding service: {e}")
        return None


def get_embedding(text: str) -> list[float] | None:
    """Get embedding for text using shared embedding service."""
    result = call_embedding_service("/encode", {"text": text})
    if result and "vector" in result:
        return result["vector"]
    return None


def get_db_path() -> Path:
    """Get database path."""
    # Priority: NIU_DB_PATH > WORKSPACE_PATH > user home > current dir
    if "NIU_DB_PATH" in os.environ:
        return Path(os.environ["NIU_DB_PATH"])

    if "WORKSPACE_PATH" in os.environ:
        workspace = Path(os.environ["WORKSPACE_PATH"])
        return workspace / "vectors.db"

    try:
        return Path.home() / ".niu" / "vectors.db"
    except RuntimeError:
        return Path("vectors.db")


# Global connection
_conn: sqlite3.Connection | None = None


def get_connection() -> sqlite3.Connection:
    """Get or create database connection."""
    global _conn
    if _conn is None:
        db_path = get_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(db_path))
        _init_schema(_conn)
    return _conn


def _init_schema(conn: sqlite3.Connection) -> None:
    """Initialize database schema."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            embedding BLOB,
            metadata TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_doc_id ON documents(id)")
    conn.commit()
    logger.info("Vector store schema initialized")


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Calculate cosine similarity between two vectors."""
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


def add_document(
    doc_id: str,
    content: str = "",
    metadata: dict[str, Any] | None = None,
    file_path: str = "",
) -> dict[str, Any]:
    """Add a document to the vector store.

    Args:
        doc_id: Unique document ID
        content: Document content (optional if file_path provided)
        metadata: Optional metadata dict
        file_path: Path to file to read content from (avoids JSON size limits)
    """
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
                    logger.info(
                        f"Skipping binary file {file_path}, use file-parser for content"
                    )
                    content = f"[Binary file: {file.name}]"
                else:
                    content = file.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning(f"Failed to read file {file_path}: {e}")

    if not content:
        return {"status": "error", "message": "No content provided"}

    conn = get_connection()

    # Get embedding
    embedding = get_embedding(content)
    embedding_blob = None
    if embedding:
        embedding_blob = np.array(embedding, dtype=np.float32).tobytes()

    # Store document
    metadata_json = json.dumps(metadata) if metadata else "{}"
    conn.execute(
        "INSERT OR REPLACE INTO documents (id, content, embedding, metadata) VALUES (?, ?, ?, ?)",
        (doc_id, content, embedding_blob, metadata_json),
    )
    conn.commit()

    return {"status": "added", "id": doc_id, "has_embedding": embedding is not None}


def search_documents(
    query: str, limit: int = 5, filter: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Search for similar documents.

    Args:
        query: Search query
        limit: Max results (default: 5)
        filter: Optional metadata filter, e.g. {"type": "event", "status": "pending"}
    """
    conn = get_connection()

    # Get query embedding
    query_embedding = get_embedding(query)

    # Build base query
    base_query = "SELECT id, content, embedding, metadata FROM documents WHERE embedding IS NOT NULL"

    # Get all documents with embeddings
    cursor = conn.execute(base_query)
    docs = cursor.fetchall()

    if not query_embedding or not docs:
        # Fallback to simple text search
        cursor = conn.execute(
            "SELECT id, content, metadata FROM documents WHERE content LIKE ? LIMIT ?",
            (f"%{query}%", limit),
        )
        results = []
        for row in cursor.fetchall():
            metadata = json.loads(row[2]) if row[2] else {}
            # Apply filter if provided
            if filter and not _matches_filter(metadata, filter):
                continue
            results.append(
                {
                    "id": row[0],
                    "content": row[1][:500],  # Truncate for display
                    "metadata": metadata,
                    "score": 0.5,  # Placeholder score
                }
            )
            if len(results) >= limit:
                break
        return results

    # Vector similarity search
    query_vec = np.array(query_embedding, dtype=np.float32)

    scored_docs = []
    for doc_id, content, embedding_blob, metadata_json in docs:
        if embedding_blob:
            metadata = json.loads(metadata_json) if metadata_json else {}
            # Apply filter if provided
            if filter and not _matches_filter(metadata, filter):
                continue
            doc_vec = np.frombuffer(embedding_blob, dtype=np.float32)
            score = cosine_similarity(query_vec, doc_vec)
            scored_docs.append((doc_id, content, metadata_json, score))

    # Sort by score descending
    scored_docs.sort(key=lambda x: x[3], reverse=True)

    results = []
    for doc_id, content, metadata_json, score in scored_docs[:limit]:
        results.append(
            {
                "id": doc_id,
                "content": content[:500],
                "metadata": json.loads(metadata_json) if metadata_json else {},
                "score": round(score, 4),
            }
        )

    return results


def _matches_filter(metadata: dict[str, Any], filter: dict[str, Any]) -> bool:
    """Check if metadata matches the filter criteria.

    All keys in filter must match the corresponding values in metadata.
    """
    for key, value in filter.items():
        if key not in metadata:
            return False
        if metadata[key] != value:
            return False
    return True


def get_document(doc_id: str) -> dict[str, Any] | None:
    """Get a document by ID."""
    conn = get_connection()
    cursor = conn.execute(
        "SELECT id, content, metadata FROM documents WHERE id = ?", (doc_id,)
    )
    row = cursor.fetchone()
    if row:
        return {
            "id": row[0],
            "content": row[1],
            "metadata": json.loads(row[2]) if row[2] else {},
        }
    return None


def delete_document(
    doc_id: str | None = None,
    query: str | None = None,
    filter: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Delete documents by ID, query, or filter.

    Args:
        doc_id: Delete by document ID (exact match)
        query: Delete documents matching content (semantic search)
        filter: Delete documents matching metadata filter, e.g. {"type": "event", "status": "cancelled"}

    Returns:
        Number of deleted documents and their IDs
    """
    conn = get_connection()
    deleted_ids = []

    if doc_id:
        # 按 ID 删除
        cursor = conn.execute("SELECT id FROM documents WHERE id = ?", (doc_id,))
        rows = cursor.fetchall()
        deleted_ids = [row[0] for row in rows]
        conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    elif query:
        # 按内容语义搜索删除
        query_embedding = get_embedding(query)
        if query_embedding:
            query_vec = np.array(query_embedding, dtype=np.float32)
            cursor = conn.execute(
                "SELECT id, content, embedding, metadata FROM documents WHERE embedding IS NOT NULL"
            )
            for row in cursor.fetchall():
                doc_id, content, embedding_blob, metadata_json = row
                if embedding_blob:
                    # 应用 filter 过滤
                    if filter:
                        metadata = json.loads(metadata_json) if metadata_json else {}
                        if not _matches_filter(metadata, filter):
                            continue
                    doc_vec = np.frombuffer(embedding_blob, dtype=np.float32)
                    score = cosine_similarity(query_vec, doc_vec)
                    if score > 0.7:  # 相似度阈值
                        deleted_ids.append(doc_id)
            # 执行删除
            if deleted_ids:
                placeholders = ",".join("?" * len(deleted_ids))
                conn.execute(
                    f"DELETE FROM documents WHERE id IN ({placeholders})", deleted_ids
                )
    elif filter:
        # 按 metadata 过滤删除
        cursor = conn.execute("SELECT id, metadata FROM documents")
        for row in cursor.fetchall():
            doc_id, metadata_json = row
            metadata = json.loads(metadata_json) if metadata_json else {}
            if _matches_filter(metadata, filter):
                deleted_ids.append(doc_id)
        if deleted_ids:
            placeholders = ",".join("?" * len(deleted_ids))
            conn.execute(
                f"DELETE FROM documents WHERE id IN ({placeholders})", deleted_ids
            )

    conn.commit()
    return {"deleted_count": len(deleted_ids), "deleted_ids": deleted_ids}


def list_documents(
    limit: int = 10, offset: int = 0, filter: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """List all documents, optionally filtered by metadata.

    Args:
        limit: Max results (default: 10)
        offset: Offset for pagination
        filter: Optional metadata filter, e.g. {"type": "l2"}
    """
    conn = get_connection()
    cursor = conn.execute(
        "SELECT id, content, metadata FROM documents LIMIT ? OFFSET ?", (limit, offset)
    )
    results = []
    for row in cursor.fetchall():
        metadata = json.loads(row[2]) if row[2] else {}
        # Apply filter if provided
        if filter and not _matches_filter(metadata, filter):
            continue
        results.append(
            {
                "id": row[0],
                "content": row[1][:500],  # Truncate for display
                "metadata": metadata,
            }
        )
    return results


def count_documents() -> int:
    """Count total documents."""
    conn = get_connection()
    cursor = conn.execute("SELECT COUNT(*) FROM documents")
    return cursor.fetchone()[0]


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List available tools."""
    return [
        Tool(
            name="add_document",
            description="Add a document to the vector store for semantic search. Use file_path to avoid passing large content through JSON.",
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Unique document ID"},
                    "content": {
                        "type": "string",
                        "description": "Document content (optional if file_path provided)",
                    },
                    "metadata": {"type": "object", "description": "Optional metadata"},
                    "file_path": {
                        "type": "string",
                        "description": "Path to file to read content from (avoids JSON size limits)",
                    },
                },
                "required": ["id"],
            },
        ),
        Tool(
            name="search_documents",
            description='Search for similar documents using semantic search. Use filter to narrow results by metadata, e.g. filter={"type": "event", "status": "pending"}.',
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default: 5)",
                    },
                    "filter": {
                        "type": "object",
                        "description": 'Optional metadata filter, e.g. {"type": "event", "status": "pending"}',
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="get_document",
            description="Get a document by ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Document ID"},
                },
                "required": ["id"],
            },
        ),
        Tool(
            name="delete_document",
            description='Delete documents by ID, query (semantic search), or metadata filter. Use filter to delete by type/status, e.g. filter={"type": "event", "status": "cancelled"}.',
            inputSchema={
                "type": "object",
                "properties": {
                    "doc_id": {
                        "type": "string",
                        "description": "Document ID to delete (exact match)",
                    },
                    "query": {
                        "type": "string",
                        "description": "Delete documents matching content (semantic search, similarity > 0.7)",
                    },
                    "filter": {
                        "type": "object",
                        "description": 'Delete documents matching metadata filter, e.g. {"type": "event", "status": "cancelled"}',
                    },
                },
            },
        ),
        Tool(
            name="list_documents",
            description="List all documents in the vector store, optionally filtered by metadata.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default: 10)",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Offset for pagination (default: 0)",
                    },
                    "filter": {
                        "type": "object",
                        "description": 'Optional metadata filter, e.g. {"type": "l2"}',
                    },
                },
            },
        ),
        Tool(
            name="count_documents",
            description="Count total documents in the vector store.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Handle tool calls."""
    try:
        result: Any = None

        if name == "add_document":
            result = add_document(
                doc_id=arguments["id"],
                content=arguments.get("content", ""),
                metadata=arguments.get("metadata"),
                file_path=arguments.get("file_path", ""),
            )
        elif name == "search_documents":
            result = search_documents(
                query=arguments["query"],
                limit=arguments.get("limit", 5),
                filter=arguments.get("filter"),
            )
        elif name == "get_document":
            result = get_document(doc_id=arguments["id"])
        elif name == "delete_document":
            result = delete_document(
                doc_id=arguments.get("doc_id"),
                query=arguments.get("query"),
                filter=arguments.get("filter"),
            )
        elif name == "list_documents":
            result = list_documents(
                limit=arguments.get("limit", 10),
                offset=arguments.get("offset", 0),
                filter=arguments.get("filter"),
            )
        elif name == "count_documents":
            result = {"count": count_documents()}
        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

    except Exception as e:
        logger.exception(f"Error executing tool {name}: {e}")
        return [TextContent(type="text", text=f"Error: {e}")]


async def run_server():
    """Run the MCP server."""
    # No need to preload embedding model - using shared embedding service
    logger.info("Vector store starting (using shared embedding service)")

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


def main():
    """Main entry point."""
    asyncio.run(run_server())


if __name__ == "__main__":
    main()
