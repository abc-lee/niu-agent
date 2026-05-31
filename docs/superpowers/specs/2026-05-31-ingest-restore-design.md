# 入库功能恢复 + MCP Sampling 改造设计

## 设计原则

**程序尽可能自己处理，处理不了才问Agent。**

用户拖入路径后，程序负责所有自动判断和处理。只在"文档需要分类且用户未指定"时，通过 `ask_agent` callback 请求 Agent 判断。照片部分永远不需要问Agent。

## 架构改造：ToolRegistry 注入 ask_agent callback

### 问题

当前同进程 ToolRegistry 架构下，photo-server 无法在工具调用过程中向 Agent 请求 LLM 推理。MCP Sampling 需要 session，但同进程模式没有 session。

### 方案：在 ToolRegistry 中注入 ask_agent callback

photo-server 已经通过 `get_registry()` 调用 lightrag-server 的工具。用同样的路径注入一个 `ask_agent` callback，让 photo-server 需要分类判断时直接调用。

```
┌─────────────┐     注册时注入      ┌─────────────┐
│   runner.py  │ ─────────────────→ │ ToolRegistry │
│              │   set_ask_agent()  │              │
└─────────────┘                     │  _ask_agent  │ ← photo-server 调用
                                    └─────────────┘
```

### 改动1：ToolRegistry 新增 ask_agent

`agent/tool_registry.py`：

```python
class ToolRegistry:
    def __init__(self):
        ...
        self._ask_agent = None  # callable(prompt: str, system_prompt: str = "", max_tokens: int = 500) -> str

    def set_ask_agent(self, fn):
        """注入 Agent LLM 回调函数，供 MCP Server 调用"""
        self._ask_agent = fn

    def ask_agent(self, prompt: str, system_prompt: str = "", max_tokens: int = 500) -> str | None:
        """请求 Agent LLM 生成回答。返回文本或 None（如果不可用）"""
        if self._ask_agent is None:
            return None
        return self._ask_agent(prompt=prompt, system_prompt=system_prompt, max_tokens=max_tokens)
```

### 改动2：runner.py 注入 ask_agent 实现

`agent/runner.py` 在初始化 ToolRegistry 后注入：

```python
def _make_ask_agent_callback(self):
    """创建 ask_agent 回调，调用当前 Agent 的 LLM"""
    def ask_agent(prompt: str, system_prompt: str = "", max_tokens: int = 500) -> str | None:
        try:
            from litellm import completion
            config = load_llm_config()
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})
            response = completion(
                model=config["model"],
                messages=messages,
                max_tokens=max_tokens,
                temperature=0.2,
                api_key=config.get("api_key"),
                api_base=config.get("api_base"),
            )
            return response.choices[0].message.content
        except Exception as e:
            logger.error(f"ask_agent failed: {e}")
            return None
    return ask_agent

# 在 __init__ 或初始化阶段
registry = get_registry()
registry.set_ask_agent(self._make_ask_agent_callback())
```

### 改动3：photo-server 使用 ask_agent

`mcp-servers/photo-server/src/niu_photo_server/__init__.py`：

文档入库需要分类时：
```python
def _ask_agent_for_category(self, preview: str, available_categories: list[str]) -> str | None:
    """请求 Agent 判断文档分类"""
    from agent.tool_registry import get_registry
    registry = get_registry()
    prompt = f"""请根据以下文档内容，从可选分类中选择最合适的一个分类。

文档内容预览：
{preview}

可选分类：{', '.join(available_categories)}

只回答分类名称，不要解释。"""
    result = registry.ask_agent(prompt=prompt, system_prompt="你是一个文档分类助手。", max_tokens=100)
    if result and result.strip() in available_categories:
        return result.strip()
    return None
```

这样 photo-server 在入库过程中可以：
1. 读取文件内容（≤20K）
2. 调用 `ask_agent` 让 Agent 判断分类
3. Agent 返回分类后继续入库
4. 全部在一个工具调用内完成

**如果 ask_agent 不可用**（返回 None），回退到现有的 `need_category` 模式——返回内容预览让子Agent多次调用。

## 完整入库流程

```
ingest(path, mode, category)
    │
    ├── classify_path(path)
    │   ├── FILE + PHOTO     → ingest_photo()
    │   ├── FILE + DOCUMENT → ingest_document()
    │   ├── DIR + PHOTO     → ingest_photo_directory()  ← 程序内部循环，一次返回
    │   ├── DIR + DOCUMENT  → ingest_document_directory()
    │   ├── DIR + MIXED     → ingest_mixed_directory()
    │   └── EMPTY            → error
    │
    ├── 照片部分（永远不问Agent）
    │   └── 逐张 ingest_photo()：EXIF → 人脸 → DB → KG
    │
    └── 文档部分
        ├── category 已指定 → 逐个直接入库，不问
        ├── category 未指定 + ask_agent 可用 → 程序内部逐个调 ask_agent 判断分类，一次返回全部结果
        └── category 未指定 + ask_agent 不可用 → 回退 need_category 模式（多轮工具调用）
```

## 入库功能恢复改动

### 改动4：新增 classify_path()

```python
class ContentType(Enum):
    PHOTO = "photo"
    DOCUMENT = "document"
    MIXED = "mixed"
    EMPTY = "empty"

def classify_path(path: str) -> ContentType:
    source = Path(path)
    if not source.exists():
        return ContentType.EMPTY
    if source.is_file():
        return ContentType.PHOTO if source.suffix.lower() in PHOTO_EXTENSIONS else ContentType.DOCUMENT
    has_photo = any(f.is_file() and f.suffix.lower() in PHOTO_EXTENSIONS for f in source.rglob("*"))
    has_doc = any(f.is_file() and f.suffix.lower() in DOCUMENT_EXTENSIONS and f.suffix.lower() not in PHOTO_EXTENSIONS for f in source.rglob("*"))
    if has_photo and has_doc:
        return ContentType.MIXED
    if has_photo:
        return ContentType.PHOTO
    if has_doc:
        return ContentType.DOCUMENT
    return ContentType.EMPTY
```

### 改动5：ingest_photo() 加 mode + 修复回滚

**签名变更**：
```python
def ingest_photo(file_path: str, mode: str = "copy", category: str | None = None) -> dict:
```

**文件操作三分支**：
```python
if mode == "copy":
    shutil.copy2(str(source), final_path)
elif mode == "move":
    shutil.move(str(source), final_path)
elif mode == "reference":
    final_path = str(source)
```

**错误回滚修复**：
```python
except Exception as e:
    if final_path is not None:
        try:
            if mode == "move":
                shutil.move(str(final_path), str(source))
            elif mode != "reference":
                if os.path.exists(final_path):
                    os.remove(final_path)
        except OSError:
            pass
```

### 改动6：批量照片改为逐张完整处理

新增 `ingest_photo_directory()`，程序内部循环逐张调用 `ingest_photo()`：

```python
def ingest_photo_directory(source_path: str, mode: str = "copy", category: str | None = None) -> dict:
    """照片目录入库：程序内部逐张走完整流程，一次返回全部结果。"""
    source = Path(source_path)
    photo_files = sorted([f for f in source.rglob("*") if f.is_file() and f.suffix.lower() in PHOTO_EXTENSIONS])

    results = []
    errors = []
    for pf in photo_files:
        result = ingest_photo(str(pf), mode=mode, category=category)
        if result.get("status") == "success":
            results.append(result)
        else:
            errors.append({"file": str(pf), "error": result.get("message", "unknown")})

    return {
        "status": "success",
        "total": len(photo_files),
        "succeeded": len(results),
        "failed": len(errors),
        "errors": errors[:10],
        "photos": results,
    }
```

### 改动7：新增文档目录入库

程序内部循环逐个调用 `ingest_document()`，需要分类时用 `ask_agent`：

```python
def ingest_document_directory(source_path: str, mode: str = "copy", category: str | None = None) -> dict:
    """文档目录入库：程序逐个处理，需要分类时用 ask_agent 或回退 need_category。"""
    source = Path(source_path)
    doc_files = sorted([f for f in source.rglob("*")
                       if f.is_file() and f.suffix.lower() in DOCUMENT_EXTENSIONS
                       and f.suffix.lower() not in PHOTO_EXTENSIONS])

    results = []
    errors = []
    unsupported = []

    for df in doc_files:
        file_category = category
        # 未指定分类时，尝试用 ask_agent 自动判断
        if not file_category:
            result_needs_cat = ingest_document(str(df), mode=mode, category="")
            if result_needs_cat.get("status") == "need_category":
                agent_category = _ask_agent_for_category(
                    result_needs_cat.get("message", ""),
                    result_needs_cat.get("available_categories", [])
                )
                if agent_category:
                    file_category = agent_category
                else:
                    # ask_agent 不可用，记录待分类文件
                    unsupported.append(result_needs_cat)
                    continue

        result = ingest_document(str(df), mode=mode, category=file_category or "")
        if result.get("status") == "success":
            results.append(result)
        elif result.get("status") == "need_category":
            unsupported.append(result)
        else:
            errors.append({"file": str(df), "error": result.get("message", "unknown")})

    status = "success" if not unsupported else "partial"
    return {
        "status": status,
        "total": len(doc_files),
        "succeeded": len(results),
        "failed": len(errors),
        "need_category": unsupported,
        "errors": errors[:10],
        "documents": results,
    }
```

### 改动8：新增混合目录入库

```python
def ingest_mixed_directory(source_path: str, mode: str = "copy", category: str | None = None) -> dict:
    """混合目录入库：照片程序处理完，文档按需问Agent。"""
    photo_result = ingest_photo_directory(source_path, mode=mode, category=category)
    doc_result = ingest_document_directory(source_path, mode=mode, category=category)

    return {
        "status": doc_result.get("status", "success"),
        "photos": photo_result,
        "documents": doc_result,
    }
```

### 改动9：重写 ingest 工具路由

`call_tool` 中 `name == "ingest"` 分支：

```python
if name == "ingest":
    path = arguments["path"]
    mode = arguments.get("mode", "copy")
    category = arguments.get("category", "") or None

    content_type = classify_path(path)

    if content_type == ContentType.EMPTY:
        return {"status": "error", "message": f"路径不存在或目录为空: {path}"}

    source = Path(path)
    if source.is_file():
        if content_type == ContentType.PHOTO:
            return ingest_photo(path, mode=mode, category=category)
        else:
            return ingest_document(path, mode=mode, category=category)

    if content_type == ContentType.PHOTO:
        return ingest_photo_directory(path, mode=mode, category=category)
    elif content_type == ContentType.DOCUMENT:
        return ingest_document_directory(path, mode=mode, category=category)
    else:
        return ingest_mixed_directory(path, mode=mode, category=category)
```

### 改动10：更新 TOOL_SCHEMAS 和 file-processor.md

- `ingest` schema description 更新
- `ingest_photo` schema 加 mode 参数
- `ingest_photos` schema 加 mode 参数
- `file-processor.md` 更新交互流程说明

## 不改动

- `ingest_document()` 单文件逻辑：保持不变
- `ingest_documents()` 批量函数：保持现有签名
- 底层辅助函数：全部保持不变
- 数据库表结构：无变化
- 分类目录来源：仍从 `preferences.json` 读取

## 文件清单

| 文件 | 改动 |
|------|------|
| `agent/tool_registry.py` | 新增 ask_agent 属性和 getter/setter（~15行） |
| `agent/runner.py` | 注入 ask_agent callback（~25行） |
| `mcp-servers/photo-server/src/niu_photo_server/__init__.py` | classify_path + 目录入库函数 + ask_agent 分类 + ingest 路由重写 + ingest_photo 加 mode |
| `config/agents/file-processor.md` | 提示词更新 |

## 实施顺序

1. **P0: ToolRegistry ask_agent 注入** — 改动最小，独立可测
2. **P1: photo-server classify_path + ingest 路由重写** — 目录入库通路
3. **P1: ingest_photo 加 mode + 回滚修复** — 照片 move/reference 恢复
4. **P2: ingest_photo_directory** — 批量照片完整处理
5. **P2: ingest_document_directory + ask_agent 分类** — 文档目录入库
6. **P3: ingest_mixed_directory** — 混合目录
7. **P3: Schema + 提示词更新**

## 测试计划

启动应用后做真实测试，6种场景 x 3种模式 = 18个测试：

| 场景 | copy | move | reference |
|------|------|------|-----------|
| 单张照片 | ✓ | ✓ | ✓ |
| 单个文档 | ✓ | ✓ | ✓ |
| 纯照片目录 | ✓ | ✓ | ✓ |
| 纯文档目录 | ✓ | ✓ | ✓ |
| 混合目录 | ✓ | ✓ | ✓ |
| 空目录 | 报错 | 报错 | 报错 |

每个测试验证：
- 文件存储位置正确
- 数据库记录写入（照片）
- 人脸检测/匹配正常（照片）
- 知识图谱写入正常（文档）
- move 模式下源文件已删除
- reference 模式下不复制文件
- move 模式错误时文件正确回滚
- ask_agent 分类判断准确