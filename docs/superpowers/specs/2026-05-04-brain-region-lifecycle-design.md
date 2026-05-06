# 脑区生命周期治理设计 v2

> 升级自 `docs/lightrag-plans/06-brain-region-activation.md`（M7 模块设计）

## 与旧方案（M7）的比对

### M7 已设计的能力

| M7 模块 | 设计内容 | 当前实现状态 | v2 处理 |
|---------|---------|-------------|---------|
| RegionDetector | Leiden 社区检测 | ✅ 已实现 | 升级：加 min_graph_size/min_community_size/resolution 自适应 |
| RegionManager | 创建/清理脑区节点 | ✅ 已实现 | 升级：保护默认脑区 + 新增萎缩解散 |
| RegionActivationManager | 激活/衰减/溢出/工具强化 | ✅ 已实现 | 升级：新增共激活计数器（合并依据） |
| RegionSync | 定时同步服务 | ✅ 已实现 | 升级：同步时增加合并检查和萎缩解散 |
| 脑区摘要 LLM 生成 | update_region_summaries | ✅ 已实现 | 不变 |
| 邻居图溢出激活 | spillover_factor | ⚠️ 框架在但 neighbor_map 为空 | 不变（需要未来填充） |
| 脑区数量控制 | 未设计 | ❌ 缺失 | **v2 新增**：检测过滤 + 合并 + 解散 |
| 基于行为的合并 | 未设计 | ❌ 缺失 | **v2 新增**：共激活合并 |
| 萎缩解散 | 未设计 | ❌ 缺失 | **v2 新增**：持续萎缩→解散→归入邻居 |
| 默认脑区保护 | 未设计 | ❌ 缺失 | **v2 新增**：cleanup 不删默认脑区 |

### M7 的关键缺陷（v2 修复）

1. **`min_graph_size=50` 定义了但从未使用** — `detect_communities()` 不检查图谱大小
2. **无社区大小过滤** — 单实体社区也被创建为正式脑区
3. **`resolution=1.0` 硬编码** — 小图谱产生过多碎片社区
4. **`cleanup_stale_regions` 不区分默认脑区** — Leiden 检测后默认脑区被删除
5. **无合并机制** — 行为趋同的脑区无法自动合并
6. **无解散机制** — 成员减少的脑区无法自动清理
7. **neighbor_map 为空** — 溢出激活功能不可用（v2 不处理，需未来填充）

## 设计

### 1. 检测阶段过滤（`region_detector.py`）

**让 `min_graph_size` 生效**：
- `detect_communities()` 开头检查：`len(nodes) < min_graph_size` → 返回空结果
- `min_graph_size` 从 `REGION_CONFIG_DEFAULTS` 传入，默认 50

**新增 `min_community_size` 参数**：
- 成员 < `min_community_size`（默认 10）的社区不返回
- 被过滤掉的小社区成员归入"未分配"集合（暂不处理，后续可归入最近邻居）

**`resolution` 自适应**：
- 节点 < 200 时用 `resolution=0.5`（倾向更大更少的社区）
- 节点 >= 200 时用 `resolution=1.0`（标准 modularity）

### 2. 基于激活行为的合并（`region_activation.py` + `region_manager.py`）

**核心思路**：如果两个脑区在多次查询中总是同时被激活（共激活频率高），说明它们语义相关，应该合并。

**`RegionActivationManager` 新增共激活计数器**：

```python
# 新增属性
_co_activation_counts: dict[tuple[str, str], int]  # (region_A, region_B) -> 同时激活次数
_total_activation_rounds: int  # 总激活轮次
```

**每次 `activate_regions()` 调用时**：
- 如果激活了多个脑区，对每对被激活的脑区，`_co_activation_counts[(A, B)] += 1`
- `_total_activation_rounds += 1`

**合并检查（在 `RegionSync.run_sync()` 中）**：
- 计算共激活比率 = `co_activation_count / total_activation_rounds`
- 如果两个脑区共激活比率 > **90%** 且合并后成员数 < 上限 → 合并
- 合并操作：调用 `lightrag_adapter.merge_entities([source], target)` 合并 KG 节点
- 更新激活管理器：移除被合并的脑区，将成员归入目标脑区

**合并时机**：每次 `RegionSync.run_sync()` 同步时检查，而非每次查询（避免高频操作）。

### 3. 萎缩解散机制（`region_manager.py`）

**新增 `dissolve_shrunk_regions()` 方法**：

- 每次同步时，检查每个脑区的当前成员数
- 成员 < 3 → 标记为"萎缩"
- 萎缩计数存储在脑区节点的 `description` 中（追加 `brain_meta_shrink_count:N`）
- 连续 3 个同步周期（约 3 天）都萎缩 → 解散
- 解散时：成员实体归入**最相似的邻居脑区**（用实体类型分布余弦相似度）
- 删除脑区节点（`delete_entity`）
- 更新激活管理器

### 4. 保护默认脑区（`region_manager.py`）

**修改 `cleanup_stale_regions()`**：
- 不删除无 `community_id` 的脑区（即默认脑区：聊天历史、文档库、知识体系）
- 只清理有 `community_id` 但不在当前检测结果中的脑区

## 修改文件清单

| 文件 | 修改 |
|------|------|
| `niu_api/internal/region_detector.py` | `min_graph_size` + `min_community_size` + resolution 自适应 |
| `niu_api/internal/region_activation.py` | 新增共激活计数器 + `get_merge_candidates()` |
| `niu_api/internal/region_manager.py` | 新增 `dissolve_shrunk_regions()` + 修改 `cleanup_stale_regions` 保护默认脑区 |
| `agent/injector/region_sync.py` | `run_sync()` 中增加合并检查和萎缩解散调用 |

## 验证

1. 小图谱（<50 节点）：不产生检测脑区，只有默认脑区
2. 中等图谱（50-200 节点）：resolution=0.5，产生较少较大脑区
3. 共激活合并：两个脑区 90%+ 同时激活 → 自动合并
4. 萎缩解散：成员 <3 持续 3 天 → 解散归入邻居
5. 默认脑区保护：Leiden 检测后默认脑区不被删除
