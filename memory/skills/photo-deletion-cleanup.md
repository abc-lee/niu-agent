---
name: photo-deletion-cleanup
description: Use when removing ingested photos, person profiles (misdetected or duplicate), or any knowledge-graph entity from Niu
status: active
created: 2026-10-13
last_tested: 2026-10-16
issue_count: 0
---

# 照片 / 人物 / 实体的删除

删除分三层，每层只清**属于该对象自己**的东西：

| 层 | 做什么 |
|---|---|
| 图 | `lightrag_delete_entity` 删节点，它的边随之自动删除 —— 删到这里为止 |
| 块（检索文本） | 只清该对象自己的**簿记块**（内容形如 `名字: 名字` 或 `名字: 名字（person_id=…）`）；照片自己的块随照片删 |
| DB | 删该对象的行（`photos` / `faces` / `co_occurrences` / `persons`） |

保留不删：以被删对象为端点的**关系块**（`X->Y: 关键词`）、带真实描述的实体块、提炼块、梦境块、被提及的共享人物与地点实体、别人的簿记块。

## 一、删实体（含人物）前：反写入

入库时新实体的描述会被并进邻居的描述。删实体前先把它反过来，否则邻居描述会继续指向已删的名字。

1. 读邻居：`disk("/lightrag/lightrag_get_graph explore --entity <实体名> --depth 1")`
2. 对描述里提到该实体的邻居，改写它的描述：去掉依赖该实体存在的内容，其余保持原样
3. 写回：`disk("/lightrag/lightrag_edit_entity <邻居名> --description <改写后的描述>")`
4. 删实体：`disk("/lightrag/lightrag_delete_entity <实体名>")`，返回 `success` 才算完成

第 1 步先于第 4 步（节点删完就查不到邻居）。参数含空格时用引号包住。

## 二、删一张已入库照片（四步全做）

1. **文件**：`bash rm -f <file_path>`（入库后在 `work/YYYY/MM/YYYY-MM-DD/` 下），删后 `ls` 确认已消失。
2. **图谱实体**：照片实体名 = 入库时间戳（如 `20261013_103355`）；另有语义描述实体（如`营业执照照片`）时逐个删。
   `disk("/lightrag/lightrag_delete_entity 20261013_103355")`
3. **DB + 块 + 向量**：`disk("/photo/cleanup_deleted_photos")`（通常交 file-processor 执行）。它是**全库扫描**，会清掉所有孤儿记录而非只清目标那条。
   报返回体每个字段的原值：`deleted_photos` / `deleted_faces` / `kg_entities_deleted` / `kg_relations_deleted` / `kg_chunks_deleted` / `media_vectors_removed` / `kg_errors`；`kg_errors` 逐条列出。
   - `kg_chunks_deleted` = 这张照片自己的块（text_chunks，连带向量）数
   - `media_vectors_removed` = 本轮移除的媒体向量数，`0` = 本来就没有
   - 它清的是这张照片自己的痕迹；共享的人物 / 地点实体不动
4. **清空核查**（用户问「删干净了吗」时）：四项计数 —— `photos.db` 的 photos / faces / co_occurrences；`~/.niu/media_vectors.json` 的 entries 条数；`work/YYYY/MM/**` 下的图片文件；图谱中 `entity_type=Photo` 的实体数。四项都为 0；`persons` **单独报**（它是姓名 + 人脸中心向量的人脸档案，与具体照片解耦，照片删完仍应保留）。

## 三、删一个（误识别 / 重复的）人物

1. **反写入**：对**该人物**的直接邻居做第一节的四步。
2. **删档案**：`disk("/photo/delete_person <person_id>")`
   - 删 `faces` / `co_occurrences` / `persons` 行 + 图谱节点（边随之），并清该人物自己的簿记块、把受影响照片的真实块与 abstract 里的人名去掉。
   - 报返回体里的 `kg_chunks_deleted` / `photo_texts_refreshed` / `kg_errors`；`kg_errors` 逐条列出。
   - 关系块、带描述的知识块、提炼 / 梦境内容、共享人物与地点实体保持不动 —— 与该人物相关的**知识仍可检索**。
3. 确认是误识别时，先按 `face-mismerge-split.md` 排除「真人脸被并进这一条」，再删。

## 四、改名 / 合并后的旧名残留

`name_person` / `merge_persons` 会自动刷新受影响照片的文本层（DB abstract + 照片真实块 + 人物虚拟块），返回 `abstracts_refreshed` / `chunks_refreshed` / `kg_errors`，照实报。发现旧名仍留在块里时，交由 photo-server 侧走同一刷新路径补做一次，不改块文本。

## 报告口径

- `cleanup_deleted_photos` 是全库扫描：报**实际清理条数**。
- `cleanup_deleted_photos` / `delete_person` 返回的 `kg_errors` 非空时逐条列出。
- 被删对象的 `persons` 档案、知识块是否保留，一并说明（保留是设计使然）。
