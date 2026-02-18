export function fmtPct(v, d = 2) {
  return Number.isFinite(v) ? `${(v * 100).toFixed(d)}%` : 'N/A';
}
export function fmtPrice(v) {
  if (!Number.isFinite(v)) return 'N/A';
  const s = Math.abs(v) >= 1 ? v.toFixed(6) : v.toFixed(10);
  return s.replace(/0+$/, '').replace(/\.$/, '');
}
export function fmtUsd(v) {
  if (!Number.isFinite(v)) return 'N/A';
  if (v >= 1e9) return `${(v / 1e9).toFixed(1)}b$`;
  if (v >= 1e6) return `${(v / 1e6).toFixed(1)}m$`;
  if (v >= 1e3) return `${(v / 1e3).toFixed(1)}k$`;
  return `${Math.round(v)}$`;
}

export function spreadPillClass(spread) {
  if (!Number.isFinite(spread)) return 'pill-bad';
  if (spread >= 0.01) return 'pill-ok';
  if (spread >= 0.003) return 'pill-mid';
  return 'pill-bad';
}

export function parseVolInput(raw) {
  const s = String(raw || '').trim().toLowerCase().replace(',', '.').replace('м', 'm');
  if (!s) return 0;
  const m = s.match(/^([0-9]+(?:\.[0-9]+)?)([kmb])?$/);
  if (!m) return Number.parseFloat(s) || 0;
  const n = Number.parseFloat(m[1]);
  const q = m[2];
  if (q === 'k') return n * 1e3;
  if (q === 'm') return n * 1e6;
  if (q === 'b') return n * 1e9;
  return n;
}
