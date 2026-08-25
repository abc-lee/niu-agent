// === Step 2: 入口 + 模式分支 ===
// 单一 Electron 入口：按 NIU_WINDOW 环境变量分支创建对应 BrowserWindow
//   - assistant: spirit + chat + sticky + tray + SSE 轮询 + Dock.hide(macOS)
//   - settings:  只创 settings 窗口（不调 Dock.hide）
//   - graph:     只创 graph 窗口（不调 Dock.hide）
// 合并自 ui/assistant/main.js + ui/settings/main.js + ui/graph/main.js（零命名冲突）
const { app, BrowserWindow, ipcMain, screen, Tray, Menu, nativeImage, dialog, shell, powerMonitor } = require('electron');
const path = require('path');
const fs = require('fs');
const os = require('os');
const url = require('url');
const http = require('http');

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
let allowQuit = false;       // macOS Cmd+Q 拦截：明确退出路径（菜单点击/close-all/系统关机 powerMonitor）才置 true

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
      nodeIntegration: false,
      sandbox: false  // 让 preload 能 require 自定义模块（font-config.js），与 spirit 一致
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
    SubagentSSEManager.disconnectAll();  // 用户关闭窗口：断开所有子 Agent SSE 连接
    chatWindow = null;
    // P2-3b：窗口在 ask_user 等待期间关闭（卡片已渲染后）——主 Agent 会一直阻塞 600s，
    // 回执 UNAVAILABLE 让 do_ask_user 走错误分支；无 pending ask 时端点返回 no pending ask，无害
    apiRequest('POST', '/api/chat/ask-answer', { answer: '__UNAVAILABLE__' })
      .catch((e) => console.error('[ask_user] report-unavailable-on-close failed', e));
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
      nodeIntegration: false,
      sandbox: false  // 让 preload 能 require 自定义模块（font-config.js），与 spirit 一致
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
    width: 1020,
    height: 750,
    minWidth: 960,
    minHeight: 600,
    resizable: true,
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

  // F12 打开开发者工具（调试用）
  // macOS: 确保编辑快捷键正常工作（与 chat/settings 窗口保持一致，参考 chatWindow 的 before-input-event）
  graphWindow.webContents.on('before-input-event', (event, input) => {
    if (input.key === 'F12') {
      graphWindow.webContents.toggleDevTools();
    }
    if (input.meta) {  // Cmd 键（macOS）；Windows 上 Chromium 原生处理 Ctrl+C/V，不在此拦截
      if (input.key === 'v') {
        graphWindow.webContents.paste();
      } else if (input.key === 'c') {
        graphWindow.webContents.copy();
      } else if (input.key === 'x') {
        graphWindow.webContents.cut();
      } else if (input.key === 'a') {
        graphWindow.webContents.selectAll();
      }
    }
  });

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
ipcMain.handle('get-stats', async (event, agentName) => {
  return new Promise((resolve, reject) => {
    const requestPath = agentName ? '/api/stats?agent=' + encodeURIComponent(agentName) : '/api/stats';
    const req = http.request({
      hostname: '127.0.0.1',
      port: 9876,
      path: requestPath,
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
  allowQuit = true;  // 明确退出意图（前端 closeAll / 未来调用），放行 before-quit 守卫
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
  // 转发到后端（整理管道睡眠状态机读取；fire-and-forget，失败吞掉）
  fetch('http://127.0.0.1:' + (process.env.NIU_API_PORT || 9876) + '/api/spirit-state', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ state })
  }).catch(() => {});
});

// 接收聊天窗口的忙碌状态通知，转发给小女孩窗口
ipcMain.on('notify-busy', (event, { isBusy, reason }) => {
  if (spiritWindow && !spiritWindow.isDestroyed()) {
    spiritWindow.webContents.send('busy-state', isBusy, reason);
  }
});

// 接收聊天窗口的忙碌状态重置通知，转发给小女孩窗口（SSE 断连修复）
ipcMain.on('reset-busy', () => {
  if (spiritWindow && !spiritWindow.isDestroyed()) {
    spiritWindow.webContents.send('reset-busy');
  }
});

// 接收聊天窗口的用户活动通知，转发给小女孩窗口（重置空闲计时器）
ipcMain.on('notify-activity', () => {
  if (spiritWindow && !spiritWindow.isDestroyed()) {
    spiritWindow.webContents.send('user-activity');
  }
});

// 接收聊天窗口的睡眠指令，转发给小女孩窗口（/sleep 命令：触发精灵 setState(SLEEP)）
ipcMain.on('enter-sleep', () => {
  if (spiritWindow && !spiritWindow.isDestroyed()) {
    spiritWindow.webContents.send('enter-sleep');
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
      message: `${action}：${filePath.replace(/\\/g, '/')}`,
      source: 'electron'
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

ipcMain.handle('send-message', async (event, message, source) => {
  return new Promise((resolve) => {
    // Load session ID from config
    const data = JSON.stringify({ message: message, source: source !== undefined ? source : 'electron' });

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
    const payload = { message: message, source: 'electron' };
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
ipcMain.handle('clear-chat', async (event) => {
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

// 触发上下文整理（/compact 命令：调后端 /api/context/tidy，T6 后仅 compact 直达机械压实）
ipcMain.handle('tidy-context', async (event, mode) => {
  return new Promise((resolve) => {
    const data = JSON.stringify({ session_id: 'default', mode: mode || 'compact' });
    const req = http.request({
      hostname: '127.0.0.1',
      port: 9876,
      path: '/api/context/tidy',
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
          console.log('[Tidy] 上下文整理:', result);
          resolve(result);
        } catch (e) {
          resolve({ status: 'error', error: '解析响应失败' });
        }
      });
    });
    req.on('error', (e) => {
      console.error('[Tidy] 上下文整理失败:', e.message);
      resolve({ status: 'error', error: e.message });
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

// 打开设置窗口（同进程：直接 createSettingsWindow()）
ipcMain.on('open-settings', () => {
  // 设置窗口已存在 → 聚焦，不创建新窗口
  if (settingsWindow) {
    settingsWindow.focus();
    return;
  }
  createSettingsWindow();
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
      reasoning_effort: "", temperature: 0.2,
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

    // 配置热更新（免重启）：通知后端清除 LLM 缓存（/api/config/reload）。
    // fire-and-forget——后端不可达时忽略（niu --settings 独立模式），
    // 后端侧惰性重载兜底（chat.py get_or_create_runner 配置比对 +
    // lightrag_manager config_key 自动重建）。
    try {
      const http = require('http');
      const port = parseInt(process.env.NIU_API_PORT || '9876');
      const req = http.request({
        hostname: '127.0.0.1', port,
        path: '/api/config/reload', method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Content-Length': 2 },
        timeout: 5000
      });
      req.on('error', () => { /* 后端不可达——惰性重载兜底 */ });
      req.on('timeout', () => { req.destroy(); });
      req.write('{}');
      req.end();
    } catch (e) { /* 通知失败不阻塞保存 */ }

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
        // 后端 test-llm read_timeout=120 + wait_for=150（llm_ready resolve_probe_budget，
        // llm.read_timeout 可覆盖至 190/220）——前端 socket 必须大于 220s（推理模型 20-120s 显式支持）
        timeout: 230000  // 230s — 对齐后端 /api/test-llm wait_for 上限 220s（llm.read_timeout 可覆盖至 190）
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
          // 推理模型首响应 20-120s，对齐主路径 230s（后端 wait_for 上限 220s）
          timeout: 230000  // 230s — 对齐后端 /api/test-llm wait_for 上限 220s（llm.read_timeout 可覆盖至 190）
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
          // 推理模型首响应 20-120s，对齐主路径 230s（后端 wait_for 上限 220s）
          timeout: 230000  // 230s — 对齐后端 /api/test-llm wait_for 上限 220s（llm.read_timeout 可覆盖至 190）
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
      timeout: 120000,  // probe read_timeout 10s + 重试 2 次，单档最坏 3×10s+退避 15s=45s，
                       // 两档 90s。豆包网关挂起场景快速失败，用户最多等 2 分钟。
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

// 能力探测档案路径：~/.niu/model_capabilities.json（与 niu_api/model_probe.py
// default_profile_path 一致；键 = api_base|model|llm / api_base|model|lightrag，
// api_base 规范化 rstrip("/")）
const capabilityProfilePath = path.join(os.homedir(), '.niu', 'model_capabilities.json');

ipcMain.handle('get-capability-profile', (event, params) => {
  // params: { apiBase, model, lightrag } → 返回该 api_base|model|场景 键的能力档案（无 → null）
  try {
    const { apiBase, model, lightrag } = params || {};
    if (!apiBase || !model) return null;
    if (!fs.existsSync(capabilityProfilePath)) return null;
    const data = JSON.parse(fs.readFileSync(capabilityProfilePath, 'utf-8'));
    const key = `${String(apiBase).replace(/\/+$/, '')}|${model}|${lightrag ? 'lightrag' : 'llm'}`;
    return data[key] || null;
  } catch (e) {
    console.error('Failed to read capability profile:', e);
    return null;
  }
});

ipcMain.handle('probe-capability', async (event, config) => {
  // POST 本机 127.0.0.1:9876/api/model-capability-probe（settings "探测能力"按钮）
  // body = llm 段或 lightrag 段配置（lightrag 段带顶层 lightrag:true）
  // socket 超时对齐 test-connection 230s（后端探测全程预算 ≈110s，留足余量）
  try {
    const http = require('http');
    const payload = JSON.stringify(config || {});
    const options = {
      hostname: '127.0.0.1',
      port: parseInt(process.env.NIU_API_PORT || '9876', 10),  // 与 test-connection 一致，支持 launcher --port 自定义
      path: '/api/model-capability-probe',
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(payload),
      },
      timeout: 230000,  // 230s — 对齐 test-connection /api/test-llm wait_for 上限 220s
    };
    return await new Promise((resolve) => {
      const req = http.request(options, (res) => {
        let data = '';
        res.on('data', (chunk) => data += chunk);
        res.on('end', () => {
          try { resolve(JSON.parse(data)); }
          catch { resolve({ probe_status: 'failed', error: '响应解析失败', raw_response: data.slice(0, 200) }); }
        });
      });
      req.on('error', (e) => resolve({ probe_status: 'failed', error: e.message }));
      req.on('timeout', () => { req.destroy(); resolve({ probe_status: 'failed', error: 'HTTP 超时（探测超过 230s）' }); });
      req.write(payload);
      req.end();
    });
  } catch (e) {
    return { probe_status: 'failed', error: String(e) };
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

// Brain region panel IPC handlers
ipcMain.handle('brain-regions', async () => {
  return apiRequest('GET', '/api/brain/regions?include_dark=true');
});

ipcMain.handle('brain-update', async (event, regions) => {
  return apiRequest('POST', '/api/brain/regions/update', { regions });
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

    // macOS 系统关机/注销：放行退出（否则 before-quit 守卫会 preventDefault，macOS 弹强制退出对话框打断关机）
    // powerMonitor 'shutdown' 事件平台：Linux/macOS（Electron 官方文档）；注销/关机/重启均触发
    // e.preventDefault()：按 Electron 文档建议延迟系统关机，等待 app 干净退出（/api/shutdown + 窗口销毁）
    powerMonitor.on('shutdown', (e) => {
      e.preventDefault();
      allowQuit = true;
      app.quit();
    });

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

app.on('before-quit', (e) => {
  // macOS Cmd+Q 拦截（2026-08-10）：assistant 模式（精灵/Chat/图谱同进程）拒绝非明确退出的 quit
  // darwin 门控：Windows/Linux 无 Cmd+Q 问题，守卫完全不生效（零语义改动）
  // 守卫覆盖面：一切未置 allowQuit 的 quit（Cmd+Q 已无 accelerator 绑定、AppleScript `quit app`、
  // 系统 quit、未来新增 app.quit() 路径）。系统关机/注销由 powerMonitor 'shutdown' 先置 allowQuit 放行。
  // allowQuit 是闩锁（置位后不复位）：明确退出（菜单点击/close-all/shutdown）中途被中止时，
  // 后续非明确 quit 可穿透守卫——当前无窗口 close 拦截（无现实中止路径），若未来加 beforeunload
  // preventDefault 需同步考虑复位语义。
  if (process.platform === 'darwin' && WINDOW_MODE === 'assistant' && !allowQuit) {
    e.preventDefault();
    console.log('[main] quit blocked (unlatched quit: AppleScript/system?). Use tray "⛔ 关闭妞妞" or menu 退出 to quit.');
    return;
  }
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
          // assistant 模式：无 accelerator（Cmd+Q 不触发退出，2026-08-10）
          // settings/graph 模式：保留 Cmd+Q（macMenu 为全模式共享，需按模式区分）
          accelerator: WINDOW_MODE === 'assistant' ? undefined : 'Cmd+Q',
          click: () => {
            allowQuit = true;
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
        if (spiritWindow && !spiritWindow.isDestroyed()) {
          // 每条 alert 都发送，spirit 端 setState 有守卫（已 ALERT 不重复）
          alerts.forEach(a => {
            const content = (a && a.content) ? a.content : '⏰';
            spiritWindow.webContents.send('alert', content);
          });
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

// ===== 子 Agent IPC handlers =====
// 子 Agent SSE 连接管理（窗口恢复时前端主动请求建立连接）
ipcMain.on('connect-subagent-sse', (_event, uniqueName) => {
  SubagentSSEManager.connect(uniqueName);
});
ipcMain.on('disconnect-subagent-sse', (_event, uniqueName) => {
  SubagentSSEManager.disconnect(uniqueName);
});

// 发送消息到子 Agent（用户补充信息 / /stop）
ipcMain.handle('send-subagent-message', async (_event, { uniqueName, message }) => {
  const apiPort = parseInt(process.env.NIU_API_PORT || '9876', 10);
  return new Promise((resolve) => {
    const data = JSON.stringify({ content: message });
    const req = http.request({
      hostname: '127.0.0.1', port: apiPort,
      path: `/api/subagents/${encodeURIComponent(uniqueName)}/message`,
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(data) }
    }, (res) => {
      let body = '';
      res.setEncoding('utf8');
      res.on('data', c => body += c);
      res.on('end', () => {
        try {
          resolve(JSON.parse(body));
        } catch (e) {
          resolve({ status: res.statusCode, raw: body });
        }
      });
    });
    req.on('error', (e) => resolve({ error: e.message }));
    req.write(data);
    req.end();
  });
});

// ========== SSE 消息事件流（来自原 ui/assistant/main.js） ==========
let sseReconnectTimer = null;
let sseConnectedBefore = false;

// ===== 子 Agent SSE 管理器 =====
// 每个运行中的子 Agent 一条独立 SSE 连接（/api/subagents/{unique_name}/stream）
// 模块级定义（startMessageEventStream 之前），确保 chatWindow.on('closed') 能引用
const SubagentSSEManager = {
  connections: {},  // { unique_name: { req, cancelled } }

  connect(uniqueName) {
    if (this.connections[uniqueName]) return;  // 已连接
    const conn = { req: null, cancelled: false, reconnectTimer: null };
    this.connections[uniqueName] = conn;
    this._connect(uniqueName, conn);
  },

  _connect(uniqueName, conn) {
    const apiPort = parseInt(process.env.NIU_API_PORT || '9876', 10);
    const req = http.get(`http://127.0.0.1:${apiPort}/api/subagents/${encodeURIComponent(uniqueName)}/stream`, (res) => {
      if (res.statusCode === 404) {
        // 子 Agent 不存在，不重连，销毁 req 释放资源
        conn.cancelled = true;  // 阻止 req.destroy() 触发的 error/end 回调安排重连
        delete this.connections[uniqueName];
        req.destroy();
        if (chatWindow && !chatWindow.isDestroyed()) {
          chatWindow.webContents.send('subagent-event', { unique_name: uniqueName, event: { type: 'subagent_closed', content: '子 Agent 不存在或已结束' } });
        }
        return;
      }
      res.setEncoding('utf8');  // 正确处理中文多字节字符跨 TCP 块分割
      let buffer = '';
      res.on('data', (chunk) => {
        buffer += chunk;
        const lines = buffer.split('\n');
        buffer = lines.pop();  // 保留不完整的行
        for (const line of lines) {
          if (line.startsWith('data: ')) {
            const jsonStr = line.slice(6);
            if (!jsonStr) continue;
            try {
              const event = JSON.parse(jsonStr);
              if (chatWindow && !chatWindow.isDestroyed()) {
                chatWindow.webContents.send('subagent-event', { unique_name: uniqueName, event });
              }
              if (event.type === 'subagent_closed') {
                this.disconnect(uniqueName);
              }
            } catch (e) {}
          }
        }
      });
      res.on('end', () => {
        // cancelled 标志：req.destroy() 触发的 end/error 回调不重连
        if (!conn.cancelled) {
          conn.reconnectTimer = setTimeout(() => this._reconnect(uniqueName, conn), 3000);
        }
      });
    });
    req.on('error', () => {
      if (!conn.cancelled) {
        conn.reconnectTimer = setTimeout(() => this._reconnect(uniqueName, conn), 3000);
      }
    });
    conn.req = req;
  },

  _reconnect(uniqueName, conn) {
    if (conn.cancelled) return;
    this._connect(uniqueName, conn);
    // 通知前端连接已恢复（ring buffer 会补发历史事件）
    // 404 路径会 delete connection，此时不发 reconnected（已发 subagent_closed）
    if (this.connections[uniqueName] && chatWindow && !chatWindow.isDestroyed()) {
      chatWindow.webContents.send('subagent-event', { unique_name: uniqueName, event: { type: 'reconnected' } });
    }
  },

  disconnect(uniqueName) {
    const conn = this.connections[uniqueName];
    if (!conn) return;
    conn.cancelled = true;
    if (conn.reconnectTimer) { clearTimeout(conn.reconnectTimer); conn.reconnectTimer = null; }
    if (conn.req) { conn.req.destroy(); conn.req = null; }
    delete this.connections[uniqueName];
  },

  disconnectAll() {
    for (const name of Object.keys(this.connections)) {
      this.disconnect(name);
    }
  }
};

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

    // 重连时通知 chat 刷新 + 同步忙碌状态（chat_idle 可能因断连丢失）
    if (sseConnectedBefore && chatWindow && !chatWindow.isDestroyed()) {
      chatWindow.webContents.send('new-message');
      chatWindow.webContents.send('sync-state');
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
              // 用户发消息时取消 spirit 的 ALERT 状态
              // 用户发消息代表已看到报警内容，无论本地还是飞书都应取消
              if (event.role === 'user' && spiritWindow && !spiritWindow.isDestroyed()) {
                spiritWindow.webContents.send('cancel-alert');
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
            } else if (event.type === 'subagent_started') {
              // 子 Agent 启动通知（顶级事件类型，非 new_message 的 role 字段）
              if (chatWindow && !chatWindow.isDestroyed()) {
                chatWindow.webContents.send('subagent-started', event);
              }
              // 建立该子 Agent 的独立 SSE 连接
              SubagentSSEManager.connect(event.unique_name);
            } else if (event.type === 'brain_region_updated') {
              // 转发脑区状态变更到聊天窗口（脑区面板在 chat.html 中）
              if (chatWindow && !chatWindow.isDestroyed()) {
                chatWindow.webContents.send('brain-regions-changed', event);
              }
            } else if (event.type === 'llm_error') {
              // 转发 LLM 调用错误到聊天窗口（⚠️ system 提示，刷新消失——不落库）
              if (chatWindow && !chatWindow.isDestroyed()) {
                chatWindow.webContents.send('llm-error', event);
              }
            } else if (event.type === 'system_notice') {
              // 转发系统提示（E4-02 强制退出等）到聊天窗口（⚠️ system 提示，刷新消失——不落库）
              if (chatWindow && !chatWindow.isDestroyed()) {
                chatWindow.webContents.send('system-notice', event);
              }
            } else if (event.type === 'mcp_load_failures') {
              // 转发 MCP 服务器加载失败状态槽到聊天窗口
              // （SSE 连接建立时服务端随连接响应返回，每连接一次——简单提示，不落库；
              //   服务端保留状态槽至下次加载周期，窗口后开时由轮询路径补拉）
              if (chatWindow && !chatWindow.isDestroyed()) {
                chatWindow.webContents.send('mcp-load-failures', event);
              }
            } else if (event.type === 'ask_user') {
              // R4-A P1-4：聊天窗口关闭时（托盘/scheduler 轮）——spirit 窗口无 ask-user 监听（preload-assistant.js
              // 无 onAskUser），转发是死路。无可渲染窗口时**立即回执后端**注入不可用标记，
              // 让 do_ask_user 走错误分支（不静默阻塞 600s）
              // P2-3a：renderer 未就绪/正在 reload（webContents.isLoading()）时 send 会被无监听页面静默丢弃——
              // 等同不可渲染，回执 UNAVAILABLE（而非假转发）
              if (chatWindow && !chatWindow.isDestroyed() && !chatWindow.webContents.isLoading()) {
                chatWindow.webContents.send('ask-user', event);
              } else {
                console.warn('[ask_user] no renderable chat window — reporting unavailable to backend', event.content);
                apiRequest('POST', '/api/chat/ask-answer', { answer: '__UNAVAILABLE__' })
                  .catch((e) => console.error('[ask_user] report-unavailable failed', e));
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
