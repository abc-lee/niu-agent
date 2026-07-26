# 见缝插针 — 停止按钮消失回归验证清单

> 前端 chat.html 无单元测试，按 [[real-testing-only]] 记忆要求用真实程序+真实 LLM 手工验证。

## 前置条件

- 清空会话数据库（避免历史对话污染）：
  ```bash
  # 实际布局（已 Glob 验证）：
  # - 会话 DB：~/.niu/messages.db（直接在根，非 databases/ 子目录）
  # - LightRAG：~/.niu/lightrag_storage/vdb_*.json（独立目录，非 *.db）
  # databases/ 目录不存在，原 rm -rf ~/.niu/databases/*.db 命令无效
  # 严格只删 messages.db 及其 -wal/-shm 辅助文件，绝不碰 lightrag_storage/
  if [ -f ~/.niu/messages.db ]; then
    mkdir -p ~/.niu/messages.db.bak.$(date +%s)
    cp ~/.niu/messages.db ~/.niu/messages.db-wal ~/.niu/messages.db-shm ~/.niu/messages.db.bak.$(date +%s)/ 2>/dev/null || true
    rm -f ~/.niu/messages.db ~/.niu/messages.db-wal ~/.niu/messages.db-shm
    echo "messages.db 已备份并清空"
  fi
  # 验证 lightrag_storage 完好
  ls ~/.niu/lightrag_storage/vdb_*.json | head -3
  ```
  **绝不能**用 `rm -rf ~/.niu/databases/` 或 `rm -rf ~/.niu/lightrag*`——前者无效，后者会永久丢失知识图谱（参见记忆 `lightrag-repair-history-failures.md`：mock测试/探针删数据曾导致 77 节点丢失）。
- 启动应用：`./niu`
- 配置 LLM API Key（如未配置）

## 测试用例

### TC-1: 普通消息路径回归

**步骤**：
1. 在聊天窗口输入 "你好" 按回车
2. 等 Agent 回复完成

**预期**：
- 回复期间停止按钮显示（`stopBtn.display='flex'`）
- 回复期间 typing 指示显示
- 回复完成后停止按钮消失
- 回复完成后 typing 指示消失
- Agent 回复正常出现在对话窗口

### TC-2: 见缝插针 — 核心修复验证

**步骤**：
1. 发送一个工具密集型任务确保 Agent 跑 30 秒以上，如 "用 file-parser 解析 ~/Documents 下所有 PDF 文档并总结"（触发子 Agent 处理多个文件），或 "写一篇 3000 字的散文，分章节输出"
2. 等 chat_busy 已推（停止按钮显示）
3. 在 Agent 跑期间（确认 chat_busy 到达后 5-10 秒内），输入 "补充：风格偏古典" 按回车

> **触发稳定性提示**：单条 LLM 调用可能 5-10 秒返回，难以稳定命中"Agent 跑期间"窗口。优先选工具密集型任务（file-parser 处理多个文件）保证有多个轮次和较长的工具执行期。

**预期**：
- 用户气泡 "补充：风格偏古典" 立即渲染
- 停止按钮**仍然显示**（关键验证点）
- HTTP 请求立即返回（后端入队补充消息）
- Agent 继续跑，回复中体现"古典风格"的影响
- Agent 完成后停止按钮消失

### TC-3: Escape 键停止（次生影响）

**步骤**：
1. 重复 TC-2 步骤 1-3（见缝插针补充消息已发送、Agent 还在跑）
2. 按键盘 Escape 键

**预期**：
- 能触发 `/stop`，Agent 停止
- 停止按钮消失
- Agent 输出"已停止"提示

### TC-4: /clear 在 Agent 忙时（次生影响）

**步骤**：
1. 重复 TC-2 步骤 1-3（见缝插针补充消息已发送、Agent 还在跑）
2. 输入 `/clear` 按回车

**预期**：
- 走"先 /stop 等 chat_idle 再清空"分支（不直接 clearChat）
- 收到 `/stop` 后等 chat_idle 事件
- chat_idle 到达后清空对话窗口

### TC-5: subagent_msg 不误触发（次生影响）

**步骤**：
1. 配置主 Agent 调用子 Agent 的场景（如让主 Agent 调 `file-processor`）
2. 子 Agent 完成后推 `subagent_msg` SSE 事件
3. 此时主 Agent 仍在忙（见缝插针补充消息已发送）

**预期**：
- `subagent_msg` SSE 走 `if (!isProcessing)` 判断为假
- 不触发新一轮主 Agent 对话
- 控制台日志："[Stage2] 收到 subagent_msg SSE 但主 Agent 忙，消息可能已从队列 pop"

## 测试后清理

```bash
# 优雅退出，不用 pkill -f niu
ps aux | grep -E 'niu|electron' | grep -v grep
# 如有残留用 kill -TERM <pid>
```

## 失败诊断

如 TC-2 失败（停止按钮仍消失）：
1. 检查 `chat.html:926-935` finally 块是否还有 `stopBtn.style.display='none'` 或 `isProcessing=false`
2. 检查后端 `niu_api/compat.py` 见缝插针分支是否立即返回（不推 chat_idle）
3. 检查后端 `niu_api/chat.py notify_new_message` 是否推 role=user（不推 chat_idle）
