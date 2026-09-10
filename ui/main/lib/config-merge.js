// 命名配置合并纯函数（UMD：main.js require / index.html script src 双通道）
// 契约：docs/superpowers/plans/2026-09-07-llm-named-configs.md §0 C1
// 单一事实源：spec §3.2.1 各段组装细则（docs/superpowers/specs/2026-09-07-llm-named-configs-design.md）

(function (root, factory) {
  if (typeof exports === 'object' && typeof module !== 'undefined') {
    module.exports = factory();
  } else {
    root.ConfigMerge = factory();
  }
})(typeof self !== 'undefined' ? self : this, function () {

  // litellm_kwargs = 基底子键浅拷贝 + thinking（''/空 → 删子键；否则写 {type: 值}）
  function buildKwargs(baseKwargs, thinking) {
    const kwargs = baseKwargs && typeof baseKwargs === 'object' ? { ...baseKwargs } : {};
    if (thinking === '' || thinking === null || thinking === undefined) {
      delete kwargs.thinking;
    } else {
      kwargs.thinking = { type: thinking };
    }
    return kwargs;
  }

  /**
   * 合并文件配置 / 已加载命名条目 / 表单值 → 新的 user-config 对象。
   * 纯函数：不改任何入参，返回新对象。
   *
   * 两层基底（表单值 > namedEntry > fileBase）：
   * - llm / lightrag_llm / vision_llm 段基底 = namedEntry?.段 ?? fileBase.段
   *   （vision_llm 无表单输入，恒为基底透传——主 Agent 手工配的段不随保存/切换丢失）
   * - storage/logging/context/Agent 等其余顶级段恒取 fileBase（全局设置不随切换丢失）
   *
   * @param {object} args
   * @param {object} args.fileBase          保存时刻重读的 user-config.json 全文（恒提供；缺段按空对象处理）
   * @param {?{llm?: object, lightrag_llm?: object, vision_llm?: object}} args.namedEntry  loadedNamedEntry：null | 合集条目
   * @param {object} args.formValues        { llm: {apiKey, apiBase, model, type, reasoning_effort, maxTokensRaw, thinking},
   *                                          lightrag: {reasoning_effort, temperature, thinking},
   *                                          context: {contextWindowSize, warningThreshold, keepRecentTurns, sleepTriggerMinutes} }
   * @param {?{response_format_mode: *, allowed_openai_params: *}} args.probeResults  探测产物；null = 不触碰产物键（含不铺底）
   * @param {string} args.configName        命名配置名 → llm.presetId
   * @returns {object} newConfig
   */
  function mergeConfig({ fileBase, namedEntry, formValues, probeResults, configName }) {
    const base = (fileBase && typeof fileBase === 'object') ? fileBase : {};
    const entry = (namedEntry && typeof namedEntry === 'object') ? namedEntry : null;
    const fv = (formValues && typeof formValues === 'object') ? formValues : {};
    const llmForm = (fv.llm && typeof fv.llm === 'object') ? fv.llm : {};
    const ltForm = (fv.lightrag && typeof fv.lightrag === 'object') ? fv.lightrag : {};
    const ctxForm = (fv.context && typeof fv.context === 'object') ? fv.context : {};

    // --- llm 段：段基底浅拷贝 + 表单五键覆盖 + max_tokens + litellm_kwargs(thinking) + presetId
    const llmBase = (entry && entry.llm && typeof entry.llm === 'object') ? entry.llm : ((base.llm && typeof base.llm === 'object') ? base.llm : {});
    const llm = { ...llmBase };
    for (const k of ['apiKey', 'apiBase', 'model', 'type', 'reasoning_effort']) {
      if (llmForm[k] !== undefined) llm[k] = llmForm[k];
    }
    // max_tokens：''/空 → 删键（可选管理键空值语义，维持现状）；否则 parseInt 写数值
    const maxTokensRaw = llmForm.maxTokensRaw;
    if (maxTokensRaw === '' || maxTokensRaw === null || maxTokensRaw === undefined) {
      delete llm.max_tokens;
    } else {
      llm.max_tokens = parseInt(maxTokensRaw, 10);
    }
    llm.litellm_kwargs = buildKwargs(llmBase.litellm_kwargs, llmForm.thinking);
    llm.presetId = configName;
    // capabilities 键仅当 fileBase.llm.capabilities.model === 合并后 llm.model 才继承
    // （V9d 防抹除 + P2-2 模型绑定）：探测把能力绑在当时的模型上（capabilities.model），
    // 切命名配置/换模型后 fileBase 的能力若仍属旧模型 → 继承会给未探测的新模型误判能力。
    // 模型不一致或 fileBase 无该键 → 删键（对应模型未探测=无能力；不铺底、不从内存继承——
    // namedEntry 是选中时刻的内存快照，不含其后探测结果）。
    if (base.llm && typeof base.llm === 'object' && base.llm.capabilities !== undefined
        && base.llm.capabilities.model === llm.model) {
      llm.capabilities = base.llm.capabilities;
    } else {
      delete llm.capabilities;
    }

    // --- lightrag_llm 段：段基底浅拷贝 + reasoning_effort/temperature 覆盖 + litellm_kwargs(thinking) + probe 产物覆写
    const ltBase = (entry && entry.lightrag_llm && typeof entry.lightrag_llm === 'object') ? entry.lightrag_llm : ((base.lightrag_llm && typeof base.lightrag_llm === 'object') ? base.lightrag_llm : {});
    const lightragLlm = { ...ltBase };
    if (ltForm.reasoning_effort !== undefined) lightragLlm.reasoning_effort = ltForm.reasoning_effort;
    // temperature 恒写数值；NaN（表单未提供/解析失败）→ 基底值 ?? 0.2（现状语义）
    const temp = Number(ltForm.temperature);
    lightragLlm.temperature = Number.isNaN(temp) ? (ltBase.temperature !== undefined && ltBase.temperature !== null ? ltBase.temperature : 0.2) : temp;
    lightragLlm.litellm_kwargs = buildKwargs(ltBase.litellm_kwargs, ltForm.thinking);
    // probe 产物键：仅 probeResults 非 null 时覆写（不来自表单也不来自基底）；null 不触碰（含不铺底）
    if (probeResults && typeof probeResults === 'object') {
      for (const k of ['response_format_mode', 'allowed_openai_params']) {
        if (probeResults[k] !== undefined) lightragLlm.litellm_kwargs[k] = probeResults[k];
      }
    }

    // --- vision_llm 段：基底透传（namedEntry?.vision_llm ?? fileBase.vision_llm）。
    // 设置页无 vision_llm 表单（第三方视觉模型由主 Agent 手工配置——SYSTEM_MANUAL 视觉能力节），
    // 表单零输入 → 基底原样透传，切命名配置/设置页保存不丢该段。
    const visionLlm = (entry && entry.vision_llm && typeof entry.vision_llm === 'object') ? { ...entry.vision_llm } : ((base.vision_llm && typeof base.vision_llm === 'object') ? { ...base.vision_llm } : {});

    // --- context 段：fileBase.context 浅拷贝 + 4 表单键覆盖（文件内其余键保留）
    const ctxBase = (base.context && typeof base.context === 'object') ? base.context : {};
    const context = { ...ctxBase };
    for (const k of ['contextWindowSize', 'warningThreshold', 'keepRecentTurns', 'sleepTriggerMinutes']) {
      if (ctxForm[k] !== undefined) context[k] = ctxForm[k];
    }

    // --- 顶级组装：storage/logging/Agent 等其余段 fileBase 原样保留；firstRun 例外置 false
    const out = {};
    for (const [k, v] of Object.entries(base)) {
      if (k === 'llm' || k === 'lightrag_llm' || k === 'vision_llm' || k === 'context') continue;
      out[k] = v;
    }
    out.llm = llm;
    out.lightrag_llm = lightragLlm;
    out.vision_llm = visionLlm;
    out.context = context;
    out.firstRun = false;
    return out;
  }

  return { mergeConfig };
});
