"""S10 repair 双口径重切测试（方案 §5 第 2/3/4 条，全 mock：禁真实 LLM、禁写 KG）。

覆盖（逐条对应方案 §5）：
2. 双口径合并：同一份 full_docs 用 tiktoken/gemma 两种口径参照切分，repair 合并后的
   full_docs_chunk_map 同时含两代块 ID（只用一代即失败——断言 compute_mdhash_id
   被两代块 ID 都调用过，且结果 map 实测命中两代）；
3. 混合代命中：图谱引用集含 tiktoken 代 + gemma 代块，两代都能被 repair 命中；
4. 降级与 warning 三项：tokenizer.json 不存在 → 不抛、有日志、退回 tiktoken、
   repair 仍命中 tiktoken 代块；gemma 通道失败 warning 必须同时含
   ①失败原因 ②被跳过的 full_docs 文档条数 ③"missing 中可能含 gemma 口径代的块、
   恢复后重跑 repair 可追回"。
   **断言机制是 loguru sink 捕获，不是 caplog**（warning 由 lightrag_repair.py
   的 loguru logger 发出，不进 stdlib logging，pytest caplog 捕不到——
   双审 B-P2；conftest 无共享 helper，本文件自带 _capture_loguru）。

测试隔离：全 mock——不调真实 LLM / embedding / 知识图谱查询，只在 tmp_path 内
造 3 真相源副本，绝不碰 ~/.niu/lightrag_storage。
"""
from __future__ import annotations

import asyncio
import json
import xml.etree.ElementTree as ET
from pathlib import Path

from loguru import logger

import niu_api.internal.gemma_tokenizer as gt
import niu_api.internal.lightrag_repair as rr

# 病态文档：每单元 = "项目记录" + 16×U+3000（o200k 单 token id 106378 = 48 倍病灶
# 的载体）。tiktoken 口径 1 token/单元（整篇 1 块），Gemma 口径 50 token/单元
# （整篇 2 块）——两代块 ID 不相交，正是双口径重切要覆盖的场景。
_UNIT = "项目记录" + "\u3000" * 16
_PATHOLOGICAL_DOC = _UNIT * 40

# 普通中文文档（两种口径各 1 块、内容相同 → 同 cid，用于验证"同内容已去重"）
_NORMAL_DOC = "通讯录：张三，电话 13800138000，邮箱 zhangsan@example.com"


def _capture_loguru(level="WARNING"):
    """loguru sink 捕获（lightrag_repair 用 loguru 而非 stdlib logging，
    pytest caplog 捕不到——同 tests/test_md_f3.py:24-28 的 per-file 复制模式；
    仓库 conftest 无共享 helper，本文件自带，不假设能 import 到）。"""
    messages = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level=level)
    return messages, sink_id


def _reference_chunk_ids(tokenizer, content: str) -> list[str]:
    """参照切分：用给定 tokenizer 口径算 full_docs 应产出的块 ID 列表。"""
    from lightrag.operate import chunking_by_token_size
    from lightrag.utils import compute_mdhash_id

    chunks = chunking_by_token_size(
        tokenizer, content,
        chunk_token_size=1200,
        chunk_overlap_token_size=50,
    )
    ids = [compute_mdhash_id(c["content"], prefix="chunk-") for c in chunks]
    contents = [c["content"] for c in chunks]
    return ids, contents


def _generation_ids(content: str) -> tuple[list[str], list[str]]:
    """返回 (tiktoken 代块 ID+内容, gemma 代块 ID+内容)。"""
    from lightrag.utils import TiktokenTokenizer

    tk = TiktokenTokenizer(model_name="gpt-4o-mini")
    gm = gt.get_tokenizer()
    assert gm is not None, "tokenizer.json 应在位（已入 git）"
    t_ids, t_contents = _reference_chunk_ids(tk, content)
    g_ids, g_contents = _reference_chunk_ids(gm, content)
    return (t_ids, t_contents), (g_ids, g_contents)


def _docs() -> dict:
    """full_docs：1 篇病态文档 + 1 篇普通文档。"""
    return {
        "doc-pathological": {
            "content": _PATHOLOGICAL_DOC,
            "file_path": "pathological.md",
            "create_time": 1781930000,
        },
        "doc-normal": {
            "content": _NORMAL_DOC,
            "file_path": "contacts.md",
            "create_time": 1781930001,
        },
    }


def _write_graphml(tmp_path: Path, chunk_ids: list[str]) -> None:
    """写最小 GraphML：每个 chunk_id 一个实体节点（d3=source_id）。"""
    ns = "http://graphml.graphdrawing.org/xmlns"
    root = ET.Element(f"{{{ns}}}graphml")
    for kid, attr_name in [
        ("d1", "entity_type"), ("d2", "description"), ("d3", "source_id"),
    ]:
        ET.SubElement(root, f"{{{ns}}}key", {
            "id": kid, "for": "all", "attr.name": attr_name, "attr.type": "string"
        })
    graph = ET.SubElement(root, f"{{{ns}}}graph", {"edgedefault": "undirected"})
    for i, cid in enumerate(chunk_ids):
        node = ET.SubElement(graph, f"{{{ns}}}node", {"id": f"entity-{i}"})
        ET.SubElement(node, f"{{{ns}}}data", {"key": "d1"}).text = "person"
        ET.SubElement(node, f"{{{ns}}}data", {"key": "d2"}).text = f"desc {i}"
        ET.SubElement(node, f"{{{ns}}}data", {"key": "d3"}).text = cid
    ET.ElementTree(root).write(
        tmp_path / "graph_chunk_entity_relation.graphml",
        xml_declaration=True, encoding="utf-8"
    )


def _write_truth_sources(tmp_path: Path, chunk_ids: list[str], docs: dict) -> None:
    """写 3 真相源副本（GraphML + full_docs + cache），派生文件清空。"""
    _write_graphml(tmp_path, chunk_ids)
    (tmp_path / "kv_store_text_chunks.json").write_text("{}", encoding="utf-8")
    (tmp_path / "kv_store_full_docs.json").write_text(
        json.dumps(docs, ensure_ascii=False), encoding="utf-8"
    )
    (tmp_path / "kv_store_llm_response_cache.json").write_text("{}", encoding="utf-8")


def _run_repair(tmp_path: Path, monkeypatch) -> dict:
    """把 repair 指向 tmp_path 副本跑一次 repair_text_chunks。"""
    monkeypatch.setattr(rr, "_STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    return asyncio.run(rr.repair_text_chunks())


def _load_text_chunks(tmp_path: Path) -> dict:
    path = tmp_path / "kv_store_text_chunks.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


# ============== §5 第 2 条：双口径合并 ==============


def test_dual_channel_merge_contains_both_generations(tmp_path, monkeypatch):
    """同一份 full_docs 两口径参照切分，repair 合并后同时含两代块 ID。"""
    (t_ids, t_contents), (g_ids, g_contents) = _generation_ids(_PATHOLOGICAL_DOC)

    # 前置 sanity：两代块 ID 确实不相交（否则"含两代"无意义）
    assert set(t_ids).isdisjoint(set(g_ids)), "病态文档两代块 ID 应不相交"
    assert len(g_ids) == 2 and len(t_ids) == 1, "病态文档应 tiktoken 1 块 / gemma 2 块"

    docs = _docs()
    # 图谱引用集 = 病态文档两代全部块 + 普通文档块（同内容两代同 cid）
    normal_ref = _reference_chunk_ids(gt.get_tokenizer(), _NORMAL_DOC)[0]
    referenced = t_ids + g_ids + normal_ref
    _write_truth_sources(tmp_path, referenced, docs)

    # 间谍：repair_text_chunks 内 compute_mdhash_id 只在 chunking 反查循环调用，
    # 记录值即合并后 full_docs_chunk_map 的 key 全集（跨两通道）。
    recorded: list[str] = []
    real_hash = rr.compute_mdhash_id

    def _spy(content, prefix="chunk-"):
        cid = real_hash(content, prefix=prefix)
        recorded.append(cid)
        return cid

    monkeypatch.setattr(rr, "compute_mdhash_id", _spy)

    result = _run_repair(tmp_path, monkeypatch)

    assert result["status"] == "ok", f"expected ok, got {result}"
    assert result["lost"] == 0
    assert result["actual"] == len(set(referenced))

    # 合并后的 map（= recorded 去重）同时含两代块 ID——只用一代即失败
    merged = set(recorded)
    for cid in t_ids:
        assert cid in merged, "tiktoken 代块 ID 不在合并后的 full_docs_chunk_map"
    for cid in g_ids:
        assert cid in merged, "gemma 代块 ID 不在合并后的 full_docs_chunk_map"

    # 全部引用都落进 text_chunks，内容与参照切分逐字一致
    tc = _load_text_chunks(tmp_path)
    for cid, content in zip(t_ids, t_contents, strict=True):
        assert tc[cid]["content"] == content
        assert tc[cid]["full_doc_id"] == "doc-pathological"
        assert tc[cid]["file_path"] == "pathological.md"
    for cid, content in zip(g_ids, g_contents, strict=True):
        assert tc[cid]["content"] == content
    # 普通文档：两口径同内容 → 同 key 已去重（仍是 1 条，tokens 取 tiktoken 代）
    assert normal_ref[0] in tc
    assert len(tc) == len(set(referenced))


# ============== §5 第 3 条：混合代命中 ==============


def test_mixed_generation_references_all_hit(tmp_path, monkeypatch):
    """图谱引用集含两代块（tiktoken 代 + gemma 代），两代都能被 repair 命中。"""
    (t_ids, t_contents), (g_ids, g_contents) = _generation_ids(_PATHOLOGICAL_DOC)

    docs = _docs()
    # 混合代引用集：病态文档的 tiktoken 代块 + gemma 代块各取一个
    referenced = [t_ids[0], g_ids[0]]
    _write_truth_sources(tmp_path, referenced, docs)

    result = _run_repair(tmp_path, monkeypatch)

    assert result["status"] == "ok", f"expected ok, got {result}"
    assert result["expected"] == 2
    assert result["lost"] == 0
    assert result.get("missing_chunks") in (None, [])
    assert result["actual"] == 2

    tc = _load_text_chunks(tmp_path)
    # tiktoken 代块：内容是整篇文档（1 块）
    assert tc[t_ids[0]]["content"] == t_contents[0]
    # gemma 代块：内容是对应切片，tokens 是 gemma 口径计数（≤1200）
    assert tc[g_ids[0]]["content"] == g_contents[0]
    assert tc[g_ids[0]]["tokens"] <= 1200


# ============== §5 第 4 条：降级与 warning 三项 ==============


def test_gemma_channel_down_warns_three_items_and_keeps_tiktoken(tmp_path, monkeypatch):
    """tokenizer.json 不存在 → 不抛、退回 tiktoken、repair 仍命中 tiktoken 代块；
    warning 必须同时含三项（失败原因 / 被跳过的 full_docs 文档条数 /
    "missing 中可能含 gemma 口径代的块、恢复后重跑 repair 可追回"）。"""
    (t_ids, t_contents), (g_ids, _g_contents) = _generation_ids(_PATHOLOGICAL_DOC)

    docs = _docs()
    referenced = [t_ids[0], g_ids[0]]
    _write_truth_sources(tmp_path, referenced, docs)

    # 降级：gemma 通道指向不存在的 tokenizer.json
    missing_path = tmp_path / "missing-tokenizer.json"
    monkeypatch.setattr(gt, "TOKENIZER_JSON_PATH", missing_path)
    gt.reset_cache()

    messages, sink_id = _capture_loguru(level="WARNING")
    try:
        result = _run_repair(tmp_path, monkeypatch)
    finally:
        logger.remove(sink_id)
        gt.reset_cache()

    # 不抛异常、repair 正常完成（status ok，不是 unrecoverable）
    assert result["status"] == "ok", f"gemma 通道失效不能让 repair 失败: {result}"
    assert not result.get("unrecoverable")

    # 退回 tiktoken：tiktoken 代块仍被命中并重建
    tc = _load_text_chunks(tmp_path)
    assert t_ids[0] in tc
    assert tc[t_ids[0]]["content"] == t_contents[0]

    # gemma 代块如实进 missing（warning 第三项的正是这个后果）
    assert g_ids[0] in result["missing_chunks"]
    assert result["lost"] == 1
    assert result["actual"] == 1

    # warning 三项，缺一即失败（loguru sink 捕获，不是 caplog）
    warnings = [m for m in messages if "gemma 口径通道加载失败" in m]
    assert len(warnings) == 1, f"应有且仅有一条 gemma 通道 warning，got: {messages}"
    warning = warnings[0]

    # ①失败原因（本次=文件不存在 + 具体路径）
    assert "不存在" in warning, "warning 缺失败原因"
    assert "missing-tokenizer.json" in warning, "warning 应含具体失败路径/原因"

    # ②被跳过的 full_docs 文档条数（本例 2 篇有效文档）
    assert "跳过 2 篇" in warning, "warning 缺被跳过的 full_docs 文档条数"

    # ③missing 中可能含 gemma 口径代的块、恢复后重跑可追回
    assert "missing" in warning, "warning 缺 missing 语义"
    assert "gemma 口径代" in warning, "warning 缺 gemma 口径代块提示"
    assert "恢复该通道后重跑 repair 可追回" in warning, "warning 缺追回提示"


def test_gemma_channel_failure_never_breaks_repair_chain(tmp_path, monkeypatch):
    """gemma 通道失效时 repair_text_chunks 不返回 unrecoverable
    （否则会触发 _repair_all_async 整链 break，把 gemma 降级放大成全线停摆）。"""
    (t_ids, _t_contents), (g_ids, _g_contents) = _generation_ids(_PATHOLOGICAL_DOC)
    _write_truth_sources(tmp_path, [t_ids[0], g_ids[0]], _docs())

    monkeypatch.setattr(gt, "TOKENIZER_JSON_PATH", tmp_path / "missing.json")
    gt.reset_cache()
    try:
        result = _run_repair(tmp_path, monkeypatch)
    finally:
        gt.reset_cache()

    assert result["status"] == "ok"
    assert result.get("unrecoverable") is not True
    # tiktoken 通道正常：存量（tiktoken 代）数据不掉队
    tc = _load_text_chunks(tmp_path)
    assert t_ids[0] in tc


def test_tiktoken_channel_failure_still_unrecoverable(tmp_path, monkeypatch):
    """tiktoken 通道失效维持 fail-loud（存量数据全靠它；该 return 触发
    _repair_all_async 整链 break，不得放宽）。"""
    _write_truth_sources(tmp_path, ["chunk-whatever"], _docs())

    def _no_tokenizer():
        return None

    monkeypatch.setattr(rr, "_get_tokenizer", _no_tokenizer)
    result = _run_repair(tmp_path, monkeypatch)

    assert result["status"] == "error"
    assert result["unrecoverable"] is True
    assert "TiktokenTokenizer" in result["message"]
