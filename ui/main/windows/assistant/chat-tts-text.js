(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.ChatTtsText = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';
  var MAX_TOTAL = 4000, MAX_CHUNK = 120, CUT_SUFFIX = '。后面内容较长，已省略。';
  function normalizeRate(value) {
    var n = Number(value);
    if (!isFinite(n) || n <= 0) return 1.0;
    if (n < 0.5) return 0.5;
    if (n > 2.0) return 2.0;
    return n;
  }
  function voiceLang(v) { return String((v && (v.lang || v.language)) || '').toLowerCase().replace(/_/g, '-'); }
  function pickVoice(voices, want) {
    if (!voices || !voices.length) return null;
    var w = String(want || '').trim(), i;
    if (w) {
      for (i = 0; i < voices.length; i++) if (voices[i].voiceURI === w && voiceLang(voices[i]).indexOf('zh') === 0) return voices[i];
      for (i = 0; i < voices.length; i++) if (voices[i].name === w && voiceLang(voices[i]).indexOf('zh') === 0) return voices[i];
      for (i = 0; i < voices.length; i++) if (voices[i].name && voices[i].name.indexOf(w) >= 0 && voiceLang(voices[i]).indexOf('zh') === 0) return voices[i];
    }
    var fallback = null;
    for (i = 0; i < voices.length; i++) {
      var lang = voiceLang(voices[i]);
      if (lang.indexOf('zh') !== 0) continue;
      if (lang.indexOf('zh-cn') === 0 || lang.indexOf('zh-hans') === 0) return voices[i];
      if (!fallback) fallback = voices[i];
    }
    return fallback;
  }
  function cleanText(raw) {
    var t = String(raw == null ? '' : raw);
    t = t.replace(/```[\s\S]*?```/g, ' ').replace(/~~~[\s\S]*?~~~/g, ' ');
    t = t.replace(/`[^`\n]*`/g, ' ');
    t = t.replace(/!\[[^\]]*\]\([^)]*\)/g, ' ');
    t = t.replace(/\[([^\]]*)\]\([^)]*\)/g, '$1');
    t = t.replace(/\b(?:https?|ftp):\/\/\S+/g, ' ').replace(/<[^>]*>/g, ' ');
    t = t.replace(/\*\*([^*\n]+)\*\*/g, '$1').replace(/__([^_\n]+)__/g, '$1').replace(/\*([^*\n]+)\*/g, '$1');
    t = t.split('\n').filter(function (line) {
      var s = line.trim();
      return s && s.charAt(0) !== '|' && !/^[-*_]{3,}$/.test(s.replace(/\s+/g, ''));
    }).map(function (line) {
      return line.replace(/^\s*#{1,6}\s+/, '').replace(/^\s*[-*+]\s+/, '').replace(/^\s*\d+\.\s+/, '').replace(/^\s*>+\s?/, '').trim();
    }).join('\n');
    t = t.replace(/[ \t]+/g, ' ').replace(/\s*\n\s*/g, '\n').replace(/\n{2,}/g, '\n').trim();
    if (t.length > MAX_TOTAL) t = t.slice(0, MAX_TOTAL - CUT_SUFFIX.length) + CUT_SUFFIX;
    return t;
  }
  function pushChunk(text, out) {
    var s = String(text).trim();
    if (!s) return;
    if (s.length <= MAX_CHUNK) { out.push(s); return; }
    for (var i = 0; i < s.length; i += MAX_CHUNK) out.push(s.slice(i, i + MAX_CHUNK));
  }
  function splitChunks(text) {
    var out = [], buf = '';
    for (var i = 0; i < text.length; i++) {
      var ch = text.charAt(i); buf += ch;
      if ('。！？!?；;\n'.indexOf(ch) >= 0 || buf.length >= MAX_CHUNK) { pushChunk(buf, out); buf = ''; }
    }
    pushChunk(buf, out);
    return out.filter(function (s) { return /[A-Za-z0-9\u4e00-\u9fff]/.test(s); });
  }
  return { cleanText: cleanText, splitChunks: splitChunks, normalizeRate: normalizeRate, pickVoice: pickVoice };
});
