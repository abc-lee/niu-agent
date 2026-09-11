"""vision-server list_targets 工具测试（plan 2026-09-10-vision-aux-tools.md §3.1/§3.3 / T1）。

全 mock——禁真实 LLM、禁真实抓屏（niu_natives 经 sys.modules 注入 fake 模块
整体替换）、零真实 ~/.niu 读写（HOME 指向 tmp_path）。沿用
test_vision_screenshot.py 的 fixture 范本：module 级 HOME 隔离 +
_install_fake_niu_natives + reload 恢复。

覆盖（plan §6 用例 1-6 + 13 的 list_targets 部分）：
- 格式化：多显示器 + 多窗口 + [应用在前台] 标记 + (主屏) + id 原样 + 无标题 (无标题)
- 截断警告：48 个窗口 → 含警告；47 个 → 无警告（MAX_LISTED_WINDOWS 启发式）
- 降级分级：niu_natives=None → _UNAVAILABLE_MSG；零窗口 = 正常态（不返回错误串）；
  list_displays 抛错/空列表 → 明确错误串且非 _UNAVAILABLE_MSG；
  list_windows 抛错 → 明确中文错误串、不抛异常
- 无前台应用（focused 全 False）→ 省略「前台应用」行、不输出 None、不崩
- 注册契约：yaml tools 键 == 模块 schema name == 函数名（三工具）；
  ToolRegistry.register_server → get_static_tools 含 vision-server/list_targets
"""

import importlib
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp-servers" / "vision-server" / "src"))

import niu_vision_server  # noqa: E402,F401  (确保模块进 sys.modules 并绑定真实/降级态 niu_natives)

_REPO_ROOT = Path(__file__).resolve().parent.parent

_UNAVAILABLE_MSG = "截图能力不可用（niu_natives 未安装/平台不支持）"


@pytest.fixture(autouse=True, scope="module")
def _home_isolated(tmp_path_factory):
    """模块级 HOME 隔离：任何落盘都写进 tmp，绝不写真 ~/.niu。"""
    old = os.environ.get("HOME")
    home = tmp_path_factory.mktemp("vision-home")
    os.environ["HOME"] = str(home)
    yield home
    if old is None:
        os.environ.pop("HOME", None)
    else:
        os.environ["HOME"] = old


@pytest.fixture(autouse=True)
def _restore_vision_binding():
    """每个测试结束后以真实 niu_natives 重新加载 niu_vision_server。

    测试经 sys.modules 注入 fake 后 reload——monkeypatch 先恢复 sys.modules
    （LIFO），本 fixture 的 post-yield 代码随后重载，确保 fake 绑定不泄漏。
    """
    yield
    importlib.reload(niu_vision_server)


def _install_fake_niu_natives(monkeypatch):
    """注入 fake niu_natives 并 reload vision 模块。返回 (module, session_mock)。

    list_displays/list_windows 的返回值由各用例自行设定（MagicMock 默认值不可迭代）。
    """
    fake_mod = types.ModuleType("niu_natives")
    session = MagicMock(name="DesktopSession-instance")
    fake_mod.DesktopSession = MagicMock(name="DesktopSession-class", return_value=session)
    monkeypatch.setitem(sys.modules, "niu_natives", fake_mod)
    m = importlib.reload(niu_vision_server)
    return m, session


def _display(**fields):
    """实机形态的显示器属性对象（字段对齐 niu-natives types.rs DesktopDisplay）。

    真实 session.list_displays() 返回 PyO3 #[pyclass] 对象——只能属性访问，
    fake 必须同形态，否则 mock 全绿、真实全挂（P0 回归锁）。
    """
    base = {
        "id": "display-1",
        "name": "Built-in Retina Display",
        "x": 0, "y": 0, "width": 1680, "height": 1050,
        "scale": 2.0,
        "pixel_x": 0, "pixel_y": 0, "pixel_width": 3360, "pixel_height": 2100,
        "is_primary": True,
    }
    base.update(fields)
    return types.SimpleNamespace(**base)


def _window(i, app="iTerm2", title="", focused=False, **fields):
    """实机形态的窗口属性对象（字段对齐 niu-natives types.rs DesktopWindow）。

    id 用 str——真实 DesktopWindow.id 是字符串（如 '13250'），fake 必须同型。
    """
    base = {
        "id": str(i), "app": app, "title": title, "pid": 100 + i,
        "x": 0, "y": 0, "width": 800, "height": 600, "focused": focused,
    }
    base.update(fields)
    return types.SimpleNamespace(**base)


# ============== 格式化（plan §6 用例 1） ==============


class TestFormatting:
    def test_displays_windows_and_markers(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        session.list_displays.return_value = [
            _display(),
            _display(id="display-2", name="External Monitor", x=1680, y=0,
                     width=1920, height=1080, scale=1.0, is_primary=False),
        ]
        session.list_windows.return_value = [
            _window(13255, title="", focused=True, width=1680, height=68),
            _window(13249, title="π NIU开发团队", focused=True, width=1680, height=1050),
            _window(74, app="通知中心", x=8, y=392, width=180, height=180),
        ]

        result = m.list_targets()

        # 前台应用行（focused 为 PID 级：两个 iTerm2 窗口同 True，去重取第一个）
        assert "前台应用: iTerm2" in result
        # 显示器段：序号 + 逻辑尺寸 + 缩放 + 逻辑位置 + (主屏)；副屏无 (主屏)
        assert "显示器 2 台：" in result
        assert "- [1] Built-in Retina Display 1680x1050 (缩放2.0) 逻辑位置 (0,0) (主屏)" in result
        assert "- [2] External Monitor 1920x1080 (缩放1.0) 逻辑位置 (1680,0)" in result
        # 窗口段：id 原样输出（opaque，不解析）+ 无标题占位 + [应用在前台] 标记
        assert "窗口 3 个：" in result
        assert '- id=13255 iTerm2 "(无标题)" 1680x68 @(0,0) [应用在前台]' in result
        assert '- id=13249 iTerm2 "π NIU开发团队" 1680x1050 @(0,0) [应用在前台]' in result
        assert '- id=74 通知中心 "(无标题)" 180x180 @(8,392)' in result
        # 非前台窗口行不带标记
        lines = [l for l in result.splitlines() if "id=74" in l]
        assert len(lines) == 1 and "[应用在前台]" not in lines[0]


# ============== 截断警告（plan §6 用例 2） ==============


class TestTruncationWarning:
    def _run(self, monkeypatch, n):
        m, session = _install_fake_niu_natives(monkeypatch)
        session.list_displays.return_value = [_display()]
        session.list_windows.return_value = [
            _window(i, app="App", title=f"t{i}") for i in range(1, n + 1)
        ]
        return m.list_targets()

    def test_48_windows_appends_warning(self, monkeypatch):
        result = self._run(monkeypatch, 48)
        assert "已达显示上限 48" in result
        assert 'screenshot(target="screen")' in result

    def test_47_windows_no_warning(self, monkeypatch):
        result = self._run(monkeypatch, 47)
        assert "已达显示上限" not in result


# ============== 降级分级（plan §6 用例 3-5） ==============


class TestDegradation:
    def test_niu_natives_missing_returns_unavailable_msg(self, monkeypatch):
        """niu_natives=None → _UNAVAILABLE_MSG（该常量仅此一档使用）。"""
        monkeypatch.setitem(sys.modules, "niu_natives", None)
        m = importlib.reload(niu_vision_server)
        assert m.niu_natives is None
        assert m.list_targets() == _UNAVAILABLE_MSG

    def test_zero_windows_is_normal_listing(self, monkeypatch):
        """零窗口 = 正常态（停在桌面时 Finder 桌面元素被排除）——绝不返回错误串。"""
        m, session = _install_fake_niu_natives(monkeypatch)
        session.list_displays.return_value = [_display()]
        session.list_windows.return_value = []

        result = m.list_targets()

        assert "显示器 1 台：" in result
        assert "- [1] Built-in Retina Display" in result
        assert "窗口 0 个" in result
        assert 'screenshot(target="screen")' in result  # 仍可截整屏提示
        assert "失败" not in result and "不可用" not in result

    def test_list_displays_exception_returns_specific_error(self, monkeypatch):
        """两后端空结果路径即 Err（capture_failed）——明确错误串，非 _UNAVAILABLE_MSG。"""
        m, session = _install_fake_niu_natives(monkeypatch)
        session.list_displays.side_effect = Exception("no active displays")

        result = m.list_targets()

        assert result != _UNAVAILABLE_MSG
        assert "无法枚举显示器" in result
        assert "no active displays" in result

    def test_list_displays_empty_returns_specific_error(self, monkeypatch):
        """防御分支：返回空列表同样走明确错误串（非 _UNAVAILABLE_MSG）。"""
        m, session = _install_fake_niu_natives(monkeypatch)
        session.list_displays.return_value = []

        result = m.list_targets()

        assert result != _UNAVAILABLE_MSG
        assert "无法枚举显示器" in result

    def test_list_windows_exception_returns_error_string_not_raise(self, monkeypatch):
        """未授权录屏时 list_windows 直接抛 Err——明确中文错误串，不得穿透到 MCP 层。"""
        m, session = _install_fake_niu_natives(monkeypatch)
        session.list_displays.return_value = [_display()]
        session.list_windows.side_effect = Exception("permission denied")

        result = m.list_targets()  # 不抛异常

        assert "无法枚举窗口" in result
        assert "permission denied" in result


# ============== 无前台应用（plan §6 用例 6） ==============


class TestNoForegroundApp:
    def test_all_unfocused_omits_front_app_line(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        session.list_displays.return_value = [_display()]
        session.list_windows.return_value = [
            _window(1, app="Finder", title="桌面"),
            _window(2, app="Safari", title="首页"),
        ]

        result = m.list_targets()

        assert "前台应用" not in result
        assert "None" not in result
        assert "窗口 2 个：" in result


# ============== 真实形态回归锁（P0：PyO3 属性对象，非 dict） ==============


class TestRealShapeObjects:
    """真实 session.list_windows()/list_displays() 返回 PyO3 #[pyclass] 对象——
    只能属性访问、无 .get()。本用例用纯属性类（连 dict 接口都没有）模拟该形态，
    断言 list_targets() 仍正常输出；修复前此用例必红
    （'...DesktopWindow...' object has no attribute 'get'）。
    """

    def test_attribute_only_objects_produce_full_listing(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)

        class FakePyO3Object:
            """模拟 #[pyclass] + #[pyo3(get)]：仅属性访问，无字典接口。"""
            def __init__(self, **kw):
                self.__dict__.update(kw)

        session.list_displays.return_value = [FakePyO3Object(**{
            "id": "display-1", "name": "Built-in Retina Display",
            "x": 0, "y": 0, "width": 1680, "height": 1050, "scale": 2.0,
            "pixel_x": 0, "pixel_y": 0, "pixel_width": 3360, "pixel_height": 2100,
            "is_primary": True,
        })]
        session.list_windows.return_value = [
            FakePyO3Object(id="13255", app="iTerm2", title="", pid=1, x=0, y=0,
                           width=1680, height=68, focused=True),
            FakePyO3Object(id="74", app="通知中心", title=None, pid=2, x=8, y=392,
                           width=180, height=180, focused=False),
        ]

        result = m.list_targets()

        assert "显示器 1 台：" in result
        assert "- [1] Built-in Retina Display 1680x1050 (缩放2.0) 逻辑位置 (0,0) (主屏)" in result
        assert "窗口 2 个：" in result
        assert '- id=13255 iTerm2 "(无标题)" 1680x68 @(0,0) [应用在前台]' in result
        assert 'id=74 通知中心' in result
        assert "前台应用: iTerm2" in result
        assert "失败" not in result and "不可用" not in result


# ============== 注册契约（plan §6 用例 13） ==============


class TestRegistrationContract:
    def test_yaml_tools_match_schemas_and_functions(self):
        """R8 键名契约：yaml tools 键 == 模块 schema name == 函数名（三工具）。"""
        cfg = yaml.safe_load(
            (_REPO_ROOT / "config" / "mcp-servers.yaml").read_text(encoding="utf-8")
        )
        entry = cfg["vision-server"]
        assert entry["tools"]["list_targets"]["visibility"] == "static"  # 显式 static

        schemas = {s["name"] for s in niu_vision_server.get_tool_schemas()}
        assert set(entry["tools"]) == {"screenshot", "list_targets", "analyze_image"} == schemas
        assert callable(getattr(niu_vision_server, "list_targets"))
        assert callable(getattr(niu_vision_server, "analyze_image"))

    def test_list_targets_enters_registry_static_tools(self):
        """真实 ToolRegistry.register_server → list_targets 进 get_static_tools——
        runner._assemble_tools_schema 自动遍历该列表，主 Agent 零代码改动可见。"""
        from agent.tool_registry import ToolRegistry

        cfg = yaml.safe_load(
            (_REPO_ROOT / "config" / "mcp-servers.yaml").read_text(encoding="utf-8")
        )
        registry = ToolRegistry()
        assert (
            registry.register_server(
                "vision-server", niu_vision_server, cfg["vision-server"]["tools"]
            )
            is True
        )
        static_tools = registry.get_static_tools()
        assert "vision-server/list_targets" in static_tools
        assert "vision-server/screenshot" in static_tools
        assert "vision-server/analyze_image" in static_tools
