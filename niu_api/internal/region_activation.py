"""
Brain Region Activation Manager

Session-level activation/decay management for brain regions (Leiden communities).
Each region has an activation score 0.0-1.0 that:
- Activates (1.0) when query hits entities in that region
- Reinforces (max(current, 0.85)) when a tool within that region is used
- Decays (*0.92) each conversation turn
- Spills over to neighboring regions at 0.3x factor
- Can be manually activated or dimmed

This is PURE IN-MEMORY state — no LightRAG calls, no persistence.

M3 module: Region activation lifecycle, M1/M2 provide detection and node management.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from niu_api.internal.region_manager import BrainRegionInfo

logger = logging.getLogger(__name__)

# ============== Constants ==============

# Status light symbols
STATUS_LIT = "🟢"
STATUS_DIMMING = "🟡"
STATUS_OFF = "⚫"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class BrainRegionState:
    """Brain region activation state (session-level, NOT persisted to graph)

    Tracks per-region activation score with decay, reinforce, and spillover
    mechanisms. Reset on each new session.
    """

    region_id: str  # community_id (e.g. "community_3")
    label: str  # human-readable region name (e.g. "Python")
    activation: float  # current activation 0.0-1.0
    last_activated_at: float  # timestamp of last activation
    activation_count: int  # number of times activated this session
    manually_dimmed: bool  # manually dimmed this turn (won't be auto-activated)


# ---------------------------------------------------------------------------
# Region Activation Manager
# ---------------------------------------------------------------------------


class RegionActivationManager:
    """Session-level brain region activation/decay management

    Core mechanisms:
    - Activate: query hits region entity -> activation=1.0
    - Reinforce: tool used within region -> activation=max(current, 0.85)
    - Decay: each turn activation *= 0.92
    - Manual control: activate/dim, dimmed regions skip auto-activation this turn
    - Spillover: activated region's neighbors get 0.3 * activation

    Usage::

        manager = RegionActivationManager()
        manager.initialize_from_regions(region_infos)
        manager.set_region_neighbors(neighbor_map)

        # On query hit
        activated = manager.activate_regions(hit_entities, entity_to_region)

        # On tool use
        manager.reinforce_by_tool_use(tool_name, tool_to_region)

        # End of turn
        manager.decay_all()
    """

    def __init__(
        self,
        decay_factor: float = 0.92,
        activation_threshold: float = 0.3,
        spillover_factor: float = 0.3,
        tool_reinforce_value: float = 0.85,
    ) -> None:
        assert 0.0 < decay_factor <= 1.0, "decay_factor must be in (0, 1]"
        assert 0.0 <= activation_threshold <= 1.0, "activation_threshold must be in [0, 1]"
        assert 0.0 <= spillover_factor <= 1.0, "spillover_factor must be in [0, 1]"
        assert 0.0 <= tool_reinforce_value <= 1.0, "tool_reinforce_value must be in [0, 1]"

        self._decay_factor = decay_factor
        self._activation_threshold = activation_threshold
        self._spillover_factor = spillover_factor
        self._tool_reinforce_value = tool_reinforce_value

        self._lock = threading.RLock()

        # region_id -> BrainRegionState
        self._regions: dict[str, BrainRegionState] = {}

        # entity_name -> region_id (built from BrainRegionInfo.members)
        self._entity_to_region: dict[str, str] = {}

        # region_id -> set of neighbor region_ids (for spillover)
        self._neighbors: dict[str, set[str]] = {}

        # label -> region_id index for O(1) lookup by label
        self._label_index: dict[str, str] = {}

        # region_id -> description (from BrainRegionInfo.description)
        self._descriptions: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    def initialize_from_regions(
        self,
        regions: list[BrainRegionInfo],
    ) -> None:
        """Initialize activation state from RegionManager's region list.

        All regions start with activation=0.0.

        Args:
            regions: List of BrainRegionInfo from RegionManager.get_all_regions()
        """
        with self._lock:
            self._regions.clear()
            self._entity_to_region.clear()
            self._label_index.clear()
            self._descriptions.clear()
            self._neighbors.clear()

            for region in regions:
                self._regions[region.community_id] = BrainRegionState(
                    region_id=region.community_id,
                    label=region.label,
                    activation=0.0,
                    last_activated_at=0.0,
                    activation_count=0,
                    manually_dimmed=False,
                )
                self._label_index[region.label] = region.community_id
                self._descriptions[region.community_id] = region.description or ""

                # Build entity -> region mapping from members
                for entity_name in region.members:
                    self._entity_to_region[entity_name] = region.community_id

            logger.info(
                "初始化脑区激活管理器: %d 个区域, %d 个实体映射",
                len(self._regions),
                len(self._entity_to_region),
            )

    # ------------------------------------------------------------------
    # Activation (query hit)
    # ------------------------------------------------------------------

    def activate_regions(
        self,
        hit_entities: list[str],
        entity_to_region: dict[str, str],
    ) -> set[str]:
        """Activate regions based on query-hit entities.

        Rules:
        - Find which regions the hit_entities belong to
        - Skip regions where manually_dimmed=True
        - Set activation = 1.0 (full, not additive)
        - Spillover: neighboring regions get spillover_factor * activation

        Args:
            hit_entities: Entity names that matched the query
            entity_to_region: entity_name -> region_id mapping
                (uses internal map as fallback)

        Returns:
            Set of activated region_ids.
        """
        with self._lock:
            activated_regions: set[str] = set()

            # Resolve each hit entity to its region
            for entity in hit_entities:
                # Prefer the provided mapping, fall back to internal map
                # Use explicit None check (not `or`) to avoid falsy-value bugs
                region_id = entity_to_region.get(entity)
                if region_id is None:
                    region_id = self._entity_to_region.get(entity)
                if region_id is None:
                    continue

                state = self._regions.get(region_id)
                if state is None:
                    continue

                # Skip manually dimmed regions
                if state.manually_dimmed:
                    logger.debug(
                        "跳过手动调暗的区域: %s (%s)", state.label, region_id
                    )
                    continue

                # Activate: full activation = 1.0
                state.activation = 1.0
                state.last_activated_at = time.time()
                state.activation_count += 1
                activated_regions.add(region_id)

            # Spillover to neighbors
            for region_id in activated_regions:
                self._spillover_to_neighbors(region_id)

            if activated_regions:
                logger.info(
                    "激活 %d 个脑区: %s",
                    len(activated_regions),
                    [self._regions[rid].label for rid in activated_regions if rid in self._regions],
                )

            return activated_regions

    # ------------------------------------------------------------------
    # Reinforce (tool use)
    # ------------------------------------------------------------------

    def reinforce_by_tool_use(
        self,
        tool_name: str,
        tool_to_region: dict[str, str],
    ) -> str | None:
        """Reinforce when a tool within a region is actually called.

        Rules:
        - Find which region the tool belongs to
        - activation = max(current, tool_reinforce_value)
        - Skip if manually_dimmed=True

        Args:
            tool_name: Name of the tool that was called
            tool_to_region: tool_name -> region_id mapping

        Returns:
            The reinforced region_id, or None.
        """
        with self._lock:
            region_id = tool_to_region.get(tool_name)
            if region_id is None:
                return None

            state = self._regions.get(region_id)
            if state is None:
                return None

            # Skip manually dimmed regions
            if state.manually_dimmed:
                logger.debug(
                    "跳过手动调暗的区域（工具强化）: %s (%s)", state.label, region_id
                )
                return None

            # Reinforce: max(current, reinforce_value)
            old_activation = state.activation
            state.activation = max(state.activation, self._tool_reinforce_value)
            state.last_activated_at = time.time()

            logger.debug(
                "工具强化脑区 %s (%s): %.2f -> %.2f",
                state.label,
                region_id,
                old_activation,
                state.activation,
            )

            return region_id

    # ------------------------------------------------------------------
    # Manual control
    # ------------------------------------------------------------------

    def manual_activate(self, region_labels: list[str]) -> set[str]:
        """Manual activation via brain_region_activate tool.

        activation = 1.0, manually_dimmed = False.

        Args:
            region_labels: Human-readable region labels to activate

        Returns:
            Set of successfully activated region_ids.
        """
        with self._lock:
            activated: set[str] = set()
            for label in region_labels:
                state = self.find_region_by_label(label)
                if state is None:
                    logger.warning("手动激活: 未找到区域 '%s'", label)
                    continue

                state.activation = 1.0
                state.manually_dimmed = False
                state.last_activated_at = time.time()
                state.activation_count += 1
                activated.add(state.region_id)

                # Spillover from manually activated region
                self._spillover_to_neighbors(state.region_id)

                logger.info("手动激活脑区: %s (%s)", label, state.region_id)
            return activated

    def manual_dim(self, region_labels: list[str]) -> None:
        """Manual dim via brain_region_dim tool.

        activation = 0.0, manually_dimmed = True.

        Args:
            region_labels: Human-readable region labels to dim
        """
        with self._lock:
            for label in region_labels:
                state = self.find_region_by_label(label)
                if state is None:
                    logger.warning("手动调暗: 未找到区域 '%s'", label)
                    continue

                state.activation = 0.0
                state.manually_dimmed = True

                logger.info("手动调暗脑区: %s (%s)", label, state.region_id)

    # ------------------------------------------------------------------
    # Decay
    # ------------------------------------------------------------------

    def decay_all(self) -> None:
        """Decay all regions after each conversation turn.

        activation *= decay_factor, then clear manually_dimmed flags
        (so they can be auto-activated on the next turn).
        """
        with self._lock:
            for state in self._regions.values():
                state.activation *= self._decay_factor
                # Clamp to avoid floating-point drift below 0
                if state.activation < 0.001:
                    state.activation = 0.0
                # Clear manually_dimmed flag for next turn
                state.manually_dimmed = False

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_active_regions(self) -> list[BrainRegionState]:
        """Get regions with activation > activation_threshold, sorted by activation desc."""
        with self._lock:
            active = [
                state
                for state in self._regions.values()
                if state.activation > self._activation_threshold
            ]
            active.sort(key=lambda s: s.activation, reverse=True)
            return active

    def get_region_map(self) -> list[BrainRegionState]:
        """Get all region states (thread-safe copy)."""
        with self._lock:
            return list(self._regions.values())

    def get_status_light(self, activation: float) -> str:
        """Three-status light:
        > 0.7 -> lit (green)
        > 0.1 -> dimming (yellow)
        else  -> off (black)
        """
        if activation > 0.7:
            return STATUS_LIT
        elif activation > 0.1:
            return STATUS_DIMMING
        else:
            return STATUS_OFF

    def get_entity_to_region_map(self) -> dict[str, str]:
        """Get entity_name -> region_id mapping for all known entities."""
        return dict(self._entity_to_region)

    def get_region_state(self, region_id: str) -> BrainRegionState | None:
        """Get activation state for a specific region by region_id."""
        return self._regions.get(region_id)

    def get_members_of_region(self, region_id: str) -> list[str]:
        """Get entity names belonging to a specific region."""
        return [
            entity
            for entity, rid in self._entity_to_region.items()
            if rid == region_id
        ]

    def get_region_description(self, region_id: str) -> str:
        """Get the description for a region (from BrainRegionInfo.description)."""
        return self._descriptions.get(region_id, "")

    # ------------------------------------------------------------------
    # Neighbor relationships
    # ------------------------------------------------------------------

    def set_region_neighbors(
        self,
        neighbor_map: dict[str, set[str]],
    ) -> None:
        """Set the neighbor relationships between regions for spillover calculation.

        Removes self-loops and stores the cleaned map under lock.

        Args:
            neighbor_map: region_id -> set of neighbor region_ids
        """
        cleaned: dict[str, set[str]] = {}
        for region_id, neighbors in neighbor_map.items():
            neighbor_set = set(neighbors)
            if region_id in neighbor_set:
                logger.warning("Self-loop in neighbor map for %s, removing", region_id)
                neighbor_set.discard(region_id)
            cleaned[region_id] = neighbor_set

        with self._lock:
            self._neighbors = cleaned
        logger.info(
            "设置脑区邻居关系: %d 个区域有邻居",
            len(cleaned),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _spillover_to_neighbors(self, region_id: str) -> None:
        """Spread activation to neighboring regions.

        Each neighbor gets spillover_factor * source activation,
        but only if neighbor is not manually dimmed and the spillover
        value exceeds neighbor's current activation.
        """
        neighbors = self._neighbors.get(region_id, set())
        source_state = self._regions.get(region_id)
        if source_state is None:
            return

        spillover_value = self._spillover_factor * source_state.activation

        for neighbor_id in neighbors:
            neighbor_state = self._regions.get(neighbor_id)
            if neighbor_state is None:
                continue

            # Skip manually dimmed neighbors
            if neighbor_state.manually_dimmed:
                continue

            # Spillover only boosts, never reduces
            if spillover_value > neighbor_state.activation:
                neighbor_state.activation = spillover_value

                logger.debug(
                    "溢出到邻居脑区: %s -> %s, %.2f",
                    source_state.label,
                    neighbor_state.label,
                    spillover_value,
                )

    def find_region_by_label(self, label: str) -> BrainRegionState | None:
        """Find a BrainRegionState by its human-readable label (O(1) via index)."""
        with self._lock:
            region_id = self._label_index.get(label)
            if region_id is not None:
                return self._regions.get(region_id)
            return None