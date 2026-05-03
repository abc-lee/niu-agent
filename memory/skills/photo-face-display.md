---
name: photo-face-display
description: Use when user asks about unnamed persons, wants to name someone in photos, or queries face recognition results
---

# Photo Face Display

## Overview

当用户查询未命名人物时，主 Agent 需要在回复中展示照片和人脸框，方便用户识别人物并命名。
- 单人照：直接用原照片路径
- 多人照：必须调用 `get_person_photos` 获取 `boxed_path`（带人脸红框），用 `::person_photo::` 标记展示

## Mark Format

使用 `::person_photo::` 标记展示带人脸红框的照片：

```
::person_photo::{"path": "带框图路径", "person_id": "人物ID", "name": "人物名"}::
```

**参数说明**：
- `path`: 带人脸红框的图片路径（从子 Agent 返回的 `boxed_path`，已画好红框，存于 ~/.niu/tmp/）
- `person_id`: 人物ID（用于后续命名）
- `name`: 人物名称（未命名人物通常是 "未命名人物_N"）

**注意**：后端已在原图上画好红框并保存到临时目录。

## Scenarios

### Scenario 1: Query unnamed persons

**用户**：有多少未命名人物？

**操作**：
1. 调用 `chat-with-file-processor("查询未命名人物")`
2. 子 Agent 返回 JSON 数据，包含 `persons` 数组
3. 遍历 `persons` 数组，为每个人物生成 `::person_photo::` 标记

**示例回复**：
```
查询到 3 个未命名人物：

::person_photo::{"path": "C:/Users/X/.niu/tmp/abc123.png", "person_id": "uuid-1", "name": "未命名人物_8"}::

这是谁？请告诉我名字。
```

### Scenario 2: User answers name

**用户**：这是李四

**操作**：
1. 提取之前返回的 `person_id`（例如 "uuid-1"）
2. 调用 `chat-with-file-processor("命名人物：uuid-1 改名为 李四")`
3. 返回结果给用户

### Scenario 3: Multiple persons

每个人物生成一个标记：

```
::person_photo::{"path": "C:/Users/X/.niu/tmp/img1.png", "person_id": "uuid-1", "name": "未命名人物_8"}::
::person_photo::{"path": "C:/Users/X/.niu/tmp/img2.png", "person_id": "uuid-2", "name": "未命名人物_9"}::
::person_photo::{"path": "C:/Users/X/.niu/tmp/img3.png", "person_id": "uuid-3", "name": "未命名人物_10"}::

请告诉我这些人的名字。
```

## Sub Agent Return Format

`get_unnamed_persons()` 返回的 JSON 格式：

```json
{
  "status": "success",
  "count": 3,
  "persons": [{
    "id": "uuid-1",
    "name": null,
    "auto_label": "未命名人物_8",
    "photo_count": 5,
    "photos": [
      {"file_path": "REDACTED_WIN_PATH/.../photo.jpg", "boxed_path": "C:/Users/X/.niu/tmp/abc123.png"}
    ]
  }]
}
```

- `photos` 数组包含多张代表照片，选择第一张或轮流展示
- `boxed_path` 是后端已画好红框的图片路径（存于 ~/.niu/tmp/）
- 使用 `id` 字段作为 `person_id`

## Common Mistakes

| 问题 | 解决方案 |
|------|---------|
| 自己生成标记 | 等待子 Agent 返回数据后再转换 |
| person_id 丢失 | **必须使用子Agent返回的 `id` 字段（UUID格式）作为 `person_id`**。绝对不能用 `boxed_path` 文件名中的 `facebox_xxx` hash 代替 UUID，那只是临时文件名的哈希值，不是数据库ID |
| 用facebox_hash当person_id | `facebox_88ce85b64781` 是临时文件哈希，不是 person_id。正确的 person_id 格式是 UUID 如 `a4317e63-23fd-4edd-b543-3600e8c5c52e`。使用 facebox hash 会导致 PERSON_NOT_FOUND 错误 |
| 多人照没有红框 | 必须用 `boxed_path`，不能直接用 `file_path` |

## Frontend Rendering

前端自动解析 `::person_photo::` 标记：显示带红框的照片，双击可用系统查看器打开。