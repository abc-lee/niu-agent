# 全项目 Ruff 代码质量修复 — 递归式实施计划（v2）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 清除全项目全部 ruff 诊断，使 `ruff check niu_api/ agent/ tests/` 输出 0 errors。

**Architecture:** 递归式 — 每轮按"诊断→方案→执行→验证"循环。每轮聚焦一批规则，修复后跑 ruff + 全量测试验证，通过后进入下一轮。下一轮开始前重新 `ruff check --statistics` 诊断当前状态。

**Tech Stack:** Python 3.11, ruff 0.16.0, pytest。根目录 `pyproject.toml` 已配置 ruff。

**R1 已完成（2026-07-29）：** `ruff --fix --unsafe-fixes` 修复了 1657 个问题。当前剩 204 个。

**当前状态（post-R1 诊断）：**
```
 73  E402  module-import-not-at-top-of-file   ← R6, 加 noqa
 49  N806  non-lowercase-variable-in-function  ← R7, 重命名
 27  E741  ambiguous-variable-name             ← R7, 重命名 l→item
 15  B904  raise-without-from-inside-except    ← R3, from e / from None
  9  E702  multiple-statements-on-one-line-semicolon ← R8, 拆行
  6  E701  multiple-statements-on-one-line-colon ← R8, 拆行
  6  N802  invalid-function-name               ← R8, noqa (有意 camelCase)
  5  I001  unsorted-imports                    ← R1 残留, 再次 --fix
  4  F401  unused-import                        ← R1 残留, 手动检查
  2  E722  bare-except                          ← R4, → except Exception
  2  F821  undefined-name                       ← R2, 真 bug
  2  N815  mixed-case-variable-in-class-scope  ← R8, noqa (Pydantic API 字段)
  1  B007  unused-loop-control-variable          ← R3, → _ratio
  1  B008  function-call-in-default-argument    ← R2, 去默认值
  1  B027  empty-method-without-abstract-decorator ← R2, pass→...
  1  UP035 deprecated-import                    ← R1 残留, 手动修
总计: 204 errors
```

**每轮的通用验证模板：**
```bash
# 1. ruff 统计 — 确认本轮规则已清零
ruff check niu_api/ agent/ tests/ --statistics

# 2. 全量测试 — 确认无回归
python/bin/python -m pytest tests/ -q --import-mode=importlib --ignore=tests/test_working_memory_removal.py

# 3. 提交
git add -A && git commit -m "<本轮提交信息>"
```

**注意：** `tests/test_working_memory_removal.py` 有 F821 bug（`HANDLER_PATH` 未定义），R2 修复前跳过该文件。`tests/` 下有同名文件冲突（`test_mcp_loader.py`/`test_tool_registry.py` 在 `tests/` 和 `tests/test_p0/` 下各一个），必须用 `--import-mode=importlib`。

---

## Round 2: 真 Bug 修复（F821 + B011 + B027 + B008，~6 个）

**目标：** 修复可能导致运行时错误的代码缺陷。

- [ ] **Step 1: 诊断**
```bash
ruff check --select F821,B011,B027,B008 niu_api/ agent/ tests/ --statistics
```

- [ ] **Step 2: 方案**
  - **F821 未定义名（2 处）：**
    - `tests/test_phase02_lightrag_migration.py:464`：`name` 未定义。查看上下文，如果有正确的循环变量用它替换；否则改为字面量 `"agent"`。
    - `tests/test_working_memory_removal.py:21`：`HANDLER_PATH = os.path.join(PROJECT_ROOT, HANDLER_PATH)` 自引用。改为 `HANDLER_PATH = os.path.join(PROJECT_ROOT, "agent", "handler.py")`。查看 L18-25 确认路径。
  - **B011 assert False（2 处）：**
    - `tests/test_lightrag_resilience_integration.py:18`：`assert False, "..."` → `raise AssertionError("...")`
    - `tests/test_subagent_registry_async.py:123`：同上
  - **B027 空抽象方法（1 处）：** `niu_api/channel/base.py:69`：`pass` → `...`（不改 `@abstractmethod`，因为子类可能未实现 `send_media`）
  - **B008 默认参数调用（1 处）：** `niu_api/brain_region_api.py:145`：`req: ConsolidateRequest = ConsolidateRequest()` → `req: ConsolidateRequest`（去掉默认值，FastAPI 自动从 body 解析 Pydantic 模型，不需要实例化）

- [ ] **Step 3: 执行** — 逐个文件修复，每个改完后 `ruff check --select <rule> <file>` 确认。

- [ ] **Step 4: 通用验证模板** — ruff + 测试（不再跳过 `test_working_memory_removal.py`）+ 提交 `fix: ruff 真 Bug 修复（F821/B011/B027/B008）`

- [ ] **Step 5: 进入 Round 3**

---

## Round 3: 异常链 B904 + 循环变量 B007（~16 个）

**目标：** `raise ... from e` 保留异常链；未用循环变量改名。

- [ ] **Step 1: 诊断**
```bash
ruff check --select B904,B007 niu_api/ agent/ tests/ --statistics
```

- [ ] **Step 2: 方案**
  - **B904（15 处，2 种子类型，必须区分）：**
    - **类型 A — 有 `as e` 绑定（10 处）：** `except Exception as e:` 块内 `raise ...(...)` → 末尾加 ` from e`
    - **类型 B — 无 `as e` 绑定（5 处）：** `except TimeoutError:` 或 `except:` 块内 `raise ...(...)` → 末尾加 ` from None`（不能加 `from e`，会 NameError）

    类型 A 文件：`alerts_api.py:35`、`brain_region_api.py:140,347,379`、`injector.py:90`、`notes_api.py:54,65,93,112`
    类型 B 文件：`chat.py:575`（`except TimeoutError:`）、`compat.py:2768`（`except:` 无绑定）、`compat.py:3462`（同上）、`internal/lightrag_manager.py:1142`（`except ImportError:`）、`llm_proxy.py:381`（`except TimeoutError:`）

  - **B007（1 处）：** `niu_api/internal/region_activation.py:720`：循环变量 `ratio` 未用 → 改为 `_ratio`（保留下划线前缀以示有意未用）

- [ ] **Step 3: 执行** — 可并行派 2 个子 Agent：一个修 B904（按类型 A/B 分别处理），一个修 B007（1 处，简单）。

- [ ] **Step 4: 通用验证模板** — ruff + 测试 + 提交 `fix: B904 raise from e/from None + B007 循环变量（16 处）`

- [ ] **Step 5: 进入 Round 4**

---

## Round 4: Bare except E722（2 处）

**目标：** `except:` → `except Exception:`

- [ ] **Step 1: 诊断**
```bash
ruff check --select E722 niu_api/ agent/ tests/
```

- [ ] **Step 2: 方案**
  - `niu_api/__main__.py:597`：健康检查端点的 bare except（故意吞所有异常）→ `except Exception:`
  - `tests/test_context_overflow_real.py:58`：吞 `requests.post` 连接异常 → `except Exception:`

- [ ] **Step 3: 执行** — 2 处简单替换，派 1 个子 Agent。

- [ ] **Step 4: 通用验证模板** — ruff + 测试 + 提交 `fix: E722 bare except → except Exception（2 处）`

- [ ] **Step 5: 进入 Round 5**

---

## Round 5: R1 残留自动修复（I001 + F401 + UP035，~10 个）

**说明：** F841（原计划 101 处）已被 R1 完全解决，本轮跳过。R1 有少量残留（可能是 `--unsafe-fixes` 需要确认的）。

- [ ] **Step 1: 诊断**
```bash
ruff check --select I001,F401,UP035 niu_api/ agent/ tests/
```

- [ ] **Step 2: 方案**
  - **I001（5 处）：** 再次 `ruff check --fix --select I001` 尝试自动修复。如果有残留，手动排序 import。
  - **F401（4 处）：** 逐个查看——可能是 `# noqa: F401` 被 R1 删了但有副作用的 import，需要恢复。如果确实无用，手动删除。
  - **UP035（1 处）：** 弃用 import（如 `from typing import ...`），手动改为 `from collections.abc import ...` 或 `list[...]`。

- [ ] **Step 3: 执行** — 先跑 `ruff check --fix --select I001,F401,UP035`，查看剩余手动修。

- [ ] **Step 4: 通用验证模板** — ruff + 测试 + 提交 `fix: R1 残留 I001/F401/UP035（10 处）`

- [ ] **Step 5: 进入 Round 6**

---

## Round 6: Import 位置 E402（~73 处，~25 文件）

**目标：** 处理 import 不在文件顶部的问题。

- [ ] **Step 1: 诊断**
```bash
ruff check --select E402 niu_api/ agent/ tests/ --statistics
```

- [ ] **Step 2: 方案** — E402 绝大多数是有意为之（`sys.path.insert` 后才能 import，或 `litellm.model_cost` 设置后才能 import）。修复方式：对每处 import 行末尾加 `# noqa: E402`。按目录分 3 批（agent/ ~3 文件、niu_api/ ~3 文件、tests/ ~19 文件）。

- [ ] **Step 3: 执行** — 可并行派 3 个子 Agent。每个子 Agent 对目录内所有 E402 的 import 行加 `# noqa: E402`。

- [ ] **Step 4: 通用验证模板** — ruff + 测试 + 提交 `style: E402 import 位置加 noqa（73 处）`

- [ ] **Step 5: 进入 Round 7**

---

## Round 7: 变量命名 N806 + E741（~76 个）

**目标：** 函数内大写变量名→小写；模糊变量名 `l`→有意义名。

- [ ] **Step 1: 诊断**
```bash
ruff check --select N806,E741 niu_api/ agent/ tests/
```

- [ ] **Step 2: 方案**
  - **N806（49 处，18 文件）：** 函数内 `MAX_LIMIT` → `max_limit` 等。**验证手段：** 每个 N806 修复后，除了 `ruff --select N806` 外，还需运行该文件相关的单元测试，因为 ruff 只检查语法层面的引用，不捕获 `getattr()` 或字符串形式的变量引用。
  - **E741（27 处，9 文件）：** 变量 `l` → `line`/`item`/`level` 等（根据上下文）。主要集中在 `tests/test_basic_tools.py`（~24 处）和 `agent/handler.py`（3 处）。

- [ ] **Step 3: 执行** — 可并行派 2 个子 Agent（N806 和 E741 各一个）。N806 子 Agent 每个文件修完后跑相关测试确认。

- [ ] **Step 4: 通用验证模板** — ruff + 测试 + 提交 `style: N806 变量小写 + E741 模糊变量名（76 处）`

- [ ] **Step 5: 进入 Round 8**

---

## Round 8: 剩余规则（E701/E702/N802/N815，~23 个）

**说明：** UP 系列（UP031/UP032/UP037/UP017/UP009/UP007）已在 R1 完全解决，本轮无 UP 修复。

- [ ] **Step 1: 诊断**
```bash
ruff check --select E701,E702,N802,N815 niu_api/ agent/ tests/
```

- [ ] **Step 2: 方案**
  - **E701（6 处）：** `tests/test_compact_status_events.py` — `if x: pass` 写在一行 → 拆成多行
  - **E702（9 处）：** `tests/test_ha_automation.py`（5 处）、`tests/test_main_agent_request_queue.py`（2 处）、`tests/test_subagent_memory.py`（2 处）— `import a; import b` 分号 → 拆成多行
  - **N802（6 处）：** 测试函数名用 camelCase（如 `test_disableBaseTools_...`）。**有意为之**——对应被测 API 参数名或分级命名约定。修法：在 `pyproject.toml` 的 `per-file-ignores` 中为 `tests/*` 排除 N802，不加 `# noqa` 以免每个函数都要标。
  - **N815（2 处）：** `niu_api/notes_api.py` 的 `createdAt`/`updatedAt` 是 Pydantic 模型字段，**前端 API 契约**（JSON key 必须是 camelCase）。**不能重命名**。修法：加 `# noqa: N815` 或在 `pyproject.toml` 的 `per-file-ignores` 中为该文件排除 N815。

- [ ] **Step 3: 执行** — 先改 `pyproject.toml` 加 per-file-ignores（N802 for tests、N815 for notes_api），再派子 Agent 修 E701/E702（拆行）。

  `pyproject.toml` 补充：
  ```toml
  [tool.ruff.lint.per-file-ignores]
  "tests/*" = ["DTZ", "N802"]
  "niu_api/notes_api.py" = ["N815"]
  ```

- [ ] **Step 4: 通用验证模板** — ruff + 测试 + 提交 `style: E701/E702 拆行 + N802/N815 noqa 配置（23 处）`

- [ ] **Step 5: 最终验证**

---

## Round 9: 最终验证

- [ ] **Step 1: ruff 零错误确认**
```bash
ruff check niu_api/ agent/ tests/
```
Expected: `All checks passed!`

- [ ] **Step 2: 全量测试通过确认**
```bash
python/bin/python -m pytest tests/ -q --import-mode=importlib
```

- [ ] **Step 3: 提交历史回顾**
```bash
git log --oneline -15
```

- [ ] **Step 4: 完成声明** — 全项目 ruff 诊断 1819 → 0，全量测试通过，无回归。
