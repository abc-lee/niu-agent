#!/usr/bin/env python3
"""导出所有MCP工具定义"""
import sys
import json
from pathlib import Path

# 添加项目根目录
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.mcp_loader import load_mcp_tools

# 加载所有工具
registry = load_mcp_tools()
schemas = registry.get_schemas()

print(f"Total MCP tools: {len(schemas)}")

# 按server分组
from collections import defaultdict
tools_by_server = defaultdict(list)

for schema in schemas:
    name = schema.get("name", "")
    if "/" in name:
        server, tool_name = name.split("/", 1)
        tools_by_server[server].append({
            "server": server,
            "name": tool_name,
            "description": schema.get("description", ""),
            "input_schema": schema.get("input_schema", {}),
            "visibility": schema.get("visibility", "dynamic")
        })

# 保存到JSON
output_file = Path(__file__).parent.parent / "data" / "mcp_tools.json"
output_file.parent.mkdir(exist_ok=True)

with open(output_file, 'w', encoding='utf-8') as f:
    json.dump(dict(tools_by_server), f, indent=2, ensure_ascii=False)

print(f"Saved to: {output_file}")

# 打印统计
for server, tools in sorted(tools_by_server.items()):
    print(f"{server}: {len(tools)} tools")
