"""Test that raw_http logs are masked before hitting disk (_mask_sensitive).

Security regression guard: _write_raw_log used to dump kwargs verbatim,
leaving plaintext api_key in ~/.niu/logs/raw_http/*_request.json.
"""

import json

import pytest

from agent.generic.litellm_adapter import (
    _is_sensitive_key,
    _mask_api_key_value,
    _mask_sensitive,
)

# --- _is_sensitive_key ---------------------------------------------------

@pytest.mark.parametrize("key", [
    "api_key",
    "apiKey",
    "API_KEY",
    "apikey",
    "api-key",
    "x-api-key",
    "Authorization",
    "authorization",
    "access_token",
    "id_token",
    "jwt_token",
    "client_secret",
    "api_secret",
])
def test_is_sensitive_key_matches(key: str):
    assert _is_sensitive_key(key)


@pytest.mark.parametrize("key", [
    "model",
    "messages",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "tokenizer",
    "stream",
    "timestamp",
])
def test_is_sensitive_key_not_matched(key: str):
    assert not _is_sensitive_key(key)


# --- _mask_api_key_value --------------------------------------------------

def test_mask_api_key_value_long():
    assert _mask_api_key_value("sk-abcdefghijklmnop") == "sk-a...nop"


def test_mask_api_key_value_short():
    assert _mask_api_key_value("short") == "***"


def test_mask_api_key_value_empty_none_non_string_untouched():
    assert _mask_api_key_value("") == ""
    assert _mask_api_key_value(None) is None
    assert _mask_api_key_value(12345) == 12345


# --- _mask_sensitive ------------------------------------------------------

def test_mask_sensitive_top_level_api_key_variants():
    raw = {
        "api_key": "ark-98de123456789f889",
        "apiKey": "sk-abcdef1234567890",
        "API_KEY": "AKIAIOSFODNN7EXAMPLE",
        "apikey": "pk-live-abcdefgh",
    }
    masked = _mask_sensitive(raw)
    assert masked["api_key"] == "ark-...889"
    assert masked["apiKey"] == "sk-a...890"
    assert masked["API_KEY"] == "AKIA...PLE"
    assert masked["apikey"] == "pk-l...fgh"


def test_mask_sensitive_authorization_and_token_and_secret():
    raw = {
        "Authorization": "Bearer sk-abcdef1234567890",
        "access_token": "ghp_abcdef1234567890",
        "client_secret": "super-secret-value",
    }
    masked = _mask_sensitive(raw)
    assert masked["Authorization"] == "Bear...890"
    assert masked["access_token"] == "ghp_...890"
    assert masked["client_secret"] == "supe...lue"


def test_mask_sensitive_token_count_fields_untouched():
    """prompt_tokens 等是计数字段，不是密钥，不能误伤。"""
    raw = {"usage": {"prompt_tokens": 12, "completion_tokens": 34, "total_tokens": 46}}
    assert _mask_sensitive(raw) == raw


def test_mask_sensitive_nested_dict_and_list():
    raw = {
        "request_params": {
            "model": "gpt-4o",
            "api_key": "sk-abcdef1234567890",
            "headers": [
                {"Authorization": "Bearer sk-abcdef1234567890"},
                {"X-API-Key": "sec-1234567890abcd"},
            ],
        },
        "tools": [{"name": "search", "params": {"token": "tok-abcdef123456"}}],
    }
    masked = _mask_sensitive(raw)
    assert masked["request_params"]["api_key"] == "sk-a...890"
    assert masked["request_params"]["headers"][0]["Authorization"] == "Bear...890"
    assert masked["request_params"]["headers"][1]["X-API-Key"] == "sec-...bcd"
    assert masked["tools"][0]["params"]["token"] == "tok-...456"


def test_mask_sensitive_no_sensitive_fields_unchanged():
    raw = {
        "timestamp": "2026-08-12 10:00:00",
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "hello"}],
        "usage": {"prompt_tokens": 5},
    }
    assert _mask_sensitive(raw) == raw


def test_mask_sensitive_is_pure_function():
    """不修改入参（纯函数契约）。"""
    raw = {
        "request_params": {"api_key": "sk-abcdef1234567890", "model": "gpt-4o"},
        "nested": {"list": [{"token": "tok-abcdef123456"}]},
    }
    original = json.loads(json.dumps(raw))
    _mask_sensitive(raw)
    assert raw == original


# --- _write_raw_log 集成 ---------------------------------------------------

def test_write_raw_log_masks_before_disk(tmp_path, monkeypatch):
    """落盘文件里不得出现明文 api_key。"""
    from agent.generic import litellm_adapter
    from niu_api.config import LoggingConfig

    monkeypatch.setattr(litellm_adapter, "_get_app_log_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "niu_api.config.get_logging_config",
        lambda: LoggingConfig(enabled=True, level="INFO"),
    )

    litellm_adapter._write_raw_log("request", {
        "timestamp": "2026-08-12 10:00:00",
        "model": "gpt-4o",
        "request_params": {
            "api_base": "https://api.example.com/v1",
            "api_key": "sk-abcdef1234567890",
            "stream": True,
        },
        "usage": {"prompt_tokens": 12, "total_tokens": 12},
    }, seq=7)

    written = json.loads(
        (tmp_path / "raw_http" / "20260812" / "000007_request.json").read_text(encoding="utf-8")
    )
    assert written["request_params"]["api_key"] == "sk-a...890"
    assert "sk-abcdef1234567890" not in json.dumps(written)
    # 非敏感字段保持原样
    assert written["request_params"]["stream"] is True
    assert written["usage"]["prompt_tokens"] == 12


def test_write_raw_log_disabled_skips(tmp_path, monkeypatch):
    """logging.enabled=False 时静默跳过（不落盘）。"""
    from agent.generic import litellm_adapter
    from niu_api.config import LoggingConfig

    monkeypatch.setattr(litellm_adapter, "_get_app_log_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "niu_api.config.get_logging_config",
        lambda: LoggingConfig(enabled=False),
    )

    litellm_adapter._write_raw_log("request", {"api_key": "sk-abcdef1234567890"}, seq=1)
    assert not list(tmp_path.rglob("*.json"))
