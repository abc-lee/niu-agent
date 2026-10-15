"""T3a1：`llama_cpp` 配置段 v2 派生量同源契约测试（方案 §5'.5，2026-10-15 定稿）。

注：lightrag_adapter 侧改用本模块函数属 T3a2（本文件现在不测它——那边还是模块常量）。

三组用例：
① 行里 `-c` 改成 16384 ⇒ embed_token_limit()==16384，三个字符护栏**各自按系数**同步
   （server×7000 / adapter×2000 / split×1500，以 8192 为基线）——三个函数独立、系数不得互换；
② **缺省断言（防漂移，最关键）**：不写 `command` ⇒ embed 8192 / server 7000 / adapter 2000 /
   split 1500 / media_http_timeout 1031.52，与写死时代逐字一致；
③ **反向断言**：`char_prefilter_adapter() != char_prefilter_server()`——两侧系数不同
   （2000 vs 7000），并成一个函数＝把 lightrag 侧守卫放宽 3.5 倍（7000 恰是被实测判死的值）。

隔离（写法照 test_llama_config_read.py）：user-config / 布局 / HOME 全指向 tmp，
绝不读写真实 ~/.niu，不联网，不加载模型。
"""
import json
import sys
from pathlib import Path

import pytest

from niu_api.internal import embedding_artifacts as ea
from niu_api.internal import llama_config as lc

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
    """user-config / 布局 / HOME 全指向 tmp；清空布局缓存与一次性告警标记，防跨用例状态泄漏。"""
    from niu_api import config as cfg_mod

    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # 布局 expanduser 与 media_artifacts 的 Path.home()
    p = tmp_path / "user-config.json"
    p.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", str(p))
    ea._LAYOUT_CACHE = None
    ea._LAYOUT_CACHE_KEY = None
    ea._WARNED_REASONS.clear()
    lc._warned_reasons.clear()
    _point_layout(monkeypatch, tmp_path, _standard_layout())


def _point_config(monkeypatch, tmp_path, payload: str) -> str:
    """写 payload 到 tmp user-config.json 并把 CONFIG_PATH 指过去，返回路径。"""
    p = tmp_path / "user-config.json"
    p.write_text(payload, encoding="utf-8")
    monkeypatch.setattr("niu_api.config.CONFIG_PATH", str(p))
    return str(p)


def _write_llama(monkeypatch, tmp_path, section: dict) -> str:
    """写入带 `llama_cpp` 段的配置（section 整体给入），返回配置路径。"""
    return _point_config(monkeypatch, tmp_path, json.dumps({"llama_cpp": section}, ensure_ascii=False))


class TestDerivedScaleWithContext:
    """① 行里 `-c` 改 16384 ⇒ token 上限与三个字符护栏各自按系数同步（系数不得互换）。"""

    def test_all_derived_scale_with_ctx_16384(self, monkeypatch, tmp_path):
        _write_llama(monkeypatch, tmp_path, {"command": "/opt/llama-server -m /m.gguf -c 16384"})
        assert lc.embed_token_limit() == 16384
        # 三个独立函数各自按自己的系数同步（16384 = 2×8192 ⇒ 恰为缺省值的两倍）
        assert lc.char_prefilter_server() == int(16384 * 7000 / 8192)     # = 14000
        assert lc.char_prefilter_adapter() == int(16384 * 2000 / 8192)    # = 4000
        assert lc.split_char_budget_adapter() == int(16384 * 1500 / 8192)  # = 3000


class TestDerivedDefaultsZeroDrift:
    """② 缺省断言（防漂移，最关键）：不写 `command` ⇒ 各派生量与写死时代逐字一致。"""

    def test_all_defaults(self, monkeypatch, tmp_path):
        _point_config(monkeypatch, tmp_path, json.dumps({}))  # fixture 已写 {} 并指路，这里显式重申
        assert lc.embed_token_limit() == 8192
        assert lc.char_prefilter_server() == 7000
        assert lc.char_prefilter_adapter() == 2000
        assert lc.split_char_budget_adapter() == 1500
        assert lc.media_http_timeout() == 1031.52


class TestAdapterServerNeverMerged:
    """③ 反向断言：两侧系数不同（2000 vs 7000），并成一个函数即把 lightrag 侧守卫放宽 3.5 倍。"""

    def test_never_equal_in_default_state(self, monkeypatch, tmp_path):
        assert lc.char_prefilter_adapter() != lc.char_prefilter_server()
        assert lc.split_char_budget_adapter() != lc.char_prefilter_server()

    def test_never_equal_when_scaled(self, monkeypatch, tmp_path):
        _write_llama(monkeypatch, tmp_path, {"command": "/opt/llama-server -m /m.gguf -c 16384"})
        assert lc.char_prefilter_adapter() != lc.char_prefilter_server()
        assert lc.split_char_budget_adapter() != lc.char_prefilter_server()
