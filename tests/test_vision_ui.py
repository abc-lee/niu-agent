"""vision-server ui 工具测试（Phase 4 桌面语义操作 spec §3.3）。

全 mock——禁真实 AX 调用（niu_natives 经 sys.modules 注入 fake 模块整体替换）、
零真实输入。fake DesktopSession 的 ax_snapshot/ax_query/ax_focused 返回值用
types.SimpleNamespace **属性对象**还原 PyO3 #[pyclass] 形态（只能属性访问，
不是 dict——Phase 3 P0 回归锁）。

覆盖：
- 四用法判别逐格（spec §3.3 表）：ui()/ui(depth=2)/ref 无 action → 错误；
  ui(action="focused_element") 合法；target+action / target+ref / target+find+action /
  未知 action → 错误（含回归锁：target+action="press" 曾静默降级为读结构）
- 用法②漏判 value 的回归锁：target+find+value → 用法错误
- 用法③ value 仅 set_value：ref+action="press"+value → 报错且不得返回假成功
- 读结构：ax_snapshot 树文本/节点数/截断标志 + depth/limit → max_depth/max_nodes 映射
- 找元素：命中 / 0 命中 / 多命中 / find 非 dict / limit 映射
- 动作分发：press→ax_perform、set_value→ax_set_value、focus→ax_focus、click→ax_click
  （断言调了哪个方法、传了什么参）
- 焦点元素：有 → 格式化；None → 「当前没有焦点元素」文案
- 错误映射：Rust RuntimeError("{code}: …") → 中文文案（StaleRef/PermissionDenied/
  AxUnsupported/WindowNotFound 兜底/无 code 兜底），异常不穿透 MCP 层
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

    _get_session() 在 try 块之外——若校验/判别错误地放行到 session，
    AssertionError 直接穿透（测试 error = 红），证明判别先于 session。
    """
    fake_mod = types.ModuleType("niu_natives")

    def _boom():
        raise AssertionError("DesktopSession 被触碰：用法判别必须先于 session 访问")

    fake_mod.DesktopSession = _boom
    monkeypatch.setitem(sys.modules, "niu_natives", fake_mod)
    return importlib.reload(niu_vision_server)


def _node(**fields):
    """实机形态的 AX 元素属性对象（PyO3 #[pyclass]——只能属性访问，不是 dict）。"""
    base = {
        "role": "Button", "title": "OK", "ref": "e1",
        "enabled": True, "focused": False,
        "x": 10, "y": 20, "width": 80, "height": 24,
        "actions": ["AXPress"],
    }
    base.update(fields)
    return types.SimpleNamespace(**base)


_NODE_LINE = ('Button "OK" [ref=e1] enabled focused '
              'bounds=(10, 20, 80x24) 全局逻辑坐标 actions=AXPress')


# ============== 四用法判别（spec §3.3 表，恰好一种） ==============


class TestUsageDiscrimination:
    def test_bare_call_rejected(self, monkeypatch):
        m = _install_boom_niu_natives(monkeypatch)
        assert m.ui() == m._UI_USAGE_ERROR

    def test_depth_only_rejected(self, monkeypatch):
        m = _install_boom_niu_natives(monkeypatch)
        assert m.ui(depth=2) == m._UI_USAGE_ERROR

    def test_ref_without_action_rejected(self, monkeypatch):
        m = _install_boom_niu_natives(monkeypatch)
        assert m.ui(ref="e1") == m._UI_USAGE_ERROR

    def test_focused_element_legal(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        session.ax_focused.return_value = _node()
        result = m.ui(action="focused_element")
        assert "当前焦点元素" in result
        session.ax_focused.assert_called_once_with()

    def test_focused_element_with_target_rejected(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        result = m.ui(action="focused_element", target="w1")
        assert result == m._UI_USAGE_ERROR
        session.ax_focused.assert_not_called()

    def test_target_action_press_rejected(self, monkeypatch):
        """回归锁：ui(target=…, action="press") 曾静默降级为读结构——必须报用法错误。"""
        m, session = _install_fake_niu_natives(monkeypatch)
        result = m.ui(target="w1", action="press")
        assert result == m._UI_USAGE_ERROR
        session.ax_snapshot.assert_not_called()

    def test_target_ref_rejected(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        result = m.ui(target="w1", ref="e1")
        assert result == m._UI_USAGE_ERROR
        session.ax_snapshot.assert_not_called()

    def test_target_find_action_rejected(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        result = m.ui(target="w1", find={"role": "button"}, action="press")
        assert result == m._UI_USAGE_ERROR
        session.ax_query.assert_not_called()

    def test_unknown_action_rejected(self, monkeypatch):
        m = _install_boom_niu_natives(monkeypatch)
        assert m.ui(ref="e1", action="hover") == m._UI_USAGE_ERROR
        assert m.ui(action="hover") == m._UI_USAGE_ERROR


class TestUsage2ValueRegression:
    def test_find_with_value_rejected(self, monkeypatch):
        """回归锁：用法②漏判 value——target+find+value 必须报用法错误，不得查询。"""
        m, session = _install_fake_niu_natives(monkeypatch)
        result = m.ui(target="w1", find={"role": "button"}, value="x")
        assert result == m._UI_USAGE_ERROR
        session.ax_query.assert_not_called()


# ============== 用法①：读结构（ax_snapshot） ==============


class TestReadStructure:
    def test_snapshot_basic(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        session.ax_snapshot.return_value = types.SimpleNamespace(
            node_count=3, truncated=False, text="Window \"Niu\" [ref=e0]\n  Button")
        result = m.ui(target="w1")
        assert result == "节点数: 3\nWindow \"Niu\" [ref=e0]\n  Button"
        session.ax_snapshot.assert_called_once_with("w1", None)

    def test_depth_limit_mapped_to_opts(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        session.ax_snapshot.return_value = types.SimpleNamespace(
            node_count=1, truncated=False, text="Window")
        m.ui(target="w1", depth=2, limit=100)
        session.ax_snapshot.assert_called_once_with("w1", {"max_depth": 2, "max_nodes": 100})

    def test_truncated_flag_surfaced(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        session.ax_snapshot.return_value = types.SimpleNamespace(
            node_count=800, truncated=True, text="Window")
        result = m.ui(target="w1", depth=2)
        assert "已截断" in result

    def test_bad_depth_rejected(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        assert "depth 须为正整数" in m.ui(target="w1", depth=0)
        assert "depth 须为正整数" in m.ui(target="w1", depth=2.5)
        session.ax_snapshot.assert_not_called()

    def test_limit_over_5000_rejected(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        assert "limit 不能超过 5000" in m.ui(target="w1", limit=6000)
        session.ax_snapshot.assert_not_called()


# ============== 用法②：找元素（ax_query） ==============


class TestFindElement:
    def test_hit_single(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        session.ax_query.return_value = [_node(focused=True)]
        result = m.ui(target="w1", find={"role": "Button", "title": "OK"})
        assert "命中 1 个：" in result
        assert "- " + _NODE_LINE in result
        # bounds 是全局逻辑坐标的警示必须随结果给出（防直接喂给 input）
        assert "全局逻辑坐标" in result and "不可直接喂给 input" in result
        session.ax_query.assert_called_once_with("w1", {"role": "Button", "title": "OK"})

    def test_zero_hits(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        session.ax_query.return_value = []
        result = m.ui(target="w1", find={"role": "MenuItem"})
        assert "未找到匹配的元素" in result

    def test_multi_hits(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        session.ax_query.return_value = [_node(ref="e1"), _node(ref="e2", title="Cancel")]
        result = m.ui(target="w1", find={"role": "Button"})
        assert "命中 2 个：" in result
        assert "[ref=e1]" in result and "[ref=e2]" in result

    def test_find_not_dict_rejected(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        assert "find 须为 dict" in m.ui(target="w1", find="button")
        session.ax_query.assert_not_called()

    def test_top_level_limit_maps_to_query(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        session.ax_query.return_value = []
        m.ui(target="w1", find={"role": "Button"}, limit=5)
        session.ax_query.assert_called_once_with("w1", {"role": "Button", "limit": 5})


# ============== 用法③：对元素动作（分发 + value 仅 set_value） ==============


class TestActionDispatch:
    def test_press_dispatches_ax_perform(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        result = m.ui(ref="e1", action="press")
        assert result == "已对元素 e1 执行动作 press。"
        session.ax_perform.assert_called_once_with("e1", "press")

    def test_set_value_dispatches_ax_set_value(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        m.ui(ref="e1", action="set_value", value=42)  # 非 str 须转 str 透传
        session.ax_set_value.assert_called_once_with("e1", "42")

    def test_set_value_requires_value(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        assert "set_value 需要 value" in m.ui(ref="e1", action="set_value")
        session.ax_set_value.assert_not_called()

    def test_focus_dispatches_ax_focus(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        m.ui(ref="e1", action="focus")
        session.ax_focus.assert_called_once_with("e1")

    def test_click_dispatches_ax_click(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        m.ui(ref="e1", action="click")
        session.ax_click.assert_called_once_with("e1")

    def test_press_with_value_rejected_no_fake_success(self, monkeypatch):
        """value 仅 set_value——press+value 必须报错，且不得返回「已执行」假成功。"""
        m, session = _install_fake_niu_natives(monkeypatch)
        result = m.ui(ref="e1", action="press", value="x")
        assert "value 仅 set_value 可用" in result
        assert "已对元素" not in result
        session.ax_perform.assert_not_called()


# ============== 用法④：焦点元素（ax_focused） ==============


class TestFocusedElement:
    def test_focused_node_formatted(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        session.ax_focused.return_value = _node(focused=True)
        result = m.ui(action="focused_element")
        assert result == "当前焦点元素：\n- " + _NODE_LINE

    def test_focused_none_message(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        session.ax_focused.return_value = None
        result = m.ui(action="focused_element")
        assert "当前没有焦点元素" in result


# ============== 错误映射（Rust RuntimeError("{code}: …") → 中文文案） ==============


class TestErrorMapping:
    def _raise_on_press(self, session, msg):
        session.ax_perform.side_effect = RuntimeError(msg)

    def test_stale_ref(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        self._raise_on_press(session, "StaleRef: e1 is stale (generation 3)")
        result = m.ui(ref="e1", action="press")
        assert "该元素引用已过期" in result and "重新调用 ui" in result

    def test_permission_denied(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        self._raise_on_press(session, "PermissionDenied: TCC AX trust missing")
        result = m.ui(ref="e1", action="press")
        assert "辅助功能" in result and "系统设置" in result

    def test_ax_unsupported(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        self._raise_on_press(session, "AxUnsupported: app has no AX tree")
        result = m.ui(ref="e1", action="press")
        assert "不支持无障碍" in result and "screenshot + input" in result

    def test_window_not_found_fallback(self, monkeypatch):
        """ui 侧无 WindowNotFound 专句 → 走兜底且保留原始 code。"""
        m, session = _install_fake_niu_natives(monkeypatch)
        self._raise_on_press(session, "WindowNotFound: w1 gone")
        result = m.ui(ref="e1", action="press")
        assert result.startswith("ui 操作失败：") and "WindowNotFound" in result

    def test_plain_runtime_error_fallback(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        self._raise_on_press(session, "boom: unexpected")
        result = m.ui(ref="e1", action="press")
        assert result == "ui 操作失败：boom: unexpected"
