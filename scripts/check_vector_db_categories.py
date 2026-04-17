#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys
import io
import sqlite3
import json
from pathlib import Path

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# 添加项目根目录到 sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.vector_search import resolve_vector_db_path

# 使用统一路径解析函数
db_path = Path(resolve_vector_db_path())

print(f"数据库路径: {db_path}")

conn = sqlite3.connect(str(db_path))
cursor = conn.cursor()

# 统计每种 category 的数量
cursor.execute('''
    SELECT json_extract(metadata, "$.category") as category, COUNT(*) as count
    FROM documents
    GROUP BY category
''')
counts = cursor.fetchall()
print('\nCategory 统计:')
for cat, count in counts:
    print(f'  {cat or "NULL"}: {count}')

# 总记录数
cursor.execute('SELECT COUNT(*) FROM documents')
total = cursor.fetchone()[0]
print(f'\n总记录数: {total}')

conn.close()
