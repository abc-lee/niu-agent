"""ha_get_image 工具测试 — 使用真实 HA 环境"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp-servers", "ha-server", "src"))
from niu_ha_server import ha_get_image, ha_setup, ha_status


def _ensure_connected():
    """确保 HA 已连接"""
    result = ha_setup()
    if not result.get("connected"):
        ha_url = os.environ.get("HA_URL", "http://localhost:8123")
        ha_token = os.environ.get("HA_TOKEN", "")
        if not ha_token:
            pytest.skip("HA_TOKEN not set")
        result = ha_setup(ha_url=ha_url, ha_token=ha_token)
        assert result.get("connected"), f"HA connection failed: {result}"


def _find_image_entity():
    """动态查询第一个 image 域实体，避免硬编码 entity_id"""
    _ensure_connected()
    status = ha_status(domain="image")
    if status.get("connected") and status.get("devices"):
        return status["devices"][0]["entity_id"]
    return None


class TestHaGetImage:
    def test_no_config_returns_error(self, monkeypatch):
        """未配置 HA 时返回连接错误"""
        import niu_ha_server
        monkeypatch.setattr(niu_ha_server, "_read_config", lambda: {})
        result = ha_get_image(entity_id="image.test_map")
        assert not result.get("success", False)
        assert "error" in result

    def test_get_image_success(self):
        """成功下载 image 域图片"""
        entity_id = _find_image_entity()
        if not entity_id:
            pytest.skip("No image entity available")
        result = ha_get_image(entity_id=entity_id)
        try:
            assert result.get("success"), f"下载失败: {result}"
            assert "path" in result
            assert os.path.exists(result["path"])
            assert os.path.getsize(result["path"]) > 0
            expected_dir = os.path.expanduser("~/.niu/tmp")
            assert result["path"].startswith(expected_dir)
            assert "content_type" in result
            assert result["content_type"].startswith("image/")
            assert "size" in result
            assert result["size"] > 0
        finally:
            if result.get("path") and os.path.exists(result["path"]):
                os.remove(result["path"])

    def test_get_image_nonexistent_entity(self):
        """不存在的 entity_id 返回错误"""
        _ensure_connected()
        result = ha_get_image(entity_id="image.nonexistent_test_12345")
        assert not result.get("success", False)
        assert "error" in result

    def test_get_image_401_returns_auth_error(self, monkeypatch):
        """token 无效时返回 401 错误"""
        _ensure_connected()
        # 必须用真实存在的 entity_id，否则 HA 返回 404 而非 401
        entity_id = _find_image_entity()
        if not entity_id:
            pytest.skip("No image entity available for 401 test")
        import niu_ha_server
        real_config = niu_ha_server._read_config()
        real_url = real_config.get("ha_url", "http://localhost:8123")
        monkeypatch.setattr(niu_ha_server, "_read_config", lambda: {
            "ha_url": real_url,
            "ha_token": "invalid_token_for_test",
        })
        result = ha_get_image(entity_id=entity_id)
        assert not result.get("success", False)
        assert "error" in result
        assert "401" in result["error"] or "认证" in result["error"]
