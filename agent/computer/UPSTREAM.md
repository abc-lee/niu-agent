# agent/computer 与上游 omp 的对照与跟版手册

> **用途**：记录本仓 `computer` 工具链（对象模型层）与上游 OMP 的**逐面关系**、**全部有意偏离**，
> 以及**上游升级后的搬运逻辑**（照此执行即可跟版）。
>
> **上游基线**：`oh-my-pi` @ `883c9507ff`
> **同族文档**：原生层见 `niu-natives/UPSTREAM.md`（4 类偏差 A/B/C/D）；本文件管**工具/对象/提示词层**。
> 核对方式：逐面比对 + 逐成员覆盖矩阵（见 §1 映射表与 §3 流程）。

## 1. 文件映射表（上游 → 本仓）

| 上游 | 本仓落点 | 适配类别 |
|---|---|---|
| `packages/coding-agent/src/tools/computer.ts`（222） | `agent/generic/assets/tools_schema.json` 的 `computer` 条目 + `agent/handler.py::do_computer` + `_get_computer_session` | 语言/绑定适配（JS 工具 → Niu 内置 `do_*` 工具） |
| `packages/coding-agent/src/tools/computer/worker.ts`（753） | `agent/computer/objects.py`（`desktop`/`Win`/`El`/`Clipboard`）+ `agent/computer/session.py` | 语言适配（JS class + 持久 worker → Python class + 持久命名空间） |
| `.../computer/{supervisor.ts,worker-entry.ts,protocol.ts}` | `agent/computer/session.py`（持久命名空间 + 单活动 run + 超时） | **结构性差异**：无独立 worker 进程（见 §2①） |
| `.../computer/exposure.ts` | 无 | 有意偏离：Niu 的 `computer` 恒为内置工具，无 unavailable 分支 |
| `packages/coding-agent/src/prompts/tools/computer.md`（26 行） | `tools_schema.json` 的 `computer.description`（逐行移植） | 逐行适配（替换表见 §3 S8） |
| `packages/coding-agent/src/prompts/system/computer-safety.md`（14 行） | `agent/runner.py::_COMPUTER_SAFETY_PROMPT`（byte-identical） | 逐字一致 |
| `packages/coding-agent/src/eval/js/shared/prelude.txt`（`read`/`write`/`tool.*`/`env`） | 无 | 有意偏离：Niu 有独立 `read`/`write`/`bash` 工具，代码内用 Python `open()`；描述已删这些名字 |
| `crates/pi-natives/src/desktop/error.rs:4-19`（14 码） | `niu-natives/src/desktop/error.rs` + `agent/computer/errors.py`（恢复句） | 逐字一致 + 恢复句从上游 `docs/tools/computer.md` 恢复表取 |
| `docs/tools/computer.md`（164） / `docs/computer-use.md`（152） | `docs/SYSTEM_MANUAL.md`「桌面操作（computer 工具）」节 + `docs/manual-troubleshooting.md` 1.11 | 文档面（语义照抄，写法面向用户） |
| `crates/pi-natives/src/desktop/**` | `niu-natives/src/desktop/**` | 见 `niu-natives/UPSTREAM.md` |

## 2. 结构性差异（三条，不可消除，只能显式登记）

① **无独立 worker 进程**：上游每会话一个 worker，超时/崩溃可杀进程重建并回 `RESTART_MESSAGE`；本仓 `code` 在 Niu 进程内的持久命名空间执行，Python 线程不可 kill。
　→ 现状：数字 `wait` 受 run 预算约束；忙时错误附事实（已运行时长/是否超预算），超阈值升级为「previous run hung — session requires restart」；**不提供模型可用的强制解锁**（保单活动 run 不变式）。
　→ 跟版注意：上游若改 supervisar 语义或 `RESTART_MESSAGE` 文案，只评估是否同步 busy/超时文案，不引入进程模型。

② **`code` 是 Python 而非 JS**：方法名与语义不变，语法适配（async→sync、camelCase→snake_case、无 `await`、`raise()`→`raise_()`）。
　→ 后果：上游 JS 里"末尾 `w = …` 作为表达式返回值"在本仓通过 AST 特判实现；`assert` 是 Python 语句（**括号写法 `assert(x, y)` 会被当成恒真元组**，运行时守卫会报错并提示正确写法）。

③ **无审批体系**：上游 `computerApproval` 把 `read_only: true` 映射到 `read` 审批级；本仓无审批基建，只保留 `worker.ts:154-157` 的**运行时闸门**（`read_only` 时一切输入/变更方法拒绝）。描述中 "lighter approval" 已删。

### 已登记的有意偏离（逐条）

| # | 偏离 | 理由 |
|---|---|---|
| D1 | 描述 In scope 行删 `read`/`write`/`tool.*` | 上游靠 prelude 注入；本仓无此物，描述必须=运行时真值 |
| D2 | 描述截图行改为「不自动显示像素，需 `analyze_image(image_path, question)`」 | Niu 工具结果无图像通道（`message_sanitizer` 会省略非 user 的图片块） |
| D3 | 描述补 Win/El **字段清单**两句 | 取自上游 `docs/tools/computer.md:69/:95`（上游把字段写在文档而非提示词里） |
| D4 | `win.bounds` 是字段（`w.bounds`）、`El.bounds()` 是方法 | 与上游一致（`worker.ts:317` / `:256`），非偏离 |
| D5 | 入参未加 `additionalProperties:false` → 改为 `do_computer` 显式拒绝未知键 | 上游 `"+": "reject"` 的等价落地（Niu schema 层无严格模式） |
| D6 | 捕捉上限只传 `max_width`（上游另有 `max_height` 896 坐标安全上限） | 与 vision-server 同口径（1280），见原生台账 |
| D7 | 无 `protocol.ts` 的 artifact 落盘 / `computer-renderer` | Niu 走 30K 全局截断 + `analyze_image`，不落 artifact |
| D8 | `assert` 括号写法反守卫 | Python 语句语义差异，运行时纠错（见 §2②） |

## 3. 上游升级流程（跟版照做）

> 纪律：**先 diff 后动手；能整文件 cp 的绝不手改；任何新偏离必须在 §2 登记**。
> 历史教训：本工具曾因"自己发明等价物"12 轮不收敛，改为"逐条照抄 + 偏差登记"后迅速收敛。

```bash
UP=/Users/lilei/tools/oh-my-pi                     # 上游只读镜像
R=/Users/lilei/tools/ai-bot                        # 本仓
```

**S1 取新基线**
```bash
cd $UP && git fetch --all && git log -1 --format='%H %ci %s' origin/main
```
记下 NEW_BASE，写进本文件表头与 `niu-natives/UPSTREAM.md` 表头。

**S2 变更面清单（先分类再动手）**
```bash
cd $UP && git diff --stat 883c9507ff..NEW_BASE -- \
  crates/pi-natives/src/desktop \
  packages/coding-agent/src/tools/computer.ts \
  packages/coding-agent/src/tools/computer \
  packages/coding-agent/src/prompts/tools/computer.md \
  packages/coding-agent/src/prompts/system/computer-safety.md \
  packages/coding-agent/src/prompts/system-prompt.md \
  docs/tools/computer.md docs/computer-use.md
```
按四类分桶：① 原生 ② 工具层/对象模型 ③ 描述+提示词 ④ 纯文档（只有④ → 直接走 S11，不动代码）。

**S3 原生层三态 + 装机验收** → 走 `niu-natives/UPSTREAM.md` 的流程（含 `cargo test`、Windows 目标 `cargo check`、停 Niu 后重装 wheel）。
**注意**：台账数字会随本仓改动过期 —— **跟版第一步必须重跑 S3 的三态 diff，不要相信台账里的行数**。

**S4 对象模型逐成员比对（工具层最易漏）**
```bash
grep -n '^class \|^\tasync \|^\t[a-zA-Z]*(\|^\treadonly ' \
  $UP/packages/coding-agent/src/tools/computer/worker.ts
```
- 成员**新增** → `objects.py` 同名 snake_case 落点实现：身份挂对象、句柄保持活对象、**值返回 JSON 原生 dict/list**；禁写 dict/attr 双兼容分支。
- 成员**删除** → 同步 `objects.py` + 描述 + 手册 + `errors.py` 恢复句。
- **签名变更** → 描述行与手册提示词包同步。
- 验证：`python/bin/python -m pytest tests/test_computer_objects.py -q`，并**照描述真跑一遍**（描述里写的每条调用都真执行一次，确认真实返回类型与描述一致）。

**S5 描述/提示词重放（逐行，禁止整段替换）**
以 `$UP/.../prompts/tools/computer.md` 为源，逐行套替换表，再补 §2 D3 的字段清单两句：

| JS（上游） | Python（本仓） |
|---|---|
| `top-level await` | 删除（同步调用） |
| `Promise<X>` / `await x` | `X`（去壳） |
| `msOrFn` / `idOrFilter` | `ms_or_fn` / `id_or_filter` |
| `focusedWindow` / `doubleClick` / `setValue` / `elementAt` | `focused_window` / `double_click` / `set_value` / `element_at` |
| `maxDepth` / `nativeRole` / `childCount` | `max_depth` / `native_role` / `child_count` |
| `{timeout?, interval?}` | `timeout=?, interval=?` |
| `raise()` | `raise_()`（Python 保留字） |
| `assert(cond, msg?)` | `assert cond, msg?`（**括号形式恒真**，见 §2②/D8） |
| `read`/`write`/`tool.*`/`env` | 删除（本仓无对应物，见 D1） |
| `lighter approval` | 删除（无审批体系，见 §2③） |

门禁（应零命中）：
```bash
grep -nE 'await|maxDepth|doubleClick|setValue\(|elementAt|focusedWindow|idOrFilter|msOrFn|auto-display|tool\.\*' \
  $R/agent/generic/assets/tools_schema.json
```

**S6 安全段与注入**
```bash
python3 -c "import re,pathlib;u=pathlib.Path('$UP/packages/coding-agent/src/prompts/system/computer-safety.md').read_text().rstrip(chr(10));o=re.search(r'_COMPUTER_SAFETY_PROMPT = \"\"\"(.*?)\"\"\"',pathlib.Path('$R/agent/runner.py').read_text(),re.S).group(1);print('OK' if o==u else 'DIFF')"
```
上游该段仅在 `computer` 工具注册时注入（`system-prompt.ts`），本仓内置工具恒在 → 恒注入；若将来做动态卸载工具，需改成条件注入。

**S7 错误码与恢复句**
```bash
python3 -c "import re,pathlib,sys;sys.path.insert(0,'$R');from agent.computer.errors import RECOVERY;c=set(re.findall(r'^\t([A-Z][A-Za-z]+),$',pathlib.Path('$UP/crates/pi-natives/src/desktop/error.rs').read_text(),re.M));print('MISMATCH',c^set(RECOVERY))"
```
期望空集：上游**新增码必须补恢复句**，恢复句语义只从上游文档取，不新造。

**S8 手册同步**（纯文档变更也走这里）
- `docs/SYSTEM_MANUAL.md` 桌面操作节（对象模型清单/最短流程/坐标与帧规则/delivery/安全边界/提示词包）
- `docs/manual-troubleshooting.md` 1.11（错误码处置）、`docs/manual-user-guide.md` 1.11（macOS 两独立权限）
- 门禁：`grep -n '自动显示\|全分辨率\|maxDepth\|doubleClick\|setValue(' docs/SYSTEM_MANUAL.md docs/manual-*.md` → 零命中

**S9 验收清单（每项要有产出证据）**
1. `cd niu-natives && cargo test`
2. `python/bin/python -m pytest tests/test_computer_objects.py -q`
3. **真机最小链**（需用户在场）：`desktop.windows()` → `desktop.window(id)` → `win.screenshot()` → `win.click/press` → `win.ax()/find()/ref()` → `clipboard.read()`
4. **wire 级取证**：`~/.niu/logs/raw_http/<YYYYMMDD>/*.json` 中 tool=computer 的请求/回执；必要时用 `messages.db` 还原会话（只读）
5. **装机**：改 native 必须先停 Niu → `maturin build` → `pip install --force-reinstall`；打包走 `launcher/build.sh`（wheel 构建必须先于 rsync `python/`）

## 4. 允许保留 vs 必须一致（判据）

**允许保留**：语言/绑定适配（JS→Python、camelCase→snake_case、async→sync）、无审批体系、无独立 worker、无 `read`/`write`/`tool.*` helper、截图不 inline 图像（走 `analyze_image`）、§2 已登记的 D1–D8、原生层的 A/B/C/D 四类。

**必须一致（或显式登记理由）**：14 个错误码名集合、恢复文案语义、`read_only` 覆盖面与文案、坐标/帧规则（指针=同 target 最近截图；AX=全局桌面坐标；两套空间禁混）、对象成员名与语义、Rules 各行、安全段文本。

## 5. 需人工决策的点（跟版时若触及，先问用户）

1. 是否引入审批分级（`read_only` → read approval）；
2. 是否给 `computer` 造 `read`/`write`/`tool.*` 等价物；
3. `assert` 护栏形态（现为运行时反守卫，见 D8）；
4. 是否引入 worker/进程隔离（解决挂死 run 无法回收）；
5. Linux 平台是否纳入；
6. 截图是否给模型 inline 图像（需重建图片直投通道）。

## 6. 风险与陷阱（历次实证）

1. **上游重命名/新增成员**（如 `maxDepth`、delivery 模式、`StaleRef` 代数规则）→ 由 S4 逐成员 + S5 逐行兜住；代数规则变更必须同步描述 Rules 行 + 手册 + `errors.py` 的 `StaleRef` 恢复句。
2. **上游新增 delivery 模式** → 描述 Rules 行硬编了 `background`/`foreground` 两个名字（`capabilities.delivery_modes` 是动态的）→ 加模式必须改描述与手册。
3. **上游 `code` 扩容**（显示选择器/结构化调用）→ 本仓 `display` 固定 `'all'`（`DesktopSession()` 无 options）→ 需人工决策。
4. **台账行数会过期**（实证：`niu-natives/UPSTREAM.md` 曾写 mod.rs 1514/912，实测 1577/969）→ 跟版第一步重跑 S3。
5. **装机陷阱**：改 native 必须先停 Niu（旧 `.so` 被占用）；打包必须先 wheel 后 rsync `python/`；Windows `cmd` 不展开 glob（用 `for`）。
6. **坐标陷阱**：region 截图使整屏帧失效（fail-loud）——任何文档改写都不得写成「区域图坐标可以直接点」。
7. **描述与实现一致性只能靠真跑暴露**（历史实证：`windows()` 返回 PyO3 对象、`win.bounds()` 当方法调、`assert(...)` 静默通过、`read`/`write` 名字不存在）→ 每次改描述后都要跑一次"照描述真跑"探针。
