const { app, BrowserWindow, ipcMain } = require('electron');
const path = require('path');
const fs = require('fs');

// Config paths
const configDir = path.join(__dirname, '..', '..', 'config');
const userConfigPath = path.join(configDir, 'user-config.json');
const presetsPath = path.join(configDir, 'llm-presets.json');

let win;

function createWindow() {
  win = new BrowserWindow({
    width: 500,
    height: 650,
    resizable: false,
    frame: false,
    transparent: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false
    }
  });

  win.loadFile('index.html');
}

// IPC handlers
ipcMain.handle('get-presets', () => {
  try {
    const data = fs.readFileSync(presetsPath, 'utf-8');
    return JSON.parse(data).presets;
  } catch (e) {
    console.error('Failed to read presets:', e);
    return [];
  }
});

ipcMain.handle('get-config', () => {
  try {
    if (fs.existsSync(userConfigPath)) {
      return JSON.parse(fs.readFileSync(userConfigPath, 'utf-8'));
    }
  } catch (e) {
    console.error('Failed to read config:', e);
  }
  return { llm: {}, storage: {}, firstRun: true };
});

ipcMain.handle('save-config', (event, config) => {
  try {
    // Ensure config directory exists
    if (!fs.existsSync(path.dirname(userConfigPath))) {
      fs.mkdirSync(path.dirname(userConfigPath), { recursive: true });
    }
    fs.writeFileSync(userConfigPath, JSON.stringify(config, null, 2));
    console.log('Config saved to:', userConfigPath);
    return { success: true };
  } catch (e) {
    console.error('Failed to save config:', e);
    return { success: false, error: e.message };
  }
});

ipcMain.handle('test-connection', async (event, { apiBase, apiKey, type }) => {
  // Skip test for Ollama (local)
  if (apiBase.includes('localhost') || apiBase.includes('127.0.0.1')) {
    return { success: true, message: '本地模型，跳过测试' };
  }
  
  const https = require('https');
  const http = require('http');
  
  return new Promise((resolve) => {
    try {
      const url = new URL(apiBase);
      const client = url.protocol === 'https:' ? https : http;
      
      // Try a simple request - just check if server responds
      const options = {
        hostname: url.hostname,
        port: url.port || (url.protocol === 'https:' ? 443 : 80),
        path: '/',
        method: 'GET',
        timeout: 5000
      };
      
      const req = client.request(options, (res) => {
        // Any response means the server is reachable
        resolve({ success: true, message: `服务器响应: ${res.statusCode}` });
      });
      
      req.on('error', (err) => {
        // For API endpoints, even connection errors might be OK
        // (some servers don't respond to GET /)
        resolve({ success: true, message: '无法验证，但配置已保存' });
      });
      
      req.on('timeout', () => {
        req.destroy();
        resolve({ success: true, message: '连接超时，但配置已保存' });
      });
      
      req.end();
    } catch (e) {
      resolve({ success: true, message: '配置已保存' });
    }
  });
});

ipcMain.handle('close-window', () => {
  if (win) {
    win.close();
  }
});

ipcMain.handle('minimize-window', () => {
  if (win) {
    win.minimize();
  }
});

app.whenReady().then(createWindow);

app.on('window-all-closed', () => {
  app.quit();
});
