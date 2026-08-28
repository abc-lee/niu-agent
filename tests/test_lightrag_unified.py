"""Tests for LightRAG unified entity structure.

Verifies that sync_photo_to_kg constructs Person entities correctly:
- No file_path field on Person entities
- No source_id field on Person entities
- description contains only the person name (not "detected in photo: ...")
"""
import os
import sys
from unittest.mock import MagicMock, patch

# Add photo-server source to path (跨平台相对路径，防单跑 collection error)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "mcp-servers", "photo-server", "src"))

from niu_photo_server import sync_photo_to_kg


# Shared mock setup: patch the local imports inside sync_photo_to_kg
def _make_mocks():
    """Create mock objects for LightRAG dependencies."""
    mock_rag = MagicMock()
    mock_ingester = MagicMock()
    return mock_rag, mock_ingester


def _patch_lightrag():
    """Return a decorator that patches all LightRAG imports used by sync_photo_to_kg."""
    mock_rag, mock_ingester = _make_mocks()

    def decorator(func):
        @patch("niu_api.internal.lightrag_manager.get_lightrag", return_value=mock_rag)
        @patch("niu_api.internal.lightrag_manager.call_async")
        @patch("niu_api.internal.lightrag_adapter.LightRAGIngester", return_value=mock_ingester)
        def wrapper(*args, **kwargs):
            # Reset ingester mock between tests
            mock_ingester.reset_mock()
            return func(*args, mock_rag=mock_rag, mock_ingester=mock_ingester, **kwargs)
        return wrapper
    return decorator, mock_rag, mock_ingester


class TestPersonEntityStructure:
    """Verify Person entity structure in sync_photo_to_kg.

    sync_photo_to_kg now uses ToolRegistry (lightrag-server/lightrag_insert,
    lightrag-server/lightrag_insert_custom_kg) instead of LightRAGIngester.
    """

    @patch("agent.tool_registry.get_registry")
    def test_person_entity_has_no_file_path(self, mock_get_registry):
        """Person entities must NOT have a file_path field."""
        mock_insert = MagicMock(return_value={"status": "ok"})
        mock_insert_custom_kg = MagicMock(return_value={"status": "ok"})
        mock_registry = MagicMock()

        def registry_get(tool_name):
            if tool_name == "lightrag-server/lightrag_insert":
                return mock_insert
            elif tool_name == "lightrag-server/lightrag_insert_custom_kg":
                return mock_insert_custom_kg
            return MagicMock()

        mock_registry.get.side_effect = registry_get
        mock_get_registry.return_value = mock_registry

        detected_persons = [
            {"id": "person_001", "name": "Alice", "similarity": 0.95},
        ]
        sync_photo_to_kg("/photos/vacation.jpg", "Beach photo", detected_persons)

        # The first inject_custom_kg call contains Person entities
        inject_call = mock_insert_custom_kg.call_args_list[0]
        entities = inject_call.kwargs.get("entities") or inject_call[1].get("entities") or inject_call[0][0]

        for entity in entities:
            if entity["entity_type"] == "Person":
                assert "file_path" not in entity, (
                    f"Person entity {entity['entity_name']} must not have file_path, "
                    f"but found: {entity['file_path']}"
                )

    @patch("agent.tool_registry.get_registry")
    def test_person_entity_has_no_source_id(self, mock_get_registry):
        """Person entities must NOT have a source_id field."""
        mock_insert = MagicMock(return_value={"status": "ok"})
        mock_insert_custom_kg = MagicMock(return_value={"status": "ok"})
        mock_registry = MagicMock()

        def registry_get(tool_name):
            if tool_name == "lightrag-server/lightrag_insert":
                return mock_insert
            elif tool_name == "lightrag-server/lightrag_insert_custom_kg":
                return mock_insert_custom_kg
            return MagicMock()

        mock_registry.get.side_effect = registry_get
        mock_get_registry.return_value = mock_registry

        detected_persons = [
            {"id": "person_002", "name": "Bob", "similarity": 0.88},
        ]
        sync_photo_to_kg("/photos/party.jpg", "Party photo", detected_persons)

        inject_call = mock_insert_custom_kg.call_args_list[0]
        entities = inject_call.kwargs.get("entities") or inject_call[1].get("entities") or inject_call[0][0]

        for entity in entities:
            if entity["entity_type"] == "Person":
                assert "source_id" not in entity, (
                    f"Person entity {entity['entity_name']} must not have source_id, "
                    f"but found: {entity['source_id']}"
                )

    @patch("agent.tool_registry.get_registry")
    def test_person_description_is_just_name(self, mock_get_registry):
        """Person entity description must be only the person name, not a longer phrase."""
        mock_insert = MagicMock(return_value={"status": "ok"})
        mock_insert_custom_kg = MagicMock(return_value={"status": "ok"})
        mock_registry = MagicMock()

        def registry_get(tool_name):
            if tool_name == "lightrag-server/lightrag_insert":
                return mock_insert
            elif tool_name == "lightrag-server/lightrag_insert_custom_kg":
                return mock_insert_custom_kg
            return MagicMock()

        mock_registry.get.side_effect = registry_get
        mock_get_registry.return_value = mock_registry

        detected_persons = [
            {"id": "person_003", "name": "Charlie", "similarity": 0.92},
        ]
        sync_photo_to_kg("/photos/birthday.jpg", "Birthday photo", detected_persons)

        inject_call = mock_insert_custom_kg.call_args_list[0]
        entities = inject_call.kwargs.get("entities") or inject_call[1].get("entities") or inject_call[0][0]

        for entity in entities:
            if entity["entity_type"] == "Person":
                assert entity["description"] == "Charlie", (
                    f"Person entity description should be just the name 'Charlie', "
                    f"but got: {entity['description']!r}"
                )
                # Also verify no "detected in photo" pattern
                assert "detected in photo" not in entity["description"], (
                    f"Person entity description must not contain 'detected in photo', "
                    f"but got: {entity['description']!r}"
                )

    @patch("agent.tool_registry.get_registry")
    def test_person_entity_only_has_required_fields(self, mock_get_registry):
        """Person entity must have exactly: entity_name, entity_type, description."""
        mock_insert = MagicMock(return_value={"status": "ok"})
        mock_insert_custom_kg = MagicMock(return_value={"status": "ok"})
        mock_registry = MagicMock()

        def registry_get(tool_name):
            if tool_name == "lightrag-server/lightrag_insert":
                return mock_insert
            elif tool_name == "lightrag-server/lightrag_insert_custom_kg":
                return mock_insert_custom_kg
            return MagicMock()

        mock_registry.get.side_effect = registry_get
        mock_get_registry.return_value = mock_registry

        detected_persons = [
            {"id": "person_004", "name": "Diana", "similarity": 0.85},
        ]
        sync_photo_to_kg("/photos/wedding.jpg", "Wedding photo", detected_persons)

        inject_call = mock_insert_custom_kg.call_args_list[0]
        entities = inject_call.kwargs.get("entities") or inject_call[1].get("entities") or inject_call[0][0]

        for entity in entities:
            if entity["entity_type"] == "Person":
                expected_keys = {"entity_name", "entity_type", "description"}
                actual_keys = set(entity.keys())
                assert actual_keys == expected_keys, (
                    f"Person entity must have exactly {expected_keys}, "
                    f"but got {actual_keys}"
                )

    @patch("agent.tool_registry.get_registry")
    def test_multiple_persons_all_clean(self, mock_get_registry):
        """All Person entities across multiple detected persons must be clean."""
        mock_insert = MagicMock(return_value={"status": "ok"})
        mock_insert_custom_kg = MagicMock(return_value={"status": "ok"})
        mock_registry = MagicMock()

        def registry_get(tool_name):
            if tool_name == "lightrag-server/lightrag_insert":
                return mock_insert
            elif tool_name == "lightrag-server/lightrag_insert_custom_kg":
                return mock_insert_custom_kg
            return MagicMock()

        mock_registry.get.side_effect = registry_get
        mock_get_registry.return_value = mock_registry

        detected_persons = [
            {"id": "p1", "name": "Eve", "similarity": 0.9},
            {"id": "p2", "name": "Frank", "similarity": 0.8},
            {"id": "p3", "name": "Grace", "similarity": 0.75},
        ]
        sync_photo_to_kg("/photos/group.jpg", "Group photo", detected_persons)

        inject_call = mock_insert_custom_kg.call_args_list[0]
        entities = inject_call.kwargs.get("entities") or inject_call[1].get("entities") or inject_call[0][0]

        person_entities = [e for e in entities if e["entity_type"] == "Person"]
        assert len(person_entities) == 3

        expected_names = {"Eve", "Frank", "Grace"}
        actual_descriptions = {e["description"] for e in person_entities}
        assert actual_descriptions == expected_names, (
            f"Person descriptions must be exactly the names, got: {actual_descriptions}"
        )

        for entity in person_entities:
            assert "file_path" not in entity
            assert "source_id" not in entity
            assert "detected in photo" not in entity["description"]

    @patch("agent.tool_registry.get_registry")
    def test_person_with_no_name_uses_id(self, mock_get_registry):
        """When person has no name, description falls back to person_id."""
        mock_insert = MagicMock(return_value={"status": "ok"})
        mock_insert_custom_kg = MagicMock(return_value={"status": "ok"})
        mock_registry = MagicMock()

        def registry_get(tool_name):
            if tool_name == "lightrag-server/lightrag_insert":
                return mock_insert
            elif tool_name == "lightrag-server/lightrag_insert_custom_kg":
                return mock_insert_custom_kg
            return MagicMock()

        mock_registry.get.side_effect = registry_get
        mock_get_registry.return_value = mock_registry

        detected_persons = [
            {"id": "face_xyz", "name": "", "similarity": 0.7},
        ]
        sync_photo_to_kg("/photos/unknown.jpg", "Unknown person", detected_persons)

        inject_call = mock_insert_custom_kg.call_args_list[0]
        entities = inject_call.kwargs.get("entities") or inject_call[1].get("entities") or inject_call[0][0]

        for entity in entities:
            if entity["entity_type"] == "Person":
                # When name is empty, code falls back to person_id
                assert entity["description"] == "face_xyz", (
                    f"Person with no name should use id as description, got: {entity['description']!r}"
                )
                assert "file_path" not in entity
                assert "source_id" not in entity

    @patch("agent.tool_registry.get_registry")
    def test_relationship_still_has_file_path(self, mock_get_registry):
        """Relationships should still have file_path and source_id (unchanged)."""
        mock_insert = MagicMock(return_value={"status": "ok"})
        mock_insert_custom_kg = MagicMock(return_value={"status": "ok"})
        mock_registry = MagicMock()

        def registry_get(tool_name):
            if tool_name == "lightrag-server/lightrag_insert":
                return mock_insert
            elif tool_name == "lightrag-server/lightrag_insert_custom_kg":
                return mock_insert_custom_kg
            return MagicMock()

        mock_registry.get.side_effect = registry_get
        mock_get_registry.return_value = mock_registry

        detected_persons = [
            {"id": "person_005", "name": "Helen", "similarity": 0.91},
        ]
        sync_photo_to_kg("/photos/concert.jpg", "Concert photo", detected_persons)

        inject_call = mock_insert_custom_kg.call_args_list[0]
        relationships = inject_call.kwargs.get("relationships") or inject_call[1].get("relationships") or inject_call[0][1]

        # The Photo->Person relationship should still have file_path and source_id
        for rel in relationships:
            if rel["keywords"] == "depicts":
                assert "file_path" in rel, "depicts relationship must have file_path"
                assert "source_id" in rel, "depicts relationship must have source_id"


class TestNamePersonKGSync:
    """Verify name_person uses lightrag-server tools via ToolRegistry."""

    @patch("niu_photo_server.get_connection")
    @patch("agent.tool_registry.get_registry")
    def test_name_person_calls_lightrag_tools(self, mock_get_registry, mock_get_conn):
        """name_person must call lightrag-server/lightrag_insert_custom_kg and lightrag_merge_entities via ToolRegistry."""
        # Setup mock DB connection
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = ("person_001", "OldName", "auto_label_001")
        mock_conn.execute.return_value = mock_cursor
        mock_conn.commit.return_value = None
        mock_get_conn.return_value = mock_conn

        # Setup mock ToolRegistry with separate mock functions
        mock_insert_custom_kg = MagicMock(return_value={"status": "ok"})
        mock_merge_entities = MagicMock(return_value={"status": "ok"})
        mock_registry = MagicMock()

        def registry_get(tool_name):
            if tool_name == "lightrag-server/lightrag_insert_custom_kg":
                return mock_insert_custom_kg
            elif tool_name == "lightrag-server/lightrag_merge_entities":
                return mock_merge_entities
            return None

        mock_registry.get.side_effect = registry_get
        mock_get_registry.return_value = mock_registry

        from niu_photo_server import name_person

        result = name_person("person_001", "Alice")

        # Verify ToolRegistry was used
        mock_get_registry.assert_called_once()
        mock_registry.get.assert_any_call("lightrag-server/lightrag_merge_entities")
        mock_registry.get.assert_any_call("lightrag-server/lightrag_insert_custom_kg")

        # Verify insert_custom_kg called to create target entity
        mock_insert_custom_kg.assert_called_once_with(
            entities=[{
                "entity_name": "Alice",
                "entity_type": "person",
                "description": "Alice，原名OldName",
            }],
            relationships=[],
            chunks=[],
            source_id="rename:OldName",
        )

        # Verify the function still returns success
        assert result["status"] == "success"
        assert result["name"] == "Alice"

    @patch("niu_photo_server.get_connection")
    @patch("agent.tool_registry.get_registry")
    def test_name_person_kg_sync_failure_does_not_fail_main(self, mock_get_registry, mock_get_conn):
        """If KG sync fails, name_person must still return success (error is logged, not raised)."""
        # Setup mock DB connection
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = ("person_002", "OldName", "auto_label_002")
        mock_conn.execute.return_value = mock_cursor
        mock_conn.commit.return_value = None
        mock_get_conn.return_value = mock_conn

        # Setup mock ToolRegistry that raises
        mock_insert_entity = MagicMock(side_effect=Exception("KG server unavailable"))
        mock_registry = MagicMock()
        mock_registry.get.return_value = mock_insert_entity
        mock_get_registry.return_value = mock_registry

        from niu_photo_server import name_person

        result = name_person("person_002", "Bob")

        # Main operation should still succeed
        assert result["status"] == "success"
        assert result["name"] == "Bob"

    @patch("niu_photo_server.get_connection")
    @patch("agent.tool_registry.get_registry")
    def test_name_person_does_not_use_lightrag_ingester(self, mock_get_registry, mock_get_conn):
        """name_person must NOT use LightRAGIngester directly (old pattern)."""
        # Setup mock DB connection
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = ("person_003", "OldName", "auto_label_003")
        mock_conn.execute.return_value = mock_cursor
        mock_conn.commit.return_value = None
        mock_get_conn.return_value = mock_conn

        # Setup mock ToolRegistry
        mock_insert_entity = MagicMock(return_value={"status": "ok"})
        mock_registry = MagicMock()
        mock_registry.get.return_value = mock_insert_entity
        mock_get_registry.return_value = mock_registry

        with patch("niu_api.internal.lightrag_adapter.LightRAGIngester") as mock_ingester_cls:
            from niu_photo_server import name_person

            name_person("person_003", "Charlie")

            # LightRAGIngester must NOT have been instantiated
            mock_ingester_cls.assert_not_called()


class TestNoteInjectUsesLightragInsert:
    """Verify _inject_note_to_lightrag uses lightrag-server/lightrag_insert via ToolRegistry
    (per-note content + independent doc_id note:{id}), NOT LightRAGIngester.inject_entity."""

    @patch("agent.tool_registry.get_registry")
    def test_inject_note_calls_lightrag_insert_with_full_json(self, mock_get_registry, tmp_path):
        """_inject_note_to_lightrag must inject each note via lightrag-server/lightrag_insert (per-note content + independent doc_id)."""
        import json

        from agent.injector.sync import SkillSync

        # Setup mock ToolRegistry
        mock_insert = MagicMock(return_value={"status": "ok"})
        mock_registry = MagicMock()
        mock_registry.get.return_value = mock_insert
        mock_get_registry.return_value = mock_registry

        with patch("pathlib.Path.home", return_value=tmp_path):
            sync = SkillSync(skills_dir=str(tmp_path / "skills"), use_watchdog=False)

        notes_data = [
            {"id": "n1", "content": "Hello", "tags": ["greeting"]},
            {"id": "n2", "content": "World", "tags": []},
        ]

        result = sync._inject_note_to_lightrag(notes_data)

        # Must return empty set on success
        assert result == set()

        # Must use ToolRegistry
        mock_get_registry.assert_called_once()

        # Must request lightrag-server/lightrag_insert (NOT lightrag_insert_entity)
        mock_registry.get.assert_called_once_with("lightrag-server/lightrag_insert")

        # Per-note injection: one lightrag_insert per note (content=single-note JSON + doc_id=note:{id})
        assert mock_insert.call_count == 2
        injected = {}
        for call in mock_insert.call_args_list:
            kwargs = call.kwargs
            assert "content" in kwargs and "doc_id" in kwargs
            parsed = json.loads(kwargs["content"])
            assert isinstance(parsed, dict)
            injected[parsed["id"]] = kwargs["doc_id"]
        assert set(injected) == {"n1", "n2"}
        assert injected["n1"] == "note:n1"
        assert injected["n2"] == "note:n2"

    @patch("agent.tool_registry.get_registry")
    def test_inject_note_does_not_call_inject_entity(self, mock_get_registry, tmp_path):
        """_inject_note_to_lightrag must NOT call LightRAGIngester.inject_entity (old per-item pattern)."""
        from agent.injector.sync import SkillSync

        mock_insert = MagicMock(return_value={"status": "ok"})
        mock_registry = MagicMock()
        mock_registry.get.return_value = mock_insert
        mock_get_registry.return_value = mock_registry

        with patch("pathlib.Path.home", return_value=tmp_path):
            sync = SkillSync(skills_dir=str(tmp_path / "skills"), use_watchdog=False)

        with patch("niu_api.internal.lightrag_adapter.LightRAGIngester") as mock_ingester_cls:
            notes_data = [{"id": "n1", "content": "Test", "tags": []}]
            sync._inject_note_to_lightrag(notes_data)

            # LightRAGIngester must NOT have been instantiated
            mock_ingester_cls.assert_not_called()

    @patch("agent.tool_registry.get_registry")
    def test_inject_note_failure_returns_false(self, mock_get_registry, tmp_path):
        """If lightrag_insert fails, _inject_note_to_lightrag must return failed note IDs."""
        from agent.injector.sync import SkillSync

        mock_insert = MagicMock(return_value={"status": "error", "message": "down"})
        mock_registry = MagicMock()
        mock_registry.get.return_value = mock_insert
        mock_get_registry.return_value = mock_registry

        with patch("pathlib.Path.home", return_value=tmp_path):
            sync = SkillSync(skills_dir=str(tmp_path / "skills"), use_watchdog=False)

        notes_data = [{"id": "n1", "content": "Test", "tags": []}]
        result = sync._inject_note_to_lightrag(notes_data)
        assert result == {"n1"}

    @patch("agent.tool_registry.get_registry")
    def test_inject_note_exception_returns_false(self, mock_get_registry, tmp_path):
        """If ToolRegistry raises, _inject_note_to_lightrag must return all note IDs as failed."""
        from agent.injector.sync import SkillSync

        mock_registry = MagicMock()
        mock_registry.get.side_effect = Exception("registry unavailable")
        mock_get_registry.return_value = mock_registry

        with patch("pathlib.Path.home", return_value=tmp_path):
            sync = SkillSync(skills_dir=str(tmp_path / "skills"), use_watchdog=False)

        notes_data = [{"id": "n1", "content": "Test", "tags": []}]
        result = sync._inject_note_to_lightrag(notes_data)
        assert result == {"n1"}

    @patch("agent.tool_registry.get_registry")
    def test_scan_notes_passes_changed_notes_as_full_list(self, mock_get_registry):
        """_scan_notes must inject each changed note via lightrag_insert (per-note content + independent doc_id)."""
        import json
        import os

        from agent.injector.sync import SkillSync

        mock_insert = MagicMock(return_value={"status": "ok"})
        mock_registry = MagicMock()
        mock_registry.get.return_value = mock_insert
        mock_get_registry.return_value = mock_registry

        # Create temp workspace with notes
        import tempfile
        with tempfile.TemporaryDirectory() as tmp_ws:
            notes_dir = os.path.join(tmp_ws, "notes")
            os.makedirs(notes_dir, exist_ok=True)
            notes_file = os.path.join(notes_dir, "notes.json")
            notes_data = [
                {"id": "n1", "content": "Hello", "tags": ["greeting"]},
                {"id": "n2", "content": "World", "tags": []},
            ]
            with open(notes_file, "w", encoding="utf-8") as f:
                json.dump(notes_data, f, ensure_ascii=False)

            import pathlib
            with patch("pathlib.Path.home", return_value=pathlib.Path(tmp_ws)):
                sync = SkillSync(skills_dir=os.path.join(tmp_ws, "skills"), use_watchdog=False)
            with patch.dict(os.environ, {"WORKSPACE_PATH": tmp_ws}):
                added, updated = sync._scan_notes()

            # Both new notes should be counted
            assert added == 2

            # Per-note injection: one lightrag_insert per changed note, each with its own doc_id
            assert mock_insert.call_count == 2
            injected_ids = set()
            for call in mock_insert.call_args_list:
                kwargs = call.kwargs
                parsed = json.loads(kwargs["content"])
                injected_ids.add(parsed["id"])
                assert kwargs["doc_id"] == f"note:{parsed['id']}"
            assert injected_ids == {"n1", "n2"}


class TestMergePersonsKGSync:
    """Verify merge_persons uses lightrag-server tools via ToolRegistry."""

    def _make_mock_conn(self, name_a="Alice", auto_label_a="auto_a", name_b=None, auto_label_b="auto_b"):
        """Create a mock DB connection that simulates the merge_persons DB flow.

        merge_persons does:
        1. SELECT ... WHERE id IN (?, ?) -> fetchall() returns 2 person rows
        2. UPDATE faces SET person_id = ...
        3. SELECT COUNT(DISTINCT photo_id) -> fetchone() returns (3,)
        4. UPDATE persons SET ...
        5. DELETE/SELECT/INSERT/DELETE on co_occurrences
        6. DELETE persons WHERE id = person_b
        7. conn.commit()
        """
        mock_conn = MagicMock()

        # Person A row: (id, name, auto_label, center_embedding, threshold_adjustment, photo_count)
        person_a_row = ("person_a", name_a, auto_label_a, b"\x00" * 16, 0.0, 2)
        person_b_row = ("person_b", name_b, auto_label_b, b"\x00" * 16, 0.0, 1)

        # We need different return values for different execute calls.
        # Use side_effect to return appropriate cursors for each call.

        def mock_execute(sql, params=None):
            sql_upper = sql.upper().strip()
            mock_cursor = MagicMock()

            if sql_upper.startswith("SELECT") and "PERSONS" in sql_upper and "IN" in sql_upper:
                # First query: get both persons
                mock_cursor.fetchall.return_value = [person_a_row, person_b_row]
            elif sql_upper.startswith("SELECT") and "COUNT" in sql_upper:
                # Photo count query
                mock_cursor.fetchone.return_value = (3,)
            elif sql_upper.startswith("SELECT") and "CO_OCCURRENCES" in sql_upper:
                # Co-occurrence queries - return empty
                if "COUNT" in sql_upper or "person_a_id" in sql:
                    mock_cursor.fetchone.return_value = None
                else:
                    mock_cursor.fetchall.return_value = []
            else:
                # UPDATE, DELETE, INSERT - no return needed
                mock_cursor.fetchone.return_value = None
                mock_cursor.fetchall.return_value = []

            return mock_cursor

        mock_conn.execute.side_effect = mock_execute
        mock_conn.commit.return_value = None
        return mock_conn

    @patch("niu_photo_server.get_connection")
    @patch("agent.tool_registry.get_registry")
    def test_merge_persons_calls_lightrag_tools(self, mock_get_registry, mock_get_conn):
        """merge_persons must call lightrag_insert_custom_kg and lightrag_merge_entities via ToolRegistry."""
        mock_conn = self._make_mock_conn()
        mock_get_conn.return_value = mock_conn

        # Setup mock ToolRegistry with separate mock functions for each tool
        mock_insert_custom_kg = MagicMock(return_value={"status": "ok"})
        mock_merge_entities = MagicMock(return_value={"status": "ok"})
        mock_registry = MagicMock()

        def registry_get(tool_name):
            if tool_name == "lightrag-server/lightrag_insert_custom_kg":
                return mock_insert_custom_kg
            elif tool_name == "lightrag-server/lightrag_merge_entities":
                return mock_merge_entities
            return MagicMock()

        mock_registry.get.side_effect = registry_get
        mock_get_registry.return_value = mock_registry

        from niu_photo_server import merge_persons

        result = merge_persons("person_a", "person_b")

        # Verify ToolRegistry was used for both tools
        mock_get_registry.assert_called_once()
        assert mock_registry.get.call_count == 2
        mock_registry.get.assert_any_call("lightrag-server/lightrag_insert_custom_kg")
        mock_registry.get.assert_any_call("lightrag-server/lightrag_merge_entities")

        # Verify lightrag_insert_custom_kg called to create/update person_a entity
        mock_insert_custom_kg.assert_called_once()

        # Verify lightrag_merge_entities called with correct parameters
        # merge_persons uses KG entity names (name_a, auto_label_b) not person IDs
        mock_merge_entities.assert_called_once_with(
            source_entities=["auto_b"],
            target_entity="Alice",
        )

        # Verify the function still returns success
        assert result["status"] == "success"
        assert result["merged_into"] == "person_a"

    @patch("niu_photo_server.get_connection")
    @patch("agent.tool_registry.get_registry")
    def test_merge_persons_kg_sync_failure_does_not_fail_main(self, mock_get_registry, mock_get_conn):
        """If KG sync fails, merge_persons must still return success (error is logged, not raised)."""
        mock_conn = self._make_mock_conn(name_a="Bob")
        mock_get_conn.return_value = mock_conn

        # Setup mock ToolRegistry that raises
        mock_insert_entity = MagicMock(side_effect=Exception("KG server unavailable"))
        mock_registry = MagicMock()
        mock_registry.get.return_value = mock_insert_entity
        mock_get_registry.return_value = mock_registry

        from niu_photo_server import merge_persons

        result = merge_persons("person_a", "person_b")

        # Main operation should still succeed
        assert result["status"] == "success"
        assert result["merged_into"] == "person_a"

    @patch("niu_photo_server.get_connection")
    @patch("agent.tool_registry.get_registry")
    def test_merge_persons_does_not_use_lightrag_ingester(self, mock_get_registry, mock_get_conn):
        """merge_persons must NOT use LightRAGIngester or get_lightrag directly (old pattern)."""
        mock_conn = self._make_mock_conn()
        mock_get_conn.return_value = mock_conn

        # Setup mock ToolRegistry
        mock_insert_custom_kg = MagicMock(return_value={"status": "ok"})
        mock_merge_entities = MagicMock(return_value={"status": "ok"})
        mock_registry = MagicMock()

        def registry_get(tool_name):
            if tool_name == "lightrag-server/lightrag_insert_custom_kg":
                return mock_insert_custom_kg
            elif tool_name == "lightrag-server/lightrag_merge_entities":
                return mock_merge_entities
            return MagicMock()

        mock_registry.get.side_effect = registry_get
        mock_get_registry.return_value = mock_registry

        with patch("niu_api.internal.lightrag_adapter.LightRAGIngester") as mock_ingester_cls, \
             patch("niu_api.internal.lightrag_manager.get_lightrag") as mock_get_lightrag:
            from niu_photo_server import merge_persons

            merge_persons("person_a", "person_b")

            # LightRAGIngester must NOT have been instantiated
            mock_ingester_cls.assert_not_called()
            # get_lightrag must NOT have been called
            mock_get_lightrag.assert_not_called()


class TestToolAliasesNoLegacyServers:
    """Verify _TOOL_ALIASES contains no vector-store/ or kg-server/ entries.

    These legacy server prefixes have been fully migrated to lightrag-server.
    Any remaining aliases would be dead code that obscures the true call path.
    """

    def test_no_vector_store_aliases(self):
        """_TOOL_ALIASES must not contain any key starting with 'vector-store/'."""
        import sys

        sys.path.insert(0, "E:/tools/ai-bot")
        from agent.handler import NiuHandler

        vs_keys = [k for k in NiuHandler._TOOL_ALIASES if k.startswith("vector-store/")]
        assert vs_keys == [], (
            f"_TOOL_ALIASES still contains vector-store/ keys: {vs_keys}. "
            "Remove them — the vector-store server has been replaced by lightrag-server."
        )

    def test_no_kg_server_aliases(self):
        """_TOOL_ALIASES must not contain any key starting with 'kg-server/'."""
        import sys

        sys.path.insert(0, "E:/tools/ai-bot")
        from agent.handler import NiuHandler

        kg_keys = [k for k in NiuHandler._TOOL_ALIASES if k.startswith("kg-server/")]
        assert kg_keys == [], (
            f"_TOOL_ALIASES still contains kg-server/ keys: {kg_keys}. "
            "Remove them — the kg-server has been replaced by lightrag-server."
        )


class TestNoDirectNetworkXOrLightRAGIngesterInPhotoServer:
    """Verify photo-server code has no direct NetworkX graph operations,
    no LightRAGIngester imports, and no inject_entity/inject_relation calls.

    All KG operations in photo-server must go through ToolRegistry
    (lightrag-server/lightrag_insert, lightrag_insert_entity, etc.).
    """

    PHOTO_SERVER_FILE = "E:/tools/ai-bot/mcp-servers/photo-server/src/niu_photo_server/__init__.py"

    def test_no_lightrag_ingester_import(self):
        """photo-server must NOT import LightRAGIngester."""
        with open(self.PHOTO_SERVER_FILE, encoding="utf-8") as f:
            source = f.read()
        assert "LightRAGIngester" not in source, (
            "photo-server still imports LightRAGIngester. "
            "All KG operations must use ToolRegistry (lightrag-server tools)."
        )

    def test_no_lightrag_adapter_import(self):
        """photo-server must NOT import from lightrag_adapter."""
        with open(self.PHOTO_SERVER_FILE, encoding="utf-8") as f:
            source = f.read()
        assert "lightrag_adapter" not in source, (
            "photo-server still imports from lightrag_adapter. "
            "All KG operations must use ToolRegistry (lightrag-server tools)."
        )

    def test_no_inject_entity_call(self):
        """photo-server must NOT call inject_entity (removed method)."""
        with open(self.PHOTO_SERVER_FILE, encoding="utf-8") as f:
            source = f.read()
        assert ".inject_entity(" not in source, (
            "photo-server still calls inject_entity. "
            "Use ToolRegistry (lightrag-server/lightrag_insert_entity) instead."
        )

    def test_no_inject_relation_call(self):
        """photo-server must NOT call inject_relation (removed method)."""
        with open(self.PHOTO_SERVER_FILE, encoding="utf-8") as f:
            source = f.read()
        assert ".inject_relation(" not in source, (
            "photo-server still calls inject_relation. "
            "Use ToolRegistry (lightrag-server/lightrag_insert_relation) instead."
        )

    def test_no_direct_networkx_operations(self):
        """photo-server must NOT use direct NetworkX graph operations."""
        with open(self.PHOTO_SERVER_FILE, encoding="utf-8") as f:
            source = f.read()
        nx_patterns = [
            "_graph.add_node",
            "_graph.add_edge",
            "nx.add_node",
            "nx.add_edge",
            "chunk_entity_relation_graph",
        ]
        found = [p for p in nx_patterns if p in source]
        assert found == [], (
            f"photo-server still has direct NetworkX operations: {found}. "
            "All graph operations must go through lightrag-server tools."
        )

    def test_no_lightrag_manager_direct_usage_in_sync_photo_to_kg(self):
        """sync_photo_to_kg must NOT call get_lightrag or call_async directly."""
        with open(self.PHOTO_SERVER_FILE, encoding="utf-8") as f:
            source = f.read()
        # Find the sync_photo_to_kg function boundaries
        start = source.find("def sync_photo_to_kg(")
        if start == -1:
            return  # Function not found, skip
        # Find the next function definition
        next_def = source.find("\ndef ", start + 1)
        func_body = source[start:next_def] if next_def != -1 else source[start:]

        assert "get_lightrag" not in func_body, (
            "sync_photo_to_kg must use ToolRegistry (lightrag-server/lightrag_insert), "
            "not call get_lightrag directly."
        )
        assert "call_async" not in func_body, (
            "sync_photo_to_kg must use ToolRegistry (lightrag-server/lightrag_insert), "
            "not call call_async directly."
        )


class TestNoInjectEntityOrInjectRelationInLightragAdapter:
    """Verify LightRAGIngester no longer has inject_entity or inject_relation methods."""

    ADAPTER_FILE = "E:/tools/ai-bot/niu_api/internal/lightrag_adapter.py"

    def test_no_inject_entity_method(self):
        """LightRAGIngester must NOT have inject_entity method."""
        with open(self.ADAPTER_FILE, encoding="utf-8") as f:
            source = f.read()
        # Find the class boundaries
        start = source.find("class LightRAGIngester:")
        if start == -1:
            return  # Class removed entirely, that's fine
        # Find next class or end of file
        next_class = source.find("\nclass ", start + 1)
        class_body = source[start:next_class] if next_class != -1 else source[start:]

        assert "def inject_entity(" not in class_body, (
            "LightRAGIngester still has inject_entity method. "
            "It should be deleted — use inject_custom_kg directly."
        )

    def test_no_inject_relation_method(self):
        """LightRAGIngester must NOT have inject_relation method."""
        with open(self.ADAPTER_FILE, encoding="utf-8") as f:
            source = f.read()
        start = source.find("class LightRAGIngester:")
        if start == -1:
            return
        next_class = source.find("\nclass ", start + 1)
        class_body = source[start:next_class] if next_class != -1 else source[start:]

        assert "def inject_relation(" not in class_body, (
            "LightRAGIngester still has inject_relation method. "
            "It should be deleted — use inject_custom_kg directly."
        )


class TestDeletedVectorStoreAndKGServer:
    """Verify that deprecated vector-store and kg-server MCP servers have been fully removed.

    These servers were replaced by the unified lightrag-server. This test ensures
    no remnants (directories, modules, or imports) remain in the codebase.
    """

    PROJECT_ROOT = "E:/tools/ai-bot"

    def test_vector_store_directory_does_not_exist(self):
        """mcp-servers/vector-store/ directory must not exist."""
        import os
        vs_dir = os.path.join(self.PROJECT_ROOT, "mcp-servers", "vector-store")
        assert not os.path.isdir(vs_dir), (
            f"mcp-servers/vector-store/ still exists at {vs_dir}. "
            "It should be deleted — replaced by lightrag-server."
        )

    def test_kg_server_directory_does_not_exist(self):
        """mcp-servers/kg-server/ directory must not exist."""
        import os
        kg_dir = os.path.join(self.PROJECT_ROOT, "mcp-servers", "kg-server")
        assert not os.path.isdir(kg_dir), (
            f"mcp-servers/kg-server/ still exists at {kg_dir}. "
            "It should be deleted — replaced by lightrag-server."
        )

    def test_vector_search_module_does_not_exist(self):
        """agent/vector_search.py must not exist."""
        import os
        vs_file = os.path.join(self.PROJECT_ROOT, "agent", "vector_search.py")
        assert not os.path.isfile(vs_file), (
            f"agent/vector_search.py still exists at {vs_file}. "
            "It should be deleted — VectorSearchAdapter replaced by lightrag-server."
        )

    def test_vector_cleanup_module_does_not_exist(self):
        """agent/vector_cleanup.py must not exist (depended entirely on vector_search)."""
        import os
        vc_file = os.path.join(self.PROJECT_ROOT, "agent", "vector_cleanup.py")
        assert not os.path.isfile(vc_file), (
            f"agent/vector_cleanup.py still exists at {vc_file}. "
            "It should be deleted — depended entirely on deleted vector_search."
        )

    def test_no_python_files_import_vector_search(self):
        """No Python files under agent/ or mcp-servers/ should import from vector_search."""
        import os
        import re

        forbidden_patterns = [
            r"from\s+\.\s*vector_search\s+import",
            r"from\s+agent\.vector_search\s+import",
            r"import\s+agent\.vector_search",
            r"from\s+vector_search\s+import",
        ]

        violations = []
        search_dirs = [
            os.path.join(self.PROJECT_ROOT, "agent"),
            os.path.join(self.PROJECT_ROOT, "mcp-servers"),
        ]

        for search_dir in search_dirs:
            if not os.path.isdir(search_dir):
                continue
            for root, _dirs, files in os.walk(search_dir):
                for fname in files:
                    if not fname.endswith(".py"):
                        continue
                    fpath = os.path.join(root, fname)
                    try:
                        with open(fpath, encoding="utf-8") as f:
                            source = f.read()
                    except Exception:
                        continue
                    for pattern in forbidden_patterns:
                        if re.search(pattern, source):
                            rel = os.path.relpath(fpath, self.PROJECT_ROOT)
                            violations.append(f"{rel}: matches '{pattern}'")

        assert violations == [], (
            "Found Python files still importing from vector_search:\n"
            + "\n".join(violations)
            + "\nAll vector_search references should be removed."
        )

    def test_no_python_files_reference_vector_search_adapter(self):
        """No Python files under agent/ or mcp-servers/ should reference VectorSearchAdapter."""
        import os

        violations = []
        search_dirs = [
            os.path.join(self.PROJECT_ROOT, "agent"),
            os.path.join(self.PROJECT_ROOT, "mcp-servers"),
        ]

        for search_dir in search_dirs:
            if not os.path.isdir(search_dir):
                continue
            for root, _dirs, files in os.walk(search_dir):
                for fname in files:
                    if not fname.endswith(".py"):
                        continue
                    fpath = os.path.join(root, fname)
                    try:
                        with open(fpath, encoding="utf-8") as f:
                            source = f.read()
                    except Exception:
                        continue
                    if "VectorSearchAdapter" in source:
                        rel = os.path.relpath(fpath, self.PROJECT_ROOT)
                        violations.append(rel)

        assert violations == [], (
            "Found Python files still referencing VectorSearchAdapter:\n"
            + "\n".join(violations)
            + "\nAll VectorSearchAdapter references should be removed."
        )

    def test_no_vector_store_or_kg_server_in_mcp_servers_yaml(self):
        """config/mcp-servers.yaml must not contain vector-store or kg-server entries."""
        import os

        yaml_path = os.path.join(self.PROJECT_ROOT, "config", "mcp-servers.yaml")
        if not os.path.isfile(yaml_path):
            return  # File removed entirely, that's fine

        with open(yaml_path, encoding="utf-8") as f:
            content = f.read()

        # Check that neither server name appears as a YAML key
        for server_name in ["vector-store", "kg-server"]:
            # Look for the server name as a top-level YAML key (at line start, followed by colon)
            import re
            pattern = rf"^{re.escape(server_name)}\s*:"
            matches = re.findall(pattern, content, re.MULTILINE)
            assert matches == [], (
                f"config/mcp-servers.yaml still contains '{server_name}' entry. "
                "It should be removed — replaced by lightrag-server."
            )
