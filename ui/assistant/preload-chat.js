const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  // 移动聊天窗口
  setPosition: (x, y) => ipcRenderer.send('set-chat-position', { x, y }),
  
  // 调整聊天窗口大小
  resizeWindow: (width, height) => ipcRenderer.send('resize-chat-window', { width, height }),
  
  // 关闭聊天窗口
  close: () => ipcRenderer.send('close-chat'),
  
  // 发送消息到后端
  sendMessage: (message) => ipcRenderer.invoke('send-message', message),
  
  // 获取统计数据
  getStats: () => ipcRenderer.invoke('get-stats'),
  
  // 打开图谱
  openGraph: () => ipcRenderer.send('open-graph'),
  
  // 接收小女孩状态
  onSpiritState: (callback) => ipcRenderer.on('spirit-state', (event, state) => callback(state)),
  
  // 通知小女孩进入/退出忙碌状态
  notifyBusy: (isBusy, reason) => ipcRenderer.send('notify-busy', { isBusy, reason }),
  
  // 通知小女孩用户正在活动（重置空闲计时器）
  notifyActivity: () => ipcRenderer.send('notify-activity'),
  
  // 获取当前 session ID
  getSessionId: () => ipcRenderer.invoke('get-chat-session-id'),
  
  // 获取历史消息
  getHistory: (limit, beforeId) => ipcRenderer.invoke('get-history', limit, beforeId),
  
  // 获取待显示消息（窗口获得焦点时）
  getPendingMessages: () => ipcRenderer.invoke('get-pending-messages'),
  
  // 获取图片显示 URL（本地路径转 file:// URL）
  getImageUrl: (filePath) => ipcRenderer.invoke('get-image-url', filePath),
  
  // 处理拖入的图片/文件（调用 Agent）
  processImage: (filePath) => ipcRenderer.invoke('process-image', filePath),
  
  // 用系统默认查看器打开文件
  openWithSystemViewer: (filePath) => ipcRenderer.send('open-with-system-viewer', filePath),
  
  // 用系统默认浏览器打开链接
  openExternal: (url) => ipcRenderer.send('open-external', url),

  // 清空聊天记录
  clearChat: () => ipcRenderer.invoke('clear-chat'),

  // 接收提醒通知（scheduler 触发的定时任务）
  onAlert: (callback) => ipcRenderer.on('alert', (event, message) => callback(message)),

  // 接收新消息通知（SSE 推送，前端从数据库读取）
  onNewMessage: (callback) => ipcRenderer.on('new-message', (_event, data) => callback(data)),

  // 接收工具调用状态通知（SSE 推送，实时显示 Agent 正在做什么）
  onToolStatus: (callback) => ipcRenderer.on('tool-status', (event, data) => callback(data)),

  // 接收上下文压缩状态通知（SSE 推送，显示压缩进度/模式）
  onCompactStatus: (callback) => ipcRenderer.on('compact-status', (_event, data) => callback(data)),

  // 接收入库开始通知（SSE推送）
  onIngestStarted: (callback) => ipcRenderer.on('ingest-started', callback),

  // 接收入库完成通知（SSE推送）
  onIngestCompleted: (callback) => ipcRenderer.on('ingest-completed', callback),

  // 获取当前 Agent 是否忙碌（窗口恢复时同步停止按钮状态）
  getChatStatus: () => ipcRenderer.invoke('get-chat-status'),

  // 窗口显示/获得焦点时通知前端同步状态
  onSyncState: (callback) => ipcRenderer.on('sync-state', () => callback()),

});
