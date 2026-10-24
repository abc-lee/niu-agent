"""Qwen3-TTS provider 后端单测（plan: docs/superpowers/plans/2026-10-21-qwen3-tts-provider.md Task 3）。

全程 mock：绝不启动真实 qwen_tts 进程（subprocess.Popen 置 boom/桩）、绝不发真实 HTTP
（_health_ok/_http_json 打桩）；HOME 一律 monkeypatch 到 tmp_path，配置/模型/日志全落临时目录。
覆盖：配置缺省与旧 enabled 忽略、路径解析、量化 flag 映射、安装判定、options 未安装不崩、
probe=true 动态音色（PM 实测 speakers 形态）、TTS_SYSTEM_ONLY 400、TTS_UNAVAILABLE 503、
成功回 WAV、reload、`--serve` 只收端口（不收 host:port）。
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from niu_api import tts_api


def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path


def _write_config(tmp_path, tts_payload):
    d = tmp_path / ".niu" / "config"
    d.mkdir(parents=True, exist_ok=True)
    (d / "user-config.json").write_text(json.dumps({"tts": tts_payload}), encoding="utf-8")


def _client():
    app = FastAPI()
    app.include_router(tts_api.router)
    return TestClient(app)


def _binary_name():
    """落盘产物名（默认解析名始终是无后缀 `qwen_tts`；Windows 上由 .exe 回落兜住）。"""
    return "qwen_tts.exe" if sys.platform == "win32" else "qwen_tts"


def _make_installed(tmp_path):
    """tmp 家目录里造出完整安装布局：可执行 binary + config.json + model.safetensors + speech_tokenizer。"""
    binary = tmp_path / ".niu" / "bin" / _binary_name()
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    mdir = tmp_path / ".niu" / "models" / "qwen3-tts" / "0.6b-customvoice"
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / "config.json").write_text("{}", encoding="utf-8")
    (mdir / "model.safetensors").write_text("w", encoding="utf-8")
    st = mdir / "speech_tokenizer"
    st.mkdir(exist_ok=True)
    (st / "model.safetensors").write_text("s", encoding="utf-8")
    return binary, mdir


class _FakeManager:
    def __init__(self, wav=b"RIFFfake-wav", unavailable=False):
        self.wav = wav
        self.unavailable = unavailable
        self.calls = []
        self.reloaded = False

    def speak(self, cfg, text, voice, rate):
        self.calls.append((text, voice, rate))
        if self.unavailable:
            raise tts_api.TtsUnavailable("mock sidecar down")
        return self.wav

    def reload(self):
        self.reloaded = True


class _StubPopen:
    """subprocess.Popen 替身：记录 argv，进程视为存活。"""

    instances = []

    def __init__(self, argv, stdout=None, stderr=None, **kwargs):
        self.argv = list(argv)
        self.kwargs = kwargs
        self.returncode = None
        _StubPopen.instances.append(self)

    def poll(self):
        return None

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


# ---------------------------------------------------------------------------
# 配置读取：缺省、旧 enabled 忽略、类型兜底
# ---------------------------------------------------------------------------

def test_defaults_when_no_config(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    cfg = tts_api.read_tts_config()
    assert cfg == {"provider": "system", "voice": "", "rate": 1.0, "qwen": tts_api.DEFAULT_QWEN}


def test_legacy_enabled_ignored_and_nested_qwen_defaults(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    _write_config(tmp_path, {
        "enabled": True,  # 旧键：一律忽略
        "provider": "QWEN3TTS",
        "voice": " vivian ",
        "rate": 0.9,
        "qwen": {"port": 18000, "bogus": 1},
    })
    cfg = tts_api.read_tts_config()
    assert "enabled" not in cfg and "enabled" not in cfg["qwen"] and "bogus" not in cfg["qwen"]
    assert cfg["provider"] == "qwen3tts"
    assert cfg["voice"] == "vivian"
    assert cfg["rate"] == 0.9
    assert cfg["qwen"]["port"] == 18000
    # 未提供的 qwen 键回缺省
    assert cfg["qwen"]["model"] == "0.6b-customvoice"
    assert cfg["qwen"]["quantization"] == "int8"
    assert cfg["qwen"]["auto_start"] is True
    assert cfg["qwen"]["idle_stop_minutes"] == 10
    assert cfg["qwen"]["fallback"] == "system"


def test_broken_config_never_raises(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    cfg_dir = tmp_path / ".niu" / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "user-config.json").write_text("{不是JSON", encoding="utf-8")
    assert tts_api.read_tts_config()["provider"] == "system"


# ---------------------------------------------------------------------------
# 路径解析与安装判定
# ---------------------------------------------------------------------------

def test_default_paths_under_home(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    q = tts_api.read_tts_config()["qwen"]
    assert tts_api.resolve_model_dir(q) == tmp_path / ".niu" / "models" / "qwen3-tts" / "0.6b-customvoice"
    # 默认名全平台 `qwen_tts`（未落盘时不加后缀，由 _resolve_existing 反向回落）
    assert tts_api.resolve_binary(q) == tmp_path / ".niu" / "bin" / "qwen_tts"


def test_explicit_paths_win(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    _write_config(tmp_path, {"qwen": {"model_dir": str(tmp_path / "elsewhere"), "binary": str(tmp_path / "my_bin")}})
    q = tts_api.read_tts_config()["qwen"]
    assert tts_api.resolve_model_dir(q) == tmp_path / "elsewhere"
    assert tts_api.resolve_binary(q) == tmp_path / "my_bin"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 默认布局的产物是 qwen_tts.exe")
def test_resolve_binary_default_resolves_exe_on_windows(tmp_path, monkeypatch):
    """默认布局只有 `qwen_tts.exe` 时，解析结果必须是 .exe（手册构建产物）。"""
    _home(tmp_path, monkeypatch)
    bindir = tmp_path / ".niu" / "bin"
    bindir.mkdir(parents=True)
    (bindir / "qwen_tts.exe").write_text("x", encoding="utf-8")
    assert tts_api.resolve_binary({"binary": ""}) == bindir / "qwen_tts.exe"


@pytest.mark.skipif(sys.platform == "win32", reason="非 Windows 无 .exe 后缀")
def test_resolve_binary_default_resolves_suffixless_off_windows(tmp_path, monkeypatch):
    """非 Windows 落盘无后缀 `qwen_tts` → 原样解析到它。"""
    _home(tmp_path, monkeypatch)
    bindir = tmp_path / ".niu" / "bin"
    bindir.mkdir(parents=True)
    (bindir / "qwen_tts").write_text("#!/bin/sh\n", encoding="utf-8")
    assert tts_api.resolve_binary({"binary": ""}) == bindir / "qwen_tts"


def test_resolve_binary_empty_name_never_raises(tmp_path, monkeypatch):
    """空文件名路径（`.`/`/`/`C:\\`）不能触发 with_name 的 ValueError（win32 回落分支）。"""
    _home(tmp_path, monkeypatch)
    for raw in (".", "/", "C:\\", "C:"):
        got = tts_api.resolve_binary({"binary": raw})
        assert isinstance(got, Path)


@pytest.mark.skipif(sys.platform != "win32", reason=".exe 回落仅 Windows")
def test_resolve_binary_explicit_suffixless_falls_back_to_exe(tmp_path, monkeypatch):
    """显式配置无后缀路径、磁盘上只有同名 .exe → 解析到 .exe（手册构建产物）。"""
    _home(tmp_path, monkeypatch)
    target = tmp_path / "bin" / "qwen_tts"
    target.parent.mkdir(parents=True)
    (target.parent / "qwen_tts.exe").write_text("x", encoding="utf-8")
    assert tts_api.resolve_binary({"binary": str(target)}) == target.with_name("qwen_tts.exe")


def test_help_probe_passes_no_window_creationflags(tmp_path, monkeypatch):
    """--help 探测在 Windows 必须带 CREATE_NO_WINDOW（打包态后端无可见控制台）。"""
    _home(tmp_path, monkeypatch)
    binary = tmp_path / "qwen_tts_probe"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)  # POSIX 上让 _binary_executable 通过；Windows 忽略
    seen = {}

    class _Help:
        stdout = b"usage: ... --int8"
        stderr = b""

    def fake_run(*args, **kwargs):
        seen.update(kwargs)
        return _Help()

    monkeypatch.setattr(tts_api.subprocess, "run", fake_run)
    assert tts_api.detect_quantizations({"binary": str(binary)}) == ["bf16", "int8"]
    if sys.platform == "win32":
        assert seen["creationflags"] == 0x08000000
    else:
        assert "creationflags" not in seen


def test_quant_args_mapping():
    assert tts_api.quant_args("bf16") == []
    assert tts_api.quant_args("int8") == ["--int8"]
    assert tts_api.quant_args("int4") == ["--int4"]
    assert tts_api.quant_args("fp8-not-a-thing") == []


def test_installed_true_with_complete_layout(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    _make_installed(tmp_path)
    assert tts_api.is_installed(tts_api.read_tts_config()["qwen"]) is True


def test_installed_false_when_pieces_missing(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    binary, mdir = _make_installed(tmp_path)
    q = tts_api.read_tts_config()["qwen"]
    (mdir / "speech_tokenizer" / "model.safetensors").unlink()
    assert tts_api.is_installed(q) is False  # speech_tokenizer 缺 → 不完整
    (mdir / "speech_tokenizer" / "model.safetensors").write_text("s", encoding="utf-8")
    if sys.platform == "win32":
        # Windows 的 os.access(X_OK) 对存在文件恒真（只认存在性）→ 删文件模拟不可执行。
        binary.unlink()
    else:
        binary.chmod(0o644)
    assert tts_api.is_installed(q) is False  # binary 不可执行 → 未安装


# ---------------------------------------------------------------------------
# GET /api/tts/options
# ---------------------------------------------------------------------------

def test_options_not_installed_never_raises(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)  # 无配置、无模型、无二进制
    r = _client().get("/api/tts/options", params={"probe": "false"})
    assert r.status_code == 200
    body = r.json()
    q = body["qwen"]
    assert body["provider"] == "system"
    assert q["installed"] is False
    assert q["health"] == "not_installed"
    assert q["voices"] == []
    assert q["quantizations"] == []
    assert q["measured"] is None
    assert [m["id"] for m in q["models"]] == list(tts_api.KNOWN_MODELS)
    assert all(m["installed"] is False for m in q["models"])
    assert body["system"]["voices"] == []


def test_options_probe_true_dynamic_voices_verified_shape(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    _make_installed(tmp_path)
    _write_config(tmp_path, {"provider": "qwen3tts", "voice": "vivian"})
    monkeypatch.setattr(tts_api, "_health_ok", lambda port: True)
    speakers = {  # PM 实测形态：{"speakers":[{"name","language","gender"}...]}
        "speakers": [
            {"name": "vivian", "language": "Chinese", "gender": "female"},
            {"name": "uncle_fu", "language": "Chinese", "gender": "male"},
        ]
    }
    monkeypatch.setattr(tts_api, "_http_json", lambda url, timeout: speakers)

    class _Help:
        stdout = b"usage: qwen_tts ... --int8 ... --int4 ..."
        stderr = b""

    monkeypatch.setattr(tts_api.subprocess, "run", lambda *a, **k: _Help())
    body = _client().get("/api/tts/options", params={"probe": "true"}).json()
    q = body["qwen"]
    assert q["installed"] is True
    assert q["health"] == "ok"
    assert q["voices"] == ["vivian", "uncle_fu"]
    assert q["languages"] == ["Chinese"]
    assert q["quantizations"] == ["bf16", "int8", "int4"]
    assert any(m["current"] and m["installed"] for m in q["models"])


def test_options_measured_from_benchmark_file(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    bench = tmp_path / ".niu" / "config" / "tts-benchmark.json"
    bench.parent.mkdir(parents=True, exist_ok=True)
    bench.write_text(json.dumps({"rtf": 2.72, "ttfa_ms": 3820, "quantization": "int8", "model": "0.6b-customvoice"}), encoding="utf-8")
    body = _client().get("/api/tts/options").json()
    assert body["qwen"]["measured"]["rtf"] == 2.72


# ---------------------------------------------------------------------------
# POST /api/tts/speak
# ---------------------------------------------------------------------------

def test_speak_system_provider_maps_400_system_only(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    _write_config(tmp_path, {"provider": "system"})
    r = _client().post("/api/tts/speak", json={"text": "你好"})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "TTS_SYSTEM_ONLY"


@pytest.mark.parametrize("payload", [
    {},
    {"text": ""},
    {"text": "   "},
    {"text": "字" * 4001},
    {"text": "你好", "rate": "fast"},
])
def test_speak_bad_request_400(tmp_path, monkeypatch, payload):
    _home(tmp_path, monkeypatch)
    _write_config(tmp_path, {"provider": "qwen3tts"})
    r = _client().post("/api/tts/speak", json=payload)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "TTS_BAD_REQUEST"


def test_speak_qwen_unavailable_maps_503(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    _write_config(tmp_path, {"provider": "qwen3tts"})
    # 隔离外部状态：机器上可能有按手册常驻的真实 sidecar 占 17987——
    # 打桩 _health_ok=False，确保走"sidecar 不可用"分支（缺 binary → TtsUnavailable），不依赖端口占用。
    monkeypatch.setattr(tts_api, "_health_ok", lambda port: False)

    def boom(*a, **k):
        raise AssertionError("绝不启动真实 sidecar 进程")

    monkeypatch.setattr(tts_api.subprocess, "Popen", boom)
    r = _client().post("/api/tts/speak", json={"text": "你好"})
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "TTS_UNAVAILABLE"


def test_speak_auto_provider_unavailable_maps_503(tmp_path, monkeypatch):
    """provider=auto：Qwen 不可用同样 503（前端同段回 system，绝不静默），不落到 system-only 分支。"""
    _home(tmp_path, monkeypatch)
    _write_config(tmp_path, {"provider": "auto"})
    # 同 test_speak_qwen_unavailable_maps_503：打桩 _health_ok=False，避免复用端口 17987 上的外部真实 sidecar。
    monkeypatch.setattr(tts_api, "_health_ok", lambda port: False)

    def boom(*a, **k):
        raise AssertionError("绝不启动真实 sidecar 进程")

    monkeypatch.setattr(tts_api.subprocess, "Popen", boom)
    r = _client().post("/api/tts/speak", json={"text": "你好"})
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "TTS_UNAVAILABLE"


def test_speak_success_returns_wav(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    _write_config(tmp_path, {"provider": "qwen3tts", "voice": "vivian", "rate": 0.9})
    fake = _FakeManager(wav=b"RIFFfake-wav")
    monkeypatch.setattr(tts_api, "manager", fake)
    r = _client().post("/api/tts/speak", json={"text": "你好", "rate": 1.5})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("audio/wav")
    assert r.content == b"RIFFfake-wav"
    # body.rate 覆盖配置 rate；voice 缺省回落配置 voice
    assert fake.calls == [("你好", "vivian", 1.5)]


def test_speak_offloads_blocking_manager_call_off_event_loop(tmp_path, monkeypatch):
    """回归（PM review）：async 路由绝不直接跑同步 manager.speak（30s+ 阻塞会卡事件循环）——
    speak 内探测 running loop：在 to_thread 工作线程里 get_running_loop 必抛 RuntimeError。"""
    _home(tmp_path, monkeypatch)
    _write_config(tmp_path, {"provider": "qwen3tts"})

    class _ProbeManager:
        def __init__(self):
            self.on_loop = None

        def speak(self, cfg, text, voice, rate):
            try:
                asyncio.get_running_loop()
                self.on_loop = True  # 还在事件循环线程 = 回归
            except RuntimeError:
                self.on_loop = False  # 已卸载到工作线程
            return b"RIFF"

    probe = _ProbeManager()
    monkeypatch.setattr(tts_api, "manager", probe)
    r = _client().post("/api/tts/speak", json={"text": "你好"})
    assert r.status_code == 200
    assert probe.on_loop is False

def test_reload_stops_manager(tmp_path, monkeypatch):
    fake = _FakeManager()
    monkeypatch.setattr(tts_api, "manager", fake)
    r = _client().post("/api/tts/reload")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert fake.reloaded is True


# ---------------------------------------------------------------------------
# QwenTtsManager 启动形态（PM 实测：--serve 只收端口；引擎监听 0.0.0.0——见模块安全注记）
# ---------------------------------------------------------------------------

def test_manager_start_argv_serves_port_not_hostport(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    _make_installed(tmp_path)
    _StubPopen.instances = []
    monkeypatch.setattr(tts_api.subprocess, "Popen", _StubPopen)
    state = {"n": 0}

    def fake_health(port):
        state["n"] += 1
        return state["n"] > 1  # 启动前探活 False（没人服务），启动等待期健康 True

    monkeypatch.setattr(tts_api, "_health_ok", fake_health)
    m = tts_api.QwenTtsManager()
    cfg = {"qwen": dict(tts_api.DEFAULT_QWEN, idle_stop_minutes=0)}
    assert m.ensure_running(cfg) == 17987
    argv = _StubPopen.instances[0].argv
    assert argv == [
        str(tmp_path / ".niu" / "bin" / _binary_name()),
        "-d", str(tmp_path / ".niu" / "models" / "qwen3-tts" / "0.6b-customvoice"),
        "--int8",
        "--serve", "17987",
    ]
    assert ":" not in argv[-1]  # `--serve` 只收端口，不是 host:port
    if sys.platform == "win32":
        assert _StubPopen.instances[0].kwargs["creationflags"] == 0x08000000  # 打包态不弹黑窗
    m.stop()


def test_manager_reuses_sidecar_already_serving_port(tmp_path, monkeypatch):
    """端口已有 Qwen 在服务（外部按手册起的）→ 复用，不重复 Popen、不抢。"""
    _home(tmp_path, monkeypatch)
    _make_installed(tmp_path)
    spawns = []
    monkeypatch.setattr(tts_api.subprocess, "Popen", lambda *a, **k: spawns.append(1))
    monkeypatch.setattr(tts_api, "_health_ok", lambda port: True)
    m = tts_api.QwenTtsManager()
    assert m.ensure_running({"qwen": dict(tts_api.DEFAULT_QWEN)}) == 17987
    assert spawns == []


def test_normalize_speakers_verified_and_bare_shapes():
    assert tts_api.normalize_speakers({"speakers": [{"name": "vivian", "language": "Chinese", "gender": "female"}]}) == ["vivian"]
    assert tts_api.normalize_speakers(["a", " b "]) == ["a", "b"]
    assert tts_api.normalize_speakers(None) == []
    assert tts_api.normalize_languages({"speakers": [{"name": "v", "language": "Chinese"}, {"name": "e", "language": "English"}]}) == ["Chinese", "English"]
