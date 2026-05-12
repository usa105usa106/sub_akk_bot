import os
import re
import json
import time
import asyncio
import inspect
import subprocess
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# ---- structural menu compatibility wrapper ----
def structural_menu(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    # Keep old callers working; real menu is defined later as structural_layers_menu.
    fn = globals().get("structural_layers_menu")
    if callable(fn):
        return fn(settings)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("OFF", callback_data="structural:off")],
        [InlineKeyboardButton("Trendline Layer", callback_data="structural:trendline")],
        [InlineKeyboardButton("Trendline + Relative Strength vs BTC", callback_data="structural:trendline_rs")],
        [InlineKeyboardButton("Trendline + RS/BTC + Super Volume", callback_data="structural:trendline_rs_volume")],
        [InlineKeyboardButton("Structural Only", callback_data="structural:structural_only")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back:main")],
    ])


import ccxt
import pandas as pd
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters

BOT_VERSION = os.getenv("BOT_VERSION", "0065")
OLLAMA_KEEP_ALIVE_DEFAULT = os.getenv("OLLAMA_KEEP_ALIVE", "10m")
AI_APPROVAL_TOP_LIMIT = int(os.getenv("AI_APPROVAL_TOP_LIMIT", "5"))
AI_SEMAPHORE = asyncio.Semaphore(int(os.getenv("AI_MAX_CONCURRENT", "1")))
AI_CHAT_OPTIONS = {"temperature": 0.2, "num_predict": int(os.getenv("AI_CHAT_NUM_PREDICT", "128"))}
AI_APPROVAL_OPTIONS = {"temperature": 0.1, "num_predict": int(os.getenv("AI_APPROVAL_NUM_PREDICT", "120"))}
OLLAMA_IDLE_UNLOAD_SECONDS = int(os.getenv("OLLAMA_IDLE_UNLOAD_SECONDS", "600"))
LAST_OLLAMA_ACTIVITY = 0.0
START_TIME = time.time()
LOCAL_TIMEZONE = os.getenv("TZ", "Europe/Stockholm")
os.environ.setdefault("TZ", LOCAL_TIMEZONE)
if hasattr(time, "tzset"):
    time.tzset()
SCAN_MAX_CONCURRENT = int(os.getenv("SCAN_MAX_CONCURRENT", "5"))
SCAN_RETRY_ATTEMPTS = int(os.getenv("SCAN_RETRY_ATTEMPTS", "3"))
SCAN_RETRY_BASE_DELAY = float(os.getenv("SCAN_RETRY_BASE_DELAY", "0.8"))
SCAN_REQUEST_PAUSE = float(os.getenv("SCAN_REQUEST_PAUSE", "0.5"))
MARKETS_CACHE_TTL = int(os.getenv("MARKETS_CACHE_TTL", "3600"))
_MARKETS_CACHE: Dict[str, Dict[str, Any]] = {}
_MARKETS_CACHE_LOCK = threading.RLock()


DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DATA_DIR.mkdir(exist_ok=True)
SETTINGS_FILE = DATA_DIR / "settings.json"
API_KEYS_FILE = DATA_DIR / "api_keys.json"
OPENAI_KEYS_FILE = DATA_DIR / "openai_keys.json"
POSITIONS_FILE = DATA_DIR / "positions.json"
COOLDOWN_FILE = DATA_DIR / "cooldown.json"
TRADE_EVENTS_FILE = DATA_DIR / "trade_events.json"
WORK_MESSAGE_IDS_FILE = DATA_DIR / "work_message_ids.json"
SETTINGS_LOCK = threading.RLock()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")

TRADING_CHAT_SYSTEM_PROMPT = """
Ты профессиональный crypto trading assistant.

Правила:
- Любые BTC, ETH, SOL, XRP, BNB, DOGE, PEPE, TAO, SUI, ADA, AVAX и другие тикеры трактуй как криптовалюты/торговые инструменты.
- Слова long, лонг, buy, покупать трактуй как LONG-позицию.
- Слова short, шорт, sell, продавать трактуй как SHORT-позицию.
- Никогда не трактуй тикеры и слова LONG/SHORT как имена людей, компании, новости или биографии.
- Отвечай только в контексте трейдинга, риска, направления, входа, стопа, тейков и таймфрейма.
- Если пользователь спрашивает "ETH лонг или шорт сейчас", дай краткий трейдинг-ответ: направление/WAIT, условия входа, SL/TP и риск.
- Не обещай гарантированную прибыль.
- Пиши по-русски, кратко и по делу.
"""

def normalize_trading_chat_query(text: str) -> str:
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    replacements = {
        "эфир": "ETH",
        "биток": "BTC",
        "биткоин": "BTC",
        "лонг": "LONG",
        "шорт": "SHORT",
        "длинная": "LONG",
        "короткая": "SHORT",
    }
    low = t.lower()
    for src, dst in replacements.items():
        low = re.sub(rf"\b{re.escape(src)}\b", dst, low, flags=re.I)
    # Normalize latin tickers and trading words without damaging Russian text.
    low = re.sub(r"\b([a-zA-Z]{2,12})(usdt)?\b", lambda m: m.group(0).upper(), low)
    low = re.sub(r"\b(LONG|SHORT|BUY|SELL|SL|TP|TF)\b", lambda m: m.group(0).upper(), low, flags=re.I)
    return low.strip()

def build_trading_chat_prompt(text: str) -> str:
    q = normalize_trading_chat_query(text)
    return (
        "Запрос пользователя в трейдинг-чате:\n"
        f"{q}\n\n"
        "Ответь как crypto trading assistant. Если данных рынка в сообщении недостаточно, "
        "не выдумывай точную цену: дай сценарий LONG/SHORT/WAIT, условия подтверждения, риск, SL/TP и таймфрейм."
    )
OLLAMA_MODELS = [x.strip() for x in os.getenv("OLLAMA_MODELS", "llama3.1:8b,deepseek-r1:8b").split(",") if x.strip()]
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "llama3.1:8b")
DEFAULT_EXCHANGE = os.getenv("DEFAULT_EXCHANGE", "mexc").lower()
SUPPORTED_EXCHANGES = {"mexc", "bingx", "binance"}

LAST_SCAN_RESULTS: Dict[int, List[Dict[str, Any]]] = {}
LAST_AI_CONFIRMED: Dict[int, List[Dict[str, Any]]] = {}
CURRENT_AI_UID: Optional[str] = None
USER_SCAN_TASKS: Dict[str, asyncio.Task] = {}

DEFAULT_SETTINGS: Dict[str, Any] = {
    "mode": "signal",
    "ai_provider": os.getenv("DEFAULT_AI_PROVIDER", "openai").lower(),
    "ollama_model": DEFAULT_MODEL,
    "openai_model": os.getenv("DEFAULT_OPENAI_MODEL", "gpt-5.4-mini"),
    "reasoning_level": os.getenv("DEFAULT_REASONING_LEVEL", "medium"),
    "exchange": DEFAULT_EXCHANGE,
    "trading_mode": "manual",
    "trading_enabled": False,
    "ai_auto": False,
    "ai_auto_p": os.getenv("DEFAULT_AI_AUTO_P", "on").lower() == "on",
    "risk_percent": float(os.getenv("DEFAULT_RISK_PERCENT", "1")),
    "max_trades": int(os.getenv("DEFAULT_MAX_TRADES", "3")),
    "max_total_risk": float(os.getenv("DEFAULT_MAX_TOTAL_RISK", "3")),
    "leverage": int(os.getenv("DEFAULT_LEVERAGE", "5")),
    "min_score": float(os.getenv("DEFAULT_MIN_SCORE", "80")),
    "top_limit": os.getenv("DEFAULT_TOP_LIMIT", "5"),
    "scanner_size": int(os.getenv("DEFAULT_SCANNER_SIZE", "100")),
    "market_universe": os.getenv("DEFAULT_MARKET_UNIVERSE", "all"),
    "timeframe_mode": os.getenv("DEFAULT_TIMEFRAME_MODE", "15m"),
    "session_filter": os.getenv("DEFAULT_SESSION_FILTER", "off").lower() == "on",
    "duplicate_protection_enabled": os.getenv("DEFAULT_DUPLICATE_PROTECTION_ENABLED", "on").lower() == "on",
    "real_execution_enabled": os.getenv("DEFAULT_REAL_EXECUTION_ENABLED", "off").lower() == "on",
    "margin_mode": "isolated",
    "breakeven_enabled": os.getenv("DEFAULT_BREAKEVEN_ENABLED", "on").lower() == "on",
    "breakeven_r": float(os.getenv("DEFAULT_BREAKEVEN_R", "1")),
    "trailing_enabled": os.getenv("DEFAULT_TRAILING_ENABLED", "on").lower() == "on",
    "trailing_r": float(os.getenv("DEFAULT_TRAILING_R", "1.5")),
    "partial_tp_enabled": os.getenv("DEFAULT_PARTIAL_TP_ENABLED", "on").lower() == "on",
    "partial_tp_r": float(os.getenv("DEFAULT_PARTIAL_TP_R", "1")),
    "partial_tp_percent": float(os.getenv("DEFAULT_PARTIAL_TP_PERCENT", "50")),
    "cooldown_enabled": os.getenv("DEFAULT_COOLDOWN_ENABLED", "on").lower() == "on",
    "cooldown_losses": int(os.getenv("DEFAULT_COOLDOWN_LOSSES", "3")),
    "cooldown_minutes": int(os.getenv("DEFAULT_COOLDOWN_MINUTES", "120")),
    "auto_scanner_interval": os.getenv("DEFAULT_AUTO_SCANNER_INTERVAL", "off"),
    "auto_scanner_last_run": 0,
    "structural_mode": os.getenv("DEFAULT_STRUCTURAL_MODE", "off"),
    "extended_tp_enabled": os.getenv("DEFAULT_EXTENDED_TP_ENABLED", "on").lower() == "on",
    "extended_tp_min_confidence": float(os.getenv("DEFAULT_EXTENDED_TP_MIN_CONFIDENCE", "80")),
    "extended_tp_rr": float(os.getenv("DEFAULT_EXTENDED_TP_RR", "4")),
    "stop_all_enabled": os.getenv("DEFAULT_STOP_ALL_ENABLED", "off").lower() == "on",
    "position_sync_enabled": os.getenv("DEFAULT_POSITION_SYNC_ENABLED", "off").lower() == "on",
    "live_trade_manager_enabled": os.getenv("DEFAULT_LIVE_TRADE_MANAGER_ENABLED", "off").lower() == "on",
    "position_sync_interval": int(os.getenv("DEFAULT_POSITION_SYNC_INTERVAL", "300")),
    "strict_ai_mode": os.getenv("DEFAULT_STRICT_AI_MODE", "on").lower() == "on",
}

def load_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default

def save_json(path: Path, data):
    path.parent.mkdir(exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)

def user_id(update: Update) -> str:
    return str(update.effective_user.id)

def get_settings(uid: str) -> Dict[str, Any]:
    with SETTINGS_LOCK:
        data = load_json(SETTINGS_FILE, {})
        if uid not in data or not isinstance(data.get(uid), dict):
            data[uid] = dict(DEFAULT_SETTINGS)
            save_json(SETTINGS_FILE, data)
        merged = dict(DEFAULT_SETTINGS)
        merged.update(data.get(uid, {}))
        return merged

def set_setting(uid: str, key: str, value):
    with SETTINGS_LOCK:
        data = load_json(SETTINGS_FILE, {})
        current = data.get(uid, {})
        if not isinstance(current, dict):
            current = {}
        s = dict(DEFAULT_SETTINGS)
        s.update(current)
        s[key] = value
        data[uid] = s
        save_json(SETTINGS_FILE, data)

def set_settings(uid: str, updates: Dict[str, Any]):
    with SETTINGS_LOCK:
        data = load_json(SETTINGS_FILE, {})
        current = data.get(uid, {})
        if not isinstance(current, dict):
            current = {}
        s = dict(DEFAULT_SETTINGS)
        s.update(current)
        s.update(updates)
        data[uid] = s
        save_json(SETTINGS_FILE, data)

def normalize_symbol(symbol: str) -> str:
    s = symbol.upper().replace("/", "").replace(":USDT", "")
    if not s.endswith("USDT"):
        s += "USDT"
    return s

def get_active_model(settings: Dict[str, Any]) -> str:
    return settings.get("openai_model") if settings.get("ai_provider") == "openai" else settings.get("ollama_model", DEFAULT_MODEL)

def set_ai_provider(uid: str, provider: str):
    provider = str(provider).lower().strip()
    if provider not in ["openai", "ollama"]:
        provider = "openai"
    updates = {"ai_provider": provider}
    if provider == "openai":
        updates["openai_model"] = get_settings(uid).get("openai_model") or DEFAULT_SETTINGS["openai_model"]
    else:
        updates["ollama_model"] = get_settings(uid).get("ollama_model") or DEFAULT_MODEL
    set_settings(uid, updates)

def set_openai_model(uid: str, model: str):
    # Choosing an OpenAI model must also lock provider to OpenAI,
    # so menus/testai/start cannot fall back to Ollama later.
    set_settings(uid, {"ai_provider": "openai", "openai_model": model})

def set_ollama_model(uid: str, model: str):
    # Choosing an Ollama model intentionally switches provider to Ollama.
    set_settings(uid, {"ai_provider": "ollama", "ollama_model": model})

def allowed_by_market_universe(symbol: str, settings: Dict[str, Any]) -> bool:
    if settings.get("market_universe") == "btc_eth":
        return normalize_symbol(symbol) in ["BTCUSDT", "ETHUSDT"]
    return True

def timeframe_pair(settings: Dict[str, Any], override: Optional[str] = None) -> Tuple[str, Optional[str]]:
    mode = override or settings.get("timeframe_mode", "15m")
    if mode in ["15m", "15"]:
        return "15m", None
    if mode in ["15m_1h", "15m/1h", "15 мин/1час"]:
        return "15m", "1h"
    if mode in ["1h_4h", "1h/4h", "1 час/4 часа"]:
        return "1h", "4h"
    if mode == "multi":
        return "15m", "1h"
    return "15m", None

def timeframe_chain(settings: Dict[str, Any], override: Optional[str] = None) -> List[str]:
    mode = override or settings.get("timeframe_mode", "15m")
    if mode in ["15m", "15"]:
        return ["15m"]
    if mode in ["15m_1h", "15m/1h", "15 мин/1час"]:
        return ["15m", "1h"]
    if mode in ["1h_4h", "1h/4h", "1 час/4 часа"]:
        return ["1h", "4h"]
    if mode == "multi":
        return ["15m", "1h", "4h", "1d"]
    return ["15m"]

def timeframe_label(mode: str) -> str:
    return {
        "15m": "15 мин",
        "15m_1h": "15 мин / 1 час",
        "1h_4h": "1 час / 4 часа",
        "multi": "мульти 15m+1h+4h+1d",
    }.get(mode, mode)

def auto_scanner_label(value: str) -> str:
    return {"15m": "15 мин", "60m": "60 мин", "4h": "4 часа", "12h": "12 часов", "24h": "24 часа", "off": "Выкл"}.get(value, "Выкл")

def auto_scanner_seconds(value: str) -> int:
    return {"15m": 900, "60m": 3600, "4h": 14400, "12h": 43200, "24h": 86400}.get(value, 0)

def asia_america_session_status() -> Dict[str, Any]:
    """Return Asia/America opening-volatility status using Moscow time.

    This is NOT a hard session blocker. It marks the high-volatility opening
    impulse windows requested by the user:
    - Asia open:    03:00-07:00 MSK
    - America open: 16:30-20:30 MSK
    """
    now_msk = datetime.now(ZoneInfo("Europe/Moscow"))
    minute = now_msk.hour * 60 + now_msk.minute
    asia_start = 3 * 60
    asia_end = 7 * 60
    america_start = 16 * 60 + 30
    america_end = 20 * 60 + 30

    if asia_start <= minute < asia_end:
        return {"active": True, "session": "Asia open volatility", "msk": now_msk.strftime("%H:%M MSK"), "window": "03:00-07:00 MSK"}
    if america_start <= minute < america_end:
        return {"active": True, "session": "America open volatility", "msk": now_msk.strftime("%H:%M MSK"), "window": "16:30-20:30 MSK"}
    return {"active": False, "session": "Outside opening volatility", "msk": now_msk.strftime("%H:%M MSK"), "window": "Asia 03:00-07:00 MSK / America 16:30-20:30 MSK"}

def session_filter_allows_trading(settings: Dict[str, Any]) -> Tuple[bool, str]:
    # Kept for old call sites, but Asia/America is no longer a hard blocker.
    if not settings.get("session_filter"):
        return True, "Asia/America volatility filter OFF"
    st = asia_america_session_status()
    if st.get("active"):
        return True, f"{st['session']} active ({st['msk']}, window {st['window']})"
    return True, f"Asia/America volatility filter ON: now {st['msk']}, outside opening impulse windows ({st['window']})"

def apply_session_volatility_filter(settings: Dict[str, Any], market: Dict[str, Any]) -> Dict[str, Any]:
    """Soft Asia/America filter: boosts setups during opening-volatility windows.

    ON does not block trades. During Asia/America opening impulse it adds a
    small score bonus and reason, so the scan/AI can prioritize these setups.
    """
    if not settings.get("session_filter"):
        return market
    st = asia_america_session_status()
    reasons = list(market.get("reasons", []) or [])
    market["session_filter"] = st
    if st.get("active"):
        market["score"] = round(min(99, safe_float(market.get("score"), 0) + 5), 1)
        reasons.append(f"{st['session']} ({st['window']})")
    else:
        reasons.append(f"Asia/America volatility window inactive ({st['msk']})")
    market["reasons"] = reasons
    return market

def structural_mode_label(value: str) -> str:
    return {
        "off": "OFF",
        "trendline": "Trendline Layer",
        "trendline_rs": "Trendline + RS/BTC",
        "trendline_rs_volume": "Trendline + RS/BTC + Super Volume",
        "structural_only": "Structural Only",
    }.get(value, "OFF")

def safe_float(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default

def create_exchange(exchange_name: str, uid: Optional[str] = None):
    exchange_name = str(exchange_name or DEFAULT_EXCHANGE).lower().strip()
    if exchange_name not in SUPPORTED_EXCHANGES:
        raise ValueError(f"Unsupported exchange: {exchange_name}. Supported: MEXC/BingX/Binance")
    cls = getattr(ccxt, exchange_name)
    options = {"defaultType": "swap"}
    if exchange_name == "binance":
        # Binance futures through ccxt uses USD-M futures with defaultType=future.
        options = {"defaultType": "future"}
    params = {"enableRateLimit": True, "options": options}
    if uid:
        keys = load_json(API_KEYS_FILE, {})
        if uid in keys and exchange_name in keys[uid]:
            params["apiKey"] = keys[uid][exchange_name].get("apiKey", "")
            params["secret"] = keys[uid][exchange_name].get("secret", "")
    return cls(params)

def get_cached_markets(exchange_name: str) -> Dict[str, Any]:
    """Cache exchange markets so every candle request does not reload all markets."""
    now = time.time()
    with _MARKETS_CACHE_LOCK:
        cached = _MARKETS_CACHE.get(exchange_name)
        if cached and now - float(cached.get("ts", 0)) < MARKETS_CACHE_TTL:
            return cached.get("markets", {})

    ex = create_exchange(exchange_name)
    markets = ex.load_markets()
    with _MARKETS_CACHE_LOCK:
        _MARKETS_CACHE[exchange_name] = {"ts": now, "markets": markets}
    return markets

def market_symbol_from_cache(exchange_name: str, symbol: str) -> str:
    markets = get_cached_markets(exchange_name)
    norm = normalize_symbol(symbol)
    candidates = [norm.replace("USDT", "/USDT:USDT"), norm.replace("USDT", "/USDT")]
    return next((c for c in candidates if c in markets), candidates[0])

def _retry_blocking_request(fn, attempts: Optional[int] = None, base_delay: Optional[float] = None):
    """Retry temporary exchange/network errors without hammering the exchange."""
    attempts = max(1, attempts or SCAN_RETRY_ATTEMPTS)
    base_delay = SCAN_RETRY_BASE_DELAY if base_delay is None else base_delay
    last_error = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as e:
            last_error = e
            if attempt >= attempts - 1:
                raise
            time.sleep(base_delay * (attempt + 1))
    raise last_error

def fetch_ohlcv_for_symbol(exchange_name: str, symbol: str, timeframe: str = "15m", limit: int = 200) -> pd.DataFrame:
    ex = create_exchange(exchange_name)
    market_symbol = market_symbol_from_cache(exchange_name, symbol)
    data = _retry_blocking_request(lambda: ex.fetch_ohlcv(market_symbol, timeframe=timeframe, limit=limit))
    df = pd.DataFrame(data, columns=["timestamp", "open", "high", "low", "close", "volume"])
    return df

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["close"] = df["close"].astype(float)
    df["volume"] = df["volume"].astype(float)
    df["ema20"] = df["close"].ewm(span=20).mean()
    df["ema50"] = df["close"].ewm(span=50).mean()
    df["ret"] = df["close"].pct_change()
    high_low = df["high"].astype(float) - df["low"].astype(float)
    high_close = (df["high"].astype(float) - df["close"].shift().astype(float)).abs()
    low_close = (df["low"].astype(float) - df["close"].shift().astype(float)).abs()
    df["atr"] = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1).rolling(14).mean()
    df["vol_ma"] = df["volume"].rolling(30).mean()
    return df

def score_market(exchange_name: str, symbol: str, timeframe: str = "15m") -> Dict[str, Any]:
    df = add_indicators(fetch_ohlcv_for_symbol(exchange_name, symbol, timeframe, 180))
    last = df.iloc[-1]
    prev = df.iloc[-12]
    change = (float(last["close"]) / float(prev["close"]) - 1) * 100
    vol_ratio = float(last["volume"]) / float(last["vol_ma"]) if last.get("vol_ma") and last["vol_ma"] else 1
    trend_up = float(last["ema20"]) > float(last["ema50"])
    trend_down = float(last["ema20"]) < float(last["ema50"])
    score = 50
    direction = "WAIT"
    reasons = []
    if change > 0.8 and trend_up:
        direction = "LONG"; score += 18; reasons.append("momentum bullish")
    elif change < -0.8 and trend_down:
        direction = "SHORT"; score += 18; reasons.append("momentum bearish")
    if vol_ratio > 1.5:
        score += 12; reasons.append(f"volume {vol_ratio:.2f}x")
    if abs(change) > 2:
        score += 8; reasons.append("strong move")
    return {"direction": direction, "score": round(min(score, 99), 1), "price": float(last["close"]), "volume_ratio": round(vol_ratio, 2), "change": round(change, 2), "reasons": reasons}

def score_market_from_df(df: pd.DataFrame) -> Dict[str, Any]:
    last = df.iloc[-1]
    prev = df.iloc[-12]
    change = (float(last["close"]) / float(prev["close"]) - 1) * 100
    vol_ratio = float(last["volume"]) / float(last["vol_ma"]) if last.get("vol_ma") and last["vol_ma"] else 1
    trend_up = float(last["ema20"]) > float(last["ema50"])
    trend_down = float(last["ema20"]) < float(last["ema50"])
    score = 50
    direction = "WAIT"
    reasons = []
    if change > 0.8 and trend_up:
        direction = "LONG"; score += 18; reasons.append("momentum bullish")
    elif change < -0.8 and trend_down:
        direction = "SHORT"; score += 18; reasons.append("momentum bearish")
    if vol_ratio > 1.5:
        score += 12; reasons.append(f"volume {vol_ratio:.2f}x")
    if abs(change) > 2:
        score += 8; reasons.append("strong move")
    return {"direction": direction, "score": round(min(score, 99), 1), "price": float(last["close"]), "volume_ratio": round(vol_ratio, 2), "change": round(change, 2), "reasons": reasons}

def score_market_multi_fast(exchange_name: str, symbol: str, settings: Dict[str, Any], primary_df: pd.DataFrame, override: Optional[str] = None) -> Dict[str, Any]:
    """Same scoring as score_market_multi, but reuses already fetched primary candles."""
    tfs = timeframe_chain(settings, override)
    primary = tfs[0]
    m = score_market_from_df(primary_df)
    details = [f"{primary}: {m['direction']} score {m['score']}"]
    m["mtf_confirmed"] = True

    for higher_tf in tfs[1:]:
        h = score_market(exchange_name, symbol, higher_tf)
        details.append(f"{higher_tf}: {h['direction']} score {h['score']}")
        if m["direction"] != "WAIT" and h["direction"] not in [m["direction"], "WAIT"]:
            m["mtf_confirmed"] = False
            m["score"] = max(0, m["score"] - 15)
            m["reasons"].append(f"{higher_tf} timeframe conflict")
        elif m["direction"] != "WAIT" and h["direction"] == m["direction"]:
            m["score"] = min(100, m["score"] + 3)
            m["reasons"].append(f"{higher_tf} confirms direction")

    m["mtf_details"] = details
    m["timeframes_checked"] = tfs
    return m

def score_market_multi(exchange_name: str, symbol: str, settings: Dict[str, Any], override: Optional[str] = None) -> Dict[str, Any]:
    tfs = timeframe_chain(settings, override)
    primary = tfs[0]
    m = score_market(exchange_name, symbol, primary)
    details = [f"{primary}: {m['direction']} score {m['score']}"]
    m["mtf_confirmed"] = True

    for higher_tf in tfs[1:]:
        h = score_market(exchange_name, symbol, higher_tf)
        details.append(f"{higher_tf}: {h['direction']} score {h['score']}")
        if m["direction"] != "WAIT" and h["direction"] not in [m["direction"], "WAIT"]:
            m["mtf_confirmed"] = False
            m["score"] = max(0, m["score"] - 15)
            m["reasons"].append(f"{higher_tf} timeframe conflict")
        elif m["direction"] != "WAIT" and h["direction"] == m["direction"]:
            m["score"] = min(100, m["score"] + 3)
            m["reasons"].append(f"{higher_tf} confirms direction")

    m["mtf_details"] = details
    m["timeframes_checked"] = tfs
    return m

def get_top_symbols(exchange_name: str, limit: int) -> List[str]:
    try:
        ex = create_exchange(exchange_name)
        tickers = _retry_blocking_request(lambda: ex.fetch_tickers())
        rows = []
        for sym, t in tickers.items():
            if "USDT" in sym and (":USDT" in sym or "/USDT" in sym):
                qv = t.get("quoteVolume") or t.get("baseVolume") or 0
                rows.append((sym, qv))
        rows.sort(key=lambda x: safe_float(x[1]), reverse=True)
        out = []
        for sym, _ in rows[:limit]:
            out.append(sym.split("/")[0].replace(":USDT", "") + "USDT")
        return out or ["BTCUSDT", "ETHUSDT"]
    except Exception:
        return ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"]

def slope(values: List[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    xs = list(range(n))
    xm = sum(xs)/n
    ym = sum(values)/n
    den = sum((x-xm)**2 for x in xs)
    return sum((xs[i]-xm)*(values[i]-ym) for i in range(n))/den if den else 0.0


def detect_3_touch_trendline_bonus(df: pd.DataFrame) -> Dict[str, Any]:
    """
    Hybrid trendline confirmation:
    - Finds simple pivot lows/highs
    - Builds a line from first to last pivot
    - Counts touches near that line
    - Checks breakout pressure
    This is used as a bonus/filter confirmation, not as the only signal source.
    """
    try:
        recent = df.tail(90).copy()
        if len(recent) < 35:
            return {"passed": False, "touches": 0, "bonus": 0, "summary": "3-touch: not enough candles"}

        highs = recent["high"].astype(float).tolist()
        lows = recent["low"].astype(float).tolist()
        closes = recent["close"].astype(float).tolist()
        atr = safe_float(recent["atr"].iloc[-1], 0)
        price = closes[-1]
        if atr <= 0:
            atr = price * 0.006

        tolerance = max(atr * 0.55, price * 0.0025)

        pivot_lows = []
        pivot_highs = []
        for i in range(2, len(recent) - 2):
            if lows[i] <= min(lows[i-2:i]) and lows[i] <= min(lows[i+1:i+3]):
                pivot_lows.append((i, lows[i]))
            if highs[i] >= max(highs[i-2:i]) and highs[i] >= max(highs[i+1:i+3]):
                pivot_highs.append((i, highs[i]))

        def evaluate_line(pivots, kind: str):
            if len(pivots) < 3:
                return {"passed": False, "touches": len(pivots), "kind": kind, "bonus": 0, "breakout": False}

            first = pivots[0]
            last = pivots[-1]
            dx = last[0] - first[0]
            if dx == 0:
                return {"passed": False, "touches": len(pivots), "kind": kind, "bonus": 0, "breakout": False}

            slope_v = (last[1] - first[1]) / dx
            intercept = first[1] - slope_v * first[0]

            touches = 0
            for idx, value in pivots:
                line_value = slope_v * idx + intercept
                if abs(value - line_value) <= tolerance:
                    touches += 1

            current_line = slope_v * (len(recent) - 1) + intercept

            if kind == "resistance":
                breakout = closes[-1] > current_line + tolerance
                direction_hint = "LONG" if breakout else "NEUTRAL"
            else:
                breakout = closes[-1] < current_line - tolerance
                direction_hint = "SHORT" if breakout else "NEUTRAL"

            passed = touches >= 3 and breakout
            bonus = 10 if passed else 5 if touches >= 3 else 0

            return {
                "passed": bool(passed),
                "touches": touches,
                "kind": kind,
                "bonus": bonus,
                "breakout": bool(breakout),
                "line_value": round(current_line, 8),
                "direction_hint": direction_hint,
            }

        support = evaluate_line(pivot_lows, "support")
        resistance = evaluate_line(pivot_highs, "resistance")

        best = support if support.get("bonus", 0) >= resistance.get("bonus", 0) else resistance
        summary = (
            f"3-touch {best.get('kind')}: touches={best.get('touches')}, "
            f"breakout={best.get('breakout')}, line={best.get('line_value')}"
        )

        return {
            "passed": bool(best.get("passed")),
            "touches": int(best.get("touches", 0)),
            "bonus": int(best.get("bonus", 0)),
            "kind": best.get("kind"),
            "breakout": bool(best.get("breakout")),
            "direction_hint": best.get("direction_hint", "NEUTRAL"),
            "summary": summary,
            "support": support,
            "resistance": resistance,
        }

    except Exception as e:
        return {"passed": False, "touches": 0, "bonus": 0, "summary": f"3-touch error: {str(e)[:120]}"}


def detect_trendline_layer(df: pd.DataFrame) -> Dict[str, Any]:
    recent = df.tail(60).copy()
    if len(recent) < 30:
        return {"passed": False, "score_bonus": 0, "summary": "not enough candles"}
    highs = recent["high"].astype(float).tolist()
    lows = recent["low"].astype(float).tolist()
    closes = recent["close"].astype(float).tolist()
    atr = safe_float(recent["atr"].iloc[-1], max(closes[-1]*0.005, 1e-9))
    tolerance = max(atr * 0.45, closes[-1] * 0.002)
    ls, hs = slope(lows), slope(highs)
    low_touches = sum(1 for i,v in enumerate(lows) if abs(v - (lows[0] + ls*i)) <= tolerance)
    high_touches = sum(1 for i,v in enumerate(highs) if abs(v - (highs[0] + hs*i)) <= tolerance)
    first_range = max(highs[:20]) - min(lows[:20])
    last_range = max(highs[-20:]) - min(lows[-20:])
    compression = first_range > 0 and last_range < first_range * 0.75
    close = closes[-1]
    up = close >= max(highs[-20:]) - tolerance
    down = close <= min(lows[-20:]) + tolerance
    passed = (low_touches >= 3 or high_touches >= 3) and (compression or up or down)
    hint = "LONG" if up or (low_touches >= 3 and ls > 0) else "SHORT" if down or (high_touches >= 3 and hs < 0) else "NEUTRAL"
    bonus = (6 if (low_touches >= 3 or high_touches >= 3) else 0) + (6 if compression else 0) + (8 if (up or down) else 0)

    hybrid_3_touch = detect_3_touch_trendline_bonus(df)
    if hybrid_3_touch.get("bonus"):
        bonus += int(hybrid_3_touch.get("bonus", 0))
    if hybrid_3_touch.get("passed"):
        passed = True
        if hybrid_3_touch.get("direction_hint") in ["LONG", "SHORT"]:
            hint = hybrid_3_touch.get("direction_hint")

    return {
        "passed": bool(passed),
        "score_bonus": bonus,
        "low_touches": low_touches,
        "high_touches": high_touches,
        "compression": bool(compression),
        "direction_hint": hint,
        "hybrid_3_touch": hybrid_3_touch,
        "summary": f"structure touches {low_touches}/{high_touches}, compression={compression}, pressure={hint}; {hybrid_3_touch.get('summary')}"
    }

def detect_super_volume_layer(df: pd.DataFrame) -> Dict[str, Any]:
    recent = df.tail(80)
    if len(recent) < 30:
        return {"passed": False, "rvol": 0, "score_bonus": 0, "summary": "not enough candles"}
    cv = float(recent["volume"].iloc[-1])
    av = float(recent["volume"].tail(30).iloc[:-1].mean())
    rvol = cv / av if av > 0 else 0
    bonus = 16 if rvol >= 10 else 10 if rvol >= 5 else 6 if rvol >= 3 else 0
    return {"passed": bool(rvol >= 3), "rvol": round(rvol, 2), "score_bonus": bonus, "summary": f"RVOL {rvol:.2f}x"}

def detect_relative_strength_vs_btc(exchange_name: str, symbol: str, timeframe: str = "1h") -> Dict[str, Any]:
    sym = normalize_symbol(symbol)
    if sym == "BTCUSDT":
        return {"passed": True, "score_bonus": 0, "relative": 0, "direction_hint": "NEUTRAL", "summary": "BTC itself, neutral"}
    try:
        coin = fetch_ohlcv_for_symbol(exchange_name, sym, timeframe, 30)
        btc = fetch_ohlcv_for_symbol(exchange_name, "BTCUSDT", timeframe, 30)
        cc = (float(coin["close"].iloc[-1]) / float(coin["close"].iloc[-12]) - 1) * 100
        bc = (float(btc["close"].iloc[-1]) / float(btc["close"].iloc[-12]) - 1) * 100
        rel = cc - bc
        bonus = 14 if abs(rel) >= 5 else 10 if abs(rel) >= 3 else 5 if abs(rel) >= 1 else 0
        return {"passed": bool(abs(rel) >= 1), "score_bonus": bonus, "relative": round(rel, 2), "direction_hint": "LONG" if rel > 0 else "SHORT", "summary": f"coin {cc:.2f}% vs BTC {bc:.2f}%, relative {rel:.2f}%"}
    except Exception as e:
        return {"passed": False, "score_bonus": 0, "summary": f"RS/BTC error: {str(e)[:120]}"}

def apply_structural_layers(exchange_name: str, symbol: str, df: pd.DataFrame, market: Dict[str, Any], settings: Dict[str, Any]) -> Dict[str, Any]:
    mode = settings.get("structural_mode", "off")
    market = dict(market)
    market["structural_mode"] = mode
    market["structural"] = {}
    if mode == "off":
        return market
    trend = detect_trendline_layer(df)
    market["structural"]["trendline"] = trend
    score = 50 + trend["score_bonus"] if mode == "structural_only" else float(market.get("score", 50)) + (trend["score_bonus"] if trend["passed"] else 0)
    direction = trend.get("direction_hint", "NEUTRAL") if mode == "structural_only" else market.get("direction", "WAIT")
    if mode in ["trendline_rs", "trendline_rs_volume", "structural_only"]:
        rs = detect_relative_strength_vs_btc(exchange_name, symbol, "1h")
        market["structural"]["relative_strength_btc"] = rs
        if rs["passed"]:
            score += rs["score_bonus"]
            if mode == "structural_only" and direction == "NEUTRAL":
                direction = rs.get("direction_hint", "NEUTRAL")
    if mode in ["trendline_rs_volume", "structural_only"]:
        vol = detect_super_volume_layer(df)
        market["structural"]["super_volume"] = vol
        if vol["passed"]:
            score += vol["score_bonus"]
    if mode == "trendline":
        passed = trend["passed"]
    elif mode == "trendline_rs":
        passed = trend["passed"] and market["structural"].get("relative_strength_btc", {}).get("passed", False)
    elif mode in ["trendline_rs_volume", "structural_only"]:
        passed = trend["passed"] and market["structural"].get("relative_strength_btc", {}).get("passed", False) and market["structural"].get("super_volume", {}).get("passed", False)
    else:
        passed = True
    market["structural_passed"] = bool(passed)
    market["score"] = round(min(score, 99), 1)
    if not passed:
        market["direction"] = "WAIT"
        market["reasons"] = market.get("reasons", []) + [f"Structural layer failed: {structural_mode_label(mode)}"]
    elif mode == "structural_only":
        market["direction"] = direction if direction in ["LONG", "SHORT"] else "WAIT"
        market["reasons"] = [f"Structural Only passed: {structural_mode_label(mode)}"]
    else:
        market["reasons"] = market.get("reasons", []) + [f"Structural layer passed: {structural_mode_label(mode)}"]
    return market

def structural_summary_lines(market: Dict[str, Any]) -> List[str]:
    if market.get("structural_mode", "off") == "off":
        return ["Structural Layers: OFF"]
    lines = [f"Structural Layers: {structural_mode_label(market.get('structural_mode'))}", f"Structural Passed: {market.get('structural_passed', False)}"]
    for k, v in market.get("structural", {}).items():
        lines.append(f"{k}: {v.get('summary')}")
        if k == "trendline" and isinstance(v, dict):
            hybrid = v.get("hybrid_3_touch", {})
            if hybrid:
                lines.append(f"3-touch hybrid: {hybrid.get('summary')}")
    return lines

def extract_ai_confidence(ai_text: str) -> float:
    if not ai_text:
        return 0
    for pat in [r"(?:Confidence|Уверенность)\s*[:\-]?\s*(\d{1,3}(?:\.\d+)?)\s*%", r"(?:Confidence|Уверенность)\s*[:\-]?\s*(\d{1,3}(?:\.\d+)?)"]:
        m = re.search(pat, ai_text, re.I)
        if m:
            return min(100, max(0, float(m.group(1))))
    return 85.0 if re.search(r"\bHIGH\b|ВЫСОК", ai_text, re.I) else 0

def should_enable_extended_tp(settings: Dict[str, Any], market: Dict[str, Any], ai_text: str) -> Tuple[bool, float]:
    conf = extract_ai_confidence(ai_text)
    return bool(settings.get("extended_tp_enabled", True) and settings.get("structural_mode") == "trendline_rs_volume" and market.get("structural_passed") and conf >= float(settings.get("extended_tp_min_confidence", 80))), conf

def validate_ai_response_or_raise(settings: Dict[str, Any], ai_text: str):
    if not settings.get("strict_ai_mode", True):
        return
    if not ai_text or not str(ai_text).strip():
        raise ValueError("STRICT AI MODE: AI response is empty. Signal/execution blocked.")
    lowered = str(ai_text).lower()
    if "openai api key не задан" in lowered:
        raise ValueError("STRICT AI MODE: OpenAI API key missing. Переключи AI Provider на Ollama или добавь /setopenai.")
    for marker in ["connection refused", "model not found", "traceback"]:
        if marker in lowered:
            raise ValueError("STRICT AI MODE: AI error detected. Signal/execution blocked.")

def format_uptime(seconds: int) -> str:
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def memory_usage_text() -> str:
    try:
        import psutil
        proc = psutil.Process(os.getpid())
        rss = proc.memory_info().rss / 1024 / 1024
        total = psutil.virtual_memory().total / 1024 / 1024
        used_pct = psutil.virtual_memory().percent
        return f"{rss:.1f} MB process / {total:.0f} MB total / {used_pct:.1f}% used"
    except Exception as e:
        return f"n/a ({str(e)[:80]})"


def ollama_model_installed(model: str) -> bool:
    try:
        r = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=10)
        if r.status_code != 200:
            return False
        models = r.json().get("models", [])
        names = [m.get("name") for m in models if isinstance(m, dict)]
        return model in names or any(str(x).split(":")[0] == model.split(":")[0] for x in names)
    except Exception:
        return False


async def notify_model_pull(context: Optional[ContextTypes.DEFAULT_TYPE], chat_id: Optional[int], model: str, percent: int, uid: Optional[str] = None):
    if context is None or chat_id is None:
        return
    try:
        text_msg = f"⬇️ Загружено {percent}%..."
        if uid is not None:
            await update_work_message(context, chat_id, str(uid), text_msg)
        else:
            await context.bot.send_message(chat_id=chat_id, text=text_msg)
    except Exception:
        pass

async def ensure_ollama_model(model: str, context: Optional[ContextTypes.DEFAULT_TYPE] = None, chat_id: Optional[int] = None, uid: Optional[str] = None) -> bool:
    """
    Ensures Ollama model is present. Sends simple 10/50/100 Telegram notifications.
    Real ollama pull progress is not stable enough to parse reliably across versions,
    so we report key phases.
    """
    if await asyncio.to_thread(ollama_model_installed, model):
        return True

    await notify_model_pull(context, chat_id, model, 10, uid)

    proc = await asyncio.create_subprocess_exec(
        "ollama", "pull", model,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT
    )

    sent_50 = False
    output_tail = []
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        decoded = line.decode("utf-8", errors="ignore").strip()
        if decoded:
            output_tail.append(decoded)
            output_tail = output_tail[-20:]
        if not sent_50:
            sent_50 = True
            await notify_model_pull(context, chat_id, model, 50, uid)

    code = await proc.wait()
    if code != 0:
        raise RuntimeError("Ollama model pull failed: " + " | ".join(output_tail[-5:]))

    await notify_model_pull(context, chat_id, model, 100, uid)
    return True


async def call_ollama_async(model: str, prompt: str, context: Optional[ContextTypes.DEFAULT_TYPE] = None, chat_id: Optional[int] = None, uid: Optional[str] = None, system_prompt: Optional[str] = None, options: Optional[Dict[str, Any]] = None) -> str:
    global LAST_OLLAMA_ACTIVITY
    LAST_OLLAMA_ACTIVITY = time.time()
    request_options = options or {"temperature": 0.2, "num_predict": 160}

    def post_api_chat():
        return requests.post(
            f"{OLLAMA_HOST}/api/chat",
            json={
                "model": model,
                "messages": ([{"role": "system", "content": system_prompt}] if system_prompt else []) + [{"role": "user", "content": prompt}],
                "stream": False,
                "keep_alive": OLLAMA_KEEP_ALIVE_DEFAULT,
                "options": request_options,
            },
            timeout=300
        )

    def post_api_generate():
        return requests.post(
            f"{OLLAMA_HOST}/api/generate",
            json={
                "model": model,
                "prompt": ((system_prompt + "\n\n") if system_prompt else "") + prompt,
                "stream": False,
                "keep_alive": OLLAMA_KEEP_ALIVE_DEFAULT,
                "options": request_options,
            },
            timeout=300
        )

    def post_openai_compatible():
        return requests.post(
            f"{OLLAMA_HOST}/v1/chat/completions",
            json={
                "model": model,
                "messages": ([{"role": "system", "content": system_prompt}] if system_prompt else []) + [{"role": "user", "content": prompt}],
                "stream": False,
                "temperature": request_options.get("temperature", 0.2),
                "max_tokens": request_options.get("num_predict", 160),
            },
            timeout=300
        )

    async with AI_SEMAPHORE:
        # First try current Ollama chat endpoint.
        response = await asyncio.to_thread(post_api_chat)

        # If model/route is missing, pull model and retry once.
        if response.status_code == 404:
            await ensure_ollama_model(model, context, chat_id, uid)
            response = await asyncio.to_thread(post_api_chat)

        # Some Railway/Ollama builds expose generate or OpenAI-compatible endpoint instead.
        if response.status_code == 404:
            response = await asyncio.to_thread(post_api_generate)

        if response.status_code == 404:
            response = await asyncio.to_thread(post_openai_compatible)

    response.raise_for_status()
    data = response.json()

    if "message" in data and isinstance(data["message"], dict):
        return data["message"].get("content", "")

    if "response" in data:
        return data.get("response", "")

    if "choices" in data and data["choices"]:
        return data["choices"][0].get("message", {}).get("content", "")

    return ""


async def check_ai_health(uid: str, context: Optional[ContextTypes.DEFAULT_TYPE] = None, chat_id: Optional[int] = None) -> str:
    s = get_settings(uid)
    started = time.perf_counter()
    try:
        if s.get("ai_provider") == "ollama":
            model = s.get("ollama_model", DEFAULT_MODEL)
            if not await asyncio.to_thread(ollama_model_installed, model):
                return f"⚠️ Ollama model not installed: {model}"

            payload_chat = {"model": model, "messages": [{"role": "user", "content": "ping"}], "stream": False}
            payload_generate = {"model": model, "prompt": "ping", "stream": False}

            endpoints = [
                ("/api/chat", payload_chat),
                ("/api/generate", payload_generate),
                ("/v1/chat/completions", payload_chat),
            ]

            last_status = None
            for endpoint, payload in endpoints:
                r = await asyncio.to_thread(requests.post, f"{OLLAMA_HOST}{endpoint}", json=payload, timeout=60)
                last_status = r.status_code
                if r.status_code == 200:
                    return f"✅ OK {endpoint} ({round((time.perf_counter()-started)*1000)} ms)"

            return f"⚠️ Ollama unavailable: last status {last_status}"

        keys = load_json(OPENAI_KEYS_FILE, {})
        api_key = keys.get(uid) or os.getenv("OPENAI_API_KEY")
        return "✅ Key present" if api_key else "⚠️ OpenAI key missing"
    except Exception as e:
        return f"❌ {str(e)[:160]}"


async def check_exchange_api(uid: str) -> str:
    s = get_settings(uid)
    started = time.perf_counter()
    try:
        ex = create_exchange(s["exchange"])
        await asyncio.to_thread(ex.fetch_ticker, "BTC/USDT:USDT")
        return f"✅ OK ({round((time.perf_counter()-started)*1000)} ms)"
    except Exception:
        try:
            await asyncio.to_thread(ex.fetch_ticker, "BTC/USDT")
            return f"✅ OK ({round((time.perf_counter()-started)*1000)} ms)"
        except Exception as e:
            return f"❌ {str(e)[:160]}"


def call_ollama(model: str, prompt: str, system_prompt: Optional[str] = None, options: Optional[Dict[str, Any]] = None) -> str:
    global LAST_OLLAMA_ACTIVITY
    LAST_OLLAMA_ACTIVITY = time.time()
    request_options = options or {"temperature": 0.2, "num_predict": 160}
    r = requests.post(
        f"{OLLAMA_HOST}/api/chat",
        json={
            "model": model,
            "messages": ([{"role": "system", "content": system_prompt}] if system_prompt else []) + [
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "stream": False,
            "keep_alive": OLLAMA_KEEP_ALIVE_DEFAULT,
            "options": request_options,
        },
        timeout=300
    )
    r.raise_for_status()
    data = r.json()
    return data.get("message", {}).get("content", "")

def _extract_openai_response_text(data: Dict[str, Any]) -> str:
    """Extract text from OpenAI Responses API or Chat Completions API."""
    if not isinstance(data, dict):
        return ""

    # Responses API usually returns output_text.
    if isinstance(data.get("output_text"), str) and data.get("output_text"):
        return data.get("output_text", "")

    # Fallback for Responses API content blocks.
    chunks = []
    for item in data.get("output", []) or []:
        for content in item.get("content", []) or []:
            if isinstance(content, dict):
                if isinstance(content.get("text"), str):
                    chunks.append(content["text"])
                elif isinstance(content.get("value"), str):
                    chunks.append(content["value"])
    if chunks:
        return "\n".join(chunks).strip()

    # Fallback for legacy Chat Completions.
    choices = data.get("choices") or []
    if choices:
        msg = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
        if isinstance(msg.get("content"), str):
            return msg.get("content", "")

    return ""


def call_openai(uid: str, model: str, prompt: str, reasoning: str, system_prompt: Optional[str] = None, options: Optional[Dict[str, Any]] = None) -> str:
    keys = load_json(OPENAI_KEYS_FILE, {})
    api_key = keys.get(uid) or os.getenv("OPENAI_API_KEY")
    if not api_key:
        return "OpenAI API key не задан. Используй /setopenai OPENAI_API_KEY"

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    request_options = options or {"temperature": 0.2, "num_predict": 160}
    max_tokens = int(request_options.get("num_predict", 160) or 160)

    # New OpenAI models use Responses API more reliably than /v1/chat/completions.
    responses_body = {
        "model": model,
        "input": prompt,
        "max_output_tokens": max_tokens,
    }

    reasoning_effort = str(reasoning or "medium").strip().lower()
    if reasoning_effort in {"low", "medium", "high"}:
        responses_body["reasoning"] = {"effort": reasoning_effort}

    if system_prompt:
        responses_body["instructions"] = system_prompt

    r = requests.post("https://api.openai.com/v1/responses", headers=headers, json=responses_body, timeout=300)
    if r.status_code == 200:
        return _extract_openai_response_text(r.json())

    # Compatibility fallback for older models/accounts.
    responses_error = r.text[:500]
    chat_body = {
        "model": model,
        "messages": ([{"role": "system", "content": system_prompt}] if system_prompt else []) + [{"role": "user", "content": prompt}],
        "temperature": request_options.get("temperature", 0.2),
        "max_tokens": max_tokens,
    }
    r2 = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=chat_body, timeout=300)
    if r2.status_code == 200:
        return _extract_openai_response_text(r2.json())

    # Raise a short, useful error for Telegram instead of a raw huge payload.
    raise RuntimeError(f"OpenAI error responses={r.status_code}: {responses_error} | chat={r2.status_code}: {r2.text[:500]}")

async def call_ai(uid: str, prompt: str, context: Optional[ContextTypes.DEFAULT_TYPE] = None, chat_id: Optional[int] = None, system_prompt: Optional[str] = None, options: Optional[Dict[str, Any]] = None) -> str:
    s = get_settings(uid)
    if s.get("ai_provider") == "openai":
        return await asyncio.to_thread(call_openai, uid, s.get("openai_model"), prompt, s.get("reasoning_level"), system_prompt, options)
    return await call_ollama_async(s.get("ollama_model", DEFAULT_MODEL), prompt, context, chat_id, uid, system_prompt, options)


def calculate_trade_management_plan(levels: Dict[str, Any]) -> Dict[str, Any]:
    """
    Trade Management Engine:
    - Move SL to breakeven at configured breakeven_r
    - Partial TP at configured partial_tp_r / partial_tp_percent
    - Trailing with configured trailing_r
    """
    try:
        entry = safe_float(levels.get("entry"), 0)
        sl = safe_float(levels.get("sl"), 0)
        tp1 = safe_float(levels.get("tp1"), 0)
        tp2 = safe_float(levels.get("tp2"), 0)
        side = levels.get("side", "WAIT")

        if not entry or not sl or side == "WAIT":
            return {}

        risk = abs(entry - sl)

        settings = DEFAULT_SETTINGS
        be_r = safe_float(settings.get("breakeven_r"), 1)
        partial_r = safe_float(settings.get("partial_tp_r"), 1)

        if side == "LONG":
            be_trigger = round(entry + risk * be_r, 8)
            partial_trigger = round(entry + risk * partial_r, 8)
        else:
            be_trigger = round(entry - risk * be_r, 8)
            partial_trigger = round(entry - risk * partial_r, 8)

        return {
            "be_trigger": be_trigger,
            "partial_trigger": partial_trigger,
            "partial_close_percent": safe_float(settings.get("partial_tp_percent"), 50),
            "trailing_enabled": bool(settings.get("trailing_enabled", True)),
            "runner_target": tp2,
        }
    except Exception:
        return {}


def infer_dynamic_rr(market: Dict[str, Any]) -> Tuple[float, str]:
    """
    Dynamic RR for TP distance:
    - Trendline + RS/BTC + Super Volume -> 1:4
    - Trendline/trending setup -> 1:3
    - Standard setup -> 1:2
    """
    structural = market.get("structural", {}) or {}
    trending = bool(structural.get("trendline", {}).get("passed"))
    rsbtc = bool(structural.get("relative_strength_btc", {}).get("passed"))
    super_volume = bool(structural.get("super_volume", {}).get("passed"))

    if trending and rsbtc and super_volume:
        return 4.0, "trend_rsbtc_super_volume_1_4"
    if trending:
        return 3.0, "trend_1_3"
    return 2.0, "standard_1_2"


def calculate_trade_levels(symbol: str, market: Dict[str, Any], df: pd.DataFrame, settings: Dict[str, Any]) -> Dict[str, Any]:
    """
    Bot-side deterministic Entry/SL/TP calculation.
    AI does NOT invent levels; AI only approves/rejects.
    """
    direction = str(market.get("direction", "WAIT")).upper()
    price = safe_float(market.get("price"), 0)
    if price <= 0:
        try:
            price = float(df["close"].iloc[-1])
        except Exception:
            price = 0

    atr = 0
    try:
        atr = safe_float(df["atr"].iloc[-1], 0)
    except Exception:
        atr = 0

    if atr <= 0 and price > 0:
        atr = price * 0.006

    rr, profile = infer_dynamic_rr(market)

    if direction not in ["LONG", "SHORT"] or price <= 0:
        return {
            "side": "WAIT",
            "entry": None,
            "sl": None,
            "tp1": None,
            "tp2": None,
            "rr": None,
            "profile": "none",
        }

    risk_distance = max(atr * 1.2, price * 0.004)

    if direction == "LONG":
        entry = price
        sl = entry - risk_distance
        tp1 = entry + risk_distance * 2
        tp2 = entry + risk_distance * rr
    else:
        entry = price
        sl = entry + risk_distance
        tp1 = entry - risk_distance * 2
        tp2 = entry - risk_distance * rr

    return {
        "side": direction,
        "entry": round(entry, 8),
        "sl": round(sl, 8),
        "tp1": round(tp1, 8),
        "tp2": round(tp2, 8),
        "rr": rr,
        "profile": profile,
    }


def collect_signal_reasons(market: Dict[str, Any], settings: Dict[str, Any]) -> List[str]:
    reasons = []

    for r in market.get("reasons", []) or []:
        if r and str(r) not in reasons:
            reasons.append(str(r))

    if market.get("mtf_confirmed", True):
        reasons.append("MTF confirmed")
    else:
        reasons.append("MTF conflict")

    structural = market.get("structural", {}) or {}
    if "trendline" in structural:
        tr = structural["trendline"]
        hybrid = tr.get("hybrid_3_touch", {}) if isinstance(tr, dict) else {}
        if tr.get("passed"):
            reasons.append("Trendline breakout / structure confirmed")
            if hybrid.get("touches", 0) >= 3:
                reasons.append(f"Hybrid 3-touch trendline confirmed ({hybrid.get('touches')} touches)")
        else:
            reasons.append("Trendline not confirmed")

    if "relative_strength_btc" in structural:
        rs = structural["relative_strength_btc"]
        if rs.get("passed"):
            rel = rs.get("relative")
            reasons.append(f"RS/BTC strong ({rel}%)")
        else:
            reasons.append("RS/BTC weak")

    if "super_volume" in structural:
        vol = structural["super_volume"]
        if vol.get("passed"):
            reasons.append(f"Super Volume spike ({vol.get('rvol')}x)")
        else:
            reasons.append("Super Volume not confirmed")

    # compact unique reasons
    clean = []
    for r in reasons:
        if r and r not in clean:
            clean.append(r)
    return clean[:8]


def extract_ai_verdict(ai_text: str, market: Dict[str, Any]) -> Dict[str, Any]:
    raw = ai_text or ""
    up = raw.upper()

    confidence = extract_ai_confidence(raw)
    if confidence <= 0:
        confidence = safe_float(market.get("score"), 0)

    # Hard safety rule:
    # If bot/core signal is WAIT, AI cannot approve it.
    if str(market.get("direction", "WAIT")).upper() == "WAIT":
        verdict = "REJECTED"
    elif "REJECTED" in up or "REJECT" in up or "WAIT" in up or "NO TRADE" in up:
        verdict = "REJECTED"
    elif "APPROVED" in up or "APPROVE" in up or "PASS" in up:
        verdict = "APPROVED"
    else:
        # fallback: approve only if bot score is strong and direction is not WAIT
        verdict = "APPROVED" if market.get("direction") in ["LONG", "SHORT"] and safe_float(market.get("score"), 0) >= 70 else "REJECTED"

    reason = ""
    m = re.search(r"(?:REASON|Причина|WHY)\s*[:\-]\s*(.+)", raw, re.I)
    if m:
        reason = m.group(1).strip()[:240]
    elif raw:
        reason = re.sub(r"\s+", " ", raw).strip()[:240]

    return {
        "verdict": verdict,
        "confidence": round(confidence, 2),
        "reason": reason or ("AI approved setup" if verdict == "APPROVED" else "AI rejected setup"),
    }


def format_strict_signal(symbol: str, timeframe: str, settings: Dict[str, Any], market: Dict[str, Any], levels: Dict[str, Any], ai_verdict: Dict[str, Any]) -> str:
    reasons = collect_signal_reasons(market, settings)
    reasons_text = "\n".join([f"• {r}" for r in reasons]) if reasons else "• No strong reasons"

    tm = calculate_trade_management_plan(levels)

    verdict_icon = "✅" if ai_verdict["verdict"] == "APPROVED" else "❌"
    side = levels.get("side") or market.get("direction", "WAIT")
    score = market.get("score", "-")

    if ai_verdict["verdict"] != "APPROVED" or side == "WAIT":
        return (
            f"📊 {normalize_symbol(symbol)} | {settings['exchange'].upper()} | {timeframe}\n\n"
            f"SIDE: WAIT\n"
            f"SCORE: {score}%\n\n"
            f"REASONS:\n{reasons_text}\n\n"
            f"AI VERDICT:\n"
            f"{verdict_icon} {ai_verdict['verdict']}\n"
            f"Confidence: {ai_verdict['confidence']}%\n"
            f"Reason: {ai_verdict['reason']}\n\n"
            f"STRICT AI: PASS — no trade without approval"
        )

    return (
        f"📊 {normalize_symbol(symbol)} | {settings['exchange'].upper()} | {timeframe}\n\n"
        f"SIDE: {side}\n"
        f"SCORE: {score}%\n\n"
        f"ENTRY: {levels.get('entry')}\n"
        f"SL: {levels.get('sl')}\n"
        f"TP1: {levels.get('tp1')}\n"
        f"TP2: {levels.get('tp2')}\n"
        f"RR: 1:{levels.get('rr')}\n"
        f"TP PROFILE: {levels.get('profile')}\n\n"
        f"TRADE MANAGEMENT:\n"
        f"• BE Trigger: {tm.get('be_trigger')}\n"
        f"• Partial TP: {tm.get('partial_close_percent')}% at TP1\n"
        f"• Trailing Stop: {'ON' if tm.get('trailing_enabled') else 'OFF'}\n"
        f"• Runner Target: {tm.get('runner_target')}\n\n"
        f"REASONS:\n{reasons_text}\n\n"
        f"AI VERDICT:\n"
        f"{verdict_icon} {ai_verdict['verdict']}\n"
        f"Confidence: {ai_verdict['confidence']}%\n"
        f"Reason: {ai_verdict['reason']}\n\n"
        f"STRICT AI: PASS"
    )



def build_signal_prompt(symbol: str, timeframe: str, market: Dict[str, Any], settings: Dict[str, Any]) -> str:
    return f"""
You are a STRICT AI trade approval engine.

Your task:
- DO NOT write an essay.
- DO NOT invent entry, SL, or TP.
- The bot calculates all levels.
- You only approve or reject the setup.

Return ONLY this format:

AI_VERDICT: APPROVED or REJECTED
CONFIDENCE: 0-100
REASON: one short sentence

Setup:
Symbol: {symbol}
Timeframe: {timeframe}
Exchange: {settings['exchange']}
Direction: {market.get('direction')}
Score: {market.get('score')}
Price: {market.get('price')}
MTF confirmed: {market.get('mtf_confirmed', True)}
Reasons: {market.get('reasons')}
Structural mode: {market.get('structural_mode')}
Structural passed: {market.get('structural_passed')}
Structural data: {market.get('structural')}

Approval rules:
- APPROVED only if setup is clear and tradable.
- REJECTED if Direction is WAIT, weak volume, unclear direction, no breakout, poor RS/BTC, MTF conflict, or low confidence.
- Keep answer under 3 lines.
"""

def get_work_message_id(uid: str) -> Optional[int]:
    data = load_json(WORK_MESSAGE_IDS_FILE, {})
    value = data.get(uid)
    try:
        return int(value) if value else None
    except Exception:
        return None


def set_work_message_id(uid: str, message_id: int):
    data = load_json(WORK_MESSAGE_IDS_FILE, {})
    data[uid] = int(message_id)
    save_json(WORK_MESSAGE_IDS_FILE, data)


async def _resolve_message_text(text) -> str:
    if inspect.isawaitable(text):
        text = await text
    return str(text)


async def update_work_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, uid: str, text: str, reply_markup=None):
    """
    One active work message for buttons/menu/status/ping/scan/model loading.
    AI Chat and trading signals are intentionally NOT routed here.
    """
    text = await _resolve_message_text(text)
    markup = reply_markup if reply_markup is not None else main_menu(get_settings(uid))
    message_id = get_work_message_id(uid)
    if message_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text[:3900],
                reply_markup=markup
            )
            return
        except Exception:
            pass
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=text[:3900],
        reply_markup=markup
    )
    set_work_message_id(uid, msg.message_id)



async def send_below_buttons(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, uid: str, reply_markup=None):
    text = await _resolve_message_text(text)
    await context.bot.send_message(chat_id=chat_id, text=text[:3900], reply_markup=reply_markup)
    # Keep main inline menu at bottom for service messages.
    # If this message is a submenu with its own inline keyboard, leave it as the active bottom menu.
    if reply_markup is None:
        try:
            await refresh_menu_bottom(context, chat_id, uid)
        except Exception:
            pass

def inline_main_menu(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    """Main inline control panel built only from fresh settings."""
    universe_label = "Only BTC/ETH" if settings.get("market_universe") == "btc_eth" else "All Market"
    provider_label = str(settings.get("ai_provider", "ollama")).upper()
    model_label = str(get_active_model(settings))[:22]
    trading_label = str(settings.get("trading_mode", "manual")).upper()
    tf_label = timeframe_label(str(settings.get("timeframe_mode", "15m")))
    auto_label = auto_scanner_label(str(settings.get("auto_scanner_interval", "off")))
    structural_label = structural_mode_label(str(settings.get("structural_mode", "off")))
    stop_label = "ON" if settings.get("stop_all_enabled") else "OFF"
    sync_label = "ON" if settings.get("position_sync_enabled") else "OFF"
    live_tm_label = "ON" if settings.get("live_trade_manager_enabled") else "OFF"
    sessions_label = "ON" if settings.get("session_filter") else "OFF"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"📋 TopLimit: {top_limit_label(settings)}", callback_data="menu:toplimit"),
            InlineKeyboardButton("💬 AI Chat", callback_data="mode:chat"),
        ],
        [
            InlineKeyboardButton(f"🤖 Provider: {provider_label}", callback_data="menu:provider"),
            InlineKeyboardButton(f"🧠 Model: {model_label}", callback_data="menu:model"),
        ],
        [
            InlineKeyboardButton(f"🧠 Reasoning: {str(settings.get('reasoning_level', 'medium')).upper()}", callback_data="menu:reasoning"),
            InlineKeyboardButton(f"🏦 Exchange: {str(settings.get('exchange', '')).upper()}", callback_data="menu:exchange"),
        ],
        [
            InlineKeyboardButton(f"🤖 Trading: {trading_label}", callback_data="menu:tradingmode"),
            InlineKeyboardButton(f"🕘 TF: {tf_label}", callback_data="menu:timeframe"),
        ],
        [
            InlineKeyboardButton(f"🔄 Auto Scanner: {auto_label}", callback_data="menu:autoscanner"),
            InlineKeyboardButton(f"🧠 Structural: {structural_label}", callback_data="menu:structural"),
        ],
        [
            InlineKeyboardButton("🔥 Top-50", callback_data="scan:50"),
            InlineKeyboardButton("🔥 Top-100", callback_data="scan:100"),
            InlineKeyboardButton("🔥 Top-200", callback_data="scan:200"),
        ],
        [
            InlineKeyboardButton("📊 Status", callback_data="status"),
            InlineKeyboardButton("📡 Ping", callback_data="ping"),
            InlineKeyboardButton("🧠 Ping AI", callback_data="ping_ai"),
        ],
        [
            InlineKeyboardButton("📊 Positions", callback_data="positions"),
            InlineKeyboardButton("🛡 Trade Mgmt", callback_data="menu:trademgmt"),
        ],
        [
            InlineKeyboardButton(f"🚨 STOP ALL: {stop_label}", callback_data="toggle:stopall"),
            InlineKeyboardButton(f"🔁 Position Sync: {sync_label}", callback_data="toggle:positionsync"),
            InlineKeyboardButton(f"📈 Live TM: {live_tm_label}", callback_data="toggle:livetrademanager"),
        ],
        [
            InlineKeyboardButton(f"🌐 {universe_label}", callback_data="toggle:btceth"),
            InlineKeyboardButton(f"🌏 Asia/US Vol: {sessions_label}", callback_data="toggle:sessions"),
        ],
        [
            InlineKeyboardButton("❓ Help", callback_data="help"),
        ],
    ])


def main_menu(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    return inline_main_menu(settings)


async def refresh_menu_bottom(context: ContextTypes.DEFAULT_TYPE, chat_id: int, uid: str, text_msg: str = None):
    """
    Keep inline menu at the bottom:
    - delete previous stored menu message if possible
    - send new menu message
    - save new message_id
    Signals and AI-chat messages remain untouched.
    """
    try:
        old_id = get_work_message_id(uid)
        if old_id:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=old_id)
            except Exception:
                pass
    except Exception:
        pass

    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=text_msg or f"🤖 Trading Bot v{BOT_VERSION}\n\nInline menu активно.",
        reply_markup=main_menu(get_settings(uid))
    )

    try:
        set_work_message_id(uid, msg.message_id)
    except Exception:
        pass

    return msg


async def send_service_and_refresh_menu(context: ContextTypes.DEFAULT_TYPE, chat_id: int, uid: str, msg: str, reply_markup=None):
    """
    Service actions: send response, then move inline menu to bottom.
    Use for settings/status/ping/menu actions.
    Do not use for AI chat or signal messages.
    """
    await context.bot.send_message(chat_id=chat_id, text=str(msg)[:3900], reply_markup=reply_markup)
    await refresh_menu_bottom(context, chat_id, uid)



async def show_inline_menu_message(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str = None):
    uid = user_id(update)
    chat_id = update.effective_chat.id
    await refresh_menu_bottom(
        context,
        chat_id,
        uid,
        text or f"🤖 Trading Bot v{BOT_VERSION}\n\nInline menu активировано."
    )

def build_main_menu(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    return inline_main_menu(settings)

main_menu = build_main_menu

def auto_scanner_menu(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    modes = [("15m", "15 мин"), ("60m", "60 мин"), ("4h", "4 часа"), ("12h", "12 часов"), ("24h", "24 часа"), ("off", "Выкл")]
    cur = settings.get("auto_scanner_interval", "off")
    rows = [[InlineKeyboardButton(("✅ " if cur == k else "") + label, callback_data=f"autoscanner:{k}")] for k, label in modes]
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="back:main")])
    return InlineKeyboardMarkup(rows)

def top_limit_menu(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    cur = normalize_top_limit_value(settings.get("top_limit", "5"), "5")
    modes = [("5", "TopLimit 5"), ("10", "TopLimit 10"), ("all", "TopLimit ALL")]
    rows = [[InlineKeyboardButton(("✅ " if cur == k else "") + label, callback_data=f"toplimit:{k}")] for k, label in modes]
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="back:main")])
    return InlineKeyboardMarkup(rows)


def normalize_top_limit_value(value: Any, default: str = "5") -> str:
    """Normalize TopLimit to one of the supported UI modes: 5, 10, all.

    Older settings/env values could contain ints, upper-case ALL, spaces, or an
    unsupported number. Keeping a single normalized representation prevents the
    scanner/AI approval from silently falling back to 5 after the user selected 10.
    """
    raw = str(value if value is not None else default).strip().lower()
    if raw in {"5", "10", "all"}:
        return raw
    return default


def selected_top_limit(settings: Dict[str, Any], fallback: int = 5) -> Optional[int]:
    value = normalize_top_limit_value(settings.get("top_limit", str(fallback)), str(fallback))
    if value == "all":
        return None
    return int(value)


def top_limit_label(settings: Dict[str, Any]) -> str:
    return normalize_top_limit_value(settings.get("top_limit", "5"), "5").upper()


def structural_layers_menu(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    modes = [("off", "OFF"), ("trendline", "Trendline Layer"), ("trendline_rs", "Trendline + Relative Strength vs BTC"), ("trendline_rs_volume", "Trendline + RS/BTC + Super Volume"), ("structural_only", "Structural Only")]
    cur = settings.get("structural_mode", "off")
    rows = [[InlineKeyboardButton(("✅ " if cur == k else "") + label, callback_data=f"structural:{k}")] for k, label in modes]
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="back:main")])
    return InlineKeyboardMarkup(rows)

def trading_mode_menu(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    modes = [("manual", "🟢 Manual"), ("confirm", "🟡 Confirm"), ("auto", "🔴 Auto")]
    return InlineKeyboardMarkup([[InlineKeyboardButton(("✅ " if settings.get("trading_mode") == m else "") + label, callback_data=f"tradingmode:{m}")] for m, label in modes] + [[InlineKeyboardButton("⬅️ Назад", callback_data="back:main")]])

def trade_mgmt_menu(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(("✅ " if settings.get("breakeven_enabled") else "❌ ") + "Breakeven", callback_data="toggle:breakeven")],
        [InlineKeyboardButton(("✅ " if settings.get("trailing_enabled") else "❌ ") + "Trailing", callback_data="toggle:trailing")],
        [InlineKeyboardButton(("✅ " if settings.get("partial_tp_enabled") else "❌ ") + "Partial TP", callback_data="toggle:partialtp")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back:main")],
    ])

def provider_menu(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(("✅ " if settings.get("ai_provider") == "ollama" else "") + "Ollama local (бесплатно)", callback_data="provider:ollama")],
        [InlineKeyboardButton(("✅ " if settings.get("ai_provider") == "openai" else "") + "OpenAI ChatGPT (платно)", callback_data="provider:openai")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back:main")],
    ])

def exchange_menu(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(("✅ " if settings.get("exchange") == ex else "") + ex.upper(), callback_data=f"exchange:{ex}")] for ex in ["mexc", "bingx", "binance"]] + [[InlineKeyboardButton("⬅️ Назад", callback_data="back:main")]])


def tradingmode_menu(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    current = str(settings.get("trading_mode", "manual")).lower()

    rows = [
        [
            InlineKeyboardButton(
                ("🟢 AUTO" if current == "auto" else "AUTO"),
                callback_data="tradingmode:auto"
            ),
            InlineKeyboardButton(
                ("🟢 MANUAL" if current == "manual" else "MANUAL"),
                callback_data="tradingmode:manual"
            ),
        ],
        [
            InlineKeyboardButton("⬅️ Back", callback_data="back:main")
        ]
    ]

    return InlineKeyboardMarkup(rows)


def apply_trading_mode(uid: str, mode: str) -> str:
    mode = str(mode or "manual").lower().strip()
    if mode == "auto":
        set_setting(uid, "trading_mode", "auto")
        set_setting(uid, "trading_enabled", True)
        set_setting(uid, "ai_auto", True)
        return "✅ Trading Mode: AUTO\n🚀 Trading ON\n🧠 AI Auto ON"
    set_setting(uid, "trading_mode", "manual")
    set_setting(uid, "trading_enabled", False)
    set_setting(uid, "ai_auto", False)
    return "✅ Trading Mode: MANUAL\n🚀 Trading OFF\n🧠 AI Auto OFF"


def timeframe_menu(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    modes = [("15m", "15 мин"), ("15m_1h", "15 мин/1час"), ("1h_4h", "1 час/4 часа"), ("multi", "мульти 15m+1h+4h+1d")]
    return InlineKeyboardMarkup([[InlineKeyboardButton(("✅ " if settings.get("timeframe_mode") == k else "") + label, callback_data=f"timeframe:{k}")] for k, label in modes] + [[InlineKeyboardButton("⬅️ Назад", callback_data="back:main")]])

def model_menu(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    if settings.get("ai_provider") == "openai":
        models = ["gpt-5.5", "gpt-5.5-pro", "gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano", "gpt-5.4-pro", "gpt-4.1-mini", "gpt-4.1", "gpt-4o-mini", "gpt-4o"]
        return InlineKeyboardMarkup([[InlineKeyboardButton(("✅ " if settings.get("openai_model") == m else "") + m, callback_data=f"openai_model:{m}")] for m in models] + [[InlineKeyboardButton("⬅️ Назад", callback_data="back:main")]])
    return InlineKeyboardMarkup([[InlineKeyboardButton(("✅ " if settings.get("ollama_model") == m else "") + m, callback_data=f"ollama_model:{m}")] for m in OLLAMA_MODELS] + [[InlineKeyboardButton("⬅️ Назад", callback_data="back:main")]])

def reasoning_menu(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(("✅ " if settings.get("reasoning_level") == r else "") + r.upper(), callback_data=f"reasoning:{r}")] for r in ["low", "medium", "high"]] + [[InlineKeyboardButton("⬅️ Назад", callback_data="back:main")]])

def _positions(uid: str) -> List[Dict[str, Any]]:
    return load_json(POSITIONS_FILE, {}).get(uid, [])

def _save_positions(uid: str, positions: List[Dict[str, Any]]):
    data = load_json(POSITIONS_FILE, {})
    data[uid] = positions
    save_json(POSITIONS_FILE, data)

def _cooldown_state(uid: str):
    return load_json(COOLDOWN_FILE, {}).get(uid, {"losses": 0, "until": 0, "last_event": None})

def _save_cooldown_state(uid: str, state):
    data = load_json(COOLDOWN_FILE, {})
    data[uid] = state
    save_json(COOLDOWN_FILE, data)

def is_cooldown_active(uid: str) -> Tuple[bool, str]:
    s = get_settings(uid)
    if not s.get("cooldown_enabled", True):
        return False, "Cooldown disabled"
    st = _cooldown_state(uid)
    until = float(st.get("until", 0) or 0)
    if time.time() < until:
        return True, f"🧊 Smart Cooldown active: ~{int((until-time.time())/60)} min left"
    return False, "Cooldown inactive"

def register_trade_event(uid: str, event_type: str, note: str = "", pnl: float = 0.0):
    data = load_json(TRADE_EVENTS_FILE, {})
    data.setdefault(uid, []).append({"ts": time.time(), "type": event_type, "note": note, "pnl": pnl})
    data[uid] = data[uid][-200:]
    save_json(TRADE_EVENTS_FILE, data)

def stop_all_active(uid: str) -> bool:
    return bool(get_settings(uid).get("stop_all_enabled", False))

def get_private_exchange(uid: str):
    s = get_settings(uid)
    keys = load_json(API_KEYS_FILE, {})
    ex_name = s["exchange"]
    if uid not in keys or ex_name not in keys[uid]:
        raise ValueError(f"API keys not set for {ex_name.upper()}. Use /setapi {ex_name} API_KEY API_SECRET")
    return create_exchange(ex_name, uid)

def exchange_symbol_for_order(ex, raw_symbol: str) -> str:
    markets = ex.load_markets()
    norm = normalize_symbol(raw_symbol)
    candidates = [norm.replace("USDT", "/USDT:USDT"), norm.replace("USDT", "/USDT")]
    for c in candidates:
        if c in markets:
            return c
    raise ValueError(f"Symbol {norm} not found")

def get_usdt_free_balance(ex) -> float:
    b = ex.fetch_balance()
    for k in ["free", "total"]:
        p = b.get(k, {})
        if isinstance(p, dict) and "USDT" in p:
            return safe_float(p["USDT"])
    return 0

def calc_amount_from_risk(entry, sl, balance, risk_percent, leverage):
    risk_usdt = balance * risk_percent / 100
    dist = abs(entry - sl)
    if entry <= 0 or dist <= 0:
        raise ValueError("Invalid sizing inputs")
    return min(risk_usdt / dist, (balance * leverage) / entry)

def set_isolated_and_leverage(ex, symbol, leverage):
    warnings = []
    try:
        if hasattr(ex, "set_margin_mode"):
            ex.set_margin_mode("isolated", symbol)
    except Exception as e:
        warnings.append(str(e)[:160])
    try:
        if hasattr(ex, "set_leverage"):
            ex.set_leverage(leverage, symbol, {"marginMode": "isolated"})
    except Exception as e:
        warnings.append(str(e)[:160])
    return warnings

def side_for(direction): return "buy" if direction.upper() == "LONG" else "sell"
def close_side_for(direction): return "sell" if direction.upper() == "LONG" else "buy"

def place_protective_order(ex, symbol, direction, amount, trigger_price, kind):
    side = close_side_for(direction)
    types = ["stop_market", "market"] if kind == "sl" else ["take_profit_market", "market"]
    errors = []
    for typ in types:
        for params in [{"reduceOnly": True, "triggerPrice": trigger_price, "marginMode": "isolated"}]:
            try:
                return ex.create_order(symbol, typ, side, amount, None, params)
            except Exception as e:
                errors.append(str(e)[:120])
    return {"warning": f"{kind} order not placed", "errors": errors}

async def execute_real_trade(uid: str, symbol: str, direction: str, stop_loss=None, take_profit=None, rr: Optional[float] = None) -> Dict[str, Any]:
    if stop_all_active(uid):
        raise ValueError("🚨 STOP ALL is ON. Execution blocked.")
    s = get_settings(uid)
    if not s.get("real_execution_enabled"):
        raise ValueError("Real execution OFF. Use /real_on.")
    if not s.get("trading_enabled"):
        raise ValueError("Trading OFF. Use /trading_on.")
    if s["exchange"] not in SUPPORTED_EXCHANGES:
        raise ValueError("Real execution supports MEXC/BingX/Binance.")
    active, msg = is_cooldown_active(uid)
    if active:
        raise ValueError(msg)
    if s.get("duplicate_protection_enabled", True):
        is_dup, dup_msg = is_duplicate_open_trade(uid, symbol, direction)
        if is_dup:
            raise ValueError(dup_msg)
    ex = get_private_exchange(uid)
    ms = exchange_symbol_for_order(ex, symbol)
    ticker = ex.fetch_ticker(ms)
    entry = safe_float(ticker.get("last") or ticker.get("close"))
    if not stop_loss:
        stop_loss = entry * (0.99 if direction.upper() == "LONG" else 1.01)
    if not take_profit:
        dist = abs(entry - stop_loss)
        rr_mult = safe_float(rr, 2.0)
        if rr_mult <= 0:
            rr_mult = 2.0
        take_profit = entry + dist * rr_mult if direction.upper() == "LONG" else entry - dist * rr_mult
    balance = get_usdt_free_balance(ex)
    lev = int(s["leverage"])
    amount = calc_amount_from_risk(entry, stop_loss, balance, float(s["risk_percent"]), lev)
    amount = float(ex.amount_to_precision(ms, amount))
    warnings = set_isolated_and_leverage(ex, ms, lev)
    entry_order = ex.create_order(ms, "market", side_for(direction), amount, None, {"marginMode": "isolated"})
    sl_order = place_protective_order(ex, ms, direction, amount, stop_loss, "sl")
    tp_order = place_protective_order(ex, ms, direction, amount, take_profit, "tp")
    pos = {"uid": str(uid), "symbol": normalize_symbol(symbol), "market_symbol": ms, "exchange": s["exchange"], "direction": direction.upper(), "entry": round(entry,8), "amount": amount, "initial_stop_loss": round(stop_loss,8), "stop_loss": round(stop_loss,8), "take_profit": round(take_profit,8), "rr": safe_float(rr, 2.0), "leverage": lev, "margin_mode": "isolated", "status": "real_opened", "remaining_percent": 100, "opened_ts": time.time(), "breakeven_enabled": bool(s.get("breakeven_enabled", False)), "breakeven_r": safe_float(s.get("breakeven_r"), 1), "trailing_enabled": bool(s.get("trailing_enabled", False)), "trailing_r": safe_float(s.get("trailing_r"), 1.5), "partial_tp_enabled": bool(s.get("partial_tp_enabled", False)), "partial_tp_r": safe_float(s.get("partial_tp_r"), 1), "partial_tp_percent": safe_float(s.get("partial_tp_percent"), 50), "warnings": warnings, "entry_order": str(entry_order)[:500], "sl_order": str(sl_order)[:500], "tp_order": str(tp_order)[:500]}
    ps = _positions(uid); ps.append(pos); _save_positions(uid, ps)
    return pos




def is_duplicate_open_trade(uid: str, symbol: str, direction: str) -> Tuple[bool, str]:
    """Return True when the same symbol + direction is already tracked as open."""
    norm_symbol = normalize_symbol(symbol)
    norm_direction = str(direction or "").upper()
    open_statuses = {"real_opened", "open", "opened", "live", "running", "active"}
    for pos in _positions(uid):
        if str(pos.get("symbol", "")).upper() != norm_symbol.upper():
            continue
        if str(pos.get("direction", "")).upper() != norm_direction:
            continue
        status = str(pos.get("status", "real_opened")).lower()
        closed = bool(pos.get("closed")) or bool(pos.get("closed_ts")) or status in {"closed", "done", "cancelled", "canceled"}
        if not closed and (status in open_statuses or status.startswith("real_")):
            return True, f"🛡 Duble protection ON: {norm_symbol} {norm_direction} already open. Duplicate blocked."
    return False, ""

def rr_mode_label(rr: Any) -> str:
    rr_val = safe_float(rr, 2.0)
    if rr_val >= 4:
        return "1:4 TREND + RS/BTC + SUPER VOLUME"
    if rr_val >= 3:
        return "1:3 TREND"
    return "1:2 STANDARD"

def format_real_opened_message(pos: Dict[str, Any]) -> str:
    trailing_state = "ENABLED" if bool(pos.get("trailing_enabled")) else "DISABLED"
    return (
        "✅ REAL OPENED\n"
        f"Symbol: {pos.get('symbol')}\n"
        f"Direction: {pos.get('direction')}\n"
        f"Entry: {pos.get('entry')}\n"
        f"SL: {pos.get('stop_loss')}\n"
        f"TP: {pos.get('take_profit')}\n"
        f"Leverage: x{pos.get('leverage')}\n"
        f"Amount: {pos.get('amount')}\n"
        f"RR Mode: {rr_mode_label(pos.get('rr'))}\n"
        f"Trailing: {trailing_state}"
    )

async def positions_text(uid: str) -> str:
    ps = _positions(uid)
    if not ps:
        return "📊 Positions\n\nНет позиций."
    lines = ["📊 Positions"]
    for i,p in enumerate(ps,1):
        lines.append(f"\n{i}. {p.get('symbol')} {p.get('direction')} | {p.get('exchange','').upper()}\nStatus: {p.get('status')}\nEntry: {p.get('entry')} | Amount: {p.get('amount')}\nSL: {p.get('stop_loss')} | TP: {p.get('take_profit')}\nLev: x{p.get('leverage')} | Margin: {p.get('margin_mode')}")
    return "\n".join(lines)[:3900]

async def sync_positions_for_user(app: Optional[Application], uid: str) -> str:
    s = get_settings(uid)
    if not s.get("position_sync_enabled"):
        return "Position Sync OFF"
    try:
        ex = get_private_exchange(uid)
        raw = ex.fetch_positions() if hasattr(ex, "fetch_positions") else []
        active = [p for p in raw or [] if abs(safe_float(p.get("contracts") or p.get("info", {}).get("positionAmt") or p.get("info", {}).get("holdVol"))) > 0]
        local = _positions(uid)
        data = load_json(POSITIONS_FILE, {})
        data[f"{uid}_exchange_snapshot"] = {"ts": time.time(), "exchange": s["exchange"], "active_count": len(active), "positions": [str(x)[:1000] for x in active[:20]]}
        save_json(POSITIONS_FILE, data)
        msg = f"🔁 Position Sync completed\nExchange active positions: {len(active)}\nLocal tracked positions: {len(local)}"
        if app and len(active) != len(local):
            await app.bot.send_message(chat_id=int(uid), text=msg + "\n⚠️ Desync possible. Проверь /positions.")
        return msg
    except Exception as e:
        return f"Position Sync error: {str(e)[:500]}"

def get_status_text(uid: str) -> str:
    s = get_settings(uid)
    top_status = "Only BTC/ETH" if s.get("market_universe") == "btc_eth" else f"Top-{s.get('scanner_size', 100)}"
    return f"""📋 Bot Status v{BOT_VERSION}

🏦 Exchange: {s['exchange'].upper()}
🤖 Provider: {s['ai_provider']}
🧠 Model: {get_active_model(s)}
⚙️ Reasoning: {s.get('reasoning_level')}

🔥 Scanner: {top_status}
🔥 Selected Top Signal: Top-{s.get('scanner_size', 100)}
🔄 Auto Scanner Top: {auto_scanner_label(s.get('auto_scanner_interval'))}
🧠 Structural Layers: {structural_mode_label(s.get('structural_mode'))}
🚀 Extended TP Auto: {'ON' if s.get('extended_tp_enabled') else 'OFF'}
🎯 Min Score: {s.get('min_score')}%
📋 TopLimit: {top_limit_label(s)}
🕒 Timeframe: {timeframe_label(s.get('timeframe_mode'))}
🌏 Asia/America volatility: {'ON' if s.get('session_filter') else 'OFF'}

🤖 Trading Mode: {s.get('trading_mode').upper()}
🚀 Trading Enabled: {'ON' if s.get('trading_enabled') else 'OFF'}
💸 Real Execution: {'ON' if s.get('real_execution_enabled') else 'OFF'}
🚨 STOP ALL: {'ON' if s.get('stop_all_enabled') else 'OFF'}
🔁 Position Sync: {'ON' if s.get('position_sync_enabled') else 'OFF'}
📈 Live Trade Manager: {'ON' if s.get('live_trade_manager_enabled') else 'OFF'}
🧠 AI Check: {'ON' if s.get('strict_ai_mode') else 'OFF'}
🧠 AI Auto Prompt: {'ON' if s.get('ai_auto_p', True) else 'OFF'}
🏦 Margin Mode: ISOLATED only

📉 Risk: {s.get('risk_percent')}%
📈 Leverage: x{s.get('leverage')}
"""

def help_text() -> str:
    return f"""🤖 Trading Bot v{BOT_VERSION}

/menu
Открыть inline-меню.

/status
Полный статус бота: биржа, модель, scanner, structural, Live TM, STOP ALL.

/ping
Быстрый ping без ожидания AI.

/ping_ai
Проверка ответа AI модели.

/start
Запуск бота и меню.

/help
Список команд.

AI:
/provider_ollama
/provider_openai
/openai_on
/openai_off
/setopenai OPENAI_API_KEY
/testai
/ai_on
/ai_off
Включить/выключить проверку монет ИИ.

Trading:
/trading_on
/trading_off
Включить/выключить торговый режим.
Кнопка Trading AUTO включает Trading ON + AI Auto ON. MANUAL выключает авто-торговлю.

/setapi mexc|bingx|binance API_KEY API_SECRET
Сохранить API ключ выбранной биржи.

/real_on
/real_off
Включить/выключить реальные ордера на бирже.

/duble on
/duble off
Защита от дублей: ON не открывает такую же сделку, если она уже открыта; OFF разрешает повторное открытие.

Live Trade Manager:
/livetrademanager_on
/livetrademanager_off
/livetrademanager_status

STOP ALL:
/stopall_on
/stopall_off
STOP ALL выключает Auto Scanner, Trading, Real Execution, Live TM, Position Sync и пытается закрыть tracked positions через reduceOnly.

Scanner:
/top50
/top100
/top200
/minscore 80
/toplimit 5
/toplimit 10
/toplimit all

Кнопка 📋 TopLimit в главном меню:
- По умолчанию TopLimit 5
- TopLimit 5 — DeepSeek проверяет до 5 лучших сетапов
- TopLimit 10 — DeepSeek проверяет до 10 лучших сетапов
- TopLimit ALL — DeepSeek проверяет все сетапы, прошедшие фильтры

Auto Scanner:
Кнопка 🔄 Auto Scanner в меню.
Интервалы: 15m / 60m / 4h / 12h / 24h / OFF.
Если Auto Scanner включен — сигналы автоматически включены. Если Auto Scanner OFF — сигналы не отправляются.
/autoscanner
/autoscanner_off

Timeframes:
15m
15m + 1h
1h + 4h
Multi = 15m + 1h + 4h + 1d

Structural:
/structural
OFF
Trendline Layer
Trendline + RS/BTC
Trendline + RS/BTC + Super Volume
Structural Only

Hybrid Trendline:
Structure breakout + 3-touch trendline bonus.
3-touch не заменяет structure breakout, а усиливает score/reasons.

RR / TP logic:
Обычный сигнал -> 1:2
Просто Trendline -> 1:2.5
Trendline + RS/BTC -> 1:3
Trendline + RS/BTC + Super Volume -> 1:4
Structural Only -> 1:4 только если все 3 слоя подтверждены

Trade Management:
BE at +1R
TP1 partial close 50%
Trailing after TP1
TP2 runner target

Positions:
/positions
/positionsync_on
/positionsync_off
/positionsync_now

Risk:
/risk 1
/leverage 5
/aiauto_on
/aiauto_off

Trade Management toggles:
/breakeven_on
/breakeven_off
/trailing_on
/trailing_off
/partialtp_on
/partialtp_off
/partialtp 50 1

⚠️ WARNING:
Test Real Execution and Live TM only with small positions first.

Version: {BOT_VERSION}
"""

async def signal_for_symbol(uid: str, symbol: str, timeframe: Optional[str] = None, context: Optional[ContextTypes.DEFAULT_TYPE] = None, chat_id: Optional[int] = None) -> str:
    s = get_settings(uid)
    if not allowed_by_market_universe(symbol, s):
        return "🟠 Включен режим Only BTC/ETH. Доступны только BTCUSDT и ETHUSDT."
    session_ok, session_msg = session_filter_allows_trading(s)
    if not session_ok:
        return "🌏 Asia/America volatility: ON\n⛔ Сигнал заблокирован фильтром сессий.\n" + session_msg

    primary_tf, _ = timeframe_pair(s, timeframe)
    df = add_indicators(fetch_ohlcv_for_symbol(s["exchange"], symbol, primary_tf, 180))
    market = score_market_multi(s["exchange"], symbol, s, timeframe)
    market = apply_structural_layers(s["exchange"], symbol, df, market, s)
    market = apply_session_volatility_filter(s, market)

    tf_display = timeframe if timeframe else timeframe_label(s.get("timeframe_mode"))
    levels = calculate_trade_levels(normalize_symbol(symbol), market, df, s)

    # WAIT signals do not call AI. WAIT means no trade.
    if str(market.get("direction", "WAIT")).upper() == "WAIT":
        ai_verdict = {
            "verdict": "REJECTED",
            "confidence": safe_float(market.get("score"), 0),
            "reason": "WAIT / no trade setup. AI skipped."
        }
        return format_strict_signal(normalize_symbol(symbol), tf_display, s, market, levels, ai_verdict)

    prompt = build_signal_prompt(normalize_symbol(symbol), tf_display, market, s)
    ai = await call_ai(uid, prompt, context, chat_id, options=AI_APPROVAL_OPTIONS)
    validate_ai_response_or_raise(s, ai)

    ai_verdict = extract_ai_verdict(ai, market)

    # Strict AI: if rejected, no trade levels are actionable.
    return format_strict_signal(normalize_symbol(symbol), tf_display, s, market, levels, ai_verdict)



async def _scan_one_symbol(exchange: str, sym: str, primary_tf: str, settings_snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Run blocking exchange/TA work outside the Telegram event loop."""
    def _work():
        df = add_indicators(fetch_ohlcv_for_symbol(exchange, sym, primary_tf, 180))
        # Do not fetch the same primary timeframe twice. The old path loaded
        # markets + OHLCV again inside score_market_multi for every symbol.
        mkt = score_market_multi_fast(exchange, sym, settings_snapshot, df)
        mkt = apply_structural_layers(exchange, sym, df, mkt, settings_snapshot)
        return apply_session_volatility_filter(settings_snapshot, mkt)
    return await asyncio.to_thread(_work)

async def _run_scan_task(uid: str, n: int, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    try:
        result = await run_top_scan(uid, n, context, chat_id)
        s_now = get_settings(uid)
        buttons = []
        if LAST_AI_CONFIRMED.get(int(uid)):
            if s_now.get("trading_mode") != "auto":
                buttons.append([InlineKeyboardButton("✅ OPEN REAL TRADE", callback_data="open_confirmed")])
                buttons.append([InlineKeyboardButton("❌ CANCEL", callback_data="cancel_confirmed")])
        elif LAST_SCAN_RESULTS.get(int(uid)) and not s_now.get("ai_auto_p", True) and s_now.get("strict_ai_mode", True):
            buttons.append([InlineKeyboardButton("🧠 AI Confirm", callback_data="ai_confirm")])
        buttons.append([InlineKeyboardButton("⬅️ Главное меню", callback_data="back:main")])
        await context.bot.send_message(
            chat_id=chat_id,
            text=result[:3900],
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    except asyncio.CancelledError:
        raise
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Scan error: {str(e)[:800]}")
    finally:
        USER_SCAN_TASKS.pop(uid, None)
        try:
            await refresh_menu_bottom(context, chat_id, uid)
        except Exception:
            pass

async def run_top_scan(uid: str, n: int, context: Optional[ContextTypes.DEFAULT_TYPE] = None, chat_id: Optional[int] = None) -> str:
    """
    Top scanner:
    - scans Top-N market
    - hard-skips WAIT before AI
    - sends only LONG/SHORT candidates to AI
    """
    set_setting(uid, "scanner_size", n)
    s = get_settings(uid)
    # Asia/America is a soft opening-volatility filter now: it never blocks scan.
    session_ok, session_msg = session_filter_allows_trading(s)
    symbols = ["BTCUSDT", "ETHUSDT"] if s.get("market_universe") == "btc_eth" else await asyncio.to_thread(get_top_symbols, s["exchange"], n)

    results = []
    skipped_wait = 0
    skipped_score = 0
    errors = 0

    total = max(len(symbols), 1)
    progress_sent = set()

    progress_message_id = None

    async def send_scan_progress(percent: int):
        nonlocal progress_message_id
        if context is not None and chat_id is not None and percent not in progress_sent:
            progress_sent.add(percent)
            text = f"🔎 Отсканировал {percent}%..."
            try:
                if progress_message_id:
                    await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_message_id, text=text)
                else:
                    msg = await context.bot.send_message(chat_id=chat_id, text=text)
                    progress_message_id = msg.message_id
            except Exception:
                try:
                    msg = await context.bot.send_message(chat_id=chat_id, text=text)
                    progress_message_id = msg.message_id
                except Exception:
                    pass

    async def send_scan_coin(sym: str):
        # No per-coin spam in chat; only 10/50/100 progress updates.
        return

    await send_scan_progress(10)

    sem = asyncio.Semaphore(max(1, SCAN_MAX_CONCURRENT))

    async def scan_symbol(sym: str):
        async with sem:
            # Small pause keeps requests from arriving as one hard burst and lowers rate-limit risk.
            if SCAN_REQUEST_PAUSE > 0:
                await asyncio.sleep(SCAN_REQUEST_PAUSE)
            # Use a fresh settings snapshot for each coin, so button changes made during scan
            # are respected, while the blocking exchange work stays outside the event loop.
            coin_settings = get_settings(uid)
            primary_tf, _ = timeframe_pair(coin_settings)
            mkt = await _scan_one_symbol(coin_settings["exchange"], sym, primary_tf, dict(coin_settings))
            return sym, mkt, coin_settings

    tasks = [asyncio.create_task(scan_symbol(sym)) for sym in symbols]
    completed = 0
    try:
        for task in asyncio.as_completed(tasks):
            completed += 1
            try:
                sym, mkt, coin_settings = await task

                # Critical rule: Top scanner never sends WAIT to AI.
                if str(mkt.get("direction", "WAIT")).upper() == "WAIT":
                    skipped_wait += 1
                else:
                    score_ok = float(mkt.get("score", 0)) >= float(coin_settings.get("min_score", 80))
                    structural_ok = coin_settings.get("structural_mode") == "structural_only"
                    if score_ok or structural_ok:
                        results.append({"symbol": sym, **mkt})
                    else:
                        skipped_score += 1
            except Exception:
                errors += 1

            current_percent = int((completed / total) * 100)
            if current_percent >= 50:
                await send_scan_progress(50)
            await asyncio.sleep(0)
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        raise

    await send_scan_progress(100)

    # Keep only valid LONG/SHORT candidates, even if some old code mutated results.
    results = [r for r in results if str(r.get("direction", "WAIT")).upper() in ["LONG", "SHORT"]]
    results.sort(key=lambda x: x.get("score", 0), reverse=True)

    # Re-read settings at the end of the scan. A Top-N scan can take long enough
    # that the user changes TopLimit while it is running; using the initial snapshot
    # here made the final list sometimes stay capped at 5 even after selecting 10.
    final_settings = get_settings(uid)
    limit = selected_top_limit(final_settings)
    if limit is not None:
        results = results[:limit]

    LAST_SCAN_RESULTS[int(uid)] = results

    summary_header = (
        f"🔥 Top-{n} Signal | {final_settings['exchange'].upper()}\n"
        f"🎯 MinScore: {final_settings['min_score']}%\n"
        f"📋 TopLimit: {top_limit_label(final_settings)}\n"
        f"🧠 Structural: {structural_mode_label(final_settings.get('structural_mode'))}\n"
        f"⏳ WAIT skipped: {skipped_wait}\n"
    )

    if not results:
        return (
            summary_header
            + "\n✅ Скан завершён.\n"
            + "Нет LONG/SHORT монет, прошедших фильтры.\n"
            + f"Score skipped: {skipped_score}\n"
            + f"Errors: {errors}"
        )[:3900]

    lines = [summary_header]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['symbol']} — {r['direction']} | Scanner Score {r['score']}% | {'MTF ✅' if r.get('mtf_confirmed', True) else 'MTF ❌'}")

    ai_result = ""
    auto_exec_text = ""
    if s.get("strict_ai_mode", True):
        if s.get("ai_auto_p", True):
            if context is not None and chat_id is not None:
                try:
                    await update_work_message(context, chat_id, uid, f"🧠 Отправляю LONG/SHORT - {len(results)} кандидатов в AI...")
                except Exception:
                    pass

            try:
                ai_result = await ai_confirm(uid)
            except Exception as e:
                ai_result = f"❌ AI confirm error: {str(e)[:800]}"

            try:
                if get_settings(uid).get("trading_mode") == "auto":
                    auto_exec_text = await execute_confirmed_from_auto(uid)
            except Exception as e:
                auto_exec_text = f"\n❌ Auto execution error: {str(e)[:500]}"
        else:
            LAST_AI_CONFIRMED[int(uid)] = []
            ai_result = "🧠 AI Auto Prompt: OFF — найденные сделки ждут ручной проверки. Нажми 🧠 AI Confirm."
    else:
        LAST_AI_CONFIRMED[int(uid)] = []
        ai_result = "🧠 AI CHECK: OFF — монеты не отправлялись в AI."

    final = "\n".join(lines)
    if ai_result:
        final += "\n\n" + ai_result
    if auto_exec_text:
        final += "\n\n" + auto_exec_text

    return final[:3900]

def _extract_json_value(raw: str) -> Any:
    """Extract valid JSON from an LLM response without showing raw model text to chat."""
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I).strip()
    text = re.sub(r"\s*```$", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    for pattern in (r"\[.*\]", r"\{.*\}"):
        m = re.search(pattern, text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                continue
    return []


def _normalize_ai_approval_items(value: Any) -> List[Dict[str, Any]]:
    if isinstance(value, dict):
        if isinstance(value.get("approved"), list):
            value = value.get("approved")
        elif isinstance(value.get("trades"), list):
            value = value.get("trades")
        elif isinstance(value.get("setups"), list):
            value = value.get("setups")
        else:
            value = [value]
    if not isinstance(value, list):
        return []
    out: List[Dict[str, Any]] = []
    source_candidates = LAST_SCAN_RESULTS.get(int(CURRENT_AI_UID or 0), []) if 'CURRENT_AI_UID' in globals() else []
    by_symbol = {normalize_symbol(str(c.get("symbol", ""))): c for c in source_candidates if isinstance(c, dict)}
    for item in value:
        if not isinstance(item, dict):
            continue
        verdict = str(item.get("verdict", item.get("decision", "APPROVED"))).upper()
        approved = item.get("approved", True)
        if verdict in ["REJECT", "REJECTED", "WAIT", "NO_TRADE", "NO TRADE"] or approved is False:
            continue
        symbol = normalize_symbol(str(item.get("symbol", "")))
        direction = str(item.get("direction", item.get("side", ""))).upper()
        if not symbol or direction not in ["LONG", "SHORT"]:
            continue
        source = by_symbol.get(symbol, {})
        try:
            confidence = int(float(item.get("confidence", item.get("success_probability", 0)) or 0))
        except Exception:
            confidence = 0
        try:
            success_probability = int(float(item.get("success_probability", confidence) or confidence))
        except Exception:
            success_probability = confidence
        try:
            scanner_score = float(source.get("score", item.get("scanner_score", item.get("score", 0))) or 0)
        except Exception:
            scanner_score = 0
        dynamic_rr, rr_profile = infer_dynamic_rr(source)
        out.append({
            "symbol": symbol,
            "direction": direction,
            "scanner_score": round(scanner_score, 1),
            "confidence": max(0, min(100, confidence)),
            "success_probability": max(0, min(100, success_probability)),
            "reason": str(item.get("reason", item.get("why", "AI approved setup")))[:220],
            "dynamic_rr": dynamic_rr,
            "rr_profile": rr_profile,
        })
    return out


def _format_ai_confirmed(confirmed: List[Dict[str, Any]]) -> str:
    lines = ["✅ AI подтвердил сделки:"]
    for i, x in enumerate(confirmed, 1):
        extra = "\n🚀 Extended TP: ON" if x.get("extended_tp_mode") else ""
        lines.append(
            f"\n{i}. 🪙 {normalize_symbol(x.get('symbol', ''))}\n"
            f"📈 Direction: {x.get('direction')}\n"
            f"🎯 Scanner Score: {x.get('scanner_score', '-')}%\n"
            f"🧠 AI Confidence: {x.get('confidence', '-')}%\n"
            f"📊 Вероятность отработки: {x.get('success_probability', x.get('confidence', '-'))}%\n"
            f"📌 Причина: {x.get('reason', 'AI approved setup')}"
            f"{extra}"
        )
    return "\n".join(lines)


async def ai_confirm(uid: str) -> str:
    s = get_settings(uid)
    active, msg = is_cooldown_active(uid)
    if active:
        LAST_AI_CONFIRMED[int(uid)] = []
        return msg + "\nAI Confirm заблокирован."
    candidates = LAST_SCAN_RESULTS.get(int(uid), [])
    candidates = [c for c in candidates if str(c.get("direction", "WAIT")).upper() in ["LONG", "SHORT"]]
    approval_limit = selected_top_limit(s, AI_APPROVAL_TOP_LIMIT)
    candidates = sorted(candidates, key=lambda x: float(x.get("score", 0) or 0), reverse=True)
    if approval_limit is not None:
        candidates = candidates[:approval_limit]
    if not candidates:
        LAST_AI_CONFIRMED[int(uid)] = []
        return "Нет LONG/SHORT кандидатов. WAIT не отправляется в AI."
    prompt = f"""Ты STRICT JSON AI approval engine для crypto trading.

Твоя задача: проверить только LONG/SHORT кандидатов и вернуть ТОЛЬКО валидный JSON.
Запрещено писать пояснения, markdown, ```json, текст до/после JSON.

Формат ответа строго:
[
  {{"symbol":"BTCUSDT","direction":"LONG","scanner_score":88,"confidence":85,"success_probability":85,"reason":"short reason"}}
]

Если нет подтверждённых сделок, верни строго пустой массив:
[]

Правила одобрения:
- НЕ одобряй сделку только из-за высокого score. Score — только предварительный фильтр.
- Проверяй market structure, MTF confirmation, volume/RVOL, reasons, momentum, volatility и risk/reward.
- REJECT, если структура слабая, рынок chop/range, MTF конфликтует, volume слабый, breakout сомнительный или риск/прибыль плохие.
- APPROVE только если направление LONG/SHORT подтверждается несколькими факторами одновременно.
- symbol должен быть из candidates.
- direction только LONG или SHORT.
- scanner_score должен быть реальным score кандидата из Candidates JSON, не MinScore.
- confidence число 0-100, отражает качество сетапа после проверки, а не просто score.
- success_probability число 0-100: вероятность отработки сделки по оценке AI.
- reason одна короткая причина до 160 символов: укажи главный структурный/MTF/volume/RR фактор.
- Не возвращай WAIT. Если сетап не подходит — просто не включай его в JSON.
- Не выдумывай новые монеты.

TopLimit сейчас: {top_limit_label(s)}.
Candidates JSON:
{json.dumps(candidates, ensure_ascii=False, default=str)}
"""
    raw = await call_ai(uid, prompt, options=AI_APPROVAL_OPTIONS)
    validate_ai_response_or_raise(s, raw)
    global CURRENT_AI_UID
    CURRENT_AI_UID = uid
    confirmed = _normalize_ai_approval_items(_extract_json_value(raw))
    CURRENT_AI_UID = None
    confirmed = confirmed[:int(s.get("max_trades", 3))]
    for x in confirmed:
        conf = float(x.get("confidence", 0) or 0)
        x["extended_tp_mode"] = bool(s.get("extended_tp_enabled") and s.get("structural_mode") == "trendline_rs_volume" and conf >= float(s.get("extended_tp_min_confidence", 80)))
        if x["extended_tp_mode"]:
            x["tp_profile"] = "extended"
            x["extended_tp_rr"] = float(s.get("extended_tp_rr", 4))
    LAST_AI_CONFIRMED[int(uid)] = confirmed
    if not confirmed:
        return "🧠 AI не подтвердил сделки из списка.\nSTRICT JSON: подтверждённых LONG/SHORT сделок нет."
    return _format_ai_confirmed(confirmed)

async def execute_confirmed_from_auto(uid: str) -> str:
    if stop_all_active(uid):
        return "🚨 STOP ALL is ON. Auto execution blocked."
    confirmed = LAST_AI_CONFIRMED.get(int(uid), [])
    if not confirmed:
        return "STRICT AI MODE: нет AI-approved сделок. Auto execution blocked."
    s = get_settings(uid)
    if s.get("trading_mode") != "auto" or not s.get("trading_enabled") or not s.get("ai_auto"):
        return "Auto Scanner нашел сделки, но Auto/Trading/AI Auto не включены."
    session_ok, session_msg = session_filter_allows_trading(s)
    if not session_ok:
        return "🌏 Asia/America volatility: ON\n⛔ Auto execution blocked by session filter.\n" + session_msg
    opened, errors = [], []
    for x in confirmed[:int(s.get("max_trades",3))]:
        sym = normalize_symbol(x.get("symbol",""))
        direction = x.get("direction","LONG").upper()
        try:
            if s.get("real_execution_enabled"):
                pos = await execute_real_trade(uid, sym, direction, x.get("stop_loss"), x.get("take_profit"), x.get("dynamic_rr", 2.0))
                opened.append(format_real_opened_message(pos) + ("\nExtended TP: ON" if x.get("extended_tp_mode") else ""))
            else:
                opened.append(f"PAPER {sym} {direction} — real execution OFF" + (" | Extended TP" if x.get("extended_tp_mode") else ""))
        except Exception as e:
            errors.append(f"{sym}: {str(e)[:180]}")
    return ("\n".join(opened) if opened else "") + (("\nОшибки:\n" + "\n".join(errors)) if errors else "")

async def run_auto_scanner_for_user(app: Application, uid: str):
    s = get_settings(uid)
    if s.get("auto_scanner_interval") == "off" or stop_all_active(uid):
        return
    n = int(s.get("scanner_size", 100))
    scan = await run_top_scan(uid, n, app, int(uid))
    await app.bot.send_message(chat_id=int(uid), text=f"🔄 Auto Scanner Top completed\n{scan[:3600]}"[:3900])

async def auto_scanner_loop(app: Application):
    while True:
        await asyncio.sleep(30)
        now = time.time()
        for uid, s in load_json(SETTINGS_FILE, {}).items():
            if s.get("stop_all_enabled"):
                continue
            sec = auto_scanner_seconds(s.get("auto_scanner_interval", "off"))
            if sec <= 0:
                continue
            if now - float(s.get("auto_scanner_last_run",0) or 0) >= sec:
                set_setting(uid, "auto_scanner_last_run", int(now))
                app.create_task(run_auto_scanner_for_user(app, uid))

async def position_sync_loop(app: Application):
    last = {}
    while True:
        await asyncio.sleep(30)
        now = time.time()
        for uid, s in load_json(SETTINGS_FILE, {}).items():
            if not s.get("position_sync_enabled"):
                continue
            interval = int(s.get("position_sync_interval", 300) or 300)
            if now - last.get(uid, 0) >= interval:
                last[uid] = now
                await sync_positions_for_user(app, uid)

def unload_ollama_model(model: str) -> bool:
    """Ask Ollama to unload a model from memory."""
    try:
        r = requests.post(
            f"{OLLAMA_HOST}/api/generate",
            json={"model": model, "prompt": "", "stream": False, "keep_alive": 0},
            timeout=20,
        )
        return r.status_code < 400
    except Exception:
        return False

async def unload_idle_models():
    global LAST_OLLAMA_ACTIVITY
    while True:
        await asyncio.sleep(60)
        if OLLAMA_IDLE_UNLOAD_SECONDS <= 0:
            continue
        if LAST_OLLAMA_ACTIVITY <= 0:
            continue
        if time.time() - LAST_OLLAMA_ACTIVITY < OLLAMA_IDLE_UNLOAD_SECONDS:
            continue

        settings = load_json(SETTINGS_FILE, {})
        models = {DEFAULT_MODEL, *OLLAMA_MODELS}
        for s in settings.values():
            if isinstance(s, dict) and s.get("ai_provider") == "ollama":
                models.add(s.get("ollama_model", DEFAULT_MODEL))

        for model in {m for m in models if m}:
            await asyncio.to_thread(unload_ollama_model, model)
        LAST_OLLAMA_ACTIVITY = 0.0

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_inline_menu_message(update, context, f"🤖 Trading Bot v{BOT_VERSION}\n\nВыбери действие в inline-меню ниже.")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(help_text())

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id(update)
    await update.message.reply_text(get_status_text(uid), reply_markup=main_menu(get_settings(uid)))

async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    started = time.perf_counter()
    uid = user_id(update)
    s = get_settings(uid)

    exchange_health = await check_exchange_api(uid)
    latency_ms = round((time.perf_counter() - started) * 1000, 2)

    await update.message.reply_text(
        f"📡 Fast Ping: {latency_ms} ms\n"
        f"⏱ Время работы: {format_uptime(int(time.time() - START_TIME))}\n"
        f"🧠 Память: {memory_usage_text()}\n"
        f"🤖 Provider: {s.get('ai_provider')}\n"
        f"🧠 Модель ИИ: {get_active_model(s)}\n"
        f"🏦 API биржи {s.get('exchange', '').upper()}: {exchange_health}\n"
        f"📦 Version: {BOT_VERSION}\n\n"
        f"Для полной AI проверки нажмите: 🧠 Ping AI",
        reply_markup=main_menu(get_settings(uid))
    )

async def ping_ai_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id(update)
    s = get_settings(uid)

    msg = await update.message.reply_text("🧠 Проверка AI модели...")

    try:
        started = time.perf_counter()
        ai_health = await check_ai_health(uid, context, update.effective_chat.id)
        latency_ms = round((time.perf_counter() - started) * 1000, 2)

        await msg.edit_text(
            f"🧠 Ping AI\n"
            f"🤖 Provider: {s.get('ai_provider')}\n"
            f"🧠 Модель: {get_active_model(s)}\n"
            f"⚡ Ответ модели: {latency_ms} ms\n"
            f"🧪 AI Status: {ai_health}"
        )
    except Exception as e:
        await msg.edit_text(f"❌ Ping AI error: {str(e)[:300]}")

async def signal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id(update)
    if not context.args:
        await update.message.reply_text("Пример: /signal BTC", reply_markup=main_menu(get_settings(uid)))
        return
    try:
        await update.message.reply_text(
            await signal_for_symbol(uid, context.args[0], context=context, chat_id=update.effective_chat.id)
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Signal error: {str(e)[:1000]}")


async def ping_cmd_from_callback(context: ContextTypes.DEFAULT_TYPE, chat_id: int, uid: str):
    started = time.perf_counter()
    s = get_settings(uid)
    exchange_health = await check_exchange_api(uid)
    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    await send_below_buttons(
        context,
        chat_id,
        f"📡 Fast Ping: {latency_ms} ms\n"
        f"⏱ Время работы: {format_uptime(int(time.time() - START_TIME))}\n"
        f"🧠 Память: {memory_usage_text()}\n"
        f"🤖 Provider: {s.get('ai_provider')}\n"
        f"🧠 Модель ИИ: {get_active_model(s)}\n"
        f"🏦 API биржи {s.get('exchange', '').upper()}: {exchange_health}\n"
        f"📦 Version: {BOT_VERSION}",
        uid
    )




async def inline_button_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q is None:
        return
    await q.answer()

    uid = str(q.from_user.id)
    chat_id = q.message.chat_id
    data = q.data or ""
    s = get_settings(uid)

    async def say(msg: str, reply_markup=None, keep_menu_bottom: bool = True):
        # Always rebuild markup from fresh settings and edit the exact message
        # where the user pressed a button. This prevents stale submenu buttons
        # with old checkmarks/cached values from staying visible.
        fresh_markup = reply_markup if reply_markup is not None else main_menu(get_settings(uid))
        text = (await _resolve_message_text(msg))[:3900]
        try:
            await q.edit_message_text(text=text, reply_markup=fresh_markup)
            try:
                set_work_message_id(uid, q.message.message_id)
            except Exception:
                pass
            return
        except Exception as e:
            # Telegram raises "message is not modified" when text/markup are identical;
            # in that case the visible buttons are already current.
            if "not modified" in str(e).lower():
                return
        await update_work_message(context, chat_id, uid, text, reply_markup=fresh_markup)


    try:
        if data == "back:main":
            await say(
                f"🤖 Trading Bot v{BOT_VERSION}\n\nInline menu активировано.",
                main_menu(get_settings(uid))
            )
        elif data == "mode:signal":
            await say("ℹ️ Кнопка Signal убрана. Сигналы отправляются автоматически, когда Auto Scanner включен.")
        elif data == "mode:chat":
            set_setting(uid, "mode", "chat")
            await say("✅ AI Chat Mode")

        elif data == "menu:provider":
            await say("AI Provider:", provider_menu(s), keep_menu_bottom=False)
        elif data == "menu:model":
            await say("Model:", model_menu(s), keep_menu_bottom=False)
        elif data == "menu:reasoning":
            await say("Reasoning:", reasoning_menu(s), keep_menu_bottom=False)
        elif data == "menu:exchange":
            await say("Exchange:", exchange_menu(s), keep_menu_bottom=False)
        elif data == "menu:tradingmode":
            await say("Trading Mode:", tradingmode_menu(s), keep_menu_bottom=False)
        elif data == "menu:timeframe":
            await say("Timeframe:", timeframe_menu(s), keep_menu_bottom=False)
        elif data == "menu:autoscanner":
            await say("Auto Scanner Top:", auto_scanner_menu(s), keep_menu_bottom=False)
        elif data == "menu:toplimit":
            await say("📋 TopLimit — сколько лучших сетапов отправлять на AI approval:", top_limit_menu(s), keep_menu_bottom=False)
        elif data == "menu:structural":
            await say("Structural Layers:", structural_layers_menu(s), keep_menu_bottom=False)
        elif data == "menu:trademgmt":
            await say("Trade Management:", trade_mgmt_menu(s), keep_menu_bottom=False)

        elif data.startswith("provider:"):
            val = data.split(":", 1)[1]
            set_ai_provider(uid, val)
            await say(f"✅ Provider: {val}")
        elif data.startswith("openai_model:"):
            val = data.split(":", 1)[1]
            set_openai_model(uid, val)
            await say(f"✅ OpenAI model: {val}")
        elif data.startswith("ollama_model:"):
            model = data.split(":", 1)[1]
            set_ollama_model(uid, model)
            await say(f"🧠 Model selected: {model}", keep_menu_bottom=False)
            try:
                await ensure_ollama_model(model, context, chat_id, uid)
                await say(f"✅ Модель {model} готова к работе.")
            except Exception as e:
                await say(f"❌ Model load error: {str(e)[:500]}")
        elif data.startswith("reasoning:"):
            val = data.split(":", 1)[1]
            set_setting(uid, "reasoning_level", val)
            await say(f"✅ Reasoning: {val}")
        elif data.startswith("exchange:"):
            val = data.split(":", 1)[1].lower().strip()
            if val not in SUPPORTED_EXCHANGES:
                await say("❌ Unsupported exchange. Use MEXC/BingX/Binance.")
            else:
                set_setting(uid, "exchange", val)
                await say(f"✅ Exchange: {val.upper()}")
        elif data.startswith("tradingmode:"):
            val = data.split(":", 1)[1]
            await say(apply_trading_mode(uid, val))
        elif data.startswith("timeframe:"):
            val = data.split(":", 1)[1]
            set_setting(uid, "timeframe_mode", val)
            await say(f"✅ Timeframe: {timeframe_label(val)}")
        elif data.startswith("autoscanner:"):
            val = data.split(":", 1)[1]
            updates = {"auto_scanner_interval": val, "auto_scanner_last_run": 0}
            if val != "off":
                updates["mode"] = "signal"
            set_settings(uid, updates)
            if val != "off":
                await say(f"✅ Auto Scanner: {auto_scanner_label(val)}\n📈 Signals: ON")
            else:
                await say(f"✅ Auto Scanner: {auto_scanner_label(val)}\n📈 Signals: OFF")
        elif data.startswith("toplimit:"):
            val = normalize_top_limit_value(data.split(":", 1)[1], "5")
            set_setting(uid, "top_limit", val)
            msg_limit = "все найденные" if val == "all" else f"до {val}"
            await say(f"✅ TopLimit: {top_limit_label({'top_limit': val})}. AI approval будет проверять {msg_limit} лучших сетапов.", top_limit_menu(get_settings(uid)), keep_menu_bottom=False)
        elif data.startswith("structural:"):
            val = data.split(":", 1)[1]
            set_setting(uid, "structural_mode", val)
            await say(f"✅ Structural: {structural_mode_label(val)}", structural_layers_menu(get_settings(uid)), keep_menu_bottom=False)

        elif data.startswith("scan:"):
            n = int(data.split(":", 1)[1])
            set_setting(uid, "scanner_size", n)
            old_task = USER_SCAN_TASKS.get(uid)
            if old_task and not old_task.done():
                await say(f"⏳ Top-{n} scan уже выполняется. Кнопки и сообщения доступны, дождись финального результата.")
            else:
                await say(f"🔎 Top-{n} scan запущен в фоне. Кнопки и сообщения доступны.", keep_menu_bottom=False)
                USER_SCAN_TASKS[uid] = context.application.create_task(_run_scan_task(uid, n, context, chat_id))
        elif data == "status":
            await say(get_status_text(uid))
        elif data == "ping":
            started = time.perf_counter()
            exchange_health = await check_exchange_api(uid)
            latency_ms = round((time.perf_counter() - started) * 1000, 2)
            s = get_settings(uid)
            await say(
                f"📡 Fast Ping: {latency_ms} ms\n"
                f"⏱ Время работы: {format_uptime(int(time.time() - START_TIME))}\n"
                f"🧠 Память: {memory_usage_text()}\n"
                f"🤖 Provider: {s.get('ai_provider')}\n"
                f"🧠 Модель ИИ: {get_active_model(s)}\n"
                f"🏦 API биржи {s.get('exchange', '').upper()}: {exchange_health}\n"
                f"📦 Version: {BOT_VERSION}"
            )
        elif data == "ping_ai":
            await say("🧠 Проверка AI модели...", keep_menu_bottom=False)
            started = time.perf_counter()
            ai_health = await check_ai_health(uid, context, chat_id)
            latency_ms = round((time.perf_counter() - started) * 1000, 2)
            s = get_settings(uid)
            await say(
                f"🧠 Ping AI\n"
                f"🤖 Provider: {s.get('ai_provider')}\n"
                f"🧠 Модель: {get_active_model(s)}\n"
                f"⚡ Ответ модели: {latency_ms} ms\n"
                f"🧪 AI Status: {ai_health}"
            )
        elif data == "positions":
            await say(await positions_text(uid))
        elif data == "toggle:stopall":
            s_now = get_settings(uid)
            msg = await stop_all_pro(uid, context.application) if not s_now.get("stop_all_enabled", False) else await stop_all_restore_defaults(uid)
            await say(msg)
        elif data == "toggle:positionsync":
            new = not bool(get_settings(uid).get("position_sync_enabled", False))
            set_setting(uid, "position_sync_enabled", new)
            await say(f"✅ Position Sync: {'ON' if new else 'OFF'}")
        elif data == "toggle:livetrademanager":
            new = not bool(get_settings(uid).get("live_trade_manager_enabled", False))
            set_setting(uid, "live_trade_manager_enabled", new)
            await say(f"✅ Live Trade Manager: {'ON' if new else 'OFF'}")
        elif data == "toggle:btceth":
            new = "btc_eth" if s.get("market_universe") != "btc_eth" else "all"
            set_setting(uid, "market_universe", new)
            await say(f"✅ Market Universe: {new}")
        elif data == "toggle:sessions":
            new = not bool(s.get("session_filter"))
            set_setting(uid, "session_filter", new)
            await say(f"✅ Asia/America volatility: {'ON' if new else 'OFF'}")
        elif data == "help":
            await say(help_text())
        elif data == "ai_confirm":
            await say("🧠 AI Confirm запущен...", keep_menu_bottom=False)
            txt = await ai_confirm(uid)
            await say(txt, InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ OPEN REAL TRADE", callback_data="open_confirmed")],
                [InlineKeyboardButton("❌ CANCEL", callback_data="cancel")],
                [InlineKeyboardButton("⬅️ Главное меню", callback_data="back:main")]
            ]), keep_menu_bottom=False)
        elif data == "open_confirmed":
            if stop_all_active(uid):
                await say("🚨 STOP ALL is ON. Opening trades is blocked.")
                return
            confirmed = LAST_AI_CONFIRMED.get(int(uid), [])
            if not confirmed:
                await say("STRICT AI MODE: нет AI-approved сделок. Открытие заблокировано.")
                return
            s_now = get_settings(uid)
            session_ok, session_msg = session_filter_allows_trading(s_now)
            if not session_ok:
                await say("🌏 Asia/America volatility: ON\n⛔ Opening trades is blocked by session filter.\n" + session_msg)
                return
            opened, errors = [], []
            for x in confirmed[:int(s_now.get("max_trades", 3))]:
                sym = normalize_symbol(x.get("symbol", ""))
                direction = x.get("direction", "LONG").upper()
                try:
                    if s_now.get("real_execution_enabled"):
                        pos = await execute_real_trade(uid, sym, direction, x.get("stop_loss"), x.get("take_profit"), x.get("dynamic_rr", 2.0))
                        opened.append(format_real_opened_message(pos) + ("\nExtended TP: ON" if x.get("extended_tp_mode") else ""))
                    else:
                        opened.append(f"PAPER {sym} {direction} — real execution OFF" + (" | Extended TP" if x.get("extended_tp_mode") else ""))
                except Exception as e:
                    errors.append(f"{sym}: {str(e)[:220]}")
            await say("🛡 Risk Manager PASSED\n\n" + "\n".join(opened) + (("\n\nОшибки:\n" + "\n".join(errors)) if errors else ""))
        elif data == "cancel":
            await say("Отменено.")
        elif data in ["toggle:breakeven", "toggle:trailing", "toggle:partialtp"]:
            key = {"toggle:breakeven": "breakeven_enabled", "toggle:trailing": "trailing_enabled", "toggle:partialtp": "partial_tp_enabled"}[data]
            cur = bool(s.get(key, False))
            set_setting(uid, key, not cur)
            await say(f"✅ {key}: {'ON' if not cur else 'OFF'}", trade_mgmt_menu(get_settings(uid)), keep_menu_bottom=False)
        elif data.startswith("tm:"):
            key = data.split(":", 1)[1]
            mapping = {"be": "breakeven_enabled", "trailing": "trailing_enabled", "partial": "partial_tp_enabled"}
            if key in mapping:
                cur = bool(s.get(mapping[key], False))
                set_setting(uid, mapping[key], not cur)
                await say(f"✅ {key}: {'ON' if not cur else 'OFF'}")
            else:
                await say(f"⚠️ Unknown TM option: {key}")
        else:
            await say(f"⚠️ Unknown button: {data}")

    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Button error: {str(e)[:800]}")



async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    uid = user_id(update)
    s = get_settings(uid)
    if s.get("mode") == "chat":
        try:
            trading_prompt = build_trading_chat_prompt(txt)
            ai = await call_ai(uid, trading_prompt, context, update.effective_chat.id, TRADING_CHAT_SYSTEM_PROMPT, options=AI_CHAT_OPTIONS)
            validate_ai_response_or_raise(s, ai)
            await update.message.reply_text(ai[:3900])
        except Exception as e:
            await update.message.reply_text(f"❌ AI error: {str(e)[:1000]}")
        return
    if re.fullmatch(r"[A-Za-z]{2,10}(USDT)?", txt):
        try:
            await update.message.reply_text(
                await signal_for_symbol(uid, txt, context=context, chat_id=update.effective_chat.id)
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Signal error: {str(e)[:1000]}")
    else:
        await update.message.reply_text("Напиши тикер, например BTC или ETH, либо /help.")

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = str(q.from_user.id)
    s = get_settings(uid)
    data = q.data
    chat_id = q.message.chat_id

    if data == "back:main":
        await send_below_buttons(context, chat_id, f"🤖 Trading Bot v{BOT_VERSION}", uid)
    elif data == "help":
        await send_below_buttons(context, chat_id, help_text(), uid)
    elif data == "ping":
        started = time.perf_counter()
        ai_health = await check_ai_health(uid, context, chat_id)
        exchange_health = await check_exchange_api(uid)
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        await send_below_buttons(
            context, chat_id,
            f"📡 Ping: {latency_ms} ms\n"
            f"⏱ Время работы: {format_uptime(int(time.time() - START_TIME))}\n"
            f"🧠 Память: {memory_usage_text()}\n"
            f"🤖 Provider: {s.get('ai_provider')}\n"
            f"🧠 Модель ИИ: {get_active_model(s)}\n"
            f"🧪 Отклик/работа модели ИИ: {ai_health}\n"
            f"🏦 API биржи {s.get('exchange', '').upper()}: {exchange_health}\n"
            f"📦 Version: {BOT_VERSION}",
            uid
        )
    elif data == "status":
        await send_below_buttons(context, chat_id, get_status_text(uid), uid)
    elif data == "positions":
        await send_below_buttons(context, chat_id, await positions_text(uid), uid)
    elif data.startswith("scan:"):
        n = int(data.split(":")[1])
        set_setting(uid, "scanner_size", n)
        await send_below_buttons(
            context, chat_id,
            await run_top_scan(uid, n, context, chat_id),
            uid,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🧠 AI Confirm", callback_data="ai_confirm")],
                [InlineKeyboardButton("⬅️ Главное меню", callback_data="back:main")]
            ])
        )
    elif data == "ai_confirm":
        txt = await ai_confirm(uid)
        await send_below_buttons(
            context, chat_id,
            txt,
            uid,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ OPEN REAL TRADE", callback_data="open_confirmed")],
                [InlineKeyboardButton("❌ CANCEL", callback_data="cancel")],
                [InlineKeyboardButton("⬅️ Главное меню", callback_data="back:main")]
            ])
        )
    elif data == "open_confirmed":
        if stop_all_active(uid):
            await send_below_buttons(context, chat_id, "🚨 STOP ALL is ON. Opening trades is blocked.", uid)
            return
        confirmed = LAST_AI_CONFIRMED.get(int(uid), [])
        if not confirmed:
            await send_below_buttons(context, chat_id, "STRICT AI MODE: нет AI-approved сделок. Открытие заблокировано.", uid)
            return
        session_ok, session_msg = session_filter_allows_trading(s)
        if not session_ok:
            await send_below_buttons(context, chat_id, "🌏 Asia/America volatility: ON\n⛔ Opening trades is blocked by session filter.\n" + session_msg, uid)
            return
        opened, errors = [], []
        for x in confirmed[:int(s.get("max_trades",3))]:
            sym = normalize_symbol(x.get("symbol",""))
            direction = x.get("direction","LONG").upper()
            try:
                if s.get("real_execution_enabled"):
                    pos = await execute_real_trade(uid, sym, direction, x.get("stop_loss"), x.get("take_profit"), x.get("dynamic_rr", 2.0))
                    opened.append(format_real_opened_message(pos))
                else:
                    opened.append(f"PAPER {sym} {direction} — real execution OFF")
            except Exception as e:
                errors.append(f"{sym}: {str(e)[:220]}")
        await send_below_buttons(context, chat_id, ("🛡 Risk Manager PASSED\n\n" + "\n".join(opened) + ("\n\nОшибки:\n" + "\n".join(errors) if errors else "")), uid)
    elif data == "menu:provider":
        await send_below_buttons(context, chat_id, "AI Provider:", uid, reply_markup=provider_menu(s))
    elif data.startswith("provider:"):
        set_ai_provider(uid, data.split(":")[1])
        await send_below_buttons(context, chat_id, f"✅ Provider: {data.split(':')[1]}", uid)
    elif data == "menu:model":
        await send_below_buttons(context, chat_id, "Model:", uid, reply_markup=model_menu(s))
    elif data.startswith("ollama_model:"):
        model = data.split(":",1)[1]
        set_ollama_model(uid, model)
        await send_below_buttons(context, chat_id, f"✅ Ollama model selected: {model}\nПроверяю/загружаю модель...", uid)
        try:
            await ensure_ollama_model(model, context, chat_id, uid)
            await send_below_buttons(context, chat_id, f"✅ Модель {model} готова к работе.", uid)
        except Exception as e:
            await send_below_buttons(context, chat_id, f"❌ Ошибка загрузки модели {model}: {str(e)[:1000]}", uid)
    elif data.startswith("openai_model:"):
        model = data.split(":",1)[1]
        set_openai_model(uid, model)
        await send_below_buttons(context, chat_id, f"✅ OpenAI model: {model}", uid)
    elif data == "menu:reasoning":
        await send_below_buttons(context, chat_id, "Reasoning:", uid, reply_markup=reasoning_menu(s))
    elif data.startswith("reasoning:"):
        set_setting(uid, "reasoning_level", data.split(":")[1])
        await send_below_buttons(context, chat_id, f"✅ Reasoning: {data.split(':')[1]}", uid)
    elif data == "menu:exchange":
        await send_below_buttons(context, chat_id, "Exchange:", uid, reply_markup=exchange_menu(s))
    elif data.startswith("exchange:"):
        val = data.split(":", 1)[1].lower().strip()
        if val not in SUPPORTED_EXCHANGES:
            await send_below_buttons(context, chat_id, "❌ Unsupported exchange. Use MEXC/BingX/Binance.", uid)
        else:
            set_setting(uid, "exchange", val)
            await send_below_buttons(context, chat_id, f"✅ Exchange: {val.upper()}", uid)
    elif data == "menu:tradingmode":
        await send_below_buttons(context, chat_id, "Trading mode:", uid, reply_markup=tradingmode_menu(s))
    elif data.startswith("tradingmode:"):
        await send_below_buttons(context, chat_id, apply_trading_mode(uid, data.split(":", 1)[1]), uid)
    elif data == "menu:timeframe":
        await send_below_buttons(context, chat_id, "Timeframe:", uid, reply_markup=timeframe_menu(s))
    elif data.startswith("timeframe:"):
        set_setting(uid, "timeframe_mode", data.split(":")[1])
        await send_below_buttons(context, chat_id, f"✅ Timeframe: {timeframe_label(data.split(':')[1])}", uid)
    elif data == "menu:autoscanner":
        await send_below_buttons(context, chat_id, "Auto Scanner Top:", uid, reply_markup=auto_scanner_menu(s))
    elif data.startswith("autoscanner:"):
        val = data.split(":")[1]
        set_setting(uid, "auto_scanner_interval", val)
        set_setting(uid, "auto_scanner_last_run", 0)
        if val != "off":
            set_setting(uid, "mode", "signal")
            await send_below_buttons(context, chat_id, f"✅ Auto Scanner: {auto_scanner_label(val)}\n📈 Signals: ON", uid)
        else:
            await send_below_buttons(context, chat_id, f"✅ Auto Scanner: {auto_scanner_label(val)}\n📈 Signals: OFF", uid)
    elif data == "menu:structural":
        await send_below_buttons(context, chat_id, "Structural Layers:", uid, reply_markup=structural_layers_menu(s))
    elif data.startswith("structural:"):
        mode = data.split(":")[1]
        set_setting(uid, "structural_mode", mode)
        await send_below_buttons(context, chat_id, f"✅ Structural: {structural_mode_label(mode)}", uid, reply_markup=structural_layers_menu(get_settings(uid)))
    elif data == "menu:trademgmt":
        await send_below_buttons(context, chat_id, "Trade Management:", uid, reply_markup=trade_mgmt_menu(s))
    elif data == "toggle:sessions":
        new = not bool(s.get("session_filter"))
        set_setting(uid, "session_filter", new)
        await send_below_buttons(context, chat_id, f"✅ Азия/Америка: {'ON' if new else 'OFF'}", uid)
    elif data == "toggle:btceth":
        new = "all" if s.get("market_universe") == "btc_eth" else "btc_eth"
        set_setting(uid, "market_universe", new)
        await send_below_buttons(context, chat_id, f"✅ Market: {'All Futures Market' if new == 'all' else 'Only BTC/ETH'}", uid)
    elif data == "toggle:stopall":
        s_now = get_settings(uid)
        if not s_now.get("stop_all_enabled", False):
            msg = await stop_all_pro(uid, context.application)
        else:
            msg = await stop_all_restore_defaults(uid)
        await send_below_buttons(context, chat_id, msg, uid)

    elif data == "toggle:positionsync":
        s_now = get_settings(uid)
        new = not bool(s_now.get("position_sync_enabled", False))
        set_setting(uid, "position_sync_enabled", new)
        await send_below_buttons(context, chat_id, f"✅ Position Sync: {'ON' if new else 'OFF'}", uid)
    elif data == "toggle:livetrademanager":
        s_now = get_settings(uid)
        new = not bool(s_now.get("live_trade_manager_enabled", False))
        set_setting(uid, "live_trade_manager_enabled", new)
        await send_below_buttons(context, chat_id, f"✅ Live Trade Manager: {'ON' if new else 'OFF'}", uid)
    elif data in ["toggle:breakeven", "toggle:trailing", "toggle:partialtp"]:
        key = {"toggle:breakeven": "breakeven_enabled", "toggle:trailing": "trailing_enabled", "toggle:partialtp": "partial_tp_enabled"}[data]
        new = not bool(s.get(key))
        set_setting(uid, key, new)
        await send_below_buttons(context, chat_id, f"✅ {key}: {'ON' if new else 'OFF'}", uid, reply_markup=trade_mgmt_menu(get_settings(uid)))
    elif data == "mode:signal":
        await send_below_buttons(context, chat_id, "ℹ️ Кнопка Signal убрана. Сигналы отправляются автоматически, когда Auto Scanner включен.", uid)
    elif data == "mode:chat":
        set_setting(uid, "mode", "chat")
        await send_below_buttons(context, chat_id, "✅ AI Chat Mode", uid)
    elif data == "cancel":
        await send_below_buttons(context, chat_id, "Отменено.", uid)
    else:
        await send_below_buttons(context, chat_id, "Unknown action", uid)


async def positions_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.message.reply_text(await positions_text(user_id(update)))
async def structural_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.message.reply_text("Выбери Structural Layers:", reply_markup=structural_layers_menu(get_settings(user_id(update))))
async def autoscanner_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.message.reply_text("Выбери Auto Scanner:", reply_markup=auto_scanner_menu(get_settings(user_id(update))))
async def autoscanner_off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id(update)
    set_setting(uid, "auto_scanner_interval", "off")
    set_setting(uid, "auto_scanner_last_run", 0)
    await update.message.reply_text("✅ Auto Scanner OFF", reply_markup=main_menu(get_settings(uid)))
async def stopall_on_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=user_id(update); set_setting(uid,"stop_all_enabled",True); set_setting(uid,"auto_scanner_interval","off"); set_setting(uid,"trading_enabled",False); set_setting(uid,"real_execution_enabled",False); await update.message.reply_text("🚨 STOP ALL ACTIVATED", reply_markup=main_menu(get_settings(uid)))
async def stopall_off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id(update)
    set_setting(uid,"stop_all_enabled",False)
    await update.message.reply_text("✅ STOP ALL DISABLED", reply_markup=main_menu(get_settings(uid)))
async def positionsync_on_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id(update)
    set_setting(uid,"position_sync_enabled",True)
    await update.message.reply_text("✅ Position Sync ON", reply_markup=main_menu(get_settings(uid)))
async def positionsync_off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id(update)
    set_setting(uid,"position_sync_enabled",False)
    await update.message.reply_text("✅ Position Sync OFF", reply_markup=main_menu(get_settings(uid)))
async def livetrademanager_on_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id(update)
    set_setting(uid, "live_trade_manager_enabled", True)
    await update.message.reply_text("✅ Live Trade Manager: ON", reply_markup=main_menu(get_settings(uid)))

async def livetrademanager_off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id(update)
    set_setting(uid, "live_trade_manager_enabled", False)
    await update.message.reply_text("✅ Live Trade Manager: OFF", reply_markup=main_menu(get_settings(uid)))

async def livetrademanager_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id(update)
    s = get_settings(uid)
    positions = _positions(uid)
    active_tm = []

    for p in positions:
        tm = p.get("tm", {}) if isinstance(p, dict) else {}
        active_tm.append(
            f"- {p.get('symbol', 'UNKNOWN')} | status={p.get('status', 'open')} | "
            f"BE={'YES' if tm.get('be_done') else 'NO'} | "
            f"Partial={'YES' if tm.get('partial_done') else 'NO'} | "
            f"Trailing={'YES' if tm.get('trailing_active') else 'NO'} | "
            f"Runner={'YES' if tm.get('runner_done') else 'NO'}"
        )

    details = "\n".join(active_tm[:15]) if active_tm else "No tracked positions."

    await update.message.reply_text(
        f"📈 Live Trade Manager Status\n"
        f"Live TM: {'ON' if s.get('live_trade_manager_enabled') else 'OFF'}\n"
        f"Real Execution: {'ON' if s.get('real_execution_enabled') else 'OFF'}\n"
        f"Exchange: {s.get('exchange', '').upper()}\n"
        f"Tracked positions: {len(positions)}\n\n"
        f"{details}"
    )


async def positionsync_now_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.message.reply_text(await sync_positions_for_user(None, user_id(update)))
async def ai_on_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id(update)
    set_setting(uid,"strict_ai_mode",True)
    await update.message.reply_text("🧠 AI CHECK: ON", reply_markup=main_menu(get_settings(uid)))
async def ai_off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id(update)
    set_setting(uid,"strict_ai_mode",False)
    LAST_AI_CONFIRMED[int(uid)] = []
    await update.message.reply_text("🧠 AI CHECK: OFF", reply_markup=main_menu(get_settings(uid)))

async def ai_auto_p_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id(update)
    if not context.args or str(context.args[0]).lower() not in ["on", "off"]:
        await update.message.reply_text("Пример: /ai_auto_p on или /ai_auto_p off")
        return
    enabled = str(context.args[0]).lower() == "on"
    set_setting(uid, "ai_auto_p", enabled)
    LAST_AI_CONFIRMED[int(uid)] = []
    if enabled:
        text = "✅ AI Auto Prompt: ON — после скана сделки сразу отправляются на проверку ИИ."
    else:
        text = "✅ AI Auto Prompt: OFF — после скана бот спросит через кнопку 🧠 AI Confirm."
    await update.message.reply_text(text, reply_markup=main_menu(get_settings(uid)))

async def testai_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id(update)
    s = get_settings(uid)
    provider = s.get("ai_provider", "openai")
    model = get_active_model(s)

    if provider != "openai":
        await update.message.reply_text(
            f"AI Provider: {provider.upper()}\nModel: {model}\n\n/testai проверяет OpenAI API key. Включи OpenAI: /openai_on",
            reply_markup=provider_menu(s)
        )
        return

    keys = load_json(OPENAI_KEYS_FILE, {})
    api_key = keys.get(uid) or os.getenv("OPENAI_API_KEY")
    if not api_key:
        await update.message.reply_text(
            "❌ OpenAI API key не задан.\nДобавь ключ: /setopenai OPENAI_API_KEY",
            reply_markup=provider_menu(s)
        )
        return

    try:
        result = await asyncio.to_thread(
            call_openai,
            uid,
            model,
            "Ответь одним словом: OK",
            s.get("reasoning_level", "medium"),
            "Ты проверяешь доступность OpenAI API key.",
            {"temperature": 0, "num_predict": 16},
        )
        short = str(result or "").strip()[:200]
        await update.message.reply_text(
            f"✅ OpenAI API key работает.\nProvider: OPENAI\nModel: {model}\nОтвет: {short or 'OK'}",
            reply_markup=main_menu(get_settings(uid))
        )
    except Exception as e:
        await update.message.reply_text(
            f"❌ OpenAI API key test failed.\nProvider: OPENAI\nModel: {model}\nОшибка: {str(e)[:700]}",
            reply_markup=provider_menu(s)
        )

async def setapi_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        await update.message.reply_text("Пример: /setapi mexc API_KEY API_SECRET")
        return
    uid = user_id(update); ex, key, sec = context.args[0].lower(), context.args[1], context.args[2]
    if ex not in SUPPORTED_EXCHANGES:
        await update.message.reply_text("❌ Биржа не поддерживается. Используй: mexc, bingx или binance")
        return
    data = load_json(API_KEYS_FILE, {})
    data.setdefault(uid, {})[ex] = {"apiKey": key, "secret": sec}
    save_json(API_KEYS_FILE, data)
    await update.message.reply_text(f"✅ API saved for {ex.upper()}")

async def setopenai_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Пример: /setopenai OPENAI_API_KEY")
        return
    uid = user_id(update)
    data = load_json(OPENAI_KEYS_FILE, {})
    data[uid] = context.args[0]
    save_json(OPENAI_KEYS_FILE, data)
    set_ai_provider(uid, "openai")
    await update.message.reply_text("✅ OpenAI key saved\n🤖 Provider: OPENAI", reply_markup=main_menu(get_settings(uid)))

def simple_setter(key, value, msg):
    async def f(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = user_id(update)
        if key == "ai_provider":
            set_ai_provider(uid, value)
        else:
            set_setting(uid, key, value)
        await update.message.reply_text(msg, reply_markup=main_menu(get_settings(uid)))
    return f

async def duble_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id(update)
    if not context.args or str(context.args[0]).lower() not in {"on", "off"}:
        state = "ON" if get_settings(uid).get("duplicate_protection_enabled", True) else "OFF"
        await update.message.reply_text(f"🛡 Duble Protection: {state}\nИспользуй: /duble on или /duble off", reply_markup=main_menu(get_settings(uid)))
        return
    enabled = str(context.args[0]).lower() == "on"
    set_setting(uid, "duplicate_protection_enabled", enabled)
    await update.message.reply_text(
        "🛡 Duble Protection: ON\nДубли одинаковых открытых сделок будут заблокированы." if enabled else "🛡 Duble Protection: OFF\nПовторное открытие одинаковой найденной сделки разрешено.",
        reply_markup=main_menu(get_settings(uid))
    )

async def numeric_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str, cast, example: str):
    if not context.args:
        await update.message.reply_text(example); return
    uid = user_id(update)
    raw_value = context.args[0]
    if key == "top_limit":
        value = normalize_top_limit_value(raw_value, "5")
    else:
        value = cast(raw_value)
    set_setting(uid, key, value)
    shown = top_limit_label({"top_limit": value}) if key == "top_limit" else raw_value
    await update.message.reply_text(f"✅ {key}: {shown}", reply_markup=main_menu(get_settings(uid)))

async def post_init(app: Application):
    app.create_task(unload_idle_models())
    app.create_task(auto_scanner_loop(app))
    app.create_task(position_sync_loop(app))
    app.create_task(live_trade_manager_loop(app))

async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_inline_menu_message(update, context)




def live_tm_close_side(side: str) -> str:
    return "sell" if str(side).upper() == "LONG" else "buy"


def live_tm_exchange_symbol(ex, raw_symbol: str) -> str:
    try:
        return exchange_symbol_for_order(ex, raw_symbol)
    except Exception:
        markets = ex.load_markets()
        norm = normalize_symbol(raw_symbol)
        candidates = [norm.replace("USDT", "/USDT:USDT"), norm.replace("USDT", "/USDT")]
        for c in candidates:
            if c in markets:
                return c
        return candidates[0]


def live_tm_amount_to_precision(ex, symbol: str, amount: float) -> float:
    try:
        return float(ex.amount_to_precision(symbol, amount))
    except Exception:
        return float(amount)


def live_tm_reduce_only_params(extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    params = {
        "reduceOnly": True,
        "marginMode": "isolated",
    }
    if extra:
        params.update(extra)
    return params


async def live_tm_partial_close(uid: str, pos: Dict[str, Any], percent: float) -> Dict[str, Any]:
    """
    Real reduceOnly partial close.
    Works only when real_execution_enabled=True and exchange API keys are set.
    """
    s = get_settings(uid)
    if not s.get("real_execution_enabled", False):
        return {"skipped": "real_execution_off"}

    ex = get_private_exchange(uid)
    symbol = live_tm_exchange_symbol(ex, pos.get("symbol") or pos.get("market_symbol"))
    side = str(pos.get("direction") or pos.get("side")).upper()
    close_side = live_tm_close_side(side)

    amount = safe_float(pos.get("amount") or pos.get("contracts") or pos.get("size"), 0)
    if amount <= 0:
        return {"skipped": "amount_missing"}

    close_amount = live_tm_amount_to_precision(ex, symbol, amount * (float(percent) / 100.0))
    if close_amount <= 0:
        return {"skipped": "close_amount_zero"}

    order = ex.create_order(
        symbol,
        "market",
        close_side,
        close_amount,
        None,
        live_tm_reduce_only_params()
    )
    return {"order": str(order)[:500], "close_amount": close_amount}


async def live_tm_close_runner(uid: str, pos: Dict[str, Any]) -> Dict[str, Any]:
    """
    Close remaining position reduceOnly.
    """
    s = get_settings(uid)
    if not s.get("real_execution_enabled", False):
        return {"skipped": "real_execution_off"}

    ex = get_private_exchange(uid)
    symbol = live_tm_exchange_symbol(ex, pos.get("symbol") or pos.get("market_symbol"))
    side = str(pos.get("direction") or pos.get("side")).upper()
    close_side = live_tm_close_side(side)

    amount = safe_float(pos.get("amount") or pos.get("contracts") or pos.get("size"), 0)
    remaining_percent = safe_float(pos.get("remaining_percent", 50), 50)
    close_amount = live_tm_amount_to_precision(ex, symbol, amount * (remaining_percent / 100.0))

    if close_amount <= 0:
        return {"skipped": "runner_amount_zero"}

    order = ex.create_order(
        symbol,
        "market",
        close_side,
        close_amount,
        None,
        live_tm_reduce_only_params()
    )
    return {"order": str(order)[:500], "close_amount": close_amount}


async def live_tm_place_or_replace_sl(uid: str, pos: Dict[str, Any], new_sl: float) -> Dict[str, Any]:
    """
    Best-effort SL replacement.
    First tries to cancel known SL order id if present, then places reduceOnly stop-market.
    Exact stop params differ by exchange; this function uses ccxt common params first.
    """
    s = get_settings(uid)
    if not s.get("real_execution_enabled", False):
        return {"skipped": "real_execution_off"}

    ex = get_private_exchange(uid)
    symbol = live_tm_exchange_symbol(ex, pos.get("symbol") or pos.get("market_symbol"))
    side = str(pos.get("direction") or pos.get("side")).upper()
    close_side = live_tm_close_side(side)

    amount = safe_float(pos.get("amount") or pos.get("contracts") or pos.get("size"), 0)
    remaining_percent = safe_float(pos.get("remaining_percent", 100), 100)
    sl_amount = live_tm_amount_to_precision(ex, symbol, amount * (remaining_percent / 100.0))

    if sl_amount <= 0:
        return {"skipped": "sl_amount_zero"}

    tm = pos.setdefault("tm", {})

    # Best effort cancel previous known stop order
    old_sl_id = tm.get("sl_order_id") or pos.get("sl_order_id")
    if old_sl_id:
        try:
            ex.cancel_order(old_sl_id, symbol)
        except Exception as e:
            tm.setdefault("warnings", []).append(f"cancel old SL failed: {str(e)[:120]}")

    errors = []
    order_types = ["stop_market", "market"]
    params_variants = [
        live_tm_reduce_only_params({"triggerPrice": new_sl, "stopPrice": new_sl}),
        live_tm_reduce_only_params({"stopLossPrice": new_sl, "triggerPrice": new_sl}),
        live_tm_reduce_only_params({"stopPrice": new_sl}),
    ]

    for typ in order_types:
        for params in params_variants:
            try:
                order = ex.create_order(symbol, typ, close_side, sl_amount, None, params)
                try:
                    tm["sl_order_id"] = order.get("id")
                except Exception:
                    pass
                return {"order": str(order)[:500], "new_sl": new_sl, "type": typ}
            except Exception as e:
                errors.append(f"{typ}: {str(e)[:120]}")

    return {"warning": "SL replace failed", "errors": errors[-5:]}


async def live_tm_update_trailing_sl(uid: str, pos: Dict[str, Any], current_price: float, trailing_r: float = 1.0) -> Dict[str, Any]:
    """
    Trailing SL by configured R-distance behind current price.
    """
    side = str(pos.get("direction") or pos.get("side")).upper()
    entry = safe_float(pos.get("entry"), 0)
    original_sl = safe_float(pos.get("initial_stop_loss") or pos.get("sl") or pos.get("stop_loss"), 0)
    if not entry or not original_sl:
        return {"skipped": "missing_entry_sl"}

    risk = abs(entry - original_sl)
    if risk <= 0:
        return {"skipped": "bad_risk"}

    trail_distance = risk * max(safe_float(trailing_r, 1.0), 0.1)
    current_sl = safe_float(pos.get("stop_loss") or pos.get("sl") or original_sl, original_sl)

    if side == "LONG":
        proposed_sl = round(current_price - trail_distance, 8)
        if proposed_sl <= current_sl or proposed_sl <= entry:
            return {"skipped": "no_trailing_improvement"}
    else:
        proposed_sl = round(current_price + trail_distance, 8)
        if proposed_sl >= current_sl or proposed_sl >= entry:
            return {"skipped": "no_trailing_improvement"}

    result = await live_tm_place_or_replace_sl(uid, pos, proposed_sl)
    if "order" in result:
        pos["stop_loss"] = proposed_sl
        pos["sl"] = proposed_sl
    return result




async def notify_user(app, uid: str, text: str):
    if app is None:
        return
    try:
        await app.bot.send_message(chat_id=int(uid), text=text[:3900])
    except Exception:
        pass


async def live_tm_close_position_reduce_only(uid: str, pos: Dict[str, Any]) -> Dict[str, Any]:
    """
    Emergency reduceOnly close for tracked position.
    Used by STOP ALL PRO and runner close fallback.
    """
    s = get_settings(uid)
    if not s.get("real_execution_enabled", False):
        return {"skipped": "real_execution_off"}

    ex = get_private_exchange(uid)
    symbol = live_tm_exchange_symbol(ex, pos.get("symbol") or pos.get("market_symbol"))
    side = str(pos.get("direction") or pos.get("side")).upper()
    close_side = live_tm_close_side(side)

    amount = safe_float(pos.get("amount") or pos.get("contracts") or pos.get("size"), 0)
    remaining_percent = safe_float(pos.get("remaining_percent", 100), 100)
    close_amount = live_tm_amount_to_precision(ex, symbol, amount * (remaining_percent / 100.0))

    if close_amount <= 0:
        return {"skipped": "close_amount_zero"}

    order = ex.create_order(
        symbol,
        "market",
        close_side,
        close_amount,
        None,
        live_tm_reduce_only_params()
    )
    return {"order": str(order)[:500], "close_amount": close_amount}


async def stop_all_pro(uid: str, app=None) -> str:
    """
    Emergency STOP ALL:
    - turns off auto scanner
    - turns off trading
    - turns off real execution
    - turns off live trade manager
    - attempts to close tracked open positions reduceOnly BEFORE disabling real execution
    """
    s = get_settings(uid)
    positions = _positions(uid)
    results = []

    # Attempt closing while current real_execution setting is still available.
    if s.get("real_execution_enabled", False) and positions:
        for pos in positions:
            try:
                if str(pos.get("status", "")).lower().startswith("closed"):
                    continue
                result = await live_tm_close_position_reduce_only(uid, pos)
                pos.setdefault("tm", {}).setdefault("events", []).append(f"STOP ALL close action: {str(result)[:180]}")
                if "order" in result:
                    pos["status"] = "closed_by_stop_all"
                    pos["remaining_percent"] = 0
                results.append(f"{pos.get('symbol')}: {result}")
            except Exception as e:
                pos.setdefault("tm", {}).setdefault("errors", []).append(f"STOP ALL close error: {str(e)[:180]}")
                results.append(f"{pos.get('symbol')}: ERROR {str(e)[:160]}")
        _save_positions(uid, positions)

    set_setting(uid, "stop_all_enabled", True)
    set_setting(uid, "auto_scanner_interval", "off")
    set_setting(uid, "trading_enabled", False)
    set_setting(uid, "real_execution_enabled", False)
    set_setting(uid, "live_trade_manager_enabled", False)
    set_setting(uid, "position_sync_enabled", False)

    msg = (
        "🚨 STOP ALL ACTIVATED\n"
        "Auto Scanner: OFF\n"
        "Trading: OFF\n"
        "Real Execution: OFF\n"
        "Live TM: OFF\n"
        "Position Sync: OFF\n"
    )

    if results:
        msg += "\nClose attempts:\n" + "\n".join(results)[:2500]
    else:
        msg += "\nNo tracked positions closed, or Real Execution was OFF."

    await notify_user(app, uid, msg)
    return msg


async def stop_all_restore_defaults(uid: str) -> str:
    """
    STOP ALL OFF:
    returns to safe default state.
    It does NOT automatically turn on real trading.
    """
    set_setting(uid, "stop_all_enabled", False)
    set_setting(uid, "auto_scanner_interval", "off")
    set_setting(uid, "trading_enabled", False)
    set_setting(uid, "real_execution_enabled", False)
    set_setting(uid, "live_trade_manager_enabled", False)
    set_setting(uid, "position_sync_enabled", False)
    return (
        "✅ STOP ALL DISABLED\n"
        "Safe defaults restored:\n"
        "Auto Scanner: OFF\n"
        "Trading: OFF\n"
        "Real Execution: OFF\n"
        "Live TM: OFF\n"
        "Position Sync: OFF"
    )



async def manage_live_trades_for_user(uid: str, app=None):
    """
    Live Trade Manager.
    v0029:
    - sends Telegram notifications for TP1/partial/BE/trailing/TP2
    - real actions only when real_execution_enabled=True
    """
    s = get_settings(uid)
    if not s.get("live_trade_manager_enabled", False):
        return

    positions = _positions(uid)
    if not positions:
        return

    changed = False

    for pos in positions:
        try:
            symbol = pos.get("symbol") or pos.get("market_symbol")
            side = str(pos.get("direction") or pos.get("side") or "").upper()
            entry = safe_float(pos.get("entry"), 0)
            sl = safe_float(pos.get("initial_stop_loss") or pos.get("sl") or pos.get("stop_loss"), 0)
            tp1 = safe_float(pos.get("tp1") or pos.get("take_profit"), 0)
            tp2 = safe_float(pos.get("tp2") or pos.get("runner_target"), 0)

            if not symbol or side not in ["LONG", "SHORT"] or not entry or not sl:
                continue

            df = fetch_ohlcv_for_symbol(s["exchange"], symbol, "1m", 2)
            price = float(df["close"].iloc[-1])
            risk = abs(entry - sl)
            if risk <= 0:
                continue

            if not pos.get("initial_stop_loss"):
                pos["initial_stop_loss"] = sl

            breakeven_enabled = bool(s.get("breakeven_enabled", False))
            partial_tp_enabled = bool(s.get("partial_tp_enabled", False))
            trailing_enabled = bool(s.get("trailing_enabled", False))
            breakeven_r = max(safe_float(s.get("breakeven_r"), 1), 0.1)
            partial_tp_r = max(safe_float(s.get("partial_tp_r"), 1), 0.1)
            partial_tp_percent = min(max(safe_float(s.get("partial_tp_percent"), 50), 1), 100)
            trailing_r = max(safe_float(s.get("trailing_r"), 1.5), 0.1)

            be_trigger = entry + risk * breakeven_r if side == "LONG" else entry - risk * breakeven_r
            partial_trigger = entry + risk * partial_tp_r if side == "LONG" else entry - risk * partial_tp_r
            trailing_trigger = entry + risk * trailing_r if side == "LONG" else entry - risk * trailing_r

            pos.setdefault("tm", {})
            tm = pos["tm"]
            tm["last_price"] = price
            tm["last_check_ts"] = time.time()
            tm["real_execution"] = bool(s.get("real_execution_enabled", False))
            tm["settings"] = {
                "breakeven_enabled": breakeven_enabled,
                "breakeven_r": breakeven_r,
                "partial_tp_enabled": partial_tp_enabled,
                "partial_tp_r": partial_tp_r,
                "partial_tp_percent": partial_tp_percent,
                "trailing_enabled": trailing_enabled,
                "trailing_r": trailing_r,
            }

            def hit_level(level: float) -> bool:
                if not level:
                    return False
                return (side == "LONG" and price >= level) or (side == "SHORT" and price <= level)

            # 1) BE move at configured R
            if breakeven_enabled and not tm.get("be_done") and hit_level(be_trigger):
                tm["be_done"] = True
                tm["new_sl"] = entry
                tm["be_triggered_ts"] = time.time()
                tm.setdefault("events", []).append(f"BE trigger hit. Move SL to entry {entry}.")

                result = {"mode": "local_only"}
                if s.get("real_execution_enabled", False):
                    result = await live_tm_place_or_replace_sl(uid, pos, entry)
                    tm["be_order_result"] = result
                    if "order" in result:
                        pos["stop_loss"] = entry
                        pos["sl"] = entry

                await notify_user(
                    app,
                    uid,
                    f"🟢 SL moved to breakeven\n"
                    f"{symbol} {side}\n"
                    f"BE: {entry}\n"
                    f"Trigger: {round(be_trigger, 8)} ({breakeven_r}R)"
                )
                changed = True
            # 2) Partial close at configured R
            if partial_tp_enabled and not tm.get("partial_done") and hit_level(partial_trigger):
                tm["partial_done"] = True
                tm["partial_close_percent"] = partial_tp_percent
                tm["partial_trigger"] = round(partial_trigger, 8)
                tm["partial_triggered_ts"] = time.time()
                tm.setdefault("events", []).append(f"Partial TP hit at {partial_tp_r}R. Close {partial_tp_percent}%.")

                result = {"mode": "local_only"}
                if s.get("real_execution_enabled", False):
                    result = await live_tm_partial_close(uid, pos, partial_tp_percent)
                    tm["partial_order_result"] = result
                    if "order" in result:
                        pos["remaining_percent"] = max(0, safe_float(pos.get("remaining_percent", 100), 100) - partial_tp_percent)

                await notify_user(
                    app,
                    uid,
                    f"🎯 Partial TP reached\n"
                    f"{symbol} {side}\n"
                    f"✅ {partial_tp_percent}% closed/planned\n"
                    f"Trigger: {round(partial_trigger, 8)} ({partial_tp_r}R)\n"
                    f"Price: {price}\n"
                    f"Result: {str(result)[:300]}"
                )
                changed = True

            # 3) Activate trailing when enabled: after partial TP, or at trailing_r if Partial TP is OFF
            if trailing_enabled and not tm.get("trailing_active"):
                activate_trailing = (partial_tp_enabled and tm.get("partial_done")) or ((not partial_tp_enabled) and hit_level(trailing_trigger))
                if activate_trailing:
                    tm["trailing_active"] = True
                    tm["trailing_started_ts"] = time.time()
                    tm["trailing_trigger"] = round(trailing_trigger, 8)
                    tm.setdefault("events", []).append(f"Trailing activated at {trailing_r}R.")

                    await notify_user(
                        app,
                        uid,
                        f"🔄 Trailing Stop activated\n"
                        f"{symbol} {side}\n"
                        f"Trailing R: {trailing_r}"
                    )
                    changed = True

            # 4) Trailing update
            if trailing_enabled and tm.get("trailing_active") and not tm.get("runner_done"):
                if s.get("real_execution_enabled", False):
                    result = await live_tm_update_trailing_sl(uid, pos, price, trailing_r)
                    if result and not result.get("skipped"):
                        tm["last_trailing_result"] = result
                        tm.setdefault("events", []).append(f"Trailing real action: {str(result)[:180]}")
                        await notify_user(
                            app,
                            uid,
                            f"🔄 Trailing updated\n"
                            f"{symbol} {side}\n"
                            f"Price: {price}\n"
                            f"New SL: {pos.get('stop_loss') or pos.get('sl')}\n"
                            f"Result: {str(result)[:300]}"
                        )
                        changed = True

            # 5) Runner close at TP2
            if tp2 and not tm.get("runner_done") and hit_level(tp2):
                tm["runner_done"] = True
                tm["runner_done_ts"] = time.time()
                tm.setdefault("events", []).append("TP2 / runner target hit.")

                result = {"mode": "local_only"}
                if s.get("real_execution_enabled", False):
                    result = await live_tm_close_runner(uid, pos)
                    tm["runner_order_result"] = result
                    if "order" in result:
                        pos["remaining_percent"] = 0
                        pos["status"] = "closed_by_live_tm"

                await notify_user(
                    app,
                    uid,
                    f"✅ POSITION CLOSED\n"
                    f"{symbol} {side}\n"
                    f"Reason: TP\n"
                    f"Price: {price}"
                )
                changed = True

        except Exception as e:
            try:
                pos.setdefault("tm", {}).setdefault("errors", []).append(str(e)[:180])
                await notify_user(app, uid, f"⚠️ Live TM error for {pos.get('symbol')}: {str(e)[:300]}")
                changed = True
            except Exception:
                pass
            continue

    if changed:
        _save_positions(uid, positions)

async def live_trade_manager_loop(app):
    while True:
        try:
            all_settings = load_json(SETTINGS_FILE, {})
            for uid, s in all_settings.items():
                if s.get("live_trade_manager_enabled", False):
                    await manage_live_trades_for_user(str(uid), app)
        except Exception:
            pass
        await asyncio.sleep(10)



async def callback_test_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Callback test:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Test button", callback_data="help")]])
    )



def validate_menu_functions():
    required = [
        "provider_menu",
        "model_menu",
        "reasoning_menu",
        "exchange_menu",
        "tradingmode_menu",
        "timeframe_menu",
        "auto_scanner_menu",
        "structural_menu",
        "trade_mgmt_menu",
    ]

    missing = [x for x in required if x not in globals()]
    if missing:
        print("WARNING: missing menu functions:", missing)
    else:
        print("Menu validation OK")



def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("callback_test", callback_test_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("ping", ping_cmd))
    app.add_handler(CommandHandler("ping_ai", ping_ai_cmd))
    app.add_handler(CommandHandler("signal", signal_cmd))
    app.add_handler(CommandHandler("positions", positions_cmd))
    app.add_handler(CommandHandler("structural", structural_cmd))
    app.add_handler(CommandHandler("autoscanner", autoscanner_cmd))
    app.add_handler(CommandHandler("autoscanner_off", autoscanner_off_cmd))
    app.add_handler(CommandHandler("setapi", setapi_cmd))
    app.add_handler(CommandHandler("setopenai", setopenai_cmd))
    app.add_handler(CommandHandler("testai", testai_cmd))
    app.add_handler(CommandHandler("ai_on", ai_on_cmd))
    app.add_handler(CommandHandler("ai_off", ai_off_cmd))
    app.add_handler(CommandHandler("stopall_on", stopall_on_cmd))
    app.add_handler(CommandHandler("stopall_off", stopall_off_cmd))
    app.add_handler(CommandHandler("positionsync_on", positionsync_on_cmd))
    app.add_handler(CommandHandler("positionsync_off", positionsync_off_cmd))
    app.add_handler(CommandHandler("positionsync_now", positionsync_now_cmd))

    app.add_handler(CommandHandler("livetrademanager_on", livetrademanager_on_cmd))
    app.add_handler(CommandHandler("livetrademanager_off", livetrademanager_off_cmd))
    app.add_handler(CommandHandler("livetrademanager_status", livetrademanager_status_cmd))
    app.add_handler(CommandHandler("trading_on", simple_setter("trading_enabled", True, "✅ Trading ON")))
    app.add_handler(CommandHandler("trading_off", simple_setter("trading_enabled", False, "✅ Trading OFF")))
    app.add_handler(CommandHandler("real_on", simple_setter("real_execution_enabled", True, "✅ REAL EXECUTION ON")))
    app.add_handler(CommandHandler("real_off", simple_setter("real_execution_enabled", False, "✅ REAL EXECUTION OFF")))
    app.add_handler(CommandHandler("duble", duble_cmd))
    app.add_handler(CommandHandler("aiauto_on", simple_setter("ai_auto", True, "✅ AI Auto ON")))
    app.add_handler(CommandHandler("aiauto_off", simple_setter("ai_auto", False, "✅ AI Auto OFF")))
    app.add_handler(CommandHandler("ai_auto_p", ai_auto_p_cmd))
    app.add_handler(CommandHandler("provider_ollama", simple_setter("ai_provider", "ollama", "✅ Provider Ollama")))
    app.add_handler(CommandHandler("provider_openai", simple_setter("ai_provider", "openai", "✅ Provider OpenAI")))
    app.add_handler(CommandHandler("openai_on", simple_setter("ai_provider", "openai", "✅ OpenAI ON")))
    app.add_handler(CommandHandler("openai_off", simple_setter("ai_provider", "ollama", "✅ OpenAI OFF / Ollama ON")))
    app.add_handler(CommandHandler("top50", simple_setter("scanner_size", 50, "✅ Scanner Top-50")))
    app.add_handler(CommandHandler("top100", simple_setter("scanner_size", 100, "✅ Scanner Top-100")))
    app.add_handler(CommandHandler("top200", simple_setter("scanner_size", 200, "✅ Scanner Top-200")))
    app.add_handler(CommandHandler("breakeven_on", simple_setter("breakeven_enabled", True, "✅ Breakeven ON")))
    app.add_handler(CommandHandler("breakeven_off", simple_setter("breakeven_enabled", False, "✅ Breakeven OFF")))
    app.add_handler(CommandHandler("trailing_on", simple_setter("trailing_enabled", True, "✅ Trailing ON")))
    app.add_handler(CommandHandler("trailing_off", simple_setter("trailing_enabled", False, "✅ Trailing OFF")))
    app.add_handler(CommandHandler("partialtp_on", simple_setter("partial_tp_enabled", True, "✅ Partial TP ON")))
    app.add_handler(CommandHandler("partialtp_off", simple_setter("partial_tp_enabled", False, "✅ Partial TP OFF")))
    app.add_handler(CommandHandler("risk", lambda u,c: numeric_cmd(u,c,"risk_percent",float,"Пример: /risk 1")))
    app.add_handler(CommandHandler("leverage", lambda u,c: numeric_cmd(u,c,"leverage",int,"Пример: /leverage 5")))
    app.add_handler(CommandHandler("minscore", lambda u,c: numeric_cmd(u,c,"min_score",float,"Пример: /minscore 80")))
    app.add_handler(CommandHandler("toplimit", lambda u,c: numeric_cmd(u,c,"top_limit",str,"Пример: /toplimit 5 или /toplimit 10 или /toplimit all")))
    app.add_handler(CallbackQueryHandler(inline_button_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.run_polling()

if __name__ == "__main__":
    main()
