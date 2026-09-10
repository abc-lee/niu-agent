"""vision-server screenshot 工具测试（可视化功能 plan v0.5.2 §4-V5 / T5）。

全 mock——禁真实 LLM、禁真实抓屏（niu_natives 经 sys.modules 注入 fake 模块
整体替换）、零真实 ~/.niu 读写（HOME 指向 tmp_path）。

覆盖：
- 三形态映射：screen→capture("desktop") / window→capture(window_id) /
  region→capture("desktop", caps, (x,y,w,h))，统一 max_width=1280 降采样
- 落盘 ~/.niu/tmp/screenshot_<ts>.png + `![截图](绝对路径)` 标记文本返回
  （V3 图片直通通道格式）+ 尺寸/显示器元数据
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


# ============== 落盘 + 标记文本（V3 图片直通通道格式） ==============


class TestSaveAndMarker:
    def test_saves_png_under_niu_tmp_and_returns_marker(self, monkeypatch, _home_isolated):
        m, _ = _install_fake_niu_natives(monkeypatch)
        result = m.screenshot(target="screen")

        first_line = result.splitlines()[0]
        assert first_line.startswith("![截图](") and first_line.endswith(")")
        path_str = first_line[len("![截图]("):-1]
        p = Path(path_str)
        assert p.is_absolute()
        assert p.parent == Path(_home_isolated) / ".niu" / "tmp"
        assert p.name.startswith("screenshot_") and p.suffix == ".png"
        assert p.read_bytes() == _FAKE_PNG

    def test_marker_carries_size_and_display_metadata(self, monkeypatch):
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


# ============== 注册契约（R8）+ 主 Agent schema 出现（plan §8-⑤） ==============


class TestRegistrationContract:
    def test_required_servers_contains_vision_server(self):
        assert ("vision-server", "niu_vision_server") in mcp_loader.REQUIRED_SERVERS

    def test_yaml_entry_explicit_static_and_key_names_consistent(self):
        """R8 键名契约：yaml server 段键 == REQUIRED server_name；
        tools 键 == 模块 schema name == 模块函数名（三处逐字符一致，两工具）。"""
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
        schemas = {s["name"] for s in niu_vision_server.get_tool_schemas()}
        assert set(entry["tools"]) == {"screenshot", "list_targets"} == schemas
        assert callable(getattr(niu_vision_server, "screenshot"))
        assert callable(getattr(niu_vision_server, "list_targets"))

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
        assert "vision-server/screenshot" in registry.get_static_tools()
