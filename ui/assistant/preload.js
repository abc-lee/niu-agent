const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  // 移动小女孩窗口
  setPosition: (x, y) => ipcRenderer.send('set-spirit-position', { x, y }),
  
  // 调整小女孩窗口大小
  resizeWindow: (w, h) => ipcRenderer.send('resize-spirit-window', { width: w, height: h }),
  
  // 保存小女孩位置
  savePosition: () => ipcRenderer.send('save-spirit-position'),
  
  // 打开聊天窗口
  openChat: () => ipcRenderer.send('open-chat'),
  
  // 关闭所有（前端+后端）
  closeAll: () => ipcRenderer.send('close-all'),
  
  // 发送状态变化
  sendState: (state) => ipcRenderer.send('spirit-state', state),
  
  // 显示/隐藏便签窗口
  showSticky: () => ipcRenderer.send('show-sticky'),
  hideSticky: () => ipcRenderer.send('hide-sticky'),
  
  // 鼠标进入/离开小女孩
  spiritMouseEnter: () => ipcRenderer.send('spirit-mouse-enter'),
  spiritMouseLeave: () => ipcRenderer.send('spirit-mouse-leave'),
  
  // 发送消息给 Agent
  sendToAgent: (message, context) => ipcRenderer.invoke('send-to-agent', { message, context }),
  
  // 闲置整理上下文（不产生新记录）
  tidyContext: () => ipcRenderer.invoke('tidy-context'),
  
  // 获取当前聊天 session ID
  getChatSessionId: () => ipcRenderer.invoke('get-chat-session-id'),
  
  // 接收聊天关闭通知
  onChatClosed: (callback) => ipcRenderer.on('chat-closed', callback),
  
  // 接收忙碌状态通知
  onBusyState: (callback) => ipcRenderer.on('busy-state', (event, isBusy, reason) => callback(isBusy, reason)),
  
  // 接收提醒消息
  onReminder: (callback) => ipcRenderer.on('reminder', (event, message) => callback(message)),
  
  // 接收蹦高通知（有新消息但窗口不在焦点）
  onAlert: (callback) => ipcRenderer.on('alert', (event, message) => callback(message)),
  
  // 接收用户活动通知（重置空闲计时器）
  onUserActivity: (callback) => ipcRenderer.on('user-activity', callback),
  
  // 获取历史消息
  getHistory: (limit) => ipcRenderer.invoke('get-history', limit),
  
  // 获取待显示消息（打开聊天窗口时）
  getPendingMessages: () => ipcRenderer.invoke('get-pending-messages')
});
