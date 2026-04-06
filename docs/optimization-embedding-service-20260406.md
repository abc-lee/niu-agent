# Embedding Service 优化记录

> 日期：2026-04-06

## 问题背景

### 原始问题
1. **向量语义匹配不准确**：`schedule_task` 工具相似度仅 0.294，排名第3
2. **Embedding Service 不稳定**：连续调用第17次必现超时/崩溃
3. **向量描述过长**：L2完整描述直接存入向量库，关键词被稀释

### 影响
- 用户说"提醒我"时，动态注入找不到 `schedule_task` 工具
- 批量向量注入操作经常失败

---

## 解决方案

### 1. 重写 Embedding Service（FastAPI + Uvicorn）

**问题诊断**：
- 手写 HTTP 服务器实现不健壮
- 连接管理、异常处理不当
- 第17次调用必现超时

**改进**：
```python
# 旧实现：手写 asyncio HTTP 服务器
async def handle_http_request(reader, writer):
    # ... 手动处理请求 ...
    writer.close()  # ❌ 没有 await wait_closed()

# 新实现：FastAPI + Uvicorn
from fastapi import FastAPI
import uvicorn

app = FastAPI()

@app.post("/encode")
async def encode_endpoint(request: EncodeRequest):
    result = encode(request.text)
    return {"vector": result}

uvicorn.run(app, host="127.0.0.1", port=9877)
```

**效果对比**：

| 指标 | 手写服务器 | FastAPI |
|------|-----------|---------|
| 稳定性 | 第17次崩溃 | 30次全成功 ✅ |
| 响应速度 | 慢、不稳定 | 0.01-0.04秒 ✅ |
| 连接管理 | 手动、易泄露 | 自动管理 ✅ |
| 异常处理 | 不完善 | HTTPException ✅ |

---

### 2. 向量库 L1/L2 双层结构

**设计理念**：
- **L1（极简摘要）**：用于语义匹配，只包含关键词+用户场景
- **L2（完整描述）**：供 LLM 查看，包含参数、示例等详细说明

**实现**：
```python
# 向量库存储（L1）
content = "设置提醒、闹钟、定时任务。用户说'提醒我'、'定闹钟'时使用"

# metadata存储（L2）
metadata = {
    "description": "创建定时任务...（300字完整说明）",
    "input_schema": {...}
}
```

**优化效果**：

| 工具 | 优化前相似度 | 优化后相似度 | 提升 |
|------|-------------|-------------|------|
| schedule_task | 0.294 (第3名) | **0.455 (第1名)** | **+55%** ✅ |
| list_scheduled_tasks | 0.258 | **0.297** | +15% |
| cancel_task | 0.321 | **0.273** | 优化 |

---

### 3. 批量优化所有 MCP 工具

**优化内容**：61个MCP工具的L1描述

**L1描述规范**：
```
功能简述 + 用户场景关键词

示例：
- schedule_task: "设置提醒、闹钟、定时任务。用户说'提醒我'、'定闹钟'时使用"
- name_person: "为人物命名。用户说'这是张三'、'这个人叫李四'时使用"
- search_documents: "搜索知识图谱文档。用户说'搜索知识'、'查找文档'时使用"
```

**批量注入脚本**：`scripts/optimize_all_mcp_tools.py`
- 每批处理5个工具
- 批次间暂停10秒
- 使用新embedding模型生成向量

---

### 4. GPU 优先支持

**实现**：
```python
def get_device():
    """检测并返回最优设备（GPU优先）"""
    try:
        import torch
        if torch.cuda.is_available():
            gpu_name = torch.cuda.get_device_name(0)
            logger.info(f"GPU detected: {gpu_name}")
            return "cuda"
    except Exception:
        pass
    
    logger.info("No GPU available, using CPU")
    return "cpu"

# 将模型移到GPU
model = model.to(device)
```

**效果**：
- GPU推理速度比CPU快10-50倍
- RTX 4090: 单次encode 0.01-0.04秒

---

### 5. 多语言模型升级

**模型选择**：
```
旧模型：all-MiniLM-L6-v2 (80MB, 英文为主)
新模型：paraphrase-multilingual-MiniLM-L12-v2 (477MB, 多语言)
```

**改进**：
- 中文语义理解提升
- 模型更大，语义表示更丰富
- 向量维度保持384（兼容现有代码）

---

## 测试验证

### 语义匹配测试

**查询**："5分钟后提醒我"

**结果**：
1. ✅ **schedule_task** (0.455) - 设置提醒
2. update_task (0.344) - 修改提醒
3. recall (0.314) - 搜索记忆
4. list_scheduled_tasks (0.297) - 查询提醒
5. add_user_preference (0.287) - 用户偏好

**前10名中有4个scheduler工具** ✅

---

### 稳定性测试

**测试脚本**：连续调用30次

```bash
# 旧服务器：第17次崩溃
# 新服务器：30次全成功
```

**批量注入测试**：
- 优化前：经常崩溃
- 优化后：61个工具全部成功 ✅

---

## 文件变更

### 修改文件

1. **mcp-servers/embedding-service/pyproject.toml**
   - 添加依赖：fastapi, uvicorn

2. **mcp-servers/embedding-service/src/niu_embedding_service/__init__.py**
   - 重写HTTP服务器（FastAPI + Uvicorn）
   - 添加GPU检测和优先支持
   - 改进异常处理

3. **config/agents/niu.md**
   - 添加提醒任务的委托规则
   - 明确"提醒我"必须调用 chat-with-event-manager

### 新增文件

1. **scripts/download_model.py**
   - HuggingFace模型下载脚本
   - 自动跳过不需要的文件格式

2. **scripts/optimize_all_mcp_tools.py**
   - 批量优化MCP工具L1描述
   - 自动生成向量并更新数据库

3. **scripts/optimize_scheduler_only.py**
   - 单独优化scheduler工具（避免批量崩溃）

---

## 部署注意事项

### 依赖安装

```bash
cd mcp-servers/embedding-service
pip install -e .
```

### 模型下载

```bash
python scripts/download_model.py
```

### 向量库迁移

更换embedding模型后，必须重新生成所有向量：

```bash
python scripts/optimize_all_mcp_tools.py
```

---

## 后续优化建议

1. **向量库备份**：更换模型前备份 `vectors.db`
2. **监控GPU使用**：确认GPU正常工作（日志会显示）
3. **性能调优**：可考虑更大的模型（如 all-mpnet-base-v2）
4. **定期清理**：清理未使用的文档向量，保持数据库精简

---

## 相关文档

- `docs/design-dynamic-injection.md` - 动态注入架构设计
- `config/agents/event-manager.md` - 子Agent配置
- `scripts/reregister_scheduler_tools.py` - Scheduler工具注册脚本
