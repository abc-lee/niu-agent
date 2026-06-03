# Spirit 拖入文件操作模式传递修复设计

## 问题

Spirit 小女孩拖入文件时，前端已检测修饰键区分操作模式（copy/move/reference），但 mode 信息在 main.js 中被丢弃，导致所有拖入一律按拷贝处理。

## 当前链路

```
spirit.html drop事件
  → 检测 e.shiftKey/e.ctrlKey → 确定 mode
  → 构造 context = {files: [...], mode: "reference"}
  → window.electronAPI.sendToAgent(context)

preload-spirit.js
  → ipcRenderer.send('send-to-agent', context)

main.js send-to-agent handler
  → 只用 context.files 构造消息文本 ❌ context.mode 被丢弃
  → fetch('/api/chat', { body: { message: "入库文件：/path" } })

后端 → Agent → LLM → 子Agent
  → ingest() 默认 mode="copy" → 全部拷贝
```

## 修复方案

### 核心思路

在消息文本中注入模式提示，让 LLM 感知操作模式，自动选择正确的 mode 参数调用 ingest 工具。

**为什么不在程序层面传递 mode**：ingest 工具由子Agent（file-processor）调用，调用链是 LLM → 工具选择 → ingest(mode=...)。mode 是 LLM 的决策参数，不是通道参数。程序层面传递 mode 需要：
1. API 增加 resources 字段
2. ChannelRouter 增加元信息传递
3. Agent prompt 注入机制修改
4. 子Agent 提示词修改

改动面大且不自然。而消息文本提示只需改 main.js 一处，利用现有 LLM 推理能力。

### 修改点

**唯一修改：`main.js` 的 `send-to-agent` handler**

当前代码（简化）：
```javascript
ipcMain.on('send-to-agent', (event, context) => {
    const files = context.files;
    const message = `入库文件：${files.join('、')}`;
    // ... fetch('/api/chat', { message })
});
```

修改后：
```javascript
ipcMain.on('send-to-agent', (event, context) => {
    const files = context.files;
    const mode = context.mode || 'copy';
    const modeText = mode === 'reference' ? '（引用模式，不要拷贝文件，创建链接）'
                   : mode === 'move' ? '（移动模式，将文件移动到存储目录）'
                   : '';
    const message = `入库文件${modeText}：${files.join('、')}`;
    // ... fetch('/api/chat', { message })
});
```

**效果对比**：

| 拖入方式 | 修改前消息 | 修改后消息 |
|---------|-----------|-----------|
| 普通拖入 | `入库文件：/path/to/file` | `入库文件：/path/to/file`（不变） |
| Shift+拖入 | `入库文件：/path/to/file` | `入库文件（移动模式，将文件移动到存储目录）：/path/to/file` |
| Ctrl+拖入 | `入库文件：/path/to/file` | `入库文件（引用模式，不要拷贝文件，创建链接）：/path/to/file` |

### 为什么这样就够了

1. **ingest 工具已支持 mode 参数**：`ingest(path, mode="copy")` / `ingest_document(path, mode="move")` / `ingest_photo(path, mode="reference")` 三个工具都已实现 copy/move/reference 三种文件操作
2. **LLM 推理能力**：消息中有明确的模式提示，LLM 会自动选择 mode 参数
3. **file-processor 子Agent**：其提示词已指导 LLM 根据用户意图选择工具和参数
4. **最小改动**：只改 main.js 一处，不影响 API、Channel、Agent 等任何其他层

### 不修改的部分

- **spirit.html**：已正确检测模式，无需修改
- **preload-spirit.js**：已完整传递 context，无需修改
- **chat.html**：对话窗口不区分拖入模式，这是设计决策（用户通过自然语言控制），无需修改
- **后端 API**：无需增加 resources 字段
- **ingest 工具**：已支持 mode 参数，无需修改

## 验证方法

1. Ctrl+拖入一个目录到小女孩 → 消息应包含"引用模式" → ingest 应以 mode="reference" 调用 → 文件应原位引用而非拷贝
2. Shift+拖入一个文件到小女孩 → 消息应包含"移动模式" → ingest 应以 mode="move" 调用 → 文件应被移动而非拷贝
3. 普通拖入 → 消息不变 → ingest 默认 mode="copy" → 行为不变
