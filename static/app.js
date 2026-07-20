/**
 * ORDER FLOW BOT — Dashboard App
 * Real-time WebSocket dashboard. All user changes (symbol, interval, exchange)
 * immediately re-subscribe via WS → server pushes a fresh snapshot back.
 */

'use strict';

// ─────────────────────────────────────────────────────────────────────────────
// State
// ─────────────────────────────────────────────────────────────────────────────
const S = {
  symbol:       'BTCUSDT',
  interval:     '5m',
  exchange:     'spot',
  tradingMode:  'signal_only',
  orderType:    'MARKET',
  ws:           null,
  wsAlive:      false,
  lastPing:     0,
  tickCount:    0,
  lastSnapshot: null,
  fpData:       [],
  deltaData:    [],
  btEquityChart: null,
  btEquitySeries: null,
  _lastSig:     '',
};

// ─────────────────────────────────────────────────────────────────────────────
// Chart instances
// ─────────────────────────────────────────────────────────────────────────────
let candleChart, candleSeries, emaFastSeries, emaSlowSeries;
let deltaChart, deltaSeries, cdSeries;
let volChart, volSeries;
let srLines = [];

const CHART_OPTS = (height) => ({
  width:  0,
  height: height,
  layout: { background: { color: '#050a0e' }, textColor: '#3a7a9a' },
  grid:   { vertLines: { color: '#0d2035' }, horzLines: { color: '#0d2035' } },
  crosshair: { mode: 0 },
  timeScale: { borderColor: '#0d2035', timeVisible: true },
  rightPriceScale: { borderColor: '#0d2035' },
});

function initCharts() {
  const cWrap = document.getElementById('candle-chart');
  candleChart  = LightweightCharts.createChart(cWrap, CHART_OPTS(cWrap.clientHeight || 300));
  candleSeries = candleChart.addCandlestickSeries({
    upColor: '#00ff9d', downColor: '#ff2d55',
    borderUpColor: '#00ff9d', borderDownColor: '#ff2d55',
    wickUpColor: '#00ff9d', wickDownColor: '#ff2d55',
  });
  emaFastSeries = candleChart.addLineSeries({ color: '#00d4ff', lineWidth: 1, priceLineVisible: false });
  emaSlowSeries = candleChart.addLineSeries({ color: '#ffaa00', lineWidth: 1, priceLineVisible: false });

  const dWrap  = document.getElementById('delta-chart');
  deltaChart   = LightweightCharts.createChart(dWrap, CHART_OPTS(dWrap.clientHeight || 80));
  deltaSeries  = deltaChart.addHistogramSeries({ color: '#00ff9d', priceFormat: { type: 'volume' } });
  cdSeries     = deltaChart.addLineSeries({ color: '#4a9eff', lineWidth: 1, priceLineVisible: false });

  const vWrap  = document.getElementById('vol-chart');
  volChart     = LightweightCharts.createChart(vWrap, CHART_OPTS(vWrap.clientHeight || 70));
  volSeries    = volChart.addHistogramSeries({ color: '#0d2035', priceFormat: { type: 'volume' } });

  syncTimeScales();
}

function syncTimeScales() {
  [candleChart, deltaChart, volChart].forEach(master => {
    master.timeScale().subscribeVisibleLogicalRangeChange(range => {
      [candleChart, deltaChart, volChart].forEach(slave => {
        if (slave !== master) slave.timeScale().setVisibleLogicalRange(range);
      });
    });
  });
}

function resizeCharts() {
  const cWrap = document.getElementById('candle-chart');
  const dWrap = document.getElementById('delta-chart');
  const vWrap = document.getElementById('vol-chart');
  if (candleChart) candleChart.applyOptions({ width: cWrap.clientWidth, height: cWrap.clientHeight });
  if (deltaChart)  deltaChart.applyOptions({  width: dWrap.clientWidth, height: dWrap.clientHeight });
  if (volChart)    volChart.applyOptions({    width: vWrap.clientWidth, height: vWrap.clientHeight });
}

window.addEventListener('resize', () => {
  resizeCharts();
  renderFootprintCanvas();
});

// ─────────────────────────────────────────────────────────────────────────────
// Loading indicator
// ─────────────────────────────────────────────────────────────────────────────
function showLoading(on) {
  const el = document.getElementById('loading-indicator');
  if (el) el.style.display = on ? 'inline' : 'none';
}

// ─────────────────────────────────────────────────────────────────────────────
// Chart rendering
// ─────────────────────────────────────────────────────────────────────────────
function updateCharts(state) {
  const candles = state.candles || [];
  if (!candles.length) return;

  candleSeries.setData(candles.map(c => ({
    time: c.time, open: c.open, high: c.high, low: c.low, close: c.close,
  })));

  if (state.ema) {
    emaFastSeries.setData(state.ema.ema_fast || []);
    emaSlowSeries.setData(state.ema.ema_slow || []);
  }

  volSeries.setData(candles.map(c => ({
    time: c.time, value: c.volume,
    color: c.close >= c.open ? 'rgba(0,255,157,0.3)' : 'rgba(255,45,85,0.3)',
  })));

  srLines.forEach(l => { try { candleSeries.removePriceLine(l); } catch(e){} });
  srLines = [];

  const sr = state.sr || {};
  (sr.all_levels || []).forEach(lv => {
    const isRes = lv.kind === 'resistance';
    const line  = candleSeries.createPriceLine({
      price: lv.price,
      color: isRes ? 'rgba(255,45,85,0.6)' : 'rgba(0,255,157,0.6)',
      lineWidth: 1,
      lineStyle: LightweightCharts.LineStyle.Dashed,
      axisLabelVisible: true,
      title: (lv.tags || [lv.kind]).join(' '),
    });
    srLines.push(line);
  });

  updateSRList(sr);
  document.getElementById('candle-sym').textContent = S.symbol;
  document.getElementById('candle-ivl').textContent = S.interval;
  showLoading(false);
}

function updateDeltaChart(data) {
  if (!data || !data.length) return;
  deltaSeries.setData(data.map(d => ({
    time: d.time, value: d.delta,
    color: d.delta >= 0 ? 'rgba(0,255,157,0.7)' : 'rgba(255,45,85,0.7)',
  })));
  cdSeries.setData(data.map(d => ({ time: d.time, value: d.cd })));
  const last = data[data.length - 1];
  if (last) {
    document.getElementById('delta-cd-val').textContent =
      `CD: ${last.cd > 0 ? '+' : ''}${fmt(last.cd)}`;
  }
}

function updateSRList(sr) {
  const list = document.getElementById('sr-list');
  if (!list) return;
  list.innerHTML = (sr.all_levels || []).slice(0, 8).map(lv => {
    const isRes = lv.kind === 'resistance';
    return `<div class="sr-row">
      <div class="sr-dot ${isRes ? 'resist' : 'support'}"></div>
      <div class="sr-price">${fmtPrice(lv.price)}</div>
      <div class="sr-tags">${(lv.tags || []).join(' ')}</div>
      <div class="sr-strength">${lv.strength}</div>
    </div>`;
  }).join('');
}

function updateSidebar(state) {
  if (!state) return;
  const signal = state.signal  || {};
  const trend  = signal.trend  || state.trend || {};
  const fp     = signal.analytics || {};
  const ticker = state.ticker  || {};

  const price = state.price || 0;
  setEl('price-main', fmtPrice(price));

  const chg   = ticker.change_pct || 0;
  const chgEl = document.getElementById('price-chg');
  if (chgEl) {
    chgEl.textContent = `${chg > 0 ? '+' : ''}${chg.toFixed(2)}%`;
    chgEl.className   = chg >= 0 ? 'chg-pos' : 'chg-neg';
  }

  const sig  = signal.signal || 'NONE';
  const vDir = document.getElementById('verdict-dir');
  if (vDir) {
    vDir.textContent = sig;
    vDir.className   = sig;
  }

  if (sig !== 'NONE' && sig !== S._lastSig) {
    const vEl = document.getElementById('verdict');
    if (vEl) {
      vEl.classList.add(`signal-flash-${sig.toLowerCase()}`);
      setTimeout(() => vEl.classList.remove(`signal-flash-${sig.toLowerCase()}`), 1000);
    }
  }
  S._lastSig = sig;

  const condList = document.getElementById('conditions-list');
  if (condList) {
    const passed = signal.passed || [];
    const failed = signal.failed || [];
    condList.innerHTML = [
      ...passed.map(k => `<div class="cond pass">${k.replace(/_/g,' ')}</div>`),
      ...failed.map(k => `<div class="cond fail">${k.replace(/_/g,' ')}</div>`),
    ].join('');
  }

  setEl('trend-dir', trend.direction || '—', trendClass(trend.direction));
  setEl('ema-fast',  fmtPrice(trend.ema_fast || 0));
  setEl('ema-slow',  fmtPrice(trend.ema_slow || 0));

  const delta = fp.delta || 0;
  setEl('delta-val', `${delta > 0 ? '+' : ''}${fmt(delta)}`, delta >= 0 ? 'buy' : 'sell');
  updateDeltaBar(delta, fp.avg_volume || 1);
  setEl('stk-buy',  fp.stacked_buy  || 0, fp.stacked_buy  >= (window._cfg?.min_stacked || 3) ? 'buy'  : '');
  setEl('stk-sell', fp.stacked_sell || 0, fp.stacked_sell >= (window._cfg?.min_stacked || 3) ? 'sell' : '');
  setEl('buy-absorb',  fp.buy_absorption  ? '⚠ YES' : 'no', fp.buy_absorption  ? 'warn' : '');
  setEl('sell-absorb', fp.sell_absorption ? '⚠ YES' : 'no', fp.sell_absorption ? 'warn' : '');

  const fpLatest = (state.footprint_history || []).slice(-1)[0];
  if (fpLatest) setEl('poc-val', fmtPrice(fpLatest.poc || 0));
}

function updateDeltaBar(delta, avgVol) {
  const fill = document.getElementById('delta-bar-fill');
  if (!fill) return;
  const ratio = Math.min(Math.abs(delta) / (avgVol || 1), 1);
  const w     = ratio * 50;
  if (delta >= 0) {
    fill.style.left       = '50%';
    fill.style.width      = `${w}%`;
    fill.style.background = 'var(--buy)';
  } else {
    fill.style.left       = `${50 - w}%`;
    fill.style.width      = `${w}%`;
    fill.style.background = 'var(--sell)';
  }
}

function updateRiskStatus(data) {
  if (!data) return;
  const pnl = data.daily_pnl || 0;
  setEl('daily-pnl',    `${pnl >= 0 ? '+' : ''}$${fmt(pnl)}`, pnl >= 0 ? 'buy' : 'sell');
  setEl('trades-today', data.trade_count || 0);
  setEl('trades-remain', data.remaining_trades || 0, data.remaining_trades === 0 ? 'sell' : 'buy');
  setEl('risk-status-v', data.halted ? '⛔ HALTED' : '✓ ACTIVE', data.halted ? 'sell' : 'buy');
}

function updateStats(data) {
  if (!data) return;
  const lg = data.logger || {};
  setEl('stat-total', lg.total || 0);
  setEl('stat-wr',    `${lg.win_rate || 0}%`, (lg.win_rate || 0) >= 50 ? 'buy' : 'sell');
  const pnl = lg.total_pnl || 0;
  setEl('stat-pnl',  `${pnl >= 0 ? '+' : ''}$${fmt(pnl)}`, pnl >= 0 ? 'buy' : 'sell');
  setEl('stat-best',  `$${fmt(lg.best_trade  || 0)}`);
  setEl('stat-worst', `$${fmt(lg.worst_trade || 0)}`);

  setEl('sb-total', lg.total || 0);
  setEl('sb-wr',    `${lg.win_rate || 0}%`);
  const sbPnl = document.getElementById('sb-pnl');
  if (sbPnl) {
    const p = lg.total_pnl || 0;
    sbPnl.textContent = `${p >= 0 ? '+' : ''}$${fmt(p)}`;
    sbPnl.className   = `s-val ${p >= 0 ? 'green' : 'red'}`;
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Open positions
// ─────────────────────────────────────────────────────────────────────────────
function updatePositions(positions) {
  const list = document.getElementById('open-positions-list');
  if (!list) return;
  if (!positions || !positions.length) {
    list.innerHTML = '<div style="padding:8px 12px;color:var(--text-dim);font-size:11px">No open positions</div>';
    return;
  }
  list.innerHTML = positions.map(p => {
    const isLong = p.direction === 'BUY';
    return `<div class="card" style="margin:6px 8px;padding:8px">
      <div style="display:flex;justify-content:space-between">
        <span class="tag ${isLong ? 'buy' : 'sell'}">${p.direction}</span>
        <span style="font-size:11px;color:var(--text-dim)">${p.symbol}</span>
      </div>
      <div class="metric-row" style="padding:3px 0"><span class="label">Entry</span><span class="value">${fmtPrice(p.entry_price)}</span></div>
      <div class="metric-row" style="padding:3px 0"><span class="label">SL</span><span class="value sell">${fmtPrice(p.stop_loss)}</span></div>
      <div class="metric-row" style="padding:3px 0"><span class="label">TP1</span><span class="value buy">${fmtPrice(p.take_profit)}</span></div>
      <div class="metric-row" style="padding:3px 0"><span class="label">Size</span><span class="value">${p.remaining_size}</span></div>
    </div>`;
  }).join('');
}

// ─────────────────────────────────────────────────────────────────────────────
// Signal log
// ─────────────────────────────────────────────────────────────────────────────
function appendSignalLog(signal) {
  const log = document.getElementById('signal-log');
  if (!log) return;
  const ts  = new Date(signal.time * 1000).toLocaleTimeString();
  const sig = signal.signal || 'NONE';
  const cls = sig === 'BUY' ? 'buy' : sig === 'SELL' ? 'sell' : '';
  const row = document.createElement('div');
  row.style.cssText = 'padding:4px 10px;border-bottom:1px solid var(--border);font-size:11px';
  row.innerHTML = `<span style="color:var(--text-dim)">${ts}</span>
    <span class="tag ${cls}" style="margin:0 6px">${sig}</span>
    <span style="color:var(--text-dim)">${signal.symbol || S.symbol}</span>
    <span style="color:var(--text-dim);float:right">${(signal.passed||[]).length}/${((signal.passed||[]).length+(signal.failed||[]).length)} pass</span>`;
  log.prepend(row);
  while (log.children.length > 50) log.removeChild(log.lastChild);
}

// ─────────────────────────────────────────────────────────────────────────────
// Trades table
// ─────────────────────────────────────────────────────────────────────────────
function renderTradesTable(trades) {
  const tbody = document.getElementById('trades-tbody');
  if (!tbody) return;
  tbody.innerHTML = trades.map(t => {
    const pnl    = t.realized_pnl || 0;
    const isLong = t.direction === 'BUY';
    return `<tr>
      <td style="color:var(--text-dim);font-size:10px">${(t.id||'').slice(-6)}</td>
      <td>${t.symbol||'—'}</td>
      <td class="${isLong ? 'dir-buy' : 'dir-sell'}">${t.direction||'—'}</td>
      <td>${fmtPrice(t.entry_price||0)}</td>
      <td>${fmtPrice(t.exit_price||0)}</td>
      <td class="pnl-neg">${fmtPrice(t.stop_loss||0)}</td>
      <td class="pnl-pos">${fmtPrice(t.take_profit||0)}</td>
      <td class="${pnl>=0?'pnl-pos':'pnl-neg'}">${pnl>=0?'+':''}$${fmt(pnl)}</td>
      <td style="font-size:10px;color:var(--text-dim)">${t.exit_reason||'OPEN'}</td>
      <td style="color:var(--text-dim)">${t.duration_human||'—'}</td>
      <td><span class="tag ${t.status==='CLOSED'?(pnl>=0?'buy':'sell'):'neutral'}">${t.status||'OPEN'}</span></td>
    </tr>`;
  }).join('');
}

// ─────────────────────────────────────────────────────────────────────────────
// Footprint Canvas
// ─────────────────────────────────────────────────────────────────────────────
function renderFootprintCanvas() {
  const canvas = document.getElementById('fp-canvas');
  if (!canvas) return;
  const wrap   = document.getElementById('fp-canvas-wrap');
  if (!wrap)   return;
  canvas.width  = wrap.clientWidth;
  canvas.height = wrap.clientHeight;
  const ctx     = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);

  const data = S.fpData;
  if (!data || !data.length) {
    ctx.fillStyle = '#3a7a9a';
    ctx.font = '13px JetBrains Mono, monospace';
    ctx.fillText('No footprint data — subscribe to a symbol', 20, 40);
    return;
  }

  const N       = Math.min(data.length, 12);
  const candles = data.slice(-N);
  const colW    = Math.floor(canvas.width / N) - 2;
  const padding = 40;

  // Gather all price levels across candles for y-axis
  let allPrices = [];
  candles.forEach(c => {
    (c.levels || []).forEach(lv => allPrices.push(lv.price));
  });
  if (!allPrices.length) return;

  const minP = Math.min(...allPrices);
  const maxP = Math.max(...allPrices);
  const range = maxP - minP || 1;

  function priceToY(p) {
    return padding + (1 - (p - minP) / range) * (canvas.height - padding * 2);
  }

  // Max volume across all levels for color scaling
  const maxVol = Math.max(...candles.flatMap(c =>
    (c.levels || []).flatMap(lv => [lv.buy_vol, lv.sell_vol])
  ), 1);

  candles.forEach((candle, ci) => {
    const x    = ci * (colW + 2) + 1;
    const levels = candle.levels || [];

    levels.forEach(lv => {
      const y    = priceToY(lv.price);
      const rowH = Math.max(4, (canvas.height - padding * 2) / (levels.length || 1));

      // Buy side (left half)
      if (lv.buy_vol > 0) {
        const alpha = Math.min(lv.buy_vol / maxVol, 1);
        ctx.fillStyle = `rgba(0,255,157,${0.15 + alpha * 0.75})`;
        ctx.fillRect(x, y - rowH / 2, colW / 2 - 1, rowH - 1);
        ctx.fillStyle = '#b0e0ff';
        ctx.font = '9px JetBrains Mono, monospace';
        ctx.fillText(fmtK(lv.buy_vol), x + 2, y + 3);
      }

      // Sell side (right half)
      if (lv.sell_vol > 0) {
        const alpha = Math.min(lv.sell_vol / maxVol, 1);
        ctx.fillStyle = `rgba(255,45,85,${0.15 + alpha * 0.75})`;
        ctx.fillRect(x + colW / 2 + 1, y - rowH / 2, colW / 2 - 1, rowH - 1);
        ctx.fillStyle = '#b0e0ff';
        ctx.font = '9px JetBrains Mono, monospace';
        ctx.fillText(fmtK(lv.sell_vol), x + colW / 2 + 2, y + 3);
      }

      // Imbalance highlight
      if (lv.imbalance === 'buy')  { ctx.strokeStyle = '#00ff9d'; ctx.lineWidth = 1; ctx.strokeRect(x, y - rowH/2, colW/2-1, rowH-1); }
      if (lv.imbalance === 'sell') { ctx.strokeStyle = '#ff2d55'; ctx.lineWidth = 1; ctx.strokeRect(x+colW/2+1, y-rowH/2, colW/2-1, rowH-1); }
    });

    // POC line
    if (candle.poc) {
      const yp = priceToY(candle.poc);
      ctx.strokeStyle = '#ffaa00';
      ctx.lineWidth   = 1;
      ctx.setLineDash([3, 3]);
      ctx.beginPath(); ctx.moveTo(x, yp); ctx.lineTo(x + colW, yp); ctx.stroke();
      ctx.setLineDash([]);
    }

    // Candle time label
    if (candle.open_time) {
      ctx.fillStyle = '#3a7a9a';
      ctx.font      = '9px JetBrains Mono, monospace';
      const t = new Date(candle.open_time * 1000).toLocaleTimeString().slice(0, 5);
      ctx.fillText(t, x, canvas.height - 4);
    }
  });

  // Price axis
  const steps = 8;
  for (let i = 0; i <= steps; i++) {
    const p = minP + (i / steps) * range;
    const y = priceToY(p);
    ctx.strokeStyle = '#0d2035';
    ctx.lineWidth   = 0.5;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(canvas.width, y); ctx.stroke();
    ctx.fillStyle = '#3a7a9a';
    ctx.font      = '9px JetBrains Mono, monospace';
    ctx.fillText(fmtPrice(p), canvas.width - 60, y - 2);
  }
}

function updateFootprintDetail(candle) {
  if (!candle) return;
  const d = candle.delta || 0;
  setEl('fp-delta', `${d >= 0 ? '+' : ''}${fmt(d)}`, d >= 0 ? 'buy' : 'sell');
  setEl('fp-bvol',  fmtK(candle.buy_volume  || 0));
  setEl('fp-svol',  fmtK(candle.sell_volume || 0));
  setEl('fp-stk-b', candle.max_stacked_buy  || 0);
  setEl('fp-stk-s', candle.max_stacked_sell || 0);
  setEl('fp-ba',    candle.buy_absorption   ? '⚠ YES' : 'no', candle.buy_absorption  ? 'warn' : '');
  setEl('fp-sa',    candle.sell_absorption  ? '⚠ YES' : 'no', candle.sell_absorption ? 'warn' : '');
  setEl('fp-exh',   candle.exhaustion       ? '⚠ YES' : 'no', candle.exhaustion      ? 'warn' : '');
  setEl('fp-poc',   fmtPrice(candle.poc || 0));
  setEl('fp-vah',   fmtPrice(candle.vah || 0));
  setEl('fp-val',   fmtPrice(candle.val || 0));

  const levels = document.getElementById('fp-levels');
  if (levels) {
    const top = (candle.levels || []).slice(0, 20);
    levels.innerHTML = `<table style="width:100%"><thead><tr><th>Price</th><th style="color:var(--buy)">Buy</th><th style="color:var(--sell)">Sell</th><th>Δ</th></tr></thead><tbody>
      ${top.map(lv => {
        const d = (lv.buy_vol||0) - (lv.sell_vol||0);
        return `<tr>
          <td>${fmtPrice(lv.price)}</td>
          <td style="color:var(--buy)">${fmtK(lv.buy_vol||0)}</td>
          <td style="color:var(--sell)">${fmtK(lv.sell_vol||0)}</td>
          <td class="${d>=0?'pnl-pos':'pnl-neg'}">${d>=0?'+':''}${fmtK(d)}</td>
        </tr>`;
      }).join('')}
    </tbody></table>`;
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Scanner
// ─────────────────────────────────────────────────────────────────────────────
function renderScanner(coins) {
  const grid = document.getElementById('scanner-grid');
  if (!grid) return;
  if (!coins || !coins.length) {
    grid.innerHTML = '<div style="grid-column:1/-1;text-align:center;color:var(--text-dim);padding:40px">No data — press ⚡ SCAN</div>';
    return;
  }
  const maxScore = Math.max(...coins.map(c => c.score || 0), 1);
  grid.innerHTML = coins.map(c => {
    const pct   = c.change_pct || 0;
    const isPos = pct >= 0;
    const fillW = Math.min((c.score / maxScore) * 100, 100);
    return `<div class="scanner-card" onclick="selectSymbol('${c.symbol}')" style="--card-accent:${isPos?'var(--buy)':'var(--sell)'}">
      <div class="sym">${c.symbol.replace('USDT','')}</div>
      <div style="display:flex;justify-content:space-between;margin-top:4px">
        <span class="price-s">${fmtPrice(c.price||0)}</span>
        <span class="change ${isPos?'pnl-pos':'pnl-neg'}">${pct>0?'+':''}${pct.toFixed(2)}%</span>
      </div>
      <div style="font-size:9px;color:var(--text-dim);margin-top:2px">Vol $${fmtK(c.volume||0)}</div>
      <div class="score-bar"><div class="score-fill" style="width:${fillW}%"></div></div>
    </div>`;
  }).join('');
}

function selectSymbol(sym) {
  S.symbol = sym;
  const sel = document.getElementById('symbol-select');
  if (sel) sel.value = sym;
  subscribe();
  switchTab('dashboard');
}

// ─────────────────────────────────────────────────────────────────────────────
// Backtest
// ─────────────────────────────────────────────────────────────────────────────
let btChart = null, btSeries = null;

function initBacktestChart() {
  const wrap = document.getElementById('bt-equity-chart');
  if (!wrap || btChart) return;
  btChart  = LightweightCharts.createChart(wrap, { ...CHART_OPTS(wrap.clientHeight || 220), width: wrap.clientWidth, height: wrap.clientHeight || 220 });
  btSeries = btChart.addAreaSeries({ lineColor: '#00d4ff', topColor: 'rgba(0,212,255,0.2)', bottomColor: 'transparent', lineWidth: 2 });
}

async function runBacktest() {
  const sym  = document.getElementById('bt-symbol')?.value  || S.symbol;
  const ivl  = document.getElementById('bt-interval')?.value || '5m';
  const bal  = parseFloat(document.getElementById('bt-balance')?.value    || 10000);
  const comm = parseFloat(document.getElementById('bt-commission')?.value || 0.1) / 100;
  const slip = parseFloat(document.getElementById('bt-slippage')?.value   || 0.05) / 100;

  const btn  = document.getElementById('bt-run-btn');
  btn.disabled    = true;
  btn.textContent = '⏳ RUNNING…';

  document.getElementById('bt-progress-wrap').style.display = 'block';
  document.getElementById('bt-log').innerHTML = '';
  document.getElementById('bt-progress-bar').style.width = '0%';
  document.getElementById('bt-status').textContent = 'Starting…';
  ['btr-total','btr-wr','btr-pnl','btr-ret','btr-dd','btr-pf','btr-aw','btr-al','btr-wins','btr-losses','btr-bal'].forEach(id => setEl(id, '—'));

  initBacktestChart();
  btSeries.setData([]);
  document.getElementById('bt-trades-tbody').innerHTML = '';

  const equityData = [];
  try {
    const resp   = await fetch('/api/backtest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbol: sym, interval: ivl, balance: bal, commission: comm, slippage: slip }),
    });
    const reader  = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const lines = buf.split('\n');
      buf = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        try { handleBacktestEvent(JSON.parse(line.slice(6)), equityData, bal); } catch(e) {}
      }
    }
  } catch(err) {
    logBt(`Error: ${err.message}`, 'error');
  }
  btn.disabled    = false;
  btn.textContent = '▶ RUN BACKTEST';
}

function handleBacktestEvent(ev, equityData, initialBal) {
  if (ev.type === 'progress') {
    document.getElementById('bt-progress-bar').style.width = `${ev.pct}%`;
    document.getElementById('bt-status').textContent = `Candle ${ev.idx} — $${fmt(ev.balance)}`;
    equityData.push({ time: Date.now(), value: ev.balance });
    if (btSeries) btSeries.setData(equityData.map((pt, i) => ({ time: i + 1, value: pt.value })));

  } else if (ev.type === 'entry') {
    logBt(`↗ ${ev.signal} @ ${fmtPrice(ev.price)} SL:${fmtPrice(ev.sl)} TP:${fmtPrice(ev.tp)}`, 'entry');

  } else if (ev.type === 'trade') {
    const pnl = ev.pnl;
    logBt(`${ev.direction} ✓ → ${fmtPrice(ev.exit)} PnL:${pnl>=0?'+':''}$${fmt(pnl)} [${ev.reason}]`, pnl>=0?'trade-win':'trade-loss');
    equityData.push({ time: ev.time_close, value: ev.balance });
    if (btSeries) btSeries.setData(equityData.map((pt, i) => ({ time: i + 1, value: pt.value })));
    const tbody = document.getElementById('bt-trades-tbody');
    if (tbody) {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td class="${ev.direction==='BUY'?'dir-buy':'dir-sell'}">${ev.direction}</td>
        <td>${fmtPrice(ev.entry)}</td><td>${fmtPrice(ev.exit)}</td>
        <td class="${pnl>=0?'pnl-pos':'pnl-neg'}">${pnl>=0?'+':''}$${fmt(pnl)}</td>
        <td style="font-size:10px;color:var(--text-dim)">${ev.reason}</td>`;
      tbody.prepend(tr);
    }

  } else if (ev.type === 'done') {
    document.getElementById('bt-progress-bar').style.width = '100%';
    document.getElementById('bt-status').textContent = '✓ Complete';
    logBt(`═══ DONE  Trades:${ev.total_trades}  WR:${ev.win_rate}%  PnL:$${fmt(ev.total_pnl)}`, 'done');
    setEl('btr-total', ev.total_trades||0);
    setEl('btr-wr',    `${ev.win_rate||0}%`);
    const p = ev.total_pnl||0;
    setEl('btr-pnl', `${p>=0?'+':''}$${fmt(p)}`);
    const r = ev.total_return_pct||0;
    setEl('btr-ret', `${r>=0?'+':''}${r.toFixed(2)}%`);
    setEl('btr-dd',  `${ev.max_drawdown_pct||0}%`);
    setEl('btr-pf',  ev.profit_factor||0);
    setEl('btr-aw',  `$${fmt(ev.avg_win||0)}`);
    setEl('btr-al',  `$${fmt(ev.avg_loss||0)}`);
    setEl('btr-wins',   ev.wins||0);
    setEl('btr-losses', ev.losses||0);
    setEl('btr-bal',    `$${fmt(ev.final_balance||0)}`);
    if (ev.equity_curve && btSeries) {
      btSeries.setData(ev.equity_curve.map((pt, i) => ({ time: i + 1, value: pt.equity })));
    }
  }
}

function logBt(msg, cls = 'progress') {
  const log = document.getElementById('bt-log');
  if (!log) return;
  const div = document.createElement('div');
  div.className = `bt-log-entry ${cls}`;
  div.textContent = msg;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

// ─────────────────────────────────────────────────────────────────────────────
// WebSocket — real-time connection
// ─────────────────────────────────────────────────────────────────────────────
function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  S.ws = new WebSocket(`${proto}://${location.host}/ws`);

  S.ws.onopen = () => {
    S.wsAlive = true;
    document.getElementById('live-dot').classList.remove('disconnected');
    subscribe();
    startPing();
  };

  S.ws.onmessage = (ev) => {
    try { handleWs(JSON.parse(ev.data)); } catch(e) {}
  };

  S.ws.onclose = () => {
    S.wsAlive = false;
    document.getElementById('live-dot').classList.add('disconnected');
    setTimeout(connect, 3000);
  };

  S.ws.onerror = () => {
    S.wsAlive = false;
  };
}

function handleWs(msg) {
  switch (msg.type) {
    case 'config':
      applyConfig(msg);
      break;
    case 'snapshot':
      onSnapshot(msg.data);
      break;
    case 'footprint_update':
      if (msg.symbol === S.symbol) {
        S.fpData = msg.data || [];
        if (document.getElementById('tab-footprint')?.classList.contains('active')) {
          renderFootprintCanvas();
          const last = S.fpData.slice(-1)[0];
          if (last) updateFootprintDetail(last);
        }
      }
      break;
    case 'delta_update':
      if (msg.symbol === S.symbol) {
        S.deltaData = msg.data || [];
        updateDeltaChart(S.deltaData);
      }
      break;
    case 'tick':
      S.tickCount++;
      break;
    case 'risk_status':
      updateRiskStatus(msg.data);
      break;
    case 'scanner_update':
      renderScanner(msg.data);
      break;
    case 'scanner_scanning':
      document.getElementById('scan-btn')?.classList.toggle('scanning', msg.scanning);
      break;
    case 'exchange_changed':
      S.exchange = msg.exchange;
      updateExchangeUI();
      // Server already pushed a fresh snapshot via _push_snapshot, so just show loading briefly
      showLoading(true);
      break;
    case 'keys_status':
      applyKeysStatus(msg.data);
      break;
    case 'settings_updated':
      applySettingsUpdate(msg);
      break;
    case 'pong':
      document.getElementById('latency').textContent = `${Date.now() - S.lastPing}ms`;
      break;
  }
}

function onSnapshot(state) {
  if (!state || state.symbol !== S.symbol) return;
  S.lastSnapshot = state;
  showLoading(false);
  updateCharts(state);
  updateSidebar(state);
  fetchPositions();
  // Update footprint if that tab is visible
  if (state.footprint_history?.length) {
    S.fpData = state.footprint_history;
    if (document.getElementById('tab-footprint')?.classList.contains('active')) {
      renderFootprintCanvas();
      const last = S.fpData.slice(-1)[0];
      if (last) updateFootprintDetail(last);
    }
  }
}

/**
 * subscribe() — called whenever the user changes symbol, interval, or exchange.
 * Sends a subscribe message over the WebSocket. The server immediately
 * invalidates its cache and pushes a fresh snapshot back.
 */
function subscribe() {
  if (!S.ws || S.ws.readyState !== WebSocket.OPEN) return;
  showLoading(true);
  S.ws.send(JSON.stringify({
    type:     'subscribe',
    symbol:   S.symbol,
    interval: S.interval,
  }));
  document.getElementById('fp-sym').textContent = S.symbol;
}

function startPing() {
  setInterval(() => {
    if (S.ws && S.ws.readyState === WebSocket.OPEN) {
      S.lastPing = Date.now();
      S.ws.send(JSON.stringify({ type: 'ping', t: S.lastPing }));
    }
  }, 5000);
}

function applyConfig(cfg) {
  window._cfg = cfg;
  const symSel = document.getElementById('symbol-select');
  const btSym  = document.getElementById('bt-symbol');
  const ivlSel = document.getElementById('interval-select');

  if (symSel && cfg.symbols) {
    symSel.innerHTML = cfg.symbols.map(s =>
      `<option value="${s}" ${s===S.symbol?'selected':''}>${s}</option>`
    ).join('');
  }
  if (btSym && cfg.symbols) {
    btSym.innerHTML = cfg.symbols.map(s =>
      `<option value="${s}" ${s===S.symbol?'selected':''}>${s}</option>`
    ).join('');
  }
  if (ivlSel && cfg.intervals) {
    ivlSel.innerHTML = cfg.intervals.map(i =>
      `<option value="${i}" ${i===S.interval?'selected':''}>${i}</option>`
    ).join('');
  }
  S.exchange = cfg.exchange || 'spot';
  if (cfg.trading_mode) {
    S.tradingMode = cfg.trading_mode;
    applyModeUI(cfg.trading_mode);
  }
  updateExchangeUI();
}

function updateExchangeUI() {
  const tag = document.getElementById('exchange-tag');
  if (tag) tag.textContent = S.exchange.toUpperCase();
  const btn = document.getElementById('exchange-toggle');
  if (btn) btn.textContent = `⇄ ${S.exchange === 'spot' ? 'FUTURES' : 'SPOT'}`;
  const sel = document.getElementById('inp-exchange-type');
  if (sel) sel.value = S.exchange;
}

// ─────────────────────────────────────────────────────────────────────────────
// Fetch helpers
// ─────────────────────────────────────────────────────────────────────────────
async function fetchPositions() {
  try {
    const r = await fetch('/api/positions');
    const d = await r.json();
    updatePositions(d.positions || []);
  } catch(e) {}
}

async function fetchTrades() {
  try {
    const r = await fetch('/api/trades?limit=100');
    const d = await r.json();
    renderTradesTable(d.trades || []);
  } catch(e) {}
}

async function fetchStats() {
  try {
    const r = await fetch('/api/stats');
    const d = await r.json();
    updateStats(d);
    updateRiskStatus(d.daily || {});
  } catch(e) {}
}

async function fetchScanner() {
  try {
    const r = await fetch('/api/scanner');
    const d = await r.json();
    renderScanner(d.coins || []);
    if (d.last_scan) {
      const t = new Date(d.last_scan * 1000);
      document.getElementById('scan-time').textContent = `Last: ${t.toLocaleTimeString()}`;
    }
  } catch(e) {}
}

async function fetchSignals() {
  try {
    const r = await fetch(`/api/signals?symbol=${S.symbol}&limit=20`);
    const d = await r.json();
    (d.signals || []).forEach(sig => appendSignalLog(sig));
  } catch(e) {}
}

// ─────────────────────────────────────────────────────────────────────────────
// Settings — API Keys
// ─────────────────────────────────────────────────────────────────────────────
async function fetchKeyStatus() {
  try {
    const r = await fetch('/api/keys/status');
    const d = await r.json();
    applyKeysStatus(d);
  } catch(e) {}
}

function applyKeysStatus(data) {
  if (!data) return;
  const dot   = document.getElementById('key-status-dot');
  const badge = document.getElementById('key-badge');
  const topBadge = document.getElementById('key-status-dot');

  if (data.configured) {
    if (dot) { dot.style.color = 'var(--buy)'; dot.title = `API Key: ${data.key_masked}`; }
    if (badge) { badge.textContent = `✓ CONFIGURED (${data.key_masked})`; badge.className = 'key-badge ok'; }
  } else {
    if (dot) { dot.style.color = 'var(--sell)'; dot.title = 'API keys not configured'; }
    if (badge) { badge.textContent = 'NOT CONFIGURED'; badge.className = 'key-badge err'; }
  }

  if (data.trading_mode) {
    S.tradingMode = data.trading_mode;
    applyModeUI(data.trading_mode);
  }
}

async function saveApiKeys() {
  const key    = document.getElementById('inp-api-key')?.value.trim();
  const secret = document.getElementById('inp-api-secret')?.value.trim();
  const exType = document.getElementById('inp-exchange-type')?.value || 'spot';

  if (!key || !secret) {
    showFeedback('keys-feedback', '✕ Both API key and secret are required.', false);
    return;
  }

  try {
    const r = await fetch('/api/keys', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ api_key: key, api_secret: secret, trading_mode: S.tradingMode }),
    });
    const d = await r.json();
    if (d.ok) {
      showFeedback('keys-feedback', `✓ Keys saved. Key: ${d.key_masked}`, true);
      applyKeysStatus(d);
      // Switch exchange if changed
      if (exType !== S.exchange) {
        changeExchange(exType);
      }
    } else {
      showFeedback('keys-feedback', `✕ ${d.error}`, false);
    }
  } catch(e) {
    showFeedback('keys-feedback', `✕ Error: ${e.message}`, false);
  }
}

async function clearApiKeys() {
  if (!confirm('Clear saved API keys?')) return;
  try {
    await fetch('/api/keys', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ api_key: '', api_secret: '' }),
    });
    document.getElementById('inp-api-key').value    = '';
    document.getElementById('inp-api-secret').value = '';
    showFeedback('keys-feedback', '✓ Keys cleared.', true);
    fetchKeyStatus();
  } catch(e) {}
}

// ─────────────────────────────────────────────────────────────────────────────
// Settings — Trading Mode
// ─────────────────────────────────────────────────────────────────────────────
function setTradingMode(mode) {
  S.tradingMode = mode;
  applyModeUI(mode);
}

function applyModeUI(mode) {
  const mSignal  = document.getElementById('mode-signal');
  const mLive    = document.getElementById('mode-live');
  const mBadge   = document.getElementById('mode-badge');
  const mBadgeLg = document.getElementById('mode-badge-lg');
  const warning  = document.getElementById('live-warning');

  if (mSignal) mSignal.classList.toggle('active', mode === 'signal_only');
  if (mLive)   mLive.classList.toggle('active',   mode === 'live');
  if (warning) warning.style.display = mode === 'live' ? 'block' : 'none';

  const label = mode === 'live' ? 'LIVE' : 'SIGNAL';
  if (mBadge)   { mBadge.textContent = label; mBadge.className = `mode-badge ${mode === 'live' ? 'mode-live' : ''}`; }
  if (mBadgeLg) { mBadgeLg.textContent = mode === 'live' ? 'LIVE TRADING' : 'SIGNAL ONLY'; mBadgeLg.className = `mode-badge-lg ${mode === 'live' ? 'live' : ''}`; }
}

function setOrderType(ot) {
  S.orderType = ot;
  document.getElementById('ot-market')?.classList.toggle('active', ot === 'MARKET');
  document.getElementById('ot-limit')?.classList.toggle('active',  ot === 'LIMIT');
}

async function saveTradingMode() {
  const autoSlTp = document.getElementById('inp-auto-sl-tp')?.checked ?? true;
  try {
    const r = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ trading_mode: S.tradingMode, order_type: S.orderType, auto_sl_tp: autoSlTp }),
    });
    const d = await r.json();
    showFeedback('mode-feedback', d.ok ? '✓ Mode saved.' : `✕ ${d.error}`, !!d.ok);
  } catch(e) {
    showFeedback('mode-feedback', `✕ ${e.message}`, false);
  }
}

async function saveRiskParams() {
  const body = {
    risk_pct:         parseFloat(document.getElementById('inp-risk-pct')?.value  || 1),
    tp_ratio:         parseFloat(document.getElementById('inp-tp-ratio')?.value  || 2),
    max_daily_loss_pct: parseFloat(document.getElementById('inp-max-loss')?.value || 3),
    max_trades_day:   parseInt(document.getElementById('inp-max-trades')?.value   || 5),
  };
  try {
    const r = await fetch('/api/settings', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const d = await r.json();
    showFeedback('risk-feedback', d.ok ? '✓ Risk params saved.' : `✕ ${d.error}`, !!d.ok);
  } catch(e) {
    showFeedback('risk-feedback', `✕ ${e.message}`, false);
  }
}

async function saveSignalParams() {
  const sessions = [];
  if (document.getElementById('sess-london')?.checked)  sessions.push('london');
  if (document.getElementById('sess-newyork')?.checked) sessions.push('new_york');
  const body = {
    delta_threshold: parseFloat(document.getElementById('inp-delta-thresh')?.value || 500),
    min_stacked:     parseInt(document.getElementById('inp-min-stacked')?.value || 3),
    sessions,
  };
  try {
    const r = await fetch('/api/settings', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const d = await r.json();
    showFeedback('signal-feedback', d.ok ? '✓ Signal params saved.' : `✕ ${d.error}`, !!d.ok);
  } catch(e) {
    showFeedback('signal-feedback', `✕ ${e.message}`, false);
  }
}

function applySettingsUpdate(data) {
  // Populate settings form fields with server-confirmed values
  if (data.risk_pct         !== undefined) { const el = document.getElementById('inp-risk-pct');    if (el) el.value = data.risk_pct; }
  if (data.tp_ratio         !== undefined) { const el = document.getElementById('inp-tp-ratio');    if (el) el.value = data.tp_ratio; }
  if (data.max_daily_loss_pct !== undefined) { const el = document.getElementById('inp-max-loss'); if (el) el.value = data.max_daily_loss_pct; }
  if (data.max_trades_day   !== undefined) { const el = document.getElementById('inp-max-trades'); if (el) el.value = data.max_trades_day; }
  if (data.delta_threshold  !== undefined) { const el = document.getElementById('inp-delta-thresh'); if (el) el.value = data.delta_threshold; }
  if (data.min_stacked      !== undefined) { const el = document.getElementById('inp-min-stacked'); if (el) el.value = data.min_stacked; }
  if (data.trading_mode) { S.tradingMode = data.trading_mode; applyModeUI(data.trading_mode); }
  if (data.order_type)   setOrderType(data.order_type);
}

async function fetchAndApplySettings() {
  try {
    const r = await fetch('/api/settings');
    const d = await r.json();
    applySettingsUpdate(d);
    if (d.sessions) {
      const l = document.getElementById('sess-london');
      const n = document.getElementById('sess-newyork');
      if (l) l.checked = d.sessions.includes('london');
      if (n) n.checked = d.sessions.includes('new_york');
    }
    if (d.auto_sl_tp !== undefined) {
      const el = document.getElementById('inp-auto-sl-tp');
      if (el) el.checked = d.auto_sl_tp;
    }
  } catch(e) {}
}

function showFeedback(id, msg, ok) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = msg;
  el.className   = `settings-feedback ${ok ? 'ok' : 'err'}`;
  setTimeout(() => { el.textContent = ''; el.className = 'settings-feedback'; }, 4000);
}

// ─────────────────────────────────────────────────────────────────────────────
// Exchange change — via WebSocket for instant server-side cache invalidation
// and immediate snapshot push back to this client
// ─────────────────────────────────────────────────────────────────────────────
function changeExchange(ex) {
  if (!S.ws || S.ws.readyState !== WebSocket.OPEN) return;
  S.exchange = ex;
  updateExchangeUI();
  showLoading(true);
  S.ws.send(JSON.stringify({ type: 'set_exchange', exchange: ex }));
}

// ─────────────────────────────────────────────────────────────────────────────
// Tab switching
// ─────────────────────────────────────────────────────────────────────────────
function switchTab(name) {
  document.querySelectorAll('.tab-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.tab === name)
  );
  document.querySelectorAll('.tab-panel').forEach(p =>
    p.classList.toggle('active', p.id === `tab-${name}`)
  );

  if (name === 'footprint') {
    renderFootprintCanvas();
    const last = S.fpData.slice(-1)[0];
    if (last) updateFootprintDetail(last);
  }
  if (name === 'positions') {
    fetchTrades();
    fetchStats();
  }
  if (name === 'scanner')  fetchScanner();
  if (name === 'settings') {
    fetchKeyStatus();
    fetchAndApplySettings();
  }
  if (name === 'backtest') {
    setTimeout(() => {
      if (btChart) btChart.applyOptions({ width: document.getElementById('bt-equity-chart').clientWidth });
    }, 50);
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Clock + tick rate
// ─────────────────────────────────────────────────────────────────────────────
function updateClock() {
  const el = document.getElementById('clock');
  if (el) el.textContent = new Date().toUTCString().slice(17, 25) + ' UTC';
}

function startTickRate() {
  let prev = 0;
  setInterval(() => {
    const rate = S.tickCount - prev;
    prev = S.tickCount;
    const el = document.getElementById('tick-pill');
    if (el) el.textContent = `${rate} t/s`;
  }, 1000);
}

// ─────────────────────────────────────────────────────────────────────────────
// Utility
// ─────────────────────────────────────────────────────────────────────────────
function setEl(id, val, cls) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = val;
  if (cls !== undefined) el.className = `value ${cls}`;
  el.classList.add('updated');
  setTimeout(() => el.classList.remove('updated'), 900);
}

function fmtPrice(n) {
  if (!n && n !== 0) return '—';
  if (n >= 10000) return n.toFixed(0);
  if (n >= 100)   return n.toFixed(2);
  if (n >= 1)     return n.toFixed(4);
  return n.toFixed(6);
}

function fmt(n) {
  if (n === null || n === undefined) return '—';
  return Math.abs(n) >= 1e6 ? (n/1e6).toFixed(2)+'M' :
         Math.abs(n) >= 1e3 ? (n/1e3).toFixed(1)+'K' :
         n.toFixed(2);
}

function fmtK(n) {
  if (!n) return '0';
  return Math.abs(n) >= 1e6 ? (n/1e6).toFixed(1)+'M' :
         Math.abs(n) >= 1e3 ? (n/1e3).toFixed(0)+'K' :
         Math.round(n).toString();
}

function trendClass(t) {
  if (t === 'BULLISH') return 'buy';
  if (t === 'BEARISH') return 'sell';
  return 'neutral';
}

// ─────────────────────────────────────────────────────────────────────────────
// Event listeners
// ─────────────────────────────────────────────────────────────────────────────
function initEvents() {
  // Tabs
  document.querySelectorAll('.tab-btn').forEach(btn =>
    btn.addEventListener('click', () => switchTab(btn.dataset.tab))
  );

  // Symbol change → immediate WS subscribe → server pushes fresh snapshot
  document.getElementById('symbol-select')?.addEventListener('change', e => {
    S.symbol = e.target.value;
    document.getElementById('fp-sym').textContent  = S.symbol;
    document.getElementById('bt-symbol').value     = S.symbol;
    subscribe();
  });

  // Interval change → immediate WS subscribe → server pushes fresh snapshot
  document.getElementById('interval-select')?.addEventListener('change', e => {
    S.interval = e.target.value;
    subscribe();
  });

  // Exchange toggle → send via WS → server clears cache + pushes snapshot immediately
  document.getElementById('exchange-toggle')?.addEventListener('click', () => {
    const next = S.exchange === 'spot' ? 'futures' : 'spot';
    changeExchange(next);
  });

  // Scanner
  document.getElementById('scan-btn')?.addEventListener('click', async () => {
    try { await fetch('/api/scanner/scan', { method: 'POST' }); } catch(e) {}
  });

  // Backtest
  document.getElementById('bt-run-btn')?.addEventListener('click', runBacktest);

  // Settings — API keys
  document.getElementById('btn-save-keys')?.addEventListener('click', saveApiKeys);
  document.getElementById('btn-clear-keys')?.addEventListener('click', clearApiKeys);

  // Password visibility toggles
  document.getElementById('toggle-key-vis')?.addEventListener('click', () => {
    const el = document.getElementById('inp-api-key');
    if (el) el.type = el.type === 'password' ? 'text' : 'password';
  });
  document.getElementById('toggle-secret-vis')?.addEventListener('click', () => {
    const el = document.getElementById('inp-api-secret');
    if (el) el.type = el.type === 'password' ? 'text' : 'password';
  });

  // Settings — Mode
  document.getElementById('btn-save-mode')?.addEventListener('click', saveTradingMode);

  // Settings — Risk
  document.getElementById('btn-save-risk')?.addEventListener('click', saveRiskParams);

  // Settings — Signal
  document.getElementById('btn-save-signal')?.addEventListener('click', saveSignalParams);
}

// ─────────────────────────────────────────────────────────────────────────────
// Boot
// ─────────────────────────────────────────────────────────────────────────────
function boot() {
  initCharts();
  initEvents();
  connect();          // Real-time WebSocket — all data flows through here
  updateClock();
  startTickRate();
  setInterval(updateClock, 1000);
  setInterval(fetchStats, 15000);
  setInterval(fetchPositions, 8000);

  fetchScanner();
  fetchSignals();
  fetchStats();
  fetchKeyStatus();
}

document.addEventListener('DOMContentLoaded', boot);
