"""S2：独立媒体检索工具 search_media —— lightrag-server 同进程面直调（新面 + 节点解析降级形状）。

对应 plan §5「S2」Step 5 用例 ①–⑥（+ ⑦ 两面存在性 / ⑧ top_k 守卫与上界）：
- 测试面走「lightrag-server 同进程面直调 `search_media`」（本模块无 stdio 面，A12）；
- 媒体库经 `NIU_MEDIA_STORE` 接缝重定向临时目录（`media_vectors.media_store_path()`
  每次现读环境变量、无模块级缓存可复位）——任何测试都不写真 ~/.niu；
- 编码面 patch **源模块** `niu_api.internal.llama_server.encode_batch`：`search_media`
  内是函数级 `from niu_api.internal.llama_server import encode_batch`（调用时从源模块
  重新解析属性）⇒ patch 源模块即生效，模块无级联 import；
- 节点解析 patch `niu_lightrag_server._get_adapter` 返回伪 adapter：其
  `get_entity_info(entity_name)` 返回与真实 `LightRAGAdapter.get_entity_info`
  逐字同型的 `{"status":"ok","data":{...,"graph_data":{file_path,description}}}` /
  `{"status":"error","message":...}`（真实形状亲验 `lightrag_adapter.py:1857–1881`）。
- 媒体库用**真实** `media_vectors.add/search`（临时 JSON 库、三参写入），不打桩 ——
  守 I-4/I-5/D-13：计分与排序只发生在媒体库内部，返回项不得混入知识检索的任何字段。
"""

import importlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# 让 lightrag-server 模块可导入（其源码在 mcp-servers/lightrag-server/src 下）
_SRC = str(Path(__file__).resolve().parent.parent / "mcp-servers" / "lightrag-server" / "src")

from niu_api.internal import media_vectors  # noqa: E402  真实媒体库（三键形态，S1 交付）

DIM = media_vectors.DIM


def _vec(*pairs) -> list[float]:
    """由 (下标, 值) 对构造 DIM 维向量（缺省全 0）。"""
    v = [0.0] * DIM
    for idx, val in pairs:
        v[idx] = val
    return v


@pytest.fixture()
def srv(tmp_path, monkeypatch):
    """lightrag-server 模块（真实 niu_api 面、每次 reload）+ 媒体库重定向临时目录。"""
    monkeypatch.setenv("NIU_MEDIA_STORE", str(tmp_path / "media_vectors.json"))
    if _SRC not in sys.path:
        sys.path.insert(0, _SRC)
    import niu_lightrag_server

    importlib.reload(niu_lightrag_server)
    return niu_lightrag_server


def _stub_encode(monkeypatch, vector: list[float]):
    """文本编码打桩：任意条输入 → 逐条同向量（返回与输入等长，契约一致）。"""
    mock = MagicMock(side_effect=lambda texts: [vector for _ in texts])
    monkeypatch.setattr("niu_api.internal.llama_server.encode_batch", mock)
    return mock


class _FakeAdapter:
    """伪 adapter：get_entity_info 按实体名返回罐头形状（或抛异常）。

    返回形状与真实 `LightRAGAdapter.get_entity_info` 逐字同型：
    成功 `{"status":"ok","data":{...,"graph_data":{...}}}`；
    LightRAG 不可用 / 异常 `{"status":"error","message":...}`（真实实现不抛，返 error 体）。
    """

    def __init__(self, resolver):
        self._resolver = resolver

    def get_entity_info(self, entity_name, include_vector_data=False, timeout=30):
        return self._resolver(entity_name)


def _install_adapter(srv, monkeypatch, resolver):
    fake = _FakeAdapter(resolver)
    monkeypatch.setattr(srv, "_get_adapter", lambda: fake)
    return fake


def _node_ok(file_path, description):
    """节点在且带位置/描述字段（真实 graph_data 形状）。"""
    return {"status": "ok", "data": {"graph_data": {"file_path": file_path, "description": description}}}


def _node_error():
    """节点缺失 / LightRAG 不可用（真实 adapter 返 error 体、不抛）。"""
    return {"status": "error", "message": "LightRAG not available"}


def _node_no_location():
    """节点在但 graph_data 无位置/描述字段。"""
    return {"status": "ok", "data": {"graph_data": {"entity_type": "Photo"}}}


def _seed3():
    """写入三条正交/斜交向量：查询向量 (0.9, 0.1) 与 sunset 最近。"""
    media_vectors.add("sunset", "image", _vec((0, 1.0)))
    media_vectors.add("beach", "image", _vec((1, 1.0)))
    media_vectors.add("mountain", "image", _vec((0, 0.7071), (1, 0.7071)))


# ---------------------------------------------------------------------------
# ① 最近邻排首位 + 节点在 ⇒ file_path/description 有值、无 degraded 键
# ---------------------------------------------------------------------------


def test_01_nearest_hit_first_with_node_fields(srv, monkeypatch):
    """① 写入 3 条（三参 add）→ 查询向量与 sunset 最近 ⇒ sunset 排首位；
    命中项含 entity_name/media_type/score/file_path/description；节点在（伪 adapter 返
    ok+graph_data）⇒ file_path/description 有值、**无** degraded 键（命中不丢）。"""
    _seed3()
    _stub_encode(monkeypatch, _vec((0, 0.9), (1, 0.1)))
    _install_adapter(
        srv,
        monkeypatch,
        lambda name: _node_ok(f"/w/{name}.jpg", f"描述-{name}"),
    )

    r = srv.search_media("日落")
    assert r["status"] == "ok"
    names = [item["entity_name"] for item in r["results"]]
    assert names == ["sunset", "mountain", "beach"], "最近的一条必须排首位，余按余弦降序"
    top = r["results"][0]
    assert set(top) == {"entity_name", "media_type", "score", "file_path", "description"}
    assert top["media_type"] == "image"
    assert top["file_path"] == "/w/sunset.jpg", "路径查询时从图谱节点解析（D-23），不是库内复制"
    assert top["description"] == "描述-sunset"
    assert isinstance(top["score"], float)
    assert "degraded" not in top, "节点在 ⇒ 无降级标记"


# ---------------------------------------------------------------------------
# ② 节点缺失 / 不可用 / 异常 ⇒ 命中不丢 + 空值 + degraded 标记（不整体报错）
# ---------------------------------------------------------------------------


def test_02a_node_error_status_degraded_hit_kept(srv, monkeypatch):
    """② 伪 adapter 一律返 error 体（节点缺失 / LightRAG 不可用）⇒ 命中项**仍返回**
    （不丢、不整体报错），file_path==""、description==""、degraded is True。"""
    _seed3()
    _stub_encode(monkeypatch, _vec((0, 0.9), (1, 0.1)))
    _install_adapter(srv, monkeypatch, lambda name: _node_error())

    r = srv.search_media("日落")
    assert r["status"] == "ok", "节点缺失不得整体报错（R17）"
    assert len(r["results"]) == 3, "命中不丢"
    for item in r["results"]:
        assert item["file_path"] == ""
        assert item["description"] == ""
        assert item["degraded"] is True, "可辨降级标记（D-23）"


def test_02b_node_without_location_fields_degraded(srv, monkeypatch):
    """② 节点在但 graph_data 无位置/描述字段 ⇒ 同样按降级处理（空值 + 标记，可辨）。"""
    _seed3()
    _stub_encode(monkeypatch, _vec((0, 0.9), (1, 0.1)))
    _install_adapter(srv, monkeypatch, lambda name: _node_no_location())

    r = srv.search_media("日落")
    assert r["status"] == "ok"
    assert len(r["results"]) == 3
    for item in r["results"]:
        assert item["file_path"] == "" and item["description"] == ""
        assert item["degraded"] is True


def test_02c_adapter_raises_degraded_not_fatal(srv, monkeypatch):
    """② adapter 调用抛异常（除分支）⇒ 同样降级，不炸出工具。"""

    def _boom(name):
        raise RuntimeError("graph backend down")

    _seed3()
    _stub_encode(monkeypatch, _vec((0, 0.9), (1, 0.1)))
    _install_adapter(srv, monkeypatch, _boom)

    r = srv.search_media("日落")
    assert r["status"] == "ok"
    assert all(item["degraded"] is True for item in r["results"])
    assert all(item["file_path"] == "" for item in r["results"])


def test_02d_mixed_nodes_degraded_is_per_item(srv, monkeypatch):
    """② 混合：sunset 节点在、其余缺失 ⇒ 逐命中项独立标记；整体仍 ok、命中不丢。"""
    _seed3()
    _stub_encode(monkeypatch, _vec((0, 0.9), (1, 0.1)))
    _install_adapter(
        srv,
        monkeypatch,
        lambda name: _node_ok("/w/sunset.jpg", "日落时分的海滩") if name == "sunset" else _node_error(),
    )

    r = srv.search_media("日落")
    assert r["status"] == "ok"
    by_name = {item["entity_name"]: item for item in r["results"]}
    assert len(by_name) == 3
    assert by_name["sunset"]["file_path"] == "/w/sunset.jpg"
    assert "degraded" not in by_name["sunset"]
    for name in ("mountain", "beach"):
        assert by_name[name]["file_path"] == ""
        assert by_name[name]["degraded"] is True


# ---------------------------------------------------------------------------
# ③ top_k 生效 + 入口守卫（非法 top_k / 空 query）+ 上界 50
# ---------------------------------------------------------------------------


def test_03a_top_k_limits_results(srv, monkeypatch):
    """③ top_k=1 ⇒ 只返回 1 条。"""
    _seed3()
    _stub_encode(monkeypatch, _vec((0, 0.9), (1, 0.1)))
    _install_adapter(srv, monkeypatch, lambda name: _node_ok(f"/w/{name}.jpg", "d"))

    r = srv.search_media("日落", top_k=1)
    assert r["status"] == "ok"
    assert len(r["results"]) == 1
    assert r["results"][0]["entity_name"] == "sunset"


def test_03b_invalid_top_k_is_error_not_storage(srv, monkeypatch):
    """③ top_k 非正整数（0/负数/不可转整数）⇒ status:error（报文钉死），
    且不得误报成存储面「库文件损坏」。"""
    _seed3()
    _stub_encode(monkeypatch, _vec((0, 0.9), (1, 0.1)))

    for bad in (0, -3, "abc", None):
        r = srv.search_media("日落", top_k=bad)
        assert r["status"] == "error", f"top_k={bad!r} 必须被入口守卫拦下"
        assert r["message"] == "top_k 必须是正整数"
        assert "损坏" not in r["message"], "不得误报成存储面库文件损坏"


def test_03c_empty_or_blank_query_is_error(srv, monkeypatch):
    """③ 空/全空白 query ⇒ status:error「query 不能为空」，编码一步都不进
    （桩零调用 ⇒ 退化向量无从污染命中）。"""
    _seed3()
    mock = _stub_encode(monkeypatch, _vec((0, 1.0)))

    for bad in ("", "   ", "\t\n "):
        r = srv.search_media(bad)
        assert r["status"] == "error", f"query={bad!r} 必须被入口守卫拦下"
        assert r["message"] == "query 不能为空"
        assert set(r) == {"status", "message"}
    mock.assert_not_called()


def test_03d_top_k_capped_at_50(srv, monkeypatch):
    """③ 巨大 top_k 按 min(top_k, 50) 封顶——不得把全库灌进 LLM。"""
    for i in range(52):
        media_vectors.add(f"p{i:02d}", "image", _vec((i, 1.0)))
    _stub_encode(monkeypatch, _vec((0, 1.0)))
    _install_adapter(srv, monkeypatch, lambda name: _node_ok(f"/w/{name}.jpg", "d"))

    r = srv.search_media("日落", top_k=99)
    assert r["status"] == "ok"
    assert len(r["results"]) == 50


# ---------------------------------------------------------------------------
# ④ 库空 → ok + 空结果；编码面失败 → status:error 报文含「文本编码失败」
# ---------------------------------------------------------------------------


def test_04a_empty_store_ok_empty_results(srv, monkeypatch):
    """④ 库空（文件不存在）⇒ {"status":"ok","results":[]}——回退条件只有"库空"，
    不算错误、不抛。"""
    _stub_encode(monkeypatch, _vec((0, 1.0)))
    _install_adapter(srv, monkeypatch, lambda name: _node_ok("/w/x.jpg", "d"))

    r = srv.search_media("任意查询")
    assert r["status"] == "ok"
    assert r["results"] == []


def test_04b_encode_failure_is_error_not_silent_empty(srv, monkeypatch):
    """④ encode_batch 抛错（llama-server 未就绪）⇒ status:error 且报文含「文本编码失败」：
    宽捕获只包编码那一步，编码面异常不得静默成空结果；错误形状钉死为 {status, message}。"""
    _seed3()
    monkeypatch.setattr(
        "niu_api.internal.llama_server.encode_batch",
        MagicMock(side_effect=RuntimeError("llama-server 未就绪（health 检查失败）")),
    )

    r = srv.search_media("日落")
    assert r["status"] == "error"
    assert set(r) == {"status", "message"}
    assert "文本编码失败" in r["message"]
    assert "存储" not in r["message"], "编码面失败不得误报成存储面"


# ---------------------------------------------------------------------------
# ⑤ 独立计分（AC-10）：返回项键集合无知识检索字段；查询路径不得以就绪态门控
# ---------------------------------------------------------------------------


def test_05_independent_scoring_no_readiness_gate(srv, monkeypatch):
    """⑤ 返回项键集合断言：只可能有媒体库自身字段（+ 降级标记），
    不得出现知识检索的任何分数/置信度/关系字段（I-4/D-13，AC-10）；
    media_ready 桩成哑弹（AssertionError）：查询路径碰它即红——
    文本编码不需要 mmproj（encode_batch 纯文本），未加载 mmproj 时库里已有向量
    仍可被文本命中（行为断言，非接线断言）。"""
    media_vectors.add("sunset", "image", _vec((0, 1.0)))
    _stub_encode(monkeypatch, _vec((0, 1.0)))
    ready_mock = MagicMock(side_effect=AssertionError("media_ready 不得用作查询门控"))
    monkeypatch.setattr("niu_api.internal.llama_server.media_ready", ready_mock)
    _install_adapter(srv, monkeypatch, lambda name: _node_ok("/w/sunset.jpg", "日落时分的海滩"))

    r = srv.search_media("日落")
    assert r["status"] == "ok"
    assert len(r["results"]) == 1, "无 mmproj 不得清空有效命中"
    hit = r["results"][0]
    assert set(hit) <= {"entity_name", "media_type", "score", "file_path", "description", "degraded"}
    assert not any(k in hit for k in ("confidence", "similarity", "relation", "relationship", "entities", "chunks", "timeline")), "不得混入知识检索的任何得分/置信度/关系字段"
    assert hit["entity_name"] == "sunset"
    assert hit["file_path"] == "/w/sunset.jpg"
    assert "degraded" not in hit


# ---------------------------------------------------------------------------
# ⑥ 旧形态库文件（含 file_path 键的条目）可被正常读取与命中（D-23 读侧容忍）
# ---------------------------------------------------------------------------


def test_06_legacy_store_file_tolerated_on_read(srv, monkeypatch):
    """⑥ v2.x 旧形态库文件（条目含 file_path 键）被手写成磁盘文件 ⇒ 查询照常命中
    （读侧容忍多余字段）；命中项的 file_path 来自**图谱节点解析**，
    不是库文件里那份会漂移的旧副本（D-23 不复制）。"""
    legacy = {
        "dim": DIM,
        "entries": {
            "legacy_photo": {
                "entity_name": "legacy_photo",
                "media_type": "image",
                "vector": _vec((0, 1.0)),
                "file_path": "/w/legacy-old-copy.jpg",  # v2.x 残留字段
            },
        },
    }
    store_path = media_vectors.media_store_path()
    store_path.write_text(json.dumps(legacy), encoding="utf-8")

    _stub_encode(monkeypatch, _vec((0, 1.0)))
    _install_adapter(srv, monkeypatch, lambda name: _node_ok("/w/legacy-resolved.jpg", "旧照片"))

    r = srv.search_media("旧照片")
    assert r["status"] == "ok"
    assert len(r["results"]) == 1
    hit = r["results"][0]
    assert hit["entity_name"] == "legacy_photo"
    assert hit["file_path"] == "/w/legacy-resolved.jpg", "路径只从节点取，不用库文件里的旧副本"
    assert hit["description"] == "旧照片"
    assert "degraded" not in hit
    assert set(hit) == {"entity_name", "media_type", "score", "file_path", "description"}


# ---------------------------------------------------------------------------
# ⑦ 两面齐全：TOOL_SCHEMAS 条目 + _TOOL_FUNCTIONS/call_tool 分派（本模块无 stdio 面）
# ---------------------------------------------------------------------------


def test_07_both_faces_present_and_dispatched(srv, monkeypatch):
    """⑦ `search_media` 同时存在于同进程 TOOL_SCHEMAS（供 ToolRegistry）与
    _TOOL_FUNCTIONS（call_tool 名分派）——本模块**无** stdio Tool() 副本面（A12），
    这两面就是全部同进程面；call_tool 分派后签名参数过滤生效。"""
    assert "search_media" in srv.TOOL_SCHEMAS
    schema = srv.TOOL_SCHEMAS["search_media"]
    assert schema["name"] == "search_media"
    assert "query" in schema["input_schema"]["required"]
    assert schema["input_schema"]["properties"]["top_k"]["default"] == 5
    assert schema["input_schema"]["properties"]["top_k"]["maximum"] == 50

    assert "search_media" in srv._TOOL_FUNCTIONS
    assert srv._TOOL_FUNCTIONS["search_media"] is srv.search_media

    _seed3()
    _stub_encode(monkeypatch, _vec((0, 0.9), (1, 0.1)))
    _install_adapter(srv, monkeypatch, lambda name: _node_ok(f"/w/{name}.jpg", "d"))

    r = srv.call_tool("search_media", {"query": "日落", "top_k": 2, "unknown_param": "x"})
    assert r["status"] == "ok"
    assert len(r["results"]) == 2
    assert r["results"][0]["entity_name"] == "sunset"


# ---------------------------------------------------------------------------
# ⑧（附加）存储面失败单独捕获：报文写明是存储面（与编码面报文可辨）
# ---------------------------------------------------------------------------


def test_08_storage_failure_message_distinguishes_encode_face(srv, monkeypatch, tmp_path):
    """⑧ 媒体库文件损坏（存储面）⇒ status:error 且报文写明存储面：
    与编码面报错（④）严格区分——两种失败不得互相误报（except 作用域各包一段）。"""
    store_path = media_vectors.media_store_path()
    store_path.write_text("{not valid json", encoding="utf-8")
    _stub_encode(monkeypatch, _vec((0, 1.0)))
    _install_adapter(srv, monkeypatch, lambda name: _node_ok("/w/x.jpg", "d"))

    r = srv.search_media("日落")
    assert r["status"] == "error"
    assert "存储面" in r["message"]
    assert "文本编码失败" not in r["message"]
