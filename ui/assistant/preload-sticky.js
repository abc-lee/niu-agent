const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
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
