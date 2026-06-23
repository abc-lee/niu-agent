# 飞书照片显示修复 + 照片展示格式统一 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复飞书端照片重复显示 bug，统一照片展示为标准 Markdown 格式，去掉非标准 person_id|auto_label 格式

**Architecture:** 照片展示统一用 `![描述](路径)` 标准格式。飞书端流式推送已处理的图片不再通过 resolve_outbound_content 重复发送。person_id 不再塞在 alt 文本中，通过子 Agent 返回的 JSON 传递。提示词中明确要求展示人脸时必须先读 skill 获取红框照片。

**Tech Stack:** Python (feishu_channel.py), Markdown (niu.md, photo-face-display.md), JavaScript (chat.html)

---

## File Structure

| 文件 | 职责 |
|------|------|
| `niu_api/channel/feishu_channel.py` | 修复 resolve_outbound_content：流式推送已处理的图片不重复发送；修复 send() 卡片未创建时图片丢失 |
| `config/agents/niu.md` | 统一照片展示格式，去掉 person_id\|auto_label，强调展示人脸必须先读 skill |
| `~/.niu/skills/photo-face-display.md` | 统一为标准 `![描述](路径)` 格式，去掉 person_id 在 alt 中的说明 |
| `ui/assistant/chat.html` | 去掉 alt 文本中 person_id 的提取逻辑（存了但没用） |
| `niu_api/channel/feishu_channel.py` (_filter_media_markers) | alt 文本不再解析 person_id\|name 格式，统一用整段 alt 作为显示名 |

---

### Task 1: 修复 resolve_outbound_content 图片重复发送

**Files:**
- Modify: `niu_api/channel/feishu_channel.py:977-994`

**当前代码**（第977-994行）：所有本地图片都生成独立 `ResolvedMessage(kind="image")` + 替换为 `↑ 照片` 文字。

**改为**：流式推送已处理的图片（路径在 `_stream_sent_media_paths` 中）跳过，不移除标记（卡片已嵌入）；未处理的图片保留原有逻辑（上传+独立发送）。

- [ ] **Step 1: 修改 resolve_outbound_content 的 is_image 分支**

将第977-994行替换为：

```python
if is_image:
    img_path = _normalize_path(raw_path)
    if not _is_local_path(img_path):
        # URL/data URI 图片不允许，跳过
        continue
    if img_path and img_path in self._stream_sent_media_paths:
        # 流式推送已处理（已嵌入卡片），移除标记避免重复，不独立发送
        replacements.append((start_idx, end_idx, ""))
        continue
    if not img_path:
        replacement = "[图片信息缺失]"
    elif not Path(img_path).exists():
        replacement = "[图片不存在]"
    else:
        display_name = alt_text or "照片"
        media_messages.append(ResolvedMessage(kind="image", local_path=img_path, caption=alt_text))
        replacement = f"↑ {display_name}的照片" if alt_text else "↑ 照片"
    replacements.append((start_idx, end_idx, replacement))
```

- [ ] **Step 2: 修复 send() 中卡片未创建时的图片丢失**

当 `_stream_card_created = False`（卡片未创建成功）时，`_filter_media_markers` 已将图片路径加入 `_stream_sent_media_paths`，但 `send()` 的 fallback 路径不经过 `route_out`，导致 `resolve_outbound_content` 的去重跳过这些图片，图片丢失。

在 `send()` 方法中，plain markdown 发送之前（约第419行），添加卡片未创建时的图片发送逻辑：

```python
# 卡片未创建时，清理去重集并发送待处理图片
if not self._stream_card_created and (self._stream_pending_images or self._stream_pending_files):
    self._stream_sent_media_paths.clear()
    try:
        await self._send_pending_media(channel_id)
    except Exception as me:
        logger.error(f"[FeishuStream] Send pending media (no card) failed: {me}")
```

- [ ] **Step 3: Python 语法验证**

Run: `python3 -c "import py_compile; py_compile.compile('niu_api/channel/feishu_channel.py', doraise=True); print('OK')"`

- [ ] **Step 4: Commit**

```bash
git add niu_api/channel/feishu_channel.py
git commit -m "fix: skip already-streamed images in resolve_outbound_content to prevent duplicate on feishu"
```

---

### Task 2: 统一 niu.md 照片展示格式

**Files:**
- Modify: `config/agents/niu.md:145-168`

**改动要点**：
1. 照片展示统一用 `![描述](本地路径)`，去掉 `![person_id|auto_label](boxed_path)` 格式
2. 明确要求：展示带人脸红框的照片时，必须先读 `~/.niu/skills/photo-face-display.md` skill，按 skill 指引调子 Agent 获取 `boxed_path`
3. person_id 从子 Agent 返回的 JSON 获取，不塞在 alt 文本中

- [ ] **Step 1: 替换"照片与文件引用"部分**

将第145-168行替换为：

```markdown
# 照片与文件引用

## 照片展示

使用 Markdown 标准图片语法 `![描述](本地路径)` 可在对话中展示照片，路径必须是本地绝对路径。

**展示带人脸红框的照片时，必须先读取 skill：`~/.niu/skills/photo-face-display.md`**，按 skill 指引调用子 Agent 获取 `boxed_path`（带红框的图片路径），然后用 `![人物名](boxed_path)` 展示。禁止用原图路径代替红框路径。

当子Agent返回照片的地点只有坐标，没有位置信息时，说明高德API Key没有设置。阅读 docs/manual-amap-setup.md

## 人物命名传参

当用户给未命名人物命名时：
1. 从子Agent返回的JSON中找到对应人物的 `id`（UUID格式）
2. 调用 `chat-with-file-processor("用name_person工具命名：person_id=368f1c93-944b-4adf-88f9-e5eda47dc474 改名为 张三")`

**禁止只传名字不传UUID**。person_id必须从子Agent返回的JSON获取，不从参考知识注入获取（向量检索不可靠）。
```

- [ ] **Step 2: Commit**

```bash
git add config/agents/niu.md
git commit -m "docs: unify photo display to standard Markdown format, require skill for face photos"
```

---

### Task 3: 统一 photo-face-display.md skill 格式

**Files:**
- Modify: `~/.niu/skills/photo-face-display.md`

**改动要点**：
1. 前端展示格式改为 `![人物名](boxed_path)`，去掉 person_id 在 alt 中的说明
2. person_id 只通过 JSON 传递，不在 Markdown 中编码
3. 示例更新

- [ ] **Step 1: 替换"前端展示格式"部分（第17-30行）**

将第17-30行替换为：

```markdown
## 前端展示格式

使用 Markdown 标准图片语法展示带人脸红框的照片：

```
![人物名](boxed_path)
```

**参数说明**：
- `人物名`：从子Agent返回的 `auto_label` 字段提取（如"未命名人物_1"），作为图片描述文字
- `boxed_path`：从子Agent返回的 `boxed_path` 字段提取，完整绝对路径，禁止修改

person_id 不编码在 Markdown 中，仅通过子Agent返回的 JSON `id` 字段传递，用于后续命名操作。
```

- [ ] **Step 2: 更新示例（第50-58行）**

将示例中的格式从 `![uuid|name](path)` 改为 `![name](path)`：

```markdown
**示例回复**：
```
查询到 3 个未命名人物：

![未命名人物_1](/Users/xxx/.niu/tmp/facebox_88ce85b64781.png)
![未命名人物_2](/Users/xxx/.niu/tmp/facebox_de53c91d05c1.png)

这是谁？请告诉我名字。
```
```

- [ ] **Step 3: 更新场景3示例（第72-78行）**

```markdown
逐个展示未命名人物，每次展示一个：
```
![未命名人物_1](/Users/xxx/.niu/tmp/facebox_88ce85b64781.png)

这是谁？请告诉我名字。
```
```

- [ ] **Step 4: 更新常见错误表（第126-133行）**

去掉"简写格式省略person_id"行，更新"alt中用name(null)"行：

```markdown
| 问题 | 正确做法 |
|------|---------|
| person_id用facebox hash | `facebox_88ce85b64781` 是临时文件哈希，不是person_id。正确格式是UUID如 `368f1c93-944b-4adf-88f9-e5eda47dc474`，从JSON的 `id` 字段获取 |
| 修改boxed_path | 必须原样使用子Agent返回的 `boxed_path`，禁止修改或编造路径 |
| 多人照没有红框 | 必须用 `boxed_path` 而非 `file_path` |
| alt中用name(null) | 未命名人物 `name` 为 null，alt必须用 `auto_label` |
```

- [ ] **Step 5: Commit**

```bash
git add ~/.niu/skills/photo-face-display.md
git commit -m "docs: unify photo-face-display skill to standard Markdown format"
```

---

### Task 4: 去掉前端 person_id 提取逻辑

**Files:**
- Modify: `ui/assistant/chat.html:971-980`

**当前代码**（第971-980行）：从 alt 文本中提取 person_id 存入 `img.dataset.personId`，但没有任何地方使用这个值。

- [ ] **Step 1: 删除 person_id 提取逻辑**

删除第971-980行的代码块：

```javascript
// 处理 alt 文本中的 person_id|name 格式
const alt = img.getAttribute('alt') || '';
if (alt && alt.includes('|')) {
  const parts = alt.split('|');
  // person_id 是 UUID 格式
  if (parts[0] && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(parts[0])) {
    img.dataset.personId = parts[0];
    img.setAttribute('alt', parts[1] || '');
  }
}
```

- [ ] **Step 2: 更新相关注释**

第933行注释从：
```javascript
// Markdown 图片 ![person_id|name](path) 由 marked.js 渲染，然后后处理添加交互
```
改为：
```javascript
// Markdown 图片 ![描述](路径) 由 marked.js 渲染，然后后处理添加交互
```

第949行注释从：
```javascript
// 后处理：将本地路径图片转为 file:/// URL，并提取 person_id 添加交互
```
改为：
```javascript
// 后处理：将本地路径图片转为 file:/// URL 并添加交互
```

- [ ] **Step 3: Commit**

```bash
git add ui/assistant/chat.html
git commit -m "refactor: remove unused person_id extraction from image alt text"
```

---

### Task 5: _filter_media_markers 简化 alt 处理

**Files:**
- Modify: `niu_api/channel/feishu_channel.py:594-600`

**当前代码**（第594-600行）：解析 alt 中的 `person_id|name` 格式，提取 name 作为显示名。

**改为**：直接用整段 alt 文本作为显示名，不再解析 `|` 分隔符。

- [ ] **Step 1: 简化 alt 处理**

将第594-600行：

```python
name = alt_text
if "|" in alt_text:
    parts = alt_text.split("|", 1)
    if len(parts[0]) >= 8 and "-" in parts[0]:
        name = parts[1]
    else:
        name = alt_text
```

替换为：

```python
name = alt_text or "照片"
```

- [ ] **Step 2: Python 语法验证**

Run: `python3 -c "import py_compile; py_compile.compile('niu_api/channel/feishu_channel.py', doraise=True); print('OK')"`

- [ ] **Step 3: Commit**

```bash
git add niu_api/channel/feishu_channel.py
git commit -m "refactor: simplify alt text handling in _filter_media_markers, no person_id parsing"
```

---

## Self-Review

### 1. Spec coverage

- 飞书照片重复显示修复 → Task 1 ✅
- 照片展示统一为标准格式 → Task 2, 3 ✅
- 防止主Agent偷懒不获取红框照片 → Task 2（提示词中明确要求先读 skill）✅
- 去掉 person_id 在 alt 中的编码 → Task 2, 3, 4, 5 ✅
- push/无卡片路径图片不丢失 → Task 1（只跳过 _stream_sent_media_paths 中的路径）✅

### 2. Placeholder scan

无 TBD/TODO/placeholder。

### 3. Type consistency

- `resolve_outbound_content` 返回 `list[ResolvedMessage]`，Task 1 不改变返回类型 ✅
- `_filter_media_markers` 返回 `str`，Task 5 不改变返回类型 ✅
