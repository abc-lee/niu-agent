"""
Tests for agent/brain_tools.py — Brain region MCP tool handlers and singleton accessor.

Verifies:
1. handle_brain_region_activate — manual_activate called, status returned
2. handle_brain_region_dim — manual_dim called, status returned
3. handle_brain_region_status — status format with include_dark option
4. handle_brain_region_status_empty — empty status when no regions
5. set_get_activation_mgr — singleton accessor round-trip
"""

from unittest.mock import MagicMock, patch

from niu_api.internal.region_activation import (
    RegionActivationManager,
    BrainRegionState,
    STATUS_LIT,
    STATUS_DIMMING,
    STATUS_OFF,
)
from niu_api.internal.region_manager import BrainRegionInfo

from agent.brain_tools import (
    handle_brain_region_activate,
    handle_brain_region_dim,
    handle_brain_region_status,
    set_activation_mgr,
    get_activation_mgr,
    set_tool_to_region,
    get_tool_to_region,
    reinforce_on_tool_use,
)


# ============== Helpers ==============


def _make_region_infos() -> list[BrainRegionInfo]:
    """Create test BrainRegionInfo list."""
    return [
        BrainRegionInfo(
            name="brain:region:Python",
            label="Python",
            community_id="community_0",
            description="Python programming",
            size=3,
            representative="Python",
            members=["Python", "Django", "FastAPI"],
            updated_at=1745366400.0,
        ),
        BrainRegionInfo(
            name="brain:region:React",
            label="React",
            community_id="community_1",
            description="React frontend framework",
            size=3,
            representative="React",
            members=["React", "Vue", "Angular"],
            updated_at=1745366400.0,
        ),
    ]


def _make_activation_mgr() -> RegionActivationManager:
    """Create a RegionActivationManager with initialized regions."""
    manager = RegionActivationManager()
    manager.initialize_from_regions(_make_region_infos())
    return manager


# ============== Test 1: handle_brain_region_activate ==============


class TestHandleBrainRegionActivate:
    """Verify handle_brain_region_activate calls manual_activate and returns status."""

    def test_activate_calls_manual_activate(self):
        """manual_activate is called for each region label."""
        mgr = _make_activation_mgr()
        set_activation_mgr(mgr)

        result = handle_brain_region_activate({"regions": ["Python"]})

        # Activation should be set to 1.0
        state = mgr._regions["community_0"]
        assert state.activation == 1.0
        assert state.manually_dimmed is False

    def test_activate_returns_formatted_status(self):
        """Result includes region label and activation value."""
        mgr = _make_activation_mgr()
        set_activation_mgr(mgr)

        result = handle_brain_region_activate({"regions": ["Python"]})

        assert "Python" in result
        assert "1.00" in result

    def test_activate_with_reason(self):
        """Reason is included in the output when provided."""
        mgr = _make_activation_mgr()
        set_activation_mgr(mgr)

        result = handle_brain_region_activate({
            "regions": ["Python"],
            "reason": "need Python knowledge",
        })

        assert "need Python knowledge" in result

    def test_activate_unknown_region(self):
        """Unknown region label shows not found in output."""
        mgr = _make_activation_mgr()
        set_activation_mgr(mgr)

        result = handle_brain_region_activate({"regions": ["Unknown"]})

        assert "not found" in result

    def test_activate_no_manager(self):
        """Returns error message when activation manager is not initialized."""
        set_activation_mgr(None)

        result = handle_brain_region_activate({"regions": ["Python"]})

        assert "not initialized" in result

    def test_activate_empty_regions(self):
        """Returns error message when regions list is empty."""
        mgr = _make_activation_mgr()
        set_activation_mgr(mgr)

        result = handle_brain_region_activate({"regions": []})

        assert "No regions specified" in result


# ============== Test 2: handle_brain_region_dim ==============


class TestHandleBrainRegionDim:
    """Verify handle_brain_region_dim calls manual_dim and returns status."""

    def test_dim_calls_manual_dim(self):
        """manual_dim is called, setting activation to 0 and manually_dimmed=True."""
        mgr = _make_activation_mgr()
        # First activate, then dim
        mgr.manual_activate(["Python"])
        set_activation_mgr(mgr)

        result = handle_brain_region_dim({"regions": ["Python"]})

        state = mgr._regions["community_0"]
        assert state.activation == 0.0
        assert state.manually_dimmed is True

    def test_dim_returns_formatted_status(self):
        """Result includes region label with dimmed status."""
        mgr = _make_activation_mgr()
        set_activation_mgr(mgr)

        result = handle_brain_region_dim({"regions": ["Python"]})

        assert "Python" in result
        assert "dimmed" in result

    def test_dim_no_manager(self):
        """Returns error message when activation manager is not initialized."""
        set_activation_mgr(None)

        result = handle_brain_region_dim({"regions": ["Python"]})

        assert "not initialized" in result

    def test_dim_empty_regions(self):
        """Returns error message when regions list is empty."""
        mgr = _make_activation_mgr()
        set_activation_mgr(mgr)

        result = handle_brain_region_dim({"regions": []})

        assert "No regions specified" in result


# ============== Test 3: handle_brain_region_status ==============


class TestHandleBrainRegionStatus:
    """Verify handle_brain_region_status format with include_dark option."""

    def test_status_shows_active_regions(self):
        """Active regions are shown with status lights and activation values."""
        mgr = _make_activation_mgr()
        # Activate Python region
        mgr.manual_activate(["Python"])
        set_activation_mgr(mgr)

        result = handle_brain_region_status({})

        assert "Python" in result
        assert "1.00" in result

    def test_status_include_dark(self):
        """include_dark=True shows all regions including inactive ones."""
        mgr = _make_activation_mgr()
        set_activation_mgr(mgr)

        # Without include_dark, only active regions shown
        result_no_dark = handle_brain_region_status({"include_dark": False})
        # With no active regions (all at 0.0), should say no active regions
        assert "No active" in result_no_dark

        # With include_dark, all regions shown
        result_with_dark = handle_brain_region_status({"include_dark": True})
        assert "Python" in result_with_dark
        assert "React" in result_with_dark

    def test_status_no_manager(self):
        """Returns error message when activation manager is not initialized."""
        set_activation_mgr(None)

        result = handle_brain_region_status({})

        assert "not initialized" in result


# ============== Test 4: handle_brain_region_status_empty ==============


class TestHandleBrainRegionStatusEmpty:
    """Verify empty status when no regions are initialized."""

    def test_empty_status_when_no_regions(self):
        """Manager with no initialized regions returns empty message."""
        mgr = RegionActivationManager()
        # Do NOT call initialize_from_regions — no regions
        set_activation_mgr(mgr)

        result = handle_brain_region_status({})

        assert "No brain regions" in result or "not initialized" in result or "No active" in result


# ============== Test 5: set_get_activation_mgr ==============


class TestSetGetActivationMgr:
    """Verify singleton accessor round-trip."""

    def test_set_and_get(self):
        """Setting and getting activation_mgr returns the same instance."""
        mgr = _make_activation_mgr()
        set_activation_mgr(mgr)

        result = get_activation_mgr()

        assert result is mgr

    def test_get_returns_none_initially(self):
        """get_activation_mgr returns None before any set call."""
        # Note: this test depends on no prior test having set the mgr
        # We explicitly set None to ensure clean state
        set_activation_mgr(None)

        result = get_activation_mgr()

        assert result is None

    def test_set_overwrites_previous(self):
        """Setting a new manager overwrites the previous one."""
        mgr1 = _make_activation_mgr()
        set_activation_mgr(mgr1)
        assert get_activation_mgr() is mgr1

        mgr2 = _make_activation_mgr()
        set_activation_mgr(mgr2)
        assert get_activation_mgr() is mgr2
        assert get_activation_mgr() is not mgr1


# ============== Test 6: reinforce_on_tool_use ==============


class TestReinforceOnToolUse:
    """Verify tool dispatch reinforce integration."""

    def test_reinforce_finds_region_for_tool(self):
        """reinforce_on_tool_use activates the correct region for a tool."""
        mgr = _make_activation_mgr()
        set_activation_mgr(mgr)
        set_tool_to_region({"kg-server/query": "community_0"})

        result = reinforce_on_tool_use("kg-server/query")

        assert result == "community_0"
        # Activation should be reinforced
        assert mgr._regions["community_0"].activation >= 0.85

    def test_reinforce_unknown_tool(self):
        """Unknown tool returns None."""
        mgr = _make_activation_mgr()
        set_activation_mgr(mgr)
        set_tool_to_region({})

        result = reinforce_on_tool_use("unknown-tool")

        assert result is None

    def test_reinforce_no_manager(self):
        """Returns None when activation manager is not set."""
        set_activation_mgr(None)

        result = reinforce_on_tool_use("kg-server/query")

        assert result is None