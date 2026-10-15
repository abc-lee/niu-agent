"""T1：`llama_cpp` 派生量同源化（方案 §5.2 派生量表，R1 P1-1 + R5 N1/N2）。

覆盖：
① 改 `batch/ubatch`（16384 ⇒ 生效钳制 10000）⇒ 五个派生量**各自按系数**变化
   （7000/2000/1500 三个系数不得互相替代——分侧函数是 R5 N1 的 P1 结论）；
② **缺省断言（防漂移，最关键）**：不配置任何键 ⇒ embed 8192 / server 7000 /
   adapter 2000 / split 1500 / image 8128 / media ≈1031.52，与写死时代逐字一致；
③ **反向断言**：`char_prefilter_adapter() != char_prefilter_server()`
   ——两侧系数 2000 vs 7000 不同，并成一个函数＝把 lightrag 侧守卫放宽 3.5 倍
   （7000 正是被实测判死的值）。
注入临时 CONFIG_PATH，绝不读写真实 ~/.niu。
"""
import json
import pytest

from niu_api.internal import llama_config as lc

# 缺省（batch=ubatch=8192）下各派生量的写死时代值——零漂移基准。
_BASE_MIN = 8192
_BASE = {
    "embed": 8192,                                        # min(batch, ubatch)
    "server": int(8192 * 7000 / 8192),                    # 7000
    "adapter": int(8192 * 2000 / 8192),                   # 2000
    "split": int(8192 * 1500 / 8192),                     # 1500
    "image": 8192 - 0 - 64,                               # 8128
    "media": 8128 * 0.09 + 300.0,                         # 1031.52
}
# 钳制后（batch=ubatch=16384 ⇒ 生效 min=10000）的期望值——按各自系数同步变化。
_CLAMPED = {
    "embed": 10000,
    "server": int(10000 * 7000 / 8192),                   # 8544
    "adapter": int(10000 * 2000 / 8192),                  # 2441
    "split": int(10000 * 1500 / 8192),                    # 1831
    "image": 10000 - 64,                                  # 9936
    "media": (10000 - 64) * 0.09 + 300.0,                 # 1194.24
}


def _derived(cfg_min):
    """把当前派生值收成 dict 便于逐系数比对。"""
    return {
        "embed": lc.embed_token_limit(),
        "server": lc.char_prefilter_server(),
        "adapter": lc.char_prefilter_adapter(),
        "split": lc.split_char_budget_adapter(),
        "image": lc.image_max_tokens(),
        "media": lc.media_http_timeout(),
    }


def _write_llama(tmp_path, **keys) -> str:
    p = tmp_path / "user-config.json"
    p.write_text(json.dumps({"llama_cpp": keys}), encoding="utf-8")
    return str(p)


class TestDerivedDefaultsZeroDrift:
    """② 缺省断言：不配置任何新键 ⇒ 六个派生量与写死时代逐字一致（防漂移）。"""

    def test_all_six_derived_values_match_legacy_constants(self, monkeypatch, tmp_path):
        monkeypatch.setattr("niu_api.config.CONFIG_PATH",
                           _write_llama(tmp_path))  # 空段＝全缺省
        got = _derived(None)
        assert got["embed"] == _BASE["embed"] == 8192
        assert got["server"] == _BASE["server"] == 7000
        assert got["adapter"] == _BASE["adapter"] == 2000
        assert got["split"] == _BASE["split"] == 1500
        assert got["image"] == _BASE["image"] == 8128
        assert got["media"] == pytest_approx(_BASE["media"])  # ≈1031.52

    def test_no_section_at_all_is_same_as_empty_section(self, monkeypatch, tmp_path):
        """配置里根本没有 `llama_cpp` 段（存量装机形态）⇒ 同样零漂移。"""
        p = tmp_path / "user-config.json"
        p.write_text("{}", encoding="utf-8")
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", str(p))
        assert lc.embed_token_limit() == 8192
        assert lc.char_prefilter_server() == 7000
        assert lc.char_prefilter_adapter() == 2000
        assert lc.split_char_budget_adapter() == 1500
        assert lc.image_max_tokens() == 8128
        assert lc.media_http_timeout() == pytest_approx(1031.52)


class TestDerivedScaleWithBatch:
    """① 改 batch/ubatch ⇒ 五个派生量各自按系数同步变化（16384 ⇒ 生效 10000）。"""

    def test_sixteen_k_clamped_scales_each_function_by_its_own_coefficient(
            self, monkeypatch, tmp_path):
        monkeypatch.setattr("niu_api.config.CONFIG_PATH",
                           _write_llama(tmp_path, batch=16384, ubatch=16384))
        got = _derived(None)
        # 各自按系数：7000/2000/1500 三个系数互不相同 ⇒ 结果必须互不相同
        assert got["embed"] == _CLAMPED["embed"] == 10000
        assert got["server"] == _CLAMPED["server"] == 8544
        assert got["adapter"] == _CLAMPED["adapter"] == 2441
        assert got["split"] == _CLAMPED["split"] == 1831
        assert got["image"] == _CLAMPED["image"] == 9936
        assert got["media"] == pytest_approx(_CLAMPED["media"])  # 1194.24
        # 两条硬不变式对钳制后的派生值仍成立
        assert got["image"] <= 9999
        assert got["media"] < 1200.0   # 严格小于 INFLIGHT_TIMEOUT

    def test_proportional_not_stepwise(self, monkeypatch, tmp_path):
        """4096 ⇒ 按比例缩到一半（3500/1000/750/4032/662.88）——不是"要么 8192 要么 10000"。"""
        monkeypatch.setattr("niu_api.config.CONFIG_PATH",
                           _write_llama(tmp_path, batch=4096, ubatch=4096))
        assert lc.embed_token_limit() == 4096
        assert lc.char_prefilter_server() == int(4096 * 7000 / 8192) == 3500
        assert lc.char_prefilter_adapter() == int(4096 * 2000 / 8192) == 1000
        assert lc.split_char_budget_adapter() == int(4096 * 1500 / 8192) == 750
        assert lc.image_max_tokens() == 4096 - 64 == 4032
        assert lc.media_http_timeout() == pytest_approx(4032 * 0.09 + 300.0)

    def test_one_sided_overshoot_does_not_move_derived_values(self, monkeypatch, tmp_path):
        """batch=16384 但 ubatch=8192 ⇒ 生效 min 仍是 8192（钳制作用于 min，不放大）。"""
        monkeypatch.setattr("niu_api.config.CONFIG_PATH",
                           _write_llama(tmp_path, batch=16384, ubatch=8192))
        assert lc.embed_token_limit() == 8192
        assert lc.char_prefilter_server() == 7000
        assert lc.char_prefilter_adapter() == 2000
        assert lc.split_char_budget_adapter() == 1500
        assert lc.image_max_tokens() == 8128

    def test_reread_each_call_tracks_config_without_restart(self, monkeypatch, tmp_path):
        """每次现读：改配置文件 ⇒ 下一次调用即按新值，免重启。"""
        p = tmp_path / "user-config.json"
        p.write_text(json.dumps({"llama_cpp": {"batch": 8192, "ubatch": 8192}}),
                     encoding="utf-8")
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", str(p))
        assert lc.embed_token_limit() == 8192
        p.write_text(json.dumps({"llama_cpp": {"batch": 16384, "ubatch": 16384}}),
                     encoding="utf-8")
        assert lc.embed_token_limit() == 10000


class TestAdapterServerNeverMerged:
    """③ 反向断言：两侧字符护栏系数不同（2000 vs 7000），并成一个函数即 P1 回归。"""

    def test_adapter_differs_from_server_at_defaults(self, monkeypatch, tmp_path):
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_llama(tmp_path))
        assert lc.char_prefilter_adapter() != lc.char_prefilter_server()

    def test_adapter_differs_from_server_after_clamp(self, monkeypatch, tmp_path):
        monkeypatch.setattr("niu_api.config.CONFIG_PATH",
                           _write_llama(tmp_path, batch=16384, ubatch=16384))
        assert lc.char_prefilter_adapter() != lc.char_prefilter_server()
        # 且 adapter 始终是三者中最小口径（2000 系数）
        assert lc.char_prefilter_adapter() < lc.char_prefilter_server()

    def test_shared_embed_limit_is_side_neutral(self, monkeypatch, tmp_path):
        """`embed_token_limit()` 两侧共用同一个值（服务器侧与 lightrag 侧同源，R1 P1-1）。"""
        monkeypatch.setattr("niu_api.config.CONFIG_PATH",
                           _write_llama(tmp_path, batch=16384, ubatch=16384))
        assert lc.embed_token_limit() == 10000 == min(lc.get().batch, lc.get().ubatch)


def pytest_approx(x):
    """近似比较（媒体超时是 float 表达式结果）。"""
    return pytest.approx(x, abs=1e-9)
