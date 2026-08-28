"""
Phase 02 Integration Tests — KuzuDB → LightRAG Migration

Validates that all KuzuDB/kg-server references have been replaced
with LightRAG adapter/pipeline calls across the codebase.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

# ============== 1. KGSync → LightRAGSync delegation ==============


class TestKGSyncDelegation:
    """KGSync should delegate to LightRAGSync, not KuzuDB."""

    def test_kg_sync_run_delegates_to_lightrag_sync(self):
        """KGSync.run_full_sync() should call LightRAGSync.run_sync()."""
        from agent.injector.kg_sync import KGSync

        ks = KGSync()
        with patch("agent.injector.lightrag_sync.LightRAGSync") as mock_sync:
            mock_instance = MagicMock()
            mock_instance.run_sync.return_value = {"photos_synced": 5}
            mock_sync.return_value = mock_instance

            result = ks.run_full_sync()
            mock_sync.assert_called_once()
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
        from niu_api.kg_api import ExploreRequest, explore_node

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


# ============== 4. notes_api.py → LightRAGIngester ==============


class TestNotesApiUsesLightRAG:
    """notes_api sync_note_to_lightrag should use lightrag for persistence."""

    def test_no_niu_kg_server_import_in_notes_api(self):
        """notes_api.py should NOT import niu_kg_server."""
        import niu_api.notes_api as mod
        source = Path(mod.__file__).read_text(encoding="utf-8")
        assert "niu_kg_server" not in source

    def test_notes_json_storage_no_sqlite(self):
        """notes.py should NOT use aiosqlite."""
        import niu_api.notes as mod
        source = Path(mod.__file__).read_text(encoding="utf-8")
        assert "aiosqlite" not in source
        assert "sqlite" not in source.lower()
        assert "notes.db" not in source


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

    def test_lightrag_sync_background_start_stop(self):
        """LightRAGSync should start and stop background thread."""
        from agent.injector.lightrag_sync import LightRAGSync

        sync = LightRAGSync(sync_interval=999999)  # Long interval so it doesn't run
        # _sync_loop 会先等 LightRAG 就绪（最坏 30s）——mock 即时返回避免拖慢测试，
        # 同时 mock get_lightrag/run_sync 防止后台线程触碰真实存储（本测试只验证线程生命周期）。
        with (
            patch("agent.injector.lightrag_sync.wait_lightrag_ready", return_value=False),
            patch("niu_api.internal.lightrag_manager.get_lightrag", return_value=None),
            patch.object(sync, "run_sync", return_value={"skills_synced": 0, "tools_synced": 0, "errors": []}),
        ):
            sync.start_background_sync()
            assert sync._thread is not None
            assert sync._thread.is_alive()

            sync.stop_background_sync()
            # stop 只 join 5s；就绪等待已 mock 即时返回，join 应很快完成
            sync._thread.join(timeout=10)
            assert not sync._thread.is_alive()

    def test_get_lightrag_sync_singleton(self):
        """get_lightrag_sync should return singleton instance."""
        import agent.injector.lightrag_sync as mod
        from agent.injector.lightrag_sync import get_lightrag_sync

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
        """runner.py should reference lightrag via disk (disk mode)."""
        import agent.runner as mod
        source = Path(mod.__file__).read_text(encoding="utf-8")
        # In disk mode, lightrag tools are accessed via disk(), not directly
        assert "disk_engine" in source


# ============== 9. photo-server — no niu_kg_server imports ==============


class TestPhotoServerNoKgServer:
    """photo-server should not import niu_kg_server."""

    def test_no_niu_kg_server_in_photo_server(self):
        """photo-server __init__.py should not import niu_kg_server."""
        source = Path("mcp-servers/photo-server/src/niu_photo_server/__init__.py").read_text(encoding="utf-8")
        assert "from niu_kg_server" not in source
        assert "import niu_kg_server" not in source

    def test_sync_photo_to_kg_uses_lightrag(self):
        """sync_photo_to_kg should use LightRAG."""
        source = Path("mcp-servers/photo-server/src/niu_photo_server/__init__.py").read_text(encoding="utf-8")
        func_start = source.find("def sync_photo_to_kg(")
        assert func_start > 0
        next_func = source.find("\ndef ", func_start + 1)
        func_body = source[func_start:next_func] if next_func > 0 else source[func_start:]
        assert "lightrag" in func_body.lower()

    def test_name_person_uses_lightrag(self):
        """name_person KG sync should use LightRAG via ToolRegistry."""
        source = Path("mcp-servers/photo-server/src/niu_photo_server/__init__.py").read_text(encoding="utf-8")
        func_start = source.find("def name_person(")
        assert func_start > 0
        next_func = source.find("\ndef ", func_start + 1)
        func_body = source[func_start:next_func] if next_func > 0 else source[func_start:]
        assert "lightrag" in func_body.lower() or "tool_registry" in func_body.lower()

    def test_merge_persons_uses_lightrag(self):
        """merge_persons KG sync should use LightRAG via ToolRegistry."""
        source = Path("mcp-servers/photo-server/src/niu_photo_server/__init__.py").read_text(encoding="utf-8")
        func_start = source.find("def merge_persons(")
        assert func_start > 0
        next_func = source.find("\ndef ", func_start + 1)
        func_body = source[func_start:next_func] if next_func > 0 else source[func_start:]
        assert "lightrag" in func_body.lower() or "tool_registry" in func_body.lower()


# ============== 10. Config files — kg-server hidden ==============


class TestConfigFilesKgServerHidden:
    """Config files should have kg-server tools hidden or removed."""

    def test_mcp_servers_yaml_kg_server_not_preloaded(self):
        """kg-server should not be in mcp-servers.yaml at all (deleted)."""
        import yaml
        config_path = Path("config/mcp-servers.yaml")
        with open(config_path, encoding="utf-8") as f:
            config = yaml.safe_load(f)

        # Handle empty YAML (None)
        if config is None:
            return  # Empty config means kg-server is definitely not there

        # kg-server has been entirely removed from config
        assert "kg-server" not in config, "kg-server should be removed from mcp-servers.yaml"

    def test_mcp_servers_yaml_kg_tools_hidden(self):
        """kg-server entry should not exist in mcp-servers.yaml (deleted)."""
        import yaml
        config_path = Path("config/mcp-servers.yaml")
        with open(config_path, encoding="utf-8") as f:
            config = yaml.safe_load(f)

        # Handle empty YAML (None)
        if config is None:
            return  # Empty config means kg-server is definitely not there

        # kg-server has been entirely removed - no tools section to check
        assert "kg-server" not in config, "kg-server should be removed from mcp-servers.yaml"

    def test_entity_extractor_no_kg_server_mcp(self):
        """entity-extractor should not list kg-server in mcpServers."""
        content = Path("config/agents/entity-extractor.md").read_text(encoding="utf-8")
        # Parse frontmatter
        if "---" in content:
            fm = content.split("---")[1]
            assert "kg-server" not in fm, "kg-server should be removed from entity-extractor's mcpServers after LightRAG migration"

    def test_kg_enricher_no_kg_server_mcp(self):
        """kg-enricher should not exist or should not list kg-server in mcpServers."""
        kg_enricher_path = Path("config/agents/kg-enricher.md")
        if not kg_enricher_path.exists():
            # kg-enricher agent was removed along with kg-server - test passes
            return
        content = kg_enricher_path.read_text(encoding="utf-8")
        if "---" in content:
            fm = content.split("---")[1]
            assert "kg-server" not in fm, "kg-server should be removed from kg-enricher's mcpServers after LightRAG migration"

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
