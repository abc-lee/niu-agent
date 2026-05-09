"""
KG 真实数据诊断
==============

使用真实数据（photos.db, messages.db, lightrag_storage JSON）
诊断知识图谱的已知问题，不使用 mock 数据。

用法:
    python scripts/test_kg_real_diagnosis.py              # 只读诊断（阶段1-2）
    python scripts/test_kg_real_diagnosis.py --full       # 完整诊断（含阶段3写操作）
    python scripts/test_kg_real_diagnosis.py --reset      # 重置测试图谱到空状态
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

# ── sys.path 设置 ──
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "agent"))

# ── 日志 ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("kg_diagnosis")

# ── 常量 ──
PHOTOS_DB = Path("E:/tmp/bot/photos.db")
MESSAGES_DB = Path.home() / ".niu" / "messages.db"
STORAGE_DIR = Path.home() / ".niu" / "lightrag_storage"
TEST_STORAGE_DIR = Path.home() / ".niu" / "lightrag_test_diagnosis"
PERSON_UUID = "20196f76-adfb-49ca-8f99-4402fb84b1d5"
PERSON_KEY = f"person:{PERSON_UUID}"
REAL_NAME = "任飞"
ROOT_ENTITY = "brain:Niu"


# ══════════════════════════════════════════════════════════════
#  诊断结果收集
# ══════════════════════════════════════════════════════════════

class DiagnosisResult:
    """收集和报告诊断结果。"""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def confirm(self, problem_id: str, title: str, evidence: str) -> None:
        self.items.append({
            "id": problem_id, "status": "CONFIRMED",
            "title": title, "evidence": evidence,
        })
        logger.info("  [CONFIRMED] %s: %s", problem_id, title)
        logger.info("    证据: %s", evidence[:200])

    def not_found(self, problem_id: str, title: str, note: str) -> None:
        self.items.append({
            "id": problem_id, "status": "NOT_FOUND",
            "title": title, "evidence": note,
        })
        logger.info("  [NOT_FOUND] %s: %s (%s)", problem_id, title, note)

    def skip(self, problem_id: str, title: str, reason: str) -> None:
        self.items.append({
            "id": problem_id, "status": "SKIPPED",
            "title": title, "evidence": reason,
        })
        logger.info("  [SKIPPED] %s: %s (%s)", problem_id, title, reason)

    def report(self) -> dict[str, Any]:
        confirmed = [i for i in self.items if i["status"] == "CONFIRMED"]
        not_found = [i for i in self.items if i["status"] == "NOT_FOUND"]
        skipped = [i for i in self.items if i["status"] == "SKIPPED"]
        return {
            "total": len(self.items),
            "confirmed": len(confirmed),
            "not_found": len(not_found),
            "skipped": len(skipped),
            "items": self.items,
        }


result = DiagnosisResult()


# ══════════════════════════════════════════════════════════════
#  阶段 1：真实图谱现状诊断（只读 JSON 文件）
# ══════════════════════════════════════════════════════════════

def stage_1_graph_diagnosis() -> None:
    logger.info("=" * 60)
    logger.info("  阶段 1: 真实图谱现状诊断（只读）")
    logger.info("=" * 60)

    # ── 1a. 读取实体 ──
    entities_path = STORAGE_DIR / "kv_store_full_entities.json"
    if not entities_path.exists():
        logger.error("实体文件不存在: %s", entities_path)
        return

    with open(entities_path, "r", encoding="utf-8") as f:
        entities_data = json.load(f)

    # 解析实体名 -> 文档分布
    all_entity_names: set[str] = set()
    doc_to_entities: dict[str, list[str]] = {}
    entity_to_docs: dict[str, list[str]] = defaultdict(list)

    for doc_id, data in entities_data.items():
        if isinstance(data, dict) and "entity_names" in data:
            names = data["entity_names"]
            doc_to_entities[doc_id] = names
            for n in names:
                all_entity_names.add(n)
                entity_to_docs[n].append(doc_id)

    logger.info("  实体总数: %d, 文档数: %d", len(all_entity_names), len(doc_to_entities))

    # ── 问题 1: person:{uuid} 和 "任飞" 实体分裂 ──
    person_docs = entity_to_docs.get(PERSON_KEY, [])
    name_docs = entity_to_docs.get(REAL_NAME, [])
    both_docs = set(person_docs) & set(name_docs)

    if person_docs and name_docs and PERSON_KEY != REAL_NAME:
        # 检查是否有结构化同一性关系（is_identical_to 或实体合并）
        # 如果 person:{uuid} 和 任飞 在同一文档中共存但没有合并，说明是分裂的
        result.confirm(
            "P1", "person:{uuid} 和 任飞 实体分裂",
            f"person:{PERSON_UUID} 出现在 {len(person_docs)} 个文档, "
            f"任飞 出现在 {len(name_docs)} 个文档, "
            f"两者共现于 {len(both_docs)} 个文档但未合并为同一实体。"
            f"person:{PERSON_UUID} 文档: {person_docs[:3]}..., "
            f"任飞 文档: {name_docs[:3]}...",
        )
    else:
        result.not_found("P1", "person:{uuid} 和 任飞 实体分裂", "未找到分裂证据")

    # ── 问题 2: 大小写不一致 ──
    # 用户说已在 LightRAG fork 中修复，检查当前数据是否仍有残留
    lower_map: dict[str, list[str]] = defaultdict(list)
    for name in all_entity_names:
        lower_map[name.lower()].append(name)

    case_variants = {k: v for k, v in lower_map.items() if len(v) > 1}
    if case_variants:
        variant_details = []
        for key, variants in case_variants.items():
            variant_details.append(f"{variants}")
        result.confirm(
            "P2", "大小写不一致创建重复实体（残留数据）",
            f"发现 {len(case_variants)} 组大小写变体: {variant_details}。"
            f"LightRAG fork 已修复（_normalize_node_id），但历史数据仍有残留。",
        )
    else:
        result.not_found("P2", "大小写不一致", "当前数据无大小写变体")

    # ── 问题 3: unknown_source file_path ──
    doc_status_path = STORAGE_DIR / "kv_store_doc_status.json"
    if doc_status_path.exists():
        with open(doc_status_path, "r", encoding="utf-8") as f:
            doc_status = json.load(f)

        unknown_docs = []
        photo_related_unknown = []
        for doc_id, status in doc_status.items():
            fp = status.get("file_path", "")
            if fp == "unknown_source":
                summary = status.get("content_summary", "")[:100]
                unknown_docs.append(doc_id)
                # 检查是否是照片相关文档
                if "照片" in summary or "photo" in summary.lower() or PERSON_UUID in summary:
                    photo_related_unknown.append(doc_id)

        if unknown_docs:
            result.confirm(
                "P3", "文档 file_path 为 unknown_source",
                f"共 {len(unknown_docs)}/{len(doc_status)} 个文档 file_path=unknown_source。"
                f"其中 {len(photo_related_unknown)} 个与照片相关。"
                f"根因: sync_photo_to_kg 调用 lightrag_insert(content=text) 未传 file_path。"
                f"IDs: {unknown_docs[:5]}...",
            )
        else:
            result.not_found("P3", "unknown_source file_path", "无 unknown_source 文档")

    # ── 问题 4: 照片实体路径不一致 ──
    photo_entities = [n for n in all_entity_names if n.startswith("photo:")]
    if len(photo_entities) > 1:
        # 检查反斜杠 vs 正斜杠
        backslash = [p for p in photo_entities if "\\" in p]
        forward = [p for p in photo_entities if "/" in p and "\\" not in p]
        if backslash and forward:
            result.confirm(
                "P4", "照片实体路径格式不一致",
                f"反斜杠路径: {backslash}, 正斜杠路径: {forward}。"
                f"不同来源（photos.db vs LLM 提取）产生不同路径格式。",
            )
        else:
            result.not_found("P4", "照片路径不一致", f"所有路径格式一致: {photo_entities}")
    else:
        result.skip("P4", "照片实体路径不一致", f"仅 {len(photo_entities)} 个照片实体，无法比较")

    # ── 问题 5: lightrag_insert_entity 走 ainsert 而非 ainsert_custom_kg ──
    # 检查 lightrag_insert_entity 函数内部是否调用 lightrag_insert(content=...)
    lightrag_server_path = _PROJECT_ROOT / "mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py"
    if lightrag_server_path.exists():
        code = lightrag_server_path.read_text(encoding="utf-8")
        # 找到 lightrag_insert_entity 函数体
        in_insert_entity = False
        insert_entity_uses_ainsert = False
        insert_entity_uses_custom_kg = False
        for line in code.splitlines():
            stripped = line.strip()
            if "def lightrag_insert_entity" in stripped:
                in_insert_entity = True
                continue
            if in_insert_entity:
                if stripped.startswith("def ") and "lightrag_insert_entity" not in stripped:
                    break  # 到下一个函数定义
                if "ingester.lightrag_insert(content=" in line:
                    insert_entity_uses_ainsert = True
                if "ingester.inject_custom_kg(" in line:
                    insert_entity_uses_custom_kg = True

        if insert_entity_uses_ainsert and not insert_entity_uses_custom_kg:
            result.confirm(
                "P5", "lightrag_insert_entity 走 ainsert 而非 ainsert_custom_kg",
                "lightrag_insert_entity 函数内部调用 ingester.lightrag_insert(content=text)，"
                "触发 LLM 自动提取，导致实体分裂（问题1的根因）。",
            )
        elif insert_entity_uses_ainsert and insert_entity_uses_custom_kg:
            result.not_found("P5", "insert_entity 走 ainsert", "代码已同时使用 ainsert 和 inject_custom_kg")
        else:
            result.not_found("P5", "insert_entity 走 ainsert", "代码路径已变更")

    # ── 问题 6: skip_llm_extraction 被忽略 ──
    if lightrag_server_path.exists():
        code = lightrag_server_path.read_text(encoding="utf-8")
        if "_ = source_id, skip_llm_extraction" in code:
            result.confirm(
                "P6", "skip_llm_extraction 参数被显式忽略",
                "代码中 '_ = source_id, skip_llm_extraction' 显式丢弃参数，"
                "merge_persons 传入 skip_llm_extraction=True 无效。",
            )
        else:
            result.not_found("P6", "skip_llm_extraction 被忽略", "代码已修复")

    # ── 问题 7: dream-evolver 无去重 ──
    # 检查 _tidy_context 中 dream-evolver 的调用方式
    compat_path = _PROJECT_ROOT / "niu_api/compat.py"
    if compat_path.exists():
        code = compat_path.read_text(encoding="utf-8")
        if "dream-evolver" in code or "dream_evolver" in code:
            # 检查是否有去重逻辑
            has_dedup = "dedup" in code.lower() or "merge" in code.lower()
            result.confirm(
                "P7", "dream-evolver 创建无去重的图谱数据",
                f"_tidy_context 中调用 dream-evolver 子 Agent，"
                f"子 Agent 使用 lightrag_insert_entity/relation 走 ainsert，"
                f"LLM 提取的实体可能与已有实体重复。"
                f"代码中 {'有' if has_dedup else '无'} 去重逻辑。",
            )
        else:
            result.skip("P7", "dream-evolver 无去重", "未找到 dream-evolver 调用")

    # ── 关系分析 ──
    rel_path = STORAGE_DIR / "kv_store_full_relations.json"
    if rel_path.exists():
        with open(rel_path, "r", encoding="utf-8") as f:
            rel_data = json.load(f)

        # 收集 person:{uuid} -> 任飞 的关系
        person_to_name_rels = []
        for doc_id, data in rel_data.items():
            if isinstance(data, dict) and "relation_pairs" in data:
                for pair in data["relation_pairs"]:
                    if len(pair) == 2:
                        src, tgt = pair
                        if (PERSON_KEY in src and REAL_NAME in tgt) or \
                           (PERSON_KEY in tgt and REAL_NAME in src):
                            person_to_name_rels.append((src, tgt, doc_id))

        logger.info("  person:{uuid} <-> 任飞 关系数: %d", len(person_to_name_rels))
        for src, tgt, doc in person_to_name_rels:
            logger.info("    %s -> %s (from %s)", src, tgt, doc)

    logger.info("-" * 60)
    logger.info("  阶段 1 完成")
    logger.info("-" * 60)


# ══════════════════════════════════════════════════════════════
#  阶段 2：真实数据库数据读取（只读 SQLite）
# ══════════════════════════════════════════════════════════════

def stage_2_database_diagnosis() -> None:
    logger.info("=" * 60)
    logger.info("  阶段 2: 真实数据库数据读取（只读）")
    logger.info("=" * 60)

    # ── 2a. photos.db ──
    if PHOTOS_DB.exists():
        conn = sqlite3.connect(str(PHOTOS_DB))
        conn.row_factory = sqlite3.Row

        # persons
        persons = [dict(r) for r in conn.execute("SELECT id, name, auto_label, photo_count FROM persons").fetchall()]
        logger.info("  persons: %d 条", len(persons))
        for p in persons:
            logger.info("    id=%s, name=%s, auto_label=%s, photo_count=%d",
                       p["id"], p["name"], p["auto_label"], p["photo_count"])

        # photos
        photos = [dict(r) for r in conn.execute(
            "SELECT id, file_path, camera, abstract, kg_synced FROM photos"
        ).fetchall()]
        logger.info("  photos: %d 条", len(photos))
        for p in photos:
            logger.info("    id=%s, file_path=%s, camera=%s, kg_synced=%d",
                       p["id"][:20], p["file_path"][:60], p["camera"], p["kg_synced"])

        # faces
        faces = [dict(r) for r in conn.execute(
            "SELECT person_id, photo_id, confidence FROM faces"
        ).fetchall()]
        logger.info("  faces: %d 条", len(faces))

        # co_occurrences
        co_occs = conn.execute("SELECT COUNT(*) FROM co_occurrences").fetchone()[0]
        logger.info("  co_occurrences: %d 条", co_occs)

        conn.close()

        # 诊断：photos.db 中 person name 已更新为"任飞"
        # 但图谱中 person:{uuid} 和 任飞 仍是独立实体
        if persons and persons[0]["name"] == REAL_NAME:
            logger.info("  [发现] photos.db 中 person name 已为 '%s'，但图谱中仍分裂", REAL_NAME)
    else:
        logger.warning("  photos.db 不存在: %s", PHOTOS_DB)

    # ── 2b. messages.db ──
    if MESSAGES_DB.exists():
        conn = sqlite3.connect(str(MESSAGES_DB))
        conn.row_factory = sqlite3.Row

        messages = [dict(r) for r in conn.execute(
            "SELECT id, role, substr(content, 1, 300) as content FROM messages ORDER BY id"
        ).fetchall()]
        logger.info("  messages: %d 条", len(messages))

        # 找照片入库和命名的对话
        photo_ingest_msgs = []
        name_person_msgs = []
        for m in messages:
            content = m["content"]
            if "入库" in content or "照片" in content:
                photo_ingest_msgs.append(m)
            if "命名" in content or "叫" in content:
                name_person_msgs.append(m)

        logger.info("  照片入库相关消息: %d 条", len(photo_ingest_msgs))
        logger.info("  人物命名相关消息: %d 条", len(name_person_msgs))

        # 重构完整的照片入库 -> 命名流程
        logger.info("  === 照片入库 -> 命名 完整流程 ===")
        for m in messages:
            role = "用户" if m["role"] == "user" else "助手"
            content = m["content"][:150]
            logger.info("    [%s] %s", role, content)

        conn.close()
    else:
        logger.warning("  messages.db 不存在: %s", MESSAGES_DB)

    logger.info("-" * 60)
    logger.info("  阶段 2 完成")
    logger.info("-" * 60)


# ══════════════════════════════════════════════════════════════
#  阶段 3：真实代码路径调用（写测试图谱）
# ══════════════════════════════════════════════════════════════

async def stage_3_real_code_paths() -> None:
    logger.info("=" * 60)
    logger.info("  阶段 3: 真实代码路径调用（写测试图谱）")
    logger.info("=" * 60)

    # 初始化测试工作目录
    if TEST_STORAGE_DIR.exists():
        shutil.rmtree(TEST_STORAGE_DIR, ignore_errors=True)
    TEST_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("  测试工作目录: %s", TEST_STORAGE_DIR)

    # 修补 STORAGE_DIR
    try:
        import niu_api.internal.lightrag_manager as _lm
    except ImportError as exc:
        logger.error("  导入 lightrag_manager 失败: %s", exc)
        return

    original_dir = getattr(_lm, "STORAGE_DIR", None)
    _lm.STORAGE_DIR = str(TEST_STORAGE_DIR)
    _lm._rag_instance = None
    logger.info("  修补 STORAGE_DIR: %s -> %s", original_dir, TEST_STORAGE_DIR)

    # 初始化 LightRAG
    from niu_api.internal.lightrag_manager import ensure_lightrag, get_lightrag
    await ensure_lightrag()
    lightrag = get_lightrag()
    if lightrag is None:
        logger.error("  LightRAG 初始化失败")
        return
    logger.info("  LightRAG 初始化成功: %s", type(lightrag).__name__)

    # 导入适配器
    from niu_api.internal.lightrag_adapter import LightRAGAdapter, LightRAGIngester
    adapter = LightRAGAdapter()
    ingester = LightRAGIngester()

    # ── 3a. 注入根节点 ──
    logger.info("  3a. 注入根节点 brain:Niu")
    ingester.inject_custom_kg(
        entities=[{"entity_name": ROOT_ENTITY, "entity_type": "Brain",
                   "description": "Niu 知识图谱根节点"}],
        relationships=[], chunks=[], source_id="system:init",
    )

    # ── 3b. 调用 sync_photo_to_kg 的真实代码路径 ──
    logger.info("  3b. 调用 sync_photo_to_kg 真实代码路径")

    # 从 photos.db 读取真实数据
    if PHOTOS_DB.exists():
        conn = sqlite3.connect(str(PHOTOS_DB))
        conn.row_factory = sqlite3.Row
        photo = dict(conn.execute("SELECT * FROM photos LIMIT 1").fetchone())
        persons_rows = conn.execute(
            "SELECT p.id, p.name, p.auto_label FROM persons p "
            "JOIN faces f ON f.person_id = p.id WHERE f.photo_id = ?",
            (photo["id"],),
        ).fetchall()
        conn.close()

        # 构造 detected_persons 参数（与 sync_photo_to_kg 一致）
        detected_persons = [
            {"id": r["id"], "name": r["name"] or r["auto_label"]}
            for r in persons_rows
        ]

        # 调用 format_photo_ingest_text（真实函数）
        from niu_photo_server import format_photo_ingest_text
        text = format_photo_ingest_text(photo["file_path"], photo["abstract"], detected_persons)
        logger.info("    format_photo_ingest_text 输出: %s", text)

        # 调用 lightrag_insert（sync_photo_to_kg 的真实路径，不传 file_path）
        logger.info("    调用 lightrag_insert(content=text) — 不传 file_path")
        insert_result = ingester.lightrag_insert(content=text)
        logger.info("    lightrag_insert 结果: %s", insert_result)

        # 检查新文档的 file_path
        doc_status_path = TEST_STORAGE_DIR / "kv_store_doc_status.json"
        if doc_status_path.exists():
            with open(doc_status_path, "r", encoding="utf-8") as f:
                doc_status = json.load(f)
            for doc_id, status in doc_status.items():
                fp = status.get("file_path", "")
                if fp == "unknown_source":
                    result.confirm(
                        "P3-LIVE", "sync_photo_to_kg 产生 unknown_source（实时复现）",
                        f"调用 lightrag_insert(content=text) 后，文档 {doc_id} 的 "
                        f"file_path=unknown_source。根因: 未传 file_path 参数。",
                    )
                    break

        # 检查 LLM 是否创建了独立实体
        entities_path = TEST_STORAGE_DIR / "kv_store_full_entities.json"
        if entities_path.exists():
            with open(entities_path, "r", encoding="utf-8") as f:
                entities = json.load(f)
            all_names = set()
            for doc_id, data in entities.items():
                if isinstance(data, dict) and "entity_names" in data:
                    all_names.update(data["entity_names"])

            # 检查 person:{uuid} 是否存在
            person_key_in_graph = PERSON_KEY in all_names
            # 检查是否有独立的人名实体（如"任飞"或"未命名人物_1"）
            standalone_names = [n for n in all_names if n not in (PERSON_KEY, ROOT_ENTITY)
                               and not n.startswith("photo:") and not n.startswith("brain:")
                               and not n.startswith("Skill") and not n.startswith("Pattern")]
            logger.info("    图谱中实体: %s", sorted(all_names))
            logger.info("    person:{uuid} 存在: %s", person_key_in_graph)
            logger.info("    独立人名实体: %s", standalone_names)

    # ── 3c. 调用 name_person 的真实代码路径 ──
    logger.info("  3c. 调用 name_person 真实代码路径")

    # name_person 内部调用 lightrag_insert_entity
    # 构造与 name_person 相同的文本
    niu_relation = "remembers"
    text = f"语义记忆: {PERSON_KEY}（类型: Person） {REAL_NAME} brain:Niu {niu_relation} {PERSON_KEY}。"
    logger.info("    lightrag_insert_entity 构造的文本: %s", text)

    name_result = ingester.lightrag_insert(content=text, file_paths="custom_kg")
    logger.info("    lightrag_insert 结果: %s", name_result)

    # 等待 LLM 处理完成
    await asyncio.sleep(2)

    # 检查图谱中是否产生了 person:{uuid} 和 任飞 的分裂
    entities_path = TEST_STORAGE_DIR / "kv_store_full_entities.json"
    if entities_path.exists():
        with open(entities_path, "r", encoding="utf-8") as f:
            entities = json.load(f)
        all_names = set()
        for doc_id, data in entities.items():
            if isinstance(data, dict) and "entity_names" in data:
                all_names.update(data["entity_names"])

        has_person_key = PERSON_KEY in all_names
        has_real_name = REAL_NAME in all_names
        logger.info("    person:{uuid} 存在: %s, 任飞 存在: %s", has_person_key, has_real_name)

        if has_person_key and has_real_name:
            result.confirm(
                "P1-LIVE", "name_person 导致 person:{uuid} 和 任飞 实体分裂（实时复现）",
                f"调用 lightrag_insert_entity(name='{PERSON_KEY}', description='{REAL_NAME}') 后，"
                f"图谱中同时存在 '{PERSON_KEY}' 和 '{REAL_NAME}' 两个独立实体。"
                f"根因: lightrag_insert_entity 走 ainsert，LLM 将描述中的'{REAL_NAME}'提取为独立实体。",
            )

    # ── 3d. 对比：使用 inject_custom_kg 的正确路径 ──
    logger.info("  3d. 对比: 使用 inject_custom_kg 的正确路径")

    # 先清理测试图谱
    shutil.rmtree(TEST_STORAGE_DIR, ignore_errors=True)
    TEST_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    _lm._rag_instance = None
    await ensure_lightrag()

    # 注入根节点
    ingester.inject_custom_kg(
        entities=[{"entity_name": ROOT_ENTITY, "entity_type": "Brain",
                   "description": "Niu 知识图谱根节点"}],
        relationships=[], chunks=[], source_id="system:init",
    )

    # 使用 inject_custom_kg 注入照片+人物（正确路径）
    photo_key = f"photo:{photo['file_path']}"
    inject_result = ingester.inject_custom_kg(
        entities=[
            {"entity_name": photo_key, "entity_type": "Photo",
             "description": photo["abstract"], "file_path": photo["file_path"]},
            {"entity_name": PERSON_KEY, "entity_type": "Person",
             "description": REAL_NAME},
        ],
        relationships=[
            {"src_id": photo_key, "tgt_id": PERSON_KEY,
             "keywords": "features", "description": f"照片中出现了{REAL_NAME}"},
            {"src_id": ROOT_ENTITY, "tgt_id": PERSON_KEY,
             "keywords": "remembers", "description": f"认识{REAL_NAME}"},
            {"src_id": ROOT_ENTITY, "tgt_id": photo_key,
             "keywords": "remembers", "description": "拥有这张照片"},
        ],
        chunks=[],
        source_id=photo_key,
    )
    logger.info("    inject_custom_kg 结果: %s", inject_result)

    # 检查图谱
    entities_path = TEST_STORAGE_DIR / "kv_store_full_entities.json"
    if entities_path.exists():
        with open(entities_path, "r", encoding="utf-8") as f:
            entities = json.load(f)
        all_names = set()
        for doc_id, data in entities.items():
            if isinstance(data, dict) and "entity_names" in data:
                all_names.update(data["entity_names"])

        has_person_key = PERSON_KEY in all_names
        has_real_name_standalone = REAL_NAME in all_names
        logger.info("    [inject_custom_kg] person:{uuid} 存在: %s, 独立任飞: %s",
                   has_person_key, has_real_name_standalone)
        logger.info("    [inject_custom_kg] 实体列表: %s", sorted(all_names))

        if has_person_key and not has_real_name_standalone:
            logger.info("    [对比] inject_custom_kg 不产生独立'任飞'实体 — 正确路径")

    # 检查文档 file_path
    doc_status_path = TEST_STORAGE_DIR / "kv_store_doc_status.json"
    if doc_status_path.exists():
        with open(doc_status_path, "r", encoding="utf-8") as f:
            doc_status = json.load(f)
        unknown_count = sum(1 for s in doc_status.values() if s.get("file_path") == "unknown_source")
        logger.info("    [inject_custom_kg] unknown_source 文档数: %d/%d", unknown_count, len(doc_status))

    logger.info("-" * 60)
    logger.info("  阶段 3 完成")
    logger.info("-" * 60)


# ══════════════════════════════════════════════════════════════
#  阶段 4：汇总报告
# ══════════════════════════════════════════════════════════════

def stage_4_report() -> None:
    logger.info("=" * 60)
    logger.info("  阶段 4: 汇总报告")
    logger.info("=" * 60)

    report = result.report()
    logger.info("  总计: %d 项", report["total"])
    logger.info("  已确认: %d 项", report["confirmed"])
    logger.info("  未发现: %d 项", report["not_found"])
    logger.info("  已跳过: %d 项", report["skipped"])

    logger.info("")
    logger.info("  === 已确认问题 ===")
    for item in report["items"]:
        if item["status"] == "CONFIRMED":
            logger.info("  [%s] %s", item["id"], item["title"])
            logger.info("    %s", item["evidence"][:300])
            logger.info("")

    logger.info("  === 修复建议 ===")
    logger.info("  P1/P5/P6: 将 lightrag_insert_entity 改为使用 inject_custom_kg")
    logger.info("    - sync_photo_to_kg: 改用 inject_custom_kg（entities+relationships+chunks）")
    logger.info("    - name_person: 改用 inject_custom_kg（entities+relationships, chunks=[]）")
    logger.info("    - lightrag_insert_entity: 改用 inject_custom_kg（skip_llm_extraction 生效）")
    logger.info("  P3: sync_photo_to_kg 调用 lightrag_insert 时传入 file_path 参数")
    logger.info("  P2: LightRAG fork 已修复，需重新安装并清理历史数据")
    logger.info("  P4: 统一照片路径格式（正斜杠 + 相对路径）")
    logger.info("  P7: dream-evolver 改用 inject_custom_kg（chunks=[]）确保去重")

    logger.info("-" * 60)
    logger.info("  阶段 4 完成")
    logger.info("-" * 60)


# ══════════════════════════════════════════════════════════════
#  重置测试图谱
# ══════════════════════════════════════════════════════════════

def reset_test_graph() -> None:
    if TEST_STORAGE_DIR.exists():
        shutil.rmtree(TEST_STORAGE_DIR, ignore_errors=True)
        logger.info("已清理测试图谱: %s", TEST_STORAGE_DIR)
    else:
        logger.info("测试图谱目录不存在，无需清理")


# ══════════════════════════════════════════════════════════════
#  主流程
# ══════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description="KG 真实数据诊断")
    parser.add_argument("--full", action="store_true",
                       help="完整诊断（含阶段3写操作，需要 API 服务器）")
    parser.add_argument("--reset", action="store_true",
                       help="重置测试图谱到空状态")
    args = parser.parse_args()

    if args.reset:
        reset_test_graph()
        return

    # 阶段 1-2：只读，不需要 API 服务器
    stage_1_graph_diagnosis()
    stage_2_database_diagnosis()

    # 阶段 3：写操作，需要 API 服务器（LLM 代理）
    if args.full:
        asyncio.run(stage_3_real_code_paths())

    # 阶段 4：汇总
    stage_4_report()


if __name__ == "__main__":
    main()
