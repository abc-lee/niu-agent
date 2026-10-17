// force-graph renderer — based on vasturiano/force-graph (d3-force engine)

// ===== Type Colors =====
const typeColors = {
  person: '#4A90D9', organization: '#5CB85C', technology: '#17BECF',
  concept: '#E06B9E', location: '#9B59B6', event: '#F39C12',
  document: '#E8A838', photo: '#1ABC9C', video: '#8E44AD',
  note: '#2ECC71', chat: '#7F8C8D', skill: '#3498DB',
  tool: '#27AE60', knowledge: '#F1C40F', interactionhabit: '#95A5A6', episodicevent: '#E74C3C',
  brainregion: '#6C5CE7', other: '#95A5A6',
};

const typeLabels = {
  person: '人物', organization: '组织', technology: '技术',
  concept: '概念', location: '地点', event: '事件',
  document: '文档', photo: '图片', video: '视频',
  note: '便利贴', chat: '对话', skill: '技能',
  tool: '工具', knowledge: '知识', interactionhabit: '习惯', episodicevent: '情景记忆',
  brainregion: '脑区', other: '其他',
};

// ===== Node Type Mapping =====
function mapNodeType(node) {
  if (node.nodeType === 'Document') {
    const sourceMap = { photo: 'photo', video: 'video', note: 'note', chat: 'chat' };
    return sourceMap[node.source] || 'document';
  }
  if (node.nodeType === 'Concept') return 'concept';
  const entityType = (node.entityType || '').toLowerCase();
  if (typeColors[entityType]) return entityType;
  return 'other';
}

function getNodeColor(node) {
  return typeColors[mapNodeType(node)] || typeColors.other;
}

function getNodeLabel(node) {
  const t = mapNodeType(node);
  return typeLabels[t] || t;
}

// ===== HTML Escape =====
function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  // Also escape quotes for safe use in HTML attribute values (e.g. data-node-id="...")
  return div.innerHTML.replace(/"/g, '&quot;');
}

// ===== Media Detection =====
const MEDIA_EXTENSIONS = ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.mp4', '.mov', '.avi', '.webm'];
const IMAGE_EXTENSIONS = ['.jpg', '.jpeg', '.png', '.gif', '.webp'];

function getMediaType(uri) {
  if (!uri) return null;
  const lower = uri.toLowerCase();
  const ext = MEDIA_EXTENSIONS.find(e => lower.endsWith(e));
  if (!ext) return null;
  return IMAGE_EXTENSIONS.includes(ext) ? 'image' : 'video';
}

// Build a file:/// URL from a local filesystem path.
// encodeURIComponent each path segment (encodes #, ?, space, CJK, etc.)
// while preserving the / separators — mirrors chat.html's convention.
// escapeHtml is applied by callers for safe use in HTML attribute values.
function toFileUrl(p) {
  const normalized = String(p).replace(/\\/g, '/');
  return 'file:///' + normalized.split('/').map(encodeURIComponent).join('/');
}

// ===== Local File Path Detection =====
function isLocalFilePath(uri) {
  if (!uri || typeof uri !== 'string') return false;
  // Windows absolute path: C:\... or \\server\share...
  if (/^[A-Za-z]:[\\\/]/.test(uri) || /^\\\\/.test(uri)) return true;
  // Unix absolute path: /home/...
  if (/^\/[^\/]/.test(uri)) return true;
  return false;
}

// ===== Current Graph Data =====
let currentData = { nodes: [], edges: [] };
let currentPerspective = null;
let currentMatchIds = null; // null = no search active
let _justReplacedData = false;
let _searchInProgress = false;
let _subgraphMode = false;        // 是否处于子图态
let _subgraphCenterId = null;     // 子图中心实体 ID
let _subgraphDepth = 1;           // 当前扩散层数（1-5）
let _subgraphRequestId = 0;       // 请求序列号，防止快速点击竞态

// ===== Edge Count Cache =====
let edgeCountCache = {};

function buildEdgeCountCache() {
  edgeCountCache = {};
  currentData.edges.forEach(edge => {
    edgeCountCache[edge.source] = (edgeCountCache[edge.source] || 0) + 1;
    edgeCountCache[edge.target] = (edgeCountCache[edge.target] || 0) + 1;
  });
}

function countEdges(nodeId) {
  return edgeCountCache[nodeId] || 0;
}

// ===== Core Node Detection =====
function isCoreNode(node) {
  if (!currentPerspective) return false;
  const orig = node._originalData || node;
  if (!orig) return false;
  // Unified matching: use mapNodeType() for all types
  // This handles both Entity types (Photo, Person, etc.) and Document subtypes
  return mapNodeType(orig) === currentPerspective;
}

// ===== Node Size Calculation =====
function getNodeSize(nodeId, asCore) {
  const edgeCount = countEdges(nodeId);
  const base = asCore ? 3 : 2;
  const scale = asCore ? 2 : 1.5;
  return base + Math.log(edgeCount + 1) * scale;
}

// ===== Loading State Helpers =====
const showLoading = () => {
  const el = document.getElementById('loading-overlay');
  if (el) el.style.display = 'flex';
};
const hideLoading = () => {
  const el = document.getElementById('loading-overlay');
  if (el) el.style.display = 'none';
};
const showEmpty = () => {
  const el = document.getElementById('empty-state');
  if (el) el.style.display = 'flex';
};
const hideEmpty = () => {
  const el = document.getElementById('empty-state');
  if (el) el.style.display = 'none';
};

// ===== Graph Unavailable Helpers (E3 D7: 简单提示 + 首屏节流) =====
// kg_api 三端点把 adapter error dict 转成错误响应（body 为 {"status":"error","message":...}）；
// 链路 renderer → electronAPI → ipcMain → apiRequest 不检查 HTTP 状态码（任何状态都
// JSON.parse 后 resolve）——所以这里判 body.status，不判 HTTP 状态。真空（空图/无结果）
// 保持 200 + 空数据，只有 status=="error" 走此分支。
const isGraphError = (body) => !!(body && body.status === 'error');

let _unavailableHintShown = false; // 页面会话内首屏提示只显示一次

const showUnavailableOverlay = () => {
  if (_unavailableHintShown) return;
  _unavailableHintShown = true;
  const el = document.getElementById('unavailable-overlay');
  if (el) el.style.display = 'flex';
};

const markGraphUnavailable = (label) => {
  console.warn(`[graph] ${label}`);
  const badge = document.getElementById('kg-status-badge');
  if (badge) {
    badge.textContent = label;
    badge.style.display = 'flex';
  }
};

const clearGraphUnavailable = () => {
  const el = document.getElementById('unavailable-overlay');
  if (el) el.style.display = 'none';
  const badge = document.getElementById('kg-status-badge');
  if (badge) badge.style.display = 'none';
};

// ===== Build force-graph data from currentData =====
// Preserves existing node positions (x, y, vx, vy) from the force-graph
// to avoid visual "jumping" on incremental updates.
let _prevNodePositions = {}; // id -> {x, y, vx, vy}

function pruneStalePositions() {
  const liveIds = new Set(currentData.nodes.map(n => n.id));
  for (const id of Object.keys(_prevNodePositions)) {
    if (!liveIds.has(id)) delete _prevNodePositions[id];
  }
}

function buildGraphData() {
  // Snapshot current positions from force-graph before rebuilding
  try {
    const fgData = graph.graphData();
    if (fgData && fgData.nodes) {
      fgData.nodes.forEach(n => {
        if (n.x != null && Number.isFinite(n.x) && Number.isFinite(n.y)) {
          _prevNodePositions[n.id] = {
            x: n.x, y: n.y,
            vx: Number.isFinite(n.vx) ? n.vx : 0,
            vy: Number.isFinite(n.vy) ? n.vy : 0,
          };
        }
      });
    }
  } catch (_) { /* graph not initialized yet */ }

  const nodes = currentData.nodes.map(node => {
    const visualType = mapNodeType(node);
    const color = typeColors[visualType] || typeColors.other;
    const asCore = currentPerspective ? isCoreNode(node) : false;
    const isMatch = currentMatchIds ? currentMatchIds.has(node.id) : false;

    let val, opacity;
    if (currentMatchIds) {
      val = isMatch ? getNodeSize(node.id, true) : getNodeSize(node.id, false);
      opacity = isMatch ? 1 : 0.35;
    } else {
      val = getNodeSize(node.id, asCore);
      opacity = asCore ? 0.95 : (currentPerspective ? 0.4 : 0.75);
    }

    const fgNode = {
      id: node.id,
      label: node.label || node.name || node.id,
      color: color,
      val: val,
      opacity: opacity,
      _originalData: node,
      _visualType: visualType,
    };

    // Restore position from previous layout to avoid jumping
    const prev = _prevNodePositions[node.id];
    if (prev && Number.isFinite(prev.x) && Number.isFinite(prev.y)) {
      fgNode.x = prev.x;
      fgNode.y = prev.y;
      fgNode.vx = Number.isFinite(prev.vx) ? prev.vx : 0;
      fgNode.vy = Number.isFinite(prev.vy) ? prev.vy : 0;
    }

    return fgNode;
  });

  // Build node ID set for filtering dangling edges
  const nodeIdSet = new Set(nodes.map(n => n.id));

  const links = currentData.edges
    .filter(edge => nodeIdSet.has(edge.source) && nodeIdSet.has(edge.target))
    .map(edge => {
      const isMatch = currentMatchIds
        ? (currentMatchIds.has(edge.source) || currentMatchIds.has(edge.target))
        : true;
      return {
        source: edge.source,
        target: edge.target,
        relation: edge.relation || '',
        color: isMatch ? 'rgba(0,0,0,0.12)' : 'rgba(0,0,0,0.04)',
        width: isMatch ? 1 : 0.5,
      };
    });

  pruneStalePositions();
  return { nodes, links };
}

// ===== Initialize force-graph =====
const container = document.getElementById('graph-container');

const graph = ForceGraph()(container)
  .backgroundColor('transparent')
  .nodeId('id')
  .nodeLabel('')
  .nodeVal(d => d.val)
  .nodeColor(d => d.color)
  .nodeCanvasObjectMode(() => 'replace')
  .nodeCanvasObject((node, ctx, globalScale) => {
    const size = Math.max(2, node.val || 4);
    const isSelected = node.id === currentSelectedNode;
    const isFlashing = flashNodeIds.has(node.id);

    // Flash glow (larger pulsing ring)
    if (isFlashing) {
      ctx.beginPath();
      ctx.arc(node.x, node.y, size + 8, 0, 2 * Math.PI);
      ctx.fillStyle = node.color;
      ctx.globalAlpha = 0.4;
      ctx.fill();
      ctx.globalAlpha = 1;
    }

    // Glow ring for selected node
    if (isSelected) {
      ctx.beginPath();
      ctx.arc(node.x, node.y, size + 4, 0, 2 * Math.PI);
      ctx.fillStyle = node.color;
      ctx.globalAlpha = 0.25;
      ctx.fill();
      ctx.globalAlpha = 1;
    }

    // Circle
    ctx.beginPath();
    ctx.arc(node.x, node.y, size, 0, 2 * Math.PI);
    ctx.fillStyle = node.color;
    ctx.globalAlpha = node.opacity ?? 0.75;
    ctx.fill();
    ctx.globalAlpha = 1;
  })
  .linkSource('source')
  .linkTarget('target')
  .linkColor(d => d.color)
  .linkWidth(d => d.width)
  .linkCurvature(0)
  .d3AlphaDecay(0.0228)
  .d3AlphaMin(0.001)
  .d3VelocityDecay(0.4)
  .minZoom(0.0001) // 允许 zoomToFit 大幅缩小（全图几千节点时 bbox 巨大，默认 0.01 会挡缩小）
  .onNodeClick((node) => {
    showDetail(node.id);
  })
  .onNodeRightClick(async (node) => {
    if (_subgraphMode) {
      await enterSubgraph(node.id, _subgraphDepth);
    } else if (node._originalData && node._originalData.nodeType !== 'Document') {
      const success = await enterSubgraph(node.id, 1);
      if (success) updateSubgraphControls();
    }
  })
  .onBackgroundClick(() => {
    hideDetail();
  })
  .onNodeHover(node => {
    if (node) {
      showTooltip(node);
    } else {
      hideTooltip();
    }
  });

// Configure d3-force parameters — apply BEFORE graphData() so simulation uses correct forces from the start
function applyForceConfig() {
  const chargeForce = graph.d3Force('charge');
  if (chargeForce) chargeForce.strength(-30);
  const linkForce = graph.d3Force('link');
  if (linkForce) linkForce.distance(30).strength(link => 1 / Math.min(link.source.links?.length || 1, link.target.links?.length || 1));
}

// ===== Load Graph Data =====
async function loadGraphSnapshot() {
  showLoading();
  hideEmpty();

  try {
    const snapshot = await window.electronAPI.getGraphSnapshot(2000, 0);
    if (isGraphError(snapshot)) {
      // E3 D7：不可用 ≠ 空库——首屏提示一次（遮罩）+ 状态角标，不渲染空库遮罩
      markGraphUnavailable('知识图谱不可用');
      showUnavailableOverlay();
      hideLoading();
      return;
    }
    currentData = { nodes: snapshot.nodes || [], edges: snapshot.edges || [] };

    if (currentData.nodes.length === 0) {
      hideLoading();
      showEmpty();
      return;
    }

    buildEdgeCountCache();
    applyForceConfig(); // Apply forces BEFORE graphData so simulation uses correct parameters from the start
    const data = buildGraphData();
    graph.graphData(data);
    updateStats();

    // 等 graphData 的 digest（1ms）初始化节点坐标后再缩放（与全局重绘同一参数，统一观感）
    setTimeout(() => graph.zoomToFit(400, 60), 20);
  } catch (error) {
    console.error('Failed to load graph:', error);
    hideLoading();
    showEmpty();
    return;
  }

  hideLoading();
}

loadGraphSnapshot();

// ===== Real-time Sync (1s polling) =====
let syncSince = null; // timestamp of last seen change
let syncTimer = null;

function startSync() {
  if (syncTimer) return;
  syncTimer = setInterval(pollChangelog, 1000);
}

function stopSync() {
  if (syncTimer) {
    clearInterval(syncTimer);
    syncTimer = null;
  }
}

let _polling = false; // guard against concurrent pollChangelog executions
async function pollChangelog() {
  if (_polling) return; // previous poll still in progress (async await)
  _polling = true;
  try {
    const result = await window.electronAPI.kgChangelog(syncSince);
    const changes = result.changes || [];

    if (changes.length === 0) return;

    // Track latest timestamp — but defer update until after snapshot_refresh
    // to avoid advancing syncSince past a failed refresh event.
    const latestTs = changes.reduce((max, c) => {
      return (!max || c.timestamp > max) ? c.timestamp : max;
    }, null);

    // Skip incremental merge if we just replaced the entire graph
    // (selectSearchEntity already set syncSince to current time)
    if (_justReplacedData) {
      if (latestTs) syncSince = latestTs;
      return;
    }

    if (_subgraphMode) {
      return;  // 不推进 syncSince，退出子图态后自然恢复
    }

    // Health check: detect and auto-repair NaN positions in force-graph.
    // When d3-force computes NaN (e.g. from dangling edges or force misconfiguration),
    // canvas arc() silently fails — nodes become invisible but still clickable.
    // This check runs every poll (1s) and resets the graph if corruption is detected.
    try {
      const fgData = graph.graphData();
      if (fgData && fgData.nodes && fgData.nodes.length > 0) {
        const nanNodes = fgData.nodes.filter(n => !Number.isFinite(n.x) || !Number.isFinite(n.y));
        if (nanNodes.length > 0) {
          console.warn(`[graph] NaN positions detected: ${nanNodes.length}/${fgData.nodes.length} nodes. Auto-repairing.`);
          // Clear the poisoned position cache and rebuild from currentData
          _prevNodePositions = {};
          buildEdgeCountCache();
          graph.graphData(buildGraphData());
          updateStats();
        }
      }
    } catch (_) { /* graph not initialized yet */ }

    // Merge incremental changes into currentData
    let changed = false;
    const existingIds = new Set(currentData.nodes.map(n => n.id));

    changes.forEach(change => {
      if (change.type === 'entity_created' && change.data) {
        const id = change.data.id;
        if (!existingIds.has(id)) {
          currentData.nodes.push({
            id: id,
            label: change.data.name || id,
            name: change.data.name || id,
            nodeType: 'Entity',
            entityType: change.data.type || 'other',
            description: (change.data.description || '').replace(/<SEP>/g, ' '),
            uri: change.data.file_path || '',
            source: change.data.source_id || '',
          });
          existingIds.add(id);
          changed = true;
        }
      } else if (change.type === 'document_created' && change.data) {
        const id = change.data.uri || change.data.id;
        if (!existingIds.has(id)) {
          currentData.nodes.push({
            id: id,
            label: change.data.title || id,
            name: change.data.title || id,
            nodeType: 'Document',
            source: change.data.source || 'document',
            uri: change.data.uri || '',
          });
          existingIds.add(id);
          changed = true;
        }
      } else if (change.type === 'edge_created' && change.data) {
        const src = change.data.source;
        const tgt = change.data.target;
        if (src && tgt && existingIds.has(src) && existingIds.has(tgt)) {
          const edgeExists = currentData.edges.some(
            e => e.source === src && e.target === tgt && e.relation === (change.data.relation || '')
          );
          if (!edgeExists) {
            currentData.edges.push({
              source: src,
              target: tgt,
              relation: change.data.relation || '',
              confidence: change.data.confidence || 0.5,
              edgeType: change.data.edge_type || 'RELATED_TO',
            });
            changed = true;
          }
        }
      } else if (change.type === 'entity_deleted' && change.data) {
        const id = change.data.id;
        currentData.nodes = currentData.nodes.filter(n => n.id !== id);
        currentData.edges = currentData.edges.filter(e => e.source !== id && e.target !== id);
        existingIds.delete(id);
        changed = true;
      } else if (change.type === 'entity_merged' && change.data) {
        const sourceIds = change.data.source_ids || [];
        const targetId = change.data.target_id;
        // Reconnect edges from source nodes to target node (not delete)
        sourceIds.forEach(srcId => {
          currentData.edges = currentData.edges.map(e => {
            if (e.source === srcId) return { ...e, source: targetId };
            if (e.target === srcId) return { ...e, target: targetId };
            return e;
          });
          // Remove source node
          currentData.nodes = currentData.nodes.filter(n => n.id !== srcId);
          existingIds.delete(srcId);
        });
        // Deduplicate edges after reconnection (same source+target+relation)
        // and remove self-loops (e.g. A→B reconnected to A→A when A and B merge)
        const edgeKeys = new Set();
        currentData.edges = currentData.edges.filter(e => {
          if (e.source === e.target) return false;
          const key = `${e.source}|${e.target}|${e.relation || ''}`;
          if (edgeKeys.has(key)) return false;
          edgeKeys.add(key);
          return true;
        });
        // Update or create target node with merged attributes
        if (targetId) {
          const existing = currentData.nodes.find(n => n.id === targetId);
          if (existing) {
            existing.label = change.data.name || existing.label;
            existing.name = change.data.name || existing.name;
            existing.entityType = change.data.type || existing.entityType;
            existing.description = (change.data.description || existing.description || '').replace(/<SEP>/g, ' ');
          } else {
            currentData.nodes.push({
              id: targetId,
              label: change.data.name || targetId,
              name: change.data.name || targetId,
              nodeType: 'Entity',
              entityType: change.data.type || 'other',
              description: (change.data.description || '').replace(/<SEP>/g, ' '),
            });
            existingIds.add(targetId);
          }
        }
        changed = true;
      }
    });

    // Handle snapshot_refresh separately (needs await, can't be inside forEach)
    const refreshEvent = changes.find(c => c.type === 'snapshot_refresh');
    if (refreshEvent) {
      try {
        const snapshot = await window.electronAPI.getGraphSnapshot(2000, 0);
        if (isGraphError(snapshot)) {
          // 快照仍不可用——保留不可用遮罩/角标；syncSince 照常推进避免无限重试
        } else {
          // 快照 fetch 成功 = 图谱后端已恢复可用——撤掉不可用遮罩与角标。
          // 空数据也清除（空库恢复：后端可用但暂无实体——展示空态而非不可用态）。
          clearGraphUnavailable();
          const newNodes = snapshot.nodes || [];
          const newEdges = snapshot.edges || [];
          // Only replace currentData if snapshot has data.
          // During pipeline processing, LightRAG's NetworkX graph may be
          // temporarily empty (entities being merged), causing snapshot to
          // return {nodes: [], edges: []}. Replacing currentData with empty
          // data would make the entire graph vanish — the "sudden blank" bug.
          if (newNodes.length > 0) {
            currentData = { nodes: newNodes, edges: newEdges };
            changed = true;
            hideEmpty();
          } else {
            // Snapshot returned empty — don't replace, show empty state and
            // wait for backend to emit another refresh when data is available.
            showEmpty();
          }
        }
      } catch (e) {
        // Snapshot fetch failed — syncSince still advances to avoid infinite retry
      }
    }

    // Advance syncSince after all processing.
    // Even if snapshot_refresh returned empty data, advance syncSince to avoid
    // infinite re-fetch of the same snapshot_refresh event. The backend will
    // emit a new snapshot_refresh when data is available again.
    if (latestTs) {
      syncSince = latestTs;
    }

    if (changed) {
      // 增量合并成功（entity_created/edge_created 等）或快照替换成功——
      // 图谱已恢复可用：撤掉不可用遮罩/角标，隐藏空态（数据已存在）。
      // 覆盖正常恢复路径：增量事件不产生 snapshot_refresh，只有这里能清除。
      clearGraphUnavailable();
      hideEmpty();
      // Rebuild graph data from currentData (source of truth) instead of
      // mutating force-graph's internal state. Mutating the internal fgData
      // object (which has d3-resolved node references in link.source/target)
      // causes rendering corruption — nodes appear without edges, simulation
      // freezes, and the graph becomes unresponsive until restart.
      buildEdgeCountCache();
      const freshData = buildGraphData(); // includes pruneStalePositions()
      graph.graphData(freshData);
      updateStats();
    }
  } catch (err) {
    console.error("[graph] pollChangelog error:", err);
  } finally {
    _polling = false;
  }
}

// Start sync after initial load succeeds
// (syncSince is set to current time so we only get future changes)
setTimeout(() => {
  syncSince = new Date().toISOString();
  startSync();
}, 2000);

// Stop sync when page is hidden / window closed
document.addEventListener('visibilitychange', () => {
  if (document.hidden) stopSync();
  else startSync();
});

// ===== Update Stats =====
const updateStats = () => {
  const prefix = _subgraphMode ? '子图: ' : '';
  const statsEl = document.getElementById('stats');
  statsEl.innerHTML = `<span class="stat-item"><strong>${prefix}${currentData.nodes.length}</strong> 节点</span><span class="stat-item"><strong>${currentData.edges.length}</strong> 关系</span>`;
};

// ===== Re-layout helper =====
// force-graph: just set new data, d3-force simulation auto-reheats
// No clear/render needed — smooth animated transition

function reLayout() {
  buildEdgeCountCache();
  applyForceConfig();
  const data = buildGraphData();
  // 视角切换：丢弃恢复的旧位置，节点从初始螺旋布局起步（与初次加载/子图返回一致，否则 zoomToFit 基于分散 bbox 缩得过小）
  data.nodes.forEach(n => { delete n.x; delete n.y; delete n.vx; delete n.vy; });
  graph.graphData(data);
  // 等 graphData 的 digest（1ms）初始化节点坐标后再缩放
  setTimeout(() => graph.zoomToFit(400, 60), 20);
}

// ===== Perspective Mode =====
const perspBtns = document.querySelectorAll('.persp-btn');

function updatePerspectiveButtons() {
  perspBtns.forEach(btn => {
    if (btn.dataset.core === currentPerspective) {
      btn.classList.add('active');
    } else {
      btn.classList.remove('active');
    }
  });
}

function switchPerspective(coreType) {
  currentPerspective = (currentPerspective === coreType) ? null : coreType;
  updatePerspectiveButtons();
  reLayout();
}

perspBtns.forEach(btn => {
  btn.addEventListener('click', () => {
    switchPerspective(btn.dataset.core);
  });
});

// ===== Tooltip =====
const tooltip = document.getElementById('node-tooltip');

function showTooltip(node) {
  const orig = node._originalData;
  if (!orig) return;
  const name = orig.label || orig.name || orig.id;
  const typeLabel = getNodeLabel(orig);
  tooltip.innerHTML = `<div>${escapeHtml(name)}</div><div class="tooltip-type">${escapeHtml(typeLabel)}</div>`;
  if (_subgraphMode) {
    tooltip.innerHTML += '<div class="tooltip-type" style="margin-top:4px;opacity:0.5;">右键点击：以此为中心扩散</div>';
  } else if (orig.nodeType !== 'Document') {
    tooltip.innerHTML += '<div class="tooltip-type" style="margin-top:4px;opacity:0.5;">右键点击：以此为中心进入子图</div>';
  }
  tooltip.classList.remove('hidden');
  // Position near mouse — force-graph doesn't give mouse coords in hover,
  // so we position near the node's screen coordinates
  const coords = graph.graph2ScreenCoords(node.x, node.y);
  if (!Number.isFinite(coords.x) || !Number.isFinite(coords.y)) return;
  tooltip.style.left = (coords.x + 15) + 'px';
  tooltip.style.top = (coords.y - 10) + 'px';
}

// ===== Flash nodes (blink effect) =====
let flashNodeIds = new Set(); // currently flashing node IDs
let flashTimer = null;

function flashNodes(nodeIds) {
  // Clear previous flash
  if (flashTimer) clearInterval(flashTimer);
  flashNodeIds = new Set(nodeIds);

  let count = 0;
  const maxBlinks = 6; // 3 full blinks (on/off)
  flashTimer = setInterval(() => {
    count++;
    if (count > maxBlinks) {
      clearInterval(flashTimer);
      flashTimer = null;
      flashNodeIds = new Set();
      // Final redraw to clear
      const c = graph.centerAt();
      graph.centerAt(c.x, c.y);
      return;
    }
    // Toggle: even = show flash, odd = hide flash
    flashNodeIds = (count % 2 === 1) ? new Set(nodeIds) : new Set();
    const c = graph.centerAt();
    graph.centerAt(c.x, c.y);
  }, 200);
}

function flashNode(nodeId) {
  flashNodes([nodeId]);
}

function hideTooltip() {
  tooltip.classList.add('hidden');
}

// ===== Detail Panel =====
let currentSelectedNode = null;
const detailPanel = document.getElementById('detail-panel');
const detailTitle = document.getElementById('detail-title');
const detailContent = document.getElementById('detail-content');
const closeDetail = document.getElementById('close-detail');
const focusNodeBtn = document.getElementById('focus-node');

const showDetail = (nodeId) => {
  const orig = currentData.nodes.find(n => n.id === nodeId);
  if (!orig) return;
  currentSelectedNode = nodeId;

  const visualType = mapNodeType(orig);
  const color = typeColors[visualType] || typeColors.other;
  detailTitle.textContent = orig.label || orig.name || orig.id;
  detailTitle.style.color = color;

  let html = '';
  html += `<div class="detail-row"><span class="detail-label">类型：</span>${escapeHtml(getNodeLabel(orig))}</div>`;

  if (orig.entityType && orig.nodeType === 'Entity') {
    html += `<div class="detail-row"><span class="detail-label">实体类型：</span>${escapeHtml(orig.entityType)}</div>`;
  }
  if (orig.description) {
    const cleanDesc = orig.description.replace(/<SEP>/g, ' ');
    html += `<div class="detail-row"><span class="detail-label">描述：</span>${escapeHtml(cleanDesc)}</div>`;
  }
  if (orig.source) {
    html += `<div class="detail-row"><span class="detail-label">来源：</span>${escapeHtml(orig.source)}</div>`;
  }

  // Media thumbnail for nodes with file URI (documents, photos, videos, etc.)
  if (orig.uri) {
    const mediaType = getMediaType(orig.uri);
    if (mediaType === 'image') {
      html += `<div class="detail-media"><img src="${escapeHtml(toFileUrl(orig.uri))}" alt="preview"></div>`;
    } else if (mediaType === 'video') {
      html += `<div class="detail-media"><video src="${escapeHtml(toFileUrl(orig.uri))}" controls style="max-width:100%;border-radius:8px;"></video></div>`;
    }
  }

  // Related edges
  const relatedEdges = currentData.edges.filter(e => e.source === orig.id || e.target === orig.id);
  if (relatedEdges.length > 0) {
    html += `<div class="detail-row" style="margin-top:8px;"><strong>关系 (${relatedEdges.length})</strong></div>`;
    html += `<div class="relation-list">`;
    relatedEdges.forEach(edge => {
      const otherId = edge.source === orig.id ? edge.target : edge.source;
      const otherNode = currentData.nodes.find(n => n.id === otherId);
      if (otherNode) {
        const otherColor = getNodeColor(otherNode);
        const relLabel = edge.relation || '';
        const otherName = otherNode.label || otherNode.name;
        html += `<div class="relation-item" data-node-id="${escapeHtml(otherId)}" style="cursor:pointer;"><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${otherColor};margin-right:6px;vertical-align:middle;"></span>`;
        if (relLabel) html += `<strong>${escapeHtml(relLabel)}：</strong>`;
        html += `${escapeHtml(otherName)}</div>`;
      }
    });
    html += `</div>`;
  }

  detailContent.innerHTML = html;
  detailPanel.classList.remove('hidden');

  // Bind click on relation items to flash the target node
  detailContent.querySelectorAll('.relation-item').forEach(item => {
    item.addEventListener('click', () => {
      flashNode(item.dataset.nodeId);
    });
    item.addEventListener('contextmenu', async (e) => {
      e.preventDefault();
      const otherId = item.dataset.nodeId;
      if (_subgraphMode) {
        await enterSubgraph(otherId, _subgraphDepth);
      } else {
        const otherNode = currentData.nodes.find(n => n.id === otherId);
        if (otherNode && otherNode.nodeType !== 'Document') {
          const success = await enterSubgraph(otherId, 1);
          if (success) updateSubgraphControls();
        }
      }
    });
  });

  // Enable/disable file buttons based on whether node has a local file URI
  const hasFile = isLocalFilePath(orig.uri);
  openFileBtn.disabled = !hasFile;
  openFolderBtn.disabled = !hasFile;

  // Trigger redraw to show selection glow
  const c = graph.centerAt();
  graph.centerAt(c.x, c.y);
};

const hideDetail = () => {
  detailPanel.classList.add('hidden');
  currentSelectedNode = null;
  // Trigger redraw to remove selection glow
  const c = graph.centerAt();
  graph.centerAt(c.x, c.y);
};

closeDetail.addEventListener('click', hideDetail);

// ===== Open File / Open Folder =====
const openFileBtn = document.getElementById('open-file');
const openFolderBtn = document.getElementById('open-folder');

openFileBtn.addEventListener('click', () => {
  if (!currentSelectedNode) return;
  const orig = currentData.nodes.find(n => n.id === currentSelectedNode);
  if (orig && isLocalFilePath(orig.uri)) {
    window.electronAPI.openPath(orig.uri);
  }
});

openFolderBtn.addEventListener('click', () => {
  if (!currentSelectedNode) return;
  const orig = currentData.nodes.find(n => n.id === currentSelectedNode);
  if (orig && isLocalFilePath(orig.uri)) {
    window.electronAPI.showItemInFolder(orig.uri);
  }
});

focusNodeBtn.addEventListener('click', () => {
  if (!currentSelectedNode) return;
  // 聚焦目标节点 + 直接邻居（包含目标+邻居，缩放合理，不强制放大）
  const neighborIds = new Set([currentSelectedNode]);
  currentData.edges.forEach(e => {
    if (e.source === currentSelectedNode) neighborIds.add(e.target);
    if (e.target === currentSelectedNode) neighborIds.add(e.source);
  });
  graph.zoomToFit(800, 60, n => neighborIds.has(n.id));
});

// ===== Search =====
// 两段式搜索（2026-10-14 口径：同时搜知识图谱 + 媒体向量库，结果分格栏：上 = 知识图谱、下 = 照片（媒体））。
// 时序纪律（I-10）：两路并行——图谱段按图谱响应一到即落定（媒体段保持「媒体搜索中…」占位）；
// 媒体段在响应到达或 renderer 侧超时（MEDIA_SEARCH_TIMEOUT_MS）后落定。最坏情形（编码后端被媒体入库
// 编码占忙、文本查询排在编码队尾；或图谱后端假死时逐命中节点解析每命中可达数十秒）由 renderer 超时
// 截断；IPC 的 30s 硬超时与路由本身不变。媒体链路的任何失败只落在媒体段（可辨行），图谱段结果照常
// 渲染（I-9）。陈旧响应守卫（I-10）：_searchRequestId 每轮搜索 +1，所有 DOM 写入先对 ID——超时竞态
// 的落败方迟到解析、新一轮搜索、清空/Esc 关闭后到的旧响应，一律不碰 DOM。
const searchInput = document.getElementById('searchInput');
const searchDropdown = document.getElementById('search-dropdown');

const MEDIA_SEARCH_TIMEOUT_MS = 12000; // renderer 侧媒体搜索超时（I-10）：截断最坏编码排队等待
let _searchRequestId = 0; // 每轮搜索 +1：本轮所有 DOM 写入以它为界，陈旧响应丢弃

// 关闭下拉列表
function closeSearchDropdown() {
  searchDropdown.classList.add('hidden');
  searchDropdown.innerHTML = '';
  searchDropdown.style.left = '';
  searchDropdown.style.top = '';
  searchDropdown.style.minWidth = '';
}

// 点击页面其他区域时关闭下拉列表
document.addEventListener('click', (e) => {
  if (!e.target.closest('.search-wrapper') && !e.target.closest('#search-dropdown')) {
    closeSearchDropdown();
  }
});

// SENTINEL-Begin buildSearchSectionModel
// 两段渲染的纯决策函数（无 DOM 依赖；tests/graph_search_media_ui.js 按哨兵注释提取后驱动断言）。
// kgState / mediaState ∈ {kind: 'pending'|'items'|'empty'|'error', pending?, items?, message?}（pending 占位文案 = 显式 pending 字段，v1.1 F4：不再按标题字符串硬匹配）
//   items（图谱段）= [{name, type, score?}]（形状同既有下拉项）；items（媒体段）= [{name, mediaType, score?}]（降级命中不进 items——构造处已过滤，本函数带同构过滤兜底，v1.5；媒体分支多一道 mediaScoreCutoff 相对及格线兜底）
// 返回 [图谱段, 媒体段]：{title, kind, message?, items?}——两段恒同时存在（空段也占位，一眼分清来源，I-3）；命中全被过滤 ⇒ kind:'empty'「无结果」（I-6 v1.5）。
function buildSearchSectionModel(kgState, mediaState) {
  const section = (title, key, st) => {
    if (st.kind === 'items') {
      let items = (st.items || []).filter(it => !it || it.degraded !== true); // 降级命中不进下拉（v1.5 I-6）
      if (key === 'media') items = mediaScoreCutoff(items);                    // 相对及格线兜底（构造处已过滤一次）
      if (items.length > 0) {
        return { title, kind: 'items', items };
      }
      return { title, kind: 'empty', message: '无结果' }; // 命中全被过滤（全降级）⇒ 按空段处理，不是失败（I-6 v1.5）
    }
    if (st.kind === 'error') {
      return { title, kind: 'error', message: st.message || title + ' 不可用' };
    }
    if (st.kind === 'pending') {
      return { title, kind: 'pending', message: st.pending };
    }
    return { title, kind: 'empty', message: '无结果' }; // 空 = 占位行，不是失败（I-4）
  };
  return [section('知识图谱', 'kg', kgState), section('照片（媒体）', 'media', mediaState)];
}
// SENTINEL-End buildSearchSectionModel
// SENTINEL-Begin mediaTypeLabel
// 媒体段类型标签（I-3）：photo→照片 / video→视频 / image→图片，其余原样显示 media_type
function mediaTypeLabel(mediaType) {
  const labels = { photo: '照片', video: '视频', image: '图片' };
  return labels[mediaType] || mediaType || '';
}
// SENTINEL-End mediaTypeLabel
// SENTINEL-Begin finiteScore
// 数值护栏纯函数（无 DOM）：null/undefined/''/布尔/非数值/NaN/Infinity ⇒ null（"没有分数"，与"分数为 0"区分）；
// 其余 ⇒ 数值。及格线与徽标共用（避免 Number(null) === 0 的坑）。
// 布尔单列一行：Number(true) === 1 / Number(false) === 0 会**穿透**，而"分数"语义上不接受布尔（R3-A P3-2）。
function finiteScore(raw) {
  if (raw === null || raw === undefined || raw === '' || typeof raw === 'boolean') return null;
  const s = Number(raw);
  return Number.isFinite(s) ? s : null;
}
// SENTINEL-End finiteScore
// SENTINEL-Begin mediaScoreCutoff
// 媒体段「相对及格线」纯函数（无 DOM；探针可断言；构造处与模型层双调用，第二遍是**幂等**兜底）。
// 口径（用户 2026-10-15）：只显示与最佳匹配接近的命中，明显落后的尾巴不显示。
//   - 有效 score 里取 max 为 top；保留 score >= top - GAP
//   - 无有效 score 的条目 ⇒ 保留（fail-open），同时不参与 top 计算
//   - 全无有效 score ⇒ 原样返回；top 自身必过线 ⇒ 非空输入不会变空
//   - **调用方须先过滤降级命中**：本函数不做降级过滤（降级项若参与算 top 会把线抬高）；
//     探针把本块当自包含单元求值，故顺序约束在此写明（R1-A P3-5）
//   - **幂等**：存活集的 max 与 top 同值 ⇒ 第二遍谓词与第一遍完全一致，不会误删
const MEDIA_SCORE_RELATIVE_GAP = 0.03;
function mediaScoreCutoff(items) {
  const list = items || [];
  let top = null;
  for (const it of list) {
    const s = finiteScore(it && it.score);
    if (s !== null && (top === null || s > top)) top = s;
  }
  if (top === null) return list.slice();
  return list.filter(it => {
    const s = finiteScore(it && it.score);
    return s === null ? true : s >= top - MEDIA_SCORE_RELATIVE_GAP;
  });
}
// SENTINEL-End mediaScoreCutoff
// SENTINEL-Begin formatScore
// 徽标文本：有效数值 ⇒ 两位小数；缺失/无效 ⇒ 空串（调用处据此不渲染 span，不留空占位）
function formatScore(score) {
  const s = finiteScore(score);
  return s === null ? '' : s.toFixed(2);
}
// SENTINEL-End formatScore

// SENTINEL-Begin buildSearchItemHtml
// 单条下拉条目 HTML 装配（无 DOM 依赖，探针可断言）：KG 段与既有模板逐字同；媒体段用 mediaTypeLabel；
// 降级命中在构造/模型层已被过滤、不进 items（v1.5 I-6）——本函数只装配正常条目（无降级条目的渲染分支）。
function buildSearchItemHtml(sectionKey, item) {
  const typeText = sectionKey === 'media' ? mediaTypeLabel(item.mediaType) : (item.type || '');
  const scoreText = formatScore(item.score);
  const scoreSpan = scoreText ? `<span class="entity-score">${scoreText}</span>` : '';
  return `<div class="search-dropdown-item" data-section="${sectionKey}" data-name="${escapeHtml(item.name)}"><span class="entity-name">${escapeHtml(item.name)}</span><span class="entity-type">${escapeHtml(typeText)}</span>${scoreSpan}</div>`;
}
// SENTINEL-End buildSearchItemHtml

const _sectionStates = { kg: { kind: 'pending', pending: '搜索中…' }, media: { kind: 'pending', pending: '媒体搜索中…' } };

// 单段写入（薄 DOM 装配：条目 HTML 装配全部委托 buildSearchItemHtml 哨兵纯函数——探针可断言；
// KG 段条目模板与既有逐字一致——I-3/I-5；降级命中在构造/模型层已过滤、零渲染——I-6 v1.5）
function paintSection(sectionKey, state) {
  _sectionStates[sectionKey] = state;
  const model = buildSearchSectionModel(_sectionStates.kg, _sectionStates.media);
  const sec = model[sectionKey === 'kg' ? 0 : 1];
  const body = searchDropdown.querySelector(`.search-dropdown-section-body[data-body="${sectionKey}"]`);
  if (!body) return; // 骨架未建立 / 下拉已关闭（清空/Esc）——!body 空守卫：放锁后媒体腿的迟到写**与 Esc / 点外部关闭下拉后的迟到写**（Esc 不递增 `_searchRequestId`，此守卫是唯一拦截者）都在此被挡（I-10）
  if (sec.kind === 'items') {
    let html = '';
    sec.items.forEach(item => {
      html += buildSearchItemHtml(sectionKey, item);
    });
    body.innerHTML = html;
  } else if (sec.kind === 'error') {
    body.innerHTML = `<div class="search-dropdown-section-note search-dropdown-section-note--error">${escapeHtml(sec.message || '')}</div>`;
  } else {
    // pending / empty：占位行（搜索中… / 媒体搜索中… / 无结果）
    body.innerHTML = `<div class="search-dropdown-section-note">${escapeHtml(sec.message || '')}</div>`;
  }
}

// 两段骨架：搜索开始即建立（两段独立落定；未落定的段保持占位行——一眼分清哪一路来源还在等）
function ensureSectionSkeleton() {
  _sectionStates.kg = { kind: 'pending', pending: '搜索中…' };
  _sectionStates.media = { kind: 'pending', pending: '媒体搜索中…' };
  searchDropdown.innerHTML =
    '<div class="search-dropdown-section" data-section="kg">' +
    '<div class="search-dropdown-section-title">知识图谱</div>' +
    '<div class="search-dropdown-section-body" data-body="kg"><div class="search-dropdown-section-note">搜索中…</div></div>' +
    '</div>' +
    '<div class="search-dropdown-section" data-section="media">' +
    '<div class="search-dropdown-section-title">照片（媒体）</div>' +
    '<div class="search-dropdown-section-body" data-body="media"><div class="search-dropdown-section-note">媒体搜索中…</div></div>' +
    '</div>';
}

// 点击 = 事件委托（容器上绑一次，innerHTML 重写后仍有效）：两段条目都走既有选中语义
// selectSearchEntity——实体名即图谱节点 id（kg_api 侧 id = name；D-15/D-16 媒体向量按实体名绑定
// 既有图谱节点）⇒ 开子图并聚焦 ⇒ 点节点弹详情页（详情页早有照片预览，无新增 UI，I-5）；
// 降级的媒体条目（图谱里无节点等）不进下拉（构造/模型层已过滤，无行可点，I-6 v1.5）。
searchDropdown.addEventListener('click', (e) => {
  const item = e.target.closest('.search-dropdown-item[data-section]');
  if (!item) return;
  selectSearchEntity({ id: item.dataset.name, name: item.dataset.name });
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

  const mySearchId = ++_searchRequestId; // 陈旧响应守卫（I-10）
  const stillCurrent = () => mySearchId === _searchRequestId;

  try {
    // 显示两段加载状态（段占位行即可辨：上 = 图谱搜索中、下 = 媒体搜索中）
    ensureSectionSkeleton();
    searchDropdown.classList.remove('hidden');
    // Position dropdown below the search input (body-level element)
    const inputRect = searchInput.getBoundingClientRect();
    searchDropdown.style.left = inputRect.left + 'px';
    searchDropdown.style.top = (inputRect.bottom + 4) + 'px';
    searchDropdown.style.minWidth = inputRect.width + 'px';

    // 两腿时序化（v1.6）：图谱腿立即发起；媒体腿在图谱段绘出后才发起——1 万张照片场景：图谱先显示、媒体段继续转圈、何时搜完何时显示；
    // 媒体腿内部 = 响应与 renderer 侧超时竞态（I-10）；两腿各自捕获，失败只落在各自动段（I-9）
    let kgPromise;
    try {
      kgPromise = window.electronAPI.searchEntities(query, 20)
        .catch(err => { console.error('Search entities failed:', err); return { __netError: String((err && err.message) || err) }; });
    } catch (e) {
      // preload 半程偏斜（旧 preload / 手工半回滚）下同步 throw 不逃逸：转本腿可辨失败值（走「搜索失败」分支），另一腿照常（I-9/I-10）
      kgPromise = Promise.resolve({ __netError: String((e && e.message) || e) });
    }
    let mediaTimeout = null; // 超时定时器句柄：竞态落定后统一 clearTimeout，IPC 先胜出也不留空跑计时器（v1.1 F5）

    // —— 媒体段：按自身响应或超时落定（I-10 的 12s 截断行在媒体腿发起后按时出现）；失败只落在媒体段，图谱段无论已落定或仍在占位都不受影响（I-9/I-10）——
    const paintMedia = (mediaResult) => {
      if (!stillCurrent()) return;
      if (mediaResult && mediaResult.status === 'ok') {
        // v1.5（I-6）：降级命中（图谱无节点 / 节点字段空 / 解析异常，三者同源）不进下拉——构造处过滤；
        // 纯函数层 buildSearchSectionModel 带同构过滤兜底
        const results = mediaScoreCutoff((mediaResult.results || []).filter(it => it.degraded !== true));
        if (results.length > 0) {
          paintSection('media', {
            kind: 'items',
            items: results.map(it => ({ name: it.entity_name, mediaType: it.media_type || '', score: it.score })),
          });
        } else {
          // 过滤后为空（含命中全为降级）⇒ 空段口径「无结果」（v1.5）
          paintSection('media', { kind: 'empty' });
        }
      } else {
        paintSection('media', { kind: 'error', message: (mediaResult && mediaResult.message) || '媒体搜索不可用' });
      }
    };
    // 媒体腿发起 = 图谱段绘出之后（v1.6）：图谱先显示，媒体段保持占位、何时落定何时显示（1 万张照片场景）；
    // 发起时机移到图谱落定之后，但 startMediaLeg 内部的竞态/超时/失败语义不变（I-9/I-10）；
    // .then 挂点仍与图谱腿 settle 时刻解耦——靠 stillCurrent() / paintSection !body 空守卫兜底，不靠时序（v1.3 G1 的教训保留）
    const startMediaLeg = () => {
      let mediaPromise;
      try {
        mediaPromise = Promise.race([
          window.electronAPI.searchMedia(query, 20)
            .catch(err => ({ status: 'error', message: `媒体搜索不可用（${String((err && err.message) || err)}）` })),
          new Promise(resolve => {
            mediaTimeout = setTimeout(() => resolve({ status: 'error', message: '媒体搜索不可用（超时：编码后端可能被媒体入库编码占忙）' }), MEDIA_SEARCH_TIMEOUT_MS);
          }),
        ]).then(r => {
          if (mediaTimeout) { clearTimeout(mediaTimeout); mediaTimeout = null; } // 竞态落定即清定时器（超时已触发则为空操作）
          return r;
        });
      } catch (e) {
        // 同上：媒体腿可辨失败行（I-9）
        mediaPromise = Promise.resolve({ status: 'error', message: `媒体搜索不可用（${String((e && e.message) || e)}）` });
      }
      // 媒体腿落定即绘
      mediaPromise.then(paintMedia);
    };

    // —— 图谱段：按其自身响应落定（既有成功/失败纪律原样保留；失败分支文案与分支形态逐字同旧搜索块，不透后端原始报文）——
    const kgResult = await kgPromise;
    if (stillCurrent()) {
      if (kgResult && kgResult.__netError) {
        // 网络层失败（IPC reject）：既有「搜索失败」分支——不弹状态角标（与旧块 catch 分支一致）
        paintSection('kg', { kind: 'error', message: '搜索失败' });
      } else if (isGraphError(kgResult)) {
        // E3 D7：检索失败显式分支——不再把 error 伪装成"未找到匹配实体"（文案同旧块，不透后端原始报文）
        markGraphUnavailable('检索失败');
        paintSection('kg', { kind: 'error', message: '检索失败' });
      } else if (kgResult && kgResult.error) {
        // 既有「搜索出错」分支（search_entities 异常 dict）
        paintSection('kg', { kind: 'error', message: '搜索出错' });
      } else {
        const entities = (kgResult && kgResult.entities) || [];
        if (entities.length > 0) {
          // 搜索结果展示成功 = 图谱后端可用——撤掉状态角标（后续成功操作清除角标）
          clearGraphUnavailable();
          paintSection('kg', { kind: 'items', items: entities.map(ent => ({ name: ent.name, type: ent.entityType || '', score: ent.score })) });
        } else {
          paintSection('kg', { kind: 'empty' }); // 空 ≠ 失败：不动状态角标（I-4）
        }
      }
    }
    // 图谱结果已绘出：此刻才发起媒体腿——媒体段保持占位继续搜索、何时搜完何时显示（1 万张照片场景要求图谱先显示，v1.6）
    if (stillCurrent()) startMediaLeg();
    // 图谱段落定即放锁（等价旧搜索块的 finally 放锁点：图谱落定后再次搜索立即可发，不被媒体腿拖至多 12s）；
    // 媒体腿在**自身落定时**渲染（`mediaPromise.then`）——靠 stillCurrent() 挡新轮开始后的迟到落定、靠 paintSection 的 !body 空守卫
    // 挡关下拉后的迟到写。try/finally 兜底（v1.1 F2）：主流程任何同步 throw（ensureSectionSkeleton/paintSection
    // 内 throw、renderer 与 preload 版本偏斜时 searchMedia 为 undefined 的同步 TypeError）不致搜索框永久锁死。
    _searchInProgress = false;

    // 竞态的落败方（超时定时器或迟到 IPC）仍在后台解析——其结果过 stillCurrent 守卫后丢弃，永不碰 DOM（I-10）
  } finally {
    // v1.3 G2：本轮仍是当前轮才放锁——主放锁点（`_searchInProgress = false;`）之前任何 throw 路径上本轮仍持锁
    // （新一轮的锁检查 `if (_searchInProgress) return;` 必被挡下）⇒ stillCurrent() 必为真，兜底不失效；
    // 本轮已被新轮取代则不碰新轮的锁位（v1.2 的无条件 finally 会抹掉新轮锁位）
    if (stillCurrent()) _searchInProgress = false;
  }
});

// 选中实体 — 进入子图模式
async function selectSearchEntity(entity) {
  closeSearchDropdown();
  _justReplacedData = true;  // Block pollChangelog BEFORE the await
  const success = await enterSubgraph(entity.id, 1);
  if (success) {
    updateSubgraphControls();
  }
}

async function enterSubgraph(entityId, depth) {
  _subgraphRequestId++;
  const myRequestId = _subgraphRequestId;
  _justReplacedData = true;
  try {
    const result = await window.electronAPI.exploreNode(entityId, depth, 0, 'both');
    if (myRequestId !== _subgraphRequestId) return false;
    if (isGraphError(result)) {
      // E3 D7：后续操作失败——仅 console + 状态角标，不重复弹提示
      _justReplacedData = false;
      markGraphUnavailable('知识图谱不可用');
      return false;
    }
    if (!result.nodes || result.nodes.length === 0) {
      _justReplacedData = false;
      if (_subgraphMode) await exitSubgraph();
      return false;
    }
    currentData = { nodes: result.nodes, edges: result.edges || [] };
    _prevNodePositions = {};
    currentPerspective = null;
    currentMatchIds = null;
    buildEdgeCountCache();
    applyForceConfig();
    const freshData = buildGraphData();
    graph.graphData(freshData);
    // 等 graphData 的 digest（1ms）初始化节点坐标后再缩放
    setTimeout(() => graph.zoomToFit(400, 60), 20);
    if (!_subgraphMode) {
      // 首次进入子图（搜索）：聚焦目标节点及直接邻居
      setTimeout(() => {
        if (myRequestId !== _subgraphRequestId) return; // 已被更新的操作取代
        if (!_subgraphMode || _subgraphCenterId !== entityId) return; // 已退出或中心改变
        const editor = graph.graphData().nodes;
        const targetNode = editor.find(n => n.id === entityId);
        if (targetNode && Number.isFinite(targetNode.x) && Number.isFinite(targetNode.y)) {
          const neighborIds = new Set([entityId]);
          currentData.edges.forEach(e => {
            if (e.source === entityId) neighborIds.add(e.target);
            if (e.target === entityId) neighborIds.add(e.source);
          });
          if (neighborIds.size > 1) {
            graph.zoomToFit(400, 60, n => neighborIds.has(n.id));
          } else {
            graph.centerAt(targetNode.x, targetNode.y, 800);
          }
        }
      }, 150);
    }
    updateStats();
    // 子图状态在 _justReplacedData=false 之前设置，使 pollChangelog 守卫更内聚
    _subgraphMode = true;
    _subgraphCenterId = entityId;
    _subgraphDepth = depth;
    _justReplacedData = false;

    currentSelectedNode = entityId;
    showDetail(entityId);
    // 子图进入成功 = 图谱后端可用——撤掉状态角标（后续成功操作清除角标）
    clearGraphUnavailable();
    return true;
  } catch (err) {
    console.error('Failed to enter subgraph:', err);
    if (myRequestId !== _subgraphRequestId) return false;
    _justReplacedData = false;
    return false;
  }
}

async function exitSubgraph() {
  _subgraphRequestId++;
  _justReplacedData = true;
  showLoading();

  try {
    const snapshot = await window.electronAPI.getGraphSnapshot(2000, 0);
    if (isGraphError(snapshot)) {
      // E3 D7：错误 ≠ 空库——保留当前图，仅 console + 状态角标
      markGraphUnavailable('知识图谱不可用');
      _justReplacedData = false;
      hideLoading();
      return;
    }
    if (snapshot.nodes && snapshot.nodes.length > 0) {
      currentData = { nodes: snapshot.nodes, edges: snapshot.edges || [] };
      hideEmpty();
    } else {
      currentData = { nodes: [], edges: [] };
      hideLoading();
      showEmpty();
      _justReplacedData = false;
      return;
    }
  } catch (err) {
    console.error('Failed to re-fetch snapshot on exit:', err);
    _justReplacedData = false;
    hideLoading();
    return;
  }

  _subgraphMode = false;
  _subgraphCenterId = null;
  _subgraphDepth = 1;
  updateSubgraphControls();
  syncSince = new Date().toISOString();

  try {
    _prevNodePositions = {};
    currentPerspective = null;
    currentMatchIds = null;
    buildEdgeCountCache();
    applyForceConfig();
    const freshData = buildGraphData();
    graph.graphData(freshData);
    // 等 graphData 的 digest（1ms）初始化节点坐标后再缩放
    setTimeout(() => graph.zoomToFit(400, 60), 20);
    updateStats();
  } finally {
    _justReplacedData = false;
    hideLoading();
    hideDetail();
  }
}

function updateSubgraphControls() {
  const controls = document.getElementById('subgraph-controls');
  if (_subgraphMode) {
    controls.classList.remove('hidden');
    document.getElementById('depth-display').textContent = _subgraphDepth;
  } else {
    controls.classList.add('hidden');
  }
  perspBtns.forEach(btn => {
    btn.disabled = _subgraphMode;
    btn.style.opacity = _subgraphMode ? '0.35' : '';
    btn.style.cursor = _subgraphMode ? 'not-allowed' : '';
  });
}

document.getElementById('depth-up').addEventListener('click', async () => {
  if (!_subgraphMode) return;
  const newDepth = Math.min(5, _subgraphDepth + 1);
  await enterSubgraph(_subgraphCenterId, newDepth);
  if (_subgraphDepth === newDepth) {
    document.getElementById('depth-display').textContent = newDepth;
  }
});

document.getElementById('depth-down').addEventListener('click', async () => {
  if (!_subgraphMode) return;
  const newDepth = Math.max(1, _subgraphDepth - 1);
  await enterSubgraph(_subgraphCenterId, newDepth);
  if (_subgraphDepth === newDepth) {
    document.getElementById('depth-display').textContent = newDepth;
  }
});

document.getElementById('exit-subgraph').addEventListener('click', () => {
  exitSubgraph();
});

// ===== Handle Window Resize =====
let resizeTimer = null;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    graph.width(container.offsetWidth);
    graph.height(container.offsetHeight);
    graph.zoomToFit(400, 60);
  }, 200);
});