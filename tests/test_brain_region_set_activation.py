"""Tests for RegionActivationManager.set_activation()."""
import pytest
from niu_api.internal.region_activation import RegionActivationManager, BrainRegionState


@pytest.fixture
def mgr():
    """Create a manager with 3 test regions."""
    m = RegionActivationManager()
    m._regions = {
        "region_a": BrainRegionState(
            region_id="region_a", community_id="c1", label="区域A",
            activation=0.0, last_activated_at=0, activation_count=0, manually_dimmed=False,
        ),
        "region_b": BrainRegionState(
            region_id="region_b", community_id="c2", label="区域B",
            activation=0.5, last_activated_at=0, activation_count=0, manually_dimmed=False,
        ),
    }
    m._label_index = {"区域A": "region_a", "区域B": "region_b"}
    return m


def test_set_activation_green(mgr):
    """Set activation to 1.0 (green)."""
    mgr.set_activation("区域A", 1.0)
    state = mgr.find_region_by_label("区域A")
    assert state.activation == 1.0
    assert state.manually_dimmed is False


def test_set_activation_yellow(mgr):
    """Set activation to 0.5 (yellow/dimming)."""
    mgr.set_activation("区域A", 0.5)
    state = mgr.find_region_by_label("区域A")
    assert state.activation == 0.5
    assert state.manually_dimmed is False


def test_set_activation_black(mgr):
    """Set activation to 0.0 (black/off)."""
    mgr.set_activation("区域B", 0.0)
    state = mgr.find_region_by_label("区域B")
    assert state.activation == 0.0
    assert state.manually_dimmed is True


def test_set_activation_updates_last_activated_at(mgr):
    """Setting activation updates last_activated_at timestamp."""
    mgr.set_activation("区域A", 1.0)
    state = mgr.find_region_by_label("区域A")
    assert state.last_activated_at > 0


def test_set_activation_unknown_label(mgr):
    """Unknown label is silently ignored (no exception)."""
    mgr.set_activation("不存在的区域", 1.0)
    # No exception raised, no state changed
    assert mgr.find_region_by_label("区域A").activation == 0.0
