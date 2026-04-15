// Node type configuration
const nodeConfigs = {
  人物: { shape: 'circle', size: 50, color: '#78b2be', stroke: '#5a8c96', label: '人物' },
  文档: { shape: 'rect', size: [60, 40], color: '#e7ca4a', stroke: '#c9af39', label: '文档' },
  照片: { shape: 'roundedRect', size: [70, 45], color: '#f8a7c8', stroke: '#f07aa8', label: '照片' },
  便签: { shape: 'polygon', size: 60, color: '#a3f0c2', stroke: '#76d8a0',
    points: [[0,0],[100,0],[100,70],[15,80],[0,70]], label: '便签' },
  链接: { shape: 'hexagon', size: 50, color: '#c4ddc8', stroke: '#a3c2a7', label: '链接' },
  组织: { shape: 'rect', size: [90, 55], color: '#9bc295', stroke: '#7da677', label: '组织' }
};

// Backend type -> Frontend visual type mapping
const typeMapping = {
  'person': '人物', 'organization': '组织',
  'location': '链接', 'event': '便签',
  'technology': '链接', 'product': '链接',
};

function mapNodeType(node) {
  if (node.nodeType === 'Document') return '文档';
  if (node.nodeType === 'Concept') return '便签';
  return typeMapping[node.entityType] || '链接';
}

const getIconForType = (type) => {
  const icons = { '人物': '👤', '文档': '📄', '照片': '📷', '便签': '📝', '链接': '🔗', '组织': '🏢' };
  return icons[type] || '📌';
};

// HTML escape for XSS prevention in detail panel
function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

// Current graph data (loaded from API)
let currentData = { nodes: [], edges: [] };

// Process nodes to add styles
const processNodes = (data) => {
  return data.nodes.map(node => {
    const visualType = mapNodeType(node);
    const config = nodeConfigs[visualType];
    const nodeConfig = {
      id: node.id,
      label: node.label || node.name || node.id,
      type: config.shape,
      size: config.size,
      style: {
        fill: config.color,
        stroke: config.stroke,
        lineWidth: 2,
        fillOpacity: 0.8
      },
      labelCfg: {
        style: {
          fontFamily: 'Ma Shan Zheng, Caveat, cursive',
          fontSize: 16,
          fill: '#2c2c2c',
          textShadow: '0.5px 0.5px 0.5px rgba(0,0,0,0.2)'
        }
      },
      _originalData: node,
      _visualType: visualType,
    };
    if (config.shape === 'polygon' && config.points) {
      nodeConfig.points = config.points;
    }
    return nodeConfig;
  });
};

// Process edges - confidence (0-1) maps to line width (1-6)
const processEdges = (data) => {
  return data.edges.map(edge => {
    const width = Math.max(1, Math.round((edge.confidence || 0.5) * 6));
    return {
      source: edge.source,
      target: edge.target,
      label: edge.relation || '',
      style: {
        stroke: '#888888',
        lineWidth: width,
        strokeOpacity: 0.7
      },
      labelCfg: {
        autoRotate: true,
        style: {
          fontFamily: 'Caveat, cursive',
          fontSize: 14,
          fill: '#555555',
          background: {
            fill: '#faf8f0',
            padding: [2, 4, 2, 4],
            radius: 4
          }
        }
      }
    };
  });
};

// Initialize G6
const container = document.getElementById('graph-container');
const width = container.offsetWidth;
const height = container.offsetHeight;

const graph = new G6.Graph({
  container: 'graph-container',
  width,
  height,
  renderer: 'canvas',
  modes: {
    default: ['drag-canvas', 'zoom-canvas', 'drag-node', 'click-select']
  },
  layout: {
    type: 'force',
    linkDistance: 120,
    center: [width / 2, height / 2],
    nodeStrength: -300,
    edgeStrength: 0.8,
    preventOverlap: true,
    nodeSize: 50
  },
  defaultNode: { type: 'circle', size: 50 },
  animate: true,
  enableOptimization: true,
  optimize: { enable: true, zoomThreshold: 0.5, showLabel: false }
});

// Loading state helpers
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

// Load graph data from backend
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

    const processed = {
      nodes: processNodes(currentData),
      edges: processEdges(currentData)
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

// Update statistics
const updateStats = () => {
  const typeCounts = {};
  currentData.nodes.forEach(node => {
    const visualType = mapNodeType(node);
    typeCounts[visualType] = (typeCounts[visualType] || 0) + 1;
  });

  const statsEl = document.getElementById('stats');
  let html = '';
  Object.entries(typeCounts).forEach(([type, count]) => {
    html += `<span class="stat-item"><strong>${count}</strong> ${type}</span>`;
  });
  html += `<span class="stat-item"><strong>${currentData.edges.length}</strong> 关系</span>`;
  statsEl.innerHTML = html;
};

// Detail panel handling
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

  const visualType = currentSelectedNode._visualType || mapNodeType(orig);
  detailTitle.textContent = `${getIconForType(visualType)} ${orig.label || orig.name || orig.id}`;

  let html = '';
  html += `<div class="detail-row"><span class="detail-label">类型：</span> ${escapeHtml(visualType)}</div>`;

  if (orig.entityType) html += `<div class="detail-row"><span class="detail-label">实体类型：</span> ${escapeHtml(orig.entityType)}</div>`;
  if (orig.description) html += `<div class="detail-row"><span class="detail-label">描述：</span> ${escapeHtml(orig.description)}</div>`;
  if (orig.source) html += `<div class="detail-row"><span class="detail-label">来源：</span> ${escapeHtml(orig.source)}</div>`;

  // Count related edges
  const relatedEdges = currentData.edges.filter(e => e.source === orig.id || e.target === orig.id);
  if (relatedEdges.length > 0) {
    html += `<div class="detail-row"><br/><strong>关系 (${relatedEdges.length})：</strong></div>`;
    html += `<div class="relation-list">`;
    relatedEdges.forEach(edge => {
      const otherId = edge.source === orig.id ? edge.target : edge.source;
      const otherNode = currentData.nodes.find(n => n.id === otherId);
      if (otherNode) {
        const otherType = mapNodeType(otherNode);
        html += `<div class="detail-row">${getIconForType(otherType)} <strong>${escapeHtml(edge.relation || edge.edgeType)}：</strong> ${escapeHtml(otherNode.label || otherNode.name)}</div>`;
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

// Node click handler
graph.on('node:click', (e) => {
  showDetail(e.item);
});

// Double-click to expand neighborhood
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
          entityType: n.type, description: n.description || ''
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

    if (addedCount > 0) updateStats();
  } catch (err) {
    console.error('Failed to expand node:', err);
  }
});

graph.on('canvas:click', () => {
  hideDetail();
});

// Search functionality
const searchInput = document.getElementById('searchInput');
searchInput.addEventListener('input', (e) => {
  const query = e.target.value.toLowerCase();
  if (!query) {
    graph.getNodes().forEach(node => graph.showItem(node.getID()));
    graph.getEdges().forEach(edge => graph.showItem(edge.getID()));
    return;
  }

  graph.getNodes().forEach(node => {
    const model = node.getModel();
    const orig = model._originalData;
    const label = (orig?.label || orig?.name || '').toLowerCase();
    const desc = (orig?.description || '').toLowerCase();
    if (label.includes(query) || desc.includes(query)) {
      graph.showItem(node.getID());
    } else {
      graph.hideItem(node.getID());
    }
  });

  graph.getEdges().forEach(edge => {
    const model = edge.getModel();
    const sourceNode = graph.findById(model.source);
    const targetNode = graph.findById(model.target);
    const sourceVisible = sourceNode && !sourceNode.getModel().hidden;
    const targetVisible = targetNode && !targetNode.getModel().hidden;
    if (sourceVisible && targetVisible) {
      graph.showItem(edge.getID());
    } else {
      graph.hideItem(edge.getID());
    }
  });
});

// Filter buttons
const filterBtns = document.querySelectorAll('.filter-btn');
filterBtns.forEach(btn => {
  btn.addEventListener('click', (e) => {
    filterBtns.forEach(b => b.classList.remove('active'));
    btn.classList.add('active');

    const filterType = btn.dataset.type;

    graph.getNodes().forEach(node => {
      const model = node.getModel();
      const visualType = model._visualType;
      if (filterType === 'all' || visualType === filterType) {
        graph.showItem(node.getID());
      } else {
        graph.hideItem(node.getID());
      }
    });

    graph.getEdges().forEach(edge => {
      const model = edge.getModel();
      const sourceNode = graph.findById(model.source);
      const targetNode = graph.findById(model.target);
      const sourceVisible = sourceNode && !sourceNode.getModel().hidden;
      const targetVisible = targetNode && !targetNode.getModel().hidden;
      if (sourceVisible && targetVisible) {
        graph.showItem(edge.getID());
      } else {
        graph.hideItem(edge.getID());
      }
    });
  });
});

// Handle window resize
window.addEventListener('resize', () => {
  const newWidth = container.offsetWidth;
  const newHeight = container.offsetHeight;
  graph.changeSize(newWidth, newHeight);
});
