"""
LightRAG Background Sync

Periodic backfill service that syncs photos and documents from local databases
into the LightRAG brain graph. Replaces the old KGSync (KuzuDB-based) with
LightRAG ainsert() for unstructured data and ainsert_custom_kg() for structured.

Architecture:
- Scans photos.db for photos with abstracts not yet in LightRAG
- Scans vectors.db for documents not yet in LightRAG
- Uses LightRAGIngester for structured person entities and relations
- Uses LightRAGPipeline for unstructured document ingestion
- Runs in a background daemon thread with configurable interval
"""

import json
import sqlite3
import threading
import time
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
            d, new_doc_ids = self._sync_vectors_db(prev_doc_ids)
            stats["documents_synced"] = d
        except Exception as e:
            logger.warning(f"[LightRAGSync] vectors.db sync failed: {e}")
            stats["errors"].append(f"vectors: {e}")
            new_doc_ids = set()

        # 3. Sync skills and tools into LightRAG (incremental)
        try:
            skills_count, tools_count, new_skill_ids, new_tool_ids = self._sync_skills_and_tools(
                prev_skill_ids, prev_tool_ids
            )
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

        Only processes items whose IDs are not in the previous sync state,
        preventing duplicate entity/relationship insertion.

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
        from niu_api.internal.lightrag_manager import call_async, get_lightrag

        rag = get_lightrag()
        if rag is None:
            logger.warning("[LightRAGSync] LightRAG not available")
            return 0, 0, set(), set(), set()

        ingester = LightRAGIngester()
        conn = sqlite3.connect(str(photos_db_path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row

        try:
            # Sync person entities (only new ones)
            persons_synced = 0
            new_person_ids: set = set()
            rows = conn.execute("SELECT id, name, auto_label FROM persons").fetchall()
            for row in rows:
                person_id = row["id"]
                if person_id in prev_person_ids:
                    continue
                person_name = row["name"] or row["auto_label"] or person_id
                try:
                    ingester.inject_entity(
                        name=f"person:{person_id}",
                        entity_type="person",
                        description=f"Person: {person_name} (backfilled from photos.db)",
                    )
                    persons_synced += 1
                    new_person_ids.add(person_id)
                except Exception as e:
                    logger.debug(f"[LightRAGSync] Person entity failed for {person_name}: {e}")

            # Sync photo documents (only new ones with abstracts)
            photos_synced = 0
            new_photo_ids: set = set()
            rows = conn.execute("SELECT id, file_path, abstract FROM photos").fetchall()
            for row in rows:
                photo_id = row["id"]
                if photo_id in prev_photo_ids:
                    continue
                file_path = row["file_path"]
                abstract = row["abstract"] or ""
                if not file_path or not abstract:
                    continue
                try:
                    title = Path(file_path).stem
                    content = f"[Photo: {title}]\n{abstract}"
                    call_async(rag.ainsert(content, file_paths=[file_path]))
                    photos_synced += 1
                    new_photo_ids.add(photo_id)
                except Exception as e:
                    logger.debug(f"[LightRAGSync] Photo document failed for {file_path}: {e}")

            # Sync co_occurrence relations (delta-tracked to avoid re-injection)
            prev_co_occ = prev_co_occ_ids or set()
            new_co_occ_ids: set = set()
            rows = conn.execute(
                "SELECT person_a_id, person_b_id, count FROM co_occurrences"
            ).fetchall()
            for row in rows:
                a_id = row["person_a_id"]
                b_id = row["person_b_id"]
                count = row["count"]
                # Stable pair key: always sort so (a,b) == (b,a)
                pair_key = f"{min(a_id, b_id)}__{max(a_id, b_id)}"
                if pair_key in prev_co_occ:
                    continue
                try:
                    ingester.inject_relation(
                        src_id=f"person:{a_id}",
                        tgt_id=f"person:{b_id}",
                        relation="co_appears_with",
                        description=f"Co-occurrence count: {count}",
                    )
                    new_co_occ_ids.add(pair_key)
                except Exception as e:
                    logger.debug(f"[LightRAGSync] Co-occurrence link failed: {e}")

            return photos_synced, persons_synced, new_photo_ids, new_person_ids, new_co_occ_ids
        finally:
            conn.close()

    def _sync_vectors_db(self, prev_doc_ids: set) -> tuple[int, set]:
        """Sync documents from vectors.db into LightRAG.

        Only processes documents whose IDs are not in the previous sync state,
        preventing duplicate ingestion.

        Args:
            prev_doc_ids: Document IDs already synced in previous runs.

        Returns:
            (documents_synced, new_doc_ids)
        """
        from agent.vector_search import resolve_vector_db_path
        try:
            vectors_db_path = Path(resolve_vector_db_path())
        except ValueError as e:
            logger.warning(f"[LightRAGSync] Cannot resolve vectors.db path: {e}")
            return 0, set()
        if not vectors_db_path.exists():
            return 0, set()

        from niu_api.internal.lightrag_manager import call_async, get_lightrag

        rag = get_lightrag()
        if rag is None:
            return 0, set()

        conn = sqlite3.connect(str(vectors_db_path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row

        try:
            rows = conn.execute(
                "SELECT id, content, metadata FROM documents"
            ).fetchall()

            synced = 0
            new_doc_ids: set = set()
            for row in rows:
                doc_id = row["id"]
                if doc_id in prev_doc_ids:
                    continue
                content = row["content"] or ""
                metadata_str = row["metadata"] or "{}"
                try:
                    metadata = json.loads(metadata_str)
                except Exception:
                    metadata = {}

                # Only sync category=document
                if metadata.get("category") != "document":
                    continue

                file_path = metadata.get("file_path", doc_id)
                title = Path(file_path).stem if file_path else doc_id

                try:
                    prefixed = f"[Document: {title}]\n{content}"
                    call_async(rag.ainsert(prefixed, file_paths=[file_path]))
                    synced += 1
                    new_doc_ids.add(doc_id)
                except Exception as e:
                    logger.debug(f"[LightRAGSync] Vector document failed for {doc_id}: {e}")

            return synced, new_doc_ids
        finally:
            conn.close()

    def _sync_skills_and_tools(
        self, prev_skill_ids: set, prev_tool_ids: set
    ) -> tuple[int, int, set, set]:
        """Sync skills and MCP tools into LightRAG knowledge graph.

        Only processes items whose IDs are not in the previous sync state,
        preventing duplicate entity insertion. New IDs are tracked and
        returned so they can be persisted in the status file.

        Args:
            prev_skill_ids: Skill IDs already synced in previous runs.
            prev_tool_ids: Tool IDs already synced in previous runs.

        Returns:
            (skills_synced, tools_synced, new_skill_ids, new_tool_ids)
        """
        skills_synced = 0
        tools_synced = 0
        new_skill_ids: set = set()
        new_tool_ids: set = set()

        # --- Skills ---
        try:
            from niu_api.internal.lightrag_adapter import LightRAGIngester

            ingester = LightRAGIngester()

            # Read skills from the same directory SkillSync uses
            base_dir = Path(__file__).parent.parent.parent
            skills_dir = base_dir / "memory" / "skills"

            if skills_dir.exists():
                for skill_file in skills_dir.glob("*.md"):
                    name = skill_file.stem
                    skill_id = f"skill:{name}"
                    if skill_id in prev_skill_ids:
                        continue
                    try:
                        content = skill_file.read_text(encoding="utf-8")
                        # Extract description (same logic as SkillSync)
                        import re
                        description = ""
                        match_l1 = re.search(r"\*\*[lL]1 摘要\*\*[：:]\s*(.+)", content)
                        if match_l1:
                            description = match_l1.group(1).strip()
                        if not description:
                            for line in content.strip().split("\n"):
                                if line.strip().startswith("# "):
                                    description = line.strip()[2:].strip()
                                    break
                        if not description:
                            match_d = re.search(r"description:\s*(.+)", content, re.IGNORECASE)
                            if match_d:
                                description = match_d.group(1).strip().strip("\"'")

                        if description:
                            result = ingester.inject_entity(
                                name=f"skill:{name}",
                                entity_type="skill",
                                description=description,
                                source_id=f"skill:{name}",
                                chunk_content=f"{name}: {description}",
                                file_path=f"skill://{name}",
                            )
                            if result.get("status") == "ok":
                                skills_synced += 1
                                new_skill_ids.add(skill_id)
                    except Exception as e:
                        logger.debug(f"[LightRAGSync] Skill inject failed for '{name}': {e}")

            if skills_synced:
                logger.info(f"[LightRAGSync] Synced {skills_synced} skills into LightRAG")
        except Exception as e:
            logger.debug(f"[LightRAGSync] Skills sync skipped: {e}")

        # --- MCP Tools ---
        try:
            from niu_api.internal.lightrag_adapter import LightRAGIngester
            from agent.tool_registry import get_registry

            ingester = LightRAGIngester()
            registry = get_registry()

            for full_name, schema in registry.get_all_schemas().items():
                if "/" not in full_name:
                    continue
                tool_id = f"tool:{full_name}"
                if tool_id in prev_tool_ids:
                    continue
                tool_name = full_name.split("/", 1)[1]
                description = schema.get("description", "")
                if not description:
                    continue
                try:
                    result = ingester.inject_entity(
                        name=f"tool:{full_name}",
                        entity_type="tool",
                        description=description,
                        source_id=f"tool:{full_name}",
                        chunk_content=f"{tool_name}: {description}",
                        file_path=f"mcp://{full_name}",
                    )
                    if result.get("status") == "ok":
                        tools_synced += 1
                        new_tool_ids.add(tool_id)
                except Exception as e:
                    logger.debug(f"[LightRAGSync] Tool inject failed for '{full_name}': {e}")

            if tools_synced:
                logger.info(f"[LightRAGSync] Synced {tools_synced} tools into LightRAG")
        except Exception as e:
            logger.debug(f"[LightRAGSync] Tools sync skipped: {e}")

        return skills_synced, tools_synced, new_skill_ids, new_tool_ids

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
        # Initial delay: 5 minutes (wait for other services)
        self._stop_event.wait(300)
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