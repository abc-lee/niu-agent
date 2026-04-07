"""
Config loading for Niu API Server
"""

import json
import os
from pathlib import Path
from typing import Optional, Dict, Any
from loguru import logger


class LLMConfig:
    """LLM configuration"""

    def __init__(self, config: Dict[str, Any]):
        self.provider = config.get("type", "openai")  # minimax, openai, anthropic, etc.
        self.api_key = config.get("apiKey", "")
        self.api_base = config.get("apiBase", "")
        self.model = config.get("model", "gpt-4o")
        self.preset_id = config.get("presetId", "")


class Config:
    """Niu API configuration"""

    def __init__(self):
        self.llm: Optional[LLMConfig] = None
        self.storage: Dict[str, str] = {"documentRoot": "", "databasePath": ""}
        self.first_run: bool = True

    @classmethod
    def load(cls, config_path: Optional[str] = None) -> "Config":
        """Load configuration from file"""
        if config_path is None:
            # Default: config/user-config.json
            config_path = os.path.join(
                os.path.dirname(__file__), "..", "config", "user-config.json"
            )

        cfg = cls()

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            if "llm" in data and data["llm"]:
                cfg.llm = LLMConfig(data["llm"])
                cfg.first_run = data.get("firstRun", False)
            else:
                # Create empty LLM config for first-run scenario
                cfg.llm = LLMConfig({})

            if "storage" in data:
                cfg.storage = data["storage"]

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
_config: Optional[Config] = None


def get_config() -> Config:
    """Get global config instance"""
    global _config
    if _config is None:
        _config = Config.load()
    return _config
