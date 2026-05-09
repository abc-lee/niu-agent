# 知识图谱脑区架构 — 问题总览与进度跟踪

> 本文档是脑区架构的**总控文件**，记录所有已知问题、调研状态和解决方案。
> 具体调研细节指向子文档，避免本文档过长。
> **原则：所有问题解决前，不进入施工阶段。**

---

## 一、项目背景

脑区架构是 Niu AI 助手知识图谱的核心设计，让图谱检索区别于向量检索：
- 向量检索：语义相似度匹配，无结构
- 图检索：沿脑区路径检索，有结构、有方向

脑区生命周期：创建 → 激活/衰减 → 萎缩 → 合并/解散

---

## 二、三大闭环

| 闭环 | 目的 | 状态 |
|------|------|------|
| **写入侧** | LightRAG 提取时考虑脑区归属 | ✅ 已实施（llm_proxy.py 注入 + brain_region_prompt.py） |
| **读取侧** | 主 Agent 检索时沿脑区走 | ✅ 注入路径工作，冷启动优化（轮询替代180s固定延迟），实例缓存 |
| **维护侧** | 脑区创建/衰减/解散/清理 | ✅ 代码存在且连接真实 LightRAG 操作 |

---

## 三、待验证问题清单

> 每个问题必须验证到"能跑通"才算解决。

### P1: lightrag_adapter.py 关键方法是否真实存在且可用

**问题**：`region_manager.py` 调用了 `list_entities`、`explore_node`、`delete_entity` 等方法，但这些方法在 `lightrag_adapter.py` 中可能不存在或为空壳。如果这些方法不工作，脑区的创建/解散/清理都无法真正执行。

**影响**：整个维护侧闭环断裂

**调研状态**：✅ 已验证

**验证结论**：`lightrag_adapter.py` 关键方法全部存在且可用

| 方法 | 行号 | 所在类 | 调用的 LightRAG 操作 |
|------|------|--------|---------------------|
| `query_data` | L176 | LightRAGAdapter | `rag.aquery_data()` — 真实查询 |
| `explore_node` | L453 | LightRAGAdapter | `rag.get_knowledge_graph()` — 真实 BFS |
| `delete_entity` | L772 | LightRAGAdapter | `rag.adelete_by_entity()` — 真实删除 |
| `list_entities` | L819 | LightRAGAdapter | 直接 NetworkX 图遍历或 `rag.get_knowledge_graph("*")` — 真实读取 |
| `inject_custom_kg` | L1102 | LightRAGIngester | `rag.ainsert_custom_kg()` — 真实写入 |

`_get_rag()` 通过 `get_lightrag()` 获取全局单例，可能返回 None（LightRAG 未初始化时），所有调用者都有 None 检查。

**遗留问题**：`inject_custom_kg` 有误导性的 DeprecationWarning（见 P8）。

---

### P2: inject_custom_kg 是否真正写入图谱

**问题**：`inject_custom_kg` 依赖 `_get_rag()` 返回有效的 LightRAG 实例。如果实例为 None 或未初始化，所有写入操作静默失败。

**影响**：脑区节点、关系边可能从未真正写入图谱

**调研状态**：✅ 已验证

**验证结论**：`inject_custom_kg` 调用真实的 `rag.ainsert_custom_kg()`，`_get_rag()` 通过 `get_lightrag()` 返回全局单例，所有调用者均有 None 检查。P1 验证已确认此链路完整。

---

### P3: brain_tools.py 已迁移到 ToolRegistry

**问题**：项目经历了 MCP stdio → ToolRegistry 的架构迁移。`brain_tools.py` 可能仍使用旧的 stdio 模式，导致工具调用失败。

**影响**：主 Agent 无法通过 MCP 工具操作脑区

**调研状态**：✅ 已验证

**验证结论**：`brain_tools.py` 已正确迁移到 ToolRegistry 架构

- 定义了 `BRAIN_TOOL_SCHEMAS`（3个schema），通过 `mcp_loader.py` 的 `_register_brain_tools()` 注册到 ToolRegistry
- 不使用旧的 MCP stdio 模式
- 不是 MCP 服务器模块，是内置工具模块（直接 import 注册）

**遗留问题**：无

---

### P4: region_activation.py 激活状态仅存内存

**问题**：激活状态存储在内存中，应用重启后丢失。所有脑区回到初始状态。

**影响**：衰减曲线无法跨重启维持

**调研状态**：✅ 已验证

**验证结论**：激活状态存储在纯内存 dict 中，无持久化 — 这是设计如此（会话级状态），非缺陷

- 文档明确声明 "PURE IN-MEMORY state — no LightRAG calls, no persistence"
- 重启后所有激活状态丢失，回到 0.0
- 衰减系数 0.92/轮（不是/分钟），14轮后低于0.3阈值
- 脑区结构数据（节点、边、shrink_count）持久化在图谱中

**待确认**：是否需要跨重启持久化激活状态（当前为会话级设计）。

---

### P5: region_injector.py 注入路径完整但有优化问题

**问题**：`BrainContextInjector` 的注入路径完整，但有两个优化问题。

**影响**：读取侧闭环功能正确，但有冷启动窗口和轻微性能浪费

**调研状态**：✅ 已验证

**问题1：冷启动180秒延迟**
- `RegionSync` 启动后固定等180秒才首次运行（等LightRAG初始化）
- 但LightRAG初始化通常只需几秒，180秒过度保守
- 前3分钟脑区注入静默跳过，无警告
- **解决方案**：监听LightRAG就绪事件，就绪后立即初始化，替代固定等待

**问题2：每轮新建4个轻量实例**
- `LightRAGAdapter()` — 轻量包装器，_rag=None，首次调用时获取实例
- `LightRAGIngester(adapter)` — 轻量，只持有adapter引用
- `RegionManager(adapter)` — 轻量，只持有adapter引用
- `BrainContextInjector(region_mgr)` — 轻量，只持有引用
- 都没有昂贵的初始化操作，重建不是性能瓶颈
- **优化方案**：缓存为runner实例变量，跨轮复用

**结论**：两个问题都不影响功能正确性，属于优化项。

---

### P6: LightRAG 提取时提示词注入方案设计

**问题**：LightRAG 的 LLM 提取请求经过 `llm_proxy.py` 代理。需要在代理中注入脑区架构说明，让 LLM 建边时考虑脑区归属。

**关键约束**：
1. 注入内容需动态读取当前脑区列表（用 local 模式 + only_need_context=True，0次LLM）
2. 不能触发死循环（查询脑区不能引发 LLM 调用）
3. 默认脑区只有3个：聊天历史、文档库、知识体系
4. 其他脑区由 LLM 自动创建，数量不固定
5. 注入内容：脑区是什么 + 当前有哪些脑区 + 如何建新脑区

**调研状态**：✅ 已实施

**实施内容**：
- 创建 `niu_api/internal/brain_region_prompt.py` — 检测、静态/动态提示词构建、注入
- 集成到 `niu_api/llm_proxy.py` — LightRAG 提取请求自动注入脑区上下文
- 28个测试全部通过（15单元 + 2集成 + 8 E2E + 3冒烟）
- 关键设计：`mode="local"` + `only_need_context=True` 防止死循环

**设计文档**：→ `docs/superpowers/plans/2026-05-07-brain-region-implementation.md`

---

### P7: ainsert_custom_kg 的 chunks 不走 LLM 提取

**问题**：`ainsert_custom_kg` 接受 chunks 参数但只存入向量库，不调用 LLM 提取。导致 chunks 成为孤立碎片，无法与已有实体建边。

**影响**：Skills 注入后没有边，无法被图谱关联

**调研状态**：✅ 不需要修改 — 已有替代方案

**替代方案**（→ `docs/superpowers/plans/2026-05-06-photo-kg-auto-ingest.md`）：
1. `inject_custom_kg` 负责精确写入结构（节点+边）— 这是正确的做法
2. chunks 存入向量库用于语义检索
3. **brain_tools 3个命令**（activate/dim/status）让大模型"认识"这些节点
4. **脑区提示词注入**（今天实施）让 LightRAG 提取时考虑脑区归属
5. 三者结合，大模型自然能与已有节点建边，无需修改 LightRAG 源码

**结论**：原 P7 计划（修改 LightRAG 源码给 chunks 加 LLM 提取）是不必要的。现有方案更优。

---

### P8: lightrag_adapter.py 中 DEPRECATED 标记需回退

**问题**：之前错误地在 `inject_custom_kg` 上添加了 DEPRECATED 标记和 warnings.warn。C1 结论是 inject_custom_kg 和 lightrag_insert 是互补关系，不是替代关系。

**影响**：误导开发者，可能导致错误地弃用必要方法

**调研状态**：✅ 已清理

**清理内容**：
- 删除 `inject_custom_kg` 上的 `warnings.warn(DeprecationWarning)` 调用
- 更新 docstring 为互补说明（inject_custom_kg = 精确控制，lightrag_insert = 自动提取）
- grep 确认无残留 DEPRECATED/warnings.warn

---

## 四、问题依赖关系

```
P1 (adapter方法存在性) ──→ P2 (inject_custom_kg写入) ──→ P6 (提示词注入)
                                                              │
P3 (brain_tools迁移)   ──→ P5 (注入影响Agent)              │
                                                              │
P4 (激活状态持久化)     ──→ P5 (注入影响Agent)              │
                                                              │
P7 (chunks LLM提取)    ──→ P6 (提示词注入，chunks建边)     ←─┘

P8 (DEPRECATED回退)    ──→ 独立，可随时处理
```

**关键路径**：P1 → P2 → P6（如果 adapter 方法不存在，写入侧和提示词注入都无法实现）

---

## 五、当前阶段

**阶段：施工完成** — P6/P8 已实施，P5 已优化

P1-P5 验证完成，P6/P8 代码已实施，P7 待后续实施。

---

## 六、决策记录

| 日期 | 决策 | 原因 |
|------|------|------|
| 2026-05-07 | inject_custom_kg 和 lightrag_insert 是互补关系 | C1 代码审查：7/8 调用者必须用 inject_custom_kg |
| 2026-05-07 | 脑区提示词注入走 LLM 代理，不改 LightRAG 源码 | 代理是自有代码，维护成本低 |
| 2026-05-07 | 注入内容不含"活跃脑区"概念 | 图谱数据异步注入，无活跃状态 |
| 2026-05-07 | 注入时动态读取脑区列表（local模式，0次LLM） | 避免硬编码，反映图谱真实状态 |
| 2026-05-07 | 默认脑区只有3个：聊天历史、文档库、知识体系 | 不预设专业脑区，由LLM自动创建 |
| 2026-05-07 | 动态查询使用 `adapter.query()` 而非 `adapter.query_data()` | query() 支持 mode 和 only_need_context 参数，query_data() 不支持 |
| 2026-05-07 | 冷启动180s固定延迟改为5s轮询重试 | LightRAG通常几秒就绪，180s过度保守 |
| 2026-05-07 | 脑区注入实例缓存为runner属性 | 4个轻量包装器每轮重建无必要 |
