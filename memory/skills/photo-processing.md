# 照片处理 Skill

description: 照片处理技能，包括入库、人脸识别、人物命名和查询
tags: [photo, face, image, 照片, 人脸]
触发关键词：照片、图片、人脸、人物、入库

## 单张照片入库

**必须调用工具 chat-with-file-processor**，参数 task="处理照片：路径"

示例工具调用：
```json
{"task": "处理照片：E:/path/to/photo.jpg"}
```

返回示例：`✅ 照片已入库，检测到 2 人`

## 批量目录处理

**必须调用工具 chat-with-file-processor**：
```json
{"task": "处理目录：E:/tmp/照片/2025/"}
```

## 人物查询

```json
{"task": "查询未命名人物"}
{"task": "搜索人物：张三"}
```

## 人物命名

```json
{"task": "命名人物：未命名人物_1 改名为 张三"}
```

## 展示人物照片

子 Agent 返回 JSON 后，展示第一张：
```
::person_photo::{"path": "...", "bbox": [...], "person_id": "uuid-1", "name": "未命名人物_8"}::
这是谁？
```

用户说"换一张"：直接展示下一张，不需要再调用子 Agent。

## 清理已删除照片

```json
{"task": "清理已删除照片的数据库记录"}
```
