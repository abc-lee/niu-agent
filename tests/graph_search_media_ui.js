// T3 探针：从 renderer.js 按哨兵注释提取 buildSearchSectionModel / finiteScore / mediaScoreCutoff / formatScore / buildSearchItemHtml，驱动断言。
// 覆盖（v1.5 校准 + 2026-10-15 相对及格线 + 两段相似度分数徽标）：两段决策表四象限 + pending/empty 占位文案（显式 pending 字段）
// + 模型层降级命中过滤（含降级命中的输入 ⇒ 输出 items 不含降级项；命中全降级 ⇒ 空段「无结果」）
// + 相对及格线决策表（B1 单条 / B2 差>GAP / B3 差严格<GAP / B4 无分 fail-open / B5 全无效原样 / B6 null/''/NaN/Infinity/布尔全形态 / B7 降级高分不抬线 / B12 '0.69' 字符串 / B13 全降级 / B14 负分）
// + finiteScore/formatScore 纯函数语义 + 两段徽标装配（有分 ⇒ 含 entity-score；无分 ⇒ 不含且与旧模板逐字节同）+ 幂等 + 模型层兜底（构造处不调 cutoff 时仍切尾）
// + 段恒存在——纯函数层行为断言，不是文本级 grep；DOM 时序 / 事件委托 / 陈旧写守卫（B15 stillCurrent）不在探针范围（由 §3.3 真机 AC 承接）。
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
// build 的 Function 须补喂 finiteScore + mediaScoreCutoff（后者的块内含其 const）——漏任一块即运行期 ReferenceError，既有用例全红；
// 三块按同款哨兵正则提取，沿用 \r?\n 行尾加固口径（顺序不承重：函数声明提升，const 在调用前已初始化）。
const mFinite = src.match(/\/\/ SENTINEL-Begin finiteScore\r?\n([\s\S]*?)\/\/ SENTINEL-End finiteScore/);
const mCutoff = src.match(/\/\/ SENTINEL-Begin mediaScoreCutoff\r?\n([\s\S]*?)\/\/ SENTINEL-End mediaScoreCutoff/);
assert.ok(mFinite, 'renderer.js 缺少 finiteScore 哨兵');
assert.ok(mCutoff, 'renderer.js 缺少 mediaScoreCutoff 哨兵');
const build = new Function(`${mFinite[1].trim()}\n${mCutoff[1].trim()}\n${m[1].trim()}\nreturn buildSearchSectionModel;`)();

// escapeHtml 在 renderer.js 里依赖 DOM（document.createElement）——无头 node 用同语义纯函数替身
// （探针测试值无 HTML 特殊字符，两实现输出逐字一致）；连同 finiteScore / mediaTypeLabel / formatScore 哨兵
// 一并喂进 item 的 new Function（formatScore 依赖 finiteScore，写在其前）。
function escapeHtml(str) {
  return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
const mLabel = src.match(/\/\/ SENTINEL-Begin mediaTypeLabel\r?\n([\s\S]*?)\/\/ SENTINEL-End mediaTypeLabel/);
assert.ok(mLabel, 'renderer.js 缺少 mediaTypeLabel 哨兵');
const mFormat = src.match(/\/\/ SENTINEL-Begin formatScore\r?\n([\s\S]*?)\/\/ SENTINEL-End formatScore/);
assert.ok(mFormat, 'renderer.js 缺少 formatScore 哨兵');
const mItem = src.match(/\/\/ SENTINEL-Begin buildSearchItemHtml\r?\n([\s\S]*?)\/\/ SENTINEL-End buildSearchItemHtml/);
assert.ok(mItem, 'renderer.js 缺少 buildSearchItemHtml 哨兵');
const item = new Function(`${escapeHtml.toString()}\n${mFinite[1].trim()}\n${mLabel[1].trim()}\n${mFormat[1].trim()}\n${mItem[1].trim()}\nreturn buildSearchItemHtml;`)();

// 纯函数直驱句柄（决策表 / 幂等 / 变异红证直接驱动 mediaScoreCutoff/finiteScore/formatScore；模型层走 build）
const pure = new Function(`${mFinite[1].trim()}\n${mCutoff[1].trim()}\n${mFormat[1].trim()}\nreturn { finiteScore, mediaScoreCutoff, formatScore };`)();

const KG = '知识图谱';
const MD = '照片（媒体）';

// ① 两段都有结果：段标题/正常条目原样透传；降级条目被模型层过滤、不进输出 items（v1.5 I-6）
let [kg, md] = build(
  { kind: 'items', items: [{ name: 'a', type: 'person' }, { name: 'b', type: '' }] },
  { kind: 'items', items: [{ name: 'p1', mediaType: 'photo' }, { name: 'p2', mediaType: 'video', degraded: true }] },
);
assert.strictEqual(kg.title, KG);
assert.strictEqual(kg.kind, 'items');
assert.strictEqual(kg.items.length, 2);
assert.strictEqual(md.title, MD);
assert.strictEqual(md.kind, 'items');
assert.strictEqual(md.items.length, 1); // 降级 p2 被模型层过滤，不进输出 items（v1.5）
assert.strictEqual(md.items[0].name, 'p1');

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
// ⑦ 给模型喂含降级命中的媒体段输入 ⇒ 输出 items 不含降级项（v1.5；取代原 ⑦ 降级形态断言）
[kg, md] = build({ kind: 'empty' }, { kind: 'items', items: [{ name: 'q1', mediaType: 'photo' }, { name: 'q2', mediaType: 'video', degraded: true }] });
assert.strictEqual(md.kind, 'items');
assert.strictEqual(md.items.length, 1);
assert.strictEqual(md.items[0].name, 'q1');
// ⑧ 命中全为降级 ⇒ 该段 kind:'empty'（「无结果」，不是失败行，v1.5）
[kg, md] = build({ kind: 'empty' }, { kind: 'items', items: [{ name: 'q3', mediaType: 'photo', degraded: true }] });
assert.strictEqual(md.kind, 'empty');
assert.strictEqual(md.message, '无结果');
// ⑨ 媒体类型标签映射（I-3；v1.1 F3）：image ⇒ 图片、photo ⇒ 照片，其余原样
assert.ok(item('media', { name: 'p1', mediaType: 'image' }).includes('图片'));
assert.ok(item('media', { name: 'p3', mediaType: 'photo' }).includes('照片'));
// ⑩ KG 条目模板与既有逐字同（I-3/I-5）：内层 span 模板逐字同旧块 :948，外层为带 data-section/data-name 的 div
// ——无分数时新模板（可选 score span 缺省为空串）与旧模板逐字节相同，这条断言保留价值即证明此点
assert.strictEqual(
  item('kg', { name: 'a', type: 'person' }),
  '<div class="search-dropdown-item" data-section="kg" data-name="a"><span class="entity-name">a</span><span class="entity-type">person</span></div>',
);

// ===== 相对及格线决策表（方案 §5 B1–B15 / §6；纯函数直驱 + 模型层兜底）=====

// B1 单条媒体命中：top = 自身 ⇒ 必保留（相对线的天然性质）
assert.strictEqual(pure.mediaScoreCutoff([{ name: 'only', mediaType: 'photo', score: 0.42 }]).length, 1);

// B2 分数差严格 > GAP(0.03) ⇒ 只留高的（用户已接受"可能只剩 1 张"）
assert.strictEqual(pure.mediaScoreCutoff([{ name: 'hi', score: 0.9 }, { name: 'lo', score: 0.5 }]).length, 1);

// B3 分数差严格 < GAP ⇒ 两条都留。差值必须严格小于 GAP：恰好等于 GAP 属二进制浮点红区
// （top-GAP 的浮点表示略大于贴线分 ⇒ 两位小数贴线对被误滤），不在断言范围（R3-A P3-1）
{
  const kept = pure.mediaScoreCutoff([{ name: 'a', score: 0.9 }, { name: 'b', score: 0.88 }]); // 差 0.02 < 0.03
  assert.strictEqual(kept.length, 2); // 0.88 >= 0.9 - 0.03 = 0.87
}

// B4 命中无 score（契约漂移/旧数据）⇒ fail-open 保留显示（徽标层另断言无 span）
assert.strictEqual(pure.mediaScoreCutoff([{ name: 'x', score: undefined }, { name: 'y', score: 0.5 }]).length, 2);

// B5 全部命中无有效 score ⇒ 原样全部保留（不因缺分清空）
assert.strictEqual(pure.mediaScoreCutoff([{ name: 'p1' }, { name: 'p2' }]).length, 2);

// B6 null / '' / NaN / Infinity / 布尔全形态 ⇒ 视为"无分数"：finiteScore 一律返回 null（不得被 Number(null)===0 误判、布尔不得穿透）
{
  assert.strictEqual(pure.finiteScore(null), null);
  assert.strictEqual(pure.finiteScore(undefined), null);
  assert.strictEqual(pure.finiteScore(''), null);
  assert.strictEqual(pure.finiteScore(true), null);
  assert.strictEqual(pure.finiteScore(false), null);
  assert.strictEqual(pure.finiteScore(NaN), null);
  assert.strictEqual(pure.finiteScore(Infinity), null);
  assert.strictEqual(pure.finiteScore('abc'), null);
  assert.strictEqual(pure.finiteScore(0), 0); // "分数为 0" ≠ "没有分数"
  assert.strictEqual(pure.finiteScore(-0.2), -0.2);
  assert.strictEqual(pure.finiteScore(0.69), 0.69);
  // 无效分条目与有效分并存：fail-open 保留、不参与 top、不抬线
  assert.strictEqual(pure.mediaScoreCutoff([{ score: 0.5 }, { score: null }, { score: '' }, { score: NaN }, { score: Infinity }, { score: true }]).length, 6);
}

// B7 降级高分不得抬线：先降级过滤、再算 top——若顺序写反（先 cutoff 后降级过滤，或 top 计入降级项）本断言即红
{
  const [, mdOut] = build({ kind: 'empty' }, { kind: 'items', items: [{ name: 'd', mediaType: 'photo', degraded: true, score: 0.9 }, { name: 'k', mediaType: 'photo', score: 0.5 }] });
  assert.strictEqual(mdOut.kind, 'items');
  assert.strictEqual(mdOut.items.length, 1); // 恰 1 条
  assert.strictEqual(mdOut.items[0].name, 'k'); // 且为 k（0.5 不因降级项 0.9 抬线而被切）
}

// B12 score 是数字字符串 '0.69'：Number 可转 ⇒ 视为有效分（参与 top、出徽标）——口径固化
{
  assert.strictEqual(pure.finiteScore('0.69'), 0.69);
  assert.strictEqual(pure.formatScore('0.69'), '0.69');
  const kept = pure.mediaScoreCutoff([{ score: '0.71' }, { score: 0.5 }]); // 字符串 0.71 参与 top，0.5 被切
  assert.strictEqual(kept.length, 1);
}

// B13 命中全为降级 ⇒ 先被降级过滤 ⇒ 段内「无结果」（既有用例⑧的对应行固化）
{
  const [, mdOut] = build({ kind: 'empty' }, { kind: 'items', items: [{ name: 'd1', mediaType: 'photo', degraded: true }, { name: 'd2', mediaType: 'video', degraded: true, score: 0.99 }] });
  assert.strictEqual(mdOut.kind, 'empty');
  assert.strictEqual(mdOut.message, '无结果');
}

// B14 分数为负（cosine 理论上可负）：照常参与 top 与线判定，不做额外夹取
{
  const kept = pure.mediaScoreCutoff([{ name: 'n1', score: -0.1 }, { name: 'n2', score: -0.5 }]);
  assert.strictEqual(kept.length, 1); // top = -0.1，线 ≈ -0.13，-0.5 被切
  assert.strictEqual(kept[0].name, 'n1');
}

// B15 陈旧响应迟到（新一轮已开始，旧轮媒体腿才落定）⇒ 被既有 stillCurrent() 守卫挡下、永不落 DOM——
// DOM 时序层，不在本探针范围（由真机 AC 承接）；本方案未触碰该守卫。

// B9/B11 徽标装配：无分数的合成置顶行形态 ⇒ 无徽标；错误/超时/未落定分支文案逐字不变（见 ②③④⑤）
assert.ok(!item('kg', { name: '测试用户', type: 'person' }).includes('entity-score')); // 合成置顶行：无 distance ⇒ 无徽标
// B10 图谱段有分数 ⇒ 每行带徽标（0.74 形态）
assert.ok(item('kg', { name: 'a', type: 'person', score: 0.74 }).includes('class="entity-score"'));
assert.ok(item('kg', { name: 'a', type: 'person', score: 0.74 }).includes('0.74'));
// 媒体段有分数 ⇒ 带徽标
assert.ok(item('media', { name: 'p', mediaType: 'photo', score: 0.69 }).includes('class="entity-score"'));
assert.ok(item('media', { name: 'p', mediaType: 'photo', score: 0.69 }).includes('0.69'));
// 媒体段无分数 ⇒ 不含该 span（模板与旧模板逐字节同，见 ⑩）
assert.ok(!item('media', { name: 'p1', mediaType: 'photo' }).includes('entity-score'));

// formatScore：有效数值 ⇒ 两位小数；缺失/无效 ⇒ 空串（调用处据此不渲染 span，不留空占位）
assert.strictEqual(pure.formatScore(0.69), '0.69');
assert.strictEqual(pure.formatScore(null), '');
assert.strictEqual(pure.formatScore(''), '');
assert.strictEqual(pure.formatScore(NaN), '');
assert.strictEqual(pure.formatScore(true), '');

// 幂等：同一输入连跑两次 mediaScoreCutoff ⇒ 输出逐元素相等（存活集 max 与 top 同值 ⇒ 第二遍谓词与第一遍一致）
{
  const input = [{ name: 'a', score: 0.9 }, { name: 'b', score: 0.5 }, { name: 'c', score: 0.88 }];
  const once = pure.mediaScoreCutoff(input);
  const twice = pure.mediaScoreCutoff(once);
  assert.strictEqual(once.length, 2); // b(0.5) 被切，a/c 存活
  assert.strictEqual(twice.length, once.length);
  for (let i = 0; i < once.length; i++) {
    assert.strictEqual(twice[i].name, once[i].name);
  }
}

// 模型层兜底：构造处未调 cutoff 时（raw items 直喂模型层），模型层仍完成切尾
{
  const [, mdOut] = build({ kind: 'empty' }, { kind: 'items', items: [{ name: 'a', mediaType: 'photo', score: 0.9 }, { name: 'b', mediaType: 'photo', score: 0.4 }] });
  assert.strictEqual(mdOut.kind, 'items');
  assert.strictEqual(mdOut.items.length, 1); // b(0.4) 被模型层 mediaScoreCutoff 切掉
  assert.strictEqual(mdOut.items[0].name, 'a');
}

console.log('graph_search_media_ui: 两段决策表 + 相对及格线决策表 + 条目 HTML 装配（分数徽标）断言全部通过');
