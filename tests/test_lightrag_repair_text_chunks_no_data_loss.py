"""repair_text_chunks 补缺语义回归测试（P0：禁止"清空 + 重写"丢数据）。

背景（PM 实测的兄弟缺陷，跟"提示词冒充 chunk 原文"同族）：
- 结构化注入路径（LightRAG ainsert_custom_kg，lightrag/lightrag.py L2413-2417）
  只写 chunks_vdb + text_chunks，**不写 full_docs**——skill/脑区/照片/人物这些
  数据的原文唯一载体就是 text_chunks；
- 旧 repair_text_chunks 先 storage._data.clear() 再只 upsert 本次能重建的条目，
  等价于"清空 + 重写"：PM 实测 423 → 236，少的 187 条绝大部分是结构化注入短块；
- 修复后 repair 只做补缺：已存在且内容有效的条目原样保留，只重建/修复"缺失"
  或"已知污染"的条目，并回报 skipped_already_valid 计数。

测试隔离：全 mock——不调真实 LLM / embedding / 知识图谱查询，只在 tmp_path 内
造 3 真相源副本，绝不碰 ~/.niu/lightrag_storage。
"""
from __future__ import annotations

import asyncio
import json
import xml.etree.ElementTree as ET
from pathlib import Path

# 结构化注入风格的短块内容（模拟 SkillSync 注入的 skill 描述：短、不在 full_docs）
_STRUCTURED_CONTENT = "skill 描述：代码评审助手，负责审查 PR 并给出修改建议"

# gleaning 轮少样本示例文本（真实 LightRAG 抽取提示词 Examples 段里的示例）
_GLEANING_EXAMPLE_TEXT = (
    "while Alex clenched his jaw, the silence between them grew heavier."
)

# 真实文档内容（full_docs 真相源里的原文）
_REAL_DOC_CONTENT = "通讯录：张三，电话 13800138000，邮箱 zhangsan@example.com"


def _build_gleaning_prompt(example_text: str) -> str:
    """gleaning 轮 cache original_prompt（补漏实体的后续轮提示词，无文档输入段）。"""
    return f"""---Task---
Extract entities and relationships from the input text that were missed in the previous round.

---Instructions---
1. Only extract entities and relationships that were not mentioned before.

---Role---
You are an information extraction assistant.

---Examples---
Input text:
```
{example_text}
```

Output:
```
("entity"<|#|>Alex<|#|>person<|#|> clenched his jaw while staying silent)
```
"""


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


def _write_truth_sources(
    tmp_path: Path,
    chunk_ids: list[str],
    docs: dict,
    cache_entries: list[dict],
) -> None:
    """写 3 真相源副本（GraphML + full_docs + cache），派生文件清空。"""
    _write_graphml(tmp_path, chunk_ids)
    (tmp_path / "kv_store_full_docs.json").write_text(
        json.dumps(docs, ensure_ascii=False), encoding="utf-8"
    )
    cache = {entry.pop("cache_key"): entry for entry in cache_entries}
    (tmp_path / "kv_store_llm_response_cache.json").write_text(
        json.dumps(cache, ensure_ascii=False), encoding="utf-8"
    )


def _run_repair(tmp_path: Path, monkeypatch) -> dict:
    """把 repair 指向 tmp_path 副本跑一次 repair_text_chunks。"""
    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    from niu_api.internal.lightrag_repair import repair_text_chunks

    return asyncio.run(repair_text_chunks())


def _load_text_chunks(tmp_path: Path) -> dict:
    """读 repair 后的 text_chunks.json（不存在则返回 {}）。"""
    path = tmp_path / "kv_store_text_chunks.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_existing_text_chunks(tmp_path: Path, entries: dict) -> None:
    """预置 repair 前的 text_chunks.json（模拟线上已有条目）。"""
    (tmp_path / "kv_store_text_chunks.json").write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8"
    )


def _make_full_docs(chunk_content: str) -> tuple[dict, str]:
    """造 full_docs 条目 + 真实 chunking 产出的 chunk_id。"""
    from lightrag.utils import compute_mdhash_id

    return (
        {
            "doc-real": {
                "content": chunk_content,
                "file_path": "contacts.md",
                "create_time": 1781930000,
            }
        },
        compute_mdhash_id(chunk_content, prefix="chunk-"),
    )


def _snapshot(tc: dict) -> dict:
    """内容快照（排除 _id/create_time/update_time，聚焦语义内容）。"""
    return {
        cid: {
            k: v for k, v in value.items()
            if k not in ("_id", "create_time", "update_time")
        }
        for cid, value in tc.items()
    }


def test_structured_injected_entry_survives_untouched(tmp_path, monkeypatch):
    """结构化注入风格的短块（full_docs 无对应文档）repair 后必须原样保留。

    防回归点（PM 实测 423 → 236 的兄弟缺陷）：结构化注入路径只写
    chunks_vdb + text_chunks 不写 full_docs，text_chunks 是原文唯一载体——
    绝不能用"full_docs 重建不了"当理由删掉它。
    """
    docs, real_chunk_id = _make_full_docs(_REAL_DOC_CONTENT)
    # 结构化注入短块：id 不是任何 full_docs 内容的 hash，也不在 GraphML 里
    structured_id = "chunk-structured-skill-0001"
    structured_entry = {
        "content": _STRUCTURED_CONTENT,
        "full_doc_id": "skill-code-review",
        "tokens": 12,
        "chunk_order_index": 0,
        "file_path": "custom_kg",
        "source_id": "skill-code-review",
        "status": "processed",
        "llm_cache_list": [],
    }
    _write_truth_sources(tmp_path, [real_chunk_id], docs, [])
    _write_existing_text_chunks(tmp_path, {structured_id: dict(structured_entry)})

    result = _run_repair(tmp_path, monkeypatch)

    assert result["status"] == "ok", f"expected ok, got {result}"
    assert result["final_count"] == 2, f"结构化条目被丢/被算错: {result}"
    assert result["skipped_already_valid"] == 1, f"应原样保留 1 条: {result}"
    tc = _load_text_chunks(tmp_path)
    # 结构化短块仍在，且内容一字未改
    assert structured_id in tc, f"结构化注入短块被 repair 删了: {sorted(tc)}"
    assert tc[structured_id]["content"] == _STRUCTURED_CONTENT
    assert tc[structured_id]["full_doc_id"] == "skill-code-review"
    # 真实文档 chunk 正常重建
    assert tc[real_chunk_id]["content"] == _REAL_DOC_CONTENT


def test_polluted_entry_repaired_from_full_docs(tmp_path, monkeypatch):
    """已存在的污染内容（提示词示例文本）必须被修正为 full_docs 的真实内容。"""
    docs, real_chunk_id = _make_full_docs(_REAL_DOC_CONTENT)
    polluted_entry = {
        "content": _build_gleaning_prompt(_GLEANING_EXAMPLE_TEXT),
        "full_doc_id": "doc-real",
        "tokens": 42,
        "chunk_order_index": 0,
        "file_path": "contacts.md",
        "llm_cache_list": [],
    }
    _write_truth_sources(tmp_path, [real_chunk_id], docs, [])
    _write_existing_text_chunks(tmp_path, {real_chunk_id: dict(polluted_entry)})

    result = _run_repair(tmp_path, monkeypatch)

    assert result["status"] == "ok", f"expected ok, got {result}"
    assert result["actual"] == 1
    assert result["repaired"] == 1, f"污染条目应被覆盖修复: {result}"
    assert result["rebuilt"] == 0
    tc = _load_text_chunks(tmp_path)
    # 污染内容被替换成 full_docs 的真实原文
    assert tc[real_chunk_id]["content"] == _REAL_DOC_CONTENT
    assert _GLEANING_EXAMPLE_TEXT not in tc[real_chunk_id]["content"]
    assert "---Task---" not in tc[real_chunk_id]["content"]


def test_missing_chunk_does_not_take_others_down(tmp_path, monkeypatch):
    """既不在 full_docs 也不在任何源里的 chunk → 记 missing，且不连带删别的条。"""
    docs, real_chunk_id = _make_full_docs(_REAL_DOC_CONTENT)
    ghost_chunk_id = "chunk-not-found-anywhere-001"
    structured_id = "chunk-structured-skill-0002"
    structured_entry = {
        "content": "skill 描述：日程提醒助手，负责定时提醒用户",
        "full_doc_id": "skill-schedule",
        "tokens": 11,
        "chunk_order_index": 0,
        "file_path": "custom_kg",
        "llm_cache_list": [],
    }
    _write_truth_sources(tmp_path, [real_chunk_id, ghost_chunk_id], docs, [])
    _write_existing_text_chunks(tmp_path, {structured_id: dict(structured_entry)})

    result = _run_repair(tmp_path, monkeypatch)

    assert result["status"] == "ok", f"expected ok, got {result}"
    assert result["lost"] == 1, f"ghost chunk 应记 missing: {result}"
    assert ghost_chunk_id in result["missing_chunks"]
    tc = _load_text_chunks(tmp_path)
    # ghost 没有源、text_chunks 里也没有 → 不出现，但绝不能把别的条带掉
    assert ghost_chunk_id not in tc
    assert structured_id in tc, f"missing 判定把结构化条目带掉了: {sorted(tc)}"
    assert tc[structured_id]["content"] == "skill 描述：日程提醒助手，负责定时提醒用户"
    assert tc[real_chunk_id]["content"] == _REAL_DOC_CONTENT
    assert result["final_count"] == 2


def test_polluted_without_source_removed_others_kept(tmp_path, monkeypatch):
    """已知污染（提示词示例文本）且无源可重建 → 只删这一条，别的条目保留。"""
    docs, real_chunk_id = _make_full_docs(_REAL_DOC_CONTENT)
    polluted_ghost_id = "chunk-polluted-no-source-001"
    structured_id = "chunk-structured-skill-0003"
    _write_truth_sources(tmp_path, [real_chunk_id, polluted_ghost_id], docs, [])
    _write_existing_text_chunks(tmp_path, {
        polluted_ghost_id: {
            "content": _build_gleaning_prompt(_GLEANING_EXAMPLE_TEXT),
            "full_doc_id": "doc-deleted",
            "tokens": 30,
            "chunk_order_index": 0,
            "file_path": "deleted.md",
            "llm_cache_list": [],
        },
        structured_id: {
            "content": "skill 描述：翻译助手，负责中英互译",
            "full_doc_id": "skill-translate",
            "tokens": 10,
            "chunk_order_index": 0,
            "file_path": "custom_kg",
            "llm_cache_list": [],
        },
    })

    result = _run_repair(tmp_path, monkeypatch)

    assert result["status"] == "ok", f"expected ok, got {result}"
    assert result["removed_polluted"] == 1, f"污染无源条目应被精确删除: {result}"
    tc = _load_text_chunks(tmp_path)
    assert polluted_ghost_id not in tc
    assert structured_id in tc
    assert tc[structured_id]["content"] == "skill 描述：翻译助手，负责中英互译"
    assert tc[real_chunk_id]["content"] == _REAL_DOC_CONTENT
    assert result["final_count"] == 2


def test_repair_twice_idempotent(tmp_path, monkeypatch):
    """连跑两次 repair：第二次不改变第一次的结果（幂等），且不再动任何条目。"""
    docs, real_chunk_id = _make_full_docs(_REAL_DOC_CONTENT)
    structured_id = "chunk-structured-skill-0004"
    _write_truth_sources(tmp_path, [real_chunk_id], docs, [])
    _write_existing_text_chunks(tmp_path, {
        structured_id: {
            "content": "skill 描述：总结助手，负责长文摘要",
            "full_doc_id": "skill-summary",
            "tokens": 9,
            "chunk_order_index": 0,
            "file_path": "custom_kg",
            "llm_cache_list": [],
        },
    })

    result1 = _run_repair(tmp_path, monkeypatch)
    assert result1["status"] == "ok", f"expected ok, got {result1}"
    tc_after_1 = _load_text_chunks(tmp_path)
    snapshot1 = _snapshot(tc_after_1)

    result2 = _run_repair(tmp_path, monkeypatch)
    assert result2["status"] == "ok", f"expected ok, got {result2}"
    tc_after_2 = _load_text_chunks(tmp_path)
    snapshot2 = _snapshot(tc_after_2)

    # 第二次前后条数与内容完全一致
    assert len(tc_after_2) == len(tc_after_1)
    assert snapshot2 == snapshot1
    # 第二次全部判定为"本来就好、没动它"，没有任何重建/修复/删除
    assert result2["skipped_already_valid"] == len(tc_after_1), f"第二次应全量跳过: {result2}"
    assert result2["actual"] == 0
    assert result2["rebuilt"] == 0
    assert result2["repaired"] == 0
    assert result2["removed_polluted"] == 0
    assert result2["final_count"] == len(tc_after_1)


def test_is_valid_text_chunk_entry_predicate():
    """有效性判定谓词：空 content / 提示词示例文本 / tokens=0 都是无效。"""
    from niu_api.internal.lightrag_repair import _is_valid_text_chunk_entry

    # 有效条目（含结构化注入风格：带 source_id/status 附加字段）
    assert _is_valid_text_chunk_entry({
        "content": "skill 描述：xxx",
        "tokens": 12,
        "full_doc_id": "skill-x",
    })
    # content 是 gleaning 提示词示例文本 → 无效（P0 污染形态）
    assert not _is_valid_text_chunk_entry({
        "content": _build_gleaning_prompt(_GLEANING_EXAMPLE_TEXT),
        "tokens": 42,
    })
    # content 就是示例文本本身 → 无效
    assert not _is_valid_text_chunk_entry({
        "content": f"prefix {_GLEANING_EXAMPLE_TEXT} suffix",
        "tokens": 42,
    })
    # content 为空 / tokens 为 0 / 非 dict / tokens 缺失 → 无效
    assert not _is_valid_text_chunk_entry({"content": "", "tokens": 5})
    assert not _is_valid_text_chunk_entry({"content": "   ", "tokens": 5})
    assert not _is_valid_text_chunk_entry({"content": "real text", "tokens": 0})
    assert not _is_valid_text_chunk_entry({"content": "real text"})
    assert not _is_valid_text_chunk_entry({"content": "real text", "tokens": True})
    assert not _is_valid_text_chunk_entry("not-a-dict")
    assert not _is_valid_text_chunk_entry(None)
