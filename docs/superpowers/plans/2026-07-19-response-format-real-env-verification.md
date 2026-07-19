# LightRAG response_format 真实环境验证报告

**验证时间**：2026-07-19
**验证环境**：用户已启动 niu_api（端口 9876），用两个真实配置文件实测
**目的**：在方案审查第 1 轮发现 Critical 问题后，验证关键假设的真实性

---

## 一、原始假设（方案 v1 的前提）

1. 豆包 Coding Plan 端点**网关静默剥离 response_format 但返回 200**，模型自由生成非 JSON 文本
2. 现有 `BadRequestError` fallback 不触发（因为返回 200 不是 400）
3. 探测时需 `drop_params=False` 让 LiteLLM 抛异常区分支持/不支持

## 二、真实测试结果（完全推翻原假设）

### 测试 1：豆包 Coding Plan 端点（`config/user-config.json`）

| Tier | response_format | drop_params | allowed_openai_params | 真实行为 |
|------|-----------------|-------------|----------------------|----------|
| 1 | json_schema strict | False | 无 | **LiteLLM 客户端抛 `UnsupportedParamsError`**（请求未发出） |
| 2 | json_object | False | 无 | **LiteLLM 客户端抛 `UnsupportedParamsError`**（请求未发出） |
| 3 | 无 | False | 无 | 200 OK，模型按 prompt 输出 `{"ok": true}`（合法 JSON） |
| 2 | json_object | **True** | **["response_format"]** | **豆包网关返回 `BadRequestError`**："response_format.type is not valid: json_object is not supported by this model" |

**关键事实**：
- 豆包 Coding Plan 网关**不是静默剥离返回 200**——而是真正透传后**返回 400 BadRequestError**
- 但要触发这个 400，必须 `drop_params=True` + `allowed_openai_params=["response_format"]`，否则 LiteLLM 客户端在 volcengine provider router 层就拒绝抛 `UnsupportedParamsError`，请求根本发不出去
- 原方案"`BadRequestError` fallback 能捕获"对豆包 Coding Plan **其实可以触发**——前提是配置正确（即探测后写入 `allowed_openai_params`）

### 测试 2：GLM 端点（`config/user-config - glm.json`）

| Tier | response_format | drop_params | 真实行为 |
|------|-----------------|-------------|----------|
| 1 | json_schema strict | False | 200 OK，但响应 `{"oko":` （字段名漂移 + 截断 + 非法 JSON） |
| 2 | json_object | True | 200 OK，但响应 `{"ok": true}\n}` （含额外字符，json.loads 失败） |
| 2 | json_object | True | 200 OK，但响应 `{"ok": true}\n\t\t...` （3 次测试都漂移） |
| 3 | 无 | - | 200 OK，响应 `{"ok": true}`（合法 JSON） |

**关键事实**：
- GLM 网关**接受 response_format 参数**（不报 400，不像豆包），但**模型不真正遵守 schema 约束**，输出仍含额外字符导致 json.loads 失败
- 这才是真正的"静默降级"——返回 200 + 非 JSON 输出，原方案 `BadRequestError` fallback 不触发
- 用户原话说"GLM 支持其他返回格式"——**实测发现 GLM 接受参数但不真正生效**，应判定为 `prompt_only`（Tier 3 输出反而最稳定）

## 三、对方案的核心修正

### 修正 1：探测必须 `drop_params=True` + `allowed_openai_params=["response_format"]`（与运行时一致）

**原方案**：探测用 `drop_params=False`，让 LiteLLM 抛 `BadRequestError` 区分支持/不支持。
**真实情况**：
- `drop_params=False` 时 volcengine provider router 在客户端就抛 `UnsupportedParamsError`，根本不触达豆包网关
- 运行时 `LiteLLMSession.chat` 在传 response_format 时强制 `drop_params=True`（`litellm_adapter.py:368-370`）
- **探测必须复用运行时路径**：`drop_params=True` + `allowed_openai_params=["response_format"]` + response_format，让请求真正发到 provider 网关

### 修正 2：探测结果判定改为"响应内容是否合法 JSON + 含目标字段"，不能只看是否抛异常

**真实结果分布**：
| Provider 行为 | 异常类型 | 响应内容 | 应判定 |
|---------------|---------|----------|--------|
| LiteLLM 客户端拒绝（drop_params=False 时） | `UnsupportedParamsError` | - | 不应出现（探测必须 drop_params=True） |
| Provider 网关拒绝（豆包 Coding Plan） | `BadRequestError` | - | `model_rejected` |
| Provider 接受但模型不遵守（GLM） | 无异常 | 200 + 非法 JSON | `gateway_blocked` |
| Provider 真正支持（OpenAI） | 无异常 | 200 + 合法 JSON 含 ok 字段 | `supported` |

**判定函数修正**：
- Tier 1 (json_schema) 探测：必须 200 + json.loads 成功 + `isinstance(data, dict)` + `"ok" in data` 才算 `supported`
- Tier 2 (json_object) 探测：必须 200 + json.loads 成功 + `isinstance(data, dict)` 才算 `supported`（不要求含 ok 字段，因为 json_object 不约束字段名）
- 任何 tier 抛 `BadRequestError` → 该 tier `model_rejected`，降级下一 tier
- 任何 tier 200 + json.loads 失败 → 该 tier `gateway_blocked`，降级下一 tier

### 修正 3：探测必须直接复用 `LiteLLMSession.chat`，不能直接调 `litellm.completion`

**原方案**：探测绕过 `LiteLLMSession`，直接调 `litellm.completion(**kwargs)` 自构造参数。
**真实情况**：
- 运行时 `LiteLLMSession.chat` 构造的 kwargs 含 `stream=True`、`stream_options`、`temperature`、`provider_params`（含 reasoning_effort）、`drop_params=True` 等
- 探测自构造参数会丢失这些，导致探测和运行时行为不一致
- **修正**：探测必须 `session = LiteLLMSession(cfg=base_llm_config); gen = session.chat(messages=..., response_format=...)` 消费生成器
- `LiteLLMSession.chat` 内部 `drop_params=True` 已对齐运行时，无需额外设置
- 但探测时需要捕获 `BadRequestError`（运行时也有 fallback 捕获）和 `UnsupportedParamsError`（运行时也会抛但被 `BadRequestError` fallback 之外的路径处理）

### 修正 4：升级后旧配置首次启动需引导探测

**真实情况**：
- 豆包 Coding Plan 旧配置含 `allowed_openai_params: ["response_format"]`——但运行时由于 LiteLLM 在传 response_format 时强制 `drop_params=True` + `allowed_openai_params` 透传，请求真正发到豆包网关，触发 `BadRequestError`，现有 fallback 接住走 prompt-only 重试——**功能正常但每次都多一次无效请求**
- GLM 旧配置**完全无 `allowed_openai_params`**——`_resolve_response_format` 返回 None（prompt_only），不构造 response_format，**不会触发 BadRequestError，但也永远不知道 GLM 接受 response_format 后输出漂移**
- **修正**：升级后首次启动若检测到 `lightrag_llm.litellm_kwargs` 无 `response_format_mode` 键，**后台自动触发一次探测**写入新字段（不阻塞启动，不弹窗）

### 修正 5：前端 `probe_failed` 必须原样保留旧 `litellm_kwargs`

**原方案 bug**：probe_failed 时仍写入 `response_format_mode: responseFormatMode`（默认 prompt_only）+ `allowed_openai_params: []`，破坏旧配置。
**修正**：
```js
litellm_kwargs: probeResult.result === 'supported'
  ? { ...existingLightragKwargs, response_format_mode: responseFormatMode, allowed_openai_params: allowedOpenaiParams }
  : { ...existingLightragKwargs }  // probe_failed：原样保留
```

### 修正 6：测试案例必须覆盖两个真实配置

**新增集成测试**：
- `test_probe_doubao_coding_plan_returns_prompt_only` — 用 `config/user-config.json`，期望 `mode == "prompt_only"`（豆包网关 400 + LiteLLM 客户端拒绝都触发降级）
- `test_probe_glm_returns_prompt_only` — 用 `config/user-config - glm.json`，期望 `mode == "prompt_only"`（GLM 接受参数但输出漂移，json.loads 失败触发降级）
- 真实 model 名：豆包是 `ark-code-latest`（不是 `doubao-seed-2.0-pro`），GLM 是 `xopglm5`

## 四、对审查报告 Critical 问题的回应

### C1+C2：探测与运行时 drop_params 不一致 → 修正 1+3 解决
- 探测改用 `LiteLLMSession.chat` 复用运行时路径，drop_params 自动对齐
- 不再绕过 LiteLLMSession 自构造 kwargs

### C3：GLM 升级后无引导探测 → 修正 4 解决
- 新增"首次启动后台探测"Task

### C4：前端 spread 顺序错误 → 修正 5 解决
- probe_failed 原样保留旧值

### I1：探测 prompt 要求过严 → 修正 2 解决
- Tier 2 json_object 不要求含 ok 字段，只要求合法 JSON

### I5：Task 5 缺 GLM 测试 → 修正 6 解决
- 新增 GLM 集成测试 case

## 五、方案 v2 修订方向

基于以上验证，方案 v2 需要：
1. Task 1 `_resolve_response_format` 不变（决策逻辑正确）
2. Task 2 探测端点重写：用 `LiteLLMSession.chat` 复用运行时路径，3 档递进判定改为"响应内容是否合法 JSON + 含目标字段"
3. Task 3 前端 spread 修正：probe_failed 原样保留
4. 新增 Task 2.5：升级后首次启动后台探测
5. Task 5 测试案例覆盖豆包 Coding Plan + GLM 两个真实配置
6. 删除"网关静默剥离返回 200"的错误描述，改为"两种失败模式：网关 400 拒绝 / 网关接受但模型输出漂移"
