# Niu Agent 自我进化系统设计方案

> **版本：** v1.0
> **日期：** 2026-04-06
> **目标：** 设计一个超越 GenericAgent 的统一自我进化架构

---

## 目录

1. [背景与目标](#1-背景与目标)
2. [架构总览](#2-架构总览)
3. [向量库 L0/L1/L2 规范](#3-向量库-l0l1l2-规范)
4. [记忆分类体系](#4-记忆分类体系)
5. [MCP 工具接口](#5-mcp-工具接口)
6. [NiuHandler 改造方案](#6-niuhandler-改造方案)
7. [迁移步骤](#7-迁移步骤)
8. [性能优化](#8-性能优化)
9. [与 GenericAgent 对比](#9-与-genericagent-对比)

---

## 1. 背景与目标

### 1.1 当前问题

**GenericAgent（3300行）**：
- ✅ 完整的自我进化能力
- ✅ L0/L1/L2 分层记忆
- ✅ 工作记忆 + 长期记忆提炼
- ❌ 文件系统架构（手动维护）
- ❌ 关键词搜索（非语义）

**Niu项目（10000+行）**：
- ✅ 向量库 + MCP 架构（先进）
- ✅ 多模态支持（文档、照片）
- ❌ 自我进化能力残缺
- ❌ 缺少统一的记忆管理
- ❌ 缺少"保存新记忆"能力

### 1.2 设计目标

**能力必须超越 GenericAgent**：
- ✅ 保留所有核心能力（工作记忆、长期记忆、SOP优化）
- ✅ 新增能力（向量语义搜索、多模态记忆、记忆关联图谱）
- ✅ 架构更统一（全向量库，无文件系统）
- ✅ 实现更优雅（MCP 工具统一接口）

**GenericAgent 有的，我们必须有（且更强）**：
- 工作记忆（短期任务上下文）
- 长期记忆（环境事实、用户偏好、任务经验）
- 记忆提炼（从对话中提取精华）
- SOP优化（自动生成技能文档）
- L0/L1/L2 分层（极简索引 → 摘要 → 详情）

**GenericAgent 没有的，我们也应该有**：
- 向量语义搜索（而非关键词匹配）
- 多模态记忆（文档、照片、对话）
- 跨会话知识迁移（一个会话学到的，其他会话也能用）
- 主动学习（Agent 主动发现知识缺口并提问）
- 记忆重要性评分（自动淘汰低价值记忆）
- 记忆关联图谱（知识之间的关系）

---

## 2. 架构总览

### 2.1 系统架构图

```
┌─────────────────────────────────────────────────────────────────┐
│                          用户对话层                               │
│                    (NiuHandler + Runner)                         │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ├─────────────────┬─────────────────────┐
                           │                 │                     │
                ┌──────────▼─────────┐ ┌────▼────────┐ ┌──────────▼─────────┐
                │   工作记忆管理器    │ │ MCP 工具调度 │ │  长期记忆提炼器     │
                │  WorkingMemory     │ │ MCPClient   │ │  MemoryDistiller   │
                └──────────┬─────────┘ └────┬────────┘ └──────────┬─────────┘
                           │                │                     │
                ┌──────────▼────────────────▼─────────────────────▼─────────┐
                │                   向量库统一存储层                          │
                │                  (vectors.db + embedding)                 │
                │                                                             │
                │  ┌──────────────────────────────────────────────────────┐ │
                │  │  L0: 极简索引层 (≤50字符标签)                          │ │
                │  │  • 快速判断是否有相关知识                              │ │
                │  │  • 示例: "硬件探测已完成" → "system_manual:硬件配置"   │ │
                │  └──────────────────────────────────────────────────────┘ │
                │                           ↕ 指针                           │
                │  ┌──────────────────────────────────────────────────────┐ │
                │  │  L1: 摘要层 (当前实现)                                 │ │
                │  │  • 标题 + 关键词 + 摘要 + 实体 + 类型 + 指针          │ │
                │  │  • 重要性评分 + 访问计数 + 创建时间                    │ │
                │  └──────────────────────────────────────────────────────┘ │
                │                           ↕ 指针                           │
                │  ┌──────────────────────────────────────────────────────┐ │
                │  │  L2: 详情层 (完整内容)                                 │ │
                │  │  • 完整文档/技能/记忆内容                              │ │
                │  │  • 用于深度阅读和精确引用                              │ │
                │  └──────────────────────────────────────────────────────┘ │
                └─────────────────────────────────────────────────────────────┘
                                            ▲
                                            │
                ┌───────────────────────────┴───────────────────────────┐
                │                                                       │
        ┌───────▼────────┐                         ┌──────────────────▼─────────┐
        │  memory-server │                         │   embedding-service        │
        │  (MCP 工具)     │                         │   (向量嵌入服务)            │
        │  • remember    │                         └────────────────────────────┘
        │  • recall      │
        │  • update      │
        │  • stats       │
        │  • cleanup     │
        └────────────────┘
```

### 2.2 数据流图

```
任务执行流程:
1. 用户输入 → NiuHandler.chat()
   ↓
2. 动态注入资源 (runner._inject_dynamic_resources)
   ├─ 检查 L0 (快速判断是否有相关知识)
   ├─ 搜索 L1 (语义匹配 Skills、MCP Tools、Knowledge、Memories)
   └─ 按需读取 L2 (深度阅读完整内容)
   ↓
3. LLM 推理 + 工具调用
   ├─ 内置工具 (file_read, code_run, etc.)
   ├─ MCP 工具 (memory-server/remember, etc.)
   └─ 子 Agent (file-processor, event-manager, etc.)
   ↓
4. 工作记忆自动记录 (tool_after_callback)
   ├─ 工具调用摘要
   ├─ 发现的关键信息
   └─ 任务进展
   ↓
5. 任务完成判断
   ├─ 无长期价值 → 结束
   └─ 有长期价值 → 触发长期记忆提炼
        ↓
6. 长期记忆提炼 (do_start_long_term_update)
   ├─ LLM 提取精华
   ├─ 分类 (environment/preferences/skills/experiences/facts)
   ├─ 重要性评分
   ├─ 保存 L2 (完整内容)
   ├─ 生成 L1 (摘要)
   └─ 更新 L0 (极简索引)
```

### 2.3 自我进化闭环

```
任务执行 → 工作记忆更新 → 长期记忆提炼 → SOP优化 → 下次改进
    ↓           ↓               ↓            ↓         ↓
 执行任务   自动记录摘要   提炼精华保存   更新向量库   注入相关知识
```

**关键组件**：

| 组件 | 作用 | 实现位置 |
|------|------|---------|
| 工作记忆 | 短期任务上下文（最近20条摘要） | `NiuHandler.tool_after_callback` |
| 长期记忆 | 环境事实、用户偏好、任务经验 | `vectors.db (L0/L1/L2)` |
| 记忆提炼 | 从对话中提取精华 | `NiuHandler.do_start_long_term_update` |
| 动态注入 | 按需注入相关知识 | `NiuRunner._inject_dynamic_resources` |

---

## 3. 向量库 L0/L1/L2 规范

### 3.1 L0：极简索引层

**存储位置**：向量库 `documents` 表，`metadata.level="l0"`

**内容格式**：
```json
{
  "id": "l0:hardware_detection",
  "content": "硬件探测已完成",
  "embedding": [0.1, 0.2, ...],
  "metadata": {
    "level": "l0",
    "category": "environment",
    "type": "hardware",
    "l1_pointer": "l1:hardware_config_summary",
    "tags": ["硬件", "GPU", "CUDA"],
    "created_at": "2026-04-06T15:00:00",
    "importance": 0.9,
    "access_count": 5
  }
}
```

**关键特性**：
- `content` 长度限制：≤ 50 字符
- 极简标签，快速判断
- 指向 L1 的指针

**示例数据**：
```json
[
  {
    "id": "l0:hardware_detection",
    "content": "硬件探测已完成",
    "metadata": {
      "level": "l0",
      "category": "environment",
      "l1_pointer": "l1:hardware_config"
    }
  },
  {
    "id": "l0:user_preference_style",
    "content": "用户偏好简洁回答",
    "metadata": {
      "level": "l0",
      "category": "preferences",
      "l1_pointer": "l1:user_preferences"
    }
  }
]
```

### 3.2 L1：摘要层（扩展现有）

**存储位置**：向量库 `documents` 表，`metadata.level="l1"`

**内容格式**：
```json
{
  "id": "l1:hardware_config",
  "content": "硬件配置|GPU:RTX4090,RAM:63.8GB,CUDA:可用|系统硬件探测结果，包含 GPU、内存、CUDA 版本等关键配置信息|RTX4090,63.8GB,CUDA|environment|l2:hardware_config_full",
  "embedding": [0.1, 0.2, ...],
  "metadata": {
    "level": "l1",
    "category": "environment",
    "type": "hardware",
    "title": "硬件配置",
    "keywords": ["GPU", "RAM", "CUDA"],
    "summary": "系统硬件探测结果，包含 GPU、内存、CUDA 版本等关键配置信息",
    "entities": ["RTX4090", "63.8GB", "CUDA"],
    "l2_pointer": "l2:hardware_config_full",
    "importance": 0.9,
    "access_count": 5,
    "created_at": "2026-04-06T15:00:00",
    "last_accessed": "2026-04-06T16:30:00"
  }
}
```

**关键特性**：
- `content` 格式：`{标题}|{关键词}|{摘要}|{实体}|{类型}|{L2指针}`
- 新增元数据：`importance`, `access_count`, `created_at`, `last_accessed`
- 指向 L2 的指针

**字段定义**：
- **标题**：≤ 20 字符，简洁概括
- **关键词**：3-5 个关键标签，逗号分隔
- **摘要**：≤ 200 字符，核心信息概括
- **实体**：关键实体（人名、地名、数值），逗号分隔
- **类型**：environment / preferences / skills / experiences / facts
- **L2 指针**：指向完整内容的 ID

### 3.3 L2：详情层

**存储位置**：向量库 `documents` 表，`metadata.level="l2"`

**内容格式**：
```json
{
  "id": "l2:hardware_config_full",
  "content": "## 硬件配置详情\n\n### GPU\n- 型号: NVIDIA GeForce RTX 4090\n- 显存: 24GB GDDR6X\n- CUDA 核心: 16384\n\n### 内存\n- 容量: 63.8 GB\n- 类型: DDR5\n\n### CUDA\n- 版本: 12.1\n- 状态: 可用\n\n### 系统\n- OS: Windows 11 Pro\n- Python: 3.13\n\n### 备注\n- 硬件探测时间: 2026-04-06\n- 探测方法: 使用 nvidia-smi 和 systeminfo 命令",
  "embedding": [0.1, 0.2, ...],
  "metadata": {
    "level": "l2",
    "category": "environment",
    "type": "hardware",
    "title": "硬件配置详情",
    "created_at": "2026-04-06T15:00:00",
    "importance": 0.9,
    "access_count": 3,
    "source": "hardware_detection_task_20260406",
    "related_memories": ["l1:cuda_setup", "l1:python_environment"]
  }
}
```

**关键特性**：
- `content` 存储完整内容（无长度限制）
- `embedding` 可选（大文档可能不生成向量）
- 包含详细的结构化信息
- 支持关联记忆图谱

### 3.4 指针关联机制

```
L0 (极简索引)
  │
  └─→ l1_pointer ─→ L1 (摘要)
                       │
                       └─→ l2_pointer ─→ L2 (详情)
```

**查询流程**：
1. **快速判断（L0）**：搜索 L0，判断是否有相关知识
2. **语义匹配（L1）**：搜索 L1，获取摘要列表
3. **深度阅读（L2）**：根据 L1 的 `l2_pointer` 读取完整内容

---

## 4. 记忆分类体系

### 4.1 完整的分类树

```python
MEMORY_TAXONOMY = {
    "environment": {
        "description": "环境事实（硬件配置、系统版本、已安装软件）",
        "retention": "永久",
        "update_strategy": "探测到变化时更新",
        "importance_default": 0.9,
        "examples": [
            "GPU: RTX 4090, 24GB VRAM",
            "Python: 3.13, pip available",
            "CUDA: 12.1, available"
        ]
    },

    "preferences": {
        "description": "用户偏好（回答风格、语言、格式）",
        "retention": "永久",
        "update_strategy": "用户明确表达时更新",
        "importance_default": 0.85,
        "examples": [
            "回答风格: 简洁、专业、直接执行",
            "语言偏好: 中文",
            "代码风格: 添加注释，使用类型提示"
        ]
    },

    "skills": {
        "description": "技能文档（如何处理某类任务）",
        "retention": "永久",
        "update_strategy": "学会新技能或优化现有技能时更新",
        "importance_default": 0.8,
        "examples": [
            "照片处理: 拖入照片自动入库、人脸识别、分类",
            "文件处理: 解析 PDF、Word、Excel、Markdown",
            "定时任务: 创建、查询、取消、更新"
        ]
    },

    "experiences": {
        "description": "任务经验（完成某任务的步骤、坑点）",
        "retention": "长期（可降级或淘汰）",
        "update_strategy": "完成复杂任务后提炼",
        "importance_default": 0.7,
        "examples": [
            "批量重命名: 使用 rename 命令时注意编码问题，建议使用 Python",
            "依赖安装: Windows 下某些包需要 Visual C++ Build Tools",
            "数据库迁移: 先备份，使用事务，验证后再提交"
        ]
    },

    "facts": {
        "description": "事实知识（用户告知的信息）",
        "retention": "永久",
        "update_strategy": "用户明确告知时记录",
        "importance_default": 0.75,
        "examples": [
            "用户称呼: 老板",
            "工作目录: E:/tmp/bot",
            "项目名称: Niu Agent"
        ]
    }
}
```

### 4.2 每类记忆的属性

| 记忆类型 | 保留时长 | 更新策略 | 注入优先级 | 重要性评分 | 示例 |
|---------|---------|---------|-----------|-----------|------|
| **environment** | 永久 | 探测到变化时更新 | 最高 | 0.9 | 硬件配置、系统版本 |
| **preferences** | 永久 | 用户表达时更新 | 最高 | 0.85 | 回答风格、语言偏好 |
| **skills** | 永久 | 学会新技能时更新 | 高 | 0.8 | 照片处理、文件解析 |
| **experiences** | 长期（可淘汰） | 完成复杂任务后提炼 | 中 | 0.7 | 重命名编码问题 |
| **facts** | 永久 | 用户告知时记录 | 高 | 0.75 | 用户称呼、工作目录 |

### 4.3 记忆淘汰策略

```python
def cleanup_memories(max_age_days=365, min_importance=0.5, min_access_count=1):
    """
    清理低价值记忆

    规则：
    1. 超过 max_age_days 且重要性 < min_importance 且访问次数 < min_access_count
    2. environment 和 preferences 类型永不清除
    """
    candidates = []

    for memory in all_memories:
        # 永久记忆跳过
        if memory.metadata.type in ["environment", "preferences"]:
            continue

        # 计算记忆年龄
        age_days = (now - memory.metadata.created_at).days

        # 淘汰条件
        if (age_days > max_age_days and
            memory.metadata.importance < min_importance and
            memory.metadata.access_count < min_access_count):
            candidates.append(memory.id)

    # 删除候选记忆
    for memory_id in candidates:
        delete_memory(memory_id)
```

---

## 5. MCP 工具接口

### 5.1 完整的工具列表

#### 1. remember - 保存新记忆

```python
{
    "name": "remember",
    "description": "保存新记忆到长期存储。自动生成 L0/L1/L2 分层。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "记忆内容（完整详情）"
            },
            "memory_type": {
                "type": "string",
                "enum": ["environment", "preferences", "skills", "experiences", "facts"],
                "description": "记忆类型"
            },
            "title": {
                "type": "string",
                "description": "记忆标题（≤20字符）"
            },
            "importance": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": "重要性评分（0-1），默认根据类型自动设置"
            },
            "metadata": {
                "type": "object",
                "description": "额外元数据"
            }
        },
        "required": ["content", "memory_type", "title"]
    }
}

# 返回值
{
    "status": "success",
    "memory_id": "l2:hardware_config_full",
    "l1_id": "l1:hardware_config",
    "l0_id": "l0:hardware_detection"
}
```

#### 2. recall - 搜索记忆（增强）

```python
{
    "name": "recall",
    "description": "语义搜索相关记忆。支持 L0/L1/L2 分层查询。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索查询"
            },
            "level": {
                "type": "string",
                "enum": ["l0", "l1", "l2"],
                "description": "查询层级（默认 l1）"
            },
            "memory_type": {
                "type": "string",
                "description": "记忆类型过滤"
            },
            "limit": {
                "type": "integer",
                "description": "返回数量限制（默认 5）"
            },
            "min_importance": {
                "type": "number",
                "description": "最低重要性过滤（默认 0）"
            }
        },
        "required": ["query"]
    }
}

# 返回值
{
    "results": [
        {
            "id": "l1:hardware_config",
            "content": "硬件配置|GPU:RTX4090...",
            "score": 0.95,
            "metadata": {...}
        }
    ]
}
```

#### 3. update_memory - 更新记忆

```python
{
    "name": "update_memory",
    "description": "更新已有记忆。自动同步 L0/L1/L2。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "memory_id": {
                "type": "string",
                "description": "记忆 ID（L2 层级）"
            },
            "new_content": {
                "type": "string",
                "description": "新内容"
            },
            "new_importance": {
                "type": "number",
                "description": "新重要性评分"
            },
            "increment_access": {
                "type": "boolean",
                "description": "是否增加访问计数（默认 true）"
            }
        },
        "required": ["memory_id"]
    }
}
```

#### 4. get_memory_stats - 记忆统计

```python
{
    "name": "get_memory_stats",
    "description": "获取记忆库统计信息。",
    "inputSchema": {
        "type": "object",
        "properties": {}
    }
}

# 返回值
{
    "total_count": 128,
    "by_type": {
        "environment": 12,
        "preferences": 8,
        "skills": 25,
        "experiences": 45,
        "facts": 38
    },
    "by_level": {
        "l0": 128,
        "l1": 128,
        "l2": 128
    },
    "avg_importance": 0.78,
    "oldest_memory": "2025-01-15",
    "newest_memory": "2026-04-06"
}
```

#### 5. cleanup_memories - 清理低价值记忆

```python
{
    "name": "cleanup_memories",
    "description": "清理低价值记忆。自动淘汰过期或低重要性记忆。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "max_age_days": {
                "type": "integer",
                "description": "最大保留天数（默认 365）"
            },
            "min_importance": {
                "type": "number",
                "description": "最低重要性（默认 0.5）"
            },
            "min_access_count": {
                "type": "integer",
                "description": "最低访问次数（默认 1）"
            },
            "dry_run": {
                "type": "boolean",
                "description": "试运行（只返回候选，不实际删除）"
            }
        }
    }
}
```

#### 6. link_memories - 关联记忆

```python
{
    "name": "link_memories",
    "description": "建立记忆之间的关联关系。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "memory_a_id": {
                "type": "string",
                "description": "记忆 A ID"
            },
            "memory_b_id": {
                "type": "string",
                "description": "记忆 B ID"
            },
            "relation": {
                "type": "string",
                "description": "关联关系（depends_on, related_to, extends）"
            }
        },
        "required": ["memory_a_id", "memory_b_id", "relation"]
    }
}
```

### 5.2 使用场景说明

**场景 1：探测到新硬件配置**
```python
# Agent 自动调用
call_mcp_tool("memory-server", "remember", {
    "content": "## 硬件配置详情\n\n### GPU\n- 型号: NVIDIA GeForce RTX 4090\n...",
    "memory_type": "environment",
    "title": "硬件配置",
    "importance": 0.9
})
```

**场景 2：用户表达偏好**
```python
# Agent 调用
call_mcp_tool("memory-server", "remember", {
    "content": "用户偏好简洁、专业的回答风格，避免冗长解释。",
    "memory_type": "preferences",
    "title": "回答风格偏好",
    "importance": 0.85
})
```

**场景 3：完成复杂任务后提炼经验**
```python
# Agent 调用
call_mcp_tool("memory-server", "remember", {
    "content": "批量重命名文件时，Windows 下 rename 命令存在编码问题，建议使用 Python 脚本处理。",
    "memory_type": "experiences",
    "title": "批量重命名编码问题",
    "importance": 0.7
})
```

---

## 6. NiuHandler 改造方案

### 6.1 需要添加的方法

#### 1. 记忆提炼工具

```python
def do_start_long_term_update(self, args: dict, response) -> StepOutcome:
    """
    提炼长期记忆 - 从当前任务中提取精华

    触发条件：
    - 用户明确要求"记住这个"
    - 发现重要环境事实（第一次探测硬件）
    - 学到重要用户偏好（"我喜欢简洁回答"）
    - 完成复杂任务（提炼经验教训）
    """
    extraction_prompt = """### [总结提炼经验]
既然你觉得当前任务有重要信息需要记忆，请提取最近一次任务中【事实验证成功且长期有效】的信息。

**提取行动验证成功的信息**：
- **环境事实**（路径/凭证/配置）→ 调用 memory-server/remember
- **用户偏好**（风格/语言/格式）→ 调用 memory-server/remember
- **任务经验**（关键坑点/前置条件/重要步骤）→ 调用 memory-server/remember

**禁止**：临时变量、具体推理过程、未验证信息、通用常识、你可以轻松复现的细节。

请按以下格式输出：
```json
{
  "memories": [
    {
      "type": "environment|preferences|skills|experiences|facts",
      "title": "记忆标题（≤20字符）",
      "content": "记忆内容（完整详情）",
      "importance": 0.0-1.0
    }
  ]
}
```
"""

    yield "[Info] Start distilling good memory for long-term storage.\n"

    return StepOutcome(
        {"status": "started", "prompt": extraction_prompt},
        next_prompt=extraction_prompt
    )
```

#### 2. 记忆保存工具（辅助）

```python
def do_save_memory(self, args: dict, response) -> StepOutcome:
    """
    保存记忆到向量库（通过 MCP 调用）

    Args:
        content: 记忆内容
        memory_type: 记忆类型
        title: 记忆标题
        importance: 重要性评分
    """
    content = args.get("content", "")
    memory_type = args.get("memory_type", "facts")
    title = args.get("title", "")
    importance = args.get("importance", 0.75)

    if not content or not title:
        return StepOutcome(
            {"status": "error", "msg": "Missing required fields: content, title"},
            next_prompt="\n"
        )

    try:
        from agent.mcp_sync_bridge import get_mcp_bridge
        bridge = get_mcp_bridge()

        result = bridge.call_tool("memory-server", "remember", {
            "content": content,
            "memory_type": memory_type,
            "title": title,
            "importance": importance
        }, timeout=30)

        yield f"[Memory] Saved: {title} (type={memory_type})\n"
        return StepOutcome(result, next_prompt=self._get_anchor_prompt())

    except Exception as e:
        yield f"[Memory] Error: {e}\n"
        return StepOutcome(
            {"status": "error", "msg": str(e)},
            next_prompt="\n"
        )
```

### 6.2 需要修改的方法

#### 1. 工作记忆注入优化

```python
def _get_anchor_prompt(self, skip=False):
    """生成工作记忆提示词 - 增强版"""
    if skip:
        return "\n"

    # 限制历史信息长度
    history_items = self.history_info[-10:]
    h_str = "\n".join(history_items)

    # 去重和压缩
    h_str = self._compress_history(h_str)

    prompt = f"\n### [WORKING MEMORY]\n<history>\n{h_str}\n</history>"
    prompt += f"\nCurrent turn: {self.current_turn}\n"

    # 注入关键信息
    if self.working.get("key_info"):
        key_info = self.working.get("key_info")[:200]
        prompt += f"\n<key_info>{key_info}</key_info>"

    # 注入相关 SOP
    if self.working.get("related_sop"):
        prompt += f"\n有不清晰的地方请再次读取{self.working.get('related_sop')}"

    # 注入长期记忆摘要
    if self.working.get("inject_memories"):
        prompt += f"\n\n### [相关长期记忆]\n{self.working.get('inject_memories')}"

    return prompt
```

#### 2. 工作记忆记录增强

```python
def tool_after_callback(self, tool_name, args, response, ret):
    """工具调用后记录摘要 - 增强版"""
    if args.get("_index", 0) > 0:
        return

    # 提取摘要
    content = getattr(response, "content", "") if response else ""
    rsumm = re.search(r"<summary>(.*?)</summary>", content, re.DOTALL)

    if rsumm:
        summary = rsumm.group(1).strip()[:200]
    else:
        clean_args = {k: v for k, v in args.items() if not k.startswith("_")}
        summary = f"调用工具{tool_name}, args: {clean_args}"
        if tool_name == "no_tool":
            summary = "直接回答了用户问题"

    # 增强：提取关键信息
    key_info = self._extract_key_info(tool_name, args, ret)
    if key_info:
        summary += f" | {key_info}"

    # 记录到工作记忆
    self.history_info.append("[Agent] " + summary[:150])

    # 增强：判断是否值得长期记忆
    if self._should_remember(tool_name, args, ret):
        self.working["suggest_remember"] = True
        self.working["remember_reason"] = self._get_remember_reason(tool_name, args, ret)

    print(
        f"[WorkingMemory] Recorded: {tool_name} -> {summary[:50]}...",
        file=sys.stderr,
        flush=True,
    )
```

#### 3. next_prompt_patcher 增强

```python
def next_prompt_patcher(self, next_prompt, outcome, turn):
    """周期性警告和记忆注入 - 增强版"""
    # 每 35 轮强制 ask_user
    if turn % 35 == 0 and "plan" not in str(self.working.get("related_sop")):
        next_prompt += (
            f"\n\n[DANGER] 已连续执行第 {turn} 轮。你必须总结情况进行 ask_user，"
            "不允许继续重试。"
        )
    # 每 7 轮警告禁止无效重试
    elif turn % 7 == 0:
        next_prompt += (
            f"\n\n[DANGER] 已连续执行第 {turn} 轮。禁止无效重试。"
            "若无有效进展，必须切换策略或请求用户协助。"
        )

    # 增强：每 5 轮注入相关长期记忆
    if turn % 5 == 0 and turn > 0:
        memories = self._recall_relevant_memories(next_prompt)
        if memories:
            next_prompt += f"\n\n### [相关长期记忆]\n{memories}"

    # 增强：如果有建议记忆的标记，提示 LLM
    if self.working.get("suggest_remember"):
        reason = self.working.get("remember_reason", "")
        next_prompt += (
            f"\n\n[SYSTEM TIP] 检测到值得长期记忆的信息: {reason}。"
            "建议调用 start_long_term_update 提炼记忆。"
        )
        self.working.pop("suggest_remember", None)

    return next_prompt
```

---

## 7. 迁移步骤

### 7.1 阶段划分

**阶段 1：数据库扩展（无破坏性）**
- 工作量：0.5 天
- 风险：低
- 内容：扩展 metadata 字段，添加新属性

**阶段 2：memory-server MCP 扩展**
- 工作量：1 天
- 风险：中
- 内容：添加 remember、update_memory、stats、cleanup 工具

**阶段 3：NiuHandler 集成**
- 工作量：1 天
- 风险：中
- 内容：添加 do_start_long_term_update、do_save_memory 方法

**阶段 4：动态注入优化**
- 工作量：0.5 天
- 风险：低
- 内容：增强 _inject_dynamic_resources，支持 L0/L1/L2 查询

**阶段 5：数据迁移脚本**
- 工作量：0.5 天
- 风险：低
- 内容：为现有数据添加新字段，生成 L0 索引

**阶段 6：测试和优化**
- 工作量：0.5 天
- 风险：低
- 内容：测试各项功能，优化性能

**总计工作量：3-5 天**

### 7.2 迁移脚本示例

```python
#!/usr/bin/env python3
"""
记忆系统迁移脚本
"""

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from agent.vector_search import get_vector_search

def migrate_existing_data():
    """迁移现有数据"""
    logger.info("开始迁移现有数据...")

    vs = get_vector_search()
    conn = vs._get_connection()

    if conn is None:
        logger.error("向量库连接失败")
        return

    cursor = conn.execute("SELECT id, content, metadata FROM documents")
    rows = cursor.fetchall()

    updated = 0
    for doc_id, content, metadata_json in rows:
        metadata = json.loads(metadata_json) if metadata_json else {}

        # 检查是否已有 level 字段
        if "level" not in metadata:
            if "summary" in metadata or "|" in content:
                metadata["level"] = "l1"
            else:
                metadata["level"] = "l2"

        # 添加新字段
        if "importance" not in metadata:
            metadata["importance"] = 0.75

        if "access_count" not in metadata:
            metadata["access_count"] = 0

        if "created_at" not in metadata:
            metadata["created_at"] = "2026-01-01T00:00:00"

        # 更新数据库
        conn.execute(
            "UPDATE documents SET metadata = ? WHERE id = ?",
            (json.dumps(metadata, ensure_ascii=False), doc_id)
        )
        updated += 1

    conn.commit()
    logger.info(f"✓ 迁移完成: 更新了 {updated} 条记录")

def generate_l0_index():
    """为现有 L1 数据生成 L0 索引"""
    logger.info("生成 L0 索引...")

    vs = get_vector_search()
    conn = vs._get_connection()

    cursor = conn.execute("SELECT id, content, metadata FROM documents")
    rows = cursor.fetchall()

    l1_docs = [row for row in rows if json.loads(row[2]).get("level") == "l1"]

    generated = 0
    for l1_id, l1_content, l1_metadata_json in l1_docs:
        l1_metadata = json.loads(l1_metadata_json)

        # 生成 L0
        l0_id = l1_id.replace("l1:", "l0:")
        l0_content = l1_metadata.get("title", "未知")[:50]

        # 检查是否已存在
        cursor = conn.execute("SELECT id FROM documents WHERE id = ?", (l0_id,))
        if cursor.fetchone():
            continue

        # 保存 L0
        import numpy as np
        embedding = vs._get_embedding(l0_content)
        if embedding:
            embedding_blob = np.array(embedding, dtype=np.float32).tobytes()

            conn.execute(
                "INSERT INTO documents (id, content, embedding, metadata) VALUES (?, ?, ?, ?)",
                (
                    l0_id,
                    l0_content,
                    embedding_blob,
                    json.dumps({
                        "level": "l0",
                        "category": l1_metadata.get("category", "unknown"),
                        "l1_pointer": l1_id,
                        "created_at": l1_metadata.get("created_at", "2026-01-01T00:00:00")
                    })
                )
            )
            generated += 1

    conn.commit()
    logger.info(f"✓ 生成了 {generated} 个 L0 索引")

def main():
    logger.info("=" * 70)
    logger.info("记忆系统迁移脚本")
    logger.info("=" * 70)

    migrate_existing_data()
    generate_l0_index()

    logger.info("=" * 70)
    logger.info("✓ 迁移完成")
    logger.info("=" * 70)

if __name__ == "__main__":
    main()
```

### 7.3 兼容性保证

**向后兼容**：
1. 保留原有 `metadata.type` 字段（映射到新分类）
2. L1 格式保持兼容（`{标题}|{关键词}|...`）
3. 现有工具调用不受影响

**渐进式迁移**：
1. 阶段 1-2：无破坏性，只是扩展
2. 阶段 3：新功能，不影响现有功能
3. 阶段 4：优化注入逻辑，向后兼容
4. 阶段 5：数据迁移，可回滚

---

## 8. 性能优化

### 8.1 向量搜索优化

**优化 1：索引优化**
```sql
-- 为常用查询字段创建索引
CREATE INDEX IF NOT EXISTS idx_level ON documents(json_extract(metadata, '$.level'));
CREATE INDEX IF NOT EXISTS idx_category ON documents(json_extract(metadata, '$.category'));
CREATE INDEX IF NOT EXISTS idx_importance ON documents(json_extract(metadata, '$.importance'));
```

**优化 2：查询缓存**
```python
from functools import lru_cache

class VectorSearchAdapter:
    @lru_cache(maxsize=100)
    def search_cached(self, query: str, level: str, limit: int) -> list:
        """缓存常用查询（适合 L0/L1 查询）"""
        return self.search(query, limit=limit, filter={"level": level})
```

**优化 3：批量嵌入**
```python
def batch_get_embeddings(texts: list[str]) -> list[list[float]]:
    """批量获取向量嵌入（减少 HTTP 请求次数）"""
    try:
        with httpx.Client(timeout=60) as client:
            response = client.post(
                f"{EMBEDDING_SERVICE_URL}/batch_encode",
                json={"texts": texts}
            )
            response.raise_for_status()
            result = response.json()
            return result["vectors"]
    except Exception as e:
        logger.error(f"批量获取向量失败: {e}")
        raise
```

### 8.2 注入速度优化

**优化 1：异步注入**
```python
import asyncio

async def _inject_dynamic_resources_async(self, user_input: str) -> str:
    """异步并行注入资源"""
    tasks = [
        self._search_category("skill", user_input, 3),
        self._search_category("mcp_tool", user_input, 5),
        self._search_category("document", user_input, 8),
        self._search_category("memory", user_input, 5),
    ]

    results = await asyncio.gather(*tasks)

    parts = []
    for category, items in zip(["skill", "mcp_tool", "document", "memory"], results):
        if items:
            parts.append(format_resources_for_prompt(items, category))

    return "\n".join(parts)
```

**优化 2：注入内容限制**
```python
def _inject_dynamic_resources(self, user_input: str) -> str:
    """动态注入 - 限制总长度"""
    # ... 搜索逻辑

    injection = "\n".join(parts)

    # 限制总长度（避免提示词过长）
    if len(injection) > 2000:
        injection = injection[:2000] + "\n... (已截断)"

    return injection
```

### 8.3 内存占用控制

**控制策略**：
1. L2 层大文档不生成向量（节省内存）
2. 定期清理低价值记忆（`cleanup_memories`）
3. 限制工作记忆长度（最近 10 条）

---

## 9. 与 GenericAgent 对比

### 9.1 功能对比表

| 能力 | GenericAgent | 本方案 | 优势 |
|------|-------------|--------|------|
| **存储架构** | 文件系统 (L0/L1/L2) | 统一向量库 (L0/L1/L2) | ✅ 更快搜索、统一管理 |
| **记忆分类** | 简单 L0/L1/L2 | 5 类分层 + 重要性评分 | ✅ 更精细管理 |
| **搜索方式** | 关键词匹配 | 向量语义搜索 | ✅ 更准确匹配 |
| **多模态** | 仅文本 | 文档 + 照片 + 对话 | ✅ 支持多模态 |
| **记忆淘汰** | 手动维护 | 自动评分淘汰 | ✅ 自动化 |
| **记忆关联** | 无 | 图谱关联 | ✅ 知识网络 |
| **工作记忆** | 显式调用工具 | 自动记录 + 智能提示 | ✅ 更智能 |
| **长期记忆** | 手动提炼 | LLM 自动提炼 | ✅ 更自动化 |
| **跨会话** | 文件共享 | 向量库共享 | ✅ 实时同步 |
| **主动学习** | 无 | 建议记忆机制 | ✅ 主动发现知识缺口 |

### 9.2 性能对比（理论）

| 指标 | GenericAgent | 本方案 | 提升 |
|------|-------------|--------|------|
| 记忆检索速度 | ~100ms（文件IO） | ~10ms（向量搜索） | 10x |
| 记忆准确率 | 60%（关键词） | 90%（语义） | 1.5x |
| 存储空间 | 无限（文件） | 受限（向量库） | - |
| 迁移难度 | 难（文件拷贝） | 易（数据库导出） | ✅ |

### 9.3 优势总结

**架构优势**：
- ✅ 统一向量库存储（无文件系统依赖）
- ✅ MCP 工具统一接口（标准化）
- ✅ L0/L1/L2 分层清晰（查询高效）

**功能优势**：
- ✅ 向量语义搜索（比关键词更准确）
- ✅ 多模态记忆（文档、照片、对话）
- ✅ 记忆重要性评分（自动淘汰低价值）
- ✅ 记忆关联图谱（知识网络）

**实现优势**：
- ✅ 代码量适中（~500 行新增）
- ✅ 向后兼容（无破坏性）
- ✅ 渐进式迁移（可回滚）

---

## 10. 附录

### 10.1 术语表

| 术语 | 定义 |
|------|------|
| **L0** | 极简索引层，≤50字符标签，快速判断 |
| **L1** | 摘要层，标题+关键词+摘要+实体+类型+指针，语义匹配 |
| **L2** | 详情层，完整内容，深度阅读 |
| **工作记忆** | 短期任务上下文（最近 10-20 条摘要） |
| **长期记忆** | 环境事实、用户偏好、任务经验（持久化存储） |
| **记忆提炼** | 从对话中提取精华，保存为长期记忆 |
| **记忆淘汰** | 自动清理低价值记忆（重要性评分 + 访问计数） |

### 10.2 相关文件

**需要修改的文件**：
- `agent/handler.py` — 添加记忆管理方法
- `agent/runner.py` — 增强动态注入
- `agent/vector_search.py` — 支持 L0/L1/L2 分层查询
- `mcp-servers/memory-server/src/niu_memory_server/__init__.py` — 扩展 MCP 工具
- `agent/generic/assets/tools_schema.json` — 更新工具描述

**需要创建的文件**：
- `scripts/migrate_memory_system.py` — 数据迁移脚本

### 10.3 参考资料

- GenericAgent 原始实现：`agent/generic/handler.py`
- L0/L1/L2 规范：`docs/implementation-L0L1L2.md`
- MCP 协议规范：`https://modelcontextprotocol.io/`

---

**文档版本历史**：
- v1.0 (2026-04-06)：初始版本，完整设计方案
