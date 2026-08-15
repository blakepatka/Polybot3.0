/* Polybot 3.0 dashboard.
 *
 * One SSE stream carries the whole application state once a second; every
 * render function is a pure projection of that snapshot. Controls POST and
 * then let the next snapshot confirm the change rather than mutating local
 * state optimistically — with real money involved, the UI should show what the
 * engine actually did, never what we hoped it would do.
 */

'use strict';

const $  = (id) => document.getElementById(id);
const el = (tag, cls, txt) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (txt !== undefined) n.textContent = txt;
  return n;
};

const state = {
  snap: null,
  range: 'all',
  // Which strategy the dashboard is describing. 'all' blends every strategy;
  // any other value scopes the entire dashboard — headline P/L, curve, wins &
  // losses, by-asset and open positions — to that strategy alone.
  strategy: 'all',
  // Last mode we rendered. Switching Paper/Live re-scopes every pane rather
  // than letting the independently-polled ones lag a few seconds behind the
  // SSE-driven ones and briefly show two modes' numbers side by side.
  lastMode: null,
  tab: 'overview',
  sub: 'positions',
  assetFilter: '',
  es: null,
  sliderHeld: false,
  limitsEditing: false,
  geom: null,        // last chart projection, for crosshair hit-testing
  hoverIndex: null,  // settlement the pointer is currently over
  wlFilter: 'all',   // wins & losses table filter
  wlRange: 'all',    // independent range for the wins & losses pane
  signalsCollapsed: false,
  vaultHydrated: false,
  liveBalance: null,
  balanceFetchedAt: 0,
  balanceInFlight: false,
};

/* ── formatting ─────────────────────────────────────────────────────────── */

const money = (v, dp = 2) => {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  const sign = v < 0 ? '−' : '';
  return `${sign}$${Math.abs(v).toLocaleString('en-US', {
    minimumFractionDigits: dp, maximumFractionDigits: dp })}`;
};
const signedMoney = (v, dp = 2) => {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return (v >= 0 ? '+' : '−') + '$' + Math.abs(v).toFixed(dp);
};
const pct  = (v, dp = 1) => (v === null || v === undefined || Number.isNaN(v))
  ? '—' : `${(v * 100).toFixed(dp)}%`;
const spct = (v, dp = 1) => (v === null || v === undefined || Number.isNaN(v))
  ? '—' : `${v >= 0 ? '+' : ''}${(v * 100).toFixed(dp)}%`;
const cls  = (v) => v > 0 ? 'pos' : v < 0 ? 'neg' : '';

const price = (v, dp) => (v === null || v === undefined)
  ? '—' : v.toLocaleString('en-US', { minimumFractionDigits: dp, maximumFractionDigits: dp });

const ago = (s) => {
  if (s === null || s === undefined) return '—';
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
};

/* ── time zone ──────────────────────────────────────────────────────────────
 * Every timestamp in the dashboard is rendered in US Central. The IANA zone is
 * used rather than a fixed -06:00 offset so the switch between CST and CDT is
 * handled automatically — a hard offset would silently be an hour wrong for
 * most of the year. Change TZ here to re-base the whole UI.
 */
const TZ = 'America/Chicago';
const TZ_LABEL = 'CT';

/** Short zone abbreviation for right now — "CST" or "CDT". */
function tzAbbrev(date = new Date()) {
  try {
    const part = new Intl.DateTimeFormat('en-US', { timeZone: TZ, timeZoneName: 'short' })
      .formatToParts(date).find((p) => p.type === 'timeZoneName');
    return part ? part.value : TZ_LABEL;
  } catch {
    return TZ_LABEL;
  }
}

const clock = (ts) =>
  new Date(ts * 1000).toLocaleTimeString('en-US', { hour12: true, timeZone: TZ });

const chartDateTime = (ts) =>
  new Date(ts * 1000).toLocaleString('en-US', {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    second: '2-digit',
    hour12: true,
    timeZone: TZ,
  });

/** "BTC Up or Down · Jul 27, 3:10 PM–3:15 PM CT" from a window's bounds. */
function marketName(asset, window, start, end) {
  const opts = { hour: 'numeric', minute: '2-digit', timeZone: TZ };
  const a = new Date(start * 1000).toLocaleTimeString('en-US', opts);
  const b = new Date(end * 1000).toLocaleTimeString('en-US', opts);
  const day = new Date(start * 1000)
    .toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: TZ });
  return `${asset.toUpperCase()} Up or Down · ${day}, ${a}–${b} ${TZ_LABEL}`;
}

const ASSET_COLORS = {
  btc:  '#f7931a', eth: '#8b93f8', sol: '#14f195', xrp: '#4b9dfa',
  bnb:  '#f3ba2f', doge: '#c3a634', hype: '#2ee6a8',
};

const ASSET_ICONS = {
  btc: '/static/assets/crypto/btc.png',
  xrp: '/static/assets/crypto/xrp.png',
  eth: '/static/assets/crypto/eth.png',
  doge: '/static/assets/crypto/doge.png',
  sol: '/static/assets/crypto/sol.png',
  bnb: '/static/assets/crypto/bnb.png',
};

function toast(msg, kind = 'ok') {
  const t = el('div', `toast ${kind === 'ok' ? '' : kind}`, msg);
  $('toasts').appendChild(t);
  setTimeout(() => t.remove(), kind === 'err' ? 9000 : 4500);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  let body = null;
  try { body = await res.json(); } catch { /* empty body is fine */ }
  if (!res.ok) throw new Error((body && body.detail) || `${res.status} ${res.statusText}`);
  return body;
}

async function refreshLiveBalance(force = false) {
  const snap = state.snap;
  if (!snap || snap.runtime.mode !== 'live') {
    state.liveBalance = null;
    state.balanceFetchedAt = 0;
    renderWalletBalance(snap);
    return null;
  }

  const now = Date.now();
  if (state.balanceInFlight || (!force && now - state.balanceFetchedAt < 15000)) {
    return state.liveBalance;
  }

  state.balanceInFlight = true;
  try {
    state.liveBalance = await api('/api/balance');
  } catch (err) {
    state.liveBalance = { ok: false, error: err.message || 'Balance unavailable' };
  } finally {
    state.balanceFetchedAt = Date.now();
    state.balanceInFlight = false;
    renderWalletBalance(state.snap);
  }
  return state.liveBalance;
}

/* ── SSE ────────────────────────────────────────────────────────────────── */

/** Query suffix shared by the stream and every independently polled pane, so
 *  no two panes can ever describe different scopes. */
function scopeQuery() {
  return `strategy=${encodeURIComponent(state.strategy)}`;
}

function connect() {
  if (state.es) state.es.close();
  const es = new EventSource(`/api/stream?range=${state.range}&${scopeQuery()}`);
  state.es = es;

  es.onmessage = (ev) => {
    try {
      state.snap = JSON.parse(ev.data);
      // The engine's mode can change from another tab, or from the mode
      // control here. Either way the independently polled panes are still
      // showing the previous mode until they are told otherwise.
      if (state.lastMode !== null && state.snap.runtime.mode !== state.lastMode) {
        state.lastMode = state.snap.runtime.mode;
        refreshScopedPanes();
      } else {
        state.lastMode = state.snap.runtime.mode;
      }
      render(state.snap);
      void refreshLiveBalance();
      $('foot-conn').textContent = '● live';
      $('foot-conn').className = 'dot-on';
    } catch (err) {
      console.error('bad frame', err);
    }
  };
  es.onerror = () => {
    $('foot-conn').textContent = '● reconnecting';
    $('foot-conn').className = 'dot-off';
  };
}

/** Re-fetch every pane that polls independently of the SSE snapshot.
 *
 *  Called whenever the scope changes — execution mode or strategy — so the
 *  whole dashboard flips together instead of the P/L card updating instantly
 *  while the tables below it keep last scope's numbers until the next tick.
 */
function refreshScopedPanes() {
  state.liveBalance = null;
  state.balanceFetchedAt = 0;
  state.hoverIndex = null;
  refreshActivity();
  if (state.tab === 'monitor') refreshSignals();
  if (state.tab === 'winloss') refreshWinLoss();
}

/** Change what the dashboard is describing, then repoint every source at it. */
function rescope(patch) {
  Object.assign(state, patch);
  connect();            // the stream carries the scope in its query string
  refreshScopedPanes();
  if (state.snap) render(state.snap);
}

/* ── render ─────────────────────────────────────────────────────────────── */

function render(s) {
  renderControlBar(s);
  renderStrategySelector(s);
  renderPnl(s);
  renderProfile(s);
  renderInfoCards(s);
  renderStrategy(s);
  renderPrices(s);
  renderPositions(s);
  renderMonitor(s);
  renderControls(s);
  renderVaultState(s);
  renderChainlink(s);
  renderFooter(s);
}

function renderControlBar(s) {
  const rt = s.runtime;
  const btn = $('btn-power');
  const running = rt.running;

  $('power-text').textContent = running ? 'Stop bot' : 'Start bot';
  btn.className = `btn ${running ? 'btn-stop' : 'btn-start'}`;
  btn.querySelector('.glyph').textContent = running ? '■' : '▶';

  const status = $('engine-status');
  if (!running)          { status.textContent = 'Engine offline'; status.className = 'engine-status'; }
  else if (rt.paused)    { status.textContent = 'Paused — settling only'; status.className = 'engine-status'; }
  else                   { status.textContent = `Running · ${rt.mode}`; status.className = 'engine-status on'; }

  document.querySelectorAll('#mode-seg .seg-btn').forEach((b) => {
    b.classList.toggle('active', b.dataset.mode === rt.mode);
    b.classList.toggle('live-armed', b.dataset.mode === 'live');
  });

  // Do not fight the user's finger while a slider is being dragged.
  if (!state.sliderHeld) {
    $('sl-trade').value  = rt.max_per_trade_usd;
    $('sl-trade2').value = rt.max_per_trade_usd;
    $('sl-window').value = rt.max_per_window_usd;
    $('sl-daily').value  = rt.daily_cap_usd;
    $('val-trade').textContent  = money(rt.max_per_trade_usd);
    $('val-window').textContent = money(rt.max_per_window_usd);
    $('big-trade').textContent  = money(rt.max_per_trade_usd);
    $('big-daily').textContent  = money(rt.daily_cap_usd);
  }

  if (!state.limitsEditing) {
    const bank = s.bankroll || s.config.bankroll;
    const entry = { ...s.config.limits, ...s.config.strategy_config };
    $('lim-bankroll').value = bank.starting_balance_usd;
    $('lim-trade').value = rt.max_per_trade_usd;
    $('lim-market-window').value = entry.hard_market_window_cap_usd ?? entry.hard_window_cap_usd;
    $('lim-window').value = entry.hard_window_cap_usd;
    $('lim-open').value = bank.max_open_exposure_usd;
    $('lim-reserve').value = bank.cash_reserve_usd;
    $('lim-loss').value = bank.max_daily_loss_usd;
    $('lim-daily').value = bank.max_daily_turnover_usd;
    $('lim-confidence').value = Math.round(entry.min_confidence * 100);
    $('lim-price').value = Math.round(entry.max_entry_price * 100);
    $('lim-timing').value = Math.round(entry.max_entry_window_fraction * 100);
  }
}

const STRATEGY_LABEL = {
  all: 'All', antsaslyku: '@antsaslyku',
};
const strategyLabel = (name) =>
  STRATEGY_LABEL[name] || (name.charAt(0).toUpperCase() + name.slice(1));

/** The strategy segment.
 *
 *  It means two different things and must say so. In Paper every configured
 *  strategy trades at once, so the segment is a *filter* over which one the
 *  dashboard describes. In Live only one strategy may trade, so choosing here
 *  is a *switch* that changes what the engine does with real money — and it
 *  therefore asks first.
 */
function renderStrategySelector(s) {
  const sel = s.strategy_selection || {};
  const exclusive = !!sel.exclusive;
  const active = new Set(sel.active || []);
  const seen = new Map((s.strategies_seen || []).map((r) => [r.strategy, r]));

  // Offer everything configured to run plus anything with history, so a
  // strategy that has been switched off can still be reviewed.
  const names = [...new Set([...(sel.configured || []), ...seen.keys()])].sort();

  const seg = $('strategy-seg');
  const wanted = ['all', ...names].join('|');
  if (seg.dataset.built !== wanted || seg.dataset.exclusive !== String(exclusive)) {
    seg.dataset.built = wanted;
    seg.dataset.exclusive = String(exclusive);
    seg.replaceChildren();
    for (const name of ['all', ...names]) {
      const b = el('button', 'seg-btn');
      b.dataset.strategy = name;
      if (name !== 'all') {
        const dot = el('span', 'live-dot');
        b.appendChild(dot);
      }
      b.appendChild(el('span', '', strategyLabel(name)));
      const row = seen.get(name);
      if (name !== 'all' && row) {
        b.appendChild(el('span', 'n', String(row.settlements)));
      }
      seg.appendChild(b);
    }
  }

  seg.querySelectorAll('.seg-btn').forEach((b) => {
    const name = b.dataset.strategy;
    b.classList.toggle('active', name === state.strategy);
    b.classList.toggle('exclusive', exclusive && name !== 'all');
    b.classList.toggle('is-idle', name !== 'all' && !active.has(name));
    const row = seen.get(name);
    const n = b.querySelector('.n');
    if (n && row) n.textContent = String(row.settlements);
    b.title = name === 'all'
      ? 'Combined P/L across every strategy'
      : `${strategyLabel(name)} — ${active.has(name) ? 'trading now' : 'not trading'}` +
        (row ? ` · ${row.settlements} settled, ${row.open_positions} open` : '');
  });

  $('strategy-hint').textContent = exclusive
    ? '· live: one at a time'
    : '· paper: all trading, filter view';
}

const RANGE_LABEL = {
  '1h': 'last hour', '5h': 'last 5 hours', '12h': 'last 12 hours',
  '24h': 'last 24 hours', '7d': 'last 7 days', all: 'all settled windows',
};

function renderPnl(s) {
  // Everything in this card describes the selected range, so the headline
  // figure and the curve beneath it always cover the same settlements.
  const st = s.range_stats || s.stats;
  const total = st.total_pnl;
  const node = $('pnl-total');
  node.textContent = (total >= 0 ? '' : '−') + '$' + Math.abs(total).toFixed(2);
  node.className = `pnl-big ${cls(total)}`;
  $('pnl-arrow').textContent = total >= 0 ? '▲' : '▼';
  $('pnl-arrow').className = `arrow ${total >= 0 ? 'up' : 'down'}`;

  let sub = `Local settlement estimate — not wallet cash · ` +
            `${st.settlements} settlement${st.settlements === 1 ? '' : 's'} · ` +
            `${RANGE_LABEL[s.pnl_range] || s.pnl_range}`;
  // When a narrower range holds everything there is, say so — otherwise the
  // buttons look broken on a young database.
  if (s.pnl_range !== 'all' && st.settlements === s.stats.settlements && st.settlements > 0) {
    sub += ' · covers all history so far';
  }
  if (st.first_at && st.settlements > 1) {
    sub += ` · from ${chartDateTime(st.first_at)} ${TZ_LABEL}`;
  }
  $('pnl-sub').textContent = sub;
  renderWalletBalance(s);

  $('m-winrate').textContent = st.settlements ? pct(st.win_rate) : '—';
  $('m-winrate').className = 'tile-val';
  $('m-roi').textContent = st.staked ? spct(st.roi) : '—';
  $('m-roi').className = `tile-val ${cls(st.roi)}`;
  $('m-best').textContent = st.biggest_win ? money(st.biggest_win) : '—';
  $('m-open').textContent = st.open_positions;

  renderHourlyAverage(s);
  drawChart(s.equity);
}

function renderWalletBalance(s) {
  const value = $('pnl-wallet');
  const note = $('pnl-wallet-note');
  if (!value || !note || !s) return;

  if (s.runtime.mode !== 'live') {
    $('pnl-wallet-label').textContent = 'Polymarket account equity';
    value.textContent = 'Paper mode';
    value.className = 'wallet-value';
    note.textContent = 'No real wallet collateral is used in Paper mode.';
    return;
  }

  const balance = state.liveBalance;
  if (!balance) {
    $('pnl-wallet-label').textContent = 'Polymarket account equity';
    value.textContent = 'Checking…';
    value.className = 'wallet-value';
    note.textContent = 'Reading spendable pUSD directly from the live account.';
    return;
  }
  if (!balance.ok) {
    $('pnl-wallet-label').textContent = 'Polymarket account equity';
    value.textContent = 'Unavailable';
    value.className = 'wallet-value bad';
    note.textContent = balance.error || 'Could not read the live collateral balance.';
    return;
  }

  const hasEquity = balance.account_equity_pusd !== null &&
    balance.account_equity_pusd !== undefined;
  $('pnl-wallet-label').textContent = hasEquity
    ? 'Polymarket account equity'
    : 'Spendable Polymarket balance';
  value.textContent = hasEquity
    ? money(balance.account_equity_pusd)
    : `${money(balance.balance_pusd)} pUSD`;
  value.className = 'wallet-value';
  note.textContent = hasEquity
    ? `${money(balance.balance_pusd)} spendable pUSD + ` +
      `${money(balance.positions_value_pusd)} open positions · venue data checked ` +
      `${ago((Date.now() - state.balanceFetchedAt) / 1000)}.`
    : `Spendable collateral checked ${ago((Date.now() - state.balanceFetchedAt) / 1000)} · ` +
      'position value unavailable; this is not derived from the local P/L ledger.';
}

function renderHourlyAverage(s) {
  const stats = s.stats;
  const value = $('m-hourly');
  const detail = $('m-hourly-detail');
  const elapsedSeconds = stats.first_at ? Math.max(0, s.ts - stats.first_at) : 0;

  if (stats.settlements < 2 || elapsedSeconds < 60) {
    value.textContent = '—';
    value.className = 'hourly-value';
    detail.textContent = stats.settlements
      ? 'More elapsed history is needed before the hourly average is meaningful.'
      : 'Waiting for the first settled window.';
    return;
  }

  const elapsedHours = elapsedSeconds / 3600;
  const hourly = stats.total_pnl / elapsedHours;
  const wholeHours = Math.floor(elapsedHours);
  const minutes = Math.floor((elapsedHours - wholeHours) * 60);
  const duration = wholeHours
    ? `${wholeHours}h ${String(minutes).padStart(2, '0')}m`
    : `${minutes}m`;

  value.textContent = `${hourly >= 0 ? '+' : '−'}$${Math.abs(hourly).toFixed(2)} / hr`;
  value.className = `hourly-value ${cls(hourly)}`;
  detail.textContent =
    `${money(stats.total_pnl)} realized over ${duration} since the first ` +
    `${s.runtime.mode} settlement · ${stats.settlements} settlements total.`;
}

function drawChart(points) {
  const svg = $('chart');
  const empty = $('chart-empty');
  svg.innerHTML = '';

  if (!points || points.length < 2) {
    empty.classList.remove('hidden');
    state.geom = null;
    $('chart-wrap').classList.remove('readable');
    hideCrosshair();
    return;
  }
  empty.classList.add('hidden');
  $('chart-wrap').classList.add('readable');

  const W = svg.clientWidth || 800;
  const H = svg.clientHeight || 360;
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);

  const ys = points.map((p) => p.v);
  let y0 = Math.min(0, ...ys), y1 = Math.max(0, ...ys);
  if (y1 - y0 < 1e-9) { y1 += 1; y0 -= 1; }
  // Keep zero in view so the chart remains honest, while using nearly all of
  // the taller plot instead of flattening the curve behind oversized padding.
  const padY = (y1 - y0) * 0.055;
  y0 -= padY; y1 += padY;

  const plot = {
    left: Math.min(58, Math.max(42, W * 0.075)),
    right: W - 12,
    top: 12,
    bottom: H - 24,
  };

  // X is the settlement index, not wall-clock time. Windows across assets close
  // on the same boundary, so many settlements share a timestamp — a time axis
  // collapses those into a single vertical line. Even spacing per settlement is
  // also the honest reading of a trade-by-trade equity curve. The selected time
  // range still applies; it is filtered server-side before we get here.
  const n = points.length - 1;
  const X = (i) => (n === 0
    ? (plot.left + plot.right) / 2
    : plot.left + (i / n) * (plot.right - plot.left));
  const Y = (v) => plot.bottom -
    ((v - y0) / (y1 - y0)) * (plot.bottom - plot.top);

  // Kept so the crosshair can map a pointer position back to a settlement
  // without recomputing the projection on every mouse move.
  state.geom = { W, H, n, X, Y, points, ...plot };

  const NS = 'http://www.w3.org/2000/svg';
  const defs = document.createElementNS(NS, 'defs');
  defs.innerHTML =
    `<linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
       <stop offset="0%"   stop-color="#fae8b4" stop-opacity=".38"/>
       <stop offset="100%" stop-color="#cbbd93" stop-opacity="0"/>
     </linearGradient>
     <linearGradient id="l" x1="0" y1="0" x2="1" y2="0">
       <stop offset="0%"   stop-color="#fae8b4"/>
       <stop offset="100%" stop-color="#cbbd93"/>
     </linearGradient>`;
  svg.appendChild(defs);

  // Four horizontal bands make the vertical scale legible. Labels are kept
  // inside the SVG so they resize with the chart instead of drifting away.
  const axisMoney = (v) => {
    const abs = Math.abs(v);
    const compact = abs >= 1000
      ? `${(abs / 1000).toFixed(abs >= 10000 ? 0 : 1)}k`
      : abs.toFixed(abs >= 100 ? 0 : (abs >= 10 ? 1 : 2));
    return `${v < 0 ? '−' : ''}$${compact}`;
  };
  const grid = document.createElementNS(NS, 'g');
  for (let i = 0; i <= 4; i += 1) {
    const value = y0 + ((y1 - y0) * i / 4);
    const gy = Y(value);
    const line = document.createElementNS(NS, 'line');
    line.setAttribute('x1', plot.left);
    line.setAttribute('x2', plot.right);
    line.setAttribute('y1', gy);
    line.setAttribute('y2', gy);
    line.setAttribute('stroke', '#4a422b');
    line.setAttribute('stroke-width', '1');
    line.setAttribute('opacity', '.38');
    grid.appendChild(line);

    const label = document.createElementNS(NS, 'text');
    label.setAttribute('x', plot.left - 7);
    label.setAttribute('y', gy + 3.5);
    label.setAttribute('text-anchor', 'end');
    label.setAttribute('fill', '#8f866a');
    label.setAttribute('font-size', '9.5');
    label.setAttribute('font-family', 'ui-monospace, SFMono-Regular, Consolas, monospace');
    label.textContent = axisMoney(value);
    grid.appendChild(label);
  }
  svg.appendChild(grid);

  // Zero reference line — the only gridline that carries meaning here.
  if (y0 < 0 && y1 > 0) {
    const z = document.createElementNS(NS, 'line');
    z.setAttribute('x1', plot.left); z.setAttribute('x2', plot.right);
    z.setAttribute('y1', Y(0)); z.setAttribute('y2', Y(0));
    z.setAttribute('stroke', '#574a24');
    z.setAttribute('stroke-dasharray', '3 4');
    svg.appendChild(z);
  }

  const d = points.map((p, i) => `${i ? 'L' : 'M'}${X(i).toFixed(2)},${Y(p.v).toFixed(2)}`).join(' ');

  const area = document.createElementNS(NS, 'path');
  area.setAttribute('d', `${d} L${X(n).toFixed(2)},${Y(y0)} L${X(0).toFixed(2)},${Y(y0)} Z`);
  area.setAttribute('fill', 'url(#g)');
  svg.appendChild(area);

  const line = document.createElementNS(NS, 'path');
  line.setAttribute('d', d);
  line.setAttribute('fill', 'none');
  line.setAttribute('stroke', 'url(#l)');
  line.setAttribute('stroke-width', '1.8');
  line.setAttribute('stroke-linejoin', 'round');
  svg.appendChild(line);

  // Crosshair layer, appended last so it always draws over the curve. It is
  // rebuilt on every redraw, so re-apply whatever the pointer was last on —
  // otherwise the crosshair blinks out once a second as new state arrives.
  const g = document.createElementNS(NS, 'g');
  g.setAttribute('id', 'crosshair');
  g.style.display = 'none';
  g.innerHTML =
    `<line id="ch-v" y1="${plot.top}" y2="${plot.bottom}" stroke="#fae8b4" stroke-width="1"
           stroke-dasharray="3 3" opacity=".55"/>
     <line id="ch-h" x1="${plot.left}" x2="${plot.right}" stroke="#fae8b4" stroke-width="1"
           stroke-dasharray="3 3" opacity=".3"/>
     <circle id="ch-d" r="4" fill="#1d1a10" stroke="#fae8b4" stroke-width="2"/>`;
  svg.appendChild(g);

  if (state.hoverIndex !== null && state.hoverIndex !== undefined) {
    showCrosshair(state.hoverIndex);
  }
}

/** Paint the crosshair at settlement ``i`` and position the tooltip. */
function showCrosshair(i) {
  const geom = state.geom;
  const g = $('crosshair');
  const tip = $('chart-tip');
  if (!geom || !g) return;

  const idx = Math.max(0, Math.min(geom.points.length - 1, i));
  const p = geom.points[idx];
  const px = geom.X(idx);
  const py = geom.Y(p.v);

  g.style.display = '';
  g.querySelector('#ch-v').setAttribute('x1', px);
  g.querySelector('#ch-v').setAttribute('x2', px);
  g.querySelector('#ch-h').setAttribute('y1', py);
  g.querySelector('#ch-h').setAttribute('y2', py);
  g.querySelector('#ch-d').setAttribute('cx', px);
  g.querySelector('#ch-d').setAttribute('cy', py);

  // Step from the previous point is this settlement's own P/L.
  const step = idx > 0 ? p.v - geom.points[idx - 1].v : p.v;
  tip.hidden = false;
  tip.innerHTML =
    `<div class="tip-v ${p.v >= 0 ? 'pos-c' : 'neg-c'}">${signedMoney(p.v)}</div>` +
    `<div class="tip-m">settlement ${p.i || idx + 1} of ${p.n || geom.points.length}</div>` +
    `<div class="tip-m">this window <span class="${step >= 0 ? 'pos-c' : 'neg-c'}">${signedMoney(step)}</span></div>` +
    `<div class="tip-m">${chartDateTime(p.t)} ${tzAbbrev(new Date(p.t * 1000))}</div>`;

  // The SVG is stretched to the wrapper, so viewBox units map linearly to CSS
  // pixels. Clamp near the edges so the tooltip never leaves the card.
  const wrap = $('chart-wrap');
  const scaleX = wrap.clientWidth / geom.W;
  const scaleY = wrap.clientHeight / geom.H;
  const left = Math.max(60, Math.min(wrap.clientWidth - 60, px * scaleX));
  tip.style.left = `${left}px`;
  tip.style.top = `${Math.max(46, py * scaleY)}px`;
}

function hideCrosshair() {
  const g = $('crosshair');
  if (g) g.style.display = 'none';
  const tip = $('chart-tip');
  if (tip) tip.hidden = true;
}

function renderProfile(s) {
  const st = s.stats, cfg = s.config, eng = s.engine, rt = s.runtime;
  const bankroll = s.bankroll || cfg.bankroll || {};
  const target = s.performance_targets || {};
  const dailyLossBlocked = !bankroll.daily_loss_bypass_active &&
    Number(bankroll.daily_pnl_usd || 0) <= -Math.abs(Number(bankroll.max_daily_loss_usd || 0));

  $('profile-title').textContent =
    `Profile reconstruction (${rt.mode === 'paper' ? 'paper only' : 'LIVE'})`;
  $('profile-sub').textContent =
    `${cfg.assets.map((a) => a.toUpperCase()).join(' / ')} candidates · ` +
    `${cfg.strategy_config.hedge_enabled ? 'hedging on (wallet-faithful)' : 'first side only'} · ${rt.mode}`;

  const pill = $('profile-pill');
  const modeLabel = String(rt.mode || 'paper').toUpperCase();
  pill.textContent = rt.running
    ? `${modeLabel} \u00b7 ${rt.paused ? 'PAUSED' : (dailyLossBlocked ? 'LOSS STOP' : 'RUNNING')}`
    : 'OFFLINE';
  pill.className = `pill ${dailyLossBlocked ? 'warn' : (rt.running && !rt.paused ? 'ok' : '')}`;

  const lock = cfg.sizing.profit_lock_usd;
  const tiles = [
    ['Bankroll',      money(bankroll.starting_balance_usd), ''],
    ['Base clip',     money(cfg.sizing.base_clip_usd), ''],
    ['Maximum clip',  money(rt.max_per_trade_usd), ''],
    ['Open exposure', `${money(bankroll.open_exposure_usd)} / ${money(bankroll.max_open_exposure_usd)}`, ''],
    ['Signal scans',  eng.signal_scans.toLocaleString(), ''],
    ['Record',        `${st.wins}–${st.losses}`, ''],
    ['Cash reserve',  money(bankroll.cash_reserve_usd), ''],
    ['Daily loss stop', bankroll.daily_loss_bypass_active
      ? `${money(bankroll.max_daily_loss_usd)} (BYPASSED)`
      : money(bankroll.max_daily_loss_usd),
      bankroll.daily_loss_bypass_active ? 'neg' : ''],
    ['Hourly ROI target', `${spct(target.hourly_roi || 0)} / ${pct(target.hourly_roi_target || 0, 0)}`,
      (target.hourly_roi || 0) >= (target.hourly_roi_target || Infinity) ? 'pos' : ''],
    ['Daily profit target', `${money(target.daily_pnl_usd || 0)} / ${money(target.daily_profit_target_min_usd || 0)}\u2013${money(target.daily_profit_target_max_usd || 0)}`,
      cls(target.daily_pnl_usd || 0)],
    ['Profit lock',   `${money(st.total_pnl)} / ${money(lock)}`, cls(st.total_pnl)],
    ['Entries',       eng.entries.toLocaleString(), ''],
  ];

  const box = $('profile-tiles');
  box.innerHTML = '';
  for (const [label, value, klass] of tiles) {
    const t = el('div', 'ptile');
    t.appendChild(el('div', 'ptile-label', label));
    t.appendChild(el('div', `ptile-val ${klass}`, value));
    box.appendChild(t);
  }

  const sc = cfg.strategy_config, lim = cfg.limits;
  const qualityHtml =
    `<b>@antsaslyku replication.</b> Buys either side at ` +
    `${Math.round(sc.min_entry_price * 100)}–${Math.round(sc.max_entry_price * 100)}¢ whenever the model ` +
    `prices it above the all-in cost, in flat ${money(sc.base_clip_usd)} clips, held to resolution — ` +
    `the wallet never sold once in 569,015 fills. ` +
    `Ladder up to ${sc.max_fills_per_window} fills per window; hedging ` +
    `<b>${sc.hedge_enabled ? 'ON' : 'OFF'}</b>. ` +
    `A claimed edge above ${Math.round(sc.max_model_edge * 100)} points is refused as a feed fault — ` +
    `the wallet's largest real edge across 101,541 windows was 5.4 points. ` +
    `Caps: ${money(lim.hard_market_window_cap_usd)} per market/window, ` +
    `${money(lim.hard_window_cap_usd)} across all assets in one timed interval, ` +
    `${money(bankroll.max_open_exposure_usd)} total open, ` +
    `${money(bankroll.max_daily_loss_usd)} UTC-day loss stop on ${money(bankroll.starting_balance_usd)}.<br><br>` +
    `<b>Measured on the wallet, not promised here:</b> replaying its own entries with flat ` +
    `${money(sc.base_clip_usd)} clips in this band returned +3.50% ROI over 98,742 windows, positive in all ` +
    `five months but decaying (+4.23% May → +1.55% Aug). This bot must reproduce the entry timing to earn ` +
    `any of it. ` +
    `Realized win rate: ${st.settlements ? pct(st.win_rate) : 'no data yet'}; ` +
    `realized ROI: ${st.staked ? spct(st.roi) : 'no data yet'}.`;
  $('quality-box').innerHTML = dailyLossBlocked
    ? `<b>New entries stopped by the daily loss limit:</b> today's Paper P/L is ` +
      `${money(bankroll.daily_pnl_usd)} versus the ${money(bankroll.max_daily_loss_usd)} stop. ` +
      `Price feeds and signal scans remain active; entries resume automatically at 00:00 UTC.<br><br>${qualityHtml}`
    : qualityHtml;
}

function renderInfoCards(s) {
  const cfg = s.config;
  const selection = s.live_market_selection || {};
  const excluded = cfg.excluded_markets || [];
  const excludedLabel = excluded.length
    ? ` Disabled: ${excluded.map((row) => `${row.asset.toUpperCase()} ${row.window}`).join(' / ')}.`
    : '';
  const sc = cfg.strategy_config;
  if (s.runtime.mode === 'live' && selection.enabled) {
    const eligible = selection.eligible || [];
    $('entry-label').textContent = 'Live paper-tested whitelist';
    $('entry-assets').textContent = eligible.length
      ? eligible.map((row) => `${row.asset.toUpperCase()} ${row.window}`).join(' · ')
      : 'No market has enough successful paper results';
    $('entry-note').textContent =
      `Live entry requires at least ${selection.min_paper_settlements} settled paper trades and ` +
      `${pct(selection.min_paper_roi, 0)} paper ROI, then keeps the top ${selection.max_markets}. ` +
      'Paper mode continues testing every enabled market.' + excludedLabel;
  } else {
    $('entry-label').textContent =
      s.runtime.mode === 'live' ? 'Live matches Paper markets' : 'Paper entry restrictions';
    $('entry-assets').textContent =
      `${cfg.assets.map((a) => a.toUpperCase()).join(' / ')} · ${cfg.windows.join(' & ')} windows`;
    $('entry-note').textContent =
      `Entry band ${Math.round(sc.min_entry_price * 100)}–${Math.round(sc.max_entry_price * 100)}¢, ` +
      `${money(sc.base_clip_usd)} flat clips, up to ${sc.max_fills_per_window} fills per window, ` +
      `hedging ${sc.hedge_enabled ? 'ON' : 'OFF'}. ` +
      `Entries run the whole window — the cloned wallet's first-fill ROI is highest in the last ` +
      `decile (+16.19%), so there is no early cut-off.` +
      excludedLabel;
  }

  const src = s.sources;
  const names = Object.keys(src);
  $('sources-line').innerHTML = names
    .map((n) => `${n}: <span class="${src[n].active ? 'pos-c' : 'neg-c'}">${src[n].active ? 'active' : 'down'}</span>`)
    .join(' · ');
  $('sources-latency').textContent = names
    .map((n) => `${n} ${src[n].latency !== null && src[n].latency !== undefined ? src[n].latency.toFixed(1) + 's' : '—'}`)
    .join(' · ');

  const need = cfg.model_trust.min_test_windows;
  const have = s.stats.settlements;
  const trusted = have >= need;
  $('trust-line').innerHTML = trusted
    ? '<span class="pos-c">Sample large enough to read</span>'
    : 'Rules baseline remains active';
  $('trust-note').textContent = trusted
    ? `${have} settled windows ≥ the ${need} required. Win rate and ROI above are now meaningful, though still only a sample.`
    : `No candidate model is trusted for selection yet: ${have} settled windows < ${need} required. ` +
      `Treat the numbers above as provisional.`;
}

function renderPrices(s) {
  const row = $('price-row');
  row.innerHTML = '';
  row.classList.toggle('single', (s.spot || []).length === 1);
  const regimes = s.risk.regimes || {};

  for (const a of s.spot) {
    const halted = regimes[a.asset] && regimes[a.asset].halted;
    const card = el('div', `pcard ${halted ? 'halted' : ''}`);

    const ico = assetIcon(a.asset, 30);
    card.appendChild(ico);

    const mid = el('div');
    mid.appendChild(el('div', 'pname', `${a.label} / USD`));
    mid.appendChild(el('div', 'pprice', a.price === null ? '—' : `$${price(a.price, a.decimals)}`));
    mid.appendChild(el('div', 'pmeta', halted ? 'halted — volatility' : ago(a.age_seconds)));
    card.appendChild(mid);

    const chg = a.change_bps_300s;
    const c = el('div', `pchg ${chg === null ? '' : (chg >= 0 ? 'pos' : 'neg')}`,
      chg === null ? '—' : `${chg >= 0 ? '+' : ''}${(chg / 100).toFixed(3)}%`);
    card.appendChild(c);

    row.appendChild(card);
  }
}

function renderStrategy(s) {
  const st = (s.strategies || {}).antsaslyku;
  const card = $('strategy-card');
  card.classList.toggle('hidden', !st);
  if (!st) return;

  const ladders = st.windows || [];
  const rejections = Object.entries((st.stats || {}).rejections || {});
  $('cnt-lad').textContent = ladders.length;
  $('cnt-rej').textContent = rejections.length;

  const pill = $('strategy-pill');
  pill.textContent = st.dry_run ? 'DRY RUN' : `LIVE LADDER · ${ladders.length} OPEN`;
  pill.className = `pill ${st.dry_run ? 'warn' : (ladders.length ? 'ok' : '')}`;

  const cfg = st.config || {};
  const stats = st.stats || {};
  const byStage = stats.by_stage || {};
  const metrics = [
    ['Capital at work', money(st.open_cost_usd),
      '', `${st.open_windows} of ${cfg.max_open_windows} windows`],
    ['Fills', String(stats.fills || 0),
      '', `${byStage.open || 0} open · ${byStage.add || 0} add · ${byStage.hedge || 0} hedge`],
    ['Clip', money(cfg.base_clip_usd), '',
      `flat — measured $5.04 at every rung`],
    ['Ladder cap', String(cfg.max_fills_per_window), '',
      cfg.max_fills_per_window > 2 ? 'wallet-faithful (2 is peak ROI)' : 'profit-maximising'],
    ['Hedging', cfg.hedge_enabled ? 'ON' : 'OFF',
      cfg.hedge_enabled ? 'neg' : 'pos',
      cfg.hedge_enabled ? 'clone: pairs cost ~$1.04' : 'first side only'],
    ['Entry band', `${(cfg.min_entry_price * 100).toFixed(0)}–${(cfg.max_entry_price * 100).toFixed(0)}¢`,
      '', `edge capped at ${(cfg.max_model_edge * 100).toFixed(0)}pts`],
  ];
  const row = $('strategy-metrics');
  row.innerHTML = '';
  for (const [label, value, klass, sub] of metrics) {
    const m = el('div', 'metric');
    m.appendChild(el('div', 'metric-label', label));
    m.appendChild(el('div', `metric-val ${klass}`, value));
    m.appendChild(el('div', 'metric-sub', sub));
    row.appendChild(m);
  }

  // -- open ladders
  const tb = $('tb-ladders');
  tb.innerHTML = '';
  if (!ladders.length) {
    const tr = el('tr', 'empty');
    const td = el('td', '', s.runtime.running
      ? 'Scanning; no window has been entered yet.'
      : 'Engine offline.');
    td.colSpan = 6;
    tr.appendChild(td); tb.appendChild(tr);
  }
  for (const w of ladders.sort((a, b) => b.cost_usd - a.cost_usd)) {
    const parts = (w.slug || '').split('-');
    const asset = parts[0] || '?';
    const window = parts[2] || '';
    const tr = el('tr');

    const c0 = el('td');
    const cell = el('div', 'asset-cell');
    cell.appendChild(assetIcon(asset, 24));
    const nm = el('div');
    nm.appendChild(el('div', 'mkt-name', `${asset.toUpperCase()} ${window}`));
    const legs = Object.entries(w.side_cost || {})
      .map(([k, v]) => `${k} ${money(v)}`).join(' · ');
    nm.appendChild(el('div', 'mkt-sub', legs || '—'));
    cell.appendChild(nm);
    c0.appendChild(cell);
    tr.appendChild(c0);

    tr.appendChild(el('td', 'r mono', String(w.fills)));
    tr.appendChild(el('td', 'r mono', money(w.cost_usd)));

    const sd = el('td');
    if (w.first_side) sd.appendChild(el('span', `badge ${w.first_side}`, w.first_side.toUpperCase()));
    else sd.textContent = '—';
    tr.appendChild(sd);

    // Two sides held is the hedge: the pair pays $1 however it resolves, so
    // paying more than that for it is the loss the measurement identified.
    tr.appendChild(el('td', `r mono ${w.sides_held > 1 ? 'neg-c' : 'pos-c'}`,
      String(w.sides_held)));
    tr.appendChild(el('td', 'r mono dim', String(w.adverse_streak || 0)));
    tb.appendChild(tr);
  }

  // -- rejections
  const tr2 = $('tb-rejections');
  tr2.innerHTML = '';
  if (!rejections.length) {
    const tr = el('tr', 'empty');
    const td = el('td', '', 'Nothing declined yet.'); td.colSpan = 2;
    tr.appendChild(td); tr2.appendChild(tr);
  }
  for (const [reason, count] of rejections.sort((a, b) => b[1] - a[1])) {
    const tr = el('tr');
    tr.appendChild(el('td', '', reason));
    tr.appendChild(el('td', 'r mono', String(count)));
    tr2.appendChild(tr);
  }

  $('strategy-note').textContent =
    `Scanned ${stats.scans || 0} times, ${stats.intents || 0} tradeable, `
    + `${money(stats.cost_usd || 0)} staked this session.`;
}

function renderPositions(s) {
  const positions = s.positions || [];
  $('cnt-pos').textContent = positions.length;
  $('panic-count').textContent = positions.length;

  // Keep the asset filter in sync with what is actually tradeable.
  const sel = $('asset-filter');
  if (sel.options.length !== s.config.assets.length + 1) {
    sel.innerHTML = '<option value="">All assets</option>';
    for (const a of s.config.assets) {
      sel.appendChild(new Option(a.toUpperCase(), a));
    }
    sel.value = state.assetFilter;
  }

  const rows = positions.filter((p) => !state.assetFilter || p.asset === state.assetFilter);
  const tb = $('tb-positions');
  tb.innerHTML = '';

  if (!rows.length) {
    const tr = el('tr', 'empty');
    const bank = s.bankroll || {};
    const dailyLossBlocked = !bank.daily_loss_bypass_active &&
      Number(bank.daily_pnl_usd || 0) <= -Math.abs(Number(bank.max_daily_loss_usd || 0));
    const td = el('td', '', !s.runtime.running
      ? 'Engine offline.'
      : dailyLossBlocked
        ? `No open positions — the ${money(bank.max_daily_loss_usd)} daily loss stop is blocking new entries until 00:00 UTC.`
        : 'No open positions — the engine is scanning but no window has cleared every gate.');
    td.colSpan = 5;
    tr.appendChild(td); tb.appendChild(tr);
    return;
  }

  for (const p of rows) {
    const tr = el('tr');

    const c1 = el('td');
    const wrap = el('div', 'mkt');
    const ico = assetIcon(p.asset, 26);
    wrap.appendChild(ico);
    const nm = el('div');
    nm.appendChild(el('div', 'mkt-name',
      marketName(p.asset, p.window, p.window_end - (p.window === '5m' ? 300 : 900), p.window_end)));
    nm.appendChild(el('div', 'mkt-sub',
      `${p.window} · ${p.shares.toFixed(1)} shares · ${Math.max(0, p.seconds_remaining)}s left`));
    wrap.appendChild(nm);
    c1.appendChild(wrap);
    tr.appendChild(c1);

    const c2 = el('td');
    c2.appendChild(el('span', `badge ${p.side}`, p.side.toUpperCase()));
    const cf = el('div', 'conf');
    const bar = el('div', 'confbar');
    const fill = el('div', 'conffill');
    fill.style.width = `${Math.round(p.confidence * 100)}%`;
    bar.appendChild(fill); cf.appendChild(bar);
    cf.appendChild(el('span', 'conftxt', pct(p.confidence, 0)));
    c2.appendChild(cf);
    tr.appendChild(c2);

    tr.appendChild(el('td', 'r mono', `${(p.entry_price * 100).toFixed(0)}¢`));
    tr.appendChild(el('td', 'r mono', money(p.stake_usd)));

    const c5 = el('td', 'r');
    const lab = el('div', p.ahead === null ? 'dim' : (p.ahead ? 'pos-c' : 'neg-c'), p.status_label);
    c5.appendChild(lab);
    c5.appendChild(el('div', 'mkt-sub mono',
      (p.spot_price !== null && p.anchor_price)
        ? `${price(p.spot_price, p.decimals)} / ${price(p.anchor_price, p.decimals)}`
        : '— / —'));
    tr.appendChild(c5);

    tb.appendChild(tr);
  }
}

async function refreshActivity() {
  try {
    const { activity } = await api('/api/activity?limit=200');
    $('cnt-act').textContent = activity.length;
    const tb = $('tb-activity');
    tb.innerHTML = '';
    if (!activity.length) {
      const tr = el('tr', 'empty');
      const td = el('td', '', 'No activity yet.'); td.colSpan = 3;
      tr.appendChild(td); tb.appendChild(tr); return;
    }
    for (const a of activity) {
      const tr = el('tr');
      tr.appendChild(el('td', 'mono dim', clock(a.ts)));
      const k = el('td');
      const kindClass = { entry: 'up', settlement: '', error: 'down', panic: 'down', reversal: 'up' }[a.kind] ?? '';
      k.appendChild(el('span', `badge ${kindClass}`, a.kind.toUpperCase()));
      tr.appendChild(k);
      const d = el('td');
      d.appendChild(el('div', '', a.message));
      if (a.detail) d.appendChild(el('div', 'mkt-sub', a.detail));
      tr.appendChild(d);
      tb.appendChild(tr);
    }
  } catch { /* transient */ }
}

function renderMonitor(s) {
  const st = s.stats, eng = s.engine;
  const metrics = [
    ['Est. settled P/L', (st.total_pnl >= 0 ? '' : '−') + '$' + Math.abs(st.total_pnl).toFixed(2), cls(st.total_pnl), 'local estimate, not wallet cash'],
    ['Win rate',      st.settlements ? pct(st.win_rate) : '—', '', `${st.wins} wins`],
    ['ROI / stake',   st.staked ? spct(st.roi) : '—', cls(st.roi), `staked ${money(st.staked)}`],
    ['Record',        `${st.wins}–${st.losses}`, '', `${st.open_positions} open windows`],
    ['Settlements',   String(st.settlements), '', `${st.settlements} retained`],
    ['Loop latency',  `${eng.last_loop_ms} ms`, 'pos', `${eng.loops} loops`],
  ];
  const row = $('metricrow');
  row.innerHTML = '';
  for (const [label, value, klass, sub] of metrics) {
    const m = el('div', 'metric');
    m.appendChild(el('div', 'metric-label', label));
    m.appendChild(el('div', `metric-val ${klass}`, value));
    m.appendChild(el('div', 'metric-sub', sub));
    row.appendChild(m);
  }

  const rej = eng.rejections || {};
  const entries = Object.entries(rej);
  const max = Math.max(1, ...entries.map(([, n]) => n));
  const box = $('rejections');
  box.innerHTML = '';
  if (!entries.length) {
    box.appendChild(el('div', 'card-note', 'Nothing rejected yet.'));
  }
  for (const [reason, n] of entries) {
    const r = el('div', 'rej');
    r.appendChild(el('div', 'rej-txt', reason));
    const bar = el('div', 'rej-bar');
    const fill = el('div', 'rej-fill');
    fill.style.width = `${(n / max) * 100}%`;
    bar.appendChild(fill);
    r.appendChild(bar);
    r.appendChild(el('div', 'rej-n', String(n)));
    box.appendChild(r);
  }

  renderSources(s);

  const tele = [
    ['Signal scans', eng.signal_scans.toLocaleString()],
    ['Entries',      eng.entries.toLocaleString()],
    ['Loops',        eng.loops.toLocaleString()],
    ['Loop time',    `${eng.last_loop_ms} ms`],
    ['Windows live', String(s.markets.live)],
    ['Windows tracked', String(s.markets.tracked)],
  ];
  const t = $('telemetry');
  t.innerHTML = '';
  for (const [label, value] of tele) {
    const p = el('div', 'ptile');
    p.appendChild(el('div', 'ptile-label', label));
    p.appendChild(el('div', 'ptile-val', value));
    t.appendChild(p);
  }
}

/* Latency thresholds in seconds. Absolute, not relative to the slowest source:
 * a pass where everything is slow should read as slow, not as "one green, one
 * red". FULL_SCALE is where the bar saturates. */
const LAT_FAST = 0.5;
const LAT_OK = 1.5;
const LAT_FULL_SCALE = 3.0;

function renderSources(s) {
  const sources = s.sources || {};
  const names = Object.keys(sources);
  const tb = $('tb-sources');
  tb.innerHTML = '';

  if (!names.length) {
    const tr = el('tr', 'empty');
    const td = el('td', '', 'Waiting for the first poll…'); td.colSpan = 7;
    tr.appendChild(td); tb.appendChild(tr);
    return;
  }

  const up = names.filter((n) => sources[n].active).length;
  const pill = $('src-pill');
  pill.textContent = `${up}/${names.length} ACTIVE`;
  pill.className = `pill ${up === names.length ? 'ok' : (up === 0 ? 'bad' : 'warn')}`;

  let slowest = 0;
  for (const name of names) {
    const src = sources[name];
    const lat = src.latency;
    if (lat !== null && lat !== undefined) slowest = Math.max(slowest, lat);

    const tr = el('tr');

    const c0 = el('td');
    c0.appendChild(el('div', `src-dot ${src.active ? '' : (lat === null ? 'idle' : 'down')}`));
    tr.appendChild(c0);

    const c1 = el('td');
    c1.appendChild(el('div', 'src-name', name.replace(/_/g, ' ')));
    c1.appendChild(el('div', 'src-url', `${src.kind || 'REST'} · ${src.url || '—'}`));
    tr.appendChild(c1);

    tr.appendChild(el('td', 'dim', src.role || '—'));

    // Latency in ms — seconds hide the difference between 40ms and 400ms.
    const ms = (lat === null || lat === undefined) ? null : lat * 1000;
    tr.appendChild(el('td', 'r mono', ms === null ? '—' : `${ms.toFixed(0)} ms`));

    const c4 = el('td');
    const wrap = el('div', 'lat-wrap');
    const bar = el('div', 'lat-bar');
    const fill = el('div', `lat-fill ${lat < LAT_FAST ? 'fast' : (lat < LAT_OK ? 'ok' : 'slow')}`);
    fill.style.width = ms === null
      ? '0%'
      : `${Math.max(3, Math.min(100, (lat / LAT_FULL_SCALE) * 100))}%`;
    bar.appendChild(fill);
    wrap.appendChild(bar);
    wrap.appendChild(el('div', 'lat-val',
      ms === null ? '—' : (lat < LAT_FAST ? 'fast' : (lat < LAT_OK ? 'ok' : 'slow'))));
    c4.appendChild(wrap);
    tr.appendChild(c4);

    // A near-zero pass means the batch was served from cache, not that the
    // network is impossibly fast. Say so rather than showing a proud "fast".
    if (ms !== null && ms < 5 && src.resolved) {
      wrap.querySelector('.lat-val').textContent = 'cached';
      wrap.querySelector('.lat-val').classList.add('dim');
      fill.className = 'lat-fill fast';
      fill.style.width = '3%';
    }

    // How much of what it was asked for actually came back.
    const cov = (src.resolved !== undefined && src.polled !== undefined)
      ? `${src.resolved}/${src.polled}`
      : (src.polled !== undefined ? `${src.polled}` : '—');
    tr.appendChild(el('td', 'r mono dim', cov));

    const c6 = el('td', src.active ? 'pos-c' : 'neg-c');
    c6.textContent = src.error ? src.error : (src.active ? 'healthy' : 'no data');
    tr.appendChild(c6);

    tb.appendChild(tr);
  }

  const stale = (s.spot || []).filter((a) => a.age_seconds !== null && a.age_seconds > 15);
  $('src-foot').innerHTML =
    `Engine loop <b>${s.engine.last_loop_ms} ms</b> · slowest source <b>${(slowest * 1000).toFixed(0)} ms</b> · ` +
    `tracking <b>${s.markets.live}</b> live windows of <b>${s.markets.tracked}</b> known` +
    (s.markets.discovery_misses
      ? ` · <span class="neg-c">${s.markets.discovery_misses} discovery miss(es)</span>` : '') +
    (stale.length
      ? ` · <span class="neg-c">${stale.map((a) => a.label).join(', ')} spot is stale</span>`
      : ' · all spot feeds fresh');
}

async function refreshSignals() {
  if (state.signalsCollapsed) return;
  try {
    const { signals } = await api('/api/signals');
    if (state.signalsCollapsed) return;
    const tb = $('tb-signals');
    const rows = signals.sort((a, b) => b.expected_roi - a.expected_roi);
    if (!rows.length) {
      const tr = el('tr', 'empty');
      const td = el('td', '', 'Waiting for the first scan…'); td.colSpan = 8;
      tr.appendChild(td); tb.replaceChildren(tr); return;
    }
    const fragment = document.createDocumentFragment();
    for (const g of rows) {
      const tr = el('tr');
      tr.appendChild(el('td', '', `${g.asset.toUpperCase()} ${g.window}`));
      const sd = el('td');
      sd.appendChild(el('span', `badge ${g.side}`, g.side.toUpperCase()));
      tr.appendChild(sd);
      tr.appendChild(el('td', 'r mono', pct(g.confidence, 1)));
      tr.appendChild(el('td', 'r mono', `${(g.entry_price * 100).toFixed(0)}¢`));
      tr.appendChild(el('td', `r mono ${g.expected_roi >= 0 ? 'pos-c' : 'neg-c'}`, spct(g.expected_roi)));
      tr.appendChild(el('td', `r mono ${g.drift_bps >= 0 ? 'pos-c' : 'neg-c'}`, `${g.drift_bps.toFixed(1)}`));
      tr.appendChild(el('td', 'r mono dim', `${Math.max(0, Math.round(g.seconds_remaining))}s`));
      tr.appendChild(el('td', g.tradeable ? 'pos-c' : 'dim', g.tradeable ? '✓ tradeable' : g.reason));
      fragment.appendChild(tr);
    }
    tb.replaceChildren(fragment);
  } catch { /* transient */ }
}

function setSignalsCollapsed(collapsed, persist = true) {
  state.signalsCollapsed = Boolean(collapsed);
  const card = $('live-signals-card');
  const toggle = $('toggle-live-signals');
  card.classList.toggle('is-collapsed', state.signalsCollapsed);
  toggle.setAttribute('aria-expanded', String(!state.signalsCollapsed));
  toggle.querySelector('.collapse-label').textContent =
    state.signalsCollapsed ? 'Expand' : 'Collapse';

  if (persist) {
    try { localStorage.setItem('polybot.liveSignalsCollapsed', String(state.signalsCollapsed)); }
    catch { /* storage may be disabled */ }
  }

  if (state.signalsCollapsed) {
    // Release potentially thousands of historical row nodes while hidden.
    $('tb-signals').replaceChildren();
  } else if (state.tab === 'monitor') {
    refreshSignals();
  }
}

/** Small circular asset badge, reused across tables. */
function assetIcon(asset, size = 24) {
  const key = String(asset || '').toLowerCase();
  const ico = el('div', `pico asset-${key}`);
  ico.style.width = ico.style.height = `${size}px`;
  ico.style.fontSize = `${Math.round(size * 0.38)}px`;

  if (ASSET_ICONS[key]) {
    ico.classList.add('has-icon');
    const img = el('img');
    img.src = ASSET_ICONS[key];
    img.alt = '';
    img.loading = 'lazy';
    img.decoding = 'async';
    ico.appendChild(img);
  } else {
    ico.textContent = key.slice(0, 2).toUpperCase();
    ico.style.background = ASSET_COLORS[key] || '#fae8b4';
  }

  return ico;
}

async function refreshWinLoss() {
  try {
    const range = encodeURIComponent(state.wlRange);
    const [{ rows }, { settlements, summary: rangeSummary }] = await Promise.all([
      api(`/api/by-asset?range=${range}&${scopeQuery()}`),
      api(`/api/settlements?limit=200&range=${range}&${scopeQuery()}`),
    ]);

    // ---- summary strip -------------------------------------------------
    const summaryData = rangeSummary || {};
    const wins = Number(summaryData.wins || 0);
    const losses = Number(summaryData.losses || 0);
    const totalSettlements = Number(summaryData.settlements || 0);
    const grossWin = Number(summaryData.gross_win || 0);
    const grossLoss = Number(summaryData.gross_loss || 0);
    // Profit factor: gross winnings per dollar of gross losses. Above 1 is
    // profitable; it is far more legible than net P/L on a small sample.
    const pf = grossLoss !== 0 ? Math.abs(grossWin / grossLoss) : null;
    const best = summaryData.best;
    const worst = summaryData.worst;

    const summary = [
      ['✓', 'Wins', String(wins), 'win'],
      ['✕', 'Losses', String(losses), 'loss'],
      ['◆', 'Profit factor', pf === null ? '—' : pf.toFixed(2), pf !== null && pf >= 1 ? 'win' : 'loss'],
      ['▲', 'Best window', best !== null && best !== undefined ? signedMoney(best) : '—', 'win'],
      ['▼', 'Worst window', worst !== null && worst !== undefined ? signedMoney(worst) : '—', 'loss'],
    ];
    const sbox = $('wl-summary');
    sbox.innerHTML = '';
    for (const [glyph, label, value, kind] of summary) {
      const m = el('div', 'metric');
      const head = el('div');
      head.style.display = 'flex';
      head.style.alignItems = 'center';
      head.style.gap = '8px';
      head.appendChild(el('div', `oico ${kind}`, glyph));
      head.appendChild(el('div', 'metric-label', label));
      m.appendChild(head);
      const v = el('div', `metric-val ${kind === 'win' ? 'pos' : 'neg'}`, value);
      v.style.marginTop = '7px';
      m.appendChild(v);
      sbox.appendChild(m);
    }

    const rangeLabel = RANGE_LABEL[state.wlRange] || state.wlRange;
    $('wl-range-note').textContent =
      `Summary and both tables cover ${rangeLabel} · ${totalSettlements} settlement` +
      `${totalSettlements === 1 ? '' : 's'}.`;
    $('wl-pill').textContent =
      `${rows.length} COMBINATION${rows.length === 1 ? '' : 'S'} · ${rangeLabel.toUpperCase()}`;

    // ---- by asset ------------------------------------------------------
    const t1 = $('tb-byasset');
    t1.innerHTML = '';
    if (!rows.length) {
      const tr = el('tr', 'empty');
      const td = el('td', '', 'No settled windows yet.'); td.colSpan = 8;
      tr.appendChild(td); t1.appendChild(tr);
    }
    for (const r of rows) {
      const tr = el('tr');

      const c1 = el('td');
      const cell = el('div', 'asset-cell');
      cell.appendChild(assetIcon(r.asset));
      cell.appendChild(el('span', '', r.asset.toUpperCase()));
      c1.appendChild(cell);
      tr.appendChild(c1);

      tr.appendChild(el('td', 'dim', r.window));
      tr.appendChild(el('td', 'r mono', String(r.settlements)));

      // Win rate with a 50% reference marker — the number alone does not say
      // whether it beat a coin flip.
      const c4 = el('td');
      const top = el('div', 'wrtop');
      top.appendChild(el('span', 'wrpct', pct(r.win_rate)));
      top.appendChild(el('span', 'mkt-sub', r.win_rate >= 0.5 ? 'above flip' : 'below flip'));
      c4.appendChild(top);
      const bar = el('div', 'wrbar');
      const fill = el('div', `wrfill ${r.win_rate >= 0.5 ? 'good' : 'bad'}`);
      fill.style.width = `${Math.round(r.win_rate * 100)}%`;
      bar.appendChild(fill);
      bar.appendChild(el('div', 'wrmark'));
      c4.appendChild(bar);
      tr.appendChild(c4);

      const c5 = el('td', 'r rec');
      const w = el('span', 'w', String(r.wins));
      const sep = el('span', 'sep', '–');
      const l = el('span', 'l', String(r.settlements - r.wins));
      c5.append(w, sep, l);
      tr.appendChild(c5);

      tr.appendChild(el('td', 'r mono', money(r.staked)));
      tr.appendChild(el('td', `r mono ${r.pnl >= 0 ? 'pos-c' : 'neg-c'}`, signedMoney(r.pnl)));
      tr.appendChild(el('td', `r mono ${r.roi >= 0 ? 'pos-c' : 'neg-c'}`, spct(r.roi)));
      t1.appendChild(tr);
    }

    // ---- recent settlements -------------------------------------------
    const filtered = settlements.filter((x) =>
      state.wlFilter === 'all' ? true : (state.wlFilter === 'win' ? x.won : !x.won));

    const t2 = $('tb-settlements');
    t2.innerHTML = '';
    if (!filtered.length) {
      const tr = el('tr', 'empty');
      const td = el('td', '', settlements.length
        ? `No ${state.wlFilter === 'win' ? 'wins' : 'losses'} to show.`
        : 'No settled windows yet.');
      td.colSpan = 8;
      tr.appendChild(td); t2.appendChild(tr);
    }
    for (const x of filtered) {
      const tr = el('tr');

      const c0 = el('td');
      c0.appendChild(el('div', `oico ${x.won ? 'win' : 'loss'}`, x.won ? '✓' : '✕'));
      tr.appendChild(c0);

      const c1 = el('td');
      const cell = el('div', 'asset-cell');
      cell.appendChild(assetIcon(x.asset, 26));
      const nm = el('div');
      nm.appendChild(el('div', 'mkt-name', `${x.asset.toUpperCase()} ${x.window}`));
      nm.appendChild(el('div', 'mkt-sub', `${x.shares.toFixed(1)} shares`));
      cell.appendChild(nm);
      c1.appendChild(cell);
      tr.appendChild(c1);

      const sd = el('td');
      sd.appendChild(el('span', `badge ${x.side}`, x.side.toUpperCase()));
      tr.appendChild(sd);

      tr.appendChild(el('td', 'r mono', `${(x.entry_price * 100).toFixed(0)}¢`));
      tr.appendChild(el('td', 'r mono', money(x.stake_usd)));
      tr.appendChild(el('td', `r mono ${x.won ? 'pos-c' : 'neg-c'}`, signedMoney(x.pnl_usd)));

      const c6 = el('td', 'mono dim');
      if (x.close_price) {
        const up = Number(x.close_price) >= Number(x.anchor_price);
        c6.innerHTML =
          `${Number(x.anchor_price).toPrecision(6)} ` +
          `<span class="${up ? 'pos-c' : 'neg-c'}">${up ? '↑' : '↓'}</span> ` +
          `${Number(x.close_price).toPrecision(6)}`;
      } else {
        c6.textContent = x.method || '—';
      }
      tr.appendChild(c6);

      tr.appendChild(el('td', 'r mono dim', clock(x.settled_at)));
      t2.appendChild(tr);
    }
  } catch { /* transient */ }
}

function renderControls(s) {
  const rt = s.runtime, risk = s.risk;
  // The literal clone ladders and hedges, which is the capital-hungry
  // configuration; flag it so the risk panel says which variant is armed.
  const sc = s.config.strategy_config || {};
  const faithful = Boolean(sc.hedge_enabled) || Number(sc.max_fills_per_window) > 2;

  $('risk-pill').textContent = faithful ? 'CLONE — FULL LADDER' : 'APPLIED';
  $('risk-pill').className = `pill ${faithful ? 'warn' : 'ok'}`;
  $('sl-window').disabled = false;
  $('sl-daily').disabled = false;
  $('trade-limit-note').textContent =
    'A hard ceiling on any one clip. Applies to the next clip and is shared by Paper and Live.';
  $('daily-limit-note').textContent =
    'Total stake the engine may deploy per UTC day across every asset. Resets at 00:00 UTC.';

  $('btn-pause').textContent = rt.paused ? 'Resume trading' : 'Pause trading';
  const tp = $('trading-pill');
  tp.textContent = rt.paused ? 'PAUSED' : 'TRADING';
  tp.className = `pill ${rt.paused ? 'warn' : 'ok'}`;

  const bank = s.bankroll || {};
  const bypassActive = Boolean(bank.daily_loss_bypass_active);
  const bypassScope = String(bank.daily_loss_bypass_scope || 'hour');
  const lossLimit = Math.abs(Number(bank.max_daily_loss_usd || 0));
  const dayPnl = Number(bank.daily_pnl_usd || 0);
  const atLossStop = lossLimit > 0 && dayPnl <= -lossLimit;
  const modeLabel = String(rt.mode || 'paper').toUpperCase();
  const minsLeft = Math.max(0, Math.ceil(
    (Number(bank.daily_loss_bypass_until || 0) * 1000 - Date.now()) / 60000));
  const hourBtn = $('btn-loss-bypass');
  const dayBtn = $('btn-loss-bypass-day');
  hourBtn.textContent = bypassActive && bypassScope === 'hour'
    ? `Loss stop bypassed · ${minsLeft}m left`
    : 'Bypass daily loss stop (1h)';
  dayBtn.textContent = bypassActive && bypassScope === 'day'
    ? 'Bypassed until 00:00 UTC'
    : 'Bypass for the day';
  // Re-firing while one is already running would only restate the same window,
  // so both stay out of the way until the current one lapses.
  hourBtn.disabled = bypassActive;
  dayBtn.disabled = bypassActive;
  const heldFor = bypassScope === 'day'
    ? 'until the 00:00 UTC reset, with no earlier expiry'
    : `for another ${minsLeft} minute(s), then it re-engages on its own`;
  $('loss-bypass-note').innerHTML = bypassActive
    ? `<b>${modeLabel} loss stop bypassed:</b> new entries are allowed past the ` +
      `${money(lossLimit)} stop ${heldFor}. Today's P/L is still ${signedMoney(dayPnl)}.`
    : atLossStop
      ? `<b>Currently stopped:</b> today's P/L is ${signedMoney(dayPnl)} against the ` +
        `${money(lossLimit)} stop. Bypassing lets ${modeLabel} open new positions — ` +
        `for one hour, or for the rest of the UTC day.`
      : `Overrides the ${money(lossLimit)} daily loss stop for one hour, or for the rest ` +
        `of the UTC day. Applies to whichever mode is running — currently ${modeLabel}.`;

  const anyHalted = Object.values(risk.regimes || {}).some((r) => r.halted);
  const halted = risk.breaker_active || anyHalted;

  const box = $('regime-box');
  const st = $('regime-state');
  box.className = `regime ${halted ? 'halted' : ''}`;
  st.className = `regime-state ${halted ? 'halted' : ''}`;
  st.textContent =
    risk.breaker_active ? 'HALTED' : (anyHalted ? 'PARTIAL' : 'NORMAL');

  if (risk.breaker_active) {
    $('regime-note').textContent =
      `${risk.breaker_reason || 'Hourly loss breaker tripped.'} Resuming in ${risk.breaker_seconds_remaining}s.`;
  } else if (anyHalted) {
    const names = Object.entries(risk.regimes).filter(([, r]) => r.halted)
      .map(([a]) => a.toUpperCase()).join(', ');
    $('regime-note').textContent = `${names} above the volatility ceiling — no new windows on those assets.`;
  } else {
    $('regime-note').textContent =
      `Rolling 60-minute net P/L ${money(risk.breaker_drawdown)} against a ` +
      `${money(-Math.abs(s.config.risk.drawdown_breaker.max_loss_usd))} loss limit.`;
  }

  const vr = $('vol-row');
  vr.innerHTML = '';
  for (const a of s.spot) {
    const r = (risk.regimes || {})[a.asset] || {};
    const t = el('div', 'vtile');
    t.appendChild(el('div', 'vtile-label', a.label));
    t.appendChild(el('div', `vtile-val ${r.halted ? 'hot' : ''}`,
      r.vol_bps === null || r.vol_bps === undefined ? '—' : r.vol_bps.toFixed(1)));
    vr.appendChild(t);
  }
  $('vol-note').textContent =
    `Realized volatility of each underlying over the last 15 minutes, in bps. Above ` +
    `${risk.halt_above_bps} an asset stops opening new windows — the lead/lag edge degrades in that ` +
    `regime. It resumes below ${risk.resume_below_bps}. Windows already open keep trading.`;
}

function renderVaultState(s) {
  const v = s.vault, av = s.live_availability;
  $('vault-chip').textContent = v.loaded ? 'loaded' : 'empty';

  const pill = $('vault-pill');
  pill.textContent = v.loaded ? (v.has_api_creds ? 'KEY + L2 CREDS' : 'KEY ONLY') : 'NOT LOADED';
  if (v.loaded && v.has_api_creds && !v.clob_v2_verified) {
    pill.textContent = 'LEGACY CLOB CREDS';
  }
  pill.className = `pill ${v.loaded && v.clob_v2_verified ? 'ok' : ''}`;

  const b = $('live-blockers');
  $('live-gate-title').textContent = av.env_enabled
    ? 'Live trading gate enabled'
    : 'Live trading gate disabled';
  $('live-gate-note').innerHTML = av.env_enabled
    ? 'The bot remains in paper mode until <b>Live</b> is explicitly selected. Live orders spend real pUSD. Read <code>LIVE_TRADING.md</code> before the first order.'
    : 'Set <code>LIVE_TRADING_ENABLED=1</code> in <code>.env</code> to permit live mode. Saving wallet credentials alone never enables real orders.';
  b.textContent = av.ready
    ? (s.runtime.mode === 'live'
      ? '● LIVE MODE ACTIVE — orders can spend real pUSD.'
      : '✓ Live trading is enabled and credentials are ready. The bot remains in paper mode until you explicitly switch to Live.')
    : `Blocked by: ${av.blockers.join('; ')}.`;
  b.style.color = av.ready ? 'var(--amber)' : 'var(--red)';

  if (v.loaded && v.address && !$('f-addr').value) {
    $('f-addr').placeholder = v.address;
  }
  if (v.loaded && v.funder && !$('f-funder').value) {
    $('f-funder').placeholder = v.funder;
  }
  if (!state.vaultHydrated) {
    $('f-sig').value = String(v.signature_type ?? 0);
    state.vaultHydrated = true;
  }

  $('f-key').placeholder = v.has_api_creds
    ? `Saved ${v.masked.api_key}`
    : 'UUID — leave blank to derive';
  $('f-secret').placeholder = v.has_api_creds
    ? `Saved ${v.masked.api_secret}`
    : 'base64 — leave blank to derive';
  $('f-pass').placeholder = v.has_api_creds
    ? `Saved ${v.masked.api_passphrase}`
    : 'passphrase — leave blank to derive';
}

function renderChainlink(s) {
  const cl = s.chainlink;
  if (!cl) return;
  const h = cl.health || {};

  const pill = $('cl-pill');
  if (!cl.configured) {
    pill.textContent = 'NOT CONFIGURED';
    pill.className = 'pill';
  } else if (h.active) {
    pill.textContent = `LIVE · ${h.feeds} FEEDS`;
    pill.className = 'pill ok';
  } else {
    pill.textContent = 'ERROR';
    pill.className = 'pill bad';
  }

  const tb = $('tb-chainlink');
  tb.innerHTML = '';
  const prices = cl.prices || {};
  const feeds = cl.feeds || {};
  const assets = Object.keys(feeds).filter((a) => feeds[a]);

  if (!assets.length) {
    const tr = el('tr', 'empty');
    const td = el('td', '', cl.configured
      ? (h.error || 'Connected, but no feeds resolved.')
      : 'No feeds connected — the engine is using Coinbase/Kraken spot, which is a proxy for the resolution source.');
    td.colSpan = 5;
    tr.appendChild(td); tb.appendChild(tr);
    return;
  }

  for (const a of assets) {
    const p = prices[a];
    const tr = el('tr');
    const c0 = el('td');
    const cell = el('div', 'asset-cell');
    cell.appendChild(assetIcon(a, 22));
    cell.appendChild(el('span', '', a.toUpperCase()));
    c0.appendChild(cell);
    tr.appendChild(c0);
    tr.appendChild(el('td', 'feedid', (feeds[a] || '').slice(0, 26) + '…'));
    tr.appendChild(el('td', 'r mono', p ? `$${p.price.toLocaleString()}` : '—'));
    // Venue-observation to local-arrival: the actual freshness of the number.
    tr.appendChild(el('td', `r mono ${p && p.latency_ms < 500 ? 'pos-c' : 'dim'}`,
      p ? `${p.latency_ms.toFixed(0)} ms` : '—'));
    tr.appendChild(el('td', 'r mono dim', p ? `${p.age_s.toFixed(1)}s` : '—'));
    tb.appendChild(tr);
  }
}

function renderFooter(s) {
  $('foot-mode').textContent = `mode: ${s.runtime.mode}`;
  $('foot-loop').textContent = `loop: ${s.engine.last_loop_ms}ms · ${s.markets.live} live windows`;
  $('foot-time').textContent = `${clock(s.ts)} ${tzAbbrev(new Date(s.ts * 1000))}`;
}

/* ── wiring ─────────────────────────────────────────────────────────────── */

function wire() {
  let savedSignalsCollapse = false;
  try { savedSignalsCollapse = localStorage.getItem('polybot.liveSignalsCollapsed') === 'true'; }
  catch { /* storage may be disabled */ }
  setSignalsCollapsed(savedSignalsCollapse, false);
  $('toggle-live-signals').addEventListener('click', () => {
    setSignalsCollapsed(!state.signalsCollapsed);
  });

  $('btn-power').addEventListener('click', async () => {
    const running = state.snap && state.snap.runtime.running;
    try {
      if (running) { await api('/api/engine/stop', { method: 'POST', body: '{}' }); toast('Engine stopped'); }
      else { await api('/api/engine/start', { method: 'POST', body: '{}' }); toast('Engine started'); }
    } catch (e) { toast(e.message, 'err'); }
  });

  document.querySelectorAll('#mode-seg .seg-btn').forEach((b) => {
    b.addEventListener('click', async () => {
      const mode = b.dataset.mode;
      if (mode === (state.snap && state.snap.runtime.mode)) return;
      if (mode === 'live') {
        const ok = confirm(
          'Switch to LIVE mode?\n\n' +
          'Live orders spend real pUSD from your wallet. The live order path in this build has ' +
          'never been executed against real funds — its first order is its first test.\n\n' +
          'Continue?');
        if (!ok) return;
      }
      try {
        await api('/api/engine/mode', { method: 'POST', body: JSON.stringify({ mode }) });
        // Re-scope immediately. Waiting for the next poll would leave the
        // tables showing the previous mode's numbers beside the new mode's
        // headline figure.
        state.lastMode = mode;
        rescope({});
        toast(`Execution mode: ${mode}`, mode === 'live' ? 'warn' : 'ok');
      } catch (e) { toast(e.message, 'err'); }
    });
  });

  // Delegated: the segment is rebuilt whenever the available strategies change.
  $('strategy-seg').addEventListener('click', async (ev) => {
    const b = ev.target.closest('.seg-btn');
    if (!b) return;
    const name = b.dataset.strategy;
    if (name === state.strategy) return;

    const sel = (state.snap && state.snap.strategy_selection) || {};
    const configured = sel.configured || [];

    // In Live the segment is not a view filter — it decides which strategy is
    // allowed to spend real money, so it must be confirmed and pushed to the
    // engine before the view follows.
    if (sel.exclusive && name !== 'all' && configured.includes(name)) {
      const ok = confirm(
        `Switch the LIVE strategy to ${strategyLabel(name)}?\n\n` +
        'Live runs exactly one strategy. ' +
        `${strategyLabel(sel.selected || '—')} will stop taking new entries ` +
        `and ${strategyLabel(name)} will start.\n\n` +
        'Open positions are unaffected — they settle into their own strategy.\n\nContinue?');
      if (!ok) return;
      try {
        await api('/api/strategy', {
          method: 'POST', body: JSON.stringify({ strategy: name }),
        });
        toast(`Live strategy: ${strategyLabel(name)}`, 'warn');
      } catch (e) { toast(e.message, 'err'); return; }
    }

    rescope({ strategy: name });
  });

  const bindSlider = (id, field, mirror) => {
    const node = $(id);
    node.addEventListener('pointerdown', () => { state.sliderHeld = true; });
    node.addEventListener('input', () => {
      const v = Number(node.value);
      if (field === 'max_per_trade_usd') { $('val-trade').textContent = money(v); $('big-trade').textContent = money(v); }
      if (field === 'max_per_window_usd') $('val-window').textContent = money(v);
      if (field === 'daily_cap_usd') $('big-daily').textContent = money(v);
      if (mirror) $(mirror).value = v;
    });
    node.addEventListener('change', async () => {
      state.sliderHeld = false;
      try { await api('/api/limits', { method: 'POST', body: JSON.stringify({ [field]: Number(node.value) }) }); }
      catch (e) { toast(e.message, 'err'); }
    });
  };
  bindSlider('sl-trade',  'max_per_trade_usd',  'sl-trade2');
  bindSlider('sl-trade2', 'max_per_trade_usd',  'sl-trade');
  bindSlider('sl-window', 'max_per_window_usd', null);
  bindSlider('sl-daily',  'daily_cap_usd',      null);

  const editableLimits = [
    'lim-bankroll', 'lim-trade', 'lim-market-window', 'lim-window', 'lim-open', 'lim-loss',
    'lim-daily', 'lim-confidence', 'lim-price', 'lim-timing',
  ];
  const updateImpliedReserve = () => {
    const bankroll = Number($('lim-bankroll').value);
    const open = Number($('lim-open').value);
    $('lim-reserve').value = Number.isFinite(bankroll) && Number.isFinite(open)
      ? Math.max(0, bankroll - open).toFixed(2)
      : '';
  };
  for (const id of editableLimits) {
    $(id).addEventListener('input', () => {
      state.limitsEditing = true;
      $('limits-pill').textContent = 'UNSAVED';
      $('limits-pill').className = 'pill warn';
      updateImpliedReserve();
    });
  }

  $('btn-save-limits').addEventListener('click', async () => {
    const body = {
      starting_balance_usd: Number($('lim-bankroll').value),
      max_per_trade_usd: Number($('lim-trade').value),
      max_per_market_window_usd: Number($('lim-market-window').value),
      max_per_window_usd: Number($('lim-window').value),
      max_open_exposure_usd: Number($('lim-open').value),
      max_daily_loss_usd: Number($('lim-loss').value),
      daily_cap_usd: Number($('lim-daily').value),
      min_confidence: Number($('lim-confidence').value) / 100,
      max_entry_price: Number($('lim-price').value) / 100,
      max_window_fraction: Number($('lim-timing').value) / 100,
    };
    try {
      await api('/api/limits', { method: 'POST', body: JSON.stringify(body) });
      state.limitsEditing = false;
      $('limits-pill').textContent = 'SAVED';
      $('limits-pill').className = 'pill ok';
      $('limits-status').textContent = 'Saved. Paper and Live now use these values.';
      toast('All trading limits saved');
    } catch (e) {
      $('limits-pill').textContent = 'CHECK VALUES';
      $('limits-pill').className = 'pill bad';
      $('limits-status').textContent = e.message;
      toast(e.message, 'err');
    }
  });

  $('btn-pause').addEventListener('click', async () => {
    const paused = state.snap && state.snap.runtime.paused;
    try {
      await api(`/api/engine/${paused ? 'resume' : 'pause'}`, { method: 'POST', body: '{}' });
      toast(paused ? 'Trading resumed' : 'Trading paused');
    } catch (e) { toast(e.message, 'err'); }
  });

  $('btn-panic').addEventListener('click', async () => {
    const n = (state.snap && state.snap.positions.length) || 0;
    if (!confirm(`Panic: sell all ${n} open position(s) into resting bids at whatever they fetch, then pause?\n\nThis realises losses immediately.`)) return;
    try {
      const r = await api('/api/engine/panic', { method: 'POST', body: '{}' });
      toast(`Panic complete: ${r.closed} sold, ${r.stranded} left to settle`, 'warn');
    } catch (e) { toast(e.message, 'err'); }
  });

  async function fireLossBypass(scope) {
    const snap = state.snap;
    const mode = (snap && snap.runtime.mode) || 'paper';
    const limit = Math.abs(Number((snap && snap.bankroll.max_daily_loss_usd) || 0));
    const held = scope === 'day' ? 'the rest of the UTC day' : 'one hour';
    // In Live the loss stop is the last automatic backstop on real money, so
    // overriding it should be a deliberate act rather than one stray click.
    // Paper costs nothing, so it fires straight away.
    if (mode === 'live' && !confirm(
      `Bypass the LIVE ${money(limit)} daily loss stop for ${held}?\n\n` +
      'The engine will keep opening real-money positions even though today\'s ' +
      'loss limit is already reached.' +
      (scope === 'day'
        ? '\n\nThis one does not expire on its own before 00:00 UTC.'
        : '\n\nIt re-engages automatically after 60 minutes.')
    )) return;
    try {
      const r = await api('/api/risk/daily-loss-bypass', {
        method: 'POST',
        body: JSON.stringify({ scope }),
      });
      toast(
        `${String(r.mode).toUpperCase()} daily loss stop bypassed for ` +
        (r.daily_loss_bypass_scope === 'day' ? 'the rest of the day' : '60 minutes'),
        'warn',
      );
    } catch (e) { toast(e.message, 'err'); }
  }

  $('btn-loss-bypass').addEventListener('click', () => fireLossBypass('hour'));
  $('btn-loss-bypass-day').addEventListener('click', () => fireLossBypass('day'));

  document.querySelectorAll('#range-tabs button').forEach((b) => {
    b.addEventListener('click', async () => {
      document.querySelectorAll('#range-tabs button').forEach((x) => x.classList.remove('active'));
      b.classList.add('active');
      state.range = b.dataset.range;
      state.hoverIndex = null;
      // Repaint from a direct fetch rather than waiting up to a second for the
      // reopened stream, so the button feels like it did something.
      try {
        const snap = await api(`/api/state?range=${state.range}&${scopeQuery()}`);
        state.snap = snap;
        render(snap);
      } catch { /* the stream will catch up */ }
      connect();
    });
  });

  document.querySelectorAll('#tabs .tab').forEach((b) => {
    b.addEventListener('click', () => {
      document.querySelectorAll('#tabs .tab').forEach((x) => x.classList.remove('active'));
      b.classList.add('active');
      state.tab = b.dataset.tab;
      document.querySelectorAll('.pane').forEach((p) => p.classList.remove('active'));
      $(`pane-${state.tab}`).classList.add('active');
      if (state.tab === 'monitor') refreshSignals();
      if (state.tab === 'winloss') refreshWinLoss();
      if (state.snap) drawChart(state.snap.equity);
    });
  });

  document.querySelectorAll('#pos-subtabs .subtab').forEach((b) => {
    b.addEventListener('click', () => {
      document.querySelectorAll('#pos-subtabs .subtab').forEach((x) => x.classList.remove('active'));
      b.classList.add('active');
      state.sub = b.dataset.sub;
      $('sub-positions').classList.toggle('hidden', state.sub !== 'positions');
      $('sub-activity').classList.toggle('hidden', state.sub !== 'activity');
      if (state.sub === 'activity') refreshActivity();
    });
  });

  document.querySelectorAll('#strategy-subtabs .subtab').forEach((b) => {
    b.addEventListener('click', () => {
      document.querySelectorAll('#strategy-subtabs .subtab').forEach((x) => x.classList.remove('active'));
      b.classList.add('active');
      $('st-ladders').classList.toggle('hidden', b.dataset.st !== 'ladders');
      $('st-rejections').classList.toggle('hidden', b.dataset.st !== 'rejections');
    });
  });

  document.querySelectorAll('#wl-filter .seg-btn').forEach((b) => {
    b.addEventListener('click', () => {
      document.querySelectorAll('#wl-filter .seg-btn').forEach((x) => x.classList.remove('active'));
      b.classList.add('active');
      state.wlFilter = b.dataset.wl;
      refreshWinLoss();
    });
  });

  document.querySelectorAll('#wl-range-tabs button').forEach((b) => {
    b.addEventListener('click', () => {
      document.querySelectorAll('#wl-range-tabs button')
        .forEach((x) => x.classList.remove('active'));
      b.classList.add('active');
      state.wlRange = b.dataset.wlRange;
      refreshWinLoss();
    });
  });

  $('asset-filter').addEventListener('change', (e) => {
    state.assetFilter = e.target.value;
    if (state.snap) renderPositions(state.snap);
  });

  $('btn-vault').addEventListener('click', () => {
    document.querySelector('#tabs .tab[data-tab="controls"]').click();
    $('vault-card').scrollIntoView({ behavior: 'smooth', block: 'center' });
  });

  $('btn-save-keys').addEventListener('click', async () => {
    const msg = $('vault-msg');
    const body = {
      private_key:    $('f-pk').value.trim(),
      wallet_address: $('f-addr').value.trim(),
      funder_address: $('f-funder').value.trim(),
      signature_type: Number($('f-sig').value),
      api_key:        $('f-key').value.trim(),
      api_secret:     $('f-secret').value.trim(),
      api_passphrase: $('f-pass').value.trim(),
      verify_online:  $('f-verify').checked,
    };
    msg.className = 'dim';
    msg.textContent = 'Validating…';
    try {
      const r = await api('/api/vault', { method: 'POST', body: JSON.stringify(body) });
      msg.className = 'msg-ok';
      msg.textContent =
        `Saved. Wallet ${r.address}` +
        (r.funder && r.funder !== r.address ? `, funder ${r.funder}` : '') +
        (r.derived_api_key ? `. Derived a new L2 API key.` : '') +
        (r.verified ? ' Verified against Polymarket.' : ' Stored without an online check.') +
        ' Restart the engine to use them.';
      // Clear the secrets from the DOM the moment they are persisted.
      ['f-pk', 'f-secret', 'f-pass', 'f-key'].forEach((i) => { $(i).value = ''; });
      state.vaultHydrated = false;
      toast('Credentials saved to .env');
    } catch (e) {
      msg.className = 'msg-err';
      msg.textContent = e.message;
      toast('Credentials rejected', 'err');
    }
  });

  $('btn-clear-keys').addEventListener('click', async () => {
    if (!confirm('Clear all stored credentials from .env?')) return;
    try {
      await api('/api/vault', { method: 'DELETE' });
      $('vault-msg').className = 'dim';
      $('vault-msg').textContent = 'Vault cleared.';
      state.vaultHydrated = false;
      toast('Vault cleared');
    } catch (e) { toast(e.message, 'err'); }
  });

  $('btn-refresh-clob').addEventListener('click', async () => {
    const ok = confirm(
      'Replace the saved CLOB credentials with a freshly derived CLOB V2 set?\n\n' +
      'This uses the private key already stored on this machine. It does not switch the bot to Live.'
    );
    if (!ok) return;
    const msg = $('vault-msg');
    msg.className = 'dim';
    msg.textContent = 'Deriving and verifying CLOB V2 credentials…';
    try {
      const r = await api('/api/vault/clob/refresh', { method: 'POST', body: '{}' });
      msg.className = 'msg-ok';
      msg.textContent =
        `CLOB V2 credentials verified for ${r.address}. Restart the engine before live trading.`;
      state.vaultHydrated = false;
      toast('CLOB V2 credentials saved');
    } catch (e) {
      msg.className = 'msg-err';
      msg.textContent = e.message;
      toast('Could not derive V2 credentials', 'err');
    }
  });

  $('btn-cl-save').addEventListener('click', async () => {
    const msg = $('cl-msg');
    msg.className = 'card-note dim';
    msg.textContent = 'Authenticating and discovering feeds…';
    try {
      const r = await api('/api/vault/chainlink', {
        method: 'POST',
        body: JSON.stringify({
          api_key: $('cl-key').value.trim(),
          api_secret: $('cl-secret').value.trim(),
        }),
      });
      msg.className = 'card-note msg-ok';
      msg.textContent = `Connected. ${r.feed_count} feed(s) entitled and mapped. ` +
        'Anchors and settlement now use the venue resolution source.';
      // Secrets out of the DOM as soon as they are stored.
      $('cl-key').value = '';
      $('cl-secret').value = '';
      toast(`Chainlink connected · ${r.feed_count} feeds`);
    } catch (e) {
      msg.className = 'card-note msg-err';
      msg.textContent = e.message;
      toast(e.message || 'Chainlink connection failed', 'err');
    }
  });

  $('btn-cl-clear').addEventListener('click', async () => {
    if (!confirm('Clear Chainlink Data Streams credentials?')) return;
    try {
      await api('/api/vault/chainlink', { method: 'DELETE' });
      $('cl-msg').className = 'card-note dim';
      $('cl-msg').textContent = 'Cleared — falling back to Coinbase/Kraken spot.';
      toast('Chainlink cleared');
    } catch (e) { toast(e.message, 'err'); }
  });

  $('btn-balance').addEventListener('click', async () => {
    const msg = $('vault-msg');
    msg.className = 'dim'; msg.textContent = 'Querying…';
    try {
      const r = await api('/api/balance');
      state.liveBalance = r;
      state.balanceFetchedAt = Date.now();
      renderWalletBalance(state.snap);
      if (r.ok) {
        msg.className = 'msg-ok';
        msg.textContent = `pUSD balance ${money(r.balance_pusd)} · ` +
          (r.approved
            ? 'trading approval active.'
            : `lowest exchange allowance ${money(r.allowance_pusd)}.`);
      } else {
        msg.className = 'msg-err';
        msg.textContent = r.error || 'Could not read balance.';
      }
    } catch (e) { msg.className = 'msg-err'; msg.textContent = e.message; }
  });

  const wrap = $('chart-wrap');
  const inspectChartAtPointer = (ev) => {
    const geom = state.geom;
    if (!geom) return;
    const rect = wrap.getBoundingClientRect();
    // Map pointer x back into viewBox units, then to the nearest settlement.
    const vx = ((ev.clientX - rect.left) / rect.width) * geom.W;
    const frac = geom.n === 0 ? 0 : (vx - geom.left) / (geom.right - geom.left);
    state.hoverIndex = Math.round(Math.max(0, Math.min(1, frac)) * geom.n);
    showCrosshair(state.hoverIndex);
  };
  wrap.addEventListener('pointermove', inspectChartAtPointer);
  wrap.addEventListener('pointerdown', (ev) => {
    wrap.focus({ preventScroll: true });
    inspectChartAtPointer(ev);
  });
  wrap.addEventListener('keydown', (ev) => {
    const geom = state.geom;
    if (!geom) return;
    let idx = state.hoverIndex ?? geom.points.length - 1;
    if (ev.key === 'ArrowLeft') idx -= 1;
    else if (ev.key === 'ArrowRight') idx += 1;
    else if (ev.key === 'Home') idx = 0;
    else if (ev.key === 'End') idx = geom.points.length - 1;
    else if (ev.key === 'Escape') {
      state.hoverIndex = null;
      hideCrosshair();
      return;
    } else return;
    ev.preventDefault();
    state.hoverIndex = Math.max(0, Math.min(geom.points.length - 1, idx));
    showCrosshair(state.hoverIndex);
  });
  wrap.addEventListener('pointerleave', (ev) => {
    if (ev.pointerType === 'mouse') {
      state.hoverIndex = null;
      hideCrosshair();
    }
  });
  wrap.addEventListener('blur', () => {
    state.hoverIndex = null;
    hideCrosshair();
  });

  window.addEventListener('resize', () => { if (state.snap) drawChart(state.snap.equity); });
}

/* ── boot ───────────────────────────────────────────────────────────────── */

wire();
connect();
refreshActivity();
setInterval(() => {
  if (state.tab === 'overview' && state.sub === 'activity') refreshActivity();
  if (state.tab === 'monitor') refreshSignals();
  if (state.tab === 'winloss') refreshWinLoss();
}, 3000);
