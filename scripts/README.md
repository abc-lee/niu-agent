# 测试脚本说明

本目录包含用于测试和验证 Niu Agent 功能的脚本。

## 自我进化系统测试

### 基础测试

#### 1. 向量库 L0/L1/L2 功能测试
```bash
python scripts/test_l0l1l2.py
```

**测试内容**：
- Level 参数过滤功能
- get_l2_content() 方法
- 数据库索引创建

**预期结果**：
```
✓ Level 参数过滤功能正常
✓ 数据库索引创建成功
✓ get_l2_content() 可以跟随指针
```

---

#### 2. Memory Server 功能测试
```bash
python scripts/test_memory_server.py
```

**测试内容**：
- L0/L1/L2 三层存储
- 记忆保存和检索
- 统计和清理功能

**预期结果**：
```
✓ 成功创建 L0/L1/L2 三层记录
✓ L1 搜索返回 L1 记录
✓ L2 搜索返回 L2 记录
✓ 统计功能正常
```

---

#### 3. Agent 层自我进化测试
```bash
python scripts/test_agent_evolution.py
```

**测试内容**：
- 记忆类型推断
- 记忆内容生成
- 重要性计算

**预期结果**：
```
✓ 记忆类型推断正确
✓ 重要性计算符合设计
```

---

### 集成测试

#### 向量库初始化
```bash
python scripts/init_vector_db.py
```

**功能**：
- 创建向量库表结构
- 同步 Skills 到向量库
- 注册 MCP 工具描述
- 注入系统说明书

---

#### 向量库验证
```bash
python scripts/verify_vector_db.py
```

**功能**：
- 验证向量库内容是否符合 L0/L1/L2 规范
- 按类型分组统计
- 测试搜索功能

---

## 使用场景测试

### 场景 1：保存记忆

**在对话中说**：
```
请记住：我喜欢使用深色主题，字体大小是14px
```

**验证**：
- 观察回复是否包含"已保存记忆"
- 运行 `python scripts/verify_vector_db.py` 查看新增记录

---

### 场景 2：检索记忆

**5轮对话后问**：
```
我之前说过我喜欢什么主题？
```

**验证**：
- 观察是否能正确回答"深色主题"
- 检查日志中的动态注入：
  ```bash
  tail -f logs/api_stderr.log | grep "Dynamic injection"
  ```

---

### 场景 3：自动记忆建议

**进行 15+ 轮复杂操作**：
```
用户: 帮我写个 Python 脚本
用户: 执行成功了
用户: 再帮我处理这个文件
... (继续操作)
```

**验证**：
- 检查日志是否出现 `[SYSTEM TIP] 检测到值得长期记忆的信息`
- 观察是否建议调用 `start_long_term_update`

---

### 场景 4：记忆统计

**在对话中说**：
```
帮我查看一下保存了多少条记忆
```

**验证**：
- 观察是否返回统计信息
- 检查统计数据的准确性

---

## 故障排查脚本

### 检查向量库状态
```bash
python -c "
from agent.vector_search import get_vector_search
vs = get_vector_search()
results = vs.search('测试', limit=1)
print(f'向量库记录数: {len(results)}')
print(f'数据库路径: {vs.db_path}')
"
```

### 检查 Memory Server 状态
```bash
python scripts/test_memory_server.py
```

### 清理测试数据
```bash
# ⚠️ 警告：会删除所有记忆数据
rm ~/.niu/vectors.db
python scripts/init_vector_db.py
```

---

## 开发调试

### 实时监控日志
```bash
# 监控记忆相关日志
tail -f logs/api_stderr.log | grep -E "记忆|MEMORY|SYSTEM TIP"

# 监控动态注入
tail -f logs/api_stderr.log | grep "Dynamic injection"

# 监控 MCP 调用
tail -f logs/api_stderr.log | grep "MCP"
```

### 数据库查询
```bash
# 查看向量库记录
sqlite3 ~/.niu/vectors.db "SELECT COUNT(*) FROM documents"

# 查看记忆类型分布
sqlite3 ~/.niu/vectors.db "
SELECT json_extract(metadata, '$.memory_type'), COUNT(*)
FROM documents
WHERE json_extract(metadata, '$.level') = 'l1'
GROUP BY json_extract(metadata, '$.memory_type')
"
```

---

## 性能测试

### 批量记忆保存测试
```python
import requests
import time

API_URL = "http://127.0.0.1:9876"

for i in range(100):
    response = requests.post(f"{API_URL}/chat/sync", json={
        "session_id": "perf-test",
        "message": f"请记住：测试数据 {i}"
    })
    print(f"保存第 {i+1} 条记忆")
    time.sleep(0.1)
```

### 检索性能测试
```bash
python -c "
import time
from agent.vector_search import get_vector_search

vs = get_vector_search()

start = time.time()
results = vs.search('测试', limit=10, level='l1')
elapsed = time.time() - start

print(f'搜索耗时: {elapsed*1000:.2f}ms')
print(f'结果数量: {len(results)}')
"
```

---

## 注意事项

1. **测试顺序**：
   - 先运行基础测试（test_l0l1l2.py, test_memory_server.py, test_agent_evolution.py）
   - 再进行对话测试
   - 最后运行集成测试

2. **数据清理**：
   - 测试前确保数据库是干净的
   - 使用 `scripts/init_vector_db.py` 重新初始化

3. **日志监控**：
   - 测试时保持日志窗口开启
   - 观察是否有异常错误

4. **性能监控**：
   - 注意内存使用（向量库会占用内存）
   - 注意 GPU 使用（embedding 模型）
