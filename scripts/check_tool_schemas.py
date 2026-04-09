"""检查工具Schema格式"""
import json
import sys
import os

# 添加项目根目录到 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.mcp_loader import load_mcp_tools

# 加载工具
registry = load_mcp_tools()
schemas = registry.get_schemas()

print(f"Total tools: {len(schemas)}")
print("\n" + "=" * 60)

# 显示前3个工具的完整schema
for i, schema in enumerate(schemas[:3]):
    print(f"\nTool #{i+1}:")
    print(json.dumps(schema, indent=2, ensure_ascii=False))
    print("-" * 60)
