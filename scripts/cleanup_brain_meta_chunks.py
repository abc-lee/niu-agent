#!/usr/bin/env python3
"""一次性存量清理工具：删除脑区记账 meta chunk（无入边孤儿）。

背景（docs/superpowers/reviews/2026-10-16-brain-region-chunk-leak-analysis.md §6）：
LightRAG 脑区记账 description 里的易变字段导致 content 变化 → chunk id 变化 →
旧虚拟 chunk 从不被回收。写入侧止血已完成（lightrag_adapter._strip_brain_meta），
本工具只清理存量。

删除判据（安全关键，勿改）：
  候选   = text_chunks 里 content 含 "brain_meta_region_id:" 的条目（与 file_path 无关）
  活引用 = graphml 全文正则抽 chunk-[0-9a-f]{32}（保守取法，宁可多算活跃、少删）
  删除集 = 候选 ∖ 活引用（只删无入边孤儿；被引用的那 7 条是"当前"虚拟 chunk，
           删了会让脑区实体 source_id 变 UNKNOWN）
只动 kv_store_text_chunks.json 与 vdb_chunks.json（同一 id 的两份存储，必须同批删）；
vdb_entities / vdb_relationships / graphml 只读，绝不改动。

存储接入：走 lightrag_repair.py 同款链路（JsonKVStorage / NanoVectorDBStorage
+ initialize_share_data + index_done_callback 显式落盘）。删除路径不计算
embedding，因此 embedding_func 用"只带 embedding_dim 的桩"（dim 从存量
vdb_chunks.json 读出，满足 NanoVectorDB 的 dim 断言），不加载任何模型。

用法：
  python/bin/python scripts/cleanup_brain_meta_chunks.py            # 真库 dry-run
  python/bin/python scripts/cleanup_brain_meta_chunks.py --apply    # 真库实删（先自动备份）
  --root <dir>   指向别的 storage 目录（自测用 /tmp 假数据）
  --port/--host  覆盖 Niu 端口守卫（自测用假监听端口）
  --backup-dir   覆盖备份目录（默认 ~/.niu/backup-<YYYYMMDD>-brainmeta-cleanup/；
                 --root 非默认根时退到 <root 父目录>/backup-<YYYYMMDD>-brainmeta-cleanup/）
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import shutil
import socket
import sys
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree

MARKER = "brain_meta_region_id:"
CHUNK_ID_RE = re.compile(r"chunk-[0-9a-f]{32}")
GRAPHML = "graph_chunk_entity_relation.graphml"
TEXT_KV = "kv_store_text_chunks.json"
VDB_CHUNKS = "vdb_chunks.json"
BACKUP_FILES = (TEXT_KV, VDB_CHUNKS, GRAPHML)

DEFAULT_ROOT = Path.home() / ".niu" / "lightrag_storage"


def port_in_use(host: str, port: int, timeout: float = 1.0) -> bool:
    """判断 (host, port) 是否有进程在监听（Niu 主进程监听 9876）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_vdb_ids(path: Path) -> set[str]:
    """读 vdb_chunks.json 的 __id__ 集合（不经过 storage 类，避免任何初始化副作用）。"""
    if not path.exists():
        return set()
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {entry["__id__"] for entry in data.get("data", [])}


def read_vdb_dim(path: Path) -> int | None:
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("embedding_dim")


def read_kv_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return set(data.keys())


def graphml_counts(path: Path) -> tuple[int, int]:
    """graphml 节点数 / 边数（只读解析）。"""
    tree = ElementTree.parse(path)
    nodes = edges = 0
    for el in tree.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "node":
            nodes += 1
        elif tag == "edge":
            edges += 1
    return nodes, edges


def graphml_live_ids(path: Path) -> set[str]:
    """保守取法：graphml 全文正则抽 chunk-[0-9a-f]{32}。"""
    return set(CHUNK_ID_RE.findall(path.read_text(encoding="utf-8")))


def backup_files(root: Path, backup_dir: Path) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    for name in BACKUP_FILES:
        src = root / name
        if src.exists():
            shutil.copy2(src, backup_dir / name)


class _DimOnlyEmbedding:
    """embedding 桩：删除路径从不计算 embedding，只暴露 embedding_dim
    （NanoVectorDB 构造时的 dim 断言用）。若被调用说明流程走偏，立即炸响。"""

    def __init__(self, embedding_dim: int):
        self.embedding_dim = embedding_dim

    def __call__(self, *args, **kwargs):
        raise RuntimeError("cleanup_brain_meta_chunks 只做删除，不应计算 embedding")


async def run(root: Path, apply: bool, backup_dir: Path, host: str, port: int) -> int:
    # ---- 前置守卫（dry-run 同样拦截：程序运行时文件可能正被写）----
    if port_in_use(host, port):
        print(f"[守卫] {host}:{port} 正在监听 ⇒ Niu 正在运行，先关程序再跑本工具。")
        return 2

    text_path = root / TEXT_KV
    vdb_path = root / VDB_CHUNKS
    graphml_path = root / GRAPHML
    for p in (text_path, vdb_path, graphml_path):
        if not p.exists():
            print(f"[守卫] 缺少存储文件：{p}")
            return 2

    # ---- 删除集计算（只读）----
    with open(text_path, encoding="utf-8") as f:
        text_kv = json.load(f)
    candidate_ids = {
        cid
        for cid, entry in text_kv.items()
        if isinstance(entry, dict) and MARKER in str(entry.get("content", ""))
    }
    live_ids = graphml_live_ids(graphml_path)
    delete_ids = candidate_ids - live_ids
    protected = candidate_ids & live_ids

    nodes, edges = graphml_counts(graphml_path)
    vdb_entities_n = 0
    p = root / "vdb_entities.json"
    if p.exists():
        with open(p, encoding="utf-8") as f:
            vdb_entities_n = len(json.load(f).get("data", []))
    vdb_rel_n = 0
    p = root / "vdb_relationships.json"
    if p.exists():
        with open(p, encoding="utf-8") as f:
            vdb_rel_n = len(json.load(f).get("data", []))

    def print_state(tag: str, text_ids: set[str], vdb_ids: set[str]) -> None:
        print(f"\n===== {tag} =====")
        print(f"text_chunks 条数 : {len(text_ids)}")
        print(f"vdb_chunks  条数 : {len(vdb_ids)}")
        only_text = sorted(text_ids - vdb_ids)
        only_vdb = sorted(vdb_ids - text_ids)
        if not (only_text or only_vdb):
            print("id 集合一致      : 是（差集为空）")
        else:
            print(f"id 集合一致      : 否（仅文本 {len(only_text)} 条 / 仅向量 {len(only_vdb)} 条）")
            for x in (only_text + only_vdb)[:10]:
                print(f"  - {x}")
        # 残留检查：text_kv 里仍含指纹的条目（清理后应只剩活引用的那批）
        marker_left = {
            cid
            for cid in text_ids
            if cid in text_kv
            and isinstance(text_kv[cid], dict)
            and MARKER in str(text_kv[cid].get("content", ""))
        }
        print(f"content 含 {MARKER!r} 残留 : {len(marker_left)} 条"
              + (f"（应等于活引用候选 {len(protected)} 条）" if tag.startswith("清理后") else ""))
        print(f"vdb_entities {vdb_entities_n} 条 / vdb_relationships {vdb_rel_n} 条 / "
              f"graphml 节点 {nodes} / 边 {edges}（只读，不应变化）")

    print("===== 判据汇总 =====")
    print(f"候选（content 含 {MARKER!r}）: {len(candidate_ids)}")
    print(f"活引用（graphml 正则抽出）   : {len(live_ids)}")
    print(f"删除集（候选 ∖ 活引用）      : {len(delete_ids)}")
    print(f"受保护（候选 ∩ 活引用）      : {len(protected)}")
    if delete_ids and not apply:
        sample = sorted(delete_ids)[:5]
        print("删除集示例（前 5）: " + ", ".join(sample))
    print_state("清理前", set(text_kv), read_vdb_ids(vdb_path))

    if not delete_ids:
        print("\n[结果] 无可删孤儿（候选全被引用或本就不存在），无操作。")
        return 0

    if not apply:
        after_text = set(text_kv) - delete_ids
        after_vdb = read_vdb_ids(vdb_path) - delete_ids
        print_state("清理后（模拟）", after_text, after_vdb)
        print("\n[dry-run] 未写盘。确认无误后加 --apply 实删（会先自动备份）。")
        return 0

    # ---- --apply：备份 → 双存储同批删 → 显式落盘 ----
    backup_files(root, backup_dir)
    print(f"\n[备份] {', '.join(BACKUP_FILES)} → {backup_dir}")

    # storage 链路（照 niu_api/internal/lightrag_repair.py 的初始化方式）
    from lightrag.kg.json_kv_impl import JsonKVStorage
    from lightrag.kg.nano_vector_db_impl import NanoVectorDBStorage
    from lightrag.kg.shared_storage import initialize_share_data, set_default_workspace
    from lightrag.namespace import NameSpace

    initialize_share_data(workers=1)
    set_default_workspace("")

    global_config = {
        "working_dir": str(root),
        "vector_db_storage_cls_kwargs": {"cosine_better_than_threshold": 0.2},
        "embedding_batch_num": 32,
    }
    text_storage = JsonKVStorage(
        namespace=NameSpace.KV_STORE_TEXT_CHUNKS,
        workspace="",
        global_config=global_config,
        embedding_func=None,  # type: ignore[arg-type]
    )
    await text_storage.initialize()
    mem_ids = set(text_storage._data)
    if mem_ids != set(text_kv):
        print(f"[守卫] text_chunks 内存态({len(mem_ids)})与磁盘({len(text_kv)})不一致，中止（可能有其他进程在写）。")
        return 2

    dim = read_vdb_dim(vdb_path)
    vdb_storage = NanoVectorDBStorage(
        namespace=NameSpace.VECTOR_STORE_CHUNKS,
        workspace="",
        global_config=global_config,
        embedding_func=_DimOnlyEmbedding(dim if dim is not None else 768),
        meta_fields={"full_doc_id", "content", "file_path"},
    )
    await vdb_storage.initialize()

    await text_storage.delete(sorted(delete_ids))
    await vdb_storage.delete(sorted(delete_ids))
    # 显式落盘（教训：nano-vectordb 裸 delete 不落盘）
    await text_storage.index_done_callback()
    vdb_flush = await vdb_storage.index_done_callback()
    if not vdb_flush:
        print("[错误] vdb_chunks 落盘失败（可能被其他进程更新），请人工核对后重跑。")
        return 3

    # 落盘后从磁盘重新读，验证确实生效
    after_text_ids = read_kv_ids(text_path)
    after_vdb_ids = read_vdb_ids(vdb_path)
    print_state("清理后（实删，读自磁盘）", after_text_ids, after_vdb_ids)
    if after_text_ids != after_vdb_ids:
        print("\n[错误] 清理后两边 id 集合不一致，请人工核对。")
        return 3
    print(f"\n[完成] 实删 {len(delete_ids)} 条孤儿 meta chunk（text_chunks + vdb_chunks 同批）。")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="一次性清理脑区记账 meta chunk（content 含 brain_meta_region_id: 且无入边的孤儿）。默认 dry-run。",
    )
    ap.add_argument("--root", default=str(DEFAULT_ROOT),
                    help=f"storage 目录（默认 {DEFAULT_ROOT}；自测可指 /tmp 假数据）")
    ap.add_argument("--apply", action="store_true",
                    help="实删（默认只演练不写盘；--apply 前会自动备份三个文件）")
    ap.add_argument("--host", default="127.0.0.1", help="Niu 守卫 host（默认 127.0.0.1）")
    ap.add_argument("--port", type=int, default=9876, help="Niu 守卫 port（默认 9876）")
    ap.add_argument("--backup-dir", default=None,
                    help="备份目录（默认 ~/.niu/backup-<YYYYMMDD>-brainmeta-cleanup/；"
                         "--root 非默认根时退到 <root 父目录> 下的同名目录）")
    args = ap.parse_args(argv)

    root = Path(args.root).expanduser().resolve()
    if args.backup_dir:
        backup_dir = Path(args.backup_dir).expanduser().resolve()
    elif root == DEFAULT_ROOT.resolve():
        backup_dir = Path.home() / ".niu" / f"backup-{datetime.now():%Y%m%d}-brainmeta-cleanup"
    else:
        backup_dir = root.parent / f"backup-{datetime.now():%Y%m%d}-brainmeta-cleanup"

    try:
        return asyncio.run(run(root, args.apply, backup_dir, args.host, args.port))
    except KeyboardInterrupt:
        print("\n[中断] 用户取消。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
