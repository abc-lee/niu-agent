---
name: photo-face-display
description: Use when user asks about unnamed persons, wants to name someone in photos, or queries face recognition results
status: active
created: 2026-06-02
last_tested: 2026-06-21
---

# Photo Face Display

## Overview

当用户查询未命名人物时，主 Agent 需要在回复中展示带人脸红框的照片，方便用户识别人物并命名。
- 子Agent返回的JSON中已包含 `boxed_path`（带人脸红框的图片路径），直接使用即可
- 不需要额外调用 `get_person_photos`（除非用户说"换一张照片")

## 前端展示格式

使用 Markdown 标准图片语法展示带人脸红框的照片：

```
![人物名](boxed_path)
```

**参数说明**：
- `人物名`：从子Agent返回的 `auto_label` 字段提取（如"未命名人物_1"），作为图片描述文字
- `boxed_path`：从子Agent返回的 `boxed_path` 字段提取，完整绝对路径，禁止修改

person_id 不编码在 Markdown 中，仅通过子Agent返回的 JSON `id` 字段传递，用于后续命名操作。

## 命名传参规则

用户说"这是张三"时，主Agent需要从子Agent返回的JSON中找到对应人物的 `id` 字段（UUID），传给子Agent：
`chat-with-file-processor("用name_person工具命名：person_id=368f1c93-944b-4adf-88f9-e5eda47dc474 改名为 张三")`

**person_id 来源**：子Agent返回的JSON `id` 字段，不要从Markdown alt文本解析，不要从参考知识注入中获取（向量检索不可靠）。

## 场景

### 场景1：查询未命名人物

**用户**：有多少未命名人物？

**操作**：
1. 调用 `chat-with-file-processor("查询未命名人物")`
2. 子Agent原样返回JSON（包含 `id`、`auto_label`、`boxed_path` 等所有字段）
3. 从JSON中提取 `id`→person_id、`auto_label`→alt名称、`boxed_path`→图片路径，生成Markdown图片

**示例回复**：
```
查询到 3 个未命名人物：

![未命名人物_1](/Users/xxx/.niu/tmp/facebox_88ce85b64781.png)
![未命名人物_2](/Users/xxx/.niu/tmp/facebox_de53c91d05c1.png)

这是谁？请告诉我名字。
```

### 场景2：用户回答名字

**用户**：这是李四

**操作**：
1. 从子Agent返回的JSON中找到对应人物的 `id` 字段（UUID格式）
2. 调用 `chat-with-file-processor("用name_person工具命名：person_id=368f1c93-944b-4adf-88f9-e5eda47dc474 改名为 李四")`
3. 将子Agent的命名结果转述给用户

**关键**：person_id 必须从子Agent返回的JSON获取，不要从参考知识注入中获取。

### 场景3：多人逐个展示

逐个展示未命名人物，每次展示一个：
```
![未命名人物_1](/Users/xxx/.niu/tmp/facebox_88ce85b64781.png)

这是谁？请告诉我名字。
```

用户回答后，从JSON中提取该人物的id，传给子Agent命名。

### 场景4：同名人物确认

`name_person` 会自动检测同名：如果数据库中已存在同名人物，返回 `need_confirm` 状态而非直接命名。

**返回示例**：
```json
{
  "status": "need_confirm",
  "message": "已存在名为\"刘永辉\"的人物",
  "current_person": {"person_id": "uuid-b", "auto_label": "未命名人物_2", "photo_count": 1},
  "existing_person": {"person_id": "uuid-a", "auto_label": "刘永辉", "photo_count": 3},
  "hint": "请确认：这是同一个人吗？如果是，请调用 merge_persons 合并；如果只是同名，请换一个名字重新命名"
}
```

**主Agent处理**：将 need_confirm 结果展示给用户，让用户决定：
- **是同一个人** → 调用 `merge_persons(person_a_id=uuid-a, person_b_id=uuid-b)` 合并
- **只是同名** → 换一个名字重新调用 `name_person`

**为什么不能自动合并**：同名可能是两个不同的人（如两个都叫"张伟"），自动合并会把长相差异大的人的向量混在一起，降低人脸识别精度。

## 子Agent返回格式

`get_unnamed_persons()` 返回的JSON（子Agent原样透传，不做格式转换）：

```json
{
  "status": "success",
  "count": 2,
  "persons": [
    {"id": "368f1c93-944b-4adf-88f9-e5eda47dc474", "name": null, "auto_label": "未命名人物_1", "photo_count": 1, "photos": [{"file_path": "/path/photo.jpg", "boxed_path": "/Users/xxx/.niu/tmp/facebox_88ce85b64781.png"}]},
    {"id": "a1b2c3d4-5678-90ab-cdef-1234567890ab", "name": null, "auto_label": "未命名人物_2", "photo_count": 3, "photos": [{"file_path": "/path/photo2.jpg", "boxed_path": "/Users/xxx/.niu/tmp/facebox_de53c91d05c1.png"}]}
  ]
}
```

**字段用途**：
- `id`：人物UUID，命名时传给name_person的person_id参数 — **这是最重要的字段，绝不能丢失**
- `name`：人物名字，未命名时为 null，展示时用 `auto_label` 代替
- `auto_label`：自动标签（如"未命名人物_1"），用于Markdown图片alt和前端显示名
- `photos[0].boxed_path`：带人脸红框的图片完整路径，用在Markdown图片的路径部分
- `photo_count`：该人物出现的照片数量

## 常见错误

| 问题 | 正确做法 |
|------|---------|
| person_id用facebox hash | `facebox_88ce85b64781` 是临时文件哈希，不是person_id。正确格式是UUID如 `368f1c93-944b-4adf-88f9-e5eda47dc474`，从JSON的 `id` 字段获取 |
| 修改boxed_path | 必须原样使用子Agent返回的 `boxed_path`，禁止修改或编造路径 |
| 多人照没有红框 | 必须用 `boxed_path` 而非 `file_path` |
| alt中用name(null) | 未命名人物 `name` 为 null，alt必须用 `auto_label` |

<!-- 执行提醒 -->
<!-- 此区域用于重申已有规则，不引入新规则。规则没错但没被遵守时在这里添加提醒。 -->
<!-- 提醒：展示人脸照片时必须使用工具返回的boxed_path，禁止根据photo_id自行编造路径文件名；第二次展示时同样必须从工具结果获取路径。 -->
