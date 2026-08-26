"""mcp-servers.yaml 双源加载 + deep merge 合并矩阵测试。

覆盖实施计划 2026-08-26-mcp-servers-dual-dir.md T1 的 16 例矩阵：
bundle 权威层 + 用户层 mcp-servers-user.yaml deep merge、D7 null 删除语义
（deleted_names 三路径兑现）、D8 弃用 warning 去重、D4 降级分支。

全部走 tmp_path 伪造文件，patch 模块级路径常量与
niu_api.config._get_bundle_config_dir——零真实 ~/.niu 读写。
"""

import builtins
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
from loguru import logger

from agent import mcp_loader
from agent.mcp_loader import (
    _deep_merge,
    _load_mcp_config,
    _reset_legacy_warned,
    get_mcp_load_failures,
    load_external_servers,
    load_mcp_tools,
    reset_mcp_load_failures,
)


class LogCapture:
    """loguru 捕获器：按级别取消息列表"""

    def __init__(self):
        self.records = []

    def __enter__(self):
        def _sink(message):
            self.records.append(message.record)

        self._sid = logger.add(_sink, level="DEBUG")
        return self

    def __exit__(self, *args):
        logger.remove(self._sid)
        return False

    def messages(self, level: str) -> list[str]:
        return [r["message"] for r in self.records if r["level"].name == level]


def _make_env(tmp_path: Path, bundle_yaml=None, user_yaml=None) -> dict:
    """构造 bundle/user 两层 yaml 文件并返回 patch 目标信息"""
    bundle_dir = tmp_path / "bundle" / "config"
    bundle_dir.mkdir(parents=True)
    if bundle_yaml is not None:
        (bundle_dir / "mcp-servers.yaml").write_text(bundle_yaml, encoding="utf-8")

    legacy_path = tmp_path / "legacy-mcp-servers.yaml"
    user_path = tmp_path / "user" / "config" / "mcp-servers-user.yaml"
    user_path.parent.mkdir(parents=True, exist_ok=True)
    if user_yaml is not None:
        user_path.write_text(user_yaml, encoding="utf-8")

    return {
        "bundle_dir": bundle_dir,
        "legacy": str(legacy_path),
        "user": str(user_path),
        "user_path": user_path,
    }


@pytest.fixture(autouse=True)
def _isolate_legacy_flag():
    """每个用例前后重置 D8 去重标志，隔离模块级状态"""
    _reset_legacy_warned()
    yield
    _reset_legacy_warned()


def _run_dual(env: dict, fn=None):
    """在双源 patch 下执行 _load_mcp_config（或自定义函数）"""
    patches = [
        patch("niu_api.config._get_bundle_config_dir", return_value=env["bundle_dir"]),
        patch.object(mcp_loader, "_LEGACY_MCP_CONFIG_PATH", env["legacy"]),
        patch.object(mcp_loader, "_USER_MCP_CONFIG_PATH", env["user"]),
    ]
    for p in patches:
        p.start()
    try:
        return fn() if fn else _load_mcp_config()
    finally:
        for p in patches:
            p.stop()


BASE_YAML = """\
session-manager:
  command: python
  args: ["-m", "niu_session_manager"]
  workdir: mcp-servers/session-manager/src
  preload: false
  tools:
    get_messages:
      visibility: static
    read_history_block:
      visibility: static
photo-server:
  command: python
  args: ["-m", "niu_photo_server"]
"""


# ============================================================================
# deep merge 纯函数层
# ============================================================================


class TestDeepMerge:
    def test_dict_recursive_scalar_and_list_user_wins(self):
        base = {
            "srv": {"preload": False, "args": ["-m", "old"], "workdir": "a/b"},
            "other": {"keep": True},
        }
        override = {"srv": {"preload": True, "args": ["-x", "new"]}}
        deleted = set()
        out = _deep_merge(base, override, deleted)
        assert out == {
            "srv": {"preload": True, "args": ["-x", "new"], "workdir": "a/b"},
            "other": {"keep": True},
        }
        assert deleted == set()

    def test_top_null_pops_key_and_collects_deleted(self):
        deleted = set()
        out = _deep_merge({"a": {}, "b": {}}, {"a": None}, deleted)
        assert "a" not in out
        assert deleted == {"a"}

    def test_nested_null_deletes_key_not_in_deleted_set(self):
        deleted = set()
        out = _deep_merge(
            {"srv": {"tools": {"t1": {"visibility": "hidden"}, "t2": {}}}},
            {"srv": {"tools": {"t1": None}}},
            deleted,
        )
        assert "t1" not in out["srv"]["tools"]
        assert "t2" in out["srv"]["tools"]
        assert deleted == set(), "嵌套 null 只删键不入集合"


# ============================================================================
# _load_mcp_config 双源加载层（真实文件读写）
# ============================================================================


class TestDualSourceLoading:
    def test_user_layer_adds_new_server(self, tmp_path):
        env = _make_env(tmp_path, BASE_YAML, "my-server:\n  command: npx\n")
        merged, deleted = _run_dual(env)
        assert merged["my-server"] == {"command": "npx"}
        assert "session-manager" in merged
        assert deleted == set()

    def test_same_name_scalar_override_user_wins(self, tmp_path):
        env = _make_env(tmp_path, BASE_YAML, "session-manager:\n  preload: true\n")
        merged, _ = _run_dual(env)
        assert merged["session-manager"]["preload"] is True
        # 未覆盖字段保持内置值
        assert merged["session-manager"]["workdir"] == "mcp-servers/session-manager/src"

    def test_tools_nested_deep_merge_diff_only(self, tmp_path):
        user = (
            "session-manager:\n"
            "  tools:\n"
            "    read_history_block:\n"
            "      visibility: hidden\n"
        )
        env = _make_env(tmp_path, BASE_YAML, user)
        merged, _ = _run_dual(env)
        tools = merged["session-manager"]["tools"]
        # 只写差异部分：被覆盖条目 + 内置其余条目并存
        assert tools["read_history_block"]["visibility"] == "hidden"
        assert tools["get_messages"]["visibility"] == "static"

    def test_list_overrides_whole(self, tmp_path):
        env = _make_env(tmp_path, BASE_YAML, 'session-manager:\n  args: ["-x", "y"]\n')
        merged, _ = _run_dual(env)
        assert merged["session-manager"]["args"] == ["-x", "y"]

    def test_user_layer_missing_is_normal(self, tmp_path):
        with LogCapture() as cap:
            merged, deleted = _run_dual(_make_env(tmp_path, BASE_YAML))
        assert merged.keys() >= {"session-manager", "photo-server"}
        assert deleted == set()
        # D5：用户层缺失=正常态，仅 debug 日志不算异常
        assert not any("Failed" in m or "not found" in m for m in cap.messages("ERROR"))

    def test_user_layer_bad_yaml_degrades(self, tmp_path):
        env = _make_env(tmp_path, BASE_YAML, "foo: [unclosed\n")
        with LogCapture() as cap:
            merged, deleted = _run_dual(env)
        # D4：error+跳过用户层，内置层照常
        assert len(cap.messages("ERROR")) >= 1
        assert "Failed to load user MCP config" in "\n".join(cap.messages("ERROR"))
        assert merged.keys() >= {"session-manager", "photo-server"}
        assert "my-server" not in merged
        assert deleted == set()

    def test_user_layer_top_non_dict_degrades(self, tmp_path):
        env = _make_env(tmp_path, BASE_YAML, "- a\n- b\n")
        with LogCapture() as cap:
            merged, deleted = _run_dual(env)
        assert any(
            "not a mapping" in m and "user" in m for m in cap.messages("ERROR")
        )
        assert merged.keys() >= {"session-manager", "photo-server"}
        assert deleted == set()

    def test_user_layer_empty_file_ok(self, tmp_path):
        env = _make_env(tmp_path, BASE_YAML, "")
        merged, deleted = _run_dual(env)
        assert merged.keys() >= {"session-manager", "photo-server"}
        assert deleted == set()

    def test_bundle_missing_error_empty_base(self, tmp_path):
        env = _make_env(tmp_path, None, "my-server:\n  command: npx\n")
        with LogCapture() as cap:
            merged, deleted = _run_dual(env)
        # D4 钉死语义：bundle 缺失=error+空基座继续跑（用户新增 server 仍在）
        assert "Bundle MCP config not found" in "\n".join(cap.messages("ERROR"))
        assert merged == {"my-server": {"command": "npx"}}
        assert deleted == set()

    def test_bundle_parse_failure_error_empty_base(self, tmp_path):
        env = _make_env(tmp_path, "broken: [unclosed\n", "session-manager:\n  x: 1\n")
        with LogCapture() as cap:
            merged, deleted = _run_dual(env)
        assert "Failed to load bundle MCP config" in "\n".join(cap.messages("ERROR"))
        assert merged == {"session-manager": {"x": 1}}
        assert deleted == set()


# ============================================================================
# D8 弃用 warning 去重 + 重置口
# ============================================================================


class TestLegacyWarningDedup:
    def test_warning_once_across_two_calls_and_reset_hook_works(self, tmp_path):
        env = _make_env(tmp_path, BASE_YAML)
        Path(env["legacy"]).write_text("legacy: true\n", encoding="utf-8")

        with LogCapture() as cap:
            _run_dual(env)
            _run_dual(env)  # 第二调用点/第二次调用
        legacy_warnings = [m for m in cap.messages("WARNING") if "已弃用" in m]
        assert len(legacy_warnings) == 1, f"D8 去重失效: {cap.messages('WARNING')}"

        # 重置口可复测
        _reset_legacy_warned()
        with LogCapture() as cap2:
            _run_dual(env)
        legacy_warnings2 = [m for m in cap2.messages("WARNING") if "已弃用" in m]
        assert len(legacy_warnings2) == 1

    def test_no_legacy_file_no_warning(self, tmp_path):
        with LogCapture() as cap:
            _run_dual(_make_env(tmp_path, BASE_YAML))
        assert not [m for m in cap.messages("WARNING") if "已弃用" in m]


# ============================================================================
# D7 删除语义三路径兑现（加载循环真跳过）
# ============================================================================


def _fake_import_factory(mock_modules: dict, fail_names: set):
    """构造 fake __import__：mock_modules 内的模块返回带 get_tool_schemas 的 Mock，
    fail_names 内的模块抛 ImportError，其余模块回退真实 import（pathlib 等基础设施）。"""
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name in mock_modules:
            mod = Mock(spec=["get_tool_schemas"])
            mod.get_tool_schemas.return_value = [
                {"name": "ok_tool", "description": "ok", "inputSchema": {}}
            ]
            return mod
        if name in fail_names:
            raise ImportError(f"No module named '{name}'")
        return real_import(name, globals, locals, fromlist, level)

    return fake_import


class TestDeletedServerPaths:
    def test_required_loop_real_skip_on_null_delete(self):
        """REQUIRED 内置被 null 禁用 → 加载循环真跳过（不 import 不计失败），
        「All N servers loaded」计数排除 skip 项"""
        from agent.tool_registry import reset_registry

        reset_mcp_load_failures()
        reset_registry()
        try:
            fake_import = _fake_import_factory({"ok-module"}, {"gone-module"})
            with patch(
                "agent.mcp_loader._load_mcp_config",
                return_value=({}, {"gone-server"}),
            ), patch("builtins.__import__", side_effect=fake_import):
                with LogCapture() as cap:
                    registry = load_mcp_tools(
                        required_servers=[
                            ("ok-server", "ok-module"),
                            ("gone-server", "gone-module"),
                        ]
                    )
                    # reset_registry 会 clear 同一实例，状态须在 try 内捕获
                    registered_servers = list(registry._server_tools.keys())
        finally:
            reset_registry()

        # gone-server 被跳过：无失败记录、registry 只有 ok-server
        failures = get_mcp_load_failures()
        assert failures == [], f"skip 不应计失败: {failures}"
        assert "ok-server" in registered_servers
        assert "gone-server" not in registered_servers
        info_logs = cap.messages("INFO")
        assert any("All 1 servers loaded" in m for m in info_logs), (
            f"计数应排除 skip 项: {info_logs}"
        )

    def test_optional_loop_real_skip_on_null_delete(self):
        """OPTIONAL 内置被 null 禁用 → 同款 skip，不计失败不触发 import"""
        from agent.tool_registry import reset_registry

        reset_mcp_load_failures()
        reset_registry()
        try:
            fake_import = _fake_import_factory({"ok-module"}, {"niu_ha_server"})
            with patch(
                "agent.mcp_loader._load_mcp_config",
                return_value=({}, {"ha-server"}),
            ), patch(
                "agent.mcp_loader.OPTIONAL_SERVERS",
                [("ha-server", "niu_ha_server")],
            ), patch("builtins.__import__", side_effect=fake_import):
                load_mcp_tools(required_servers=[("ok-server", "ok-module")])
        finally:
            reset_registry()

        failures = get_mcp_load_failures()
        assert failures == [], f"OPTIONAL skip 不应计失败: {failures}"

    def test_deleted_external_server_not_connected(self, tmp_path):
        """null 删除外部 stdio server → load_external_servers 不崩且不连接；
        非 dict 条目守卫同场验证"""
        base = (
            "ext-tool:\n  mode: stdio\n  command: npx\n  args: ['-y', '@mcp/x']\n"
        )
        user = "ext-tool: null\nscalar-entry: just-a-string\n"
        env = _make_env(tmp_path, base, user)

        mcp_client = Mock()
        mcp_client.connect_stdio = AsyncMock()

        async def _run():
            await load_external_servers(mcp_client, registry=None)

        def call():
            import asyncio

            asyncio.run(_run())

        with LogCapture() as cap:
            _run_dual(env, fn=call)

        mcp_client.connect_stdio.assert_not_awaited()
        assert any("not a mapping" in m for m in cap.messages("ERROR"))

    def test_user_added_stdio_external_end_to_end(self, tmp_path):
        """用户层新增 stdio 外部 server → 端到端连接参数正确"""
        user = (
            "ext-tool:\n"
            "  mode: stdio\n"
            "  command: npx\n"
            "  args: ['-y', '@mcp/server']\n"
        )
        env = _make_env(tmp_path, BASE_YAML, user)

        mcp_client = Mock()
        mcp_client.connect_stdio = AsyncMock()
        mcp_client.list_tools = AsyncMock(return_value=[])

        async def _run():
            from agent.tool_registry import ToolRegistry

            await load_external_servers(mcp_client, registry=ToolRegistry())

        def call():
            import asyncio

            asyncio.run(_run())

        _run_dual(env, fn=call)

        mcp_client.connect_stdio.assert_awaited_once_with(
            "ext-tool", "npx", ["-y", "@mcp/server"], None
        )


# ============================================================================
# server.tools 非 dict 守卫（register 前 visibility_map 类型防线）
# ============================================================================


class TestToolsNonDictGuard:
    def test_tools_list_ignored_with_warning_registration_proceeds(self):
        from agent.tool_registry import reset_registry

        reset_mcp_load_failures()
        reset_registry()
        try:
            fake_import = _fake_import_factory({"ok-module"}, {})
            with patch(
                "agent.mcp_loader._load_mcp_config",
                return_value=({"ok-server": {"tools": ["a", "b"]}}, set()),
            ), patch("builtins.__import__", side_effect=fake_import):
                with LogCapture() as cap:
                    registry = load_mcp_tools(
                        required_servers=[("ok-server", "ok-module")]
                    )
                    # reset_registry 会 clear 同一实例，状态须在 try 内捕获
                    registered_servers = list(registry._server_tools.keys())
        finally:
            reset_registry()

        assert any(
            "'tools' is not a mapping" in m for m in cap.messages("WARNING")
        )
        # 注册不受影响（visibility_map 忽略为 None）
        assert "ok-server" in registered_servers
