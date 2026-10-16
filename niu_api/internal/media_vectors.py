"""独立媒体向量存储（纯索引；方案 D-12/D-13/D-23）：媒体向量只写本库，**永不**进 LightRAG。

不变量（I-3）：本模块是媒体向量的**唯一写入点**——`vdb_chunks` / `vdb_entities` /
`text_chunks` / 图谱节点边均不得出现媒体向量（repair 与一切重嵌入路径因此天然碰不到它）。
绑定键（D-16）：条目以**图谱实体名**（落盘路径的小写 stem）为键，与图谱实体节点永久绑定。
v3.0 纯索引（D-23）：**不复制落盘路径**——路径与描述在查询时从图谱节点取（不复制、防漂移）。

旧形态库文件自愈：v2.x 条目含 `file_path` 键；写侧从此不再含该字段，且**任意一次写入**都会把
全部条目归一化为三键后整文件重写，完成形态升级；读侧对旧条目残留的多余字段**容忍忽略**
（无需迁移路径）。

存储：单个 JSON 文件（自持，不依赖 SQLite / LightRAG 任何存储原语）：
    {"dim": 768, "entries": {"<实体名>": {"entity_name", "media_type", "vector"}}}
原子写（同目录临时文件 + os.replace，同 lightrag_integrity._atomic_write_json 先例）。
同进程写者：v3.0 下同进程写者 = 后台工作者（调度器既有线程池内）+ 删照片清理（``remove``），由模块锁串行化；查询侧只读。

并发契约：``add`` / ``add_many`` / ``remove`` 用模块 ``threading.Lock`` 串行化整个 read-modify-write——
同进程并发调用不再丢更新；跨进程不保证（单写者假设）。只读操作（search / count / has）不取锁：
``os.replace`` 原子，读方恒见整份旧版或整份新版。

规模约束：单 JSON + 全量重写 + 纯 Python 线扫，适用**数千条量级**（v2.x 实测：约 5000 条
≈ 78MB 文件；逐条 add 每次全量重写，勿在逐张照片循环里用——v3.0 下写入者是后台 FIFO，
天然一张一写、节奏由编码耗时决定）。``search`` 为 O(N×768)，数千条实测 ~2s。
规模再往上须重评存储形态（spec D-12 形态已定，本模块不改）。
"""
from __future__ import annotations

import json
import math
import os
import threading
from loguru import logger
from pathlib import Path

#: 向量维度（embeddinggemma-2 文本/媒体同空间，与 spec D-1 的 768 维一致）。
DIM = 768

#: 测试接缝：设此环境变量则媒体库落该路径（仿 NIU_DB_PATH / NIU_MODELS_PATH 惯例）。
#: 生产环境无人设置 ⇒ 落家目录。测试一律经它重定向到临时目录。
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
    """读取本库条目表（实体名 → 条目）。文件不存在 → 空表（不抛）；损坏 → 向上抛（fail-loud）。
    旧形态条目（含 v2.x 残留的 `file_path` 键）原样返回——读侧容忍，写侧归一化。"""
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


def _normalize_entries(entries: dict[str, dict]) -> dict[str, dict]:
    """写侧全量归一化（D-23 旧形态自愈）：每条条目只保留三键，
    旧形态的 `file_path` 等残留键在此一次性清除（整文件重写即完成形态升级）。

    无法归一化的毒条目（条目值非 dict / 向量非 DIM 维数字序列 / 元素非数值）**丢弃 + WARNING**：
    v2.x 写侧校验产不出这种条目（只能来自手工损坏/异常写入），读侧本就容忍；
    若保留，此后每次写入都先炸在归一化上、自愈永远完成不了——写路径被永久卡死。
    """
    cleaned: dict[str, dict] = {}
    for name, entry in entries.items():
        vector = entry.get("vector") if isinstance(entry, dict) else None  # 条目值非 dict（手工损坏）⇒ 按毒条目丢弃，不穿透
        if not isinstance(vector, (list, tuple)) or len(vector) != DIM:
            logger.warning(f"[MediaVectors] 自愈时丢弃毒条目（向量非 {DIM} 维序列）: {name}")
            continue
        try:
            cleaned[name] = {
                "entity_name": name,
                "media_type": entry.get("media_type", "image"),
                "vector": [float(x) for x in vector],
            }
        except (TypeError, ValueError):
            logger.warning(f"[MediaVectors] 自愈时丢弃毒条目（向量元素非数值）: {name}")
    return cleaned


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


def add_many(items: list[tuple[str, str, list[float]]]) -> int:
    """批量写入：每条为 ``entity_name, media_type, vector`` 三元组（纯索引，D-23）。

    整批**一次 read-modify-write** 完成；整批先校验（空绑定键 / 维度不符 / 全零向量 /
    NaN-inf → 整批抛 ValueError、一条不落，不留部分写）；批内同 entity_name 为覆盖。
    返回写入条数（= 批内去重后的 entity_name 数；空批返回 0 且不动文件）。
    写入前对全部条目做三键归一化（D-23 旧形态自愈）。
    """
    if not items:
        return 0
    for item in items:
        entity_name, _media_type, vector = item
        _validate_writable(entity_name, vector, "add_many")
    with _store_lock:
        entries = _load_store()
        for entity_name, media_type, vector in items:
            entries[entity_name] = {
                "entity_name": entity_name,
                "media_type": media_type,
                "vector": [float(x) for x in vector],
            }
        _atomic_write_store(media_store_path(), _normalize_entries(entries))
    return len({item[0] for item in items})


def add(entity_name: str, media_type: str, vector: list[float]) -> None:
    """写入一条媒体向量（**同 entity_name 覆盖**，不追加）。``add_many`` 的单条薄包装。"""
    add_many([(entity_name, media_type, vector)])


def remove(entity_name: str) -> bool:
    """移除一条媒体向量（删照片清理，D-4）。``add_many`` 的逆操作（不写向量）。

    持 ``_store_lock`` 做读-改-写 + 原子落盘（与 ``add_many`` 同一把锁，防并发丢更新）；
    写侧校验只复用「绑定键非空」这半段——``_validate_writable`` 要求向量参数，``remove``
    没有向量、不能原样调用。键不存在 ⇒ 返回 ``False``（不落盘、不创建文件，与
    ``has`` / ``count`` 读侧容错风格一致）；文件损坏仍 fail-loud（``_load_store`` 原语义）。
    """
    if not entity_name:
        raise ValueError("entity_name 不得为空（绑定键是图谱实体名）")
    with _store_lock:
        entries = _load_store()
        if entity_name not in entries:
            return False
        del entries[entity_name]
        _atomic_write_store(media_store_path(), _normalize_entries(entries))
    return True


def search(query_vector: list[float], top_k: int) -> list[dict]:
    """独立检索（D-13：只在本库内计分排序，不与任何知识检索分数融合/比较）。

    命中项 = ``{entity_name, media_type, score}``（纯索引，无 file_path——D-23）。
    库空 / 文件不存在 → 返回 []（不抛）。``top_k <= 0`` → 返回 []（入口守卫，与 v2.x 同型）。
    查询向量维度不符 → 抛 ValueError（同写侧口径）。
    逐条容错（v2.x 保留）：单条毒条目（非 list / 维度不符）跳过、不阻断整个检索。
    cosine 并列时按 entity_name 升序（确定性排序）。
    """
    _check_vector(query_vector, "search")
    if top_k <= 0:
        return []
    entries = _load_store()
    q_norm = math.sqrt(sum(float(x) * float(x) for x in query_vector))
    if q_norm == 0.0:
        return []
    hits: list[dict] = []
    for name, entry in entries.items():
        vector = entry.get("vector")
        if not isinstance(vector, (list, tuple)) or len(vector) != DIM:
            continue  # 单条损坏不阻断整个检索（读侧容错；写侧严格）
        v_norm = math.sqrt(sum(float(x) * float(x) for x in vector))
        if v_norm == 0.0:
            continue
        cos = sum(float(a) * float(b) for a, b in zip(query_vector, vector)) / (q_norm * v_norm)
        hits.append({"entity_name": name, "media_type": entry.get("media_type", "image"), "score": cos})
    hits.sort(key=lambda h: (-h["score"], h["entity_name"]))
    return hits[:top_k]


def count() -> int:
    """已存条目数。文件不存在 → 0（不抛）。"""
    return len(_load_store())


def has(entity_name: str) -> bool:
    """该实体是否已有媒体向量（入库去重用）。文件不存在 → False（不抛）。"""
    return entity_name in _load_store()
