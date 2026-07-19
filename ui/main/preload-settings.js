const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  getPresets: () => ipcRenderer.invoke('get-presets'),
  getConfig: () => ipcRenderer.invoke('get-config'),
  saveConfig: (config) => ipcRenderer.invoke('save-config', config),
  testConnection: (params) => ipcRenderer.invoke('test-connection', params),
  probeResponseFormat: (config) => ipcRenderer.invoke('probe-response-format', config),
  closeWindow: () => ipcRenderer.invoke('close-window'),
  minimizeWindow: () => ipcRenderer.invoke('minimize-window')
});
