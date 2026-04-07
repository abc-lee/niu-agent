# 向量库初始化和定期清理方案（修订版）

## 第一部分：自动初始化

### 设计思路

**错误方案**：在启动代码里写自动初始化逻辑。

**问题**：启动时没有工作目录，需要 Agent 先问用户、拿到工作目录后才能初始化向量库。

**正确方案**：利用现有 firstRun 机制。

### 实施步骤

#### 1. 修改系统手册

**文件**：`docs/SYSTEM_MANUAL.md`

**位置**：首次使用章节

**内容**：
```markdown
## 首次使用流程

当检测到首次启动时（memory.json 中存在 firstRun 字段），执行以下步骤：

### 1. 询问工作目录
"嗨！我是妞妞。为了帮你管理知识，请告诉我你的工作目录想放在哪里？"

### 2. 创建工作目录
用户回答后，使用 bash 工具：
```bash
mkdir -p <用户指定目录>
```

### 3. 写入 memory.json
```bash
cat > ~/.niu/memory.json << 'EOF'
{
  "workspace": {"path": "<用户指定目录>"},
  "user": {"name": "用户", "role": "个人助手用户"}
}
EOF
```
注意：删除 firstRun 字段。

### 4. 初始化向量库
```bash
cd <项目目录>
python scripts/init_vector_db.py
```

等待初始化完成（约 30 秒）。

### 5. 确认完成
"工作目录已设置，向量库初始化完成。现在可以开始对话了！"
```

#### 2. 无需修改代码

- `agent/runner.py` 的 firstRun 注入已实现
- Agent 会自动读取手册并执行
- 无需编写额外的自动初始化程序

---

## 第二部分：向量库清理

### 设计思路

**核心问题**：
1. 不能简单凭时间判断"过期"
2. 需要检查 L1 指针有效性
3. 需要记录记忆热度

### 解决方案

#### 1. 记录热度指标

**修改向量库结构**：

每次搜索匹配到记忆时，更新：
- `access_count` - 访问次数（+1）
- `last_accessed_at` - 最后访问时间
- `last_score` - 最后匹配分数

**实现位置**：`agent/vector_search.py` 的 `search()` 方法

```python
def search(self, query: str, ...):
    # ... 搜索逻辑 ...

    # 更新热度指标
    for doc_id, content, metadata, score in scored_docs[:limit]:
        metadata["access_count"] = metadata.get("access_count", 0) + 1
        metadata["last_accessed_at"] = datetime.now().isoformat()
        metadata["last_score"] = score

        # 更新到数据库
        conn.execute(
            "UPDATE documents SET metadata = ? WHERE id = ?",
            (json.dumps(metadata, ensure_ascii=False), doc_id)
        )
```

#### 2. L1 指针有效性检查

**逻辑**：
1. 查询所有 L1 记录
2. 检查 `l2_pointer` 指向的 L2 是否存在
3. 如果 L2 不存在，删除 L1

**实现**：
```python
def cleanup_invalid_l1_pointers(self):
    """清理 L1 无效指针"""
    conn = sqlite3.connect(self.db_path)
    cursor = conn.cursor()

    # 查询所有 L1 记录
    cursor.execute(
        "SELECT id, metadata FROM documents WHERE json_extract(metadata, '$.level') = 'l1'"
    )
    l1_records = cursor.fetchall()

    deleted = 0
    for l1_id, metadata_json in l1_records:
        metadata = json.loads(metadata_json)
        l2_id = metadata.get("l2_pointer") or metadata.get("pointer")

        if not l2_id:
            continue

        # 检查 L2 是否存在
        cursor.execute("SELECT id FROM documents WHERE id = ?", (l2_id,))
        if not cursor.fetchone():
            # L2 不存在，删除 L1
            cursor.execute("DELETE FROM documents WHERE id = ?", (l1_id,))
            deleted += 1
            logger.info(f"[Cleanup] Deleted L1 with invalid pointer: {l1_id}")

    conn.commit()
    conn.close()
    return deleted
```

#### 3. 热度判断清理

**逻辑**：
- 访问次数低（< 3 次）
- 且长时间未访问（> 60 天）
- 且匹配分数低（< 0.6）

**实现**：
```python
def cleanup_low_value_memories(self, min_access=3, days=60, min_score=0.6):
    """清理低价值记忆"""
    conn = sqlite3.connect(self.db_path)
    cursor = conn.cursor()

    cutoff_date = (datetime.now() - timedelta(days=days)).isoformat()

    # 查找低价值记忆
    cursor.execute(
        """
        SELECT id, metadata FROM documents
        WHERE json_extract(metadata, '$.level') IN ('l0', 'l1', 'l2')
          AND (
              json_extract(metadata, '$.access_count') < ?
              OR json_extract(metadata, '$.access_count') IS NULL
          )
          AND json_extract(metadata, '$.last_accessed_at') < ?
          AND json_extract(metadata, '$.last_score') < ?
        """,
        (min_access, cutoff_date, min_score)
    )

    low_value = cursor.fetchall()

    deleted = 0
    for doc_id, metadata_json in low_value:
        # 删除所有层级（L0/L1/L2 是一组）
        memory_base_id = doc_id.split(":l")[0]
        cursor.execute("DELETE FROM documents WHERE id LIKE ?", (f"{memory_base_id}%",))
        deleted += cursor.rowcount

    conn.commit()
    conn.close()

    logger.info(f"[Cleanup] Deleted {len(low_value)} low-value memories ({deleted} records)")
    return len(low_value)
```

#### 4. 清理时机

**定期清理**：
- 每周执行一次
- 启动时执行一次（如果上次清理 > 7 天前）

**手动触发**：
- `POST /api/vector/cleanup`

---

## 实施计划

### Phase 1: 系统手册更新（0.5 小时）

- 在 `docs/SYSTEM_MANUAL.md` 添加首次使用流程
- 包含向量库初始化指令

### Phase 2: 热度记录（1 小时）

- 修改 `agent/vector_search.py` 的 `search()` 方法
- 每次匹配更新 `access_count`、`last_accessed_at`、`last_score`

### Phase 3: 清理服务（2 小时）

- 创建 `agent/vector_cleanup.py`
- 实现：
  - `cleanup_invalid_l1_pointers()` - L1 指针有效性检查
  - `cleanup_low_value_memories()` - 热度判断清理
  - `cleanup_orphaned_skills()` - 失效 Skills
  - `cleanup_orphaned_mcp_tools()` - 失效 MCP 工具
  - `cleanup_duplicates()` - 去重

### Phase 4: 集成和测试（1 小时）

- 添加 `POST /api/vector/cleanup` 端点
- 添加 `GET /api/vector/stats` 端点（包含热度统计）
- 测试验证

**总计**：4.5 小时

---

## 验证方案

### 热度记录测试

```bash
# 1. 发送消息，触发记忆
curl -X POST http://127.0.0.1:9876/api/chat/session \
  -H "Content-Type: application/json" \
  -d '{"message": "记住我的生日是3月15日"}'

# 2. 发送相关查询
curl -X POST http://127.0.0.1:9876/api/chat/session \
  -H "Content-Type: application/json" \
  -d '{"message": "我生日是哪天？"}'

# 3. 检查热度指标
curl http://127.0.0.1:9876/api/vector/stats | jq '.hot_memories'
```

### 清理测试

```bash
# 触发清理
curl -X POST http://127.0.0.1:9876/api/vector/cleanup

# 检查结果
curl http://127.0.0.1:9876/api/vector/stats
```

---

## 关键差异对比

| 方面 | 原方案 | 修订方案 |
|------|--------|---------|
| 初始化时机 | 启动代码自动执行 | Agent 读手册执行 |
| 过期判断 | 时间（> 90 天） | 热度（访问少 + 时间久） |
| L1 指针检查 | 无 | 检查并清理无效指针 |
| 实施复杂度 | 高 | 低 |
