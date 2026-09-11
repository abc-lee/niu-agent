"""
vision-server — 屏幕截图 + 识图 MCP 服务器（可视化功能 plan v0.5.2 §4-V5 /
2026-09-11-vision-channel-refactor.md）

三工具（plan 2026-09-11-vision-channel-refactor.md §3）：
- list_targets：无参，列出当前可截取目标（显示器 + 窗口清单，含前台应用行与
  48 个窗口截断警告）——截图前先调用它拿窗口编号。
- screenshot：niu_natives DesktopSession capture（desktop/window_id/region
  三形态）→ 降采样 ≤1280 宽 → 落盘 ~/.niu/tmp/screenshot_<ts>.png → 返回
  **纯绝对路径** + 尺寸/显示器元数据（不返回图标记——与用户发图同形；
  要理解画面内容调 analyze_image）。
- analyze_image(image_path, question)：把指定图片 + 提示词送进视觉模型，
  返回**文字答案**。模型内部自选（主模型优先：主模型有视觉 → 用主模型；
  否则用 vision_llm 段；皆无 → 明确错误含配置指引）。不依赖 niu_natives。

D-D：screenshot/analyze_image 是基础工具与视觉能力无关——visibility: static
无条件直挂主 Agent（yaml 显式 static，register_server 默认 hidden）；子 Agent
经 frontmatter `mcpServers: [vision-server]` 声明即用。

niu_natives import 降级（R11）：模块级 try/except——失败 → DesktopSession=None
+ logger.warning，screenshot 返回明确错误串，服务器正常加载（.so 缺失/跨平台
未编不得炸 Niu 启动——REQUIRED_SERVERS __import__ 失败会 RuntimeError 终止）。
"""

from __future__ import annotations

import json
import math
import os
import sys
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

# 降采样上限：≤1280 宽（plan §4-V5；多模态 token 成本与视觉模型输入限制）
MAX_WIDTH = 1280

_UNAVAILABLE_MSG = "截图能力不可用（niu_natives 未安装/平台不支持）"

# 窗口列表硬上限副本——锚定 Rust 出处：niu-natives/src/desktop/{macos,win32}/capture.rs
# 的 MAX_LISTED_WINDOWS = 48。Rust 侧达到上限后静默 break（无截断标志），Python
# 侧只能以 len(windows) >= 48 启发式检测并显式告知 Agent，否则它以为屏幕上就这些。
MAX_LISTED_WINDOWS = 48


def _get_session():
    """惰性获取/创建 DesktopSession 单例。"""
    global _session
    if niu_natives is None:
        return None
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

    return _format_result(out_path, result)


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


# ============== analyze_image（plan 2026-09-11-vision-channel-refactor §3.2） ==============


def _load_image_data_uri(image_path: str):
    """读图片文件 → data URI（复用 agent.image_channel helper：魔数 MIME 探测 /
    >4MB 降采样）。失败（缺文件/非图/超限且降采样失败）→ None。

    函数级 import（plan R5——独立 MCP server 进程与 Niu 主进程同构，仓库既有先例）。
    """
    from agent.image_channel import _image_to_data_uri
    return _image_to_data_uri(image_path)


def _pick_vision_llm_config():
    """选模型（D-D 主模型优先）：

    - 主模型有视觉（main_has_vision，fail-closed 三条件）→ 用主 llm 段原配置
      （不读 vision 段——主模型自身 reasoning_effort 等参数全保留）；
    - 否则读**原始 user-config.json** 判 vision_llm.model 非空 → get_llm_config(use_vision_config=True)
      （不能用该调用的返回值判空——段 model 空时它继承主 llm model，返回恒非空，
      判据成死代码；plan §3.2 步骤 2 / R2-A P2）；
    - 皆无 → None（调用方返回含配置指引的明确错误）。

    任何读盘/解析失败 → None（降级为「未配置」语义，不抛异常）。
    """
    from niu_api.llm_proxy import get_llm_config
    from niu_api.config import CONFIG_PATH
    from agent.image_channel import main_has_vision

    try:
        cfg = get_llm_config()
        if main_has_vision(cfg):
            return cfg
        data = json.loads(Path(CONFIG_PATH).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        vision_model = (data.get("vision_llm") or {}).get("model")
        if not vision_model:
            return None
        return get_llm_config(use_vision_config=True)
    except Exception as e:
        logger.warning(f"[vision-server] analyze_image 选模型失败: {e}")
        return None


def _call_vision_model(cfg: dict, data_uri: str, question: str) -> str:
    """把图 + 提示词送进视觉模型，同步驱动 LiteLLMSession，返回文字答案。

    cfg 键映射照 niu_api/llm_proxy.py call_llm_via_litellm 先例：get_llm_config
    返回全小写键且类型键名为 "type"，而 LiteLLMSession 读 "api_type"——直传会
    静默丢 type、api_type 恒 openai（plan R1-B P2）。
    """
    from agent.generic.litellm_adapter import LiteLLMSession

    llm_config = {
        "api_type": cfg.get("type", "openai"),
        "apikey": cfg["apikey"],
        "apibase": cfg["apibase"],
        "model": cfg["model"],
        "reasoning_effort": cfg.get("reasoning_effort"),
        "provider": cfg.get("provider", ""),
        "litellm_kwargs": cfg.get("litellm_kwargs", {}),
        "read_timeout": cfg.get("read_timeout") or 300,
        # 独立 sticky id（plan R1-A P3）：防与主对话/其它通道串扰（"mcp-sampling" 先例）
        "sticky_session_id": "analyze-image",
        # 参数约束 deny 补键（config 来自 get_llm_config，段内 capabilities 经小写化保留；缺键 → fail-closed 空集）
        "capabilities": cfg.get("capabilities"),
    }
    if cfg.get("max_tokens") is not None:
        llm_config["max_tokens"] = cfg["max_tokens"]

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

    if mock_response is None:
        return "识图失败：模型未返回响应"
    if getattr(mock_response, "stream_error", False):
        err = getattr(mock_response, "error_msg", None) or "未知错误"
        return f"识图失败（模型调用出错）：{err}"
    content = mock_response.content or ""
    if not content.strip():
        # 空回答细分（plan R2-B P2 / §1.6 实验 A）：思考型视觉模型 + 低 max_tokens →
        # 推理链耗尽预算，finish_reason=length——与「模型失败」不得混同
        if getattr(mock_response, "finish_reason", None) == "length":
            return ("识图失败：输出预算耗尽（思考型模型的推理链占满了 max_tokens，正文无输出）。"
                    "请调大该模型配置的 max_tokens 后重试，或收窄问题范围")
        return "识图失败：模型返回空内容"
    return content


def analyze_image(image_path: str, question: str) -> str:
    """把指定图片 + 提示词送进视觉模型，返回文字答案（不返回图标记——D-C）。

    执行流程（plan §3.2）：读图（魔数 MIME 探测 / 超限降采样 → data URI）→
    选模型（主模型优先：主模型有视觉用主模型，否则 vision_llm 段；皆无 →
    含配置指引的明确错误）→ LiteLLMSession 同步调用 → 纯文本。

    停止语义归属 agent_loop 外层放弃等待（所有工具执行被统一包装）——
    本工具内不做独立 stop 包装（plan R2-B P1）。一切失败返回明确中文错误串，
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

    cfg = _pick_vision_llm_config()
    if cfg is None:
        return ("识图不可用：主模型无视觉能力，且 vision_llm 段未配置。"
                "请把主模型换成支持视觉的模型并完成能力探测（设置页），"
                "或在 vision_llm 段配置第三方视觉模型（见 SYSTEM_MANUAL 视觉能力节）")

    try:
        return _call_vision_model(cfg, data_uri, question)
    except Exception as e:
        logger.warning(f"[vision-server] analyze_image failed: {e}")
        return f"识图失败：{e}"


# ============== TOOL_SCHEMAS ==============
# 键名契约（R8）：schema name == yaml tools 键 == 模块函数名，三处逐字符一致
# （'screenshot' / 'list_targets' / 'analyze_image'）——不一致则 visibility_map 查不到，工具静默落 hidden。

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
