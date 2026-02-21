import asyncio
import base64
import json
import hashlib
import logging
import math
import os
import secrets
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("arb_dashboard")

import aiohttp
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.gzip import GZipMiddleware
import uvicorn

from models import MarketRow

try:
    import redis.asyncio as aioredis  # type: ignore[import]
    _REDIS_AVAILABLE = True
except ImportError:
    aioredis = None  # type: ignore[assignment]
    _REDIS_AVAILABLE = False

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
# Shorter timeout used for background interval-refresh fetches (best-effort, not critical path)
INTERVAL_FETCH_TIMEOUT = 5
MAX_BINGX_SYMBOLS = 260
BINGX_CONCURRENCY = 18
DEFAULT_EXCH_ENABLED = {"MEXC": True, "Bybit": True, "BingX": True}
MAX_FREE_SPREAD = 0.02
SESSION_TTL_SEC = 7 * 24 * 3600

MEXC_TICKERS = "https://contract.mexc.com/api/v1/contract/ticker"
MEXC_CONTRACT_DETAIL = "https://contract.mexc.com/api/v1/contract/detail"
MEXC_FUNDING_RATE_BTC = "https://contract.mexc.com/api/v1/contract/funding_rate/BTC_USDT"
BYBIT_TICKERS = "https://api.bybit.com/v5/market/tickers"
BYBIT_INSTRUMENTS = "https://api.bybit.com/v5/market/instruments-info"
BINGX_CONTRACTS = "https://open-api.bingx.com/openApi/swap/v2/quote/contracts"
BINGX_BOOK_TICKER = "https://open-api.bingx.com/openApi/swap/v2/quote/bookTicker"
BINGX_TICKER_24H = "https://open-api.bingx.com/openApi/swap/v2/quote/ticker"
BINGX_PREMIUM_INDEX = "https://open-api.bingx.com/openApi/swap/v2/quote/premiumIndex"
# Timestamps > this value are in milliseconds; divide by 1000 to get seconds
TIMESTAMP_MS_THRESHOLD = 1e12
# How long to reuse a cached MEXC next-funding-time (seconds)
MEXC_FUNDING_CACHE_TTL_SEC = 60
# Cache for MEXC next-funding time fetched directly (avoids per-row API calls)
_MEXC_FUND_CACHE: dict = {"ts_ms": 0, "at": 0.0}
# Per-symbol MEXC funding time cache  key = symbol e.g. "BTC_USDT"
_MEXC_SYM_FUND_CACHE: Dict[str, dict] = {}
# Per-symbol MEXC funding interval cache  key = symbol e.g. "BTC_USDT" → hours
# Populated by _mexc_intervals_refresher() background task (once per hour)
_MEXC_INTERVALS: Dict[str, int] = {}
# Unix timestamp of last full _MEXC_INTERVALS refresh (refetch when > TTL stale)
_MEXC_INTERVALS_AT: float = 0.0
MEXC_INTERVALS_TTL = 3600  # seconds; funding intervals rarely change — refresh hourly
# Per-symbol Bybit funding interval cache, key = symbol e.g. "BTCUSDT" → hours.
# Populated from /v5/market/instruments-info (fetched once per cycle alongside tickers).
_BYBIT_INTERVALS: Dict[str, int] = {}
# Per-symbol BingX funding interval cache, key = norm_sym e.g. "BTCUSDT" → hours.
# Populated by _bingx_intervals_refresher() background task (bulk prem first, per-symbol fallback).
_BINGX_INTERVALS: Dict[str, int] = {}
_BINGX_INTERVALS_AT: float = 0.0
BINGX_INTERVALS_TTL = 3600  # seconds; refresh hourly


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


def ensure_assets() -> None:
    os.makedirs(LOGOS_DIR, exist_ok=True)
    os.makedirs(SOUNDS_DIR, exist_ok=True)
    os.makedirs(STATIC_DIR, exist_ok=True)

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


def _safe_float(v: Any) -> Optional[float]:
    """Return v as float, or None (JSON null) if not finite or not a number.

    Starlette's JSONResponse uses allow_nan=False, so math.nan / inf in
    a response body causes a 500 error.  Wrap all exchange-sourced floats
    that may be nan/None with this helper before putting them in a row dict.
    """
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _pick_ts_or_delta(d: dict, keys: List[str]) -> float:
    """Like _pick_ts but also handles MEXC-style remaining-time deltas.

    MEXC bulk ticker and funding_rate endpoints return ``nextSettleTime``
    as milliseconds *remaining* until settlement (a delta), not an absolute
    Unix timestamp.  Values below 86 400 000 ms (1 day) cannot be a valid
    absolute timestamp in seconds, so they are interpreted as a delta:
        abs_ts_sec = now + delta_ms / 1000
    """
    ts = _pick_ts(d, keys)
    if math.isfinite(ts):
        return ts
    # Try delta-ms interpretation (MEXC nextSettleTime)
    one_day_ms = 86_400_000.0
    for key in keys:
        val = to_float(d.get(key))
        if math.isfinite(val) and 0 < val < one_day_ms:
            return time.time() + val / 1000.0
    return math.nan


def _norm_interval_h(raw_val) -> int:
    """Normalise a raw funding interval value to hours (int).

    Handles milliseconds (>=3_600_000), seconds (3600..86400), minutes (60..1440),
    and direct hours (1..24).  Returns 0 for invalid/zero values.
    """
    val = to_float(raw_val)
    if not (math.isfinite(val) and val > 0):
        return 0
    if val >= 3_600_000:          # ms  → seconds
        val = val / 1000.0
    # Round to nearest integer before modulo to avoid float precision issues
    # (e.g. 28800.0000001 % 60 is not exactly 0.0 in some float representations)
    ival = int(round(val))
    while ival > 24 and ival % 60 == 0:   # seconds or minutes → hours
        ival = ival // 60
    return max(1, ival)


def _pick_int(d: dict, keys: List[str], default: int = 8) -> int:
    for key in keys:
        raw = d.get(key)
        if raw is None:
            continue
        if isinstance(raw, str):
            txt = raw.strip().lower().replace("hours", "h").replace("hour", "h")
            if txt.endswith("h"):
                txt = txt[:-1]
            raw = txt
        ih = _norm_interval_h(raw)
        if ih > 0:
            return ih
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


async def _refresh_mexc_intervals(session: aiohttp.ClientSession, symbols: List[str]) -> None:
    """Fetch collectCycle per MEXC symbol from funding_rate/{sym} endpoint.

    Called by _mexc_intervals_refresher() background task — NOT on the critical
    path of load_mexc/compute_once.  Semaphore(10) + INTERVAL_FETCH_TIMEOUT
    keeps total time ~20s for 200 symbols without hammering MEXC.
    """
    global _MEXC_INTERVALS, _MEXC_INTERVALS_AT
    sem = asyncio.Semaphore(10)

    async def _one(sym: str) -> None:
        async with sem:
            try:
                async with session.get(
                    f"https://contract.mexc.com/api/v1/contract/funding_rate/{sym}",
                    timeout=aiohttp.ClientTimeout(total=INTERVAL_FETCH_TIMEOUT),
                ) as resp:
                    d = await resp.json(content_type=None)
                if isinstance(d, dict) and d.get("success"):
                    cc = (d.get("data") or {}).get("collectCycle", 0)
                    ih = _norm_interval_h(cc)
                    if ih > 0:
                        _MEXC_INTERVALS[sym] = ih
            except Exception as exc:
                logger.debug("[MEXC] funding_rate/%s failed: %s", sym, exc)

    await asyncio.gather(*[_one(s) for s in symbols])
    _MEXC_INTERVALS_AT = time.time()


async def _mexc_intervals_refresher() -> None:
    """Background task: refresh MEXC per-symbol funding intervals once per hour.

    Strategy (fast startup):
    1. Try MEXC_CONTRACT_DETAIL first (ONE bulk call, has ``fundingInterval`` seconds
       for every symbol) — populates _MEXC_INTERVALS within ~500 ms of startup.
    2. For any symbols NOT covered by detail, fall back to per-symbol funding_rate/{sym}.
    This means after one refresh cycle _MEXC_INTERVALS contains all intervals and
    the first compute_once() already shows correct per-coin funding periods.
    """
    global _MEXC_INTERVALS_AT
    await asyncio.sleep(3)  # short initial delay so app finishes booting first
    while True:
        try:
            timeout = aiohttp.ClientTimeout(total=INTERVAL_FETCH_TIMEOUT * 8)
            async with aiohttp.ClientSession(timeout=timeout) as bg_session:
                # Step 1: fast bulk init from contract/detail (fundingInterval in seconds)
                detail_found = 0
                try:
                    detail_data = await fetch_json(bg_session, MEXC_CONTRACT_DETAIL)
                    for c in (detail_data.get("data") or [] if isinstance(detail_data, dict) else []):
                        sym = str(c.get("symbol") or "")
                        if not sym:
                            continue
                        ih = _norm_interval_h(c.get("fundingInterval", 0))
                        if ih > 0:
                            _MEXC_INTERVALS[sym] = ih
                            detail_found += 1
                    if detail_found:
                        _MEXC_INTERVALS_AT = time.time()
                        logger.info("[MEXC] %d intervals from contract/detail", detail_found)
                except Exception as exc:
                    logger.warning("[MEXC] contract/detail fetch failed: %s", exc)

                # Step 2: per-symbol fallback for any symbols not found in detail
                ticker_data = await fetch_json(bg_session, MEXC_TICKERS)
                items = (ticker_data.get("data") if isinstance(ticker_data, dict) else ticker_data) or []
                if isinstance(items, list):
                    all_syms = [
                        str(it.get("symbol", ""))
                        for it in items
                        if isinstance(it, dict)
                        and "_" in str(it.get("symbol", ""))
                        and str(it.get("symbol", "")).split("_", 1)[1].upper() == "USDT"
                    ]
                    missing = [s for s in all_syms if s not in _MEXC_INTERVALS]
                    if missing:
                        await _refresh_mexc_intervals(bg_session, missing)
                        logger.info("[MEXC] per-symbol fallback filled %d missing intervals", len(missing))
                    logger.info("[MEXC] interval refresh done (%d total cached)", len(_MEXC_INTERVALS))
        except Exception as exc:
            logger.warning("[MEXC] _mexc_intervals_refresher error: %s", exc)
        await asyncio.sleep(MEXC_INTERVALS_TTL)


async def _bingx_intervals_refresher() -> None:
    """Background task: refresh BingX per-symbol funding intervals once per hour.

    Strategy:
    1. Fetch bulk BINGX_PREMIUM_INDEX (free, no extra API calls) and check for
       ``fundingInterval`` field (present in some BingX API versions, in ms).
    2. For any symbol where bulk prem has no interval, fetch single-symbol
       premiumIndex (has ``fundingInterval`` in ms reliably).
    Populates _BINGX_INTERVALS[norm_sym] = hours.
    """
    global _BINGX_INTERVALS, _BINGX_INTERVALS_AT
    await asyncio.sleep(4)  # short initial delay so app finishes booting first
    while True:
        try:
            timeout = aiohttp.ClientTimeout(total=INTERVAL_FETCH_TIMEOUT * 8)
            async with aiohttp.ClientSession(timeout=timeout) as bg_session:
                # Step 1: bulk premiumIndex (one call — some BingX API versions include fundingInterval)
                prem_data = await fetch_json(bg_session, BINGX_PREMIUM_INDEX)
                prem_items = _as_list(prem_data)
                missing_raws: list = []
                for item in prem_items:
                    raw = str(item.get("symbol") or "")
                    if not raw or "-" not in raw:
                        continue
                    base, quote = raw.split("-", 1)
                    if quote.upper() != "USDT":
                        continue
                    norm = normalize_usdt(base)
                    ih = _pick_int(item, ["fundingInterval", "fundingIntervalHours", "fundingIntervalHour", "fundingRateInterval"], default=0)
                    if ih > 0:
                        _BINGX_INTERVALS[norm] = ih
                    else:
                        missing_raws.append((norm, raw))

                # Step 2: per-symbol premiumIndex for symbols not covered by bulk
                sem = asyncio.Semaphore(10)

                async def _fetch_bingx_one(norm: str, raw: str) -> None:
                    async with sem:
                        try:
                            resp = await fetch_json(
                                bg_session, BINGX_PREMIUM_INDEX,
                                params={"symbol": raw},
                            )
                            # Single-symbol response may be wrapped in data dict or list
                            if isinstance(resp, dict):
                                item = resp.get("data") or resp
                                if isinstance(item, list) and item:
                                    item = item[0]
                            elif isinstance(resp, list) and resp:
                                item = resp[0]
                            else:
                                item = {}
                            ih = _pick_int(item, ["fundingInterval", "fundingIntervalHours", "fundingRateInterval"], default=0)
                            if ih > 0:
                                _BINGX_INTERVALS[norm] = ih
                        except Exception as exc:
                            logger.debug("[BingX] interval fetch %s failed: %s", raw, exc)

                await asyncio.gather(*[_fetch_bingx_one(n, r) for n, r in missing_raws])
                _BINGX_INTERVALS_AT = time.time()
                logger.info("[BingX] interval refresh done (%d symbols cached)", len(_BINGX_INTERVALS))
        except Exception as exc:
            logger.warning("[BingX] _bingx_intervals_refresher error: %s", exc)
        await asyncio.sleep(BINGX_INTERVALS_TTL)


async def load_mexc(session: aiohttp.ClientSession) -> Dict[str, MarketRow]:
    out: Dict[str, MarketRow] = {}
    try:
        # Fetch ticker only on the critical path.
        # Intervals come from _MEXC_INTERVALS (background task: contract/detail + per-symbol funding_rate).
        ticker_data = await fetch_json(session, MEXC_TICKERS)
        data = ticker_data
        items = data.get("data") if isinstance(data, dict) else data
        if not isinstance(items, list):
            return out

        # _MEXC_INTERVALS is populated by the _mexc_intervals_refresher() background task.
        # load_mexc() does NOT fetch intervals inline — that would block compute_once().

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
            # MEXC bulk ticker returns nextSettleTime as a delta in ms (not absolute ts)
            next_ts = _pick_ts_or_delta(it, ["nextFundingTime", "nextSettleTime", "fundingTime"])
            # Priority: _MEXC_INTERVALS (from funding_rate/{sym}, updated hourly)
            # → collectCycle in ticker row (available in some future API update)
            # → hardcoded 8h default
            interval_h = (
                _MEXC_INTERVALS.get(symbol)
                or _pick_int(
                    it,
                    ["collectCycle", "fundingInterval", "settlePeriod",
                     "fundingRateInterval", "settleInterval", "settleCycle"],
                    default=0,
                )
                or 8
            )
            out[normalize_usdt(base)] = MarketRow(
                exchange="MEXC",
                bid=to_float(it.get("bid1")),
                ask=to_float(it.get("ask1")),
                last=to_float(it.get("lastPrice")),
                vol24_usd=to_float(it.get("amount24")),
                fund_rate=fund,
                fund24_est=funding_24h_estimate(fund, interval_h),
                url=mexc_trade_url(symbol),
                next_funding_ts=next_ts,
                funding_interval_h=interval_h,
            )
    except Exception as e:
        logger.error("MEXC load error: %s: %s", type(e).__name__, e)
        return {}
    return out


async def load_bybit(session: aiohttp.ClientSession) -> Dict[str, MarketRow]:
    out: Dict[str, MarketRow] = {}
    try:
        # Fetch tickers + instruments-info in parallel.
        # instruments-info has fundingInterval (minutes) which tickers do NOT include.
        ticker_fut = fetch_json(session, BYBIT_TICKERS, params={"category": "linear"})
        inst_fut = fetch_json(session, BYBIT_INSTRUMENTS, params={"category": "linear"})
        ticker_data, inst_data = await asyncio.gather(ticker_fut, inst_fut, return_exceptions=True)

        # Build symbol → interval_h from instruments-info.
        # fundingInterval is in MINUTES (e.g. 480 = 8h, 240 = 4h, 60 = 1h).
        # _pick_int while-loop divides by 60 while val > 24 and divisible by 60:
        # 480 → 8h, 240 → 4h, 60 → 1h.
        if isinstance(inst_data, Exception):
            logger.warning("Bybit instruments-info fetch failed (intervals defaulting to 8h): %s", inst_data)
        elif isinstance(inst_data, dict):
            inst_items = inst_data.get("result", {}).get("list", [])
            if isinstance(inst_items, list):
                for d in inst_items:
                    if not isinstance(d, dict):
                        continue
                    isym = str(d.get("symbol") or "").upper()
                    ih = _pick_int(d, ["fundingInterval", "fundingIntervalHour", "fundingIntervalHours"], default=0)
                    if isym and ih > 0:
                        _BYBIT_INTERVALS[isym] = ih

        if isinstance(ticker_data, Exception):
            raise ticker_data
        items = ticker_data.get("result", {}).get("list", []) if isinstance(ticker_data, dict) else []
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
            # Use per-symbol interval from instruments-info; ticker has no interval field.
            interval_h = _BYBIT_INTERVALS.get(symbol, 0) or 8
            out[symbol] = MarketRow(
                exchange="Bybit",
                bid=to_float(it.get("bid1Price") or it.get("bidPrice")),
                ask=to_float(it.get("ask1Price") or it.get("askPrice")),
                last=to_float(it.get("lastPrice")),
                vol24_usd=to_float(it.get("turnover24h") or it.get("turnover24H") or it.get("volume24h")),
                fund_rate=fund,
                fund24_est=funding_24h_estimate(fund, interval_h),
                url=bybit_trade_url(symbol),
                next_funding_ts=next_ts,
                funding_interval_h=interval_h,
            )
    except Exception as e:
        logger.error("Bybit load error: %s: %s", type(e).__name__, e)
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
                        # Cache interval from per-symbol prem (single-symbol prem has fundingInterval in ms)
                        ih = _pick_int(prem, ["fundingInterval", "fundingIntervalHours", "fundingIntervalHour", "fundingRateInterval"], default=0)
                        if ih > 0 and norm_sym not in _BINGX_INTERVALS:
                            _BINGX_INTERVALS[norm_sym] = ih

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

                # Compute interval BEFORE MarketRow so fund24_est uses the correct value.
                # Priority: _BINGX_INTERVALS (background task, per-symbol premiumIndex in ms)
                # → prem dict (bulk premiumIndex — fundingInterval present in some API versions)
                # → contract dict (BINGX_CONTRACTS — rarely has interval field)
                # → default 8h
                bingx_interval_h = (
                    _BINGX_INTERVALS.get(norm_sym, 0)
                    or _pick_int(prem, ["fundingIntervalHours", "fundingIntervalHour", "fundingInterval", "fundingRateInterval"], default=0)
                    or _pick_int(contract, ["settleCycle", "fundingIntervalHours", "fundingInterval", "fundingTime", "fundingRateInterval"], default=0)
                    or 8
                )

                market_row = MarketRow(
                    exchange="BingX",
                    bid=bid,
                    ask=ask,
                    last=last,
                    vol24_usd=vol,
                    fund_rate=fund,
                    fund24_est=funding_24h_estimate(fund, bingx_interval_h),
                    url=bingx_trade_url(raw),
                    next_funding_ts=next_ts,
                    funding_interval_h=bingx_interval_h,
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
        logger.info(
            "[BingX] selected=%d ok=%d bulk=%d fallback=%d rejected=%d",
            dbg["selected"], len(out), dbg["from_bulk"], dbg["from_fallback"], dbg["rejected_no_quote"]
        )
        return out
    except Exception as e:
        logger.error("BingX load error: %s: %s", type(e).__name__, e)
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
                # Use _safe_float for all exchange-sourced floats that may be nan.
                # Starlette JSONResponse uses allow_nan=False → nan causes HTTP 500.
                "buy_funding": _safe_float(buy.fund_rate),
                "sell_funding": _safe_float(sell.fund_rate),
                "buy_funding_adjusted": _safe_float(adj_buy),
                "sell_funding_adjusted": _safe_float(adj_sell),
                "funding_spread": _safe_float(fund_spread),
                "funding_eta_buy": funding_eta_str(buy.next_funding_ts, fallback_hours=buy.funding_interval_h),
                "funding_eta_sell": funding_eta_str(sell.next_funding_ts, fallback_hours=sell.funding_interval_h),
                "buy_next_ts_ms": int(buy.next_funding_ts * 1000) if math.isfinite(buy.next_funding_ts) else 0,
                "sell_next_ts_ms": int(sell.next_funding_ts * 1000) if math.isfinite(sell.next_funding_ts) else 0,
                "buy_funding_interval": f"{buy.funding_interval_h}h",
                "sell_funding_interval": f"{sell.funding_interval_h}h",
                "buy_vol": _safe_float(buy.vol24_usd),
                "sell_vol": _safe_float(sell.vol24_usd),
                "buy_url": buy.url,
                "sell_url": sell.url,
            })
    return out


async def _push_pairs_to_live_rows(
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
            await _rlive_set(key, pair)


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
    await _redis_connect()
    asyncio.create_task(updater_loop())
    asyncio.create_task(_mexc_intervals_refresher())   # non-blocking MEXC interval refresh
    asyncio.create_task(_bingx_intervals_refresher())  # non-blocking BingX interval refresh
    if _REDIS is not None:
        asyncio.create_task(_redis_sse_subscriber())
    yield
    await _redis_disconnect()


ensure_assets()
app = FastAPI(lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=500)  # ~120KB → ~25KB (-80%)
app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)
_START_TIME = time.time()  # used by /health endpoint
CFG = load_config()
CACHE = {"updated_at": None, "rows": [], "dbg": {"mexc": 0, "bybit": 0, "bingx": 0, "kept": 0, "took_ms": 0}}
CACHE_LOCK = asyncio.Lock()
PAIR_HISTORY: Dict[str, List[Dict[str, Any]]] = {}
PAIR_HISTORY_MAX = 300
LIVE_ROWS: Dict[str, dict] = {}
_SSE_QUEUES: List[asyncio.Queue] = []

# Cache-busting version tag based on startup time.
# Every server restart (= every deploy) produces a new tag, so browsers
# re-download CSS/JS.  Between restarts they serve from cache for free.
_STATIC_VER = hex(int(_START_TIME))[2:]
# ---------------------------------------------------------------------------
# Optional Redis layer
# Set REDIS_URL env var (e.g. redis://localhost:6379/0) to enable Redis.
# When Redis is available:
#   - LIVE_ROWS are stored in Redis Hash "arb:live"  → survives restarts,
#     shared across multiple uvicorn workers.
#   - PAIR_HISTORY entries stored in Redis Lists "arb:hist:{pair_key}".
#   - CACHE metadata stored as "arb:cache_meta".
#   - SSE broadcast published on "arb:sse" pub/sub channel so all workers
#     can push to their own connected clients.
# When Redis is NOT available: falls back to in-process dicts (current
# behaviour, unchanged).
# ---------------------------------------------------------------------------

_REDIS_KEY_LIVE = "arb:live"
_REDIS_KEY_CACHE_META = "arb:cache_meta"
_REDIS_CHANNEL_SSE = "arb:sse"
_REDIS: Optional[Any] = None  # redis.asyncio.Redis instance, or None


async def _redis_connect() -> None:
    global _REDIS
    url = os.environ.get("REDIS_URL", "").strip()
    if not url or not _REDIS_AVAILABLE:
        return
    try:
        client = aioredis.from_url(url, encoding="utf-8", decode_responses=True)
        await client.ping()
        _REDIS = client
        logger.info("[Redis] Connected: %s", url)
    except Exception as exc:
        logger.warning("[Redis] Cannot connect to %r: %s — using in-memory fallback", url, exc)
        _REDIS = None


async def _redis_disconnect() -> None:
    global _REDIS
    if _REDIS is not None:
        try:
            await _REDIS.aclose()
        except Exception:
            pass
        _REDIS = None


# LIVE_ROWS helpers -----------------------------------------------------------

async def _rlive_set(pair_key: str, row: dict) -> None:
    """Write one row to Redis hash (or in-memory dict)."""
    LIVE_ROWS[pair_key] = row
    if _REDIS is not None:
        try:
            await _REDIS.hset(_REDIS_KEY_LIVE, pair_key, json.dumps(row))
        except Exception:
            pass


async def _rlive_del(pair_key: str) -> None:
    """Delete one row from Redis hash and in-memory dict."""
    LIVE_ROWS.pop(pair_key, None)
    if _REDIS is not None:
        try:
            await _REDIS.hdel(_REDIS_KEY_LIVE, pair_key)
        except Exception:
            pass


async def _rlive_all() -> Dict[str, dict]:
    """Return all live rows from Redis (or in-memory fallback)."""
    if _REDIS is not None:
        try:
            raw = await _REDIS.hgetall(_REDIS_KEY_LIVE)
            if raw:
                return {k: json.loads(v) for k, v in raw.items()}
        except Exception:
            pass
    return dict(LIVE_ROWS)


# PAIR_HISTORY helpers --------------------------------------------------------

async def _rhist_append(pair_key: str, entry: dict) -> None:
    """Append a history entry; also update in-memory PAIR_HISTORY."""
    h = PAIR_HISTORY.setdefault(pair_key, [])
    h.append(entry)
    if len(h) > PAIR_HISTORY_MAX:
        del h[:-PAIR_HISTORY_MAX]
    if _REDIS is not None:
        rkey = f"arb:hist:{pair_key}"
        try:
            pipe = _REDIS.pipeline()
            pipe.rpush(rkey, json.dumps(entry))
            pipe.ltrim(rkey, -PAIR_HISTORY_MAX, -1)
            await pipe.execute()
        except Exception:
            pass


async def _rhist_get(pair_key: str) -> List[dict]:
    """Fetch history from Redis if available, else in-memory."""
    if _REDIS is not None:
        rkey = f"arb:hist:{pair_key}"
        try:
            raw = await _REDIS.lrange(rkey, 0, -1)
            if raw:
                return [json.loads(x) for x in raw]
        except Exception:
            pass
    return list(PAIR_HISTORY.get(pair_key, []))


# CACHE metadata helpers -------------------------------------------------------

async def _rcache_set(meta: dict) -> None:
    if _REDIS is not None:
        try:
            await _REDIS.set(_REDIS_KEY_CACHE_META, json.dumps(meta))
        except Exception:
            pass


async def _rcache_get() -> dict:
    if _REDIS is not None:
        try:
            raw = await _REDIS.get(_REDIS_KEY_CACHE_META)
            if raw:
                return json.loads(raw)
        except Exception:
            pass
    return {}


# SSE broadcast (Redis pub/sub for multi-worker) ------------------------------

async def _redis_sse_subscriber() -> None:
    """Subscribe to the Redis pub/sub SSE channel and forward messages
    to all in-process SSE clients.  Runs as a background task when Redis
    is available.  If the connection drops it retries after 5 seconds."""
    if _REDIS is None:
        return
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        return
    while True:
        try:
            sub_client = aioredis.from_url(url, encoding="utf-8", decode_responses=True)
            pubsub = sub_client.pubsub()
            await pubsub.subscribe(_REDIS_CHANNEL_SSE)
            async for message in pubsub.listen():
                if message and message.get("type") == "message":
                    data = message.get("data", "")
                    # Forward to in-process queues (clients on THIS worker)
                    for q in list(_SSE_QUEUES):
                        try:
                            q.put_nowait(data)
                        except asyncio.QueueFull:
                            pass
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning("[Redis] SSE subscriber error: %s — retrying in 5s", exc)
            await asyncio.sleep(5)


def _broadcast_sse(payload: str) -> None:
    """Push a message to every connected SSE client (fire-and-forget).

    When Redis is configured the message is also published to the
    ``arb:sse`` pub/sub channel so workers that have no local SSE
    subscriber still deliver the update to their clients via
    ``_redis_sse_subscriber``.
    """
    for q in list(_SSE_QUEUES):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            pass  # slow client – skip this tick
    if _REDIS is not None:
        # Schedule publish on the running event loop without blocking the caller.
        try:
            asyncio.get_running_loop().create_task(
                _REDIS.publish(_REDIS_CHANNEL_SSE, payload)
            )
        except RuntimeError:
            pass  # no running loop (shouldn't happen in normal async context)



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
        await _push_pairs_to_live_rows(mexc, bybit, {}, min_vol, min_spread)
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
            await _push_pairs_to_live_rows(mexc, bybit, {norm_sym: bingx_row}, min_vol, min_spread, {norm_sym})
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
        entry = {
            "ts": now_ts,
            "spread": float(r.get("spread") or 0.0),
            "buy_price": float(r.get("buy_ask") or math.nan),
            "sell_price": float(r.get("sell_bid") or math.nan),
            "buy_ex": r.get("buy_ex"),
            "sell_ex": r.get("sell_ex"),
            "symbol": r.get("symbol"),
        }
        await _rhist_append(k, entry)

    # Sync LIVE_ROWS: apply final authoritative data and remove stale pairs
    final_valid_keys = {r["pair_key"] for r in rows_out}
    stale_keys = [k for k in list(LIVE_ROWS) if k not in final_valid_keys]
    for k in stale_keys:
        await _rlive_del(k)
    for r in rows_out:
        await _rlive_set(r["pair_key"], r)

    # Persist cache metadata to Redis
    cache_meta = {
        "updated_at": time.strftime("%H:%M:%S"),
        "dbg": {
            "mexc": len(mexc),
            "bybit": len(bybit),
            "bingx": len(bingx),
            "kept": len(rows_out),
            "took_ms": int((time.time() - started) * 1000),
        },
    }
    await _rcache_set(cache_meta)

    return {
        "started_ts": started,
        "updated_at": cache_meta["updated_at"],
        "rows": rows_out,
        "dbg": cache_meta["dbg"],
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
            logger.exception("updater_loop: compute_once raised an error")
        elapsed = max(0.0, time.time() - cycle_started)
        wait_for = max(0.05, float(CFG.get("refresh_sec", DEFAULT_REFRESH_SEC)) - elapsed)
        await asyncio.sleep(wait_for)


def _spread_sort_key(r: dict) -> float:
    """Safe sort key for rows — converts 'spread' to float, returns 0.0 on error."""
    try:
        return float(r.get("spread") or 0.0)
    except (TypeError, ValueError):
        return 0.0


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Serve the dashboard with server-injected initial snapshot.

    Embedding the current LIVE_ROWS snapshot directly into the HTML lets the
    browser render the full table on first paint — no extra /api/data round-trip.
    Auth token lives in localStorage (not a cookie), so this page request is
    always guest-level; the JS calls /api/me + /api/data in parallel on load
    to upgrade to the authenticated view within one SSE cycle.
    """
    live = await _rlive_all()
    rows = sorted(live.values(), key=_spread_sort_key, reverse=True)
    # Page request never carries Bearer token (token is in localStorage, not cookies)
    rows, spread_limit, _is_admin, _is_paid = _limit_rows_for_access(rows, None)
    async with CACHE_LOCK:
        meta = await _rcache_get()
        updated_at = meta.get("updated_at") or CACHE.get("updated_at") or ""
        dbg = meta.get("dbg") or dict(CACHE.get("dbg", {"mexc": 0, "bybit": 0, "bingx": 0, "kept": 0, "took_ms": 0}))
    initial_data = json.dumps({
        "updated_at": updated_at,
        "dbg": {**dbg, "kept": len(rows)},
        "rows": rows,
        "access": {"username": None, "is_admin": False, "subscription_approved": False, "spread_limit": spread_limit},
    }, ensure_ascii=False)
    initial_config = json.dumps(CFG, ensure_ascii=False)
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "initial_data": initial_data,
        "initial_config": initial_config,
        "sv": _STATIC_VER,
    })


@app.get("/health")
async def health():
    """Health-check endpoint for nginx/systemd/uptime monitors.

    Returns HTTP 200 as long as the process is alive.  Nginx and systemd
    can poll this to detect hangs and auto-restart the service proactively.
    """
    live = await _rlive_all()
    return JSONResponse({
        "ok": True,
        "uptime_s": int(time.time() - _START_TIME),
        "rows_cached": len(live),
    })


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


@app.get("/api/funding-next")
async def api_funding_next(exchange: str = "", symbol: str = ""):
    """Return the nearest next-funding timestamp (ms UTC) for the given exchange.
    When `symbol` is provided for MEXC (e.g. BTC_USDT), fetches per-symbol
    nextSettleTime from MEXC contract/funding_rate endpoint (cached per symbol).
    Falls back to exchange-level minimum across live rows, then BTC_USDT fallback.
    """
    live = await _rlive_all()
    ex = exchange.strip().lower()
    nearest_funding_ms: int = 0
    now_ms = int(time.time() * 1000)

    # Per-symbol MEXC lookup — most precise (each contract has its own cycle)
    sym_upper = symbol.strip().upper()
    if ex == "mexc" and sym_upper:
        cached = _MEXC_SYM_FUND_CACHE.get(sym_upper, {})
        if (time.time() - cached.get("at", 0.0)) < MEXC_FUNDING_CACHE_TTL_SEC and cached.get("ts_ms", 0) > now_ms:
            return JSONResponse({"nextFundingTime": cached["ts_ms"], "exchange": exchange, "symbol": symbol})
        try:
            async with aiohttp.ClientSession() as _s:
                raw = await fetch_json(_s, f"https://contract.mexc.com/api/v1/contract/funding_rate/{sym_upper}")
            d = raw.get("data") if isinstance(raw, dict) else None
            if isinstance(d, dict):
                ts_raw = d.get("nextSettleTime") or d.get("nextFundingTime")
                ts_ms = _mexc_ts_raw_to_ms(ts_raw, now_ms)
                if ts_ms > now_ms:
                    _MEXC_SYM_FUND_CACHE[sym_upper] = {"ts_ms": ts_ms, "at": time.time()}
                    logger.info("[MEXC] %s: next %s", sym_upper, datetime.fromtimestamp(ts_ms/1000, tz=timezone.utc).isoformat())
                    return JSONResponse({"nextFundingTime": ts_ms, "exchange": exchange, "symbol": symbol})
        except Exception as _e:
            logger.warning("[MEXC] per-symbol funding-next error for %s: %s", sym_upper, _e)
        # Fall through to exchange-level lookup below

    for row in live.values():
        if row.get("buy_ex", "").lower() == ex:
            ts = int(row.get("buy_next_ts_ms") or 0)
            if ts > now_ms and (nearest_funding_ms == 0 or ts < nearest_funding_ms):
                nearest_funding_ms = ts
        if row.get("sell_ex", "").lower() == ex:
            ts = int(row.get("sell_next_ts_ms") or 0)
            if ts > now_ms and (nearest_funding_ms == 0 or ts < nearest_funding_ms):
                nearest_funding_ms = ts

    # MEXC bulk ticker often omits nextSettleTime — fall back to BTC_USDT direct call
    if ex == "mexc" and nearest_funding_ms == 0:
        cached_ts = _MEXC_FUND_CACHE["ts_ms"]
        cached_at = _MEXC_FUND_CACHE["at"]
        if cached_ts > now_ms and (time.time() - cached_at) < MEXC_FUNDING_CACHE_TTL_SEC:
            nearest_funding_ms = cached_ts
        else:
            try:
                async with aiohttp.ClientSession() as _s:
                    raw = await fetch_json(_s, MEXC_FUNDING_RATE_BTC)
                d = raw.get("data") if isinstance(raw, dict) else None
                if isinstance(d, dict):
                    ts_raw = d.get("nextSettleTime") or d.get("nextFundingTime")
                    ts_ms = _mexc_ts_raw_to_ms(ts_raw, now_ms)
                    if ts_ms > now_ms:
                        _MEXC_FUND_CACHE["ts_ms"] = ts_ms
                        _MEXC_FUND_CACHE["at"] = time.time()
                        nearest_funding_ms = ts_ms
                        logger.info("[MEXC] next funding: %s", datetime.fromtimestamp(ts_ms/1000, tz=timezone.utc).isoformat())
            except Exception as _e:
                logger.warning("[MEXC] funding-next fallback error: %s", _e)

    return JSONResponse({"nextFundingTime": nearest_funding_ms, "exchange": exchange})


def _mexc_ts_raw_to_ms(ts_raw: Any, now_ms: int) -> int:
    """Convert MEXC nextSettleTime/nextFundingTime to an absolute UTC millisecond timestamp.

    MEXC may return either:
    - An absolute timestamp in milliseconds (> 1e12)
    - An absolute timestamp in seconds (> 1e9)
    - A **remaining-time delta in milliseconds** (< 86 400 000 ms = 1 day) ← common case
    Returns 0 if value is invalid or in the past.
    """
    ts_val = to_float(ts_raw)
    if not math.isfinite(ts_val) or ts_val <= 0:
        return 0
    if ts_val > TIMESTAMP_MS_THRESHOLD:
        ts_ms = int(ts_val)            # already ms timestamp
    elif ts_val > 1e9:
        ts_ms = int(ts_val * 1000)     # seconds timestamp → ms
    else:
        ts_ms = now_ms + int(ts_val)   # delta in ms → absolute timestamp
    return ts_ms if ts_ms > now_ms else 0


@app.get("/api/data")
async def api_data(request: Request):
    user = _session_user(request)
    # Serve from LIVE_ROWS (Redis-backed when available) for real-time per-coin updates
    live = await _rlive_all()
    rows = sorted(live.values(), key=_spread_sort_key, reverse=True)
    rows, spread_limit, is_admin, is_paid = _limit_rows_for_access(rows, user)
    # Prefer Redis metadata; fall back to in-memory CACHE
    async with CACHE_LOCK:
        meta = await _rcache_get()
        updated_at = meta.get("updated_at") or CACHE.get("updated_at") or time.strftime("%H:%M:%S")
        dbg = meta.get("dbg") or dict(CACHE.get("dbg", {"mexc": 0, "bybit": 0, "bingx": 0, "kept": 0, "took_ms": 0}))
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
    live = await _rlive_all()
    row = live.get(pair_key)
    if not row:
        return JSONResponse({"ok": False, "error": "pair_not_found"}, status_code=404)
    filtered_rows, spread_limit, _is_admin, _is_paid = _limit_rows_for_access([row], user)
    if not filtered_rows:
        return JSONResponse({"ok": False, "error": "forbidden_by_tier", "spread_limit": spread_limit}, status_code=403)
    hist = await _rhist_get(pair_key)
    return JSONResponse({"ok": True, "row": filtered_rows[0], "history": hist[-PAIR_HISTORY_MAX:]})




@app.get("/graph", response_class=HTMLResponse)
async def graph_page(request: Request):
    return templates.TemplateResponse("graph.html", {"request": request, "sv": _STATIC_VER})


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
    # RSA decrypt + PBKDF2 hash are CPU-bound (≥200ms). Run in thread pool
    # so the async event loop is never blocked — table updates keep flowing.
    username, password = await asyncio.to_thread(_extract_auth_credentials, payload)

    if len(username) < 3 or len(password) < 6:
        return JSONResponse({"ok": False, "error": "invalid_credentials"}, status_code=400)

    async with USERS_LOCK:
        if username in USERS:
            return JSONResponse({"ok": False, "error": "user_exists"}, status_code=400)
        salt, pwh = await asyncio.to_thread(_make_password_record, password)
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
    # RSA decrypt + PBKDF2 verify are CPU-bound (≥200ms). Run in thread pool.
    username, password = await asyncio.to_thread(_extract_auth_credentials, payload)

    user = USERS.get(username)
    if not user or not await asyncio.to_thread(
        _verify_password, password, user.get("salt", ""), user.get("password_hash", "")
    ):
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
