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
const { mergeConfig, isLightragCustomized } = require('../ui/main/lib/config-merge.js');

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

// ---------------------------------------------------------------------------
// C1 isLightragCustomized 判定矩阵 + mergeConfig C3/vision 恒基底
// 契约：docs/superpowers/plans/2026-09-11-settings-vision-lightrag-fix.md §0 C1/C3 + §4 T1
// ---------------------------------------------------------------------------

const LLM = { apiKey: 'ka', apiBase: 'http://a/v1', model: 'main-m', type: 'openai' };
const VISION_A = { apiKey: 'vk', apiBase: 'http://192.168.3.88:8080/v1', model: 'qwen38-xl' };
const FORM_VALUES = {
  llm: { apiKey: 'sk-form', apiBase: 'http://form/v1', model: 'form-model', type: 'openai', reasoning_effort: '', maxTokensRaw: '', thinking: '' },
  lightrag: { reasoning_effort: '', temperature: 0.2, thinking: '' },
  context: {}
};

describe('isLightragCustomized（C1 判定矩阵）', () => {
  test('lightrag_llm 全空/缺失 → false', () => {
    assert.equal(isLightragCustomized({ llm: LLM, lightrag_llm: {} }), false);
    assert.equal(isLightragCustomized({ llm: LLM }), false, '段缺失 → false');
    assert.equal(isLightragCustomized(null), false, 'fileBase 缺失 → false');
  });

  test('连接三键全空、页面三项有值（reasoning_effort/temperature/thinking）→ false（页面领土差异不算改过）', () => {
    const fb = { llm: LLM, lightrag_llm: { model: '', apiKey: '', apiBase: '', reasoning_effort: 'high', temperature: 0.3, litellm_kwargs: { thinking: { type: 'enabled' } } } };
    assert.equal(isLightragCustomized(fb), false);
  });

  test('程序产物键差异（capabilities/presetId/response_format_mode/allowed_openai_params，llm 无）→ false', () => {
    const fb = { llm: LLM, lightrag_llm: { capabilities: { model: 'x' }, presetId: '本地', litellm_kwargs: { response_format_mode: 'json_object', allowed_openai_params: ['a'] } } };
    assert.equal(isLightragCustomized(fb), false);
  });

  test('model 非空且 ≠ llm.model → true', () => {
    assert.equal(isLightragCustomized({ llm: LLM, lightrag_llm: { model: 'lt-m' } }), true);
  });

  test('apiKey 非空且 ≠ llm.apiKey → true', () => {
    assert.equal(isLightragCustomized({ llm: LLM, lightrag_llm: { apiKey: 'kl' } }), true);
  });

  test('apiBase 非空且 ≠ llm.apiBase → true', () => {
    assert.equal(isLightragCustomized({ llm: LLM, lightrag_llm: { apiBase: 'http://l/v1' } }), true);
  });

  test('litellm_kwargs 其余子键不一致（lightrag kwargs 有 max_tokens:1000，llm 无）→ true', () => {
    assert.equal(isLightragCustomized({ llm: LLM, lightrag_llm: { litellm_kwargs: { max_tokens: 1000 } } }), true);
  });

  test('空值归一化：lightrag.apiBase="" vs llm 有值 → false；kwargs 子键="" vs llm 缺失 → false', () => {
    const fb = { llm: LLM, lightrag_llm: { apiBase: '', litellm_kwargs: { max_tokens: '' } } };
    assert.equal(isLightragCustomized(fb), false);
  });

  test('嵌套深比较：extra_headers 同内容不同引用 → false', () => {
    const fb = { llm: { ...LLM, litellm_kwargs: { extra_headers: { a: 1 } } }, lightrag_llm: { litellm_kwargs: { extra_headers: { a: 1 } } } };
    assert.equal(isLightragCustomized(fb), false);
  });

  test('非空等值：lightrag.model 非空且 == llm.model（主 Agent 显式写同值）→ false', () => {
    assert.equal(isLightragCustomized({ llm: LLM, lightrag_llm: { model: 'main-m' } }), false);
  });

  test('model 空但其余键非空（max_tokens=8192，llm 无）→ true（防 model-empty 特例）', () => {
    assert.equal(isLightragCustomized({ llm: LLM, lightrag_llm: { model: '', max_tokens: 8192 } }), true);
  });
});

describe('mergeConfig（C3 customized 透传 / vision 恒基底 / 非 customized 回归）', () => {
  test('vision_llm 恒基底：namedEntry 带 vision_llm + fileBase 带 vision_llm → 输出 = fileBase 值', () => {
    const out = mergeConfig({
      fileBase: { llm: LLM, lightrag_llm: {}, vision_llm: VISION_A, context: {} },
      namedEntry: { llm: { model: 'entry-main' }, lightrag_llm: {}, vision_llm: { model: 'entry-vision' } },
      formValues: FORM_VALUES,
      probeResults: null,
      configName: '条目名'
    });
    assert.deepEqual(out.vision_llm, VISION_A);
  });

  test('customized 透传：customized fileBase + ltForm 三项有值 + probeResults 非 null + namedEntry.lightrag_llm 有值 → 输出 lightrag 段 = fileBase 原样', () => {
    const ltCustom = { model: 'lt-custom', apiKey: 'kl', max_tokens: 4096, litellm_kwargs: { max_tokens: 1000 } };
    const out = mergeConfig({
      fileBase: { llm: LLM, lightrag_llm: ltCustom, vision_llm: {}, context: {} },
      namedEntry: { llm: { model: 'entry-main' }, lightrag_llm: { model: 'entry-lt' } },
      formValues: { ...FORM_VALUES, lightrag: { reasoning_effort: 'high', temperature: 0.5, thinking: 'enabled' } },
      probeResults: { response_format_mode: 'json_object', allowed_openai_params: ['x'] },
      configName: '条目名'
    });
    assert.deepEqual(out.lightrag_llm, ltCustom, '三项覆盖/probe 覆写/条目基底均不生效');
  });

  test('非 customized 回归：ltForm 覆盖 + probe 覆写 + namedEntry 基底（现状行为）', () => {
    const out = mergeConfig({
      fileBase: { llm: LLM, lightrag_llm: {}, context: {} },
      namedEntry: { llm: { model: 'entry-main' }, lightrag_llm: { model: 'entry-lt', apiBase: 'http://e/v1' } },
      formValues: { ...FORM_VALUES, lightrag: { reasoning_effort: 'high', temperature: 0.5, thinking: 'enabled' } },
      probeResults: { response_format_mode: 'json_object' },
      configName: '条目名'
    });
    assert.deepEqual(out.lightrag_llm, {
      model: 'entry-lt', apiBase: 'http://e/v1', reasoning_effort: 'high', temperature: 0.5,
      litellm_kwargs: { thinking: { type: 'enabled' }, response_format_mode: 'json_object' }
    });
  });
});
