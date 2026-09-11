// vision_llm 段 JS 保护链测试（plan 2026-09-11-settings-vision-lightrag-fix.md §4 T1）。
// 覆盖：config-merge.js mergeConfig 恒基底透传（namedEntry 快照一律忽略，零表单输入）
//       + save-config.js saveConfigAndCollection upsert 两段快照。
// 运行：node --test tests/js/
'use strict';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const { mergeConfig } = require('../../ui/main/lib/config-merge.js');
const { saveConfigAndCollection } = require('../../ui/main/lib/save-config.js');

// 设置页表单最小值（vision_llm 无表单——formValues 不含该段）
const FORM = {
  llm: { apiKey: 'k1', apiBase: 'https://api.main/v1', model: 'main-model', type: 'openai', reasoning_effort: '', maxTokensRaw: '', thinking: '' },
  lightrag: { reasoning_effort: '', temperature: 0.2, thinking: '' },
  context: {}
};

const VISION = { apiKey: 'vk', apiBase: 'http://192.168.3.88:8080/v1', model: 'qwen38-xl', max_tokens: 8192 };

test('mergeConfig: fileBase.vision_llm 透传（namedEntry=null，主 Agent 手工配的段保存不丢）', () => {
  const out = mergeConfig({
    fileBase: { llm: { model: 'old' }, lightrag_llm: {}, vision_llm: VISION, context: {} },
    namedEntry: null,
    formValues: FORM,
    probeResults: null,
    configName: '主配置'
  });
  assert.deepStrictEqual(out.vision_llm, VISION);
});

test('mergeConfig: namedEntry.vision_llm 忽略（恒基底——合集不再携带 vision_llm 段）', () => {
  const entryVision = { model: 'entry-vision', apiBase: 'http://entry/v1' };
  const out = mergeConfig({
    fileBase: { llm: {}, lightrag_llm: {}, vision_llm: VISION, context: {} },
    namedEntry: { llm: { model: 'entry-main' }, lightrag_llm: {}, vision_llm: entryVision },
    formValues: FORM,
    probeResults: null,
    configName: '条目名'
  });
  assert.deepStrictEqual(out.vision_llm, VISION);
});

test('mergeConfig: namedEntry 有 vision_llm 键同样忽略（存量三段条目兼容）', () => {
  const out = mergeConfig({
    fileBase: { llm: {}, lightrag_llm: {}, vision_llm: VISION, context: {} },
    namedEntry: { llm: { model: 'entry-main' }, lightrag_llm: {}, vision_llm: { model: 'legacy-vision' } },
    formValues: FORM,
    probeResults: null,
    configName: '旧条目'
  });
  assert.deepStrictEqual(out.vision_llm, VISION);
});

test('mergeConfig: 两侧均无 vision_llm → 空对象（不引入键值噪音之外的行为）', () => {
  const out = mergeConfig({
    fileBase: { llm: {}, lightrag_llm: {}, context: {} },
    namedEntry: null,
    formValues: FORM,
    probeResults: null,
    configName: 'x'
  });
  assert.deepStrictEqual(out.vision_llm, {});
});

test('saveConfigAndCollection: user-config.json 保留 vision_llm + 合集条目两段快照', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'niu-vision-test-'));
  try {
    fs.writeFileSync(path.join(dir, 'user-config.json'), JSON.stringify({
      llm: { presetId: '本地', model: 'main-model' },
      lightrag_llm: {},
      vision_llm: VISION,
      context: {}
    }));

    const reloads = [];
    const res = saveConfigAndCollection({
      niuConfigDir: dir,
      configName: '本地',
      formValues: FORM,
      probeResults: null,
      loadedNamedEntry: null,
      notifyReload: () => reloads.push(1)
    });
    assert.strictEqual(res.success, true);

    // ④ user-config.json：vision_llm 段原样保留（fileBase 透传）
    const saved = JSON.parse(fs.readFileSync(path.join(dir, 'user-config.json'), 'utf-8'));
    assert.deepStrictEqual(saved.vision_llm, VISION);

    // ⑤ 合集条目 = {llm, lightrag_llm} 两段快照（vision_llm 不入合集）
    const coll = JSON.parse(fs.readFileSync(path.join(dir, 'llm-configs.json'), 'utf-8'));
    const entry = coll.configs['本地'];
    assert.deepStrictEqual(Object.keys(entry).sort(), ['lightrag_llm', 'llm']);

    // ⑥ reload 恰一次
    assert.strictEqual(reloads.length, 1);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('saveConfigAndCollection: loadedNamedEntry 带 vision_llm → 切换后 user-config 仍取 fileBase 顶层（条目段忽略）', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'niu-vision-test-'));
  try {
    fs.writeFileSync(path.join(dir, 'user-config.json'), JSON.stringify({
      llm: { presetId: 'A', model: 'main-a' },
      lightrag_llm: {},
      vision_llm: VISION,
      context: {}
    }));

    const entryVision = { model: 'entry-vision' };
    const res = saveConfigAndCollection({
      niuConfigDir: dir,
      configName: 'B',
      formValues: FORM,
      probeResults: null,
      loadedNamedEntry: { llm: { model: 'main-b' }, lightrag_llm: {}, vision_llm: entryVision },
      notifyReload: () => {}
    });
    assert.strictEqual(res.success, true);

    const saved = JSON.parse(fs.readFileSync(path.join(dir, 'user-config.json'), 'utf-8'));
    assert.deepStrictEqual(saved.vision_llm, VISION, 'vision_llm 恒基底——条目快照不覆盖顶层');

    const coll = JSON.parse(fs.readFileSync(path.join(dir, 'llm-configs.json'), 'utf-8'));
    assert.deepStrictEqual(Object.keys(coll.configs['B']).sort(), ['lightrag_llm', 'llm'], '新条目两段快照，无 vision_llm 键');
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
