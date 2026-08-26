"""
Config loading for Niu API Server
"""

import json
import os
import shutil
from pathlib import Path
from typing import Any

from loguru import logger


class LLMConfig:
    """LLM configuration"""

    def __init__(self, config: dict[str, Any]):
        self.provider = config.get("type", "openai")  # minimax, openai, anthropic, etc.
        self.api_key = config.get("apiKey", "")
        self.api_base = config.get("apiBase", "")
        self.model = config.get("model", "gpt-4o")
        self.preset_id = config.get("presetId", "")


class LoggingConfig:
    """Logging sub-configuration.

    缺省 enabled=False：所有日志输出（loguru sink、raw_http 两层日志、
    llm_interaction 可读日志、im_adapter_stderr、http-log 服务）应关闭。
    只有显式 enabled=True 才按 level 输出。
    """

    def __init__(self, enabled: bool = False, level: str = "INFO"):
        self.enabled = bool(enabled)
        self.level = str(level).upper() if level else "INFO"


def _parse_logging(data: dict) -> LoggingConfig:
    """从原始 config dict 解析 logging 子节点，缺省 enabled=False。"""
    raw = data.get("logging") or {}
    return LoggingConfig(
        enabled=raw.get("enabled", False),
        level=raw.get("level", "INFO"),
    )


def _get_bundle_config_dir() -> Path:
    """返回 bundle/exe 内的 config 目录（作为模板源）。
    dev 模式: __file__=niu_api/config.py → parent.parent = 项目根 → /config
    bundle 模式: __file__=Contents/Resources/niu_api/config.py → parent.parent = Contents/Resources → /config
    """
    return Path(__file__).resolve().parent.parent / "config"


def _get_config_path() -> str:
    """返回 ~/.niu/config/user-config.json。首次启动从 bundle 内复制模板。"""
    home = os.path.expanduser("~")
    niu_config_dir = Path(home) / ".niu" / "config"
    user_config = niu_config_dir / "user-config.json"
    if not user_config.exists():
        bundle_config = _get_bundle_config_dir() / "user-config.json"
        if bundle_config.exists():
            niu_config_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(bundle_config, user_config)
    return str(user_config)


# 模块级默认 config 路径常量（让测试 monkeypatch 生效）
CONFIG_PATH = _get_config_path()


class Config:
    """Niu API configuration"""

    def __init__(self):
        self.llm: LLMConfig | None = None
        self.storage: dict[str, str] = {"documentRoot": "", "databasePath": ""}
        self.first_run: bool = True
        self.logging: LoggingConfig = LoggingConfig()

    @classmethod
    def load(cls, config_path: str | None = None) -> "Config":
        """Load configuration from file"""
        if config_path is None:
            # Default: config/user-config.json
            config_path = CONFIG_PATH

        cfg = cls()

        try:
            with open(config_path, encoding="utf-8") as f:
                data = json.load(f)

            if "llm" in data and data["llm"]:
                cfg.llm = LLMConfig(data["llm"])
                cfg.first_run = data.get("firstRun", False)
            else:
                # Create empty LLM config for first-run scenario
                cfg.llm = LLMConfig({})

            if "storage" in data:
                cfg.storage = data["storage"]

            cfg.logging = _parse_logging(data)

            logger.info(f"Config loaded from {config_path}")
            logger.info(f"LLM: provider={cfg.llm.provider}, model={cfg.llm.model}")

        except FileNotFoundError:
            logger.warning(f"Config file not found: {config_path}")
            logger.warning("Using default configuration")
            cfg.llm = LLMConfig({})
        except Exception as e:
            logger.error(f"Failed to load config: {e}")
            cfg.llm = LLMConfig({})

        return cfg


# Global config instance
_config: Config | None = None


def get_config() -> Config:
    """Get global config instance"""
    global _config
    if _config is None:
        _config = Config.load()
    return _config


def get_logging_config() -> LoggingConfig:
    """获取 logging 子配置。失败时兜底返回 enabled=False（保守默认）。

    agent/generic/litellm_adapter.py:38 的 install_http_logger() 在模块导入时
    调用本函数，此时若 config 加载异常不能让 Agent 模块 import 失败。
    """
    try:
        return get_config().logging
    except Exception:
        return LoggingConfig(enabled=False, level="INFO")
