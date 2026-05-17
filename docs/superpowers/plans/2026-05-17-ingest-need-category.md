# 文件入库分类修复：恢复 need_category 两阶段交互

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 恢复 ingest_document 的 need_category 两阶段交互，让子 Agent 能根据文件内容判断分类目录，而非全部默认"其他"。

**Architecture:** 入库工具先读取文件内容预览，返回 need_category 状态+内容给子 Agent；子 Agent 看到内容后再次调用传入 category 完成入库。同进程模式下直接改函数签名默认值。

**Tech Stack:** Python, MCP ToolRegistry 同进程调用

---

## 根因

1. `ingest_document` 函数签名 `category: str = "其他"` 硬编码了默认值
2. 子 Agent 不传 category 时自动填"其他"，need_category 分支永远不可达
3. 之前 c995d02d 实现了 need_category 但因默认值问题被撤销(eb6f869a)

## 修改清单

### Task 1: 修改 ingest_document 函数签名和 need_category 逻辑

**Files:**
- Modify: `mcp-servers/photo-server/src/niu_photo_server/__init__.py`

- [ ] **Step 1: 修改函数签名默认值**

将 `ingest_document` 函数签名中 `category: str = "其他"` 改为 `category: str = ""`。
空字符串表示"未指定分类"，与"其他"（明确指定为其他）语义不同。

- [ ] **Step 2: 在函数开头添加 need_category 逻辑**

当 `category` 为空字符串时：
1. 调用 `read_file_content(file_path)` 读取内容预览（已有函数，20K限制）
2. 返回 `{"status": "need_category", "file_path": file_path, "content_preview": 内容预览, "message": "请根据内容判断分类目录"}`

- [ ] **Step 3: 修改 call_tool 调度器中的默认值**

`call_tool` 函数中 `category=arguments.get("category", "其他")` 改为 `category=arguments.get("category", "")`，与函数签名一致。

- [ ] **Step 4: 修改 TOOL_SCHEMA 中 category 描述**

将 category 的 description 从"仅填写用户明确要求的分类，否则不填"改为"文件分类目录，如'工作文档'、'个人资料'等。不传则返回内容预览供判断"。
default 值从 `"其他"` 改为 `""`。

### Task 2: 修改 file-processor 子 Agent prompt

**Files:**
- Modify: `config/agents/file-processor.md`

- [ ] **Step 1: 更新子 Agent 的入库指引**

添加说明：当 ingest_document 返回 need_category 时，根据内容预览判断分类目录，然后再次调用 ingest_document 传入 category 参数完成入库。

### Task 3: 真实测试

**Files:**
- Test: 启动完整应用，拖入文件验证

- [ ] **Step 1: 启动应用**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && go run main.go
```

- [ ] **Step 2: 拖入一个文档文件**

验证：
1. 子 Agent 调用 ingest_document 不传 category
2. 工具返回 need_category + 内容预览
3. 子 Agent 根据内容判断分类
4. 再次调用传入 category 完成入库
5. 文件存到正确目录而非"其他"

- [ ] **Step 3: 测试结束后杀掉进程**

```bash
ps aux | grep niu_api | grep -v grep | awk '{print $2}' | xargs kill
```
