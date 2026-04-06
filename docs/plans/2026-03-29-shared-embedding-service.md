# 共享向量服务设计文档

> 创建日期：2026-03-29
> 状态：设计阶段

## 1. 问题背景

### 1.1 当前问题

多个 MCP 服务各自加载 embedding 模型：

| 服务 | 模型 | 加载时机 | 内存占用 |
|------|------|----------|----------|
| vector-store | all-MiniLM-L6-v2 | 预加载 | ~90MB |
| photo-server | all-MiniLM-L6-v2 | 首次调用时 | ~90MB |

**问题**：同一个模型被加载两次，浪费内存和启动时间。

### 1.2 根本原因

- MCP 服务是独立进程，无法共享内存
- 每个需要向量功能的服务都自己加载模型
- 缺乏统一的向量服务

## 2. 设计目标

1. **单次加载**：embedding 模型只加载一次
2. **共享访问**：所有需要向量的服务都能调用
3. **按需加载**：可选的懒加载策略
4. **打包友好**：支持完整打包后的内部通讯

## 3. 架构设计

### 3.1 整体架构

```
┌─────────────────────────────────────────────────────────────┐
│                     Go 后端 (main.go)                        │
│                                                              │
│  ┌─────────────────┐                                        │
│  │  向量服务管理器   │ ← 启动/停止子进程                      │
│  └────────┬────────┘                                        │
│           │ stdio / socket                                   │
│           ▼                                                  │
│  ┌─────────────────────────────────────────────────────┐    │
│  │            向量服务 (Python 子进程)                    │    │
│  │                                                       │    │
│  │   ┌─────────────────────────────────────────────┐   │    │
│  │   │     all-MiniLM-L6-v2 (只加载一次)            │   │    │
│  │   └─────────────────────────────────────────────┘   │    │
│  │                                                       │    │
│  │   接口：                                              │    │
│  │   - encode(text) → vector                           │    │
│  │   - similarity(text1, text2) → float                │    │
│  │   - batch_encode(texts) → vectors                   │    │
│  └─────────────────────────────────────────────────────┘    │
│                                                              │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │ vector-store │  │ photo-server │  │ 其他服务...   │      │
│  │   (MCP)      │  │   (MCP)      │  │   (MCP)      │      │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘      │
│         │                 │                 │               │
│         └─────────────────┼─────────────────┘               │
│                           │                                  │
│                           ▼                                  │
│              通过 Go 后端代理调用向量服务                      │
└─────────────────────────────────────────────────────────────┘
```

### 3.2 通讯方式

#### 开发环境：HTTP

```
向量服务监听 127.0.0.1:9877
MCP 服务通过 HTTP 调用
```

#### 打包后：stdio / 内部调用

```
Go 后端启动向量服务子进程
通过 stdin/stdout 通讯（JSON-RPC 风格）
无需额外端口
```

### 3.3 接口设计

```python
# 向量服务接口

def encode(text: str) -> list[float]:
    """单个文本转向量"""
    pass

def batch_encode(texts: list[str]) -> list[list[float]]:
    """批量文本转向量"""
    pass

def similarity(text1: str, text2: str) -> float:
    """计算两个文本的相似度"""
    pass

def similarity_vectors(vec1: list[float], vec2: list[float]) -> float:
    """计算两个向量的相似度"""
    pass
```

## 4. 实现计划

### 4.1 Phase 1: 创建向量服务

1. 创建 `mcp-servers/embedding-service/`
2. 实现 HTTP 服务（开发调试）
3. 实现 stdio 通讯（打包后）
4. 预加载模型

### 4.2 Phase 2: Go 后端集成

1. 在 `main.go` 中添加向量服务启动逻辑
2. 提供代理接口供 MCP 服务调用
3. 管理服务生命周期

### 4.3 Phase 3: 迁移现有服务

1. **vector-store**：移除内部 embedding 加载，改用共享服务
2. **photo-server**：移除 `get_embedding_model()`，改用共享服务
3. 删除重复代码

### 4.4 Phase 4: 清理

1. 移除 photo-server 的 `calculate_content_similarity`
2. 统一文档相似度判断入口
3. 更新配置和文档

## 5. 文件变更

### 新增文件

```
mcp-servers/embedding-service/
├── src/
│   └── niu_embedding_service/
│       ├── __init__.py
│       ├── __main__.py
│       └── pyproject.toml
└── README.md
```

### 修改文件

```
main.go                          # 启动向量子进程
pkg/assistant/runtime.go         # 添加向量服务代理
mcp-servers/vector-store/...     # 移除 embedding 加载
mcp-servers/photo-server/...     # 移除 embedding 加载
config/mcp-servers.yaml          # 配置变更
```

### 删除代码

```python
# photo-server
- get_embedding_model()
- calculate_content_similarity()
- update_person_name_embedding()  # 已删除
- search_persons() 的向量搜索部分  # 已改为 SQL LIKE

# vector-store  
- 内部的 embedding 加载逻辑（改为调用共享服务）
```

## 6. 风险与缓解

| 风险 | 缓解措施 |
|------|----------|
| 单点故障 | 服务挂了影响所有依赖方，需要健壮的重试机制 |
| 性能瓶颈 | 批量接口、异步处理、连接池 |
| 打包复杂度 | 统一进程管理，优雅退出 |

## 7. 后续优化

1. **缓存**：常用文本的向量缓存
2. **批量处理**：支持批量编码减少调用次数
3. **多模型支持**：可选不同大小的 embedding 模型
