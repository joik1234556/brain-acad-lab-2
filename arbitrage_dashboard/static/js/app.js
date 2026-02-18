import { apiGet, apiPost } from './api.js';
import { fmtPct, fmtPrice, fmtUsd, parsePctInput, parseVolInput, spreadPillClass } from './ui.js';

const state = {
  data: { rows: [], dbg: {} },
  sortKey: 'spread',
  sortDir: 'desc',
  error: null,
};

const els = {
  tbody: document.getElementById('arbTbody'),
  search: document.getElementById('searchInput'),
  minVol: document.getElementById('minVolInput'),
  minSpread: document.getElementById('minSpreadInput'),
  reset: document.getElementById('resetFiltersBtn'),
  refreshNow: document.getElementById('refreshNowBtn'),
  updated: document.getElementById('updatedAt'),
  indicator: document.getElementById('refreshIndicator'),
  error: document.getElementById('errorBanner'),
  stats: document.getElementById('statsText'),
  exchChecks: [...document.querySelectorAll('.exch-check')],
  sortable: [...document.querySelectorAll('.sortable')],
};

function setIndicator(text, cls = 'text-bg-secondary') {
  els.indicator.className = `badge ${cls}`;
  els.indicator.textContent = text;
}

function showError(message) {
  if (!message) {
    els.error.classList.add('d-none');
    els.error.textContent = '';
    return;
  }
  els.error.textContent = message;
  els.error.classList.remove('d-none');
}

function activeExchanges() {
  return new Set(els.exchChecks.filter(x => x.checked).map(x => x.value));
}

function filteredRows(rows) {
  const q = (els.search.value || '').trim().toUpperCase();
  const minVol = parseVolInput(els.minVol.value || '0');
  const parsedSpread = parsePctInput(els.minSpread.value || '');
  const minSpread = parsedSpread === null ? 0 : parsedSpread;
  const exSet = activeExchanges();

  return rows.filter(r => {
    const sym = String(r.symbol || '').toUpperCase();
    if (q && !sym.startsWith(q)) return false;
    if (minSpread > 0 && !(r.spread >= minSpread)) return false;
    if (minVol > 0) {
      const bOk = Number.isFinite(r.buy_vol) ? r.buy_vol >= minVol : true;
      const sOk = Number.isFinite(r.sell_vol) ? r.sell_vol >= minVol : true;
      if (!(bOk && sOk)) return false;
    }
    if (!exSet.has(r.buy_ex) || !exSet.has(r.sell_ex)) return false;
    return true;
  });
}

function sortRows(rows) {
  const dir = state.sortDir === 'asc' ? 1 : -1;
  const key = state.sortKey;
  rows.sort((a, b) => {
    const va = a[key];
    const vb = b[key];
    const na = typeof va === 'string' ? va.toUpperCase() : Number(va);
    const nb = typeof vb === 'string' ? vb.toUpperCase() : Number(vb);
    if (na < nb) return -1 * dir;
    if (na > nb) return 1 * dir;
    return 0;
  });
  return rows;
}

function rowKey(r) {
  return `${r.symbol}|${r.buy_ex}|${r.sell_ex}`;
}

function rowHtml(r) {
  return `
    <td><strong>${r.symbol || ''}</strong></td>
    <td><span class="pill ${spreadPillClass(r.spread)}">${fmtPct(r.spread, 2)}</span></td>
    <td><a href="${r.buy_url}" target="_blank" rel="noreferrer">${r.buy_ex}</a></td>
    <td><a href="${r.sell_url}" target="_blank" rel="noreferrer">${r.sell_ex}</a></td>
    <td class="font-monospace">${fmtPrice(r.buy_ask)}</td>
    <td class="font-monospace">${fmtPrice(r.sell_bid)}</td>
    <td class="font-monospace">${fmtPct(r.funding_spread, 3)}</td>
    <td class="font-monospace">${r.funding_eta_buy || '--:--:--'} / ${r.funding_eta_sell || '--:--:--'}</td>
    <td class="font-monospace">${fmtUsd(r.buy_vol)}</td>
    <td class="font-monospace">${fmtUsd(r.sell_vol)}</td>
  `;
}

function patchTable(rows) {
  const byKey = new Map([...els.tbody.querySelectorAll('tr[data-key]')].map(tr => [tr.dataset.key, tr]));
  const orderedKeys = [];

  for (const r of rows) {
    const key = rowKey(r);
    orderedKeys.push(key);
    let tr = byKey.get(key);
    if (!tr) {
      tr = document.createElement('tr');
      tr.dataset.key = key;
    }
    tr.innerHTML = rowHtml(r);
    els.tbody.appendChild(tr);
    byKey.delete(key);
  }

  for (const [_, tr] of byKey) tr.remove();

  if (!rows.length) {
    els.tbody.innerHTML = '<tr><td colspan="10" class="text-muted">Ничего не найдено</td></tr>';
  }
}

function render() {
  const rows = sortRows(filteredRows([...(state.data.rows || [])]));
  patchTable(rows);
  const dbg = state.data.dbg || {};
  els.stats.textContent = `mexc=${dbg.mexc ?? 0}, bybit=${dbg.bybit ?? 0}, bingx=${dbg.bingx ?? 0}, kept=${dbg.kept ?? rows.length}, took=${dbg.took_ms ?? '-'}ms`;
  els.updated.textContent = `updated: ${state.data.updated_at || '—'}`;
}

async function loadData() {
  try {
    setIndicator('обновляется…', 'text-bg-info');
    showError(null);
    state.data = await apiGet('/api/data');
    render();
    setIndicator('актуально', 'text-bg-success');
  } catch (e) {
    setIndicator('ошибка', 'text-bg-danger');
    showError(`API недоступен: ${e.message}. Повтор через 5 сек.`);
  }
}

function bindUi() {
  [els.search, els.minVol, els.minSpread].forEach(el => el.addEventListener('input', render));
  els.minSpread.addEventListener('change', () => {
    const parsed = parsePctInput(els.minSpread.value);
    els.minSpread.value = parsed === null ? '' : `${(parsed * 100).toString().replace('.', ',')}%`;
    render();
  });
  els.exchChecks.forEach(el => el.addEventListener('change', render));

  els.reset.addEventListener('click', () => {
    els.search.value = '';
    els.minVol.value = '';
    els.minSpread.value = '0';
    els.exchChecks.forEach(x => (x.checked = true));
    render();
  });

  els.refreshNow.addEventListener('click', async () => {
    try { await apiPost('/api/refresh', {}); } catch (_) {}
    await loadData();
  });

  els.sortable.forEach(th => {
    th.addEventListener('click', () => {
      const k = th.dataset.sort;
      if (state.sortKey === k) state.sortDir = state.sortDir === 'asc' ? 'desc' : 'asc';
      else { state.sortKey = k; state.sortDir = 'desc'; }
      render();
    });
  });
}

bindUi();
loadData();
setInterval(loadData, 5000);
