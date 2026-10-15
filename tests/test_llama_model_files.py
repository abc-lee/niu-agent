"""布局派生模型文件路径 + 体积校验可选化（方案 v1.7，§5.1 配置键表）。

旧的 `llama_cpp.gguf_path` / `llama_cpp.mmproj_path` 配置覆盖键已作废删除（换
二进制/换模型＝改配置里那一行 `llama_cpp.command`）；本文件只验布局派生缺省与
体积校验可选化行为。

三用例：
① 无配置覆盖 ⇒ 与布局种子缺省零漂移：GGUF＝`<models>/<布局 gguf.filename>`，
   mmproj＝`~/.niu/models/<布局 mmproj.filename>`；
② 体积不匹配 ⇒ `verify_gguf` 仍返回可用 + WARNING（不抛、不判残损）；
③ 布局数据里 `size` 为 null ⇒ 跳过体积比对（静默，无任何告警）。

全部离线、不碰真实 ~/.niu：user-config / 布局 / HOME 全指向 tmp（布局内容为
包内平台种子 memory/llama-layout.<os>.json，拷到 tmp 后经 `_layout_path` 注入，
先例 test_llama_config_read.py）；布局缓存与一次性告警标记每用例重置。
"""
import json
import sys
from pathlib import Path

import pytest
from loguru import logger

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
def _isolated(monkeypatch, tmp_path):
    """user-config / 布局 / HOME 全指向 tmp（布局＝包内平台种子的 tmp 拷贝）；
    清空布局缓存与告警标记，防跨用例状态泄漏。"""
    p = tmp_path / "user-config.json"
    p.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", str(p))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home" / ".niu" / "models").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
    ea._LAYOUT_CACHE = None
    ea._LAYOUT_CACHE_KEY = None
    ea._WARNED_REASONS.clear()
    _point_layout(monkeypatch, tmp_path, _standard_layout())


def _capture_loguru():
    """loguru sink 捕获（模块用 loguru 而非 stdlib logging，pytest caplog 捕不到——
    项目既有模式，同 test_gemma_tokenizer.py）。"""
    messages: list[str] = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    return messages, sink_id


class TestDefaultPathZeroDrift:
    def test_no_new_keys_zero_default_drift(self, monkeypatch, tmp_path):
        """① 无配置覆盖（旧键已删）⇒ 与布局种子缺省零漂移：GGUF＝`<models>/<布局 gguf.filename>`，
        mmproj＝`~/.niu/models/<布局 mmproj.filename>`。"""
        monkeypatch.setenv("NIU_MODELS_PATH", str(tmp_path))
        layout = _standard_layout()
        _point_layout(monkeypatch, tmp_path, layout)
        assert ea.gguf_path() == tmp_path / layout["gguf"]["filename"]
        assert ma.mmproj_path() == (
            tmp_path / "home" / ".niu" / "models" / layout["mmproj"]["filename"]
        )


class TestVerifyGgufOptionalSize:
    def test_size_mismatch_usable_with_warning(self, monkeypatch, tmp_path):
        """② 体积不匹配（布局期望 vs 实得）⇒ 仍判可用 + WARNING，不抛、不判残损。"""
        layout = _standard_layout()
        expected = layout["gguf"]["size"]
        p = tmp_path / "g.gguf"
        p.write_bytes(b"x" * 10)  # 10 字节 ≠ expected（布局期望 309855456）
        _point_layout(monkeypatch, tmp_path, layout)
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
        """③ 布局数据 `gguf.size` 为 null ⇒ 关掉期望体积比对：任意非空文件皆可用，静默无告警。"""
        layout = _standard_layout()
        layout["gguf"]["size"] = None
        p = tmp_path / "g.gguf"
        p.write_bytes(b"q4-anything" * 123)  # 任意体积，与任何期望都不符
        _point_layout(monkeypatch, tmp_path, layout)
        messages, sink_id = _capture_loguru()
        try:
            assert ea.verify_gguf(p)
        finally:
            logger.remove(sink_id)
        assert messages == [], f"size=null 是显式关断，不得有任何告警：{messages}"
