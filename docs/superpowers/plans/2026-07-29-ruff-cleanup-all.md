# 全项目 Ruff 代码质量修复 — 递归式实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 清除全项目全部 ruff 诊断，使 `ruff check niu_api/ agent/ tests/` 输出 0 errors。

**Architecture:** 递归式 — 每轮按"诊断→方案→执行→验证"循环。每轮聚焦一批规则，修复后跑 ruff + 全量测试验证，通过后进入下一轮。下一轮开始前重新 `ruff check --statistics` 诊断当前状态（因为前一轮修复可能连带解决或暴露新问题）。

**Tech Stack:** Python 3.11, ruff 0.16.0, pytest。根目录 `pyproject.toml` 已配置 ruff。

**当前状态（2026-07-29 诊断）：**
```
553  I001   unsorted-imports          ← 可自动修复
225  UP006  non-pep585-annotation      ← 可自动修复
203  F401  unused-import              ← 可自动修复
156  UP045 non-pep604-annotation       ← 可自动修复
140  F541  f-string-missing-placeholders ← 可自动修复
101  F841  unused-variable            ← 需手动
 78  E402  module-import-not-at-top    ← 需手动 (noqa)
 55  UP035 deprecated-import          ← 可自动修复
 49  N806  non-lowercase-variable      ← 需手动
 37  UP015 redundant-open-modes        ← 可自动修复
 28  UP041 timeout-error-alias        ← 可自动修复
 28  W292  missing-newline-at-end      ← 可自动修复
 27  E741  ambiguous-variable-name     ← 需手动
 22  F811  redefined-while-unused      ← 可自动修复
 15  B904  raise-without-from          ← 需手动
 12  B007  unused-loop-control-variable ← 需手动
 10  E401  multiple-imports-on-one-line ← 可自动修复
 10  UP017 datetime-timezone-utc      ← 可自动修复
  + 其余小批量规则 ~50 个
总计: 1819 errors (1565 可自动修复, 254 需手动)
```

**每轮的通用验证模板：**
```bash
# 1. ruff 统计 — 确认本轮规则已清零
ruff check niu_api/ agent/ tests/ --statistics

# 2. 全量测试 — 确认无回归
python/bin/python -m pytest tests/ -x -q

# 3. 提交
git add -A && git commit -m "<本轮提交信息>"
```

---

## Round 1: 自动修复（~1565 个）

**目标：** 跑 `ruff --fix --unsafe-fixes`，一次性解决所有可机械替换的规则。

**覆盖规则：** I001, UP006, UP045, F401, F541, UP035, UP015, UP041, W292, F811, E401, UP017, UP009, UP007, B009, UP032, UP037

- [ ] **Step 1: 备份**
```bash
git add -A && git commit -m "chore: ruff 全量修复前备份"
```

- [ ] **Step 2: 自动修复**
```bash
ruff check --fix --unsafe-fixes niu_api/ agent/ tests/
```

- [ ] **Step 3: 重新诊断** — 用通用验证模板跑 `--statistics`，确认可自动修复规则已清零。如果出现新的 ImportError/TypeError（自动修复误删了有副作用的 import），逐个 `git checkout <file>` 恢复并手动保留。

- [ ] **Step 4: 全量测试**
```bash
python/bin/python -m pytest tests/ -x -q
```

- [ ] **Step 5: 提交**
```bash
git add -A && git commit -m "style: ruff 自动修复 ~1565 个代码质量问题"
```

- [ ] **Step 6: 进入 Round 2** — 重新 `ruff check --statistics` 诊断剩余问题。

---

## Round 2: 真 Bug 修复（F821 + B011 + B027 + B008，~6 个）

**目标：** 修复可能导致运行时错误的代码缺陷。

- [ ] **Step 1: 诊断** — `ruff check --select F821,B011,B027,B008 niu_api/ agent/ tests/ --statistics`

- [ ] **Step 2: 方案** — 逐个查看详情，确定修复方式：
  - **F821 未定义名（2 处）：** `tests/test_phase02_lightrag_migration.py:464`（`name` 未定义 → 用字面量或正确变量名）、`tests/test_working_memory_removal.py:21`（`HANDLER_PATH` 自引用 → 补全路径字符串）
  - **B011 assert False（2 处）：** `tests/test_lightrag_resilience_integration.py:18`、`tests/test_subagent_registry_async.py:123` → `raise AssertionError(...)`
  - **B027 空抽象方法（1 处）：** `niu_api/channel/base.py:69` → `pass` 改 `...`
  - **B008 默认参数调用（1 处）：** `niu_api/brain_region_api.py:145` → 查看 FastAPI `Depends()` 模式

- [ ] **Step 3: 执行** — 逐个文件修复，每个改完后 `ruff check --select <rule> <file>` 确认。

- [ ] **Step 4: 通用验证模板** — ruff 统计 + 全量测试 + 提交 `fix: ruff 真 Bug 修复（F821/B011/B027/B008）`

- [ ] **Step 5: 进入 Round 3**

---

## Round 3: 异常链 + 循环变量（B904 + B007，~27 个）

**目标：** `raise ... from e` 保留异常链；未用循环变量改 `_`。

- [ ] **Step 1: 诊断** — `ruff check --select B904,B007 niu_api/ agent/ tests/ --statistics`

- [ ] **Step 2: 方案**
  - **B904（15 处）：** 全在 `niu_api/` 下 8 个文件。模式统一：`except Exception as e:` 块内 `raise HTTPException(...)` → 末尾加 ` from e`。可派子 Agent 批量处理。
  - **B007（12 处）：** 7 个文件。模式统一：未用循环变量名 → `_`。

- [ ] **Step 3: 执行** — 可并行派 2 个子 Agent：一个修 B904（8 文件），一个修 B007（7 文件）。

- [ ] **Step 4: 通用验证模板** — ruff + 测试 + 提交 `fix: B904 raise from + B007 循环变量（27 处）`

- [ ] **Step 5: 进入 Round 4**

---

## Round 4: 集合操作 + 其余小批量 Bug 类（C401/C408/C416/B905/E722，~13 个）

**目标：** 不必要集合操作、zip 缺 strict、bare except。

- [ ] **Step 1: 诊断** — `ruff check --select C401,C408,C416,B905,E722 niu_api/ agent/ tests/`

- [ ] **Step 2: 方案** — 逐个查看上下文确定修复方式（C401→set comprehension、C408→dict 字面量、C416→直接构造器、B905→加 `strict=True`、E722→`except Exception`）。

- [ ] **Step 3: 执行** — 可派 1 个子 Agent 批量修复。

- [ ] **Step 4: 通用验证模板** — ruff + 测试 + 提交

- [ ] **Step 5: 进入 Round 5**

---

## Round 5: 未用变量 F841（~101 个，43 文件）

**目标：** 删除或标记未使用的局部变量。这是手动批次中最大的一个。

- [ ] **Step 1: 诊断** — `ruff check --select F841 niu_api/ agent/ tests/ --statistics`

- [ ] **Step 2: 方案** — 按目录分 3 批：
  - 批 A: `agent/` 下 ~4 文件（生产代码，需谨慎——可能是 `yield from` 赋值未用、或调用结果未用）
  - 批 B: `niu_api/` 下 ~3 文件（生产代码）
  - 批 C: `tests/` 下 ~36 文件（测试代码——通常是调用后赋值但未断言，删赋值或加 `# noqa: F841`）

  修复策略：
  - 无用赋值 → 删除赋值行
  - `result = func()` 未用 → `func()`（直接调用）
  - 有意保留（调试/可读性）→ `# noqa: F841`

- [ ] **Step 3: 执行** — 可并行派 3 个子 Agent，每个处理一个目录批次。每个子 Agent 修复后自己跑 `ruff check --select F841 <目录>` 验证。

- [ ] **Step 4: 通用验证模板** — ruff + 测试 + 提交 `fix: F841 未用变量清理（101 处）`

- [ ] **Step 5: 进入 Round 6**

---

## Round 6: Import 位置 E402（~78 个，28 文件）

**目标：** 处理 import 不在文件顶部的问题。

- [ ] **Step 1: 诊断** — `ruff check --select E402 niu_api/ agent/ tests/ --statistics`

- [ ] **Step 2: 方案** — E402 绝大多数是有意为之（`sys.path.insert` 后才能 import，或 `litellm.model_cost` 设置后才能 import）。修复方式：对每处 import 行末尾加 `# noqa: E402`。按目录分 3 批（agent/ 3 文件、niu_api/ 3 文件、tests/ 22 文件）。

- [ ] **Step 3: 执行** — 可并行派 3 个子 Agent。每个子 Agent 对目录内所有 E402 的 import 行加 `# noqa: E402`。

- [ ] **Step 4: 通用验证模板** — ruff + 测试 + 提交

- [ ] **Step 5: 进入 Round 7**

---

## Round 7: 变量命名 N806 + E741（~76 个）

**目标：** 函数内大写变量名→小写；模糊变量名 `l`→有意义名。

- [ ] **Step 1: 诊断** — `ruff check --select N806,E741 niu_api/ agent/ tests/`

- [ ] **Step 2: 方案**
  - **N806（49 处，18 文件）：** 函数内 `MAX_LIMIT` → `max_limit` 等。需确保同函数内所有引用同步更新。
  - **E741（27 处，9 文件）：** 变量 `l` → `line`/`item` 等。主要集中在 `tests/test_basic_tools.py`（~24 处）和 `agent/handler.py`（3 处）。

- [ ] **Step 3: 执行** — 可并行派 2 个子 Agent（N806 和 E741 各一个）。注意 N806 重命名后需确认同函数内所有引用都更新。

- [ ] **Step 4: 通用验证模板** — ruff + 测试 + 提交

- [ ] **Step 5: 进入 Round 8**

---

## Round 8: 剩余规则（E701/E702/UP031/UP032/UP037/UP017/UP009/UP007/E731/W293/N815/N802，~50 个）

**目标：** 清除最后一波小批量规则。

- [ ] **Step 1: 诊断** — `ruff check niu_api/ agent/ tests/ --statistics` — 确认剩余规则和数量。

- [ ] **Step 2: 方案** — 先尝试 `ruff check --fix --unsafe-fixes --select UP031,UP032,UP037,UP017,UP009,UP007 niu_api/ agent/ tests/` 自动修复 UP 系列。剩余 E701/E702/E731/W293/N815/N802 手动修。

- [ ] **Step 3: 执行** — 先跑自动修复，再派子 Agent 手动修剩余。

- [ ] **Step 4: 通用验证模板** — ruff + 测试 + 提交

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
python/bin/python -m pytest tests/ -q
```

- [ ] **Step 3: 提交历史回顾**
```bash
git log --oneline -15
```

- [ ] **Step 4: 完成声明** — 全项目 ruff 诊断 1819 → 0，全量测试通过，无回归。
