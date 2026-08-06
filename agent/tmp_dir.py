"""临时文件目录管理 — 存放画了人脸框的图片等数据库无法直接存储的内容"""
import datetime
import os
import shutil
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
