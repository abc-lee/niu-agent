# 统一入库工具设计

## 目标

将 photo-server 的 4 个入库工具（ingest_photo、ingest_photos、ingest_document、ingest_documents）合并为 1 个 `ingest` 工具，自动判断路径类型和内容类型，与子 Agent 形成 L0 生成循环。

## 当前问题

| 问题 | 说明 |
|------|------|
| 4 个工具职责重叠 | ingest_document 传入目录会转调 ingest_photos_batch，ingest_photos 是调度器 |
| LLM 需要判断调哪个 | 子 Agent 必须先判断文件类型再选工具，判断错误就混乱 |
| batch 不做智能处理 | ingest_photos_batch 只拷贝，不做人脸识别、EXIF、KG 同步 |
| L0 循环分散 | ingest_document 返回 need_l1，LLM 再调 store_document_l1，两步分离 |

## 新设计：单一 `ingest` 工具

### 调用接口

```python
ingest(path: str, mode: str = "copy", category: str | None = None) -> dict
```

- `path`: 文件路径或目录路径
- `mode`: "copy" | "move" | "reference"（默认 copy）
- `category`: 分类，不传则自动推断

### 自动判断逻辑

工具内部按以下顺序判断：

1. **路径类型**：`is_dir()` → 目录模式，`is_file()` → 文件模式
2. **内容类型**（文件模式）：按扩展名判断
   - 照片扩展名（.jpg/.png/.heic 等）→ 照片流程
   - 其他 → 文档流程
3. **内容类型**（目录模式）：扫描目录内容
   - 全是照片 → 照片批量流程
   - 全是文档 → 文档批量流程
   - 混合 → 分类后分别处理

### 处理流程

#### 照片（单张）

1. 提取 EXIF
2. 拷贝/移动/引用到存储目录
3. 人脸检测 → 人物匹配/创建 → 写 photos/faces/co_occurrences 表
4. 生成 L0 摘要（程序内生成，不需要 LLM）
5. KG 同步
6. 返回结果

#### 照片（批量）

逐张执行照片单张流程，汇总结果返回。

#### 文档（单个）

1. 拷贝/移动/引用到存储目录
2. 返回 `status: "need_l1"` + 文件内容/指针
3. 子 Agent 生成 L1 摘要，调用 `ingest(l1=...)` 送回
4. 工具存储 L1/L2 到向量库
5. KG 同步
6. 返回结果

#### 文档（批量）

逐个执行文档流程，遇到 need_l1 时暂停返回，子 Agent 送回 L1 后继续。

### L1 循环机制

```
子 Agent 调用 ingest(path="/xxx/report.pdf")
  → 工具拷贝文件，返回 {status: "need_l1", file_path: "...", content: "文件内容..."}

子 Agent 生成 L1，调用 ingest(file_path="...", l1="标题|关键词|摘要|实体|类型|指针")
  → 工具存储 L1/L2 到向量库，KG 同步，返回 {status: "success", ...}
```

照片不需要 L1 循环（L0 由程序内生成），只有文档需要。

### 开发策略

1. 在 `scripts/` 下独立开发 `ingest_unified.py`，可独立运行测试
2. 测试通过后，替换 photo-server 中的 4 个工具
3. 删除旧工具的 TOOL_SCHEMAS 和函数（保留内部函数供新工具调用）

### 测试用例

1. 单张照片 → EXIF + 人脸 + KG + 向量库
2. 照片目录 → 逐张完整处理
3. 单个文档 → 拷贝 + need_l1 → 送回 L1 → 向量库 + KG
4. 混合目录 → 照片走照片流程，文档走文档流程
5. 重复照片 → 跳过（基于文件哈希去重，待实现）
6. 不存在的路径 → 返回错误

### 替换后的工具清单

| 旧工具 | 新工具 | 状态 |
|--------|--------|------|
| ingest_photo | ingest | 删除 |
| ingest_photos | ingest | 删除 |
| ingest_document | ingest | 删除 |
| ingest_documents | ingest | 删除 |
| store_document_l1 | ingest（L1 循环内） | 删除 |
| store_documents_l1 | ingest（L1 循环内） | 删除 |
| name_person | name_person | 保留 |
| merge_persons | merge_persons | 保留 |
| search_persons | search_persons | 保留 |
| get_unnamed_persons | get_unnamed_persons | 保留 |
| delete_person | delete_person | 保留 |
| cleanup_deleted_photos | cleanup_deleted_photos | 保留 |
| get_person_photos | get_person_photos | 保留 |
| unload_face_model | unload_face_model | 保留 |
