"""EmbeddingGemma 2 产物解析与缺失检测 —— 产物位置的唯一真相源（S1，方案 §4'）。

只回答三件事：产物在哪、缺没缺、缺的时候怎么说人话。
**不含加载与编码**（那是 llama-server 后端的职责），且**零下载能力**（方案 §4'.1：
程序不做下载，获取产物是《系统管理手册》『可选组件安装』节的事）。

布局事实（目录名 / 各平台二进制名 / GGUF 文件名与体积 / mmproj 文件名）全部来自
数据文件，不在代码里：程序**只读家目录那份** `~/.niu/llama-layout.json`——出厂缺省由
启动器首启时从包内只读种子 `memory/llama-layout.<os>.json` 拷入（不在才拷、在则绝不
覆盖）；程序永不读包内原件（除启动器复制时）、永不写包内。

消费方：
- 运行时缺失自检（llama-server 拉起前，缺失时经 describe_missing() 写 .startup_error）
- 手册自检命令（manual-troubleshooting.md：describe_missing / verify_gguf / gguf_path）

无 torch / 无网络 / 无模型加载。经 `get_models_dir` 传递依赖 numpy、loguru（niu_api
既有硬依赖，非新增）。**故障路径**：布局数据缺失不致命——WARNING 后按内置回退布局名
继续派生路径（缺文件清单仍可读），只有"产物真缺"才产出 Fatal 文案。
"""

from __future__ import annotations

import json
import os
import platform
import sys
from pathlib import Path

from loguru import logger

from niu_api.internal.embedding import get_models_dir

# ---------------- 布局数据（"用"产物所需的事实，不是下载规格） ----------------

_LAYOUT_FILENAME = "llama-layout.json"

# 布局数据缺失/坏掉时的回退布局事实——仅在该降级态生效，防止"清单文件丢了就什么
# 都算不出"。这些是产品自身稳定的摆放名（不含上游构建号/压缩包名）。
_FALLBACK_DIRNAME = "llama.cpp"
_FALLBACK_GGUF_FILENAME = "embeddinggemma-2-Q8_0.gguf"

# 《系统管理手册》『可选组件安装』对 llama.cpp 的兼容性要求（方案 §4'.2，完整 6 条，
# 平台无关）——缺失文案只含"缺哪些文件 + 这些要求 + 手册指引"，不提平台名/构建号/
# 脚本名/启动方式。
_COMPAT_REQUIREMENTS: tuple[str, ...] = (
    "支持嵌入模式：认 --embedding；POST /v1/embeddings 接受 "
    '{"input":[...],"encoding_format":"float"} 并返回 data[].embedding（条数与输入等长）',
    "带多模态（MTMD）：认 --mmproj 与 --image-max-tokens；能接受图片输入（data: base64）",
    "构建够新：能加载 EmbeddingGemma 2 的 GGUF；认 --no-mmproj",
    'GET /health 返回 JSON 且 status 为 "ok"（只回 200 不算就绪——看的是字段）',
    "POST /tokenize 返回 tokens 数组（超限截断护栏的备用计量通道）",
    "认基础 flag：-m / --port / --host / -c / -b / -ub",
)


class UnsupportedPlatformError(NotImplementedError):
    """OS 层真不支持（非 darwin/win32）——显式失败，禁止静默回退到错误架构的二进制。

    平台键是否在布局数据声明的清单里是另一层校验：布局数据在而未声明该平台 ⇒
    仅 WARNING、按派生路径继续；布局数据缺 ⇒ 跳过校验（WARNING 由布局加载处发出）。
    """


# 布局文件进程内缓存（仅当家目录文件 stat 变化才重读）+ 每进程一次告警
_LAYOUT_CACHE: dict | None = None
_LAYOUT_CACHE_KEY: tuple | None = None
_WARNED_REASONS: set[str] = set()


def _warn_once(reason: str, msg: str) -> None:
    if reason not in _WARNED_REASONS:
        _WARNED_REASONS.add(reason)
        logger.warning(msg)


def _layout_path() -> Path:
    """家目录布局文件：`~/.niu/llama-layout.json`（启动器首启时从包内只读种子
    `memory/llama-layout.<os>.json` 拷入的出厂缺省）。程序只读这份，永不读包内原件、
    永不写包内。函数级 expanduser 是测试注入点（HOME 重定向即可隔离）。"""
    return Path(os.path.expanduser("~")) / ".niu" / _LAYOUT_FILENAME


def _layout_stat_key(p: Path) -> tuple:
    try:
        st = p.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return ()


def _layout_raw() -> dict | None:
    """读家目录布局文件（`~/.niu/llama-layout.json`——启动器首启拷入的出厂缺省）。

    缺失 ⇒ WARNING（每进程一次）并返回 None——调用方按回退布局降级，绝不抛、绝不
    Fatal（"布局文件丢了" ≠ "产物缺了"）；解析失败/非 dict 同按缺失降级。稳态进程内
    缓存：仅当文件 stat 变化才重读。
    """
    global _LAYOUT_CACHE, _LAYOUT_CACHE_KEY
    p = _layout_path()
    key = _layout_stat_key(p)
    if key == _LAYOUT_CACHE_KEY:
        return _LAYOUT_CACHE
    data: dict | None = None
    try:
        parsed = json.loads(p.read_text(encoding="utf-8"))
    except OSError:
        parsed = None
    except ValueError:
        _warn_once(f"parse:{p.name}", f"布局文件 {p} 解析失败，按缺失降级")
        parsed = None
    if isinstance(parsed, dict):
        data = parsed
    elif parsed is not None:
        _warn_once(f"type:{p.name}", f"布局文件 {p} 不是 JSON 对象，按缺失降级")
    if data is None:
        _warn_once(
            "no-layout",
            "布局文件缺失（~/.niu/llama-layout.json 未找到）：产物路径按内置回退名派生。"
            "启动器首启应已从 memory/llama-layout.<os>.json 拷入出厂缺省；"
            "请按《系统管理手册》『可选组件安装』补齐布局与产物",
        )
    _LAYOUT_CACHE_KEY = key
    _LAYOUT_CACHE = data
    return _LAYOUT_CACHE


# ---------------- 布局事实访问器 ----------------


def layout_dirname() -> str:
    """产物目录名（布局数据 dirname；数据缺失时回退 llama.cpp）。"""
    v = (_layout_raw() or {}).get("dirname")
    return v if isinstance(v, str) and v else _FALLBACK_DIRNAME


def gguf_filename() -> str:
    """GGUF 权重文件名（布局数据 gguf.filename；数据缺失时回退产品默认名）。"""
    v = ((_layout_raw() or {}).get("gguf") or {}).get("filename")
    return v if isinstance(v, str) and v else _FALLBACK_GGUF_FILENAME


def supported_platforms() -> list[str]:
    """布局数据声明的平台键清单（布局数据缺失 ⇒ 空清单，调用方跳过成员校验）。"""
    plats = (_layout_raw() or {}).get("platforms") or {}
    return sorted(plats) if isinstance(plats, dict) else []


# ---------------- 平台与路径 ----------------


def current_platform() -> str:
    """返回 `<sys.platform>-<arch>` 平台键——纯由 OS/架构派生，不依赖任何布局数据。

    OS 层非 darwin/win32 必须抛错（UnsupportedPlatformError）：静默回退会让 x64
    机器拿 arm64 二进制，报错信息难归因。平台键是否在布局数据清单里由调用路径
    另行校验（未声明仅 WARNING，不抛）。
    """
    if sys.platform not in ("darwin", "win32"):
        raise UnsupportedPlatformError(
            f"EmbeddingGemma 2 后端未适配操作系统 {sys.platform!r}（仅 darwin/win32）"
        )
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    return f"{sys.platform}-{arch}"


def models_dir() -> Path:
    """模型根目录（沿用既有 NIU_MODELS_PATH 机制）。"""
    return get_models_dir()


def _platform_declared() -> bool:
    """当前平台键是否在布局数据声明的清单里（布局缺失 ⇒ 视同已声明，告警已在加载处发）。"""
    layout = _layout_raw()
    if layout is None:
        return True
    plats = layout.get("platforms") or {}
    if not isinstance(plats, dict):
        return True
    key = current_platform()
    if key not in plats:
        _warn_once(
            f"platform:{key}",
            f"布局数据未声明平台 {key!r}：按其派生路径继续（自备 GPU/AMD 构建请改配置 llama_cpp.command 首词）",
        )
        return False
    return True


def platform_dir() -> Path:
    """当前平台的 llama.cpp 产物目录：`<models>/<dirname>/<platform>/`。"""
    _platform_declared()
    return models_dir() / layout_dirname() / current_platform()


def gguf_path() -> Path:
    """GGUF 权重路径——布局派生 `<models>/<gguf 文件名>`（布局数据缺失时用产品默认名，
    D-4 零缺省漂移）。换模型文件（官方 BF16/Q8_0 两档、社区 Q6/Q5/Q4 等任意量化）＝
    改配置里那一行 `llama_cpp.command` 的 `-m` 指向；本模块只管布局派生路径。
    """
    return models_dir() / gguf_filename()


def llama_server_name() -> str:
    """llama-server 可执行文件名——布局数据优先，**调用期**求值。

    布局数据声明的当前平台二进制名为准；布局缺失/未声明时按 OS 回退（Windows
    为 .exe）。不得做成 import 期常量：测试需 monkeypatch `sys.platform` 验证
    Windows 分支，import 期冻结的值无法被回头重绑。
    """
    layout = _layout_raw()
    plats = (layout or {}).get("platforms") or {}
    entry = plats.get(current_platform()) if isinstance(plats, dict) else None
    name = entry.get("binary") if isinstance(entry, dict) else None
    if isinstance(name, str) and name:
        return name
    return "llama-server.exe" if sys.platform == "win32" else "llama-server"


def llama_needs_exec_bit() -> bool:
    """当前平台的二进制是否需要显式赋予可执行位（Windows 无此概念）。

    方案 §4'.3 死链裁决③：重定义为 `sys.platform != "win32"`——不再依赖已删除的
    资产表（可执行位是 OS 语义，与布局数据无关）。
    """
    return sys.platform != "win32"


def llama_server_path() -> Path:
    """llama-server 二进制路径——布局派生 `<models>/<dirname>/<platform>/<二进制名>`
    （调用期求值）。换 GPU/AMD 构建＝改配置里那一行 `llama_cpp.command` 的首词；
    本模块只管布局派生路径。
    """
    return platform_dir() / llama_server_name()


# ---------------- 缺失检测与文案 ----------------


def missing_artifacts() -> list[Path]:
    """当前平台缺失的必需产物；顺序固定：权重在前、二进制在后。

    只检 GGUF 权重 + llama-server 二进制（mmproj 由 media_artifacts 单独管，不折入）；
    二进制**总是**来自布局（换 GPU/AMD 构建＝改配置 `llama_cpp.command` 首词），
    所以**总是**检查它。
    """
    missing: list[Path] = []
    if not gguf_path().is_file():
        missing.append(gguf_path())
    if not llama_server_path().is_file():
        missing.append(llama_server_path())
    return missing


def artifacts_ready() -> bool:
    return not missing_artifacts()


def describe_missing() -> str:
    """把缺失产物写成人读、可行动的文案（供 llama-server 拉起前写 .startup_error）。

    硬约束：每条必须指名**具体文件路径**（布局数据缺失时按回退名派生，清单仍可读）；
    只含"缺哪些文件 + 兼容性要求（方案 §4'.2 六条）+ 见《系统管理手册》『可选组件安装』"，
    不提平台名/构建号/脚本名/启动方式。产物齐备时返回空串。
    首行与 launcher 的 MISSING_ARTIFACTS_HANDLE 字节级一致（改首行必须同步 Rust）。
    """
    missing = missing_artifacts()
    if not missing:
        return ""
    lines = ["向量模型产物缺失，程序无法启动。缺少："]
    lines.extend(f"  - {p}" for p in missing)
    lines.append("对 llama.cpp 的兼容性要求（平台无关，须同时满足；自检办法见手册）：")
    lines.extend(f"  {n}. {req}" for n, req in enumerate(_COMPAT_REQUIREMENTS, 1))
    lines.append("获取与自检办法见《系统管理手册》『可选组件安装』。")
    return "\n".join(lines)


def verify_gguf(path: Path) -> bool:
    """GGUF 完整性粗校（方案 v1.7：体积校验可选化）。

    缺省判定＝**文件存在 + 可读 + 大小 > 0** ⇒ 可用。期望体积取自布局数据 `gguf.size`：
    与实得不符**仅 WARNING**（可能换了量化、或文件不完整）后**仍返回可用**——绝不因用户
    自备了不同量化版本而判"残缺"；布局数据里 `size` 为 null ⇒ **关掉**期望体积比对
    （静默跳过）。仅当文件缺失/不可读/零字节时判不可用。
    """
    try:
        if not path.is_file():
            return False
        actual = path.stat().st_size
    except OSError:
        return False
    if actual <= 0:
        return False
    size = None
    layout = _layout_raw()
    if layout is None:
        _warn_once("gguf-size", "布局数据缺失：verify_gguf 无期望体积可比对，仅按存在且非空判定")
    else:
        cand = ((layout.get("gguf") or {}).get("size"))
        if isinstance(cand, int) and not isinstance(cand, bool) and cand > 0:
            size = cand
    if size is not None and actual != size:
        logger.warning(
            "GGUF 体积不符（实际 {}，布局数据期望 {}）：可能换了量化、或文件不完整；"
            "仍按可用处理，请按手册自检办法确认", actual, size)
    return True
