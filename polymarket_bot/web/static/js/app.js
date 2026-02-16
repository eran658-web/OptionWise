/* OptionWise Dashboard — Frontend Logic */

const App = {
  ws: null,
  currentTab: 'dashboard',
  logFilter: 'ALL',

  // ── Initialisation ──────────────────────────────────────────────
  init() {
    this.setupTabs();
    this.connectWebSocket();
    this.loadStrategies();
    this.loadConfig();
    this.loadTrades();
  },

  // ── Tab navigation ──────────────────────────────────────────────
  setupTabs() {
    document.querySelectorAll('.tab').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
        btn.classList.add('active');
        const tabId = btn.dataset.tab;
        document.getElementById('tab-' + tabId).classList.add('active');
        this.currentTab = tabId;

        // Refresh data when switching tabs
        if (tabId === 'strategies') this.loadStrategies();
        if (tabId === 'trades') this.loadTrades();
        if (tabId === 'config') this.loadConfig();
      });
    });
  },

  // ── WebSocket ───────────────────────────────────────────────────
  connectWebSocket() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    this.ws = new WebSocket(`${proto}//${location.host}/ws`);

    this.ws.onmessage = (event) => {
      const msg = JSON.parse(event.data);
      if (msg.type === 'dashboard') this.renderDashboard(msg.data);
      if (msg.type === 'positions') this.renderPositions(msg.data);
      if (msg.type === 'logs') this.appendLogs(msg.data);
    };

    this.ws.onclose = () => {
      setTimeout(() => this.connectWebSocket(), 3000);
    };

    this.ws.onerror = () => {
      this.ws.close();
    };
  },

  // ── Dashboard rendering ─────────────────────────────────────────
  renderDashboard(d) {
    // Status badges
    const statusBadge = document.getElementById('status-badge');
    if (d.running) {
      statusBadge.textContent = 'Running';
      statusBadge.className = 'badge badge-success';
      document.getElementById('btn-start').style.display = 'none';
      document.getElementById('btn-stop').style.display = '';
    } else {
      statusBadge.textContent = 'Stopped';
      statusBadge.className = 'badge badge-stopped';
      document.getElementById('btn-start').style.display = '';
      document.getElementById('btn-stop').style.display = 'none';
    }

    const dryBadge = document.getElementById('dry-run-badge');
    dryBadge.style.display = d.dry_run ? '' : 'none';

    // Header stats
    document.getElementById('uptime').textContent = 'Uptime: ' + this.formatTime(d.uptime_sec || 0);
    document.getElementById('cycles').textContent = 'Cycles: ' + (d.cycle_count || 0);

    // Metric cards
    this.setCardValue('total-pnl', d.total_pnl_usd);
    this.setCardValue('unrealized-pnl', d.unrealized_pnl_usd);
    this.setCardValue('daily-pnl', d.daily_pnl_usd);
    document.getElementById('exposure').textContent = this.formatUsd(d.total_exposure_usd || 0);
    document.getElementById('drawdown').textContent = (d.drawdown_pct || 0).toFixed(1) + '%';
    document.getElementById('drawdown').className = 'card-value' + (d.drawdown_pct > 5 ? ' negative' : '');
    document.getElementById('position-count').textContent = d.positions || 0;
    document.getElementById('daily-trades').textContent = d.daily_trades || 0;
    document.getElementById('markets-count').textContent = d.markets_count || 0;

    // Error banner
    const banner = document.getElementById('error-banner');
    if (d.error) {
      banner.textContent = 'Bot error: ' + d.error;
      banner.style.display = '';
    } else {
      banner.style.display = 'none';
    }

    // Risk status
    const riskBadge = document.getElementById('risk-paused');
    if (d.paused) {
      riskBadge.textContent = 'Paused: ' + (d.pause_reason || '');
      riskBadge.className = 'badge badge-danger';
    } else {
      riskBadge.textContent = 'Active';
      riskBadge.className = 'badge badge-success';
    }
  },

  setCardValue(id, value) {
    const el = document.getElementById(id);
    const v = value || 0;
    el.textContent = this.formatUsd(v);
    el.className = 'card-value' + (v > 0 ? ' positive' : v < 0 ? ' negative' : '');
  },

  // ── Positions rendering ─────────────────────────────────────────
  renderPositions(positions) {
    const tbody = document.getElementById('positions-body');
    if (!positions || positions.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" class="empty">No open positions</td></tr>';

      // Update strategy pills on dashboard
      this.updateStrategyPills([]);
      return;
    }

    tbody.innerHTML = positions.map(p => `
      <tr>
        <td>${p.token_id.substring(0, 12)}...</td>
        <td class="side-${p.side.toLowerCase()}">${p.side}</td>
        <td>${p.entry_price.toFixed(4)}</td>
        <td>${p.current_price.toFixed(4)}</td>
        <td>${this.formatUsd(p.size_usd)}</td>
        <td class="${p.pnl >= 0 ? 'pnl-pos' : 'pnl-neg'}">${this.formatUsd(p.pnl)}</td>
        <td>${p.strategy}</td>
      </tr>
    `).join('');
  },

  updateStrategyPills(strategies) {
    const container = document.getElementById('strategy-status');
    if (!strategies || strategies.length === 0) {
      // Don't clear if we have no data — leave existing pills
      return;
    }
    container.innerHTML = strategies.map(s => `
      <div class="strategy-pill">
        <span class="dot ${s.active || s.enabled ? 'dot-active' : 'dot-inactive'}"></span>
        ${s.name.replace('_', ' ')}
      </div>
    `).join('');
  },

  // ── Strategies ──────────────────────────────────────────────────
  async loadStrategies() {
    try {
      const data = await this.apiFetch('/api/strategies');
      this.renderStrategiesTab(data);
      this.updateStrategyPills(data);
    } catch (e) {
      // Ignore
    }
  },

  renderStrategiesTab(strategies) {
    const container = document.getElementById('strategies-container');
    if (!strategies || strategies.length === 0) {
      container.innerHTML = '<p class="empty">No strategies configured</p>';
      return;
    }

    container.innerHTML = strategies.map(s => {
      const paramRows = Object.entries(s.params || {}).map(([key, val]) =>
        `<div class="form-row">
          <label class="form-label">${this.formatParamName(key)}</label>
          <input class="form-input" type="number" step="any" name="${key}" value="${val}">
        </div>`
      ).join('');

      return `
        <div class="strategy-card">
          <div class="strategy-card-header">
            <h4>${s.name.replace('_', ' ')}</h4>
            <label class="toggle">
              <input type="checkbox" ${s.enabled ? 'checked' : ''}
                     onchange="App.toggleStrategy('${s.name}')">
              <span class="toggle-slider"></span>
            </label>
          </div>
          <form onsubmit="App.saveStrategyParams('${s.name}', event)">
            ${paramRows}
            ${paramRows ? '<button type="submit" class="btn btn-primary" style="margin-top:12px;width:100%">Save Parameters</button>' : ''}
          </form>
        </div>
      `;
    }).join('');
  },

  async toggleStrategy(name) {
    try {
      const resp = await this.apiFetch('/api/strategies/' + name + '/toggle', { method: 'POST' });
      this.toast(name + ' ' + (resp.enabled ? 'enabled' : 'disabled'), 'success');
      this.loadStrategies();
    } catch (e) {
      this.toast('Failed to toggle strategy', 'error');
    }
  },

  async saveStrategyParams(name, event) {
    event.preventDefault();
    const form = event.target;
    const params = {};
    form.querySelectorAll('.form-input').forEach(input => {
      params[input.name] = parseFloat(input.value);
    });

    try {
      await this.apiFetch('/api/strategies/' + name + '/params', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(params),
      });
      this.toast('Parameters saved', 'success');
    } catch (e) {
      this.toast('Failed to save parameters', 'error');
    }
  },

  // ── Trade History ───────────────────────────────────────────────
  async loadTrades() {
    try {
      const trades = await this.apiFetch('/api/trades');
      this.renderTrades(trades);
    } catch (e) {
      // Ignore
    }
  },

  renderTrades(trades) {
    const tbody = document.getElementById('trades-body');
    if (!trades || trades.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" class="empty">No trades yet</td></tr>';
      return;
    }

    tbody.innerHTML = trades.map(t => `
      <tr>
        <td>${this.formatTimestamp(t.timestamp)}</td>
        <td>${t.strategy}</td>
        <td class="side-${t.side.toLowerCase()}">${t.side}</td>
        <td>${t.token_id.substring(0, 12)}...</td>
        <td>${t.price.toFixed(4)}</td>
        <td>${this.formatUsd(t.size_usd)}</td>
        <td>${t.edge_pct.toFixed(1)}%</td>
      </tr>
    `).join('');
  },

  // ── Configuration ───────────────────────────────────────────────
  async loadConfig() {
    try {
      const config = await this.apiFetch('/api/config');
      this.renderConfigSection('risk', config.risk || {});
      this.renderConfigSection('bot', config.bot || {});
    } catch (e) {
      // Ignore
    }
  },

  renderConfigSection(section, data) {
    const container = document.getElementById('config-' + section + '-fields');
    if (!container) return;

    container.innerHTML = Object.entries(data).map(([key, val]) => {
      const type = typeof val === 'boolean' ? 'checkbox' :
                   typeof val === 'number' ? 'number' : 'text';

      if (type === 'checkbox') {
        return `
          <div class="form-row">
            <label class="form-label">${this.formatParamName(key)}</label>
            <label class="toggle">
              <input type="checkbox" name="${key}" ${val ? 'checked' : ''}>
              <span class="toggle-slider"></span>
            </label>
          </div>
        `;
      }

      if (key === 'log_level') {
        return `
          <div class="form-row">
            <label class="form-label">${this.formatParamName(key)}</label>
            <select class="form-select" name="${key}">
              ${['DEBUG','INFO','WARNING','ERROR'].map(l =>
                `<option value="${l}" ${val === l ? 'selected' : ''}>${l}</option>`
              ).join('')}
            </select>
          </div>
        `;
      }

      return `
        <div class="form-row">
          <label class="form-label">${this.formatParamName(key)}</label>
          <input class="form-input" type="${type}" step="any" name="${key}" value="${val}">
        </div>
      `;
    }).join('');
  },

  async saveConfig(section, event) {
    event.preventDefault();
    const form = event.target;
    const data = {};
    const params = {};

    form.querySelectorAll('.form-input').forEach(input => {
      params[input.name] = parseFloat(input.value);
    });
    form.querySelectorAll('.form-select').forEach(select => {
      params[select.name] = select.value;
    });
    form.querySelectorAll('.toggle input[type="checkbox"]').forEach(cb => {
      params[cb.name] = cb.checked;
    });

    data[section] = params;

    try {
      await this.apiFetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
      });
      this.toast('Configuration saved', 'success');
    } catch (e) {
      this.toast('Failed to save configuration', 'error');
    }
  },

  // ── Logs ────────────────────────────────────────────────────────
  appendLogs(entries) {
    const container = document.getElementById('log-container');
    entries.forEach(entry => {
      const div = document.createElement('div');
      div.className = 'log-entry';
      div.dataset.level = entry.level;
      div.innerHTML =
        `<span class="log-time">${entry.timestamp}</span> ` +
        `<span class="log-level-${entry.level}">[${entry.level}]</span> ` +
        `<span>${this.escapeHtml(entry.name)}: ${this.escapeHtml(entry.message)}</span>`;

      if (this.logFilter !== 'ALL' && entry.level !== this.logFilter) {
        div.style.display = 'none';
      }
      container.appendChild(div);
    });

    // Trim old entries
    while (container.children.length > 1000) {
      container.removeChild(container.firstChild);
    }

    // Auto-scroll
    if (document.getElementById('log-autoscroll').checked) {
      container.scrollTop = container.scrollHeight;
    }
  },

  filterLogs(level, btn) {
    this.logFilter = level;
    document.querySelectorAll('.log-filters .btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');

    document.querySelectorAll('#log-container .log-entry').forEach(el => {
      if (level === 'ALL' || el.dataset.level === level) {
        el.style.display = '';
      } else {
        el.style.display = 'none';
      }
    });
  },

  clearLogs() {
    document.getElementById('log-container').innerHTML = '';
  },

  // ── Bot controls ────────────────────────────────────────────────
  async startBot() {
    try {
      await this.apiFetch('/api/bot/start', { method: 'POST' });
      this.toast('Bot starting...', 'success');
    } catch (e) {
      this.toast('Failed to start bot', 'error');
    }
  },

  async stopBot() {
    try {
      await this.apiFetch('/api/bot/stop', { method: 'POST' });
      this.toast('Bot stopping...', 'success');
    } catch (e) {
      this.toast('Failed to stop bot', 'error');
    }
  },

  async toggleDryRun() {
    try {
      const resp = await this.apiFetch('/api/bot/toggle-dry-run', { method: 'POST' });
      this.toast('Dry run: ' + (resp.dry_run ? 'ON' : 'OFF'), 'success');
    } catch (e) {
      this.toast('Failed to toggle dry run', 'error');
    }
  },

  // ── Helpers ─────────────────────────────────────────────────────
  async apiFetch(url, opts) {
    const resp = await fetch(url, opts);
    return resp.json();
  },

  formatUsd(amount) {
    if (amount == null) return '$0.00';
    const prefix = amount < 0 ? '-$' : '$';
    return prefix + Math.abs(amount).toFixed(2);
  },

  formatTime(seconds) {
    if (!seconds || seconds <= 0) return '--';
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    if (h > 0) return `${h}h ${m}m`;
    if (m > 0) return `${m}m ${s}s`;
    return `${s}s`;
  },

  formatTimestamp(ts) {
    if (!ts) return '--';
    const d = new Date(ts * 1000);
    return d.toLocaleTimeString();
  },

  formatParamName(key) {
    return key.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
  },

  escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  },

  toast(message, type) {
    const existing = document.querySelector('.toast');
    if (existing) existing.remove();

    const div = document.createElement('div');
    div.className = 'toast toast-' + (type || 'success');
    div.textContent = message;
    document.body.appendChild(div);
    setTimeout(() => div.remove(), 3000);
  },
};

document.addEventListener('DOMContentLoaded', () => App.init());
