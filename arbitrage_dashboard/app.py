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
MAX_BINGX_SYMBOLS = 220
BINGX_CONCURRENCY = 12
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
    next_funding_ts: float


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


def _pick_ts(d: dict, keys: List[str]) -> float:
    for key in keys:
        raw = d.get(key)
        val = to_float(raw)
        if not math.isfinite(val):
            continue
        if val > 1e12:
            val = val / 1000.0
        if val > 1e9:
            return val
    return math.nan


def funding_eta_str(next_ts: float, fallback_hours: int = 8) -> str:
    now = datetime.now(timezone.utc)
    if math.isfinite(next_ts) and next_ts > time.time():
        target = datetime.fromtimestamp(next_ts, tz=timezone.utc)
    else:
        base = now.replace(minute=0, second=0, microsecond=0)
        step = fallback_hours
        nxt = ((base.hour // step) + 1) * step
        day = 0
        if nxt >= 24:
            nxt -= 24
            day = 1
        target = (base + timedelta(days=day)).replace(hour=nxt)

    delta = target - now
    sec = max(0, int(delta.total_seconds()))
    hh = sec // 3600
    mm = (sec % 3600) // 60
    ss = sec % 60
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
        next_ts = _pick_ts(it, ["nextSettleTime", "nextFundingTime", "fundingTime"])
        out[normalize_usdt(base)] = MarketRow(
            exchange="MEXC",
            bid=to_float(it.get("bid1")),
            ask=to_float(it.get("ask1")),
            last=to_float(it.get("lastPrice")),
            vol24_usd=to_float(it.get("amount24")),
            fund_rate=fund,
            fund24_est=funding_24h_estimate(fund),
            url=mexc_trade_url(symbol),
            next_funding_ts=next_ts,
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
        next_ts = _pick_ts(it, ["nextFundingTime", "nextFundingTimestamp"])
        out[symbol] = MarketRow(
            exchange="Bybit",
            bid=to_float(it.get("bid1Price") or it.get("bidPrice")),
            ask=to_float(it.get("ask1Price") or it.get("askPrice")),
            last=to_float(it.get("lastPrice")),
            vol24_usd=to_float(it.get("turnover24h") or it.get("turnover24H") or it.get("volume24h")),
            fund_rate=fund,
            fund24_est=funding_24h_estimate(fund),
            url=bybit_trade_url(symbol),
            next_funding_ts=next_ts,
        )
    return out


async def _load_bingx_bulk(session: aiohttp.ClientSession) -> Tuple[Dict[str, dict], Dict[str, dict], Dict[str, dict]]:
    book, ticker, prem = await asyncio.gather(
        fetch_json(session, BINGX_BOOK_TICKER),
        fetch_json(session, BINGX_TICKER_24H),
        fetch_json(session, BINGX_PREMIUM_INDEX),
        return_exceptions=True,
    )
    if any(isinstance(x, Exception) for x in (book, ticker, prem)):
        return {}, {}, {}
    b_map = {str(x.get("symbol")): x for x in _as_list(book) if x.get("symbol")}
    t_map = {str(x.get("symbol")): x for x in _as_list(ticker) if x.get("symbol")}
    p_map = {str(x.get("symbol")): x for x in _as_list(prem) if x.get("symbol")}
    return b_map, t_map, p_map


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
    if len(chosen_norms) < min(60, len(norm_to_raw)):
        seen = set(chosen_norms)
        for sym in norm_to_raw.keys():
            if sym in seen:
                continue
            chosen_norms.append(sym)
            if len(chosen_norms) >= MAX_BINGX_SYMBOLS:
                break

    b_map, t_map, p_map = await _load_bingx_bulk(session)

    async def fetch_one(norm_sym: str) -> Optional[Tuple[str, MarketRow]]:
        raw = norm_to_raw.get(norm_sym)
        if not raw:
            return None
        try:
            book = b_map.get(raw)
            tick = t_map.get(raw)
            prem = p_map.get(raw)

            if not (book and tick and prem):
                f_book, f_tick, f_prem = await asyncio.gather(
                    fetch_json(session, BINGX_BOOK_TICKER, params={"symbol": raw}),
                    fetch_json(session, BINGX_TICKER_24H, params={"symbol": raw}),
                    fetch_json(session, BINGX_PREMIUM_INDEX, params={"symbol": raw}),
                    return_exceptions=True,
                )
                if not isinstance(f_book, Exception):
                    lst = _as_list(f_book)
                    book = lst[0] if lst else book
                if not isinstance(f_tick, Exception):
                    lst = _as_list(f_tick)
                    tick = lst[0] if lst else tick
                if not isinstance(f_prem, Exception):
                    lst = _as_list(f_prem)
                    prem = lst[0] if lst else prem

            book = book or {}
            tick = tick or {}
            prem = prem or {}

            bid = _pick_float(book, ["bidPrice", "bid", "bestBidPrice", "bestBid"])
            ask = _pick_float(book, ["askPrice", "ask", "bestAskPrice", "bestAsk"])
            last = _pick_float(tick, ["lastPrice", "last", "close", "markPrice", "indexPrice"])
            vol_quote = _pick_float(tick, ["quoteVolume", "quoteQty", "turnover", "turnover24h", "turnover24H", "quoteVolume24h", "quoteVolume24H", "volumeQuote"])
            vol_base = _pick_float(tick, ["volume", "baseVolume", "qty", "amount", "vol", "volume24h"])
            vol = vol_quote if is_pos(vol_quote) else (vol_base * last if is_pos(vol_base) and is_pos(last) else math.nan)
            fund = _pick_float(prem, ["fundingRate", "lastFundingRate", "funding"])
            next_ts = _pick_ts(prem, ["nextFundingTime", "nextFundingTimestamp", "nextSettleTime"]) 

            return norm_sym, MarketRow(
                exchange="BingX",
                bid=bid,
                ask=ask,
                last=last,
                vol24_usd=vol,
                fund_rate=fund,
                fund24_est=funding_24h_estimate(fund),
                url=bingx_trade_url(raw),
                next_funding_ts=next_ts,
            )
        except Exception:
            return None

    sem = asyncio.Semaphore(BINGX_CONCURRENCY)

    async def guarded(sym: str):
        async with sem:
            return await fetch_one(sym)

    results = await asyncio.gather(*[guarded(sym) for sym in chosen_norms], return_exceptions=True)
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
        "funding_eta_buy": funding_eta_str(buy.next_funding_ts),
        "funding_eta_sell": funding_eta_str(sell.next_funding_ts),
        "buy_vol": buy.vol24_usd,
        "sell_vol": sell.vol24_usd,
        "buy_url": buy.url,
        "sell_url": sell.url,
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
      --bg:#f0f0f2;
      --panel:#e7e7ea;
      --line:#d3d4d8;
      --text:#1f2329;
      --muted:#5e6673;
      --chip:#ece5c5;
      --chip-border:#ddd4aa;
      --good:#68df8c;
      --bad:#e15d5d;
      --link:#1f2329;
    }
    body.theme-dark{
      --bg:#0b1524;--panel:#12253d;--line:#254568;--text:#e9f1ff;--muted:#96afcd;--chip:#20395a;--chip-border:#315786;--good:#63de8b;--bad:#ff8686;--link:#e9f1ff;
    }
    body.theme-light{
      --bg:#f7f8fa;--panel:#ffffff;--line:#d7dde4;--text:#1f2833;--muted:#62707e;--chip:#f4ecd1;--chip-border:#e6dcb6;--good:#53d778;--bad:#d95f5f;--link:#1f2833;
    }
    body.theme-classic{
      --bg:#f0f0f2;--panel:#e7e7ea;--line:#d3d4d8;--text:#1f2329;--muted:#5e6673;--chip:#ece5c5;--chip-border:#ddd4aa;--good:#68df8c;--bad:#e15d5d;--link:#1f2329;
    }
    *{box-sizing:border-box}
    body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,system-ui,Segoe UI,Arial,sans-serif;font-size:15px}
    .wrap{max-width:1600px;margin:0 auto;padding:12px}

    .filter-card{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:12px;margin-bottom:10px}
    .filter-head{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:10px}
    .filter-title{font-size:18px;font-weight:700}
    .btn{border:1px solid var(--line);background:var(--chip);color:var(--text);padding:8px 12px;border-radius:10px;font-size:15px;cursor:pointer}
    .btn[disabled]{opacity:.45;cursor:not-allowed}

    .filter-grid{display:grid;grid-template-columns:1.2fr 1fr 1fr 1fr 1fr 1fr auto;gap:10px;align-items:end}
    .lbl{font-size:14px;color:var(--muted);margin-bottom:6px;font-weight:600}
    input,select{width:100%;background:transparent;color:var(--text);border:1px solid var(--line);border-radius:10px;padding:10px 10px;font-size:15px}

    .chips{display:flex;gap:8px;flex-wrap:wrap}
    .chip{display:inline-flex;align-items:center;gap:8px;background:var(--chip);border:1px solid var(--chip-border);padding:6px 10px;border-radius:12px;font-size:16px;font-weight:600}
    .chip.off{opacity:.45}
    .chip img{width:22px;height:22px;object-fit:contain;border-radius:6px}

    .meta{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0 10px}
    .badge{border:1px solid var(--line);background:var(--panel);padding:6px 10px;border-radius:999px;font-size:13px;color:var(--muted)}

    .table-wrap{overflow:auto;border:1px solid var(--line);border-radius:14px;background:var(--panel)}
    table{width:100%;border-collapse:collapse;min-width:1400px}
    th,td{padding:10px 10px;border-bottom:1px solid var(--line);font-size:15px}
    th{position:sticky;top:0;background:var(--panel);text-align:left;font-size:14px;font-weight:700}
    th.sortable{cursor:pointer;user-select:none}
    th.sortable .arr{opacity:.7;margin-left:6px;font-size:12px}
    tr:hover{background:rgba(125,130,140,.09)}
    .pinned{background:rgba(241,210,66,.16)!important}

    .fav{font-size:19px;cursor:pointer}
    .token{font-size:30px;font-weight:800;line-height:1}
    .pair-line{display:flex;align-items:center;gap:8px;margin:2px 0}
    .long{color:var(--good);font-weight:700}
    .short{color:var(--bad);font-weight:700}
    .xlogo{width:22px;height:22px;object-fit:contain;border-radius:99px;background:transparent}
    a{color:var(--link);text-decoration:none}
    a:hover{text-decoration:underline}
    .mono{font-family:ui-monospace,Menlo,Consolas,monospace}
    .spread-pill{display:inline-block;background:var(--good);padding:4px 8px;border-radius:8px;font-weight:800;color:#0f2817}

    @media(max-width:1300px){.filter-grid{grid-template-columns:1fr 1fr 1fr}}
    @media(max-width:760px){.filter-grid{grid-template-columns:1fr 1fr}}
    @media(max-width:560px){.filter-grid{grid-template-columns:1fr}}
  </style>
</head>
<body class="theme-classic">
<div class="wrap">
  <div class="filter-card">
    <div class="filter-head">
      <div class="filter-title" id="filterTitle">Фильтр</div>
      <button class="btn" id="clearFiltersBtn">Очистить фильтр</button>
    </div>
    <div class="filter-grid">
      <div>
        <div class="lbl">Поиск по началу токена</div>
        <input id="q" placeholder="BTC" />
      </div>
      <div>
        <div class="lbl" id="lblMinVol">Оборот 24h (USD)</div>
        <input id="minVol" type="text" placeholder="1m / 0.5m / 250k" />
      </div>
      <div>
        <div class="lbl" id="lblMinSpread">OpenSpread, %</div>
        <input id="minSpread" type="number" min="0" step="0.01" />
      </div>
      <div>
        <div class="lbl" id="lblLang">Язык</div>
        <select id="langSel">
          <option value="ru">🇷🇺 Русский</option>
          <option value="uk">🇺🇦 Українська</option>
          <option value="en">🇬🇧 English</option>
        </select>
      </div>
      <div>
        <div class="lbl" id="lblTheme">Тема</div>
        <select id="themeSel">
          <option value="theme-dark">Dark Blue</option>
          <option value="theme-light">Light</option>
          <option value="theme-classic">Classic Gray</option>
        </select>
      </div>
      <div>
        <div class="lbl" id="lblSound">Оповещение</div>
        <label class="chip"><input type="checkbox" id="soundToggle" /> звук</label>
      </div>
      <div>
        <button class="btn" id="refreshBtn">↻ Refresh</button>
      </div>
    </div>

    <div style="border-top:1px solid var(--line);margin:12px 0 10px"></div>
    <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:8px">
      <div class="lbl" style="margin:0" id="lblExchanges">Биржи</div>
      <button class="btn" id="clearExBtn">Очистить</button>
    </div>
    <div class="chips" id="exchangeBox"></div>
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
          <th id="thToken">Токен</th>
          <th id="thPair">Покупка / Продажа</th>
          <th class="sortable" data-sort="buy_ask">Buy Ask<span class="arr"></span></th>
          <th class="sortable" data-sort="sell_bid">Sell Bid<span class="arr"></span></th>
          <th class="sortable" data-sort="buy_funding">Fund Buy<span class="arr"></span></th>
          <th class="sortable" data-sort="sell_funding">Fund Sell<span class="arr"></span></th>
          <th class="sortable" data-sort="funding_spread">F Spread<span class="arr"></span></th>
          <th>Funding calc in</th>
          <th class="sortable" data-sort="spread">Open Spread<span class="arr"></span></th>
          <th class="sortable" data-sort="buy_vol">Buy Vol<span class="arr"></span></th>
          <th class="sortable" data-sort="sell_vol">Sell Vol<span class="arr"></span></th>
        </tr>
      </thead>
      <tbody id="tbody"><tr><td colspan="12">Загрузка...</td></tr></tbody>
    </table>
  </div>
</div>
<script>
const REFRESH_COOLDOWN_SEC=8;
const EXCHANGE_LOGO={
  'MEXC':'data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"><rect width="64" height="64" rx="8" fill="white"/><path d="M10 45l13-23c3-5 10-5 13 0l13 23c2 4-1 9-6 9H16c-5 0-8-5-6-9z" fill="%231b4ae8"/><path d="M34 17c3-5 10-5 13 0l10 18c2 4 1 9-4 11l-16-27z" fill="%23255df3"/></svg>',
  'Bybit':'data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"><circle cx="32" cy="32" r="31" fill="%2318192d"/><text x="17" y="39" font-size="17" fill="white" font-family="Arial" font-weight="700">BYB</text><rect x="40" y="17" width="5" height="30" fill="%23f3b735"/><text x="46" y="39" font-size="17" fill="white" font-family="Arial" font-weight="700">T</text></svg>',
  'BingX':'data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"><circle cx="32" cy="32" r="31" fill="%231d63f0"/><path d="M18 24c5-8 13-8 20 0l8 12c3 4 2 8-3 8h-5l-9-13c-3-4-5-4-8 0h-6c0-2 1-4 3-7z" fill="white"/><path d="M46 24c-4-7-12-8-19-1l-9 11h8l5-6c3-3 5-3 8 1l6 9h7c3 0 4-4 2-7z" fill="%23dce5ff"/></svg>'
};

let lastAlertKey='';
let cooldown=0;
let timerId=null;
let STATE={
  config:null,
  data:null,
  pinned:new Set(JSON.parse(localStorage.getItem('pinnedSymbols')||'[]')),
  theme:localStorage.getItem('theme')||'theme-classic',
  sound:(localStorage.getItem('soundOn')||'0')==='1',
  lang:localStorage.getItem('lang')||'ru',
  sortKey:'spread',
  sortDir:'desc'
};

const I18N={
  ru:{
    filterTitle:'Фильтр', lblMinVol:'Оборот 24h (USD)', lblMinSpread:'OpenSpread, %', lblLang:'Язык', lblTheme:'Тема', lblSound:'Оповещение', lblExchanges:'Биржи',
    clearFilters:'Очистить фильтр', clear:'Очистить', token:'Токен', pair:'Покупка / Продажа',
  },
  uk:{
    filterTitle:'Фільтр', lblMinVol:'Обсяг 24h (USD)', lblMinSpread:'OpenSpread, %', lblLang:'Мова', lblTheme:'Тема', lblSound:'Сповіщення', lblExchanges:'Біржі',
    clearFilters:'Очистити фільтр', clear:'Очистити', token:'Токен', pair:'Купівля / Продаж',
  },
  en:{
    filterTitle:'Filter', lblMinVol:'Volume 24h (USD)', lblMinSpread:'OpenSpread, %', lblLang:'Language', lblTheme:'Theme', lblSound:'Alert', lblExchanges:'Exchanges',
    clearFilters:'Clear filter', clear:'Clear', token:'Token', pair:'Buy / Sell',
  }
};

const fmtPct=(x,d=2)=>Number.isFinite(x)?(x*100).toFixed(d)+'%':'N/A';
const fmtUsd=x=>!Number.isFinite(x)?'N/A':(x>=1e9?(x/1e9).toFixed(2)+'b$':x>=1e6?(x/1e6).toFixed(2)+'m$':x>=1e3?(x/1e3).toFixed(1)+'k$':Math.round(x)+'$');
const fmtPrice=x=>Number.isFinite(x)?x.toFixed(Math.abs(x)>=1?6:10).replace(/0+$/,'').replace(/\.$/,''):'N/A';

function playSmsBeep(){
  try{
    const ac=new (window.AudioContext||window.webkitAudioContext)();
    const osc=ac.createOscillator();
    const gain=ac.createGain();
    osc.type='triangle';
    osc.frequency.value=900;
    gain.gain.setValueAtTime(0.0001,ac.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.18,ac.currentTime+0.01);
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

function applyLang(){
  const t=I18N[STATE.lang]||I18N.ru;
  document.getElementById('filterTitle').textContent=t.filterTitle;
  document.getElementById('lblMinVol').textContent=t.lblMinVol;
  document.getElementById('lblMinSpread').textContent=t.lblMinSpread;
  document.getElementById('lblLang').textContent=t.lblLang;
  document.getElementById('lblTheme').textContent=t.lblTheme;
  document.getElementById('lblSound').textContent=t.lblSound;
  document.getElementById('lblExchanges').textContent=t.lblExchanges;
  document.getElementById('clearFiltersBtn').textContent=t.clearFilters;
  document.getElementById('clearExBtn').textContent=t.clear;
  document.getElementById('thToken').textContent=t.token;
  document.getElementById('thPair').textContent=t.pair;
  document.getElementById('langSel').value=STATE.lang;
  localStorage.setItem('lang',STATE.lang);
}

function parseVolumeInput(raw){
  const s=(raw||'').toString().trim().toLowerCase().replace(',', '.').replace('м','m');
  if(!s) return 0;
  const m=s.match(/^([0-9]+(?:\.[0-9]+)?)([kmb])?$/i);
  if(!m) return parseFloat(s)||0;
  const val=parseFloat(m[1]);
  const suf=(m[2]||'').toLowerCase();
  if(suf==='k') return val*1e3;
  if(suf==='m') return val*1e6;
  if(suf==='b') return val*1e9;
  return val;
}

function setCooldown(sec){
  cooldown=sec;
  const btn=document.getElementById('refreshBtn');
  if(timerId) clearInterval(timerId);
  timerId=setInterval(()=>{
    cooldown=Math.max(0,cooldown-1);
    btn.disabled=cooldown>0;
    btn.textContent=cooldown>0?`↻ Refresh (${cooldown})`:'↻ Refresh';
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
    const checked=!!STATE.config.enabled?.[ex];
    chip.className='chip'+(checked?'':' off');
    chip.innerHTML=`<input type="checkbox" ${checked?'checked':''}/> <img src="${EXCHANGE_LOGO[ex]}" alt="${ex}"/> ${ex}`;
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

function clearExchangeFilters(){
  STATE.config.enabled={MEXC:true,Bybit:true,BingX:true};
  apiPost('/api/config',{enabled:STATE.config.enabled}).then(async (cfg)=>{STATE.config=cfg; renderExchangeFilters(); await refreshData();});
}

function clearAllFilters(){
  document.getElementById('q').value='';
  document.getElementById('minVol').value='0';
  document.getElementById('minSpread').value='0';
  STATE.config.min_vol=0;
  STATE.config.min_spread=0;
  STATE.config.enabled={MEXC:true,Bybit:true,BingX:true};
  apiPost('/api/config',{min_vol:0,min_spread:0,enabled:STATE.config.enabled}).then(async (cfg)=>{STATE.config=cfg; renderExchangeFilters(); await refreshData();});
}

function isPinned(symbol){ return STATE.pinned.has(symbol); }
function togglePinned(symbol){
  if(STATE.pinned.has(symbol)) STATE.pinned.delete(symbol); else STATE.pinned.add(symbol);
  localStorage.setItem('pinnedSymbols',JSON.stringify([...STATE.pinned]));
  render();
}

function applyFilters(rows){
  const q=(document.getElementById('q').value||'').trim().toUpperCase();
  const minVol=parseVolumeInput(document.getElementById('minVol').value||'0');
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

function sortRows(rows){
  const key=STATE.sortKey;
  const dir=STATE.sortDir==='asc'?1:-1;
  rows.sort((a,b)=>{
    const pa=isPinned(a.symbol)?1:0;
    const pb=isPinned(b.symbol)?1:0;
    if(pa!==pb) return pb-pa;
    const va=Number.isFinite(a[key])?a[key]:-Infinity;
    const vb=Number.isFinite(b[key])?b[key]:-Infinity;
    if(va<vb) return -1*dir;
    if(va>vb) return 1*dir;
    return 0;
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

function refreshSortIndicators(){
  document.querySelectorAll('th.sortable').forEach(th=>{
    const arr=th.querySelector('.arr');
    const key=th.getAttribute('data-sort');
    arr.textContent = (key===STATE.sortKey) ? (STATE.sortDir==='asc'?'▲':'▼') : '↕';
  });
}

function render(){
  if(!STATE.data) return;
  document.getElementById('updated').textContent=`Updated: ${STATE.data.updated_at||'—'}`;
  document.getElementById('dbg').textContent=`DBG mexc=${STATE.data.dbg.mexc} bybit=${STATE.data.dbg.bybit} bingx=${STATE.data.dbg.bingx} kept=${STATE.data.dbg.kept} took=${STATE.data.dbg.took_ms}ms`;

  let rows=applyFilters([...(STATE.data.rows||[])]);
  sortRows(rows);
  refreshSortIndicators();
  maybePlayAlert(rows);

  const tbody=document.getElementById('tbody');
  tbody.innerHTML='';
  if(!rows.length){
    tbody.innerHTML='<tr><td colspan="12">Ничего не найдено по фильтрам.</td></tr>';
    return;
  }

  rows.forEach(r=>{
    const pinned=isPinned(r.symbol);
    const tr=document.createElement('tr');
    if(pinned) tr.classList.add('pinned');
    tr.innerHTML=`
      <td><span class="fav">${pinned?'★':'☆'}</span></td>
      <td class="token">${r.symbol.replace('USDT','')}</td>
      <td>
        <div class="pair-line long">⬆ LONG <img class="xlogo" src="${EXCHANGE_LOGO[r.buy_ex]}" alt="${r.buy_ex}"/> <a href="${r.buy_url}" target="_blank">${r.buy_ex}</a></div>
        <div class="pair-line short">⬇ SHORT <img class="xlogo" src="${EXCHANGE_LOGO[r.sell_ex]}" alt="${r.sell_ex}"/> <a href="${r.sell_url}" target="_blank">${r.sell_ex}</a></div>
      </td>
      <td class="mono">${fmtPrice(r.buy_ask)}</td>
      <td class="mono">${fmtPrice(r.sell_bid)}</td>
      <td class="mono">${fmtPct(r.buy_funding,3)}</td>
      <td class="mono">${fmtPct(r.sell_funding,3)}</td>
      <td class="mono">${fmtPct(r.funding_spread,3)}</td>
      <td class="mono"><div>${r.funding_eta_buy||'--:--:--'}</div><div>${r.funding_eta_sell||'--:--:--'}</div></td>
      <td><span class="spread-pill">${fmtPct(r.spread,2)}</span></td>
      <td class="mono">${fmtUsd(r.buy_vol)}</td>
      <td class="mono">${fmtUsd(r.sell_vol)}</td>
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
  applyLang();
  renderExchangeFilters();
  render();

  document.getElementById('q').addEventListener('input', render);
  document.getElementById('minVol').addEventListener('change', async e=>{
    const min_vol=Math.max(0,parseVolumeInput(e.target.value||'0'));
    STATE.config=await apiPost('/api/config',{min_vol});
    e.target.value=String(min_vol);
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

  document.getElementById('langSel').addEventListener('change', e=>{
    STATE.lang=e.target.value;
    applyLang();
    render();
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

  document.getElementById('clearFiltersBtn').addEventListener('click', clearAllFilters);
  document.getElementById('clearExBtn').addEventListener('click', clearExchangeFilters);

  document.querySelectorAll('th.sortable').forEach(th=>{
    th.addEventListener('click',()=>{
      const key=th.getAttribute('data-sort');
      if(STATE.sortKey===key){
        STATE.sortDir=STATE.sortDir==='asc'?'desc':'asc';
      }else{
        STATE.sortKey=key;
        STATE.sortDir='desc';
      }
      render();
    });
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
