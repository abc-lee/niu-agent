"""
手动同步 Skills 到向量库
"""

import sys
import os
import io

# 设置 stdout 编码为 UTF-8
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.injector.sync import get_skill_sync
from agent.vector_search import get_vector_search

def main():
    print("=" * 60)
    print("Skills 同步工具")
    print("=" * 60)

    # 1. 检查向量库中现有的 Skills
    print("\n[1] 检查向量库中现有的 Skills...")
    vs = get_vector_search()

    # 搜索所有 skill 类型的记录
    import json
    import sqlite3

    conn = vs._get_connection()
    if conn:
        cursor = conn.execute(
            """SELECT id, json_extract(metadata, '$.name') as name,
                      json_extract(metadata, '$.category') as category
               FROM documents
               WHERE json_extract(metadata, '$.category') = 'skill'
               ORDER BY id"""
        )
        rows = cursor.fetchall()

        print(f"\n找到 {len(rows)} 个 Skills:")
        for row in rows:
            print(f"  - {row[1]} (ID: {row[0]})")

    # 2. 检查 Skills 目录中的文件
    print("\n[2] 检查 Skills 目录...")
    skills_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent", "memory", "skills")

    if os.path.exists(skills_dir):
        skill_files = [f for f in os.listdir(skills_dir) if f.endswith('.md')]
        print(f"\n找到 {len(skill_files)} 个 Skill 文件:")
        for f in skill_files:
            print(f"  - {f}")
    else:
        print(f"\nSkills 目录不存在: {skills_dir}")
        return

    # 3. 手动触发同步
    print("\n[3] 触发同步...")
    sync = get_skill_sync(auto_start=False)

    # 执行一次扫描
    added, updated, deleted = sync.scan_and_sync()

    print(f"\n同步结果:")
    print(f"  - 新增: {added}")
    print(f"  - 更新: {updated}")
    print(f"  - 删除: {deleted}")

    # 4. 验证同步结果
    print("\n[4] 验证同步结果...")
    if conn:
        cursor = conn.execute(
            """SELECT id, json_extract(metadata, '$.name') as name
               FROM documents
               WHERE json_extract(metadata, '$.category') = 'skill'
               ORDER BY id"""
        )
        rows = cursor.fetchall()

        print(f"\n向量库中现有 {len(rows)} 个 Skills:")
        for row in rows:
            print(f"  ✅ {row[1]}")

    print("\n" + "=" * 60)
    print("同步完成！")
    print("=" * 60)

if __name__ == "__main__":
    main()
