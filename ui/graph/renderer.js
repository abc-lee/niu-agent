// ===== Type Colors =====
const typeColors = {
  person: '#4A90D9',
  organization: '#5CB85C',
  technology: '#17BECF',
  document: '#E8A838',
  photo: '#1ABC9C',
  video: '#8E44AD',
  note: '#2ECC71',
  chat: '#7F8C8D',
  concept: '#E06B9E',
  location: '#9B59B6',
  event: '#F39C12',
  other: '#95A5A6',
};

const typeLabels = {
  person: '人物', organization: '组织', technology: '技术',
  document: '文档', photo: '图片', video: '视频',
  note: '便利贴', chat: '对话', concept: '概念',
  location: '地点', event: '事件', other: '其他',
};

// ===== Node Type Mapping =====
function mapNodeType(node) {
  if (node.nodeType === 'Document') {
    // Document 节点按 source 字段细分
    const sourceMap = { photo: 'photo', video: 'video', note: 'note', chat: 'chat' };
    return sourceMap[node.source] || 'document';
  }
  if (node.nodeType === 'Concept') return 'concept';
  const entityType = node.entityType || '';
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
let currentPerspective = null; // null = default even layout

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
  const orig = node._originalData;
  if (!orig) return false;

  // Document subtypes (photo, video, note, chat, document)
  const docSubtypes = ['document', 'photo', 'video', 'note', 'chat'];
  if (docSubtypes.includes(currentPerspective)) {
    return orig.nodeType === 'Document' && mapNodeType(orig) === currentPerspective;
  }
  if (currentPerspective === 'concept') return orig.nodeType === 'Concept';
  // person, organization, technology, etc.
  return orig.entityType === currentPerspective;
}

// ===== Node Size Calculation =====
function getNodeSize(nodeId, asCore) {
  const edgeCount = countEdges(nodeId);
  const base = asCore ? 18 : 5;
  const scale = asCore ? 10 : 3;
  return base + Math.log(edgeCount + 1) * scale;
}

// ===== Process Nodes =====
const processNodes = (data) => {
  return data.nodes.map(node => {
    const visualType = mapNodeType(node);
    const color = typeColors[visualType] || typeColors.other;
    const asCore = currentPerspective ? isCoreNode(node) : false;
    const size = getNodeSize(node.id, asCore);

    return {
      id: node.id,
      label: '', // No text labels on nodes
      type: 'circle',
      size: size,
      style: {
        fill: color,
        stroke: color,
        lineWidth: 1,
        fillOpacity: asCore ? 0.9 : 0.7,
      },
      stateStyles: {
        selected: {
          fill: color,
          stroke: color,
          lineWidth: 3,
          fillOpacity: 1,
          shadowColor: color,
          shadowBlur: 15,
        },
      },
      _originalData: node,
      _visualType: visualType,
    };
  });
};

// ===== Process Edges — thin lines, no text =====
const processEdges = (data) => {
  return data.edges.map(edge => {
    return {
      source: edge.source,
      target: edge.target,
      label: '',
      style: {
        stroke: 'rgba(0,0,0,0.12)',
        lineWidth: 1,
        strokeOpacity: 1,
      },
    };
  });
};

// ===== Layout Config =====
function getLayoutConfig() {
  const container = document.getElementById('graph-container');
  const w = container.offsetWidth;
  const h = container.offsetHeight;

  if (!currentPerspective) {
    return {
      type: 'force',
      linkDistance: 80,
      center: [w / 2, h / 2],
      nodeStrength: -200,
      edgeStrength: 0.1,
      preventOverlap: true,
      nodeSize: 15,
      collideStrength: 0.8,
    };
  }

  return {
    type: 'force',
    linkDistance: 100,
    center: [w / 2, h / 2],
    nodeStrength: (d) => {
      return isCoreNode(d) ? -500 : -30;
    },
    edgeStrength: 0.1,
    preventOverlap: true,
    nodeSize: 15,
    collideStrength: 0.8,
  };
}

// ===== Initialize G6 =====
const container = document.getElementById('graph-container');
const width = container.offsetWidth;
const height = container.offsetHeight;

const graph = new G6.Graph({
  container: 'graph-container',
  width,
  height,
  renderer: 'canvas',
  modes: {
    default: ['drag-canvas', 'zoom-canvas', 'drag-node', 'click-select'],
  },
  layout: getLayoutConfig(),
  defaultNode: {
    type: 'circle',
    size: 12,
    style: { fill: '#95A5A6', stroke: '#95A5A6', lineWidth: 1, fillOpacity: 0.7 },
  },
  defaultEdge: {
    style: { stroke: 'rgba(0,0,0,0.12)', lineWidth: 1 },
  },
  animate: true,
  enableOptimization: true,
  optimize: { enable: true, zoomThreshold: 0.5, showLabel: false },
});

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

    const processed = {
      nodes: processNodes(currentData),
      edges: processEdges(currentData),
    };

    graph.data(processed);
    graph.render();
    updateStats();
  } catch (error) {
    console.error('Failed to load graph:', error);
    hideLoading();
    showEmpty();
    return;
  }

  hideLoading();
}

loadGraphSnapshot();

// ===== Update Stats =====
const updateStats = () => {
  const statsEl = document.getElementById('stats');
  statsEl.innerHTML = `<span class="stat-item"><strong>${currentData.nodes.length}</strong> 节点</span><span class="stat-item"><strong>${currentData.edges.length}</strong> 关系</span>`;
};

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
  // Toggle: clicking active perspective deactivates it
  currentPerspective = (currentPerspective === coreType) ? null : coreType;
  updatePerspectiveButtons();
  reprocessAndRelayout();
}

perspBtns.forEach(btn => {
  btn.addEventListener('click', () => {
    switchPerspective(btn.dataset.core);
  });
});

function reprocessAndRelayout() {
  // Update node sizes and styles based on new perspective
  graph.getNodes().forEach(nodeItem => {
    const model = nodeItem.getModel();
    const orig = model._originalData;
    if (!orig) return;

    const asCore = currentPerspective ? isCoreNode(model) : false;
    const size = getNodeSize(model.id, asCore);
    const color = getNodeColor(orig);

    graph.updateItem(nodeItem, {
      size: size,
      style: {
        fill: color,
        stroke: color,
        fillOpacity: asCore ? 0.9 : 0.7,
      },
    });
  });

  // Re-layout with animation
  graph.updateLayout(getLayoutConfig());
}

// ===== Tooltip =====
const tooltip = document.getElementById('node-tooltip');

graph.on('node:mouseenter', (e) => {
  const model = e.item.getModel();
  const orig = model._originalData;
  if (!orig) return;

  const name = orig.label || orig.name || orig.id;
  const typeLabel = getNodeLabel(orig);
  tooltip.innerHTML = `<div>${escapeHtml(name)}</div><div class="tooltip-type">${escapeHtml(typeLabel)}</div>`;
  tooltip.classList.remove('hidden');

  // Position near mouse — use graph point converted to canvas
  const canvasX = graph.getCanvasByPoint(e.x, e.y);
  const containerRect = container.getBoundingClientRect();
  tooltip.style.left = (containerRect.left + canvasX.x + 15) + 'px';
  tooltip.style.top = (containerRect.top + canvasX.y - 10) + 'px';
});

graph.on('node:mouseleave', () => {
  tooltip.classList.add('hidden');
});

graph.on('node:drag', () => {
  tooltip.classList.add('hidden');
});

// ===== Detail Panel =====
let currentSelectedNode = null;
const detailPanel = document.getElementById('detail-panel');
const detailTitle = document.getElementById('detail-title');
const detailContent = document.getElementById('detail-content');
const closeDetail = document.getElementById('close-detail');
const focusNodeBtn = document.getElementById('focus-node');

const showDetail = (node) => {
  currentSelectedNode = node.getModel();
  const orig = currentSelectedNode._originalData;
  if (!orig) return;

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

  // Media thumbnail for documents
  if (orig.nodeType === 'Document' && orig.uri) {
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
        html += `<div class="detail-row"><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${otherColor};margin-right:6px;vertical-align:middle;"></span>`;
        if (relLabel) html += `<strong>${escapeHtml(relLabel)}：</strong>`;
        html += `${escapeHtml(otherName)}</div>`;
      }
    });
    html += `</div>`;
  }

  detailContent.innerHTML = html;
  detailPanel.classList.remove('hidden');
};

const hideDetail = () => {
  detailPanel.classList.add('hidden');
  currentSelectedNode = null;
};

closeDetail.addEventListener('click', hideDetail);

focusNodeBtn.addEventListener('click', () => {
  if (!currentSelectedNode) return;
  graph.focusItem(currentSelectedNode.id);
});

// ===== Node Click =====
graph.on('node:click', (e) => {
  showDetail(e.item);
});

// ===== Double-click to expand neighborhood =====
graph.on('node:dblclick', async (e) => {
  const node = e.item.getModel();
  const orig = node._originalData;
  if (!orig || orig.nodeType !== 'Entity') return;

  const entityId = orig.id.replace(/^entity:/, '');

  try {
    const result = await window.electronAPI.exploreNode(entityId, 2, 0, 'both');
    if (!result.nodes || result.nodes.length === 0) return;

    const existingIds = new Set(currentData.nodes.map(n => n.id));
    let addedCount = 0;

    result.nodes.forEach(n => {
      const nodeId = n.id.startsWith('entity:') ? n.id : `entity:${n.id}`;
      if (!existingIds.has(nodeId)) {
        const newNode = {
          id: nodeId, label: n.name, nodeType: 'Entity',
          entityType: n.type, description: n.description || '',
        };
        currentData.nodes.push(newNode);
        const processed = processNodes({ nodes: [newNode], edges: [] });
        graph.addItem('node', processed[0]);
        existingIds.add(nodeId);
        addedCount++;
      }
    });

    result.edges.forEach(edge => {
      const srcId = edge.source.startsWith('entity:') ? edge.source : `entity:${edge.source}`;
      const tgtId = edge.target.startsWith('entity:') ? edge.target : `entity:${edge.target}`;
      if (!existingIds.has(srcId) || !existingIds.has(tgtId)) return;
      const edgeExists = currentData.edges.some(e => e.source === srcId && e.target === tgtId && e.relation === edge.relation);
      if (!edgeExists) {
        const newEdge = { source: srcId, target: tgtId, relation: edge.relation, confidence: edge.confidence, edgeType: 'RELATED_TO' };
        currentData.edges.push(newEdge);
        const processed = processEdges({ nodes: [], edges: [newEdge] });
        graph.addItem('edge', processed[0]);
      }
    });

    if (addedCount > 0) {
      buildEdgeCountCache();
      updateStats();
    }
  } catch (err) {
    console.error('Failed to expand node:', err);
  }
});

graph.on('canvas:click', () => {
  hideDetail();
});

// ===== Search — re-layout with matches as centers =====
const searchInput = document.getElementById('searchInput');
let searchActive = false;

searchInput.addEventListener('input', (e) => {
  const query = e.target.value.toLowerCase().trim();

  if (!query) {
    searchActive = false;
    // Restore node sizes to current perspective
    reprocessAndRelayout();
    return;
  }

  searchActive = true;
  const container = document.getElementById('graph-container');
  const centerX = container.offsetWidth / 2;
  const centerY = container.offsetHeight / 2;

  // Find matching nodes
  const matchIds = new Set();
  currentData.nodes.forEach(node => {
    const label = (node.label || node.name || '').toLowerCase();
    const desc = (node.description || '').toLowerCase();
    if (label.includes(query) || desc.includes(query)) {
      matchIds.add(node.id);
    }
  });

  // Update all nodes: enlarge matches, shrink others
  graph.getNodes().forEach(nodeItem => {
    const model = nodeItem.getModel();
    const isMatch = matchIds.has(model.id);
    const orig = model._originalData;
    const color = orig ? getNodeColor(orig) : typeColors.other;

    let size;
    if (isMatch) {
      size = getNodeSize(model.id, true) * 1.3;
    } else {
      size = getNodeSize(model.id, false);
    }

    graph.updateItem(nodeItem, {
      size: size,
      style: {
        fill: color,
        stroke: color,
        fillOpacity: isMatch ? 1 : 0.4,
      },
    });
  });

  // Fade non-matching edges
  graph.getEdges().forEach(edgeItem => {
    const model = edgeItem.getModel();
    const sourceMatch = matchIds.has(model.source);
    const targetMatch = matchIds.has(model.target);
    const connected = sourceMatch || targetMatch;

    graph.updateItem(edgeItem, {
      style: {
        stroke: connected ? 'rgba(0,0,0,0.15)' : 'rgba(0,0,0,0.04)',
        lineWidth: connected ? 1 : 0.5,
      },
    });
  });

  // Re-layout with matches pulled to center
  graph.updateLayout({
    type: 'force',
    linkDistance: 80,
    center: [centerX, centerY],
    nodeStrength: (d) => matchIds.has(d.id) ? -400 : -50,
    edgeStrength: 0.1,
    preventOverlap: true,
    nodeSize: 15,
    collideStrength: 0.8,
  });
});

// ===== Handle Window Resize =====
window.addEventListener('resize', () => {
  const newWidth = container.offsetWidth;
  const newHeight = container.offsetHeight;
  graph.changeSize(newWidth, newHeight);
});
