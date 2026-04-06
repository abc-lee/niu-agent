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
    """Get database path from environment or default location."""
    # Priority: NIU_DB_PATH > WORKSPACE_PATH > user home > current dir
    if "NIU_DB_PATH" in os.environ:
        return Path(os.environ["NIU_DB_PATH"])

    if "WORKSPACE_PATH" in os.environ:
        workspace = Path(os.environ["WORKSPACE_PATH"])
        return workspace / "knowledge.db"

    # Try user home first
    try:
        home = Path.home()
        return home / ".niu" / "knowledge.db"
    except RuntimeError:
        # Fallback to current directory
        return Path(".niu/knowledge.db")


DEFAULT_DB_PATH = get_db_path()

# Initialize MCP server
server = Server("niu-kg-server")

# Global database connection
_db: kuzu.Database | None = None
_conn: kuzu.Connection | None = None


def get_connection() -> kuzu.Connection:
    """Get or create database connection."""
    global _db, _conn
    if _conn is None:
        db_path = get_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _db = kuzu.Database(str(db_path))
        _conn = kuzu.Connection(_db)
        _init_schema(_conn)
    return _conn


def _init_schema(conn: kuzu.Connection) -> None:
    """Initialize database schema."""
    # Document node table
    conn.execute("""
        CREATE NODE TABLE IF NOT EXISTS Document (
            uri STRING,
            title STRING,
            content STRING,
            source STRING,
            created_at STRING,
            PRIMARY KEY (uri)
        )
    """)

    # Entity node table
    conn.execute("""
        CREATE NODE TABLE IF NOT EXISTS Entity (
            id STRING,
            name STRING,
            type STRING,
            description STRING,
            PRIMARY KEY (id)
        )
    """)

    # Concept node table
    conn.execute("""
        CREATE NODE TABLE IF NOT EXISTS Concept (
            name STRING,
            description STRING,
            PRIMARY KEY (name)
        )
    """)

    # Relation tables
    conn.execute("""
        CREATE REL TABLE IF NOT EXISTS MENTIONS (
            FROM Document TO Entity
        )
    """)

    conn.execute("""
        CREATE REL TABLE IF NOT EXISTS CONTAINS (
            FROM Document TO Concept
        )
    """)

    conn.execute("""
        CREATE REL TABLE IF NOT EXISTS RELATED_TO (
            FROM Entity TO Entity,
            relation STRING
        )
    """)

    logger.info("Database schema initialized")


def create_document(
    uri: str, title: str, content: str = "", source: str = "", file_path: str = ""
) -> dict[str, Any]:
    """Create a document node in the graph.

    Args:
        uri: Unique identifier for the document (e.g., file path)
        title: Document title
        content: Document content (optional if file_path provided)
        source: Source of the document (optional)
        file_path: Path to file to read content from (optional)

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

    from datetime import datetime

    created_at = datetime.now().isoformat()

    conn.execute(
        "MERGE (d:Document {uri: $uri}) SET d.title = $title, d.content = $content, d.source = $source, d.created_at = $created_at",
        {
            "uri": uri,
            "title": title,
            "content": content,
            "source": source,
            "created_at": created_at,
        },
    )

    return {"status": "created", "uri": uri, "title": title}


def create_entity(
    id: str, name: str, entity_type: str, description: str = ""
) -> dict[str, Any]:
    """Create an entity node in the graph."""
    conn = get_connection()

    conn.execute(
        "MERGE (e:Entity {id: $id}) SET e.name = $name, e.type = $type, e.description = $description",
        {"id": id, "name": name, "type": entity_type, "description": description},
    )

    return {"status": "created", "id": id, "name": name, "type": entity_type}


def create_concept(name: str, description: str = "") -> dict[str, Any]:
    """Create a concept node in the graph."""
    conn = get_connection()

    conn.execute(
        "MERGE (c:Concept {name: $name}) SET c.description = $description",
        {"name": name, "description": description},
    )

    return {"status": "created", "name": name}


def link_document_entity(doc_uri: str, entity_id: str) -> dict[str, Any]:
    """Link a document to an entity (MENTIONS relation)."""
    conn = get_connection()

    conn.execute(
        "MATCH (d:Document {uri: $doc_uri}), (e:Entity {id: $entity_id}) MERGE (d)-[:MENTIONS]->(e)",
        {"doc_uri": doc_uri, "entity_id": entity_id},
    )

    return {"status": "linked", "document": doc_uri, "entity": entity_id}


def link_document_concept(doc_uri: str, concept_name: str) -> dict[str, Any]:
    """Link a document to a concept (CONTAINS relation)."""
    conn = get_connection()

    conn.execute(
        "MATCH (d:Document {uri: $doc_uri}), (c:Concept {name: $concept_name}) MERGE (d)-[:CONTAINS]->(c)",
        {"doc_uri": doc_uri, "concept_name": concept_name},
    )

    return {"status": "linked", "document": doc_uri, "concept": concept_name}


def link_entities(entity1_id: str, entity2_id: str, relation: str) -> dict[str, Any]:
    """Create a relation between two entities."""
    conn = get_connection()

    conn.execute(
        "MATCH (e1:Entity {id: $e1_id}), (e2:Entity {id: $e2_id}) MERGE (e1)-[:RELATED_TO {relation: $relation}]->(e2)",
        {"e1_id": entity1_id, "e2_id": entity2_id, "relation": relation},
    )

    return {
        "status": "linked",
        "entity1": entity1_id,
        "entity2": entity2_id,
        "relation": relation,
    }


def query_graph(cypher: str) -> list[dict[str, Any]]:
    """Execute a Cypher query and return results."""
    conn = get_connection()
    result = conn.execute(cypher)

    rows = []
    while result.has_next():
        row = result.get_next()
        rows.append(row)

    return rows


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


def search_documents(keyword: str, limit: int = 10) -> list[dict[str, Any]]:
    """Search documents by keyword in title or content."""
    conn = get_connection()
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
                        "description": "Entity type (e.g., person, organization, location)",
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
                doc_uri=arguments["doc_uri"], entity_id=arguments["entity_id"]
            )
        elif name == "link_document_concept":
            result = link_document_concept(
                doc_uri=arguments["doc_uri"], concept_name=arguments["concept_name"]
            )
        elif name == "link_entities":
            result = link_entities(
                entity1_id=arguments["entity1_id"],
                entity2_id=arguments["entity2_id"],
                relation=arguments["relation"],
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
