// Laguna Translator — frontend
const API = {
  devices: () => fetch('/api/devices').then(r => r.json()),
  start: (dir, cfg) => fetch(`/api/start/${dir}`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(cfg)
  }).then(r => r.json()),
  stop: (dir) => fetch(`/api/stop/${dir}`, {method: 'POST'}).then(r => r.json()),
  gain: (dir, body) => fetch(`/api/gain/${dir}`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  }).then(r => r.json()).catch(() => null),
};

const state = {
  devices: {inputs: [], outputs: [], loopbacks: [], laguna: {}},
  running: new Set(),
};

const LS_PREFIX = 'laguna';
const LS_KEYS = {
  theme: `${LS_PREFIX}-theme`,
  lang: `${LS_PREFIX}-lang`,
  cfg: (dir) => `${LS_PREFIX}-cfg-${dir}`,
};

// Campos que persistimos por painel.
const PERSIST_FIELDS = {
  falar: [
    ['src_lang', 'value'],
    ['tgt_lang', 'value'],
    ['capture_device', 'value'],
    ['virtual_out', 'value'],
    ['fone_device', 'value'],
    ['also_fone', 'checked'],
    ['passthrough_falar', 'checked'],
    ['model_size', 'value'],
    ['device', 'value'],
    ['skip_same_lang', 'checked'],
    ['output_gain_db', 'value'],
    ['passthrough_gain_db', 'value'],
  ],
  escutar: [
    ['src_lang', 'value'],
    ['tgt_lang', 'value'],
    ['capture_device', 'value'],
    ['fone_device', 'value'],
    ['use_loopback', 'checked'],
    ['passthrough_escutar', 'checked'],
    ['model_size', 'value'],
    ['device', 'value'],
    ['skip_same_lang', 'checked'],
    ['output_gain_db', 'value'],
    ['passthrough_gain_db', 'value'],
  ],
};

function $all(sel, root = document) { return Array.from(root.querySelectorAll(sel)); }
function $(sel, root = document) { return root.querySelector(sel); }
function $role(panel, role) { return panel.querySelector(`[data-role="${role}"]`); }

// Resolve eventos do backend: se trouxer `key`, traduz via LAGUNA_T e interpola
// `{placeholder}` com `args`. Senão, fallback para `ev.msg` cru (compat).
function resolveEvent(ev) {
  const T = window.LAGUNA_T || ((k) => k);
  if (ev && ev.key) {
    const tpl = T(ev.key);
    const args = ev.args || {};
    return tpl.replace(/\{(\w+)\}/g, (_, k) => (args[k] !== undefined ? args[k] : `{${k}}`));
  }
  return ev && ev.msg ? ev.msg : '';
}

function fillSelect(sel, items, placeholderKey) {
  const ph = placeholderKey ? (window.LAGUNA_T ? window.LAGUNA_T(placeholderKey) : placeholderKey) : null;
  sel.innerHTML = '';
  if (ph) {
    const opt = document.createElement('option');
    opt.value = '';
    opt.textContent = ph;
    sel.appendChild(opt);
  }
  for (const d of items) {
    const opt = document.createElement('option');
    opt.value = d.index;
    opt.textContent = d.label;
    if (d.tags.includes('laguna')) opt.dataset.laguna = '1';
    if (d.tags.includes('vb_cable_in') || d.tags.includes('vb_cable_out')) opt.dataset.cable = '1';
    sel.appendChild(opt);
  }
}

function selectByPreference(sel, preferredTag) {
  for (const opt of sel.options) {
    if (opt.dataset[preferredTag] === '1') { sel.value = opt.value; return true; }
  }
  return false;
}

async function loadDevices() {
  state.devices = await API.devices();
  updateLagunaBadge();
  populatePanels();
  // restaura configs salvas DEPOIS de popular (senão os selects ainda não têm opções)
  restorePanelConfig('falar');
  restorePanelConfig('escutar');
  // também aplica toggle de fone_row conforme also_fone
  syncFoneRow();
  // refresca labels de volume caso tenham sido restaurados de localStorage
  refreshVolumeLabels('falar');
  refreshVolumeLabels('escutar');
}

// Re-detecta dispositivos sem recarregar a página. Reaproveita loadDevices()
// e preserva a escolha explícita do usuário quando o device ainda existe —
// não deixa a preferência automática de populatePanels() sobrescrevê-la.
async function refreshDevices() {
  const btn = document.getElementById('refresh-devices');
  if (btn && btn.disabled) return; // evita reentrância se clicar em sequência

  // snapshot dos selects atuais ANTES de re-popular (fillSelect limpa as opções)
  const snapshot = {};
  for (const dir of ['falar', 'escutar']) {
    const panel = document.querySelector(`.panel[data-dir="${dir}"]`);
    if (!panel) continue;
    snapshot[dir] = {};
    for (const [role, prop] of PERSIST_FIELDS[dir]) {
      if (prop !== 'value') continue;
      const el = $role(panel, role);
      if (el && el.tagName === 'SELECT') snapshot[dir][role] = el.value;
    }
  }

  if (btn) { btn.disabled = true; btn.classList.add('is-refreshing'); }
  try {
    await loadDevices();
    // re-aplica a seleção capturada por cima da preferência automática,
    // mas só quando o device continua existindo entre as opções
    for (const dir of ['falar', 'escutar']) {
      const panel = document.querySelector(`.panel[data-dir="${dir}"]`);
      if (!panel || !snapshot[dir]) continue;
      for (const [role, val] of Object.entries(snapshot[dir])) {
        if (val === '' || val == null) continue;
        const sel = $role(panel, role);
        if (sel && Array.from(sel.options).some(o => o.value === String(val))) {
          sel.value = String(val);
          savePanelConfig(dir);
        }
      }
    }
  } finally {
    if (btn) { btn.disabled = false; btn.classList.remove('is-refreshing'); }
  }
}

function refreshVolumeLabels(dir) {
  const panel = document.querySelector(`.panel[data-dir="${dir}"]`);
  if (!panel) return;
  const outVol = $role(panel, 'output_gain_db');
  const ptVol = $role(panel, 'passthrough_gain_db');
  const outLbl = $role(panel, 'vol_out_label');
  const ptLbl = $role(panel, 'vol_pt_label');
  if (outLbl && outVol) outLbl.textContent = `${parseFloat(outVol.value || '0')} dB`;
  if (ptLbl && ptVol) ptLbl.textContent = `${parseFloat(ptVol.value || '0')} dB`;
}

function updateLagunaBadge() {
  const el = document.getElementById('laguna-badge');
  const L = state.devices.laguna || {};
  const T = window.LAGUNA_T || ((k) => k);
  if (L.has_laguna_name) {
    el.textContent = T('badge.laguna_ok');
    el.className = 'badge ok';
  } else if (L.virtual_in != null || L.virtual_out != null) {
    el.textContent = T('badge.laguna_cable');
    el.className = 'badge warn';
  } else {
    el.textContent = T('badge.laguna_none');
    el.className = 'badge err';
  }
}

function populatePanels() {
  const {inputs, outputs, loopbacks, laguna} = state.devices;

  // FALAR
  const falar = document.querySelector('.panel[data-dir="falar"]');
  fillSelect($role(falar, 'capture_device'), inputs, 'placeholder.mic');
  fillSelect($role(falar, 'virtual_out'), outputs, 'placeholder.virtual_out');
  fillSelect($role(falar, 'fone_device'), outputs, 'placeholder.fone');

  // ESCUTAR: captura pode ser input OU WASAPI output p/ loopback
  const escutar = document.querySelector('.panel[data-dir="escutar"]');
  const useLoop = $role(escutar, 'use_loopback').checked;
  fillSelect($role(escutar, 'capture_device'), useLoop ? loopbacks : inputs, 'placeholder.capture');
  fillSelect($role(escutar, 'fone_device'), outputs, 'placeholder.fone');

  // preferencias automáticas (só se nada salvo vai sobrepor)
  if (laguna.virtual_out != null) $role(falar, 'virtual_out').value = laguna.virtual_out;
  else selectByPreference($role(falar, 'virtual_out'), 'cable');

  if (laguna.virtual_in != null && !useLoop) $role(escutar, 'capture_device').value = laguna.virtual_in;
}

function syncFoneRow() {
  const falar = document.querySelector('.panel[data-dir="falar"]');
  const alsoFone = $role(falar, 'also_fone');
  const row = $role(falar, 'fone_row');
  if (alsoFone && row) row.classList.toggle('hidden', !alsoFone.checked);
}

function bindPanel(dir) {
  const panel = document.querySelector(`.panel[data-dir="${dir}"]`);

  // fone toggle (só FALAR)
  const alsoFone = $role(panel, 'also_fone');
  if (alsoFone) {
    alsoFone.addEventListener('change', () => {
      $role(panel, 'fone_row').classList.toggle('hidden', !alsoFone.checked);
      savePanelConfig(dir);
    });
  }

  // loopback toggle (só ESCUTAR) — re-popula captura
  const loopCk = $role(panel, 'use_loopback');
  if (loopCk) loopCk.addEventListener('change', () => {
    populatePanels();
    savePanelConfig(dir);
  });

  // start/stop
  $role(panel, 'start').addEventListener('click', () => startDirection(dir));
  $role(panel, 'stop').addEventListener('click', () => stopDirection(dir));

  // persistência: escuta change em todos os campos do painel
  for (const [role, _prop] of PERSIST_FIELDS[dir]) {
    const el = $role(panel, role);
    if (el) el.addEventListener('change', () => savePanelConfig(dir));
  }

  // volume sliders: atualiza label, persiste e empurra ao backend em tempo real
  const outVol = $role(panel, 'output_gain_db');
  const ptVol = $role(panel, 'passthrough_gain_db');
  const outLbl = $role(panel, 'vol_out_label');
  const ptLbl = $role(panel, 'vol_pt_label');
  const renderVol = () => {
    if (outLbl && outVol) outLbl.textContent = `${parseFloat(outVol.value || '0')} dB`;
    if (ptLbl && ptVol) ptLbl.textContent = `${parseFloat(ptVol.value || '0')} dB`;
  };
  renderVol();
  const pushGain = () => {
    if (!state.running.has(dir)) return;
    API.gain(dir, {
      output_gain_db: parseFloat(outVol.value || '0'),
      passthrough_gain_db: parseFloat(ptVol.value || '0'),
    });
  };
  if (outVol) {
    outVol.addEventListener('input', () => { renderVol(); pushGain(); });
    outVol.addEventListener('change', () => savePanelConfig(dir));
  }
  if (ptVol) {
    ptVol.addEventListener('input', () => { renderVol(); pushGain(); });
    ptVol.addEventListener('change', () => savePanelConfig(dir));
  }
}

function savePanelConfig(dir) {
  try {
    const panel = document.querySelector(`.panel[data-dir="${dir}"]`);
    const data = {};
    for (const [role, prop] of PERSIST_FIELDS[dir]) {
      const el = $role(panel, role);
      if (el) data[role] = el[prop];
    }
    localStorage.setItem(LS_KEYS.cfg(dir), JSON.stringify(data));
  } catch {}
}

function restorePanelConfig(dir) {
  try {
    const raw = localStorage.getItem(LS_KEYS.cfg(dir));
    if (!raw) return;
    const data = JSON.parse(raw);
    const panel = document.querySelector(`.panel[data-dir="${dir}"]`);
    for (const [role, prop] of PERSIST_FIELDS[dir]) {
      const el = $role(panel, role);
      if (!el) continue;
      if (data[role] == null) continue;
      // para <select>, só seta se a opção existe
      if (prop === 'value' && el.tagName === 'SELECT') {
        const val = String(data[role]);
        if (Array.from(el.options).some(o => o.value === val)) {
          el.value = val;
        }
      } else {
        el[prop] = data[role];
      }
    }
  } catch {}
}

async function startDirection(dir) {
  const panel = document.querySelector(`.panel[data-dir="${dir}"]`);
  const cfg = buildConfig(panel, dir);
  if (!cfg) return;
  setStatusKey(panel, 'loading', 'status.loading');
  toggleButtons(panel, true);
  const res = await API.start(dir, cfg);
  if (res.error) {
    // 400 do backend: resolve error_key via i18n e interpola {device}/{detail}
    // (reusa resolveEvent); sem key, cai no texto cru de res.error.
    const text = resolveEvent({ key: res.error_key, args: res.args, msg: res.error });
    setStatus(panel, 'error', text);
    toggleButtons(panel, false);
    return;
  }
  state.running.add(dir);
}

async function stopDirection(dir) {
  const panel = document.querySelector(`.panel[data-dir="${dir}"]`);
  await API.stop(dir);
  state.running.delete(dir);
  setStatusKey(panel, 'idle', 'status.idle');
  toggleButtons(panel, false);
  // zera meters
  const mi = $role(panel, 'meter-in');
  const mo = $role(panel, 'meter-out');
  if (mi) mi.style.width = '0%';
  if (mo) mo.style.width = '0%';
}

function buildConfig(panel, dir) {
  const T = window.LAGUNA_T || ((k) => k);
  const cap = $role(panel, 'capture_device').value;
  if (!cap) { alert(T('alert.need_capture')); return null; }

  const cfg = {
    src_lang: $role(panel, 'src_lang').value,
    tgt_lang: $role(panel, 'tgt_lang').value,
    capture_device: parseInt(cap, 10),
    model_size: $role(panel, 'model_size').value,
    device: $role(panel, 'device').value,
    skip_same_lang: $role(panel, 'skip_same_lang').checked,
    use_loopback: false,
    output_devices: [],
    output_gain_db: parseFloat($role(panel, 'output_gain_db').value || '0'),
    passthrough_gain_db: parseFloat($role(panel, 'passthrough_gain_db').value || '0'),
  };

  if (dir === 'falar') {
    const vo = $role(panel, 'virtual_out').value;
    if (vo) cfg.output_devices.push(parseInt(vo, 10));
    if ($role(panel, 'also_fone').checked) {
      const f = $role(panel, 'fone_device').value;
      if (f) cfg.output_devices.push(parseInt(f, 10));
    }
    // FALAR passthrough: envia voz original para a saída virtual (Discord).
    const ptFalar = $role(panel, 'passthrough_falar');
    if (ptFalar && ptFalar.checked) {
      if (!vo) { alert(T('alert.need_fone_for_passthrough')); return null; }
      if (String(cfg.capture_device) === String(vo)) {
        alert(T('alert.feedback'));
        return null;
      }
      cfg.passthrough = true;
      cfg.passthrough_device = parseInt(vo, 10);
    }
  } else {
    const f = $role(panel, 'fone_device').value;
    if (f) cfg.output_devices.push(parseInt(f, 10));
    cfg.use_loopback = $role(panel, 'use_loopback').checked;

    const pt = $role(panel, 'passthrough_escutar');
    if (pt && pt.checked) {
      if (!f) { alert(T('alert.need_fone_for_passthrough')); return null; }
      if (String(cfg.capture_device) === String(f)) {
        alert(T('alert.feedback'));
        return null;
      }
      cfg.passthrough = true;
      cfg.passthrough_device = parseInt(f, 10);
    }
  }

  if (cfg.src_lang === cfg.tgt_lang) {
    alert(T('alert.same_langs'));
    return null;
  }
  return cfg;
}

function setStatus(panel, cls, text) {
  const el = $role(panel, 'status');
  el.className = `status ${cls}`;
  el.textContent = text;
  el.removeAttribute('data-i18n'); // texto custom — não reaplica via i18n
}

function setStatusKey(panel, cls, key) {
  const el = $role(panel, 'status');
  el.className = `status ${cls}`;
  el.setAttribute('data-i18n', key);
  el.textContent = window.LAGUNA_T ? window.LAGUNA_T(key) : key;
}

function toggleButtons(panel, running) {
  $role(panel, 'start').disabled = running;
  $role(panel, 'stop').disabled = !running;
}

function onEvent(ev) {
  const dir = ev.dir;
  if (!dir) {
    if (ev.kind === 'hello' && Array.isArray(ev.running)) {
      for (const d of ev.running) {
        state.running.add(d);
        const p = document.querySelector(`.panel[data-dir="${d}"]`);
        if (p) { setStatusKey(p, 'running', 'status.running'); toggleButtons(p, true); }
      }
    }
    return;
  }
  const panel = document.querySelector(`.panel[data-dir="${dir}"]`);
  if (!panel) return;

  switch (ev.kind) {
    case 'status':
      if (ev.key === 'status.loading_models') setStatus(panel, 'loading', resolveEvent(ev));
      else setStatus(panel, 'running', resolveEvent(ev));
      break;
    case 'ready':
      setStatusKey(panel, 'running', 'status.running');
      break;
    case 'stt': {
      const el = $role(panel, 'stt');
      const langTag = ev.lang ? ` <span style="color:var(--text-dim);font-size:11px">[${ev.lang}]</span>` : '';
      el.innerHTML = escapeHTML(ev.text) + langTag;
      break;
    }
    case 'mt':
      $role(panel, 'mt').textContent = ev.text;
      break;
    case 'skipped': {
      const el = $role(panel, 'mt');
      el.textContent = window.LAGUNA_T ? window.LAGUNA_T('mt.skipped') : '(same language)';
      el.classList.add('skipped');
      setTimeout(() => el.classList.remove('skipped'), 1500);
      break;
    }
    case 'level': {
      const mi = $role(panel, 'meter-in');
      const mo = $role(panel, 'meter-out');
      // escala logarítmica leve: rms ∈ [0, ~0.5] → largura 0..100%
      const scale = (v) => {
        const x = Math.max(0, Math.min(1, v || 0));
        const pct = Math.min(100, Math.pow(x, 0.5) * 180);
        return pct.toFixed(1) + '%';
      };
      if (mi && ev.in_level != null) mi.style.width = scale(ev.in_level);
      if (mo && ev.out_level != null) mo.style.width = scale(ev.out_level);
      break;
    }
    case 'latency':
      $role(panel, 'p50').textContent = ev.total_p50 ?? '—';
      $role(panel, 'p95').textContent = ev.total_p95 ?? '—';
      $role(panel, 'n').textContent = ev.samples ?? 0;
      break;
    case 'error':
      setStatus(panel, 'error', resolveEvent(ev));
      toggleButtons(panel, false);
      state.running.delete(dir);
      break;
  }
}

function escapeHTML(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'
  }[c]));
}

// Indicador global de conexão WS: reaproveita as classes de badge (ok/warn/err)
// e mantém o rótulo traduzível — seta o data-i18n do label para que o toggle
// PT/EN reaplique o estado atual via applyI18n.
function setConnState(cls, key) {
  const el = document.getElementById('conn-badge');
  if (!el) return;
  el.className = `badge ${cls}`;
  const label = el.querySelector('[data-role="conn-label"]');
  if (label) {
    label.setAttribute('data-i18n', key);
    label.textContent = window.LAGUNA_T ? window.LAGUNA_T(key) : key;
  }
}

// Backoff exponencial com teto para reconexão do WS.
const WS_BACKOFF_MIN = 500;
const WS_BACKOFF_MAX = 10000;
let wsBackoff = WS_BACKOFF_MIN;
let wsReconnectTimer = null;

function scheduleReconnect() {
  // No teto tratamos como offline (servidor provavelmente caiu); antes disso,
  // "reconectando" para sinalizar que ainda estamos tentando.
  const atCap = wsBackoff >= WS_BACKOFF_MAX;
  setConnState(atCap ? 'err' : 'warn', atCap ? 'conn.offline' : 'conn.reconnecting');
  clearTimeout(wsReconnectTimer);
  wsReconnectTimer = setTimeout(connectWS, wsBackoff);
  wsBackoff = Math.min(wsBackoff * 2, WS_BACKOFF_MAX);
}

function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  let ws;
  try {
    ws = new WebSocket(`${proto}://${location.host}/ws`);
  } catch {
    scheduleReconnect();
    return;
  }
  ws.onopen = () => {
    wsBackoff = WS_BACKOFF_MIN;
    setConnState('ok', 'conn.online');
  };
  ws.onmessage = e => {
    try { onEvent(JSON.parse(e.data)); } catch {}
  };
  // onerror não reagenda: o onclose que o segue faz o reschedule (evita timer duplo).
  ws.onclose = () => scheduleReconnect();
}

function applyTheme(theme) {
  if (theme === 'light') {
    document.documentElement.setAttribute('data-theme', 'light');
    const btn = document.getElementById('theme-toggle');
    if (btn) btn.textContent = '☀️';
  } else {
    document.documentElement.removeAttribute('data-theme');
    const btn = document.getElementById('theme-toggle');
    if (btn) btn.textContent = '🌙';
  }
  try { localStorage.setItem(LS_KEYS.theme, theme); } catch {}
}

function initTheme() {
  let saved;
  try { saved = localStorage.getItem(LS_KEYS.theme); } catch {}
  const preferred = saved || (window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
  applyTheme(preferred);

  const btn = document.getElementById('theme-toggle');
  if (btn) {
    btn.addEventListener('click', () => {
      const cur = document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
      applyTheme(cur === 'light' ? 'dark' : 'light');
    });
  }
  document.addEventListener('keydown', (e) => {
    if (e.shiftKey && (e.key === 'T' || e.key === 't')) {
      const cur = document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
      applyTheme(cur === 'light' ? 'dark' : 'light');
    }
  });
}

function initLang() {
  let saved;
  try { saved = localStorage.getItem(LS_KEYS.lang); } catch {}
  const browser = (navigator.language || 'pt').toLowerCase().startsWith('pt') ? 'pt' : 'en';
  const lang = saved || browser;
  window.applyI18n(lang);

  const btn = document.getElementById('lang-toggle');
  if (btn) {
    btn.addEventListener('click', () => {
      const cur = window.LAGUNA_LANG || 'pt';
      const next = cur === 'pt' ? 'en' : 'pt';
      window.applyI18n(next);
      try { localStorage.setItem(LS_KEYS.lang, next); } catch {}
      // atualiza badge e placeholders de selects (dependem de T)
      updateLagunaBadge();
      refreshPlaceholders();
    });
  }
}

function refreshPlaceholders() {
  // só atualiza o texto da primeira <option> (placeholder), sem reconstruir o select
  const map = [
    ['falar', 'capture_device', 'placeholder.mic'],
    ['falar', 'virtual_out', 'placeholder.virtual_out'],
    ['falar', 'fone_device', 'placeholder.fone'],
    ['escutar', 'capture_device', 'placeholder.capture'],
    ['escutar', 'fone_device', 'placeholder.fone'],
  ];
  for (const [dir, role, key] of map) {
    const panel = document.querySelector(`.panel[data-dir="${dir}"]`);
    if (!panel) continue;
    const sel = $role(panel, role);
    if (sel && sel.options.length && sel.options[0].value === '') {
      sel.options[0].textContent = window.LAGUNA_T(key);
    }
  }
}

document.addEventListener('DOMContentLoaded', async () => {
  initTheme();
  initLang();
  bindPanel('falar');
  bindPanel('escutar');
  const refreshBtn = document.getElementById('refresh-devices');
  if (refreshBtn) refreshBtn.addEventListener('click', refreshDevices);
  await loadDevices();
  connectWS();
});
