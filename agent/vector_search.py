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
import time
from typing import Any, Optional
from dataclasses import dataclass

import numpy as np


def resolve_vector_db_path() -> str:
    """
    统一向量库路径解析函数（唯一真实来源）。

    解析优先级：
    1. NIU_DB_PATH 环境变量（显式覆盖）
    2. WORKSPACE_PATH 环境变量（由 Go 启动器 main.go 设置）
    3. ~/.niu/memory.json 的 workspace.path → {workspace.path}/vectors.db
    4. 如果无法确定路径，抛出 ValueError（不降级、不创建流氓库）

    所有需要 vectors.db 路径的组件必须调用此函数，
    禁止各自硬编码或降级到 ~/.niu/vectors.db。
    """
    # 1. 显式覆盖
    if "NIU_DB_PATH" in os.environ:
        return os.environ["NIU_DB_PATH"]

    # 2. 环境变量（由 Go 启动器设置）
    if "WORKSPACE_PATH" in os.environ:
        ws = os.environ["WORKSPACE_PATH"]
        if not os.path.exists(ws):
            raise ValueError(
                f"WORKSPACE_PATH 指向不存在的目录: {ws}。"
                f"请检查目录是否已被删除或移动。"
            )
        return os.path.join(ws, "vectors.db")

    # 3. 从 ~/.niu/memory.json 读取 workspace.path
    memory_path = os.path.join(os.path.expanduser("~"), ".niu", "memory.json")
    if os.path.exists(memory_path):
        try:
            with open(memory_path, "r", encoding="utf-8") as f:
                memory = json.load(f)
        except Exception as e:
            raise ValueError(
                f"无法从 {memory_path} 解析 JSON: {e}。"
                f"请检查 memory.json 格式是否正确。"
            ) from e

        workspace_path = memory.get("workspace", {}).get("path")
        if workspace_path:
            if os.path.exists(workspace_path):
                return os.path.join(workspace_path, "vectors.db")
            raise ValueError(
                f"workspace.path 指向不存在的目录: {workspace_path}。"
                f"请检查目录是否已被删除或移动。"
            )

    raise ValueError(
        f"无法确定向量库路径：{memory_path} 不存在或缺少 workspace.path 配置。"
        f"请设置 WORKSPACE_PATH 环境变量，或在 ~/.niu/memory.json 中设置 workspace.path。"
    )


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

    def __init__(self, db_path: Optional[str] = None):
        if db_path:
            self.db_path: Optional[str] = db_path
        else:
            try:
                self.db_path = resolve_vector_db_path()
            except ValueError as e:
                import logging
                logging.getLogger(__name__).warning(f"向量库路径解析失败，向量搜索不可用: {e}")
                self.db_path = None
        self._conn: Optional[sqlite3.Connection] = None
        self._indexes_created: bool = False  # 索引创建标志

    @staticmethod
    def _default_db_path() -> str:
        """获取默认向量库路径（委托给统一路径解析函数）"""
        return resolve_vector_db_path()

    def _get_connection(self) -> Optional[sqlite3.Connection]:
        if self.db_path is None:
            return None
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

                # 记录递归信息
                recursion_depth = 4 - max_recursion
                print(f"[Recursive Query] {query} → {refined} (depth: {recursion_depth}/3)",
                      file=sys.stderr, flush=True)

                # 第二轮检索，排除查询模式
                results = self._search_once(
                    query=refined,
                    limit=limit,
                    min_score=min_score,
                    filter=None,  # 不过滤，在后面排除
                    level=level
                )

                # 排除查询模式
                filtered_results = [
                    r for r in results
                    if r.metadata.get("type") != "query_pattern"
                ]

                return filtered_results

        # 没有递归标记，直接返回
        return results

    def upsert_interaction_habit(
        self,
        habit_type: str,
        content: str,
        metadata: dict,
        habit_id: Optional[str] = None
    ) -> bool:
        """
        写入或更新 Interaction Habit 到向量库

        Args:
            habit_type: habit type (tool_dialect/user_state/user_profile)
            content: 习惯内容
            metadata: 必须包含 confidence, source, level="l1", category="interaction_habit"
            habit_id: 可选，指定 ID（格式: {type}:{subtype}:{counter}）

        Returns:
            是否成功
        """
        if habit_id is None:
            counter = int(time.time() * 1000) % 100000
            habit_id = f"habit:{habit_type}:{counter}"

        full_metadata = {
            "level": "l1",
            "category": "interaction_habit",
            **metadata
        }

        embedding = self._get_embedding(content)
        if embedding is None:
            return False

        vec = np.array(embedding, dtype=np.float32)
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        embedding_blob = vec.tobytes()

        conn = self._get_connection()
        if conn is None:
            return False

        conn.execute(
            """
            INSERT INTO documents (id, content, embedding, metadata)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                content = excluded.content,
                embedding = excluded.embedding,
                metadata = excluded.metadata
            """,
            (habit_id, content, embedding_blob, json.dumps(full_metadata, ensure_ascii=False)),
        )
        conn.commit()
        return True

    def search_interaction_habits(
        self,
        query: str,
        habit_type: str = None,
        limit: int = 5,
        min_score: float = 0.4
    ) -> list:
        """
        检索 Interaction Habits

        Args:
            query: 搜索内容
            habit_type: 筛选特定类型（tool_dialect/user_state/user_profile）
            limit: 返回数量
            min_score: 最低分数

        Returns:
            匹配的 SearchResult 列表
        """
        filter_dict = {"level": "l1", "category": "interaction_habit"}
        if habit_type:
            filter_dict["type"] = habit_type
        return self.search(query, limit=limit, min_score=min_score, filter=filter_dict)

    def update_habit_confidence(
        self,
        habit_id: str,
        result: str
    ) -> bool:
        """
        更新 Interaction Habit 的置信度

        Args:
            habit_id: habit 记录 ID
            result: 调用结果 ("success" | "fail")

        Returns:
            是否成功
        """
        conn = self._get_connection()
        if conn is None:
            return False

        row = conn.execute(
            "SELECT metadata FROM documents WHERE id = ?", (habit_id,)
        ).fetchone()
        if not row:
            return False

        metadata = json.loads(row[0])
        conf = metadata.get("confidence", {})

        if result == "success":
            conf["success_count"] = conf.get("success_count", 0) + 1
        elif result == "fail":
            conf["fail_count"] = conf.get("fail_count", 0) + 1

        conf["last_used"] = time.strftime("%Y-%m-%d")
        metadata["confidence"] = conf

        if conf.get("fail_count", 0) >= 3:
            conn.execute("DELETE FROM documents WHERE id = ?", (habit_id,))
            conn.commit()
            print(f"[InteractionHabits] Deleted low-confidence habit: {habit_id}", flush=True)
            return True

        conn.execute(
            "UPDATE documents SET metadata = ? WHERE id = ?",
            (json.dumps(metadata, ensure_ascii=False), habit_id)
        )
        conn.commit()
        return True

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
                doc_norm = np.linalg.norm(doc_vec)
                if doc_norm == 0:
                    continue
                query_norm = np.linalg.norm(query_vec)
                if query_norm == 0:
                    break
                score = float(np.dot(query_vec, doc_vec) / (query_norm * doc_norm))

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

    def search_multi(
        self, query: str, categories: dict, level: str = "l1",
        enable_recursion: bool = False,
    ) -> dict[str, list[SearchResult]]:
        """
        一次向量检索，按 category 分组返回。避免同一 query 多次 embedding 计算。

        Args:
            query: 搜索查询
            categories: {category_name: {"limit": int, "min_score": float}, ...}
            level: 层级过滤（默认 l1）
            enable_recursion: 是否启用递归检索（query_pattern -> refined_query）

        Returns:
            {category_name: [SearchResult, ...], ...}

        Example:
            results = vs.search_multi(
                query="上网查新闻",
                categories={
                    "skill": {"limit": 3, "min_score": 0.35},
                    "mcp_tool": {"limit": 10, "min_score": 0.25},
                    "document": {"limit": 8, "min_score": 0.45},
                    "interaction_habit": {"limit": 3, "min_score": 0.4},
                }
            )
        """
        conn = self._get_connection()
        if conn is None:
            return {cat: [] for cat in categories}

        # 1. 算一次 embedding
        query_embedding = self._get_embedding(query)
        if not query_embedding:
            return {cat: [] for cat in categories}

        query_vec = np.array(query_embedding, dtype=np.float32)
        query_norm = np.linalg.norm(query_vec)
        if query_norm == 0:
            return {cat: [] for cat in categories}

        # 2. 一次查出所有 l1 记录
        sql = "SELECT id, content, embedding, metadata FROM documents WHERE embedding IS NOT NULL AND json_extract(metadata, '$.level') = ?"
        cursor = conn.execute(sql, [level])
        docs = cursor.fetchall()

        if not docs:
            return {cat: [] for cat in categories}

        # 3. 计算相似度，按 category 分桶
        buckets: dict[str, list] = {cat: [] for cat in categories}
        query_pattern_hits: list[tuple[float, str, str, dict]] = []
        for doc_id, content, embedding_blob, metadata_json in docs:
            if not embedding_blob:
                continue
            metadata = json.loads(metadata_json) if metadata_json else {}
            cat = metadata.get("category", "")
            if cat not in categories:
                # 递归检索：临时收集 query_pattern 匹配结果（不放入 buckets）
                if enable_recursion and cat == "query_pattern":
                    doc_vec = np.frombuffer(embedding_blob, dtype=np.float32)
                    doc_norm = np.linalg.norm(doc_vec)
                    if doc_norm == 0:
                        continue
                    score = float(np.dot(query_vec, doc_vec) / (query_norm * doc_norm))
                    if score >= 0.3 and metadata.get("is_recursive") is True:
                        query_pattern_hits.append((score, doc_id, content, metadata))
                continue

            doc_vec = np.frombuffer(embedding_blob, dtype=np.float32)
            doc_norm = np.linalg.norm(doc_vec)
            if doc_norm == 0:
                continue
            score = float(np.dot(query_vec, doc_vec) / (query_norm * doc_norm))

            cfg = categories[cat]
            if score >= cfg["min_score"]:
                buckets[cat].append((score, doc_id, content, metadata))

        # 4. 各桶排序 + 截断
        results: dict[str, list[SearchResult]] = {}
        for cat, items in buckets.items():
            items.sort(key=lambda x: -x[0])
            cfg = categories[cat]
            results[cat] = [
                SearchResult(id=doc_id, content=content, score=score, metadata=metadata)
                for score, doc_id, content, metadata in items[:cfg["limit"]]
            ]

        # 5. 递归检索：用 query_pattern 的 refined_query 对 target_category 做递归检索
        #    数据驱动：二次检索结果可能又触发 is_recursive=True，需要继续递归
        #    深度限制：最多 3 轮递归（与旧 search() 的 max_recursion=3 一致）
        #    合并策略：保留原始基础结果，累积所有递归结果，循环结束后一次性合并截断
        if enable_recursion and query_pattern_hits:
            target_category = "mcp_tool"
            if target_category in results:
                max_recursion_depth = 3
                current_query = query
                current_hits = query_pattern_hits
                consumed_qp_ids: set[str] = set()
                # 保存原始基础结果，循环内只累积递归结果
                base_mcp_results = list(results[target_category])
                all_recursive_results: list[tuple[float, str, str, dict]] = []
                for depth in range(1, max_recursion_depth + 1):
                    current_hits.sort(key=lambda x: -x[0])
                    best_doc_id = current_hits[0][1]
                    best_qp_meta = current_hits[0][3]
                    refined_query = best_qp_meta.get("refined_query", "")
                    if not refined_query:
                        break
                    consumed_qp_ids.add(best_doc_id)

                    refined_embedding = self._get_embedding(refined_query)
                    if not refined_embedding:
                        break
                    refined_vec = np.array(refined_embedding, dtype=np.float32)
                    refined_norm = np.linalg.norm(refined_vec)
                    if refined_norm == 0:
                        break

                    # 对 target_category 桶做检索，同时收集新的 query_pattern 触发
                    second_pass: list[tuple[float, str, str, dict]] = []
                    next_qp_hits: list[tuple[float, str, str, dict]] = []
                    for doc_id, content, embedding_blob, metadata_json in docs:
                        if not embedding_blob:
                            continue
                        metadata = json.loads(metadata_json) if metadata_json else {}
                        doc_cat = metadata.get("category", "")

                        # 收集下一轮递归的 query_pattern 触发（跳过已消费的）
                        if doc_cat == "query_pattern" and metadata.get("is_recursive") is True:
                            if doc_id in consumed_qp_ids:
                                continue
                            doc_vec = np.frombuffer(embedding_blob, dtype=np.float32)
                            doc_norm = np.linalg.norm(doc_vec)
                            if doc_norm == 0:
                                continue
                            score = float(np.dot(refined_vec, doc_vec) / (refined_norm * doc_norm))
                            if score >= 0.3:
                                next_qp_hits.append((score, doc_id, content, metadata))
                            continue

                        # 只检索 target_category
                        if doc_cat != target_category:
                            continue
                        if metadata.get("type") == "query_pattern":
                            continue
                        doc_vec = np.frombuffer(embedding_blob, dtype=np.float32)
                        doc_norm = np.linalg.norm(doc_vec)
                        if doc_norm == 0:
                            continue
                        score = float(np.dot(refined_vec, doc_vec) / (refined_norm * doc_norm))
                        cfg = categories.get(target_category, {})
                        min_score = cfg.get("min_score", 0.0)
                        if score >= min_score:
                            second_pass.append((score, doc_id, content, metadata))

                    all_recursive_results.extend(second_pass)
                    print(f"[Recursive Query] depth={depth}/{max_recursion_depth} query={current_query!r} -> refined_query={refined_query!r} target_category={target_category!r} hits={len(second_pass)}", file=sys.stderr, flush=True)

                    # 检查是否需要继续递归
                    if not next_qp_hits:
                        break
                    current_query = refined_query
                    current_hits = next_qp_hits

                # 循环结束后：基础结果 + 所有递归结果一次性合并截断
                # 同 doc_id 取 max(base_score, recursive_score)
                best_scores: dict[str, tuple[float, str, str, dict]] = {}
                for r in base_mcp_results:
                    best_scores[r.id] = (r.score, r.id, r.content, r.metadata)
                for score, doc_id, content, metadata in all_recursive_results:
                    if doc_id in best_scores:
                        if score > best_scores[doc_id][0]:
                            best_scores[doc_id] = (score, doc_id, content, metadata)
                    else:
                        best_scores[doc_id] = (score, doc_id, content, metadata)
                merged = sorted(best_scores.values(), key=lambda x: -x[0])
                limit = categories.get(target_category, {}).get("limit", 10)
                results[target_category] = [
                    SearchResult(id=doc_id, content=content, score=score, metadata=metadata)
                    for score, doc_id, content, metadata in merged[:limit]
                ]

        return results

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
