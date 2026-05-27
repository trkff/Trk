/* ═══════════════════════════════════════════════════════════════════
   RazorHL Dashboard — Core JS
   SocketIO connection, utilities, status updates
   ═══════════════════════════════════════════════════════════════════ */

const socket = io();

// ── Status dot update ──────────────────────────────────────────────
function updateStatusIndicator(status) {
  const dot = document.getElementById('status-dot');
  const text = document.getElementById('status-text');
  if (!dot || !text) return;

  dot.className = 'status-dot ' + (status || 'stopped');
  const labels = {
    running: 'Rodando',
    paused: 'Pausado',
    stopped: 'Parado',
    error: 'Erro'
  };
  text.textContent = labels[status] || status || '--';
}

// ── SocketIO — overview push updates ───────────────────────────────
//
// Backend emits one `overview_update.<profile_id>` per running profile every
// 5s (plus a legacy `overview_update` for profile 1 only). The active-profile
// payload drives the sidebar status indicator + page-specific listeners.
// Status dots inside the profile dropdown listen to ALL profiles so users see
// their state live without switching context.

function _handleOverviewUpdate(data) {
  const pid = data && data.profile_id;
  // Always keep the dropdown dots fresh for whichever profile this event is for
  if (pid != null) {
    const li = document.querySelector('#profileList li[data-id="' + pid + '"]');
    if (li) {
      const dot = li.querySelector('.profile-status-dot');
      if (dot) dot.className = 'profile-status-dot ' + (data.bot_status || 'stopped');
    }
    if (pid === _activeProfileId) {
      const headDot = document.getElementById('profileStatusDot');
      if (headDot) headDot.className = 'profile-status-dot ' + (data.bot_status || 'stopped');
    }
  }
  // Only the active profile's payload drives the main UI
  if (pid != null && pid !== _activeProfileId) return;
  updateStatusIndicator(data.bot_status);
  document.dispatchEvent(new CustomEvent('hlUpdate', { detail: data }));
}

// Catch every `overview_update.<pid>` event (Socket.IO v3+ supports onAny)
if (typeof socket.onAny === 'function') {
  socket.onAny(function(event, data) {
    if (typeof event === 'string' && event.indexOf('overview_update.') === 0) {
      _handleOverviewUpdate(data);
    }
  });
}

// Legacy fallback: profile 1 still emits `overview_update` for backwards-compat
socket.on('overview_update', function(data) {
  _handleOverviewUpdate(data);
});

socket.on('connect', function() {
  console.log('Dashboard connected');
});

// ── Utility functions ──────────────────────────────────────────────
function formatPnl(value) {
  const v = parseFloat(value) || 0;
  const sign = v >= 0 ? '+' : '';
  return sign + v.toFixed(2);
}

function pnlClass(value) {
  const v = parseFloat(value) || 0;
  if (v > 0) return 'positive';
  if (v < 0) return 'negative';
  return '';
}

function sideClass(side) {
  return side === 'long' ? 'side-long' : 'side-short';
}

function formatTime(isoStr) {
  if (!isoStr) return '--';
  const d = new Date(isoStr);
  return d.toLocaleString('pt-BR', { timeZone: 'America/Sao_Paulo' });
}

function formatTimeShort(isoStr) {
  if (!isoStr) return '--';
  const d = new Date(isoStr);
  return d.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit', second: '2-digit', timeZone: 'America/Sao_Paulo' });
}

function showToast(message, isError) {
  const toast = document.getElementById('toast');
  if (!toast) return;
  toast.textContent = message;
  toast.className = 'toast show' + (isError ? ' error' : '');
  setTimeout(() => { toast.className = 'toast'; }, 3500);
}

async function apiPost(url) {
  try {
    const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' } });
    return await res.json();
  } catch (e) {
    console.error('API error:', e);
    return { error: e.message };
  }
}

async function apiGet(url) {
  try {
    const res = await fetch(url);
    return await res.json();
  } catch (e) {
    console.error('API error:', e);
    return null;
  }
}

// ── Profile selector ────────────────────────────────────────────────
let _profilesCache = [];
let _activeProfileId = null;

async function loadProfiles() {
  const list = await apiGet('/api/profiles');
  if (!Array.isArray(list)) return;
  _profilesCache = list;
  const active = list.find(p => p.is_active) || list[0];
  _activeProfileId = active ? active.id : null;
  renderProfileMenu();
}

function renderProfileMenu() {
  const ul = document.getElementById('profileList');
  if (!ul) return;
  ul.innerHTML = '';
  for (const p of _profilesCache) {
    const li = document.createElement('li');
    li.dataset.id = p.id;
    if (p.id === _activeProfileId) li.classList.add('active');
    const dot = document.createElement('span');
    dot.className = 'profile-status-dot ' + (p.bot_status || 'stopped');
    const name = document.createElement('span');
    name.textContent = p.name;
    li.appendChild(dot);
    li.appendChild(name);
    li.addEventListener('click', () => activateProfile(p.id));
    ul.appendChild(li);
  }
  const active = _profilesCache.find(p => p.id === _activeProfileId);
  if (active) {
    const nameEl = document.getElementById('profileCurrentName');
    const dotEl = document.getElementById('profileStatusDot');
    if (nameEl) nameEl.textContent = active.name;
    if (dotEl) dotEl.className = 'profile-status-dot ' + (active.bot_status || 'stopped');
  }
}

async function activateProfile(pid) {
  if (pid === _activeProfileId) {
    closeProfileMenu();
    return;
  }
  const res = await fetch('/api/profiles/' + pid + '/activate', { method: 'POST' });
  if (res.ok) window.location.reload();
}

function openProfileMenu() {
  const m = document.getElementById('profileMenu');
  const btn = document.getElementById('profileCurrentBtn');
  if (!m) return;
  m.hidden = false;
  if (btn) btn.setAttribute('aria-expanded', 'true');
}
function closeProfileMenu() {
  const m = document.getElementById('profileMenu');
  const btn = document.getElementById('profileCurrentBtn');
  if (!m) return;
  m.hidden = true;
  if (btn) btn.setAttribute('aria-expanded', 'false');
}
function toggleProfileMenu() {
  const m = document.getElementById('profileMenu');
  if (!m) return;
  if (m.hidden) openProfileMenu(); else closeProfileMenu();
}

function openProfileModal(opts) {
  const back = document.getElementById('profileModalBackdrop');
  const title = document.getElementById('profileModalTitle');
  const form = document.getElementById('profileForm');
  if (!back || !form) return;
  form.reset();
  form.dataset.mode = opts.mode;
  form.dataset.id = opts.id != null ? String(opts.id) : '';
  const titles = { create: 'Novo perfil', rename: 'Renomear perfil', creds: 'Editar credenciais' };
  title.textContent = titles[opts.mode] || 'Perfil';
  form.querySelectorAll('fieldset.creds').forEach(fs => {
    fs.style.display = (opts.mode === 'rename') ? 'none' : '';
  });
  if (opts.mode !== 'create') {
    const p = _profilesCache.find(x => x.id === opts.id);
    if (p) {
      form.name.value = p.name || '';
      if (form.exchange) form.exchange.value = p.exchange || 'lighter';
      if (form.lighter_wallet_address) form.lighter_wallet_address.value = p.lighter_wallet_address || '';
      if (form.hyperliquid_address) form.hyperliquid_address.value = p.hyperliquid_address || '';
    }
  }
  back.hidden = false;
}
function closeProfileModal() {
  const back = document.getElementById('profileModalBackdrop');
  if (back) back.hidden = true;
}

async function deleteActiveProfile() {
  const active = _profilesCache.find(p => p.id === _activeProfileId);
  const label = active ? active.name : 'este perfil';
  if (!confirm('Excluir "' + label + '"? Trades, signals e configs do perfil serao removidos.')) return;
  const res = await fetch('/api/profiles/' + _activeProfileId, { method: 'DELETE' });
  if (res.status === 204) { window.location.reload(); return; }
  let msg = 'HTTP ' + res.status;
  try { const b = await res.json(); if (b && b.error) msg = b.error; } catch (e) {}
  alert('Nao foi possivel excluir: ' + msg);
}

document.addEventListener('DOMContentLoaded', () => {
  const btn = document.getElementById('profileCurrentBtn');
  const sel = document.getElementById('profileSelector');
  const back = document.getElementById('profileModalBackdrop');
  const form = document.getElementById('profileForm');
  if (!btn || !sel) return;

  btn.addEventListener('click', (e) => { e.stopPropagation(); toggleProfileMenu(); });

  sel.addEventListener('click', (e) => {
    const action = e.target && e.target.dataset && e.target.dataset.action;
    if (!action) return;
    e.stopPropagation();
    closeProfileMenu();
    if (action === 'new') openProfileModal({ mode: 'create' });
    else if (action === 'rename') openProfileModal({ mode: 'rename', id: _activeProfileId });
    else if (action === 'edit-creds') openProfileModal({ mode: 'creds', id: _activeProfileId });
    else if (action === 'delete') deleteActiveProfile();
  });

  document.addEventListener('click', (e) => {
    if (sel.contains(e.target)) return;
    closeProfileMenu();
  });

  if (back) {
    back.addEventListener('click', (e) => {
      if (e.target === back || (e.target.dataset && e.target.dataset.close !== undefined)) closeProfileModal();
    });
  }

  if (form) {
    const exch = form.querySelector('select[name="exchange"]');
    const updateExchangeFields = () => {
      const mode = form.dataset.mode;
      if (mode === 'rename') return;
      const v = exch.value;
      const lt = form.querySelector('.creds-lighter');
      const hl = form.querySelector('.creds-hl');
      if (lt) lt.hidden = v !== 'lighter';
      if (hl) hl.hidden = v !== 'hyperliquid';
    };
    if (exch) exch.addEventListener('change', updateExchangeFields);

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      const mode = form.dataset.mode;
      const id = form.dataset.id;
      const credKeys = ['lighter_wallet_address','lighter_public_key','lighter_private_key',
                         'hyperliquid_address','hyperliquid_secret'];
      const creds = {};
      credKeys.forEach(k => {
        const v = (fd.get(k) || '').toString().trim();
        if (v) creds[k] = v;
      });
      let res;
      if (mode === 'create') {
        res = await fetch('/api/profiles', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            name: (fd.get('name') || '').toString().trim(),
            exchange: fd.get('exchange') || 'lighter',
            credentials: creds,
          }),
        });
      } else {
        const body = (mode === 'rename')
          ? { name: (fd.get('name') || '').toString().trim() }
          : { exchange: fd.get('exchange') || 'lighter', credentials: creds };
        res = await fetch('/api/profiles/' + id, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
      }
      if (!res.ok) {
        let msg = 'HTTP ' + res.status;
        try { const b = await res.json(); if (b && b.error) msg = b.error; } catch (e) {}
        alert(msg);
        return;
      }
      closeProfileModal();
      window.location.reload();
    });
  }

  loadProfiles();
});
