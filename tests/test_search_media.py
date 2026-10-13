"""T4：独立媒体检索工具 search_media — 错误形状 / 异常作用域 / 无 mmproj 不回退 / 字段集。

全 mock 面（不真实编码、不写真库）：
- 媒体库走 ``NIU_MEDIA_STORE`` 测试接缝（T2 定死）→ 指向 tmp_path 的
  autouse fixture；``media_vectors.media_store_path()`` 每次现读环境变量，
  无模块级文件缓存可复位。
- ``encode_batch`` / ``media_ready`` 的 patch 目标：与 T3 的 ``encode_media``
  **完全同型** —— photo-server 对 llama_server **无模块级 import**，
  ``search_media`` 内是函数级 ``from niu_api.internal.llama_server import
  encode_batch``（调用时从源模块重新解析属性）⇒ patch **源模块**
  ``niu_api.internal.llama_server.encode_batch`` / ``.media_ready`` 即生效。
- 媒体库用**真实** ``media_vectors.add/search``（临时 JSON 库），不打桩 ——
  守 I-4/I-5：排名只发生在媒体库内部，返回键即绑定键。

用例 ①–⑥ 逐条对应方案 Task 4 Step 1；⑦–⑧ 为入口守卫补充用例（空 query / top_k 归一化与上界）。
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# 让 niu_photo_server 模块可导入（其源码在 mcp-servers/photo-server/src 下）
_SRC = os.path.join(os.path.dirname(__file__), "..", "mcp-servers", "photo-server", "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import niu_photo_server  # noqa: E402
from niu_photo_server import media_vectors  # noqa: E402

DIM = media_vectors.DIM


def _vec(*pairs) -> list[float]:
    """由 (下标, 值) 对构造 DIM 维向量（缺省全 0）。"""
    v = [0.0] * DIM
    for idx, val in pairs:
        v[idx] = val
    return v


@pytest.fixture(autouse=True)
def media_env(tmp_path, monkeypatch):
    """媒体库重定向到临时文件（NIU_MEDIA_STORE 每次现读，无缓存可复位）。"""
    monkeypatch.setenv("NIU_MEDIA_STORE", str(tmp_path / "media_vectors.json"))
    yield tmp_path


def _stub_encode(monkeypatch, vector: list[float]):
    """文本编码打桩：任意条输入 → 逐条同向量（返回与输入等长，契约一致）。"""
    mock = MagicMock(side_effect=lambda texts: [vector for _ in texts])
    monkeypatch.setattr("niu_api.internal.llama_server.encode_batch", mock)
    return mock


# ---------------------------------------------------------------------------
# ① 最近邻排首位（只写媒体库、排名只在库内）
# ---------------------------------------------------------------------------


def test_01_nearest_hit_first(media_env, monkeypatch):
    """① 写入 3 条 → 查询向量与 sunset 最近 → sunset 排首位。"""
    media_vectors.add("sunset", "image", _vec((0, 1.0)), "/w/sunset.jpg")
    media_vectors.add("beach", "image", _vec((1, 1.0)), "/w/beach.jpg")
    media_vectors.add("mountain", "image", _vec((0, 0.7071), (1, 0.7071)), "/w/mountain.jpg")
    _stub_encode(monkeypatch, _vec((0, 0.9), (1, 0.1)))

    r = niu_photo_server.search_media("日落")
    assert r["status"] == "ok"
    assert r["results"][0]["entity_name"] == "sunset", "最近的一条必须排首位"
    assert len(r["results"]) == 3


# ---------------------------------------------------------------------------
# ② top_k 生效
# ---------------------------------------------------------------------------


def test_02_top_k_applied(media_env, monkeypatch):
    """② top_k=1 → 只返回 1 条。"""
    media_vectors.add("sunset", "image", _vec((0, 1.0)), "/w/sunset.jpg")
    media_vectors.add("beach", "image", _vec((1, 1.0)), "/w/beach.jpg")
    media_vectors.add("mountain", "image", _vec((0, 0.7071), (1, 0.7071)), "/w/mountain.jpg")
    _stub_encode(monkeypatch, _vec((0, 0.9), (1, 0.1)))

    r = niu_photo_server.search_media("日落", top_k=1)
    assert r["status"] == "ok"
    assert len(r["results"]) == 1
    assert r["results"][0]["entity_name"] == "sunset"


# ---------------------------------------------------------------------------
# ③ 空库 → ok + 空结果（回退条件只有"库空"，不抛）
# ---------------------------------------------------------------------------


def test_03_empty_store_ok_empty(media_env, monkeypatch):
    """③ 空库 → results == [] 且 status == ok（不抛、不误报编码面错误）。"""
    _stub_encode(monkeypatch, _vec((0, 1.0)))

    r = niu_photo_server.search_media("任意查询")
    assert r["status"] == "ok"
    assert r["results"] == []


# ---------------------------------------------------------------------------
# ④ 返回体字段集：恰 4 字段，无任何 LightRAG 分数/置信度/关系字段
# ---------------------------------------------------------------------------


def test_04_result_fields_exact_no_kg_fields(media_env, monkeypatch):
    """④ 每条结果恰为 {entity_name, media_type, file_path, score}：
    entity_name 必须保留（D-16 绑定键 + 图谱走边入口，不是 KG 计分字段）；
    不得出现 LightRAG 的任何分数/置信度/关系字段（I-4）。"""
    media_vectors.add("sunset", "image", _vec((0, 1.0)), "/w/sunset.jpg")
    _stub_encode(monkeypatch, _vec((0, 0.9), (1, 0.1)))

    r = niu_photo_server.search_media("日落")
    assert r["status"] == "ok"
    assert len(r["results"]) == 1
    hit = r["results"][0]
    assert set(hit) == {"entity_name", "media_type", "file_path", "score"}
    assert hit["entity_name"] == "sunset"
    assert hit["media_type"] == "image"
    assert hit["file_path"] == "/w/sunset.jpg"
    assert isinstance(hit["score"], float)


# ---------------------------------------------------------------------------
# ⑤ 库非空但没挂 mmproj（media_ready 桩成哑弹）→ 仍返回命中
# ---------------------------------------------------------------------------


def test_05_no_mmproj_still_returns_hits(media_env, monkeypatch):
    """⑤ 查询走 encode_batch（纯文本，不需 mmproj），媒体库落盘独立文件 ⇒
    无 mmproj 只挡入库时的图片编码，不得清空查询结果。

    media_ready 桩成哑弹（side_effect=AssertionError）：实现任何路径碰它——无论
    当查询门控还是当探针——异常要么落入实现侧宽捕获被转成 status: error（下方
    status==ok 断言红），要么直接炸穿测试；碰了必红，不碰才绿。这是行为断言，
    不是接线断言（assert_called() 只证明桩被碰过，不证明行为）。

    残余局限（如实，不夸大）：本用例守的是函数级 import 形态——patch 目标是源模块
    niu_api.internal.llama_server.media_ready，只有调用时从源模块重新解析属性才踩
    得到哑弹；若日后回归成模块级 import（import 时即绑定本地名），patch 变 no-op，
    本用例永不触发、会假绿。该残余窗口不做机制封堵，靠实现里「零 media_ready
    调用」的读 diff 人工保证。"""
    media_vectors.add("sunset", "image", _vec((0, 1.0)), "/w/sunset.jpg")
    _stub_encode(monkeypatch, _vec((0, 1.0)))
    ready_mock = MagicMock(side_effect=AssertionError("media_ready 不得用作查询门控"))
    monkeypatch.setattr("niu_api.internal.llama_server.media_ready", ready_mock)

    r = niu_photo_server.search_media("日落")
    assert r["status"] == "ok"
    assert len(r["results"]) == 1, "无 mmproj 不得清空有效命中"
    assert r["results"][0]["entity_name"] == "sunset"


# ---------------------------------------------------------------------------
# ⑥ 库非空 + encode_batch 抛异常 → status: error（message 非空），不静默成空结果
# ---------------------------------------------------------------------------


def test_06_encode_failure_is_error(media_env, monkeypatch):
    """⑥ llama-server 未起时 encode_batch 先抛 RuntimeError（走不到 urlopen）
    ⇒ 宽捕获转工具错误：错误形状钉死为 {status: error, message}（message 非空、
    无 reason 字段 —— 跟随本模块 __init__.py:63 的既有错误约定）。"""
    media_vectors.add("sunset", "image", _vec((0, 1.0)), "/w/sunset.jpg")
    monkeypatch.setattr(
        "niu_api.internal.llama_server.encode_batch",
        MagicMock(side_effect=RuntimeError("llama-server 未就绪（health 检查失败）")),
    )

    r = niu_photo_server.search_media("日落")
    assert r["status"] == "error"
    assert set(r) == {"status", "message"}, "错误形状必须钉死为 {status, message}"
    assert r["message"], "message 必须非空（机械原因）"

# ---------------------------------------------------------------------------
# ⑦ 入口守卫：空/全空白 query → status: error，不进编码（桩不得被调用）
# ---------------------------------------------------------------------------


def test_07_empty_or_blank_query_is_error(media_env, monkeypatch):
    """⑦ 空串/全空白 query 无守卫时 encode_batch 照跑空串 → 退化向量 →
    status:ok 回 top_k 条噪声命中（静默错答案）。有守卫：status: error、
    message 钉死「query 不能为空」，且编码一步都不进（桩零调用 ⇒
    退化向量无从污染命中）。"""
    media_vectors.add("sunset", "image", _vec((0, 1.0)), "/w/sunset.jpg")
    mock = _stub_encode(monkeypatch, _vec((0, 1.0)))

    for bad in ("", "   ", "\t\n "):
        r = niu_photo_server.search_media(bad)
        assert r["status"] == "error", f"query={bad!r} 必须被入口守卫拦下"
        assert r["message"] == "query 不能为空"
        assert set(r) == {"status", "message"}
    mock.assert_not_called()


# ---------------------------------------------------------------------------
# ⑧ 入口守卫：top_k 归一化（非正整数 → error，不得误报存储面）与上界 min(top_k, 50)
# ---------------------------------------------------------------------------


def test_08_top_k_guard_and_cap(media_env, monkeypatch):
    """⑧ top_k 不能转为整数或 <1 → status: error（message 钉死「top_k 必须是正整数」）：
    字符串 top_k 的 TypeError 不得落入存储面 except 被误报成"库文件可能损坏"；
    可转整数则按 min(top_k, 50) 封顶 —— 巨大值不得返回全库灌进 LLM。"""
    for i in range(52):
        media_vectors.add(f"p{i:02d}", "image", _vec((i, 1.0)), f"/w/p{i:02d}.jpg")
    _stub_encode(monkeypatch, _vec((0, 1.0)))

    for bad in (0, -3, "abc", None):
        r = niu_photo_server.search_media("日落", top_k=bad)
        assert r["status"] == "error", f"top_k={bad!r} 必须被入口守卫拦下"
        assert r["message"] == "top_k 必须是正整数"
        assert "损坏" not in r["message"], "不得误报成存储面库文件损坏"

    r = niu_photo_server.search_media("日落", top_k=99)
    assert r["status"] == "ok"
    assert len(r["results"]) == 50, "上界 50：巨大 top_k 不得返回全库"
