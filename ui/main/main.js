// === Step 2: 入口 + 模式分支 ===
// 单一 Electron 入口：按 NIU_WINDOW 环境变量分支创建对应 BrowserWindow
//   - assistant: spirit + chat + sticky + tray + SSE 轮询 + Dock.hide(macOS)
//   - settings:  只创 settings 窗口（不调 Dock.hide）
//   - graph:     只创 graph 窗口（不调 Dock.hide）
// 合并自 ui/assistant/main.js + ui/settings/main.js + ui/graph/main.js（零命名冲突）
const { app, BrowserWindow, ipcMain, screen, Tray, Menu, nativeImage, dialog, shell } = require('electron');
const path = require('path');
const fs = require('fs');
const os = require('os');
const url = require('url');
const http = require('http');
const { exec, spawn } = require('child_process');

const WINDOW_MODE = process.env.NIU_WINDOW || 'assistant';

let spiritWindow = null;  // 小女孩窗口
let chatWindow = null;    // 聊天窗口
let stickyWindow = null;  // 便签窗口
let settingsWindow = null;  // 设置窗口
let graphWindow = null;     // 图谱窗口
let tray = null;          // 系统托盘
let stickyHideTimer = null; // 便签隐藏定时器
let mouseOnSticky = false;  // 鼠标是否在便签上
let mouseOnSpirit = false;  // 鼠标是否在小女孩上

function shouldCreateTray() { return WINDOW_MODE === 'assistant'; }

// 默认尺寸
const SPIRIT_SIZE = {
  normal: { width: 96, height: 144 },
  hover: { width: 128, height: 192 }
};

// ========== 配置文件 ==========
// window-config.json 写到 ~/.niu/（bundle 内只读）
const niuHome = path.join(os.homedir(), '.niu');
if (!fs.existsSync(niuHome)) {
  fs.mkdirSync(niuHome, { recursive: true });
}
const configPath = path.join(niuHome, 'window-config.json');

// 迁移：首次启动若 ~/.niu/window-config.json 不存在但 bundle 内旧路径有文件，复制过去
// 旧路径：ui/main/windows/assistant/window-config.json（升级前用户数据在这里）
const oldConfigPath = path.join(__dirname, 'windows', 'assistant', 'window-config.json');
if (!fs.existsSync(configPath) && fs.existsSync(oldConfigPath)) {
  try {
    fs.copyFileSync(oldConfigPath, configPath);
    console.log('Migrated window-config.json from bundle to ~/.niu/');
  } catch (e) {
    console.error('Failed to migrate window-config.json:', e);
  }
}

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

// === Step 3: createSpiritWindow（来自 ui/assistant/main.js） ===
// 改动：preload → preload-assistant.js，HTML/GIF/icons/fonts 路径加 windows/assistant/ 前缀
function createSpiritWindow() {
  const primaryDisplay = screen.getPrimaryDisplay();
  const { width: screenWidth, height: screenHeight } = primaryDisplay.workAreaSize;

  const defaultWidth = SPIRIT_SIZE.normal.width;
  const defaultHeight = SPIRIT_SIZE.normal.height;

  // 使用保存的位置，或默认位置
  const x = config.spirit.x ?? Math.floor(screenWidth - defaultWidth - 20);
  const y = config.spirit.y ?? Math.floor(screenHeight - defaultHeight - 20);

  // 图标路径
  const iconPath = path.join(__dirname, 'windows', 'assistant', 'icons', 'icon-64.png');

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
      preload: path.join(__dirname, 'preload-assistant.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false
    }
  });

  spiritWindow.loadFile(path.join(__dirname, 'windows', 'assistant', 'spirit.html'));
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

// === Step 4: createChatWindow（来自 ui/assistant/main.js） ===
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
  const iconPath = path.join(__dirname, 'windows', 'assistant', 'icons', 'icon-64.png');

  chatWindow = new BrowserWindow({
    width: width,
    height: height,
    x: x,
    y: y,
    minWidth: 300,
    minHeight: 400,
    frame: false,
    alwaysOnTop: false,  // 聊天窗口是普通窗口，不置顶
    resizable: true,
    skipTaskbar: true,
    icon: iconPath,
    webPreferences: {
      preload: path.join(__dirname, 'preload-chat.js'),
      contextIsolation: true,
      nodeIntegration: false
    }
  });

  chatWindow.loadFile(path.join(__dirname, 'windows', 'assistant', 'chat.html'));
  chatWindow.setBackgroundColor('#faf8f0');

  // F12 打开开发者工具（调试用）
  chatWindow.webContents.on('before-input-event', (event, input) => {
    if (input.key === 'F12') {
      chatWindow.webContents.toggleDevTools();
    }
    // macOS: 确保编辑快捷键正常工作
    // Electron 在某些情况下可能不处理这些快捷键
    if (input.meta) {  // Cmd 键
      if (input.key === 'v') {
        chatWindow.webContents.paste();
      } else if (input.key === 'c') {
        chatWindow.webContents.copy();
      } else if (input.key === 'x') {
        chatWindow.webContents.cut();
      } else if (input.key === 'a') {
        chatWindow.webContents.selectAll();
      }
    }
  });

  // 拦截所有导航：阻止在 Electron 窗口内打开外部链接
  chatWindow.webContents.on('will-navigate', (event, url) => {
    // 允许加载本地文件（chat.html 等），只拦截外部链接
    if (!url.startsWith('file://')) {
      event.preventDefault();
      try {
        const parsed = new URL(url);
        if (parsed.protocol === 'http:' || parsed.protocol === 'https:') {
          shell.openExternal(url);
        }
      } catch (e) {
        // Invalid URL, ignore
      }
    }
  });

  // 拦截新窗口打开（target="_blank" 等）
  chatWindow.webContents.setWindowOpenHandler(({ url }) => {
    try {
      const parsed = new URL(url);
      if (parsed.protocol === 'http:' || parsed.protocol === 'https:') {
        shell.openExternal(url);
      }
    } catch (e) {
      // Invalid URL, ignore
    }
    return { action: 'deny' };
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

  // 窗口显示/获得焦点时通知前端同步状态（如停止按钮的可见性）
  chatWindow.on('show', () => {
    if (chatWindow && !chatWindow.isDestroyed()) {
      chatWindow.webContents.send('sync-state');
    }
  });
  chatWindow.on('focus', () => {
    if (chatWindow && !chatWindow.isDestroyed()) {
      chatWindow.webContents.send('sync-state');
    }
  });

  chatWindow.on('closed', () => {
    chatWindow = null;
    if (spiritWindow && !spiritWindow.isDestroyed()) {
      spiritWindow.webContents.send('chat-closed');
    }
  });

  return chatWindow;
}

// === Step 5: createStickyWindow（来自 ui/assistant/main.js） ===
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

  const iconPath = path.join(__dirname, 'windows', 'assistant', 'icons', 'icon-32.png');

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

  stickyWindow.loadFile(path.join(__dirname, 'windows', 'assistant', 'sticky.html'));
  stickyWindow.setBackgroundColor('#00000000');
  stickyWindow.hide(); // 默认隐藏

  // 编辑快捷键：确保 Cmd/Ctrl+A/C/V/X/Z 在便签窗口正常工作
  stickyWindow.webContents.on('before-input-event', (event, input) => {
    if (input.meta || input.control) {
      if (input.key === 'v') {
        event.preventDefault();
        stickyWindow.webContents.paste();
      } else if (input.key === 'c') {
        event.preventDefault();
        stickyWindow.webContents.copy();
      } else if (input.key === 'x') {
        event.preventDefault();
        stickyWindow.webContents.cut();
      } else if (input.key === 'a') {
        event.preventDefault();
        stickyWindow.webContents.selectAll();
      } else if (input.key === 'z' && !input.shift) {
        event.preventDefault();
        stickyWindow.webContents.undo();
      } else if (input.key === 'z' && input.shift) {
        event.preventDefault();
        stickyWindow.webContents.redo();
      }
    }
  });

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

// === Step 6: createSettingsWindow（来自 ui/settings/main.js） ===
// 改动：preload → preload-settings.js，HTML 路径加 windows/settings/ 前缀
// 注意：settings 模式不调 Dock.hide（否则窗口不显示）
function createSettingsWindow() {
  settingsWindow = new BrowserWindow({
    width: 500,
    height: 650,
    resizable: false,
    frame: false,
    transparent: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload-settings.js'),
      contextIsolation: true,
      nodeIntegration: false
    }
  });

  settingsWindow.loadFile(path.join(__dirname, 'windows', 'settings', 'index.html'));

  // F12 打开开发者工具（调试用）+ macOS 编辑快捷键
  // 与 chat 窗口保持一致（参考 L181-198）
  settingsWindow.webContents.on('before-input-event', (event, input) => {
    if (input.key === 'F12') {
      settingsWindow.webContents.toggleDevTools();
    }
    // macOS: 确保编辑快捷键正常工作
    if (input.meta) {  // Cmd 键
      if (input.key === 'v') {
        settingsWindow.webContents.paste();
      } else if (input.key === 'c') {
        settingsWindow.webContents.copy();
      } else if (input.key === 'x') {
        settingsWindow.webContents.cut();
      } else if (input.key === 'a') {
        settingsWindow.webContents.selectAll();
      }
    }
  });

  settingsWindow.on('closed', () => {
    settingsWindow = null;
  });
}

// === Step 7: createGraphWindow（来自 ui/graph/main.js） ===
// 改动：preload → preload-graph.js，HTML 路径加 windows/graph/ 前缀
// 注意：显式调用 show() 激活窗口（原 ui/graph/main.js 没显式 show，但单进程时需要确保前台显示）
function createGraphWindow() {
  if (graphWindow && !graphWindow.isDestroyed()) {
    graphWindow.focus();
    return graphWindow;
  }

  graphWindow = new BrowserWindow({
    width: 1280,
    height: 800,
    minWidth: 800,
    minHeight: 600,
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload-graph.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  graphWindow.loadFile(path.join(__dirname, 'windows', 'graph', 'index.html'));
  graphWindow.show();

  graphWindow.on('closed', () => {
    graphWindow = null;
  });

  return graphWindow;
}

// === Step 8: createTray + open-graph 同进程化 ===
// 来自 ui/assistant/main.js
// 改动：托盘菜单"打开图谱"click 改为 createGraphWindow()（不再 spawn niu --graph）
function createTray() {
  // 创建托盘图标 - 使用头像
  const iconPath = path.join(__dirname, 'windows', 'assistant', 'icons', 'icon-16.png');
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
        // 同进程：直接创建 graph 窗口（不再 spawn niu --graph）
        if (graphWindow && !graphWindow.isDestroyed()) {
          graphWindow.focus();
        } else {
          createGraphWindow();
        }
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
          // Rust launcher 会检测到 Electron 退出并清理所有资源
          app.exit(0);
        }
      }
    }
  ]);

  tray.setContextMenu(contextMenu);

  // 点击托盘图标显示/隐藏小女孩
  tray.on('click', () => {
    if (spiritWindow && !spiritWindow.isDestroyed()) {
      if (spiritWindow.isVisible()) {
        spiritWindow.hide();
      } else {
        spiritWindow.show();
        spiritWindow.setAlwaysOnTop(true, 'floating'); // 确保置顶状态
      }
    }
  });
}

// === Step 9: 所有 IPC handler ===

// ---------- 来自 ui/assistant/main.js（33 个，含 open-graph 已改同进程） ----------

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

// 用系统默认浏览器打开链接（仅允许 http/https）
ipcMain.on('open-external', (event, url) => {
  if (!url) return;
  try {
    const parsed = new URL(url);
    if (parsed.protocol === 'http:' || parsed.protocol === 'https:') {
      shell.openExternal(url);
    } else {
      console.warn('[Main] Blocked non-HTTP external URL:', url);
    }
  } catch (e) {
    // Invalid URL, ignore
  }
});

// 用系统默认查看器打开文件
ipcMain.on('open-with-system-viewer', (event, filePath) => {
  if (!filePath) return;
  // marked.js 会将路径中的空格/括号等编码为 %XX，需要解码
  try { filePath = decodeURIComponent(filePath); } catch(e) {}
  // Validate: must be an existing local file with safe extension
  if (!fs.existsSync(filePath) || !fs.statSync(filePath).isFile()) {
    console.warn('[Main] open-with-system-viewer: path does not exist or is not a file:', filePath);
    return;
  }
  // Block UNC paths (network shares)
  if (filePath.startsWith('\\\\') || filePath.startsWith('//')) {
    console.warn('[Main] open-with-system-viewer: UNC paths not allowed:', filePath);
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
    console.warn('[Main] open-with-system-viewer: blocked executable extension:', ext);
    return;
  }
  shell.openPath(filePath).catch(err => {
    console.error('[Main] 打开文件失败:', err);
  });
});

// 获取图片显示 URL（本地路径转 file:// URL）
ipcMain.handle('get-image-url', async (event, filePath) => {
  if (!filePath) return null;
  // Use Node.js pathToFileURL — correctly handles #, spaces, Unicode
  try {
    return url.pathToFileURL(filePath).href;
  } catch {
    // Fallback for non-absolute paths
    const normalized = filePath.replace(/\\/g, '/');
    return 'file:///' + normalized;
  }
});

// 处理拖入的图片（调用后端 API）
ipcMain.handle('process-image', async (event, filePath) => {
  return new Promise((resolve) => {
    const isImage = /\.(jpg|jpeg|png|gif|bmp|webp|tiff?|heic|heif)$/i.test(filePath);
    const action = isImage ? '入库照片' : '入库文件';
    const data = JSON.stringify({
      session_id: config.chatSessionId || null,
      message: `${action}：${filePath.replace(/\\/g, '/')}`
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
      // No timeout — agent tasks can run indefinitely; user controls termination via Stop button
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
      // No timeout — agent tasks can run indefinitely; user controls termination via Stop button
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
    // 传递 resources：将拖入文件和模式信息以结构化方式传递给后端
    const resources = (context && context.files)
      ? context.files.map(f => ({
          path: (f.path || f).replace(/\\/g, '/'),
          mode: context.mode || 'copy'
        }))
      : undefined;
    const payload = { message: message };
    if (resources) {
      payload.resources = resources;
    }
    const data = JSON.stringify(payload);

    const req = http.request({
      hostname: '127.0.0.1',
      port: 9876,
      path: '/api/chat/session',
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(data)
      }
      // No timeout — agent tasks can run indefinitely; user controls termination via Stop button
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
    req.on('error', (e) => {
      resolve({ error: e.message });
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
  // 清空待推送消息队列
  pendingAlertMessages = [];

  return new Promise((resolve) => {
    const data = JSON.stringify({ sessionId: 'default' });
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
          console.log('清空聊天记录:', result);
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

// 打开图谱窗口（同进程：直接 createGraphWindow()，不再 spawn niu --graph）
ipcMain.on('open-graph', () => {
  if (graphWindow && !graphWindow.isDestroyed()) {
    graphWindow.focus();
  } else {
    createGraphWindow();
  }
});

// ---------- 来自 ui/settings/main.js（6 个） ----------

// Config paths：user-config.json 写到 ~/.niu/config/（bundle 内只读）
// llm-presets.json 只读，仍从 bundle 内读
const niuConfigDir = path.join(os.homedir(), '.niu', 'config');
if (!fs.existsSync(niuConfigDir)) {
  fs.mkdirSync(niuConfigDir, { recursive: true });
}
const bundleConfigDir = path.join(__dirname, '..', '..', 'config');
const userConfigPath = path.join(niuConfigDir, 'user-config.json');
const presetsPath = path.join(bundleConfigDir, 'llm-presets.json');  // 只读模板

// 注意：user-config.json 无模板文件设计——文件由设置窗口 save-config 创建。
// 文件不存在时（首次启动），get-config 返回代码内联标准缺省（与 config-manager
// load_user_config 兜底、settings testAndSave 缺省常量三处一致）——
// 保证表单初始值和 probe 探测都拿到完整基础配置（thinking/reasoning_effort 等），
// 而不是空骨架 {llm:{}}（2026-07-27 首次启动 probe 失败根因）。

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
  // 首次启动兜底：完整标准缺省（三处一致：本处 / testAndSave 常量 / config-manager 兜底）
  return {
    llm: {
      presetId: "", apiKey: "", apiBase: "", model: "", type: "openai",
      reasoning_effort: "",
      litellm_kwargs: {}
    },
    lightrag_llm: {
      presetId: "", apiKey: "", apiBase: "", model: "", type: "openai",
      reasoning_effort: "high", temperature: 0.2,
      litellm_kwargs: { thinking: { type: "disabled" }, allowed_openai_params: [] }
    },
    context: {
      contextWindowSize: 200000, warningThreshold: 0.8,
      compressTargetTokens: 60000, sleepTriggerMinutes: 5
    },
    storage: {},
    firstRun: true,
    logging: { enabled: false, level: "INFO" }
  };
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

ipcMain.handle('probe-response-format', async (event, config) => {
  // 通过 HTTP POST 调本机 127.0.0.1:9876/api/probe-response-format
  // 失败时返回 probe_failed，保留旧配置（不破坏用户已有 response_format_mode）
  try {
    const http = require('http');
    const payload = JSON.stringify(config);
    const options = {
      hostname: '127.0.0.1',
      port: parseInt(process.env.NIU_API_PORT || '9876', 10),  // 与 test-connection :1180 一致，支持 launcher --port 自定义
      path: '/api/probe-response-format',
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(payload),
      },
      timeout: 300000,  // 三次采样 + 限流/超时重试最坏耗时 ~250s/档（限流主导早返 ~160s），两档 ~500s。
                       // 正常场景 3 次采样 + 无重试约 90s/档。设 300s 覆盖正常+限流场景，病态连续
                       // 超时场景（~335s/档）前端会先放弃，属可接受取舍。
    };
    return await new Promise((resolve) => {
      const req = http.request(options, (res) => {
        let data = '';
        res.on('data', (chunk) => data += chunk);
        res.on('end', () => {
          try { resolve(JSON.parse(data)); }
          catch { resolve({ result: 'probe_failed', reason: '响应解析失败', mode: null, raw_response: data.slice(0, 200) }); }
        });
      });
      req.on('error', (e) => resolve({ result: 'probe_failed', reason: e.message, mode: null, raw_response: '' }));
      req.on('timeout', () => { req.destroy(); resolve({ result: 'probe_failed', reason: 'HTTP 超时', mode: null, raw_response: '' }); });
      req.write(payload);
      req.end();
    });
  } catch (e) {
    return { result: 'probe_failed', reason: String(e), mode: null, raw_response: '' };
  }
});

ipcMain.handle('close-window', () => {
  // 优先关 settings 窗口；若 assistant 模式下也调用此 IPC（虽然原 assistant 无此 handler），
  // 退化为关闭当前焦点窗口
  if (settingsWindow) {
    settingsWindow.close();
  } else if (graphWindow) {
    graphWindow.close();
  } else {
    const focused = BrowserWindow.getFocusedWindow();
    if (focused) focused.close();
  }
});

ipcMain.handle('minimize-window', () => {
  if (settingsWindow) {
    settingsWindow.minimize();
  } else if (graphWindow) {
    graphWindow.minimize();
  } else {
    const focused = BrowserWindow.getFocusedWindow();
    if (focused) focused.minimize();
  }
});

// ---------- 来自 ui/graph/main.js（12 个） ----------

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

ipcMain.handle('kg-search-entities', async (event, query, topK) => {
  const params = new URLSearchParams({ query: query || '', top_k: topK || 20 });
  return apiRequest('GET', `/api/kg/search_entities?${params}`);
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

// === Step 10: app.whenReady + 模式分支 ===
app.whenReady().then(() => {
  if (WINDOW_MODE === 'assistant') {
    // macOS: 隐藏 Dock 图标，只保留系统托盘图标
    // 仅 assistant 模式调（settings/graph 不调，否则窗口不显示）
    if (process.platform === 'darwin' && app.dock) {
      app.dock.hide();
    }

    createSpiritWindow();
    createChatWindow();
    createStickyWindow();
    if (shouldCreateTray()) createTray();

    // 启动 alerts 轮询（延迟 10s 等后端启动）
    setTimeout(() => {
      startPendingAlertsPolling();
      console.log('[Alerts] 开始轮询待推送消息');
    }, 10000);

    // 启动 SSE 事件流（延迟 5s 等后端启动）
    setTimeout(() => {
      startMessageEventStream();
      console.log('[SSE] Starting message event stream');
    }, 5000);
  } else if (WINDOW_MODE === 'settings') {
    createSettingsWindow();
  } else if (WINDOW_MODE === 'graph') {
    createGraphWindow();
  } else {
    console.error('Unknown NIU_WINDOW:', WINDOW_MODE);
    app.quit();
  }
});

// === Step 11: app.on 事件 ===

app.on('window-all-closed', () => {
  // assistant 模式：保持托盘，不退出（Windows/Linux）
  // settings/graph 模式：所有窗口关闭即退出
  if (WINDOW_MODE !== 'assistant') {
    app.quit();
  }
});

app.on('before-quit', () => {
  // 仅 assistant 模式管理 Python API 生命周期（调 /api/shutdown + destroy 全部窗口 + tray）
  // settings/graph 模式不管理 Python API 生命周期
  if (WINDOW_MODE === 'assistant') {
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
    if (stickyWindow) stickyWindow.destroy();
    if (graphWindow) graphWindow.destroy();
    if (tray) tray.destroy();
  }
});

app.on('activate', () => {
  // 来自原 ui/graph/main.js：graph 模式下窗口全关后重新创建
  if (WINDOW_MODE === 'graph' && !graphWindow) createGraphWindow();
});

// ========== macOS 菜单（来自原 ui/assistant/main.js） ==========
if (process.platform === 'darwin') {
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

// ========== 定时提醒轮询（来自原 ui/assistant/main.js） ==========
let pendingAlertMessages = [];
let alertsPollingTimer = null;  // 保存定时器引用

// 轮询待推送消息
function startPendingAlertsPolling() {
  alertsPollingTimer = setInterval(async () => {
    try {
      const alerts = await fetchPendingAlerts();
      if (alerts && alerts.length > 0) {
        // 直接触发小女孩蹦高，不判断任何条件
        if (spiritWindow && !spiritWindow.isDestroyed()) {
          spiritWindow.webContents.send('alert', '⏰');
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

// 获取当前 Agent 是否忙碌（前端窗口恢复时同步停止按钮状态）
ipcMain.handle('get-chat-status', async () => {
  return new Promise((resolve) => {
    http.get('http://127.0.0.1:' + (process.env.NIU_API_PORT || '9876') + '/api/chat/status', (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => {
        try {
          resolve(JSON.parse(data));
        } catch (e) {
          resolve({ busy: false });
        }
      });
    }).on('error', (e) => {
      console.error('获取聊天状态失败:', e.message);
      resolve({ busy: false });
    });
  });
});

// ========== SSE 消息事件流（来自原 ui/assistant/main.js） ==========
let sseReconnectTimer = null;
let sseConnectedBefore = false;

function startMessageEventStream() {
  if (sseReconnectTimer) {
    clearTimeout(sseReconnectTimer);
    sseReconnectTimer = null;
  }

  const options = {
    hostname: '127.0.0.1',
    port: 9876,
    path: '/api/events/stream',
    method: 'GET',
    headers: { 'Accept': 'text/event-stream' }
  };

  const req = http.request(options, (res) => {
    let buffer = '';
    res.setEncoding('utf8');  // 正确处理多字节字符跨 TCP 块分割
    console.log('[SSE] Connected to message event stream');

    // 通知后端 frontend_ready（首次连接才通知，重连不重复通知）
    // scheduler _delayed_start 等此通知才扫描过期任务
    if (!sseConnectedBefore) {
      const readyReq = http.request({
        hostname: '127.0.0.1',
        port: 9876,
        path: '/api/frontend-ready',
        method: 'POST',
      }, () => {});
      readyReq.on('error', (e) => console.warn('[FRONTEND_READY] notify failed:', e.message));
      readyReq.end();
    }

    // 重连时通知 chat 刷新（chat 的 onNewMessage 会触发 refreshFromDB）
    if (sseConnectedBefore && chatWindow && !chatWindow.isDestroyed()) {
      chatWindow.webContents.send('new-message');
    }
    sseConnectedBefore = true;

    res.on('data', (chunk) => {
      buffer += chunk.toString();
      // 解析 SSE data: 行
      const lines = buffer.split('\n');
      buffer = lines.pop(); // 保留不完整的行
      for (const line of lines) {
        if (line.startsWith('data: ')) {
          const jsonStr = line.slice(6);
          if (!jsonStr) continue;  // 跳过空 data 行
          try {
            const event = JSON.parse(jsonStr);
            if (event.type === 'new_message') {
              // 通知 chat 有新消息（传递 role/content/source 字段，用于 chat_busy/chat_idle 状态机控制 + ask_main_agent 跨进程转发）
              if (chatWindow && !chatWindow.isDestroyed()) {
                chatWindow.webContents.send('new-message', {
                  role: event.role,
                  content: event.content,
                  source: event.source
                });
              }
            } else if (event.type === 'tool_status') {
              // 转发工具调用状态到聊天窗口
              if (chatWindow && !chatWindow.isDestroyed()) {
                chatWindow.webContents.send('tool-status', event);
              }
            } else if (event.type === 'compact_status') {
              // 转发上下文压缩状态到聊天窗口
              if (chatWindow && !chatWindow.isDestroyed()) {
                chatWindow.webContents.send('compact-status', event);
              }
            } else if (event.type === 'ingest-started' || event.type === 'ingest-completed') {
              // 转发入库进度事件到 spirit 和 chat 窗口
              if (spiritWindow && !spiritWindow.isDestroyed()) {
                spiritWindow.webContents.send(event.type);
              }
              if (chatWindow && !chatWindow.isDestroyed()) {
                chatWindow.webContents.send(event.type);
              }
            }
          } catch (e) {
            // 忽略解析错误（可能是心跳等非 JSON 行）
          }
        }
      }
    });

    res.on('end', () => {
      console.log('[SSE] Connection closed, reconnecting in 3s');
      sseReconnectTimer = setTimeout(startMessageEventStream, 3000);
    });
  });

  req.on('error', (e) => {
    console.log('[SSE] Connection error, reconnecting in 3s:', e.message);
    sseReconnectTimer = setTimeout(startMessageEventStream, 3000);
  });

  req.end();
}
