"""T2b1：模型文件/量化版本可配（`llama_cpp.gguf_path` / `llama_cpp.mmproj_path`）+
体积校验可选化（方案 v1.7，§5.1 配置键表）。

五用例（规格逐字对应）：
① `llama_cpp.gguf_path` 非空 ⇒ `ea.gguf_path()` 直接返回它（不拼布局路径）；
② `llama_cpp.mmproj_path` 非空 ⇒ `ma.mmproj_path()` 同理；
③ 不配新键 ⇒ 与 T2a 缺省逐字一致（零漂移）；
④ 体积不匹配 ⇒ `verify_gguf` 仍返回可用 + WARNING（不抛、不判残损）；
⑤ 布局数据里 `size` 为 null ⇒ 跳过体积比对（静默，无任何告警）。

全部离线、不碰真实 ~/.niu：user-config 重定向 tmp（CONFIG_PATH 注入，先例
test_llama_layout_config.py）；布局两层一律写 tmp 后经 monkeypatch 指向；
`Path.home` 钉 tmp（mmproj 家目录用例）；布局缓存与一次性告警标记每用例重置。
"""
import json
from pathlib import Path

import pytest
from loguru import logger

from niu_api import config as cfg_mod
from niu_api.internal import embedding_artifacts as ea
from niu_api.internal import media_artifacts as ma


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """user-config 指向 tmp（空 {}）；布局用户层指向 tmp 不存在文件（防读真实
    ~/.niu/config/llama-layout-user.json）；清空布局缓存与告警标记，防跨用例状态泄漏。"""
    p = tmp_path / "user-config.json"
    p.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", str(p))
    monkeypatch.setattr(ea, "_user_layout_path", lambda: tmp_path / "absent-layout.json")
    ea._LAYOUT_CACHE = None
    ea._LAYOUT_CACHE_KEY = None
    ea._WARNED_REASONS.clear()
    home = tmp_path / "home"
    (home / ".niu" / "models").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    return tmp_path


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


def _write_llama_section(payload: dict) -> None:
    """把 `llama_cpp` 段写进 CONFIG_PATH 指向的 tmp 文件（autouse 已重定向）。"""
    Path(cfg_mod.CONFIG_PATH).write_text(
        json.dumps({"llama_cpp": payload}, ensure_ascii=False), encoding="utf-8"
    )


def _capture_loguru():
    """loguru sink 捕获（模块用 loguru 而非 stdlib logging，pytest caplog 捕不到——
    项目既有模式，同 test_gemma_tokenizer.py）。"""
    messages: list[str] = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    return messages, sink_id


class TestModelFilePathOverride:
    def test_gguf_path_override_wins(self, monkeypatch, tmp_path):
        """① `llama_cpp.gguf_path` 非空 ⇒ gguf_path() 直接返回它（不拼布局路径）。"""
        monkeypatch.setenv("NIU_MODELS_PATH", str(tmp_path))
        custom = tmp_path / "custom-weights" / "my-q4.gguf"
        _write_llama_section({"gguf_path": str(custom)})
        _point_layout(monkeypatch, tmp_path, _standard_layout(), None)
        assert ea.gguf_path() == custom
        # 不是布局派生路径：布局路径在 models/ 下，覆盖路径在其外（任意位置）
        assert ea.gguf_path().parent != ea.models_dir()
        # 缺失清单同样按覆盖路径判：布局二进制补齐后，清单只剩覆盖的 GGUF 路径
        binary = ea.llama_server_path()
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(b"x")
        assert ea.missing_artifacts() == [custom]

    def test_mmproj_path_override_wins(self, monkeypatch, tmp_path):
        """② `llama_cpp.mmproj_path` 非空 ⇒ mmproj_path() 直接返回它（不在家目录布局下）。"""
        custom = tmp_path / "somewhere-else" / "alt-mmproj.gguf"
        _write_llama_section({"mmproj_path": str(custom)})
        _point_layout(monkeypatch, tmp_path, _standard_layout(), None)
        assert ma.mmproj_path() == custom
        assert ma.mmproj_path().parent != tmp_path / "home" / ".niu" / "models"

    def test_no_new_keys_zero_default_drift(self, monkeypatch, tmp_path):
        """③ 不配新键 ⇒ 与 T2a 缺省逐字一致：GGUF＝`<models>/<布局 gguf.filename>`，
        mmproj＝`~/.niu/models/<布局 mmproj.filename>`。"""
        monkeypatch.setenv("NIU_MODELS_PATH", str(tmp_path))
        layout = _standard_layout()
        _point_layout(monkeypatch, tmp_path, layout, None)
        assert ea.gguf_path() == tmp_path / layout["gguf"]["filename"]
        assert ma.mmproj_path() == (
            tmp_path / "home" / ".niu" / "models" / layout["mmproj"]["filename"]
        )


class TestVerifyGgufOptionalSize:
    def test_size_mismatch_usable_with_warning(self, monkeypatch, tmp_path):
        """④ 体积不匹配（布局期望 vs 实得）⇒ 仍判可用 + WARNING，不抛、不判残损。"""
        layout = _standard_layout()
        expected = layout["gguf"]["size"]
        p = tmp_path / "g.gguf"
        p.write_bytes(b"x" * 10)  # 10 字节 ≠ expected（布局期望 309855456）
        _point_layout(monkeypatch, tmp_path, layout, None)
        assert 10 != expected, "前置条件：布局期望体积必须与实得不同"
        messages, sink_id = _capture_loguru()
        try:
            assert ea.verify_gguf(p)  # 可用，且不得抛异常
        finally:
            logger.remove(sink_id)
        assert any("量化" in m or "不完整" in m for m in messages), (
            f"体积不符必须 WARNING 写清归因（换量化/文件不完整）：{messages}"
        )

    def test_null_size_skips_comparison(self, monkeypatch, tmp_path):
        """⑤ 布局数据 `gguf.size` 为 null ⇒ 关掉期望体积比对：任意非空文件皆可用，静默无告警。"""
        layout = _standard_layout()
        layout["gguf"]["size"] = None
        p = tmp_path / "g.gguf"
        p.write_bytes(b"q4-anything" * 123)  # 任意体积，与任何期望都不符
        _point_layout(monkeypatch, tmp_path, layout, None)
        messages, sink_id = _capture_loguru()
        try:
            assert ea.verify_gguf(p)
        finally:
            logger.remove(sink_id)
        assert messages == [], f"size=null 是显式关断，不得有任何告警：{messages}"
