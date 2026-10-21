"""AC-9（方案 Task 2 Step 6，spec D-12 / 不变量 I-3）：repair 不碰独立媒体库，
媒体向量不泄漏进 LightRAG 的任何存储。

断言两件事（缺一不可）：
1. 媒体库文件**逐字节不变**（repair 前后 sha256 相同）——repair/重嵌入路径对独立媒体库零感知；
2. repair 重建出的 ``vdb_chunks`` / ``vdb_entities`` / ``text_chunks`` 中**无媒体向量**
   （向量逐条比对 + 元数据/内容字符串扫描，双向都查）。

三套存储重定向缺一不可（绝不写真 ``~/.niu``，也绝不拷贝真实 ``~/.niu/lightrag_storage``）：
- **媒体库** → ``NIU_MEDIA_STORE`` 环境变量（T2 定死的接缝，见 media_vectors.media_store_path）；
- **照片库** → patch ``niu_photo_server.get_db_path`` + 复位模块级连接缓存
  ``_conn`` / ``_db_path_failed``（get_connection() 只在 _conn is None 时重连，不复位会
  让先前建立的连接指向真实库——tests/test_photo_server_model_path.py 的复位纪律）；
- **LightRAG 存储** → ``monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(tmp))``。
  ⚠️ 不能靠 patch ``Path.home``：``lightrag_repair.py:64`` 的 ``_STORAGE_DIR`` 是**模块导入期
  求值的模块级全局量**，patch home 对已绑定的全局量无效（``lightrag_repair.py:74-76`` 自述
  "唯一生效途径是直接 monkeypatch 模块属性"）。先例：tests/test_lightrag_repair_unit.py 与
  tests/test_lightrag_repair_v9_7_scenarios.py 的全部同款用例。

另外**必须** patch ``niu_api.internal.embedding.get_model``：未 patch 时 ``get_model()``
（embedding.py:203-209）会无条件调 ``llama_server.start()`` 自启一个**真** llama-server 子进程
（RSS ≥338MB，且不被测试收尾），即便 user-config 开关没开。先例：
tests/test_lightrag_repair_unit.py 的 ``_FakeEmbedModel``（:1622/:1667/:1713 等）。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

import numpy as np
import pytest

# 让 niu_photo_server 模块可导入（其源码在 mcp-servers/photo-server/src 下）
_SRC = os.path.join(os.path.dirname(__file__), "..", "mcp-servers", "photo-server", "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# 媒体库种子的**结构化**向量（周期斜坡，数值域在 [0,1) 之外）：
# repair 重建出的向量来自 _FakeEmbedModel 的 np.random.rand（[0,1) 逐元素独立同分布），
# 一条 768 维随机行逐元素 allclose 到固定斜坡图案的概率 ~ 1e-1200，泄漏判定不会假绿。
MEDIA_ENTITY = "IMG_20260101_120000"
MEDIA_ENTITY_2 = "IMG_20260102_093000"
MEDIA_PATH_1 = "/tmp/niu-test/IMG_20260101_120000.jpg"
MEDIA_PATH_2 = "/tmp/niu-test/IMG_20260102_093000.jpg"
_MEDIA_VEC_1 = [2.5 + (i % 8) * 0.25 for i in range(768)]
_MEDIA_VEC_2 = [1.0 + (i % 5) * 1.0 for i in range(768)]
# 泄漏扫描的标记集：文件路径 + 实体名（绑定键，D-16）
_MEDIA_TOKENS = (MEDIA_PATH_1, MEDIA_PATH_2, MEDIA_ENTITY, MEDIA_ENTITY_2)


@pytest.fixture(autouse=True)
def _redirect_media_store(tmp_path, monkeypatch):
    """兜底接缝（与 tests/test_media_vectors.py 同名同款 fixture）：强制媒体库落临时目录。

    测试体内自己的 ``monkeypatch.setenv("NIU_MEDIA_STORE", ...)`` 可覆盖本 fixture 的值
    （teardown 按相反顺序恢复）；兜底目的：任何忘记设 env 的用例也不会碰真实 ``~/.niu``。
    """
    monkeypatch.setenv("NIU_MEDIA_STORE", str(tmp_path / "media_store.json"))


class _FakeEmbedModel:
    """假 embedding 模型（与 tests/test_lightrag_repair_unit.py 的同名类同形态）。"""

    def __init__(self, dim: int = 768):
        self.dim = dim
        self._call_count = 0

    def encode(self, texts, **kwargs):  # type: ignore[no-untyped-def]
        self._call_count += 1
        return np.random.rand(len(texts), self.dim).astype(np.float32)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _normalized(v: list[float]) -> np.ndarray:
    a = np.asarray(v, dtype=np.float32)
    n = np.linalg.norm(a)
    return a / n if n > 0 else a


def _decode_vdb_vector(entry: dict) -> np.ndarray | None:
    """解压 data[].vector（base64(zlib(float16))）→ float32 L2 归一化向量。

    语义同 niu_api.internal.lightrag_integrity._decode_vdb_vector（本地复刻，避免耦合私有 API）。
    """
    try:
        raw = base64.b64decode(entry["vector"])
        vec = np.frombuffer(zlib.decompress(raw), dtype=np.float16).astype(np.float32)
        norm = np.linalg.norm(vec)
        if not np.isfinite(norm) or norm <= 0:
            return None
        return vec / norm
    except Exception:  # noqa: BLE001
        return None


def _scan_vdb_file_for_media_leak(path: Path) -> str | None:
    """扫 vdb 文件：matrix 行 + data[].vector 逐条比对媒体向量，再扫全部元数据字符串。

    返回泄漏描述（断言失败文案）；无泄漏 → None。
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    dim = data.get("embedding_dim") or 768
    norm_media = [_normalized(_MEDIA_VEC_1), _normalized(_MEDIA_VEC_2)]

    matrix = data.get("matrix")
    if matrix:
        rows = np.frombuffer(base64.b64decode(matrix), dtype=np.float32).reshape(-1, dim)
        for row in rows:
            if any(np.allclose(row, m, atol=2e-3) for m in norm_media):
                return f"{path.name}: matrix 行与某条媒体向量逐元素相等（float16 容差内）"

    for entry in data.get("data", []) or []:
        for key, value in entry.items():
            if isinstance(value, str) and any(tok in value for tok in _MEDIA_TOKENS):
                return f"{path.name}: data 条目 {entry.get('__id__')} 的 {key} 含媒体标记: {value[:80]}"
        vec = _decode_vdb_vector(entry)
        if vec is not None and any(np.allclose(vec, m, atol=2e-3) for m in norm_media):
            return f"{path.name}: data 条目 {entry.get('__id__')} 的向量与某条媒体向量相等"
    return None


def _scan_text_chunks_for_media_leak(path: Path) -> str | None:
    """text_chunks 是纯文本 KV（无向量字段）：逐条扫描 content / full_doc_id / 所有字符串值。"""
    if not path.exists():
        return None  # 未重建 = 无内容可泄漏
    data = json.loads(path.read_text(encoding="utf-8"))
    for chunk_id, entry in data.items():
        if not isinstance(entry, dict):
            continue
        for key, value in entry.items():
            if isinstance(value, str) and any(tok in value for tok in _MEDIA_TOKENS):
                return f"kv_store_text_chunks.json: 条目 {chunk_id} 的 {key} 含媒体标记: {value[:80]}"
    return None


def _make_synthetic_lightrag_fixture(tmp_storage: Path) -> None:
    """自包含 LightRAG 存储夹具（**绝不**拷贝真实 ~/.niu/lightrag_storage）。

    形态与 tests/test_lightrag_repair_v9_7_scenarios.py::_make_synthetic_fixture 一致：
    GraphML 2 实体 + 1 边；full_docs 2 文档（content 经真实 compute_mdhash_id 算出与图谱
    source_id 一致的 chunk_id，保证 repair_text_chunks 的 full_docs 反查能命中）；
    cache 2 条 extract 条目；派生文件不预建（repair_all 先删后重建）。
    """
    from lightrag.utils import compute_mdhash_id

    doc_1_content = "AC9 fixture document one: synthetic landscape photo note"
    doc_2_content = "AC9 fixture document two: synthetic portrait photo note"
    chunk_id_1 = compute_mdhash_id(doc_1_content, prefix="chunk-")
    chunk_id_2 = compute_mdhash_id(doc_2_content, prefix="chunk-")

    ns = "http://graphml.graphdrawing.org/xmlns"
    root = ET.Element(f"{{{ns}}}graphml")
    for kid, attr_name, attr_type in [
        ("d1", "entity_type", "string"),
        ("d2", "description", "string"),
        ("d3", "source_id", "string"),
        ("d7", "weight", "double"),
        ("d8", "description", "string"),
        ("d9", "keywords", "string"),
        ("d10", "source_id", "string"),
    ]:
        ET.SubElement(
            root,
            f"{{{ns}}}key",
            {"id": kid, "for": "all", "attr.name": attr_name, "attr.type": attr_type},
        )
    graph = ET.SubElement(root, f"{{{ns}}}graph", {"edgedefault": "undirected"})

    a = ET.SubElement(graph, f"{{{ns}}}node", {"id": "synthetic-photo"})
    ET.SubElement(a, f"{{{ns}}}data", {"key": "d2"}).text = "synthetic photo entity"
    ET.SubElement(a, f"{{{ns}}}data", {"key": "d3"}).text = chunk_id_1

    b = ET.SubElement(graph, f"{{{ns}}}node", {"id": "synthetic-place"})
    ET.SubElement(b, f"{{{ns}}}data", {"key": "d2"}).text = "synthetic place entity"
    ET.SubElement(b, f"{{{ns}}}data", {"key": "d3"}).text = chunk_id_2

    edge = ET.SubElement(graph, f"{{{ns}}}edge", {"source": "synthetic-photo", "target": "synthetic-place"})
    ET.SubElement(edge, f"{{{ns}}}data", {"key": "d7"}).text = "0.5"
    ET.SubElement(edge, f"{{{ns}}}data", {"key": "d8"}).text = "edge desc"
    ET.SubElement(edge, f"{{{ns}}}data", {"key": "d9"}).text = "keyword1"
    ET.SubElement(edge, f"{{{ns}}}data", {"key": "d10"}).text = f"{chunk_id_1}<SEP>{chunk_id_2}"

    ET.ElementTree(root).write(
        tmp_storage / "graph_chunk_entity_relation.graphml",
        xml_declaration=True,
        encoding="utf-8",
    )

    (tmp_storage / "kv_store_full_docs.json").write_text(
        json.dumps(
            {
                "doc-1": {"content": doc_1_content, "file_path": "v1.md", "create_time": 1000},
                "doc-2": {"content": doc_2_content, "file_path": "v2.md", "create_time": 2000},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    (tmp_storage / "kv_store_llm_response_cache.json").write_text(
        json.dumps(
            {
                "default:extract:chunk1": {
                    "return": "entity<|#|>synthetic-photo<|#|>object<|#|>desc a",
                    "cache_type": "extract",
                    "chunk_id": chunk_id_1,
                    "create_time": 1500,
                },
                "default:extract:chunk2": {
                    "return": "entity<|#|>synthetic-place<|#|>location<|#|>desc b",
                    "cache_type": "extract",
                    "chunk_id": chunk_id_2,
                    "create_time": 1500,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


async def test_repair_isolates_media_store_and_never_leaks_media_vectors(tmp_path, monkeypatch):
    """跑完整 repair_all 链：媒体库逐字节不变，vdb_chunks/vdb_entities/text_chunks 无媒体向量。"""
    # ── 重定向 1：媒体库 → NIU_MEDIA_STORE（T2 接缝）──────────────────────────
    media_store = tmp_path / "media_store.json"
    monkeypatch.setenv("NIU_MEDIA_STORE", str(media_store))

    # ── 重定向 2：照片库 → get_db_path + 复位连接缓存 ────────────────────────
    import niu_photo_server

    monkeypatch.setattr(niu_photo_server, "get_db_path", lambda: tmp_path / "photos.db")
    monkeypatch.setattr(niu_photo_server, "_conn", None)
    monkeypatch.setattr(niu_photo_server, "_db_path_failed", False)

    # ── 重定向 3：LightRAG 存储 → 模块属性直接 patch（导入期全局量，patch home 无效）──
    tmp_storage = tmp_path / "lightrag_storage"
    tmp_storage.mkdir()
    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", str(tmp_storage))
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", str(tmp_storage))

    # ── 重定向 4（必须）：嵌入模型 → _FakeEmbedModel（防 get_model 无条件自启真 llama-server）──
    from niu_api.internal import embedding as niu_embedding

    fake_model = _FakeEmbedModel(dim=768)
    monkeypatch.setattr(niu_embedding, "get_model", lambda: fake_model)

    # ── 种子：LightRAG 自包含夹具 + 媒体库两条向量 ───────────────────────────
    _make_synthetic_lightrag_fixture(tmp_storage)

    from niu_api.internal import media_vectors

    media_vectors.add(MEDIA_ENTITY, "image", _MEDIA_VEC_1)
    media_vectors.add(MEDIA_ENTITY_2, "image", _MEDIA_VEC_2)
    assert media_vectors.count() == 2

    media_sha_before = _sha256(media_store)

    # ── 跑完整 repair 链（9 个派生文件全删全重建）────────────────────────────
    from niu_api.internal.lightrag_repair import _repair_all_async

    result = await _repair_all_async()

    # ── 断言 1：repair 正常跑完（夹具形态与 v9.7 场景 2 一致，应无 unrecoverable）──
    assert not result.get("_unrecoverable", False), (
        f"repair 报 unrecoverable（夹具应完好）: {result.get('_unrecoverable_reason', '')}"
    )
    # 嵌入必须走桩（若 get_model 未被 patch，_embed_batch 会走真 llama_server 或返回 None，
    # 重建出的 vdb 向量就不是本测试比对基准——此断言保证泄漏判定基准成立）
    assert fake_model._call_count > 0, "repair 链未调用嵌入桩（get_model 可能未生效）"

    # ── 断言 2：媒体库文件逐字节不变（repair 对独立媒体库零感知）──────────────
    assert _sha256(media_store) == media_sha_before, (
        "媒体库文件在 repair 后被改动（违反 D-12/I-3：repair 与重嵌入路径不得碰独立媒体库）"
    )
    assert media_vectors.count() == 2
    assert media_vectors.has(MEDIA_ENTITY) and media_vectors.has(MEDIA_ENTITY_2)

    # ── 断言 3：vdb_chunks / vdb_entities 无媒体向量（向量 + 元数据双向扫描）──
    for fname in ("vdb_chunks.json", "vdb_entities.json"):
        path = tmp_storage / fname
        assert path.exists() and path.stat().st_size > 0, (
            f"{fname} 应被 repair 重建（夹具 GraphML 有数据）——未重建意味着本测试的泄漏判定是空断言"
        )
        leak = _scan_vdb_file_for_media_leak(path)
        assert leak is None, f"媒体向量/标记泄漏进 {fname}: {leak}"

    # ── 断言 4：text_chunks 无媒体向量（纯文本 KV，扫描全部字符串值）──────────
    tc_path = tmp_storage / "kv_store_text_chunks.json"
    assert tc_path.exists(), "kv_store_text_chunks.json 应被 repair 重建"
    leak = _scan_text_chunks_for_media_leak(tc_path)
    assert leak is None, f"媒体标记泄漏进 text_chunks: {leak}"
