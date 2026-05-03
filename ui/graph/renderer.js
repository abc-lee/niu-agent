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
  return div.innerHTML;
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

// ===== Current Graph Data =====
let currentData = { nodes: [], edges: [] };
let currentPerspective = null;
let currentMatchIds = null; // null = no search active

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
function buildGraphData() {
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

    return {
      id: node.id,
      label: node.label || node.name || node.id,
      color: color,
      val: val,
      opacity: opacity,
      _originalData: node,
      _visualType: visualType,
    };
  });

  const links = currentData.edges.map(edge => {
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
  .d3AlphaDecay(0.02)
  .d3VelocityDecay(0.3)
  .cooldownTime(3000)
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
  if (chargeForce) chargeForce.strength(-2.5);
  const linkForce = graph.d3Force('link');
  if (linkForce) linkForce.distance(30).strength(0.8);
}

// ===== Load Graph Data =====
async function loadGraphSnapshot() {
  showLoading();
  hideEmpty();

  try {
    const snapshot = await window.electronAPI.getGraphSnapshot(200, 0);
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

    // 初始加载：模拟稳定后 zoomToFit
    graph.onEngineStop(() => {
      graph.zoomToFit(400, 40);
    });
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

async function pollChangelog() {
  try {
    const result = await window.electronAPI.kgChangelog(syncSince);
    const changes = result.changes || [];
    if (changes.length === 0) return;

    // Track latest timestamp for next poll
    const latestTs = changes.reduce((max, c) => {
      return (!max || c.timestamp > max) ? c.timestamp : max;
    }, null);
    if (latestTs) syncSince = latestTs;

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
      }
    });

    if (changed) {
      // Incremental update: keep existing node/link references, only append new ones
      // This preserves positions of existing nodes — force-graph matches by id
      const fgData = graph.graphData();
      const fgNodeIds = new Set(fgData.nodes.map(n => n.id));

      // Add new nodes to currentData and force-graph
      currentData.nodes.forEach(node => {
        if (!fgNodeIds.has(node.id)) {
          const visualType = mapNodeType(node);
          const color = typeColors[visualType] || typeColors.other;
          const edgeCount = countEdges(node.id);
          const size = 2 + Math.log(edgeCount + 1) * 1.5;
          fgData.nodes.push({
            id: node.id,
            label: node.label || node.name || node.id,
            color: color,
            val: size,
            opacity: 0.75,
            _originalData: node,
            _visualType: visualType,
          });
        }
      });

      // Add new links
      const fgLinkKeys = new Set(fgData.links.map(l => `${l.source.id || l.source}->${l.target.id || l.target}`));
      currentData.edges.forEach(edge => {
        const key = `${edge.source}->${edge.target}`;
        if (!fgLinkKeys.has(key)) {
          fgData.links.push({
            source: edge.source,
            target: edge.target,
            relation: edge.relation || '',
            color: 'rgba(0,0,0,0.12)',
            width: 1,
          });
        }
      });

      buildEdgeCountCache();
      graph.graphData(fgData); // force-graph preserves positions of existing nodes
      updateStats();
    }
  } catch (err) {
    // Silently ignore — next poll will retry
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
function reLayout() {
  buildEdgeCountCache();
  applyForceConfig(); // Apply forces BEFORE graphData so simulation uses correct parameters
  const data = buildGraphData();
  graph.graphData(data);
  // Wait for simulation to settle before zooming to fit
  graph.onEngineStop(() => {
    graph.zoomToFit(400, 40);
  });
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

  // Enable/disable file buttons based on whether node has a file URI
  const hasFile = !!(orig.uri);
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
  if (orig && orig.uri) {
    window.electronAPI.openPath(orig.uri);
  }
});

openFolderBtn.addEventListener('click', () => {
  if (!currentSelectedNode) return;
  const orig = currentData.nodes.find(n => n.id === currentSelectedNode);
  if (orig && orig.uri) {
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
  if (!orig || orig.nodeType !== 'Entity') return;

  const entityId = orig.id.replace(/^entity:/, '');

  try {
    const result = await window.electronAPI.exploreNode(entityId, 2, 0, 'both');
    if (!result.nodes || result.nodes.length === 0) return;

    const existingIds = new Set(currentData.nodes.map(n => n.id));
    let addedCount = 0;

    result.nodes.forEach(n => {
      const nid = n.id.startsWith('entity:') ? n.id : `entity:${n.id}`;
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
      const srcId = edge.source.startsWith('entity:') ? edge.source : `entity:${edge.source}`;
      const tgtId = edge.target.startsWith('entity:') ? edge.target : `entity:${edge.target}`;
      if (!existingIds.has(srcId) || !existingIds.has(tgtId)) return;
      const edgeExists = currentData.edges.some(e => e.source === srcId && e.target === tgtId && e.relation === edge.relation);
      if (!edgeExists) {
        currentData.edges.push({ source: srcId, target: tgtId, relation: edge.relation, confidence: edge.confidence, edgeType: 'RELATED_TO' });
      }
    });

    if (addedCount > 0) {
      // Incremental: append new nodes/links to force-graph data, preserve positions
      const fgData = graph.graphData();
      const fgNodeIds = new Set(fgData.nodes.map(n => n.id));

      result.nodes.forEach(n => {
        const nid = n.id.startsWith('entity:') ? n.id : `entity:${n.id}`;
        if (!fgNodeIds.has(nid)) {
          const visualType = mapNodeType({ nodeType: n.nodeType || 'Entity', entityType: n.entityType });
          const color = typeColors[visualType] || typeColors.other;
          fgData.nodes.push({
            id: nid,
            label: n.label || n.name || nid,
            color: color,
            val: 2,
            opacity: 0.75,
            _originalData: { id: nid, label: n.label || n.name, nodeType: n.nodeType || 'Entity', entityType: n.entityType, description: n.description || '', uri: n.uri || '', source: n.source || '' },
            _visualType: visualType,
          });
        }
      });

      const fgLinkKeys = new Set(fgData.links.map(l => `${l.source.id || l.source}->${l.target.id || l.target}`));
      result.edges.forEach(edge => {
        const srcId = edge.source.startsWith('entity:') ? edge.source : `entity:${edge.source}`;
        const tgtId = edge.target.startsWith('entity:') ? edge.target : `entity:${edge.target}`;
        const key = `${srcId}->${tgtId}`;
        if (!fgLinkKeys.has(key) && fgNodeIds.has(srcId) && fgNodeIds.has(tgtId)) {
          fgData.links.push({
            source: srcId,
            target: tgtId,
            relation: edge.relation || '',
            color: 'rgba(0,0,0,0.12)',
            width: 1,
          });
        }
      });

      buildEdgeCountCache();
      graph.graphData(fgData);
      updateStats();
    }
  } catch (err) {
    console.error('Failed to expand node:', err);
  }
}

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