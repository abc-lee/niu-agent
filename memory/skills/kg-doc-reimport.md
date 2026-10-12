---
name: kg-doc-reimport
description: Use when a document needs to be re-imported into the knowledge graph and shows "Content already exists" error, or when deleting a document from KG requires complete cleanup
status: active
issue_count: 0
created: 2026-08-18
last_tested: 2026-10-12
---

# Knowledge Graph Document Re-import

## Overview
文档重新入库时，仅删除文件和实体不足以彻底清理，必须用 `lightrag_delete_document` 按 doc_id 删除向量库中的文档数据（含 chunks、entities、relationships），否则会报 "Content already exists" 错误。

## When to Use

- 文档入库时报 "Content already exists" 错误
- 需要彻底删除知识图谱中的某篇文档并重新入库
- 文件已删除但知识图谱仍残留文档数据

## Steps

1. **拿到 doc_id（三种来源，按顺序试）**
   - a. 若报过 "Content already exists"：错误信息里有 `Original doc_id: doc-xxxxxxxx`，直接用。
   - b. **否则自己查（首选，不要先去问人）**：调用
     `lightrag_list_entities(list_type="documents")`，它返回
     `{doc_id: {status, file_path, chunks_count, ...}}` —— 按**文件路径**反查出 doc_id。
   - c. 两条都不行，才去问用户。
   - **doc_id 格式是 `doc-<32位hash>`，绝不是文件路径。禁止拿文件路径去试 `lightrag_delete_document`。**

2. **删除向量库文档**：调用 `lightrag_delete_document <doc_id>`，级联删除该文档的
   chunks、entities、relationships（**共享实体会保留并重建描述**）。
   - **返回值里带 `"note": "not_found_treated_as_ok"` → 一律当作失败处理**：
     说明传进去的 id 在库里不存在，**什么都没删**。必须回到第 1 步重新确认 doc_id，
     **不得向用户汇报"已删除"**。
     （2026-10-12 实测教训：拿文件路径当 doc_id → 返回该 note → 仍被当成删除成功，实际零删除。）
   - 只有返回 `status="success"`（`result` 里是 `DeletionResult(status='success', status_code=200)`）
     才算真的删掉。

3. **删除本地文件**：`rm -f <file_path>` 删除存储路径下的文件。

4. **验证清理结果（不能只看工具返回值）**：搜索相关实体/文档名，确认图谱里该文档的
   实体与关系已消失；若可查向量库，确认该文档的 chunk 已不存在。

5. **重新入库**：调用文件入库工具重新导入。

## Common Mistakes
- **只删文件不删向量库**：文件删了但向量库中 chunk 数据还在，二次入库仍报 Content already exists
- **只删实体不删文档**：`lightrag_delete_entity` 只删实体节点，不删向量库中的文档和 chunk 数据
- **拿文件路径当 doc_id**：`delete_document` 只认 `doc-<32位hash>`；传路径会得到
  `not_found_treated_as_ok`（零删除），却容易被误读成"已删除"
- **只看 `status` 不看 `note`**：`status:"ok"` + `note:"not_found_treated_as_ok"` = 没找到 = 没删
- **反复删了又入**：没找到根因前盲目重复操作，越搞越乱。先停，查清楚当前状态再动手

<!-- 执行提醒 -->
<!-- 此区域用于重申已有规则，不引入新规则。规则没错但没被遵守时在这里添加提醒。 -->
