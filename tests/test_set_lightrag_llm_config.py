"""set_lightrag_llm_config max_tokens 参数测试。

monkeypatch 配置路径到 tmp_path，不触碰真实 ~/.niu/config/。
"""
import json
import sys
from pathlib import Path

import pytest

# config-manager 是独立包（mcp-servers/config-manager/src 布局），加入 sys.path
sys.path.insert(
    0,
    str(
        Path(__file__).resolve().parent.parent
        / "mcp-servers"
        / "config-manager"
        / "src"
    ),
)

import niu_config_manager as ncm


@pytest.fixture
def tmp_config(monkeypatch, tmp_path):
    """把模块级 CONFIG_DIR / USER_CONFIG_PATH 重定向到 tmp_path。"""
    monkeypatch.setattr(ncm, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ncm, "USER_CONFIG_PATH", tmp_path / "user-config.json")
    return tmp_path


def _read_config(tmp_config):
    return json.loads((tmp_config / "user-config.json").read_text(encoding="utf-8"))


def test_set_lightrag_llm_config_max_tokens(tmp_config):
    """set_lightrag_llm_config(max_tokens=8192) 写入 lightrag_llm.max_tokens。"""
    result = ncm.set_lightrag_llm_config(model="m", max_tokens=8192)
    assert result["status"] == "updated"

    config = _read_config(tmp_config)
    assert config["lightrag_llm"]["max_tokens"] == 8192


def test_set_lightrag_llm_config_max_tokens_clear(tmp_config):
    """max_tokens=0 清除该键（回退不传）。"""
    ncm.set_lightrag_llm_config(model="m", max_tokens=8192)
    assert _read_config(tmp_config)["lightrag_llm"]["max_tokens"] == 8192

    ncm.set_lightrag_llm_config(max_tokens=0)
    config = _read_config(tmp_config)
    assert "max_tokens" not in config["lightrag_llm"]


def test_set_lightrag_llm_config_max_tokens_none_untouched(tmp_config):
    """max_tokens=None（缺省）不动现有值。"""
    ncm.set_lightrag_llm_config(model="m", max_tokens=8192)
    assert _read_config(tmp_config)["lightrag_llm"]["max_tokens"] == 8192

    # 缺省调用（如只改 reasoning_effort）不得动 max_tokens
    ncm.set_lightrag_llm_config(reasoning_effort="low")
    config = _read_config(tmp_config)
    assert config["lightrag_llm"]["max_tokens"] == 8192
    assert config["lightrag_llm"]["reasoning_effort"] == "low"


def test_get_lightrag_llm_config_returns_max_tokens(tmp_config):
    """get_lightrag_llm_config 返回 dict 含 max_tokens（读回确认）。"""
    ncm.set_lightrag_llm_config(model="m", max_tokens=8192)
    result = ncm.get_lightrag_llm_config()
    assert result["max_tokens"] == 8192

    # 未设置时返回 None（缺省不传）
    ncm.set_lightrag_llm_config(max_tokens=0)
    assert ncm.get_lightrag_llm_config()["max_tokens"] is None
