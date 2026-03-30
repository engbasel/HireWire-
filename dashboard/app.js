/* ═══════════════════════════════════════════════════════
   HireWire Dashboard — JavaScript
═══════════════════════════════════════════════════════ */

const API = '';            // Same origin (served by Flask)
let logOffset    = 0;
let logSSE       = null;
let pollTimer    = null;
let toastTimer   = null;
let displayedLogs = 0;

// ══════════════════════════════════════
// Navigation
// ══════════════════════════════════════
function navigate(page, el) {
  // Deactivate all
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));

  // Activate target
  if (el) el.classList.add('active');
  else document.querySelector(`[data-page="${page}"]`).classList.add('active');
  document.getElementById(`page-${page}`).classList.add('active');

  // Update topbar
  const titles = {
    dashboard: ['Dashboard', 'Overview & Control Center'],
    logs:      ['Live Logs', 'Real-time log stream'],
    projects:  ['Projects DB', 'Processed project memory'],
    settings:  ['Settings', 'Credentials & bot configuration'],
  };
  document.getElementById('page-title').textContent    = titles[page][0];
  document.getElementById('page-subtitle').textContent = titles[page][1];

  // Load data for specific pages
  if (page === 'projects') loadProjects();
  if (page === 'settings') loadSettings();
  if (page === 'logs') startLogStream();

  return false;
}

// ══════════════════════════════════════
// Status Polling
// ══════════════════════════════════════
async function pollStatus() {
  try {
    const res = await fetch(`${API}/api/bot/status`);
    if (!res.ok) throw new Error('API not reachable');
    const data = await res.json();

    const running = data.running;

    // Dot + label
    const dot = document.getElementById('status-dot');
    const label = document.getElementById('status-label');
    dot.className = 'status-dot ' + (running ? 'online' : 'offline');
    label.textContent = running ? 'Bot Running' : 'Bot Offline';

    // Buttons
    document.getElementById('start-btn').style.display = running ? 'none' : '';
    document.getElementById('stop-btn').style.display  = running ? '' : 'none';

    // Stat card
    const sv = document.getElementById('stat-status-val');
    sv.textContent = running ? '● Running' : '○ Offline';
    sv.style.color = running ? 'var(--green)' : 'var(--red)';

    // DB stats
    if (data.db_stats) {
      setText('stat-total', data.db_stats.total ?? '—');
      setText('stat-week',  data.db_stats.last_7_days ?? '—');
      setText('stat-today', data.db_stats.today ?? '—');
    }

    // Info
    setText('info-last-run', data.last_run || 'Never');
    setText('info-pid', data.pid ? `#${data.pid}` : '—');

    // Uptime
    document.getElementById('uptime-val').textContent = data.uptime || '00:00:00';

  } catch (e) {
    document.getElementById('status-label').textContent = 'Server offline';
    document.getElementById('status-dot').className = 'status-dot';
  }
}

function setText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}

// ══════════════════════════════════════
// Bot Control
// ══════════════════════════════════════
async function startBot() {
  const btn = document.getElementById('start-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="btn-icon">⏳</span> Starting...';

  try {
    const res = await fetch(`${API}/api/bot/start`, { method: 'POST' });
    const data = await res.json();
    if (data.ok) {
      toast('✅ ' + data.message, 'success');
      startLogStream();
    } else {
      toast('❌ ' + data.message, 'error');
    }
  } catch (e) {
    toast('❌ Could not reach API server.', 'error');
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<span class="btn-icon">▶</span> Start Bot';
    pollStatus();
  }
}

async function stopBot() {
  const btn = document.getElementById('stop-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="btn-icon">⏳</span> Stopping...';

  try {
    const res = await fetch(`${API}/api/bot/stop`, { method: 'POST' });
    const data = await res.json();
    toast(data.ok ? '⏹ ' + data.message : '❌ ' + data.message, data.ok ? 'info' : 'error');
  } catch (e) {
    toast('❌ Could not reach API server.', 'error');
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<span class="btn-icon">⏹</span> Stop Bot';
    pollStatus();
  }
}

// ══════════════════════════════════════
// Log Streaming (SSE)
// ══════════════════════════════════════
function startLogStream() {
  if (logSSE) return; // already streaming

  logSSE = new EventSource(`${API}/api/logs/stream`);

  logSSE.onmessage = (e) => {
    const line = JSON.parse(e.data);
    appendLog(document.getElementById('log-terminal'), line);
    appendLog(document.getElementById('log-preview'), line, true);
    displayedLogs++;
  };

  logSSE.onerror = () => {
    // SSE dropped — fallback to polling
    logSSE.close();
    logSSE = null;
    setTimeout(startLogStream, 3000);
  };
}

function appendLog(container, line, preview = false) {
  if (!container) return;

  // Remove empty placeholder
  const empty = container.querySelector('.log-empty');
  if (empty) empty.remove();

  const el = document.createElement('div');
  el.className = 'log-line ' + classifyLog(line);
  el.textContent = line;
  container.appendChild(el);

  // Auto-scroll
  if (!preview) {
    const cb = document.getElementById('log-autoscroll');
    if (!cb || cb.checked) container.scrollTop = container.scrollHeight;
  } else {
    container.scrollTop = container.scrollHeight;
  }

  // Trim preview to last 30 lines
  if (preview) {
    while (container.children.length > 30) container.removeChild(container.firstChild);
  }
}

function classifyLog(line) {
  if (!line) return 'debug';
  const l = line.toLowerCase();
  if (l.includes('error') || l.includes('❌') || l.includes('💥')) return 'error';
  if (l.includes('warn') || l.includes('⚠')) return 'warn';
  if (l.includes('✅') || l.includes('success') || l.includes('complete')) return 'success';
  if (l.includes('info') || l.includes('🔄') || l.includes('📊')) return 'info';
  return 'debug';
}

function clearLogDisplay() {
  const t = document.getElementById('log-terminal');
  t.innerHTML = '<div class="log-empty">Display cleared. New logs will appear here.</div>';
}

// ══════════════════════════════════════
// Projects DB
// ══════════════════════════════════════
async function loadProjects() {
  const tbody = document.getElementById('projects-tbody');
  tbody.innerHTML = '<tr><td colspan="6" class="table-empty">Loading...</td></tr>';

  try {
    const res = await fetch(`${API}/api/db/recent`);
    const data = await res.json();
    const projects = data.projects || [];

    if (!projects.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="table-empty">No projects in database yet.</td></tr>';
      return;
    }

    tbody.innerHTML = projects.map((p, i) => {
      const platform = detectPlatform(p.url);
      const date = formatDate(p.created_at);
      return `
      <tr>
        <td style="color:var(--text-muted);font-size:11px">${i + 1}</td>
        <td title="${escHtml(p.title)}">${escHtml(p.title)}</td>
        <td><span class="platform-chip chip-${platform}">${platform}</span></td>
        <td>${p.hiring_rate}%</td>
        <td style="color:var(--text-muted);font-size:12px">${date}</td>
        <td><a class="table-link" href="${escHtml(p.url)}" target="_blank">Open ↗</a></td>
      </tr>`;
    }).join('');
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="6" class="table-empty">Failed to load: ${e.message}</td></tr>`;
  }
}

async function clearDB() {
  if (!confirm('Clear all database entries? The bot will re-evaluate all projects on next run.')) return;
  try {
    const res = await fetch(`${API}/api/db/clear`, { method: 'POST' });
    const data = await res.json();
    toast(data.ok ? '🗑 ' + data.message : '❌ ' + data.message, data.ok ? 'success' : 'error');
    if (data.ok) loadProjects();
  } catch (e) {
    toast('❌ ' + e.message, 'error');
  }
}

// ══════════════════════════════════════
// Settings
// ══════════════════════════════════════
async function loadSettings() {
  try {
    const res = await fetch(`${API}/api/config`);
    const data = await res.json();

    const s = data.settings || {};
    const c = data.credentials || {};
    const d = data.display || {};

    // Populate settings form
    setVal('inp-criteria', s.AI_CRITERIA || '');
    setVal('inp-interval', s.INTERVAL_MINUTES || 5);
    setVal('inp-hiring', s.MIN_HIRING_RATE || 1);
    setVal('inp-grace', s.NEW_CLIENT_DAYS || 5);
    setVal('inp-maxprojects', s.MAX_PROJECTS_PER_RUN || 30);
    setVal('inp-model', s.GEMINI_MODEL || 'gemini-2.5-flash');

    // Credentials display
    setVal('inp-gemini', d.GEMINI_API_KEY || '');
    setVal('inp-telegram', d.TELEGRAM_BOT_TOKEN || '');
    setVal('inp-chatid', d.TELEGRAM_CHAT_ID || '');

    // Cred badges in dashboard
    setBadge('cred-gemini', c.GEMINI_API_KEY);
    setBadge('cred-telegram', c.TELEGRAM_BOT_TOKEN);
    setBadge('cred-chat', c.TELEGRAM_CHAT_ID);

    // Info panel
    setText('info-interval', (s.INTERVAL_MINUTES || '5') + ' min');
    setText('info-model', s.GEMINI_MODEL || '—');
    setText('info-hiring', (s.MIN_HIRING_RATE || '0') + '%');

  } catch (e) {
    toast('⚠️ Could not load settings: ' + e.message, 'error');
  }
}

async function saveCredentials() {
  const payload = {
    GEMINI_API_KEY:     document.getElementById('inp-gemini').value.trim(),
    TELEGRAM_BOT_TOKEN: document.getElementById('inp-telegram').value.trim(),
    TELEGRAM_CHAT_ID:   document.getElementById('inp-chatid').value.trim(),
  };
  try {
    const res = await fetch(`${API}/api/config/credentials`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    toast(data.ok ? '✅ ' + data.message : '❌ ' + data.message, data.ok ? 'success' : 'error');
    if (data.ok) loadSettings();
  } catch (e) {
    toast('❌ ' + e.message, 'error');
  }
}

async function saveSettings() {
  const payload = {
    AI_CRITERIA:         document.getElementById('inp-criteria').value.trim(),
    INTERVAL_MINUTES:    parseInt(document.getElementById('inp-interval').value),
    MIN_HIRING_RATE:     parseInt(document.getElementById('inp-hiring').value),
    NEW_CLIENT_DAYS:     parseInt(document.getElementById('inp-grace').value),
    MAX_PROJECTS_PER_RUN: parseInt(document.getElementById('inp-maxprojects').value),
    GEMINI_MODEL:        document.getElementById('inp-model').value,
  };
  try {
    const res = await fetch(`${API}/api/config/settings`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    toast(data.ok ? '✅ ' + data.message : '❌ ' + data.message, data.ok ? 'success' : 'error');
    if (data.ok) loadSettings();
  } catch (e) {
    toast('❌ ' + e.message, 'error');
  }
}

// ══════════════════════════════════════
// Helpers
// ══════════════════════════════════════
function detectPlatform(url) {
  if (!url) return 'other';
  if (url.includes('mostaql')) return 'mostaql';
  if (url.includes('nafezly')) return 'nafezly';
  if (url.includes('peopleperhour')) return 'pph';
  if (url.includes('guru')) return 'guru';
  return 'other';
}

function formatDate(dt) {
  if (!dt) return '—';
  try {
    const d = new Date(dt);
    return d.toLocaleString('en-GB', { dateStyle: 'short', timeStyle: 'short' });
  } catch { return dt; }
}

function setBadge(id, isSet) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = isSet ? '✓ Set' : '✗ Missing';
  el.className = 'cred-badge ' + (isSet ? 'set' : 'missing');
}

function setVal(id, val) {
  const el = document.getElementById(id);
  if (el) el.value = val;
}

function escHtml(str) {
  return String(str || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function toast(msg, type = 'info') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = `toast show ${type}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.classList.remove('show'); }, 3500);
}

// ══════════════════════════════════════
// Boot
// ══════════════════════════════════════
(async function init() {
  await pollStatus();
  await loadSettings();
  startLogStream();
  // Poll status every 5 seconds
  pollTimer = setInterval(pollStatus, 5000);
})();
