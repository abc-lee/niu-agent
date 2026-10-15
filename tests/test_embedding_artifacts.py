"""EmbeddingGemma 2 产物解析与缺失检测（S1，方案 §4'）。无网络 / 无模型加载 / 无 LLM。

隔离：user-config 重定向 tmp（llama_cpp 段读取不碰真实 ~/.niu，先例
test_context_window_live_read.py）；布局缓存与一次性告警标记每用例重置。
"""

import json

import pytest
from loguru import logger

from niu_api.internal import embedding_artifacts as ea


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """user-config 指向 tmp（空 {}）；清空布局缓存与告警标记，防跨用例状态泄漏。"""
    from niu_api import config as cfg_mod

    p = tmp_path / "user-config.json"
    p.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", str(p))
    ea._LAYOUT_CACHE = None
    ea._LAYOUT_CACHE_KEY = None
    ea._WARNED_REASONS.clear()


class TestPlatform:
    def test_current_platform_is_mapped(self):
        # 方案 §4'.4（R4 P2-A）：口径＝"平台键 ∈ 布局数据的平台清单"
        assert ea.current_platform() in ea.supported_platforms()

    def test_unknown_platform_raises_not_silently(self, monkeypatch):
        monkeypatch.setattr(ea.sys, "platform", "sunos5")
        with pytest.raises(ea.UnsupportedPlatformError):
            ea.current_platform()

    def test_arch_distinguishes_arm64(self, monkeypatch):
        monkeypatch.setattr(ea.sys, "platform", "darwin")
        monkeypatch.setattr(ea.platform, "machine", lambda: "arm64")
        assert ea.current_platform() == "darwin-arm64"

    def test_exec_bit_redefined_by_os_not_asset_table(self, monkeypatch):
        # 方案 §4'.3 死链裁决③：llama_needs_exec_bit 重定义为 sys.platform != "win32"
        # （不再查已删除的资产表；llama_server.py:326 的 chmod 依赖此判定）
        monkeypatch.setattr(ea.sys, "platform", "win32")
        assert ea.llama_needs_exec_bit() is False
        monkeypatch.setattr(ea.sys, "platform", "darwin")
        assert ea.llama_needs_exec_bit() is True


class TestPaths:
    def test_paths_live_under_models_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("NIU_MODELS_PATH", str(tmp_path))
        # 方案 §4'.4：GGUF_FILENAME / LLAMA_CPP_DIRNAME 常量删除 ⇒ 改读布局数据
        assert ea.gguf_path() == tmp_path / ea.gguf_filename()
        assert ea.llama_server_path().parent == tmp_path / ea.layout_dirname() / ea.current_platform()

    def test_llama_server_exe_on_windows(self, monkeypatch, tmp_path):
        monkeypatch.setenv("NIU_MODELS_PATH", str(tmp_path))
        monkeypatch.setattr(ea.sys, "platform", "win32")
        # R2-③：必须同批 patch machine —— 否则 Apple Silicon 主机上派生出
        # win32-arm64（布局未声明时仅告警不抛，死链裁决①），仍保持显式 pin 到
        # 已声明的 win32-x64，用例语义不变。
        monkeypatch.setattr(ea.platform, "machine", lambda: "x86_64")
        assert ea.llama_server_path().name == "llama-server.exe"

    def test_arm64_derives_its_own_layout_path(self, monkeypatch):
        # 方案 §4'.4（R3 P2-2）：llama_asset_name() 删除 ⇒ 用例重写为"布局路径可派生"
        # （平台键自派生、二进制名取布局数据对该平台的声明，不串到别架构）
        monkeypatch.setattr(ea.sys, "platform", "darwin")
        monkeypatch.setattr(ea.platform, "machine", lambda: "arm64")
        assert ea.current_platform() == "darwin-arm64"
        assert ea.platform_dir().name == "darwin-arm64"
        assert ea.llama_server_name() == "llama-server"


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
        # 方案 §4'.4（R4 P2-E）：文案不再提脚本名（与"零下载"互斥），改指手册节名
        assert "package_all_dependencies" not in msg
        assert "系统管理手册" in msg
        assert "可选组件安装" in msg

    def test_missing_order_weight_first(self, monkeypatch, tmp_path):
        monkeypatch.setenv("NIU_MODELS_PATH", str(tmp_path))
        missing = ea.missing_artifacts()
        assert missing[0] == ea.gguf_path()
        assert missing[1] == ea.llama_server_path()


class TestVerifyGguf:
    def test_mismatch_usable_with_warning(self, tmp_path):
        # 方案 v1.7：体积不匹配（布局期望 309855456 vs 实得 10）⇒ 不再判残损，
        # 仍可用 + WARNING（可能换量化/文件不完整），不抛（旧"必被拒"断言作废）
        p = tmp_path / "g.gguf"
        p.write_bytes(b"x" * 10)
        messages: list[str] = []
        sink_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
        try:
            assert ea.verify_gguf(p)
        finally:
            logger.remove(sink_id)
        assert any("量化" in m or "不完整" in m for m in messages), f"体积不符必须 WARNING：{messages}"

    def test_accepts_exact_size(self, tmp_path, monkeypatch):
        # 方案 §4'.4：GGUF_SIZE 常量删除 ⇒ monkeypatch 布局数据（gguf.size 改为 4）
        layout_dir = tmp_path / "layout"
        layout_dir.mkdir()
        lp = layout_dir / "llama-layout.json"
        layout = json.loads(ea._bundle_layout_path().read_text(encoding="utf-8"))
        layout["gguf"]["size"] = 4
        lp.write_text(json.dumps(layout), encoding="utf-8")
        monkeypatch.setattr(ea, "_bundle_layout_path", lambda: lp)
        monkeypatch.setattr(ea, "_user_layout_path", lambda: layout_dir / "absent.json")
        p = tmp_path / "g.gguf"
        p.write_bytes(b"abcd")
        assert ea.verify_gguf(p)

    def test_missing_file_is_false(self, tmp_path):
        assert not ea.verify_gguf(tmp_path / "nope.gguf")
