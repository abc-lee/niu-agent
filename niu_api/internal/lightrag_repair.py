"""LightRAG 外挂修复（v2 真实格式+数据源）

修复策略（按优先级）：
1. 从损坏 vdb 自身的 data 字段读文本（matrix 损坏 data 完好是常见场景）
2. data 也损坏时 fallback：
   - chunks → kv_store_text_chunks.json（有 content 字段）
   - entities/relations → GraphML 图节点 attributes（TODO，暂不支持）

vector 字段编码：base64(zlib(float16 bytes)) 三层（跟 LightRAG 一致）
matrix 字段编码：base64(float32 bytes) 一层

_embed_text 不依赖 LightRAG 实例，直接用 niu_api.internal.embedding 预加载的模型。
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import zlib
from pathlib import Path
from typing import Any

from loguru import logger

_STORAGE_DIR = Path.home() / ".niu" / "lightrag_storage"

# vdb 文件名 → 文本字段名（vdb data 内部）
_VDB_TEXT_FIELD = {
    "vdb_entities.json": "content",
    "vdb_relationships.json": "content",
    "vdb_chunks.json": "content",
}

# data 损坏时的 fallback 数据源
_VDB_FALLBACK_KV = {
    "vdb_chunks.json": ("kv_store_text_chunks.json", "content"),
    # entities/relations 的 fallback 是 GraphML，暂不支持（返回 error）
}


def _embed_text(text: str) -> list[float]:
    """用预加载的 embedding 模型生成向量，不依赖 LightRAG 实例。

    embedding 模型在 __main__.py 启动时预加载（LightRAG eager init 之前）。
    """
    try:
        from niu_api.internal.embedding import get_model
        model = get_model()
        if model is None:
            raise RuntimeError("embedding 模型未加载")
        vec = model.encode([text])
        return vec[0].tolist()
    except ImportError:
        # fallback：用 LightRAG 实例（如果已初始化）
        from niu_api.internal.lightrag_manager import get_lightrag
        rag = get_lightrag()
        if rag is None:
            raise RuntimeError("embedding 模型未加载且 LightRAG 未初始化")
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(rag.embedding_func([text]))
            return result[0].tolist()
        finally:
            loop.close()


def _encode_vector(vec_f16) -> str:
    """vector 字段三层编码：base64(zlib(float16 bytes))"""
    import numpy as np
    arr = np.array(vec_f16, dtype=np.float16) if not hasattr(vec_f16, 'astype') else vec_f16.astype(np.float16)
    return base64.b64encode(zlib.compress(arr.tobytes())).decode()


def _encode_matrix(matrix_f32) -> str:
    """matrix 字段一层编码：base64(float32 bytes)"""
    import numpy as np
    arr = np.array(matrix_f32, dtype=np.float32) if not hasattr(matrix_f32, 'astype') else matrix_f32.astype(np.float32)
    return base64.b64encode(arr.tobytes()).decode()


def _storage_dir() -> Path:
    """获取当前 _STORAGE_DIR（兼容 str / Path 两种被 monkeypatch 的形式）。"""
    return Path(_STORAGE_DIR)


def _read_data_from_vdb(vdb_filename: str) -> list[dict] | None:
    """尝试从损坏 vdb 的 data 字段读文本（matrix 损坏 data 完好场景）。

    Returns:
        data 列表（含 __id__ + content），如果 data 也损坏返回 None。
    """
    vdb_path = _storage_dir() / vdb_filename
    if not vdb_path.exists():
        return None
    try:
        with open(vdb_path, encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, Exception):
        return None  # data 也损坏
    data_list = raw.get("data")
    if not isinstance(data_list, list) or not data_list:
        return None
    text_field = _VDB_TEXT_FIELD.get(vdb_filename, "content")
    # 验证 data 里的文本字段可用
    valid = [item for item in data_list if isinstance(item, dict) and item.get(text_field)]
    return valid if valid else None


def _read_data_from_kv_store(vdb_filename: str) -> tuple[list[dict] | None, str | None]:
    """data 损坏时从 fallback kv_store 读文本。

    Returns:
        (data_list, source_name)：data_list 为 None 表示无可用 fallback。
        source_name 用作 repair_vdb 返回值里的 source 字段（值如 "kv_store_text_chunks"）。
    """
    fallback = _VDB_FALLBACK_KV.get(vdb_filename)
    if not fallback:
        return None, None  # entities/relations 暂不支持 fallback
    kv_filename, text_field = fallback
    kv_path = _storage_dir() / kv_filename
    if not kv_path.exists():
        return None, None
    try:
        with open(kv_path, encoding="utf-8") as f:
            kv_data = json.load(f)
    except (json.JSONDecodeError, Exception):
        return None, None
    data_list = []
    for key, value in kv_data.items():
        if isinstance(value, dict):
            text = value.get(text_field)
            if text:
                data_list.append({"__id__": key, "content": text})
    # source 用 fallback kv 文件主名（去 .json 后缀），跟测试期望对齐
    source_name = kv_filename[:-5] if kv_filename.endswith(".json") else kv_filename
    return (data_list if data_list else None), source_name


def repair_vdb(vdb_filename: str) -> dict[str, Any]:
    """修复单个 vdb 文件。

    优先从 vdb 自身 data 字段重建，fallback 到 kv_store。

    Returns:
        {"status": "ok"|"error", "rebuilt_count": int, "source": str, "message": str}
    """
    if vdb_filename not in _VDB_TEXT_FIELD:
        return {"status": "error", "message": f"未知的 vdb 文件: {vdb_filename}"}

    # 1. 优先从 vdb data 字段读
    data_list = _read_data_from_vdb(vdb_filename)
    source = "vdb_data_field" if data_list else None

    # 2. fallback 到 kv_store
    if not data_list:
        data_list, fallback_source = _read_data_from_kv_store(vdb_filename)
        if data_list:
            source = fallback_source

    if not data_list:
        return {
            "status": "error",
            "message": f"无可用数据源重建 {vdb_filename}（vdb data 和 fallback kv_store 都损坏）",
        }

    # 3. 重新 embedding（保留原 data 所有非 vector 字段，只重算 vector）
    import numpy as np
    text_field = _VDB_TEXT_FIELD.get(vdb_filename, "content")
    new_data = []
    vectors = []
    for item in data_list:
        text = item.get(text_field, "")
        try:
            vec = _embed_text(text)
        except Exception as e:
            logger.warning(f"[LightRAGRepair] embedding 失败 {item.get('__id__')}: {e}，跳过")
            continue
        # 保留原 data 所有非 vector 字段（__created_at__ / entity_name / src_id / tgt_id / source_id / full_doc_id / file_path 等）
        # 只重算 vector 字段
        new_item = {k: v for k, v in item.items() if k != "vector"}
        new_item["vector"] = _encode_vector(np.array(vec, dtype=np.float16))
        new_data.append(new_item)
        vectors.append(vec)

    if not new_data:
        return {"status": "error", "message": "embedding 全部失败，无数据可重建"}

    embedding_dim = len(vectors[0])
    matrix_f32 = np.array(vectors, dtype=np.float32)

    # 4. 备份损坏 vdb 到 .corrupt.bak
    vdb_path = _storage_dir() / vdb_filename
    if vdb_path.exists():
        corrupt_bak = _storage_dir() / f"{vdb_filename}.corrupt.bak"
        try:
            if corrupt_bak.exists():
                corrupt_bak.unlink()
            vdb_path.rename(corrupt_bak)
            logger.info(f"[LightRAGRepair] 损坏 vdb 备份到: {corrupt_bak}")
        except Exception as e:
            logger.warning(f"[LightRAGRepair] 备份损坏 vdb 失败: {e}")

    # 5. 原子写新 vdb
    storage = {
        "embedding_dim": embedding_dim,
        "data": new_data,
        "matrix": _encode_matrix(matrix_f32),
    }
    tmp_file = vdb_path.with_suffix(".json.tmp")
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(storage, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_file, vdb_path)

    logger.info(f"[LightRAGRepair] 重建 {vdb_filename}: {len(new_data)} 条 (source={source})")
    return {
        "status": "ok",
        "rebuilt_count": len(new_data),
        "source": source,
        "message": f"从 {source} 重建 {len(new_data)} 条",
    }


def repair_kv_store(kv_filename: str) -> dict[str, Any]:
    """kv_store 损坏时从 .bak 恢复。"""
    kv_path = _storage_dir() / kv_filename
    bak_path = _storage_dir() / f"{kv_filename}.bak"
    if not bak_path.exists():
        return {"status": "error", "message": f"备份文件不存在: {bak_path}"}
    try:
        shutil.copy2(bak_path, kv_path)
        logger.info(f"[LightRAGRepair] 从 .bak 恢复 {kv_filename}")
        return {"status": "ok", "message": f"从 .bak 恢复"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def repair_all() -> dict[str, Any]:
    """一键修复所有 vdb。"""
    results: dict[str, Any] = {}
    for vdb_file in _VDB_TEXT_FIELD:
        results[vdb_file] = repair_vdb(vdb_file)
    return results
