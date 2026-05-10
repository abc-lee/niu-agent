#!/usr/bin/env python3
"""
真实KG数据诊断脚本 — 只读，不修改任何数据
直接读取 graphml (networkx) + photos.db (sqlite3)，诊断实体碎片化问题

诊断5个问题:
  P1: 照片实体碎片化 — 同一张照片有3个实体
  P2: 人物实体碎片化 — 同一个人有2个实体
  P3: 边方向错误 — Person->Photo 应为 Photo->Person
  P4: abstract内容不当 — 包含代码推断的"合影"
  P5: 旧照片实体未删除 — name_person后旧实体仍孤立

用法:
  python scripts/test_kg_real_diagnosis.py
"""

from __future__ import annotations

import os
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import networkx as nx

# ── 常量 ──
PHOTOS_DB = Path("E:/tmp/bot/photos.db")
STORAGE_DIR = Path.home() / ".niu" / "lightrag_storage"
GRAPHML_PATH = STORAGE_DIR / "graph_chunk_entity_relation.graphml"
ROOT_ENTITY = "Niu"

# ── 诊断结果 ──


@dataclass
class Issue:
    """单个诊断问题。"""

    id: str
    severity: str  # CRITICAL, HIGH, MEDIUM, LOW
    title: str
    evidence: str
    status: str = "CONFIRMED"  # CONFIRMED, NOT_FOUND, SKIPPED


@dataclass
class DiagnosisResult:
    """诊断结果收集器。"""

    issues: list[Issue] = field(default_factory=list)

    def confirm(self, id: str, severity: str, title: str, evidence: str) -> None:
        self.issues.append(Issue(id=id, severity=severity, title=title,
                                evidence=evidence, status="CONFIRMED"))

    def not_found(self, id: str, title: str, note: str) -> None:
        self.issues.append(Issue(id=id, severity="INFO", title=title,
                                evidence=note, status="NOT_FOUND"))

    def skip(self, id: str, title: str, reason: str) -> None:
        self.issues.append(Issue(id=id, severity="INFO", title=title,
                                evidence=reason, status="SKIPPED"))

    def report(self) -> None:
        confirmed = [i for i in self.issues if i.status == "CONFIRMED"]
        not_found = [i for i in self.issues if i.status == "NOT_FOUND"]
        skipped = [i for i in self.issues if i.status == "SKIPPED"]

        print()
        print("=" * 70)
        print("  诊断汇总")
        print("=" * 70)
        print(f"  总计:   {len(self.issues)} 项")
        print(f"  已确认: {len(confirmed)} 项")
        print(f"  未发现: {len(not_found)} 项")
        print(f"  已跳过: {len(skipped)} 项")
        print()

        if confirmed:
            print("  --- 已确认问题 ---")
            for item in confirmed:
                print(f"  [{item.severity}] {item.id}: {item.title}")
                print(f"    证据: {item.evidence[:200]}")
                print()

        if not_found:
            print("  --- 未发现问题 ---")
            for item in not_found:
                print(f"  [INFO] {item.id}: {item.title} ({item.evidence})")
            print()

        if skipped:
            print("  --- 已跳过 ---")
            for item in skipped:
                print(f"  [INFO] {item.id}: {item.title} ({item.evidence})")
            print()


# ── 数据加载 ──


def load_graph() -> nx.DiGraph:
    """加载真实图谱 (graphml) 并转为有向图。

    networkx.read_graphml 默认返回无向 Graph，
    但 LightRAG 的 graphml 实际上存储了有向边。
    需要显式转为 DiGraph 才能用 successors/predecessors。
    """
    if not GRAPHML_PATH.exists():
        print(f"ERROR: graphml 文件不存在: {GRAPHML_PATH}")
        sys.exit(1)
    G_raw = nx.read_graphml(str(GRAPHML_PATH))
    # graphml 可能是 Graph 或 DiGraph，统一转为 DiGraph
    if not G_raw.is_directed():
        G = nx.DiGraph()
        G.add_nodes_from(G_raw.nodes(data=True))
        G.add_edges_from(G_raw.edges(data=True))
    else:
        G = G_raw
    print(f"[加载] graphml: {G.number_of_nodes()} 节点, {G.number_of_edges()} 边 (DiGraph)")
    return G


def load_photos_db() -> sqlite3.Connection:
    """加载真实照片数据库 (只读)。"""
    if not PHOTOS_DB.exists():
        print(f"ERROR: photos.db 不存在: {PHOTOS_DB}")
        sys.exit(1)
    conn = sqlite3.connect(f"file:{PHOTOS_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    print(f"[加载] photos.db: OK")
    return conn


# ── 辅助函数 ──


def get_entity_id(G: nx.DiGraph, node_id: str) -> str:
    """获取节点的 entity_id 属性，不存在则返回 node_id 本身。"""
    return G.nodes[node_id].get("entity_id", node_id)


def get_entity_type(G: nx.DiGraph, node_id: str) -> str:
    """获取节点的 entity_type 属性。"""
    return G.nodes[node_id].get("entity_type", "UNKNOWN")


def find_node_by_entity_id(G: nx.DiGraph, entity_id: str) -> str | None:
    """按 entity_id 查找节点，返回 node_id 或 None。"""
    for nid, attrs in G.nodes(data=True):
        if attrs.get("entity_id") == entity_id:
            return nid
    return None


def get_neighbors_info(G: nx.DiGraph, node_id: str) -> list[dict[str, Any]]:
    """获取节点的所有邻居（入+出）及边属性。"""
    neighbors = []
    # 出边
    for tgt in G.successors(node_id):
        edge_data = G.edges[node_id, tgt]
        neighbors.append({
            "direction": "out",
            "neighbor": get_entity_id(G, tgt),
            "neighbor_type": get_entity_type(G, tgt),
            "keywords": edge_data.get("keywords", ""),
            "weight": edge_data.get("weight", 0),
        })
    # 入边
    for src in G.predecessors(node_id):
        edge_data = G.edges[src, node_id]
        neighbors.append({
            "direction": "in",
            "neighbor": get_entity_id(G, src),
            "neighbor_type": get_entity_type(G, src),
            "keywords": edge_data.get("keywords", ""),
            "weight": edge_data.get("weight", 0),
        })
    return neighbors


# ══════════════════════════════════════════════════════════════
#  P1: 照片实体碎片化
# ══════════════════════════════════════════════════════════════

def diagnose_photo_fragmentation(G: nx.DiGraph, result: DiagnosisResult) -> None:
    """诊断同一张照片产生多个实体的问题。

    方法: 从 photos.db 获取每张照片的 file_path 和 abstract，
    然后在图谱中搜索所有可能对应同一张照片的实体节点，
    检测同一照片是否有多个KG实体。
    """
    print()
    print("-" * 60)
    print("  P1: 照片实体碎片化诊断")
    print("-" * 60)

    # 收集所有可能是照片相关的节点，按以下特征识别:
    #   1. entity_type == "photo"
    #   2. entity_id 是文件名格式 (YYYYMMDD_HHMMSS)
    #   3. entity_id 包含"合影"(代码推断的照片描述)
    photo_nodes = []
    filename_nodes = []
    heying_nodes = []

    for nid, attrs in G.nodes(data=True):
        eid = attrs.get("entity_id", nid)
        etype = attrs.get("entity_type", "")

        if etype == "photo":
            photo_nodes.append((nid, eid, attrs))
        # 文件名实体: 类似 20090603_092316 (YYYYMMDD_HHMMSS)
        if len(eid) >= 13 and eid.replace("_", "").replace(":", "").isdigit():
            filename_nodes.append((nid, eid, attrs))
        # 描述中包含"合影"的实体 (排除系统类型)
        if ("合影" in eid) and etype not in (
            "brainregion", "BrainRegion", "skill", "tool", "location",
        ):
            heying_nodes.append((nid, eid, attrs))

    print(f"  photo 类型节点: {len(photo_nodes)}")
    for nid, eid, attrs in photo_nodes:
        print(f"    [{attrs.get('entity_type')}] {eid}")

    print(f"  文件名格式节点: {len(filename_nodes)}")
    for nid, eid, attrs in filename_nodes:
        print(f"    [{attrs.get('entity_type')}] {eid}")

    print(f"  含'合影'的节点: {len(heying_nodes)}")
    for nid, eid, attrs in heying_nodes:
        print(f"    [{attrs.get('entity_type')}] {eid}")

    # 合并所有照片相关实体 (去重)
    all_photo_related = {}
    for nid, eid, attrs in photo_nodes + filename_nodes + heying_nodes:
        if nid not in all_photo_related:
            all_photo_related[nid] = (nid, eid, attrs)

    all_items = list(all_photo_related.values())

    # 按日期分组: 提取日期前缀(YYYYMMDD)用于分组
    def extract_date(eid: str) -> str | None:
        """从实体ID中提取8位日期前缀。"""
        digits = eid.replace("_", "").replace(":", "").replace("，", "").replace(" ", "")
        # 查找8位连续数字 (YYYYMMDD)
        for i in range(len(digits) - 7):
            seg = digits[i:i+8]
            if seg[:4] in ("2009", "2010", "2011", "2012", "2013", "2014",
                           "2015", "2016", "2017", "2018", "2019", "2020",
                           "2021", "2022", "2023", "2024", "2025", "2026"):
                return seg
        return None

    date_groups: dict[str, list[tuple]] = {}
    for item in all_items:
        nid, eid, attrs = item
        date_key = extract_date(eid)
        if date_key:
            if date_key not in date_groups:
                date_groups[date_key] = []
            date_groups[date_key].append(item)

    # 找到同一日期有多个实体的组 (碎片化)
    fragmented_groups = {k: v for k, v in date_groups.items() if len(v) > 1}

    if fragmented_groups:
        total_fragments = sum(len(v) for v in fragmented_groups.values())
        group_details = []
        for date_key, items in fragmented_groups.items():
            entity_descs = []
            for nid, eid, attrs in items:
                etype = attrs.get("entity_type", "?")
                desc = attrs.get("description", "")[:40]
                entity_descs.append(f"[{etype}] {eid} (desc={desc})")
            group_details.append(f"日期{date_key}: " + "; ".join(entity_descs))

        # 计算严重程度: 按最大组的碎片数
        max_frag = max(len(v) for v in fragmented_groups.values())
        severity = "CRITICAL" if max_frag >= 3 else "HIGH"

        result.confirm(
            "P1", severity,
            "照片实体碎片化 — 同一张照片有多个KG实体",
            f"发现 {len(fragmented_groups)} 组碎片化照片实体 "
            f"(共 {total_fragments} 个实体): "
            + " | ".join(group_details)
            + "。根因: abstract内容在name_person后变化('未命名人物_1'->'任飞')，"
            + "导致照片实体名变了，旧实体未删除。",
        )
    else:
        # 即使没有按日期分组成功，也检查总数
        if len(all_items) > 0:
            result.not_found("P1", "照片实体碎片化",
                             f"按日期分组未发现碎片，共 {len(all_items)} 个照片相关实体")
        else:
            result.skip("P1", "照片实体碎片化", "未找到照片相关实体")

    # 额外: 检查照片实体之间的边
    if len(all_items) > 1:
        print()
        print("  照片相关实体之间的边:")
        for i, (nid_i, eid_i, _) in enumerate(all_items):
            for j, (nid_j, eid_j, _) in enumerate(all_items):
                if i >= j:
                    continue
                if G.has_edge(nid_i, nid_j):
                    edge = G.edges[nid_i, nid_j]
                    print(f"    {eid_i} --[{edge.get('keywords','')}]--> {eid_j}")
                if G.has_edge(nid_j, nid_i):
                    edge = G.edges[nid_j, nid_i]
                    print(f"    {eid_j} --[{edge.get('keywords','')}]--> {eid_i}")


# ══════════════════════════════════════════════════════════════
#  P2: 人物实体碎片化
# ══════════════════════════════════════════════════════════════

def diagnose_person_fragmentation(G: nx.DiGraph, db: sqlite3.Connection, result: DiagnosisResult) -> None:
    """诊断同一个人产生多个实体的问题。

    方法: 从 photos.db 获取每个人物的 name 和 auto_label，
    检查图谱中是否同时存在 auto_label 实体和 name 实体，
    即同一人在图谱中有多个不同名称的person实体。
    """
    print()
    print("-" * 60)
    print("  P2: 人物实体碎片化诊断")
    print("-" * 60)

    # 收集所有 person 类型的节点
    person_nodes = []
    for nid, attrs in G.nodes(data=True):
        etype = attrs.get("entity_type", "")
        eid = attrs.get("entity_id", nid)
        if etype == "person":
            person_nodes.append((nid, eid, attrs))

    print(f"  person 类型节点: {len(person_nodes)}")
    for nid, eid, attrs in person_nodes:
        desc = attrs.get("description", "")[:60]
        print(f"    {eid} (desc={desc})")

    # 从 photos.db 获取真实人物数据
    db_persons = [dict(r) for r in db.execute(
        "SELECT id, name, auto_label FROM persons"
    ).fetchall()]
    print(f"  photos.db persons: {len(db_persons)}")
    for p in db_persons:
        print(f"    id={p['id']}, name={p['name']}, auto_label={p['auto_label']}")

    # 诊断: 对每个DB人物，检查图谱中是否存在碎片化
    # 碎片化 = 同一人在图谱中既有 auto_label 实体又有 name 实体
    fragmented_persons = []
    for p in db_persons:
        name = p.get("name", "")
        auto_label = p.get("auto_label", "")
        person_id = p.get("id", "")

        if not name or not auto_label or name == auto_label:
            continue

        # 检查图谱中是否存在 name 实体
        name_node = find_node_by_entity_id(G, name)
        # 检查图谱中是否存在 auto_label 实体
        auto_node = find_node_by_entity_id(G, auto_label)
        # 也检查以 auto_label 开头的实体 (如 "未命名人物_N" 模板)
        auto_prefix_nodes = []
        for nid, eid, attrs in person_nodes:
            if eid.startswith(auto_label.rsplit("_", 1)[0] + "_"):
                auto_prefix_nodes.append((nid, eid))

        has_name = name_node is not None
        has_auto = auto_node is not None
        has_auto_prefix = len(auto_prefix_nodes) > 0

        print(f"  检查 person_id={person_id[:20]}: "
              f"name='{name}'(KG:{'Y' if has_name else 'N'}), "
              f"auto_label='{auto_label}'(KG:{'Y' if has_auto else 'N'}), "
              f"auto_prefix匹配:{auto_prefix_nodes})")

        if has_name and (has_auto or has_auto_prefix):
            evidence_parts = [f"已命名实体 '{name}'"]
            if has_auto:
                evidence_parts.append(f"auto_label实体 '{auto_label}'")
            for _, prefix_eid in auto_prefix_nodes:
                if prefix_eid != auto_label:
                    evidence_parts.append(f"模板实体 '{prefix_eid}'")
            fragmented_persons.append({
                "person_id": person_id,
                "name": name,
                "auto_label": auto_label,
                "evidence": evidence_parts,
            })

    if fragmented_persons:
        details = []
        for fp in fragmented_persons:
            details.append(
                f"person_id={fp['person_id'][:20]}: "
                + ", ".join(fp["evidence"])
            )
        result.confirm(
            "P2", "HIGH",
            "人物实体碎片化 — 同一个人有多个KG实体",
            f"发现 {len(fragmented_persons)} 个碎片化人物: "
            + "; ".join(details)
            + "。根因: 第一次ainsert让LLM提取了'未命名人物_N'模板实体，"
            + "name_person用inject_custom_kg注入了命名实体，两者未合并。",
        )
    else:
        # 即使没有与DB交叉引用的碎片，也检查是否有未命名人物模板残留
        unnamed_in_graph = [
            (nid, eid) for nid, eid, _ in person_nodes
            if eid.startswith("未命名人物")
        ]
        if unnamed_in_graph:
            result.confirm(
                "P2", "MEDIUM",
                "人物实体碎片化 — '未命名人物'模板实体残留",
                f"图谱中存在 {len(unnamed_in_graph)} 个'未命名人物'模板实体: "
                f"{[eid for _, eid in unnamed_in_graph]}。"
                f"这些是LLM提取的临时命名实体，应被合并到正式命名实体中。",
            )
        else:
            result.not_found("P2", "人物实体碎片化",
                             f"KG中 {len(person_nodes)} 个person实体，无碎片")

    # 额外: 检查 auto_label 实体是否还存在于图谱中
    for p in db_persons:
        auto_label = p.get("auto_label", "")
        if auto_label:
            auto_node = find_node_by_entity_id(G, auto_label)
            if auto_node:
                print(f"  [注意] DB中 auto_label='{auto_label}' 仍在图谱中存在")


# ══════════════════════════════════════════════════════════════
#  P3: 边方向错误
# ══════════════════════════════════════════════════════════════

def diagnose_edge_direction(G: nx.DiGraph, result: DiagnosisResult) -> None:
    """诊断边方向错误: Person->Photo 应为 Photo->Person。"""
    print()
    print("-" * 60)
    print("  P3: 边方向错误诊断")
    print("-" * 60)

    # 收集所有 person->photo 方向的边 (features)
    wrong_direction_edges = []
    correct_direction_edges = []

    for src, tgt, attrs in G.edges(data=True):
        src_type = get_entity_type(G, src)
        tgt_type = get_entity_type(G, tgt)
        src_eid = get_entity_id(G, src)
        tgt_eid = get_entity_id(G, tgt)
        keywords = attrs.get("keywords", "")

        # Person -> Photo 方向的 features 边 (错误: 应为 Photo -> Person)
        if src_type == "person" and tgt_type == "photo" and "features" in keywords:
            wrong_direction_edges.append({
                "src": src_eid, "src_type": src_type,
                "tgt": tgt_eid, "tgt_type": tgt_type,
                "keywords": keywords, "weight": attrs.get("weight", 0),
            })

        # Photo -> Person 方向的 features 边 (正确)
        if src_type == "photo" and tgt_type == "person" and "features" in keywords:
            correct_direction_edges.append({
                "src": src_eid, "src_type": src_type,
                "tgt": tgt_eid, "tgt_type": tgt_type,
                "keywords": keywords, "weight": attrs.get("weight", 0),
            })

    # 也检查人物->照片相关实体的边 (可能类型不是photo但包含"合影")
    for src, tgt, attrs in G.edges(data=True):
        src_type = get_entity_type(G, src)
        tgt_type = get_entity_type(G, tgt)
        src_eid = get_entity_id(G, src)
        tgt_eid = get_entity_id(G, tgt)
        keywords = attrs.get("keywords", "")

        # Person -> 含"合影"实体 (方向反了: 应该是照片->人物)
        if src_type == "person" and "合影" in tgt_eid and "features" in keywords:
            already = any(e["src"] == src_eid and e["tgt"] == tgt_eid
                         for e in wrong_direction_edges)
            if not already:
                wrong_direction_edges.append({
                    "src": src_eid, "src_type": src_type,
                    "tgt": tgt_eid, "tgt_type": tgt_type,
                    "keywords": keywords, "weight": attrs.get("weight", 0),
                })

        # 含"合影"实体 -> Person (正确方向)
        if "合影" in src_eid and tgt_type == "person" and "features" in keywords:
            already = any(e["src"] == src_eid and e["tgt"] == tgt_eid
                         for e in correct_direction_edges)
            if not already:
                correct_direction_edges.append({
                    "src": src_eid, "src_type": src_type,
                    "tgt": tgt_eid, "tgt_type": tgt_type,
                    "keywords": keywords, "weight": attrs.get("weight", 0),
                })

    print(f"  错误方向边 (Person->Photo): {len(wrong_direction_edges)}")
    for e in wrong_direction_edges:
        print(f"    {e['src']} [{e['src_type']}] --[{e['keywords']}]--> "
              f"{e['tgt']} [{e['tgt_type']}] w={e['weight']}")

    print(f"  正确方向边 (Photo->Person): {len(correct_direction_edges)}")
    for e in correct_direction_edges:
        print(f"    {e['src']} [{e['src_type']}] --[{e['keywords']}]--> "
              f"{e['tgt']} [{e['tgt_type']}] w={e['weight']}")

    if wrong_direction_edges:
        edge_descs = []
        for e in wrong_direction_edges:
            edge_descs.append(
                f"{e['src']}({e['src_type']}) --[{e['keywords']}]--> "
                f"{e['tgt']}({e['tgt_type']})"
            )
        result.confirm(
            "P3", "HIGH",
            "边方向错误 — Person->Photo 应为 Photo->Person",
            f"发现 {len(wrong_direction_edges)} 条方向错误的features边: "
            + "; ".join(edge_descs)
            + "。语义: '照片中出现了谁' 应为照片->人物，不是人物->照片。",
        )
    else:
        result.not_found("P3", "边方向错误", "所有features边方向正确")


# ══════════════════════════════════════════════════════════════
#  P4: abstract内容不当
# ══════════════════════════════════════════════════════════════

def diagnose_abstract_content(G: nx.DiGraph, db: sqlite3.Connection, result: DiagnosisResult) -> None:
    """诊断abstract内容问题: 代码推断的'合影'不是照片实际内容。"""
    print()
    print("-" * 60)
    print("  P4: abstract内容不当诊断")
    print("-" * 60)

    # 从 photos.db 检查 abstract
    db_photos = [dict(r) for r in db.execute(
        "SELECT id, file_path, abstract, camera FROM photos"
    ).fetchall()]

    problematic_abstracts = []
    for p in db_photos:
        abstract = p.get("abstract", "")
        file_path = p.get("file_path", "")
        # 检查 abstract 是否包含代码推断的"合影"
        if "合影" in abstract:
            # 进一步检查: "合影"是代码推断的，还是照片内容真实描述
            # 如果abstract格式是 "XXX合影，YYYY:MM:DD"，说明是代码生成的
            if "，" in abstract and abstract.endswith(("03", "04", "05", "06", "07", "08", "09")):
                # 以日期结尾，是代码生成的格式
                problematic_abstracts.append({
                    "file_path": file_path,
                    "abstract": abstract,
                    "reason": "格式为'人物名+合影+日期'，是代码推断生成，非照片实际内容",
                })

    # 从图谱检查: photo 类型节点的 description
    photo_entities_with_合影 = []
    for nid, attrs in G.nodes(data=True):
        etype = attrs.get("entity_type", "")
        eid = attrs.get("entity_id", nid)
        desc = attrs.get("description", "")
        if etype == "photo" and "合影" in (eid + desc):
            photo_entities_with_合影.append({
                "entity_id": eid,
                "description": desc[:80],
            })

    print(f"  photos.db 中含'合影'的abstract: {len(problematic_abstracts)}")
    for pa in problematic_abstracts:
        print(f"    {pa['file_path']}: abstract='{pa['abstract']}'")
        print(f"      原因: {pa['reason']}")

    print(f"  图谱中含'合影'的photo实体: {len(photo_entities_with_合影)}")
    for pe in photo_entities_with_合影:
        print(f"    {pe['entity_id']}: desc={pe['description']}")

    if problematic_abstracts:
        abs_details = []
        for pa in problematic_abstracts:
            abs_details.append(f"'{pa['abstract']}' ({pa['reason']})")
        result.confirm(
            "P4", "MEDIUM",
            "abstract内容不当 — '合影'是代码推断，非照片实际内容",
            f"photos.db中有 {len(problematic_abstracts)} 条abstract包含代码推断的'合影': "
            + "; ".join(abs_details)
            + "。'合影'来自检测到多个人脸后的推断逻辑，而非照片实际场景描述。",
        )
    else:
        result.not_found("P4", "abstract内容不当", "未发现包含'合影'的abstract")

    # 额外: 检查图谱中photo实体的entity_id是否也包含"合影"
    if photo_entities_with_合影:
        eid_with_合影 = [pe["entity_id"] for pe in photo_entities_with_合影]
        print(f"  [注意] 图谱中photo实体ID也含'合影': {eid_with_合影}")
        print(f"    根因: sync_photo_to_kg 用abstract作为entity_id的一部分")


# ══════════════════════════════════════════════════════════════
#  P5: 旧照片实体未删除
# ══════════════════════════════════════════════════════════════

def diagnose_stale_entities(G: nx.DiGraph, db: sqlite3.Connection, result: DiagnosisResult) -> None:
    """诊断name_person后旧照片实体仍孤立的问题。"""
    print()
    print("-" * 60)
    print("  P5: 旧照片实体未删除诊断")
    print("-" * 60)

    # 从 photos.db 获取当前的 abstract (即期望的实体名)
    db_photos = [dict(r) for r in db.execute(
        "SELECT id, file_path, abstract, kg_synced FROM photos"
    ).fetchall()]

    # 收集所有 photo 类型和照片相关的节点
    photo_related_nodes = []
    for nid, attrs in G.nodes(data=True):
        etype = attrs.get("entity_type", "")
        eid = attrs.get("entity_id", nid)
        # photo 类型 或 含"合影" 或 文件名格式
        if etype == "photo":
            photo_related_nodes.append((nid, eid, attrs))
        elif "合影" in eid:
            photo_related_nodes.append((nid, eid, attrs))
        elif len(eid) >= 13 and eid.replace("_", "").replace(":", "").isdigit():
            photo_related_nodes.append((nid, eid, attrs))

    print(f"  图谱中照片相关实体: {len(photo_related_nodes)}")
    for nid, eid, attrs in photo_related_nodes:
        etype = attrs.get("entity_type", "?")
        neighbors = get_neighbors_info(G, nid)
        has_niu_remembers = any(
            n["neighbor"] == ROOT_ENTITY and "remembers" in n.get("keywords", "")
            for n in neighbors
        )
        # 也检查反向: Niu -> this node
        niu_node = find_node_by_entity_id(G, ROOT_ENTITY)
        has_niu_out = False
        if niu_node and G.has_edge(niu_node, nid):
            edge_kw = G.edges[niu_node, nid].get("keywords", "")
            if "remembers" in edge_kw:
                has_niu_out = True

        edge_count = len(neighbors)
        print(f"    [{etype}] {eid} | 边数={edge_count} | Niu->remembers={has_niu_out}")

    # 诊断: 找到与DB abstract不匹配的照片实体 (旧实体)
    stale_entities = []
    for photo in db_photos:
        db_abstract = photo.get("abstract", "")
        db_file_path = photo.get("file_path", "")
        db_kg_synced = photo.get("kg_synced", 0)

        # 如果 kg_synced=1，说明已同步过，但可能旧实体还在
        if db_kg_synced == 1:
            for nid, eid, attrs in photo_related_nodes:
                # 当前DB的abstract对应的实体 (新实体)
                if eid == db_abstract:
                    continue  # 这是当前正确的实体
                # 检查是否是同一照片的旧实体
                # 旧实体特征: 包含旧人物名(如"未命名人物_1")或文件名
                is_old = False
                if "未命名人物" in eid and "合影" in eid:
                    is_old = True
                # 文件名实体 (如 20090603_092316)
                file_stem = Path(db_file_path).stem if db_file_path else ""
                if eid == file_stem:
                    is_old = True

                if is_old:
                    # 检查这个旧实体是否是孤立的
                    niu_node = find_node_by_entity_id(G, ROOT_ENTITY)
                    has_niu_edge = False
                    if niu_node:
                        if G.has_edge(niu_node, nid):
                            has_niu_edge = True
                        if G.has_edge(nid, niu_node):
                            has_niu_edge = True

                    # 计算连接的边数
                    neighbors = get_neighbors_info(G, nid)
                    stale_entities.append({
                        "entity_id": eid,
                        "entity_type": attrs.get("entity_type", "?"),
                        "has_niu_edge": has_niu_edge,
                        "neighbor_count": len(neighbors),
                        "is_orphan": not has_niu_edge and len(neighbors) <= 2,
                    })

    # 另一种方式: 直接检查图谱中"未命名人物"开头的photo实体
    # 它们一定是旧实体 (因为name_person后人物名已更新)
    for nid, eid, attrs in photo_related_nodes:
        if eid.startswith("未命名人物") and "合影" in eid:
            already = any(s["entity_id"] == eid for s in stale_entities)
            if not already:
                niu_node = find_node_by_entity_id(G, ROOT_ENTITY)
                has_niu_edge = False
                if niu_node:
                    if G.has_edge(niu_node, nid) or G.has_edge(nid, niu_node):
                        has_niu_edge = True
                neighbors = get_neighbors_info(G, nid)
                stale_entities.append({
                    "entity_id": eid,
                    "entity_type": attrs.get("entity_type", "?"),
                    "has_niu_edge": has_niu_edge,
                    "neighbor_count": len(neighbors),
                    "is_orphan": not has_niu_edge,
                })

    # 也检查文件名实体 (如 20090603_092316) 是否为孤立碎片
    for nid, eid, attrs in photo_related_nodes:
        if len(eid) >= 13 and eid.replace("_", "").replace(":", "").isdigit():
            already = any(s["entity_id"] == eid for s in stale_entities)
            if not already:
                niu_node = find_node_by_entity_id(G, ROOT_ENTITY)
                has_niu_edge = False
                if niu_node:
                    if G.has_edge(niu_node, nid) or G.has_edge(nid, niu_node):
                        has_niu_edge = True
                neighbors = get_neighbors_info(G, nid)
                # 文件名实体: 没有 Niu->它的 remembers 边
                stale_entities.append({
                    "entity_id": eid,
                    "entity_type": attrs.get("entity_type", "?"),
                    "has_niu_edge": has_niu_edge,
                    "neighbor_count": len(neighbors),
                    "is_orphan": not has_niu_edge,
                })

    print(f"  疑似旧/孤立实体: {len(stale_entities)}")
    for s in stale_entities:
        print(f"    {s['entity_id']} [{s['entity_type']}] "
              f"| Niu边={s['has_niu_edge']} | 邻居数={s['neighbor_count']} "
              f"| 孤立={s['is_orphan']}")

    if stale_entities:
        orphans = [s for s in stale_entities if s["is_orphan"]]
        non_orphans = [s for s in stale_entities if not s["is_orphan"]]

        evidence_parts = []
        for s in stale_entities:
            status = "孤立" if s["is_orphan"] else "有边但应为旧实体"
            evidence_parts.append(f"{s['entity_id']}({status})")

        severity = "HIGH" if orphans else "MEDIUM"
        result.confirm(
            "P5", severity,
            "旧照片实体未删除 — name_person后旧实体仍存在",
            f"发现 {len(stale_entities)} 个旧/孤立照片实体: "
            + "; ".join(evidence_parts)
            + f"。其中 {len(orphans)} 个完全孤立(无Niu连接)。"
            + "根因: name_person更新abstract后重新sync_photo_to_kg，"
            + "但旧的实体未被删除或合并。",
        )
    else:
        result.not_found("P5", "旧照片实体未删除", "未发现旧/孤立照片实体")


# ══════════════════════════════════════════════════════════════
#  额外诊断: Niu根节点的remembers边
# ══════════════════════════════════════════════════════════════

def diagnose_niu_remembers(G: nx.DiGraph, result: DiagnosisResult) -> None:
    """诊断Niu根节点的remembers边是否合理。"""
    print()
    print("-" * 60)
    print("  额外: Niu根节点remembers边诊断")
    print("-" * 60)

    niu_node = find_node_by_entity_id(G, ROOT_ENTITY)
    if not niu_node:
        print("  [跳过] 未找到Niu根节点")
        return

    # Niu的出边
    remembers_targets = []
    for tgt in G.successors(niu_node):
        edge = G.edges[niu_node, tgt]
        keywords = edge.get("keywords", "")
        if "remembers" in keywords:
            tgt_eid = get_entity_id(G, tgt)
            tgt_type = get_entity_type(G, tgt)
            remembers_targets.append({
                "entity_id": tgt_eid,
                "entity_type": tgt_type,
                "keywords": keywords,
            })

    print(f"  Niu --[remembers]--> 目标数: {len(remembers_targets)}")
    for t in remembers_targets:
        print(f"    [{t['entity_type']}] {t['entity_id']} (keywords={t['keywords']})")

    # 检查: Niu是否直接remembers到照片实体 (而非通过brain region)
    photo_remembers = [t for t in remembers_targets
                       if t["entity_type"] == "photo"
                       or "合影" in t["entity_id"]]
    if photo_remembers:
        print(f"  [注意] Niu直接remembers到 {len(photo_remembers)} 个照片实体，"
              f"而非通过聊天历史脑区间接连接")

    # 检查: 是否有Niu -> 旧照片实体的 remembers 边
    stale_remembers = [t for t in remembers_targets
                       if "未命名人物" in t["entity_id"]]
    if stale_remembers:
        print(f"  [注意] Niu remembers 到 {len(stale_remembers)} 个旧'未命名人物'实体:")


# ══════════════════════════════════════════════════════════════
#  额外诊断: 脑区归属检查
# ══════════════════════════════════════════════════════════════

def diagnose_brain_region_attribution(G: nx.DiGraph) -> None:
    """诊断照片/人物实体的脑区归属是否正确。"""
    print()
    print("-" * 60)
    print("  额外: 脑区归属检查")
    print("-" * 60)

    # 照片/人物实体应该属于聊天历史脑区，而非直接挂在Niu下
    chat_brain = find_node_by_entity_id(G, "聊天历史脑区")

    # 找所有person和photo实体
    for nid, attrs in G.nodes(data=True):
        etype = attrs.get("entity_type", "")
        eid = attrs.get("entity_id", nid)
        if etype in ("person", "photo") or "合影" in eid:
            # 检查是否有聊天历史脑区 -> 此实体的边
            if chat_brain:
                has_brain_edge = G.has_edge(chat_brain, nid)
                edge_info = ""
                if has_brain_edge:
                    kw = G.edges[chat_brain, nid].get("keywords", "")
                    edge_info = f" (keywords={kw})"
                print(f"  [{etype}] {eid}: "
                      f"聊天历史脑区->{'有' if has_brain_edge else '无'}连接{edge_info}")


# ══════════════════════════════════════════════════════════════
#  主流程
# ══════════════════════════════════════════════════════════════

def main() -> None:
    print("=" * 70)
    print("  真实KG数据诊断 — 只读，不修改任何数据")
    print("=" * 70)
    print(f"  图谱文件: {GRAPHML_PATH}")
    print(f"  数据库:   {PHOTOS_DB}")

    # 加载数据
    G = load_graph()
    db = load_photos_db()

    result = DiagnosisResult()

    # 输出图谱概览
    print()
    print("-" * 60)
    print("  图谱概览")
    print("-" * 60)

    # 统计各类型节点数
    type_counts: dict[str, int] = {}
    for nid, attrs in G.nodes(data=True):
        etype = attrs.get("entity_type", "UNKNOWN")
        type_counts[etype] = type_counts.get(etype, 0) + 1

    print(f"  节点总数: {G.number_of_nodes()}")
    for etype, count in sorted(type_counts.items(), key=lambda x: -x[1]):
        print(f"    {etype}: {count}")
    print(f"  边总数: {G.number_of_edges()}")

    # 5项诊断
    diagnose_photo_fragmentation(G, result)
    diagnose_person_fragmentation(G, db, result)
    diagnose_edge_direction(G, result)
    diagnose_abstract_content(G, db, result)
    diagnose_stale_entities(G, db, result)

    # 额外诊断
    diagnose_niu_remembers(G, result)
    diagnose_brain_region_attribution(G)

    # 汇总
    result.report()

    # 关闭数据库
    db.close()

    # 返回码: 有确认问题返回1，否则返回0
    confirmed = [i for i in result.issues if i.status == "CONFIRMED"]
    sys.exit(1 if confirmed else 0)


if __name__ == "__main__":
    main()
