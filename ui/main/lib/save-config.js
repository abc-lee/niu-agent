// 命名配置保存纯函数（不依赖 Electron，纯 Node.js fs/path）
// 契约：docs/superpowers/plans/2026-09-07-llm-named-configs.md §0 C2
// main.js save-config handler = 本函数的薄壳（notifyReload 注入 HTTP fire-and-forget）

const path = require('path');
const fs = require('fs');

const { mergeConfig } = require('./config-merge.js');
const { upsertNamedConfig } = require('./named-configs.js');

/**
 * save-config IPC 核心逻辑（C2）：user-config.json + llm-configs.json 双写。
 *
 * 步骤：①configName trim 空 → {success:false,error}（不写任何文件、不发 reload）
 *       ②保存时刻重读 user-config.json 作 fileBase（不存在=空对象；JSON 损坏=空对象+告警）
 *       ③mergeConfig 两层基底合并（表单值 > loadedNamedEntry > fileBase）
 *       ④写 user-config.json（JSON.stringify(x,null,2)）
 *       ⑤upsertNamedConfig 单条 upsert {llm, lightrag_llm} 两段快照（vision_llm 恒在 user-config.json 顶层，不入合集）；
 *          {ok:false,warning} → collectionWarning=warning（原坏文件保留不写）；
 *          非解析类异常（磁盘满/权限）→ collectionWarning='配置合集写入失败: ...' 继续执行（不 throw）
 *       ⑥两文件处理后调 notifyReload() 一次（无论 warning——user-config.json 已落盘，主配置已变）
 *
 * @param {object} args
 * @param {string} args.niuConfigDir      ~/.niu/config 目录路径（测试可指向 tmp 目录）
 * @param {string} args.configName        配置名（trim 后须非空）
 * @param {object} args.formValues        C1 formValues 结构
 * @param {?{response_format_mode: *, allowed_openai_params: *}} args.probeResults  null = 不触碰产物键
 * @param {?{llm?: object, lightrag_llm?: object}} args.loadedNamedEntry  null | 合集条目（旧格式条目若带 vision_llm 键则忽略）
 * @param {Function} args.notifyReload    reload 通知回调（handler 注入；本函数只负责调一次）
 * @returns {{ success: true, collectionWarning?: string } | { success: false, error: string }}
 */
function saveConfigAndCollection({ niuConfigDir, configName, formValues, probeResults, loadedNamedEntry, notifyReload }) {
  const name = (typeof configName === 'string') ? configName.trim() : '';
  if (!name) {
    return { success: false, error: '配置名字不能为空' };
  }

  try {
    const userConfigPath = path.join(niuConfigDir, 'user-config.json');

    // ② 保存时刻重读 fileBase（无论是否加载条目都重读）
    let fileBase = {};
    if (fs.existsSync(userConfigPath)) {
      try {
        const parsed = JSON.parse(fs.readFileSync(userConfigPath, 'utf-8'));
        if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
          fileBase = parsed;
        }
      } catch (e) {
        console.warn('user-config.json 解析失败，按空对象处理:', e.message);
      }
    }

    // ③ 两层基底合并
    const newConfig = mergeConfig({
      fileBase, namedEntry: loadedNamedEntry, formValues, probeResults, configName: name
    });

    // ④ 写 user-config.json
    fs.mkdirSync(path.dirname(userConfigPath), { recursive: true });
    fs.writeFileSync(userConfigPath, JSON.stringify(newConfig, null, 2));
    console.log('Config saved to:', userConfigPath);

    // ⑤ 合集单条 upsert（两段快照 llm/lightrag_llm；损坏 → 跳过写 + warning；非解析类异常 → warning 继续执行）
    let collectionWarning;
    try {
      const up = upsertNamedConfig(niuConfigDir, name, {
        llm: newConfig.llm,
        lightrag_llm: newConfig.lightrag_llm
      });
      if (!up.ok) collectionWarning = up.warning;
    } catch (e) {
      // 非解析类异常（磁盘满/权限等）：user-config.json 已落盘、主配置已变 → 降级为 warning，不阻断 reload
      console.error('Failed to sync named config collection:', e);
      collectionWarning = '配置合集写入失败: ' + e.message;
    }

    // ⑥ 两文件处理后触发一次 reload（warning 路径也触发——主配置已变，后端必须重载）
    notifyReload();

    return collectionWarning ? { success: true, collectionWarning } : { success: true };
  } catch (e) {
    console.error('Failed to save config:', e);
    return { success: false, error: e.message };
  }
}

module.exports = { saveConfigAndCollection };
