"""
Tests for niu_api/internal/lightrag_pipeline.py

LightRAGPipeline: background ingestion queue with backpressure, retry, and status tracking.
IngestTask: dataclass for tracking individual ingestion tasks.
IngestRetryPolicy: exponential backoff retry configuration.

TDD RED phase — these tests define the expected interface.
"""

from unittest.mock import AsyncMock, MagicMock, patch

# ============== IngestTask Tests ==============


class TestIngestTask:
    """Test IngestTask data structure."""

    def test_create_task_with_defaults(self):
        from niu_api.internal.lightrag_pipeline import IngestTask

        task = IngestTask(
            content="Test content",
            source_id="photo:123",
            source_type="photo",
        )
        assert task.content == "Test content"
        assert task.source_id == "photo:123"
        assert task.source_type == "photo"
        assert task.status == "queued"
        assert task.error is None
        assert task.attempt == 0

    def test_create_task_with_custom_status(self):
        from niu_api.internal.lightrag_pipeline import IngestTask

        task = IngestTask(
            content="Test",
            source_id="note:1",
            source_type="note",
            status="processing",
        )
        assert task.status == "processing"

    def test_task_source_types(self):
        """J6: Verify all source types are valid."""
        from niu_api.internal.lightrag_pipeline import IngestTask

        for source_type in ["photo", "note", "document", "file"]:
            task = IngestTask(
                content="Test",
                source_id=f"{source_type}:1",
                source_type=source_type,
            )
            assert task.source_type == source_type


# ============== IngestRetryPolicy Tests ==============


class TestIngestRetryPolicy:
    """Test retry policy configuration."""

    def test_default_max_retries(self):
        from niu_api.internal.lightrag_pipeline import IngestRetryPolicy

        policy = IngestRetryPolicy()
        assert policy.max_retries == 3

    def test_default_backoff_base(self):
        from niu_api.internal.lightrag_pipeline import IngestRetryPolicy

        policy = IngestRetryPolicy()
        assert policy.backoff_base == 30.0

    def test_get_delay_increases(self):
        """J3: Exponential backoff should increase with attempts."""
        from niu_api.internal.lightrag_pipeline import IngestRetryPolicy

        policy = IngestRetryPolicy()
        delay0 = policy.get_delay(0)
        delay1 = policy.get_delay(1)
        delay2 = policy.get_delay(2)
        # Exponential: base * 2^attempt + jitter
        assert delay0 < delay1 < delay2

    def test_get_delay_within_bounds(self):
        """J3: Delay should be reasonable (not too large)."""
        from niu_api.internal.lightrag_pipeline import IngestRetryPolicy

        policy = IngestRetryPolicy()
        delay = policy.get_delay(2)
        # base * 2^2 + jitter(10) = 120 + 10 = 130 max
        assert delay < 150


# ============== LightRAGPipeline Tests ==============


class TestPipelineSubmit:
    """J1, J2: Submit tasks and track status."""

    def test_submit_returns_task(self):
        from niu_api.internal.lightrag_pipeline import IngestTask, LightRAGPipeline

        pipeline = LightRAGPipeline()
        task = IngestTask(
            content="Test document",
            source_id="photo:1",
            source_type="photo",
        )
        with patch.object(pipeline, "_get_rag", return_value=MagicMock()):
            result = pipeline.submit(task)
            assert result.source_id == "photo:1"
            assert result.status == "queued"

    def test_get_status_returns_queued(self):
        """J2: After submit, status should be 'queued'."""
        from niu_api.internal.lightrag_pipeline import IngestTask, LightRAGPipeline

        pipeline = LightRAGPipeline()
        task = IngestTask(
            content="Test",
            source_id="note:1",
            source_type="note",
        )
        with patch.object(pipeline, "_get_rag", return_value=MagicMock()):
            pipeline.submit(task)
            status = pipeline.get_status("note:1")
            assert status == "queued"

    def test_get_status_returns_none_for_unknown(self):
        from niu_api.internal.lightrag_pipeline import LightRAGPipeline

        pipeline = LightRAGPipeline()
        assert pipeline.get_status("nonexistent") is None

    def test_submit_multiple_tasks(self):
        from niu_api.internal.lightrag_pipeline import IngestTask, LightRAGPipeline

        pipeline = LightRAGPipeline()
        with patch.object(pipeline, "_get_rag", return_value=MagicMock()):
            for i in range(5):
                task = IngestTask(
                    content=f"Content {i}",
                    source_id=f"doc:{i}",
                    source_type="document",
                )
                pipeline.submit(task)
            # All should be tracked
            for i in range(5):
                assert pipeline.get_status(f"doc:{i}") == "queued"


class TestPipelineProcess:
    """J1, J2: Process tasks and update status."""

    def test_process_updates_status_to_completed(self):
        """J2: After processing, status should be 'completed'."""
        from niu_api.internal.lightrag_pipeline import IngestTask, LightRAGPipeline

        pipeline = LightRAGPipeline()
        mock_rag = MagicMock()
        mock_rag.ainsert = AsyncMock(return_value="track-1")

        task = IngestTask(
            content="Test content",
            source_id="photo:1",
            source_type="photo",
        )

        with patch.object(pipeline, "_get_rag", return_value=mock_rag):
            pipeline.submit(task)
            # Process the task synchronously (bypass queue for testing)
            pipeline._process_task_sync(task)
            assert pipeline.get_status("photo:1") == "completed"

    def test_process_updates_status_to_failed_on_error(self):
        """J2: On exception, status should be 'failed'."""
        from niu_api.internal.lightrag_pipeline import IngestTask, LightRAGPipeline

        pipeline = LightRAGPipeline()
        mock_rag = MagicMock()
        mock_rag.ainsert = AsyncMock(side_effect=RuntimeError("LLM error"))

        task = IngestTask(
            content="Test content",
            source_id="photo:1",
            source_type="photo",
        )

        with patch.object(pipeline, "_get_rag", return_value=mock_rag):
            pipeline.submit(task)
            pipeline._process_task_sync(task)
            assert pipeline.get_status("photo:1") == "failed"

    def test_failed_task_records_error(self):
        from niu_api.internal.lightrag_pipeline import IngestTask, LightRAGPipeline

        pipeline = LightRAGPipeline()
        mock_rag = MagicMock()
        mock_rag.ainsert = AsyncMock(side_effect=RuntimeError("LLM timeout"))

        task = IngestTask(
            content="Test",
            source_id="note:1",
            source_type="note",
        )

        with patch.object(pipeline, "_get_rag", return_value=mock_rag):
            pipeline.submit(task)
            pipeline._process_task_sync(task)
            assert task.error is not None
            assert "LLM timeout" in task.error


class TestPipelineRetry:
    """J3: Retry failed ingestions."""

    def test_retry_failed_tasks(self):
        from niu_api.internal.lightrag_pipeline import IngestTask, LightRAGPipeline

        pipeline = LightRAGPipeline()
        mock_rag = MagicMock()
        # Fail first, succeed on retry
        mock_rag.ainsert = AsyncMock(
            side_effect=[RuntimeError("transient"), "track-ok"]
        )

        task = IngestTask(
            content="Test",
            source_id="doc:1",
            source_type="document",
        )

        with patch.object(pipeline, "_get_rag", return_value=mock_rag):
            pipeline.submit(task)
            pipeline._process_task_sync(task)
            assert task.status == "failed"
            assert task.attempt == 1

            # Retry
            pipeline.retry_failed()
            assert task.status == "queued"
            assert task.attempt == 1  # attempt counter preserved

            pipeline._process_task_sync(task)
            assert task.status == "completed"

    def test_max_retries_exhausted(self):
        """J3: After max retries, task stays failed."""
        from niu_api.internal.lightrag_pipeline import IngestTask, LightRAGPipeline

        pipeline = LightRAGPipeline()
        mock_rag = MagicMock()
        mock_rag.ainsert = AsyncMock(side_effect=RuntimeError("persistent error"))

        task = IngestTask(
            content="Test",
            source_id="doc:1",
            source_type="document",
        )

        with patch.object(pipeline, "_get_rag", return_value=mock_rag):
            pipeline.submit(task)
            # Process 3 times (simulating retry loop)
            for _ in range(3):
                pipeline._process_task_sync(task)
                if task.status == "failed" and task.attempt < 3:
                    task.status = "queued"  # Simulate retry
                    # Don't increment attempt - _process_task_sync does it

            # After 3 attempts, task should be failed
            assert task.attempt == 3
            assert task.status == "failed"

    def test_retry_resets_error(self):
        from niu_api.internal.lightrag_pipeline import IngestTask, LightRAGPipeline

        pipeline = LightRAGPipeline()
        mock_rag = MagicMock()
        mock_rag.ainsert = AsyncMock(side_effect=RuntimeError("error"))

        task = IngestTask(
            content="Test",
            source_id="doc:1",
            source_type="document",
        )

        with patch.object(pipeline, "_get_rag", return_value=mock_rag):
            pipeline.submit(task)
            pipeline._process_task_sync(task)
            assert task.error is not None

            pipeline.retry_failed()
            assert task.error is None
            assert task.status == "queued"


class TestPipelineBackpressure:
    """J5: Concurrency control."""

    def test_default_max_concurrent(self):
        from niu_api.internal.lightrag_pipeline import LightRAGPipeline

        pipeline = LightRAGPipeline()
        assert pipeline.max_concurrent == 3

    def test_custom_max_concurrent(self):
        from niu_api.internal.lightrag_pipeline import LightRAGPipeline

        pipeline = LightRAGPipeline(max_concurrent=5)
        assert pipeline.max_concurrent == 5


class TestPipelineDocumentUpdate:
    """J4: Update document (delete old + reinsert)."""

    def test_update_document_deletes_then_inserts(self):
        from niu_api.internal.lightrag_pipeline import LightRAGPipeline

        pipeline = LightRAGPipeline()
        mock_rag = MagicMock()
        mock_rag.adelete_by_doc_id = AsyncMock(return_value=MagicMock())
        mock_rag.ainsert = AsyncMock(return_value="track-new")

        with patch.object(pipeline, "_get_rag", return_value=mock_rag):
            result = pipeline.update_document(
                doc_id="doc-old-1",
                new_content="Updated content",
                source_id="note:1",
                source_type="note",
            )
            assert result["status"] == "ok"
            # Verify delete was called first
            mock_rag.adelete_by_doc_id.assert_called_once()
            # Verify insert was called
            mock_rag.ainsert.assert_called_once()

    def test_update_document_handles_delete_failure(self):
        from niu_api.internal.lightrag_pipeline import LightRAGPipeline

        pipeline = LightRAGPipeline()
        mock_rag = MagicMock()
        mock_rag.adelete_by_doc_id = AsyncMock(side_effect=RuntimeError("delete failed"))
        mock_rag.ainsert = AsyncMock(return_value="track-new")

        with patch.object(pipeline, "_get_rag", return_value=mock_rag):
            result = pipeline.update_document(
                doc_id="doc-old-1",
                new_content="Updated content",
                source_id="note:1",
                source_type="note",
            )
            # Should still try to insert even if delete fails
            assert result["status"] == "ok"

    def test_update_document_returns_error_when_no_lightrag(self):
        from niu_api.internal.lightrag_pipeline import LightRAGPipeline

        pipeline = LightRAGPipeline()
        with patch.object(pipeline, "_get_rag", return_value=None):
            result = pipeline.update_document(
                doc_id="doc-1",
                new_content="content",
                source_id="note:1",
                source_type="note",
            )
            assert result["status"] == "error"


class TestPipelineSourceSpecific:
    """J6: Source-specific ingestion with preprocessing."""

    def test_ingest_photo_adds_prefix(self):
        """J6: Photo content gets [Photo: id] prefix."""
        from niu_api.internal.lightrag_pipeline import IngestTask, LightRAGPipeline

        pipeline = LightRAGPipeline()
        mock_rag = MagicMock()
        mock_rag.ainsert = AsyncMock(return_value="track-1")

        task = IngestTask(
            content="A sunset over the ocean",
            source_id="photo:123",
            source_type="photo",
        )

        with patch.object(pipeline, "_get_rag", return_value=mock_rag):
            pipeline.submit(task)
            pipeline._process_task_sync(task)
            # Verify content was prefixed
            call_args = mock_rag.ainsert.call_args
            inserted_content = call_args[0][0]
            assert "[Photo:" in inserted_content

    def test_ingest_note_adds_prefix(self):
        """J6: Note content gets [Note: title] prefix."""
        from niu_api.internal.lightrag_pipeline import IngestTask, LightRAGPipeline

        pipeline = LightRAGPipeline()
        mock_rag = MagicMock()
        mock_rag.ainsert = AsyncMock(return_value="track-1")

        task = IngestTask(
            content="Remember to buy groceries",
            source_id="note:shopping",
            source_type="note",
        )

        with patch.object(pipeline, "_get_rag", return_value=mock_rag):
            pipeline.submit(task)
            pipeline._process_task_sync(task)
            call_args = mock_rag.ainsert.call_args
            inserted_content = call_args[0][0]
            assert "[Note:" in inserted_content

    def test_ingest_document_adds_prefix(self):
        """J6: Document content gets [Document: path] prefix."""
        from niu_api.internal.lightrag_pipeline import IngestTask, LightRAGPipeline

        pipeline = LightRAGPipeline()
        mock_rag = MagicMock()
        mock_rag.ainsert = AsyncMock(return_value="track-1")

        task = IngestTask(
            content="Chapter 1: Introduction to AI",
            source_id="document:/docs/ai-intro.pdf",
            source_type="document",
        )

        with patch.object(pipeline, "_get_rag", return_value=mock_rag):
            pipeline.submit(task)
            pipeline._process_task_sync(task)
            call_args = mock_rag.ainsert.call_args
            inserted_content = call_args[0][0]
            assert "[Document:" in inserted_content

    def test_ingest_file_no_prefix(self):
        """J6: File content passes through without prefix (raw)."""
        from niu_api.internal.lightrag_pipeline import IngestTask, LightRAGPipeline

        pipeline = LightRAGPipeline()
        mock_rag = MagicMock()
        mock_rag.ainsert = AsyncMock(return_value="track-1")

        task = IngestTask(
            content="Raw file content",
            source_id="file:/tmp/data.txt",
            source_type="file",
        )

        with patch.object(pipeline, "_get_rag", return_value=mock_rag):
            pipeline.submit(task)
            pipeline._process_task_sync(task)
            call_args = mock_rag.ainsert.call_args
            inserted_content = call_args[0][0]
            # File type should pass through as-is
            assert inserted_content == "Raw file content"


class TestPipelineStatus:
    """J2: Comprehensive status tracking."""

    def test_get_all_statuses(self):
        from niu_api.internal.lightrag_pipeline import IngestTask, LightRAGPipeline

        pipeline = LightRAGPipeline()
        with patch.object(pipeline, "_get_rag", return_value=MagicMock()):
            for i in range(3):
                task = IngestTask(
                    content=f"Content {i}",
                    source_id=f"doc:{i}",
                    source_type="document",
                )
                pipeline.submit(task)

            statuses = pipeline.get_all_statuses()
            assert len(statuses) == 3
            assert all(s["status"] == "queued" for s in statuses)

    def test_get_failed_tasks(self):
        from niu_api.internal.lightrag_pipeline import IngestTask, LightRAGPipeline

        pipeline = LightRAGPipeline()
        mock_rag = MagicMock()
        mock_rag.ainsert = AsyncMock(side_effect=RuntimeError("error"))

        with patch.object(pipeline, "_get_rag", return_value=mock_rag):
            task1 = IngestTask(content="ok", source_id="doc:1", source_type="document")
            task2 = IngestTask(content="fail", source_id="doc:2", source_type="document")
            pipeline.submit(task1)
            pipeline.submit(task2)

            # Fail task2
            pipeline._process_task_sync(task2)

            failed = pipeline.get_failed_tasks()
            assert len(failed) == 1
            assert failed[0].source_id == "doc:2"
