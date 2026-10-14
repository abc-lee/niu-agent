// T3 探针：从 renderer.js 按哨兵注释提取 buildSearchSectionModel 与 buildSearchItemHtml，驱动断言。
// 覆盖（v1.1 校准，与实现相符）：两段决策表四象限 + pending/empty 占位文案（显式 pending 字段）
// + 条目 HTML 装配（降级条目灰化类 / data-degraded / 「无图谱节点」标签、媒体类型标签映射、
// KG 条目模板与既有逐字同）+ 段恒存在——纯函数层行为断言，不是文本级 grep；
// DOM 时序 / 事件委托 / 陈旧写守卫层不在探针范围（由 §3.3 真机 AC 承接）。
const fs = require('fs');
const path = require('path');
const assert = require('assert');

const src = fs.readFileSync(
  path.join(__dirname, '..', 'ui', 'main', 'windows', 'graph', 'renderer.js'),
  'utf8',
);
const m = src.match(/\/\/ SENTINEL-Begin buildSearchSectionModel\r?\n([\s\S]*?)\/\/ SENTINEL-End buildSearchSectionModel/);
if (!m) {
  console.error('FAIL: renderer.js 缺少哨兵注释（未按方案落盘）');
  process.exit(1);
}
const build = new Function(`${m[1].trim()}\nreturn buildSearchSectionModel;`)();

// escapeHtml 在 renderer.js 里依赖 DOM（document.createElement）——无头 node 用同语义纯函数替身
// （探针测试值无 HTML 特殊字符，两实现输出逐字一致）；连同 mediaTypeLabel 哨兵一并喂进 new Function。
function escapeHtml(str) {
  return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
const mLabel = src.match(/\/\/ SENTINEL-Begin mediaTypeLabel\r?\n([\s\S]*?)\/\/ SENTINEL-End mediaTypeLabel/);
assert.ok(mLabel, 'renderer.js 缺少 mediaTypeLabel 哨兵');
const mItem = src.match(/\/\/ SENTINEL-Begin buildSearchItemHtml\r?\n([\s\S]*?)\/\/ SENTINEL-End buildSearchItemHtml/);
assert.ok(mItem, 'renderer.js 缺少 buildSearchItemHtml 哨兵');
const item = new Function(`${escapeHtml.toString()}\n${mLabel[1].trim()}\n${mItem[1].trim()}\nreturn buildSearchItemHtml;`)();

const KG = '知识图谱';
const MD = '照片（媒体）';

// ① 两段都有结果：段标题/条目原样透传（含 degraded 标记）
let [kg, md] = build(
  { kind: 'items', items: [{ name: 'a', type: 'person' }, { name: 'b', type: '' }] },
  { kind: 'items', items: [{ name: 'p1', mediaType: 'photo', degraded: false }, { name: 'p2', mediaType: 'video', degraded: true }] },
);
assert.strictEqual(kg.title, KG);
assert.strictEqual(kg.kind, 'items');
assert.strictEqual(kg.items.length, 2);
assert.strictEqual(md.title, MD);
assert.strictEqual(md.kind, 'items');
assert.strictEqual(md.items[1].degraded, true);

// ② 两段皆空 ⇒ 各占位「无结果」（不是失败，I-4）
[kg, md] = build({ kind: 'empty' }, { kind: 'empty' });
assert.strictEqual(kg.kind, 'empty');
assert.strictEqual(kg.message, '无结果');
assert.strictEqual(md.kind, 'empty');

// ③ 图谱失败 + 媒体有结果：图谱段失败行、媒体段照常（I-9）
[kg, md] = build({ kind: 'error', message: '检索失败' }, { kind: 'items', items: [{ name: 'p', mediaType: 'photo' }] });
assert.strictEqual(kg.kind, 'error');
assert.strictEqual(kg.message, '检索失败');
assert.strictEqual(md.kind, 'items');

// ④ 媒体失败 + 图谱有结果：反向（I-9/I-10）
[kg, md] = build({ kind: 'items', items: [{ name: 'a', type: 'person' }] }, { kind: 'error', message: '媒体搜索不可用（超时：编码后端可能被媒体入库编码占忙）' });
assert.strictEqual(kg.kind, 'items');
assert.strictEqual(md.kind, 'error');
assert.ok(md.message.includes('超时'));

// ⑤ pending 占位：搜索开始瞬间两段各自可辨（I-10；v1.1 F4：占位文案取显式 pending 字段）
[kg, md] = build({ kind: 'pending', pending: '搜索中…' }, { kind: 'pending', pending: '媒体搜索中…' });
assert.strictEqual(kg.message, '搜索中…');
assert.strictEqual(md.message, '媒体搜索中…');

// ⑥ 两段恒同时存在（任一段 error 也不缺段，I-3）
assert.strictEqual(kg.title, KG);
assert.strictEqual(md.title, MD);

// ⑦ 降级条目可辨形态（I-6；v1.1 F11/F12）：灰化类 + data-degraded + 「无图谱节点」标签
const deg = item('media', { name: 'p2', mediaType: 'image', degraded: true });
assert.ok(deg.includes('search-dropdown-item--degraded'));
assert.ok(deg.includes('data-degraded="1"'));
assert.ok(deg.includes('无图谱节点'));
// ⑧ 媒体类型标签映射（I-3；v1.1 F3）：非降级 image ⇒ 图片、photo ⇒ 照片，其余原样
assert.ok(item('media', { name: 'p1', mediaType: 'image', degraded: false }).includes('图片'));
assert.ok(item('media', { name: 'p3', mediaType: 'photo' }).includes('照片'));
// ⑨ KG 条目模板与既有逐字同（I-3/I-5）：内层 span 模板逐字同旧块 :948，外层为带 data-section/data-name 的 div
assert.strictEqual(
  item('kg', { name: 'a', type: 'person' }),
  '<div class="search-dropdown-item" data-section="kg" data-name="a"><span class="entity-name">a</span><span class="entity-type">person</span></div>',
);

console.log('graph_search_media_ui: 两段决策表 + 条目 HTML 装配断言全部通过');
