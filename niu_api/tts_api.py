"""Qwen3-TTS 外挂语音 provider（spec: docs/superpowers/specs/2026-10-21-qwen3-tts-provider.md）

架构：Chat 前端 POST /api/tts/speak → 本模块代理 → 纯 C sidecar `qwen_tts --serve <port>`。
单 Python 环境：只用标准库 + httpx，绝不 import torch/transformers/numpy；
模型与二进制是用户家目录可选安装（缺省 ~/.niu/models/qwen3-tts/<model>、~/.niu/bin/qwen_tts；Windows 为 qwen_tts.exe），
永不进项目目录/安装包；未安装时 options 只报状态，不崩、不自动下载。

安全注记（PM 2026-10-21 实测真实引擎）：`--serve` **只收端口**（不收 host:port），且引擎
硬编码 INADDR_ANY 监听 0.0.0.0——sidecar 端口理论上局域网可达。本模块一律经 127.0.0.1 访问，
但不因此声称环路专用；局域网暴露由用户防火墙/信任边界负责（手册已披露）。
"""

import asyncio
import atexit
import json
import math
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from loguru import logger

router = APIRouter(prefix="/api/tts", tags=["tts"])

# sidecar 生命周期常量
SIDECAR_START_TIMEOUT = 30.0   # 启动后等 /v1/health 的最长秒数
SIDECAR_HEALTH_POLL = 0.5      # 健康轮询间隔
SIDECAR_SPEAK_TIMEOUT = 30.0   # /v1/tts 请求超时
SIDECAR_PROBE_TIMEOUT = 2.0    # /v1/speakers 只读探测超时
HELP_TIMEOUT = 5.0             # 二进制 --help 解析超时
LOG_MAX_BYTES = 5 * 1024 * 1024

MAX_TEXT = 4000                # 与前端 chat-tts-text.js MAX_TOTAL 同口径
KNOWN_MODELS = ("0.6b-customvoice", "1.7b-customvoice")
KNOWN_QUANTS = ("bf16", "int8", "int4")
QUANT_FLAGS = {"bf16": (), "int8": ("--int8",), "int4": ("--int4",)}

DEFAULT_QWEN = {
    "model": "0.6b-customvoice",
    "quantization": "int8",
    "model_dir": "",
    "binary": "",
    "port": 17987,
    "auto_start": True,
    "idle_stop_minutes": 10,
    "fallback": "system",
}


def _popen_flags() -> dict:
    """平台相关 Popen/run 参数：Windows 隐藏控制台窗口（打包态后端无可见控制台，
    否则侧车/--help 子进程会新建控制台宿主）。"""
    if sys.platform == "win32":
        return {"creationflags": 0x08000000}  # CREATE_NO_WINDOW
    return {}


class TtsUnavailable(Exception):
    """Qwen sidecar 不可用（未安装/无法启动/health 超时/代理请求失败）→ 路由层映射 503。"""


# ---------------------------------------------------------------------------
# 配置：每次调用现读 ~/.niu/config/user-config.json 顶层 tts 段；
# 缺文件/坏 JSON/类型错一律回缺省，绝不抛（与 media_ingest_config 同契约）。
# 旧 `enabled` 键继续忽略（与 main.js get-tts-config 一致）。
# ---------------------------------------------------------------------------

def _user_config_path() -> Path:
    return Path.home() / ".niu" / "config" / "user-config.json"


def _benchmark_path() -> Path:
    return Path.home() / ".niu" / "config" / "tts-benchmark.json"


def _clamp_rate(value, default: float = 1.0) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(n):
        return default
    return min(2.0, max(0.5, n))


def read_tts_config() -> dict:
    cfg = {"provider": "system", "voice": "", "rate": 1.0, "qwen": dict(DEFAULT_QWEN)}
    try:
        data = json.loads(_user_config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg
    if not isinstance(data, dict):
        return cfg
    raw = data.get("tts")
    if not isinstance(raw, dict):
        return cfg
    provider = raw.get("provider")
    if isinstance(provider, str) and provider.strip().lower() in ("system", "qwen3tts", "auto"):
        cfg["provider"] = provider.strip().lower()
    voice = raw.get("voice")
    if isinstance(voice, str):
        cfg["voice"] = voice.strip()
    cfg["rate"] = _clamp_rate(raw.get("rate"), 1.0)
    q = raw.get("qwen")
    if isinstance(q, dict):
        merged = dict(DEFAULT_QWEN)
        for key in ("model", "quantization", "model_dir", "binary", "fallback"):
            v = q.get(key)
            if isinstance(v, str) and v.strip():
                merged[key] = v.strip()
        port = q.get("port")
        if isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535:
            merged["port"] = port
        if isinstance(q.get("auto_start"), bool):
            merged["auto_start"] = q["auto_start"]
        idle = q.get("idle_stop_minutes")
        if isinstance(idle, (int, float)) and not isinstance(idle, bool) and idle >= 0:
            merged["idle_stop_minutes"] = float(idle)
        if merged["quantization"].lower() not in QUANT_FLAGS:
            merged["quantization"] = DEFAULT_QWEN["quantization"]
        cfg["qwen"] = merged
    return cfg


# ---------------------------------------------------------------------------
# 路径解析与安装判定（家目录可选安装；model_dir/binary 留空 = 默认布局）
# ---------------------------------------------------------------------------

def resolve_model_dir(qwen: dict) -> Path:
    d = str(qwen.get("model_dir") or "").strip()
    if d:
        return Path(d).expanduser()
    return Path.home() / ".niu" / "models" / "qwen3-tts" / str(qwen.get("model") or DEFAULT_QWEN["model"])


def resolve_binary(qwen: dict) -> Path:
    """解析引擎二进制路径。Windows 只认 `.exe`（无后缀写法一律补 `.exe`）；
    非 Windows 用无后缀。不做磁盘探测/回落。"""
    b = str(qwen.get("binary") or "").strip()
    if b:
        p = Path(b).expanduser()
        if sys.platform == "win32" and p.name and not p.suffix:
            p = p.with_name(p.name + ".exe")
        return p
    name = "qwen_tts.exe" if sys.platform == "win32" else "qwen_tts"
    return Path.home() / ".niu" / "bin" / name


def quant_args(quantization) -> list[str]:
    """量化 → sidecar 启动 flag：bf16 无 flag（默认）；int8 → --int8；int4 → --int4。"""
    return list(QUANT_FLAGS.get(str(quantization).strip().lower(), ()))


def _binary_executable(binary: Path) -> bool:
    try:
        return binary.is_file() and os.access(binary, os.X_OK)
    except OSError:
        return False


def _model_complete(model_dir: Path) -> bool:
    try:
        return (
            model_dir.is_dir()
            and (model_dir / "config.json").is_file()
            and (model_dir / "model.safetensors").is_file()
            and (model_dir / "speech_tokenizer" / "model.safetensors").is_file()
        )
    except OSError:
        return False


def is_installed(qwen: dict) -> bool:
    return _binary_executable(resolve_binary(qwen)) and _model_complete(resolve_model_dir(qwen))


# ---------------------------------------------------------------------------
# sidecar HTTP（PM 实测形态：/v1/health 200 {"status":"ok",...}；
# /v1/speakers {"speakers":[{"name","language","gender"}, ...]}）
# ---------------------------------------------------------------------------

def _http_json(url: str, timeout: float):
    try:
        r = httpx.get(url, timeout=timeout)
        if r.status_code != 200:
            return None
        return r.json()
    except (httpx.HTTPError, OSError, ValueError):
        return None


def _health_ok(port: int) -> bool:
    """探活：GET http://127.0.0.1:<port>/v1/health 返回 200 + JSON。"""
    return _http_json(f"http://127.0.0.1:{port}/v1/health", 1.0) is not None


def normalize_speakers(data) -> list[str]:
    """规整 /v1/speakers 结果为音色名列表（实测 dict 包裹 list[dict]；容忍 list[str]）。"""
    if isinstance(data, dict):
        data = data.get("speakers", data.get("voices"))
    if not isinstance(data, list):
        return []
    out: list[str] = []
    for item in data:
        if isinstance(item, str):
            name = item
        elif isinstance(item, dict):
            name = item.get("name") or item.get("speaker") or item.get("id")
        else:
            name = None
        if isinstance(name, str) and name.strip():
            out.append(name.strip())
    return out


def normalize_languages(data) -> list[str]:
    if isinstance(data, dict):
        data = data.get("speakers", data.get("voices"))
    if not isinstance(data, list):
        return []
    out: list[str] = []
    for item in data:
        lang = item.get("language") if isinstance(item, dict) else None
        if isinstance(lang, str) and lang.strip() and lang.strip() not in out:
            out.append(lang.strip())
    return out


def detect_quantizations(qwen: dict) -> list[str]:
    """从二进制 --help 解析 --int8/--int4（bf16 无 flag = 默认），不硬编码未来新增项。
    二进制缺失/不可执行 → []；--help 跑不动（但二进制在）→ 回已知三项 KNOWN_QUANTS。"""
    binary = resolve_binary(qwen)
    if not _binary_executable(binary):
        return []
    try:
        proc = subprocess.run(
            [str(binary), "--help"], capture_output=True, timeout=HELP_TIMEOUT, **_popen_flags()
        )
        text = ((proc.stdout or b"") + (proc.stderr or b"")).decode("utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return list(KNOWN_QUANTS)
    out = ["bf16"]
    for flag, name in (("--int8", "int8"), ("--int4", "int4")):
        if flag in text:
            out.append(name)
    return out


# ---------------------------------------------------------------------------
# QwenTtsManager：单例进程代理——懒启动、单实例锁、端口已有 Qwen 在服务则复用（不抢）、
# 空闲 idle_stop_minutes 自动停、进程输出追加写 ~/.niu/logs/qwen-tts.log（>5MB 先截断）。
# ---------------------------------------------------------------------------

class QwenTtsManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._log_file = None
        self._owned = False
        self._idle_minutes = 0.0
        self._last_used = 0.0
        self._watchdog: threading.Thread | None = None

    # ---- 日志 ----
    def _log_path(self) -> Path:
        return Path.home() / ".niu" / "logs" / "qwen-tts.log"

    def _open_log(self):
        p = self._log_path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            mode = "wb" if (p.exists() and p.stat().st_size > LOG_MAX_BYTES) else "ab"
            return open(p, mode)
        except OSError:
            return None

    def _close_log(self):
        if self._log_file is not None:
            try:
                self._log_file.close()
            except OSError:
                pass
            self._log_file = None

    # ---- 生命周期 ----
    def ensure_running(self, cfg: dict) -> int:
        """返回可用 sidecar 端口；未安装/起不来/health 超时 → TtsUnavailable。"""
        q = cfg["qwen"]
        port = int(q["port"])
        with self._lock:
            self._last_used = time.monotonic()
            if _health_ok(port):
                # 本进程拉起的在服务，或端口已被（外部启动的）Qwen sidecar 占用 → 复用，不重复起、不杀
                return port
            if self._owned and self._proc is not None:
                if self._proc.poll() is None:
                    # 自己拉起的进程仍在加载模型 → 继续等健康，绝不双开
                    return self._wait_healthy_locked(port)
                self._terminate_locked()
            return self._start_locked(q, port)

    def _start_locked(self, q: dict, port: int) -> int:
        binary = resolve_binary(q)
        model_dir = resolve_model_dir(q)
        if not _binary_executable(binary):
            raise TtsUnavailable(f"qwen_tts 二进制缺失或不可执行: {binary}")
        if not _model_complete(model_dir):
            raise TtsUnavailable(
                "模型目录不完整（需 config.json + model.safetensors + speech_tokenizer/model.safetensors）: "
                f"{model_dir}"
            )
        # `--serve` 只收端口（PM 实测）；引擎硬编码 INADDR_ANY 监听 0.0.0.0——局域网暴露风险见模块 docstring
        argv = [str(binary), "-d", str(model_dir), *quant_args(q.get("quantization")), "--serve", str(port)]
        self._log_file = self._open_log()
        try:
            self._proc = subprocess.Popen(
                argv, stdout=self._log_file, stderr=subprocess.STDOUT, **_popen_flags()
            )
        except OSError as e:
            self._close_log()
            raise TtsUnavailable(f"sidecar 启动失败: {e}") from e
        self._owned = True
        self._idle_minutes = float(q.get("idle_stop_minutes") or 0)
        logger.info(f"[TTS] sidecar 启动: {' '.join(argv)}（日志 {self._log_path()}）")
        self._start_watchdog_locked()
        return self._wait_healthy_locked(port)

    def _wait_healthy_locked(self, port: int) -> int:
        deadline = time.monotonic() + SIDECAR_START_TIMEOUT
        while time.monotonic() < deadline:
            if _health_ok(port):
                self._last_used = time.monotonic()
                return port
            if self._proc is not None and self._proc.poll() is not None:
                code = self._proc.returncode
                self._terminate_locked()
                raise TtsUnavailable(f"sidecar 提前退出（exit={code}），详见 {self._log_path()}")
            time.sleep(SIDECAR_HEALTH_POLL)
        self._terminate_locked()
        raise TtsUnavailable(f"sidecar /v1/health 超时（{SIDECAR_START_TIMEOUT}s）")

    def _start_watchdog_locked(self):
        if self._idle_minutes <= 0 or self._watchdog is not None:
            return
        self._watchdog = threading.Thread(target=self._watchdog_loop, name="qwen-tts-idle", daemon=True)
        self._watchdog.start()

    def _watchdog_loop(self):
        while True:
            time.sleep(5.0)
            with self._lock:
                if not self._owned or self._proc is None:
                    self._watchdog = None
                    return
                if self._proc.poll() is not None:
                    self._terminate_locked()
                    self._watchdog = None
                    return
                idle = time.monotonic() - self._last_used
                if self._idle_minutes > 0 and idle >= self._idle_minutes * 60:
                    logger.info(f"[TTS] sidecar 空闲 {int(idle)}s，自动停止（idle_stop_minutes={self._idle_minutes}）")
                    self._terminate_locked()
                    self._watchdog = None
                    return

    def _terminate_locked(self):
        proc = self._proc
        self._proc = None
        self._owned = False
        self._close_log()
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
        except Exception as e:  # 永不抛：停止路径不得失败
            logger.warning(f"[TTS] sidecar 停止异常（忽略）: {e}")

    def stop(self):
        with self._lock:
            self._terminate_locked()
            self._watchdog = None

    def reload(self):
        """配置变更（量化/模型/端口）后调用：停 sidecar 并清状态，下次 speak 按最新配置重启。"""
        self.stop()

    # ---- 代理 ----
    def _touch(self):
        with self._lock:
            self._last_used = time.monotonic()

    def speak(self, cfg: dict, text: str, voice: str, rate: float) -> bytes:
        """POST /v1/tts JSON {text, speaker, language:'Chinese', rate} → WAV bytes。
        voice 为空时不带 speaker 键，由引擎自选默认音色。"""
        port = self.ensure_running(cfg)
        payload = {"text": text, "language": "Chinese", "rate": rate}
        if voice:
            payload["speaker"] = voice
        try:
            r = httpx.post(f"http://127.0.0.1:{port}/v1/tts", json=payload, timeout=SIDECAR_SPEAK_TIMEOUT)
        except (httpx.HTTPError, OSError) as e:
            raise TtsUnavailable(f"/v1/tts 请求失败: {e}") from e
        if r.status_code != 200:
            raise TtsUnavailable(f"/v1/tts 返回 {r.status_code}")
        data = r.content
        if not data:
            raise TtsUnavailable("/v1/tts 返回空音频")
        self._touch()
        return data


manager = QwenTtsManager()
atexit.register(manager.stop)


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

def _model_entry(model_id: str, path: Path, current: bool) -> dict:
    installed = _model_complete(path)
    size = 0
    try:
        if path.is_dir():
            for root, _dirs, files in os.walk(path):
                for f in files:
                    try:
                        size += (Path(root) / f).stat().st_size
                    except OSError:
                        pass
    except OSError:
        pass
    return {"id": model_id, "path": str(path), "installed": installed, "sizeBytes": size, "current": current}


def _models_list(qwen: dict) -> list[dict]:
    base = Path.home() / ".niu" / "models" / "qwen3-tts"
    known = [base / mid for mid in KNOWN_MODELS]
    configured = resolve_model_dir(qwen)
    entries = [_model_entry(mid, p, current=(p == configured)) for mid, p in zip(KNOWN_MODELS, known)]
    if configured not in known:
        entries.append(_model_entry(str(qwen.get("model") or DEFAULT_QWEN["model"]), configured, current=True))
    return entries


def _measured():
    """最近一次基准（~/.niu/config/tts-benchmark.json）；缺失/坏 → None。非永久真值。"""
    try:
        data = json.loads(_benchmark_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


@router.get("/options")
def tts_options(probe: bool = False):
    """只读探测：安装态/模型/量化/动态音色/健康/最近基准。缺配置缺模型绝不抛。

    voices 仅在 sidecar 正在服务、或 probe=true 且已安装（auto_start=true 才真启动）时返回；
    auto_start=false 时 options 只报状态、不启动。"""
    cfg = read_tts_config()
    q = cfg["qwen"]
    port = int(q["port"])
    installed = is_installed(q)
    voices: list[str] = []
    languages: list[str] = []
    if not installed:
        health = "not_installed"
    else:
        alive = _health_ok(port)
        if not alive and probe and q.get("auto_start", True):
            try:
                port = manager.ensure_running(cfg)
                alive = True
            except TtsUnavailable as e:
                logger.warning(f"[TTS_OPTIONS] probe 启动 sidecar 失败: {e}")
        health = "ok" if alive else "not_started"
        if alive:
            speakers = _http_json(f"http://127.0.0.1:{port}/v1/speakers", SIDECAR_PROBE_TIMEOUT)
            if speakers is not None:
                voices = normalize_speakers(speakers)
                languages = normalize_languages(speakers)
    return {
        "provider": cfg["provider"],
        "voice": cfg["voice"],
        "rate": cfg["rate"],
        "qwen": {
            "installed": installed,
            "binary": str(resolve_binary(q)),
            "model": q["model"],
            "quantization": q["quantization"],
            "port": port,
            "models": _models_list(q),
            "quantizations": detect_quantizations(q),
            "voices": voices,
            "languages": languages,
            "health": health,
            "measured": _measured(),
        },
        "system": {"voices": []},
    }


@router.post("/speak")
async def tts_speak(request: Request):
    """合成一条语音，成功返回 audio/wav。

    provider=system → 400 TTS_SYSTEM_ONLY（前端 speechSynthesis 自己读）；
    provider=qwen3tts/auto 且 sidecar 不可用 → 503 TTS_UNAVAILABLE（前端同段回落系统语音，不静默）。"""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"code": "TTS_BAD_REQUEST", "message": "请求体必须是 JSON 对象"})
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail={"code": "TTS_BAD_REQUEST", "message": "请求体必须是 JSON 对象"})
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(status_code=400, detail={"code": "TTS_BAD_REQUEST", "message": "text 不能为空"})
    if len(text) > MAX_TEXT:
        raise HTTPException(status_code=400, detail={"code": "TTS_BAD_REQUEST", "message": f"text 最长 {MAX_TEXT} 字"})
    voice = body.get("voice")
    if voice is not None and not isinstance(voice, str):
        raise HTTPException(status_code=400, detail={"code": "TTS_BAD_REQUEST", "message": "voice 必须是字符串"})
    cfg = read_tts_config()
    rate = cfg["rate"]
    if body.get("rate") is not None:
        r = body["rate"]
        if isinstance(r, bool) or not isinstance(r, (int, float)) or not math.isfinite(float(r)):
            raise HTTPException(status_code=400, detail={"code": "TTS_BAD_REQUEST", "message": "rate 必须是 0.5–2.0 的数字"})
        rate = min(2.0, max(0.5, float(r)))
    if cfg["provider"] == "system":
        raise HTTPException(
            status_code=400,
            detail={"code": "TTS_SYSTEM_ONLY", "message": "provider=system：朗读由前端 speechSynthesis 完成，不经后端"},
        )
    speak_voice = (voice or "").strip() or cfg["voice"]
    try:
        # sidecar 调用是同步阻塞（ensure_running 最长 30s + httpx.post 30s）——
        # 必须 to_thread 卸载，否则卡死 FastAPI 事件循环
        wav = await asyncio.to_thread(manager.speak, cfg, text, speak_voice, rate)
    except TtsUnavailable as e:
        raise HTTPException(status_code=503, detail={"code": "TTS_UNAVAILABLE", "message": str(e)})
    return Response(content=wav, media_type="audio/wav")


@router.post("/reload")
def tts_reload():
    """配置（量化/模型/端口）变更后重启 sidecar 的唯一入口：停进程清状态，下次 /speak 按新配置懒启动。
    Niu Agent 写完 user-config.json 调这里即可，不重启 Niu。"""
    manager.reload()
    return {"ok": True}
