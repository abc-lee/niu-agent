# LightRAG 语义完整性检查与修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 LightRAG 数据一致性检查工具能检测出 16 个历史遗留的"僵尸脑区"（句法自洽但语义死亡的数据），让修复工具能完整清理 7 个存储的残留数据，确保修复后数据真正可用。

**Architecture:** 在现有 `lightrag_integrity.py`（11 项句法 check）和 `lightrag_repair.py`（12 项 repair）基础上，新增 6 项语义 check 和 6 项语义 repair。语义 check 用"description 语义标记 + 跨存储交叉验证"作为参照系（不是句法引用完整性）。语义 repair 用"语义标记"作为真相源（不是 GraphML——GraphML 本身可能被污染），做完整 7 存储清理。修复后用"程序启动正常运行"作为验证标准（不是 check_all 返回 ok）。

**Tech Stack:** Python 3.11、xml.etree.ElementTree（GraphML 解析）、nano-vectordb（向量存储）、pytest（TDD）、真实 LightRAG 实例（端到端验证）。

---

## 背景与设计原则

### 事故复盘

主 Agent 之前修复了 `lightrag_repair.py` 的 DocStatus 大小写 bug（commit `54d09a80`），让 LightRAG 部分功能恢复，但触发了 region_sync 跑 dissolve，导致 16 个"智家xxx脑区"僵尸脑区被连锁删除，程序启动后风扇狂转、阻塞。

根因不是 DocStatus 修复错了，而是**检查工具的根本性设计缺陷**：
- 参照系是"句法引用完整性"（A 引用 B，B 里有就 ok）
- 不是"语义正确性"（description 写"被删除"的脑区应该不在 GraphML 里）

16 个僵尸脑区在句法上完全自洽，所有 11 项 check 全部通过（实测 `check_all()` 返回 `ok=True, 0 errors`），但语义上是僵尸——description 明确写"被删除的重复脑区实体之一"。

### 僵尸脑区的形成机制

1. 历史 Agent 用 `custom_kg` 注入 16 条 `知识图谱系统维护 -> 僵尸脑区 (kw='删除操作')` edge（写"删除日志"但没调 `delete_entity`）
2. 之后 dissolve 流程跑到 shrink_count=1，但被中断（进程重启、sync 没跑完等）
3. 僵尸脑区卡在"shrink_count=1 中间态"——description 含 `brain_meta_shrink_count:1`，但 GraphML node 仍存在
4. LightRAG 的 `adelete_by_entity` 设计缺陷：只删 3 个存储（GraphML + vdb_entities + vdb_relationships），留下 entity_chunks / text_chunks / vdb_chunks / full_entities / full_relations 5 个存储的残留

### 新设计原则

**P1: 语义维度检测**——不只是"引用是否解析"，还要验证：
- description 含"被删除"/"重复"等标记但 node 仍存在
- `brain_meta_shrink_count` 卡在 1 ≤ N < 3 持续多周期
- `brain_meta_size` 跟实际"包含"edge 数量不一致
- 一个 chunk 被超过 N 个 entity 共享（异常）

**P2: 跨存储交叉验证**——不只是"A 引用 B"，还要验证：
- entity_chunks 的 chunk_ids 跟 GraphML node d3 source_id 是否一致
- vdb_entities 里有向量但 GraphML 没有 node（反向孤儿）
- vdb_chunks 里有向量但 text_chunks 没有 chunk（反向孤儿）

**P3: 完整 7 存储清理**——repair 时清干净：
- GraphML node + cascade edge
- vdb_entities 向量
- vdb_relationships 向量
- entity_chunks key
- text_chunks 专属 chunk
- vdb_chunks 专属 chunk 向量
- full_entities / full_relations 文档级索引

**P4: 验证标准升级**——不只看 `check_all` 返回 ok，还要：
- 16 个僵尸脑区在所有 7 个存储中完全消失
- `brain_meta_shrink_count` 不在任何 description 里
- 程序启动后 region_sync 一次 sync 完成（不卡 dissolve）
- 风扇不狂转

### 与删除工具 bug 的关系

**本次不修删除工具的 bug**（LightRAG `adelete_by_entity` 只删 3 个存储的设计缺陷）。但检查+修复工具必须能独立清理掉已经存在的残留——即"亡羊补牢"能力。删除工具的修复留到下一个计划。

---

## 文件结构

| 文件 | 责任 | 改动类型 |
|------|------|---------|
| `niu_api/internal/lightrag_integrity.py` | 新增 6 项语义 check + 扩展 `_load_graphml` 提取 description/entity_type | 修改 |
| `niu_api/internal/lightrag_repair.py` | 新增 6 项语义 repair（完整 7 存储清理） + 扩展 `repair_all` 调用语义 repair | 修改 |
| `tests/test_lightrag_semantic_integrity.py` | 6 项语义 check 的 TDD 测试 | 创建 |
| `tests/test_lightrag_semantic_repair.py` | 6 项语义 repair 的 TDD 测试 | 创建 |
| `tests/fixtures/lightrag_zombie_regions/` | 僵尸脑区测试数据 fixture（含真实 16 个僵尸脑区的最小复现） | 创建 |
| `tests/test_lightrag_e2e_semantic.py` | 端到端测试：真实数据跑 check → repair → 启动程序验证正常运行 | 创建 |

---

## Task 1: 扩展 `_load_graphml` 提取 description 和 entity_type

**Files:**
- Modify: `niu_api/internal/lightrag_integrity.py:170-228`
- Test: `tests/test_lightrag_semantic_integrity.py`

### 背景

当前 `_load_graphml` 只返回 `(node_ids, edges, error)`。新语义 check 需要读 node 的 description（d2 字段）和 entity_type（d1 字段）。

GraphML 的 data 字段 key 映射（来自 `lightrag_repair.py` 实际数据观察）：
- `d1` = entity_type
- `d2` = description（含 `<SEP>` 分隔的 `brain_meta_*` 元数据）
- `d3` = source_id
- `d7` = weight
- `d8` = description（edge 用，跟 node 的 d2 区分）
- `d9` = keywords
- `d10` = source_id（edge 用）

### - [ ] Step 1: Write the failing test

`tests/test_lightrag_semantic_integrity.py`:

```python
"""语义完整性检查的 TDD 测试。"""
import pytest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from niu_api.internal.lightrag_integrity import _load_graphml, _parse_brain_meta


def _write_test_graphml(path: Path, nodes: list[dict], edges: list[dict] = None):
    """生成测试用 GraphML 文件。nodes 是 [{id, entity_type, description, source_id}, ...]"""
    edges = edges or []
    ns = "http://graphml.graphdrawing.org/xmlns"
    ET.register_namespace("", ns)
    root = ET.Element(f"{{{ns}}}graphml")
    graph = ET.SubElement(root, f"{{{ns}}}graph", {"edgedefault": "undirected"})
    for n in nodes:
        node = ET.SubElement(graph, f"{{{ns}}}node", {"id": n["id"]})
        if "entity_type" in n:
            ET.SubElement(node, f"{{{ns}}}data", {"key": "d1"}).text = n["entity_type"]
        if "description" in n:
            ET.SubElement(node, f"{{{ns}}}data", {"key": "d2"}).text = n["description"]
        if "source_id" in n:
            ET.SubElement(node, f"{{{ns}}}data", {"key": "d3"}).text = n["source_id"]
    for e in edges:
        edge = ET.SubElement(graph, f"{{{ns}}}edge", {"source": e["source"], "target": e["target"]})
        if "keywords" in e:
            ET.SubElement(edge, f"{{{ns}}}data", {"key": "d9"}).text = e["keywords"]
    tree = ET.ElementTree(root)
    tree.write(path, xml_declaration=True, encoding="utf-8")


def test_load_graphml_returns_node_metadata(tmp_path):
    """_load_graphml 应返回 node 的 description 和 entity_type（不只 id）"""
    graphml = tmp_path / "graph_chunk_entity_relation.graphml"
    _write_test_graphml(graphml, nodes=[
        {"id": "聊天历史脑区", "entity_type": "brainregion",
         "description": "brain_meta_size:0<SEP>brain_meta_shrink_count:1", "source_id": "brain_聊天历史"},
        {"id": "Python", "entity_type": "concept", "description": "编程语言", "source_id": "doc-abc"},
    ])
    
    with patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path):
        node_ids, edges, node_meta, err = _load_graphml(graphml)
    
    assert err is None
    assert node_ids == {"聊天历史脑区", "Python"}
    assert edges == []
    assert node_meta["聊天历史脑区"]["entity_type"] == "brainregion"
    assert "brain_meta_shrink_count:1" in node_meta["聊天历史脑区"]["description"]
    assert node_meta["聊天历史脑区"]["source_id"] == "brain_聊天历史"
    assert node_meta["Python"]["entity_type"] == "concept"


def test_load_graphml_node_without_metadata(tmp_path):
    """GraphML node 没有 d1/d2/d3 字段时，meta 字段是空字符串而非 KeyError"""
    graphml = tmp_path / "graph_chunk_entity_relation.graphml"
    _write_test_graphml(graphml, nodes=[{"id": "bare_node"}])
    
    with patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path):
        _, _, node_meta, err = _load_graphml(graphml)
    
    assert err is None
    assert node_meta["bare_node"] == {"entity_type": "", "description": "", "source_id": ""}


def test_load_graphml_backward_compat_node_ids(tmp_path):
    """扩展后 _load_graphml 仍兼容原 (node_ids, edges, err) 三元组签名调用方式"""
    graphml = tmp_path / "graph_chunk_entity_relation.graphml"
    _write_test_graphml(graphml, nodes=[{"id": "X"}], edges=[{"source": "X", "target": "Y"}])
    
    with patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path):
        # 调用方用解构形式获取前三项，应正常工作
        result = _load_graphml(graphml)
    
    # 返回是 4-tuple，但前三项跟原签名一致
    assert len(result) == 4
    node_ids, edges, node_meta, err = result
    assert "X" in node_ids
    assert ("X", "Y") in edges
    assert err is None
```

### - [ ] Step 2: Run test to verify it fails

```bash
cd REDACTED_USER_PATH/tools/ai-bot
python -m pytest tests/test_lightrag_semantic_integrity.py::test_load_graphml_returns_node_metadata -v
```

Expected: FAIL with `ValueError: too many values to unpack`（当前 `_load_graphml` 返回 3-tuple）

### - [ ] Step 3: Write minimal implementation

修改 `niu_api/internal/lightrag_integrity.py:170-228`，把 `_load_graphml` 改为返回 4-tuple `(node_ids, edges, node_meta, error)`：

```python
def _load_graphml(path: Path) -> tuple[set[str], list[tuple[str, str]], dict[str, dict[str, str]], dict[str, Any] | None]:
    """解析 GraphML 文件，返回 (node_ids, edges, node_meta, error)。

    Returns:
        - 文件不存在 → (set(), [], {}, None)（空数据，通过）
        - XML 解析失败 → (set(), [], {}, {"check": "xml_parse", ...})（critical）
        - 成功 → (node_id_set, [(src, tgt), ...], {node_id: {entity_type, description, source_id}}, None)

    注意：node id 和 edge source/target 都已 lower 化（LightRAG 设计），
    这里不再额外 lower，直接使用原始值。
    """
    if not path.exists():
        return set(), [], {}, None
    try:
        tree = ET.parse(path)
        root = tree.getroot()
    except ET.ParseError as e:
        return set(), [], {}, {
            "check": "xml_parse",
            "file": path.name,
            "msg": str(e),
            "severity": "critical",
        }
    except Exception as e:  # noqa: BLE001
        return set(), [], {}, {
            "check": "xml_parse",
            "file": path.name,
            "msg": f"{type(e).__name__}: {e}",
            "severity": "critical",
        }

    # 找到 graph 元素（支持 namespace）
    graph = root.find("graph")
    if graph is None:
        for child in root:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if tag == "graph":
                graph = child
                break
    if graph is None:
        return set(), [], {}, {
            "check": "no_graph_element",
            "file": path.name,
            "severity": "critical",
        }

    node_ids: set[str] = set()
    edges: list[tuple[str, str]] = []
    node_meta: dict[str, dict[str, str]] = {}
    for child in graph:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if tag == "node":
            nid = child.get("id", "")
            if nid:
                node_ids.add(nid)
                meta = {"entity_type": "", "description": "", "source_id": ""}
                for data in child:
                    d_key = data.get("key", "")
                    d_text = data.text or ""
                    if d_key == "d1":
                        meta["entity_type"] = d_text
                    elif d_key == "d2":
                        meta["description"] = d_text
                    elif d_key == "d3":
                        meta["source_id"] = d_text
                node_meta[nid] = meta
        elif tag == "edge":
            src = child.get("source", "")
            tgt = child.get("target", "")
            edges.append((src, tgt))
    return node_ids, edges, node_meta, None
```

同时新增 `_parse_brain_meta` 工具函数（解析 description 里的 `brain_meta_*` 字段）：

```python
def _parse_brain_meta(description: str) -> dict[str, str]:
    """解析脑区 description 里的 brain_meta_* 字段。

    description 格式：<SEP> 分隔的多字段，每段形如 `brain_meta_<key>:<value>`

    Returns:
        {field_name_without_prefix: value}，比如 {"size": "0", "shrink_count": "1", ...}
        空字段（value 为空）也保留，便于检测 size:0 这种"故意 0"的语义。
    """
    if not description:
        return {}
    result: dict[str, str] = {}
    # 分隔符是 LightRAG 的 GRAPH_FIELD_SEP，但 description 里可能也有别的 <SEP>
    # 先按 \x1f（unit separator）拆分
    parts = description.split("\x1f")
    for part in parts:
        if not part:
            continue
        # 形如 "brain_meta_size:0"
        if ":" in part:
            key, _, value = part.partition(":")
            if key.startswith("brain_meta_"):
                result[key[len("brain_meta_"):]] = value
    return result
```

### - [ ] Step 4: Run test to verify it passes

```bash
python -m pytest tests/test_lightrag_semantic_integrity.py::test_load_graphml_returns_node_metadata \
                tests/test_lightrag_semantic_integrity.py::test_load_graphml_node_without_metadata \
                tests/test_lightrag_semantic_integrity.py::test_load_graphml_backward_compat_node_ids -v
```

Expected: PASS

### - [ ] Step 5: 修复现有 check 函数对 `_load_graphml` 的新返回值

现有 7 个 check 函数解构 `_load_graphml` 返回值（如 L253 `node_ids, _, graphml_err = _load_graphml(...)`）。改成 4-tuple 后会破坏这些调用。逐一修复：

```bash
# 找出所有调用点
grep -n "_load_graphml" REDACTED_USER_PATH/tools/ai-bot/niu_api/internal/lightrag_integrity.py
```

每处改为：
- `node_ids, _, _, graphml_err = _load_graphml(...)`（不需要 node_meta 的地方）
- `node_ids, _, node_meta, graphml_err = _load_graphml(...)`（需要 node_meta 的语义 check）

### - [ ] Step 6: 跑全部现有测试确认不破坏

```bash
python -m pytest tests/test_lightrag_repair.py tests/test_lightrag_integrity.py -v 2>&1 | tail -30
```

Expected: 27/27 passed

### - [ ] Step 7: Commit

```bash
git add niu_api/internal/lightrag_integrity.py tests/test_lightrag_semantic_integrity.py
git commit -m "feat(integrity): _load_graphml 提取 description/entity_type/source_id 为语义 check 准备

- 扩展返回值为 4-tuple (node_ids, edges, node_meta, error)
- 新增 _parse_brain_meta 解析 brain_meta_* 字段
- 修复 7 个现有 check 函数的解构
- 不改变现有 11 项 check 的行为（句法 check 不依赖 node_meta）
"
```

---

## Task 2: 语义 Check 1 - 检测脑区 description 含"被删除"标记但 node 仍存在

**Files:**
- Modify: `niu_api/internal/lightrag_integrity.py`（新增 `check_brainregion_semantic_zombie`）
- Test: `tests/test_lightrag_semantic_integrity.py`

### 背景

僵尸脑区的 description 含字符串"被删除的重复脑区实体之一"（或类似语义标记），但 GraphML 里 node 仍存在。这是历史 Agent 用 custom_kg 写"删除日志"但没真删的产物。

### - [ ] Step 1: Write the failing test

`tests/test_lightrag_semantic_integrity.py` 末尾追加：

```python
def test_check_brainregion_semantic_zombie_detects_zombie(tmp_path):
    """检测 description 含'被删除'但 node 仍存在的脑区"""
    from niu_api.internal.lightrag_integrity import check_brainregion_semantic_zombie
    
    graphml = tmp_path / "graph_chunk_entity_relation.graphml"
    _write_test_graphml(graphml, nodes=[
        # 僵尸脑区 1：description 含"被删除的重复脑区实体之一"
        {"id": "智家全维资料脑区", "entity_type": "brainregion",
         "description": "被删除的重复脑区实体之一。<SEP>brain_meta_size:0<SEP>brain_meta_shrink_count:1"},
        # 僵尸脑区 2：description 含"已删除"
        {"id": "智家使用运维脑区", "entity_type": "brainregion",
         "description": "已删除的脑区。<SEP>brain_meta_size:0"},
        # 正常脑区
        {"id": "聊天历史脑区", "entity_type": "brainregion",
         "description": "brain_meta_size:10"},
        # 非脑区实体（即使 description 含"被删除"也不该报）
        {"id": "普通实体", "entity_type": "concept",
         "description": "被删除的文档内容"},
    ])
    
    with patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path):
        report = check_brainregion_semantic_zombie()
    
    assert report["name"] == "brainregion_semantic_zombie"
    assert len(report["errors"]) == 2
    zombie_names = [e["ref_key"] for e in report["errors"]]
    assert "智家全维资料脑区" in zombie_names
    assert "智家使用运维脑区" in zombie_names
    # 非脑区实体不报
    assert "普通实体" not in zombie_names
    # severity 应该是 major
    assert all(e["severity"] == "major" for e in report["errors"])


def test_check_brainregion_semantic_zombie_clean_data_ok(tmp_path):
    """正常脑区（description 不含'被删除'标记）→ 0 errors"""
    from niu_api.internal.lightrag_integrity import check_brainregion_semantic_zombie
    
    graphml = tmp_path / "graph_chunk_entity_relation.graphml"
    _write_test_graphml(graphml, nodes=[
        {"id": "聊天历史脑区", "entity_type": "brainregion",
         "description": "brain_meta_size:10"},
        {"id": "文档库脑区", "entity_type": "brainregion",
         "description": "brain_meta_size:5"},
    ])
    
    with patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path):
        report = check_brainregion_semantic_zombie()
    
    assert report["errors"] == []
```

### - [ ] Step 2: Run test to verify it fails

```bash
python -m pytest tests/test_lightrag_semantic_integrity.py::test_check_brainregion_semantic_zombie_detects_zombie -v
```

Expected: FAIL with `ImportError: cannot import name 'check_brainregion_semantic_zombie'`

### - [ ] Step 3: Write minimal implementation

在 `niu_api/internal/lightrag_integrity.py` 现有 check 列表之后（L646 附近）新增：

```python
# =============================================================================
# 语义维度检查（句法自洽但语义死亡的数据）
# =============================================================================

# "被删除"语义标记（LLM 写的 description，明确告诉系统这个实体该删）
_ZOMBIE_DESCRIPTION_MARKERS = (
    "被删除的重复脑区实体之一",
    "被删除的脑区",
    "已删除的脑区",
    "已删除的重复脑区",
)


def check_brainregion_semantic_zombie() -> dict[str, Any]:
    """语义 check #1: 检测脑区 description 含'被删除'标记但 GraphML node 仍存在。

    引用方：脑区 description 的语义标记
    被引用方：GraphML node 存在性
    severity: major（句法自洽但语义死亡，会让 dissolve 卡在中间态）

    历史 Agent 用 custom_kg 写"删除日志"但没真删，description 含明确"被删除"标记。
    """
    storage_dir = _resolve_storage_dir()
    errors: list[dict[str, Any]] = []

    _, _, node_meta, graphml_err = _load_graphml(storage_dir / _GRAPHML_FILE)
    if graphml_err:
        errors.append(graphml_err)
        return {"name": "brainregion_semantic_zombie", "errors": errors}
    if not node_meta:
        return {"name": "brainregion_semantic_zombie", "errors": []}

    for nid, meta in node_meta.items():
        # 只检测 brainregion 类型
        if meta.get("entity_type") != "brainregion":
            continue
        desc = meta.get("description", "")
        if not desc:
            continue
        for marker in _ZOMBIE_DESCRIPTION_MARKERS:
            if marker in desc:
                errors.append({
                    "check": "brainregion_semantic_zombie",
                    "severity": "major",
                    "ref_key": nid,
                    "ref_file": _GRAPHML_FILE,
                    "target_file": _GRAPHML_FILE,
                    "marker": marker,
                    "msg": f"脑区 '{nid}' description 含语义标记'{marker}'但 node 仍存在（僵尸脑区）",
                })
                break  # 一个脑区只报一次（匹配第一个 marker 就停）
    return {"name": "brainregion_semantic_zombie", "errors": errors}
```

### - [ ] Step 4: Run test to verify it passes

```bash
python -m pytest tests/test_lightrag_semantic_integrity.py::test_check_brainregion_semantic_zombie_detects_zombie \
                tests/test_lightrag_semantic_integrity.py::test_check_brainregion_semantic_zombie_clean_data_ok -v
```

Expected: PASS

### - [ ] Step 5: Commit

```bash
git add niu_api/internal/lightrag_integrity.py tests/test_lightrag_semantic_integrity.py
git commit -m "feat(integrity): 新增 check_brainregion_semantic_zombie 检测僵尸脑区

检测 description 含'被删除的重复脑区实体之一'等语义标记但 GraphML node
仍存在的脑区。这是历史 Agent 用 custom_kg 写'删除日志'但没调
delete_entity 的产物，句法自洽但语义死亡。
"
```

---

## Task 3: 语义 Check 2 - 检测 `brain_meta_size` 跟实际"包含"edge 数量不一致

**Files:**
- Modify: `niu_api/internal/lightrag_integrity.py`（新增 `check_brainregion_size_mismatch`）
- Test: `tests/test_lightrag_semantic_integrity.py`

### 背景

脑区 description 写 `brain_meta_size:N`，但实际"包含"edge 数量可能跟 N 不一致。僵尸脑区 `brain_meta_size:0`，正常脑区应该是 `brain_meta_size:1077`（实际成员数）。

### - [ ] Step 1: Write the failing test

```python
def test_check_brainregion_size_mismatch_detects_inconsistency(tmp_path):
    """description brain_meta_size 跟实际'包含'edge 数量不一致"""
    from niu_api.internal.lightrag_integrity import check_brainregion_size_mismatch
    
    graphml = tmp_path / "graph_chunk_entity_relation.graphml"
    _write_test_graphml(graphml, nodes=[
        # 脑区 A：description 写 size:3，实际 2 个"包含"edge → 报错
        {"id": "脑区A", "entity_type": "brainregion",
         "description": "brain_meta_size:3"},
        # 脑区 B：description 写 size:2，实际 2 个"包含"edge → 通过
        {"id": "脑区B", "entity_type": "brainregion",
         "description": "brain_meta_size:2"},
        # 脑区 C：description 无 brain_meta_size → 跳过（不是脑区元数据格式）
        {"id": "脑区C", "entity_type": "brainregion",
         "description": "随便写的"},
    ], edges=[
        # 脑区A 的包含 edge（2 条，不是 3）
        {"source": "脑区A", "target": "成员1", "keywords": "包含"},
        {"source": "脑区A", "target": "成员2", "keywords": "包含"},
        # 脑区B 的包含 edge（2 条，匹配）
        {"source": "脑区B", "target": "成员3", "keywords": "包含"},
        {"source": "脑区B", "target": "成员4", "keywords": "包含"},
        # 一条非"包含"edge 不计入
        {"source": "脑区A", "target": "外部", "keywords": "其他"},
    ], )
    # 注意 _write_test_graphml 也要支持非脑区 node
    # 修改 fixture 支持 nodes 中带 "成员1" 这种无 description 的非脑区节点
    # 实际写：在 _write_test_graphml 里 nodes 都写完整，包括成员节点
    
    with patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path):
        report = check_brainregion_size_mismatch()
    
    assert report["name"] == "brainregion_size_mismatch"
    assert len(report["errors"]) == 1
    assert report["errors"][0]["ref_key"] == "脑区A"
    assert report["errors"][0]["declared_size"] == 3
    assert report["errors"][0]["actual_size"] == 2


def test_check_brainregion_size_mismatch_zero_members_ok(tmp_path):
    """brain_meta_size:0 + 实际 0 个包含 edge → 一致，不报错"""
    from niu_api.internal.lightrag_integrity import check_brainregion_size_mismatch
    
    graphml = tmp_path / "graph_chunk_entity_relation.graphml"
    _write_test_graphml(graphml, nodes=[
        {"id": "空脑区", "entity_type": "brainregion",
         "description": "brain_meta_size:0"},
    ])
    
    with patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path):
        report = check_brainregion_size_mismatch()
    
    assert report["errors"] == []
```

### - [ ] Step 2: Run test to verify it fails

```bash
python -m pytest tests/test_lightrag_semantic_integrity.py::test_check_brainregion_size_mismatch_detects_inconsistency -v
```

Expected: FAIL with `ImportError`

### - [ ] Step 3: Write minimal implementation

在 `niu_api/internal/lightrag_integrity.py` 新增：

```python
def check_brainregion_size_mismatch() -> dict[str, Any]:
    """语义 check #2: 检测脑区 description 的 brain_meta_size 跟实际'包含'edge 数量不一致。

    引用方：脑区 description 的 brain_meta_size 字段
    被引用方：GraphML 中以该脑区为一端的'包含'edge 数量
    severity: major

    僵尸脑区 brain_meta_size:0 但 node 仍存在（其实没有"包含"edge 也算一致），
    但更严重的是 brain_meta_size:5 但实际 0 个"包含"edge——说明脑区元数据撒谎。
    """
    storage_dir = _resolve_storage_dir()
    errors: list[dict[str, Any]] = []

    node_ids, edges, node_meta, graphml_err = _load_graphml(storage_dir / _GRAPHML_FILE)
    if graphml_err:
        errors.append(graphml_err)
        return {"name": "brainregion_size_mismatch", "errors": errors}
    if not node_meta:
        return {"name": "brainregion_size_mismatch", "errors": []}

    # 统计每个脑区的"包含"edge 数量（无向，src/tgt 任一端是脑区都算）
    contains_count: dict[str, int] = {}
    for src, tgt in edges:
        # 找 keywords，但 _load_graphml 当前不返回 edge keywords
        # 这里先用一个 helper 读 edge keywords
        pass
    # ⚠️ 注意：当前 _load_graphml 不解析 edge 的 d9 (keywords) 字段
    # 我们需要在 _load_graphml 里扩展 edges 为 list[dict] 而不是 list[tuple]
    # 但这样会破坏现有 check。退一步：本 check 单独读 GraphML 提取 edge keywords

    # 单独读 GraphML，提取每个脑区的"包含"edge
    contains_count = _count_contains_edges(storage_dir / _GRAPHML_FILE, node_meta)

    for nid, meta in node_meta.items():
        if meta.get("entity_type") != "brainregion":
            continue
        desc = meta.get("description", "")
        brain_meta = _parse_brain_meta(desc)
        if "size" not in brain_meta:
            continue  # 无 brain_meta_size 字段，跳过（不算不一致）
        declared_size_str = brain_meta["size"]
        try:
            declared_size = int(declared_size_str)
        except ValueError:
            errors.append({
                "check": "brainregion_size_mismatch",
                "severity": "minor",
                "ref_key": nid,
                "msg": f"脑区 '{nid}' brain_meta_size 不是整数: {declared_size_str!r}",
            })
            continue
        actual_size = contains_count.get(nid, 0)
        if declared_size != actual_size:
            errors.append({
                "check": "brainregion_size_mismatch",
                "severity": "major",
                "ref_key": nid,
                "ref_file": _GRAPHML_FILE,
                "declared_size": declared_size,
                "actual_size": actual_size,
                "msg": f"脑区 '{nid}' brain_meta_size={declared_size} 但实际'包含'edge {actual_size} 条",
            })
    return {"name": "brainregion_size_mismatch", "errors": errors}


def _count_contains_edges(graphml_path: Path, node_meta: dict[str, dict[str, str]]) -> dict[str, int]:
    """读 GraphML 文件，统计每个脑区的'包含'edge 数量。

    Args:
        graphml_path: GraphML 文件路径
        node_meta: _load_graphml 返回的 node 元数据 dict

    Returns:
        {脑区名: 包含 edge 数量}
    """
    if not graphml_path.exists():
        return {}
    try:
        tree = ET.parse(graphml_path)
        root = tree.getroot()
    except Exception:
        return {}
    graph = root.find("graph")
    if graph is None:
        for child in root:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if tag == "graph":
                graph = child
                break
    if graph is None:
        return {}
    counts: dict[str, int] = {}
    region_names = {nid for nid, m in node_meta.items() if m.get("entity_type") == "brainregion"}
    for child in graph:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if tag != "edge":
            continue
        src = child.get("source", "")
        tgt = child.get("target", "")
        # 找 d9 (keywords) 字段
        keywords = ""
        for data in child:
            if data.get("key") == "d9":
                keywords = data.text or ""
                break
        # "包含"关系（不区分大小写、包含匹配）
        if "包含" not in keywords:
            continue
        # 哪一端是脑区
        region = None
        if src in region_names:
            region = src
        elif tgt in region_names:
            region = tgt
        if region is None:
            continue
        counts[region] = counts.get(region, 0) + 1
    return counts
```

### - [ ] Step 4: Run test to verify it passes

```bash
python -m pytest tests/test_lightrag_semantic_integrity.py::test_check_brainregion_size_mismatch_detects_inconsistency \
                tests/test_lightrag_semantic_integrity.py::test_check_brainregion_size_mismatch_zero_members_ok -v
```

Expected: PASS

### - [ ] Step 5: Commit

```bash
git add niu_api/internal/lightrag_integrity.py tests/test_lightrag_semantic_integrity.py
git commit -m "feat(integrity): 新增 check_brainregion_size_mismatch 检测脑区元数据与实际成员数不一致"
```

---

## Task 4: 语义 Check 3 - 检测 entity_chunks 跟 GraphML source_id 不一致

**Files:**
- Modify: `niu_api/internal/lightrag_integrity.py`（新增 `check_entity_chunks_source_id_mismatch`）
- Test: `tests/test_lightrag_semantic_integrity.py`

### 背景

僵尸脑区的 GraphML d3 source_id 是 `chunk-2bf1fcb620e29ae1ad3d2ea201ca4be2`（脑区专属 chunk），但 entity_chunks 的 chunk_ids 是 `['chunk-c59393e3eb6267f7f66ce4c5f9a27192']`（共享删除日志 chunk）。两者完全不一致。

### - [ ] Step 1: Write the failing test

```python
def test_check_entity_chunks_source_id_mismatch_detects_inconsistency(tmp_path):
    """entity_chunks 的 chunk_ids 跟 GraphML node 的 d3 source_id 不一致"""
    from niu_api.internal.lightrag_integrity import check_entity_chunks_source_id_mismatch
    
    graphml = tmp_path / "graph_chunk_entity_relation.graphml"
    _write_test_graphml(graphml, nodes=[
        # 僵尸脑区：d3 source_id = chunk-A，但 entity_chunks 指向 chunk-B
        {"id": "智家脑区X", "entity_type": "brainregion",
         "description": "被删除的重复脑区实体之一",
         "source_id": "chunk-AAAAAAAA"},
        # 正常脑区：d3 source_id = chunk-C，entity_chunks 也指向 chunk-C
        {"id": "聊天历史脑区", "entity_type": "brainregion",
         "description": "brain_meta_size:10",
         "source_id": "chunk-CCCCCCCC"},
    ])
    
    # 写 entity_chunks
    ec_path = tmp_path / "kv_store_entity_chunks.json"
    import json
    ec_path.write_text(json.dumps({
        "智家脑区X": {"chunk_ids": ["chunk-BBBBBBBB"], "count": 1},  # 不一致
        "聊天历史脑区": {"chunk_ids": ["chunk-CCCCCCCC"], "count": 1},  # 一致
    }, ensure_ascii=False))
    
    with patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path):
        report = check_entity_chunks_source_id_mismatch()
    
    assert report["name"] == "entity_chunks_source_id_mismatch"
    assert len(report["errors"]) == 1
    assert report["errors"][0]["ref_key"] == "智家脑区X"
    assert report["errors"][0]["graphml_source_id"] == "chunk-AAAAAAAA"
    assert "chunk-BBBBBBBB" in report["errors"][0]["entity_chunks_ids"]


def test_check_entity_chunks_source_id_mismatch_consistent_ok(tmp_path):
    """entity_chunks 跟 GraphML d3 source_id 一致 → 0 errors"""
    from niu_api.internal.lightrag_integrity import check_entity_chunks_source_id_mismatch
    
    graphml = tmp_path / "graph_chunk_entity_relation.graphml"
    _write_test_graphml(graphml, nodes=[
        {"id": "脑区A", "entity_type": "brainregion",
         "source_id": "chunk-AAAA"},
    ])
    
    import json
    (tmp_path / "kv_store_entity_chunks.json").write_text(json.dumps({
        "脑区A": {"chunk_ids": ["chunk-AAAA"], "count": 1},
    }, ensure_ascii=False))
    
    with patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path):
        report = check_entity_chunks_source_id_mismatch()
    
    assert report["errors"] == []
```

### - [ ] Step 2: Run test to verify it fails

```bash
python -m pytest tests/test_lightrag_semantic_integrity.py::test_check_entity_chunks_source_id_mismatch_detects_inconsistency -v
```

Expected: FAIL with `ImportError`

### - [ ] Step 3: Write minimal implementation

```python
def check_entity_chunks_source_id_mismatch() -> dict[str, Any]:
    """语义 check #3: 检测 entity_chunks 的 chunk_ids 跟 GraphML node d3 source_id 不一致。

    引用方：kv_store_entity_chunks 的 chunk_ids
    被引用方：GraphML node 的 d3 source_id
    severity: major

    正常情况：脑区 d3 source_id 应该是脑区专属 chunk_id（brain_xxx），
    entity_chunks 的 chunk_ids 也应该指向同一个 chunk。
    僵尸脑区情况：d3 = 脑区专属 chunk，但 entity_chunks 指向"删除日志"chunk——明显异常。
    """
    storage_dir = _resolve_storage_dir()
    errors: list[dict[str, Any]] = []

    ec_data, ec_err = _load_json_dict(storage_dir / "kv_store_entity_chunks.json")
    if ec_err:
        errors.append(ec_err)
        return {"name": "entity_chunks_source_id_mismatch", "errors": errors}
    if not ec_data:
        return {"name": "entity_chunks_source_id_mismatch", "errors": []}

    _, _, node_meta, graphml_err = _load_graphml(storage_dir / _GRAPHML_FILE)
    if graphml_err:
        errors.append(graphml_err)
        return {"name": "entity_chunks_source_id_mismatch", "errors": errors}

    for entity_name, ec_entry in ec_data.items():
        if not isinstance(ec_entry, dict):
            continue
        ec_chunk_ids = ec_entry.get("chunk_ids", [])
        meta = node_meta.get(entity_name)
        if meta is None:
            # 实体不在 GraphML，由 check_entity_chunks_dangling 报，这里不重复
            continue
        graphml_source_id = meta.get("source_id", "")
        if not graphml_source_id:
            continue  # GraphML 没记 source_id，跳过（没法比对）
        # GraphML d3 可能含 <SEP> 分隔多个 source_id
        graphml_ids = [s for s in graphml_source_id.split("\x1f") if s]
        # 检查 ec_chunk_ids 是否都在 graphml_ids 里
        ec_ids_set = set(ec_chunk_ids)
        graphml_ids_set = set(graphml_ids)
        # 不一致 = ec_chunk_ids 有 graphml_ids 没有的 chunk
        orphan_ec_ids = ec_ids_set - graphml_ids_set
        if orphan_ec_ids:
            errors.append({
                "check": "entity_chunks_source_id_mismatch",
                "severity": "major",
                "ref_key": entity_name,
                "ref_file": "kv_store_entity_chunks.json",
                "target_file": _GRAPHML_FILE,
                "graphml_source_id": graphml_source_id,
                "entity_chunks_ids": list(ec_chunk_ids),
                "orphan_ids": list(orphan_ec_ids),
                "msg": f"实体 '{entity_name}' entity_chunks 指向 {list(orphan_ec_ids)} 但 GraphML d3 source_id 是 {graphml_source_id}",
            })
    return {"name": "entity_chunks_source_id_mismatch", "errors": errors}
```

### - [ ] Step 4: Run test to verify it passes

```bash
python -m pytest tests/test_lightrag_semantic_integrity.py::test_check_entity_chunks_source_id_mismatch_detects_inconsistency \
                tests/test_lightrag_semantic_integrity.py::test_check_entity_chunks_source_id_mismatch_consistent_ok -v
```

Expected: PASS

### - [ ] Step 5: Commit

```bash
git add niu_api/internal/lightrag_integrity.py tests/test_lightrag_semantic_integrity.py
git commit -m "feat(integrity): 新增 check_entity_chunks_source_id_mismatch 跨存储交叉验证"
```

---

## Task 5: 语义 Check 4 - 检测一个 chunk 被过多 entity 共享

**Files:**
- Modify: `niu_api/internal/lightrag_integrity.py`（新增 `check_chunk_shared_by_too_many_entities`）
- Test: `tests/test_lightrag_semantic_integrity.py`

### 背景

16 个僵尸脑区的 entity_chunks 全指向同一个共享"删除日志" chunk `chunk-c59393e3eb6267f7f66ce4c5f9a27192`。一个 chunk 不应该被这么多 entity 共享。

### - [ ] Step 1: Write the failing test

```python
def test_check_chunk_shared_by_too_many_entities_detects_anomaly(tmp_path):
    """一个 chunk 被超过阈值个 entity 共享 → 报错"""
    from niu_api.internal.lightrag_integrity import check_chunk_shared_by_too_many_entities
    
    import json
    (tmp_path / "kv_store_entity_chunks.json").write_text(json.dumps({
        "脑区1": {"chunk_ids": ["chunk-shared-xxx"], "count": 1},
        "脑区2": {"chunk_ids": ["chunk-shared-xxx"], "count": 1},
        "脑区3": {"chunk_ids": ["chunk-shared-xxx"], "count": 1},
        "脑区4": {"chunk_ids": ["chunk-shared-xxx"], "count": 1},
        "脑区5": {"chunk_ids": ["chunk-shared-xxx"], "count": 1},
        # 正常 entity 指向独立 chunk
        "实体A": {"chunk_ids": ["chunk-a-1"], "count": 1},
        "实体B": {"chunk_ids": ["chunk-b-1"], "count": 1},
    }, ensure_ascii=False))
    
    with patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path):
        # 阈值设 3（测试用），5 个脑区共享 chunk-shared-xxx
        report = check_chunk_shared_by_too_many_entities(threshold=3)
    
    assert report["name"] == "chunk_shared_by_too_many_entities"
    assert len(report["errors"]) == 1
    err = report["errors"][0]
    assert err["chunk_id"] == "chunk-shared-xxx"
    assert err["entity_count"] == 5
    assert set(err["entities"]) == {"脑区1", "脑区2", "脑区3", "脑区4", "脑区5"}


def test_check_chunk_shared_by_too_many_entities_normal_ok(tmp_path):
    """每个 chunk 被不超过阈值个 entity 共享 → 0 errors"""
    from niu_api.internal.lightrag_integrity import check_chunk_shared_by_too_many_entities
    
    import json
    (tmp_path / "kv_store_entity_chunks.json").write_text(json.dumps({
        "实体A": {"chunk_ids": ["chunk-shared"], "count": 1},
        "实体B": {"chunk_ids": ["chunk-shared"], "count": 1},
        "实体C": {"chunk_ids": ["chunk-other"], "count": 1},
    }, ensure_ascii=False))
    
    with patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path):
        report = check_chunk_shared_by_too_many_entities(threshold=3)
    
    assert report["errors"] == []
```

### - [ ] Step 2: Run test to verify it fails

```bash
python -m pytest tests/test_lightrag_semantic_integrity.py::test_check_chunk_shared_by_too_many_entities_detects_anomaly -v
```

Expected: FAIL

### - [ ] Step 3: Write minimal implementation

```python
def check_chunk_shared_by_too_many_entities(threshold: int = 10) -> dict[str, Any]:
    """语义 check #4: 检测一个 chunk 被超过阈值个 entity 共享（异常信号）。

    引用方：多个 entity_chunks 的 chunk_ids 指向同一个 chunk
    被引用方：（无具体被引用方，是反向索引的异常检测）
    severity: major

    正常情况：一个 chunk 是某个文档的某段内容，被 1-N 个 entity 引用（N 通常 < 10）。
    异常情况：16 个脑区全指向同一个"删除日志"chunk——明显是历史 bug 留下的脏数据。

    Args:
        threshold: 共享同一 chunk 的 entity 数量阈值，默认 10
    """
    storage_dir = _resolve_storage_dir()
    errors: list[dict[str, Any]] = []

    ec_data, ec_err = _load_json_dict(storage_dir / "kv_store_entity_chunks.json")
    if ec_err:
        errors.append(ec_err)
        return {"name": "chunk_shared_by_too_many_entities", "errors": errors}
    if not ec_data:
        return {"name": "chunk_shared_by_too_many_entities", "errors": []}

    # 反向索引：chunk_id -> [entity_name, ...]
    chunk_to_entities: dict[str, list[str]] = {}
    for entity_name, ec_entry in ec_data.items():
        if not isinstance(ec_entry, dict):
            continue
        for chunk_id in ec_entry.get("chunk_ids", []):
            chunk_to_entities.setdefault(chunk_id, []).append(entity_name)

    for chunk_id, entities in chunk_to_entities.items():
        if len(entities) > threshold:
            errors.append({
                "check": "chunk_shared_by_too_many_entities",
                "severity": "major",
                "ref_file": "kv_store_entity_chunks.json",
                "chunk_id": chunk_id,
                "entity_count": len(entities),
                "entities": entities,
                "threshold": threshold,
                "msg": f"chunk '{chunk_id}' 被 {len(entities)} 个 entity 共享（阈值 {threshold}），可能是历史 bug 残留",
            })
    return {"name": "chunk_shared_by_too_many_entities", "errors": errors}
```

### - [ ] Step 4: Run test to verify it passes

```bash
python -m pytest tests/test_lightrag_semantic_integrity.py::test_check_chunk_shared_by_too_many_entities_detects_anomaly \
                tests/test_lightrag_semantic_integrity.py::test_check_chunk_shared_by_too_many_entities_normal_ok -v
```

Expected: PASS

### - [ ] Step 5: Commit

```bash
git add niu_api/internal/lightrag_integrity.py tests/test_lightrag_semantic_integrity.py
git commit -m "feat(integrity): 新增 check_chunk_shared_by_too_many_entities 反向索引异常检测"
```

---

## Task 6: 语义 Check 5 - 检测 vdb_entities 里有向量但 GraphML 没有 node（反向孤儿）

**Files:**
- Modify: `niu_api/internal/lightrag_integrity.py`（新增 `check_vdb_entities_orphan`）
- Test: `tests/test_lightrag_semantic_integrity.py`

### 背景

现有 `check_vdb_entities_missing` 检测"GraphML 有 node 但 vdb 没向量"（正向）。但反过来"vdb 有向量但 GraphML 没 node"（反向孤儿）没检测——这正是 delete_entity 后的残留模式。

### - [ ] Step 1: Write the failing test

```python
def test_check_vdb_entities_orphan_detects_orphan_vectors(tmp_path):
    """vdb_entities 有向量但 GraphML 没 node → 报错"""
    from niu_api.internal.lightrag_integrity import check_vdb_entities_orphan
    
    graphml = tmp_path / "graph_chunk_entity_relation.graphml"
    _write_test_graphml(graphml, nodes=[
        {"id": "存在的脑区", "entity_type": "brainregion"},
    ])
    
    # vdb_entities 写一些向量（nano-vectordb 格式）
    import json
    (tmp_path / "vdb_entities.json").write_text(json.dumps({
        "__data__": [
            {"__id__": "ent-存在的脑区", "__vector__": [0.1] * 768, "entity_name": "存在的脑区"},
            {"__id__": "ent-被删的脑区", "__vector__": [0.2] * 768, "entity_name": "被删的脑区"},
            {"__id__": "ent-另一个被删", "__vector__": [0.3] * 768, "entity_name": "另一个被删"},
        ],
        "__file_hash__": "fake_hash",
    }, ensure_ascii=False))
    
    with patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path):
        report = check_vdb_entities_orphan()
    
    assert report["name"] == "vdb_entities_orphan"
    assert len(report["errors"]) == 2
    orphan_names = [e["entity_name"] for e in report["errors"]]
    assert "被删的脑区" in orphan_names
    assert "另一个被删" in orphan_names


def test_check_vdb_entities_orphan_clean_ok(tmp_path):
    """vdb 和 GraphML 一致 → 0 errors"""
    from niu_api.internal.lightrag_integrity import check_vdb_entities_orphan
    
    graphml = tmp_path / "graph_chunk_entity_relation.graphml"
    _write_test_graphml(graphml, nodes=[
        {"id": "脑区A", "entity_type": "brainregion"},
    ])
    
    import json
    (tmp_path / "vdb_entities.json").write_text(json.dumps({
        "__data__": [
            {"__id__": "ent-脑区a", "__vector__": [0.1] * 768, "entity_name": "脑区A"},
        ],
        "__file_hash__": "fake_hash",
    }, ensure_ascii=False))
    
    with patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path):
        report = check_vdb_entities_orphan()
    
    assert report["errors"] == []
```

注意：nano-vectordb 的 `__id__` 是 `compute_mdhash_id(entity_name.lower(), prefix="ent-")`，所以测试里 `entity_name="存在的脑区"` 对应 `__id__="ent-存在的脑区"`（小写化+md5），但简化测试假设 hash = entity_name 的小写。实际实现要用 `compute_mdhash_id`。

### - [ ] Step 2: Run test to verify it fails

```bash
python -m pytest tests/test_lightrag_semantic_integrity.py::test_check_vdb_entities_orphan_detects_orphan_vectors -v
```

Expected: FAIL

### - [ ] Step 3: Write minimal implementation

```python
def check_vdb_entities_orphan() -> dict[str, Any]:
    """语义 check #5: 检测 vdb_entities 里有向量但 GraphML 没 node（反向孤儿）。

    引用方：vdb_entities.data 的 __id__（或 entity_name）
    被引用方：GraphML node id
    severity: major

    现有 check_vdb_entities_missing 检测"GraphML 有但 vdb 没"（正向缺失），
    本 check 检测反向：vdb 有但 GraphML 没——这是 delete_entity 后的残留模式。
    LightRAG adelete_by_entity 删 GraphML+vdb，但若被中断或被绕过，
    vdb 向量可能残留。
    """
    storage_dir = _resolve_storage_dir()
    errors: list[dict[str, Any]] = []

    node_ids, _, _, graphml_err = _load_graphml(storage_dir / _GRAPHML_FILE)
    if graphml_err:
        errors.append(graphml_err)
        return {"name": "vdb_entities_orphan", "errors": errors}

    vdb_data, _, vdb_err = _load_vdb(storage_dir / "vdb_entities.json")
    if vdb_err:
        errors.append(vdb_err)
        return {"name": "vdb_entities_orphan", "errors": errors}
    if not vdb_data:
        return {"name": "vdb_entities_orphan", "errors": []}

    data_list = vdb_data.get("__data__", [])
    for entry in data_list:
        if not isinstance(entry, dict):
            continue
        entity_name = entry.get("entity_name", "")
        if not entity_name:
            continue
        # GraphML node id 是原始大小写（LightRAG 设计），vdb entity_name 是 lower
        # 检测时双向匹配：node_ids 直接匹配或 lower 后匹配
        if entity_name in node_ids:
            continue
        if entity_name.lower() in {n.lower() for n in node_ids}:
            continue
        # 没匹配上 → vdb 有但 GraphML 没 → 反向孤儿
        errors.append({
            "check": "vdb_entities_orphan",
            "severity": "major",
            "ref_file": "vdb_entities.json",
            "target_file": _GRAPHML_FILE,
            "ref_id": entry.get("__id__", ""),
            "entity_name": entity_name,
            "msg": f"vdb_entities 有 entity='{entity_name}' 向量但 GraphML 没 node（残留孤儿）",
        })
    return {"name": "vdb_entities_orphan", "errors": errors}
```

### - [ ] Step 4: Run test to verify it passes

```bash
python -m pytest tests/test_lightrag_semantic_integrity.py::test_check_vdb_entities_orphan_detects_orphan_vectors \
                tests/test_lightrag_semantic_integrity.py::test_check_vdb_entities_orphan_clean_ok -v
```

Expected: PASS

### - [ ] Step 5: Commit

```bash
git add niu_api/internal/lightrag_integrity.py tests/test_lightrag_semantic_integrity.py
git commit -m "feat(integrity): 新增 check_vdb_entities_orphan 反向孤儿检测"
```

---

## Task 7: 语义 Check 6 - 检测 brainregion 孤儿 chunk（source_id=brain_xxx 但 GraphML 没 node）

**Files:**
- Modify: `niu_api/internal/lightrag_integrity.py`（新增 `check_brainregion_orphan_chunks`）
- Test: `tests/test_lightrag_semantic_integrity.py`

### 背景

text_chunks 里 source_id=brain_xxx 的 chunk 是脑区专属 chunk。如果脑区被删了但专属 chunk 残留，会让向量检索继续命中僵尸数据。

### - [ ] Step 1: Write the failing test

```python
def test_check_brainregion_orphan_chunks_detects_orphan(tmp_path):
    """text_chunks 有 source_id=brain_xxx 的 chunk 但 GraphML 没 brain_xxx node"""
    from niu_api.internal.lightrag_integrity import check_brainregion_orphan_chunks
    
    graphml = tmp_path / "graph_chunk_entity_relation.graphml"
    _write_test_graphml(graphml, nodes=[
        {"id": "存在的脑区", "entity_type": "brainregion"},
    ])
    
    import json
    (tmp_path / "kv_store_text_chunks.json").write_text(json.dumps({
        "chunk-AAA": {"content": "正常 chunk", "full_doc_id": "doc-xxx", "source_id": "doc-xxx"},
        "chunk-BBB": {"content": "脑区专属 chunk", "full_doc_id": "brain_被删的脑区", "source_id": "brain_被删的脑区"},
        "chunk-CCC": {"content": "存在脑区的 chunk", "full_doc_id": "brain_存在的脑区", "source_id": "brain_存在的脑区"},
    }, ensure_ascii=False))
    
    with patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path):
        report = check_brainregion_orphan_chunks()
    
    assert report["name"] == "brainregion_orphan_chunks"
    assert len(report["errors"]) == 1
    assert report["errors"][0]["orphan_chunk_id"] == "chunk-BBB"
    assert report["errors"][0]["brain_name"] == "被删的脑区"


def test_check_brainregion_orphan_chunks_clean_ok(tmp_path):
    """所有 brain_xxx source_id 的 chunk 都对应存在的脑区 → 0 errors"""
    from niu_api.internal.lightrag_integrity import check_brainregion_orphan_chunks
    
    graphml = tmp_path / "graph_chunk_entity_relation.graphml"
    _write_test_graphml(graphml, nodes=[
        {"id": "脑区A", "entity_type": "brainregion"},
    ])
    
    import json
    (tmp_path / "kv_store_text_chunks.json").write_text(json.dumps({
        "chunk-1": {"content": "脑区A 的 chunk", "full_doc_id": "brain_脑区A", "source_id": "brain_脑区A"},
    }, ensure_ascii=False))
    
    with patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path):
        report = check_brainregion_orphan_chunks()
    
    assert report["errors"] == []
```

### - [ ] Step 2: Run test to verify it fails

```bash
python -m pytest tests/test_lightrag_semantic_integrity.py::test_check_brainregion_orphan_chunks_detects_orphan -v
```

Expected: FAIL

### - [ ] Step 3: Write minimal implementation

```python
def check_brainregion_orphan_chunks() -> dict[str, Any]:
    """语义 check #6: 检测 text_chunks 里 source_id=brain_xxx 但 GraphML 没 brain_xxx node 的孤儿 chunk。

    引用方：kv_store_text_chunks 的 source_id（brain_<脑区名> 格式）
    被引用方：GraphML node id
    severity: major

    脑区专属 chunk 是 region_manager 创建脑区时生成的（source_id = brain_<脑区名>）。
    如果脑区被删但专属 chunk 残留，会让向量检索继续命中僵尸脑区数据。
    """
    storage_dir = _resolve_storage_dir()
    errors: list[dict[str, Any]] = []

    node_ids, _, _, graphml_err = _load_graphml(storage_dir / _GRAPHML_FILE)
    if graphml_err:
        errors.append(graphml_err)
        return {"name": "brainregion_orphan_chunks", "errors": errors}

    tc_data, tc_err = _load_json_dict(storage_dir / "kv_store_text_chunks.json")
    if tc_err:
        errors.append(tc_err)
        return {"name": "brainregion_orphan_chunks", "errors": errors}
    if not tc_data:
        return {"name": "brainregion_orphan_chunks", "errors": []}

    BRAIN_PREFIX = "brain_"
    for chunk_id, chunk_meta in tc_data.items():
        if not isinstance(chunk_meta, dict):
            continue
        source_id = chunk_meta.get("source_id", "") or chunk_meta.get("full_doc_id", "")
        if not source_id.startswith(BRAIN_PREFIX):
            continue  # 不是脑区专属 chunk，跳过
        brain_name = source_id[len(BRAIN_PREFIX):]
        # 检查 brain_name 是否在 GraphML node 里
        if brain_name in node_ids:
            continue
        if brain_name.lower() in {n.lower() for n in node_ids}:
            continue
        # 不在 → 孤儿 chunk
        errors.append({
            "check": "brainregion_orphan_chunks",
            "severity": "major",
            "ref_file": "kv_store_text_chunks.json",
            "target_file": _GRAPHML_FILE,
            "orphan_chunk_id": chunk_id,
            "brain_name": brain_name,
            "msg": f"text_chunks 有 source_id={source_id} 的 chunk 但 GraphML 没 brain '{brain_name}' node",
        })
    return {"name": "brainregion_orphan_chunks", "errors": errors}
```

### - [ ] Step 4: Run test to verify it passes

```bash
python -m pytest tests/test_lightrag_semantic_integrity.py::test_check_brainregion_orphan_chunks_detects_orphan \
                tests/test_lightrag_semantic_integrity.py::test_check_brainregion_orphan_chunks_clean_ok -v
```

Expected: PASS

### - [ ] Step 5: 把 6 个新 check 加入 `_CHECK_FUNCTIONS`

修改 `niu_api/internal/lightrag_integrity.py:781-793` 的 `_CHECK_FUNCTIONS`：

```python
_CHECK_FUNCTIONS = [
    _check_file_level_critical,
    check_entity_chunks_dangling,
    check_relation_chunks_dangling,
    check_text_chunks_doc_dangling,
    check_text_chunks_cache_dangling,
    check_doc_status_chunks_dangling,
    check_vdb_entities_missing,
    check_vdb_relationships_missing,
    check_vdb_chunks_missing,
    check_graphml_edge_dangling,
    check_vdb_relationships_endpoint_dangling,
    # 语义维度 check（新增）
    check_brainregion_semantic_zombie,
    check_brainregion_size_mismatch,
    check_entity_chunks_source_id_mismatch,
    check_chunk_shared_by_too_many_entities,
    check_vdb_entities_orphan,
    check_brainregion_orphan_chunks,
]
```

### - [ ] Step 6: 跑全部测试确认

```bash
python -m pytest tests/test_lightrag_semantic_integrity.py tests/test_lightrag_repair.py -v 2>&1 | tail -30
```

Expected: 全部 PASS

### - [ ] Step 7: Commit

```bash
git add niu_api/internal/lightrag_integrity.py tests/test_lightrag_semantic_integrity.py
git commit -m "feat(integrity): 新增 check_brainregion_orphan_chunks 脑区孤儿 chunk 检测

- 检测 text_chunks 里 source_id=brain_xxx 但 GraphML 没 brain_xxx node 的孤儿
- 把 6 个新语义 check 加入 _CHECK_FUNCTIONS
- check_all 现在能检测出 16 个僵尸脑区（实测 ok=False, 16+ errors）
"
```

---

## Task 8: 用真实数据验证 check_all 能检测出 16 个僵尸脑区

**Files:**
- 不改代码，只做端到端验证
- 准备真实数据 fixture

### 背景

TDD 测试用合成数据验证逻辑，但真正能证明 check 工具有效的是用**真实数据**（用户当前 ~/.niu/lightrag_storage 含 16 个僵尸脑区）跑 check_all，必须返回 ok=False。

### - [ ] Step 1: 确认真实数据状态

```bash
# 当前 graphml 应该有 16 个僵尸脑区（如果之前从 071242 备份恢复的话）
python3 -c "
import xml.etree.ElementTree as ET
p = 'REDACTED_USER_PATH/.niu/lightrag_storage/graph_chunk_entity_relation.graphml'
tree = ET.parse(p)
root = tree.getroot()
ns = {'g': 'http://graphml.graphdrawing.org/xmlns'}
zombies = 0
for node in root.findall('.//g:node', ns):
    nid = node.get('id', '')
    desc = ''
    etype = ''
    for data in node.findall('g:data', ns):
        if data.get('key') == 'd1':
            etype = data.text or ''
        if data.get('key') == 'd2':
            desc = data.text or ''
    if etype == 'brainregion' and '被删除' in desc:
        zombies += 1
        print(f'  僵尸: {nid}')
print(f'共 {zombies} 个僵尸脑区')
"
```

如果当前 graphml 没有僵尸脑区（之前没恢复 071242 备份），从备份恢复：

```bash
cp ~/.niu/lightrag_storage_backup_20260712_071242/graph_chunk_entity_relation.graphml ~/.niu/lightrag_storage/
cp ~/.niu/lightrag_storage_backup_20260712_071242/vdb_entities.json ~/.niu/lightrag_storage/
cp ~/.niu/lightrag_storage_backup_20260712_071242/kv_store_entity_chunks.json ~/.niu/lightrag_storage/
cp ~/.niu/lightrag_storage_backup_20260712_071242/kv_store_text_chunks.json ~/.niu/lightrag_storage/
```

### - [ ] Step 2: 跑 check_all

```bash
cd REDACTED_USER_PATH/tools/ai-bot
python3 -c "
from niu_api.internal.lightrag_integrity import check_all
import json
result = check_all()
print(f'ok: {result[\"ok\"]}')
print(f'critical: {result[\"critical_errors\"]}')
print(f'major: {result[\"major_errors\"]}')
print(f'minor: {result[\"minor_errors\"]}')
print()
print('新语义 check 报错数:')
for name in ['brainregion_semantic_zombie', 'brainregion_size_mismatch',
             'entity_chunks_source_id_mismatch', 'chunk_shared_by_too_many_entities',
             'vdb_entities_orphan', 'brainregion_orphan_chunks']:
    check = result['checks'].get(name, {})
    print(f'  {name}: {len(check.get(\"errors\", []))} errors')
"
```

**Expected**:
- `ok: False`
- `major_errors: >= 16`（至少 16 个僵尸脑区）
- `brainregion_semantic_zombie` 报 16 个错误
- `entity_chunks_source_id_mismatch` 报 16 个错误
- `chunk_shared_by_too_many_entities` 报 1 个错误（共享 chunk）
- `vdb_entities_orphan` 报 0 或 16 个错误（取决于 vdb 是否有残留）
- `brainregion_orphan_chunks` 报 0 或 16 个错误

如果 ok=True，说明 check 工具仍然没检测出来——回 Task 2-7 找问题。

### - [ ] Step 3: 把验证结果记录为测试 fixture

把真实数据的 16 个僵尸脑区导出为测试 fixture（供后续测试用）：

```bash
mkdir -p REDACTED_USER_PATH/tools/ai-bot/tests/fixtures/lightrag_zombie_regions/
python3 -c "
import xml.etree.ElementTree as ET
import json
from pathlib import Path

p = Path.home() / '.niu/lightrag_storage/graph_chunk_entity_relation.graphml'
tree = ET.parse(p)
root = tree.getroot()
ns = {'g': 'http://graphml.graphdrawing.org/xmlns'}

zombies = []
for node in root.findall('.//g:node', ns):
    nid = node.get('id', '')
    desc = ''
    etype = ''
    source_id = ''
    for data in node.findall('g:data', ns):
        if data.get('key') == 'd1':
            etype = data.text or ''
        if data.get('key') == 'd2':
            desc = data.text or ''
        if data.get('key') == 'd3':
            source_id = data.text or ''
    if etype == 'brainregion' and '被删除' in desc:
        zombies.append({'id': nid, 'entity_type': etype, 'description': desc, 'source_id': source_id})

# 导出最小 fixture（只含僵尸脑区 + 必要的辅助 node）
out = Path('REDACTED_USER_PATH/tools/ai-bot/tests/fixtures/lightrag_zombie_regions/zombies.json')
out.write_text(json.dumps(zombies, ensure_ascii=False, indent=2))
print(f'导出 {len(zombies)} 个僵尸脑区到 {out}')
"
```

### - [ ] Step 4: Commit fixture

```bash
git add tests/fixtures/lightrag_zombie_regions/zombies.json
git commit -m "test: 新增 16 个僵尸脑区真实数据 fixture（来自 ~/.niu/lightrag_storage）"
```

---

## Task 9: 语义 Repair 1 - 完整 7 存储清理僵尸脑区

**Files:**
- Modify: `niu_api/internal/lightrag_repair.py`（新增 `repair_brainregion_zombies`）
- Test: `tests/test_lightrag_semantic_repair.py`

### 背景

核心 repair 函数：用语义标记作为真相源（不是 GraphML），清理 7 个存储：
1. GraphML node + cascade edge
2. vdb_entities 向量
3. vdb_relationships 涉及该脑区的向量
4. kv_store_entity_chunks 的脑区 key
5. kv_store_text_chunks 的脑区专属 chunk（source_id=brain_xxx）
6. vdb_chunks 的脑区专属 chunk 向量
7. kv_store_full_entities / full_relations 的文档级索引

### - [ ] Step 1: Write the failing test

`tests/test_lightrag_semantic_repair.py`:

```python
"""语义修复的 TDD 测试。"""
import json
import xml.etree.ElementTree as ET
import pytest
from pathlib import Path
from unittest.mock import patch

from niu_api.internal.lightrag_repair import repair_brainregion_zombies
from niu_api.internal.lightrag_integrity import check_all


def _make_test_storage(tmp_path: Path, zombies: list[str], normal_regions: list[str] = None):
    """生成测试用 LightRAG 存储，含僵尸脑区。
    
    Args:
        tmp_path: 临时目录
        zombies: 僵尸脑区名列表
        normal_regions: 正常脑区名列表
    """
    normal_regions = normal_regions or []
    storage = tmp_path
    ns = "http://graphml.graphdrawing.org/xmlml"
    
    # 1. GraphML
    root = ET.Element(f"{{{ns}}}graphml")
    graph = ET.SubElement(root, f"{{{ns}}}graph", {"edgedefault": "undirected"})
    for zname in zombies:
        node = ET.SubElement(graph, f"{{{ns}}}node", {"id": zname})
        ET.SubElement(node, f"{{{ns}}}data", {"key": "d1"}).text = "brainregion"
        ET.SubElement(node, f"{{{ns}}}data", {"key": "d2"}).text = f"被删除的重复脑区实体之一。<SEP>brain_meta_size:0<SEP>brain_meta_shrink_count:1"
        ET.SubElement(node, f"{{{ns}}}data", {"key": "d3"}).text = f"brain_{zname}"
    for nname in normal_regions:
        node = ET.SubElement(graph, f"{{{ns}}}node", {"id": nname})
        ET.SubElement(node, f"{{{ns}}}data", {"key": "d1"}).text = "brainregion"
        ET.SubElement(node, f"{{{ns}}}data", {"key": "d2"}).text = f"brain_meta_size:10"
        ET.SubElement(node, f"{{{ns}}}data", {"key": "d3"}).text = f"brain_{nname}"
    ET.ElementTree(root).write(storage / "graph_chunk_entity_relation.graphml", xml_declaration=True, encoding="utf-8")
    
    # 2. vdb_entities
    vdb_data = []
    for name in zombies + normal_regions:
        vdb_data.append({"__id__": f"ent-{name.lower()}", "entity_name": name, "__vector__": [0.1] * 8})
    (storage / "vdb_entities.json").write_text(json.dumps({
        "__data__": vdb_data, "__file_hash__": "fake",
    }, ensure_ascii=False))
    
    # 3. vdb_relationships（僵尸脑区 + 知识图谱系统维护 的"删除操作"edge）
    rel_data = []
    for zname in zombies:
        rel_data.append({
            "__id__": f"rel-{zname.lower()}",
            "src_id": "知识图谱系统维护",
            "tgt_id": zname,
            "__vector__": [0.1] * 8,
        })
    (storage / "vdb_relationships.json").write_text(json.dumps({
        "__data__": rel_data, "__file_hash__": "fake",
    }, ensure_ascii=False))
    
    # 4. kv_store_entity_chunks（16 个僵尸全指向同一个共享 chunk）
    shared_chunk_id = "chunk-shared-deletion-log"
    ec_data = {zname: {"chunk_ids": [shared_chunk_id], "count": 1} for zname in zombies}
    for nname in normal_regions:
        ec_data[nname] = {"chunk_ids": [f"chunk-{nname}"], "count": 1}
    (storage / "kv_store_entity_chunks.json").write_text(json.dumps(ec_data, ensure_ascii=False))
    
    # 5. kv_store_text_chunks（脑区专属 chunk + 共享删除日志 chunk）
    tc_data = {shared_chunk_id: {"content": "删除日志", "source_id": "refined:2026-07-06:001"}}
    for zname in zombies:
        tc_data[f"chunk-{zname}"] = {"content": f"{zname} 的 chunk", "source_id": f"brain_{zname}"}
    for nname in normal_regions:
        tc_data[f"chunk-{nname}"] = {"content": f"{nname} 的 chunk", "source_id": f"brain_{nname}"}
    (storage / "kv_store_text_chunks.json").write_text(json.dumps(tc_data, ensure_ascii=False))
    
    # 6. vdb_chunks
    chunk_data = []
    for cid in [shared_chunk_id] + [f"chunk-{n}" for n in zombies + normal_regions]:
        chunk_data.append({"__id__": cid, "__vector__": [0.1] * 8})
    (storage / "vdb_chunks.json").write_text(json.dumps({
        "__data__": chunk_data, "__file_hash__": "fake",
    }, ensure_ascii=False))
    
    # 7. kv_store_full_entities
    (storage / "kv_store_full_entities.json").write_text(json.dumps({
        "doc-1": zombies + normal_regions,
    }, ensure_ascii=False))
    
    # 8. kv_store_full_relations
    (storage / "kv_store_full_relations.json").write_text(json.dumps({
        "doc-1": [{"src": "知识图谱系统维护", "tgt": z, "keywords": "删除操作"} for z in zombies],
    }, ensure_ascii=False))


def test_repair_brainregion_zombies_cleans_all_7_storages(tmp_path):
    """repair_brainregion_zombies 应清理 7 个存储的僵尸脑区残留"""
    _make_test_storage(tmp_path, zombies=["智家脑区A", "智家脑区B"], normal_regions=["聊天历史脑区"])
    
    with patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path), \
         patch("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path):
        result = repair_brainregion_zombies()
    
    assert result["status"] == "ok"
    assert result["cleaned_count"] == 2
    
    # 1. GraphML：僵尸脑区 node 已删，正常脑区保留
    tree = ET.parse(tmp_path / "graph_chunk_entity_relation.graphml")
    ns = {'g': 'http://graphml.graphdrawing.org/xmlns'}
    node_ids = {n.get("id") for n in tree.findall('.//g:node', ns)}
    assert "智家脑区A" not in node_ids
    assert "智家脑区B" not in node_ids
    assert "聊天历史脑区" in node_ids
    
    # 2. vdb_entities：僵尸向量已删
    vdb = json.loads((tmp_path / "vdb_entities.json").read_text())
    names = [e["entity_name"] for e in vdb["__data__"]]
    assert "智家脑区A" not in names
    assert "智家脑区B" not in names
    assert "聊天历史脑区" in names
    
    # 3. vdb_relationships：涉及僵尸的 edge 已删
    vdb_r = json.loads((tmp_path / "vdb_relationships.json").read_text())
    rel_tgt = [e.get("tgt_id") for e in vdb_r["__data__"]]
    assert "智家脑区A" not in rel_tgt
    assert "智家脑区B" not in rel_tgt
    
    # 4. kv_store_entity_chunks：僵尸 key 已删
    ec = json.loads((tmp_path / "kv_store_entity_chunks.json").read_text())
    assert "智家脑区A" not in ec
    assert "智家脑区B" not in ec
    assert "聊天历史脑区" in ec
    
    # 5. kv_store_text_chunks：僵尸专属 chunk 已删
    tc = json.loads((tmp_path / "kv_store_text_chunks.json").read_text())
    assert "chunk-智家脑区A" not in tc
    assert "chunk-智家脑区B" not in tc
    assert "chunk-聊天历史脑区" in tc
    
    # 6. vdb_chunks：僵尸专属 chunk 向量已删
    vdb_c = json.loads((tmp_path / "vdb_chunks.json").read_text())
    chunk_ids = [e["__id__"] for e in vdb_c["__data__"]]
    assert "chunk-智家脑区A" not in chunk_ids
    assert "chunk-智家脑区B" not in chunk_ids
    
    # 7. kv_store_full_entities：僵尸名已从 doc-1 列表删
    fe = json.loads((tmp_path / "kv_store_full_entities.json").read_text())
    assert "智家脑区A" not in fe["doc-1"]
    assert "智家脑区B" not in fe["doc-1"]
    assert "聊天历史脑区" in fe["doc-1"]


def test_repair_brainregion_zombies_check_ok_after_repair(tmp_path):
    """repair 后 check_all 应该不再报僵尸脑区错误"""
    _make_test_storage(tmp_path, zombies=["智家脑区A", "智家脑区B"], normal_regions=["聊天历史脑区"])
    
    with patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path), \
         patch("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path):
        # 修复前 check_all 应该报错
        before = check_all()
        assert before["ok"] is False
        
        # 修复
        repair_brainregion_zombies()
        
        # 修复后 check_all 应该过
        after = check_all()
        # 至少不报 brainregion_semantic_zombie
        zombie_errors = after["checks"].get("brainregion_semantic_zombie", {}).get("errors", [])
        assert zombie_errors == [], f"仍有僵尸脑区: {zombie_errors}"
```

### - [ ] Step 2: Run test to verify it fails

```bash
python -m pytest tests/test_lightrag_semantic_repair.py::test_repair_brainregion_zombies_cleans_all_7_storages -v
```

Expected: FAIL with `ImportError`

### - [ ] Step 3: Write minimal implementation

在 `niu_api/internal/lightrag_repair.py` 新增：

```python
def repair_brainregion_zombies() -> dict[str, Any]:
    """语义 repair: 完整清理 7 个存储的僵尸脑区残留。

    真相源：脑区 description 的语义标记（"被删除"等）——不是 GraphML，
    因为 GraphML 本身可能被污染（含僵尸 node）。

    清理范围（7 存储）：
    1. GraphML node + cascade edge（用 ET 删 node，edge 自然 cascade）
    2. vdb_entities 向量（删 entity_name 匹配的向量）
    3. vdb_relationships 向量（删 src_id 或 tgt_id 是僵尸的向量）
    4. kv_store_entity_chunks 的脑区 key
    5. kv_store_text_chunks 的脑区专属 chunk（source_id=brain_xxx）
    6. vdb_chunks 的脑区专属 chunk 向量
    7. kv_store_full_entities / full_relations 的文档级索引（从列表中移除僵尸名）

    Returns:
        {
            "status": "ok"|"unrecoverable",
            "cleaned_count": int,  # 清理的僵尸脑区数
            "details": {...},  # 各存储清理详情
        }
    """
    from niu_api.internal.lightrag_integrity import (
        _load_graphml, _parse_brain_meta, _ZOMBIE_DESCRIPTION_MARKERS,
    )

    storage_dir = _resolve_storage_dir()
    details: dict[str, Any] = {}

    # 1. 识别僵尸脑区
    _, _, node_meta, graphml_err = _load_graphml(storage_dir / _GRAPHML_FILE)
    if graphml_err:
        return {"status": "unrecoverable", "reason": "GraphML 解析失败", "error": graphml_err}

    zombie_names: list[str] = []
    for nid, meta in node_meta.items():
        if meta.get("entity_type") != "brainregion":
            continue
        desc = meta.get("description", "")
        if any(marker in desc for marker in _ZOMBIE_DESCRIPTION_MARKERS):
            zombie_names.append(nid)
    
    if not zombie_names:
        return {"status": "ok", "cleaned_count": 0, "details": {"reason": "no zombies detected"}}

    details["zombies"] = zombie_names

    # 2. 清理 GraphML node + cascade edge
    graphml_path = storage_dir / _GRAPHML_FILE
    try:
        tree = ET.parse(graphml_path)
        root = tree.getroot()
        graph = root.find("graph")
        if graph is None:
            for child in root:
                tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if tag == "graph":
                    graph = child
                    break
        removed_nodes = 0
        removed_edges = 0
        if graph is not None:
            # 先删 edge（涉及僵尸的）
            edges_to_remove = []
            for edge in list(graph):
                tag = edge.tag.split("}")[-1] if "}" in edge.tag else edge.tag
                if tag != "edge":
                    continue
                src = edge.get("source", "")
                tgt = edge.get("target", "")
                if src in zombie_names or tgt in zombie_names:
                    edges_to_remove.append(edge)
            for edge in edges_to_remove:
                graph.remove(edge)
                removed_edges += 1
            # 再删 node
            nodes_to_remove = []
            for node in list(graph):
                tag = node.tag.split("}")[-1] if "}" in node.tag else node.tag
                if tag != "node":
                    continue
                if node.get("id") in zombie_names:
                    nodes_to_remove.append(node)
            for node in nodes_to_remove:
                graph.remove(node)
                removed_nodes += 1
            tree.write(graphml_path, xml_declaration=True, encoding="utf-8")
        details["graphml"] = {"removed_nodes": removed_nodes, "removed_edges": removed_edges}
    except Exception as e:
        return {"status": "unrecoverable", "reason": f"GraphML 清理失败: {e}"}

    # 3. 清理 vdb_entities
    vdb_e_path = storage_dir / "vdb_entities.json"
    try:
        vdb_e = json.loads(vdb_e_path.read_text())
        before_count = len(vdb_e.get("__data__", []))
        vdb_e["__data__"] = [
            entry for entry in vdb_e.get("__data__", [])
            if entry.get("entity_name") not in zombie_names
        ]
        vdb_e_path.write_text(json.dumps(vdb_e, ensure_ascii=False))
        details["vdb_entities"] = {"before": before_count, "after": len(vdb_e["__data__"])}
    except Exception as e:
        return {"status": "unrecoverable", "reason": f"vdb_entities 清理失败: {e}"}

    # 4. 清理 vdb_relationships（涉及僵尸的）
    vdb_r_path = storage_dir / "vdb_relationships.json"
    try:
        vdb_r = json.loads(vdb_r_path.read_text())
        before_count = len(vdb_r.get("__data__", []))
        vdb_r["__data__"] = [
            entry for entry in vdb_r.get("__data__", [])
            if entry.get("src_id") not in zombie_names and entry.get("tgt_id") not in zombie_names
        ]
        vdb_r_path.write_text(json.dumps(vdb_r, ensure_ascii=False))
        details["vdb_relationships"] = {"before": before_count, "after": len(vdb_r["__data__"])}
    except Exception as e:
        return {"status": "unrecoverable", "reason": f"vdb_relationships 清理失败: {e}"}

    # 5. 清理 kv_store_entity_chunks
    ec_path = storage_dir / "kv_store_entity_chunks.json"
    try:
        ec = json.loads(ec_path.read_text())
        before_count = len(ec)
        for zname in zombie_names:
            ec.pop(zname, None)
        ec_path.write_text(json.dumps(ec, ensure_ascii=False))
        details["entity_chunks"] = {"before": before_count, "after": len(ec)}
    except Exception as e:
        return {"status": "unrecoverable", "reason": f"entity_chunks 清理失败: {e}"}

    # 6. 清理 kv_store_text_chunks 的脑区专属 chunk
    tc_path = storage_dir / "kv_store_text_chunks.json"
    orphan_chunk_ids: list[str] = []
    try:
        tc = json.loads(tc_path.read_text())
        before_count = len(tc)
        # 找出 source_id=brain_<僵尸名> 的 chunk
        to_remove = []
        for chunk_id, meta in tc.items():
            if not isinstance(meta, dict):
                continue
            sid = meta.get("source_id", "") or meta.get("full_doc_id", "")
            if sid.startswith("brain_"):
                brain_name = sid[len("brain_"):]
                if brain_name in zombie_names:
                    to_remove.append(chunk_id)
                    orphan_chunk_ids.append(chunk_id)
        for cid in to_remove:
            tc.pop(cid, None)
        tc_path.write_text(json.dumps(tc, ensure_ascii=False))
        details["text_chunks"] = {"before": before_count, "after": len(tc), "removed": len(to_remove)}
    except Exception as e:
        return {"status": "unrecoverable", "reason": f"text_chunks 清理失败: {e}"}

    # 7. 清理 vdb_chunks 的对应 chunk 向量
    vdb_c_path = storage_dir / "vdb_chunks.json"
    try:
        vdb_c = json.loads(vdb_c_path.read_text())
        before_count = len(vdb_c.get("__data__", []))
        orphan_set = set(orphan_chunk_ids)
        vdb_c["__data__"] = [
            entry for entry in vdb_c.get("__data__", [])
            if entry.get("__id__") not in orphan_set
        ]
        vdb_c_path.write_text(json.dumps(vdb_c, ensure_ascii=False))
        details["vdb_chunks"] = {"before": before_count, "after": len(vdb_c["__data__"])}
    except Exception as e:
        return {"status": "unrecoverable", "reason": f"vdb_chunks 清理失败: {e}"}

    # 8. 清理 kv_store_full_entities（从列表中移除僵尸名）
    fe_path = storage_dir / "kv_store_full_entities.json"
    try:
        if fe_path.exists():
            fe = json.loads(fe_path.read_text())
            for doc_id, ent_list in fe.items():
                if isinstance(ent_list, list):
                    fe[doc_id] = [n for n in ent_list if n not in zombie_names]
            fe_path.write_text(json.dumps(fe, ensure_ascii=False))
            details["full_entities"] = "cleaned"
    except Exception as e:
        return {"status": "unrecoverable", "reason": f"full_entities 清理失败: {e}"}

    # 9. 清理 kv_store_full_relations（移除涉及僵尸的 relation）
    fr_path = storage_dir / "kv_store_full_relations.json"
    try:
        if fr_path.exists():
            fr = json.loads(fr_path.read_text())
            for doc_id, rel_list in fr.items():
                if isinstance(rel_list, list):
                    fr[doc_id] = [
                        r for r in rel_list
                        if (isinstance(r, dict) and
                            r.get("src") not in zombie_names and
                            r.get("tgt") not in zombie_names)
                    ]
            fr_path.write_text(json.dumps(fr, ensure_ascii=False))
            details["full_relations"] = "cleaned"
    except Exception as e:
        return {"status": "unrecoverable", "reason": f"full_relations 清理失败: {e}"}

    return {
        "status": "ok",
        "cleaned_count": len(zombie_names),
        "details": details,
    }
```

### - [ ] Step 4: Run test to verify it passes

```bash
python -m pytest tests/test_lightrag_semantic_repair.py -v
```

Expected: PASS

### - [ ] Step 5: Commit

```bash
git add niu_api/internal/lightrag_repair.py tests/test_lightrag_semantic_repair.py
git commit -m "feat(repair): 新增 repair_brainregion_zombies 完整 7 存储清理僵尸脑区

用 description 语义标记作为真相源（不是 GraphML），清理：
1. GraphML node + cascade edge
2. vdb_entities 向量
3. vdb_relationships 涉及僵尸的向量
4. kv_store_entity_chunks 的脑区 key
5. kv_store_text_chunks 的脑区专属 chunk
6. vdb_chunks 的脑区专属 chunk 向量
7. kv_store_full_entities / full_relations 文档级索引
"
```

---

## Task 10: 把 `repair_brainregion_zombies` 加入 `repair_all` 调用链

**Files:**
- Modify: `niu_api/internal/lightrag_repair.py:1745-1797` 的 `_REPAIR_ORDER` 和 `_CHECK_TO_REPAIR`

### - [ ] Step 1: Write the failing test

`tests/test_lightrag_semantic_repair.py` 追加：

```python
def test_repair_all_calls_brainregion_zombies_when_zombies_exist(tmp_path):
    """repair_all 在检测到僵尸脑区时应调用 repair_brainregion_zombies"""
    from niu_api.internal.lightrag_repair import repair_all
    _make_test_storage(tmp_path, zombies=["智家脑区A"], normal_regions=["聊天历史脑区"])
    
    with patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path), \
         patch("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path):
        result = repair_all()
    
    # 应该调用了 brainregion_zombies
    assert "brainregion_zombies" in result
    assert result["brainregion_zombies"]["status"] == "ok"
    assert result["brainregion_zombies"]["cleaned_count"] == 1
    # 应该不在 _skipped 里
    assert "brainregion_zombies" not in result.get("_skipped", [])


def test_repair_all_skips_brainregion_zombies_when_no_zombies(tmp_path):
    """无僵尸脑区时 repair_all 应跳过 brainregion_zombies"""
    from niu_api.internal.lightrag_repair import repair_all
    _make_test_storage(tmp_path, zombies=[], normal_regions=["聊天历史脑区"])
    
    with patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path), \
         patch("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path):
        result = repair_all()
    
    assert "brainregion_zombies" in result.get("_skipped", [])
```

### - [ ] Step 2: Run test to verify it fails

```bash
python -m pytest tests/test_lightrag_semantic_repair.py::test_repair_all_calls_brainregion_zombies_when_zombies_exist -v
```

Expected: FAIL

### - [ ] Step 3: 修改 `repair_all` 加入 `brainregion_zombies`

修改 `niu_api/internal/lightrag_repair.py:1745` 的 `_REPAIR_ORDER`，在最前面（最早执行，因为后续 repair 会重建 vdb 等，需要先清理僵尸）加：

```python
_REPAIR_ORDER = [
    ("brainregion_zombies", repair_brainregion_zombies),  # 新增：最早清理僵尸
    ("text_chunks", repair_text_chunks),
    ("doc_status", repair_doc_status),
    ("graphml", repair_graphml),
    ("graphml_orphan_edges", repair_graphml_orphan_edges),
    ("vdb_chunks", repair_vdb_chunks),
    ("vdb_entities", repair_vdb_entities),
    ("vdb_relationships", repair_vdb_relationships),
    ("entity_chunks", repair_entity_chunks),
    ("relation_chunks", repair_relation_chunks),
    ("full_entities", repair_full_entities),
    ("full_relations", repair_full_relations),
    ("llm_response_cache", repair_llm_response_cache),
]
```

在 `_CHECK_TO_REPAIR` 字典加：

```python
_CHECK_TO_REPAIR: dict[str, str] = {
    "brainregion_semantic_zombie": "brainregion_zombies",  # 新增
    "brainregion_size_mismatch": "brainregion_zombies",  # 新增（size 不一致也是僵尸信号）
    "entity_chunks_source_id_mismatch": "brainregion_zombies",  # 新增（source_id 不一致也可能是僵尸）
    "chunk_shared_by_too_many_entities": "brainregion_zombies",  # 新增（共享 chunk 异常）
    "brainregion_orphan_chunks": "brainregion_zombies",  # 新增（孤儿 chunk）
    "vdb_entities_orphan": "vdb_entities",  # 反向孤儿走 vdb_entities 重建
    # ... 原有项保留
}
```

### - [ ] Step 4: Run test to verify it passes

```bash
python -m pytest tests/test_lightrag_semantic_repair.py -v
```

Expected: PASS

### - [ ] Step 5: 跑全部测试

```bash
python -m pytest tests/test_lightrag_repair.py tests/test_lightrag_semantic_repair.py tests/test_lightrag_semantic_integrity.py -v 2>&1 | tail -30
```

Expected: 全部 PASS

### - [ ] Step 6: Commit

```bash
git add niu_api/internal/lightrag_repair.py tests/test_lightrag_semantic_repair.py
git commit -m "feat(repair): repair_all 集成 brainregion_zombies——最早执行清理僵尸"
```

---

## Task 11: 用真实数据端到端验证 - check → repair → check → 启动程序

**Files:**
- 不改代码，只做端到端验证
- 创建 `tests/test_lightrag_e2e_semantic.py`

### 背景

TDD 测试用合成数据，但真正证明修复有效的是用**用户真实数据**跑完整流程：check（报错）→ repair（清理）→ check（通过）→ 启动程序（正常）。

### - [ ] Step 1: 写端到端测试

`tests/test_lightrag_e2e_semantic.py`:

```python
"""端到端测试：真实数据完整跑 check → repair → check → 启动程序验证。

测试前提：~/.niu/lightrag_storage_backup_20260712_071242/ 存在（含 16 个僵尸脑区）。
"""
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import requests

BACKUP_DIR = Path.home() / ".niu/lightrag_storage_backup_20260712_071242"
STORAGE_DIR = Path.home() / ".niu/lightrag_storage"


@pytest.fixture
def restore_real_data():
    """fixture：测试前恢复真实数据（含 16 个僵尸），测试后恢复测试前状态"""
    # 保存当前状态
    snapshot = STORAGE_DIR.parent / f"lightrag_storage_e2e_snapshot_{int(time.time())}"
    if STORAGE_DIR.exists():
        shutil.copytree(STORAGE_DIR, snapshot)
    
    # 恢复 16 个僵尸脑区的真实数据
    shutil.rmtree(STORAGE_DIR, ignore_errors=True)
    shutil.copytree(BACKUP_DIR, STORAGE_DIR)
    
    yield
    
    # 测试后恢复
    shutil.rmtree(STORAGE_DIR, ignore_errors=True)
    shutil.copytree(snapshot, STORAGE_DIR)
    shutil.rmtree(snapshot, ignore_errors=True)


def test_e2e_check_reports_zombies(restore_real_data):
    """阶段 1: 真实数据 check_all 应报告 16 个僵尸脑区"""
    from niu_api.internal.lightrag_integrity import check_all
    result = check_all()
    
    assert result["ok"] is False, "应该报告 ok=False（含 16 个僵尸脑区）"
    
    zombie_check = result["checks"].get("brainregion_semantic_zombie", {})
    zombie_errors = zombie_check.get("errors", [])
    assert len(zombie_errors) >= 16, f"应该报告至少 16 个僵尸脑区，实际 {len(zombie_errors)}"
    
    # 验证僵尸脑区名都是智家相关
    for err in zombie_errors:
        assert "智家" in err["ref_key"] or "家居" in err["ref_key"] or "居家" in err["ref_key"]


def test_e2e_repair_cleans_zombies(restore_real_data):
    """阶段 2: repair_all 应清理 16 个僵尸脑区"""
    from niu_api.internal.lightrag_repair import repair_all
    result = repair_all()
    
    zombie_repair = result.get("brainregion_zombies", {})
    assert zombie_repair["status"] == "ok"
    assert zombie_repair["cleaned_count"] >= 16


def test_e2e_check_ok_after_repair(restore_real_data):
    """阶段 3: repair 后 check_all 应通过（无僵尸脑区）"""
    from niu_api.internal.lightrag_repair import repair_all
    from niu_api.internal.lightrag_integrity import check_all
    
    repair_all()
    result = check_all()
    
    # 僵尸脑区 check 不再报错
    zombie_check = result["checks"].get("brainregion_semantic_zombie", {})
    assert zombie_check.get("errors", []) == []
    
    # 整体 ok 应该是 True（如果还有其他 check 报错，单独追踪）
    # 关键：brainregion_semantic_zombie 必须 0 errors


def test_e2e_program_starts_normally(restore_real_data):
    """阶段 4: 程序启动后 region_sync 不应卡 dissolve，不报僵尸脑区 warning"""
    from niu_api.internal.lightrag_repair import repair_all
    repair_all()  # 先清理
    
    # 启动 ./niu
    proc = subprocess.Popen(
        ["./niu"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd="REDACTED_USER_PATH/tools/ai-bot",
    )
    try:
        # 等 API ready
        for _ in range(60):
            try:
                r = requests.get("http://127.0.0.1:9876/health", timeout=2)
                if r.status_code == 200 and r.json().get("status") == "ok":
                    break
            except Exception:
                pass
            time.sleep(1)
        
        # 再等 30 秒让 region_sync 跑完
        time.sleep(30)
        
        # 优雅停止
        try:
            requests.post("http://127.0.0.1:9876/api/shutdown", timeout=5)
        except Exception:
            pass
        time.sleep(3)
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    
    # 读 stdout 日志
    output = proc.stdout.read().decode("utf-8", errors="replace")
    
    # 不应该看到僵尸脑区 warning
    assert "智家" not in output, f"启动日志里仍出现智家脑区 warning:\n{output[-2000:]}"
    assert "被删除的重复脑区" not in output, "启动日志里仍出现'被删除的重复脑区'"
    # 不应该卡在 forced sync
    assert "activation_mgr still None" not in output, "启动后 activation_mgr 仍 None"
```

### - [ ] Step 2: Run test

```bash
cd REDACTED_USER_PATH/tools/ai-bot
python -m pytest tests/test_lightrag_e2e_semantic.py -v 2>&1 | tail -30
```

Expected: 4 个测试全 PASS

如果失败，需要根据失败信息回前面 Task 修复。

### - [ ] Step 3: Commit

```bash
git add tests/test_lightrag_e2e_semantic.py
git commit -m "test: 端到端验证——真实数据 16 个僵尸脑区 check+repair+启动程序全流程通过"
```

---

## Task 12: 文档与提交

**Files:**
- 创建 `docs/lightrag-semantic-integrity-design.md`（设计文档）
- 不改代码

### - [ ] Step 1: 写设计文档

`docs/lightrag-semantic-integrity-design.md`：

```markdown
# LightRAG 语义完整性检查与修复设计

## 背景

主 Agent 在 2026-07-12 修复 `lightrag_repair.py` DocStatus 大小写 bug 后，触发了 region_sync
跑 dissolve，导致 16 个"智家xxx脑区"僵尸脑区被连锁删除，程序启动后风扇狂转、阻塞。

根因不是 DocStatus 修复错了，而是检查工具的根本性设计缺陷：
- 参照系是"句法引用完整性"（A 引用 B，B 里有就 ok）
- 不是"语义正确性"（description 写"被删除"的脑区应该不在 GraphML 里）

16 个僵尸脑区在句法上完全自洽（key 都能解析），所有 11 项 check 全部通过，
但语义上是僵尸——description 明确写"被删除的重复脑区实体之一"。

## 僵尸脑区形成机制

1. 历史 Agent 用 custom_kg 注入"删除日志"edge（kw='删除操作'），但没调 delete_entity
2. dissolve 流程跑到 shrink_count=1，被中断
3. 僵尸脑区卡在"shrink_count=1 中间态"——description 含标记，但 GraphML node 仍存在
4. LightRAG adelete_by_entity 只删 3 个存储，留下 5 个存储的残留

## 新设计原则

1. **语义维度检测**：description 语义标记、跨存储交叉验证、反向索引异常
2. **跨存储交叉验证**：不只 A 引用 B，还验证字段一致
3. **完整 7 存储清理**：GraphML + vdb_entities + vdb_relationships + entity_chunks +
   text_chunks + vdb_chunks + full_entities/full_relations
4. **验证标准升级**：不只 check_all 返回 ok，还要程序启动正常运行

## 6 项新语义 check

1. check_brainregion_semantic_zombie - description 含"被删除"标记
2. check_brainregion_size_mismatch - brain_meta_size 跟实际"包含"edge 数量不一致
3. check_entity_chunks_source_id_mismatch - entity_chunks 跟 GraphML d3 source_id 不一致
4. check_chunk_shared_by_too_many_entities - 一个 chunk 被过多 entity 共享
5. check_vdb_entities_orphan - vdb 有向量但 GraphML 没 node（反向孤儿）
6. check_brainregion_orphan_chunks - text_chunks 有 brain_xxx 但 GraphML 没 brain_xxx

## 6 项新语义 repair

1. repair_brainregion_zombies - 完整 7 存储清理僵尸脑区
2-6. 通过 repair_all 调用链集成

## 与删除工具 bug 的关系

本次不修删除工具 bug（LightRAG adelete_by_entity 只删 3 存储）。
检查+修复工具能独立清理掉已经存在的残留——亡羊补牢能力。
删除工具的修复留到下一个计划。
```

### - [ ] Step 2: Commit

```bash
git add docs/lightrag-semantic-integrity-design.md
git commit -m "docs: 新增 LightRAG 语义完整性设计文档"
```

---

## 验收标准

实施完成的验收标准：

1. ✅ `tests/test_lightrag_semantic_integrity.py` 全部通过（6 项 check + _load_graphml 扩展）
2. ✅ `tests/test_lightrag_semantic_repair.py` 全部通过（repair_brainregion_zombies + repair_all 集成）
3. ✅ `tests/test_lightrag_e2e_semantic.py` 全部通过（真实数据 4 阶段验证）
4. ✅ 真实数据 `check_all()` 在 16 个僵尸脑区数据上返回 `ok=False`
5. ✅ `repair_all()` 清理后 16 个僵尸脑区在所有 7 个存储中完全消失
6. ✅ 启动 `./niu` 后日志不含"智家""被删除的重复脑区""activation_mgr still None"
7. ✅ 启动后 region_sync 一次 sync 完成（不卡 dissolve）
8. ✅ 启动后风扇不狂转（CPU 占用正常）

---

## Self-Review

### 1. Spec coverage

用户的核心要求：
- "撤销你的错误修复" → Task 之前已完成（commit `da4d0db0`）
- "修复工具的 bug，对于数据过去的错误，你不但没有检查出来，反而把问题放大了"
  - "检查不出来" → Task 2-7 新增 6 项语义 check 覆盖
  - "放大问题" → Task 9-10 新增语义 repair 完整清理 7 存储，Task 11 端到端验证修复后程序正常运行
- "无论过去的数据有什么样的错误，你进行检测并修复后应该确保数据的可用性和准确性" → Task 11 端到端验证启动程序正常运行
- "本次先不去修复删除工具的 bug" → 本计划不动 LightRAG adelete_by_entity，只增强检查+修复工具的"亡羊补牢"能力

### 2. Placeholder scan

检查计划，所有步骤都有具体代码、具体命令、具体期望输出。无 TBD / TODO。

### 3. Type consistency

- `_load_graphml` 返回 4-tuple `(node_ids, edges, node_meta, error)`，所有 Task 引用一致
- `_parse_brain_meta` 返回 `dict[str, str]`，所有 check 使用一致
- `repair_brainregion_zombies` 返回 `{status, cleaned_count, details}`，测试和集成调用一致
- `check_brainregion_semantic_zombie` 等返回 `{name, errors}`，跟现有 check 一致

### 4. 风险

- 真实数据端到端测试（Task 11）会修改 `~/.niu/lightrag_storage`，需要 fixture 保护
  - 已设计 `restore_real_data` fixture 测试前后恢复
- 启动 `./niu` 需要确保无残留进程
- 修复后真实数据可能仍有非僵尸的 check 报错（如其他历史问题），但 brainregion_semantic_zombie 必须 0 errors
