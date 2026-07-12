# LightRAG 语义完整性检查与修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 LightRAG 数据一致性检查工具能检测出 16 个历史遗留的"僵尸脑区"（句法自洽但语义死亡的数据），让修复工具能完整清理 8 个存储的残留数据，确保修复后数据真正可用。

**Architecture:** 在现有 `lightrag_integrity.py`（11 项句法 check）和 `lightrag_repair.py`（12 项 repair）基础上，新增 5 项语义 check 和 5 项语义 repair（原计划 6 项，`check_brainregion_size_mismatch` 因真实数据无效已删除，见 Task 3）。语义 check 用"description 语义标记 + 跨存储交叉验证"作为参照系（不是句法引用完整性）。语义 repair 用"语义标记"作为真相源（不是 GraphML——GraphML 本身可能被污染），做完整 8 存储清理（含 `kv_store_relation_chunks`——Bug #3 修复）。修复后用"程序启动正常运行"作为验证标准（不是 check_all 返回 ok）。同时修复 4 个 P0 遗漏（Task 13-16：region_sync 覆盖率检查/forced sync 死循环/shrink_threshold/forced sync 阻塞）。

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
2. `shrink_threshold=100` 误判正常小脑区（成员数 < 100）为萎缩，dissolve 流程跑到 shrink_count=1
3. dissolve 被中断（进程重启、sync 没跑完等），僵尸脑区卡在"shrink_count=1 中间态"——description 含 `brain_meta_shrink_count:1`，但 GraphML node 仍存在
4. LightRAG 的 `adelete_by_entity` 设计缺陷：只删 3 个存储（GraphML + vdb_entities + vdb_relationships），留下 entity_chunks / text_chunks / vdb_chunks / full_entities / full_relations 5 个存储的残留

> 根因之一是 `shrink_threshold=100` 太高（Task 15 修复），让正常小脑区被判萎缩进入 dissolve 流程。
> 此外 region_sync 的覆盖率检查（Task 13）和 forced sync 死循环（Task 14）+ 阻塞（Task 16）
> 是程序启动卡死的直接原因。

### 新设计原则

**P1: 语义维度检测**——不只是"引用是否解析"，还要验证：
- description 含"被删除"/"重复"等标记但 node 仍存在
- `brain_meta_shrink_count` 卡在 1 ≤ N < 3 持续多周期
- text_chunks 的 brain_xxx 专属 chunk content 含"被删除"标记但 node 仍存在
- 一个 chunk 被超过 N 个 entity 共享（异常）

> 注：原计划有"`brain_meta_size` 跟实际'包含'edge 数量不一致"作为 P1 检测项，
> 但真实数据验证发现 16 个僵尸脑区 `brain_meta_size:0` + 实际 0 条"包含"edge
> 一致，此检测项无效，已从 check 列表删除（见 Task 3）。改用"chunk content
> 含'被删除'标记"作为 chunk 侧僵尸信号检测。

**P2: 跨存储交叉验证**——不只是"A 引用 B"，还要验证：
- entity_chunks 的 chunk_ids 跟 GraphML node d3 source_id 是否一致
- vdb_entities 里有向量但 GraphML 没有 node（反向孤儿）
- vdb_chunks 里有向量但 text_chunks 没有 chunk（反向孤儿）

**P3: 完整 8 存储清理**——repair 时清干净：
- GraphML node + cascade edge
- vdb_entities 向量
- vdb_relationships 向量
- entity_chunks key
- text_chunks 专属 chunk
- vdb_chunks 专属 chunk 向量
- full_entities / full_relations 文档级索引
- relation_chunks 僵尸关系 chunk（Bug #3 修复：key 格式 `src<SEP>tgt`，src 或 tgt 是僵尸脑区则删）

**P4: 验证标准升级**——不只看 `check_all` 返回 ok，还要：
- 16 个僵尸脑区在所有 8 个存储中完全消失
- `brain_meta_shrink_count` 不在任何 description 里
- 程序启动后 region_sync 一次 sync 完成（不卡 dissolve）
- 风扇不狂转

### 与删除工具 bug 的关系

**本次不修删除工具的 bug**（LightRAG `adelete_by_entity` 只删 3 个存储的设计缺陷）。但检查+修复工具必须能独立清理掉已经存在的残留——即"亡羊补牢"能力。删除工具的修复留到下一个计划。

---

## 文件结构

| 文件 | 责任 | 改动类型 |
|------|------|---------|
| `niu_api/internal/lightrag_integrity.py` | 新增 5 项语义 check + 扩展 `_load_graphml` 提取 description/entity_type | 修改 |
| `niu_api/internal/lightrag_repair.py` | 新增 5 项语义 repair（完整 8 存储清理，含 `kv_store_relation_chunks`） + 扩展 `repair_all` 调用语义 repair + 顶部加 `import zlib` | 修改 |
| `tests/test_lightrag_semantic_integrity.py` | 5 项语义 check 的 TDD 测试 | 创建 |
| `tests/test_lightrag_semantic_repair.py` | 5 项语义 repair 的 TDD 测试 | 创建 |
| `tests/fixtures/lightrag_zombie_regions/` | 僵尸脑区测试数据 fixture（含真实 16 个僵尸脑区的最小复现） | 创建 |
| `tests/test_lightrag_e2e_semantic.py` | 端到端测试：真实数据跑 check → repair → 启动程序验证正常运行 | 创建 |
| `agent/injector/region_sync.py` | 删除 `_refresh_activation_manager` 覆盖率检查（Task 13）+ `shrink_threshold` 从 100 降到 10（Task 15） | 修改 |
| `agent/runner.py` | forced sync 加 5 分钟失败冷却 + 成功后重置冷却时间（Task 14）+ forced sync 改异步触发（Task 16）+ 顶部加 `import time`（Bug #6） | 修改 |
| `niu_api/internal/region_manager.py` | `dissolve_shrunk_regions` 参数默认值 `shrink_threshold` 从 100 降到 10（Task 15） | 修改 |
| `tests/test_region_sync.py` | 删除 `test_refresh_activation_manager_skips_when_coverage_too_low`（Task 13 Step 3.5，覆盖率检查已删） | 修改 |

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
    # 分隔符是 LightRAG 的 GRAPH_FIELD_SEP（字符串 "<SEP>"）
    # 真实数据观察：description 用 "<SEP>" 字符串分隔，不是 \x1f unit separator
    parts = description.split("<SEP>")
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

具体 8 处调用点修改对照（行号基于当前 `lightrag_integrity.py`，Bug H 修复后从 7 处补到 8 处）：

```python
# L253 (check_entity_chunks_dangling)
- node_ids, _, graphml_err = _load_graphml(storage_dir / _GRAPHML_FILE)
+ node_ids, _, _, graphml_err = _load_graphml(storage_dir / _GRAPHML_FILE)

# L288 (check_relation_chunks_dangling)
- _, edges, graphml_err = _load_graphml(storage_dir / _GRAPHML_FILE)
+ _, edges, _, graphml_err = _load_graphml(storage_dir / _GRAPHML_FILE)

# L478 (check_vdb_entities_missing)
- node_ids, _, graphml_err = _load_graphml(storage_dir / _GRAPHML_FILE)
+ node_ids, _, _, graphml_err = _load_graphml(storage_dir / _GRAPHML_FILE)

# L522 (check_vdb_relationships_missing)
- _, edges, graphml_err = _load_graphml(storage_dir / _GRAPHML_FILE)
+ _, edges, _, graphml_err = _load_graphml(storage_dir / _GRAPHML_FILE)

# L617 (check_graphml_edge_dangling)
- node_ids, edges, graphml_err = _load_graphml(storage_dir / _GRAPHML_FILE)
+ node_ids, edges, _, graphml_err = _load_graphml(storage_dir / _GRAPHML_FILE)

# L656 (check_vdb_relationships_endpoint_dangling 第一处)
- node_ids, _, graphml_err = _load_graphml(storage_dir / _GRAPHML_FILE)
+ node_ids, _, _, graphml_err = _load_graphml(storage_dir / _GRAPHML_FILE)

# L687 (check_vdb_relationships_endpoint_dangling 第二处——Bug H 修复)
# 审查发现原计划漏掉 L687 这处调用。该函数内部有两处 _load_graphml 调用，
# 一处在 L656 提取 node_ids 用于 endpoint dangling 检测，
# 另一处在 L687 重新解析（或在该函数后段循环里重新调用——实施时以 grep 输出为准）
- node_ids, _, graphml_err = _load_graphml(storage_dir / _GRAPHML_FILE)
+ node_ids, _, _, graphml_err = _load_graphml(storage_dir / _GRAPHML_FILE)
# 注意：L687 的解构形式可能是 `node_ids, _, graphml_err` 也可能是 `_, edges, graphml_err` 等，
# 实施时必须 Read 该行确认具体解构形式再改。

# L769 (_check_file_level_critical 内部调用)
- _, _, err = _load_graphml(storage_dir / _GRAPHML_FILE)
+ _, _, _, err = _load_graphml(storage_dir / _GRAPHML_FILE)
```

注意：实际行号可能在修改时已偏移，以 `grep -n "_load_graphml"` 输出为准——逐一打开每处确认当前是 3-tuple 解构，改成 4-tuple。**真实代码有 8 处调用（不含函数定义），不是 7 处**——审查 Bug H 修复前原计划漏了 L687。

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

## Task 3: [已删除] `check_brainregion_size_mismatch` 在真实数据上无效

> **此 Task 已在计划审查时删除**——真实数据验证发现 16 个僵尸脑区的
> `brain_meta_size` 全是 0，实际"包含"edge 数量也是 0（一致），本 check
> 检测不出僵尸。为避免给实施者虚假"已检测"的印象，删除整个 check。
>
> 后续 Task 4-12 编号保持不变（不重新编号），避免打乱交叉引用。
> 新增 Task 13-16 修复 4 个 P0 遗漏问题。
>
> **实施者跳过此 Task，直接进入 Task 4。**

### 删除理由

- 真实数据：16 个僵尸脑区 `brain_meta_size:0` + 实际 0 条"包含"edge → 一致，不报错
- 保留本 check 会给实施者虚假的"已检测僵尸"印象，但实际检测不出
- 真正能检测僵尸的是 Task 2 (`check_brainregion_semantic_zombie`，语义标记)
- `_CHECK_FUNCTIONS` 列表（Task 7 Step 5）已同步移除 `check_brainregion_size_mismatch`
- `Task 8 Step 2 Expected` 已同步移除 `brainregion_size_mismatch` 行
- `_CHECK_TO_REPAIR`（Task 10）已同步移除 `brainregion_size_mismatch` 映射

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
        graphml_ids = [s for s in graphml_source_id.split("<SEP>") if s]
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
    # 注意：真实 vdb 顶层字段是 `data`（不是 `__data__`），entry 的向量字段是 `vector`（不是 `__vector__`），
    # 值是 base64 字符串（不是 list[float]）。这里简化用 base64 字符串占位。
    import json
    (tmp_path / "vdb_entities.json").write_text(json.dumps({
        "data": [
            {"__id__": "ent-存在的脑区", "vector": "AAAAAA==", "entity_name": "存在的脑区"},
            {"__id__": "ent-被删的脑区", "vector": "AAAAAA==", "entity_name": "被删的脑区"},
            {"__id__": "ent-另一个被删", "vector": "AAAAAA==", "entity_name": "另一个被删"},
        ],
        "file_hash": "fake_hash",
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
        "data": [
            {"__id__": "ent-脑区a", "vector": "AAAAAA==", "entity_name": "脑区A"},
        ],
        "file_hash": "fake_hash",
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

    _, vdb_data_list, vdb_err = _load_vdb(storage_dir / "vdb_entities.json")
    if vdb_err:
        errors.append(vdb_err)
        return {"name": "vdb_entities_orphan", "errors": errors}
    if not vdb_data_list:
        return {"name": "vdb_entities_orphan", "errors": []}

    # 防御性 check：当前真实数据 vdb_entities.json 可能为空（len(data)=0），
    # 此时反向孤儿永远 0 个。本 check 作为防御性检测，未来 vdb 有数据时能检测。
    for entry in vdb_data_list:
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


def test_check_brainregion_orphan_chunks_detects_zombie_content(tmp_path):
    """brain_xxx chunk content 含'被删除'标记但 GraphML 仍有 brain_xxx node → chunk 侧僵尸信号"""
    from niu_api.internal.lightrag_integrity import check_brainregion_orphan_chunks
    
    graphml = tmp_path / "graph_chunk_entity_relation.graphml"
    # 注意：脑区 node 仍在（这是僵尸脑区的特征：description 含标记但 node 还在）
    _write_test_graphml(graphml, nodes=[
        {"id": "智家僵尸脑区", "entity_type": "brainregion",
         "description": "被删除的重复脑区实体之一。<SEP>brain_meta_size:0"},
    ])
    
    import json
    (tmp_path / "kv_store_text_chunks.json").write_text(json.dumps({
        # 脑区专属 chunk content 含"被删除"标记 → 报错（chunk 侧僵尸信号）
        "chunk-zombie": {
            "content": "这是被删除的重复脑区实体之一的专属 chunk",
            "full_doc_id": "brain_智家僵尸脑区",
            "source_id": "brain_智家僵尸脑区",
        },
        # 正常脑区专属 chunk，content 不含标记 → 不报
        "chunk-normal": {
            "content": "智家僵尸脑区的正常内容",
            "full_doc_id": "brain_智家僵尸脑区",
            "source_id": "brain_智家僵尸脑区",
        },
    }, ensure_ascii=False))
    
    with patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path):
        report = check_brainregion_orphan_chunks()
    
    assert report["name"] == "brainregion_orphan_chunks"
    assert len(report["errors"]) == 1
    assert report["errors"][0]["orphan_chunk_id"] == "chunk-zombie"
    assert report["errors"][0]["brain_name"] == "智家僵尸脑区"
    assert "marker" in report["errors"][0]
    assert "被删除" in report["errors"][0]["marker"]
```

### - [ ] Step 2: Run test to verify it fails

```bash
python -m pytest tests/test_lightrag_semantic_integrity.py::test_check_brainregion_orphan_chunks_detects_orphan -v
```

Expected: FAIL

### - [ ] Step 3: Write minimal implementation

```python
def check_brainregion_orphan_chunks() -> dict[str, Any]:
    """语义 check #6: 检测脑区孤儿 chunk（两种形式）。

    引用方：kv_store_text_chunks 的 source_id 或 content
    被引用方：GraphML node id
    severity: major

    检测两种孤儿 chunk：
    1. text_chunks 里 source_id=brain_xxx 但 GraphML 没 brain_xxx node（脑区被删但专属 chunk 残留）
    2. text_chunks 里 source_id=brain_xxx 的 chunk content 含"被删除"标记，
       但 GraphML 仍有该 brain_xxx node（chunk 侧标记 node 是僵尸，配合
       check_brainregion_semantic_zombie 交叉验证）

    脑区专属 chunk 是 region_manager 创建脑区时生成的（source_id = brain_<脑区名>）。
    如果脑区被删但专属 chunk 残留，会让向量检索继续命中僵尸脑区数据。
    如果脑区 node 还在但专属 chunk content 含"被删除"标记，说明 chunk 侧已标记删除
    但 node 没删——这也是僵尸信号。
    """
    storage_dir = _resolve_storage_dir()
    errors: list[dict[str, Any]] = []

    _, _, node_meta, graphml_err = _load_graphml(storage_dir / _GRAPHML_FILE)
    if graphml_err:
        errors.append(graphml_err)
        return {"name": "brainregion_orphan_chunks", "errors": errors}

    tc_data, tc_err = _load_json_dict(storage_dir / "kv_store_text_chunks.json")
    if tc_err:
        errors.append(tc_err)
        return {"name": "brainregion_orphan_chunks", "errors": errors}
    if not tc_data:
        return {"name": "brainregion_orphan_chunks", "errors": []}

    node_ids = set(node_meta.keys())
    BRAIN_PREFIX = "brain_"
    seen_orphan_chunk_ids: set[str] = set()  # 避免同一 chunk 报两次
    for chunk_id, chunk_meta in tc_data.items():
        if not isinstance(chunk_meta, dict):
            continue
        source_id = chunk_meta.get("source_id", "") or chunk_meta.get("full_doc_id", "")
        if not source_id.startswith(BRAIN_PREFIX):
            continue  # 不是脑区专属 chunk，跳过
        brain_name = source_id[len(BRAIN_PREFIX):]
        content = chunk_meta.get("content", "") or ""
        # 检查 1：brain_name 不在 GraphML → 孤儿 chunk
        brain_in_graph = brain_name in node_ids or brain_name.lower() in {n.lower() for n in node_ids}
        if not brain_in_graph:
            errors.append({
                "check": "brainregion_orphan_chunks",
                "severity": "major",
                "ref_file": "kv_store_text_chunks.json",
                "target_file": _GRAPHML_FILE,
                "orphan_chunk_id": chunk_id,
                "brain_name": brain_name,
                "msg": f"text_chunks 有 source_id={source_id} 的 chunk 但 GraphML 没 brain '{brain_name}' node",
            })
            seen_orphan_chunk_ids.add(chunk_id)
            continue
        # 检查 2：brain_name 在 GraphML 但 chunk content 含"被删除"标记 → chunk 侧僵尸信号
        # 与 check_brainregion_semantic_zombie 配合（node description + chunk content 都含标记）
        if chunk_id in seen_orphan_chunk_ids:
            continue
        for marker in _ZOMBIE_DESCRIPTION_MARKERS:
            if marker in content:
                errors.append({
                    "check": "brainregion_orphan_chunks",
                    "severity": "major",
                    "ref_file": "kv_store_text_chunks.json",
                    "target_file": _GRAPHML_FILE,
                    "orphan_chunk_id": chunk_id,
                    "brain_name": brain_name,
                    "marker": marker,
                    "msg": f"text_chunks chunk '{chunk_id}' (brain={brain_name}) content 含语义标记'{marker}'（chunk 侧僵尸信号）",
                })
                seen_orphan_chunk_ids.add(chunk_id)
                break  # 一个 chunk 只报一次
    return {"name": "brainregion_orphan_chunks", "errors": errors}
```

### - [ ] Step 4: Run test to verify it passes

```bash
python -m pytest tests/test_lightrag_semantic_integrity.py::test_check_brainregion_orphan_chunks_detects_orphan \
                tests/test_lightrag_semantic_integrity.py::test_check_brainregion_orphan_chunks_clean_ok \
                tests/test_lightrag_semantic_integrity.py::test_check_brainregion_orphan_chunks_detects_zombie_content -v
```

Expected: PASS

### - [ ] Step 5: 把 5 个新 check 加入 `_CHECK_FUNCTIONS`

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
    check_entity_chunks_source_id_mismatch,
    check_chunk_shared_by_too_many_entities,
    check_vdb_entities_orphan,
    check_brainregion_orphan_chunks,
]
```

> 注意：`check_brainregion_size_mismatch` 原为第 2 项语义 check，因在真实数据上无效
> （16 个僵尸脑区 brain_meta_size:0 + 实际 0 条包含 edge 一致）已删除，见 Task 3。

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
- 把 5 个新语义 check 加入 _CHECK_FUNCTIONS（check_brainregion_size_mismatch 因无效已删除）
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
for name in ['brainregion_semantic_zombie',
             'entity_chunks_source_id_mismatch', 'chunk_shared_by_too_many_entities',
             'vdb_entities_orphan', 'brainregion_orphan_chunks']:
    check = result['checks'].get(name, {})
    print(f'  {name}: {len(check.get(\"errors\", []))} errors')
"
```

**Expected**（真实数据实测值，审查重做时确认）：
- `ok: False`
- `major_errors: >= 16`（至少 16 个僵尸脑区）
- `brainregion_semantic_zombie` 报 **16** 个错误（16 个僵尸脑区主要被本 check 检测出来）
- `entity_chunks_source_id_mismatch` 报 **23** 个错误（16 个僵尸 + 其他历史 source_id 不一致残留）
- `chunk_shared_by_too_many_entities` 报 **84** 个错误（84 个共享 chunk——含其他历史问题，不只是 16 个僵尸脑区共享的"删除日志"chunk）
- `vdb_entities_orphan` 报 **0** 个错误（当前 vdb_entities.json 为空文件，反向孤儿永远 0；本 check 作为防御性检测，未来 vdb 有数据时能检测）
- `brainregion_orphan_chunks` 报 **39** 个错误（39 个孤儿 chunk——含其他历史残留孤儿 chunk，不只是 16 个僵尸脑区的 brain_xxx 专属 chunk；16 个僵尸脑区的 brain_xxx 专属 chunk 的 brain_name 在 GraphML 里有 node，所以不报"孤儿"，但部分会被 `check_brainregion_semantic_zombie` 跨存储交叉检测出来）

注意：
- `check_brainregion_size_mismatch` 已删除（见 Task 3），不在 Expected 列表里。
- 84 个共享 chunk 和 39 个孤儿 chunk 含其他历史残留问题，不全是 16 个僵尸脑区造成的——修复工具只清理"含'被删除'语义标记"的僵尸脑区及其关联 chunk，其他历史残留不在本次修复范围。
- 16 个僵尸脑区主要通过 `brainregion_semantic_zombie` 检测（description 含"被删除"语义标记）。

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

## Task 9: 语义 Repair 1 - 完整 8 存储清理僵尸脑区

**Files:**
- Modify: `niu_api/internal/lightrag_repair.py`（新增 `repair_brainregion_zombies`）
- Test: `tests/test_lightrag_semantic_repair.py`

### 背景

核心 repair 函数：用语义标记作为真相源（不是 GraphML），清理 8 个存储：
1. GraphML node + cascade edge
2. vdb_entities 向量
3. vdb_relationships 涉及该脑区的向量
4. kv_store_entity_chunks 的脑区 key
5. kv_store_text_chunks 的脑区专属 chunk（source_id=brain_xxx）
6. vdb_chunks 的脑区专属 chunk 向量
7. kv_store_full_entities / full_relations 的文档级索引
8. kv_store_relation_chunks 的僵尸关系 chunk（Bug #3 修复：真实数据含 16 个僵尸脑区关系 chunk，key 格式 "src<SEP>tgt"，src 或 tgt 是僵尸脑区则删）

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
    
    # 2. vdb_entities（真实格式：顶层 `data` 字段，entry `vector` 是 base64 字符串）
    vdb_data = []
    for name in zombies + normal_regions:
        vdb_data.append({"__id__": f"ent-{name.lower()}", "entity_name": name, "vector": "AAAAAA=="})
    (storage / "vdb_entities.json").write_text(json.dumps({
        "data": vdb_data, "file_hash": "fake",
    }, ensure_ascii=False))

    # 3. vdb_relationships（僵尸脑区 + 知识图谱系统维护 的"删除操作"edge）
    rel_data = []
    for zname in zombies:
        rel_data.append({
            "__id__": f"rel-{zname.lower()}",
            "src_id": "知识图谱系统维护",
            "tgt_id": zname,
            "vector": "AAAAAA==",
        })
    (storage / "vdb_relationships.json").write_text(json.dumps({
        "data": rel_data, "file_hash": "fake",
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
    
    # 6. vdb_chunks（真实格式：顶层 `data` 字段，entry `vector` 是 base64 字符串）
    chunk_data = []
    for cid in [shared_chunk_id] + [f"chunk-{n}" for n in zombies + normal_regions]:
        chunk_data.append({"__id__": cid, "vector": "AAAAAA=="})
    (storage / "vdb_chunks.json").write_text(json.dumps({
        "data": chunk_data, "file_hash": "fake",
    }, ensure_ascii=False))
    
    # 7. kv_store_full_entities（真实结构：dict[doc_id] -> 单 entity dict {entity_name, description, source_id}）
    # 真实数据 form 2（不是 form 1 的 {entity_names: list, count}）。
    # 一个 doc_id 对应一个 entity_name（不是 list），description 含语义标记。
    fe_data = {}
    for i, name in enumerate(zombies + normal_regions):
        is_zombie = name in zombies
        desc = "被删除的重复脑区实体之一。<SEP>brain_meta_size:0" if is_zombie else "brain_meta_size:10"
        fe_data[f"doc-{i+1}"] = {
            "entity_name": name,
            "description": desc,
            "source_id": f"brain_{name}",
        }
    (storage / "kv_store_full_entities.json").write_text(json.dumps(fe_data, ensure_ascii=False))

    # 8. kv_store_full_relations（真实结构：dict[doc_id] -> {relation_pairs: list[list], count: int, ...}）
    # 每个 pair 是 [src, tgt, ...] 形式
    pairs = [["知识图谱系统维护", z, "删除操作"] for z in zombies]
    (storage / "kv_store_full_relations.json").write_text(json.dumps({
        "doc-1": {
            "relation_pairs": pairs,
            "count": len(pairs),
            "create_time": "2026-07-06T00:00:00",
            "update_time": "2026-07-06T00:00:00",
            "_id": "doc-1",
        },
    }, ensure_ascii=False))

    # 9. kv_store_relation_chunks（Bug #3 修复：第 8 个存储）
    # 真实结构：dict[key] -> {chunk_ids, count}，key 格式 "src<SEP>tgt"（<SEP> 是 GRAPH_FIELD_SEP 字符串）
    # 僵尸脑区的关系 chunk key 形如 "智家xxx脑区<SEP>知识图谱系统维护"
    rc_data = {}
    for z in zombies:
        # src 是僵尸脑区
        rc_data[f"{z}<SEP>知识图谱系统维护"] = {"chunk_ids": [f"chunk-rel-{z}"], "count": 1}
    for n in normal_regions:
        # 正常脑区的关系 chunk 保留
        rc_data[f"{n}<SEP>知识图谱系统维护"] = {"chunk_ids": [f"chunk-rel-{n}"], "count": 1}
    (storage / "kv_store_relation_chunks.json").write_text(json.dumps(rc_data, ensure_ascii=False))


def test_repair_brainregion_zombies_cleans_all_8_storages(tmp_path):
    """repair_brainregion_zombies 应清理 8 个存储的僵尸脑区残留"""
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
    names = [e["entity_name"] for e in vdb["data"]]
    assert "智家脑区A" not in names
    assert "智家脑区B" not in names
    assert "聊天历史脑区" in names

    # 3. vdb_relationships：涉及僵尸的 edge 已删
    vdb_r = json.loads((tmp_path / "vdb_relationships.json").read_text())
    rel_tgt = [e.get("tgt_id") for e in vdb_r["data"]]
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
    chunk_ids = [e["__id__"] for e in vdb_c["data"]]
    assert "chunk-智家脑区A" not in chunk_ids
    assert "chunk-智家脑区B" not in chunk_ids
    
    # 7. kv_store_full_entities：僵尸 entity 的 doc 已删，正常 entity 保留
    fe = json.loads((tmp_path / "kv_store_full_entities.json").read_text())
    # 真实结构（form 2）：dict[doc_id] -> {entity_name, description, source_id}
    # 僵尸 entity 的 doc 整体被删（不是从 list 移除）
    zombie_docs = [doc_id for doc_id, ent in fe.items()
                   if isinstance(ent, dict) and ent.get("entity_name") in ["智家脑区A", "智家脑区B"]]
    assert len(zombie_docs) == 0, f"僵尸 entity 的 doc 仍存在: {zombie_docs}"
    # 正常 entity 的 doc 保留
    normal_docs = [doc_id for doc_id, ent in fe.items()
                   if isinstance(ent, dict) and ent.get("entity_name") == "聊天历史脑区"]
    assert len(normal_docs) == 1, f"正常 entity 的 doc 应保留 1 个，实际 {len(normal_docs)}"

    # 8. kv_store_relation_chunks（Bug #3 修复）：僵尸关系 chunk 已删，正常关系 chunk 保留
    rc = json.loads((tmp_path / "kv_store_relation_chunks.json").read_text())
    # key 格式 "src<SEP>tgt"，src 是僵尸脑区的 key 应被删
    zombie_rc_keys = [k for k in rc.keys()
                      if any(z in k.split("<SEP>") for z in ["智家脑区A", "智家脑区B"])]
    assert len(zombie_rc_keys) == 0, f"僵尸关系 chunk key 仍存在: {zombie_rc_keys}"
    # 正常脑区的关系 chunk 保留
    normal_rc_keys = [k for k in rc.keys() if "聊天历史脑区" in k.split("<SEP>")]
    assert len(normal_rc_keys) == 1, f"正常关系 chunk 应保留 1 个，实际 {len(normal_rc_keys)}"


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
python -m pytest tests/test_lightrag_semantic_repair.py::test_repair_brainregion_zombies_cleans_all_8_storages -v
```

Expected: FAIL with `ImportError`

### - [ ] Step 3: Write minimal implementation

在 `niu_api/internal/lightrag_repair.py` 新增（顶部需 `import zlib`，与 `lightrag_repair.py` 现有 `import numpy as np` / `import base64` 并列）：

```python
import zlib
import numpy as np
import base64


def _rebuild_vdb_matrix(vdb_data: dict) -> dict:
    """清理 vdb data 后重建 matrix 字段。

    nano-vectordb 的 vdb 顶层字段是 `embedding_dim` + `data` + `matrix`：
    - `embedding_dim`: int，向量维度
    - `data`: list[entry]，每个 entry 含 `__id__` / `entity_name` / `vector`
    - `matrix`: base64 编码的 float32 矩阵，长度 = 4 * embedding_dim * len(data_list)

    `_load_vdb` 会校验 `4 * embedding_dim * len(data_list) == len(matrix_bytes)`。
    删 entry 后 `len(data_list)` 变小，matrix 长度不变，触发 `matrix_size_mismatch` critical。

    本函数在删 entry 后调用，按当前 data_list 重建 matrix：
    - 遍历 data_list 每个 entry 的 `vector` 字段（三层编码：base64(zlib(float16)) 字符串）
    - 解码失败或缺失时用零向量填充（embedding_dim 维度）
    - 拼接为 2D 矩阵，转 float32，base64 编码（单层，无 zlib）后写回 `matrix` 字段

    重要编码差异（审查实测确认）：
    - `vector` 字段：三层编码 base64(zlib(float16 bytes))——参考 lightrag_repair.py _encode_vector (L177-182)
    - `matrix` 字段：单层编码 base64(float32 bytes)——无 zlib 压缩
    本函数读 vector 时用三层解码，写 matrix 时用单层编码。
    """
    embedding_dim = vdb_data.get("embedding_dim", 0)
    data_list = vdb_data.get("data", [])
    if embedding_dim == 0 or not data_list:
        # 空数据，matrix 设空字符串
        vdb_data["matrix"] = ""
        return vdb_data
    vectors = []
    for entry in data_list:
        vec_b64 = entry.get("vector", "") if isinstance(entry, dict) else ""
        if vec_b64:
            try:
                # 三层解码：base64 → zlib → float16 → float32
                raw_bytes = base64.b64decode(vec_b64)
                decompressed = zlib.decompress(raw_bytes)
                vec = np.frombuffer(decompressed, dtype=np.float16).astype(np.float32)
                # 维度对齐（防止 entry vector 跟 embedding_dim 不一致）
                if len(vec) != embedding_dim:
                    vec = np.zeros(embedding_dim, dtype=np.float32)
                vectors.append(vec)
            except Exception:
                # 解码失败，用零向量填充
                vectors.append(np.zeros(embedding_dim, dtype=np.float32))
        else:
            vectors.append(np.zeros(embedding_dim, dtype=np.float32))
    matrix = np.array(vectors, dtype=np.float32)
    # matrix 字段是 base64(float32) 单层编码（无 zlib）
    vdb_data["matrix"] = base64.b64encode(matrix.tobytes()).decode("ascii")
    return vdb_data


def repair_brainregion_zombies() -> dict[str, Any]:
    """语义 repair: 完整清理 8 个存储的僵尸脑区残留。

    真相源：脑区 description 的语义标记（"被删除"等）——不是 GraphML，
    因为 GraphML 本身可能被污染（含僵尸 node）。

    清理范围（8 存储）：
    1. GraphML node + cascade edge（用 ET 删 node，edge 自然 cascade）
    2. vdb_entities 向量（删 entity_name 匹配的向量）
    3. vdb_relationships 向量（删 src_id 或 tgt_id 是僵尸的向量）
    4. kv_store_entity_chunks 的脑区 key
    5. kv_store_text_chunks 的脑区专属 chunk（source_id=brain_xxx）
    6. vdb_chunks 的脑区专属 chunk 向量
    7. kv_store_full_entities / full_relations 的文档级索引（从列表中移除僵尸名）
    8. kv_store_relation_chunks 的僵尸关系 chunk（key 格式 "src<SEP>tgt"，src 或 tgt 是僵尸则删——Bug #3 修复）

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

    storage_dir = _storage_dir()
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

    # =====================================================================
    # Bug I 修复：事务式保护——先全部读入内存，在内存中修改，最后统一写盘
    # 原 8 个存储清理用独立 try/except，中间失败会状态不一致（半写盘）。
    # 改为：所有清理在内存中完成，全部成功后才统一写盘；写盘失败则内存修改丢失
    # （不会半写盘），返回 unrecoverable。
    # =====================================================================

    # 2. 读入所有需要修改的存储到内存
    try:
        graphml_path = storage_dir / _GRAPHML_FILE
        graphml_tree = ET.parse(graphml_path)
        graphml_root = graphml_tree.getroot()
        graphml_graph = graphml_root.find("graph")
        if graphml_graph is None:
            for child in graphml_root:
                tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if tag == "graph":
                    graphml_graph = child
                    break

        vdb_e_path = storage_dir / "vdb_entities.json"
        vdb_e = json.loads(vdb_e_path.read_text()) if vdb_e_path.exists() else {"data": [], "embedding_dim": 0, "matrix": ""}

        vdb_r_path = storage_dir / "vdb_relationships.json"
        vdb_r = json.loads(vdb_r_path.read_text()) if vdb_r_path.exists() else {"data": [], "embedding_dim": 0, "matrix": ""}

        ec_path = storage_dir / "kv_store_entity_chunks.json"
        ec = json.loads(ec_path.read_text()) if ec_path.exists() else {}

        tc_path = storage_dir / "kv_store_text_chunks.json"
        tc = json.loads(tc_path.read_text()) if tc_path.exists() else {}

        vdb_c_path = storage_dir / "vdb_chunks.json"
        vdb_c = json.loads(vdb_c_path.read_text()) if vdb_c_path.exists() else {"data": [], "embedding_dim": 0, "matrix": ""}

        fe_path = storage_dir / "kv_store_full_entities.json"
        fe = json.loads(fe_path.read_text()) if fe_path.exists() else {}

        fr_path = storage_dir / "kv_store_full_relations.json"
        fr = json.loads(fr_path.read_text()) if fr_path.exists() else {}

        # Bug #3 修复：遗漏的第 8 个存储——僵尸脑区的关系 chunk
        # 真实数据 kv_store_relation_chunks.json 含 16 个僵尸脑区的关系 chunk，
        # key 格式 "智家xxx脑区<SEP>知识图谱系统维护"（src<SEP>tgt，<SEP> 是 GRAPH_FIELD_SEP 字符串）。
        # 不清理会残留导致 check_relation_chunks_dangling 报 16 个 major error。
        rc_path = storage_dir / "kv_store_relation_chunks.json"
        rc = json.loads(rc_path.read_text()) if rc_path.exists() else {}
    except Exception as e:
        return {"status": "unrecoverable", "reason": f"读入存储失败: {e}"}

    # 3. 在内存中修改（不写盘）——所有清理逻辑
    orphan_chunk_ids: list[str] = []

    # 3.1 GraphML node + cascade edge
    removed_nodes = 0
    removed_edges = 0
    if graphml_graph is not None:
        edges_to_remove = []
        for edge in list(graphml_graph):
            tag = edge.tag.split("}")[-1] if "}" in edge.tag else edge.tag
            if tag != "edge":
                continue
            src = edge.get("source", "")
            tgt = edge.get("target", "")
            if src in zombie_names or tgt in zombie_names:
                edges_to_remove.append(edge)
        for edge in edges_to_remove:
            graphml_graph.remove(edge)
            removed_edges += 1
        nodes_to_remove = []
        for node in list(graphml_graph):
            tag = node.tag.split("}")[-1] if "}" in node.tag else node.tag
            if tag != "node":
                continue
            if node.get("id") in zombie_names:
                nodes_to_remove.append(node)
        for node in nodes_to_remove:
            graphml_graph.remove(node)
            removed_nodes += 1
    details["graphml"] = {"removed_nodes": removed_nodes, "removed_edges": removed_edges}

    # 3.2 vdb_entities
    before_e = len(vdb_e.get("data", []))
    vdb_e["data"] = [
        entry for entry in vdb_e.get("data", [])
        if entry.get("entity_name") not in zombie_names
    ]
    _rebuild_vdb_matrix(vdb_e)
    details["vdb_entities"] = {"before": before_e, "after": len(vdb_e["data"])}

    # 3.3 vdb_relationships
    before_r = len(vdb_r.get("data", []))
    vdb_r["data"] = [
        entry for entry in vdb_r.get("data", [])
        if entry.get("src_id") not in zombie_names and entry.get("tgt_id") not in zombie_names
    ]
    _rebuild_vdb_matrix(vdb_r)
    details["vdb_relationships"] = {"before": before_r, "after": len(vdb_r["data"])}

    # 3.4 kv_store_entity_chunks
    before_ec = len(ec)
    for zname in zombie_names:
        ec.pop(zname, None)
    details["entity_chunks"] = {"before": before_ec, "after": len(ec)}

    # 3.5 kv_store_text_chunks 的脑区专属 chunk
    before_tc = len(tc)
    tc_to_remove = []
    for chunk_id, meta in tc.items():
        if not isinstance(meta, dict):
            continue
        sid = meta.get("source_id", "") or meta.get("full_doc_id", "")
        if sid.startswith("brain_"):
            brain_name = sid[len("brain_"):]
            if brain_name in zombie_names:
                tc_to_remove.append(chunk_id)
                orphan_chunk_ids.append(chunk_id)
    for cid in tc_to_remove:
        tc.pop(cid, None)
    details["text_chunks"] = {"before": before_tc, "after": len(tc), "removed": len(tc_to_remove)}

    # 3.6 vdb_chunks 的对应 chunk 向量
    before_vc = len(vdb_c.get("data", []))
    orphan_set = set(orphan_chunk_ids)
    vdb_c["data"] = [
        entry for entry in vdb_c.get("data", [])
        if entry.get("__id__") not in orphan_set
    ]
    _rebuild_vdb_matrix(vdb_c)
    details["vdb_chunks"] = {"before": before_vc, "after": len(vdb_c["data"])}

    # 3.7 kv_store_full_entities
    # 真实结构（form 2）：dict[doc_id] -> {entity_name, description, source_id}（单 entity 文档）
    cleaned_fe = 0
    fe_docs_to_remove = []
    for doc_id, ent_data in fe.items():
        if not isinstance(ent_data, dict):
            continue
        # form 1: {entity_names: list, count}（兼容历史 form）
        if "entity_names" in ent_data and isinstance(ent_data["entity_names"], list):
            before = len(ent_data["entity_names"])
            ent_data["entity_names"] = [
                n for n in ent_data["entity_names"] if n not in zombie_names
            ]
            if "count" in ent_data:
                ent_data["count"] = len(ent_data["entity_names"])
            cleaned_fe += before - len(ent_data["entity_names"])
        # form 2: {entity_name: str, description, source_id} - 单 entity 文档（真实数据用此 form）
        elif "entity_name" in ent_data and ent_data.get("entity_name") in zombie_names:
            fe_docs_to_remove.append(doc_id)
    for doc_id in fe_docs_to_remove:
        fe.pop(doc_id, None)
    details["full_entities"] = {"cleaned_count": cleaned_fe, "removed_docs": len(fe_docs_to_remove)}

    # 3.8 kv_store_full_relations
    # 真实结构：dict[doc_id] -> {relation_pairs: list[list], count, create_time, update_time, _id}
    # 每个 pair 是 [src, tgt, ...] 2 元素以上形式
    cleaned_fr = 0
    for doc_id, rel_data in fr.items():
        if not isinstance(rel_data, dict):
            continue
        pairs = rel_data.get("relation_pairs", [])
        if isinstance(pairs, list):
            before = len(pairs)
            rel_data["relation_pairs"] = [
                p for p in pairs
                if isinstance(p, list) and len(p) >= 2
                and p[0] not in zombie_names and p[1] not in zombie_names
            ]
            if "count" in rel_data:
                rel_data["count"] = len(rel_data["relation_pairs"])
            cleaned_fr += before - len(rel_data["relation_pairs"])
    details["full_relations"] = {"cleaned_count": cleaned_fr}

    # 3.9 kv_store_relation_chunks（Bug #3 修复：遗漏的第 8 个存储）
    # 真实数据：dict[key] -> {chunk_ids, count}，key 格式 "src<SEP>tgt"（<SEP> 是 GRAPH_FIELD_SEP 字符串）
    # 16 个僵尸脑区的关系 chunk key 形如 "智家xxx脑区<SEP>知识图谱系统维护"——src 或 tgt 是僵尸脑区则删
    before_rc = len(rc)
    rc_keys_to_remove = []
    for key in list(rc.keys()):
        # key 格式 "src<SEP>tgt"（<SEP> 是 GRAPH_FIELD_SEP 字符串）
        if "<SEP>" in key:
            parts = key.split("<SEP>")
            # src 或 tgt 是僵尸脑区则删
            if any(p in zombie_names for p in parts):
                rc_keys_to_remove.append(key)
    for key in rc_keys_to_remove:
        rc.pop(key, None)
    details["relation_chunks"] = {
        "before": before_rc,
        "after": len(rc),
        "removed": len(rc_keys_to_remove),
    }

    # 4. 全部内存修改成功后，统一写盘（事务式）
    try:
        graphml_tree.write(graphml_path, xml_declaration=True, encoding="utf-8")
        vdb_e_path.write_text(json.dumps(vdb_e, ensure_ascii=False))
        vdb_r_path.write_text(json.dumps(vdb_r, ensure_ascii=False))
        ec_path.write_text(json.dumps(ec, ensure_ascii=False))
        tc_path.write_text(json.dumps(tc, ensure_ascii=False))
        vdb_c_path.write_text(json.dumps(vdb_c, ensure_ascii=False))
        if fe_path.exists() or fe:  # 只在原文件存在或有内容时写
            fe_path.write_text(json.dumps(fe, ensure_ascii=False))
        if fr_path.exists() or fr:
            fr_path.write_text(json.dumps(fr, ensure_ascii=False))
        if rc_path.exists() or rc:  # Bug #3：写回清理后的 relation_chunks
            rc_path.write_text(json.dumps(rc, ensure_ascii=False))
    except Exception as e:
        # 写盘失败：内存修改已丢失（不会半写盘），但部分文件可能已写——返回 unrecoverable
        return {"status": "unrecoverable", "reason": f"写盘失败（部分文件可能已写）: {e}"}

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
git commit -m "feat(repair): 新增 repair_brainregion_zombies 完整 8 存储清理僵尸脑区

用 description 语义标记作为真相源（不是 GraphML），清理：
1. GraphML node + cascade edge
2. vdb_entities 向量
3. vdb_relationships 涉及僵尸的向量
4. kv_store_entity_chunks 的脑区 key
5. kv_store_text_chunks 的脑区专属 chunk
6. vdb_chunks 的脑区专属 chunk 向量
7. kv_store_full_entities / full_relations 文档级索引
8. kv_store_relation_chunks 的僵尸关系 chunk（Bug #3 修复：遗漏的第 8 个存储）
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
    "entity_chunks_source_id_mismatch": "brainregion_zombies",  # 新增（source_id 不一致也可能是僵尸）
    "chunk_shared_by_too_many_entities": "brainregion_zombies",  # 新增（共享 chunk 异常）
    "brainregion_orphan_chunks": "brainregion_zombies",  # 新增（孤儿 chunk + chunk 侧僵尸标记）
    "vdb_entities_orphan": "vdb_entities",  # 反向孤儿走 vdb_entities 重建
    # ... 原有项保留
}
```

> 注意：`brainregion_size_mismatch` 已从 check 列表删除（见 Task 3），
> 此处不再映射。

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
    """fixture：测试前恢复真实数据（含 16 个僵尸），测试后恢复测试前状态。
    
    使用 try/finally 保护：测试失败时也能恢复用户数据，避免污染真实环境。
    """
    # 保存当前状态
    snapshot = STORAGE_DIR.parent / f"lightrag_storage_e2e_snapshot_{int(time.time())}"
    if STORAGE_DIR.exists():
        shutil.copytree(STORAGE_DIR, snapshot)

    # 恢复 16 个僵尸脑区的真实数据
    shutil.rmtree(STORAGE_DIR, ignore_errors=True)
    shutil.copytree(BACKUP_DIR, STORAGE_DIR)

    try:  # try/finally 保护：测试失败也确保恢复用户数据
        yield
    finally:
        # 测试后恢复，无论测试是否失败都执行
        shutil.rmtree(STORAGE_DIR, ignore_errors=True)
        if snapshot.exists():
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
        # 优雅停止逻辑移到 finally 块（确保异常时也能 shutdown + kill fallback，Bug J 修复）
    finally:
        # 优雅停止：先 SIGTERM，失败后 SIGKILL fallback（Bug J 修复）
        try:
            requests.post("http://127.0.0.1:9876/api/shutdown", timeout=5)
        except Exception:
            pass
        time.sleep(3)

        # 先 SIGTERM
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            # SIGTERM 失败（进程不响应），用 SIGKILL fallback
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass  # SIGKILL 后仍不退出——极端情况，记录但不阻塞测试

        # 额外清理：杀残留子进程（Electron / niu-api / mcp 等）
        # 用 psutil 杀进程树（如果可用），否则用 pkill fallback
        import signal
        try:
            import psutil
            try:
                parent = psutil.Process(proc.pid)
                for child in parent.children(recursive=True):
                    try:
                        child.send_signal(signal.SIGTERM)
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                try:
                    parent.send_signal(signal.SIGKILL)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            except psutil.NoSuchProcess:
                pass  # 进程已退出
        except ImportError:
            # psutil 不可用，用 pkill 兜底（只杀 niu 和 Electron，不杀其他）
            subprocess.run(["pkill", "-9", "-f", "niu"], check=False, timeout=10)
            subprocess.run(["pkill", "-9", "-f", "Electron"], check=False, timeout=10)
    
    # 读 stdout 日志
    output = proc.stdout.read().decode("utf-8", errors="replace")
    
    # 16 个僵尸脑区的特征标记（来自真实数据——description 含"被删除"且脑区名含"智家/家居/居家"）
    # 必须全部检查，避免只查"智家"漏掉"家居智能应用脑区"等
    ZOMBIE_MARKERS = [
        "被删除的重复脑区实体之一",
        "智家全维资料脑区",
        "智家使用运维脑区",
        "智家打理相关脑区",
        "智家综合事务脑区",
        "家居智能应用脑区",
        "家居智能实践脑区",
        "家庭智能物联脑区",
        "家庭智能运维脑区",
        "个人智家档案库脑区",
        "个人智家运营脑区",
        "个人智用空间脑区",
        "个人智能库脑区",
        "智能家居内容脑区",
        "智能家居实践区脑区",
        "智能家居管理脑区",
        "居家智能脑区",
    ]
    for marker in ZOMBIE_MARKERS:
        assert marker not in output, (
            f"启动日志里仍出现僵尸脑区标记: {marker}\n日志末尾:\n{output[-2000:]}"
        )
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

## Task 13: [P0] 修复 `_refresh_activation_manager` 覆盖率检查过严

**Files:**
- Modify: `agent/injector/region_sync.py:383-391`

### 背景

审查发现 region_sync 的 `_refresh_activation_manager` 在读取脑区"包含"edge 后做"覆盖率检查"——如果覆盖率 < 50% 直接 return，认为"图未就绪或读取失败"。但真实数据里 17 个非预置脑区没有"包含"edge 是正常状态（这些脑区是历史 dissolve 流程产生的中间态），覆盖率自然 < 50%，导致 activation_manager 永远为 None，触发 forced sync 死循环。

### - [ ] Step 1: 先用 gitnexus_impact 分析影响范围

```bash
# 在改代码前必须分析 blast radius
# 用 gitnexus_impact({target: "_refresh_activation_manager", direction: "upstream"})
```

### - [ ] Step 2: 读取现有代码

```bash
sed -n '370,400p' REDACTED_USER_PATH/tools/ai-bot/agent/injector/region_sync.py
```

确认 L383-391 是覆盖率检查代码（类似 `if coverage < 0.5: return`）。

### - [ ] Step 3: 修改代码

删除 L383-391 的覆盖率检查（`if coverage < 0.5: ...` 整段）。保留 L376-381 的非空检查（图未就绪或读取失败时返回空）。

```python
# 删除前（L383-391 大致结构）：
# if coverage < 0.5:
#     logger.warning(f"activation_manager coverage too low: {coverage}")
#     return  # ← 这里直接 return，导致 activation_mgr 永远 None

# 删除后：覆盖率低不视为失败，继续构建 activation_manager
```

注意：删除覆盖率检查后，activation_manager 会用现有数据（哪怕覆盖率为 0）构建，不再触发 forced sync 死循环。

### - [ ] Step 3.5: 删除或重写现有测试 test_refresh_activation_manager_skips_when_coverage_too_low

**文件**：`tests/test_region_sync.py:319-353`

该测试验证覆盖率 < 50% 时 `initialize_from_regions` 不被调用（`mock_initialize.assert_not_called()`）。
删除覆盖率检查后，`initialize_from_regions` 会被调用，测试 `assert_not_called()` 失败。

重写为：验证覆盖率低时仍构建 activation_manager（不再跳过）：

```python
def test_refresh_activation_manager_builds_even_when_coverage_low(...):
    # ... setup with low coverage (e.g., 0% 覆盖率) ...
    sync._refresh_activation_manager(...)
    # 验证 activation_manager 被构建（不再跳过）
    mock_initialize.assert_called()
```

或者直接删除该测试（如果重写太复杂——重写需要构造低覆盖率 fixture，可能引入新 bug）。

建议方案：**直接删除该测试**（删除 L319-353 整段），原因：
1. 覆盖率检查已删除，原测试逻辑无对应实现
2. 重写后的新测试应该验证"低覆盖率也能构建"，这跟 Task 11 端到端启动测试语义重复
3. 删除比重写风险低，避免新测试引入新 bug

操作命令：

```bash
# 先 Read 确认行号（实际行号可能因前期修改偏移）
sed -n '315,360p' tests/test_region_sync.py

# 删除整个测试函数（用 git diff 确认范围正确）
# 推荐用编辑器或 sed 删除 L319-353，删除后跑测试确认无残留引用
```

删除后跑测试确认：

```bash
python -m pytest tests/test_region_sync.py -v 2>&1 | tail -30
```

Expected: 全部 PASS（`test_refresh_activation_manager_skips_when_coverage_too_low` 不再存在，其他测试不受影响）

### - [ ] Step 4: 跑现有 region_sync 测试

```bash
python -m pytest tests/test_region_sync*.py -v 2>&1 | tail -30
```

Expected: 全部 PASS（如果有 fail，需要根据失败信息调整）

### - [ ] Step 5: Commit

```bash
git add agent/injector/region_sync.py
git commit -m "fix(region_sync): 删除 _refresh_activation_manager 覆盖率检查过严

17 个非预置脑区没有'包含'edge 是数据正常状态，不应判为'读取失败'。
覆盖率检查 < 50% 直接 return 导致 activation_mgr 永远 None，
触发 forced sync 死循环。删除该检查，让 activation_mgr 用现有数据构建。

P0 修复：region_sync 死循环根因之一。
"
```

---

## Task 14: [P0] 修复 `_get_brain_injector` forced sync 死循环

**Files:**
- Modify: `agent/runner.py:1695-1723`

### 背景

`_get_brain_injector` 在 activation_manager 为 None 时触发 forced sync（`run_sync()`），失败后清空 cache。下次再调用 `_get_brain_injector` 时 activation_manager 仍是 None，又触发 forced sync——死循环。

### - [ ] Step 1: 先用 gitnexus_impact 分析影响范围

```bash
# 用 gitnexus_impact({target: "_get_brain_injector", direction: "upstream"})
```

### - [ ] Step 2: 读取现有代码

```bash
sed -n '1690,1730p' REDACTED_USER_PATH/tools/ai-bot/agent/runner.py
```

确认 L1695-1723 是 forced sync 调用 + 失败后清空 cache 的逻辑。

### - [ ] Step 3: 修改代码

加失败冷却时间（5 分钟），失败后 5 分钟内不再触发 forced sync：

**注意属性名**：runner.py 真实代码用 `self._cached_activation_mgr`（L616）+ 模块级 `get_activation_mgr()` 函数（L1681、L1702），不是 `self._activation_manager`。修改前先 Read 确认。

```python
# 在 __init__ 或类属性加 instance 变量：
# self._last_forced_sync_fail_time: float = 0.0
# self._forced_sync_running = threading.Event()  # 见 Task 16 Bug E

# 在 _get_brain_injector 触发 forced sync 前加冷却检查：
import time

FORCED_SYNC_COOLDOWN_SECONDS = 300  # 5 分钟

def _get_brain_injector(self, ...):
    # ... 现有逻辑 ...
    # Bug #4 修复：读全局单例 get_activation_mgr()，不读 cache self._cached_activation_mgr。
    # cache 被清空后会误判 activation_mgr 未就绪，触发死循环。
    # get_activation_mgr() 定义在 agent/brain_tools.py:39，runner.py 通过
    # `from agent.brain_tools import get_activation_mgr` import 后调用。
    _activation_mgr = get_activation_mgr()
    if _activation_mgr is None and self._brain_adapter._get_rag() is not None:
        # 冷却检查：失败后 5 分钟内不重试
        if time.time() - self._last_forced_sync_fail_time < FORCED_SYNC_COOLDOWN_SECONDS:
            logger.debug("forced sync in cooldown, skip")
            return None
        try:
            # Bug #5 修复：run_sync 是 RegionSync 实例方法，不是模块级函数。
            # 通过 get_region_sync().run_sync() 调用（get_region_sync 定义在
            # agent/injector/region_sync.py）。
            from agent.injector.region_sync import get_region_sync
            get_region_sync().run_sync()  # forced sync（Task 16 会改为异步触发）
            # 重新读取 activation_manager（用真实属性名）
            self._cached_activation_mgr = get_activation_mgr()
            _activation_mgr = self._cached_activation_mgr
            # Bug #7 修复：成功后重置冷却时间（_last_forced_sync_fail_time = 0.0）
            # 否则第一次成功后 _last_forced_sync_fail_time 保持 0.0，
            # 下次调用 time.time() - 0.0 < 300 永远 True，冷却永远不解除。
            self._last_forced_sync_fail_time = 0.0
        except Exception as e:
            logger.error(f"forced sync failed: {e}")
            self._last_forced_sync_fail_time = time.time()  # 记录失败时间
            # 清空 cache（保留原逻辑——若 cache 是 dict 则清空键值）
            ...
            return None
    # ...
```

> **Bug #6 重要说明：runner.py 顶部需要加 `import time`**
> runner.py 顶部不 import time，但 Task 14 用 `time.time()`。
> 实施者必须先检查：
> ```bash
> grep "^import time" REDACTED_USER_PATH/tools/ai-bot/agent/runner.py
> ```
> 如果没有输出，在 runner.py 顶部加 `import time`（与现有 `import threading` 等并列）。

> **属性名重要提示**：
> - `self._cached_activation_mgr` 是真实属性名（runner.py L616）
> - `get_activation_mgr()` 是真实函数（定义在 `agent/brain_tools.py:39`，runner.py 通过 `from agent.brain_tools import get_activation_mgr` import 后调用，L1681、L1702 使用）
> - `run_sync()` 是 `RegionSync` 实例方法，不是模块级函数，必须通过 `get_region_sync().run_sync()` 调用（`get_region_sync` 定义在 `agent/injector/region_sync.py`）
> - 不要用 `self._activation_manager`——这是错误的属性名，会让实施者写代码时引用不存在的属性导致 AttributeError

### - [ ] Step 4: 跑 runner 测试

```bash
python -m pytest tests/test_runner*.py -v 2>&1 | tail -30
```

Expected: 全部 PASS

### - [ ] Step 5: Commit

```bash
git add agent/runner.py
git commit -m "fix(runner): _get_brain_injector forced sync 加 5 分钟失败冷却

forced sync 失败后清空 cache，下次又触发 forced sync——死循环。
加冷却时间：失败后 5 分钟内不再触发，避免短时间内反复重试。

P0 修复：region_sync 死循环根因之二。
"
```

> **Bug E 提示（实施者必读）**：
> Task 16 会引入 `self._forced_sync_running = threading.Event()` 标志避免并发启动多个 forced sync daemon 线程。
> 实施者在 Task 14 阶段**只需要**引入 `self._last_forced_sync_fail_time: float = 0.0`。
> `self._forced_sync_running` 在 Task 16 Step 3 加（同时要在 `__init__` 或类属性初始化）。
> 如果 Task 14 实施时还没加 `_forced_sync_running`，冷却检查（`if time.time() - self._last_forced_sync_fail_time < ...`）已经能避免死循环——但并发问题（多个 daemon 线程同时跑 sync）需要 Task 16 的 `_forced_sync_running` 标志解决。

---

## Task 15: [P0] 降低 `shrink_threshold` 从 100 到 10

**Files:**
- Modify: `niu_api/internal/region_manager.py:1016`（`dissolve_shrunk_regions` 函数参数默认值）
- Modify: `agent/injector/region_sync.py:41`（`REGION_CONFIG_DEFAULTS["shrink_threshold"]` 字典 key）

### 背景

`shrink_threshold=100` 让正常小脑区（成员数 < 100）被判萎缩（`shrink_count` 累加），dissolve 流程跑到 `shrink_count=1` 中间态被中断后就成僵尸。真实数据里很多正常脑区成员数 < 100，不应判萎缩。

### - [ ] Step 1: 先用 gitnexus_impact 分析影响范围

```bash
# 用 gitnexus_impact({target: "dissolve_shrunk_regions", direction: "upstream"})
# 或 gitnexus_impact({target: "REGION_CONFIG_DEFAULTS", direction: "upstream"})
```

### - [ ] Step 2: 读取现有代码

```bash
sed -n '1010,1020p' REDACTED_USER_PATH/tools/ai-bot/niu_api/internal/region_manager.py
sed -n '38,46p' REDACTED_USER_PATH/tools/ai-bot/agent/injector/region_sync.py
```

确认：
- `region_manager.py:1016` 是 `dissolve_shrunk_regions(self, shrink_threshold: int = 100, ...)` 参数默认值
- `region_sync.py:41` 是 `REGION_CONFIG_DEFAULTS["shrink_threshold"] = 100` 字典 key（不是 `SHRINK_THRESHOLD = 100` 常量）

### - [ ] Step 3: 修改代码

`shrink_threshold` 从 100 降到 10（或改为相对阈值——成员数 < 平均成员数 * 0.1 才判萎缩。本 Task 先用绝对值 10）：

**注意真实代码属性**：
- `agent/injector/region_sync.py:41` 是 `REGION_CONFIG_DEFAULTS["shrink_threshold"] = 100`（字典 key），不是 `SHRINK_THRESHOLD = 100` 常量
- `niu_api/internal/region_manager.py:1016` 是 `dissolve_shrunk_regions(self, shrink_threshold: int = 100, ...)`（参数默认值）

```python
# niu_api/internal/region_manager.py:1016
# 函数签名参数默认值
- def dissolve_shrunk_regions(self, shrink_threshold: int = 100, ...) -> dict:
+ def dissolve_shrunk_regions(self, shrink_threshold: int = 10, ...) -> dict:

# agent/injector/region_sync.py:41
# 字典默认配置（不是常量）
- REGION_CONFIG_DEFAULTS["shrink_threshold"] = 100
+ REGION_CONFIG_DEFAULTS["shrink_threshold"] = 10  # 成员数 < 10 才判萎缩（原 100 误判正常小脑区）
```

> **常量名重要提示**：
> - 真实代码没有 `SHRINK_THRESHOLD = 100` 这种模块级常量
> - `region_sync.py:41` 是字典 key `REGION_CONFIG_DEFAULTS["shrink_threshold"]`
> - `region_manager.py:1016` 是函数参数默认值 `shrink_threshold: int = 100`
> - 实施者必须 Read 这两处确认，不要按 `SHRINK_THRESHOLD` 常量名找——会找不到

### - [ ] Step 4: 跑 region_manager 测试

```bash
python -m pytest tests/test_region_manager*.py -v 2>&1 | tail -30
```

Expected: 全部 PASS

### - [ ] Step 5: Commit

```bash
git add niu_api/internal/region_manager.py agent/injector/region_sync.py
git commit -m "fix(region): shrink_threshold 从 100 降到 10

shrink_threshold=100 让正常小脑区（成员数 < 100）被判萎缩，
dissolve 跑到 shrink_count=1 中断后成僵尸。降到 10 避免误判。

P0 修复：僵尸脑区形成根因。
"
```

---

## Task 16: [P0] forced sync 改异步触发（避免同步阻塞 43 秒）

**Files:**
- Modify: `agent/runner.py:1709`

### 背景

`_get_brain_injector` 触发 forced sync 时同步调用 `run_sync()`，阻塞主线程 43 秒（实测），导致程序启动卡死。改为异步触发——启动后台线程跑 `run_sync`，主线程立即返回 None（让用户感受到程序已启动，后台慢慢 sync）。

### - [ ] Step 1: 先用 gitnexus_impact 分析影响范围

```bash
# 用 gitnexus_impact({target: "run_sync", direction: "upstream"})
```

### - [ ] Step 2: 读取现有代码

```bash
sed -n '1705,1725p' REDACTED_USER_PATH/tools/ai-bot/agent/runner.py
```

确认 L1709 是同步 `run_sync()` 调用。

### - [ ] Step 3: 修改代码

**Bug E 修复（CRITICAL）：并发启动多个 forced sync 问题**

异步线程失败后 set `_last_forced_sync_fail_time`，但主线程立即返回 None，下一次调用时冷却检查基于"上次失败时间"——但异步线程可能还在跑（5 分钟内多次调用会启动多个 daemon 线程）。

**修复方案**：加 `self._forced_sync_running` 标志（threading.Event），避免并发启动多个 forced sync 线程。

**初始化（在 `__init__` 或类属性加）**：

```python
import threading

# runner.py 类初始化时加（如果 __init__ 已有 _last_forced_sync_fail_time，加在旁边）
self._forced_sync_running = threading.Event()
self._last_forced_sync_fail_time: float = 0.0  # Task 14 已引入
```

改为异步触发（启动线程跑 `run_sync`，主线程立即返回 None）：

**注意属性名**：runner.py 真实代码用 `self._cached_activation_mgr`（L616）+ `get_activation_mgr()` 函数（定义在 `agent/brain_tools.py:39`，runner.py 通过 `from agent.brain_tools import get_activation_mgr` import 后调用），不是 `self._activation_manager`。`run_sync` 是 `RegionSync` 实例方法，必须通过 `get_region_sync().run_sync()` 调用（`get_region_sync` 定义在 `agent/injector/region_sync.py`）。

```python
import threading

def _get_brain_injector(self, ...):
    # ... 现有逻辑 ...
    # Bug #4 修复：读全局单例 get_activation_mgr()，不读 cache self._cached_activation_mgr。
    _activation_mgr = get_activation_mgr()
    if _activation_mgr is None and self._brain_adapter._get_rag() is not None:
        # 冷却检查（Task 14 已加）
        if time.time() - self._last_forced_sync_fail_time < FORCED_SYNC_COOLDOWN_SECONDS:
            return None
        # 检查是否正在运行（Bug E 修复：避免并发启动多个 forced sync daemon 线程）
        if self._forced_sync_running.is_set():
            logger.debug("[BrainInjector] forced sync already running, skipping")
            return None
        # 异步触发 forced sync（不阻塞主线程）
        self._forced_sync_running.set()
        def _run_forced_sync():
            try:
                # Bug #5 修复：run_sync 是 RegionSync 实例方法，通过 get_region_sync().run_sync() 调用
                from agent.injector.region_sync import get_region_sync
                get_region_sync().run_sync()
                # 成功后刷新 activation_manager（用真实属性名 + 函数）
                self._cached_activation_mgr = get_activation_mgr()
                # Bug #7 修复：成功后重置冷却时间，避免下次调用 time.time() - 0.0 < 300 永远 True
                self._last_forced_sync_fail_time = 0.0
            except Exception as e:
                logger.error(f"forced sync failed: {e}")
                self._last_forced_sync_fail_time = time.time()
            finally:
                self._forced_sync_running.clear()
        threading.Thread(target=_run_forced_sync, daemon=True, name="forced-sync").start()
        return None  # 主线程立即返回，不阻塞
    # ...
```

注意：
- 异步触发后，`self._cached_activation_mgr` 在后台 sync 完成后才就绪。期间 `_get_brain_injector` 返回 None，主流程继续（不阻塞启动）。
- 下一次 `_get_brain_injector` 调用时（冷却期过后）会读到就绪的 activation_manager。
- Bug E 修复：用 `self._forced_sync_running`（threading.Event）避免 5 分钟内多次调用启动多个 daemon 线程。

### - [ ] Step 4: 跑 runner 测试

```bash
python -m pytest tests/test_runner*.py -v 2>&1 | tail -30
```

Expected: 全部 PASS

### - [ ] Step 5: 端到端验证启动不阻塞

```bash
# 启动 ./niu，观察是否 5 秒内 API ready
./niu &
sleep 5
curl http://127.0.0.1:9876/health
# 应该返回 {"status":"ok"}，而不是卡 43 秒
```

### - [ ] Step 6: Commit

```bash
git add agent/runner.py
git commit -m "fix(runner): forced sync 改异步触发，避免同步阻塞 43 秒

forced sync 同步调用 run_sync 阻塞主线程 43 秒，程序启动卡死。
改为启动 daemon 线程跑 run_sync，主线程立即返回 None。
后台 sync 完成后 activation_manager 就绪，下次调用读到。

P0 修复：程序启动卡死根因。
"
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
2. `shrink_threshold=100` 误判正常小脑区（成员数 < 100）为萎缩，dissolve 流程跑到 shrink_count=1
3. dissolve 被中断（进程重启、sync 没跑完等），僵尸脑区卡在"shrink_count=1 中间态"——description 含标记，但 GraphML node 仍存在
4. LightRAG adelete_by_entity 只删 3 个存储，留下 5 个存储的残留

## 新设计原则

1. **语义维度检测**：description 语义标记、跨存储交叉验证、反向索引异常
2. **跨存储交叉验证**：不只 A 引用 B，还验证字段一致
3. **完整 8 存储清理**：GraphML + vdb_entities + vdb_relationships + entity_chunks +
   text_chunks + vdb_chunks + full_entities/full_relations + relation_chunks（Bug #3 修复）
4. **验证标准升级**：不只 check_all 返回 ok，还要程序启动正常运行

## P0 遗漏修复（与本次计划一并完成）

审查发现 4 个 P0 问题不是删除工具 bug，而是 region_sync/runner/region_manager 的设计缺陷，
与本次检查+修复工具一并修复：

1. `_refresh_activation_manager` 覆盖率 < 50% 直接 return（Task 13：删除覆盖率检查）
2. `_get_brain_injector` forced sync 死循环（Task 14：加 5 分钟失败冷却）
3. `shrink_threshold=100` 太高（Task 15：降到 10，僵尸脑区形成根因）
4. forced sync 同步阻塞 43 秒（Task 16：改异步触发）

## 5 项新语义 check

1. check_brainregion_semantic_zombie - description 含"被删除"标记
2. check_entity_chunks_source_id_mismatch - entity_chunks 跟 GraphML d3 source_id 不一致
3. check_chunk_shared_by_too_many_entities - 一个 chunk 被过多 entity 共享
4. check_vdb_entities_orphan - vdb 有向量但 GraphML 没 node（反向孤儿，防御性）
5. check_brainregion_orphan_chunks - text_chunks 有 brain_xxx 但 GraphML 没 brain_xxx，或 chunk content 含"被删除"标记

> 注：原计划有 6 项，`check_brainregion_size_mismatch` 因在真实数据上无效（16 个僵尸
> brain_meta_size:0 + 实际 0 条包含 edge 一致）已删除，见 Task 3。

## 5 项新语义 repair

1. repair_brainregion_zombies - 完整 8 存储清理僵尸脑区
2-5. 通过 repair_all 调用链集成

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

1. ✅ `tests/test_lightrag_semantic_integrity.py` 全部通过（5 项 check + _load_graphml 扩展）
2. ✅ `tests/test_lightrag_semantic_repair.py` 全部通过（repair_brainregion_zombies + repair_all 集成）
3. ✅ `tests/test_lightrag_e2e_semantic.py` 全部通过（真实数据 4 阶段验证）
4. ✅ 真实数据 `check_all()` 在 16 个僵尸脑区数据上返回 `ok=False`
5. ✅ `repair_all()` 清理后 16 个僵尸脑区在所有 8 个存储中完全消失
6. ✅ 启动 `./niu` 后日志不含 16 个僵尸脑区名（智家全维资料脑区等）+ "被删除的重复脑区" + "activation_mgr still None"
7. ✅ 启动后 region_sync 一次 sync 完成（不卡 dissolve，不进入 forced sync 死循环）
8. ✅ 启动后风扇不狂转（CPU 占用正常）
9. ✅ P0 修复完成（Task 13-16）：
   - region_sync 不再因覆盖率 < 50% return（Task 13）
   - forced sync 失败后 5 分钟冷却（Task 14）
   - shrink_threshold 从 100 降到 10（Task 15）
   - forced sync 改异步触发，启动不阻塞 43 秒（Task 16）

---

## Self-Review

### 1. Spec coverage

用户的核心要求：
- "撤销你的错误修复" → Task 之前已完成（commit `da4d0db0`）
- "修复工具的 bug，对于数据过去的错误，你不但没有检查出来，反而把问题放大了"
  - "检查不出来" → Task 2-7 新增 5 项语义 check 覆盖（原 6 项，`check_brainregion_size_mismatch` 因真实数据无效已删除）
  - "放大问题" → Task 9-10 新增语义 repair 完整清理 8 存储，Task 11 端到端验证修复后程序正常运行
- "无论过去的数据有什么样的错误，你进行检测并修复后应该确保数据的可用性和准确性" → Task 11 端到端验证启动程序正常运行
- "本次先不去修复删除工具的 bug" → 本计划不动 LightRAG adelete_by_entity，只增强检查+修复工具的"亡羊补牢"能力

### 审查后修复的 10 个 bug

计划审查发现 10 个 bug（6 CRITICAL + 4 HIGH），已全部修复：

1. [CRITICAL] vdb 字段名 `__data__` → `data`（影响 Task 6/9 代码示例和测试 fixture）
2. [CRITICAL] description 分隔符 `\x1f` → `<SEP>`（影响 Task 1 `_parse_brain_meta` + Task 4 `check_entity_chunks_source_id_mismatch`）
3. [CRITICAL] `full_entities` / `full_relations` 结构理解错误（dict 不是 list，影响 Task 9 repair 代码）
4. [CRITICAL] `check_brainregion_size_mismatch` 在真实数据上无效（Task 3 已删除，避免给实施者虚假"已检测"印象）
5. [CRITICAL] `check_vdb_entities_orphan` 在真实数据上无效（Task 8 Expected 改为 0 errors，check 保留作为防御性）
6. [CRITICAL] `check_brainregion_orphan_chunks` 在真实数据上只报 2 个（Task 7 改 check 逻辑，增加检测"chunk content 含'被删除'标记"）
7. [HIGH] Task 11 端到端测试会失败（4 个 P0 未修）→ 新增 Task 13-16 修复 4 个 P0
8. [HIGH] Task 11 fixture 无 try/finally 保护 → 已加 try/finally
9. [HIGH] Task 1 Step 5 没有具体代码 → 已补全 7 处调用点 old/new 代码对照
10. [HIGH] Task 11 断言 `"智家" not in output` 不完整 → 改为 16 个僵尸脑区特征标记全检查

### 4 个 P0 遗漏（Task 13-16 修复）

审查发现 4 个 P0 问题不是删除工具 bug，而是 region_sync/runner/region_manager 的设计缺陷：
1. `_refresh_activation_manager` 覆盖率 < 50% 直接 return（Task 13 修复：删除覆盖率检查）
2. `_get_brain_injector` forced sync 死循环（Task 14 修复：加 5 分钟失败冷却）
3. `shrink_threshold=100` 太高（Task 15 修复：降到 10）
4. forced sync 同步阻塞 43 秒（Task 16 修复：改异步触发）

### 2. Placeholder scan

检查计划，所有步骤都有具体代码、具体命令、具体期望输出。无 TBD / TODO。

### 3. Type consistency

- `_load_graphml` 返回 4-tuple `(node_ids, edges, node_meta, error)`，所有 Task 引用一致
- `_parse_brain_meta` 返回 `dict[str, str]`，所有 check 使用一致（分隔符是 `<SEP>` 字符串）
- `repair_brainregion_zombies` 返回 `{status, cleaned_count, details}`，测试和集成调用一致
- `check_brainregion_semantic_zombie` 等返回 `{name, errors}`，跟现有 check 一致
- vdb 文件格式：顶层 `data` 字段（不是 `__data__`），entry `vector` 是 base64 字符串（不是 list[float]）
- `kv_store_full_entities[doc_id]` 是 dict（`{entity_names: list, count}` 或 `{entity_name: str, ...}`），不是 list
- `kv_store_full_relations[doc_id]` 是 dict（`{relation_pairs: list[list], count, ...}`），不是 list

### 4. 风险

- 真实数据端到端测试（Task 11）会修改 `~/.niu/lightrag_storage`，需要 fixture 保护
  - 已设计 `restore_real_data` fixture 测试前后恢复，并加 try/finally 保护（审查 Bug 8 修复）
- 启动 `./niu` 需要确保无残留进程
- 修复后真实数据可能仍有非僵尸的 check 报错（如其他历史问题），但 brainregion_semantic_zombie 必须 0 errors
- P0 修复（Task 13-16）改 region_sync/runner/region_manager 代码，必须先用 gitnexus_impact 分析影响范围
- forced sync 改异步（Task 16）后，activation_manager 在后台 sync 完成后才就绪，期间脑区激活功能暂时不可用——这是可接受的（比阻塞 43 秒好）

### 重做审查后修复的 10 个新 bug

重做审查（baseline 9698bd15）发现 10 个新重大 bug（5 CRITICAL + 5 HIGH），已全部修复：

1. [CRITICAL] **Bug A** Task 8 Step 2 Expected 数字严重错误：计划写 `16/16/1/0/2`，真实数据是 `16/23/84/0/39`。已修正为真实值，并说明 84 个共享 chunk 和 39 个孤儿 chunk 含其他历史残留（不全是 16 个僵尸脑区造成）。
2. [CRITICAL] **Bug B** Task 9 清理 vdb 后未重建 matrix：vdb 顶层 `matrix` 是 base64 float32 矩阵，`_load_vdb` 会校验 `4 * embedding_dim * len(data_list) == len(matrix_bytes)`。删 entry 后 matrix 长度不变触发 `matrix_size_mismatch` critical。已加 `_rebuild_vdb_matrix` 函数，在清理 vdb_entities/vdb_relationships/vdb_chunks 后调用重建 matrix。
3. [CRITICAL] **Bug C** Task 13 会破坏现有测试：`tests/test_region_sync.py:319-353` 的 `test_refresh_activation_manager_skips_when_coverage_too_low` 验证覆盖率 < 50% 时 `initialize_from_regions` 不被调用。Task 13 删除覆盖率检查后该测试失败。已新增 Step 3.5 删除/重写该测试。
4. [CRITICAL] **Bug D** Task 14/16 属性名错误：计划用 `self._activation_manager`，真实代码用 `self._cached_activation_mgr`（runner.py L616）+ 模块级 `get_activation_mgr()` 函数（L1681、L1702）。已把所有 `self._activation_manager` 改为 `self._cached_activation_mgr` + `get_activation_mgr()`。
5. [CRITICAL] **Bug E** Task 16 异步线程安全 + 并发启动多个 forced sync：异步线程失败后 set `_last_forced_sync_fail_time`，主线程立即返回 None，5 分钟内多次调用会启动多个 daemon 线程。已加 `self._forced_sync_running`（threading.Event）标志，避免并发启动多个 forced sync 线程。
6. [HIGH] **Bug F** Task 15 常量名错误：计划用 `SHRINK_THRESHOLD = 100`（region_sync.py L42），真实代码无此常量。真实位置是 `REGION_CONFIG_DEFAULTS["shrink_threshold"] = 100`（region_sync.py L41，字典 key）+ `dissolve_shrunk_regions(self, shrink_threshold: int = 100, ...)`（region_manager.py L1016，参数默认值）。已改为真实代码属性。
7. [HIGH] **Bug G** Task 9 测试 fixture 与真实数据不符：Task 9 测试 fixture 用 form 1 `{entity_names: list, count}` 写 full_entities，但真实数据是 form 2 `{entity_name, description, source_id}`（单 entity）。测试能过但真实数据清理无效。已把 fixture 改为 form 2 真实结构，并同步修正断言（僵尸 entity 的 doc 整体被删，不是从 list 移除）。
8. [HIGH] **Bug H** Task 1 Step 5 漏 1 处调用点：真实 grep 显示 `_load_graphml` 在 lightrag_integrity.py 有 10 处调用（不含定义），Task 1 Step 5 只列 7 处。漏掉 L687（`check_vdb_relationships_endpoint_dangling` 内部第二处）。已补上 L687 调点的 old/new 代码对照，从 7 处补到 8 处。
9. [HIGH] **Bug I** Task 9 清理无事务式保护：8 个存储清理用独立 try/except，中间失败会状态不一致（半写盘）。已改为"in-memory 修改 + 统一写入"模式——所有清理在内存中完成，全部成功后才统一写盘；写盘失败则内存修改丢失（不会半写盘），返回 unrecoverable。
10. [HIGH] **Bug J** Task 11 finally 块无 kill fallback：`proc.terminate() + proc.wait(timeout=10)` 如果进程不响应 SIGTERM 会超时抛异常，proc 成为僵尸进程。已加 `proc.kill()` fallback + psutil 杀进程树（或 pkill 兜底）。

### 第三次审查后修复的 7 个新 bug

第三次审查（baseline 274fe65b）发现 7 个新 bug（3 CRITICAL + 4 HIGH），已全部修复：

1. [CRITICAL] **Bug #1** `_rebuild_vdb_matrix` 解码 vector 编码格式错误：原用 `np.frombuffer(base64.b64decode(vec_b64), dtype=np.float32)` 解码，但真实 vector 字段是 `base64(zlib(float16 bytes))` 三层编码（参考 `lightrag_repair.py` L177-182 `_encode_vector`）。原解码会抛 `buffer size must be a multiple of element size` 异常，被 except 用零向量填充，matrix 全零。已改为三层解码（base64 → zlib → float16 → float32），并在 docstring 明确 vector 字段三层编码、matrix 字段单层编码的差异。顶部加 `import zlib`。
2. [CRITICAL] **Bug #2** Task 9 函数名错误：`lightrag_repair.py` 用 `_storage_dir()`（L68 定义），不是 `_resolve_storage_dir()`（这是 `lightrag_integrity.py` L52 的函数）。Task 9 `repair_brainregion_zombies` 函数里 L1709 的 `_resolve_storage_dir()` 已改为 `_storage_dir()`。其他 5 处 `_resolve_storage_dir()` 在 `lightrag_integrity.py` 的 check 函数里，保持不变。
3. [CRITICAL] **Bug #3** Task 9 遗漏清理 `kv_store_relation_chunks.json`：真实数据含 16 个僵尸脑区的关系 chunk（key 格式 `智家xxx脑区<SEP>知识图谱系统维护`，`<SEP>` 是 GRAPH_FIELD_SEP 字符串），修复后残留导致 `check_relation_chunks_dangling` 报 16 个 major error。已加 3.9 段清理逻辑（读入 + src/tgt 是僵尸脑区则删 + 写盘），Task 9 清理范围从 7 存储改为 8 存储，commit message 同步更新。
4. [HIGH] **Bug #4** Task 14 `_activation_mgr = self._cached_activation_mgr` 逻辑错误：应读全局单例 `get_activation_mgr()`（定义在 `agent/brain_tools.py:39`），不是读 cache `self._cached_activation_mgr`。cache 被清空后会误判 activation_mgr 未就绪。Task 14 和 Task 16 的 `_activation_mgr = self._cached_activation_mgr` 已改为 `_activation_mgr = get_activation_mgr()`。
5. [HIGH] **Bug #5** Task 14/16 `run_sync()` 裸调用错误：`run_sync` 是 `RegionSync` 实例方法，不是模块级函数，裸调用会 `NameError`。已改为 `from agent.injector.region_sync import get_region_sync; get_region_sync().run_sync()`（`get_region_sync` 定义在 `agent/injector/region_sync.py`）。Task 14 Step 3 和 Task 16 Step 3 两处都已修复。
6. [HIGH] **Bug #6** Task 14/16 缺 `import time`：runner.py 顶部不 import time，但 Task 14/16 用 `time.time()`。已在 Task 14 Step 3 加明确说明：实施者必须先 `grep "^import time" agent/runner.py` 检查，若无输出则在 runner.py 顶部加 `import time`。
7. [HIGH] **Bug #7** Task 14 forced sync 成功后冷却永远不解除：第一次 forced sync 成功后 `_last_forced_sync_fail_time` 保持 0.0，下次调用 `time.time() - 0.0 < 300` 永远 True，冷却永远不解除。已在 Task 14 和 Task 16 的 forced sync 成功分支加 `self._last_forced_sync_fail_time = 0.0` 重置冷却时间。

