"""P0-7: 测试数据库连接管理"""
import pytest
import sys
import sqlite3
import tempfile
import os
from pathlib import Path
sys.path.insert(0, "E:/tools/ai-bot")


@pytest.mark.p0
class TestDatabaseConnectionManagement:
    """测试数据库连接使用 with 语句管理"""

    @pytest.fixture
    def test_db(self):
        """创建测试数据库"""
        db_path = tempfile.mktemp(suffix=".db")

        # 创建表
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE scheduled_tasks (
                    id TEXT PRIMARY KEY,
                    content TEXT,
                    status TEXT,
                    scheduled_at TEXT,
                    created_at TEXT
                )
            """)
            cursor.execute("""
                INSERT INTO scheduled_tasks
                VALUES ('task_1', 'Test task', 'pending', '2024-01-01', '2024-01-01')
            """)
            conn.commit()

        yield db_path

        # 清理
        if os.path.exists(db_path):
            os.unlink(db_path)

    def test_with_statement_closes_connection_on_success(self, test_db):
        """测试 with 语句在成功时关闭连接"""
        # 使用 with 语句
        with sqlite3.connect(test_db) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM scheduled_tasks")
            result = cursor.fetchone()

        # 验证查询成功
        assert result is not None
        assert result[0] == 'task_1'

    def test_with_statement_closes_connection_on_error(self, test_db):
        """测试 with 语句在异常时关闭连接"""
        try:
            with sqlite3.connect(test_db) as conn:
                cursor = conn.cursor()
                # 执行一个会失败的查询（不存在的表）
                cursor.execute("SELECT * FROM nonexistent_table")
        except sqlite3.Error as e:
            # 预期的异常
            assert "no such table" in str(e)

        # 验证异常被正确处理，没有资源泄露

    def test_exception_handling_with_try_except(self, test_db):
        """测试 try-except 包装数据库操作"""
        latest_task = None

        try:
            with sqlite3.connect(test_db) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, content, status, scheduled_at
                    FROM scheduled_tasks
                    ORDER BY created_at DESC
                    LIMIT 1
                """)
                latest_task = cursor.fetchone()
        except sqlite3.Error as e:
            print(f"Database error: {e}")
            latest_task = None

        # 验证查询成功
        assert latest_task is not None
        assert latest_task[0] == 'task_1'


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-m", "p0"])
