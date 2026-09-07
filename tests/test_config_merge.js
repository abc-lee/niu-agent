// 测试用 node:test 原生 API（不依赖 jest），断言用 node:assert/strict
// 契约：docs/superpowers/plans/2026-09-07-llm-named-configs.md §0 C1 + T1「mergeConfig」段
const { test, describe } = require('node:test');
const assert = require('node:assert/strict');

const { mergeConfig } = require('../ui/main/lib/config-merge.js');

// 文件基底（恒提供）：含各段额外键，验证"保留不丢"
function fileBase() {
  return {
    firstRun: true,
    llm: {
      apiKey: 'FILE_KEY', apiBase: 'http://file', model: 'file-model', type: 'openai',
      reasoning_effort: '', max_tokens: 4096, read_timeout: 220, custom_agent_field: 7,
      litellm_kwargs: { thinking: { type: 'file' }, other_kwarg: 1 }
    },
    lightrag_llm: {
      model: 'file-lt', temperature: 0.5, reasoning_effort: 'high', extra_lt_key: 'keep',
      litellm_kwargs: { response_format_mode: 'old', allowed_openai_params: ['x'], lt_extra: 2 }
    },
    context: {
      contextWindowSize: 131072, warningThreshold: 0.85, keepRecentTurns: 4, sleepTriggerMinutes: 5,
      ctx_extra: 'keep'
    },
    storage: { mode: 'file', extra: 1 },
    logging: { level: 'info' },
    Agent: { someAgentField: 'agent-kept' }
  };
}

// 合集条目（loadedNamedEntry）：含段额外键 + 干扰性顶级段（验证"恒取 fileBase"不被条目带偏）
function namedEntry() {
  return {
    llm: {
      apiKey: 'ENTRY_KEY', apiBase: 'http://entry', model: 'entry-model', type: 'openai',
      reasoning_effort: '', entry_extra: 'from-entry', litellm_kwargs: {}
    },
    lightrag_llm: {
      model: 'entry-lt', temperature: 0.9, reasoning_effort: '', lt_entry_extra: 'from-entry'
    },
    storage: { mode: 'entry-should-be-ignored' },
    context: { contextWindowSize: 999, entry_ctx_extra: 'should-be-ignored' }
  };
}

// 表单值（默认全空哨兵：maxTokensRaw ''/thinking ''/reasoning_effort ''）
function formValues(over = {}) {
  return {
    llm: Object.assign({ apiKey: 'FORM_KEY', apiBase: 'http://form', model: 'form-model', type: 'openai', reasoning_effort: '', maxTokensRaw: '', thinking: '' }, over.llm || {}),
    lightrag: Object.assign({ reasoning_effort: '', temperature: 0.7, thinking: '' }, over.lightrag || {}),
    context: { contextWindowSize: 262144, warningThreshold: 0.9, keepRecentTurns: 6, sleepTriggerMinutes: 8 }
  };
}

describe('mergeConfig', () => {

  test('namedEntry 非 null → llm/lightrag_llm 段基底=条目；storage/logging/Agent 顶级段恒=fileBase；context 基底恒=fileBase.context（R1-A P1-1 回归锁）', () => {
    const fb = fileBase();
    const ne = namedEntry();
    const out = mergeConfig({ fileBase: fb, namedEntry: ne, formValues: formValues(), probeResults: null, configName: 'my' });

    // llm/lightrag_llm 段基底=条目（表单未覆盖的键取条目值）
    assert.equal(out.llm.entry_extra, 'from-entry', 'llm 段基底应为条目');
    assert.equal(out.lightrag_llm.model, 'entry-lt', 'lightrag_llm 段基底应为条目');
    assert.equal(out.lightrag_llm.lt_entry_extra, 'from-entry', 'lightrag_llm 段基底应为条目（额外键）');

    // storage/logging/Agent 顶级段恒=fileBase（条目里的干扰段不得渗入）
    assert.deepEqual(out.storage, { mode: 'file', extra: 1 }, 'storage 恒取 fileBase');
    assert.deepEqual(out.logging, { level: 'info' }, 'logging 恒取 fileBase');
    assert.deepEqual(out.Agent, { someAgentField: 'agent-kept' }, 'Agent 顶级段保留 fileBase');

    // context 基底恒=fileBase.context（条目干扰键不渗入，文件额外键保留）
    assert.equal(out.context.ctx_extra, 'keep', 'context 基底应为 fileBase.context');
    assert.ok(!('entry_ctx_extra' in out.context), '条目 context 干扰键不得渗入');
  });

  test('namedEntry null → llm/lightrag_llm 段基底=fileBase', () => {
    const fb = fileBase();
    const out = mergeConfig({ fileBase: fb, namedEntry: null, formValues: formValues(), probeResults: null, configName: 'my' });
    assert.equal(out.llm.read_timeout, 220, 'llm 段基底应为 fileBase');
    assert.equal(out.llm.custom_agent_field, 7, 'llm 段基底应为 fileBase（额外键）');
    assert.equal(out.lightrag_llm.extra_lt_key, 'keep', 'lightrag_llm 段基底应为 fileBase');
    assert.equal(out.lightrag_llm.model, 'file-lt', 'lightrag_llm 段基底应为 fileBase');
  });

  test('表单五键覆盖（apiKey/apiBase/model/type/reasoning_effort，优先级高于条目）', () => {
    const out = mergeConfig({ fileBase: fileBase(), namedEntry: namedEntry(), formValues: formValues(), probeResults: null, configName: 'my' });
    assert.equal(out.llm.apiKey, 'FORM_KEY');
    assert.equal(out.llm.apiBase, 'http://form');
    assert.equal(out.llm.model, 'form-model');
    assert.equal(out.llm.type, 'openai');
    assert.equal(out.llm.reasoning_effort, '', 'reasoning_effort 空值写 ""（合法哨兵）');
  });

  test("max_tokens：表单空串 → 删键（基底有也删）；有值 → parseInt 写数值", () => {
    const outEmpty = mergeConfig({ fileBase: fileBase(), namedEntry: null, formValues: formValues(), probeResults: null, configName: 'my' });
    assert.ok(!('max_tokens' in outEmpty.llm), "'' 应删键（基底 max_tokens=4096 不得保留）");

    const outVal = mergeConfig({ fileBase: fileBase(), namedEntry: null, formValues: formValues({ llm: { maxTokensRaw: '8192' } }), probeResults: null, configName: 'my' });
    assert.equal(outVal.llm.max_tokens, 8192);
    assert.equal(typeof outVal.llm.max_tokens, 'number');
  });

  test("thinking：空串 → 删子键（基底有也删）；有值 → {type: 值}（llm/lightrag_llm 两段同规则）", () => {
    const outEmpty = mergeConfig({ fileBase: fileBase(), namedEntry: null, formValues: formValues(), probeResults: null, configName: 'my' });
    assert.ok(!('thinking' in outEmpty.llm.litellm_kwargs), "llm thinking '' 应删子键（基底有 {type:'file'}）");
    assert.ok(!('thinking' in outEmpty.lightrag_llm.litellm_kwargs), "lightrag_llm thinking '' 应删子键");

    const outVal = mergeConfig({ fileBase: fileBase(), namedEntry: null, formValues: formValues({ llm: { thinking: 'enabled' }, lightrag: { thinking: 'disabled' } }), probeResults: null, configName: 'my' });
    assert.deepEqual(outVal.llm.litellm_kwargs.thinking, { type: 'enabled' });
    assert.deepEqual(outVal.lightrag_llm.litellm_kwargs.thinking, { type: 'disabled' });
  });

  test('temperature：NaN → 基底值 ?? 0.2；有效值恒写数值', () => {
    // 条目基底有 temperature=0.9 → NaN 回退基底
    const outBase = mergeConfig({ fileBase: fileBase(), namedEntry: namedEntry(), formValues: formValues({ lightrag: { temperature: NaN } }), probeResults: null, configName: 'my' });
    assert.equal(outBase.lightrag_llm.temperature, 0.9, 'NaN → 基底值');

    // 基底无 temperature（fileBase 段删掉该键）→ NaN 回退 0.2
    const fb = fileBase();
    delete fb.lightrag_llm.temperature;
    const outDefault = mergeConfig({ fileBase: fb, namedEntry: null, formValues: formValues({ lightrag: { temperature: NaN } }), probeResults: null, configName: 'my' });
    assert.equal(outDefault.lightrag_llm.temperature, 0.2, 'NaN 且基底无值 → 0.2');

    // 有效值恒写数值（字符串也转数值）
    const outVal = mergeConfig({ fileBase: fileBase(), namedEntry: null, formValues: formValues({ lightrag: { temperature: '0.3' } }), probeResults: null, configName: 'my' });
    assert.equal(outVal.lightrag_llm.temperature, 0.3);
    assert.equal(typeof outVal.lightrag_llm.temperature, 'number');
  });

  test('probeResults：null → 不触碰产物键（基底旧值保留 + 无基底时不铺底）；非 null → 覆写两键', () => {
    const outNull = mergeConfig({ fileBase: fileBase(), namedEntry: null, formValues: formValues(), probeResults: null, configName: 'my' });
    assert.equal(outNull.lightrag_llm.litellm_kwargs.response_format_mode, 'old', 'null 时基底旧值保留');
    assert.deepEqual(outNull.lightrag_llm.litellm_kwargs.allowed_openai_params, ['x'], 'null 时基底旧值保留');

    // 不铺底：基底无产物键 + probeResults null → 结果也不得出现该键
    const fb = fileBase();
    delete fb.lightrag_llm.litellm_kwargs.response_format_mode;
    delete fb.lightrag_llm.litellm_kwargs.allowed_openai_params;
    const outNoSeed = mergeConfig({ fileBase: fb, namedEntry: null, formValues: formValues(), probeResults: null, configName: 'my' });
    assert.ok(!('response_format_mode' in outNoSeed.lightrag_llm.litellm_kwargs), '不铺底');
    assert.ok(!('allowed_openai_params' in outNoSeed.lightrag_llm.litellm_kwargs), '不铺底');

    const outProbe = mergeConfig({ fileBase: fileBase(), namedEntry: null, formValues: formValues(), probeResults: { response_format_mode: 'json', allowed_openai_params: ['a', 'b'] }, configName: 'my' });
    assert.equal(outProbe.lightrag_llm.litellm_kwargs.response_format_mode, 'json');
    assert.deepEqual(outProbe.lightrag_llm.litellm_kwargs.allowed_openai_params, ['a', 'b']);
  });

  test('presetId = configName（独立参数）', () => {
    const out = mergeConfig({ fileBase: fileBase(), namedEntry: null, formValues: formValues(), probeResults: null, configName: '豆包-深度' });
    assert.equal(out.llm.presetId, '豆包-深度');
  });

  test('context：4 表单键覆盖 + 文件内其余键保留', () => {
    const out = mergeConfig({ fileBase: fileBase(), namedEntry: null, formValues: formValues(), probeResults: null, configName: 'my' });
    assert.equal(out.context.contextWindowSize, 262144);
    assert.equal(out.context.warningThreshold, 0.9);
    assert.equal(out.context.keepRecentTurns, 6);
    assert.equal(out.context.sleepTriggerMinutes, 8);
    assert.equal(out.context.ctx_extra, 'keep', '文件内其余键保留');
  });

  test('firstRun 恒 false（fileBase=true 也置 false；fileBase 无该键也写入 false）', () => {
    const out = mergeConfig({ fileBase: fileBase(), namedEntry: null, formValues: formValues(), probeResults: null, configName: 'my' });
    assert.equal(out.firstRun, false);
    const fb = fileBase();
    delete fb.firstRun;
    const out2 = mergeConfig({ fileBase: fb, namedEntry: null, formValues: formValues(), probeResults: null, configName: 'my' });
    assert.equal(out2.firstRun, false);
  });

  test('Agent 顶级段保留（fileBase 原样）', () => {
    const out = mergeConfig({ fileBase: fileBase(), namedEntry: namedEntry(), formValues: formValues(), probeResults: null, configName: 'my' });
    assert.deepEqual(out.Agent, { someAgentField: 'agent-kept' });
  });

  test('llm 段 base 额外键保留（read_timeout/custom_agent_field + litellm_kwargs 其余子键）', () => {
    const out = mergeConfig({ fileBase: fileBase(), namedEntry: null, formValues: formValues(), probeResults: null, configName: 'my' });
    assert.equal(out.llm.read_timeout, 220);
    assert.equal(out.llm.custom_agent_field, 7);
    assert.equal(out.llm.litellm_kwargs.other_kwarg, 1, 'litellm_kwargs 其余子键保留');
  });

  test('lightrag_llm 段 base 额外键保留（顶级 + litellm_kwargs 子键）', () => {
    const out = mergeConfig({ fileBase: fileBase(), namedEntry: null, formValues: formValues(), probeResults: null, configName: 'my' });
    assert.equal(out.lightrag_llm.extra_lt_key, 'keep');
    assert.equal(out.lightrag_llm.litellm_kwargs.lt_extra, 2);
  });

  test('基底缺段不炸（fileBase 空对象 / 条目缺 lightrag_llm 段）', () => {
    const outEmpty = mergeConfig({ fileBase: {}, namedEntry: null, formValues: formValues({ lightrag: { temperature: NaN } }), probeResults: null, configName: 'my' });
    assert.equal(outEmpty.llm.model, 'form-model');
    assert.equal(outEmpty.lightrag_llm.temperature, 0.2, '无基底 + NaN → 0.2（不炸）');
    assert.equal(outEmpty.firstRun, false);

    const ne = namedEntry();
    delete ne.lightrag_llm;
    const outNoLt = mergeConfig({ fileBase: fileBase(), namedEntry: ne, formValues: formValues(), probeResults: null, configName: 'my' });
    assert.equal(outNoLt.lightrag_llm.model, 'file-lt', '条目缺段 → 回退 fileBase 段');
  });

  test('函数纯：不改任何入参', () => {
    const fb = fileBase();
    const ne = namedEntry();
    const fv = formValues({ llm: { maxTokensRaw: '8192', thinking: 'enabled' }, lightrag: { thinking: 'disabled' } });
    const probe = { response_format_mode: 'json', allowed_openai_params: ['a'] };
    const fbCopy = JSON.parse(JSON.stringify(fb));
    const neCopy = JSON.parse(JSON.stringify(ne));
    const fvCopy = JSON.parse(JSON.stringify(fv));
    const probeCopy = JSON.parse(JSON.stringify(probe));

    mergeConfig({ fileBase: fb, namedEntry: ne, formValues: fv, probeResults: probe, configName: 'my' });

    assert.deepEqual(fb, fbCopy, 'fileBase 未被修改');
    assert.deepEqual(ne, neCopy, 'namedEntry 未被修改');
    assert.deepEqual(fv, fvCopy, 'formValues 未被修改');
    assert.deepEqual(probe, probeCopy, 'probeResults 未被修改');
  });

  test('UMD：Node require 导出 mergeConfig 函数', () => {
    assert.equal(typeof mergeConfig, 'function');
  });
});
