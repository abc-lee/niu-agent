"""T1：图谱窗口媒体检索面 GET /api/media/search（media_api.media_search）——端点函数直调行为断言。

同 test_search_media.py 的接缝纪律（本端点是同一语义的 HTTP 面，I-1 同口径）：
- 媒体库经 NIU_MEDIA_STORE 接缝重定向临时目录（media_store_path() 每次现读环境变量、无模块级
  缓存可复位）——任何用例都不写真 ~/.niu；真实库文件做前后快照断言（autouse，触碰真库即红）；
- 编码面 patch **源模块** niu_api.internal.llama_server.encode_batch：路由内是函数级
  from-import（调用时从源模块重新解析属性）⇒ patch 源模块即生效，无级联 import；
- 节点解析 patch 本模块单例 niu_api.media_api._adapter（_get_adapter 的持有位）返回伪 adapter：
  其 get_entity_info(entity_name) 返回与真实 LightRAGAdapter.get_entity_info 逐字同型的
  {"status":"ok","data":{...,"graph_data":{file_path,description}}} / {"status":"error","message":...}；
- 媒体库用**真实** media_vectors.add/search（临时 JSON 库、三键写入），不打桩——守 D-13：
  计分与排序只发生在媒体库内部，返回项不得混入知识检索的任何字段；
- 端点函数直调（media_search(query, top_k)，top_k 传字符串——路由声明为 str，I-1 同工具守卫）。

用例覆盖（§3.2 命令 2）：守卫（空 query / 非法 top_k）、top_k 上界 50、库空 ok、三形态节点解析
降级（缺字段/缺节点/adapter 异常 ⇒ degraded 且命中不丢）、编码面失败、存储面失败、非降级形状、
以及**只读面不拉起编码后端**（start() 零调用——I-1「不造第二套口径」的硬断言）。
"""

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import niu_api.media_api as media_api
from niu_api.media_api import media_search
from niu_api.internal import media_vectors
from niu_api.internal import llama_server

DIM = media_vectors.DIM


def _vec(*pairs) -> list[float]:
    """由 (下标, 值) 对构造 DIM 维向量（缺省全 0）。"""
    v = [0.0] * DIM
    for idx, val in pairs:
        v[idx] = val
    return v


class _FakeAdapter:
    """伪 adapter：get_entity_info 按罐头 shape 返回，或抛异常（三种降级形态）。"""

    def __init__(self, resolver):
        self._resolver = resolver

    def get_entity_info(self, entity_name):
        return self._resolver(entity_name)


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    """媒体库重定向临时目录 + 真实库前后快照（触碰真库即红）+ 编码面/节点解析打桩基座。"""
    real = media_vectors.media_store_path()
    try:
        before = (os.stat(real).st_mtime_ns, open(real, "rb").read())
    except OSError:
        before = None
    monkeypatch.setenv("NIU_MEDIA_STORE", str(tmp_path / "media_vectors.json"))
    # 编码面：任意文本 → 同一条查询向量 (0.9, 0.1)（与入库向量同空间；v1.1 F10：不再与入库向量共线，
    # cosine 按入库向量方向真正可控——三条共线会 cosine 全 1.0 并列、退化按 entity_name 升序）
    monkeypatch.setattr(
        "niu_api.internal.llama_server.encode_batch",
        MagicMock(side_effect=lambda texts: [_vec((0, 0.9), (1, 0.1))] * len(texts)),
    )
    # 只读面不得拉起编码后端：start() 一旦被调即红（I-1）
    monkeypatch.setattr(llama_server, "start", MagicMock())
    # 节点解析：缺省「节点在且有字段」形态；单测按用例覆盖
    monkeypatch.setattr(
        media_api, "_adapter",
        _FakeAdapter(lambda name: {
            "status": "ok",
            "data": {"graph_data": {"file_path": f"/tmp/{name}.jpg", "description": f"desc {name}"}},
        }),
    )
    yield
    after = None
    try:
        after = (os.stat(real).st_mtime_ns, open(real, "rb").read())
    except OSError:
        after = None
    assert after == before, "测试触碰了真实媒体库文件（须打桩 NIU_MEDIA_STORE）"


def _seed(names=("sunset", "mountain", "beach")):
    """真实写库：三条正交/斜交向量（对齐 tests/test_search_media.py 的 _seed3 先例）——查询向量 (0.9, 0.1)
    ⇒ cosine 按 sunset 0.9939 / mountain 0.7809 / beach 0.1104 降序（实测；0.9/0.707/0.1 是点积口径、非 cosine），排序真正可控（v1.1 F10：旧版三条共线
    ⇒ cosine 全 1.0 并列、退化按 entity_name 升序 ⇒ 排序断言必然红）。"""
    sunset, mountain, beach = names
    media_vectors.add_many([
        (sunset, "photo", _vec((0, 1.0))),
        (beach, "photo", _vec((1, 1.0))),
        (mountain, "photo", _vec((0, 0.7071), (1, 0.7071))),
    ])


# ---------------------------------------------------------------------------
# 守卫与上界（I-1：与 search_media 工具同守卫）
# ---------------------------------------------------------------------------

def test_guard_empty_query():
    for bad in ("", "   ", "\t\n "):
        r = media_search(bad, "20")
        assert r["status"] == "error"
        assert r["message"] == "query 不能为空"
        assert set(r) == {"status", "message"}


def test_guard_bad_top_k():
    for bad in ("0", "-3", "abc", "1e3", ""):
        r = media_search("日落", bad)
        assert r["status"] == "error", f"top_k={bad!r} 必须被入口守卫拦下"
        assert r["message"] == "top_k 必须是正整数"
        assert "损坏" not in r["message"]


def test_top_k_plus_sign_and_trailing_space_accepted():
    # 实现经 int(top_k) 解析（media_api.media_search）："+3" 与 "20 "（尾随空白）均被 int() 接受
    # ⇒ 按正整数正常走检索，不被守卫拦下（若改实现为 isdigit() 口径，本用例会红）
    _seed()
    for good in ("+3", "20 "):
        r = media_search("日落", good)
        assert r["status"] == "ok", f"top_k={good!r} 按 int() 口径应被接受"
        assert r["results"]


def test_top_k_capped_at_50(monkeypatch):
    # 写入 60 条不同方向向量 ⇒ top_k=99 封顶为 50 条
    media_vectors.add_many([
        (f"n{i:03d}", "photo", _vec((i % DIM, 1.0))) for i in range(60)
    ])
    r = media_search("日落", "99")
    assert r["status"] == "ok"
    assert len(r["results"]) == 50
    # 超大整数 int() 可解析（Python 无界整数）⇒ 同样按 min(top_k, 50) 封顶（与 "99" 同形态，合并本用例）
    r = media_search("日落", "99999999999999999999")
    assert r["status"] == "ok"
    assert len(r["results"]) == 50


# ---------------------------------------------------------------------------
# 检索与降级形状（D-13/D-23）
# ---------------------------------------------------------------------------

def test_search_ranking_and_shape():
    _seed()
    r = media_search("日落", "3")
    assert r["status"] == "ok"
    names = [it["entity_name"] for it in r["results"]]
    # 查询向量 (0.9, 0.1)；三条正交/斜交 ⇒ cosine 0.9939 / 0.7809 / 0.1104 降序（实测；0.9/0.707/0.1 是点积口径、非 cosine）（v1.1 F10：不再共线、无并列，排序恒成立）
    assert names == ["sunset", "mountain", "beach"]
    hit = r["results"][0]
    assert set(hit) >= {"entity_name", "media_type", "score", "file_path", "description"}
    assert "degraded" not in hit, "节点在且有字段 ⇒ 不降级"
    assert hit["file_path"] == "/tmp/sunset.jpg"


def test_degraded_missing_fields():
    _seed()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(media_api, "_adapter", _FakeAdapter(
        lambda name: {"status": "ok", "data": {"graph_data": {}}}))
    try:
        r = media_search("日落", "3")
        assert r["status"] == "ok"
        assert len(r["results"]) == 3, "命中不丢"
        for it in r["results"]:
            assert it["degraded"] is True
            assert it["file_path"] == "" and it["description"] == ""
    finally:
        monkey.undo()


def test_degraded_missing_node():
    _seed()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(media_api, "_adapter", _FakeAdapter(
        lambda name: {"status": "error", "message": f"entity {name} not found"}))
    try:
        r = media_search("日落", "3")
        assert r["status"] == "ok", "节点缺失不得整体报错（R17）"
        for it in r["results"]:
            assert it["degraded"] is True
    finally:
        monkey.undo()


def test_degraded_adapter_exception():
    _seed()
    def boom(name):
        raise RuntimeError("adapter down")
    monkey = pytest.MonkeyPatch()
    monkey.setattr(media_api, "_adapter", _FakeAdapter(boom))
    try:
        r = media_search("日落", "3")
        assert r["status"] == "ok"
        assert all(it["degraded"] is True for it in r["results"])
    finally:
        monkey.undo()


def test_empty_store_ok():
    # 不 _seed：临时库文件不存在 ⇒ search 返回 []（不抛）
    r = media_search("任意查询", "20")
    assert r["status"] == "ok"
    assert r["results"] == []


def test_encode_failure_surfaces(monkeypatch):
    _seed()
    monkeypatch.setattr(
        "niu_api.internal.llama_server.encode_batch",
        MagicMock(side_effect=RuntimeError("llama-server 未就绪（health 检查失败）")),
    )
    r = media_search("日落", "20")
    assert r["status"] == "error"
    assert "媒体查询文本编码失败" in r["message"]
    assert "RuntimeError" in r["message"]


def test_store_failure_surfaces(monkeypatch):
    _seed()
    monkeypatch.setattr(media_vectors, "search", MagicMock(side_effect=OSError("库文件损坏")))
    r = media_search("日落", "20")
    assert r["status"] == "error"
    assert "媒体库检索失败（存储面" in r["message"]


# ---------------------------------------------------------------------------
# 只读面硬断言（I-1：不为窗口搜索拉起编码后端）
# ---------------------------------------------------------------------------

def test_readonly_never_starts_backend():
    _seed()
    media_search("日落", "20")
    assert llama_server.start.call_count == 0, "只读面不得调用 llama_server.start()"
