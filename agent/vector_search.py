"""
Vector Search Adapter

提供向量检索能力，用于每轮对话前的知识注入。

设计原则：
- 直接调用向量数据库（不通过 MCP）
- 返回格式化的参考知识
- 支持阈值过滤和数量限制
"""

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional
from dataclasses import dataclass

import numpy as np


@dataclass
class SearchResult:
    """A search result with score"""

    id: str
    content: str
    score: float
    metadata: dict


class VectorSearchAdapter:
    """
    向量检索适配器

    直接访问向量数据库，不依赖 MCP 服务器
    """

    def __init__(self, db_path: str = None):
        self.db_path = db_path or self._default_db_path()
        self._conn: Optional[sqlite3.Connection] = None
        self._indexes_created: bool = False  # 索引创建标志

    @staticmethod
    def _default_db_path() -> str:
        """获取默认向量库路径，优先使用用户配置的工作目录"""
        # 1. 尝试从 memory.json 读取工作目录
        memory_path = os.path.join(os.path.expanduser("~"), ".niu", "memory.json")
        if os.path.exists(memory_path):
            try:
                with open(memory_path, "r", encoding="utf-8") as f:
                    memory = json.load(f)
                    workspace_path = memory.get("workspace", {}).get("path")
                    if workspace_path and os.path.exists(workspace_path):
                        return os.path.join(workspace_path, "vectors.db")
            except Exception:
                pass

        # 2. 降级到 home 目录
        home = os.path.expanduser("~")
        return os.path.join(home, ".niu", "vectors.db")

    def _get_connection(self) -> Optional[sqlite3.Connection]:
        if self._conn is None:
            if os.path.exists(self.db_path):
                # check_same_thread=False 允许跨线程使用
                self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
                # 只在首次连接时创建索引
                if not self._indexes_created:
                    self._ensure_indexes()
                    self._indexes_created = True
            else:
                # 数据库不存在，返回 None
                return None
        return self._conn

    def _ensure_indexes(self):
        """创建数据库索引以提高查询性能"""
        if self._conn is None:
            return

        try:
            # 为 metadata 中的 level 字段创建索引
            # SQLite 不支持直接在 JSON 字段上创建索引，使用表达式索引
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_level ON documents(json_extract(metadata, '$.level'))"
            )

            # 为 category 字段创建索引
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_category ON documents(json_extract(metadata, '$.category'))"
            )

            # 为 server 字段创建索引（MCP 工具）
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_server ON documents(json_extract(metadata, '$.server'))"
            )

            self._conn.commit()
        except Exception as e:
            print(f"[VectorSearch] Failed to create indexes: {e}", file=sys.stderr, flush=True)

    def _get_embedding(self, text: str) -> Optional[list[float]]:
        """获取向量 - 直接调用内部模块"""
        try:
            from niu_api.internal.embedding import encode
            return encode(text)
        except Exception as e:
            print(f"[VectorSearch] Embedding error: {e}", file=sys.stderr, flush=True)
            return None

    def search(
        self, query: str, limit: int = 10, min_score: float = 0.5,
        filter: Optional[dict] = None, level: Optional[str] = None,
        max_recursion: int = 3
    ) -> list[SearchResult]:
        """
        语义搜索（支持递归查询）

        Args:
            query: 搜索查询
            limit: 最大结果数（默认 10）
            min_score: 最低相似度阈值（默认 0.5，即 50 分）
            filter: 元数据过滤条件
            level: L0/L1/L2 层级过滤（可选：'l0', 'l1', 'l2'）
            max_recursion: 最大递归次数（默认 3，硬编码上限）

        Returns:
            搜索结果列表，按分数降序排列
        """
        # 安全限制：硬编码最多递归3次
        if max_recursion <= 0:
            recursion_depth = 4 - max_recursion
            print(f"[WARNING] Max recursion reached ({recursion_depth}/3), returning results",
                  file=sys.stderr, flush=True)
            return []

        # 验证 level 参数
        if level is not None and level not in ('l0', 'l1', 'l2'):
            print(f"[VectorSearch] Invalid level '{level}', ignoring.", file=sys.stderr, flush=True)
            level = None

        # 单次检索
        results = self._search_once(query, limit, min_score, filter, level)

        # 检查是否有递归查询标记
        for result in results:
            if result.metadata.get("is_recursive") == True:
                # 发现递归查询标记，提取精简查询
                refined = result.metadata.get("refined_query")
                if not refined:
                    continue

                # 构建新的过滤条件
                new_filter = {"category": result.metadata.get("category")}

                # 记录递归信息
                recursion_depth = 4 - max_recursion
                print(f"[Recursive Query] {query} → {refined} (depth: {recursion_depth}/3)",
                      file=sys.stderr, flush=True)

                # 递归调用（强制递减）
                return self.search(
                    query=refined,
                    limit=limit,
                    min_score=min_score,
                    filter=new_filter,
                    level=level,
                    max_recursion=max_recursion - 1  # ✅ 强制递减
                )

        # 没有递归标记，直接返回
        return results

    def _search_once(
        self, query: str, limit: int, min_score: float,
        filter: Optional[dict], level: Optional[str]
    ) -> list[SearchResult]:
        """单次向量检索（内部方法）"""
        conn = self._get_connection()
        if conn is None:
            return []

        # 获取查询向量
        query_embedding = self._get_embedding(query)

        # 构建 SQL 查询（支持 level 过滤）
        sql = "SELECT id, content, embedding, metadata FROM documents WHERE embedding IS NOT NULL"
        params = []

        if level:
            # 使用 JSON 函数过滤 level
            sql += " AND json_extract(metadata, '$.level') = ?"
            params.append(level)

        cursor = conn.execute(sql, params)
        docs = cursor.fetchall()

        if not query_embedding or not docs:
            # 降级到文本搜索
            return self._text_search(query, limit, min_score, filter, level)

        # 向量相似度搜索
        query_vec = np.array(query_embedding, dtype=np.float32)

        scored_docs = []
        for doc_id, content, embedding_blob, metadata_json in docs:
            if embedding_blob:
                metadata = json.loads(metadata_json) if metadata_json else {}

                # 应用过滤条件
                if filter and not self._matches_filter(metadata, filter):
                    continue

                doc_vec = np.frombuffer(embedding_blob, dtype=np.float32)
                score = float(
                    np.dot(query_vec, doc_vec)
                    / (np.linalg.norm(query_vec) * np.linalg.norm(doc_vec))
                )

                # 应用阈值
                if score >= min_score:
                    scored_docs.append((doc_id, content, metadata, score))

        # 按分数降序排序
        scored_docs.sort(key=lambda x: x[3], reverse=True)

        # 限制数量
        results = []
        for doc_id, content, metadata, score in scored_docs[:limit]:
            results.append(SearchResult(id=doc_id, content=content, score=score, metadata=metadata))

        return results

    def _text_search(
        self, query: str, limit: int, min_score: float,
        filter: Optional[dict], level: Optional[str] = None
    ) -> list[SearchResult]:
        """降级的文本搜索"""
        conn = self._get_connection()
        if conn is None:
            return []

        sql = "SELECT id, content, metadata FROM documents WHERE content LIKE ?"
        params: list[Any] = [f"%{query}%"]

        if level:
            sql += " AND json_extract(metadata, '$.level') = ?"
            params.append(level)

        sql += " LIMIT ?"
        params.append(limit * 2)

        cursor = conn.execute(sql, params)

        results = []
        for row in cursor.fetchall():
            metadata = json.loads(row[2]) if row[2] else {}
            if filter and not self._matches_filter(metadata, filter):
                continue
            results.append(
                SearchResult(
                    id=row[0],
                    content=row[1],
                    score=0.5,  # 文本匹配给默认分数
                    metadata=metadata,
                )
            )
            if len(results) >= limit:
                break

        return results

    def _matches_filter(self, metadata: dict, filter: dict) -> bool:
        """检查元数据是否匹配过滤条件（支持数组值）"""
        for key, value in filter.items():
            if key not in metadata:
                return False
            if isinstance(value, list):
                # 数组匹配：metadata[key] 在 value 列表中
                if metadata[key] not in value:
                    return False
            else:
                # 单值匹配
                if metadata[key] != value:
                    return False
        return True

    def get_l2_content(self, l1_id: str) -> Optional[str]:
        """
        从 L1 记录获取对应的 L2 原文内容

        Args:
            l1_id: L1 记录的 ID

        Returns:
            L2 原文内容，如果不存在则返回 None
        """
        conn = self._get_connection()
        if conn is None:
            return None

        # 查询 L1 记录
        cursor = conn.execute(
            "SELECT metadata FROM documents WHERE id = ?",
            (l1_id,)
        )
        row = cursor.fetchone()
        if not row:
            return None

        metadata = json.loads(row[0]) if row[0] else {}
        # 兼容两种命名：l2_pointer（规范）和 pointer（旧版）
        l2_id = metadata.get("l2_pointer") or metadata.get("pointer")

        if not l2_id:
            return None

        # 查询 L2 记录
        cursor = conn.execute(
            "SELECT content FROM documents WHERE id = ?",
            (l2_id,)
        )
        row = cursor.fetchone()
        return row[0] if row else None

    def close(self):
        """关闭数据库连接"""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def format_for_prompt(self, results: list[SearchResult]) -> str:
        """
        格式化搜索结果为提示词注入格式

        格式：
        ### [参考知识]
        1. xxx... (分数: 87)
        2. yyy... (分数: 73)
        ...
        """
        if not results:
            return ""

        lines = ["\n\n### [参考知识]"]
        for i, r in enumerate(results, 1):
            # 截断过长的内容
            content = r.content[:300] + "..." if len(r.content) > 300 else r.content
            # 分数转换为百分比
            score_pct = int(r.score * 100)
            lines.append(f"{i}. {content} (分数: {score_pct})")

        return "\n".join(lines)


# 全局实例
_vector_search: Optional[VectorSearchAdapter] = None


def get_vector_search(db_path: Optional[str] = None) -> VectorSearchAdapter:
    """获取全局向量搜索实例"""
    global _vector_search
    if _vector_search is None:
        _vector_search = VectorSearchAdapter(db_path)
    return _vector_search


def search_knowledge(query: str, limit: int = 10, min_score: float = 0.5) -> str:
    """
    便捷函数：搜索知识并返回格式化的提示词

    用于注入到 System Prompt
    """
    adapter = get_vector_search()
    results = adapter.search(query, limit, min_score)
    return adapter.format_for_prompt(results)
