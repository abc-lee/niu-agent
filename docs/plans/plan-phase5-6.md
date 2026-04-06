# Phase 5-6 执行计划

## 目标
1. Phase 5: 简化 main.go，使其调用 Python agent
2. Phase 6: 删除 Nanobot Go 代码

## 当前状态

### Python Agent (已完成)
- agent/__main__.py: CLI 入口
- agent/llm_client.py: LiteLLM 封装
- agent/handler.py: 工具处理器
- agent/tools/: 7 个原子工具

### Go main.go (需要简化)
- 1683 行代码
- 关键端点:
  - `/api/chat/session` - 主聊天端点
  - `/api/chat/clear` - 清空聊天
  - `/api/context/messages` - 获取历史
  - `/api/shutdown` - 关闭服务
- 依赖:
  - pkg/toolloop - Agent 循环
  - pkg/session - 会话管理
  - pkg/llm - LLM 客户端
  - pkg/agents - Agent 定义
  - pkg/runtime - 运行时
  - pkg/config - 配置加载

## Phase 5: 简化 main.go

### 5.1 保留的功能
- HTTP 服务器
- Electron 窗口管理
- MCP 服务器代理
- 静态文件服务

### 5.2 修改的端点

| 端点 | 修改 |
|------|------|
| `/api/chat/session` | 调用 Python agent HTTP API |
| `/api/chat/clear` | 保留（Python agent session 独立管理） |
| `/api/context/messages` | 保留（从数据库读取） |
| `/api/shutdown` | 保留 |

### 5.3 删除的代码
- toolloop.ChatWithToolLoop 调用
- nanobotRuntime.NewRuntime
- rt.Service, rt.Call
- config.Load() 中 Agent 相关部分

### 5.4 新增代码
- Python agent HTTP 客户端
- 调用 `http://127.0.0.1:9878/chat` 转发请求

## Phase 6: 删除 Nanobot 代码

### 6.1 删除的 pkg/ 目录
```
pkg/
├── agents/          # 删除
├── sampling/        # 删除
├── server/          # 删除
├── toolloop/        # 删除
├── runtime/         # 删除
├── tools/           # 删除
├── types/           # 删除
├── config/          # 删除
├── mcp/             # 删除
├── llm/             # 删除
├── servers/         # 删除
├── assistant/       # 删除
├── supervise/       # 删除
├── scheduler/       # 删除
├── skillformat/     # 删除
├── sessiondata/     # 删除
├── system/          # 删除
├── log/             # 删除
├── uuid/            # 删除
└── version/         # 保留
```

### 6.2 保留的 pkg/ 目录
```
pkg/
├── session/         # 保留（会话数据库）
└── version/         # 保留（版本信息）
```

### 6.3 执行顺序
1. 创建简化版 main.go (main_simplified.go)
2. 测试简化版能编译通过
3. 替换 main.go
4. 编译验证
5. 删除不需要的 pkg/ 目录
6. 最终测试

## 执行步骤

### Step 1: 创建 Python agent HTTP 服务器模式
- 修改 agent/__main__.py 添加 serve 命令
- 监听 127.0.0.1:9878
- 提供 /chat POST 端点

### Step 2: 创建简化版 main.go
- 复制 main.go 到 main_simplified.go
- 删除 Nanobot 相关调用
- 添加 Python agent HTTP 调用

### Step 3: 测试编译
- go build -o niu_test.exe main_simplified.go
- 解决编译错误

### Step 4: 替换并删除
- 备份原 main.go
- 用简化版替换
- 删除不需要的 pkg/ 目录

### Step 5: 最终验证
- 编译 niu.exe
- 运行测试

## 回滚计划

如果出现问题:
1. `git checkout HEAD~1 -- main.go` 恢复 main.go
2. `git checkout HEAD~1 -- pkg/` 恢复 pkg/
3. 重新编译

## Git 提交策略

每完成一个步骤就提交:
1. "feat(agent): add HTTP server mode"
2. "refactor(main): simplify main.go for Python agent"
3. "refactor(pkg): remove Nanobot code"
4. "test: verify Python agent integration"
