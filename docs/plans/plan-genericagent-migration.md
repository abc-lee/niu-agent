# GenericAgent 迁移方案

> 目标：删除所有 Nanobot Go 代码，用 Python agent core 替代

## 背景

GenericAgent 用 3300 行代码实现了一个智能 Agent，而 Nanobot 用 53 万行代码却效果不如前者。

**核心差异**：
| 维度 | GenericAgent | Nanobot |
|------|-------------|---------|
| 代码量 | ~3,300 行 | ~53万行 |
| 工具数量 | 7 个原子工具 | 56 个专用工具 |
| 自我进化 | ✅ 自动沉淀 Skills | ❌ 静态能力 |
| 能力获取 | code_run 动态创建 | 预定义所有工具 |

## 关键决策

| 决策 | 选择 | 原因 |
|------|------|------|
| LLM SDK | LiteLLM | 原生 MiniMax 支持，比 Vercel AI SDK 简单 |
| Session 存储 | Python SQLite | 完全去除 Go 依赖 |
| 嵌入服务 | 统一 embedding-service | 节省 ~90MB 内存 |
| 迁移策略 | 一次性删除 | 用户明确要求 |

## 工期：16 天

| 阶段 | 工期 | 内容 |
|------|------|------|
| Phase 1 | 3 天 | Python 核心 + LiteLLM + Session |
| Phase 2 | 3 天 | 原子工具迁移 |
| Phase 3 | 4 天 | AgentLoop 移植 |
| Phase 4 | 2 天 | Go Bridge 层 |
| Phase 5 | 1 天 | 删除所有 Nanobot 代码 |
| Phase 6 | 3 天 | 测试验证 |

## Phase 1: Python 核心 + LiteLLM + Session (3天)

### 1.1 创建目录结构

```
E:\tools\ai-bot\
├── agent/                      # 新增 Python agent 核心
│   ├── __init__.py
│   ├── agent_loop.py          # 从 GenericAgent 移植
│   ├── session.py             # SQLite session 管理
│   ├── llm_client.py          # LiteLLM 封装
│   ├── tools/                 # 工具模块
│   │   ├── __init__.py
│   │   ├── code_run.py
│   │   ├── file_ops.py
│   │   ├── web_ops.py
│   │   └── ask_user.py
│   └── memory/                # 三层记忆系统
│       ├── __init__.py
│       ├── l0_rules.py
│       ├── l2_facts.py
│       └── l3_skills.py
├── main.go                     # 瘦身为 thin proxy
└── mcp-servers/                # 保留
```

### 1.2 Python 依赖

```toml
# pyproject.toml
[project]
name = "niu-agent"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "litellm>=1.80.0",           # LLM 统一接口
    "aiosqlite>=0.20.0",         # 异步 SQLite
    "mcp>=1.0.0",                # MCP 协议
    "pydantic>=2.0.0",           # 数据验证
    "httpx>=0.27.0",             # HTTP 客户端
    "loguru>=0.7.0",             # 日志
]
```

### 1.3 LiteLLM 配置

```python
# agent/llm_client.py
import litellm
from litellm import completion

# MiniMax 配置
litellm.api_key = "YOUR_MINIMAX_API_KEY"
litellm.api_base = "https://api.minimax.chat/v1"

async def chat(model: str, messages: list, tools: list = None):
    response = await litellm.acompletion(
        model=f"minimax/{model}",
        messages=messages,
        tools=tools,
        stream=True,
    )
    return response
```

### 1.4 Session Store (SQLite)

```python
# agent/session.py
import aiosqlite
import json
from datetime import datetime

class SessionStore:
    def __init__(self, db_path: str = "sessions.db"):
        self.db_path = db_path
    
    async def init_db(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT,
                    tool_calls TEXT,
                    created_at TEXT
                )
            ''')
            await db.commit()
    
    async def add_message(self, session_id: str, role: str, content: str, tool_calls: list = None):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                INSERT INTO messages (id, session_id, role, content, tool_calls, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (str(uuid4()), session_id, role, content, json.dumps(tool_calls), datetime.now().isoformat()))
            await db.commit()
```

## Phase 2: 原子工具迁移 (3天)

### 2.1 code_run 工具

```python
# agent/tools/code_run.py
import subprocess
import tempfile
import os

async def code_run(code: str, code_type: str = "python", timeout: int = 60, cwd: str = None):
    """执行代码片段"""
    if code_type == "python":
        # 写入临时文件
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode='w') as f:
            f.write(code)
            tmp_path = f.name
        
        # 执行
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", tmp_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd
        )
        
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
            return {
                "status": "success" if proc.returncode == 0 else "error",
                "stdout": stdout.decode('utf-8'),
                "stderr": stderr.decode('utf-8'),
                "exit_code": proc.returncode
            }
        except asyncio.TimeoutError:
            proc.kill()
            return {"status": "error", "msg": "Timeout"}
        finally:
            os.unlink(tmp_path)
```

### 2.2 文件操作工具

```python
# agent/tools/file_ops.py
from pathlib import Path

async def file_read(path: str, start: int = 1, count: int = 200, keyword: str = None):
    """读取文件内容"""
    ...

async def file_write(path: str, content: str, mode: str = "overwrite"):
    """写入文件"""
    ...

async def file_patch(path: str, old_content: str, new_content: str):
    """局部修改文件"""
    ...
```

### 2.3 统一 embedding service

修改 `memory-server` 和 `vector-store`，改为调用 `embedding-service` 的 HTTP API，不再各自加载模型。

```python
# 统一调用
async def get_embedding(text: str) -> list:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "http://127.0.0.1:9877/embed",
            json={"texts": [text]}
        )
        return resp.json()["embeddings"][0]
```

## Phase 3: AgentLoop 移植 (4天)

### 3.1 从 GenericAgent 移植 agent_loop.py

```python
# agent/agent_loop.py
from dataclasses import dataclass
from typing import Any, Optional

@dataclass
class StepOutcome:
    data: Any
    next_prompt: Optional[str] = None
    should_exit: bool = False

class BaseHandler:
    def tool_before_callback(self, tool_name, args, response): pass
    def tool_after_callback(self, tool_name, args, response, ret): pass
    def next_prompt_patcher(self, next_prompt, outcome, turn): 
        return next_prompt
    
    def dispatch(self, tool_name, args, response, index=0):
        method_name = f"do_{tool_name}"
        if hasattr(self, method_name):
            args['_index'] = index
            ret = yield from getattr(self, method_name)(args, response)
            return ret
        else:
            yield f"未知工具: {tool_name}\n"
            return StepOutcome(None, next_prompt=f"未知工具 {tool_name}")

async def agent_runner_loop(client, system_prompt, user_input, handler, tools_schema, max_turns=40):
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_input}
    ]
    turn = 0
    
    while turn < max_turns:
        turn += 1
        response = await client.chat(messages=messages, tools=tools_schema)
        
        if not response.tool_calls:
            return {'result': 'DONE', 'data': response.content}
        
        tool_results = []
        for tc in response.tool_calls:
            outcome = await handler.dispatch(tc.name, tc.args, response)
            tool_results.append({'tool_use_id': tc.id, 'content': outcome.data})
        
        messages.append({"role": "user", "content": "", "tool_results": tool_results})
    
    return {'result': 'MAX_TURNS_EXCEEDED'}
```

### 3.2 三层记忆系统

```
memory/
├── l0_rules.txt          # 元规则（≤30行）
├── l2_facts.txt          # 环境事实
└── skills/               # 任务 Skills
    ├── plan_sop.md
    ├── debug_sop.md
    └── ...
```

## Phase 4: Go Bridge 层 (2天)

### 4.1 main.go 瘦身

删除所有 Nanobot 内部依赖，只保留：
- HTTP server
- Electron 窗口管理
- 调用 Python agent 的 bridge

```go
// main.go (精简后)
package main

import (
    "net/http"
    "os/exec"
)

func main() {
    mux := http.NewServeMux()
    
    // 聊天端点 → 转发到 Python agent
    mux.HandleFunc("/api/chat/session", func(w http.ResponseWriter, r *http.Request) {
        // 调用 Python agent
        cmd := exec.Command("python", "-m", "agent", "chat")
        // ...
    })
    
    // MCP 服务器端点 → 直接代理
    mux.HandleFunc("/mcp/", mcpProxyHandler)
    
    http.ListenAndServe(":9876", mux)
}
```

## Phase 5: 删除 Nanobot 代码 (1天)

### 5.1 删除文件清单

```
pkg/
├── agents/          # 删除（~15 文件）
├── sampling/        # 删除（~10 文件）
├── server/          # 删除（~20 文件）
├── toolloop/        # 删除（~5 文件）
├── runtime/         # 删除（~10 文件）
├── tools/           # 删除（~30 文件）
├── types/           # 删除（~15 文件）
├── config/          # 删除（~20 文件）
├── mcp/             # 删除（~20 文件）
├── llm/             # 删除（~15 文件）
└── ...              # 共 ~180 个文件
```

### 5.2 保留内容

```
pkg/
└── session/         # 保留，供 SQLite 迁移参考

mcp-servers/
├── photo-server/    # 保留
├── file-parser/     # 保留
├── vector-store/    # 保留
├── kg-server/       # 保留
├── memory-server/   # 保留
├── embedding-service/ # 保留
└── config-manager/  # 保留

config/
├── agents/niu.md    # 保留，转为 Python prompt
└── mcp-servers.yaml # 保留

ui/                  # 全部保留
```

## Phase 6: 测试验证 (3天)

### 6.1 功能测试

- [ ] 聊天对话正常
- [ ] 工具调用正常
- [ ] MCP 服务器可用
- [ ] Session 持久化
- [ ] 记忆系统工作

### 6.2 性能测试

- [ ] 内存占用下降（去除重复模型加载）
- [ ] 响应速度（LiteLLM 流式输出）
- [ ] 并发处理

### 6.3 回归测试

- [ ] Electron 窗口管理
- [ ] 悬浮窗功能
- [ ] 便签功能
- [ ] 知识图谱展示

## 风险与缓解

| 风险 | 缓解措施 |
|------|----------|
| LiteLLM MiniMax 兼容性 | 先测试，必要时自定义 provider |
| 工具调用格式差异 | 按 GenericAgent 格式适配 |
| Session 数据迁移 | 写迁移脚本，保留原数据 |
| Go/Python 通信开销 | 用 Unix socket 或共享内存 |

## 参考文档

- `E:\tools\GenericAgent\agent_loop.py` - 核心 Agent Loop
- `E:\tools\GenericAgent\ga.py` - 工具实现
- `E:\tools\GenericAgent\llmcore.py` - LLM 抽象层
- `E:\tools\GenericAgent\memory/` - 三层记忆系统
