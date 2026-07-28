// 测试用 node:test 原生 API（不依赖 jest），断言用 node:assert/strict
// 测试用临时目录，不碰真实 ~/.niu/preferences.json
const { test, describe, beforeEach, after } = require('node:test');
const assert = require('node:assert/strict');
const path = require('path');
const fs = require('fs');
const os = require('os');

const { loadFontConfig, DEFAULT_FONT_FAMILY } = require('../ui/main/lib/font-config.js');

let _tmpDir;
function freshTmpNiu() {
  _tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'niu-font-test-'));
  return _tmpDir;
}

function writePrefs(niuDir, prefs) {
  const prefsPath = path.join(niuDir, 'preferences.json');
  fs.mkdirSync(path.dirname(prefsPath), { recursive: true });
  fs.writeFileSync(prefsPath, JSON.stringify(prefs), 'utf-8');
}

function writeFontFile(niuDir, filename) {
  const fontsDir = path.join(niuDir, 'fonts');
  fs.mkdirSync(fontsDir, { recursive: true });
  fs.writeFileSync(path.join(fontsDir, filename), 'fake-ttf-content', 'utf-8');
}

describe('loadFontConfig', () => {
  beforeEach(() => { _tmpDir = freshTmpNiu(); });
  after(() => {
    if (_tmpDir) fs.rmSync(_tmpDir, { recursive: true, force: true });
  });

  test('无 font 配置时返回空 fontFaceCss + 仿宋兜底 fontFamily', () => {
    const niuDir = _tmpDir;
    writePrefs(niuDir, { context: { sleepTriggerMinutes: 5 } });  // 有 preferences 但无 font 段
    const result = loadFontConfig(niuDir);
    assert.equal(result.fontFaceCss, '');
    assert.equal(result.fontFamily, DEFAULT_FONT_FAMILY);
  });

  test('preferences.json 不存在时返回空 fontFaceCss + 仿宋兜底', () => {
    const niuDir = _tmpDir;  // 不写 preferences
    const result = loadFontConfig(niuDir);
    assert.equal(result.fontFaceCss, '');
    assert.equal(result.fontFamily, DEFAULT_FONT_FAMILY);
  });

  test('有 font 配置且字体文件存在时返回 @font-face + 自定义 fontFamily', () => {
    const niuDir = _tmpDir;
    writePrefs(niuDir, { font: { name: 'MyHand', file: 'my.ttf' } });
    writeFontFile(niuDir, 'my.ttf');
    const result = loadFontConfig(niuDir);
    assert.ok(result.fontFaceCss.includes('@font-face'), '应含 @font-face');
    assert.ok(result.fontFaceCss.includes("font-family: 'MyHand'"), '应含自定义字体名');
    assert.ok(result.fontFaceCss.includes('data:font/truetype;base64,'), '应用 base64 data URI');
    assert.ok(result.fontFaceCss.includes('font-display: swap'), '应含 font-display: swap');
    assert.equal(result.fontFamily, "'MyHand', 'STFangsong', 'Songti SC', 'FangSong', 'SimSun', serif");
  });

  test('有 font 配置但字体文件不存在时降级为兜底（不注入 @font-face）', () => {
    const niuDir = _tmpDir;
    writePrefs(niuDir, { font: { name: 'MyHand', file: 'missing.ttf' } });
    // 不写字体文件
    const result = loadFontConfig(niuDir);
    assert.equal(result.fontFaceCss, '');
    assert.equal(result.fontFamily, DEFAULT_FONT_FAMILY);
  });

  test('font 配置缺 name 字段时降级为兜底', () => {
    const niuDir = _tmpDir;
    writePrefs(niuDir, { font: { file: 'my.ttf' } });
    writeFontFile(niuDir, 'my.ttf');
    const result = loadFontConfig(niuDir);
    assert.equal(result.fontFaceCss, '');
    assert.equal(result.fontFamily, DEFAULT_FONT_FAMILY);
  });

  test('font 配置缺 file 字段时降级为兜底', () => {
    const niuDir = _tmpDir;
    writePrefs(niuDir, { font: { name: 'MyHand' } });
    const result = loadFontConfig(niuDir);
    assert.equal(result.fontFaceCss, '');
    assert.equal(result.fontFamily, DEFAULT_FONT_FAMILY);
  });

  test('preferences.json 是损坏 JSON 时降级为兜底', () => {
    const niuDir = _tmpDir;
    const prefsPath = path.join(niuDir, 'preferences.json');
    fs.mkdirSync(path.dirname(prefsPath), { recursive: true });
    fs.writeFileSync(prefsPath, '{ broken json }}}', 'utf-8');
    const result = loadFontConfig(niuDir);
    assert.equal(result.fontFaceCss, '');
    assert.equal(result.fontFamily, DEFAULT_FONT_FAMILY);
  });
});
