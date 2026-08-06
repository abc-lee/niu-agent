"""Tests for agent/tmp_dir.py — temporary file directory management"""
import os
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def tmp_dir_fixture(tmp_path):
    """Use tmp_path as ~/.niu/tmp/ for isolation"""
    niu_tmp = tmp_path / ".niu" / "tmp"
    niu_tmp.mkdir(parents=True, exist_ok=True)
    with patch("agent.tmp_dir.Path.home", return_value=tmp_path):
        yield niu_tmp


class TestGetTmpDir:
    def test_creates_directory(self, tmp_dir_fixture):
        from agent.tmp_dir import get_tmp_dir
        result = get_tmp_dir()
        assert result.exists()
        assert result.is_dir()

    def test_returns_path_object(self, tmp_dir_fixture):
        from agent.tmp_dir import get_tmp_dir
        result = get_tmp_dir()
        assert isinstance(result, Path)

    def test_idempotent(self, tmp_dir_fixture):
        from agent.tmp_dir import get_tmp_dir
        result1 = get_tmp_dir()
        result2 = get_tmp_dir()
        assert result1 == result2


class TestSaveToTmp:
    def test_saves_file_and_returns_path(self, tmp_dir_fixture):
        from agent.tmp_dir import save_to_tmp
        data = b"test image data"
        path = save_to_tmp("test.jpg", data)
        assert os.path.exists(path)
        with open(path, "rb") as f:
            assert f.read() == data

    def test_path_is_under_tmp_dir(self, tmp_dir_fixture):
        from agent.tmp_dir import get_tmp_dir, save_to_tmp
        path = save_to_tmp("test.jpg", b"data")
        tmp_dir = str(get_tmp_dir())
        assert path.startswith(tmp_dir)

    def test_overwrites_existing(self, tmp_dir_fixture):
        from agent.tmp_dir import save_to_tmp
        save_to_tmp("test.jpg", b"old")
        path = save_to_tmp("test.jpg", b"new")
        with open(path, "rb") as f:
            assert f.read() == b"new"


class TestIsTmpFile:
    def test_returns_true_for_tmp_file(self, tmp_dir_fixture):
        from agent.tmp_dir import is_tmp_file, save_to_tmp
        path = save_to_tmp("test.jpg", b"data")
        assert is_tmp_file(path) is True

    def test_returns_false_for_non_tmp_file(self, tmp_dir_fixture):
        from agent.tmp_dir import is_tmp_file
        assert is_tmp_file("C:/Users/test/photo.jpg") is False
        assert is_tmp_file("/home/user/photo.jpg") is False


class TestCleanupTmpFiles:
    def test_deletes_specified_tmp_files(self, tmp_dir_fixture):
        from agent.tmp_dir import cleanup_tmp_files, save_to_tmp
        path1 = save_to_tmp("a.jpg", b"data1")
        path2 = save_to_tmp("b.jpg", b"data2")
        assert os.path.exists(path1)
        assert os.path.exists(path2)

        deleted = cleanup_tmp_files([path1, path2])
        assert deleted == 2
        assert not os.path.exists(path1)
        assert not os.path.exists(path2)

    def test_skips_non_tmp_files(self, tmp_dir_fixture):
        from agent.tmp_dir import cleanup_tmp_files
        # Non-tmp file should be skipped (not deleted)
        deleted = cleanup_tmp_files(["C:/Users/test/photo.jpg"])
        assert deleted == 0

    def test_skips_nonexistent_files(self, tmp_dir_fixture):
        from agent.tmp_dir import cleanup_tmp_files, get_tmp_dir
        fake_path = str(get_tmp_dir() / "nonexistent.jpg")
        deleted = cleanup_tmp_files([fake_path])
        assert deleted == 0

    def test_mixed_files(self, tmp_dir_fixture):
        from agent.tmp_dir import cleanup_tmp_files, save_to_tmp
        path = save_to_tmp("a.jpg", b"data")
        deleted = cleanup_tmp_files([path, "C:/non-tmp.jpg", "/tmp/fake.jpg"])
        assert deleted == 1
        assert not os.path.exists(path)


class TestCleanupAllTmp:
    def test_removes_all_files_in_tmp_dir(self, tmp_dir_fixture):
        from agent.tmp_dir import cleanup_all_tmp, get_tmp_dir, save_to_tmp
        save_to_tmp("a.jpg", b"data1")
        save_to_tmp("b.jpg", b"data2")

        deleted = cleanup_all_tmp()
        assert deleted == 2
        # Directory should still exist (recreated)
        assert get_tmp_dir().exists()
        # But be empty
        assert len(list(get_tmp_dir().iterdir())) == 0

    def test_returns_zero_for_empty_dir(self, tmp_dir_fixture):
        from agent.tmp_dir import cleanup_all_tmp
        deleted = cleanup_all_tmp()
        assert deleted == 0


class TestCleanupOldTmp:
    def test_deletes_old_files_in_root(self, tmp_dir_fixture):
        """根目录超过24小时的文件被删除"""
        import time
        from agent.tmp_dir import cleanup_old_tmp
        # 创建一个旧文件（修改时间设为2天前）
        old_file = tmp_dir_fixture / "old.txt"
        old_file.write_text("old")
        old_time = time.time() - (2 * 24 * 3600)  # 2天前
        os.utime(old_file, (old_time, old_time))
        # 创建一个新文件
        new_file = tmp_dir_fixture / "new.txt"
        new_file.write_text("new")
        deleted = cleanup_old_tmp()
        assert deleted == 1
        assert not old_file.exists()
        assert new_file.exists()

    def test_deletes_old_files_in_subdirectory(self, tmp_dir_fixture):
        """子目录中超过24小时的文件被删除"""
        import time
        from agent.tmp_dir import cleanup_old_tmp
        subdir = tmp_dir_fixture / "subdir"
        subdir.mkdir()
        old_file = subdir / "old_sub.txt"
        old_file.write_text("old in subdir")
        old_time = time.time() - (2 * 24 * 3600)
        os.utime(old_file, (old_time, old_time))
        deleted = cleanup_old_tmp()
        assert deleted == 1
        assert not old_file.exists()

    def test_deletes_old_files_in_nested_subdirectory(self, tmp_dir_fixture):
        """多级子目录中超过24小时的文件被删除"""
        import time
        from agent.tmp_dir import cleanup_old_tmp
        deep_dir = tmp_dir_fixture / "a" / "b" / "c"
        deep_dir.mkdir(parents=True)
        old_file = deep_dir / "deep_old.txt"
        old_file.write_text("deep old")
        old_time = time.time() - (2 * 24 * 3600)
        os.utime(old_file, (old_time, old_time))
        deleted = cleanup_old_tmp()
        assert deleted == 1
        assert not old_file.exists()

    def test_deletes_empty_directories(self, tmp_dir_fixture):
        """文件被删除后空目录被删除"""
        import time
        from agent.tmp_dir import cleanup_old_tmp
        subdir = tmp_dir_fixture / "empty_after_cleanup"
        subdir.mkdir()
        old_file = subdir / "old.txt"
        old_file.write_text("old")
        old_time = time.time() - (2 * 24 * 3600)
        os.utime(old_file, (old_time, old_time))
        deleted = cleanup_old_tmp()
        assert deleted == 1
        assert not subdir.exists(), "空目录应被删除"

    def test_keeps_nonempty_directories(self, tmp_dir_fixture):
        """子目录中有新文件时不删除目录"""
        import time
        from agent.tmp_dir import cleanup_old_tmp
        subdir = tmp_dir_fixture / "has_new_file"
        subdir.mkdir()
        # 旧文件会被删除
        old_file = subdir / "old.txt"
        old_file.write_text("old")
        old_time = time.time() - (2 * 24 * 3600)
        os.utime(old_file, (old_time, old_time))
        # 新文件保留
        new_file = subdir / "new.txt"
        new_file.write_text("new")
        cleanup_old_tmp()
        assert subdir.exists(), "有新文件的目录不应被删除"
        assert new_file.exists()

    def test_keeps_recent_files(self, tmp_dir_fixture):
        """24小时内的文件不被删除"""
        from agent.tmp_dir import cleanup_old_tmp
        recent_file = tmp_dir_fixture / "recent.txt"
        recent_file.write_text("recent")
        deleted = cleanup_old_tmp()
        assert deleted == 0
        assert recent_file.exists()

    def test_returns_zero_for_empty_dir(self, tmp_dir_fixture):
        """空目录返回0"""
        from agent.tmp_dir import cleanup_old_tmp
        deleted = cleanup_old_tmp()
        assert deleted == 0

    def test_keeps_files_under_24_hours(self, tmp_dir_fixture):
        """23小时前的文件不被删除（超过24小时才删除，用 mtime < cutoff 严格小于判断）"""
        import time
        from agent.tmp_dir import cleanup_old_tmp
        # 23小时前的文件 — 不应删除
        recent_file = tmp_dir_fixture / "23h.txt"
        recent_file.write_text("recent")
        recent_time = time.time() - (23 * 3600)
        os.utime(recent_file, (recent_time, recent_time))
        deleted = cleanup_old_tmp()
        assert deleted == 0
        assert recent_file.exists()
