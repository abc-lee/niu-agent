"""
Tests for inject_document changelog behavior (document_created events).

When doc_id is not passed, the changelog entry must fall back to track_id
so the graph frontend never sees an empty id (which renders as a fake node).
"""

from unittest.mock import AsyncMock, MagicMock, patch

from niu_api.internal.lightrag_adapter import LightRAGIngester


def _record_change_data(mock_change_log):
    """Extract the data dict passed to record_change (positional or keyword)."""
    call = mock_change_log.record_change.call_args
    args, kwargs = call.args, call.kwargs
    if "data" in kwargs:
        return kwargs["data"]
    return args[1]


class TestInjectDocumentChangelog:
    """document_created changelog entries use track_id when doc_id is absent."""

    def test_no_doc_id_uses_track_id(self):
        """doc_id=None → record_change id == track_id."""
        ingester = LightRAGIngester()
        mock_rag = MagicMock()
        mock_rag.ainsert = AsyncMock(return_value="track-123")
        mock_change_log = MagicMock()

        with patch.object(ingester, "_get_rag", return_value=mock_rag), \
                patch("niu_api.internal.lightrag_manager.get_change_log",
                      return_value=mock_change_log):
            result = ingester.inject_document(content="Refined document text")

        assert result["status"] == "ok"
        assert result["track_id"] == "track-123"
        mock_change_log.record_change.assert_called_once()
        args, _ = mock_change_log.record_change.call_args
        assert args[0] == "document_created"
        data = _record_change_data(mock_change_log)
        assert data["id"] == "track-123"
        assert data["title"] == "track-123"

    def test_explicit_doc_id_uses_doc_id(self):
        """doc_id passed → record_change id == doc_id (not track_id)."""
        ingester = LightRAGIngester()
        mock_rag = MagicMock()
        mock_rag.ainsert = AsyncMock(return_value="track-456")
        mock_change_log = MagicMock()

        with patch.object(ingester, "_get_rag", return_value=mock_rag), \
                patch("niu_api.internal.lightrag_manager.get_change_log",
                      return_value=mock_change_log):
            result = ingester.inject_document(
                content="Some document text", doc_id="unique-doc-1"
            )

        assert result["status"] == "ok"
        mock_change_log.record_change.assert_called_once()
        data = _record_change_data(mock_change_log)
        assert data["id"] == "unique-doc-1"
        assert data["title"] == "unique-doc-1"

    def test_no_rag_skips_changelog(self):
        """_get_rag returns None → get_change_log is never called."""
        ingester = LightRAGIngester()
        mock_change_log = MagicMock()

        with patch.object(ingester, "_get_rag", return_value=None), \
                patch("niu_api.internal.lightrag_manager.get_change_log",
                      return_value=mock_change_log) as mock_get_cl:
            result = ingester.inject_document(content="Orphan document")

        assert result["status"] == "error"
        mock_get_cl.assert_not_called()
        mock_change_log.record_change.assert_not_called()
