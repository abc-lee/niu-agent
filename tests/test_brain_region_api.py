"""Tests for POST /api/brain/regions/update endpoint."""
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """Create test client with mocked RegionManager."""
    with patch("niu_api.brain_region_api._get_region_mgr") as mock_mgr, \
         patch("niu_api.brain_region_api._get_activation_mgr") as mock_act:
        mock_mgr.return_value = MagicMock()
        mock_mgr.return_value.get_all_regions.return_value = []
        mock_act_mgr = MagicMock()
        mock_act_mgr.get_region_map.return_value = []
        mock_act_mgr.get_status_light.return_value = "⚫"
        mock_act_mgr.set_activation = MagicMock(return_value=True)
        mock_act.return_value = mock_act_mgr
        from niu_api.__main__ import app
        yield TestClient(app)


def test_update_regions_success(client):
    """POST /api/brain/regions/update with valid data returns ok."""
    response = client.post("/api/brain/regions/update", json={
        "regions": [
            {"label": "聊天历史", "activation": 1.0},
            {"label": "文档库", "activation": 0.5},
            {"label": "知识体系", "activation": 0.0},
        ]
    })
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["updated"] == 3


def test_update_regions_empty_list(client):
    """POST /api/brain/regions/update with empty list returns updated=0."""
    response = client.post("/api/brain/regions/update", json={
        "regions": []
    })
    assert response.status_code == 200
    assert response.json()["updated"] == 0


def test_update_regions_calls_set_activation(client):
    """Verify set_activation is called with correct args."""
    with patch("niu_api.brain_region_api._get_activation_mgr") as mock_act:
        mock_act_mgr = MagicMock()
        mock_act_mgr.set_activation = MagicMock(return_value=True)
        mock_act.return_value = mock_act_mgr
        response = client.post("/api/brain/regions/update", json={
            "regions": [{"label": "聊天历史", "activation": 0.5}]
        })
        assert response.status_code == 200
        mock_act_mgr.set_activation.assert_called_once_with("聊天历史", 0.5)


def test_update_regions_503_when_activation_mgr_none():
    """Returns 503 when activation manager is not initialized."""
    with patch("niu_api.brain_region_api._get_region_mgr") as mock_mgr, \
         patch("niu_api.brain_region_api._get_activation_mgr", return_value=None):
        mock_mgr.return_value = MagicMock()
        mock_mgr.return_value.get_all_regions.return_value = []
        from niu_api.__main__ import app
        client = TestClient(app)
        response = client.post("/api/brain/regions/update", json={
            "regions": [{"label": "聊天历史", "activation": 1.0}]
        })
        assert response.status_code == 503


def test_update_regions_422_on_out_of_range_activation(client):
    """Returns 422 when activation is out of [0.0, 1.0] range."""
    response = client.post("/api/brain/regions/update", json={
        "regions": [{"label": "聊天历史", "activation": 1.5}]
    })
    assert response.status_code == 422

    response2 = client.post("/api/brain/regions/update", json={
        "regions": [{"label": "聊天历史", "activation": -0.1}]
    })
    assert response2.status_code == 422


def test_update_regions_500_on_set_activation_exception(client):
    """Returns 500 when set_activation raises an unexpected exception."""
    with patch("niu_api.brain_region_api._get_activation_mgr") as mock_act:
        mock_act_mgr = MagicMock()
        mock_act_mgr.set_activation = MagicMock(side_effect=RuntimeError("boom"))
        mock_act.return_value = mock_act_mgr
        response = client.post("/api/brain/regions/update", json={
            "regions": [{"label": "聊天历史", "activation": 0.5}]
        })
        assert response.status_code == 500


def test_update_regions_partial_success(client):
    """Updated count only includes regions where set_activation returned True."""
    with patch("niu_api.brain_region_api._get_activation_mgr") as mock_act:
        mock_act_mgr = MagicMock()
        mock_act_mgr.set_activation = MagicMock(side_effect=[True, False, True])
        mock_act.return_value = mock_act_mgr
        response = client.post("/api/brain/regions/update", json={
            "regions": [
                {"label": "聊天历史", "activation": 1.0},
                {"label": "不存在的", "activation": 0.5},
                {"label": "文档库", "activation": 0.0},
            ]
        })
        assert response.status_code == 200
        assert response.json()["updated"] == 2
