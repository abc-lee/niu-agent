"""
Niu Configuration Manager MCP Server

Allows the assistant to read and write user configuration, identity settings, and memory.
"""

import asyncio
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from loguru import logger
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

server = Server("niu-config-manager")

# ============== Tool Schemas ==============

TOOL_SCHEMAS = {
    "get_llm_config": {
        "name": "get_llm_config",
        "description": "Get current LLM configuration (API key is hidden).",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    "set_llm_config": {
        "name": "set_llm_config",
        "description": "Set LLM configuration. preset_id loads a named config from llm-configs.json: the entry's llm AND lightrag_llm sections wholly replace the current ones (a true config switch), other parameters are ignored. Without preset_id, modifies individual fields and auto-syncs the named collection entry (two-section snapshot) when llm.presetId is set; a damaged collection file is never overwritten (sync skipped with a warning).",
        "input_schema": {
            "type": "object",
            "properties": {
                "preset_id": {
                    "type": "string",
                    "description": "Named config to load from llm-configs.json (name = llm.presetId). Loading replaces both llm and lightrag_llm sections; other parameters are ignored.",
                },
                "api_key": {"type": "string", "description": "API key"},
                "api_base": {"type": "string", "description": "API base URL"},
                "model": {"type": "string", "description": "Model name"},
                "llm_type": {
                    "type": "string",
                    "description": "Provider type: 'openai' or 'anthropic'",
                },
                "reasoning_effort": {
                    "type": "string",
                    "description": "Reasoning depth: 'none', 'low', 'medium', 'high' (model-dependent). Affects how deeply the model reasons before responding. Controls reasoning depth only, NOT thinking-chain output (thinking-chain return is controlled by litellm_kwargs.thinking).",
                },
            },
        },
    },
    "list_llm_configs": {
        "name": "list_llm_configs",
        "description": "List all named LLM configs from llm-configs.json (name + model/apiBase summary). Returns an empty list with a warning if the collection file is damaged.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    "test_llm_connection": {
        "name": "test_llm_connection",
        "description": "Test LLM connection with current configuration.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    "get_lightrag_llm_config": {
        "name": "get_lightrag_llm_config",
        "description": "Get LightRAG LLM configuration (without API key for security). Returns the lightrag_llm section if configured, otherwise indicates it will fall back to the llm section.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    "set_lightrag_llm_config": {
        "name": "set_lightrag_llm_config",
        "description": "Set LightRAG LLM configuration. preset_id loads only the entry's lightrag_llm section from llm-configs.json (other parameters are ignored). Without preset_id, modifies individual fields and auto-syncs the named collection entry when llm.presetId is set (a damaged collection file is never overwritten). If model is set to empty string, removes the lightrag_llm section so that LightRAG falls back to the main LLM configuration (also synced).",
        "input_schema": {
            "type": "object",
            "properties": {
                "preset_id": {
                    "type": "string",
                    "description": "Named config whose lightrag_llm section to load from llm-configs.json",
                },
                "api_key": {"type": "string", "description": "API key (inherits from main llm if not set)"},
                "api_base": {"type": "string", "description": "API base URL (inherits from main llm if not set)"},
                "model": {"type": "string", "description": "Model name (empty string to clear and fall back to main llm)"},
                "llm_type": {
                    "type": "string",
                    "description": "Provider type: 'openai' or 'anthropic'",
                },
                "reasoning_effort": {
                    "type": "string",
                    "description": "Reasoning depth: 'none', 'low', 'medium', 'high'. Controls reasoning depth only, NOT thinking-chain output (controlled by litellm_kwargs.thinking). Default is model default / config-page driven (不强制档位).",
                },
                "max_tokens": {
                    "type": "integer",
                    "description": "Max output tokens per response (output budget). Omit/0 = not set (server default). Set larger (e.g. 8192/16384) when long reports get truncated.",
                },
            },
        },
    },
    "get_storage_config": {
        "name": "get_storage_config",
        "description": "Get storage configuration (document root, database path).",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    "set_storage_config": {
        "name": "set_storage_config",
        "description": "Set storage configuration.",
        "input_schema": {
            "type": "object",
            "properties": {
                "document_root": {
                    "type": "string",
                    "description": "Root directory for documents",
                },
                "database_path": {
                    "type": "string",
                    "description": "Path to knowledge database",
                },
            },
        },
    },
    "get_identity": {
        "name": "get_identity",
        "description": "Get assistant identity settings (name, gender, personality).",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    "update_identity": {
        "name": "update_identity",
        "description": "Update assistant identity. User can change name, personality, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Assistant name"},
                "gender": {
                    "type": "string",
                    "description": "Gender: 'male' or 'female'",
                },
                "personality": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Personality traits (e.g., ['warm', 'professional', 'concise'])",
                },
                "greeting_style": {
                    "type": "string",
                    "description": "How to greet users",
                },
            },
        },
    },
    "get_workspace": {
        "name": "get_workspace",
        "description": "Get workspace path where documents and database are stored.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    "set_workspace": {
        "name": "set_workspace",
        "description": "Set workspace path. This is where all documents and knowledge will be stored.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace directory path (e.g., 'D:\\\\MyKnowledge')",
                }
            },
            "required": ["path"],
        },
    },
    "get_user_info": {
        "name": "get_user_info",
        "description": "Get user information (name, preferences).",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    "set_user_info": {
        "name": "set_user_info",
        "description": "Set user information.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "User's name or nickname",
                },
                "preferences": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "User preferences (e.g., ['concise answers', 'Chinese'])",
                },
            },
        },
    },
    "add_user_preference": {
        "name": "add_user_preference",
        "description": "Add a user preference to remember.",
        "input_schema": {
            "type": "object",
            "properties": {
                "preference": {
                    "type": "string",
                    "description": "Preference to remember (e.g., 'likes concise answers')",
                }
            },
            "required": ["preference"],
        },
    },
    "is_first_run": {
        "name": "is_first_run",
        "description": "Check if this is the first run (need to show setup wizard).",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    "complete_setup": {
        "name": "complete_setup",
        "description": "Complete initial setup with workspace path and optional settings.",
        "input_schema": {
            "type": "object",
            "properties": {
                "workspace_path": {
                    "type": "string",
                    "description": "Workspace directory path",
                },
                "user_name": {"type": "string", "description": "User's name"},
                "assistant_name": {
                    "type": "string",
                    "description": "Assistant name",
                },
                "user_nickname": {
                    "type": "string",
                    "description": "User's nickname or preferred form of address",
                },
                "user_occupation": {
                    "type": "string",
                    "description": "User's occupation or profession",
                },
                "user_organization": {
                    "type": "string",
                    "description": "User's workplace or organization",
                },
            },
        },
    },
    "get_full_memory": {
        "name": "get_full_memory",
        "description": "Get full memory for system prompt (all settings and info).",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    "mkdir": {
        "name": "mkdir",
        "description": "Create a directory (and parent directories if needed). Use this to create year-based directories like 'documents/2025'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path to create (e.g., 'E:\\\\tmp\\\\bot\\\\documents\\\\2025')",
                },
            },
            "required": ["path"],
        },
    },
    "copy_to_path": {
        "name": "copy_to_path",
        "description": "Copy a file to a specific destination path. Use this to organize files by year/category. Example: copy_to_path('C:/file.pdf', 'E:/workspace/documents/2025/file.pdf')",
        "input_schema": {
            "type": "object",
            "properties": {
                "source_path": {
                    "type": "string",
                    "description": "Source file path",
                },
                "dest_path": {
                    "type": "string",
                    "description": "Destination file path (absolute path)",
                },
            },
            "required": ["source_path", "dest_path"],
        },
    },
    "move_to_path": {
        "name": "move_to_path",
        "description": "Move a file to a specific destination path. Use this to organize files by year/category.",
        "input_schema": {
            "type": "object",
            "properties": {
                "source_path": {
                    "type": "string",
                    "description": "Source file path",
                },
                "dest_path": {
                    "type": "string",
                    "description": "Destination file path (absolute path)",
                },
            },
            "required": ["source_path", "dest_path"],
        },
    },
    "list_files_in_workspace": {
        "name": "list_files_in_workspace",
        "description": "List all files in the workspace documents directory.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
}


def get_tool_schemas() -> list[dict]:
    """返回所有工具的 schema 列表（用于 MCP Loader 注册）"""
    return list(TOOL_SCHEMAS.values())


# Config paths
# user-config.json 读写 ~/.niu/config/（首次启动由 niu_api.config 复制模板）
# llm-configs.json = 用户命名配置合集（与 user-config.json 同目录；键=配置名=llm.presetId）
CONFIG_DIR = Path(os.path.expanduser("~")) / ".niu" / "config"
USER_CONFIG_PATH = CONFIG_DIR / "user-config.json"
LLM_CONFIGS_PATH = CONFIG_DIR / "llm-configs.json"


# Memory paths (in user home)
def get_home_dir() -> Path:
    """Get user home directory with fallback."""
    # Try standard methods
    try:
        home = Path.home()
        if home.exists():
            return home
    except RuntimeError:
        pass

    # Fallback: check environment variables (优先 Unix/Mac 兼容的 HOME)
    for env_var in ["HOME", "USERPROFILE", "HOMEPATH"]:
        path_str = os.environ.get(env_var)
        if path_str and Path(path_str).exists():
            return Path(path_str)

    # Last resort: use current directory
    return Path.cwd()


HOME_DIR = get_home_dir()
NIU_DIR = HOME_DIR / ".niu"
MEMORY_PATH = NIU_DIR / "memory.json"


def load_user_config() -> dict[str, Any]:
    """Load user configuration.

    文件不存在时返回代码内联标准缺省（user-config.json 无模板文件设计）。
    本缺省 = Python 侧真相源，前端两处缺省（main.js get-config 兜底 /
    settings testAndSave 常量）内容必须与本处一致（tests/test_config_defaults.py
    锁定本处，E2E 验证整体一致性）。
    """
    if USER_CONFIG_PATH.exists():
        return json.loads(USER_CONFIG_PATH.read_text(encoding="utf-8"))
    return {
        "llm": {
            "presetId": "",
            "apiKey": "",
            "apiBase": "",
            "model": "",
            "type": "openai",
            "reasoning_effort": "",
            "litellm_kwargs": {},
        },
        "lightrag_llm": {
            "presetId": "",
            "apiKey": "",
            "apiBase": "",
            "model": "",
            "type": "openai",
            "reasoning_effort": "",
            "temperature": 0.2,
            "litellm_kwargs": {
                "allowed_openai_params": [],
            },
        },
        "context": {
            "contextWindowSize": 200000,
            "warningThreshold": 0.8,
            "sleepTriggerMinutes": 5,
        },
        "storage": {"documentRoot": "", "databasePath": ""},
        "firstRun": True,
        "logging": {"enabled": False, "level": "INFO"},
    }


def save_user_config(config: dict[str, Any]) -> None:
    """Save user configuration."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    USER_CONFIG_PATH.write_text(
        json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def load_memory() -> dict[str, Any]:
    """Load memory from ~/.niu/memory.json."""
    if MEMORY_PATH.exists():
        return json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
    return {
        "version": 1,
        "identity": {
            "name": "妞妞",
            "gender": "female",
            "personality": ["温暖", "专业", "简洁", "主动"],
            "greetingStyle": "友好问候，简洁明了",
        },
        "workspace": {"path": "", "createdAt": ""},
        "user": {"name": "", "preferences": []},
        "firstRun": True,
        "createdAt": "",
        "lastActiveAt": "",
    }


def save_memory(memory: dict[str, Any]) -> None:
    """Save memory to ~/.niu/memory.json."""
    NIU_DIR.mkdir(parents=True, exist_ok=True)
    memory["lastActiveAt"] = datetime.now().isoformat()
    # 原子写：先写 tmp，再 replace（保证 reader 永远看到完整文件）
    MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(MEMORY_PATH.parent),
        prefix=MEMORY_PATH.name + ".",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(memory, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, MEMORY_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_named_configs() -> dict[str, Any]:
    """Load named LLM config collection (~/.niu/config/llm-configs.json).

    返回 {"<配置名>": {"llm": {...}, "lightrag_llm": {...}}} 字典。
    文件不存在 = 空合集（返回 {}）；JSON 损坏或 configs 值非对象时抛异常，
    由调用方区分处理（读侧展示降级 / 写侧跳过保护两条路径各自 catch）。
    扁平旧格式（无 configs 顶层）data.get 返回 {} = 正常空合集语义不变。
    """
    if not LLM_CONFIGS_PATH.exists():
        return {}
    data = json.loads(LLM_CONFIGS_PATH.read_text(encoding="utf-8"))
    configs = data.get("configs", {})
    if not isinstance(configs, dict):
        raise ValueError(
            f"配置合集文件损坏: configs 应为对象，实际为 {type(configs).__name__}"
        )
    return configs


def _sync_named_config(config: dict[str, Any]) -> Optional[str]:
    """写后同步：保存时刻重读合集，单条 upsert 当前 llm+lightrag_llm 两段快照。

    仅当 llm.presetId 非空时同步；合集 JSON 损坏 → 跳过写并返回 warning 文案
    （防"损坏=空合集"整体覆写销毁全部条目）；无需同步/同步成功 → None。
    """
    name = config.get("llm", {}).get("presetId", "")
    if not name:
        return None
    try:
        configs = load_named_configs()
    except Exception as e:
        logger.warning(f"llm-configs.json 损坏，跳过写后同步: {e}")
        return "配置合集文件损坏未同步"
    configs[name] = {
        "llm": config.get("llm", {}),
        "lightrag_llm": config.get("lightrag_llm", {}),
    }
    # 原子写：tmp + replace（reader 永远看到完整文件）
    LLM_CONFIGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(LLM_CONFIGS_PATH.parent),
        prefix=LLM_CONFIGS_PATH.name + ".",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"configs": configs}, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, LLM_CONFIGS_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return None


def get_llm_config() -> dict[str, Any]:
    """Get current LLM configuration (without API key for security)."""
    config = load_user_config()
    llm = config.get("llm", {})
    return {
        "presetId": llm.get("presetId", ""),
        "apiBase": llm.get("apiBase", ""),
        "model": llm.get("model", ""),
        "type": llm.get("type", "openai"),
        "hasApiKey": bool(llm.get("apiKey", "")),
        "reasoning_effort": llm.get("reasoning_effort", ""),
    }


def set_llm_config(
    preset_id: str = None,
    api_key: str = None,
    api_base: str = None,
    model: str = None,
    llm_type: str = None,
    reasoning_effort: str = None,
) -> dict[str, Any]:
    """Set LLM configuration.

    preset_id 加载型：从 llm-configs.json 整条加载命名配置——条目的
    {llm, lightrag_llm} 两段整体替换 user-config.json 对应段（真"切换配置"），
    其余参数忽略并在结果中说明；跳过写后同步（刚读自合集，对齐是幂等 no-op）。
    不带 preset_id = 逐项修改型：写 user-config.json 后，llm.presetId 非空时
    写后同步——保存时刻重读合集，单条 upsert 该名条目 = 两段完整快照。
    """
    config = load_user_config()

    # preset_id 加载型：整条加载 = 段级整体替换
    if preset_id:
        try:
            configs = load_named_configs()
        except Exception as e:
            logger.warning(f"llm-configs.json 损坏，加载失败: {e}")
            return {"status": "error", "message": "配置合集文件损坏，无法加载"}
        entry = configs.get(preset_id)
        if entry is None:
            return {"status": "error", "message": f"配置 '{preset_id}' 不存在"}
        if not isinstance(entry, dict):
            return {"status": "error", "message": f"配置 '{preset_id}' 格式损坏"}
        llm = dict(entry.get("llm", {}))
        # presetId 归一化为键值（防手工编辑的畸形条目把 user-config.presetId 带偏）
        llm["presetId"] = preset_id
        config["llm"] = llm
        config["lightrag_llm"] = dict(entry.get("lightrag_llm", {}))
        save_user_config(config)
        result: dict[str, Any] = {"status": "updated", "llm": get_llm_config()}
        ignored = [
            name
            for name, value in (
                ("api_key", api_key),
                ("api_base", api_base),
                ("model", model),
                ("llm_type", llm_type),
                ("reasoning_effort", reasoning_effort),
            )
            if value is not None
        ]
        if ignored:
            result["message"] = "preset_id 加载已忽略其余参数: " + ", ".join(ignored)
        return result

    # 逐项修改型
    llm = config.get("llm", {})

    # Override with explicit values
    if api_key is not None:
        llm["apiKey"] = api_key
    if api_base is not None:
        llm["apiBase"] = api_base
    if model is not None:
        llm["model"] = model
    if llm_type is not None:
        llm["type"] = llm_type
    if reasoning_effort is not None:
        llm["reasoning_effort"] = reasoning_effort

    config["llm"] = llm
    save_user_config(config)

    result = {"status": "updated", "llm": get_llm_config()}
    warning = _sync_named_config(config)
    if warning:
        result["warning"] = warning
    return result


def get_lightrag_llm_config() -> dict[str, Any]:
    """Get LightRAG LLM configuration (without API key for security).

    Returns the lightrag_llm section if configured, otherwise indicates
    it will fall back to the llm section. Includes litellm_kwargs so the
    main agent can inspect response_format_mode (probe result) and other
    provider-specific params.
    """
    config = load_user_config()
    lightrag_llm = config.get("lightrag_llm", {})
    return {
        "presetId": lightrag_llm.get("presetId", ""),
        "apiBase": lightrag_llm.get("apiBase", ""),
        "model": lightrag_llm.get("model", ""),
        "type": lightrag_llm.get("type", "openai"),
        "hasApiKey": bool(lightrag_llm.get("apiKey", "")),
        "configured": bool(lightrag_llm.get("model", "")),
        "reasoning_effort": lightrag_llm.get("reasoning_effort", "none"),
        "max_tokens": lightrag_llm.get("max_tokens"),
        "temperature": lightrag_llm.get("temperature", 0.2),
        "litellm_kwargs": lightrag_llm.get("litellm_kwargs", {}),
    }


def set_lightrag_llm_config(
    preset_id: str = None,
    api_key: str = None,
    api_base: str = None,
    model: str = None,
    llm_type: str = None,
    reasoning_effort: str = None,
    max_tokens: int = None,
) -> dict[str, Any]:
    """Set LightRAG LLM configuration.

    If model is set to empty string, removes model-specific fields
    but preserves reasoning_effort 和 max_tokens（均为独立维度）.
    model='' 清空分支优先于 preset_id 加载分支（矛盾输入时清空生效）。
    """
    config = load_user_config()

    # If clearing the model (model=""), remove model-specific fields
    # but preserve reasoning_effort 和 max_tokens（均为独立维度）
    if model == "":
        lightrag_llm = config.get("lightrag_llm", {})
        for key in ("presetId", "apiKey", "apiBase", "model", "type"):
            lightrag_llm.pop(key, None)
        # Apply reasoning_effort even when clearing model (two independent dimensions)
        if reasoning_effort is not None:
            lightrag_llm["reasoning_effort"] = reasoning_effort
        if max_tokens is not None:
            if max_tokens > 0:
                lightrag_llm["max_tokens"] = max_tokens
            else:
                lightrag_llm.pop("max_tokens", None)  # 0/负数 = 清除（回退不传）
        if lightrag_llm:
            config["lightrag_llm"] = lightrag_llm
        else:
            config.pop("lightrag_llm", None)
        save_user_config(config)
        result: dict[str, Any] = {"status": "cleared", "message": "LightRAG model cleared, will use main LLM model"}
        warning = _sync_named_config(config)
        if warning:
            result["warning"] = warning
        return result

    # preset_id 加载型：只替换 lightrag_llm 段，其余参数忽略并在结果中说明；跳过写后同步
    if preset_id:
        try:
            configs = load_named_configs()
        except Exception as e:
            logger.warning(f"llm-configs.json 损坏，加载失败: {e}")
            return {"status": "error", "message": "配置合集文件损坏，无法加载"}
        entry = configs.get(preset_id)
        if entry is None:
            return {"status": "error", "message": f"配置 '{preset_id}' 不存在"}
        if not isinstance(entry, dict):
            return {"status": "error", "message": f"配置 '{preset_id}' 格式损坏"}
        config["lightrag_llm"] = dict(entry.get("lightrag_llm", {}))
        save_user_config(config)
        result = {"status": "updated", "lightrag_llm": get_lightrag_llm_config()}
        ignored = [
            name
            for name, value in (
                ("api_key", api_key),
                ("api_base", api_base),
                ("model", model),
                ("llm_type", llm_type),
                ("reasoning_effort", reasoning_effort),
                ("max_tokens", max_tokens),
            )
            if value is not None
        ]
        if ignored:
            result["message"] = "preset_id 加载已忽略其余参数: " + ", ".join(ignored)
        return result

    # 逐项修改型
    lightrag_llm = config.get("lightrag_llm", {})

    # Override with explicit values
    if api_key is not None:
        lightrag_llm["apiKey"] = api_key
    if api_base is not None:
        lightrag_llm["apiBase"] = api_base
    if model is not None:
        lightrag_llm["model"] = model
    if llm_type is not None:
        lightrag_llm["type"] = llm_type
    if reasoning_effort is not None:
        lightrag_llm["reasoning_effort"] = reasoning_effort
    if max_tokens is not None:
        if max_tokens > 0:
            lightrag_llm["max_tokens"] = max_tokens
        else:
            lightrag_llm.pop("max_tokens", None)  # 0/负数 = 清除（回退不传）

    config["lightrag_llm"] = lightrag_llm
    save_user_config(config)

    result = {"status": "updated", "lightrag_llm": get_lightrag_llm_config()}
    warning = _sync_named_config(config)
    if warning:
        result["warning"] = warning
    return result


def list_llm_configs() -> dict[str, Any]:
    """List all named LLM configs from the collection.

    返回 {"configs": [{"name", "model", "apiBase"}]}，供 Agent 发现可用配置。
    合集损坏 → 读侧降级：空列表 + warning 字段。
    """
    try:
        configs = load_named_configs()
    except Exception as e:
        logger.warning(f"llm-configs.json 损坏，按空合集降级: {e}")
        return {"configs": [], "warning": "配置合集文件损坏"}
    return {
        "configs": [
            {
                "name": name,
                "model": entry.get("llm", {}).get("model", ""),
                "apiBase": entry.get("llm", {}).get("apiBase", ""),
            }
            for name, entry in configs.items()
            # 畸形条目（手工编辑成非对象）跳过，不抛 AttributeError
            if isinstance(entry, dict)
        ]
    }


def test_llm_connection() -> dict[str, Any]:
    """Test LLM connection with current configuration."""
    import httpx

    config = load_user_config()
    llm = config.get("llm", {})
    api_key = llm.get("apiKey", "")
    api_base = llm.get("apiBase", "")
    model = llm.get("model", "")
    llm_type = llm.get("type", "openai")

    if not api_key:
        return {"status": "error", "message": "No API key configured"}

    if not api_base:
        return {"status": "error", "message": "No API base URL configured"}

    try:
        with httpx.Client(timeout=10.0) as client:
            if llm_type == "anthropic":
                # Test Anthropic API
                response = client.get(
                    f"{api_base.rsplit('/', 1)[0]}/models",
                    headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
                )
            else:
                # Test OpenAI-compatible API
                response = client.get(
                    f"{api_base}/models", headers={"Authorization": f"Bearer {api_key}"}
                )

            if response.status_code == 200:
                return {"status": "success", "message": "Connection successful"}
            else:
                return {
                    "status": "error",
                    "message": f"HTTP {response.status_code}: {response.text[:100]}",
                }

    except Exception as e:
        return {"status": "error", "message": str(e)}


def get_storage_config() -> dict[str, Any]:
    """Get storage configuration."""
    config = load_user_config()
    return config.get("storage", {"documentRoot": "", "databasePath": ""})


def set_storage_config(
    document_root: str = None, database_path: str = None
) -> dict[str, Any]:
    """Set storage configuration."""
    document_root = os.path.expanduser(document_root) if document_root else document_root
    database_path = os.path.expanduser(database_path) if database_path else database_path
    config = load_user_config()
    storage = config.get("storage", {})

    if document_root is not None:
        storage["documentRoot"] = document_root
    if database_path is not None:
        storage["databasePath"] = database_path

    config["storage"] = storage
    save_user_config(config)

    return {"status": "updated", "storage": storage}


# ========== Identity & Memory Management ==========


def get_identity() -> dict[str, Any]:
    """Get assistant identity settings."""
    memory = load_memory()
    return memory.get(
        "identity",
        {
            "name": "妞妞",
            "gender": "female",
            "personality": ["温暖", "专业", "简洁", "主动"],
            "greetingStyle": "友好问候，简洁明了",
        },
    )


def update_identity(
    name: str = None,
    gender: str = None,
    personality: list[str] = None,
    greeting_style: str = None,
) -> dict[str, Any]:
    """Update assistant identity settings."""
    memory = load_memory()
    identity = memory.get("identity", {})

    if name is not None:
        identity["name"] = name
    if gender is not None:
        identity["gender"] = gender
    if personality is not None:
        identity["personality"] = personality
    if greeting_style is not None:
        identity["greetingStyle"] = greeting_style

    memory["identity"] = identity
    save_memory(memory)

    return {"status": "updated", "identity": identity}


def get_workspace() -> dict[str, Any]:
    """Get workspace configuration."""
    memory = load_memory()
    return memory.get("workspace", {"path": "", "createdAt": ""})


def set_workspace(path: str) -> dict[str, Any]:
    """Set workspace path."""
    path = os.path.expanduser(path) if path else path
    memory = load_memory()

    # Create directory if it doesn't exist
    workspace_path = Path(path)
    workspace_path.mkdir(parents=True, exist_ok=True)

    # Create subdirectories
    (workspace_path / "documents").mkdir(exist_ok=True)

    memory["workspace"] = {
        "path": str(workspace_path.absolute()),
        "createdAt": memory.get("workspace", {}).get("createdAt")
        or datetime.now().isoformat(),
    }

    # Also update user config storage
    config = load_user_config()
    config["storage"]["documentRoot"] = str(workspace_path.absolute())
    config["storage"]["databasePath"] = str(workspace_path.absolute() / "knowledge.db")
    save_user_config(config)

    save_memory(memory)

    return {"status": "updated", "workspace": memory["workspace"]}


def get_user_info() -> dict[str, Any]:
    """Get user information."""
    memory = load_memory()
    return memory.get("user", {"name": "", "preferences": []})


def set_user_info(name: str = None, preferences: list[str] = None) -> dict[str, Any]:
    """Set user information."""
    memory = load_memory()
    user = memory.get("user", {})

    if name is not None:
        user["name"] = name
    if preferences is not None:
        user["preferences"] = preferences

    memory["user"] = user
    save_memory(memory)

    return {"status": "updated", "user": user}


def add_user_preference(preference: str) -> dict[str, Any]:
    """Add a user preference."""
    memory = load_memory()
    user = memory.get("user", {"name": "", "preferences": []})
    preferences = user.get("preferences", [])

    if preference not in preferences:
        preferences.append(preference)
        user["preferences"] = preferences
        memory["user"] = user
        save_memory(memory)

    return {"status": "updated", "preferences": preferences}


def is_first_run() -> bool:
    """Check if this is the first run."""
    memory = load_memory()
    return memory.get("firstRun", True)


def complete_setup(
    workspace_path: str = None,
    user_name: str = None,
    assistant_name: str = None,
    user_nickname: str = None,
    user_occupation: str = None,
    user_organization: str = None,
) -> dict[str, Any]:
    """Complete initial setup."""
    workspace_path = os.path.expanduser(workspace_path) if workspace_path else workspace_path
    memory = load_memory()

    # Set workspace if provided
    if workspace_path:
        workspace_path_obj = Path(workspace_path)
        workspace_path_obj.mkdir(parents=True, exist_ok=True)
        (workspace_path_obj / "documents").mkdir(exist_ok=True)

        memory["workspace"] = {
            "path": str(workspace_path_obj.absolute()),
            "createdAt": datetime.now().isoformat(),
        }

        # Update user config
        config = load_user_config()
        config["storage"]["documentRoot"] = str(workspace_path_obj.absolute())
        config["storage"]["databasePath"] = str(
            workspace_path_obj.absolute() / "knowledge.db"
        )
        save_user_config(config)

    # Set user name if provided
    if user_name:
        memory["user"] = memory.get("user", {})
        memory["user"]["name"] = user_name
    if user_nickname:
        memory["user"]["nickname"] = user_nickname
    if user_occupation:
        memory["user"]["occupation"] = user_occupation
    if user_organization:
        memory["user"]["organization"] = user_organization

    # Set assistant name if provided
    if assistant_name:
        memory["identity"] = memory.get("identity", {})
        memory["identity"]["name"] = assistant_name

    # Mark as initialized
    memory["firstRun"] = False
    memory["createdAt"] = memory.get("createdAt") or datetime.now().isoformat()

    save_memory(memory)

    # Also update user config
    config = load_user_config()
    config["firstRun"] = False
    if "context" not in config:
        config["context"] = {
            "contextWindowSize": 200000,
            "warningThreshold": 0.8,
            "sleepTriggerMinutes": 5,
        }
    save_user_config(config)

    return {"status": "completed", "memory": memory}


def get_full_memory() -> dict[str, Any]:
    """Get full memory for system prompt injection."""
    return load_memory()


# ========== File Operations ==========

import shutil
from datetime import datetime


def mkdir(path: str) -> dict[str, Any]:
    """Create a directory (and parent directories if needed).

    Args:
        path: Directory path to create

    Returns:
        dict with status and path
    """
    path = os.path.expanduser(path) if path else path
    try:
        dir_path = Path(path)
        dir_path.mkdir(parents=True, exist_ok=True)
        return {
            "status": "success",
            "action": "mkdir",
            "path": str(dir_path.absolute()),
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


def copy_to_path(source_path: str, dest_path: str) -> dict[str, Any]:
    """Copy a file to a specific destination path.

    Use this when you need to organize files by year/category.
    Example: copy_to_path("C:/file.pdf", "E:/workspace/documents/2025/report/file.pdf")

    Args:
        source_path: Source file path
        dest_path: Destination file path (absolute path)

    Returns:
        dict with status, source, destination, size
    """
    source_path = os.path.expanduser(source_path) if source_path else source_path
    dest_path = os.path.expanduser(dest_path) if dest_path else dest_path
    source = Path(source_path)
    if not source.exists():
        return {"status": "error", "message": f"Source file not found: {source_path}"}

    dest = Path(dest_path)
    # Create parent directories if needed
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Copy file
    shutil.copy2(source, dest)

    return {
        "status": "success",
        "action": "copy",
        "source": str(source.absolute()),
        "destination": str(dest.absolute()),
        "size": dest.stat().st_size,
    }


def move_to_path(source_path: str, dest_path: str) -> dict[str, Any]:
    """Move a file to a specific destination path.

    Use this when you need to organize files by year/category.
    Example: move_to_path("C:/file.pdf", "E:/workspace/documents/2025/report/file.pdf")

    Args:
        source_path: Source file path
        dest_path: Destination file path (absolute path)

    Returns:
        dict with status, source, destination, size
    """
    source_path = os.path.expanduser(source_path) if source_path else source_path
    dest_path = os.path.expanduser(dest_path) if dest_path else dest_path
    source = Path(source_path)
    if not source.exists():
        return {"status": "error", "message": f"Source file not found: {source_path}"}

    dest = Path(dest_path)
    # Create parent directories if needed
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Move file
    shutil.move(str(source), str(dest))

    return {
        "status": "success",
        "action": "move",
        "source": str(source.absolute()),
        "destination": str(dest.absolute()),
        "size": dest.stat().st_size,
    }


def list_files_in_workspace() -> list[dict[str, Any]]:
    """List all files in the workspace documents directory."""
    memory = load_memory()
    workspace = memory.get("workspace", {})
    workspace_path = workspace.get("path", "")

    if not workspace_path:
        return []

    documents_dir = Path(workspace_path) / "documents"
    if not documents_dir.exists():
        return []

    files = []
    for f in documents_dir.iterdir():
        if f.is_file():
            files.append(
                {"name": f.name, "path": str(f.absolute()), "size": f.stat().st_size}
            )

    return files


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        # LLM Configuration
        Tool(
            name="get_llm_config",
            description="Get current LLM configuration (API key is hidden).",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="set_llm_config",
            description="Set LLM configuration. preset_id loads a named config from llm-configs.json (replaces both llm and lightrag_llm sections; other parameters ignored); without preset_id, modifies individual fields and auto-syncs the named collection entry.",
            inputSchema={
                "type": "object",
                "properties": {
                    "preset_id": {
                        "type": "string",
                        "description": "Named config to load from llm-configs.json (name = llm.presetId)",
                    },
                    "api_key": {"type": "string", "description": "API key"},
                    "api_base": {"type": "string", "description": "API base URL"},
                    "model": {"type": "string", "description": "Model name"},
                    "llm_type": {
                        "type": "string",
                        "description": "Provider type: 'openai' or 'anthropic'",
                    },
                    "reasoning_effort": {
                        "type": "string",
                        "description": "Reasoning depth: 'none', 'low', 'medium', 'high' (model-dependent). Affects how deeply the model reasons before responding. Controls reasoning depth only, NOT thinking-chain output (thinking-chain return is controlled by litellm_kwargs.thinking).",
                    },
                },
            },
        ),
        Tool(
            name="list_llm_configs",
            description="List all named LLM configs from llm-configs.json (name + model/apiBase summary).",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="test_llm_connection",
            description="Test LLM connection with current configuration.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="get_lightrag_llm_config",
            description="Get LightRAG LLM configuration. Returns model, reasoning_effort, and whether it falls back to main llm.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="set_lightrag_llm_config",
            description="Set LightRAG LLM configuration. preset_id loads only the entry's lightrag_llm section from llm-configs.json (other parameters ignored); without preset_id, modifies individual fields and auto-syncs the named collection entry. If model='', clears the section (falls back to main llm).",
            inputSchema={
                "type": "object",
                "properties": {
                    "preset_id": {
                        "type": "string",
                        "description": "Named config whose lightrag_llm section to load from llm-configs.json",
                    },
                    "api_key": {"type": "string", "description": "API key (inherits from main llm if not set)"},
                    "api_base": {"type": "string", "description": "API base URL (inherits from main llm if not set)"},
                    "model": {"type": "string", "description": "Model name (empty string to clear)"},
                    "llm_type": {
                        "type": "string",
                        "description": "Provider type: 'openai' or 'anthropic'",
                    },
                    "reasoning_effort": {
                        "type": "string",
                        "description": "Reasoning depth: 'none', 'low', 'medium', 'high'. Controls reasoning depth only, NOT thinking-chain output (controlled by litellm_kwargs.thinking). Default is model default / config-page driven (不强制档位).",
                    },
                    "max_tokens": {
                        "type": "integer",
                        "description": "Max output tokens per response (output budget). Omit/0 = not set (server default). Set larger (e.g. 8192/16384) when long reports get truncated.",
                    },
                },
            },
        ),
        # Storage Configuration
        Tool(
            name="get_storage_config",
            description="Get storage configuration (document root, database path).",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="set_storage_config",
            description="Set storage configuration.",
            inputSchema={
                "type": "object",
                "properties": {
                    "document_root": {
                        "type": "string",
                        "description": "Root directory for documents",
                    },
                    "database_path": {
                        "type": "string",
                        "description": "Path to knowledge database",
                    },
                },
            },
        ),
        # Identity & Memory Management
        Tool(
            name="get_identity",
            description="Get assistant identity settings (name, gender, personality).",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="update_identity",
            description="Update assistant identity. User can change name, personality, etc.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Assistant name"},
                    "gender": {
                        "type": "string",
                        "description": "Gender: 'male' or 'female'",
                    },
                    "personality": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Personality traits (e.g., ['warm', 'professional', 'concise'])",
                    },
                    "greeting_style": {
                        "type": "string",
                        "description": "How to greet users",
                    },
                },
            },
        ),
        Tool(
            name="get_workspace",
            description="Get workspace path where documents and database are stored.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="set_workspace",
            description="Set workspace path. This is where all documents and knowledge will be stored.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace directory path (e.g., 'D:\\\\MyKnowledge')",
                    }
                },
                "required": ["path"],
            },
        ),
        Tool(
            name="get_user_info",
            description="Get user information (name, preferences).",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="set_user_info",
            description="Set user information.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "User's name or nickname",
                    },
                    "preferences": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "User preferences (e.g., ['concise answers', 'Chinese'])",
                    },
                },
            },
        ),
        Tool(
            name="add_user_preference",
            description="Add a user preference to remember.",
            inputSchema={
                "type": "object",
                "properties": {
                    "preference": {
                        "type": "string",
                        "description": "Preference to remember (e.g., 'likes concise answers')",
                    }
                },
                "required": ["preference"],
            },
        ),
        # First Run & Setup
        Tool(
            name="is_first_run",
            description="Check if this is the first run (need to show setup wizard).",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="complete_setup",
            description="Complete initial setup with workspace path and optional settings.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace_path": {
                        "type": "string",
                        "description": "Workspace directory path",
                    },
                    "user_name": {"type": "string", "description": "User's name"},
                    "assistant_name": {
                        "type": "string",
                        "description": "Assistant name",
                    },
                    "user_nickname": {
                        "type": "string",
                        "description": "User's nickname or preferred form of address",
                    },
                    "user_occupation": {
                        "type": "string",
                        "description": "User's occupation or profession",
                    },
                    "user_organization": {
                        "type": "string",
                        "description": "User's workplace or organization",
                    },
                },
            },
        ),
        Tool(
            name="get_full_memory",
            description="Get full memory for system prompt (all settings and info).",
            inputSchema={"type": "object", "properties": {}},
        ),
        # File Operations - Basic Tools
        Tool(
            name="mkdir",
            description="Create a directory (and parent directories if needed). Use this to create year-based directories like 'documents/2025'.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path to create (e.g., 'E:\\\\tmp\\\\bot\\\\documents\\\\2025')",
                    },
                },
                "required": ["path"],
            },
        ),
        Tool(
            name="copy_to_path",
            description="Copy a file to a specific destination path. Use this to organize files by year/category. Example: copy_to_path('C:/file.pdf', 'E:/workspace/documents/2025/file.pdf')",
            inputSchema={
                "type": "object",
                "properties": {
                    "source_path": {
                        "type": "string",
                        "description": "Source file path",
                    },
                    "dest_path": {
                        "type": "string",
                        "description": "Destination file path (absolute path)",
                    },
                },
                "required": ["source_path", "dest_path"],
            },
        ),
        Tool(
            name="move_to_path",
            description="Move a file to a specific destination path. Use this to organize files by year/category.",
            inputSchema={
                "type": "object",
                "properties": {
                    "source_path": {
                        "type": "string",
                        "description": "Source file path",
                    },
                    "dest_path": {
                        "type": "string",
                        "description": "Destination file path (absolute path)",
                    },
                },
                "required": ["source_path", "dest_path"],
            },
        ),
        Tool(
            name="list_files_in_workspace",
            description="List all files in the workspace documents directory.",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    try:
        result: Any = None

        # LLM Configuration
        if name == "get_llm_config":
            result = get_llm_config()
        elif name == "set_llm_config":
            result = set_llm_config(
                preset_id=arguments.get("preset_id"),
                api_key=arguments.get("api_key"),
                api_base=arguments.get("api_base"),
                model=arguments.get("model"),
                llm_type=arguments.get("llm_type"),
                reasoning_effort=arguments.get("reasoning_effort"),
            )
        elif name == "list_llm_configs":
            result = list_llm_configs()
        elif name == "test_llm_connection":
            result = test_llm_connection()
        elif name == "get_lightrag_llm_config":
            result = get_lightrag_llm_config()
        elif name == "set_lightrag_llm_config":
            result = set_lightrag_llm_config(
                preset_id=arguments.get("preset_id"),
                api_key=arguments.get("api_key"),
                api_base=arguments.get("api_base"),
                model=arguments.get("model"),
                llm_type=arguments.get("llm_type"),
                reasoning_effort=arguments.get("reasoning_effort"),
                max_tokens=arguments.get("max_tokens"),
            )

        # Storage Configuration
        elif name == "get_storage_config":
            result = get_storage_config()
        elif name == "set_storage_config":
            result = set_storage_config(
                document_root=arguments.get("document_root"),
                database_path=arguments.get("database_path"),
            )

        # Identity & Memory Management
        elif name == "get_identity":
            result = get_identity()
        elif name == "update_identity":
            result = update_identity(
                name=arguments.get("name"),
                gender=arguments.get("gender"),
                personality=arguments.get("personality"),
                greeting_style=arguments.get("greeting_style"),
            )
        elif name == "get_workspace":
            result = get_workspace()
        elif name == "set_workspace":
            result = set_workspace(path=arguments.get("path"))
        elif name == "get_user_info":
            result = get_user_info()
        elif name == "set_user_info":
            result = set_user_info(
                name=arguments.get("name"),
                preferences=arguments.get("preferences"),
            )
        elif name == "add_user_preference":
            result = add_user_preference(preference=arguments.get("preference"))

        # First Run & Setup
        elif name == "is_first_run":
            result = {"firstRun": is_first_run()}
        elif name == "complete_setup":
            result = complete_setup(
                workspace_path=arguments.get("workspace_path"),
                user_name=arguments.get("user_name"),
                assistant_name=arguments.get("assistant_name"),
                user_nickname=arguments.get("user_nickname"),
                user_occupation=arguments.get("user_occupation"),
                user_organization=arguments.get("user_organization"),
            )
        elif name == "get_full_memory":
            result = get_full_memory()

        # File Operations - Basic Tools
        elif name == "mkdir":
            result = mkdir(path=arguments.get("path"))
        elif name == "copy_to_path":
            result = copy_to_path(
                source_path=arguments.get("source_path"),
                dest_path=arguments.get("dest_path"),
            )
        elif name == "move_to_path":
            result = move_to_path(
                source_path=arguments.get("source_path"),
                dest_path=arguments.get("dest_path"),
            )
        elif name == "list_files_in_workspace":
            result = list_files_in_workspace()

        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

    except Exception as e:
        logger.exception(f"Error: {e}")
        return [TextContent(type="text", text=f"Error: {e}")]


async def run_server():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


def main():
    asyncio.run(run_server())


if __name__ == "__main__":
    main()
