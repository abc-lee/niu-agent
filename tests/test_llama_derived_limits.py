"""T3a1：`llama_cpp` 配置段 v2 派生量同源契约测试（方案 §5'.5，2026-10-15 定稿）。

T3a2 已落地：lightrag_adapter / media_worker 侧的三阈值改为调用本模块函数（单一真相），
本文件第④组用例测两侧同源（哨兵值改变适配器行为）与三常量的防回流。

四组用例：
① 行里 `-c` 改成 16384 ⇒ embed_token_limit()==16384，三个字符护栏**各自按系数**同步
   （server×7000 / adapter×2000 / split×1500，以 8192 为基线）——三个函数独立、系数不得互换；
② **缺省断言（防漂移，最关键）**：不写 `command` ⇒ embed 8192 / server 7000 / adapter 2000 /
   split 1500 / media_http_timeout 1031.52，与写死时代逐字一致；
③ **反向断言**：`char_prefilter_adapter() != char_prefilter_server()`——两侧系数不同
   （2000 vs 7000），并成一个函数＝把 lightrag 侧守卫放宽 3.5 倍（7000 恰是被实测判死的值）。
④ **两侧同源 + 防回流（T3a2）**：哨兵值改 embed 上限 ⇒ lightrag 守卫的预筛 / 预算 / 判定
   随生效值缩放（同文不同判，证明调用点走函数而非常量）；两模块不再持有三个旧常量。

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


# 三个已退役的模块常量名（T3a2 前存在于 lightrag_adapter；拼接构造防回流断言，
# 避免本文件字面持有旧名而让全仓 grep 防残留判据失效）。
_RETIRED_CONSTANT_NAMES = (
    "EMBED" + "_TOKEN_LIMIT",
    "_GUARD_CHAR" + "_PREFILTER",
    "_GUARD_SPLIT_CHAR" + "_BUDGET",
)


class TestT3a2AdapterSameSource:
    """④ T3a2：lightrag_adapter 侧三阈值已改为调 llama_config 函数（与 llama-server 侧
    单一真相同源），随生效行 -c 缩放；三常量退役、防回流。"""

    def test_adapter_guard_follows_effective_limit(self, monkeypatch, tmp_path):
        """改 command 行 -c（或哨兵值改 embed 上限）⇒ lightrag 守卫的预筛 / 预算 / 判定
        随生效值同步缩放——同一输入同文不同判，证明调用点走的是函数而非常量。"""
        from niu_api.internal import lightrag_adapter as la

        # 权威计量打桩：恒返回远超任何上限的值（真实 /tokenize 依赖 llama-server 在位，
        # 契约层用哨兵值替代，只验派生阈值如何改变守卫走向）。
        monkeypatch.setattr(la, "_count_embed_tokens", lambda text: 999999)

        # 生效行 -c 16384 ⇒ 预筛 4000 / 预算 3000（服务侧 embed 上限同读 16384）。
        _write_llama(monkeypatch, tmp_path, {"command": "/opt/llama-server -m /m.gguf -c 16384"})
        assert lc.embed_token_limit() == 16384
        # 2000 字符 ≤ 预筛 4000 ⇒ 直接放行，不做权威计量。
        assert la._guard_embed_text("chunk", "t", "a" * 2000) is None
        # 5000 字符超预筛且计量超限 ⇒ 按预算 3000 分段（3000 + 2000）。
        segs = la._guard_embed_text("chunk", "t", "a" * 5000)
        assert [len(s) for s in segs] == [3000, 2000]

        # 哨兵值：embed 上限改 4096 ⇒ 预筛 1000 / 预算 750，同一 2000 字符现在超限并被分段。
        monkeypatch.setattr(lc, "embed_token_limit", lambda: 4096)
        assert la._guard_embed_text("chunk", "t", "a" * 2000) is not None
        segs = la._guard_embed_text("chunk", "t", "a" * 2000)
        assert [len(s) for s in segs] == [750, 750, 500]

    def test_guard_call_sites_are_llama_config_functions(self):
        """调用点审计：lightrag_adapter 源码里对 llama_config 的派生调用须覆盖
        embed_token_limit / char_prefilter_adapter / split_char_budget_adapter 三者
        （AST 判定，不依赖运行环境）。"""
        import ast

        from niu_api.internal import lightrag_adapter as la

        src = Path(la.__file__).read_text(encoding="utf-8")
        calls = set()
        for node in ast.walk(ast.parse(src)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "llama_config"
            ):
                calls.add(node.func.attr)
        wanted = {"embed_token_limit", "char_prefilter_adapter", "split_char_budget_adapter"}
        assert wanted <= calls, f"lightrag_adapter 缺少 llama_config 派生调用：{wanted - calls}"

    def test_retired_constants_absent(self):
        """防回流：lightrag_adapter / media_worker 两模块不得再持有三个已退役常量名。"""
        from niu_api.internal import lightrag_adapter as la
        from niu_api.internal import media_worker as mw

        for name in _RETIRED_CONSTANT_NAMES:
            assert not hasattr(la, name), f"lightrag_adapter 残留已退役常量 {name}"
            assert not hasattr(mw, name), f"media_worker 残留已退役常量 {name}"
