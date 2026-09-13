"""
vision-server — 屏幕截图 + 识图 MCP 服务器（可视化功能 plan v0.5.2 §4-V5 /
2026-09-11-vision-channel-refactor.md）

三工具：
- list_targets：无参，列出当前可截取目标（显示器 + 窗口清单，含前台应用行与
  48 个窗口截断警告）——截图前先调用它拿窗口编号。
- screenshot：niu_natives DesktopSession capture（desktop/window_id/region
  三形态）→ 降采样 ≤1280 宽 → 落盘 ~/.niu/tmp/screenshot_<ts>.png → 返回
  **纯绝对路径** + 尺寸/显示器元数据（不返回图标记——与用户发图同形；
  要理解画面内容调 analyze_image）。
- analyze_image(image_path, question)：把指定图片 + 提示词送进视觉模型，
  返回**文字答案**。模型内部自选（主模型优先：主模型有视觉 → 用主模型；
  否则用 vision_llm 段；皆无 → 明确错误含配置指引）。不依赖 niu_natives。

桌面操作由内置 `computer` 工具承担（agent/computer，对象模型）——本模块为它
提供共享底座：_get_session（DesktopSession 单例，帧缓存/AX ref 登记都在其中）、
_save_png、_format_result 与识图降级链（_vision_chain/_call_with_fallback/analyze_image）。

D-D：screenshot/analyze_image 是基础工具与视觉能力无关——visibility: static
无条件直挂主 Agent（yaml 显式 static，register_server 默认 hidden）；子 Agent
经 frontmatter `mcpServers: [vision-server]` 声明即用。

niu_natives import 降级（R11）：模块级 try/except——失败 → DesktopSession=None
+ logger.warning，screenshot 返回明确错误串，服务器正常加载（.so 缺失/跨平台
未编不得炸 Niu 启动——REQUIRED_SERVERS __import__ 失败会 RuntimeError 终止）。
"""

from __future__ import annotations

import httpx
import json
import math
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

# 独立 MCP server（python -m niu_vision_server）运行时把仓库根加入 sys.path，
# 使仓库根下的一方包可直接 import。
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..'))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from loguru import logger

# ============== niu_natives 降级导入（R11） ==============
# .so 缺失/跨平台未编（第一期只编 macOS+Windows）→ ImportError；任何加载异常
# 都走同一降级路径——模块照常可 import，screenshot 返回明确错误串。

try:
    import niu_natives
except Exception as e:  # ImportError 或 pyo3 初始化失败等
    niu_natives = None
    logger.warning(f"[vision-server] niu_natives 不可用，screenshot 降级为错误提示: {e}")

# DesktopSession 实例惰性创建（首次 screenshot 调用时）——import 期不启动
# native worker 线程（REQUIRED_SERVERS 启动即 import 全部模块）。
_session = None
# 单例创建锁：主 Agent 的 computer run 与异步子 Agent 的 MCP screenshot 可能
# **并发首触**——无锁 check-then-set 会创建两个 DesktopSession，败者连同其原生
# worker 线程泄漏，且帧缓存 / AX ref 登记分裂（InvalidCoordinateFrame/StaleRef）。
_session_lock = threading.Lock()

# 降采样上限：≤1280 宽（plan §4-V5；多模态 token 成本与视觉模型输入限制）
MAX_WIDTH = 1280

_UNAVAILABLE_MSG = "截图能力不可用（niu_natives 未安装/平台不支持）"

# 窗口列表硬上限副本——锚定 Rust 出处：niu-natives/src/desktop/{macos,win32}/capture.rs
# 的 MAX_LISTED_WINDOWS = 48。Rust 侧达到上限后静默 break（无截断标志），Python
# 侧只能以 len(windows) >= 48 启发式检测并显式告知 Agent，否则它以为屏幕上就这些。
MAX_LISTED_WINDOWS = 48


def _get_session():
    """惰性获取/创建 DesktopSession 单例（线程安全）。

    double-checked locking：无锁读快路径，未创建才持 `_session_lock` 二次判空后
    创建——并发首触时所有调用者拿到同一实例（构造恰好一次）。不变式：每次调用
    返回同一对象；不在导入期创建（避免启动期副作用）。
    """
    global _session
    if niu_natives is None:
        return None
    if _session is None:
        with _session_lock:
            if _session is None:
                _session = niu_natives.DesktopSession()
    return _session


def _tmp_dir() -> Path:
    """截图落盘目录 ~/.niu/tmp（不存在则创建）。"""
    d = Path.home() / ".niu" / "tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_png(png_bytes: bytes) -> Path:
    """PNG 落盘 ~/.niu/tmp/screenshot_<时间戳>.png，返回绝对路径。

    时间戳精确到毫秒（LLM 重试循环同秒连拍不互相覆盖）。
    """
    now = time.time()
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(now)) + f"_{int((now % 1) * 1000):03d}"
    out_path = _tmp_dir() / f"screenshot_{stamp}.png"
    out_path.write_bytes(png_bytes)
    return out_path


def _format_result(out_path: Path, result: dict) -> str:
    """组装返回文本：`截图已保存: <绝对路径>` + 尺寸/显示器元数据（纯路径，无图标记）。

    路径独占首行——与用户发图同形（裸路径）；要理解画面内容调 analyze_image。
    """
    w, h = result.get("width"), result.get("height")
    sw, sh = result.get("source_width"), result.get("source_height")
    meta = f"尺寸: {w}x{h}"
    if (sw, sh) != (w, h) and None not in (sw, sh):
        meta += f"（原始 {sw}x{sh}，已降采样）"
    backend = result.get("backend") or "unknown"
    displays = result.get("displays") or []
    meta += f" · 后端: {backend} · 显示器: {len(displays)} 台"
    return f"截图已保存: {out_path}\n{meta}"


# ============== 工具实现 ==============


def _resolve_region_ratio(session, region_ratio):
    """把 `[左,上,右,下]`（0~1，恒相对整个逻辑桌面）换算为绝对逻辑坐标。

    无状态纯函数——不依赖「上一次截图」：以 list_displays() 求所有显示器合成
    的逻辑范围（min_x/min_y + 宽高，与 capture_displays 的 min_x/min_y 口径一致），
    再 x = min_x + left*W、y = min_y + top*H、width = (right-left)*W、
    height = (bottom-top)*H。调用前已完成形态/越界校验（plan §3.2）。

    成功返回 (x, y, width, height)；list_displays() 抛错/空列表、或据显示器字段
    求逻辑范围失败（防御：真实 PyO3 字段恒在）→ 均返回明确错误串
    （非 _UNAVAILABLE_MSG——那是「niu_natives 缺失」专属语义，误用会让 Agent
    以为视觉能力整体损坏）。
    """
    try:
        displays = session.list_displays()
        if not displays:
            return ("无法枚举显示器：系统未报告任何活动显示器"
                    "（请检查显示连接或录屏权限后重试）")
        left, top, right, bottom = region_ratio
        min_x = min(_target_field(d, "x") for d in displays)
        min_y = min(_target_field(d, "y") for d in displays)
        max_x = max(_target_field(d, "x") + _target_field(d, "width") for d in displays)
        max_y = max(_target_field(d, "y") + _target_field(d, "height") for d in displays)
        w = max_x - min_x
        h = max_y - min_y
    except Exception as e:
        logger.warning(f"[vision-server] screenshot list_displays failed: {e}")
        return f"无法枚举显示器：{e}（请检查录屏/屏幕录制权限后重试）"
    return (min_x + left * w, min_y + top * h, (right - left) * w, (bottom - top) * h)


def _is_valid_ratio(v):
    """region_ratio 单项校验：必须是 0~1 的有限数值。

    先做范围比较（int 不转 float，超大整数在此短路），再判有限性
    （NaN/inf/-inf 与任何数比较恒 False，须靠 isfinite 兜住）。
    绝不抛异常——超大 int（如 10**400）会使 isfinite 内部转换抛 OverflowError。
    """
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    if v < 0 or v > 1:
        return False
    try:
        return math.isfinite(v)
    except (OverflowError, TypeError, ValueError):
        return False


def screenshot(target: str = "screen", window_id=None, x=None, y=None, width=None,
               height=None, region_ratio=None) -> str:
    """截取屏幕画面，落盘 PNG 并返回纯路径 + 尺寸/显示器元数据（要理解内容调 analyze_image）。

    三形态映射（plan §4-V5）：
    - target=screen  → capture("desktop")
    - target=window  → capture(window_id)——窗口 ID 来自 list_windows
      （X11/Win32/macOS 为数字，Wayland 为 atspi 字符串）
    - target=region  → capture("desktop", region=(x, y, width, height))
      ——逻辑桌面坐标（左上角原点），裁剪先于降采样

    region_ratio（plan §3.2）：`[左,上,右,下]` 4 个 0~1 数值，恒相对整个逻辑桌面
    （= target=screen 那张图），无状态纯函数——换算成绝对坐标后走同一 capture 路径。

    全部形态统一 caps={"max_width": 1280} 降采样。
    """
    if niu_natives is None:
        return _UNAVAILABLE_MSG

    target = str(target or "screen").strip().lower()
    if target not in ("screen", "window", "region"):
        return f"错误：target 必须是 screen/window/region 之一（收到 {target!r}）"

    session = _get_session()
    if session is None:
        return _UNAVAILABLE_MSG

    # ---- region_ratio 校验（plan §3.2）：全部在调用 capture 前完成，
    # 报错返回明确中文串、不抛异常（穿透到 MCP 层会暴露英文原始异常）。----
    has_abs_coords = any(v is not None for v in (x, y, width, height))
    if region_ratio is not None and target != "region":
        return (f'错误：region_ratio 只在 target="region" 时有效（当前 target={target!r}），'
                '请改 target="region"')
    if region_ratio is not None and has_abs_coords:
        return ("错误：region_ratio 与 x/y/width/height 互斥，只能二选一"
                "（推荐 region_ratio）")
    if target == "region":
        if region_ratio is None:
            if not has_abs_coords or any(v is None for v in (x, y, width, height)):
                return ("错误：target=region 需要指定区域，二选一："
                        "region_ratio=[左,上,右,下]（0~1 比例，相对整个逻辑桌面，推荐）"
                        "或 x/y/width/height（逻辑桌面绝对坐标，四个须齐全）")
        else:
            if (not isinstance(region_ratio, (list, tuple)) or len(region_ratio) != 4
                    or any(not _is_valid_ratio(v) for v in region_ratio)):
                return ("错误：region_ratio 必须是恰好 4 项的数值序列 [左,上,右,下]，"
                        "每项为 0~1 之间的有限数值（示例：[0.3, 0.2, 0.6, 0.5]）")
            left, top, right, bottom = region_ratio
            if (any(v < 0 or v > 1 for v in (left, top, right, bottom))
                    or left >= right or top >= bottom):
                return ("错误：region_ratio 每项须在 0~1 之间，且 左<右、上<下"
                        f"（收到 [左,上,右,下] = {list(region_ratio)!r}）")

    # region_ratio → 绝对逻辑坐标（无状态纯函数；失败返回明确错误串）
    abs_region = None
    if target == "region" and region_ratio is not None:
        abs_region = _resolve_region_ratio(session, region_ratio)
        if isinstance(abs_region, str):
            return abs_region

    caps = {"max_width": MAX_WIDTH}
    try:
        if target == "screen":
            result = session.capture("desktop", caps)
        elif target == "window":
            if window_id in (None, ""):
                return "错误：target=window 需要 window_id 参数（窗口 ID）"
            result = session.capture(str(window_id), caps)
        else:  # region
            result = session.capture(
                "desktop", caps, abs_region or (x, y, width, height))
    except Exception as e:
        logger.warning(f"[vision-server] screenshot failed (target={target}): {e}")
        return f"截图失败：{e}"

    png_bytes = result.get("png_bytes") if isinstance(result, dict) else None
    if not png_bytes:
        return "截图失败：未返回图片数据"

    try:
        out_path = _save_png(png_bytes)
    except Exception as e:
        logger.warning(f"[vision-server] screenshot 落盘失败: {e}")
        return f"截图失败（落盘）：{e}"

    text = _format_result(out_path, result)
    if target == "region":
        # 事实性提示：区域图是裁剪图，其像素坐标与整屏指针输入坐标系不同。
        text += ("\n注意：该图是屏幕区域裁剪图，其像素坐标不能用作指针输入坐标"
                 "（指针输入需同 target 的整屏截图 target=screen）")
    return text


def _target_field(obj, name, default=None):
    """读取 DesktopWindow/DesktopDisplay 字段。

    底层是 PyO3 对象（属性访问）；**刻意不做 dict 兼容**——测试 fake 必须
    还原真实类型形态，否则 mock 与生产不一致时会静默漏测（历史 P0 教训）。
    """
    return getattr(obj, name, default)


def list_targets() -> str:
    """列出当前可截取的所有目标：显示器 + 窗口（截图前先调用拿窗口编号）。

    降级分级（plan §3.1——三条互斥，不得混用）：
    - niu_natives 缺失 → _UNAVAILABLE_MSG（该常量仅此一档使用）
    - list_displays() 抛错/空列表 → 明确错误串（非 _UNAVAILABLE_MSG——那是
      「整体能力不可用」语义，会误导 Agent 放弃视觉能力）
    - list_windows() 抛错 → 明确错误串（同上）
    - 零窗口 = 正常态（停在桌面时 Finder 桌面元素被 ExcludeDesktopElements
      排除，列表可为空）→ 显示器照列 + 「窗口 0 个」+ 仍可截整屏提示

    focused 是 PID 级语义（同 App 多窗口全部为 True）——窗口行标记用
    [应用在前台]；无任何窗口 focused 时省略「前台应用」行（不输出 None）。
    """
    if niu_natives is None:
        return _UNAVAILABLE_MSG

    session = _get_session()
    if session is None:
        return _UNAVAILABLE_MSG

    try:
        try:
            displays = session.list_displays()
        except Exception as e:
            logger.warning(f"[vision-server] list_targets list_displays failed: {e}")
            return f"无法枚举显示器：{e}（请检查录屏/屏幕录制权限后重试）"
        if not displays:
            return "无法枚举显示器：系统未报告任何活动显示器（请检查显示连接或录屏权限后重试）"

        try:
            windows = session.list_windows()
        except Exception as e:
            logger.warning(f"[vision-server] list_targets list_windows failed: {e}")
            return f"无法枚举窗口：{e}（请检查录屏/屏幕录制权限后重试）"

        lines: List[str] = []

        # 前台应用行（focused 的 app 去重取第一个；无 focused 窗口则整行省略）
        front_apps: List[str] = []
        for w in windows:
            if _target_field(w, "focused"):
                app = str(_target_field(w, "app") or "")
                if app and app not in front_apps:
                    front_apps.append(app)
        if front_apps:
            lines.append(f"前台应用: {front_apps[0]}")

        # 显示器段：序号 + 名称 + 逻辑尺寸 + 缩放 + 逻辑位置 + (主屏)
        lines.append(f"显示器 {len(displays)} 台：")
        for i, d in enumerate(displays, start=1):
            line = (
                f"- [{i}] {_target_field(d, 'name')} "
                f"{_target_field(d, 'width')}x{_target_field(d, 'height')}"
                f" (缩放{_target_field(d, 'scale')}) 逻辑位置 ({_target_field(d, 'x')},{_target_field(d, 'y')})"
            )
            if _target_field(d, "is_primary"):
                line += " (主屏)"
            lines.append(line)

        # 窗口段：id 原样输出（opaque，禁止解析/换算）
        lines.append(f"窗口 {len(windows)} 个：")
        for w in windows:
            title = str(_target_field(w, "title") or "").strip() or "(无标题)"
            line = (
                f"- id={_target_field(w, 'id')} {_target_field(w, 'app')} \"{title}\""
                f" {_target_field(w, 'width')}x{_target_field(w, 'height')}"
                f" @({_target_field(w, 'x')},{_target_field(w, 'y')})"
            )
            if _target_field(w, "focused"):
                line += " [应用在前台]"
            lines.append(line)

        # 尾注：零窗口 = 正常态提示；达上限 = 截断警告（Rust 侧静默 break）
        if len(windows) == 0:
            lines.append('（当前无窗口可截取，仍可 screenshot(target="screen") 截整屏）')
        elif len(windows) >= MAX_LISTED_WINDOWS:
            lines.append(
                f'⚠️ 已达显示上限 {MAX_LISTED_WINDOWS} 个，可能还有更多窗口未列出：'
                '可改用 screenshot(target="screen")\n'
                "   看整屏，或请用户关闭部分窗口后重试。"
            )

        return "\n".join(lines)
    except Exception as e:
        # 兜底：任何未预期异常不得穿透到 MCP 层（handler 会暴露英文原始异常）
        logger.warning(f"[vision-server] list_targets failed: {e}")
        return f"列出可截取目标失败：{e}"


# ============== analyze_image（plan 2026-09-11-vision-channel-refactor §3.2 /
#                 2026-09-11-vision-model-fallback 链式选模 + 自动降级） ==============

# --- 错误分类表（D-D；语义与 litellm_adapter.py:87-107 对齐，但自带字符串元组——
# 不 import 适配层私有常量，避免依赖私有 API；R-8）---
# F-8（2026-09-12 用户定案）：本层不再重试任何错误——重试由底层 SDK 执行。
# 以下分类结果仅供 stopped 判定消费（retryable/fatal/unknown 不再驱动任何控制流，也不进文案）。
_VISION_RETRYABLE_EXC = ("RateLimitError", "ServiceUnavailableError")
_VISION_FATAL_EXC = ("AuthenticationError", "PermissionDeniedError",
                     "BudgetExceededError", "ContentPolicyViolationError")

# 文本兜底关键词（小写匹配）：F-1 两类文本信号（忙/暂不可用） / 认证·欠费·配额类
_VISION_RETRY_HINTS = ("retry after", "try again in", "rate limit",
                       "overloaded", "too many requests",
                       "loading model", "service unavailable", "temporarily unavailable")
_VISION_FATAL_HINTS = ("authentication", "unauthorized", "invalid api key",
                       "permission denied", "forbidden", "quota", "billing",
                       "payment required", "insufficient balance", "credit",
                       "401", "402", "403")

# 总预算（D-F）：每次新调用（每模型一查）前检查累计耗时，超此值停止降级并返回「预算中断」文案；
# 预算不 gate in-flight 调用（已发出的请求不打断，read_timeout 默认 300s）。
_VISION_CHAIN_BUDGET_SECONDS = 600

# D-E 汇总文案中每条原因的长度上限
_VISION_REASON_MAX_CHARS = 120

# --- F-5（2026-09-12 用户定案）：多模型链的「最近成功模型」记忆 ---
# 纯进程内（不落盘、重启即忘）；30 分钟内有效；**仅多模型链使用**（单模型不做任何记忆逻辑）。
_VISION_SUCCESS_TTL_SECONDS = 30 * 60
_VISION_LAST_SUCCESS: Dict[str, Any] = {}   # {"model": <模型名>, "ts": <time.monotonic()>}


def _remembered_start(chain: List[dict]) -> int:
    """F-5：本轮起点下标。命中「30 分钟内成功过的模型」→ 该模型下标；否则 0（链首）。
    单模型链（len < 2）恒 0；无记忆/ts 缺失/超 TTL/名字不在链中 → 0。"""
    if len(chain) < 2:
        return 0
    model = str(_VISION_LAST_SUCCESS.get("model") or "")
    ts = _VISION_LAST_SUCCESS.get("ts") or 0
    if not model or (time.monotonic() - ts) > _VISION_SUCCESS_TTL_SECONDS:
        return 0
    for i, cfg in enumerate(chain):
        if str(cfg.get("model") or "") == model:
            return i
    return 0


def _remember_vision_success(chain: List[dict], name: str) -> None:
    """F-5：仅多模型链记录本次成功的模型。"""
    if len(chain) > 1:
        _VISION_LAST_SUCCESS["model"] = name
        _VISION_LAST_SUCCESS["ts"] = time.monotonic()


def _forget_vision_success(chain: List[dict]) -> None:
    """F-5：一轮全失败 → 清记忆（下次仍从链首重新开始）。仅多模型链。"""
    if len(chain) > 1:
        _VISION_LAST_SUCCESS.clear()


def _load_image_data_uri(image_path: str):
    """读图片文件 → data URI（复用 agent.image_channel helper：魔数 MIME 探测 /
    >4MB 降采样）。失败（缺文件/非图/超限且降采样失败）→ None。

    函数级 import（plan R5——独立 MCP server 进程与 Niu 主进程同构，仓库既有先例）。
    """
    from agent.image_channel import _image_to_data_uri
    return _image_to_data_uri(image_path)


def _normalize_vision_node(node: dict, main_cfg: dict) -> dict:
    """归一化 vision_llm 链节/单对象段（D-A / R-2：独立重实现 get_llm_config 的空键继承，
    仅 5 个键——避免为单点扩公共 API 造成回归面）。

    空键继承主 llm 段：apiKey/apiBase/type/provider/litellm_kwargs；
    **max_tokens 不继承**：省略即不下发，由模型/服务端自决（写死小值会让思考型模型
    推理链耗尽预算 → 正文空 + finish_reason=length）。
    reasoning_effort 缺省 ""。节自己的 model 保留（model 空的节由调用方过滤，
    **不**继承主 llm model——那会变成「拿主模型当视觉模型」的静默错误端点）。
    返回与 get_llm_config 同形的小写键 dict（_call_vision_model 直接可消费）。
    """
    cfg = {str(k).lower(): v for k, v in node.items()}
    if not cfg.get("apikey"):
        cfg["apikey"] = main_cfg.get("apikey", "")
    if not cfg.get("apibase"):
        cfg["apibase"] = main_cfg.get("apibase", "")
    if not cfg.get("type"):
        cfg["type"] = main_cfg.get("type", "openai")
    if not cfg.get("provider"):
        cfg["provider"] = main_cfg.get("provider", "")
    if not cfg.get("litellm_kwargs"):
        cfg["litellm_kwargs"] = main_cfg.get("litellm_kwargs", {})
    if not cfg.get("reasoning_effort"):
        cfg["reasoning_effort"] = ""
    return cfg


def _vision_chain():
    """构建视觉模型链（D-A / D-C）→ (chain: list[dict], skipped: int)。

    - 主模型有视觉（main_has_vision，fail-closed 三条件）→ 链首 = 主 llm 段
      （C-1 用户已拍板：主模型入链，失败自动降级到 vision_llm.models）；
    - vision_llm.models 非空数组 → 逐节归一化追加（空键继承主 llm 段；
      model 为空/非字符串/纯空白或非对象节 → 跳过 + skipped+1，不继承主 llm model）；
    - models 非数组或空数组 → 兼容回退单对象 vision_llm.model（视作链长 1，既有装机零迁移）；
      残留组合（models==[] 且旧 model 键仍在）→ 告警一次（不阻断，D-A R2-A P3-1）；
    - 读盘/解析失败 → ([], 0)（降级为「未配置」语义，调用方返回含配置指引的明确错误）。
    """
    from niu_api.llm_proxy import get_llm_config
    from niu_api.config import CONFIG_PATH
    from agent.image_channel import main_has_vision

    try:
        data = json.loads(Path(CONFIG_PATH).read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"[vision-server] 读视觉模型链配置失败: {e}")
        return [], 0
    if not isinstance(data, dict):
        return [], 0

    main_cfg = get_llm_config()
    chain: List[dict] = []
    skipped = 0
    if main_has_vision(main_cfg):
        chain.append(main_cfg)

    vision_llm = data.get("vision_llm")
    if not isinstance(vision_llm, dict):
        vision_llm = {}
    models = vision_llm.get("models")
    legacy_model = vision_llm.get("model")
    has_legacy = isinstance(legacy_model, str) and bool(legacy_model.strip())

    if isinstance(models, list) and len(models) > 0:
        for node in models:
            if not isinstance(node, dict):
                skipped += 1
                logger.warning(f"[vision-server] vision_llm.models 节非对象，跳过: {node!r}")
                continue
            model = node.get("model")
            if not isinstance(model, str) or not model.strip():
                # 链节有效性（D-A R1-B P2-5）：空 model 不继承主 llm model
                skipped += 1
                logger.warning(f"[vision-server] vision_llm.models 节 model 为空，跳过: {node!r}")
                continue
            chain.append(_normalize_vision_node(node, main_cfg))
    else:
        if models is not None and not isinstance(models, list):
            logger.warning(f"[vision-server] vision_llm.models 非数组（{type(models).__name__}），"
                           "忽略并按单对象 model 回退")
        elif models == [] and has_legacy:
            # 残留组合（手工编辑遗留）：空数组 + 旧单对象键仍在——按兼容规则回退单对象；
            # 主模型有视觉时静默成 [主模型, legacy] 双链。告警一次不阻断（D-A R2-A P3-1）。
            logger.warning("[vision-server] vision_llm.models 为空数组但旧单对象 model 键仍在，"
                           "按兼容规则回退单对象；建议删整段或同时删 model 键")
        if has_legacy:
            chain.append(_normalize_vision_node(dict(vision_llm), main_cfg))
    return chain, skipped


def _classify_error_text(text: str) -> str:
    """文本兜底分类（D-D）：重试提示 → retryable；认证/欠费/配额类关键词 → fatal；其余 unknown。"""
    t = (text or "").lower()
    if any(h in t for h in _VISION_RETRY_HINTS):
        return "retryable"
    if any(h in t for h in _VISION_FATAL_HINTS):
        return "fatal"
    return "unknown"


def _classify_vision_error(exc=None, mock_resp=None):
    """视觉模型错误分类（D-D 双通道）→ (kind, type_name, msg)。

    kind ∈ {"retryable", "fatal", "unknown", "stopped"}：
    - **A 裸异常**（exc 非 None，适配层初始建连失败 re-raise）：按异常类名查自带常量表，
      未归类 → 文本兜底；
    - **B MockResponse**（mock_resp 非 None）：判据是 error_type 字段——fatal/stopped 直判；
      retry_exhausted 须文本含重试提示才算 retryable（否则 unknown）；None → 文本兜底
      （stream_error 且 error_msg 含限流提示时仍可归 retryable）。
      **不得用 error_type_name 判 fatal**（fatal 路径下它不可靠，plan v0.2 亲验）。
    """
    type_name = ""
    msg = ""
    if exc is not None:
        type_name = type(exc).__name__
        msg = str(exc) or type_name
        if type_name in _VISION_RETRYABLE_EXC:
            kind = "retryable"
        elif type_name in _VISION_FATAL_EXC:
            kind = "fatal"
        else:
            kind = _classify_error_text(msg)
    elif mock_resp is not None:
        error_type = getattr(mock_resp, "error_type", None)
        type_name = getattr(mock_resp, "error_type_name", None) or ""
        msg = getattr(mock_resp, "error_msg", None) or ""
        if error_type == "fatal":
            kind = "fatal"
        elif error_type == "stopped":
            kind = "stopped"
        elif error_type == "retry_exhausted":
            t = (msg or "").lower()
            kind = "retryable" if any(h in t for h in _VISION_RETRY_HINTS) else "unknown"
        else:
            kind = _classify_error_text(msg)
    else:
        kind = "unknown"
    return kind, type_name, msg


def _call_vision_model(cfg: dict, data_uri: str, question: str):
    """把图 + 提示词送进视觉模型，同步驱动 LiteLLMSession。

    返回 (answer, error)：成功 → (content, None)；失败 → (None, error_dict)。
    error_dict = {"kind", "type_name", "msg", "reason"}——kind 来自
    _classify_vision_error（D-D），reason 是中文原因（供 D-E 文案组装）。

    四种失败形态归一：无响应 / stream_error / 空 content（含 finish_reason=length
    细分，plan R2-B P2）/ 调用抛异常（适配层初始建连失败直接 re-raise——D-D 通道 A，
    本层 try/except 捕获）。

    cfg 键映射照 niu_api/llm_proxy.py call_llm_via_litellm 先例：get_llm_config
    返回全小写键且类型键名为 "type"，而 LiteLLMSession 读 "api_type"——直传会
    静默丢 type、api_type 恒 openai（plan R1-B P2）。
    """
    from agent.generic.litellm_adapter import LiteLLMSession

    # F-9（2026-09-12 用户定案）：请求级连接超时 5s——SDK 的 except Exception 兜底不区分
    # 「服务器不存在」与「暂时连不上」，不加连接上界 → 不可达主机 = 4 × 内核超时（≈75s）。
    # 四个分量必须全部显式给出（httpx 语义：未指定分量不继承默认值 → 变「无超时」）。
    timeout = httpx.Timeout(connect=5.0, read=float(cfg.get("read_timeout") or 300),
                            write=30.0, pool=5.0)
    # F-8（2026-09-12 用户定案）：重试全交 SDK（max_retries=3 = 首次 1 次 + 重试 3 次），
    # 本层不再自做退避；用户自带 litellm_kwargs 的 max_retries/timeout 被覆盖。
    litellm_kwargs = {**(cfg.get("litellm_kwargs") or {}), "max_retries": 3, "timeout": timeout}

    llm_config = {
        "api_type": cfg.get("type", "openai"),
        "apikey": cfg["apikey"],
        "apibase": cfg["apibase"],
        "model": cfg["model"],
        "reasoning_effort": cfg.get("reasoning_effort"),
        "provider": cfg.get("provider", ""),
        "litellm_kwargs": litellm_kwargs,
        "read_timeout": cfg.get("read_timeout") or 300,
        # 独立 sticky id（plan R1-A P3）：防与主对话/其它通道串扰（"mcp-sampling" 先例）
        "sticky_session_id": "analyze-image",
        # 参数约束 deny 补键（config 来自 get_llm_config，段内 capabilities 经小写化保留；缺键 → fail-closed 空集）
        "capabilities": cfg.get("capabilities"),
    }
    if cfg.get("max_tokens") is not None:
        llm_config["max_tokens"] = cfg["max_tokens"]

    try:
        session = LiteLLMSession(cfg=llm_config)
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": question},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ],
        }]

        # chat() 返回 generator：yield str chunks，StopIteration.value 携带 MockResponse
        # （照 llm_proxy.py sync_call——for 循环会吞掉 StopIteration 返回值，必须 next()）
        gen = session.chat(messages=messages)
        mock_response = None
        try:
            while True:
                next(gen)
        except StopIteration as e:
            mock_response = e.value
    except Exception as e:
        # 初始建连/调用异常（401/429/404 等适配层直接 re-raise）→ D-D 通道 A
        kind, type_name, msg = _classify_vision_error(exc=e)
        return None, {"kind": kind, "type_name": type_name, "msg": msg,
                      "reason": f"调用异常（{type_name}）：{msg or '未知错误'}"}

    if mock_response is None:
        return None, {"kind": "unknown", "type_name": "", "msg": "",
                      "reason": "模型未返回响应"}
    if getattr(mock_response, "stream_error", False):
        kind, type_name, msg = _classify_vision_error(mock_resp=mock_response)
        return None, {"kind": kind, "type_name": type_name, "msg": msg,
                      "reason": msg or "模型调用出错（未知错误）"}
    content = mock_response.content or ""
    if not content.strip():
        # 空回答细分（plan R2-B P2 / §1.6 实验 A）：思考型视觉模型 + 低 max_tokens →
        # 推理链耗尽预算，finish_reason=length——与「模型失败」不得混同；归 unknown，由 SDK 决定重试，本层不额外处理
        if getattr(mock_response, "finish_reason", None) == "length":
            return None, {"kind": "unknown", "type_name": "", "msg": "",
                          "reason": ("输出预算耗尽（思考型模型的推理链占满了 max_tokens，正文无输出）。"
                                     "请删掉该模型配置的 max_tokens（缺省即由模型自决）后重试，或收窄问题范围")}
        return None, {"kind": "unknown", "type_name": "", "msg": "",
                      "reason": "模型返回空内容"}
    return content, None


def _clip_reason(text: str) -> str:
    """D-E：汇总/注记文案中每条原因截断 ≤120 字符。"""
    t = str(text or "").strip()
    return t[:_VISION_REASON_MAX_CHARS] if len(t) > _VISION_REASON_MAX_CHARS else t


def _skipped_note(skipped: int) -> str:
    """D-E：失败文案追加跳过节注记（防「配了 N 项却报无备用」的观感误导）。"""
    return f"（另有 {skipped} 个配置无效被跳过）" if skipped > 0 else ""


def _call_with_fallback(chain: List[dict], skipped: int, data_uri: str, question: str) -> str:
    """链式降级循环（D-B / D-D / D-E / D-F），返回最终给工具的文字。

    每模型一次调用（F-8 2026-09-12 用户定案：重试全交 SDK，本层不再自做退避）；
    致命/未知直接换下一个模型；已停止（stopped）不降级、立即返回。
    stop 检查点三处：循环顶 / 每次调用返回后 / stopped-kind（全局 is_stop_requested——
    vision-server 同进程与主循环共享同一 Event）。
    总预算：每模型一查（新调用前）累计耗时，超 _VISION_CHAIN_BUDGET_SECONDS 停止降级
    并返回「预算中断」文案（不 gate in-flight 调用）。
    F-5：多模型链记住 30 分钟内成功过的模型作本轮起点；一轮全失败清记忆。
    """
    from agent.generic.litellm_adapter import is_stop_requested

    start = time.monotonic()
    failures: List[tuple] = []  # [(模型名, 原因)]——失败的模型

    def _over_budget() -> bool:
        return (time.monotonic() - start) >= _VISION_CHAIN_BUDGET_SECONDS

    def _budget_msg() -> str:
        """D-E「预算中断」：M=1（仅链首被调用，未发生降级）时不含「已自动降级」。"""
        listed = list(failures)
        tried = len(listed)
        k = len(chain) - tried
        tail = f"，因累计耗时超 {_VISION_CHAIN_BUDGET_SECONDS}s 停止继续降级"
        if k > 0:
            tail += f"（尚有 {k} 个模型未尝试）"
        if tried >= 2:
            first_name, first_reason = listed[0]
            return (f"识图失败：{first_name} 不可用（{_clip_reason(first_reason)}），"
                    f"已自动降级尝试 {tried} 个模型均失败{tail}" + _skipped_note(skipped))
        if tried == 1:
            first_name, first_reason = listed[0]
            return (f"识图失败：{first_name} 不可用（{_clip_reason(first_reason)}）"
                    f"{tail}" + _skipped_note(skipped))
        # 理论不可达（预算在链首首次调用前耗尽）——防御性兜底
        return f"识图失败：因累计耗时超 {_VISION_CHAIN_BUDGET_SECONDS}s 停止继续降级" \
            + _skipped_note(skipped)

    order = list(range(len(chain)))
    start_idx = _remembered_start(chain)
    if start_idx:
        order = order[start_idx:] + order[:start_idx]

    for pos, idx in enumerate(order):
        cfg = chain[idx]
        name = str(cfg.get("model") or "未知模型")
        if is_stop_requested():
            return "识图已停止"
        if _over_budget():
            return _budget_msg()

        answer, error = _call_vision_model(cfg, data_uri, question)
        # 调用返回后再查 stop（R3-A P2：末位模型调用中 stop 时适配层返回空响应，
        # 仅靠 error_type 会被误判「未知」而误报「无备用模型可降级」）
        if is_stop_requested():
            return "识图已停止"
        if error is None:
            if pos == 0:
                _remember_vision_success(chain, name)
                return answer  # 本轮起点一次成功：零附加提示
            # 非起点模型成功（D-E / U-5）：只提首模型原因
            first_name, first_reason = failures[0]
            _remember_vision_success(chain, name)
            return (f"{answer}\n\n"
                    f"（注：首模型不可用（{_clip_reason(first_reason)}），已自动降级到 {name}）")

        if error["kind"] == "stopped":
            return "识图已停止"  # 不降级（D-D）
        failures.append((name, error["reason"]))

    n = len(failures)
    if n == 1:
        # D-E「单模型失败」（N=1 划界：未发生降级，有意偏离 U-5 字面）
        name, reason = failures[0]
        return f"识图失败：{name} 不可用：{_clip_reason(reason)}（无备用模型可降级）" + _skipped_note(skipped)

    # D-E「全链失败」（N≥2）：每条原因截断 ≤120；>3 个只列前 3 + 「等 N 个」
    first_name, first_reason = failures[0]
    marks = "①②③④⑤⑥⑦⑧⑨"
    items = []
    for i, (mname, mreason) in enumerate(failures[:3]):
        mark = marks[i] if i < len(marks) else f"{i + 1}."
        items.append(f"{mark}{mname}：{_clip_reason(mreason)}")
    if n > 3:
        items.append(f"等 {n} 个")
    _forget_vision_success(chain)
    return (f"识图失败：首模型不可用（{first_name}：{_clip_reason(first_reason)}），"
            f"已自动降级尝试 {n} 个模型均失败：" + "；".join(items)
            + _skipped_note(skipped))


def analyze_image(image_path: str, question: str) -> str:
    """把指定图片 + 提示词送进视觉模型，返回文字答案（不返回图标记——D-C）。

    执行流程（plan §3.2 / vision-model-fallback D-B）：读图（魔数 MIME 探测 /
    超限降采样 → data URI）→ 构建视觉模型链（主模型有视觉作链首，其后接
    vision_llm.models / 单对象回退；皆无 → 含配置指引的明确错误）→
    _call_with_fallback 按序调用（每模型一次、重试交 SDK，致命/未知直接降级、
    stop/总预算中断即停；多模型链记忆 30 分钟内成功过的模型作本轮起点）→ 纯文本。

    停止语义归属 agent_loop 外层放弃等待（所有工具执行被统一包装）——
    本工具内不做独立 stop 包装（plan R2-B P1），只在调用间隙查全局
    is_stop_requested 决定「继续降级还是立即返回」。一切失败返回明确中文错误串，
    不抛异常（穿透到 MCP 层会暴露英文原始异常）。
    """
    image_path = str(image_path or "").strip()
    question = str(question or "").strip()
    if not image_path:
        return "错误：image_path 必填（图片的绝对路径）"
    if not os.path.isabs(image_path):
        return f"错误：image_path 必须是绝对路径（收到 {image_path!r}）"
    if not question:
        return ("错误：question 必填——要向模型提的问题，决定模型看图时关注什么、输出什么")

    data_uri = _load_image_data_uri(image_path)
    if data_uri is None:
        return (f"读图失败：{image_path} 不存在，或不是受支持的图片"
                "（支持 PNG/JPEG/GIF/WebP/BMP/HEIC），或超限后降采样仍解码失败")

    chain, skipped = _vision_chain()
    if not chain:
        return ("识图不可用：主模型无视觉能力，且 vision_llm 段未配置。"
                "请把主模型换成支持视觉的模型并完成能力探测（设置页），"
                "或在 vision_llm 段配置第三方视觉模型（见 SYSTEM_MANUAL 视觉能力节）；"
                "也可配置 vision_llm.models 多模型链实现自动降级")

    try:
        return _call_with_fallback(chain, skipped, data_uri, question)
    except Exception as e:
        logger.warning(f"[vision-server] analyze_image failed: {e}")
        return f"识图失败：{e}"


# ============== TOOL_SCHEMAS ==============
# 键名契约（R8）：schema name == yaml tools 键 == 模块函数名，三处逐字符一致
# （'screenshot' / 'list_targets' / 'analyze_image'）——不一致则 visibility_map
# 查不到，工具静默落 hidden。

TOOL_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "screenshot": {
        "name": "screenshot",
        "description": (
            "截取屏幕画面（整屏/指定窗口/指定区域），图片落盘并返回纯绝对路径 + 尺寸元数据"
            "（不返回图标记）。当你需要看到屏幕上的内容（界面、报错、图表、用户正在看的画面）时使用；"
            "要理解截到的画面内容，拿到路径后调 analyze_image(路径, 问题)。"
            "target=screen 截整个桌面；target=window 需 window_id（窗口 ID）；"
            "target=region 截取矩形区域：推荐用 region_ratio=[左,上,右,下]"
            "（4 个 0~1 数值，相对整屏图按比例框选——工具内部负责换算坐标，无需自己算）；"
            "也可用 x/y/width/height（逻辑桌面绝对坐标，左上角原点）。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "enum": ["screen", "window", "region"],
                    "description": "截图形态：screen=整个桌面（默认），window=指定窗口，region=指定矩形区域",
                    "default": "screen",
                },
                "window_id": {
                    "type": ["string", "integer"],
                    "description": "窗口 ID（target=window 时必填）",
                },
                "x": {
                    "type": "number",
                    "description": ("区域左上角 x（target=region 时使用，逻辑桌面坐标；"
                                    "与 region_ratio 二选一，推荐用 region_ratio 比例写法）"),
                },
                "y": {
                    "type": "number",
                    "description": ("区域左上角 y（target=region 时使用，逻辑桌面坐标；"
                                    "与 region_ratio 二选一，推荐用 region_ratio 比例写法）"),
                },
                "width": {
                    "type": "number",
                    "description": "区域宽度（target=region 时使用；与 region_ratio 二选一，推荐用 region_ratio 比例写法）",
                },
                "height": {
                    "type": "number",
                    "description": "区域高度（target=region 时使用；与 region_ratio 二选一，推荐用 region_ratio 比例写法）",
                },
                "region_ratio": {
                    "type": "array",
                    "items": {"type": "number"},
                    "minItems": 4,
                    "maxItems": 4,
                    "description": (
                        "按比例截取区域：恰好 4 个 0~1 数值 [左,上,右,下]，"
                        "恒相对整个逻辑桌面（所有显示器合成的范围，即 target=screen 那张图）。"
                        "仅在 target=region 时有效；与 x/y/width/height 互斥。"
                        "推荐优先使用——工具内部负责换算绝对坐标，无需自己算。"
                    ),
                },
            },
            "required": ["target"],
        },
    },
    "list_targets": {
        "name": "list_targets",
        "description": (
            "列出当前可截取的所有目标：显示器（逻辑尺寸/位置/缩放，主屏标注）+ 窗口"
            "（id/app/标题/尺寸/位置，前台应用标注）。无参数。截图前先调用它确认目标、"
            "拿到窗口编号，再用 screenshot(target=\"window\", window_id=...) 截指定窗口。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    "analyze_image": {
        "name": "analyze_image",
        "description": (
            "用视觉模型解读图片内容并返回文字结论（不返回图片）。"
            "`question` 是你要向模型提的问题——它决定模型关注什么、输出什么："
            "- 想了解整体：问宽泛的（如「这张图里有什么」）——模型会给出整体描述"
            "（密集界面可能因输出预算只覆盖一部分，没提到的内容不代表没看到）。"
            "- 想知道某个细节：带着具体问题再问一次同一张图（如「顶部状态栏显示什么」）——"
            "聚焦提问会让模型只看那一处，回答更准且输出更省（实测同一张图：泛问 1129 token，"
            "聚焦问 77 token）。"
            "- 同一张图可以带不同问题反复调用——第一次的回答往往能告诉你「还有什么可问」。"
            "模型内部自动选择（主模型有视觉用主模型，否则用 vision_llm 段），无需指定。"
            "工具内部按配置的多个视觉模型依次尝试，首个不可用时自动降级到下一个（重试由底层 SDK 执行，"
            "最多重试 3 次；仍失败则换下一个模型；总预算内），"
            "返回结果会标注是否发生降级。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "image_path": {
                    "type": "string",
                    "description": "图片的绝对路径（截图产物 / 用户拖入的图 / 任意本地图片）",
                },
                "question": {
                    "type": "string",
                    "description": "要向模型提的问题——决定模型看图时关注什么、输出什么",
                },
            },
            "required": ["image_path", "question"],
        },
    },
}


def get_tool_schemas() -> List[Dict[str, Any]]:
    """Return all tool schemas for MCP Loader registration."""
    return list(TOOL_SCHEMAS.values())


# ============== MCP Server (for standalone stdio mode) ==============

try:
    from mcp.server import Server
    from mcp.types import Tool, TextContent

    server = Server("vision-server")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name=schema["name"],
                description=schema["description"],
                inputSchema=schema["input_schema"],
            )
            for schema in get_tool_schemas()
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        try:
            if name == "screenshot":
                result = screenshot(**arguments)
            elif name == "list_targets":
                result = list_targets()
            elif name == "analyze_image":
                result = analyze_image(**arguments)
            else:
                return [TextContent(type="text", text=f"Unknown tool: {name}")]

            return [TextContent(type="text", text=str(result))]
        except Exception as e:
            logger.exception(f"Error executing tool {name}: {e}")
            return [TextContent(type="text", text=f"Error: {e}")]

except ImportError:
    server = None


def main():
    """Entry point for standalone MCP server (stdio mode)."""
    if server is None:
        print("mcp package not installed, cannot run as standalone server")
        return
    import asyncio
    from mcp.server.stdio import stdio_server

    async def run():
        # mcp 1.27 stdio_server() 无参（返回 stdin/stdout 流）——对齐 config-manager/
        # photo-server 等主流写法（brain-region 的 stdio_server(server) 是 SDK 旧签名残留）
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    asyncio.run(run())
