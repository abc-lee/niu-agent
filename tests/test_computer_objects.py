"""computer 对象模型形态契约：值类型 = JSON 原生 dict/list；活句柄 = 对象。

不依赖真实桌面：fake session 用属性访问对象（SimpleNamespace，模拟 PyO3 pyclass
的 `#[pyo3(get)]` 行为），monkeypatch `agent.computer.session.get_desktop_session`
绕开 niu_natives / vision-server。锁住两类形态：

- `desktop.windows()/displays()/capabilities()` → dict/list（模型按工具描述
  `w["id"]` 下标访问必须可用，且整体可 json.dumps）；
- `desktop.window(...)` → Win 句柄对象（属性访问 + 可调方法，跨 run 存活）。
"""
import json
import threading
import time
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


# ============== run-scope 重注入（P1-2）：误写后下一 run 自愈 ==============

def test_run_scope_reinjected_after_overwrite(monkeypatch):
    """用户代码覆写 desktop/display → 下一 run 重注入 pristine 对象（上游 setRunScope，
    runtime.ts:237-240）。修复前：一次误写后后续所有 run 全部 AttributeError，直到重启。"""
    fake = FakeSession()
    monkeypatch.setattr(computer_session, "get_desktop_session", lambda: fake)
    cs = computer_session.ComputerSession()
    cs.run("desktop = 42")                              # 误写：覆写 desktop 句柄
    out = cs.run("desktop.windows()[0]['id']")          # 修复前：AttributeError 永久
    assert out.strip() == "w1"
    cs.run("display = 7")                               # 误写：覆写 display
    out = cs.run("display('hi'); 'ok'")
    assert "hi" in out and "ok" in out


# ============== wait 预算约束（P1-3）+ busy 事实文案 ==============

def test_numeric_wait_clamped_to_run_budget(monkeypatch):
    """数字 wait(ms) 受 run 预算上限约束。修复前：无上限 sleep → join 超时抛错，
    后台线程持锁继续睡满（会话永久 busy）。"""
    fake = FakeSession()
    monkeypatch.setattr(computer_session, "get_desktop_session", lambda: fake)
    cs = computer_session.ComputerSession()
    t0 = time.monotonic()
    out = cs.run("wait(60_000)", timeout=1)   # 预算 1s → wait 被 clamp 到 budget_bound≈1ms
    assert time.monotonic() - t0 < 0.9        # 修复前：抛 "timed out after 1000ms"
    assert out == ""


def test_busy_message_reports_facts_and_hang_escalation(monkeypatch):
    """挂死 run 持锁 → busy 错误文本带事实（已运行时长/预算）；显著超阈值
    （≥5× 预算且 ≥30s）→ 升级为"需重启"文案。"""
    fake = FakeSession()
    monkeypatch.setattr(computer_session, "get_desktop_session", lambda: fake)
    cs = computer_session.ComputerSession()
    cs._ensure_namespace()
    assert cs._lock.acquire(blocking=False)   # 模拟挂死 run 持锁
    try:
        # 未超阈值：只报事实，不升级（2s 前启动、预算 300s）
        cs._active_run = (threading.current_thread(), time.monotonic() - 2.0, 300.0)
        with pytest.raises(objects.ComputerToolError) as exc:
            cs.run("desktop.windows()", timeout=1)
        msg = str(exc.value)
        assert "Computer worker is busy" in msg
        assert "running for 2s" in msg and "budget 300s" in msg
        assert "restart" not in msg
        # 显著超阈值（40s > max(30s, 5×1s)）：升级重启文案
        cs._active_run = (threading.current_thread(), time.monotonic() - 40.0, 1.0)
        with pytest.raises(objects.ComputerToolError) as exc:
            cs.run("desktop.windows()", timeout=1)
        msg = str(exc.value)
        assert "running for 40s" in msg and "budget 1s" in msg
        assert "previous run hung — session requires restart (Niu 重启后可恢复)" in msg
    finally:
        cs._lock.release()


# ============== 末尾赋值返回值（P2-5） ==============

def test_run_trailing_assignment_returns_value(monkeypatch):
    """末尾单目标赋值 → returnValue（上游 JS `w = 42` 是 ExpressionStatement 会返回值；
    修复前 Python Assign 不是 Expr → run("w = 42") 返回空）。"""
    fake = FakeSession()
    monkeypatch.setattr(computer_session, "get_desktop_session", lambda: fake)
    cs = computer_session.ComputerSession()
    assert cs.run("w = 42").strip() == "42"        # 修复前：空
    assert cs.run("w: int = 7").strip() == "7"     # AnnAssign
    assert cs.run("a, b = 1, 2") == ""             # 多目标/解包不处理 → 无 returnValue


# ============== wait 垃圾值回退默认（P2-8） ==============

def test_wait_garbage_timeout_falls_back_to_default(monkeypatch):
    """wait(predicate, timeout=负数/NaN/非数值) → 按省略处理回退默认再 clamp 预算。
    修复前：min(-5, budget) → 立即抛 "timed out after -5ms"（上游 resolvePredicateTimeout
    :320-325 对垃圾值回退默认）。"""
    fake = FakeSession()
    monkeypatch.setattr(computer_session, "get_desktop_session", lambda: fake)
    cs = computer_session.ComputerSession()
    with pytest.raises(objects.ComputerToolError) as exc:
        cs.run("wait(lambda: False, timeout=-5)", timeout=1)   # 预算 1s → budget_bound≈1ms
    assert "timed out after -5ms" not in str(exc.value)
    assert "timed out after 1ms" in str(exc.value)             # 回退默认后 clamp 预算
    for bad in ("float('nan')", "'60'"):                       # NaN/非数值同样回退（修复前泄漏 TypeError）
        with pytest.raises(objects.ComputerToolError):
            cs.run(f"wait(lambda: False, timeout={bad})", timeout=1)


# ============== assert 括号写法 AST 守卫 ==============

def test_assert_paren_misuse_rejected_by_ast_guard(monkeypatch):
    """JS 式 `assert(False, 'boom')` → Python 解析为非空元组恒真（断言静默通过）。
    修复前：run 正常返回（仅 stderr SyntaxWarning）；修复后：AST 守卫抛 ComputerToolError。"""
    fake = FakeSession()
    monkeypatch.setattr(computer_session, "get_desktop_session", lambda: fake)
    cs = computer_session.ComputerSession()
    with pytest.raises(objects.ComputerToolError) as exc:
        cs.run("assert(False, 'boom')")
    msg = str(exc.value)
    assert "tuple" in msg and "assert cond" in msg   # 文案说明元组恒真 + 正确写法


def test_assert_guard_catches_nested_function_body(monkeypatch):
    """守卫递归遍历（含函数体内）——不依赖 f() 被调用。"""
    fake = FakeSession()
    monkeypatch.setattr(computer_session, "get_desktop_session", lambda: fake)
    cs = computer_session.ComputerSession()
    with pytest.raises(objects.ComputerToolError):
        cs.run("def f():\n    assert(False, 'x')\nf()")


def test_assert_statement_forms_still_work(monkeypatch):
    """正确 Python 写法不受守卫影响：语句式 `assert False, 'boom'` 照常抛 AssertionError；
    真断言不报错。"""
    fake = FakeSession()
    monkeypatch.setattr(computer_session, "get_desktop_session", lambda: fake)
    cs = computer_session.ComputerSession()
    with pytest.raises(AssertionError):
        cs.run("assert False, 'boom'")
    assert cs.run("assert 1 == 1") == ""


# ============== do_computer 未知参数拒绝（上游 schema "+": "reject"）==============

def test_do_computer_rejects_unknown_parameters(monkeypatch):
    """未知参数（如 readonly）→ 修复前静默忽略照常执行；修复后明确报错列出未知键 + 合法键。
    _index（框架注入）放行，合法参数照常工作。"""
    import agent.handler as handler_mod
    fake = FakeSession()
    monkeypatch.setattr(computer_session, "get_desktop_session", lambda: fake)
    cs = computer_session.ComputerSession()
    dummy = SimpleNamespace(_get_computer_session=lambda: cs)
    outcome = handler_mod.NiuHandler.do_computer(
        dummy, {"code": "1", "readonly": True}, None)
    data = str(outcome.data)
    assert "Unknown" in data and "readonly" in data       # 点名未知键
    assert "code" in data and "read_only" in data         # 给出合法键
    ok = handler_mod.NiuHandler.do_computer(
        dummy, {"code": "1", "_index": 3}, None)
    assert str(ok.data).strip() == "1"                    # _index 放行，正常执行
