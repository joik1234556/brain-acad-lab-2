import asyncio
import json
import math
import os
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

if getattr(sys, "frozen", False):
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")


def app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_PATH = os.path.join(app_dir(), "arb_dashboard_config.json")
DEFAULT_REFRESH_SEC = 5
DEFAULT_MIN_VOL_USD = 5_000_000.0
DEFAULT_MIN_SPREAD = 0.0
HTTP_TIMEOUT = 12
MAX_BINGX_SYMBOLS = 180
BINGX_CONCURRENCY = 16
DEFAULT_EXCH_ENABLED = {"MEXC": True, "Bybit": True, "BingX": True}

MEXC_TICKERS = "https://contract.mexc.com/api/v1/contract/ticker"
BYBIT_TICKERS = "https://api.bybit.com/v5/market/tickers"
BINGX_CONTRACTS = "https://open-api.bingx.com/openApi/swap/v2/quote/contracts"
BINGX_BOOK_TICKER = "https://open-api.bingx.com/openApi/swap/v2/quote/bookTicker"
BINGX_TICKER_24H = "https://open-api.bingx.com/openApi/swap/v2/quote/ticker"
BINGX_PREMIUM_INDEX = "https://open-api.bingx.com/openApi/swap/v2/quote/premiumIndex"


@dataclass
class MarketRow:
    exchange: str
    bid: float
    ask: float
    last: float
    vol24_usd: float
    fund_rate: float
    fund24_est: float
    url: str


def mexc_trade_url(symbol_mexc: str) -> str:
    return f"https://www.mexc.com/futures/{symbol_mexc}"


def bybit_trade_url(symbol_bybit: str) -> str:
    return f"https://www.bybit.com/trade/usdt/{symbol_bybit}"


def bingx_trade_url(symbol_bingx: str) -> str:
    return f"https://bingx.com/en/perpetual/{symbol_bingx}"


def to_float(x: Any) -> float:
    try:
        return float(x)
    except Exception:
        return math.nan


def is_pos(x: float) -> bool:
    return math.isfinite(x) and x > 0


def funding_24h_estimate(rate: float, interval_h: int = 8) -> float:
    return rate * (24.0 / interval_h) if math.isfinite(rate) else math.nan


def normalize_usdt(base: str) -> str:
    b = (base or "").upper()
    if b == "XBT":
        b = "BTC"
    return f"{b}USDT"


def _as_list(resp: Any) -> List[dict]:
    if isinstance(resp, dict):
        data = resp.get("data")
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            return [data]
    if isinstance(resp, list):
        return [x for x in resp if isinstance(x, dict)]
    return []


def _pick_float(d: dict, keys: List[str]) -> float:
    for key in keys:
        value = to_float(d.get(key))
        if math.isfinite(value):
            return value
    return math.nan


def funding_eta(interval_h: int = 8) -> str:
    now = datetime.now(timezone.utc)
    base = now.replace(minute=0, second=0, microsecond=0)
    cur = base.hour
    next_h = ((cur // interval_h) + 1) * interval_h
    day = 0
    if next_h >= 24:
        next_h -= 24
        day = 1
    target = (base + timedelta(days=day)).replace(hour=next_h)
    delta = target - now
    total = max(0, int(delta.total_seconds()))
    hh = total // 3600
    mm = (total % 3600) // 60
    ss = total % 60
    return f"{hh:02d}:{mm:02d}:{ss:02d}"


async def fetch_json(session: aiohttp.ClientSession, url: str, params: Optional[dict] = None) -> Any:
    async with session.get(url, params=params, timeout=HTTP_TIMEOUT) as response:
        return await response.json(content_type=None)


async def load_mexc(session: aiohttp.ClientSession) -> Dict[str, MarketRow]:
    out: Dict[str, MarketRow] = {}
    data = await fetch_json(session, MEXC_TICKERS)
    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return out

    for it in items:
        if not isinstance(it, dict):
            continue
        symbol = str(it.get("symbol") or "")
        if "_" not in symbol:
            continue
        base, quote = symbol.split("_", 1)
        if quote.upper() != "USDT":
            continue
        fund = to_float(it.get("fundingRate"))
        out[normalize_usdt(base)] = MarketRow(
            exchange="MEXC",
            bid=to_float(it.get("bid1")),
            ask=to_float(it.get("ask1")),
            last=to_float(it.get("lastPrice")),
            vol24_usd=to_float(it.get("amount24")),
            fund_rate=fund,
            fund24_est=funding_24h_estimate(fund),
            url=mexc_trade_url(symbol),
        )
    return out


async def load_bybit(session: aiohttp.ClientSession) -> Dict[str, MarketRow]:
    out: Dict[str, MarketRow] = {}
    data = await fetch_json(session, BYBIT_TICKERS, params={"category": "linear"})
    items = data.get("result", {}).get("list", []) if isinstance(data, dict) else []
    if not isinstance(items, list):
        return out

    for it in items:
        if not isinstance(it, dict):
            continue
        symbol = str(it.get("symbol") or "").upper()
        if not symbol.endswith("USDT"):
            continue
        fund = to_float(it.get("fundingRate"))
        out[symbol] = MarketRow(
            exchange="Bybit",
            bid=to_float(it.get("bid1Price") or it.get("bidPrice")),
            ask=to_float(it.get("ask1Price") or it.get("askPrice")),
            last=to_float(it.get("lastPrice")),
            vol24_usd=to_float(it.get("turnover24h") or it.get("turnover24H") or it.get("volume24h")),
            fund_rate=fund,
            fund24_est=funding_24h_estimate(fund),
            url=bybit_trade_url(symbol),
        )
    return out


async def load_bingx(session: aiohttp.ClientSession, candidate_norm: List[str]) -> Dict[str, MarketRow]:
    out: Dict[str, MarketRow] = {}
    contracts_resp = await fetch_json(session, BINGX_CONTRACTS)
    contracts = _as_list(contracts_resp)

    norm_to_raw: Dict[str, str] = {}
    for c in contracts:
        raw = str(c.get("symbol") or "")
        if "-" not in raw:
            continue
        base, quote = raw.split("-", 1)
        if quote.upper() == "USDT":
            norm_to_raw[normalize_usdt(base)] = raw

    chosen_norms = [s for s in candidate_norm if s in norm_to_raw][:MAX_BINGX_SYMBOLS]
    if not chosen_norms:
        chosen_norms = list(norm_to_raw.keys())[:MAX_BINGX_SYMBOLS]

    async def fetch_one(norm_sym: str) -> Optional[Tuple[str, MarketRow]]:
        raw = norm_to_raw.get(norm_sym)
        if not raw:
            return None
        try:
            book, tick, prem = await asyncio.gather(
                fetch_json(session, BINGX_BOOK_TICKER, params={"symbol": raw}),
                fetch_json(session, BINGX_TICKER_24H, params={"symbol": raw}),
                fetch_json(session, BINGX_PREMIUM_INDEX, params={"symbol": raw}),
                return_exceptions=True,
            )
            if any(isinstance(x, Exception) for x in (book, tick, prem)):
                return None
            b0 = _as_list(book)[0] if _as_list(book) else {}
            t0 = _as_list(tick)[0] if _as_list(tick) else {}
            p0 = _as_list(prem)[0] if _as_list(prem) else {}

            bid = _pick_float(b0, ["bidPrice", "bid", "bestBidPrice", "bestBid"])
            ask = _pick_float(b0, ["askPrice", "ask", "bestAskPrice", "bestAsk"])
            last = _pick_float(t0, ["lastPrice", "last", "close", "markPrice", "indexPrice"])
            vol_quote = _pick_float(t0, ["quoteVolume", "quoteQty", "turnover", "turnover24h", "turnover24H", "quoteVolume24h", "quoteVolume24H", "volumeQuote"])
            vol_base = _pick_float(t0, ["volume", "baseVolume", "qty", "amount", "vol", "volume24h"])
            vol = vol_quote
            if not is_pos(vol):
                vol = vol_base * last if is_pos(vol_base) and is_pos(last) else math.nan
            fund = _pick_float(p0, ["fundingRate", "lastFundingRate", "funding"])

            return norm_sym, MarketRow(
                exchange="BingX",
                bid=bid,
                ask=ask,
                last=last,
                vol24_usd=vol,
                fund_rate=fund,
                fund24_est=funding_24h_estimate(fund),
                url=bingx_trade_url(raw),
            )
        except Exception:
            return None

    sem = asyncio.Semaphore(BINGX_CONCURRENCY)

    async def guarded_fetch(sym: str):
        async with sem:
            return await fetch_one(sym)

    results = await asyncio.gather(*[guarded_fetch(sym) for sym in chosen_norms], return_exceptions=True)
    for item in results:
        if isinstance(item, tuple):
            out[item[0]] = item[1]
    return out


def exec_spread(buy: MarketRow, sell: MarketRow) -> float:
    if not (is_pos(buy.ask) and is_pos(sell.bid)):
        return math.nan
    return (sell.bid - buy.ask) / buy.ask


def best_pair(rows: List[MarketRow], min_vol: float) -> Optional[Dict[str, Any]]:
    valid = [r for r in rows if is_pos(r.ask) and is_pos(r.bid) and math.isfinite(r.vol24_usd) and r.vol24_usd >= min_vol]
    if len(valid) < 2:
        return None

    best: Optional[Tuple[MarketRow, MarketRow, float]] = None
    for buy in valid:
        for sell in valid:
            if buy.exchange == sell.exchange:
                continue
            spread = exec_spread(buy, sell)
            if math.isfinite(spread) and (best is None or spread > best[2]):
                best = (buy, sell, spread)

    if not best:
        return None

    buy, sell, spread = best
    fund_spread = sell.fund_rate - buy.fund_rate if math.isfinite(sell.fund_rate) and math.isfinite(buy.fund_rate) else math.nan
    return {
        "spread": spread,
        "buy_ex": buy.exchange,
        "sell_ex": sell.exchange,
        "buy_ask": buy.ask,
        "sell_bid": sell.bid,
        "buy_funding": buy.fund_rate,
        "sell_funding": sell.fund_rate,
        "buy_funding24": buy.fund24_est,
        "sell_funding24": sell.fund24_est,
        "funding_spread": fund_spread,
        "funding_eta": funding_eta(),
        "buy_vol": buy.vol24_usd,
        "sell_vol": sell.vol24_usd,
        "buy_url": buy.url,
        "sell_url": sell.url,
        "pair_key": f"{buy.exchange}-{sell.exchange}",
    }


def load_config() -> Dict[str, Any]:
    defaults = {
        "refresh_sec": DEFAULT_REFRESH_SEC,
        "min_vol": DEFAULT_MIN_VOL_USD,
        "min_spread": DEFAULT_MIN_SPREAD,
        "enabled": dict(DEFAULT_EXCH_ENABLED),
    }
    if not os.path.exists(CONFIG_PATH):
        return defaults
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
        if not isinstance(loaded, dict):
            return defaults
        defaults.update(loaded)
        enabled = defaults.get("enabled", {})
        defaults["enabled"] = {
            "MEXC": bool(enabled.get("MEXC", True)),
            "Bybit": bool(enabled.get("Bybit", True)),
            "BingX": bool(enabled.get("BingX", True)),
        }
        return defaults
    except Exception:
        return defaults


def save_config(cfg: Dict[str, Any]) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)


@asynccontextmanager
async def lifespan(_: FastAPI):
    asyncio.create_task(updater_loop())
    yield


app = FastAPI(lifespan=lifespan)
CFG = load_config()
CACHE = {"updated_at": None, "rows": [], "dbg": {"mexc": 0, "bybit": 0, "bingx": 0, "kept": 0, "took_ms": 0}}
CACHE_LOCK = asyncio.Lock()

HTML_PAGE = r"""
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Arbitrage Dashboard</title>
  <style>
    :root{
      --bg:#101419; --card:#1c2229; --line:#2d353e; --text:#edf1f6; --muted:#9ca7b5; --accent:#69dd91;
      --chip:#2a323c; --chipText:#f4f7fa; --danger:#ff7d7d; --good:#5ad87d;
    }
    body.theme-dark{
      --bg:#081428; --card:#13223d; --line:#27416e; --text:#edf2ff; --muted:#8ea8cf; --accent:#6ca4ff;
      --chip:#1a3158; --chipText:#eff5ff; --danger:#ff8a8a; --good:#5fe47f;
    }
    body.theme-light{
      --bg:#f2f4f7; --card:#ffffff; --line:#d9dee5; --text:#1b2430; --muted:#5c6a79; --accent:#21a864;
      --chip:#eff3f8; --chipText:#27303a; --danger:#e06060; --good:#26b766;
    }
    *{box-sizing:border-box}
    body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,system-ui,Segoe UI,Arial,sans-serif}
    .wrap{max-width:1500px;margin:0 auto;padding:12px}
    .toolbar{display:grid;grid-template-columns:1.1fr 1fr 1fr 1fr 1fr auto;gap:10px;align-items:end}
    .panel{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:10px}
    .label{font-size:12px;color:var(--muted);margin-bottom:6px}
    input,select{width:100%;background:transparent;color:var(--text);border:1px solid var(--line);border-radius:8px;padding:8px}
    .checklist{display:flex;gap:6px;flex-wrap:wrap}
    .chip{border:1px solid var(--line);background:var(--chip);color:var(--chipText);padding:5px 8px;border-radius:999px;font-size:12px;display:flex;gap:6px;align-items:center}
    .btn{border:1px solid var(--line);background:var(--chip);color:var(--text);padding:9px 12px;border-radius:8px;cursor:pointer}
    .btn[disabled]{opacity:.45;cursor:not-allowed}
    .meta{margin:8px 0 10px;display:flex;gap:8px;flex-wrap:wrap;font-size:12px;color:var(--muted)}
    .badge{padding:4px 8px;border-radius:999px;border:1px solid var(--line);background:var(--card)}
    .table-wrap{overflow:auto;border:1px solid var(--line);border-radius:14px;background:var(--card)}
    table{width:100%;border-collapse:collapse;font-size:14px;min-width:1300px}
    th,td{padding:10px 8px;border-bottom:1px solid var(--line);vertical-align:middle}
    th{position:sticky;top:0;background:var(--card);text-align:left}
    tr:hover{background:rgba(120,130,150,.08)}
    .symbol{font-weight:800;font-size:30px;line-height:1}
    .fav{cursor:pointer;font-size:18px}
    .pinned{background:rgba(248,219,97,.18)!important}
    .buy{color:var(--good);font-weight:700}
    .sell{color:var(--danger);font-weight:700}
    .spread{font-weight:800;padding:4px 8px;background:#74e48f;color:#11331f;border-radius:8px;display:inline-block}
    .mono{font-family:ui-monospace,Menlo,Consolas,monospace}
    .logo{display:inline-flex;min-width:18px;justify-content:center}
    .fund-time{color:var(--muted);font-size:12px}
    @media(max-width:1300px){.toolbar{grid-template-columns:1fr 1fr 1fr}}
    @media(max-width:780px){.toolbar{grid-template-columns:1fr 1fr}}
    @media(max-width:560px){.toolbar{grid-template-columns:1fr}}
  </style>
</head>
<body class="theme-dark">
<div class="wrap">
  <div class="toolbar">
    <div class="panel">
      <div class="label">Поиск (с начала символа)</div>
      <input id="q" placeholder="BTC" />
    </div>
    <div class="panel">
      <div class="label">Min Volume 24h (USD)</div>
      <input id="minVol" type="number" min="0" step="100000" />
    </div>
    <div class="panel">
      <div class="label">Min Spread (%)</div>
      <input id="minSpread" type="number" min="0" step="0.01" />
    </div>
    <div class="panel">
      <div class="label">Биржи в поиске</div>
      <div class="checklist" id="exchangeBox"></div>
    </div>
    <div class="panel">
      <div class="label">Тема и звук</div>
      <div style="display:flex;gap:8px">
        <select id="themeSel">
          <option value="theme-dark">Dark Blue</option>
          <option value="theme-light">Light</option>
          <option value="theme-classic">Classic Gray</option>
        </select>
        <label class="chip"><input type="checkbox" id="soundToggle" /> sound</label>
      </div>
    </div>
    <div>
      <button class="btn" id="refreshBtn">↻ Refresh now</button>
    </div>
  </div>

  <div class="meta">
    <div class="badge" id="updated">Updated: —</div>
    <div class="badge" id="dbg">DBG: —</div>
    <div class="badge" id="cooldownBadge">Manual refresh cooldown: 0s</div>
  </div>

  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>Fav</th>
          <th>Токен</th>
          <th>Покупка / Продажа</th>
          <th>Prices</th>
          <th>Funding buy/sell</th>
          <th>Funding spread</th>
          <th>Funding calc in</th>
          <th>Spread</th>
          <th>Volume 24h</th>
        </tr>
      </thead>
      <tbody id="tbody"><tr><td colspan="9">Загрузка...</td></tr></tbody>
    </table>
  </div>
</div>
<script>
const EXCHANGE_LOGO={MEXC:'🟦',Bybit:'🟠',BingX:'🔵'};
const REFRESH_COOLDOWN_SEC=8;
let lastAlertKey='';
let cooldown=0;
let timerId=null;
let STATE={config:null,data:null,pinned:new Set(JSON.parse(localStorage.getItem('pinnedSymbols')||'[]')),theme:localStorage.getItem('theme')||'theme-dark',sound:(localStorage.getItem('soundOn')||'0')==='1'};

const fmtPct=(x,d=2)=>Number.isFinite(x)?(x*100).toFixed(d)+'%':'N/A';
const fmtUsd=x=>!Number.isFinite(x)?'N/A':(x>=1e9?(x/1e9).toFixed(2)+'b$':x>=1e6?(x/1e6).toFixed(2)+'m$':x>=1e3?(x/1e3).toFixed(1)+'k$':Math.round(x)+'$');
const fmtPrice=x=>Number.isFinite(x)?x.toFixed(Math.abs(x)>=1?6:10).replace(/0+$/,'').replace(/\.$/,''):'N/A';

function playSmsBeep(){
  try{
    const ac=new (window.AudioContext||window.webkitAudioContext)();
    const osc=ac.createOscillator();
    const gain=ac.createGain();
    osc.type='triangle';
    osc.frequency.value=880;
    gain.gain.setValueAtTime(0.0001,ac.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.20,ac.currentTime+0.01);
    gain.gain.exponentialRampToValueAtTime(0.0001,ac.currentTime+0.14);
    osc.connect(gain); gain.connect(ac.destination); osc.start(); osc.stop(ac.currentTime+0.15);
  }catch(_e){}
}

async function apiGet(path){ const r=await fetch(path,{cache:'no-store'}); return r.json(); }
async function apiPost(path,body){ const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}); return r.json(); }

function applyTheme(){
  document.body.className=STATE.theme;
  document.getElementById('themeSel').value=STATE.theme;
  localStorage.setItem('theme',STATE.theme);
}

function setCooldown(sec){
  cooldown=sec;
  const btn=document.getElementById('refreshBtn');
  if(timerId) clearInterval(timerId);
  timerId=setInterval(()=>{
    cooldown=Math.max(0,cooldown-1);
    btn.disabled=cooldown>0;
    btn.textContent=cooldown>0?`↻ Refresh (${cooldown})`:'↻ Refresh now';
    document.getElementById('cooldownBadge').textContent=`Manual refresh cooldown: ${cooldown}s`;
    if(cooldown===0){clearInterval(timerId);timerId=null;}
  },1000);
  btn.disabled=true;
  btn.textContent=`↻ Refresh (${cooldown})`;
}

function renderExchangeFilters(){
  const box=document.getElementById('exchangeBox'); box.innerHTML='';
  ['MEXC','Bybit','BingX'].forEach(ex=>{
    const chip=document.createElement('label');
    chip.className='chip';
    const checked=!!STATE.config.enabled?.[ex];
    chip.innerHTML=`<input type="checkbox" ${checked?'checked':''}/> ${EXCHANGE_LOGO[ex]||'◉'} ${ex}`;
    chip.addEventListener('click', async (e)=>{
      e.preventDefault();
      const enabled={...(STATE.config.enabled||{})};
      enabled[ex]=!enabled[ex];
      STATE.config=await apiPost('/api/config',{enabled});
      renderExchangeFilters();
      await refreshData();
    });
    box.appendChild(chip);
  });
}

function isPinned(symbol){ return STATE.pinned.has(symbol); }
function togglePinned(symbol){
  if(STATE.pinned.has(symbol)) STATE.pinned.delete(symbol); else STATE.pinned.add(symbol);
  localStorage.setItem('pinnedSymbols',JSON.stringify([...STATE.pinned]));
  render();
}

function applyFilters(rows){
  const q=(document.getElementById('q').value||'').trim().toUpperCase();
  const minVol=parseFloat(document.getElementById('minVol').value||'0');
  const minSpreadPct=parseFloat(document.getElementById('minSpread').value||'0');
  const minSpread=Number.isFinite(minSpreadPct)?minSpreadPct/100:0;

  return rows.filter(r=>{
    const sym=(r.symbol||'').toUpperCase();
    if(q && !sym.startsWith(q)) return false;
    if(Number.isFinite(minVol) && minVol>0 && !(r.buy_vol>=minVol && r.sell_vol>=minVol)) return false;
    if(Number.isFinite(minSpread) && minSpread>0 && !(r.spread>=minSpread)) return false;
    return true;
  });
}

function maybePlayAlert(rows){
  if(!STATE.sound || !rows.length) return;
  const top=rows[0];
  const key=`${top.symbol}|${top.buy_ex}|${top.sell_ex}|${(top.spread||0).toFixed(4)}`;
  if(lastAlertKey!==key){
    lastAlertKey=key;
    playSmsBeep();
  }
}

function render(){
  if(!STATE.data) return;
  document.getElementById('updated').textContent=`Updated: ${STATE.data.updated_at||'—'}`;
  document.getElementById('dbg').textContent=`DBG mexc=${STATE.data.dbg.mexc} bybit=${STATE.data.dbg.bybit} bingx=${STATE.data.dbg.bingx} kept=${STATE.data.dbg.kept} took=${STATE.data.dbg.took_ms}ms`;

  let rows=applyFilters([...(STATE.data.rows||[])]);
  rows.sort((a,b)=>{
    const pa=isPinned(a.symbol)?1:0;
    const pb=isPinned(b.symbol)?1:0;
    if(pa!==pb) return pb-pa;
    return (b.spread||0)-(a.spread||0);
  });

  maybePlayAlert(rows);

  const tbody=document.getElementById('tbody');
  tbody.innerHTML='';
  if(!rows.length){
    tbody.innerHTML='<tr><td colspan="9">Ничего не найдено по фильтрам.</td></tr>';
    return;
  }

  rows.forEach(r=>{
    const pinned=isPinned(r.symbol);
    const tr=document.createElement('tr');
    if(pinned) tr.classList.add('pinned');
    tr.innerHTML=`
      <td><span class="fav" title="pin/unpin">${pinned?'★':'☆'}</span></td>
      <td class="symbol">${r.symbol.replace('USDT','')}</td>
      <td>
        <div class="buy">⬆ LONG <span class="logo">${EXCHANGE_LOGO[r.buy_ex]||'◉'}</span> <a href="${r.buy_url}" target="_blank">${r.buy_ex}</a></div>
        <div class="sell">⬇ SHORT <span class="logo">${EXCHANGE_LOGO[r.sell_ex]||'◉'}</span> <a href="${r.sell_url}" target="_blank">${r.sell_ex}</a></div>
      </td>
      <td class="mono">
        <div>${fmtPrice(r.buy_ask)}</div>
        <div>${fmtPrice(r.sell_bid)}</div>
      </td>
      <td class="mono">
        <div>${fmtPct(r.buy_funding,3)}</div>
        <div>${fmtPct(r.sell_funding,3)}</div>
      </td>
      <td class="mono">${fmtPct(r.funding_spread,3)}</td>
      <td>
        <div class="mono">${r.funding_eta||'--:--:--'}</div>
        <div class="fund-time">до next funding</div>
      </td>
      <td><span class="spread">${fmtPct(r.spread,2)}</span></td>
      <td class="mono"><div>${fmtUsd(r.buy_vol)}</div><div>${fmtUsd(r.sell_vol)}</div></td>
    `;
    tr.querySelector('.fav').addEventListener('click',()=>togglePinned(r.symbol));
    tbody.appendChild(tr);
  });
}

async function refreshData(){
  STATE.data=await apiGet('/api/data');
  render();
}

async function boot(){
  STATE.config=await apiGet('/api/config');
  STATE.data=await apiGet('/api/data');

  document.getElementById('minVol').value=String(STATE.config.min_vol||0);
  document.getElementById('minSpread').value=String((STATE.config.min_spread||0)*100);
  document.getElementById('soundToggle').checked=STATE.sound;

  applyTheme();
  renderExchangeFilters();
  render();

  document.getElementById('q').addEventListener('input', render);
  document.getElementById('minVol').addEventListener('change', async e=>{
    const min_vol=Math.max(0,parseFloat(e.target.value||'0'));
    STATE.config=await apiPost('/api/config',{min_vol});
    await refreshData();
  });
  document.getElementById('minSpread').addEventListener('change', async e=>{
    const min_spread=Math.max(0,parseFloat(e.target.value||'0'))/100;
    STATE.config=await apiPost('/api/config',{min_spread});
    await refreshData();
  });

  document.getElementById('themeSel').addEventListener('change', e=>{
    STATE.theme=e.target.value;
    applyTheme();
  });
  document.getElementById('soundToggle').addEventListener('change', e=>{
    STATE.sound=!!e.target.checked;
    localStorage.setItem('soundOn',STATE.sound?'1':'0');
    if(STATE.sound) playSmsBeep();
  });

  document.getElementById('refreshBtn').addEventListener('click', async ()=>{
    if(cooldown>0) return;
    setCooldown(REFRESH_COOLDOWN_SEC);
    await apiPost('/api/refresh',{});
    await refreshData();
  });

  setInterval(refreshData, Math.max(1000,(STATE.config.refresh_sec||5)*1000));
}

boot();
</script>
</body>
</html>
"""


async def compute_once() -> Dict[str, Any]:
    started = time.time()
    async with aiohttp.ClientSession() as session:
        enabled = CFG.get("enabled", DEFAULT_EXCH_ENABLED)

        mexc_task = asyncio.create_task(load_mexc(session)) if enabled.get("MEXC", True) else None
        bybit_task = asyncio.create_task(load_bybit(session)) if enabled.get("Bybit", True) else None

        mexc = await mexc_task if mexc_task else {}
        bybit = await bybit_task if bybit_task else {}

        candidates: Dict[str, float] = {}
        for source in (mexc, bybit):
            for symbol, row in source.items():
                vol = row.vol24_usd if math.isfinite(row.vol24_usd) else 0.0
                candidates[symbol] = max(candidates.get(symbol, 0.0), vol)

        sorted_candidates = [x[0] for x in sorted(candidates.items(), key=lambda item: item[1], reverse=True)]
        bingx = await load_bingx(session, sorted_candidates) if enabled.get("BingX", True) else {}

    rows_out: List[dict] = []
    min_vol = float(CFG.get("min_vol", DEFAULT_MIN_VOL_USD))
    min_spread = float(CFG.get("min_spread", DEFAULT_MIN_SPREAD))
    all_symbols = set(mexc.keys()) | set(bybit.keys()) | set(bingx.keys())

    for symbol in all_symbols:
        rows = [r for r in (mexc.get(symbol), bybit.get(symbol), bingx.get(symbol)) if r]
        if len(rows) < 2:
            continue
        best = best_pair(rows, min_vol=min_vol)
        if not best:
            continue
        if best["spread"] < min_spread:
            continue
        best["symbol"] = symbol
        rows_out.append(best)

    rows_out.sort(key=lambda row: row["spread"], reverse=True)
    return {
        "updated_at": time.strftime("%H:%M:%S"),
        "rows": rows_out,
        "dbg": {
            "mexc": len(mexc),
            "bybit": len(bybit),
            "bingx": len(bingx),
            "kept": len(rows_out),
            "took_ms": int((time.time() - started) * 1000),
        },
    }


async def updater_loop():
    while True:
        try:
            data = await compute_once()
            async with CACHE_LOCK:
                CACHE.update(data)
        except Exception:
            pass
        await asyncio.sleep(max(1, int(CFG.get("refresh_sec", DEFAULT_REFRESH_SEC))))


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PAGE


@app.get("/api/config")
async def api_config():
    return JSONResponse(CFG)


@app.post("/api/config")
async def api_config_set(payload: Dict[str, Any]):
    changed = False
    for key, caster in (("min_vol", float), ("min_spread", float), ("refresh_sec", int)):
        if key in payload:
            try:
                CFG[key] = caster(payload[key])
                changed = True
            except Exception:
                pass

    if "enabled" in payload and isinstance(payload["enabled"], dict):
        enabled = {**CFG.get("enabled", DEFAULT_EXCH_ENABLED)}
        for key, value in payload["enabled"].items():
            if key in DEFAULT_EXCH_ENABLED:
                enabled[key] = bool(value)
        CFG["enabled"] = enabled
        changed = True

    if changed:
        save_config(CFG)
    return JSONResponse(CFG)


@app.get("/api/data")
async def api_data():
    async with CACHE_LOCK:
        return JSONResponse(CACHE)


@app.post("/api/refresh")
async def api_refresh():
    data = await compute_once()
    async with CACHE_LOCK:
        CACHE.update(data)
    return JSONResponse({"ok": True})


def run():
    uvicorn.run(app, host="127.0.0.1", port=8000, log_config=None, access_log=False)


if __name__ == "__main__":
    run()
