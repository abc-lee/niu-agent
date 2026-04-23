# LightRAG 融合工程 — 汇总

> 最后更新：2026-04-23
> 用途：快速了解工程全貌、各子工程进度、关键决策

## 工程目标

用 LightRAG 替代现有 vector-store（SQLite + MiniLM-L12）+ kg-server（KuzuDB），统一检索基础设施。

## 关键决策记录

| # | 决策 | 结论 | 理由 |
|---|------|------|------|
| D1 | LLM调用 | 恢复 page_agent_proxy.py 为通用 LLM 代理 | LightRAG 支持 OpenAI 格式，代理消解 LLM 适配问题 |
| D2 | Embedding模型 | 从 MiniLM-L12(384维) 切换到 BAAI/bge-m3(1024维) | LightRAG 推荐，多语言，同规模最优 |
| D3 | Reranker | BAAI/bge-reranker-v2-m3，初期可不用 | 提升检索精度，作为优化项 |
| D4 | 数据注入双路径 | 结构化数据→ainsert_custom_kg()，非结构化→ainsert() | Skills/MCP/照片名称必须精确，LLM提取会破坏 |
| D5 | 交互习惯 | 纠错文档 + ainsert()，替代 SQLite | 一个md文件，人可读可编辑，LLM自动提取意图→工具关系 |
| D6 | V8递归检索 | 取消，由图谱遍历替代 | LightRAG hybrid模式天然支持多跳检索 |
| D7 | 记忆系统 | 脑图方案，用 ainsert_custom_kg() 在图谱中建 brain: 命名空间 | 图谱比向量库更像人脑记忆网络 |
| D8 | MCP工具递归 | 注入时建立 USED_FOR / OFTEN_WITH 关系，检索时图谱遍历 | 不需要预定义 query_pattern |
| D9 | adelete 安全性 | 信任 LightRAG 自身逻辑，不做引用计数层 | 港大团队开发，有理论基础 |
| D10 | 实体去重 | 不做模糊匹配，靠手动 same_as 关系 | "小李"vs"李某某"无法自动匹配 |
| D11 | 脑图主实体 | brain:Niu，所有记忆从它出发 | 类似人脑"自我"节点 |
| D12 | 脑图检索 | 直接用 LightRAG aquery(mode="mix") | 不自建检索逻辑，LightRAG天然做"点亮" |
| D13 | 脑图与文档知识 | 共享 LightRAG 实例 | 脑图节点和文档实体需要连接 |
| D14 | 脑图核心价值 | 上下文注入，让 Agent "显得聪明" | 存储不是目的，注入才是 |
| D15 | 无数据迁移 | 02和03都没有历史数据迁移 | 直接替换代码 |
| D16 | Embedding/Reranker可插拔 | 配置文件层切换，不改代码 | 用户电脑性能好时可换更好模型 |
| D17 | 插拔方式写入系统管理手册 | 主Agent看手册后可帮用户切换 | Agent能指导用户操作 |

## 子工程清单

| # | 子工程 | 方案文档 | 状态 | 涉及场景 |
|---|--------|---------|------|---------|
| 01 | 数据注入与检索策略 | [01-data-in3jection-retrieval.md](01-data-injection-retrieval.md) | ✅ 实施完成，29测试通过 | V1,V3,V4,V5,V6,V7 |
| 02 | 文档入库与实体提取流水线 | [02-document-entity-pipeline.md](02-document-entity-pipeline.md) | ✅ 实施完成，代码审查通过 | K1,K2,K3,K4,K5 |
| 03 | 记忆脑图设计 | [03-memory-brain-graph.md](03-memory-brain-graph.md) | ✅ 实施完成，39测试通过，4轮代码审查通过 | C1 |
| 04 | MCP工具接口重设计 | [04-mcp-tool-interface.md](04-mcp-tool-interface.md) | ✅ 方案完成，已讨论确认 | K6,K8 |
| 05 | LLM代理+Embedding+Reranker | [05-llm-proxy-embedding.md](05-llm-proxy-embedding.md) | ✅ 实施完成，91测试通过 | 基础设施 |

## 场景迁移状态

| 场景 | 描述 | 迁移方式 | 目标子工程 |
|------|------|---------|-----------|
| V1 | 每轮动态资源注入 | aquery_data(mode="mix") | 01 |
| V2 | 工具生命周期评分 | aquery_data(mode="local") | 01 |
| V3 | Skill同步 | ainsert_custom_kg() | 01 |
| V4 | 交互习惯追踪 | 纠错文档 + ainsert() | 01 |
| V5 | MCP工具描述索引 | ainsert_custom_kg() + USED_FOR/OFTEN_WITH | 01 |
| V6 | 系统手册L1注入 | ainsert() (LLM提取) | 01 |
| V7 | 照片/文档存储 | ainsert_custom_kg() (人物/地点) + ainsert() (场景) | 01 |
| ~~V8~~ | ~~递归查询模式搜索~~ | ~~已取消~~ | 由图谱遍历替代 |
| V9 | 向量库清理 | LightRAG 自带数据管理 | 02 |
| K1 | 文档入库到KG | ainsert() 同步提取 | 02 |
| K2 | 实体提取(pending文档) | LightRAG 内建，KGScanner废弃 | 02 |
| K3 | KG丰富化 | ainsert_custom_kg() | 02 |
| K4 | Dream Evolver知识写入 | ainsert_custom_kg() | 02 |
| K5 | KG批量回填同步 | vectors→KG同步废弃，photos→LightRAG保留 | 02 |
| K6 | 图可视化API | LightRAG get_knowledge_graph() + 客户端分析 | 04 |
| K7 | 知识探索引导 | hybrid模式已含图扩展，引导可简化 | 04 |
| K8 | LLM直接调用KG工具 | 27工具→12工具重设计 | 04 |
| C1 | 记忆存取 | 脑图方案(brain:命名空间) | 03 |

## 保留独立的场景

| 场景 | 理由 |
|------|------|
| 无 | 所有场景均已规划迁移方案 |

## 下一步

- [x] 讨论子工程 01 方案 ✅
- [x] 讨论子工程 02 方案 ✅
- [x] 讨论子工程 03 方案 ✅
- [x] 讨论子工程 04 方案 ✅
- [x] 讨论子工程 05 方案 ✅
- [x] 实施子工程 05（LLM代理+Embedding+Reranker）✅ 91测试通过
- [x] 实施子工程 01（数据注入与检索策略）✅ 29测试通过
- [x] 实施子工程 02（文档入库与实体提取流水线）✅ 28测试通过
- [x] 实施子工程 03（记忆脑图设计）✅ 39测试通过，4轮代码审查
- [ ] 实施子工程 04（MCP工具接口重设计）
