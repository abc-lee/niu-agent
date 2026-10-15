"""布局数据（config/llama-layout.json）读取 / 用户层合并 / 故障降级 —— 方案 §9.1 第 1 行五用例。

全部离线、不碰真实 ~/.niu：user-config 重定向 tmp，布局两层一律写 tmp 后经
monkeypatch 指向（先例 test_context_window_live_read.py 的 CONFIG_PATH 注入）。
标准布局内容直接读 bundle 随附文件，保证"零缺省漂移"的基线与分发文件同源。
"""

import json

import pytest

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


def _standard_layout() -> dict:
    """bundle 随附布局数据文件的内容（必须在 monkeypatch 布局路径之前取）。"""
    return json.loads(ea._bundle_layout_path().read_text(encoding="utf-8"))


def _point_layout(monkeypatch, tmp_path, bundle_payload=None, user_payload=None):
    """把布局两层指向 tmp 文件（payload=None ⇒ 该层缺失）。"""
    bdir = tmp_path / "bundle-layout"
    udir = tmp_path / "user-layout"
    bdir.mkdir(exist_ok=True)
    udir.mkdir(exist_ok=True)
    bp = bdir / "llama-layout.json" if bundle_payload is not None else bdir / "absent.json"
    if bundle_payload is not None:
        bp.write_text(json.dumps(bundle_payload, ensure_ascii=False), encoding="utf-8")
    up = udir / "llama-layout-user.json" if user_payload is not None else udir / "absent.json"
    if user_payload is not None:
        up.write_text(json.dumps(user_payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(ea, "_bundle_layout_path", lambda: bp)
    monkeypatch.setattr(ea, "_user_layout_path", lambda: up)


class TestLayoutMissingDegrades:
    def test_layout_missing_no_fatal_and_list_readable(self, monkeypatch, tmp_path):
        """① 布局数据缺失 ⇒ 不 Fatal：文案降级（缺产物 + 去手册），缺文件清单仍可读。"""
        monkeypatch.setenv("NIU_MODELS_PATH", str(tmp_path))
        _point_layout(monkeypatch, tmp_path, None, None)
        msg = ea.describe_missing()  # 不得抛异常
        assert msg
        # 清单仍可读：按回退布局名派生出具体文件路径
        assert str(ea.gguf_path()) in msg
        assert str(ea.llama_server_path()) in msg
        # 降级文案指手册，不带平台名/构建号/脚本名/启动方式
        assert "系统管理手册" in msg and "可选组件安装" in msg
        assert not ea.artifacts_ready()

    def test_layout_missing_but_artifacts_present_is_not_fatal(self, monkeypatch, tmp_path):
        """产物齐 + 布局缺失 ⇒ 仅 WARNING：describe_missing() 空串，artifacts_ready() True。"""
        monkeypatch.setenv("NIU_MODELS_PATH", str(tmp_path))
        _point_layout(monkeypatch, tmp_path, None, None)
        ea.gguf_path().write_bytes(b"x")
        binary = ea.llama_server_path()
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(b"x")
        assert ea.artifacts_ready()
        assert ea.describe_missing() == ""


class TestUserLayerMerge:
    def test_user_layer_overrides_filename_and_platform_entry(self, monkeypatch, tmp_path):
        """② 用户层覆盖 merge（用户赢）：改文件名 / 平台二进制名可生效。"""
        monkeypatch.setenv("NIU_MODELS_PATH", str(tmp_path))
        monkeypatch.setattr(ea.sys, "platform", "darwin")
        monkeypatch.setattr(ea.platform, "machine", lambda: "x86_64")
        bundle = _standard_layout()
        user = {
            "gguf": {"filename": "custom-weights.gguf"},
            "platforms": {"darwin-x64": {"binary": "llama-server-custom"}},
        }
        _point_layout(monkeypatch, tmp_path, bundle, user)
        # 用户层赢：GGUF 文件名 / 该平台的二进制名换成用户值
        assert ea.gguf_path().name == "custom-weights.gguf"
        assert ea.llama_server_path().name == "llama-server-custom"
        # 用户没碰的键保持 bundle 值（deep merge 而非替换）
        assert ea.layout_dirname() == bundle["dirname"]
        merged = ea._layout_raw()
        assert merged["mmproj"]["filename"] == bundle["mmproj"]["filename"]
        assert merged["platforms"]["darwin-x64"]["binary"] == "llama-server-custom"

    def test_user_layer_absent_is_silent_default(self, monkeypatch, tmp_path):
        """用户层缺失＝正常缺省形态（静默）：合并结果＝bundle 权威层原样。"""
        monkeypatch.setenv("NIU_MODELS_PATH", str(tmp_path))
        bundle = _standard_layout()
        _point_layout(monkeypatch, tmp_path, bundle, None)
        assert ea._layout_raw() == bundle


class TestZeroDefaultDrift:
    def test_paths_byte_identical_to_today(self, monkeypatch, tmp_path):
        """③ 零缺省漂移：不配置任何新键 ⇒ 二进制 / GGUF 路径与今天写死时代逐字节一致。"""
        monkeypatch.setenv("NIU_MODELS_PATH", str(tmp_path))
        _point_layout(monkeypatch, tmp_path, _standard_layout(), None)
        binary = "llama-server.exe" if ea.sys.platform == "win32" else "llama-server"
        assert ea.gguf_path() == tmp_path / "embeddinggemma-2-Q8_0.gguf"
        assert ea.llama_server_path() == tmp_path / "llama.cpp" / ea.current_platform() / binary

    def test_paths_follow_layout_data_not_code(self, monkeypatch, tmp_path):
        """布局路径/文件名跟随数据（防 V1 变异把字面量写回代码：那样此用例必红）。"""
        monkeypatch.setenv("NIU_MODELS_PATH", str(tmp_path))
        tampered = _standard_layout()
        tampered["dirname"] = "third-party"
        tampered["gguf"]["filename"] = "tampered.gguf"
        _point_layout(monkeypatch, tmp_path, tampered, None)
        assert ea.gguf_path() == tmp_path / "tampered.gguf"
        assert ea.llama_server_path().parent == tmp_path / "third-party" / ea.current_platform()


class TestBinaryPathOverride:
    def test_binary_path_wins_and_binary_check_skipped(self, monkeypatch, tmp_path):
        """④ llama_cpp.binary_path 非空 ⇒ llama_server_path() 返回它，
        missing_artifacts() 跳过布局二进制检查（只检 GGUF）。"""
        monkeypatch.setenv("NIU_MODELS_PATH", str(tmp_path))
        custom = tmp_path / "my-llama-server"  # 故意不创建文件
        (tmp_path / "user-config.json").write_text(
            json.dumps({"llama_cpp": {"binary_path": str(custom)}}), encoding="utf-8"
        )
        _point_layout(monkeypatch, tmp_path, _standard_layout(), None)
        assert ea.llama_server_path() == custom
        # GGUF 齐 + 布局二进制不在 + 用户二进制未放 ⇒ 二进制检查被跳过，判齐备
        ea.gguf_path().write_bytes(b"x")
        assert ea.artifacts_ready()
        # GGUF 缺 ⇒ 清单只有 GGUF，绝不含二进制项
        ea.gguf_path().unlink()
        assert ea.missing_artifacts() == [ea.gguf_path()]


class TestPlatformKeyInLayout:
    def test_platform_key_in_layout_platforms(self, monkeypatch, tmp_path):
        """⑤ 平台键 ∈ 布局数据的平台清单：current_platform() 纯派生不死，
        布局缺失/未声明该平台时跳过成员校验（仅 WARNING，不抛）。"""
        monkeypatch.setenv("NIU_MODELS_PATH", str(tmp_path))
        bundle = _standard_layout()
        assert ea.current_platform() in bundle["platforms"]  # 分发布局声明本机平台
        _point_layout(monkeypatch, tmp_path, bundle, None)
        key = ea.current_platform()
        assert key in ea.supported_platforms()
        # 布局未声明该平台：仍按派生键继续，不抛 UnsupportedPlatformError
        undeclared = {k: v for k, v in bundle["platforms"].items() if k != key}
        _point_layout(monkeypatch, tmp_path, {**bundle, "platforms": undeclared}, None)
        assert ea.current_platform() == key
        assert ea.llama_server_path().name  # 回退名仍可派生，不崩
