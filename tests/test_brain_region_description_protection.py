"""
Brain region description protection tests.

Tests call actual production functions — not inlined copies of protection logic.
If someone changes the protection conditions in production code, these tests
will fail (rather than silently passing).
"""
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Ensure lightrag package is importable from the bundled python/ directory
# ---------------------------------------------------------------------------
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_lightrag_site = os.path.join(_PROJECT_ROOT, "python", "lib", "python3.11", "site-packages")
if _lightrag_site not in sys.path:
    sys.path.insert(0, _lightrag_site)

_niu_api = _PROJECT_ROOT
if _niu_api not in sys.path:
    sys.path.insert(0, _niu_api)

from lightrag.utils_graph import _edit_entity_impl, _merge_entities_impl

from niu_api.internal.region_manager import RegionManager

# ===========================================================================
# Helpers — mock graph / vdb storages
# ===========================================================================

def _make_graph_mock(node_data_map: dict[str, dict], edges_map: dict | None = None):
    """Return an AsyncMock that behaves like chunk_entity_relation_graph.

    Parameters
    ----------
    node_data_map : {entity_name: node_data_dict}
    edges_map : optional {entity_name: [(src, tgt), ...]}
    """
    graph = AsyncMock()

    async def _has_node(name):
        return name in node_data_map

    async def _get_node(name):
        return node_data_map.get(name)

    async def _get_node_edges(name):
        if edges_map and name in edges_map:
            return edges_map[name]
        return []

    graph.has_node = AsyncMock(side_effect=_has_node)
    graph.get_node = AsyncMock(side_effect=_get_node)
    graph.get_node_edges = AsyncMock(side_effect=_get_node_edges)
    # no-op stubs
    graph.upsert_node = AsyncMock()
    graph.delete_node = AsyncMock()
    graph.get_edge = AsyncMock(return_value=None)
    graph.upsert_edge = AsyncMock()
    graph.index_done_callback = AsyncMock()
    return graph


def _make_vdb_mock():
    """Return an AsyncMock that behaves like entities_vdb / relationships_vdb."""
    vdb = AsyncMock()
    vdb.upsert = AsyncMock()
    vdb.delete = AsyncMock()
    vdb.index_done_callback = AsyncMock()
    # get_by_id is called by get_entity_info when include_vector_data=True
    vdb.get_by_id = AsyncMock(return_value=None)
    return vdb


# ===========================================================================
# TestExtractLabelFromContent — calls RegionManager._extract_label_from_content
# ===========================================================================

class TestExtractLabelFromContent:
    """Tests for RegionManager._extract_label_from_content (region_manager.py:1368)."""

    @pytest.fixture()
    def manager(self):
        """RegionManager with mock dependencies (only _extract_label_from_content is used)."""
        adapter = MagicMock()
        ingester = MagicMock()
        return RegionManager(adapter, ingester)

    def test_json_parse(self, manager):
        """JSON input: both label and description extracted correctly."""
        content = '{"label": "量子计算", "description": "量子比特与量子算法研究"}'
        label, description = manager._extract_label_from_content(content)
        assert label == "量子计算"
        assert description == "量子比特与量子算法研究"

    def test_regex_fallback(self, manager):
        """Non-JSON content containing label/description keys: regex fallback works."""
        content = 'Here is the result: {"label": "机器学习", "description": "ML模型与训练技术"}'
        label, description = manager._extract_label_from_content(content)
        assert label == "机器学习"
        assert description == "ML模型与训练技术"

    def test_empty_on_failure(self, manager):
        """Content with no label at all: returns empty tuple."""
        content = "这是一段没有任何标签的普通文本"
        label, description = manager._extract_label_from_content(content)
        assert label == ""
        assert description == ""

    def test_description_defaults_empty(self, manager):
        """JSON with label but no description: description defaults to empty string."""
        content = '{"label": "标签"}'
        label, description = manager._extract_label_from_content(content)
        assert label == "标签"
        assert description == ""

    def test_flexible_key_order(self, manager):
        """JSON with description before label: both extracted correctly."""
        content = '{"description": "量子算法研究", "id": 0, "label": "量子计算"}'
        label, description = manager._extract_label_from_content(content)
        assert label == "量子计算"
        assert description == "量子算法研究"


# ===========================================================================
# TestEditEntityBrainRegionProtection — calls _edit_entity_impl
# ===========================================================================

class TestEditEntityBrainRegionProtection:
    """Tests for _edit_entity_impl brain-region protection (utils_graph.py:309-320)."""

    @pytest.mark.asyncio
    async def test_brainregion_description_protected(self):
        """Editing a brainregion node: description must NOT be overwritten."""
        original_desc = "brain_meta_region_id:community_1<SEP>brain_meta_priority:permanent<SEP>文档库"
        node_data = {
            "entity_type": "brainregion",
            "description": original_desc,
            "source_id": "chunk-1",
        }
        graph = _make_graph_mock({"脑区实体": node_data})
        entities_vdb = _make_vdb_mock()
        relationships_vdb = _make_vdb_mock()

        await _edit_entity_impl(
            chunk_entity_relation_graph=graph,
            entities_vdb=entities_vdb,
            relationships_vdb=relationships_vdb,
            entity_name="脑区实体",
            updated_data={"description": "新的描述"},
        )

        # The graph's upsert_node should have been called with the original description preserved
        call_args = graph.upsert_node.call_args_list
        # Find the upsert for the main entity (not a rename — same name)
        upserted_data = None
        for call in call_args:
            name, data = call[0]
            if name == "脑区实体":
                upserted_data = data
                break
        assert upserted_data is not None, "upsert_node should have been called for 脑区实体"
        assert upserted_data["description"] == original_desc, (
            f"brainregion description must be preserved, got: {upserted_data['description']}"
        )

    @pytest.mark.asyncio
    async def test_brainregion_entity_type_protected(self):
        """Editing a brainregion node: entity_type must NOT be overwritten."""
        node_data = {
            "entity_type": "brainregion",
            "description": "brain_meta_region_id:community_1",
            "source_id": "chunk-1",
        }
        graph = _make_graph_mock({"脑区实体": node_data})
        entities_vdb = _make_vdb_mock()
        relationships_vdb = _make_vdb_mock()

        await _edit_entity_impl(
            chunk_entity_relation_graph=graph,
            entities_vdb=entities_vdb,
            relationships_vdb=relationships_vdb,
            entity_name="脑区实体",
            updated_data={"entity_type": "concept"},
        )

        upserted_data = None
        for call in graph.upsert_node.call_args_list:
            name, data = call[0]
            if name == "脑区实体":
                upserted_data = data
                break
        assert upserted_data is not None
        # entity_type is normalized to lowercase by production code,
        # but the protection logic restores "brainregion" before normalization.
        assert upserted_data["entity_type"] == "brainregion", (
            f"brainregion entity_type must be preserved, got: {upserted_data['entity_type']}"
        )

    @pytest.mark.asyncio
    async def test_normal_entity_description_updated(self):
        """Editing a normal (non-brainregion) entity: description should be updated."""
        node_data = {
            "entity_type": "person",
            "description": "旧描述",
            "source_id": "chunk-1",
        }
        graph = _make_graph_mock({"普通实体": node_data})
        entities_vdb = _make_vdb_mock()
        relationships_vdb = _make_vdb_mock()

        await _edit_entity_impl(
            chunk_entity_relation_graph=graph,
            entities_vdb=entities_vdb,
            relationships_vdb=relationships_vdb,
            entity_name="普通实体",
            updated_data={"description": "新描述"},
        )

        upserted_data = None
        for call in graph.upsert_node.call_args_list:
            name, data = call[0]
            if name == "普通实体":
                upserted_data = data
                break
        assert upserted_data is not None
        assert upserted_data["description"] == "新描述", (
            f"normal entity description should be updated, got: {upserted_data['description']}"
        )


# ===========================================================================
# TestMergeEntitiesBrainRegionProtection — calls _merge_entities_impl
# ===========================================================================

class TestMergeEntitiesBrainRegionProtection:
    """Tests for _merge_entities_impl brain-region protection (utils_graph.py:1291-1315)."""

    @pytest.mark.asyncio
    async def test_target_brainregion_preserved(self):
        """When target entity is a brainregion, its description must be preserved after merge."""
        brain_desc = "brain_meta_region_id:community_1<SEP>brain_meta_priority:permanent<SEP>文档库"
        target_data = {
            "entity_type": "brainregion",
            "description": brain_desc,
            "source_id": "chunk-target",
        }
        source_data = {
            "entity_type": "concept",
            "description": "源实体的描述",
            "source_id": "chunk-source",
        }

        node_data_map = {
            "目标脑区": target_data,
            "源实体A": source_data,
        }
        graph = _make_graph_mock(node_data_map)
        entities_vdb = _make_vdb_mock()
        relationships_vdb = _make_vdb_mock()

        await _merge_entities_impl(
            chunk_entity_relation_graph=graph,
            entities_vdb=entities_vdb,
            relationships_vdb=relationships_vdb,
            source_entities=["源实体A"],
            target_entity="目标脑区",
        )

        # Check upsert_node for target entity
        upserted_data = None
        for call in graph.upsert_node.call_args_list:
            name, data = call[0]
            if name == "目标脑区":
                upserted_data = data
                break
        assert upserted_data is not None
        assert upserted_data["description"] == brain_desc, (
            f"brainregion target description must be preserved, got: {upserted_data['description']}"
        )
        assert upserted_data["entity_type"] == "brainregion", (
            f"brainregion target entity_type must be preserved, got: {upserted_data['entity_type']}"
        )

    @pytest.mark.asyncio
    async def test_source_brainregion_preserved(self):
        """When a source entity is a brainregion, its description must be preserved after merge."""
        brain_desc = "brain_meta_region_id:community_2<SEP>brain_meta_priority:permanent<SEP>知识库"
        source_data = {
            "entity_type": "brainregion",
            "description": brain_desc,
            "source_id": "chunk-source",
        }
        target_data = {
            "entity_type": "concept",
            "description": "目标实体的描述",
            "source_id": "chunk-target",
        }

        node_data_map = {
            "源脑区": source_data,
            "目标实体": target_data,
        }
        graph = _make_graph_mock(node_data_map)
        entities_vdb = _make_vdb_mock()
        relationships_vdb = _make_vdb_mock()

        await _merge_entities_impl(
            chunk_entity_relation_graph=graph,
            entities_vdb=entities_vdb,
            relationships_vdb=relationships_vdb,
            source_entities=["源脑区"],
            target_entity="目标实体",
        )

        upserted_data = None
        for call in graph.upsert_node.call_args_list:
            name, data = call[0]
            if name == "目标实体":
                upserted_data = data
                break
        assert upserted_data is not None
        assert upserted_data["description"] == brain_desc, (
            f"source brainregion description must be preserved in merge, got: {upserted_data['description']}"
        )
        assert upserted_data["entity_type"] == "brainregion", (
            f"entity_type must be brainregion when source is brainregion, got: {upserted_data['entity_type']}"
        )

    @pytest.mark.asyncio
    async def test_normal_merge_unaffected(self):
        """Merging non-brainregion entities: description should use normal merge strategy."""
        source_data = {
            "entity_type": "concept",
            "description": "源描述",
            "source_id": "chunk-source",
        }
        target_data = {
            "entity_type": "person",
            "description": "目标描述",
            "source_id": "chunk-target",
        }

        node_data_map = {
            "源实体A": source_data,
            "目标实体": target_data,
        }
        graph = _make_graph_mock(node_data_map)
        entities_vdb = _make_vdb_mock()
        relationships_vdb = _make_vdb_mock()

        await _merge_entities_impl(
            chunk_entity_relation_graph=graph,
            entities_vdb=entities_vdb,
            relationships_vdb=relationships_vdb,
            source_entities=["源实体A"],
            target_entity="目标实体",
        )

        upserted_data = None
        for call in graph.upsert_node.call_args_list:
            name, data = call[0]
            if name == "目标实体":
                upserted_data = data
                break
        assert upserted_data is not None
        # Normal merge uses "concatenate" strategy for description, so it should NOT be
        # the original target description alone.
        assert upserted_data["description"] != "目标描述", (
            "normal merge should concatenate descriptions, not preserve single value"
        )
        # The entity_type should be normalized (keep_first → "concept" from first source)
        assert upserted_data["entity_type"] == "concept"


# ===========================================================================
# TestRebuildSingleEntityBrainRegionProtection — calls _rebuild_single_entity
# ===========================================================================

class TestRebuildSingleEntityBrainRegionProtection:
    """Tests for _rebuild_single_entity brain-region protection (operate.py:1101)."""

    @pytest.fixture(autouse=True)
    def _import_target(self):
        """Import _rebuild_single_entity (sys.path already set at module level)."""
        from lightrag.operate import _rebuild_single_entity
        self._rebuild_single_entity = _rebuild_single_entity

    # -- helpers ---------------------------------------------------------------

    def _make_graph_mock(self, node_data: dict, edges_map: dict | None = None, edge_data_map: dict | None = None):
        """Return an AsyncMock graph storage.

        Parameters
        ----------
        node_data : single node dict returned by get_node
        edges_map : {entity_name: [(src, tgt), ...]}
        edge_data_map : {(src, tgt): edge_dict}
        """
        graph = AsyncMock()

        async def _get_node(_name):
            return node_data

        async def _get_node_edges(name):
            if edges_map and name in edges_map:
                return edges_map[name]
            return []

        async def _get_edge(src, tgt):
            if edge_data_map:
                return edge_data_map.get((src, tgt))
            return None

        graph.get_node = AsyncMock(side_effect=_get_node)
        graph.get_node_edges = AsyncMock(side_effect=_get_node_edges)
        graph.get_edge = AsyncMock(side_effect=_get_edge)
        graph.upsert_node = AsyncMock()
        graph.index_done_callback = AsyncMock()
        return graph

    def _make_vdb_mock(self):
        vdb = AsyncMock()
        vdb.upsert = AsyncMock()
        vdb.index_done_callback = AsyncMock()
        return vdb

    def _make_llm_cache_mock(self):
        cache = AsyncMock()
        cache.get_by_id = AsyncMock(return_value=None)
        return cache

    def _make_chunks_storage_mock(self):
        storage = AsyncMock()
        storage.upsert = AsyncMock()
        return storage

    def _global_config(self):
        return {
            "max_source_ids_per_entity": 10,
            "source_ids_limit_method": "keep",
            "max_file_paths": 10,
            "file_path_more_placeholder": "...more...",
        }

    def _find_upserted_data(self, graph, entity_name):
        """Extract the data dict passed to upsert_node for *entity_name*."""
        for call in graph.upsert_node.call_args_list:
            name, data = call[0]
            if name == entity_name:
                return data
        return None

    # -- early-return path (chunk_entities empty) ------------------------------

    @pytest.mark.asyncio
    async def test_brainregion_entity_type_preserved_early_return(self):
        """brainregion node in early-return path: entity_type stays 'brainregion'."""
        original_desc = "brain_meta_region_id:community_1<SEP>brain_meta_priority:permanent<SEP>文档库"
        node_data = {
            "entity_type": "brainregion",
            "description": original_desc,
            "source_id": "chunk-1",
            "file_path": "file1",
        }
        edge_data_map = {
            ("脑区实体", "other"): {"description": "edge desc", "file_path": "file1"},
        }
        graph = self._make_graph_mock(
            node_data,
            edges_map={"脑区实体": [("脑区实体", "other")]},
            edge_data_map=edge_data_map,
        )
        entities_vdb = self._make_vdb_mock()
        llm_cache = self._make_llm_cache_mock()
        chunks_storage = self._make_chunks_storage_mock()

        await self._rebuild_single_entity(
            knowledge_graph_inst=graph,
            entities_vdb=entities_vdb,
            entity_name="脑区实体",
            chunk_ids=["chunk-1"],
            chunk_entities={},  # empty → early return path
            llm_response_cache=llm_cache,
            global_config=self._global_config(),
            entity_chunks_storage=chunks_storage,
        )

        upserted = self._find_upserted_data(graph, "脑区实体")
        assert upserted is not None, "upsert_node should have been called for 脑区实体"
        assert upserted["entity_type"] == "brainregion", (
            f"brainregion entity_type must be preserved in early-return, got: {upserted['entity_type']}"
        )

    @pytest.mark.asyncio
    async def test_brainregion_description_preserved_early_return(self):
        """brainregion node in early-return path: description stays original."""
        original_desc = "brain_meta_region_id:community_1<SEP>brain_meta_priority:permanent<SEP>文档库"
        node_data = {
            "entity_type": "brainregion",
            "description": original_desc,
            "source_id": "chunk-1",
            "file_path": "file1",
        }
        edge_data_map = {
            ("脑区实体", "other"): {"description": "edge desc", "file_path": "file1"},
        }
        graph = self._make_graph_mock(
            node_data,
            edges_map={"脑区实体": [("脑区实体", "other")]},
            edge_data_map=edge_data_map,
        )
        entities_vdb = self._make_vdb_mock()
        llm_cache = self._make_llm_cache_mock()
        chunks_storage = self._make_chunks_storage_mock()

        await self._rebuild_single_entity(
            knowledge_graph_inst=graph,
            entities_vdb=entities_vdb,
            entity_name="脑区实体",
            chunk_ids=["chunk-1"],
            chunk_entities={},  # empty → early return path
            llm_response_cache=llm_cache,
            global_config=self._global_config(),
            entity_chunks_storage=chunks_storage,
        )

        upserted = self._find_upserted_data(graph, "脑区实体")
        assert upserted is not None
        assert upserted["description"] == original_desc, (
            f"brainregion description must be preserved in early-return, got: {upserted['description']}"
        )

    # -- full-rebuild path (chunk_entities non-empty) --------------------------

    @pytest.mark.asyncio
    async def test_brainregion_entity_type_preserved_full_rebuild(self):
        """brainregion node in full-rebuild path: entity_type stays 'brainregion' even when chunk data says 'concept'."""
        original_desc = "brain_meta_region_id:community_2<SEP>brain_meta_priority:permanent<SEP>知识库"
        node_data = {
            "entity_type": "brainregion",
            "description": original_desc,
            "source_id": "chunk-1",
            "file_path": "file1",
        }
        graph = self._make_graph_mock(node_data)
        entities_vdb = self._make_vdb_mock()
        llm_cache = self._make_llm_cache_mock()
        chunks_storage = self._make_chunks_storage_mock()

        chunk_entities = {
            "chunk-1": {
                "脑区实体": [
                    {"description": "从chunk提取的描述", "entity_type": "concept", "file_path": "file1"},
                ]
            }
        }

        await self._rebuild_single_entity(
            knowledge_graph_inst=graph,
            entities_vdb=entities_vdb,
            entity_name="脑区实体",
            chunk_ids=["chunk-1"],
            chunk_entities=chunk_entities,
            llm_response_cache=llm_cache,
            global_config=self._global_config(),
            entity_chunks_storage=chunks_storage,
        )

        upserted = self._find_upserted_data(graph, "脑区实体")
        assert upserted is not None
        assert upserted["entity_type"] == "brainregion", (
            f"brainregion entity_type must override voting result, got: {upserted['entity_type']}"
        )

    @pytest.mark.asyncio
    async def test_brainregion_description_preserved_full_rebuild(self):
        """brainregion node in full-rebuild path: description stays original (LLM summary skipped)."""
        original_desc = "brain_meta_region_id:community_2<SEP>brain_meta_priority:permanent<SEP>知识库"
        node_data = {
            "entity_type": "brainregion",
            "description": original_desc,
            "source_id": "chunk-1",
            "file_path": "file1",
        }
        graph = self._make_graph_mock(node_data)
        entities_vdb = self._make_vdb_mock()
        llm_cache = self._make_llm_cache_mock()
        chunks_storage = self._make_chunks_storage_mock()

        chunk_entities = {
            "chunk-1": {
                "脑区实体": [
                    {"description": "从chunk提取的描述", "entity_type": "concept", "file_path": "file1"},
                ]
            }
        }

        await self._rebuild_single_entity(
            knowledge_graph_inst=graph,
            entities_vdb=entities_vdb,
            entity_name="脑区实体",
            chunk_ids=["chunk-1"],
            chunk_entities=chunk_entities,
            llm_response_cache=llm_cache,
            global_config=self._global_config(),
            entity_chunks_storage=chunks_storage,
        )

        upserted = self._find_upserted_data(graph, "脑区实体")
        assert upserted is not None
        assert upserted["description"] == original_desc, (
            f"brainregion description must be preserved in full-rebuild, got: {upserted['description']}"
        )

    @pytest.mark.asyncio
    async def test_normal_entity_rebuild_unaffected(self):
        """Normal (non-brainregion) entity in full-rebuild: description and entity_type update normally."""
        node_data = {
            "entity_type": "person",
            "description": "旧描述",
            "source_id": "chunk-0",
            "file_path": "file0",
        }
        graph = self._make_graph_mock(node_data)
        entities_vdb = self._make_vdb_mock()
        llm_cache = self._make_llm_cache_mock()
        chunks_storage = self._make_chunks_storage_mock()

        chunk_entities = {
            "chunk-1": {
                "普通实体": [
                    {"description": "新描述A", "entity_type": "person", "file_path": "file1"},
                    {"description": "新描述B", "entity_type": "person", "file_path": "file2"},
                ]
            }
        }

        # For normal entities _handle_entity_relation_summary is called.
        # Patch it to avoid real LLM invocation.
        from unittest.mock import patch
        with patch(
            "lightrag.operate._handle_entity_relation_summary",
            new_callable=AsyncMock,
            return_value=("LLM摘要结果", True),
        ):
            await self._rebuild_single_entity(
                knowledge_graph_inst=graph,
                entities_vdb=entities_vdb,
                entity_name="普通实体",
                chunk_ids=["chunk-1"],
                chunk_entities=chunk_entities,
                llm_response_cache=llm_cache,
                global_config=self._global_config(),
                entity_chunks_storage=chunks_storage,
            )

        upserted = self._find_upserted_data(graph, "普通实体")
        assert upserted is not None
        # entity_type should be "person" (from majority voting of chunk data)
        assert upserted["entity_type"] == "person", (
            f"normal entity_type should be from chunk data, got: {upserted['entity_type']}"
        )
        # description should be the LLM summary result (not the original)
        assert upserted["description"] == "LLM摘要结果", (
            f"normal entity description should be updated via LLM, got: {upserted['description']}"
        )
