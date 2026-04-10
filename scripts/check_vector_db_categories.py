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

# 使用正确的数据库路径
db_path = Path("REDACTED_WIN_PATH/vectors.db")
if not db_path.exists():
    db_path = Path.home() / '.niu' / 'vectors.db'

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
