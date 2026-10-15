"""EmbeddingGemma 2 的**媒体**产物（mmproj）—— 家目录解析与就绪检查（方案 D-11 / §4'.3）。

**家目录解析与 `embedding_artifacts.models_dir()` 隔离**（I-11）：`get_models_dir()` 缺省
回落**项目根 `models/`**（`embedding.py:84-89`），运行时那是**只读的 .app bundle**，且
`NIU_MODELS_PATH` 在生产环境无人设置（只有测试设）⇒ 复用会静默失败/污染 bundle（审查 B
取证）。mmproj 由本工程自己的家目录解析函数求值：`~/.niu/models/`，**不进打包**（P11：
DMG/7z 不加 529 MB），安装与开关都由主 Agent 按手册执行（P12）。

**程序零下载**（方案 §4'.1，v1.2 口径）：本模块**不出现任何 URL / 下载哈希常量 / 下载函数**——
获取产物是**手册的事**（主 Agent 按《系统管理手册》『可选组件安装』的兼容性要求自行获取）。
对 `embedding_artifacts` 的引用仅限**布局事实读取**（`_layout_raw()` 取 mmproj 文件名，
单源真相 D-5）与**配置覆盖读取**（`_model_path_override()` 读 `llama_cpp.mmproj_path`），
**不得**复用其 `models_dir()` / `embedding.py` 的模型目录解析（I-11 禁令不变）。

**模型文件可配（v1.7）**：`llama_cpp.mmproj_path` 非空 ⇒ 指向用户自备的任意量化/任意
位置（官方仓库有 BF16/Q8_0 两档，社区还有 Q6/Q5/Q4 等）；不配 ⇒ 家目录布局缺省
（`~/.niu/models/<布局数据 mmproj.filename>`，D-11 语义不变）。
"""
from __future__ import annotations

from pathlib import Path

from niu_api.internal import embedding_artifacts as _ea

# mmproj 的**缺省**产物名（布局数据 `mmproj.filename` 缺失时的回退；是产品自身稳定的
# 摆放名，不是上游资产名/构建号）。布局数据在时以数据为准（`mmproj_filename()`）。
MMPROJ_FILENAME = "mmproj-embeddinggemma-2-Q8_0.gguf"


def media_models_dir() -> Path:
    """mmproj 的家目录根：`~/.niu/models`（调用期求值；**不读 NIU_MODELS_PATH**，D-11）。"""
    return Path.home() / ".niu" / "models"


def mmproj_filename() -> str:
    """mmproj 产物文件名——布局数据 `mmproj.filename` 优先（用户层可覆盖），
    布局数据缺失时回退产品默认名。"""
    v = ((_ea._layout_raw() or {}).get("mmproj") or {}).get("filename")
    return v if isinstance(v, str) and v else MMPROJ_FILENAME


def mmproj_path() -> Path:
    """mmproj 产物路径（调用期求值，测试可 monkeypatch `Path.home`）。

    `llama_cpp.mmproj_path` 非空 ⇒ 直接返回它（用户自备任意量化版本、任意位置，
    不拼家目录布局）；为空 ⇒ 家目录布局缺省 `~/.niu/models/<mmproj_filename()>`。
    """
    override = _ea._model_path_override("mmproj_path")
    if override:
        return Path(override)
    return media_models_dir() / mmproj_filename()


def media_artifacts_ready() -> bool:
    """mmproj 是否就位（跟随 `mmproj_path()`：用户自备路径时按用户路径判）。"""
    return mmproj_path().is_file()


def describe_missing_media() -> str:
    """mmproj 缺失时的可行动文案（fail-loud，绝不静默，D-7/R3）。

    硬约束（方案 §4'.3 / R3 P2-1）：只含**缺哪个文件 + 兼容性要求**（方案 §4'.2 的
    平台无关口径，简述）+ **见《系统管理手册》『可选组件安装』**；**不得**出现 URL /
    哈希值 / 构建号 / 平台名 / 脚本名——获取产物是手册的事（§4'.1），程序零下载。
    产物齐备时返回空串。
    """
    path = mmproj_path()
    if path.is_file():
        return ""
    return (
        f"llama-server 无法加载 mmproj：媒体产物缺失 {path}\n"
        "兼容性要求（平台无关）：所用的 llama-server 必须识别 --mmproj 参数（自检办法见手册），"
        "且 mmproj 与 GGUF 权重须同属 EmbeddingGemma 2 模型家族，否则多模态嵌入会错位。\n"
        "获取与自检办法见《系统管理手册》『可选组件安装』。"
    )
