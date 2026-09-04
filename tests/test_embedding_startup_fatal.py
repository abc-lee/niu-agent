"""
Embedding 致命错误处理（契约 A，自 reranker 工程保留子集——2026-09-04 用户拍板：
启动器检测向量模型加载失败后触发停止启动的逻辑应保留，rerank 功能本体已回退）。

契约 A：_handle_embedding_preload_failure → ~/.niu/.startup_error 文件 + SystemExit
        （Rust 启动器以「进程早退 + 文件存在且非空」判定 Fatal 红字展示）；
        _clear_startup_error_file 双清理（main() 首行 + lifespan 开头）。

无真实模型加载 / 网络 / LLM / 图谱写入。
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


class TestHandleEmbeddingPreloadFailure:
    def test_writes_startup_error_and_exits(self):
        import niu_api.__main__ as main_mod

        with tempfile.TemporaryDirectory() as tmp:
            with patch("pathlib.Path.home", return_value=Path(tmp)):
                with pytest.raises(SystemExit):
                    main_mod._handle_embedding_preload_failure(
                        RuntimeError("model file missing")
                    )
            err_file = Path(tmp) / ".niu" / ".startup_error"
            assert err_file.exists()
            content = err_file.read_text(encoding="utf-8")
            assert "向量模型（embedding）加载失败" in content
            assert "model file missing" in content

    def test_creates_niu_dir_when_missing(self):
        import niu_api.__main__ as main_mod

        with tempfile.TemporaryDirectory() as tmp:
            # .niu 目录不存在——函数必须自行创建（首次启动即失败的边界）
            assert not (Path(tmp) / ".niu").exists()
            with patch("pathlib.Path.home", return_value=Path(tmp)):
                with pytest.raises(SystemExit):
                    main_mod._handle_embedding_preload_failure(ValueError("boom"))
            assert (Path(tmp) / ".niu" / ".startup_error").exists()


class TestClearStartupErrorFile:
    def test_removes_stale_file(self):
        import niu_api.__main__ as main_mod

        with tempfile.TemporaryDirectory() as tmp:
            err_file = Path(tmp) / ".niu" / ".startup_error"
            err_file.parent.mkdir(parents=True)
            err_file.write_text("stale crash from last run", encoding="utf-8")
            with patch("pathlib.Path.home", return_value=Path(tmp)):
                main_mod._clear_startup_error_file()
            assert not err_file.exists()

    def test_no_error_when_file_absent(self):
        import niu_api.__main__ as main_mod

        with tempfile.TemporaryDirectory() as tmp:
            with patch("pathlib.Path.home", return_value=Path(tmp)):
                main_mod._clear_startup_error_file()  # missing_ok：不 raise
