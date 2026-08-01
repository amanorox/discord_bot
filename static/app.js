const scriptEl   = document.getElementById('script');
const offsetEl   = document.getElementById('offset');
const statusEl   = document.getElementById('status-text');
const dotEl      = document.getElementById('dot');
const wsDotEl    = document.getElementById('ws-dot');
const btnPlay    = document.getElementById('btn-play');
const btnStop    = document.getElementById('btn-stop');
const btnLeave   = document.getElementById('btn-leave');
const nameEl     = document.getElementById('script-name');
const selectEl   = document.getElementById('script-select');
const btnSave    = document.getElementById('btn-save');
const btnLoad    = document.getElementById('btn-load');
const btnDelete  = document.getElementById('btn-delete');
const channelSelectEl = document.getElementById('channel-select');

let isPlaying = false;
let botReady  = false;

// ---------- Voice channel selection (localStorage) ----------
const LAST_CHANNEL_KEY = 'discordBotController.lastChannelId';

function getSavedChannelId() {
  const raw = localStorage.getItem(LAST_CHANNEL_KEY);
  return raw ? raw : '';
}

function saveChannelId(channelId) {
  if (channelId) {
    localStorage.setItem(LAST_CHANNEL_KEY, String(channelId));
  }
}

function getSelectedChannelId() {
  // Keep as string: Discord snowflake IDs exceed JS Number safe-integer
  // range, so converting to a Number here would lose precision.
  const value = channelSelectEl.value;
  return value ? value : null;
}

async function loadVoiceChannels() {
  try {
    const res = await fetch('/voice_channels');
    if (!res.ok) {
      channelSelectEl.innerHTML = '<option value="">-- 取得失敗 --</option>';
      return;
    }
    const data = await res.json();
    if (!data.ok) {
      channelSelectEl.innerHTML = '<option value="">-- 取得失敗 --</option>';
      return;
    }

    const savedId = getSavedChannelId();
    const defaultId = String(data.default_channel_id);
    channelSelectEl.innerHTML = '';
    for (const ch of data.channels) {
      const opt = document.createElement('option');
      opt.value = String(ch.id);
      opt.textContent = `${ch.guild_name} / ${ch.name}`;
      channelSelectEl.appendChild(opt);
    }

    const idToSelect = [...channelSelectEl.options].some(o => o.value === savedId)
      ? savedId
      : ([...channelSelectEl.options].some(o => o.value === defaultId) ? defaultId : '');
    if (idToSelect) {
      channelSelectEl.value = idToSelect;
    }
  } catch (e) {
    channelSelectEl.innerHTML = '<option value="">-- 取得失敗 --</option>';
  }
}

channelSelectEl.addEventListener('change', () => {
  saveChannelId(channelSelectEl.value);
});

// ---------- Saved scripts (localStorage) ----------
const STORAGE_KEY = 'discordBotController.savedScripts';

function loadSavedScripts() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch (e) {
    return {};
  }
}

function persistSavedScripts(scripts) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(scripts));
}

function refreshScriptSelect(selectName = '') {
  const scripts = loadSavedScripts();
  const names = Object.keys(scripts).sort((a, b) => a.localeCompare(b, 'ja'));
  selectEl.innerHTML = '<option value="">-- 選択 --</option>';
  for (const name of names) {
    const opt = document.createElement('option');
    opt.value = name;
    opt.textContent = name;
    selectEl.appendChild(opt);
  }
  if (selectName && names.includes(selectName)) {
    selectEl.value = selectName;
  }
}

btnSave.addEventListener('click', () => {
  const name = nameEl.value.trim();
  if (!name) { setStatus('スクリプト名を入力してください。', 'error'); return; }
  const scripts = loadSavedScripts();
  scripts[name] = { script: scriptEl.value, offset: offsetEl.value };
  persistSavedScripts(scripts);
  refreshScriptSelect(name);
  setStatus(`「${name}」を保存しました。`, 'idle');
});

btnLoad.addEventListener('click', () => {
  const name = selectEl.value;
  if (!name) { setStatus('ロードするスクリプトを選択してください。', 'error'); return; }
  const scripts = loadSavedScripts();
  const entry = scripts[name];
  if (!entry) { setStatus('選択したスクリプトが見つかりません。', 'error'); return; }
  scriptEl.value = entry.script ?? '';
  offsetEl.value = entry.offset ?? 0;
  nameEl.value = name;
  setStatus(`「${name}」をロードしました。`, 'idle');
});

btnDelete.addEventListener('click', () => {
  const name = selectEl.value;
  if (!name) { setStatus('削除するスクリプトを選択してください。', 'error'); return; }
  const scripts = loadSavedScripts();
  delete scripts[name];
  persistSavedScripts(scripts);
  refreshScriptSelect();
  setStatus(`「${name}」を削除しました。`, 'idle');
});

// ---------- UI helpers ----------
function setStatus(message, state /* 'idle' | 'busy' | 'error' */ = 'idle') {
  statusEl.textContent = message;
  dotEl.className = 'dot ' + state;
}

function refreshButtons() {
  btnPlay.disabled  = !botReady || isPlaying;
  btnStop.disabled  = !botReady || !isPlaying;
  btnLeave.disabled = !botReady || isPlaying;
}

// ---------- WebSocket ----------
let ws = null;
let wsRetryDelay = 1000;

function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.addEventListener('open', () => {
    wsDotEl.classList.add('connected');
    wsRetryDelay = 1000;
  });

  ws.addEventListener('message', (ev) => {
    try {
      const data = JSON.parse(ev.data);
      isPlaying = !!data.is_playing;
      setStatus(data.message, isPlaying ? 'busy' : 'idle');
      refreshButtons();
    } catch (e) { /* ignore */ }
  });

  ws.addEventListener('close', () => {
    wsDotEl.classList.remove('connected');
    setStatus('サーバーへの接続が切れました。再接続しています...', 'error');
    setTimeout(connectWS, wsRetryDelay);
    wsRetryDelay = Math.min(wsRetryDelay * 2, 30000);
  });

  ws.addEventListener('error', () => { ws.close(); });
}

// ---------- Initial status poll ----------
async function pollStatus() {
  try {
    const res = await fetch('/status');
    if (!res.ok) return;
    const data = await res.json();
    botReady  = data.bot_ready;
    isPlaying = data.is_playing;
    if (!botReady) {
      setStatus('Bot起動中...', 'idle');
    } else if (isPlaying) {
      setStatus('再生中...', 'busy');
    } else {
      setStatus('待機中。', 'idle');
    }
    refreshButtons();
    if (!botReady) setTimeout(pollStatus, 2000);
  } catch (e) {
    setTimeout(pollStatus, 3000);
  }
}

// ---------- API helpers ----------
async function apiPost(path, body = {}) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return res.json();
}

// ---------- Button handlers ----------
btnPlay.addEventListener('click', async () => {
  const script   = scriptEl.value.trim();
  const offsetRaw = offsetEl.value.trim();
  const offset   = offsetRaw === '' ? 0 : parseInt(offsetRaw, 10);
  const channelId = getSelectedChannelId();

  if (!script)     { setStatus('スクリプトを入力してください。', 'error'); return; }
  if (isNaN(offset)) { setStatus('オフセット秒数は整数で入力してください。', 'error'); return; }
  if (!channelId)  { setStatus('参加するボイスチャンネルを選択してください。', 'error'); return; }

  btnPlay.disabled = true;
  setStatus('再生リクエスト送信中...', 'busy');

  try {
    const data = await apiPost('/play', { script, offset, channel_id: channelId });
    if (!data.ok) {
      setStatus(data.message, 'error');
      isPlaying = false;
    } else {
      isPlaying = true;
      saveChannelId(channelId);
    }
  } catch (e) {
    setStatus(`通信エラー: ${e}`, 'error');
    isPlaying = false;
  }
  refreshButtons();
});

btnStop.addEventListener('click', async () => {
  btnStop.disabled = true;
  setStatus('停止リクエスト送信中...', 'busy');
  try {
    const data = await apiPost('/stop', { channel_id: getSelectedChannelId() });
    setStatus(data.message, data.ok ? 'idle' : 'error');
    isPlaying = false;
  } catch (e) {
    setStatus(`通信エラー: ${e}`, 'error');
  }
  refreshButtons();
});

btnLeave.addEventListener('click', async () => {
  btnLeave.disabled = true;
  setStatus('退室リクエスト送信中...', 'busy');
  try {
    const data = await apiPost('/leave', { channel_id: getSelectedChannelId() });
    setStatus(data.message, data.ok ? 'idle' : 'error');
    isPlaying = false;
  } catch (e) {
    setStatus(`通信エラー: ${e}`, 'error');
  }
  refreshButtons();
});

// ---------- Boot ----------
connectWS();
pollStatus();
refreshScriptSelect();
loadVoiceChannels();
