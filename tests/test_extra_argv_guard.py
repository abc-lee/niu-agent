"""T1：`extra_argv` 冲突守卫（方案 §5.1 R1 P3-3，变异 V7）。

用户可经 `llama_cpp.extra_argv` 追加任意自定义启动参数（`--ngl 99`、`-dev cuda`……），
但与程序固定语义冲突的 flag（`--port/--host/-m/--mmproj/--embedding/-c/-b/-ub`）出现时
必须**剔除该 flag 及其值**（`--port=123` 等号形态一并剔除）+ WARNING——否则服务实际绑定
的端口 ≠ Python 探活 / Rust 清理所用的端口 ⇒ "等满超时 Fatal" 与 "杀不到真实实例"。
其余条目原样保留。取值接口 `llama_argv_overrides()` 供 T3 的 `_build_argv()` 使用。
注入临时 CONFIG_PATH，绝不读写真实 ~/.niu。
"""
import json

import pytest
from loguru import logger

from niu_api.internal import llama_config as lc


def _write_llama(tmp_path, **keys) -> str:
    p = tmp_path / "user-config.json"
    p.write_text(json.dumps({"llama_cpp": keys}), encoding="utf-8")
    return str(p)


def _capture_warning():
    messages: list[str] = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    return messages, sink_id


class TestConflictFlagStripped:
    """冲突 flag ⇒ 剔除（连同其值）+ WARNING；实际端口仍取配置值。"""

    def test_port_pair_stripped_and_warning_emitted(self, monkeypatch, tmp_path):
        """`["--port","12345"]` ⇒ 被剔除；配置里 port=19879 ⇒ 实际端口仍是 19879。"""
        monkeypatch.setattr("niu_api.config.CONFIG_PATH",
                           _write_llama(tmp_path, port=19879,
                                        extra_argv=["--port", "12345"]))
        msgs, sink_id = _capture_warning()
        try:
            overrides = lc.llama_argv_overrides()
            port = lc.get().port
        finally:
            logger.remove(sink_id)
        assert overrides == [], f"冲突 --port 连同其值必须剔除：{overrides}"
        assert port == 19879, "实际端口必须仍取配置值，不被 extra_argv 篡改"
        assert any("--port" in m and "[llama-config]" in m for m in msgs), (
            f"剔除必须记 WARNING（不静默）：{msgs}")

    def test_equals_form_stripped(self, monkeypatch, tmp_path):
        """`--port=12345` 等号形态同样剔除（单 token 即完整冲突项）。"""
        monkeypatch.setattr("niu_api.config.CONFIG_PATH",
                           _write_llama(tmp_path, extra_argv=["--port=12345", "--ngl", "99"]))
        msgs, sink_id = _capture_warning()
        try:
            overrides = lc.llama_argv_overrides()
        finally:
            logger.remove(sink_id)
        assert overrides == ["--ngl", "99"]
        assert any("--port=12345" in m for m in msgs), f"必须 WARNING：{msgs}"

    @pytest.mark.parametrize("flag", ["--host", "-m", "--mmproj", "-c", "-b", "-ub"])
    def test_each_value_flag_strips_its_value_token(self, monkeypatch, tmp_path, flag):
        """每个带值的冲突 flag 都把**其后一个 token（值）**一并吞掉。"""
        monkeypatch.setattr("niu_api.config.CONFIG_PATH",
                           _write_llama(tmp_path, extra_argv=[flag, "SOMEVALUE", "--ngl", "99"]))
        msgs, sink_id = _capture_warning()
        try:
            overrides = lc.llama_argv_overrides()
        finally:
            logger.remove(sink_id)
        assert overrides == ["--ngl", "99"], f"{flag} 及其值必须剔除：{overrides}"
        assert any(flag in m for m in msgs), f"{flag} 剔除必须 WARNING：{msgs}"

    def test_boolean_embedding_flag_swallows_no_value(self, monkeypatch, tmp_path):
        """`--embedding` 是布尔开关（无值）⇒ 只剔除它自己，后面的 token 保留。"""
        monkeypatch.setattr("niu_api.config.CONFIG_PATH",
                           _write_llama(tmp_path,
                                        extra_argv=["--embedding", "--ngl", "99"]))
        msgs, sink_id = _capture_warning()
        try:
            overrides = lc.llama_argv_overrides()
        finally:
            logger.remove(sink_id)
        assert overrides == ["--ngl", "99"]
        assert any("--embedding" in m for m in msgs)

    def test_trailing_value_flag_without_value_strips_itself(self, monkeypatch, tmp_path):
        """冲突 flag 出现在列表末尾、无值可吞 ⇒ 只剔除该 flag，不越界。"""
        monkeypatch.setattr("niu_api.config.CONFIG_PATH",
                           _write_llama(tmp_path, extra_argv=["--ngl", "99", "--port"]))
        assert lc.llama_argv_overrides() == ["--ngl", "99"]


class TestNonConflictingKept:
    """非冲突参数（`--ngl 99`、`-dev cuda`）原样保留、不告警。"""

    def test_ngl_pair_kept_as_is(self, monkeypatch, tmp_path):
        """`["--ngl","99"]` ⇒ 原样保留（GPU 全层 offload 正是用户想加的）。"""
        monkeypatch.setattr("niu_api.config.CONFIG_PATH",
                           _write_llama(tmp_path, extra_argv=["--ngl", "99"]))
        msgs, sink_id = _capture_warning()
        try:
            overrides = lc.llama_argv_overrides()
        finally:
            logger.remove(sink_id)
        assert overrides == ["--ngl", "99"]
        assert msgs == [], f"非冲突参数不得告警：{msgs}"

    def test_cuda_device_args_kept(self, monkeypatch, tmp_path):
        """AMD/GPU 变体常用参数（`-dev cuda` / `--rpc`）原样保留。"""
        monkeypatch.setattr("niu_api.config.CONFIG_PATH",
                           _write_llama(tmp_path,
                                        extra_argv=["-dev", "cuda", "--rpc", "host:50052"]))
        msgs, sink_id = _capture_warning()
        try:
            overrides = lc.llama_argv_overrides()
        finally:
            logger.remove(sink_id)
        assert overrides == ["-dev", "cuda", "--rpc", "host:50052"]
        assert msgs == []

    def test_mixed_list_strips_only_conflicts(self, monkeypatch, tmp_path):
        """混合列表：只剔除冲突项，其余（含紧跟冲突项之后的合法参数）全部保留。"""
        monkeypatch.setattr("niu_api.config.CONFIG_PATH",
                           _write_llama(tmp_path,
                                        extra_argv=["-m", "/tmp/x.gguf", "-b", "4096",
                                                    "--ngl", "99", "-dev", "cuda"]))
        msgs, sink_id = _capture_warning()
        try:
            overrides = lc.llama_argv_overrides()
        finally:
            logger.remove(sink_id)
        assert overrides == ["--ngl", "99", "-dev", "cuda"]
        assert len([m for m in msgs if "[llama-config]" in m]) == 2, f"两个冲突 flag 各一条 WARNING：{msgs}"

    def test_empty_extra_argv_is_empty_list(self, monkeypatch, tmp_path):
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_llama(tmp_path, extra_argv=[]))
        assert lc.llama_argv_overrides() == []

    def test_no_section_defaults_to_empty(self, monkeypatch, tmp_path):
        p = tmp_path / "user-config.json"
        p.write_text("{}", encoding="utf-8")
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", str(p))
        assert lc.llama_argv_overrides() == []
