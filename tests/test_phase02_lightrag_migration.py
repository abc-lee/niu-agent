"""
Phase 02 Integration Tests — KuzuDB → LightRAG Migration

Validates that all KuzuDB/kg-server references have been replaced
with LightRAG adapter/pipeline calls across the codebase.
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock, call
import json
import sqlite3
import tempfile
from pathlib import Path


# ============== 1. KGSync → LightRAGSync delegation ==============


class TestKGSyncDelegation:
    """KGSync should delegate to LightRAGSync, not KuzuDB."""

    def test_kg_sync_run_delegates_to_lightrag_sync(self):
        """KGSync.run_full_sync() should call LightRAGSync.run_sync()."""
        from agent.injector.kg_sync import KGSync

        ks = KGSync()
        with patch("agent.injector.lightrag_sync.LightRAGSync") as MockSync:
            mock_instance = MagicMock()
            mock_instance.run_sync.return_value = {"photos_synced": 5}
            MockSync.return_value = mock_instance

            result = ks.run_full_sync()
            MockSync.assert_called_once()
            mock_instance.run_sync.assert_called_once()
            assert result == {"photos_synced": 5}

    def test_kg_sync_start_delegates_to_lightrag_sync(self):
        """KGSync.start_background_sync() should start LightRAGSync."""
        from agent.injector.kg_sync import KGSync

        ks = KGSync()
        with patch("agent.injector.lightrag_sync.get_lightrag_sync") as mock_get:
            mock_instance = MagicMock()
            mock_get.return_value = mock_instance

            ks.start_background_sync()
            mock_get.assert_called_once()

    def test_kg_sync_stop_delegates_to_lightrag_sync(self):
        """KGSync.stop_background_sync() should stop LightRAGSync."""
        from agent.injector.kg_sync import KGSync

        ks = KGSync()
        mock_lightrag_sync = MagicMock()
        ks._lightrag_sync = mock_lightrag_sync

        ks.stop_background_sync()
        mock_lightrag_sync.stop_background_sync.assert_called_once()

    def test_no_niu_kg_server_import_in_kg_sync(self):
        """kg_sync.py should NOT import niu_kg_server."""
        import agent.injector.kg_sync as mod
        source = Path(mod.__file__).read_text(encoding="utf-8")
        assert "from niu_kg_server" not in source
        assert "import niu_kg_server" not in source


# ============== 2. KGScanner disabled ==============


class TestKGScannerDisabled:
    """KGScanner should be disabled — LightRAG handles entity extraction."""

    def test_get_kg_scanner_returns_none(self):
        """get_kg_scanner() should return None."""
        from agent.injector.kg_scanner import get_kg_scanner
        assert get_kg_scanner() is None

    def test_kg_scanner_class_is_noop(self):
        """KGScanner methods should be no-ops."""
        from agent.injector.kg_scanner import KGScanner

        scanner = KGScanner()
        scanner.start()  # should not raise
        scanner.stop()  # should not raise
        result = scanner.scan_and_extract()
        assert result == []

    def test_global_kg_scanner_is_none(self):
        """Global _kg_scanner should be None."""
        from agent.injector.kg_scanner import _kg_scanner
        assert _kg_scanner is None

    def test_no_niu_kg_server_import_in_kg_scanner(self):
        """kg_scanner.py should NOT import niu_kg_server."""
        import agent.injector.kg_scanner as mod
        source = Path(mod.__file__).read_text(encoding="utf-8")
        assert "niu_kg_server" not in source


# ============== 3. kg_api.py → LightRAGAdapter ==============


class TestKgApiUsesLightRAG:
    """kg_api.py should use LightRAGAdapter, not niu_kg_server."""

    def test_no_niu_kg_server_import_in_kg_api(self):
        """kg_api.py should NOT import niu_kg_server."""
        import niu_api.kg_api as mod
        source = Path(mod.__file__).read_text(encoding="utf-8")
        assert "niu_kg_server" not in source

    def test_kg_api_snapshot_uses_adapter(self):
        """graph_snapshot should use LightRAGAdapter.get_graph_snapshot()."""
        from niu_api.kg_api import graph_snapshot

        mock_adapter = MagicMock()
        mock_adapter.get_graph_snapshot.return_value = {"nodes": [], "edges": []}

        with patch("niu_api.kg_api._get_adapter", return_value=mock_adapter):
            # Pass explicit min_confidence to avoid FastAPI Query object comparison
            result = graph_snapshot(limit=200, min_confidence=0.0)
            mock_adapter.get_graph_snapshot.assert_called_once()
            assert "nodes" in result

    def test_kg_api_explore_uses_adapter(self):
        """explore_node should use LightRAGAdapter.explore_node()."""
        from niu_api.kg_api import explore_node, ExploreRequest

        mock_adapter = MagicMock()
        mock_adapter.explore_node.return_value = {"center": None, "nodes": [], "edges": [], "stats": {"nodes": 0, "edges": 0}}

        with patch("niu_api.kg_api._get_adapter", return_value=mock_adapter):
            req = ExploreRequest(entity_id="person:123")
            result = explore_node(req)
            mock_adapter.explore_node.assert_called_once()
            assert "nodes" in result

    def test_kg_api_stats_uses_lightrag_manager(self):
        """graph_stats should use lightrag_manager.get_lightrag_status()."""
        from niu_api.kg_api import graph_stats

        mock_status = {"installed": True, "initialized": True}
        with patch("niu_api.internal.lightrag_manager.get_lightrag_status", return_value=mock_status):
            result = graph_stats()
            assert result["installed"] is True


# ============== 4. notes_api.py → LightRAG ainsert ==============


class TestNotesApiUsesLightRAG:
    """notes_api sync_note_to_kg should use LightRAG ainsert."""

    def test_no_niu_kg_server_import_in_notes_api(self):
        """notes_api.py should NOT import niu_kg_server."""
        import niu_api.notes_api as mod
        source = Path(mod.__file__).read_text(encoding="utf-8")
        assert "niu_kg_server" not in source

    def test_sync_note_to_kg_calls_ainsert(self):
        """sync_note_to_kg should call rag.ainsert()."""
        from niu_api.notes_api import sync_note_to_kg

        mock_rag = MagicMock()
        mock_rag.ainsert = AsyncMock(return_value=None)

        with patch("niu_api.internal.lightrag_manager.get_lightrag", return_value=mock_rag):
            with patch("niu_api.internal.lightrag_manager.call_async", return_value=None) as mock_call:
                sync_note_to_kg("note-1", "Shopping list: milk, eggs")
                # call_async should have been called
                mock_call.assert_called_once()

    def test_sync_note_to_kg_handles_no_lightrag(self):
        """sync_note_to_kg should handle LightRAG not available."""
        from niu_api.notes_api import sync_note_to_kg

        with patch("niu_api.internal.lightrag_manager.get_lightrag", return_value=None):
            # Should not raise, just log warning
            sync_note_to_kg("note-1", "test content")


# ============== 5. LightRAGSync ==============


class TestLightRAGSync:
    """LightRAGSync should sync photos and documents to LightRAG."""

    def test_lightrag_sync_init(self):
        """LightRAGSync should initialize with default interval."""
        from agent.injector.lightrag_sync import LightRAGSync

        sync = LightRAGSync()
        assert sync.sync_interval == 21600  # 6 hours

    def test_lightrag_sync_init_custom_interval(self):
        """LightRAGSync should accept custom interval."""
        from agent.injector.lightrag_sync import LightRAGSync

        sync = LightRAGSync(sync_interval=3600)
        assert sync.sync_interval == 3600

    def test_lightrag_sync_no_niu_kg_server(self):
        """lightrag_sync.py should NOT import niu_kg_server."""
        import agent.injector.lightrag_sync as mod
        source = Path(mod.__file__).read_text(encoding="utf-8")
        assert "niu_kg_server" not in source

    def test_lightrag_sync_photos_db_uses_ainsert(self):
        """_sync_photos_db should use ainsert for photos."""
        from agent.injector.lightrag_sync import LightRAGSync

        sync = LightRAGSync()
        # Create a temp photos.db with test data
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE persons (id TEXT, name TEXT, auto_label TEXT)")
        conn.execute("CREATE TABLE photos (id TEXT, file_path TEXT, abstract TEXT)")
        conn.execute("CREATE TABLE co_occurrences (person_a_id TEXT, person_b_id TEXT, count INTEGER)")
        conn.execute("INSERT INTO persons VALUES ('p1', 'Alice', 'Person A')")
        conn.execute("INSERT INTO photos VALUES ('ph1', '/photos/test.jpg', 'A photo of Alice')")
        conn.execute("INSERT INTO co_occurrences VALUES ('p1', 'p2', 3)")
        conn.commit()
        conn.close()

        mock_rag = MagicMock()
        mock_rag.ainsert = AsyncMock(return_value=None)

        with patch("niu_api.internal.lightrag_manager.get_lightrag", return_value=mock_rag):
            with patch("niu_api.internal.lightrag_manager.call_async", return_value=None) as mock_call:
                with patch("niu_api.internal.lightrag_adapter.LightRAGIngester") as MockIngester:
                    mock_ingester = MagicMock()
                    mock_ingester.inject_entity.return_value = {"status": "ok"}
                    mock_ingester.inject_relation.return_value = {"status": "ok"}
                    MockIngester.return_value = mock_ingester

                    # Mock the photo server module import inside _sync_photos_db
                    mock_ps = MagicMock()
                    mock_ps.get_db_path.return_value = db_path
                    with patch.dict("sys.modules", {"niu_photo_server": mock_ps}):
                        import sys
                        photos, persons, _, _, _ = sync._sync_photos_db(set(), set(), set())
                        # Should have synced at least 1 photo and 1 person
                        assert photos >= 1
                        assert persons >= 1

        # Cleanup
        Path(db_path).unlink(missing_ok=True)

    def test_lightrag_sync_vectors_db_filters_documents(self):
        """_sync_vectors_db should only sync category=document records."""
        from agent.injector.lightrag_sync import LightRAGSync

        sync = LightRAGSync()
        # Create a temp vectors.db
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE documents (id TEXT, content TEXT, metadata TEXT)")
        conn.execute("""INSERT INTO documents VALUES ('d1', 'Doc content', '{"category": "document", "file_path": "/doc/test.pdf"}')""")
        conn.execute("""INSERT INTO documents VALUES ('d2', 'Not a doc', '{"category": "interaction_habit"}')""")
        conn.commit()
        conn.close()

        mock_rag = MagicMock()
        mock_rag.ainsert = AsyncMock(return_value=None)

        with patch("niu_api.internal.lightrag_manager.get_lightrag", return_value=mock_rag):
            with patch("niu_api.internal.lightrag_manager.call_async", return_value=None) as mock_call:
                with patch("agent.vector_search.resolve_vector_db_path", return_value=db_path):
                    synced, _ = sync._sync_vectors_db(set())
                    # Only 1 document should be synced (category=document)
                    assert synced == 1

        Path(db_path).unlink(missing_ok=True)

    def test_lightrag_sync_background_start_stop(self):
        """LightRAGSync should start and stop background thread."""
        from agent.injector.lightrag_sync import LightRAGSync

        sync = LightRAGSync(sync_interval=999999)  # Long interval so it doesn't run
        sync.start_background_sync()
        assert sync._thread is not None
        assert sync._thread.is_alive()

        sync.stop_background_sync()
        # Thread should stop (with timeout)
        assert not sync._thread.is_alive()

    def test_get_lightrag_sync_singleton(self):
        """get_lightrag_sync should return singleton instance."""
        from agent.injector.lightrag_sync import get_lightrag_sync, _lightrag_sync_lock
        import agent.injector.lightrag_sync as mod

        # Reset global for clean test
        original = mod._lightrag_sync
        mod._lightrag_sync = None

        try:
            sync1 = get_lightrag_sync()
            sync2 = get_lightrag_sync()
            assert sync1 is sync2
        finally:
            mod._lightrag_sync = original


# ============== 6. __main__.py startup/shutdown ==============


class TestMainLifespan:
    """__main__.py should use LightRAG, not KGScanner/KGSync."""

    def test_no_kg_scanner_in_main(self):
        """__main__.py should NOT reference KGScanner."""
        import niu_api.__main__ as mod
        source = Path(mod.__file__).read_text(encoding="utf-8")
        assert "kg_scanner" not in source
        assert "KGScanner" not in source

    def test_lightrag_sync_in_startup(self):
        """__main__.py startup should reference lightrag_sync."""
        import niu_api.__main__ as mod
        source = Path(mod.__file__).read_text(encoding="utf-8")
        assert "lightrag_sync" in source

    def test_lightrag_sync_in_shutdown(self):
        """__main__.py shutdown should stop lightrag_sync."""
        import niu_api.__main__ as mod
        source = Path(mod.__file__).read_text(encoding="utf-8")
        # Find shutdown section
        shutdown_idx = source.find("Shutdown")
        assert shutdown_idx > 0
        shutdown_section = source[shutdown_idx:]
        assert "lightrag_sync" in shutdown_section
        assert "stop_background_sync" in shutdown_section


# ============== 7. mcp_loader.py — kg-server removed ==============


class TestMcpLoaderNoKgServer:
    """mcp_loader.py should not load kg-server."""

    def test_kg_server_not_in_required_servers(self):
        """kg-server should not be in REQUIRED_SERVERS."""
        from agent.mcp_loader import REQUIRED_SERVERS
        server_names = [name for name, _ in REQUIRED_SERVERS]
        assert "kg-server" not in server_names

    def test_mcp_loader_source_has_removal_comment(self):
        """mcp_loader.py should have a comment about kg-server removal."""
        import agent.mcp_loader as mod
        source = Path(mod.__file__).read_text(encoding="utf-8")
        assert "kg-server removed" in source or "LightRAG" in source


# ============== 8. runner.py — kg-server prompt replaced ==============


class TestRunnerNoKgServerPrompt:
    """runner.py should not reference kg-server tools in prompts."""

    def test_no_kg_server_tool_in_prompt(self):
        """runner.py should not reference kg-server/explore_node."""
        import agent.runner as mod
        source = Path(mod.__file__).read_text(encoding="utf-8")
        assert "kg-server/explore_node" not in source
        assert "kg-server/get_related_entities" not in source

    def test_lightrag_query_in_prompt(self):
        """runner.py should reference lightrag_query instead."""
        import agent.runner as mod
        source = Path(mod.__file__).read_text(encoding="utf-8")
        assert "lightrag_query" in source


# ============== 9. photo-server — no niu_kg_server imports ==============


class TestPhotoServerNoKgServer:
    """photo-server should not import niu_kg_server."""

    def test_no_niu_kg_server_in_photo_server(self):
        """photo-server __init__.py should not import niu_kg_server."""
        source = Path("mcp-servers/photo-server/src/niu_photo_server/__init__.py").read_text(encoding="utf-8")
        assert "from niu_kg_server" not in source
        assert "import niu_kg_server" not in source

    def test_sync_to_kg_uses_lightrag(self):
        """sync_to_kg should use LightRAG ainsert."""
        source = Path("mcp-servers/photo-server/src/niu_photo_server/__init__.py").read_text(encoding="utf-8")
        # Find sync_to_kg function
        func_start = source.find("def sync_to_kg(")
        assert func_start > 0
        # Find next function definition
        next_func = source.find("\ndef ", func_start + 1)
        func_body = source[func_start:next_func] if next_func > 0 else source[func_start:]
        assert "lightrag_manager" in func_body or "lightrag_adapter" in func_body

    def test_sync_photo_to_kg_uses_lightrag(self):
        """sync_photo_to_kg should use LightRAG."""
        source = Path("mcp-servers/photo-server/src/niu_photo_server/__init__.py").read_text(encoding="utf-8")
        func_start = source.find("def sync_photo_to_kg(")
        assert func_start > 0
        next_func = source.find("\ndef ", func_start + 1)
        func_body = source[func_start:next_func] if next_func > 0 else source[func_start:]
        assert "lightrag" in func_body.lower()

    def test_name_person_uses_lightrag(self):
        """name_person KG sync should use LightRAG."""
        source = Path("mcp-servers/photo-server/src/niu_photo_server/__init__.py").read_text(encoding="utf-8")
        func_start = source.find("def name_person(")
        assert func_start > 0
        next_func = source.find("\ndef ", func_start + 1)
        func_body = source[func_start:next_func] if next_func > 0 else source[func_start:]
        assert "lightrag_adapter" in func_body or "LightRAGIngester" in func_body

    def test_merge_persons_uses_lightrag(self):
        """merge_persons KG sync should use LightRAG."""
        source = Path("mcp-servers/photo-server/src/niu_photo_server/__init__.py").read_text(encoding="utf-8")
        func_start = source.find("def merge_persons(")
        assert func_start > 0
        next_func = source.find("\ndef ", func_start + 1)
        func_body = source[func_start:next_func] if next_func > 0 else source[func_start:]
        assert "lightrag_adapter" in func_body or "LightRAGIngester" in func_body


# ============== 10. Config files — kg-server hidden ==============


class TestConfigFilesKgServerHidden:
    """Config files should have kg-server tools hidden or removed."""

    def test_mcp_servers_yaml_kg_server_not_preloaded(self):
        """kg-server should not be preloaded in mcp-servers.yaml."""
        import yaml
        config_path = Path("config/mcp-servers.yaml")
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)

        kg_config = config.get("kg-server", {})
        assert kg_config.get("preload") is not True

    def test_mcp_servers_yaml_kg_tools_hidden(self):
        """All kg-server tools should be hidden in mcp-servers.yaml."""
        import yaml
        config_path = Path("config/mcp-servers.yaml")
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)

        kg_tools = config.get("kg-server", {}).get("tools", {})
        for tool_name, tool_config in kg_tools.items():
            assert tool_config.get("visibility") == "hidden", f"kg-server/{tool_name} should be hidden"

    def test_mcp_tools_json_kg_tools_hidden(self):
        """All kg-server tools should be hidden in mcp_tools.json."""
        with open("data/mcp_tools.json", "r", encoding="utf-8") as f:
            data = json.load(f)

        for tool in data.get("kg-server", []):
            assert tool.get("visibility") == "hidden", f"kg-server/{tool.get('name')} should be hidden"

    def test_entity_extractor_no_kg_server_mcp(self):
        """entity-extractor should not list kg-server in mcpServers."""
        content = Path("config/agents/entity-extractor.md").read_text(encoding="utf-8")
        # Parse frontmatter
        if "---" in content:
            fm = content.split("---")[1]
            assert "kg-server" not in fm, f"kg-server should be removed from {name}'s mcpServers after LightRAG migration"

    def test_kg_enricher_no_kg_server_mcp(self):
        """kg-enricher should not list kg-server in mcpServers."""
        content = Path("config/agents/kg-enricher.md").read_text(encoding="utf-8")
        if "---" in content:
            fm = content.split("---")[1]
            assert "kg-server" not in fm, f"kg-server should be removed from {name}'s mcpServers after LightRAG migration"

    def test_dream_evolver_no_kg_server_mcp(self):
        """dream-evolver should not list kg-server in mcpServers."""
        content = Path("config/agents/dream-evolver.md").read_text(encoding="utf-8")
        if "---" in content:
            fm = content.split("---")[1]
            assert "kg-server" not in fm


# ============== 11. injector/__init__.py exports ==============


class TestInjectorExports:
    """injector/__init__.py should export LightRAGSync."""

    def test_lightrag_sync_exported(self):
        """LightRAGSync and get_lightrag_sync should be importable."""
        from agent.injector import LightRAGSync, get_lightrag_sync
        assert LightRAGSync is not None
        assert callable(get_lightrag_sync)

    def test_kg_sync_still_exported(self):
        """KGSync and get_kg_sync should still be importable (backward compat)."""
        from agent.injector import KGSync, get_kg_sync
        assert KGSync is not None
        assert callable(get_kg_sync)


# ============== 12. ingest_unified.py — lightrag_sync key ==============


class TestIngestUnified:
    """ingest_unified.py should use lightrag_sync key."""

    def test_no_kg_sync_key(self):
        """ingest_unified.py should not use 'kg_sync' key."""
        source = Path("scripts/ingest_unified.py").read_text(encoding="utf-8")
        assert '"kg_sync"' not in source
        assert "'kg_sync'" not in source

    def test_lightrag_sync_key(self):
        """ingest_unified.py should use 'lightrag_sync' key."""
        source = Path("scripts/ingest_unified.py").read_text(encoding="utf-8")
        assert "lightrag_sync" in source
