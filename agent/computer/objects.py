"""Computer Use 对象模型 —— 上游 OMP worker.ts (@883c9507ff) 的照抄移植。

上游源（packages/coding-agent/src/tools/computer/worker.ts，753 行）：
- El            ← worker.ts:220-310
- Win           ← worker.ts:312-414
- desktop facade ← worker.ts:641-737（#createDesktopScope）
- captureScreenshot ← worker.ts:180-217
- nativeError / nativeCall / pointerOptions / chordKeys / matchesFilter / guardRun
                    ← worker.ts:130-178

允许的语言适配（JS → Python，唯一允许的偏差类别）：
- async/await → 同步调用（niu_natives 方法本身阻塞等待原生 worker 回复）；
- AsyncLocalStorage<ComputerRunContext>（worker.ts:420-426, 527-531）→ contextvars.ContextVar，
  语义相同：句柄跨 run 存活，调用时永远解析**当前 run** 的策略与输出通道；
- camelCase → snake_case（nativeRole→native_role、maxDepth→max_depth；
  delivery→delivery_mode 是原生 wire 键名，照抄 worker.ts:146-154 的映射）;
- ToolError → ComputerToolError。错误**名**不丢：niu_natives 把原生错误映射为
  RuntimeError("{code}: {message}")（niu-natives/src/desktop/error.rs），StaleRef /
  PermissionDenied / InvalidCoordinateFrame 等代码前缀原样保留在消息里。

会话单例不在本模块创建：`get_desktop_session()` 复用 vision-server 的
`_get_session()`（同进程 MCP 内部服务器，模块级单例）——帧缓存 / AX ref 登记都在
那个会话内，两套会话会把帧锚定拆开。截图落盘与回执同样复用 `_save_png` /
`_format_result`；降采样上限复用同一 `MAX_WIDTH` 口径。

成员状态（已实现 / BLOCKED）见 docs/superpowers/refs/2026-09-13-computer-objectmap.md。
"""

from __future__ import annotations

import base64
import contextvars
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple, Union


# ============== 错误（上游 ToolError / nativeError，worker.ts:130-142） ==============

class ComputerToolError(Exception):
    """上游 ToolError。原生错误的 `{code}: {message}` 前缀由 niu_natives 保留，
    错误名（StaleRef / PermissionDenied / InvalidCoordinateFrame …）不丢。"""


def native_call(call: Callable[[], Any]) -> Any:
    """worker.ts:130-142（nativeError + nativeCall）：原生异常一律包成 ToolError，消息原样保留。

    上游的 throwIfAborted(signal) 属运行时中止通道（C2+），C1 骨架无 signal。
    """
    try:
        return call()
    except ComputerToolError:
        raise
    except Exception as e:
        raise ComputerToolError(str(e)) from e


# ============== run 上下文（上游 ComputerRunContext，worker.ts:93-99） ==============

@dataclass
class RunContext:
    """每次 run() 一份。上游经 AsyncLocalStorage 携带；此处 ContextVar——句柄跨 run
    存活，但方法调用时永远解析当前 run 的 read-only 策略 / 输出通道 / 截图记录。"""

    read_only: bool = False
    output: List[str] = field(default_factory=list)        # 文本回执（上游 context.output）
    screenshots: List[dict] = field(default_factory=list)  # ComputerScreenshot（protocol.ts:41-48）


_run_context_var: "contextvars.ContextVar[Optional[RunContext]]" = contextvars.ContextVar(
    "computer_run_context", default=None
)


def current_run_context() -> RunContext:
    """上游 #currentRunContext（worker.ts:631-635）：无活动 run → ToolError('no active computer run')。"""
    context = _run_context_var.get()
    if context is None:
        raise ComputerToolError("no active computer run")
    return context


def guard_run(context: RunContext, method: str) -> None:
    """worker.ts:175-178：read-only run 上动作用法 → ToolError（消息照抄）。"""
    if context.read_only:
        raise ComputerToolError(f"read-only run: '{method}' requires read_only: false")


# ============== 选项映射（上游 pointerOptions / chordKeys / matchesFilter，worker.ts:146-173） ==============

def pointer_options(options: Optional[dict]) -> Optional[dict]:
    """worker.ts:146-154：button/count/modifiers 透传；delivery → 原生 wire 键 delivery_mode。"""
    if options is None:
        return None
    mapped: Dict[str, Any] = {}
    for key in ("button", "count", "modifiers"):
        if options.get(key) is not None:
            mapped[key] = options[key]
    if options.get("delivery") is not None:
        mapped["delivery_mode"] = options["delivery"]
    return mapped


def chord_keys(chord: Union[str, List[str]]) -> List[str]:
    """worker.ts:156-163：字符串按 '+' 拆分、逐项 trim、丢空项；序列原样转 list。"""
    if isinstance(chord, str):
        return [key.strip() for key in chord.split("+") if key.strip()]
    return list(chord)


def matches_filter(window: Any, filter: Optional[dict]) -> bool:
    """worker.ts:165-173：app/title 大小写不敏感子串匹配（上游参数名就叫 filter）。"""
    if not filter:
        return True
    app = (filter.get("app") or "").lower()
    title = (filter.get("title") or "").lower()
    window_app = str(getattr(window, "app", "")).lower()
    window_title = str(getattr(window, "title", "")).lower()
    return ((not app) or app in window_app) and ((not title) or title in window_title)


# ============== vision-server 共享层（底层共用一套，禁止自建会话/落盘/回执） ==============

def _vision_server():
    """惰性 import niu_vision_server（与 MCP 内部服务器同进程，模块级单例天然共享）。

    正常路径：mcp_loader._add_server_workdirs_to_sys_path 已把 workdir
    （config/mcp-servers.yaml: vision-server.workdir = mcp-servers/vision-server/src）
    加入 sys.path。下方 fallback 只覆盖 agent.computer 先于 MCP 加载被 import 的时序。
    """
    try:
        import niu_vision_server
    except ImportError:
        src = Path(__file__).resolve().parents[2] / "mcp-servers" / "vision-server" / "src"
        if str(src) not in sys.path:
            sys.path.insert(0, str(src))
        import niu_vision_server
    return niu_vision_server


def get_desktop_session():
    """唯一的 DesktopSession —— vision-server 的 `_get_session()` 单例（niu_natives 缺失时返回 None）。"""
    return _vision_server()._get_session()


# ============== 截图（上游 captureScreenshot，worker.ts:180-217） ==============

def capture_screenshot(session: Any, context: RunContext, target: str,
                       options: Optional[dict] = None) -> dict:
    """worker.ts:180-217：capture → 落盘 → 记 screenshots →（非 silent）推回执 → 返回 {path,width,height}。

    Niu 适配：落盘/回执复用 vision-server `_save_png` / `_format_result`；降采样上限
    复用同一 `MAX_WIDTH`（=1280）口径。上游的 per-run snapshot.captureMaxWidth/Height
    （computer.ts 默认 1280×896）在 C1 统一为共享常量。
    """
    vs = _vision_server()
    caps = {"max_width": vs.MAX_WIDTH}
    frame = native_call(lambda: session.capture(target, caps))
    out_path = vs._save_png(frame["png_bytes"])
    context.screenshots.append({
        "path": str(out_path),
        "width": frame["width"],
        "height": frame["height"],
        "source_width": frame["source_width"],
        "source_height": frame["source_height"],
        "target": target,
    })
    if not (options or {}).get("silent"):
        context.output.append(vs._format_result(out_path, frame))
    return {"path": str(out_path), "width": frame["width"], "height": frame["height"]}


# ============== El（上游 worker.ts:220-310） ==============

class El:
    """AX 元素句柄。身份字段在构造时从 AxNode 拷贝（worker.ts:231-243）；
    value/bounds/actions 是**方法**——每次经 ref 重读活节点，不是缓存字段。"""

    def __init__(self, session: Any, node: Any):
        self._session = session
        self.ref = node.ref
        self.role = node.role
        self.native_role = node.native_role
        self.title = node.title
        self.description = node.description
        self.enabled = node.enabled
        self.focused = node.focused
        self.child_count = node.child_count

    def __repr__(self) -> str:
        """可读身份摘要（上游 JS 对象经 JSON.stringify 打印自身属性；Python print/display
        回落到 __repr__）：role/title/ref 是重定位活元素的三要素。"""
        return f"El(role={self.role!r}, title={self.title!r}, ref={self.ref!r})"

    def value(self) -> Optional[str]:
        """worker.ts:245-248：axNode(ref).value（重读，非缓存）。"""
        return native_call(lambda: self._session.ax_node(self.ref)).value

    def set_value(self, value: str) -> None:
        """worker.ts:250-254。"""
        context = current_run_context()
        guard_run(context, "setValue")
        native_call(lambda: self._session.ax_set_value(self.ref, value))

    def bounds(self) -> Optional[dict]:
        """worker.ts:256-262：x/y/width/height 任一缺失 → None。全局桌面坐标（非截图像素）。"""
        node = native_call(lambda: self._session.ax_node(self.ref))
        if node.x is None or node.y is None or node.width is None or node.height is None:
            return None
        return {"x": node.x, "y": node.y, "width": node.width, "height": node.height}

    def attributes(self) -> dict:
        """worker.ts:264-267：Object.fromEntries(axAttributes(ref))。"""
        pairs = native_call(lambda: self._session.ax_attributes(self.ref))
        return dict(pairs)

    def actions(self) -> List[str]:
        """worker.ts:269-272：axNode(ref).actions ?? []。"""
        node = native_call(lambda: self._session.ax_node(self.ref))
        return node.actions or []

    def perform(self, action: str) -> None:
        """worker.ts:274-278。"""
        context = current_run_context()
        guard_run(context, "perform")
        native_call(lambda: self._session.ax_perform(self.ref, action))

    def press(self) -> None:
        """worker.ts:280-284：perform("press")。"""
        context = current_run_context()
        guard_run(context, "press")
        native_call(lambda: self._session.ax_perform(self.ref, "press"))

    def click(self, options: Optional[dict] = None) -> None:
        """worker.ts:286-290。"""
        context = current_run_context()
        guard_run(context, "click")
        native_call(lambda: self._session.ax_click(self.ref, pointer_options(options)))

    def focus(self) -> None:
        """worker.ts:292-296。"""
        context = current_run_context()
        guard_run(context, "focus")
        native_call(lambda: self._session.ax_focus(self.ref))

    def parent(self) -> Optional["El"]:
        """worker.ts:298-302：axParent(ref) → El | None（根节点为 None）。"""
        node = native_call(lambda: self._session.ax_parent(self.ref))
        return El(self._session, node) if node is not None else None

    def children(self) -> List["El"]:
        """worker.ts:304-309。"""
        nodes = native_call(lambda: self._session.ax_children(self.ref))
        return [El(self._session, n) for n in nodes]


# ============== Win（上游 worker.ts:312-414） ==============

class Win:
    """窗口句柄。指针 x,y = **该 target 最近一次截图**的像素；未先截图 → 原生层拒绝
    （InvalidCoordinateFrame，niu-natives/src/desktop/mod.rs:250-257）。AX bounds /
    elementAt = 全局桌面坐标——两套空间，不可混用（prompts/tools/computer.md Rules）。"""

    def __init__(self, session: Any, window: Any):
        self._session = session
        self.id = window.id
        self.app = window.app
        self.title = window.title
        self.pid = window.pid
        self.bounds = {"x": window.x, "y": window.y, "width": window.width, "height": window.height}
        self.focused = window.focused

    def __repr__(self) -> str:
        """可读身份摘要（上游 JS 对象经 JSON.stringify 打印自身属性；Python print/display
        回落到 __repr__）：id/app/title 是重定位窗口的三要素。"""
        return f"Win(id={self.id!r}, app={self.app!r}, title={self.title!r})"

    def screenshot(self, options: Optional[dict] = None) -> dict:
        """worker.ts:333-335：captureScreenshot(target=win.id)。截图是读动作，read-only 放行。"""
        return capture_screenshot(self._session, current_run_context(), self.id, options)

    def click(self, x: float, y: float, options: Optional[dict] = None) -> None:
        """worker.ts:337-341。"""
        context = current_run_context()
        guard_run(context, "click")
        native_call(lambda: self._session.click(self.id, x, y, pointer_options(options)))

    def double_click(self, x: float, y: float, options: Optional[dict] = None) -> None:
        """worker.ts:343-349：强制 count=2（上游 Omit<ClickOptions,'count'> 是编译期约束；
        运行时 {...options, count: 2} 覆盖传入值）。"""
        context = current_run_context()
        guard_run(context, "doubleClick")
        merged = {**(options or {}), "count": 2}
        native_call(lambda: self._session.click(self.id, x, y, pointer_options(merged)))

    def move(self, x: float, y: float) -> None:
        """worker.ts:351-355：上游无 options 参数（不传 PointerOptions）。"""
        context = current_run_context()
        guard_run(context, "move")
        native_call(lambda: self._session.move_mouse(self.id, x, y))

    def drag(self, points: List[tuple], options: Optional[dict] = None) -> None:
        """worker.ts:357-367：[[x,y],…] → [{x,y},…] 路径。"""
        context = current_run_context()
        guard_run(context, "drag")
        path = [{"x": p[0], "y": p[1]} for p in points]
        native_call(lambda: self._session.drag(self.id, path, pointer_options(options)))

    def scroll(self, x: float, y: float, options: Optional[dict] = None) -> None:
        """worker.ts:369-375：dx/dy 缺省 0。"""
        context = current_run_context()
        guard_run(context, "scroll")
        opts = options or {}
        native_call(lambda: self._session.scroll(
            self.id, x, y, opts.get("dx", 0), opts.get("dy", 0), pointer_options(opts)))

    def type(self, text: str, options: Optional[dict] = None) -> None:
        """worker.ts:377-381。"""
        context = current_run_context()
        guard_run(context, "type")
        native_call(lambda: self._session.type_text(self.id, text, pointer_options(options)))

    def press(self, chord: Union[str, List[str]], options: Optional[dict] = None) -> None:
        """worker.ts:383-389：chordKeys 拆分后 keyChord。"""
        context = current_run_context()
        guard_run(context, "press")
        keys = chord_keys(chord)
        native_call(lambda: self._session.key_chord(self.id, keys, pointer_options(options)))

    def raise_(self) -> None:
        """worker.ts:391-395 → session.raiseWindow(id)：最小化则先还原，再前置。
        `raise` 是 Python 保留字，落点名 raise_。"""
        context = current_run_context()
        guard_run(context, "raise")
        native_call(lambda: self._session.raise_window(self.id))

    def ax(self, options: Optional[dict] = None) -> str:
        """worker.ts:397-400：axSnapshot(id, {all?, maxDepth?}).text —— 返回**字符串树**
        （每行一个节点带 [ref=eN] 标签，不是数组，不可迭代）。JS maxDepth → Python max_depth。"""
        opts = options or {}
        native_opts = {k: opts[k] for k in ("all", "max_depth") if k in opts}
        return native_call(
            lambda: self._session.ax_snapshot(self.id, native_opts or None)).text

    def find(self, query: Optional[dict]) -> List["El"]:
        """worker.ts:402-407：axQuery(id, {role?, title?, value?, limit?}) → 全部匹配。"""
        nodes = native_call(lambda: self._session.ax_query(self.id, query))
        return [El(self._session, n) for n in nodes]

    def ref(self, ref: str) -> "El":
        """worker.ts:409-412：按 ref 重读活元素；过期 → StaleRef（原生错误码，ref 按快照代际有效）。"""
        node = native_call(lambda: self._session.ax_node(ref))
        return El(self._session, node)


# ============== Clipboard（上游 desktop.clipboard，worker.ts:714-735） ==============

class Clipboard:
    """上游实现 = utils/clipboard.ts：darwin 读 = pbpaste（:338）、win32 读 = PowerShell
    Get-Clipboard -Raw（:209-264）；写 = OSC52(TTY) + arboard（:98-137）。

    Niu 适配（语义相同，机制按平台 CLI）：Niu 是守护进程——无 TTY（OSC52 分支不适用）、
    niu_natives 无 arboard 绑定 → 写走 pbcopy / PowerShell Set-Clipboard。"""

    def read(self) -> str:
        """worker.ts:715-723 → readTextFromClipboard()；失败 → ""（上游同语义）。"""
        current_run_context()
        if sys.platform == "darwin":
            proc = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5)
            return proc.stdout if proc.returncode == 0 else ""
        if sys.platform == "win32":
            script = (
                "$ErrorActionPreference = 'Stop'\n"
                "[Console]::OutputEncoding = [Text.Encoding]::UTF8\n"
                "[Console]::Out.Write([string](Get-Clipboard -Raw))\n"
            )
            proc = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True, timeout=8)
            if proc.returncode != 0:
                return ""
            return proc.stdout.replace("\r\n", "\n")
        raise ComputerToolError("clipboard read unsupported on this platform")

    def write(self, text: str) -> None:
        """worker.ts:725-734 → copyToClipboard(text)，guardRun('clipboard.write')。"""
        context = current_run_context()
        guard_run(context, "clipboard.write")
        if sys.platform == "darwin":
            subprocess.run(["pbcopy"], input=text, text=True, check=True, timeout=5)
        elif sys.platform == "win32":
            b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
            script = (
                "$ErrorActionPreference = 'Stop'; "
                f"Set-Clipboard -Value ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{b64}')))"
            )
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                check=True, timeout=10)
        else:
            raise ComputerToolError("clipboard write unsupported on this platform")


# ============== 值类型转换（napi→PyO3 绑定适配，唯一允许偏差类别） ==============
#
# 上游 napi 把 Rust 结构体自动序列化为 JS 普通对象——`desktop.windows()` 在模型眼里
# 就是 `[{id, app, title, ...}]`（worker.ts:669-674，工具描述照抄）。PyO3 返回的是活
# 对象（属性访问），直接泄漏给模型 → `w["id"]` 必然 TypeError。判据：结果是**数据**
# （值类型）→ JSON 原生 dict/list；结果还能调方法（句柄 Win/El）→ 保持对象。
# 键名严格取 types.rs 的 wire 字段名（与工具描述一致），不改名、不增删字段。

_WINDOW_FIELDS = ("id", "app", "title", "pid", "x", "y", "width", "height", "focused")
_DISPLAY_FIELDS = ("id", "name", "x", "y", "width", "height", "scale",
                   "pixel_x", "pixel_y", "pixel_width", "pixel_height", "is_primary")
_CAPABILITIES_FIELDS = ("backend", "display_server", "capture", "input", "ax",
                        "background_window_input", "delivery_modes",
                        "capture_permission", "input_permission", "ax_permission",
                        "display_count")


def _as_value(obj: Any, fields: Tuple[str, ...]) -> dict:
    """PyO3 值对象 → JSON 原生 dict（键名 = types.rs wire 字段，逐字）。"""
    return {name: getattr(obj, name) for name in fields}


# ============== desktop facade（上游 worker.ts:641-737 #createDesktopScope） ==============

_DESKTOP_WINDOW = SimpleNamespace(
    id="desktop", app="desktop", title="desktop", pid=None,
    x=0, y=0, width=0, height=0, focused=False,
)


class Desktop:
    """上游 desktop 对象（worker.ts:655-736 返回的字面量）。click/type/… 绑定在合成
    Win(id="desktop") 上（worker.ts:645-654）——输入落到当前焦点窗口的语义由原生层完成。"""

    def __init__(self, session: Any):
        self._session = session
        self._desktop_target = Win(session, _DESKTOP_WINDOW)
        self.clipboard = Clipboard()

    def capabilities(self) -> dict:
        """worker.ts:656-663：原生 getter（永不失败，worker 不可用时回退最近快照）。
        值类型 → JSON 原生 dict（上游 napi 自动序列化为普通对象；键名 = types.rs
        DesktopCapabilities wire 字段）。"""
        current_run_context()
        caps = native_call(lambda: self._session.capabilities)
        return _as_value(caps, _CAPABILITIES_FIELDS)

    def displays(self) -> List[dict]:
        """worker.ts:665-668。值类型 → dict 列表（键名 = types.rs DesktopDisplay wire 字段）。"""
        current_run_context()
        displays = native_call(lambda: self._session.list_displays())
        return [_as_value(d, _DISPLAY_FIELDS) for d in displays]

    def windows(self, filter: Optional[dict] = None, **kwargs: Any) -> List[dict]:
        """worker.ts:669-674：listWindows + matchesFilter（上游参数名就叫 filter）。
        值类型 → dict 列表 `[{id, app, title, pid, x, y, width, height, focused}]`
        （与工具描述逐字一致；模型可 `w["id"]` 下标访问）。

        误用形态守卫（errors report surface failure）：filter 只接受 dict/None，
        不接受关键字参数——上游对象参数 `{app?, title?}` 形态在 Python 里就是 dict。"""
        current_run_context()
        if kwargs:
            raise ComputerToolError(
                "desktop.windows() takes no keyword arguments — pass the filter as an object, "
                f"e.g. desktop.windows({{'app': 'Safari'}}) (got {', '.join(kwargs)})")
        if filter is not None and not isinstance(filter, dict):
            raise ComputerToolError(
                "desktop.windows() filter must be a dict like {'app': 'Safari', 'title': 'Notes'} or None — "
                f"the filter is an object, not a string: got {type(filter).__name__} ({filter!r}), "
                "use desktop.windows({'app': ...})")
        windows = native_call(lambda: self._session.list_windows())
        return [_as_value(w, _WINDOW_FIELDS) for w in windows if matches_filter(w, filter)]

    def window(self, selector: Optional[Union[str, int, dict]] = None, **kwargs: Any) -> "Win":
        """worker.ts:675-690：字符串=精确 id 匹配；对象=app/title 子串过滤。
        0 命中 / 多命中 → ToolError（消息照抄，多命中列出候选 `id app "title"`）。

        绑定层容错（非新语义）：int 按 str() 归一后继续——另一工具 list_targets 的输出里
        id 是不带引号的数字，模型从那里抄数字是高频真实路径；两处 id 同一原生 id 空间，
        归一不会指错目标。

        selector 可选（默认 None）只为让误用进得了函数体：只传关键字时 Python 在入参前就抛
        TypeError，守卫无机会执行——None/缺参/关键字统一转可读 ToolError（errors report surface failure）。"""
        current_run_context()
        if kwargs:
            raise ComputerToolError(
                "desktop.window() takes no keyword arguments — call it with a window id, "
                f"e.g. desktop.window('21021'), or a filter object, e.g. desktop.window({{'app': ...}}) "
                f"(got {', '.join(kwargs)})")
        if selector is None:
            raise ComputerToolError(
                "desktop.window() requires a window id (str or int), e.g. desktop.window('21021'), "
                "or a filter object, e.g. desktop.window({'app': ...})")
        if isinstance(selector, int):
            selector = str(selector)
        if not isinstance(selector, (str, dict)):
            raise ComputerToolError(
                "desktop.window() selector must be a window id (str or int), or a filter dict like "
                f"{{'app': 'Safari'}} — got {type(selector).__name__} ({selector!r})")
        windows = native_call(lambda: self._session.list_windows())
        if isinstance(selector, str):
            matches = [w for w in windows if w.id == selector]
        else:
            matches = [w for w in windows if matches_filter(w, selector)]
        if len(matches) == 0:
            raise ComputerToolError(
                f"no window matches {json.dumps(selector, ensure_ascii=False)}")
        if len(matches) > 1:
            candidates = "\n".join(
                f"{w.id} {w.app} {json.dumps(w.title, ensure_ascii=False)}" for w in matches)
            raise ComputerToolError(
                f"multiple windows match {json.dumps(selector, ensure_ascii=False)}:\n{candidates}")
        return Win(self._session, matches[0])

    def focused_window(self) -> Optional["Win"]:
        """worker.ts:691-695：第一个 focused 窗口，否则 None。"""
        current_run_context()
        windows = native_call(lambda: self._session.list_windows())
        for w in windows:
            if w.focused:
                return Win(self._session, w)
        return None

    def screenshot(self, options: Optional[dict] = None) -> dict:
        """worker.ts:696：captureScreenshot(target="desktop")——全显示器合成图。"""
        return capture_screenshot(self._session, current_run_context(), "desktop", options)

    # worker.ts:697-703：全部绑定到合成 Win(id="desktop")
    def click(self, x: float, y: float, options: Optional[dict] = None) -> None:
        self._desktop_target.click(x, y, options)

    def double_click(self, x: float, y: float, options: Optional[dict] = None) -> None:
        self._desktop_target.double_click(x, y, options)

    def move(self, x: float, y: float) -> None:
        self._desktop_target.move(x, y)

    def drag(self, points: List[tuple], options: Optional[dict] = None) -> None:
        self._desktop_target.drag(points, options)

    def scroll(self, x: float, y: float, options: Optional[dict] = None) -> None:
        self._desktop_target.scroll(x, y, options)

    def type(self, text: str, options: Optional[dict] = None) -> None:
        self._desktop_target.type(text, options)

    def press(self, chord: Union[str, List[str]], options: Optional[dict] = None) -> None:
        self._desktop_target.press(chord, options)

    def element_at(self, x: float, y: float) -> Optional["El"]:
        """worker.ts:704-708：axElementAt("desktop", x, y)——**全局桌面坐标**（非截图像素），
        无需先截图。"""
        current_run_context()
        node = native_call(lambda: self._session.ax_element_at("desktop", x, y))
        return El(self._session, node) if node is not None else None

    def focused_element(self) -> Optional["El"]:
        """worker.ts:709-713：axFocused() → 当前焦点 AX 元素，否则 None。"""
        current_run_context()
        node = native_call(lambda: self._session.ax_focused())
        return El(self._session, node) if node is not None else None


# ============== 返回值序列化（上游 stringifyReturnValue，computer.ts:195-201） ==============

def _json_default(obj: Any) -> Any:
    """JS JSON.stringify 语义：own enumerable properties。Python 类取 __dict__ 公共属性；
    PyO3 对象（无 __dict__）按 dir() 收集公共非可调用属性。"""
    d = getattr(obj, "__dict__", None)
    if isinstance(d, dict):
        return {k: v for k, v in d.items() if not k.startswith("_")}
    out: Dict[str, Any] = {}
    for name in dir(obj):
        if name.startswith("_"):
            continue
        try:
            value = getattr(obj, name)
        except Exception:
            continue
        if callable(value):
            continue
        out[name] = value
    return out


def _stringify_return_value(value: Any) -> str:
    """computer.ts:195-201：字符串原样；否则 JSON(indent=2)；失败 → str()。"""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, indent=2, ensure_ascii=False, default=_json_default)
    except (TypeError, ValueError):
        return str(value)
