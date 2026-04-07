# 向量库初始化和定期清理方案

## 问题背景

### 当前状况

1. **初始化机制**
   - 只有手动脚本 `scripts/init_vector_db.py`
   - 没有自动初始化，用户首次启动时向量库不存在会导致功能缺失
   - 需要手动运行 `python scripts/init_vector_db.py`

2. **Skills 同步**
   - 已有实时监听机制（`agent/injector/sync.py` 使用 watchdog）
   - 文件变化时自动同步到向量库
   - 但只能增量更新，无法清理已删除文件的残留数据

3. **定期清理**
   - 向量库没有定期清理机制
   - `memory-server` 有 `cleanup_memories()` 方法，但主向量库（`vectors.db`）不在此范围
   - 可能存在的问题：
     - 失效的 Skills（文件已删除，向量库仍有记录）
     - 失效的 MCP 工具描述（服务器已移除，向量库仍有记录）
     - 过期的记忆（时间久远不再相关）
     - 重复内容（同一 ID 多条记录）

### 数据量分析

向量库存储的内容类型：

| 类型 | Level | 更新频率 | 失效风险 | 占比估计 |
|------|-------|---------|---------|---------|
| Skills | L1 | 低（手动编辑） | 中（文件删除） | 5% |
| MCP 工具描述 | L1 | 低（服务变更） | 中（服务移除） | 3% |
| 系统说明书 | L1 | 低（版本更新） | 低 | 2% |
| L0 记忆 | L0 | 高（每次对话） | 中（遗忘） | 10% |
| L1 摘要 | L1 | 中（总结时） | 低 | 15% |
| L2 原文 | L2 | 低（记忆时） | 低 | 65% |

**预计增长**：
- 每次对话：+1 条 L0 记忆（~500 tokens）
- 每周总结：+10 条 L1 摘要（~2000 tokens）
- 每月存储：+100 条 L2 原文（~20000 tokens）

**一年后预估**：~10000 条记录，~50MB 数据库大小

---

## 方案设计

### 第一部分：自动初始化

#### 1.1 启动时检测

**位置**：`niu_api/__main__.py` 的 `lifespan()` 函数

**逻辑**：
```python
# 2. Preload embedding model（已存在）
from niu_api.internal.embedding import preload as preload_embedding
logger.info("Preloading embedding model...")
preload_embedding()

# 新增：检测并初始化向量库
from agent.vector_search import get_vector_search
vs = get_vector_search()
if vs._get_connection() is None:
    logger.info("Vector database not found, initializing...")
    from scripts.init_vector_db import init_vector_db, sync_skills, register_mcp_tools
    db_path = vs.db_path
    init_vector_db(db_path)
    sync_skills()
    register_mcp_tools()
    logger.info("Vector database initialized successfully")
else:
    logger.info("Vector database found, skipping initialization")
```

**优点**：
- 用户无需手动运行脚本
- 首次启动自动完成初始化
- 后续启动检测到已存在则跳过

**风险**：
- 初始化耗时较长（~30秒，主要是 embedding）
- 需要在 Go 启动器的 preload 等待时间内完成

#### 1.2 API 端点

**新增端点**：`POST /api/vector/init`

**用途**：
- 手动触发重新初始化
- 修复向量库损坏
- 更新 MCP 工具描述

**实现**：
```python
# niu_api/compat.py

@router.post("/api/vector/init")
async def init_vector_db():
    """重新初始化向量库"""
    from scripts.init_vector_db import init_vector_db, sync_skills, register_mcp_tools
    from agent.vector_search import get_vector_search

    try:
        vs = get_vector_search()
        init_vector_db(vs.db_path)
        sync_skills()
        register_mcp_tools()
        return {"status": "success", "message": "Vector database initialized"}
    except Exception as e:
        logger.error(f"Failed to initialize vector database: {e}")
        return {"status": "error", "message": str(e)}
```

---

### 第二部分：定期清理

#### 2.1 清理策略

| 清理类型 | 触发条件 | 清理动作 | 执行频率 |
|---------|---------|---------|---------|
| 失效 Skills | 文件不存在 | 删除向量库记录 | 每日 |
| 失效 MCP 工具 | 服务器不在配置中 | 删除向量库记录 | 每日 |
| 过期记忆 | 创建时间 > 90 天 | 删除所有层级 | 每周 |
| 重复内容 | 同一 ID 多条记录 | 保留最新 | 每日 |

#### 2.2 清理实现

**新建文件**：`agent/vector_cleanup.py`

核心方法：
- `cleanup_orphaned_skills()` - 清理失效的 Skills（文件已删除）
- `cleanup_orphaned_mcp_tools()` - 清理失效的 MCP 工具描述（服务器已移除）
- `cleanup_expired_memories(days=90)` - 清理过期的记忆
- `cleanup_duplicates()` - 清理重复内容（同一 ID 多条记录，保留最新）
- `run_full_cleanup()` - 执行完整清理
- `start_periodic_cleanup(interval_hours=24)` - 启动后台定期清理线程

#### 2.3 集成到启动流程

**修改**：`niu_api/__main__.py`

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动
    logger.info("Niu API Server starting...")

    # ... 现有代码 ...

    # 5. 初始化向量库（新增）
    from agent.vector_search import get_vector_search
    vs = get_vector_search()
    if vs._get_connection() is None:
        logger.info("Vector database not found, initializing...")
        from scripts.init_vector_db import init_vector_db, sync_skills, register_mcp_tools
        init_vector_db(vs.db_path)
        sync_skills()
        register_mcp_tools()
        logger.info("Vector database initialized")
    else:
        logger.info("Vector database found")

    # 6. 启动定期清理（新增）
    from agent.vector_cleanup import get_cleanup_service
    cleanup = get_cleanup_service()
    cleanup.run_full_cleanup()  # 启动时执行一次
    cleanup.start_periodic_cleanup(interval_hours=24)  # 后台每天执行

    # 7. 预加载 MCP 工具
    # ... 现有代码 ...

    yield

    # 关闭
    logger.info("Niu API Server shutting down...")

    # 停止清理服务（新增）
    cleanup.stop_periodic_cleanup()

    # ... 现有代码 ...
```

#### 2.4 API 端点

**新增端点**：`POST /api/vector/cleanup`

```python
# niu_api/compat.py

@router.post("/api/vector/cleanup")
async def trigger_cleanup():
    """手动触发向量库清理"""
    from agent.vector_cleanup import get_cleanup_service

    try:
        cleanup = get_cleanup_service()
        cleanup.run_full_cleanup()
        return {"status": "success", "message": "Cleanup completed"}
    except Exception as e:
        logger.error(f"Cleanup failed: {e}")
        return {"status": "error", "message": str(e)}
```

---

### 第三部分：监控和统计

#### 3.1 统计端点

**新增端点**：`GET /api/vector/stats`

```python
@router.get("/api/vector/stats")
async def get_vector_stats():
    """获取向量库统计信息"""
    from agent.vector_search import get_vector_search

    vs = get_vector_search()
    conn = vs._get_connection()
    if conn is None:
        return {"error": "Vector database not initialized"}

    cursor = conn.cursor()

    # 总数
    cursor.execute("SELECT COUNT(*) FROM documents")
    total = cursor.fetchone()[0]

    # 按类别统计
    cursor.execute(
        """
        SELECT json_extract(metadata, '$.category') as category, COUNT(*) as count
        FROM documents
        GROUP BY category
        """
    )
    by_category = {row[0] or "unknown": row[1] for row in cursor.fetchall()}

    # 按层级统计
    cursor.execute(
        """
        SELECT json_extract(metadata, '$.level') as level, COUNT(*) as count
        FROM documents
        GROUP BY level
        """
    )
    by_level = {row[0] or "unknown": row[1] for row in cursor.fetchall()}

    # 数据库大小
    import os
    db_size_mb = os.path.getsize(vs.db_path) / (1024 * 1024) if os.path.exists(vs.db_path) else 0

    return {
        "total": total,
        "by_category": by_category,
        "by_level": by_level,
        "db_size_mb": round(db_size_mb, 2),
        "db_path": vs.db_path,
    }
```

#### 3.2 配置参数

**新增配置**：`~/.niu/preferences.json`

```json
{
  "vector": {
    "cleanup": {
      "enabled": true,
      "interval_hours": 24,
      "memory_retention_days": 90
    }
  }
}
```

---

## 实施计划

### Phase 1: 自动初始化（优先级：高）

**任务**：
1. 修改 `niu_api/__main__.py` 添加向量库检测和初始化逻辑
2. 新增 `POST /api/vector/init` 端点
3. 测试首次启动自动初始化

**预计时间**：1 小时

### Phase 2: 清理服务（优先级：高）

**任务**：
1. 创建 `agent/vector_cleanup.py`
2. 实现 4 种清理逻辑
3. 修改 `niu_api/__main__.py` 集成清理服务
4. 新增 `POST /api/vector/cleanup` 端点

**预计时间**：2 小时

### Phase 3: 监控和配置（优先级：中）

**任务**：
1. 新增 `GET /api/vector/stats` 端点
2. 添加配置参数到 `preferences.json`
3. 文档更新

**预计时间**：1 小时

### Phase 4: 测试和优化（优先级：中）

**任务**：
1. 单元测试
2. 集成测试
3. 性能测试（大数据量场景）

**预计时间**：1 小时

**总计**：5 小时

---

## 验证方案

### 功能测试

1. **初始化测试**
   ```bash
   # 删除向量库
   rm ~/.niu/vectors.db

   # 启动服务
   ./niu.exe

   # 验证向量库已创建
   python -c "from agent.vector_search import get_vector_search; print(get_vector_search()._get_connection())"
   ```

2. **清理测试**
   ```bash
   # 触发清理
   curl -X POST http://127.0.0.1:9876/api/vector/cleanup

   # 验证结果
   curl http://127.0.0.1:9876/api/vector/stats
   ```

3. **定期清理测试**
   - 修改清理间隔为 1 分钟
   - 观察日志输出
   - 验证清理执行

---

## 风险评估

| 风险 | 级别 | 缓解措施 |
|------|------|----------|
| 初始化阻塞启动 | 中 | 优化初始化速度，显示进度 |
| 清理删除重要数据 | 高 | 增加确认机制，先备份 |
| 数据库锁定冲突 | 低 | 使用 WAL 模式，避免长时间事务 |
| 清理服务崩溃 | 低 | 异常捕获，自动恢复 |

---

## 参考资料

- [SQLite WAL 模式](https://www.sqlite.org/wal.html)
- [向量数据库最佳实践](https://docs.pinecone.io/docs/best-practices)
- `docs/design-self-evolution-system.md` - Agent Memory System Design
