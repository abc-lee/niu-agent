const { test } = require('node:test');
const assert = require('node:assert/strict');
const T = require('../ui/main/windows/assistant/chat-tts-text.js');

test('cleanText removes code, URLs, tables, keeps labels', () => {
  const out = T.cleanText('你好\n```js\nconst x=1\n```\n[文档](https://a.b) https://x.y\n| a | b |\n|---|---|\n**重点**');
  assert.ok(!out.includes('const x'));
  assert.ok(!out.includes('https://'));
  assert.ok(!out.includes('|'));
  assert.ok(out.includes('文档') && out.includes('重点'));
});

test('cleanText caps total at 4000 including suffix', () => {
  const out = T.cleanText('长'.repeat(4100));
  assert.ok(out.length <= 4000);
  assert.ok(out.endsWith('。后面内容较长，已省略。'));
});

test('splitChunks drops pure punctuation and caps 120', () => {
  const chunks = T.splitChunks('第一句。第二句！' + '长'.repeat(130) + '。。。');
  assert.ok(chunks.length >= 3);
  assert.ok(chunks.every(c => c.length <= 120));
  assert.equal(T.splitChunks('。。。').length, 0);
});

test('normalizeRate', () => {
  assert.equal(T.normalizeRate(undefined), 1.0);
  assert.equal(T.normalizeRate(0), 1.0);
  assert.equal(T.normalizeRate(0.1), 0.5);
  assert.equal(T.normalizeRate(3), 2.0);
});

test('pickVoice priority is voiceURI, name, contains; zh only', () => {
  const voices = [
    { name: 'English', lang: 'en-US', voiceURI: 'en' },
    { name: 'Mainland A', lang: 'zh_CN', voiceURI: 'cn' },
    { name: 'Mainland B', lang: 'zh-CN', voiceURI: 'other' }
  ];
  assert.equal(T.pickVoice(voices, 'other').voiceURI, 'other'); // voiceURI exact
  assert.equal(T.pickVoice(voices, 'Mainland A').voiceURI, 'cn'); // name exact
  assert.equal(T.pickVoice(voices, 'Mainland').voiceURI, 'cn'); // contains
  assert.equal(T.pickVoice(voices, 'English').voiceURI, 'cn'); // non-zh rejected
  assert.equal(T.pickVoice([voices[0]], ''), null); // no zh voice → null
});
