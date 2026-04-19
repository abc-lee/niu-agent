"""Tests for agent/tmp_dir.py — temporary file directory management"""
import os
import shutil
import tempfile
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
        from agent.tmp_dir import save_to_tmp, get_tmp_dir
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
        from agent.tmp_dir import save_to_tmp, is_tmp_file
        path = save_to_tmp("test.jpg", b"data")
        assert is_tmp_file(path) is True

    def test_returns_false_for_non_tmp_file(self, tmp_dir_fixture):
        from agent.tmp_dir import is_tmp_file
        assert is_tmp_file("C:/Users/test/photo.jpg") is False
        assert is_tmp_file("/home/user/photo.jpg") is False


class TestCleanupTmpFiles:
    def test_deletes_specified_tmp_files(self, tmp_dir_fixture):
        from agent.tmp_dir import save_to_tmp, cleanup_tmp_files
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
        from agent.tmp_dir import get_tmp_dir, cleanup_tmp_files
        fake_path = str(get_tmp_dir() / "nonexistent.jpg")
        deleted = cleanup_tmp_files([fake_path])
        assert deleted == 0

    def test_mixed_files(self, tmp_dir_fixture):
        from agent.tmp_dir import save_to_tmp, cleanup_tmp_files
        path = save_to_tmp("a.jpg", b"data")
        deleted = cleanup_tmp_files([path, "C:/non-tmp.jpg", "/tmp/fake.jpg"])
        assert deleted == 1
        assert not os.path.exists(path)


class TestCleanupAllTmp:
    def test_removes_all_files_in_tmp_dir(self, tmp_dir_fixture):
        from agent.tmp_dir import save_to_tmp, cleanup_all_tmp, get_tmp_dir
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
