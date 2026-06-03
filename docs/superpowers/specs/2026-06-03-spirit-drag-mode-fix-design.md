# Spirit 拖入文件操作模式传递修复设计

## 问题

Spirit 小女孩拖入文件时，前端已检测修饰键区分操作模式（copy/move/reference），但 mode 信息传递不完整，导致所有拖入一律按拷贝处理。

## 当前链路

```
spirit.html drop事件 (line ~596)
  → 检测 e.shiftKey/e.ctrlKey → 确定 mode
  → 构造 context = {files: [...], mode: "reference"}
  → 消息文本已有模式："入库文件（reference模式）：/path"  ⚠️ 措辞是编程术语
  → window.electronAPI.sendToAgent(context)

preload-spirit.js
  → ipcRenderer.send('send-to-agent', context)

main.js send-to-agent handler (line 708)
  → const data = JSON.stringify({ message: message })  ❌ context 被丢弃
  → fetch('/api/chat', { body: data })

后端 → Agent → LLM → 子Agent
  → ingest() 默认 mode="copy" → 全部拷贝
```

## 问题分析

### 问题1：main.js 丢弃了 context

`main.js:708` 只取了 `message` 字段构造请求体，`context.files` 和 `context.mode` 被完全丢弃。但消息文本本身（由 spirit.html 构造）已经包含了模式描述，所以 context 丢失不会导致 mode 信息完全丢失。

### 问题2：模式措辞不够明确

当前 spirit.html 生成的消息如 `入库文件（reference模式）：/path`，"reference模式" 是编程术语，LLM 可能不够敏感，不会主动映射到 ingest 工具的 `mode="reference"` 参数。

### 问题3：多文件/目录场景

拖入目录时，file-parser 会遍历目录内所有文件，逐个调用 ingest。LLM 需要对每个文件都传递 mode 参数，单靠消息文本提示可能不够可靠。

## 修复方案

### 方案：改 spirit.html 的模式提示文本 + main.js 传递 context

**改动1：spirit.html — 增强模式提示措辞**

将编程术语改为 LLM 更容易理解的语义描述：

```javascript
// 修改前
const modeText = mode !== 'copy' ? `（${mode}模式）` : '';

// 修改后
const modeText = mode === 'reference' ? '（引用模式，使用原路径引用文件，不要拷贝）'
               : mode === 'move' ? '（移动模式，将文件移动到存储目录）'
               : '';
```

效果对比：

| 拖入方式 | 修改前 | 修改后 |
|---------|--------|--------|
| 普通拖入 | `入库文件：/path` | `入库文件：/path`（不变） |
| Shift+拖入 | `入库文件（move模式）：/path` | `入库文件（移动模式，将文件移动到存储目录）：/path` |
| Ctrl+拖入 | `入库文件（reference模式）：/path` | `入库文件（引用模式，使用原路径引用文件，不要拷贝）：/path` |

**改动2：main.js — 将 context.files 作为结构化数据传递**

当前 main.js 只传 message 字符串。增加 `resources` 字段，让 mode 以结构化方式传递：

```javascript
// 修改前 (line 708)
const data = JSON.stringify({ message: message });

// 修改后
const resources = context.files.map(f => ({
    path: f,
    mode: context.mode || 'copy'
}));
const data = JSON.stringify({ message: message, resources: resources });
```

### 为什么需要两个改动

- **改动1**（spirit.html）：确保消息文本中的模式提示足够明确，LLM 能正确推断 mode 参数
- **改动2**（main.js）：提供结构化的 resources 数据，作为可靠 fallback。即使 LLM 忽略了消息文本提示，后端也可以从 resources 字段获取 mode

### 后端处理

检查后端 API 的 chat 请求体是否已有 `resources` 字段。如果有，直接利用；如果没有，需要增加。读取 `niu_api/api.py` 确认。

### 不修改的部分

- **preload-spirit.js**：已完整传递 context，无需修改
- **chat.html**：对话窗口不区分拖入模式，这是设计决策（用户通过自然语言控制），无需修改
- **ingest 工具**：已支持 mode 参数，无需修改

## 验证方法

1. Ctrl+拖入一个目录到小女孩 → 消息应包含"引用模式" + resources 中 mode="reference" → ingest 应以 mode="reference" 调用 → 文件应原位引用
2. Shift+拖入一个文件到小女孩 → 消息应包含"移动模式" + resources 中 mode="move" → ingest 应以 mode="move" 调用 → 文件应被移动
3. 普通拖入 → 消息不变 → resources 中 mode="copy" → 行为不变
