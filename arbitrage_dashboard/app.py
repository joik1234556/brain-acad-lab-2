import asyncio
import json
import math
import os
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
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
MAX_BINGX_SYMBOLS = 120
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
    base = (base or "").upper()
    return f"{'BTC' if base == 'XBT' else base}USDT"


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
        out[normalize_usdt(base)] = MarketRow(
            exchange="MEXC",
            bid=to_float(it.get("bid1")),
            ask=to_float(it.get("ask1")),
            last=to_float(it.get("lastPrice")),
            vol24_usd=to_float(it.get("amount24")),
            fund_rate=to_float(it.get("fundingRate")),
            fund24_est=funding_24h_estimate(to_float(it.get("fundingRate"))),
            url=mexc_trade_url(symbol),
        )
    return out


async def load_bybit(session: aiohttp.ClientSession) -> Dict[str, MarketRow]:
    out: Dict[str, MarketRow] = {}
    data = await fetch_json(session, BYBIT_TICKERS, params={"category": "linear"})
    lst = data.get("result", {}).get("list", []) if isinstance(data, dict) else []
    if not isinstance(lst, list):
        return out

    for it in lst:
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


async def _load_bingx_bulk(session: aiohttp.ClientSession) -> tuple[dict, dict, dict]:
    book, ticker, premium = await asyncio.gather(
        fetch_json(session, BINGX_BOOK_TICKER),
        fetch_json(session, BINGX_TICKER_24H),
        fetch_json(session, BINGX_PREMIUM_INDEX),
        return_exceptions=True,
    )
    if any(isinstance(x, Exception) for x in (book, ticker, premium)):
        return {}, {}, {}

    b_map = {str(x.get("symbol")): x for x in _as_list(book) if x.get("symbol")}
    t_map = {str(x.get("symbol")): x for x in _as_list(ticker) if x.get("symbol")}
    p_map = {str(x.get("symbol")): x for x in _as_list(premium) if x.get("symbol")}
    return b_map, t_map, p_map


async def load_bingx(session: aiohttp.ClientSession, candidate_norm: List[str]) -> Dict[str, MarketRow]:
    out: Dict[str, MarketRow] = {}
    contracts = await fetch_json(session, BINGX_CONTRACTS)
    contract_list = _as_list(contracts)

    norm_to_raw = {}
    for contract in contract_list:
        raw = str(contract.get("symbol") or "")
        if "-" not in raw:
            continue
        base, quote = raw.split("-", 1)
        if quote.upper() == "USDT":
            norm_to_raw[normalize_usdt(base)] = raw

    chosen = [sym for sym in candidate_norm if sym in norm_to_raw][:MAX_BINGX_SYMBOLS]
    if not chosen:
        return out

    b_map, t_map, p_map = await _load_bingx_bulk(session)

    async def fetch_single(raw: str):
        book, tick, prem = await asyncio.gather(
            fetch_json(session, BINGX_BOOK_TICKER, params={"symbol": raw}),
            fetch_json(session, BINGX_TICKER_24H, params={"symbol": raw}),
            fetch_json(session, BINGX_PREMIUM_INDEX, params={"symbol": raw}),
            return_exceptions=True,
        )
        if any(isinstance(x, Exception) for x in (book, tick, prem)):
            return {}, {}, {}
        return (_as_list(book)[0] if _as_list(book) else {}, _as_list(tick)[0] if _as_list(tick) else {}, _as_list(prem)[0] if _as_list(prem) else {})

    sem = asyncio.Semaphore(BINGX_CONCURRENCY)

    async def row_for(norm_sym: str):
        raw = norm_to_raw[norm_sym]
        book = b_map.get(raw, {})
        tick = t_map.get(raw, {})
        prem = p_map.get(raw, {})
        if not (book and tick):
            async with sem:
                book, tick, prem = await fetch_single(raw)

        bid = _pick_float(book, ["bidPrice", "bid", "bestBidPrice", "bestBid"])
        ask = _pick_float(book, ["askPrice", "ask", "bestAskPrice", "bestAsk"])
        last = _pick_float(tick, ["lastPrice", "last", "close", "markPrice", "indexPrice"])
        vol_quote = _pick_float(tick, ["quoteVolume", "quoteQty", "turnover", "turnover24h", "turnover24H", "quoteVolume24h", "volumeQuote"])
        vol_base = _pick_float(tick, ["volume", "baseVolume", "qty", "amount", "vol", "volume24h"])
        vol = vol_quote if is_pos(vol_quote) else vol_base * last if is_pos(vol_base) and is_pos(last) else math.nan
        fund = _pick_float(prem, ["fundingRate", "lastFundingRate", "funding"])

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

    result = await asyncio.gather(*[row_for(sym) for sym in chosen], return_exceptions=True)
    for item in result:
        if isinstance(item, tuple):
            out[item[0]] = item[1]
    return out


def exec_spread(buy: MarketRow, sell: MarketRow) -> float:
    return (sell.bid - buy.ask) / buy.ask if is_pos(buy.ask) and is_pos(sell.bid) else math.nan


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
            file_cfg = json.load(fh)
        if not isinstance(file_cfg, dict):
            return defaults
        defaults.update(file_cfg)
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

HTML_PAGE = """<!doctype html><html lang='ru'><head><meta charset='utf-8'/><meta name='viewport' content='width=device-width,initial-scale=1'/><title>Arbitrage Dashboard</title>
<style>body{margin:0;font-family:Inter,system-ui;background:#071022;color:#eaf2ff}.wrap{max-width:1200px;margin:0 auto;padding:16px}.top{display:flex;gap:12px;justify-content:space-between;flex-wrap:wrap}.panel{background:#0d1933;border:1px solid #203055;border-radius:14px;padding:10px 12px}.row{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:12px 0}.table{overflow:auto;border:1px solid #203055;border-radius:14px}.pill{font-size:12px}.btn{background:#3057c7;color:white;border:none;border-radius:8px;padding:8px 10px;cursor:pointer}table{width:100%;border-collapse:collapse;font-size:12px}th,td{padding:10px;border-bottom:1px solid #1f2b4a}th{background:#09142c;position:sticky;top:0}input,select{width:100%;padding:8px;background:#0a1430;color:#eaf2ff;border:1px solid #21345f;border-radius:8px}.good{color:#31d267;font-weight:700}.mid{color:#f0b733;font-weight:700}.mono{font-family:ui-monospace,monospace}@media(max-width:900px){.row{grid-template-columns:1fr 1fr}}@media(max-width:640px){.row{grid-template-columns:1fr}}</style></head>
<body><div class='wrap'><div class='top'><div><h2 style='margin:0'>Arbitrage Dashboard</h2><div class='pill' id='updated'>Updated: —</div></div><div style='display:flex;gap:8px;align-items:center'><button class='btn' id='refreshBtn'>↻ Refresh</button></div></div>
<div class='row'><div class='panel'><div>Поиск</div><input id='q' placeholder='BTCUSDT'/></div><div class='panel'><div>Min Volume 24h</div><select id='minVolSel'><option value='0'>0</option><option value='1000000'>1m$</option><option value='5000000'>5m$</option><option value='10000000'>10m$</option></select></div><div class='panel'><div>Min Spread</div><select id='minSpreadSel'><option value='0'>0%</option><option value='0.001'>0.1%</option><option value='0.002'>0.2%</option><option value='0.005'>0.5%</option></select></div><div class='panel'><div class='pill' id='dbg'>DBG</div></div></div>
<div class='table'><table><thead><tr><th>Symbol</th><th>Spread</th><th>Buy</th><th>Sell</th><th>Prices</th><th>Funding</th><th>Funding24</th><th>Volume24h</th></tr></thead><tbody id='tbody'><tr><td colspan='8'>Загрузка...</td></tr></tbody></table></div></div>
<script>
let STATE={config:null,data:null};
const fmt=(x,d=2)=>Number.isFinite(x)?(x*100).toFixed(d)+'%':'N/A'; const usd=x=>!Number.isFinite(x)?'N/A':x>1e6?(x/1e6).toFixed(1)+'m$':Math.round(x)+'$'; const px=x=>Number.isFinite(x)?x.toFixed(6).replace(/0+$/,'').replace(/\\.$/,''):'N/A';
async function jget(u){return (await fetch(u,{cache:'no-store'})).json()}; async function jpost(u,b){return (await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)})).json()};
function render(){ if(!STATE.data) return; let rows=STATE.data.rows||[]; const q=(document.getElementById('q').value||'').toUpperCase(); const minVol=parseFloat(document.getElementById('minVolSel').value||'0'); const minSpread=parseFloat(document.getElementById('minSpreadSel').value||'0'); rows=rows.filter(r=>(!q||r.symbol.includes(q))&&r.buy_vol>=minVol&&r.sell_vol>=minVol&&r.spread>=minSpread).sort((a,b)=>b.spread-a.spread); document.getElementById('updated').textContent='Updated: '+(STATE.data.updated_at||'—'); document.getElementById('dbg').textContent=`DBG mexc=${STATE.data.dbg.mexc} bybit=${STATE.data.dbg.bybit} bingx=${STATE.data.dbg.bingx} kept=${STATE.data.dbg.kept} took=${STATE.data.dbg.took_ms}ms`; const tb=document.getElementById('tbody'); tb.innerHTML=''; if(!rows.length){tb.innerHTML="<tr><td colspan='8'>Ничего не найдено.</td></tr>"; return;} for(const r of rows){const cls=r.spread>=0.01?'good':(r.spread>=0.004?'mid':''); tb.insertAdjacentHTML('beforeend',`<tr><td class='mono'>${r.symbol}</td><td class='mono ${cls}'>${fmt(r.spread)}</td><td><a href='${r.buy_url}' target='_blank'>${r.buy_ex}</a></td><td><a href='${r.sell_url}' target='_blank'>${r.sell_ex}</a></td><td class='mono'>${px(r.buy_ask)} / ${px(r.sell_bid)}</td><td class='mono'>${fmt(r.buy_funding,3)} / ${fmt(r.sell_funding,3)}</td><td class='mono'>${fmt(r.buy_funding24,3)} / ${fmt(r.sell_funding24,3)}</td><td class='mono'>${usd(r.buy_vol)} / ${usd(r.sell_vol)}</td></tr>`)} }
async function boot(){STATE.config=await jget('/api/config'); document.getElementById('minVolSel').value=String(STATE.config.min_vol); document.getElementById('minSpreadSel').value=String(STATE.config.min_spread); for(const id of ['q','minVolSel','minSpreadSel']) document.getElementById(id).addEventListener('input',render); document.getElementById('minVolSel').addEventListener('change',async e=>{await jpost('/api/config',{min_vol:parseFloat(e.target.value)}); STATE.config=await jget('/api/config'); STATE.data=await jget('/api/data'); render();}); document.getElementById('minSpreadSel').addEventListener('change',async e=>{await jpost('/api/config',{min_spread:parseFloat(e.target.value)}); STATE.config=await jget('/api/config'); STATE.data=await jget('/api/data'); render();}); document.getElementById('refreshBtn').addEventListener('click',async()=>{await jpost('/api/refresh',{}); STATE.data=await jget('/api/data'); render();}); STATE.data=await jget('/api/data'); render(); setInterval(async()=>{STATE.data=await jget('/api/data'); render();}, Math.max(1000,(STATE.config.refresh_sec||5)*1000)); }
boot();
</script></body></html>"""


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
            for sym, row in source.items():
                candidates[sym] = max(candidates.get(sym, 0.0), row.vol24_usd if math.isfinite(row.vol24_usd) else 0.0)
        candidate_sorted = [x[0] for x in sorted(candidates.items(), key=lambda v: v[1], reverse=True)]

        bingx = await load_bingx(session, candidate_sorted) if enabled.get("BingX", True) else {}

    rows_out = []
    min_vol = float(CFG.get("min_vol", DEFAULT_MIN_VOL_USD))
    min_spread = float(CFG.get("min_spread", DEFAULT_MIN_SPREAD))
    all_symbols = set(mexc) | set(bybit) | set(bingx)

    for symbol in all_symbols:
        rows = [x for x in (mexc.get(symbol), bybit.get(symbol), bingx.get(symbol)) if x]
        if len(rows) < 2:
            continue
        best = best_pair(rows, min_vol)
        if best and best["spread"] >= min_spread:
            best["symbol"] = symbol
            rows_out.append(best)

    rows_out.sort(key=lambda x: x["spread"], reverse=True)
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
        CFG["enabled"] = {**CFG.get("enabled", DEFAULT_EXCH_ENABLED), **{k: bool(v) for k, v in payload["enabled"].items()}}
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
