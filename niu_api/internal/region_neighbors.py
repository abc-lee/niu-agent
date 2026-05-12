"""Brain region neighbor map construction.

Provides utility to build neighbor relationships between brain regions
based on shared members. Used for spillover activation.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def build_neighbor_map(
    regions: list[dict[str, Any]],
) -> dict[str, set[str]]:
    """Build neighbor map for spillover activation.

    Two regions are neighbors if they share at least one member.
    This enables spillover activation: when one region is activated,
    its neighbors receive partial activation (spillover_factor * activation).

    Args:
        regions: List of region info dicts, each with:
            - community_id: str (unique identifier)
            - members: list[str] (entity names belonging to the region)

    Returns:
        Dict mapping community_id -> set of neighbor community_ids.
        Only includes regions that have at least one neighbor.

    Example:
        >>> regions = [
        ...     {"community_id": "r1", "members": ["a", "b", "c"]},
        ...     {"community_id": "r2", "members": ["c", "d"]},  # shares "c" with r1
        ...     {"community_id": "r3", "members": ["e"]},       # no shared members
        ... ]
        >>> build_neighbor_map(regions)
        {'r1': {'r2'}, 'r2': {'r1'}}
    """
    neighbor_map: dict[str, set[str]] = {}

    for region in regions:
        neighbors = set()
        region_id = region.get("community_id", "")
        region_members = set(region.get("members", []))

        if not region_id or not region_members:
            continue

        for other in regions:
            other_id = other.get("community_id", "")
            other_members = set(other.get("members", []))

            # Different community + shared members = neighbors
            if region_id != other_id and region_members & other_members:
                neighbors.add(other_id)

        if neighbors:
            neighbor_map[region_id] = neighbors

    logger.debug("构建脑区邻居映射: %d 个区域有邻居", len(neighbor_map))
    return neighbor_map
