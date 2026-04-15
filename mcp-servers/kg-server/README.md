# Niu Knowledge Graph Server

MCP Server for managing a knowledge graph using Kuzu database.

## Tools

### Entity & Document Management
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

### Graph Analysis
- `query_graph` - Execute a read-only Cypher query (CREATE/DELETE/SET blocked)
- `explore_node` - BFS traversal from an entity (depth, direction, confidence filter)
- `find_path` - Find shortest path between two entities
- `graph_stats` - Overview statistics (nodes/edges by type, confidence distribution, density)
- `hub_entities` - Most central entities by degree centrality (outgoing + incoming)
- `surprising_connections` - Find entity pairs sharing neighbors but not directly linked
- `graph_changelog` - Recent graph changes sorted by timestamp (entities + edges)

## Confidence Mechanism

All relations have a `confidence` (0.0-1.0) and `created_at` timestamp:

| Level | Range | Use Case |
|-------|-------|----------|
| High | 0.7-1.0 | User confirmed, LLM extracted |
| Medium | 0.4-0.7 | Agent inferred |
| Low | 0.0-0.4 | Algorithm discovered |

`confidence` defaults to 1.0 for backward compatibility. `created_at` is auto-set to UTC ISO 8601.

## Installation

```bash
cd mcp-servers/kg-server
uv pip install -e .
```

## Database Location

The knowledge graph database is stored at `~/.niu/knowledge.db`.
