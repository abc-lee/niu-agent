import sqlite3
import sys
from pathlib import Path

# 添加项目根目录到 sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.vector_search import resolve_vector_db_path

db_path = Path(resolve_vector_db_path())
conn = sqlite3.connect(str(db_path))

cursor = conn.execute("""
    SELECT id, json_extract(metadata, '$.server') as server, json_extract(metadata, '$.name') as name
    FROM documents
    WHERE json_extract(metadata, '$.category') = 'mcp_tool'
""")

tools = cursor.fetchall()

print(f'MCP tools in vector DB: {len(tools)}')
print()

# 按server分组
from collections import Counter
servers = Counter([t[1] for t in tools if t[1]])
print('By server:')
for server, count in sorted(servers.items()):
    print(f'  {server}: {count}')

print()
print('Tool list:')
for tid, server, name in tools:
    print(f'  {server}/{name}')

conn.close()
