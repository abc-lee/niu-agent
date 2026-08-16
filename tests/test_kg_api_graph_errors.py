"""kg_api 三端点 error dict → 错误响应测试（E3 T3，详设 D7）。

验证：graph_snapshot / explore_node / search_entities 识别 adapter 返回的
error dict（status=="error"）→ 转错误响应（HTTP 4xx/5xx + body
{"status": "error", "message": ...}）——不再把 error dict 当图数据回前端
（error 不再被 normalize 跳过原样回 / 不再 data.get("entities") 空列表静默）；
真空（no_results / 空图）保持 200 + 空数据，不误转错误响应。

挂载方式参照 tests/test_lightrag_repair_api.py L7-13：仅挂载 kg_api router，
避免触发整个 niu_api.__main__ 初始化。mock 源为 niu_api.kg_api._get_adapter
（端点内调用点），不触碰真实 LightRAG 单例 / DB。
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_client():
    """构造仅挂载 kg_api router 的临时 FastAPI app，避免触发整个 niu_api.__main__ 初始化。"""
    from niu_api import kg_api

    app = FastAPI()
    app.include_router(kg_api.router)
    return TestClient(app)


class _FakeAdapter:
    """预设返回结果的假 adapter——避免触碰真实 LightRAG 单例与 ~/.niu 数据。"""

    def __init__(self, snapshot=None, explore=None, query_data=None):
        self._snapshot = snapshot
        self._explore = explore
        self._query_data = query_data

    def get_graph_snapshot(self, limit=2000):
        return self._snapshot

    def explore_node(self, entity_name, depth=2, edge_types=None):
        return self._explore

    def query_data(self, query, mode="local", top_k=None, keywords=None, **kwargs):
        return self._query_data


def _patch_adapter(monkeypatch, fake):
    monkeypatch.setattr("niu_api.kg_api._get_adapter", lambda: fake)


# ---------------------------------------------------------------------------
# 错误分支：error dict（status=="error"）→ 错误响应（status/message 保留）
# ---------------------------------------------------------------------------


def test_graph_snapshot_unavailable_dict_to_503(monkeypatch):
    """graph_snapshot: adapter 返回不可用 error dict → 503 + status/message 保留。"""
    fake = _FakeAdapter(
        snapshot={
            "nodes": [], "edges": [],
            "status": "error", "message": "知识图谱不可用（初始化门控拒绝）",
        }
    )
    _patch_adapter(monkeypatch, fake)

    resp = _make_client().get("/api/kg/snapshot")

    assert resp.status_code == 503
    assert resp.json() == {"status": "error", "message": "知识图谱不可用（初始化门控拒绝）"}


def test_graph_snapshot_error_dict_to_500(monkeypatch):
    """graph_snapshot: adapter 返回普通读取错误 dict → 500 + status/message 保留。"""
    fake = _FakeAdapter(
        snapshot={"nodes": [], "edges": [], "status": "error", "message": "snapshot error: boom"}
    )
    _patch_adapter(monkeypatch, fake)

    resp = _make_client().get("/api/kg/snapshot")

    assert resp.status_code == 500
    assert resp.json() == {"status": "error", "message": "snapshot error: boom"}


def test_explore_node_error_dict_to_error_response(monkeypatch):
    """explore_node: adapter 返回 error dict → 错误响应（不再 normalize 跳过原样回）。"""
    fake = _FakeAdapter(
        explore={
            "center": None, "nodes": [], "edges": [],
            "stats": {"nodes": 0, "edges": 0, "max_depth": 2},
            "status": "error", "message": "explore failed: graph error",
        }
    )
    _patch_adapter(monkeypatch, fake)

    resp = _make_client().post("/api/kg/explore", json={"entity_id": "x", "depth": 2})

    assert resp.status_code == 500
    assert resp.json() == {"status": "error", "message": "explore failed: graph error"}


def test_search_entities_error_dict_to_error_response(monkeypatch):
    """search_entities: adapter 返回 error dict → 错误响应（不再空列表静默）。"""
    fake = _FakeAdapter(query_data={"status": "error", "message": "query failed: boom"})
    _patch_adapter(monkeypatch, fake)

    resp = _make_client().get("/api/kg/search_entities", params={"query": "python", "top_k": 20})

    assert resp.status_code == 500
    body = resp.json()
    assert body["status"] == "error"
    assert body["message"] == "query failed: boom"
    assert "entities" not in body


# ---------------------------------------------------------------------------
# 真空分支：no_results / 空图 → 200 + 空数据（不误转错误响应）
# ---------------------------------------------------------------------------


def test_graph_snapshot_empty_graph_stays_200(monkeypatch):
    """graph_snapshot: 空图（无 status 空壳）→ 200 + 空 nodes/edges。"""
    fake = _FakeAdapter(
        snapshot={"nodes": [], "edges": [], "stats": {"nodes": 0, "edges": 0, "limit": 2000}}
    )
    _patch_adapter(monkeypatch, fake)

    resp = _make_client().get("/api/kg/snapshot")

    assert resp.status_code == 200
    body = resp.json()
    assert body["nodes"] == []
    assert body["edges"] == []


def test_explore_node_no_results_stays_200(monkeypatch):
    """explore_node: 实体无邻居（无 status 空壳）→ 200 + 空数据。"""
    fake = _FakeAdapter(
        explore={
            "center": None, "nodes": [], "edges": [],
            "stats": {"nodes": 0, "edges": 0, "max_depth": 2},
        }
    )
    _patch_adapter(monkeypatch, fake)

    resp = _make_client().post("/api/kg/explore", json={"entity_id": "nonexistent", "depth": 2})

    assert resp.status_code == 200
    body = resp.json()
    assert body["nodes"] == []
    assert body["edges"] == []


def test_search_entities_no_results_stays_200(monkeypatch):
    """search_entities: no_results → 200 + {"entities": []}（不误转错误响应）。"""
    fake = _FakeAdapter(
        query_data={"status": "no_results", "data": {"entities": [], "relationships": [], "chunks": []}}
    )
    _patch_adapter(monkeypatch, fake)

    resp = _make_client().get("/api/kg/search_entities", params={"query": "zzz", "top_k": 20})

    assert resp.status_code == 200
    assert resp.json() == {"entities": []}


def test_search_entities_empty_query_stays_200(monkeypatch):
    """search_entities: 空 query → 200 + 空列表（不触碰 adapter）。"""
    def _boom():
        raise AssertionError("empty query must not touch adapter")

    monkeypatch.setattr("niu_api.kg_api._get_adapter", _boom)

    resp = _make_client().get("/api/kg/search_entities", params={"query": "  "})

    assert resp.status_code == 200
    assert resp.json() == {"entities": []}
