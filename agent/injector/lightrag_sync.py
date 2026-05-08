"""
LightRAG Background Sync

Periodic backfill service that syncs photos and documents from local databases
into the LightRAG brain graph. Photos use lightrag_insert (ainsert)
so LightRAG auto-extracts entities, merges same-name nodes, and builds edges.

Architecture:
- Scans photos.db for photos with abstracts not yet in LightRAG
- Uses LightRAGIngester.lightrag_insert for photos
- Skills sync is handled by SkillSync (agent/injector/sync.py)
- Runs in a background daemon thread with configurable interval
"""

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from loguru import logger


class LightRAGSync:
    """LightRAG periodic backfill service.

    Scans photos.db and vectors.db, ingests any data not yet in LightRAG.
    Uses a status file to track the last sync time and avoid re-processing.
    """

    def __init__(self, sync_interval: int = 21600):
        self.sync_interval = sync_interval  # default 6 hours
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._status_file = Path.home() / ".niu" / "last_lightrag_sync.json"

    def run_sync(self) -> dict:
        """Execute one full sync cycle.

        Uses incremental/delta tracking: loads previously synced IDs from
        the status file, skips already-synced items, then persists the
        updated ID sets so subsequent runs only process new records.

        Returns:
            Stats dict with counts of synced items.
        """
        stats = {
            "photos_synced": 0,
            "persons_synced": 0,
            "documents_synced": 0,
            "skills_synced": 0,
            "tools_synced": 0,
            "errors": [],
        }

        # Load previously synced IDs for delta tracking
        prev_state = self._load_status()
        prev_photo_ids = set(prev_state.get("synced_photo_ids", []))
        prev_person_ids = set(prev_state.get("synced_person_ids", []))
        prev_doc_ids = set(prev_state.get("synced_doc_ids", []))
        prev_skill_ids = set(prev_state.get("synced_skill_ids", []))
        prev_tool_ids = set(prev_state.get("synced_tool_ids", []))
        prev_co_occ_ids = set(prev_state.get("synced_co_occ_ids", []))

        # 1. Sync photos from photos.db
        try:
            p, e, new_photo_ids, new_person_ids, new_co_occ_ids = self._sync_photos_db(
                prev_photo_ids, prev_person_ids, prev_co_occ_ids
            )
            stats["photos_synced"] = p
            stats["persons_synced"] = e
        except Exception as e:
            logger.warning(f"[LightRAGSync] photos.db sync failed: {e}")
            stats["errors"].append(f"photos: {e}")
            new_photo_ids = set()
            new_person_ids = set()
            new_co_occ_ids = set()

        # 2. Sync documents from vectors.db
        try:
            d, new_doc_ids = self._sync_vectors_db()
            stats["documents_synced"] = d
        except Exception as e:
            logger.warning(f"[LightRAGSync] vectors.db sync failed: {e}")
            stats["errors"].append(f"vectors: {e}")
            new_doc_ids = set()

        # 3. Sync skills and tools (delegated to SkillSync — returns zeros)
        try:
            skills_count, tools_count, new_skill_ids, new_tool_ids = self._sync_skills_and_tools()
            stats["skills_synced"] = skills_count
            stats["tools_synced"] = tools_count
        except Exception as e:
            logger.warning(f"[LightRAGSync] skills/tools sync failed: {e}")
            stats["errors"].append(f"skills_tools: {e}")
            new_skill_ids = set()
            new_tool_ids = set()

        # 4. Merge previous + newly synced IDs and save
        all_photo_ids = prev_photo_ids | new_photo_ids
        all_person_ids = prev_person_ids | new_person_ids
        all_doc_ids = prev_doc_ids | new_doc_ids
        all_skill_ids = prev_skill_ids | new_skill_ids
        all_tool_ids = prev_tool_ids | new_tool_ids
        all_co_occ_ids = prev_co_occ_ids | new_co_occ_ids
        self._save_status(stats, all_photo_ids, all_person_ids, all_doc_ids, all_skill_ids, all_tool_ids, all_co_occ_ids)

        logger.info(
            f"[LightRAGSync] Sync complete: "
            f"{stats['photos_synced']} photos, {stats['persons_synced']} persons, "
            f"{stats['documents_synced']} documents, "
            f"{stats['skills_synced']} skills, {stats['tools_synced']} tools | "
            f"tracked IDs: {len(all_photo_ids)} photos, {len(all_person_ids)} persons, "
            f"{len(all_doc_ids)} docs, {len(all_skill_ids)} skills, {len(all_tool_ids)} tools"
        )
        return stats

    def _sync_photos_db(
        self, prev_photo_ids: set, prev_person_ids: set, prev_co_occ_ids: set | None = None
    ) -> tuple[int, int, set, set, set]:
        """Sync photos from photos.db into LightRAG.

        Uses lightrag_insert (ainsert) so LightRAG auto-extracts entities,
        merges same-name nodes, and builds edges. All new items are collected
        into a single structured text paragraph and inserted in one call,
        avoiding N separate ainsert calls that would block the event loop.

        Only processes items whose IDs are not in the previous sync state,
        preventing duplicate ingestion.

        Args:
            prev_photo_ids: Photo IDs already synced in previous runs.
            prev_person_ids: Person IDs already synced in previous runs.

        Returns:
            (photos_synced, persons_synced, new_photo_ids, new_person_ids, new_co_occ_ids)
        """
        try:
            from niu_photo_server import get_db_path as get_photo_db_path
            photos_db_path = Path(get_photo_db_path())
        except (ImportError, ValueError) as e:
            logger.warning(f"[LightRAGSync] Cannot resolve photos.db path: {e}")
            return 0, 0, set(), set(), set()
        if not photos_db_path.exists():
            return 0, 0, set(), set(), set()

        from niu_api.internal.lightrag_adapter import LightRAGIngester

        ingester = LightRAGIngester()
        conn = sqlite3.connect(str(photos_db_path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row

        try:
            new_person_ids: set = set()
            new_photo_ids: set = set()
            new_co_occ_ids: set = set()
            photo_ids_to_mark: list[int] = []
            text_parts: list[str] = []

            # 1. Collect person descriptions (only new ones)
            rows = conn.execute("SELECT id, name, auto_label FROM persons").fetchall()
            for row in rows:
                person_id = row["id"]
                if person_id in prev_person_ids:
                    continue
                person_name = row["name"] or row["auto_label"] or person_id
                if person_name.startswith("未命名人物"):
                    text_parts.append(f"一位未命名人物（人物ID: {person_id}）。")
                else:
                    text_parts.append(f"人物ID为 {person_id} 的人，姓名为{person_name}。")
                new_person_ids.add(person_id)

            # 2. Collect photo descriptions (only new ones)
            rows = conn.execute(
                "SELECT p.id, p.file_path, p.abstract FROM photos p WHERE p.kg_synced = 0"
            ).fetchall()
            for row in rows:
                photo_id = row["id"]
                if photo_id in prev_photo_ids:
                    continue
                file_path = row["file_path"]
                abstract = row["abstract"] or ""
                if not file_path or not abstract:
                    continue
                title = Path(file_path).stem
                # Filter out "未命名人物" names from abstract
                safe_abstract = abstract
                if abstract:
                    parts = abstract.split("，")
                    if len(parts) > 1 and "未命名人物" in parts[0]:
                        safe_abstract = "，".join(parts[1:])
                # Build structured text
                photo_parts = [f"照片文件 {title}（照片ID: {file_path}）"]
                if safe_abstract:
                    photo_parts.append(safe_abstract)
                # Add person references from faces table
                face_rows = conn.execute(
                    "SELECT f.person_id, p.name, p.auto_label FROM faces f "
                    "LEFT JOIN persons p ON f.person_id = p.id "
                    "WHERE f.photo_id = ?",
                    (photo_id,),
                ).fetchall()
                if face_rows:
                    person_descs = []
                    for fr in face_rows:
                        pid = fr["person_id"]
                        pname = fr["name"] or fr["auto_label"] or ""
                        if pname.startswith("未命名人物"):
                            person_descs.append(f"一位未命名人物（人物ID: {pid}）")
                        else:
                            person_descs.append(f"{pname}（人物ID: {pid}）")
                    photo_parts.append(f"照片中出现的人物: {'、'.join(person_descs)}")
                text_parts.append("，".join(photo_parts) + "。")
                new_photo_ids.add(photo_id)
                photo_ids_to_mark.append(photo_id)

            # 3. Collect co_occurrence relationships (delta-tracked)
            prev_co_occ = prev_co_occ_ids or set()
            rows = conn.execute(
                "SELECT person_a_id, person_b_id, count FROM co_occurrences"
            ).fetchall()
            for row in rows:
                a_id = row["person_a_id"]
                b_id = row["person_b_id"]
                count = row["count"]
                pair_key = f"{min(a_id, b_id)}__{max(a_id, b_id)}"
                if pair_key in prev_co_occ:
                    continue
                text_parts.append(f"人物ID {a_id} 和人物ID {b_id} 在照片中共同出现了{count}次。")
                new_co_occ_ids.add(pair_key)

            # 4. Batch insert all collected text in one call
            persons_synced = 0
            photos_synced = 0
            if text_parts:
                combined_text = "\n".join(text_parts)
                try:
                    result = ingester.lightrag_insert(content=combined_text)
                    if result.get("status") == "ok":
                        persons_synced = len(new_person_ids)
                        photos_synced = len(new_photo_ids)
                        # Mark kg_synced=1 for all synced photos
                        for pid in photo_ids_to_mark:
                            try:
                                conn.execute("UPDATE photos SET kg_synced = 1 WHERE id = ?", (pid,))
                            except Exception as kg_err:
                                logger.warning(f"[LightRAGSync] Failed to mark kg_synced for photo {pid}: {kg_err}")
                        conn.commit()
                        logger.info(
                            f"[LightRAGSync] Synced {photos_synced} photos, "
                            f"{persons_synced} persons, {len(new_co_occ_ids)} co-occurrences "
                            f"via ainsert ({len(text_parts)} text segments)"
                        )
                    else:
                        logger.warning(
                            f"[LightRAGSync] ainsert returned status={result.get('status')}, "
                            f"not tracking {len(text_parts)} items"
                        )
                        # Clear new IDs since insert failed — items will be retried next run
                        new_person_ids.clear()
                        new_photo_ids.clear()
                        new_co_occ_ids.clear()
                except Exception as e:
                    logger.error(f"[LightRAGSync] Batch ainsert failed: {e}")
                    new_person_ids.clear()
                    new_photo_ids.clear()
                    new_co_occ_ids.clear()

            return photos_synced, persons_synced, new_photo_ids, new_person_ids, new_co_occ_ids
        finally:
            conn.close()

    def _sync_vectors_db(self) -> tuple[int, set]:
        """Sync documents from vectors.db into LightRAG.

        Returns:
            (documents_synced, new_doc_ids)
        """
        # vector-store deleted — vectors.db sync no longer available
        logger.info("[LightRAGSync] vectors.db sync skipped (vector-store removed)")
        return 0, set()  # removed: vector-store deleted
        
    def _sync_skills_and_tools(
        self,
    ) -> tuple[int, int, set, set]:
        """Skills and tools sync — delegated to SkillSync.

        SkillSync (agent/injector/sync) handles real-time skill synchronization
        with hash-based change detection, structured injection (inject_custom_kg),
        and deletion. This method delegates to SkillSync and returns actual counts.

        MCP Tools sync removed — tools are discovered via disk YAML in
        disk mode, not via LightRAG retrieval.
        """
        try:
            from agent.injector.sync import get_skill_sync
            skill_sync = get_skill_sync(auto_start=False)
            added, updated, deleted = skill_sync.scan_and_sync()
            skills_count = added + updated
            logger.info(f"[LightRAGSync] SkillSync: added={added}, updated={updated}, deleted={deleted}")
            return skills_count, 0, set(), set()
        except Exception as e:
            logger.warning(f"[LightRAGSync] SkillSync delegation failed: {e}")
            return 0, 0, set(), set()

    def _load_status(self) -> dict:
        """Load previous sync status from file.

        Returns:
            Dict with keys: last_sync, stats, synced_photo_ids,
            synced_person_ids, synced_doc_ids, synced_co_occ_ids.
            Returns empty dict if the file does not exist or cannot be parsed.
        """
        try:
            if self._status_file.exists():
                data = json.loads(self._status_file.read_text(encoding="utf-8"))
                # Ensure list fields exist (backward compat with old format)
                data.setdefault("synced_photo_ids", [])
                data.setdefault("synced_person_ids", [])
                data.setdefault("synced_doc_ids", [])
                data.setdefault("synced_skill_ids", [])
                data.setdefault("synced_tool_ids", [])
                data.setdefault("synced_co_occ_ids", [])
                return data
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"[LightRAGSync] Failed to load status: {e}")
        return {}

    def _save_status(
        self,
        stats: dict,
        synced_photo_ids: set,
        synced_person_ids: set,
        synced_doc_ids: set,
        synced_skill_ids: set | None = None,
        synced_tool_ids: set | None = None,
        synced_co_occ_ids: set | None = None,
    ):
        """Save sync status and tracked IDs to file.

        Args:
            stats: Sync statistics for this run.
            synced_photo_ids: All photo IDs synced so far (previous + new).
            synced_person_ids: All person IDs synced so far (previous + new).
            synced_doc_ids: All document IDs synced so far (previous + new).
            synced_skill_ids: All skill IDs synced so far (previous + new).
            synced_tool_ids: All tool IDs synced so far (previous + new).
            synced_co_occ_ids: All co-occurrence pair IDs synced so far (previous + new).
        """
        try:
            self._status_file.parent.mkdir(parents=True, exist_ok=True)
            status = {
                "last_sync": datetime.now().isoformat(),
                "stats": stats,
                "synced_photo_ids": sorted(synced_photo_ids),
                "synced_person_ids": sorted(synced_person_ids),
                "synced_doc_ids": sorted(synced_doc_ids),
                "synced_skill_ids": sorted(synced_skill_ids or set()),
                "synced_tool_ids": sorted(synced_tool_ids or set()),
                "synced_co_occ_ids": sorted(synced_co_occ_ids or set()),
            }
            self._status_file.write_text(
                json.dumps(status, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"[LightRAGSync] Failed to save status: {e}")

    def start_background_sync(self):
        """Start the background sync thread."""
        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._sync_loop, daemon=True)
        self._thread.start()
        logger.info(f"[LightRAGSync] Started background sync (interval: {self.sync_interval}s)")

    def stop_background_sync(self):
        """Stop the background sync thread."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _sync_loop(self):
        """Background sync loop.

        Runs first sync after 5-minute initial delay (to let other services
        start), then repeats every sync_interval seconds.
        """
        # Initial delay: 420s (staggered vs region_sync's 180s to avoid
        # both services competing for the LightRAG event loop simultaneously)
        self._stop_event.wait(420)
        while True:
            try:
                self.run_sync()
            except Exception as e:
                logger.error(f"[LightRAGSync] Sync loop error: {e}")
            if self._stop_event.wait(self.sync_interval):
                break


# Global instance + thread-safe lock
_lightrag_sync: Optional[LightRAGSync] = None
_lightrag_sync_lock = threading.Lock()


def get_lightrag_sync(sync_interval: int = 21600, auto_start: bool = False) -> LightRAGSync:
    """Get the global LightRAGSync instance."""
    global _lightrag_sync
    with _lightrag_sync_lock:
        if _lightrag_sync is None:
            _lightrag_sync = LightRAGSync(sync_interval)
            should_start = auto_start
        else:
            should_start = False
    if should_start:
        _lightrag_sync.start_background_sync()
    return _lightrag_sync