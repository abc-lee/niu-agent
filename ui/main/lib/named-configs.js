// 命名配置合集读写模块（不依赖 Electron，纯 Node.js fs/path）
// 合集文件：<niuConfigDir>/llm-configs.json → { "configs": { [name]: { llm, lightrag_llm } } }
// （spec §3.1 冻结包装层——顶层键恒为 "configs"，跨端 parity 锚点）
// 契约：docs/superpowers/plans/2026-09-07-llm-named-configs.md §0 C3（读侧）+ T1（写侧 upsert）

const path = require('path');
const fs = require('fs');

const COLLECTION_FILE = 'llm-configs.json';

function collectionPath(niuConfigDir) {
  return path.join(niuConfigDir, COLLECTION_FILE);
}

function isPlainObject(v) {
  return v !== null && typeof v === 'object' && !Array.isArray(v);
}

/**
 * 从解析后的顶层对象提取 configs 包装层（读侧/写侧共用）。
 *
 * 顶层必须是含 "configs" 键的对象；缺失/非对象（旧扁平格式、手工编辑走样）→ null（按损坏处理）。
 * 条目遍历用 hasOwnProperty 过滤 + defineProperty 落键——防 "__proto__" 原型链键
 * （普通赋值 configs['__proto__']=x 会触发原型 setter 静默丢条目/改原型）。
 *
 * @param {*} parsed JSON.parse 产物
 * @returns {?Object<string, object>} 自有条目对象；不合法 → null
 */
function extractConfigs(parsed) {
  if (!isPlainObject(parsed)) return null;
  const raw = parsed.configs;
  if (!isPlainObject(raw)) return null;
  const configs = {};
  for (const key of Object.keys(raw)) {
    if (!Object.prototype.hasOwnProperty.call(raw, key)) continue;
    Object.defineProperty(configs, key, { value: raw[key], enumerable: true, writable: true, configurable: true });
  }
  return configs;
}

/**
 * 读配置合集（读侧降级，spec §3.1）。
 *
 * 文件不存在 → { configs: {} }（首次使用/老用户升级，不报错）；
 * JSON 损坏 / 顶层非对象 / 缺 "configs" 包装层 → { configs: {}, warning: '配置合集文件损坏' }。
 *
 * @param {string} niuConfigDir ~/.niu/config 目录路径
 * @returns {{ configs: Object<string, object>, warning?: string }}
 */
function loadNamedConfigs(niuConfigDir) {
  const p = collectionPath(niuConfigDir);
  if (!fs.existsSync(p)) {
    return { configs: {} };
  }
  let parsed;
  try {
    parsed = JSON.parse(fs.readFileSync(p, 'utf-8'));
  } catch (e) {
    return { configs: {}, warning: '配置合集文件损坏' };
  }
  const configs = extractConfigs(parsed);
  if (!configs) {
    return { configs: {}, warning: '配置合集文件损坏' };
  }
  return { configs };
}

/**
 * 单条 upsert（读-改-写，保存时刻重读）。
 *
 * name === '__proto__' → { ok: false, warning: '配置名非法' } 拒绝（JS 原型 setter 会静默丢条目）；
 * 文件存在但解析失败/缺 "configs" 包装层 → { ok: false, warning: '配置合集文件损坏未同步' } 且不写（原坏文件保留）；
 * 成功 → configs[name] = entry，包 {"configs": {...}} 原子写回。
 * tmp 文件名带 pid+时间戳（防两个 Electron 进程并发写互相截断落半 JSON）。
 * 非解析类异常（磁盘满/权限）向上抛出——由调用方降级为 collectionWarning。
 *
 * @param {string} niuConfigDir ~/.niu/config 目录路径
 * @param {string} name 配置名（调用方已保证 trim 非空）
 * @param {object} entry { llm, lightrag_llm } 两段快照
 * @returns {{ ok: boolean, warning?: string }}
 */
function upsertNamedConfig(niuConfigDir, name, entry) {
  if (name === '__proto__') {
    return { ok: false, warning: '配置名非法' };
  }
  const p = collectionPath(niuConfigDir);
  let configs;
  if (fs.existsSync(p)) {
    let parsed;
    try {
      parsed = JSON.parse(fs.readFileSync(p, 'utf-8'));
    } catch (e) {
      return { ok: false, warning: '配置合集文件损坏未同步' };
    }
    configs = extractConfigs(parsed);
    if (!configs) {
      return { ok: false, warning: '配置合集文件损坏未同步' };
    }
  } else {
    configs = {};
  }
  Object.defineProperty(configs, name, { value: entry, enumerable: true, writable: true, configurable: true });
  fs.mkdirSync(path.dirname(p), { recursive: true });
  const tmp = p + '.' + process.pid + '.' + Date.now() + '.tmp';
  try {
    fs.writeFileSync(tmp, JSON.stringify({ configs }, null, 2));
    fs.renameSync(tmp, p);
  } catch (e) {
    try { fs.unlinkSync(tmp); } catch (_) { /* tmp 可能未创建 */ }
    throw e;
  }
  return { ok: true };
}

module.exports = { loadNamedConfigs, upsertNamedConfig, COLLECTION_FILE };
