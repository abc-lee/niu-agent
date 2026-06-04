const { app, BrowserWindow, ipcMain, shell } = require('electron');
const path = require('path');
const fs = require('fs');
const http = require('http');

const API_HOST = '127.0.0.1';
const API_PORT = 9876;

function apiRequest(method, apiPath, body = null) {
  return new Promise((resolve, reject) => {
    const options = {
      hostname: API_HOST,
      port: API_PORT,
      path: apiPath,
      method: method,
      headers: { 'Content-Type': 'application/json' },
      timeout: 30000,
    };
    const req = http.request(options, (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => {
        try { resolve(JSON.parse(data)); }
        catch (e) { reject(new Error(`Parse error: ${e.message}`)); }
      });
    });
    req.on('error', reject);
    req.on('timeout', () => { req.destroy(); reject(new Error('Request timeout')); });
    if (body) req.write(JSON.stringify(body));
    req.end();
  });
}

function createWindow() {
  const mainWindow = new BrowserWindow({
    width: 1280,
    height: 800,
    minWidth: 800,
    minHeight: 600,
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  mainWindow.loadFile('index.html');
}

// ========== IPC Handlers ==========

ipcMain.handle('kg-snapshot', async (event, limit, minConfidence) => {
  const params = new URLSearchParams({ limit: limit || 2000, min_confidence: minConfidence || 0 });
  return apiRequest('GET', `/api/kg/snapshot?${params}`);
});

ipcMain.handle('kg-stats', async () => {
  return apiRequest('GET', '/api/kg/stats');
});

ipcMain.handle('kg-hubs', async (event, limit) => {
  return apiRequest('GET', `/api/kg/hubs?limit=${limit || 20}`);
});

ipcMain.handle('kg-explore', async (event, entityId, depth, minConfidence, direction) => {
  return apiRequest('POST', '/api/kg/explore', {
    entity_id: entityId, depth: depth || 2,
    min_confidence: minConfidence || 0, direction: direction || 'both',
  });
});

ipcMain.handle('kg-find-path', async (event, fromId, toId) => {
  return apiRequest('POST', '/api/kg/find-path', { from_id: fromId, to_id: toId });
});

ipcMain.handle('kg-entities', async (event, limit, entityType) => {
  const params = new URLSearchParams({ limit: limit || 100 });
  if (entityType) params.set('entity_type', entityType);
  return apiRequest('GET', `/api/kg/entities?${params}`);
});

ipcMain.handle('kg-concepts', async (event, limit) => {
  return apiRequest('GET', `/api/kg/concepts?limit=${limit || 100}`);
});

ipcMain.handle('kg-surprising', async (event, minShared) => {
  return apiRequest('GET', `/api/kg/surprising?min_shared=${minShared || 2}`);
});

ipcMain.handle('kg-changelog', async (event, since) => {
  const params = new URLSearchParams({ limit: 100 });
  if (since) params.set('since', since);
  return apiRequest('GET', `/api/kg/changelog?${params}`);
});

// File operations (with same security checks as chat window)
ipcMain.handle('open-path', async (event, filePath) => {
  if (!filePath) return;
  if (!fs.existsSync(filePath) || !fs.statSync(filePath).isFile()) {
    console.warn('[Graph] open-path: path does not exist or is not a file:', filePath);
    return;
  }
  // Block UNC paths (network shares)
  if (filePath.startsWith('\\\\') || filePath.startsWith('//')) {
    console.warn('[Graph] open-path: UNC paths not allowed:', filePath);
    return;
  }
  // Block executable extensions
  const ext = path.extname(filePath).toLowerCase();
  const blockedExts = new Set([
    '.exe', '.bat', '.cmd', '.ps1', '.vbs', '.vbe', '.wsf', '.wsh',
    '.msi', '.scr', '.com', '.cpl', '.hta', '.pif', '.reg', '.url',
    '.inf', '.application', '.appx', '.msix',
    '.lnk', '.sct', '.msp', '.diagpkg', '.ws',
  ]);
  if (blockedExts.has(ext)) {
    console.warn('[Graph] open-path: blocked executable extension:', ext);
    return;
  }
  return shell.openPath(filePath);
});

ipcMain.handle('show-item-in-folder', async (event, filePath) => {
  if (!filePath) return;
  if (!fs.existsSync(filePath)) {
    console.warn('[Graph] show-item-in-folder: path does not exist:', filePath);
    return;
  }
  // Block UNC paths
  if (filePath.startsWith('\\\\') || filePath.startsWith('//')) {
    console.warn('[Graph] show-item-in-folder: UNC paths not allowed:', filePath);
    return;
  }
  shell.showItemInFolder(filePath);
});

// ========== App Lifecycle ==========

app.whenReady().then(() => {
  createWindow();
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});
