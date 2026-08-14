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


# ============== consolidate: Task 2 — Step 4.5 update_default_region_sizes ==============


def _consolidate_context(update_return_value=None, update_side_effect=None):
    """consolidate 全 mock 上下文（T2-2 mock 清单）。

    get_region_sync（锁）+ LightRAGAdapter 构造 + CommunityDetector.detect_communities
    （partitions 非空 list + modularity float——round() 需要）+ _get_region_mgr
    （带 return values）+ _get_activation_mgr None + update/assign patch create=True。
    """
    from contextlib import ExitStack
    from unittest import mock

    stack = ExitStack()
    sync_mock = mock.MagicMock()
    sync_mock.try_acquire_sync.return_value = True
    region_mgr = mock.MagicMock()
    region_mgr.cleanup_stale_regions.return_value = ([], [], set())
    region_mgr.create_region_nodes.return_value = []
    region_mgr.get_all_regions.return_value = []
    region_mgr.update_region_summaries.return_value = None
    region_mgr.dissolve_shrunk_regions.return_value = []
    region_mgr.decay_structural_edges.return_value = {"decayed": 0, "deleted": 0}
    detection = mock.MagicMock()
    detection.partitions = [mock.MagicMock()]  # 非空——consolidate 无分区早退不触发
    detection.modularity = 0.42
    detection.total_regions = 3

    stack.enter_context(
        mock.patch("agent.injector.region_sync.get_region_sync", return_value=sync_mock)
    )
    stack.enter_context(
        mock.patch("niu_api.internal.lightrag_adapter.LightRAGAdapter")
    )
    stack.enter_context(
        mock.patch(
            "niu_api.internal.region_detector.CommunityDetector.detect_communities",
            return_value=detection,
        )
    )
    stack.enter_context(
        mock.patch("niu_api.brain_region_api._get_region_mgr", return_value=region_mgr)
    )
    stack.enter_context(
        mock.patch("niu_api.brain_region_api._get_activation_mgr", return_value=None)
    )
    update_mock = stack.enter_context(
        mock.patch(
            "niu_api.internal.region_manager.update_default_region_sizes",
            create=True,
            return_value=update_return_value,
            side_effect=update_side_effect,
        )
    )
    assign_mock = stack.enter_context(
        mock.patch(
            "niu_api.internal.region_manager.assign_entities_to_default_regions",
            create=True,
            return_value={},
        )
    )
    return stack, region_mgr, update_mock, assign_mock


def test_t2_2_consolidate_no_assign_update_sizes_decay_kept():
    """T2-2：consolidate Step 4.5 不再调 assign + 调 update_default_region_sizes + Step 8 decay 保留。"""
    from niu_api.brain_region_api import ConsolidateRequest, consolidate_brain_regions

    stack, region_mgr, update_mock, assign_mock = _consolidate_context(
        update_return_value={"updated": 3},
    )
    with stack:
        result = consolidate_brain_regions(ConsolidateRequest(resolution=1.0))

    assert result["status"] == "ok"
    update_mock.assert_called_once()
    assign_mock.assert_not_called()
    # Step 4.6 正常执行
    region_mgr.update_region_summaries.assert_called()
    # Step 8 decay 保留原位
    region_mgr.decay_structural_edges.assert_called()


def test_t2_2_variant_a_update_error_step46_continues():
    """update 抛异常 → 被吞 + Step 4.6 summaries 仍执行 + 返回 ok。"""
    from niu_api.brain_region_api import ConsolidateRequest, consolidate_brain_regions

    stack, region_mgr, update_mock, assign_mock = _consolidate_context(
        update_side_effect=RuntimeError("boom"),
    )
    with stack:
        result = consolidate_brain_regions(ConsolidateRequest(resolution=1.0))

    assert result["status"] == "ok"
    region_mgr.update_region_summaries.assert_called()
    assign_mock.assert_not_called()


def test_t2_2_variant_b_updated_zero_no_log():
    """updated=0：无 [Consolidate] Updated 日志。"""
    from unittest import mock

    import niu_api.brain_region_api as api_mod
    from niu_api.brain_region_api import ConsolidateRequest, consolidate_brain_regions

    info_calls = []
    stack, region_mgr, update_mock, assign_mock = _consolidate_context(
        update_return_value={"updated": 0},
    )
    with stack:
        with mock.patch.object(
            api_mod.logger, "info",
            side_effect=lambda *a, **k: info_calls.append(a),
        ):
            result = consolidate_brain_regions(ConsolidateRequest(resolution=1.0))

    assert result["status"] == "ok"
    assert not any(
        "Updated" in str(c) and "default region sizes" in str(c)
        for c in info_calls
    ), f"updated=0 不应有 Updated 日志: {info_calls}"
