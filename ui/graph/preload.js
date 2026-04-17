const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  // Graph data
  getGraphSnapshot: (limit, minConfidence) =>
    ipcRenderer.invoke('kg-snapshot', limit, minConfidence),
  getGraphStats: () => ipcRenderer.invoke('kg-stats'),
  getHubEntities: (limit) => ipcRenderer.invoke('kg-hubs', limit),
  exploreNode: (entityId, depth, minConfidence, direction) =>
    ipcRenderer.invoke('kg-explore', entityId, depth, minConfidence, direction),
  findPath: (fromId, toId) => ipcRenderer.invoke('kg-find-path', fromId, toId),
  listEntities: (limit, entityType) => ipcRenderer.invoke('kg-entities', limit, entityType),
  listConcepts: (limit) => ipcRenderer.invoke('kg-concepts', limit),
  getSurprisingConnections: (minShared) => ipcRenderer.invoke('kg-surprising', minShared),
  // File operations
  openPath: (path) => ipcRenderer.invoke('open-path', path),
  showItemInFolder: (path) => ipcRenderer.invoke('show-item-in-folder', path),
});
