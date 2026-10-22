# AGENTS.md — Niu (niu-agent) 权威项目指南

> **本文件是项目的权威指南，每次会话必读。** 记录架构、开发规范与历史更新日志。

核心架构：**Electron 33 前端 + Rust 启动器 + Python Agent 核心 + 多个 MCP 服务器** 的混合架构。

**核心特色**：MCP 虚拟磁盘 — 把 100+ MCP 工具的 Schema 映射为 Unix 风格虚拟文件系统，Agent 用单一 `disk()` 工具以 `ls`/`cat`/路径调用的方式使用所有工具，彻底解决 MCP 工具爆炸导致的上下文占用问题。

**核心架构**：
```
用户界面 (Electron 33)
    ↓ HTTP/SSE
Rust 启动器 (launcher/)  ← Iced 仅用于 Splash 启动画面
    ↓ 启动 + 监控
Python API 服务 (niu_api/)
    ↓ 调用
Agent 核心 (agent/generic/)
    ↓ MCP 协议
MCP 服务器集群 (mcp-servers/)
```

---

## ⛔ 不可违反的铁律（每次对话必读）

1. **你是项目经理** — 不要自己遍历代码，把控全局，减少无价值上下文占用。
2. **禁止自己改代码** — 所有代码修改必须委托给子 Agent 执行，主对话只做分析和决策。
3. **修改前必须先做临时提交备份** — `git add -A && git commit`，恢复前也必须先备份当前状态，不能直接 `git checkout` 覆盖；完整回退到过去的某点必须经过用户同意。
4. **修改前必须用 先 分析影响范围** — 评估 blast radius 后再动手。
5. **测试必须用真实数据 + 真实 LLM** — 绕过 LLM 的测试是假测试。
6. **`python/` 目录必须是完整的自包含 Python 安装** — 所有二进制、库、依赖必须真实存在于 `python/` 目录内，禁止符号链接指向外部路径（如 `/Library/Frameworks/Python.framework/`）。此目录最终要打包分发，客户无需自装 Python 环境和依赖。当前 `python/` 目录的 stdlib 仍指向系统 Python（自包含 stdlib 尚未复制进来），需另开会话重建。**［2026-10-16 PM 实测复核：此句已过时——stdlib 现为真文件（非软链）、`python/bin/python3` 是真二进制、`python/` 下**零个绝对目标软链**；`otool -L python/bin/python3` 走 `@rpath/libPython3.11.dylib`（rpath=`@loader_path/../lib` ⇒ 解析到包内 `python/lib/libPython3.11.dylib`，该 dylib 仅依赖 System 框架）⇒ **运行时似已自包含**。残留仅为**元数据**：`sys.base_prefix` 与 `sysconfig` 的 `LIBDIR`/`LIBPL` 仍记 `/Library/Frameworks/...`（只影响"运行期编译/链接 C 扩展"，不影响纯 Python 运行）；**已查实**（2026-10-16）：运行时代码（`agent/`、`niu_api/`、`mcp-servers/`）**零处**使用 `sysconfig`/`base_prefix`/`python-config`，且包内解释器自检通过（`prefix`=包内 python、stdlib 就位）⇒ **确认为惰性元数据**；**尚未在无系统框架的机器上实测**。⇒ 待办收敛为"（可选）洗掉 sysconfig 里的框架路径"，不再是"重建 stdlib"。］**numpy<2 和 opencv<4.12 是隐性约束（torch 2.2.2 / insightface C 扩展用 numpy 1.x ABI；opencv 4.12+ 强制 numpy>=2）。
7. **git 操作后必须修复文件权限** — `git checkout/reset` 会丢失可执行权限，执行后必须运行：
   ```bash
   find python/bin/ -type f -exec grep -l '^#!' {} \; | xargs chmod +x
   find ui/*/node_modules/.bin/ -type f ! -perm -u+x -exec chmod +x {} \;
   find models/llama.cpp -type f -name "llama-server*" ! -perm -u+x -exec chmod +x {} \;
   ```
8. **Rust 启动器编译必须用 `bash launcher/build.sh`，禁止直接 `cargo build`** — `cargo build` 只输出到 `launcher/target/debug/`，不会复制到项目根目录的 `niu`，导致测试用旧二进制。`bash launcher/build.sh` 编译后自动 `cp target/release/niu-launcher ../niu`（并刷新/重签 `niu.app`、重装 `niu-natives` wheel、把 `VERSION` 拷进 bundle）。每次改 Rust 代码（`launcher/src/`）后必须跑它。**注意必须带 `bash` 前缀**：`launcher/build.sh` 无执行位（`-rw-r--r--`，2026-09-13 实测），写成 `./launcher/build.sh` 会 `permission denied`。此铁律必须传达给派出去的子 Agent。
9. **方案/计划等私有文档（`docs/superpowers/` 整个目录）在独立 git 仓库管理（2026-08-16 起，替代原 plans 分支机制——分支切换会导致运行中 Niu 服务的延迟导入读到旧代码，事故实证 2026-08-16）**：
   - `docs/superpowers/` 是**独立 git 仓库**（内层 `.git/`）——外层 main 仓库的 `.gitignore` 排除该目录（push 天然干净、pull 不删除、`git status` 零干扰）
   - **写方案/计划直接在 `docs/superpowers/` 内提交**：`cd docs/superpowers && git add -A && git commit`——不切分支、无 plans 分支、Niu 服务运行中写文档零影响
   - 读 main 代码用 `git show main:<path>`；审查 Agent 直接读工作区计划文件（禁 pytest、禁改代码、禁跑测试）
   - 实施：在 main 分支执行，与文档仓库无关；实施计划提交只进 main
   - 文档仓库**永远不推送**（本地版本比对用）
   此铁律必须传达给派出去的子 Agent。
10. **禁止用 Python/脚本直接修改任何代码或文档** — 所有文件修改（代码、配置、计划、手册）必须用 **Edit 工具**（先读后改：old_string 不匹配会显式报错，不会静默失败）。**Python 仅限只读分析**（读文件、算数据、grep 统计），禁止 `open(p,'w').write()` 写文件。`python -c 's=open(f).read().replace(...); open(f,"w").write(s)'` 一类批量静默替换**一律禁止**——`str.replace` 的 old_string 不匹配时静默跳过不报错，是"改了但没改对"的根源（2026-08-14 脑区 assign 计划 19 轮审查教训：行号漂移连续三轮、fake 结构修错、改一处漏同步，全因静默 replace）。此铁律必须传达给派出去的子 Agent。
11. 开工前必须读项目流程 Skill（niu-plan-review-gate-and-sdd / niu-sdd-execution-flow）；Skill 必须真读真执行。记住本地Agent优先的约定，只要不需要过大上下文的操作，优先使用本地Agent。双审可以一个本地，一个远端。

12. **发布前必做「全新 Clone 可用么」审计**（2026-10-11 实证教训）：把运行期要读的**本地文件**（模型权重 / tokenizer / 配置模板）逐个问三句 —— **入 git 了吗？有下载脚本吗？文档写了吗？** 三问有一答否就是漏洞。唯一可靠判据是 `git ls-files <path>` **有输出**；`git status` 干净、`git check-ignore` 未命中都不算数（negation 规则 `!xxx` 会让文件"看起来该被跟踪"，实际却躺在工作树里）。已实证踩坑：`models/tokenizers/deepseek-v3/tokenizer.json` 被「目录 ignore + `!models/**/*.json` 兜着但没人 add」的组合漏出版本库，装机端 token 计数静默降级、只有一条易漏 WARNING。**审计完必须重打安装包并挂载验证产物在位。**
13. **一次性产物名与第三方启动参数严禁写死**（用户 2026-10-15 当面指出的原话口径）——随上游发版而变的一次性下载产物名（压缩包名等）、上游构建号、第三方 server 的启动方式与参数，**一律不得写进程序代码**——必须走**配置文件/数据文件**（本项目落点：`~/.niu/config/user-config.json` 的 `llama_cpp.command` 行、`memory/llama-layout.<os>.json` 出厂种子）。**自查法**：写出一个常量前问一句——「这个值会随环境/上游时间而变吗？变了要不要改代码？」答案是「要」就说明写法错了。同一事实**只允许一处真相**：同一个值（如 llama-server 端口）不得多个进程/模块各留一份常量副本，一律从同一来源读取（Python 与 Rust 启动器都读配置里那一行，不许两处硬编码）。新增配置/数据文件先问三句：① **它在签名区吗？**（包内 ⇒ 错——macOS 上改包内文件即签名失效）② **它是首启必需吗？**（是 ⇒ `memory/` 必须有出厂种子）③ **用户会改吗？**（会 ⇒ **家目录那份才是权威**，包内只作种子）。两条硬约束：**包内只读、程序永不写包内**。

**违反任何一条就停下来，不要继续。**

---

## ⚡ 项目管理纪律（2026-09-09 用户当面定稿——每次会话必读）

### 当前工作：可视化功能（重做版，用户大白话需求）
1. 目标：Agent 能截屏、能看图——截图工具抓屏图，丢给能看图的模型拿回文字结论（Phase 1 niu-natives 抓图底座已交付、用户真机验证通过，勿动）
2. 视觉能力探测**只问主模型**：在现有「探测能力」流程顺带测（纯红测试图问主色判定），结果记能力 `input: ["text","image"]`；**设置页不加任何 UI、不加按钮、一个字符不改**
3. 有能看图的模型才挂截图/看图工具；没有能看图的模型 → 工具不出现
4. 主模型不能看图时，用户自行配置第三方视觉模型：**测试方法与配置方法写进 SYSTEM_MANUAL**，主 Agent 读手册自己测通、自己配；**程序不自动探测第三方模型**
5. 用户手工配置的配置段**必须持久保留**——切换其他模型、设置页保存任何配置都不得覆盖丢
6. 禁止项（2026-09-09 已全部回退重做，勿再犯）：设置页视觉模型 UI/表单；程序自动探测第三方视觉模型；保存链自动触发探测
7. 动手前必须用大白话与用户对齐需求；方向做错必须立即承认并彻底回退（Phase 2 错 12+ 小时被全撤的教训）

### 任务派发硬门禁（本地模型优先——用户连续多次提醒）
- Niu 项目任务派发：**单 Agent = `tasks[].agent` 显式 `"local-vision"`**（自包含任务、128K 内）；**双 Agent（双审）= A 角 local-vision + B 角远端 reviewer**；禁双远端、禁双本地并行（local-vision 单槽位排队）
- **`write` 唤醒一个已结束的 Agent = 又派了一个本地 Agent（2026-10-13 实测教训，用户从机器负载上抓出）**：给已完成的 Agent 补要求，**必须按"新派一个"对待**——本地**任何时刻只允许一个在跑**。实证：PM 用 `write` 去"催"一个已跑完的 local-vision（本意只是补要求），把它**重新唤醒**，与随后新派的第二个本地 Agent **同时在调那台局域网模型**，用户看到"不止一个在抢资源"并手动停掉 ✗。⇒ **催办不许用唤醒**：要改就直接落到文件/新任务里；要确认它改了没，**读 `git diff` / `history://<id>` transcript，不唤醒** ✗。
- **单 Agent 工作禁止远端模型（用户 2026-09-11 当面定）**：远端无法从外部判断「在干活 vs 卡死」（实证：远端 reviewer 跑 50 分钟 41 次工具调用无输出，PM 误判卡死 hard-abort，成果全丢）；本地 local-vision 跑在局域网机器上，风扇/负载可感知。**远端仅用于双审 B 角（与 local-vision 并行，有对照）**。
- **effort 档位（用户 2026-10-22 定）**：远端 Minimax 系模型推理极冗长，派它做审查/分析**必须 `effort: "lo"`**（否则一小时也回不来）；其余模型（local-vision / 其他远端）做审查、逻辑推理类工作**一律 `effort: "hi"`**。
- **验证归 PM、双审归 Agent**：diff / 跑验证脚本 / grep 核对属 PM 自己的活，不派 Agent（派 Agent 的价值=独立视角找缺陷，不是复述指令）。
- **取消 Agent 前必读 transcript 收割成果**：硬取消无法唤醒，但 jsonl transcript 保留（`~/.omp/agent/sessions/<proj>/<sess>/<name>.jsonl`）——提取 digest 喂给新 local-vision 续做，实测 2m44s 补完前任 50 分钟未成文的活。
- 系统模板默认 scout 是远端——Niu 项目不得套模板默认；每份派发逐个 tasks[i].agent 显式写。**靠门禁不靠记忆**
- **给 local-vision 派单不得强制任何输出格式（2026-10-13 实测确认）**：NInfer 服务器**只支持 `response_format:{"type":"text"}`**，`json_object` / `json_schema` 一律被拒（`response_format_not_supported`："requires constrained output, which NInfer cannot guarantee"）—— 它不做约束解码。⇒ 一旦在任务书/prompt 里要求"必须按某格式返回 / 必须给结构化结论"，请求被拒、Agent 交白卷（实证：同一份 spec 审查连开两轮，两轮都只 yield 一行元信息或畸形空壳，看着像"模型笨"，实为服务器能力所限）。**做法：要它审查/分析就用自然语言分节自由写**；需要结构化结果时**由 PM 事后抽取**，或改用支持约束输出的模型。这也意味着 **给 local-vision 设 `outputSchema` 是无效且有害的**。
- **更稳的做法：让审查/分析类任务把结论直接落盘（2026-10-13 用户提，采纳为默认做法）** ⇒ 派单里写明"把结论用自由 markdown 写到 `docs/superpowers/reviews/<YYYY-MM-DD>-<topic>.md`（目录不存在就建），**yield 只回一行文件路径**"。好处：① **工具调用不受约束输出限制**，彻底绕开上面的服务器限制；② 内容在任务中途就落盘 ⇒ 即便最终 yield 仍是空的，成果也不丢（优于事后收割 transcript）；③ 报告进 `docs/superpowers/` 独立仓，**留痕可复查可 diff**；④ PM 用 `read` 读文件做裁决，不依赖任何结构化返回。**审查类、调研类、多轮分析类任务一律这么派。**

### 审查门禁（按产物分层，2026-10-20 用户再次确认）
- **通用**：每轮 2 个异角度并行（A=设计/逻辑链/场景走查；B=技术可行性/事实核查/风险）；审查员输出限行数（≤15-25 行）防流中断，prompt 要求按完整逻辑链分析，不只盯一条语句。
- **无代码设计 / spec**：**至少做 1 轮标准审查**（A/B 逻辑链、可行性、风险），**不要求连续两轮**，**不要求双 APPROVE**；由 **PM 裁决**，标准是**大逻辑框架不跑偏、P1/P2 已处理或明确接受**。spec 审查不阻断进入 plan；但**不等于可以跳过审查直接实施**。
- **带全量代码的 plan**：**A/B 双审 + 连续两轮零 P1/P2** 才通过门禁；每轮修订版本号 + 修订记录 + commit。按子方案粒度判，拆得好的简单子方案本地审查足够。
- **通用"连续两轮"只适用于带全量代码的 plan，严禁套到无代码 spec 上**；反过来，无代码 spec 也不得因为"没有代码"就跳过审查。
- **远端模型按需**：按被审对象的逻辑复杂度决定是否叫远端（并发/线程模型/跨进程/状态机这类才值得），不要一刀切成"双审就必须一个远端"；设计文档与简单子方案一律本地。
- 推论：**审查是手段不是仪式** —— 拿不准的复杂度就上重流程、拿得准的就别浪费轮次；PM 对"该用多重"负判断责任。

### 工程流程要求
- 中等以上改动（跨 MCP 工具签名/多调用点/子 Agent 提示词）：plan 文档（`docs/superpowers/plans/`）→ 双审门禁 → SDD（每 Task 新鲜子 Agent + spec/quality 两阶段审查 + 微修闭环）→ 实机验证；PM 复核后才 commit
- **方案变更绝不允许直接实施**（无论看起来多小——用户原话「每次这么做完了都会带来一堆的 bug」）；用户拍板方案细节 ≠ 授权实施，拍板只是 plan 输入
- 测试纪律：只跑点名文件、全 mock 禁真实 LLM/图谱写入；实施后立即验证不拖延
- 高频炸点（历轮实证，plan 必须核查）：MCP 工具参数改动=TOOL_SCHEMAS+直调实现+stdio Tool() 副本+dispatch 分派多处同步；函数级 import 块随删随收缩；运行时配置 copy-once 到不了存量装机；游标/水位线失效需 fail-loud 兜底

---

## 工作原则

1. 修改代码必须经过用户同意，说清楚修改的原因。
2. 未经同意，不得覆盖仓库内任何备份。
3. 从仓库恢复代码时，先回忆上次备份的内容；不确定就不能盲目恢复。
4. 遍历仓库历史测试原历史代码时，先把当前代码做临时提交。
5. 代码调试过程中验证无效后，必须马上撤销调试代码，恢复原始干净代码，再增加新的调试代码。
6. 项目代码量较大，为保护上下文窗口，无需长期记忆或大代码量的遍历工作交给子 Agent。
7. 代码质量优先，用户不在乎 token 消耗。
8. 版本号变更**只改根目录 `VERSION` 文件一处**（单一真相源，0.3.5 起）：`ui/main/windows/assistant/chat.html` 的版本 label 经 `preload-chat.js` 的 `APP_VERSION` 常量、`niu_api/compat.py` 的 `User-Agent: Niu/<版本号>` 经 `_read_version()`、`tests/test_list_models_endpoint.py` 的 UA 断言动态读 `VERSION`，全部自动同步。**未来新增版本号消费点一律读 `VERSION`，禁止新增硬编码副本**。Cargo.toml、package.json、pyproject.toml 等文件中的 version 字段是各子包的开发版本号，与产品版本号语义不同，**不要**强行统一。
9. 私有文档（`docs/superpowers/` 整个目录）遵循铁律 9：在**独立 git 仓库**（`docs/superpowers/` 内层 `.git`）编写与提交（有 git 历史供多轮审查），该仓库永不推送；main 通过 `.gitignore` 排除该目录，push 天然干净。

---

## 开发环境设置

### 前置要求

- **Go**: 不再使用（历史遗留的 `main.go` / `pkg/` 已移除或废弃）。
- **Rust**: 用于启动器（`launcher/`，含 Iced Splash 启动画面）。
- **Node.js**: 用于 Electron 前端（`ui/main/`），建议 LTS。
- **Python**: 3.11+（Agent 和 MCP 服务器）。
- **SQLite**: 会话持久化。
- **.NET 8 SDK**（Windows 构建磨砂模糊原生件 `native/niu-winfx-win` 用；最终用户不需要——产物自包含，随 7z 包分发）

### 安装依赖

两套依赖必须都装：
1. **Python 依赖**（Agent 核心 + MCP 服务器）→ `python/` 自包含环境
2. **Electron 前端依赖**（`ui/main/node_modules`）→ `npm install`

```bash
# 1. 创建自包含 Python 运行时（venv + 全量依赖）
# macOS
python3.11 -m venv --copies python
python/bin/pip install --upgrade pip
python/bin/pip install -r requirements.txt

# Windows（用完整路径指定 Python 3.11）
C:\Python311\python.exe -m venv --copies python
python\Scripts\pip.exe install --upgrade pip
python\Scripts\pip.exe install -r requirements.txt

# 2. 安装 Electron 前端依赖
cd ui/main && npm install && cd ../..

# 3. 开发/测试依赖（可选，不进入分发包）
# macOS: python/bin/pip install -r requirements-dev.txt
# Windows: python\Scripts\pip.exe install -r requirements-dev.txt
```

MCP 服务器不需要 `pip install`，通过 `config/mcp-servers.yaml` 的 `workdir` 配置即可加载模块。
Rust 启动器在 Windows 上通过 `cmd /C npm start` 拉起 Electron，`node_modules` 不存在会导致设置窗口无法弹出、启动器直接退出。

### 运行项目

**完整启动**：
```bash
./niu   # 直接运行编译好的二进制
```

**单独启动前端**：
```bash
cd ui/main && npm start   # 前端是独立 Electron 进程，由 Rust 启动器自动拉起
```

**单独启动 Python API**：
```bash
python -m niu_api
# API 端口默认 9876，可通过环境变量 NIU_API_PORT 修改
```

Windows 开发环境注意：不要在 shell 里设置 `PYTHONIOENCODING`——它优先于 UTF-8 模式（`PYTHONUTF8`），会把 stdio 错误处理收紧成 strict；启动器已注入正确取值，手动启动保持默认即可。

### 打包发布

**macOS .app + DMG 打包**（由 `launcher/build.sh` 自动完成；**脚本无执行位，必须用 `bash` 前缀**）：

> ⛔ **打包前必做：先下模型产物**（`build.sh` 自己不会下）。`models/llama.cpp/` 在
> `.gitignore` 里、`models/embeddinggemma-2-Q8_0.gguf` 也未入 git → 全新克隆上一个都没有。
> 跳过这步会产出**没有向量模型的 bundle**，用户启动直接卡契约 A。
> ```bash
> python scripts/package_all_dependencies.py
> ```

```bash
bash launcher/build.sh          # 只打 .app bundle（开发调试用）
bash launcher/build.sh --dmg    # 打 .app bundle + DMG 安装包（发布用）
```

`build.sh` 会：cargo build → 构造 `niu.app/`（复制资源 + 签名 + LaunchServices 注册 + quarantine）→ 可选生成 DMG（`dist/Niu-${VERSION}-mac-intel.dmg`）。

**关键约束**：
- 必须用 `bash launcher/build.sh`，禁止直接 `cargo build`（铁律 8；脚本无执行位）。
- 重打 DMG 前必须先 `rm -rf niu.app`——rsync `--delete --exclude` 会保护被 exclude 的旧文件不删除（许可证合规排除的 igraph/buffalo_l onnx/字体 ttf 等），删掉重打才干净。
- DMG 产物在 `dist/Niu-<VERSION>-mac-intel.dmg`，VERSION 从根目录 `VERSION` 文件读。
- M 系列 Mac 打包：必须在 arm64 host 上 `pip install` / `npm install`（不能 cross-compile），详见 `docs/manual-installation.md`。

**DMG 生成流程**（build.sh 内部）：
1. 准备临时目录 `/tmp/niu_dmg_stage_<pid>/`
2. 软链 `Applications`（支持拖拽安装）
3. 复制 `niu.app` 到临时目录
4. `hdiutil create -format UDZO -imagekey zlib-level=9` 生成 DMG（zlib 压缩，~3.3G bundle → ~1.2G DMG）
5. 清理临时目录

**许可证合规排除**（build.sh 的 rsync exclude）：
- `python/` 排除 igraph/leidenalg/texttable（GPL）
- `models/` 排除 `buffalo_l/*.onnx`（非商业许可；程序内**禁止自动下载**——本地缺失时人脸识别直接报错并提示按 README「可选：启用照片处理」手动安装）
- `ui/main/` 排除阿朱泡泡体 ttf（许可证存疑）

**Windows 绿色包打包**（由 `pack.bat` 完成）：

> ⛔ **打包前必做：先下模型产物**（`pack.bat` 自己不会下）。`models/llama.cpp/` 在
> `.gitignore` 里、`models/embeddinggemma-2-Q8_0.gguf` 也未入 git → **全新克隆上一个都没有**。
> 跳过这步会产出**没有向量模型的 7z**，用户装上直接卡在契约 A「向量模型产物缺失，
> 程序无法启动」。
> ```cmd
> python scripts\package_all_dependencies.py
> ```
> 按当前平台下载 GGUF（约 310 MB，含 sha256 校验）+ llama.cpp 二进制，落到
> `models\` 与 `models\llama.cpp\win32-x64\`。macOS 打包同样要先跑这条（见上一小节）。

```cmd
pack.bat
```
Windows 是绿色安装，用户解压 7z 即用，无需安装程序。前置：已安装 [7-Zip](https://7-zip.org/)（`pack.bat` 按序探测：`where 7z`（PATH）→ `%ProgramFiles%\7-Zip\7z.exe` → `%ProgramFiles(x86)%\7-Zip\7z.exe`，都没有才中止——**2026-10-16 起不再硬编码 `C:\`/`E:\`**）。打包前需已完成：Rust 编译（`bash launcher/build.sh` 或 `cargo build --release` + 复制 `niu-launcher.exe` 到根目录 `niu.exe`）、`npm install`、Python venv 创建、`pip install -r requirements-dev.txt`（提供 maturin）、.NET 8 SDK 安装（`dotnet --version` 可用，供 `pack.bat` 调用 `native\niu-winfx-win\build.ps1` 构建磨砂模糊原生件——`pack.bat` 硬依赖 dotnet，缺则立即中止）。

`pack.bat` 会：
1. 构建 niu-natives wheel 装进 `python\`（缺 `.pyd`/`node_modules` 即中止，不产残包）
2. 自动清理 `launcher/target/`、`__pycache__/`、`*.pyc`（不进 7z，也不需要保留）
3. 用 robocopy 复制文件到临时目录，排除 `.git/`、`backup/`、缓存目录等
4. 用 7-Zip 压缩（LZMA2 -mx=9，压缩率高于 zip）
5. 产物在 `dist/Niu-<VERSION>-win-x64.7z`，VERSION 从根目录 `VERSION` 文件读

### 测试

```bash
cd agent && pytest
```

Windows（或非 UTF-8 locale 环境）下跑测试建议以 UTF-8 模式运行：`PYTHONUTF8=1 pytest`——未显式指定 encoding 的文本 IO 跟随进程默认编码，ANSI code page 下中文内容会解错。

### 代码检查

```bash
cd agent
ruff check .      # Python 代码检查
ruff format .     # Python 自动格式化
```

---

## 核心架构

### Agent 核心（`agent/generic/`）

**核心文件**：
- `agent_loop.py` — 主循环 + V4 逐轮 persist 推送 + chat_busy/chat_idle 状态机
- `handler.py` — 工具实现 + 工作记忆机制
- `llmcore.py` — LLM 抽象层，支持多厂商

**重要机制**：
1. **工作记忆**：`tool_after_callback` + `_get_anchor_prompt` + `next_prompt_patcher`
   - 每 35 轮强制询问用户
   - 每 7 轮警告避免无效重试
   - 保留最近 20 条工具调用摘要
2. **思考链处理**：统一处理 DeepSeek/MiniMax/Qwen/Claude/OpenAI o1 的思考链格式
3. **Token 统计**：`MockResponse.usage` 返回 input/output/total_tokens

**适配层**：
- `session_adapter.py` — Session 隔离 + SQLite 持久化
- `runner.py` — 整合层（GenericAgentRunner）+ 动态注入架构
- `vector_search.py` — 向量检索适配器
- `thinking_chain.py` — 思考链处理器
- `tool_registry.py` — MCP 工具注册中心（新架构核心）
- `mcp_loader.py` — MCP 模块加载器（新架构核心）
- `mcp_sync_bridge.py` — 同步/异步桥接（已废弃，保留向后兼容）

### MCP 服务器架构

#### MCP 同进程架构（In-Process Architecture）

**架构升级（2026-04）**：
- **旧架构**：MCP stdio 通信（进程隔离，性能低）。
- **新架构**：同进程直接调用（无进程通信，性能提升 ~40000x）。

**核心组件**：
1. **ToolRegistry**（`agent/tool_registry.py`）：全局工具注册中心，管理所有 MCP 工具的注册、获取和 schema 返回，支持 `get_registry().get("server-name/tool-name")` 直接调用。
2. **MCP Loader**（`agent/mcp_loader.py`）：启动时加载所有必需的 MCP 模块，严格验证（任何加载失败将终止应用），支持自定义服务器列表。
3. **TOOL_SCHEMAS 模式**：每个 MCP 服务器模块定义 `TOOL_SCHEMAS` 字典，提供 `get_tool_schemas()` 函数返回 schema 列表，工具函数直接在模块中实现。

**性能对比**：
```
10 次工具调用：
- stdio 模式：~40 秒（进程启动 + JSON-RPC 序列化）
- 同进程模式：~0 秒（直接 Python 函数调用）
- 性能提升：~40000x
```

**使用示例**：
```python
from agent.tool_registry import get_registry

registry = get_registry()
tool_fn = registry.get("memory-server/user_memory_remember")
result = tool_fn(content="用户喜欢 Python", type="memory")  # 直接调用，无需 stdio
schemas = registry.get_schemas()
```

**废弃组件**（保留向后兼容）：
- `MCPSyncBridge`（`agent/mcp_sync_bridge.py`）：保留但不再使用。
- `mcp_client.py` 的 stdio 通信函数：标记为废弃，建议使用 ToolRegistry。

**注册规范**（所有 MCP 服务器必须遵守）：

1. **目录结构**：
   ```
   mcp-servers/<name>/
   ├── src/
   │   └── niu_<name>/
   │       ├── __init__.py      # MCP 工具定义
   │       └── __main__.py      # 入口点
   └── pyproject.toml
   ```

2. **配置要求**：
   - `workdir` 必须指向 `src/` 目录（自动加入 sys.path）。
   - **不需要 `pip install`**，通过 workdir 即可找到模块。
   - `python -m niu_xxx` 需要模块目录下有 `__main__.py`。

3. **配置示例**（`config/mcp-servers.yaml`）：
   ```yaml
   server-name:
     command: ${PYTHON_PATH}  # 装饰性字段：同进程架构下内置服务器经 ToolRegistry 直调不执行 command；仅外部 stdio 服务器消费，需写真实命令
     args:
       - "-m"
       - "niu_server_name"
     workdir: ../mcp-servers/server-name/src
     preload: true  # 可选，启动时预加载
   ```

4. **pyproject.toml 模板**：
   ```toml
   [project]
   name = "niu-<server-name>"
   version = "0.1.0"
   requires-python = ">=3.11"
   dependencies = ["mcp>=1.0.0", "loguru>=0.7.0"]

   [project.scripts]
   niu-<server-name> = "niu_<server_name>:main"

   [build-system]
   requires = ["hatchling"]
   build-backend = "hatchling.build"
   ```

**已实现的 MCP 服务器**：

| 服务器 | 功能 | 预加载 |
|--------|------|--------|
| `file-parser` | 文档解析（PDF/Word/PPT/Excel/MD/HTML） | ✅ |
| `lightrag-server` | 知识图谱 + 向量检索（LightRAG 统一管理） | ✅ |
| `photo-server` | 照片管理 + 人脸识别（InsightFace） | ✅ |
| `config-manager` | 配置管理（读/写用户配置和记忆） | ✅ |
| `memory-server` | 用户长期记忆和工作便签（permanent array 10 条） | ✅ |
| `session-manager` | 会话管理（消息压缩） | ❌ |
| `browser-server` | 浏览器自动化（WebSocket Bridge + 系统 Chrome，CDP 协议） | ✅ |
| `brain-region-server` | 脑区激活/调暗/状态管理 | ✅ |
| `scheduler-server` | 定时任务调度（增删改查） | ❌ |
| `feishu-server` | 飞书消息收发（可选） | ❌ |

**Browser-Server 架构**：
- 旧架构（已废弃）：`playwright.async_api` 守护线程模式，playwright 库已从依赖中移除。
- 新架构：WebSocket Bridge + 系统 Chrome（CDP 协议）。
- 核心文件：
  - `mcp-servers/browser-server/src/niu_browser_server/launcher.py` — 启动系统 Chrome（带 remote-debugging-port）
  - `mcp-servers/browser-server/src/niu_browser_server/ws_bridge.py` — WebSocket 桥接 CDP 命令
- 浏览器扩展：`extensions/niu-browser-ext/`（基于 alibaba/page-agent 二次开发），负责页面 DOM 提取和用户交互。
- 优势：不再需要内嵌浏览器，复用用户系统 Chrome（含登录态、插件）。

### 子 Agent 架构

**定义位置**：`config/agents/*.md`

**调用方式**：主 Agent 通过 `chat-with-xxx` 工具调用子 Agent。

**关键实现**：
- `agent/subagent.py` — 子 Agent 工具生成
- `agent/mcp_client.py` — `get_mcp_tools_for_servers()` 按 server 名称过滤工具

**委托规则**：文件处理等耗时任务必须委托给子 Agent（`file-processor`）。

### 动态注入架构

**实现**：
- `agent/injector/sync.py` — Skills 定时扫描同步到向量库
- `niu_api/injector.py` — API 端点手动注册 MCP 工具描述
- `agent/runner.py` — `_inject_dynamic_resources()` 按语义搜索并注入
- `agent/runner.py` — `_on_before_llm()` 每轮 LLM 调用前重读 memory.json + 刷新动态注入（轮次级）
- `agent/runner.py` — `_on_turn_end()` 每轮结束后脑区 decay_all + tools_schema 刷新（轮次级）

**轮次级刷新机制**：
- 每次 LLM 调用前，`agent_runner_loop()` 调用 `on_before_llm` 回调：重读 memory.json 重建 system 静态区，并经 `_inject_dynamic_resources` 向量检索刷新动态块。
- 每工具轮结果 persist 落库后（tool_results 非空）调用 `on_tool_round_refresh` 回调（主 Agent 专用）：从 DB 全量重建视图并原地替换 messages（`assemble_view_sync` + `transform_history`，与入口同一套组装流程——新输出编号/折叠态/仪表盘与 DB 同步；动态块由下轮 `_on_before_llm` 幂等重插）。子 Agent 不传 = None 跳过。
- 每轮循环末尾调用 `on_turn_end` 回调：脑区激活衰减（`decay_all`）+ tools_schema 刷新（`~/.niu/agents/` 有变化时重算 base 集）。

**工具生命周期（衰减-覆盖评分模式）——已退役，非迁移**：
- 旧 `agent/tool_lifecycle.py` 已删除；其工具分数衰减（-10/轮）、向量检索命中覆盖、`hit_tool()` 逻辑未迁入任何模块（生产代码零残留引用）。
- MCP 工具不再参与分数制动态注入/移除：visibility=static 的直接注入 tools_schema，hidden 的经虚拟磁盘 `disk()` 统一访问。

**知识库标签**（LightRAG 统一管理）：
- `l1` — L1 摘要
- `l2` — L2 原文
- `skill` — Skills 文件
- `mcp_tool` — MCP 工具描述

---

## 配置文件架构

### 程序目录 `config/`

| 文件 | 用途 |
|------|------|
| `config/user-config.json` | LLM API Key、模型选择 |
| `config/agents/niu.md` | 主 Agent 定义（提示词、权限、MCP服务器） |
| `config/agents/file-processor.md` | 子 Agent 定义（文件处理专用） |
| `config/mcp-servers.yaml` | MCP 服务器配置（bundle 权威层，随版本升级直读）；用户自定义放 `~/.niu/config/mcp-servers-user.yaml`（deep merge 用户赢，0.3.0 双目录模型） |
| `config/disk/*.yaml` | MCP 虚拟磁盘配置（把 100+ MCP 工具 Schema 映射为 Unix 风格路径，解决工具爆炸问题） |

**MCP 虚拟磁盘**（项目核心特色）：
- 所有 MCP 工具的 Schema 不直接注入 Agent 上下文（避免上下文爆炸）。
- 通过 `config/disk/*.yaml` 映射为 Unix 风格虚拟文件系统。
- Agent 用单一 `disk()` 工具以 `ls /`、`cat /memory/xxx`、`/memory/xxx(params)` 方式调用。
- 详细规范见 `docs/manual-mcp-disk.md`。

### 模型目录 `models/`

| 目录 | 大小 | 用途 |
|------|------|------|
| `models/embeddinggemma-2-Q8_0.gguf` | ~310 MB | EmbeddingGemma 2 向量模型（768d，8192 ctx，GGUF Q8_0，经 llama.cpp 推理） |
| `models/llama.cpp/<platform>/` | ~5 MB | llama-server 可执行体（darwin-x64 / win32-x64 等） |
| `models/models/buffalo_l/` | ~326 MB | InsightFace 人脸识别 |

**加载逻辑**：EmbeddingGemma 2 走 llama.cpp 子进程（`llama-server`），产物路径由
`niu_api/internal/embedding_artifacts.py` 单一定义；`embedding.py` 的 `SUPPORTED_MODELS`
现只剩 `embeddinggemma-2` 一项，`DEFAULT_MODEL` 亦为它。旧 bge-minilm 系列已移除，
存量 `~/.niu/preferences.json` 里的旧 `embedding_model` 值会被既有守卫自动回落。

### 用户目录 `~/.niu/`

| 文件 | 用途 |
|------|------|
| `memory.json` | 用户记忆（身份、偏好、工作目录） |
| `preferences.json` | 存储配置（分类、路径结构、冲突阈值） |
| `config/llm-configs.json` | LLM 命名配置合集（选择设置保存的命名配置，键=配置名=llm.presetId，条目=llm+lightrag_llm 两段快照） |

---

## 关键技术点

### MCP 工具调用规范

**推荐：使用 ToolRegistry 同进程调用（新架构）**：
```python
from agent.tool_registry import get_registry

registry = get_registry()
tool_fn = registry.get("server-name/tool-name")
result = tool_fn(param1="value1", param2="value2")
```

**废弃：使用 stdio 通信（旧架构）**：
```python
# 已废弃：stdio 通信（性能低，不推荐）
result = await call_mcp_tool("server-name/tool-name", {"param1": "value1"})
```

**注意事项**：
- InsightFace/ONNX Runtime 在异步环境中可能导致问题，建议使用同步调用。
- ToolRegistry 已经是同步架构，无需 `asyncio.to_thread`。
- 所有新代码应使用 ToolRegistry，避免 stdio 通信。

### 人脸识别模型管理

**内存管理**：
- InsightFace 模型加载后占用 ~326MB 内存。
- 空闲 5 分钟自动卸载（`MODEL_IDLE_TIMEOUT_SECONDS = 300`）。
- **不要在卸载时调用 `gc.collect()`**：可能导致崩溃。

**预加载机制**：
```python
# 在 MCP stdio 启动前预加载 cv2 和 InsightFace 模块代码
preload_face_model()
```

### 历史对话管理

**消息顺序**：最旧在上，最新在下，滚动到顶部加载更多。

**API**：
- `getHistory(limit, before_id)` — 获取历史消息
- `getMessagesBefore(message_id, limit)` — 加载更早的消息

### 上下文窗口管理

**配置**（`~/.niu/preferences.json`）：
```json
{
  "context": {
    "warningThreshold": 0.80,
    "sleepTriggerMinutes": 5,
    "contextWindowSize": 200000
  }
}
```

### Electron 窗口管理

前端是 Electron 33（`ui/main/`），含三套窗口：
- `assistant/` — 主对话窗口（精灵 + 聊天）
- `settings/` — 设置窗口
- `graph/` — 知识图谱可视化（force-graph 渲染）

**关闭流程**：
1. 前端窗口关闭 → 触发关闭事件
2. 调用 `/api/shutdown` 通知 Python API
3. Python API 清理资源
4. Rust 启动器终止所有子进程

---

## 常见问题

### 照片拖入卡死

**原因**：历史上 MCP 走 stdio 时存在此问题。当前 MCP 已同进程化（ToolRegistry 直接调用），此问题已不存在。保留此节作为历史参考。

**历史解决方案**：
1. 将 MCP 工具调用改为同步。
2. 添加 `preload_face_model()` 在 MCP stdio 启动前预加载。

### 主 Agent 工具丢失

**检查点**：
- `config/agents/niu.md` 的 `mcpServers` 列表是否完整。
- MCP 服务器配置是否正确（`workdir` 指向 `src/`）。

### 子 Agent 缺少 MCP 工具

**检查**：`agent/subagent.py` 的 `get_subagent_mcp_tools_schema()` 是否根据 `mcpServers` 配置获取工具。

### 历史对话丢失

**检查**：`niu_api/session.py` 的 API 调用参数是否正确，避免将 `session_id` 当作 `limit` 参数传入。

### 记忆无法保存或检索

**检查**：
1. LightRAG 是否初始化：检查日志中是否有 "LightRAG initialized"。
2. Memory Server 是否正常：`python -m niu_memory_server`。
3. 日志中是否有错误：`tail -f logs/api_stderr.log | grep "记忆|MEMORY|LightRAG"`。

**解决**：
- 检查 LightRAG 工作目录：`~/.niu/lightrag/`。
- 检查数据库路径：`~/.niu/memory.json` 中的 `workspace.path`。

---

## 相关文档

- `docs/SYSTEM_MANUAL.md` — 系统手册（功能列表、架构设计、分册索引）
- `docs/manual-mcp-disk.md` — MCP 虚拟磁盘手册
- `docs/manual-general-subagent.md` — 通用子 Agent 体系（阶段三）
- `docs/personal-assistant-architecture-v2.md` — 产品定位与核心亮点（架构 v2）
- `docs/feature-photo-processing.md` — 照片处理设计
- `docs/feature-file-management.md` — 文件管理设计
- `docs/feature-document-processing.md` — 文档处理设计
- `docs/feature-scheduled-tasks.md` — 定时任务设计
- `docs/note-agent-communication.md` — Agent 通讯技术笔记
- `docs/implementation-L0L1L2.md` — L0/L1/L2 三级存储实现分析
- `docs/design-self-evolution-system.md` — 自我进化系统设计规范
- `docs/USAGE-self-evolution.md` — 自我进化系统使用指南
- `docs/analysis-genericagent-evolution.md` — GenericAgent 进化机制分析
---

## 工程历史归档

**本文件只保留仍在生效的约定与终态**（铁律、项目管理纪律、工作原则、开发环境、核心架构、常见问题、相关文档）。

全部工程日志（2026-08-21 起逐条原文，含每笔 commit / 验证 / 边界 / 教训）在 **`docs/AGENTS-HISTORY.md`**——查旧工程、旧 commit 链、某个机制当时怎么定的：

- `grep -n "<关键词>" docs/AGENTS-HISTORY.md`（含日期节 `### YYYY-MM-DD`，倒序）
- `git log -- AGENTS.md`（本文件的逐次修订）

新增工程日志**不再写进本文件**：直接写在 `docs/AGENTS-HISTORY.md` 的日期节里；只有当某项约定**仍然生效、且每次会话都需要知道**时才提炼进本文件正文。
