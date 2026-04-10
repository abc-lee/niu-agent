const { app, BrowserWindow, ipcMain, screen, Tray, Menu, nativeImage, dialog } = require('electron');
const path = require('path');
const fs = require('fs');
const http = require('http');
const { exec, spawn } = require('child_process');

let spiritWindow = null;  // 小女孩窗口
let chatWindow = null;    // 聊天窗口
let stickyWindow = null;   // 便签窗口
let tray = null;         // 系统托盘
let stickyHideTimer = null; // 便签隐藏定时器
let mouseOnSticky = false;  // 鼠标是否在便签上
let mouseOnSpirit = false;   // 鼠标是否在小女孩上

// 默认尺寸
const SPIRIT_SIZE = {
  normal: { width: 96, height: 144 },
  hover: { width: 128, height: 192 }
};

// ========== 配置文件 ==========
const configPath = path.join(__dirname, 'window-config.json');

// 默认配置
const defaultConfig = {
  spirit: { x: null, y: null },  // null 表示使用默认位置
  chat: { x: null, y: null, width: 400, height: 500 },
  sticky: { x: null, y: null },
  stickySize: 80,
};

// 加载配置
function loadConfig() {
  try {
    if (fs.existsSync(configPath)) {
      const data = fs.readFileSync(configPath, 'utf-8');
      return { ...defaultConfig, ...JSON.parse(data) };
    }
  } catch (e) {
    console.error('加载配置失败:', e);
  }
  return { ...defaultConfig };
}

// 保存配置
function saveConfig(config) {
  try {
    fs.writeFileSync(configPath, JSON.stringify(config, null, 2));
  } catch (e) {
    console.error('保存配置失败:', e);
  }
}

// 当前配置
let config = loadConfig();

// ========== 小女孩窗口 ==========
function createSpiritWindow() {
  const primaryDisplay = screen.getPrimaryDisplay();
  const { width: screenWidth, height: screenHeight } = primaryDisplay.workAreaSize;

  const defaultWidth = SPIRIT_SIZE.normal.width;
  const defaultHeight = SPIRIT_SIZE.normal.height;

  // 使用保存的位置，或默认位置
  const x = config.spirit.x ?? Math.floor(screenWidth - defaultWidth - 20);
  const y = config.spirit.y ?? Math.floor(screenHeight - defaultHeight - 20);
  
  // 图标路径
  const iconPath = path.join(__dirname, 'icons', 'icon-64.png');

  spiritWindow = new BrowserWindow({
    width: defaultWidth,
    height: defaultHeight,
    x: x,
    y: y,
    frame: false,
    transparent: true,
    alwaysOnTop: true,
    resizable: false,
    skipTaskbar: true,
    hasShadow: false,
    icon: iconPath,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false
    }
  });

  spiritWindow.loadFile('spirit.html');
  spiritWindow.setBackgroundColor('#00000000');

  // 窗口显示时重新确保置顶状态（修复Electron sometimes loses alwaysOnTop state）
  spiritWindow.on('show', () => {
    if (spiritWindow && !spiritWindow.isDestroyed()) {
      spiritWindow.setAlwaysOnTop(true, 'floating');
    }
  });

  // 窗口移动时保存位置
  spiritWindow.on('moved', () => {
    if (!spiritWindow) return;
    const [posX, posY] = spiritWindow.getPosition();
    config.spirit.x = posX;
    config.spirit.y = posY;
    saveConfig(config);
  });
  
  spiritWindow.on('closed', () => {
    spiritWindow = null;
    if (chatWindow) chatWindow.close();
    if (stickyWindow) stickyWindow.close();
  });
}

// ========== 聊天窗口 ==========
function createChatWindow() {
  if (chatWindow && !chatWindow.isDestroyed()) {
    chatWindow.show();
    chatWindow.focus();
    return chatWindow;
  }

  const primaryDisplay = screen.getPrimaryDisplay();
  const { width: screenWidth, height: screenHeight } = primaryDisplay.workAreaSize;

  // 默认：小女孩旁边
  const [spiritX, spiritY] = spiritWindow ? spiritWindow.getPosition() : [100, 100];
  
  // 使用保存的位置/大小，或默认值
  const x = config.chat.x ?? spiritX + 140;
  const y = config.chat.y ?? spiritY;
  const width = config.chat.width || 400;
  const height = config.chat.height || 500;
  
  // 图标路径
  const iconPath = path.join(__dirname, 'icons', 'icon-64.png');

  chatWindow = new BrowserWindow({
    width: width,
    height: height,
    x: x,
    y: y,
    minWidth: 300,
    minHeight: 400,
    maxWidth: 800,
    maxHeight: 900,
    frame: false,
    transparent: true,
    alwaysOnTop: false,  // 聊天窗口是普通窗口，不置顶
    resizable: true,
    skipTaskbar: true,
    hasShadow: false,
    icon: iconPath,
    webPreferences: {
      preload: path.join(__dirname, 'preload-chat.js'),
      contextIsolation: true,
      nodeIntegration: false
    }
  });

  chatWindow.loadFile('chat.html');
  chatWindow.setBackgroundColor('#00000000');
  
  // F12 打开开发者工具（调试用）
  chatWindow.webContents.on('before-input-event', (event, input) => {
    if (input.key === 'F12') {
      chatWindow.webContents.toggleDevTools();
    }
  });
  
  // 窗口移动时保存位置
  chatWindow.on('moved', () => {
    if (!chatWindow) return;
    const [posX, posY] = chatWindow.getPosition();
    config.chat.x = posX;
    config.chat.y = posY;
    saveConfig(config);
  });
  
  // 窗口大小变化时保存
  chatWindow.on('resized', () => {
    if (!chatWindow) return;
    const [w, h] = chatWindow.getSize();
    config.chat.width = w;
    config.chat.height = h;
    saveConfig(config);
  });
  
  chatWindow.on('closed', () => {
    chatWindow = null;
    if (spiritWindow && !spiritWindow.isDestroyed()) {
      spiritWindow.webContents.send('chat-closed');
    }
  });
  
  return chatWindow;
}

// ========== IPC 通信 ==========

ipcMain.on('set-spirit-position', (event, { x, y }) => {
  if (spiritWindow) spiritWindow.setPosition(Math.round(x), Math.round(y));
});

ipcMain.on('resize-spirit-window', (event, { width, height }) => {
  if (spiritWindow) {
    spiritWindow.setSize(Math.round(width), Math.round(height));
  }
});

ipcMain.on('save-spirit-position', () => {
  if (spiritWindow) {
    const [posX, posY] = spiritWindow.getPosition();
    config.spirit.x = posX;
    config.spirit.y = posY;
    saveConfig(config);
  }
});

ipcMain.on('close-chat', () => {
  if (chatWindow) chatWindow.close();
});

ipcMain.on('open-chat', () => {
  createChatWindow();
});

ipcMain.on('show-sticky', () => {
  if (stickyWindow && !stickyWindow.isDestroyed()) {
    // 取消待执行的隐藏
    clearTimeout(stickyHideTimer);
    stickyHideTimer = null;

    // 计算位置：根据 spirit 在屏幕哪一侧决定 sticky 显示在哪边
    if (spiritWindow && !spiritWindow.isDestroyed()) {
      const [sx, sy] = spiritWindow.getPosition();
      const spiritSize = spiritWindow.getSize();
      const primaryDisplay = screen.getPrimaryDisplay();
      const { width: screenWidth, height: screenHeight } = primaryDisplay.workAreaSize;

      // 动态窗口尺寸
      const currentSize = config.stickySize || 80;
      const windowWidth = currentSize + 24;
      const windowHeight = currentSize * 2 + 48;

      let stickyX, stickyY;

      // spirit 在屏幕右侧，sticky 显示在左侧
      if (sx > screenWidth / 2) {
        stickyX = sx - windowWidth - 10;
      } else {
        stickyX = sx + spiritSize[0] + 10; // spirit 右边 + 10px
      }

      // 确保不超出屏幕
      stickyX = Math.max(0, Math.min(stickyX, screenWidth - windowWidth));
      stickyY = Math.max(0, Math.min(sy, screenHeight - windowHeight));

      stickyWindow.setPosition(Math.round(stickyX), Math.round(stickyY));
    }
    stickyWindow.show();
    stickyWindow.setAlwaysOnTop(true, 'floating'); // 确保置顶状态
  }
});

// 延迟隐藏便签 - 添加延迟避免鼠标在窗口间移动时误触发
function scheduleHideSticky() {
  clearTimeout(stickyHideTimer);
  // 延迟 200ms 再开始计时，给鼠标进入另一个窗口留出时间
  stickyHideTimer = setTimeout(() => {
    if (!mouseOnSticky && !mouseOnSpirit) {
      if (stickyWindow && !stickyWindow.isDestroyed()) {
        stickyWindow.hide();
      }
    }
  }, 2000); // 2秒延迟
}

ipcMain.on('hide-sticky', () => {
  scheduleHideSticky();
});

// 鼠标进入便签
ipcMain.on('sticky-mouse-enter', () => {
  mouseOnSticky = true;
  clearTimeout(stickyHideTimer);
  stickyHideTimer = null;
});

// 鼠标离开便签
ipcMain.on('sticky-mouse-leave', () => {
  mouseOnSticky = false;
  scheduleHideSticky();
});

// 鼠标进入小女孩
ipcMain.on('spirit-mouse-enter', () => {
  mouseOnSpirit = true;
  clearTimeout(stickyHideTimer);
  stickyHideTimer = null;
});

// 鼠标离开小女孩
ipcMain.on('spirit-mouse-leave', () => {
  mouseOnSpirit = false;
  scheduleHideSticky();
});

// ========== 便签 CRUD ==========
// 创建便签
ipcMain.handle('create-note', async (event, note) => {
  return new Promise((resolve) => {
    const data = JSON.stringify(note);
    
    const req = http.request({
      hostname: '127.0.0.1',
      port: 9876,
      path: '/api/notes',
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(data)
      }
    }, (res) => {
      let body = '';
      res.on('data', chunk => body += chunk);
      res.on('end', () => {
        try {
          const result = JSON.parse(body);
          resolve(result);
        } catch (e) {
          resolve({ success: false, error: e.message });
        }
      });
    });
    req.on('error', (e) => {
      console.error('创建便签失败:', e.message);
      resolve({ success: false, error: e.message });
    });
    req.write(data);
    req.end();
  });
});

// 更新便签
ipcMain.handle('update-note', async (event, note) => {
  return new Promise((resolve) => {
    const data = JSON.stringify(note);
    
    const req = http.request({
      hostname: '127.0.0.1',
      port: 9876,
      path: `/api/notes/${note.id}`,
      method: 'PUT',
      headers: {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(data)
      }
    }, (res) => {
      let body = '';
      res.on('data', chunk => body += chunk);
      res.on('end', () => {
        try {
          const result = JSON.parse(body);
          resolve(result);
        } catch (e) {
          resolve({ success: false, error: e.message });
        }
      });
    });
    req.on('error', (e) => {
      console.error('更新便签失败:', e.message);
      resolve({ success: false, error: e.message });
    });
    req.write(data);
    req.end();
  });
});

// 删除便签
ipcMain.handle('delete-note', async (event, id) => {
  return new Promise((resolve) => {
    const req = http.request({
      hostname: '127.0.0.1',
      port: 9876,
      path: `/api/notes/${id}`,
      method: 'DELETE'
    }, (res) => {
      let body = '';
      res.on('data', chunk => body += chunk);
      res.on('end', () => {
        try {
          const result = JSON.parse(body);
          resolve(result);
        } catch (e) {
          resolve({ success: false, error: e.message });
        }
      });
    });
    req.on('error', (e) => {
      console.error('删除便签失败:', e.message);
      resolve({ success: false, error: e.message });
    });
    req.end();
  });
});

// 获取便签尺寸
ipcMain.handle('get-sticky-size', async () => {
  return config.stickySize || 80;
});

// 获取统计数据（供聊天窗口使用）
ipcMain.handle('get-stats', async () => {
  return new Promise((resolve, reject) => {
    const req = http.request({
      hostname: '127.0.0.1',
      port: 9876,
      path: '/api/stats',
      method: 'GET'
    }, (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => {
        try {
          resolve(JSON.parse(data));
        } catch (e) {
          reject(e);
        }
      });
    });
    req.on('error', reject);
    req.end();
  });
});

// 保存便签尺寸
ipcMain.on('save-sticky-size', (event, size) => {
  // 限制范围 60-250
  size = Math.max(60, Math.min(250, size));
  config.stickySize = size;
  saveConfig(config);

  // 动态调整窗口大小和位置
  if (stickyWindow && !stickyWindow.isDestroyed()) {
    const windowWidth = size + 24;
    const windowHeight = size * 2 + 48;
    stickyWindow.setSize(windowWidth, windowHeight);

    // 重新计算位置避免超出屏幕
    if (spiritWindow && !spiritWindow.isDestroyed()) {
      const [sx, sy] = spiritWindow.getPosition();
      const spiritSize = spiritWindow.getSize();
      const primaryDisplay = screen.getPrimaryDisplay();
      const { width: screenWidth, height: screenHeight } = primaryDisplay.workAreaSize;

      let stickyX;
      if (sx > screenWidth / 2) {
        stickyX = sx - windowWidth - 10;
      } else {
        stickyX = sx + spiritSize[0] + 10;
      }

      // 确保不超出屏幕
      stickyX = Math.max(0, Math.min(stickyX, screenWidth - windowWidth));
      const stickyY = Math.max(0, Math.min(sy, screenHeight - windowHeight));

      stickyWindow.setPosition(Math.round(stickyX), Math.round(stickyY));
    }
  }
});

ipcMain.on('close-all', () => {
  // 关闭所有窗口
  if (chatWindow) chatWindow.close();
  if (spiritWindow) spiritWindow.hide(); // 隐藏而不是关闭，因为还要用
  
  // 通知后端关闭
  try {
    const req = http.request({
      hostname: '127.0.0.1',
      port: 9876,
      path: '/api/shutdown',
      method: 'POST',
      timeout: 1000  // 1秒超时
    }, () => {});
    req.on('error', () => {});
    req.end();
  } catch (e) {}
  
  // 退出应用
  app.quit();
});

ipcMain.on('spirit-state', (event, state) => {
  if (chatWindow && !chatWindow.isDestroyed()) {
    chatWindow.webContents.send('spirit-state', state);
  }
});

// 接收聊天窗口的忙碌状态通知，转发给小女孩窗口
ipcMain.on('notify-busy', (event, { isBusy, reason }) => {
  if (spiritWindow && !spiritWindow.isDestroyed()) {
    spiritWindow.webContents.send('busy-state', isBusy, reason);
  }
});

// 接收聊天窗口的用户活动通知，转发给小女孩窗口（重置空闲计时器）
ipcMain.on('notify-activity', () => {
  if (spiritWindow && !spiritWindow.isDestroyed()) {
    spiritWindow.webContents.send('user-activity');
  }
});

// 用系统默认查看器打开文件
ipcMain.on('open-with-system-viewer', (event, filePath) => {
  if (!filePath) return;
  // 使用 Electron 的 shell.openPath 打开文件
  const { shell } = require('electron');
  shell.openPath(filePath).catch(err => {
    console.error('[Main] 打开文件失败:', err);
  });
});

// 获取图片显示 URL（本地路径转 file:// URL）
ipcMain.handle('get-image-url', async (event, filePath) => {
  if (!filePath) return null;
  // Windows 路径转 file:// URL
  // REDACTED_WIN_PATH\photo.jpg → file:///REDACTED_WIN_PATH/photo.jpg
  const normalized = filePath.replace(/\\/g, '/');
  // 处理中文路径
  const encoded = encodeURI('file:///' + normalized);
  return encoded;
});

// 处理拖入的图片（调用后端 API）
ipcMain.handle('process-image', async (event, filePath) => {
  return new Promise((resolve) => {
    const data = JSON.stringify({
      session_id: config.chatSessionId || null,
      message: `处理照片: ${filePath}`
    });
    
    const req = http.request({
      hostname: '127.0.0.1',
      port: 9876,
      path: '/api/chat/session',
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(data)
      }
    }, (res) => {
      let body = '';
      res.on('data', chunk => body += chunk);
      res.on('end', () => {
        try {
          const result = JSON.parse(body);
          if (result.session_id) {
            config.chatSessionId = result.session_id;
            saveConfig(config);
          }
          resolve(result);
        } catch (e) {
          resolve({ error: '解析响应失败' });
        }
      });
    });
    req.on('error', (e) => resolve({ error: e.message }));
    req.write(data);
    req.end();
  });
});

ipcMain.handle('send-message', async (event, message) => {
  return new Promise((resolve) => {
    // Load session ID from config
    const data = JSON.stringify({ message: message });
    
    const req = http.request({
      hostname: '127.0.0.1',
      port: 9876,
      path: '/api/chat/session',
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(data)
      }
    }, (res) => {
      let body = '';
      res.on('data', chunk => body += chunk);
      res.on('end', () => {
        try {
          const result = JSON.parse(body);
          resolve(result);
        } catch (e) {
          resolve({ error: '解析响应失败' });
        }
      });
    });
    req.on('error', (e) => resolve({ error: e.message }));
    req.write(data);
    req.end();
  });
});

// 发送消息给 Agent（用于文件拖拽等场景）
// 使用 /api/chat/session 实现会话持久化
ipcMain.handle('send-to-agent', async (event, { message, context }) => {
  console.log('发送消息给 Agent:', message.substring(0, 100) + '...');
  
  return new Promise((resolve) => {
    // 使用与聊天窗口相同的 sessionID，实现上下文共享
    const data = JSON.stringify({ message: message });
    
    const req = http.request({
      hostname: '127.0.0.1',
      port: 9876,
      path: '/api/chat/session',
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(data)
      },
      timeout: 300000  // 5分钟超时，给 LLM 足够时间
    }, (res) => {
      let body = '';
      res.on('data', chunk => body += chunk);
      res.on('end', () => {
        try {
          const result = JSON.parse(body);
          // 保存 session ID 用于后续对话持久化
          if (result.sessionId) {
            config.chatSessionId = result.sessionId;
            saveConfig(config);
          }
          console.log('Agent 响应:', result.reply ? result.reply.substring(0, 100) + '...' : result.error);
          resolve(result);
        } catch (e) {
          console.error('解析 Agent 响应失败:', e);
          resolve({ error: '解析响应失败' });
        }
      });
    });
    req.on('error', (e) => resolve({ error: e.message }));
    req.on('timeout', () => {
      console.error('Agent 请求超时');
      req.destroy();
      resolve({ error: '请求超时' });
    });
    req.write(data);
    req.end();
  });
});

// 获取当前聊天 session ID
ipcMain.handle('get-chat-session-id', async () => {
  return config.chatSessionId || 'default';  // 返回当前 session ID 或默认值
});

// 获取历史消息
ipcMain.handle('get-history', async (event, limit, beforeId) => {
  // No session ID needed
  return new Promise((resolve) => {
    let url = `http://127.0.0.1:9876/api/context/messages?limit=${limit || 20}&full=true`;
    if (beforeId) {
      url += `&before_id=${beforeId}`;
    }
    http.get(url, (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => {
        try {
          const result = JSON.parse(data);
          // 返回消息和总数
          resolve({
            messages: result.messages || [],
            totalInDb: result.total_in_db || 0
          });
        } catch (e) {
          resolve({ messages: [], totalInDb: 0 });
        }
      });
    }).on('error', (e) => {
      console.error('获取历史消息失败:', e.message);
      resolve({ messages: [], totalInDb: 0 });
    });
  });
});

// 清空聊天记录
ipcMain.handle('clear-chat', async () => {
  const sessionId = config.chatSessionId;
  if (!sessionId) {
    return { success: true, message: 'No session to clear' };
  }

  // 清空待推送消息队列
  pendingAlertMessages = [];

  return new Promise((resolve) => {
    const data = JSON.stringify({ sessionId: sessionId });
    const req = http.request({
      hostname: '127.0.0.1',
      port: 9876,
      path: '/api/chat/clear',
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(data)
      }
    }, (res) => {
      let body = '';
      res.on('data', chunk => body += chunk);
      res.on('end', () => {
        try {
          const result = JSON.parse(body);
          console.log('清空聊天记录:', sessionId, result);
          resolve(result);
        } catch (e) {
          resolve({ success: false, error: '解析响应失败' });
        }
      });
    });
    req.on('error', (e) => {
      console.error('清空聊天记录失败:', e.message);
      resolve({ success: false, error: e.message });
    });
    req.write(data);
    req.end();
  });
});

// 打开图谱窗口
ipcMain.on('open-graph', () => {
  const { spawn } = require('child_process');
  const exePath = path.join(__dirname, '..', '..', 'niu.exe');
  const uiPath = path.join(__dirname, '..', '..');
  console.log('Opening graph, exe:', exePath, 'cwd:', uiPath);
  spawn(exePath, ['--graph'], {
    cwd: uiPath,
    detached: true,
    stdio: 'ignore',
    shell: true
  }).unref();
});

// ========== 便签窗口 ==========
function createStickyWindow() {
  if (stickyWindow && !stickyWindow.isDestroyed()) {
    stickyWindow.show();
    stickyWindow.setAlwaysOnTop(true, 'floating'); // 确保置顶状态
    return stickyWindow;
  }

  const primaryDisplay = screen.getPrimaryDisplay();
  const { width: screenWidth, height: screenHeight } = primaryDisplay.workAreaSize;

  // 默认位置：小女孩右边
  const [spiritX, spiritY] = spiritWindow ? spiritWindow.getPosition() : [100, 100];
  const x = config.sticky?.x ?? spiritX + 110;
  const y = config.sticky?.y ?? spiritY;
  
  const iconPath = path.join(__dirname, 'icons', 'icon-32.png');

  // 窗口尺寸 = stickySize * 2 (上下两个) + padding + gap
  const currentStickySize = config.stickySize || 80;
  const windowWidth = currentStickySize + 24;
  const windowHeight = currentStickySize * 2 + 48;

  stickyWindow = new BrowserWindow({
    width: windowWidth,
    height: windowHeight,
    x: x,
    y: y,
    frame: false,
    transparent: true,
    alwaysOnTop: true,
    resizable: false,
    skipTaskbar: true,
    hasShadow: false,
    icon: iconPath,
    webPreferences: {
      preload: path.join(__dirname, 'preload-sticky.js'),
      contextIsolation: true,
      nodeIntegration: false
    }
  });

  stickyWindow.loadFile('sticky.html');
  stickyWindow.setBackgroundColor('#00000000');
  stickyWindow.hide(); // 默认隐藏

  // 窗口显示时重新确保置顶状态（修复Electron sometimes loses alwaysOnTop state）
  stickyWindow.on('show', () => {
    if (stickyWindow && !stickyWindow.isDestroyed()) {
      stickyWindow.setAlwaysOnTop(true, 'floating');
    }
  });

  // 保存位置
  stickyWindow.on('moved', () => {
    if (!stickyWindow) return;
    const [posX, posY] = stickyWindow.getPosition();
    config.sticky = config.sticky || {};
    config.sticky.x = posX;
    config.sticky.y = posY;
    saveConfig(config);
  });

  return stickyWindow;
}

// ========== 启动 ==========
// niu.exe 已经启动了后端，这里只需要创建窗口
app.whenReady().then(() => {
  createSpiritWindow();
  createStickyWindow();
  createTray();
});

// ========== 系统托盘 ==========
function createTray() {
  // 创建托盘图标 - 使用头像
  const iconPath = path.join(__dirname, 'icons', 'icon-16.png');
  let trayIcon;
  
  if (fs.existsSync(iconPath)) {
    trayIcon = nativeImage.createFromPath(iconPath);
  } else {
    // 如果没有图标，创建一个简单的
    trayIcon = nativeImage.createEmpty();
  }
  
  // Windows 下调整图标大小
  if (process.platform === 'win32') {
    trayIcon = trayIcon.resize({ width: 16, height: 16 });
  }
  
  tray = new Tray(trayIcon);
  tray.setToolTip('妞妞 - 个人知识助理');
  
  // 创建托盘菜单
  const contextMenu = Menu.buildFromTemplate([
    {
      label: '💬 打开聊天',
      click: () => {
        createChatWindow();
      }
    },
    {
      label: '📊 打开图谱',
      click: () => {
        const exePath = path.join(__dirname, '..', '..', 'niu.exe');
        const uiPath = path.join(__dirname, '..', '..');
        spawn(exePath, ['--graph'], {
          cwd: uiPath,
          detached: true,
          stdio: 'ignore',
          shell: true
        }).unref();
      }
    },
    { type: 'separator' },
    {
      label: '⛔ 关闭妞妞',
      click: async () => {
        const result = await dialog.showMessageBox({
          type: 'warning',
          buttons: ['取消', '确认关闭'],
          defaultId: 0,
          cancelId: 0,
          title: '确认关闭',
          message: '⚠️ 确认关闭',
          detail: '所有后台任务将停止'
        });
        
        if (result.response === 1) {
          // 用户点击了"确认关闭"
          // 直接退出 Electron，不调用 /api/shutdown
          // Go launcher 会检测到 Electron 退出并清理所有资源
          app.exit(0);
        }
      }
    }
  ]);
  
  tray.setContextMenu(contextMenu);
  
  // 点击托盘图标显示/隐藏小女孩
  tray.on('click', () => {
    if (spiritWindow) {
      if (spiritWindow.isVisible()) {
        spiritWindow.hide();
      } else {
        spiritWindow.show();
        spiritWindow.setAlwaysOnTop(true, 'floating'); // 确保置顶状态
      }
    }
  });
}

app.on('window-all-closed', () => {
  // Windows/Linux: 保持托盘，不退出
  // macOS: 不处理，由 menu 处理
});

app.on('before-quit', () => {
  // 停止轮询
  stopPendingAlertsPolling();
  
  // 通知后端关闭
  try {
    const req = http.request({
      hostname: '127.0.0.1',
      port: 9876,
      path: '/api/shutdown',
      method: 'POST',
      timeout: 500
    }, () => {});
    req.on('error', () => {});
    req.end();
  } catch (e) {}
  
  if (chatWindow) chatWindow.destroy();
  if (spiritWindow) spiritWindow.destroy();
  if (tray) tray.destroy();
});

// ========== macOS 菜单 ==========
if (process.platform === 'darwin') {
  const { Menu } = require('electron');
  
  const macMenu = Menu.buildFromTemplate([
    {
      label: '妞妞',
      submenu: [
        { role: 'about' },
        { type: 'separator' },
        {
          label: '退出',
          accelerator: 'Cmd+Q',
          click: () => {
            app.quit();
          }
        }
      ]
    }
  ]);
  
  Menu.setApplicationMenu(macMenu);
}

// ========== 定时提醒轮询 ==========
let pendingAlertMessages = [];
let alertsPollingTimer = null;  // 保存定时器引用

// 轮询待推送消息
function startPendingAlertsPolling() {
  alertsPollingTimer = setInterval(async () => {
    try {
      const alerts = await fetchPendingAlerts();
      if (alerts && alerts.length > 0) {
        console.log('[Alerts] 收到', alerts.length, '条待推送消息');
        
        // 判断聊天窗口是否在焦点
        const chatFocused = chatWindow && chatWindow.isFocused() && chatWindow.isVisible();
        
        if (!chatFocused) {
          // 聊天窗口不在焦点，小女孩蹦高
          alerts.forEach(alert => {
            pendingAlertMessages.push(alert.content);
          });
          
          // 通知小女孩蹦高
          if (spiritWindow) {
            spiritWindow.webContents.send('alert', alerts[alerts.length - 1].content);
          }
        }
      }
    } catch (e) {
      // 忽略错误，继续轮询
    }
  }, 10000);  // 每10秒轮询一次
}

// 停止轮询
function stopPendingAlertsPolling() {
  if (alertsPollingTimer) {
    clearInterval(alertsPollingTimer);
    alertsPollingTimer = null;
  }
}

// 获取待推送消息
function fetchPendingAlerts() {
  return new Promise((resolve) => {
    http.get('http://127.0.0.1:9876/api/pending-alerts', (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => {
        try {
          resolve(JSON.parse(data));
        } catch (e) {
          resolve([]);
        }
      });
    }).on('error', () => {
      resolve([]);
    });
  });
}

// 获取待显示消息（聊天窗口打开时调用）
ipcMain.handle('get-pending-messages', async () => {
  const messages = [...pendingAlertMessages];
  pendingAlertMessages = [];
  return messages;
});

// 延迟启动轮询（等待后端完全启动）
setTimeout(() => {
  startPendingAlertsPolling();
  console.log('[Alerts] 开始轮询待推送消息');
}, 10000);
