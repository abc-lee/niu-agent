"""T3a1：`llama_cpp` 配置段 v2 命令行解析与派生量契约测试（方案 §5'.2 / §5'.5，2026-10-15 定稿）。

六组用例：
① 缺省行等价：不写 `command` ⇒ `llama_config.argv()` 与旧实现 `llama_server._build_argv()`
   **逐元素相等**（`media_ingest.enabled` 开 / 关两分支各一例，同一隔离环境里算）；
② `--port` / `--host` 解析：空格与等号两种形态都识别；行里没写 ⇒ `19878` / `127.0.0.1`；
   坏值（`0` / 非整数）与行尾悬 flag ⇒ 回缺省、绝不抛；
③ `-c` / `--ctx-size`：空格、`--ctx-size`、等号三形态同值；缺失 / 非法（`0` / 非整数）⇒ `8192`；
④ `shlex` 失败：行里引号不配对 ⇒ `argv()` 退空白分词（不抛），`command_line()` 一字不改地
   返回原行；
⑤ 多模态三态：`--mmproj <路径>` ⇒ `True`；`--no-mmproj` ⇒ `False`；
   两者都无 ⇒ `None`（未声明，绝不静默当 True）；
⑥ 首词与模型路径：`executable()` ＝ `argv()[0]`；`-m` / `--model`（空格与等号两形态）取值；
   行里没有 ⇒ `None`。

隔离（写法照 test_llama_config_read.py）：user-config / 布局 / HOME 全指向 tmp，清空布局缓存
与一次性告警标记——绝不读写真实 ~/.niu，不联网，不加载模型。`llama_server` 仅作旧实现
argv 的等价基准（模块 import 不拉起进程——`atexit` 钩子在 `start()` 里才注册）。
"""
import json
import sys
from pathlib import Path

import pytest
from loguru import logger

from niu_api.internal import embedding_artifacts as ea
from niu_api.internal import llama_config as lc
from niu_api.internal import llama_server as ls

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


def _point_config(monkeypatch, tmp_path, payload: str) -> str:
    """写 payload 到 tmp user-config.json 并把 CONFIG_PATH 指过去，返回路径。"""
    p = tmp_path / "user-config.json"
    p.write_text(payload, encoding="utf-8")
    monkeypatch.setattr("niu_api.config.CONFIG_PATH", str(p))
    return str(p)


def _write_llama(monkeypatch, tmp_path, section: dict) -> str:
    """写入带 `llama_cpp` 段的配置（section 整体给入），返回配置路径。"""
    return _point_config(monkeypatch, tmp_path, json.dumps({"llama_cpp": section}, ensure_ascii=False))


def _capture_warning():
    """loguru sink 捕获（llama_config 用 loguru，pytest caplog 捕获不到——项目既有模式）。"""
    messages: list[str] = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    return messages, sink_id


class TestDefaultLineParityWithLegacyBuilder:
    """① 不写 `command` ⇒ 缺省行（布局逐段生成）与旧 `llama_server._build_argv()` 逐元素相等。"""

    @pytest.mark.parametrize("enabled", [True, False], ids=["media-on", "media-off"])
    def test_default_argv_matches_legacy_build_argv(self, monkeypatch, tmp_path, enabled):
        _point_config(monkeypatch, tmp_path, json.dumps({"media_ingest": {"enabled": enabled}}))
        assert lc.command_line() == lc.default_command(), "不写 command ⇒ 生效行就是内置缺省行"
        assert lc.argv() == ls._build_argv(), \
            f"media_ingest.enabled={enabled}：v2 缺省行必须与旧实现 argv 逐元素相等（零漂移）"
        # 三态判定同样从缺省行得出：开 ⇒ 声明多模态（True），关 ⇒ 明确关闭（False，不是 None）
        assert lc.mmproj_state() is (True if enabled else False)


class TestPortHostParsing:
    """② `--port` / `--host`：空格形态与等号形态都识别；没写 ⇒ 缺省；坏值 / 悬 flag ⇒ 回缺省不抛。"""

    def test_space_forms(self, monkeypatch, tmp_path):
        _write_llama(monkeypatch, tmp_path,
                     {"command": "/opt/llama-server -m /m.gguf --port 12345 --host 0.0.0.0"})
        assert lc.port() == 12345
        assert lc.host() == "0.0.0.0"

    def test_equals_forms(self, monkeypatch, tmp_path):
        _write_llama(monkeypatch, tmp_path,
                     {"command": "/opt/llama-server -m /m.gguf --port=12345 --host=0.0.0.0"})
        assert lc.port() == 12345
        assert lc.host() == "0.0.0.0"

    def test_missing_port_falls_back_to_default(self, monkeypatch, tmp_path):
        """行里没写 `--port` ⇒ 19878（`--host` 照常解析到，不受影响）。"""
        _write_llama(monkeypatch, tmp_path,
                     {"command": "/opt/llama-server -m /m.gguf --host 0.0.0.0"})
        assert lc.port() == 19878
        assert lc.host() == "0.0.0.0"

    @pytest.mark.parametrize("command", [
        "/opt/llama-server -m /m.gguf --port=0",     # 非正
        "/opt/llama-server -m /m.gguf --port=abc",   # 非整数
        "/opt/llama-server -m /m.gguf --port",       # flag 悬在行尾没有值
    ])
    def test_bad_port_falls_back_without_raising(self, monkeypatch, tmp_path, command):
        _write_llama(monkeypatch, tmp_path, {"command": command})
        assert lc.port() == 19878  # 绝不抛


class TestContextSizeParsing:
    """③ `-c` / `--ctx-size`：三种形态同值；缺失 / 非法 ⇒ 8192（零漂移），绝不抛。"""

    @pytest.mark.parametrize("command", [
        "/opt/llama-server -m /m.gguf -c 4096",
        "/opt/llama-server -m /m.gguf --ctx-size 4096",
        "/opt/llama-server -m /m.gguf --ctx-size=4096",
    ])
    def test_all_forms_yield_same_value(self, monkeypatch, tmp_path, command):
        _write_llama(monkeypatch, tmp_path, {"command": command})
        assert lc.context_size() == 4096
        assert lc.embed_token_limit() == 4096, "embed token 上限与行里 -c 同源"

    @pytest.mark.parametrize("command", [
        "/opt/llama-server -m /m.gguf",            # 没写 -c
        "/opt/llama-server -m /m.gguf -c 0",       # 非正
        "/opt/llama-server -m /m.gguf -c abc",     # 非整数
    ])
    def test_missing_or_bad_ctx_falls_back(self, monkeypatch, tmp_path, command):
        _write_llama(monkeypatch, tmp_path, {"command": command})
        assert lc.context_size() == 8192
        assert lc.embed_token_limit() == 8192


class TestShlexFailureFallback:
    """④ 行里引号不配对 ⇒ `shlex.split` 抛 ValueError ⇒ 退空白分词 + WARNING，`command_line()` 原样返回。"""

    LINE = '/opt/llama-server -m /m.gguf --host "1.2.3.4'

    def test_ugly_quote_falls_back_and_line_untouched(self, monkeypatch, tmp_path):
        path = _write_llama(monkeypatch, tmp_path, {"command": self.LINE})
        before = Path(path).read_bytes()
        msgs, sink_id = _capture_warning()
        try:
            argv = lc.argv()          # 绝不抛
            line = lc.command_line()
            host = lc.host()
            exe = lc.executable()
        finally:
            logger.remove(sink_id)
        assert line == self.LINE, "生效行必须一字不改地返回（程序不得加工用户行）"
        assert argv == self.LINE.split(), "shlex 失败 ⇒ 退空白分词"
        assert host == '"1.2.3.4', "未闭合引号原样留在词里（shlex 若成功会剥引号——此值只可能来自空白分词）"
        assert exe == "/opt/llama-server"
        assert any("shlex" in m for m in msgs), f"shlex 失败必须记 WARNING（不静默）：{msgs}"
        assert Path(path).read_bytes() == before, "解析失败不得把用户的行写回文件"


class TestMmprojTristate:
    """⑤ 多模态三态（从生效行判）：`--mmproj` ⇒ True；`--no-mmproj` ⇒ False；
    两者都无 ⇒ None（未声明——绝不静默当 True）；两者都出现 ⇒ 行里后出现者胜。"""

    @pytest.mark.parametrize("command, expect", [
        ("/opt/llama-server -m /m.gguf --mmproj /mm.gguf", True),
        ("/opt/llama-server -m /m.gguf --mmproj=/mm.gguf", True),
        ("/opt/llama-server -m /m.gguf --no-mmproj", False),
        # 两者都出现：行里后出现者胜（llama.cpp 的 flag 覆盖语义）
        ("/opt/llama-server -m /m.gguf --no-mmproj --mmproj /mm.gguf", True),
        ("/opt/llama-server -m /m.gguf --mmproj /mm.gguf --no-mmproj", False),
        # 关键：两者都无 ⇒ 未声明（None），绝不静默当 True
        ("/opt/llama-server -m /m.gguf", None),
    ])
    def test_tristate(self, monkeypatch, tmp_path, command, expect):
        _write_llama(monkeypatch, tmp_path, {"command": command})
        assert lc.mmproj_state() is expect, f"{command!r} ⇒ {expect!r}"


class TestExecutableAndModelPath:
    """⑥ 首词 ＝ 可执行体；`-m` / `--model`（空格与等号两形态）取值；行里没有 ⇒ None。"""

    def test_executable_is_first_token(self, monkeypatch, tmp_path):
        _write_llama(monkeypatch, tmp_path, {"command": "/opt/bin/llama-server-2 -m /m.gguf"})
        assert lc.executable() == "/opt/bin/llama-server-2"
        assert lc.executable() == lc.argv()[0]

    @pytest.mark.parametrize("command", [
        "/opt/llama-server -m /models/qwen.gguf",
        "/opt/llama-server --model=/models/qwen.gguf",
    ])
    def test_model_path_from_dash_m_and_equals(self, monkeypatch, tmp_path, command):
        _write_llama(monkeypatch, tmp_path, {"command": command})
        assert lc.model_path() == "/models/qwen.gguf"

    def test_model_path_absent_is_none(self, monkeypatch, tmp_path):
        _write_llama(monkeypatch, tmp_path, {"command": "/opt/llama-server --embedding --port 19878"})
        assert lc.model_path() is None
