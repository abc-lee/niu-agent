(function () {
  'use strict';
  var DEFAULTS = { provider: 'system', voice: '', rate: 1.0 };
  var SEEN_CAP = 200, VOICES_TIMEOUT = 1500;
  var T = window.ChatTtsText;
  if (!T) { console.error('[TTS] chat-tts-text.js missing'); return; }
  var synth = window.speechSynthesis || null;
  var ttsBtn = null, config = Object.assign({}, DEFAULTS);
  var sessionEnabled = false;
  var unavailable = !synth || (typeof SpeechSynthesisUtterance === 'undefined');
  var noChineseVoice = false, speechBlocked = false;
  var seenIds = new Set(), voicesPromise = null, generation = 0;
  var lastText = '', lastTextAt = 0;
  var queue = [], speaking = false;
  function getBtn() { if (!ttsBtn) ttsBtn = document.getElementById('tts-btn'); return ttsBtn; }
  function isMini() { return !!(document.body && document.body.classList.contains('mini')); }
  function readConfig() {
    var api = window.electronAPI;
    if (!api || typeof api.getTtsConfig !== 'function') return Promise.resolve(Object.assign({}, DEFAULTS));
    try {
      return Promise.resolve(api.getTtsConfig()).then(function (raw) {
        return (raw && typeof raw === 'object' && !Array.isArray(raw)) ? raw : Object.assign({}, DEFAULTS);
      }).catch(function (e) { console.warn('[TTS] config rejected:', e); return Object.assign({}, DEFAULTS); });
    } catch (e) { return Promise.resolve(Object.assign({}, DEFAULTS)); }
  }
  function applyConfig(raw) {
    var next = Object.assign({}, DEFAULTS);
    if (raw && typeof raw === 'object') {
      if (typeof raw.provider === 'string' && raw.provider) next.provider = raw.provider;
      if (typeof raw.voice === 'string') next.voice = raw.voice;
      next.rate = T.normalizeRate(raw.rate);
    }
    config = next;
    syncButton(); probeVoice();
  }
  function refreshConfig() { return readConfig().then(applyConfig); }
  function collectVoices() {
    if (!synth) return Promise.resolve([]);
    var now = [];
    try { now = synth.getVoices() || []; } catch (e) { now = []; }
    if (now.length) return Promise.resolve(now);
    return new Promise(function (resolve) {
      var done = false;
      function finish() {
        if (done) return; done = true;
        if (typeof synth.removeEventListener === 'function') synth.removeEventListener('voiceschanged', finish);
        var list = []; try { list = synth.getVoices() || []; } catch (e) { list = []; }
        resolve(list);
      }
      if (typeof synth.addEventListener === 'function') synth.addEventListener('voiceschanged', finish);
      setTimeout(finish, VOICES_TIMEOUT);
    });
  }
  function ensureVoices() {
    if (!voicesPromise) voicesPromise = collectVoices().then(function (list) { if (!list.length) voicesPromise = null; return list; });
    return voicesPromise;
  }
  function probeVoice() {
    if (unavailable) return Promise.resolve(false);
    return ensureVoices().then(function (voices) {
      if (!voices.length) {
        noChineseVoice = true;
        sessionEnabled = false;
        syncButton();
        return false;
      }
      var v = T.pickVoice(voices, config.voice);
      noChineseVoice = !v;
      if (noChineseVoice) sessionEnabled = false;
      syncButton(); return !!v;
    });
  }
  function speakChunks(chunks, voice, myGen, onDone) {
    var rate = T.normalizeRate(config.rate);
    var lang = String((voice && (voice.lang || voice.language)) || 'zh-CN');
    function next(i) {
      if (myGen !== generation) return;
      if (i >= chunks.length) { if (onDone) onDone(); return; }
      var u = new SpeechSynthesisUtterance(chunks[i]);
      var advanced = false, started = false, watchdog = null;
      function advance() { if (advanced) return; advanced = true; clearTimeout(watchdog); if (myGen === generation) next(i + 1); }
      watchdog = setTimeout(function () {
        if (!started && myGen === generation) {
          try { synth.cancel(); } catch (e) { /* no-op */ }
          advance();
        }
      }, 3000);
      u.voice = voice; u.lang = lang; u.rate = rate; u.pitch = 1; u.volume = 1;
      u.onstart = function () { started = true; clearTimeout(watchdog); };
      u.onend = advance;
      u.onerror = function (e) {
        var err = e && e.error;
        if (err === 'not-allowed' || err === 'NotAllowedError') {
          advanced = true; clearTimeout(watchdog); speechBlocked = true; sessionEnabled = false; syncButton();
          if (onDone) onDone();
          return;
        }
        console.warn('[TTS] utterance error:', err || 'unknown'); advance();
      };
      try { synth.speak(u); } catch (e) { console.warn('[TTS] speak failed:', e); advance(); }
    }
    next(0);
  }
  function enqueue(text, id) {
    if (!sessionEnabled || unavailable || noChineseVoice || speechBlocked || isMini()) return;
    if (id && seenIds.has(id)) return;
    if (lastText && Date.now() - lastTextAt < 5000 &&
        (text === lastText || text.indexOf(lastText) === 0 || lastText.indexOf(text) === 0)) return;
    if (id) { seenIds.add(id); if (seenIds.size > SEEN_CAP) seenIds.delete(seenIds.values().next().value); }
    var chunks = T.splitChunks(T.cleanText(text));
    if (!chunks.length) return;
    lastText = text;
    lastTextAt = Date.now();
    queue.push(chunks);
    pump();
  }
  function endCurrent(myGen) {
    if (myGen !== generation || !speaking) return;
    speaking = false;
    pump();
  }
  function pump() {
    if (speaking || !queue.length) return;
    if (!sessionEnabled || unavailable || noChineseVoice || speechBlocked || isMini()) { queue.length = 0; return; }
    var chunks = queue.shift();
    var myGen = generation;
    speaking = true;
    ensureVoices().then(function (voices) {
      if (myGen !== generation) return;
      if (!voices.length) {
        console.warn('[TTS] no voices; skip');
        speaking = false; pump();
        return;
      }
      var voice = T.pickVoice(voices, config.voice);
      if (!voice) {
        noChineseVoice = true; sessionEnabled = false; syncButton();
        queue.length = 0; speaking = false;
        return;
      }
      setTimeout(function () {
        if (myGen !== generation) return;
        speakChunks(chunks, voice, myGen, function () { endCurrent(myGen); });
      }, 50);
    }).catch(function (e) {
      console.warn('[TTS] ensureVoices failed:', e);
      if (myGen === generation) { speaking = false; pump(); }
    });
  }
  function stop() {
    generation++;
    queue.length = 0; speaking = false;
    try { synth && synth.cancel(); } catch (e) { /* no-op */ }
  }
  function getTitle() {
    if (unavailable) return '朗读：本机不支持语音合成';
    if (speechBlocked) return '朗读：被系统自动播放策略阻止';
    if (noChineseVoice) return '朗读：本机无中文语音，已停用';
    return sessionEnabled ? '朗读：开' : '朗读：关';
  }
  function syncButton() {
    var b = getBtn(); if (!b) return;
    b.setAttribute('aria-pressed', sessionEnabled ? 'true' : 'false');
    b.classList.toggle('active', sessionEnabled); b.title = getTitle();
    b.disabled = unavailable || noChineseVoice;
  }
  function setEnabled(on) {
    speechBlocked = false;
    if (unavailable || noChineseVoice) { sessionEnabled = false; syncButton(); return; }
    sessionEnabled = !!on; if (!sessionEnabled) stop(); syncButton();
  }
  function toggle() { setEnabled(!sessionEnabled); }

  var configReady = false, pendingTts = null;
  function handleTts(data) {
    // 单一通道：读 new_message 里的 assistant 文本（工具轮旁白 + 最终回复都读），
    // 按消息 id 去重、按到达顺序排队；空内容不读。
    if (!data || data.role !== 'assistant' || !data.id) return;
    var text = data.content == null ? '' : String(data.content);
    if (!text) return;
    enqueue(text, String(data.id));
  }
  if (window.electronAPI && typeof window.electronAPI.onNewMessage === 'function') {
    window.electronAPI.onNewMessage(function (data) {
      if (!data) return;
      if (data.role === 'user' || data.role === 'chat_busy') { stop(); return; }
      if (!configReady) { pendingTts = data; return; }
      handleTts(data);
    });
  }
  if (window.electronAPI && typeof window.electronAPI.onSyncState === 'function') {
    window.electronAPI.onSyncState(function () { refreshConfig(); });
  }
  window.addEventListener('pagehide', function () { stop(); });
  syncButton();
  refreshConfig().then(function () {
    configReady = true;
    if (pendingTts) { var d = pendingTts; pendingTts = null; handleTts(d); }
  }).catch(function (e) {
    console.warn('[TTS] init failed:', e);
    configReady = true;
    if (pendingTts) { var d = pendingTts; pendingTts = null; handleTts(d); }
  });
  window.NiuTts = {
    isEnabled: function () { return sessionEnabled; },
    isAvailable: function () { return !unavailable && !noChineseVoice; },
    setEnabled: setEnabled, toggle: toggle, stop: stop, refreshConfig: refreshConfig,
    getTitle: getTitle,
    getState: function () { return { config: Object.assign({}, config), sessionEnabled: sessionEnabled, unavailable: unavailable, noChineseVoice: noChineseVoice, speechBlocked: speechBlocked }; }
  };
})();
