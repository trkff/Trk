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
socket.on('overview_update', function(data) {
  updateStatusIndicator(data.bot_status);
  // Page-specific handlers can listen via custom events
  document.dispatchEvent(new CustomEvent('hlUpdate', { detail: data }));
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
