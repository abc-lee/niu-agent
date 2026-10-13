"""T1：mmproj 的家目录产物定义与就绪检查（D-11 / I-11）。

核心鉴别器：`get_models_dir()` 缺省回落**项目根 `models/`**（运行时只读的
.app bundle），mmproj **不得**走它 —— 必须自己的 `Path.home()` 解析，且**不读**
`NIU_MODELS_PATH`。全部用临时家目录，不触碰真实 ~/.niu/models（本机 mmproj 已就位，
真读会破坏"缺产物"前提）。
"""
from pathlib import Path

import pytest

from niu_api.internal import media_artifacts as ma


@pytest.fixture
def fake_home(monkeypatch, tmp_path):
    """Path.home 钉到临时目录：media_models_dir 必须跟随家目录。"""
    home = tmp_path / "home"
    (home / ".niu" / "models").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    return home
class TestHomeDirResolution:
    def test_models_dir_under_home(self, fake_home):
        assert ma.media_models_dir() == fake_home / ".niu" / "models"

    def test_mmproj_path_under_home(self, fake_home):
        p = ma.mmproj_path()
        assert p == fake_home / ".niu" / "models" / ma.MMPROJ_FILENAME
        assert p.suffix == ".gguf"

    def test_ignores_niu_models_path_env(self, fake_home, monkeypatch, tmp_path):
        """I-11 鉴别器：NIU_MODELS_PATH 指向别处时 mmproj 仍在 ~ 下。"""
        monkeypatch.setenv("NIU_MODELS_PATH", str(tmp_path / "other"))
        assert str(ma.mmproj_path()).startswith(str(fake_home))

    def test_not_project_models_dir(self, fake_home, monkeypatch):
        """不得经 get_models_dir（缺省回落项目根 models/，生产 bundle 只读）。"""
        import niu_api.internal.embedding as emb

        monkeypatch.delenv("NIU_MODELS_PATH", raising=False)
        assert emb.get_models_dir() != ma.media_models_dir()


class TestDownloadUrls:
    def test_nonempty_https_list(self):
        urls = ma.mmproj_download_urls()
        assert urls
        assert all(isinstance(u, str) and u.startswith("https://") for u in urls)
        assert any(ma.MMPROJ_FILENAME in u for u in urls)

    def test_c11_source_present(self):
        assert any(
            "hf-mirror.com/ggml-org/embeddinggemma-2-GGUF" in u
            for u in ma.mmproj_download_urls()
        )


class TestReadyAndDescribe:
    def test_missing_not_ready(self, fake_home):
        assert not ma.media_artifacts_ready()

    def test_present_is_ready(self, fake_home):
        ma.mmproj_path().write_bytes(b"x")
        assert ma.media_artifacts_ready()

    def test_describe_names_concrete_path_when_missing(self, fake_home):
        msg = ma.describe_missing_media()
        assert str(ma.mmproj_path()) in msg
        # 可行动文案必须带校验依据，防止装错文件被当成装好了
        assert "sha256" in msg

    def test_describe_empty_when_present(self, fake_home):
        ma.mmproj_path().write_bytes(b"x")
        assert ma.describe_missing_media() == ""
