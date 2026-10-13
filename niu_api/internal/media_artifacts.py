"""EmbeddingGemma 2 的**媒体**产物（mmproj）—— 家目录解析与就绪检查（方案 D-11）。

**严禁复用 `embedding_artifacts.models_dir()` / `embedding.py` 的模型目录解析函数**
（I-11）：它缺省回落**项目根 `models/`**（`embedding.py:84-89`），运行时那是
**只读的 .app bundle**，且 `NIU_MODELS_PATH` 在生产环境无人设置（只有测试设）
⇒ 复用会静默失败/污染 bundle（审查 B 取证）。mmproj 由本工程自己的家目录解析
函数求值：`~/.niu/models/`，**不进打包**（P11：DMG/7z 不加 529 MB），安装与
开关都由主 Agent 按手册执行（P12）。
"""
from __future__ import annotations

from pathlib import Path

# 产物文件名与 sha256（C11 溯源闭环：远端 hf-mirror 的 x-linked-etag 与本地逐字一致）。
MMPROJ_FILENAME = "mmproj-embeddinggemma-2-Q8_0.gguf"
MMPROJ_SHA256 = "c4a8a52691ecef40618438928bdf9e68379b854e24166f292592353db0aab64f"

# 下载源（与文本 GGUF 同仓同源，单一来源；换源需同步修订 C11 溯源结论）。
_MMPROJ_SOURCES: tuple[str, ...] = (
    f"https://hf-mirror.com/ggml-org/embeddinggemma-2-GGUF/resolve/main/{MMPROJ_FILENAME}",
)


def media_models_dir() -> Path:
    """mmproj 的家目录根：`~/.niu/models`（调用期求值；**不读 NIU_MODELS_PATH**，D-11）。"""
    return Path.home() / ".niu" / "models"


def mmproj_path() -> Path:
    """mmproj 产物的家目录路径（调用期求值，测试可 monkeypatch `Path.home`）。"""
    return media_models_dir() / MMPROJ_FILENAME


def mmproj_download_urls() -> list[str]:
    return list(_MMPROJ_SOURCES)


def media_artifacts_ready() -> bool:
    """mmproj 是否就位。"""
    return mmproj_path().is_file()


def describe_missing_media() -> str:
    """mmproj 缺失时的可行动文案（fail-loud：指名具体路径与校验依据，绝不静默，D-7/R3）。"""
    path = mmproj_path()
    if path.is_file():
        return ""
    return (
        f"llama-server 无法加载 mmproj：媒体产物缺失 {path}\n"
        f"安装：从 {mmproj_download_urls()[0]} 下载后放到该路径"
        f"（sha256 必须为 {MMPROJ_SHA256}），然后按手册重启 llama-server。"
    )
