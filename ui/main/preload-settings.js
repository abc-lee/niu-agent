const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  getNamedConfigs: () => ipcRenderer.invoke('get-named-configs'),
  getConfig: () => ipcRenderer.invoke('get-config'),
  saveConfig: (payload) => ipcRenderer.invoke('save-config', payload),
  testConnection: (params) => ipcRenderer.invoke('test-connection', params),
  probeResponseFormat: (config) => ipcRenderer.invoke('probe-response-format', config),
  getCapabilityProfile: (params) => ipcRenderer.invoke('get-capability-profile', params),
  probeCapability: (config) => ipcRenderer.invoke('probe-capability', config),
  listModels: (config) => ipcRenderer.invoke('list-models', config),
  closeWindow: () => ipcRenderer.invoke('close-window'),
  minimizeWindow: () => ipcRenderer.invoke('minimize-window')
});
