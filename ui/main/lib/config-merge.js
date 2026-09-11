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

  // C1 排除清单（程序产物键不参与比对）：顶层键 + litellm_kwargs 子键层
  const LT_CUSTOMIZED_EXCLUDED_KEYS = ['reasoning_effort', 'temperature', 'capabilities', 'presetId'];
  const LT_KWARGS_EXCLUDED_KEYS = ['thinking', 'response_format_mode', 'allowed_openai_params'];

  // 空值归一化（顶层键与子键层同规则，主语恒为 lightrag 侧）：
  // undefined / null / '' / 缺失键 / 空对象 {} / 空数组 [] 恒视为「空」（= 跟随主模型）
  function isEmptyValue(v) {
    if (v === undefined || v === null || v === '') return true;
    if (Array.isArray(v)) return v.length === 0;
    if (typeof v === 'object') return Object.keys(v).length === 0;
    return false;
  }

  // 嵌套值归一化：对象递归键排序（防 === 引用不等误判）；数组按序保留（顺序本身有语义，不排序）
  function normalizeForCompare(v) {
    if (Array.isArray(v)) return v.map(normalizeForCompare);
    if (v && typeof v === 'object') {
      const out = {};
      for (const k of Object.keys(v).sort()) out[k] = normalizeForCompare(v[k]);
      return out;
    }
    return v;
  }

  function deepEqualValue(a, b) {
    return JSON.stringify(normalizeForCompare(a)) === JSON.stringify(normalizeForCompare(b));
  }

  /**
   * C1：判定主 Agent 是否手工自定义过 lightrag_llm（相对 llm）。
   * 非对称规则：只遍历 lightrag_llm 段的键集（减排除清单）——lightrag 侧归一化后非空且与 llm 侧对应值不等 → true；
   * lightrag 侧空 = 跟随（无论 llm 侧值，llm 侧独有键不参与）；全部键跟随 → false。
   * litellm_kwargs 整体不作顶层比对，其子键按下述子键层排除清单按同一非对称规则遍历。
   *
   * @param {object} fileBase user-config.json 全文（缺失/非对象按空对象处理）
   * @returns {boolean} true = lightrag_llm 被自定义过（设置页知识图谱容器应整体变灰）
   */
  function isLightragCustomized(fileBase) {
    const base = (fileBase && typeof fileBase === 'object') ? fileBase : {};
    const llm = (base.llm && typeof base.llm === 'object') ? base.llm : {};
    const lt = (base.lightrag_llm && typeof base.lightrag_llm === 'object') ? base.lightrag_llm : null;
    if (!lt) return false;

    for (const k of Object.keys(lt)) {
      if (LT_CUSTOMIZED_EXCLUDED_KEYS.indexOf(k) !== -1) continue;
      const v = lt[k];
      if (k === 'litellm_kwargs') {
        // 子键层：只遍历 lightrag kwargs 的子键集，同一非对称规则（空=跟随；非空且不等→true）
        const ltKw = (v && typeof v === 'object' && !Array.isArray(v)) ? v : {};
        const llmKw = (llm.litellm_kwargs && typeof llm.litellm_kwargs === 'object' && !Array.isArray(llm.litellm_kwargs)) ? llm.litellm_kwargs : {};
        for (const sk of Object.keys(ltKw)) {
          if (LT_KWARGS_EXCLUDED_KEYS.indexOf(sk) !== -1) continue;
          const sv = ltKw[sk];
          if (isEmptyValue(sv)) continue; // 空 = 跟随，无论 llm 侧值
          if (!deepEqualValue(sv, llmKw[sk])) return true;
        }
        continue;
      }
      if (isEmptyValue(v)) continue; // 空 = 跟随，无论 llm 侧值
      if (!deepEqualValue(v, llm[k])) return true;
    }
    return false;
  }

  /**
   * 合并文件配置 / 已加载命名条目 / 表单值 → 新的 user-config 对象。
   * 纯函数：不改任何入参，返回新对象。
   *
   * 两层基底（表单值 > namedEntry > fileBase）：
   * - llm 段基底 = namedEntry?.llm ?? fileBase.llm
   * - lightrag_llm 段：isLightragCustomized(fileBase)===true → fileBase.lightrag_llm 整段透传（C3，忽略条目/表单/探测产物）；
   *   否则段基底 = namedEntry?.lightrag_llm ?? fileBase.lightrag_llm
   * - vision_llm 无表单输入且不入合集机制，恒为 fileBase.vision_llm 透传
   *   （主 Agent 手工配的段不随保存/切换丢失）
   * - storage/logging/context/Agent 等其余顶级段恒取 fileBase（全局设置不随切换丢失）
   *
   * @param {object} args
   * @param {object} args.fileBase          保存时刻重读的 user-config.json 全文（恒提供；缺段按空对象处理）
   * @param {?{llm?: object, lightrag_llm?: object}} args.namedEntry  loadedNamedEntry：null | 合集条目（旧格式条目若带 vision_llm 键则忽略）
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

    // --- lightrag_llm 段：C3 customized 分支优先——isLightragCustomized(fileBase)===true（主 Agent 手工自定义过）
    // → fileBase.lightrag_llm 整段透传（缺失/非对象 → {}），忽略 namedEntry 基底 / ltForm 三项覆盖 / probeResults 产物覆写；
    // 非 customized 走现状：段基底浅拷贝 + reasoning_effort/temperature 覆盖 + litellm_kwargs(thinking) + probe 产物覆写
    let lightragLlm;
    if (isLightragCustomized(base)) {
      lightragLlm = (base.lightrag_llm && typeof base.lightrag_llm === 'object') ? { ...base.lightrag_llm } : {};
    } else {
      const ltBase = (entry && entry.lightrag_llm && typeof entry.lightrag_llm === 'object') ? entry.lightrag_llm : ((base.lightrag_llm && typeof base.lightrag_llm === 'object') ? base.lightrag_llm : {});
      lightragLlm = { ...ltBase };
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
    }

    // --- vision_llm 段：恒 fileBase 透传（C2/D-B——合集不再携带 vision_llm 段，namedEntry 快照一律忽略）。
    // 设置页无 vision_llm 表单（第三方视觉模型由主 Agent 手工配置——SYSTEM_MANUAL 视觉能力节），
    // 表单零输入 → 基底原样透传，切命名配置/设置页保存不动顶层视觉模型；base.vision_llm 缺失/非对象 → {}。
    const visionLlm = (base.vision_llm && typeof base.vision_llm === 'object') ? { ...base.vision_llm } : {};

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

  return { mergeConfig, isLightragCustomized };
});
