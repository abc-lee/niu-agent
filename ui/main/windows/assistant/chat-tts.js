(function () {
  'use strict';
  var DEFAULTS = { enabled: false, provider: 'system', voice: '', rate: 1.0 };
  var SEEN_CAP = 200, VOICES_TIMEOUT = 1500;
  var T = window.ChatTtsText;
  if (!T) { console.error('[TTS] chat-tts-text.js missing'); return; }
  var synth = window.speechSynthesis || null;
  var ttsBtn = null, config = Object.assign({}, DEFAULTS);
  var configEnabled = false, sessionEnabled = false, userOverride = false;
  var unavailable = !synth || (typeof SpeechSynthesisUtterance === 'undefined');
  var noChineseVoice = false, speechBlocked = false;
  var seenIds = new Set(), voicesPromise = null, generation = 0;
  var lastText = '', lastTextAt = 0;
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
      if (typeof raw.enabled === 'boolean') next.enabled = raw.enabled;
      if (typeof raw.provider === 'string' && raw.provider) next.provider = raw.provider;
      if (typeof raw.voice === 'string') next.voice = raw.voice;
      next.rate = T.normalizeRate(raw.rate);
    }
    config = next; configEnabled = !!config.enabled;
    if (speechBlocked) sessionEnabled = false;
    else if (!userOverride) sessionEnabled = configEnabled;
    if (!configEnabled && !userOverride) stop();
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
  function speakChunks(chunks, voice, myGen) {
    var rate = T.normalizeRate(config.rate);
    var lang = String((voice && (voice.lang || voice.language)) || 'zh-CN');
    function next(i) {
      if (myGen !== generation) return;
      if (i >= chunks.length) return;
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
          advanced = true; clearTimeout(watchdog); speechBlocked = true; sessionEnabled = false; syncButton(); return;
        }
        console.warn('[TTS] utterance error:', err || 'unknown'); advance();
      };
      try { synth.speak(u); } catch (e) { console.warn('[TTS] speak failed:', e); advance(); }
    }
    next(0);
  }
  function speak(text, id) {
    if (!sessionEnabled || unavailable || noChineseVoice || speechBlocked || isMini()) return;
    if (id && seenIds.has(id)) return;
    if (lastText && Date.now() - lastTextAt < 5000 &&
        (text === lastText || text.indexOf(lastText) === 0 || lastText.indexOf(text) === 0)) return;
    if (id) { seenIds.add(id); if (seenIds.size > SEEN_CAP) seenIds.delete(seenIds.values().next().value); }
    var chunks = T.splitChunks(T.cleanText(text));
    if (!chunks.length) return;
    generation++;
    var myGen = generation;
    try { synth.cancel(); } catch (e) { /* no-op */ }
    ensureVoices().then(function (voices) {
      if (myGen !== generation) return;
      if (!voices.length) { console.warn('[TTS] no voices; skip'); return; }
      var voice = T.pickVoice(voices, config.voice);
      if (!voice) { noChineseVoice = true; sessionEnabled = false; syncButton(); return; }
      lastText = text;
      lastTextAt = Date.now();
      setTimeout(function () { if (myGen === generation) speakChunks(chunks, voice, myGen); }, 50);
    });
  }
  function stop() { generation++; try { synth && synth.cancel(); } catch (e) { /* no-op */ } }
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
    userOverride = true; speechBlocked = false;
    if (unavailable || noChineseVoice) { sessionEnabled = false; syncButton(); return; }
    sessionEnabled = !!on; if (!sessionEnabled) stop(); syncButton();
  }
  function toggle() { setEnabled(!sessionEnabled); }

  var configReady = false, pendingTts = null;
  function handleTts(data) {
    if (!data || data.role !== 'assistant' || data.final !== true || !data.id || data.source !== 'electron') return;
    var id = String(data.id);
    if (seenIds.has(id)) return;
    speak(data.content, id);
  }
  if (window.electronAPI && typeof window.electronAPI.onTtsEvent === 'function') {
    window.electronAPI.onTtsEvent(function (data) {
      if (!configReady) { pendingTts = data; return; }
      handleTts(data);
    });
  }
  if (window.electronAPI && typeof window.electronAPI.onNewMessage === 'function') {
    window.electronAPI.onNewMessage(function (data) {
      if (data && (data.role === 'user' || data.role === 'chat_busy')) stop();
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
    getState: function () { return { config: Object.assign({}, config), configEnabled: configEnabled, sessionEnabled: sessionEnabled, userOverride: userOverride, unavailable: unavailable, noChineseVoice: noChineseVoice, speechBlocked: speechBlocked }; }
  };
})();
