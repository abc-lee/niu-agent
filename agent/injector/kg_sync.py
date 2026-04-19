"""
KG Batch Sync

知识图谱定期批量整理服务。补建 KG 中缺失的 Document/Entity 节点，
清理孤立节点，更新关系强度。按 SkillSync 模式实现。
"""

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from loguru import logger


class KGSync:
    """
    知识图谱定期批量整理服务

    扫描 photos.db 和 vectors.db，补建 KG 中缺失的节点和边，
    清理孤立节点，更新关系强度。
    """

    def __init__(self, sync_interval: int = 21600):
        self.sync_interval = sync_interval  # 默认 6 小时
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._status_file = Path.home() / ".niu" / "last_kg_sync.json"

    def run_sync(self) -> dict:
        """执行一次完整的 KG 批量整理。

        Returns:
            统计信息字典
        """
        stats = {
            "photos_synced": 0,
            "persons_synced": 0,
            "vectors_synced": 0,
            "orphan_nodes_removed": 0,
            "errors": [],
        }

        try:
            from niu_kg_server import (
                create_document, create_entity, link_document_entity,
                link_entities, get_connection,
            )
        except ImportError:
            logger.warning("[KGSync] niu_kg_server not available, skipping")
            return stats

        # 1. 从 photos.db 补建 KG 节点
        try:
            p, e = self._sync_photos_db(create_document, create_entity, link_document_entity, link_entities, get_connection)
            stats["photos_synced"] = p
            stats["persons_synced"] = e
        except Exception as e:
            logger.warning(f"[KGSync] photos.db sync failed: {e}")
            stats["errors"].append(f"photos: {e}")

        # 2. 从 vectors.db 补建 KG Document 节点
        try:
            v = self._sync_vectors_db(create_document, get_connection)
            stats["vectors_synced"] = v
        except Exception as e:
            logger.warning(f"[KGSync] vectors.db sync failed: {e}")
            stats["errors"].append(f"vectors: {e}")

        # 3. 清理孤立节点
        try:
            removed = self._cleanup_orphans(get_connection)
            stats["orphan_nodes_removed"] = removed
        except Exception as e:
            logger.warning(f"[KGSync] orphan cleanup failed: {e}")
            stats["errors"].append(f"orphans: {e}")

        # 4. 保存同步时间戳
        self._save_status(stats)

        logger.info(
            f"[KGSync] Batch sync complete: "
            f"{stats['photos_synced']} photos, {stats['persons_synced']} persons, "
            f"{stats['vectors_synced']} vectors, "
            f"{stats['orphan_nodes_removed']} orphans removed"
        )
        return stats

    def _sync_photos_db(self, create_document, create_entity, link_document_entity, link_entities, get_connection) -> tuple[int, int]:
        """从 photos.db 补建 KG 节点（仅补建缺失的）。

        Returns:
            (photos_synced, persons_synced)
        """
        try:
            from niu_photo_server import get_db_path as get_photo_db_path
            photos_db_path = Path(get_photo_db_path())
        except (ImportError, ValueError) as e:
            logger.warning(f"[KGSync] Cannot resolve photos.db path: {e}")
            return 0, 0
        if not photos_db_path.exists():
            return 0, 0

        # 获取 KG 中已有节点，避免重复创建
        kg_conn = get_connection()
        existing_person_ids = set()
        existing_doc_uris = set()
        try:
            result = kg_conn.execute("MATCH (e:Entity) WHERE e.type = 'person' RETURN e.id")
            for row in result:
                existing_person_ids.add(row[0])
        except Exception:
            pass
        try:
            result = kg_conn.execute("MATCH (d:Document) WHERE d.source = 'photo' RETURN d.uri")
            for row in result:
                existing_doc_uris.add(row[0])
        except Exception:
            pass

        conn = sqlite3.connect(str(photos_db_path))
        conn.row_factory = sqlite3.Row

        try:
            # 补建 person Entity 节点（仅缺失的）
            persons_synced = 0
            rows = conn.execute("SELECT id, name, auto_label FROM persons").fetchall()
            for row in rows:
                person_id = row["id"]
                entity_id = f"person:{person_id}"
                if entity_id in existing_person_ids:
                    continue
                person_name = row["name"] or row["auto_label"] or person_id
                try:
                    create_entity(
                        id=entity_id, name=person_name,
                        entity_type="person", description="Backfilled from photos.db",
                    )
                    persons_synced += 1
                except Exception as e:
                    logger.debug(f"[KGSync] Person entity failed for {person_name}: {e}")

            # 补建 photo Document 节点 + MENTIONS 边（仅缺失的）
            photos_synced = 0
            rows = conn.execute(
                "SELECT p.id, p.file_path, p.abstract, f.person_id "
                "FROM photos p LEFT JOIN faces f ON p.id = f.photo_id"
            ).fetchall()

            # 按 photo_id 分组
            photo_map: dict[str, dict] = {}
            for row in rows:
                pid = row["id"]
                if pid not in photo_map:
                    photo_map[pid] = {
                        "file_path": row["file_path"],
                        "abstract": row["abstract"] or "",
                        "person_ids": [],
                    }
                if row["person_id"]:
                    photo_map[pid]["person_ids"].append(row["person_id"])

            for pid, info in photo_map.items():
                file_path = info["file_path"]
                if not file_path or file_path in existing_doc_uris:
                    continue
                try:
                    title = Path(file_path).stem
                    create_document(uri=file_path, title=title, content=info["abstract"], source="photo")
                    for person_id in info["person_ids"]:
                        link_document_entity(
                            doc_uri=file_path,
                            entity_id=f"person:{person_id}",
                            confidence=0.7,
                        )
                    photos_synced += 1
                except Exception as e:
                    logger.debug(f"[KGSync] Photo document failed for {file_path}: {e}")

            # 补建 co_occurrence RELATED_TO 边
            rows = conn.execute(
                "SELECT person_a_id, person_b_id, count FROM co_occurrences"
            ).fetchall()
            for row in rows:
                a_id = row["person_a_id"]
                b_id = row["person_b_id"]
                count = row["count"]
                try:
                    link_entities(
                        entity1_id=f"person:{a_id}",
                        entity2_id=f"person:{b_id}",
                        relation="co_appears_with",
                        confidence=min(0.3 + count * 0.05, 0.9),
                    )
                except Exception as e:
                    logger.debug(f"[KGSync] Co-occurrence link failed: {e}")

            return photos_synced, persons_synced
        finally:
            conn.close()

    def _sync_vectors_db(self, create_document, get_connection) -> int:
        """从 vectors.db 补建 KG Document 节点（仅 category=document 的记录）。

        Returns:
            vectors_synced
        """
        from agent.vector_search import resolve_vector_db_path
        try:
            vectors_db_path = Path(resolve_vector_db_path())
        except ValueError as e:
            logger.warning(f"[KGSync] Cannot resolve vectors.db path: {e}")
            return 0
        if not vectors_db_path.exists():
            return 0

        conn = sqlite3.connect(str(vectors_db_path))
        conn.row_factory = sqlite3.Row

        try:
            # 获取 KG 中已有 Document URI
            kg_conn = get_connection()
            existing_uris = set()
            try:
                result = kg_conn.execute("MATCH (d:Document) RETURN d.uri")
                for row in result:
                    existing_uris.add(row[0])
            except Exception:
                pass

            # 查找所有向量记录，按 metadata 精确过滤
            rows = conn.execute(
                "SELECT id, content, metadata FROM documents"
            ).fetchall()

            synced = 0
            for row in rows:
                doc_id = row["id"]
                content = row["content"] or ""
                metadata_str = row["metadata"] or "{}"
                try:
                    metadata = json.loads(metadata_str)
                except Exception:
                    metadata = {}

                # 精确过滤：仅同步 category=document 的记录
                if metadata.get("category") != "document":
                    continue

                # 已过滤 category=document，KG source 固定为 "document"
                source = "document"
                file_path = metadata.get("file_path", doc_id)

                # 跳过 KG 中已有的
                if file_path in existing_uris:
                    continue

                title = Path(file_path).stem if file_path else doc_id
                try:
                    create_document(uri=file_path, title=title, content=content, source=source or "document")

                    # 从 L1 提取实体（同 sync_to_kg 逻辑）
                    parts = content.split("|")
                    if len(parts) >= 4:
                        entity_str = parts[3].strip()
                        if entity_str:
                            from niu_kg_server import create_entity as _create_entity, link_document_entity as _link_de
                            for pair in entity_str.split(","):
                                pair = pair.strip()
                                if ":" in pair:
                                    name, etype = pair.rsplit(":", 1)
                                    name = name.strip()
                                    etype = etype.strip().lower()
                                else:
                                    name = pair.strip()
                                    etype = "other"
                                if not name:
                                    continue
                                entity_id = f"{etype}:{name}"
                                try:
                                    _create_entity(
                                        id=entity_id, name=name,
                                        entity_type=etype, description=f"Backfilled from {title}",
                                    )
                                    _link_de(
                                        doc_uri=file_path, entity_id=entity_id, confidence=0.7,
                                    )
                                except Exception:
                                    pass

                    synced += 1
                except Exception as e:
                    logger.debug(f"[KGSync] Vector document failed for {doc_id}: {e}")

            return synced
        finally:
            conn.close()

    def _cleanup_orphans(self, get_connection) -> int:
        """清理无任何边的孤立节点（批量删除）。

        Returns:
            removed count
        """
        conn = get_connection()
        removed = 0

        # 批量清理孤立 Entity 节点
        try:
            result = conn.execute("MATCH (e:Entity) WHERE NOT (e)--() DELETE e")
            # KuzuDB 返回删除行数
            removed += len(list(result)) if result else 0
        except Exception as e:
            # 如果 NOT (e)--() 语法不支持，降级为逐个删除
            logger.debug(f"[KGSync] Batch entity delete failed, trying one-by-one: {e}")
            try:
                result = conn.execute("MATCH (e:Entity) WHERE NOT (e)--() RETURN e.id")
                orphan_ids = [row[0] for row in result]
                for oid in orphan_ids:
                    conn.execute("MATCH (e:Entity {id: $id}) DELETE e", {"id": oid})
                    removed += 1
                if orphan_ids:
                    logger.info(f"[KGSync] Removed {len(orphan_ids)} orphan Entity nodes")
            except Exception as e2:
                logger.warning(f"[KGSync] Entity orphan cleanup failed: {e2}")

        # 批量清理孤立 Document 节点
        try:
            result = conn.execute("MATCH (d:Document) WHERE NOT (d)--() DELETE d")
            removed += len(list(result)) if result else 0
        except Exception as e:
            logger.debug(f"[KGSync] Batch document delete failed, trying one-by-one: {e}")
            try:
                result = conn.execute("MATCH (d:Document) WHERE NOT (d)--() RETURN d.uri")
                orphan_uris = [row[0] for row in result]
                for uri in orphan_uris:
                    conn.execute("MATCH (d:Document {uri: $uri}) DELETE d", {"uri": uri})
                    removed += 1
                if orphan_uris:
                    logger.info(f"[KGSync] Removed {len(orphan_uris)} orphan Document nodes")
            except Exception as e2:
                logger.warning(f"[KGSync] Document orphan cleanup failed: {e2}")

        return removed

    def _save_status(self, stats: dict):
        """保存同步状态"""
        try:
            self._status_file.parent.mkdir(parents=True, exist_ok=True)
            status = {
                "last_sync": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "stats": stats,
            }
            self._status_file.write_text(
                json.dumps(status, ensure_ascii=False, indent=2)
            )
        except Exception as e:
            logger.warning(f"[KGSync] Failed to save status: {e}")

    def start_background_sync(self):
        """启动后台批量整理"""
        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._sync_loop, daemon=True)
        self._thread.start()
        logger.info(f"[KGSync] Started background sync (interval: {self.sync_interval}s)")

    def stop_background_sync(self):
        """停止后台批量整理"""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _sync_loop(self):
        """后台同步循环"""
        # 首次启动延时 5 分钟（等待其他服务就绪）
        self._stop_event.wait(300)
        while not self._stop_event.wait(self.sync_interval):
            try:
                self.run_sync()
            except Exception as e:
                logger.error(f"[KGSync] Sync loop error: {e}")


# 全局实例 + 线程安全锁
_kg_sync: Optional[KGSync] = None
_kg_sync_lock = threading.Lock()


def get_kg_sync(sync_interval: int = 21600, auto_start: bool = False) -> KGSync:
    """获取全局 KGSync 实例"""
    global _kg_sync
    with _kg_sync_lock:
        if _kg_sync is None:
            _kg_sync = KGSync(sync_interval)
            if auto_start:
                _kg_sync.start_background_sync()
    return _kg_sync
