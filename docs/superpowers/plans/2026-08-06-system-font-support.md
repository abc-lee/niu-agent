# 系统字体支持 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 支持通过 `font.name`（不配 `font.file`）直接引用系统已安装字体，无需自定义字体文件。

**Architecture:** 修改 `font-config.js` 的 `loadFontConfig()` 函数，在 name+file 验证失败后增加一个分支：如果只有 name 没有 file，跳过 @font-face 生成（不内联字体文件），直接返回 `fontFaceCss=''` + `fontFamily='name, serif'`。HTML 端无需改动——已有的注入逻辑会跳过空的 FONT_FACE_CSS，只注入 FONT_FAMILY。

**Tech Stack:** Node.js (fs/path)，Electron contextBridge，原生 CSS font-family

---

## File Structure

| File | Responsibility |
|---|---|
| `ui/main/lib/font-config.js` | 核心字体配置加载模块。修改验证逻辑，增加"只有 name"分支 |
| `tests/test_font_config.js` | 字体配置测试。修改"缺 file"测试为系统字体模式断言 |
| `docs/SYSTEM_MANUAL.md` | 系统管理手册。同步字体配置说明（file 变为可选、新增系统字体模式说明、修正容错描述） |

**不改动的文件**（现有逻辑已兼容）：
- `ui/main/preload-assistant.js` / `preload-chat.js` / `preload-sticky.js` — 只透传 `fontFaceCss` 和 `fontFamily`，不关心来源
- `ui/main/windows/assistant/chat.html` / `spirit.html` / `sticky.html` — 注入 IIFE 已处理 `fontFaceCss=''` 的情况（不注入 @font-face），`fontFamily` 非空就注入 font-family 覆盖

**文档分册检查：** 已搜索全部 14 个分册（`docs/manual-*.md`），均无字体相关内容。字体配置文档只存在于 `SYSTEM_MANUAL.md` 主册 L525-576。

---

## 当前逻辑 vs 目标逻辑

### 当前（font-config.js L33）
```
fontCfg 存在？
  ├─ 否 → 降级（空）
  └─ 是 → name 和 file 都有？
       ├─ 否 → 降级（空）       ← "只有 name" 在这里被吞掉
       └─ 是 → 文件存在？
            ├─ 否 → 降级（空）
            └─ 是 → @font-face + fontFamily
```

### 目标
```
fontCfg 存在？
  ├─ 否 → 降级（空）
  └─ 是 → name 存在？
       ├─ 否 → 降级（空）
       └─ 是 → file 存在？
            ├─ 否 → 系统字体模式：fontFaceCss='' + fontFamily='name, serif'  ← 新增
            └─ 是 → 文件存在？
                 ├─ 否 → 降级（空）
                 └─ 是 → @font-face + fontFamily
```

关键变化：把 `!fontCfg.name || !fontCfg.file` 拆成两步——先检查 name，再检查 file。name 有但 file 没有时走系统字体模式。

---

### Task 1: 修改"缺 file"测试用例（从降级改为系统字体模式）

**Files:**
- Test: `tests/test_font_config.js:80-86`

**注意：** 现有测试 "font 配置缺 file 字段时降级为兜底"（L80-86）断言 `fontFamily === DEFAULT_FONT_FAMILY`（空）。新逻辑改为系统字体模式后，这个断言会失败。需要修改这个测试。

- [ ] **Step 1: 修改现有测试用例**

将 `tests/test_font_config.js` L80-86 的：

```javascript
  test('font 配置缺 file 字段时降级为兜底', () => {
    const niuDir = _tmpDir;
    writePrefs(niuDir, { font: { name: 'MyHand' } });
    const result = loadFontConfig(niuDir);
    assert.equal(result.fontFaceCss, '');
    assert.equal(result.fontFamily, DEFAULT_FONT_FAMILY);
  });
```

替换为：

```javascript
  test('font 配置只有 name 没有 file 时使用系统字体（不注入 @font-face，fontFamily 为 name）', () => {
    const niuDir = _tmpDir;
    writePrefs(niuDir, { font: { name: 'MyHand' } });
    const result = loadFontConfig(niuDir);
    assert.equal(result.fontFaceCss, '', '系统字体模式不应注入 @font-face');
    assert.equal(result.fontFamily, "'MyHand', serif");
  });
```

- [ ] **Step 2: 运行测试验证它失败**

Run: `node --test tests/test_font_config.js`
Expected: FAIL — 修改后的测试断言 `fontFamily === "'MyHand', serif"`，但当前逻辑返回 `''`（降级）

- [ ] **Step 3: Commit**

```bash
git add tests/test_font_config.js
git commit -m "test: change 'missing file' test to expect system font mode"
```

---

### Task 2: 实现系统字体模式

**Files:**
- Modify: `ui/main/lib/font-config.js:10-35`

- [ ] **Step 1: 修改验证逻辑**

将 `ui/main/lib/font-config.js` L33-35 的：

```javascript
    if (!fontCfg || !fontCfg.name || !fontCfg.file) {
      return { fontFaceCss: '', fontFamily: DEFAULT_FONT_FAMILY };
    }
```

替换为：

```javascript
    if (!fontCfg || !fontCfg.name) {
      return { fontFaceCss: '', fontFamily: DEFAULT_FONT_FAMILY };
    }
    // 系统字体模式：只有 name 没有 file → 直接用系统已安装字体，不内联字体文件
    if (!fontCfg.file) {
      return { fontFaceCss: '', fontFamily: `'${fontCfg.name}', serif` };
    }
```

- [ ] **Step 2: 更新函数 JSDoc 注释**

将 L10-16 的注释替换为：

```javascript
/**
 * 读取 ~/.niu/preferences.json 的 font 段，校验字体文件存在性，
 * 返回 { fontFaceCss, fontFamily }。
 *
 * 无配置 / 缺 name / JSON 损坏 → 返回空 fontFaceCss + 空 fontFamily（用浏览器系统默认字体）。
 * 只有 name 没有 file（系统字体模式）→ 返回空 fontFaceCss + "name, serif"（直接引用系统已安装字体）。
 * name + file 且文件存在 → 返回 @font-face CSS（base64 data URI 内联，绕开 file:// CORS）+ "自定义字体, serif" 的 fontFamily。
 * name + file 但文件缺失 → 返回空 fontFaceCss + 空 fontFamily（用浏览器系统默认字体）。
 *
 * 用 base64 内联而非 file:// URL，原因：Electron webSecurity 默认 true，
 * 跨目录 file:// 字体加载可能被 CORS 拦截。base64 完全在渲染进程内，无网络/文件协议问题。
 *
 * @param {string} [niuDirOverride] 可选，测试用：覆盖 ~/.niu 目录路径（默认读 os.homedir()/.niu）
 * @returns {{ fontFaceCss: string, fontFamily: string }}
 */
```

- [ ] **Step 3: 运行全部字体测试验证通过**

Run: `node --test tests/test_font_config.js`
Expected: PASS — 全部 7 个测试通过（原 7 个，修改了 1 个）

- [ ] **Step 4: Commit**

```bash
git add ui/main/lib/font-config.js
git commit -m "feat: support system font by name without file field"
```

---

### Task 3: 更新系统管理手册

**Files:**
- Modify: `docs/SYSTEM_MANUAL.md:528-576`

- [ ] **Step 1: 更新配置 schema 说明（L533-536）**

将：

```json
{
  "font": {
    "name": "字体显示名（CSS font-family 名，自定义）",
    "file": "字体文件名（放在 ~/.niu/fonts/ 目录下）"
  }
}
```

替换为：

```json
{
  "font": {
    "name": "字体名（CSS font-family 名，自定义）",
    "file": "字体文件名（放在 ~/.niu/fonts/ 目录下，可选）"
  }
}
```

> `file` 可选。不配 `file` 时，`name` 直接引用系统已安装字体（如 `PingFang SC`、`Microsoft YaHei`），无需字体文件。

- [ ] **Step 2: 在"字体文件目录"段后新增"系统字体模式"段（L542 之后）**

在 `### 字体文件目录` 段之后、`### 不配置时的缺省字体` 段之前，插入：

```markdown
### 系统字体模式

只配 `name` 不配 `file` 时，直接引用系统已安装字体，不内联字体文件、不注入 `@font-face`，只覆盖 `font-family`。

适用于系统已有字体（如 macOS 的 `PingFang SC`、Windows 的 `Microsoft YaHei`），无需下载字体文件。

```json
{
  "font": {
    "name": "PingFang SC"
  }
}
```
```

- [ ] **Step 3: 更新容错说明（L572-575）**

将：

```markdown
以下情况自动降级为系统默认字体（不注入 `@font-face`、不覆盖 `font-family`），不影响使用：
- `preferences.json` 不存在或无 `font` 段
- `font` 段缺 `name` 或 `file` 字段
- 字体文件不存在（`~/.niu/fonts/` 下找不到）
- `preferences.json` JSON 格式损坏
```

替换为：

```markdown
以下情况自动降级为系统默认字体（不注入 `@font-face`、不覆盖 `font-family`），不影响使用：
- `preferences.json` 不存在或无 `font` 段
- `font` 段缺 `name` 字段
- `font` 段配了 `file` 但字体文件不存在（`~/.niu/fonts/` 下找不到）
- `preferences.json` JSON 格式损坏
```

> 注意：`file` 可选。只缺 `file` 不缺 `name` 时走系统字体模式（非降级）。

- [ ] **Step 4: Commit**

```bash
git add docs/SYSTEM_MANUAL.md
git commit -m "docs: update system manual for system font support"
```

---

## Self-Review

### 1. Spec coverage

- ✅ "font 段缺 name 或 file 字段 → 自动降级为系统默认字体" — 当 name 缺失时降级（Task 2 的 `!fontCfg.name` 分支），当 file 缺失但有 name 时走系统字体模式（Task 2 的 `!fontCfg.file` 分支，Task 1 修改对应测试）
- ✅ "要支持系统字体的话，得加个逻辑——如果只有 name 没有 file" — Task 2 实现了这个分支
- ✅ 不影响现有"自定义字体文件"方式 — name+file+文件存在仍走 @font-face 内联
- ✅ 系统管理手册同步 — Task 3 更新配置 schema、新增系统字体模式段、修正容错描述
- ✅ 分册检查 — 已搜索全部 14 个分册（manual-*.md），均无字体相关内容，只需更新 SYSTEM_MANUAL.md

### 2. Placeholder scan

- 无 TBD/TODO
- 所有代码步骤都有完整代码
- 测试用例有具体断言

### 3. Type consistency

- `loadFontConfig()` 返回值 `{ fontFaceCss: string, fontFamily: string }` — 系统字体模式返回 `''` 和 `'name, serif'`，类型一致
- `DEFAULT_FONT_FAMILY` 仍为 `""`，系统字体模式不使用它（直接构造 fontFamily 字符串）
- HTML 端注入逻辑：`FONT_FACE_CSS` 为空字符串 → falsy → 不注入 @font-face ✅；`FONT_FAMILY` 非空 → truthy → 注入 font-family 覆盖 ✅
