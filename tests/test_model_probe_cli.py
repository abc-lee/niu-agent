"""模型能力探测器 CLI 壳单测（组件 1，Task 2）。

覆盖：
① argparse 解析（--api-base/--model 必填；--api-type/--lightrag/--api-key 可选；
   --help 正常）
② --api-key 缺省从 user-config.json 对应段读（get_llm_config 小写键——大小写归一；
   llm 段 vs lightrag 段分流）
③ stdout 脱敏（apiKey 零出现——显式 key 与配置 key 均不出现在 stdout）
④ stdout 摘要含 ignores_unknown 字段
⑤ --lightrag 档案键 api_base|model|lightrag；llm 场景键 api_base|model|llm
⑥ 本地模型（localhost/127.0.0.1）apiKey 豁免（不读配置、置空）
⑦ 探测失败退出码 1；配置读取失败退出码 1

禁真实 LLM：patch CLI 层 probe / get_llm_config。
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import model_capability_probe as cli  # noqa: E402


def _ok_profile(**overrides):
    profile = {
        "api_base": "https://api.example.com/v1",
        "model": "m1",
        "probed_at": "2026-08-18T00:00:00",
        "probe_status": "ok",
        "ignores_unknown": False,
        "reasoning_effort": {
            "supported": ["minimal", "low", "medium", "high"],
            "unsupported": ["xhigh", "none", "max"],
        },
        "thinking": {"enabled": True, "disabled": True, "returns_reasoning_content": False},
        "response_format": {"status": "ok", "supported": ["json_object"]},
        "tools": {"status": "ok", "supported": ["probe_tool"]},
    }
    profile.update(overrides)
    return profile


def _capture_probe(monkeypatch, *, profile=None):
    """mock cli.probe，捕获调用参数。返回 (captured, set_profile)。"""
    captured = {}

    def _fake_probe(**kwargs):
        captured.update(kwargs)
        return profile if profile is not None else _ok_profile()

    monkeypatch.setattr(cli, "probe", _fake_probe)
    return captured


# ---------------------------------------------------------------------------
# ① argparse 解析
# ---------------------------------------------------------------------------


def test_argparse_required_and_optional_args():
    """--api-base/--model 必填；--api-type/--lightrag/--api-key 可选解析。"""
    args = cli.build_parser().parse_args([
        "--api-base", "https://api.example.com/v1/",
        "--model", "m1",
        "--api-type", "anthropic",
        "--lightrag",
        "--api-key", "k",
    ])
    assert args.api_base == "https://api.example.com/v1/"
    assert args.model == "m1"
    assert args.api_type == "anthropic"
    assert args.lightrag is True
    assert args.api_key == "k"


def test_argparse_requires_api_base_and_model():
    """缺 --api-base 或缺 --model → SystemExit（argparse required）。"""
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--model", "m1"])
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--api-base", "https://x"])


def test_help_exits_zero(capsys):
    """--help 正常（CLI 可独立运行验收）。"""
    with pytest.raises(SystemExit) as excinfo:
        cli.build_parser().parse_args(["--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "--api-base" in out
    assert "--lightrag" in out


# ---------------------------------------------------------------------------
# ② --api-key 缺省从 user-config.json 对应段读（键名大小写归一 + 段分流）
# ---------------------------------------------------------------------------


def test_api_key_default_from_llm_section(monkeypatch, capsys):
    """llm 场景：--api-key 缺省读 llm 段（get_llm_config 小写键 apikey）。"""
    calls = []

    def _fake_get_llm_config(use_lightrag_config=False):
        calls.append(use_lightrag_config)
        return {"apikey": "cfg-llm-secret", "apibase": "https://api.example.com/v1/",
                "model": "m1", "type": "openai"}

    monkeypatch.setattr(cli, "get_llm_config", _fake_get_llm_config)
    captured = _capture_probe(monkeypatch)

    rc = cli.main(["--api-base", "https://api.example.com/v1/", "--model", "m1"])
    assert rc == 0
    assert calls == [False], "llm 场景读 llm 段"
    assert captured["api_key"] == "cfg-llm-secret"
    assert captured["lightrag"] is False


def test_api_key_default_from_lightrag_section(monkeypatch, capsys):
    """--lightrag 场景：--api-key 缺省读 lightrag_llm 段。"""
    calls = []

    def _fake_get_llm_config(use_lightrag_config=False):
        calls.append(use_lightrag_config)
        return {"apikey": "cfg-lr-secret"}

    monkeypatch.setattr(cli, "get_llm_config", _fake_get_llm_config)
    captured = _capture_probe(monkeypatch)

    rc = cli.main(["--api-base", "https://api.example.com/v1/", "--model", "m1", "--lightrag"])
    assert rc == 0
    assert calls == [True], "lightrag 场景读 lightrag_llm 段"
    assert captured["api_key"] == "cfg-lr-secret"
    assert captured["lightrag"] is True


def test_api_key_argv_override(monkeypatch, capsys):
    """--api-key 显式传入优先于配置文件。"""
    def _fail_if_called(**kw):
        raise AssertionError("显式 --api-key 时不应读配置文件")

    monkeypatch.setattr(cli, "get_llm_config", _fail_if_called)
    captured = _capture_probe(monkeypatch)

    rc = cli.main(["--api-base", "https://api.example.com/v1/", "--model", "m1",
                   "--api-key", "argv-secret"])
    assert rc == 0
    assert captured["api_key"] == "argv-secret"


# ---------------------------------------------------------------------------
# ③ stdout 脱敏（apiKey 零出现）
# ---------------------------------------------------------------------------


def test_stdout_does_not_leak_api_key(monkeypatch, capsys):
    """stdout 脱敏：显式 key 与配置 key 均零出现（输出 JSON 不含 apiKey 字段/值）。"""
    monkeypatch.setattr(cli, "get_llm_config",
                        lambda use_lightrag_config=False: {"apikey": "cfg-secret-key-xyz"})
    _capture_probe(monkeypatch)

    rc = cli.main(["--api-base", "https://api.example.com/v1/", "--model", "m1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "cfg-secret-key-xyz" not in out
    assert "argv-secret" not in out
    assert "apiKey" not in out
    assert "apikey" not in out
    assert "api_key" not in out


def test_stdout_sanitizes_probe_exception_message(monkeypatch, capsys):
    """探测抛异常 → stdout error 脱敏（key=xxx 掩码），退出码 1。"""
    monkeypatch.setattr(cli, "get_llm_config",
                        lambda use_lightrag_config=False: {"apikey": "k"})

    def _boom(**kw):
        raise RuntimeError("connection failed?key=super-secret-token")

    monkeypatch.setattr(cli, "probe", _boom)
    rc = cli.main(["--api-base", "https://api.example.com/v1/", "--model", "m1"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "super-secret-token" not in out
    assert json.loads(out)["probe_status"] == "failed"


# ---------------------------------------------------------------------------
# ④ stdout 摘要含 ignores_unknown
# ---------------------------------------------------------------------------


def test_stdout_summary_contains_ignores_unknown(monkeypatch, capsys):
    """stdout JSON 摘要含 ignores_unknown + supported/unsupported 摘要 + 档案路径。"""
    monkeypatch.setattr(cli, "get_llm_config",
                        lambda use_lightrag_config=False: {"apikey": "k"})
    _capture_probe(monkeypatch, profile=_ok_profile(ignores_unknown=True))

    rc = cli.main(["--api-base", "https://api.example.com/v1/", "--model", "m1"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["probe_status"] == "ok"
    assert data["ignores_unknown"] is True
    assert data["reasoning_effort"]["supported"] == ["minimal", "low", "medium", "high"]
    assert data["reasoning_effort"]["unsupported"] == ["xhigh", "none", "max"]
    assert data["profile_path"].endswith("model_capabilities.json")
    assert data["profile_key"] == "https://api.example.com/v1|m1|llm"


# ---------------------------------------------------------------------------
# ⑤ 档案键（llm / lightrag 双场景）
# ---------------------------------------------------------------------------


def test_llm_scenario_profile_key_suffix(monkeypatch, capsys):
    """llm 场景档案键 api_base|model|llm。"""
    monkeypatch.setattr(cli, "get_llm_config",
                        lambda use_lightrag_config=False: {"apikey": "k"})
    _capture_probe(monkeypatch)

    rc = cli.main(["--api-base", "https://api.example.com/v1/", "--model", "m1"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["profile_key"] == "https://api.example.com/v1|m1|llm"


def test_lightrag_scenario_profile_key_suffix(monkeypatch, capsys):
    """--lightrag 档案键 api_base|model|lightrag（尾部斜杠规范化）。"""
    monkeypatch.setattr(cli, "get_llm_config",
                        lambda use_lightrag_config=False: {"apikey": "k"})
    _capture_probe(monkeypatch)

    rc = cli.main(["--api-base", "https://api.example.com/v1/", "--model", "m1", "--lightrag"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["profile_key"] == "https://api.example.com/v1|m1|lightrag"


# ---------------------------------------------------------------------------
# ⑥ 本地模型 apiKey 豁免
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("api_base", [
    "http://localhost:11434/v1",
    "http://127.0.0.1:11434/v1",
    "https://localhost:8080/v1",
])
def test_local_model_api_key_exemption(monkeypatch, capsys, api_base):
    """本地模型（localhost/127.0.0.1）免 apiKey：不读配置、probe 收到空 key。"""
    def _should_not_read_config(**kw):
        raise AssertionError("本地模型不应读配置文件获取 key")

    monkeypatch.setattr(cli, "get_llm_config", _should_not_read_config)
    captured = _capture_probe(monkeypatch)

    rc = cli.main(["--api-base", api_base, "--model", "llama3"])
    assert rc == 0
    assert captured["api_key"] == ""


# ---------------------------------------------------------------------------
# ⑦ 失败退出码
# ---------------------------------------------------------------------------


def test_failed_probe_exit_code_1(monkeypatch, capsys):
    """探测失败（probe_status=failed）→ 退出码 1，stdout 如实汇报。"""
    monkeypatch.setattr(cli, "get_llm_config",
                        lambda use_lightrag_config=False: {"apikey": "k"})
    _capture_probe(monkeypatch, profile=_ok_profile(probe_status="failed"))

    rc = cli.main(["--api-base", "https://api.example.com/v1/", "--model", "m1"])
    assert rc == 1
    data = json.loads(capsys.readouterr().out)
    assert data["probe_status"] == "failed"


def test_partial_probe_exit_code_0(monkeypatch, capsys):
    """部分成功（probe_status=partial，值域已落盘）→ 退出码 0。"""
    monkeypatch.setattr(cli, "get_llm_config",
                        lambda use_lightrag_config=False: {"apikey": "k"})
    _capture_probe(monkeypatch, profile=_ok_profile(probe_status="partial"))

    rc = cli.main(["--api-base", "https://api.example.com/v1/", "--model", "m1"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["probe_status"] == "partial"


def test_config_read_failure_exit_code_1(monkeypatch, capsys):
    """配置读取失败 → 退出码 1 + error 字段（无 key 泄漏）。"""
    def _boom(**kw):
        raise RuntimeError("permission denied")

    monkeypatch.setattr(cli, "get_llm_config", _boom)
    rc = cli.main(["--api-base", "https://api.example.com/v1/", "--model", "m1"])
    assert rc == 1
    data = json.loads(capsys.readouterr().out)
    assert data["probe_status"] == "failed"
    assert "error" in data


def test_config_read_failure_error_sanitized(monkeypatch, capsys):
    """配置读取异常信息脱敏（key 掩码）。"""
    def _boom(**kw):
        raise RuntimeError("read error?apiKey=leak-me")

    monkeypatch.setattr(cli, "get_llm_config", _boom)
    rc = cli.main(["--api-base", "https://api.example.com/v1/", "--model", "m1"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "leak-me" not in out
