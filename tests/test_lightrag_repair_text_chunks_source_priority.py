"""repair_text_chunks 来源优先级回归测试（P0：禁止从 LLM 提示词抠 chunk 原文）。

背景（PM 实测机制）：
- cache entry 的 original_prompt 是**完整提示词**，不是文档原文；
- gleaning 轮（补漏实体的后续轮）提示词结构为
  ---Task---/---Instructions---/---Role---/---Instructions---/---Examples---，
  没有 ---Data to be Processed--- 段，整条不含文档原文，只有少样本示例；
- 实测 536 条缓存中 234 条的 original_prompt 第一个三反引号块是示例文本
  （"while Alex clenched his jaw..."），0 条是文档原文。

因此 repair_text_chunks 已删除"抠 original_prompt 第一个 ``` 块"的旧 9.1 路径，
来源优先级改为：full_docs（真相源原文）→ 脑区直接构造 → missing；cache 只提供
llm_cache_list 列表信息。本文件守卫该优先级，防止回退。

测试隔离：全 mock——不调真实 LLM / embedding / 知识图谱查询，只在 tmp_path 内
造 3 真相源副本，绝不碰 ~/.niu/lightrag_storage。
"""
from __future__ import annotations

import asyncio
import json
import xml.etree.ElementTree as ET
from pathlib import Path

# gleaning 轮少样本示例文本（真实 LightRAG 抽取提示词 Examples 段里的示例）。
# 旧实现按 create_time 取到 gleaning 轮时，会把这句当 chunk 原文写进 KG。
_GLEANING_EXAMPLE_TEXT = (
    "while Alex clenched his jaw, the silence between them grew heavier."
)

# 真实文档内容（模拟通讯录类文档），chunk 原文真相源只在 full_docs 里
_REAL_DOC_CONTENT = "通讯录：张三，电话 13800138000，邮箱 zhangsan@example.com"


def _build_gleaning_prompt(example_text: str) -> str:
    """构造 gleaning 轮 cache original_prompt（补漏实体的后续轮提示词）。

    结构跟真实 gleaning 提示词一致：---Task---/---Instructions---/---Role---/
    ---Instructions---/---Examples---，**没有** ---Data to be Processed--- 段。
    Examples 里含三反引号包裹的少样本示例——整条提示词的第一个三反引号块
    就是这段示例文本，不是任何文档原文。
    """
    return f"""---Task---
Extract entities and relationships from the input text that were missed in the previous round.

---Instructions---
1. Only extract entities and relationships that were not mentioned before.
2. Each entity is separated by new line.

---Role---
You are an information extraction assistant.

---Instructions---
Return the entity list first, each entity separated by new line.

---Examples---
Input text:
```
{example_text}
```

Output:
```
("entity"<|#|>Alex<|#|>person<|#|> clenched his jaw while staying silent)
("relationship"<|#|>Alex<|#|>tension<|#|>caused<|#|>silence<|#|>tense)
```
"""


def _build_extraction_prompt(doc_text: str) -> str:
    """构造首次抽取轮 cache original_prompt（含 ---Data--- 段 + ``` 包裹文档段）。

    用于对照：首次抽取轮提示词第一个 ``` 块确实是文档原文，但 gleaning 轮不是——
    这正是旧实现"按 create_time 取最大"翻车的原因。
    """
    return f"""---Task---
Extract entities and relationships from the input text.

---Data---
```
{doc_text}
```

---Output---
```
("entity"<|#|>名字<|#|>person<|#|>描述)
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
    (tmp_path / "kv_store_text_chunks.json").write_text("{}", encoding="utf-8")
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
    """读 repair 重建后的 text_chunks.json（不存在则返回 {}）。"""
    path = tmp_path / "kv_store_text_chunks.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


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


def test_gleaning_prompt_never_used_full_docs_content_wins(tmp_path, monkeypatch):
    """gleaning 形态 prompt 存在时，chunk content 必须来自 full_docs 真实原文。

    防回归点：旧 9.1 按 create_time 最大取 cache entry、抠第一个三反引号块，
     gleaning 轮（create_time 更大）抠出来永远是少样本示例文本。
    """
    docs, real_chunk_id = _make_full_docs(_REAL_DOC_CONTENT)

    cache_entries = [
        {
            # 较早的首次抽取轮：第一个 ``` 块确实是文档原文
            "cache_key": "default:extract:first",
            "return": "entity<|#|>张三<|#|>person<|#|>联系人",
            "cache_type": "extract",
            "chunk_id": real_chunk_id,
            "original_prompt": _build_extraction_prompt(_REAL_DOC_CONTENT),
            "create_time": 1781930000,
        },
        {
            # 更晚的 gleaning 轮：提示词第一个 ``` 块是少样本示例，不是原文
            "cache_key": "default:extract:gleaning",
            "return": "entity<|#|>Alex<|#|>person<|#|>clenched jaw",
            "cache_type": "extract",
            "chunk_id": real_chunk_id,
            "original_prompt": _build_gleaning_prompt(_GLEANING_EXAMPLE_TEXT),
            "create_time": 1781930999,  # 更大——旧实现会错误地选这条
        },
    ]

    _write_truth_sources(tmp_path, [real_chunk_id], docs, cache_entries)

    result = _run_repair(tmp_path, monkeypatch)

    assert result["status"] == "ok", f"expected ok, got {result}"
    assert result["actual"] == 1
    assert result["lost"] == 0

    tc = _load_text_chunks(tmp_path)
    assert real_chunk_id in tc
    # content 等于 full_docs 的真实内容
    assert tc[real_chunk_id]["content"] == _REAL_DOC_CONTENT
    # 且不等于（不含）提示词示例文本
    assert tc[real_chunk_id]["content"] != _GLEANING_EXAMPLE_TEXT
    assert _GLEANING_EXAMPLE_TEXT not in tc[real_chunk_id]["content"]
    assert "Alex" not in tc[real_chunk_id]["content"]
    # full_doc_id / file_path 也来自 full_docs
    assert tc[real_chunk_id]["full_doc_id"] == "doc-real"
    assert tc[real_chunk_id]["file_path"] == "contacts.md"
    # llm_cache_list 仍从 cache 取（两次 LLM 调用都记录）
    assert "default:extract:first" in tc[real_chunk_id]["llm_cache_list"]
    assert "default:extract:gleaning" in tc[real_chunk_id]["llm_cache_list"]


def test_cache_create_time_update_cannot_change_content(tmp_path, monkeypatch):
    """即使把 gleaning cache entry 的 create_time 改得更晚，content 也不变。

    防回归点：防止有人把"删掉的分支"改成"按 create_time 取 gleaning 轮"之类
    的等价回退。
    """
    docs, real_chunk_id = _make_full_docs(_REAL_DOC_CONTENT)

    def _cache_with_ct(ct: int) -> list[dict]:
        return [{
            "cache_key": "default:extract:gleaning",
            "return": "entity<|#|>Alex<|#|>person<|#|>clenched jaw",
            "cache_type": "extract",
            "chunk_id": real_chunk_id,
            "original_prompt": _build_gleaning_prompt(_GLEANING_EXAMPLE_TEXT),
            "create_time": ct,
        }]

    # 第 1 次：gleaning entry create_time 已比 full_docs 新
    _write_truth_sources(tmp_path, [real_chunk_id], docs, _cache_with_ct(1781930999))
    result1 = _run_repair(tmp_path, monkeypatch)
    assert result1["status"] == "ok"
    tc1 = _load_text_chunks(tmp_path)
    assert tc1[real_chunk_id]["content"] == _REAL_DOC_CONTENT

    # 第 2 次：把 gleaning entry 的 create_time 改到更晚（远超一切合理时间），重跑
    _write_truth_sources(tmp_path, [real_chunk_id], docs, _cache_with_ct(9999999999))
    result2 = _run_repair(tmp_path, monkeypatch)
    assert result2["status"] == "ok"
    tc2 = _load_text_chunks(tmp_path)
    # create_time 更新不改变结果：仍是 full_docs 原文，不是提示词示例
    assert tc2[real_chunk_id]["content"] == _REAL_DOC_CONTENT
    assert _GLEANING_EXAMPLE_TEXT not in tc2[real_chunk_id]["content"]
    assert result2["lost"] == 0


def test_cache_only_chunk_becomes_missing(tmp_path, monkeypatch):
    """只在 cache 有 gleaning prompt、full_docs 没有的 chunk → missing。

    证明"抠 original_prompt"路径已真删：cache 不再能凭空产出 chunk 内容。
    """
    ghost_chunk_id = "chunk-only-in-cache-0001"
    cache_entries = [{
        "cache_key": "default:extract:ghost",
        "return": "entity<|#|>Alex<|#|>person<|#|>clenched jaw",
        "cache_type": "extract",
        "chunk_id": ghost_chunk_id,
        "original_prompt": _build_gleaning_prompt(_GLEANING_EXAMPLE_TEXT),
        "create_time": 1781930999,
    }]

    _write_truth_sources(tmp_path, [ghost_chunk_id], {}, cache_entries)

    result = _run_repair(tmp_path, monkeypatch)

    assert result["status"] == "ok", f"expected ok, got {result}"
    assert result["expected"] == 1
    assert result["actual"] == 0
    assert result["lost"] == 1
    # 提示词示例文本绝不能出现在任何 chunk content 里
    tc = _load_text_chunks(tmp_path)
    assert ghost_chunk_id not in tc
    leaked = [cid for cid, v in tc.items() if _GLEANING_EXAMPLE_TEXT in v.get("content", "")]
    assert not leaked, f"提示词示例文本泄漏进 chunk: {leaked}"
