# 测试脚本说明

本目录包含用于测试和验证 Niu Agent 功能的脚本。

## LightRAG 查询测试

```bash
python scripts/lightrag_query_test.py
```

测试 LightRAG 各种查询模式（local/global/hybrid/mix/naive）的输出质量和性能。

---

## 用户长期记忆

用户长期记忆使用 `memory.json` permanent 数组（最多 10 条：1 task + 9 memory），不再使用向量库。

相关工具（通过磁盘工具调用）：
- `disk("/memory/user_memory_remember <content> --type task|memory")` — 添加
- `disk("/memory/user_memory_forget <content>")` — 删除
- `disk("/memory/user_memory_list")` — 查看所有

---

## 故障排查

### 检查知识图谱状态

```bash
curl http://127.0.0.1:9876/api/brain/status
```

### 检查 LightRAG 状态

```bash
curl http://127.0.0.1:9876/api/lightrag/status
```

### 实时监控日志

```bash
# 监控记忆相关日志
tail -f logs/api_stderr.log | grep -E "记忆|MEMORY|Dynamic injection"
```

---

## 开发调试

### 监控动态注入

```bash
tail -f logs/api_stderr.log | grep "Dynamic injection"
```

### 监控 MCP 调用

```bash
tail -f logs/api_stderr.log | grep "MCP"
```
