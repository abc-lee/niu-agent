"""测试 LightRAG 操作超时配置读取。"""
from unittest.mock import patch

from niu_api.internal.lightrag_manager import lightrag_timeout


def test_default_when_config_empty():
    """lightrag 段无该键时返回默认值。"""
    with patch("niu_api.internal.lightrag_manager._get_lightrag_config", return_value={}):
        assert lightrag_timeout("insert_timeout", 600) == 600
        assert lightrag_timeout("query_timeout", 120) == 120


def test_configured_value_wins():
    """lightrag 段有该键时使用配置值。"""
    with patch("niu_api.internal.lightrag_manager._get_lightrag_config",
               return_value={"insert_timeout": 900, "query_timeout": 60}):
        assert lightrag_timeout("insert_timeout", 600) == 900
        assert lightrag_timeout("query_timeout", 120) == 60


def test_invalid_value_falls_back():
    """非法值（非数字/负数）回退默认。"""
    with patch("niu_api.internal.lightrag_manager._get_lightrag_config",
               return_value={"insert_timeout": "abc", "query_timeout": -5}):
        assert lightrag_timeout("insert_timeout", 600) == 600
        assert lightrag_timeout("query_timeout", 120) == 120
