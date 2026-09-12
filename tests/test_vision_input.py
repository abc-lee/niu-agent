"""vision-server input 工具测试（Phase 4 像素兜底输入 spec §3.3/§3.4）。

全 mock——禁真实输入/AX（niu_natives 经 sys.modules 注入 fake 模块整体替换）、
零真实键鼠事件。fake DesktopSession 用 MagicMock 记录调用，断言**调了哪个 Rust
方法、传了什么参**（delivery→delivery_mode、double_click→click(count=2)、path 原样透传）。

覆盖：
- 动作矩阵逐格（spec §3.3 表）：7 个 action 各 1 合法格 + 每类非法格
  （缺必填 / 混入被拒参数：double_click+count、drag+count、
  move/scroll/type/key+modifiers、未知 action、缺 target、path<2 点、keys 空）
- 校验先于 session：非法参数时 DesktopSession 不得被实例化（一被触碰就炸的 fake）
- 错误文案：InvalidCoordinateFrame→「先截图」、BackgroundUnavailable→指引、
  PermissionDenied→权限指引、WindowNotFound→重查 list_targets
- 坐标原值透传：x/y/path 原样传给 Rust，Python 侧不做任何换算（spec §3.4）
"""

import importlib
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp-servers" / "vision-server" / "src"))

import niu_vision_server  # noqa: E402,F401


@pytest.fixture(autouse=True)
def _restore_vision_binding():
    """每个测试结束后以真实 niu_natives 重新加载 niu_vision_server。

    测试经 sys.modules 注入 fake 后 reload——monkeypatch 先恢复 sys.modules
    （LIFO），本 fixture 的 post-yield 代码随后重载，确保 fake 绑定不泄漏。
    """
    yield
    importlib.reload(niu_vision_server)


def _install_fake_niu_natives(monkeypatch):
    """注入 fake niu_natives 并 reload vision 模块。返回 (module, session_mock)。"""
    fake_mod = types.ModuleType("niu_natives")
    session = MagicMock(name="DesktopSession-instance")
    fake_mod.DesktopSession = MagicMock(name="DesktopSession-class", return_value=session)
    monkeypatch.setitem(sys.modules, "niu_natives", fake_mod)
    m = importlib.reload(niu_vision_server)
    return m, session


def _install_boom_niu_natives(monkeypatch):
    """注入「一被实例化就炸」的 niu_natives。

    _get_session() 在 try 块之外——若校验错误地放行到 session，
    AssertionError 直接穿透（测试 error = 红），证明矩阵校验先于 session。
    """
    fake_mod = types.ModuleType("niu_natives")

    def _boom():
        raise AssertionError("DesktopSession 被触碰：参数校验必须先于 session 访问")

    fake_mod.DesktopSession = _boom
    monkeypatch.setitem(sys.modules, "niu_natives", fake_mod)
    return importlib.reload(niu_vision_server)


# ============== 动作矩阵合法格（spec §3.3 表逐格） ==============


class TestActionMatrixLegal:
    def test_click_default_background(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        result = m.input(target="w1", action="click", x=10, y=20)
        assert "已点击" in result
        # delivery 缺省 → Rust delivery_mode="background"（参数名 ≠ Rust 键名）
        session.click.assert_called_once_with("w1", 10, 20, {"delivery_mode": "background"})

    def test_click_full_options(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        m.input(target="w1", action="click", x=1, y=2, button="right", count=3,
                modifiers=["cmd"], delivery="foreground")
        session.click.assert_called_once_with(
            "w1", 1, 2,
            {"button": "right", "modifiers": ["cmd"], "delivery_mode": "foreground", "count": 3})

    def test_double_click_maps_to_click_count_2(self, monkeypatch):
        """Rust 无 double_click → click(count=2)；count 已在矩阵中拒绝。"""
        m, session = _install_fake_niu_natives(monkeypatch)
        result = m.input(target="w1", action="double_click", x=5, y=6)
        assert "已双击" in result
        session.click.assert_called_once_with("w1", 5, 6, {"delivery_mode": "background", "count": 2})

    def test_move_dispatches_move_mouse(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        m.input(target="w1", action="move", x=1, y=2)
        session.move_mouse.assert_called_once_with("w1", 1, 2, {"delivery_mode": "background"})

    def test_drag_list_points_passthrough(self, monkeypatch):
        """path 元素原样透传（[x,y] 序列形态），不做任何换算。"""
        m, session = _install_fake_niu_natives(monkeypatch)
        result = m.input(target="w1", action="drag", path=[[0, 0], [10, 10]])
        assert "已拖拽 2 点路径" in result
        session.drag.assert_called_once_with(
            "w1", [[0, 0], [10, 10]], {"delivery_mode": "background"})

    def test_drag_dict_points_passthrough(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        pts = [{"x": 1, "y": 2}, {"x": 3, "y": 4}]
        m.input(target="w1", action="drag", path=pts)
        session.drag.assert_called_once_with(
            "w1", [{"x": 1, "y": 2}, {"x": 3, "y": 4}], {"delivery_mode": "background"})

    def test_scroll_dispatches(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        m.input(target="w1", action="scroll", x=1, y=2, dx=0, dy=-3)
        session.scroll.assert_called_once_with(
            "w1", 1, 2, 0, -3, {"delivery_mode": "background"})

    def test_type_dispatches(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        result = m.input(target="w1", action="type", text="hello")
        assert "已键入 5 个字符" in result
        session.type_text.assert_called_once_with("w1", "hello", {"delivery_mode": "background"})

    def test_key_dispatches(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        result = m.input(target="w1", action="key", keys=["cmd+shift+p"])
        assert "已按键 cmd+shift+p" in result
        session.key_chord.assert_called_once_with(
            "w1", ["cmd+shift+p"], {"delivery_mode": "background"})


# ============== 动作矩阵非法格（缺必填 / 混入被拒参数 → 中文错误） ==============


class TestActionMatrixIllegal:
    def test_double_click_rejects_count(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        result = m.input(target="w1", action="double_click", x=1, y=2, count=3)
        assert "不接受参数 count" in result
        # 矩阵尾句逐格提示：列出该 action 全部被拒参数（count 在其中）
        assert "禁止传 path、dx、dy、text、keys、count" in result
        session.click.assert_not_called()

    def test_drag_rejects_count(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        result = m.input(target="w1", action="drag", path=[[0, 0], [1, 1]], count=2)
        assert "不接受参数 count" in result
        session.drag.assert_not_called()

    @pytest.mark.parametrize("action,kw", [
        ("move", {"x": 1, "y": 2}),
        ("scroll", {"x": 1, "y": 2, "dx": 0, "dy": -1}),
        ("type", {"text": "hi"}),
        ("key", {"keys": ["enter"]}),
    ])
    def test_modifiers_rejected(self, monkeypatch, action, kw):
        """modifiers 仅 click/drag 可选——move/scroll/type/key 混入必须报错。"""
        m, session = _install_fake_niu_natives(monkeypatch)
        result = m.input(target="w1", action=action, modifiers=["cmd"], **kw)
        assert "不接受参数 modifiers" in result
        assert not any(method.call_count for method in
                        (session.click, session.move_mouse, session.drag,
                         session.scroll, session.type_text, session.key_chord))

    def test_unknown_action(self, monkeypatch):
        m = _install_boom_niu_natives(monkeypatch)
        result = m.input(target="w1", action="hover")
        assert "action 须为 click/double_click/move/drag/scroll/type/key 之一" in result
        assert "'hover'" in result

    def test_missing_target(self, monkeypatch):
        m = _install_boom_niu_natives(monkeypatch)
        result = m.input(action="click", x=1, y=2)
        assert "input 需要 target" in result

    def test_click_missing_required(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        result = m.input(target="w1", action="click")
        assert "缺少必填参数 x、y" in result
        session.click.assert_not_called()

    @pytest.mark.parametrize("action,kw,missing", [
        ("move", {}, "x、y"),
        ("scroll", {"x": 1, "y": 2}, "dx、dy"),
        ("type", {}, "text"),
    ], ids=["move-missing-xy", "scroll-missing-dxdy", "type-missing-text"])
    def test_missing_required(self, monkeypatch, action, kw, missing):
        """缺必填逐格：错误须点名缺失参数（锁 _INPUT_MATRIX 的 required 元组）。"""
        m, session = _install_fake_niu_natives(monkeypatch)
        result = m.input(target="w1", action=action, **kw)
        assert f"缺少必填参数 {missing}" in result
        assert not any(method.call_count for method in
                        (session.click, session.move_mouse, session.drag,
                         session.scroll, session.type_text, session.key_chord))

    @pytest.mark.parametrize("extra,kw", [
        ("path", {"path": [[0, 0], [1, 1]]}),
        ("text", {"text": "hi"}),
    ], ids=["click+path", "click+text"])
    def test_click_rejects_unrelated_params(self, monkeypatch, extra, kw):
        """click 混入无关参数（path/text）→ 中文错误，不静默忽略。"""
        m, session = _install_fake_niu_natives(monkeypatch)
        result = m.input(target="w1", action="click", x=1, y=2, **kw)
        assert f"不接受参数 {extra}" in result
        session.click.assert_not_called()

    def test_drag_path_too_short(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        result = m.input(target="w1", action="drag", path=[[1, 2]])
        assert "path 须为至少 2 个点" in result
        session.drag.assert_not_called()

    def test_key_empty_keys(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        result = m.input(target="w1", action="key", keys=[])
        assert "keys 须为非空字符串数组" in result
        session.key_chord.assert_not_called()


# ============== 校验先于 session（非法参数不得触碰 DesktopSession） ==============


class TestValidationBeforeSession:
    @pytest.mark.parametrize("call", [
        lambda m: m.input(target="w1", action="double_click", x=1, y=2, count=3),
        lambda m: m.input(action="click", x=1, y=2),
        lambda m: m.input(target="w1", action="hover"),
        lambda m: m.input(target="w1", action="drag", path=[[1, 2]]),
        lambda m: m.input(target="w1", action="key", keys=[]),
    ], ids=["double_click+count", "missing-target", "unknown-action",
            "path<2", "empty-keys"])
    def test_illegal_params_never_touch_session(self, monkeypatch, call):
        m = _install_boom_niu_natives(monkeypatch)
        result = call(m)  # 若校验放行 → DesktopSession() 炸出 AssertionError（红）
        assert isinstance(result, str) and result.startswith(("错误", "input"))


# ============== 错误文案（Rust RuntimeError("{code}: …") → 中文指引） ==============


class TestErrorMessages:
    def _raise_on_click(self, session, msg):
        session.click.side_effect = RuntimeError(msg)

    def test_invalid_coordinate_frame_points_to_screenshot(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        self._raise_on_click(session, "InvalidCoordinateFrame: no frame for w1")
        result = m.input(target="w1", action="click", x=1, y=2)
        assert "该目标还没有截图" in result and "先调用 screenshot" in result

    def test_background_unavailable_guides_foreground(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        self._raise_on_click(session, "BackgroundUnavailable: full-screen space")
        result = m.input(target="w1", action="click", x=1, y=2)
        assert "无法在不打扰你的前提下投递" in result
        assert 'delivery="foreground"' in result

    def test_permission_denied_guides_tcc(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        self._raise_on_click(session, "PermissionDenied: TCC input monitoring missing")
        result = m.input(target="w1", action="click", x=1, y=2)
        assert "系统设置" in result and "勾选 Niu" in result

    def test_window_not_found_points_to_list_targets(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        self._raise_on_click(session, "WindowNotFound: w1 gone")
        result = m.input(target="w1", action="click", x=1, y=2)
        assert "窗口未找到" in result and "list_targets" in result


# ============== 坐标原值透传（spec §3.4：Python 侧不做任何换算） ==============


class TestCoordinatePassthrough:
    def test_click_float_coords_untouched(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        m.input(target=7, action="click", x=10.5, y=20)  # target int→str；x/y 原值
        session.click.assert_called_once_with("7", 10.5, 20, {"delivery_mode": "background"})

    def test_drag_float_path_untouched(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        m.input(target="w1", action="drag", path=[[1.5, 2.5], [3.5, 4.5]])
        session.drag.assert_called_once_with(
            "w1", [[1.5, 2.5], [3.5, 4.5]], {"delivery_mode": "background"})
