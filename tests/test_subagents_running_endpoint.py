"""/api/subagents/running 端点测试。"""
from unittest.mock import patch, MagicMock


def test_running_endpoint_empty():
    """无子 Agent 时返回 count=0。"""
    with patch("agent.subagent_registry.SubagentRegistry.list_running", return_value=[]):
        from niu_api.__main__ import app
        from fastapi.testclient import TestClient
        client = TestClient(app)
        resp = client.get("/api/subagents/running")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0
        assert data["subagents"] == []


def test_running_endpoint_with_subagents():
    """有子 Agent 时返回 count 和名字列表。"""
    mock_inst1 = MagicMock(unique_name="file-processor-a1b2", agent_type="file-processor", is_sync=True)
    mock_inst2 = MagicMock(unique_name="context-manager-c3d4", agent_type="context-manager", is_sync=True)
    with patch("agent.subagent_registry.SubagentRegistry.list_running", return_value=[mock_inst1, mock_inst2]):
        from niu_api.__main__ import app
        from fastapi.testclient import TestClient
        client = TestClient(app)
        resp = client.get("/api/subagents/running")
        data = resp.json()
        assert data["count"] == 2
        assert len(data["subagents"]) == 2
        assert data["subagents"][0]["unique_name"] == "file-processor-a1b2"
        assert data["subagents"][1]["unique_name"] == "context-manager-c3d4"
