"""computer 对象模型形态契约：值类型 = JSON 原生 dict/list；活句柄 = 对象。

不依赖真实桌面：fake session 用属性访问对象（SimpleNamespace，模拟 PyO3 pyclass
的 `#[pyo3(get)]` 行为），monkeypatch `agent.computer.session.get_desktop_session`
绕开 niu_natives / vision-server。锁住两类形态：

- `desktop.windows()/displays()/capabilities()` → dict/list（模型按工具描述
  `w["id"]` 下标访问必须可用，且整体可 json.dumps）；
- `desktop.window(...)` → Win 句柄对象（属性访问 + 可调方法，跨 run 存活）。
"""
import json
from types import SimpleNamespace

import pytest

import agent.computer.objects as objects
import agent.computer.session as computer_session


def _fake_window(id="w1", app="Safari", title="Niu", pid=4242,
                 x=0, y=25, width=1200, height=800, focused=True):
    return SimpleNamespace(id=id, app=app, title=title, pid=pid,
                           x=x, y=y, width=width, height=height, focused=focused)


def _fake_display(**kw):
    base = {"id": "D1", "name": "Built-in Retina", "x": 0, "y": 0, "width": 1440,
            "height": 900, "scale": 2.0, "pixel_x": 0, "pixel_y": 0,
            "pixel_width": 2880, "pixel_height": 1800, "is_primary": True}
    base.update(kw)
    return SimpleNamespace(**base)


def _fake_caps():
    return SimpleNamespace(backend="macos", display_server=None, capture=True,
                           input=True, ax=False, background_window_input=True,
                           delivery_modes=["background", "foreground"],
                           capture_permission="granted", input_permission="granted",
                           ax_permission="unavailable", display_count=1)


class FakeSession:
    """模拟 niu_natives.DesktopSession：值对象走属性访问（PyO3 pyclass 行为）。"""

    def __init__(self):
        self._windows = [
            _fake_window(),
            _fake_window(id="w2", app="Terminal", title="zsh", pid=7, focused=False),
        ]
        self._displays = [_fake_display()]

    @property
    def capabilities(self):
        return _fake_caps()

    def list_windows(self):
        return list(self._windows)

    def list_displays(self):
        return list(self._displays)


@pytest.fixture
def desktop(monkeypatch):
    fake = FakeSession()
    monkeypatch.setattr(computer_session, "get_desktop_session", lambda: fake)
    token = objects._run_context_var.set(objects.RunContext())
    try:
        yield objects.Desktop(fake)
    finally:
        objects._run_context_var.reset(token)


# ============== 值类型：JSON 原生 dict/list ==============

def test_windows_returns_json_native_dicts(desktop):
    windows = desktop.windows()
    assert isinstance(windows, list) and len(windows) == 2
    for w in windows:
        assert type(w) is dict
        assert set(w) == {"id", "app", "title", "pid", "x", "y", "width", "height", "focused"}
    # 模型按工具描述下标访问 + 整体可 JSON 序列化（无 PyO3 对象泄漏）
    assert windows[0]["id"] == "w1"
    json.dumps(windows)


def test_windows_filter_still_applies(desktop):
    safari = desktop.windows({"app": "safari"})   # 大小写不敏感子串（matchesFilter）
    assert [w["id"] for w in safari] == ["w1"]
    zsh = desktop.windows({"title": "ZSH"})
    assert [w["id"] for w in zsh] == ["w2"]


def test_displays_returns_dicts_with_wire_fields(desktop):
    displays = desktop.displays()
    assert isinstance(displays, list) and len(displays) == 1
    d = displays[0]
    assert type(d) is dict
    assert set(d) == {"id", "name", "x", "y", "width", "height", "scale",
                      "pixel_x", "pixel_y", "pixel_width", "pixel_height", "is_primary"}
    assert d["is_primary"] is True and d["scale"] == 2.0
    json.dumps(displays)


def test_capabilities_returns_dict_with_wire_fields(desktop):
    caps = desktop.capabilities()
    assert type(caps) is dict
    assert set(caps) == {"backend", "display_server", "capture", "input", "ax",
                         "background_window_input", "delivery_modes",
                         "capture_permission", "input_permission", "ax_permission",
                         "display_count"}
    assert caps["delivery_modes"] == ["background", "foreground"]
    json.dumps(caps)


# ============== 活句柄：仍是对象（属性访问 + 可调方法） ==============

def test_window_handle_is_object_not_dict(desktop):
    w = desktop.window("w1")
    assert isinstance(w, objects.Win) and not isinstance(w, dict)
    assert callable(w.click) and callable(w.screenshot)
    assert w.id == "w1" and w.app == "Safari"   # 句柄身份字段走属性访问


def test_focused_window_handle(desktop):
    w = desktop.focused_window()
    assert isinstance(w, objects.Win) and w.id == "w1"
    assert desktop.focused_window.__self__.focused_window is not None  # facade 绑定在位


# ============== run(code) 端到端形态（fake session，不碰真实桌面） ==============

def test_run_value_subscript_and_handle_survival(monkeypatch):
    fake = FakeSession()
    monkeypatch.setattr(computer_session, "get_desktop_session", lambda: fake)
    cs = computer_session.ComputerSession()

    # 值类型：下标访问可用，returnValue 字符串化为 JSON
    out = cs.run("desktop.windows()[0]['id']")
    assert out.strip() == "w1"
    out = cs.run("desktop.windows()[0]")
    assert '"id": "w1"' in out

    # 句柄跨 run 存活：run 1 取 Win，run 2 属性访问 + 方法在位
    cs.run("w = desktop.window('w2')")
    out = cs.run("display(w.id, w.app); callable(w.click)")
    # returnValue 走 JSON.stringify 语义（computer.ts:195-201）→ 小写 true
    assert "w2 Terminal" in out and "true" in out


# ============== 误用形态：可读 ComputerToolError（errors report surface failure） ==============

def _fake_ax_node(ref="e1", role="AXButton", title="Save"):
    return SimpleNamespace(ref=ref, role=role, native_role=role, title=title,
                           description=None, enabled=True, focused=False, child_count=0)


def test_windows_string_filter_rejected_with_usage(desktop):
    with pytest.raises(objects.ComputerToolError) as exc:
        desktop.windows("iTerm")   # 字符串当过滤器 → 不再泄漏 AttributeError
    msg = str(exc.value)
    assert "filter must be a dict" in msg
    assert "desktop.windows({'app': ...})" in msg   # 消息含正确写法示例


def test_windows_keyword_args_rejected_with_usage(desktop):
    with pytest.raises(objects.ComputerToolError) as exc:
        desktop.windows(app="iTerm")   # 关键字形式 → 不再泄漏 TypeError
    msg = str(exc.value)
    assert "no keyword arguments" in msg
    assert "desktop.windows({'app': 'Safari'})" in msg


def test_window_bad_selector_type_rejected_with_usage(desktop):
    with pytest.raises(objects.ComputerToolError) as exc:
        desktop.window(1.5)   # 既非 id 也非过滤器的类型 → 说明接受形态
    msg = str(exc.value)
    assert "selector must be a window id (str or int)" in msg
    assert "filter dict" in msg


def test_window_kwargs_only_rejected_with_usage(desktop):
    """只传关键字：selector 可选化后误用进得了函数体 → 守卫报错（不再泄漏 TypeError）。"""
    with pytest.raises(objects.ComputerToolError) as exc:
        desktop.window(app="iTerm")
    msg = str(exc.value)
    assert "no keyword arguments" in msg
    assert "desktop.window('21021')" in msg   # 两种正确形态都点出（id / 过滤对象）
    assert "desktop.window({'app': ...})" in msg


def test_window_missing_selector_rejected_with_usage(desktop):
    with pytest.raises(objects.ComputerToolError) as exc:
        desktop.window()   # 缺参 → 说明两种正确形态
    msg = str(exc.value)
    assert "requires a window id (str or int)" in msg
    assert "desktop.window('21021')" in msg and "desktop.window({'app': ...})" in msg


def test_window_int_id_normalized(monkeypatch):
    """int id 按 str() 归一后继续（list_targets 输出裸数字 id，模型抄数字是高频真实路径）。"""
    fake = FakeSession()
    fake._windows.append(_fake_window(id="21021", app="iTerm", title="zsh", pid=9, focused=False))
    monkeypatch.setattr(computer_session, "get_desktop_session", lambda: fake)
    token = objects._run_context_var.set(objects.RunContext())
    try:
        d = objects.Desktop(fake)
        w = d.window(21021)
        assert isinstance(w, objects.Win) and w.id == "21021" and w.app == "iTerm"
    finally:
        objects._run_context_var.reset(token)


def test_run_misuse_surfaces_readable_error(monkeypatch):
    """误用经 run(code) 端到端浮现：ComputerToolError 原样上抛（无原生码前缀 → 恢复句不追加）。"""
    fake = FakeSession()
    monkeypatch.setattr(computer_session, "get_desktop_session", lambda: fake)
    cs = computer_session.ComputerSession()
    with pytest.raises(objects.ComputerToolError) as exc:
        cs.run("desktop.windows('iTerm')")
    assert "filter must be a dict" in str(exc.value)


# ============== 句柄 repr：可读身份摘要（display(e) 不再打印 object at 0x…） ==============

def test_el_and_win_repr_readable(monkeypatch):
    fake = FakeSession()
    monkeypatch.setattr(computer_session, "get_desktop_session", lambda: fake)
    token = objects._run_context_var.set(objects.RunContext())
    try:
        d = objects.Desktop(fake)
        w = d.window("w1")
        assert repr(w) == "Win(id='w1', app='Safari', title='Niu')"
        el = objects.El(fake, _fake_ax_node())
        assert repr(el) == "El(role='AXButton', title='Save', ref='e1')"
        # 属性真实类型不变（repr 只是显示层）
        assert w.id == "w1" and el.ref == "e1"
    finally:
        objects._run_context_var.reset(token)
