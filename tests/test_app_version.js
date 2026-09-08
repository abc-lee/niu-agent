// 测试用 node:test 原生 API（不依赖 jest），断言用 node:assert/strict
// 测试用临时目录，不碰仓库根真实 VERSION 文件
const { test, describe, beforeEach, after } = require('node:test');
const assert = require('node:assert/strict');
const path = require('path');
const fs = require('fs');
const os = require('os');

const { readAppVersion } = require('../ui/main/lib/app-version.js');

let _tmpDir;
function freshTmpBase() {
  _tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'niu-app-version-test-'));
  return _tmpDir;
}

describe('readAppVersion', () => {
  beforeEach(() => { _tmpDir = freshTmpBase(); });
  after(() => {
    if (_tmpDir) fs.rmSync(_tmpDir, { recursive: true, force: true });
  });

  test('VERSION 文件存在时返回 trim 后的版本号', () => {
    fs.writeFileSync(path.join(_tmpDir, 'VERSION'), '1.2.3\n', 'utf-8');
    assert.equal(readAppVersion(_tmpDir), '1.2.3');
  });

  test('VERSION 文件不存在时兜底为 dev', () => {
    // 不写 VERSION 文件
    assert.equal(readAppVersion(_tmpDir), 'dev');
  });

  test('VERSION 文件为空时兜底为 dev', () => {
    fs.writeFileSync(path.join(_tmpDir, 'VERSION'), '', 'utf-8');
    assert.equal(readAppVersion(_tmpDir), 'dev');
  });
});
