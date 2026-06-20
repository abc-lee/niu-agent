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
  .d3VelocityDecay(0.4)
  .cooldownTime(15000)
  .onNodeClick(node => {
    showDetail(node.id);
  })
  .onNodeRightClick(node => {
    // Double-click expand
    expandNode(node.id);
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

    // Initial load: zoom to fit once, then unregister
    let _initZoomPending = true;
    const onFirstStop = () => {
      if (!_initZoomPending) return;
      _initZoomPending = false;
      graph.zoomToFit(400, 40);
      graph.onEngineStop(() => {}); // Reset to no-op (null would crash force-graph)
    };
    graph.onEngineStop(onFirstStop);
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

    if (changes.length === 0) return;

    // Track latest timestamp — but defer update until after snapshot_refresh
    // to avoid advancing syncSince past a failed refresh event.
    const latestTs = changes.reduce((max, c) => {
      return (!max || c.timestamp > max) ? c.timestamp : max;
    }, null);

    // Skip incremental merge if we just replaced the entire graph
    // (selectSearchEntity already set syncSince to current time)
    if (_justReplacedData) {
      _justReplacedData = false;
      if (latestTs) syncSince = latestTs;
      return;
    }

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
            description: change.data.description || '',
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
            existing.description = change.data.description || existing.description;
          } else {
            currentData.nodes.push({
              id: targetId,
              label: change.data.name || targetId,
              name: change.data.name || targetId,
              nodeType: 'Entity',
              entityType: change.data.type || 'other',
              description: change.data.description || '',
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
        } else {
          // Snapshot returned empty — don't replace, wait for backend to emit another refresh
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
      // Rebuild graph data from currentData (source of truth) instead of
      // mutating force-graph's internal state. Mutating the internal fgData
      // object (which has d3-resolved node references in link.source/target)
      // causes rendering corruption — nodes appear without edges, simulation
      // freezes, and the graph becomes unresponsive until restart.
      // Cancel any pending reLayout zoomToFit since we're replacing graph data
      _reLayoutPending = false;
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
  const statsEl = document.getElementById('stats');
  statsEl.innerHTML = `<span class="stat-item"><strong>${currentData.nodes.length}</strong> 节点</span><span class="stat-item"><strong>${currentData.edges.length}</strong> 关系</span>`;
};

// ===== Re-layout helper =====
// force-graph: just set new data, d3-force simulation auto-reheats
// No clear/render needed — smooth animated transition
let _reLayoutPending = false; // guard against rapid consecutive reLayout calls
function reLayout() {
  buildEdgeCountCache();
  applyForceConfig(); // Apply forces BEFORE graphData so simulation uses correct parameters
  const data = buildGraphData();
  graph.graphData(data);
  // Wait for simulation to settle before zooming to fit, then unregister
  _reLayoutPending = true;
  const onLayoutStop = () => {
    if (!_reLayoutPending) return; // stale callback from a previous reLayout
    _reLayoutPending = false;
    graph.zoomToFit(400, 40);
    graph.onEngineStop(() => {}); // Reset to no-op (null would crash force-graph)
  };
  graph.onEngineStop(onLayoutStop);
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
    html += `<div class="detail-row"><span class="detail-label">描述：</span>${escapeHtml(orig.description)}</div>`;
  }
  if (orig.source) {
    html += `<div class="detail-row"><span class="detail-label">来源：</span>${escapeHtml(orig.source)}</div>`;
  }

  // Media thumbnail for nodes with file URI (documents, photos, videos, etc.)
  if (orig.uri) {
    const mediaType = getMediaType(orig.uri);
    if (mediaType === 'image') {
      html += `<div class="detail-media"><img src="file:///${escapeHtml(orig.uri.replace(/\\/g, '/'))}" alt="preview"></div>`;
    } else if (mediaType === 'video') {
      html += `<div class="detail-media"><video src="file:///${escapeHtml(orig.uri.replace(/\\/g, '/'))}" controls style="max-width:100%;border-radius:8px;"></video></div>`;
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
  // Find node in force-graph data and center on it
  const fgData = graph.graphData();
  const node = fgData.nodes.find(n => n.id === currentSelectedNode);
  if (node && node.x != null) {
    graph.centerAt(node.x, node.y, 800);
    graph.zoom(3, 800);
  }
});

// ===== Double-click to expand neighborhood =====
async function expandNode(nodeId) {
  const orig = currentData.nodes.find(n => n.id === nodeId);
  if (!orig || orig.nodeType === 'Document') return;

  const entityId = orig.id;

  try {
    const result = await window.electronAPI.exploreNode(entityId, 2, 0, 'both');
    if (!result.nodes || result.nodes.length === 0) return;

    const existingIds = new Set(currentData.nodes.map(n => n.id));
    let addedCount = 0;

    result.nodes.forEach(n => {
      const nid = n.id;
      if (!existingIds.has(nid)) {
        currentData.nodes.push({
          id: nid, label: n.label || n.name, nodeType: n.nodeType || 'Entity',
          entityType: n.entityType, description: n.description || '',
          uri: n.uri || '', source: n.source || '',
        });
        existingIds.add(nid);
        addedCount++;
      }
    });

    result.edges.forEach(edge => {
      const srcId = edge.source;
      const tgtId = edge.target;
      if (!existingIds.has(srcId) || !existingIds.has(tgtId)) return;
      const edgeExists = currentData.edges.some(e => e.source === srcId && e.target === tgtId && e.relation === edge.relation);
      if (!edgeExists) {
        currentData.edges.push({ source: srcId, target: tgtId, relation: edge.relation, confidence: edge.confidence, edgeType: 'RELATED_TO' });
      }
    });

    if (addedCount > 0) {
      // Rebuild from currentData (source of truth) to avoid mutating
      // force-graph's internal state with d3-resolved node references
      buildEdgeCountCache();
      const freshData = buildGraphData();
      graph.graphData(freshData);
      updateStats();
    }
  } catch (err) {
    console.error('Failed to expand node:', err);
  }
}

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
  _justReplacedData = true;  // Block pollChangelog BEFORE the await

  try {
    const result = await window.electronAPI.exploreNode(entity.id, 2, 0, 'both');
    if (!result.nodes || result.nodes.length === 0) {
      _justReplacedData = false;  // Reset on failure
      return;
    }

    // /api/kg/explore 已通过 _normalize_nodes/_normalize_edges 返回标准格式，直接使用
    currentData = {
      nodes: result.nodes,
      edges: result.edges || [],
    };

    // 清除旧位置缓存，确保全新布局
    _prevNodePositions = {};

    // 重置 changelog 同步时间戳，防止旧增量数据污染替换后的聚焦视图
    syncSince = new Date().toISOString();

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

// ===== Handle Window Resize =====
let resizeTimer = null;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    graph.width(container.offsetWidth);
    graph.height(container.offsetHeight);
    graph.zoomToFit(400, 40);
  }, 200);
});