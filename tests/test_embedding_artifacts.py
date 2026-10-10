"""EmbeddingGemma 2 产物解析与缺失检测（S1）。无网络 / 无模型加载 / 无 LLM。"""

import pytest

from niu_api.internal import embedding_artifacts as ea


class TestPlatform:
    def test_current_platform_is_mapped(self):
        assert ea.current_platform() in ea._PLATFORM_ASSETS

    def test_unknown_platform_raises_not_silently(self, monkeypatch):
        monkeypatch.setattr(ea.sys, "platform", "sunos5")
        with pytest.raises(ea.UnsupportedPlatformError):
            ea.current_platform()

    def test_arch_distinguishes_arm64(self, monkeypatch):
        monkeypatch.setattr(ea.sys, "platform", "darwin")
        monkeypatch.setattr(ea.platform, "machine", lambda: "arm64")
        assert ea.current_platform() == "darwin-arm64"


class TestPaths:
    def test_paths_live_under_models_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("NIU_MODELS_PATH", str(tmp_path))
        assert ea.gguf_path() == tmp_path / ea.GGUF_FILENAME
        assert ea.llama_server_path().parent == tmp_path / ea.LLAMA_CPP_DIRNAME / ea.current_platform()

    def test_llama_server_exe_on_windows(self, monkeypatch, tmp_path):
        monkeypatch.setenv("NIU_MODELS_PATH", str(tmp_path))
        monkeypatch.setattr(ea.sys, "platform", "win32")
        # R2-③：必须同批 patch machine —— 否则 Apple Silicon 主机上
        # current_platform() 得 win32-arm64（未适配）而先抛 UnsupportedPlatformError，
        # 该用例在 arm64 开发机必红（正是 import 期常量那类单平台测试形态）。
        monkeypatch.setattr(ea.platform, "machine", lambda: "x86_64")
        assert ea.llama_server_path().name == "llama-server.exe"

    def test_arm64_does_not_get_x64_asset(self, monkeypatch):
        monkeypatch.setattr(ea.sys, "platform", "darwin")
        monkeypatch.setattr(ea.platform, "machine", lambda: "arm64")
        assert "macos-arm64" in ea.llama_asset_name()


class TestMissingAndDescribe:
    def test_both_present_is_ready(self, monkeypatch, tmp_path):
        monkeypatch.setenv("NIU_MODELS_PATH", str(tmp_path))
        ea.gguf_path().write_bytes(b"x")
        ea.llama_server_path().parent.mkdir(parents=True, exist_ok=True)
        ea.llama_server_path().write_bytes(b"x")
        assert ea.artifacts_ready()
        assert ea.describe_missing() == ""

    def test_missing_names_concrete_files(self, monkeypatch, tmp_path):
        monkeypatch.setenv("NIU_MODELS_PATH", str(tmp_path))
        msg = ea.describe_missing()
        assert str(ea.gguf_path()) in msg
        assert str(ea.llama_server_path()) in msg
        assert "package_all_dependencies" in msg

    def test_missing_order_weight_first(self, monkeypatch, tmp_path):
        monkeypatch.setenv("NIU_MODELS_PATH", str(tmp_path))
        missing = ea.missing_artifacts()
        assert missing[0] == ea.gguf_path()
        assert missing[1] == ea.llama_server_path()


class TestVerifyGguf:
    def test_rejects_wrong_size(self, tmp_path):
        p = tmp_path / "g.gguf"
        p.write_bytes(b"x" * 10)
        assert not ea.verify_gguf(p)

    def test_accepts_exact_size(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ea, "GGUF_SIZE", 4)
        p = tmp_path / "g.gguf"
        p.write_bytes(b"abcd")
        assert ea.verify_gguf(p)

    def test_missing_file_is_false(self, tmp_path):
        assert not ea.verify_gguf(tmp_path / "nope.gguf")
