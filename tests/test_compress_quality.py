"""context-manager 压缩质量修复测试。"""
import json
from pathlib import Path
from unittest.mock import patch

from agent.subagent import (
    _read_compress_target_tokens,
    _read_max_output_tokens,
)


def test_read_compress_target_tokens_default():
    """配置无 compressTargetTokens 时返回默认 60000。"""
    with patch("agent.subagent._get_user_config_path") as mock_path:
        tmp = Path("/tmp/test_niu_config_empty.json")
        tmp.write_text(json.dumps({"context": {}}))
        mock_path.return_value = tmp
        assert _read_compress_target_tokens() == 60000
    tmp.unlink()


def test_read_compress_target_tokens_custom():
    """配置有 compressTargetTokens 时返回自定义值。"""
    with patch("agent.subagent._get_user_config_path") as mock_path:
        tmp = Path("/tmp/test_niu_config_custom.json")
        tmp.write_text(json.dumps({"context": {"compressTargetTokens": 80000}}))
        mock_path.return_value = tmp
        assert _read_compress_target_tokens() == 80000
    tmp.unlink()


def test_read_max_output_tokens_default():
    """配置无 maxOutputTokens 时返回默认 16384。"""
    with patch("agent.subagent._get_user_config_path") as mock_path:
        tmp = Path("/tmp/test_niu_config_empty2.json")
        tmp.write_text(json.dumps({"context": {}}))
        mock_path.return_value = tmp
        assert _read_max_output_tokens() == 16384
    tmp.unlink()


def test_read_max_output_tokens_custom():
    """配置有 maxOutputTokens 时返回自定义值。"""
    with patch("agent.subagent._get_user_config_path") as mock_path:
        tmp = Path("/tmp/test_niu_config_custom2.json")
        tmp.write_text(json.dumps({"context": {"maxOutputTokens": 32768}}))
        mock_path.return_value = tmp
        assert _read_max_output_tokens() == 32768
    tmp.unlink()
