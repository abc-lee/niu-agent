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

ipcMain.handle('test-connection', async (event, config) => {
  const http = require('http');
  const https = require('https');
  const { apiBase, apiKey, type, model } = config;
  const port = parseInt(process.env.NIU_API_PORT || '9876');

  // 优先走 Python API（验证完整 LiteLLM 链路，包括 provider 路由）
  try {
    const result = await new Promise((resolve) => {
      const body = JSON.stringify(config || {});
      const req = http.request({
        hostname: '127.0.0.1', port,
        path: '/api/test-llm', method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) },
        timeout: 20000
      }, (res) => {
        let data = '';
        res.on('data', (chunk) => { data += chunk; });
        res.on('end', () => {
          try { resolve(JSON.parse(data)); }
          catch (e) { resolve({ success: false, error: `API 返回非 JSON 响应 (HTTP ${res.statusCode})` }); }
        });
      });
      req.on('error', () => { resolve(null); });  // API 不可达 → fallback
      req.on('timeout', () => { req.destroy(); resolve(null); });
      req.write(body);
      req.end();
    });

    if (result !== null) {
      return { success: result.success, message: result.success ? (result.message || '测试通过') : (result.error || '未知错误') };
    }
  } catch (e) { /* fallback below */ }

  // Fallback: 直接 HTTP 调用 LLM（niu --settings 独立模式，API 未启动）

  // Skip test for Ollama (local) — just verify server is reachable
  if (apiBase.includes('localhost') || apiBase.includes('127.0.0.1')) {
    try {
      const url = new URL(apiBase);
      const client = url.protocol === 'https:' ? https : http;
      const reachable = await new Promise((resolve) => {
        const req = client.request({
          hostname: url.hostname,
          port: url.port || (url.protocol === 'https:' ? 443 : 80),
          path: '/',
          method: 'GET',
          timeout: 5000
        }, (res) => { resolve(true); });
        req.on('error', () => { resolve(false); });
        req.on('timeout', () => { req.destroy(); resolve(false); });
        req.end();
      });
      if (reachable) {
        return { success: true, message: '本地模型服务已连接（未验证 provider 路由）' };
      } else {
        return { success: false, message: '本地模型服务未启动，请确认 Ollama 正在运行' };
      }
    } catch (e) {
      return { success: false, message: '本地模型服务未启动: ' + e.message };
    }
  }

  // Real LLM test: send a minimal chat completion request
  try {
    const url = new URL(apiBase);
    const client = url.protocol === 'https:' ? https : http;

    if (type === 'anthropic') {
      // Anthropic Messages API format
      const body = JSON.stringify({
        model: model,
        max_tokens: 5,
        messages: [{ role: 'user', content: 'hi' }]
      });

      const result = await new Promise((resolve, reject) => {
        const req = client.request({
          hostname: url.hostname,
          port: url.port || (url.protocol === 'https:' ? 443 : 80),
          path: (() => { let p = url.pathname === '/' ? '' : url.pathname; if (p.endsWith('/v1/messages')) p = p.slice(0, -13); else if (p.endsWith('/v1')) p = p.slice(0, -3); return p + '/v1/messages'; })(),
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'x-api-key': apiKey,
            'anthropic-version': '2023-06-01',
            'Content-Length': Buffer.byteLength(body)
          },
          timeout: 15000
        }, (res) => {
          let data = '';
          res.on('data', (chunk) => { data += chunk; });
          res.on('end', () => {
            if (res.statusCode === 200) {
              try {
                const json = JSON.parse(data);
                const content = json.content && json.content[0] && json.content[0].text;
                resolve({ success: true, message: `模型响应正常 (${model})（未验证 provider 路由）` });
              } catch (e) {
                resolve({ success: true, message: `模型响应正常 (${model})（未验证 provider 路由）` });
              }
            } else {
              let errorMsg = `HTTP ${res.statusCode}`;
              try {
                const errJson = JSON.parse(data);
                errorMsg = errJson.error?.message || errJson.message || errorMsg;
              } catch (e) {}
              resolve({ success: false, message: `测试失败: ${errorMsg}` });
            }
          });
        });
        req.on('error', (err) => {
          resolve({ success: false, message: '连接失败: ' + err.message });
        });
        req.on('timeout', () => {
          req.destroy();
          resolve({ success: false, message: '连接超时，请检查网络和API地址' });
        });
        req.write(body);
        req.end();
      });

      return result;

    } else {
      // OpenAI-compatible chat completions API format
      const body = JSON.stringify({
        model: model,
        max_tokens: 5,
        messages: [{ role: 'user', content: 'hi' }]
      });

      const result = await new Promise((resolve, reject) => {
        const req = client.request({
          hostname: url.hostname,
          port: url.port || (url.protocol === 'https:' ? 443 : 80),
          path: (() => { let p = url.pathname === '/' ? '' : url.pathname; if (p.endsWith('/chat/completions')) p = p.slice(0, -17); return p + '/chat/completions'; })(),
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Authorization': `Bearer ${apiKey}`,
            'Content-Length': Buffer.byteLength(body)
          },
          timeout: 15000
        }, (res) => {
          let data = '';
          res.on('data', (chunk) => { data += chunk; });
          res.on('end', () => {
            if (res.statusCode === 200) {
              resolve({ success: true, message: `模型响应正常 (${model})（未验证 provider 路由）` });
            } else {
              let errorMsg = `HTTP ${res.statusCode}`;
              try {
                const errJson = JSON.parse(data);
                errorMsg = errJson.error?.message || errJson.message || errorMsg;
              } catch (e) {}
              resolve({ success: false, message: `测试失败: ${errorMsg}` });
            }
          });
        });
        req.on('error', (err) => {
          resolve({ success: false, message: '连接失败: ' + err.message });
        });
        req.on('timeout', () => {
          req.destroy();
          resolve({ success: false, message: '连接超时，请检查网络和API地址' });
        });
        req.write(body);
        req.end();
      });

      return result;
    }
  } catch (e) {
    return { success: false, message: '测试异常: ' + e.message };
  }
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
