"""EmbeddingGemma 2 产物解析与缺失检测 —— 产物位置的唯一真相源（S1）。

只回答三件事：产物在哪、缺没缺、缺的时候怎么说人话。
**不含加载与编码**（那是 S2 的 llama.cpp 后端职责）。

消费方：
- `scripts/package_all_dependencies.py`（供模型：往这些路径填产物）
- 运行时缺失自检（S2 接入：缺失时经 `_handle_embedding_preload_failure` 写 .startup_error）

无 torch / 无网络 / 无模型加载。注意：本模块经 `get_models_dir` 传递依赖
`niu_api.internal.embedding` 的模块级 import（numpy、loguru），二者本是 niu_api 的既有硬依赖，
不新增额外依赖；**但不等于零依赖**，供模型脚本在无 numpy/loguru 的环境仍会 ImportError。
"""

from __future__ import annotations

import platform
import sys
from pathlib import Path

from niu_api.internal.embedding import get_models_dir

# ---------------- 产物单一真相源 ----------------

GGUF_FILENAME = "embeddinggemma-2-Q8_0.gguf"
GGUF_SIZE = 309_855_456  # 字节；hf-mirror x-linked-size，2026-10-10 实测
GGUF_HF_REPO = "ggml-org/embeddinggemma-2-GGUF"

LLAMA_CPP_BUILD = "b11540"  # llama.cpp nightly tag，2026-10-10 实测含 gemma-embedding2
LLAMA_CPP_DIRNAME = "llama.cpp"

# 平台键 -> (发布资产名, 是否需要赋予可执行位)
# tarball/zip 内二进制统一位于单一顶层目录 llama-<build>/
_PLATFORM_ASSETS: dict[str, tuple[str, bool]] = {
    "darwin-x64": (f"llama-{LLAMA_CPP_BUILD}-bin-macos-x64.tar.gz", True),
    "darwin-arm64": (f"llama-{LLAMA_CPP_BUILD}-bin-macos-arm64.tar.gz", True),
    "win32-x64": (f"llama-{LLAMA_CPP_BUILD}-bin-win-cpu-x64.zip", False),
}

# 二进制下载源候选（按序尝试）：GitHub 直连优先，第三方镜像仅作回落。
# 单列第三方代理有供应链风险，故不作为唯一来源。
_LLAMA_SOURCES: tuple[str, ...] = (
    "https://github.com/ggml-org/llama.cpp/releases/download/",
    "https://gh-proxy.com/https://github.com/ggml-org/llama.cpp/releases/download/",
)

_GGUF_SOURCES: tuple[str, ...] = (
    f"https://hf-mirror.com/{GGUF_HF_REPO}/resolve/main/{GGUF_FILENAME}",
)


class UnsupportedPlatformError(NotImplementedError):
    """平台不在 _PLATFORM_ASSETS 中 —— 显式失败，禁止静默回退到错误架构的二进制。"""


def current_platform() -> str:
    """返回 `<sys.platform>-<arch>` 平台键。

    未适配平台必须抛错：静默回退会让 x64 机器拿 arm64 二进制，
    报错信息难归因（"file not found" 而非"架构不符"）。
    """
    if sys.platform not in ("darwin", "win32"):
        raise UnsupportedPlatformError(
            f"EmbeddingGemma 2 后端未适配平台 {sys.platform!r}；"
            f"已适配：{sorted(_PLATFORM_ASSETS)}"
        )
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    key = f"{sys.platform}-{arch}"
    if key not in _PLATFORM_ASSETS:
        raise UnsupportedPlatformError(f"未适配的平台组合 {key!r}；已适配：{sorted(_PLATFORM_ASSETS)}")
    return key


def models_dir() -> Path:
    """模型根目录（沿用既有 NIU_MODELS_PATH 机制）。"""
    return get_models_dir()


def platform_dir() -> Path:
    """当前平台的 llama.cpp 产物目录：`<models>/llama.cpp/<platform>/`。"""
    return models_dir() / LLAMA_CPP_DIRNAME / current_platform()


def gguf_path() -> Path:
    return models_dir() / GGUF_FILENAME


def llama_server_name() -> str:
    """llama-server 可执行文件名 —— **调用期**求值。

    不得做成 import 期常量：测试需 monkeypatch `sys.platform` 来验证 Windows
    分支，import 期冻结的常量无法被回头重绑，会让该测试在 macOS 上恒失败
    （实测教训：R1 双审两角独立命中）。
    """
    return "llama-server.exe" if sys.platform == "win32" else "llama-server"


def llama_server_path() -> Path:
    return platform_dir() / llama_server_name()


def llama_asset_name() -> str:
    """当前平台应下载的发布资产名。"""
    return _PLATFORM_ASSETS[current_platform()][0]


def llama_needs_exec_bit() -> bool:
    """当前平台的二进制是否需要显式赋予可执行位（Windows 无此概念）。"""
    return _PLATFORM_ASSETS[current_platform()][1]


def llama_download_urls() -> list[str]:
    asset = llama_asset_name()
    return [f"{base}{LLAMA_CPP_BUILD}/{asset}" for base in _LLAMA_SOURCES]


def gguf_download_urls() -> list[str]:
    return list(_GGUF_SOURCES)


def missing_artifacts() -> list[Path]:
    """当前平台缺失的必需产物；顺序固定：权重在前、二进制在后。"""
    missing: list[Path] = []
    if not gguf_path().is_file():
        missing.append(gguf_path())
    if not llama_server_path().is_file():
        missing.append(llama_server_path())
    return missing


def artifacts_ready() -> bool:
    return not missing_artifacts()


def describe_missing() -> str:
    """把缺失产物写成人读、可行动的文案（供 S2 写进 .startup_error）。

    硬约束：每条必须指名**具体文件路径**；禁止"模型缺失"这类无法行动的话。
    产物齐备时返回空串。
    """
    missing = missing_artifacts()
    if not missing:
        return ""
    lines = ["向量模型产物缺失，程序无法启动。缺少："]
    lines.extend(f"  - {p}" for p in missing)
    lines.append(
        "获取方式：运行 scripts/package_all_dependencies.py"
        f"（下载 GGUF 权重与 llama.cpp {LLAMA_CPP_BUILD} 预编译二进制），"
        "或手动把文件放到上述路径。"
    )
    return "\n".join(lines)


def verify_gguf(path: Path) -> bool:
    """按体积粗校 GGUF（防下到半截/错误文件）。

    只校验字节数，不做哈希——镜像站给出的 x-linked-size 已可拦截绝大多数
    下载残损；强校验留给后续如需引入时再加，不在 S1 扩大范围。
    """
    try:
        return path.is_file() and path.stat().st_size == GGUF_SIZE
    except OSError:
        return False
