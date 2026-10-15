"""T1：`llama_cpp` 段唯一读取点（方案 §5.1，Task 1）——坏输入回缺省 + WARNING、绝不抛。

覆盖：文件缺失 / 空文件 / 坏 JSON / 顶级或段类型错 ⇒ 全键回缺省（零漂移）+ WARNING；
逐键类型错/越界 ⇒ 该键回缺省（batch/ubatch 超 10000 ⇒ 钳制到 10000 而非回缺省）；
`image_max_tokens` 显式值违反 `N ≤ 9999` 或 `N < min(batch,ubatch)` ⇒ 回派生值 + WARNING。
全部注入**临时** CONFIG_PATH，绝不读写真实 ~/.niu（monkeypatch 先例：
test_context_window_live_read.py / test_media_ingest_config.py）。
"""
import json

import pytest
from loguru import logger

from niu_api.internal import llama_config as lc


def _write_config(tmp_path, payload: str) -> str:
    p = tmp_path / "user-config.json"
    p.write_text(payload, encoding="utf-8")
    return str(p)


def _write_llama(tmp_path, **keys) -> str:
    """写入带 `llama_cpp` 段的配置（keys 直接铺进该段）。"""
    return _write_config(tmp_path, json.dumps({"llama_cpp": keys}))


def _capture_warning():
    """loguru sink 捕获（llama_config 用 loguru，pytest caplog 捕获不到——项目既有模式）。"""
    messages: list[str] = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    return messages, sink_id


def _assert_all_defaults(cfg):
    """零缺省漂移：全键与写死时代逐字一致。"""
    assert cfg.port == 19878
    assert cfg.host == "127.0.0.1"
    assert cfg.context_size == 8192
    assert cfg.batch == 8192
    assert cfg.ubatch == 8192
    assert cfg.image_max_tokens == 8128  # 8192 − 0 − 64，派生
    assert cfg.startup_timeout == 60.0
    assert cfg.http_timeout == 120.0
    assert cfg.extra_argv == ()
    assert cfg.variant == "cpu"
    assert cfg.binary_path is None


class TestBrokenFileNeverRaises:
    """文件缺失 / 空文件 / 坏 JSON / 类型错 ⇒ 全键回缺省 + WARNING、绝不抛。"""

    @pytest.mark.parametrize("payload", [
        "",            # 空文件
        "{不是JSON",    # 坏 JSON
        "[1,2]",       # 顶级类型错（list）
        '"str"',       # 顶级类型错（字符串）
        "null",        # 顶级类型错（null）
    ])
    def test_bad_file_falls_back_to_defaults_with_warning(self, monkeypatch, tmp_path, payload):
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_config(tmp_path, payload))
        msgs, sink_id = _capture_warning()
        try:
            cfg = lc.get()  # 绝不抛
        finally:
            logger.remove(sink_id)
        _assert_all_defaults(cfg)
        assert any("[llama-config]" in m for m in msgs), f"坏输入必须记 WARNING（不静默）：{msgs}"

    def test_missing_file_falls_back_with_warning(self, monkeypatch, tmp_path):
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", str(tmp_path / "no-such.json"))
        msgs, sink_id = _capture_warning()
        try:
            cfg = lc.get()
        finally:
            logger.remove(sink_id)
        _assert_all_defaults(cfg)
        assert any("[llama-config]" in m for m in msgs), f"文件缺失必须记 WARNING：{msgs}"

    def test_section_wrong_type_falls_back_with_warning(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "niu_api.config.CONFIG_PATH",
            _write_config(tmp_path, json.dumps({"llama_cpp": 5})))
        msgs, sink_id = _capture_warning()
        try:
            cfg = lc.get()
        finally:
            logger.remove(sink_id)
        _assert_all_defaults(cfg)
        assert any("[llama-config]" in m for m in msgs)

    def test_no_section_is_default_not_warning(self, monkeypatch, tmp_path):
        """无 `llama_cpp` 段＝用户没配（正常形态）⇒ 缺省、不告警。"""
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_config(tmp_path, "{}"))
        msgs, sink_id = _capture_warning()
        try:
            cfg = lc.get()
        finally:
            logger.remove(sink_id)
        _assert_all_defaults(cfg)
        assert msgs == [], f"正常形态（无段）不得告警：{msgs}"


class TestKeyValidation:
    """逐键类型错/越界 ⇒ 该键回缺省 + WARNING；合法覆盖原样生效。"""

    @pytest.mark.parametrize("key, bad, default", [
        ("port", "19878", 19878),            # 字符串类型错
        ("port", 0, 19878),                  # 越界（下）
        ("port", 65536, 19878),              # 越界（上）
        ("host", "", "127.0.0.1"),           # 空串
        ("host", 1234, "127.0.0.1"),         # 类型错
        ("context_size", -8, 8192),          # 越界
        ("context_size", "8192", 8192),      # 类型错
        ("batch", -1, 8192),                 # 越界（≤0 回缺省，非钳制）
        ("ubatch", "8192", 8192),            # 类型错
        ("startup_timeout", -60.0, 60.0),    # 非正
        ("startup_timeout", "60", 60.0),     # 类型错
        ("http_timeout", None, 120.0),       # null 类型错
        ("extra_argv", "not-a-list", ()),    # 类型错
        ("variant", 42, "cpu"),              # 类型错
        ("binary_path", ["/x"], None),       # 类型错
    ])
    def test_bad_key_falls_back_with_warning(self, monkeypatch, tmp_path, key, bad, default):
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_llama(tmp_path, **{key: bad}))
        msgs, sink_id = _capture_warning()
        try:
            cfg = lc.get()
        finally:
            logger.remove(sink_id)
        got = getattr(cfg, key)
        assert got == default, f"{key}={bad!r} 应回缺省 {default!r}，实得 {got!r}"
        assert any(key in m and "[llama-config]" in m for m in msgs), (
            f"{key} 坏值必须记 WARNING（不静默）：{msgs}")

    def test_batch_over_cap_clamps_to_10000_not_default(self, monkeypatch, tmp_path):
        """硬不变式①：> 10000 钳制到 10000（**不是**回缺省 8192）+ WARNING 说明原因。"""
        monkeypatch.setattr(
            "niu_api.config.CONFIG_PATH",
            _write_llama(tmp_path, batch=16384, ubatch=16384))
        msgs, sink_id = _capture_warning()
        try:
            cfg = lc.get()
        finally:
            logger.remove(sink_id)
        assert cfg.batch == 10000 and cfg.ubatch == 10000
        assert any("钳制" in m or "10000" in m for m in msgs), f"钳制必须 WARNING 说明原因：{msgs}"

    def test_valid_overrides_applied_as_is(self, monkeypatch, tmp_path):
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_llama(
            tmp_path, port=19879, host="0.0.0.0", context_size=4096,
            batch=4096, ubatch=4096, image_max_tokens=4000,
            startup_timeout=30, http_timeout=60,
            extra_argv=["--ngl", "99"], variant="cuda", binary_path="/opt/llama-server"))
        msgs, sink_id = _capture_warning()
        try:
            cfg = lc.get()
        finally:
            logger.remove(sink_id)
        assert cfg.port == 19879
        assert cfg.host == "0.0.0.0"
        assert cfg.context_size == 4096
        assert cfg.batch == 4096 and cfg.ubatch == 4096
        assert cfg.image_max_tokens == 4000   # 4000 ≤ 9999 且 < 4096 ⇒ 合法，原样
        assert cfg.startup_timeout == 30.0
        assert cfg.http_timeout == 60.0
        assert cfg.extra_argv == ("--ngl", "99")
        assert cfg.variant == "cuda"
        assert cfg.binary_path == "/opt/llama-server"
        assert msgs == [], f"全合法输入不得告警：{msgs}"

    def test_binary_path_null_is_none(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "niu_api.config.CONFIG_PATH", _write_llama(tmp_path, binary_path=None))
        assert lc.get().binary_path is None


class TestImageMaxTokensInvariants:
    """显式 `image_max_tokens` 必须同时满足 N ≤ 9999 且 N < min(batch,ubatch)，
    否则回派生值 + WARNING（两条硬不变式对显式值同样成立）。"""

    def test_explicit_above_ninety_nine_ninety_nine_falls_back(self, monkeypatch, tmp_path):
        """N = 12000 > 9999 ⇒ 回派生 8128（缺省 min=8192）+ WARNING。"""
        monkeypatch.setattr(
            "niu_api.config.CONFIG_PATH", _write_llama(tmp_path, image_max_tokens=12000))
        msgs, sink_id = _capture_warning()
        try:
            assert lc.get().image_max_tokens == 8128
        finally:
            logger.remove(sink_id)
        assert any("image_max_tokens" in m for m in msgs), f"必须 WARNING：{msgs}"

    def test_explicit_equal_to_min_falls_back(self, monkeypatch, tmp_path):
        """N = 8192 = min(batch,ubatch)：违反**严格**小于 ⇒ 回派生 8128 + WARNING。"""
        monkeypatch.setattr(
            "niu_api.config.CONFIG_PATH",
            _write_llama(tmp_path, image_max_tokens=8192))
        msgs, sink_id = _capture_warning()
        try:
            assert lc.get().image_max_tokens == 8128
        finally:
            logger.remove(sink_id)
        assert any("image_max_tokens" in m for m in msgs), f"必须 WARNING：{msgs}"

    def test_explicit_between_min_and_cap_falls_back(self, monkeypatch, tmp_path):
        """min=8192 时 N=8193：违反 N < min ⇒ 回派生。"""
        monkeypatch.setattr(
            "niu_api.config.CONFIG_PATH",
            _write_llama(tmp_path, image_max_tokens=8193))
        assert lc.get().image_max_tokens == 8128

    def test_explicit_valid_at_clamped_min(self, monkeypatch, tmp_path):
        """边界：batch/ubatch 钳制后 min=10000，N=9999 ≤ 9999 且 < 10000 ⇒ 合法原样。"""
        monkeypatch.setattr(
            "niu_api.config.CONFIG_PATH",
            _write_llama(tmp_path, batch=16384, ubatch=16384, image_max_tokens=9999))
        cfg = lc.get()
        assert cfg.batch == 10000 and cfg.ubatch == 10000
        assert cfg.image_max_tokens == 9999

    def test_missing_key_derives_silently(self, monkeypatch, tmp_path):
        """不写 `image_max_tokens` ⇒ 派生，且无告警（这是默认形态，不是坏输入）。"""
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_llama(tmp_path, batch=8192))
        msgs, sink_id = _capture_warning()
        try:
            assert lc.get().image_max_tokens == 8128
        finally:
            logger.remove(sink_id)
        assert msgs == []
