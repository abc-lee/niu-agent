"""布局文件（~/.niu/llama-layout.json，启动器首启自包内种子拷入的家目录那份）读取 /
故障降级 —— 方案 §9.1 第 1 行五用例（seed 机制版）。

全部离线、不碰真实 ~/.niu：user-config 重定向 tmp，布局文件写 tmp 后经
monkeypatch `_layout_path` 指向（先例 test_context_window_live_read.py 的 CONFIG_PATH
注入）。标准布局内容直接读包内平台种子（memory/llama-layout.<os>.json），保证
"零缺省漂移"的基线与启动器首启拷入的分发文件同源。
"""

import json
import sys
from pathlib import Path

import pytest

from niu_api.internal import embedding_artifacts as ea

REPO_ROOT = Path(__file__).resolve().parent.parent


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


def _platform_seed_suffix() -> str:
    """当前 OS 对应的种子文件名后缀（与启动器 platform_seed_suffix 同口径：
    macOS → darwin，Windows → win32）。"""
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


class TestLayoutMissingDegrades:
    def test_layout_missing_no_fatal_and_list_readable(self, monkeypatch, tmp_path):
        """① 家目录布局文件缺失 ⇒ 不 Fatal：文案降级（缺产物 + 去手册），
        缺文件清单仍可读（按内置回退名派生）。"""
        monkeypatch.setenv("NIU_MODELS_PATH", str(tmp_path))
        _point_layout(monkeypatch, tmp_path, None)
        msg = ea.describe_missing()  # 不得抛异常
        assert msg
        # 清单仍可读：按回退布局名派生出具体文件路径
        assert str(ea.gguf_path()) in msg
        assert str(ea.llama_server_path()) in msg
        # 降级文案指手册，不带平台名/构建号/脚本名/启动方式
        assert "系统管理手册" in msg and "可选组件安装" in msg
        assert not ea.artifacts_ready()

    def test_layout_missing_but_artifacts_present_is_not_fatal(self, monkeypatch, tmp_path):
        """产物齐 + 布局文件缺失 ⇒ 仅 WARNING：describe_missing() 空串，artifacts_ready() True。"""
        monkeypatch.setenv("NIU_MODELS_PATH", str(tmp_path))
        _point_layout(monkeypatch, tmp_path, None)
        ea.gguf_path().write_bytes(b"x")
        binary = ea.llama_server_path()
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(b"x")
        assert ea.artifacts_ready()
        assert ea.describe_missing() == ""

    def test_layout_file_unparseable_degrades_to_fallback(self, monkeypatch, tmp_path):
        """布局文件存在但 JSON 坏掉 ⇒ 按缺失降级（WARNING），不抛、不 Fatal。"""
        monkeypatch.setenv("NIU_MODELS_PATH", str(tmp_path))
        d = tmp_path / "home-layout"
        d.mkdir(exist_ok=True)
        p = d / "llama-layout.json"
        p.write_text("{not valid json", encoding="utf-8")
        monkeypatch.setattr(ea, "_layout_path", lambda: p)
        assert ea._layout_raw() is None
        # 回退名仍派生出可读路径
        assert ea.layout_dirname() == "llama.cpp"
        assert ea.gguf_filename() == "embeddinggemma-2-Q8_0.gguf"
        assert not ea.artifacts_ready()


class TestHomeCopyAuthoritative:
    def test_home_copy_edits_take_effect(self, monkeypatch, tmp_path):
        """②（seed 机制改述）家目录那份是唯一权威：用户直接改它的文件名 / 平台
        二进制名即生效，无需任何用户覆盖层。"""
        monkeypatch.setenv("NIU_MODELS_PATH", str(tmp_path))
        monkeypatch.setattr(ea.sys, "platform", "darwin")
        monkeypatch.setattr(ea.platform, "machine", lambda: "x86_64")
        layout = _standard_layout()
        layout["gguf"]["filename"] = "custom-weights.gguf"
        layout["platforms"]["darwin-x64"] = {"binary": "llama-server-custom"}
        _point_layout(monkeypatch, tmp_path, layout)
        assert ea.gguf_path().name == "custom-weights.gguf"
        assert ea.llama_server_path().name == "llama-server-custom"
        # 没被用户动的键保持种子值
        assert ea.layout_dirname() == layout["dirname"]
        assert ea._layout_raw()["mmproj"]["filename"] == layout["mmproj"]["filename"]

    def test_home_copy_read_as_is(self, monkeypatch, tmp_path):
        """家目录文件内容原样生效（无第二层合并语义）。"""
        monkeypatch.setenv("NIU_MODELS_PATH", str(tmp_path))
        layout = _standard_layout()
        _point_layout(monkeypatch, tmp_path, layout)
        assert ea._layout_raw() == layout


class TestZeroDefaultDrift:
    def test_paths_byte_identical_to_today(self, monkeypatch, tmp_path):
        """③ 零缺省漂移：家目录那份＝出厂种子（启动器首启拷入）⇒ 二进制 / GGUF
        路径与今天逐字节一致。"""
        monkeypatch.setenv("NIU_MODELS_PATH", str(tmp_path))
        _point_layout(monkeypatch, tmp_path, _standard_layout())
        binary = "llama-server.exe" if ea.sys.platform == "win32" else "llama-server"
        assert ea.gguf_path() == tmp_path / "embeddinggemma-2-Q8_0.gguf"
        assert ea.llama_server_path() == tmp_path / "llama.cpp" / ea.current_platform() / binary

    def test_paths_follow_layout_data_not_code(self, monkeypatch, tmp_path):
        """布局路径/文件名跟随数据（防 V1 变异把字面量写回代码：那样此用例必红）。"""
        monkeypatch.setenv("NIU_MODELS_PATH", str(tmp_path))
        tampered = _standard_layout()
        tampered["dirname"] = "third-party"
        tampered["gguf"]["filename"] = "tampered.gguf"
        _point_layout(monkeypatch, tmp_path, tampered)
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
        _point_layout(monkeypatch, tmp_path, _standard_layout())
        assert ea.llama_server_path() == custom
        # GGUF 齐 + 布局二进制不在 + 用户二进制未放 ⇒ 二进制检查被跳过，判齐备
        ea.gguf_path().write_bytes(b"x")
        assert ea.artifacts_ready()
        # GGUF 缺 ⇒ 清单只有 GGUF，绝不含二进制项
        ea.gguf_path().unlink()
        assert ea.missing_artifacts() == [ea.gguf_path()]


class TestPlatformKeyInLayout:
    def test_platform_key_in_layout_platforms(self, monkeypatch, tmp_path):
        """⑤ 平台键 ∈ 布局文件的平台清单：current_platform() 纯派生不死，
        文件缺失/未声明该平台时跳过成员校验（仅 WARNING，不抛）。"""
        monkeypatch.setenv("NIU_MODELS_PATH", str(tmp_path))
        layout = _standard_layout()
        assert ea.current_platform() in layout["platforms"]  # 平台种子声明本机平台
        _point_layout(monkeypatch, tmp_path, layout)
        key = ea.current_platform()
        assert key in ea.supported_platforms()
        # 布局未声明该平台：仍按派生键继续，不抛 UnsupportedPlatformError
        undeclared = {k: v for k, v in layout["platforms"].items() if k != key}
        _point_layout(monkeypatch, tmp_path, {**layout, "platforms": undeclared})
        assert ea.current_platform() == key
        assert ea.llama_server_path().name  # 回退名仍可派生，不崩
