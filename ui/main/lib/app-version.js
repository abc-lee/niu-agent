// 应用版本号读取模块（不依赖 Electron，纯 Node.js fs/path）
// 版本号单一真相源 = 仓库根 VERSION 文件；被 preload-chat.js 调用暴露给 chat.html

const path = require('path');
const fs = require('fs');

/**
 * 读取 baseDir/VERSION 文件，trim 后返回版本号字符串。
 *
 * 文件不存在 / 空内容（含纯空白）/ 任何异常 → 返回 'dev'（兜底，无害）。
 *
 * @param {string} baseDir 仓库根目录（开发态：ui/main/ 上两级；打包态：Resources/ 上两级）
 * @returns {string} 版本号字符串或 'dev'
 */
function readAppVersion(baseDir) {
  try {
    const raw = fs.readFileSync(path.join(baseDir, 'VERSION'), 'utf-8');
    const version = raw.trim();
    if (!version) {
      return 'dev';
    }
    return version;
  } catch (e) {
    // 文件缺失或其他异常 → 兜底
    return 'dev';
  }
}

module.exports = { readAppVersion };
