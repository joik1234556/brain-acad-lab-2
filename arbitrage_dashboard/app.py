import asyncio
import base64
import json
import hashlib
import math
import os
import secrets
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
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


BASE_DIR = app_dir()
ASSETS_DIR = os.path.join(BASE_DIR, "assets")
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
LOGOS_DIR = os.path.join(ASSETS_DIR, "logos")
SOUNDS_DIR = os.path.join(ASSETS_DIR, "sounds")
CONFIG_PATH = os.path.join(BASE_DIR, "arb_dashboard_config.json")
AUTH_KEY_PATH = os.path.join(BASE_DIR, "auth_secret.key")
USERS_DB_PATH = os.path.join(BASE_DIR, "users.db.enc")
DEFAULT_REFRESH_SEC = 1
DEFAULT_MIN_VOL_USD = 5_000_000.0
DEFAULT_MIN_SPREAD = 0.0
HTTP_TIMEOUT = 12
MAX_BINGX_SYMBOLS = 260
BINGX_CONCURRENCY = 18
DEFAULT_EXCH_ENABLED = {"MEXC": True, "Bybit": True, "BingX": True}
MAX_FREE_SPREAD = 0.02
SESSION_TTL_SEC = 7 * 24 * 3600

MEXC_TICKERS = "https://contract.mexc.com/api/v1/contract/ticker"
BYBIT_TICKERS = "https://api.bybit.com/v5/market/tickers"
BINGX_CONTRACTS = "https://open-api.bingx.com/openApi/swap/v2/quote/contracts"
BINGX_BOOK_TICKER = "https://open-api.bingx.com/openApi/swap/v2/quote/bookTicker"
BINGX_TICKER_24H = "https://open-api.bingx.com/openApi/swap/v2/quote/ticker"
BINGX_PREMIUM_INDEX = "https://open-api.bingx.com/openApi/swap/v2/quote/premiumIndex"


def _get_or_create_auth_key() -> bytes:
    env_key = os.environ.get("ARB_AUTH_KEY")
    if env_key:
        return env_key.encode("utf-8")
    if os.path.exists(AUTH_KEY_PATH):
        with open(AUTH_KEY_PATH, "rb") as fh:
            return fh.read().strip()
    key = Fernet.generate_key()
    with open(AUTH_KEY_PATH, "wb") as fh:
        fh.write(key)
    return key


def _hash_password(password: str, salt_b64: str) -> str:
    salt = base64.b64decode(salt_b64.encode("utf-8"))
    raw = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 250_000)
    return base64.b64encode(raw).decode("utf-8")


def _make_password_record(password: str) -> Tuple[str, str]:
    salt_b64 = base64.b64encode(secrets.token_bytes(16)).decode("utf-8")
    return salt_b64, _hash_password(password, salt_b64)


def _verify_password(password: str, salt_b64: str, expected_hash: str) -> bool:
    return secrets.compare_digest(_hash_password(password, salt_b64), expected_hash)


def _normalize_username(username: str) -> str:
    return "".join(ch for ch in (username or "").strip().lower() if ch.isalnum() or ch in "._-")[:32]


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
    funding_interval_h: int


def ensure_assets() -> None:
    os.makedirs(LOGOS_DIR, exist_ok=True)
    os.makedirs(SOUNDS_DIR, exist_ok=True)

    logos_readme = os.path.join(LOGOS_DIR, "README.txt")
    if not os.path.exists(logos_readme):
        with open(logos_readme, "w", encoding="utf-8") as f:
            f.write(
                "Put exchange logos here by naming:\n"
                "- mexc.png / mexc.svg\n"
                "- bybit.png / bybit.svg\n"
                "- bingx.png / bingx.svg\n"
            )

    sounds_readme = os.path.join(SOUNDS_DIR, "README.txt")
    if not os.path.exists(sounds_readme):
        with open(sounds_readme, "w", encoding="utf-8") as f:
            f.write("Put notification sounds here (wav/mp3/ogg), e.g. sms.wav\n")


def find_logo(exchange: str) -> str:
    base = exchange.lower()
    for ext in (".svg", ".png", ".jpg", ".jpeg", ".webp"):
        p = os.path.join(LOGOS_DIR, base + ext)
        if os.path.exists(p):
            return f"/assets/logos/{base}{ext}"
    return ""


def list_sounds() -> List[str]:
    out: List[str] = []
    if not os.path.isdir(SOUNDS_DIR):
        return out
    for name in sorted(os.listdir(SOUNDS_DIR)):
        if name.lower().endswith((".wav", ".mp3", ".ogg")):
            out.append(name)
    return out


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


def normalize_symbol_key(symbol: str) -> str:
    return (symbol or "").upper().replace("-", "").replace("_", "").replace("/", "")


def _as_list(resp: Any) -> List[dict]:
    if isinstance(resp, dict):
        d = resp.get("data")
        if isinstance(d, list):
            return [x for x in d if isinstance(x, dict)]
        if isinstance(d, dict):
            if isinstance(d.get("list"), list):
                return [x for x in d.get("list") if isinstance(x, dict)]
            return [d]
    if isinstance(resp, list):
        return [x for x in resp if isinstance(x, dict)]
    return []


def _pick_float(d: dict, keys: List[str]) -> float:
    for key in keys:
        value = to_float(d.get(key))
        if math.isfinite(value):
            return value
    return math.nan


def _match_symbol_entry(items: List[dict], variants: List[str]) -> Optional[dict]:
    if not items:
        return None
    wanted = {normalize_symbol_key(v) for v in variants if v}
    if not wanted:
        return items[0]
    for it in items:
        s = str(it.get("symbol") or it.get("s") or "")
        if normalize_symbol_key(s) in wanted:
            return it
    if len(items) == 1:
        return items[0]
    return None


def _pick_ts(d: dict, keys: List[str]) -> float:
    for key in keys:
        raw = d.get(key)
        val = to_float(raw)
        if not math.isfinite(val):
            continue
        if val > 1e12:
            val /= 1000.0
        if val > 1e9:
            return val
    return math.nan


def _pick_int(d: dict, keys: List[str], default: int = 8) -> int:
    for key in keys:
        raw = d.get(key)
        if raw is None:
            continue
        if isinstance(raw, str):
            txt = raw.strip().lower().replace("hours", "h").replace("hour", "h")
            if txt.endswith("h"):
                txt = txt[:-1]
            val = to_float(txt)
        else:
            val = to_float(raw)
        if math.isfinite(val) and val > 0:
            while val > 24 and val % 60 == 0:
                val = val / 60.0
            return max(1, int(round(val)))
    return default


def funding_eta_str(next_ts: float, fallback_hours: int = 8) -> str:
    now = datetime.now(timezone.utc)
    if math.isfinite(next_ts) and next_ts > time.time():
        target = datetime.fromtimestamp(next_ts, tz=timezone.utc)
    else:
        base = now.replace(minute=0, second=0, microsecond=0)
        nxt = ((base.hour // fallback_hours) + 1) * fallback_hours
        day = 0
        if nxt >= 24:
            nxt -= 24
            day = 1
        target = (base + timedelta(days=day)).replace(hour=nxt)
    sec = max(0, int((target - now).total_seconds()))
    return f"{sec//3600:02d}:{(sec%3600)//60:02d}:{sec%60:02d}"


async def fetch_json(session: aiohttp.ClientSession, url: str, params: Optional[dict] = None) -> Any:
    async with session.get(url, params=params, timeout=HTTP_TIMEOUT) as response:
        return await response.json(content_type=None)


async def load_mexc(session: aiohttp.ClientSession) -> Dict[str, MarketRow]:
    out: Dict[str, MarketRow] = {}
    try:
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
            next_ts = _pick_ts(it, ["nextFundingTime", "nextSettleTime", "fundingTime"])
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
                funding_interval_h=_pick_int(it, ["fundingInterval", "settleInterval", "collectCycle"], default=8),
            )
    except Exception as e:
        print(f"MEXC load error: {type(e).__name__}: {e}")
        return {}
    return out


async def load_bybit(session: aiohttp.ClientSession) -> Dict[str, MarketRow]:
    out: Dict[str, MarketRow] = {}
    try:
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
                funding_interval_h=_pick_int(it, ["fundingIntervalHour", "fundingInterval", "fundingIntervalHours"], default=8),
            )
    except Exception as e:
        print(f"Bybit load error: {type(e).__name__}: {e}")
        return {}
    return out


async def load_bingx(session: aiohttp.ClientSession, candidate_norm: List[str], on_symbol=None) -> Dict[str, MarketRow]:
    out: Dict[str, MarketRow] = {}
    try:
        dbg = {"selected": 0, "from_bulk": 0, "from_fallback": 0, "rejected_no_quote": 0}
        contracts_resp = await fetch_json(session, BINGX_CONTRACTS)
        contracts = _as_list(contracts_resp)

        norm_to_raw: Dict[str, str] = {}
        contract_by_raw: Dict[str, dict] = {}
        for c in contracts:
            raw = str(c.get("symbol") or "")
            if not raw:
                continue
            contract_by_raw[raw] = c
            if "-" in raw:
                base, quote = raw.split("-", 1)
                if quote.upper() == "USDT":
                    norm_to_raw[normalize_usdt(base)] = raw
            else:
                upper_raw = raw.upper()
                if upper_raw.endswith("USDT"):
                    norm_to_raw[upper_raw] = raw

        selected = [s for s in candidate_norm if s in norm_to_raw][:MAX_BINGX_SYMBOLS]
        if len(selected) < 120:
            for s in norm_to_raw:
                if s not in selected:
                    selected.append(s)
                if len(selected) >= MAX_BINGX_SYMBOLS:
                    break

        sem = asyncio.Semaphore(BINGX_CONCURRENCY)
        dbg["selected"] = len(selected)
        bulk_book_resp, bulk_tick_resp, bulk_prem_resp = await asyncio.gather(
            fetch_json(session, BINGX_BOOK_TICKER),
            fetch_json(session, BINGX_TICKER_24H),
            fetch_json(session, BINGX_PREMIUM_INDEX),
            return_exceptions=True,
        )
        bulk_book: Dict[str, dict] = {}
        bulk_tick: Dict[str, dict] = {}
        bulk_prem: Dict[str, dict] = {}
        if not isinstance(bulk_book_resp, Exception):
            bulk_book = {normalize_symbol_key(str(x.get("symbol") or "")): x for x in _as_list(bulk_book_resp)}
        if not isinstance(bulk_tick_resp, Exception):
            bulk_tick = {normalize_symbol_key(str(x.get("symbol") or "")): x for x in _as_list(bulk_tick_resp)}
        if not isinstance(bulk_prem_resp, Exception):
            bulk_prem = {normalize_symbol_key(str(x.get("symbol") or "")): x for x in _as_list(bulk_prem_resp)}

        async def one(norm_sym: str) -> Optional[Tuple[str, MarketRow]]:
            raw = norm_to_raw.get(norm_sym)
            if not raw:
                return None
            contract = contract_by_raw.get(raw, {})

            async def fetch_symbol(url: str) -> dict:
                variants = [raw]
                compact = raw.replace("-", "")
                undersc = raw.replace("-", "_")
                for v in (compact, undersc):
                    if v not in variants:
                        variants.append(v)
                for sym in variants:
                    resp = await fetch_json(session, url, params={"symbol": sym})
                    lst = _as_list(resp)
                    if lst:
                        rec = _match_symbol_entry(lst, variants)
                        if rec:
                            return rec
                return {}

            try:
                raw_key = normalize_symbol_key(raw)
                book = dict(bulk_book.get(raw_key, {}))
                tick = dict(bulk_tick.get(raw_key, {}))
                prem = dict(bulk_prem.get(raw_key, {}))

                used_fallback = False
                if not (book and tick):
                    used_fallback = True
                    async with sem:
                        fb, ft, fp = await asyncio.gather(
                            fetch_symbol(BINGX_BOOK_TICKER),
                            fetch_symbol(BINGX_TICKER_24H),
                            fetch_symbol(BINGX_PREMIUM_INDEX),
                            return_exceptions=True,
                        )
                    if isinstance(fb, dict) and fb:
                        book = fb
                    if isinstance(ft, dict) and ft:
                        tick = ft
                    if isinstance(fp, dict) and fp:
                        prem = fp

                bid = _pick_float(book, ["bidPrice", "bid", "bestBidPrice", "bestBid"])
                ask = _pick_float(book, ["askPrice", "ask", "bestAskPrice", "bestAsk"])
                if not is_pos(bid):
                    bid = _pick_float(tick, ["bidPrice", "bid", "bestBidPrice", "bestBid"])
                if not is_pos(ask):
                    ask = _pick_float(tick, ["askPrice", "ask", "bestAskPrice", "bestAsk"])
                last = _pick_float(tick, ["lastPrice", "last", "close", "markPrice", "indexPrice"])

                vol_quote = _pick_float(tick, [
                    "quoteVolume", "quoteQty", "turnover", "turnover24h", "turnover24H", "quoteVolume24h", "quoteVolume24H",
                    "amountQuote", "volumeQuote",
                ])
                vol_base = _pick_float(tick, ["volume", "baseVolume", "qty", "baseQty", "amount", "vol", "amountBase", "volumeBase", "volume24h"])
                vol = vol_quote
                if not is_pos(vol):
                    price = last if is_pos(last) else (bid + ask) / 2 if is_pos(bid) and is_pos(ask) else math.nan
                    if is_pos(vol_base) and is_pos(price):
                        vol = vol_base * price
                if not is_pos(vol):
                    vol = _pick_float(contract, ["quoteVolume", "quoteVolume24h", "turnover", "turnover24h", "amount24", "volumeQuote"])

                fund = _pick_float(prem, ["fundingRate", "lastFundingRate", "funding"])
                if not math.isfinite(fund):
                    fund = _pick_float(tick, ["fundingRate", "lastFundingRate", "funding"])
                next_ts = _pick_ts(prem, ["nextFundingTime", "nextFundingTimestamp", "nextSettleTime"])
                if not math.isfinite(next_ts):
                    next_ts = _pick_ts(contract, ["nextFundingTime", "nextFundingTimestamp", "nextSettleTime"])

                if not (is_pos(bid) and is_pos(ask)):
                    dbg["rejected_no_quote"] += 1
                    return None

                if used_fallback:
                    dbg["from_fallback"] += 1
                else:
                    dbg["from_bulk"] += 1

                market_row = MarketRow(
                    exchange="BingX",
                    bid=bid,
                    ask=ask,
                    last=last,
                    vol24_usd=vol,
                    fund_rate=fund,
                    fund24_est=funding_24h_estimate(fund),
                    url=bingx_trade_url(raw),
                    next_funding_ts=next_ts,
                    funding_interval_h=_pick_int(
                        prem if prem else contract,
                        ["fundingIntervalHours", "fundingIntervalHour", "fundingInterval", "fundingRateInterval"],
                        default=8,
                    ),
                )
                if on_symbol is not None:
                    try:
                        await on_symbol(norm_sym, market_row)
                    except Exception:
                        pass
                return norm_sym, market_row
            except Exception:
                return None

        res = await asyncio.gather(*[one(s) for s in selected], return_exceptions=True)
        for item in res:
            if isinstance(item, tuple):
                out[item[0]] = item[1]
        print(
            f"[BingX] selected={dbg['selected']} ok={len(out)} "
            f"bulk={dbg['from_bulk']} fallback={dbg['from_fallback']} rejected={dbg['rejected_no_quote']}"
        )
        return out
    except Exception as e:
        print(f"BingX load error: {type(e).__name__}: {e}")
        return {}


def exec_spread(buy: MarketRow, sell: MarketRow) -> float:
    if not (is_pos(buy.ask) and is_pos(sell.bid)):
        return math.nan
    return (sell.bid - buy.ask) / buy.ask


def _adjusted_fund(rate: float, next_ts: float, interval_h: int) -> float:
    """Return funding rate scaled by the fraction of the current period remaining.

    Adjusted = rate × (time_left / interval)
    This gives the expected funding payment until the nearest settlement.
    If next_ts is unknown, returns the full rate (worst-case assumption).
    """
    if not math.isfinite(rate):
        return math.nan
    if math.isfinite(next_ts) and next_ts > time.time():
        time_left_h = (next_ts - time.time()) / 3600.0
        ratio = min(1.0, max(0.0, time_left_h / interval_h)) if interval_h > 0 else 1.0
    else:
        ratio = 1.0  # unknown next_ts → assume full period remaining
    return rate * ratio


def best_pairs(rows: List[MarketRow], min_vol: float) -> List[Dict[str, Any]]:
    def _vol_ok(row: MarketRow) -> bool:
        if row.exchange == "BingX":
            return True
        if math.isfinite(row.vol24_usd):
            return row.vol24_usd >= min_vol
        return False

    valid = [r for r in rows if is_pos(r.ask) and is_pos(r.bid) and _vol_ok(r)]
    if len(valid) < 2:
        return []

    out: List[Dict[str, Any]] = []
    for buy in valid:
        for sell in valid:
            if buy.exchange == sell.exchange:
                continue
            spread = exec_spread(buy, sell)
            if not math.isfinite(spread):
                continue
            adj_buy = _adjusted_fund(buy.fund_rate, buy.next_funding_ts, buy.funding_interval_h)
            adj_sell = _adjusted_fund(sell.fund_rate, sell.next_funding_ts, sell.funding_interval_h)
            fund_spread = adj_sell - adj_buy if math.isfinite(adj_buy) and math.isfinite(adj_sell) else math.nan
            out.append({
                "spread": spread,
                "pair_key": "",
                "buy_ex": buy.exchange,
                "sell_ex": sell.exchange,
                "buy_ask": buy.ask,
                "sell_bid": sell.bid,
                "buy_funding": buy.fund_rate,
                "sell_funding": sell.fund_rate,
                "buy_funding_adjusted": adj_buy,
                "sell_funding_adjusted": adj_sell,
                "funding_spread": fund_spread,
                "funding_eta_buy": funding_eta_str(buy.next_funding_ts, fallback_hours=buy.funding_interval_h),
                "funding_eta_sell": funding_eta_str(sell.next_funding_ts, fallback_hours=sell.funding_interval_h),
                "buy_funding_interval": f"{buy.funding_interval_h}h",
                "sell_funding_interval": f"{sell.funding_interval_h}h",
                "buy_vol": buy.vol24_usd,
                "sell_vol": sell.vol24_usd,
                "buy_url": buy.url,
                "sell_url": sell.url,
            })
    return out


def _push_pairs_to_live_rows(
    mexc: Dict[str, "MarketRow"],
    bybit: Dict[str, "MarketRow"],
    bingx: Dict[str, "MarketRow"],
    min_vol: float,
    min_spread: float,
    symbols: Optional[set] = None,
) -> None:
    if symbols is None:
        symbols = set(mexc.keys()) | set(bybit.keys()) | set(bingx.keys())
    for symbol in symbols:
        market_rows = [r for r in (mexc.get(symbol), bybit.get(symbol), bingx.get(symbol)) if r is not None]
        if len(market_rows) < 2:
            continue
        pairs = best_pairs(market_rows, min_vol=min_vol)
        for pair in pairs:
            if min_spread > 0 and pair["spread"] < min_spread:
                continue
            pair["symbol"] = symbol
            key = f"{symbol}|{pair['buy_ex']}|{pair['sell_ex']}"
            pair["pair_key"] = key
            LIVE_ROWS[key] = pair


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


AUTH_CIPHER = Fernet(_get_or_create_auth_key())
RSA_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
RSA_PUBLIC_PEM = RSA_PRIVATE_KEY.public_key().public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo,
).decode("utf-8")


def _save_users(users: Dict[str, Any]) -> None:
    raw = json.dumps(users, ensure_ascii=False).encode("utf-8")
    token = AUTH_CIPHER.encrypt(raw)
    with open(USERS_DB_PATH, "wb") as fh:
        fh.write(token)


def _seed_admin(users: Dict[str, Any]) -> None:
    if "admin" not in users:
        salt, pwh = _make_password_record("salimonenkodima")
        users["admin"] = {
            "username": "admin",
            "salt": salt,
            "password_hash": pwh,
            "is_admin": True,
            "subscription_approved": True,
            "created_at": int(time.time()),
        }
    if "adminegor" not in users:
        salt2, pwh2 = _make_password_record("egorkorotkov96!")
        users["adminegor"] = {
            "username": "adminegor",
            "salt": salt2,
            "password_hash": pwh2,
            "is_admin": True,
            "subscription_approved": True,
            "created_at": int(time.time()),
        }


def _load_users() -> Dict[str, Any]:
    users: Dict[str, Any] = {}
    if os.path.exists(USERS_DB_PATH):
        try:
            with open(USERS_DB_PATH, "rb") as fh:
                users = json.loads(AUTH_CIPHER.decrypt(fh.read()).decode("utf-8"))
        except Exception:
            users = {}
    _seed_admin(users)
    _save_users(users)
    return users


def _decrypt_client_field(value: str) -> str:
    if not isinstance(value, str) or not value:
        return ""
    decoded = base64.b64decode(value.encode("utf-8"))
    plain = RSA_PRIVATE_KEY.decrypt(
        decoded,
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    return plain.decode("utf-8")


def _extract_auth_credentials(payload: Dict[str, Any]) -> Tuple[str, str]:
    plain_username = _normalize_username(str(payload.get("username") or ""))
    plain_password = str(payload.get("password") or "")

    dec_username = ""
    dec_password = ""

    enc_u = str(payload.get("username_enc") or "")
    enc_p = str(payload.get("password_enc") or "")
    if enc_u and enc_p:
        try:
            dec_username = _normalize_username(_decrypt_client_field(enc_u))
            dec_password = _decrypt_client_field(enc_p)
        except Exception:
            dec_username = ""
            dec_password = ""

    username = dec_username or plain_username
    password = dec_password or plain_password
    return username, password


USERS = _load_users()
USERS_LOCK = asyncio.Lock()
SESSIONS: Dict[str, Dict[str, Any]] = {}


def _make_session(username: str) -> str:
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = {"username": username, "expires": time.time() + SESSION_TTL_SEC}
    return token


def _session_user(request: Request) -> Optional[Dict[str, Any]]:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:]
    rec = SESSIONS.get(token)
    if not rec:
        return None
    if rec["expires"] < time.time():
        SESSIONS.pop(token, None)
        return None
    user = USERS.get(rec["username"])
    if not user:
        return None
    return user


def _limit_rows_for_access(rows: List[dict], user: Optional[Dict[str, Any]]) -> Tuple[List[dict], Optional[float], bool, bool]:
    is_admin = bool(user and user.get("is_admin"))
    is_logged = bool(user)
    is_paid = bool(user and user.get("subscription_approved"))
    spread_limit: Optional[float] = None
    if not is_logged:
        spread_limit = MAX_FREE_SPREAD
        rows = [r for r in rows if float(r.get("spread") or 0.0) <= spread_limit]
    return rows, spread_limit, is_admin, is_paid


@asynccontextmanager
async def lifespan(_: FastAPI):
    asyncio.create_task(updater_loop())
    yield


ensure_assets()
app = FastAPI(lifespan=lifespan)
app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)
CFG = load_config()
CACHE = {"updated_at": None, "rows": [], "dbg": {"mexc": 0, "bybit": 0, "bingx": 0, "kept": 0, "took_ms": 0}}
CACHE_LOCK = asyncio.Lock()
PAIR_HISTORY: Dict[str, List[Dict[str, Any]]] = {}
PAIR_HISTORY_MAX = 300
LIVE_ROWS: Dict[str, dict] = {}
_SSE_QUEUES: List[asyncio.Queue] = []


def _broadcast_sse(payload: str) -> None:
    """Push a message to every connected SSE client (fire-and-forget)."""
    for q in list(_SSE_QUEUES):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            pass  # slow client – skip this tick

HTML_PAGE = r"""
<!doctype html><html lang="ru"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/><title>Arbitrage Insights</title><link rel="icon" type="image/png" href="/static/mmua-logo.png"/>
<link rel="preconnect" href="https://fonts.googleapis.com"/><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/><link href="https://fonts.googleapis.com/css2?family=Inter:wght@600;700;800;900&display=swap" rel="stylesheet"/>
<style>
:root,body.theme-classic{--bg:#eaecf4;--panel:#ffffff;--line:#dde0ec;--text:#13161e;--muted:#5d6480;--chip:#ede8df;--good:#0da05c;--bad:#d8233a;--link:#4f46e5;--accent:#4f46e5;--accent2:#7c3aed;--glow:rgba(79,70,229,.18);--shadow:0 4px 28px rgba(79,70,229,.09)}
body.theme-dark-blue{--bg:#040c1a;--panel:#0b1d38;--line:#173260;--text:#d6eaff;--muted:#6896cc;--chip:#0e2040;--good:#00d68f;--bad:#ff6060;--link:#5aaeff;--accent:#4aa8ff;--accent2:#818cf8;--glow:rgba(74,168,255,.22);--shadow:0 6px 36px rgba(0,90,220,.18)}
body.theme-light{--bg:#f1f5fd;--panel:#ffffff;--line:#d4ddf0;--text:#1a2030;--muted:#526080;--chip:#eaeeff;--good:#059669;--bad:#e01c44;--link:#2254e6;--accent:#2254e6;--accent2:#7c3aed;--glow:rgba(34,84,230,.18);--shadow:0 4px 22px rgba(34,84,230,.07)}
body.theme-binance{--bg:#07090f;--panel:#0f1420;--line:#1c2438;--text:#f0f2f8;--muted:#78889c;--chip:#141b2c;--good:#0ecb81;--bad:#f6465d;--link:#f0b90b;--accent:#f0b90b;--accent2:#e8a800;--glow:rgba(240,185,11,.20);--shadow:0 6px 32px rgba(240,185,11,.07)}
body.theme-tradingview{--bg:#0a0c12;--panel:#131620;--line:#20293c;--text:#c4c8d4;--muted:#646e82;--chip:#18202e;--good:#26a69a;--bad:#ef5350;--link:#4e88d6;--accent:#4e88d6;--accent2:#7b61ff;--glow:rgba(78,136,214,.20);--shadow:0 6px 30px rgba(78,136,214,.07)}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-family:'Inter',system-ui,'Segoe UI',Arial,sans-serif;font-size:14px;line-height:1.55;-webkit-font-smoothing:antialiased}
body.theme-dark-blue{background-image:radial-gradient(ellipse 80% 40% at 15% 0%,rgba(74,168,255,.07) 0%,transparent 60%)}
body.theme-binance{background-image:radial-gradient(ellipse 60% 30% at 85% 5%,rgba(240,185,11,.06) 0%,transparent 55%)}
body.theme-tradingview{background-image:radial-gradient(ellipse 60% 30% at 10% 90%,rgba(78,136,214,.06) 0%,transparent 55%)}
.wrap{max-width:1640px;margin:0 auto;padding:14px}
.filter-card{background:var(--panel);border:1px solid var(--line);border-radius:20px;padding:16px;margin-bottom:12px;box-shadow:var(--shadow)}
.topbar{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:12px}
.brand{display:flex;align-items:center;gap:11px;font-weight:900;font-size:19px;color:var(--accent);background:linear-gradient(130deg,var(--accent),var(--good));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.brand img{width:44px;height:44px;border-radius:13px;border:1.5px solid var(--line);box-shadow:var(--shadow)}
.topbar .lang-box{display:flex;align-items:center;gap:8px}.topbar select{min-width:160px}
.filter-head{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:12px}.filter-title{font-size:16px;font-weight:700}
.btn{border:1.5px solid var(--line);background:var(--chip);color:var(--text);padding:8px 15px;border-radius:12px;font-size:13px;font-weight:600;cursor:pointer;transition:all .17s cubic-bezier(.4,0,.2,1);letter-spacing:.01em}
.btn:hover{border-color:var(--accent);color:var(--accent);box-shadow:0 0 0 3px var(--glow);transform:translateY(-1px)}.btn:active{transform:translateY(0);box-shadow:none}.btn[disabled]{opacity:.36;cursor:not-allowed;transform:none}
.filter-actions{display:flex;gap:8px;align-items:center}.filter-panel{display:none;border-top:1px solid var(--line);padding-top:14px;margin-top:12px}.filter-panel.open{display:block}.filter-grid{display:grid;grid-template-columns:1.2fr 1fr 1fr 1fr 1fr;gap:12px;align-items:end}
.lbl{font-size:11px;color:var(--muted);margin-bottom:5px;font-weight:700;text-transform:uppercase;letter-spacing:.07em}
input,select{width:100%;background:var(--bg);color:var(--text);border:1.5px solid var(--line);border-radius:12px;padding:9px 12px;font-size:13px;font-family:inherit;transition:border-color .14s,box-shadow .14s;outline:none}
input:focus,select:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--glow)}
body.theme-dark-blue select option,body.theme-binance select option,body.theme-tradingview select option{background:#0f1420;color:#ddeeff}
.chips{display:flex;gap:8px;flex-wrap:wrap}.chip{display:inline-flex;align-items:center;gap:8px;background:var(--chip);border:1.5px solid var(--line);padding:7px 14px;border-radius:100px;font-size:13px;font-weight:600;transition:all .17s;cursor:pointer}
.chip:hover{border-color:var(--accent);box-shadow:0 0 0 3px var(--glow);color:var(--accent)}.chip.off{opacity:.38;filter:grayscale(.5)}
.chip img{width:20px;height:20px;object-fit:contain;border-radius:50%}
.meta{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0 12px}.badge{border:1px solid var(--line);background:var(--bg);padding:5px 12px;border-radius:100px;font-size:11px;color:var(--muted);font-weight:500}
.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:18px;background:var(--panel);box-shadow:var(--shadow)} table{width:100%;border-collapse:collapse;min-width:1450px}
th,td{padding:10px 12px;border-bottom:1px solid var(--line);font-size:13px} th{position:sticky;top:0;background:var(--panel);text-align:left;font-size:11px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;z-index:1}
th.sortable{cursor:pointer;user-select:none;transition:color .14s} th.sortable:hover{color:var(--accent)} th.sortable .arr{opacity:.7;margin-left:4px;font-size:10px}
tr{transition:background .1s} tr:hover{background:rgba(128,136,160,.07)} .pinned{background:rgba(239,208,70,.10)!important}
.fav{font-size:16px;cursor:pointer;transition:transform .15s;display:inline-block}.fav:hover{transform:scale(1.35)}
.token{font-size:15px;font-weight:800;letter-spacing:-.02em}.pair-line{display:flex;align-items:center;gap:7px;min-height:34px}
.long{color:var(--good);font-weight:700;font-size:11px;letter-spacing:.03em}.short{color:var(--bad);font-weight:700;font-size:11px;letter-spacing:.03em}.xlogo{width:18px;height:18px;object-fit:contain;border-radius:50%}
.split-cell{padding:0!important}.split-cell .line{display:flex;align-items:center;min-height:34px;padding:0 12px}.split-cell .line + .line{border-top:1px solid var(--line)}
.auth-wrap{margin:10px 0;padding:12px 14px;border:1px solid var(--line);border-radius:16px;background:var(--bg)}
.auth-row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.auth-row input{max-width:200px}
#authForm{display:none}
.small{font-size:12px;color:var(--muted)}
a{color:var(--link);text-decoration:none}a:hover{text-decoration:underline}.mono{font-family:ui-monospace,'JetBrains Mono',Menlo,Consolas,monospace}
.spread-pill{display:inline-block;padding:4px 11px;border-radius:100px;font-weight:800;font-size:12px;letter-spacing:.02em}.spread-pill.pos{background:var(--good);color:#0a2010;box-shadow:0 2px 12px rgba(0,200,100,.28)}.spread-pill.neg{background:var(--bad);color:#fff;box-shadow:0 2px 8px rgba(220,30,30,.22)}.fpos{color:var(--good);font-weight:600}.fneg{color:var(--bad);font-weight:600}
@media(max-width:1300px){.filter-grid{grid-template-columns:1fr 1fr 1fr}}
@media(max-width:760px){.topbar{flex-wrap:wrap}.filter-grid{grid-template-columns:1fr 1fr}}
@media(max-width:560px){.filter-grid{grid-template-columns:1fr}}
@media(max-width:639px){
.wrap{padding:8px}
.filter-card{padding:10px 12px;border-radius:14px}
.topbar{flex-direction:column;align-items:stretch;gap:8px;margin-bottom:8px}
.topbar .lang-box{width:100%}.topbar .lang-box .lbl{display:none}.topbar .lang-box select,.topbar select{min-width:unset;width:100%}
.brand{font-size:16px}.brand img{width:36px;height:36px;border-radius:10px}
.filter-head{flex-wrap:wrap;gap:6px}
.filter-actions{width:100%;gap:6px}.filter-actions .btn{flex:1;text-align:center;padding:10px 6px;font-size:12px}
.auth-wrap{padding:8px 10px}
.auth-row{flex-direction:column;align-items:stretch;gap:6px}
.auth-row .btn{width:100%;text-align:center;padding:10px}.auth-row input{max-width:100%!important;width:100%}
.meta{gap:5px}.badge{padding:4px 10px;font-size:10px}
.table-wrap{overflow:visible;border:none;background:transparent;box-shadow:none;border-radius:0;margin:0 -2px}
table{display:block;min-width:unset;width:100%}
thead{display:none}
tbody{display:flex;flex-direction:column;gap:10px}
tr{display:grid;grid-template-columns:1fr 1fr;gap:2px 10px;background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:10px 12px;box-shadow:var(--shadow)}
tr.empty-row{display:block}
tr.empty-row td{text-align:center;padding:18px;font-size:14px;color:var(--muted)}
td{display:flex;flex-direction:column;padding:2px 0;border:none;font-size:12px;gap:1px;min-width:0}
td::before{content:attr(data-label);font-size:9px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;line-height:1.5}
td[data-col=fav]{display:none}
td[data-col=token]{grid-column:1;font-size:16px;font-weight:800;letter-spacing:-.02em;justify-content:center;padding:0 0 6px}
td[data-col=spread]{grid-column:2;align-items:flex-end;justify-content:center;padding:0 0 6px}
td[data-col=pair]{grid-column:1/-1;border-bottom:1px solid var(--line);padding-bottom:7px;margin-bottom:2px}
td[data-col=price]{grid-column:1}
td[data-col=vol]{grid-column:2}
td[data-col=funding]{display:none}
td[data-col=feta]{display:none}
td[data-col=fspread]{grid-column:1}
td[data-col=graf]{grid-column:1/-1;align-items:center;padding-top:8px;border-top:1px solid var(--line);margin-top:4px}
td[data-col=token]::before,td[data-col=spread]::before,td[data-col=pair]::before,td[data-col=graf]::before{display:none}
td[data-col=graf] a{display:block;width:100%;text-align:center;padding:9px 8px!important;font-size:13px!important}
.split-cell{padding:0!important}
.split-cell .line{min-height:22px;padding:0;font-size:12px}
.split-cell .line+.line{border-top:1px solid var(--line);margin-top:2px;padding-top:2px}
.pair-line{gap:5px;min-height:22px;font-size:12px}
.spread-pill{font-size:13px;padding:5px 13px}
.chips{gap:6px}.chip{padding:6px 12px;font-size:12px}
.filter-panel.open{display:block}}
</style></head><body class="theme-classic"><div class="wrap">
<div class="filter-card"><div class="topbar"><div class="brand"><img src="/static/mmua-logo.png" alt="Arbitrage Insights logo"/><span>Arbitrage Insights</span></div><div class="lang-box"><span class="lbl" id="lblLang" style="margin:0">Язык</span><select id="langSel"><option value="ru">🇷🇺 Русский</option><option value="uk">🇺🇦 Українська</option><option value="en">🇬🇧 English</option></select></div></div>
<div class="auth-wrap"><div class="auth-row"><button class="btn" id="btnRegister">Регистрация</button><button class="btn" id="btnLogin">Вход</button><button class="btn" id="btnLogout">Выход</button><span class="small" id="authState">Гость: ограничение до 2% спреда</span></div><div id="authForm" class="auth-row" style="margin-top:8px"><input id="authUser" placeholder="login"/><input id="authPass" type="password" placeholder="password"/><button class="btn" id="btnAuthSubmit">Продолжить</button><button class="btn" id="btnAuthCancel">Скрыть</button></div><div id="adminBox" style="display:none;margin-top:8px"><button class="btn" id="btnLoadUsers">Загрузить пользователей</button><div id="adminUsers" class="small" style="margin-top:6px"></div></div></div>
<div class="filter-head"><div class="filter-title" id="filterTitle">Фильтр</div><div class="filter-actions"><button class="btn" id="filterToggleBtn">Показать фильтр</button><button class="btn" id="clearFiltersBtn">Очистить фильтр</button></div></div>
<div id="filterPanel" class="filter-panel"><div class="filter-grid"><div><div class="lbl" id="lblSearch">Поиск монеты</div><input id="q" placeholder="BTC"/></div><div><div class="lbl" id="lblMinVol">Оборот 24h (USD)</div><input id="minVol" type="text" placeholder="1m / 0.5m / 250k"/></div><div><div class="lbl" id="lblMinSpread">OpenSpread, %</div><input id="minSpread" type="text"/></div><div><div class="lbl" id="lblTheme">Тема</div><select id="themeSel"><option value="theme-dark-blue">Dark Blue</option><option value="theme-light">Light</option><option value="theme-classic">Classic Gray</option><option value="theme-binance">Binance Dark</option><option value="theme-tradingview">TradingView Dark</option></select></div><div><div class="lbl" id="lblSound">Оповещение</div><div style="display:flex;gap:6px"><label class="chip"><input type="checkbox" id="soundToggle"/> <span id="lblSoundCheck">звук</span></label><select id="soundSel"></select></div></div></div>
<div style="border-top:1px solid var(--line);margin:12px 0 10px"></div><div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:8px"><div class="lbl" style="margin:0" id="lblExchanges">Биржи</div><button class="btn" id="refreshBtn">↻ Refresh</button></div><div class="chips" id="exchangeBox"></div></div>
<div class="meta"><div class="badge" id="updated">Updated: —</div><div class="badge" id="dbg">DBG: —</div><div class="badge" id="cooldownBadge">Manual refresh cooldown: 0s</div></div>
<div class="table-wrap"><table><thead><tr><th>Fav</th><th id="thToken">Токен</th><th id="thPair">Покупка / Продажа</th><th class="sortable" id="thPrice" data-sort="buy_ask">Цена вход/выход<span class="arr"></span></th><th class="sortable" data-sort="buy_funding">Funding buy/sell<span class="arr"></span></th><th id="thFundingCalc">Funding calc in</th><th class="sortable" data-sort="funding_spread">F Spread (adj)<span class="arr"></span></th><th class="sortable" data-sort="spread">Open Spread<span class="arr"></span></th><th class="sortable" data-sort="buy_vol">Volume buy/sell<span class="arr"></span></th><th>Grafic</th></tr></thead><tbody id="tbody"><tr><td colspan="10">Загрузка...</td></tr></tbody></table></div>
</div>
<script>
const REFRESH_COOLDOWN_SEC=8;
let LAST_ALERT='';
let cooldown=0; let timerId=null;
let STATE={config:null,data:null,pinned:new Set(JSON.parse(localStorage.getItem('pinnedPairs')||'[]')),theme:localStorage.getItem('theme')||'theme-classic',sound:(localStorage.getItem('soundOn')||'0')==='1',lang:localStorage.getItem('lang')||'ru',soundFile:localStorage.getItem('soundFile')||'sms.wav',assets:{logos:{},sounds:[]},sortKey:'spread',sortDir:'desc',token:localStorage.getItem('authToken')||'',user:null,publicKey:'',authMode:'login'};
const I18N={
  ru:{filterTitle:'Фильтр',search:'Поиск монеты',vol:'Оборот 24h (USD)',spread:'OpenSpread, %',lang:'Язык',theme:'Тема',alert:'Оповещение',ex:'Биржи',clearFilters:'Очистить фильтр',clear:'Очистить',token:'Токен',pair:'Покупка / Продажа',price:'Цена вход/выход',register:'Регистрация',login:'Вход',logout:'Выход',guestAccess:'Гость: доступ',guestLimit:'до 2% спреда',userPrefix:'Пользователь:',adminRole:'admin (без лимита)',userRole:'пользователь (без лимита)',cancelBtn:'Скрыть',continueBtn:'Продолжить',registerBtn:'Зарегистрироваться',loginBtn:'Войти',loadUsers:'Загрузить пользователей',noAccess:'Нет доступа',enterCreds:'Введите логин и пароль',regOk:'Регистрация успешна',regErr:'Ошибка регистрации: ',loginErr:'Ошибка входа: ',showFilter:'Показать фильтр',hideFilter:'Скрыть фильтр',soundCheck:'звук',notFound:'Ничего не найдено.',loading:'Загрузка...',disableSub:'Отключить подписку',approveSub:'Подтвердить подписку'},
  uk:{filterTitle:'Фільтр',search:'Пошук монети',vol:'Обсяг 24h (USD)',spread:'OpenSpread, %',lang:'Мова',theme:'Тема',alert:'Сповіщення',ex:'Біржі',clearFilters:'Очистити фільтр',clear:'Очистити',token:'Токен',pair:'Купівля / Продаж',price:'Ціна вхід/вихід',register:'Реєстрація',login:'Вхід',logout:'Вихід',guestAccess:'Гість: доступ',guestLimit:'до 2% спреду',userPrefix:'Користувач:',adminRole:'admin (без ліміту)',userRole:'користувач (без ліміту)',cancelBtn:'Сховати',continueBtn:'Продовжити',registerBtn:'Зареєструватися',loginBtn:'Увійти',loadUsers:'Завантажити користувачів',noAccess:'Немає доступу',enterCreds:'Введіть логін і пароль',regOk:'Реєстрація успішна',regErr:'Помилка реєстрації: ',loginErr:'Помилка входу: ',showFilter:'Показати фільтр',hideFilter:'Сховати фільтр',soundCheck:'звук',notFound:'Нічого не знайдено.',loading:'Завантаження...',disableSub:'Вимкнути підписку',approveSub:'Підтвердити підписку'},
  en:{filterTitle:'Filter',search:'Search coin',vol:'24h Volume (USD)',spread:'OpenSpread, %',lang:'Language',theme:'Theme',alert:'Alert',ex:'Exchanges',clearFilters:'Clear filter',clear:'Clear',token:'Token',pair:'Buy / Sell',price:'Entry/Exit price',register:'Register',login:'Login',logout:'Logout',guestAccess:'Guest: access',guestLimit:'up to 2% spread',userPrefix:'User:',adminRole:'admin (no limit)',userRole:'user (no limit)',cancelBtn:'Hide',continueBtn:'Continue',registerBtn:'Register',loginBtn:'Sign in',loadUsers:'Load users',noAccess:'No access',enterCreds:'Enter login and password',regOk:'Registration successful',regErr:'Registration error: ',loginErr:'Login error: ',showFilter:'Show filter',hideFilter:'Hide filter',soundCheck:'sound',notFound:'Nothing found.',loading:'Loading...',disableSub:'Disable subscription',approveSub:'Approve subscription'}
};
const FALLBACK_LOGO={MEXC:'',Bybit:'',BingX:''};

const fmtPct=(x,d=2)=>Number.isFinite(x)?(x*100).toFixed(d)+'%':'N/A';
const fmtUsd=x=>!Number.isFinite(x)?'N/A':(x>=1e9?(x/1e9).toFixed(2)+'b$':x>=1e6?(x/1e6).toFixed(2)+'m$':x>=1e3?(x/1e3).toFixed(1)+'k$':Math.round(x)+'$');
const fmtPrice=x=>Number.isFinite(x)?x.toFixed(Math.abs(x)>=1?6:10).replace(/0+$/,'').replace(/\.$/,''):'N/A';
function authHeaders(base={}){if(STATE.token)base['Authorization']=`Bearer ${STATE.token}`; return base;}
const apiGet=async p=>(await fetch(p,{cache:'no-store',headers:authHeaders({})})).json();
const apiPost=async(p,b)=>(await fetch(p,{method:'POST',headers:authHeaders({'Content-Type':'application/json'}),body:JSON.stringify(b)})).json();

function b64(arr){let s=''; const bytes=new Uint8Array(arr); for(const b of bytes)s+=String.fromCharCode(b); return btoa(s);}
async function ensurePubKey(){if(STATE.publicKey)return STATE.publicKey; const j=await (await fetch('/api/auth/pubkey',{cache:'no-store'})).json(); STATE.publicKey=j.public_key||''; return STATE.publicKey;}
async function encryptWithPub(plain){
  const pem=await ensurePubKey();
  const clean=pem.replace(/-----BEGIN PUBLIC KEY-----|-----END PUBLIC KEY-----|\s/g,'');
  const der=Uint8Array.from(atob(clean),c=>c.charCodeAt(0));
  const key=await crypto.subtle.importKey('spki',der.buffer,{name:'RSA-OAEP',hash:'SHA-256'},false,['encrypt']);
  const enc=await crypto.subtle.encrypt({name:'RSA-OAEP'},key,new TextEncoder().encode(plain));
  return b64(enc);
}
function setAuthStateText(msg){document.getElementById('authState').textContent=msg;}
function openAuthForm(mode){STATE.authMode=mode; const f=document.getElementById('authForm'); f.style.display='flex'; const t=I18N[STATE.lang]||I18N.ru; document.getElementById('btnAuthSubmit').textContent=mode==='register'?t.registerBtn:t.loginBtn;}
function closeAuthForm(){document.getElementById('authForm').style.display='none';}
async function registerUser(){const u=document.getElementById('authUser').value.trim(); const p=document.getElementById('authPass').value; const t=I18N[STATE.lang]||I18N.ru; if(!u||!p){setAuthStateText(t.enterCreds); return;} let payload={username:u,password:p}; try{payload={username:u,password:p,username_enc:await encryptWithPub(u),password_enc:await encryptWithPub(p)};}catch(_e){} const r=await apiPost('/api/auth/register',payload); setAuthStateText(r.ok?t.regOk:t.regErr+(r.error||'unknown')); if(r.ok)closeAuthForm();}
async function loginUser(){const u=document.getElementById('authUser').value.trim(); const p=document.getElementById('authPass').value; const t=I18N[STATE.lang]||I18N.ru; if(!u||!p){setAuthStateText(t.enterCreds); return;} let payload={username:u,password:p}; try{payload={username:u,password:p,username_enc:await encryptWithPub(u),password_enc:await encryptWithPub(p)};}catch(_e){} const r=await apiPost('/api/auth/login',payload); if(!r.ok){setAuthStateText(t.loginErr+(r.error||'bad_login')); return;} STATE.token=r.token||''; localStorage.setItem('authToken',STATE.token); STATE.user=r.user||null; closeAuthForm(); await refreshData(); renderAuth();}
async function logoutUser(){await apiPost('/api/auth/logout',{}); STATE.token=''; STATE.user=null; localStorage.removeItem('authToken'); closeAuthForm(); await refreshData(); renderAuth();}
async function loadMe(){if(!STATE.token){STATE.user=null; return;} const r=await apiGet('/api/auth/me'); if(!r.ok){STATE.token=''; STATE.user=null; localStorage.removeItem('authToken'); return;} STATE.user=r.user;}
function renderAuth(){
  const u=STATE.user;
  const t=I18N[STATE.lang]||I18N.ru;
  const adminBox=document.getElementById('adminBox');
  const bLogin=document.getElementById('btnLogin');
  const bReg=document.getElementById('btnRegister');
  const bOut=document.getElementById('btnLogout');
  const lim=STATE.data&&STATE.data.access&&Number.isFinite(STATE.data.access.spread_limit)?`до ${(STATE.data.access.spread_limit*100).toFixed(0)}%`:'';
  if(!u){
    setAuthStateText(`${t.guestAccess} ${lim||t.guestLimit}`);
    adminBox.style.display='none';
    bLogin.style.display='inline-block';
    bReg.style.display='inline-block';
    bOut.style.display='none';
    return;
  }
  const status=u.is_admin?t.adminRole:t.userRole;
  setAuthStateText(`${t.userPrefix} ${u.username} • ${status}`);
  adminBox.style.display=u.is_admin?'block':'none';
  bLogin.style.display='none';
  bReg.style.display='none';
  bOut.style.display='inline-block';
}
async function loadUsersAdmin(){
  const t=I18N[STATE.lang]||I18N.ru;
  const r=await apiGet('/api/admin/users');
  if(!r.ok){document.getElementById('adminUsers').textContent=t.noAccess; return;}
  const box=document.getElementById('adminUsers');
  box.innerHTML='';
  r.users.forEach(x=>{const row=document.createElement('div'); row.style.margin='4px 0'; const btn=document.createElement('button'); btn.className='btn'; btn.textContent=x.subscription_approved?t.disableSub:t.approveSub; btn.onclick=async()=>{await apiPost('/api/admin/subscription',{username:x.username,approved:!x.subscription_approved}); await loadUsersAdmin();}; row.textContent=`${x.username} ${x.is_admin?'(admin)':''} ${x.subscription_approved?'✅':'⏳'} `; if(!x.is_admin)row.appendChild(btn); box.appendChild(row);});
}

function parseVolumeInput(raw){const s=(raw||'').toString().trim().toLowerCase().replace(',', '.').replace('м','m'); if(!s) return 0; const m=s.match(/^([0-9]+(?:\.[0-9]+)?)([kmb])?$/i); if(!m) return parseFloat(s)||0; const v=parseFloat(m[1]); const suf=(m[2]||'').toLowerCase(); if(suf==='k') return v*1e3; if(suf==='m') return v*1e6; if(suf==='b') return v*1e9; return v;}

function logoFor(ex){return STATE.assets.logos?.[ex]||FALLBACK_LOGO[ex]||'';}
function applyTheme(){document.body.className=STATE.theme; document.getElementById('themeSel').value=STATE.theme; localStorage.setItem('theme',STATE.theme);}
function applyLang(){const t=I18N[STATE.lang]||I18N.ru; document.getElementById('filterTitle').textContent=t.filterTitle; document.getElementById('lblSearch').textContent=t.search; document.getElementById('lblMinVol').textContent=t.vol; document.getElementById('lblMinSpread').textContent=t.spread; document.getElementById('lblLang').textContent=t.lang; document.getElementById('lblTheme').textContent=t.theme; document.getElementById('lblSound').textContent=t.alert; document.getElementById('lblExchanges').textContent=t.ex; document.getElementById('clearFiltersBtn').textContent=t.clearFilters; document.getElementById('thToken').textContent=t.token; document.getElementById('thPair').textContent=t.pair; document.getElementById('thPrice').childNodes[0].textContent=t.price; document.getElementById('btnRegister').textContent=t.register; document.getElementById('btnLogin').textContent=t.login; document.getElementById('btnLogout').textContent=t.logout; document.getElementById('btnAuthCancel').textContent=t.cancelBtn; document.getElementById('btnLoadUsers').textContent=t.loadUsers; document.getElementById('lblSoundCheck').textContent=t.soundCheck; const fp=document.getElementById('filterPanel'); const ftBtn=document.getElementById('filterToggleBtn'); ftBtn.textContent=fp.classList.contains('open')?t.hideFilter:t.showFilter; document.getElementById('langSel').value=STATE.lang; localStorage.setItem('lang',STATE.lang); renderAuth();}
function setCooldown(sec){cooldown=sec; const btn=document.getElementById('refreshBtn'); if(timerId)clearInterval(timerId); timerId=setInterval(()=>{cooldown=Math.max(0,cooldown-1); btn.disabled=cooldown>0; btn.textContent=cooldown>0?`↻ Refresh (${cooldown})`:'↻ Refresh'; document.getElementById('cooldownBadge').textContent=`Manual refresh cooldown: ${cooldown}s`; if(cooldown===0){clearInterval(timerId);timerId=null;}},1000); btn.disabled=true; btn.textContent=`↻ Refresh (${cooldown})`;}
function pairKey(r){return `${r.symbol}|${r.buy_ex}|${r.sell_ex}`;}
function isPinnedPair(r){return STATE.pinned.has(pairKey(r));}
function togglePinnedPair(r){const k=pairKey(r); if(STATE.pinned.has(k))STATE.pinned.delete(k); else STATE.pinned.add(k); localStorage.setItem('pinnedPairs',JSON.stringify([...STATE.pinned])); render();}
function refreshSortIndicators(){document.querySelectorAll('th.sortable').forEach(th=>{const key=th.getAttribute('data-sort'); th.querySelector('.arr').textContent=(key===STATE.sortKey)?(STATE.sortDir==='asc'?'▲':'▼'):'↕';});}

function renderExchangeFilters(){const box=document.getElementById('exchangeBox'); box.innerHTML=''; ['MEXC','Bybit','BingX'].forEach(ex=>{const chip=document.createElement('label'); const on=!!STATE.config.enabled?.[ex]; chip.className='chip'+(on?'':' off'); const logo=logoFor(ex); chip.innerHTML=`<input type="checkbox" ${on?'checked':''}/> ${logo?`<img src="${logo}" alt="${ex}"/>`:''} ${ex}`; chip.onclick=async (e)=>{e.preventDefault(); const en={...(STATE.config.enabled||{})}; en[ex]=!en[ex]; STATE.config=await apiPost('/api/config',{enabled:en}); renderExchangeFilters(); await refreshData();}; box.appendChild(chip);});}
function clearAllFilters(){document.getElementById('q').value=''; document.getElementById('minVol').value='0'; localStorage.setItem('minVolInput','0'); document.getElementById('minSpread').value='0%'; STATE.config.min_vol=0; STATE.config.min_spread=0; STATE.config.enabled={MEXC:true,Bybit:true,BingX:true}; apiPost('/api/config',{min_vol:0,min_spread:0,enabled:STATE.config.enabled}).then(async c=>{STATE.config=c; renderExchangeFilters(); await refreshData();});}

function applyFilters(rows){const q=(document.getElementById('q').value||'').trim().toUpperCase(); const minVol=parseVolumeInput(document.getElementById('minVol').value||'0'); const minSp=parsePctInput(document.getElementById('minSpread').value||'0'); return rows.filter(r=>{const sym=(r.symbol||'').toUpperCase(); if(q && !sym.startsWith(q)) return false; if(minVol>0){const buyOk=Number.isFinite(r.buy_vol)?r.buy_vol>=minVol:true; const sellOk=Number.isFinite(r.sell_vol)?r.sell_vol>=minVol:true; if(!(buyOk&&sellOk)) return false;} if(Number.isFinite(minSp)&&minSp>0&&!(r.spread>=minSp)) return false; return true;});}
function sortRows(rows){const key=STATE.sortKey; const dir=STATE.sortDir==='asc'?1:-1; rows.sort((a,b)=>{const pa=isPinnedPair(a)?1:0; const pb=isPinnedPair(b)?1:0; if(pa!==pb) return pb-pa; const va=Number.isFinite(a[key])?a[key]:-Infinity; const vb=Number.isFinite(b[key])?b[key]:-Infinity; if(va<vb) return -1*dir; if(va>vb) return 1*dir; return 0;});}
function fundingClass(v){if(!Number.isFinite(v)) return ''; return v<0?'fneg':'fpos';}
function spreadClass(v){if(!Number.isFinite(v)) return 'neg'; return v<0?'neg':'pos';}

async function playAlert(){ if(!STATE.sound) return; try{ if(STATE.soundFile){const a=new Audio(`/assets/sounds/${encodeURIComponent(STATE.soundFile)}`); a.volume=0.8; await a.play(); return;} }catch(_e){} try{const ac=new (window.AudioContext||window.webkitAudioContext)(); const o=ac.createOscillator(); const g=ac.createGain(); o.type='triangle'; o.frequency.value=920; g.gain.setValueAtTime(0.0001,ac.currentTime); g.gain.exponentialRampToValueAtTime(0.18,ac.currentTime+0.01); g.gain.exponentialRampToValueAtTime(0.0001,ac.currentTime+0.14); o.connect(g); g.connect(ac.destination); o.start(); o.stop(ac.currentTime+0.15);}catch(_e2){} }

function render(){
if(!STATE.data)return;
const srvLimit=(STATE.data.access&&Number.isFinite(STATE.data.access.spread_limit))?STATE.data.access.spread_limit:null;
document.getElementById('updated').textContent=`Updated: ${STATE.data.updated_at||'—'}`;
const dbgEl=document.getElementById('dbg'); const dbg=(STATE.data&&STATE.data.dbg)||{mexc:0,bybit:0,bingx:0,kept:0,took_ms:0}; if(STATE.user&&STATE.user.is_admin){dbgEl.style.display='inline-block'; dbgEl.textContent=`DBG mexc=${dbg.mexc} bybit=${dbg.bybit} bingx=${dbg.bingx} kept=${dbg.kept} took=${dbg.took_ms}ms`;} else {dbgEl.style.display='none';}
let rows=[...(STATE.data.rows||[])];
if(srvLimit!==null){rows=rows.filter(r=>Number.isFinite(r.spread)?r.spread<=srvLimit:false);}
rows=applyFilters(rows);
sortRows(rows);
refreshSortIndicators();
const tb=document.getElementById('tbody');
if(!rows.length){tb.innerHTML=`<tr class="empty-row"><td colspan="10">${(I18N[STATE.lang]||I18N.ru).notFound}</td></tr>`; return;}
const top=rows[0];
const alertKey=`${top.symbol}|${top.buy_ex}|${top.sell_ex}|${(top.spread||0).toFixed(4)}`;
if(alertKey!==LAST_ALERT){LAST_ALERT=alertKey; playAlert();}

const split=(a,b,col='',lbl='')=>`<td class='split-cell mono' data-col='${col}' data-label='${lbl}'><div class='line'>${a}</div><div class='line'>${b}</div></td>`;
const existingRows=new Map([...tb.querySelectorAll('tr[data-key]')].map(tr=>[tr.dataset.key,tr]));
[...tb.querySelectorAll('tr:not([data-key])')].forEach(tr=>tr.remove());
rows.forEach(r=>{
  const rKey=pairKey(r);
  const pin=isPinnedPair(r);
  let tr=existingRows.get(rKey);
  if(!tr){tr=document.createElement('tr'); tr.dataset.key=rKey;}
  tr.className=pin?'pinned':'';
  const lbuy=logoFor(r.buy_ex);
  const lsell=logoFor(r.sell_ex);
  tr.innerHTML=`
    <td data-col='fav'><span class='fav'>${pin?'★':'☆'}</span></td>
    <td class='token' data-col='token' data-label=''>${r.symbol.replace('USDT','')}</td>
    <td class='split-cell' data-col='pair' data-label=''>
      <div class='line pair-line long'>⬆ LONG ${lbuy?`<img class='xlogo' src='${lbuy}'/>`:''} <a href='${r.buy_url}' target='_blank'>${r.buy_ex}</a></div>
      <div class='line pair-line short'>⬇ SHORT ${lsell?`<img class='xlogo' src='${lsell}'/>`:''} <a href='${r.sell_url}' target='_blank'>${r.sell_ex}</a></div>
    </td>
    ${split(fmtPrice(r.buy_ask),fmtPrice(r.sell_bid),'price','Цена')}
    ${split(`${fmtPct(r.buy_funding,3)} → ${fmtPct(r.buy_funding_adjusted??r.buy_funding,3)} • ${r.buy_funding_interval||'8h'}`,`${fmtPct(r.sell_funding,3)} → ${fmtPct(r.sell_funding_adjusted??r.sell_funding,3)} • ${r.sell_funding_interval||'8h'}`,'funding','Funding')}
    ${split(r.funding_eta_buy||'--:--:--',r.funding_eta_sell||'--:--:--','feta','ETA')}
    <td class='mono ${fundingClass(r.funding_spread)}' data-col='fspread' data-label='F.Спред'>${fmtPct(r.funding_spread,3)}</td>
    <td data-col='spread' data-label=''><span class='spread-pill ${spreadClass(r.spread)}'>${fmtPct(r.spread,2)}</span></td>
    ${split(fmtUsd(r.buy_vol),fmtUsd(r.sell_vol),'vol','Объём')}
    <td data-col='graf' data-label=''><a class='btn' style='padding:4px 8px;font-size:12px' href='/graph?pair_key=${encodeURIComponent(pairKey(r))}' target='_blank' rel='noopener'>Grafic</a></td>
  `;
  tr.querySelector('.fav').onclick=()=>togglePinnedPair(r);
  tb.appendChild(tr);
  existingRows.delete(rKey);
});
existingRows.forEach(tr=>tr.remove());
}

async function refreshData(){
  try{ STATE.data=await apiGet('/api/data'); }
  catch(e){ console.error('refreshData failed',e); return; }
  render();
}

let EVENTS_BOUND=false;
function bindUiEvents(){
  if(EVENTS_BOUND) return;
  EVENTS_BOUND=true;
  document.getElementById('q').addEventListener('input',render);
  document.getElementById('minVol').addEventListener('change',async e=>{const raw=(e.target.value||'0').trim(); const v=Math.max(0,parseVolumeInput(raw)); localStorage.setItem('minVolInput',raw||'0'); try{STATE.config=await apiPost('/api/config',{min_vol:v});}catch(err){console.error(err);} await refreshData();});
  document.getElementById('minSpread').addEventListener('change',async e=>{const v=parsePctInput(e.target.value||'0'); e.target.value=(v*100).toFixed(2).replace(/\.00$/,'')+'%'; try{STATE.config=await apiPost('/api/config',{min_spread:v});}catch(err){console.error(err);} await refreshData();});
  document.getElementById('themeSel').addEventListener('change',e=>{STATE.theme=e.target.value; applyTheme();});
  document.getElementById('langSel').addEventListener('change',e=>{STATE.lang=e.target.value; applyLang(); render();});
  document.getElementById('soundToggle').addEventListener('change',e=>{STATE.sound=!!e.target.checked; localStorage.setItem('soundOn',STATE.sound?'1':'0'); if(STATE.sound) playAlert();});
  document.getElementById('soundSel').addEventListener('change',e=>{STATE.soundFile=e.target.value; localStorage.setItem('soundFile',STATE.soundFile);});
  document.getElementById('refreshBtn').addEventListener('click',async()=>{if(cooldown>0)return; setCooldown(REFRESH_COOLDOWN_SEC); try{await apiPost('/api/refresh',{});}catch(err){console.error(err);} await refreshData();});
  document.getElementById('clearFiltersBtn').addEventListener('click',clearAllFilters);
  document.getElementById('filterToggleBtn').addEventListener('click',()=>{const p=document.getElementById('filterPanel'); const open=p.classList.toggle('open'); const t=I18N[STATE.lang]||I18N.ru; document.getElementById('filterToggleBtn').textContent=open?t.hideFilter:t.showFilter;});
  document.getElementById('btnRegister').addEventListener('click',()=>openAuthForm('register')); document.getElementById('btnLogin').addEventListener('click',()=>openAuthForm('login')); document.getElementById('btnLogout').addEventListener('click',logoutUser); document.getElementById('btnAuthCancel').addEventListener('click',closeAuthForm); document.getElementById('btnAuthSubmit').addEventListener('click',async()=>{if(STATE.authMode==='register') await registerUser(); else await loginUser();}); document.getElementById('btnLoadUsers').addEventListener('click',loadUsersAdmin);
  document.querySelectorAll('th.sortable').forEach(th=>{th.addEventListener('click',()=>{const k=th.getAttribute('data-sort'); if(STATE.sortKey===k){STATE.sortDir=STATE.sortDir==='asc'?'desc':'asc';}else{STATE.sortKey=k;STATE.sortDir='desc';} render();});});
}

function parsePctInput(v){const t=String(v||'').replace('%','').replace(',','.').trim(); if(!t)return 0; const n=parseFloat(t); return Number.isFinite(n)?(n/100):0;}

async function boot(){
  bindUiEvents();
  try{
    STATE.config=await apiGet('/api/config');
  }catch(e){
    console.error('config load failed',e);
    STATE.config={refresh_sec:1,min_vol:0,min_spread:0,enabled:{MEXC:true,Bybit:true,BingX:true}};
  }
  try{ await loadMe(); }catch(e){ console.error('loadMe failed',e); STATE.user=null; }
  try{ STATE.data=await apiGet('/api/data'); }catch(e){ console.error('data load failed',e); STATE.data={rows:[],updated_at:'—',dbg:{mexc:0,bybit:0,bingx:0,kept:0,took_ms:0}}; }
  try{ STATE.assets=await apiGet('/api/assets'); }catch(e){ console.error('assets load failed',e); STATE.assets={logos:{},sounds:[]}; }

  document.getElementById('minVol').value=localStorage.getItem('minVolInput')||String(STATE.config.min_vol||0);
  document.getElementById('minSpread').value=String((STATE.config.min_spread||0)*100)+'%';
  document.getElementById('soundToggle').checked=STATE.sound;

  applyTheme(); applyLang(); renderAuth();
  const ss=document.getElementById('soundSel'); ss.innerHTML='';
  (STATE.assets.sounds||[]).forEach(n=>{const o=document.createElement('option'); o.value=n; o.textContent=n; ss.appendChild(o);});
  if((STATE.assets.sounds||[]).includes(STATE.soundFile)){ss.value=STATE.soundFile;} else if((STATE.assets.sounds||[]).length){STATE.soundFile=STATE.assets.sounds[0]; ss.value=STATE.soundFile; localStorage.setItem('soundFile',STATE.soundFile);} 
  renderExchangeFilters(); render();

  let _sseActive=false;
  function connectSSE(){
    if(typeof EventSource==='undefined')return;
    const src=new EventSource('/events');
    src.onopen=()=>{_sseActive=true;};
    src.onmessage=async e=>{try{const m=JSON.parse(e.data);if(m.t==='upd')await refreshData();}catch(_e){}};
    src.onerror=()=>{_sseActive=false;src.close();setTimeout(connectSSE,8000);};
  }
  connectSSE();
  setInterval(async()=>{if(!_sseActive)await refreshData();},3000);
}
boot();
</script></body></html>
"""


async def compute_once() -> Dict[str, Any]:
    started = time.time()
    async with aiohttp.ClientSession() as session:
        enabled = CFG.get("enabled", DEFAULT_EXCH_ENABLED)
        mexc_task = asyncio.create_task(load_mexc(session)) if enabled.get("MEXC", True) else None
        bybit_task = asyncio.create_task(load_bybit(session)) if enabled.get("Bybit", True) else None
        mexc = await mexc_task if mexc_task else {}
        bybit = await bybit_task if bybit_task else {}

        min_vol = float(CFG.get("min_vol", DEFAULT_MIN_VOL_USD))
        min_spread = float(CFG.get("min_spread", DEFAULT_MIN_SPREAD))

        # Phase 1: immediately push MEXC+Bybit pairs so clients see updates fast
        _push_pairs_to_live_rows(mexc, bybit, {}, min_vol, min_spread)
        _broadcast_sse(json.dumps({"t": "upd", "at": time.strftime("%H:%M:%S")}))

        candidates: Dict[str, float] = {}
        for source in (mexc, bybit):
            for symbol, row in source.items():
                vol = row.vol24_usd if math.isfinite(row.vol24_usd) else 0.0
                candidates[symbol] = max(candidates.get(symbol, 0.0), vol)

        sorted_candidates = [x[0] for x in sorted(candidates.items(), key=lambda item: item[1], reverse=True)]

        # Phase 2: BingX – update LIVE_ROWS per coin as each symbol's data arrives
        _bingx_count = 0
        async def on_bingx_symbol(norm_sym: str, bingx_row: MarketRow) -> None:
            nonlocal _bingx_count
            _push_pairs_to_live_rows(mexc, bybit, {norm_sym: bingx_row}, min_vol, min_spread, {norm_sym})
            _bingx_count += 1
            if _bingx_count % 25 == 0:
                _broadcast_sse(json.dumps({"t": "upd", "at": time.strftime("%H:%M:%S")}))

        bingx = await load_bingx(session, sorted_candidates, on_symbol=on_bingx_symbol) if enabled.get("BingX", True) else {}

    rows_out: List[dict] = []
    all_symbols = set(mexc.keys()) | set(bybit.keys()) | set(bingx.keys())

    for symbol in all_symbols:
        rows = [r for r in (mexc.get(symbol), bybit.get(symbol), bingx.get(symbol)) if r]
        if len(rows) < 2:
            continue
        pairs = best_pairs(rows, min_vol=min_vol)
        if not pairs:
            continue
        for best in pairs:
            if min_spread > 0 and best["spread"] < min_spread:
                continue
            best["symbol"] = symbol
            best["pair_key"] = f"{symbol}|{best['buy_ex']}|{best['sell_ex']}"
            rows_out.append(best)

    rows_out.sort(key=lambda row: row["spread"], reverse=True)
    now_ts = int(time.time())
    for r in rows_out:
        k = r.get("pair_key")
        if not k:
            continue
        h = PAIR_HISTORY.setdefault(k, [])
        h.append({
            "ts": now_ts,
            "spread": float(r.get("spread") or 0.0),
            "buy_price": float(r.get("buy_ask") or math.nan),
            "sell_price": float(r.get("sell_bid") or math.nan),
            "buy_ex": r.get("buy_ex"),
            "sell_ex": r.get("sell_ex"),
            "symbol": r.get("symbol"),
        })
        if len(h) > PAIR_HISTORY_MAX:
            del h[:-PAIR_HISTORY_MAX]

    # Sync LIVE_ROWS: apply final authoritative data and remove stale pairs
    final_valid_keys = {r["pair_key"] for r in rows_out}
    for k in [k for k in list(LIVE_ROWS) if k not in final_valid_keys]:
        LIVE_ROWS.pop(k, None)
    for r in rows_out:
        LIVE_ROWS[r["pair_key"]] = r

    return {
        "started_ts": started,
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
        cycle_started = time.time()
        try:
            data = await compute_once()
            async with CACHE_LOCK:
                CACHE.update(data)
            cycle_started = float(data.get("started_ts", cycle_started)) if isinstance(data, dict) else cycle_started
            _broadcast_sse(json.dumps({"t": "upd", "at": data.get("updated_at", "")}))
        except Exception:
            pass
        elapsed = max(0.0, time.time() - cycle_started)
        wait_for = max(0.05, float(CFG.get("refresh_sec", DEFAULT_REFRESH_SEC)) - elapsed)
        await asyncio.sleep(wait_for)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    _ = request
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
                val = caster(payload[key])
                if key == "refresh_sec":
                    val = max(1, val)
                CFG[key] = val
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


@app.get("/api/assets")
async def api_assets():
    logos = {ex: find_logo(ex) for ex in ("MEXC", "Bybit", "BingX")}
    return JSONResponse({"logos": logos, "sounds": list_sounds()})


@app.get("/api/data")
async def api_data(request: Request):
    user = _session_user(request)
    # Serve from LIVE_ROWS for real-time per-coin incremental updates
    rows = sorted(LIVE_ROWS.values(), key=lambda r: float(r.get("spread") or 0.0), reverse=True)
    rows, spread_limit, is_admin, is_paid = _limit_rows_for_access(rows, user)
    async with CACHE_LOCK:
        updated_at = CACHE.get("updated_at") or time.strftime("%H:%M:%S")
        dbg = dict(CACHE.get("dbg", {"mexc": 0, "bybit": 0, "bingx": 0, "kept": 0, "took_ms": 0}))
    data = {
        "updated_at": updated_at,
        "dbg": {**dbg, "kept": len(rows)},
        "rows": rows,
        "access": {
            "username": user.get("username") if user else None,
            "is_admin": is_admin,
            "subscription_approved": is_paid,
            "spread_limit": spread_limit,
        },
    }
    return JSONResponse(data)


@app.get("/api/pair")
async def api_pair(request: Request, pair_key: str):
    user = _session_user(request)
    row = LIVE_ROWS.get(pair_key)
    if not row:
        return JSONResponse({"ok": False, "error": "pair_not_found"}, status_code=404)
    filtered_rows, spread_limit, _is_admin, _is_paid = _limit_rows_for_access([row], user)
    if not filtered_rows:
        return JSONResponse({"ok": False, "error": "forbidden_by_tier", "spread_limit": spread_limit}, status_code=403)
    hist = PAIR_HISTORY.get(pair_key, [])
    return JSONResponse({"ok": True, "row": filtered_rows[0], "history": hist[-PAIR_HISTORY_MAX:]})


GRAPH_PAGE = r"""
<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<link rel="preconnect" href="https://fonts.googleapis.com"/><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/><link href="https://fonts.googleapis.com/css2?family=Inter:wght@500;600;700;800&display=swap" rel="stylesheet"/>
<title>Grafic</title><style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Inter',system-ui,Arial,sans-serif;background:#080c18;color:#c8d0e0;-webkit-font-smoothing:antialiased;background-image:radial-gradient(ellipse 70% 40% at 50% 0%,rgba(78,136,214,.08) 0%,transparent 60%)}
.wrap{max-width:1100px;margin:0 auto;padding:20px}
.card{background:#101622;border:1px solid #1e2a40;border-radius:16px;padding:14px 16px;margin-bottom:14px;box-shadow:0 4px 24px rgba(0,0,0,.3)}
.back-link{color:#5aabff;font-size:13px;font-weight:600;text-decoration:none;display:inline-flex;align-items:center;gap:5px;margin-bottom:4px}
.back-link:hover{color:#8acfff}
#title{font-size:17px;font-weight:800;color:#d8eeff;letter-spacing:-.01em}
#meta{font-size:13px;color:#6898cc;margin-top:5px;font-weight:500}
.calc-row{display:flex;flex-wrap:wrap;gap:12px;align-items:center}
.calc-row label{font-size:11px;color:#6898cc;font-weight:700;text-transform:uppercase;letter-spacing:.06em;display:flex;flex-direction:column;gap:4px}
input{background:#080c18;border:1.5px solid #1e2a40;border-radius:10px;color:#c8d0e0;padding:8px 10px;font-size:13px;font-family:inherit;outline:none;transition:border-color .14s,box-shadow .14s;width:130px}
input:focus{border-color:#4e88d6;box-shadow:0 0 0 3px rgba(78,136,214,.20)}
#pnl{font-size:15px;font-weight:700;color:#26a69a;padding:6px 12px;background:rgba(38,166,154,.10);border-radius:8px;border:1px solid rgba(38,166,154,.25)}
canvas{background:#080c18;border:1px solid #1e2a40;border-radius:14px;width:100%;height:340px;display:block}
</style></head><body><div class='wrap'>
<div class='card'><a class='back-link' href='/'>&larr; Back</a><div id='title'>Grafic</div><div id='meta'></div></div>
<div class='card'><div class='calc-row'>
<label>Entry Buy<input id='entryBuy' type='number' step='any' placeholder='0.00'></label>
<label>Entry Sell<input id='entrySell' type='number' step='any' placeholder='0.00'></label>
<label>Leverage<input id='lev' type='number' step='1' value='1' placeholder='1'></label>
<span id='pnl'></span>
</div></div>
<div class='card'><canvas id='cv' width='1050' height='340'></canvas></div>
</div>
<script>
const qp=new URLSearchParams(location.search); const pair_key=qp.get('pair_key')||'';
const cv=document.getElementById('cv'); const cx=cv.getContext('2d');
function line(points,color,minY,maxY){if(points.length<2)return;cx.strokeStyle=color;cx.beginPath();points.forEach((p,i)=>{const x=i*(cv.width/(points.length-1));const y=cv.height-((p-minY)/(maxY-minY||1))*cv.height; if(i===0)cx.moveTo(x,y); else cx.lineTo(x,y);});cx.stroke();}
function draw(h){cx.clearRect(0,0,cv.width,cv.height); const b=h.map(x=>x.buy_price).filter(Number.isFinite); const s=h.map(x=>x.sell_price).filter(Number.isFinite); const all=b.concat(s); if(!all.length) return; const minY=Math.min(...all), maxY=Math.max(...all); line(b,'#22c55e',minY,maxY); line(s,'#ef4444',minY,maxY);}
function updPnl(row){const eb=parseFloat(document.getElementById('entryBuy').value||'0');const es=parseFloat(document.getElementById('entrySell').value||'0');const lev=Math.max(1,parseFloat(document.getElementById('lev').value||'1')); if(!(eb>0&&es>0)) return; const pnl=((row.sell_bid-es)/es - (row.buy_ask-eb)/eb)*100*lev; document.getElementById('pnl').textContent=' PnL≈ '+pnl.toFixed(2)+'%';}
async function load(){const r=await fetch('/api/pair?pair_key='+encodeURIComponent(pair_key),{cache:'no-store'}); const j=await r.json(); if(!j.ok){document.getElementById('meta').textContent='Error: '+(j.error||''); return;} const row=j.row; const h=j.history||[]; document.getElementById('title').textContent=`${row.symbol} ${row.buy_ex}->${row.sell_ex}`; document.getElementById('meta').textContent=`Spread: ${(row.spread*100).toFixed(2)}% | Buy: ${row.buy_ask} | Sell: ${row.sell_bid}`; draw(h); updPnl(row); document.getElementById('entryBuy').oninput=()=>updPnl(row); document.getElementById('entrySell').oninput=()=>updPnl(row); document.getElementById('lev').oninput=()=>updPnl(row);}
setInterval(load,5000); load();
</script></body></html>
"""


@app.get("/graph", response_class=HTMLResponse)
async def graph_page(request: Request):
    _ = request
    return GRAPH_PAGE


@app.post("/api/refresh")
async def api_refresh():
    data = await compute_once()
    async with CACHE_LOCK:
        CACHE.update(data)
    _broadcast_sse(json.dumps({"t": "upd", "at": data.get("updated_at", "")}))
    return JSONResponse({"ok": True})


@app.get("/events")
async def sse_stream(request: Request):
    """Server-Sent Events endpoint.

    Each connected client holds one open TCP connection.
    The updater_loop broadcasts a lightweight 'update available' message
    to all queues; clients then fetch /api/data once.
    This decouples user count from exchange API call frequency.
    """
    q: asyncio.Queue = asyncio.Queue(maxsize=5)
    _SSE_QUEUES.append(q)

    async def generate():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=25.0)
                    yield f"data: {payload}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"  # keepalive comment
        finally:
            try:
                _SSE_QUEUES.remove(q)
            except ValueError:
                pass

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/auth/pubkey")
async def api_auth_pubkey():
    return JSONResponse({"public_key": RSA_PUBLIC_PEM})


@app.post("/api/auth/register")
async def api_auth_register(payload: Dict[str, Any]):
    username, password = _extract_auth_credentials(payload)

    if len(username) < 3 or len(password) < 6:
        return JSONResponse({"ok": False, "error": "invalid_credentials"}, status_code=400)

    async with USERS_LOCK:
        if username in USERS:
            return JSONResponse({"ok": False, "error": "user_exists"}, status_code=400)
        salt, pwh = _make_password_record(password)
        USERS[username] = {
            "username": username,
            "salt": salt,
            "password_hash": pwh,
            "is_admin": False,
            "subscription_approved": False,
            "created_at": int(time.time()),
        }
        _save_users(USERS)
    return JSONResponse({"ok": True})


@app.post("/api/auth/login")
async def api_auth_login(payload: Dict[str, Any]):
    username, password = _extract_auth_credentials(payload)

    user = USERS.get(username)
    if not user or not _verify_password(password, user.get("salt", ""), user.get("password_hash", "")):
        return JSONResponse({"ok": False, "error": "bad_login"}, status_code=401)

    token = _make_session(username)
    return JSONResponse(
        {
            "ok": True,
            "token": token,
            "user": {
                "username": user["username"],
                "is_admin": bool(user.get("is_admin")),
                "subscription_approved": bool(user.get("subscription_approved")),
            },
        }
    )


@app.get("/api/auth/me")
async def api_auth_me(request: Request):
    user = _session_user(request)
    if not user:
        return JSONResponse({"ok": False, "user": None}, status_code=401)
    return JSONResponse(
        {
            "ok": True,
            "user": {
                "username": user["username"],
                "is_admin": bool(user.get("is_admin")),
                "subscription_approved": bool(user.get("subscription_approved")),
            },
        }
    )


@app.post("/api/auth/logout")
async def api_auth_logout(request: Request):
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        SESSIONS.pop(auth[7:], None)
    return JSONResponse({"ok": True})


@app.get("/api/admin/users")
async def api_admin_users(request: Request):
    user = _session_user(request)
    if not user or not user.get("is_admin"):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    items = []
    for u in USERS.values():
        items.append({
            "username": u.get("username"),
            "is_admin": bool(u.get("is_admin")),
            "subscription_approved": bool(u.get("subscription_approved")),
            "created_at": u.get("created_at"),
        })
    items.sort(key=lambda x: (not x["is_admin"], x["username"]))
    return JSONResponse({"ok": True, "users": items})


@app.post("/api/admin/subscription")
async def api_admin_subscription(request: Request, payload: Dict[str, Any]):
    admin = _session_user(request)
    if not admin or not admin.get("is_admin"):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    username = _normalize_username(str(payload.get("username") or ""))
    approved = bool(payload.get("approved"))
    if not username or username not in USERS:
        return JSONResponse({"ok": False, "error": "user_not_found"}, status_code=404)
    if USERS[username].get("is_admin"):
        return JSONResponse({"ok": False, "error": "cant_change_admin"}, status_code=400)
    async with USERS_LOCK:
        USERS[username]["subscription_approved"] = approved
        _save_users(USERS)
    return JSONResponse({"ok": True})


def run():
    env = os.getenv("ENV", "production").strip().lower()
    if env not in {"development", "production"}:
        env = "production"

    is_dev = env == "development"
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))

    uvicorn.run(
        app,
        host=host,
        port=port,
        reload=is_dev,
        log_config=None,
        access_log=is_dev,
    )


if __name__ == "__main__":
    run()
