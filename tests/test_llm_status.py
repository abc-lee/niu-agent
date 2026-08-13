"""llm-status 三态测试（启动探测去重 Task 1）。

背景：llm-status 原只查配置存在性（ready = apiKey/apiBase/model 非空）；
修复后升级为三态——ready（配置存在 AND lifespan 探测通过）、probe_failed
（配置存在 AND 探测失败）、not_ready（配置缺失）——启动器据此跳过重复 test-llm。
"""
import json
from unittest.mock import patch

from niu_api.compat import get_llm_status


def _make_config_path(tmp_path, data: dict | None):
    """构造 CONFIG_PATH 指向的配置文件；data=None 表示文件不存在。"""
    cfg = tmp_path / "user-config.json"
    if data is not None:
        cfg.write_text(json.dumps(data), encoding="utf-8")
    return cfg


async def _status(tmp_path, data, gate_ready):
    cfg = _make_config_path(tmp_path, data)
    # R1-P0 修正：get_llm_status 函数内 `from niu_api.config import CONFIG_PATH`
    # （compat.py L1424）——调用时从 niu_api.config 命名空间取属性——
    # patch 目标必须是源头模块 niu_api.config.CONFIG_PATH（patch compat 命名空间无效→AttributeError）
    with patch("niu_api.config.CONFIG_PATH", str(cfg)), \
         patch("niu_api.internal.lightrag_manager._llm_gate_ready", gate_ready):
        return await get_llm_status()


async def test_not_ready_config_missing_file(tmp_path):
    """配置文件不存在 → not_ready（ready=false, probe_failed=false）。"""
    result = await _status(tmp_path, None, True)
    assert result["ready"] is False
    assert result["probe_failed"] is False


async def test_not_ready_missing_api_key(tmp_path):
    """配置存在但缺 apiKey → not_ready。"""
    result = await _status(tmp_path, {"llm": {"apiBase": "http://x", "model": "m"}}, True)
    assert result["ready"] is False
    assert result["probe_failed"] is False


async def test_not_ready_missing_api_base(tmp_path):
    """配置存在但缺 apiBase → not_ready（R3-P3 补分支覆盖）。"""
    result = await _status(tmp_path, {"llm": {"apiKey": "sk", "model": "m"}}, True)
    assert result["ready"] is False
    assert result["probe_failed"] is False


async def test_ready_config_and_gate_passed(tmp_path):
    """配置存在 + 探测通过（flag=True）→ ready=true, probe_failed=false。"""
    data = {"llm": {"apiKey": "sk", "apiBase": "https://api.example.com/v1", "model": "m"}}
    result = await _status(tmp_path, data, True)
    assert result["ready"] is True
    assert result["probe_failed"] is False


async def test_probe_failed_config_but_gate_failed(tmp_path):
    """配置存在 + 探测失败（flag=False）→ ready=false, probe_failed=true（核心新增语义）。"""
    data = {"llm": {"apiKey": "sk", "apiBase": "https://api.example.com/v1", "model": "m"}}
    result = await _status(tmp_path, data, False)
    assert result["ready"] is False
    assert result["probe_failed"] is True
