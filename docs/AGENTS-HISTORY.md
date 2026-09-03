# AGENTS.md 工程历史归档（AGENTS-HISTORY）

本文件 = AGENTS.md 工程日志的完整历史归档：智能压缩移出条目的**原文逐字保留**，主文档（AGENTS.md）仅保留近期工程与仍在引用的终态（原样节）与压缩索引行。

- **用途**：查旧工程 / 旧 commit 链请在本文件 grep（`docs/AGENTS-HISTORY.md`）或 `git log -- AGENTS.md`。
- **追加规则**：后续压缩把被移节原文按原 `### 日期` 格式 append 到本文件末尾，不截断、不改写、不摘要。
- **主文档现行保留区**：见 `AGENTS.md`（含「## 工程历史归档」指针节）。

---

### 2026-09-01

#### 新增：图谱详情面板关联实体右键进子图（commit d045fb7b，用户实机验证通过）

- **需求**：右侧详情面板关联实体列表此前只有左键（主图闪烁定位），右键无功能；补右键 = 以该实体为中心重绘扩散图（等同主图实体右键）
- **实现**：renderer.js showDetail 的 .relation-item forEach 内与 click 并列加 contextmenu 监听（+13 行）——主图态 Document 守卫后 enterSubgraph(id, 1) + updateSubgraphControls()；子图态 enterSubgraph(id, _subgraphDepth) 重建中心；严格对齐主图 onNodeRightClick 写法
- **注意**：面板场景从 currentData.nodes 取原始节点判 nodeType（无 _originalData 包装）——force-graph 回调节点才需要 _originalData 层级，两处数据形态不同勿混
- **验证**：playwright mock 8 断言全过（脚本 /tmp/graph-rctx-test/test.mjs 可复跑）+ 用户实机关窗重开验证通过；renderer 改动关窗重开即生效

#### 工程：定时任务第三种类型 task_kind='subagent'——子 Agent 静默执行 + @end report 反馈通道（计划 v1.0→v2.6 八轮双审门禁 + SDD T1-T6，main 2147f9a5..90a48eb7 共 8 commits）
- **背景（用户质疑）**：journal-daily 直调子 Agent 是为一个子 Agent 做的特殊硬编码（写死 agent 名/任务文本/开关/锁）——「既然做这个逻辑，就应该放到台面上来，变成一种专属的定时任务调用方法」
- **三类型并列（用户定案）**：①reminder=主 Agent 执行 ②background_script=后台脚本执行 ③subagent=子 Agent 静默执行；主 Agent 可自己 schedule_task 创建 subagent 类任务（task_kind + agent_name 参数，agent 存在性双目录校验 config/agents/+~/.niu/agents/）
- **report 反馈通道（用户拍板替代 @niu-agent 挂起方案）**：`汇报正文 @end {"report": "内容"}`——**例外通道非每次必带**，默认全程静默（结果落日志），仅子 Agent 遇到解决不了的问题才用；提取=exit_content 尾部锚定（先剥降级标注行 `[^\n]*` 字符类——reason 可含 `]`，再尾部 JSON/宽松正则，失败静默）；送达主 Agent 格式 `[后台任务「{task_label}」结束报告] {report}`（task_label=name or content[:20]，明确标注非用户消息——用户要求）；单向通知无挂起无接续
- **隔离三层（用户拍板）**：①工具面：frontmatter `visibility: hidden`（复用 MCP 既有标志不新开发）→ runner get_tools_schema 跳过——只挡 chat-with 注册不挡程序直调；②提示词：report 教学只在后台子 Agent md（普通子 Agent 不知道该语法——这一类子 Agent 不能与其他子 Agent 共用，用户拍板）；③语义：送达前缀与普通 [定时任务] 区分
- **journal 迁移**：新建 config/agents/journal-daily-agent.md（hidden，复用 journal-agent 整理协议、删交互判据与报告生成段、补静默+report 教学）+ _system_tasks 条目 kind→subagent + `_migrate_legacy_journal_daily(ts)` 独立函数（保用户 cron）+ ensure 循环 create_task 转发 agent_name（fresh-install 接线）；交互版 journal-agent.md 保留（对话入口+周报路径）
- **关键审查发现（八轮双审锤炼）**：R1-A P1-2 report 提取源 return_value["messages"] 不可达（EXITED 不追加末轮）→ exit_content 尾部锚定；R1-A P1-1 创建点实为 5 处（模块函数是 ToolRegistry 生产入口，只改 schema 丢参数）+实施期发现第 6 处（disk yaml 声明层）；R1-A P1-3 迁移失败滞留 journal_daily 落 reminder 兜底=治理输出反污染 → 未知 kind 显式拒绝；R2-A P2-1 fresh-install 接线（ensure create_task 不传 agent_name → 静默死）；R3-B 降级标注后缀污染尾部解析；T3+T4 双审 Quality P2：hidden 检查插在 frontmatter 类型守卫前——非标量 frontmatter 使 get_tools_schema 整体崩溃（isinstance dict 守卫修复）
- **Skill/文档**：background-script.md 改写改名 scheduled-tasks.md「创建定时任务」（三类型对照+创建方法+report 主 Agent 视角）；niu.md 静态段补 subagent 类型一句+`[后台任务「」结束报告]` 前缀识别一句（先例：[定时任务]/[智能家居] 前缀都在静态段，skill 检索注入不保证在场）；SYSTEM_MANUAL 三漂移节点+subagent 机制节+升级注记；event-manager.md 示例同步
- **验证**：T1-T6 每 Task 双审（T3+T4 修复后复审 APPROVE）+ 收官整体审查 READY FOR DELIVERY（零发现）+ 177 点名测试全绿（豁免 test_scheduler_message_sse 1 例预存量失败——agent.context_manager 退役，基线实证非本工程引入，记入测试债台账）
- **实机验证（待用户重启）**：①journal-daily 每日 18 点行为不变（迁移后 task_kind='subagent'）②主 Agent 可 schedule_task(task_kind='subagent', agent_name=..., content=..., cron_expr=...) 创建后台任务 ③后台子 Agent `@end {"report": "..."}` → 主 Agent 收到 [后台任务「x」结束报告] 消息 ④chat-with-journal-daily-agent 不在主 Agent 工具列表

### 2026-08-31

#### 工程：同步子 Agent 挂起丢失防护——退出前拦截警告 + cleanup 现场保留 + 4 端点清理（计划 v1→v6 六轮双审门禁 + SDD T1-T4；反转 2026-08-26「同步子 Agent 随主循环退出被回收」定案）

- **背景（用户报告）**：同步调起的子 Agent 中途 `@niu-agent` 提问主 Agent，主 Agent 没回答反而下一轮错误地再次调用 `chat-with-xxx(task=...)` → 第一个子 Agent 上下文被全部清空、重新从零开始
- **根因链（全代码实证）**：子 Agent `@niu-agent` 提问 → INTERCEPTED_SYNC 拦截 → `_maybe_suspend_session` 置 state="waiting_for_answer" + 保存完整上下文，call_subagent finally 跳过 unregister → 提问文本作为 tool 结果返回主 Agent（正确做法：同轮调 `chat-with-xxx(answer=..., unique_name=...)` 接续）→ **主 Agent 本轮没调 answer** → 纯文本退出 CURRENT_TASK_DONE → runner finally 无条件调 `cleanup_suspended_sync_subagents()` unregister 所有 waiting_for_answer 同步实例 → 上下文永久丢失 → 后续 task= 调用注册表已空、register 不抛 ValueError、全新子 Agent 从零开始
- **核心缺陷**：cleanup 把「主 Agent 本轮没来得及回答」和「主 Agent 决定放弃回答」混为一谈——同步子 Agent 挂起 = 主 Agent 未完成的工具调用，系统却静默销毁现场、把错误合法化；本应挡住错误 task= 调用的同名 register ValueError 自纠提示因实例已被清掉而失效
- **反转 2026-08-26 定案**：旧定案「同步子 Agent 随主循环退出被回收（主 Agent 代答省无意义往返是合理体验）」反转——用户报障 + 根因链属实，旧权衡是效率压倒正确性；harness 记忆（learned.md）已同步更新
- **方案定案（用户逐条拍板）**：① **退出前拦截警告**：主 Agent 无工具调用准备退出工具循环且存在同步挂起子 Agent（`is_sync and waiting_for_answer`）时注入一次性 user 警告 + `continue`——LLM 同一循环内看到警告，决定继续处理（调 answer=）或明确放弃（再输出纯文本则放行）；异步不警告（独立线程生命周期，警告会导致永远退不出来）；② **警告用户不可见**：role=user 消息不 yield persist，靠 persist_agent_reply 的 role=user skip 不进 db；③ **不加超时**：工具循环 7 条退出路径 + max_turns=40 硬顶无卡死点，挂起保留收敛靠既有机制（提问 tool 结果已 persist 跨轮可见 / 同名 register ValueError / 用户 /stop 全清 / 进程重启清空）；④ **cleanup 语义反转**：仅用户显式停止（STOPPED/TERMINATED_BY_SUPPLEMENT/is_stop_requested 兜底）才清理，其余结束路径一律保留现场；⑤ **4 个会话清空端点**（compat.py clear_chat / chat.py clear_session / session.py delete_messages/delete_session）均在 reset_derived_state() 旁补 `cleanup_suspended_sync_subagents({"result": "STOPPED"})`——清空会话 = 显式放弃当前全部工作
- **交付链**：T1 agent_loop.py Path A 拦截警告（函数级 `_sync_suspend_warned` 防重复，最多注入一次）+ T2 runner.py cleanup 语义反转 + runner finally `cleanup_suspended_sync_subagents(return_value)` 接线 + 4 端点清理 + T3 tests/test_sync_subagent_exit_guard.py 14 新测试（拦截警告 / answer 接续 / 无挂起 / 异步排除 / 子 Agent 路径排除 / 警告不进 db / cleanup 判定矩阵 + finally 接线 / 不推送通知 / 4 端点）+ 既有 test_ask_user_cleanup_protection 适配（传 STOPPED 保持原断言）+ T4 文档同步（niu.md 防御纵深教学 / SYSTEM_MANUAL L37+L452 / manual-developer L179 / 本条目）
- **后续修正（同日夜，用户三轮纠正收敛）**：
  - **警告文案五版定稿**：①照抄用户示意话术（错——需求描述非文案）→ ②旁观者机制解释"指示其停止（子 Agent 会 @end 退出）"（错——用户批"话术整个都是反着的，没处在主 Agent 位置思考"）→ ③教学灌入 answer=/answer='/stop'（错——与 niu.md 提示词重复二次灌入且冲突）→ ④写死"子 Agent"（错——后台挂起可能是其他工具，名字已有就不要提类别）→ **⑤定稿：「[系统警告] 同步进程仍在挂起等待你的回答：{名}。这一轮你没有调用工具，确定要退出这次的工具循环吗？这可能造成数据丢失。」**——只提醒事实+询问退出，**不教终止方法**（不同工具终止方法不同，主 Agent 从工具 schema/提示词自行知道）；"这可能造成数据丢失"保留（通用性优先）
  - **教学位置**：answer= 继续对话 / answer='/stop' 结束子 Agent 工作——完整教学一次性写入 niu.md 同步调用段 + SYSTEM_MANUAL「同步 vs 异步调用」+ 同步/异步语义补充（**同步=封闭交互链**：挂起期间主 Agent 被绑定，不能自由转向用户，可穿插 ask_user 征求意见；**同时跟两边自由交互必须异步调用**）
  - **source 过滤**：Path A 警告检查加 `source != "program"`——只警告主 Agent 自己调起的同步挂起（user/scheduler）；程序触发子 Agent（睡眠管道 entity-extractor/dream-evolver/context-manager、journal-daily，program_triggered→source="program"）挂起残留（auto-answer 接续失败/异常/同名冲突）与主 Agent 无关，不警告（否则每轮对话被误警告打扰）
  - **实机验证（raw_http 20260831）**：警告触发（无工具调用+有挂起 → 注入+continue）/answer 连续 8 轮接续/ask_user 穿插/跨两轮用户介入后仍可接续/无死循环（每轮循环一次警告）全通过；本地 Agent 零背景模拟主 Agent 两场景（继续：ask_user→answer=；终止：answer='/stop'）均正确推导——话术可理解
- **已知边界（接受）**：警告最多一轮（LLM 再放弃则放行，挂起保留）；拦截轮回复已流式展示（用户可见两段，DB 仅存第二轮）；警告可见性依赖 persist_agent_reply role=user skip；挂起无超时（同名 register ValueError 阻塞收敛，用户 /stop/停止按钮/4 端点/重启可解）

#### 工程：Browser elements 大小控制——精简逻辑全删，elements 原样输出 + 头尾截断保护 tabSummary（计划 v1→v7 七轮双审门禁 + SDD T1-T3 双审全 PASS，main 798f06ad/a30e8666/4cdd1935）

- **背景（用户质疑 get_state 大页面截断）**：京东主页 dom_tree.js 原始 elements 实测 **33,900 字符 / 1,449 行** > MAX_TOOL_RESULT_CHARS=30000 → 走 disk 工具 30K 硬截断（从开头保留）→ **tabSummary（dict 末尾）被截掉** → LLM 看不到标签页 → 无法 close_tab（初始 bug 根因链）
- **精简方案证伪（16P 实测）**：旧 simplify_elements（正则解析去属性）在 16personalities.com 丢全部 radio 选项（`<input ... aria-label=I strongly agree />`——aria-label 带空格、无文本自闭合 `_RE_WITH_CLOSE` 不匹配）、京东 818 元素"精简"实为大量无文本元素静默丢弃——**任何"保留哪些元素"的规则都不可靠**（归纳法固有边界，换网站必遇反例）
- **方案定案（用户拍板）**：不精简——elements **原样输出**（dom_tree.js 给什么就是什么，零格式假设）；**从我们这一端控制总大小** <30K：超预算按行边界截断 elements（**保留头部**=搜索框/导航 + **尾部 30 行**=分页/提交按钮，中间折叠）+ 折叠标记（含行数统计 + 完整内容临时文件路径）+ 全量写 `~/.niu/tmp/browser_state_*.txt` 供 read 按需查看；**tabSummary/currentTabId 是结构化字段天然在截断后**（根治尾巴丢失，不再依赖"elements 后面还有位置"）
- **关键设计（七轮审查锤炼）**：
  - **预算转义感知**（R-A P1）：约束是 json.dumps 大小——`\n`→`\\n`(+1)、`"`→`\"`(+1)、`\`→`\\`(+1)、`\t`/`\r`(+1)：行成本上界 = len + 转义计数 + 2（+2 为 json.dumps 包裹引号，T1 实施实证：计划代码漏计 2 字符致 test_escape_aware 必挂，按"修代码不改断言"修正）
  - **截断路径最终校验**（R-B P1-1）：无 ≤60 行早退（40 行×800 字符长文本页会复发原 bug）；尾部行数从 30 递减直至 fit（head_budget≤0 时递减给头部腾空间）；`truncated + fold_note` 总成本超预算 → 降级最小 stub
  - **总大小统一判断**（R-C P1-1）：elements 小但 tabSummary 巨大（>26K 数百标签页）也会超 30K——透传判断只比 `_json_size(elements)`（elements_budget 已扣 fixed_cost，双重扣减会让"本可透传"的响应走截断+虚假折叠标记）；fixed_cost 用 `_json_size(v)` 转义感知
  - **stub 分支**（R-B P2-1）：`elements_budget ≤ 0`（fixed_cost ≥ 26K）→ elements 降级固定 stub + 文件路径；保护不可达阈值 tabSummary >~29.7K
  - **测试尺寸全部实测复算**（R-D/E/F）：700 行 tab 实测 30.5K（stub 后 31,504 必挂）→ 600 行 26,093（stub 后 27.0K ✓）；4 元组解包全仓穷举（R-F 抓出 test_truncation_line_boundary 漏改）；京东 fixture 33,900 字符/1,449 行（browser 注入 dom_tree.js Node 侧 fs.writeFileSync 落盘，wc -c 40,239 字节证实 CJK 3 字节——校验必须用字符计数）
- **交付链**：T1 simplify.py 重写（fit_response 纯大小控制 + `_json_size`/`_line_cost`/`_truncate_head_tail`/`write_full_elements`，删 simplify_elements 全正则）+ 13 单测 + __init__.py 4 处失实注释修正；T2 集成测试适配（4 大页面测试断言新语义 + **MD_DIR 显式 monkeypatch 隔离**——经全链路会写真实 ~/.niu/tmp）；T3 SYSTEM_MANUAL L96 更新（grep 残留零命中）
- **验证**：13+6=19 测试全绿（单测 + 集成）；**smoke 直演**（京东真实 fixture 走 fit_response）：33,900 → 23,163 字符，总 JSON 26,039 < 30K，tabSummary/currentTabId 完整，头 `*[0]` + 尾末行双保留，折叠标记 + 临时文件就位
- **实机验证（待用户重启）**：browser 工具打开大页面 → get_state 响应 < 30K 且 tabSummary 可见 → 主 Agent 可正常 close_tab；折叠标记含完整内容路径 → read 可查全量

### 2026-08-28

#### 工程：测试债清算（T0-T7）——147 条失败全核销 + 版本 0.3.1（HEAD a57cb80a+dafa069f）

- **背景**：历轮工程攒下 147 条测试失败/错误（TestDebtInventory 盘点 145 + test_photo 2 条补录）；T0 权威台账 131F + 16 excluded（触网/写图）逐条归属到 Task，无未归属条目
- **2 真 bug（修生产代码，独立 commit）**：①repair GraphML edge 字段改按 attr.name 解析——key 编号漂移致 full_relations 静默不重建（42a4b187）②`_extract_tmp_paths` 双缺陷（file:// 前缀 + markdown 标点收尾）致 tmp 文件永不清理（a3e31fa8）；其余 8 例疑似真 bug 全部定性为假 bug（陈旧断言/mock 失配），只改测试对齐现役契约
- **删除**：10 个死代码测试整文件删（v8 repair 体系 6 死文件 + inject_entity/inject_relation 族 + 双管道遗留 + Windows 机器绑定源码扫描）+ 生产侧 agent/tool_lifecycle.py 退役（T3，守卫测试转绿）
- **REDACTED 全仓清零**：5 文件脱敏字面量清扫；test_context_overflow_real 标 e2e 门控 + 修相对路径
- **测试边界纪律补（test_kg_merge_tdd 写图教训）**：盘点实证该文件绿用例写生产图谱（+2 实体/+2 chunk/+1 depicts 边，已按 lightrag-data-repair 流程恢复）→ T1 双入口 patch（get_lightrag + LightRAGIngester 直构造）+ 守卫断言根治，零写入双实证后进全量
- **历轮「既有豁免」全部作废**：本工程核销 AGENTS.md 全部历史 pre-existing/既有失败豁免条目（test_lightrag_adapter TestIngester*/TestSearch*、test_lightrag_repair_unit 真实数据、test_tidy_cursor、test_subagent_overflow client.backend None、test_compress_*、phase02 等）——全量 3218 passed / 0 failed / 151 skipped，不再存在"已知失败"基线
- **版本 0.3.0 → 0.3.1**：三处同步（VERSION + chat.html version-label + compat.py UA）+ test_list_models_endpoint 2 断言；工作原则 8 由「两处」改「三处」（UA 为第三处，连带测试断言）
- **验收**：全量 3218 passed / 0 failed / 151 skipped（e2e/integration 门控 skip 除外；T0 基线 3114P/131F/156S 对拍）+ lightrag_storage 零污染

#### 工程：read 工具智能分页——29000 字符页预算按行截断取代行内均分截断（计划 v1.0→v1.3 四轮双审门禁 + SDD T1-T3）

- **背景（用户提出）**：dream-evolver/entity-extractor 提示词"每次读取不超过 150 行"是过度保守浪费轮次——核查发现 150 行是绕开行内截断缺陷的提示词补丁（500 行/页时每行被均分预算砍到 1000 字符，静默破坏 F1/F3 tool 记录），非根治
- **方案（用户拍板）**：500 行硬上限不变；工具自动计量内容大小，贴 29000 字符页预算**按行截断**（行边界停，永不在行中切；单行超预算才走行内截断兜底）；页长自适应省轮次
- **交付**（main `0c29cd4e`/`4d9978d8`/`92291d0d`）：
  - T1 read_file 重写：预算累积（每行成本=len(f"{i}|{line}")+1）+ 单行兜底（line[:预算−len(tag)−len(前缀)−1]+tag）+ 续读标记 `[Truncated at line {N}. Use offset={N+1} to read more.]` 精确行号且**移至输出末尾**（recency 利于弱模型；旧标记在 header 第 2 行且预算截断时跳行）+ 删行内均分截断四行 + schema 描述补三句；测试 45 绿（新增 6 用例：14 行/页精确断言/单行兜底/页中超长行跨页/预算不变式固定种子/tail 回归）
  - T2 提示词配套：两子 Agent 撤 150 行指导改"工具自动分页、见末尾标记按 offset 续读"；零背景推演验收（续读链/processed_line 语义零歧义）
  - T3 收尾：死函数 file_read 删除（46 行化石，512000 公式；工具名别名映射保留）+ session-manager 两处"对齐 read_file"失实注释自述化；回归 54 绿
- **设计要点**：预算口径=字符（对齐下游 agent_loop 30000 字符截断，返回整体 ≤30000 永不二次截断）；标记不归因截断原因（预算/limit/500 硬顶同文案，offset 恒正确）；READ_PAGE_BUDGET_CHARS=29000 常量注释锚定 agent_loop.py:598 耦合
- **实机验证（待用户观察）**：下次睡眠管道 dream-evolver/entity-extractor 读 F3/F1 时页长自适应（短行文件一次读满 500 行），raw 日志可见 read 调用轮次减少
#### 修复：read 工具 tail 读预算方向——EOF 锚定窗口 + 反向累积（用户指出 + 四轮审查冻结，commit c96bfae5）

- **背景（用户提出）**：智能分页改造只覆盖了前向读；tail 语义（offset<0 读末尾 |offset| 行）仍沿用前向预算逻辑，方向错误
- **4 缺陷实证**：D1 tail 预算正向累积（长行处断页保留窗口头部、丢弃真正 EOF 行，tail 意图落空）；D2 单行超预算仍切行首（tail 读者要行尾）；D3 测试固化错误行为（test_tail_offset_with_budget 断言"只返回第 8 行"）；D4 limit 前向锚定（offset=-50 limit=10 返回 51-60 而非 91-100，窗口最旧 10 行）
- **修复语义**：窗口=末尾 `min(|offset|, limit)` 行（EOF 端固定，limit 从旧端收缩起点）；预算从窗口末行向首行**反向累积**（页保留最新行，被挤掉的只能是更旧行）；窗口末行超预算保**行尾**+前导 `[TRUNCATED] ... `（16 字符镜像）；反向续读标记 `[Truncated at line {k}. Use offset={wstart} limit={k-wstart} to read lines {wstart}-{k-1}.]` 含显式区间（弱模型免相对推断）；header 报页实际首行
- **质量链**：计划 v1.0→v1.2 三轮审查（TailReview scout 复核 + R1 双 CONDITIONAL + R2 双 CONDITIONAL + R3 本地单审 APPROVE——远端 scout 卡 stream-stalled 40 分钟重试，改本地单审收口）+ forward 零变化对拍钉死（改造前 seed=42 fixture 全页快照 tests/fixtures/read_file_forward_snapshot_seed42.json，逐字节对拍断言）
- **验证**：TestReadFile 23 passed（改 2 增 1）+ 全文件 46 passed + 63 回归绿；smoke 直演验收 5 项 + 边界 8/9（窗口中部超长行走正向兜底、反向区间再超预算多页续读）
- **文档同步**：SYSTEM_MANUAL §2.2.1 tail 条目扩展 + 托管技能 niu-read-tool-behavior §2/§3/§4 + tools_schema.json L21/L24/L25 + read_file/do_read docstring limit 收缩语义
- **已知瑕疵（接受）**：tail 反向标记引导的正向补读链末端 marker 指向已读区（冗余有界，forward 契约不可为 tail 特判——read_file 无来向信息）

#### 加固：子 Agent 压缩可见性两条微改造（归档机制经论证不建，commit e099e8da）

- **背景**：用户提议子 Agent 模仿主 Agent 组装器做"溢出归档到 ~/.niu/tmp/ + read 召回"；讨论中用户两点实证推翻——①子 Agent 是工作 Agent（一条指令跑到尾），会话轮切割概念退化；②占位符化（80% 触发）实证全部吸收溢出，FIFO 阶段 2 零真实触发——为从未发生的路径建机制收益为零；且子 Agent 工具输出绝大多数可再生（文件/DB/图谱可重查），召回通道隐性存在。**定案：完整归档机制不建**，若日志见 `[FIFO] Proactive pruning` 频繁出现再重启（届时逻辑=归档工具输出而非轮次）
- **落地两条微加固**：①占位符文案加再生指引 `[name 输出已裁剪，如需原文可重新调用该工具获取]`（幂等判定兼容新旧后缀，防恢复会话旧占位符被二次替换）；②`_fifo_prune` 真删时在切割位置插入可见标记消息（`[上下文提示：更早的 N 条消息已因上下文超限被移除]`）——无声丢失变有声
- **验证**：3 测试文件 41 passed + test_subagent_overflow 44 例中 4 failed 经 stash 基线复核全部 pre-existing（3 例 client.backend None 既有豁免 + 1 例 journal 迁出睡眠管道后的陈旧源码扫描断言）；新增 4 用例全绿
- **遗留跟进点（已闭环 2026-08-28 测试债清算 T7）**：compaction.py L244 旧文案已统一为子 Agent 同款再生指引，test_compaction 断言同步

#### 修复：请求组装 thinking 双通道去冗余（用户看日志发现 + 双审通过，commit 1225a1a9）

- **现象（用户报告）**：raw_http 日志查看器的应用层 Request Params 里 `thinking` 出现两份（顶层 + extra_body），主 Agent 与知识图谱入库路径同现；另质疑知识图谱入库请求无 response_format
- **实证结论**：
  - **thinking 重复**：真但无害——`chat()` 先把 `litellm_kwargs` 全量透传顶层（旧通道），`assemble_request_params` 又注入 `extra_body.thinking`（新通道）；litellm 发送时 drop_params 丢弃顶层、合并 extra_body，上线 HTTP body 只有一份（transport 层日志 16 条全核对）。远端审查对照安装版 litellm 源码实证：volcengine 路由原生就把顶层 thinking pop 进 extra_body（map_openai_params）、extra_body 合并 `data.update(extra_body)` 是全路由通用行为——修复前后 wire 完全等价
  - **response_format 缺失**：设计行为非 bug——`_llm_model_func` 只在 `keyword_extraction=True` 调用挂 response_format（json_schema 是 high/low_level_keywords 专用结构），实体抽取（分隔符文本输出）本就不挂；用户配置的 `response_format_mode: "json_schema"` 未丢
- **修复**：`chat()` 顶层透传剔除 thinking 单键（`{k: v for k, v in self.litellm_kwargs.items() if k != "thinking"}`），extra_body 为唯一送达通道——与 reasoning_effort 同策略、与探测路径 `model_probe._strip_thinking_key`（R13 单一来源纪律）同款；`allowed_openai_params` 等其余键保留顶层（litellm 当 kwarg 消费）；drop_params 置位逻辑不变
- **通用性**（用户拍板：Anthropic 不管，其余厂商要通用）：openai 兼容网关（deepseek/GLM/qwen/豆包）顶层 thinking 本就被 drop_params 丢弃，extra_body 本就是唯一实际通道——任意厂商 wire 行为不变
- **验证**：旧行为锁定测试断言反转 + 新不变式测试（顶层无 thinking/extra_body 送达/其余键透传保留），tests/test_llm_extra_body.py 16 绿；本地实施 + 远端 scout 源码级双审 APPROVE
- **实机验证（待用户重启后观察）**：raw_http 查看器应用层 Request Params 中 thinking 只出现在 extra_body 内

### 2026-08-27

#### 工程：设置页模型列表在线探测 + 选中自动填档（计划 v1.0→v1.15 共 16 轮双审门禁 + SDD T1-T3 双审全 PASS）

- **背景**：用户质疑上下文窗口靠手填——实测三条「便宜拿」路径全败（litellm 静态表对豆包 Coding Plan 给 256000 但实际窗口实测 ~229K 二分撞出、网关 /models 404、max_tokens 边界只探出输出上限 131072）；用户拍板：在线标准方法（`GET {apiBase}/models`）探测，探到就预填，探不到维持手填；litellm 离线表禁用（打包即冻结/口径是标准产品线非 Plan 线/程序无法判断表项对错）
- **交付**（main 00f09ae3/64ff0794/335841e6）：
  - T1 `POST /api/list-models`（compat.py）：openai/anthropic 双类型 URL 组装、窗口字段四键提取（context_length/max_input_tokens/context_window/top_provider.context_length）、三态返回（ok/unsupported/error）永不 500、本地免 key、非字符串字段守卫；18 测试
  - T2 IPC 通道：preload listModels + main.js list-models 转发（timeout 15s，**4 个失败出口全归一 {status, reason}**——get-config 直读文件不经 API server，server 未起时设置页可用，ECONNREFUSED 是可达主路径）
  - T3 前端 combobox：datalist+hint（四路径终态全钉死：成功/unsupported 缓存/error 静默重试/change 复位）+ 选中自动探测（D4 datalist options 单容器判定）+ **probeCapability 快照三元组 (apiBase|model|type) 全完成分支防陈旧复写**（手动按钮路径同治）+ probeInFlight 模块级单标志（置位点钉死三条校验后侧效代码前+check-and-set 二次检查）+ 失效三件套（清缓存+复位 hint+清 datalist，preset 路径同补）+ 窗口预填 clamp 32000-2000000；13/13 mock 场景 PASS
- **审查亮点**：16 轮双审抓出两个双审交叉级发现——探测陈旧复写竞态（探测在途 1 分钟窗口换模型，旧结果覆盖新配置）与 IPC 失败形状缝隙；R10 双审独立同发现快照维度不全（model→三元组）
- **申报偏差**：T3 为保一屏放下做纯留白 CSS -20px（内容高 743px ≤750）；手输恰等于列表项会触发自动探测（datalist 不可区分，无害披露）
- **实机验证（待用户执行）**：计划 §7 清单 6 条（前置纪律：设置页改动必须关窗重开）——豆包 404 降级手输/标准网关下拉+自动探测+窗口预填/手输不自动探/探测中改模型与改网关双竞态回归/保存全流程

#### 修复：测试隔离漏洞——test_clear_brain_state 真删生产指针块库（commit 887b533f）

- **现象**：context_blocks.db 消失仅剩 .lock，聊天历史无块号——用户质疑压缩工程失效
- **根因**：tests/test_clear_brain_state.py 调真实 compat.clear_chat() 未 patch reset_derived_state（T8 给 clear_chat 加派生状态复位后该老测试变炸弹），08-26 14:32 测试运行真删 ~/.niu/context_blocks.db + token_calibration.json；姊妹测试（test_remove_outer_timeouts/test_pipeline_queue_t4）都有防删 patch，此文件漏网。生产端点全部排除（4 个删除点均与 clear_messages 成对，messages.db 738 条完整）
- **修复**：补 patch（两处调用点）+ 全仓审计无其他漏网；token_calibration.json 已由校准回写自动重建
- **教训**：给既有函数加副作用时必须穷举该函数的全部测试调用方——姊妹文件补了不代表全仓补了
- **机制认知**：块号只在压实后出现（D16 水位线），块库误删后用量 63%<80% 触发线故视图纯原文窗口——工程是生效的，非失效；[摘要]/[合并] 行是旧压缩体系物理改写 DB 的历史遗留，组装器视 DB 为真相源原样呈现

### 2026-08-26（续）

#### 工程：journal 子 Agent 直读 DB——日志即水位线（计划 v1.0→v1.5 双审 R1-R6 门禁 + T1/T2 SDD 双审收敛）

- **背景与病灶**：T7 后 journal 链路=程序把 DB 增量导出到 `~/.niu/md/journal_workset.md` 让子 Agent 自读+last_journal.json 程序侧游标。用户实测「日志子 Agent 无法工作」实证三病灶：①导出文件是动态中间产物（覆盖写/unlink/并发窗口）非准确历史②零增量或导出失败时任务文本无文件路径、子 Agent 无从获取消息（直接断链）③游标仅夜间推进、程序监听不到交互路径结果。组装器新架构使 messages.db 只增不改，直读 DB 的历史障碍消失。
- **设计定案（用户逐条拍板）**：D-A 数据源唯一=messages.db；D-B 日志即水位线——每条整理条目尾带机器可读标记「覆盖至: <message_id>」，单一工件自描述，交互记录条目不带标记；D-C 分支判据=是否提取 DB 内容（记录单件事不动标记/整理类完整流程/报告类默认纯聚合不足再整理）；D-G error 归因分级（invalid_after_id→首次兜底/transient→轮空不写标记防覆盖空洞）；D-H mcpToolFilter 嵌套 dict 钉死只暴露 get_messages（平铺列表会使 subagent.py L656 AttributeError——R3 抓出）。
- **交付链**（main 61d37700→80fcbdd8 共 2 commits，24 文件净删 539 行）：
  - T1 `61d37700`：get_messages 四处 schema 同步扩展 after_id/limit/full_tool_output+created_at/has_more/next_after_id+reason 分级错误；折叠直接 import 复用 agent/md_mirror.truncate_tool_output（<已精简> 2000B 头60%尾40%）；stdio dispatch get_messages 分支改直调消除双实现（申报偏差，对齐 read_history_block 先例）；14 单测
  - T2 `80fcbdd8`：journal-agent.md 重写（三分支判据+七步整理流程）；handler _build_journal_task_for_handler 整删薄层化；scheduler 任务文本自理化+import 收缩（R4 抓出漏改则夜间静默 ImportError）；compat 游标链整链退役（_export/_parse_processed_up_to/JOURNAL_*/_read_write_cursor_with_lock/_ALL_CURSOR_FILES+_reset_all_cursors 四调用点）；SYSTEM_MANUAL/niu.md 同步；测试处置（grep 穷举+create=True 保零写退役反向钉）
- **质量链**：计划 R1-R6 六轮双审（R1 P0×1 /new 清库后标记失效无恢复→D-G；R3 P1×1 mcpToolFilter 格式错误照抄即崩；R4 P2×1 import 块收缩漏点名；R5+R6 连续两轮双 APPROVE 达成门禁）+ 每 Task spec/quality 双审（T1 双 PASS、T2 双 PASS+微修闭环：dispatcher docstring 残留/SYSTEM_MANUAL「复位全部游标」虚假陈述）
- **验证**：点名回归 150 passed；真实 load_mcp_tools 断言 journal-agent 工具面恰为 [get_messages] 且 Schema 含三新参；DiskEngine.get_schema() 零泄漏；py_compile/ruff 零新增

### 2026-08-26（续二）

#### 工程：mcp-servers.yaml 双目录化——copy-once 设计债清偿（计划 v1.5 双审 R1-R5 门禁 + T1-T3 SDD 收官，版本 0.3.0）

- **背景与病灶**：mcp-servers.yaml 是三配置面中唯一 copy-once 例外（launcher 首启复制到 `~/.niu/config/` 后仓库侧变更永不达存量装机）——b248c8b6 式手工修复即此类脱节；任何内置服务器的新增/参数/tools visibility 变更都卡在同一死点。关键实证：`${PYTHON_PATH}` 是装饰性字段——全仓无替换/执行代码（MCP 同进程化后内置 server 全经 ToolRegistry 直调，command 仅外部 stdio 消费），bundle 配置零机器相关值可直读。
- **方案定案（docs/superpowers/plans/2026-08-26-mcp-servers-dual-dir.md v1.5）**：
  - D1 bundle 权威层直读 + 用户层 `~/.niu/config/mcp-servers-user.yaml` deep merge（dict 递归合并 / 标量 list 用户赢），用户只写差异段即可给内置 server 补 tool visibility
  - D2 同名冲突用户赢；D3 迁移=0.3.0 升级说明删旧文件、零自动迁移（范围仅此一文件，user-config.json 不碰）
  - D4 任一层解析失败 error 降级空基座继续启动（config 解析失败从不终止启动）；D5 用户层缺失=正常态
  - D7 删除语义：用户层 `server名: null` = 禁用该内置 server——deleted_names 集合在 REQUIRED/OPTIONAL 两条加载循环兑现 skip（不计失败不触发严格终止），嵌套 null 只删键不入集合；两调用点均解包 `(merged, deleted)` tuple
  - D8 旧文件弃用 warning 模块级去重（双调用点共享至多一条）+ 测试重置口；D9 跨平台零新增分支（os.path.expanduser 同款先例）
- **实施**（T1/T2 改动在工作树待提交 + 本条目 T3）：
  - T1 Python 双源加载：_load_mcp_config 返回 (merged, deleted_names)；deep merge/null 删除/bundle 缺失降级/load_external_servers 非 dict 条目守卫；niu_api/config.py 删 _get_mcp_servers_path 惰性兜底复制；16 例合并矩阵测试 tests/test_mcp_config_dual_source.py；test_p0/test_mcp_loader.py 三处 patch 契约改 tuple
  - T2 Rust launcher：init_niu_dir 删 mcp-servers.yaml 复制段（53 行纯删除；user-config.json 复制段保留不动）
  - T3 文档收官：manual-mcp-disk.md L102 与 AGENTS.md L259 两处 `${PYTHON_PATH}` 失实修正 + manual-mcp-disk.md 新增 2.7 用户层配置节 + SYSTEM_MANUAL.md 新增 2.1.1 双目录加载节（含 0.3.0 升级说明，两文档交叉引用）+ 版本 bump 0.3.0（VERSION/chat.html version-label 两处同步）
- **验证**：点名回归 test_mcp_config_dual_source.py + test_p0/test_mcp_loader.py 共 32 passed；grep 全仓 `${PYTHON_PATH}` 失实描述零残留（config/mcp-servers.yaml 内为字面量数据不受影响）
- **实机验证清单（待用户执行）**：①删除 `~/.niu/config/mcp-servers.yaml`（先备份）后启动 → 内置 10 server 照常加载②建 mcp-servers-user.yaml 写测试 server → registry 中并存③用户层覆盖 preload/tool visibility 生效④`server名: null` 禁用 REQUIRED server → 启动 warning skip 不终止⑤旧文件残留时弃用 warning 至多一条且不影响加载⑥重打包后 launcher 启动日志无 mcp-servers.yaml 复制行

### 2026-08-25

#### 工程：MD 中继工程五——force dream 保护链退役 + dream 游标终退 + 化石清理（方案 v1.0→v2.5 共九版，R1-R7 双审+全局架构审计收敛）

- **背景**：用户质询「force 保护链在文件驱动下不存在」——工程四完成提炼文件驱动化后，F1/F2 不受 DB 压缩影响，基于 `~/.niu/last_dream_evolve.json` 的 force dream 哨兵保护链成为旧范式自洽残留（提示词层引用已消失的游标 UUID、机制层哨兵计算与砍半互斥空转）；全局架构审计清查提示词/机制/文档三层残留后重构计划。
- **方案**（docs/superpowers/plans/2026-08-24-md-relay-project5-cursor-retirement.md v2.5；门禁=同文本连续两轮零发现）：
  - **T1 提示词层对齐**：context-manager.md 三处 dream 边界描述改写（模式一=last_compress_id 之后全量无上界）；dream-evolver.md frontmatter mcpServers+mcpToolFilter 双删 session-manager（单删过滤项会因缺省 filter 全放行而扩权），get_messages 禁止理由改「对话记录在 F3 文件中自读」
  - **T2 机制层七件套整链退役**：force/runner-force 哨兵与边界防护、睡眠 cm 锚点排除+cascade cursor 分量、dream 循环游标回写与 fresh_ids 校验、`_build_force_prompt` 安全边界行、砍半互斥、`_ALL_CURSOR_FILES` 收缩两键（journal+compress）、入口共享读取删除、`_f_id_to_idx` 反向映射整删；磁盘清算 `~/.niu/last_dream_evolve.json`(+.lock)；新建 tests/test_cursor_retirement.py 六组退役钉
  - **T3 化石清理与回归收尾**：context-manager.md 三处「安全边界」死文本块删除+「未提取知识」悬空引用改写为现行语义+决策流程列表缩进修复；步骤编号化石（compat force 分支 2.5/3·3/3、runner 2.5/4·3/4 → 1/2·2/2）；CP3 注释改文件驱动措辞；SYSTEM_MANUAL 睡眠管道段澄清 force 只跑压缩对；AGENTS.md 增量游标存量化石标注退役；md_alignment docstring 补 [摘要] 补写边缘态标注
- **拆链后终态**：模式三=对全部消息 keep/update/delete（无 dream 边界；PROTECTED 近期消息排除照常保留）；dream-evolver 只删 F2 前缀、无任何游标读写；journal/compress 两游标语义不变
- **验证**：点名回归 10 文件全绿（test_cursor_retirement/test_sleep_reorder/test_md_f3/test_dream_segment_v2/test_entity_segment_v2/test_journal_agent_tidy/test_compress_prompt_lean/test_compress_degradation/test_compress_history/test_compress_quality）；py_compile+ruff 零新增
- **commit**：`7dd61379`（T1）+ `8aaba576`（T2）+ 本条目（T3）
- **实机验证（待用户重启）**：①/compact 正常且模式三方案覆盖全部消息（无边界截断）②睡眠 dream 多轮循环正常、F2 前缀按 processed_line 删除③/new 后仅复位 journal/compress 两键④`~/.niu/last_dream_evolve.json` 不复活

### 2026-08-24

#### 工程：MD 中继工程四——睡眠管道重排 + 压缩前置门控清算 + 游标清算（方案 v1.0→v1.9 共十一轮双审收敛，连续两轮零发现）

- **背景**：工程二/三完成提炼文件驱动化（F1/F2/F3 中继）后，睡眠管道仍保留旧物理顺序 entity→dream→journal→compress；spec §5/D3 定稿顺序为压缩在前、提炼在后——用户实机验证时发现该盲区，重排补进工程四走完整计划→双审→SDD 流程。
- **方案**（docs/superpowers/plans/2026-08-24-md-relay-project4-reorder.md v1.9，R1-R11 双审全远端派审）：
  - **睡眠管道重排**：entity→dream→journal→compress 改为 **journal（仅模式2及以上 usage≥50%）→ context-manager → entity-extractor → dream-evolver 多轮循环**。安全性根基=提炼文件驱动化：DB 压缩只动 Message DB 不触 F1/F2 文件（镜像仅挂 add_message，压缩的 [摘要] 替换不回写），entity/dream 读到的永远是完整原文，压缩先行零丢失
  - **CP 重排**：CP0 排队唤醒非睡眠 → cancelled；CP1 journal+压缩段完成后 / CP2 entity 段完成后 / CP3 dream 循环完成后 → interrupted（已推进不回滚，下次续跑）
  - **门控清算**：删压缩前置游标追平校验 `_cursors_caught_up` 调用及三孤儿函数（`_cursors_caught_up`/`_dream_only`/`_read_cursor_value`，含 7 处 monkeypatch 缝）；/compact 不再出现 skipped 状态，不被梦境积压阻塞
  - **游标收缩与哨兵保护**：模式一 end_cursor 上界移除；post-dream 范围守卫收窄但**保留锚点排除+cascade**（数据源改入口读取 last_dream_evolve_id——force 哨兵承重墙）；dream 游标继续回写（force 边界唯一数据源，停写=永久全保护退化）；复位表三键方案（journal/compress/dream 全留，防 /new 后陈旧游标+F2 truncate 致 force 哨兵 0↔len 翻转静默关闭边界）；`last_entity_extract.json` 死键清算，scripts/backfill_f1_from_db.py DEFAULT_CURSOR 默认空串防误跑
  - **已知边界**：cm 失败 mode-2 早退中止整个 sleep（entity/dream 延迟一轮自愈）、mode-1 三类失败吸收续跑；压缩对内部无检查点，唤醒最早 CP1 被感知（既有段落原子性非新退化）；force 保护边界滞后为保守方向（多保护不少删）
- **实施**（main 2 commits，SDD 每 Task 新鲜子 Agent）：`23b8c4c1` T1 重排+CP 检查点迁移+12 文件约 30 例测试适配（含 test_subagent_overflow getsource 源码序断言——三轮被删符号 grep 盲区，R4-B 抓出）；`9eb1d36a` T2 门控三孤儿删除+复位表三键+backfill 防误跑+残留游标档清理
- **验证**：T3 点名回归 20 文件 307 passed；剩余 7 failed 全部基线既有豁免（test_tidy_cursor 4 例 PROTECTED 类 + test_subagent_overflow 3 例 client.backend None 环境）+ 本次顺带修复 6 例历史存量测试腐化（test_compress_quality FakeClient.backend×2 与 _read_cursor_locked patch×2、test_compress_history 用户轮边界语义×1、test_stop_interruptible _ensure_session_chain 桩缺失×1——worktree 对照 b3bb7cb7 实证全部 pre-existing）；py_compile 三文件通过，ruff 17 条告警与基线逐码一致零新增
- **实机验证（待用户重启）**：①睡眠日志顺序 journal→cm→entity→dream 多轮循环 ②手动 /compact 不再被积压阻塞且无 dream-evolver tab ③/new 后 F1/F2/F3 清空且游标复位正常 ④force 边界行为正常（[Tidy] 游标跳过告警频度观测）

### 2026-08-22

#### 修复：向量检索精确名短路——query 恰为实体名时图层精确命中置顶（精确名查询根治）

- **现象**：睡眠后向量检索某高频人物实体检索不到（rank 49 掉出 top_k）。
- **根因链（全实证）**：① dream-evolver 触发式精简（`lightrag_edit_entity`）把该实体描述归纳覆盖为**无主语属性堆叠**（正文零次实体名、丢称呼锚点）→ ② 向量 = embed(entity_name + "\n" + description)，bge pooling 全文加权——开头 1 次名字被 130 字正文稀释（量化：无主语堆叠 sim 0.4409 rank 49；名字首句（"XX是…"）sim 0.5780 ≈历史 #1）→ ③ **检索侧真实缺陷**：`search_entities`→`query_data(mode=local)` 纯向量语义排序，query 恰好等于实体名时无图层精确索引短路——向量排序决定精确名查询命运。
- **机制认知（bge 稀释）**：向量化构造 name+\n+description 本身没错，但隐含假设"description 是自然语言描述（主语会反复出现）"——精简成无主语电报体后假设破裂。name 在开头出现 1 次被正文稀释，正文再出现名字信号加倍。
- **方案（用户拍板）**：向量检索与图层精确名检索**并行**，精确命中实体分数置最高。落地为 query_data 返回前后置修正（**不动 LightRAG fork**——成熟产品不改原则）；**事实更正**：返回实体项无 rank 分数字段（rank 是 operate.py 内部图度数中间值，convert_to_user_format 已丢弃）——"分数放到最高分"落地为"置顶到首位"。
- **实施（main 2 commits）**：
  - `26a37871`：query_data 短路块（45 行）——守卫 G1 status==success（failure 零命中不短路）/ G2 filter_lambda is None（search_by_file_path 技能通道契约不绕过）/ G3 mode∈(local,global,hybrid,mix)（naive/bypass 实体恒空）/ G4 data dict+entities list 双形态；query.strip() 非空+≤50 字符+has_entity(q) lowercase 精确命中 → get_entity_info 下钻 data.graph_data 构造同构项（无 rank 无 distance；fields 在场同过滤）→ 在列重排首位/不在列插入首位截断生效 top_k；全块 try/except 异常返回原结果
  - `89ac2cdd`：tests/test_query_data_exact_match.py 10 用例（命中不在列截断/命中在列重排/未命中/组合 query/异常防御/failure/filter_lambda/info error/naive 门控/51 字符门控）
- **一个入口全覆盖**（调用点核对一致）：MCP search_entities/lightrag_query_data、kg_api 前端搜索、search_multi_lightrag 动态注入、region_injector 脑区激活、timeline_query、photo-server 全过 query_data；filter_lambda 通道（search_by_file_path/search_within_region）G2 豁免
- **下游效应（有利）**：置顶实体 distance 缺失 → runner 衰减池 fallback i=0 → 1.0 = 池内最高分（decay_pool 降序）——与置顶意图一致
- **质量链**：方案 v1.0→v1.2（R1 双审交叉抓出 rank 事实错误+failure 门控 2 P1 + filter_lambda/mode 契约 2 P2；R2 双审仅剩 P3 级行号/清单/测试增补，修正后确认可交付）→ 实施 2 commits → 实施后双审 APPROVE（Quality 零缺陷）
- **验证**：新测试 10 passed；adapter 回归 51 passed/9 failed 与 pre-existing 基线精确一致零新增
- **实机验证（待用户重启）**：search_entities("<实体名>") 或对话查该实体 → 该实体在结果首位（永久免疫描述形态漂移）
- **已知边界**：多词 query（"<实体名>是谁"）不短路走向量（保守语义）；>50 字符实体名不短路（DEFAULT_ENTITY_NAME_MAX_LENGTH=256 的取舍）；向量异常/failure 路径短路不可达（门控优先）；语义通道（模糊查询）质量交由 dream-evolver 自然演进（精简锚点规则不改——用户拍板）；受损描述不手动修（短路后精确名查询已免疫）

### 2026-08-21

#### 修复：entity-extractor 提炼入库 doc_id 撞车静默丢失（方案 R1+R2 双轮审查 + T1-T4 实施 + 实机验证）

- **现象（用户报告）**：内容提炼调 `lightrag_insert` 入库后，知识图谱**多数情况无动作**、少数才有——当天第二次提炼起全部静默丢失（2026-08-21 实证：10:42 首次入库成功，11:08 第二次同 doc_id 被吞）。
- **根因三层**：
  1. **提示词层**：entity-extractor.md L44/L66 教 LLM 自编 `doc_id="refined:{date}:{seq:03d}"`——LLM 不知道当天已用几号 seq → 恒写 `001` → 当天第二次提炼撞车
  2. **去重层**：LightRAG `apipeline_enqueue_documents`（lightrag.py L1452-1513）`doc_status.filter_keys` 检出 doc_id 已存在 → 过滤出处理队列（early-return L1508-1510）→ 仅 warning → **不做实体抽取**；`ainsert` 仍正常返回 track_id（L1237-1270）→ 工具/LLM/程序三层无感知
  3. **清洗层**（dup- 记录不可见之谜）：撞车时 upsert 的 `dup-` FAILED 记录**立即落盘**（json_doc_status_impl.py L222 upsert 自带 index_done_callback），但 `GET /api/kg/pipeline_status`（kg_api.py L569-620）被 chat.html:2390/spirit.html:636 每 3s 轮询，管道完成后 `_cleanup_failed_docs`（kg_api.py L23-104）删除全部 dup- 条目——**dup 记录活不过一个轮询周期**，事后排查永远看不到撞车痕迹
- **方案 A（删 doc_id 走内容 MD5）**：root cause 修复——去重键从"LLM 瞎编的序号"变"内容本身"（不同内容永不撞车、相同内容合理去重）。`lightrag_insert` schema 的 doc_id 本就 Optional（auto-generated if omitted）
- **实施（main 4 commits + AGENTS.md 本条）**：
  - `c13c28a1`：entity-extractor.md L44/L66 删 doc_id 指导 + 显式"不要传 doc_id"（提示词每次调用现读，无需重启）
  - `9730a4c9`：inject_document changelog 空 id 修复——`"id": doc_id or track_id or ""`（doc_id=None 时用 track_id，防图谱前端空 id 伪节点；对齐既有先例 L2040）
  - `5283838f`：删除 message_injector.py 死代码（4 函数生产零调用——generate_doc_id 生成的正是 `refined:{date}:{seq:03d}`，同一错误抽象的化石）+ 其唯一测试
  - `0104fdeb`：新测试 tests/test_inject_document_changelog.py（3 用例：doc_id=None→id==track_id / 显式 doc_id→id==doc_id / rag None→不 record_change）
- **验证**：新测试 3 passed；adapter 套件 9 failed 与基线完全一致（pre-existing 陈旧测试）；实施后双审 APPROVE（唯一 P3 = 本条日志补录）
- **已知边界**：11:08 被吞内容不补救（游标已推进，内容价值低且部分过时）；同内容提炼仍会被合理去重（MD5 撞车=设计语义）；file-processor/主 Agent 经 disk 仍可传 doc_id（未教学、行为=修复前，接受）；撞车保持不可观测（程序调用方无法解释错误——用户拍板）
- **实机验证（待用户）**：新对话产生游标后新消息 → 自然睡眠或 /sleep → doc_status 出现 `doc-xxxx` 新条目（content_summary 含"记忆提炼"）且 status=processed；图谱前端 changelog 无空 id 伪节点
