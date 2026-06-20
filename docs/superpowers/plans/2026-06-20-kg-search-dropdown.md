# 知识图谱搜索栏改造：回车搜索 + 下拉选择 + 以实体为根刷新

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将知识图谱搜索栏从实时本地匹配改为回车触发语义搜索 + 下拉选择实体 + 以选中实体为根替换刷新图谱。

**Architecture:** 三层改造：(1) 后端新增 `/api/kg/search_entities` 端点，委托 `LightRAGAdapter.query_data(mode="local")` 做语义搜索；(2) Electron IPC 层注册新通道桥接前端调用；(3) 前端搜索栏改为回车触发搜索 + 下拉列表 + 选中后调用 `exploreNode` 替换刷新。保留原有闪烁效果。

**Tech Stack:** Python (FastAPI), LightRAG (向量语义搜索), Electron (IPC), force-graph (d3-force)

---

## 修改文件清单

| 文件 | 职责 |
|------|------|
| `niu_api/kg_api.py` | 新增 `/api/kg/search_entities` 端点 |
| `ui/graph/main.js` | 注册 `kg-search-entities` IPC 通道 |
| `ui/graph/preload.js` | 暴露 `searchEntities` 给渲染进程 |
| `ui/graph/index.html` | 搜索框下方添加下拉列表容器 |
| `ui/graph/styles.css` | 下拉列表样式 |
| `ui/graph/renderer.js` | 搜索逻辑改造：回车搜索、下拉渲染、选中刷新 |

---

### Task 1: 后端 — 新增 `/api/kg/search_entities` 端点

**Files:**
- Modify: `niu_api/kg_api.py`

**原因:** 现有 `/api/kg/entities` 只支持按 `entity_type` 精确过滤，不支持关键词搜索。需要新增语义搜索端点，复用 `LightRAGAdapter.query_data(mode="local")` 底层能力。

- [ ] **Step 1: 在 `kg_api.py` 中添加 `/api/kg/search_entities` 端点**

在文件末尾（`/api/kg/entities` 端点之后）添加：

```python
@app.get("/api/kg/search_entities")
def search_entities(query: str = "", top_k: int = 20):
    """按关键词语义搜索实体，返回匹配的实体列表（供前端搜索栏使用）。"""
    if not query.strip():
        return {"entities": []}

    try:
        adapter = _get_adapter()
        result = adapter.query_data(query=query, mode="local", top_k=top_k)

        if result is None:
            return {"entities": []}

        # query_data returns {status, data: {entities: [...], relationships: [...], chunks: [...]}}
        data = result.get("data", {})
        if not data:
            data = result
        raw_entities = data.get("entities", [])

        entities = []
        seen = set()
        for ent in raw_entities:
            name = ent.get("entity_name", "")
            if name and name not in seen:
                seen.add(name)
                entities.append({
                    "id": name,
                    "name": name,
                    "entity_type": ent.get("entity_type", ""),
                    "description": (ent.get("description", "") or "")[:120],
                })

        return {"entities": entities[:top_k]}
    except Exception as e:
        logger.error(f"search_entities failed: {e}")
        return {"entities": [], "error": str(e)}
```

**注意：** 使用 `def`（非 `async def`），因为 `query_data()` 是同步阻塞调用。FastAPI 会在线程池中运行普通 `def` 端点，不会阻塞 ASGI 事件循环。与 `kg_api.py` 中其他端点一致（见第497-499行注释）。

- [ ] **Step 2: 语法检查**

```bash
python -m py_compile niu_api/kg_api.py
```

- [ ] **Step 3: 提交**

```bash
git add niu_api/kg_api.py
git commit -m "feat: add /api/kg/search_entities endpoint for semantic entity search"
```

---

### Task 2: Electron IPC — 注册搜索通道

**Files:**
- Modify: `ui/graph/main.js`
- Modify: `ui/graph/preload.js`

- [ ] **Step 1: 在 `main.js` 中注册 `kg-search-entities` IPC 通道**

在 `kg-entities` handler 之后（约第80行后）添加：

```javascript
ipcMain.handle('kg-search-entities', async (event, query, topK) => {
  const params = new URLSearchParams({ query: query || '', top_k: topK || 20 });
  return apiRequest('GET', `/api/kg/search_entities?${params}`);
});
```

- [ ] **Step 2: 在 `preload.js` 中暴露 `searchEntities`**

在 `listEntities` 之后添加：

```javascript
  searchEntities: (query, topK) => ipcRenderer.invoke('kg-search-entities', query, topK),
```

- [ ] **Step 3: 提交**

```bash
git add ui/graph/main.js ui/graph/preload.js
git commit -m "feat: add kg-search-entities IPC channel for graph search"
```

---

### Task 3: 前端 HTML — 添加下拉列表容器

**Files:**
- Modify: `ui/graph/index.html`

- [ ] **Step 1: 在搜索框下方添加下拉列表容器**

将搜索框部分（第26-29行）：
```html
    <div class="search-box">
      <span class="search-icon">🔍</span>
      <input type="text" id="searchInput" placeholder="搜索节点...">
    </div>
```

改为：
```html
    <div class="search-wrapper">
      <div class="search-box">
        <span class="search-icon">🔍</span>
        <input type="text" id="searchInput" placeholder="输入关键词，回车搜索...">
      </div>
      <div id="search-dropdown" class="search-dropdown hidden"></div>
    </div>
```

- [ ] **Step 2: 提交**

```bash
git add ui/graph/index.html
git commit -m "feat: add search dropdown container in graph HTML"
```

---

### Task 4: 前端 CSS — 下拉列表样式

**Files:**
- Modify: `ui/graph/styles.css`

- [ ] **Step 1: 在搜索框样式之后（第60行后）添加下拉列表样式**

```css
/* Search dropdown */
.search-wrapper {
  position: relative;
}

.search-dropdown {
  position: absolute;
  top: 100%;
  left: 0;
  min-width: 240px;
  max-width: 360px;
  max-height: 320px;
  overflow-y: auto;
  background: #fff;
  border: 1px solid rgba(0, 0, 0, 0.12);
  border-radius: 8px;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.12);
  z-index: 100;
  margin-top: 4px;
}

.search-dropdown.hidden {
  display: none;
}

.search-dropdown-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  cursor: pointer;
  font-size: 13px;
  color: #2c2c2c;
  border-bottom: 1px solid rgba(0, 0, 0, 0.04);
  transition: background-color 0.15s;
}

.search-dropdown-item:last-child {
  border-bottom: none;
}

.search-dropdown-item:hover {
  background-color: rgba(0, 0, 0, 0.04);
}

.search-dropdown-item .entity-type {
  font-size: 11px;
  color: #999;
  flex-shrink: 0;
}

.search-dropdown-item .entity-name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.search-dropdown-empty {
  padding: 12px;
  text-align: center;
  font-size: 13px;
  color: #aaa;
}

.search-dropdown-loading {
  padding: 12px;
  text-align: center;
  font-size: 13px;
  color: #888;
}
```

- [ ] **Step 2: 提交**

```bash
git add ui/graph/styles.css
git commit -m "feat: add search dropdown styles for graph entity search"
```

---

### Task 5: 前端 JS — 搜索逻辑改造

**Files:**
- Modify: `ui/graph/renderer.js`

**原因:** 将搜索从实时本地匹配改为回车触发语义搜索 + 下拉选择 + 选中后以实体为根替换刷新图谱。

- [ ] **Step 1: 替换搜索逻辑**

将 `renderer.js` 第836-865行的搜索部分：
```javascript
// ===== Search =====
const searchInput = document.getElementById('searchInput');

searchInput.addEventListener('input', (e) => {
  const query = e.target.value.toLowerCase().trim();

  if (!query) {
    currentMatchIds = null;
    if (flashTimer) { clearInterval(flashTimer); flashTimer = null; flashNodeIds = new Set(); }
    reLayout();
    return;
  }

  currentMatchIds = new Set();
  currentData.nodes.forEach(node => {
    const label = (node.label || node.name || '').toLowerCase();
    const desc = (node.description || '').toLowerCase();
    if (label.includes(query) || desc.includes(query)) {
      currentMatchIds.add(node.id);
    }
  });

  reLayout();

  // 搜索匹配后，所有选中节点同时闪3下（延迟等待布局稳定）
  if (currentMatchIds.size > 0) {
    const matchIds = Array.from(currentMatchIds);
    setTimeout(() => flashNodes(matchIds), 600);
  }
});
```

替换为：
```javascript
// ===== Search =====
const searchInput = document.getElementById('searchInput');
const searchDropdown = document.getElementById('search-dropdown');

// 关闭下拉列表
function closeSearchDropdown() {
  searchDropdown.classList.add('hidden');
  searchDropdown.innerHTML = '';
}

// 点击页面其他区域时关闭下拉列表
document.addEventListener('click', (e) => {
  if (!e.target.closest('.search-wrapper')) {
    closeSearchDropdown();
  }
});

// 搜索框键盘事件：Enter 搜索，Escape 关闭下拉
searchInput.addEventListener('keydown', async (e) => {
  if (e.key === 'Escape') {
    closeSearchDropdown();
    return;
  }
  if (e.key !== 'Enter') return;

  const query = searchInput.value.trim();
  if (!query) {
    closeSearchDropdown();
    currentMatchIds = null;
    if (flashTimer) { clearInterval(flashTimer); flashTimer = null; flashNodeIds = new Set(); }
    return;
  }

  if (_searchInProgress) return;
  _searchInProgress = true;

  // 显示加载状态
  searchDropdown.innerHTML = '<div class="search-dropdown-loading">搜索中...</div>';
  searchDropdown.classList.remove('hidden');

  try {
    const result = await window.electronAPI.searchEntities(query, 20);
    const entities = result.entities || [];

    if (entities.length === 0) {
      searchDropdown.innerHTML = '<div class="search-dropdown-empty">未找到匹配实体</div>';
      return;
    }

    searchDropdown.innerHTML = '';
    entities.forEach(ent => {
      const item = document.createElement('div');
      item.className = 'search-dropdown-item';
      item.innerHTML = `<span class="entity-name">${escapeHtml(ent.name)}</span><span class="entity-type">${escapeHtml(ent.entity_type || '')}</span>`;
      item.addEventListener('click', () => selectSearchEntity(ent));
      searchDropdown.appendChild(item);
    });
  } catch (err) {
    console.error('Search entities failed:', err);
    searchDropdown.innerHTML = '<div class="search-dropdown-empty">搜索失败</div>';
  } finally {
    _searchInProgress = false;
  }
});

// 选中实体 — 以该实体为根替换刷新图谱
async function selectSearchEntity(entity) {
  closeSearchDropdown();

  try {
    const result = await window.electronAPI.exploreNode(entity.id, 2, 0, 'both');
    if (!result.nodes || result.nodes.length === 0) return;

    // /api/kg/explore 已通过 _normalize_nodes/_normalize_edges 返回标准格式，直接使用
    currentData = {
      nodes: result.nodes,
      edges: result.edges || [],
    };

    // 清除旧位置缓存，确保全新布局
    _prevNodePositions = {};

    // 重置 changelog 同步时间戳，防止旧增量数据污染替换后的聚焦视图
    syncSince = new Date().toISOString();
    _justReplacedData = true;

    currentPerspective = null;
    currentMatchIds = null;
    buildEdgeCountCache();
    const freshData = buildGraphData();
    graph.graphData(freshData);
    graph.zoomToFit(400, 40);
    updateStats();

    // 中心节点闪烁
    setTimeout(() => flashNodes([entity.id]), 600);
  } catch (err) {
    console.error('Failed to navigate to entity:', err);
  }
}
```

**注意：** 需要在 renderer.js 中做两处额外修改：

1. 在全局变量区域（`let currentData = ...` 附近）添加：
```javascript
let _justReplacedData = false;
let _searchInProgress = false;
```

2. 在 `pollChangelog` 函数中，在 `latestTs` 计算之后、增量合并开始之前（`let changed = false;` 之前）插入：
```javascript
    // Skip incremental merge if we just replaced the entire graph
    // (selectSearchEntity already set syncSince to current time)
    if (_justReplacedData) {
      _justReplacedData = false;
      if (latestTs) syncSince = latestTs;
      return;
    }
```

3. 在搜索框 keydown 监听器中，Enter 处理开头加入防重复提交：
```javascript
  if (_searchInProgress) return;
  _searchInProgress = true;
  try {
    // ... 搜索逻辑 ...
  } finally {
    _searchInProgress = false;
  }
```

4. `selectSearchEntity` 替换 currentData 后清除旧位置缓存：
```javascript
    _prevNodePositions = {};  // 清除旧位置缓存，确保全新布局
```

- [ ] **Step 2: 语法检查**（JS 无编译，用 Node 检查）

```bash
node -c ui/graph/renderer.js
```

- [ ] **Step 3: 提交**

```bash
git add ui/graph/renderer.js
git commit -m "feat: replace search with enter-triggered semantic search + dropdown + entity-rooted refresh"
```

---

## 验证步骤

1. 启动程序 `./niu`，打开知识图谱窗口
2. 在搜索框输入关键词（如"任飞"），按回车
3. 确认下拉列表弹出，显示匹配实体（名称 + 类型）
4. 点击某个实体，确认图谱清空并以该实体为根重新渲染
5. 确认中心节点闪三下
6. 按 ESC 或点击空白处，确认下拉列表关闭
7. 输入不存在的关键词，确认显示"未找到匹配实体"
8. 下拉列表实体多时（>8个），确认可滚动
