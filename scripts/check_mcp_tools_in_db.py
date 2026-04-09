import sqlite3
from pathlib import Path
from collections import Counter

db_path = Path('REDACTED_WIN_PATH/vectors.db')
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
servers = Counter([t[1] for t in tools if t[1]])
print('By server:')
for server, count in sorted(servers.items()):
    print(f'  {server}: {count}')

print()
print('Tool list:')
for tid, server, name in tools:
    print(f'  {server}/{name}')

conn.close()
