# 照片处理流程 - 详细设计

> 版本：v1.0
> 日期：2026-03-22
> 状态：详细设计完成

---

## 一、设计理念

### 1.1 核心理念

**人脸识别的目的：建立人物关系，而非存储人脸向量。**

| 系统 | 目的 | 数据 |
|------|------|------|
| **人脸识别** | 确定照片里是谁 | 人脸特征（临时使用）|
| **向量搜索** | 搜索人物/照片 | 人物名/照片描述向量 |

### 1.2 关键设计决策

| 决策 | 说明 |
|------|------|
| **人脸识别模型按需加载** | 不常驻内存，处理完即卸载 |
| **不存人脸特征向量** | 用于匹配，匹配完即可丢弃 |
| **存人物名向量** | 用于搜索"张三的照片" |
| **人物合并学习机制** | 用户反馈 → 调整阈值 |

---

## 二、处理流程

### 2.1 整体流程

```
照片入库
    │
    ├── 1. EXIF 提取
    │   ├── 拍摄时间
    │   ├── GPS 位置（如有）
    │   └── 相机信息
    │
    ├── 2. 人脸识别（InsightFace，按需加载）
    │   ├── 检测人脸
    │   ├── 提取特征
    │   ├── 匹配/创建人物
    │   └── 输出：[张三, 李四]
    │
    ├── 3. L0/L1 生成
    │   ├── L0: "张三、李四合影，2026-03-22"
    │   └── L1: 人物列表、拍摄信息
    │
    ├── 4. 向量化
    │   ├── L0 文本向量
    │   └── 图像 CLIP 向量
    │
    └── 5. 图谱更新
        ├── 创建照片节点
        └── 创建人物关系 + 同框关系
```

### 2.2 人脸识别流程

```
加载 InsightFace 模型（按需）
    │
    ▼
检测人脸位置
    │
    ▼
提取人脸特征（128 维向量）
    │
    ▼
与已有人物匹配
    │
    ├── 匹配成功 → 更新人物信息
    │
    └── 未匹配 → 创建"未命名人物_N"
    │
    ▼
释放模型内存
    │
    ▼
返回人物 ID 列表
```

---

## 三、人物匹配机制

### 3.1 匹配算法

```python
def match_face(new_embedding, persons, threshold=0.7):
    """
    匹配人脸到已有人物
    
    Args:
        new_embedding: 新人脸特征向量
        persons: 已有人物列表（含中心向量）
        threshold: 匹配阈值
    
    Returns:
        person_id 或 None（新人物）
    """
    best_match = None
    best_similarity = 0
    
    for person in persons:
        similarity = cosine_similarity(new_embedding, person.center_embedding)
        
        # 调整后的阈值（学习机制）
        adjusted_threshold = threshold - person.threshold_adjustment
        
        if similarity > adjusted_threshold and similarity > best_similarity:
            best_match = person.id
            best_similarity = similarity
    
    return best_match
```

### 3.2 人物中心向量

```python
def update_center_embedding(person, new_embedding):
    """
    更新人物中心向量（平均值）
    """
    # 获取该人物所有人脸嵌入
    all_embeddings = person.face_embeddings + [new_embedding]
    
    # 计算中心向量
    person.center_embedding = np.mean(all_embeddings, axis=0)
    
    # 存储新嵌入
    person.face_embeddings.append(new_embedding)
```

---

## 四、人物合并与学习机制

### 4.1 合并流程

```
用户说：人物A 和 人物B 是同一人
    │
    ├── 1. 合并人物信息
    │   ├── 保留用户指定的名称
    │   ├── 合并所有照片关联
    │   └── 合并图谱关系
    │
    ├── 2. 合并人脸特征
    │   ├── 合并所有嵌入向量
    │   └── 重新计算中心向量
    │
    └── 3. 学习：调整阈值
        └── 降低匹配门槛，避免再次分开
```

### 4.2 学习机制

```python
def merge_persons(person_a, person_b):
    """
    合并两个人物，并学习调整
    """
    # 1. 计算原始相似度
    original_similarity = cosine_similarity(
        person_a.center_embedding,
        person_b.center_embedding
    )
    
    # 2. 合并人物
    merged = Person()
    merged.name = person_a.name  # 保留A的名称
    merged.face_embeddings = person_a.face_embeddings + person_b.face_embeddings
    merged.center_embedding = np.mean(merged.face_embeddings, axis=0)
    
    # 3. 学习：计算阈值调整
    # 原本相似度 0.65 被判定为不同人
    # 现在应该判定为同一人，阈值需要降低
    threshold_adjustment = (0.7 - original_similarity) + 0.05
    
    # 4. 应用调整
    merged.threshold_adjustment = threshold_adjustment
    
    # 5. 更新数据库
    db.merge_person(person_a.id, person_b.id, merged)
    
    return merged
```

### 4.3 效果

| 情况 | 处理前 | 处理后 |
|------|--------|--------|
| 相似度 0.65 | 判定为不同人 | 判定为同一人 |
| 阈值 | 0.7 | 0.6（针对这类人脸）|

**系统会越来越准确。**

---

## 五、L0/L1 生成

### 5.1 L0：照片摘要（~100 tokens）

```markdown
张三、李四合影，2026-03-22，北京朝阳区。共2人，户外场景。
```

### 5.2 L1：照片概览（~2k tokens）

```markdown
# 照片概览

## 基本信息
- 拍摄时间：2026-03-22 14:30:00
- 拍摄地点：北京市朝阳区三里屯
- 相机：iPhone 15 Pro
- 尺寸：4032×3024

## 人物信息
| 姓名 | 置信度 | 位置 |
|------|--------|------|
| 张三 | 98% | 左侧 |
| 李四 | 95% | 右侧 |

## 场景分析
- 场景类型：户外、街景
- 光线：自然光
- 天气：晴

## 关联信息
- 相关文档：无
- 相关便签：无

## 元信息
- 文件名：IMG_20260322_001.jpg
- 入库时间：2026-03-22 15:00
- 文件大小：2.3 MB
```

---

## 六、同框关系建立

### 6.1 关系类型

| 关系 | 说明 | 强度 |
|------|------|------|
| **APPEARS_IN** | 人物出现在照片中 | 1.0 |
| **CO_OCCURS_WITH** | 同框关系 | 共现次数 |

### 6.2 同框关系更新

```python
def update_co_occurrence(photo, persons):
    """
    更新同框关系
    """
    for i, person_a in enumerate(persons):
        for person_b in persons[i+1:]:
            # 检查是否已有关系
            relation = db.get_relation(person_a, person_b, "CO_OCCURS_WITH")
            
            if relation:
                # 更新强度
                relation.strength += 1
                relation.last_seen = photo.taken_at
            else:
                # 创建新关系
                db.create_relation(
                    from_person=person_a,
                    to_person=person_b,
                    type="CO_OCCURS_WITH",
                    strength=1,
                    first_seen=photo.taken_at,
                    last_seen=photo.taken_at
                )
```

---

## 七、按需加载模型

### 7.1 设计原则

**人脸识别模型不常驻内存，处理完即卸载。**

```python
class FaceRecognitionService:
    _model = None
    
    @classmethod
    def get_model(cls):
        """按需加载模型"""
        if cls._model is None:
            from insightface.app import FaceAnalysis
            cls._model = FaceAnalysis(name='buffalo_l')
            cls._model.prepare(ctx_id=0)
        return cls._model
    
    @classmethod
    def unload_model(cls):
        """卸载模型"""
        cls._model = None
        import gc
        gc.collect()
    
    @classmethod
    def process_photo(cls, photo_path):
        """处理照片"""
        model = cls.get_model()
        # 处理逻辑...
        return results
    
    @classmethod
    def process_batch(cls, photo_paths):
        """批量处理照片"""
        model = cls.get_model()
        results = []
        for path in photo_paths:
            results.append(cls.process_photo(path))
        cls.unload_model()  # 批量处理完后卸载
        return results
```

---

## 八、存储结构

### 8.1 人物表

```sql
CREATE TABLE persons (
    id TEXT PRIMARY KEY,
    name TEXT,                      -- 用户命名
    auto_label TEXT,                -- 自动编号（未命名人物_N）
    center_embedding BLOB,          -- 中心向量（可选存储）
    threshold_adjustment REAL,      -- 阈值调整值
    photo_count INTEGER,            -- 出现次数
    first_seen DATETIME,
    last_seen DATETIME,
    created_at DATETIME
);
```

### 8.2 照片表

```sql
CREATE TABLE photos (
    id TEXT PRIMARY KEY,
    file_path TEXT,
    taken_at DATETIME,              -- EXIF 拍摄时间
    location TEXT,                  -- GPS 位置
    camera TEXT,                    -- 相机信息
    
    -- L0/L1
    abstract TEXT,                  -- L0 摘要
    overview TEXT,                  -- L1 概览
    
    -- 向量
    vector_id TEXT,                 -- 向量库ID
    
    ingested_at DATETIME
);
```

### 8.3 人脸表

```sql
CREATE TABLE faces (
    id TEXT PRIMARY KEY,
    photo_id TEXT,
    person_id TEXT,
    embedding BLOB,                 -- 人脸嵌入向量
    bounding_box TEXT,              -- 人脸位置
    confidence REAL,                -- 置信度
    
    FOREIGN KEY (photo_id) REFERENCES photos(id),
    FOREIGN KEY (person_id) REFERENCES persons(id)
);
```

---

## 九、未命名人物管理

### 9.1 问题背景

照片入库后检测到未识别人物，系统会自动创建"未命名人物_N"。但如果用户长期不处理：
- 未命名人物会无限累积
- 可能出现多个"未命名人物"实际是同一个人
- 需要自动聚类和批量处理机制

### 9.2 自动聚类策略

**定期后台任务**：每周检查未命名人物，尝试自动合并。

```
后台聚类任务（每周执行）
    │
    ▼
获取所有未命名人物
    │
    ▼
计算两两相似度
    │
    ├── 相似度 > 0.85 → 自动合并
    │   └── 保留出现次数多的为主
    │
    ├── 相似度 0.70-0.85 → 加入"待确认"队列
    │   └── 用户确认后合并
    │
    └── 相似度 < 0.70 → 不处理
    │
    ▼
更新图谱关系
```

### 9.3 相似度计算

```python
def calculate_person_similarity(person_a: Person, person_b: Person) -> float:
    """计算两个未命名人物的相似度"""
    
    # 1. 人脸特征相似度（主要依据）
    face_sim = cosine_similarity(person_a.center_embedding, person_b.center_embedding)
    
    # 2. 时间共现（辅助）
    # 如果两个人物经常在同一时间段出现，可能是同一人
    time_overlap = calculate_time_overlap(person_a.photo_times, person_b.photo_times)
    
    # 3. 地点共现（辅助）
    location_overlap = calculate_location_overlap(person_a.photo_locations, person_b.photo_locations)
    
    # 加权平均
    # 人脸相似度权重最高
    score = face_sim * 0.8 + time_overlap * 0.1 + location_overlap * 0.1
    
    return score
```

### 9.4 合并阈值

| 阈值 | 动作 | 说明 |
|------|------|------|
| **> 0.85** | 自动合并 | 高置信度，无需用户确认 |
| **0.70 - 0.85** | 加入待确认队列 | 需要用户确认 |
| **< 0.70** | 不处理 | 不太可能是同一人 |

### 9.5 待确认队列 UI

```
┌─────────────────────────────────────────────┐
│  💡 发现可能的重复人物                       │
├─────────────────────────────────────────────┤
│                                             │
│  未命名人物_01 (15张照片)                   │
│  未命名人物_03 (8张照片)                    │
│  相似度: 82%                                │
│                                             │
│  [合并] [不是同一人] [稍后]                 │
│                                             │
├─────────────────────────────────────────────┤
│  未命名人物_02 (3张照片)                    │
│  未命名人物_05 (2张照片)                    │
│  相似度: 76%                                │
│                                             │
│  [合并] [不是同一人] [稍后]                 │
│                                             │
└─────────────────────────────────────────────┘
```

### 9.6 批量处理 UI

```
┌─────────────────────────────────────────────┐
│  未命名人物管理                             │
├─────────────────────────────────────────────┤
│                                             │
│  共有 12 个未命名人物，涉及 45 张照片        │
│                                             │
│  ┌─────────────────────────────────────┐    │
│  │ 未命名人物_01    15张照片    [命名]  │    │
│  │ 未命名人物_02    8张照片     [命名]  │    │
│  │ 未命名人物_03    6张照片     [命名]  │    │
│  │ ...                                 │    │
│  └─────────────────────────────────────┘    │
│                                             │
│  [批量命名] [自动合并相似人物]              │
│                                             │
└─────────────────────────────────────────────┘
```

### 9.7 合并学习机制

当用户确认两个未命名人物是同一人时：

```python
def learn_from_merge(person_a: Person, person_b: Person, db):
    """从用户确认中学习"""
    
    # 计算原始相似度
    original_sim = calculate_person_similarity(person_a, person_b)
    
    # 记录阈值调整
    # 如果用户确认 82% 相似度的合并，说明阈值可以降低
    threshold_adjustment = (0.85 - original_sim) + 0.05
    
    # 存储调整值
    db.execute("""
        UPDATE persons 
        SET threshold_adjustment = ?
        WHERE id = ?
    """, threshold_adjustment, merged_person.id)
    
    # 后续匹配时使用调整后的阈值
    # effective_threshold = 0.85 - threshold_adjustment
```

---

*文档结束*
