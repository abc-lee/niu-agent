// save-config IPC 核心逻辑（lib/save-config.js）定向测试——node:test 原生 API，断言 node:assert/strict
// 契约：docs/superpowers/plans/2026-09-07-llm-named-configs.md §1 T2（R1-A P2-3：tmp 目录 + stub notifyReload）
const { test, describe, beforeEach, after } = require('node:test');
const assert = require('node:assert/strict');
const path = require('path');
const fs = require('fs');
const os = require('os');

const { saveConfigAndCollection } = require('../ui/main/lib/save-config.js');

let _tmpDir;
function freshTmpNiu() {
  _tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'niu-save-cfg-test-'));
  return _tmpDir;
}

function writeJson(dir, filename, content) {
  const p = path.join(dir, filename);
  fs.mkdirSync(path.dirname(p), { recursive: true });
  if (typeof content === 'string') {
    fs.writeFileSync(p, content, 'utf-8');
  } else {
    fs.writeFileSync(p, JSON.stringify(content, null, 2), 'utf-8');
  }
}

function readJson(dir, filename) {
  return JSON.parse(fs.readFileSync(path.join(dir, filename), 'utf-8'));
}

// C1 formValues 标准结构（测试用固定值）
const FORM = {
  llm: { apiKey: 'sk-form', apiBase: 'http://form-base/v1', model: 'form-model', type: 'openai', reasoning_effort: 'high', maxTokensRaw: '', thinking: '' },
  lightrag: { reasoning_effort: '', temperature: 0.3, thinking: '' },
  context: { contextWindowSize: 200000, warningThreshold: 0.8, keepRecentTurns: 6, sleepTriggerMinutes: 5 }
};

describe('saveConfigAndCollection', () => {
  beforeEach(() => { _tmpDir = freshTmpNiu(); });
  after(() => {
    if (_tmpDir) fs.rmSync(_tmpDir, { recursive: true, force: true });
  });

  test('①正常双写 + reload 一次', () => {
    const dir = _tmpDir;
    writeJson(dir, 'user-config.json', { llm: { apiKey: 'sk-old', model: 'old-model' }, firstRun: true });
    let reloadCount = 0;

    const result = saveConfigAndCollection({
      niuConfigDir: dir, configName: 'alpha', formValues: FORM, probeResults: null,
      loadedNamedEntry: null, notifyReload: () => { reloadCount++; }
    });

    assert.equal(result.success, true);
    assert.equal(result.collectionWarning, undefined);
    assert.equal(reloadCount, 1, 'reload 恰好一次');

    const userCfg = readJson(dir, 'user-config.json');
    assert.equal(userCfg.llm.presetId, 'alpha');
    assert.equal(userCfg.llm.model, 'form-model');
    assert.equal(userCfg.firstRun, false);

    const collection = readJson(dir, 'llm-configs.json');
    assert.deepEqual(Object.keys(collection), ['configs'], '顶层键恒为 configs（spec §3.1 包装格式）');
    assert.ok(collection.configs.alpha, '合集应出现 alpha 条目');
    assert.deepEqual(Object.keys(collection.configs.alpha).sort(), ['lightrag_llm', 'llm', 'vision_llm'], '条目=三段快照（T2：新增 vision_llm）');
    assert.equal(collection.configs.alpha.llm.model, 'form-model', '条目 llm 段=user-config 写入值');
    assert.deepEqual(collection.configs.alpha.vision_llm, {}, 'user-config 无 vision_llm 段→基底透传空对象（config-merge.js:85）');
  });

  test('②合集损坏→collectionWarning+原坏文件内容保留+reload 仍发一次', () => {
    const dir = _tmpDir;
    writeJson(dir, 'user-config.json', { llm: { apiKey: 'sk-old', model: 'old-model' } });
    const broken = '{ broken json }}}';
    writeJson(dir, 'llm-configs.json', broken);
    let reloadCount = 0;

    const result = saveConfigAndCollection({
      niuConfigDir: dir, configName: 'alpha', formValues: FORM, probeResults: null,
      loadedNamedEntry: null, notifyReload: () => { reloadCount++; }
    });

    assert.equal(result.success, true);
    assert.equal(result.collectionWarning, '配置合集文件损坏未同步');
    assert.equal(reloadCount, 1, 'warning 路径 reload 仍发一次');
    assert.equal(fs.readFileSync(path.join(dir, 'llm-configs.json'), 'utf-8'), broken, '原坏文件内容保留不写');
    const userCfg = readJson(dir, 'user-config.json');
    assert.equal(userCfg.llm.presetId, 'alpha', 'user-config 正常写入');
  });

  test('③configName 空/纯空白→success:false 不写文件', () => {
    for (const badName of ['', '   ']) {
      const dir = freshTmpNiu();
      let reloadCount = 0;

      const result = saveConfigAndCollection({
        niuConfigDir: dir, configName: badName, formValues: FORM, probeResults: null,
        loadedNamedEntry: null, notifyReload: () => { reloadCount++; }
      });

      assert.equal(result.success, false, `configName=${JSON.stringify(badName)} 应失败`);
      assert.ok(result.error, '应带 error');
      assert.equal(fs.existsSync(path.join(dir, 'user-config.json')), false, '不写 user-config.json');
      assert.equal(fs.existsSync(path.join(dir, 'llm-configs.json')), false, '不写合集');
      assert.equal(reloadCount, 0, '不发 reload');
    }
  });

  test('④已加载条目保存后 fileBase 的 storage/Agent 顶级段仍在 user-config.json（两层基底接线锁）', () => {
    const dir = _tmpDir;
    const fileBase = {
      llm: { apiKey: 'sk-file', apiBase: 'http://file-base/v1', model: 'file-model', type: 'openai' },
      lightrag_llm: { model: 'file-lt' },
      context: { contextWindowSize: 100000 },
      storage: { maxSizeMb: 512, custom: true },
      logging: { enabled: false, level: 'INFO' },
      Agent: { persona: 'niu', extra: 42 },
      firstRun: true
    };
    writeJson(dir, 'user-config.json', fileBase);
    const loadedNamedEntry = {
      llm: { apiKey: 'sk-entry', apiBase: 'http://entry-base/v1', model: 'entry-model', type: 'openai', read_timeout: 220 },
      lightrag_llm: { model: 'entry-lt', temperature: 0.5 }
    };

    const result = saveConfigAndCollection({
      niuConfigDir: dir, configName: 'beta', formValues: FORM, probeResults: null,
      loadedNamedEntry, notifyReload: () => {}
    });

    assert.equal(result.success, true);
    const userCfg = readJson(dir, 'user-config.json');
    assert.deepEqual(userCfg.storage, fileBase.storage, 'storage 顶级段恒=fileBase');
    assert.deepEqual(userCfg.Agent, fileBase.Agent, 'Agent 顶级段恒=fileBase');
    assert.equal(userCfg.llm.read_timeout, 220, 'llm 段基底=条目（额外键保留）');
    assert.equal(userCfg.llm.model, 'form-model', '表单值覆盖条目');
    assert.equal(userCfg.lightrag_llm.temperature, 0.3, 'lightrag 表单 temperature 覆盖');
  });

  test('⑤upsert 非解析类异常（磁盘满/权限）→ collectionWarning"配置合集写入失败: ..." + reload 仍发一次 + user-config 已落盘', () => {
    const dir = _tmpDir;
    writeJson(dir, 'user-config.json', { llm: { apiKey: 'sk-old', model: 'old-model' } });

    // save-config.js 在 require 时解构引用 upsertNamedConfig → 换模块缓存后重新 require 才能注入 stub
    const ncPath = require.resolve('../ui/main/lib/named-configs.js');
    const scPath = require.resolve('../ui/main/lib/save-config.js');
    const realNcEntry = require.cache[ncPath];
    let reloadCount = 0;
    try {
      require.cache[ncPath] = {
        id: ncPath, filename: ncPath, loaded: true,
        exports: {
          upsertNamedConfig: () => { throw new Error('ENOSPC: no space left on device'); },
          loadNamedConfigs: () => ({ configs: {} }),
          COLLECTION_FILE: 'llm-configs.json'
        }
      };
      delete require.cache[scPath];
      const { saveConfigAndCollection } = require(scPath);

      const result = saveConfigAndCollection({
        niuConfigDir: dir, configName: 'alpha', formValues: FORM, probeResults: null,
        loadedNamedEntry: null, notifyReload: () => { reloadCount++; }
      });

      assert.equal(result.success, true, 'upsert 抛异常不阻断保存（主配置已落盘）');
      assert.equal(result.collectionWarning, '配置合集写入失败: ENOSPC: no space left on device');
      assert.equal(reloadCount, 1, '异常路径 reload 仍恒发一次');
      const userCfg = readJson(dir, 'user-config.json');
      assert.equal(userCfg.llm.presetId, 'alpha', 'user-config 正常写入');
    } finally {
      require.cache[ncPath] = realNcEntry;
      delete require.cache[scPath];
    }
  });
});
