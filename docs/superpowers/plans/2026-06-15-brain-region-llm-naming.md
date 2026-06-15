# 脑区 LLM 命名改进 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将脑区命名从启发式（取第一个实体名）改为 LLM 生成语义化名称，同时修复 description 格式和 chunks 缺失问题，使脑区可被向量检索命中。

**Architecture:** 拆分 `_summarize_region()` 为 `_generate_region_label()`（LLM）和 `_generate_region_summary()`（启发式）；description 改为 top-10 实体名 `<SEP>` 拼接；chunks 的 source_id 用 unique 格式匹配 entity 重写后的 source_id，避免虚拟 chunk 重复生成。

**Tech Stack:** LiteLLMSession、litellm.token_counter(model="gpt-4o")、igraph.subgraph().degree()

---

## 文件结构

| 文件 | 职责 | 操作 |
|------|------|------|
| `niu_api/internal/region_manager.py` | 核心改动：拆分方法、LLM调用、chunks生成 | 修改 |
| `niu_api/internal/region_detector.py` | 社区内度数排序 | 修改 |
| `tests/test_region_manager.py` | RegionManager 单元测试 | 修改 |
| `tests/test_region_detector.py` | 社区度数排序测试 | 修改 |

---

### Task 1: 社区内度数排序（region_detector.py）

**背景：** 当前 `_build_partitions()` 中 `entity_names` 按 igraph 顶点 ID 顺序排列，`entity_names[0]` 不是真正的社区中心。需要按社区内度数降序排列，这样 `entity_names[0]` 才是最高度数实体，后续的 top-10 实体选取和 representative 也能用上。

**Files:**
- Modify: `niu_api/internal/region_detector.py:284-297`
- Test: `tests/test_region_detector.py`

- [ ] **Step 1: 写失败测试 — 度数排序**

在 `tests/test_region_detector.py` 末尾新增测试类：

```python
class TestBuildPartitionsDegreeSort:
    """Test that _build_partitions sorts entity_names by in-community degree."""

    def test_entity_names_sorted_by_degree(self):
        """entity_names should be ordered by in-community degree (descending)."""
        import igraph as ig

        # Build a graph with clear degree differences in community [0, 1, 2]
        # B(1) connects to A, C, and D(3) — degree 2 within community
        # A(0) connects to B — degree 1 within community
        # C(2) connects to B — degree 1 within community
        # D(3) is in its own community, edge B-D is cross-community
        g = ig.Graph()
        g.add_vertices(4)
        g.vs["name"] = ["A", "B", "C", "D"]
        g.vs["entity_type"] = ["skill", "person", "org", "skill"]
        # Community [0,1,2] edges: A-B, B-C (B has degree 2, A and C have degree 1)
        # Cross-community edge: B-D
        g.add_edges([(0, 1), (1, 2), (1, 3)])  # A-B, B-C, B-D

        from niu_api.internal.region_detector import CommunityDetector
        detector = CommunityDetector.__new__(CommunityDetector)

        # Create a mock partition where community 0 = [0, 1, 2]
        class MockPartition:
            q = 0.5
            def __iter__(self):
                yield [0, 1, 2]
                yield [3]

        partitions = detector._build_partitions(g, MockPartition(), min_community_size=1)

        # First partition: subgraph of [0,1,2] has edges A-B and B-C
        # B(1) degree=2, A(0) degree=1, C(2) degree=1
        # Sorted by degree descending: B(2), then A(1) and C(1) in original order
        assert len(partitions) == 2
        p0 = partitions[0]
        assert p0.entity_names[0] == "B"  # Highest degree first
        assert set(p0.entity_names) == {"A", "B", "C"}
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest tests/test_region_detector.py::TestBuildPartitionsDegreeSort -v`
Expected: FAIL — 当前 entity_names 顺序为 ["A", "B", "C"]（igraph 顶点 ID 顺序）

- [ ] **Step 3: 实现度数排序**

在 `niu_api/internal/region_detector.py` 的 `_build_partitions()` 方法中，替换收集实体名称和类型的循环（第 284-296 行）。在原代码 `for vidx in member_indices:` 循环之前，插入度数计算和排序：

```python
            # 按社区内度数降序排列顶点索引
            subgraph = graph.subgraph(member_indices)
            degrees = subgraph.degree()
            sorted_pairs = sorted(
                zip(member_indices, degrees), key=lambda x: x[1], reverse=True
            )

            # 收集实体名称和类型（按度数降序）
            entity_names: list[str] = []
            entity_type_counts: dict[str, int] = {}
            entity_name_to_type: dict[str, str] = {}

            for vidx, _deg in sorted_pairs:
                v = graph.vs[vidx]
                name = v["name"] if "name" in v.attributes() else f"entity_{vidx}"
                entity_names.append(name)

                etype = v["entity_type"] if "entity_type" in v.attributes() else "unknown"
                entity_type_counts[etype] = entity_type_counts.get(etype, 0) + 1
                entity_name_to_type[name] = etype
```

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest tests/test_region_detector.py::TestBuildPartitionsDegreeSort -v`
Expected: PASS

- [ ] **Step 5: 运行现有测试确认无回归**

Run: `pytest tests/test_region_detector.py -v`
Expected: 全部 PASS

- [ ] **Step 6: 提交**

```bash
git add niu_api/internal/region_detector.py tests/test_region_detector.py
git commit -m "feat: sort community entities by in-community degree in _build_partitions"
```

---

### Task 2: `_generate_region_summary()` — description 改为实体名 `<SEP>` 拼接

**背景：** 当前 `_summarize_region()` 返回的 summary 格式为 `"Python(skill)、Django(framework)等3个实体"`，向量检索无法命中。改为 top-10 实体名用 `<SEP>` 分隔，同时 `MAX_SUMMARY_ENTITIES` 从 5 改为 10。

**Files:**
- Modify: `niu_api/internal/region_manager.py:60` (MAX_SUMMARY_ENTITIES)
- Modify: `niu_api/internal/region_manager.py:793-858` (_summarize_region → 拆出 _generate_region_summary)
- Test: `tests/test_region_manager.py`

- [ ] **Step 1: 写失败测试 — `_generate_region_summary()` 新格式**

在 `tests/test_region_manager.py` 新增测试类：

```python
class TestGenerateRegionSummary:
    """Test _generate_region_summary — top-10 entity names joined by <SEP>."""

    def test_summary_uses_sep_separator(self):
        """Summary should use <SEP> separator between entity names."""
        manager = RegionManager.__new__(RegionManager)
        entity_summaries = ["Python(skill)", "Django(framework)", "FastAPI(framework)"]
        result = manager._generate_region_summary(entity_summaries)
        # Should be "Python<SEP>Django<SEP>FastAPI"
        assert result == "Python<SEP>Django<SEP>FastAPI"

    def test_summary_top_10_entities(self):
        """Summary should include at most 10 entity names."""
        manager = RegionManager.__new__(RegionManager)
        entity_summaries = [f"E{i}(type)" for i in range(15)]
        result = manager._generate_region_summary(entity_summaries)
        parts = result.split("<SEP>")
        assert len(parts) == 10

    def test_summary_extracts_name_only(self):
        """Summary should contain entity names without type labels."""
        manager = RegionManager.__new__(RegionManager)
        entity_summaries = ["Python(skill)", "Django(framework)"]
        result = manager._generate_region_summary(entity_summaries)
        assert "skill" not in result
        assert "framework" not in result
        assert "Python" in result
        assert "Django" in result

    def test_summary_empty_input(self):
        """Empty input should return empty string."""
        manager = RegionManager.__new__(RegionManager)
        result = manager._generate_region_summary([])
        assert result == ""

    def test_summary_sanitizes_sep_in_names(self):
        """Entity names containing <SEP> or | should be sanitized."""
        manager = RegionManager.__new__(RegionManager)
        entity_summaries = ["Bad<SEP>Name(type)", "Pipe|Name(type2)"]
        result = manager._generate_region_summary(entity_summaries)
        assert "<SEP>" not in result.split("brain_meta_")[0].replace("<SEP>", "__KEEP__")
        # The name "Bad<SEP>Name" should have <SEP> replaced with -
        assert "Bad-Name" in result
        assert "Pipe-Name" in result
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest tests/test_region_manager.py::TestGenerateRegionSummary -v`
Expected: FAIL — `_generate_region_summary` 方法不存在

- [ ] **Step 3: 实现 `_generate_region_summary()`**

在 `niu_api/internal/region_manager.py` 中：

1. 修改常量 `MAX_SUMMARY_ENTITIES = 5` → `MAX_SUMMARY_ENTITIES = 10`

2. 在 `_summarize_region()` 方法之前（约第 793 行），添加新方法：

```python
def _generate_region_summary(self, entity_summaries: list[str]) -> str:
    """Generate region description from top entity names using <SEP> separator.

    Entity names are joined by <SEP> (LightRAG's GRAPH_FIELD_SEP) so that
    vector search can match individual entity names as semantic fragments.
    """
    if not entity_summaries:
        return ""

    entity_names: list[str] = []
    for summary in entity_summaries[:MAX_SUMMARY_ENTITIES]:
        match = re.match(r"([^(]+)\(([^)]+)\)", summary)
        if match:
            name = match.group(1).strip()
        else:
            name = summary.strip()
        # Sanitize: replace <SEP> and | to avoid breaking description parsing
        name = name.replace("<SEP>", "-").replace("|", "-")
        entity_names.append(name)

    return "<SEP>".join(entity_names)
```

3. 修改 `_summarize_region()` 方法，让它内部调用 `_generate_region_summary()` 来生成 summary，保持返回值 `(label, summary)` 不变以维持向后兼容：

```python
def _summarize_region(
    self,
    entity_summaries: list[str],
) -> tuple[str, str]:
    """Generate region name and summary from entity descriptions

    Uses a heuristic approach: first entity name as label, top entity
    names with <SEP> as summary. LLM naming is in _generate_region_label().

    Args:
        entity_summaries: ["Python(skill)", "Django(framework)", ...]

    Returns:
        (region_name, region_summary)
    """
    if not entity_summaries:
        return ("unknown", "")

    # Parse names from summaries
    entity_names: list[str] = []
    for summary in entity_summaries:
        match = re.match(r"([^(]+)\(([^)]+)\)", summary)
        if match:
            entity_names.append(match.group(1).strip())
        else:
            entity_names.append(summary.strip())

    if not entity_names:
        return ("unknown", "")

    # Heuristic: Use the first entity (highest-degree) as region label
    region_label = entity_names[0].replace("<SEP>", "-").replace("|", "-")

    # Generate summary using <SEP> format
    region_summary = self._generate_region_summary(entity_summaries)

    return (region_label, region_summary)
```

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest tests/test_region_manager.py::TestGenerateRegionSummary -v`
Expected: PASS

- [ ] **Step 5: 运行现有测试，修复因 summary 格式变化导致的失败**

Run: `pytest tests/test_region_manager.py -v`

以下旧测试需要更新：

- `TestSummarizeRegionHeuristic.test_returns_representative_as_name`：断言 `"Python(language)" in summary` 需改为 `assert "Python" in summary`（类型标签不再包含在 summary 中）
- `TestSummarizeRegionHeuristic.test_summary_limits_to_max_entities`：断言 `"10个实体" in summary` 需改为 `assert "<SEP>" in summary`（不再有"等N个实体"后缀）
- `TestSummarizeRegionHeuristic.test_summary_joins_with_chinese_comma`：断言 `"、" in summary` 需改为 `assert "<SEP>" in summary`（分隔符从 `、` 改为 `<SEP>`）

- [ ] **Step 6: 提交**

```bash
git add niu_api/internal/region_manager.py tests/test_region_manager.py
git commit -m "feat: change region summary format to entity names with <SEP> separator"
```

---

### Task 3: `_generate_region_label()` — LLM 生成标签名

**背景：** 新增 LLM 调用方法，为脑区生成语义化中文标签名。包含容错机制（JSON解析→正则→重试→fallback）。

**Files:**
- Modify: `niu_api/internal/region_manager.py` — 新增 `_generate_region_label()` 和辅助函数
- Test: `tests/test_region_manager.py`

- [ ] **Step 1: 写失败测试 — `_generate_region_label()` 基础功能**

在 `tests/test_region_manager.py` 新增测试类：

```python
class TestGenerateRegionLabel:
    """Test _generate_region_label — LLM-generated semantic region label."""

    def test_returns_label_from_llm_json(self):
        """Should extract label from LLM JSON response."""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        # Mock the LLM call to return valid JSON
        manager._call_llm_for_label = lambda prompt: '{"label": "编程开发"}'

        entity_summaries = ["Python(skill)", "Django(framework)", "FastAPI(framework)"]
        existing_regions = []

        result = manager._generate_region_label(entity_summaries, existing_regions)
        assert result == "编程开发"

    def test_fallback_on_json_parse_failure(self):
        """Should fallback to entity_names[0] when JSON parse fails after retry."""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        # Mock LLM to return unparseable content
        call_count = 0
        def bad_llm(prompt):
            nonlocal call_count
            call_count += 1
            return "这是一个编程相关的社区"
        manager._call_llm_for_label = bad_llm

        entity_summaries = ["Python(skill)", "Django(framework)"]
        existing_regions = []

        result = manager._generate_region_label(entity_summaries, existing_regions)
        # Should fallback to first entity name
        assert result == "Python"

    def test_regex_fallback_on_malformed_json(self):
        """Should try regex extraction when JSON parse fails."""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        # Mock LLM to return JSON with surrounding text
        manager._call_llm_for_label = lambda prompt: '结果是 {"label": "编程开发"} 哦'

        entity_summaries = ["Python(skill)", "Django(framework)"]
        existing_regions = []

        result = manager._generate_region_label(entity_summaries, existing_regions)
        assert result == "编程开发"

    def test_label_truncated_over_8_chars(self):
        """Label should be truncated to 8 characters."""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        manager._call_llm_for_label = lambda prompt: '{"label": "这是一个非常非常长的标签名称"}'

        entity_summaries = ["Python(skill)"]
        existing_regions = []

        result = manager._generate_region_label(entity_summaries, existing_regions)
        assert len(result) <= 8

    def test_duplicate_label_gets_suffix(self):
        """Should add numeric suffix when label duplicates existing region."""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        manager._call_llm_for_label = lambda prompt: '{"label": "编程开发"}'

        entity_summaries = ["Python(skill)"]
        existing_regions = ["编程开发"]

        result = manager._generate_region_label(entity_summaries, existing_regions)
        assert result.startswith("编程开发")
        assert result != "编程开发"

    def test_empty_input_returns_unknown(self):
        """Empty entity_summaries should return 'unknown'."""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        result = manager._generate_region_label([], [])
        assert result == "unknown"
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest tests/test_region_manager.py::TestGenerateRegionLabel -v`
Expected: FAIL — `_generate_region_label` 方法不存在

- [ ] **Step 3: 实现 `_generate_region_label()` 和 `_call_llm_for_label()`**

在 `niu_api/internal/region_manager.py` 的 `_generate_region_summary()` 之后，`_summarize_region()` 之前，添加：

```python
def _generate_region_label(
    self,
    entity_summaries: list[str],
    existing_regions: list[str],
) -> str:
    """Generate a semantic Chinese label for a brain region via LLM.

    Falls back to heuristic (entity_names[0]) on any LLM failure.

    Args:
        entity_summaries: ["Python(skill)", "Django(framework)", ...]
        existing_regions: List of existing region labels to avoid duplicates

    Returns:
        A label string (max 8 chars), e.g. "编程开发"
    """
    if not entity_summaries:
        return "unknown"

    # Extract entity names for prompt and fallback
    entity_names: list[str] = []
    entity_list_parts: list[str] = []
    for summary in entity_summaries:
        match = re.match(r"([^(]+)\(([^)]+)\)", summary)
        if match:
            name = match.group(1).strip()
            etype = match.group(2).strip()
            entity_names.append(name)
            entity_list_parts.append(f"{name}({etype})")
        else:
            entity_names.append(summary.strip())
            entity_list_parts.append(summary.strip())

    if not entity_names:
        return "unknown"

    fallback_label = entity_names[0].replace("<SEP>", "-").replace("|", "-")

    # Build prompt
    entity_list_str = ", ".join(entity_list_parts)
    existing_str = ", ".join(existing_regions) if existing_regions else "无"

    prompt = (
        "你是一个知识图谱分析师。根据以下社区内的实体列表，为这个社区生成一个简洁的中文标签名。\n\n"
        "要求：\n"
        "- 8个字以下\n"
        "- 概括这些实体的共同主题\n"
        "- 不要跟现有脑区重名或语义接近\n"
        "- 只能返回JSON格式：{\"label\": \"标签名\"}\n"
        "- 返回其他任何格式或内容将判定失败\n\n"
        f"现有脑区：{existing_str}\n\n"
        f"实体列表：{entity_list_str}"
    )

    # Token truncation check
    try:
        import litellm
        token_count = litellm.token_counter(model="gpt-4o", text=prompt)
        context_window = _read_context_window_size()
        if token_count > context_window - 500:
            # Truncate entity list from the tail
            while entity_list_parts and token_count > context_window - 500:
                entity_list_parts.pop()
                entity_list_str = ", ".join(entity_list_parts)
                prompt = (
                    "你是一个知识图谱分析师。根据以下社区内的实体列表，为这个社区生成一个简洁的中文标签名。\n\n"
                    "要求：\n"
                    "- 8个字以下\n"
                    "- 概括这些实体的共同主题\n"
                    "- 不要跟现有脑区重名或语义接近\n"
                    "- 只能返回JSON格式：{\"label\": \"标签名\"}\n"
                    "- 返回其他任何格式或内容将判定失败\n\n"
                    f"现有脑区：{existing_str}\n\n"
                    f"实体列表：{entity_list_str}"
                )
                token_count = litellm.token_counter(model="gpt-4o", text=prompt)
    except Exception:
        pass  # Token counting failure should not block

    # Call LLM with retry
    label = self._parse_label_from_llm(prompt, fallback_label)

    # Truncate to 8 chars first
    if len(label) > 8:
        label = label[:8]

    # Check for duplicate names (suffix must fit in 8 chars)
    if label in existing_regions:
        # Reserve 1 char for suffix digit: truncate base to 7 chars
        base = label[:7]
        n = 2
        candidate = f"{base}{n}"
        while candidate in existing_regions and n < 10:
            n += 1
            candidate = f"{base}{n}"
        label = candidate

    return label

def _parse_label_from_llm(self, prompt: str, fallback: str) -> str:
    """Call LLM and parse label with retry logic.

    1. Try JSON parse
    2. Try regex extraction
    3. Retry once on failure
    4. Fallback to heuristic
    """
    for attempt in range(2):  # 2 attempts: initial + 1 retry
        try:
            content = self._call_llm_for_label(prompt)
            label = self._extract_label_from_content(content)
            if label:
                # Truncate to 8 chars
                if len(label) > 8:
                    label = label[:8]
                return label
        except Exception as e:
            logger.debug("LLM label generation attempt %d failed: %s", attempt + 1, e)

    logger.warning("LLM label generation failed after retry, fallback to: %s", fallback)
    return fallback

def _extract_label_from_content(self, content: str) -> str:
    """Extract label from LLM response content.

    1. Try JSON parse
    2. Try regex extraction
    3. Return empty string on failure
    """
    content = content.strip()

    # Try JSON parse
    try:
        import json
        data = json.loads(content)
        if isinstance(data, dict) and "label" in data:
            label = str(data["label"]).strip()
            if label:
                return label
    except (json.JSONDecodeError, ValueError):
        pass

    # Try regex extraction
    match = re.search(r'"label"\s*:\s*"([^"]+)"', content)
    if match:
        label = match.group(1).strip()
        if label:
            return label

    return ""

def _call_llm_for_label(self, prompt: str) -> str:
    """Call LLM via LiteLLMSession to generate a label.

    Consumes the streaming generator and returns the full text content.
    30-second timeout via thread-based mechanism.
    Note: on timeout, the daemon thread continues running until the generator
    exhausts or the process exits. This is acceptable because RegionSync runs
    once per 24h and timeouts are expected to be rare.
    """
    from niu_api.internal.lightrag_manager import _get_litellm_session
    from niu_api.llm_proxy import get_llm_config

    config = get_llm_config()  # 主 Agent 同款模型
    session = _get_litellm_session(config)
    gen = session.chat(messages=[{"role": "user", "content": prompt}])

    # Consume generator with 30s timeout
    chunks: list[str] = []
    try:
        import threading

        result_holder: list = [None, None]  # [content, exception]

        def _consume():
            try:
                while True:
                    chunk = next(gen)
                    if isinstance(chunk, str):
                        chunks.append(chunk)
            except StopIteration:
                pass
            except Exception as e:
                result_holder[1] = e

        thread = threading.Thread(target=_consume, daemon=True)
        thread.start()
        thread.join(timeout=30)

        if thread.is_alive():
            # Thread still running after timeout
            logger.warning("LLM label generation timed out after 30s, using partial result")
        if result_holder[1]:
            raise result_holder[1]

    except Exception as e:
        if not chunks:
            raise
        logger.warning("LLM label generation error: %s, using partial result", e)

    return "".join(chunks)
```

在 `region_manager.py` 顶部（函数区域，类之外）添加上下文窗口读取函数：

```python
def _read_context_window_size() -> int:
    """Read context window size from user config.

    Returns 200000 as default if config is missing or unreadable.
    """
    try:
        config_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "config", "user-config.json",
        )
        if os.path.exists(config_path):
            with open(config_path, encoding="utf-8") as f:
                data = json.load(f)
            return data.get("context", {}).get("contextWindowSize", 200000)
    except Exception:
        pass
    return 200000
```

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest tests/test_region_manager.py::TestGenerateRegionLabel -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add niu_api/internal/region_manager.py tests/test_region_manager.py
git commit -m "feat: add _generate_region_label with LLM-based semantic naming"
```

---

### Task 4: 修改 `create_region_nodes()` — 使用 LLM 标签 + 加入 chunks

**背景：** 将 `create_region_nodes()` 中的 `_summarize_region()` 调用替换为 `_generate_region_label()` + `_generate_region_summary()`，同时为每个脑区生成 chunk，chunk 的 source_id 用 unique 格式。

**Files:**
- Modify: `niu_api/internal/region_manager.py:180-303`
- Test: `tests/test_region_manager.py`

- [ ] **Step 1: 写失败测试 — `create_region_nodes` 使用 LLM 标签**

在 `tests/test_region_manager.py` 新增测试类：

```python
class TestCreateRegionNodesWithLLMLabel:
    """Test create_region_nodes uses _generate_region_label for naming."""

    def test_uses_llm_label_for_region_name(self):
        """Region name should use _generate_region_label result."""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        # Mock LLM label generation
        manager._generate_region_label = lambda summaries, existing: "编程开发"

        partitions = [
            RegionPartition(
                region_id=0,
                region_name="region_0",
                entity_names=[f"E{i}" for i in range(100)],
                entity_types={"skill": 100},
                edge_count=2,
                modularity_score=0.15,
            ),
        ]
        result = _make_partition_result(partitions)
        region_names = manager.create_region_nodes(result)

        assert len(region_names) == 1
        assert region_names[0] == "编程开发脑区"

    def test_injects_chunks_with_unique_source_id(self):
        """Chunks should have source_id matching entity's rewritten source_id format."""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        manager._generate_region_label = lambda summaries, existing: "编程开发"

        partitions = [
            RegionPartition(
                region_id=0,
                region_name="region_0",
                entity_names=[f"E{i}" for i in range(100)],
                entity_types={"skill": 100},
                edge_count=2,
                modularity_score=0.15,
            ),
        ]
        result = _make_partition_result(partitions)
        manager.create_region_nodes(result)

        # Verify inject_custom_kg was called with chunks
        call_kwargs = ingester.inject_custom_kg.call_args[1]
        chunks = call_kwargs.get("chunks", [])
        assert len(chunks) >= 1

        # Chunk source_id should match unique format: "brain_编程开发脑区"
        chunk = chunks[0]
        assert chunk["source_id"] == "brain_编程开发脑区"

    def test_entity_source_id_is_base(self):
        """Entity source_id should be base 'brain' (inject_custom_kg will rewrite it)."""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        manager._generate_region_label = lambda summaries, existing: "编程开发"

        partitions = [
            RegionPartition(
                region_id=0,
                region_name="region_0",
                entity_names=[f"E{i}" for i in range(100)],
                entity_types={"skill": 100},
                edge_count=2,
                modularity_score=0.15,
            ),
        ]
        result = _make_partition_result(partitions)
        manager.create_region_nodes(result)

        call_kwargs = ingester.inject_custom_kg.call_args[1]
        entities = call_kwargs.get("entities", [])
        assert len(entities) == 1
        # Entity source_id should be base "brain"
        assert entities[0]["source_id"] == "brain"

    def test_chunk_content_contains_label_and_members(self):
        """Chunk content should include region label and top member names."""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        manager._generate_region_label = lambda summaries, existing: "编程开发"

        partitions = [
            RegionPartition(
                region_id=0,
                region_name="region_0",
                entity_names=[f"E{i}" for i in range(100)],
                entity_types={"skill": 100},
                edge_count=2,
                modularity_score=0.15,
            ),
        ]
        result = _make_partition_result(partitions)
        manager.create_region_nodes(result)

        call_kwargs = ingester.inject_custom_kg.call_args[1]
        chunks = call_kwargs.get("chunks", [])
        assert len(chunks) >= 1
        # Chunk content should contain label
        assert "编程开发" in chunks[0]["content"]
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest tests/test_region_manager.py::TestCreateRegionNodesWithLLMLabel -v`
Expected: FAIL — 当前 create_region_nodes 仍使用 `_summarize_region()` 且 chunks=[]

- [ ] **Step 3: 修改 `create_region_nodes()`**

替换 `niu_api/internal/region_manager.py` 的 `create_region_nodes()` 方法。关键改动：

1. 获取现有脑区标签列表用于 LLM 去重
2. 调用 `_generate_region_label()` 替代 `_summarize_region()` 中的 label 生成
3. 调用 `_generate_region_summary()` 替代 `_summarize_region()` 中的 summary 生成
4. 添加 chunks 列表，每个脑区一个 chunk
5. chunk 的 source_id 使用 unique 格式 `f"brain_{region_name}"`

```python
def create_region_nodes(
    self,
    partition_result: CommunityDetectionResult,
) -> list[str]:
    """Create master nodes + relationships for each community

    Uses batch injection: collects all entities, relationships and chunks first,
    then calls inject_custom_kg once. This avoids serially blocking
    the LightRAG event loop with N individual calls per community.

    Args:
        partition_result: Community detection result from M1

    Returns:
        List of created region names (e.g. ["编程开发脑区", ...])
    """
    all_entities: list[dict] = []
    all_relationships: list[dict] = []
    all_chunks: list[dict] = []
    created_regions: list[str] = []

    # Pre-fetch existing region labels for LLM dedup
    existing_labels: list[str] = []
    try:
        for region in self.get_all_regions():
            existing_labels.append(region.label)
    except Exception:
        pass

    for partition in partition_result.partitions:
        # Step 1: Filter out existing region nodes
        members = [
            name
            for name in partition.entity_names
            if not (name.endswith(REGION_SUFFIX) or name.startswith(REGION_PREFIX))
        ]

        if not members or len(members) < MIN_COMMUNITY_SIZE:
            logger.debug(
                "社区 %d 成员数 %d < %d，跳过",
                partition.region_id,
                len(members),
                MIN_COMMUNITY_SIZE,
            )
            continue

        # Build entity summaries for region naming
        entity_summaries = self._build_entity_summaries(
            members, partition.entity_types, partition.entity_name_to_type
        )

        # Step 2: Generate region label via LLM and summary via heuristic
        region_label = self._generate_region_label(entity_summaries, existing_labels)
        region_summary = self._generate_region_summary(entity_summaries)

        # Track label for dedup in subsequent iterations
        existing_labels.append(region_label)

        # Pick representative: first entity name (highest-degree in community)
        representative = members[0].replace("<SEP>", "-").replace("|", "-") if members else ""
        community_id = f"community_{partition.region_id}"
        now = time.time()

        # Full region entity name
        region_name = f"{region_label}{REGION_SUFFIX}"

        # Step 3: Collect region master node entity
        description = _encode_description(
            summary=region_summary,
            region_id=community_id,
            size=len(members),
            representative=representative,
            updated_at=now,
        )

        all_entities.append({
            "entity_name": region_name,
            "entity_type": REGION_ENTITY_TYPE,
            "description": description,
            "source_id": REGION_SOURCE_ID,  # inject_custom_kg rewrites to "brain_编程开发脑区"
        })

        # Step 4: Collect chunk for vector search visibility
        top_members = members[:MAX_SUMMARY_ENTITIES]
        chunk_source_id = f"{REGION_SOURCE_ID}_{region_name}"  # "brain_编程开发脑区"

        all_chunks.append({
            "content": f"{region_label}脑区：{', '.join(top_members)}",
            "source_id": chunk_source_id,
            "file_path": REGION_FILE_PATH,
        })

        # Step 5: Collect anchor relation from Niu to region
        all_relationships.append({
            "src_id": NIU_ENTITY,
            "tgt_id": region_name,
            "keywords": ANCHOR_RELATION,
            "description": f"Brain region anchor: {region_label}",
            "weight": 0.5,
            "source_id": REGION_SOURCE_ID,
            "file_path": REGION_FILE_PATH,
        })

        # Step 6: Collect belongs_to relations from region to each member
        for member in members:
            all_relationships.append({
                "src_id": region_name,
                "tgt_id": member,
                "keywords": BELONGS_TO_RELATION,
                "description": f"{member} belongs to region {region_label}",
                "weight": 0.5,
                "source_id": REGION_SOURCE_ID,
                "file_path": REGION_FILE_PATH,
            })

        created_regions.append(region_name)
        logger.info(
            "收集脑区节点: %s (社区 %d, %d 成员, 代表: %s)",
            region_name,
            partition.region_id,
            len(members),
            representative,
        )

    # Batch inject all collected data in one call
    if all_entities or all_relationships:
        result = self._ingester.inject_custom_kg(
            entities=all_entities,
            relationships=all_relationships,
            chunks=all_chunks,
            source_id=REGION_SOURCE_ID,
        )
        if isinstance(result, dict) and result.get("status") == "error":
            logger.warning(
                "批量注入脑区实体失败: %s (collected %d regions)",
                result.get("message", "unknown"),
                len(created_regions),
            )
            return []
        logger.info(
            "批量注入 %d 个脑区实体, %d 条关系, %d 个chunks",
            len(all_entities),
            len(all_relationships),
            len(all_chunks),
        )

    logger.info("共创建 %d 个脑区节点", len(created_regions))
    return created_regions
```

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest tests/test_region_manager.py::TestCreateRegionNodesWithLLMLabel -v`
Expected: PASS

- [ ] **Step 5: 修复现有 `TestCreateRegionNodes` 测试**

旧测试可能因为 `create_region_nodes()` 内部调用 `_generate_region_label()` 导致 mock 不匹配。需要在旧测试中 mock `_generate_region_label` 方法，或者让旧测试继续使用 `_summarize_region` 的 fallback 路径。

在 `TestCreateRegionNodes` 的每个测试方法开头添加 mock：

```python
manager._generate_region_label = lambda summaries, existing: summaries[0].split("(")[0] if summaries else "unknown"
```

这样旧测试的 label 仍然是第一个实体名，保持兼容。

- [ ] **Step 6: 运行全部测试确认无回归**

Run: `pytest tests/test_region_manager.py -v`
Expected: 全部 PASS

- [ ] **Step 7: 提交**

```bash
git add niu_api/internal/region_manager.py tests/test_region_manager.py
git commit -m "feat: create_region_nodes uses LLM label + chunks with unique source_id"
```

---

### Task 5: 修改 `update_region_summaries()` — 只更新 summary 不触发 LLM

**背景：** `update_region_summaries()` 只需要更新 description 中的 summary 部分，不需要重新生成标签。所以只调用 `_generate_region_summary()`，不调用 `_generate_region_label()`。

**Files:**
- Modify: `niu_api/internal/region_manager.py:305-406`
- Test: `tests/test_region_manager.py`

- [ ] **Step 1: 写失败测试 — `update_region_summaries` 不调用 LLM**

在 `tests/test_region_manager.py` 新增测试类：

```python
class TestUpdateRegionSummariesNoLLM:
    """Test update_region_summaries does NOT call _generate_region_label."""

    def test_update_does_not_call_generate_label(self):
        """update_region_summaries should not call _generate_region_label."""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        # Mock _generate_region_label to track calls
        label_calls = []
        original_fn = manager._generate_region_label
        def track_label_calls(*args, **kwargs):
            label_calls.append(1)
            return original_fn(*args, **kwargs)
        manager._generate_region_label = track_label_calls

        # Setup: return existing region data
        adapter.list_entities.return_value = {
            "status": "ok",
            "data": [
                {
                    "id": "Python脑区",
                    "description": _encode_description(
                        summary="旧摘要", region_id="community_0",
                        size=3, representative="Python", updated_at=1000.0,
                    ),
                },
            ],
        }

        # Mock get_region_members
        from unittest.mock import patch
        with patch.object(manager, "get_region_members", return_value=["Python", "Django"]):
            manager.update_region_summaries(["Python脑区"])

        # _generate_region_label should NOT have been called
        assert label_calls == []

    def test_update_uses_generate_region_summary(self):
        """update_region_summaries should use _generate_region_summary format."""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        adapter.list_entities.return_value = {
            "status": "ok",
            "data": [
                {
                    "id": "Python脑区",
                    "description": _encode_description(
                        summary="旧摘要", region_id="community_0",
                        size=3, representative="Python", updated_at=1000.0,
                    ),
                },
            ],
        }

        from unittest.mock import patch
        with patch.object(manager, "get_region_members", return_value=["Python", "Django"]):
            manager.update_region_summaries(["Python脑区"])

        # Verify inject_custom_kg was called
        call_kwargs = ingester.inject_custom_kg.call_args[1]
        entities = call_kwargs.get("entities", [])
        assert len(entities) == 1
        # Description should contain <SEP> format summary
        desc = entities[0]["description"]
        assert "Python" in desc
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest tests/test_region_manager.py::TestUpdateRegionSummariesNoLLM -v`
Expected: FAIL — 当前 update_region_summaries 调用 `_summarize_region()` 同时生成 label 和 summary

- [ ] **Step 3: 修改 `update_region_summaries()`**

替换 `niu_api/internal/region_manager.py` 中 `update_region_summaries()` 方法的第 372-373 行：

```python
            # 旧代码：
            # entity_summaries = self._build_entity_summaries(members, set(), {})
            # _, region_summary = self._summarize_region(entity_summaries)

            # 新代码：只生成 summary，不生成 label
            entity_summaries = self._build_entity_summaries(members, set(), {})
            region_summary = self._generate_region_summary(entity_summaries)
```

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest tests/test_region_manager.py::TestUpdateRegionSummariesNoLLM -v`
Expected: PASS

- [ ] **Step 5: 运行全部测试确认无回归**

Run: `pytest tests/test_region_manager.py -v`
Expected: 全部 PASS

- [ ] **Step 6: 提交**

```bash
git add niu_api/internal/region_manager.py tests/test_region_manager.py
git commit -m "feat: update_region_summaries uses _generate_region_summary only, no LLM call"
```

---

### Task 6: 批量 LLM 调用优化

**背景：** 当一次同步检测到 3 个以上新脑区时，将所有社区数据合并到一个 prompt 中，一次 LLM 调用生成所有标签，减少延迟。

**Files:**
- Modify: `niu_api/internal/region_manager.py` — 新增 `_generate_region_labels_batch()` 方法，修改 `create_region_nodes()` 调用逻辑
- Test: `tests/test_region_manager.py`

- [ ] **Step 1: 写失败测试 — 批量 LLM 调用**

在 `tests/test_region_manager.py` 新增测试类：

```python
class TestBatchLabelGeneration:
    """Test batch LLM label generation for 3+ regions."""

    def test_batch_label_for_many_regions(self):
        """When 3+ regions, should use single batch LLM call."""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        batch_called = []
        def mock_batch(prompts_list, existing):
            batch_called.append(len(prompts_list))
            return {i: f"标签{i}" for i in range(len(prompts_list))}
        manager._generate_region_labels_batch = mock_batch

        single_called = []
        original_single = manager._generate_region_label
        def mock_single(summaries, existing):
            single_called.append(1)
            return original_single(summaries, existing)
        manager._generate_region_label = mock_single

        # 3 partitions = should trigger batch
        entity_summaries_list = [
            ["Python(skill)", "Django(framework)"],
            ["任飞(person)", "李明(person)"],
            ["雄安分行(org)", "河北分行(org)"],
        ]
        existing = []
        labels = manager._generate_labels(entity_summaries_list, existing)

        assert len(labels) == 3
        assert batch_called == [3]
        assert single_called == []

    def test_individual_label_for_few_regions(self):
        """When < 3 regions, should use individual LLM calls."""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        manager._generate_region_label = lambda summaries, existing: "测试标签"

        # 2 partitions = should use individual calls
        entity_summaries_list = [
            ["Python(skill)"],
            ["任飞(person)"],
        ]
        existing = []
        labels = manager._generate_labels(entity_summaries_list, existing)

        assert len(labels) == 2

    def test_batch_fallback_on_missing_regions(self):
        """When batch returns fewer labels than input, fallback to individual."""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        # Batch returns only 2 out of 3 labels
        def mock_batch(prompts_list, existing):
            return {0: "标签0", 1: "标签1"}  # Missing index 2
        manager._generate_region_labels_batch = mock_batch

        # Individual fallback for missing region
        manager._generate_region_label = lambda summaries, existing: "fallback标签"

        entity_summaries_list = [
            ["Python(skill)"],
            ["任飞(person)"],
            ["雄安分行(org)"],
        ]
        existing = []
        labels = manager._generate_labels(entity_summaries_list, existing)

        assert len(labels) == 3
        assert labels[0] == "标签0"
        assert labels[1] == "标签1"
        assert labels[2] == "fallback标签"
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest tests/test_region_manager.py::TestBatchLabelGeneration -v`
Expected: FAIL — `_generate_labels` 和 `_generate_region_labels_batch` 方法不存在

- [ ] **Step 3: 实现批量 LLM 调用方法**

在 `niu_api/internal/region_manager.py` 的 `_generate_region_label()` 之后添加：

```python
def _generate_labels(
    self,
    entity_summaries_list: list[list[str]],
    existing_regions: list[str],
) -> list[str]:
    """Generate labels for multiple regions, using batch or individual calls.

    Uses batch LLM call for 3+ regions, individual for fewer.
    """
    if len(entity_summaries_list) >= 3:
        try:
            batch_result = self._generate_region_labels_batch(
                entity_summaries_list, existing_regions
            )
            # Check if batch returned all labels
            labels = []
            missing_indices = []
            for i in range(len(entity_summaries_list)):
                if i in batch_result:
                    labels.append(batch_result[i])
                else:
                    labels.append(None)
                    missing_indices.append(i)

            # Fallback to individual for missing
            for i in missing_indices:
                try:
                    label = self._generate_region_label(
                        entity_summaries_list[i], existing_regions
                    )
                    labels[i] = label
                except Exception:
                    labels[i] = entity_summaries_list[i][0].split("(")[0] if entity_summaries_list[i] else "unknown"

            # De-duplicate: if batch LLM returned same label for multiple regions, add suffixes
            # Reserve 1 char for suffix digit: truncate base to 7 chars
            seen_labels = set(existing_regions)
            for i, label in enumerate(labels):
                if label is not None and label in seen_labels:
                    base = label[:7]
                    n = 2
                    candidate = f"{base}{n}"
                    while candidate in seen_labels and n < 10:
                        n += 1
                        candidate = f"{base}{n}"
                    labels[i] = candidate
                if label is not None:
                    seen_labels.add(label)

            # Final truncation to 8 chars (safety net)
            for i, label in enumerate(labels):
                if label is not None and len(label) > 8:
                    labels[i] = label[:8]

            return labels
        except Exception as e:
            logger.warning("Batch label generation failed: %s, falling back to individual", e)
            # Fall through to individual calls

    # Individual calls for < 3 regions or batch failure
    labels = []
    for entity_summaries in entity_summaries_list:
        label = self._generate_region_label(entity_summaries, existing_regions)
        labels.append(label)
        existing_regions = existing_regions + [label]  # Avoid in-place mutation

    return labels

def _generate_region_labels_batch(
    self,
    entity_summaries_list: list[list[str]],
    existing_regions: list[str],
) -> dict[int, str]:
    """Generate labels for all regions in a single LLM call.

    Returns dict of {index: label} for successfully parsed regions.
    """
    # Build batch prompt
    community_lines = []
    for i, entity_summaries in enumerate(entity_summaries_list):
        entity_parts = []
        for s in entity_summaries[:20]:  # Top 20 per community
            entity_parts.append(s)
        community_lines.append(f"社区{i}实体：{', '.join(entity_parts)}")

    existing_str = ", ".join(existing_regions) if existing_regions else "无"
    communities_str = "\n".join(community_lines)

    prompt = (
        "你是一个知识图谱分析师。根据以下社区内的实体列表，为每个社区生成一个简洁的中文标签名。\n\n"
        "要求：\n"
        "- 每个标签8个字以下\n"
        "- 概括该社区实体的共同主题\n"
        "- 不要跟现有脑区重名或语义接近\n"
        "- 只能返回JSON格式：{\"regions\": [{\"id\": 0, \"label\": \"标签1\"}, ...]}\n"
        "- 返回其他任何格式或内容将判定失败\n\n"
        f"现有脑区：{existing_str}\n\n"
        f"{communities_str}"
    )

    # Token truncation
    try:
        import litellm
        token_count = litellm.token_counter(model="gpt-4o", text=prompt)
        context_window = _read_context_window_size()
        if token_count > context_window - 500:
            # Truncate from tail communities
            while len(community_lines) > 1 and token_count > context_window - 500:
                community_lines.pop()
                communities_str = "\n".join(community_lines)
                prompt = (
                    "你是一个知识图谱分析师。根据以下社区内的实体列表，为每个社区生成一个简洁的中文标签名。\n\n"
                    "要求：\n"
                    "- 每个标签8个字以下\n"
                    "- 概括该社区实体的共同主题\n"
                    "- 不要跟现有脑区重名或语义接近\n"
                    "- 只能返回JSON格式：{\"regions\": [{\"id\": 0, \"label\": \"标签1\"}, ...]}\n"
                    "- 返回其他任何格式或内容将判定失败\n\n"
                    f"现有脑区：{existing_str}\n\n"
                    f"{communities_str}"
                )
                token_count = litellm.token_counter(model="gpt-4o", text=prompt)
    except Exception:
        pass

    # Call LLM
    content = self._call_llm_for_label(prompt)

    # Parse batch response
    import json
    try:
        data = json.loads(content.strip())
        if isinstance(data, dict) and "regions" in data:
            result = {}
            for item in data["regions"]:
                idx = item.get("id")
                label = str(item.get("label", "")).strip()
                if idx is not None and label and len(label) <= 8:
                    result[int(idx)] = label
            return result
    except (json.JSONDecodeError, ValueError):
        pass

    # Try regex fallback for batch
    result = {}
    for match in re.finditer(r'"id"\s*:\s*(\d+)\s*,\s*"label"\s*:\s*"([^"]+)"', content):
        idx = int(match.group(1))
        label = match.group(2).strip()
        if label and len(label) <= 8:
            result[idx] = label

    return result
```

- [ ] **Step 4: 替换 `create_region_nodes()` 使用批量调用**

**NOTE: This completely replaces the `create_region_nodes` from Task 4 Step 3. Delete the Task 4 version and write this one instead.**

在 `create_region_nodes()` 中，将循环内的 `_generate_region_label()` 调用替换为先收集所有 entity_summaries，再统一调用 `_generate_labels()`：

修改 `create_region_nodes()` 方法，在循环之前收集所有有效的 entity_summaries 列表，然后调用 `_generate_labels()` 一次性获取所有标签，再在循环中使用对应的标签。

具体改动：将原来的 `for partition in partition_result.partitions:` 循环拆分为两遍：
- 第一遍：过滤有效社区、构建 entity_summaries 列表
- 调用 `_generate_labels()` 获取所有标签
- 第二遍：使用标签构建 entities/relationships/chunks

```python
def create_region_nodes(
    self,
    partition_result: CommunityDetectionResult,
) -> list[str]:
    """Create master nodes + relationships for each community

    Uses batch injection: collects all entities, relationships and chunks first,
    then calls inject_custom_kg once.

    Args:
        partition_result: Community detection result from M1

    Returns:
        List of created region names (e.g. ["编程开发脑区", ...])
    """
    all_entities: list[dict] = []
    all_relationships: list[dict] = []
    all_chunks: list[dict] = []
    created_regions: list[str] = []

    # Pre-fetch existing region labels for LLM dedup
    existing_labels: list[str] = []
    try:
        for region in self.get_all_regions():
            existing_labels.append(region.label)
    except Exception:
        pass

    # Pass 1: Filter valid communities and collect data
    valid_communities: list[tuple[Any, list[str], list[str]]] = []  # (partition, members, entity_summaries)
    for partition in partition_result.partitions:
        members = [
            name
            for name in partition.entity_names
            if not (name.endswith(REGION_SUFFIX) or name.startswith(REGION_PREFIX))
        ]
        if not members or len(members) < MIN_COMMUNITY_SIZE:
            logger.debug(
                "社区 %d 成员数 %d < %d，跳过",
                partition.region_id,
                len(members),
                MIN_COMMUNITY_SIZE,
            )
            continue

        entity_summaries = self._build_entity_summaries(
            members, partition.entity_types, partition.entity_name_to_type
        )
        valid_communities.append((partition, members, entity_summaries))

    # Pass 2: Generate all labels (batch for 3+, individual for fewer)
    entity_summaries_list = [es for _, _, es in valid_communities]
    labels = self._generate_labels(entity_summaries_list, existing_labels)

    # Pass 3: Build entities, relationships, chunks using generated labels
    for (partition, members, entity_summaries), region_label in zip(valid_communities, labels):
        region_summary = self._generate_region_summary(entity_summaries)
        representative = members[0].replace("<SEP>", "-").replace("|", "-") if members else ""
        community_id = f"community_{partition.region_id}"
        now = time.time()
        region_name = f"{region_label}{REGION_SUFFIX}"

        description = _encode_description(
            summary=region_summary,
            region_id=community_id,
            size=len(members),
            representative=representative,
            updated_at=now,
        )

        all_entities.append({
            "entity_name": region_name,
            "entity_type": REGION_ENTITY_TYPE,
            "description": description,
            "source_id": REGION_SOURCE_ID,
        })

        top_members = members[:MAX_SUMMARY_ENTITIES]
        chunk_source_id = f"{REGION_SOURCE_ID}_{region_name}"

        all_chunks.append({
            "content": f"{region_label}脑区：{', '.join(top_members)}",
            "source_id": chunk_source_id,
            "file_path": REGION_FILE_PATH,
        })

        all_relationships.append({
            "src_id": NIU_ENTITY,
            "tgt_id": region_name,
            "keywords": ANCHOR_RELATION,
            "description": f"Brain region anchor: {region_label}",
            "weight": 0.5,
            "source_id": REGION_SOURCE_ID,
            "file_path": REGION_FILE_PATH,
        })

        for member in members:
            all_relationships.append({
                "src_id": region_name,
                "tgt_id": member,
                "keywords": BELONGS_TO_RELATION,
                "description": f"{member} belongs to region {region_label}",
                "weight": 0.5,
                "source_id": REGION_SOURCE_ID,
                "file_path": REGION_FILE_PATH,
            })

        created_regions.append(region_name)
        logger.info(
            "收集脑区节点: %s (社区 %d, %d 成员, 代表: %s)",
            region_name,
            partition.region_id,
            len(members),
            representative,
        )

    # Batch inject all collected data in one call
    if all_entities or all_relationships:
        result = self._ingester.inject_custom_kg(
            entities=all_entities,
            relationships=all_relationships,
            chunks=all_chunks,
            source_id=REGION_SOURCE_ID,
        )
        if isinstance(result, dict) and result.get("status") == "error":
            logger.warning(
                "批量注入脑区实体失败: %s (collected %d regions)",
                result.get("message", "unknown"),
                len(created_regions),
            )
            return []
        logger.info(
            "批量注入 %d 个脑区实体, %d 条关系, %d 个chunks",
            len(all_entities),
            len(all_relationships),
            len(all_chunks),
        )

    logger.info("共创建 %d 个脑区节点", len(created_regions))
    return created_regions
```

- [ ] **Step 5: 更新 Task 4 的测试以匹配批量调用**

Task 6 修改 `create_region_nodes()` 后调用的是 `_generate_labels()` 而非 `_generate_region_label()`，需要更新 `TestCreateRegionNodesWithLLMLabel` 的 mock：

```python
# 旧 mock（Task 4 阶段）：
# manager._generate_region_label = lambda summaries, existing: "编程开发"

# 新 mock（Task 6 阶段）：
manager._generate_labels = lambda summaries_list, existing: ["编程开发"] * len(summaries_list)
```

将 `TestCreateRegionNodesWithLLMLabel` 中所有 `_generate_region_label` 的 mock 替换为 `_generate_labels`。

同时更新 `TestCreateRegionNodes`（旧测试类，Task 4 Step 5 添加的 mock）：

```python
# 旧 mock（Task 4 Step 5 阶段）：
# manager._generate_region_label = lambda summaries, existing: summaries[0].split("(")[0] if summaries else "unknown"

# 新 mock（Task 6 阶段）：
manager._generate_labels = lambda summaries_list, existing: [
    summaries[0].split("(")[0] if summaries else "unknown"
    for summaries in summaries_list
]
```

- [ ] **Step 6: 运行测试验证通过**

Run: `pytest tests/test_region_manager.py::TestBatchLabelGeneration tests/test_region_manager.py::TestCreateRegionNodesWithLLMLabel -v`
Expected: PASS

- [ ] **Step 7: 运行全部测试确认无回归**

Run: `pytest tests/test_region_manager.py -v`
Expected: 全部 PASS

- [ ] **Step 8: 提交**

```bash
git add niu_api/internal/region_manager.py tests/test_region_manager.py
git commit -m "feat: batch LLM label generation for 3+ regions with fallback"
```

---

### Task 7: summary 展示层处理

**背景：** `<SEP>` 格式的 summary 是给向量检索用的，前端展示时需要替换为人类可读的 `"、"` 分隔符。需要在 `_parse_description()` 的返回值中标注原始格式，以及在 `get_all_regions()` 返回的 `BrainRegionInfo.description` 中做展示层替换。

**Files:**
- Modify: `niu_api/internal/region_manager.py` — `get_all_regions()` 中对 summary 做展示替换
- Test: `tests/test_region_manager.py`

- [ ] **Step 1: 写失败测试 — summary 展示格式**

在 `tests/test_region_manager.py` 新增测试类：

```python
class TestSummaryDisplayFormat:
    """Test that BrainRegionInfo.description uses readable separator for display."""

    def test_description_replaces_sep_with_chinese_comma(self):
        """BrainRegionInfo.description should replace <SEP> with '、' for display."""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        # Setup: region with <SEP> format summary
        adapter.list_entities.return_value = {
            "status": "ok",
            "data": [
                {
                    "id": "编程开发脑区",
                    "description": _encode_description(
                        summary="Python<SEP>Django<SEP>FastAPI",
                        region_id="community_0",
                        size=3,
                        representative="Python",
                        updated_at=1000.0,
                    ),
                },
            ],
        }

        regions = manager.get_all_regions()
        assert len(regions) == 1
        # Display format should use "、" separator
        assert regions[0].description == "Python、Django、FastAPI"
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest tests/test_region_manager.py::TestSummaryDisplayFormat -v`
Expected: FAIL — 当前 `get_all_regions()` 返回的 description 仍包含 `<SEP>`

- [ ] **Step 3: 修改 `get_all_regions()` 中的 summary 展示替换**

在 `get_all_regions()` 方法中，构建 `BrainRegionInfo` 时，对 `parsed.get("summary", "")` 做替换：

```python
            # 将 <SEP> 替换为 "、" 用于前端展示
            display_summary = parsed.get("summary", "").replace("<SEP>", "、")

            regions.append(
                BrainRegionInfo(
                    name=entity_name,
                    label=label,
                    community_id=parsed.get("region_id", ""),
                    description=display_summary,
                    size=int(parsed.get("size", "0") or "0"),
                    representative=parsed.get("representative", ""),
                    members=[],
                    updated_at=float(parsed.get("updated_at", "0") or "0"),
                )
            )
```

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest tests/test_region_manager.py::TestSummaryDisplayFormat -v`
Expected: PASS

- [ ] **Step 5: 运行全部测试确认无回归**

Run: `pytest tests/test_region_manager.py -v`
Expected: 全部 PASS

- [ ] **Step 6: 提交**

```bash
git add niu_api/internal/region_manager.py tests/test_region_manager.py
git commit -m "feat: replace <SEP> with '、' in BrainRegionInfo.description for display"
```

---

### Task 8: 全量集成测试

**背景：** 运行所有相关测试文件，确认改动无回归。同时验证 `_summarize_region()` 的旧调用路径仍然工作（用于向后兼容的地方）。

**Files:**
- No new files

- [ ] **Step 1: 运行全部脑区相关测试**

Run: `pytest tests/test_region_manager.py tests/test_region_detector.py tests/test_region_sync.py tests/test_region_injector.py tests/test_default_regions.py -v`
Expected: 全部 PASS

- [ ] **Step 2: 运行脑区集成测试（如果 leidenalg 可用）**

Run: `pytest tests/test_brain_region_integration.py -v -k "not e2e"`
Expected: PASS（可能需要跳过某些需要真实 LightRAG 的测试）

- [ ] **Step 3: 最终提交（如有遗漏的修复）**

```bash
git add -A
git commit -m "fix: resolve remaining test failures from brain region LLM naming refactor"
```
