"""vision-server screenshot 工具测试（可视化功能 plan v0.5.2 §4-V5 / T5）。

全 mock——禁真实 LLM、禁真实抓屏（niu_natives 经 sys.modules 注入 fake 模块
整体替换）、零真实 ~/.niu 读写（HOME 指向 tmp_path）。

覆盖：
- 三形态映射：screen→capture("desktop") / window→capture(window_id) /
  region→capture("desktop", caps, (x,y,w,h))，统一 max_width=1280 降采样
- 落盘 ~/.niu/tmp/screenshot_<ts>.png + 纯绝对路径返回（无图标记，
  plan 2026-09-11-vision-channel-refactor D-A）+ 尺寸/显示器元数据
- region_ratio（plan §3.2 / 用例 7-12）：换算正确性（单屏/双屏含负坐标）、
  target 联动（screen/window 两分支）、与绝对坐标互斥/均未给、越界退化、
  形态非法（不抛 ValueError）、非有限值 NaN/±inf（绕过比较式越界检查）、
  list_displays 抛错/空列表降级（非 _UNAVAILABLE_MSG）
- niu_natives 缺失降级（R11）：import 失败分支 → 模块照常可 import +
  screenshot 返回明确错误串，不炸 Niu 启动
- 注册契约（R8）：REQUIRED_SERVERS 含 vision-server；yaml 显式 static +
  三处键名逐字符一致（schema name == yaml tools 键 == 模块函数名）
- 主 Agent schema 出现（plan §8-⑤）：真实 ToolRegistry.register_server →
  screenshot 进 get_static_tools（runner._assemble_tools_schema 自动遍历）
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
from agent import mcp_loader  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parent.parent

_FAKE_PNG = b"\x89PNG\r\n\x1a\nfake-png-bytes"

_UNAVAILABLE_MSG = "截图能力不可用（niu_natives 未安装/平台不支持）"


@pytest.fixture(autouse=True, scope="module")
def _home_isolated(tmp_path_factory):
    """模块级 HOME 隔离：任何用例经真实 _save_png 落盘都写进 tmp，绝不写真 ~/.niu。

    Path.home() 调用时读 HOME 环境变量——模块级设一次即覆盖全部用例
    （TestCaptureMapping 等映射用例也走真实 _save_png 落盘）。yield 隔离后的 home 目录。
    """
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
    （LIFO：依赖项 teardown 晚于本 fixture 的 yield 前逻辑、早于 post-yield），
    本 fixture 的 post-yield 代码随后重载，确保 fake 绑定不泄漏给后续测试。
    """
    yield
    importlib.reload(niu_vision_server)


def _install_fake_niu_natives(monkeypatch):
    """注入 fake niu_natives 并 reload vision 模块。返回 (module, session_mock)。"""
    fake_mod = types.ModuleType("niu_natives")
    session = MagicMock(name="DesktopSession-instance")
    session.capture.return_value = {
        "png_bytes": _FAKE_PNG,
        "width": 1280,
        "height": 720,
        "source_width": 2560,
        "source_height": 1440,
        "backend": "xcap",
        "geometry": {"kind": "desktop"},
        "displays": [{"id": "display-1"}],
    }
    fake_mod.DesktopSession = MagicMock(name="DesktopSession-class", return_value=session)
    monkeypatch.setitem(sys.modules, "niu_natives", fake_mod)
    m = importlib.reload(niu_vision_server)
    return m, session


# ============== 三形态映射（plan §4-V5） ==============


class TestCaptureMapping:
    def test_screen_maps_to_desktop_capture(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        m.screenshot(target="screen")
        session.capture.assert_called_once_with("desktop", {"max_width": 1280})

    def test_window_maps_to_window_id_capture(self, monkeypatch):
        # int（X11/Win32/macOS 数字窗口 ID）→ str 归一化
        m, session = _install_fake_niu_natives(monkeypatch)
        m.screenshot(target="window", window_id=12345)
        session.capture.assert_called_once_with("12345", {"max_width": 1280})

        # str（Wayland atspi 复合 ID）→ 原样透传
        m, session = _install_fake_niu_natives(monkeypatch)
        wayland_id = "atspi::1.31:/org/a11y/atspi/accessible/1"
        m.screenshot(target="window", window_id=wayland_id)
        session.capture.assert_called_once_with(wayland_id, {"max_width": 1280})

    def test_region_maps_to_desktop_capture_with_tuple(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        m.screenshot(target="region", x=10, y=20, width=300, height=200)
        session.capture.assert_called_once_with(
            "desktop", {"max_width": 1280}, (10, 20, 300, 200)
        )


# ============== 参数校验与错误路径 ==============


class TestValidationAndErrors:
    def test_invalid_target_rejected_without_capture(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        result = m.screenshot(target="bogus")
        assert "screen/window/region" in result
        session.capture.assert_not_called()

    def test_window_requires_window_id(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        result = m.screenshot(target="window")
        assert "window_id" in result
        session.capture.assert_not_called()

    def test_region_requires_all_coords(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        result = m.screenshot(target="region", x=10, y=20, width=300)
        assert "x/y/width/height" in result
        session.capture.assert_not_called()

    def test_capture_exception_returns_error_string(self, monkeypatch):
        # 真实场景：TCC 权限被拒 / region 不重叠任何显示器 → Rust 侧抛异常
        m, session = _install_fake_niu_natives(monkeypatch)
        session.capture.side_effect = Exception("TCC permission denied")
        result = m.screenshot(target="screen")
        assert result.startswith("截图失败：")
        assert "TCC permission denied" in result


# ============== 落盘 + 纯路径文本（D-A：不返回图标记） ==============


class TestSaveAndPurePath:
    def test_saves_png_under_niu_tmp_and_returns_pure_path(self, monkeypatch, _home_isolated):
        m, _ = _install_fake_niu_natives(monkeypatch)
        result = m.screenshot(target="screen")

        first_line = result.splitlines()[0]
        assert first_line.startswith("截图已保存: ")
        path_str = first_line[len("截图已保存: "):]
        p = Path(path_str)
        assert p.is_absolute()
        assert p.parent == Path(_home_isolated) / ".niu" / "tmp"
        assert p.name.startswith("screenshot_") and p.suffix == ".png"
        assert p.read_bytes() == _FAKE_PNG
        assert "![" not in result  # D-A：不再返回图标记

    def test_result_carries_size_and_display_metadata(self, monkeypatch):
        m, _ = _install_fake_niu_natives(monkeypatch)
        result = m.screenshot(target="screen")
        meta = "\n".join(result.splitlines()[1:])
        assert "1280x720" in meta  # 降采样后尺寸
        assert "2560x1440" in meta  # 原始分辨率（已降采样标注）
        assert "显示器" in meta


# ============== niu_natives 缺失降级（R11） ==============


class TestNiuNativesMissing:
    def test_import_failure_degrades_to_error_string(self, monkeypatch):
        """sys.modules['niu_natives']=None → import 抛 ImportError →
        模块照常可 import（不炸 REQUIRED_SERVERS __import__），screenshot 返回明确错误串。"""
        monkeypatch.setitem(sys.modules, "niu_natives", None)
        m = importlib.reload(niu_vision_server)
        assert m.niu_natives is None
        result = m.screenshot(target="screen")
        assert result == "截图能力不可用（niu_natives 未安装/平台不支持）"


# ============== region_ratio 按比例截区域（plan §3.2 / §6 用例 7-12） ==============


def _display(x=0, y=0, width=1680, height=1050):
    """实机形态的显示器属性对象——真实 session.list_displays() 返回 PyO3
    #[pyclass] 对象（只能属性访问），用 SimpleNamespace 忠实还原，不用 dict。"""
    return types.SimpleNamespace(x=x, y=y, width=width, height=height)


class TestRegionRatioConversion:
    """用例 7：换算正确性——断言传给 capture 的绝对 region 数值。"""

    def test_single_display_center_quarter(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        session.list_displays.return_value = [_display(0, 0, 1680, 1050)]
        result = m.screenshot(target="region", region_ratio=[0.25, 0.25, 0.75, 0.75])
        # W=1680 H=1050 → x=420 y=262.5 w=840 h=525
        session.capture.assert_called_once_with(
            "desktop", {"max_width": 1280}, (420, 262.5, 840, 525)
        )
        assert result.startswith("截图已保存: ")

    def test_dual_display_positive_offset(self, monkeypatch):
        # 副屏在主屏右侧且上缘抬高：min_y=-100，合成 W=3286 H=1180
        m, session = _install_fake_niu_natives(monkeypatch)
        session.list_displays.return_value = [
            _display(0, 0, 1920, 1080),
            _display(1920, -100, 1366, 768),
        ]
        m.screenshot(target="region", region_ratio=[0.5, 0.5, 1.0, 1.0])
        # x=0+0.5*3286=1643 y=-100+0.5*1180=490 w=1643 h=590
        session.capture.assert_called_once_with(
            "desktop", {"max_width": 1280}, (1643, 490, 1643, 590)
        )

    def test_dual_display_negative_origin(self, monkeypatch):
        # 副屏在主屏左侧（负坐标原点）：min_x=-1920，合成 W=3840 H=1080
        m, session = _install_fake_niu_natives(monkeypatch)
        session.list_displays.return_value = [
            _display(-1920, 0, 1920, 1080),
            _display(0, 0, 1920, 1080),
        ]
        m.screenshot(target="region", region_ratio=[0.0, 0.0, 0.5, 1.0])
        session.capture.assert_called_once_with(
            "desktop", {"max_width": 1280}, (-1920, 0, 1920, 1080)
        )


class TestRegionRatioTargetCoupling:
    """用例 8：region_ratio 非空且 target != region → 报错且不调 capture。"""

    def test_screen_with_region_ratio_rejected_without_capture(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        result = m.screenshot(target="screen", region_ratio=[0.1, 0.1, 0.2, 0.2])
        assert 'target="region"' in result
        session.capture.assert_not_called()
        session.list_displays.assert_not_called()

    def test_window_with_region_ratio_rejected_without_capture(self, monkeypatch):
        # target=window 与 screen 走同一校验分支（P3-4）→ 同样报错且不调 capture
        m, session = _install_fake_niu_natives(monkeypatch)
        result = m.screenshot(target="window", window_id="1", region_ratio=[0.1, 0.1, 0.2, 0.2])
        assert 'target="region"' in result
        session.capture.assert_not_called()
        session.list_displays.assert_not_called()


class TestRegionRatioExclusivity:
    """用例 9：与绝对坐标互斥；两者均未给 → 报错文案枚举两种选项。"""

    def test_ratio_and_abs_coords_mutually_exclusive(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        result = m.screenshot(
            target="region", region_ratio=[0.1, 0.1, 0.5, 0.5],
            x=10, y=20, width=300, height=200,
        )
        assert "互斥" in result
        session.capture.assert_not_called()

    def test_region_with_neither_option_rejected(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        result = m.screenshot(target="region")
        assert "region_ratio=[左,上,右,下]" in result
        assert "x/y/width/height" in result
        session.capture.assert_not_called()

    def test_region_with_partial_abs_coords_rejected(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        result = m.screenshot(target="region", x=10, y=20, width=300)
        assert "region_ratio=[左,上,右,下]" in result
        assert "x/y/width/height" in result
        session.capture.assert_not_called()


class TestRegionRatioBounds:
    """用例 10：越界/退化 → 报错。"""

    @pytest.mark.parametrize("ratio", [
        [0.5, 0.2, 0.5, 0.8],   # left == right（退化）
        [0.6, 0.2, 0.4, 0.8],   # left > right
        [0.1, 0.7, 0.9, 0.3],   # top > bottom
        [0.1, 0.1, 0.5, 1.2],   # 任一项 > 1
        [-0.1, 0.1, 0.5, 0.8],  # 任一项 < 0
    ])
    def test_out_of_range_or_degenerate_rejected(self, monkeypatch, ratio):
        m, session = _install_fake_niu_natives(monkeypatch)
        result = m.screenshot(target="region", region_ratio=ratio)
        assert "0~1" in result
        session.capture.assert_not_called()


class TestRegionRatioShape:
    """用例 11：形态非法 → 明确报错，不抛 ValueError。"""

    @pytest.mark.parametrize("bad", [
        [0.1, 0.1, 0.2],                       # 3 项
        [0.1, 0.1, 0.2, 0.3, 0.4],             # 5 项
        "0.1 0.1 0.2 0.2",                     # 非列表（字符串）
        0.5,                                    # 非列表（数字）
        ["0.1", "0.1", "0.2", "0.2"],          # 含字符串元素
    ])
    def test_malformed_shape_rejected_without_exception(self, monkeypatch, bad):
        m, session = _install_fake_niu_natives(monkeypatch)
        result = m.screenshot(target="region", region_ratio=bad)
        assert "[左,上,右,下]" in result
        session.capture.assert_not_called()


class TestRegionRatioNonFinite:
    """P3-1：NaN/±inf 非有限值——与任何数比较恒 False，会绕过 0~1 越界检查
    （裸 NaN 字面量可经 json.loads 到达工具参数）→ 必须显式拒绝（math.isfinite），
    报明确中文错误且不调 capture（否则穿透到原生层暴露英文原始异常）。"""

    @pytest.mark.parametrize("ratio", [
        [float("nan"), 0.1, 0.5, 0.5],   # NaN：v<0 与 v>1 均 False，越界检查全漏
        [float("inf"), 0, 1, 1],          # +inf
    ])
    def test_non_finite_rejected_without_capture(self, monkeypatch, ratio):
        m, session = _install_fake_niu_natives(monkeypatch)
        result = m.screenshot(target="region", region_ratio=ratio)
        assert "有限数值" in result
        session.capture.assert_not_called()

    @pytest.mark.parametrize("ratio", [
        [10**400, 0, 0.5, 0.5],      # 超大正整数：isfinite 内部转 float 抛 OverflowError
        [-10**400, 0, 1, 1],         # 超大负整数（极端）
    ])
    def test_huge_int_rejected_without_overflow(self, monkeypatch, ratio):
        """P3-2：JSON 允许任意精度整数，json.loads 解析为 Python int——
        isfinite(超大int) 抛 OverflowError 会穿透 screenshot() 暴露英文原始异常。
        范围比较先于 isfinite 短路（int 不转 float）→ 返回中文错误串、绝不抛异常。"""
        m, session = _install_fake_niu_natives(monkeypatch)
        result = m.screenshot(target="region", region_ratio=ratio)
        assert isinstance(result, str)
        assert "0~1" in result
        session.capture.assert_not_called()


class TestRegionRatioDisplaysFailure:
    """用例 12：list_displays 抛错/返回空 → 明确错误串（非 _UNAVAILABLE_MSG）。"""

    def test_list_displays_exception(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        session.list_displays.side_effect = Exception("TCC permission denied")
        result = m.screenshot(target="region", region_ratio=[0.1, 0.1, 0.5, 0.5])
        assert "无法枚举显示器" in result
        assert result != _UNAVAILABLE_MSG
        session.capture.assert_not_called()

    def test_list_displays_empty(self, monkeypatch):
        m, session = _install_fake_niu_natives(monkeypatch)
        session.list_displays.return_value = []
        result = m.screenshot(target="region", region_ratio=[0.1, 0.1, 0.5, 0.5])
        assert "无法枚举显示器" in result
        assert result != _UNAVAILABLE_MSG
        session.capture.assert_not_called()


# ============== 注册契约（R8）+ 主 Agent schema 出现（plan §8-⑤） ==============


class TestRegistrationContract:
    def test_required_servers_contains_vision_server(self):
        assert ("vision-server", "niu_vision_server") in mcp_loader.REQUIRED_SERVERS

    def test_yaml_entry_explicit_static_and_key_names_consistent(self):
        """R8 键名契约：yaml server 段键 == REQUIRED server_name；
        tools 键 == 模块 schema name == 模块函数名（三处逐字符一致，三工具）。"""
        cfg = yaml.safe_load(
            (_REPO_ROOT / "config" / "mcp-servers.yaml").read_text(encoding="utf-8")
        )
        entry = cfg["vision-server"]
        assert entry["workdir"] == "mcp-servers/vision-server/src"  # 承重字段无前缀
        assert entry["preload"] is True
        assert entry["tools"]["screenshot"]["visibility"] == "static"  # 显式 static

        server_name = dict(mcp_loader.REQUIRED_SERVERS)["vision-server"]
        assert server_name == "niu_vision_server"
        assert entry["tools"]["list_targets"]["visibility"] == "static"  # 显式 static
        assert entry["tools"]["analyze_image"]["visibility"] == "static"  # 显式 static
        schemas = {s["name"] for s in niu_vision_server.get_tool_schemas()}
        assert set(entry["tools"]) == {"screenshot", "list_targets", "analyze_image"} == schemas
        assert callable(getattr(niu_vision_server, "screenshot"))
        assert callable(getattr(niu_vision_server, "list_targets"))
        assert callable(getattr(niu_vision_server, "analyze_image"))

    def test_screenshot_enters_registry_static_tools(self):
        """真实 ToolRegistry.register_server → screenshot 进 get_static_tools——
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
        assert "vision-server/screenshot" in static_tools
        assert "vision-server/analyze_image" in static_tools
