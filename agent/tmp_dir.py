"""临时文件目录管理 — 存放画了人脸框的图片等数据库无法直接存储的内容"""
import datetime
import json
import os
import shutil
import tempfile
from pathlib import Path


def get_tmp_dir() -> Path:
    """获取临时目录 ~/.niu/tmp/"""
    tmp_dir = Path.home() / ".niu" / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    return tmp_dir


def save_to_tmp(filename: str, data: bytes) -> str:
    """保存文件到临时目录，返回绝对路径"""
    tmp_dir = get_tmp_dir()
    filepath = tmp_dir / filename
    filepath.write_bytes(data)
    return str(filepath)


def is_tmp_file(filepath: str) -> bool:
    """判断文件是否在临时目录中"""
    tmp_dir = str(get_tmp_dir())
    # 统一用正斜杠比较，兼容 Windows
    return filepath.replace("\\", "/").startswith(tmp_dir.replace("\\", "/"))


def cleanup_tmp_files(filepaths: list[str]) -> int:
    """删除临时目录中的文件，返回删除数量"""
    deleted = 0
    for fp in filepaths:
        if is_tmp_file(fp) and os.path.exists(fp):
            os.remove(fp)
            deleted += 1
    return deleted


def cleanup_all_tmp() -> int:
    """清空整个临时目录，返回删除数量"""
    tmp_dir = get_tmp_dir()
    if not tmp_dir.exists():
        return 0
    count = sum(1 for _ in tmp_dir.iterdir())
    shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    return count


def cleanup_old_tmp() -> int:
    """清理超过24小时的临时文件（含子目录），空目录自动删除，返回删除文件数量"""
    tmp_dir = get_tmp_dir()
    if not tmp_dir.exists():
        return 0
    cutoff = datetime.datetime.now() - datetime.timedelta(hours=24)
    deleted = 0
    # topdown=False: 自底向上遍历，先处理文件再处理子目录
    for root, dirs, files in os.walk(tmp_dir, topdown=False):
        for filename in files:
            filepath = os.path.join(root, filename)
            try:
                mtime = datetime.datetime.fromtimestamp(os.path.getmtime(filepath))
            except OSError:
                continue
            if mtime < cutoff:
                try:
                    os.remove(filepath)
                    deleted += 1
                except OSError:
                    pass
        for dirname in dirs:
            dirpath = os.path.join(root, dirname)
            try:
                os.rmdir(dirpath)  # 只删除空目录，非空会抛 OSError
            except OSError:
                pass
    return deleted


def write_archive(unique_name: str, data: dict) -> bool:
    """原子写子 Agent 完成态存档到 ~/.niu/tmp/<unique_name>.json。

    临时文件 + os.replace 保证读侧永不看到半写内容；权限 600。
    任何失败返回 False 不抛——调用方（子 Agent 完成通知组装）据返回值决定续跑承诺话术。
    """
    tmp_dir = get_tmp_dir()
    filepath = tmp_dir / f"{unique_name}.json"
    fd = None
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=str(tmp_dir), prefix=".archive-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = None  # fdopen 接管，交由 with 关闭
            json.dump(data, f, ensure_ascii=False)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, filepath)
        tmp_path = None  # replace 成功，无需清理
        return True
    except Exception:
        return False
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path is not None:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def read_archive(unique_name: str) -> dict | None:
    """读取子 Agent 完成态存档 ~/.niu/tmp/<unique_name>.json。

    不存在 / JSON 解析失败 / 非 dict 形态 → None。
    不做类型假设：tmp 顶层可能有同名非存档 JSON，agent_type 校验由读档方负责。
    """
    filepath = get_tmp_dir() / f"{unique_name}.json"
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
