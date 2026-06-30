"""context-manager 压缩质量修复测试。"""
import json
from unittest.mock import patch

from agent.subagent import (
    _read_compress_target_tokens,
    _read_max_output_tokens,
)


def test_read_compress_target_tokens_default(tmp_path):
    """配置无 compressTargetTokens 时返回默认 60000。"""
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"context": {}}))
    with patch("agent.subagent._get_user_config_path", return_value=config_file):
        assert _read_compress_target_tokens() == 60000


def test_read_compress_target_tokens_custom(tmp_path):
    """配置有 compressTargetTokens 时返回自定义值。"""
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"context": {"compressTargetTokens": 80000}}))
    with patch("agent.subagent._get_user_config_path", return_value=config_file):
        assert _read_compress_target_tokens() == 80000


def test_read_max_output_tokens_default(tmp_path):
    """配置无 maxOutputTokens 时返回默认 16384。"""
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"context": {}}))
    with patch("agent.subagent._get_user_config_path", return_value=config_file):
        assert _read_max_output_tokens() == 16384


def test_read_max_output_tokens_custom(tmp_path):
    """配置有 maxOutputTokens 时返回自定义值。"""
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"context": {"maxOutputTokens": 32768}}))
    with patch("agent.subagent._get_user_config_path", return_value=config_file):
        assert _read_max_output_tokens() == 32768


def test_read_compress_target_tokens_invalid_returns_default(tmp_path):
    """配置 compressTargetTokens 为非法值（0/负数/字符串/bool）时返回默认 60000。"""
    config_file = tmp_path / "config.json"
    for invalid_val in [0, -100, "60000", True, None]:
        config_file.write_text(json.dumps({"context": {"compressTargetTokens": invalid_val}}))
        with patch("agent.subagent._get_user_config_path", return_value=config_file):
            assert _read_compress_target_tokens() == 60000, f"非法值 {invalid_val!r} 应返回默认 60000"


def test_read_max_output_tokens_invalid_returns_default(tmp_path):
    """配置 maxOutputTokens 为非法值时返回默认 16384。"""
    config_file = tmp_path / "config.json"
    for invalid_val in [0, -100, "16384", True, None]:
        config_file.write_text(json.dumps({"context": {"maxOutputTokens": invalid_val}}))
        with patch("agent.subagent._get_user_config_path", return_value=config_file):
            assert _read_max_output_tokens() == 16384, f"非法值 {invalid_val!r} 应返回默认 16384"
