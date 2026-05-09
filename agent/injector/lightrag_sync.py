"""
LightRAG Background Sync

Periodic backfill service that syncs documents and skills into the LightRAG
brain graph. Photos are NOT synced here — photo-server calls
pipeline_enqueue_file directly at import time, so no background sync is needed.

Architecture:
- Skills sync is handled by SkillSync (agent/injector/sync.py)
- Document sync from vectors.db (currently disabled — vector-store removed)
- Runs in a background daemon thread with configurable interval
"""

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from loguru import logger


class LightRAGSync:
    """LightRAG periodic backfill service.

    Syncs skills and documents into LightRAG.
    Photos are handled by photo-server at import time — no background sync needed.
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
            "documents_synced": 0,
            "skills_synced": 0,
            "tools_synced": 0,
            "errors": [],
        }

        # Load previously synced IDs for delta tracking
        prev_state = self._load_status()
        prev_doc_ids = set(prev_state.get("synced_doc_ids", []))
        prev_skill_ids = set(prev_state.get("synced_skill_ids", []))
        prev_tool_ids = set(prev_state.get("synced_tool_ids", []))

        # 1. Sync documents from vectors.db
        try:
            d, new_doc_ids = self._sync_vectors_db()
            stats["documents_synced"] = d
        except Exception as e:
            logger.warning(f"[LightRAGSync] vectors.db sync failed: {e}")
            stats["errors"].append(f"vectors: {e}")
            new_doc_ids = set()

        # 2. Sync skills and tools (delegated to SkillSync — returns zeros)
        try:
            skills_count, tools_count, new_skill_ids, new_tool_ids = self._sync_skills_and_tools()
            stats["skills_synced"] = skills_count
            stats["tools_synced"] = tools_count
        except Exception as e:
            logger.warning(f"[LightRAGSync] skills/tools sync failed: {e}")
            stats["errors"].append(f"skills_tools: {e}")
            new_skill_ids = set()
            new_tool_ids = set()

        # 3. Merge previous + newly synced IDs and save
        all_doc_ids = prev_doc_ids | new_doc_ids
        all_skill_ids = prev_skill_ids | new_skill_ids
        all_tool_ids = prev_tool_ids | new_tool_ids
        self._save_status(stats, all_doc_ids, all_skill_ids, all_tool_ids)

        logger.info(
            f"[LightRAGSync] Sync complete: "
            f"{stats['documents_synced']} documents, "
            f"{stats['skills_synced']} skills, {stats['tools_synced']} tools | "
            f"tracked IDs: "
            f"{len(all_doc_ids)} docs, {len(all_skill_ids)} skills, {len(all_tool_ids)} tools"
        )
        return stats

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

        Old status files may contain synced_photo_ids, synced_person_ids,
        synced_co_occ_ids fields — they are read but no longer used.

        Returns:
            Dict with keys: last_sync, stats, synced_doc_ids,
            synced_skill_ids, synced_tool_ids.
            Returns empty dict if the file does not exist or cannot be parsed.
        """
        try:
            if self._status_file.exists():
                data = json.loads(self._status_file.read_text(encoding="utf-8"))
                # Ensure list fields exist (backward compat with old format)
                data.setdefault("synced_doc_ids", [])
                data.setdefault("synced_skill_ids", [])
                data.setdefault("synced_tool_ids", [])
                return data
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"[LightRAGSync] Failed to load status: {e}")
        return {}

    def _save_status(
        self,
        stats: dict,
        synced_doc_ids: set,
        synced_skill_ids: set | None = None,
        synced_tool_ids: set | None = None,
    ):
        """Save sync status and tracked IDs to file.

        Args:
            stats: Sync statistics for this run.
            synced_doc_ids: All document IDs synced so far (previous + new).
            synced_skill_ids: All skill IDs synced so far (previous + new).
            synced_tool_ids: All tool IDs synced so far (previous + new).
        """
        try:
            self._status_file.parent.mkdir(parents=True, exist_ok=True)
            status = {
                "last_sync": datetime.now().isoformat(),
                "stats": stats,
                "synced_doc_ids": sorted(synced_doc_ids),
                "synced_skill_ids": sorted(synced_skill_ids or set()),
                "synced_tool_ids": sorted(synced_tool_ids or set()),
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
