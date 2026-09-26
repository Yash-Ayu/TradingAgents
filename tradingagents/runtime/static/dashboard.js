'use strict';

// =============================================================================
// DOM Selectors and Formatting Utilities
// =============================================================================
const $ = id => document.getElementById(id);

const money = value => (typeof value === 'number' && !isNaN(value))
  ? new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR', maximumFractionDigits: 2 }).format(value)
  : '—';

const text = (id, value) => {
  const el = $(id);
  if (el) el.textContent = (value !== undefined && value !== null) ? value : '—';
};

function getControlToken() {
  const meta = document.querySelector('meta[name="control-token"]');
  return meta ? meta.content : '';
}

function showFeedback(msg, duration = 3500) {
  const fb = $('feedback');
  if (!fb) return;
  fb.textContent = msg;
  clearTimeout(fb._timer);
  fb._timer = setTimeout(() => {
    fb.textContent = '';
  }, duration);
}

// =============================================================================
// Application State
// =============================================================================
let appState = null;
let activeTab = 'overview';
let chartMode = 'candles';
let currentInterval = '5m';
let currentChartData = null;
let currentChartSymbol = '';
let tvChart = null;
let candlestickSeries = null;
let areaSeries = null;
let lineSeries = null;
let barSeries = null;
let volumeSeries = null;
let ema20Series = null;
let ema50Series = null;
let vwapSeries = null;
let latestCandle = null;
let isDarkTheme = false;
let activeIndicators = { ema20: false, ema50: false, vwap: false };
let busy = false;
let quoteCache = {};

const DEFAULT_WATCHLIST = [
  { symbol: 'SBIN.NS', name: 'State Bank of India', exchange: 'NSE' },
  { symbol: 'RELIANCE.NS', name: 'Reliance Industries', exchange: 'NSE' },
  { symbol: 'NIFTY', name: 'Nifty 50 Index', exchange: 'NSE' },
  { symbol: 'BANKNIFTY', name: 'Nifty Bank Index', exchange: 'NSE' },
  { symbol: 'TCS.NS', name: 'Tata Consultancy Services', exchange: 'NSE' },
  { symbol: 'INFY.NS', name: 'Infosys Ltd', exchange: 'NSE' },
  { symbol: 'HDFCBANK.NS', name: 'HDFC Bank Ltd', exchange: 'NSE' },
  { symbol: 'ICICIBANK.NS', name: 'ICICI Bank Ltd', exchange: 'NSE' },
  { symbol: 'TATAMOTORS.NS', name: 'Tata Motors Ltd', exchange: 'NSE' },
  { symbol: 'ITC.NS', name: 'ITC Ltd', exchange: 'NSE' },
  { symbol: 'DEMO-EQ', name: 'Synthetic Demo Stock', exchange: 'DEMO' },
];

let watchlist = [];
try {
  const saved = localStorage.getItem('tradingagents_watchlist_v3');
  watchlist = saved ? JSON.parse(saved) : DEFAULT_WATCHLIST;
} catch (e) {
  watchlist = DEFAULT_WATCHLIST;
}

let selectedSymbol = watchlist.length ? watchlist[0].symbol : 'SBIN';

// =============================================================================
// Tab Switching (Top Navigation + Portfolio Tabs)
// =============================================================================
function switchTab(tabId) {
  if (!tabId) return;
  activeTab = tabId;

  // 1. Update Topbar Navigation Buttons
  document.querySelectorAll('.topbar nav button.nav').forEach(btn => {
    const isTarget = btn.dataset.tab === tabId;
    btn.classList.toggle('active', isTarget);
  });

  // 2. Update Portfolio Tablist Buttons
  document.querySelectorAll('.portfolio-tabs button').forEach(btn => {
    const isTarget = btn.dataset.tab === tabId;
    btn.setAttribute('aria-selected', isTarget ? 'true' : 'false');
  });

  // 3. Show/Hide Panels
  const allTabs = ['overview', 'positions', 'orders', 'funds'];
  allTabs.forEach(id => {
    const panel = $('panel-' + id);
    if (panel) {
      panel.hidden = (id !== tabId);
    }
  });
}

function initTabs() {
  // Listen to clicks on Topbar nav buttons
  document.querySelectorAll('.topbar nav button.nav').forEach(btn => {
    btn.addEventListener('click', e => {
      e.preventDefault();
      const tabId = btn.dataset.tab;
      switchTab(tabId);
      if (tabId !== 'overview') {
        const portfolio = document.querySelector('.portfolio-panel');
        if (portfolio) {
          portfolio.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        }
      }
    });
  });

  // Listen to clicks on Portfolio tablist buttons
  document.querySelectorAll('.portfolio-tabs button').forEach(btn => {
    btn.addEventListener('click', e => {
      e.preventDefault();
      switchTab(btn.dataset.tab);
    });
  });
}

// =============================================================================
// Watchlist Search, Render & Add
// =============================================================================
function saveWatchlist() {
  try {
    localStorage.setItem('tradingagents_watchlist', JSON.stringify(watchlist));
  } catch (e) {
    console.warn('Failed to persist watchlist to localStorage', e);
  }
}

function renderWatchlist(query = '') {
  const container = $('watch-items');
  if (!container) return;

  const q = (query || '').trim().toUpperCase();
  const filtered = q
    ? watchlist.filter(item => item.symbol.toUpperCase().includes(q) || (item.name && item.name.toUpperCase().includes(q)))
    : watchlist;

  text('watch-count', watchlist.length);

  if (!filtered.length) {
    container.innerHTML = `<div class="empty" style="height: auto; padding: 24px 16px;">
      Ticker nahi mila.<br>Enter dabakar add karein.
    </div>`;
    return;
  }

  container.replaceChildren(...filtered.map(item => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = `watch-row ${item.symbol === selectedSymbol ? 'selected' : ''}`;

    const quote = quoteCache[item.symbol];
    const priceStr = quote ? money(quote.price) : '—';
    const changePct = quote ? quote.change_pct : null;
    const changeClass = changePct !== null ? (changePct >= 0 ? 'positive' : 'negative') : '';
    const changeStr = changePct !== null ? `${changePct >= 0 ? '+' : ''}${changePct.toFixed(2)}%` : '—';

    btn.innerHTML = `
      <div>
        <strong>${item.symbol}</strong>
        <small>${item.name || item.symbol}</small>
      </div>
      <div>
        <strong>${priceStr}</strong>
        <small class="${changeClass}">${changeStr}</small>
      </div>
    `;

    btn.addEventListener('click', () => {
      selectStock(item.symbol);
    });

    return btn;
  }));
}

function updateStockQuickPicker() {
  const picker = $('stock-quick-picker');
  if (!picker) return;
  picker.innerHTML = '';

  watchlist.forEach(item => {
    const opt = document.createElement('option');
    opt.value = item.symbol;
    opt.textContent = `${item.symbol} • ${item.name || item.symbol}`;
    if (item.symbol === selectedSymbol) {
      opt.selected = true;
    }
    picker.appendChild(opt);
  });

  const customOpt = document.createElement('option');
  customOpt.value = '__ADD_NEW__';
  customOpt.textContent = '➕ Search or Enter New Ticker...';
  picker.appendChild(customOpt);

  picker.value = selectedSymbol;
}

function selectStock(symbol) {
  if (!symbol) return;
  selectedSymbol = symbol.trim().toUpperCase();

  text('chart-symbol', selectedSymbol);
  const exch = selectedSymbol.endsWith('.BO') ? 'BSE' : (selectedSymbol.includes('.') ? selectedSymbol.split('.')[1] : 'NSE');
  text('chart-exchange', exch);
  text('analysis-symbol', selectedSymbol);
  text('analysis-exchange', exch);

  const picker = $('stock-quick-picker');
  if (picker && picker.value !== selectedSymbol) {
    picker.value = selectedSymbol;
  }

  const analyzeBtn = $('analyze');
  if (analyzeBtn) {
    analyzeBtn.disabled = (selectedSymbol === 'DEMO-EQ' || selectedSymbol === 'DEMO');
  }

  // If new stock selected, clear previous stock's recommendation box so user sees fresh prompt
  if (appState && appState.ai && appState.ai.result && appState.ai.result.symbol !== selectedSymbol) {
    const resBox = $('ai-result');
    if (resBox) resBox.hidden = true;
    const rptPanel = $('report-panel');
    if (rptPanel) rptPanel.hidden = true;
    text('ai-progress', `${selectedSymbol} selected. Click "Analyze stock" to run analysis.`);
  }

  renderWatchlist($('watch-search') ? $('watch-search').value : '');
  updateStockQuickPicker();
  fetchAndRenderChart(selectedSymbol);
}

function initWatchlist() {
  const form = $('watch-form');
  const searchInput = $('watch-search');
  const dropdown = $('search-dropdown');
  let searchTimeout = null;

  function addAndSelectStock(sym, name, exch) {
    const cleanSym = sym.trim().toUpperCase();
    const existing = watchlist.find(w => w.symbol === cleanSym);
    if (!existing) {
      watchlist.unshift({
        symbol: cleanSym,
        name: name || cleanSym,
        exchange: exch || 'NSE'
      });
      saveWatchlist();
    }
    selectStock(cleanSym);
    renderWatchlist();
    showFeedback(`Selected: ${cleanSym}`);
  }

  const picker = $('stock-quick-picker');
  if (picker) {
    picker.addEventListener('change', () => {
      const val = picker.value;
      if (val === '__ADD_NEW__') {
        const custom = window.prompt('Enter NSE/BSE stock ticker (e.g. RELIANCE, TCS, TATAMOTORS, INFY):');
        if (custom && custom.trim()) {
          let clean = custom.trim().toUpperCase();
          if (!clean.includes('.') && !clean.includes(' ') && !clean.startsWith('^') && clean !== 'NIFTY' && clean !== 'BANKNIFTY') {
            clean += '.NS';
          }
          addAndSelectStock(clean, clean, clean.endsWith('.BO') ? 'BSE' : 'NSE');
        } else {
          picker.value = selectedSymbol;
        }
        return;
      }
      selectStock(val);
      showFeedback(`Selected ${val}`);
    });
  }

  if (searchInput && dropdown) {
    searchInput.addEventListener('input', () => {
      const q = searchInput.value.trim();
      renderWatchlist(q);
      clearTimeout(searchTimeout);
      if (q.length < 1) {
        dropdown.hidden = true;
        dropdown.innerHTML = '';
        return;
      }
      searchTimeout = setTimeout(async () => {
        try {
          const res = await fetch(`/api/instruments/search?q=${encodeURIComponent(q)}`);
          if (!res.ok) return;
          const data = await res.json();
          const items = data.results || [];
          if (!items.length) {
            dropdown.hidden = true;
            return;
          }
          dropdown.hidden = false;
          dropdown.innerHTML = items.map(item => `
            <div class="search-item" data-symbol="${item.symbol}" data-tradingsymbol="${item.tradingsymbol || item.symbol}" data-exchange="${item.exchange || 'NSE'}" data-name="${item.name || item.symbol}">
              <div>
                <strong>${item.tradingsymbol || item.symbol}</strong>
                <small>${item.name || item.tradingsymbol || item.symbol}</small>
              </div>
              <span class="item-exchange">${item.exchange || 'NSE'}</span>
            </div>
          `).join('');

          dropdown.querySelectorAll('.search-item').forEach(el => {
            el.addEventListener('click', () => {
              const sym = el.dataset.symbol;
              const name = el.dataset.name;
              const exch = el.dataset.exchange;
              addAndSelectStock(sym, name, exch);
              dropdown.hidden = true;
              searchInput.value = '';
              renderWatchlist();
            });
          });
        } catch (e) {
          console.warn('Search failed:', e);
        }
      }, 180);
    });

    document.addEventListener('click', e => {
      if (!form.contains(e.target)) {
        dropdown.hidden = true;
      }
    });
  }

  if (form) {
    form.addEventListener('submit', e => {
      e.preventDefault();
      if (!searchInput) return;
      let raw = searchInput.value.trim().toUpperCase();
      if (!raw) return;
      if (!raw.includes('.') && !raw.includes(' ') && !raw.startsWith('^') && raw !== 'DEMO' && raw !== 'DEMO-EQ' && raw !== 'NIFTY' && raw !== 'BANKNIFTY') {
        raw = raw + '.NS';
      }
      if (dropdown) dropdown.hidden = true;
      addAndSelectStock(raw, raw, raw.endsWith('.BO') ? 'BSE' : 'NSE');
      searchInput.value = '';
    });
  }

  renderWatchlist();
}

// =============================================================================
// TradingView Pro Interactive Chart & Indicators Engine
// =============================================================================
function calculateEMA(data, period) {
  if (!data || data.length === 0) return [];
  const k = 2 / (period + 1);
  let ema = data[0].close;
  const result = [];
  for (let i = 0; i < data.length; i++) {
    ema = (data[i].close - ema) * k + ema;
    result.push({ time: data[i].time, value: Number(ema.toFixed(2)) });
  }
  return result;
}

function calculateVWAP(data) {
  if (!data || data.length === 0) return [];
  let cumVol = 0;
  let cumVolPrice = 0;
  let currentDay = null;
  const result = [];
  for (let i = 0; i < data.length; i++) {
    const d = new Date(data[i].time * 1000).getUTCDate();
    if (d !== currentDay) {
      cumVol = 0;
      cumVolPrice = 0;
      currentDay = d;
    }
    const typicalPrice = (data[i].high + data[i].low + data[i].close) / 3;
    const vol = data[i].volume > 0 ? data[i].volume : 1;
    cumVol += vol;
    cumVolPrice += typicalPrice * vol;
    result.push({ time: data[i].time, value: Number((cumVolPrice / cumVol).toFixed(2)) });
  }
  return result;
}

function initTradingViewChart() {
  const container = $('tv-chart');
  if (!container || typeof window.LightweightCharts === 'undefined') {
    return false;
  }
  if (tvChart) return true;

  try {
    const width = container.clientWidth || 800;
    const height = container.clientHeight || 350;

    tvChart = window.LightweightCharts.createChart(container, {
      width: width,
      height: height,
      layout: {
        background: { type: 'solid', color: isDarkTheme ? '#131722' : '#ffffff' },
        textColor: isDarkTheme ? '#d1d4dc' : '#637083',
        fontSize: 11,
        fontFamily: 'Inter, -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, sans-serif',
      },
      grid: {
        vertLines: { color: isDarkTheme ? '#1e222d' : '#f0f3f8', style: 1 },
        horzLines: { color: isDarkTheme ? '#1e222d' : '#f0f3f8', style: 1 },
      },
      crosshair: {
        mode: window.LightweightCharts.CrosshairMode.Normal,
        vertLine: {
          color: isDarkTheme ? '#505668' : '#8e9ab0',
          width: 1,
          style: 2,
          labelBackgroundColor: '#3d51df',
        },
        horzLine: {
          color: isDarkTheme ? '#505668' : '#8e9ab0',
          width: 1,
          style: 2,
          labelBackgroundColor: '#3d51df',
        },
      },
      rightPriceScale: {
        borderColor: isDarkTheme ? '#2a2e39' : '#e5eaf2',
        scaleMargins: {
          top: 0.08,
          bottom: 0.22,
        },
        autoScale: true,
      },
      timeScale: {
        borderColor: isDarkTheme ? '#2a2e39' : '#e5eaf2',
        timeVisible: true,
        secondsVisible: false,
      },
      handleScroll: {
        mouseWheel: true,
        pressedMouseMove: true,
        horzTouchDrag: true,
        vertTouchDrag: true,
      },
      handleScale: {
        axisPressedMouseMove: true,
        mouseWheel: true,
        pinch: true,
      },
    });

    // 1. Volume Series (bottom 22% overlay)
    volumeSeries = tvChart.addSeries(window.LightweightCharts.HistogramSeries, {
      priceFormat: { type: 'volume' },
      priceScaleId: 'volume',
    });
    tvChart.priceScale('volume').applyOptions({
      scaleMargins: {
        top: 0.78,
        bottom: 0,
      },
    });

    // 2. Candlestick Series
    candlestickSeries = tvChart.addSeries(window.LightweightCharts.CandlestickSeries, {
      upColor: '#089981',
      downColor: '#f23645',
      borderVisible: false,
      wickUpColor: '#089981',
      wickDownColor: '#f23645',
    });

    // 3. Area Series (TradingView gradient)
    areaSeries = tvChart.addSeries(window.LightweightCharts.AreaSeries, {
      topColor: 'rgba(41, 98, 255, 0.38)',
      bottomColor: 'rgba(41, 98, 255, 0.0)',
      lineColor: '#2962ff',
      lineWidth: 2,
      visible: false,
    });

    // 4. Line Series
    lineSeries = tvChart.addSeries(window.LightweightCharts.LineSeries, {
      color: '#3d51df',
      lineWidth: 2,
      crosshairMarkerVisible: true,
      visible: false,
    });

    // 5. Bar Series (OHLC bars)
    barSeries = tvChart.addSeries(window.LightweightCharts.BarSeries, {
      upColor: '#089981',
      downColor: '#f23645',
      visible: false,
    });

    // 6. Indicators: EMA 20, EMA 50, VWAP
    ema20Series = tvChart.addSeries(window.LightweightCharts.LineSeries, {
      color: '#2962ff',
      lineWidth: 1.5,
      priceLineVisible: false,
      lastValueVisible: false,
      visible: false,
    });

    ema50Series = tvChart.addSeries(window.LightweightCharts.LineSeries, {
      color: '#e07a5f',
      lineWidth: 1.5,
      priceLineVisible: false,
      lastValueVisible: false,
      visible: false,
    });

    vwapSeries = tvChart.addSeries(window.LightweightCharts.LineSeries, {
      color: '#9c27b0',
      lineWidth: 1.5,
      priceLineVisible: false,
      lastValueVisible: false,
      visible: false,
    });

    // 7. Crosshair Tracking & Live Legend
    tvChart.subscribeCrosshairMove(param => {
      if (!param || !param.time) {
        updateLegend(latestCandle);
        return;
      }
      let cData = null;
      if (chartMode === 'candles' && candlestickSeries) {
        cData = param.seriesData ? param.seriesData.get(candlestickSeries) : null;
      } else if (chartMode === 'area' && areaSeries) {
        cData = param.seriesData ? param.seriesData.get(areaSeries) : null;
      } else if (chartMode === 'bars' && barSeries) {
        cData = param.seriesData ? param.seriesData.get(barSeries) : null;
      } else if (lineSeries) {
        cData = param.seriesData ? param.seriesData.get(lineSeries) : null;
      }

      if (cData) {
        const vData = volumeSeries && param.seriesData ? param.seriesData.get(volumeSeries) : null;
        updateLegend({
          open: cData.open !== undefined ? cData.open : cData.value,
          high: cData.high !== undefined ? cData.high : cData.value,
          low: cData.low !== undefined ? cData.low : cData.value,
          close: cData.close !== undefined ? cData.close : cData.value,
          volume: vData ? vData.value : null,
          time: param.time,
        });

        // Indicator values on crosshair hover
        if (activeIndicators.ema20 && ema20Series && param.seriesData) {
          const ed = param.seriesData.get(ema20Series);
          text('val-ema20', ed && ed.value !== undefined ? ed.value.toFixed(2) : '—');
        }
        if (activeIndicators.ema50 && ema50Series && param.seriesData) {
          const ed = param.seriesData.get(ema50Series);
          text('val-ema50', ed && ed.value !== undefined ? ed.value.toFixed(2) : '—');
        }
        if (activeIndicators.vwap && vwapSeries && param.seriesData) {
          const vd = param.seriesData.get(vwapSeries);
          text('val-vwap', vd && vd.value !== undefined ? vd.value.toFixed(2) : '—');
        }
      } else {
        updateLegend(latestCandle);
      }
    });

    // 8. Container Resize Observer
    const wrap = $('chart-wrap');
    if (wrap && window.ResizeObserver) {
      const ro = new ResizeObserver(entries => {
        for (const entry of entries) {
          const w = entry.contentRect.width;
          const h = entry.contentRect.height;
          if (w > 0 && h > 0 && tvChart) {
            tvChart.applyOptions({ width: w, height: h });
          }
        }
      });
      ro.observe(wrap);
    }

    return true;
  } catch (err) {
    console.warn('TradingView initialization failed, fallback to SVG:', err);
    tvChart = null;
    return false;
  }
}

function applyChartTheme(dark) {
  isDarkTheme = dark;
  const panel = document.querySelector('.chart-panel');
  if (panel) panel.classList.toggle('dark-theme', dark);
  const themeBtn = $('chart-theme-btn');
  if (themeBtn) themeBtn.textContent = dark ? '☀ Light' : '☾ Dark';

  if (!tvChart) return;
  tvChart.applyOptions({
    layout: {
      background: { type: 'solid', color: dark ? '#131722' : '#ffffff' },
      textColor: dark ? '#d1d4dc' : '#637083',
    },
    grid: {
      vertLines: { color: dark ? '#1e222d' : '#f0f3f8' },
      horzLines: { color: dark ? '#1e222d' : '#f0f3f8' },
    },
    rightPriceScale: {
      borderColor: dark ? '#2a2e39' : '#e5eaf2',
    },
    timeScale: {
      borderColor: dark ? '#2a2e39' : '#e5eaf2',
    },
    crosshair: {
      vertLine: {
        color: dark ? '#505668' : '#8e9ab0',
      },
      horzLine: {
        color: dark ? '#505668' : '#8e9ab0',
      },
    },
  });
}

function toggleChartFullscreen() {
  const panel = document.querySelector('.chart-panel');
  if (!panel) return;
  const isFull = panel.classList.toggle('fullscreen');
  const btn = $('chart-fullscreen-btn');
  if (btn) btn.textContent = isFull ? '✕ Exit' : '⛶';

  setTimeout(() => {
    const wrap = $('chart-wrap');
    if (wrap && tvChart) {
      tvChart.applyOptions({
        width: wrap.clientWidth,
        height: wrap.clientHeight,
      });
      tvChart.timeScale().fitContent();
    }
  }, 100);
}

function updateLegend(candle) {
  text('leg-symbol', selectedSymbol || 'DEMO-EQ');
  text('leg-interval', currentInterval || '5m');
  if (!candle) {
    text('leg-o', '—');
    text('leg-h', '—');
    text('leg-l', '—');
    text('leg-c', '—');
    text('leg-v', '—');
    const chgEl = $('leg-chg');
    if (chgEl) {
      chgEl.textContent = '—';
      chgEl.className = '';
    }
    return;
  }
  const o = Number(candle.open);
  const h = Number(candle.high);
  const l = Number(candle.low);
  const c = Number(candle.close);
  const v = candle.volume !== undefined && candle.volume !== null ? Number(candle.volume) : null;

  text('leg-o', isNaN(o) ? '—' : o.toFixed(2));
  text('leg-h', isNaN(h) ? '—' : h.toFixed(2));
  text('leg-l', isNaN(l) ? '—' : l.toFixed(2));
  text('leg-c', isNaN(c) ? '—' : c.toFixed(2));

  if (v !== null && !isNaN(v)) {
    const vStr = v >= 1e7 ? (v / 1e7).toFixed(2) + 'Cr' : (v >= 1e5 ? (v / 1e5).toFixed(2) + 'L' : (v >= 1e3 ? (v / 1e3).toFixed(1) + 'K' : String(v)));
    text('leg-v', vStr);
  } else {
    text('leg-v', '—');
  }

  const chgEl = $('leg-chg');
  if (chgEl && !isNaN(o) && !isNaN(c) && o > 0) {
    const diff = c - o;
    const pct = (diff / o) * 100;
    const sign = diff >= 0 ? '+' : '';
    chgEl.textContent = `${sign}${diff.toFixed(2)} (${sign}${pct.toFixed(2)}%)`;
    chgEl.className = diff >= 0 ? 'positive' : 'negative';
  }
}

function parseCandlesForTV(rawCandles) {
  if (!rawCandles || !Array.isArray(rawCandles)) return { candles: [], volumes: [], lines: [] };

  const parsed = [];
  const nowSec = Math.floor(Date.now() / 1000);
  const IST_OFFSET = 19800; // 5 hours 30 minutes in seconds

  rawCandles.forEach((c, idx) => {
    let t = Math.floor(new Date(c[0]).getTime() / 1000);
    if (isNaN(t) || t <= 0) {
      t = nowSec - (rawCandles.length - idx) * 300;
    } else {
      // Shift epoch so Lightweight Charts UTC rendering matches exact Indian Standard Time (IST)
      t += IST_OFFSET;
    }
    const o = Number(c[1]);
    const h = Number(c[2]);
    const l = Number(c[3]);
    const cl = Number(c[4]);
    const v = Number(c[5]) || 0;

    if (!isNaN(o) && !isNaN(h) && !isNaN(l) && !isNaN(cl)) {
      parsed.push({
        time: t,
        open: o,
        high: h,
        low: l,
        close: cl,
        volume: v,
      });
    }
  });

  parsed.sort((a, b) => a.time - b.time);

  const candles = [];
  const volumes = [];
  const lines = [];

  for (let i = 0; i < parsed.length; i++) {
    const item = parsed[i];
    if (candles.length > 0 && candles[candles.length - 1].time >= item.time) {
      item.time = candles[candles.length - 1].time + 1;
    }
    candles.push({
      time: item.time,
      open: item.open,
      high: item.high,
      low: item.low,
      close: item.close,
      volume: item.volume,
    });
    volumes.push({
      time: item.time,
      value: item.volume,
      color: item.close >= item.open ? 'rgba(8, 153, 129, 0.45)' : 'rgba(242, 54, 69, 0.45)',
    });
    lines.push({
      time: item.time,
      value: item.close,
    });
  }

  return { candles, volumes, lines };
}

function renderTradingViewChart(rawCandles, mode = 'candles') {
  if (!initTradingViewChart()) {
    return false;
  }

  const { candles, volumes, lines } = parseCandlesForTV(rawCandles);
  const empty = $('chart-empty');
  const tvContainer = $('tv-chart');
  const svg = $('chart');

  if (!candles.length) {
    if (empty) empty.hidden = false;
    if (tvContainer) tvContainer.style.opacity = '0';
    return true;
  }

  if (empty) empty.hidden = true;
  if (svg) svg.hidden = true;
  if (tvContainer) tvContainer.style.opacity = '1';

  // Update Series Data
  candlestickSeries.setData(candles);
  areaSeries.setData(lines);
  lineSeries.setData(lines);
  barSeries.setData(candles);
  volumeSeries.setData(volumes);

  // Compute Technical Indicators
  const ema20Data = calculateEMA(candles, 20);
  const ema50Data = calculateEMA(candles, 50);
  const vwapData = calculateVWAP(candles);

  ema20Series.setData(ema20Data);
  ema50Series.setData(ema50Data);
  vwapSeries.setData(vwapData);

  // Apply Chart Mode
  candlestickSeries.applyOptions({ visible: mode === 'candles' });
  areaSeries.applyOptions({ visible: mode === 'area' });
  lineSeries.applyOptions({ visible: mode === 'line' });
  barSeries.applyOptions({ visible: mode === 'bars' });

  // Apply Indicator Visibility
  ema20Series.applyOptions({ visible: !!activeIndicators.ema20 });
  ema50Series.applyOptions({ visible: !!activeIndicators.ema50 });
  vwapSeries.applyOptions({ visible: !!activeIndicators.vwap });

  // Update Legend with Latest Bar
  const last = candles[candles.length - 1];
  const lastVol = volumes[volumes.length - 1];
  latestCandle = {
    open: last.open,
    high: last.high,
    low: last.low,
    close: last.close,
    volume: lastVol ? lastVol.value : 0,
    time: last.time,
  };
  updateLegend(latestCandle);

  // Update latest indicator values
  if (ema20Data.length) text('val-ema20', ema20Data[ema20Data.length - 1].value.toFixed(2));
  if (ema50Data.length) text('val-ema50', ema50Data[ema50Data.length - 1].value.toFixed(2));
  if (vwapData.length) text('val-vwap', vwapData[vwapData.length - 1].value.toFixed(2));

  if (tvChart) {
    tvChart.timeScale().applyOptions({
      timeVisible: currentInterval !== '1d',
      secondsVisible: false,
    });
    tvChart.timeScale().fitContent();
  }
  return true;
}

async function fetchAndRenderChart(symbol, interval) {
  const sym = symbol || selectedSymbol;
  const itv = interval || currentInterval;
  currentChartSymbol = sym;
  currentInterval = itv;
  text('chart-caption', `Loading chart for ${sym} (${itv})…`);

  try {
    const res = await fetch(`/api/chart?symbol=${encodeURIComponent(sym)}&interval=${encodeURIComponent(itv)}`);
    if (!res.ok) {
      throw new Error(`Chart data unavailable (${res.status})`);
    }
    const data = await res.json();
    if (currentChartSymbol !== sym) return; // Prevent race conditions

    currentChartData = data;
    quoteCache[sym] = { price: data.price, change_pct: data.change_pct };

    // Update Header Quotes
    text('price', money(data.price));
    const chgEl = $('price-change');
    if (chgEl) {
      const pct = data.change_pct || 0;
      chgEl.className = pct >= 0 ? 'positive' : 'negative';
      chgEl.textContent = `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}% (${data.interval || itv})`;
    }

    // Market Status Badge & Data Source
    const statusBadge = $('market-status-badge');
    if (statusBadge) {
      const isLive = data.is_market_open && data.source === 'LIVE_ANGEL_ONE';
      statusBadge.className = `market-status-badge ${isLive ? 'badge-live' : (data.is_market_open ? 'badge-live' : 'badge-closed')}`;
      statusBadge.textContent = isLive ? 'LIVE' : (data.is_market_open ? 'OPEN' : 'MARKET CLOSED');
    }

    const sourceEl = $('chart-source');
    if (sourceEl) {
      let srcText = 'Research Feed';
      if (data.source === 'LIVE_ANGEL_ONE') srcText = 'Angel One SmartAPI';
      else if (data.source === 'YAHOO_FINANCE_RESEARCH') srcText = 'Yahoo Finance (Research)';
      else if (data.source === 'DEMO_SYNTHETIC') srcText = 'Synthetic Demo';
      sourceEl.textContent = srcText;
    }

    text('chart-symbol', data.symbol || sym);
    text('analysis-symbol', data.symbol || sym);
    if (data.exchange) text('chart-exchange', data.exchange);

    text('leg-symbol', data.tradingsymbol || data.symbol || sym);
    text('updated', data.timestamp ? new Date(data.timestamp).toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit' }) + ' IST' : 'Live');
    text('chart-interval', data.interval || itv);
    text('leg-interval', data.interval || itv);
    text('chart-caption', `${data.symbol} • ${sourceEl ? sourceEl.textContent : 'Market Data'} • Indian Standard Time (IST)`);

    drawChart(data.chart, chartMode);
    renderWatchlist($('watch-search') ? $('watch-search').value : '');
  } catch (err) {
    console.warn(`Chart fetch error for ${sym}:`, err);
    if ((sym === 'DEMO-EQ' || sym === 'DEMO') && appState && appState.snapshot && appState.snapshot.chart) {
      drawChart(appState.snapshot.chart, chartMode);
    } else {
      const empty = $('chart-empty');
      const tvContainer = $('tv-chart');
      const svg = $('chart');
      if (svg) svg.innerHTML = '';
      if (tvContainer) tvContainer.style.opacity = '0';
      if (empty) {
        empty.hidden = false;
        const str = empty.querySelector('strong');
        const par = empty.querySelector('p');
        if (str) str.textContent = `${sym} Chart Unavailable`;
        if (par) par.textContent = 'Market data connect hone tak ya valid ticker select karne tak wait karein.';
      }
      text('chart-caption', `${sym} data temporarily unavailable`);
    }
  }
}

function drawChart(candles, mode = 'candles') {
  renderTradingViewChart(candles, mode);
  drawSvgChart(candles, mode);
}

function drawSvgChart(candles, mode = 'candles') {
  const svg = $('chart');
  const empty = $('chart-empty');
  if (!svg) return;

  if (!candles || !candles.length) {
    svg.innerHTML = '';
    if (empty) empty.hidden = false;
    return;
  }

  // SVG Geometry
  const W = 1000;
  const H = 350;
  const padTop = 25;
  const padBottom = 35;
  const padLeft = 20;
  const padRight = 75;
  const chartW = W - padLeft - padRight;
  const chartH = H - padTop - padBottom;

  // Extract prices
  const lows = candles.map(c => Number(c[3]));
  const highs = candles.map(c => Number(c[2]));
  const closes = candles.map(c => Number(c[4]));
  let minP = Math.min(...lows);
  let maxP = Math.max(...highs);

  if (minP === maxP) {
    minP *= 0.98;
    maxP *= 1.02;
  }
  const range = maxP - minP;
  const buf = range * 0.05;
  minP -= buf;
  maxP += buf;
  const fullRange = maxP - minP;

  const toY = p => padTop + chartH * (1 - (p - minP) / fullRange);
  const n = candles.length;
  const step = chartW / Math.max(n - 1, 1);
  const toX = i => padLeft + i * step;

  let elements = [];

  // 1. Grid Lines & Price Ticks (4 levels)
  const ticksCount = 4;
  for (let i = 0; i <= ticksCount; i++) {
    const p = minP + (fullRange * i) / ticksCount;
    const y = toY(p);
    elements.push(`<line x1="${padLeft}" y1="${y.toFixed(1)}" x2="${(padLeft + chartW).toFixed(1)}" y2="${y.toFixed(1)}" stroke="#eef1f6" stroke-dasharray="3,3" stroke-width="1" />`);
    elements.push(`<text x="${(padLeft + chartW + 8).toFixed(1)}" y="${(y + 3).toFixed(1)}" font-size="10" fill="#929bac" font-family="Inter,sans-serif">${p.toFixed(2)}</text>`);
  }

  // 2. Time Ticks on X-axis (4 to 5 time points)
  const timeStep = Math.max(Math.floor(n / 4), 1);
  for (let i = 0; i < n; i += timeStep) {
    const c = candles[i];
    const x = toX(i);
    const dateObj = new Date(c[0]);
    const timeStr = !isNaN(dateObj.getTime())
      ? dateObj.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
      : `T-${n - i}`;
    elements.push(`<text x="${x.toFixed(1)}" y="${(H - 12).toFixed(1)}" font-size="10" fill="#929bac" text-anchor="middle" font-family="Inter,sans-serif">${timeStr}</text>`);
  }

  // 3. Draw Price Series
  if (mode === 'candles' || mode === 'bars') {
    const candleWidth = Math.max(Math.min(step * 0.7, 12), 2);
    candles.forEach((c, i) => {
      const x = toX(i);
      const o = Number(c[1]);
      const h = Number(c[2]);
      const l = Number(c[3]);
      const cl = Number(c[4]);
      const isUp = cl >= o;
      const color = isUp ? '#089981' : '#f23645';

      const yHigh = toY(h);
      const yLow = toY(l);
      const yOpen = toY(o);
      const yClose = toY(cl);

      const bodyY = Math.min(yOpen, yClose);
      const bodyH = Math.max(Math.abs(yClose - yOpen), 1.5);

      elements.push(`<line x1="${x.toFixed(1)}" y1="${yHigh.toFixed(1)}" x2="${x.toFixed(1)}" y2="${yLow.toFixed(1)}" stroke="${color}" stroke-width="1.2" />`);
      elements.push(`<rect x="${(x - candleWidth / 2).toFixed(1)}" y="${bodyY.toFixed(1)}" width="${candleWidth.toFixed(1)}" height="${bodyH.toFixed(1)}" fill="${color}" rx="1" />`);
    });
  } else {
    let dLine = '';
    let dArea = `M ${toX(0).toFixed(1)} ${(padTop + chartH).toFixed(1)}`;

    candles.forEach((c, i) => {
      const x = toX(i).toFixed(1);
      const y = toY(Number(c[4])).toFixed(1);
      if (i === 0) {
        dLine += `M ${x} ${y}`;
      } else {
        dLine += ` L ${x} ${y}`;
      }
      dArea += ` L ${x} ${y}`;
    });

    dArea += ` L ${toX(n - 1).toFixed(1)} ${(padTop + chartH).toFixed(1)} Z`;

    elements.push(`
      <defs>
        <linearGradient id="areaGrad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#2962ff" stop-opacity="0.3" />
          <stop offset="100%" stop-color="#2962ff" stop-opacity="0.0" />
        </linearGradient>
      </defs>
      <path d="${dArea}" fill="url(#areaGrad)" />
      <path d="${dLine}" fill="none" stroke="#2962ff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" />
    `);
  }

  const lastC = closes[closes.length - 1];
  const lastY = toY(lastC);
  elements.push(`
    <line x1="${padLeft}" y1="${lastY.toFixed(1)}" x2="${(padLeft + chartW).toFixed(1)}" y2="${lastY.toFixed(1)}" stroke="#2962ff" stroke-dasharray="2,2" stroke-width="1.2" />
    <rect x="${(padLeft + chartW + 2).toFixed(1)}" y="${(lastY - 9).toFixed(1)}" width="65" height="18" rx="3" fill="#2962ff" />
    <text x="${(padLeft + chartW + 34).toFixed(1)}" y="${(lastY + 4).toFixed(1)}" font-size="10" fill="#fff" text-anchor="middle" font-weight="600" font-family="Inter,sans-serif">${lastC.toFixed(2)}</text>
  `);

  svg.innerHTML = elements.join('');
}

function initChart() {
  // Chart Style Toggles (Candles, Area, Line, Bars)
  document.querySelectorAll('.chart-toggle').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.chart-toggle').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      chartMode = btn.dataset.chart || 'candles';
      if (currentChartData && currentChartData.chart) {
        drawChart(currentChartData.chart, chartMode);
      }
    });
  });

  // Timeframe Interval Switcher (1m, 5m, 15m, 1h, 1D)
  document.querySelectorAll('.interval-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.interval-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      currentInterval = btn.dataset.interval || '5m';
      text('chart-interval', currentInterval);
      text('leg-interval', currentInterval);
      fetchAndRenderChart(selectedSymbol, currentInterval);
    });
  });

  // Technical Indicators Toggle (EMA20, EMA50, VWAP)
  document.querySelectorAll('.indicator-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const ind = btn.dataset.indicator;
      if (!ind) return;
      activeIndicators[ind] = !activeIndicators[ind];
      btn.classList.toggle('active', activeIndicators[ind]);

      const legendPill = $(`leg-${ind}`);
      if (legendPill) legendPill.hidden = !activeIndicators[ind];

      if (ind === 'ema20' && ema20Series) ema20Series.applyOptions({ visible: activeIndicators[ind] });
      if (ind === 'ema50' && ema50Series) ema50Series.applyOptions({ visible: activeIndicators[ind] });
      if (ind === 'vwap' && vwapSeries) vwapSeries.applyOptions({ visible: activeIndicators[ind] });

      showFeedback(`${ind.toUpperCase()} ${activeIndicators[ind] ? 'on' : 'off'}`);
    });
  });

  // Fit / Reset Zoom Button
  const fitBtn = $('chart-fit-btn');
  if (fitBtn) {
    fitBtn.addEventListener('click', () => {
      if (tvChart) {
        tvChart.timeScale().fitContent();
        showFeedback('Chart view reset / fit content');
      }
    });
  }

  // Theme Toggle Button (Dark / Light)
  const themeBtn = $('chart-theme-btn');
  if (themeBtn) {
    themeBtn.addEventListener('click', () => {
      applyChartTheme(!isDarkTheme);
    });
  }

  // Fullscreen Button
  const fsBtn = $('chart-fullscreen-btn');
  if (fsBtn) {
    fsBtn.addEventListener('click', () => {
      toggleChartFullscreen();
    });
  }
}

// =============================================================================
// AI Setup Dialog & Model Analysis
// =============================================================================
function initAI() {
  const dialog = $('ai-dialog');
  const setupTop = $('setup-top');
  const setupAi = $('setup-ai');
  const closeAi = $('close-ai');
  const aiForm = $('ai-form');
  const providerSelect = $('ai-provider');
  const keyLabel = $('key-label');
  const endpointLabel = $('endpoint-label');
  const analyzeBtn = $('analyze');
  const disconnectBtn = $('disconnect-ai');

  const openDialog = () => {
    if (dialog && typeof dialog.showModal === 'function') {
      $('ai-form-error').textContent = '';
      const modelInput = $('ai-model');
      if (modelInput && !modelInput.value) {
        modelInput.value = 'gemini-3.6-flash';
      }
      dialog.showModal();
    }
  };

  const closeDialog = () => {
    if (dialog && typeof dialog.close === 'function') {
      dialog.close();
    }
  };

  if (setupTop) setupTop.addEventListener('click', openDialog);
  if (setupAi) setupAi.addEventListener('click', openDialog);
  if (closeAi) closeAi.addEventListener('click', closeDialog);

  if (providerSelect) {
    providerSelect.addEventListener('change', () => {
      const isOllama = providerSelect.value === 'ollama';
      if (endpointLabel) endpointLabel.hidden = !isOllama;
      if (keyLabel) keyLabel.hidden = isOllama;
      const modelInput = $('ai-model');
      if (modelInput) {
        if (providerSelect.value === 'google') modelInput.value = 'gemini-3.6-flash';
        else if (providerSelect.value === 'openai') modelInput.value = 'gpt-4.1-mini';
        else if (providerSelect.value === 'anthropic') modelInput.value = 'claude-haiku-4-5';
        else if (providerSelect.value === 'deepseek') modelInput.value = 'deepseek-chat';
        else if (providerSelect.value === 'ollama') modelInput.value = 'llama3.2';
      }
    });
  }

  if (aiForm) {
    aiForm.addEventListener('submit', async e => {
      e.preventDefault();
      const errBox = $('ai-form-error');
      errBox.textContent = '';

      const provider = $('ai-provider').value;
      const model = $('ai-model').value.trim();
      const apiKey = $('ai-key').value.trim();
      const endpoint = $('ai-endpoint').value.trim();

      const payload = { provider, model };
      if (apiKey) payload.api_key = apiKey;
      if (provider === 'ollama' && endpoint) payload.base_url = endpoint;

      try {
        const res = await fetch('/api/ai/configure', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-Control-Token': getControlToken()
          },
          body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (!res.ok) {
          throw new Error(data.error || 'AI setup failed');
        }
        closeDialog();
        showFeedback(`AI Connected: ${provider} (${model})`);
        render(data);
      } catch (err) {
        errBox.textContent = err.message || 'AI setup failed. Verify model ID and credentials.';
      }
    });
  }

  if (analyzeBtn) {
    analyzeBtn.addEventListener('click', async () => {
      if (!selectedSymbol || selectedSymbol === 'DEMO-EQ' || selectedSymbol === 'DEMO') {
        showFeedback('Kripya watchlist se real research stock select karein.');
        return;
      }
      if (appState && appState.ai && appState.ai.state === 'not_configured') {
        showFeedback('AI Setup zaroori hai. Configure modal open ho raha hai…');
        const dialog = $('ai-dialog');
        if (dialog && typeof dialog.showModal === 'function') {
          dialog.showModal();
        }
        return;
      }
      analyzeBtn.disabled = true;
      text('ai-progress', `Running multi-agent analysis for ${selectedSymbol}…`);

      try {
        const res = await fetch('/api/ai/analyze', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-Control-Token': getControlToken()
          },
          body: JSON.stringify({ symbol: selectedSymbol })
        });
        const data = await res.json();
        if (!res.ok) {
          const errCode = data.error || 'analysis_failed';
          if (errCode === 'connect_ai_first') {
            const dialog = $('ai-dialog');
            if (dialog && typeof dialog.showModal === 'function') dialog.showModal();
            throw new Error('AI Provider connect nahi hai. Kripya pehle AI Configure karein.');
          }
          throw new Error(errCode);
        }
        showFeedback(`Analysis started for ${selectedSymbol}`);
        render(data);
      } catch (err) {
        showFeedback(err.message.startsWith('AI') ? err.message : `Analysis notice: ${err.message}`);
        text('ai-progress', err.message);
        analyzeBtn.disabled = false;
        analyzeBtn.innerHTML = 'Analyze stock <span>→</span>';
      }
    });
  }

  if (disconnectBtn) {
    disconnectBtn.addEventListener('click', async () => {
      if (!window.confirm('AI disconnect karna chahte hain?')) return;
      try {
        const res = await fetch('/api/ai/disconnect', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-Control-Token': getControlToken()
          },
          body: '{}'
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Disconnect failed');
        showFeedback('AI disconnected successfully');
        render(data);
      } catch (err) {
        showFeedback(`Disconnect error: ${err.message}`);
      }
    });
  }
}

// =============================================================================
// Runner Actions (Start, Pause, Kill, Reset)
// =============================================================================
function initActions() {
  document.querySelectorAll('[data-action]').forEach(button => {
    button.addEventListener('click', async () => {
      if (busy) return;
      busy = true;
      const action = button.dataset.action;

      if (action === 'kill' && !window.confirm('Emergency stop paper execution? Open paper positions will remain tracked.')) {
        busy = false;
        return;
      }

      try {
        const res = await fetch(`/api/${action}`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-Control-Token': getControlToken()
          },
          body: '{}'
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Control failed');
        render(data);
        showFeedback(action === 'kill' ? 'Emergency stop saved. Positions remain open.' : `${action.toUpperCase()} complete`);
      } catch (err) {
        showFeedback(err.message || 'Operation failed');
      } finally {
        busy = false;
      }
    });
  });
}

// =============================================================================
// Render Data Tables & Sections
// =============================================================================
function facts(id, entries) {
  const el = $(id);
  if (!el) return;
  el.replaceChildren(...entries.flatMap(([name, value]) => {
    const dt = document.createElement('dt');
    const dd = document.createElement('dd');
    dt.textContent = name;
    dd.textContent = (value !== undefined && value !== null) ? value : '—';
    return [dt, dd];
  }));
}

function renderPositionsTable(positions) {
  const tbody = $('positions');
  if (!tbody) return;

  if (!positions || !positions.length) {
    tbody.innerHTML = `<tr><td colspan="5" class="empty">No open paper positions</td></tr>`;
    return;
  }

  tbody.replaceChildren(...positions.map(p => {
    const row = document.createElement('tr');
    const pnl = Number(p.unrealized_pnl) || 0;
    const pnlClass = pnl >= 0 ? 'positive' : 'negative';

    row.innerHTML = `
      <td><strong>${p.symbol || '—'}</strong></td>
      <td>${p.qty || 0}</td>
      <td>${money(p.average)}</td>
      <td>${money(p.mark)}</td>
      <td class="${pnlClass}"><strong>${money(pnl)}</strong></td>
    `;
    return row;
  }));
}

function renderOrdersTable(orders) {
  const tbody = $('orders');
  if (!tbody) return;

  if (!orders || !orders.length) {
    tbody.innerHTML = `<tr><td colspan="8" class="empty">No paper orders yet</td></tr>`;
    return;
  }

  tbody.replaceChildren(...orders.map(o => {
    const row = document.createElement('tr');
    const side = (o.side || 'BUY').toUpperCase();
    const sideClass = side === 'BUY' ? 'positive' : 'negative';
    const timeStr = o.timestamp ? new Date(o.timestamp).toLocaleTimeString() : '—';

    row.innerHTML = `
      <td>${timeStr}</td>
      <td><strong>${o.symbol || '—'}</strong></td>
      <td><strong class="${sideClass}">${side}</strong></td>
      <td>${o.qty || 0}</td>
      <td>${money(o.price)}</td>
      <td>${o.lots || 1}</td>
      <td>${money(o.realized_pnl)}</td>
      <td><span class="pill">${o.status || 'FILLED'}</span></td>
    `;
    return row;
  }));
}

// =============================================================================
// Main State Sync & Render Loop
// =============================================================================
function render(s) {
  if (!s) return;
  appState = s;

  // 1. Top Strip & Header
  text('session-label', `${s.instrument?.exchange || 'NSE'} • ${s.instrument?.symbol || 'DEMO-EQ'} (${(s.source || 'paper').toUpperCase()})`);
  text('cash-top', money(s.account?.cash));
  text('equity-top', money(s.account?.equity));

  // 2. Metrics & Account Balances
  text('equity', money(s.account?.equity));
  text('cash', money(s.account?.cash));
  text('pnl', money(s.account?.daily_pnl));

  const pnlEl = $('pnl');
  if (pnlEl) {
    pnlEl.className = (s.account?.daily_pnl || 0) < 0 ? 'negative' : 'positive';
  }
  text('drawdown', `Drawdown ${(s.account?.drawdown_pct || 0).toFixed(2)}%`);

  // 3. Tab Badge Counts (Just the numbers)
  const posCount = s.account?.positions ? s.account.positions.length : 0;
  const ordCount = s.orders ? s.orders.length : 0;
  text('position-count', posCount);
  text('order-count', ordCount);

  // 4. Tables
  renderPositionsTable(s.account?.positions || []);
  renderOrdersTable(s.orders || []);

  // 5. Funds Summary
  const fundsSummary = $('funds-summary');
  if (fundsSummary) {
    fundsSummary.innerHTML = `Available Cash: <strong>${money(s.account?.cash)}</strong> | `
      + `Total Paper Equity: <strong>${money(s.account?.equity)}</strong> | `
      + `Daily P&L: <strong class="${(s.account?.daily_pnl || 0) >= 0 ? 'positive' : 'negative'}">${money(s.account?.daily_pnl)}</strong> | `
      + `Drawdown: <strong>${(s.account?.drawdown_pct || 0).toFixed(2)}%</strong>`;
  }
  text('broker', s.snapshot?.broker_account ? JSON.stringify(s.snapshot.broker_account, null, 2) : 'No authenticated Angel One broker connected');

  // 6. Runner Card Controls
  const isKilled = Boolean(s.account?.kill_switch);
  const isRunning = s.status === 'running';

  text('state', isKilled ? 'Emergency Stop' : (isRunning ? 'Running' : 'Paused'));
  const stateEl = $('state');
  if (stateEl) {
    stateEl.className = isKilled ? 'pill negative' : (isRunning ? 'pill positive' : 'pill');
  }

  text('instrument', `${s.instrument?.exchange || 'NSE'} / ${s.instrument?.symbol || 'DEMO-EQ'}`);
  text('risk', s.risk?.risk_state || 'Waiting');

  if ($('start')) $('start').disabled = isRunning || isKilled;
  if ($('stop')) $('stop').disabled = !isRunning;
  if ($('reset')) $('reset').hidden = !isKilled;

  text('source-note', s.source === 'demo'
    ? 'DEMO DATA • Synthetic prices and a simulated paper session. No strategy signal is fabricated.'
    : 'ANGEL READ-ONLY DATA • Real broker market feed; execution is 100% paper simulation.');

  // 7. Advanced Facts & Audit Trail
  facts('market-facts', [
    ['Session', s.market_open ? 'Open (09:15 - 15:30 IST)' : 'Closed'],
    ['Feed', s.connection || 'connected'],
    ['VIX', s.snapshot?.vix?.toFixed(2) || '—'],
    ['ATR ratio', s.snapshot?.atr_ratio?.toFixed(3) || '—'],
    ['Trend strength', s.snapshot?.trend_strength?.toFixed(3) || '—'],
    ['Trading gate', s.allow_trade ? 'Eligible for trade checks' : 'Blocked / Monitoring']
  ]);

  text('reasons', s.risk?.reasons?.join(' · ') || 'Risk metrics within configured limits');

  facts('decision-facts', [
    ['Latest signal', s.last_decision || 'No decision'],
    ['Cycle status', s.last_result?.status || 'stopped'],
    ['Reason', s.last_result?.reason || s.last_error || '—'],
    ['Scheduler interval', `${s.interval_seconds || 60}s`],
    ['Errors', s.error_count || 0],
    ['Last paper order', s.last_result?.order_id || '—']
  ]);

  text('audit', s.events && s.events.length
    ? s.events.map(e => `${e.timestamp}  ${e.event}\n${JSON.stringify(e.details)}`).join('\n\n')
    : 'No audit events logged yet');

  // 8. AI Card Updates
  if (s.ai) {
    const isBusy = Boolean(s.ai.worker_busy || s.ai.state === 'analyzing');
    const aiDot = $('ai-dot');
    if (aiDot) {
      aiDot.className = `status-dot ${isBusy ? 'busy' : (s.ai.state === 'ready' || s.ai.state === 'complete' ? 'ready' : '')}`;
    }
    const analyzeBtn = $('analyze');
    if (analyzeBtn) {
      analyzeBtn.disabled = isBusy || (selectedSymbol === 'DEMO-EQ' || selectedSymbol === 'DEMO');
      if (isBusy) {
        analyzeBtn.innerHTML = 'Analyzing stock… <span style="display:inline-block;">⏳</span>';
      } else {
        analyzeBtn.innerHTML = 'Analyze stock <span>→</span>';
      }
    }
    text('engine', s.ai.configured ? `${s.ai.provider || 'AI'} • ${s.ai.model || 'Configured'}` : 'Setup required');
    text('ai-progress', s.ai.progress || 'AI ko connect karein. Phir selected stock ka analysis yahin milega.');

    const disBtn = $('disconnect-ai');
    if (disBtn) disBtn.hidden = !s.ai.configured;

    const resBox = $('ai-result');
    const rptPanel = $('report-panel');
    if (s.ai.result) {
      if (resBox) resBox.hidden = false;
      const decision = s.ai.result.decision || s.ai.result.signal || 'HOLD';
      text('ai-signal', decision);
      const sigEl = $('ai-signal');
      if (sigEl) {
        const dUp = decision.toUpperCase();
        sigEl.style.color = dUp.includes('BUY') ? '#089981' : (dUp.includes('SELL') ? '#f23645' : '#2962ff');
      }
      text('ai-summary', s.ai.result.summary || '');
      if (s.ai.result.reports && rptPanel) {
        rptPanel.hidden = false;
        $('reports').innerHTML = Object.entries(s.ai.result.reports)
          .map(([k, v]) => `<details open style="margin-bottom:12px;"><summary style="cursor:pointer;padding:8px 12px;font-weight:700;background:#f0f3f8;border-radius:4px;margin-bottom:6px;">${k.replace(/_/g, ' ').toUpperCase()}</summary><pre style="white-space:pre-wrap;word-break:break-word;font-family:inherit;font-size:13px;line-height:1.6;padding:12px;background:#ffffff;border:1px solid #e0e3eb;border-radius:4px;">${v}</pre></details>`)
          .join('');
      }
    } else {
      if (resBox) resBox.hidden = true;
      if (rptPanel) rptPanel.hidden = true;
    }
  }

  // 9. Sync Live Feed Ticks into Chart if symbol matches
  if (s.snapshot && s.snapshot.price && selectedSymbol === (s.instrument?.symbol || 'DEMO-EQ')) {
    text('price', money(s.snapshot.price));
    text('updated', s.snapshot.timestamp ? new Date(s.snapshot.timestamp).toLocaleTimeString() : 'Live');
    const p = Number(s.snapshot.price);
    if (!isNaN(p) && p > 0 && latestCandle && tvChart) {
      latestCandle.close = p;
      latestCandle.high = Math.max(latestCandle.high, p);
      latestCandle.low = Math.min(latestCandle.low, p);
      if (candlestickSeries) candlestickSeries.update(latestCandle);
      if (areaSeries) areaSeries.update({ time: latestCandle.time, value: p });
      if (lineSeries) lineSeries.update({ time: latestCandle.time, value: p });
      if (barSeries) barSeries.update(latestCandle);
      updateLegend(latestCandle);
    }
  }

  text('poll', `Updated ${new Date().toLocaleTimeString()}`);
}

function applyLiveTick(tickData) {
  const p = Number(tickData.ltp);
  if (!p || isNaN(p) || !latestCandle || !tvChart) return;
  text('price', money(p));
  latestCandle.close = p;
  latestCandle.high = Math.max(latestCandle.high, p);
  latestCandle.low = Math.min(latestCandle.low, p);
  if (candlestickSeries) candlestickSeries.update(latestCandle);
  if (areaSeries) areaSeries.update({ time: latestCandle.time, value: p });
  if (lineSeries) lineSeries.update({ time: latestCandle.time, value: p });
  if (barSeries) barSeries.update(latestCandle);
  updateLegend(latestCandle);
  const statusBadge = $('market-status-badge');
  if (statusBadge && tickData.is_market_open) {
    statusBadge.className = 'market-status-badge badge-live';
    statusBadge.textContent = 'LIVE';
  }
}

async function refresh() {
  try {
    const res = await fetch('/api/status');
    if (!res.ok) throw new Error('Status unavailable');
    const data = await res.json();
    render(data);

    // Fetch fresh quote tick for active selected symbol
    if (selectedSymbol && selectedSymbol !== 'DEMO' && selectedSymbol !== 'DEMO-EQ') {
      try {
        const tickRes = await fetch(`/api/tick?symbol=${encodeURIComponent(selectedSymbol)}`);
        if (tickRes.ok) {
          const tickData = await tickRes.json();
          if (tickData.ltp && selectedSymbol === currentChartSymbol) {
            applyLiveTick(tickData);
          }
        }
      } catch (_) {}
    }
  } catch (err) {
    text('poll', 'Connecting…');
  }
}

// =============================================================================
// App Initialization
// =============================================================================
function initApp() {
  initTabs();
  initWatchlist();
  initChart();
  initAI();
  initActions();

  // Load initial stock chart
  selectStock(selectedSymbol);

  // Initial status fetch & interval polling
  refresh();
  setInterval(refresh, 3000);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initApp);
} else {
  initApp();
}
