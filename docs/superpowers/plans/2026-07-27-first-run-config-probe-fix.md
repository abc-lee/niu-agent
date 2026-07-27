# 首次启动配置缺省标准化 + probe 失败不写文件 整改方案

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复"删除 ~/.niu 模拟首次启动 → 模型配置测试卡 4-5 分钟失败 → 失败仍写入不完整配置"的 bug，把三处配置缺省（前端 get-config 兜底 / 前端 testAndSave / config-manager 兜底）统一为代码内联的标准缺省对象。

**Architecture:** user-config.json 无模板文件设计（用户确认），缺省值由代码内联写出标准配置。三处缺省对象内容一致：`main.js get-config` 兜底（给表单初始值 + probeConfig 用）、`settings/index.html testAndSave` 缺省常量（给 probe/save 用）、`config-manager load_user_config` 兜底（给 MCP 工具用）。一致性靠 pytest 锁定 Python 侧 + E2E 验证整体。`probe_failed` 分支改为不写文件。

**Tech Stack:** Electron 33 (ui/main)、FastAPI (niu_api)、LiteLLM SDK、config-manager MCP 服务器、pytest。

---

## 需求追溯（用户全部要求，从第一句到最后一句）

| # | 用户原话（浓缩） | 转化为方案条目 |
|---|---|---|
| R1 | 删 ~/.niu 后配置测试永远不通过；页面外的配置缺省值首次启动不存在 | Task 1/2/3 三处缺省补齐全部基础字段 |
| R2 | 五六分钟后自己退出，太长 | 根因修复后 probe 走正常路径（每次采样几秒），不再撞重试预算；不重收紧重试参数（YAGNI） |
| R3 | 配置写进文件但测试失败 | Task 3: probe_failed 不写文件 |
| R4 | 保存的配置没写知识图谱必需的 Response 格式 | Task 3: 仅 probe 成功才写 `response_format_mode` |
| R5 | **测试失败了你写文件干什么？测试成功才会写** | Task 3 核心原则：测试（连通性+探测）完整通过才 saveConfig |
| R6 | 两个配置大模型没区别，为什么原配置成功新配置失败？ | 根因：probeConfig 继承空 existingConfig，缺 `thinking:disabled`，探测环境≠运行时环境（见根因分析） |
| R7 | 不要用豆包分析；通用 Agent 兼容性；现成 SDK 解决思考链 | 修复不针对任何特定模型——把运行时基础配置提前为缺省，任何模型适用；LiteLLM `drop_params` 继续负责模型适配 |
| R8 | 测试逻辑简单：连通性 + Response 格式种类；必须通用适配所有模型 | 本方案不改 probe 探测逻辑本身（3 档递进已通用），只修它的输入（缺省配置）和输出处理（失败不写文件） |
| R9 | 保底逻辑没有错 | 保留 Tier 3 prompt_only 保底，不动 |
| R10 | 连最后一个都不支持就不是大模型 | prompt_only 是底线，任何大模型可达；不动 |
| R11 | 写入的不是标准配置；**关闭思考链、思维深度 High 是运行基础配置** | 缺省对象：`lightrag_llm.litellm_kwargs.thinking={type:"disabled"}`、`reasoning_effort="high"` |
| R12 | 结合标准配置检查缺省缺什么 | 已完成对照检查（见根因分析附表），本方案按检查结果修 |
| R13 | 睡眠时间 5 分钟没有错；我的配置是个人优化后的，不代表全是缺省 | `sleepTriggerMinutes` 缺省保持 5，**不改**；`logging.enabled` 缺省保持 false，**不改** |
| R14 | 主模型不需要温度值（在提示词里）；知识图谱温度已写进去了 | 缺省 `llm` 段**不加** temperature；`lightrag_llm.temperature=0.2` 保持现有逻辑，不动 |
| R15 | reasoning_effort=none 不代表禁用思考链，只代表思维深度关闭；原有描述写错了 | Task 4: 修正 `llm_proxy.py:198` 注释 + config-manager 3 处工具 description（第 1 轮审查补全追溯） |
| R16 | **user-config 原来就没有设计模板文件；配置缺省由代码自动写出标准模板，不要整个模板文件** | 不新建任何模板 JSON；缺省对象内联在三处代码里，pytest 锁定 Python 侧，E2E 锁定整体 |

## 根因分析

### 完整故障链条（首次启动场景）

1. 删 ~/.niu → Electron `get-config` 返回 `{llm:{}, storage:{}, firstRun:true}`（`ui/main/main.js:1158`）
2. 用户点"测试连接并保存" → `/api/test-llm` 连通性测试**通过**（apiKey/apiBase/model 来自表单，正确）
3. 进入 probe 阶段：`probeConfig.litellm_kwargs = existingConfig.lightrag_llm?.litellm_kwargs || existingConfig.llm?.litellm_kwargs || {}`（`settings/index.html:410`）→ 首次启动 = **`{}`（空）**
4. probe 用"裸"参数发 `json_schema strict` 探测请求——**缺 `thinking:{type:"disabled"}`**，与运行时知识图谱实际调用参数不一致（运行时靠它保证 JSON 输出干净，见 `llm_proxy.py` + `litellm_adapter.py`）
5. 网关对带思考链的 json_schema 请求响应慢/挂起 → 每次采样吃满 15s `read_timeout`（`compat.py:1601`）→ 分类为 `timeout` → 重试不计失败
6. 重试预算：`MAX_TRANSIENT_RETRIES=5`，指数退避 5+10+20+40+80=155s（`compat.py:1446-1469`）→ Tier 1 单档最坏 ≈ 6 次×15s + 155s = **245s** → 返回 `rate_limited` → `probe_failed`（后端永远走不到 Tier 3 prompt_only 保底）
7. **前端 `probe_failed` 仍执行 `saveConfig`**（`settings/index.html:436-437, 455`）→ `newLitellmKwargs = {...空}` → 写入缺 `response_format_mode`/`thinking`/`allowed_openai_params` 的不完整配置；且 `reasoning_effort` 缺省写死 `"none"`（`index.html:450`）
8. 1.5s 后自动关窗 → launcher `try_wait()` 检测到（`main.rs:1841-1869`）→ 无条件退出整个进程（设计如此：让用户重启）
9. 用户视角：等了 4-5 分钟 → 程序退出 → 配置写了但"测试失败"

### 为什么"原配置改网址再测"能成功（同一大模型字段）

`existingConfig` 已存在 → `probeConfig.litellm_kwargs` 继承了已有的 `thinking:{type:"disabled"}` 等基础字段 → 探测环境=运行时环境 → 网关正常响应 → probe 几秒内完成 → 写入 `response_format_mode`。

**唯一差异就是 probeConfig 的 litellm_kwargs 内容**——这不是豆包特定问题：任何"thinking 参数影响响应格式/耗时"的模型都会触发。根因是**缺省配置缺失导致探测环境与运行时环境不一致**，这是通用设计缺陷。

### 缺省配置对照检查结果（标准配置 = 用户提供的真实配置）

| 字段 | 用户标准 | 首次启动实际 | 处置 |
|---|---|---|---|
| `llm.litellm_kwargs.thinking` | `{type:"enabled"}` | 缺失 | **补进缺省对象** |
| `llm.reasoning_effort` | `""` | `""` | 不动 |
| `llm.temperature` | 无此字段 | 无 | 不动（主模型温度在提示词文档里，R14） |
| `lightrag_llm.reasoning_effort` | `"high"` | `"none"`（index.html:450 写死）/ `"xhigh"`（config-manager:387） | **统一为 "high"** |
| `lightrag_llm.temperature` | `0.2` | `0.2`（index.html:451 已正确） | 不动（R14） |
| `lightrag_llm.litellm_kwargs.thinking` | `{type:"disabled"}` | 缺失 | **补进缺省对象 + probeConfig 缺省** |
| `lightrag_llm.litellm_kwargs.allowed_openai_params` | `[]` | 缺失 | **补进缺省对象** |
| `lightrag_llm.litellm_kwargs.response_format_mode` | `"prompt_only"` | 缺失 | probe 成功才写入（Task 3 保证）；缺省对象**不含**此字段 |
| `context.sleepTriggerMinutes` | `30`（个人优化） | `5` | **不动**，缺省保持 5（R13） |
| `context` 其他三项 | 200000 / 0.8 / 60000 | 一致 | 不动 |
| `logging` | `{enabled:true}`（个人优化） | 缺失 | 补字段结构，值用设计缺省 `{enabled:false, level:"INFO"}`（R13） |
| `config-manager` 兜底 `targetThreshold:0.5`、`storage:{documentRoot,databasePath}` | 用户配置无 | 有 | 兜底保留（向后兼容），不删 |

### 顺带发现的独立 bug（本方案一并修）

- `ui/main/preload-assistant.js:8` 从 bundle 路径 `<bundle>/config/user-config.json` 读 `sleepTriggerMinutes`——该文件不存在（.gitignore 排除），永远走 catch 用默认值 5 分钟。真实配置在 `~/.niu/config/user-config.json`。用户改睡眠时间永远不生效。
- `niu_api/internal/region_manager.py:36-38` **同类路径 bug**（第 1 轮审查发现）：`_read_context_window_size()` 用 `dirname×3(__file__) + "config/user-config.json"` 拼出 `<项目根>/config/user-config.json`——同样不存在，永远 fallback 200000。用户在设置页改的 `contextWindowSize` 对脑区逻辑从不生效。修复：改从 `niu_api.config.CONFIG_PATH` 读（与 `agent/subagent.py:117-120` 的正确读法一致）。
- `ui/main/main.js:1135-1138` 注释声称"Electron 启动时 user-config.json 已存在"——前提是 bundle 有模板，但 user-config.json 从来无模板设计（R16），注释前提不成立。
- `ui/main/main.js:1362` probe-response-format 端口**硬编码 9876**，而 test-connection :1180 用 `process.env.NIU_API_PORT || '9876'`。自定义端口时 probe 必然 probe_failed——修复前仍 saveConfig（降级可用），本方案 R5 变更后不写文件 → 自定义端口用户被硬阻断。顺手改为读 NIU_API_PORT（一行）。
- `config-manager/__init__.py` **6 处** MCP 工具 description 与 `llm_proxy.py:198` 同款错误语义（把 reasoning_effort 说成"思考链开关"，2 处还列着本次消除的 `xhigh`）：现役 TOOL_SCHEMAS 3 处（:51/:82/:99，ToolRegistry 同进程路径，主 Agent 实际可见）+ 废弃 stdio `list_tools` 副本 3 处（:1010/:1032/:1049，向后兼容路径）。这些 description 会注入主 Agent 上下文，留着会继续误导 Agent（R15 追溯，第 1 轮发现前 3 处、第 4 轮发现后 3 处）。
- `launcher/src/main.rs launch_window()` 启动 Electron 时**从不传 NIU_API_PORT**（只设 `NIU_WINDOW`）——main.js:1180（test-connection）和 :1362（probe）读 `process.env.NIU_API_PORT` 永远 fallback 9876，自定义端口场景 probe/test-connection 全部打错端口（第 2 轮审查发现，Task 6 修复）。

## 修复原则

1. **缺省代码内联，无模板文件**（R16）：三处缺省对象（get-config 兜底 / testAndSave 常量 / config-manager 兜底）**核心字段一致**（thinking / reasoning_effort / temperature / context 四项 / logging）；`targetThreshold`、`storage` 内部结构为 Python 兜底特有的历史字段（向后兼容保留，JS 两处不含——已验证无害：config-manager:736/813 是赋值非读取，config.py:89 有缺省，Agent subagent.py:162-164 读 targetThreshold 自带 0.30 缺省）。一致性靠 pytest 锁定 Python 侧 + E2E 锁定整体
2. **测试失败不写文件**：连通性 + probe 探测都成功才 saveConfig（R5）
3. **探测环境 = 运行时环境**：probeConfig 必须含运行时基础配置（`thinking:disabled` 等），不依赖 historical config（R6/R7）
4. **通用性**：不引入任何特定模型的分支逻辑；LiteLLM `drop_params` 继续负责参数适配（R7/R8）
5. **保留保底**：Tier 3 prompt_only 逻辑不动（R9/R10）
6. **个人优化项不进缺省**：sleepTriggerMinutes=5、logging.enabled=false 保持（R13）
7. **`niu_api/config.py _get_config_path` 不动**：复制逻辑本来就是"模板存在才复制"的可选设计，无模板即跳过，行为正确，不改

## File Structure

| 文件 | 动作 | 责任 |
|---|---|---|
| `mcp-servers/config-manager/src/niu_config_manager/__init__.py` | 修改 `load_user_config` (:368-397) + 6 处工具 description (:51/:82/:99 + stdio 副本 :1010/:1032/:1049) | Python 侧标准缺省（pytest 锁定）+ R15 错误语义修正 |
| `ui/main/main.js` | 修改 `get-config` (:1150-1159) + 注释 (:1135-1138) + probe 端口 (:1362) | Electron 兜底返回完整标准缺省 + NIU_API_PORT 硬编码修复 |
| `ui/main/windows/settings/index.html` | 修改 `testAndSave` (:380-478) | 缺省常量 + probe_failed 不写文件（核心） |
| `niu_api/llm_proxy.py` | 修改注释 (:196-199) | reasoning_effort 语义修正（R15） |
| `ui/main/preload-assistant.js` | 修改 (:8) | 配置文件路径修正（顺带 bug） |
| `niu_api/internal/region_manager.py` | 修改 `_read_context_window_size` (:30-46) | 同类配置路径 bug（第 1 轮审查发现） |
| `launcher/src/main.rs` | 修改 `launch_window` (:1206-1265) + 3 调用点 (:1458/:1833/:1875) | 传 NIU_API_PORT 给 Electron（第 2 轮审查发现） |
| `tests/test_config_defaults.py` | 新建 | config-manager 缺省字段完整性测试 |

---

### Task 0: 前置——备份 + 影响分析

**Files:** 无（只读操作）

- [ ] **Step 1: 临时提交备份当前工作区**

```bash
cd /Users/lilei/tools/ai-bot
git status
# 仅当存在未提交改动时执行（工作区干净则跳过，禁止空提交）：
git add -A && git commit -m "backup: 首次启动配置缺省整改前的临时备份（基线: $(git rev-parse --short HEAD)）"
```

- [ ] **Step 2: gitnexus 影响分析（CLAUDE.md 铁律 4）**

对以下符号跑 `gitnexus_impact(direction: "upstream")`，记录 blast radius：
- `load_user_config`（config-manager）— 被哪些工具函数使用
- `_read_context_window_size`（region_manager.py）— 被脑区逻辑哪些调用方使用
- `launch_window`（launcher/src/main.rs）— Task 6 修改符号，调用点 :1458/:1833/:1875（grep 已确认全仓仅 3 处）
- `get_llm_config`（llm_proxy.py）— 只改注释，确认无行为影响

预期 LOW/MEDIUM 风险。若任一返回 HIGH/CRITICAL，**停下来报告用户**。

---

### Task 1: config-manager `load_user_config` 兜底标准化（TDD，Python 侧缺省锁定）

**Files:**
- Modify: `mcp-servers/config-manager/src/niu_config_manager/__init__.py:368-397`
- Test: `tests/test_config_defaults.py`（新建）

- [ ] **Step 1: 写失败测试**

创建 `tests/test_config_defaults.py`：

```python
"""首次启动标准缺省配置字段完整性测试（config-manager 兜底 = Python 侧缺省真相源）。

Why: 2026-07-27 首次启动 bug——缺省配置缺 thinking/reasoning_effort 等基础字段，
导致 probe 探测环境与运行时环境不一致（245s+ 重试预算耗尽 probe_failed），
且 probe_failed 仍写文件产生不完整配置。user-config.json 无模板文件设计（用户确认），
缺省由代码内联写出；本测试锁定 config-manager 兜底字段。

三处一致性的精确含义（第 1 轮审查澄清）：前端两处缺省（get-config 兜底 /
testAndSave 常量）与本处的**核心字段**一致（thinking / reasoning_effort /
temperature / context 四项 / logging）；targetThreshold、storage 内部结构为
Python 兜底特有的历史字段（向后兼容保留，JS 两处不含，已验证无害）。
"""
import json

import pytest


@pytest.fixture()
def fallback(tmp_path, monkeypatch) -> dict:
    """文件不存在时 load_user_config 的兜底返回。"""
    import niu_config_manager

    monkeypatch.setattr(niu_config_manager, "USER_CONFIG_PATH", tmp_path / "nonexistent.json")
    return niu_config_manager.load_user_config()


def test_fallback_llm_fields(fallback):
    llm = fallback["llm"]
    assert llm["apiKey"] == ""
    assert llm["apiBase"] == ""
    assert llm["model"] == ""
    assert llm["type"] == "openai"
    assert llm["reasoning_effort"] == ""
    # 主聊天模型开思考链（用户可见思考过程）
    assert llm["litellm_kwargs"]["thinking"] == {"type": "enabled"}
    # 主模型不需要温度字段（温度在提示词文档里，R14）
    assert "temperature" not in llm


def test_fallback_lightrag_llm_fields(fallback):
    lightrag = fallback["lightrag_llm"]
    assert lightrag["type"] == "openai"
    # 思维深度 High（用户确认的基础配置，R11）；历史错误值 "none"/"xhigh" 统一为 "high"
    assert lightrag["reasoning_effort"] == "high"
    # 知识图谱模型温度（与前端 testAndSave 现有缺省一致，R14）
    assert lightrag["temperature"] == 0.2
    # 关闭思考链返回（保证 JSON 输出干净）——探测环境=运行时环境的关键
    assert lightrag["litellm_kwargs"]["thinking"] == {"type": "disabled"}
    assert lightrag["litellm_kwargs"]["allowed_openai_params"] == []
    # probe 成功前缺省不含 response_format_mode（失败不写文件的语义保证，R4/R5）
    assert "response_format_mode" not in lightrag["litellm_kwargs"]


def test_fallback_context_fields(fallback):
    ctx = fallback["context"]
    assert ctx["contextWindowSize"] == 200000
    assert ctx["warningThreshold"] == 0.8
    assert ctx["compressTargetTokens"] == 60000
    # 缺省睡眠时间 5 分钟（R13：用户的 30 是个人优化，不进缺省）
    assert ctx["sleepTriggerMinutes"] == 5


def test_fallback_top_level_fields(fallback):
    assert fallback["firstRun"] is True
    assert "storage" in fallback
    # logging 字段结构补全，值用设计缺省（R13：用户的 true 是个人优化）
    assert fallback["logging"] == {"enabled": False, "level": "INFO"}
```

- [ ] **Step 2: 跑测试确认失败**

用**已装 agent/MCP 依赖的项目 Python**（`niu_config_manager` 模块级 import `mcp.server`/`loguru`，裸系统 python 未装这些包会在收集期 ImportError，报错不指向根因）：

```bash
cd /Users/lilei/tools/ai-bot
PYTHONPATH=mcp-servers/config-manager/src python/bin/python -m pytest tests/test_config_defaults.py -v
# 若 python/bin/python 不可用，用你平时跑 agent 测试的解释器（需已装 mcp/loguru 依赖）
```
预期：FAIL——`reasoning_effort` 当前是 `"xhigh"` ≠ `"high"`，`lightrag_llm` 缺 `temperature`/`litellm_kwargs`，`llm` 缺 `litellm_kwargs`，`context` 缺 `compressTargetTokens`，顶层缺 `logging`

- [ ] **Step 3: 修改 load_user_config**

旧（:368-397）：

```python
def load_user_config() -> dict[str, Any]:
    """Load user configuration."""
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
        },
        "lightrag_llm": {
            "presetId": "",
            "apiKey": "",
            "apiBase": "",
            "model": "",
            "type": "openai",
            "reasoning_effort": "xhigh",
        },
        "context": {
            "contextWindowSize": 200000,
            "warningThreshold": 0.8,
            "targetThreshold": 0.5,
            "sleepTriggerMinutes": 5,
        },
        "storage": {"documentRoot": "", "databasePath": ""},
        "firstRun": True,
    }
```

新：

```python
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
            "provider": "",
            "reasoning_effort": "",
            "litellm_kwargs": {"thinking": {"type": "enabled"}},
        },
        "lightrag_llm": {
            "presetId": "",
            "apiKey": "",
            "apiBase": "",
            "model": "",
            "type": "openai",
            "reasoning_effort": "high",
            "temperature": 0.2,
            "litellm_kwargs": {
                "thinking": {"type": "disabled"},
                "allowed_openai_params": [],
            },
        },
        "context": {
            "contextWindowSize": 200000,
            "warningThreshold": 0.8,
            "targetThreshold": 0.5,
            "compressTargetTokens": 60000,
            "sleepTriggerMinutes": 5,
        },
        "storage": {"documentRoot": "", "databasePath": ""},
        "firstRun": True,
        "logging": {"enabled": False, "level": "INFO"},
    }
```

- [ ] **Step 4: 语法检查 + 跑测试确认通过**

```bash
python -c "import ast; ast.parse(open('/Users/lilei/tools/ai-bot/mcp-servers/config-manager/src/niu_config_manager/__init__.py').read())"
cd /Users/lilei/tools/ai-bot
PYTHONPATH=mcp-servers/config-manager/src python/bin/python -m pytest tests/test_config_defaults.py -v
```
预期：4 passed

- [ ] **Step 5: 提交**

```bash
git add mcp-servers/config-manager/src/niu_config_manager/__init__.py tests/test_config_defaults.py
git commit -m "fix(config-manager): load_user_config 兜底补齐基础字段（reasoning_effort xhigh→high 等）"
```

---

### Task 2: `main.js` get-config 兜底返回完整标准缺省 + 修正过时注释

**Files:**
- Modify: `ui/main/main.js:1135-1159`

- [ ] **Step 1: 修改注释 + get-config handler**

旧注释（:1135-1138）：

```js
// 注意：user-config.json 首次启动复制由 Python 侧（niu_api.config._get_config_path）负责。
// Python API 启动比 Electron 早，Electron 启动时 user-config.json 已存在。
// 如果 Electron 启动时仍未存在（极端时序），get-config handler 返回默认 {llm:{}, storage:{}, firstRun:true}，
// 用户通过设置窗口保存时 save-config 会创建文件，不会冲突。
```

新注释：

```js
// 注意：user-config.json 无模板文件设计——文件由设置窗口 save-config 创建。
// 文件不存在时（首次启动），get-config 返回代码内联标准缺省（与 config-manager
// load_user_config 兜底、settings testAndSave 缺省常量三处一致）——
// 保证表单初始值和 probe 探测都拿到完整基础配置（thinking/reasoning_effort 等），
// 而不是空骨架 {llm:{}}（2026-07-27 首次启动 probe 失败根因）。
```

`get-config` handler（:1150-1159）改为：

```js
ipcMain.handle('get-config', () => {
  try {
    if (fs.existsSync(userConfigPath)) {
      return JSON.parse(fs.readFileSync(userConfigPath, 'utf-8'));
    }
  } catch (e) {
    console.error('Failed to read config:', e);
  }
  // 首次启动兜底：完整标准缺省（三处一致：本处 / testAndSave 常量 / config-manager 兜底）
  return {
    llm: {
      presetId: "", apiKey: "", apiBase: "", model: "", type: "openai",
      provider: "", reasoning_effort: "",
      litellm_kwargs: { thinking: { type: "enabled" } }
    },
    lightrag_llm: {
      presetId: "", apiKey: "", apiBase: "", model: "", type: "openai",
      reasoning_effort: "high", temperature: 0.2,
      litellm_kwargs: { thinking: { type: "disabled" }, allowed_openai_params: [] }
    },
    context: {
      contextWindowSize: 200000, warningThreshold: 0.8,
      compressTargetTokens: 60000, sleepTriggerMinutes: 5
    },
    storage: {},
    firstRun: true,
    logging: { enabled: false, level: "INFO" }
  };
});
```

- [ ] **Step 2: probe 端口硬编码修复（:1362，第 1 轮审查发现）**

`probe-response-format` handler 的 `options` 定义（:1360-1363）：

旧：

```js
    const options = {
      hostname: '127.0.0.1',
      port: 9876,
      path: '/api/probe-response-format',
```

新：

```js
    const options = {
      hostname: '127.0.0.1',
      port: parseInt(process.env.NIU_API_PORT || '9876', 10),  // 与 test-connection :1180 一致，支持 launcher --port 自定义
      path: '/api/probe-response-format',
```

Why: test-connection :1180 用 `NIU_API_PORT`，probe 却写死 9876；launcher 支持 `--port`（main.rs:1287）并传给 Python API。自定义端口时 probe 必然 probe_failed——修复前仍 saveConfig（降级可用），本方案 R5 变更后不写文件 → 自定义端口用户被硬阻断。
注意：本读取依赖 launcher 把 NIU_API_PORT 传给 Electron 进程（**Task 6**，第 2 轮审查发现 launcher 从未传递）；Task 6 完成前本行与修复前行为相同（fallback 9876，默认端口场景无害）。

- [ ] **Step 3: 语法检查**

```bash
node --check /Users/lilei/tools/ai-bot/ui/main/main.js && echo "syntax OK"
```
预期：`syntax OK`

- [ ] **Step 4: 提交**

```bash
git add ui/main/main.js
git commit -m "fix(config): get-config 兜底返回完整标准缺省 + probe 端口读 NIU_API_PORT"
```

---

### Task 3: `settings/index.html` testAndSave——缺省标准化 + probe_failed 不写文件（核心）

**Files:**
- Modify: `ui/main/windows/settings/index.html:380-478`

- [ ] **Step 1: 修改 testAndSave 函数**

在 `async function testAndSave() {` 之上加缺省常量，并完整替换 :380-478 函数体：

```js
    // 标准基础配置缺省（三处一致：本处 / main.js get-config 兜底 / config-manager 兜底）。
    // Why: 首次启动 existingConfig 是空骨架，probe/save 的缺省必须自带这些
    // 基础运行字段，保证 probe 探测环境 = 运行时环境（2026-07-27 首次启动
    // probe 失败根因：缺 thinking:disabled，探测请求与运行时实际调用不一致）。
    const DEFAULT_LLM_KWARGS = { thinking: { type: "enabled" } };
    const DEFAULT_LIGHTRAG_KWARGS = { thinking: { type: "disabled" }, allowed_openai_params: [] };

    async function testAndSave() {
      const existingConfig = await window.electronAPI.getConfig();
      const presetId = document.getElementById('preset').value;
      const apiKey = document.getElementById('apiKey').value;
      const apiBase = document.getElementById('apiBase').value;
      const model = document.getElementById('model').value;
      const type = document.getElementById('apiType').value;
      const provider = document.getElementById('provider').value;

      if (!apiKey) { setStatus('请输入 API Key', 'error'); return; }
      if (!apiBase) { setStatus('请选择预设或填写 API 地址', 'error'); return; }
      if (!model) { setStatus('请输入模型名称', 'error'); return; }

      setStatus('测试模型连接中...', 'loading');
      document.getElementById('testBtn').disabled = true;

      // 第一步：测试（传表单值到请求体，不保存文件）
      const config = {
        apiKey, apiBase, model, type,
        provider: provider,
        litellm_kwargs: { ...DEFAULT_LLM_KWARGS, ...(existingConfig.llm?.litellm_kwargs || {}) }
      };
      const result = await window.electronAPI.testConnection(config);

      if (result.success) {
        // 第一步半：测试通过后追加 response_format 探测（3 档递进）
        setStatus('探测格式化输出能力中（可能需要 1-5 分钟（三次采样 + 限流/超时重试））...', 'loading');
        // probe 必须用知识图谱模型的运行时基础配置（含 thinking:disabled），
        // 保证探测环境 = 运行时环境，探测档位结果对运行时才有效
        const probeConfig = {
          ...config,
          litellm_kwargs: { ...DEFAULT_LIGHTRAG_KWARGS, ...(existingConfig.lightrag_llm?.litellm_kwargs || {}) },
        };
        const probeResult = await window.electronAPI.probeResponseFormat(probeConfig);

        // probe_failed（限流/超时/基础设施错误）：不写文件，提示重试。
        // Why（用户核心原则）：测试失败不写文件，测试成功才写。失败时写文件会
        // 产生缺 response_format_mode 的不完整配置，知识图谱运行时拿不到
        // Response 格式档位。不自动关窗，让用户决定重试或放弃。
        if (probeResult.result !== 'supported') {
          setStatus('格式化输出探测失败（' + (probeResult.reason || '未知错误') + '），配置未保存。请稍后点击按钮重试。', 'error');
          document.getElementById('testBtn').disabled = false;
          return;
        }

        const responseFormatMode = probeResult.mode || 'prompt_only';
        const allowedOpenaiParams = responseFormatMode === 'prompt_only' ? [] : ['response_format'];
        const probeReason = probeResult.reason || `格式化输出档位: ${responseFormatMode}`;

        // 第二步：保存配置（仅连通性测试 + probe 探测都成功才执行到这里）
        setStatus('保存配置中...（' + probeReason + '）', 'loading');
        const existingLightragLlm = existingConfig.lightrag_llm || {};
        const existingLightragKwargs = existingLightragLlm.litellm_kwargs || {};
        // 基础配置（thinking:disabled 等）无条件写入 + 探测结果覆盖
        const newLitellmKwargs = {
          ...DEFAULT_LIGHTRAG_KWARGS,
          ...existingLightragKwargs,
          response_format_mode: responseFormatMode,
          allowed_openai_params: allowedOpenaiParams,
        };
        const lightragLlmConfig = {
          presetId: existingLightragLlm.presetId || "",
          apiKey: existingLightragLlm.apiKey || "",
          apiBase: existingLightragLlm.apiBase || "",
          model: existingLightragLlm.model || "",
          type: existingLightragLlm.type || "openai",
          // 思维深度 High 是知识图谱基础配置（用户确认），缺省不是 "none"
          reasoning_effort: existingLightragLlm.reasoning_effort || "high",
          temperature: existingLightragLlm.temperature ?? 0.2,
          litellm_kwargs: newLitellmKwargs,
        };

        const saveResult = await window.electronAPI.saveConfig({
          llm: { ...config, presetId: presetId, reasoning_effort: existingConfig.llm?.reasoning_effort || "" },
          lightrag_llm: lightragLlmConfig,
          context: {
            contextWindowSize: parseInt(document.getElementById('contextWindowSize').value) || 200000,
            warningThreshold: parseFloat(document.getElementById('warningThreshold').value) || 0.8,
            compressTargetTokens: parseInt(document.getElementById('compressTargetTokens').value) || 60000,
            sleepTriggerMinutes: parseInt(document.getElementById('sleepTriggerMinutes').value) || 5
          },
          storage: existingConfig.storage || {},
          // 保留用户已有 logging 设置；首次启动用设计缺省（关日志）
          logging: existingConfig.logging || { enabled: false, level: "INFO" },
          firstRun: false
        });
        if (saveResult.success) {
          testPassedInSession = true;
          setStatus('模型测试通过，配置已保存！格式化输出: ' + probeReason, 'success');
          setTimeout(() => { window.electronAPI.closeWindow(); }, 1500);
        } else {
          setStatus('保存失败: ' + saveResult.error, 'error');
        }
      } else {
        setStatus('模型测试失败: ' + (result.message || '请检查配置'), 'error');
      }
      document.getElementById('testBtn').disabled = false;
    }
```

改动点清单（供审查）：
1. 函数外新增 `DEFAULT_LLM_KWARGS` / `DEFAULT_LIGHTRAG_KWARGS` 常量
2. `config.litellm_kwargs`：`existingConfig.llm?.litellm_kwargs || {}` → `{...DEFAULT_LLM_KWARGS, ...(existing)}`
3. `probeConfig.litellm_kwargs`：`existing.lightrag || existing.llm || {}` → `{...DEFAULT_LIGHTRAG_KWARGS, ...(existing.lightrag || {})}`（**不再 fallback 到 llm 段**——lightrag 探测要用 lightrag 的运行时配置 thinking:**disabled**，fallback 到 llm 的 thinking:**enabled** 是错误来源之一）
4. `probe_failed` 分支：提前 return，**不执行 saveConfig、不自动关窗**
5. `lightragLlmConfig.reasoning_effort` 缺省 `"none"` → `"high"`
6. `newLitellmKwargs`：无条件合并 `DEFAULT_LIGHTRAG_KWARGS`（原仅 probe 成功才补 thinking）
7. `save-config` 对象新增 `logging` 字段（保留已有，缺省 `{enabled:false, level:"INFO"}`）
8. 删除原 `probeFailed` 标志变量（不再需要——失败直接 return）

- [ ] **Step 2: JS 语法检查（提取 script 段验证）**

```bash
cd /Users/lilei/tools/ai-bot
python -c "
import re
html = open('ui/main/windows/settings/index.html').read()
scripts = re.findall(r'<script>(.*?)</script>', html, re.S)
open('/tmp/settings_inline.js', 'w').write('\n'.join(scripts))
print(f'extracted {len(scripts)} script blocks')
"
node --check /tmp/settings_inline.js && echo "syntax OK"
```
预期：`syntax OK`

- [ ] **Step 3: 提交**

```bash
git add ui/main/windows/settings/index.html
git commit -m "fix(settings): probe_failed 不写文件 + 缺省配置标准化（thinking/reasoning_effort）"
```

---

### Task 4: `llm_proxy.py` + config-manager 3 处 description——reasoning_effort 语义修正（R15 全量追溯）

**Files:**
- Modify: `niu_api/llm_proxy.py:196-199`
- Modify: `mcp-servers/config-manager/src/niu_config_manager/__init__.py:51, :82, :99`

- [ ] **Step 1: 修改 llm_proxy.py docstring**

旧（:196-199）：

```python
            model 为空时使用主 llm 同一模型（正常默认行为）。
            apiKey/apiBase/type 为空时从 llm 段继承。
            reasoning_effort 默认 "none"（独立于模型配置，强制禁用思考链）。
            用户可在 lightrag_llm 段显式设置 reasoning_effort 覆盖默认值。
```

新：

```python
            model 为空时使用主 llm 同一模型（正常默认行为）。
            apiKey/apiBase/type 为空时从 llm 段继承。
            reasoning_effort 默认 "none"（思维深度关闭；仅控制推理深度，
            不控制思考链返回——思考链返回由 litellm_kwargs.thinking 独立控制，
            两者是不同参数，不可混淆）。
            用户可在 lightrag_llm 段显式设置 reasoning_effort 覆盖默认值。
```

只改注释，不改 :224-226 的兜底逻辑（缺省配置写入 "high" 后文件有值，兜底不触发）。

- [ ] **Step 2: 修改 config-manager TOOL_SCHEMAS 3 处工具 description（第 1 轮审查发现，R15 追溯补全）**

这些 description 会注入主 Agent 上下文指导工具调用，"none 禁用思考链"的错误心智模型留着会继续误导 Agent。

**:51（set_llm_config 的 reasoning_effort）** 旧：

```python
                    "description": "Thinking chain depth: 'none' (disable), 'low', 'medium', 'high', 'xhigh'. Affects how deeply the model reasons before responding.",
```

新：

```python
                    "description": "Reasoning depth: 'none', 'low', 'medium', 'high' (model-dependent). Affects how deeply the model reasons before responding. Controls reasoning depth only, NOT thinking-chain output (thinking-chain return is controlled by litellm_kwargs.thinking).",
```

**:82（set_lightrag_llm_config 的 description）** 旧：

```python
        "description": "Set LightRAG LLM configuration. If model is set to empty string, removes the lightrag_llm section so that LightRAG falls back to the main LLM configuration. Default reasoning_effort is 'none' (disables thinking chain).",
```

新：

```python
        "description": "Set LightRAG LLM configuration. If model is set to empty string, removes the lightrag_llm section so that LightRAG falls back to the main LLM configuration.",
```

**:99（set_lightrag_llm_config 的 reasoning_effort）** 旧：

```python
                    "description": "Thinking chain depth: 'none' (default for LightRAG), 'low', 'medium', 'high'. LightRAG officially recommends 'none' to avoid timeouts.",
```

新：

```python
                    "description": "Reasoning depth: 'none', 'low', 'medium', 'high'. Controls reasoning depth only, NOT thinking-chain output (controlled by litellm_kwargs.thinking). Standard default config uses 'high'.",
```

- [ ] **Step 3: 修改 stdio `list_tools` 副本 3 处（第 4 轮审查发现，R15 全量追溯）**

同文件 `@server.list_tools()` 处理器（:982 起，废弃但保留向后兼容的 stdio 路径）含 3 处同款错误文本，一并修正，与 Step 2 新文本保持一致。

**:1010（set_llm_config 的 reasoning_effort）** 旧：

```python
                        "description": "Thinking chain depth: 'none' (disable), 'low', 'medium', 'high', 'xhigh'. Affects how deeply the model reasons before responding.",
```

新：

```python
                        "description": "Reasoning depth: 'none', 'low', 'medium', 'high' (model-dependent). Affects how deeply the model reasons before responding. Controls reasoning depth only, NOT thinking-chain output (thinking-chain return is controlled by litellm_kwargs.thinking).",
```

**:1032（set_lightrag_llm_config 的 description）** 旧：

```python
            description="Set LightRAG LLM configuration. If model='', clears the section (falls back to main llm). Default reasoning_effort='none' disables thinking chain.",
```

新：

```python
            description="Set LightRAG LLM configuration. If model='', clears the section (falls back to main llm).",
```

**:1049（set_lightrag_llm_config 的 reasoning_effort）** 旧：

```python
                        "description": "Thinking chain depth: 'none', 'low', 'medium', 'high'. Default 'none'.",
```

新：

```python
                        "description": "Reasoning depth: 'none', 'low', 'medium', 'high'. Controls reasoning depth only, NOT thinking-chain output (controlled by litellm_kwargs.thinking). Standard default config uses 'high'.",
```

- [ ] **Step 4: 语法检查 + 验证无残留 + 提交**

```bash
python -c "import ast; ast.parse(open('/Users/lilei/tools/ai-bot/niu_api/llm_proxy.py').read())"
python -c "import ast; ast.parse(open('/Users/lilei/tools/ai-bot/mcp-servers/config-manager/src/niu_config_manager/__init__.py').read())"
# 验证 6 处错误文本全部消除（应无输出）：
grep -n "Thinking chain depth\|disables thinking chain\|xhigh" /Users/lilei/tools/ai-bot/mcp-servers/config-manager/src/niu_config_manager/__init__.py
git add niu_api/llm_proxy.py mcp-servers/config-manager/src/niu_config_manager/__init__.py
git commit -m "docs(llm_proxy,config-manager): 修正 reasoning_effort 语义描述 6 处（思维深度≠思考链开关，R15 全量追溯）"
```

---

### Task 5: 同类配置路径 bug 修复——`preload-assistant.js` + `region_manager.py`

**Files:**
- Modify: `ui/main/preload-assistant.js:8`
- Modify: `niu_api/internal/region_manager.py:30-46`

- [ ] **Step 1: 修改 preload-assistant.js 读取路径**

旧（:8）：

```js
  const userConfigPath = path.join(__dirname, '..', '..', 'config', 'user-config.json');
```

新：

```js
  const userConfigPath = path.join(require('os').homedir(), '.niu', 'config', 'user-config.json');
```

Why: 原路径指向 bundle 内 `config/user-config.json`——该文件被 .gitignore 排除、
打包不存在，永远走 catch 用默认值 5 分钟。用户在设置页改的 sleepTriggerMinutes
对 assistant 窗口从不生效。真实配置在 `~/.niu/config/user-config.json`。
（assistant 窗口 sandbox:false，require('os') 可用——main.js:116-120）

- [ ] **Step 2: 修改 region_manager.py `_read_context_window_size`（第 1 轮审查发现，同类 bug）**

旧（:30-46）：

```python
def _read_context_window_size() -> int:
    """Read context window size from user config.

    Returns 200000 as default if config is missing or unreadable.
    """
    try:
        config_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "config", "user-config.json",
        )
        if os.path.exists(config_path):
            with open(config_path, encoding="utf-8") as f:
                data = json.load(f)
            return data.get("context", {}).get("contextWindowSize", 200000)
    except Exception:
        pass
    return 200000
```

新：

```python
def _read_context_window_size() -> int:
    """Read context window size from user config.

    Returns 200000 as default if config is missing or unreadable.
    """
    try:
        # 真实配置在 ~/.niu/config/user-config.json（niu_api.config.CONFIG_PATH），
        # 与 agent/subagent.py:117-120 的正确读法一致。旧路径 <项目根>/config/
        # user-config.json 被 .gitignore 排除、不存在，永远 fallback 200000——
        # 用户改的 contextWindowSize 对脑区逻辑从不生效（与 preload-assistant.js:8 同类 bug）。
        from niu_api.config import CONFIG_PATH
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, encoding="utf-8") as f:
                data = json.load(f)
            return data.get("context", {}).get("contextWindowSize", 200000)
    except Exception:
        pass
    return 200000
```

- [ ] **Step 3: 语法检查 + 提交**

```bash
node --check /Users/lilei/tools/ai-bot/ui/main/preload-assistant.js && echo "JS syntax OK"
python -c "import ast; ast.parse(open('/Users/lilei/tools/ai-bot/niu_api/internal/region_manager.py').read())"
git add ui/main/preload-assistant.js niu_api/internal/region_manager.py
git commit -m "fix(config-path): preload-assistant 与 region_manager 改从 ~/.niu 读配置（原读项目根不存在的文件）"
```

---

### Task 6: launcher 传 NIU_API_PORT 给 Electron（Rust，第 2 轮审查发现）

**Files:**
- Modify: `launcher/src/main.rs`（`launch_window` :1206-1265 + 3 调用点 :1458/:1833/:1875）

**背景：** 第 2 轮审查发现 `launch_window()` 启动 Electron 时只设置 `.env("NIU_WINDOW", name)`（:1214/:1234/:1244/:1258 四分支），**从未传 NIU_API_PORT**——main.js:1180（test-connection）和 :1362（probe，Task 2 修复后）读 `process.env.NIU_API_PORT` 永远 fallback 9876。自定义端口（`./niu --port 8888`）时 probe 必然 ECONNREFUSED → probe_failed → 本方案 R5 变更后不写文件 → 自定义端口用户被永久硬阻断。

- [ ] **Step 1: 修改 launch_window 签名 + 四分支加 env**

签名（:1206）：

旧：

```rust
fn launch_window(name: &str) -> Result<std::process::Child, Box<dyn std::error::Error>> {
```

新：

```rust
fn launch_window(name: &str, port: u16) -> Result<std::process::Child, Box<dyn std::error::Error>> {
```

四个分支的 `.env("NIU_WINDOW", name)`（:1214/:1234/:1244/:1258）各加一行（Windows 分支示例，其余三分支相同）：

```rust
        cmd.args(["/C", "npm", "start"])
            .env("NIU_WINDOW", name)
            .env("NIU_API_PORT", port.to_string())  // 让 Electron 的 test-connection/probe 打到正确端口
            .current_dir(&window_dir);
```

注：若 `args.port` 实际类型不是 u16（读 Args 定义 :1271-1290 确认），签名类型与之一致。

- [ ] **Step 2: 修改 3 个调用点**

- :1458（--settings/--graph 模式）：`launch_window(window_name)` → `launch_window(window_name, args.port)`
- :1833（settings 流程）：`launch_window("settings")` → `launch_window("settings", port)`
- :1875（assistant 流程）：`launch_window("assistant")` → `launch_window("assistant", port)`

- [ ] **Step 3: 编译（CLAUDE.md 铁律 8：禁止直接 cargo build）**

```bash
cd /Users/lilei/tools/ai-bot
./launcher/build.sh
# build.sh 编译后自动 cp target/release/niu-launcher → 项目根 niu
ls -la niu  # 确认时间戳为刚刚
```

- [ ] **Step 4: 提交**

```bash
git add launcher/src/main.rs
git commit -m "fix(launcher): launch_window 传 NIU_API_PORT 给 Electron（自定义端口场景 probe/test-connection 打通）"
```

---

### Task 7: 提交前检查 + 真实 E2E 验证（真实 LLM，禁止 mock）

**Files:** 无（验证操作）

- [ ] **Step 1: gitnexus detect_changes（CLAUDE.md 铁律）**

跑 `gitnexus_detect_changes()`，确认改动只涉及本方案列出的符号，无意外影响面。

- [ ] **Step 2: 全量 pytest 回归**

用与 Task 1 相同的项目 Python（裸系统 python 缺 mcp/loguru 会在收集期 ImportError）：

```bash
cd /Users/lilei/tools/ai-bot
PYTHONPATH=mcp-servers/config-manager/src python/bin/python -m pytest tests/test_config_defaults.py -v
```
预期：4 passed。再跑既有 config 相关测试确认无回归。

- [ ] **Step 3: E2E 前置——备份真实 ~/.niu**

```bash
mv ~/.niu ~/.niu.bak-20260727
```

- [ ] **Step 4: 场景 A（核心）——首次启动完整通过**

1. `./niu` 启动（此时 ~/.niu 不存在，模拟首次启动）
2. 确认进入模型配置页面
3. **记录开始时间**（`date +%s`），填真实配置（volcengine ark-code-latest），点"测试连接并保存"
4. **预期**：
   - 连通性测试通过
   - probe 探测在数秒内完成（不再卡 4-5 分钟）——因为 probeConfig 已含 `thinking:disabled`，探测环境=运行时环境
   - 配置写入 `~/.niu/config/user-config.json`
   - 自动关窗 → launcher 退出 → 重启 `./niu` → 正常对话（脑区注入正常）

验证命令（测试通过后）：

```bash
# 字段完整性（含第 1 轮审查补充的 temperature / allowed_openai_params 断言）
python -c "
import json
cfg = json.load(open('$HOME/.niu/config/user-config.json'))
lk = cfg['lightrag_llm']['litellm_kwargs']
assert lk['thinking'] == {'type': 'disabled'}, lk
assert 'response_format_mode' in lk, lk
mode = lk['response_format_mode']
assert lk['allowed_openai_params'] == ([] if mode == 'prompt_only' else ['response_format']), (mode, lk)
assert cfg['lightrag_llm']['reasoning_effort'] == 'high', cfg['lightrag_llm']
assert cfg['lightrag_llm']['temperature'] == 0.2, cfg['lightrag_llm']
assert cfg['llm']['litellm_kwargs']['thinking'] == {'type': 'enabled'}, cfg['llm']
assert 'logging' in cfg, cfg.keys()
assert cfg['context']['sleepTriggerMinutes'] == 5, cfg['context']
print('场景 A 配置完整性: PASS')
"

# 耗时验证（R2 核心诉求，第 2 轮审查修正）：用 wall-clock 断言，不用 grep 日志——
# logging.enabled=false 缺省下探测日志不产生（compat.py loguru 被 logger.disable），
# grep 不存在文件会误报"正常"。
# 判定：点击测试按钮前记录 T0=$(date +%s)，"配置已保存"提示出现时记录 T1=$(date +%s)，
# 断言 T1-T0 < 60s（正常路径每次采样几秒；重试耗尽路径 ≥245s 必然超限）
```

- [ ] **Step 5: 场景 B——测试失败不写文件**

1. 删 ~/.niu（保留 .bak）→ `./niu` → 配置页面**故意填错误 apiBase**（如 `https://invalid.example.com/v1`）+ 真实 apiKey/model
2. 点测试 → **预期**：连通性测试失败 → 不写文件
3. 验证：`ls ~/.niu/config/user-config.json` 应**不存在**（save-config 未执行）
4. 改正 apiBase 为真实地址 → 再点测试 → 预期通过并写入

- [ ] **Step 6: 场景 C——回归：已有配置改网址再测**

1. 场景 A 完成后（已有完整配置）→ 改错 apiBase → 启动进配置页面 → 改回正确 → 测试
2. **预期**：与修复前行为一致——测试通过 + `response_format_mode` 正常写入（原配置里的 `thinking:disabled` 等字段经 existingConfig 保留）

- [ ] **Step 7: 场景 E——自定义端口冒烟（Task 6 验证，第 2 轮审查补充）**

1. 删 ~/.niu（保留 .bak）→ `./niu --port 8899` 启动
2. 配置页面填真实配置 → 点测试
3. **预期**：probe 打通（不再 ECONNREFUSED）→ 测试通过 → 配置写入。若 probe_failed 且 reason 含连接拒绝/超时，说明 Electron 未拿到 NIU_API_PORT，Task 6 修复未生效
4. 验证 Electron 进程环境：`ps eww <Electron进程pid> | tr ' ' '\n' | grep NIU_API_PORT` 应输出 `NIU_API_PORT=8899`

- [ ] **Step 8: 清理测试进程 + 恢复真实配置**

```bash
# 必须 kill -TERM 优雅退出（禁止 pkill 强杀，会损坏 LightRAG vdb）
ps aux | grep -E "niu|niu_api" | grep -v grep
kill -TERM <pid列表>
# 等进程退出后：
rm -rf ~/.niu
mv ~/.niu.bak-20260727 ~/.niu
```

- [ ] **Step 9: 修复 git 操作后的文件权限（CLAUDE.md 铁律 7，若期间做过 checkout/reset）**

```bash
find python/bin/ -type f -exec grep -l '^#!' {} \; | xargs chmod +x
find ui/*/node_modules/.bin/ -type f ! -perm -u+x -exec chmod +x {} \;
```

---

## Self-Review

**1. Spec coverage（对照需求追溯表）：**
- R1 缺省补齐 → Task 1/2/3 ✓
- R2 耗时 → 根因修复（Task 3 probeConfig 含 thinking:disabled 后走正常路径）✓（不收紧重试参数，YAGNI——重试预算是异常路径保护，非本 bug 根因）
- R3/R4/R5 失败不写文件 → Task 3 ✓
- R6 根因 → 根因分析节 + Task 3 ✓
- R7/R8 通用性 → 无特定模型分支；DEFAULT 常量是项目运行时标准（LiteLLM 对不支持的模型 drop_params 自动丢弃 thinking 参数）✓
- R9/R10 保底不动 → 未触碰 compat.py probe 逻辑 ✓
- R11 thinking/reasoning_effort → Task 1/2/3 ✓
- R12 对照检查 → 根因分析附表 ✓
- R13 sleepTriggerMinutes/logging 不动 → Task 1 测试锁定 5/false ✓
- R14 temperature → 缺省 llm 段无 temperature，lightrag 0.2 不动 ✓
- R15 注释语义 → Task 4 ✓
- R16 无模板文件 → 全方案无新建 JSON 模板，缺省内联三处代码 ✓

**2. Placeholder scan：** 每个 Task 的 Step 均含完整代码/命令，无 TBD/TODO。

**3. Type consistency：**
- `DEFAULT_LLM_KWARGS` / `DEFAULT_LIGHTRAG_KWARGS`（Task 3 JS）与 get-config 兜底 `litellm_kwargs`（Task 2 JS）与 config-manager 兜底（Task 1 Python）三处字段一致 ✓
- 三处 `reasoning_effort`：llm 段 `""`、lightrag 段 `"high"` 一致 ✓
- 测试断言（Task 1）与 config-manager 兜底字段一一对应 ✓

**风险与边界：**
- 缺省 `llm.provider=""`：用户标准是 `"volcengine"`，但 provider 是表单字段（用户选择），缺省留空正确 ✓
- `allowed_openai_params` 语义：probe 成功时会被覆盖为 `['response_format']` 或 `[]`（按档位），缺省 `[]` 与 prompt_only 语义一致 ✓
- probeConfig 不再 fallback 到 `llm.litellm_kwargs`（原 `|| existingConfig.llm?.litellm_kwargs`）：lightrag 探测必须用 lightrag 的运行时配置（thinking:**disabled**），fallback 到 llm 的 thinking:**enabled** 是错误来源之一 ✓
- 三处缺省对象为**核心字段一致**（thinking/reasoning_effort/temperature/context 四项/logging）；`targetThreshold`、`storage` 内部结构为 Python 兜底特有历史字段（向后兼容保留，JS 两处不含，已验证无害）。靠"pytest 锁定 Python 侧 + E2E 锁定整体 + 注释互相指引"保持；未来改缺省必须三处同改 ✓
- **已接受风险（第 1 轮审查 P2-3）**：`/api/test-llm` 路径首次携带 `thinking:{type:"enabled"}` 参数后，该路径 drop_params 未开启（compat.py:1274 `reasoning_effort:None` 且无 response_format → litellm_adapter.py:384-388 两个 drop_params 条件均不满足 → 参数原样传递）。这与运行时主聊天行为**一致**（运行时用户配置同样带 thinking:enabled）——探测环境=运行时环境原则下，provider 不兼容 thinking 导致的测试期失败=运行时也会失败的合理暴露。对不支持 thinking 的 provider，LiteLLM 多数路由自动忽略未知 kwargs。不改代码，记录备查 ✓
- **已有显式值不迁移（第 1 轮审查 P2-6，已向用户明示）**：`existingLightragLlm.reasoning_effort || "high"` 对已有真值原样保留——被旧 bug 写坏配置（reasoning_effort 被写死 "none"）的用户，修复后重测仍停在 "none"，需删除配置重走首启才拿到 "high"。不做一次性迁移（避免覆盖用户主动设置）；本仓库唯一用户的真实配置已是 "high"，无迁移需求 ✓
- launcher 端 180s preload 轮询、probe 前端 300s timeout、后端重试预算 5 次：**均不动**。根因修复后 probe 走正常路径（每次采样几秒），异常路径保护参数维持现状（YAGNI）✓

---

## 交付条件

1. 全部 Task 完成，4 个 pytest 通过
2. E2E 场景 A/B/C/E 全部 PASS（真实 LLM、真实数据）
3. 方案审查连续两轮无 bug（按用户既定审查流程）
