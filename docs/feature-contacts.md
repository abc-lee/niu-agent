# 通讯录功能 - 详细设计

> 版本：v1.0
> > 日期：2026-03-22
> > 状态：详细设计完成

---

## 一、设计理念

### 1.1 核心理念

**通讯录是人脉图谱的"身份锚点"。**

| 传统通讯录 | 本助理通讯录 |
|------------|--------------|
| 只有名字、电话 | 关联照片、文档、事件 |
| 静态存储 | 动态更新（人脸识别触发）|
| 孤立数据 | 图谱节点，有社会关系 |
| 手动维护 | 自动发现（照片入库→识别人物→关联联系人）|

### 1.2 核心功能

| 功能 | 说明 |
|------|------|
| **导入通讯录** | vCard、CSV、手机导出 |
| **人脸关联** | 照片识别→匹配联系人 |
| **智能去重** | 导入时自动检测重复 |
| **图谱集成** | 联系人成为图谱节点 |
| **语义搜索** | "张三的电话"→直接找到 |

---

## 二、数据模型

### 2.1 联系人表

```sql
CREATE TABLE contacts (
    id TEXT PRIMARY KEY,
    
    -- 基本信息
    given_name TEXT,              -- 名
    family_name TEXT,             -- 姓
    full_name TEXT,               -- 全名（计算字段）
    display_name TEXT,            -- 显示名称
    
    -- 联系方式（一对多，单独表）
    -- phones, emails, addresses
    
    -- 组织信息
    organization TEXT,            -- 公司/组织
    job_title TEXT,               -- 职位
    department TEXT,              -- 部门
    
    -- 个人信息
    birthday TEXT,                -- 生日（ISO格式）
    notes TEXT,                   -- 备注
    
    -- 照片
    avatar_path TEXT,             -- 头像路径
    avatar_source TEXT,           -- 头像来源（vcard/手动选择/照片自动）
    
    -- 元信息
    source TEXT,                  -- 来源：vcard/manual/face_recognition
    source_file TEXT,             -- 导入文件名
    created_at DATETIME,
    updated_at DATETIME,
    
    -- 图谱关联
    graph_node_id TEXT,           -- Kuzu 节点ID
    
    -- 人脸关联
    face_cluster_id TEXT,         -- 关联的人脸聚类ID
    face_encoding BLOB            -- 人脸特征向量（可选，用于快速匹配）
);
```

### 2.2 电话表

```sql
CREATE TABLE contact_phones (
    id TEXT PRIMARY KEY,
    contact_id TEXT,
    number TEXT,                  -- 电话号码
    type TEXT,                    -- mobile/work/home/other
    is_primary INTEGER DEFAULT 0, -- 是否主号码
    
    FOREIGN KEY (contact_id) REFERENCES contacts(id)
);
```

### 2.3 邮箱表

```sql
CREATE TABLE contact_emails (
    id TEXT PRIMARY KEY,
    contact_id TEXT,
    email TEXT,                   -- 邮箱地址
    type TEXT,                    -- work/personal/other
    is_primary INTEGER DEFAULT 0,
    
    FOREIGN KEY (contact_id) REFERENCES contacts(id)
);
```

### 2.4 地址表

```sql
CREATE TABLE contact_addresses (
    id TEXT PRIMARY KEY,
    contact_id TEXT,
    street TEXT,                  -- 街道地址
    city TEXT,                    -- 城市
    state TEXT,                   -- 省/州
    postal_code TEXT,             -- 邮编
    country TEXT,                 -- 国家
    type TEXT,                    -- work/home/other
    
    FOREIGN KEY (contact_id) REFERENCES contacts(id)
);
```

### 2.5 人脸关联表

```sql
CREATE TABLE contact_faces (
    id TEXT PRIMARY KEY,
    contact_id TEXT,              -- 联系人
    face_id TEXT,                 -- 人脸ID（来自人脸识别）
    photo_id TEXT,                -- 来源照片
    confidence REAL,              -- 匹配置信度
    is_primary INTEGER DEFAULT 0, -- 是否主头像
    created_at DATETIME,
    
    FOREIGN KEY (contact_id) REFERENCES contacts(id),
    FOREIGN KEY (face_id) REFERENCES faces(id)
);
```

---

## 三、vCard 导入

### 3.1 支持的格式

| 格式 | 说明 |
|------|------|
| vCard 2.1 | 旧版格式，基本字段 |
| vCard 3.0 | 标准格式，常用 |
| vCard 4.0 | 最新格式，支持更多字段 |

### 3.2 字段映射

| vCard 字段 | 系统字段 |
|------------|----------|
| FN | display_name |
| N (Family, Given) | family_name, given_name |
| TEL | contact_phones |
| EMAIL | contact_emails |
| ORG | organization |
| TITLE | job_title |
| ADR | contact_addresses |
| BDAY | birthday |
| NOTE | notes |
| PHOTO | avatar_path |
| X-* | 扩展字段存入 notes |

### 3.3 导入流程

```
用户拖入 .vcf 文件
    │
    ▼
解析 vCard（使用 vobject）
    │
    ├── 单个联系人
    │   └── 直接创建联系人
    │
    └── 多个联系人（批量）
        ├── 逐个解析
        ├── 检测重复
        └── 创建/合并
    │
    ▼
创建图谱节点
    │
    ▼
通知用户完成
```

### 3.4 Python 实现

```python
import vobject

def import_vcard(file_path, db):
    """导入 vCard 文件"""
    with open(file_path, 'r', encoding='utf-8') as f:
        try:
            # 尝试解析多个联系人
            vcards = list(vobject.readComponents(f.read()))
        except:
            # 单个联系人
            vcards = [vobject.readOne(f.read())]
    
    imported = []
    for vcard in vcards:
        contact = parse_vcard(vcard)
        
        # 检测重复
        existing = find_duplicate(contact, db)
        if existing:
            # 合并信息
            contact = merge_contacts(existing, contact, db)
        else:
            # 创建新联系人
            contact = create_contact(contact, db)
        
        imported.append(contact)
    
    return imported

def parse_vcard(vcard):
    """解析单个 vCard"""
    contact = {}
    
    # 姓名
    if 'fn' in vcard.contents:
        contact['display_name'] = vcard.fn.value
    
    if 'n' in vcard.contents:
        name = vcard.n.value
        contact['family_name'] = name.family or ''
        contact['given_name'] = name.given or ''
    
    # 电话
    contact['phones'] = []
    for tel in vcard.contents.get('tel', []):
        contact['phones'].append({
            'number': tel.value,
            'type': parse_phone_type(tel.params.get('TYPE', []))
        })
    
    # 邮箱
    contact['emails'] = []
    for email in vcard.contents.get('email', []):
        contact['emails'].append({
            'email': email.value,
            'type': parse_email_type(email.params.get('TYPE', []))
        })
    
    # 组织
    if 'org' in vcard.contents:
        contact['organization'] = vcard.org.value[0]
    
    # 职位
    if 'title' in vcard.contents:
        contact['job_title'] = vcard.title.value
    
    # 备注
    if 'note' in vcard.contents:
        contact['notes'] = vcard.note.value
    
    # 生日
    if 'bday' in vcard.contents:
        contact['birthday'] = vcard.bday.value
    
    # 照片
    if 'photo' in vcard.contents:
        photo = vcard.photo.value
        # 保存照片文件...
        contact['avatar_path'] = save_avatar(photo)
    
    return contact
```

---

## 四、人脸-联系人关联

### 4.1 关联场景

| 场景 | 触发 | 流程 |
|------|------|------|
| **新建联系人时** | 用户手动添加 | 如果有头像→提取人脸特征→关联 |
| **照片入库时** | 人脸识别完成 | 匹配已有人物→提示关联联系人 |
| **用户确认时** | 发现新面孔 | 用户命名→创建新联系人 |

### 4.2 两套"人物"数据的关系

**核心概念**：

| 数据来源 | 表 | 说明 |
|----------|-----|------|
| **照片识别** | `persons` 表 | 通过人脸识别创建，可能有名字或"未命名人物_N" |
| **通讯录导入** | `contacts` 表 | 通过 vCard/CSV 导入，有完整联系信息 |

**关系图**：

```
┌─────────────────────────────────────────────────────────────────┐
│                        照片入库                                  │
│                                                                 │
│  照片 A.jpg ──人脸识别──► 人脸 ──聚类──► 人物 (persons)         │
│                                         │                       │
│                                         │ 未命名人物_01         │
│                                         │                       │
└─────────────────────────────────────────┼───────────────────────┘
                                          │
                                          │ 用户确认关联
                                          ▼
┌─────────────────────────────────────────────────────────────────┐
│                        通讯录 (contacts)                         │
│                                                                 │
│  联系人: 张三                                                    │
│  ├── 电话: 138****1234                                          │
│  ├── 邮箱: zhangsan@abc.com                                     │
│  └── 人脸关联: [未命名人物_01] ← contact_faces 表               │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
                                          │
                                          │ 图谱节点
                                          ▼
┌─────────────────────────────────────────────────────────────────┐
│                        知识图谱 (Kuzu)                           │
│                                                                 │
│  ┌─────────┐     APPEARS_IN     ┌─────────┐                    │
│  │  张三   │──────────────────►│ 照片 A  │                    │
│  │(Contact)│                    │ (Photo) │                    │
│  └─────────┘                    └─────────┘                    │
│       │                                                        │
│       │ WORKS_AT                                               │
│       ▼                                                        │
│  ┌─────────┐                                                   │
│  │ ABC公司 │                                                   │
│  │(Org)    │                                                   │
│  └─────────┘                                                   │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 4.3 关联流程详解

```
场景1：照片入库，检测到人物
    │
    ▼
在人脸库中查找匹配
    │
    ├── 匹配到已有人物 (persons 表)
    │   │
    │   ├── 该人物已关联联系人？(查询 contact_faces 表)
    │   │   │
    │   │   ├── 是 → 更新图谱关系（Contact -[APPEARS_IN]-> Photo）
    │   │   │
    │   │   └── 否 → 提示用户关联联系人
    │   │              │
    │   │              ├── 用户选择"创建新联系人"
    │   │              │   └── 创建 Contact + 创建 contact_faces 关联
    │   │              │
    │   │              └── 用户选择"关联已有联系人"
    │   │                  └── 选择 Contact + 创建 contact_faces 关联
    │   │
    │   └── 更新 persons 表的 name 字段（如果用户命名）
    │
    └── 未匹配到 → 创建新人物 (persons 表)
        │
        └── 提示用户命名/关联

场景2：导入通讯录，联系人有头像
    │
    ▼
提取头像人脸特征
    │
    ▼
在人脸库中查找匹配
    │
    ├── 找到匹配 → 
    │   ├── 创建 contact_faces 关联
    │   └── 更新 persons 表的 name 字段为联系人姓名
    │
    └── 未找到 → 
        ├── 创建新人物 (persons 表)
        └── 创建 contact_faces 关联
```

### 4.4 关联确认 UI

**在悬浮助手展开态中显示：**

```
┌─────────────────────────────────────────────┐
│  👤 发现新面孔                               │
├─────────────────────────────────────────────┤
│                                             │
│      ┌─────────────────────────────────┐    │
│      │         [头像图片]              │    │
│      │         120×120px              │    │
│      └─────────────────────────────────┘    │
│                                             │
│  来源：照片A.jpg                            │
│                                             │
│  这是一个新面孔，请选择：                   │
│                                             │
│  ○ 创建新联系人                             │
│    ┌─────────────────────────────────────┐  │
│    │ 姓名：                           │  │
│    └─────────────────────────────────────┘  │
│                                             │
│  ○ 关联到已有联系人                         │
│    ┌─────────────────────────────────────┐  │
│    │ 搜索联系人...                    ▼  │  │
│    └─────────────────────────────────────┘  │
│                                             │
│       [保存]     [跳过]                     │
└─────────────────────────────────────────────┘
```

### 4.4 选择已有联系人

```
┌─────────────────────────────────────────────┐
│  选择联系人                           [×]   │
├─────────────────────────────────────────────┤
│  🔍 搜索姓名或组织...                        │
├─────────────────────────────────────────────┤
│                                             │
│  ┌─────────────────────────────────────┐    │
│  │ 👤 张三 - 经理 - ABC公司     [选择] │    │
│  │    📱 138****1234                   │    │
│  ├─────────────────────────────────────┤    │
│  │ 👤 张三丰 - 总监 - XYZ公司   [选择] │    │
│  │    📱 139****5678                   │    │
│  ├─────────────────────────────────────┤    │
│  │ 👤 小张 - 助理 - DEF公司     [选择] │    │
│  │    📱 137****9012                   │    │
│  └─────────────────────────────────────┘    │
│                                             │
│  [取消]                                     │
└─────────────────────────────────────────────┘
```

---

## 五、联系人去重

### 5.1 去重策略

**三阶段去重：**

```
导入联系人
    │
    ▼
阶段1：精确匹配（自动）
    │
    ├── 邮箱完全相同 → 判定为重复
    ├── 电话完全相同 → 判定为重复
    │
    ▼
阶段2：模糊匹配（建议）
    │
    ├── 姓名相似度 > 90% + 同组织 → 建议合并
    ├── 姓名相似度 > 95% → 建议合并
    │
    ▼
阶段3：用户确认
    │
    └── 显示潜在重复，用户决定
```

### 5.2 相似度计算

```python
from difflib import SequenceMatcher

def calculate_similarity(contact1, contact2):
    """计算两个联系人的相似度"""
    scores = []
    
    # 1. 邮箱匹配（权重最高）
    emails1 = {e['email'].lower() for e in contact1.get('emails', [])}
    emails2 = {e['email'].lower() for e in contact2.get('emails', [])}
    if emails1 and emails2:
        if emails1 & emails2:  # 有交集
            scores.append(1.0)  # 精确匹配
        else:
            scores.append(0.0)
    
    # 2. 电话匹配
    phones1 = {normalize_phone(p['number']) for p in contact1.get('phones', [])}
    phones2 = {normalize_phone(p['number']) for p in contact2.get('phones', [])}
    if phones1 and phones2:
        if phones1 & phones2:
            scores.append(1.0)
        else:
            scores.append(0.0)
    
    # 3. 姓名相似度
    name1 = contact1.get('display_name', '') or f"{contact1.get('given_name', '')}{contact1.get('family_name', '')}"
    name2 = contact2.get('display_name', '') or f"{contact2.get('given_name', '')}{contact2.get('family_name', '')}"
    if name1 and name2:
        name_sim = SequenceMatcher(None, name1.lower(), name2.lower()).ratio()
        scores.append(name_sim * 0.8)  # 权重较低
    
    # 4. 组织相同加分
    org1 = contact1.get('organization', '')
    org2 = contact2.get('organization', '')
    if org1 and org2 and org1.lower() == org2.lower():
        scores.append(0.3)
    
    return max(scores) if scores else 0.0

def normalize_phone(phone):
    """标准化电话号码"""
    import re
    # 只保留数字
    return re.sub(r'[^\d]', '', phone)
```

### 5.3 合并策略

```python
def merge_contacts(existing, new):
    """合并两个联系人"""
    merged = existing.copy()
    
    # 姓名：保留已有，除非新数据更完整
    if not merged.get('display_name') and new.get('display_name'):
        merged['display_name'] = new['display_name']
    
    # 电话：合并去重
    existing_phones = {p['number'] for p in merged.get('phones', [])}
    for phone in new.get('phones', []):
        if phone['number'] not in existing_phones:
            merged.setdefault('phones', []).append(phone)
    
    # 邮箱：合并去重
    existing_emails = {e['email'] for e in merged.get('emails', [])}
    for email in new.get('emails', []):
        if email['email'] not in existing_emails:
            merged.setdefault('emails', []).append(email)
    
    # 组织：优先保留非空的
    merged['organization'] = merged.get('organization') or new.get('organization')
    
    # 职位：优先保留非空的
    merged['job_title'] = merged.get('job_title') or new.get('job_title')
    
    # 备注：合并
    if new.get('notes'):
        merged['notes'] = (merged.get('notes', '') + '\n' + new['notes']).strip()
    
    # 人脸：合并
    if new.get('face_id'):
        merged.setdefault('face_ids', []).append(new['face_id'])
    
    return merged
```

### 5.4 去重确认 UI

```
┌─────────────────────────────────────────────┐
│  ⚠️ 发现可能的重复联系人                     │
├─────────────────────────────────────────────┤
│                                             │
│  已有联系人：                               │
│  ┌─────────────────────────────────────┐    │
│  │ 👤 张三                             │    │
│  │ 📱 138****1234                      │    │
│  │ 📧 zhangsan@abc.com                 │    │
│  │ 🏢 ABC公司 - 经理                   │    │
│  └─────────────────────────────────────┘    │
│                                             │
│  新导入：                                   │
│  ┌─────────────────────────────────────┐    │
│  │ 👤 张三                             │    │
│  │ 📱 139****5678  (新)                │    │
│  │ 📧 zhangsan@gmail.com  (新)         │    │
│  │ 🏢 ABC公司 - 经理                   │    │
│  └─────────────────────────────────────┘    │
│                                             │
│  相似度：95%（姓名相同、组织相同）           │
│                                             │
│  ○ 合并（保留两者所有信息）                  │
│  ○ 保留两者（不合并）                        │
│  ○ 忽略新导入                               │
│                                             │
│       [确认]     [取消]                     │
└─────────────────────────────────────────────┘
```

---

## 六、图谱集成

### 6.1 节点类型

联系人作为图谱节点：

```cypher
// 创建联系人节点
CREATE (c:Contact {
    id: $contact_id,
    name: $full_name,
    organization: $organization,
    job_title: $job_title
})
```

### 6.2 关系类型

| 关系 | 起点 | 终点 | 说明 |
|------|------|------|------|
| **WORKS_AT** | Contact | Organization | 工作单位 |
| **KNOWS** | Contact | Contact | 认识（用户确认）|
| **CO_OCCURS_WITH** | Contact | Contact | 同框（自动发现）|
| **APPEARS_IN** | Contact | Photo | 出现在照片中 |
| **MENTIONED_IN** | Contact | Document | 在文档中被提及 |

### 6.3 同框关系建立

```python
def create_co_occurrence(contact_a, contact_b, photo, db, graph):
    """创建同框关系"""
    
    # 检查是否已有关系
    existing = graph.run("""
        MATCH (a:Contact {id: $id_a})-[r:CO_OCCURS_WITH]->(b:Contact {id: $id_b})
        RETURN r
    """, id_a=contact_a.id, id_b=contact_b.id).data()
    
    if existing:
        # 更新强度
        graph.run("""
            MATCH (a:Contact {id: $id_a})-[r:CO_OCCURS_WITH]->(b:Contact {id: $id_b})
            SET r.strength = r.strength + 1,
                r.last_seen = $photo_date
        """, id_a=contact_a.id, id_b=contact_b.id, photo_date=photo.taken_at)
    else:
        # 创建新关系
        graph.run("""
            MATCH (a:Contact {id: $id_a})
            MATCH (b:Contact {id: $id_b})
            CREATE (a)-[:CO_OCCURS_WITH {
                strength: 1,
                first_seen: $photo_date,
                last_seen: $photo_date,
                source: 'photo'
            }]->(b)
        """, id_a=contact_a.id, id_b=contact_b.id, photo_date=photo.taken_at)
```

### 6.4 图谱展示

```
点击联系人节点 → 展开关系网络：

           ┌─────────┐
           │ ABC公司 │ ← 上门：所属组织
           └────┬────┘
                │
           ┌────┴────┐
           │  张三   │ ← 中心节点
           └────┬────┘
                │
    ┌───────────┼───────────┐
    │           │           │
┌───┴───┐  ┌────┴────┐  ┌───┴───┐
│ 李四  │  │ 合同X   │  │ 照片Y  │
│(同框3次)│ │(提及)   │  │(出现)  │
└───────┘  └─────────┘  └───────┘
```

---

## 七、语义搜索

### 7.1 搜索场景

| 搜索方式 | 示例 | 实现 |
|----------|------|------|
| **按姓名** | "张三" | 精确/模糊匹配 |
| **按组织** | "ABC公司的人" | 组织过滤 |
| **按电话** | "138****1234是谁" | 电话反查 |
| **按语义** | "上个月见过的那个客户" | 向量搜索 + 时间过滤 |

### 7.2 搜索索引

```sql
-- 全文搜索索引
CREATE VIRTUAL TABLE contact_fts USING fts5(
    full_name,
    organization,
    notes,
    content=contacts,
    content_rowid=rowid
);

-- 向量索引（LanceDB）
-- 存储：姓名 + 组织 + 备注的 embedding
```

### 7.3 搜索实现

```python
def search_contacts(query, db, vector_db, limit=10):
    """混合搜索联系人"""
    
    results = []
    
    # 1. 全文搜索
    fts_results = db.execute("""
        SELECT c.*, bm25(contact_fts) as score
        FROM contacts c
        JOIN contact_fts fts ON c.rowid = fts.rowid
        WHERE contact_fts MATCH ?
        ORDER BY score
        LIMIT ?
    """, (query, limit)).fetchall()
    results.extend(fts_results)
    
    # 2. 向量搜索
    query_embedding = get_embedding(query)
    vector_results = vector_db.search(
        table='contacts',
        query_vector=query_embedding,
        limit=limit
    )
    
    # 3. 合并结果
    merged = merge_search_results(results, vector_results)
    
    return merged[:limit]
```

---

## 八、导入来源支持

### 8.1 支持的格式

| 来源 | 格式 | 说明 |
|------|------|------|
| **vCard** | `.vcf` | 标准联系人格式 |
| **CSV** | `.csv` | 通用表格格式 |
| **手机导出** | 各种 | 自动识别格式 |

### 8.2 CSV 字段映射

用户导入 CSV 时，需要手动映射字段：

```
┌─────────────────────────────────────────────┐
│  导入 CSV 文件                               │
├─────────────────────────────────────────────┤
│  文件：contacts.csv                         │
│  行数：256                                  │
│                                             │
│  字段映射：                                  │
│  ┌────────────────┬────────────────┐        │
│  │ CSV 列名       │ 系统字段       │        │
│  ├────────────────┼────────────────┤        │
│  │ 姓名          → 显示名称    ▼  │        │
│  │ 手机          → 电话        ▼  │        │
│  │ 邮箱          → 邮箱        ▼  │        │
│  │ 公司          → 组织        ▼  │        │
│  │ 职务          → 职位        ▼  │        │
│  │ 备注          → 备注        ▼  │        │
│  └────────────────┴────────────────┘        │
│                                             │
│  ☑ 跳过第一行（标题）                        │
│                                             │
│       [预览]     [导入]     [取消]           │
└─────────────────────────────────────────────┘
```

---

## 九、权限与隐私

### 9.1 敏感信息处理

| 信息类型 | 处理方式 |
|----------|----------|
| 电话号码 | 存储明文，搜索时可模糊显示 |
| 邮箱 | 存储明文 |
| 地址 | 存储明文 |
| 人脸特征 | 仅用于匹配，不暴露 |

### 9.2 数据安全

- 所有数据存储在本地
- 不上传到云端
- LLM 调用时脱敏（电话、邮箱部分隐藏）

---

## 十、与其他模块的交互

### 10.1 与照片模块

```
照片入库
    │
    ▼
人脸识别
    │
    ▼
匹配人物
    │
    ├── 匹配到已关联联系人 → 更新图谱关系
    │
    └── 未匹配/未关联 → 提示用户关联
```

### 10.2 与文档模块

```
文档入库
    │
    ▼
实体提取
    │
    ▼
发现人名
    │
    ├── 匹配已有联系人 → 创建 MENTIONED_IN 关系
    │
    └── 未匹配 → 创建"待确认人物"节点
```

### 10.3 与便签模块

```
便签创建
    │
    ▼
实体提取
    │
    ▼
发现人名
    │
    └── 匹配已有联系人 → 创建关联
```

---

## 十一、代码量估算

| 组件 | 代码量 |
|------|--------|
| 联系人数据模型 | ~300 行 |
| vCard 导入导出 | ~400 行 |
| CSV 导入 | ~300 行 |
| 去重算法 | ~200 行 |
| 人脸关联 | ~300 行 |
| 图谱集成 | ~200 行 |
| 搜索功能 | ~200 行 |
| UI 组件 | ~600 行 |
| **总计** | **~2,500 行** |

---

## 十二、参考资料

### 开源项目

- **Frappe Contact** - 联系人数据模型参考
- **OCA Partner Deduplicate** - 去重算法参考
- **vobject** - vCard 解析库

### 技术文档

- [vCard 规范 (RFC 6350)](https://tools.ietf.org/html/rfc6350)
- [SQLite FTS5](https://www.sqlite.org/fts5.html)

---

*文档结束*
