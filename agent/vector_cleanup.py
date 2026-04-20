"""
向量库被动清理服务

清理策略：
1. L1 指针有效性检查 - L2 不存在则删 L1
2. 失效 Skills 清理 - 文件已删除
3. 失效 MCP 工具清理 - 服务器已移除
4. 去重 - 同一 ID 多条记录，保留最新

不判断记忆价值，只清理明确失效的数据。
"""

import json
import sqlite3
from pathlib import Path
from typing import Tuple

from loguru import logger

from .vector_search import get_vector_search


class VectorCleanup:
    """向量库清理服务"""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or get_vector_search().db_path

    def _get_connection(self) -> sqlite3.Connection:
        """获取配置了 WAL 模式的数据库连接"""
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def cleanup_invalid_l1_pointers(self) -> int:
        """
        清理 L1 无效指针（L2 不存在）

        Returns:
            删除的 L1 记录数量
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        # 查询所有 L1 记录
        cursor.execute(
            "SELECT id, metadata FROM documents WHERE json_extract(metadata, '$.level') = 'l1'"
        )
        l1_records = cursor.fetchall()

        deleted = 0
        for l1_id, metadata_json in l1_records:
            metadata = json.loads(metadata_json) if metadata_json else {}
            l2_id = metadata.get("l2_pointer") or metadata.get("pointer")

            if not l2_id:
                continue

            # 检查 L2 是否存在
            cursor.execute("SELECT id FROM documents WHERE id = ?", (l2_id,))
            if not cursor.fetchone():
                # L2 不存在，删除 L1
                cursor.execute("DELETE FROM documents WHERE id = ?", (l1_id,))
                deleted += 1
                logger.info(f"[Cleanup] Deleted L1 with invalid pointer: {l1_id[:50]}...")

        conn.commit()
        conn.close()

        if deleted > 0:
            logger.info(f"[Cleanup] Deleted {deleted} L1 records with invalid pointers")
        return deleted

    def cleanup_orphaned_skills(self) -> Tuple[int, int]:
        """
        清理失效的 Skills（文件已删除）

        Returns:
            (删除数量, 保留数量)
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        # 查询所有 Skills
        cursor.execute(
            "SELECT id, metadata FROM documents WHERE json_extract(metadata, '$.category') = 'skill'"
        )
        skills = cursor.fetchall()

        deleted = 0
        kept = 0

        for doc_id, metadata_json in skills:
            metadata = json.loads(metadata_json) if metadata_json else {}
            # 优先使用 metadata.source（_sync_skill 写入的绝对路径）
            source = metadata.get("source", "")
            if source:
                skill_path = Path(source)
            else:
                # 回退：使用 SkillSync 的默认目录
                from .injector.sync import SkillSync
                skills_dir = Path(SkillSync._default_skills_dir())
                skill_name = metadata.get("name", "")
                if not skill_name:
                    logger.warning(f"[Cleanup] Skill record has no source and no name, skipping: {doc_id[:50]}")
                    kept += 1
                    continue
                skill_path = skills_dir / f"{skill_name}.md"

            if not skill_path.exists():
                cursor.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
                deleted += 1
                logger.info(f"[Cleanup] Deleted orphaned skill: {metadata.get('name', 'unknown')}")
            else:
                kept += 1

        conn.commit()
        conn.close()

        if deleted > 0:
            logger.info(f"[Cleanup] Deleted {deleted} orphaned skills, kept {kept}")
        return deleted, kept

    def cleanup_orphaned_mcp_tools(self) -> Tuple[int, int]:
        """
        清理失效的 MCP 工具描述（服务器已移除）

        Returns:
            (删除数量, 保留数量)
        """
        # 获取当前配置的服务器列表
        import yaml
        config_path = Path(__file__).parent.parent / "config" / "mcp-servers.yaml"
        if not config_path.exists():
            logger.warning("[Cleanup] mcp-servers.yaml not found, skipping MCP tools cleanup")
            return 0, 0

        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)

        active_servers = set(config.keys())

        # 查询所有 MCP 工具
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT id, metadata FROM documents WHERE json_extract(metadata, '$.category') = 'mcp_tool'"
        )
        tools = cursor.fetchall()

        deleted = 0
        kept = 0

        for doc_id, metadata_json in tools:
            metadata = json.loads(metadata_json) if metadata_json else {}
            server = metadata.get("server")

            if server not in active_servers:
                cursor.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
                deleted += 1
                tool_name = metadata.get("name", "unknown")
                logger.info(f"[Cleanup] Deleted orphaned MCP tool: {tool_name} (server: {server})")
            else:
                kept += 1

        conn.commit()
        conn.close()

        if deleted > 0:
            logger.info(f"[Cleanup] Deleted {deleted} orphaned MCP tools, kept {kept}")
        return deleted, kept

    def cleanup_duplicates(self) -> int:
        """
        清理重复内容（同一 ID 多条记录，保留最新）

        Returns:
            删除的记录数量
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        # 查找重复 ID
        cursor.execute(
            """
            SELECT id, COUNT(*) as count
            FROM documents
            GROUP BY id
            HAVING count > 1
            """
        )

        duplicates = cursor.fetchall()
        deleted_count = 0

        for doc_id, count in duplicates:
            # 保留最新的一条（按 rowid）
            cursor.execute(
                """
                DELETE FROM documents
                WHERE id = ?
                  AND rowid NOT IN (
                      SELECT rowid FROM documents WHERE id = ? ORDER BY rowid DESC LIMIT 1
                  )
                """,
                (doc_id, doc_id),
            )
            deleted_count += cursor.rowcount
            logger.warning(f"[Cleanup] Deleted {cursor.rowcount} duplicate records for ID: {doc_id[:50]}...")

        conn.commit()
        conn.close()

        if deleted_count > 0:
            logger.info(f"[Cleanup] Deleted {deleted_count} duplicate records")
        return deleted_count

    def run_full_cleanup(self):
        """执行完整清理（每个步骤独立 try-except，避免部分失败导致后续步骤不执行）"""
        logger.info("[Cleanup] Starting vector database cleanup...")

        import time
        start_time = time.time()

        del_skills = 0
        del_mcp = 0
        del_l1 = 0
        del_duplicates = 0

        # 1. 清理失效 Skills
        try:
            del_skills, _ = self.cleanup_orphaned_skills()
        except Exception as e:
            logger.error(f"[Cleanup] cleanup_orphaned_skills failed: {e}")

        # 2. 清理失效 MCP 工具
        try:
            del_mcp, _ = self.cleanup_orphaned_mcp_tools()
        except Exception as e:
            logger.error(f"[Cleanup] cleanup_orphaned_mcp_tools failed: {e}")

        # 3. 清理 L1 无效指针
        try:
            del_l1 = self.cleanup_invalid_l1_pointers()
        except Exception as e:
            logger.error(f"[Cleanup] cleanup_invalid_l1_pointers failed: {e}")

        # 4. 清理重复内容
        try:
            del_duplicates = self.cleanup_duplicates()
        except Exception as e:
            logger.error(f"[Cleanup] cleanup_duplicates failed: {e}")

        elapsed = time.time() - start_time

        logger.info(
            f"[Cleanup] Completed in {elapsed:.1f}s. "
            f"Deleted: {del_skills} skills, {del_mcp} MCP tools, "
            f"{del_l1} invalid L1, {del_duplicates} duplicates"
        )


# 全局实例
_cleanup_service: VectorCleanup = None


def get_cleanup_service() -> VectorCleanup:
    """获取清理服务实例"""
    global _cleanup_service
    if _cleanup_service is None:
        _cleanup_service = VectorCleanup()
    return _cleanup_service
