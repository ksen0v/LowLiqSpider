'use strict';

/* LowLiqSpider — интерфейс: сетка графиков по алертам, лента, настройки. */

const LC = window.LightweightCharts;
const $ = (sel, root = document) => root.querySelector(sel);

function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else if (k === 'dataset') Object.assign(node.dataset, v);
    else node.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

const DET_COLORS = {
  ping_pong: '#f0b90b',
  spread: '#9d7bff',
  wicks: '#29b6f6',
  volume_spike: '#ff9800',
  price_change: '#ec407a',
  manual: '#848e9c',
};
const STEP = { '1m': 60, '5m': 300, '15m': 900, '1h': 3600 };
// lightweight-charts рисует время в UTC, сдвигаем в локальное
const TZ_SHIFT = -new Date().getTimezoneOffset() * 60;
const GRID_PRESETS = [[1, 1], [1, 2], [2, 2], [2, 3], [3, 3], [3, 4], [4, 4]];

const state = {
  meta: null,
  settings: null,
  schema: null,
  slots: [],
  feed: [],
  seen: new Set(),
  filters: { exchanges: new Set(), detectors: new Set() },
  unseen: 0,
  maximized: null,
  ws: null,
  wsDelay: 1000,
  lastStatus: null,
};

const store = {
  get(key, fallback) {
    try {
      const v = localStorage.getItem('spider.' + key);
      return v ? JSON.parse(v) : fallback;
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem('spider.' + key, JSON.stringify(value));
    } catch {
      /* приватный режим или переполнение — не критично */
    }
  },
};

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts });
  let body = null;
  try {
    body = await res.json();
  } catch {
    body = null;
  }
  if (!res.ok) {
    const msg = typeof body?.detail === 'string' ? body.detail : `HTTP ${res.status}`;
    const err = new Error(msg);
    err.status = res.status;
    err.body = body;
    throw err;
  }
  return body;
}

// ------------------------------------------------------------------ форматирование

function precisionFor(p) {
  if (!p || !isFinite(p)) return 4;
  if (p >= 1000) return 2;
  if (p >= 1) return 4;
  return Math.min(10, Math.ceil(-Math.log10(p)) + 3);
}

function fmtPrice(p) {
  if (p === null || p === undefined) return '—';
  return Number(p).toFixed(precisionFor(Math.abs(p)));
}

function fmtQty(q) {
  if (q >= 1e6) return (q / 1e6).toFixed(2) + 'M';
  if (q >= 1e4) return (q / 1e3).toFixed(1) + 'K';
  if (q >= 100) return q.toFixed(0);
  if (q >= 1) return q.toFixed(2);
  return q.toPrecision(3);
}

function fmtUsd(v) {
  if (v === null || v === undefined) return '—';
  if (v >= 1e6) return '$' + (v / 1e6).toFixed(2) + 'M';
  if (v >= 1e3) return '$' + (v / 1e3).toFixed(1) + 'K';
  return '$' + v.toFixed(0);
}

function fmtTime(sec) {
  return new Date(sec * 1000).toLocaleTimeString('ru-RU', { hour12: false });
}

function ago(sec) {
  const d = Math.max(0, Date.now() / 1000 - sec);
  if (d < 60) return `${Math.floor(d)} с`;
  if (d < 3600) return `${Math.floor(d / 60)} мин`;
  if (d < 86400) return `${Math.floor(d / 3600)} ч`;
  return `${Math.floor(d / 86400)} д`;
}

function exchangeName(id) {
  return state.meta?.exchanges.find((e) => e.id === id)?.name || id.toUpperCase();
}

function detectorShort(key) {
  if (key === 'manual') return 'ВРУЧНУЮ';
  return state.meta?.detectors.find((d) => d.key === key)?.short || key;
}

// ------------------------------------------------------------------ звук

let audioCtx = null;
let lastBeep = 0;
const TONES = {
  ping_pong: [880, 1320],
  spread: [660, 990],
  wicks: [990, 740],
  volume_spike: [523, 784],
  price_change: [740, 1110],
};

function ensureAudio() {
  if (!audioCtx) {
    try {
      audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    } catch {
      return;
    }
  }
  if (audioCtx.state === 'suspended') audioCtx.resume().then(renderSoundButton);
}

function beep(detector) {
  if (!state.settings?.ui.sound || !audioCtx || audioCtx.state !== 'running') return;
  const now = performance.now();
  if (now - lastBeep < 350) return;
  lastBeep = now;
  const [f1, f2] = TONES[detector] || [800, 1000];
  const t = audioCtx.currentTime;
  [[f1, 0], [f2, 0.13]].forEach(([freq, offset]) => {
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.type = 'sine';
    osc.frequency.value = freq;
    gain.gain.setValueAtTime(0.0001, t + offset);
    gain.gain.exponentialRampToValueAtTime(0.25, t + offset + 0.01);
    gain.gain.exponentialRampToValueAtTime(0.0001, t + offset + 0.12);
    osc.connect(gain).connect(audioCtx.destination);
    osc.start(t + offset);
    osc.stop(t + offset + 0.13);
  });
}

function renderSoundButton() {
  const btn = $('#sound-btn');
  const on = state.settings?.ui.sound;
  const blocked = on && (!audioCtx || audioCtx.state !== 'running');
  btn.textContent = on ? (blocked ? '🔈' : '🔔') : '🔕';
  btn.title = on
    ? blocked
      ? 'Звук включён, но браузер ждёт клика по странице'
      : 'Звук включён'
    : 'Звук выключен';
}

// ------------------------------------------------------------------ карточка

class Card {
  constructor({ exchange, symbol, base, quote, url, alerts = [], pinned = false, interval = null, manual = false }) {
    this.exchange = exchange;
    this.symbol = symbol;
    this.base = base;
    this.quote = quote;
    this.url = url || '';
    this.alerts = [...alerts].sort((a, b) => a.ts - b.ts);
    this.pinned = pinned;
    this.manual = manual;
    this.interval = interval || state.settings.ui.chart_interval;
    this.selectedId = this.alerts.length ? this.alerts[this.alerts.length - 1].id : null;
    this.createdAt = Date.now() / 1000;
    this.priceLines = [];
    this.timer = null;
    this.destroyed = false;
    this.precision = null;
    this.candles = [];
    this.build();
  }

  get key() {
    return `${this.exchange}:${this.symbol}`;
  }

  get lastTs() {
    return this.alerts.length ? this.alerts[this.alerts.length - 1].ts : this.createdAt;
  }

  get selected() {
    return this.alerts.find((a) => a.id === this.selectedId) || this.alerts[this.alerts.length - 1] || null;
  }

  build() {
    this.badgesEl = h('div', { class: 'det-badges' });
    this.ageEl = h('span', { class: 'age' });
    this.priceEl = h('span', { class: 'last-price' });
    this.pinBtn = h('button', { class: 'btn', title: 'Закрепить: карточку не вытеснят новые алерты', onclick: () => this.togglePin() }, '📌');
    this.intervalsEl = h(
      'div',
      { class: 'intervals' },
      Object.keys(STEP).map((iv) =>
        h('button', { class: iv === this.interval ? 'on' : '', onclick: () => this.setInterval(iv), dataset: { iv } }, iv),
      ),
    );
    this.reasonEl = h('div', { class: 'card-reason', title: 'Нажмите, чтобы развернуть', onclick: () => this.reasonEl.classList.toggle('open') });
    this.chartEl = h('div', { class: 'chart' });
    this.msgEl = h('div', { class: 'chart-msg', text: 'Загрузка…' });
    this.chartEl.append(this.msgEl, this.intervalsEl);
    this.tapeRows = h('div', { class: 'tape-rows' });
    this.tapeEl = h(
      'div',
      { class: 'tape' },
      h('div', { class: 'tape-head' }, h('span', {}, 'Время'), h('span', {}, 'Цена'), h('span', {}, 'Объём')),
      this.tapeRows,
    );

    this.root = h(
      'div',
      { class: 'card' },
      h(
        'div',
        { class: 'card-head', ondblclick: (e) => !e.target.closest('button,a') && toggleMaximize(this) },
        h('span', { class: `ex-badge ${this.exchange}` }, exchangeName(this.exchange)),
        h(
          'a',
          { class: 'pair', href: this.url || '#', target: '_blank', rel: 'noopener', title: 'Открыть пару на бирже' },
          this.base,
          h('span', { class: 'q' }, '/' + this.quote),
        ),
        this.priceEl,
        this.badgesEl,
        this.ageEl,
        h(
          'div',
          { class: 'card-actions' },
          h('a', { class: 'btn open-book', href: this.url || '#', target: '_blank', rel: 'noopener', title: 'Открыть стакан на бирже' }, 'Стакан ↗'),
          this.pinBtn,
          h('button', { class: 'btn', title: 'Развернуть на всю сетку (или двойной клик по заголовку)', onclick: () => toggleMaximize(this) }, '⤢'),
          h('button', { class: 'btn', title: 'Убрать из сетки', onclick: () => removeCard(this) }, '✕'),
        ),
      ),
      this.reasonEl,
      h('div', { class: 'card-body' }, this.chartEl, this.tapeEl),
    );

    this.chart = LC.createChart(this.chartEl, {
      autoSize: true,
      // атрибуция TradingView вынесена в подвал ленты, чтобы логотип не закрывал свечи в мелких карточках
      layout: { background: { type: 'solid', color: '#14181d' }, textColor: '#848e9c', fontSize: 11, attributionLogo: false },
      grid: { vertLines: { color: '#1c2128' }, horzLines: { color: '#1c2128' } },
      rightPriceScale: { borderColor: '#262c34' },
      timeScale: { borderColor: '#262c34', timeVisible: true, secondsVisible: false, rightOffset: 3 },
      crosshair: { mode: 0 },
      // не полагаемся на navigator.language: у некоторых систем там невалидный тег вроде en-US@posix
      localization: { locale: 'ru-RU' },
    });
    this.candleSeries = this.chart.addSeries(LC.CandlestickSeries, {
      upColor: '#0ecb81',
      downColor: '#f6465d',
      borderVisible: false,
      wickUpColor: '#0ecb81',
      wickDownColor: '#f6465d',
    });
    this.candleSeries.priceScale().applyOptions({ scaleMargins: { top: 0.08, bottom: 0.24 } });
    this.volumeSeries = this.chart.addSeries(LC.HistogramSeries, {
      priceFormat: { type: 'volume' },
      priceScaleId: 'vol',
      lastValueVisible: false,
      priceLineVisible: false,
    });
    this.chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
    this.markers = LC.createSeriesMarkers(this.candleSeries, []);

    this.renderMeta();
    this.updateTapeVisibility();
  }

  mount(slotEl) {
    slotEl.classList.remove('empty');
    slotEl.replaceChildren(this.root);
    if (!this.timer) this.loop();
  }

  loop() {
    if (this.destroyed) return;
    this.refresh().finally(() => {
      if (this.destroyed) return;
      this.timer = setTimeout(() => this.loop(), state.settings.ui.card_refresh_sec * 1000);
    });
  }

  async refresh(force = false) {
    if (document.hidden && !force) return;
    const qs = `exchange=${encodeURIComponent(this.exchange)}&symbol=${encodeURIComponent(this.symbol)}`;
    const wantTape = state.settings.ui.show_tape;
    const [candles, trades] = await Promise.allSettled([
      api(`/api/candles?${qs}&interval=${this.interval}&limit=${state.settings.ui.chart_candles}`),
      wantTape ? api(`/api/trades?${qs}&limit=40`) : Promise.resolve(null),
    ]);
    if (this.destroyed) return;
    if (candles.status === 'fulfilled') this.setCandles(candles.value);
    else this.showMsg(`Не удалось загрузить свечи: ${candles.reason.message}`);
    if (trades.status === 'fulfilled' && trades.value) this.setTrades(trades.value);
  }

  showMsg(text) {
    this.msgEl.textContent = text;
    this.msgEl.hidden = !text;
  }

  setCandles(rows) {
    if (!rows.length) {
      this.showMsg('Нет свечей');
      return;
    }
    this.showMsg('');
    const last = rows[rows.length - 1].close;
    const precision = precisionFor(last);
    if (precision !== this.precision) {
      this.precision = precision;
      this.candleSeries.applyOptions({ priceFormat: { type: 'price', precision, minMove: Math.pow(10, -precision) } });
    }
    this.candles = rows;
    this.candleSeries.setData(
      rows.map((c) => ({ time: c.time + TZ_SHIFT, open: c.open, high: c.high, low: c.low, close: c.close })),
    );
    this.volumeSeries.setData(
      rows.map((c) => ({
        time: c.time + TZ_SHIFT,
        value: c.volume,
        color: c.close >= c.open ? 'rgba(14,203,129,.45)' : 'rgba(246,70,93,.45)',
      })),
    );
    this.priceEl.textContent = fmtPrice(last);
    this.renderMarkers();
  }

  setTrades(trades) {
    // подсвечиваем повторяющиеся объёмы (±1.5%) — так пинг-понг видно глазами
    const sorted = trades.map((t, i) => [t.qty, i]).sort((a, b) => a[0] - b[0]);
    const repeated = new Set();
    for (let k = 1; k < sorted.length; k++) {
      const [prev, pi] = sorted[k - 1];
      const [cur, ci] = sorted[k];
      if (cur - prev <= cur * 0.015) {
        repeated.add(pi);
        repeated.add(ci);
      }
    }
    this.tapeRows.replaceChildren(
      ...trades.map((t, i) =>
        h(
          'div',
          { class: `tape-row ${t.side}${repeated.has(i) ? ' rep' : ''}` },
          h('span', { class: 't' }, fmtTime(t.ts / 1000)),
          h('span', { class: 'p' }, fmtPrice(t.price)),
          h('span', { class: 'q' }, fmtQty(t.qty)),
        ),
      ),
    );
  }

  updateTapeVisibility() {
    this.tapeEl.hidden = !state.settings.ui.show_tape;
  }

  setInterval(iv) {
    if (iv === this.interval) return;
    this.interval = iv;
    this.intervalsEl.querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.dataset.iv === iv));
    this.precision = null;
    this.candleSeries.setData([]);
    this.volumeSeries.setData([]);
    this.showMsg('Загрузка…');
    this.refresh(true).then(() => this.chart.timeScale().fitContent());
    saveGrid();
  }

  addAlert(alert, { flash = true } = {}) {
    if (this.alerts.some((a) => a.id === alert.id)) return;
    this.alerts.push(alert);
    this.alerts.sort((a, b) => a.ts - b.ts);
    if (this.alerts.length > 30) this.alerts.splice(0, this.alerts.length - 30);
    this.selectedId = alert.id;
    if (alert.url) this.url = alert.url;
    this.renderMeta();
    this.renderMarkers();
    if (flash) this.flash(alert.detector);
  }

  flash(detector) {
    this.root.style.setProperty('--flash', DET_COLORS[detector] || '#f0b90b');
    this.root.classList.remove('flash');
    void this.root.offsetWidth;
    this.root.classList.add('flash');
  }

  select(id) {
    this.selectedId = id;
    this.renderMeta();
  }

  togglePin() {
    this.pinned = !this.pinned;
    this.renderMeta();
    saveGrid();
  }

  renderMeta() {
    // бейджи детекторов: по одному на тип, с количеством срабатываний
    const byDet = new Map();
    for (const a of this.alerts) {
      const entry = byDet.get(a.detector) || { count: 0, last: a };
      entry.count += 1;
      entry.last = a;
      byDet.set(a.detector, entry);
    }
    const sel = this.selected;
    const badges = [...byDet.entries()].map(([det, { count, last }]) => {
      const color = DET_COLORS[det] || '#848e9c';
      const isSel = sel && sel.detector === det;
      return h(
        'button',
        {
          class: 'det' + (isSel ? ' sel' : ''),
          style: `color:${color};${isSel ? `background:${color}` : ''}`,
          title: `${last.detector_name}: ${last.title}`,
          onclick: () => this.select(last.id),
        },
        detectorShort(det),
        count > 1 ? h('span', { class: 'n' }, `×${count}`) : null,
      );
    });
    if (this.manual && !this.alerts.length) {
      badges.push(h('span', { class: 'det sel', style: `color:#000;background:${DET_COLORS.manual}` }, 'ВРУЧНУЮ'));
    }
    this.badgesEl.replaceChildren(...badges);

    if (sel) {
      this.reasonEl.replaceChildren(h('b', {}, sel.title), ' · ', sel.reason);
      this.reasonEl.title = `${sel.detector_name} · ${fmtTime(sel.ts)}\n\n${sel.reason}`;
    } else {
      this.reasonEl.replaceChildren(h('b', {}, 'Открыто вручную'), ' · алертов по паре пока не было');
    }
    for (const a of this.root.querySelectorAll('a.pair, a.open-book')) a.href = this.url || '#';
    this.pinBtn.classList.toggle('on', this.pinned);
    this.root.classList.toggle('pinned', this.pinned);
    this.updateAge();
    this.renderLevels();
  }

  renderLevels() {
    for (const line of this.priceLines) this.candleSeries.removePriceLine(line);
    this.priceLines = [];
    const sel = this.selected;
    if (!sel) return;
    for (const lv of sel.levels || []) {
      this.priceLines.push(
        this.candleSeries.createPriceLine({
          price: lv.price,
          color: lv.color,
          lineWidth: 1,
          lineStyle: 2,
          axisLabelVisible: true,
          title: lv.label,
        }),
      );
    }
  }

  renderMarkers() {
    if (!this.candles.length) return;
    const step = STEP[this.interval];
    const first = this.candles[0].time;
    const last = this.candles[this.candles.length - 1].time;
    const markers = this.alerts
      .map((a) => ({ a, t: Math.floor(a.ts / step) * step }))
      .filter(({ t }) => t >= first && t <= last)
      .map(({ a, t }) => ({
        time: t + TZ_SHIFT,
        position: 'aboveBar',
        color: DET_COLORS[a.detector] || '#f0b90b',
        shape: 'arrowDown',
        text: detectorShort(a.detector),
      }))
      .sort((x, y) => x.time - y.time);
    this.markers.setMarkers(markers);
  }

  updateAge() {
    this.ageEl.textContent = this.alerts.length ? ago(this.lastTs) : '';
    this.ageEl.title = this.alerts.length ? `Последний алерт в ${fmtTime(this.lastTs)}` : '';
  }

  destroy() {
    this.destroyed = true;
    clearTimeout(this.timer);
    this.chart.remove();
    this.root.remove();
  }

  toJSON() {
    return {
      exchange: this.exchange,
      symbol: this.symbol,
      base: this.base,
      quote: this.quote,
      url: this.url,
      alerts: this.alerts.slice(-10),
      pinned: this.pinned,
      interval: this.interval,
      manual: this.manual,
    };
  }
}

// ------------------------------------------------------------------ сетка

function gridSize() {
  return { rows: state.settings.ui.grid_rows, cols: state.settings.ui.grid_cols };
}

function saveGrid() {
  store.set('grid', state.slots.map((c) => (c ? c.toJSON() : null)));
}

function renderGrid() {
  const { rows, cols } = gridSize();
  const n = rows * cols;
  if (state.slots.length > n) {
    // при уменьшении сетки оставляем закреплённые и самые свежие
    const cards = state.slots.filter(Boolean).sort((a, b) => b.pinned - a.pinned || b.lastTs - a.lastTs);
    cards.slice(n).forEach((c) => c.destroy());
    state.slots = cards.slice(0, n);
  }
  while (state.slots.length < n) state.slots.push(null);

  const grid = $('#grid');
  grid.style.gridTemplateColumns = `repeat(${cols}, minmax(0, 1fr))`;
  grid.style.gridTemplateRows = `repeat(${rows}, minmax(0, 1fr))`;
  const slotEls = [...grid.children];
  while (slotEls.length < n) {
    const s = h('div', { class: 'slot' });
    grid.append(s);
    slotEls.push(s);
  }
  while (slotEls.length > n) slotEls.pop().remove();
  state.slots.forEach((card, i) => mountSlot(i, slotEls[i]));
  if (state.maximized && !state.slots.includes(state.maximized)) toggleMaximize(null);
  saveGrid();
}

function mountSlot(i, slotEl = $('#grid').children[i]) {
  const card = state.slots[i];
  slotEl.classList.toggle('max', !!card && card === state.maximized);
  if (card) {
    if (card.root.parentElement !== slotEl) card.mount(slotEl);
  } else {
    slotEl.classList.add('empty');
    slotEl.replaceChildren(h('span', {}, 'Ждём алерт…'));
  }
}

function findCard(key) {
  return state.slots.find((c) => c && c.key === key) || null;
}

/** Куда поставить новую карточку: пустой слот или самая старая незакреплённая. */
function freeSlot() {
  let idx = state.slots.findIndex((s) => !s);
  if (idx >= 0) return idx;
  let best = -1;
  state.slots.forEach((c, i) => {
    if (!c.pinned && c !== state.maximized && (best < 0 || c.lastTs < state.slots[best].lastTs)) best = i;
  });
  if (best >= 0) state.slots[best].destroy();
  return best;
}

function placeCard(card) {
  const idx = freeSlot();
  if (idx < 0) {
    card.destroy();
    return false;
  }
  state.slots[idx] = card;
  mountSlot(idx);
  saveGrid();
  return true;
}

function removeCard(card) {
  const idx = state.slots.indexOf(card);
  if (card === state.maximized) toggleMaximize(null);
  card.destroy();
  if (idx >= 0) {
    state.slots[idx] = null;
    mountSlot(idx);
  }
  saveGrid();
}

function toggleMaximize(card) {
  const grid = $('#grid');
  state.maximized = card && state.maximized !== card ? card : null;
  grid.classList.toggle('maximized', !!state.maximized);
  [...grid.children].forEach((s, i) => s.classList.toggle('max', !!state.maximized && state.slots[i] === state.maximized));
}

function restoreGrid() {
  const saved = store.get('grid', []);
  const known = new Set(state.meta.exchanges.map((e) => e.id));
  state.slots = (Array.isArray(saved) ? saved : []).map((c) => {
    if (!c || !known.has(c.exchange)) return null;
    try {
      return new Card(c);
    } catch {
      return null;
    }
  });
}

// ------------------------------------------------------------------ алерты

function passesFilter(alert) {
  return state.filters.exchanges.has(alert.exchange) && state.filters.detectors.has(alert.detector);
}

function handleAlert(alert, { live = true } = {}) {
  if (state.seen.has(alert.id)) return;
  state.seen.add(alert.id);
  state.feed.unshift(alert);
  if (state.feed.length > 500) state.feed.length = 500;
  prependFeedItem(alert, live);
  if (!passesFilter(alert)) return;

  const card = findCard(`${alert.exchange}:${alert.symbol}`);
  if (card) {
    card.addAlert(alert, { flash: live });
    saveGrid();
  } else {
    placeCard(new Card({ ...alert, alerts: [alert] }));
  }
  if (live) {
    beep(alert.detector);
    if (document.hidden) {
      state.unseen += 1;
      document.title = `(${state.unseen}) LowLiqSpider`;
    }
  }
}

function openFromAlert(alert) {
  const card = findCard(`${alert.exchange}:${alert.symbol}`);
  if (card) {
    card.addAlert(alert, { flash: false });
    card.select(alert.id);
    card.flash(alert.detector);
    return;
  }
  const c = new Card({ ...alert, alerts: [alert] });
  if (!placeCard(c)) alertBox('Все карточки закреплены — открепите одну, чтобы освободить место.');
}

function alertBox(text) {
  window.alert(text);
}

// ------------------------------------------------------------------ лента алертов

function feedItem(alert, fresh) {
  const color = DET_COLORS[alert.detector] || '#848e9c';
  return h(
    'div',
    {
      class: 'feed-item' + (fresh ? ' new' : ''),
      dataset: { id: alert.id },
      title: alert.reason,
      onclick: () => openFromAlert(alert),
    },
    h('span', { class: `ex-badge ${alert.exchange}` }, exchangeName(alert.exchange)),
    h(
      'span',
      {},
      h('span', { class: 'pair' }, alert.base, h('span', { class: 'q' }, '/' + alert.quote)),
      ' ',
      h('span', { class: 'det', style: `color:${color}` }, detectorShort(alert.detector)),
    ),
    h('span', { class: 'time' }, fmtTime(alert.ts)),
    h('span', { class: 'title' }, alert.title),
  );
}

function prependFeedItem(alert, fresh) {
  if (!passesFilter(alert)) return;
  const list = $('#feed-list');
  $('.feed-empty', list)?.remove();
  list.prepend(feedItem(alert, fresh));
  while (list.children.length > 300) list.lastElementChild.remove();
  updateFeedCount();
}

function renderFeed() {
  const items = state.feed.filter(passesFilter).slice(0, 300);
  const list = $('#feed-list');
  list.replaceChildren(...items.map((a) => feedItem(a, false)));
  if (!items.length) list.append(h('div', { class: 'feed-empty' }, 'Алертов пока нет'));
  updateFeedCount();
}

function updateFeedCount() {
  $('#feed-count').textContent = String(state.feed.filter(passesFilter).length);
}

// ------------------------------------------------------------------ статус

function renderStatus(st) {
  state.lastStatus = st;
  const pills = [];
  for (const ex of st.exchanges) {
    let dot = 'ok';
    if (!ex.enabled) dot = 'off';
    else if (!ex.last_tickers_at || Date.now() / 1000 - ex.last_tickers_at > 60) dot = 'err';
    else if (ex.errors_5m > 0) dot = 'warn';
    const s = ex.universe_stats || {};
    const tip = [
      `${ex.name}: торгуемых пар ${ex.symbols}`,
      `в скане: ${ex.universe}`,
      `отсеяно — есть на ликвидных: ${s.liquid || 0}, малый объём: ${s.low_volume || 0}, большой объём: ${s.high_volume || 0}, плечевые токены: ${s.leveraged || 0}, чёрный список: ${s.blacklist || 0}`,
      `глубоких сканов в минуту: ${ex.scans_per_min}` + (ex.pass_minutes ? `, полный круг ≈ ${ex.pass_minutes} мин` : ''),
      `горячих в очереди: ${ex.hot}`,
      `алертов с запуска: ${ex.alerts}`,
      ex.errors_5m ? `ошибок за 5 мин: ${ex.errors_5m}\nпоследняя: ${ex.last_error}` : '',
    ]
      .filter(Boolean)
      .join('\n');
    pills.push(
      h(
        'span',
        { class: 'pill', title: tip },
        h('span', { class: `dot ${dot}` }),
        h('b', {}, ex.name),
        ex.enabled ? `${ex.universe} монет · ${ex.scans_per_min}/мин` : 'выключена',
      ),
    );
  }
  if (!st.demo) {
    const total = st.liquid.length;
    const loaded = st.liquid.filter((l) => l.count > 0).length;
    const failed = st.liquid.filter((l) => l.error);
    pills.push(
      h(
        'span',
        {
          class: 'pill',
          title:
            st.liquid.map((l) => `${l.name}: ${l.count}${l.error ? ' — ошибка: ' + l.error : ''}`).join('\n') ||
            'Ни одна ликвидная биржа не включена',
        },
        h('span', { class: `dot ${failed.length ? (loaded ? 'warn' : 'err') : 'ok'}` }),
        `Ликвидные ${loaded}/${total}`,
      ),
    );
  }
  if (st.telegram.enabled) {
    pills.push(
      h(
        'span',
        { class: 'pill', title: st.telegram.last_error || (st.telegram.configured ? 'Telegram подключён' : 'Не указан токен или chat_id') },
        h('span', { class: `dot ${st.telegram.last_error || !st.telegram.configured ? 'err' : 'ok'}` }),
        'Telegram',
      ),
    );
  }
  $('#status').replaceChildren(...pills);
  $('#demo-badge').hidden = !st.demo;
}

// ------------------------------------------------------------------ websocket

function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  state.ws = ws;
  ws.onopen = () => {
    state.wsDelay = 1000;
    $('#conn').classList.add('ok');
    catchUp();
  };
  ws.onmessage = (ev) => {
    let msg;
    try {
      msg = JSON.parse(ev.data);
    } catch {
      return;
    }
    if (msg.type === 'alert') handleAlert(msg.alert);
    else if (msg.type === 'status') renderStatus(msg.status);
  };
  ws.onclose = () => {
    $('#conn').classList.remove('ok');
    setTimeout(connectWS, state.wsDelay);
    state.wsDelay = Math.min(state.wsDelay * 2, 10000);
  };
}

/** После переподключения подтягиваем алерты, пришедшие, пока связи не было. */
async function catchUp() {
  try {
    const alerts = await api('/api/alerts?limit=100');
    const missed = alerts.filter((a) => !state.seen.has(a.id)).reverse();
    missed.forEach((a, i) => handleAlert(a, { live: i === missed.length - 1 }));
  } catch {
    /* сервер ещё поднимается */
  }
}

// ------------------------------------------------------------------ шапка

function renderFilters() {
  const exWrap = $('#exchange-filters');
  exWrap.replaceChildren(
    ...state.meta.exchanges.map((ex) =>
      chip(ex.name, ex.id === 'mexc' ? '#2f8cff' : '#17e6a1', state.filters.exchanges, ex.id),
    ),
  );
  const detWrap = $('#detector-filters');
  detWrap.replaceChildren(...state.meta.detectors.map((d) => chip(d.short, DET_COLORS[d.key], state.filters.detectors, d.key, d.name)));
}

function chip(label, color, set, value, title) {
  const on = set.has(value);
  return h(
    'button',
    {
      class: 'chip' + (on ? ' on' : ''),
      style: on ? `background:${color}` : `color:${color}88`,
      title: title || label,
      onclick: () => {
        if (set.has(value)) set.delete(value);
        else set.add(value);
        store.set('filters', { exchanges: [...state.filters.exchanges], detectors: [...state.filters.detectors] });
        renderFilters();
        renderFeed();
      },
    },
    label,
  );
}

function renderGridSelect() {
  const sel = $('#grid-size');
  const { rows, cols } = gridSize();
  const presets = GRID_PRESETS.some(([r, c]) => r === rows && c === cols) ? GRID_PRESETS : [...GRID_PRESETS, [rows, cols]];
  sel.replaceChildren(
    ...presets.map(([r, c]) => h('option', { value: `${r}x${c}`, selected: r === rows && c === cols }, `${r}×${c}`)),
  );
}

async function saveSettingsPartial(mutate) {
  const next = structuredClone(state.settings);
  mutate(next);
  state.settings = await api('/api/settings', { method: 'PUT', body: JSON.stringify(next) });
  applySettings();
}

function applySettings() {
  renderGridSelect();
  renderSoundButton();
  renderGrid();
  state.slots.forEach((c) => c && c.updateTapeVisibility());
}

// ------------------------------------------------------------------ настройки

const TABS = [
  { key: 'mexc', title: 'MEXC' },
  { key: 'gate', title: 'Gate' },
  { key: 'liquid', title: 'Ликвидные биржи' },
  { key: 'telegram', title: 'Telegram' },
  { key: 'ui', title: 'Интерфейс' },
];
const ENUM_LABELS = { both: 'Любое', up: 'Только рост', down: 'Только падение' };

function resolveRef(schema) {
  if (!schema) return {};
  if (schema.$ref) return state.schema.$defs[schema.$ref.split('/').pop()];
  if (schema.allOf && schema.allOf.length === 1) return resolveRef(schema.allOf[0]);
  return schema;
}

function isObjectSchema(s) {
  return s && s.type === 'object' && s.properties;
}

function setPath(obj, path, value) {
  let o = obj;
  path.slice(0, -1).forEach((k) => {
    if (o[k] == null) o[k] = {};
    o = o[k];
  });
  o[path[path.length - 1]] = value;
}

function fieldRow(key, prop, schema, path, value) {
  const title = prop.title || schema.title || key;
  const desc = prop.description || schema.description;
  const dataPath = path.join('.');
  const hint = desc ? h('div', { class: 'hint' }, desc) : null;

  if (schema.type === 'boolean') {
    const input = h('input', { type: 'checkbox', checked: !!value, dataset: { path: dataPath, type: 'bool' } });
    return h('div', { class: 'field bool' }, h('label', {}, title), h('label', { class: 'switch' }, input, h('span')), hint);
  }
  if (schema.enum) {
    const select = h(
      'select',
      { dataset: { path: dataPath, type: 'str' } },
      schema.enum.map((v) => h('option', { value: v, selected: v === value }, ENUM_LABELS[v] || v)),
    );
    return h('div', { class: 'field' }, h('label', {}, title), select, hint);
  }
  if (schema.type === 'integer' || schema.type === 'number') {
    const input = h('input', {
      type: 'number',
      value: value ?? '',
      min: schema.minimum ?? schema.exclusiveMinimum,
      max: schema.maximum ?? schema.exclusiveMaximum,
      step: schema.type === 'integer' ? 1 : 'any',
      dataset: { path: dataPath, type: schema.type === 'integer' ? 'int' : 'num' },
    });
    return h('div', { class: 'field' }, h('label', {}, title), input, hint);
  }
  if (schema.type === 'array') {
    const input = h('input', {
      type: 'text',
      value: (value || []).join(', '),
      placeholder: 'через запятую',
      dataset: { path: dataPath, type: 'list' },
    });
    return h('div', { class: 'field text' }, h('label', {}, title), input, hint);
  }
  const secret = /token|password/i.test(key);
  const input = h('input', { type: secret ? 'password' : 'text', value: value ?? '', autocomplete: 'off', dataset: { path: dataPath, type: 'str' } });
  const control = secret
    ? h(
        'div',
        { class: 'secret' },
        input,
        h('button', { type: 'button', class: 'btn icon', title: 'Показать', onclick: () => (input.type = input.type === 'password' ? 'text' : 'password') }, '👁'),
      )
    : input;
  return h('div', { class: 'field text' }, h('label', {}, title), control, hint);
}

function makeFieldset(title, schema, path, value, extraClass = '') {
  const fs = h('fieldset', { class: extraClass }, h('legend', {}, title));
  for (const [k, prop] of Object.entries(schema.properties)) {
    fs.append(fieldRow(k, prop, resolveRef(prop), [...path, k], value?.[k]));
  }
  // выключенный детектор/биржа визуально приглушается
  const toggle = fs.querySelector('[data-type=bool]');
  if (toggle && toggle.dataset.path.endsWith('.enabled')) {
    const sync = () => fs.classList.toggle('disabled', !toggle.checked);
    toggle.addEventListener('change', sync);
    sync();
  }
  return fs;
}

/** Рисует объект схемы: скалярные поля — в «Основное», вложенные объекты — отдельными блоками. */
function renderGroup(schema, path, value, container, mainTitle) {
  const grid = h('div', { class: 'fieldsets' });
  const scalars = { type: 'object', properties: {} };
  const deferred = [];
  for (const [k, prop] of Object.entries(schema.properties)) {
    const r = resolveRef(prop);
    if (!isObjectSchema(r)) {
      scalars.properties[k] = prop;
      continue;
    }
    const title = prop.title || r.title || k;
    const nested = Object.values(r.properties).some((p) => isObjectSchema(resolveRef(p)));
    if (nested) deferred.push([k, r, title]);
    else grid.append(makeFieldset(title, r, [...path, k], value?.[k], path[path.length - 1] === 'detectors' ? `det-${k}` : ''));
  }
  if (Object.keys(scalars.properties).length) {
    grid.prepend(makeFieldset(mainTitle, scalars, path, value));
  }
  if (grid.children.length) container.append(grid);
  for (const [k, r, title] of deferred) {
    container.append(h('div', { class: 'section-title' }, title));
    renderGroup(r, [...path, k], value?.[k], container, title);
  }
}

function liquidExtras() {
  const table = h('table', { class: 'status-table' });
  const fill = (rows) => {
    table.replaceChildren(
      h('tr', {}, h('th', {}, 'Источник'), h('th', {}, 'Монет'), h('th', {}, 'Обновлено'), h('th', {}, 'Ошибка')),
      ...rows.map((r) =>
        h(
          'tr',
          {},
          h('td', {}, r.name),
          h('td', {}, String(r.count)),
          h('td', {}, r.updated ? `${ago(r.updated)} назад` : '—'),
          h('td', { class: r.error ? 'err' : '' }, r.error || ''),
        ),
      ),
    );
  };
  fill(state.lastStatus?.liquid || []);
  const refreshBtn = h('button', { type: 'button', class: 'btn' }, 'Обновить листинги сейчас');
  refreshBtn.onclick = async () => {
    refreshBtn.disabled = true;
    refreshBtn.textContent = 'Загружаю…';
    try {
      fill(await api('/api/liquid/refresh', { method: 'POST' }));
    } catch (e) {
      alertBox(e.message);
    } finally {
      refreshBtn.disabled = false;
      refreshBtn.textContent = 'Обновить листинги сейчас';
    }
  };
  const checkInput = h('input', { type: 'text', placeholder: 'Тикер, например PEPE' });
  const checkOut = h('span', { class: 'muted' });
  const checkBtn = h('button', { type: 'button', class: 'btn' }, 'Проверить');
  checkBtn.onclick = async () => {
    const base = checkInput.value.trim();
    if (!base) return;
    const r = await api(`/api/liquid/check?base=${encodeURIComponent(base)}`);
    checkOut.textContent = r.listed_on.length ? `${r.base} есть на: ${r.listed_on.join(', ')}` : `${r.base} не найдена на включённых ликвидных биржах`;
  };
  return h(
    'div',
    {},
    h(
      'p',
      { class: 'note' },
      'Монета с MEXC/Gate, которая торгуется хотя бы на одной из включённых бирж, не сканируется. ' +
        'Листинги грузятся через публичные API и кэшируются. Изменения применяются после сохранения.',
    ),
    table,
    h('div', { class: 'row', style: 'margin:10px 0 18px' }, refreshBtn, checkInput, checkBtn, checkOut),
  );
}

function telegramExtras() {
  const out = h('span', { class: 'muted' });
  const btn = h('button', { type: 'button', class: 'btn' }, 'Отправить тестовое сообщение');
  btn.onclick = async () => {
    out.textContent = 'Отправляю…';
    try {
      await api('/api/telegram/test', { method: 'POST' });
      out.textContent = 'Отправлено, проверьте Telegram';
    } catch (e) {
      out.textContent = 'Ошибка: ' + e.message;
    }
  };
  return h(
    'div',
    {},
    h(
      'p',
      { class: 'note' },
      '1) Создайте бота у @BotFather и скопируйте токен. 2) Напишите своему боту /start. ' +
        '3) Узнайте свой chat_id у @userinfobot (для группы — добавьте бота в группу). ' +
        '4) Сохраните настройки и отправьте тест.',
    ),
    h('div', { class: 'row', style: 'margin-bottom:14px' }, btn, out),
  );
}

function openSettings() {
  const body = $('#settings-body');
  const tabs = $('#settings-tabs');
  body.replaceChildren();
  const active = store.get('settingsTab', 'mexc');
  for (const tab of TABS) {
    const prop = state.schema.properties[tab.key];
    const schema = resolveRef(prop);
    const pane = h('div', { class: 'tab-pane', dataset: { tab: tab.key }, hidden: tab.key !== active });
    if (tab.key === 'liquid') pane.append(liquidExtras());
    if (tab.key === 'telegram') pane.append(telegramExtras());
    renderGroup(schema, [tab.key], state.settings[tab.key], pane, 'Основное');
    body.append(pane);
  }
  tabs.replaceChildren(...TABS.map((t) => h('button', { type: 'button', class: t.key === active ? 'on' : '', dataset: { tab: t.key }, onclick: () => showTab(t.key) }, t.title)));
  setMsg('');
  $('#settings-dialog').showModal();
}

function showTab(key) {
  store.set('settingsTab', key);
  $('#settings-tabs').querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.dataset.tab === key));
  $('#settings-body').querySelectorAll('.tab-pane').forEach((p) => (p.hidden = p.dataset.tab !== key));
}

function setMsg(text, kind = '') {
  const m = $('#settings-msg');
  m.textContent = text;
  m.className = 'form-msg ' + kind;
}

function collectSettings() {
  const out = structuredClone(state.settings);
  for (const input of $('#settings-body').querySelectorAll('[data-path]')) {
    const path = input.dataset.path.split('.');
    let v;
    switch (input.dataset.type) {
      case 'bool':
        v = input.checked;
        break;
      case 'int':
        v = input.value === '' ? null : Number.parseInt(input.value, 10);
        break;
      case 'num':
        v = input.value === '' ? null : Number.parseFloat(input.value);
        break;
      case 'list':
        v = input.value
          .split(/[,;\s]+/)
          .map((s) => s.trim().toUpperCase())
          .filter(Boolean);
        break;
      default:
        v = input.value.trim();
    }
    setPath(out, path, v);
  }
  return out;
}

async function saveSettings(ev) {
  ev.preventDefault();
  const body = $('#settings-body');
  body.querySelectorAll('.field.invalid').forEach((f) => {
    f.classList.remove('invalid');
    f.querySelector('.err')?.remove();
  });
  const data = collectSettings();
  try {
    state.settings = await api('/api/settings', { method: 'PUT', body: JSON.stringify(data) });
  } catch (e) {
    const errors = Array.isArray(e.body?.detail) ? e.body.detail : null;
    if (!errors) {
      setMsg('Ошибка: ' + e.message, 'err');
      return;
    }
    let firstTab = null;
    for (const err of errors) {
      const input = body.querySelector(`[data-path="${CSS.escape(err.loc)}"]`);
      if (!input) continue;
      const field = input.closest('.field');
      field.classList.add('invalid');
      field.append(h('div', { class: 'err' }, err.msg));
      firstTab = firstTab || input.closest('.tab-pane').dataset.tab;
    }
    if (firstTab) showTab(firstTab);
    setMsg(`Проверьте поля: ${errors.length}`, 'err');
    return;
  }
  applySettings();
  setMsg('Сохранено', 'ok');
  setTimeout(() => $('#settings-dialog').open && $('#settings-dialog').close(), 500);
}

async function resetSettings() {
  if (!confirm('Сбросить все настройки, включая Telegram, к значениям по умолчанию?')) return;
  state.settings = await api('/api/settings/reset', { method: 'POST' });
  applySettings();
  openSettings();
  setMsg('Настройки сброшены', 'ok');
}

// ------------------------------------------------------------------ ручное открытие пары

let searchTimer = null;

function openAddPair() {
  const sel = $('#add-exchange');
  sel.replaceChildren(...state.meta.exchanges.map((e) => h('option', { value: e.id }, e.name)));
  $('#add-query').value = '';
  $('#add-results').replaceChildren();
  $('#add-dialog').showModal();
  $('#add-query').focus();
  searchPairs();
}

async function searchPairs() {
  const ex = $('#add-exchange').value;
  const q = $('#add-query').value.trim();
  const rows = await api(`/api/symbols?exchange=${encodeURIComponent(ex)}&q=${encodeURIComponent(q)}&limit=40`);
  $('#add-results').replaceChildren(
    ...(rows.length
      ? rows.map((r) =>
          h(
            'div',
            { class: 'sr-item', onclick: () => addManual(ex, r) },
            h('span', {}, h('b', {}, r.base), h('span', { class: 'muted' }, '/' + r.quote), '  ', h('span', { class: 'muted' }, r.volume_24h != null ? fmtUsd(r.volume_24h) + ' за 24ч' : '')),
            r.listed_on.length
              ? h('span', { class: 'tag liq', title: r.listed_on.join(', ') }, 'есть на ликвидных')
              : h('span', { class: 'tag' + (r.in_universe ? ' ok' : '') }, r.in_universe ? 'в скане' : 'не в скане'),
            h('span', { class: 'tag' }, 'Открыть'),
          ),
        )
      : [h('div', { class: 'feed-empty' }, 'Ничего не найдено')]),
  );
}

async function addManual(exchange, r) {
  $('#add-dialog').close();
  const existing = findCard(`${exchange}:${r.symbol}`);
  if (existing) {
    existing.flash('manual');
    return;
  }
  let url = '';
  try {
    url = (await api(`/api/pair-url?exchange=${encodeURIComponent(exchange)}&symbol=${encodeURIComponent(r.symbol)}`)).url;
  } catch {
    url = '';
  }
  const card = new Card({ exchange, symbol: r.symbol, base: r.base, quote: r.quote, url, pinned: true, manual: true });
  if (!placeCard(card)) alertBox('Все карточки закреплены — открепите одну, чтобы освободить место.');
}

// ------------------------------------------------------------------ запуск

function bindUI() {
  $('#grid-size').addEventListener('change', (e) => {
    const [rows, cols] = e.target.value.split('x').map(Number);
    saveSettingsPartial((s) => {
      s.ui.grid_rows = rows;
      s.ui.grid_cols = cols;
    });
  });
  $('#sound-btn').addEventListener('click', () => {
    ensureAudio();
    saveSettingsPartial((s) => {
      s.ui.sound = !s.ui.sound;
    }).then(() => state.settings.ui.sound && setTimeout(() => beep('ping_pong'), 50));
  });
  $('#feed-btn').addEventListener('click', () => {
    const feed = $('#feed');
    feed.hidden = !feed.hidden;
    store.set('feedOpen', !feed.hidden);
  });
  $('#feed-clear').addEventListener('click', async () => {
    if (!confirm('Удалить всю историю алертов на сервере?')) return;
    await api('/api/alerts', { method: 'DELETE' });
    state.feed = [];
    renderFeed();
  });
  $('#settings-btn').addEventListener('click', openSettings);
  $('#settings-form').addEventListener('submit', saveSettings);
  $('#settings-reset').addEventListener('click', resetSettings);
  $('#add-pair-btn').addEventListener('click', openAddPair);
  $('#add-exchange').addEventListener('change', searchPairs);
  $('#add-query').addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(searchPairs, 250);
  });
  for (const dlg of document.querySelectorAll('dialog')) {
    dlg.addEventListener('click', (e) => {
      if (e.target.closest('[data-close]')) dlg.close();
    });
  }
  // браузер разрешает звук только после взаимодействия со страницей
  document.addEventListener('pointerdown', () => {
    ensureAudio();
    setTimeout(renderSoundButton, 100);
  });
  document.addEventListener('keydown', () => ensureAudio(), { once: true });
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      state.unseen = 0;
      document.title = 'LowLiqSpider';
      state.slots.forEach((c) => c && c.refresh());
    }
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && state.maximized && !document.querySelector('dialog[open]')) toggleMaximize(null);
  });
}

async function init() {
  const [meta, settings, schema] = await Promise.all([api('/api/meta'), api('/api/settings'), api('/api/settings/schema')]);
  state.meta = meta;
  state.settings = settings;
  state.schema = schema;

  const savedFilters = store.get('filters', null);
  state.filters.exchanges = new Set(savedFilters?.exchanges ?? meta.exchanges.map((e) => e.id));
  state.filters.detectors = new Set(savedFilters?.detectors ?? meta.detectors.map((d) => d.key));
  $('#feed').hidden = !store.get('feedOpen', true);

  bindUI();
  renderFilters();
  renderGridSelect();
  renderSoundButton();
  restoreGrid();
  renderGrid();

  const alerts = await api('/api/alerts?limit=300');
  state.feed = alerts;
  alerts.forEach((a) => state.seen.add(a.id));
  renderFeed();
  // карточки, восстановленные из прошлой сессии, получают алерты, пришедшие без нас
  for (const a of [...alerts].reverse()) {
    const card = findCard(`${a.exchange}:${a.symbol}`);
    if (card) card.addAlert(a, { flash: false });
  }
  // пустую сетку заполняем последними алертами
  if (!state.slots.some(Boolean)) {
    const latest = new Map();
    for (const a of alerts) {
      if (!passesFilter(a)) continue;
      const key = `${a.exchange}:${a.symbol}`;
      if (!latest.has(key)) latest.set(key, alerts.filter((x) => `${x.exchange}:${x.symbol}` === key).reverse());
      if (latest.size >= state.slots.length) break;
    }
    [...latest.values()].reverse().forEach((group) => {
      const last = group[group.length - 1];
      placeCard(new Card({ ...last, alerts: group }));
    });
  }
  saveGrid();
  connectWS();
  setInterval(() => state.slots.forEach((c) => c && c.updateAge()), 5000);
}

init().catch((e) => {
  document.body.append(h('div', { class: 'feed-empty' }, 'Не удалось загрузить интерфейс: ' + e.message));
  console.error(e);
});
