const scriptEl = document.getElementById('script');
const offsetEl = document.getElementById('offset');
const statusEl = document.getElementById('status-text');
const dotEl    = document.getElementById('dot');
const wsDotEl  = document.getElementById('ws-dot');
const btnPlay  = document.getElementById('btn-play');
const btnStop  = document.getElementById('btn-stop');
const btnLeave = document.getElementById('btn-leave');

let isPlaying = false;
let botReady  = false;

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

  if (!script)     { setStatus('スクリプトを入力してください。', 'error'); return; }
  if (isNaN(offset)) { setStatus('オフセット秒数は整数で入力してください。', 'error'); return; }

  btnPlay.disabled = true;
  setStatus('再生リクエスト送信中...', 'busy');

  try {
    const data = await apiPost('/play', { script, offset });
    if (!data.ok) {
      setStatus(data.message, 'error');
      isPlaying = false;
    } else {
      isPlaying = true;
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
    const data = await apiPost('/stop');
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
    const data = await apiPost('/leave');
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

