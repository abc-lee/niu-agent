"""T3a1：`llama_cpp` 配置段 v2（方案 §5'.5，2026-10-15 定稿）唯一读取点契约。

四组用例：
① 回缺省 + 绝不抛：user-config.json 缺失 / 坏 JSON / `llama_cpp` 非对象 ⇒ 四键全缺省
   （`command is None`、60.0 / 120.0 / 1031.52）、绝不抛 + WARNING；
   `startup_timeout` 坏值（"abc" / 非正）⇒ 回缺省 + WARNING，`null` ⇒ 静默回缺省。
② 旧结构化键不生效：段里写 port / batch / extra_argv / binary_path / gguf_path ⇒
   `port()` 仍是 19878、三个字符护栏仍是 7000 / 2000 / 1500（未被旧键影响）、
   一条废弃 WARNING；文件**未被改回**（读回逐字节比对）。
③ 缺段补写：配置里没有 `llama_cpp` 段（其它段在）⇒ `get()` 后文件出现**恰四个键**
   的缺省段（`command` = `default_command()` 展开后的生效命令行），其它段一字不变；
   反向：段已存在（哪怕只写一个键）⇒ 文件**逐字节不变**（单个缺键也不补）。
④ `command` 原样生效：自定义行 ⇒ `command_line()` 逐字返回（程序一个字都不加工）。

隔离（写法照 test_llama_layout_config.py）：`niu_api.config.CONFIG_PATH` monkeypatch
到 tmp 文件、`embedding_artifacts._layout_path` 指向 tmp 布局（内容取自包内平台种子，
与启动器首启拷入那份同源）、HOME 重定向 tmp——绝不读写真实 ~/.niu，不联网，不加载模型。
"""
import json
import sys
from pathlib import Path

import pytest
from loguru import logger

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
    """user-config / 布局 / HOME 全指向 tmp；清空布局缓存与两个告警标记，防跨用例状态泄漏。"""
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


def _write_config(tmp_path, payload: str) -> str:
    p = tmp_path / "user-config.json"
    p.write_text(payload, encoding="utf-8")
    return str(p)


def _point_config(monkeypatch, tmp_path, payload: str) -> str:
    """写 payload 到 tmp user-config.json 并把 CONFIG_PATH 指过去，返回路径。"""
    p = _write_config(tmp_path, payload)
    monkeypatch.setattr("niu_api.config.CONFIG_PATH", p)
    return p


def _write_llama(monkeypatch, tmp_path, section: dict) -> str:
    """写入带 `llama_cpp` 段的配置（section 整体给入），返回配置路径。"""
    return _point_config(monkeypatch, tmp_path, json.dumps({"llama_cpp": section}, ensure_ascii=False))


def _capture_warning():
    """loguru sink 捕获（llama_config 用 loguru，pytest caplog 捕获不到——项目既有模式）。"""
    messages: list[str] = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    return messages, sink_id


def _assert_all_defaults(cfg):
    """v2 四键零缺省漂移：`command` 为 None（⇒ 内置缺省行），三超时与写死时代逐字一致。"""
    assert cfg.command is None
    assert cfg.startup_timeout == 60.0
    assert cfg.http_timeout == 120.0
    assert cfg.media_http_timeout == 1031.52


class TestFallbackNeverRaises:
    """① 文件缺失 / 坏 JSON / 顶级或段类型错 ⇒ 四键回缺省 + WARNING、绝不抛。"""

    @pytest.mark.parametrize("payload", [
        "",            # 空文件
        "{不是JSON",    # 坏 JSON
        "[1,2]",       # 顶级类型错（list）
        '"str"',       # 顶级类型错（字符串）
        "null",        # 顶级类型错（null）
    ])
    def test_bad_file_falls_back_with_warning(self, monkeypatch, tmp_path, payload):
        _point_config(monkeypatch, tmp_path, payload)
        msgs, sink_id = _capture_warning()
        try:
            cfg = lc.get()  # 绝不抛
        finally:
            logger.remove(sink_id)
        _assert_all_defaults(cfg)
        assert any("[llama_config]" in m for m in msgs), f"坏输入必须记 WARNING（不静默）：{msgs}"

    def test_missing_file_falls_back_with_warning(self, monkeypatch, tmp_path):
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", str(tmp_path / "no-such.json"))
        msgs, sink_id = _capture_warning()
        try:
            cfg = lc.get()  # 绝不抛
        finally:
            logger.remove(sink_id)
        _assert_all_defaults(cfg)
        assert any("[llama_config]" in m for m in msgs), f"文件缺失必须记 WARNING：{msgs}"

    @pytest.mark.parametrize("section", [5, "s", [1, 2], None])
    def test_section_wrong_type_falls_back_with_warning(self, monkeypatch, tmp_path, section):
        _write_llama(monkeypatch, tmp_path, section)  # 段非 dict：数字 / 字符串 / list / null
        msgs, sink_id = _capture_warning()
        try:
            cfg = lc.get()  # 绝不抛
        finally:
            logger.remove(sink_id)
        _assert_all_defaults(cfg)
        assert any("[llama_config]" in m for m in msgs), f"段类型错必须记 WARNING：{msgs}"

    @pytest.mark.parametrize("value, expect_warning", [
        ("abc", True),   # 类型错 ⇒ WARNING + 回缺省
        (-1, True),      # 非正 ⇒ WARNING + 回缺省
        (0, True),       # 非正 ⇒ WARNING + 回缺省
        (None, False),   # null ⇒ 契约「null/缺失 ⇒ 缺省」，静默、不告警
    ])
    def test_startup_timeout_bad_falls_back(self, monkeypatch, tmp_path, value, expect_warning):
        _write_llama(monkeypatch, tmp_path, {"startup_timeout": value})
        msgs, sink_id = _capture_warning()
        try:
            cfg = lc.get()  # 绝不抛
        finally:
            logger.remove(sink_id)
        assert cfg.startup_timeout == 60.0
        assert (cfg.http_timeout, cfg.media_http_timeout) == (120.0, 1031.52)
        assert cfg.command is None
        if expect_warning:
            assert any("startup_timeout" in m for m in msgs), f"坏超时值必须记 WARNING：{msgs}"
        else:
            assert msgs == [], f"null 超时是合法形态，不得告警：{msgs}"

    def test_other_timeouts_share_validator(self, monkeypatch, tmp_path):
        """http_timeout / media_http_timeout 与 startup_timeout 同一校验器：坏值回各自缺省。"""
        _write_llama(monkeypatch, tmp_path, {"http_timeout": "abc", "media_http_timeout": -5})
        cfg = lc.get()
        assert cfg.http_timeout == 120.0
        assert cfg.media_http_timeout == 1031.52


class TestDeprecatedKeysInert:
    """② v2 旧结构化键一律不读：出现 ⇒ 一条废弃 WARNING，不生效、不写回。"""

    def test_old_keys_do_not_take_effect_and_file_untouched(self, monkeypatch, tmp_path):
        path = _write_llama(monkeypatch, tmp_path, {
            "port": 12345, "batch": 16384, "extra_argv": ["--ngl", "99"],
            "binary_path": "/x/y", "gguf_path": "/z.g",
        })
        before = Path(path).read_bytes()
        msgs, sink_id = _capture_warning()
        try:
            cfg = lc.get()
        finally:
            logger.remove(sink_id)
        assert cfg.command is None  # 旧键不产生生效行 ⇒ 仍是内置缺省行
        # “只取三件”全部从生效行（缺省行 --port 19878 / -c 8192）派生：旧键一个都不影响
        assert lc.port() == 19878
        assert lc.char_prefilter_server() == 7000
        assert lc.char_prefilter_adapter() == 2000
        assert lc.split_char_budget_adapter() == 1500
        assert any("已废弃" in m and "command" in m for m in msgs), f"废弃键必须记 WARNING（不静默）：{msgs}"
        assert Path(path).read_bytes() == before, "旧键段不得被写回（文件一个字都不改）"


class TestMissingSectionMaterialized:
    """③ 缺段补写（存量装机）：段缺失 ⇒ 原子补写四键缺省段；段已存在 ⇒ 文件一个字都不动。"""

    def test_missing_section_materialized_exactly_four_keys(self, monkeypatch, tmp_path):
        """其它段在、无 `llama_cpp` ⇒ 补写后段恰为四键，`command` 是展开后的生效行，其它段原样。"""
        media = {"enabled": True}
        misc = {"note": "keep-me", "n": 3}
        path = _point_config(monkeypatch, tmp_path, json.dumps({"media_ingest": media, "misc": misc}))
        cfg = lc.get()  # 绝不抛（补写失败也不得毒化调用方）
        _assert_all_defaults(cfg)  # get() 仍回缺省：补写在读取之后发生
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        assert data["media_ingest"] == media and data["misc"] == misc, "其它段一字不得变"
        sec = data["llama_cpp"]
        assert set(sec) == {"command", "startup_timeout", "http_timeout", "media_http_timeout"}
        assert isinstance(sec["command"], str) and sec["command"], "`command` 必须是非空字符串"
        assert sec["command"] == lc.default_command(), "补写的 `command` ＝展开后的生效命令行"
        assert (sec["startup_timeout"], sec["http_timeout"], sec["media_http_timeout"]) == (60.0, 120.0, 1031.52)

    def test_existing_section_never_touched_even_partial(self, monkeypatch, tmp_path):
        """反向：段已存在（哪怕只写一个键）⇒ 调用后文件逐字节不变（单个缺键也不补）。"""
        path = _write_llama(monkeypatch, tmp_path, {"startup_timeout": 99})
        before = Path(path).read_bytes()
        cfg = lc.get()
        assert cfg.startup_timeout == 99.0
        assert (cfg.http_timeout, cfg.media_http_timeout) == (120.0, 1031.52)  # 文件不补，读层回缺省
        assert Path(path).read_bytes() == before, "段已存在 ⇒ 文件一个字都不动"


class TestCommandVerbatim:
    """④ `command` 原样生效：用户行 ⇒ `command_line()` 逐字返回（程序一个字都不改）。"""

    CUSTOM = "/opt/llama-server -m /models/qwen.gguf --no-mmproj --port 9999"

    def test_custom_line_returned_unchanged(self, monkeypatch, tmp_path):
        path = _write_llama(monkeypatch, tmp_path, {"command": self.CUSTOM})
        before = Path(path).read_bytes()
        cfg = lc.get()
        assert cfg.command == self.CUSTOM
        assert lc.command_line() == self.CUSTOM, "生效行必须逐字等于用户行（不经任何加工）"
        assert lc.argv()[0] == "/opt/llama-server"  # “只取三件”从用户的行里取
        assert lc.port() == 9999
        assert Path(path).read_bytes() == before

    def test_blank_command_falls_back_to_default_line(self, monkeypatch, tmp_path):
        """纯空白行 ＝ 没写 ⇒ 回内置缺省行（不告警——用户自己留白的正常形态）。"""
        _write_llama(monkeypatch, tmp_path, {"command": "   "})
        cfg = lc.get()
        assert cfg.command is None
        assert lc.command_line() == lc.default_command()
