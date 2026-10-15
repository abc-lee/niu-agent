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


# ---------------------------------------------------------------------------
# 防御分支：result is None（adapter 不可用）→ graph_snapshot/explore_node 503，
# search_entities 200 空列表（T3 审查补全）
# ---------------------------------------------------------------------------


def test_graph_snapshot_none_to_503(monkeypatch):
    """graph_snapshot: adapter 返回 None → 503 + "LightRAG not available" 字面量。"""
    _patch_adapter(monkeypatch, _FakeAdapter(snapshot=None))

    resp = _make_client().get("/api/kg/snapshot")

    assert resp.status_code == 503
    assert resp.json() == {"status": "error", "message": "LightRAG not available"}


def test_explore_node_none_to_503(monkeypatch):
    """explore_node: adapter 返回 None → 503 + "LightRAG not available" 字面量。"""
    _patch_adapter(monkeypatch, _FakeAdapter(explore=None))

    resp = _make_client().post("/api/kg/explore", json={"entity_id": "x", "depth": 2})

    assert resp.status_code == 503
    assert resp.json() == {"status": "error", "message": "LightRAG not available"}


def test_search_entities_none_stays_200(monkeypatch):
    """search_entities: adapter 返回 None → 200 + 空列表（与 no_results 同语义）。"""
    _patch_adapter(monkeypatch, _FakeAdapter(query_data=None))

    resp = _make_client().get("/api/kg/search_entities", params={"query": "python", "top_k": 20})

    assert resp.status_code == 200
    assert resp.json() == {"entities": []}


# ---------------------------------------------------------------------------
# 503 分类精确匹配：仅两个字面量归 503，含 "not available" 子串的真实错误归 500
# （T3 审查补全——防宽子串误分类）
# ---------------------------------------------------------------------------


def test_graph_snapshot_not_available_substring_is_500(monkeypatch):
    """graph_snapshot: 错误文本含 "not available" 子串但非字面量 → 500（不误分类 503）。"""
    fake = _FakeAdapter(
        snapshot={
            "nodes": [], "edges": [],
            "status": "error", "message": "storage not available: disk error",
        }
    )
    _patch_adapter(monkeypatch, fake)

    resp = _make_client().get("/api/kg/snapshot")

    assert resp.status_code == 500
    assert resp.json() == {"status": "error", "message": "storage not available: disk error"}


# ---------------------------------------------------------------------------
# error dict 缺 message 键 → 端点默认文案（T3 审查补全）
# ---------------------------------------------------------------------------


def test_graph_snapshot_error_dict_missing_message_defaults(monkeypatch):
    """graph_snapshot: error dict 无 message 键 → 500 + 默认文案"知识图谱不可用"。"""
    _patch_adapter(monkeypatch, _FakeAdapter(snapshot={"nodes": [], "edges": [], "status": "error"}))

    resp = _make_client().get("/api/kg/snapshot")

    assert resp.status_code == 500
    assert resp.json() == {"status": "error", "message": "知识图谱不可用"}


def test_explore_node_error_dict_missing_message_defaults(monkeypatch):
    """explore_node: error dict 无 message 键 → 500 + 默认文案"知识图谱不可用"。"""
    _patch_adapter(
        monkeypatch,
        _FakeAdapter(explore={"center": None, "nodes": [], "edges": [], "status": "error"}),
    )

    resp = _make_client().post("/api/kg/explore", json={"entity_id": "x", "depth": 2})

    assert resp.status_code == 500
    assert resp.json() == {"status": "error", "message": "知识图谱不可用"}


def test_search_entities_error_dict_missing_message_defaults(monkeypatch):
    """search_entities: error dict 无 message 键 → 500 + 默认文案"知识图谱检索失败"。"""
    _patch_adapter(monkeypatch, _FakeAdapter(query_data={"status": "error"}))

    resp = _make_client().get("/api/kg/search_entities", params={"query": "python", "top_k": 20})

    assert resp.status_code == 500
    assert resp.json() == {"status": "error", "message": "知识图谱检索失败"}


# ---------------------------------------------------------------------------
# score 透传：distance → score（数值透传 / 缺失·非数值·NaN ⇒ None）
# （photo-search-cutoff T1 追加；既有用例一字未改）
# ---------------------------------------------------------------------------


def test_search_entities_score_numeric_passthrough(monkeypatch):
    """search_entities: distance 为数值 ⇒ 响应 score 为该数值，且既有字段原样保留。"""
    fake = _FakeAdapter(query_data={
        "status": "success",
        "data": {"entities": [
            {"entity_name": "行驶证", "entity_type": "Document", "description": "a<SEP>b", "distance": 0.742},
            {"entity_name": "身份证", "entity_type": "Document", "description": "c", "distance": 0.61},
        ]},
    })
    _patch_adapter(monkeypatch, fake)

    resp = _make_client().get("/api/kg/search_entities", params={"query": "证件", "top_k": 20})

    assert resp.status_code == 200
    body = resp.json()
    assert body["entities"][0] == {
        "id": "行驶证",
        "name": "行驶证",
        "entityType": "Document",
        "description": "a b",
        "score": 0.742,
    }
    assert body["entities"][1] == {
        "id": "身份证",
        "name": "身份证",
        "entityType": "Document",
        "description": "c",
        "score": 0.61,
    }


def test_search_entities_score_missing_is_none(monkeypatch):
    """search_entities: 实体无 distance（合成置顶行 / fields 剥离）⇒ score 是 None。"""
    fake = _FakeAdapter(query_data={
        "status": "success",
        "data": {"entities": [
            {"entity_name": "<user>", "entity_type": "Person", "description": "person"},
        ]},
    })
    _patch_adapter(monkeypatch, fake)

    resp = _make_client().get("/api/kg/search_entities", params={"query": "<user>", "top_k": 20})

    assert resp.status_code == 200
    entity = resp.json()["entities"][0]
    assert entity["score"] is None
    # 既有字段不受 score 键影响
    assert entity["id"] == "<user>"
    assert entity["name"] == "<user>"
    assert entity["entityType"] == "Person"
    assert entity["description"] == "person"


def test_search_entities_score_non_numeric_is_none(monkeypatch):
    """search_entities: distance 非数值（"abc"）⇒ score 是 None（float 转换失败，不 500）。"""
    fake = _FakeAdapter(query_data={
        "status": "success",
        "data": {"entities": [
            {"entity_name": "a", "entity_type": "T", "description": "", "distance": "abc"},
        ]},
    })
    _patch_adapter(monkeypatch, fake)

    resp = _make_client().get("/api/kg/search_entities", params={"query": "a", "top_k": 20})

    assert resp.status_code == 200
    entity = resp.json()["entities"][0]
    assert entity["score"] is None
    assert entity["id"] == "a"


def test_search_entities_score_nan_is_none(monkeypatch):
    """search_entities: distance 为 NaN ⇒ score 是 None（math.isfinite 护栏，不泄漏 NaN 到 JSON）。"""
    fake = _FakeAdapter(query_data={
        "status": "success",
        "data": {"entities": [
            {"entity_name": "n", "entity_type": "T", "description": "", "distance": float("nan")},
        ]},
    })
    _patch_adapter(monkeypatch, fake)

    resp = _make_client().get("/api/kg/search_entities", params={"query": "n", "top_k": 20})

    assert resp.status_code == 200
    entity = resp.json()["entities"][0]
    assert entity["score"] is None
    assert entity["id"] == "n"
def test_search_entities_score_inf_is_none(monkeypatch):
    """search_entities: distance 为 Inf ⇒ score 是 None（math.isfinite 护栏，与 NaN 用例同构）。"""
    fake = _FakeAdapter(query_data={
        "status": "success",
        "data": {"entities": [
            {"entity_name": "i", "entity_type": "T", "description": "", "distance": float("inf")},
        ]},
    })
    _patch_adapter(monkeypatch, fake)

    resp = _make_client().get("/api/kg/search_entities", params={"query": "i", "top_k": 20})

    assert resp.status_code == 200
    entity = resp.json()["entities"][0]
    assert entity["score"] is None
    assert entity["id"] == "i"


def test_search_entities_dedup_and_top_k_truncation_unchanged(monkeypatch):
    """search_entities: 加 score 键后去重（entity_name 首个保留）与 top_k 截断行为不变。"""
    fake = _FakeAdapter(query_data={
        "status": "success",
        "data": {"entities": [
            {"entity_name": "a", "entity_type": "T", "description": "", "distance": 0.9},
            {"entity_name": "a", "entity_type": "T", "description": "", "distance": 0.5},  # 重复，丢弃
            {"entity_name": "b", "entity_type": "T", "description": "", "distance": 0.8},
            {"entity_name": "c", "entity_type": "T", "description": "", "distance": 0.7},
        ]},
    })
    _patch_adapter(monkeypatch, fake)

    resp = _make_client().get("/api/kg/search_entities", params={"query": "x", "top_k": 2})

    assert resp.status_code == 200
    body = resp.json()
    # 去重后 top_k=2 截断：保留首个 a 与 b（c 被截断）
    assert [e["name"] for e in body["entities"]] == ["a", "b"]
    assert body["entities"][0]["score"] == 0.9   # 保留的是首个重复项的 distance
    assert body["entities"][1]["score"] == 0.8
