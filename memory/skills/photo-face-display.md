# 照片人脸显示 - 使用指南

**触发关键词**：未命名、人脸、照片、改名、命名人物

**L1 摘要**：Unnamed person query|unnamed,face,photo,naming|Display photo with face bounding box (pre-drawn by backend) when querying unnamed persons, user answers name to complete naming|unnamed person,face box,photo display,naming tool,boxed_path,chat-with-file-processor,chat-with-photo-processor|skill|memory/skills/photo-face-display.md

## 概述

当用户查询未命名人物时，主 Agent 需要在回复中展示照片和人脸框，方便用户识别人物并命名。
- 单人照：直接用原照片路径
- 多人照：必须调用 `get_person_photos` 获取 `boxed_path`（带人脸红框），用 `::person_photo::` 标记展示

## 标记格式

使用特殊的 `::person_photo::` 标记来展示带人脸红框的照片：

```
::person_photo::{"path": "带框图路径", "person_id": "人物ID", "name": "人物名"}::
```

**参数说明**：
- `path`: 带人脸红框的图片路径（从子 Agent 返回的 `boxed_path`，已画好红框，存于 ~/.niu/tmp/）
- `person_id`: 人物ID（用于后续命名）
- `name`: 人物名称（未命名人物通常是 "未命名人物_N"）

**注意**：后端已在原图上画好红框并保存到临时目录。

## 使用场景

### 场景 1：查询未命名人物

**用户**：有多少未命名人物？

**你的操作**：
1. 调用 `chat-with-file-processor("查询未命名人物")`
2. 子 Agent 返回 JSON 数据，包含 `persons` 数组
3. 遍历 `persons` 数组，为每个人物生成一个 `::person_photo::` 标记

**示例回复**：
```
查询到 3 个未命名人物：

::person_photo::{"path": "C:/Users/X/.niu/tmp/abc123.png", "person_id": "uuid-1", "name": "未命名人物_8"}::

这是谁？请告诉我名字。
```

### 场景 2：用户回答名字

**用户**：这是李四

**你的操作**：
1. 提取之前返回的 `person_id`（例如 "uuid-1"）
2. 调用 `chat-with-file-processor("命名人物：uuid-1 改名为 李四")`
3. 返回结果给用户

**示例回复**：
```
✅ 已将未命名人物_8 命名为 李四
```

### 场景 3：多个人物

如果查询到多个人物，每个人物生成一个标记：

```
查询到 3 个未命名人物：

::person_photo::{"path": "C:/Users/X/.niu/tmp/img1.png", "person_id": "uuid-1", "name": "未命名人物_8"}::

::person_photo::{"path": "C:/Users/X/.niu/tmp/img2.png", "person_id": "uuid-2", "name": "未命名人物_9"}::

::person_photo::{"path": "C:/Users/X/.niu/tmp/img3.png", "person_id": "uuid-3", "name": "未命名人物_10"}::

请告诉我这些人的名字。
```

## 子 Agent 返回格式

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
      {"file_path": "E:/tmp/bot/.../photo.jpg", "boxed_path": "C:/Users/X/.niu/tmp/abc123.png"}
    ]
  }]
}
```

**注意**：
- `photos` 数组包含多张代表照片，你可以选择第一张或轮流展示
- `boxed_path` 是后端已画好红框的图片路径（存于 ~/.niu/tmp/），用这个路径作为 `::person_photo::` 标记的 `path`
- 使用 `id` 字段作为 `person_id`

## 前端渲染

前端会自动解析 `::person_photo::` 标记：
1. 显示带红框的照片（后端已画好，前端直接显示）
2. 双击照片可用系统查看器打开

## 注意事项

1. **不要自己生成标记**：等待子 Agent 返回数据后再转换
2. **保持 person_id**：用户回答名字后，使用正确的 `person_id` 调用命名工具
3. **多张照片**：可以选择展示第一张，或让用户确认后切换下一张

## 关键词

照片、人脸、未命名、改名、命名、识别人物
