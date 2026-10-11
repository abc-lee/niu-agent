# 交接单：llama-server 单条输入上限 与 API 日志落盘（主分支待办）

> 2026-10-11 ｜ 来源：Windows 端 v0.5.0 真机升级验收发现 ｜ 交接对象：主分支负责人
>
> **一句话**：根因已修（`-b/-ub` 提到 8192，见 §1.4），但「我们不会送超过 8192 的内容」**没有结构性保证**；
> 且 Windows GUI 下 **API 日志被整体丢弃**，任何静默失败都不可诊断。建议按 §2 → §3 → §4 推进。

---

## 1. 背景与已完成（windows 分支，已推送）

### 1.1 现象
10 个 skill 里 `miaodong`（2118 token）与 `scheduled-tasks`（2087 token）**长期无法入库**：

- `~/.niu/skill_sync_state.json` 永远只有 8 条，缺这两个；
- 知识图谱里既无 `miaodong`/`scheduled-tasks` 节点，也无「知识体系脑区 → skill」边
  （`scheduled-tasks` 在 graphml 里出现 **0 次**；`miaodong` 的 8 次全是别的实体描述里提到它的文字）；
- 按 `SkillSync.scan_and_sync` 的语义（**只在注入成功时才写 hash，失败不写**）⇒ **每 60 秒重试一次、永不成功**；
- 全程**无任何可见报错**（原因见 §2）。

### 1.2 根因（实测定论）
llama-server **单条输入的 token 上限 = min(逻辑批 `-b`, 物理批 `-ub`)**。

原参数只写了 `-ub 2048`、`-b` 取默认值 2048 ⇒ 实际上限 **2048**；而本模块对 LightRAG 声明的
`max_seq_length = 8192` ⇒ 两边语义不一致。超过上限的文本编码直接 HTTP 500：

```json
{"error":{"code":500,"message":"input (2118 tokens) is too large to process.
 increase the physical batch size (current batch size: 2048)","type":"server_error"}}
```

⇒ `niu_api/internal/llama_server.py::encode_batch` 抛异常 ⇒ `LightRAGIngester.inject_custom_kg`
捕获后返回 `{"status":"error"}` ⇒ **整条 `ainsert_custom_kg` 回滚**（部分成功也不落状态）⇒ 下一轮重试、再失败。

10 个 skill 的 token 数分界完全吻合（用生产实例 `/tokenize` 量的）：

| skill | token | 入库 |
|---|---|---|
| computer-tool-automation | 680 | ✓ |
| note-management | 743 | ✓ |
| brain-region-management | 769 | ✓ |
| browser-automation | 1068 | ✓ |
| report-skill | 1333 | ✓ |
| office-docs | 1757 | ✓ |
| photo-face-display | 1873 | ✓ |
| daily-routine-data | 1951 | ✓ |
| **miaodong** | **2118** | ✗ |
| **scheduled-tasks** | **2085** | ✗ |

### 1.3 参数对照实测（拿两个失败 skill 的真实文本直接打编码接口）

| llama-server 参数 | 结果 |
|---|---|
| `-ub 2048`（原状） | ✗ HTTP 500 |
| `-ub 8192`（只改物理批） | ✗ HTTP 500 |
| `-b 8192`（只改逻辑批） | ✗ HTTP 500 |
| **`-b 8192 -ub 8192`** | ✅ **2/2 条、768 维，成功** |

⇒ 两者取 min，**必须同时给够**。

### 1.4 已修（windows 分支 `dcb507b7`）
`niu_api/internal/llama_server.py`：`_CTX_SIZE=8192` 之下新增 `_BATCH=8192`、`_UBATCH` 由 2048 提到 8192，
argv 补 `-b`。真机验证（用户重启后实测）：

- llama-server 命令行 = `-c 8192 -b 8192 -ub 8192`；
- 生产实例对 2118 token 文本编码成功（此前 500）；
- `skill_sync_state.json` 8 → **10** 条；
- 图谱 528/887 → **530/889**（两节点 + 两边）；`vdb_chunks` 69 → 80；
  `vdb_entities`/`vdb_relationships` 与图谱**逐条一致**（530/889）；
- 代价：llama-server 常驻内存 2515MB → **2887MB（+372MB）**。

---

## 2. 待办 ①（最高优先）：API 日志落盘

**现状链路**：`niu_api/__main__.py` 的 loguru 唯一 sink = `sys.stderr`
→ 启动器 `drain_lines` 捕获 → `info!/warn!/error!`（tracing）
→ `tracing_subscriber::fmt().init()` **默认写 stdout**
→ Windows GUI 子系统（`#![windows_subsystem = "windows"]`）**没有 stdout** ⇒ **全部丢弃**。

**实证**：`~/.niu/logs/` 下只有 `llama_server.log`、`llm_interaction_*.log`、`raw_http/`，
**没有任何 API 日志**。本次根因是靠「读代码 + 逐条比对存储数据」挖出来的（十几步），
期间每天有几十条 `[SkillSync] Failed to add skill ...` 被静默丢掉。

**建议**
- `tracing_subscriber` 增加**文件 writer** → `~/.niu/logs/launcher.log`（必须落在 `~/.niu/logs`：
  不能写 bundle，macOS 运行时只读）
- 带**大小上限/轮转**（如 5MB × 3），否则长跑无限增长
- 有控制台时（macOS 终端启动 / 开发机）保留 stdout 输出
- 明确 `logging.enabled`（读 `~/.niu/config/user-config.json`）是否也门控文件 writer —— 建议照旧门控，不改变语义
- `drain_lines` 线程**绝不能阻塞**（管道填满会让子进程写阻塞 → API 挂死；该函数注释里已有此说明）

**验收**：正常启动后 `~/.niu/logs/launcher.log` 存在且含 `niu_api ...` 行；能搜到 `[SkillSync] ...`；
文件大小受轮转约束。

---

## 3. 待办 ②（兜底）：编码失败的反应式护栏

**现状**：`encode_batch` 遇 500 直接抛 ⇒ 整条注入回滚（本 bug 的放大机制）。

**且客户端零校验**（已核代码）：`python/Lib/site-packages/lightrag/utils.py` 的
`EmbeddingFunc.__call__`（约 L497–510）只做两件事——注入 `embedding_dim`、
把 `max_token_size` 透传给**签名里认这个参数的下层函数**；我们的 `_LlamaCppAdapter.encode`
不认该参数 ⇒ **`max_token_size` 根本没传下去，既不截断也不校验长度**。
唯一的墙就是 llama-server 那道上限。

**建议**：`niu_api/internal/llama_server.py::encode_batch` 捕获 `HTTPError`（500 且 body 含
`too large to process`）→ 拆批 → 逐条重试 → 对超限那条**按长度折半截断**直到通过 →
`logger.warning`（带上服务端报文里的真实 token 数）。注意保持既有 `_encode_lock` 串行化语义。

**验收**：构造一条 >8192 token 的文本 → 不抛、被截断、有明确 warn；该条不再让整条注入回滚。

---

## 4. 待办 ③（策略）：上限要不要抬 / 长 skill 要不要分块

**事实**
- **文档侧永远够不着上限**：LightRAG 按 `chunk_token_size=1200`
  （`niu_api/internal/lightrag_manager.py:1283`）切块 —— 实测非 skill 块**最大 1052 字符、中位 207**
  ⇒ 一直在安全区。**这就是当初只有两个 skill 挂、文档全好的原因**。
- **未切块的只有 skill 与 custom-KG**：skill 是**整文件一个块**（实测 3138–4722 字符/块）。
- 模型 GGUF 元数据 `context_length=262144`；当前 `-c/-b/-ub = 8192` 是保守取值
  （见 `llama_server.py` 顶部注释：未做长文本质量验证前取 8192）。
- 内存实测：`-ub 2048` → 2515MB；`-b/-ub 8192` → 2887MB（+372MB）。

**选项（择一或组合，需 owner 定）**
1. **维持 8192 + 靠 §3 护栏兜底**（代价最小，推荐先做这个）
2. 抬到 `16384/32768`（治「内容变长」，内存再涨；未做长文质量验证前有风险）
3. **长 skill 分块注入**（结构性改动，会改变 skill 的语义召回形态 ⇒ 需另立方案 + 双审门禁）

**已知残留风险**：写一个 >8192 token（约 1.2 万汉字，现最长 4699 字符/2085 token）的 skill，
即触发旧病：整条回滚 + 每分钟重试 + 无声（§2 修好前连日志都没有）。

---

## 5. 附：本机复现 / 自查命令（可照抄到另一台机器）

```bash
# 0) 当前 llama-server 生效参数（含 -b/-ub 是否给够）
#    Windows: powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='llama-server.exe'\" | Select-Object -ExpandProperty CommandLine"
ps aux | grep '[l]lama-server' | head -1

# 1) 某段文本的真实 token 数（用生产实例自己量，最准；不要用字符数估算）
curl -s http://127.0.0.1:19878/tokenize -H 'Content-Type: application/json' \
     --data-binary '{"content":"<text>"}' | python3 -c 'import sys,json;print(len(json.load(sys.stdin)["tokens"]))'

# 2) 直接复现「超限必 500」
curl -s http://127.0.0.1:19878/v1/embeddings -H 'Content-Type: application/json' \
     --data-binary '{"input":["<超限文本>"],"encoding_format":"float"}'

# 3) skill 同步状态条目数（期望 = 磁盘 skill 数，Windows 路径为 %USERPROFILE%\.niu\...）
python3 -c "import json,os;d=json.load(open(os.path.expanduser('~/.niu/skill_sync_state.json'),encoding='utf-8'));print(len([k for k in d if k!='_notes']))"

# 4) 图谱 vs 向量库是否逐条一致（节点/边数应分别等于 vdb_entities/vdb_relationships）
python3 - <<'PY'
import json,os,re
b=os.path.expanduser('~/.niu/lightrag_storage')
g=open(os.path.join(b,'graph_chunk_entity_relation.graphml'),encoding='utf-8').read()
print('graphml node/edge =', g.count('<node '), g.count('<edge '))
for f in ('vdb_entities.json','vdb_relationships.json','vdb_chunks.json'):
    print(f, len(json.load(open(os.path.join(b,f),encoding='utf-8'))['data']))
PY
```

---

## 6. 同批已完成的相关修复（windows 分支，供合并参考）

| commit | 内容 |
|---|---|
| `dcb507b7` | 本单 §1.4：llama-server `-b/-ub` 提到 8192 |
| `a3521f77` | Windows Chat 窗自绘圆角（`html.plat-win` 门控，Mac 零改动） |
| `c59fd35a` | Windows 设置窗自绘圆角（与 Chat 同源；额外阻断 CSS 背景传播，纸底改 `position:fixed` 的 `body::before`） |
| `70f7fed9`（main） | 关机 HTTP 通知超时 2s→8s |
| `702acc32`（main） | DeepSeek-V3 tokenizer 入 git（`models/tokenizers/deepseek-v3/tokenizer.json`） |

> 圆角两笔的共同根因：迷你模式 T1（`09c82e18`，2026-09-15）把 Chat 窗改为
> `frame:false + transparent:true` 后，**Windows 的 DWM 不再为透明窗做圆角**
> （macOS 由 Electron `roundedCorners` 默认提供、且仅 macOS 有此选项），
> 而页面侧只把不透明底从窗口属性搬到 CSS，未补自绘圆角。设置窗同款选项 ⇒ 同病。
