# 日志开关与控制台窗口关闭 v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `config/user-config.json` 增加 `logging.enabled` 开关（缺省 false），关闭时：所有 Python 日志（loguru + stdlib logging + uvicorn + raw_http 两层 + llm_interaction + im_adapter_stderr + http-log 服务）都不输出，Rust tracing 也不输出；Windows 上 niu.exe 编译为 GUI 子系统不弹 cmd 窗口，macOS 上构造 .app bundle 从 Finder 双击不弹 Terminal。

**Architecture:**
- **配置层**：`config/user-config.json` 顶层新增 `logging: { enabled: bool, level: str }` 子节点（缺省 `enabled=false`）。`niu_api/config.py` 增加 `LoggingConfig` 类 + `CONFIG_PATH` 常量 + `get_logging_config()` 兜底函数。Rust 启动器 `launcher/src/main.rs` 也读同一份配置（启动期一次性）。
- **Python 日志 gate**：`niu_api/__main__.py` 顶层根据 `get_logging_config().enabled` 决定 loguru sink（disabled 用 `logger.disable("")` 全局禁用）+ stdlib logging（`logging.disable(CRITICAL)`）+ uvicorn（log_level/access_log）+ http_log_router 条件 include。`agent/generic/http_logger.py` 的 `install_http_logger` 加 flag gate + 幂等守卫。`agent/generic/litellm_adapter.py` 的 `_write_raw_log`/`_write_interaction_log` 加 flag gate + 抽 `_get_app_log_dir()`。`niu_api/channel/gateway.py` 的飞书 adapter stderr 重定向按 flag gate（disabled 时用 `subprocess.DEVNULL`）。
- **Rust 控制台关闭**：`launcher/src/main.rs` 顶部加 `#![cfg_attr(all(target_os = "windows", not(debug_assertions)), windows_subsystem = "windows")]`。`launcher/build.sh` 编译后构造 macOS .app bundle（`niu.app/Contents/MacOS/niu` + `Info.plist` 含 `LSUIElement=true`）。Rust tracing 按 flag gate（disabled 时不 init tracing_subscriber）+ `log_fatal_error` 独立文件保留致命错误诊断。
- **范围界定（用户明确决定）**：Electron 前端 `console.log`（`ui/main/main.js` 28+ 处、preload 脚本）**不纳入本轮范围**——前端 console 输出在 DevTools 关闭时本就不显示，生产构建 Electron 可通过 `webPreferences.devTools: false` 关闭 DevTools，本轮不做。独立 MCP 服务器进程（如 scheduler-server 独立启动时）的 `logging.basicConfig` 也不受主 config gate 控制，本轮不覆盖（scheduler-server 在主进程内嵌时不调 basicConfig，独立启动时是另一个进程）。

**Tech Stack:** Python 3.11 + loguru + FastAPI + uvicorn + Rust + Electron 33 + macOS .app bundle + Windows GUI subsystem

---

## 上一轮教训（必读）

| 上一轮错误 | 本轮修正 |
|---|---|
| 误诊控制台窗口根因为"npm 子进程 stdio inherit" | 正确根因：Windows console subsystem + macOS 裸二进制（无 .app bundle） |
| 改 `launch_window` 的 `Stdio::inherit()` → `Stdio::null()` | 本轮**不动 launch_window stdio**（子进程 stdio 不是控制台根因） |
| 漏掉 `im_adapter_stderr.log` 日志源（gateway.py:156） | 本轮 Task 6 专门 gate 飞书 adapter stderr |
| 漏掉 Rust tracing 日志源 | 本轮 Task 8 专门 gate Rust tracing |
| 跑全量测试套件（156 失败 + 损坏 LightRAG 风险） | 本轮**禁止全量测试**，只跑本次新增测试 |

---

## 现状摸底结论（写代码前必读）

| 关注点 | 现状 | 行号 |
|---|---|---|
| Windows console subsystem | Cargo.toml/main.rs 无 `windows_subsystem` 属性，niu.exe 是 console 程序 | `launcher/Cargo.toml` + `launcher/src/main.rs:1` |
| macOS .app bundle | 无 .app 目录、无 Info.plist、build.sh 只 cp 裸二进制 | `launcher/build.sh` |
| Python loguru sink | `logger.add(sys.stderr, level="INFO")` 无开关 | `niu_api/__main__.py:35-40` |
| stdlib logging | 10+ 处 `logging.getLogger(__name__)` 散落使用，默认输出 stderr | `niu_api/db_monitor.py:21` + `niu_api/internal/*.py` + `agent/*.py` |
| raw_http transport 日志 | `install_http_logger()` 模块导入时无条件 patch HTTP | `agent/generic/litellm_adapter.py:38` + `agent/generic/http_logger.py:172` |
| raw_http 应用层日志 | `_write_raw_log(log_type, data, seq=None)` 写 `{seq}_request.json`/`_response.json` | `agent/generic/litellm_adapter.py:80` |
| LLM interaction 可读日志 | `_write_interaction_log(log_entry: Dict)` 写 `llm_interaction_{date}.log` | `agent/generic/litellm_adapter.py:108` |
| 飞书 adapter stderr | `open(log_dir / "im_adapter_stderr.log", "a")` + `subprocess.Popen(stderr=...)` | `niu_api/channel/gateway.py:156` |
| http-log 服务端点 | FastAPI router 默认 include | `niu_api/__main__.py:527` + `niu_api/http_log_api.py:15` |
| Rust tracing | `tracing_subscriber::fmt().init()` 默认输出 stderr | `launcher/src/main.rs:1401-1405` |
| Rust println! | 2 处 `println!`（L1465、L1870） | `launcher/src/main.rs` |
| 配置承载文件 | `config/user-config.json`，由 `niu_api/config.py` 加载（单例），Rust 启动期也读 | `config/user-config.json` + `niu_api/config.py` + `launcher/src/main.rs:867` |

**关键约束**：
- `install_http_logger()` 在 `litellm_adapter.py:38` 模块导入时执行，flag gate 必须在 install 前位置，且 gate 要从已加载的配置读取——`get_logging_config()` 兜底返回 enabled=False 防止 config 未就绪时崩溃。
- `litellm_adapter.py:94/131` 用绝对路径 `Path(__file__).parent.parent.parent / "logs"`，`monkeypatch.chdir` 无效——必须抽 `_get_app_log_dir()` 函数让测试 monkeypatch。
- `install_http_logger` 重复调用会让 `original_post` 指向已 patch 版本形成递归——必须加模块级 `_patched` 幂等守卫。
- macOS .app bundle 的 `LSUIElement=true` 等价于当前 `main.rs:542-560` 的 objc `setActivationPolicy:1` hack——加 Info.plist 后可删 objc hack（但本轮先保留，避免引入新风险，下轮再清理）。
- Rust `windows_subsystem = "windows"` 只在 release build 生效（`not(debug_assertions)`），debug build 保留 console 方便调试。
- Rust 启动器读 `config/user-config.json` 用 `serde_json`，需要定义对应的 Rust struct（`LoggingConfig { enabled: bool, level: String }`）。

---

## File Structure

| 文件 | 职责 | 改动类型 |
|---|---|---|
| `niu_api/config.py` | `Config` 类增加 `logging` 字段，提供 `get_logging_config()` + `CONFIG_PATH` 常量 | Modify |
| `niu_api/__main__.py` | loguru sink + stdlib logging + uvicorn + http_log_router 按 flag gate | Modify |
| `agent/generic/http_logger.py` | `install_http_logger` flag gate + 幂等守卫 + `_write_log_entry` flag gate | Modify |
| `agent/generic/litellm_adapter.py` | 新增 `_get_app_log_dir()` + `_write_raw_log`/`_write_interaction_log` flag gate | Modify |
| `niu_api/channel/gateway.py` | 飞书 adapter stderr 按 flag gate（disabled 用 `subprocess.DEVNULL`） | Modify |
| `launcher/src/main.rs` | 顶部加 `windows_subsystem` 属性 + Rust tracing 按 flag gate | Modify |
| `launcher/Cargo.toml` | 无需改（`windows_subsystem` 在 main.rs 顶部声明） | - |
| `launcher/build.sh` | 编译后构造 macOS .app bundle（mkdir + cp + 写 Info.plist） | Modify |
| `tests/test_logging_config.py` | 新建：测试 Config 类 logging 字段解析 | Create |
| `tests/test_http_logger_flag.py` | 新建：测试 install_http_logger / _write_raw_log / _write_interaction_log flag gate | Create |
| `tests/test_http_log_router_conditional.py` | 新建：测试 http_log_router 条件 include | Create |
| `tests/test_gateway_stderr_flag.py` | 新建：测试飞书 adapter stderr 在 flag 关闭时用 DEVNULL | Create |
| `config/user-config.json` | 新增 `logging` 字段示例（缺省 enabled=false） | Modify |

---

### Task 1: Config 类增加 logging 字段 + CONFIG_PATH 常量

**Files:**
- Modify: `niu_api/config.py`
- Create: `tests/test_logging_config.py`
- Modify: `config/user-config.json`

**关键设计**：
- 现有 `niu_api/config.py` 的 `Config.load(config_path=None)` 默认路径硬编码 `os.path.join(os.path.dirname(__file__), "..", "config", "user-config.json")`，**没有 CONFIG_PATH 常量**，导致测试用 `monkeypatch.setattr(cfg_mod, "CONFIG_PATH", ...)` 无效。本 Task 必须先把默认路径抽成模块级 `CONFIG_PATH` 常量，并让 `Config.load()` 默认用它。
- `get_logging_config()` 必须 try/except 兜底返回 `LoggingConfig(enabled=False, level="INFO")`——因为 `agent/generic/litellm_adapter.py:38` 的 `install_http_logger()` 在模块导入时调用 `get_logging_config()`，此时若 config 加载异常不能让 Agent 模块 import 失败。

- [ ] **Step 1: 用 gitnexus 分析 Config 类改动 blast radius**

Run: 用 `mcp__gitnexus__impact` 工具，参数 `target="Config"`, `direction="upstream"`, `repo="niu-agent"`, `file_path="niu_api/config.py"`。
Expected: 报告 blast radius，HIGH/CRITICAL 告诉用户。

- [ ] **Step 2: 读 niu_api/config.py 现有 Config 类结构**

Run: `cat niu_api/config.py | head -120`
Expected: 看到 Config 类、`llm`/`lightrag_llm`/`context`/`storage`/`firstRun` 字段、`get_config()` 单例函数、`Config.load(config_path=None)` 默认路径硬编码

- [ ] **Step 3: 写失败测试 test_logging_config.py**

Create `tests/test_logging_config.py`:

```python
"""测试 config/user-config.json 的 logging 子节点解析。

缺省情况下（user-config.json 不含 logging 字段或 logging.enabled=false），
所有日志输出应关闭（loguru sink、raw_http 两层日志、llm_interaction 可读日志、
im_adapter_stderr、http-log 服务）。只有显式 logging.enabled=true 才按 level 输出。
"""
import json


def test_config_no_logging_field_defaults_to_disabled(tmp_path, monkeypatch):
    """user-config.json 不含 logging 字段时，logging 默认 enabled=False"""
    from niu_api import config as cfg_mod

    cfg_file = tmp_path / "user-config.json"
    cfg_file.write_text(json.dumps({"llm": {"apikey": "x"}}), encoding="utf-8")
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", str(cfg_file))
    cfg_mod._config = None  # 重置单例

    cfg = cfg_mod.get_config()
    assert cfg.logging.enabled is False
    assert cfg.logging.level == "INFO"  # 默认 INFO


def test_config_logging_enabled_true(tmp_path, monkeypatch):
    """user-config.json 含 logging.enabled=true 时，logging.enabled 为 True"""
    from niu_api import config as cfg_mod

    cfg_file = tmp_path / "user-config.json"
    cfg_file.write_text(json.dumps({
        "llm": {"apikey": "x"},
        "logging": {"enabled": True, "level": "DEBUG"},
    }), encoding="utf-8")
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", str(cfg_file))
    cfg_mod._config = None

    cfg = cfg_mod.get_config()
    assert cfg.logging.enabled is True
    assert cfg.logging.level == "DEBUG"


def test_config_logging_enabled_missing_field_defaults_false(tmp_path, monkeypatch):
    """logging 字段存在但 enabled 字段缺失时，enabled=False"""
    from niu_api import config as cfg_mod

    cfg_file = tmp_path / "user-config.json"
    cfg_file.write_text(json.dumps({
        "llm": {"apikey": "x"},
        "logging": {"level": "INFO"},
    }), encoding="utf-8")
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", str(cfg_file))
    cfg_mod._config = None

    cfg = cfg_mod.get_config()
    assert cfg.logging.enabled is False
    assert cfg.logging.level == "INFO"


def test_get_logging_config_returns_logging_object(tmp_path, monkeypatch):
    """get_logging_config() 返回 logging 子对象"""
    from niu_api import config as cfg_mod

    cfg_file = tmp_path / "user-config.json"
    cfg_file.write_text(json.dumps({
        "llm": {"apikey": "x"},
        "logging": {"enabled": True, "level": "WARNING"},
    }), encoding="utf-8")
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", str(cfg_file))
    cfg_mod._config = None

    log_cfg = cfg_mod.get_logging_config()
    assert log_cfg.enabled is True
    assert log_cfg.level == "WARNING"


def test_get_logging_config_fallback_on_exception(monkeypatch):
    """Config 加载异常时，get_logging_config() 兜底返回 enabled=False"""
    from niu_api import config as cfg_mod

    def _raise(*_, **__):
        raise RuntimeError("config load failed")

    monkeypatch.setattr(cfg_mod, "get_config", _raise)

    log_cfg = cfg_mod.get_logging_config()
    assert log_cfg.enabled is False
    assert log_cfg.level == "INFO"


def test_config_file_not_found_defaults_disabled(tmp_path, monkeypatch):
    """config 文件不存在时，Config.load 内部 catch FileNotFoundError 返回默认 Config（logging=False）"""
    from niu_api import config as cfg_mod

    nonexistent = tmp_path / "nonexistent-config.json"
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", str(nonexistent))
    cfg_mod._config = None

    cfg = cfg_mod.get_config()
    assert cfg.logging.enabled is False
    assert cfg.logging.level == "INFO"
```

- [ ] **Step 4: 运行测试验证失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_logging_config.py -v`
Expected: FAIL with AttributeError: 'Config' object has no attribute 'logging' 或 ModuleNotFoundError

- [ ] **Step 5: 修改 niu_api/config.py 增加 LoggingConfig 类、CONFIG_PATH 常量、Config.logging 字段、get_logging_config 兜底**

用 Read 工具读 `niu_api/config.py` 完整内容。

**改动点 1：增加 CONFIG_PATH 常量**

找到现有 `Config.load(config_path=None)` 中的默认路径硬编码：
```python
config_path = os.path.join(os.path.dirname(__file__), "..", "config", "user-config.json")
```

把它抽到模块级常量（在 Config 类定义之前）：
```python
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "user-config.json")
```

然后 `Config.load` 改为：
```python
@classmethod
def load(cls, config_path: Optional[str] = None) -> "Config":
    if config_path is None:
        config_path = CONFIG_PATH
    # 原有加载逻辑
    ...
```

**改动点 2：增加 LoggingConfig 类和 _parse_logging 函数**

在 Config 类之前增加（风格与现有 `LLMConfig` 一致——手写 `__init__`，不用 dataclass）：
```python
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
```

**改动点 3：Config 类增加 logging 字段**

在 `Config.__init__` 中增加（与现有 `self.llm` / `self.storage` / `self.first_run` 风格一致）：
```python
def __init__(self):
    self.llm: Optional[LLMConfig] = None
    self.storage: Dict[str, str] = {"documentRoot": "", "databasePath": ""}
    self.first_run: bool = True
    self.logging: LoggingConfig = LoggingConfig()
```

在 `Config.load` 中（解析 data 后）增加：
```python
cfg.logging = _parse_logging(data)
```

**改动点 4：增加 get_logging_config 函数（带兜底）**

在 `get_config()` 函数之后增加：
```python
def get_logging_config() -> LoggingConfig:
    """获取 logging 子配置。失败时兜底返回 enabled=False（保守默认）。

    agent/generic/litellm_adapter.py:38 的 install_http_logger() 在模块导入时
    调用本函数，此时若 config 加载异常不能让 Agent 模块 import 失败。
    """
    try:
        return get_config().logging
    except Exception:
        return LoggingConfig(enabled=False, level="INFO")
```

- [ ] **Step 6: 运行测试验证通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_logging_config.py -v`
Expected: PASS, 6 tests

- [ ] **Step 7: 同步更新 config/user-config.json 增加 logging 字段示例**

用 Read 工具读 `config/user-config.json`，然后用 Edit 在顶层 JSON 增加 `"logging"` 字段（缺省 enabled=false）。保留现有所有字段不动，只在末尾追加。注意 JSON 语法（前一个字段后加逗号）。

- [ ] **Step 8: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add niu_api/config.py tests/test_logging_config.py config/user-config.json
git commit -m "feat(config): add logging.enabled/level subnode + CONFIG_PATH constant

- 增加 LoggingConfig 类（enabled=False / level=INFO 默认）
- CONFIG_PATH 模块级常量（让测试 monkeypatch 生效）
- get_logging_config() 带 try/except 兜底（litellm_adapter 模块导入时调用）
- Config.load() 默认用 CONFIG_PATH 而非硬编码路径"
```

---

### Task 2: loguru + stdlib logging + uvicorn + http_log_router 按 flag gate

**Files:**
- Modify: `niu_api/__main__.py`（L35-40 loguru sink + L527 http_log_router include + L600-605 uvicorn.run）
- Create: `tests/test_http_log_router_conditional.py`

**关键设计**：
- loguru 用官方推荐的 `logger.disable("")` 全局禁用（不是 add dev/null sink）。enabled=true 时 `logger.enable("")` + `logger.add(sys.stderr, level=cfg.level)`。
- stdlib logging 用 `logging.disable(logging.CRITICAL)` 全局禁用——10+ 处散落的 `logging.getLogger(__name__)` 全部沉默。enabled=true 时 `logging.disable(logging.NOTSET)` 恢复。
- uvicorn 的 log_level 和 access_log 按 flag 动态切换。
- http_log_router 按 flag 条件 include。

- [ ] **Step 1: 用 gitnexus 分析 niu_api/__main__.py 的 app 改动 blast radius**

Run: 用 `mcp__gitnexus__impact` 工具，参数 `target="app"`, `direction="upstream"`, `repo="niu-agent"`, `file_path="niu_api/__main__.py"`。
Expected: 报告 blast radius。

- [ ] **Step 2: 读 niu_api/__main__.py 现有 loguru 配置、uvicorn.run、router include 代码**

Run: `sed -n '1,60p;520,540p;595,610p' niu_api/__main__.py`
Expected: 看到 loguru sink 块、http_log_router include 行、uvicorn.run 调用

- [ ] **Step 3: 写失败测试 test_http_log_router_conditional.py**

Create `tests/test_http_log_router_conditional.py`:

```python
"""测试 http_log_router 在 logging.enabled=false 时不被 include。

缺省情况下 /http-log/ 端点不应存在（避免暴露 LLM 请求日志）。
只有 logging.enabled=true 才挂载路由。

注意：未挂载 http_log_router 时，GET /http-log/ 无匹配路由，返回 404 Not Found。
断言用 != 200 表达"HTTP log viewer 不暴露"，能正确处理 404 情况。
"""
import json
from fastapi.testclient import TestClient


def _build_app_with_logging(tmp_path, monkeypatch, logging_enabled: bool):
    """构建一个临时 niu_api app，注入指定的 logging 配置"""
    from niu_api import config as cfg_mod

    cfg_file = tmp_path / "user-config.json"
    cfg_file.write_text(json.dumps({
        "llm": {"apikey": "x"},
        "logging": {"enabled": logging_enabled, "level": "INFO"},
    }), encoding="utf-8")
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", str(cfg_file))
    if hasattr(cfg_mod, "_config"):
        cfg_mod._config = None
    cfg_mod.get_config()  # 预热单例（防 get_logging_config 兜底吞异常导致 false pass）

    import importlib
    import niu_api.__main__ as main_mod
    importlib.reload(main_mod)
    return main_mod.app


def test_http_log_router_not_included_when_logging_disabled(tmp_path, monkeypatch):
    """logging.enabled=false 时，/http-log/ 端点不暴露（返回 404 非 200）"""
    app = _build_app_with_logging(tmp_path, monkeypatch, logging_enabled=False)
    client = TestClient(app)
    resp = client.get("/http-log/")
    assert resp.status_code != 200, f"logging.enabled=false 时 /http-log/ 不应暴露，但返回 {resp.status_code}"


def test_http_log_router_included_when_logging_enabled(tmp_path, monkeypatch):
    """logging.enabled=true 时，/http-log/ 端点返回 200"""
    app = _build_app_with_logging(tmp_path, monkeypatch, logging_enabled=True)
    client = TestClient(app)
    resp = client.get("/http-log/")
    assert resp.status_code == 200, f"logging.enabled=true 时 /http-log/ 应返回 200，但返回 {resp.status_code}"
```

- [ ] **Step 4: 运行测试验证失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_http_log_router_conditional.py -v`
Expected: FAIL（现状是无条件 include）

- [ ] **Step 5: 修改 niu_api/__main__.py L35-40 loguru sink + stdlib logging gate**

用 Read 工具读 `niu_api/__main__.py:1-60`。

原代码（L35-40 附近，需以读到的实际为准）：
```python
logger.remove()
logger.add(sys.stderr, format=..., level="INFO")
```

改为：
```python
import logging as _stdlib_logging
from niu_api.config import get_logging_config

_logging_cfg = get_logging_config()
logger.remove()
if _logging_cfg.enabled:
    logger.enable("")  # 恢复 loguru 全局启用
    logger.add(sys.stderr, format=..., level=_logging_cfg.level)
    _stdlib_logging.disable(_stdlib_logging.NOTSET)  # 恢复 stdlib logging
    logger.info(f"Logging enabled at level {_logging_cfg.level}")
else:
    logger.disable("")  # loguru 官方推荐全局禁用方式（不 add dev/null sink）
    _stdlib_logging.disable(_stdlib_logging.CRITICAL)  # 禁用 10+ 处散落的 stdlib logger
```

注意：`format=...` 保留原 format 字符串不要改。

- [ ] **Step 6: 修改 niu_api/__main__.py L527 http_log_router 条件 include**

用 Read 工具读 `niu_api/__main__.py:520-540`，找到 `app.include_router(http_log_router)` 行。

改为：
```python
if get_logging_config().enabled:
    app.include_router(http_log_router)
    logger.info("HTTP log viewer service enabled at /http-log/")
else:
    logger.info("HTTP log viewer service disabled (logging.enabled=false)")
```

- [ ] **Step 7: 修改 niu_api/__main__.py L600-605 uvicorn.run 参数按 flag 动态切换**

用 Read 工具读 `niu_api/__main__.py:595-610`，找到 `uvicorn.run(...)` 调用。

原代码：
```python
uvicorn.run(app, host=..., port=..., log_level="warning")
```

改为（保留原有 host/port/reload 等参数不变）：
```python
_uv_log_level = "warning" if get_logging_config().enabled else "critical"
_uv_access_log = get_logging_config().enabled
uvicorn.run(
    app,
    host=...,
    port=...,
    log_level=_uv_log_level,
    access_log=_uv_access_log,
)
```

- [ ] **Step 8: 运行测试验证通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_http_log_router_conditional.py -v`
Expected: PASS, 2 tests

- [ ] **Step 9: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add niu_api/__main__.py tests/test_http_log_router_conditional.py
git commit -m "feat(api): gate loguru + stdlib logging + uvicorn + http-log router behind logging.enabled

缺省 logging.enabled=false：
- loguru 用 logger.disable('') 全局禁用（不 add dev/null sink）
- stdlib logging.disable(CRITICAL) 禁用 10+ 处散落 logger
- uvicorn log_level='critical' + access_log=False
- /http-log/ 端点不挂载（非 200）
enabled=true 时按 level 字段恢复输出并挂载。"
```

---

### Task 3: raw_http transport 层日志按 flag gate + 幂等守卫

**Files:**
- Modify: `agent/generic/http_logger.py`（`install_http_logger`、新增 `_do_patch_http`、`_write_log_entry`、新增模块级 `_patched` 标志）
- Create: `tests/test_http_logger_flag.py`

**关键设计**：
- **现有代码摸底**（必读）：
  - `http_logger.py:27-32` 的 `_get_log_dir()` **已存在**，返回 `Path("logs") / "raw_http" / date_str` 并 `mkdir(parents=True, exist_ok=True)`——**不要新增此函数，也不要改它的签名或路径计算**。它已经是模块级函数，测试可以直接 `monkeypatch.setattr(hl_mod, "_get_log_dir", lambda: fake_dir)` 拦截。
  - `http_logger.py:91-99` 的 `_write_log_entry(seq: int, entry: dict)` **签名是先 seq 再 entry**，函数体调 `_get_log_dir()` 拿目录后写文件——**只加 flag gate，不改签名不改路径逻辑**。
  - `http_logger.py:172` 附近的 `install_http_logger()` 是模块导入时被 `litellm_adapter.py:38` 调用的入口函数。
- 把 `install_http_logger()` 函数体内的 patch 逻辑抽到新函数 `_do_patch_http()`，`install_http_logger()` 加 flag gate + 幂等守卫。
- **幂等守卫放在 `install_http_logger()` 入口**（flag gate 之后、调 `_do_patch_http()` 之前），而不是放在 `_do_patch_http()` 内部。这样测试可以 mock `_do_patch_http` 同时仍能验证 `install_http_logger` 的幂等行为。
- 测试用 `monkeypatch.setattr` 替换 `_get_log_dir` 函数（已有的可直接 patch），不用 `monkeypatch.chdir`。

- [ ] **Step 1: 用 gitnexus 分析 install_http_logger 改动 blast radius**

Run: 用 `mcp__gitnexus__impact` 工具，参数 `target="install_http_logger"`, `direction="upstream"`, `repo="niu-agent"`, `file_path="agent/generic/http_logger.py"`。

- [ ] **Step 2: 读 agent/generic/http_logger.py 现有 install_http_logger / _get_log_dir / _write_log_entry 实现**

Run: `sed -n '25,35p;88,100p;160,210p' agent/generic/http_logger.py`
Expected: 看到 `_get_log_dir()` 已存在（L27-32）、`_write_log_entry(seq, entry)` 已存在（L91-99）、`install_http_logger()` 函数定义和 patch 逻辑（L172+）

- [ ] **Step 3: 写失败测试 test_http_logger_flag.py**

Create `tests/test_http_logger_flag.py`:

```python
"""测试 raw_http transport 层日志在 logging.enabled=false 时不写文件。

agent/generic/http_logger.py 的 install_http_logger() 现状是模块导入时无条件 patch
HTTP client。整改后：install_http_logger() 入口检查 logging.enabled，false 时不 patch。
install_http_logger() 加 _patched 幂等守卫（在 flag gate 之后、_do_patch_http 之前），
重复调用只 patch 一次（防 original_post 指向已 patch 版本形成递归）。
"""
import json
from unittest import mock


def _setup_config(tmp_path, monkeypatch, logging_enabled: bool):
    """在 tmp_path 下生成临时 user-config.json 并 monkeypatch CONFIG_PATH"""
    from niu_api import config as cfg_mod

    cfg_file = tmp_path / "user-config.json"
    cfg_file.write_text(json.dumps({
        "llm": {"apikey": "x"},
        "logging": {"enabled": logging_enabled, "level": "INFO"},
    }), encoding="utf-8")
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", str(cfg_file))
    if hasattr(cfg_mod, "_config"):
        cfg_mod._config = None
    cfg_mod.get_config()  # 预热单例


def test_install_http_logger_skips_when_logging_disabled(tmp_path, monkeypatch):
    """logging.enabled=false 时，install_http_logger 不 patch HTTP client"""
    _setup_config(tmp_path, monkeypatch, logging_enabled=False)

    import agent.generic.http_logger as hl_mod
    hl_mod._patched = False  # 重置幂等标志

    patched_called = mock.MagicMock()
    # raising=False：Step 3 写测试时 _do_patch_http 还不存在（Step 5 才实现），
    # 默认 raising=True 会 AttributeError 让测试错误信息不清晰
    monkeypatch.setattr(hl_mod, "_do_patch_http", patched_called, raising=False)

    hl_mod.install_http_logger()
    assert patched_called.called is False, "logging.enabled=false 时不应该 patch HTTP"


def test_install_http_logger_patches_when_logging_enabled(tmp_path, monkeypatch):
    """logging.enabled=true 时，install_http_logger 正常 patch HTTP client"""
    _setup_config(tmp_path, monkeypatch, logging_enabled=True)

    import agent.generic.http_logger as hl_mod
    hl_mod._patched = False

    patched_called = mock.MagicMock()
    monkeypatch.setattr(hl_mod, "_do_patch_http", patched_called, raising=False)

    hl_mod.install_http_logger()
    assert patched_called.called is True, "logging.enabled=true 时应该 patch HTTP"


def test_install_http_logger_idempotent(tmp_path, monkeypatch):
    """重复调 install_http_logger 不应多次 patch（防递归）。

    幂等守卫放在 install_http_logger 入口（flag gate 之后），所以 mock _do_patch_http
    后仍能验证守卫——_patched=True 时 install_http_logger 直接 return 不调 _do_patch_http。
    """
    _setup_config(tmp_path, monkeypatch, logging_enabled=True)

    import agent.generic.http_logger as hl_mod
    hl_mod._patched = False

    call_count = mock.MagicMock()
    monkeypatch.setattr(hl_mod, "_do_patch_http", call_count, raising=False)

    hl_mod.install_http_logger()
    hl_mod.install_http_logger()  # 第二次
    hl_mod.install_http_logger()  # 第三次
    assert call_count.call_count == 1, f"幂等守卫失败，_do_patch_http 被调了 {call_count.call_count} 次"


def test_write_log_entry_skipped_when_logging_disabled(tmp_path, monkeypatch):
    """logging.enabled=false 时，_write_log_entry 不写文件"""
    _setup_config(tmp_path, monkeypatch, logging_enabled=False)

    import agent.generic.http_logger as hl_mod

    # _get_log_dir 是 http_logger.py 现有模块级函数（L27-32），直接 monkeypatch 替换
    fake_dir = tmp_path / "fake_logs"
    monkeypatch.setattr(hl_mod, "_get_log_dir", lambda: fake_dir)

    # _write_log_entry 真实签名是 (seq: int, entry: dict)
    hl_mod._write_log_entry(1, {"test": "data"})
    assert not fake_dir.exists(), "logging.enabled=false 时不应写 raw_http 日志文件"
```

- [ ] **Step 4: 运行测试验证失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_http_logger_flag.py -v`
Expected: FAIL（现状是无条件 patch，无幂等守卫，无 flag gate，无 _do_patch_http 函数）

- [ ] **Step 5: 重构 http_logger.py**

用 Read 工具读 `agent/generic/http_logger.py` 完整内容。

**改动点 1：增加模块级 _patched 标志**

在 `_get_log_dir` 函数之前（约 L25）增加：
```python
_patched = False  # 幂等守卫：防止 install_http_logger 被重复调用导致递归 patch
```

**注意**：`_get_log_dir()` 函数（L27-32）**不动**。

**改动点 2：抽 _do_patch_http 函数，把原 install_http_logger 的 patch 逻辑搬过来**

找到现有 `install_http_logger()` 函数体（L172 附近），把里面的 patch 逻辑抽到一个新函数 `_do_patch_http()`：

```python
def _do_patch_http() -> None:
    """实际 patch HTTP client 的逻辑（原 install_http_logger 函数体）

    幂等守卫在 install_http_logger 入口（不在本函数内），本函数只负责执行 patch。
    """
    # 原有 patch 代码搬过来：
    # original_post = HTTPHandler.post
    # def patched_post(...): ...
    # HTTPHandler.post = patched_post
    # 等
    ...
```

**改动点 3：install_http_logger 加 flag gate + 幂等守卫**

```python
def install_http_logger() -> None:
    """Install HTTP client patches to capture raw HTTP traffic.

    缺省 logging.enabled=false 时不 patch（不写 transport 层日志）。
    幂等：_patched=True 时直接 return，避免重复 patch 导致 original_post 指向
    已被 patch 的版本形成无限递归。

    幂等守卫放在本函数入口（flag gate 之后、_do_patch_http 之前），这样测试可以
    mock _do_patch_http 同时仍能验证本函数的幂等行为。
    """
    global _patched
    from niu_api.config import get_logging_config
    if not get_logging_config().enabled:
        return  # flag 关闭，不 patch
    if _patched:
        return  # 幂等守卫：已 patch 过直接 return
    _patched = True
    _do_patch_http()
```

**改动点 4：_write_log_entry 加 flag gate（签名不变，路径逻辑不变）**

```python
def _write_log_entry(seq: int, entry: dict) -> None:
    """写入 JSON 日志文件。"""
    from niu_api.config import get_logging_config
    if not get_logging_config().enabled:
        return  # 静默跳过
    log_dir = _get_log_dir()  # 不动，仍调现有 _get_log_dir
    filepath = log_dir / f"{seq:06d}.json"
    with _write_lock:
        filepath.write_text(
            _json.dumps(entry, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
```

- [ ] **Step 6: 运行测试验证通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_http_logger_flag.py -v`
Expected: PASS, 4 tests

- [ ] **Step 7: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add agent/generic/http_logger.py tests/test_http_logger_flag.py
git commit -m "feat(agent): gate raw_http transport logger behind logging.enabled

- install_http_logger() 入口加 flag gate + _patched 幂等守卫（防重复 patch 递归）
- _do_patch_http() 是原 patch 逻辑抽出（守卫在外层，便于测试 mock）
- _write_log_entry() 在 flag 关闭时静默跳过（签名/路径逻辑不变）
- _get_log_dir() 已有函数不动，测试直接 monkeypatch.setattr 拦截"
```

---

### Task 4: raw_http 应用层日志和 LLM interaction 日志按 flag gate

**Files:**
- Modify: `agent/generic/litellm_adapter.py`（新增 `_get_app_log_dir`、`_write_raw_log` 加 flag gate、`_write_interaction_log` 加 flag gate）
- Modify: `tests/test_http_logger_flag.py`（追加 2 个应用层日志测试）

**关键设计**：
- **现有代码摸底**（必读）：
  - `litellm_adapter.py:80` 的 `_write_raw_log(log_type: str, data: dict, seq: Optional[int] = None)` 签名是 `log_type`（不是 `kind`），L94 路径硬编码 `Path(__file__).parent.parent.parent / "logs" / "raw_http" / datetime.now().strftime("%Y%m%d")`。
  - `litellm_adapter.py:108` 的 `_write_interaction_log(log_entry: Dict[str, Any])` 签名是单个 dict，L131 路径硬编码 `Path(__file__).parent.parent.parent / "logs"`。
- **新增 `_get_app_log_dir()` 函数**（litellm_adapter 没有，要新增），返回 `Path(__file__).parent.parent.parent / "logs"`——把 L94 和 L131 共同的路径计算抽出来，便于测试 monkeypatch。
- `_write_raw_log` 和 `_write_interaction_log` **签名不变**，只在入口加 flag gate + 用 `_get_app_log_dir()` 替换硬编码路径。
- 测试用 `monkeypatch.setattr` 替换 `_get_app_log_dir` 返回 tmp_path，不用 `monkeypatch.chdir`。

- [ ] **Step 1: 用 gitnexus 分析 _write_raw_log / _write_interaction_log 改动 blast radius**

Run: 用 `mcp__gitnexus__impact` 工具，参数 `target="_write_raw_log"`, `direction="upstream"`, `repo="niu-agent"`, `file_path="agent/generic/litellm_adapter.py"`。再对 `_write_interaction_log` 做一次。

- [ ] **Step 2: 读 litellm_adapter.py 现有 _write_raw_log / _write_interaction_log 实现**

Run: `sed -n '78,145p;390,400p;615,625p' agent/generic/litellm_adapter.py`
Expected: 看到 `_write_raw_log(log_type, data, seq=None)` 函数体、`_write_interaction_log(log_entry)` 函数体、调用点

- [ ] **Step 3: 追加失败测试到 test_http_logger_flag.py**

在 `tests/test_http_logger_flag.py` 末尾追加（接 Task 3 的 4 个测试之后）：

```python
def test_write_raw_log_skipped_when_logging_disabled(tmp_path, monkeypatch):
    """logging.enabled=false 时，_write_raw_log 不写 {seq}_request.json / _response.json"""
    _setup_config(tmp_path, monkeypatch, logging_enabled=False)

    import agent.generic.litellm_adapter as la_mod

    # monkeypatch 新增的 _get_app_log_dir 函数（litellm_adapter 用绝对路径，chdir 无效）
    fake_dir = tmp_path / "fake_app_logs"
    monkeypatch.setattr(la_mod, "_get_app_log_dir", lambda: fake_dir)

    # _write_raw_log 真实签名是 (log_type, data, seq=None)
    la_mod._write_raw_log("request", {"test": "data"}, seq=1)
    la_mod._write_raw_log("response", {"test": "data"}, seq=1)

    if fake_dir.exists():
        files = list(fake_dir.rglob("*.json"))
        assert files == [], f"logging.enabled=false 时不应写应用层 raw_http 日志，但找到 {files}"


def test_write_interaction_log_skipped_when_logging_disabled(tmp_path, monkeypatch):
    """logging.enabled=false 时，_write_interaction_log 不写 llm_interaction_*.log"""
    _setup_config(tmp_path, monkeypatch, logging_enabled=False)

    import agent.generic.litellm_adapter as la_mod

    fake_dir = tmp_path / "fake_app_logs"
    monkeypatch.setattr(la_mod, "_get_app_log_dir", lambda: fake_dir)

    # _write_interaction_log 真实签名是 (log_entry: Dict)
    la_mod._write_interaction_log({
        "user_input": "test input",
        "assistant_output": "test output",
        "model": "test-model",
    })

    if fake_dir.exists():
        interaction_files = list(fake_dir.glob("llm_interaction_*.log"))
        assert interaction_files == [], f"logging.enabled=false 时不应写 interaction 日志，但找到 {interaction_files}"
```

- [ ] **Step 4: 运行测试验证失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_http_logger_flag.py -v`
Expected: 新增 2 个测试 FAIL

- [ ] **Step 5: 给 litellm_adapter.py 增加 _get_app_log_dir 函数 + _write_raw_log / _write_interaction_log 加 flag gate**

用 Read 工具读 `agent/generic/litellm_adapter.py:78-145`。

**改动点 1：增加模块级 _get_app_log_dir 函数**

在 `_write_raw_log` 函数之前（约 L78）增加：
```python
def _get_app_log_dir() -> Path:
    """获取应用层日志根目录，便于测试 monkeypatch。

    litellm_adapter 原用 Path(__file__).parent.parent.parent / "logs" 绝对路径
    （见 _write_raw_log 和 _write_interaction_log），chdir 无效。
    抽出此函数让测试可拦截。带 resolve() 与 gateway.py 的 _get_gateway_log_dir 保持一致。
    """
    return Path(__file__).resolve().parent.parent.parent / "logs"
```

**改动点 2：_write_raw_log 函数入口加 flag gate + 用 _get_app_log_dir**

原路径行 `log_dir = Path(__file__).parent.parent.parent / "logs" / "raw_http" / datetime.now().strftime("%Y%m%d")` 改为：
```python
def _write_raw_log(log_type: str, data: dict, seq: Optional[int] = None) -> None:
    """...（保留原 docstring）"""
    from niu_api.config import get_logging_config
    if not get_logging_config().enabled:
        return  # 静默跳过
    log_dir = _get_app_log_dir() / "raw_http" / datetime.now().strftime("%Y%m%d")
    # 原有后续写入逻辑（mkdir、写文件等）保留不变
    ...
```

**改动点 3：_write_interaction_log 函数入口加 flag gate + 用 _get_app_log_dir**

原路径行 `log_dir = Path(__file__).parent.parent.parent / "logs"` 改为：
```python
def _write_interaction_log(log_entry: Dict[str, Any]):
    """...（保留原 docstring）"""
    from niu_api.config import get_logging_config
    if not get_logging_config().enabled:
        return  # 静默跳过
    log_dir = _get_app_log_dir()
    # 原有后续写入逻辑（mkdir、写 llm_interaction_{date}.log 等）保留不变
    ...
```

- [ ] **Step 6: 运行测试验证通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_http_logger_flag.py -v`
Expected: PASS, 6 tests（4 旧 + 2 新）

- [ ] **Step 7: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add agent/generic/litellm_adapter.py tests/test_http_logger_flag.py
git commit -m "feat(agent): gate raw_http app-layer and interaction logs behind logging.enabled

- 新增 _get_app_log_dir() 抽出绝对路径计算（litellm_adapter 用 __file__ 相对路径，chdir 无效）
- _write_raw_log(log_type, data, seq) 入口加 flag gate（签名/路径逻辑不变）
- _write_interaction_log(log_entry) 入口加 flag gate（签名/路径逻辑不变）
- 避免每次 LLM 调用写 3 个文件（transport 层 + 应用层 request + 应用层 response）"
```

---

### Task 5: 飞书 adapter stderr 按 flag gate（保留启动失败诊断）

**Files:**
- Modify: `niu_api/channel/gateway.py:156`（stderr 重定向 + 启动失败诊断独立文件）

**关键设计**：
- 现状：`gateway.py:156` `adapter_stderr = open(log_dir / "im_adapter_stderr.log", "a")` + `subprocess.Popen(argv, stderr=adapter_stderr)`——飞书 adapter 子进程的 stderr 写入文件。
- 改法：logging.enabled=false 时用 `subprocess.DEVNULL` 代替文件，enabled=true 时保留原文件重定向。
- **关键修正（审查 Critical 5）**：`_launch_adapter` 有多处 `logger.error`（L131/L144/L163）——这些用 loguru 的 logger，被 Task 2 的 `logger.disable("")` gate 后 enabled=false 时丢失。但飞书 adapter 启动失败是**关键诊断**（app_id 配错、端口占用、credentials 缺失），用户必须能看到。
- **修法**：增加一个独立函数 `_log_gateway_error(msg)` 写 `logs/gateway_error.log`，不受 flag 控制。在 `_launch_adapter` 的 3 处 `logger.error` 旁边同时调用 `_log_gateway_error`，确保 enabled=false 时仍有诊断。
- 测试用 `monkeypatch.setattr` 拦截 `subprocess.Popen` 验证 stderr 参数 + 验证 gateway_error.log 在启动失败时被写。

- [ ] **Step 1: 用 gitnexus 分析 gateway.py 改动 blast radius**

Run: 用 `mcp__gitnexus__impact` 工具，参数 `target="ImageMessageGateway"` 或 `_launch_adapter`, `direction="upstream"`, `repo="niu-agent"`, `file_path="niu_api/channel/gateway.py"`。

- [ ] **Step 2: 读 gateway.py 现有 stderr 重定向 + _launch_adapter 实现**

Run: `sed -n '115,170p' niu_api/channel/gateway.py`
Expected: 看到 `_launch_adapter` 方法、L156 的 `adapter_stderr = open(...)` + `subprocess.Popen(...)`、L131/L144/L163 的 `logger.error` 调用

- [ ] **Step 3: 写失败测试 test_gateway_stderr_flag.py**

Create `tests/test_gateway_stderr_flag.py`:

```python
"""测试飞书 adapter 子进程 stderr 在 logging.enabled=false 时用 DEVNULL。

现状：niu_api/channel/gateway.py:156 open(log_dir / "im_adapter_stderr.log", "a")
作为子进程 stderr，导致每次启动都写日志文件。
整改后：logging.enabled=false 时用 subprocess.DEVNULL，enabled=true 时保留文件重定向。

关键修正（审查 Critical 5）：_launch_adapter 的 logger.error（L131/L144/L163）被
Task 2 的 logger.disable('') gate 后 enabled=false 时丢失。增加 _log_gateway_error()
独立写 logs/gateway_error.log，不受 flag 控制，确保启动失败仍可诊断。
"""
import json
import subprocess
from pathlib import Path
from unittest import mock


def _setup_config(tmp_path, monkeypatch, logging_enabled: bool):
    """在 tmp_path 下生成临时 user-config.json 并 monkeypatch CONFIG_PATH"""
    from niu_api import config as cfg_mod

    cfg_file = tmp_path / "user-config.json"
    cfg_file.write_text(json.dumps({
        "llm": {"apikey": "x"},
        "logging": {"enabled": logging_enabled, "level": "INFO"},
    }), encoding="utf-8")
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", str(cfg_file))
    if hasattr(cfg_mod, "_config"):
        cfg_mod._config = None
    cfg_mod.get_config()


def _make_gateway(tmp_path, monkeypatch):
    """构造一个 IMGateway 实例用于测试（mock 掉 preferences.json + adapter_workdir）

    _launch_adapter 实际逻辑（gateway.py:115-160）：
    - L118 读 Path.home() / ".niu" / "preferences.json"，不存在直接 return
    - L122-126 读 im.adapter，为空直接 return
    - L129-132 检查 adapter_workdir 是否存在，不存在 return
    - L143-145 检查 app_id/app_secret，为空 return
    所以为让 Popen 真被调，必须 mock Path.home() 返回 tmp_path，
    并在 tmp_path/.niu/preferences.json 写入飞书配置。
    """
    import niu_api.channel.gateway as gw_mod
    from unittest.mock import MagicMock

    # mock Path.home() 返回 tmp_path（让 preferences.json 读到 tmp_path/.niu/）
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    # 在 tmp_path/.niu/preferences.json 写入飞书 adapter 配置
    niu_dir = tmp_path / ".niu"
    niu_dir.mkdir(parents=True, exist_ok=True)
    prefs = {
        "im": {"adapter": "feishu"},
        "feishu": {"app_id": "test_app_id", "app_secret": "test_app_secret"},
    }
    (niu_dir / "preferences.json").write_text(json.dumps(prefs), encoding="utf-8")

    # mock adapter_workdir.exists() 返回 True（避免真路径检查失败）
    # gateway.py:129 adapter_workdir = Path(__file__).resolve().parent.parent.parent / "im-adapters" / adapter_type / "src"
    # 用 monkeypatch 让 Path.exists 对该路径返回 True
    original_exists = __import__("pathlib").Path.exists

    def mock_exists(self):
        if "im-adapters" in str(self):
            return True
        return original_exists(self)

    monkeypatch.setattr("pathlib.Path.exists", mock_exists)

    # IMGateway.__init__(self, channel_router, port: int = 19877)
    channel_router = MagicMock()
    gateway = gw_mod.IMGateway(channel_router=channel_router, port=0)
    return gateway


def test_gateway_stderr_devnull_when_logging_disabled(tmp_path, monkeypatch):
    """logging.enabled=false 时，飞书 adapter 子进程 stderr 用 DEVNULL"""
    _setup_config(tmp_path, monkeypatch, logging_enabled=False)

    captured_popen = mock.MagicMock()
    monkeypatch.setattr(subprocess, "Popen", captured_popen)

    gateway = _make_gateway(tmp_path, monkeypatch)
    # 调 _launch_adapter（可能需要 mock adapter_workdir 等让流程走到 Popen）
    try:
        gateway._launch_adapter()
    except Exception:
        pass  # 测试只关心 Popen 被调时的 stderr 参数

    if captured_popen.called:
        _, kwargs = captured_popen.call_args
        assert kwargs.get("stderr") == subprocess.DEVNULL, \
            f"logging.enabled=false 时 stderr 应该是 DEVNULL，但传了 {kwargs.get('stderr')}"


def test_gateway_stderr_file_when_logging_enabled(tmp_path, monkeypatch):
    """logging.enabled=true 时，飞书 adapter 子进程 stderr 重定向到文件"""
    _setup_config(tmp_path, monkeypatch, logging_enabled=True)

    captured_popen = mock.MagicMock()
    monkeypatch.setattr(subprocess, "Popen", captured_popen)

    gateway = _make_gateway(tmp_path, monkeypatch)
    try:
        gateway._launch_adapter()
    except Exception:
        pass

    if captured_popen.called:
        _, kwargs = captured_popen.call_args
        assert kwargs.get("stderr") != subprocess.DEVNULL, \
            "logging.enabled=true 时 stderr 应该重定向到文件，不应是 DEVNULL"


def test_gateway_error_logged_to_file_on_launch_failure(tmp_path, monkeypatch):
    """飞书 adapter 启动失败时，_log_gateway_error 写 logs/gateway_error.log（不受 flag 控制）"""
    _setup_config(tmp_path, monkeypatch, logging_enabled=False)  # flag 关闭

    # monkeypatch _get_app_log_dir 让 gateway_error.log 写到 tmp_path
    import niu_api.channel.gateway as gw_mod
    fake_log_dir = tmp_path / "fake_logs"
    monkeypatch.setattr(gw_mod, "_get_gateway_log_dir", lambda: fake_log_dir, raising=False)

    # 直接调 _log_gateway_error
    gw_mod._log_gateway_error("test error message")

    error_log = fake_log_dir / "gateway_error.log"
    assert error_log.exists(), "gateway_error.log 应该被创建"
    content = error_log.read_text(encoding="utf-8")
    assert "test error message" in content, f"错误消息应写入文件，但内容是：{content}"
```

注意：implementer 需要根据 gateway.py 实际代码结构调整 `_make_gateway` 的参数和 `_launch_adapter` 的调用方式。`_get_gateway_log_dir` 是新增函数（见 Step 5），测试用 raising=False 因为 Step 3 写测试时该函数还不存在。

- [ ] **Step 4: 运行测试验证失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_gateway_stderr_flag.py -v`
Expected: FAIL（现状是无条件文件重定向，无 _log_gateway_error 函数）

- [ ] **Step 5: 修改 gateway.py：增加 _log_gateway_error + _get_gateway_log_dir + stderr 按 flag gate**

用 Read 工具读 `niu_api/channel/gateway.py:1-50`（imports 和模块顶部）+ `115-170`（_launch_adapter）。

**改动点 1：增加 _get_gateway_log_dir 和 _log_gateway_error 函数（模块级）**

在 gateway.py 模块顶部增加（类似 litellm_adapter 的 _get_app_log_dir 模式）：
```python
def _get_gateway_log_dir() -> Path:
    """获取 gateway 日志根目录，便于测试 monkeypatch。"""
    return Path(__file__).resolve().parent.parent.parent / "logs"


def _log_gateway_error(msg: str) -> None:
    """记录 gateway 致命错误到 logs/gateway_error.log，不受 logging flag 控制。

    飞书 adapter 启动失败是关键诊断（app_id 配错、端口占用、credentials 缺失），
    即使 logging.enabled=false 也必须写，确保用户能诊断。
    """
    try:
        log_dir = _get_gateway_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "gateway_error.log"
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] ERROR: {msg}\n")
    except Exception:
        pass  # 日志写入失败不影响主流程
```

**改动点 2：_launch_adapter 的 stderr 按 flag gate**

原 L156 附近：
```python
adapter_stderr = open(log_dir / "im_adapter_stderr.log", "a")
# ... subprocess.Popen(argv, stderr=adapter_stderr)
```

改为：
```python
from niu_api.config import get_logging_config
if get_logging_config().enabled:
    adapter_stderr = open(log_dir / "im_adapter_stderr.log", "a")
else:
    adapter_stderr = subprocess.DEVNULL  # logging 关闭时不写文件
# ... subprocess.Popen(argv, stderr=adapter_stderr) 保留不动
```

**改动点 3：在 3 处 logger.error 旁边调用 _log_gateway_error**

找到 L131、L144、L163 的 `logger.error(...)`，每处旁边增加 `_log_gateway_error(...)`：

L131 附近：
```python
logger.error(f"[IMGateway] Adapter not found: {adapter_workdir}")
_log_gateway_error(f"Adapter not found: {adapter_workdir}")
```

L144 附近：
```python
logger.error(f"[IMGateway] {adapter_type} credentials missing, skipping")
_log_gateway_error(f"{adapter_type} credentials missing, skipping")
```

L163 附近：
```python
logger.error(f"[IMGateway] Launch failed: {e}")
_log_gateway_error(f"Launch failed: {e}")
```

- [ ] **Step 6: 运行测试验证通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_gateway_stderr_flag.py -v`
Expected: PASS, 3 tests

- [ ] **Step 7: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add niu_api/channel/gateway.py tests/test_gateway_stderr_flag.py
git commit -m "feat(channel): gate feishu adapter stderr + preserve launch failure diagnostics

- gateway.py 启动飞书 adapter 子进程时，logging.enabled=false 用 subprocess.DEVNULL
  代替文件重定向，避免写 logs/im_adapter_stderr.log
- 新增 _log_gateway_error() 写 logs/gateway_error.log，不受 flag 控制
- _launch_adapter 的 3 处 logger.error 旁边同时调 _log_gateway_error，
  确保 enabled=false 时用户仍能诊断飞书 adapter 启动失败
- 新增 _get_gateway_log_dir() 抽出路径计算，便于测试 monkeypatch"
```

---

### Task 6 + Task 7: Windows GUI subsystem + Rust tracing gate（必须一起 commit）

**关键约束**：Task 6 和 Task 7 **必须一起 commit**——Task 6 的 `windows_subsystem=windows` 会让 Windows release 模式下 println!/tracing 输出丢失，Task 7 才处理 tracing gate + 致命错误独立日志。如果 Task 6 先 commit 但 Task 7 未完成，中间状态 Windows GUI 模式下**诊断完全盲区**。implementer 可以先做 Task 6 再做 Task 7，但**两个 Task 的代码改动必须放在同一个 commit**（或 Task 6 改动暂不 commit，等 Task 7 完成后一起 commit）。

---

### Task 6: Windows GUI subsystem（关闭 niu.exe 控制台窗口）

**Files:**
- Modify: `launcher/src/main.rs`（顶部加 `windows_subsystem` 属性）

**关键设计**：
- Windows 上 Rust 二进制默认是 console 程序，双击 niu.exe 会弹 cmd 窗口。
- 加 `#![cfg_attr(all(target_os = "windows", not(debug_assertions)), windows_subsystem = "windows")]` 到 main.rs 顶部——release build 且 Windows 下生效，debug build 保留 console 方便调试。
- 这个属性只在 Windows 编译时生效，macOS/Linux 不受影响。
- 副作用：Windows release 模式下 `println!`/`tracing` 输出到 stderr 会被丢（GUI 程序无 console）。Task 7 会处理 Rust tracing gate。

- [ ] **Step 1: 用 gitnexus 分析 main.rs 改动 blast radius**

Run: 用 `mcp__gitnexus__impact` 工具，参数 `target="main"`, `direction="upstream"`, `repo="niu-agent"`, `file_path="launcher/src/main.rs"`。

- [ ] **Step 2: 读 launcher/src/main.rs 顶部和 Cargo.toml**

Run: `head -5 launcher/src/main.rs && echo "---" && head -20 launcher/Cargo.toml`
Expected: main.rs 第一行是 `use std::env;`，Cargo.toml 无 `[[bin]]` 段

- [ ] **Step 3: 修改 launcher/src/main.rs 顶部加 windows_subsystem 属性**

用 Read 工具读 `launcher/src/main.rs:1-5`。

原代码（L1）：
```rust
use std::env;
```

改为（在 L1 之前加属性）：
```rust
//! Niu Launcher - Rust 启动器
//!
//! Windows release build 下编译为 GUI 子系统（不弹 cmd 窗口）。
//! debug build 保留 console 方便调试。macOS/Linux 不受影响。

#![cfg_attr(all(target_os = "windows", not(debug_assertions)), windows_subsystem = "windows")]

use std::env;
```

注意：`#![...]` 是 inner attribute，必须在文件顶部、任何 use/item 之前。

- [ ] **Step 4: 用 launcher/build.sh 编译 + 验证 ./niu mtime 更新**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && ls -la niu | awk '{print $6,$7,$8,$9}' && ./launcher/build.sh 2>&1 | tail -5 && ls -la niu | awk '{print $6,$7,$8,$9}'`
Expected: 编译成功；第二次 `ls -la niu` mtime 应比第一次新

注意 CLAUDE.md 铁律 8：Rust 启动器编译必须用 `launcher/build.sh`，禁止直接 `cargo build`。

- [ ] **Step 5: 暂不 commit（等 Task 7 完成后一起 commit）**

Task 6 的代码改动**暂不 commit**——等 Task 7 完成（tracing gate + 致命错误日志）后，Task 6 + Task 7 的改动一起 commit。这样避免中间状态 Windows GUI 模式下诊断盲区。

implementer 完成 Task 6 代码改动后，直接开始 Task 7，Task 7 完成后一起 commit。

---

### Task 7: Rust tracing 按 flag gate（保留错误诊断）

**Files:**
- Modify: `launcher/src/main.rs`（L1401-1405 tracing init）

**关键设计**：
- 现状：`tracing_subscriber::fmt().init()` 默认输出到 stderr，Windows GUI 模式下 stderr 被丢，macOS/Linux 下输出到终端。
- 改法：Rust 启动期读 `config/user-config.json` 的 `logging.enabled` 字段（用 serde_json），enabled=false 时不 init tracing_subscriber（tracing 调用静默丢弃），enabled=true 时保留原 init。
- **关键修正（审查 Critical 4）**：main.rs L1465/L1870 的 `println!` 旁边**已经有 `error!(...)` 调用**（L1463 error! + L1869 error!）。这些 error! 是错误诊断，被 tracing gate 控制后 enabled=false 时会丢失。但这两处是**致命错误**（API 未运行 / assistant 启动失败，程序要 exit）——必须保留诊断手段。
- **修法**：不改 println!（它本来就是给人看的终端输出），也不改 error!。改为：在 tracing gate 之外，额外用一个独立的 `logs/launcher_error.log` 文件记录致命错误，不受 flag 控制。这样 enabled=false 时用户仍能从 launcher_error.log 看到致命错误。
- **关键修正（审查第三轮 Critical）**：`tracing_subscriber::fmt().init()` 在 main.rs:1401，但 `project_root` 变量在 main.rs:1480 附近才计算（通过 `env::current_exe()` 检测）。**implementer 必须把 tracing init 从 L1401 移到 project_root 计算之后**（约 L1506），或让 `should_enable_logging` 内部用 `env::current_exe()` 自行检测 project_root，不依赖外部传入。
- **L1465 的 `log_fatal_error` 调用**：L1465 在 settings/graph 分支（早 return 路径），在 project_root 计算之前。implementer 需要在 L1465 附近也用 `env::current_exe()` 自行检测 project_root，或把 `log_fatal_error` 改为不接收 project_root 参数、内部自行检测。
- **简化方案**（推荐）：`should_enable_logging` 和 `log_fatal_error` 都不接收 project_root 参数，内部用 `env::current_exe()` 自行检测项目根目录。这样 tracing init 不需要挪位置，L1465 的 `log_fatal_error` 也不依赖作用域。
- Rust 读 config 需要 `serde_json` 依赖（Cargo.toml 已有）。
- timestamp 用 `time` crate（Cargo.toml 已有 `time = { version = "0.3", features = ["macros"] }`），不引入 chrono。

- [ ] **Step 1: 用 gitnexus 分析 main 函数改动 blast radius**

Run: 用 `mcp__gitnexus__impact` 工具，参数 `target="main"`, `direction="upstream"`, `repo="niu-agent"`, `file_path="launcher/src/main.rs"`。

- [ ] **Step 2: 读 launcher/src/main.rs 现有 tracing init + println! 位置 + load_context_config**

Run: `sed -n '1395,1410p;1460,1470p;1865,1875p;860,880p' launcher/src/main.rs`
Expected: 看到 tracing_subscriber::fmt().init()、两处 println!（旁边已有 error!）、load_context_config 接收 project_root 参数的模式

- [ ] **Step 3: 增加 should_enable_logging 函数（内部复用 main.rs 现有 project_root 检测逻辑）**

用 Read 工具读 `launcher/src/main.rs:1475-1510`（现有 project_root 检测逻辑：先试 exeDir 检查 memory/ 是否存在，不存在 fallback 到 cwd 检查 memory/）。

在 main.rs 中增加 `detect_project_root()` 函数（复用现有逻辑）+ `should_enable_logging()` 函数：

```rust
fn detect_project_root() -> String {
    // 复用 main.rs:1475-1505 现有 project_root 检测逻辑
    // Primary: executable directory；Fallback: cwd（检查 memory/ 是否存在）
    let exe_path = std::env::current_exe().unwrap_or_else(|_| std::path::PathBuf::from("."));
    let mut project_root = exe_path
        .parent()
        .map(|p| p.to_string_lossy().to_string())
        .unwrap_or_else(|| ".".to_string());
    let memory_dir = std::path::PathBuf::from(&project_root).join("memory");
    if !memory_dir.exists() {
        let cwd = std::env::current_dir()
            .map(|d| d.to_string_lossy().to_string())
            .unwrap_or_else(|| ".".to_string());
        let cwd_memory_dir = std::path::PathBuf::from(&cwd).join("memory");
        if cwd_memory_dir.exists() {
            project_root = cwd;
        }
    }
    project_root
}

fn should_enable_logging() -> bool {
    // 读 config/user-config.json 的 logging.enabled 字段
    // project_root 通过 detect_project_root() 检测（复用 main.rs 现有逻辑，含 cwd fallback）
    // 失败时返回 false（保守默认）
    let project_root = detect_project_root();
    let config_path = std::path::PathBuf::from(&project_root)
        .join("config")
        .join("user-config.json");
    match std::fs::read_to_string(&config_path) {
        Ok(content) => {
            match serde_json::from_str::<serde_json::Value>(&content) {
                Ok(v) => v.get("logging")
                    .and_then(|l| l.get("enabled"))
                    .and_then(|e| e.as_bool())
                    .unwrap_or(false),
                Err(_) => false,
            }
        }
        Err(_) => false,
    }
}
```

注意：
- `detect_project_root()` 复用 main.rs:1475-1505 的现有逻辑（先试 exeDir 检查 memory/，fallback 到 cwd 检查 memory/）
- **消除重复**：implementer 必须把 main() 函数内 L1476-1503 的 project_root 检测代码也替换为 `let project_root = detect_project_root();`，消除两份相同逻辑并存。原 L1476-1503 的 `info!`/`warn!` 日志可保留（用 detect_project_root 返回值后再调），或直接删除（detect_project_root 内部不记日志，main() 内调用后可按需补日志）
- 这保证开发模式和打包模式都能正确找到 project_root

- [ ] **Step 4: 修改 tracing init 按 flag gate（不需要挪位置）**

用 Read 工具读 `launcher/src/main.rs:1395-1410`。

原代码：
```rust
tracing_subscriber::fmt()
    .with_timer(...)
    .init();
```

改为（`should_enable_logging()` 不接收参数，内部用 detect_project_root 检测，所以不需要 project_root 在作用域内）：
```rust
if should_enable_logging() {
    tracing_subscriber::fmt()
        .with_timer(...)
        .init();
}
// else: 不 init，tracing 调用静默丢弃（logging.enabled=false）
```

注意：L1401-1505 之间有 `info!`/`warn!` 调用（如 L1493/L1498 的 project_root 检测日志），这些在 tracing init 之前调用——如果 enabled=true，tracing 在 L1401 init，L1493 的 `info!` 能输出；如果 enabled=false，tracing 不 init，L1493 的 `info!` 静默丢弃。行为正确。

- [ ] **Step 5: 增加 log_fatal_error 函数（不接收 project_root，用 time crate 格式化时间戳）**

为了在 logging.enabled=false 时仍能诊断致命错误，增加一个辅助函数写 `logs/launcher_error.log`：

```rust
fn log_fatal_error(msg: &str) {
    // 致命错误独立写文件，不受 logging flag 控制
    // project_root 通过 detect_project_root() 检测（与 should_enable_logging 一致）
    let project_root = detect_project_root();
    let log_path = std::path::PathBuf::from(&project_root)
        .join("logs")
        .join("launcher_error.log");
    let _ = std::fs::create_dir_all(log_path.parent().unwrap_or(std::path::Path::new(".")));
    // 用 time crate（Cargo.toml 已有 time = { version = "0.3", features = ["macros"] }）
    // 格式化为 YYYY-MM-DD HH:MM:SS，用户可读
    use time::macros::format_description;
    let format = format_description!("[year]-[month]-[day] [hour]:[minute]:[second]");
    let timestamp = match time::OffsetDateTime::now_local() {
        Ok(t) => t.format(format).unwrap_or_else(|_| "unknown".to_string()),
        Err(_) => "unknown".to_string(),
    };
    let line = format!("[{}] FATAL: {}\n", timestamp, msg);
    let _ = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)
        .and_then(|mut f| std::io::Write::write_all(&mut f, line.as_bytes()));
}
```

注意：
- **先验证 Cargo.toml**：implementer 必须先运行 `grep "^time" launcher/Cargo.toml` 确认 `time = { version = "0.3", features = ["macros"] }` 依赖存在（摸底报告确认有，但 implementer 应自行验证）
- 用 `detect_project_root()` 检测 project_root（复用 main.rs 现有逻辑，含 cwd fallback）
- 用 `time` crate 格式化时间戳（`time::OffsetDateTime::now_local()` + `format_description!` 宏，格式 `YYYY-MM-DD HH:MM:SS` 用户可读）
- 如果 `now_local()` 失败（如时区检测问题），fallback 为 "unknown"
- 如果 Cargo.toml 没有 `time` 依赖（摸底报告说有，但 implementer 验证后若没有），改用 `std::time::SystemTime` 的 Unix 秒数格式（不友好但可工作）
- `log_fatal_error` 不接收 project_root 参数，内部用 `detect_project_root()` 检测，避免作用域问题

- [ ] **Step 6: 在 L1465/L1870 两处致命错误处调用 log_fatal_error**

用 Read 工具读 `launcher/src/main.rs:1460-1470` 和 `1865-1875`。

L1465 附近（API 未运行）：
```rust
// 原有 error! 和 println! 保留不动（给 enabled=true 时的 tracing + 终端）
error!("API is not running, please start the main program first (port={})", args.port);
println!("Error: API is not running on port {}. Please start the main program (niu) first.", args.port);
// 新增：独立文件记录致命错误（不受 flag 控制，内部用 current_exe 检测 project_root）
log_fatal_error(&format!("API is not running on port {}", args.port));
std::process::exit(1);
```

L1870 附近（assistant 启动失败）：
```rust
// 原有 error! 和 println! 保留不动
error!("Failed to launch assistant window: {}", e);
println!("\nPlease run manually: cd ui/main && NIU_WINDOW=assistant npm start");
// 新增：独立文件记录
log_fatal_error(&format!("Failed to launch assistant window: {}", e));
```

注意：`log_fatal_error` 只接收 `msg` 参数（不接收 project_root），内部用 `current_exe()` 自行检测，避免 L1465 在 project_root 作用域之前的问题。

- [ ] **Step 7: 用 launcher/build.sh 编译 + 验证 ./niu mtime 更新**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && ls -la niu | awk '{print $6,$7,$8,$9}' && ./launcher/build.sh 2>&1 | tail -5 && ls -la niu | awk '{print $6,$7,$8,$9}'`
Expected: 编译成功无 warning，mtime 更新

注意：`log_fatal_error` 用 `time` crate（Cargo.toml 已有）。如果 implementer 检查发现 Cargo.toml 没有 `time` 依赖（`grep "^time" launcher/Cargo.toml`），需要改用 `std::time::SystemTime`（格式为 Unix 秒数，不友好但可工作）。

- [ ] **Step 8: Commit（包含 Task 6 的 windows_subsystem 改动）**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add launcher/src/main.rs
git commit -m "feat(launcher): windows_subsystem=windows + gate Rust tracing + fatal error log

Task 6 + Task 7 一起 commit（避免中间状态 Windows GUI 模式诊断盲区）：

Task 6: main.rs 顶部加 #![cfg_attr(all(target_os=\"windows\", not(debug_assertions)),
windows_subsystem=\"windows\")]。release build 且 Windows 下编译为 GUI 子系统，
双击 niu.exe 不弹 cmd 窗口。debug build 保留 console。macOS/Linux 不受影响。

Task 7:
- 增加 should_enable_logging() 读 config/user-config.json 的 logging.enabled
  （不接收参数，内部用 current_exe 检测 project_root，避免作用域问题）
- logging.enabled=false 时不 init tracing_subscriber（tracing 调用静默丢弃）
- L1465/L1870 致命错误处增加 log_fatal_error() 写 logs/launcher_error.log
  （不受 flag 控制，用 time crate 格式化时间戳，确保 enabled=false 时用户仍能诊断致命错误）
- 原有 error!/println! 保留不动（给 enabled=true 时的 tracing + 终端）"
```

---

### Task 8: macOS .app bundle 构造（关闭 Finder 双击 Terminal 窗口）

**Files:**
- Modify: `launcher/build.sh`（编译后构造 .app bundle）

**关键设计**：
- 现状：`build.sh` 只 `cargo build --release` + `cp target/release/niu-launcher ../niu`——裸二进制，macOS Finder 双击会激活 Terminal.app 运行。
- 改法：build.sh 编译后构造 `niu.app/Contents/MacOS/niu` + `Info.plist`（含 `LSUIElement=true` 隐藏 Dock + 不弹 Terminal）。
- **关键修正（审查 Important）**：`LSUIElement=true` 等价于 `main.rs:542-560` 的 objc `setActivationPolicy:1` hack。加 Info.plist 后两者叠加不会有问题（都设 Accessory policy），但 objc hack 在裸二进制启动时（命令行 `./niu`）仍生效。**保留 objc hack 不删**——理由：(1) 命令行 `./niu` 启动时没有 .app bundle 上下文，objc hack 仍需要；(2) 删 objc hack 风险高，程序激活行为可能变化，本轮不做。
- 只在 macOS 平台构造 .app（build.sh 用 `uname` 判断平台）。
- **Linux 平台**：裸二进制在 Linux 桌面双击一般不弹终端（因为没有 Terminal=true 的 .desktop 入口）。如果未来需要 Linux 桌面集成，可加 .desktop 文件，但本轮不做。

- [ ] **Step 1: 读 launcher/build.sh 现有编译逻辑**

Run: `cat launcher/build.sh`
Expected: 看到 cargo build + cp 逻辑

- [ ] **Step 2: 修改 build.sh 编译后构造 macOS .app bundle**

用 Read 工具读 `launcher/build.sh` 完整内容。

在 `cp target/release/niu-launcher ../niu` 之后增加 macOS 平台分支：
```bash
# macOS: 构造 .app bundle（让 Finder 双击不弹 Terminal）
if [ "$(uname)" = "Darwin" ]; then
    APP_DIR="../niu.app/Contents/MacOS"
    mkdir -p "$APP_DIR"
    cp target/release/niu-launcher "$APP_DIR/niu"
    # 写最小 Info.plist
    cat > "../niu.app/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key>
    <string>com.niu.launcher</string>
    <key>CFBundleExecutable</key>
    <string>niu</string>
    <key>CFBundleName</key>
    <string>Niu</string>
    <key>LSUIElement</key>
    <true/>
    <key>LSMinimumSystemVersion</key>
    <string>11.0</string>
</dict>
</plist>
PLIST
    echo "[build.sh] macOS .app bundle created at ../niu.app"
fi
```

注意：
- 保留原有 `cp target/release/niu-launcher ../niu`（裸二进制仍保留，方便命令行 `./niu` 启动）
- .app bundle 是额外产物，Finder 双击用 niu.app，命令行用 ./niu
- Info.plist 的 `LSUIElement=true` 让程序不显示 Dock 图标 + 不弹 Terminal
- 保留 main.rs:542-560 的 objc setActivationPolicy hack（命令行 ./niu 启动时仍需要）

- [ ] **Step 3: 运行 build.sh 验证 .app 构造成功**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && ./launcher/build.sh 2>&1 | tail -10 && ls -la niu.app/Contents/ && cat niu.app/Contents/Info.plist`
Expected: 编译成功 + `niu.app/Contents/MacOS/niu` 存在 + Info.plist 内容正确

- [ ] **Step 3b: 验证 .app 能启动（用 open 命令）**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && open niu.app && sleep 3 && pgrep -f "niu.app/Contents/MacOS/niu" && pkill -f "niu.app/Contents/MacOS/niu" 2>/dev/null; echo "open test done"`
Expected: `pgrep` 找到进程（.app 能启动），不弹 Terminal 窗口

注意：如果 `open niu.app` 失败或弹 Terminal，说明 Info.plist 配置有问题。implementer 需要检查 Info.plist 的 CFBundleExecutable / CFBundleIdentifier 是否正确。

- [ ] **Step 4: 手动验证 Finder 双击不弹 Terminal**

这一步由用户人工确认。implementer 只需验证 .app 结构正确。

- [ ] **Step 5: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add launcher/build.sh
git commit -m "feat(launcher): build macOS .app bundle to stop Finder from spawning Terminal

build.sh 编译后构造 niu.app/Contents/MacOS/niu + Info.plist（LSUIElement=true）。
Finder 双击 niu.app 不再激活 Terminal.app 运行。命令行 ./niu 裸二进制仍保留。

LSUIElement=true 等价于 main.rs:542-560 的 objc setActivationPolicy:1 hack，
本轮保留 objc hack 不删（避免引入新风险，下轮再清理）。"
```

---

### Task 9: 集成验证 + 恢复缺省状态

**Files:**
- Verify: 所有日志开关在缺省关闭状态下行为正确
- Modify: `config/user-config.json`（确认缺省 enabled=false）

**关键设计**：
- **禁止全量测试套件回归**（上一轮教训）——只跑本次新增的测试文件。
- 验证缺省状态：`config/user-config.json` 的 `logging.enabled=false`。
- 验证 ./niu 二进制 mtime 是新的。
- 验证 macOS .app bundle 结构正确（如果 Task 8 在本环境执行）。

- [ ] **Step 1: 运行所有新增测试**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_logging_config.py tests/test_http_logger_flag.py tests/test_http_log_router_conditional.py tests/test_gateway_stderr_flag.py -v`
Expected: PASS, 共 17 tests（6+6+2+3）

**注意**：禁止跑 `pytest tests/` 全量测试——会触发 LightRAG 修复测试损坏 ~/.niu 数据。

- [ ] **Step 2: 验证缺省状态（logging.enabled=false）下所有日志都不写**

Run:
```bash
cd REDACTED_USER_PATH/tools/ai-bot
# 备份现有日志（不直接 rm -rf）
if [ -d logs ]; then mv logs logs.bak.$(date +%s); fi
mkdir -p logs
# 确认 user-config.json 的 logging.enabled=false
python/bin/python -c "
import json
with open('config/user-config.json') as f:
    cfg = json.load(f)
print('logging.enabled =', cfg.get('logging', {}).get('enabled'))
"
```
Expected: `logging.enabled = False`

- [ ] **Step 3: 验证 ./niu 二进制 mtime 是新的**

Run: `ls -la niu | awk '{print $6,$7,$8,$9}'`
Expected: mtime 是最近编译时间

- [ ] **Step 4: 验证 macOS .app bundle 结构（如果在本环境）**

Run: `ls -la niu.app/Contents/ 2>/dev/null && cat niu.app/Contents/Info.plist 2>/dev/null || echo "非 macOS 环境或 Task 8 未执行"`
Expected: niu.app/Contents/MacOS/niu 存在 + Info.plist 含 LSUIElement=true

- [ ] **Step 5: 恢复 config/user-config.json 到缺省关闭状态**

用 Read 工具读 `config/user-config.json`，确认 `logging.enabled=false`。如果被改成 `true`，用 Edit 改回 `false`。

- [ ] **Step 6: Commit 最终状态（如果有变化）**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git status
# 如果 config/user-config.json 有变化（应该没有，因为 .gitignore 不入库）：
git add config/user-config.json 2>/dev/null
git commit -m "docs(config): confirm logging subnode defaults to disabled" || echo "Nothing to commit"
```

---

## Self-Review 检查

**Spec coverage（用户原始要求）：**

| 用户要求 | 对应 Task |
|---|---|
| 找一个使用率比较高的合适的配置文件，增加日志开启选项 | Task 1（用 config/user-config.json + CONFIG_PATH 常量） |
| 缺省情况下所有日志关闭 | Task 1 + Task 2 + Task 3 + Task 4 + Task 5 + Task 7 + Task 9 |
| 控制台窗口不要自动打开（Windows） | Task 6（windows_subsystem=windows） |
| 控制台窗口不要自动打开（macOS） | Task 8（.app bundle + LSUIElement=true） |
| 所有日志输出都要关闭 | Task 2（loguru + stdlib + uvicorn） + Task 3（transport） + Task 4（应用层 + interaction） + Task 5（im_adapter_stderr） + Task 7（Rust tracing） |
| 大模型日志非常占用硬盘都要关闭 | Task 3 + Task 4（raw_http 两层 + llm_interaction） |
| http://localhost:9876/http-log/ 服务要关闭 | Task 2（条件 include router） |
| 多平台支持 | Task 6（Windows） + Task 8（macOS） + Task 7（Rust tracing 跨平台 gate） |

**Placeholder scan：** 计划中所有代码块都是完整可运行的实现或测试，无 TBD / TODO / "Similar to Task N" / "add appropriate error handling" 之类占位符。注释中 `...` 表示"保留原代码不变"，配合明确说明。Task 5 和 Task 7 的 implementer 需要根据实际代码补全测试调用方式 / config 读取逻辑——这是必要的灵活性，不是 placeholder。

**Type consistency：** `LoggingConfig` 类的字段 `enabled: bool` / `level: str` 在 Task 1 定义，Task 2/3/4/5/7 都通过 `get_logging_config().enabled` 访问，签名一致。Task 3/4 测试都用 `_setup_config` 辅助函数统一 monkeypatch CONFIG_PATH + 重置单例 + 预热，命名一致。

**上一轮错误避免：**
1. ✅ 控制台窗口根因正确诊断（Windows console subsystem + macOS 裸二进制）
2. ✅ 不动 launch_window 的 stdio（上一轮错误改法）
3. ✅ 覆盖 im_adapter_stderr.log 日志源（Task 5）
4. ✅ 覆盖 Rust tracing 日志源（Task 7）
5. ✅ 禁止全量测试（Task 9 明确禁止）
6. ✅ 多平台支持（Task 6 Windows + Task 8 macOS）

**残余注意事项（implementer 必读）：**
1. Task 2 的 loguru `logger.disable("")` 是官方推荐全局禁用方式，但调用顺序很重要：必须先 `logger.remove()` 清空默认 sink，再 `logger.disable("")`。否则 disable 不会清掉已 add 的 sink。
2. Task 3/4 测试用 `monkeypatch.setattr(hl_mod, "_do_patch_http", ...)` 拦截实际 patch，不真正 patch HTTP client——避免测试间状态污染。
3. Task 5 测试需要根据 gateway.py 实际代码结构补全调用方式——implementer 要先读 gateway.py 找到启动飞书 adapter 的入口函数。
4. Task 6 修改 Rust 代码后必须用 `launcher/build.sh`（CLAUDE.md 铁律 8）。
5. Task 7 的 `should_enable_logging()` 函数路径要复用 launcher 现有 config 读取逻辑（L867 附近），不要重写。
6. Task 8 的 .app bundle 构造只在 macOS 执行（build.sh 用 `uname` 判断）。
7. Task 9 禁止跑 `pytest tests/` 全量测试——只跑本次新增的 4 个测试文件。

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-07-22-logging-and-console-v2.md`.**

按用户要求：方案写完后派 Agent 做方案审查，必须连续两次审查无 bug 后才可提交，提交后直接用 SDD 方式执行。全部方案执行后做方案对齐审查和代码质量审查，同样连续两次无 bug 才可关闭本次 loop。
