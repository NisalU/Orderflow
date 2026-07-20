/**
 * ORDER FLOW BOT — Dashboard App
 * Hacker terminal UI with live WebSocket data, lightweight-charts,
 * footprint canvas renderer, and streaming backtest.
 */

'use strict';

// ─────────────────────────────────────────────────────────────────────────────
// State
// ─────────────────────────────────────────────────────────────────────────────
const S = {
  symbol:   'BTCUSDT',
  interval: '5m',
  exchange: 'spot',
  ws:       null,
  wsAlive:  false,
  lastPing: 0,
  tickCount: 0,
  tickTimer: null,
  lastSnapshot: null,
  fpData:   [],          // footprint candles
  deltaData: [],
  btEquityChart: null,
  btEquitySeries: null,
};

// ─────────────────────────────────────────────────────────────────────────────
// lightweight-charts setup
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
  // ── Candle chart ──────────────────────────────────────────────────────────
  const cWrap = document.getElementById('candle-chart');
  candleChart = LightweightCharts.createChart(cWrap, CHART_OPTS(cWrap.clientHeight || 300));
  candleSeries = candleChart.addCandlestickSeries({
    upColor:   '#00ff9d', downColor: '#ff2d55',
    borderUpColor: '#00ff9d', borderDownColor: '#ff2d55',
    wickUpColor: '#00ff9d', wickDownColor: '#ff2d55',
  });
  emaFastSeries = candleChart.addLineSeries({ color: '#00d4ff', lineWidth: 1, priceLineVisible: false });
  emaSlowSeries = candleChart.addLineSeries({ color: '#ffaa00', lineWidth: 1, priceLineVisible: false });

  // ── Delta chart ───────────────────────────────────────────────────────────
  const dWrap = document.getElementById('delta-chart');
  deltaChart  = LightweightCharts.createChart(dWrap, CHART_OPTS(dWrap.clientHeight || 80));
  deltaSeries = deltaChart.addHistogramSeries({
    color: '#00ff9d', priceFormat: { type: 'volume' },
  });
  cdSeries = deltaChart.addLineSeries({ color: '#4a9eff', lineWidth: 1, priceLineVisible: false });

  // ── Volume chart ──────────────────────────────────────────────────────────
  const vWrap = document.getElementById('vol-chart');
  volChart  = LightweightCharts.createChart(vWrap, CHART_OPTS(vWrap.clientHeight || 70));
  volSeries = volChart.addHistogramSeries({
    color: '#0d2035', priceFormat: { type: 'volume' },
  });

  syncTimeScales();
}

function syncTimeScales() {
  // Sync crosshair + scroll across all charts
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
// Data rendering
// ─────────────────────────────────────────────────────────────────────────────

function updateCharts(state) {
  if (!state) return;
  const candles = state.candles || [];
  if (!candles.length) return;

  // Candle series
  candleSeries.setData(candles.map(c => ({
    time: c.time, open: c.open, high: c.high, low: c.low, close: c.close
  })));

  // EMA overlays
  if (state.ema) {
    emaFastSeries.setData(state.ema.ema_fast || []);
    emaSlowSeries.setData(state.ema.ema_slow || []);
  }

  // Volume
  volSeries.setData(candles.map(c => ({
    time: c.time,
    value: c.volume,
    color: c.close >= c.open ? 'rgba(0,255,157,0.3)' : 'rgba(255,45,85,0.3)',
  })));

  // Remove old S/R lines
  srLines.forEach(l => { try { candleChart.removePriceLine(l); } catch(e){} });
  srLines = [];

  // Draw S/R levels
  const sr = state.sr || {};
  (sr.all_levels || []).forEach(lv => {
    const isRes = lv.kind === 'resistance';
    const line = candleSeries.createPriceLine({
      price:      lv.price,
      color:      isRes ? 'rgba(255,45,85,0.6)' : 'rgba(0,255,157,0.6)',
      lineWidth:  1,
      lineStyle:  LightweightCharts.LineStyle.Dashed,
      axisLabelVisible: true,
      title:      (lv.tags || [lv.kind]).join(' '),
    });
    srLines.push(line);
  });

  updateSRList(sr);
  document.getElementById('candle-sym').textContent = S.symbol;
  document.getElementById('candle-ivl').textContent = S.interval;
}

function updateDeltaChart(data) {
  if (!data || !data.length) return;
  deltaSeries.setData(data.map(d => ({
    time:  d.time,
    value: d.delta,
    color: d.delta >= 0 ? 'rgba(0,255,157,0.7)' : 'rgba(255,45,85,0.7)',
  })));
  cdSeries.setData(data.map(d => ({
    time:  d.time,
    value: d.cd,
  })));
  const last = data[data.length - 1];
  if (last) {
    document.getElementById('delta-cd-val').textContent =
      `CD: ${last.cd > 0 ? '+' : ''}${fmt(last.cd)}`;
  }
}

function updateSidebar(state) {
  if (!state) return;
  const signal = state.signal || {};
  const trend  = signal.trend  || state.trend || {};
  const fp     = signal.analytics || {};
  const ticker = state.ticker  || {};

  // Price
  const price = state.price || 0;
  setEl('price-main', fmtPrice(price));

  const chg = ticker.change_pct || 0;
  const chgEl = document.getElementById('price-chg');
  chgEl.textContent = `${chg > 0 ? '+' : ''}${chg.toFixed(2)}%`;
  chgEl.className   = chg >= 0 ? 'chg-pos' : 'chg-neg';

  // Verdict
  const sig = signal.signal || 'NONE';
  const vDir = document.getElementById('verdict-dir');
  vDir.textContent = sig;
  vDir.className   = sig;

  // Flash on new signal
  if (sig !== 'NONE' && sig !== (S._lastSig || '')) {
    document.getElementById('verdict').classList.add(`signal-flash-${sig.toLowerCase()}`);
    setTimeout(() => document.getElementById('verdict').classList.remove(`signal-flash-${sig.toLowerCase()}`), 1000);
  }
  S._lastSig = sig;

  // Conditions
  const condList = document.getElementById('conditions-list');
  const passed = signal.passed || [];
  const failed = signal.failed || [];
  condList.innerHTML = [...passed.map(k => `<div class="cond pass">${k.replace(/_/g,' ')}</div>`),
                         ...failed.map(k => `<div class="cond fail">${k.replace(/_/g,' ')}</div>`)].join('');

  // Trend
  setEl('trend-dir', trend.direction || '—', trendClass(trend.direction));
  setEl('ema-fast', fmtPrice(trend.ema_fast || 0));
  setEl('ema-slow', fmtPrice(trend.ema_slow || 0));

  // Delta / footprint
  const delta = fp.delta || 0;
  setEl('delta-val', `${delta > 0 ? '+' : ''}${fmt(delta)}`, delta >= 0 ? 'buy' : 'sell');
  updateDeltaBar(delta, fp.avg_volume || 1);
  setEl('stk-buy',  fp.stacked_buy  || 0, fp.stacked_buy  >= (window._cfg?.min_stacked || 3) ? 'buy'  : '');
  setEl('stk-sell', fp.stacked_sell || 0, fp.stacked_sell >= (window._cfg?.min_stacked || 3) ? 'sell' : '');
  setEl('buy-absorb',  fp.buy_absorption  ? '⚠ YES' : 'no', fp.buy_absorption  ? 'warn' : '');
  setEl('sell-absorb', fp.sell_absorption ? '⚠ YES' : 'no', fp.sell_absorption ? 'warn' : '');

  // POC from latest FP candle
  const fpLatest = (state.footprint_history || []).slice(-1)[0];
  if (fpLatest) setEl('poc-val', fmtPrice(fpLatest.poc || 0));
}

function updateDeltaBar(delta, avgVol) {
  const bg   = document.getElementById('delta-bar-bg');
  const fill = document.getElementById('delta-bar-fill');
  if (!bg || !fill) return;
  const cap  = Math.max(Math.abs(delta) * 2, avgVol * 100, 1);
  const pct  = Math.min(Math.abs(delta) / cap * 100, 100);
  if (delta >= 0) {
    fill.style.left       = '50%';
    fill.style.width      = `${pct / 2}%`;
    fill.style.background = 'var(--buy)';
    fill.style.boxShadow  = '0 0 6px var(--buy)';
  } else {
    fill.style.left       = `${50 - pct / 2}%`;
    fill.style.width      = `${pct / 2}%`;
    fill.style.background = 'var(--sell)';
    fill.style.boxShadow  = '0 0 6px var(--sell)';
  }
}

function updateSRList(sr) {
  const list = document.getElementById('sr-list');
  if (!list || !sr) return;
  const levels = sr.all_levels || [];
  list.innerHTML = levels.map(lv => {
    const isRes = lv.kind === 'resistance';
    return `<div class="sr-level-row">
      <div class="sr-dot ${isRes ? 'resist' : 'support'}"></div>
      <div class="sr-price">${fmtPrice(lv.price)}</div>
      <div class="sr-tags">${(lv.tags || []).join(' ')}</div>
      <div class="sr-strength">${lv.strength}</div>
    </div>`;
  }).join('');
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
  setEl('stat-best',  `$${fmt(lg.best_trade || 0)}`);
  setEl('stat-worst', `$${fmt(lg.worst_trade || 0)}`);

  // Stats bar (positions tab)
  setEl('sb-total', lg.total || 0);
  setEl('sb-wr',    `${lg.win_rate || 0}%`);
  const p = lg.total_pnl || 0;
  const sbPnl = document.getElementById('sb-pnl');
  if (sbPnl) { sbPnl.textContent = `${p>=0?'+':''}$${fmt(p)}`; sbPnl.className = `s-val ${p>=0?'green':'red'}`; }
}

// ─────────────────────────────────────────────────────────────────────────────
// Open positions list
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
    const pnl = ((p.exit_price || 0) - p.entry_price) * (isLong ? 1 : -1) * p.remaining_size;
    return `<div class="card" style="margin:6px 8px;padding:8px">
      <div style="display:flex;justify-content:space-between">
        <span class="tag ${isLong ? 'buy' : 'sell'}">${p.direction}</span>
        <span style="font-size:11px;color:var(--text-dim)">${p.symbol}</span>
      </div>
      <div class="metric-row" style="padding:3px 0">
        <span class="label">Entry</span><span class="value">${fmtPrice(p.entry_price)}</span>
      </div>
      <div class="metric-row" style="padding:3px 0">
        <span class="label">SL</span><span class="value sell">${fmtPrice(p.stop_loss)}</span>
      </div>
      <div class="metric-row" style="padding:3px 0">
        <span class="label">TP1</span><span class="value buy">${fmtPrice(p.take_profit)}</span>
      </div>
      <div class="metric-row" style="padding:3px 0">
        <span class="label">Size</span><span class="value">${p.remaining_size}</span>
      </div>
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
  // Trim to 50 entries
  while (log.children.length > 50) log.removeChild(log.lastChild);
}

// ─────────────────────────────────────────────────────────────────────────────
// Trades table
// ─────────────────────────────────────────────────────────────────────────────

function renderTradesTable(trades) {
  const tbody = document.getElementById('trades-tbody');
  if (!tbody) return;
  tbody.innerHTML = trades.map(t => {
    const pnl   = t.realized_pnl || 0;
    const isLong = t.direction === 'BUY';
    const dur   = t.duration_human || '—';
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
      <td style="color:var(--text-dim)">${dur}</td>
      <td><span class="tag ${t.status==='CLOSED'?(pnl>=0?'buy':'sell'):'neutral'}">${t.status||'OPEN'}</span></td>
    </tr>`;
  }).join('');
}

// ─────────────────────────────────────────────────────────────────────────────
// Footprint Canvas Renderer
// ─────────────────────────────────────────────────────────────────────────────

function renderFootprintCanvas() {
  const canvas = document.getElementById('fp-canvas');
  if (!canvas) return;
  const wrap   = document.getElementById('fp-canvas-wrap');
  if (!wrap) return;
  canvas.width  = wrap.clientWidth;
  canvas.height = wrap.clientHeight;
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);

  const data = S.fpData;
  if (!data || !data.length) {
    ctx.fillStyle = 'rgba(58,122,154,0.5)';
    ctx.font = '13px JetBrains Mono';
    ctx.textAlign = 'center';
    ctx.fillText('Waiting for footprint data…', canvas.width / 2, canvas.height / 2);
    return;
  }

  const candles  = data.slice(-30);  // show last 30 candles
  const nCandles = candles.length;
  const canvasW  = canvas.width;
  const canvasH  = canvas.height;
  const colW     = Math.max(Math.floor((canvasW - 60) / nCandles), 60);
  const levelH   = 13;
  const priceArea = canvasH - 30;

  // Global price range
  let minP = Infinity, maxP = -Infinity;
  for (const c of candles) {
    for (const lv of (c.lvls || [])) {
      minP = Math.min(minP, lv.p);
      maxP = Math.max(maxP, lv.p);
    }
    minP = Math.min(minP, c.l);
    maxP = Math.max(maxP, c.h);
  }
  if (minP === Infinity) return;
  const priceRange = maxP - minP || 1;

  // Max volume for color scaling
  let maxVol = 0;
  for (const c of candles) {
    for (const lv of (c.lvls || [])) {
      maxVol = Math.max(maxVol, lv.b, lv.s);
    }
  }

  // Draw candles
  candles.forEach((c, i) => {
    const x = 60 + i * colW;
    const levels = c.lvls || [];

    // OHLC bar (mini)
    const yHigh  = priceArea - ((c.h - minP) / priceRange) * priceArea;
    const yLow   = priceArea - ((c.l - minP) / priceRange) * priceArea;
    const yOpen  = priceArea - ((c.o - minP) / priceRange) * priceArea;
    const yClose = priceArea - ((c.c - minP) / priceRange) * priceArea;
    const bullish = c.c >= c.o;
    ctx.strokeStyle = bullish ? '#00ff9d' : '#ff2d55';
    ctx.lineWidth   = 1;
    ctx.beginPath();
    ctx.moveTo(x + colW / 2, yHigh);
    ctx.lineTo(x + colW / 2, yLow);
    ctx.stroke();

    // Level footprint
    levels.forEach(lv => {
      const yLv = priceArea - ((lv.p - minP) / priceRange) * priceArea;
      if (yLv < 0 || yLv > priceArea) return;

      // Background
      let bg = 'transparent';
      if (lv.im === 'buy')  bg = 'rgba(0,255,157,0.15)';
      if (lv.im === 'sell') bg = 'rgba(255,45,85,0.15)';
      if (lv.p === c.poc)   bg = 'rgba(255,170,0,0.2)';
      if (bg !== 'transparent') {
        ctx.fillStyle = bg;
        ctx.fillRect(x + 2, yLv - levelH / 2, colW - 4, levelH);
      }

      // Buy vol bar (left half)
      if (maxVol > 0 && lv.b > 0) {
        const bw = (lv.b / maxVol) * (colW / 2 - 6);
        ctx.fillStyle = 'rgba(0,255,157,0.6)';
        ctx.fillRect(x + 2, yLv - 2, bw, 4);
      }

      // Sell vol bar (right half)
      if (maxVol > 0 && lv.s > 0) {
        const sw = (lv.s / maxVol) * (colW / 2 - 6);
        ctx.fillStyle = 'rgba(255,45,85,0.6)';
        ctx.fillRect(x + colW / 2, yLv - 2, sw, 4);
      }

      // Text (only if enough space)
      if (colW > 70) {
        ctx.fillStyle = lv.b >= lv.s ? '#00ff9d' : '#ff2d55';
        ctx.font = '9px JetBrains Mono';
        ctx.textAlign = 'left';
        ctx.fillText(fmtK(lv.b), x + 2, yLv + 3);
        ctx.textAlign = 'right';
        ctx.fillText(fmtK(lv.s), x + colW - 2, yLv + 3);
      }
    });

    // Candle body
    ctx.fillStyle = bullish ? 'rgba(0,255,157,0.3)' : 'rgba(255,45,85,0.3)';
    const bodyTop = Math.min(yOpen, yClose);
    const bodyH   = Math.abs(yOpen - yClose) || 1;
    ctx.fillRect(x + Math.floor(colW * 0.2), bodyTop, Math.floor(colW * 0.6), bodyH);

    // Delta label (bottom)
    ctx.fillStyle = c.delta >= 0 ? '#00ff9d' : '#ff2d55';
    ctx.font = '9px JetBrains Mono';
    ctx.textAlign = 'center';
    ctx.fillText(`Δ${fmtK(c.delta)}`, x + colW / 2, canvasH - 14);

    // Time label
    ctx.fillStyle = '#3a7a9a';
    ctx.font = '8px JetBrains Mono';
    const dt = new Date(c.ot * 1000);
    ctx.fillText(`${dt.getHours().toString().padStart(2,'0')}:${dt.getMinutes().toString().padStart(2,'0')}`,
                 x + colW / 2, canvasH - 4);
  });

  // Price axis (left)
  ctx.fillStyle = '#3a7a9a';
  ctx.font = '9px JetBrains Mono';
  ctx.textAlign = 'right';
  const ticks = 8;
  for (let i = 0; i <= ticks; i++) {
    const p = minP + (maxP - minP) * (i / ticks);
    const y = priceArea - ((p - minP) / priceRange) * priceArea;
    ctx.fillText(fmtPrice(p), 55, y + 3);
    ctx.strokeStyle = '#0d2035';
    ctx.lineWidth = 0.5;
    ctx.beginPath();
    ctx.moveTo(58, y);
    ctx.lineTo(canvasW, y);
    ctx.stroke();
  }

  // Update FP detail sidebar from selected candle
  updateFpDetail(data[data.length - 1]);
}

function updateFpDetail(c) {
  if (!c) return;
  const detail = document.getElementById('fp-detail');
  if (detail) {
    detail.innerHTML = `
      <div class="metric-row"><span class="label">Delta</span><span class="value ${c.delta>=0?'buy':'sell'}">${c.delta>=0?'+':''}${fmt(c.delta)}</span></div>
      <div class="metric-row"><span class="label">Buy Vol</span><span class="value buy">${fmt(c.bvol)}</span></div>
      <div class="metric-row"><span class="label">Sell Vol</span><span class="value sell">${fmt(c.svol)}</span></div>
      <div class="metric-row"><span class="label">Stk Buy</span><span class="value">${c.msb}</span></div>
      <div class="metric-row"><span class="label">Stk Sell</span><span class="value">${c.mss}</span></div>
      <div class="metric-row"><span class="label">Buy Absorb</span><span class="value ${c.ba?'warn':''}">${c.ba?'YES':'no'}</span></div>
      <div class="metric-row"><span class="label">Sell Absorb</span><span class="value ${c.sa?'warn':''}">${c.sa?'YES':'no'}</span></div>
      <div class="metric-row"><span class="label">Exhaustion</span><span class="value ${c.ex?'warn':''}">${c.ex?'YES':'no'}</span></div>
      <div class="metric-row"><span class="label">POC</span><span class="value">${fmtPrice(c.poc)}</span></div>
      <div class="metric-row"><span class="label">VAH</span><span class="value">${fmtPrice(c.vah)}</span></div>
      <div class="metric-row"><span class="label">VAL</span><span class="value">${fmtPrice(c.val)}</span></div>
    `;
  }
  const levels = document.getElementById('fp-levels');
  if (levels && c.lvls) {
    levels.innerHTML = c.lvls.slice().reverse().map(lv => `
      <div class="fp-level ${lv.im?`im-${lv.im}`:''} ${lv.p===c.poc?'poc':''}">
        <span class="buy-v">${fmtK(lv.b)}</span>
        <span style="color:var(--text-dim);font-size:8px">${fmtPrice(lv.p)}</span>
        <span class="sell-v">${fmtK(lv.s)}</span>
      </div>
    `).join('');
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
    const pct    = c.change_pct || 0;
    const isPos  = pct >= 0;
    const fillW  = Math.min((c.score / maxScore) * 100, 100);
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
  document.getElementById('symbol-select').value = sym;
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
  btChart = LightweightCharts.createChart(wrap, {
    ...CHART_OPTS(wrap.clientHeight || 220),
    width: wrap.clientWidth,
    height: wrap.clientHeight || 220,
  });
  btSeries = btChart.addAreaSeries({
    lineColor:  '#00d4ff',
    topColor:   'rgba(0,212,255,0.2)',
    bottomColor: 'transparent',
    lineWidth: 2,
  });
}

async function runBacktest() {
  const sym  = document.getElementById('bt-symbol')?.value || S.symbol;
  const ivl  = document.getElementById('bt-interval')?.value || '5m';
  const bal  = parseFloat(document.getElementById('bt-balance')?.value || 10000);
  const comm = parseFloat(document.getElementById('bt-commission')?.value || 0.1) / 100;
  const slip = parseFloat(document.getElementById('bt-slippage')?.value || 0.05) / 100;

  const btn = document.getElementById('bt-run-btn');
  btn.disabled = true;
  btn.textContent = '⏳ RUNNING…';

  document.getElementById('bt-progress-wrap').style.display = 'block';
  document.getElementById('bt-log').innerHTML = '';
  document.getElementById('bt-progress-bar').style.width = '0%';
  document.getElementById('bt-status').textContent = 'Starting…';

  // Clear results
  ['btr-total','btr-wr','btr-pnl','btr-ret','btr-dd','btr-pf','btr-aw','btr-al','btr-wins','btr-losses','btr-bal'].forEach(id => setEl(id, '—'));

  initBacktestChart();
  btSeries.setData([]);
  document.getElementById('bt-trades-tbody').innerHTML = '';

  const equityData = [];

  try {
    const resp = await fetch('/api/backtest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbol: sym, interval: ivl, balance: bal, commission: comm, slippage: slip }),
    });

    const reader = resp.body.getReader();
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
        try {
          const ev = JSON.parse(line.slice(6));
          handleBacktestEvent(ev, equityData, bal);
        } catch(e) {}
      }
    }
  } catch (err) {
    logBt(`Error: ${err.message}`, 'error');
  }

  btn.disabled = false;
  btn.textContent = '▶ RUN BACKTEST';
}

function handleBacktestEvent(ev, equityData, initialBal) {
  if (ev.type === 'progress') {
    document.getElementById('bt-progress-bar').style.width = `${ev.pct}%`;
    document.getElementById('bt-status').textContent = `Processing candle ${ev.idx} — Balance: $${fmt(ev.balance)}`;
    equityData.push({ time: Math.floor(Date.now() / 1000), value: ev.balance });
    if (btSeries && equityData.length > 0) {
      // Use index as pseudo-time for real-time equity curve
      btSeries.setData(equityData.map((pt, i) => ({ time: i + 1, value: pt.value })));
    }

  } else if (ev.type === 'entry') {
    logBt(`↗ ${ev.signal} @ ${fmtPrice(ev.price)} SL:${fmtPrice(ev.sl)} TP:${fmtPrice(ev.tp)}  [Bal:$${fmt(ev.balance)}]`, 'entry');

  } else if (ev.type === 'trade') {
    const pnl = ev.pnl;
    logBt(`${ev.direction} ✓ → ${fmtPrice(ev.exit)} PnL: ${pnl>=0?'+':''}$${fmt(pnl)} [${ev.reason}]  Bal:$${fmt(ev.balance)}`,
          pnl >= 0 ? 'trade-win' : 'trade-loss');
    equityData.push({ time: ev.time_close, value: ev.balance });
    if (btSeries) btSeries.setData(equityData.map((pt, i) => ({ time: i + 1, value: pt.value })));

    // Add to BT trades table
    const tbody = document.getElementById('bt-trades-tbody');
    if (tbody) {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td class="${ev.direction==='BUY'?'dir-buy':'dir-sell'}">${ev.direction}</td>
        <td>${fmtPrice(ev.entry)}</td>
        <td>${fmtPrice(ev.exit)}</td>
        <td class="${pnl>=0?'pnl-pos':'pnl-neg'}">${pnl>=0?'+':''}$${fmt(pnl)}</td>
        <td style="font-size:10px;color:var(--text-dim)">${ev.reason}</td>
      `;
      tbody.prepend(tr);
    }

  } else if (ev.type === 'done') {
    document.getElementById('bt-progress-bar').style.width = '100%';
    document.getElementById('bt-status').textContent = '✓ Complete';
    logBt(`═══ BACKTEST COMPLETE ═══ Trades:${ev.total_trades} WR:${ev.win_rate}% PnL:$${fmt(ev.total_pnl)} DD:${ev.max_drawdown_pct}%`, 'done');

    // Results panel
    setEl('btr-total',  ev.total_trades || 0);
    setEl('btr-wr',     `${ev.win_rate || 0}%`);
    const p = ev.total_pnl || 0;
    setEl('btr-pnl',    `${p>=0?'+':''}$${fmt(p)}`);
    const r = ev.total_return_pct || 0;
    setEl('btr-ret',    `${r>=0?'+':''}${r.toFixed(2)}%`);
    setEl('btr-dd',     `${ev.max_drawdown_pct || 0}%`);
    setEl('btr-pf',     ev.profit_factor || 0);
    setEl('btr-aw',     `$${fmt(ev.avg_win || 0)}`);
    setEl('btr-al',     `$${fmt(ev.avg_loss || 0)}`);
    setEl('btr-wins',   ev.wins || 0);
    setEl('btr-losses', ev.losses || 0);
    setEl('btr-bal',    `$${fmt(ev.final_balance || 0)}`);

    // Final equity curve
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
// WebSocket
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
    try {
      const msg = JSON.parse(ev.data);
      handleWs(msg);
    } catch(e) {}
  };

  S.ws.onclose = () => {
    S.wsAlive = false;
    document.getElementById('live-dot').classList.add('disconnected');
    setTimeout(connect, 3000);
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
        if (document.getElementById('tab-footprint').classList.contains('active')) {
          renderFootprintCanvas();
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
      break;
    case 'pong':
      const lat = Date.now() - S.lastPing;
      document.getElementById('latency').textContent = `${lat}ms`;
      break;
  }
}

function onSnapshot(state) {
  if (!state || state.symbol !== S.symbol) return;
  S.lastSnapshot = state;
  updateCharts(state);
  updateSidebar(state);
  // Positions
  fetchPositions();
}

function subscribe() {
  if (!S.ws || S.ws.readyState !== 1) return;
  S.ws.send(JSON.stringify({
    type: 'subscribe',
    symbol: S.symbol,
    interval: S.interval,
  }));
  document.getElementById('fp-sym').textContent = S.symbol;
}

function startPing() {
  setInterval(() => {
    if (S.ws && S.ws.readyState === 1) {
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
  updateExchangeUI();
}

function updateExchangeUI() {
  const tag = document.getElementById('exchange-tag');
  if (tag) tag.textContent = S.exchange.toUpperCase();
  const btn = document.getElementById('exchange-toggle');
  if (btn) btn.textContent = `⇄ ${S.exchange === 'spot' ? 'FUTURES' : 'SPOT'}`;
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
    const st = d.last_scan;
    if (st) {
      const t  = new Date(st * 1000);
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
// Tab switching
// ─────────────────────────────────────────────────────────────────────────────

function switchTab(name) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.id === `tab-${name}`));

  if (name === 'footprint') {
    renderFootprintCanvas();
  }
  if (name === 'positions') {
    fetchTrades();
    fetchStats();
  }
  if (name === 'scanner') {
    fetchScanner();
  }
  if (name === 'backtest') {
    setTimeout(() => { if (btChart) btChart.applyOptions({ width: document.getElementById('bt-equity-chart').clientWidth }); }, 50);
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Clock
// ─────────────────────────────────────────────────────────────────────────────

function updateClock() {
  const el = document.getElementById('clock');
  if (!el) return;
  const now = new Date();
  el.textContent = now.toUTCString().slice(17, 25) + ' UTC';
}

// ─────────────────────────────────────────────────────────────────────────────
// Tick rate
// ─────────────────────────────────────────────────────────────────────────────

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
// Utility helpers
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
  return Math.abs(n) >= 1000000 ? (n/1000000).toFixed(2)+'M' :
         Math.abs(n) >= 1000    ? (n/1000).toFixed(1)+'K'    :
         n.toFixed(2);
}

function fmtK(n) {
  if (!n) return '0';
  return Math.abs(n) >= 1000000 ? (n/1000000).toFixed(1)+'M' :
         Math.abs(n) >= 1000    ? (n/1000).toFixed(0)+'K'    :
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
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });

  document.getElementById('symbol-select')?.addEventListener('change', e => {
    S.symbol = e.target.value;
    document.getElementById('fp-sym').textContent = S.symbol;
    document.getElementById('bt-symbol').value = S.symbol;
    subscribe();
  });

  document.getElementById('interval-select')?.addEventListener('change', e => {
    S.interval = e.target.value;
    subscribe();
  });

  document.getElementById('exchange-toggle')?.addEventListener('click', async () => {
    const next = S.exchange === 'spot' ? 'futures' : 'spot';
    try {
      await fetch('/api/exchange', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ exchange: next }),
      });
    } catch(e) {}
  });

  document.getElementById('scan-btn')?.addEventListener('click', async () => {
    try {
      await fetch('/api/scanner/scan', { method: 'POST' });
    } catch(e) {}
  });

  document.getElementById('bt-run-btn')?.addEventListener('click', runBacktest);
}

// ─────────────────────────────────────────────────────────────────────────────
// Boot
// ─────────────────────────────────────────────────────────────────────────────

function boot() {
  initCharts();
  initEvents();
  connect();
  updateClock();
  startTickRate();
  setInterval(updateClock, 1000);
  setInterval(fetchStats, 15000);
  setInterval(fetchPositions, 8000);

  // Initial data
  fetchScanner();
  fetchSignals();
  fetchStats();
}

document.addEventListener('DOMContentLoaded', boot);
