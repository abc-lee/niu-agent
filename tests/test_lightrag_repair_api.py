"""LightRAG 修复 API 测试"""
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_client():
    """构造仅挂载 kg_api router 的临时 FastAPI app，避免触发整个 niu_api.__main__ 初始化。"""
    from niu_api import kg_api

    app = FastAPI()
    app.include_router(kg_api.router)
    return TestClient(app)


def test_repair_endpoint_all_targets(monkeypatch):
    repair_calls = []
    monkeypatch.setattr("niu_api.internal.lightrag_repair.repair_all",
                        lambda: repair_calls.append("all") or {"vdb_entities.json": {"status": "ok"}})
    monkeypatch.setattr("niu_api.internal.lightrag_manager.reset_init_state", lambda: None)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity.check_all",
                        lambda: {"ok": True, "total_errors": 0})

    client = _make_client()
    # router prefix 是 /api/kg，端点是 /lightrag/repair，拼起来是 /api/kg/lightrag/repair
    response = client.post("/api/kg/lightrag/repair", params={"target": "all"})

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert repair_calls == ["all"]
    assert data["integrity"] == {"ok": True, "total_errors": 0}


def test_repair_endpoint_specific_vdb(monkeypatch):
    repair_calls = []
    monkeypatch.setattr("niu_api.internal.lightrag_repair.repair_vdb",
                        lambda name: repair_calls.append(name) or {"status": "ok", "rebuilt_count": 5})
    monkeypatch.setattr("niu_api.internal.lightrag_manager.reset_init_state", lambda: None)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity.check_all",
                        lambda: {"ok": True, "total_errors": 0})

    client = _make_client()
    response = client.post("/api/kg/lightrag/repair", params={"target": "vdb_entities.json"})

    assert response.status_code == 200
    assert repair_calls == ["vdb_entities.json"]


def test_repair_endpoint_unknown_target(monkeypatch):
    client = _make_client()
    response = client.post("/api/kg/lightrag/repair", params={"target": "unknown.txt"})

    assert response.status_code == 400
