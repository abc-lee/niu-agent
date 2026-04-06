# Niu File Parser

MCP Server for parsing various document formats.

## Supported Formats

- PDF (.pdf)
- Microsoft Word (.docx)
- Microsoft PowerPoint (.pptx)
- Microsoft Excel (.xlsx)
- Markdown (.md)
- HTML (.html)
- Plain Text (.txt)

## Installation

```bash
cd mcp-servers/file-parser
uv pip install -e .
```

## Usage

This is an MCP server that communicates via stdio. It provides two tools:

### `parse_file`

Parse a document and extract text content.

```json
{
  "name": "parse_file",
  "arguments": {
    "file_path": "/path/to/document.pdf"
  }
}
```

### `list_supported_formats`

List all supported file formats.

```json
{
  "name": "list_supported_formats",
  "arguments": {}
}
```
