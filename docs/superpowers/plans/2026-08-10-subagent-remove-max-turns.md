# 计划：去掉子 Agent 轮数上限 + 未完成结果游标不推进（R1 修订版）

日期：2026-08-10
状态：R1 双审查员审查完成，已修复 findings，待 R2 复审

## Goal

1. **去掉子 Agent 轮数上限**：`max_turns=None` = 无上限（长程任务跑到底）；主 Agent 默认 40 轮**保持不变**（runner.py 入口）。
2. **未完成结果带标记 + 游标不推进**：子 Agent 因 `MAX_TURNS_EXCEEDED`/`STOPPED`/`TERMINATED_BY_SUPPLEMENT` 终止时，`call_subagent` 返回 incomplete JSON（不再把中间文本当最终结果）；**全库 11 处**游标决策点对 incomplete 结果**不推进游标**（保留进度，下次续做）。

## 背景（2026-08-10 实机日志 + raw_http 实证）

睡眠整理 context-manager 子 Agent 逐条精简 103 条消息，第 20 次 LLM 调用（raw_http 20260810/000031，finish_reason=tool_calls，要求再精简 idx:33/67/71）响应后，`agent_runner_loop` 撞线 `call_subagent` 硬编码 `max_turns=20` → 返回 `{"result": "MAX_TURNS_EXCEEDED"}` → `call_subagent` 后处理只有 LLM_ERROR/length/CONTEXT_OVERFLOW 三分支 → 落 `return last_reply`（中间文本"再精简几个小工具输出：idx:33..."）→ `_tidy_context_impl` 判非 overflow → **游标自动推进到范围末尾（6327de4d=idx:103）** → "压缩没结束但完成了"。idx:33/67/71 未处理但游标已越过。

用户拍板：**子 Agent 是智能体，不需要轮数上限**（工具循环已有重复调用检测等多级保护）；游标误判**一并修**；存量游标：首次回退（6327de4d → 12ba93d6）被 15:18 整理重演覆盖，用户关闭程序后**已再次回退完成**（15:20，12ba93d6，备份 .bak-20260810-1520，见已知取舍 5、R4-2）。

## R1 审查 findings 与修复（双审查员交叉验证，PM 已复核）

| # | 级别 | Finding | 修复 |
|---|---|---|---|
| R1-1 | P0 | **Task 2 改错函数**：`max_turns: int = 20` 属 `_run_agent_loop`（subagent.py L232），`call_subagent`（L762）**无 max_turns 参数**；计划写 `L987/L1029 → max_turns=max_turns` 会在 call_subagent 作用域 NameError 崩溃全部子 Agent 调用 | Task 2 重设计：`call_subagent` 新增 `max_turns: int | None = None` 参数；三路径（resume L940-966 显式 + 异步 L987 + 同步 L1029）透传 `max_turns=max_turns` |
| R1-2 | P0 | **Task 4 严重漏网点**：全库 `if _is_subagent_overflow(x)` 共 **11 处**（compat 7 + runner 3 + handler 1），计划只覆盖 compat 7（且计数写"6"自相矛盾）。漏：runner.py L1297（Nap entity）/L1378（Nap dream）/L1765（_run_subagent_step，force entity/dream/journal 共享）+ handler.py L963（_update_journal_cursor，chat-with journal）→ incomplete JSON 流到漏网点判非 overflow → 兜底推进，重演"没结束但完成" | Task 4 扩到全库 11 处 |
| R1-3 | P0 | **TERMINATED_BY_SUPPLEMENT 漏分支**（双审查员独立发现）：/stop drain 时序（agent_loop.py L1288-1332）返回 `{"result": "TERMINATED_BY_SUPPLEMENT"}` 而非 STOPPED（runner.py `_stop_subagent` 双机制 push is_terminate + terminate_event.set） | Task 3 分支条件扩为 `result in ("MAX_TURNS_EXCEEDED", "STOPPED", "TERMINATED_BY_SUPPLEMENT")`；补测试用例 |
| R1-4 | P1 | **partial_result 幽灵进度**：`_parse_processed_up_to`（compat.py L605）子串正则全文匹配；result_text 累加文本含 `processed_up_to=N` 字面量 → 漏网点误解析幽灵进度；且全量 result_text 入 JSON 放大负载（103 条 tidy 可达百 KB） | partial_result 只用 `last_reply`（不用 result_text），截断 ≤2000 字符；幽灵风险由 Task 4 全修复消除 |
| R1-5 | P1 | **resume 路径漏改**：resume 分支（answer 非 None）调 `_run_agent_loop` 不传 max_turns → 若只改 call_subagent 参数，resume 仍用 `_run_agent_loop` 默认（20）→ 长程任务挂起恢复后仍被掐断 | `_run_agent_loop` 签名默认改 `max_turns: int | None = None`；resume 分支显式传 `max_turns=max_turns`（防未来默认漂移） |
| R1-6 | P2 | Task 4 测试 fixture 引用错：`TestTidyContextImplIntegration` 只测 `_build_incremental_msg_text` 不驱动 `_tidy_context_impl`；正确模式在 test_stop_interruptible.py `_tidy_common_patches`（patch call_subagent_with_auto_answer + store AsyncMock + chat_lock_already_held=True） | Task 4 测试改用 test_stop_interruptible 模式 + 构造非空 compress 区间（空区间下 cm 阶段不跑） |
| R1-7 | P2 | 测试夹具坑：create_client→None 会在 subagent.py L866 `client.backend.stop_check` AttributeError（与 pre-existing 失败同根） | Task 2/3 测试 create_client → Mock()（自动带 .backend） |
| R1-8 | P2 | mode2（L3016-3113）incomplete JSON 无 keep= 行 → 日志误导"completed"再报"No keep="；force-cm（L3714-3823）天然 fail-loud 安全（JSON 单行无 keep= 行首 → ValueError → 不推进） | mode2 入口加 incomplete 短路（日志清晰）；force-cm 盘点注明不改 |
| R1-9 | P2 | 异步完成通知误报：`_run_subagent_async`（L1504-1510）用 last_reply 拼"[名] 已完成"——incomplete 时误报 | 文案区分：incomplete 时"未完成（被停止/轮次耗尽）" |
| R1-10 | P2 | chat-with 主 Agent 收到裸 JSON（handler.py L1102） | incomplete JSON 转自然语言提示（如"子Agent未完成任务（被停止）"）再返回 LLM；顺带在 handler 改动点 L963 附近处理 |

## 设计决策（修订）

- **max_turns=None = 无上限**：`agent_runner_loop` 循环条件 `while handler.max_turns is None or turn < handler.max_turns`；`_run_agent_loop` 默认 None；`call_subagent` 新增参数默认 None 并透传三路径。显式传小值（测试用 1/2/5）行为不变（仍触发 MAX_TURNS_EXCEEDED）。
- **incomplete JSON 契约**（call_subagent 后处理，**LLM_ERROR 分支之后、finish_reason=length 分支之前**——防 TERMINATED_BY_SUPPLEMENT+finish_reason=length 被 length 分支抢先拦截，R2-3/R4-1）：
  ```json
  {"incomplete": true, "agent": "<name>", "reason": "MAX_TURNS_EXCEEDED|STOPPED|TERMINATED_BY_SUPPLEMENT", "partial_result": "<last_reply 截断≤2000>"}
  ```
  `json.dumps` 字符串返回（与 LLM_ERROR/COMPACT_TRUNCATED/CONTEXT_OVERFLOW 字符串约定一致）；JSON 单行无行首 keep=/update=（天然不进 mode2 解析）。
- **`_is_subagent_incomplete(result)`**（compat.py，放 `_is_subagent_overflow` 附近）：解析 JSON，`data.get("incomplete") is True`。纯文本/overflow JSON 均 False。
- **全库 11 处游标决策点**加 `or _is_subagent_incomplete(x)`（详见 Task 4 清单）。
- **handler.py**：`_update_journal_cursor`（L963）加 incomplete 判断 + chat-with（L1102 区域）incomplete JSON 转自然语言。
- 主 Agent（runner.py L3005/L3099 直连 agent_runner_loop，默认 40）**零改动**。

## 已知取舍

1. 主 Agent 40 轮保留（用户只要求子 Agent 无上限）。
2. STOPPED/TERMINATED_BY_SUPPLEMENT 也带 incomplete（用户 /stop 打断时游标不推进，下次续做）。
3. 无上限后防失控依赖：stop_predicate（/stop 穿透三检查点）+ 上下文溢出保护 + 重复工具调用检测注入。
4. force-cm（L3714-3823）天然 fail-loud，不改。
5. **存量游标已回退（2026-08-10 15:20 完成，R4-2 实证）**：程序关闭窗口内回退 `12ba93d6`（idx:32），备份 `last_compress.json.bak-20260810-1520`（含 buggy 6327de4d@15:18）；**实施后仅需验证**回退仍保持（用户已承诺修复完成前不打开程序）
6. **journal 重复处理**（R3-10）：incomplete → journal 游标不推进 → 下次 chat-with journal 重跑同范围，已部分落库条目可能重复——保守不推进 > 误推进（重复条目幂等可接受）。
7. **incomplete 提示不强制重试**（R3-10）：主 Agent 收到"子Agent未完成任务（reason）"后自行决策（next_prompt 为空不强制）。

## R2 审查 findings 与修复（双复审员交叉验证，PM 已复核）

| # | 级别 | Finding | 修复 |
|---|---|---|---|
| R2-1 | P0 | **handler 转换顺序未钉死**（B P0-1 / A P1-2 同一问题）：`_call_subagent_gen` 当前顺序 = L1102 call_subagent → L1112 SUBAGENT_ERROR → L1116 COMPACT_TRUNCATED → **L1122 `_update_journal_cursor(result)` 用原始 result** → L1189 StepOutcome(result)。若 L1102 区域"就地转自然语言"改写了 result 变量 → L963 收到纯文本 → `_is_subagent_incomplete` False → else 兜底 `journal_msg_ids[-1]` → journal 游标完整推进，原 bug 原样复现 | Task 4 钉死顺序：①`_update_journal_cursor` 必须用**原始 result**（L1122 不动）；②incomplete JSON → 自然语言转换只作用于**返回 LLM 的副本**（L1122 之后、StepOutcome 之前，如 `display_result = _incomplete_to_text(result) if ... else result`）；③集成断言「journal-agent + mock incomplete JSON → last_journal.json 不写入」 |
| R2-2 | P1 | **Nap dream 1/3 fallback 与 or 化机械指令冲突**（双审查员独立确认）：runner.py L1378-1383 `if _is_subagent_overflow(dream_result): [日志] if len(dream_msg_ids) > 10: new_dream_id = dream_msg_ids[len//3]`——fallback 嵌在 overflow 分支体内，`or` 化后 incomplete 也触发 1/3 推进（Nap 是 program 源，/stop terminate 恰是打断场景） | Task 4 该处**重构为三分支机制**（非注释）：`if _is_subagent_overflow(...): <原 1/3 fallback 原样> elif _is_subagent_incomplete(...): logger.warning("...incomplete, cursor not advanced"); new_dream_id = last_dream_id else: <processed_up_to 解析>` |
| R2-3 | P1 | **length 分支抢先拦截**（B P1-2 / A P2-1）：TERMINATED_BY_SUPPLEMENT 返回含 `finish_reason: summary_response.finish_reason`（agent_loop.py L1330）；终止总结截断（length）时，先行的 length 分支（subagent.py L1085-1090）返回 COMPACT_TRUNCATED 而非 incomplete JSON → 11 处决策点判非 incomplete → 兜底推进 | Task 3 分支移到 **length 分支之前**（LLM_ERROR 之后立即）；补测试：return_value={result: TERMINATED_BY_SUPPLEMENT, finish_reason: "length"} → 断言返 incomplete JSON 而非 COMPACT_TRUNCATED |
| R2-4 | P2 | Nap 测试"复用既有模式"是空头支票：tests/ 无 nap 专用测试文件，Nap 逻辑嵌 `_nap_impl` 后台线程依赖 LightRAG/`_ensure_session_chain`，集成成本高 | Task 4 测试聚焦：compat cm 集成（test_stop_interruptible 模式）+ handler journal 集成；runner Nap 两处靠 `_is_subagent_incomplete` 单测 + 代码审查覆盖 |
| R2-5 | P2 | `_tidy_common_patches` 含 `_build_incremental_msg_text→""`（区间恒空 → cm 阶段不跑），直接复用断言落空；且 entity/dream/journal/cm 四阶段同跑都写各自游标 | 新测试**去掉 `_build_incremental_msg_text` patch**（用真函数 + cursor="" + `_tidy_messages()` 2 条 + _FakeCalc 100 token/条 + window 8000 → usage <50% 走模式一且 <70% 不 skip，范围非空）；断言按 `_write_cursor_with_lock` call args 过滤 compress 游标（忽略 entity/dream/journal） |
| R2-6 | P2 | `_run_subagent_async`（L1504-1510）通知拼"[名] 已完成"用 `_last_reply`——incomplete 时 _last_reply 非空（中间文本），不能靠 last_reply 判 | 通知判断基于 **result**：`_is_subagent_incomplete(result)`（函数级 `from niu_api.compat import _is_subagent_incomplete`，subagent.py 顶层无 compat import 防循环依赖）；incomplete → 文案"未完成（被停止/轮次耗尽）" |
| R2-7 | P2 | test_subagent_overflow.py 既有 create_client→None 测试未盘点（L62/L386/L418，L866 backend 坑同根） | 实施前先跑 test_subagent_overflow.py 基线，确认既有用例现状；新测试用 Mock() client |
| R2-8 | P2 | 计划行号微漂移：Task 2 引用 L987/L1029 实为 L980/L1022；resume 分支 L938-968 | 按实际代码锚点实施 |
| R2-9 | P2 | 备份文件未见（INFERENCE） | 实施时核对 ~/.niu/.bak-20260810-1442 存在（ls -la 含隐藏） |

## R3 审查 findings 与修复（双复审员交叉验证，PM 已复核）

| # | 级别 | Finding | 修复 |
|---|---|---|---|
| R3-1 | P1 | **R2-8 行号"修正"本身错误（PM 被带偏一次，已实证纠正）**：R2-A 称"L987/L1029 实为 L980/L1022"——grep/sed 实证 **L987（异步分支）/L1029（同步分支）恰为 `max_turns=20` 行**，L980/L1022 是 registry 回填/terminate_event 赋值 | **撤销 R2-8**；Task 2 正文锚点 L987/L1029 正确，恢复；resume 分支按实码 L938-941（无 max_turns 传参，需补显式传参） |
| R3-2 | P1 | **Nap dream 三分支 else 规格省略 range-end 兜底**：runner.py L1390-1393 else 分支除 processed_up_to 解析外还有 range-end 兜底（`elif dream_msg_ids: new_dream_id = dream_msg_ids[-1]`）；计划字面实现会使正常路径游标行为静默改变 | Task 4 规格：else 分支 = **原 else 全量原样**（解析 + 兜底），只把 `if overflow:` 改三分支 |
| R3-3 | P1 | **存量游标回退前提失效（15:18 实况击穿）**：`~/.niu/last_compress.json` 当前 = 6327de4d（buggy 值），last_compress_at=15:18:18——代码修复未实施前每次整理都会重演 bug 推进；备份 `.bak-20260810-1442` 已不在目录 | **游标回退挪到实施完成后最后做**（Task 4 之后）：重做备份 + 回退 12ba93d6 + 验证；在此之前不再回退（会被再次覆盖） |
| R3-4 | P1 | **cm 集成测试可能空洞通过**：`_tidy_context_impl` 直接读真实游标文件（compat.py L2638-2645），本环境真实游标存在 → 增量区间空 → cm 阶段跳过 → 断言恒真 | Task 4 集成 fixture **额外 patch 四个游标文件 READ**（entity/dream/journal/compress 全部强制 cursor=''），断言才有判别力 |
| R3-5 | P2 | handler 锚点漂移：游标调用实为 L1126（非 L1122）、StepOutcome 实为 L1164（非 L1189）；display_result 需覆盖 L1163 tool_marker | Task 4 按实码锚点 L1126/L1163/L1164 |
| R3-6 | P2 | `_run_subagent_async` incomplete 短路须在内容选择（`_last_reply` 优先）**之前** | Task 4 通知块：先判 incomplete（基于 result），再选内容 |
| R3-7 | P2 | `_is_subagent_incomplete` 的 from-import 追加未显式列出（runner.py L1250、handler.py L937 import 块） | Task 4 列出 import 修改点 |
| R3-8 | P2 | Task 1 测试锚点 L189-217 略偏（实为 L180-223） | 按实码 |
| R3-9 | P2 | R2-5 新测试若改共享 fixture `_tidy_common_patches`，既有 test_sleep_tidy_stop_aware_false/test_force_tidy_stop_aware_true 隐性耦合 | **新建独立 fixture**，共享 `_tidy_common_patches` 不动 |
| R3-10 | P2 | journal 重复处理未论证：incomplete → journal 游标不推进 → 下次 chat-with journal 重跑同范围，已部分落库条目可能重复；主 Agent 收到"子Agent未完成任务"时 next_prompt='' 无强制重试 | 已知取舍补两条：①journal 重复处理 = 保守不推进 > 误推进（重复条目幂等可接受）②incomplete 提示不强制重试（由主 Agent 自行决策） |
| R3-11 | P2 | runner L1297/L1765 or 化后 incomplete 打"overflow: 0 turns"日志误导（计划只为 compat 7 处区分日志） | runner/handler 分支内也补 incomplete 日志区分（reason） |
| R3-12 | P2 | STOPPED 首轮空 last_reply 边界未点名 | Task 3 补一例断言 partial_result='' |
| R3-13 | P2 | `_is_subagent_incomplete` 严格性测试缺 `{"incomplete": false}` 与 `{"incomplete": "true"}` | 单测补两例钉住 `is True` 严格判定 |
| R3-14 | P2 | mode2 短路插入点未定 | Task 4：短路放 COMPACT_TRUNCATED 检查后、`_strip_analysis` 前 |

## R4 审查 findings 与修复（双复审员，PM 已复核）

| # | 级别 | Finding | 修复 |
|---|---|---|---|
| R4-1 | P1 | **计划内部矛盾——incomplete 分支位置两处规格冲突**：设计决策节"CONTEXT_OVERFLOW 之后、extract 之前"（R1 初稿残留）vs Task 3 节"LLM_ERROR 后、length 前"（R2-3 正确）；实码顺序 LLM_ERROR→length→CONTEXT_OVERFLOW→extract，按设计决策节实现会落在 length 之后 → TERMINATED_BY_SUPPLEMENT+length 被抢先 → R2-3 原样复现 | 设计决策节同步为"LLM_ERROR 之后、finish_reason=length 之前"（已改） |
| R4-2 | P1 | **存量游标状态过时（方向相反）**：计划 R3-3/取舍5/约束节称"当前=6327de4d、备份丢失、回退待做"——实测当前=12ba93d6（人工回退已完成，15:20）、备份在 .bak-20260810-1520 | 计划全部更新为"已回退完成，实施后仅需验证保持"（已改） |
| R4-3 | P2 | CURRENT_TASK_DONE 负例测试缺失（防正常完成误中 incomplete 分支） | Task 3 测试补负例：mock return_value={result: CURRENT_TASK_DONE} → 不返 incomplete JSON |
| R4-4 | P2 | R3-12 partial_result='' 断言未落入 Task 3 规格 | Task 3 测试补：STOPPED 首轮空 last_reply → partial_result='' |
| R4-5 | P2 | runner L1297/L1765 incomplete 日志区分未落入规格 | Task 4 规格补：runner/handler 分支内 incomplete 日志（reason） |
| R4-6 | P2 | cm 集成断言丢弃 entity/dream 同款信号 | Task 4 集成断言同时覆盖：mock entity/dream/journal 也为 incomplete 时各自游标不动（或注明 cm 为主、其余靠单测+审查） |
| R4-7 | P2 | journal"不写入"断言非 hermetic（本环境 last_journal.json 已存在） | 断言基于 mock 的 `_write_cursor_with_lock` call args 过滤 journal 游标（不依赖真实文件） |
| R4-8 | P2 | 'runner Nap 两处'漏第三处 L1765 | 覆盖点表述：runner 三处（L1297/L1378/L1765） |
| R4-9 | P2 | 回归豁免清单可能不完整：create_client→None/FakeClient-pass 类 call_subagent 测试 L866 崩溃超出 12 项清单 | 实施前跑 test_subagent_overflow/test_compress_quality 基线，新失败独立复现确认 pre-existing 后记入豁免 |
| R4-10 | P2 | 新 fixture 隐性依赖 _read_protect_recent_count→0 等 4 个 patch | Task 4 测试节注明独立 fixture 所需 patch 清单 |

## Task 分解（TDD，每 Task 独立 commit + spec/quality 双审）

### Task 1：agent_loop.py 支持 max_turns=None（无上限）
文件：`agent/generic/agent_loop.py`
- L642 签名 `max_turns=40` → `max_turns: int | None = 40`
- L750 `while turn < handler.max_turns:` → `while handler.max_turns is None or turn < handler.max_turns:`
- L740 赋值不变；L1375 MAX_TURNS_EXCEEDED 返回保留；L759/L1064/L1137 STOPPED、L1288-1332 TERMINATED_BY_SUPPLEMENT 不动
测试（挂 `tests/test_agent_loop_return_messages.py`，复用 L196-226 既有模式 `test_return_value_contains_messages_on_max_turns_exceeded`：`_make_client` + dispatch_loop 续环）：
- 新增：`max_turns=None` 时连续 24 轮 tool_calls + 第 25 轮纯文本 → 自然退出（CURRENT_TASK_DONE），断言不触发 MAX_TURNS_EXCEEDED
- 既有 `test_return_value_contains_messages_on_max_turns_exceeded`（显式 max_turns=2）必须仍绿

### Task 2：_run_agent_loop 默认 None + call_subagent 参数透传
文件：`agent/subagent.py`
- L232 `_run_agent_loop` 签名 `max_turns: int = 20` → `max_turns: int | None = None`（默认无上限；resume 继承）
- L762 `call_subagent` **新增参数** `max_turns: int | None = None`（放签名尾部，docstring 同步）
- 三路径：resume 分支（L940-966 区域）显式传 `max_turns=max_turns`；L987（异步）`max_turns=20` → `max_turns=max_turns`；L1029（同步）同
- call_subagent_with_auto_answer（L1094）**kwargs 自动透传，不改
测试（挂 `tests/test_subagent_overflow.py` 或新文件，create_client → Mock() 防 L866 backend 坑）：
- call_subagent 默认把 `max_turns=None` 传给 `_run_agent_loop`（mock 捕获 kwargs）
- 显式传 5 → 透传 5
- resume 路径（answer 非 None）透传 `max_turns=None`

### Task 3：call_subagent 后处理补 incomplete JSON 分支
文件：`agent/subagent.py`
- **在 LLM_ERROR 分支之后、finish_reason=length 分支之前**插入（R2-3，防 TERMINATED_BY_SUPPLEMENT+length 双重命中）：
  ```python
  # 未完成终止（轮次耗尽 / 被停止 / supplement 终止）：返回结构化报告，
  # 避免中间文本被调用方误判为成功（游标误推进）。优先于 finish_reason=length
  # 判断——终止总结截断时仍必须带 incomplete 标记。
  if return_value and isinstance(return_value, dict) and return_value.get("result") in (
      "MAX_TURNS_EXCEEDED", "STOPPED", "TERMINATED_BY_SUPPLEMENT",
  ):
      _partial = (last_reply or "")[:2000]
      report = {
          "incomplete": True,
          "agent": agent_name,
          "reason": return_value.get("result"),
          "partial_result": _partial,
      }
      logger.warning(f"[SubAgent] {agent_name}: {return_value.get('result')} — task incomplete")
      return json.dumps(report, ensure_ascii=False)
  ```
测试：mock `_run_agent_loop` 返回三种 result（MAX_TURNS_EXCEEDED/STOPPED/TERMINATED_BY_SUPPLEMENT）→ 均返回含 `incomplete: true` + 正确 reason 的 JSON；**边界：{result: TERMINATED_BY_SUPPLEMENT, finish_reason: "length"} → incomplete JSON 而非 COMPACT_TRUNCATED**（R2-3）；**负例：{result: CURRENT_TASK_DONE} → 不返 incomplete JSON**（R4-3）；**STOPPED 首轮空 last_reply → partial_result=''**（R4-4）；partial_result 截断 ≤2000；既有 overflow/LLM_ERROR/length 分支测试仍绿

### Task 4：全库 11 处游标决策点 incomplete 不推进
文件：`niu_api/compat.py`、`agent/runner.py`、`agent/handler.py`、`agent/subagent.py`（通知文案）
- compat.py 新增 `_is_subagent_incomplete(result: str) -> bool`（放 `_is_subagent_overflow` L40 之后）
- **全库 11 处**游标决策点改造（分支内 incomplete 日志区分 reason，R4-5）：
  - compat.py 7 处（L2697/L2780/L2862/L3279/L3453/L3535/L3617）：`if _is_subagent_overflow(x) or _is_subagent_incomplete(x):` → 游标不动，分支内日志区分（incomplete 打 reason）
  - runner.py L1297（Nap entity）与 L1765（_run_subagent_step，force 三步共享）：`or _is_subagent_incomplete(x)` → 不动，**分支内 incomplete 日志区分（reason）**（R5-B-P2-1）
  - **runner.py L1378（Nap dream）三分支重构（R2-2 + R3-2）**：`if _is_subagent_overflow(dream_result): <原 1/3 fallback 原样> elif _is_subagent_incomplete(dream_result): new_dream_id = last_dream_id + warning（reason） else: <原 else 全量原样：processed_up_to 解析 + range-end 兜底>`——fallback 必须 overflow 专属；else 兜底不得省略
  - handler.py L963（_update_journal_cursor）：`if _is_subagent_overflow(journal_result) or _is_subagent_incomplete(journal_result):` → 不动，分支内 incomplete 日志区分（R3-11）
- **handler.py 顺序钉死（R2-1 + R3-5 锚点修正）**：L1126 `_update_journal_cursor` 保持用**原始 result**；incomplete JSON → 自然语言转换只作用于返回 LLM 的副本（L1126 之后，**覆盖 L1163 tool_marker 显示**，StepOutcome L1164 用转换副本）
- subagent.py `_run_subagent_async`（L1504-1510）通知文案（R2-6 + R3-6）：**先判 incomplete（基于 result，函数级 import `_is_subagent_incomplete`）**，再选内容（_last_reply 优先）；incomplete → "未完成（被停止/轮次耗尽）"
- import 追加（R3-7 + R4-A-P1）：runner.py 顶部 L1250 import 块 + **`_run_subagent_step` 函数级 import 块（L1739-1742，L1765 决策点所在函数自身，漏则 NameError）** + handler.py L937 import 块 + compat 内部同模块免 import；subagent.py 函数级 `from niu_api.compat import _is_subagent_incomplete`（防循环依赖）
- mode2 入口（compat.py L3016 区域）加 incomplete 短路：**放 COMPACT_TRUNCATED 检查后、`_strip_analysis` 前**（R3-14），日志"Mode-2: context-manager incomplete, compression skipped"
测试：
- `_is_subagent_incomplete` 单测（R3-13）：incomplete JSON→True；`{"incomplete": false}`→False；`{"incomplete": "true"}`→False；overflow JSON→False；纯文本→False；畸形 JSON→False
- 集成（新建独立 fixture，**不动 `_tidy_common_patches`**，R3-9 + **patch 清单显式化（R4-10）：四个游标文件 READ 强制 cursor=''、`_read_protect_recent_count`→0 等 4 个 patch**）：基于 test_stop_interruptible `_tidy_common_patches` 模式复制为独立 fixture + **额外 patch 四个游标文件 READ（entity/dream/journal/compress 全部 cursor=''，R3-4）** + 去掉 `_build_incremental_msg_text` patch（真函数 + `_tidy_messages()` 2 条 + _FakeCalc 100 token/条 + window 8000 → 模式一非空范围）；mock `call_subagent_with_auto_answer` 返回 incomplete JSON → 断言 `_write_cursor_with_lock` 未被以新 compress id 调用（**按 call args 过滤，忽略 entity/dream/journal 游标写**，R4-7）；**entity/dream/journal 同款 incomplete 信号时各自游标不动也断言**（R4-6）
- handler journal 集成：journal-agent + mock incomplete JSON → **断言基于 mock `_write_cursor_with_lock` call args（journal 游标未写），不依赖真实 last_journal.json**（R4-7）
- runner 三处（L1297/L1378/L1765，R4-8）：`_is_subagent_incomplete` 单测 + 代码审查覆盖（R2-4）；分支内 incomplete 日志区分（R4-5）

## 约束（沿用项目惯例）

- 不改主 Agent 路径（runner.py 主 Agent 入口、agent_loop 默认 40 保留）
- 不跑全量 pytest / 真实 LLM；只跑涉及测试文件（test_agent_loop_return_messages / test_subagent_overflow / test_stop_interruptible / test_tidy_cursor / test_on_before_llm_callback 等）
- `python/bin/python` 跑测试；`/usr/local/bin/ruff` 只修本次引入问题（agent_loop.py 既有 I001/UP035/N806 不修）
- git add 指定文件（禁 -A）；每 Task 独立 commit（main 分支直接提交，项目惯例）
- 已知 12 个 pre-existing 测试失败（AGENTS.md 豁免清单）+ test_tidy_cursor 4 个 PROTECTED 断言失败——与本次改动无关，不得视为新失败；改动后独立复现确认
- 存量游标已回退完成（15:20：备份 .bak-20260810-1520 + 回退 12ba93d6）；实施后验证保持即可（用户承诺修复完成前不打开程序）

## 审查记录

- **R1（2026-08-10，双审查员异角度）**：❌ 2 P0 + 3 P1 + 5 P2（R1-1 至 R1-10 表）。审查员 A 独立发现 Task 2 改错函数（call_subagent 无 max_turns 参数→NameError）与 Task 4 漏 4 网点（runner 3 + handler 1，全库 11 处）；审查员 B 独立发现 TERMINATED_BY_SUPPLEMENT 漏分支（/stop drain 时序）与 resume 路径继承 20 上限。双审查员在 handler journal 游标漏网（P0）上独立交叉确认。PM 亲自复核：call_subagent L762 无 max_turns 参数 ✅、全库 `if _is_subagent_overflow(` 恰 11 处（compat 7 + runner 3 + handler 1）✅、mode2 keep= 解析失败走 error 分支不删不更新（force-cm fail-loud 确认）✅、_parse_processed_up_to L605 子串正则 ✅。全部采纳，修复见上表。
- **R2（复审，双复审员）**：❌ 1 P0 + 3 P1 + 6 P2（R2-1 至 R2-9 表，其中 R2-2/R2-3 双审查员独立交叉确认）。R1 三大 P0 修复验证到位（Task 2 参数透传无 NameError、11 处清单精确、TERMINATED_BY_SUPPLEMENT 分支插入点正确）。R2-1（handler 转换顺序）B 标 P0 / A 标 P1-2 同一问题——`_update_journal_cursor` L1122 用原始 result，转换必须只作用于返 LLM 副本；R2-2（Nap dream 1/3 fallback 嵌 overflow 分支体）双审查员独立确认，需三分支机制重构；R2-3（length 分支抢先拦截 TERMINATED_BY_SUPPLEMENT+finish_reason=length）双审查员独立确认，incomplete 分支需移到 length 之前。PM 复核：handler L1102→L1122→L1189 顺序实证 ✅、runner L1378-1383 fallback 嵌套实证 ✅、agent_loop L1330 finish_reason 字段实证 ✅。全部采纳，修复见 R2 表 + Task 3/4 规格。
- **R3（复审，双复审员）**：❌ 2 P1 + 13 P2（R3-1 至 R3-14 表）。R3-A 抓出 **R2-8 行号"修正"本身错误**（grep/sed 实证 L987/L1029 恰为 max_turns=20 行，L980/L1022 是 registry 回填/terminate_event——PM 曾采纳 R2-A 的错误修正，R3-A 纠正，撤销 R2-8）、Nap dream else 省略 range-end 兜底（R3-2）、**存量游标回退前提失效**（R3-3，15:18 实况：6327de4d 再次写入、备份丢失）。R3-B 抓出 cm 集成测试空洞通过风险（R3-4，_tidy_context_impl 读真实游标文件致区间空）、共享 fixture 隐性耦合（R3-9）等。PM 复核：L987/L1029 内容实证 ✅、last_compress.json=6327de4d/@15:18 ✅（用户确认=程序再次睡眠压缩重演）、compat L2638-2645 真实游标读取 ✅。《已知取舍》补 3 条。全部采纳。
- **R4（复审，双复审员）**：❌ 1 P1 + 1 P1（R4-1/R4-2）+ 8 P2（R4-3 至 R4-10）。R4-A：零 P0，1 P1（runner.py L1739-1742 `_run_subagent_step` 函数级 import 漏列——按计划字面实施 L1765 将 NameError）。R4-B：零 P0，2 P1（R4-1 计划内部规格矛盾——设计决策节 vs Task 3 节 incomplete 分支位置冲突，按旧节实现 R2-3 复现；R4-2 存量游标状态过时——实测当前已=12ba93d6 人工回退完成、备份在 .bak-20260810-1520）。PM 复核：实码分支顺序 LLM_ERROR→length→CONTEXT_OVERFLOW→extract ✅、last_compress.json=12ba93d6（无 at 字段，人工写入）✅、runner L1739-1742 import 块存在 ✅。全部采纳，修复见 R4 表 + Task 3/4 规格。**R4 有 P1 → 不满足零 bug，修复后 R5 复审。**
- **R5（终审，双复审员）**：✅ 零 P0、零 P1（第 1 轮零 bug）。R5-A 机械层 4 审查点全过（锚点逐行核实、R4-1 双节统一、负例/partial_result='' 断言准确、L1765 import 追加无 NameError）；R5-B 逻辑链 5 审查点全过（组合矩阵闭环、测试完备、回归零破坏、存量游标实机确认、mode2/force-cm fail-loud）。P2×5（runner L1297/L1765 日志区分落入正文、Task 1 测试锚点 L196-226、R2-1 措辞 L1126、L1280 drain 锚点）已修。
- **门禁口径（R5-B 提醒，PM 拍板）**：R4 的 2 P1 属文档/状态类（R4-1 规格矛盾、R4-2 状态过时）已实证解决，但严格"连续两轮零 bug"要求 R4 不算零 bug 轮 → R5 构成第 1 轮 → **派 R6 复审**达成连续两轮。
- **R6（复审，双复审员）**：✅ 零 P0、零 P1（第 2 轮零 bug）→ **R5+R6 连续两轮零 bug，达成交付条件，计划可进入实施**。R6-A 机械层全锚点逐行核实（5 生产文件 + 3 测试文件）、11 处计数精确、R4-1 双节一致、R5 修复 3 项落正文；R6-B 逻辑链 5 审查点全过（组合矩阵闭环、fixture 数学验证 200/8000=2.5% 模式一、journal hermetic、回归零破坏、存量游标实机确认）。P2×4 非阻塞（R2-1 行措辞 L1122 未同步 L1126、L1280 记录措辞、fixture 打桩机制 Path.exists→False、通知附 partial_result 可选）。

**计划审查总结**：R1（2P0+3P1+5P2）→ R2（1P0+3P1+6P2）→ R3（2P1+13P2）→ R4（2P1+8P2）→ R5（零 P0/P1 + 5P2）→ R6（零 P0/P1）。连续两轮零 bug 达成。PM 全程复核审查员结论（实证：L987/L1029 行号之争 R3-A 纠正 R2-A、11 处计数、mode2/force-cm 行为、存量游标实况），纠正被带偏一次（R2-8）并记录。
