// 测试用 node:test 原生 API（不依赖 jest），断言用 node:assert/strict
// 测试用临时目录，不碰真实 ~/.niu/config/llm-configs.json
// 契约：docs/superpowers/plans/2026-09-07-llm-named-configs.md §0 C3 + T1「named-configs」段
// 文件格式（spec §3.1 冻结包装层）：{ "configs": { "<名>": { llm, lightrag_llm } } }
const { test, describe, beforeEach, after } = require('node:test');
const assert = require('node:assert/strict');
const path = require('path');
const fs = require('fs');
const os = require('os');

const { loadNamedConfigs, upsertNamedConfig, COLLECTION_FILE } = require('../ui/main/lib/named-configs.js');

let _tmpDir;
function freshTmpConfigDir() {
  _tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'niu-namedcfg-test-'));
  return _tmpDir;
}

function collectionPath(dir) {
  return path.join(dir, COLLECTION_FILE);
}

describe('named-configs', () => {
  beforeEach(() => { _tmpDir = freshTmpConfigDir(); });
  after(() => {
    if (_tmpDir) fs.rmSync(_tmpDir, { recursive: true, force: true });
  });

  test('文件不存在 → 空 configs，无 warning', () => {
    const result = loadNamedConfigs(_tmpDir);
    assert.deepEqual(result.configs, {});
    assert.equal(result.warning, undefined);
  });

  test('文件损坏 → configs 空 + warning"配置合集文件损坏"', () => {
    fs.writeFileSync(collectionPath(_tmpDir), '{ broken json }}}', 'utf-8');
    const result = loadNamedConfigs(_tmpDir);
    assert.deepEqual(result.configs, {});
    assert.equal(result.warning, '配置合集文件损坏');
  });

  test('文件存在但缺 "configs" 顶层键（旧扁平格式/手工编辑）→ load 按损坏降级 + upsert 不写', () => {
    const flat = JSON.stringify({ A: { llm: { model: 'a' } } }); // 无包装层的旧格式
    fs.writeFileSync(collectionPath(_tmpDir), flat, 'utf-8');

    const result = loadNamedConfigs(_tmpDir);
    assert.deepEqual(result.configs, {}, '缺 configs 键 → 空合集降级');
    assert.equal(result.warning, '配置合集文件损坏');

    const res = upsertNamedConfig(_tmpDir, 'B', { llm: { model: 'b' } });
    assert.equal(res.ok, false);
    assert.equal(res.warning, '配置合集文件损坏未同步');
    assert.equal(fs.readFileSync(collectionPath(_tmpDir), 'utf-8'), flat, '原文件内容保留（不写）');
  });

  test('正常读取：条目原样返回', () => {
    const entryA = { llm: { model: 'a' }, lightrag_llm: { model: 'a-lt' } };
    fs.writeFileSync(collectionPath(_tmpDir), JSON.stringify({ configs: { A: entryA } }), 'utf-8');
    const result = loadNamedConfigs(_tmpDir);
    assert.deepEqual(result.configs, { A: entryA });
    assert.equal(result.warning, undefined);
  });

  test('写入文件顶层键恒为 "configs"（包装格式锁——跨端 parity 锚点）', () => {
    const res = upsertNamedConfig(_tmpDir, 'A', { llm: { model: 'a' }, lightrag_llm: {} });
    assert.equal(res.ok, true);

    const raw = JSON.parse(fs.readFileSync(collectionPath(_tmpDir), 'utf-8'));
    assert.deepEqual(Object.keys(raw), ['configs'], '顶层键恒为 configs');
    assert.deepEqual(Object.keys(raw.configs), ['A']);
    assert.deepEqual(raw.configs.A, { llm: { model: 'a' }, lightrag_llm: {} });

    // 二次 upsert 后顶层结构不变
    const res2 = upsertNamedConfig(_tmpDir, 'B', { llm: { model: 'b' } });
    assert.equal(res2.ok, true);
    const raw2 = JSON.parse(fs.readFileSync(collectionPath(_tmpDir), 'utf-8'));
    assert.deepEqual(Object.keys(raw2), ['configs']);
    assert.deepEqual(Object.keys(raw2.configs).sort(), ['A', 'B']);
  });

  test("name='__proto__' → 拒绝（配置名非法），不写文件", () => {
    const res = upsertNamedConfig(_tmpDir, '__proto__', { llm: { model: 'x' } });
    assert.equal(res.ok, false);
    assert.equal(res.warning, '配置名非法');
    assert.ok(!fs.existsSync(collectionPath(_tmpDir)), '文件未被创建');

    // 已有条目时同样拒绝且原文件不变
    fs.writeFileSync(collectionPath(_tmpDir), JSON.stringify({ configs: { A: { llm: {} } } }), 'utf-8');
    const res2 = upsertNamedConfig(_tmpDir, '__proto__', { llm: { model: 'x' } });
    assert.equal(res2.ok, false);
    assert.equal(res2.warning, '配置名非法');
    const reloaded = loadNamedConfigs(_tmpDir);
    assert.deepEqual(Object.keys(reloaded.configs), ['A'], '原条目不受影响');
  });

  test('upsert 新增单条：其它条目保留', () => {
    const entryA = { llm: { model: 'a' }, lightrag_llm: {} };
    fs.writeFileSync(collectionPath(_tmpDir), JSON.stringify({ configs: { A: entryA } }), 'utf-8');
    const entryB = { llm: { model: 'b' }, lightrag_llm: { model: 'b-lt' } };

    const res = upsertNamedConfig(_tmpDir, 'B', entryB);
    assert.equal(res.ok, true);
    assert.equal(res.warning, undefined);

    const reloaded = loadNamedConfigs(_tmpDir);
    assert.deepEqual(reloaded.configs.A, entryA, '其它条目保留');
    assert.deepEqual(reloaded.configs.B, entryB, '新条目写入');
  });

  test('upsert 覆盖同名条目：仅该条变化', () => {
    fs.writeFileSync(collectionPath(_tmpDir), JSON.stringify({ configs: { A: { llm: { model: 'old' } }, B: { llm: { model: 'b' } } } }), 'utf-8');
    const res = upsertNamedConfig(_tmpDir, 'A', { llm: { model: 'new' }, lightrag_llm: {} });
    assert.equal(res.ok, true);
    const reloaded = loadNamedConfigs(_tmpDir);
    assert.deepEqual(reloaded.configs.A, { llm: { model: 'new' }, lightrag_llm: {} }, '同名条目被覆盖');
    assert.deepEqual(reloaded.configs.B, { llm: { model: 'b' } }, '其它条目不受影响');
  });

  test('upsert 文件不存在：创建新合集（包装格式）', () => {
    const res = upsertNamedConfig(_tmpDir, 'A', { llm: { model: 'a' }, lightrag_llm: {} });
    assert.equal(res.ok, true);
    const reloaded = loadNamedConfigs(_tmpDir);
    assert.deepEqual(reloaded.configs, { A: { llm: { model: 'a' }, lightrag_llm: {} } });
  });

  test('文件损坏 → upsert 返回 ok:false + warning"配置合集文件损坏未同步"，且不写（原坏文件内容保留）', () => {
    const broken = '{ broken json }}}';
    fs.writeFileSync(collectionPath(_tmpDir), broken, 'utf-8');
    const res = upsertNamedConfig(_tmpDir, 'A', { llm: { model: 'a' } });
    assert.equal(res.ok, false);
    assert.equal(res.warning, '配置合集文件损坏未同步');
    assert.equal(fs.readFileSync(collectionPath(_tmpDir), 'utf-8'), broken, '原坏文件内容保留（不写）');
  });

  test('原子写：成功后无 .tmp 残留', () => {
    const res = upsertNamedConfig(_tmpDir, 'A', { llm: { model: 'a' }, lightrag_llm: {} });
    assert.equal(res.ok, true);
    const leftovers = fs.readdirSync(_tmpDir).filter(f => f.startsWith(COLLECTION_FILE + '.') && f.endsWith('.tmp'));
    assert.deepEqual(leftovers, [], '.tmp 文件不残留');
    assert.ok(fs.existsSync(collectionPath(_tmpDir)), '正式文件存在');
  });
});
