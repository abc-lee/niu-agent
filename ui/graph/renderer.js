// Node type configuration
const nodeConfigs = {
  人物: {
    shape: 'circle',
    size: 50,
    color: '#78b2be',
    stroke: '#5a8c96',
    label: '人物'
  },
  文档: {
    shape: 'rect',
    size: [60, 40],
    color: '#e7ca4a',
    stroke: '#c9af39',
    label: '文档'
  },
  照片: {
    shape: 'roundedRect',
    size: [70, 45],
    color: '#f8a7c8',
    stroke: '#f07aa8',
    label: '照片'
  },
  便签: {
    shape: 'polygon',
    size: 60,
    color: '#a3f0c2',
    stroke: '#76d8a0',
    points: [
      [0, 0],
      [100, 0],
      [100, 70],
      [15, 80],
      [0, 70]
    ],
    label: '便签'
  },
  链接: {
    shape: 'hexagon',
    size: 50,
    color: '#c4ddc8',
    stroke: '#a3c2a7',
    label: '链接'
  },
  组织: {
    shape: 'rect',
    size: [90, 55],
    color: '#9bc295',
    stroke: '#7da677',
    label: '组织'
  }
};

// Relationship strength to line thickness
const getEdgeWidth = (strength) => {
  if (strength >= 5) return 8;
  if (strength >= 3) return 5;
  if (strength >= 1) return 3;
  return 1;
};

// Get icon for node type
const getIconForType = (type) => {
  const icons = {
    '人物': '👤',
    '文档': '📄',
    '照片': '📷',
    '便签': '📝',
    '链接': '🔗',
    '组织': '🏢'
  };
  return icons[type] || '📌';
};

// Sample data
const sampleData = {
  nodes: [
    { id: '1', label: '张三', type: '人物', title: '张三', info: { 职务: '产品经理', 组织: 'ABC公司' } },
    { id: '2', label: '王五', type: '人物', title: '王五', info: { 职务: '部门总监', 组织: 'ABC公司' } },
    { id: '3', label: '李四', type: '人物', title: '李四', info: { 职务: '工程师', 组织: 'ABC公司' } },
    { id: '4', label: '小明', type: '人物', title: '小明', info: { 职务: '设计师', 组织: 'ABC公司' } },
    { id: '5', label: '小红', type: '人物', title: '小红', info: { 职务: '开发', 组织: 'ABC公司' } },
    { id: '6', label: '需求文档', type: '文档', title: '需求文档', info: { 创建时间: '2024-01-15' } },
    { id: '7', label: '设计稿', type: '文档', title: '设计稿', info: { 创建时间: '2024-01-20' } },
    { id: '8', label: '年会合影', type: '照片', title: '年会合影', info: { 日期: '2023-12-25' } },
    { id: '9', label: '团建照片', type: '照片', title: '团建照片', info: { 日期: '2024-02-10' } },
    { id: '10', label: '待办', type: '便签', title: '待办', info: { 内容: 'Q2 roadmap 评审' } },
    { id: '11', label: '笔记', type: '便签', title: '笔记', info: { 内容: '客户访谈记录' } },
    { id: '12', label: '公司官网', type: '链接', title: '公司官网', info: { url: 'https://abc.com' } },
    { id: '13', label: 'ABC公司', type: '组织', title: 'ABC公司', info: { 地址: '北京市朝阳区', 规模: '50-100人' } },
    { id: '14', label: '市场部', type: '组织', title: '市场部', info: { 负责人: '赵六', 人数: '8人' } },
    { id: '15', label: '技术部', type: '组织', title: '技术部', info: { 负责人: '王五', 人数: '15人' } },
    { id: '16', label: '产品部', type: '组织', title: '产品部', info: { 负责人: '张三', 人数: '5人' } }
  ],
  edges: [
    { source: '1', target: '2', strength: 3, label: '上级' },
    { source: '1', target: '4', strength: 6, label: '下级' },
    { source: '1', target: '5', strength: 5, label: '下级' },
    { source: '3', target: '1', strength: 2, label: '同事' },
    { source: '3', target: '2', strength: 1, label: '汇报' },
    { source: '1', target: '6', strength: 4, label: '撰写' },
    { source: '4', target: '7', strength: 5, label: '设计' },
    { source: '1', target: '8', strength: 2, label: '出现' },
    { source: '2', target: '8', strength: 1, label: '出现' },
    { source: '3', target: '8', strength: 1, label: '出现' },
    { source: '1', target: '9', strength: 1, label: '出现' },
    { source: '3', target: '9', strength: 1, label: '出现' },
    { source: '4', target: '9', strength: 1, label: '出现' },
    { source: '5', target: '9', strength: 1, label: '出现' },
    { source: '1', target: '10', strength: 3, label: '创建' },
    { source: '5', target: '11', strength: 2, label: '记录' },
    { source: '13', target: '12', strength: 1, label: '官网' },
    { source: '1', target: '13', strength: 5, label: '成员' },
    { source: '2', target: '13', strength: 6, label: '成员' },
    { source: '3', target: '13', strength: 4, label: '成员' },
    { source: '4', target: '13', strength: 3, label: '成员' },
    { source: '5', target: '13', strength: 3, label: '成员' },
    { source: '13', target: '14', strength: 4, label: '部门' },
    { source: '13', target: '15', strength: 6, label: '部门' },
    { source: '13', target: '16', strength: 3, label: '部门' },
    { source: '15', target: '2', strength: 5, label: '管理' },
    { source: '16', target: '1', strength: 4, label: '管理' }
  ]
};

// Process nodes to add styles
const processNodes = (data) => {
  return data.nodes.map(node => {
    const config = nodeConfigs[node.type];
    const nodeConfig = {
      id: node.id,
      label: node.label,
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
      }
    };
    if (config.shape === 'polygon' && config.points) {
      nodeConfig.points = config.points;
    }
    return nodeConfig;
  });
};

// Process edges to add styles based on strength
const processEdges = (data) => {
  return data.edges.map(edge => {
    const width = getEdgeWidth(edge.strength);
    return {
      source: edge.source,
      target: edge.target,
      label: edge.label || '',
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

// Register polygon for sticky note - G6 already has polygon built-in
const graph = new G6.Graph({
  container: 'graph-container',
  width,
  height,
  renderer: 'canvas',
  modes: {
    default: [
      'drag-canvas',
      'zoom-canvas',
      'drag-node',
      'click-select'
    ]
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
  defaultNode: {
    type: 'circle',
    size: 50
  },
  animate: true,
  enableOptimization: true,
  optimize: {
    enable: true,
    zoomThreshold: 0.5,
    showLabel: false
  }
});

// Process and load data
const processedData = {
  nodes: processNodes(sampleData),
  edges: processEdges(sampleData)
};

graph.data(processedData);
graph.render();

// Update statistics
const updateStats = () => {
  const typeCounts = sampleData.nodes.reduce((acc, node) => {
    acc[node.type] = (acc[node.type] || 0) + 1;
    return acc;
  }, {});
  
  const statsEl = document.getElementById('stats');
  let html = '';
  Object.entries(typeCounts).forEach(([type, count]) => {
    html += `<span class="stat-item"><strong>${count}</strong> ${type}</span>`;
  });
  html += `<span class="stat-item"><strong>${sampleData.edges.length}</strong> 关系</span>`;
  statsEl.innerHTML = html;
};
updateStats();

// Detail panel handling
let currentSelectedNode = null;
const detailPanel = document.getElementById('detail-panel');
const detailTitle = document.getElementById('detail-title');
const detailContent = document.getElementById('detail-content');
const closeDetail = document.getElementById('close-detail');
const focusNodeBtn = document.getElementById('focus-node');

const showDetail = (node) => {
  currentSelectedNode = node.getModel();
  const originalNode = sampleData.nodes.find(n => n.id === currentSelectedNode.id);
  
  detailTitle.textContent = `${getIconForType(originalNode.type)} ${originalNode.title}`;
  
  let html = '';
  html += `<div class="detail-row"><span class="detail-label">类型：</span> ${originalNode.type}</div>`;
  
  if (originalNode.info) {
    Object.entries(originalNode.info).forEach(([key, value]) => {
      html += `<div class="detail-row"><span class="detail-label">${key}：</span> ${value}</div>`;
    });
  }
  
  // Count media
  const relatedEdges = sampleData.edges.filter(e => 
    e.source === originalNode.id || e.target === originalNode.id
  );
  const photoCount = relatedEdges.filter(e => {
    const otherId = e.source === originalNode.id ? e.target : e.source;
    const otherNode = sampleData.nodes.find(n => n.id === otherId);
    return otherNode && otherNode.type === '照片';
  }).length;
  const docCount = relatedEdges.filter(e => {
    const otherId = e.source === originalNode.id ? e.target : e.source;
    const otherNode = sampleData.nodes.find(n => n.id === otherId);
    return otherNode && otherNode.type === '文档';
  }).length;
  
  if (photoCount > 0 || docCount > 0) {
    html += `<div class="detail-row">`;
    const parts = [];
    if (photoCount > 0) parts.push(`${photoCount} 张照片`);
    if (docCount > 0) parts.push(`${docCount} 份文档`);
    html += `出现：${parts.join('、')}`;
    html += `</div>`;
  }
  
  // Relationships
  if (relatedEdges.length > 0) {
    html += `<div class="detail-row"><br/><strong>关系：</strong></div>`;
    html += `<div class="relation-list">`;
    relatedEdges.forEach(edge => {
      const otherId = edge.source === originalNode.id ? edge.target : edge.source;
      const otherNode = sampleData.nodes.find(n => n.id === otherId);
      if (otherNode) {
        html += `<div class="detail-row">${getIconForType(otherNode.type)} <strong>${edge.label}：</strong> ${otherNode.title}</div>`;
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
  const node = e.item;
  showDetail(node);
});

graph.on('canvas:click', () => {
  hideDetail();
});

// Search functionality
const searchInput = document.getElementById('searchInput');
searchInput.addEventListener('input', (e) => {
  const query = e.target.value.toLowerCase();
  if (!query) {
    // Show all nodes
    processedData.nodes.forEach(node => {
      graph.showItem(node.id);
    });
    processedData.edges.forEach(edge => {
      graph.showItem(edge.source + '-' + edge.target);
    });
    return;
  }
  
  // Search and filter
  processedData.nodes.forEach(node => {
    const originalNode = sampleData.nodes.find(n => n.id === node.id);
    const label = originalNode.label.toLowerCase();
    const title = originalNode.title.toLowerCase();
    if (label.includes(query) || title.includes(query)) {
      graph.showItem(node.id);
    } else {
      graph.hideItem(node.id);
    }
  });
  
  // Only show edges connected to visible nodes
  processedData.edges.forEach(edge => {
    const sourceVisible = !graph.findById(edge.source).getModel().hidden;
    const targetVisible = !graph.findById(edge.target).getModel().hidden;
    if (sourceVisible && targetVisible) {
      graph.showItem(edge.source + '-' + edge.target);
    } else {
      graph.hideItem(edge.source + '-' + edge.target);
    }
  });
});

// Filter buttons
const filterBtns = document.querySelectorAll('.filter-btn');
filterBtns.forEach(btn => {
  btn.addEventListener('click', (e) => {
    // Remove active class from all
    filterBtns.forEach(b => b.classList.remove('active'));
    // Add active to clicked
    btn.classList.add('active');
    
    const filterType = btn.dataset.type;
    
    // Filter by type
    processedData.nodes.forEach(node => {
      const originalNode = sampleData.nodes.find(n => n.id === node.id);
      if (filterType === 'all' || originalNode.type === filterType) {
        graph.showItem(node.id);
      } else {
        graph.hideItem(node.id);
      }
    });
    
    // Only show edges connected to visible nodes
    processedData.edges.forEach(edge => {
      const sourceVisible = !graph.findById(edge.source).getModel().hidden;
      const targetVisible = !graph.findById(edge.target).getModel().hidden;
      if (sourceVisible && targetVisible) {
        graph.showItem(edge.source + '-' + edge.target);
      } else {
        graph.hideItem(edge.source + '-' + edge.target);
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
