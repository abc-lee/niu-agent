"""HA 自动化/场景/脚本集成测试 — 使用真实 HA 环境"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp-servers", "ha-server", "src"))
from niu_ha_server import ha_automation, ha_scene, ha_script, ha_setup


def _ensure_connected():
    """确保 HA 已连接"""
    result = ha_setup()
    if not result.get("connected"):
        ha_url = os.environ.get("HA_URL", "http://homeassistant.local:8123")
        ha_token = os.environ.get("HA_TOKEN", "")
        if not ha_token:
            pytest.skip("HA_TOKEN not set")
        result = ha_setup(ha_url=ha_url, ha_token=ha_token)
        assert result.get("connected"), f"HA connection failed: {result}"


class TestHaAutomation:
    def test_list(self):
        """list 操作返回自动化摘要列表"""
        _ensure_connected()
        result = ha_automation(action="list")
        assert isinstance(result, dict)
        assert "automations" in result or "error" in result
        if "automations" in result:
            for a in result["automations"]:
                assert "name" in a
                assert "entity_id" in a

    def test_list_detail(self):
        """list detail=true 返回完整配置"""
        _ensure_connected()
        result = ha_automation(action="list", detail=True)
        assert isinstance(result, dict)
        if "automations" in result and result["automations"]:
            first = result["automations"][0]
            assert "config" in first

    def test_create_and_get(self):
        """创建自动化后可以 get 到"""
        _ensure_connected()
        # 使用真实存在的 entity（从 ha_status 获取第一个灯设备）
        from niu_ha_server import ha_status
        status = ha_status()
        real_entity = None
        if status.get("connected") and status.get("devices"):
            light = next((d for d in status["devices"] if d["entity_id"].startswith("light.")), None)
            if light:
                real_entity = light["entity_id"]
        if not real_entity:
            pytest.skip("No light entity available for test")
        result = ha_automation(action="create", name="测试自动删除", config={
            "triggers": [{"platform": "time", "at": "08:00:00"}],
            "actions": [{"action": "light.turn_on", "target": {"entity_id": real_entity}}],
            "mode": "single",
        })
        assert result.get("success"), f"创建失败: {result}"
        try:
            import time
            time.sleep(3)  # noqa: I001
            get_result = ha_automation(action="get", name="测试自动删除")
            assert get_result.get("config"), f"获取配置失败: {get_result}"
        finally:
            ha_automation(action="delete", name="测试自动删除", confirm=True)

    def test_delete_preview(self):
        """delete 不带 confirm 返回预览"""
        _ensure_connected()
        # 先创建
        ha_automation(action="create", name="测试删除预览", config={
            "triggers": [{"platform": "time", "at": "09:00:00"}],
            "actions": [{"action": "persistent_notification.create", "data": {"message": "test"}}],
            "mode": "single",
        })
        import time
        time.sleep(3)  # noqa: I001
        try:
            result = ha_automation(action="delete", name="测试删除预览")
            assert result.get("preview"), f"应返回预览: {result}"
        finally:
            ha_automation(action="delete", name="测试删除预览", confirm=True)

    def test_enable_disable(self):
        """启用/禁用自动化"""
        _ensure_connected()
        # 先创建
        ha_automation(action="create", name="测试开关", config={
            "triggers": [{"platform": "time", "at": "10:00:00"}],
            "actions": [{"action": "persistent_notification.create", "data": {"message": "test"}}],
            "mode": "single",
        })
        import time
        time.sleep(3)  # noqa: I001
        try:
            # 禁用
            result = ha_automation(action="disable", name="测试开关")
            assert result.get("success"), f"禁用失败: {result}"
            # 启用
            result = ha_automation(action="enable", name="测试开关")
            assert result.get("success"), f"启用失败: {result}"
        finally:
            ha_automation(action="delete", name="测试开关", confirm=True)

    def test_name_not_found(self):
        """名称不存在时返回错误"""
        _ensure_connected()
        result = ha_automation(action="get", name="不存在的自动化_xyz")
        assert "error" in result


class TestHaScene:
    def test_list(self):
        _ensure_connected()
        result = ha_scene(action="list")
        assert isinstance(result, dict)
        assert "scenes" in result or "error" in result

    def test_create_activate_delete(self):
        """创建场景并激活，使用真实 entity"""
        _ensure_connected()
        from niu_ha_server import ha_status
        status = ha_status()
        real_entity = None
        if status.get("connected") and status.get("devices"):
            light = next((d for d in status["devices"] if d["entity_id"].startswith("light.")), None)
            if light:
                real_entity = light["entity_id"]
        if not real_entity:
            pytest.skip("No light entity available for test")
        result = ha_scene(action="create", name="测试场景删除", config={
            "entities": {real_entity: {"state": "on", "brightness": 128}}
        })
        assert result.get("success"), f"创建失败: {result}"
        try:
            import time
            time.sleep(1)  # noqa: I001
            result = ha_scene(action="activate", name="测试场景删除")
            assert result.get("success"), f"激活失败: {result}"
        finally:
            ha_scene(action="delete", name="测试场景删除", confirm=True)

    def test_snapshot_with_real_entities(self):
        """snapshot 使用真实 entity_ids 创建快照"""
        _ensure_connected()
        from niu_ha_server import ha_status
        status = ha_status()
        if not status.get("connected") or not status.get("devices"):
            pytest.skip("No devices available for snapshot test")
        entity_ids = [d["entity_id"] for d in status["devices"][:2]]
        result = ha_scene(action="snapshot", name="测试快照删除", entity_ids=entity_ids)
        assert result.get("success"), f"快照失败: {result}"
        try:
            # Verify config API can read it back
            get_result = ha_scene(action="get", name="测试快照删除")
            assert get_result.get("config"), f"获取快照配置失败: {get_result}"
        finally:
            ha_scene(action="delete", name="测试快照删除", confirm=True)


class TestHaScript:
    def test_list(self):
        _ensure_connected()
        result = ha_script(action="list")
        assert isinstance(result, dict)
        assert "scripts" in result or "error" in result

    def test_create_run_delete(self):
        """创建脚本并运行，使用 delay 避免依赖真实 entity"""
        _ensure_connected()
        result = ha_script(action="create", name="测试脚本删除", config={
            "mode": "single",
            "sequence": [{"delay": {"seconds": 1}}],
        })
        assert result.get("success"), f"创建失败: {result}"
        try:
            import time
            time.sleep(3)  # noqa: I001
            result = ha_script(action="run", name="测试脚本删除")
            assert result.get("success"), f"运行失败: {result}"
        finally:
            ha_script(action="delete", name="测试脚本删除", confirm=True)
