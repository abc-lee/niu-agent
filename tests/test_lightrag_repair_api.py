"""LightRAG 修复 API 测试"""

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_client():
    """构造仅挂载 kg_api router 的临时 FastAPI app，避免触发整个 niu_api.__main__ 初始化。"""
    from niu_api import kg_api

    app = FastAPI()
    app.include_router(kg_api.router)
    return TestClient(app)


def test_repair_endpoint_all_targets(monkeypatch):
    """v5: /api/kg/lightrag/repair 调 run_repair_on_user_request"""
    repair_calls = []
    monkeypatch.setattr("niu_api.internal.lightrag_manager.run_repair_on_user_request",
                        lambda: repair_calls.append("repair") or {
                            "repaired": True,
                            "check_ok": True,
                            "repair_result": {"vdb_entities.json": {"status": "ok"}},
                            "check_result": {"ok": True, "total_errors": 0},
                        })

    client = _make_client()
    response = client.post("/api/kg/lightrag/repair", params={"target": "all"})

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert repair_calls == ["repair"]
    assert data["result"]["repaired"] is True


def test_repair_endpoint_rejects_vdb_target(monkeypatch):
    """v5: target=vdb_entities.json 返回 400（v5 只支持 all）"""
    client = _make_client()
    response = client.post("/api/kg/lightrag/repair", params={"target": "vdb_entities.json"})

    assert response.status_code == 400


def test_repair_endpoint_unknown_target(monkeypatch):
    client = _make_client()
    response = client.post("/api/kg/lightrag/repair", params={"target": "unknown.txt"})

    assert response.status_code == 400
