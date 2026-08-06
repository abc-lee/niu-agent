// 字体配置读取模块（不依赖 Electron，纯 Node.js fs/path）
// 被 preload-assistant.js / preload-chat.js / preload-sticky.js 共享调用

const path = require('path');
const fs = require('fs');
const os = require('os');

const DEFAULT_FONT_FAMILY = "";

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
function loadFontConfig(niuDirOverride) {
  try {
    const niuDir = niuDirOverride || path.join(os.homedir(), '.niu');
    const prefsPath = path.join(niuDir, 'preferences.json');
    if (!fs.existsSync(prefsPath)) {
      return { fontFaceCss: '', fontFamily: DEFAULT_FONT_FAMILY };
    }
    const raw = fs.readFileSync(prefsPath, 'utf-8');
    const prefs = JSON.parse(raw);
    const fontCfg = prefs && prefs.font;
    if (!fontCfg || !fontCfg.name) {
      return { fontFaceCss: '', fontFamily: DEFAULT_FONT_FAMILY };
    }
    // 系统字体模式：只有 name 没有 file → 直接用系统已安装字体，不内联字体文件
    if (!fontCfg.file) {
      return { fontFaceCss: '', fontFamily: `'${fontCfg.name}', serif` };
    }
    const fontFile = path.join(niuDir, 'fonts', fontCfg.file);
    if (!fs.existsSync(fontFile)) {
      return { fontFaceCss: '', fontFamily: DEFAULT_FONT_FAMILY };
    }
    // 文件存在，读为 base64 生成 @font-face（data URI 内联，绕开 file:// CORS）
    const fontBytes = fs.readFileSync(fontFile);
    const base64 = fontBytes.toString('base64');
    const fontFaceCss = [
      '@font-face {',
      `  font-family: '${fontCfg.name}';`,
      `  src: url(data:font/truetype;base64,${base64}) format('truetype');`,
      '  font-display: swap;',
      '}'
    ].join('\n');
    const fontFamily = `'${fontCfg.name}', serif`;
    return { fontFaceCss, fontFamily };
  } catch (e) {
    // JSON 损坏或其他异常 → 兜底
    return { fontFaceCss: '', fontFamily: DEFAULT_FONT_FAMILY };
  }
}

module.exports = { loadFontConfig, DEFAULT_FONT_FAMILY };
