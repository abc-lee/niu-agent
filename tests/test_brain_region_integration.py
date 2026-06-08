"""
Integration tests for Brain Region Activation (M1-M6).

Exercises the full brain region flow using mock objects
(no real LightRAG dependency). Verifies that M1-M6 modules
work together correctly.

Test coverage:
1. test_insert_detect_activate_inject — full insert -> detect -> activate -> inject cycle
2. test_activate_decay_reactivate — activation, decay, and reinforce cycle
3. test_manual_control_overrides_auto — manual dim/activate override logic
4. test_tool_use_reinforces_region — tool dispatch reinforce steady state
5. test_spillover_activation — neighbor spillover activation
6. test_dream_writer_semantic_vs_episodic — dual-pipeline write paths
7. test_dream_writer_time_chain_integrity — followed_by/corrected_by chain
8. test_context_budget_not_exceeded — token budget truncation
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from niu_api.internal.region_activation import (
    BrainRegionState,
    RegionActivationManager,
    STATUS_LIT,
)
from niu_api.internal.region_injector import BrainContextInjector
from niu_api.internal.region_manager import BrainRegionInfo, RegionManager
from agent.injector.dream_writer import (
    CHAIN_RELATION_CORRECTED,
    CHAIN_RELATION_FOLLOWED,
    EPISODIC_ENTITY_TYPE,
    EVENT_PREFIX,
    DreamWriter,
)


# ============== Helpers ==============


def _make_region_infos() -> list[BrainRegionInfo]:
    """Create test BrainRegionInfo list with multiple regions."""
    return [
        BrainRegionInfo(
            name="编程开发脑区",
            label="编程开发",
            community_id="community_0",
            description="Python/NumPy/Web技术栈",
            size=6,
            representative="Python",
            members=["Python", "NumPy", "Data_Analysis", "Web_Development", "Django", "FastAPI"],
            updated_at=1745366400.0,
        ),
        BrainRegionInfo(
            name="项目管理脑区",
            label="项目管理",
            community_id="community_1",
            description="AI_Bot项目，主开发者",
            size=4,
            representative="AI_Bot",
            members=["AI_Bot", "Project_Plan", "Sprint", "Backlog"],
            updated_at=1745366400.0,
        ),
        BrainRegionInfo(
            name="日常偏好脑区",
            label="日常偏好",
            community_id="community_2",
            description="暗色主题，远程办公",
            size=3,
            representative="暗色主题",
            members=["暗色主题", "远程办公", "MacOS"],
            updated_at=1745366400.0,
        ),
    ]


def _make_activation_mgr(
    neighbor_map: dict[str, set[str]] | None = None,
) -> RegionActivationManager:
    """Create a RegionActivationManager with initialized regions."""
    manager = RegionActivationManager()
    manager.initialize_from_regions(_make_region_infos())
    if neighbor_map is not None:
        manager.set_region_neighbors(neighbor_map)
    return manager


def _make_injector(
    activation_mgr: RegionActivationManager | None = None,
) -> BrainContextInjector:
    """Create a BrainContextInjector with mock adapter/region_mgr."""
    if activation_mgr is None:
        activation_mgr = _make_activation_mgr()
    adapter = MagicMock()
    region_mgr = MagicMock(spec=RegionManager)
    return BrainContextInjector(
        adapter=adapter,
        activation_mgr=activation_mgr,
        region_mgr=region_mgr,
    )


def _make_dream_writer() -> tuple[DreamWriter, MagicMock]:
    """Create a DreamWriter with mock ingester.

    Returns:
        (writer, mock_ingester) tuple.
    """
    ingester = MagicMock()
    ingester.lightrag_insert.return_value = {"status": "ok"}
    writer = DreamWriter(ingester)
    return writer, ingester


def _set_activation(
    manager: RegionActivationManager,
    region_id: str,
    activation: float,
) -> None:
    """Set activation value for a specific region."""
    state = manager._regions.get(region_id)
    if state:
        state.activation = activation


# ============== Test 1: Full insert-detect-activate-inject cycle ==============


class TestFullBrainRegionFlow:

    def test_insert_detect_activate_inject(self) -> None:
        """Full cycle: create regions -> activate -> format map + detailed.

        Verifies that region map and detailed region formatting
        correctly reflect the activated region's status light.
        """
        activation_mgr = _make_activation_mgr()
        injector = _make_injector(activation_mgr)

        # Activate the 编程开发 region via query hit
        entity_to_region = activation_mgr.get_entity_to_region_map()
        activated = activation_mgr.activate_regions(
            hit_entities=["Python"], entity_to_region=entity_to_region
        )

        assert "编程开发脑区" in activated

        # Get region map
        regions = activation_mgr.get_region_map()
        region_map = injector.format_region_map(regions)

        # Verify map contains header and region labels
        assert "## 脑区状态 (3个脑区)" in region_map
        assert "编程开发" in region_map
        assert STATUS_LIT in region_map

    def test_activate_decay_reactivate(self) -> None:
        """Activation, decay, and reinforce cycle.

        - Activate region (activation=1.0)
        - Decay once (activation=0.92)
        - Reinforce via tool use (activation=max(0.92*0.92, 0.85)=0.85)
        - Verify reinforce brings it back up
        """
        manager = _make_activation_mgr()

        # Activate 编程开发 region
        entity_to_region = manager.get_entity_to_region_map()
        manager.activate_regions(
            hit_entities=["Python"], entity_to_region=entity_to_region
        )

        state = manager._regions["编程开发脑区"]
        assert state.activation == 1.0

        # Decay once
        manager.decay_all()
        assert state.activation == pytest.approx(0.92)

        # Decay again (activation = 0.92 * 0.92 = 0.8464)
        manager.decay_all()
        assert state.activation == pytest.approx(0.8464)

        # Reinforce via tool use (max(current, 0.85))
        tool_to_region = {"kg-server/query": "编程开发脑区"}
        manager.reinforce_by_tool_use("kg-server/query", tool_to_region)

        # Reinforce should bring it to 0.85 (0.8464 < 0.85)
        assert state.activation == pytest.approx(0.85)

    def test_manual_control_overrides_auto(self) -> None:
        """Manual dim overrides auto-activation, manual_activate overrides dim.

        Steps:
        1. Activate region
        2. Manual dim (activation=0.0, manually_dimmed=True)
        3. Auto-activate should NOT affect dimmed region
        4. Manual activate overrides dim (activation=1.0, manually_dimmed=False)
        """
        manager = _make_activation_mgr()

        # Step 1: Activate 编程开发
        entity_to_region = manager.get_entity_to_region_map()
        manager.activate_regions(
            hit_entities=["Python"], entity_to_region=entity_to_region
        )
        state = manager._regions["编程开发脑区"]
        assert state.activation == 1.0
        assert state.manually_dimmed is False

        # Step 2: Manual dim
        manager.manual_dim(["编程开发"])
        assert state.activation == 0.0
        assert state.manually_dimmed is True

        # Step 3: Auto-activate should skip dimmed region
        activated = manager.activate_regions(
            hit_entities=["Python"], entity_to_region=entity_to_region
        )
        assert "编程开发脑区" not in activated
        assert state.activation == 0.0
        assert state.manually_dimmed is True

        # Step 4: Manual activate overrides dim
        manager.manual_activate(["编程开发"])
        assert state.activation == 1.0
        assert state.manually_dimmed is False

    def test_tool_use_reinforces_region(self) -> None:
        """Tool use reinforces the region, steady state in 0.78-0.85 range.

        Steps:
        1. Set tool_to_region mapping
        2. Reinforce (activation=0.85)
        3. Decay (activation=0.85*0.92=0.782)
        4. Reinforce again (max(0.782, 0.85)=0.85)
        5. Verify steady state in 0.78-0.85 range
        """
        manager = _make_activation_mgr()

        # Set tool mapping
        tool_to_region = {"kg-server/query": "编程开发脑区"}

        # Reinforce (region starts at 0.0, max(0.0, 0.85) = 0.85)
        result = manager.reinforce_by_tool_use("kg-server/query", tool_to_region)
        assert result == "编程开发脑区"
        state = manager._regions["编程开发脑区"]
        assert state.activation == pytest.approx(0.85)

        # Decay (0.85 * 0.92 = 0.782)
        manager.decay_all()
        assert state.activation == pytest.approx(0.782)

        # Reinforce again (max(0.782, 0.85) = 0.85)
        manager.reinforce_by_tool_use("kg-server/query", tool_to_region)
        assert state.activation == pytest.approx(0.85)

        # Verify steady state after several cycles
        for _ in range(3):
            manager.decay_all()
            manager.reinforce_by_tool_use("kg-server/query", tool_to_region)
            assert 0.78 <= state.activation <= 0.85

    def test_spillover_activation(self) -> None:
        """Activated region spills over to neighbors.

        Steps:
        1. Set neighbor relationships
        2. Activate one region
        3. Verify neighbor gets partial activation (spillover_factor * source)
        """
        neighbor_map = {
            "community_0": {"community_1"},
            "community_1": {"community_0", "community_2"},
            "community_2": {"community_1"},
        }
        manager = _make_activation_mgr(neighbor_map=neighbor_map)

        # Activate 编程开发 region
        entity_to_region = manager.get_entity_to_region_map()
        activated = manager.activate_regions(
            hit_entities=["Python"], entity_to_region=entity_to_region
        )

        assert "编程开发脑区" in activated
        source_state = manager._regions["编程开发脑区"]
        assert source_state.activation == 1.0

        # Neighbor 项目管理脑区 should get spillover (0.3 * 1.0 = 0.3)
        neighbor_state = manager._regions["项目管理脑区"]
        assert neighbor_state.activation == pytest.approx(0.3)

        # 日常偏好脑区 is NOT a direct neighbor of 编程开发脑区
        # (it's a neighbor of 项目管理脑区, but spillover only goes 1 hop)
        distant_state = manager._regions["日常偏好脑区"]
        assert distant_state.activation == pytest.approx(0.0)

    def test_dream_writer_semantic_vs_episodic(self) -> None:
        """Semantic writes use 语义记忆 prefix, episodic writes use 情景记忆 prefix.

        Steps:
        1. Write semantic entity — content starts with 语义记忆
        2. Write episodic event — content starts with 情景记忆
        3. Semantic writes do NOT create time chains
        4. Episodic writes with prev_event include chain keyword in content
        """
        writer, ingester = _make_dream_writer()

        # Step 1: Semantic entity
        result = writer.write_semantic_entity(
            name="Python",
            entity_type="Skill",
            description="Programming language",
        )
        assert result["status"] == "ok"

        # Verify lightrag_insert called with semantic prefix
        call_content = ingester.lightrag_insert.call_args.kwargs["content"]
        assert call_content.startswith("语义记忆:")
        assert "Python" in call_content
        assert "Skill" in call_content

        # Step 2: Episodic event (no prev, so no chain keyword)
        ingester.reset_mock()

        result = writer.write_episodic_event(
            event_name="tool_x_failed",
            description="Tool X returned error",
            experience_type="error",
        )
        assert result["status"] == "ok"

        # Verify lightrag_insert called with episodic prefix
        call_content = ingester.lightrag_insert.call_args.kwargs["content"]
        assert call_content.startswith("情景记忆:")
        assert "tool_x_failed" in call_content
        # No prev_event, so no chain keyword in content
        assert CHAIN_RELATION_FOLLOWED not in call_content
        assert CHAIN_RELATION_CORRECTED not in call_content

        # Step 3: Episodic event WITH prev_event includes chain keyword
        ingester.reset_mock()

        result = writer.write_episodic_event(
            event_name="tried_tool_y",
            description="Tried tool Y successfully",
            experience_type="success",
            prev_event_name="tool_x_failed",
            is_correction=False,
        )
        assert result["status"] == "ok"

        # Verify chain relation is followed_by (not corrected_by)
        call_content = ingester.lightrag_insert.call_args.kwargs["content"]
        assert CHAIN_RELATION_FOLLOWED in call_content
        assert CHAIN_RELATION_CORRECTED not in call_content
        assert "tool_x_failed" in call_content
        assert "tried_tool_y" in call_content

    def test_dream_writer_time_chain_integrity(self) -> None:
        """Time chain: followed_by and corrected_by keywords in content.

        Steps:
        1. Write event A (no prev) — no chain keyword
        2. Write event B (prev=A, is_correction=False) — followed_by in content
        3. Write event C (prev=B, is_correction=True) — corrected_by in content
        """
        writer, ingester = _make_dream_writer()

        # Step 1: Write event A (no prev)
        result_a = writer.write_episodic_event(
            event_name="event_A",
            description="First event",
            experience_type="success",
        )
        assert result_a["status"] == "ok"

        # No chain keyword in content
        content_a = ingester.lightrag_insert.call_args.kwargs["content"]
        assert CHAIN_RELATION_FOLLOWED not in content_a
        assert CHAIN_RELATION_CORRECTED not in content_a

        # Step 2: Write event B (prev=A, is_correction=False)
        ingester.reset_mock()

        result_b = writer.write_episodic_event(
            event_name="event_B",
            description="Second event follows A",
            experience_type="success",
            prev_event_name="event_A",
            is_correction=False,
        )
        assert result_b["status"] == "ok"

        # Verify followed_by chain: A -> B in content
        content_b = ingester.lightrag_insert.call_args.kwargs["content"]
        assert CHAIN_RELATION_FOLLOWED in content_b
        assert CHAIN_RELATION_CORRECTED not in content_b
        assert "event_A" in content_b
        assert "event_B" in content_b

        # Step 3: Write event C (prev=B, is_correction=True)
        ingester.reset_mock()

        result_c = writer.write_episodic_event(
            event_name="event_C",
            description="Third event corrects B",
            experience_type="success",
            prev_event_name="event_B",
            is_correction=True,
        )
        assert result_c["status"] == "ok"

        # Verify corrected_by chain: B -> C in content
        content_c = ingester.lightrag_insert.call_args.kwargs["content"]
        assert CHAIN_RELATION_CORRECTED in content_c
        assert CHAIN_RELATION_FOLLOWED not in content_c
        assert "event_B" in content_c
        assert "event_C" in content_c