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


from niu_api.compat import _strip_analysis


def test_strip_analysis_closed():
    """闭合的 <analysis>...</analysis> 块被剥离。"""
    raw = "<analysis>\n第一份 idx 1-100\n</analysis>\n\nkeep=1,2,3\nupdate=1|摘要"
    result = _strip_analysis(raw)
    assert "<analysis>" not in result
    assert "keep=1,2,3" in result
    assert "update=1|摘要" in result
    assert "第一份" not in result


def test_strip_analysis_unclosed():
    """未闭合的 <analysis>（有开始无结束）被剥离到字符串末尾。"""
    raw = "<analysis>\n第一份 idx 1-100\nkeep=1,2,3"
    result = _strip_analysis(raw)
    assert "<analysis>" not in result
    assert "keep=1,2,3" not in result  # 未闭合时 keep= 在 analysis 块里被一起剥离


def test_strip_analysis_case_insensitive():
    """大小写不敏感：<ANALYSIS> 也能剥离。"""
    raw = "<ANALYSIS>\n分析内容\n</ANALYSIS>\n\nkeep=1,2,3"
    result = _strip_analysis(raw)
    assert "<ANALYSIS>" not in result.lower()
    assert "keep=1,2,3" in result


def test_strip_analysis_missing():
    """没有 <analysis> 块时原样返回。"""
    raw = "keep=1,2,3\nupdate=1|摘要"
    result = _strip_analysis(raw)
    assert result == raw


def test_strip_analysis_multiline():
    """analysis 块跨多行（含换行）被完整剥离。"""
    raw = """<analysis>
第一份 idx 1-100：含 3 个会话单元
第二份 idx 101-200：估算释放 3K
累计 11K，已达目标
</analysis>

keep=1,5,15,30
update=1|[摘要] 智能家居;5|[摘要] 知识图谱
cursor=30"""
    result = _strip_analysis(raw)
    assert "<analysis>" not in result
    assert "keep=1,5,15,30" in result
    assert "cursor=30" in result
    assert "会话单元" not in result
