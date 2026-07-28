const { contextBridge, ipcRenderer, webUtils } = require('electron');

// 读取字体配置（同步，preload 在页面脚本前执行）
const { loadFontConfig } = require('./lib/font-config.js');
const _fontConfig = loadFontConfig();

// 同步读取睡眠触发时间配置（preload 在页面脚本前执行，零时序风险）
let _idleTimeoutMs = 5 * 60 * 1000;  // 默认 5 分钟
try {
  const fs = require('fs');
  const path = require('path');
  const userConfigPath = path.join(require('os').homedir(), '.niu', 'config', 'user-config.json');
  const raw = fs.readFileSync(userConfigPath, 'utf-8');
  const cfg = JSON.parse(raw);
  const minutes = cfg?.context?.sleepTriggerMinutes;
  if (typeof minutes === 'number' && minutes > 0) {
    _idleTimeoutMs = minutes * 60 * 1000;
  }
} catch (e) {
  // 读取失败用默认值
}

contextBridge.exposeInMainWorld('electronAPI', {
  IDLE_TIMEOUT: _idleTimeoutMs,  // 睡眠触发时间（毫秒），从 user-config.json 读取
  FONT_FACE_CSS: _fontConfig.fontFaceCss,  // @font-face CSS（无配置时为空串）
  FONT_FAMILY: _fontConfig.fontFamily,     // font-family 值（无配置时为仿宋兜底）
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

  // 获取 File 对象的真实路径（Electron 33 后 file.path 被废弃，必须用 webUtils.getPathForFile）
  getFilePath: (file) => {
    try {
      return webUtils.getPathForFile(file);
    } catch (e) {
      console.error('[preload] getFilePath failed:', e.message);
      return '';
    }
  },

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
  getPendingMessages: () => ipcRenderer.invoke('get-pending-messages'),

  // 接收入库开始通知（SSE推送，立即触发进度轮询）
  onIngestStarted: (callback) => ipcRenderer.on('ingest-started', callback),

  // 接收入库完成通知（SSE推送，触发最终进度轮询）
  onIngestCompleted: (callback) => ipcRenderer.on('ingest-completed', callback),
});
