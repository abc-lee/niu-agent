const { contextBridge, ipcRenderer } = require('electron');

// 读取字体配置（同步，preload 在页面脚本前执行）
const { loadFontConfig } = require('./lib/font-config.js');
const _fontConfig = loadFontConfig();

contextBridge.exposeInMainWorld('electronAPI', {
  FONT_FACE_CSS: _fontConfig.fontFaceCss,  // @font-face CSS（无配置时为空串）
  FONT_FAMILY: _fontConfig.fontFamily,     // font-family 值（无配置时为仿宋兜底）
  // 鼠标进入/离开便签窗口
  stickyMouseEnter: () => ipcRenderer.send('sticky-mouse-enter'),
  stickyMouseLeave: () => ipcRenderer.send('sticky-mouse-leave'),
  
  // 隐藏便签窗口
  hideSticky: () => ipcRenderer.send('hide-sticky'),
  
  // 便签 CRUD 操作
  createNote: (note) => ipcRenderer.invoke('create-note', note),
  updateNote: (note) => ipcRenderer.invoke('update-note', note),
  deleteNote: (id) => ipcRenderer.invoke('delete-note', id),
  
  // 便签尺寸
  getStickySize: () => ipcRenderer.invoke('get-sticky-size'),
  saveStickySize: (size) => ipcRenderer.send('save-sticky-size', size)
});
