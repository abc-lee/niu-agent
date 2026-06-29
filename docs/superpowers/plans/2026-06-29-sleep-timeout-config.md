# 睡眠触发时间配置生效修复实施计划（v2 — preload 注入方案）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 让 `spirit.html` 的 IDLE_TIMEOUT 从 `config/user-config.json` 的 `context.sleepTriggerMinutes` 读取，用户修改配置后实际生效。

**Architecture:** 在 preload.js 中同步读取 user-config.json，用 `contextBridge.exposeInMainWorld('IDLE_TIMEOUT', ms)` 暴露给渲染进程。preload 在页面脚本前执行（Electron 官方保证），零时序风险。spirit.html 读取 `window.IDLE_TIMEOUT`，回退默认 5 分钟。

**Tech Stack:** Electron, preload contextBridge, Node.js fs

---

## 问题分析

### 当前状态
- `config/user-config.json:30` 用户设 `sleepTriggerMinutes: 30`
- `ui/assistant/spirit.html:165` 硬编码 `IDLE_TIMEOUT = 5 * 60 * 1000`
- 配置链路断裂：Rust 读取后丢弃、Python 不读取、前端硬编码

### 为什么用 preload + contextBridge（方案P1）

之前考虑过 `did-finish-load + executeJavaScript` 注入，但审查发现致命时序缺陷：
- Electron 官方文档：`did-finish-load` 在 `onload` 之后触发
- spirit.html 内联脚本在 HTML 解析阶段执行（远早于 onload）
- 注入的值永远不会被已执行的 `const IDLE_TIMEOUT` 读取

preload 方案的优势：
- preload 脚本在渲染进程 web content 加载前执行（官方保证）
- `contextBridge.exposeInMainWorld` 暴露的值在页面脚本运行前就可用
- 零时序风险，改动小（不用改 main.js）
- 符合 spirit.html:366-367 "渲染进程不直接读文件，一律走 IPC" 的原则（preload 属于主进程侧代理）

### 设计原则
- preload 有 Node.js fs 权限（`nodeIntegration: false` 不影响 preload 的 Node 访问）
- `contextIsolation: true` 下，preload 不能直接 `window.X = ...`，必须用 contextBridge
- 同步 `fs.readFileSync` 阻塞渲染进程启动，但 user-config.json <1KB，可忽略

---

### Task 1: preload.js 读取配置并暴露 IDLE_TIMEOUT

**Files:**
- Modify: `ui/assistant/preload.js`

- [ ] **Step 1: 读取当前 preload.js 结构**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && cat ui/assistant/preload.js | head -70`

确认 preload.js 的结构：
- 顶部 require（fs、path、contextBridge、ipcRenderer）
- `contextBridge.exposeInMainWorld('electronAPI', {...})` 的位置

- [ ] **Step 2: 在 preload.js 顶部添加同步读取配置的逻辑**

搜索 `contextBridge.exposeInMainWorld` 定位（约 preload.js 第3行）。在其**之前**添加读取配置的代码：

```javascript
// 同步读取睡眠触发时间配置（preload 在页面脚本前执行，零时序风险）
let _idleTimeoutMs = 5 * 60 * 1000;  // 默认 5 分钟
try {
  const fs = require('fs');
  const path = require('path');
  const userConfigPath = path.join(__dirname, '..', '..', 'config', 'user-config.json');
  const raw = fs.readFileSync(userConfigPath, 'utf-8');
  const cfg = JSON.parse(raw);
  const minutes = cfg?.context?.sleepTriggerMinutes;
  if (typeof minutes === 'number' && minutes > 0) {
    _idleTimeoutMs = minutes * 60 * 1000;
  }
} catch (e) {
  // 读取失败用默认值
}
```

说明：
- 路径 `path.join(__dirname, '..', '..', 'config', 'user-config.json')` 与 `ui/settings/main.js:6-7` 一致
- `__dirname` 在 preload 中是 `ui/assistant/` 目录
- 默认 5 分钟，与原硬编码一致
- 校验 `minutes > 0` 避免 0 或负数导致 setTimeout 立即触发
- require 在 preload 顶部已有（如果 preload.js 顶部已 require fs/path，复用即可；否则在 try 块内 require 也可）

- [ ] **Step 3: 在 contextBridge.exposeInMainWorld 中暴露 IDLE_TIMEOUT**

搜索 `contextBridge.exposeInMainWorld('electronAPI'` 定位。在暴露的对象中新增 `IDLE_TIMEOUT` 字段。

当前代码（约 preload.js 第3-65行）结构类似：
```javascript
contextBridge.exposeInMainWorld('electronAPI', {
  // ... 现有接口
});
```

在暴露对象中新增一行（建议放在对象开头或末尾，与其他接口一起）：
```javascript
contextBridge.exposeInMainWorld('electronAPI', {
  IDLE_TIMEOUT: _idleTimeoutMs,  // 睡眠触发时间（毫秒），从 user-config.json 读取
  // ... 现有接口
});
```

说明：
- 暴露为 `window.electronAPI.IDLE_TIMEOUT`
- 是只读值（contextBridge 暴露的原始类型值是拷贝，渲染进程无法修改主进程的值）

- [ ] **Step 4: 验证语法**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && node -c ui/assistant/preload.js && echo "syntax OK"`

Expected: `syntax OK`

- [ ] **Step 5: 验证配置读取**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && node -e "
const fs = require('fs');
const path = require('path');
let _idleTimeoutMs = 5 * 60 * 1000;
try {
  const userConfigPath = path.join('ui/assistant', '..', '..', 'config', 'user-config.json');
  const raw = fs.readFileSync(userConfigPath, 'utf-8');
  const cfg = JSON.parse(raw);
  const minutes = cfg?.context?.sleepTriggerMinutes;
  if (typeof minutes === 'number' && minutes > 0) {
    _idleTimeoutMs = minutes * 60 * 1000;
  }
} catch (e) {}
console.log('IDLE_TIMEOUT ms:', _idleTimeoutMs, '(', _idleTimeoutMs/60000, 'minutes)');
"
```

Expected: `IDLE_TIMEOUT ms: 1800000 ( 30 minutes )`（用户当前配置30分钟）

- [ ] **Step 6: 临时提交**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add ui/assistant/preload.js
git commit -m "feat(ui): preload.js reads sleepTriggerMinutes and exposes IDLE_TIMEOUT via contextBridge"
```

---

### Task 2: spirit.html 读取暴露的配置

**Files:**
- Modify: `ui/assistant/spirit.html`

- [ ] **Step 1: 确认当前 IDLE_TIMEOUT 定义**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && grep -n "IDLE_TIMEOUT" ui/assistant/spirit.html`

确认第 165 行的硬编码定义。

- [ ] **Step 2: 修改 IDLE_TIMEOUT 读取暴露值**

当前代码（第 165 行）：
```javascript
const IDLE_TIMEOUT = 5 * 60 * 1000;
```

改为：
```javascript
const IDLE_TIMEOUT = (window.electronAPI && window.electronAPI.IDLE_TIMEOUT) || (5 * 60 * 1000);
```

说明：
- `window.electronAPI.IDLE_TIMEOUT` 由 preload 在页面脚本前注入
- 回退到 5 分钟默认值（preload 失败或未注入时）
- 不需要改动 `startIdleTimer()`（第 334-339 行），它读取的 `IDLE_TIMEOUT` 变量已经是正确值
- 不需要改动初始化区（第 666-669 行），仍是同步执行

- [ ] **Step 3: 临时提交**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add ui/assistant/spirit.html
git commit -m "feat(ui): spirit.html reads IDLE_TIMEOUT from electronAPI (injected by preload)"
```

---

### Task 3: 端到端验证（需启动程序）

**Files:**
- 无文件修改，纯验证

- [ ] **Step 1: 快速验证（临时改配置为1分钟）**

临时修改 `config/user-config.json` 的 `sleepTriggerMinutes` 为 1：
```bash
cd REDACTED_USER_PATH/tools/ai-bot && python3 -c "
import json
with open('config/user-config.json') as f:
    cfg = json.load(f)
cfg['context']['sleepTriggerMinutes'] = 1
with open('config/user-config.json', 'w') as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
print('临时改为1分钟')
"
```

- [ ] **Step 2: 启动程序验证**

用户执行：
1. 启动 `./niu`
2. 等 spirit 窗口出现
3. 不操作等待 1 分钟
4. 确认 1 分钟后触发睡眠（而非 5 分钟）

- [ ] **Step 3: 恢复用户配置**

验证成功后恢复原配置：
```bash
cd REDACTED_USER_PATH/tools/ai-bot && python3 -c "
import json
with open('config/user-config.json') as f:
    cfg = json.load(f)
cfg['context']['sleepTriggerMinutes'] = 30
with open('config/user-config.json', 'w') as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
print('恢复为30分钟')
"
```

---

## 自审检查

### 1. Spec 覆盖
- preload.js 读取配置 → Task 1 Step 2 ✅
- preload.js 暴露 IDLE_TIMEOUT → Task 1 Step 3 ✅
- spirit.html 读取暴露值 → Task 2 Step 2 ✅
- 回退默认值 → Task 1 Step 2 + Task 2 Step 2 ✅
- 端到端验证 → Task 3 ✅

### 2. Placeholder 扫描
无 TBD/TODO。所有步骤包含具体代码。

### 3. 类型一致性
- `_idleTimeoutMs` 是 number（毫秒）
- `contextBridge.exposeInMainWorld('IDLE_TIMEOUT', _idleTimeoutMs)` 暴露 number
- spirit.html `window.electronAPI.IDLE_TIMEOUT` 是 number（毫秒）
- 单位一致（读取时已 `* 60 * 1000` 转为毫秒）

### 4. 时序保证（关键）
- preload 脚本在渲染进程 web content 加载前执行（Electron 官方保证）
- `contextBridge.exposeInMainWorld` 暴露的值在页面脚本运行前就可用
- spirit.html 第 165 行 `const IDLE_TIMEOUT = window.electronAPI.IDLE_TIMEOUT || ...` 执行时，`window.electronAPI.IDLE_TIMEOUT` 已被 preload 注入
- 零时序风险

### 5. 边界条件
- user-config.json 不存在 → catch 用默认5分钟
- sleepTriggerMinutes 为0或负数 → `minutes > 0` 校验失败用默认5
- sleepTriggerMinutes 不是数字 → `typeof minutes === 'number'` 失败用默认5
- user-config.json 解析失败 → catch 用默认5
- preload 未注入（contextBridge 失败）→ spirit.html `window.electronAPI` 检查 + `||` 回退5

### 6. 不改动的部分
- main.js 不需要改动（配置读取移到 preload）
- Rust 启动器的 `_context_config` 死代码不清理（不影响功能）
- Python 端不新增配置端点
- preload.js 现有 IPC 接口不变（只新增 IDLE_TIMEOUT 字段）
