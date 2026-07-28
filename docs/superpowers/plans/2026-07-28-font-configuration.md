# 字体配置化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把字体来源从硬编码改成配置驱动——不配字体时用跨平台仿宋兜底链，配了字体时前端读 `~/.niu/preferences.json` 动态生成 `@font-face` + `font-family`，主 Agent 通过系统手册知道怎么帮用户改字体。

**Architecture:** 前端 3 个窗口（chat/spirit/sticky）各自有 preload 脚本，preload 在页面脚本前同步执行。字体配置由 preload 读取 `~/.niu/preferences.json` 的 `font` 段，通过 `contextBridge.exposeInMainWorld` 注入 `FONT_FACE_CSS` 和 `FONT_FAMILY` 两个常量给前端。前端在 `<head>` 用内联 JS 注入 `<style>`（无配置时不注入，CSS 留跨平台兜底 `font-family`）。用户字体文件放 `~/.niu/fonts/` 固定目录。

**Tech Stack:** Electron 33 preload（Node.js fs/path）、HTML/CSS、Python（config-manager MCP server 已有读写 preferences.json 的能力，无需新增后端代码）

**改造范围（scout 盘点确认）：**
- ✅ 改：`assistant/chat.html`（阿朱泡泡体 + Caveat）、`assistant/spirit.html`（同）、`assistant/sticky.html`（同）、`settings/index.html`（马善政体 Ma Shan Zheng + Caveat，**之前漏盘点，本计划补上**）
- ❌ 不改：`graph/index.html`（纯 system-ui，干净）、`graph/demo.html` + `graph/test-api.html`（开发测试页，未注册为窗口）

## Global Constraints

- **铁律**：主 Agent 是项目经理不改代码，所有改动委托子 Agent；改前 git 备份 + gitnexus 影响分析；git 操作后修文件权限（`find ui/*/node_modules/.bin/ -type f ! -perm -u+x -exec chmod +x {} \;`）
- **不配字体时的兜底**：`font-family: 'STFangsong', 'FangSong', 'SimSun', serif;`（macOS 华文仿宋 → Windows 仿宋 → Windows 宋体 → serif 终极兜底）
- **配置驱动**：`~/.niu/preferences.json` 有 `font` 段才注入 `@font-face`，没有就不注入（用兜底）
- **字体文件目录**：`~/.niu/fonts/`（用户放 ttf 的固定目录，配置只填文件名不填路径）
- **不改后端**：字体配置由前端 preload 直读文件，不走 niu_api HTTP API（与现有 `sleepTriggerMinutes` 读取模式一致）
- **Caveat Google Font 移除**：3 个 html 的 `<link href="https://fonts.googleapis.com/css2?family=Caveat...">` 删除（离线环境/打包后无网络，且阿朱泡泡体已删，Caveat 无意义）
- **3 个窗口字体统一**：chat/spirit/sticky 用同一套兜底 + 同一套配置读取逻辑（不再区分场景）

---

## 文件结构

| 文件 | 责任 | 动作 |
|------|------|------|
| `ui/main/lib/font-config.js` | 新建。读取 `~/.niu/preferences.json` 的 `font` 段 + 校验 `~/.niu/fonts/` 下文件存在性，返回 `{ fontFaceCss, fontFamily }`。被 3 个 preload 共享。 | Create |
| `ui/main/preload-assistant.js` | spirit.html 的 preload。调用 `font-config.js`，注入 `FONT_FACE_CSS` + `FONT_FAMILY`。 | Modify |
| `ui/main/preload-chat.js` | chat.html 的 preload。同上。 | Modify |
| `ui/main/preload-sticky.js` | sticky.html 的 preload。同上。 | Modify |
| `ui/main/preload-settings.js` | settings/index.html 的 preload。同上（**scout 盘点补上的第 4 个 preload**）。 | Modify |
| `ui/main/windows/assistant/chat.html` | 删 `@font-face` + Caveat link + 硬编码 `font-family`；加兜底 `font-family` + 内联 JS 注入配置字体。 | Modify |
| `ui/main/windows/assistant/spirit.html` | 同 chat.html。 | Modify |
| `ui/main/windows/assistant/sticky.html` | 同 chat.html。 | Modify |
| `ui/main/windows/settings/index.html` | 删 Ma Shan Zheng Google Font link + 硬编码 `font-family`（5 处）；加兜底 `font-family` + 内联 JS 注入配置字体。**settings 原来无 @font-face（纯 Google Font），所以只删 link 不删 @font-face。** | Modify |
| `docs/SYSTEM_MANUAL.md` | 加"字体配置"章节，说明配置位置、格式、字体文件目录。 | Modify |
| `tests/test_font_config.js` | 新建。单元测试 `font-config.js` 的读取/校验逻辑（用 Node 直接跑，不依赖 Electron）。 | Create |

---

### Task 1: 新建 `font-config.js` 共享字体配置读取模块

**Files:**
- Create: `ui/main/lib/font-config.js`
- Test: `tests/test_font_config.js`

**Interfaces:**
- Produces: `loadFontConfig()` → `{ fontFaceCss: string, fontFamily: string }`
  - `fontFaceCss`：完整 `@font-face { ... }` CSS 字符串（无配置时为空串 `""`）
  - `fontFamily`：CSS `font-family` 值（无配置时为 `"'STFangsong', 'FangSong', 'SimSun', serif"`）
  - 配置示例（`~/.niu/preferences.json`）：
    ```json
    {
      "font": {
        "name": "MyHandwriting",
        "file": "my-font.ttf"
      }
    }
    ```
  - 有配置时返回：
    - `fontFaceCss`: `@font-face { font-family: 'MyHandwriting'; src: url('file:///Users/xxx/.niu/fonts/my-font.ttf') format('truetype'); font-display: swap; }`
    - `fontFamily`: `"'MyHandwriting', 'STFangsong', 'FangSong', 'SimSun', serif"`（自定义字体在前，仿宋兜底在后）

- [ ] **Step 1: 写失败测试**

创建 `tests/test_font_config.js`：

```javascript
// 测试不依赖 Electron，直接 require font-config.js
// font-config.js 用 require('os').homedir() 和 require('fs')，Node 原生模块
const path = require('path');
const fs = require('fs');
const os = require('os');

// 让 font-config.js 能被 require（它内部用相对路径 require electron 会有问题，所以 font-config.js 不应该 require electron）
const { loadFontConfig } = require('../ui/main/lib/font-config.js');

function writePrefs(prefs) {
  const prefsPath = path.join(os.homedir(), '.niu', 'preferences.json');
  fs.mkdirSync(path.dirname(prefsPath), { recursive: true });
  fs.writeFileSync(prefsPath, JSON.stringify(prefs), 'utf-8');
}

function writeFontFile(filename) {
  const fontsDir = path.join(os.homedir(), '.niu', 'fonts');
  fs.mkdirSync(fontsDir, { recursive: true });
  fs.writeFileSync(path.join(fontsDir, filename), 'fake-ttf-content', 'utf-8');
}

function cleanup() {
  const niuDir = path.join(os.homedir(), '.niu');
  try { fs.unlinkSync(path.join(niuDir, 'preferences.json')); } catch {}
  try { fs.rmSync(path.join(niuDir, 'fonts'), { recursive: true, force: true }); } catch {}
}

const DEFAULT_FAMILY = "'STFangsong', 'FangSong', 'SimSun', serif";

describe('loadFontConfig', () => {
  beforeEach(() => cleanup());
  afterAll(() => cleanup());

  test('无 font 配置时返回空 fontFaceCss + 仿宋兜底 fontFamily', () => {
    writePrefs({ context: { sleepTriggerMinutes: 5 } });  // 有 preferences 但无 font 段
    const result = loadFontConfig();
    expect(result.fontFaceCss).toBe('');
    expect(result.fontFamily).toBe(DEFAULT_FAMILY);
  });

  test('preferences.json 不存在时返回空 fontFaceCss + 仿宋兜底', () => {
    cleanup();  // 确保文件不存在
    const result = loadFontConfig();
    expect(result.fontFaceCss).toBe('');
    expect(result.fontFamily).toBe(DEFAULT_FAMILY);
  });

  test('有 font 配置且字体文件存在时返回 @font-face + 自定义 fontFamily', () => {
    writePrefs({ font: { name: 'MyHand', file: 'my.ttf' } });
    writeFontFile('my.ttf');
    const result = loadFontConfig();
    expect(result.fontFaceCss).toContain("@font-face");
    expect(result.fontFaceCss).toContain("font-family: 'MyHand'");
    expect(result.fontFaceCss).toContain("file://");
    expect(result.fontFaceCss).toContain("my.ttf");
    expect(result.fontFaceCss).toContain("font-display: swap");
    expect(result.fontFamily).toBe("'MyHand', 'STFangsong', 'FangSong', 'SimSun', serif");
  });

  test('有 font 配置但字体文件不存在时降级为兜底（不注入 @font-face）', () => {
    writePrefs({ font: { name: 'MyHand', file: 'missing.ttf' } });
    // 不写字体文件
    const result = loadFontConfig();
    expect(result.fontFaceCss).toBe('');
    expect(result.fontFamily).toBe(DEFAULT_FAMILY);
  });

  test('font 配置缺 name 字段时降级为兜底', () => {
    writePrefs({ font: { file: 'my.ttf' } });
    writeFontFile('my.ttf');
    const result = loadFontConfig();
    expect(result.fontFaceCss).toBe('');
    expect(result.fontFamily).toBe(DEFAULT_FAMILY);
  });

  test('font 配置缺 file 字段时降级为兜底', () => {
    writePrefs({ font: { name: 'MyHand' } });
    const result = loadFontConfig();
    expect(result.fontFaceCss).toBe('');
    expect(result.fontFamily).toBe(DEFAULT_FAMILY);
  });

  test('preferences.json 是损坏 JSON 时降级为兜底', () => {
    const prefsPath = path.join(os.homedir(), '.niu', 'preferences.json');
    fs.mkdirSync(path.dirname(prefsPath), { recursive: true });
    fs.writeFileSync(prefsPath, '{ broken json }}}', 'utf-8');
    const result = loadFontConfig();
    expect(result.fontFaceCss).toBe('');
    expect(result.fontFamily).toBe(DEFAULT_FAMILY);
  });
});
```

- [ ] **Step 2: 运行测试验证它失败**

Run: `cd ui/main && npx jest --config '{}' ../../tests/test_font_config.js` 或 `node --test tests/test_font_config.js`
Expected: FAIL — `Cannot find module '../ui/main/lib/font-config.js'`

- [ ] **Step 3: 实现 `font-config.js`**

创建 `ui/main/lib/font-config.js`：

```javascript
// 字体配置读取模块（不依赖 Electron，纯 Node.js fs/path）
// 被 preload-assistant.js / preload-chat.js / preload-sticky.js 共享调用

const fs = require('fs');
const path = require('path');
const os = require('os');

const DEFAULT_FONT_FAMILY = "'STFangsong', 'FangSong', 'SimSun', serif";

/**
 * 读取 ~/.niu/preferences.json 的 font 段，校验字体文件存在性，
 * 返回 { fontFaceCss, fontFamily }。
 *
 * 无配置 / 配置不完整 / 字体文件缺失 / JSON 损坏 → 返回空 fontFaceCss + 仿宋兜底 fontFamily。
 * 配置完整且文件存在 → 返回 @font-face CSS + "自定义字体, 仿宋兜底" 的 fontFamily。
 *
 * @returns {{ fontFaceCss: string, fontFamily: string }}
 */
function loadFontConfig() {
  try {
    const prefsPath = path.join(os.homedir(), '.niu', 'preferences.json');
    if (!fs.existsSync(prefsPath)) {
      return { fontFaceCss: '', fontFamily: DEFAULT_FONT_FAMILY };
    }
    const raw = fs.readFileSync(prefsPath, 'utf-8');
    const prefs = JSON.parse(raw);
    const fontCfg = prefs && prefs.font;
    if (!fontCfg || !fontCfg.name || !fontCfg.file) {
      return { fontFaceCss: '', fontFamily: DEFAULT_FONT_FAMILY };
    }
    const fontFile = path.join(os.homedir(), '.niu', 'fonts', fontCfg.file);
    if (!fs.existsSync(fontFile)) {
      return { fontFaceCss: '', fontFamily: DEFAULT_FONT_FAMILY };
    }
    // 文件存在，生成 @font-face
    const fontFaceCss = [
      '@font-face {',
      `  font-family: '${fontCfg.name}';`,
      `  src: url('file://${fontFile}') format('truetype');`,
      '  font-display: swap;',
      '}'
    ].join('\n');
    const fontFamily = `'${fontCfg.name}', 'STFangsong', 'FangSong', 'SimSun', serif`;
    return { fontFaceCss, fontFamily };
  } catch (e) {
    // JSON 损坏或其他异常 → 兜底
    return { fontFaceCss: '', fontFamily: DEFAULT_FONT_FAMILY };
  }
}

module.exports = { loadFontConfig, DEFAULT_FONT_FAMILY };
```

- [ ] **Step 4: 运行测试验证它通过**

Run: `node --test tests/test_font_config.js`
Expected: PASS — 7 tests passed

- [ ] **Step 5: 提交**

```bash
git add ui/main/lib/font-config.js tests/test_font_config.js
git commit -m "feat(font): 新建 font-config.js 共享字体配置读取模块

- 读取 ~/.niu/preferences.json 的 font 段
- 校验 ~/.niu/fonts/ 下文件存在性
- 无配置/配置不全/文件缺失/JSON损坏 → 仿宋兜底
- 配置完整 → 生成 @font-face CSS + 自定义 fontFamily"
```

---

### Task 2: 3 个 preload 注入字体配置

**Files:**
- Modify: `ui/main/preload-assistant.js`
- Modify: `ui/main/preload-chat.js`
- Modify: `ui/main/preload-sticky.js`

**Interfaces:**
- Consumes: `loadFontConfig()` from Task 1
- Produces: `window.electronAPI.FONT_FACE_CSS` (string) + `window.electronAPI.FONT_FAMILY` (string) 给 3 个 html 前端用

- [ ] **Step 1: 改 `preload-assistant.js`**

在文件顶部（`const { contextBridge, ipcRenderer, webUtils } = require('electron');` 之后）加：

```javascript
// 读取字体配置（同步，preload 在页面脚本前执行）
const { loadFontConfig } = require('./lib/font-config.js');
const _fontConfig = loadFontConfig();
```

在 `contextBridge.exposeInMainWorld('electronAPI', {` 内部，`IDLE_TIMEOUT` 那行之后加两行：

```javascript
  IDLE_TIMEOUT: _idleTimeoutMs,  // 睡眠触发时间（毫秒），从 user-config.json 读取
  FONT_FACE_CSS: _fontConfig.fontFaceCss,  // @font-face CSS（无配置时为空串）
  FONT_FAMILY: _fontConfig.fontFamily,     // font-family 值（无配置时为仿宋兜底）
```

- [ ] **Step 2: 改 `preload-chat.js`**

在文件顶部加：

```javascript
// 读取字体配置（同步，preload 在页面脚本前执行）
const { loadFontConfig } = require('./lib/font-config.js');
const _fontConfig = loadFontConfig();
```

在 `contextBridge.exposeInMainWorld('electronAPI', {` 内部第一行加：

```javascript
  FONT_FACE_CSS: _fontConfig.fontFaceCss,  // @font-face CSS（无配置时为空串）
  FONT_FAMILY: _fontConfig.fontFamily,     // font-family 值（无配置时为仿宋兜底）
```

- [ ] **Step 3: 改 `preload-sticky.js`**

同 Step 2（preload-sticky.js 和 preload-chat.js 结构一样，顶部加 require + 内部加两行）。

- [ ] **Step 4: 改 `preload-settings.js`**

同 Step 2（preload-settings.js 结构相同，顶部加 require + 内部加两行）。

- [ ] **Step 5: 语法验证**

Run:
```bash
node -e "require('./ui/main/lib/font-config.js'); console.log('font-config OK')"
node --check ui/main/preload-assistant.js && echo "assistant OK"
node --check ui/main/preload-chat.js && echo "chat OK"
node --check ui/main/preload-sticky.js && echo "sticky OK"
node --check ui/main/preload-settings.js && echo "settings OK"
```
Expected: 5 行 OK

- [ ] **Step 6: 提交**

```bash
git add ui/main/preload-assistant.js ui/main/preload-chat.js ui/main/preload-sticky.js ui/main/preload-settings.js
git commit -m "feat(font): 4 个 preload 注入 FONT_FACE_CSS + FONT_FAMILY"
```

---

### Task 3: 3 个 html 改用兜底 + 动态注入配置字体

**Files:**
- Modify: `ui/main/windows/assistant/chat.html`
- Modify: `ui/main/windows/assistant/spirit.html`
- Modify: `ui/main/windows/assistant/sticky.html`

**Interfaces:**
- Consumes: `window.electronAPI.FONT_FACE_CSS` + `window.electronAPI.FONT_FAMILY` from Task 2

- [ ] **Step 1: 改 `chat.html`**

**1a. 删 Caveat Google Font link**（第 18 行）：
```html
<!-- 删掉这行 -->
<link href="https://fonts.googleapis.com/css2?family=Caveat:wght@400;600&display=swap" rel="stylesheet">
```

**1b. 删 `@font-face` 块**（第 21-28 行）：
```css
<!-- 删掉整个 @font-face { font-family: 'AZhuPaoPaoTi'; ... } 块 -->
```

**1c. 改 `html, body` 的 `font-family`**（第 36 行）：
```css
/* 改前 */
font-family: 'AZhuPaoPaoTi', 'Caveat', system-ui, sans-serif;
/* 改后（兜底，会被动态注入覆盖） */
font-family: 'STFangsong', 'FangSong', 'SimSun', serif;
```

**1d. 改其他 `'AZhuPaoPaoTi', cursive` 引用**（第 116/290/310/381 行）：
```css
/* 改前 */
font-family: 'AZhuPaoPaoTi', cursive;
/* 改后 */
font-family: 'STFangsong', 'FangSong', 'SimSun', serif;
```

**1e. 在 `<head>` 末尾（`</head>` 之前）加动态注入脚本**：
```html
    <!-- 字体配置动态注入：无配置时不注入（用 CSS 兜底仿宋），有配置时注入 @font-face + 覆盖 font-family -->
    <script>
      (function() {
        if (window.electronAPI && window.electronAPI.FONT_FACE_CSS) {
          var style = document.createElement('style');
          style.textContent = window.electronAPI.FONT_FACE_CSS;
          document.head.appendChild(style);
        }
        if (window.electronAPI && window.electronAPI.FONT_FAMILY) {
          var style2 = document.createElement('style');
          style2.textContent = 'html, body, * { font-family: ' + window.electronAPI.FONT_FAMILY + ' !important; }';
          document.head.appendChild(style2);
        }
      })();
    </script>
```

- [ ] **Step 2: 改 `spirit.html`**

同 chat.html 的 5 个子步骤：
- 2a. 删第 8 行 Caveat link
- 2b. 删第 11-18 行 `@font-face` 块
- 2c. 改 `html, body` 的 `font-family`（如果有）为兜底
- 2d. 改第 109/128 行 `'AZhuPaoPaoTi', cursive` 为兜底
- 2e. 在 `</head>` 前加同样的动态注入脚本

- [ ] **Step 3: 改 `sticky.html`**

同上：
- 3a. 删第 8 行 Caveat link
- 3b. 删第 11-18 行 `@font-face` 块
- 3c. 改第 25 行 `font-family: 'AZhuPaoPaoTi', 'Caveat', system-ui, sans-serif;` 为兜底
- 3d. 改第 75 行 `'AZhuPaoPaoTi', cursive` 为兜底
- 3e. 在 `</head>` 前加同样的动态注入脚本

- [ ] **Step 4: 改 `settings/index.html`**

**注意：settings 原来用的是 Google Font `Ma Shan Zheng`（马善政体），不是阿朱泡泡体，也没有本地 `@font-face`（纯 Google Font link）。**

- 4a. 删 Google Font link（第 6 行）：
```html
<!-- 删掉这行（含 Ma Shan Zheng + Caveat） -->
<link href="https://fonts.googleapis.com/css2?family=Ma+Shan+Zheng&family=Caveat:wght@400;600&display=swap" rel="stylesheet">
```
- 4b. **settings 无 @font-face，跳过这步**
- 4c. 改所有 `'Ma Shan Zheng', 'Caveat', cursive` 引用（第 10/110 行）为兜底：
```css
/* 改前 */
font-family: 'Ma Shan Zheng', 'Caveat', cursive;
/* 改后 */
font-family: 'STFangsong', 'FangSong', 'SimSun', serif;
```
- 4d. 改所有 `'Ma Shan Zheng', cursive` 引用（第 46/128/155 行）为兜底
- 4e. 在 `</head>` 前加同样的动态注入脚本（与 chat.html Step 1e 相同）

- [ ] **Step 5: 验证无残留字体引用**

Run:
```bash
grep -rn "AZhuPaoPaoTi\|Caveat\|Ma Shan Zheng\|googleapis.*font\|googleapis.*Caveat" ui/main/windows/assistant/*.html ui/main/windows/settings/*.html
```
Expected: 无输出（全部清理干净）

- [ ] **Step 6: 验证兜底 font-family 都在**

Run:
```bash
grep -n "STFangsong\|FangSong\|SimSun" ui/main/windows/assistant/*.html ui/main/windows/settings/*.html | wc -l
```
Expected: ≥ 8（4 个文件 × 至少 2 处：CSS 兜底 html,body + 其他 cursive 改的）

- [ ] **Step 7: 提交**

```bash
git add ui/main/windows/assistant/chat.html ui/main/windows/assistant/spirit.html ui/main/windows/assistant/sticky.html ui/main/windows/settings/index.html
git commit -m "refactor(font): 4 个窗口删硬编码字体，改用仿宋兜底 + 配置动态注入

- assistant 三窗口：删 @font-face 阿朱泡泡体 + Caveat Google Font link
- settings 窗口：删 Ma Shan Zheng + Caveat Google Font link（无 @font-face）
- CSS font-family 兜底改仿宋（STFangsong/FangSong/SimSun/serif）
- <head> 加内联 JS：有 FONT_FACE_CSS 时注入 @font-face，有 FONT_FAMILY 时覆盖 font-family"
```

---

### Task 4: 系统手册加字体配置章节

**Files:**
- Modify: `docs/SYSTEM_MANUAL.md`

**Interfaces:** 无（纯文档）

- [ ] **Step 1: 找到插入位置**

读 `docs/SYSTEM_MANUAL.md`，找到"## 二、架构设计"或"## 三、用户目录"相关章节。在用户目录 `~/.niu/` 说明附近加"### 字体配置"小节。如果没有合适的章节，加在文档末尾"## 分册索引"之前，作为新的顶级章节"## X. 字体配置"。

- [ ] **Step 2: 插入字体配置章节**

插入内容：

```markdown
## 字体配置

### 配置位置

字体配置在 `~/.niu/preferences.json` 的 `font` 段：

```json
{
  "font": {
    "name": "字体显示名（CSS font-family 名，自定义）",
    "file": "字体文件名（放在 ~/.niu/fonts/ 目录下）"
  }
}
```

### 字体文件目录

用户自定义字体文件（.ttf/.otf）放在 `~/.niu/fonts/` 目录下。配置里 `file` 字段只填文件名，不填完整路径。

### 不配置时的缺省字体

不配置 `font` 段时，所有窗口使用跨平台仿宋兜底链：
```
'STFangsong', 'FangSong', 'SimSun', serif
```
- macOS：华文仿宋（STFangsong）
- Windows：仿宋（FangSong）→ 宋体（SimSun）兜底
- 其他：serif 终极兜底

### 配置示例

假设用户想用"方正楷体"：

1. 把 `FZKai-Z03.ttf` 放到 `~/.niu/fonts/`
2. 编辑 `~/.niu/preferences.json`：
```json
{
  "font": {
    "name": "FZKaiTi",
    "file": "FZKai-Z03.ttf"
  }
}
```
3. 重启 Niu，所有窗口字体生效

### 配置生效时机

字体配置在窗口启动时由 preload 脚本读取（同步），修改配置后需重启 Niu 生效。

### 容错

以下情况自动降级为仿宋兜底，不影响使用：
- `preferences.json` 不存在或无 `font` 段
- `font` 段缺 `name` 或 `file` 字段
- 字体文件不存在（`~/.niu/fonts/` 下找不到）
- `preferences.json` JSON 格式损坏
```

- [ ] **Step 3: 验证**

Run: `grep -n "字体配置\|preferences.json.*font\|~/.niu/fonts" docs/SYSTEM_MANUAL.md`
Expected: 至少 3 行匹配

- [ ] **Step 4: 提交**

```bash
git add docs/SYSTEM_MANUAL.md
git commit -m "docs: 系统手册加字体配置章节（位置/格式/缺省/容错）"
```

---

## Self-Review

**1. Spec coverage:**
- ✅ "不配字体就用系统缺省（仿宋）" → Task 3 的 CSS 兜底 + Task 1 的 `DEFAULT_FONT_FAMILY`
- ✅ "配了字体才改配置" → Task 1 `loadFontConfig` 无配置返回空 `fontFaceCss`，Task 3 前端只在 `FONT_FACE_CSS` 非空时注入
- ✅ "原兜底逻辑去掉" → Task 3 删 `@font-face` + Caveat + Ma Shan Zheng + 硬编码 `font-family`
- ✅ "字体文件放 ~/.niu/fonts/" → Task 1 `loadFontConfig` 从该目录找文件
- ✅ "preferences.json 配置" → Task 1 读 `font` 段
- ✅ "系统手册说明配置" → Task 4
- ✅ "跨平台兼容" → 兜底链 `STFangsong`(macOS) + `FangSong`(Win) + `SimSun`(Win) + `serif`
- ✅ "用户提醒的设置页 + 知识图谱页" → scout 盘点确认：settings/index.html 需改（Ma Shan Zheng），graph/index.html 纯 system-ui 无需改，已纳入 Task 2/3

**2. Placeholder scan:** 无 TODO/TBD，所有步骤都有完整代码。

**3. Type consistency:**
- `loadFontConfig()` 返回 `{ fontFaceCss: string, fontFamily: string }` — Task 1 定义，Task 2 使用，一致 ✓
- `window.electronAPI.FONT_FACE_CSS` + `FONT_FAMILY` — Task 2 注入，Task 3 使用，一致 ✓
- 兜底值 `"'STFangsong', 'FangSong', 'SimSun', serif"` — Task 1 的 `DEFAULT_FONT_FAMILY` 与 Task 3 的 CSS 兜底字面量一致 ✓

**4. 风险点：**
- **铁律遵守**：本计划所有代码改动由子 Agent 执行（SDD 方式），主 Agent 只 review。改前 git 备份 + gitnexus 影响分析由子 Agent 在各自 Task 内执行。
- **settings 页特殊性**：settings 原来用 Google Font `Ma Shan Zheng`（马善政体），不是阿朱泡泡体，也没有本地 @font-face。Task 3 Step 4 单独处理：只删 Google Font link，不删 @font-face（本来就没有）。改完后 settings 在离线环境也能正常显示（之前依赖 Google Font CDN，离线会降级到 cursive，现在用仿宋兜底更好）。
- **`!important` 覆盖**：Task 3 的动态注入用 `font-family: ... !important` 确保覆盖 CSS 兜底。这是必要的——CSS 兜底写在 `<style>` 里先执行，动态注入后追加的 `<style>` 不带 `!important` 会被源序覆盖。但 `!important` 会让所有元素都用配置字体，包括代码块等——这是预期行为（用户配了字体就是要全用）。
- **测试环境**：Task 1 的测试用 `node --test`，需要 Node.js 18+（内置 test runner）。项目用 Electron 33，Node 版本足够。测试直接读写 `~/.niu/`，会污染开发者的真实配置——`beforeEach`/`afterAll` 有 cleanup，但若测试中途崩溃可能残留。可接受（开发者环境）。
- **4 个 preload 重复代码**：4 个 preload 都 require `font-config.js` + 注入两个常量，有重复但可接受（preload 之间无法共享，各自独立注入）。如果未来要加更多共享配置，可抽公共 preload 模块，不在本计划范围。
- **spirit.html 的 SVG text**：spirit.html 第 158 行有 `font-family="system-ui, sans-serif"` 的 SVG `<text>`，是进度百分比文字，不属于聊天字体范畴，**不改**（保持 system-ui，SVG 内嵌字体独立）。Task 3 Step 2 的 2d 只改 `'AZhuPaoPaoTi', cursive` 引用，不动 SVG text。
- **graph 页不改**：scout 确认 graph/index.html 纯 system-ui，无任何自定义字体引用，不在改造范围。如果用户未来想给 graph 页也配字体，动态注入脚本机制可复用，但本计划不主动改 graph。

无问题，计划可执行。

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-07-28-font-configuration.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — 我派 fresh subagent 每个 Task 执行，任务间 review 门控，迭代快

**2. Inline Execution** — 在当前会话批量执行，检查点 review

**Which approach?**
