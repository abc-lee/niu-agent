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

    region_id: str  # unique region name (e.g. "Python脑区") — used as dict key
    community_id: str  # Leiden community ID (e.g. "community_3"), empty for default regions
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

        # Co-activation tracking for merge candidates
        self._co_activation_counts: dict[tuple[str, str], int] = {}
        self._total_activation_rounds: int = 0
        # Cached member counts (updated on initialize/remove)
        self._member_counts: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    def initialize_from_regions(
        self,
        regions: list[BrainRegionInfo],
    ) -> None:
        """Initialize activation state from RegionManager's region list.

        Existing regions preserve their activation state; new regions start with activation=0.0.

        Args:
            regions: List of BrainRegionInfo from RegionManager.get_all_regions()
        """
        with self._lock:
            # Preserve existing activation state across re-initialization
            old_state = {
                rid: (state.activation, state.last_activated_at, state.activation_count, state.manually_dimmed)
                for rid, state in self._regions.items()
            }
            self._regions.clear()
            self._entity_to_region.clear()
            self._label_index.clear()
            self._descriptions.clear()
            self._neighbors.clear()
            # Preserve co-activation state across re-initialization
            # (merge candidates depend on accumulated history)
            # co_activation_counts and total_activation_rounds are NOT cleared

            for region in regions:
                # Restore preserved state if region existed before, otherwise default to 0
                prev_activation, prev_last_at, prev_count, prev_dimmed = old_state.get(region.name, (0.0, 0.0, 0, False))
                self._regions[region.name] = BrainRegionState(
                    region_id=region.name,
                    community_id=region.community_id,
                    label=region.label,
                    activation=prev_activation,
                    last_activated_at=prev_last_at,
                    activation_count=prev_count,
                    manually_dimmed=prev_dimmed,
                )
                self._label_index[region.label] = region.name
                self._descriptions[region.name] = region.description or ""

                # Build entity -> region mapping from members
                for entity_name in region.members:
                    self._entity_to_region[entity_name] = region.name

                # Cache member count for O(1) lookup in get_merge_candidates
                self._member_counts[region.name] = len(region.members)

            preserved_count = sum(1 for rid in old_state if rid in self._regions)
            logger.info(
                "初始化脑区激活管理器: %d 个区域, %d 个实体映射, %d 个保留激活状态",
                len(self._regions),
                len(self._entity_to_region),
                preserved_count,
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
                if region_id is not None and region_id not in self._regions:
                    region_id = None  # Stale external mapping, try internal
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

            # Track co-activation for merge candidates
            activated_list = sorted(activated_regions)
            self._total_activation_rounds += 1
            for i in range(len(activated_list)):
                for j in range(i + 1, len(activated_list)):
                    pair = (activated_list[i], activated_list[j])
                    self._co_activation_counts[pair] = (
                        self._co_activation_counts.get(pair, 0) + 1
                    )

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

    def manual_dim(self, region_labels: list[str], reason: str = "") -> None:
        """Manual dim via brain_region_dim tool.

        activation = 0.0, manually_dimmed = True.

        Args:
            region_labels: Human-readable region labels to dim
            reason: Optional reason for dimming (for memory logging).
        """
        with self._lock:
            for label in region_labels:
                state = self.find_region_by_label(label)
                if state is None:
                    logger.warning("手动调暗: 未找到区域 '%s'", label)
                    continue

                state.activation = 0.0
                state.manually_dimmed = True

                if reason:
                    logger.info("手动调暗脑区: %s (%s), reason: %s", label, state.region_id, reason)
                else:
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
        > 0.3 -> dimming (yellow)
        else  -> off (black)
        """
        if activation > 0.7:
            return STATUS_LIT
        elif activation > 0.3:
            return STATUS_DIMMING
        else:
            return STATUS_OFF

    def get_entity_to_region_map(self) -> dict[str, str]:
        """Get entity_name -> region_id mapping for all known entities."""
        with self._lock:
            return dict(self._entity_to_region)

    def get_region_state(self, region_id: str) -> BrainRegionState | None:
        """Get activation state for a specific region by region_id."""
        with self._lock:
            return self._regions.get(region_id)

    def get_members_of_region(self, region_id: str) -> list[str]:
        """Get entity names belonging to a specific region."""
        with self._lock:
            return [
                entity
                for entity, rid in self._entity_to_region.items()
                if rid == region_id
            ]

    def get_region_description(self, region_id: str) -> str:
        """Get the description for a region (from BrainRegionInfo.description)."""
        with self._lock:
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

        The input neighbor_map is keyed by community_id (from build_neighbor_map).
        We translate keys/values to region_id (region.name) so spillover lookups
        work against the _regions dict which is keyed by region_id.

        Args:
            neighbor_map: community_id -> set of neighbor community_ids
        """
        with self._lock:
            # Build community_id -> region_id translation table
            cid_to_rid: dict[str, str] = {}
            for state in self._regions.values():
                if state.community_id:
                    cid_to_rid[state.community_id] = state.region_id

            cleaned: dict[str, set[str]] = {}
            for cid, neighbors in neighbor_map.items():
                # Translate community_id key to region_id
                rid = cid_to_rid.get(cid, cid)
                neighbor_set = set()
                for n_cid in neighbors:
                    n_rid = cid_to_rid.get(n_cid, n_cid)
                    neighbor_set.add(n_rid)

                if rid in neighbor_set:
                    logger.warning("Self-loop in neighbor map for %s, removing", rid)
                    neighbor_set.discard(rid)
                cleaned[rid] = neighbor_set

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

    # ------------------------------------------------------------------
    # Merge candidates (co-activation based)
    # ------------------------------------------------------------------

    def get_merge_candidates(
        self,
        co_activation_threshold: float = 0.9,
    ) -> list[tuple[str, str]]:
        """Return pairs of region_ids that should be merged.

        Two regions are merge candidates if:
        - Their co-activation ratio > co_activation_threshold (default 90%)

        Args:
            co_activation_threshold: Minimum co-activation ratio (0-1)

        Returns:
            List of (region_A_id, region_B_id) pairs, sorted by ratio desc.
        """
        with self._lock:
            if self._total_activation_rounds < 5:
                return []

            candidates: list[tuple[str, str, float]] = []
            for (a, b), count in self._co_activation_counts.items():
                ratio = count / self._total_activation_rounds
                if ratio < co_activation_threshold:
                    continue

                # Both must still exist
                if a not in self._regions or b not in self._regions:
                    continue
                candidates.append((a, b, ratio))

            # Sort by ratio descending (merge strongest pairs first)
            candidates.sort(key=lambda x: x[2], reverse=True)

            # Deduplicate: each region appears in at most one pair
            used: set[str] = set()
            result: list[tuple[str, str]] = []
            for a, b, ratio in candidates:
                if a in used or b in used:
                    continue
                result.append((a, b))
                used.add(a)
                used.add(b)

            if result:
                logger.info(
                    "发现 %d 对合并候选脑区（共激活阈值 %.0f%%）",
                    len(result), co_activation_threshold * 100,
                )

            return result

    def merge_region_into(self, source_id: str, target_id: str) -> None:
        """Merge source region into target region (after KG merge).

        Transfers all entity-to-region mappings and member counts from
        source to target, then removes the source region.

        Args:
            source_id: Region ID being merged away.
            target_id: Region ID absorbing the source.
        """
        with self._lock:
            # Get source state before removal (need label for index cleanup)
            source_state = self._regions.get(source_id)

            # Reassign source entities to target
            source_members = [
                entity for entity, rid in self._entity_to_region.items()
                if rid == source_id
            ]
            for entity in source_members:
                self._entity_to_region[entity] = target_id

            # Update target member count
            target_count = self._member_counts.get(target_id, 0)
            source_count = self._member_counts.get(source_id, 0)
            self._member_counts[target_id] = target_count + source_count

            # Remove source region state (in-line to avoid lock gap)
            self._regions.pop(source_id, None)
            self._member_counts.pop(source_id, None)

            # Clean label index and descriptions
            if source_state is not None:
                self._label_index.pop(source_state.label, None)
            self._descriptions.pop(source_id, None)

            # Remove co-activation counts involving source
            keys_to_remove = [
                key for key in self._co_activation_counts
                if source_id in key
            ]
            for key in keys_to_remove:
                self._co_activation_counts.pop(key, None)

            # Clean stale neighbor references and transfer source's
            # neighbors to target so spillover paths survive the merge
            source_neighbors = self._neighbors.pop(source_id, set())
            target_neighbors = self._neighbors.get(target_id, set())
            self._neighbors[target_id] = target_neighbors | source_neighbors
            self._neighbors[target_id].discard(target_id)  # Remove potential self-loop
            for neighbors in self._neighbors.values():
                neighbors.discard(source_id)

    def remove_region(self, region_id: str) -> None:
        """Remove a region from activation tracking (after merge or dissolve).

        Cleans up the region state, entity mappings, and co-activation counts.
        """
        with self._lock:
            # Remove region state
            state = self._regions.pop(region_id, None)
            if state is None:
                return

            # Remove from label index
            self._label_index.pop(state.label, None)
            self._descriptions.pop(region_id, None)
            self._member_counts.pop(region_id, None)

            # Remove entity -> region mappings for this region
            entities_to_remove = [
                entity for entity, rid in self._entity_to_region.items()
                if rid == region_id
            ]
            for entity in entities_to_remove:
                self._entity_to_region.pop(entity, None)

            # Remove co-activation counts involving this region
            keys_to_remove = [
                key for key in self._co_activation_counts
                if region_id in key
            ]
            for key in keys_to_remove:
                self._co_activation_counts.pop(key, None)

            # Clean stale neighbor references
            for neighbors in self._neighbors.values():
                neighbors.discard(region_id)
            self._neighbors.pop(region_id, None)

            logger.info("移除脑区激活追踪: %s (%s)", state.label, region_id)