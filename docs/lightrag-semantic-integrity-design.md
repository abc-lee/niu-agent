# LightRAG 语义完整性检查与修复设计

## 背景

主 Agent 在 2026-07-12 修复 `lightrag_repair.py` DocStatus 大小写 bug 后，触发了 region_sync
跑 dissolve，导致 16 个"智家xxx脑区"僵尸脑区被连锁删除，程序启动后风扇狂转、阻塞。

根因不是 DocStatus 修复错了，而是检查工具的根本性设计缺陷：
- 参照系是"句法引用完整性"（A 引用 B，B 里有就 ok）
- 不是"语义正确性"（description 写"被删除"的脑区应该不在 GraphML 里）

16 个僵尸脑区在句法上完全自洽（key 都能解析），所有 11 项 check 全部通过，
但语义上是僵尸——description 明确写"被删除的重复脑区实体之一"。

## 僵尸脑区形成机制

1. 历史 Agent 用 custom_kg 注入"删除日志"edge（kw='删除操作'），但没调 delete_entity
2. `shrink_threshold=100` 误判正常小脑区（成员数 < 100）为萎缩，dissolve 流程跑到 shrink_count=1
3. dissolve 被中断（进程重启、sync 没跑完等），僵尸脑区卡在"shrink_count=1 中间态"——description 含标记，但 GraphML node 仍存在
4. LightRAG adelete_by_entity 只删 3 个存储，留下 5 个存储的残留

## 新设计原则

1. **语义维度检测**：description 语义标记、跨存储交叉验证、反向索引异常
2. **跨存储交叉验证**：不只 A 引用 B，还验证字段一致
3. **完整 8 存储清理**：GraphML + vdb_entities + vdb_relationships + entity_chunks +
   text_chunks + vdb_chunks + full_entities/full_relations + relation_chunks（Bug #3 修复）
4. **验证标准升级**：不只 check_all 返回 ok，还要程序启动正常运行

## P0 遗漏修复（与本次计划一并完成）

审查发现 4 个 P0 问题不是删除工具 bug，而是 region_sync/runner/region_manager 的设计缺陷，
与本次检查+修复工具一并修复：

1. `_refresh_activation_manager` 覆盖率 < 50% 直接 return（Task 13：删除覆盖率检查）
2. `_get_brain_injector` forced sync 死循环（Task 14：加 5 分钟失败冷却）
3. `shrink_threshold=100` 太高（Task 15：降到 10，僵尸脑区形成根因）
4. forced sync 同步阻塞 43 秒（Task 16：改异步触发）

## 5 项新语义 check

1. check_brainregion_semantic_zombie - description 含"被删除"标记
2. check_entity_chunks_source_id_mismatch - entity_chunks 跟 GraphML d3 source_id 不一致
3. check_chunk_shared_by_too_many_entities - 一个 chunk 被过多 entity 共享
4. check_vdb_entities_orphan - vdb 有向量但 GraphML 没 node（反向孤儿，防御性）
5. check_brainregion_orphan_chunks - text_chunks 有 brain_xxx 但 GraphML 没 brain_xxx，或 chunk content 含"被删除"标记

> 注：原计划有 6 项，`check_brainregion_size_mismatch` 因在真实数据上无效（16 个僵尸
> brain_meta_size:0 + 实际 0 条包含 edge 一致）已删除，见 Task 3。

## 1 项新语义 repair 函数（覆盖 5 个语义 check）

1. `repair_brainregion_zombies` - 完整 8 存储清理僵尸脑区（覆盖 5 个语义 check 的修复需求）
2-5. 通过 repair_all 调用链集成

## 与删除工具 bug 的关系

本次不修删除工具 bug（LightRAG adelete_by_entity 只删 3 存储）。
检查+修复工具能独立清理掉已经存在的残留——亡羊补牢能力。
删除工具的修复留到下一个计划。
