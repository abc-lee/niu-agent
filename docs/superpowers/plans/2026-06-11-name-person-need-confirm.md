# name_person 同名检测（need_confirm）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `name_person()` 在执行命名前检测数据库是否已存在同名人物，存在则返回 `need_confirm` 状态让用户确认，防止不同人被错误合并。

**Architecture:** 将 `name_person()` 中所有数据库读写（SELECT + 同名检测 + UPDATE）统一放入 `_db_write_lock` 保护范围内，彻底消除 TOCTOU 并发问题。检测到同名时返回 `need_confirm` JSON（含 `merge_suggestion` 明确指定合并参数），由主Agent展示给用户决策。KG 层已有 `_merge_duplicate_person_entities` 处理同名合并，无需修改。

**Tech Stack:** Python, SQLite

---

## 修改文件

| 操作 | 文件 | 说明 |
|------|------|------|
| Modify | `mcp-servers/photo-server/src/niu_photo_server/__init__.py:2267-2354` | `name_person()` 增加同名检测逻辑 |
| Modify | `~/.niu/skills/photo-face-display.md` 场景4 | 同步更新 need_confirm JSON 示例，添加 `name` 和 `merge_suggestion` 字段 |

---

## 场景分析

**两个数据来源会产生人物实体：**

1. **照片入库** → SQLite `persons` 表 + KG 人名实体（通过 `sync_photo_to_kg` + `name_person` 写入）
2. **文档入库** → KG 人名实体（通过 LightRAG `ainsert` 自动提取，仅 KG，SQLite 无记录）

**需要处理的场景：**

| 场景 | SQLite | KG | 当前行为 | 修改后行为 |
|------|--------|-----|---------|-----------|
| 命名新人物，DB 无同名 | 无同名记录 | 可能有/无 | 直接命名 | 直接命名（不变） |
| 命名新人物，DB 有同名 | 有同名记录 | 可能有同名实体 | 直接覆盖（BUG） | 返回 `need_confirm` |
| 用户确认是同一人 | 有同名记录 | — | — | 调用 `merge_persons` 合并 |
| 用户确认只是同名 | 有同名记录 | — | — | 用户换名字重新 `name_person` |

**为什么 KG 不需要额外修改：**
- `name_person` 后续调用的 `merge_entities` + `_merge_duplicate_person_entities` 已经处理 KG 同名合并
- `merge_persons` 中的 KG 逻辑也已有处理
- 场景 2（文档入库先建了 KG 人名实体）不影响 `name_person`，因为 KG 实体名和 SQLite `name` 字段是独立存储

**核心逻辑：** `name_person` 只需检查 SQLite `persons` 表中是否有同名人物。这是唯一会出错的层面。

---

### Task 1: 在 name_person() 中增加同名检测

**Files:**
- Modify: `mcp-servers/photo-server/src/niu_photo_server/__init__.py:2267-2354`

- [ ] **Step 1: 将 SELECT + 同名检测 + UPDATE 统一放入 _db_write_lock**

将 `name_person()` 中从 `# Check if person exists` 注释开始到 `conn.commit()` 的整段代码替换为以下结构（所有 DB 读写都在锁内）：

```python
        # All DB reads/writes inside _db_write_lock to prevent TOCTOU
        with _db_write_lock:
            cursor = conn.execute(
                "SELECT id, name, auto_label, photo_count FROM persons WHERE id = ?", (person_id,)
            )
            row = cursor.fetchone()

            if not row:
                return {
                    "status": "error",
                    "error_code": "PERSON_NOT_FOUND",
                    "message": f"Person not found: {person_id}",
                }

            current_name = row[1]  # name column
            auto_label = row[2]    # auto_label column
            current_photo_count = row[3] or 0  # photo_count

            # Same-name detection: check if another person with the same name already exists
            if name and name != current_name:
                dup_cursor = conn.execute(
                    "SELECT id, name, auto_label, photo_count FROM persons WHERE name = ? AND id != ?",
                    (name, person_id),
                )
                dup_row = dup_cursor.fetchone()

                if dup_row:
                    return {
                        "status": "need_confirm",
                        "message": f"已存在名为\"{name}\"的人物",
                        "current_person": {
                            "person_id": person_id,
                            "name": current_name,
                            "auto_label": auto_label,
                            "photo_count": current_photo_count,
                        },
                        "existing_person": {
                            "person_id": dup_row[0],
                            "name": dup_row[1],
                            "auto_label": dup_row[2],
                            "photo_count": dup_row[3] or 0,
                        },
                        "merge_suggestion": {
                            "person_a_id": dup_row[0],   # existing_person — 保留（已命名、照片多）
                            "person_b_id": person_id,    # current_person — 合并后删除
                        },
                        "hint": "请确认：这是同一个人吗？如果是，请调用 merge_persons(person_a_id, person_b_id) 合并；如果只是同名，请换一个名字重新命名",
                    }

            conn.execute("UPDATE persons SET name = ? WHERE id = ?", (name, person_id))
            conn.commit()
```

**关键设计决策：**
- **全锁保护**：SELECT + 同名检测 + UPDATE 都在 `_db_write_lock` 内，彻底消除 TOCTOU
- **`name != current_name`**：如果此人已经是这个名字，跳过检测直接更新（幂等）
- **`name` 非空检查**：`if name and name != current_name` — 空字符串或 None 不触发检测
- **`WHERE name = ? AND id != ?`**：排除自身，防止命名自己已有名字时误触发
- **`merge_suggestion`**：明确指定 `merge_persons` 的参数映射，避免主Agent传反 person_a/person_b 导致已命名人物被删除
  - `person_a_id` = existing_person（保留，因为已有名字且照片多）
  - `person_b_id` = current_person（合并后删除）
- **返回格式**：匹配 Skill 文档 `photo-face-display.md` 场景 4，`current_person` 和 `existing_person` 均包含 `name` 字段

- [ ] **Step 2: 验证代码语法正确**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -c "import ast; ast.parse(open('mcp-servers/photo-server/src/niu_photo_server/__init__.py').read()); print('Syntax OK')"
```

- [ ] **Step 3: 同步更新 Skill 文档场景4的 JSON 示例**

将 `~/.niu/skills/photo-face-display.md` 场景4的返回示例更新，添加 `name` 和 `merge_suggestion` 字段：

```json
{
  "status": "need_confirm",
  "message": "已存在名为\"刘永辉\"的人物",
  "current_person": {"person_id": "uuid-b", "name": null, "auto_label": "未命名人物_2", "photo_count": 1},
  "existing_person": {"person_id": "uuid-a", "name": "刘永辉", "auto_label": "刘永辉", "photo_count": 3},
  "merge_suggestion": {"person_a_id": "uuid-a", "person_b_id": "uuid-b"},
  "hint": "请确认：这是同一个人吗？如果是，请调用 merge_persons(person_a_id, person_b_id) 合并；如果只是同名，请换一个名字重新命名"
}
```

同时更新场景4的**主Agent处理**说明：

```
**主Agent处理**：将 need_confirm 结果展示给用户，让用户决定：
- **是同一个人** → 调用 `merge_persons(person_a_id=merge_suggestion.person_a_id, person_b_id=merge_suggestion.person_b_id)` 合并
- **只是同名** → 换一个名字重新调用 `name_person`
```

- [ ] **Step 4: 提交**

```bash
git add mcp-servers/photo-server/src/niu_photo_server/__init__.py
git commit -m "feat: add same-name detection to name_person — return need_confirm when duplicate name exists"
```

---

## 不需要修改的部分

| 组件 | 原因 |
|------|------|
| `merge_persons()` | 已有完整合并逻辑（DB + KG + embedding），无需改动 |
| `_merge_duplicate_person_entities()` | KG 同名合并已有，`name_person` 命名成功后仍会调用 |
| 主Agent 提示词 | Skill 已指导主Agent如何处理 `need_confirm`，无需额外修改 |
