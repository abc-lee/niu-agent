# Niu Vector Store

MCP Server for semantic search using vector embeddings.

## Tools

- `add_document` - Add a document for indexing
- `search_documents` - Semantic search for similar documents
- `get_document` - Get a document by ID
- `delete_document` - Delete a document
- `list_documents` - List all documents
- `count_documents` - Count total documents

## Features

- Uses OpenAI embeddings API (requires OPENAI_API_KEY)
- Falls back to simple text search if no API key
- Stores vectors in SQLite for persistence
- Cosine similarity for semantic matching

## Installation

```bash
cd mcp-servers/vector-store
uv pip install -e .
```
