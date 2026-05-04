"""
LightRAG Document Pipeline

Background ingestion queue with backpressure control, retry policy,
and document-level status tracking.

Architecture:
- IngestTask: dataclass tracking individual ingestion tasks
- IngestRetryPolicy: exponential backoff configuration
- LightRAGPipeline: background queue + status tracking + retry

The pipeline wraps LightRAG's ainsert() with:
1. Source-specific content preprocessing (photo/note/document prefixes)
2. Backpressure via max_concurrent semaphore
3. Retry with exponential backoff
4. Document update via delete-then-reinsert
"""

import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from loguru import logger

from niu_api.internal.lightrag_manager import call_async, get_lightrag


# ============== IngestTask ==============


@dataclass
class IngestTask:
    """Track an ingestion task through the pipeline.

    Attributes:
        content: Document text to ingest.
        source_id: Application-level ID (e.g., "photo:123", "note:shopping").
        source_type: Source category ("photo", "note", "document", "file").
        status: Current status (queued, processing, completed, failed).
        error: Error message if failed, None otherwise.
        attempt: Number of processing attempts (for retry tracking).
    """

    content: str
    source_id: str
    source_type: str
    status: str = "queued"
    error: Optional[str] = None
    attempt: int = 0


# ============== IngestRetryPolicy ==============


@dataclass
class IngestRetryPolicy:
    """Retry policy for LightRAG ingestion failures.

    Uses exponential backoff with jitter to avoid thundering herd.
    """

    max_retries: int = 3
    backoff_base: float = 30.0  # seconds
    jitter_max: float = 10.0  # seconds

    def get_delay(self, attempt: int) -> float:
        """Calculate delay for the given attempt number.

        Args:
            attempt: Zero-based attempt number.

        Returns:
            Delay in seconds (exponential backoff + random jitter).
        """
        return self.backoff_base * (2 ** attempt) + random.uniform(0, self.jitter_max)


# ============== Content Preprocessing ==============


def _preprocess_content(task: IngestTask) -> str:
    """Add source-type prefix for better entity extraction.

    LightRAG's entity extraction benefits from context about the
    source type. Prefixes help the LLM understand what kind of
    entities to extract.
    """
    if task.source_type == "photo":
        # Extract ID from source_id (e.g., "photo:123" -> "123")
        photo_id = task.source_id.split(":", 1)[1] if ":" in task.source_id else task.source_id
        return f"[Photo: {photo_id}]\n{task.content}"
    elif task.source_type == "document":
        doc_id = task.source_id.split(":", 1)[1] if ":" in task.source_id else task.source_id
        return f"[Document: {doc_id}]\n{task.content}"
    else:
        # "file" and other types: pass through as-is
        return task.content


# ============== LightRAGPipeline ==============


class LightRAGPipeline:
    """Background ingestion pipeline with backpressure and retry.

    Manages the lifecycle of document ingestion tasks:
    - Submit tasks for background processing
    - Track status per source_id
    - Retry failed tasks with exponential backoff
    - Update documents via delete-then-reinsert
    """

    MAX_TRACKED_TASKS = 1000  # Evict oldest completed tasks beyond this limit

    def __init__(self, max_concurrent: int = 3, retry_policy: Optional[IngestRetryPolicy] = None):
        self.max_concurrent = max_concurrent
        self._semaphore = threading.Semaphore(max_concurrent)
        self.retry_policy = retry_policy or IngestRetryPolicy()
        self._tracked_tasks: Dict[str, IngestTask] = {}

    def _evict_completed_tasks(self) -> None:
        """Evict oldest completed/failed tasks when _tracked_tasks exceeds MAX_TRACKED_TASKS."""
        if len(self._tracked_tasks) <= self.MAX_TRACKED_TASKS:
            return
        # Remove completed and failed tasks first (oldest by insertion order — dict preserves order in Python 3.7+)
        to_remove = [k for k, v in self._tracked_tasks.items() if v.status in ("completed", "failed")]
        excess = len(self._tracked_tasks) - self.MAX_TRACKED_TASKS
        for key in to_remove[:excess]:
            del self._tracked_tasks[key]

    def _get_rag(self):
        """Get the LightRAG instance (delegates to lightrag_manager)."""
        return get_lightrag()

    # ============== Task Submission ==============

    def submit(self, task: IngestTask) -> IngestTask:
        """Submit an ingestion task for processing.

        The task is tracked by source_id. In production, this would
        enqueue to a background worker. For testing, use _process_task_sync().

        Args:
            task: The ingestion task to submit.

        Returns:
            The submitted task (with status set to "queued").
        """
        task.status = "queued"
        self._tracked_tasks[task.source_id] = task
        self._evict_completed_tasks()
        logger.info(f"[PIPELINE] Task queued: {task.source_type}/{task.source_id}")
        return task

    # ============== Synchronous Processing ==============

    def _process_task_sync(self, task: IngestTask) -> None:
        """Process a single task synchronously (for testing and direct use).

        Applies content preprocessing, calls ainsert, and updates status.
        Uses semaphore to limit concurrent LightRAG operations.
        """
        with self._semaphore:
            rag = self._get_rag()
            if rag is None:
                task.status = "failed"
                task.error = "LightRAG not available"
                return

            task.status = "processing"
            task.attempt += 1

            try:
                content = _preprocess_content(task)
                call_async(rag.ainsert(content), timeout=600)
                task.status = "completed"
                task.error = None
                logger.info(f"[PIPELINE] Task completed: {task.source_id}")
            except Exception as e:
                task.status = "failed"
                task.error = str(e)
                logger.error(f"[PIPELINE] Task failed (attempt {task.attempt}): {task.source_id}: {e}")

    # ============== Status Tracking ==============

    def get_status(self, source_id: str) -> Optional[str]:
        """Get the status of a tracked task."""
        task = self._tracked_tasks.get(source_id)
        return task.status if task else None

    def get_all_statuses(self) -> List[Dict[str, Any]]:
        """Get status of all tracked tasks."""
        return [
            {
                "source_id": task.source_id,
                "source_type": task.source_type,
                "status": task.status,
                "attempt": task.attempt,
                "error": task.error,
            }
            for task in self._tracked_tasks.values()
        ]

    def get_failed_tasks(self) -> List[IngestTask]:
        """Get all tasks with status 'failed'."""
        return [t for t in self._tracked_tasks.values() if t.status == "failed"]

    # ============== Retry ==============

    def retry_failed(self) -> int:
        """Reset all failed tasks to 'queued' for retry.

        Returns:
            Number of tasks reset.
        """
        count = 0
        for task in self._tracked_tasks.values():
            if task.status == "failed":
                task.status = "queued"
                task.error = None
                count += 1
        if count > 0:
            logger.info(f"[PIPELINE] {count} failed tasks reset for retry")
        return count

    # ============== Document Update ==============

    def update_document(
        self,
        doc_id: str,
        new_content: str,
        source_id: str,
        source_type: str,
    ) -> Dict[str, Any]:
        """Update a document by deleting old version and reinserting.

        Uses LightRAG's adelete_by_doc_id to remove the old document,
        then ainsert to add the new version.

        If delete succeeds but insert fails, tracks a failed task to
        prevent silent data loss.

        Args:
            doc_id: LightRAG document ID to delete.
            new_content: New document content to insert.
            source_id: Application-level source ID.
            source_type: Source type for preprocessing.

        Returns:
            Dict with status and details.
        """
        rag = self._get_rag()
        if rag is None:
            return {"status": "error", "message": "LightRAG not available"}

        task = IngestTask(
            content=new_content,
            source_id=source_id,
            source_type=source_type,
        )

        # Step 1: Delete old document (best-effort, don't block on failure)
        delete_ok = False
        try:
            call_async(rag.adelete_by_doc_id(doc_id), timeout=600)
            logger.info(f"[PIPELINE] Deleted old document: {doc_id}")
            delete_ok = True
        except Exception as e:
            logger.warning(f"[PIPELINE] Delete failed for {doc_id}: {e} (continuing with insert)")

        # Step 2: Insert new content
        try:
            content = _preprocess_content(task)
            track_id = call_async(rag.ainsert(content), timeout=600)
            self._tracked_tasks[source_id] = task
            task.status = "completed"
            return {"status": "ok", "track_id": track_id}
        except Exception as e:
            logger.error(f"[PIPELINE] Re-insert failed for {source_id}: {e}")
            # Track as failed so data loss is visible (especially if delete succeeded)
            task.status = "failed"
            task.error = f"Re-insert failed after delete (delete_ok={delete_ok}): {e}"
            self._tracked_tasks[source_id] = task
            return {"status": "error", "message": task.error}
