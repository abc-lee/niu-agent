"""T1/T2b1：mmproj 的家目录产物定义、就绪检查与零下载文案（D-11 / I-11 / 方案 §4'.3）。

核心鉴别器：`get_models_dir()` 缺省回落**项目根 `models/`**（运行时只读的
.app bundle），mmproj **不得**走它 —— 必须自己的 `Path.home()` 解析，且**不读**
`NIU_MODELS_PATH`。全部用临时家目录，不触碰真实 ~/.niu/models（本机 mmproj 已就位，
真读会破坏"缺产物"前提）。

T2b1（方案 v1.7 口径）：程序**零下载**——下载 URL 函数 / sha256 常量 / 源清单从本模块
删除，缺失文案只含"缺哪个文件 + 兼容性要求（§4'.2 口径简述）+ 见《系统管理手册》
『可选组件安装』"，不得出现 URL / sha256 / 构建号 / 平台名 / 脚本名；换 mmproj
文件（任意量化/任意位置）＝改配置里那一行 `llama_cpp.command` 的 `--mmproj` 指向，
本模块只返家目录布局缺省。
"""
import json
import sys
from pathlib import Path

import pytest

from niu_api import config as cfg_mod
from niu_api.internal import embedding_artifacts as ea
from niu_api.internal import media_artifacts as ma

REPO_ROOT = Path(__file__).resolve().parent.parent


def _platform_seed_suffix() -> str:
    """当前 OS 对应的种子文件名后缀（与启动器 platform_seed_suffix 同口径）。"""
    return "win32" if sys.platform == "win32" else "darwin"


def _standard_layout() -> dict:
    """包内随附的平台种子内容（与启动器首启拷入 ~/.niu/llama-layout.json 的那份同源）。"""
    return json.loads(
        (REPO_ROOT / "memory" / f"llama-layout.{_platform_seed_suffix()}.json")
        .read_text(encoding="utf-8")
    )


def _point_layout(monkeypatch, tmp_path, payload=None):
    """把家目录布局文件指向 tmp 文件（payload=None ⇒ 该文件缺失）。"""
    d = tmp_path / "home-layout"
    d.mkdir(exist_ok=True)
    p = d / "llama-layout.json"
    if payload is not None:
        p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(ea, "_layout_path", lambda: p)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """user-config / 布局 / HOME 全指向 tmp（布局＝包内平台种子的 tmp 拷贝，先例
    test_llama_config_read.py）；清空布局缓存与一次性告警标记，防跨用例状态泄漏。"""
    p = tmp_path / "user-config.json"
    p.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", str(p))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    ea._LAYOUT_CACHE = None
    ea._LAYOUT_CACHE_KEY = None
    ea._WARNED_REASONS.clear()
    _point_layout(monkeypatch, tmp_path, _standard_layout())

@pytest.fixture
def fake_home(monkeypatch, tmp_path):
    """Path.home 钉到临时目录：media_models_dir 必须跟随家目录。"""
    home = tmp_path / "home"
    (home / ".niu" / "models").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    return home


def _standard_mmproj_filename() -> str:
    """包内平台种子里的 mmproj 文件名（缺省形态的真相源）。"""
    return _standard_layout()["mmproj"]["filename"]


class TestHomeDirResolution:
    def test_models_dir_under_home(self, fake_home):
        assert ma.media_models_dir() == fake_home / ".niu" / "models"

    def test_mmproj_path_under_home(self, fake_home):
        # 缺省文件名跟随布局数据（包内种子），落点仍是家目录 models/
        p = ma.mmproj_path()
        assert p == fake_home / ".niu" / "models" / _standard_mmproj_filename()
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


class TestNoDownloadSurface:
    def test_download_apis_removed(self):
        """程序零下载（方案 §4'.3 / R3 P2-1）：下载 URL 函数 / sha256 常量 / 源清单不得存在。"""
        assert not hasattr(ma, "mmproj_download_urls")
        assert not hasattr(ma, "MMPROJ_SHA256")
        assert not hasattr(ma, "_MMPROJ_SOURCES")

    def test_module_source_has_no_url_or_hash(self):
        """口径门禁：模块源码里不得出现任何 URL / sha256 字面量（含注释）。"""
        src = Path(ma.__file__).read_text(encoding="utf-8")
        assert "http://" not in src and "https://" not in src
        assert "sha256" not in src


class TestReadyAndDescribe:
    def test_missing_not_ready(self, fake_home):
        assert not ma.media_artifacts_ready()

    def test_present_is_ready(self, fake_home):
        ma.mmproj_path().write_bytes(b"x")
        assert ma.media_artifacts_ready()

    def test_describe_names_concrete_path_when_missing(self, fake_home):
        msg = ma.describe_missing_media()
        assert str(ma.mmproj_path()) in msg
        # 零下载口径：不得出现 URL / sha256（旧"校验依据"断言作废）；改指手册节名
        assert "http" not in msg and "sha256" not in msg
        assert "系统管理手册" in msg and "可选组件安装" in msg

    def test_describe_cites_compatibility_not_sources(self, fake_home):
        """旧 C11 源断言的改写：文案只含兼容性要求（§4'.2 口径简述），不含镜像源。"""
        msg = ma.describe_missing_media()
        assert "hf-mirror" not in msg
        assert "--mmproj" in msg  # 兼容性要求：llama-server 须识别 --mmproj

    def test_describe_empty_when_present(self, fake_home):
        ma.mmproj_path().write_bytes(b"x")
        assert ma.describe_missing_media() == ""
