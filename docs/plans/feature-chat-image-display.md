# 聊天对话框图片显示功能设计

> 目标：支持在对话框显示图片，展示人脸识别结果，实现交互式人物命名

## 背景

### 当前问题

1. **对话框不支持图片**：消息格式为纯文本（`textContent`）
2. **无法展示人脸识别结果**：用户看不到"这是谁"
3. **无法交互式命名**：Agent 无法与用户讨论人物身份

### 数据来源

- 照片存储：`E:\tmp\bot\`（结构化路径）
- 人脸数据：`photos.db` 的 `faces` 表，包含 `bounding_box`（JSON）
- 人物数据：`persons` 表，包含 `name`

---

## 技术方案

### 数据流

```
用户拖入照片
    ↓
chat.html 显示预览
    ↓
IPC: process-image → main.js
    ↓
MCP: photo-server.ingest_photo
    ↓
返回 { photo_id, file_path, faces: [{bbox, person_id, person_name}] }
    ↓
chat.html 渲染图片 + 人脸框
    ↓
Agent 收到结果，可交互询问
```

### 人脸框格式

```json
{
  "bbox": [x1, y1, x2, y2],
  "confidence": 0.95,
  "person_id": "uuid",
  "person_name": "未命名人物_8"
}
```

- 坐标系：原图像素坐标
- 渲染时需按显示尺寸缩放

---

## 实施阶段

### Phase 1: 基础图片显示

**目标**：让对话框能显示图片

**改动文件**：
| 文件 | 改动 |
|------|------|
| `chat.html` | 添加 CSS、修改 `addMessage()` |
| `preload-chat.js` | 添加 `getImageUrl` API |
| `main.js` | 添加 `get-image-url` IPC 处理器 |

**关键代码**：

```javascript
// chat.html - 增强的消息渲染
function addMessage(role, text, images = []) {
  const div = document.createElement('div');
  div.className = `message ${role}`;
  
  // 文本内容
  if (text) {
    const textDiv = document.createElement('div');
    textDiv.className = 'message-text';
    textDiv.textContent = text;
    div.appendChild(textDiv);
  }
  
  // 图片
  if (images && images.length > 0) {
    const imagesDiv = document.createElement('div');
    imagesDiv.className = 'message-images';
    images.forEach(img => {
      const imgEl = document.createElement('img');
      imgEl.src = img.src;
      imgEl.className = 'chat-image';
      imagesDiv.appendChild(imgEl);
    });
    div.appendChild(imagesDiv);
  }
  
  messages.appendChild(div);
}
```

### Phase 2: 拖放功能

**目标**：支持拖入图片文件

**改动文件**：
| 文件 | 改动 |
|------|------|
| `chat.html` | 添加 drag/drop 事件监听 |
| `preload-chat.js` | 添加 `processImage` API |
| `main.js` | 添加 `process-image` IPC 处理器 |

**关键代码**：

```javascript
// chat.html - 拖放处理
messagesDiv.addEventListener('drop', async (e) => {
  e.preventDefault();
  const files = Array.from(e.dataTransfer.files).filter(f => 
    f.type.startsWith('image/')
  );
  for (const file of files) {
    await handleDroppedImage(file);
  }
});
```

### Phase 3: 人脸框显示

**目标**：Agent 查询未命名人物时，展示照片+人脸框让用户命名

**改动文件**：
| 文件 | 改动 |
|------|------|
| `mcp-servers/photo-server/__init__.py` | `get_unnamed_persons` 返回 `representative_photo`（已完成 ✅） |
| `ui/assistant/chat.html` | 解析 Agent 消息中的图片+人脸框，渲染显示 |
| `config/agents/niu.md` | 添加消息格式规范 |

**数据格式**（Agent 调用 `get_unnamed_persons` 返回）：
```json
{
  "persons": [
    {
      "id": "uuid",
      "auto_label": "未命名人物_8",
      "photo_count": 5,
      "representative_photo": {
        "file_path": "E:/tmp/bot/2026/04/.../photo.jpg",
        "bbox": [x1, y1, x2, y2]
      }
    }
  ]
}
```

**Agent 消息格式**：
Agent 在消息中使用特殊标记让前端渲染图片+人脸框：
```
::person_photo::{"path": "E:/tmp/bot/.../photo.jpg", "bbox": [x1,y1,x2,y2], "person_id": "uuid", "name": "未命名人物_8"}::
```

前端检测 `::person_photo::` 标记并渲染图片+人脸框。

**渲染逻辑**：

```javascript
// 解析消息中的 ::person_photo:: 标记
function parsePersonPhotoMarkers(text) {
  const regex = /::person_photo::(\{.*?\})::/g;
  const results = [];
  let match;
  while ((match = regex.exec(text)) !== null) {
    results.push(JSON.parse(match[1]));
  }
  return results;
}

// 渲染图片+人脸框
async function renderPersonPhoto(data) {
  const container = document.createElement('div');
  container.className = 'image-container';
  
  const img = document.createElement('img');
  img.src = await window.electronAPI.getImageUrl(data.path);
  img.className = 'chat-image';
  img.onload = () => renderFaceBox(img, data.bbox, data.name);
  
  container.appendChild(img);
  return container;
}

// 渲染人脸框
function renderFaceBox(img, bbox, name) {
  const [x1, y1, x2, y2] = bbox;
  const scaleX = img.clientWidth / img.naturalWidth;
  const scaleY = img.clientHeight / img.naturalHeight;
  
  const box = document.createElement('div');
  box.className = 'face-box';
  box.style.left = (x1 * scaleX) + 'px';
  box.style.top = (y1 * scaleY) + 'px';
  box.style.width = ((x2 - x1) * scaleX) + 'px';
  box.style.height = ((y2 - y1) * scaleY) + 'px';
  
  const label = document.createElement('div');
  label.className = 'face-label';
  label.textContent = name;
  box.appendChild(label);
  
  img.parentElement.appendChild(box);
}
```

### Phase 4: Agent 集成

**目标**：Agent 可以与用户讨论人物身份

**改动文件**：
| 文件 | 改动 |
|------|------|
| `config/agents/niu.md` | 添加图片处理相关提示 |
| `config/agents/file-processor.md` | 添加人物命名交互逻辑 |

**交互流程**：

```
Agent: 我在这张照片里发现了 3 个人：
       [图片+人脸框]
       第一个人是"张三"
       第二个人还没名字，这是谁？
User: 这是李四
Agent: 好的，已命名为"李四"
```

---

## CSS 样式

```css
/* 图片显示 */
.message-images {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 8px;
}
.chat-image {
  max-width: 200px;
  max-height: 200px;
  border-radius: 8px;
  cursor: pointer;
  object-fit: cover;
}

/* 人脸框 */
.image-container {
  position: relative;
  display: inline-block;
}
.face-box {
  position: absolute;
  border: 2px solid #f8a7c8;
  border-radius: 4px;
}
.face-label {
  position: absolute;
  bottom: -20px;
  background: #f8a7c8;
  color: white;
  font-size: 11px;
  padding: 1px 4px;
  border-radius: 3px;
}

/* 拖放 */
.messages.drag-over {
  outline: 2px dashed #78b2be;
  background: rgba(120, 178, 190, 0.1);
}
```

---

## IPC 接口

### preload-chat.js 新增 API

```javascript
// 获取图片显示 URL
getImageUrl: (filePath) => ipcRenderer.invoke('get-image-url', filePath)

// 处理拖入的图片
processImage: (filePath) => ipcRenderer.invoke('process-image', filePath)

// 获取照片及人脸信息
getPhotoWithFaces: (photoId) => ipcRenderer.invoke('get-photo-with-faces', photoId)
```

### main.js 新增 IPC 处理器

```javascript
// 转换本地路径为 file:// URL
ipcMain.handle('get-image-url', async (event, filePath) => {
  // Windows: E:\tmp\bot\photo.jpg → file:///E:/tmp/bot/photo.jpg
  // 中文路径需要 encodeURIComponent
});

// 处理图片（调用 photo-server）
ipcMain.handle('process-image', async (event, filePath) => {
  // 调用 MCP 工具 ingest_photo
  // 返回人脸检测结果
});

// 获取照片及人脸信息
ipcMain.handle('get-photo-with-faces', async (event, photoId) => {
  // 查询数据库
});
```

---

## 注意事项

1. **Electron 安全**：使用 `file://` 协议需要处理中文路径编码
2. **图片尺寸**：原图可能很大，考虑缩略图或 CSS 限制
3. **性能**：多张图片同时显示时考虑懒加载
4. **错误处理**：图片不存在、格式不支持等情况

---

## 更新日志

- 2026-04-02: 创建设计文档
