"""
Tests for Notes JSON Storage — niu_api/notes.py

Validates the JSON-based sticky notes storage layer.
"""

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from niu_api.notes import (
    create_note,
    delete_note,
    get_note,
    list_notes,
    read_notes,
    update_note,
)


@pytest.fixture
def tmp_workspace(tmp_path):
    """Provide a temporary workspace directory and set WORKSPACE_PATH."""
    with patch.dict(os.environ, {"WORKSPACE_PATH": str(tmp_path)}):
        # Clear module-level path cache by reimporting if needed
        yield tmp_path


class TestNotesJsonStorage:
    """Tests for JSON-based notes storage functions."""

    def test_read_notes_returns_empty_when_file_missing(self, tmp_workspace):
        result = read_notes()
        assert result == []

    def test_read_notes_returns_existing_notes(self, tmp_workspace):
        notes_dir = tmp_workspace / "notes"
        notes_dir.mkdir()
        notes_file = notes_dir / "notes.json"
        notes_data = [
            {
                "id": "n1",
                "content": "Hello",
                "tags": ["greeting"],
                "created_at": "2026-01-01T00:00:00",
                "updated_at": None,
            }
        ]
        notes_file.write_text(json.dumps(notes_data, ensure_ascii=False, indent=2), encoding="utf-8")

        result = read_notes()
        assert len(result) == 1
        assert result[0]["id"] == "n1"
        assert result[0]["content"] == "Hello"
        assert result[0]["tags"] == ["greeting"]

    def test_create_note_appends_to_file(self, tmp_workspace):
        result = create_note(note_id="n1", content="First note", tags=["test"])
        assert result == {"id": "n1", "status": "created"}

        # Verify written to file
        notes_file = tmp_workspace / "notes" / "notes.json"
        assert notes_file.exists()
        data = json.loads(notes_file.read_text(encoding="utf-8"))
        assert len(data) == 1
        assert data[0]["id"] == "n1"
        assert data[0]["content"] == "First note"
        assert data[0]["tags"] == ["test"]
        assert data[0]["created_at"] is not None
        assert data[0]["updated_at"] is None

    def test_create_note_creates_directory_if_missing(self, tmp_workspace):
        notes_dir = tmp_workspace / "notes"
        assert not notes_dir.exists()

        create_note(note_id="n1", content="Auto-dir")

        assert notes_dir.exists()
        assert (notes_dir / "notes.json").exists()

    def test_create_note_with_custom_created_at(self, tmp_workspace):
        result = create_note(note_id="n2", content="Timed", created_at="2026-04-26T12:00:00")
        assert result["status"] == "created"

        notes = read_notes()
        assert notes[0]["created_at"] == "2026-04-26T12:00:00"

    def test_update_note_modifies_content(self, tmp_workspace):
        create_note(note_id="n1", content="Original", tags=["old"])

        result = update_note(note_id="n1", content="Updated", tags=["new"])
        assert result == {"id": "n1", "status": "updated"}

        notes = read_notes()
        assert notes[0]["content"] == "Updated"
        assert notes[0]["tags"] == ["new"]
        assert notes[0]["updated_at"] is not None

    def test_update_note_partial_update_content_only(self, tmp_workspace):
        create_note(note_id="n1", content="Original", tags=["keep"])

        update_note(note_id="n1", content="New content")

        notes = read_notes()
        assert notes[0]["content"] == "New content"
        assert notes[0]["tags"] == ["keep"]

    def test_update_note_not_found(self, tmp_workspace):
        result = update_note(note_id="nonexistent", content="Nope")
        assert result == {"id": "nonexistent", "status": "not_found"}

    def test_delete_note_removes_from_file(self, tmp_workspace):
        create_note(note_id="n1", content="Delete me")
        create_note(note_id="n2", content="Keep me")

        with patch("niu_api.internal.lightrag_adapter.LightRAGAdapter") as mock_adapter_cls:
            result = delete_note(note_id="n1")

        assert result == {"id": "n1", "status": "deleted"}

        notes = read_notes()
        assert len(notes) == 1
        assert notes[0]["id"] == "n2"

    def test_delete_note_calls_lightrag_delete_document(self, tmp_workspace):
        create_note(note_id="n1", content="KG note")

        with patch("niu_api.internal.lightrag_adapter.LightRAGAdapter") as mock_adapter_cls:
            mock_adapter = mock_adapter_cls.return_value
            result = delete_note(note_id="n1")
            mock_adapter.delete_document.assert_called_once_with("note:n1")

        assert result["status"] == "deleted"

    def test_delete_note_not_found(self, tmp_workspace):
        result = delete_note(note_id="nonexistent")
        assert result == {"id": "nonexistent", "status": "not_found"}

    def test_list_notes_returns_all_ordered(self, tmp_workspace):
        create_note(note_id="n1", content="First", created_at="2026-01-01T00:00:00")
        create_note(note_id="n2", content="Second", created_at="2026-06-15T00:00:00")
        create_note(note_id="n3", content="Third", created_at="2026-03-10T00:00:00")

        result = list_notes()
        assert len(result) == 3
        # Sorted by created_at DESC
        assert result[0]["id"] == "n2"
        assert result[1]["id"] == "n3"
        assert result[2]["id"] == "n1"

    def test_get_note_returns_single(self, tmp_workspace):
        create_note(note_id="n1", content="Find me")
        create_note(note_id="n2", content="Other")

        result = get_note("n1")
        assert result is not None
        assert result["id"] == "n1"
        assert result["content"] == "Find me"

    def test_get_note_returns_none_for_missing(self, tmp_workspace):
        result = get_note("nonexistent")
        assert result is None

    def test_read_notes_returns_empty_on_corrupt_file(self, tmp_workspace):
        notes_dir = tmp_workspace / "notes"
        notes_dir.mkdir()
        notes_file = notes_dir / "notes.json"
        notes_file.write_text("NOT VALID JSON{{{", encoding="utf-8")

        result = read_notes()
        assert result == []

    def test_read_notes_returns_empty_on_non_list_file(self, tmp_workspace):
        notes_dir = tmp_workspace / "notes"
        notes_dir.mkdir()
        notes_file = notes_dir / "notes.json"
        notes_file.write_text('{"not": "a list"}', encoding="utf-8")

        result = read_notes()
        assert result == []


class TestSkillSyncNotes:
    """Tests for SkillSync._scan_notes() change detection."""

    def test_scan_notes_detects_new_note(self, tmp_workspace):
        """New note should be injected to LightRAG."""
        from agent.injector.sync import SkillSync

        notes_dir = tmp_workspace / "notes"
        notes_dir.mkdir()
        notes_file = notes_dir / "notes.json"
        notes_file.write_text(
            json.dumps([{"id": "n1", "content": "Hello", "tags": []}]),
            encoding="utf-8",
        )

        sync = SkillSync(skills_dir=str(tmp_workspace / "skills"), use_watchdog=False)
        with patch.dict(os.environ, {"WORKSPACE_PATH": str(tmp_workspace)}):
            with patch.object(sync, "_inject_note_to_lightrag", return_value=set()) as mock_inject:
                added, updated = sync._scan_notes()
                assert added == 1
                assert updated == 0
                mock_inject.assert_called_once()
                # Called with list of changed notes
                notes_arg = mock_inject.call_args[0][0]
                assert isinstance(notes_arg, list)
                assert len(notes_arg) == 1
                assert notes_arg[0]["id"] == "n1"
                assert notes_arg[0]["content"] == "Hello"

    def test_scan_notes_detects_changed_note(self, tmp_workspace):
        """Changed content should trigger re-injection."""
        from agent.injector.sync import SkillSync

        notes_dir = tmp_workspace / "notes"
        notes_dir.mkdir()
        notes_file = notes_dir / "notes.json"

        sync = SkillSync(skills_dir=str(tmp_workspace / "skills"), use_watchdog=False)
        with patch.dict(os.environ, {"WORKSPACE_PATH": str(tmp_workspace)}):
            # First scan — note is new
            notes_file.write_text(
                json.dumps([{"id": "n1", "content": "Original", "tags": []}]),
                encoding="utf-8",
            )
            with patch.object(sync, "_inject_note_to_lightrag", return_value=set()):
                sync._scan_notes()

            # Second scan — content changed
            notes_file.write_text(
                json.dumps([{"id": "n1", "content": "Changed", "tags": []}]),
                encoding="utf-8",
            )
            with patch.object(sync, "_inject_note_to_lightrag", return_value=set()) as mock_inject:
                added, updated = sync._scan_notes()
                assert added == 0
                assert updated == 1
                mock_inject.assert_called_once()
                notes_arg = mock_inject.call_args[0][0]
                assert isinstance(notes_arg, list)
                assert len(notes_arg) == 1
                assert notes_arg[0]["id"] == "n1"
                assert notes_arg[0]["content"] == "Changed"

    def test_scan_notes_skips_unchanged(self, tmp_workspace):
        """Unchanged content should not trigger injection."""
        from agent.injector.sync import SkillSync

        notes_dir = tmp_workspace / "notes"
        notes_dir.mkdir()
        notes_file = notes_dir / "notes.json"
        notes_file.write_text(
            json.dumps([{"id": "n1", "content": "Same", "tags": []}]),
            encoding="utf-8",
        )

        sync = SkillSync(skills_dir=str(tmp_workspace / "skills"), use_watchdog=False)
        with patch.dict(os.environ, {"WORKSPACE_PATH": str(tmp_workspace)}):
            with patch.object(sync, "_inject_note_to_lightrag", return_value=set()):
                sync._scan_notes()

            # Second scan — same content
            with patch.object(sync, "_inject_note_to_lightrag", return_value=set()) as mock_inject:
                added, updated = sync._scan_notes()
                assert added == 0
                assert updated == 0
                mock_inject.assert_not_called()

    def test_scan_notes_detects_deletion(self, tmp_workspace):
        """Deleted note should be removed from LightRAG."""
        from agent.injector.sync import SkillSync

        notes_dir = tmp_workspace / "notes"
        notes_dir.mkdir()
        notes_file = notes_dir / "notes.json"

        sync = SkillSync(skills_dir=str(tmp_workspace / "skills"), use_watchdog=False)
        with patch.dict(os.environ, {"WORKSPACE_PATH": str(tmp_workspace)}):
            # First scan — two notes
            notes_file.write_text(
                json.dumps([
                    {"id": "n1", "content": "Keep", "tags": []},
                    {"id": "n2", "content": "Delete", "tags": []},
                ]),
                encoding="utf-8",
            )
            with patch.object(sync, "_inject_note_to_lightrag", return_value=set()):
                sync._scan_notes()

            # Second scan — n2 removed
            notes_file.write_text(
                json.dumps([{"id": "n1", "content": "Keep", "tags": []}]),
                encoding="utf-8",
            )
            with patch.object(sync, "_inject_note_to_lightrag", return_value=set()):
                with patch("niu_api.internal.lightrag_adapter.LightRAGAdapter") as mock_cls:
                    mock_adapter = MagicMock()
                    mock_cls.return_value = mock_adapter
                    added, updated = sync._scan_notes()
                    mock_adapter.delete_document.assert_called_once_with("note:n2")

    def test_scan_notes_handles_corrupt_json(self, tmp_workspace):
        """Corrupt JSON should not crash."""
        from agent.injector.sync import SkillSync

        notes_dir = tmp_workspace / "notes"
        notes_dir.mkdir()
        notes_file = notes_dir / "notes.json"
        notes_file.write_text("NOT JSON{{{", encoding="utf-8")

        sync = SkillSync(skills_dir=str(tmp_workspace / "skills"), use_watchdog=False)
        with patch.dict(os.environ, {"WORKSPACE_PATH": str(tmp_workspace)}):
            added, updated = sync._scan_notes()
            assert added == 0
            assert updated == 0

    def test_scan_notes_null_content_does_not_crash(self, tmp_workspace):
        """Note with null content should be handled gracefully."""
        from agent.injector.sync import SkillSync

        notes_dir = tmp_workspace / "notes"
        notes_dir.mkdir()
        notes_file = notes_dir / "notes.json"
        notes_file.write_text(
            json.dumps([{"id": "n1", "content": None, "tags": None}]),
            encoding="utf-8",
        )

        sync = SkillSync(skills_dir=str(tmp_workspace / "skills"), use_watchdog=False)
        with patch.dict(os.environ, {"WORKSPACE_PATH": str(tmp_workspace)}):
            with patch.object(sync, "_inject_note_to_lightrag", return_value=set()) as mock_inject:
                added, updated = sync._scan_notes()
                assert added == 1
                mock_inject.assert_called_once()
                notes_arg = mock_inject.call_args[0][0]
                assert isinstance(notes_arg, list)
                assert len(notes_arg) == 1
                assert notes_arg[0]["id"] == "n1"

    def test_scan_notes_failed_injection_not_recorded(self, tmp_workspace):
        """Failed injection should not record hash — note will be retried next scan."""
        from agent.injector.sync import SkillSync

        notes_dir = tmp_workspace / "notes"
        notes_dir.mkdir()
        notes_file = notes_dir / "notes.json"
        notes_file.write_text(
            json.dumps([{"id": "n1", "content": "Hello", "tags": []}]),
            encoding="utf-8",
        )

        sync = SkillSync(skills_dir=str(tmp_workspace / "skills"), use_watchdog=False)
        with patch.dict(os.environ, {"WORKSPACE_PATH": str(tmp_workspace)}):
            # First scan — injection fails
            with patch.object(sync, "_inject_note_to_lightrag", return_value={"n1"}):
                added, updated = sync._scan_notes()
                assert added == 0  # not counted because injection failed
                assert "n1" not in sync._last_notes_scan  # hash not recorded

            # Second scan — injection succeeds, note is retried
            with patch.object(sync, "_inject_note_to_lightrag", return_value=set()):
                added, updated = sync._scan_notes()
                assert added == 1  # now counted as new
                assert "n1" in sync._last_notes_scan

    def test_scan_notes_failed_deletion_keeps_hash(self, tmp_workspace):
        """Failed LightRAG deletion should keep hash so it's retried next scan."""
        from agent.injector.sync import SkillSync

        notes_dir = tmp_workspace / "notes"
        notes_dir.mkdir()
        notes_file = notes_dir / "notes.json"

        sync = SkillSync(skills_dir=str(tmp_workspace / "skills"), use_watchdog=False)
        with patch.dict(os.environ, {"WORKSPACE_PATH": str(tmp_workspace)}):
            # First scan — two notes
            notes_file.write_text(
                json.dumps([
                    {"id": "n1", "content": "Keep", "tags": []},
                    {"id": "n2", "content": "Delete", "tags": []},
                ]),
                encoding="utf-8",
            )
            with patch.object(sync, "_inject_note_to_lightrag", return_value=set()):
                sync._scan_notes()
            assert "n2" in sync._last_notes_scan

            # Second scan — n2 removed, but LightRAG delete fails
            notes_file.write_text(
                json.dumps([{"id": "n1", "content": "Keep", "tags": []}]),
                encoding="utf-8",
            )
            with patch("niu_api.internal.lightrag_adapter.LightRAGAdapter") as mock_cls:
                mock_cls.return_value.delete_document.side_effect = RuntimeError("LightRAG down")
                added, updated = sync._scan_notes()

            # Hash should still be present for retry
            assert "n2" in sync._last_notes_scan


class TestNotesDuplicateId:
    """Tests for duplicate note ID handling."""

    def test_create_duplicate_note_returns_duplicate_status(self, tmp_workspace):
        """Creating a note with same ID should return 'duplicate' status."""
        create_note(note_id="n1", content="First")
        result = create_note(note_id="n1", content="Second")
        assert result == {"id": "n1", "status": "duplicate"}

        # Only one note in file
        notes = read_notes()
        assert len(notes) == 1
        assert notes[0]["content"] == "First"


class TestNotesDeleteFailure:
    """Tests for delete_note behavior when LightRAG fails."""

    def test_delete_note_succeeds_even_if_lightrag_fails(self, tmp_workspace):
        """Note should be deleted from JSON even if LightRAG delete_document raises."""
        create_note(note_id="n1", content="Delete me")

        with patch("niu_api.internal.lightrag_adapter.LightRAGAdapter") as mock_cls:
            mock_cls.return_value.delete_document.side_effect = RuntimeError("LightRAG down")
            result = delete_note(note_id="n1")

        # Note is still deleted from JSON despite LightRAG failure
        assert result["status"] == "deleted"
        notes = read_notes()
        assert len(notes) == 0


class TestNotesIdValidation:
    """Tests for note ID format validation."""

    def test_valid_note_ids(self, tmp_workspace):
        for nid in ["abc", "note-1", "my_note", "A1_b2-C3", "x" * 128]:
            result = create_note(note_id=nid, content="test")
            assert result["status"] == "created", f"Expected 'created' for id={nid!r}, got {result['status']}"

    def test_invalid_note_id_special_chars(self, tmp_workspace):
        result = create_note(note_id="bad id", content="test")
        assert result["status"] == "invalid_id"

    def test_invalid_note_id_path_traversal(self, tmp_workspace):
        result = create_note(note_id="../etc/passwd", content="test")
        assert result["status"] == "invalid_id"

    def test_invalid_note_id_empty(self, tmp_workspace):
        result = create_note(note_id="", content="test")
        assert result["status"] == "invalid_id"

    def test_invalid_note_id_too_long(self, tmp_workspace):
        result = create_note(note_id="x" * 129, content="test")
        assert result["status"] == "invalid_id"

    def test_invalid_delete_note_id(self, tmp_workspace):
        result = delete_note(note_id="bad id")
        assert result["status"] == "invalid_id"

    def test_invalid_update_note_id(self, tmp_workspace):
        result = update_note(note_id="../etc/passwd", content="test")
        assert result["status"] == "invalid_id"
