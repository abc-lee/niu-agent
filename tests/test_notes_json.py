"""
Tests for Notes JSON Storage — niu_api/notes.py

Validates the JSON-based sticky notes storage layer.
"""

import json
import os
from unittest.mock import patch

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

    def test_delete_note_calls_lightrag_delete_entity(self, tmp_workspace):
        create_note(note_id="n1", content="KG note")

        with patch("niu_api.internal.lightrag_adapter.LightRAGAdapter") as mock_adapter_cls:
            mock_adapter = mock_adapter_cls.return_value
            result = delete_note(note_id="n1")
            mock_adapter.delete_entity.assert_called_once_with("note:n1")

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
