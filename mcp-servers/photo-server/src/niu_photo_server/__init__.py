"""
Niu File & Photo MCP Server

Provides tools for file and photo management.
Supports document ingestion, photo processing with face recognition.
"""

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
from loguru import logger
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# ============== Tool Schemas ==============

TOOL_SCHEMAS = {
    "ingest": {
        "name": "ingest",
        "description": """统一入库工具 — 自动判断路径类型和内容类型

参数:
- path: 必填，文件路径或目录路径
- mode: copy（复制）| move（移动）| reference（引用），默认 copy
- category: 分类，不传则自动推断
- l1: L1 摘要（文档入库第二轮调用时传入）
- file_path: 文档存储路径（L1 回传时使用）

自动判断逻辑:
1. 路径类型：is_dir() → 目录模式，is_file() → 文件模式
2. 内容类型（文件）：按扩展名判断 — 照片(.jpg/.png等) → 照片流程，其他 → 文档流程
3. 内容类型（目录）：扫描目录 — 全照片 → 批量照片，全文档 → 批量文档，混合 → 分别处理

照片流程: EXIF → 人脸检测 → 人物匹配 → L0摘要 → KG同步
文档流程: 拷贝 → 返回 need_l1 → 子Agent生成L1 → 调用 ingest(file_path=..., l1=...) 存储

返回:
- 照片: {status, photo_id, detected_persons, abstract, exif, lightrag_sync}
- 文档(首轮): {status: "need_l1", file_path, content, hint}
- 文档(L1回传): {status: "success", file_path}
- 目录: {status, total, success/need_l1, results/files}""",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径或目录路径"},
                "mode": {
                    "type": "string",
                    "enum": ["copy", "move", "reference"],
                    "default": "copy",
                    "description": "文件操作模式",
                },
                "category": {"type": "string", "description": "分类，不传则自动推断"},
                "l1": {"type": "string", "description": "L1 摘要（文档入库第二轮调用时传入）"},
                "file_path": {"type": "string", "description": "文档存储路径（L1 回传时使用）"},
            },
            "required": ["path"],
        },
    },
    "name_person": {
        "name": "name_person",
        "description": """为人物命名

参数:
- person_id: 必填，人物ID
- name: 必填，新名称

返回:
- status: success | error
- person_id: 人物ID
- name: 新名称
- auto_label: 自动标签（如"未命名人物_1"）""",
        "input_schema": {
            "type": "object",
            "properties": {
                "person_id": {"type": "string", "description": "人物ID"},
                "name": {"type": "string", "description": "新名称"},
            },
            "required": ["person_id", "name"],
        },
    },
    "merge_persons": {
        "name": "merge_persons",
        "description": """合并两个人物

参数:
- person_a_id: 必填，保留的人物ID
- person_b_id: 必填，要合并到A的人物ID

返回:
- status: success | error
- merged_into: 合并到的人物ID
- name: 保留的名称
- photo_count: 合并后的照片数量
- deleted_person_id: 被删除的人物ID

说明:
- 保留 person_a 的名称
- 合并所有人脸向量，重新计算中心
- 更新所有照片关联
- 学习机制：如果相似度低于阈值，自动调整阈值""",
        "input_schema": {
            "type": "object",
            "properties": {
                "person_a_id": {"type": "string", "description": "保留的人物ID"},
                "person_b_id": {"type": "string", "description": "要合并的人物ID"},
            },
            "required": ["person_a_id", "person_b_id"],
        },
    },
    "search_persons": {
        "name": "search_persons",
        "description": """搜索人物（按名字模糊匹配）

参数:
- query: 搜索词（人名，子串匹配）
- limit: 返回数量（默认10）

返回:
- 匹配的人物列表""",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索词（人名）"},
                "limit": {
                    "type": "integer",
                    "description": "返回数量",
                    "default": 10,
                },
            },
            "required": ["query"],
        },
    },
    "get_unnamed_persons": {
        "name": "get_unnamed_persons",
        "description": """获取所有未命名人物

返回:
- 未命名人物列表，按出现次数排序
- 包含：id, auto_label, photo_count, has_valid_photos, photos: [{file_path, boxed_path}]
- boxed_path 是带人脸红框的图片路径，前端用 ::person_photo:: 标记显示
- has_valid_photos=false 表示该人物的照片文件已不存在""",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    "delete_person": {
        "name": "delete_person",
        "description": """删除人物及其所有关联数据

警告：这会删除人物图谱中的节点，请谨慎使用。
只有在用户明确要求删除时才调用。

参数:
- person_id: 要删除的人物ID

返回:
- status: success | error
- message: 结果说明""",
        "input_schema": {
            "type": "object",
            "properties": {
                "person_id": {"type": "string", "description": "人物ID"},
            },
            "required": ["person_id"],
        },
    },
    "cleanup_deleted_photos": {
        "name": "cleanup_deleted_photos",
        "description": """清理已删除照片的数据库记录

在删除照片文件/目录后调用，清理数据库中的孤儿记录。
扫描 photos 表，删除文件不存在的记录及其关联的 faces 记录。

返回:
- deleted_photos: 删除的照片记录数
- deleted_faces: 删除的人脸记录数

使用场景：
- 用户删除了照片目录后
- 清理数据库中的残留记录""",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    "get_person_photos": {
        "name": "get_person_photos",
        "description": """获取某人物的多张照片（用于"换一张"场景）

当用户说"看不清"、"换一张"时调用此工具。

参数:
- person_id: 人物ID
- limit: 最多返回几张照片（默认5）

返回:
- person_id, person_name
- photos: [{file_path, boxed_path, taken_at}, ...]

boxed_path 是带人脸红框的图片路径，前端用 ::person_photo:: 标记显示。""",
        "input_schema": {
            "type": "object",
            "properties": {
                "person_id": {"type": "string", "description": "人物ID"},
                "limit": {
                    "type": "integer",
                    "description": "最多返回几张（默认5）",
                },
            },
            "required": ["person_id"],
        },
    },
    "unload_face_model": {
        "name": "unload_face_model",
        "description": """卸载人脸识别模型，释放内存（约 326MB）

在长时间空闲时调用此工具释放内存。
通常由系统在 SLEEP 状态时自动调用。

返回:
- status: success
- message: 卸载结果""",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
}


def get_tool_schemas() -> list[dict]:
    """返回所有工具的 schema 列表（用于 MCP Loader 注册）"""
    return list(TOOL_SCHEMAS.values())


# ============== Database ==============


def get_db_path() -> Path:
    """Get photo database path (3-level priority, consistent with get_vector_db_path).

    Priority:
    1. NIU_DB_PATH env var (explicit override, replaces vectors.db with photos.db)
    2. WORKSPACE_PATH env var (set by Go launcher main.go)
    3. ~/.niu/memory.json workspace.path
    """
    # 1. NIU_DB_PATH — replace vectors.db suffix with photos.db
    if "NIU_DB_PATH" in os.environ:
        p = Path(os.environ["NIU_DB_PATH"])
        if not p.parent.exists():
            raise ValueError(f"NIU_DB_PATH 父目录不存在: {p.parent}。请检查配置。")
        return p.parent / "photos.db"
    # 2. WORKSPACE_PATH env var
    if "WORKSPACE_PATH" in os.environ:
        ws = Path(os.environ["WORKSPACE_PATH"])
        if not ws.exists():
            raise ValueError(f"WORKSPACE_PATH 指向不存在的目录: {ws}。请检查配置。")
        return ws / "photos.db"
    # 3. ~/.niu/memory.json workspace.path
    memory_path = Path.home() / ".niu" / "memory.json"
    if memory_path.exists():
        try:
            with open(memory_path, "r", encoding="utf-8") as f:
                memory = json.load(f)
            workspace_path = memory.get("workspace", {}).get("path")
            if workspace_path and Path(workspace_path).exists():
                return Path(workspace_path) / "photos.db"
            if workspace_path:
                raise ValueError(f"workspace.path 指向不存在的目录: {workspace_path}。请检查 memory.json 配置。")
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"无法从 {memory_path} 解析 JSON: {e}。") from e
    raise ValueError(
        f"无法确定照片库路径：~/.niu/memory.json 不存在或缺少 workspace.path 配置。"
        f"请在 ~/.niu/memory.json 中设置 workspace.path，或设置 WORKSPACE_PATH 环境变量。"
    )


_conn: sqlite3.Connection | None = None
_db_path_failed: bool = False


def get_connection() -> sqlite3.Connection:
    """Get or create database connection."""
    global _conn, _db_path_failed
    if _db_path_failed:
        raise RuntimeError("照片库路径解析失败，无法建立连接。请检查 memory.json 中 workspace.path 配置。")
    if _conn is None:
        try:
            db_path = get_db_path()
        except ValueError as e:
            _db_path_failed = True
            logger.error(f"照片库路径解析失败: {e}")
            raise RuntimeError(f"照片库路径解析失败: {e}") from e
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(db_path), check_same_thread=False)
        _init_schema(_conn)
    return _conn


def _init_schema(conn: sqlite3.Connection) -> None:
    """Initialize database schema."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS persons (
            id TEXT PRIMARY KEY,
            name TEXT,
            auto_label TEXT,
            name_embedding BLOB,          -- 人物名向量（用于搜索）
            center_embedding BLOB,        -- 人脸中心向量
            threshold_adjustment REAL DEFAULT 0,
            photo_count INTEGER DEFAULT 0,
            first_seen TEXT,
            last_seen TEXT,
            created_at TEXT
        );
        
        CREATE TABLE IF NOT EXISTS photos (
            id TEXT PRIMARY KEY,
            file_path TEXT,
            taken_at TEXT,
            location TEXT,
            camera TEXT,
            abstract TEXT,
            ingested_at TEXT
        );
        
        CREATE TABLE IF NOT EXISTS faces (
            id TEXT PRIMARY KEY,
            photo_id TEXT,
            person_id TEXT,
            embedding BLOB,
            bounding_box TEXT,
            confidence REAL,
            FOREIGN KEY (photo_id) REFERENCES photos(id),
            FOREIGN KEY (person_id) REFERENCES persons(id)
        );
        
        -- 同框关系表
        CREATE TABLE IF NOT EXISTS co_occurrences (
            person_a_id TEXT,
            person_b_id TEXT,
            count INTEGER DEFAULT 1,
            first_seen TEXT,
            last_seen TEXT,
            PRIMARY KEY (person_a_id, person_b_id),
            FOREIGN KEY (person_a_id) REFERENCES persons(id),
            FOREIGN KEY (person_b_id) REFERENCES persons(id)
        );
        
        CREATE INDEX IF NOT EXISTS idx_faces_photo ON faces(photo_id);
        CREATE INDEX IF NOT EXISTS idx_faces_person ON faces(person_id);
        CREATE INDEX IF NOT EXISTS idx_photos_taken ON photos(taken_at);
        CREATE INDEX IF NOT EXISTS idx_co_occurrences ON co_occurrences(person_a_id, person_b_id);
    """)
    conn.commit()
    logger.info("Photo database schema initialized")


# ============== 共享向量服务 ==============
# 同进程架构：直接调用 niu_api.internal.embedding，不走 HTTP


def call_embedding_service(endpoint: str, data: dict) -> dict | None:
    """直接调用 niu_api.internal.embedding（同进程，无 HTTP 开销）。"""
    try:
        from niu_api.internal.embedding import encode, similarity

        if endpoint == "/encode":
            text = data.get("text", "")
            embedding = encode(text)
            return {"embedding": embedding}
        elif endpoint == "/similarity":
            text1 = data.get("text1", "")
            text2 = data.get("text2", "")
            sim = similarity(text1, text2)
            return {"similarity": sim}
    except Exception as e:
        logger.warning(f"[Embedding] 同进程调用失败: {e}")
        return None


# ============== 知识图谱同步 ==============


def sync_to_kg(file_path: str, l1: str, source: str = "document") -> dict:
    """同步文档到 LightRAG 知识图谱。

    通过 LightRAG ainsert() 入库，实体提取由 LightRAG 内部自动完成。
    失败不影响主流程（向量库写入已成功）。
    """
    try:
        from niu_api.internal.lightrag_manager import get_lightrag, call_async

        rag = get_lightrag()
        if rag is None:
            logger.warning("[KG] LightRAG not available, skipping KG sync")
            return {"status": "skipped", "reason": "LightRAG not available"}

        content = f"[Document: {file_path}]\n{l1}"
        call_async(rag.ainsert(content, file_paths=[file_path]))
        logger.info(f"[KG] Document ingested into LightRAG: {file_path}")

        return {"status": "success", "doc_uri": file_path}

    except Exception as e:
        logger.warning(f"[KG] Sync failed: {e}")
        return {"status": "error", "reason": str(e)}


def sync_photo_to_kg(file_path: str, abstract: str, detected_persons: list) -> dict:
    """同步照片和人物到 LightRAG 知识图谱。

    照片描述通过 ainsert() 入库（LightRAG 自动提取实体），
    人物通过 ainsert_custom_kg() 精确注入（避免 LLM 提取破坏人名）。
    失败不影响主流程（照片入库已成功）。
    """
    try:
        from niu_api.internal.lightrag_adapter import LightRAGIngester
        from niu_api.internal.lightrag_manager import get_lightrag, call_async

        rag = get_lightrag()
        if rag is None:
            logger.warning("[KG] LightRAG not available, skipping photo KG sync")
            return {"status": "skipped", "reason": "LightRAG not available"}

        title = Path(file_path).stem

        # 1. 照片描述通过 ainsert 入库（LightRAG 自动提取实体和关系）
        content = f"[Photo: {file_path}]\n{abstract}"
        call_async(rag.ainsert(content))
        logger.info(f"[KG] Photo ingested into LightRAG: {file_path}")

        # 2. 人物实体通过 ainsert_custom_kg 精确注入
        ingester = LightRAGIngester()
        entities_created = []
        if detected_persons:
            entities = []
            relationships = []
            for person in detected_persons:
                person_id = person.get("id", "")
                person_name = person.get("name", "")
                similarity = person.get("similarity", 0.7)
                if not person_id:
                    continue
                if not person_name:
                    person_name = person_id

                entity_name = f"person:{person_id}"
                entities.append({
                    "entity_name": entity_name,
                    "entity_type": "Person",
                    "description": f"{person_name}, detected in photo: {title}",
                    "source_id": "photo",
                    "file_path": file_path,
                })
                # Photo -> Person relationship
                relationships.append({
                    "src_id": file_path,
                    "tgt_id": entity_name,
                    "keywords": "depicts",
                    "description": f"Photo {title} depicts {person_name}",
                    "source_id": "photo",
                    "file_path": file_path,
                    "weight": round(similarity, 2),
                })
                entities_created.append(entity_name)

            if entities:
                result = ingester.inject_custom_kg(
                    entities=entities,
                    relationships=relationships,
                    chunks=[],
                    source_id="photo",
                )
                logger.info(f"[KG] Photo persons injected: {result}")

        # 3. 同框人物之间建立 co_appears_with 关系
        relations_created = 0
        if len(detected_persons) >= 2:
            co_rels = []
            for i in range(len(detected_persons)):
                for j in range(i + 1, len(detected_persons)):
                    a_id = detected_persons[i].get("id", "")
                    b_id = detected_persons[j].get("id", "")
                    if not a_id or not b_id:
                        continue
                    if a_id > b_id:
                        a_id, b_id = b_id, a_id
                    co_rels.append({
                        "src_id": f"person:{a_id}",
                        "tgt_id": f"person:{b_id}",
                        "keywords": "co_appears_with",
                        "description": f"Appeared together in photo: {title}",
                        "source_id": "photo",
                        "file_path": file_path,
                        "weight": 0.3,
                    })
                    relations_created += 1
            if co_rels:
                ingester.inject_custom_kg(
                    entities=[],
                    relationships=co_rels,
                    chunks=[],
                    source_id="photo",
                )

        logger.info(
            f"[KG] Photo sync complete: {len(entities_created)} persons, "
            f"{relations_created} co-occurrences for {file_path}"
        )
        return {
            "status": "success",
            "doc_uri": file_path,
            "entities": entities_created,
            "relations": relations_created,
        }

    except Exception as e:
        logger.warning(f"[KG] Photo sync failed: {e}")
        return {"status": "error", "reason": str(e)}


def sync_video_to_kg(file_path: str, abstract: str) -> dict:
    """同步视频到 LightRAG 知识图谱。

    视频描述通过 ainsert() 入库，实体提取由 LightRAG 自动完成。
    """
    try:
        from niu_api.internal.lightrag_manager import get_lightrag, call_async

        rag = get_lightrag()
        if rag is None:
            logger.warning("[KG] LightRAG not available, skipping video KG sync")
            return {"status": "skipped", "reason": "LightRAG not available"}

        content = f"[Video: {file_path}]\n{abstract}"
        call_async(rag.ainsert(content))
        logger.info(f"[KG] Video ingested into LightRAG: {file_path}")
        return {"status": "success", "doc_uri": file_path}

    except Exception as e:
        logger.warning(f"[KG] Video sync failed: {e}")
        return {"status": "error", "reason": str(e)}


# ============== 模型路径（人脸识别用） ==============


def _detect_available_providers() -> list[str]:
    """
    自动检测可用的 ONNX Runtime ExecutionProvider

    优先级：CUDA > DirectML > CPU

    Returns:
        可用的 provider 列表
    """
    try:
        import onnxruntime as ort

        available = ort.get_available_providers()
        logger.info(f"[GPU_DETECT] Available providers: {available}")

        # 按优先级排序
        priority = ["CUDAExecutionProvider", "DmlExecutionProvider", "CPUExecutionProvider"]

        selected = []
        for provider in priority:
            if provider in available:
                selected.append(provider)

        # 如果没有找到任何优先 provider，使用所有可用的
        if not selected:
            selected = available

        logger.info(f"[GPU_DETECT] Selected providers: {selected}")
        return selected

    except Exception as e:
        logger.warning(f"[GPU_DETECT] Failed to detect providers: {e}, using CPU")
        return ["CPUExecutionProvider"]


def get_models_dir() -> Path:
    """获取模型目录路径（人脸识别模型使用）"""
    if "NIU_MODELS_PATH" in os.environ:
        return Path(os.environ["NIU_MODELS_PATH"])
    return Path(__file__).parent.parent.parent.parent.parent / "models"


# ============== Face recognition model ==============
_face_model = None
_last_model_use_time = None
_model_in_use = False  # 防止卸载定时器在推理期间卸载模型
MODEL_IDLE_TIMEOUT_SECONDS = 300  # 5 分钟无使用自动卸载
_model_check_interval = 60  # 每 60 秒检查一次


def _start_model_unload_timer():
    """启动后台定时器，定期检查并卸载空闲模型"""
    import threading
    import time

    def check_and_unload():
        global _face_model, _last_model_use_time
        while True:
            time.sleep(_model_check_interval)

            if _face_model is not None and _last_model_use_time is not None and not _model_in_use:
                idle_seconds = (datetime.now() - _last_model_use_time).total_seconds()
                if idle_seconds > MODEL_IDLE_TIMEOUT_SECONDS:
                    logger.info(
                        f"[MODEL_UNLOAD] Model idle for {idle_seconds:.0f}s, unloading ~326MB..."
                    )
                    _face_model = None
                    _last_model_use_time = None
                    # 不调用 gc.collect()，让 Python 自然回收
                    # 避免 detect_faces() 正在使用模型时被释放
                    logger.info("[MODEL_UNLOAD] Face model unloaded, memory released")

    thread = threading.Thread(target=check_and_unload, daemon=True)
    thread.start()
    logger.info("[MODEL_UNLOAD] Background unload timer started")


def get_face_model():
    """Get or load InsightFace model. Local first, download if not found.

    自动检测 GPU，优先使用 GPU 加速，无 GPU 时降级到 CPU。
    """
    import sys

    global _face_model, _last_model_use_time

    if _face_model is None:
        try:
            print(
                "[GET_FACE_MODEL] Starting to load InsightFace...",
                file=sys.stderr,
                flush=True,
            )
            logger.info("[GET_FACE_MODEL] Starting to load InsightFace...")
            from insightface.app import FaceAnalysis

            # 本地路径
            models_dir = get_models_dir()
            print(
                f"[GET_FACE_MODEL] Models dir: {models_dir}",
                file=sys.stderr,
                flush=True,
            )
            logger.info(f"[GET_FACE_MODEL] Models dir: {models_dir}")

            # 检查本地是否存在
            local_model_path = models_dir / "models" / "buffalo_l"

            if local_model_path.exists():
                print(
                    f"[GET_FACE_MODEL] Local model exists: {local_model_path}",
                    file=sys.stderr,
                    flush=True,
                )
                logger.info(
                    f"[GET_FACE_MODEL] Loading face model from local: {local_model_path}"
                )
            else:
                # 本地没有，需要下载
                print(
                    f"[GET_FACE_MODEL] Local model not found, will download...",
                    file=sys.stderr,
                    flush=True,
                )
                logger.info(
                    f"[GET_FACE_MODEL] Local face model not found, will download..."
                )
                logger.info(f"[GET_FACE_MODEL] Expected path: {local_model_path}")

            print(
                "[GET_FACE_MODEL] Creating FaceAnalysis instance...",
                file=sys.stderr,
                flush=True,
            )
            logger.info("[GET_FACE_MODEL] Creating FaceAnalysis instance...")

            # 自动检测可用的 ExecutionProvider
            providers = _detect_available_providers()

            # 关键：临时抑制 stdout，防止 ONNX Runtime 污染 MCP stdio 通信
            import os
            import contextlib

            @contextlib.contextmanager
            def suppress_stdout():
                """临时抑制 stdout（ONNX Runtime 会污染 stdout）"""
                devnull = open(os.devnull, 'w')
                old_stdout = os.dup(1)
                try:
                    os.dup2(devnull.fileno(), 1)
                    yield
                finally:
                    os.dup2(old_stdout, 1)
                    devnull.close()
                    os.close(old_stdout)

            with suppress_stdout():
                _face_model = FaceAnalysis(
                    name="buffalo_l",
                    root=str(models_dir),
                    providers=providers,
                )
                # ctx_id: 0 = GPU, -1 = CPU
                ctx_id = 0 if "CUDAExecutionProvider" in providers else -1
                _face_model.prepare(ctx_id=ctx_id)

            print(
                f"[GET_FACE_MODEL] FaceAnalysis created with providers: {providers}",
                file=sys.stderr,
                flush=True,
            )
            logger.info(f"[GET_FACE_MODEL] FaceAnalysis created with providers: {providers}")

            print(
                f"[GET_FACE_MODEL] Face model loaded: buffalo_l (ctx_id={ctx_id})",
                file=sys.stderr,
                flush=True,
            )
            logger.info(f"[GET_FACE_MODEL] Face model loaded: buffalo_l (ctx_id={ctx_id})")
        except ImportError as e:
            print(
                f"[GET_FACE_MODEL] insightface not installed: {e}",
                file=sys.stderr,
                flush=True,
            )
            logger.warning(f"[GET_FACE_MODEL] insightface not installed: {e}")
            return None
        except Exception as e:
            print(
                f"[GET_FACE_MODEL] Failed to load face model: {e}",
                file=sys.stderr,
                flush=True,
            )
            logger.warning(f"[GET_FACE_MODEL] Failed to load face model: {e}")
            return None
    else:
        print("[GET_FACE_MODEL] Using cached face model", file=sys.stderr, flush=True)
        logger.info("[GET_FACE_MODEL] Using cached face model")

    # 更新最后使用时间
    _last_model_use_time = datetime.now()
    return _face_model


def unload_face_model():
    """卸载人脸识别模型，释放内存"""
    global _face_model, _last_model_use_time
    if _face_model is not None:
        _face_model = None
        _last_model_use_time = None
        # 不调用 gc.collect()，让 Python 自然回收
        # 避免 detect_faces() 正在使用模型时被释放
        logger.info("Face model unloaded, ~326MB memory released")
    return {"status": "success"}


def update_co_occurrences(persons: list[dict], photo_taken_at: str | None = None):
    """更新同框关系"""
    if len(persons) < 2:
        return

    try:
        conn = get_connection()
        now = photo_taken_at or datetime.now().isoformat()

        # 每对人创建/更新同框关系
        for i, person_a in enumerate(persons):
            for person_b in persons[i + 1 :]:
                # 确保 a < b（避免重复）
                a_id, b_id = sorted([person_a["id"], person_b["id"]])

                # 检查是否已有关系
                cursor = conn.execute(
                    "SELECT count FROM co_occurrences WHERE person_a_id = ? AND person_b_id = ?",
                    (a_id, b_id),
                )
                row = cursor.fetchone()

                if row:
                    # 更新
                    conn.execute(
                        """UPDATE co_occurrences 
                           SET count = count + 1, last_seen = ? 
                           WHERE person_a_id = ? AND person_b_id = ?""",
                        (now, a_id, b_id),
                    )
                else:
                    # 创建
                    conn.execute(
                        """INSERT INTO co_occurrences (person_a_id, person_b_id, count, first_seen, last_seen)
                           VALUES (?, ?, 1, ?, ?)""",
                        (a_id, b_id, now, now),
                    )

        # 不在此处 commit，由调用者控制事务原子性
        logger.info(f"[CO_OCCURRENCE] Updated {len(persons)} persons co-occurrence")
    except Exception as e:
        logger.warning(f"[CO_OCCURRENCE] Failed: {e}")


# ============== 配置路径 ==============


def get_config_path() -> Path:
    """获取配置目录路径"""
    if "NIU_CONFIG_PATH" in os.environ:
        return Path(os.environ["NIU_CONFIG_PATH"])
    return Path.home() / ".niu"


def get_memory() -> dict:
    """读取 memory.json"""
    memory_path = get_config_path() / "memory.json"
    if memory_path.exists():
        with open(memory_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"workspace": {"path": str(Path.home() / "Documents" / "niu")}}


def get_workspace_path() -> Path:
    """获取工作区路径（3 层优先级，与 resolve_vector_db_path 一致）

    Priority:
    1. WORKSPACE_PATH 环境变量（由 Go 启动器 main.go 设置）
    2. ~/.niu/memory.json 的 workspace.path
    不降级、不合成，路径无效时抛出 ValueError。
    """
    # 1. WORKSPACE_PATH 环境变量
    if "WORKSPACE_PATH" in os.environ:
        ws = Path(os.environ["WORKSPACE_PATH"])
        if not ws.exists():
            raise ValueError(f"WORKSPACE_PATH 指向不存在的目录: {ws}。请检查配置。")
        return ws
    # 2. 从 ~/.niu/memory.json 读取 workspace.path
    memory_path = Path.home() / ".niu" / "memory.json"
    if memory_path.exists():
        try:
            with open(memory_path, "r", encoding="utf-8") as f:
                memory = json.load(f)
            workspace_path = memory.get("workspace", {}).get("path")
            if workspace_path and Path(workspace_path).exists():
                return Path(workspace_path)
            if workspace_path:
                raise ValueError(f"workspace.path 指向不存在的目录: {workspace_path}。请检查 memory.json 配置。")
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"无法从 {memory_path} 解析 JSON: {e}。") from e
    raise ValueError(
        f"无法确定工作区路径：~/.niu/memory.json 不存在或缺少 workspace.path 配置。"
        f"请在 ~/.niu/memory.json 中设置 workspace.path，或设置 WORKSPACE_PATH 环境变量。"
    )


def get_preferences() -> dict:
    """读取 preferences.json"""
    prefs_path = get_config_path() / "preferences.json"
    if prefs_path.exists():
        with open(prefs_path, "r", encoding="utf-8") as f:
            return json.load(f)
    raise FileNotFoundError(f"preferences.json not found at {prefs_path}")


# ============== EXIF Extraction ==============


def extract_exif(file_path: str) -> dict:
    """Extract EXIF data from photo."""
    result: dict[str, Any] = {
        "taken_at": None,
        "location": None,
        "camera": None,
    }

    try:
        from PIL import Image
        from PIL.ExifTags import TAGS, GPSTAGS

        img = Image.open(file_path)
        exif_data = img._getexif()  # type: ignore

        if not exif_data:
            return result

        # Extract datetime
        for tag_id, value in exif_data.items():
            tag = TAGS.get(tag_id, tag_id)

            if tag == "DateTimeOriginal":
                result["taken_at"] = value
            elif tag == "DateTimeDigitized":
                if not result["taken_at"]:
                    result["taken_at"] = value
            elif tag == "Model":
                result["camera"] = value
            elif tag == "Make":
                if result["camera"]:
                    result["camera"] = f"{value} {result['camera']}"
                else:
                    result["camera"] = value

        # Extract GPS
        for tag_id, value in exif_data.items():
            tag = TAGS.get(tag_id, tag_id)
            if tag == "GPSInfo":
                gps_data: dict[str, Any] = {}
                for gps_tag, gps_val in value.items():
                    gps_tag_name = str(GPSTAGS.get(gps_tag, gps_tag))
                    gps_data[gps_tag_name] = gps_val

                lat = gps_data.get("GPSLatitude")
                lat_ref = gps_data.get("GPSLatitudeRef")
                lon = gps_data.get("GPSLongitude")
                lon_ref = gps_data.get("GPSLongitudeRef")

                if lat and lat_ref and lon and lon_ref:
                    lat_val = lat[0] + lat[1] / 60 + lat[2] / 3600
                    if lat_ref == "S":
                        lat_val = -lat_val
                    lon_val = lon[0] + lon[1] / 60 + lon[2] / 3600
                    if lon_ref == "W":
                        lon_val = -lon_val
                    result["location"] = f"{lat_val:.6f},{lon_val:.6f}"

                break

    except ImportError:
        logger.warning("PIL not installed, EXIF extraction disabled")
    except Exception as e:
        logger.warning(f"EXIF extraction failed: {e}")

    return result


# ============== Face Recognition ==============


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Calculate cosine similarity between two vectors."""
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


def get_all_persons() -> list[dict]:
    """Get all persons from database."""
    conn = get_connection()
    cursor = conn.execute(
        "SELECT id, name, auto_label, center_embedding, threshold_adjustment, photo_count FROM persons"
    )
    persons = []
    for row in cursor.fetchall():
        persons.append(
            {
                "id": row[0],
                "name": row[1],
                "auto_label": row[2],
                "center_embedding": np.frombuffer(row[3], dtype=np.float32)
                if row[3]
                else None,
                "threshold_adjustment": row[4] or 0,
                "photo_count": row[5] or 0,
            }
        )
    return persons


def get_next_auto_label() -> str:
    """Get next auto label for unnamed person."""
    conn = get_connection()
    cursor = conn.execute(
        "SELECT auto_label FROM persons WHERE auto_label LIKE '未命名人物_%' ORDER BY auto_label DESC LIMIT 1"
    )
    row = cursor.fetchone()
    if row:
        # Extract number and increment
        try:
            num = int(row[0].split("_")[-1])
            return f"未命名人物_{num + 1}"
        except (ValueError, IndexError, AttributeError):
            pass
    return "未命名人物_1"


def search_persons(query: str, limit: int = 10) -> list[dict]:
    """
    搜索人物（按名字模糊匹配）

    Args:
        query: 搜索词（人名）
        limit: 返回数量

    Returns:
        匹配的人物列表
    """
    try:
        conn = get_connection()
        # 使用 SQL LIKE 模糊匹配，转义 LIKE 通配符
        escaped = query.replace("%", "\\%").replace("_", "\\_")
        cursor = conn.execute(
            """SELECT id, name, auto_label, photo_count
               FROM persons
               WHERE name IS NOT NULL AND name LIKE ? ESCAPE '\\'""",
            (f"%{escaped}%",),
        )

        results = []
        for row in cursor.fetchall():
            person_id, name, auto_label, photo_count = row
            results.append(
                {
                    "id": person_id,
                    "name": name,
                    "auto_label": auto_label,
                    "photo_count": photo_count or 0,
                }
            )

        return results[:limit]

    except Exception as e:
        logger.exception(f"[SEARCH_PERSONS] Failed: {e}")
        return []


def get_unnamed_persons() -> list[dict]:
    """获取未命名人物列表，包含多张代表照片和人脸框

    返回每个人的多张照片（最多3张），供主 Agent 轮流展示。
    只返回存在的照片文件。如果人物没有任何有效照片，标记 has_valid_photos=false。
    """
    from pathlib import Path

    conn = get_connection()
    cursor = conn.execute(
        """SELECT id, name, auto_label, photo_count, first_seen, last_seen 
           FROM persons 
           WHERE name IS NULL 
           ORDER BY photo_count DESC"""
    )

    persons = []
    for row in cursor.fetchall():
        person_id = row[0]

        # 获取该人物的所有照片（去重，同一照片只取第一个bbox）
        photo_cursor = conn.execute(
            """SELECT p.file_path, f.bounding_box, p.id
               FROM photos p
               JOIN faces f ON f.photo_id = p.id
               WHERE f.person_id = ?
               ORDER BY p.taken_at DESC""",
            (person_id,),
        )

        seen_photo_ids = set()
        photos = []
        for photo_row in photo_cursor.fetchall():
            # 同一照片只取一次（避免多个人脸导致重复）
            photo_id = photo_row[2]
            if photo_id in seen_photo_ids:
                continue
            seen_photo_ids.add(photo_id)

            # 检查照片文件是否存在
            if not Path(photo_row[0]).exists():
                continue

            bbox = None
            if photo_row[1]:
                try:
                    bbox = json.loads(photo_row[1])
                except (json.JSONDecodeError, TypeError, ValueError):
                    logger.warning(f"[UnnamedPersons] Invalid bbox JSON for photo {photo_row[0]}")

            # 在原图上画人脸红框
            boxed_path = None
            if bbox:
                boxed_path = draw_face_boxes_on_image(photo_row[0], [bbox])

            photos.append(
                {
                    "file_path": photo_row[0],
                    "boxed_path": boxed_path,  # 带红框图路径
                }
            )

            # 最多返回3张存在的照片
            if len(photos) >= 3:
                break

        persons.append(
            {
                "id": person_id,
                "name": row[1],
                "auto_label": row[2],
                "photo_count": row[3] or 0,
                "first_seen": row[4],
                "last_seen": row[5],
                "photos": photos,  # 只包含存在的照片
                "has_valid_photos": len(photos) > 0,  # 是否有有效照片
            }
        )

    return persons


def delete_person(person_id: str) -> dict:
    """删除人物及其所有关联数据

    警告：这会删除人物图谱中的节点，请谨慎使用。
    只有在用户明确要求删除时才调用。
    """
    conn = get_connection()

    # 检查人物是否存在
    cursor = conn.execute(
        "SELECT name, auto_label FROM persons WHERE id = ?", (person_id,)
    )
    row = cursor.fetchone()
    if not row:
        return {"status": "error", "message": f"Person not found: {person_id}"}

    person_name = row[0] if row[0] else row[1]

    # 找出同照片中其他人物（删除 faces 前查询）
    co_person_ids = conn.execute(
        """SELECT DISTINCT f2.person_id FROM faces f1
           JOIN faces f2 ON f1.photo_id = f2.photo_id
           WHERE f1.person_id = ? AND f2.person_id != ? AND f2.person_id IS NOT NULL""",
        (person_id, person_id),
    ).fetchall()

    # 删除关联的人脸记录
    conn.execute("DELETE FROM faces WHERE person_id = ?", (person_id,))

    # 重新计算同照片中其他人物的 photo_count
    for (pid,) in co_person_ids:
        actual_count = conn.execute(
            "SELECT COUNT(DISTINCT photo_id) FROM faces WHERE person_id = ?",
            (pid,),
        ).fetchone()[0]
        conn.execute(
            "UPDATE persons SET photo_count = ? WHERE id = ?",
            (actual_count, pid),
        )

    # 删除同框关系
    conn.execute(
        "DELETE FROM co_occurrences WHERE person_a_id = ? OR person_b_id = ?",
        (person_id, person_id),
    )

    # 删除人物记录
    conn.execute("DELETE FROM persons WHERE id = ?", (person_id,))

    conn.commit()

    # 同步删除知识图谱中的实体
    # Note: LightRAG doesn't support direct entity deletion by name;
    # for person entities, we rely on the graph's natural cleanup.
    logger.info(f"[DELETE_PERSON] LightRAG entity cleanup for person:{person_id}")

    return {
        "status": "success",
        "message": f"Deleted person: {person_name}",
        "person_id": person_id,
    }


def cleanup_deleted_photos() -> dict:
    """清理已删除照片的数据库记录

    扫描 photos 表，删除文件不存在的照片记录及其关联的 faces 记录。
    在删除照片文件后调用此工具清理数据库。

    返回:
    - deleted_photos: 删除的照片记录数
    - deleted_faces: 删除的人脸记录数
    """
    from pathlib import Path

    conn = get_connection()

    # 找出文件不存在的照片
    cursor = conn.execute("SELECT id, file_path FROM photos")
    deleted_photo_ids = []

    for row in cursor.fetchall():
        photo_id = row[0]
        file_path = row[1]
        if not Path(file_path).exists():
            deleted_photo_ids.append(photo_id)

    if not deleted_photo_ids:
        return {
            "status": "success",
            "message": "No deleted photos to cleanup",
            "deleted_photos": 0,
            "deleted_faces": 0,
        }

    placeholders = ",".join("?" * len(deleted_photo_ids))

    # 找出受影响的 person_id（必须在删除 faces 之前查询）
    affected_person_ids = conn.execute(
        f"SELECT DISTINCT person_id FROM faces WHERE photo_id IN ({placeholders}) AND person_id IS NOT NULL",
        deleted_photo_ids,
    ).fetchall()

    # 删除关联的 faces 记录
    cursor = conn.execute(
        f"DELETE FROM faces WHERE photo_id IN ({placeholders})",
        deleted_photo_ids,
    )
    deleted_faces = cursor.rowcount

    # 删除 photos 记录
    cursor = conn.execute(
        f"DELETE FROM photos WHERE id IN ({placeholders})",
        deleted_photo_ids,
    )
    deleted_photos = cursor.rowcount

    # 重新计算受影响人物的 photo_count
    for (pid,) in affected_person_ids:
        actual_count = conn.execute(
            "SELECT COUNT(DISTINCT photo_id) FROM faces WHERE person_id = ?",
            (pid,),
        ).fetchone()[0]
        conn.execute(
            "UPDATE persons SET photo_count = ? WHERE id = ?",
            (actual_count, pid),
        )

    conn.commit()

    return {
        "status": "success",
        "message": f"Cleaned up {deleted_photos} deleted photos, {deleted_faces} faces",
        "deleted_photos": deleted_photos,
        "deleted_faces": deleted_faces,
    }


def draw_face_boxes_on_image(file_path: str, bbox_list: list[list[float]]) -> str | None:
    """在原图上画人脸红框，保存到临时目录，返回带框图路径。

    Args:
        file_path: 原图路径
        bbox_list: 人脸框列表，每个为 [x1, y1, x2, y2]

    Returns:
        带框图路径（~/.niu/tmp/ 下），失败返回 None
    """
    try:
        import cv2

        # 读取图片（支持中文路径）
        with open(file_path, "rb") as f:
            img_bytes = np.frombuffer(f.read(), dtype=np.uint8)
        img = cv2.imdecode(img_bytes, cv2.IMREAD_COLOR)
        if img is None:
            logger.warning(f"[DrawBox] Cannot read image: {file_path}")
            return None

        # 画红框
        for bbox in bbox_list:
            if len(bbox) == 4:
                x1, y1, x2, y2 = int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])
                cv2.rectangle(img, (x1, y1), (x2, y2), (0, 0, 255), 3)

        # 保存到临时目录（确定性文件名，相同输入复用同一文件）

        # 编码为 PNG bytes
        success, encoded = cv2.imencode(".png", img)
        if not success:
            logger.warning(f"[DrawBox] Failed to encode image: {file_path}")
            return None

        # 用路径+bbox+文件修改时间生成确定性文件名，避免重复生成
        bbox_key = "_".join(str(int(b)) for bbox in bbox_list for b in bbox)
        mtime = int(os.path.getmtime(file_path))
        name_hash = hashlib.md5(f"{file_path}:{bbox_key}:{mtime}".encode()).hexdigest()[:12]
        tmp_name = f"facebox_{name_hash}.png"

        # 获取临时目录（优先用 agent.tmp_dir，fallback 到 ~/.niu/tmp/）
        try:
            from agent.tmp_dir import get_tmp_dir
            tmp_dir = get_tmp_dir()
        except ImportError:
            tmp_dir = os.path.join(os.path.expanduser("~"), ".niu", "tmp")
            os.makedirs(tmp_dir, exist_ok=True)

        tmp_path = os.path.join(tmp_dir, tmp_name)
        if not os.path.exists(tmp_path):
            # 先写临时文件，再原子重命名，避免并发写或写失败导致损坏
            tmp_write_path = tmp_path + ".tmp"
            with open(tmp_write_path, "wb") as f:
                f.write(encoded.tobytes())
            os.replace(tmp_write_path, tmp_path)
        logger.info(f"[DrawBox] Saved boxed image to: {tmp_path}")
        return tmp_path

    except Exception as e:
        logger.error(f"[DrawBox] Error drawing face boxes: {e}")
        return None


def get_person_photos(person_id: str, limit: int = 5) -> dict:
    """获取某人物的所有照片（带人脸红框的图片路径）

    用于"换一张照片看看"的场景。
    在原图上画人脸红框，保存到临时目录，返回带框图路径。
    """
    conn = get_connection()

    # 验证人物存在
    cursor = conn.execute(
        "SELECT name, auto_label FROM persons WHERE id = ?", (person_id,)
    )
    row = cursor.fetchone()
    if not row:
        return {"status": "error", "message": f"Person not found: {person_id}"}

    person_name = row[0] if row[0] else row[1]

    # 获取该人物的所有照片（去重，同一照片只取第一个bbox）
    cursor = conn.execute(
        """SELECT p.file_path, f.bounding_box, p.taken_at, p.id
           FROM photos p
           JOIN faces f ON f.photo_id = p.id
           WHERE f.person_id = ?
           ORDER BY p.taken_at DESC""",
        (person_id,),
    )

    seen_photo_ids = set()
    photos = []
    for photo_row in cursor.fetchall():
        # 同一照片只取一次
        photo_id = photo_row[3]
        if photo_id in seen_photo_ids:
            continue
        seen_photo_ids.add(photo_id)

        if len(photos) >= limit:
            break

        bbox = None
        if photo_row[1]:
            try:
                bbox = json.loads(photo_row[1])
            except (json.JSONDecodeError, TypeError, ValueError):
                logger.warning(f"[PersonPhotos] Invalid bbox JSON for photo {photo_row[0]}")

        # 在原图上画人脸红框，保存到临时目录
        boxed_path = None
        if bbox and os.path.exists(photo_row[0]):
            boxed_path = draw_face_boxes_on_image(photo_row[0], [bbox])

        photos.append(
            {
                "file_path": photo_row[0],
                "boxed_path": boxed_path,  # 带红框图路径（~/.niu/tmp/），前端直接显示
                "taken_at": photo_row[2],
            }
        )

    return {
        "person_id": person_id,
        "person_name": person_name,
        "photos": photos,
    }


def match_face_to_person(
    face_embedding: np.ndarray, threshold: float = 0.7
) -> tuple[str | None, float]:
    """Match face embedding to existing person. Returns (person_id, similarity)."""
    persons = get_all_persons()

    best_match = None
    best_similarity = 0

    for person in persons:
        if person["center_embedding"] is None:
            continue

        similarity = cosine_similarity(face_embedding, person["center_embedding"])
        # Apply threshold adjustment (learning mechanism)
        adjusted_threshold = threshold - person["threshold_adjustment"]

        if similarity > adjusted_threshold and similarity > best_similarity:
            best_match = person["id"]
            best_similarity = similarity

    return best_match, best_similarity


def update_person_center(person_id: str, new_embedding: np.ndarray, increment_count: bool = True) -> None:
    """Update person center embedding with new face.

    Args:
        increment_count: If True, increment photo_count. Set to False when the same
                         person has multiple faces in the same photo (only count once).
    """
    conn = get_connection()

    # Get existing embedding and face count
    cursor = conn.execute(
        "SELECT center_embedding, photo_count FROM persons WHERE id = ?", (person_id,)
    )
    row = cursor.fetchone()

    if row and row[0]:
        existing = np.frombuffer(row[0], dtype=np.float32)
        photo_count = row[1] or 0

        # Incremental update: weighted average
        # existing center has weight photo_count, new embedding has weight 1
        updated = (existing * photo_count + new_embedding) / (photo_count + 1)
    else:
        updated = new_embedding

    # Update database
    if increment_count:
        conn.execute(
            "UPDATE persons SET center_embedding = ?, photo_count = photo_count + 1, last_seen = ? WHERE id = ?",
            (updated.tobytes(), datetime.now().isoformat(), person_id),
        )
    else:
        conn.execute(
            "UPDATE persons SET center_embedding = ?, last_seen = ? WHERE id = ?",
            (updated.tobytes(), datetime.now().isoformat(), person_id),
        )
    # 不在此处 commit，由调用者控制事务原子性


def detect_faces(file_path: str) -> list[dict]:
    """Detect faces in photo using InsightFace."""
    import sys

    print("[DETECT_FACES] Starting face detection...", file=sys.stderr, flush=True)

    logger.info(f"[DETECT_FACES] Starting face detection for: {file_path}")

    global _model_in_use
    _model_in_use = True  # 在获取模型之前设置，防止卸载定时器在获取和使用之间卸载
    try:
        logger.info("[DETECT_FACES] Getting face model...")
        print("[DETECT_FACES] Getting face model...", file=sys.stderr, flush=True)
        face_model = get_face_model()
        if face_model is None:
            logger.warning("[DETECT_FACES] Face model not available")
            return []
        logger.info("[DETECT_FACES] Importing cv2...")
        print("[DETECT_FACES] Importing cv2...", file=sys.stderr, flush=True)
        import cv2
        import numpy as np

        # 使用 numpy 读取文件，解决中文路径问题
        # OpenCV 的 cv2.imread 不支持中文路径
        logger.info(f"[DETECT_FACES] Reading image file: {file_path}")
        print(f"[DETECT_FACES] Reading image file...", file=sys.stderr, flush=True)
        with open(file_path, "rb") as f:
            img_bytes = np.frombuffer(f.read(), dtype=np.uint8)

        logger.info("[DETECT_FACES] Decoding image with cv2...")
        print("[DETECT_FACES] Decoding image with cv2...", file=sys.stderr, flush=True)
        img = cv2.imdecode(img_bytes, cv2.IMREAD_COLOR)

        if img is None:
            logger.error(f"[DETECT_FACES] Cannot read image: {file_path}")
            return []

        logger.info(f"[DETECT_FACES] Image decoded, shape: {img.shape}")

        # Detect faces
        logger.info("[DETECT_FACES] Running face_model.get()...")
        print("[DETECT_FACES] Running face_model.get()...", file=sys.stderr, flush=True)
        faces = face_model.get(img)
        logger.info(f"[DETECT_FACES] Face detection complete, found {len(faces)} faces")

        results = []
        for face in faces:
            results.append(
                {
                    "embedding": face.embedding,
                    "bbox": face.bbox.tolist() if hasattr(face, "bbox") else [],
                    "confidence": float(face.det_score)
                    if hasattr(face, "det_score")
                    else 1.0,
                }
            )

        logger.info(f"[DETECT_FACES] Detected {len(results)} faces in {file_path}")
        return results

    except Exception as e:
        logger.exception(f"[DETECT_FACES] Face detection failed: {e}")
        return []
    finally:
        _model_in_use = False


def generate_l0_abstract(person_names: list[str], taken_at: str | None) -> str:
    """Generate L0 abstract for photo."""
    parts = []

    if person_names:
        persons_str = "、".join(person_names)
        parts.append(f"{persons_str}合影")
    else:
        parts.append("单人照片")

    if taken_at:
        # Try to format the date nicely
        try:
            dt = datetime.strptime(taken_at, "%Y:%m:%d %H:%M:%S")
            parts.append(dt.strftime("%Y年%m月%d日"))
        except (ValueError, TypeError):
            parts.append(taken_at[:10] if len(taken_at) >= 10 else taken_at)

    return "，".join(parts)


# ============== Photo Tools ==============


def build_photo_storage_path(category: str, file_name: str) -> str:
    """构建照片存储路径（相对路径）"""
    prefs = get_preferences()
    structure = prefs["storage"]["structure"].get("photos", "{year}")

    now = datetime.now()
    path = structure.replace("{year}", str(now.year))
    path = path.replace("{month}", f"{now.month:02d}")
    path = path.replace("{date}", now.strftime("%Y-%m-%d"))
    path = path.replace("{category}", category)

    return path


def build_photo_file_name(source_name: str, taken_at: str | None = None) -> str:
    """构建照片文件名：日期_时间.扩展名

    不包含人物名，因为人物名会被修改，文件名应该只包含固定信息。
    - 有拍摄时间：20260402_092311.jpg
    - 无拍摄时间：20260402_143052.jpg（使用当前时间）
    """
    # 日期和时间
    if taken_at:
        try:
            # EXIF 格式: "2026:04:19 14:30:00" → 先尝试 strptime
            dt = datetime.strptime(taken_at, "%Y:%m:%d %H:%M:%S")
            date_str = dt.strftime("%Y%m%d")
            time_str = dt.strftime("%H%M%S")
        except (ValueError, TypeError):
            try:
                # ISO 格式: "2026-04-19T14:30:00" 或 "2026-04-19 14:30:00"
                dt = datetime.fromisoformat(taken_at.replace("Z", "+00:00"))
                date_str = dt.strftime("%Y%m%d")
                time_str = dt.strftime("%H%M%S")
            except (ValueError, TypeError, AttributeError):
                date_str = datetime.now().strftime("%Y%m%d")
                time_str = datetime.now().strftime("%H%M%S")
    else:
        date_str = datetime.now().strftime("%Y%m%d")
        time_str = datetime.now().strftime("%H%M%S")

    # 原始扩展名
    suffix = Path(source_name).suffix

    return f"{date_str}_{time_str}{suffix}"


def handle_photo_conflict(target_path: Path) -> str:
    """
    处理照片文件重名（防重名，不做相似度检测）
    返回最终存储路径
    """
    if not target_path.exists():
        return str(target_path)

    # 重名时自动改名
    stem = target_path.stem
    suffix = target_path.suffix
    parent = target_path.parent

    index = 1
    new_path = parent / f"{stem}_{index}{suffix}"
    while new_path.exists():
        index += 1
        new_path = parent / f"{stem}_{index}{suffix}"

    logger.info(f"[PHOTO_CONFLICT] 重名，改为: {new_path}")
    return str(new_path)


def ingest_photo(file_path: str, category: str | None = None) -> dict:
    """Ingest photo with face detection and person matching."""
    conn = None
    final_path = None  # Track for cleanup on failure
    try:
        logger.info(f"[INGEST_PHOTO] Processing: {file_path}")

        source = Path(file_path)
        if not source.exists():
            return {
                "status": "error",
                "error_code": "FILE_NOT_FOUND",
                "message": f"File not found: {file_path}",
            }

        # Get default category from preferences
        if category is None:
            try:
                prefs = get_preferences()
                category = prefs["categories"]["photos"][0]
            except (KeyError, TypeError, IndexError):
                category = "生活"

        # 确保 category 不为 None
        if category is None:
            category = "生活"

        # 1. Extract EXIF
        logger.info("[INGEST_PHOTO] Extracting EXIF...")
        exif = extract_exif(file_path)
        logger.info(f"[INGEST_PHOTO] EXIF: {exif}")

        # 2. Detect faces
        logger.info("[INGEST_PHOTO] Detecting faces...")
        faces = detect_faces(file_path)
        logger.info(f"[INGEST_PHOTO] Found {len(faces)} faces")

        # 3. Match/create persons
        detected_persons = []
        conn = get_connection()
        now = datetime.now().isoformat()

        seen_person_ids = set()  # 防止同一人物多张人脸时 photo_count 重复递增
        for face_data in faces:
            face_embedding = face_data["embedding"]

            # Match to existing person
            matched_id, similarity = match_face_to_person(face_embedding)

            if matched_id:
                person_id = matched_id
                logger.info(
                    f"[INGEST_PHOTO] Matched face to person {person_id} (similarity: {similarity:.3f})"
                )
            else:
                # Create new person
                person_id = str(uuid.uuid4())
                auto_label = get_next_auto_label()
                similarity = 1.0  # 新人相似度设为 1.0（完全匹配）

                conn.execute(
                    """INSERT INTO persons (id, auto_label, center_embedding, photo_count, first_seen, last_seen, created_at)
                       VALUES (?, ?, ?, 0, ?, ?, ?)""",
                    (person_id, auto_label, face_embedding.tobytes(), now, now, now),
                )
                logger.info(f"[INGEST_PHOTO] Created new person: {auto_label}")

            # Update center embedding（仅首次出现时递增 photo_count）
            is_new_photo = person_id not in seen_person_ids
            update_person_center(person_id, face_embedding, increment_count=is_new_photo)
            seen_person_ids.add(person_id)

            # Get person info for response
            cursor = conn.execute(
                "SELECT name, auto_label FROM persons WHERE id = ?", (person_id,)
            )
            row = cursor.fetchone()
            if row:
                person_name = row[0] if row[0] else row[1]
                detected_persons.append(
                    {
                        "id": person_id,
                        "name": person_name,
                        "similarity": similarity,
                        "bbox": face_data.get(
                            "bbox", []
                        ),  # 人脸框坐标 [x1, y1, x2, y2]
                        "confidence": face_data.get("confidence", 0.0),
                    }
                )

        # 不在此处 commit，等文件复制和照片/人脸记录写入后一起提交

        # 4. Copy photo to storage
        workspace = get_workspace_path()

        # 构建存储路径
        relative_dir = build_photo_storage_path(category, source.name)
        target_dir = workspace / relative_dir
        target_dir.mkdir(parents=True, exist_ok=True)

        # 构建文件名（日期_时间）
        new_file_name = build_photo_file_name(source.name, exif.get("taken_at"))

        # 检查重名
        target_path = target_dir / new_file_name
        final_path = handle_photo_conflict(target_path)

        # 复制文件
        shutil.copy2(str(source), final_path)
        logger.info(f"[INGEST_PHOTO] Copied to: {final_path}")

        # 5. Create photo record
        photo_id = str(uuid.uuid4())

        # Generate L0 abstract (person_names 用于摘要，不用于文件名)
        person_names = [p["name"] for p in detected_persons]
        abstract = generate_l0_abstract(person_names, exif.get("taken_at"))

        conn.execute(
            """INSERT INTO photos (id, file_path, taken_at, location, camera, abstract, ingested_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                photo_id,
                str(Path(final_path).resolve()),
                exif.get("taken_at"),
                exif.get("location"),
                exif.get("camera"),
                abstract,
                now,
            ),
        )

        # 6. Create face records
        if len(faces) != len(detected_persons):
            logger.error(f"[INGEST_PHOTO] Face/person count mismatch: {len(faces)} vs {len(detected_persons)}")
        for i, (face_data, person) in enumerate(zip(faces, detected_persons)):
            face_id = str(uuid.uuid4())
            bbox_str = json.dumps(face_data["bbox"])
            conn.execute(
                """INSERT INTO faces (id, photo_id, person_id, embedding, bounding_box, confidence)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    face_id,
                    photo_id,
                    person["id"],
                    face_data["embedding"].tobytes(),
                    bbox_str,
                    face_data["confidence"],
                ),
            )

        # 7. Update co-occurrence relations（去重，防止同一人物多张人脸导致自共现）
        unique_persons = []
        seen_pids = set()
        for p in detected_persons:
            if p["id"] not in seen_pids:
                seen_pids.add(p["id"])
                unique_persons.append(p)
        update_co_occurrences(unique_persons, exif.get("taken_at"))

        conn.commit()

        # 8. 同步到知识图谱（失败不影响照片入库）
        final_path_resolved = str(Path(final_path).resolve())
        kg_result = sync_photo_to_kg(final_path_resolved, abstract, detected_persons)

        # 9. Unload face model to release memory (optional, for single photo)
        # For batch processing, keep model loaded
        # unload_face_model()

        logger.info(
            f"[INGEST_PHOTO] Completed: photo_id={photo_id}, persons={len(detected_persons)}"
        )

        return {
            "status": "success",
            "photo_id": photo_id,
            "file_path": str(Path(final_path).resolve()),
            "original_path": str(source),
            "category": category,
            "detected_persons": detected_persons,
            "abstract": abstract,
            "exif": exif,
            "lightrag_sync": kg_result,
        }

    except Exception as e:
        logger.exception(f"[INGEST_PHOTO] Failed: {e}")
        # Rollback any uncommitted changes on the shared connection
        try:
            conn.rollback()
        except Exception:
            pass
        # Clean up orphaned file if copy succeeded but DB failed
        if final_path is not None:
            try:
                if os.path.exists(final_path):
                    os.remove(final_path)
            except Exception:
                pass
        return {
            "status": "error",
            "error_code": "UNKNOWN_ERROR",
            "message": str(e),
        }


def name_person(person_id: str, name: str) -> dict:
    """Name an existing person."""
    try:
        conn = get_connection()

        # Check if person exists
        cursor = conn.execute(
            "SELECT id, auto_label FROM persons WHERE id = ?", (person_id,)
        )
        row = cursor.fetchone()

        if not row:
            return {
                "status": "error",
                "error_code": "PERSON_NOT_FOUND",
                "message": f"Person not found: {person_id}",
            }

        # Update name
        conn.execute("UPDATE persons SET name = ? WHERE id = ?", (name, person_id))
        conn.commit()

        # 同步更新 LightRAG 知识图谱中的实体名称
        try:
            from niu_api.internal.lightrag_adapter import LightRAGIngester
            ingester = LightRAGIngester()
            ingester.inject_entity(
                name=f"person:{person_id}",
                entity_type="person",
                description=f"Renamed to: {name}",
            )
        except Exception as e:
            logger.warning(f"[NAME_PERSON] LightRAG sync failed: {e}")

        logger.info(f"[NAME_PERSON] Updated person {person_id} name to: {name}")

        return {
            "status": "success",
            "person_id": person_id,
            "name": name,
            "auto_label": row[1],
        }

    except Exception as e:
        logger.exception(f"[NAME_PERSON] Failed: {e}")
        return {
            "status": "error",
            "error_code": "UNKNOWN_ERROR",
            "message": str(e),
        }


def merge_persons(person_a_id: str, person_b_id: str) -> dict:
    """Merge two persons into one (keeping person_a's name)."""
    conn = None
    try:
        conn = get_connection()

        # Get both persons (包含 threshold_adjustment)
        cursor = conn.execute(
            "SELECT id, name, auto_label, center_embedding, threshold_adjustment, photo_count FROM persons WHERE id IN (?, ?)",
            (person_a_id, person_b_id),
        )
        rows = cursor.fetchall()

        if len(rows) != 2:
            return {
                "status": "error",
                "error_code": "PERSON_NOT_FOUND",
                "message": "One or both persons not found",
            }

        # Find person_a and person_b
        persons = {row[0]: row for row in rows}
        person_a = persons.get(person_a_id)
        person_b = persons.get(person_b_id)

        if not person_a or not person_b:
            return {
                "status": "error",
                "error_code": "PERSON_NOT_FOUND",
                "message": "One or both persons not found",
            }

        # Keep person_a's name
        name_a = person_a[1]
        auto_label_a = person_a[2]

        # Merge embeddings
        embedding_a = (
            np.frombuffer(person_a[3], dtype=np.float32) if person_a[3] else None
        )
        embedding_b = (
            np.frombuffer(person_b[3], dtype=np.float32) if person_b[3] else None
        )

        if embedding_a is not None and embedding_b is not None:
            # Weighted average based on photo count
            count_a = person_a[5] or 1
            count_b = person_b[5] or 1
            total = count_a + count_b
            merged_embedding = (embedding_a * count_a + embedding_b * count_b) / total
        elif embedding_a is not None:
            merged_embedding = embedding_a
        elif embedding_b is not None:
            merged_embedding = embedding_b
        else:
            merged_embedding = None

        # Calculate threshold adjustment based on original similarity
        threshold_adjustment = 0.0
        if embedding_a is not None and embedding_b is not None:
            similarity = cosine_similarity(embedding_a, embedding_b)
            # If similarity was below threshold but user confirms same person,
            # adjust threshold to make future matches easier
            if similarity < 0.7:
                threshold_adjustment = (0.7 - similarity) + 0.05
                threshold_adjustment = min(threshold_adjustment, 0.3)  # Cap at 0.3

        # Update all faces from person_b to person_a
        conn.execute(
            "UPDATE faces SET person_id = ? WHERE person_id = ?",
            (person_a_id, person_b_id),
        )

        # Calculate merged_count from actual face records (handles overlapping photos)
        merged_count = conn.execute(
            "SELECT COUNT(DISTINCT photo_id) FROM faces WHERE person_id = ?",
            (person_a_id,),
        ).fetchone()[0]

        # Update person_a with merged data（始终执行，即使 embedding 为 None）
        conn.execute(
            """UPDATE persons SET center_embedding = ?, photo_count = ?,
               threshold_adjustment = ?, last_seen = ? WHERE id = ?""",
            (
                merged_embedding.tobytes() if merged_embedding is not None else None,
                merged_count,
                max(threshold_adjustment, person_a[4] if person_a[4] else 0),
                datetime.now().isoformat(),
                person_a_id,
            ),
        )

        # Clean up co_occurrences referencing person_b
        # 1. Delete the pair (person_a, person_b) — self-co-occurrence after merge
        pair = tuple(sorted([person_a_id, person_b_id]))
        conn.execute(
            "DELETE FROM co_occurrences WHERE person_a_id = ? AND person_b_id = ?",
            pair,
        )
        # 2. Transfer person_b's co-occurrences to person_a (merge counts)
        #    For each row where person_b appears with a third person X,
        #    add the count to person_a's existing row with X (or create new row)
        b_rows = conn.execute(
            "SELECT person_a_id, person_b_id, count, first_seen, last_seen FROM co_occurrences WHERE person_a_id = ? OR person_b_id = ?",
            (person_b_id, person_b_id),
        ).fetchall()
        for row_a, row_b, count, first_seen, last_seen in b_rows:
            # Determine the third person X
            other_id = row_b if row_a == person_b_id else row_a
            if other_id == person_a_id:
                continue  # Skip self-pair (already deleted above)
            co_pair = tuple(sorted([person_a_id, other_id]))
            # Try to add count to existing row
            existing = conn.execute(
                "SELECT count, first_seen, last_seen FROM co_occurrences WHERE person_a_id = ? AND person_b_id = ?",
                co_pair,
            ).fetchone()
            if existing:
                existing_first = existing[1]
                existing_last = existing[2]
                merged_first = min(existing_first, first_seen) if existing_first and first_seen else (existing_first or first_seen)
                merged_last = max(existing_last, last_seen) if existing_last and last_seen else (existing_last or last_seen)
                conn.execute(
                    "UPDATE co_occurrences SET count = count + ?, first_seen = ?, last_seen = ? WHERE person_a_id = ? AND person_b_id = ?",
                    (count, merged_first, merged_last, co_pair[0], co_pair[1]),
                )
            else:
                conn.execute(
                    "INSERT INTO co_occurrences (person_a_id, person_b_id, count, first_seen, last_seen) VALUES (?, ?, ?, ?, ?)",
                    (co_pair[0], co_pair[1], count, first_seen, last_seen),
                )
        # 3. Delete all remaining rows referencing person_b
        conn.execute(
            "DELETE FROM co_occurrences WHERE person_a_id = ? OR person_b_id = ?",
            (person_b_id, person_b_id),
        )

        # Delete person_b
        conn.execute("DELETE FROM persons WHERE id = ?", (person_b_id,))

        conn.commit()

        # 同步 LightRAG：更新 person_a 实体，person_b 合并后不再单独存在
        try:
            from niu_api.internal.lightrag_adapter import LightRAGIngester
            ingester = LightRAGIngester()
            merged_name = name_a if name_a else auto_label_a
            # 更新 person_a 实体
            ingester.inject_entity(
                name=f"person:{person_a_id}",
                entity_type="person",
                description=f"Merged with {person_b_id}, name: {merged_name}",
            )
            # person_b 合并到 person_a，建立关系
            ingester.inject_relation(
                src_id=f"person:{person_b_id}",
                tgt_id=f"person:{person_a_id}",
                relation="merged_into",
                description=f"Person {person_b_id} merged into {person_a_id}",
            )
        except Exception as e:
            logger.warning(f"[MERGE_PERSONS] LightRAG sync failed: {e}")

        logger.info(f"[MERGE_PERSONS] Merged {person_b_id} into {person_a_id}")

        return {
            "status": "success",
            "merged_into": person_a_id,
            "name": name_a if name_a else auto_label_a,
            "photo_count": merged_count,
            "deleted_person_id": person_b_id,
        }

    except Exception as e:
        logger.exception(f"[MERGE_PERSONS] Failed: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return {
            "status": "error",
            "error_code": "UNKNOWN_ERROR",
            "message": str(e),
        }


# ============== 照片批量处理 ==============

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".heif"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv", ".m4v"}


def is_photo(file_path: str) -> bool:
    """判断是否为照片文件"""
    return Path(file_path).suffix.lower() in PHOTO_EXTENSIONS


def is_video(file_path: str) -> bool:
    """判断是否为视频文件"""
    return Path(file_path).suffix.lower() in VIDEO_EXTENSIONS


def ingest_photos_batch(source_path: str, category: str | None = None) -> dict:
    """
    批量入库照片目录（保持原目录结构）

    Args:
        source_path: 源目录路径
        category: 分类名称（作为目标根目录名）

    Returns:
        处理结果
    """
    try:
        source_dir = Path(source_path)
        if not source_dir.exists():
            return {
                "status": "error",
                "error_code": "DIR_NOT_FOUND",
                "message": f"目录不存在: {source_path}",
            }

        if not source_dir.is_dir():
            return {
                "status": "error",
                "error_code": "NOT_A_DIRECTORY",
                "message": f"不是目录: {source_path}",
            }

        if category is None:
            try:
                prefs = get_preferences()
                category = prefs["categories"]["photos"][0]
            except (KeyError, TypeError, IndexError):
                category = "生活"

        # 确保 category 不为 None
        if category is None:
            category = "生活"

        workspace = get_workspace_path()

        # 构建目标路径：{year}/{category}/{原目录名}
        now = datetime.now()
        target_root = workspace / str(now.year) / category / source_dir.name

        # 收集所有照片文件（去重，Windows 大小写不敏感会重复）
        photo_files = []
        seen_paths = set()
        for ext in PHOTO_EXTENSIONS:
            for pf in source_dir.rglob(f"*{ext}"):
                key = str(pf.resolve()).lower()
                if key not in seen_paths:
                    seen_paths.add(key)
                    photo_files.append(pf)
            for pf in source_dir.rglob(f"*{ext.upper()}"):
                key = str(pf.resolve()).lower()
                if key not in seen_paths:
                    seen_paths.add(key)
                    photo_files.append(pf)

        if not photo_files:
            return {
                "status": "error",
                "error_code": "NO_PHOTOS_FOUND",
                "message": f"目录中没有找到照片文件: {source_path}",
            }

        logger.info(f"[BATCH_PHOTOS] Found {len(photo_files)} photos in {source_path}")

        # 复制目录结构
        success_count = 0
        failed_count = 0
        results = []

        for photo_file in photo_files:
            try:
                # 计算相对路径
                relative_path = photo_file.relative_to(source_dir)
                target_path = target_root / relative_path

                # 创建目标目录
                target_path.parent.mkdir(parents=True, exist_ok=True)

                # 检查重名
                if target_path.exists():
                    stem = target_path.stem
                    suffix = target_path.suffix
                    parent = target_path.parent
                    index = 1
                    while target_path.exists():
                        target_path = parent / f"{stem}_{index}{suffix}"
                        index += 1

                # 复制文件
                shutil.copy2(str(photo_file), str(target_path))
                success_count += 1
                results.append(
                    {
                        "file": str(relative_path),
                        "status": "success",
                        "target": str(target_path),
                    }
                )

            except Exception as e:
                failed_count += 1
                results.append(
                    {
                        "file": str(photo_file.relative_to(source_dir)),
                        "status": "error",
                        "message": str(e),
                    }
                )

        logger.info(f"[BATCH_PHOTOS] Completed: {success_count}/{len(photo_files)}")

        return {
            "status": "success",
            "source_path": source_path,
            "target_path": str(target_root),
            "total": len(photo_files),
            "success": success_count,
            "failed": failed_count,
            "category": category,
            "note": f"已复制 {success_count} 张照片到 {target_root}，保持原目录结构",
        }

    except Exception as e:
        logger.exception(f"[BATCH_PHOTOS] Failed: {e}")
        return {
            "status": "error",
            "error_code": "UNKNOWN_ERROR",
            "message": str(e),
        }


def ingest_photos(source_path: str, category: str | None = None) -> dict:
    """
    智能照片入库（自动判断单张/多张/目录）

    Args:
        source_path: 文件路径或目录路径
        category: 分类名称

    Returns:
        处理结果
    """
    path = Path(source_path)

    if not path.exists():
        return {
            "status": "error",
            "error_code": "PATH_NOT_FOUND",
            "message": f"路径不存在: {source_path}",
        }

    # 目录 → 批量处理
    if path.is_dir():
        logger.info(f"[INGEST_PHOTOS] 批量模式: {source_path}")
        return ingest_photos_batch(source_path, category)

    # 文件 → 单张处理
    if path.is_file():
        logger.info(f"[INGEST_PHOTOS] 单张模式: {source_path}")
        return ingest_photo(source_path, category)

    return {
        "status": "error",
        "error_code": "INVALID_PATH",
        "message": f"无效路径: {source_path}",
    }


DOCUMENT_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".doc",
    ".txt",
    ".md",
    ".xlsx",
    ".xls",
    ".pptx",
    ".ppt",
}


def is_document(file_path: str) -> bool:
    """判断是否为文档类文件"""
    return Path(file_path).suffix.lower() in DOCUMENT_EXTENSIONS


def calculate_file_hash(file_path: str) -> str:
    """计算文件哈希"""
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def read_file_content(path: str) -> str:
    """读取文件内容"""
    suffix = Path(path).suffix.lower()

    if suffix in {".txt", ".md"}:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()

    elif suffix == ".pdf":
        try:
            from pypdf import PdfReader

            with open(path, "rb") as f:
                reader = PdfReader(f)
                return " ".join([page.extract_text() or "" for page in reader.pages])
        except Exception as e:
            logger.warning(f"[SIMILARITY] PDF读取失败: {e}")
            return ""

    else:
        # 其他格式，返回文件名作为标识
        return Path(path).stem


def calculate_content_similarity(file1: str, file2: str) -> float:
    """
    计算文档内容相似度（语义向量 + 余弦相似度）
    使用共享向量服务
    """
    try:
        logger.info(f"[SIMILARITY] 开始计算: {file1} vs {file2}")

        # 读取文件内容
        text1 = read_file_content(file1)
        text2 = read_file_content(file2)
        logger.info(f"[SIMILARITY] 文本长度: {len(text1)} vs {len(text2)}")

        if not text1 or not text2:
            logger.info("[SIMILARITY] 空文本，返回 0.0")
            return 0.0

        # 截断过长的文本（避免内存问题）
        max_len = 8000
        if len(text1) > max_len:
            text1 = text1[:max_len]
        if len(text2) > max_len:
            text2 = text2[:max_len]

        # 调用共享向量服务计算相似度
        logger.info("[SIMILARITY] 调用共享向量服务...")
        result = call_embedding_service("/similarity", {"text1": text1, "text2": text2})

        if result and "similarity" in result:
            similarity = result["similarity"]
            logger.info(f"[SIMILARITY] 结果: {similarity:.4f}")
            return similarity
        else:
            logger.warning("[SIMILARITY] 向量服务不可用，返回 0.0")
            return 0.0

    except Exception as e:
        logger.exception(f"[SIMILARITY] 失败: {e}")
        return 0.0


def build_storage_path(
    category: str, file_name: str, file_type: str = "documents"
) -> str:
    """构建存储路径（相对路径）"""
    prefs = get_preferences()
    structure = prefs["storage"]["structure"].get(file_type, "{year}/{category}")

    now = datetime.now()
    path = structure.replace("{year}", str(now.year))
    path = path.replace("{month}", f"{now.month:02d}")
    path = path.replace("{date}", now.strftime("%Y-%m-%d"))
    path = path.replace("{category}", category)

    return path


def handle_conflict(target_path: Path, source_path: Path) -> tuple[str, str]:
    """
    处理文件冲突
    返回: (最终路径, 动作说明)
    """
    logger.info(f"[CONFLICT] 检查: {target_path}")
    if not target_path.exists():
        logger.info("[CONFLICT] 文件不存在，创建新文件")
        return str(target_path), "created"

    logger.info("[CONFLICT] 文件已存在，读取冲突配置...")
    prefs = get_preferences()
    conflict_config = prefs["storage"]["conflict"]

    if is_document(str(target_path)):
        logger.info("[CONFLICT] 文档类型，计算哈希...")
        hash_existing = calculate_file_hash(str(target_path))
        hash_new = calculate_file_hash(str(source_path))
        logger.info(
            f"[CONFLICT] 哈希: existing={hash_existing[:8]}... new={hash_new[:8]}..."
        )

        if hash_existing == hash_new:
            logger.info("[CONFLICT] 哈希相同，跳过")
            return str(target_path), "skipped"

        # 哈希不同，计算语义相似度
        logger.info("[CONFLICT] 哈希不同，计算语义相似度...")
        threshold = conflict_config["document"]["similarity_threshold"]
        similarity = calculate_content_similarity(str(target_path), str(source_path))
        logger.info(f"[CONFLICT] 相似度: {similarity:.4f}, 阈值: {threshold}")

        if similarity > threshold:
            logger.info("[CONFLICT] 相似，版本管理")
            return create_version(target_path), "versioned"
        else:
            logger.info("[CONFLICT] 不相似，改名")
            return rename_file(target_path), "renamed"
    else:
        logger.info("[CONFLICT] 非文档类型，改名")
        return rename_file(target_path), "renamed"


def create_version(file_path: Path) -> str:
    """创建版本：原文件改名，返回原文件名路径"""
    timestamp = datetime.now().strftime("%Y%m%d")
    stem = file_path.stem
    suffix = file_path.suffix

    version_name = f"{stem}_v1_{timestamp}{suffix}"
    version_path = file_path.parent / version_name

    index = 1
    while version_path.exists():
        index += 1
        version_name = f"{stem}_v{index}_{timestamp}{suffix}"
        version_path = file_path.parent / version_name

    shutil.move(str(file_path), str(version_path))
    logger.info(f"版本管理: {file_path} -> {version_path}")

    return str(file_path)


def rename_file(file_path: Path) -> str:
    """改名：返回新的文件名"""
    stem = file_path.stem
    suffix = file_path.suffix
    parent = file_path.parent

    index = 1
    new_path = parent / f"{stem}_{index}{suffix}"
    while new_path.exists():
        index += 1
        new_path = parent / f"{stem}_{index}{suffix}"

    logger.info(f"文件改名预留: {file_path} -> {new_path}")
    return str(new_path)


# ============== 工具实现 ==============


def ingest_document(file_path: str, category: str = "其他", mode: str = "copy") -> dict:
    """文档入库工具"""
    try:
        logger.info(f"[INGEST] 开始处理: {file_path}")
        source = Path(file_path)
        if not source.exists():
            logger.error(f"[INGEST] 文件不存在: {file_path}")
            return {
                "status": "error",
                "error_code": "FILE_NOT_FOUND",
                "message": f"文件不存在: {file_path}",
                "suggestion": "请检查文件路径是否正确",
            }

        # 检查是否为目录
        if source.is_dir():
            logger.info(f"[INGEST] 检测到目录，检查是否包含照片...")
            # 检查目录中是否有照片（去重）
            photo_files = []
            seen_paths = set()
            for ext in PHOTO_EXTENSIONS:
                for pf in source.rglob(f"*{ext}"):
                    key = str(pf.resolve()).lower()
                    if key not in seen_paths:
                        seen_paths.add(key)
                        photo_files.append(pf)
                for pf in source.rglob(f"*{ext.upper()}"):
                    key = str(pf.resolve()).lower()
                    if key not in seen_paths:
                        seen_paths.add(key)
                        photo_files.append(pf)

            if photo_files:
                logger.info(
                    f"[INGEST] 目录包含 {len(photo_files)} 张照片，转到照片批量处理"
                )
                return ingest_photos_batch(file_path, category)
            else:
                return {
                    "status": "error",
                    "error_code": "DIRECTORY_NO_PHOTOS",
                    "message": f"目录中没有找到照片文件: {file_path}",
                    "suggestion": "请确认目录中包含照片（.jpg, .png 等）",
                }

        logger.info("[INGEST] 读取配置...")
        workspace = get_workspace_path()

        logger.info("[INGEST] 构建存储路径...")
        relative_dir = build_storage_path(category, source.name, "documents")
        target_dir = workspace / relative_dir
        target_dir.mkdir(parents=True, exist_ok=True)

        target_path = target_dir / source.name
        logger.info(f"[INGEST] 目标路径: {target_path}")

        logger.info("[INGEST] 检查冲突...")
        final_path, action = handle_conflict(target_path, source)
        logger.info(f"[INGEST] 冲突处理结果: {action}")

        if action == "skipped":
            logger.info("[INGEST] 文件已存在，跳过")
            return {
                "status": "success",
                "action": "skipped",
                "file_path": str(Path(final_path).resolve()),
                "original_path": str(source),
                "category": category,
                "note": "文件已存在，跳过重复入库",
            }

        logger.info(f"[INGEST] 执行文件操作: {mode}")
        if mode == "copy":
            shutil.copy2(str(source), final_path)
        elif mode == "move":
            shutil.move(str(source), final_path)
        elif mode == "reference":
            final_path = str(source)
            action = "referenced"

        logger.info(f"[INGEST] 完成: {final_path}")

        # 读取文件内容，准备生成 L1
        file_content = None
        try:
            file_content = read_file_content(str(final_path))
            if file_content and len(file_content) > 10000:
                file_content = file_content[:10000] + "\n... [内容已截断]"
        except Exception as e:
            logger.warning(f"[INGEST] 无法读取文件内容: {e}")
            file_content = None

        # 返回 need_l1 状态，驱动工具循环
        return {
            "status": "need_l1",
            "action": action,
            "file_path": str(Path(final_path).resolve()),
            "original_path": str(source),
            "category": category,
            "content": file_content,
            "hint": "请生成 L1 摘要（极简格式：标题|关键词|摘要|实体|类型|指针），然后调用 store_document_l1 存储",
        }

    except PermissionError as e:
        logger.error(f"[INGEST] 权限错误: {e}")
        return {
            "status": "error",
            "error_code": "PERMISSION_DENIED",
            "message": f"无权限: {e}",
            "suggestion": "请检查文件或目录权限",
        }
    except Exception as e:
        logger.exception(f"[INGEST] 失败: {e}")
        return {
            "status": "error",
            "error_code": "UNKNOWN_ERROR",
            "message": str(e),
            "suggestion": "请检查日志获取详细信息",
        }


def get_vector_db_path() -> Path:
    """获取向量数据库路径（与 resolve_vector_db_path() 一致的 3 层优先级）"""
    import os
    # 1. NIU_DB_PATH 环境变量（显式覆盖）
    if "NIU_DB_PATH" in os.environ:
        p = Path(os.environ["NIU_DB_PATH"])
        if not p.parent.exists():
            raise ValueError(f"NIU_DB_PATH 父目录不存在: {p.parent}。请检查配置。")
        return p
    # 2. WORKSPACE_PATH 环境变量（由 Go 启动器设置）
    if "WORKSPACE_PATH" in os.environ:
        ws = Path(os.environ["WORKSPACE_PATH"])
        if not ws.exists():
            raise ValueError(f"WORKSPACE_PATH 指向不存在的目录: {ws}。请检查配置。")
        return ws / "vectors.db"
    # 3. 从 ~/.niu/memory.json 读取 workspace.path（直接读取，不使用 get_memory() 的回退逻辑）
    memory_path = Path.home() / ".niu" / "memory.json"
    if memory_path.exists():
        try:
            with open(memory_path, "r", encoding="utf-8") as f:
                memory = json.load(f)
            workspace_path = memory.get("workspace", {}).get("path")
            if workspace_path and Path(workspace_path).exists():
                return Path(workspace_path) / "vectors.db"
            if workspace_path:
                raise ValueError(f"workspace.path 指向不存在的目录: {workspace_path}。请检查 memory.json 配置。")
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"无法从 {memory_path} 解析 JSON: {e}。") from e
    raise ValueError(
        f"无法确定向量库路径：~/.niu/memory.json 不存在或缺少 workspace.path 配置。"
        f"请在 ~/.niu/memory.json 中设置 workspace.path，或设置 WORKSPACE_PATH 环境变量。"
    )


_vector_db_failed: bool = False


def get_vector_db_connection() -> sqlite3.Connection:
    """获取向量数据库连接（已废弃：请使用 get_vector_search()._get_connection() 复用共享连接）"""
    global _vector_db_failed
    if _vector_db_failed:
        raise RuntimeError("向量库路径解析失败，无法建立连接。请检查 memory.json 中 workspace.path 配置。")
    try:
        db_path = get_vector_db_path()
    except ValueError as e:
        _vector_db_failed = True
        logger.error(f"向量库路径解析失败: {e}")
        raise RuntimeError(f"向量库路径解析失败: {e}") from e
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    # 确保表存在
    conn.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            embedding BLOB,
            metadata TEXT
        )
    """)
    conn.commit()
    return conn


def store_document_l1(file_path: str, l1: str, l2: str | None = None) -> dict:
    """存储文档的 L1 摘要到向量库

    参数:
    - file_path: 文档存储路径
    - l1: 极简格式摘要（标题|关键词|摘要|实体|类型|指针）
    - l2: 完整内容（可选）
    """
    try:
        logger.info(f"[STORE_L1] 存储 L1: {file_path}")

        # 生成唯一 ID
        import uuid

        l1_id = f"doc_{uuid.uuid4().hex[:12]}"

        # 调用 embedding-service 生成向量
        embedding_blob = None
        embedding_result = call_embedding_service("/encode", {"text": l1})
        if embedding_result and "embedding" in embedding_result:
            embedding_blob = np.array(
                embedding_result["embedding"], dtype=np.float32
            ).tobytes()
            logger.info(
                f"[STORE_L1] 向量生成成功，维度: {len(embedding_result['embedding'])}"
            )

        # Embedding 失败则直接返回错误（无向量的记录无法被检索）
        if embedding_blob is None:
            return {
                "status": "error",
                "reason": "Embedding 服务不可用，无法生成向量。请确保 embedding 服务已启动。",
                "file_path": file_path,
            }

        # 使用 VectorSearchAdapter 的共享连接（避免独立连接与共享连接并发冲突）
        from agent.vector_search import get_vector_search
        vs = get_vector_search()
        conn = vs._get_connection()
        if conn is None:
            return {"status": "error", "reason": "向量库不可用", "file_path": file_path}
        try:
            # 存储 L1（符合规范：只存L1摘要，文件内容不作为L2）
            metadata = {
                "level": "l1",  # 小写，符合规范
                "category": "document",
                "file_path": file_path,
            }
            conn.execute(
                "INSERT INTO documents (id, content, embedding, metadata) VALUES (?, ?, ?, ?)",
                (l1_id, l1, embedding_blob, json.dumps(metadata)),
            )

            # L2不存储到向量库（文件内容保留在原位置，通过L1指针访问）
            # 根据规范：L2只存储对话产生的内容，文件不应该作为L2

            conn.commit()
        except Exception as db_err:
            try:
                conn.rollback()
            except Exception:
                pass
            raise RuntimeError(f"向量库写入失败: {db_err}") from db_err

        logger.info(f"[STORE_L1] L1 存储成功: {l1_id}")

        # 同步到知识图谱（失败不影响向量库写入）
        kg_result = sync_to_kg(file_path, l1, source="document")

        return {
            "status": "success",
            "l1_id": l1_id,
            "file_path": file_path,
            "message": "文档摘要已存储到向量库",
            "lightrag_sync": kg_result,
        }

    except Exception as e:
        logger.exception(f"[STORE_L1] 失败: {e}")
        return {
            "status": "error",
            "error_code": "STORE_FAILED",
            "message": str(e),
        }


def store_documents_l1(documents: list[dict]) -> dict:
    """批量存储文档的 L1 摘要到向量库

    参数:
    - documents: 文档列表，每个包含 file_path, l1, l2(可选)
    """
    import uuid

    results = []
    success_count = 0
    failed_count = 0

    try:
        # 使用 VectorSearchAdapter 的共享连接（避免独立连接与共享连接并发冲突）
        from agent.vector_search import get_vector_search
        vs = get_vector_search()
        conn = vs._get_connection()
        if conn is None:
            return {"status": "error", "reason": "向量库不可用", "total": len(documents)}
        try:
            for doc in documents:
                file_path = doc.get("file_path", "")
                l1 = doc.get("l1", "")
                l2 = doc.get("l2")

                if not file_path or not l1:
                    results.append(
                        {
                            "file_path": file_path,
                            "status": "error",
                            "reason": "缺少 file_path 或 l1",
                        }
                    )
                    failed_count += 1
                    continue

                try:
                    # 生成唯一 ID
                    l1_id = f"doc_{uuid.uuid4().hex[:12]}"

                    # 生成向量
                    embedding_blob = None
                    embedding_result = call_embedding_service("/encode", {"text": l1})
                    if embedding_result and "embedding" in embedding_result:
                        embedding_blob = np.array(
                            embedding_result["embedding"], dtype=np.float32
                        ).tobytes()

                    # Embedding 失败则跳过（无向量的记录无法被检索，是废数据）
                    if embedding_blob is None:
                        results.append(
                            {
                                "file_path": file_path,
                                "status": "error",
                                "reason": "Embedding 服务不可用，无法生成向量",
                            }
                        )
                        failed_count += 1
                        continue

                    # 存储 L1
                    metadata = {
                        "level": "l1",
                        "category": "document",
                        "file_path": file_path,
                    }
                    conn.execute(
                        "INSERT INTO documents (id, content, embedding, metadata) VALUES (?, ?, ?, ?)",
                        (l1_id, l1, embedding_blob, json.dumps(metadata)),
                    )

                    # 存储 L2（如果提供且 embedding 成功）
                    l2_id = None
                    if l2 and len(l2) <= 10000:
                        l2_embedding_blob = None
                        l2_embedding_result = call_embedding_service(
                            "/encode", {"text": l2}
                        )
                        if l2_embedding_result and "embedding" in l2_embedding_result:
                            l2_embedding_blob = np.array(
                                l2_embedding_result["embedding"], dtype=np.float32
                            ).tobytes()

                        if l2_embedding_blob is not None:
                            l2_id = f"doc_{uuid.uuid4().hex[:12]}"
                            l2_metadata = {
                                "level": "l2",
                                "category": "document",
                                "file_path": file_path,
                                "l1_id": l1_id,
                            }
                            conn.execute(
                                "INSERT INTO documents (id, content, embedding, metadata) VALUES (?, ?, ?, ?)",
                                (l2_id, l2, l2_embedding_blob, json.dumps(l2_metadata)),
                            )
                        else:
                            logger.warning(f"[STORE_DOCS_L1] L2 embedding failed for {file_path}, skipping L2")

                    # 同步到知识图谱（失败不影响向量库写入）
                    kg_result = sync_to_kg(file_path, l1, source="document")

                    results.append(
                        {
                            "file_path": file_path,
                            "status": "success",
                            "l1_id": l1_id,
                            "l2_id": l2_id,
                            "lightrag_sync": kg_result,
                        }
                    )
                    success_count += 1

                except Exception as e:
                    logger.error(f"[STORE_DOCS_L1] 单个文件失败: {file_path}, {e}")
                    results.append(
                        {
                            "file_path": file_path,
                            "status": "error",
                            "reason": str(e),
                        }
                    )
                    failed_count += 1

            conn.commit()
        except Exception as db_err:
            try:
                conn.rollback()
            except Exception:
                pass
            raise RuntimeError(f"向量库写入失败: {db_err}") from db_err

        logger.info(f"[STORE_DOCS_L1] 批量存储完成: {success_count}/{len(documents)}")

        return {
            "status": "success" if failed_count == 0 else "partial_success",
            "total": len(documents),
            "processed": success_count,
            "failed": failed_count,
            "results": results,
            "message": f"已存储 {success_count}/{len(documents)} 个文档摘要",
        }

    except Exception as e:
        logger.exception(f"[STORE_DOCS_L1] 失败: {e}")
        return {
            "status": "error",
            "error_code": "BATCH_STORE_FAILED",
            "message": str(e),
            "results": results,
        }


def ingest_documents(
    file_paths: list[str], category: str = "其他", mode: str = "copy"
) -> dict:
    """批量文档入库

    返回 need_l1 时，需要为每个新文件调用 store_document_l1 存储 L1 摘要。
    """
    results = []
    need_l1_files = []  # 需要生成 L1 的文件
    skipped_files = []  # 跳过的文件
    failed_files = []  # 失败的文件

    for file_path in file_paths:
        result = ingest_document(file_path, category, mode)

        if result["status"] == "need_l1":
            # 新文件，需要 L1
            need_l1_files.append(
                {
                    "file": Path(file_path).name,
                    "file_path": result["file_path"],
                    "content": result.get("content"),
                }
            )
            results.append(
                {
                    "file": Path(file_path).name,
                    "status": "need_l1",
                    "action": result.get("action", ""),
                    "path": result.get("file_path", ""),
                }
            )
        elif result["status"] == "success":
            # 跳过的文件
            skipped_files.append(Path(file_path).name)
            results.append(
                {
                    "file": Path(file_path).name,
                    "status": "success",
                    "action": result.get("action", ""),
                    "path": result.get("file_path", ""),
                }
            )
        else:
            # 失败
            failed_files.append(
                {
                    "file": Path(file_path).name,
                    "reason": result.get("message", ""),
                }
            )
            results.append(
                {
                    "file": Path(file_path).name,
                    "status": "error",
                    "reason": result.get("message", ""),
                }
            )

    # 如果有需要 L1 的文件，返回 need_l1 状态驱动工具循环
    if need_l1_files:
        return {
            "status": "need_l1",
            "total": len(file_paths),
            "new_files": len(need_l1_files),
            "skipped": len(skipped_files),
            "failed": len(failed_files),
            "files_need_l1": need_l1_files,
            "hint": f"有 {len(need_l1_files)} 个新文件需要生成 L1 摘要。请一次性为所有文件生成 L1，然后调用 store_documents_l1 批量存储。",
            "example": {
                "tool": "store_documents_l1",
                "documents": [
                    {
                        "file_path": "<file_path>",
                        "l1": "标题|关键词|摘要|实体|类型|指针",
                    }
                ],
            },
        }

    # 全部完成（都是跳过或失败）
    return {
        "status": "success",
        "total": len(file_paths),
        "processed": len(skipped_files),
        "failed": len(failed_files),
        "results": results,
        "summary": f"已处理 {len(skipped_files)}/{len(file_paths)} 文件，{len(failed_files)} 个失败",
    }


# ============== MCP Server ==============

server = Server("niu-photo-server")


@server.list_tools()
async def list_tools() -> list[Tool]:
    """列出可用工具"""
    return [
        Tool(
            name="ingest_document",
            description="""文档入库工具

参数:
- file_path: 必填，源文件绝对路径
- category: 分类名称，从 preferences.json 的 categories.documents 中选取（财务、合同、报告、方案、其他）
- mode: copy（复制）| move（移动）| reference（引用）

返回:
- status: success | error
- action: created | versioned | renamed | referenced | skipped
- file_path: 存储后的完整路径
- note: 处理说明

冲突处理:
- 完全相同文件（哈希相同）→ 跳过
- 内容相似（语义相似度 > 阈值）→ 版本管理
- 内容不同 → 自动改名""",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "源文件绝对路径"},
                    "category": {
                        "type": "string",
                        "description": "分类（财务/合同/报告/方案/其他）",
                        "default": "其他",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["copy", "move", "reference"],
                        "default": "copy",
                    },
                },
                "required": ["file_path"],
            },
        ),
        Tool(
            name="ingest_documents",
            description="""批量文档入库工具

参数:
- file_paths: 必填，源文件路径列表
- category: 分类名称
- mode: copy | move | reference

返回:
- status: success
- total: 总数
- processed: 成功数
- failed: 失败数
- results: 每个文件的处理结果
- summary: 总结""",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "源文件路径列表",
                    },
                    "category": {"type": "string", "default": "其他"},
                    "mode": {
                        "type": "string",
                        "enum": ["copy", "move", "reference"],
                        "default": "copy",
                    },
                },
                "required": ["file_paths"],
            },
        ),
        Tool(
            name="ingest_photo",
            description="""照片入库工具（带人脸识别）

参数:
- file_path: 必填，照片文件绝对路径
- category: 分类（生活/工作/旅行/证件/其他），默认从 preferences.json 读取

返回:
- status: success | error
- photo_id: 照片唯一ID
- detected_persons: 检测到的人物列表 [{id, name, similarity}]
- abstract: L0 摘要（人物+时间）
- exif: EXIF 信息（taken_at, location, camera）

处理流程:
1. 提取 EXIF 信息（拍摄时间、GPS、相机）
2. 使用 InsightFace 检测人脸
3. 匹配已有人物或创建"未命名人物_N"
4. 生成 L0 摘要""",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "照片文件绝对路径"},
                    "category": {
                        "type": "string",
                        "description": "分类（生活/工作/旅行/证件/其他）",
                    },
                },
                "required": ["file_path"],
            },
        ),
        Tool(
            name="name_person",
            description="""为人物命名

参数:
- person_id: 必填，人物ID
- name: 必填，新名称

返回:
- status: success | error
- person_id: 人物ID
- name: 新名称
- auto_label: 自动标签（如"未命名人物_1"）""",
            inputSchema={
                "type": "object",
                "properties": {
                    "person_id": {"type": "string", "description": "人物ID"},
                    "name": {"type": "string", "description": "新名称"},
                },
                "required": ["person_id", "name"],
            },
        ),
        Tool(
            name="merge_persons",
            description="""合并两个人物

参数:
- person_a_id: 必填，保留的人物ID
- person_b_id: 必填，要合并到A的人物ID

返回:
- status: success | error
- merged_into: 合并到的人物ID
- name: 保留的名称
- photo_count: 合并后的照片数量
- deleted_person_id: 被删除的人物ID

说明:
- 保留 person_a 的名称
- 合并所有人脸向量，重新计算中心
- 更新所有照片关联
- 学习机制：如果相似度低于阈值，自动调整阈值""",
            inputSchema={
                "type": "object",
                "properties": {
                    "person_a_id": {"type": "string", "description": "保留的人物ID"},
                    "person_b_id": {"type": "string", "description": "要合并的人物ID"},
                },
                "required": ["person_a_id", "person_b_id"],
            },
        ),
        Tool(
            name="ingest_photos",
            description="""智能照片入库（自动判断单张/目录）

参数:
- source_path: 必填，**单个**文件路径或目录路径
- category: 分类（生活/工作/旅行/证件/其他）

两种模式:
1. 目录路径 → 批量模式：保持原目录结构，整体搬迁
2. 单个文件路径 → 单张模式：按模板重命名、人脸识别、分类存储

⚠️ 重要：
- 此工具只接受**一个路径**（文件或目录）
- 多个独立文件 → 需要分别调用此工具多次（每个文件一次）
- 不要提取共同目录路径，而是逐个处理每个文件

批量模式返回:
- total: 照片总数
- success: 成功数
- target_path: 目标目录

单张模式返回:
- photo_id: 照片ID
- detected_persons: 检测到的人物
- file_path: 存储路径""",
            inputSchema={
                "type": "object",
                "properties": {
                    "source_path": {
                        "type": "string",
                        "description": "文件路径或目录路径",
                    },
                    "category": {
                        "type": "string",
                        "description": "分类（生活/工作/旅行/证件/其他）",
                    },
                },
                "required": ["source_path"],
            },
        ),
        Tool(
            name="search_persons",
            description="""搜索人物（按名字模糊匹配）

参数:
- query: 搜索词（人名，子串匹配）
- limit: 返回数量（默认10）

返回:
- 匹配的人物列表""",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索词（人名）"},
                    "limit": {
                        "type": "integer",
                        "description": "返回数量",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="get_unnamed_persons",
            description="""获取所有未命名人物

返回:
- 未命名人物列表，按出现次数排序
- 包含：id, auto_label, photo_count, has_valid_photos, photos: [{file_path, boxed_path}]
- boxed_path 是带人脸红框的图片路径，前端用 ::person_photo:: 标记显示
- has_valid_photos=false 表示该人物的照片文件已不存在""",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="delete_person",
            description="""删除人物及其所有关联数据

警告：这会删除人物图谱中的节点，请谨慎使用。
只有在用户明确要求删除时才调用。

参数:
- person_id: 要删除的人物ID

返回:
- status: success | error
- message: 结果说明""",
            inputSchema={
                "type": "object",
                "properties": {
                    "person_id": {"type": "string", "description": "人物ID"},
                },
                "required": ["person_id"],
            },
        ),
        Tool(
            name="cleanup_deleted_photos",
            description="""清理已删除照片的数据库记录

在删除照片文件/目录后调用，清理数据库中的孤儿记录。
扫描 photos 表，删除文件不存在的记录及其关联的 faces 记录。

返回:
- deleted_photos: 删除的照片记录数
- deleted_faces: 删除的人脸记录数

使用场景：
- 用户删除了照片目录后
- 清理数据库中的残留记录""",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="get_person_photos",
            description="""获取某人物的多张照片（用于"换一张"场景）

当用户说"看不清"、"换一张"时调用此工具。

参数:
- person_id: 人物ID
- limit: 最多返回几张照片（默认5）

返回:
- person_id, person_name
- photos: [{file_path, boxed_path, taken_at}, ...]

boxed_path 是带人脸红框的图片路径，前端用 ::person_photo:: 标记显示。""",
            inputSchema={
                "type": "object",
                "properties": {
                    "person_id": {"type": "string", "description": "人物ID"},
                    "limit": {
                        "type": "integer",
                        "description": "最多返回几张（默认5）",
                    },
                },
                "required": ["person_id"],
            },
        ),
        Tool(
            name="store_document_l1",
            description="""存储单个文档的 L1 摘要到向量库

当 ingest_document 返回 status="need_l1" 时，调用此工具存储生成的摘要。

参数:
- file_path: 必填，文档存储路径（从 ingest_document 返回值获取）
- l1: 必填，极简格式摘要：标题|关键词|摘要|实体|类型|指针
- l2: 可选，完整内容（如果不提供则只存储 L1）

返回:
- status: success | error
- l1_id: 文档ID

L1 格式说明:
- 标题：文档标题
- 关键词：3-5个核心概念，用逗号分隔
- 摘要：50-80字现代中文摘要
- 实体：命名实体（人名、地名、技术名词）
- 类型：文档类型（技术文档/合同/报告等）
- 指针：文件路径或其他定位信息

示例:
Zellij使用指南|终端,复用器,Rust|Zellij终端复用器的基本使用方法和配置说明|Zellij,终端|技术文档|/docs/zellij.md""",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "文档存储路径"},
                    "l1": {"type": "string", "description": "L1 极简格式摘要"},
                    "l2": {"type": "string", "description": "完整内容（可选）"},
                },
                "required": ["file_path", "l1"],
            },
        ),
        Tool(
            name="store_documents_l1",
            description="""批量存储文档的 L1 摘要到向量库

当 ingest_documents 返回 status="need_l1" 时，调用此工具一次性存储所有摘要。

参数:
- documents: 必填，文档列表，每个包含：
  - file_path: 文档存储路径
  - l1: L1 极简格式摘要
  - l2: 完整内容（可选）

返回:
- status: success | error
- total: 总数
- processed: 成功数
- failed: 失败数
- results: 每个文档的处理结果

这是批量处理的首选方式，一次调用完成所有 L1 存储，然后向主 Agent 汇报。""",
            inputSchema={
                "type": "object",
                "properties": {
                    "documents": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "file_path": {"type": "string"},
                                "l1": {"type": "string"},
                                "l2": {"type": "string"},
                            },
                            "required": ["file_path", "l1"],
                        },
                        "description": "文档列表",
                    },
                },
                "required": ["documents"],
            },
        ),
        Tool(
            name="unload_face_model",
            description="""卸载人脸识别模型，释放内存（约 326MB）

在长时间空闲时调用此工具释放内存。
通常由系统在 SLEEP 状态时自动调用。

返回:
- status: success
- message: 卸载结果""",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """调用工具 - 同步调用，避免 asyncio.to_thread 在 MCP stdio 环境中的问题"""
    try:
        logger.info(f"[CALL_TOOL] 开始: {name}")

        if name == "ingest":
            import sys as _sys
            # __file__ = .../mcp-servers/photo-server/src/niu_photo_server/__init__.py
            # 5 parents to reach project root
            _scripts_dir = str(Path(__file__).parent.parent.parent.parent.parent / "scripts")
            if _scripts_dir not in _sys.path:
                _sys.path.insert(0, _scripts_dir)
            from ingest_unified import ingest as unified_ingest
            result = unified_ingest(
                path=arguments["path"],
                mode=arguments.get("mode", "copy"),
                category=arguments.get("category"),
                l1=arguments.get("l1"),
                file_path=arguments.get("file_path"),
            )
        elif name == "ingest_document":
            result = ingest_document(
                file_path=arguments["file_path"],
                category=arguments.get("category", "其他"),
                mode=arguments.get("mode", "copy"),
            )
        elif name == "ingest_documents":
            result = ingest_documents(
                file_paths=arguments["file_paths"],
                category=arguments.get("category", "其他"),
                mode=arguments.get("mode", "copy"),
            )
        elif name == "ingest_photo":
            result = ingest_photo(
                file_path=arguments["file_path"],
                category=arguments.get("category"),
            )
        elif name == "name_person":
            result = name_person(
                person_id=arguments["person_id"],
                name=arguments["name"],
            )
        elif name == "merge_persons":
            result = merge_persons(
                person_a_id=arguments["person_a_id"],
                person_b_id=arguments["person_b_id"],
            )
        elif name == "delete_person":
            result = delete_person(person_id=arguments["person_id"])
        elif name == "cleanup_deleted_photos":
            result = cleanup_deleted_photos()
        elif name == "ingest_photos":
            result = ingest_photos(
                source_path=arguments["source_path"],
                category=arguments.get("category"),
            )
        elif name == "search_persons":
            persons = search_persons(
                query=arguments["query"],
                limit=arguments.get("limit", 10),
            )
            result = {
                "status": "success",
                "query": arguments["query"],
                "results": persons,
            }
        elif name == "get_unnamed_persons":
            persons = get_unnamed_persons()
            result = {"status": "success", "count": len(persons), "persons": persons}
        elif name == "get_person_photos":
            result = get_person_photos(
                person_id=arguments["person_id"],
                limit=arguments.get("limit", 5),
            )
        elif name == "store_document_l1":
            result = store_document_l1(
                file_path=arguments["file_path"],
                l1=arguments["l1"],
                l2=arguments.get("l2"),
            )
        elif name == "store_documents_l1":
            result = store_documents_l1(
                documents=arguments["documents"],
            )
        elif name == "unload_face_model":
            result = unload_face_model()
        else:
            result = {
                "status": "error",
                "error_code": "UNKNOWN_TOOL",
                "message": f"未知工具: {name}",
            }

        logger.info(f"[CALL_TOOL] 返回: {result.get('status')} {result.get('action')}")
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

    except Exception as e:
        logger.exception(f"[CALL_TOOL] 错误: {e}")
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {"status": "error", "message": str(e)}, ensure_ascii=False
                ),
            )
        ]


async def run_server():
    """运行 MCP 服务器"""
    # 使用共享向量服务，无需预加载 embedding model
    logger.info("[INIT] Photo server starting (using shared embedding service)")

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


def preload_face_model():
    """预加载人脸识别模型（在 MCP stdio 启动前）"""
    import sys
    import os
    from pathlib import Path

    # 跨平台日志路径：使用 ~/.niu/logs
    log_dir = Path.home() / ".niu" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "photo-server-preload.log"

    # 强制输出到 stderr，确保在 MCP stdio 启动前可见
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(f"[PRELOAD] Starting at {datetime.now().isoformat()}\n")
        f.flush()

    print("[PRELOAD] Pre-loading InsightFace and cv2...", file=sys.stderr, flush=True)
    logger.info("[PRELOAD] Pre-loading InsightFace and cv2...")

    try:
        print("[PRELOAD] Importing cv2...", file=sys.stderr, flush=True)
        import cv2

        print(
            f"[PRELOAD] cv2 imported, version: {cv2.__version__}",
            file=sys.stderr,
            flush=True,
        )

        print("[PRELOAD] Importing InsightFace...", file=sys.stderr, flush=True)
        from insightface.app import FaceAnalysis

        print("[PRELOAD] InsightFace imported", file=sys.stderr, flush=True)

        # 预加载模型（可选，看是否需要在启动时加载）
        # models_dir = get_models_dir()
        # face_model = FaceAnalysis(name="buffalo_l", root=str(models_dir), providers=["CPUExecutionProvider"])
        # face_model.prepare(ctx_id=-1)
        # global _face_model
        # _face_model = face_model

        print("[PRELOAD] Pre-load complete", file=sys.stderr, flush=True)
        logger.info("[PRELOAD] Pre-load complete")
    except Exception as e:
        print(f"[PRELOAD] Failed: {e}", file=sys.stderr, flush=True)
        logger.warning(f"[PRELOAD] Failed: {e}")


def main():
    """入口函数"""
    # 预加载 cv2 和 InsightFace 模块代码（不是模型）
    # 这是关键：在 MCP stdio 启动前预加载模块，避免动态导入时卡死
    # 注意：只导入模块，不加载模型（模型按需加载）
    preload_face_model()
    # 启动后台模型卸载定时器
    _start_model_unload_timer()
    asyncio.run(run_server())


if __name__ == "__main__":
    main()
