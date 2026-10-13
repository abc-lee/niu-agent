"""独立媒体向量存储（方案 D-12 / spec §5.3）：媒体向量只写本库，**永不**进 LightRAG。

不变量（I-3）：本模块是媒体向量的**唯一写入点**——`vdb_chunks` / `vdb_entities` /
`text_chunks` / 图谱节点边均不得出现媒体向量（repair 与一切重嵌入路径因此天然碰不到它）。
绑定键（D-16）：条目以**图谱实体名**（落盘路径的小写 stem）为键，与图谱实体节点永久绑定；
入库那一刻的 `file_path` 只是记录，事后文件移动/改名**不追、不管**（I-5：本模块没有、
也不允许有路径修复/迁移逻辑）。

存储：单个 JSON 文件（自持，不依赖 SQLite / LightRAG 任何存储原语）：
    {"dim": 768, "entries": {"<实体名>": {"entity_name", "media_type", "file_path", "vector"}}}
原子写（同目录临时文件 + os.replace，同 lightrag_integrity._atomic_write_json 先例）。
单写者假设：photo-server 进程是唯一写者（I-6 的降级包装在 T3 入库路径）。

并发契约：``add`` / ``add_many`` 用模块 ``threading.Lock`` 串行化整个 read-modify-write
——同进程并发调用不再丢更新（无锁时固定 ``.tmp`` 名会让后 replace 的线程整文件覆盖前者）；
跨进程不保证（跨进程多写者仍依赖上面的单写者假设）。只读操作（search / count / has）不取锁：
``os.replace`` 原子，读方恒见整份旧版或整份新版。

规模约束：本库形态是单 JSON + 全量重写 + 纯 Python 线扫，适用**数千条量级**——
约 5000 条 ≈ 78MB 文件（2026-10 本机实测）；逐条 ``add`` 每次全量重写整个文件
（实测 5000 条库单次 add 写盘 ~5s，5000 次逐条 add 累计写盘达数百 GB，故批量入库一律在循环外用 ``add_many``——
5000 条一次 add_many 实测 ~4s）；``search`` 为 O(N×768)，数千条实测 ~2s，勿在逐张照片循环里调用。
规模再往上须重评存储形态（spec D-12 形态已定，本模块不改）。
"""
from __future__ import annotations

import json
import math
import os
import threading
from pathlib import Path

#: 向量维度（embeddinggemma-2 文本/媒体同空间，与 spec D-1 的 768 维一致）。
DIM = 768

#: 测试接缝：设此环境变量则媒体库落该路径（仿 NIU_DB_PATH / NIU_MODELS_PATH 惯例）。
#: 生产环境无人设置 ⇒ 落家目录。T2/T3/T4 的测试一律经它重定向到临时目录。
_ENV_VAR = "NIU_MEDIA_STORE"

_DEFAULT_FILENAME = "media_vectors.json"

#: 模块级写锁：串行化 ``add`` / ``add_many`` 的 read-modify-write（并发契约见模块 docstring）。
_store_lock = threading.Lock()


def media_store_path() -> Path:
    """媒体向量库文件路径。**每次现读**（无缓存）：先看环境变量 ``NIU_MEDIA_STORE``
    （设有则用它，须为绝对路径），否则 ``~/.niu/media_vectors.json``。"""
    env = os.environ.get(_ENV_VAR)
    if env:
        p = Path(env)
        if not p.is_absolute():
            raise ValueError(f"{_ENV_VAR} 必须是绝对路径: {p}")
        return p
    return Path.home() / ".niu" / _DEFAULT_FILENAME


def _load_store() -> dict[str, dict]:
    """读取本库条目表（实体名 → 条目）。文件不存在 → 空表（不抛）；损坏 → 向上抛（fail-loud）。"""
    path = media_store_path()
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"媒体向量库格式损坏（非 dict）: {path}")
    entries = raw.get("entries", {})
    if not isinstance(entries, dict):
        raise ValueError(f"媒体向量库格式损坏（entries 非 dict）: {path}")
    return entries


def _atomic_write_store(path: Path, entries: dict[str, dict]) -> None:
    """原子写：同目录临时文件 + os.replace（写一半崩溃不损坏既有库）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"dim": DIM, "entries": entries}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _check_vector(vector: object, context: str) -> None:
    actual = len(vector) if hasattr(vector, "__len__") else "非序列"
    if not isinstance(vector, (list, tuple)) or len(vector) != DIM:
        raise ValueError(f"{context}: 向量维度须为 {DIM}，实际 {actual}")


def _validate_writable(entity_name: str, vector: object, context: str) -> None:
    """写侧校验（比 search 读侧更严）：绑定键非空、维度 = DIM、元素全有限、非零向量。

    全零向量永远不会被检索到（search 读侧 v_norm==0 直接跳过）；NaN/inf 一旦入库会污染
    分数排序 / 产生未定义范数——写侧一律拒绝，不静默存下。
    """
    if not entity_name:
        raise ValueError("entity_name 不得为空（绑定键是图谱实体名）")
    _check_vector(vector, context)
    if all(float(x) == 0.0 for x in vector):
        raise ValueError(f"{context}: 向量不得全为零向量（零向量永不可检索，不应入库）")
    if any(not math.isfinite(float(x)) for x in vector):
        raise ValueError(f"{context}: 向量不得含 NaN/inf（NaN 污染分数排序，inf 范数未定义）")


def add_many(items: list[tuple[str, str, list[float], str]]) -> int:
    """批量写入：每条为 ``entity_name, media_type, vector, file_path`` 四元组。

    整批**一次 read-modify-write** 完成（N 条 vs. N 次单条 ``add`` 的 N 次全文件重写，
    省 ~N-1 次整文件写盘）——批量入库（如 T3 的逐照片嵌入循环）必须用它。

    语义：先整批校验（空绑定键 / 维度不符 / 全零向量 / NaN-inf → 整批抛 ValueError、
    一条不落，不留部分写）；批内同 entity_name 为覆盖（后者胜，与 ``add`` 一致）。
    返回写入条数（= 批内去重后的 entity_name 数；空批返回 0 且不动文件）。

    并发契约：read-modify-write 由模块锁串行化——同进程并发 ``add_many`` 不丢更新；
    跨进程不保证（依赖单写者假设，见模块 docstring）。
    """
    if not items:
        return 0
    for item in items:
        entity_name, _media_type, vector, _file_path = item
        _validate_writable(entity_name, vector, "add_many")
    with _store_lock:
        entries = _load_store()
        for entity_name, media_type, vector, file_path in items:
            entries[entity_name] = {
                "entity_name": entity_name,
                "media_type": media_type,
                "file_path": file_path,
                "vector": [float(x) for x in vector],
            }
        _atomic_write_store(media_store_path(), entries)
    return len({item[0] for item in items})


def add(entity_name: str, media_type: str, vector: list[float], file_path: str) -> None:
    """写入一条媒体向量（**同 entity_name 覆盖**，不追加）。``add_many`` 的单条薄包装。

    写侧校验：空 entity_name / 维度不符 / 全零向量 / NaN-inf → ValueError（不静默写入）。
    ``file_path`` 是入库那一刻的记录（D-16/I-5）。

    并发契约：read-modify-write 由模块锁串行化——同进程并发 ``add`` 不丢更新；跨进程
    不保证（依赖单写者假设）。批量场景请改用 ``add_many``（见模块 docstring 规模约束）。
    """
    add_many([(entity_name, media_type, vector, file_path)])


def search(query_vector: list[float], top_k: int) -> list[dict]:
    """独立检索（D-13：只在本库内计分排序，不与任何知识检索分数融合）。

    返回每项 ``{entity_name, media_type, file_path, score}``，**按 cosine 降序**；
    **cosine 并列时按 ``entity_name`` 升序 tie-break**（确定性输出，与插入顺序无关）；
    最多 ``top_k`` 条；库为空/文件不存在 → ``[]``（不抛）。维度不符 → ValueError。
    实体名取 dict 键（绑定键）——条目值里的同名字段只是冗余副本，缺失不炸检索。

    规模约束：O(N×768) 纯 Python 线扫，适用数千条量级（约 5000 条需 1-2s）；
    勿在逐照片循环里调用（完整规模上限见模块 docstring）。
    """
    _check_vector(query_vector, "search")
    if top_k <= 0:
        return []
    q = [float(x) for x in query_vector]
    q_norm = math.sqrt(sum(x * x for x in q))
    if q_norm == 0.0:
        return []

    hits: list[dict] = []
    for name, entry in _load_store().items():
        vec = entry.get("vector")
        if not isinstance(vec, list) or len(vec) != DIM:
            continue  # 单条损坏不阻断整个检索（读侧容错；写侧严格）
        v = [float(x) for x in vec]
        v_norm = math.sqrt(sum(x * x for x in v))
        if v_norm == 0.0:
            continue
        score = sum(a * b for a, b in zip(q, v)) / (q_norm * v_norm)
        hits.append({
            "entity_name": name,  # 取遍历键（绑定键），不依赖条目值里的冗余同名副本
            "media_type": entry.get("media_type", ""),
            "file_path": entry.get("file_path", ""),
            "score": score,
        })
    hits.sort(key=lambda h: (-h["score"], h["entity_name"]))
    return hits[:top_k]


def count() -> int:
    """已存条目数。文件不存在 → 0（不抛）。"""
    return len(_load_store())


def has(entity_name: str) -> bool:
    """该实体是否已有媒体向量（入库去重用）。文件不存在 → False（不抛）。"""
    return entity_name in _load_store()
