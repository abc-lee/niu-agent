# File & Photo Server 设计文档

> 版本：v1.2
> 日期：2026-03-27
> 状态：设计阶段

---

## 零、核心原则

### 0.1 工具调用闭环

**所有工具必须有明确的返回结果，形成闭环。**

```
Agent 调用工具
    ↓
工具执行逻辑
    ↓
工具返回结果（成功/失败都要返回）
    ↓
Agent 根据结果决定下一步
```

**返回结果模板**：

```json
// 成功
{
  "status": "success",
  "action": "created",          // created | updated | versioned | renamed
  "file_path": "完整存储路径",
  "note": "简洁说明",
  "details": { ... }            // 可选详情
}

// 失败
{
  "status": "error",
  "error_code": "PERMISSION_DENIED",
  "message": "无法写入目标目录",
  "suggestion": "请检查目录权限"  // 给 Agent 的建议
}
```

### 0.2 角色分工

| 角色 | 职责 |
|------|------|
| 主 Agent | 理解用户意图、读 preferences.json、调用工具、处理返回结果 |
| 工具（server）| 执行具体逻辑、读 memory.json 拼接路径、返回明确结果 |
| 子 Agent | ❌ 不使用 |

### 0.3 配置文件分离

| 文件 | 内容 | 读取者 |
|------|------|--------|
| `~/.niu/memory.json` | 工作目录、用户身份 | 工具读取拼接路径 |
| `~/.niu/preferences.json` | 分类规则、存储结构、冲突处理 | Agent + 工具都读取 |

---

## 一、用户偏好配置

### 1.1 preferences.json 模板

```json
{
  "version": "1.0",
  "storage": {
    "structure": {
      "documents": "{year}/{category}",
      "photos": "{year}/{month}/{date}",
      "notes": "{year}/notes"
    },
    "naming": {
      "documents": "{date}_{title}",
      "photos": "{date}_{persons}"
    },
    "conflict": {
      "document": {
        "similarity_threshold": 0.7,
        "similar_action": "version",
        "different_action": "rename"
      },
      "non_document": {
        "action": "rename"
      },
      "rename_pattern": "{name}_{index}"
    }
  },
  "categories": {
    "documents": ["财务", "合同", "报告", "方案", "其他"],
    "photos": ["生活", "工作", "旅行", "证件", "其他"]
  },
  "photo": {
    "auto_face_recognition": true,
    "person_naming": "ask",
    "merge_threshold": 0.85
  }
}
```

### 1.2 memory.json（工具读取）

```json
{
  "workspace": {
    "path": "E:/Documents/niu"
  },
  "user": {
    "name": "李磊"
  }
}
```

### 1.3 路径拼接逻辑（工具内部）

```python
def get_full_path(relative_structure: str, variables: dict) -> str:
    """
    工具内部拼接完整路径
    1. 读取 ~/.niu/memory.json 获取 workspace.path
    2. 替换变量 {year}, {category} 等
    3. 拼接完整路径
    """
    memory = load_memory()  # ~/.niu/memory.json
    workspace = memory["workspace"]["path"]
    path = relative_structure
    for key, value in variables.items():
        path = path.replace(f"{{{key}}}", value)
    return os.path.join(workspace, path)
```

### 1.4 路径模板变量

| 变量 | 说明 | 示例 |
|------|------|------|
| `{year}` | 年份 | 2026 |
| `{month}` | 月份 | 03 |
| `{date}` | 日期 | 2026-03-27 |
| `{category}` | 分类 | 财务 |
| `{persons}` | 人物 | 张三_李四 |

---

## 二、重名文件冲突处理

### 2.1 处理逻辑（工具内部自动处理）

```
检测同名文件
    │
    ├── 不存在 → 直接存储 → 返回成功
    │
    └── 存在 → 判断文件类型
        │
        ├── 文档类（.pdf, .docx, .txt, .md, .xlsx, .pptx）
        │   │
        │   └── 计算相似度
        │       │
        │       ├── 相似度 > 70% → 版本管理
        │       │   ├── 原文件改名为 xxx_v1_日期.ext
        │       │   └── 新文件存为 xxx.ext
        │       │
        │       └── 相似度 <= 70% → 改名
        │           └── 新文件存为 xxx_1.ext
        │
        └── 非文档类（.jpg, .png, .mp4 等）
            │
            └── 一律改名 → xxx_1.ext
```

### 2.2 返回结果示例

```json
// 版本管理
{
  "status": "success",
  "action": "versioned",
  "file_path": "E:/Documents/niu/2026/财务/报告.pdf",
  "previous_version": "E:/Documents/niu/2026/财务/报告_v1_20260327.pdf",
  "note": "已创建新版本，旧版本已归档"
}

// 改名
{
  "status": "success",
  "action": "renamed",
  "file_path": "E:/Documents/niu/2026/财务/报告_1.pdf",
  "original_name": "报告.pdf",
  "note": "同名文件内容不同，已自动改名"
}

// 失败
{
  "status": "error",
  "error_code": "FILE_CONFLICT_UNRESOLVED",
  "message": "无法处理文件冲突",
  "suggestion": "请手动处理同名文件"
}
```

---

## 三、工具定义

### 3.1 文档入库

**工具名**：`ingest_document`

**输入参数**：
```json
{
  "file_path": "E:/Downloads/报告.pdf",    // 必填：文件绝对路径
  "category": "财务",                       // 分类，从 preferences.json 选取
  "mode": "copy"                            // copy | move | reference
}
```

**输出结果**：
```json
{
  "status": "success",
  "action": "created",
  "file_path": "E:/Documents/niu/2026/财务/报告.pdf",
  "document_id": "doc_20260327_001",
  "note": "文档已入库"
}
```

### 3.2 批量文档入库

**工具名**：`ingest_documents`

**输入参数**：
```json
{
  "file_paths": ["E:/Downloads/报告1.pdf", "E:/Downloads/报告2.pdf"],
  "category": "财务",
  "mode": "copy"
}
```

**输出结果**：
```json
{
  "status": "success",
  "total": 2,
  "processed": 2,
  "results": [
    {"file": "报告1.pdf", "status": "created", "path": "..."},
    {"file": "报告2.pdf", "status": "renamed", "path": "..."}
  ],
  "summary": "已处理 2 个文档"
}
```

### 3.3 照片入库

**工具名**：`ingest_photo`

**输入参数**：
```json
{
  "file_path": "E:/Photos/IMG_001.jpg",
  "category": "生活",
  "mode": "copy"
}
```

**输出结果**：
```json
{
  "status": "success",
  "photo_id": "photo_20260327_001",
  "file_path": "E:/Documents/niu/2026/03/2026-03-27_IMG_001.jpg",
  "exif": {
    "taken_at": "2026-03-27 14:30:00",
    "location": "北京市朝阳区",
    "camera": "iPhone 15 Pro"
  },
  "persons": [
    {"id": "person_001", "name": "张三", "confidence": 0.95},
    {"id": "person_new_001", "name": "未命名人物_01", "is_new": true}
  ],
  "note": "照片已入库，识别出 2 人（1 新人物）"
}
```

### 3.4 批量照片入库

**工具名**：`ingest_photos`

**输入参数**：
```json
{
  "file_paths": ["E:/Photos/IMG_001.jpg", "E:/Photos/IMG_002.jpg"],
  "mode": "copy"
}
```

**输出结果**：
```json
{
  "status": "success",
  "total": 2,
  "processed": 2,
  "new_persons": 3,
  "summary": "已处理 2 张照片，识别出 5 人（3 新人物）"
}
```

### 3.5 人物命名

**工具名**：`name_person`

**输入参数**：
```json
{
  "person_id": "person_new_001",
  "name": "李四"
}
```

**输出结果**：
```json
{
  "status": "success",
  "person_id": "person_new_001",
  "name": "李四",
  "photo_count": 5,
  "note": "人物已命名"
}
```

### 3.6 人物合并

**工具名**：`merge_persons`

**输入参数**：
```json
{
  "person_id_1": "person_001",
  "person_id_2": "person_new_001",
  "keep_name": "person_001"
}
```

**输出结果**：
```json
{
  "status": "success",
  "merged_person_id": "person_001",
  "name": "张三",
  "photo_count": 12,
  "note": "人物已合并"
}
```

### 3.7 搜索人物

**工具名**：`search_persons`

**输入参数**：
```json
{
  "query": "张三",
  "limit": 10
}
```

**输出结果**：
```json
{
  "status": "success",
  "persons": [
    {"id": "person_001", "name": "张三", "photo_count": 15, "last_seen": "2026-03-27"},
    {"id": "person_002", "name": "张三丰", "photo_count": 3, "last_seen": "2026-01-15"}
  ]
}
```

### 3.8 搜索照片

**工具名**：`search_photos`

**输入参数**：
```json
{
  "query": "张三和李四的合影",
  "filters": {
    "date_from": "2026-01-01",
    "date_to": "2026-03-27"
  },
  "limit": 20
}
```

**输出结果**：
```json
{
  "status": "success",
  "photos": [
    {
      "id": "photo_20260327_001",
      "file_path": "E:/Documents/niu/2026/03/2026-03-27_IMG_001.jpg",
      "taken_at": "2026-03-27 14:30:00",
      "persons": ["张三", "李四"]
    }
  ],
  "total": 5
}
```

---

## 四、数据结构

### 4.1 persons 表

```sql
CREATE TABLE persons (
    id TEXT PRIMARY KEY,
    name TEXT,
    auto_label TEXT,              -- 未命名人物_N
    center_embedding BLOB,        -- 中心向量（128维）
    threshold_adjustment REAL,    -- 学习调整值
    photo_count INTEGER,
    first_seen TEXT,
    last_seen TEXT,
    created_at TEXT
);
```

### 4.2 photos 表

```sql
CREATE TABLE photos (
    id TEXT PRIMARY KEY,
    file_path TEXT,
    original_path TEXT,
    taken_at TEXT,
    location TEXT,
    camera TEXT,
    abstract TEXT,                -- L0 摘要
    overview TEXT,                -- L1 概览
    vector_id TEXT,
    ingested_at TEXT
);
```

### 4.3 faces 表

```sql
CREATE TABLE faces (
    id TEXT PRIMARY KEY,
    photo_id TEXT,
    person_id TEXT,
    embedding BLOB,
    bounding_box TEXT,
    confidence REAL,
    FOREIGN KEY (photo_id) REFERENCES photos(id),
    FOREIGN KEY (person_id) REFERENCES persons(id)
);
```

### 4.4 documents 表

```sql
CREATE TABLE documents (
    id TEXT PRIMARY KEY,
    file_path TEXT,
    original_path TEXT,
    category TEXT,
    title TEXT,
    abstract TEXT,                -- L0 摘要
    overview TEXT,                -- L1 概览
    vector_id TEXT,
    ingested_at TEXT
);
```

---

## 五、InsightFace 集成

### 5.1 按需加载

```python
class FaceRecognitionService:
    _model = None
    
    @classmethod
    def get_model(cls):
        if cls._model is None:
            from insightface.app import FaceAnalysis
            cls._model = FaceAnalysis(name='buffalo_l', providers=['CPUExecutionProvider'])
            cls._model.prepare(ctx_id=-1)
        return cls._model
    
    @classmethod
    def unload_model(cls):
        cls._model = None
        import gc
        gc.collect()
```

### 5.2 模型打包

- 位置：`mcp-servers/photo-server/models/buffalo_l/`
- 大小：~200MB
- 首次运行从本地加载

---

## 六、与现有服务集成

| 服务 | 集成方式 |
|------|----------|
| kg-server | 人物用 Entity 存储，同框关系用 CO_OCCURS_WITH |
| vector-store | 文档/照片 L0 向量化存储 |
| file-parser | 解析文档内容 |

---

## 七、边缘情况处理

### 7.1 批量处理反馈原则

**核心原则：中间不反馈，全部完成后一次性返回。**

原因：中间反馈可能打断 Agent 流程，Agent 可能误以为任务结束。

```json
// 批量处理最终返回格式
{
  "status": "success",
  "total": 10,
  "processed": 8,
  "failed": 2,
  "results": [
    {"file": "报告1.pdf", "status": "created", "path": "..."},
    {"file": "报告2.pdf", "status": "renamed", "path": "..."},
    {"file": "损坏文件.pdf", "status": "skipped", "reason": "文件损坏无法读取"},
    {"file": "权限问题.pdf", "status": "failed", "reason": "无读取权限"}
  ],
  "summary": "已处理 8/10 文件，2 个失败（已回退）"
}
```

### 7.2 文件相似度计算

**两步判断：先哈希，再内容相似度。**

```python
def handle_document_conflict(existing_path: str, new_path: str) -> str:
    """处理文档类文件冲突"""
    
    # 1. 快速判断：比较哈希
    hash1 = calculate_file_hash(existing_path)
    hash2 = calculate_file_hash(new_path)
    
    if hash1 == hash2:
        # 完全相同的文件 → 版本管理
        return "version"
    
    # 2. 内容相似度判断
    similarity = calculate_content_similarity(existing_path, new_path)
    
    if similarity > 0.7:
        # 内容相似 → 版本管理
        return "version"
    else:
        # 内容不同 → 改名
        return "rename"


def calculate_file_hash(file_path: str) -> str:
    """计算文件哈希，快速判断是否完全相同"""
    import hashlib
    sha256 = hashlib.sha256()
    with open(file_path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            sha256.update(chunk)
    return sha256.hexdigest()


def calculate_content_similarity(file1: str, file2: str) -> float:
    """计算文档内容相似度（TF-IDF + 余弦）"""
    # 1. 用 file-parser 解析文档内容
    text1 = parse_document(file1)
    text2 = parse_document(file2)
    
    # 2. TF-IDF 向量化 + 余弦相似度
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    
    vectorizer = TfidfVectorizer()
    tfidf = vectorizer.fit_transform([text1, text2])
    return cosine_similarity(tfidf[0:1], tfidf[1:2])[0][0]
```

| 场景 | 判断方式 | 处理 |
|------|----------|------|
| 哈希相同 | 快速判断 | 版本管理 |
| 哈希不同 + 相似度 > 70% | 内容相似 | 版本管理 |
| 哈希不同 + 相似度 <= 70% | 内容不同 | 改名 |

### 7.3 EXIF 缺失处理

```python
def get_photo_date(file_path: str) -> str:
    """获取照片日期，优先级：EXIF > 文件修改时间 > 文件名"""
    
    # 1. 尝试 EXIF
    exif_date = extract_exif_date(file_path)
    if exif_date:
        return exif_date
    
    # 2. 使用文件修改时间
    mtime = os.path.getmtime(file_path)
    return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
```

### 7.4 损坏/无法处理文件

| 问题 | 处理 | 返回 |
|------|------|------|
| 文件损坏 | 跳过，不入库 | `status: "skipped", reason: "文件损坏"` |
| 无读取权限 | 跳过，不入库 | `status: "failed", reason: "无读取权限"` |
| 不支持的格式 | 跳过，不入库 | `status: "skipped", reason: "不支持的格式"` |
| 磁盘空间不足 | 停止处理，回退已处理文件 | `status: "error", error_code: "DISK_FULL"` |

### 7.5 事务回滚

**原则：批量处理失败时，回退已处理的文件，保持一致性。**

```python
def ingest_files(file_paths: list, mode: str) -> dict:
    processed = []
    try:
        for file_path in file_paths:
            result = process_file(file_path, mode)
            if result['status'] == 'success':
                processed.append(result['file_path'])
            # 失败继续处理下一个，最后统一报告
    except DiskFullError:
        # 磁盘满，回退所有已处理文件
        rollback(processed, mode)
        return {"status": "error", "error_code": "DISK_FULL", "rolled_back": len(processed)}
    
    return build_final_result(processed)
```

### 7.6 与 kg-server 集成失败

```python
# 文件入库成功，但知识图谱创建失败 → 回滚文件
def ingest_photo(file_path: str) -> dict:
    # 1. 文件存储
    stored_path = store_file(file_path)
    
    # 2. 创建知识图谱实体
    try:
        kg_result = kg_server.create_entity(...)
    except Exception as e:
        # 回滚文件
        os.remove(stored_path)
        return {"status": "error", "error_code": "KG_ERROR", "message": str(e)}
    
    return {"status": "success", "file_path": stored_path}
```

### 7.7 人脸检测特殊情况

| 场景 | 处理 |
|------|------|
| 检测到 0 张人脸 | 正常入库，persons 为空数组 |
| 检测到多张人脸 | 全部返回，逐个匹配 |
| 人脸模糊无法识别 | 降低置信度，依然尝试匹配 |

---

## 八、错误码定义

### 8.1 通用错误码

| 错误码 | 说明 | 建议 |
|--------|------|------|
| `FILE_NOT_FOUND` | 文件不存在 | 检查文件路径 |
| `PERMISSION_DENIED` | 无读取/写入权限 | 检查文件权限 |
| `FILE_CORRUPTED` | 文件损坏 | 无法处理，跳过 |
| `UNSUPPORTED_FORMAT` | 不支持的文件格式 | 检查文件类型 |
| `DISK_FULL` | 磁盘空间不足 | 清理磁盘空间 |
| `KG_ERROR` | 知识图谱操作失败 | 检查 kg-server 状态 |
| `VECTOR_ERROR` | 向量化失败 | 检查 vector-store 状态 |

### 8.2 照片特有错误码

| 错误码 | 说明 | 建议 |
|--------|------|------|
| `EXIF_READ_ERROR` | EXIF 读取失败 | 使用文件修改时间 |
| `FACE_DETECTION_ERROR` | 人脸检测失败 | 照片依然入库，无人脸信息 |

### 8.3 人物特有错误码

| 错误码 | 说明 | 建议 |
|--------|------|------|
| `PERSON_NOT_FOUND` | 人物不存在 | 检查 person_id |
| `PERSON_ALREADY_EXISTS` | 人物已存在 | 使用 merge_persons 合并 |

---

*文档结束*
