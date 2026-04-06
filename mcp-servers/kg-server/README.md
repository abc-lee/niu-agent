# Niu Knowledge Graph Server

MCP Server for managing a knowledge graph using Kuzu database.

## Tools

- `create_document` - Create a document node
- `create_entity` - Create an entity node (person, organization, etc.)
- `create_concept` - Create a concept node
- `link_document_entity` - Link a document to an entity
- `link_document_concept` - Link a document to a concept
- `link_entities` - Create a relation between two entities
- `get_document` - Get a document by URI
- `list_documents` - List all documents
- `search_documents` - Search documents by keyword
- `get_related_entities` - Get entities mentioned in a document
- `get_related_concepts` - Get concepts in a document
- `query_graph` - Execute a Cypher query

## Installation

```bash
cd mcp-servers/kg-server
uv pip install -e .
```

## Database Location

The knowledge graph database is stored at `~/.niu/knowledge.db`.
