# Agent 能力 - 详细设计

> 版本：v1.1
> 日期：2026-03-25
> 状态：详细设计完成
> 更新：补充子 Agent 委托机制、温度差异设计

---

## 一、设计理念

### 1.1 核心定位

**Agent = 用户与知识库的智能桥梁**

| 传统软件 | 本助理 Agent |
|----------|--------------|
| 用户自己操作 | 用户说一句话，Agent 帮你做 |
| 功能分散在各处 | 统一入口，对话即操作 |
| 需要学习界面 | 自然语言交互 |

### 1.2 能力矩阵

| 能力 | 说明 | 触发方式 |
|------|------|----------|
| **对话问答** | 回答问题、搜索知识库 | 用户发问 |
| **任务执行** | 搜索文件、创建提醒、关联人物 | 用户指令 |
| **周报生成** | 自动汇总时间段内的活动 | 用户请求 / 定时 |
| **提醒功能** | 定时提醒、事务提醒 | 用户设置 / 便签提取 |
| **主动建议** | 发现关联、提醒待办 | 后台分析 |

---

## 二、架构设计

### 2.1 整体架构

```
┌─────────────────────────────────────────────────────────────────┐
│                        用户入口                                  │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐              │
│  │ 悬浮助手    │  │ 图谱页面    │  │ IM 消息     │              │
│  │ (对话)      │  │ (探索)      │  │ (远程)      │              │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘              │
│         │                │                │                      │
│         └────────────────┼────────────────┘                      │
│                          │                                       │
└──────────────────────────┼───────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Agent 核心 (Go)                               │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  Main Agent                                              │   │
│  │  ├── 意图识别                                            │   │
│  │  ├── 任务路由                                            │   │
│  │  ├── 对话生成                                            │   │
│  │  └── 上下文管理                                          │   │
│  └─────────────────────────────────────────────────────────┘   │
│                           │                                     │
│         ┌─────────────────┼─────────────────┐                   │
│         │                 │                 │                   │
│         ▼                 ▼                 ▼                   │
│  ┌────────────┐   ┌────────────┐   ┌────────────┐              │
│  │ 文档 Agent │   │ 照片 Agent │   │ 提醒 Agent │              │
│  │ (子Agent)  │   │ (子Agent)  │   │ (子Agent)  │              │
│  └────────────┘   └────────────┘   └────────────┘              │
└─────────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                    MCP 工具层 (Python)                           │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐           │
│  │ 文档解析  │ │ 人脸识别  │ │ 向量搜索  │ │ 图谱查询  │           │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘           │
└─────────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                    数据存储层                                    │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐           │
│  │ LanceDB  │ │  Kuzu    │ │ SQLite   │ │ 文件系统  │           │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘           │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 基于 Nanobot 的实现

```go
// 主 Agent 配置
// config/agents.yaml
agents:
  main:
    name: "个人助理"
    description: "本地知识助理，帮助用户管理文件、照片、联系人"
    model: "gpt-4o"
    mcpServers:
      - document-parser
      - face-recognition
      - vector-store
      - knowledge-graph
      - reminder
    prompts:
      - system-prompt
    
  document-agent:
    name: "文档处理"
    description: "解析文档、提取实体"
    model: "gpt-4o-mini"
    mcpServers:
      - document-parser
      - vector-store
    
  face-agent:
    name: "人脸识别"
    description: "检测人脸、匹配人物"
    mcpServers:
      - face-recognition
      - knowledge-graph
```

---

## 三、对话功能

### 3.1 意图识别

用户消息 → 意图分类 → 路由处理

```python
INTENTS = {
    # 搜索类
    "search_file": "找文件、搜索文档",
    "search_photo": "找照片、搜索图片",
    "search_contact": "找人、搜索联系人",
    
    # 操作类
    "create_reminder": "创建提醒",
    "create_note": "创建便签",
    "link_person": "关联人物",
    
    # 问答类
    "qa_general": "一般问答",
    "qa_knowledge": "知识库问答",
    
    # 生成类
    "generate_report": "生成周报/总结",
    "generate_summary": "生成摘要",
}
```

### 3.2 意图识别流程

```python
def classify_intent(message: str, context: dict) -> str:
    """识别用户意图"""
    
    # 关键词匹配（快速路径）
    if any(kw in message for kw in ["找", "搜索", "查", "有没有"]):
        if any(kw in message for kw in ["文件", "文档", "合同", "pdf"]):
            return "search_file"
        if any(kw in message for kw in ["照片", "图片", "合影"]):
            return "search_photo"
        if any(kw in message for kw in ["人", "联系", "电话"]):
            return "search_contact"
    
    if any(kw in message for kw in ["提醒", "记得", "别忘了"]):
        return "create_reminder"
    
    if any(kw in message for kw in ["周报", "总结", "汇报"]):
        return "generate_report"
    
    # LLM 分类（复杂情况）
    return llm_classify_intent(message, context)
```

### 3.3 对话上下文管理

```python
class ConversationContext:
    """对话上下文管理"""
    
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.messages = []  # 对话历史
        self.active_entity = None  # 当前关注的实体
        self.pending_tasks = []  # 待处理任务
    
    def add_message(self, role: str, content: str):
        """添加消息"""
        self.messages.append({
            "role": role,
            "content": content,
            "timestamp": datetime.now()
        })
    
    def get_context_for_llm(self) -> str:
        """构建 LLM 上下文"""
        parts = []
        
        # 1. 系统提示
        parts.append(self._build_system_prompt())
        
        # 2. 当前关注实体
        if self.active_entity:
            parts.append(f"当前关注: {self.active_entity}")
        
        # 3. 对话历史（最近 N 条）
        for msg in self.messages[-10:]:
            parts.append(f"{msg['role']}: {msg['content']}")
        
        return "\n\n".join(parts)
    
    def _build_system_prompt(self) -> str:
        """构建系统提示"""
        return """你是个人知识助理，帮助用户管理文件、照片、联系人。

你的能力：
- 搜索文件、照片、联系人
- 创建提醒和便签
- 生成周报和总结
- 回答问题

数据概况：
- 文件数量：{file_count}
- 照片数量：{photo_count}
- 联系人数量：{contact_count}
- 已识别人物：{person_count}

当前时间：{current_time}

请用简洁、友好的方式回复用户。"""
```

### 3.4 系统提示模板

```markdown
# 个人知识助理

你是本地个人知识助理，帮助用户管理文件、照片、联系人。

## 核心能力

1. **搜索**：通过语义描述搜索文件、照片、联系人
2. **提醒**：创建定时提醒
3. **便签**：记录临时想法
4. **周报**：自动生成时间段总结

## 数据概况

{data_summary}

## 当前时间

{current_time}

## 回复原则

- 简洁友好，不说废话
- 需要操作时，先确认再执行
- 不确定时，主动询问
- 保持专业，但有人情味
```

---

## 四、搜索功能

### 4.1 统一搜索接口

```python
class UnifiedSearch:
    """统一搜索"""
    
    def search(self, query: str, filters: dict = None) -> list:
        """统一搜索入口"""
        
        results = []
        
        # 1. 文件搜索
        file_results = self.search_files(query, filters)
        results.extend(file_results)
        
        # 2. 照片搜索
        photo_results = self.search_photos(query, filters)
        results.extend(photo_results)
        
        # 3. 联系人搜索
        contact_results = self.search_contacts(query, filters)
        results.extend(contact_results)
        
        # 4. 便签搜索
        note_results = self.search_notes(query, filters)
        results.extend(note_results)
        
        # 按相关度排序
        results.sort(key=lambda x: x['score'], reverse=True)
        
        return results[:20]  # 返回前 20 条
    
    def search_files(self, query: str, filters: dict) -> list:
        """搜索文件"""
        # 向量搜索
        query_embedding = self.get_embedding(query)
        vector_results = self.vector_db.search(
            table='files',
            query_vector=query_embedding,
            limit=10
        )
        
        # 过滤
        if filters and 'file_type' in filters:
            vector_results = [
                r for r in vector_results 
                if r['file_type'] == filters['file_type']
            ]
        
        return vector_results
```

### 4.2 搜索结果展示

```python
def format_search_results(results: list) -> str:
    """格式化搜索结果"""
    
    if not results:
        return "没有找到相关内容。"
    
    lines = ["找到以下相关内容：\n"]
    
    for i, r in enumerate(results[:10], 1):
        icon = {
            'file': '📄',
            'photo': '🖼️',
            'contact': '👤',
            'note': '📝'
        }.get(r['type'], '📌')
        
        lines.append(f"{i}. {icon} {r['title']}")
        if r.get('summary'):
            lines.append(f"   {r['summary'][:100]}")
        lines.append(f"   相关度: {r['score']:.0%}")
        lines.append("")
    
    return "\n".join(lines)
```

---

## 五、周报/总结生成

### 5.1 数据来源

| 来源 | 数据 | 权重 |
|------|------|------|
| **文件入库** | 新增文档列表 | 中 |
| **照片入库** | 新增照片、人物活动 | 高 |
| **便签记录** | 用户记录的想法 | 高 |
| **提醒完成** | 完成的事务 | 高 |
| **图谱变化** | 新增关系 | 低 |

### 5.2 周报生成流程

```
用户请求："生成上周周报"
    │
    ▼
确定时间范围（上周一至周日）
    │
    ▼
收集数据
    ├── 新增文件
    ├── 新增照片（按人物分组）
    ├── 新增便签
    ├── 完成的提醒
    └── 新增关系
    │
    ▼
按类别汇总
    │
    ▼
LLM 生成文本
    │
    ▼
返回周报
```

### 5.3 周报模板

```python
def generate_weekly_report(start_date, end_date, db, graph):
    """生成周报"""
    
    # 收集数据
    files = db.get_files_in_range(start_date, end_date)
    photos = db.get_photos_in_range(start_date, end_date)
    notes = db.get_notes_in_range(start_date, end_date)
    reminders = db.get_completed_reminders(start_date, end_date)
    
    # 按人物分组照片
    photos_by_person = {}
    for photo in photos:
        for person in photo['persons']:
            photos_by_person.setdefault(person, []).append(photo)
    
    # 构建摘要
    summary = {
        "时间范围": f"{start_date} 至 {end_date}",
        "新增文件": len(files),
        "新增照片": len(photos),
        "涉及人物": list(photos_by_person.keys()),
        "新增便签": len(notes),
        "完成事项": len(reminders),
    }
    
    # LLM 生成
    prompt = f"""请根据以下数据生成一份周报：

{json.dumps(summary, ensure_ascii=False, indent=2)}

详细数据：
- 文件：{[f['name'] for f in files[:10]]}
- 照片人物：{photos_by_person}
- 便签：{[n['content'][:50] for n in notes[:10]]}
- 完成事项：{[r['content'] for r in reminders]}

要求：
1. 结构清晰，分点列出
2. 语言简洁专业
3. 突出重要事项
4. 不超过 500 字
"""
    
    report = llm_generate(prompt)
    return report
```

### 5.4 周报示例

```markdown
# 本周工作总结（2026-03-15 至 2026-03-21）

## 📊 概览
- 新增文件：12 份
- 新增照片：35 张
- 涉及人物：5 人
- 新增便签：8 条
- 完成事项：3 项

## 📄 文档
新增合同文档 3 份：
- 合同-张三-20260315.pdf
- 合作协议-ABC公司.pdf
- 采购合同模板.docx

## 📷 照片活动
本周共拍摄 35 张照片，主要涉及：
- 张三（15 张）：会议、签约现场
- 李四（8 张）：团队活动
- 王五（5 张）：客户拜访

## ✅ 完成事项
- 提交项目进度报告
- 回复客户邮件
- 准备下周会议材料

## 💡 便签要点
- 跟进ABC公司合作意向
- 下周安排张三面谈
- 整理采购清单

---
*由个人助理自动生成*
```

---

## 六、提醒功能

### 6.1 提醒类型

| 类型 | 触发条件 | 示例 |
|------|----------|------|
| **定时提醒** | 指定时间 | "明天下午3点提醒我开会" |
| **周期提醒** | 周期重复 | "每周一早上提醒我写周报" |
| **事务提醒** | 便签提取 | "记得给张三打电话" |

### 6.2 时间解析

```python
import re
from datetime import datetime, timedelta

def parse_time_expression(text: str) -> dict:
    """解析时间表达式"""
    
    now = datetime.now()
    
    # 相对时间
    patterns = {
        r"今天(\d+)[点时](\d+)?分?": lambda m: now.replace(hour=int(m[1]), minute=int(m[2] or 0)),
        r"明天(\d+)[点时](\d+)?分?": lambda m: (now + timedelta(days=1)).replace(hour=int(m[1]), minute=int(m[2] or 0)),
        r"后天": lambda m: now + timedelta(days=2),
        r"下周([一二三四五六日])": lambda m: next_weekday(m[1]),
        r"(\d+)月(\d+)日": lambda m: now.replace(month=int(m[1]), day=int(m[2])),
        r"(\d+)分钟后": lambda m: now + timedelta(minutes=int(m[1])),
        r"(\d+)小时后": lambda m: now + timedelta(hours=int(m[1])),
    }
    
    for pattern, getter in patterns.items():
        match = re.search(pattern, text)
        if match:
            return {
                "trigger_time": getter(match.groups()),
                "matched": match.group()
            }
    
    return None
```

### 6.3 提醒存储

```sql
CREATE TABLE reminders (
    id TEXT PRIMARY KEY,
    content TEXT,                  -- 提醒内容
    trigger_time DATETIME,         -- 触发时间
    repeat_rule TEXT,              -- 重复规则（none/daily/weekly/monthly）
    status TEXT,                   -- pending/completed/cancelled
    source TEXT,                   -- manual/note_extracted
    related_entity TEXT,           -- 关联实体（如联系人）
    created_at DATETIME,
    triggered_at DATETIME          -- 实际触发时间
);
```

### 6.4 提醒触发

```python
class ReminderService:
    """提醒服务"""
    
    def __init__(self, db, notifier):
        self.db = db
        self.notifier = notifier
        self.running = False
    
    def start(self):
        """启动提醒服务"""
        self.running = True
        while self.running:
            self.check_reminders()
            time.sleep(60)  # 每分钟检查一次
    
    def check_reminders(self):
        """检查待触发的提醒"""
        now = datetime.now()
        
        # 查询即将触发的提醒（前后 1 分钟）
        reminders = self.db.execute("""
            SELECT * FROM reminders
            WHERE status = 'pending'
            AND trigger_time BETWEEN ? AND ?
        """, (now - timedelta(minutes=1), now + timedelta(minutes=1)))
        
        for reminder in reminders:
            self.trigger(reminder)
    
    def trigger(self, reminder):
        """触发提醒"""
        # 发送通知
        self.notifier.send(
            title="提醒",
            message=reminder['content'],
            sound=True
        )
        
        # 更新状态
        if reminder['repeat_rule'] == 'none':
            self.db.execute("""
                UPDATE reminders SET status = 'completed', triggered_at = ?
                WHERE id = ?
            """, datetime.now(), reminder['id'])
        else:
            # 计算下次触发时间
            next_time = self.calculate_next_time(reminder)
            self.db.execute("""
                UPDATE reminders SET trigger_time = ?, triggered_at = ?
                WHERE id = ?
            """, next_time, datetime.now(), reminder['id'])
```

### 6.5 便签提取提醒

```python
def extract_reminder_from_note(note: str) -> dict:
    """从便签中提取提醒"""
    
    # 时间关键词
    time_keywords = ["明天", "后天", "下周", "记得", "别忘了", "提醒"]
    
    has_time = any(kw in note for kw in time_keywords)
    
    if not has_time:
        return None
    
    # 解析时间
    time_info = parse_time_expression(note)
    
    if not time_info:
        return None
    
    # 提取内容
    content = note.replace(time_info['matched'], '').strip()
    
    return {
        "content": content,
        "trigger_time": time_info['trigger_time'],
        "source": "note_extracted"
    }
```

---

## 七、主动建议

### 7.1 触发场景

| 场景 | 建议 | 触发条件 |
|------|------|----------|
| **发现关联** | "张三和李五在3张照片中同框" | 新增关系 |
| **待处理事务** | "有 5 个未命名人物需要确认" | 阈值触发 |
| **便签提醒** | "你昨天说今天要给张三打电话" | 时间匹配 |
| **知识关联** | "这份合同和上周见的人有关" | 语义相似 |

### 7.2 建议生成

```python
class SuggestionEngine:
    """建议引擎"""
    
    def generate_suggestions(self) -> list:
        """生成建议"""
        suggestions = []
        
        # 1. 未命名人物
        unnamed = self.get_unnamed_persons()
        if len(unnamed) >= 3:
            suggestions.append({
                "type": "pending_task",
                "priority": "high",
                "title": f"有 {len(unnamed)} 个未命名人物",
                "action": "face_confirm",
                "data": unnamed
            })
        
        # 2. 发现新关联
        new_relations = self.get_new_relations(days=7)
        for relation in new_relations[:3]:
            suggestions.append({
                "type": "discovery",
                "priority": "medium",
                "title": f"发现 {relation['person_a']} 和 {relation['person_b']} 经常同框",
                "action": "view_relation",
                "data": relation
            })
        
        # 3. 便签提醒
        pending_notes = self.get_pending_note_reminders()
        for note in pending_notes:
            suggestions.append({
                "type": "reminder",
                "priority": "high",
                "title": note['content'],
                "action": "complete_reminder",
                "data": note
            })
        
        return suggestions
```

### 7.3 建议展示

在悬浮助手中显示：

```
┌─────────────────────────────────────────────┐
│  💡 发现                                     │
├─────────────────────────────────────────────┤
│                                             │
│  👤 有 5 个未命名人物需要确认               │
│     [现在处理]  [稍后]                      │
│                                             │
│  🔗 张三和李五最近3次同框                   │
│     [查看关系]                              │
│                                             │
│  📝 你昨天说今天要给张三打电话              │
│     [标记完成]  [推迟]                      │
│                                             │
└─────────────────────────────────────────────┘
```

---

## 八、子 Agent 调度

### 8.1 设计理念

**主 Agent 负责对话，子 Agent 负责执行。**

| 角色 | 温度 | 职责 | 行为特点 |
|------|------|------|----------|
| **主 Agent** | 0.7 | 对话、理解意图、委托任务 | 灵活、有创造性、像人类对话 |
| **子 Agent** | 0.2 | 严格执行任务、调用工具 | 严格、按规则办事、不发散 |

**为什么需要温度差异？**

- **主 Agent 温度高 (0.7)**：让对话更自然、更有温度，能够理解用户模糊的表达
- **子 Agent 温度低 (0.2)**：确保严格执行流程，不会跳过步骤，不会自作主张

### 8.2 子 Agent 类型

| 子 Agent | 职责 | 触发条件 | 温度 |
|----------|------|----------|------|
| **file-processor** | 文件处理：复制、解析、存储、向量化 | 文件拖入 | 0.2 |
| **文档 Agent** | 解析文档、提取实体 | 文件入库 | 0.2 |
| **人脸 Agent** | 检测人脸、匹配人物 | 照片入库 | 0.1 |
| **图谱 Agent** | 更新关系、计算强度 | 数据变化 | 0.2 |
| **提醒 Agent** | 检查触发、发送通知 | 定时检查 | 0.1 |

### 8.3 委托机制

主 Agent 通过 `chat-with-{agentName}` 工具委托任务给子 Agent：

```
用户拖入文件
    │
    ▼
主 Agent 接收文件路径
    │
    ├── 识别任务类型："文件处理"
    │
    ├── 查看可用技能
    │   └── file-processing skill: "调用 chat-with-file-processor"
    │
    ├── 调用工具
    │   chat-with-file-processor("处理文件：xxx.pdf 路径：E:\tmp\xxx.pdf")
    │
    └── 子 Agent 执行
        │
        ├── 检查用户偏好（按年份存储？）
        ├── 创建目录
        ├── 复制文件
        ├── 解析内容
        ├── 存储到知识图谱
        ├── 向量化
        │
        └── 返回结果给主 Agent
    │
    ▼
主 Agent 报告给用户："✅ 文件处理完成"
```

### 8.4 配置示例

**主 Agent 配置** (`config/agents/niu.md`)：

```yaml
---
name: 妞妞
description: 个人知识助理
temperature: 0.7                    # 高温度，自然对话
permissions:
  '*': allow
agents:                             # 可委托的子 Agent
  - file-processor
mcpServers:
  - file-parser
  - kg-server
  - vector-store
  - config-manager
---

你是妞妞，一个智能个人知识助理...

# 核心能力
...

# 子 Agent 委托
当用户拖入文件时，委托给 file-processor 子 Agent 处理。
调用 chat-with-file-processor 工具。
```

**子 Agent 配置** (`config/agents/file-processor.md`)：

```yaml
---
name: file-processor
description: 处理文件：复制、解析、存储、向量化
temperature: 0.2                    # 低温度，严格执行
mode: subagent                      # 子 Agent 模式
permissions:
  '*': allow
mcpServers:
  - file-parser
  - kg-server
  - vector-store
  - config-manager
---

你是文件处理子 Agent，负责处理用户拖入的文件。

## 核心规则

1. **严格按用户偏好执行**：用户偏好存储在 memory.json 中
2. **按年份存储**：如果用户偏好包含"年份"，必须先创建年份目录
3. **完整执行流程**：不能跳过任何步骤
4. **禁止递归调用**：你是子 Agent，不要再调用其他 Agent

## 处理流程

### 步骤 1：检查用户偏好
调用 config-manager/get_memory 获取用户偏好

### 步骤 2：创建目录并复制文件
使用 bash 工具执行 mkdir 和 cp

### 步骤 3：解析文件
调用 file-parser/parse_file

### 步骤 4：存储到知识图谱
调用 kg-server/create_document

### 步骤 5：向量化
调用 vector-store/add_document

## 输出格式
✅ 文件处理完成
- 文件名：xxx
- 存储位置：documents/2025/xxx
- 状态：成功/失败
```

### 8.5 调度流程

```
用户操作
    │
    ▼
主 Agent 接收
    │
    ├── 意图识别
    │
    ├── 判断是否需要委托
    │   ├── 文件处理 → file-processor
    │   ├── 照片处理 → face-agent
    │   └── 复杂推理 → 自己处理
    │
    ├── 调用子 Agent（并行执行）
    │   ├── chat-with-file-processor(...)
    │   └── chat-with-face-agent(...)
    │
    ├── 等待子 Agent 返回
    │
    └── 汇总结果返回用户
```

### 8.6 并行执行示例

```python
# 主 Agent 收到多个文件
files = ["report.pdf", "photo.jpg", "contract.docx"]

# 并行委托给不同的子 Agent
async def process_files(files):
    tasks = []
    
    for file in files:
        if is_document(file):
            tasks.append(chat_with_file_processor(file))
        elif is_photo(file):
            tasks.append(chat_with_face_agent(file))
    
    # 并行执行所有子 Agent
    results = await asyncio.gather(*tasks)
    
    return results
```

### 8.7 禁止递归调用

**关键规则**：子 Agent 不能再调用其他 Agent（包括自己）。

```yaml
# 子 Agent 配置中必须包含
rules:
  - 你是子 Agent，不能再调用其他 Agent
  - 不要调用 chat-with-file-processor（那是主 Agent 调用的）
  - 直接调用 MCP 工具完成任务
```

**原因**：
- 防止无限循环
- 避免权限混乱
- 简化错误追踪

### 8.8 错误处理

```python
async def delegate_to_subagent(agent_name: str, task: str) -> dict:
    """委托任务给子 Agent"""
    
    try:
        result = await call_tool(
            f"chat-with-{agent_name}",
            {"prompt": task}
        )
        return {"success": True, "result": result}
    
    except TimeoutError:
        # 子 Agent 超时
        return {"success": False, "error": "子 Agent 执行超时"}
    
    except ToolNotFoundError:
        # 子 Agent 不存在
        return {"success": False, "error": f"子 Agent {agent_name} 不存在"}
    
    except Exception as e:
        # 其他错误
        return {"success": False, "error": str(e)}
```

### 8.9 子 Agent 监控

```python
class SubagentMonitor:
    """子 Agent 监控"""
    
    def __init__(self):
        self.active_subagents = {}  # 正在执行的子 Agent
        self.history = []           # 执行历史
    
    def start_subagent(self, name: str, task: str):
        """记录子 Agent 开始执行"""
        self.active_subagents[name] = {
            "task": task,
            "start_time": datetime.now(),
            "status": "running"
        }
    
    def end_subagent(self, name: str, result: dict):
        """记录子 Agent 执行完成"""
        record = self.active_subagents.pop(name)
        record["end_time"] = datetime.now()
        record["duration"] = (record["end_time"] - record["start_time"]).total_seconds()
        record["result"] = result
        record["status"] = "completed" if result["success"] else "failed"
        
        self.history.append(record)
    
    def get_active_count(self) -> int:
        """获取正在执行的子 Agent 数量"""
        return len(self.active_subagents)
```

---

## 九、MCP 工具定义

### 9.1 搜索工具

```python
# tools/search/server.py

@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="search_files",
            description="搜索文件",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词或语义描述"},
                    "file_type": {"type": "string", "description": "文件类型过滤"},
                    "date_range": {"type": "string", "description": "时间范围"}
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="search_photos",
            description="搜索照片",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                    "person": {"type": "string", "description": "人物名"},
                    "date_range": {"type": "string"}
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="search_contacts",
            description="搜索联系人",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "姓名或组织"}
                },
                "required": ["query"]
            }
        )
    ]
```

### 9.2 提醒工具

```python
# tools/reminder/server.py

@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="create_reminder",
            description="创建提醒",
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "提醒内容"},
                    "trigger_time": {"type": "string", "description": "触发时间（ISO格式）"},
                    "repeat": {"type": "string", "enum": ["none", "daily", "weekly", "monthly"]}
                },
                "required": ["content", "trigger_time"]
            }
        ),
        Tool(
            name="list_reminders",
            description="列出提醒",
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["pending", "completed", "all"]}
                }
            }
        )
    ]
```

### 9.3 周报工具

```python
# tools/report/server.py

@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="generate_weekly_report",
            description="生成周报",
            inputSchema={
                "type": "object",
                "properties": {
                    "start_date": {"type": "string", "description": "开始日期"},
                    "end_date": {"type": "string", "description": "结束日期"}
                }
            }
        ),
        Tool(
            name="generate_summary",
            description="生成摘要",
            inputSchema={
                "type": "object",
                "properties": {
                    "entity_type": {"type": "string", "description": "实体类型"},
                    "entity_id": {"type": "string", "description": "实体ID"}
                }
            }
        )
    ]
```

---

## 十、LLM 调用策略

### 10.1 模型选择

| 场景 | 模型 | 原因 |
|------|------|------|
| **简单问答** | gpt-4o-mini | 快速、便宜 |
| **复杂推理** | gpt-4o | 准确 |
| **文档解析** | gpt-4o-mini | 够用 |
| **周报生成** | gpt-4o | 需要综合能力 |

### 10.2 Token 优化

```python
def optimize_context(messages: list, max_tokens: int = 8000) -> list:
    """优化上下文，控制 token 数量"""
    
    # 1. 保留系统提示
    system_messages = [m for m in messages if m['role'] == 'system']
    
    # 2. 保留最近对话
    recent_messages = messages[-10:]
    
    # 3. 压缩历史
    if len(messages) > 20:
        # 摘要早期对话
        early_messages = messages[:-10]
        summary = summarize_messages(early_messages)
        compressed = [{"role": "system", "content": f"历史摘要：{summary}"}]
    else:
        compressed = []
    
    return system_messages + compressed + recent_messages
```

### 10.3 错误处理

```python
async def call_llm_with_retry(prompt: str, max_retries: int = 3):
    """带重试的 LLM 调用"""
    
    for attempt in range(max_retries):
        try:
            response = await llm_client.generate(prompt)
            return response
        except RateLimitError:
            wait_time = (2 ** attempt) * 1  # 指数退避
            await asyncio.sleep(wait_time)
        except APIError as e:
            if attempt == max_retries - 1:
                raise
            await asyncio.sleep(1)
    
    raise LLMError("LLM call failed after retries")
```

---

## 十一、代码量估算

| 组件 | 代码量 |
|------|--------|
| 意图识别 | ~300 行 |
| 对话管理 | ~400 行 |
| 搜索工具 | ~500 行 |
| 周报生成 | ~300 行 |
| 提醒服务 | ~400 行 |
| 主动建议 | ~300 行 |
| 子 Agent 调度 | ~400 行 |
| MCP 工具层 | ~600 行 |
| **总计** | **~3,200 行** |

---

## 十二、参考资料

### 技术框架

- **Nanobot** - Go Agent 框架，MCP 协议实现
- **OpenViking Context** - 上下文构建模式

### 设计模式

- Intent Classification
- Tool Calling
- Agent Orchestration

---

*文档结束*
