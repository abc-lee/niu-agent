#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一入库工具 — 替代 ingest_photo / ingest_photos / ingest_document / ingest_documents

自动判断路径类型（文件/目录）和内容类型（照片/文档/混合），
与子 Agent 形成 L1 生成循环。
"""

import json
import os
import shutil
import sys
import uuid
from datetime import datetime
from enum import Enum
from pathlib import Path

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".heif"}

# 导入 photo-server 内部函数
_project_root = Path(__file__).parent.parent
_photo_server_src = _project_root / "mcp-servers" / "photo-server" / "src"
if str(_photo_server_src) not in sys.path:
    sys.path.insert(0, str(_photo_server_src))


class PathType(Enum):
    FILE = "file"
    DIRECTORY = "directory"
    NOT_FOUND = "not_found"


class ContentType(Enum):
    PHOTO = "photo"
    DOCUMENT = "document"
    MIXED = "mixed"
    EMPTY = "empty"


class PathInfo:
    def __init__(self, path_type: PathType, content_type: ContentType):
        self.path_type = path_type
        self.content_type = content_type


def classify_path(path: Path) -> PathInfo:
    """判断路径类型和内容类型"""
    if not path.exists():
        return PathInfo(PathType.NOT_FOUND, ContentType.EMPTY)
    if path.is_file():
        ext = path.suffix.lower()
        if ext in PHOTO_EXTENSIONS:
            return PathInfo(PathType.FILE, ContentType.PHOTO)
        return PathInfo(PathType.FILE, ContentType.DOCUMENT)
    if path.is_dir():
        photos = 0
        docs = 0
        for f in path.rglob("*"):
            if f.is_file():
                if f.suffix.lower() in PHOTO_EXTENSIONS:
                    photos += 1
                else:
                    docs += 1
        if photos > 0 and docs > 0:
            return PathInfo(PathType.DIRECTORY, ContentType.MIXED)
        if photos > 0:
            return PathInfo(PathType.DIRECTORY, ContentType.PHOTO)
        if docs > 0:
            return PathInfo(PathType.DIRECTORY, ContentType.DOCUMENT)
        return PathInfo(PathType.DIRECTORY, ContentType.EMPTY)
    return PathInfo(PathType.NOT_FOUND, ContentType.EMPTY)


def _get_photo_server():
    """延迟导入 photo-server 模块"""
    import niu_photo_server
    return niu_photo_server


def _ingest_single_photo(path: str, category: str | None = None, mode: str = "copy") -> dict:
    """单张照片入库（完整流程：EXIF + 人脸 + KG + 向量库）"""
    ps = _get_photo_server()
    source = Path(path)

    if not source.exists():
        return {"status": "error", "error_code": "FILE_NOT_FOUND", "message": f"文件不存在: {path}"}

    # 默认分类
    if category is None:
        try:
            prefs = ps.get_preferences()
            category = prefs.get("categories", {}).get("photos", ["生活"])[0]
        except Exception:
            category = "生活"

    conn = ps.get_connection()
    now = datetime.now().isoformat()
    final_path = None

    try:
        # 1. 提取 EXIF
        exif = ps.extract_exif(str(source))
        taken_at = exif.get("taken_at")
        location = exif.get("location")
        camera = exif.get("camera")

        # 2. 人脸检测
        detected_persons = []
        face_embeddings = []  # 保留原始 embedding 用于写入 faces 表
        seen_person_ids = set()  # 防止同一人物多张人脸时 photo_count 重复递增
        try:
            faces = ps.detect_faces(str(source))
            for face_data in faces:
                face_embedding = face_data["embedding"]
                bbox = face_data.get("bbox", [])
                confidence = face_data.get("confidence", 0)

                # 匹配已有人物
                match_id, similarity = ps.match_face_to_person(face_embedding)
                if match_id:
                    person_id = match_id
                else:
                    # 创建新人物（与原始 ingest_photo 保持一致）
                    person_id = str(uuid.uuid4())
                    auto_label = ps.get_next_auto_label()
                    similarity = 1.0
                    conn.execute(
                        """INSERT INTO persons (id, auto_label, center_embedding, photo_count, first_seen, last_seen, created_at)
                           VALUES (?, ?, ?, 0, ?, ?, ?)""",
                        (person_id, auto_label, face_embedding.tobytes(), now, now, now),
                    )

                # 更新中心嵌入（仅首次出现时递增 photo_count）
                is_new_photo = person_id not in seen_person_ids
                ps.update_person_center(person_id, face_embedding, increment_count=is_new_photo)
                seen_person_ids.add(person_id)

                # 获取人物名称
                cursor = conn.execute("SELECT name, auto_label FROM persons WHERE id = ?", (person_id,))
                row = cursor.fetchone()
                person_name = row[0] if row and row[0] else (row[1] if row else "未知")

                detected_persons.append({
                    "id": person_id,
                    "name": person_name,
                    "similarity": similarity,
                    "bbox": bbox,
                    "confidence": confidence,
                })
                face_embeddings.append(face_embedding)
        except Exception as e:
            print(f"[ingest] 人脸检测失败: {e}", file=sys.stderr)
            try:
                conn.rollback()
            except Exception:
                pass
            detected_persons = []
            face_embeddings = []

        # 不在此处 commit，等文件复制和照片/人脸记录写入后一起提交

        # 3. 拷贝到存储目录
        workspace = ps.get_workspace_path()
        relative_dir = ps.build_photo_storage_path(category, source.name)
        target_dir = workspace / relative_dir
        target_dir.mkdir(parents=True, exist_ok=True)

        new_name = ps.build_photo_file_name(source.name, taken_at)
        target_path = target_dir / new_name

        # 冲突处理：handle_photo_conflict 返回最终路径（重名时自动改名）
        final_path = ps.handle_photo_conflict(target_path)

        if mode == "copy":
            shutil.copy2(str(source), str(final_path))
        elif mode == "move":
            shutil.move(str(source), str(final_path))
        elif mode == "reference":
            final_path = str(source)

        # 4. 生成 L0 摘要
        person_names = [p["name"] for p in detected_persons]
        abstract = ps.generate_l0_abstract(person_names, taken_at)

        # 5. 写 photos 表
        photo_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO photos (id, file_path, taken_at, location, camera, abstract, ingested_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (photo_id, str(Path(final_path).resolve()), taken_at, location, camera, abstract, now),
        )

        # 6. 写 faces 表（存储实际 embedding 字节）
        for person, face_embedding in zip(detected_persons, face_embeddings):
            face_id = str(uuid.uuid4())
            bbox_str = json.dumps(person.get("bbox", []))
            conn.execute(
                """INSERT INTO faces (id, photo_id, person_id, embedding, bounding_box, confidence)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (face_id, photo_id, person["id"], face_embedding.tobytes(), bbox_str, person.get("confidence", 0)),
            )

        # 7. 共现关系（写入同一事务，单次原子提交）
        if len(detected_persons) > 1:
            unique_persons = []
            seen_pids = set()
            for p in detected_persons:
                if p["id"] not in seen_pids:
                    seen_pids.add(p["id"])
                    unique_persons.append(p)
            if len(unique_persons) > 1:
                ps.update_co_occurrences(unique_persons, taken_at)

        conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        # 清理已复制的孤立文件
        if final_path and mode != "reference":
            try:
                if mode == "move":
                    # move 模式：将文件移回原位置，防止数据丢失
                    shutil.move(str(final_path), str(source))
                else:
                    os.remove(str(final_path))
            except OSError:
                pass
        return {"status": "error", "error_code": "INGEST_FAILED", "message": str(e)}

    # 8. KG 同步
    kg_result = None
    try:
        kg_result = ps.sync_photo_to_kg(str(final_path), abstract, detected_persons)
    except Exception as e:
        print(f"[ingest] KG 同步失败: {e}", file=sys.stderr)

    return {
        "status": "success",
        "photo_id": photo_id,
        "file_path": str(final_path),
        "original_path": str(source),
        "category": category,
        "detected_persons": detected_persons,
        "abstract": abstract,
        "exif": exif,
        "kg_sync": kg_result,
    }


def _ingest_single_document(path: str, category: str = "其他", mode: str = "copy", l1: str | None = None) -> dict:
    """单个文档入库"""
    ps = _get_photo_server()
    source = Path(path)

    if not source.exists():
        return {"status": "error", "error_code": "FILE_NOT_FOUND", "message": f"文件不存在: {path}"}

    workspace = ps.get_workspace_path()

    # 构建存储路径
    relative_dir = ps.build_storage_path(category, source.name, "documents")
    target_dir = workspace / relative_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / source.name

    # 冲突处理
    final_path, action = ps.handle_conflict(target_path, source)
    if action == "skipped":
        return {"status": "success", "action": "skipped", "file_path": str(final_path), "note": "文档已存在"}

    # 执行文件操作
    if mode == "copy":
        shutil.copy2(str(source), str(final_path))
    elif mode == "move":
        shutil.move(str(source), str(final_path))
    elif mode == "reference":
        final_path = str(source)
        action = "referenced"

    # 如果有 L1，存储到向量库
    if l1:
        l1_error = None
        try:
            ps.store_document_l1(str(final_path), l1)
        except Exception as e:
            print(f"[ingest] L1 存储失败: {e}", file=sys.stderr)
            l1_error = str(e)

        # KG 同步（即使 L1 失败也尝试同步）
        try:
            ps.sync_to_kg(str(final_path), l1, source="document")
        except Exception as e:
            print(f"[ingest] KG 同步失败: {e}", file=sys.stderr)

        if l1_error:
            return {"status": "error", "error_code": "L1_STORE_FAILED", "message": f"L1 存储失败: {l1_error}", "file_path": str(final_path)}
        return {"status": "success", "action": action, "file_path": str(final_path), "category": category}

    # 没有 L1，返回 need_l1
    file_content = None
    try:
        file_content = ps.read_file_content(str(final_path))
        if file_content and len(file_content) > 10000:
            file_content = file_content[:10000] + "\n... [内容已截断]"
    except Exception:
        pass

    return {
        "status": "need_l1",
        "action": action,
        "file_path": str(Path(final_path).resolve()),
        "original_path": str(source),
        "category": category,
        "content": file_content,
        "hint": "请生成 L1 摘要（极简格式：标题|关键词|摘要|实体|类型|指针），然后调用 ingest(file_path=..., l1=...) 存储",
    }


def ingest(
    path: str,
    mode: str = "copy",
    category: str | None = None,
    l1: str | None = None,
    file_path: str | None = None,
) -> dict:
    """统一入库工具

    Args:
        path: 文件路径或目录路径
        mode: "copy" | "move" | "reference"
        category: 分类，不传则自动推断
        l1: L1 摘要（文档入库第二轮调用时传入）
        file_path: 文档存储路径（L1 回传时使用）
    """
    # L1 回传模式
    if l1 and file_path:
        ps = _get_photo_server()
        try:
            ps.store_document_l1(file_path, l1)
        except Exception as e:
            return {"status": "error", "message": f"L1 存储失败: {e}"}
        try:
            ps.sync_to_kg(file_path, l1, source="document")
        except Exception as e:
            print(f"[ingest] KG 同步失败: {e}", file=sys.stderr)
        return {"status": "success", "file_path": file_path}

    # 防止空路径
    if not path:
        return {"status": "error", "error_code": "MISSING_PATH", "message": "path 参数不能为空"}

    source = Path(path)
    info = classify_path(source)

    if info.path_type == PathType.NOT_FOUND:
        return {"status": "error", "error_code": "FILE_NOT_FOUND", "message": f"路径不存在: {path}"}

    # 单张照片
    if info.path_type == PathType.FILE and info.content_type == ContentType.PHOTO:
        return _ingest_single_photo(path, category, mode)

    # 单个文档
    if info.path_type == PathType.FILE and info.content_type == ContentType.DOCUMENT:
        return _ingest_single_document(path, category or "其他", mode, l1)

    # 照片目录
    if info.path_type == PathType.DIRECTORY and info.content_type == ContentType.PHOTO:
        return _ingest_photo_directory(path, category, mode)

    # 文档目录
    if info.path_type == PathType.DIRECTORY and info.content_type == ContentType.DOCUMENT:
        return _ingest_document_directory(path, category or "其他", mode)

    # 混合目录
    if info.content_type == ContentType.MIXED:
        return _ingest_mixed_directory(path, category, mode)

    # 空目录
    return {"status": "error", "error_code": "EMPTY_DIRECTORY", "message": f"目录为空: {path}"}


def _ingest_photo_directory(path: str, category: str | None = None, mode: str = "copy") -> dict:
    """照片目录批量入库（逐张完整处理）"""
    source_dir = Path(path)
    # Windows 不区分大小写，只用小写扩展名搜索即可
    photo_files = []
    for ext in PHOTO_EXTENSIONS:
        photo_files.extend(source_dir.rglob(f"*{ext}"))
    # 去重（Windows 上 rglob 可能返回重复）
    seen = set()
    unique_files = []
    for pf in photo_files:
        key = str(pf.resolve()).lower()
        if key not in seen:
            seen.add(key)
            unique_files.append(pf)

    results = []
    success = 0
    failed = 0
    for pf in sorted(unique_files):
        try:
            result = _ingest_single_photo(str(pf), category, mode)
            results.append(result)
            if result.get("status") == "success":
                success += 1
            else:
                failed += 1
        except Exception as e:
            results.append({"status": "error", "file_path": str(pf), "message": str(e)})
            failed += 1

    return {
        "status": "success",
        "source_path": path,
        "total": len(unique_files),
        "success": success,
        "failed": failed,
        "results": results,
    }


def _ingest_document_directory(path: str, category: str = "其他", mode: str = "copy") -> dict:
    """文档目录批量入库"""
    source_dir = Path(path)
    doc_files = [f for f in source_dir.rglob("*") if f.is_file() and f.suffix.lower() not in PHOTO_EXTENSIONS]

    results = []
    need_l1_files = []
    for df in sorted(doc_files):
        try:
            result = _ingest_single_document(str(df), category, mode)
            results.append(result)
            if result.get("status") == "need_l1":
                need_l1_files.append(result)
        except Exception as e:
            results.append({"status": "error", "file_path": str(df), "message": str(e)})

    if need_l1_files:
        return {
            "status": "need_l1",
            "total": len(doc_files),
            "need_l1": len(need_l1_files),
            "files": need_l1_files,
            "hint": "请为每个文件生成 L1 摘要，然后调用 ingest(file_path=..., l1=...) 存储",
        }

    return {"status": "success", "total": len(doc_files), "results": results}


def _ingest_mixed_directory(path: str, category: str | None = None, mode: str = "copy") -> dict:
    """混合目录入库"""
    source_dir = Path(path)
    photo_files = []
    doc_files = []
    for f in source_dir.rglob("*"):
        if f.is_file():
            if f.suffix.lower() in PHOTO_EXTENSIONS:
                photo_files.append(f)
            else:
                doc_files.append(f)

    # 照片部分
    photo_result = _ingest_photo_directory(path, category, mode) if photo_files else {"total": 0, "success": 0}

    # 文档部分
    doc_result = _ingest_document_directory(path, category or "其他", mode) if doc_files else {"total": 0, "need_l1": 0}

    return {
        "status": "success" if doc_result.get("status") != "need_l1" else "need_l1",
        "photos": {"total": len(photo_files), "success": photo_result.get("success", 0)},
        "documents": {"total": len(doc_files), "need_l1": doc_result.get("need_l1", 0)},
        "doc_files": doc_result.get("files", []) if doc_result.get("status") == "need_l1" else [],
    }
