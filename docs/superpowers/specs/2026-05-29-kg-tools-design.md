# 知识图谱工具全量对接设计

## 背景

当前MCP工具层只提供了知识图谱的插入（带dedup）、删除（删实体连带删关系）、查询功能。缺少编辑、精确查询、单独删关系等基础能力。LightRAG fork版本已提供7个异步函数，需要全量对接。

## 核心设计原则

1. dedup发现重复时，不静默跳过，反馈可操作选项给Agent
2. 两条入库路径各司其职：结构化入库创建新实体，非结构化入库追加描述（<SEP>合并）
3. TDD开发，真实环境集成测试，不允许mock

## 新增7个MCP工具

| 工具名 | 对应LightRAG函数 | 功能 |
|--------|-----------------|------|
| lightrag_edit_entity | aedit_entity | 修改实体属性（支持改名、合并） |
| lightrag_edit_relation | aedit_relation | 修改关系属性 |
| lightrag_delete_relation | adelete_by_relation | 只删关系不删实体 |
| lightrag_get_entity_info | get_entity_info | 查询单个实体详情 |
| lightrag_get_relation_info | get_relation_info | 查询单个关系详情 |
| lightrag_create_entity | acreate_entity | 单独创建实体 |
| lightrag_create_relation | acreate_relation | 单独创建关系 |

### 各工具参数设计（基于LightRAG源码确认）

#### lightrag_edit_entity
对应 `aedit_entity(entity_name, updated_data, allow_rename, allow_merge)`
- entity_name (位置参数1): 实体名
- --entity_type: 新实体类型
- --description: 新描述（覆盖式）
- --new_name: 新实体名（allow_rename=True时生效）
- --allow_rename: 是否允许改名（默认False）
- --allow_merge: 是否允许合并（默认False，True时合并description和keywords）

#### lightrag_edit_relation
对应 `aedit_relation(source_entity, target_entity, relation_type, updated_data)`
- source_entity (位置参数1): 源实体名
- target_entity (位置参数2): 目标实体名
- --keywords: 关系关键词（用于定位关系）
- --new_keywords: 新关键词
- --new_description: 新描述
- --new_weight: 新权重

#### lightrag_delete_relation
对应 `adelete_by_relation(source_entity, target_entity, relation_type=None)`
- source_entity (位置参数1): 源实体名
- target_entity (位置参数2): 目标实体名
- --keywords: 关系关键词（不指定则删除两实体间所有关系）

#### lightrag_get_entity_info
对应 `get_entity_info(entity_name, include_vector_data=False)`
- entity_name (位置参数1): 实体名
- --include_vector_data: 是否包含向量数据（默认False）

#### lightrag_get_relation_info
对应 `get_relation_info(source_entity, target_entity, include_vector_data=False)`
- source_entity (位置参数1): 源实体名
- target_entity (位置参数2): 目标实体名
- --include_vector_data: 是否包含向量数据（默认False）

#### lightrag_create_entity
对应 `acreate_entity(entity_name, entity_type, description=None)`
- entity_name (位置参数1): 实体名
- --entity_type: 实体类型（必填）
- --description: 实体描述

#### lightrag_create_relation
对应 `acreate_relation(source_entity, target_entity, relation_type, description=None, weight=None)`
- source_entity (位置参数1): 源实体名
- target_entity (位置参数2): 目标实体名
- --keywords: 关系关键词（必填）
- --description: 关系描述
- --weight: 关系权重

## 对接层次

### 1. Adapter层（lightrag_adapter.py）

为7个函数各封装一个同步方法，复用 call_async 桥接模式：

```python
def edit_entity(self, entity_name, updated_data, allow_rename=False, allow_merge=False):
    return self.call_async(self._rag.aedit_entity, entity_name, updated_data, allow_rename=allow_rename, allow_merge=allow_merge)
```

### 2. MCP工具层（niu_lightrag_server/__init__.py）

在 TOOL_SCHEMAS 中新增7个工具定义，格式与现有工具一致。每个工具函数通过 ToolRegistry 调用 adapter 方法。

### 3. YAML配置（config/disk/lightrag-server.yaml）

新增7个工具的虚拟磁盘路径映射，格式与现有映射一致。

### 4. dedup改造

lightrag_insert_custom_kg / lightrag_insert_entity / lightrag_insert_relation 的 dedup 反馈信息改造。

## dedup反馈信息改造

当前：发现重复 → 跳过 → 只报告"已存在"
改为：发现重复 → 跳过注入 → 返回可操作选项

实体重复反馈格式：
```
实体"XXX"已存在（当前描述：YYY）。可选操作：
1. 追加描述：disk("/lightrag/lightrag_insert '新描述内容'")
2. 删除重建：disk("/lightrag/lightrag_delete_entity 'XXX'") 后重新插入
3. 修改描述：disk("/lightrag/lightrag_edit_entity 'XXX' --description '新描述'")
```

关系重复反馈格式：
```
关系"SRC→TGT(KEYWORDS)"已存在。可选操作：
1. 修改关系：disk("/lightrag/lightrag_edit_relation 'SRC' 'TGT' --keywords 'KEYWORDS' --new_description '新描述'")
2. 删除关系：disk("/lightrag/lightrag_delete_relation 'SRC' 'TGT' --keywords 'KEYWORDS'")
```

## 测试要求

- TDD：先写测试再写实现
- 真实环境：启动主程序做集成测试
- 不允许mock
- 测试覆盖：每个工具的正常路径 + 边界条件（实体不存在、关系不存在、空参数等）
- dedup反馈信息测试：验证重复注入时返回正确的可操作选项

## KG开发字典实时同步（硬性要求）

KG开发字典（`docs/kg-dev-dictionary.md`）必须与代码保持实时同步。每次新增、修改、删除MCP工具时，字典必须同步更新。

**规则**：
1. **代码变更即字典变更**：任何MCP工具的新增/修改/删除，必须同时更新字典，不允许滞后
2. **字典内容**：工具名、参数、用法示例、注意事项，供主Agent和子Agent运行时查阅
3. **实现顺序**：每个工具开发完成后立即更新对应字典条目，不是全部完成后再补
4. **验证**：字典内容必须与TOOL_SCHEMAS定义、YAML映射、adapter方法签名三者一致
