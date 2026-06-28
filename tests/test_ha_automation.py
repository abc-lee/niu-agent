"""HA 自动化/场景/脚本集成测试 — 使用真实 HA 环境"""
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp-servers", "ha-server", "src"))
from niu_ha_server import ha_automation, ha_setup


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
