"""
记忆存储 - 使用内部 embedding 模块，支持 L0/L1/L2 三层存储
"""

import os
import json
import sqlite3
import numpy as np
from typing import Optional, List, Dict, Any
from loguru import logger
from datetime import datetime


def get_embedding_sync(text: str) -> List[float]:
    """获取向量 - 直接调用内部模块"""
    try:
        from niu_api.internal.embedding import encode
        return encode(text)
    except Exception as e:
        logger.error(f"获取向量失败: {e}")
        raise


def get_db_path() -> str:
    """获取向量数据库路径"""
    workspace = os.environ.get("WORKSPACE_PATH", ".")
    return os.path.join(workspace, "vectors.db")


class MemoryStorage:
    """记忆存储类 - 支持 L0/L1/L2 三层存储"""

    def __init__(self):
        self.db_path = get_db_path()
        self._init_db()

    def _init_db(self):
        """初始化数据库"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    embedding BLOB,
                    metadata TEXT
                )
            """)

            # 创建索引
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_level
                ON documents(json_extract(metadata, '$.level'))
            """)

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_memory_type
                ON documents(json_extract(metadata, '$.memory_type'))
            """)

            conn.commit()
            conn.close()

            logger.info(f"数据库初始化完成: {self.db_path}")

        except Exception as e:
            logger.error(f"数据库初始化失败: {e}")

    def store_memory(
        self, content: str, memory_type: str, metadata: dict = None,
        title: str = None, importance: float = None
    ) -> str:
        """
        存储记忆 - 兼容旧版接口
        自动生成 L0/L1/L2 三层记录

        Args:
            content: 记忆内容
            memory_type: 记忆类型
            metadata: 可选元数据
            title: 可选标题（≤20字符）
            importance: 可选重要性评分（0-1）
        """
        import uuid

        try:
            memory_id = f"memory:{memory_type}:{uuid.uuid4().hex[:8]}"

            # 如果没有提供 importance，根据类型自动设置
            if importance is None:
                importance_defaults = {
                    "environment": 0.9,
                    "preferences": 0.8,
                    "skills": 0.7,
                    "experiences": 0.6,
                    "facts": 0.5,
                }
                importance = importance_defaults.get(memory_type, 0.5)

            # 生成三层记录
            l0_id = f"{memory_id}:l0"
            l1_id = f"{memory_id}:l1"
            l2_id = f"{memory_id}:l2"

            # L2: 完整内容
            l2_content = content
            l2_metadata = {
                "level": "l2",
                "memory_type": memory_type,
                "title": title or f"{memory_type}记录",
                "importance": importance,
                "created_at": datetime.now().isoformat(),
                "access_count": 0,
                **(metadata or {}),
            }

            # L1: 摘要
            l1_content = self._generate_l1_summary(content, memory_type, title)
            l1_metadata = {
                "level": "l1",
                "memory_type": memory_type,
                "title": title or f"{memory_type}记录",
                "importance": importance,
                "l2_pointer": l2_id,
                "created_at": datetime.now().isoformat(),
                **(metadata or {}),
            }

            # L0: 极简索引
            l0_content = self._generate_l0_index(content, memory_type)
            l0_metadata = {
                "level": "l0",
                "memory_type": memory_type,
                "l1_pointer": l1_id,
                "created_at": datetime.now().isoformat(),
                **(metadata or {}),
            }

            # 存储三层
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            # L2（原文）
            embedding_l2 = np.array(get_embedding_sync(l2_content), dtype=np.float32)
            # ✅ L2归一化（符合L1规范v2.0）
            norm_l2 = np.linalg.norm(embedding_l2)
            if norm_l2 > 0:
                embedding_l2 = embedding_l2 / norm_l2
            cursor.execute(
                "INSERT INTO documents (id, content, embedding, metadata) VALUES (?, ?, ?, ?)",
                (l2_id, l2_content, embedding_l2.tobytes(), json.dumps(l2_metadata)),
            )

            # L1（摘要）
            embedding_l1 = np.array(get_embedding_sync(l1_content), dtype=np.float32)
            # ✅ L2归一化（符合L1规范v2.0）
            norm_l1 = np.linalg.norm(embedding_l1)
            if norm_l1 > 0:
                embedding_l1 = embedding_l1 / norm_l1
            cursor.execute(
                "INSERT INTO documents (id, content, embedding, metadata) VALUES (?, ?, ?, ?)",
                (l1_id, l1_content, embedding_l1.tobytes(), json.dumps(l1_metadata)),
            )

            # L0（极简索引）
            embedding_l0 = np.array(get_embedding_sync(l0_content), dtype=np.float32)
            # ✅ L2归一化（符合L1规范v2.0）
            norm_l0 = np.linalg.norm(embedding_l0)
            if norm_l0 > 0:
                embedding_l0 = embedding_l0 / norm_l0
            cursor.execute(
                "INSERT INTO documents (id, content, embedding, metadata) VALUES (?, ?, ?, ?)",
                (l0_id, l0_content, embedding_l0.tobytes(), json.dumps(l0_metadata)),
            )

            conn.commit()
            conn.close()

            logger.info(f"存储记忆 (L0/L1/L2): {memory_id} ({memory_type})")
            return memory_id

        except Exception as e:
            logger.error(f"存储记忆失败: {e}")
            raise

    def _generate_l0_index(self, content: str, memory_type: str) -> str:
        """
        生成 L0 极简索引（≤50字符）

        策略：
        1. 如果内容本身 ≤50 字符，直接使用
        2. 提取第一句话（按句号分割）
        3. 如果还是太长，截取前 47 字符加 "..."
        """
        if len(content) <= 50:
            return content

        # 提取第一句话
        first_sentence = content.split('。')[0].split('\n')[0].strip()
        if len(first_sentence) <= 50:
            return first_sentence

        return first_sentence[:47] + "..."

    def _generate_l1_summary(self, content: str, memory_type: str, title: str = None) -> str:
        """
        生成 L1 摘要（英文，符合L1规范v2.0）

        格式：{title}|{keywords}|{summary}|{entities}|{type}|{pointer}

        **重要**：根据L1规范v2.0，L1内容必须是英文。
        如果content是中文，应该在外部先翻译成英文再传入。

        Args:
            content: 记忆内容（建议英文）
            memory_type: 记忆类型
            title: 可选标题，不提供则自动生成
        """
        import re

        # 提取标题（第一行或前 20 字符）
        if title:
            title_str = title[:20]
        else:
            lines = content.strip().split('\n')
            title_str = lines[0].strip('# ')[:20] if lines else f"{memory_type} record"

        # 提取关键词（简单实现：提取英文单词和数字）
        keywords = re.findall(r'\b[A-Z][a-z]+\b|\b\d+\b', content)
        keywords_str = ','.join(set(keywords[:5]))  # 最多 5 个

        # 生成摘要（前 200 字符）
        summary_str = content[:200].replace('\n', ' ')

        # 提取实体（简单实现：提取数字+单位）
        entities = re.findall(r'\d+(?:GB|MB|TB|MHz|GHz|ms|s)\b', content)
        entities_str = ','.join(set(entities[:5]))

        return f"{title_str}|{keywords_str}|{summary_str}|{entities_str}|{memory_type}|l2"

    def search_memories(
        self, query: str, limit: int = 5, filter_dict: dict = None, level: str = "l1"
    ) -> list[dict]:
        """
        搜索相关记忆

        Args:
            query: 搜索查询
            limit: 返回数量限制
            filter_dict: 过滤条件
            level: 搜索层级（默认 l1）
        """
        try:
            # 获取查询向量
            query_embedding = np.array(get_embedding_sync(query), dtype=np.float32)

            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            # 使用 level 参数过滤
            sql = "SELECT id, content, embedding, metadata FROM documents WHERE json_extract(metadata, '$.level') = ?"
            params = [level]

            cursor.execute(sql, params)
            rows = cursor.fetchall()
            conn.close()

            results = []
            for row in rows:
                memory_id, content, embedding_bytes, metadata_json = row

                metadata = json.loads(metadata_json) if metadata_json else {}

                # 应用其他过滤条件
                if filter_dict:
                    skip = False
                    for key, value in filter_dict.items():
                        if metadata.get(key) != value:
                            skip = True
                            break
                    if skip:
                        continue

                # 计算相似度
                embedding = np.frombuffer(embedding_bytes, dtype=np.float32)
                similarity = np.dot(query_embedding, embedding) / (
                    np.linalg.norm(query_embedding) * np.linalg.norm(embedding)
                )

                results.append(
                    {
                        "id": memory_id,
                        "content": content,
                        "metadata": metadata,
                        "similarity": float(similarity),
                    }
                )

            results.sort(key=lambda x: x["similarity"], reverse=True)
            return results[:limit]

        except Exception as e:
            logger.error(f"搜索记忆失败: {e}")
            return []

    def list_memories(self, memory_type: str = None, limit: int = 20) -> list[dict]:
        """列出记忆"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            if memory_type:
                cursor.execute("SELECT id, content, metadata FROM documents")
                rows = cursor.fetchall()

                results = []
                for row in rows:
                    memory_id, content, metadata_json = row
                    metadata = json.loads(metadata_json) if metadata_json else {}

                    if metadata.get("type") == memory_type:
                        results.append(
                            {"id": memory_id, "content": content, "metadata": metadata}
                        )

                return results[:limit]
            else:
                cursor.execute(
                    "SELECT id, content, metadata FROM documents LIMIT ?", (limit,)
                )
                rows = cursor.fetchall()

                return [
                    {
                        "id": row[0],
                        "content": row[1],
                        "metadata": json.loads(row[2]) if row[2] else {},
                    }
                    for row in rows
                ]

        except Exception as e:
            logger.error(f"列出记忆失败: {e}")
            return []

    def delete_memory(self, memory_id: str):
        """删除记忆（删除所有层级）"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            # 删除所有层级（L0/L1/L2）
            cursor.execute("DELETE FROM documents WHERE id LIKE ?", (f"{memory_id}%",))

            conn.commit()
            conn.close()
            logger.info(f"删除记忆（所有层级）: {memory_id}")
        except Exception as e:
            logger.error(f"删除记忆失败: {e}")
            raise

    def update_memory(self, memory_id: str, content: str, metadata: dict = None) -> str:
        """更新记忆"""
        try:
            # 获取旧记忆的 metadata
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            cursor.execute("SELECT metadata FROM documents WHERE id = ?", (f"{memory_id}:l2",))
            row = cursor.fetchone()

            if not row:
                logger.error(f"记忆不存在: {memory_id}")
                raise ValueError(f"记忆不存在: {memory_id}")

            old_metadata = json.loads(row[0])
            memory_type = old_metadata.get("memory_type", "unknown")

            # 删除旧记忆
            conn.close()
            self.delete_memory(memory_id)

            # 重新存储
            return self.store_memory(content, memory_type, metadata)

        except Exception as e:
            logger.error(f"更新记忆失败: {e}")
            raise

    def get_memory_stats(self) -> dict:
        """获取记忆统计"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            # 总数统计
            cursor.execute("SELECT COUNT(*) FROM documents")
            total = cursor.fetchone()[0]

            # 按层级统计
            cursor.execute("""
                SELECT json_extract(metadata, '$.level'), COUNT(*)
                FROM documents
                GROUP BY json_extract(metadata, '$.level')
            """)
            by_level = {row[0]: row[1] for row in cursor.fetchall()}

            # 按类型统计（L1 层级）
            cursor.execute("""
                SELECT json_extract(metadata, '$.memory_type'), COUNT(*)
                FROM documents
                WHERE json_extract(metadata, '$.level') = 'l1'
                GROUP BY json_extract(metadata, '$.memory_type')
            """)
            by_type = {row[0]: row[1] for row in cursor.fetchall()}

            conn.close()

            return {
                "total": total,
                "by_level": by_level,
                "by_type": by_type,
            }

        except Exception as e:
            logger.error(f"获取统计失败: {e}")
            return {"total": 0, "by_level": {}, "by_type": {}}

    def cleanup_memories(self, days: int = 30) -> int:
        """清理过期的记忆"""
        try:
            from datetime import datetime, timedelta

            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            # 计算截止时间
            cutoff_date = (datetime.now() - timedelta(days=days)).isoformat()

            # 查找过期记忆
            cursor.execute("""
                SELECT DISTINCT substr(id, 1, instr(id, ':l') - 1)
                FROM documents
                WHERE json_extract(metadata, '$.created_at') < ?
            """, (cutoff_date,))

            expired_ids = [row[0] for row in cursor.fetchall()]

            # 删除过期记忆（所有层级）
            deleted_count = 0
            for memory_id in expired_ids:
                cursor.execute("DELETE FROM documents WHERE id LIKE ?", (f"{memory_id}%",))
                deleted_count += cursor.rowcount

            conn.commit()
            conn.close()

            logger.info(f"清理过期记忆: {len(expired_ids)} 条记忆（{deleted_count} 条记录）")
            return len(expired_ids)

        except Exception as e:
            logger.error(f"清理记忆失败: {e}")
            return 0

    def link_memories(self, memory_id_1: str, memory_id_2: str, relation: str) -> bool:
        """关联两条记忆"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            # 更新 L1 记录的 metadata
            for memory_id in [memory_id_1, memory_id_2]:
                l1_id = f"{memory_id}:l1"

                cursor.execute("SELECT metadata FROM documents WHERE id = ?", (l1_id,))
                row = cursor.fetchone()

                if row:
                    metadata = json.loads(row[0])
                    related = metadata.get("related_memories", [])
                    related.append({
                        "id": memory_id_2 if memory_id == memory_id_1 else memory_id_1,
                        "relation": relation,
                    })
                    metadata["related_memories"] = related

                    cursor.execute(
                        "UPDATE documents SET metadata = ? WHERE id = ?",
                        (json.dumps(metadata), l1_id),
                    )

            conn.commit()
            conn.close()

            logger.info(f"关联记忆: {memory_id_1} <-> {memory_id_2} ({relation})")
            return True

        except Exception as e:
            logger.error(f"关联记忆失败: {e}")
            return False
