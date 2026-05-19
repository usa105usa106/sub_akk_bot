from __future__ import annotations
SCAN_MODE = "momentum"
REVERSAL_CHARTS = False

def get_scan_mode(uid: Optional[str] = None):
    """Current scanner mode. Stored in settings when uid is known; global fallback keeps old calls compatible."""
    try:
        if uid is not None and "get_settings" in globals():
            return str(get_settings(str(uid)).get("scan_mode", SCAN_MODE)).lower()
    except Exception:
        pass
    return str(SCAN_MODE or "momentum").lower()

def set_scan_mode(mode, uid: Optional[str] = None):
    global SCAN_MODE
    mode = str(mode).lower().strip()
    mode = mode if mode in {"momentum", "reversal", "hybrid"} else "momentum"
    SCAN_MODE = mode
    try:
        if uid is not None and "set_setting" in globals():
            set_setting(str(uid), "scan_mode", mode)
    except Exception:
        pass
    return mode

def get_hybrid_variant(uid: Optional[str] = None):
    try:
        if uid is not None and "get_settings" in globals():
            val = str(get_settings(str(uid)).get("hybrid_variant", "light")).lower()
            return val if val in {"light", "full"} else "light"
    except Exception:
        pass
    return "light"

def set_hybrid_variant(variant, uid: Optional[str] = None):
    variant = str(variant or "light").lower().strip()
    variant = variant if variant in {"light", "full"} else "light"
    try:
        if uid is not None and "set_setting" in globals():
            set_setting(str(uid), "hybrid_variant", variant)
    except Exception:
        pass
    return variant

def get_reversal_charts(uid: Optional[str] = None) -> bool:
    try:
        if uid is not None and "get_settings" in globals():
            return bool(get_settings(str(uid)).get("reversal_charts", REVERSAL_CHARTS))
    except Exception:
        pass
    return bool(REVERSAL_CHARTS)

def set_reversal_charts(enabled: bool, uid: Optional[str] = None):
    global REVERSAL_CHARTS
    REVERSAL_CHARTS = bool(enabled)
    try:
        if uid is not None and "set_setting" in globals():
            set_setting(str(uid), "reversal_charts", bool(enabled))
    except Exception:
        pass
    return bool(enabled)

def reversal_signal_reason(symbol):
    return "Drop + Base + Compression + Trendline Break + RVOL + RS/BTC + 2R"

import os
import re
import json
import time
import asyncio
import inspect
import subprocess
import threading
import gc
try:
    import fcntl
except Exception:
    fcntl = None
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

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
# Matplotlib is intentionally lazy-imported only when charts are ON.
# Importing it at startup costs a lot of RAM on Railway even when Charts OFF.
plt = None
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters

BOT_VERSION = os.getenv("BOT_VERSION", "0127")
EXCHANGE_PING_TIMEOUT_SEC = float(os.getenv("EXCHANGE_PING_TIMEOUT_SEC", "2.0"))
EXCHANGE_PING_TIMEOUT_MS = int(os.getenv("EXCHANGE_PING_TIMEOUT_MS", "2000"))
OLLAMA_KEEP_ALIVE_DEFAULT = os.getenv("OLLAMA_KEEP_ALIVE", "10m")
AI_APPROVAL_TOP_LIMIT = int(os.getenv("AI_APPROVAL_TOP_LIMIT", "5"))
AI_SEMAPHORE = asyncio.Semaphore(int(os.getenv("AI_MAX_CONCURRENT", "1")))
AI_CHAT_OPTIONS = {"temperature": 0.2, "num_predict": int(os.getenv("AI_CHAT_NUM_PREDICT", "120")), "chat_mode": True}
AI_APPROVAL_OPTIONS = {"temperature": 0.1, "num_predict": int(os.getenv("AI_APPROVAL_NUM_PREDICT", "120"))}


REVERSAL_AI_SYSTEM_PROMPT = """You are validating a crypto reversal breakout setup.

Analyze:
- Prior strong selloff
- Accumulation/base formation
- Compression before breakout
- Breakout quality
- RVOL increase
- RS/BTC strength
- BTC market stability
- Distance to resistance
- Risk/reward quality
- Structure cleanliness

Reject setups with:
- Late breakout
- Weak volume
- Resistance too close
- Choppy structure
- Exhausted breakout candle
- Overextended move
- Poor RR (<2R)

Respond ONLY with:
APPROVE or REJECT
Confidence: X%
Reason: short 1 sentence
"""

REVERSAL_AI_JSON_RULES = """Reversal Mode validation rules:
- Validate ONLY reversal breakout setups.
- APPROVE only if prior strong selloff, base/accumulation, compression, breakout quality, RVOL increase, positive RS/BTC, BTC stability, clean structure, and at least 2R to resistance are present.
- REJECT late breakouts, weak/declining volume, resistance too close, choppy structure, exhausted breakout candle, overextended moves, and poor RR (<2R).
- Keep reason short: one sentence.
"""

HYBRID_AI_SYSTEM_PROMPT = """You are validating a hybrid crypto setup.

The setup may contain:
- reversal breakout characteristics
- momentum continuation characteristics
- or both combined

Strongest setups:
- REVERSAL + MOMENTUM alignment
- Rising RVOL
- Positive RS/BTC
- Clean breakout structure
- Healthy BTC market
- Minimum RR >= 2

Reject:
- weak momentum
- late breakout
- exhausted candle
- weak volume
- poor RR
- choppy structure

Respond ONLY with:
APPROVE or REJECT
Confidence: X%
Reason: short 1 sentence
"""

HYBRID_AI_JSON_RULES = """Hybrid Mode validation rules:
- Validate LONG crypto setups that may be REVERSAL, MOMENTUM, or REVERSAL+MOMENTUM.
- Highest priority: candidates where setup is REVERSAL+MOMENTUM or priority is HIGH.
- APPROVE if the candidate has clean structure, rising RVOL, positive RS/BTC, healthy BTC context, and acceptable RR.
- REJECT weak momentum, late/exhausted breakouts, weak or declining volume, resistance too close, choppy structure, and poor RR.
- Do not force approval: only include high-quality candidates.
- Keep reason short: one sentence.
"""
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
SCAN_REQUEST_PAUSE = float(os.getenv("SCAN_REQUEST_PAUSE", "0.55"))
SCAN_SYMBOL_TIMEOUT = float(os.getenv("SCAN_SYMBOL_TIMEOUT", "12"))
MAX_OHLCV_LIMIT = int(os.getenv("MAX_OHLCV_LIMIT", "160"))
SCAN_GC_EVERY = int(os.getenv("SCAN_GC_EVERY", "25"))
MAX_LIQUIDITY_CACHE = int(os.getenv("MAX_LIQUIDITY_CACHE", "300"))
MAX_INSTITUTIONAL_CACHE = int(os.getenv("MAX_INSTITUTIONAL_CACHE", "300"))
MARKETS_CACHE_TTL = int(os.getenv("MARKETS_CACHE_TTL", "21600"))
MAX_LAST_SCAN_RESULTS = int(os.getenv("MAX_LAST_SCAN_RESULTS", "25"))
MAX_AI_CONFIRMED_RESULTS = int(os.getenv("MAX_AI_CONFIRMED_RESULTS", "25"))
EXCHANGE_TIMEOUT_MS = int(os.getenv("EXCHANGE_TIMEOUT_MS", "8000"))
_MARKETS_CACHE: Dict[str, Dict[str, Any]] = {}
_MARKETS_CACHE_LOCK = threading.RLock()
_PUBLIC_EXCHANGE_LOCAL = threading.local()
MAX_CANDIDATE_REASONS = int(os.getenv("MAX_CANDIDATE_REASONS", "8"))


def get_public_thread_exchange(exchange_name: str):
    """Reuse one public CCXT instance per worker thread.

    Top-200 scans call fetch_ohlcv hundreds of times. Creating a new CCXT
    object for every candle request allocates sessions/rate-limit state and can
    make Railway memory grow without improving signal quality. Public requests
    do not need user API keys, so a thread-local instance is safe for scanner
    OHLCV while private trading continues to use create_exchange(uid).
    """
    exchange_name = str(exchange_name or DEFAULT_EXCHANGE).lower().strip()
    pool = getattr(_PUBLIC_EXCHANGE_LOCAL, "pool", None)
    if pool is None:
        pool = {}
        _PUBLIC_EXCHANGE_LOCAL.pool = pool
    ex = pool.get(exchange_name)
    if ex is None:
        ex = create_exchange(exchange_name)
        pool[exchange_name] = ex
    return ex


def compact_scan_candidate(item: Dict[str, Any]) -> Dict[str, Any]:
    """Drop bulky/non-actionable scanner fields without changing trade logic.

    Keeps every field needed for AI approval and auto execution: symbol,
    direction, score, setup, RR, reversal SL/TP levels, structural summary,
    hybrid priority and short reasons. Removes accidental raw payloads/DataFrame
    style objects if a filter added them.
    """
    if not isinstance(item, dict):
        return {}
    keep = {
        "symbol", "direction", "score", "scanner_score", "scan_phase", "hybrid",
        "hybrid_priority", "priority_label", "hybrid_match", "setup",
        "confidence", "success_probability", "reason",
        "rr", "mtf_confirmed", "extended_tp_mode", "tp_profile",
        "dynamic_rr", "rr_profile", "stop_loss", "take_profit",
        "tp1", "tp2", "tp3", "reversal_rr", "extended_tp_rr",
    }
    out = {k: v for k, v in item.items() if k in keep}
    reasons = item.get("reasons") or []
    if isinstance(reasons, (list, tuple)):
        out["reasons"] = [str(x)[:180] for x in list(reasons)[:MAX_CANDIDATE_REASONS]]
    elif reasons:
        out["reasons"] = [str(reasons)[:180]]
    for key in ("reversal", "structural"):
        val = item.get(key)
        if isinstance(val, dict):
            slim = {}
            for k, v in val.items():
                if k in {"sl", "tp1", "tp2", "tp3", "rr", "summary", "touches", "strength", "rvol"}:
                    slim[k] = v if not isinstance(v, str) else v[:220]
            if slim:
                out[key] = slim
    return out


def prune_runtime_caches() -> None:
    """Bound non-critical runtime caches and ask Python to release cyclic garbage.

    Keeps Top-200 enabled, but prevents old liquidity/institutional entries and
    completed scan objects from accumulating across scan cycles.
    """
    now = time.time()
    try:
        cache = globals().get("LIQUIDITY_CACHE")
        if isinstance(cache, dict):
            for k in list(cache.keys()):
                item = cache.get(k) or {}
                if now - float(item.get("ts", 0)) > float(globals().get("LIQUIDITY_CACHE_TTL", 180)):
                    cache.pop(k, None)
            max_items = int(globals().get("MAX_LIQUIDITY_CACHE", 300))
            if len(cache) > max_items:
                for k, _ in sorted(cache.items(), key=lambda kv: float((kv[1] or {}).get("ts", 0)))[: len(cache) - max_items]:
                    cache.pop(k, None)
    except Exception:
        pass
    try:
        cache = globals().get("INSTITUTIONAL_CACHE")
        if isinstance(cache, dict):
            for k in list(cache.keys()):
                item = cache.get(k) or {}
                if now - float(item.get("ts", 0)) > float(globals().get("INSTITUTIONAL_CACHE_TTL", 120)):
                    cache.pop(k, None)
            max_items = int(globals().get("MAX_INSTITUTIONAL_CACHE", 300))
            if len(cache) > max_items:
                for k, _ in sorted(cache.items(), key=lambda kv: float((kv[1] or {}).get("ts", 0)))[: len(cache) - max_items]:
                    cache.pop(k, None)
    except Exception:
        pass
    try:
        max_scan = int(globals().get("MAX_LAST_SCAN_RESULTS", 25))
        for uid_key in list(globals().get("LAST_SCAN_RESULTS", {}).keys()):
            items = globals()["LAST_SCAN_RESULTS"].get(uid_key) or []
            globals()["LAST_SCAN_RESULTS"][uid_key] = list(items[:max_scan])
        max_ai = int(globals().get("MAX_AI_CONFIRMED_RESULTS", 25))
        for uid_key in list(globals().get("LAST_AI_CONFIRMED", {}).keys()):
            items = globals()["LAST_AI_CONFIRMED"].get(uid_key) or []
            globals()["LAST_AI_CONFIRMED"][uid_key] = list(items[:max_ai])
    except Exception:
        pass
    try:
        gc.collect()
    except Exception:
        pass

def _default_data_dir() -> Path:
    """Persistent data directory.

    Railway often needs a mounted volume (commonly /data).  If DATA_DIR is not
    set and /data exists or can be created, use it.  This prevents settings/API
    keys from disappearing when the working directory is recreated.
    """
    env_dir = os.getenv("DATA_DIR")
    if env_dir:
        return Path(env_dir)
    try:
        d = Path("/data")
        d.mkdir(parents=True, exist_ok=True)
        if os.access(str(d), os.W_OK):
            return d
    except Exception:
        pass
    return Path("data")

DATA_DIR = _default_data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)
SETTINGS_FILE = DATA_DIR / "settings.json"
API_KEYS_FILE = DATA_DIR / "api_keys.json"
PROXY_FILE = DATA_DIR / "proxies.json"
OPENAI_KEYS_FILE = DATA_DIR / "openai_keys.json"
OPENAI_ENV_FALLBACK = str(os.getenv("OPENAI_ENV_FALLBACK", "0")).lower() in ["1", "true", "yes", "on"]
POSITIONS_FILE = DATA_DIR / "positions.json"
COOLDOWN_FILE = DATA_DIR / "cooldown.json"
TRADE_EVENTS_FILE = DATA_DIR / "trade_events.json"
WORK_MESSAGE_IDS_FILE = DATA_DIR / "work_message_ids.json"
SETTINGS_LOCK = threading.RLock()
SETTINGS_CACHE: Optional[Dict[str, Dict[str, Any]]] = None

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")

TRADING_CHAT_SYSTEM_PROMPT = """
Ты crypto trading assistant.
Отвечай очень кратко, без Markdown, без звездочек, без списков с точками и без длинных объяснений.
Формат ответа строго такой:
Монета ТФ
LONG: xx%
SHORT: xx%
Стоп: цена или условие
Тейки: TP1 / TP2 / TP3
Проходимость: xx%

Правила:
- Тикеры BTC, ETH, SOL, XRP, BNB, DOGE, PEPE, TAO, SUI, ADA, AVAX и другие это криптовалюты.
- Не пиши биографии, новости и лишний текст.
- Не обещай гарантированную прибыль.
- Если нет точной рыночной цены, не выдумывай цену, пиши условие.
- Максимум 6 строк.
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



def is_ai_chat_control_text(text: str) -> bool:
    """Texts that must never be sent to AI chat."""
    t = (text or "").strip().lower()
    controls = {
        "меню", "menu", "help", "помощь", "выход", "exit", "/exit", "стоп",
        "status", "статус", "positions", "позиции", "ping", "пинг",
    }
    if t in controls:
        return True
    if t.startswith("/"):
        return True
    return False

async def quick_trade_chat_answer(uid: str, text: str, context: Optional[ContextTypes.DEFAULT_TYPE] = None, chat_id: Optional[int] = None) -> Optional[str]:
    """Fast local replies for AI chat. Return None to call the LLM.
    This prevents bot control words/buttons from being routed into the model.
    """
    t = (text or "").strip().lower()
    if t in {"exit", "/exit", "выход"}:
        set_setting(uid, "mode", "signal")
        return "✅ AI Chat OFF. Бот вернулся в обычный режим. Auto Scanner продолжает работать."
    if t in {"меню", "menu"}:
        set_setting(uid, "mode", "signal")
        return "✅ AI Chat OFF. Открываю меню."
    if t in {"help", "помощь"}:
        return help_text()
    if t in {"status", "статус"}:
        return get_status_text(uid)
    if t in {"positions", "позиции"}:
        try:
            return await positions_text(uid)
        except Exception as e:
            return f"❌ Positions error: {compact_exchange_error(e, 500)}"
    if t in {"ping", "пинг"}:
        try:
            exchange_health = await check_exchange_api(uid)
            st = get_settings(uid)
            return f"📡 Ping OK\n🏦 API {st.get('exchange','').upper()}: {exchange_health}\n📦 Version: {BOT_VERSION}"
        except Exception as e:
            return f"❌ Ping error: {compact_exchange_error(e, 500)}"
    return None

def sanitize_ai_chat_answer(text: str) -> str:
    """Clean AI chat output for Telegram: no markdown/code fences, no reasoning garbage, max 6 useful lines."""
    t = str(text or "").strip()
    if not t:
        return ""
    # Remove common LLM wrappers / markdown.
    t = re.sub(r"```(?:json|text|markdown)?", "", t, flags=re.I).replace("```", "")
    t = t.replace("**", "").replace("__", "").replace("`", "")
    t = re.sub(r"^[\s>*•\-]+", "", t, flags=re.M)
    # Drop thinking/meta lines that some local models add.
    bad_prefixes = ("мысл", "рассуж", "reason", "analysis", "ответ:", "итог:", "как ии", "я не", "конечно")
    lines = []
    for raw in t.splitlines():
        line = re.sub(r"\s+", " ", raw).strip(" .\t")
        if not line:
            continue
        low = line.lower()
        if any(low.startswith(x) for x in bad_prefixes):
            continue
        # Keep only compact trading-oriented lines.
        lines.append(line[:160])
        if len(lines) >= 6:
            break
    cleaned = "\n".join(lines).strip()
    return cleaned[:1200]

OLLAMA_MODELS = [x.strip() for x in os.getenv("OLLAMA_MODELS", "llama3.1:8b,deepseek-r1:8b").split(",") if x.strip()]
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "llama3.1:8b")
DEFAULT_EXCHANGE = os.getenv("DEFAULT_EXCHANGE", "mexc").lower()
SUPPORTED_EXCHANGES = {"mexc", "bingx", "binance"}

LAST_SCAN_RESULTS: Dict[int, List[Dict[str, Any]]] = {}
LAST_AI_CONFIRMED: Dict[int, List[Dict[str, Any]]] = {}
CURRENT_AI_UID: Optional[str] = None
USER_SCAN_TASKS: Dict[str, asyncio.Task] = {}
USER_SCAN_LOCKS: Dict[str, bool] = {}

def register_user_scan_task(uid: str, task: asyncio.Task) -> asyncio.Task:
    """Register one active scan task per user and remove it as soon as it finishes.

    This prevents finished asyncio Task objects (including their traceback locals,
    scan results, and DataFrames) from being retained in USER_SCAN_TASKS.
    """
    uid = str(uid)
    USER_SCAN_TASKS[uid] = task

    def _cleanup(done_task: asyncio.Task, _uid: str = uid) -> None:
        try:
            if USER_SCAN_TASKS.get(_uid) is done_task:
                USER_SCAN_TASKS.pop(_uid, None)
            # Consume exceptions so asyncio does not keep traceback chains alive.
            if done_task.done() and not done_task.cancelled():
                try:
                    done_task.exception()
                except Exception:
                    pass
        finally:
            try:
                prune_runtime_caches()
            except Exception:
                pass

    task.add_done_callback(_cleanup)
    return task

def cancel_user_scan(uid: str) -> bool:
    """Cancel any running manual/auto scan for this user and clear scan lock.
    Used by STOP ALL so scanner cannot keep consuming Railway/API resources.
    """
    uid = str(uid)
    cancelled = False
    task = USER_SCAN_TASKS.pop(uid, None)
    if task and not task.done():
        task.cancel()
        cancelled = True
    USER_SCAN_LOCKS.pop(uid, None)
    return cancelled

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
    "btc_trend_filter": False,
    "funding_filter": False,
    "open_interest_filter": False,
    "liquidity_filter": False,
    "heatmap_strength": False,
    "real_execution_enabled": os.getenv("DEFAULT_REAL_EXECUTION_ENABLED", "off").lower() == "on",
    "margin_mode": "isolated",
    "trade_mgmt_enabled": os.getenv("DEFAULT_TRADE_MGMT_ENABLED", "on").lower() == "on",
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
    "scan_mode": os.getenv("DEFAULT_SCAN_MODE", "momentum").lower() if os.getenv("DEFAULT_SCAN_MODE", "momentum").lower() in {"momentum", "reversal", "hybrid"} else "momentum",
    "reversal_charts": os.getenv("DEFAULT_REVERSAL_CHARTS", "off").lower() == "on",
    "hybrid_variant": os.getenv("DEFAULT_HYBRID_VARIANT", "light").lower() if os.getenv("DEFAULT_HYBRID_VARIANT", "light").lower() in {"light", "full"} else "light",
}

JSON_IO_LOCK = threading.RLock()

def _lock_file_handle(handle, exclusive: bool = False):
    if fcntl is not None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        except Exception:
            pass

def _unlock_file_handle(handle):
    if fcntl is not None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass

def load_json(path: Path, default):
    """Read JSON with process/thread-safe locking.

    Important: never silently resets state on read. If the file is temporarily
    busy/corrupted during write, return default for this call only; setters
    always reload under exclusive lock before saving.
    """
    try:
        path = Path(path)
        if not path.exists():
            return default
        with JSON_IO_LOCK:
            with path.open("r", encoding="utf-8") as f:
                _lock_file_handle(f, exclusive=False)
                try:
                    return json.load(f)
                finally:
                    _unlock_file_handle(f)
    except Exception:
        return default

def save_json(path: Path, data):
    """Atomic JSON save with cross-process lock.

    Prevents two background loops/callbacks or two bot processes from writing
    half-old/half-new settings over each other.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with JSON_IO_LOCK:
        with lock_path.open("a+", encoding="utf-8") as lock_f:
            _lock_file_handle(lock_f, exclusive=True)
            try:
                tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
                tmp.write_text(payload, encoding="utf-8")
                os.replace(tmp, path)
            finally:
                _unlock_file_handle(lock_f)

def update_json_file(path: Path, updater, default):
    """Atomic read-modify-write helper under one exclusive lock."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with JSON_IO_LOCK:
        with lock_path.open("a+", encoding="utf-8") as lock_f:
            _lock_file_handle(lock_f, exclusive=True)
            try:
                try:
                    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
                except Exception:
                    data = default
                if not isinstance(data, type(default)):
                    data = default
                new_data, result = updater(data)
                tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
                tmp.write_text(json.dumps(new_data, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(tmp, path)
                return result
            finally:
                _unlock_file_handle(lock_f)

def user_id(update: Update) -> str:
    return str(update.effective_user.id)

def _load_settings_cache_locked() -> Dict[str, Dict[str, Any]]:
    # Kept for compatibility with old callers, but reads fresh from disk.
    raw = load_json(SETTINGS_FILE, {})
    return raw if isinstance(raw, dict) else {}

def _flush_settings_cache_locked() -> None:
    # No-op: settings are written by set_setting/set_settings atomically.
    return None

def get_settings(uid: str) -> Dict[str, Any]:
    """Return effective settings without rewriting settings.json.

    The previous version wrote defaults on every read. If another callback or
    old process had stale defaults in memory, it could overwrite the user's real
    choice and the menu looked like it 'reset itself'. Reads are now read-only.
    """
    uid = str(uid)
    with SETTINGS_LOCK:
        data = _load_settings_cache_locked()
        current = data.get(uid, {})
        if not isinstance(current, dict):
            current = {}
        merged = dict(DEFAULT_SETTINGS)
        merged.update(current)
        return dict(merged)

def set_setting(uid: str, key: str, value):
    return set_settings(uid, {key: value})

def set_settings(uid: str, updates: Dict[str, Any]):
    uid = str(uid)
    updates = dict(updates or {})

    def updater(data):
        if not isinstance(data, dict):
            data = {}
        current = data.get(uid, {})
        if not isinstance(current, dict):
            current = {}
        s_eff = dict(DEFAULT_SETTINGS)
        s_eff.update(current)
        s_eff.update(updates)
        # Save only explicit/user values + existing values, not a fresh default
        # snapshot every read. This prevents defaults from resurrecting over UI.
        data[uid] = s_eff
        return data, dict(s_eff)

    with SETTINGS_LOCK:
        return update_json_file(SETTINGS_FILE, updater, {})

def persist_openai_key(uid: str, key: str):
    uid = str(uid)
    key = str(key or "").strip()
    def updater(data):
        if not isinstance(data, dict):
            data = {}
        data[uid] = key
        return data, True
    return update_json_file(OPENAI_KEYS_FILE, updater, {})

def delete_openai_key(uid: str):
    uid = str(uid)
    def updater(data):
        if not isinstance(data, dict):
            data = {}
        existed = uid in data
        data.pop(uid, None)
        return data, existed
    return update_json_file(OPENAI_KEYS_FILE, updater, {})

def get_saved_openai_key(uid: str) -> str:
    keys = load_json(OPENAI_KEYS_FILE, {})
    if isinstance(keys, dict):
        return str(keys.get(str(uid)) or "").strip()
    return ""

def get_openai_key(uid: str, allow_env: Optional[bool] = None) -> str:
    # v0071: do not silently treat Railway/global OPENAI_API_KEY as a user key.
    # This caused /testai to show that a key exists after the user had not saved one.
    # To intentionally use a deployment-wide key, set OPENAI_ENV_FALLBACK=1.
    saved_key = get_saved_openai_key(uid)
    if saved_key:
        return saved_key
    if OPENAI_ENV_FALLBACK if allow_env is None else allow_env:
        return str(os.getenv("OPENAI_API_KEY", "")).strip()
    return ""

def normalize_symbol(symbol: str) -> str:
    s = symbol.upper().replace("/", "").replace(":USDT", "").replace("_", "")
    if not s.endswith("USDT"):
        s += "USDT"
    return s

def mexc_contract_symbol(symbol: str) -> str:
    """Return MEXC futures raw contract symbol format, e.g. btc_usdt.

    The bot keeps internal symbols as BTCUSDT and uses CCXT unified symbols
    like BTC/USDT:USDT for ccxt calls, but MEXC private Futures payloads may
    require the raw contract symbol as btc_usdt.
    """
    norm = normalize_symbol(symbol)
    if norm.endswith("USDT") and len(norm) > 4:
        return f"{norm[:-4]}_USDT".lower()
    return str(symbol or "").replace("/", "_").replace(":", "_").lower()

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

def mask_proxy_url(proxy_url: str) -> str:
    """Mask proxy credentials for chat output/logs."""
    try:
        from urllib.parse import urlsplit, urlunsplit
        u = urlsplit(str(proxy_url or ""))
        if not u.scheme or not u.hostname:
            return "not set"
        host = u.hostname or ""
        port = f":{u.port}" if u.port else ""
        if u.username:
            netloc = f"{u.username}:***@{host}{port}"
        else:
            netloc = f"{host}{port}"
        return urlunsplit((u.scheme, netloc, "", "", ""))
    except Exception:
        return "***"

def normalize_proxy_url(proxy_url: str) -> str:
    """Validate and normalize proxy URL for requests/ccxt.

    Supported examples:
      socks5://login:pass@host:port
      http://login:pass@host:port
      https://login:pass@host:port
    """
    from urllib.parse import urlsplit
    proxy_url = str(proxy_url or "").strip()
    if not proxy_url:
        raise ValueError("empty proxy url")
    u = urlsplit(proxy_url)
    if u.scheme not in {"socks5", "socks5h", "http", "https"}:
        raise ValueError("proxy must start with socks5://, socks5h://, http:// or https://")
    if not u.hostname or not u.port:
        raise ValueError("proxy must include host and port")
    return proxy_url

def get_user_proxy(uid: Optional[str]) -> Optional[str]:
    if not uid:
        return None
    data = load_json(PROXY_FILE, {})
    val = data.get(str(uid))
    if isinstance(val, dict):
        val = val.get("proxy")
    if not val:
        return None
    try:
        return normalize_proxy_url(str(val))
    except Exception:
        return None

def set_user_proxy(uid: str, proxy_url: str) -> str:
    proxy_url = normalize_proxy_url(proxy_url)
    data = load_json(PROXY_FILE, {})
    data[str(uid)] = {"proxy": proxy_url, "updated_at": int(time.time())}
    save_json(PROXY_FILE, data)
    return proxy_url

def delete_user_proxy(uid: str) -> bool:
    data = load_json(PROXY_FILE, {})
    existed = str(uid) in data
    if existed:
        data.pop(str(uid), None)
        save_json(PROXY_FILE, data)
    return existed

def requests_proxy_kwargs(uid: Optional[str]) -> Dict[str, Any]:
    proxy = get_user_proxy(uid)
    if not proxy:
        return {}
    return {"proxies": {"http": proxy, "https": proxy}}

def create_exchange(exchange_name: str, uid: Optional[str] = None):
    exchange_name = str(exchange_name or DEFAULT_EXCHANGE).lower().strip()
    if exchange_name not in SUPPORTED_EXCHANGES:
        raise ValueError(f"Unsupported exchange: {exchange_name}. Supported: MEXC/BingX/Binance")
    cls = getattr(ccxt, exchange_name)
    options = {"defaultType": "swap"}
    if exchange_name == "binance":
        # Binance futures through ccxt uses USD-M futures with defaultType=future.
        options = {"defaultType": "future"}
    # recvWindow/time sync help avoid false signed-request rejects.
    options = dict(options)
    options.setdefault("recvWindow", int(os.getenv("MEXC_RECV_WINDOW", "10000")))
    options.setdefault("adjustForTimeDifference", True)
    params = {
        "enableRateLimit": True,
        "options": options,
        "headers": {
            "User-Agent": os.getenv("BOT_USER_AGENT", "Mozilla/5.0 (X11; Linux x86_64) trading-bot/1.0")
        },
        "timeout": EXCHANGE_TIMEOUT_MS,
    }
    proxy = get_user_proxy(uid)
    if proxy:
        # CCXT sync Python uses requests; this proxies private MEXC calls too.
        params["proxies"] = {"http": proxy, "https": proxy}
    if uid:
        keys = load_json(API_KEYS_FILE, {})
        if uid in keys and exchange_name in keys[uid]:
            params["apiKey"] = keys[uid][exchange_name].get("apiKey", "")
            params["secret"] = keys[uid][exchange_name].get("secret", "")
    ex_obj = cls(params)

    # MEXC support confirmed that some networks/CDN paths return HTTP 403 for
    # Futures private calls through contract.mexc.com. Force ccxt's MEXC
    # Futures endpoints to api.mexc.com so order submit uses:
    # https://api.mexc.com/api/v1/private/order/submit
    # instead of:
    # https://contract.mexc.com/api/v1/private/order/submit
    if exchange_name == "mexc":
        try:
            api_urls = ex_obj.urls.setdefault("api", {})
            contract_urls = api_urls.setdefault("contract", {})
            contract_urls["public"] = os.getenv("MEXC_CONTRACT_PUBLIC_URL", "https://api.mexc.com/api/v1/contract")
            contract_urls["private"] = os.getenv("MEXC_CONTRACT_PRIVATE_URL", "https://api.mexc.com/api/v1/private")
        except Exception:
            pass

    if proxy:
        try:
            ex_obj.proxies = {"http": proxy, "https": proxy}
        except Exception:
            pass
    return ex_obj

def get_cached_markets(exchange_name: str) -> Dict[str, Any]:
    """Cache exchange markets and prevent concurrent load_markets stampedes.

    Top-200 with Semaphore(5) can start several worker threads at once. If the
    cache is cold and the lock is released before load_markets(), each worker
    loads the full MEXC markets list. That wastes RAM and delays the first scan.
    Holding this small critical section during the first load keeps quality the
    same while avoiding duplicate market objects.
    """
    exchange_name = str(exchange_name or DEFAULT_EXCHANGE).lower().strip()
    now = time.time()
    with _MARKETS_CACHE_LOCK:
        cached = _MARKETS_CACHE.get(exchange_name)
        if cached and now - float(cached.get("ts", 0)) < MARKETS_CACHE_TTL:
            return cached.get("markets", {})
        ex = get_public_thread_exchange(exchange_name)
        markets = ex.load_markets()
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
    # Top-200 scanning can create hundreds of DataFrames per cycle.
    # Cap candles by default to keep RAM stable; indicators need far less than 200 candles.
    try:
        limit = max(30, min(int(limit), int(MAX_OHLCV_LIMIT)))
    except Exception:
        limit = 160
    ex = get_public_thread_exchange(exchange_name)
    market_symbol = market_symbol_from_cache(exchange_name, symbol)
    data = _retry_blocking_request(lambda: ex.fetch_ohlcv(market_symbol, timeframe=timeframe, limit=limit))
    df = pd.DataFrame(data, columns=["timestamp", "open", "high", "low", "close", "volume"])
    if len(df) > limit:
        df = df.tail(limit).reset_index(drop=True)
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


def _linreg(values: List[float]) -> Tuple[float, float]:
    n = len(values)
    if n < 2:
        return 0.0, values[-1] if values else 0.0
    xs = list(range(n))
    xm = sum(xs) / n
    ym = sum(values) / n
    den = sum((x - xm) ** 2 for x in xs) or 1e-9
    m = sum((xs[i] - xm) * (values[i] - ym) for i in range(n)) / den
    b = ym - m * xm
    return m, b

def _pivot_levels(df: pd.DataFrame, lookback: int = 80) -> Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]:
    recent = df.tail(lookback).copy()
    highs = recent["high"].astype(float).tolist()
    lows = recent["low"].astype(float).tolist()
    ph, pl = [], []
    for i in range(2, len(recent) - 2):
        if highs[i] >= max(highs[i-2:i]) and highs[i] >= max(highs[i+1:i+3]):
            ph.append((i, highs[i]))
        if lows[i] <= min(lows[i-2:i]) and lows[i] <= min(lows[i+1:i+3]):
            pl.append((i, lows[i]))
    return ph, pl

def score_reversal_market(exchange_name: str, symbol: str, settings: Dict[str, Any], df15: pd.DataFrame) -> Dict[str, Any]:
    """Bullish reversal breakout scanner based on the video logic.

    HTF context: 1H + 4H for selloff/base/resistance/BTC.
    LTF trigger: 15m for breakout/RVOL/entry.
    Returns LONG only or WAIT.
    """
    reasons: List[str] = []
    metrics: Dict[str, Any] = {}
    score = 0.0
    direction = "WAIT"
    try:
        df15 = add_indicators(df15).copy()
        df1h = add_indicators(fetch_ohlcv_for_symbol(exchange_name, symbol, "1h", 180))
        df4h = add_indicators(fetch_ohlcv_for_symbol(exchange_name, symbol, "4h", 120))
        btc15 = add_indicators(fetch_ohlcv_for_symbol(exchange_name, "BTCUSDT", "15m", 120))
    except Exception as e:
        return {"direction": "WAIT", "score": 0, "price": None, "reasons": [f"reversal data error: {str(e)[:100]}"], "setup": "REVERSAL"}

    try:
        price = float(df15["close"].iloc[-1])
        atr15 = safe_float(df15["atr"].iloc[-1], price * 0.006) or price * 0.006
        htf_high_lookback = df4h.tail(80)["high"].astype(float)
        htf_low_lookback = df4h.tail(80)["low"].astype(float)
        recent_close_4h = float(df4h["close"].iloc[-1])
        prior_high = float(htf_high_lookback.max())
        prior_low = float(htf_low_lookback.min())
        drop_pct = (prior_high / max(prior_low, 1e-12) - 1.0) * 100.0
        metrics["prior_drop_pct"] = round(drop_pct, 2)
        if drop_pct >= 12:
            score += 14; reasons.append(f"Prior selloff {drop_pct:.1f}%")
        elif drop_pct >= 7:
            score += 8; reasons.append(f"Moderate prior selloff {drop_pct:.1f}%")
        else:
            reasons.append("Prior selloff too small")

        # Accumulation/base: last 30 1h candles have relatively tight range and price holds above base low.
        base = df1h.tail(36).copy()
        base_high = float(base["high"].max())
        base_low = float(base["low"].min())
        base_mid = (base_high + base_low) / 2
        base_range_pct = (base_high / max(base_low, 1e-12) - 1.0) * 100
        metrics.update({"base_high": base_high, "base_low": base_low, "base_range_pct": round(base_range_pct, 2)})
        base_ok = base_range_pct <= max(10.0, drop_pct * 0.45) and price > base_low * 1.01
        if base_ok:
            score += 16; reasons.append(f"Accumulation/base {base_range_pct:.1f}%")
        else:
            reasons.append("Base not clean")

        # Compression: ATR/range shrinking before breakout.
        atr_now = safe_float(df1h["atr"].tail(12).mean(), 0)
        atr_prev = safe_float(df1h["atr"].iloc[-48:-24].mean(), 0)
        compression_ratio = atr_now / atr_prev if atr_prev else 1.0
        metrics["compression_ratio"] = round(compression_ratio, 3)
        if compression_ratio <= 0.82:
            score += 12; reasons.append(f"Compression {compression_ratio:.2f}")
        elif compression_ratio <= 1.0:
            score += 6; reasons.append("Mild compression")
        else:
            reasons.append("No compression")

        # Descending trendline from recent 1h pivot highs, breakout on 15m/last price.
        ph, pl = _pivot_levels(df1h, 90)
        recent_ph = ph[-6:]
        trendline = None
        trendline_ok = False
        if len(recent_ph) >= 2:
            xs = [x for x, _ in recent_ph]
            ys = [y for _, y in recent_ph]
            m, b = _linreg(ys)  # pivot sequence line, not absolute candle index
            # convert to current pivot-sequence value: last sequence point + 1
            current_line = m * (len(ys)) + b
            descending = m < 0
            trendline = {"slope": m, "current": float(current_line), "pivots": recent_ph}
            tolerance = max(atr15 * 0.35, price * 0.002)
            trendline_ok = bool(descending and price > current_line + tolerance)
        metrics["trendline"] = trendline or {}
        if trendline_ok:
            score += 16; reasons.append("Descending trendline breakout")
        else:
            reasons.append("No clean trendline breakout")

        # LTF breakout: close above recent 15m range.
        last_close = price
        recent_high = float(df15["high"].iloc[-28:-1].max())
        breakout_ok = last_close > recent_high
        metrics["recent_15m_high"] = recent_high
        if breakout_ok:
            score += 14; reasons.append("15m breakout confirmed")
        else:
            reasons.append("15m breakout not confirmed")

        # RVOL growth.
        rvol = safe_float(df15["volume"].iloc[-1], 0) / max(safe_float(df15["vol_ma"].iloc[-1], 0), 1e-12)
        metrics["rvol"] = round(rvol, 2)
        if rvol >= 2.0:
            score += 12; reasons.append(f"RVOL {rvol:.2f}x")
        elif rvol >= 1.35:
            score += 6; reasons.append(f"RVOL rising {rvol:.2f}x")
        else:
            reasons.append("Weak RVOL")

        # RS/BTC: 15m recent performance better than BTC.
        coin_ret = (float(df15["close"].iloc[-1]) / float(df15["close"].iloc[-16]) - 1.0) * 100
        btc_ret = (float(btc15["close"].iloc[-1]) / float(btc15["close"].iloc[-16]) - 1.0) * 100
        rs_btc = coin_ret - btc_ret
        metrics.update({"coin_ret_15": round(coin_ret, 2), "btc_ret_15": round(btc_ret, 2), "rs_btc": round(rs_btc, 2)})
        if rs_btc > 0.7:
            score += 10; reasons.append(f"RS/BTC +{rs_btc:.2f}%")
        elif rs_btc > 0:
            score += 5; reasons.append("RS/BTC positive")
        else:
            reasons.append("RS/BTC weak")

        # BTC filter: BTC not dumping hard over last 16x15m and not under strong short EMA panic.
        btc_ema_ok = float(btc15["ema20"].iloc[-1]) >= float(btc15["ema50"].iloc[-1]) * 0.995
        btc_ok = btc_ret > -1.2 and btc_ema_ok
        metrics["btc_filter"] = bool(btc_ok)
        if btc_ok:
            score += 8; reasons.append("BTC filter OK")
        else:
            reasons.append("BTC weak / risk-off")

        # Resistance and RR. nearest resistance is HTF pivot/high above price.
        htf_res_candidates = [v for _, v in ph[-10:] if v > price * 1.003]
        htf_res_candidates += [base_high] if base_high > price * 1.003 else []
        htf_res_candidates += [prior_high] if prior_high > price * 1.003 else []
        resistance = min(htf_res_candidates) if htf_res_candidates else price + max(atr15 * 6, price * 0.04)
        sl = min(base_low, float(df15["low"].tail(18).min())) - max(atr15 * 0.35, price * 0.002)
        if sl <= 0 or sl >= price:
            sl = price - max(atr15 * 1.6, price * 0.01)
        risk = max(price - sl, price * 0.002)
        rr_to_res = (resistance - price) / risk
        metrics.update({"resistance": resistance, "sl": sl, "rr": round(rr_to_res, 2), "risk": risk})
        if rr_to_res >= 2.0:
            score += 18; reasons.append(f"RR {rr_to_res:.2f}R to resistance")
        else:
            reasons.append(f"RR too low {rr_to_res:.2f}R")

        # Exhaustion/fake breakout filters.
        candle_body = abs(float(df15["close"].iloc[-1]) - float(df15["open"].iloc[-1]))
        candle_range = max(float(df15["high"].iloc[-1]) - float(df15["low"].iloc[-1]), 1e-12)
        upper_wick = float(df15["high"].iloc[-1]) - max(float(df15["close"].iloc[-1]), float(df15["open"].iloc[-1]))
        exhausted = (upper_wick / candle_range > 0.55) or ((price / float(df15["close"].iloc[-8]) - 1.0) * 100 > 9.0)
        clean_candle = not exhausted and candle_body / candle_range >= 0.35
        metrics["clean_candle"] = bool(clean_candle)
        if clean_candle:
            score += 8; reasons.append("Clean breakout candle")
        else:
            score -= 10; reasons.append("Possible exhausted/fake breakout")

        hard_filters = [base_ok, trendline_ok, breakout_ok, rvol >= 1.25, rs_btc > 0, btc_ok, rr_to_res >= 2.0, clean_candle]
        min_score = safe_float(settings.get("min_score", 80), 80)
        if all(hard_filters) and score >= min_score:
            direction = "LONG"
        else:
            direction = "WAIT"

        # TP projection: base height projection + standard R ladder. Execution code still uses common SL/TP.
        base_height = max(base_high - base_low, risk)
        tp1 = price + risk * 1.5
        tp2 = price + risk * 2.5
        tp3 = max(price + risk * 4.0, base_high + base_height)
        metrics.update({"entry": price, "tp1": tp1, "tp2": tp2, "tp3": tp3, "base_height": base_height})

        return {
            "direction": direction,
            "score": round(max(0, min(100, score)), 1),
            "price": price,
            "reasons": reasons[:10],
            "setup": "REVERSAL BREAKOUT",
            "mtf_confirmed": bool(direction == "LONG"),
            "rvol": round(rvol, 2),
            "rs_btc": round(rs_btc, 2),
            "btc_filter": bool(btc_ok),
            "rr": round(rr_to_res, 2),
            "resistance_distance": round((resistance / price - 1) * 100, 2),
            "reversal": metrics,
            "structural": {"reversal": {"passed": direction == "LONG", "summary": "; ".join(reasons[:5])}},
            "volume_ratio": round(rvol, 2),
            "change": round(coin_ret, 2),
        }
    except Exception as e:
        return {"direction": "WAIT", "score": 0, "price": None, "reasons": [f"reversal engine error: {str(e)[:120]}"], "setup": "REVERSAL"}

def calculate_reversal_trade_levels(market: Dict[str, Any], df: pd.DataFrame) -> Dict[str, Any]:
    rev = market.get("reversal", {}) or {}
    entry = safe_float(rev.get("entry") or market.get("price"), 0)
    sl = safe_float(rev.get("sl"), 0)
    tp1 = safe_float(rev.get("tp1"), 0)
    tp2 = safe_float(rev.get("tp2"), 0)
    tp3 = safe_float(rev.get("tp3"), 0)
    if entry <= 0 or sl <= 0 or sl >= entry:
        price = entry or safe_float(df["close"].iloc[-1], 0)
        atr = safe_float(df["atr"].iloc[-1], price * 0.006) or price * 0.006
        entry = price; sl = entry - max(atr * 1.6, entry * 0.01)
    risk = max(entry - sl, entry * 0.002)
    if tp1 <= entry: tp1 = entry + risk * 1.5
    if tp2 <= entry: tp2 = entry + risk * 2.5
    if tp3 <= entry: tp3 = entry + risk * 4.0
    rr = (tp2 - entry) / risk if risk else None
    return {"side": "LONG", "entry": round(entry, 8), "sl": round(sl, 8), "tp1": round(tp1, 8), "tp2": round(tp2, 8), "tp3": round(tp3, 8), "rr": round(rr or 0, 2), "profile": "reversal_base_projection"}

def render_reversal_chart(symbol: str, df15: pd.DataFrame, market: Dict[str, Any], out_dir: Optional[Path] = None) -> Optional[str]:
    global plt
    try:
        # Lazy import: only load matplotlib when user enabled Charts.
        if plt is None:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as _plt
            plt = _plt
        out_dir = out_dir or Path("/tmp")
        out_dir.mkdir(parents=True, exist_ok=True)
        df = df15.tail(90).copy()
        if df.empty:
            return None
        rev = market.get("reversal", {}) or {}
        entry = safe_float(rev.get("entry") or market.get("price"), 0)
        sl = safe_float(rev.get("sl"), 0)
        tp1 = safe_float(rev.get("tp1"), 0)
        tp2 = safe_float(rev.get("tp2"), 0)
        tp3 = safe_float(rev.get("tp3"), 0)
        base_low = safe_float(rev.get("base_low"), 0)
        base_high = safe_float(rev.get("base_high"), 0)
        resistance = safe_float(rev.get("resistance"), 0)
        closes = df["close"].astype(float).tolist()
        highs = df["high"].astype(float).tolist()
        lows = df["low"].astype(float).tolist()
        xs = list(range(len(df)))
        fig, ax = plt.subplots(figsize=(9, 4.8))
        ax.plot(xs, closes, linewidth=1.6, label="Close")
        # simple candles as high-low lines for readability
        for i in range(0, len(df), max(1, len(df)//45)):
            ax.vlines(i, lows[i], highs[i], linewidth=0.6, alpha=0.55)
        if base_low and base_high and base_high > base_low:
            ax.axhspan(base_low, base_high, alpha=0.16, label="Accumulation zone")
        # approximate descending trendline from stored pivot highs if available
        tl = rev.get("trendline") or {}
        pivots = tl.get("pivots") or []
        if len(pivots) >= 2:
            # Map 1h pivot sequence visually onto chart width; approximate visual reference.
            pvals = [float(v) for _, v in pivots[-4:]]
            x0, x1 = int(len(xs)*0.15), int(len(xs)*0.86)
            ax.plot([x0, x1], [pvals[0], pvals[-1]], linestyle="--", linewidth=1.2, label="Descending trendline")
        for y, name in [(entry, "ENTRY"), (sl, "SL"), (tp1, "TP1"), (tp2, "TP2"), (tp3, "TP3"), (resistance, "Resistance")]:
            if y and y > 0:
                ax.axhline(y, linestyle="--" if name not in ["SL", "ENTRY"] else "-", linewidth=1.0)
                ax.text(len(xs)-1, y, f" {name} {y:.6g}", va="center", fontsize=8)
        ax.set_title(f"{symbol} REVERSAL BREAKOUT | Score {market.get('score')} | RR {market.get('rr')}")
        ax.grid(True, alpha=0.2)
        ax.legend(loc="upper left", fontsize=8)
        fig.tight_layout()
        path = out_dir / f"reversal_{normalize_symbol(symbol)}_{int(time.time()*1000)}.png"
        fig.savefig(path, dpi=130)
        plt.close(fig)
        return str(path)
    except Exception:
        try:
            plt.close("all")
        except Exception:
            pass
        return None

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

        api_key = get_openai_key(uid)
        return "✅ Key present" if api_key else "⚠️ OpenAI key missing"
    except Exception as e:
        return f"❌ {compact_exchange_error(e, 180)}"

async def check_exchange_api(uid: str, timeout_sec: Optional[float] = None) -> str:
    """Fast exchange health check with a hard timeout.

    Ping/menu must never wait 10-30s for a slow exchange/CDN response. This
    check is intentionally isolated from scanner/trading requests and uses a
    short ccxt timeout plus asyncio.wait_for.
    """
    s = get_settings(uid)
    timeout_sec = EXCHANGE_PING_TIMEOUT_SEC if timeout_sec is None else float(timeout_sec)
    started = time.perf_counter()
    ex = None
    try:
        ex = create_exchange(s["exchange"], uid)
        try:
            ex.timeout = EXCHANGE_PING_TIMEOUT_MS
        except Exception:
            pass

        async def _fetch(symbol: str):
            return await asyncio.wait_for(asyncio.to_thread(ex.fetch_ticker, symbol), timeout=timeout_sec)

        try:
            await _fetch("BTC/USDT:USDT")
        except Exception:
            await _fetch("BTC/USDT")
        return f"✅ OK ({round((time.perf_counter()-started)*1000)} ms)"
    except asyncio.TimeoutError:
        return f"⚠️ timeout >{round(timeout_sec, 1)}s"
    except Exception as e:
        return f"❌ {compact_exchange_error(e, 180)}"
    finally:
        if ex is not None:
            try:
                await asyncio.to_thread(ex.close)
            except Exception:
                pass

def local_fast_ping_text(uid: str, started: Optional[float] = None) -> str:
    """Local bot health text. Does not call MEXC/OpenAI, so menu stays responsive."""
    s = get_settings(uid)
    latency_ms = round((time.perf_counter() - (started or time.perf_counter())) * 1000, 2)
    return (
        f"📡 Local Ping: {latency_ms} ms\n"
        f"⏱ Время работы: {format_uptime(int(time.time() - START_TIME))}\n"
        f"🧠 Память: {memory_usage_text()}\n"
        f"🤖 Provider: {s.get('ai_provider')}\n"
        f"🧠 Модель ИИ: {get_active_model(s)}\n"
        f"📦 Version: {BOT_VERSION}"
    )

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
    """Extract visible text from OpenAI Responses API or Chat Completions API."""
    if not isinstance(data, dict):
        return ""

    if isinstance(data.get("output_text"), str) and data.get("output_text").strip():
        return data.get("output_text", "").strip()

    chunks: List[str] = []

    def walk(obj: Any):
        if isinstance(obj, dict):
            typ = str(obj.get("type", ""))
            # Responses API text blocks are usually type=output_text with a text field.
            if typ in {"output_text", "text"} and isinstance(obj.get("text"), str):
                chunks.append(obj["text"])
            elif isinstance(obj.get("content"), str):
                chunks.append(obj["content"])
            elif isinstance(obj.get("value"), str):
                chunks.append(obj["value"])
            for v in obj.values():
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(data.get("output", []))
    if chunks:
        text = "\n".join(x.strip() for x in chunks if x and x.strip()).strip()
        if text:
            return text

    choices = data.get("choices") or []
    if choices:
        msg = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            walk(content)
            text = "\n".join(x.strip() for x in chunks if x and x.strip()).strip()
            if text:
                return text

    return ""

def call_openai(uid: str, model: str, prompt: str, reasoning: str, system_prompt: Optional[str] = None, options: Optional[Dict[str, Any]] = None) -> str:
    api_key = get_openai_key(uid)
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
    responses_error = r.text[:500]
    if r.status_code == 200:
        text = _extract_openai_response_text(r.json())
        if text:
            return text
        responses_error = "200 OK, but empty visible text"

    # Compatibility fallback. Also used when Responses API returned an empty answer.
    chat_body = {
        "model": model,
        "messages": ([{"role": "system", "content": system_prompt}] if system_prompt else []) + [{"role": "user", "content": prompt}],
    }
    # New reasoning models often reject temperature/max_tokens; use max_completion_tokens.
    model_l = str(model or "").lower()
    if model_l.startswith(("gpt-5", "o1", "o3", "o4")):
        chat_body["max_completion_tokens"] = max_tokens
    else:
        chat_body["temperature"] = request_options.get("temperature", 0.2)
        chat_body["max_tokens"] = max_tokens

    r2 = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=chat_body, timeout=300)
    if r2.status_code == 200:
        text = _extract_openai_response_text(r2.json())
        if text:
            return text
        raise RuntimeError("OpenAI returned empty visible text after Responses and Chat fallback")

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

    if str(market.get("setup", "")).upper().startswith("REVERSAL") and direction == "LONG":
        return calculate_reversal_trade_levels(market, df)

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
    setup_name = str(market.get("setup", "")).upper()
    scan_mode_name = str(settings.get("scan_mode", get_scan_mode())).lower()
    if scan_mode_name == "hybrid" or "REVERSAL+MOMENTUM" in setup_name:
        return f"""
{HYBRID_AI_SYSTEM_PROMPT}

Setup data:
Symbol: {symbol}
Timeframe: {timeframe}
Exchange: {settings['exchange']}
Direction: {market.get('direction')}
Setup: {market.get('setup')}
Priority: {market.get('priority')}
Score: {market.get('score')}
Reversal Score: {market.get('reversal_score')}
Momentum Score: {market.get('momentum_score')}
Price: {market.get('price')}
MTF confirmed: {market.get('mtf_confirmed', True)}
Reasons: {market.get('reasons')}
Structural data: {market.get('structural')}
RVOL: {market.get('rvol')}
RS/BTC: {market.get('rs_btc')}
RR: {market.get('rr')}
Resistance distance: {market.get('resistance_distance')}
BTC filter: {market.get('btc_filter')}
"""
    if scan_mode_name == "reversal" or setup_name.startswith("REVERSAL"):
        return f"""
{REVERSAL_AI_SYSTEM_PROMPT}

Setup data:
Symbol: {symbol}
Timeframe: {timeframe}
Exchange: {settings['exchange']}
Direction: {market.get('direction')}
Score: {market.get('score')}
Price: {market.get('price')}
MTF confirmed: {market.get('mtf_confirmed', True)}
Reasons: {market.get('reasons')}
Structural data: {market.get('structural')}
RVOL: {market.get('rvol')}
RS/BTC: {market.get('rs_btc')}
RR: {market.get('rr')}
Resistance distance: {market.get('resistance_distance')}
BTC filter: {market.get('btc_filter')}
"""
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
    trade_mgmt_label = "ON" if settings.get("trade_mgmt_enabled", True) else "OFF"
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
            InlineKeyboardButton(f"⚙️ MODE: {str(settings.get('scan_mode', get_scan_mode())).upper()}", callback_data="menu:scanmode"),
            InlineKeyboardButton(("📊 Charts: ON" if settings.get('reversal_charts', get_reversal_charts()) else "📊 Charts: OFF"), callback_data="toggle:reversalcharts"),
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
            InlineKeyboardButton(f"🛡 Trade Mgmt: {trade_mgmt_label}", callback_data="menu:trademgmt"),
        ],
        [
            InlineKeyboardButton("🏦 Institutional Filters", callback_data="menu:institutional"),
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

def bottom_reply_keyboard() -> ReplyKeyboardMarkup:
    """Persistent ordinary keyboard under the input field."""
    return ReplyKeyboardMarkup(
        [["Меню", "/help"]],
        resize_keyboard=True,
        is_persistent=True
    )

async def ensure_bottom_reply_keyboard(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text="⌨️ Нижнее меню включено: Меню / /help",
            reply_markup=bottom_reply_keyboard()
        )
    except Exception:
        pass

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
    await ensure_bottom_reply_keyboard(context, chat_id)
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

def scanner_mode_menu(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    cur = str(settings.get("scan_mode", get_scan_mode())).lower()
    charts = bool(settings.get("reversal_charts", get_reversal_charts()))
    hybrid_variant = str(settings.get("hybrid_variant", get_hybrid_variant())).lower()
    if hybrid_variant not in {"light", "full"}:
        hybrid_variant = "light"
    hybrid_label = "Hybrid: LIGHT" if hybrid_variant == "light" else "Hybrid: FULL"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(("✅ " if cur == "momentum" else "") + "Momentum Scanner", callback_data="scanmode:momentum")],
        [InlineKeyboardButton(("✅ " if cur == "reversal" else "") + "Reversal Breakout", callback_data="scanmode:reversal")],
        [InlineKeyboardButton(("✅ " if cur == "hybrid" else "") + hybrid_label, callback_data="scanmode:hybrid")],
        [
            InlineKeyboardButton(("✅ " if hybrid_variant == "light" else "") + "Hybrid Light", callback_data="hybridvariant:light"),
            InlineKeyboardButton(("✅ " if hybrid_variant == "full" else "") + "Hybrid Full", callback_data="hybridvariant:full"),
        ],
        [InlineKeyboardButton(("📊 Reversal Charts: ON" if charts else "📊 Reversal Charts: OFF"), callback_data="toggle:reversalcharts")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back:main")],
    ])

def trading_mode_menu(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    modes = [("manual", "🟢 Manual"), ("confirm", "🟡 Confirm"), ("auto", "🔴 Auto")]
    return InlineKeyboardMarkup([[InlineKeyboardButton(("✅ " if settings.get("trading_mode") == m else "") + label, callback_data=f"tradingmode:{m}")] for m, label in modes] + [[InlineKeyboardButton("⬅️ Назад", callback_data="back:main")]])

def trade_mgmt_menu(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    trade_mgmt_on = bool(settings.get("trade_mgmt_enabled", True))
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(("✅ " if trade_mgmt_on else "❌ ") + "Trade Mgmt (SL/TP)", callback_data="toggle:trademgmt")],
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
    norm = normalize_symbol(raw_symbol)
    candidates = [norm.replace("USDT", "/USDT:USDT"), norm.replace("USDT", "/USDT")]
    try:
        markets = get_cached_markets(str(getattr(ex, "id", DEFAULT_EXCHANGE) or DEFAULT_EXCHANGE))
    except Exception as e:
        # Do not dump the full /contract/detail response into Telegram.
        # For swap symbols, try the standard CCXT form as a safe fallback.
        if norm.endswith("USDT"):
            return candidates[0]
        raise ValueError("Market metadata load failed: " + compact_exchange_error(e))
    for c in candidates:
        if c in markets:
            return c
    # Fallback for exchanges whose market cache is incomplete but accept CCXT swap syntax.
    if norm.endswith("USDT"):
        return candidates[0]
    raise ValueError(f"Symbol {norm} not found")

def get_usdt_free_balance(ex) -> float:
    """Return USDT free balance from futures/swap wallet first.

    This is intentionally futures-first so position sizing does not use spot
    or unified account totals by mistake. Falls back to default balance only
    if the exchange wrapper does not accept swap/futures params.
    """
    last_error = None
    balances = []
    for params in ({"type": "swap"}, {"type": "future"}, {}):
        try:
            balances.append(ex.fetch_balance(params) if params else ex.fetch_balance())
        except Exception as e:
            last_error = e

    for b in balances:
        if not isinstance(b, dict):
            continue
        for k in ["free", "total"]:
            p = b.get(k, {})
            if isinstance(p, dict) and "USDT" in p:
                val = safe_float(p["USDT"])
                if val > 0:
                    return val
        # MEXC futures sometimes returns useful USDT data inside info.
        info = b.get("info", {})
        if isinstance(info, dict):
            for key in ("availableBalance", "available", "cashBalance", "equity", "balance", "marginBalance"):
                val = safe_float(info.get(key))
                if val > 0:
                    return val
        elif isinstance(info, list):
            for row in info:
                if not isinstance(row, dict):
                    continue
                ccy = str(row.get("currency") or row.get("asset") or row.get("coin") or "").upper()
                if ccy and ccy != "USDT":
                    continue
                for key in ("availableBalance", "available", "cashBalance", "equity", "balance", "marginBalance"):
                    val = safe_float(row.get(key))
                    if val > 0:
                        return val
    if last_error:
        raise last_error
    return 0

DEFAULT_MAX_SINGLE_TRADE_MARGIN_PERCENT = float(os.getenv("DEFAULT_MAX_SINGLE_TRADE_MARGIN_PERCENT", "20"))

def market_contract_size(market: Dict[str, Any]) -> float:
    """Return contract multiplier for swap sizing.

    CCXT futures amounts are usually contract counts, not raw coin quantity.
    For linear swaps notional ~= contracts * contractSize * price.
    Fallback to 1.0 keeps spot-like/exchange-unknown sizing conservative.
    """
    if not isinstance(market, dict):
        return 1.0
    info = market.get("info") if isinstance(market.get("info"), dict) else {}
    for v in (
        market.get("contractSize"), market.get("contract_size"),
        info.get("contractSize"), info.get("contract_size"), info.get("contract_size_unit"),
        info.get("contractVal"), info.get("contract_value"), info.get("ctVal"),
    ):
        cs = safe_float(v, 0)
        if cs > 0:
            return cs
    return 1.0

def market_amount_limit(market: Dict[str, Any], which: str) -> float:
    if not isinstance(market, dict):
        return 0.0
    limits = market.get("limits", {}) if isinstance(market.get("limits", {}), dict) else {}
    amount_limits = limits.get("amount", {}) if isinstance(limits.get("amount", {}), dict) else {}
    candidates = [amount_limits.get(which)]
    info = market.get("info") if isinstance(market.get("info"), dict) else {}
    if which == "max":
        candidates += [
            info.get("maxVol"), info.get("maxVolume"), info.get("maxQty"),
            info.get("maxAmount"), info.get("maxOrderQty"), info.get("maxOrderVol"),
            info.get("maxTradeAmount"), info.get("maxTradeVol"),
        ]
    else:
        candidates += [
            info.get("minVol"), info.get("minVolume"), info.get("minQty"),
            info.get("minAmount"), info.get("minOrderQty"), info.get("minOrderVol"),
            info.get("minTradeAmount"), info.get("minTradeVol"),
        ]
    vals = [safe_float(x, 0) for x in candidates]
    vals = [x for x in vals if x > 0]
    return min(vals) if which == "min" and vals else (max(vals) if vals else 0.0)

def calc_amount_from_risk(entry, sl, balance, risk_percent, leverage, market: Optional[Dict[str, Any]] = None):
    """Market-aware futures position sizing.

    Keeps the original risk-by-stop logic, but clamps by available margin and
    exchange max order amount. This prevents MEXC errors like:
    - 2005 Balance insufficient
    - 2051 Exceeds maximum order amount for a single order
    """
    entry = safe_float(entry, 0)
    sl = safe_float(sl, 0)
    balance = safe_float(balance, 0)
    risk_percent = safe_float(risk_percent, 1)
    leverage = max(1, int(safe_float(leverage, 1)))
    dist = abs(entry - sl)
    if entry <= 0 or dist <= 0 or balance <= 0:
        raise ValueError("Invalid sizing inputs")
    market = market if isinstance(market, dict) else {}
    contract_size = market_contract_size(market)
    risk_usdt = balance * risk_percent / 100.0
    # Amount is contracts. Price movement PnL ~= contracts * contractSize * price_distance.
    by_risk = risk_usdt / max(dist * contract_size, 1e-12)

    max_margin_pct = safe_float(os.getenv("MAX_SINGLE_TRADE_MARGIN_PERCENT", DEFAULT_MAX_SINGLE_TRADE_MARGIN_PERCENT), DEFAULT_MAX_SINGLE_TRADE_MARGIN_PERCENT)
    if max_margin_pct <= 0 or max_margin_pct > 100:
        max_margin_pct = DEFAULT_MAX_SINGLE_TRADE_MARGIN_PERCENT
    max_margin = balance * max_margin_pct / 100.0
    max_notional = max_margin * leverage
    by_margin = max_notional / max(entry * contract_size, 1e-12)

    amount = min(by_risk, by_margin)
    max_amount = market_amount_limit(market, "max")
    if max_amount > 0:
        amount = min(amount, max_amount)
    return max(amount, 0.0)

def estimate_order_notional(amount: float, entry_price: float, market: Optional[Dict[str, Any]] = None) -> float:
    return safe_float(amount, 0) * safe_float(entry_price, 0) * market_contract_size(market or {})

def clamp_amount_to_margin_and_limits(ex, symbol: str, amount: float, entry_price: float, balance: float, leverage: int, market: Optional[Dict[str, Any]] = None) -> float:
    market = market if isinstance(market, dict) else (ex.market(symbol) if hasattr(ex, "market") else {})
    max_amount = market_amount_limit(market, "max")
    if max_amount > 0:
        amount = min(amount, max_amount)
    max_margin_pct = safe_float(os.getenv("MAX_SINGLE_TRADE_MARGIN_PERCENT", DEFAULT_MAX_SINGLE_TRADE_MARGIN_PERCENT), DEFAULT_MAX_SINGLE_TRADE_MARGIN_PERCENT)
    if max_margin_pct <= 0 or max_margin_pct > 100:
        max_margin_pct = DEFAULT_MAX_SINGLE_TRADE_MARGIN_PERCENT
    max_notional = safe_float(balance, 0) * max_margin_pct / 100.0 * max(1, int(leverage))
    contract_size = market_contract_size(market)
    if entry_price > 0 and contract_size > 0 and max_notional > 0:
        amount = min(amount, max_notional / (entry_price * contract_size))
    amount = safe_float(ex.amount_to_precision(symbol, amount), 0)
    # Some exchanges/precisions round up. Step down until the estimate fits.
    for _ in range(8):
        if estimate_order_notional(amount, entry_price, market) <= max_notional * 1.0001 or max_notional <= 0:
            break
        amount = safe_float(ex.amount_to_precision(symbol, amount * 0.98), 0)
    return max(amount, 0.0)

def exchange_id(ex) -> str:
    return str(getattr(ex, "id", "") or getattr(ex, "name", "")).lower()


def compact_exchange_error(err: Any, limit: int = 360) -> str:
    """Return a short human-readable exchange error without dumping huge JSON/HTML.

    Some exchanges/ccxt errors can include full contract metadata responses
    (for example /contract/detail). Sending that raw text to Telegram creates
    huge detail.json attachments and makes the chat unusable.
    """
    try:
        text = str(err)
    except Exception:
        text = repr(err)
    text = re.sub(r"\s+", " ", text).strip()
    # Collapse giant MEXC contract/detail market metadata dumps.
    if "/contract/detail" in text or "contract/detail" in text:
        m = re.search(r"(\d{3})\s+Forbidden|HTTPError\('([^']+)'\)|ExchangeError\('([^']+)'\)", text)
        prefix = "MEXC contract detail/market metadata response"
        if "Access Denied" in text or "Forbidden" in text:
            prefix += " blocked/forbidden"
        elif "success" in text.lower() and ("data" in text[:1000].lower() or "symbol" in text[:1000].lower()):
            prefix += " received but was too large to display"
        return prefix[:limit]
    # Remove long raw JSON/HTML bodies.
    text = re.sub(r"<HTML.*", "<HTML response hidden>", text, flags=re.I|re.S)
    text = re.sub(r"\{[\s\S]{800,}\}", "{large JSON response hidden}", text)
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def is_bingx_exchange(ex) -> bool:
    return "bingx" in exchange_id(ex)


def set_isolated_and_leverage(ex, symbol, leverage):
    """Best-effort margin/leverage setup for supported swap exchanges.

    BingX accepts slightly different params across one-way/hedge accounts, so we
    try safe variants and keep non-fatal warnings instead of blocking execution.
    """
    warnings = []
    if hasattr(ex, "set_margin_mode"):
        for params in ({}, {"marginMode": "isolated"}):
            try:
                if "mexc" in exchange_id(ex):
                    params = dict(params or {})
                    params.setdefault("symbol", mexc_contract_symbol(symbol))
                ex.set_margin_mode("isolated", symbol, params)
                break
            except Exception as e:
                warnings.append(f"set_margin_mode: {compact_exchange_error(e, 160)}")
    if hasattr(ex, "set_leverage"):
        leverage_params = [{"marginMode": "isolated"}]
        if is_bingx_exchange(ex):
            leverage_params = [{}, {"side": "LONG"}, {"side": "SHORT"}, {"positionSide": "LONG"}, {"positionSide": "SHORT"}]
        ok = False
        for params in leverage_params:
            try:
                if "mexc" in exchange_id(ex):
                    params = dict(params or {})
                    params.setdefault("symbol", mexc_contract_symbol(symbol))
                ex.set_leverage(int(leverage), symbol, params)
                ok = True
                break
            except Exception as e:
                warnings.append(f"set_leverage: {compact_exchange_error(e, 160)}")
        if not ok and not warnings:
            warnings.append("set_leverage failed")
    return warnings[-5:]

def side_for(direction): return "buy" if direction.upper() == "LONG" else "sell"
def close_side_for(direction): return "sell" if direction.upper() == "LONG" else "buy"

def position_side_for(direction: str) -> str:
    return "LONG" if str(direction or "").upper() == "LONG" else "SHORT"


def isolated_order_params(leverage=None, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Generic isolated futures params kept for MEXC/backward compatibility."""
    params = {"marginMode": "isolated"}
    if leverage is not None:
        params["leverage"] = int(leverage)
    if extra:
        params.update(extra)
    return params


def exchange_order_params(ex, direction: Optional[str] = None, leverage=None, extra: Optional[Dict[str, Any]] = None, reduce_only: bool = False, include_position_side: bool = True) -> Dict[str, Any]:
    """Exchange-aware futures order params.

    MEXC needs leverage in isolated orders. BingX often needs positionSide in
    hedge-mode accounts. We generate params centrally so Momentum/Reversal/Hybrid
    all use the same execution adapter.
    """
    params = {"marginMode": "isolated"}
    if leverage is not None:
        params["leverage"] = int(leverage)
    if reduce_only:
        params["reduceOnly"] = True
    if is_bingx_exchange(ex) and direction and include_position_side:
        params["positionSide"] = position_side_for(direction)
    if extra:
        params.update(extra)
    return params


def order_param_variants(ex, direction: Optional[str], leverage=None, extra: Optional[Dict[str, Any]] = None, reduce_only: bool = False) -> List[Dict[str, Any]]:
    variants = [exchange_order_params(ex, direction, leverage, extra, reduce_only, include_position_side=True)]
    if is_bingx_exchange(ex):
        # Fallback for one-way BingX accounts where positionSide can be rejected.
        variants.append(exchange_order_params(ex, direction, leverage, extra, reduce_only, include_position_side=False))
    return variants


def create_order_with_param_variants(ex, symbol: str, typ: str, side: str, amount: float, price: Any, params_variants: List[Dict[str, Any]]) -> Any:
    errors = []
    for params in params_variants:
        try:
            final_params = dict(params or {})
            if "mexc" in exchange_id(ex):
                # MEXC Futures raw REST payload uses underscore contract symbols
                # such as btc_usdt / eth_usdt. CCXT still receives the unified
                # symbol argument, while this param prevents private submit/plan
                # endpoints from sending BTCUSDT without the underscore.
                final_params.setdefault("symbol", mexc_contract_symbol(symbol))
            return ex.create_order(symbol, typ, side, amount, price, final_params)
        except Exception as e:
            errors.append(compact_exchange_error(e, 220))
    raise Exception("create_order failed: " + " | ".join(errors[-4:]))


def create_entry_order_adapter(ex, symbol: str, direction: str, amount: float, leverage=None) -> Any:
    return create_order_with_param_variants(
        ex, symbol, "market", side_for(direction), amount, None,
        order_param_variants(ex, direction, leverage, reduce_only=False)
    )


def create_reduce_only_market_order_adapter(ex, symbol: str, direction: str, amount: float, leverage=None) -> Any:
    return create_order_with_param_variants(
        ex, symbol, "market", close_side_for(direction), amount, None,
        order_param_variants(ex, direction, leverage, extra={"reduceOnly": True}, reduce_only=True)
    )


def protective_param_variants(ex, direction: str, trigger_price: float, kind: str, leverage=None) -> List[Dict[str, Any]]:
    direction_u = str(direction or "").upper()
    if "mexc" in exchange_id(ex):
        # MEXC plan-order triggerType: 1 = >= trigger, 2 = <= trigger.
        # LONG: SL below entry uses <=, TP above entry uses >=.
        # SHORT: SL above entry uses >=, TP below entry uses <=.
        trigger_type = 2 if (direction_u == "LONG" and kind == "sl") or (direction_u == "SHORT" and kind != "sl") else 1
        extras = [
            {
                "triggerPrice": trigger_price,
                "stopPrice": trigger_price,
                "triggerType": trigger_type,
                "executeCycle": 1,
                "trend": 1,
                "orderType": 5,
                "reduceOnly": True,
            }
        ]
    elif kind == "sl":
        extras = [
            {"triggerPrice": trigger_price, "stopPrice": trigger_price, "reduceOnly": True},
            {"stopLossPrice": trigger_price, "triggerPrice": trigger_price, "reduceOnly": True},
            {"stopPrice": trigger_price, "reduceOnly": True},
        ]
    else:
        extras = [
            {"triggerPrice": trigger_price, "stopPrice": trigger_price, "reduceOnly": True},
            {"takeProfitPrice": trigger_price, "triggerPrice": trigger_price, "reduceOnly": True},
            {"stopPrice": trigger_price, "reduceOnly": True},
        ]
    variants: List[Dict[str, Any]] = []
    for extra in extras:
        variants.extend(order_param_variants(ex, direction, leverage, extra=extra, reduce_only=True))
    return variants

def extract_order_id(order: Any) -> Optional[str]:
    """Best-effort order id extraction from ccxt response."""
    try:
        oid = order.get("id")
        if oid:
            return str(oid)
        info = order.get("info") or {}
        for key in ("orderId", "order_id", "id"):
            if info.get(key):
                return str(info.get(key))
    except Exception:
        return None
    return None



def order_has_id(order: Any) -> bool:
    return bool(extract_order_id(order))


def extract_order_filled_amount(order: Any) -> float:
    """Best-effort filled/contract amount extraction from a ccxt order."""
    if not isinstance(order, dict):
        return 0.0
    info = order.get("info", {}) if isinstance(order.get("info", {}), dict) else {}
    candidates = [
        order.get("filled"), order.get("amount"), order.get("contracts"),
        info.get("filled"), info.get("vol"), info.get("dealVol"),
        info.get("filledAmount"), info.get("executedQty"), info.get("cumQty"),
    ]
    for v in candidates:
        amt = abs(safe_float(v, 0))
        if amt > 0:
            return amt
    return 0.0


def refresh_local_position_from_exchange(uid: str, pos: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Refresh local position amount from exchange; returns exchange position or None."""
    try:
        ex = get_private_exchange(uid)
        exch_pos = fetch_exchange_position_for_local(ex, pos)
        amt = extract_position_amount(exch_pos) if exch_pos else 0.0
        if amt > 0:
            old_amount = safe_float(pos.get("initial_amount") or pos.get("amount"), amt)
            pos["amount"] = amt
            if old_amount > 0:
                pos["remaining_percent"] = min(100, max(0, (amt / old_amount) * 100))
            pos["exchange_synced_ts"] = time.time()
            pos["exchange_position"] = str(exch_pos)[:1000]
            return exch_pos
    except Exception as e:
        pos.setdefault("tm", {}).setdefault("warnings", []).append(f"refresh exchange position failed: {compact_exchange_error(e, 180)}")
    return None


def protective_order_ok(order: Any) -> bool:
    if not isinstance(order, dict):
        return False
    if order.get("warning") or order.get("errors"):
        return False
    return bool(extract_order_id(order) or order.get("info") or order.get("status") or order.get("type"))


def validate_order_size(ex, symbol: str, amount: float, entry_price: float, balance: Optional[float] = None, leverage: Optional[int] = None) -> None:
    """Fail before entry if size is outside exchange/account limits."""
    try:
        market = ex.market(symbol)
    except Exception:
        market = (getattr(ex, "markets", {}) or {}).get(symbol, {})
    limits = market.get("limits", {}) if isinstance(market, dict) else {}
    cost_limits = limits.get("cost", {}) or {}
    min_amount = market_amount_limit(market, "min")
    max_amount = market_amount_limit(market, "max")
    min_cost = safe_float(cost_limits.get("min"), 0)
    notional = estimate_order_notional(amount, entry_price, market)
    if safe_float(amount, 0) <= 0:
        raise ValueError("Calculated order amount is zero after precision rounding.")
    if min_amount and amount < min_amount:
        raise ValueError(f"Order amount {amount} is below exchange min amount {min_amount} for {symbol}.")
    if max_amount and amount > max_amount:
        raise ValueError(f"Order amount {amount} exceeds exchange max amount {max_amount} for {symbol}.")
    if min_cost and notional < min_cost:
        raise ValueError(f"Order notional {notional:.8f} is below exchange min notional {min_cost} for {symbol}.")
    if balance is not None and leverage is not None:
        max_margin_pct = safe_float(os.getenv("MAX_SINGLE_TRADE_MARGIN_PERCENT", DEFAULT_MAX_SINGLE_TRADE_MARGIN_PERCENT), DEFAULT_MAX_SINGLE_TRADE_MARGIN_PERCENT)
        if max_margin_pct <= 0 or max_margin_pct > 100:
            max_margin_pct = DEFAULT_MAX_SINGLE_TRADE_MARGIN_PERCENT
        max_notional = safe_float(balance, 0) * max_margin_pct / 100.0 * max(1, int(leverage))
        if max_notional > 0 and notional > max_notional * 1.02:
            raise ValueError(f"Order notional {notional:.4f} exceeds configured max notional {max_notional:.4f}.")


def extract_position_amount(raw_pos: Dict[str, Any]) -> float:
    info = raw_pos.get("info", {}) if isinstance(raw_pos, dict) else {}
    candidates = [
        raw_pos.get("contracts"), raw_pos.get("contractSize"), raw_pos.get("size"), raw_pos.get("amount"),
        info.get("holdVol"), info.get("positionAmt"), info.get("positionAmount"), info.get("vol"), info.get("availableVol"),
    ]
    for v in candidates:
        amt = abs(safe_float(v, 0))
        if amt > 0:
            return amt
    return 0.0


def position_symbol_matches(pos: Dict[str, Any], symbol: str, norm_symbol: str) -> bool:
    vals = [pos.get("symbol"), pos.get("market_symbol"), (pos.get("info") or {}).get("symbol")]
    norm_target = normalize_symbol(norm_symbol or symbol)
    for v in vals:
        if not v:
            continue
        if str(v) == symbol:
            return True
        if normalize_symbol(str(v)) == norm_target:
            return True
    return False


def position_side_matches(raw_pos: Dict[str, Any], direction: str) -> bool:
    direction = str(direction or "").upper()
    side = str(raw_pos.get("side") or (raw_pos.get("info") or {}).get("positionType") or (raw_pos.get("info") or {}).get("side") or "").upper()
    amt_signed = safe_float(raw_pos.get("contracts") or raw_pos.get("info", {}).get("positionAmt"), 0)
    if direction == "LONG":
        return side in ("LONG", "BUY", "BID") or amt_signed > 0 or not side
    if direction == "SHORT":
        return side in ("SHORT", "SELL", "ASK") or amt_signed < 0 or not side
    return True


def fetch_exchange_position_for_local(ex, pos: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    symbol = pos.get("market_symbol") or live_tm_exchange_symbol(ex, pos.get("symbol"))
    norm = pos.get("symbol") or symbol
    try:
        raw = ex.fetch_positions([symbol]) if hasattr(ex, "fetch_positions") else []
    except Exception:
        raw = ex.fetch_positions() if hasattr(ex, "fetch_positions") else []
    matches = []
    for rp in raw or []:
        if not isinstance(rp, dict):
            continue
        amt = extract_position_amount(rp)
        if amt <= 0:
            continue
        if position_symbol_matches(rp, symbol, norm) and position_side_matches(rp, pos.get("direction")):
            matches.append(rp)
    return matches[0] if matches else None


def emergency_close_position(ex, symbol: str, direction: str, amount: float, leverage=None) -> Any:
    close_amount = float(ex.amount_to_precision(symbol, amount))
    return create_reduce_only_market_order_adapter(ex, symbol, direction, close_amount, leverage)

def cancel_order_best_effort(ex, order_id: Optional[str], symbol: str) -> Optional[str]:
    if not order_id:
        return None
    try:
        ex.cancel_order(order_id, symbol)
        return "cancelled"
    except Exception as e:
        return f"cancel_failed: {compact_exchange_error(e, 180)}"

def place_protective_order(ex, symbol, direction, amount, trigger_price, kind, leverage=None):
    """Place exchange-side protective SL/TP order only.

    Important: never fall back to an immediate plain market order here. For MEXC
    Futures ccxt requires type="market" + triggerPrice/orderType=5 for plan
    orders; using type="stop_market" is rejected before the request is sent.
    """
    side = close_side_for(direction)
    trigger_price = safe_float(trigger_price, 0)
    if trigger_price <= 0:
        return {"warning": f"{kind} order not placed", "errors": ["bad_trigger_price"]}

    exid = exchange_id(ex)
    typ = "market" if "mexc" in exid else ("stop_market" if kind == "sl" else "take_profit_market")

    errors = []
    params_variants = protective_param_variants(ex, direction, trigger_price, kind, leverage)
    for params in params_variants:
        try:
            return ex.create_order(symbol, typ, side, amount, None, params)
        except Exception as e:
            errors.append(compact_exchange_error(e, 180))
    return {"warning": f"{kind} order not placed", "errors": errors[-5:]}


def calc_take_profit_by_rr(entry, stop_loss, direction, rr):
    entry = safe_float(entry, 0)
    stop_loss = safe_float(stop_loss, 0)
    rr_mult = safe_float(rr, 2.0)
    if entry <= 0 or stop_loss <= 0:
        return None
    if rr_mult <= 0:
        rr_mult = 2.0
    dist = abs(entry - stop_loss)
    if dist <= 0:
        return None
    if str(direction).upper() == "LONG":
        return entry + dist * rr_mult
    return entry - dist * rr_mult

def effective_rr_for_signal(x):
    if x.get("extended_tp_mode"):
        return safe_float(x.get("extended_tp_rr"), safe_float(x.get("dynamic_rr"), 4.0))
    return safe_float(x.get("dynamic_rr"), 2.0)

async def execute_real_trade(uid: str, symbol: str, direction: str, stop_loss=None, take_profit=None, rr: Optional[float] = None, tp1=None, tp2=None, tp3=None, setup: Optional[str] = None) -> Dict[str, Any]:
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
    ex = get_private_exchange(uid)
    ms = exchange_symbol_for_order(ex, symbol)
    if s.get("duplicate_protection_enabled", True):
        # Refresh from exchange before local duplicate blocking.
        # This avoids blocking a new signal when a previous local position was already closed on MEXC.
        is_dup, dup_msg = is_duplicate_open_trade(uid, symbol, direction, ex=ex, market_symbol=ms)
        if is_dup:
            raise ValueError(dup_msg)
    ticker = ex.fetch_ticker(ms)
    entry = safe_float(ticker.get("last") or ticker.get("close"))
    if not stop_loss:
        stop_loss = entry * (0.99 if direction.upper() == "LONG" else 1.01)
    rr_mult = safe_float(rr, 2.0)
    if rr_mult <= 0:
        rr_mult = 2.0
    # Respect scanner-provided TP when present (Reversal/Hybrid engine already
    # calculates TP2 from structure). If no TP is provided, fall back to RR-based TP.
    # This avoids overriding Reversal TP projection with a generic RR level.
    if take_profit:
        take_profit = safe_float(take_profit, 0)
    if not take_profit:
        forced_take_profit = calc_take_profit_by_rr(entry, stop_loss, direction, rr_mult)
        if forced_take_profit:
            take_profit = forced_take_profit
        else:
            dist = abs(entry - stop_loss)
            take_profit = entry + dist * rr_mult if direction.upper() == "LONG" else entry - dist * rr_mult
    balance = get_usdt_free_balance(ex)
    lev = int(s["leverage"])
    try:
        market = ex.market(ms)
    except Exception:
        market = (getattr(ex, "markets", {}) or {}).get(ms, {})
    amount = calc_amount_from_risk(entry, stop_loss, balance, float(s["risk_percent"]), lev, market)
    amount = clamp_amount_to_margin_and_limits(ex, ms, amount, entry, balance, lev, market)
    validate_order_size(ex, ms, amount, entry, balance, lev)
    warnings = set_isolated_and_leverage(ex, ms, lev)
    entry_order = create_entry_order_adapter(ex, ms, direction, amount, lev)
    # Use actual filled/exchange position amount for all protective orders when available.
    # This prevents SL/TP size mismatch after partial fills or exchange-side rounding.
    requested_amount = amount
    actual_amount = extract_order_filled_amount(entry_order) or amount
    try:
        temp_pos_for_fill = {"symbol": normalize_symbol(symbol), "market_symbol": ms, "direction": direction.upper()}
        exch_fill_pos = fetch_exchange_position_for_local(ex, temp_pos_for_fill)
        exch_amt = extract_position_amount(exch_fill_pos) if exch_fill_pos else 0.0
        if exch_amt > 0:
            actual_amount = float(ex.amount_to_precision(ms, exch_amt))
    except Exception:
        pass
    if actual_amount > 0:
        amount = actual_amount

    trade_mgmt_enabled = bool(s.get("trade_mgmt_enabled", True))
    sl_order = None
    tp_order = None
    sl_order_id = None
    tp_order_id = None
    if trade_mgmt_enabled:
        sl_order = place_protective_order(ex, ms, direction, amount, stop_loss, "sl", lev)
        tp_order = place_protective_order(ex, ms, direction, amount, take_profit, "tp", lev)
        sl_order_id = extract_order_id(sl_order)
        tp_order_id = extract_order_id(tp_order)
        if not protective_order_ok(sl_order) or not protective_order_ok(tp_order):
            # If only one protective order was created, cancel it before/after emergency close.
            # Otherwise a stale reduceOnly conditional order could affect a later position.
            orphan_sl_cancel = cancel_order_best_effort(ex, sl_order_id, ms)
            orphan_tp_cancel = cancel_order_best_effort(ex, tp_order_id, ms)
            close_result = None
            emergency_closed = False
            try:
                close_result = emergency_close_position(ex, ms, direction, amount, lev)
                emergency_closed = isinstance(close_result, dict) and not close_result.get("warning") and not close_result.get("emergency_close_failed")
            except Exception as close_error:
                close_result = {"emergency_close_failed": compact_exchange_error(close_error, 320)}
            close_status = "emergency close executed" if emergency_closed else "EMERGENCY CLOSE FAILED - MANUAL CHECK REQUIRED"
            raise ValueError(
                f"Trade Mgmt ON: SL/TP protection failed; {close_status}. "
                f"SL={str(sl_order)[:240]} TP={str(tp_order)[:240]} CLOSE={str(close_result)[:240]} "
                f"CANCEL_SL={orphan_sl_cancel} CANCEL_TP={orphan_tp_cancel}"
            )

    # Preserve scanner-specific TP ladder (Reversal/Hybrid) for Live TM and status.
    # take_profit remains the exchange-side protective TP, while tp1/tp2/tp3/runner_target
    # let Live TM and messages use the structured levels calculated by the scanner.
    tp1_val = safe_float(tp1, 0)
    tp2_val = safe_float(tp2, 0)
    tp3_val = safe_float(tp3, 0)
    if tp2_val <= 0 and take_profit:
        tp2_val = safe_float(take_profit, 0)
    if tp1_val <= 0:
        tp1_val = safe_float(take_profit, 0)
    runner_target_val = tp3_val if tp3_val > 0 else tp2_val
    setup_label = str(setup or "").upper().strip()


    pos = {"uid": str(uid), "symbol": normalize_symbol(symbol), "market_symbol": ms, "exchange": s["exchange"], "direction": direction.upper(), "entry": round(entry,8), "amount": amount, "initial_amount": amount, "requested_amount": requested_amount, "initial_stop_loss": round(stop_loss,8), "stop_loss": round(stop_loss,8), "take_profit": round(take_profit,8), "tp1": round(tp1_val,8) if tp1_val > 0 else None, "tp2": round(tp2_val,8) if tp2_val > 0 else None, "tp3": round(tp3_val,8) if tp3_val > 0 else None, "runner_target": round(runner_target_val,8) if runner_target_val > 0 else None, "setup": setup_label or None, "rr": safe_float(rr, 2.0), "leverage": lev, "margin_mode": "isolated", "trade_mgmt_enabled": trade_mgmt_enabled, "status": "real_opened", "remaining_percent": 100, "opened_ts": time.time(), "breakeven_enabled": bool(s.get("breakeven_enabled", False)), "breakeven_r": safe_float(s.get("breakeven_r"), 1), "trailing_enabled": bool(s.get("trailing_enabled", False)), "trailing_r": safe_float(s.get("trailing_r"), 1.5), "partial_tp_enabled": bool(s.get("partial_tp_enabled", False)), "partial_tp_r": safe_float(s.get("partial_tp_r"), 1), "partial_tp_percent": safe_float(s.get("partial_tp_percent"), 50), "warnings": warnings, "entry_order": str(entry_order)[:500], "sl_order": str(sl_order)[:500] if sl_order is not None else "DISABLED_BY_TRADE_MGMT_OFF", "tp_order": str(tp_order)[:500] if tp_order is not None else "DISABLED_BY_TRADE_MGMT_OFF", "sl_order_id": sl_order_id, "tp_order_id": tp_order_id, "tm": {"enabled": trade_mgmt_enabled, "sl_order_id": sl_order_id, "tp_order_id": tp_order_id}}
    ps = _positions(uid); ps.append(pos); _save_positions(uid, ps)
    return pos

def is_duplicate_open_trade(uid: str, symbol: str, direction: str, ex=None, market_symbol: Optional[str] = None) -> Tuple[bool, str]:
    """Return True when the same symbol + direction is already tracked as open.

    v0086: before blocking, confirm the duplicate on the exchange when possible.
    If the local record is stale and the exchange has no matching position twice/safely,
    mark it stale instead of blocking the next valid signal.
    """
    norm_symbol = normalize_symbol(symbol)
    norm_direction = str(direction or "").upper()
    open_statuses = {"real_opened", "open", "opened", "live", "running", "active"}
    positions = _positions(uid)
    changed = False

    for pos in positions:
        if str(pos.get("symbol", "")).upper() != norm_symbol.upper():
            continue
        if str(pos.get("direction", "")).upper() != norm_direction:
            continue
        status = str(pos.get("status", "real_opened")).lower()
        closed = bool(pos.get("closed")) or bool(pos.get("closed_ts")) or status in {"closed", "done", "cancelled", "canceled", "closed_on_exchange", "closed_by_live_tm", "closed_by_stop_all"}
        if closed or not (status in open_statuses or status.startswith("real_")):
            continue

        # Exchange confirmation prevents stale local state from blocking forever.
        if ex is not None:
            try:
                check_pos = dict(pos)
                if market_symbol:
                    check_pos["market_symbol"] = market_symbol
                exch_pos = fetch_exchange_position_for_local(ex, check_pos)
                exch_amt = extract_position_amount(exch_pos) if exch_pos else 0.0
                if exch_amt > 0:
                    return True, f"🛡 Duplicate protection ON: {norm_symbol} {norm_direction} already open on exchange. Duplicate blocked."
                misses = int(pos.get("duplicate_exchange_misses", 0)) + 1
                pos["duplicate_exchange_misses"] = misses
                pos["duplicate_exchange_checked_ts"] = time.time()
                if misses >= 2:
                    pos["status"] = "closed_on_exchange"
                    pos["remaining_percent"] = 0
                    pos.setdefault("tm", {}).setdefault("events", []).append("Duplicate check: no matching exchange position twice; local trade marked stale/closed_on_exchange.")
                    changed = True
                    continue
                # One miss may be a transient API/position response issue: block once.
                return True, f"🛡 Duplicate protection ON: local {norm_symbol} {norm_direction} exists; exchange confirmation pending. Duplicate blocked once."
            except Exception as e:
                pos.setdefault("tm", {}).setdefault("warnings", []).append(f"duplicate exchange refresh failed: {compact_exchange_error(e, 180)}")
                return True, f"🛡 Duplicate protection ON: {norm_symbol} {norm_direction} already tracked locally. Duplicate blocked; exchange refresh failed."

        return True, f"🛡 Duplicate protection ON: {norm_symbol} {norm_direction} already open. Duplicate blocked."

    if changed:
        _save_positions(uid, positions)
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
        + (f"TP1/TP2/TP3: {pos.get('tp1')}/{pos.get('tp2')}/{pos.get('tp3')}\n" if pos.get('tp1') or pos.get('tp2') or pos.get('tp3') else "")
        + (f"Setup: {pos.get('setup')}\n" if pos.get('setup') else "")
        + f"Leverage: x{pos.get('leverage')}\n"
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
        active = [p for p in raw or [] if extract_position_amount(p) > 0]
        local = _positions(uid)
        changed = False
        closed_count = 0
        updated_count = 0

        for pos in local:
            status = str(pos.get("status", "")).lower()
            if status.startswith("closed") or pos.get("remaining_percent") == 0:
                continue
            match = None
            try:
                symbol = pos.get("market_symbol") or live_tm_exchange_symbol(ex, pos.get("symbol"))
                norm = pos.get("symbol") or symbol
                for rp in active:
                    if position_symbol_matches(rp, symbol, norm) and position_side_matches(rp, pos.get("direction")):
                        match = rp
                        break
            except Exception:
                match = None
            if match:
                amt = extract_position_amount(match)
                if amt > 0:
                    pos["amount"] = amt
                    pos["exchange_synced_ts"] = time.time()
                    pos["exchange_position"] = str(match)[:1000]
                    pos["sync_missing_count"] = 0
                    updated_count += 1
                    changed = True
            else:
                # Avoid false close on a temporary empty/partial API response.
                # Mark closed only after two consecutive misses; first miss is a warning only.
                miss_count = int(pos.get("sync_missing_count", 0) or 0) + 1
                pos["sync_missing_count"] = miss_count
                pos["exchange_synced_ts"] = time.time()
                if miss_count >= 2:
                    pos["status"] = "closed_on_exchange"
                    pos["remaining_percent"] = 0
                    pos.setdefault("tm", {}).setdefault("events", []).append("Position Sync: no active exchange position twice; marked closed_on_exchange.")
                    closed_count += 1
                else:
                    pos.setdefault("tm", {}).setdefault("warnings", []).append("Position Sync: active exchange position not found once; waiting for next sync before marking closed.")
                changed = True

        data = load_json(POSITIONS_FILE, {})
        data[f"{uid}_exchange_snapshot"] = {"ts": time.time(), "exchange": s["exchange"], "active_count": len(active), "positions": [str(x)[:1000] for x in active[:20]]}
        save_json(POSITIONS_FILE, data)
        if changed:
            _save_positions(uid, local)
        msg = f"🔁 Position Sync completed\nExchange active positions: {len(active)}\nLocal tracked positions: {len(local)}\nUpdated: {updated_count}\nMarked closed: {closed_count}"
        if app and closed_count:
            await app.bot.send_message(chat_id=int(uid), text=msg)
        return msg
    except Exception as e:
        return f"Position Sync error: {compact_exchange_error(e, 500)}"

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
⚙️ Scanner Mode: {str(s.get('scan_mode', get_scan_mode(uid))).upper()}
🔀 Hybrid Variant: {str(s.get('hybrid_variant', get_hybrid_variant(uid))).upper()}
📊 Reversal Charts: {'ON' if s.get('reversal_charts', get_reversal_charts(uid)) else 'OFF'}
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
🛡 Trade Mgmt: {'ON' if s.get('trade_mgmt_enabled', True) else 'OFF'}
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

/balance
Проверка private futures balance API без открытия ордеров.

/api_check
Диагностика futures API, server time, endpoint, proxy/IP.

/ip
Показать внешний IP бота напрямую и через proxy.

/scan_mode momentum|reversal|hybrid
Переключение scanner mode.

/charts_on
/charts_off
Hybrid Light/Full: Light = Reversal Top-N + Momentum confirm по найденным монетам; Full = Reversal Top-N + полный Momentum Top-N.
Вкл/выкл графики reversal mode. Также доступно через кнопку ⚙️ MODE.

/proxy socks5://login:pass@host:port
Сохранить proxy для API-запросов бота.

/del_proxy
Удалить proxy и работать напрямую.

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
    scan_mode_single = str(s.get("scan_mode", get_scan_mode(uid))).lower()
    if scan_mode_single in {"reversal", "hybrid"}:
        primary_tf = "15m"
    df = add_indicators(fetch_ohlcv_for_symbol(s["exchange"], symbol, primary_tf, 180))
    if scan_mode_single == "reversal":
        market = score_reversal_market(s["exchange"], symbol, s, df)
    elif scan_mode_single == "hybrid":
        rev_settings = dict(s); rev_settings["scan_mode"] = "reversal"
        mom_settings = dict(s); mom_settings["scan_mode"] = "momentum"
        rev_market = score_reversal_market(s["exchange"], symbol, rev_settings, df)
        mom_market = score_market_multi(s["exchange"], symbol, mom_settings, timeframe)
        mom_market = apply_structural_layers(s["exchange"], symbol, df, mom_market, mom_settings)
        if str(rev_market.get("direction", "WAIT")).upper() == "LONG" and str(mom_market.get("direction", "WAIT")).upper() == "LONG":
            market = dict(rev_market if float(rev_market.get("score", 0) or 0) >= float(mom_market.get("score", 0) or 0) else mom_market)
            market["setup"] = "REVERSAL+MOMENTUM"
            market["score"] = min(100, max(float(rev_market.get("score", 0) or 0), float(mom_market.get("score", 0) or 0)) + 5)
            market["hybrid_match"] = True
            if rev_market.get("reversal"):
                market["reversal"] = rev_market.get("reversal")
                market["rr"] = rev_market.get("rr")
        elif str(rev_market.get("direction", "WAIT")).upper() == "LONG":
            market = rev_market
        else:
            market = mom_market
    else:
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
        scan_mode = str(settings_snapshot.get("scan_mode", get_scan_mode())).lower()
        if scan_mode == "reversal":
            mkt = score_reversal_market(exchange, sym, settings_snapshot, df)
            return apply_session_volatility_filter(settings_snapshot, mkt)
        if scan_mode == "hybrid":
            # Hybrid must evaluate both engines during Top/Auto scan, not only
            # the momentum fast path. Otherwise AUTO could miss reversal setups
            # even though /signal shows them.
            rev_settings = dict(settings_snapshot)
            rev_settings["scan_mode"] = "reversal"
            mom_settings = dict(settings_snapshot)
            mom_settings["scan_mode"] = "momentum"
            rev_market = score_reversal_market(exchange, sym, rev_settings, df)
            mom_market = score_market_multi_fast(exchange, sym, mom_settings, df)
            mom_market = apply_structural_layers(exchange, sym, df, mom_market, mom_settings)
            if str(rev_market.get("direction", "WAIT")).upper() == "LONG" and str(mom_market.get("direction", "WAIT")).upper() == "LONG":
                mkt = dict(rev_market if float(rev_market.get("score", 0) or 0) >= float(mom_market.get("score", 0) or 0) else mom_market)
                mkt["setup"] = "REVERSAL+MOMENTUM"
                mkt["score"] = min(100, max(float(rev_market.get("score", 0) or 0), float(mom_market.get("score", 0) or 0)) + 5)
                mkt["hybrid_match"] = True
                if rev_market.get("reversal"):
                    mkt["reversal"] = rev_market.get("reversal")
                    mkt["rr"] = rev_market.get("rr")
            elif str(rev_market.get("direction", "WAIT")).upper() == "LONG":
                mkt = rev_market
            else:
                mkt = mom_market
            return apply_session_volatility_filter(settings_snapshot, mkt)
        mkt = score_market_multi_fast(exchange, sym, settings_snapshot, df)
        mkt = apply_structural_layers(exchange, sym, df, mkt, settings_snapshot)
        return apply_session_volatility_filter(settings_snapshot, mkt)
    return await asyncio.to_thread(_work)


def manual_confirm_keyboard(uid: str) -> Optional[InlineKeyboardMarkup]:
    """
    Show OPEN/CANCEL buttons only when AI has approved trades and user is in MANUAL mode.
    UI-only fix: does not touch AI prompt, filters, RR, TP, SL, or execution logic.
    """
    try:
        s_now = get_settings(uid)
        if s_now.get("trading_mode") == "auto":
            return None
        if not LAST_AI_CONFIRMED.get(int(uid)):
            return None
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ OPEN REAL TRADE", callback_data="open_confirmed")],
            [InlineKeyboardButton("❌ CANCEL", callback_data="cancel")],
            [InlineKeyboardButton("⬅️ Главное меню", callback_data="back:main")]
        ])
    except Exception:
        return None

async def _run_scan_task(uid: str, n: int, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    try:
        result = await run_top_scan(uid, n, context, chat_id)
        s_now = get_settings(uid)
        # Robust MANUAL confirmation UI.
        # If AI approved trades and trading_mode is MANUAL, always attach OPEN/CANCEL buttons.
        reply_markup = manual_confirm_keyboard(uid)
        if reply_markup is None:
            buttons = []
            if LAST_SCAN_RESULTS.get(int(uid)) and not s_now.get("ai_auto_p", True) and s_now.get("strict_ai_mode", True):
                buttons.append([InlineKeyboardButton("🧠 AI Confirm", callback_data="ai_confirm")])
            buttons.append([InlineKeyboardButton("⬅️ Главное меню", callback_data="back:main")])
            reply_markup = InlineKeyboardMarkup(buttons)

        await context.bot.send_message(
            chat_id=chat_id,
            text=result[:3900],
            reply_markup=reply_markup
        )
    except asyncio.CancelledError:
        raise
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Scan error: {compact_exchange_error(e, 800)}")
    finally:
        # Do not remove a newer scan task that may have been started after this one was cancelled/finished.
        if USER_SCAN_TASKS.get(uid) is asyncio.current_task():
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
    if stop_all_active(uid):
        return "🚨 STOP ALL is ON. Scan skipped."
    if USER_SCAN_LOCKS.get(uid):
        return f"⏳ Top-{n} scan уже выполняется. Новый запуск пропущен, чтобы не перегружать Railway/API."
    USER_SCAN_LOCKS[uid] = True
    try:
        return await _run_top_scan_locked(uid, n, context, chat_id)
    finally:
        USER_SCAN_LOCKS.pop(uid, None)

async def _run_top_scan_locked(uid: str, n: int, context: Optional[ContextTypes.DEFAULT_TYPE] = None, chat_id: Optional[int] = None) -> str:
    set_setting(uid, "scanner_size", n)
    if stop_all_active(uid):
        return "🚨 STOP ALL is ON. Scan skipped."
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
            # Telegram does not move an edited old message to the bottom of the chat.
            # To keep scan progress below the menu/buttons and always visible, delete
            # the previous progress message and send a fresh one at the bottom.
            try:
                if progress_message_id:
                    try:
                        await context.bot.delete_message(chat_id=chat_id, message_id=progress_message_id)
                    except Exception:
                        pass
                msg = await context.bot.send_message(chat_id=chat_id, text=text)
                progress_message_id = msg.message_id
            except Exception:
                # Fallback: if delete/send fails, try editing the old message so progress is not lost.
                try:
                    if progress_message_id:
                        await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_message_id, text=text)
                except Exception:
                    pass

    async def send_scan_coin(sym: str):
        # No per-coin spam in chat; only 10/50/100 progress updates.
        return

    async def send_phase_message(text: str):
        if context is not None and chat_id is not None:
            try:
                await context.bot.send_message(chat_id=chat_id, text=text)
            except Exception:
                pass

    await send_scan_progress(10)

    sem = asyncio.Semaphore(max(1, SCAN_MAX_CONCURRENT))
    scan_mode_now = str(s.get("scan_mode", get_scan_mode(uid))).lower()
    hybrid_variant = str(s.get("hybrid_variant", get_hybrid_variant(uid))).lower()
    if hybrid_variant not in {"light", "full"}:
        hybrid_variant = "light"
    phases = ["reversal", "momentum"] if scan_mode_now == "hybrid" and hybrid_variant == "full" else [scan_mode_now if scan_mode_now in {"momentum", "reversal"} else "momentum"]

    async def scan_symbol(sym: str, phase_mode: str):
        async with sem:
            if SCAN_REQUEST_PAUSE > 0:
                await asyncio.sleep(SCAN_REQUEST_PAUSE)
            coin_settings = dict(get_settings(uid))
            coin_settings["scan_mode"] = phase_mode
            primary_tf, _ = timeframe_pair(coin_settings)
            if phase_mode == "reversal":
                primary_tf = "15m"
            try:
                mkt = await asyncio.wait_for(
                    _scan_one_symbol(coin_settings["exchange"], sym, primary_tf, dict(coin_settings)),
                    timeout=SCAN_SYMBOL_TIMEOUT,
                )
            except asyncio.TimeoutError:
                return sym, {
                    "direction": "WAIT",
                    "score": 0,
                    "reasons": [f"scan timeout after {SCAN_SYMBOL_TIMEOUT:g}s"],
                    "error": "timeout",
                }, coin_settings, phase_mode
            if phase_mode == "momentum" and str(mkt.get("direction", "WAIT")).upper() == "SHORT" and scan_mode_now == "hybrid":
                # The channel-style Hybrid mode is LONG-only: reversal LONG first, then momentum LONG.
                mkt = {**mkt, "direction": "WAIT", "reasons": list(mkt.get("reasons", [])) + ["hybrid long-only: short skipped"]}
            return sym, mkt, coin_settings, phase_mode

    async def run_phase(phase_mode: str, phase_symbols: list):
        nonlocal skipped_wait, skipped_score, errors, completed, results
        if scan_mode_now == "hybrid":
            if phase_mode == "reversal":
                await send_phase_message(f"🔄 HYBRID {hybrid_variant.upper()}: начался Reversal scan...")
            elif hybrid_variant == "light":
                await send_phase_message("⚡ HYBRID LIGHT: Momentum confirm только по найденным Reversal монетам...")
            else:
                await send_phase_message("⚡ HYBRID FULL: Reversal завершён, начался полный Momentum scan...")
        phase_total = max(len(phase_symbols), 1)
        tasks = [asyncio.create_task(scan_symbol(sym, phase_mode)) for sym in phase_symbols]
        try:
            for task in asyncio.as_completed(tasks):
                if stop_all_active(uid):
                    for t in tasks:
                        t.cancel()
                    return "🚨 STOP ALL activated. Scan cancelled."
                completed += 1
                try:
                    sym, mkt, coin_settings, used_phase = await task
                    if str(mkt.get("direction", "WAIT")).upper() == "WAIT":
                        skipped_wait += 1
                    else:
                        score_ok = float(mkt.get("score", 0)) >= float(coin_settings.get("min_score", 80))
                        structural_ok = coin_settings.get("structural_mode") == "structural_only"
                        if score_ok or structural_ok:
                            item = {"symbol": sym, **mkt}
                            item["scan_phase"] = used_phase
                            if scan_mode_now == "hybrid":
                                item["hybrid"] = True
                            results.append(compact_scan_candidate(item))
                        else:
                            skipped_score += 1
                except Exception:
                    errors += 1

                current_percent = int((completed / total) * 100)
                # Update at every 10% step. The progress message is re-sent, not only edited,
                # so it moves to the bottom of the chat and the user sees that the scan is alive.
                progress_step = min(100, max(10, (current_percent // 10) * 10))
                if progress_step >= 10:
                    await send_scan_progress(progress_step)
                if SCAN_GC_EVERY > 0 and completed % SCAN_GC_EVERY == 0:
                    prune_runtime_caches()
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finally:
            for task in tasks:
                if task.done() and not task.cancelled():
                    try:
                        task.exception()
                    except Exception:
                        pass
            tasks.clear()
            prune_runtime_caches()
        return None

    if scan_mode_now == "hybrid" and hybrid_variant == "full":
        # Hybrid Full scans the same Top-N universe twice: first Reversal, then full Momentum.
        total = max(len(symbols) * 2, 1)
    else:
        total = max(len(symbols), 1)
    completed = 0

    if scan_mode_now == "hybrid" and hybrid_variant == "light":
        # Hybrid Light: full Reversal scan first, then Momentum confirmation only for found Reversal candidates.
        cancelled_msg = await run_phase("reversal", symbols)
        if cancelled_msg:
            return cancelled_msg
        reversal_candidates = []
        seen_rev = set()
        for r in results:
            if str(r.get("scan_phase", "")).lower() == "reversal" and str(r.get("direction", "WAIT")).upper() == "LONG":
                sym_key = normalize_symbol(r.get("symbol", ""))
                if sym_key and sym_key not in seen_rev:
                    seen_rev.add(sym_key)
                    reversal_candidates.append(sym_key)
        if reversal_candidates:
            total = max(completed + len(reversal_candidates), 1)
            cancelled_msg = await run_phase("momentum", reversal_candidates)
            if cancelled_msg:
                return cancelled_msg
        else:
            await send_phase_message("ℹ️ HYBRID LIGHT: Reversal кандидатов нет, Momentum confirm пропущен.")
    else:
        for phase in phases:
            cancelled_msg = await run_phase(phase, symbols)
            if cancelled_msg:
                return cancelled_msg

    prune_runtime_caches()
    await send_scan_progress(100)

    # Keep only valid LONG/SHORT candidates, even if some old code mutated results.
    results = [r for r in results if str(r.get("direction", "WAIT")).upper() in ["LONG", "SHORT"]]

    if scan_mode_now == "hybrid":
        # Merge duplicate symbols from Reversal and Momentum. If the same LONG appears in both,
        # keep one trade candidate, add a small priority bonus, and label it as Reversal+Momentum.
        merged = {}
        for r in results:
            sym_key = normalize_symbol(r.get("symbol", ""))
            if not sym_key:
                continue
            prev = merged.get(sym_key)
            if prev is None:
                merged[sym_key] = dict(r)
                continue
            prev_score = float(prev.get("score", 0) or 0)
            new_score = float(r.get("score", 0) or 0)
            base = prev if prev_score >= new_score else dict(r)
            other = r if base is prev else prev
            base["score"] = min(100, max(prev_score, new_score) + 5)
            base["setup"] = "REVERSAL+MOMENTUM"
            base["hybrid_match"] = True
            base["reversal_score"] = prev_score if str(prev.get("scan_phase")) == "reversal" else new_score if str(r.get("scan_phase")) == "reversal" else base.get("reversal_score")
            base["momentum_score"] = prev_score if str(prev.get("scan_phase")) == "momentum" else new_score if str(r.get("scan_phase")) == "momentum" else base.get("momentum_score")
            # Preserve reversal levels/metrics when available, because Reversal owns SL/TP projection.
            rev_src = prev if prev.get("reversal") else r if r.get("reversal") else None
            if rev_src:
                for k in ["reversal", "rr", "sl", "tp1", "tp2", "tp3", "setup"]:
                    if k in rev_src and k not in base:
                        base[k] = rev_src[k]
                base["setup"] = "REVERSAL+MOMENTUM"
            merged[sym_key] = base
        # Mark unmatched Hybrid candidates explicitly and assign priority.
        # Priority order:
        # 1) REVERSAL + MOMENTUM match by symbol
        # 2) REVERSAL only
        # 3) MOMENTUM only
        for item in merged.values():
            setup = str(item.get("setup", "")).upper()
            phase = str(item.get("scan_phase", "")).lower()
            if item.get("hybrid_match") or "REVERSAL+MOMENTUM" in setup:
                item["setup"] = "REVERSAL+MOMENTUM"
                item["hybrid_priority"] = 3
                item["priority_label"] = "HIGH"
            elif setup.startswith("REVERSAL") or phase == "reversal" or item.get("reversal"):
                item["setup"] = "REVERSAL"
                item["hybrid_priority"] = 2
                item["priority_label"] = "NORMAL"
            else:
                item["setup"] = "MOMENTUM"
                item["hybrid_priority"] = 1
                item["priority_label"] = "NORMAL"
        results = list(merged.values())

    if scan_mode_now == "hybrid":
        results.sort(key=lambda x: (int(x.get("hybrid_priority", 0) or 0), float(x.get("score", 0) or 0)), reverse=True)
    else:
        results.sort(key=lambda x: x.get("score", 0), reverse=True)

    # Re-read settings at the end of the scan. A Top-N scan can take long enough
    # that the user changes TopLimit while it is running; using the initial snapshot
    # here made the final list sometimes stay capped at 5 even after selecting 10.
    final_settings = get_settings(uid)
    limit = selected_top_limit(final_settings)
    if limit is not None:
        results = results[:limit]

    # Store only the final shortlist, not all Top-200 intermediate objects.
    LAST_SCAN_RESULTS[int(uid)] = [compact_scan_candidate(dict(r)) for r in results[:max(limit or len(results), 20)]]

    scan_mode_label = str(final_settings.get("scan_mode", get_scan_mode(uid))).upper()
    chart_label = "ON" if final_settings.get("reversal_charts", get_reversal_charts(uid)) else "OFF"
    summary_header = (
        f"🔥 Top-{n} Signal | {final_settings['exchange'].upper()}\n"
        f"⚙️ Mode: {scan_mode_label}\n"
        f"🔀 Hybrid: {str(final_settings.get('hybrid_variant', get_hybrid_variant(uid))).upper()}\n"
        f"🎯 MinScore: {final_settings['min_score']}%\n"
        f"📋 TopLimit: {top_limit_label(final_settings)}\n"
        f"🧠 Structural: {structural_mode_label(final_settings.get('structural_mode'))}\n"
        f"📊 Reversal Charts: {chart_label}\n"
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
        setup_label = str(r.get("setup", "MOMENTUM")).upper()
        rr_txt = f" | RR {r.get('rr')}R" if r.get("rr") else ""
        priority_txt = f" | Priority {r.get('priority_label')}" if scan_mode_now == "hybrid" and r.get("priority_label") else ""
        lines.append(f"{i}. {r['symbol']} — {r['direction']} | {setup_label}{priority_txt} | Scanner Score {r['score']}%{rr_txt} | {'MTF ✅' if r.get('mtf_confirmed', True) else 'MTF ❌'}")

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
                ai_result = f"❌ AI confirm error: {compact_exchange_error(e, 800)}"

            try:
                if get_settings(uid).get("trading_mode") == "auto":
                    auto_exec_text = await execute_confirmed_from_auto(uid)
            except Exception as e:
                auto_exec_text = f"\n❌ Auto execution error: {compact_exchange_error(e, 500)}"
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

    # Reversal charts are generated only for final candidates/AI-approved symbols, not for all Top-N coins.
    try:
        if str(final_settings.get("scan_mode", get_scan_mode(uid))).lower() in {"reversal", "hybrid"} and final_settings.get("reversal_charts", get_reversal_charts(uid)) and context is not None and chat_id is not None:
            ai_was_run = bool(final_settings.get("strict_ai_mode", True) and final_settings.get("ai_auto_p", True))
            approved_symbols = {normalize_symbol(x.get("symbol", "")) for x in LAST_AI_CONFIRMED.get(int(uid), [])}
            if ai_was_run and not approved_symbols:
                chart_candidates = []
            else:
                chart_candidates = [
                    r for r in results
                    if (str(r.get("setup", "")).upper().startswith("REVERSAL") or r.get("reversal"))
                    and (not approved_symbols or normalize_symbol(r.get("symbol", "")) in approved_symbols)
                ]
            for r in chart_candidates[:3]:
                sym = normalize_symbol(r.get("symbol", ""))
                try:
                    df_chart = add_indicators(await asyncio.to_thread(fetch_ohlcv_for_symbol, final_settings["exchange"], sym, "15m", 160))
                    chart_path = await asyncio.to_thread(render_reversal_chart, sym, df_chart, r)
                    if chart_path:
                        with open(chart_path, "rb") as img:
                            await context.bot.send_photo(chat_id=chat_id, photo=img, caption=f"📊 {sym} REVERSAL chart")
                        try:
                            os.remove(chart_path)
                        except Exception:
                            pass
                except Exception:
                    continue
                finally:
                    try:
                        del df_chart
                    except Exception:
                        pass
                    prune_runtime_caches()
    except Exception:
        pass

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
        extra_trade_fields = {}
        # Reversal engine calculates deterministic SL/TP/RR before AI.
        # Preserve those levels after AI approval so execution uses the same
        # setup that was validated, not the generic 1% fallback stop.
        if str(source.get("setup", "")).upper().startswith("REVERSAL"):
            rev = source.get("reversal", {}) or {}
            sl = safe_float(rev.get("sl"), 0)
            tp1 = safe_float(rev.get("tp1"), 0)
            tp2 = safe_float(rev.get("tp2"), 0)
            tp3 = safe_float(rev.get("tp3"), 0)
            rr_src = safe_float(source.get("rr") or rev.get("rr"), 0)
            if sl > 0:
                extra_trade_fields["stop_loss"] = sl
            if tp2 > 0:
                extra_trade_fields["take_profit"] = tp2
            if tp1 > 0:
                extra_trade_fields["tp1"] = tp1
            if tp2 > 0:
                extra_trade_fields["tp2"] = tp2
            if tp3 > 0:
                extra_trade_fields["tp3"] = tp3
            if rr_src > 0:
                dynamic_rr, rr_profile = rr_src, "reversal_rr_to_tp2"
            extra_trade_fields["setup"] = source.get("setup", "REVERSAL BREAKOUT")
            extra_trade_fields["reversal_rr"] = rr_src
        out.append({
            "symbol": symbol,
            "direction": direction,
            "scanner_score": round(scanner_score, 1),
            "confidence": max(0, min(100, confidence)),
            "success_probability": max(0, min(100, success_probability)),
            "reason": str(item.get("reason", item.get("why", "AI approved setup")))[:220],
            "dynamic_rr": dynamic_rr,
            "rr_profile": rr_profile,
            **extra_trade_fields,
        })
    return out

def _format_ai_confirmed(confirmed: List[Dict[str, Any]]) -> str:
    lines = ["✅ AI подтвердил сделки:"]
    for i, x in enumerate(confirmed, 1):
        extra = "\n🚀 Extended TP: ON" if x.get("extended_tp_mode") else ""
        reversal_extra = ""
        if str(x.get("setup", "")).upper().startswith("REVERSAL"):
            reversal_extra = (
                f"\n🧩 Setup: {str(x.get('setup', 'REVERSAL BREAKOUT')).upper()}"
                f"\n🛑 SL: {x.get('stop_loss', '-')}"
                f"\n🎯 TP1/TP2/TP3: {x.get('tp1', '-')}/{x.get('tp2', '-')}/{x.get('tp3', '-')}"
                f"\n📐 RR: {x.get('dynamic_rr', x.get('reversal_rr', '-'))}R"
            )
        lines.append(
            f"\n{i}. 🪙 {normalize_symbol(x.get('symbol', ''))}\n"
            f"📈 Direction: {x.get('direction')}\n"
            f"🎯 Scanner Score: {x.get('scanner_score', '-')}%\n"
            f"🧠 AI Confidence: {x.get('confidence', '-')}%\n"
            f"📊 Вероятность отработки: {x.get('success_probability', x.get('confidence', '-'))}%\n"
            f"📌 Причина: {x.get('reason', 'AI approved setup')}"
            f"{reversal_extra}"
            f"{extra}"
        )
    return "\n".join(lines)

async def ai_confirm(uid: str) -> str:
    s = get_settings(uid)
    active, msg = is_cooldown_active(uid)
    if active:
        LAST_AI_CONFIRMED[int(uid)] = []
        return msg + "\nAI Confirm заблокирован."
    candidates = [dict(c) for c in LAST_SCAN_RESULTS.get(int(uid), [])]
    candidates = [c for c in candidates if str(c.get("direction", "WAIT")).upper() in ["LONG", "SHORT"]]
    approval_limit = selected_top_limit(s, AI_APPROVAL_TOP_LIMIT)
    # Keep Hybrid strongest matches at the top for AI validation, then score.
    if str(s.get("scan_mode", get_scan_mode(uid))).lower() == "hybrid":
        candidates = sorted(
            candidates,
            key=lambda x: (int(x.get("hybrid_priority", 0) or 0), float(x.get("score", 0) or 0)),
            reverse=True,
        )
    else:
        candidates = sorted(candidates, key=lambda x: float(x.get("score", 0) or 0), reverse=True)
    if approval_limit is not None:
        candidates = candidates[:approval_limit]
    if not candidates:
        LAST_AI_CONFIRMED[int(uid)] = []
        return "Нет LONG/SHORT кандидатов. WAIT не отправляется в AI."

    # Real institutional context layer: BTC Trend / Funding / Open Interest
    if s.get("btc_trend_filter") or s.get("funding_filter") or s.get("open_interest_filter"):
        for c in candidates:
            inst_block = institutional_prompt_block(s, c)
            if inst_block:
                c["institutional_context"] = inst_block

    liquidity_status = ""
    if s.get("liquidity_filter"):
        liq_ok = False
        for c in candidates:
            liq_block = liquidity_prompt_block(c, s)
            if liq_block:
                c["liquidity_context"] = liq_block
                liq_ok = True
        liquidity_status = "🔥 Получил данные ликвидности" if liq_ok else "⚠️ Не получил данные ликвидности"

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
- Проверяй market structure, MTF confirmation, volume/RVOL, reasons, momentum, volatility, risk/reward, institutional_context и liquidity_context если они есть.
- REJECT, если структура слабая, рынок chop/range, MTF конфликтует, volume слабый, breakout сомнительный или риск/прибыль плохие.
- APPROVE только если направление LONG/SHORT подтверждается несколькими факторами одновременно.
- symbol должен быть из candidates.
- direction только LONG или SHORT.
- scanner_score должен быть реальным score кандидата из Candidates JSON, не MinScore.
- confidence число 0-100, отражает качество сетапа после проверки, а не просто score.
- success_probability число 0-100: вероятность отработки сделки по оценке AI.
- reason одна короткая причина до 160 символов: укажи главный structural/MTF/volume/RR/institutional/liquidity фактор.
- Не возвращай WAIT. Если сетап не подходит — просто не включай его в JSON.
- Не выдумывай новые монеты.

TopLimit сейчас: {top_limit_label(s)}.
Candidates JSON:
{json.dumps(candidates, ensure_ascii=False, default=str)}
"""
    scan_mode_for_ai = str(s.get("scan_mode", get_scan_mode(uid))).lower()
    if scan_mode_for_ai == "reversal":
        prompt = f"""Ты STRICT JSON AI approval engine для crypto trading.

Текущий режим: REVERSAL BREAKOUT.
{REVERSAL_AI_JSON_RULES}

Верни ТОЛЬКО валидный JSON, без markdown и текста до/после.

Формат ответа строго:
[
  {{"symbol":"BTCUSDT","direction":"LONG","scanner_score":88,"confidence":85,"success_probability":85,"reason":"Clean accumulation breakout with strong RVOL and 2.7R to resistance"}}
]

Если нет APPROVE-сетапов, верни строго пустой массив:
[]

Правила:
- Для Reversal Mode преимущественно подтверждай только LONG reversal setups.
- Не возвращай REJECT-объекты: отклонённые сетапы просто не включай в JSON.
- symbol должен быть из candidates.
- direction только LONG или SHORT, но SHORT разрешай только если candidate явно SHORT.
- confidence число 0-100.
- success_probability число 0-100.
- reason одна короткая причина до 160 символов.
- Не выдумывай новые монеты.

TopLimit сейчас: {top_limit_label(s)}.
Candidates JSON:
{json.dumps(candidates, ensure_ascii=False, default=str)}
"""
    elif scan_mode_for_ai == "hybrid":
        prompt = f"""Ты STRICT JSON AI approval engine для crypto trading.

Текущий режим: HYBRID.
{HYBRID_AI_JSON_RULES}

Верни ТОЛЬКО валидный JSON, без markdown и текста до/после.

Формат ответа строго:
[
  {{"symbol":"BTCUSDT","direction":"LONG","scanner_score":88,"confidence":85,"success_probability":85,"reason":"Reversal and momentum alignment with rising RVOL and clean structure"}}
]

Если нет APPROVE-сетапов, верни строго пустой массив:
[]

Правила:
- Для Hybrid Mode приоритет имеют REVERSAL+MOMENTUM и Priority HIGH.
- Разрешай только LONG, если режим Hybrid LONG-only.
- Не возвращай REJECT-объекты: отклонённые сетапы просто не включай в JSON.
- symbol должен быть из candidates.
- scanner_score должен быть реальным score кандидата из Candidates JSON.
- confidence число 0-100.
- success_probability число 0-100.
- reason одна короткая причина до 160 символов.
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
    LAST_AI_CONFIRMED[int(uid)] = [compact_scan_candidate(x) if isinstance(x, dict) else x for x in confirmed]
    confirmed = LAST_AI_CONFIRMED[int(uid)]
    prune_runtime_caches()
    if not confirmed:
        base_msg = "🧠 AI не подтвердил сделки из списка.\nSTRICT JSON: подтверждённых LONG/SHORT сделок нет."
        return (liquidity_status + "\n" + base_msg).strip() if liquidity_status else base_msg
    formatted = _format_ai_confirmed(confirmed)
    return (liquidity_status + "\n" + formatted).strip() if liquidity_status else formatted

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
                pos = await execute_real_trade(uid, sym, direction, x.get("stop_loss"), x.get("take_profit"), effective_rr_for_signal(x), tp1=x.get("tp1"), tp2=x.get("tp2"), tp3=x.get("tp3"), setup=x.get("setup"))
                opened.append(format_real_opened_message(pos) + ("\nExtended TP: ON" if x.get("extended_tp_mode") else ""))
            else:
                opened.append(f"PAPER {sym} {direction} — real execution OFF" + (" | Extended TP" if x.get("extended_tp_mode") else ""))
        except Exception as e:
            errors.append(f"{sym}: {compact_exchange_error(e, 220)}")
    return ("\n".join(opened) if opened else "") + (("\nОшибки:\n" + "\n".join(errors)) if errors else "")

async def run_auto_scanner_for_user(app: Application, uid: str):
    s = get_settings(uid)
    if s.get("auto_scanner_interval") == "off" or stop_all_active(uid):
        return

    # v0094: auto scanner task is registered by auto_scanner_loop before it starts.
    # Do not treat the current auto task as a duplicate of itself, otherwise
    # the first cycle exits immediately and Auto Scanner looks like it does not loop.
    current_task = asyncio.current_task()
    existing_task = USER_SCAN_TASKS.get(uid)
    if existing_task and existing_task is not current_task and not existing_task.done():
        return
    if USER_SCAN_LOCKS.get(uid):
        return

    n = int(s.get("scanner_size", 100))
    if current_task is not None:
        register_user_scan_task(uid, current_task)
    try:
        scan = await run_top_scan(uid, n, app, int(uid))
    except asyncio.CancelledError:
        return
    finally:
        if USER_SCAN_TASKS.get(uid) is current_task:
            USER_SCAN_TASKS.pop(uid, None)
    if stop_all_active(uid):
        return
    await app.bot.send_message(
        chat_id=int(uid),
        text=f"🔄 Auto Scanner Top completed\n{scan[:3600]}"[:3900],
        reply_markup=manual_confirm_keyboard(uid)
    )

async def auto_scanner_loop(app: Application):
    while True:
        await asyncio.sleep(30)
        now = time.time()
        for uid, s in _load_settings_cache_locked().items():
            if s.get("stop_all_enabled"):
                continue
            sec = auto_scanner_seconds(s.get("auto_scanner_interval", "off"))
            if sec <= 0:
                continue
            if now - float(s.get("auto_scanner_last_run",0) or 0) >= sec:
                if stop_all_active(uid) or USER_SCAN_LOCKS.get(str(uid)):
                    continue
                existing_task = USER_SCAN_TASKS.get(str(uid))
                if existing_task and not existing_task.done():
                    continue
                set_setting(uid, "auto_scanner_last_run", int(now))
                task = app.create_task(run_auto_scanner_for_user(app, uid))
                register_user_scan_task(str(uid), task)

async def position_sync_loop(app: Application):
    last = {}
    while True:
        await asyncio.sleep(30)
        now = time.time()
        for uid, s in _load_settings_cache_locked().items():
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
            timeout=45,
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

        settings = _load_settings_cache_locked()
        models = {DEFAULT_MODEL, *OLLAMA_MODELS}
        for s in settings.values():
            if isinstance(s, dict) and s.get("ai_provider") == "ollama":
                models.add(s.get("ollama_model", DEFAULT_MODEL))

        for model in {m for m in models if m}:
            await asyncio.to_thread(unload_ollama_model, model)
        LAST_OLLAMA_ACTIVITY = 0.0

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id(update)
    set_setting(uid, "mode", "signal")
    await show_inline_menu_message(update, context, f"🤖 Trading Bot v{BOT_VERSION}\n\nВыбери действие в inline-меню ниже.")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(help_text(), reply_markup=bottom_reply_keyboard())

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id(update)
    await update.message.reply_text(get_status_text(uid), reply_markup=main_menu(get_settings(uid)))

async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    started = time.perf_counter()
    uid = user_id(update)
    s = get_settings(uid)
    text = local_fast_ping_text(uid, started)
    msg = await update.message.reply_text(
        text + f"\n🏦 API биржи {s.get('exchange', '').upper()}: проверяю до {EXCHANGE_PING_TIMEOUT_SEC:g}s...\n\nДля полной AI проверки нажмите: 🧠 Ping AI",
        reply_markup=main_menu(get_settings(uid))
    )
    exchange_health = await check_exchange_api(uid)
    try:
        await msg.edit_text(
            text + f"\n🏦 API биржи {s.get('exchange', '').upper()}: {exchange_health}\n\nДля полной AI проверки нажмите: 🧠 Ping AI",
            reply_markup=main_menu(get_settings(uid))
        )
    except Exception:
        pass

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
        await update.message.reply_text(f"❌ Signal error: {compact_exchange_error(e, 1000)}")

async def ping_cmd_from_callback(context: ContextTypes.DEFAULT_TYPE, chat_id: int, uid: str):
    started = time.perf_counter()
    s = get_settings(uid)
    text = local_fast_ping_text(uid, started)
    await send_below_buttons(
        context,
        chat_id,
        text + f"\n🏦 API биржи {s.get('exchange', '').upper()}: проверяю до {EXCHANGE_PING_TIMEOUT_SEC:g}s...",
        uid
    )
    exchange_health = await check_exchange_api(uid)
    await send_below_buttons(
        context,
        chat_id,
        text + f"\n🏦 API биржи {s.get('exchange', '').upper()}: {exchange_health}",
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
            set_setting(uid, "mode", "signal")
            await say(
                f"🤖 Trading Bot v{BOT_VERSION}\n\nAI Chat OFF. Inline menu активировано.",
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
            await say("Timeframe:", timeframe_menu(get_settings(uid)), keep_menu_bottom=False)
        elif data == "menu:autoscanner":
            await say("Auto Scanner Top:", auto_scanner_menu(get_settings(uid)), keep_menu_bottom=False)
        elif data == "menu:toplimit":
            await say("📋 TopLimit — сколько лучших сетапов отправлять на AI approval:", top_limit_menu(s), keep_menu_bottom=False)
        elif data == "menu:structural":
            await say("Structural Layers:", structural_layers_menu(get_settings(uid)), keep_menu_bottom=False)
        elif data == "menu:scanmode":
            await say("⚙️ Scanner MODE:", scanner_mode_menu(get_settings(uid)), keep_menu_bottom=False)
        elif data == "menu:institutional":
            await say("🏦 Institutional Filters:", institutional_menu(get_settings(uid)), keep_menu_bottom=False)
        elif data == "menu:trademgmt":
            await say("Trade Management:", trade_mgmt_menu(get_settings(uid)), keep_menu_bottom=False)

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
                await say(f"❌ Model load error: {compact_exchange_error(e, 500)}")
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
            allowed = {"15m", "15m_1h", "1h_4h", "multi"}
            if val not in allowed:
                await say("❌ Unknown Timeframe", timeframe_menu(get_settings(uid)), keep_menu_bottom=False)
            else:
                fresh = set_setting(uid, "timeframe_mode", val)
                if get_settings(uid).get("timeframe_mode") != val:
                    await say("❌ Timeframe не сохранился. Проверь DATA_DIR/settings.json", timeframe_menu(get_settings(uid)), keep_menu_bottom=False)
                else:
                    await say(f"✅ Timeframe сохранён: {timeframe_label(val)}", timeframe_menu(fresh), keep_menu_bottom=False)
        elif data.startswith("scanmode:"):
            val = data.split(":", 1)[1].lower().strip()
            if val not in {"momentum", "reversal", "hybrid"}:
                await say("❌ Unknown scanner mode", scanner_mode_menu(get_settings(uid)), keep_menu_bottom=False)
            else:
                set_scan_mode(val, uid)
                if val == "momentum":
                    msg = "Momentum scanner active."
                elif val == "reversal":
                    msg = "Reversal Breakout engine active."
                else:
                    msg = f"Hybrid active: {str(get_settings(uid).get('hybrid_variant', get_hybrid_variant(uid))).upper()} — сначала Reversal, потом Momentum."
                await say(f"✅ Scanner MODE: {val.upper()}\n" + msg, scanner_mode_menu(get_settings(uid)), keep_menu_bottom=False)
        elif data.startswith("hybridvariant:"):
            val = data.split(":", 1)[1].lower().strip()
            if val not in {"light", "full"}:
                await say("❌ Unknown Hybrid variant", scanner_mode_menu(get_settings(uid)), keep_menu_bottom=False)
            else:
                set_hybrid_variant(val, uid)
                desc = "Reversal Top-N + Momentum confirm only for found Reversal coins" if val == "light" else "Reversal Top-N + full Momentum Top-N"
                await say(f"✅ Hybrid Variant: {val.upper()}\n{desc}", scanner_mode_menu(get_settings(uid)), keep_menu_bottom=False)

        elif data == "toggle:reversalcharts":
            new_state = not bool(get_settings(uid).get("reversal_charts", get_reversal_charts(uid)))
            set_reversal_charts(new_state, uid)
            await say(f"📊 Reversal charts: {'ON' if new_state else 'OFF'}", scanner_mode_menu(get_settings(uid)), keep_menu_bottom=False)
        elif data.startswith("autoscanner:"):
            val = data.split(":", 1)[1]
            if val != "off" and stop_all_active(uid):
                await say("🚨 STOP ALL is ON. Auto Scanner не включён.")
                return
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
            allowed = {"off", "trendline", "trendline_rs", "trendline_rs_volume", "structural_only"}
            if val not in allowed:
                await say("❌ Unknown Structural mode", structural_layers_menu(get_settings(uid)), keep_menu_bottom=False)
            else:
                fresh = set_setting(uid, "structural_mode", val)
                if get_settings(uid).get("structural_mode") != val:
                    await say("❌ Structural не сохранился. Проверь DATA_DIR/settings.json", structural_layers_menu(get_settings(uid)), keep_menu_bottom=False)
                else:
                    await say(f"✅ Structural сохранён: {structural_mode_label(val)}", structural_layers_menu(fresh), keep_menu_bottom=False)

        elif data.startswith("scan:"):
            n = int(data.split(":", 1)[1])
            if stop_all_active(uid):
                await say("🚨 STOP ALL is ON. Scan blocked.")
                return
            set_setting(uid, "scanner_size", n)
            old_task = USER_SCAN_TASKS.get(uid)
            if old_task and not old_task.done():
                await say(f"⏳ Top-{n} scan уже выполняется. Кнопки и сообщения доступны, дождись финального результата.")
            else:
                task = context.application.create_task(_run_scan_task(uid, n, context, chat_id))
                register_user_scan_task(uid, task)
                await say(f"🔎 Top-{n} scan запущен в фоне. Кнопки и сообщения доступны.", keep_menu_bottom=False)
        elif data == "status":
            await say(get_status_text(uid))
        elif data == "ping":
            started = time.perf_counter()
            s = get_settings(uid)
            text = local_fast_ping_text(uid, started)
            await say(text + f"\n🏦 API биржи {s.get('exchange', '').upper()}: проверяю до {EXCHANGE_PING_TIMEOUT_SEC:g}s...")
            exchange_health = await check_exchange_api(uid)
            await say(text + f"\n🏦 API биржи {s.get('exchange', '').upper()}: {exchange_health}")
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
            s_now = get_settings(uid)
            if not s_now.get("trade_mgmt_enabled", True):
                set_setting(uid, "live_trade_manager_enabled", False)
                await say("⚠️ Live TM requires Trade Mgmt ON. Live TM: OFF")
            else:
                new = not bool(s_now.get("live_trade_manager_enabled", False))
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
            reply_markup = manual_confirm_keyboard(uid) or InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Главное меню", callback_data="back:main")]
            ])
            await say(txt, reply_markup, keep_menu_bottom=False)
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
                        pos = await execute_real_trade(uid, sym, direction, x.get("stop_loss"), x.get("take_profit"), effective_rr_for_signal(x), tp1=x.get("tp1"), tp2=x.get("tp2"), tp3=x.get("tp3"), setup=x.get("setup"))
                        opened.append(format_real_opened_message(pos) + ("\nExtended TP: ON" if x.get("extended_tp_mode") else ""))
                    else:
                        opened.append(f"PAPER {sym} {direction} — real execution OFF" + (" | Extended TP" if x.get("extended_tp_mode") else ""))
                except Exception as e:
                    errors.append(f"{sym}: {compact_exchange_error(e, 260)}")
            if opened:
                await say("🛡 Risk Manager PASSED\n\n" + "\n".join(opened) + (("\n\nОшибки:\n" + "\n".join(errors)) if errors else ""))
            else:
                await say("❌ Сделка не открыта" + (("\n\nОшибки:\n" + "\n".join(errors)) if errors else "\nНет подтверждённых сделок."))
        elif data in ["toggle:btc_trend_filter", "toggle:funding_filter", "toggle:open_interest_filter", "toggle:liquidity_filter", "toggle:heatmap_strength"]:
            mapping = {
                "toggle:btc_trend_filter": "btc_trend_filter",
                "toggle:funding_filter": "funding_filter",
                "toggle:open_interest_filter": "open_interest_filter",
                "toggle:liquidity_filter": "liquidity_filter",
                "toggle:heatmap_strength": "heatmap_strength",
            }
            key = mapping[data]
            cur = bool(s.get(key, False))
            set_setting(uid, key, not cur)
            await say(f"✅ {key}: {'ON' if not cur else 'OFF'}", institutional_menu(get_settings(uid)), keep_menu_bottom=False)
        elif data == "cancel":
            await say("Отменено.")
        elif data == "toggle:trademgmt":
            cur = bool(s.get("trade_mgmt_enabled", True))
            new = not cur
            set_setting(uid, "trade_mgmt_enabled", new)
            if not new:
                set_setting(uid, "live_trade_manager_enabled", False)
            await say(f"✅ Trade Mgmt: {'ON' if new else 'OFF'}" + ("\n📈 Live TM: OFF" if not new else ""), trade_mgmt_menu(get_settings(uid)), keep_menu_bottom=False)
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
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Button error: {compact_exchange_error(e, 800)}")

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    uid = user_id(update)
    if txt.lower() in {"меню", "menu"}:
        set_setting(uid, "mode", "signal")
        await show_inline_menu_message(update, context, f"🤖 Trading Bot v{BOT_VERSION}\n\nAI Chat OFF. Inline menu активировано.")
        return
    if txt.lower() in {"/help", "help", "помощь"}:
        set_setting(uid, "mode", "signal")
        await update.message.reply_text(help_text(), reply_markup=bottom_reply_keyboard())
        return
    s = get_settings(uid)
    if s.get("mode") == "chat":
        try:
            quick = await quick_trade_chat_answer(uid, txt, context, update.effective_chat.id)
            if quick:
                await update.message.reply_text(quick[:1200])
                return

            trading_prompt = build_trading_chat_prompt(txt)
            ai = await call_ai(uid, trading_prompt, context, update.effective_chat.id, TRADING_CHAT_SYSTEM_PROMPT, options=AI_CHAT_OPTIONS)
            # In AI Chat mode do not block normal conversation with STRICT trade-execution validation.
            # STRICT validation remains active for signals/scanner/auto-execution.
            ai = sanitize_ai_chat_answer(ai)
            if not ai:
                raise RuntimeError("AI вернул пустой ответ. Проверь модель/API ключ или переключи Provider на Ollama.")
            await update.message.reply_text(ai[:1200])
        except Exception as e:
            await update.message.reply_text(f"AI error: {compact_exchange_error(e, 1000)}")
        return
    if re.fullmatch(r"[A-Za-z]{2,10}(USDT)?", txt):
        try:
            await update.message.reply_text(
                await signal_for_symbol(uid, txt, context=context, chat_id=update.effective_chat.id)
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Signal error: {compact_exchange_error(e, 1000)}")
    else:
        await update.message.reply_text("Напиши тикер, например BTC или ETH, либо /help.")

async def _legacy_button_disabled(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        exchange_health = await check_exchange_api(uid)
        await send_below_buttons(
            context, chat_id,
            local_fast_ping_text(uid, started) + f"\n🏦 API биржи {s.get('exchange', '').upper()}: {exchange_health}",
            uid
        )
    elif data == "status":
        await send_below_buttons(context, chat_id, get_status_text(uid), uid)
    elif data == "positions":
        await send_below_buttons(context, chat_id, await positions_text(uid), uid)
    elif data.startswith("scan:"):
        n = int(data.split(":")[1])
        set_setting(uid, "scanner_size", n)
        scan_text = await run_top_scan(uid, n, context, chat_id)
        reply_markup = manual_confirm_keyboard(uid)
        if reply_markup is None:
            reply_markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("🧠 AI Confirm", callback_data="ai_confirm")],
                [InlineKeyboardButton("⬅️ Главное меню", callback_data="back:main")]
            ])
        await send_below_buttons(
            context, chat_id,
            scan_text,
            uid,
            reply_markup=reply_markup
        )
    elif data == "ai_confirm":
        txt = await ai_confirm(uid)
        reply_markup = manual_confirm_keyboard(uid) or InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Главное меню", callback_data="back:main")]
        ])
        await send_below_buttons(
            context, chat_id,
            txt,
            uid,
            reply_markup=reply_markup
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
                    pos = await execute_real_trade(uid, sym, direction, x.get("stop_loss"), x.get("take_profit"), effective_rr_for_signal(x), tp1=x.get("tp1"), tp2=x.get("tp2"), tp3=x.get("tp3"), setup=x.get("setup"))
                    opened.append(format_real_opened_message(pos))
                else:
                    opened.append(f"PAPER {sym} {direction} — real execution OFF")
            except Exception as e:
                errors.append(f"{sym}: {compact_exchange_error(e, 260)}")
        if opened:
            await send_below_buttons(context, chat_id, ("🛡 Risk Manager PASSED\n\n" + "\n".join(opened) + ("\n\nОшибки:\n" + "\n".join(errors) if errors else "")), uid)
        else:
            await send_below_buttons(context, chat_id, ("❌ Сделка не открыта" + ("\n\nОшибки:\n" + "\n".join(errors) if errors else "\nНет подтверждённых сделок.")), uid)
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
            await send_below_buttons(context, chat_id, f"❌ Ошибка загрузки модели {model}: {compact_exchange_error(e, 1000)}", uid)
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
        if val != "off" and stop_all_active(uid):
            await send_below_buttons(context, chat_id, "🚨 STOP ALL is ON. Auto Scanner не включён.", uid)
            return
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
    elif data == "menu:institutional":
        await send_below_buttons(context, chat_id, "🏦 Institutional Filters:", uid, reply_markup=institutional_menu(get_settings(uid)))
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
        if not s_now.get("trade_mgmt_enabled", True):
            set_setting(uid, "live_trade_manager_enabled", False)
            await send_below_buttons(context, chat_id, "⚠️ Live TM requires Trade Mgmt ON. Live TM: OFF", uid)
        else:
            new = not bool(s_now.get("live_trade_manager_enabled", False))
            set_setting(uid, "live_trade_manager_enabled", new)
            await send_below_buttons(context, chat_id, f"✅ Live Trade Manager: {'ON' if new else 'OFF'}", uid)
    elif data == "toggle:trademgmt":
        s_now = get_settings(uid)
        new = not bool(s_now.get("trade_mgmt_enabled", True))
        set_setting(uid, "trade_mgmt_enabled", new)
        if not new:
            set_setting(uid, "live_trade_manager_enabled", False)
        await send_below_buttons(context, chat_id, f"✅ Trade Mgmt: {'ON' if new else 'OFF'}" + ("\n📈 Live TM: OFF" if not new else ""), uid, reply_markup=trade_mgmt_menu(get_settings(uid)))
    elif data in ["toggle:breakeven", "toggle:trailing", "toggle:partialtp"]:
        key = {"toggle:breakeven": "breakeven_enabled", "toggle:trailing": "trailing_enabled", "toggle:partialtp": "partial_tp_enabled"}[data]
        new = not bool(s.get(key))
        set_setting(uid, key, new)
        await send_below_buttons(context, chat_id, f"✅ {key}: {'ON' if new else 'OFF'}", uid, reply_markup=trade_mgmt_menu(get_settings(uid)))
    elif data in ["toggle:btc_trend_filter", "toggle:funding_filter", "toggle:open_interest_filter", "toggle:liquidity_filter", "toggle:heatmap_strength"]:
        mapping = {
            "toggle:btc_trend_filter": "btc_trend_filter",
            "toggle:funding_filter": "funding_filter",
            "toggle:open_interest_filter": "open_interest_filter",
            "toggle:liquidity_filter": "liquidity_filter",
            "toggle:heatmap_strength": "heatmap_strength",
        }
        key = mapping[data]
        new = not bool(s.get(key, False))
        set_setting(uid, key, new)
        await send_below_buttons(
            context,
            chat_id,
            f"✅ {key}: {'ON' if new else 'OFF'}",
            uid,
            reply_markup=institutional_menu(get_settings(uid))
        )
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
    uid = user_id(update)
    msg = await stop_all_pro(uid, context.application)
    await update.message.reply_text(msg, reply_markup=main_menu(get_settings(uid)))
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
    if not get_settings(uid).get("trade_mgmt_enabled", True):
        set_setting(uid, "live_trade_manager_enabled", False)
        await update.message.reply_text("⚠️ Live TM requires Trade Mgmt ON. Live TM: OFF", reply_markup=main_menu(get_settings(uid)))
        return
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
        f"Trade Mgmt: {'ON' if s.get('trade_mgmt_enabled', True) else 'OFF'}\n"
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

    api_key = get_openai_key(uid)
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

async def state_debug_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id(update)
    s = get_settings(uid)
    has_openai = bool(get_openai_key(uid))
    await update.message.reply_text(
        "🧪 State Debug\n"
        f"Version: {BOT_VERSION}\n"
        f"DATA_DIR: {DATA_DIR.resolve()}\n"
        f"settings.json: {SETTINGS_FILE.resolve()} exists={SETTINGS_FILE.exists()}\n"
        f"openai_keys.json: {OPENAI_KEYS_FILE.resolve()} exists={OPENAI_KEYS_FILE.exists()} has_key={has_openai}\n"
        f"Provider: {s.get('ai_provider')}\n"
        f"Model: {get_active_model(s)}\n"
        f"TF: {s.get('timeframe_mode')}\n"
        f"Structural: {s.get('structural_mode')}\n"
        f"AutoScanner: {s.get('auto_scanner_interval')}\n"
        f"TopLimit: {s.get('top_limit')}"
    )

async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check private futures balance without opening orders.

    This command is intentionally read-only. It helps diagnose whether API keys,
    futures read permissions, and private futures endpoints work before testing
    real order submit.
    """
    uid = user_id(update)
    try:
        ex = get_private_exchange(uid)
        s = get_settings(uid)
        ex_name = str(s.get("exchange", "mexc")).upper()

        balance = None
        last_error = None
        # Prefer explicit swap/futures balance, fallback to default balance for
        # exchanges/ccxt versions that ignore or reject the params.
        for params in ({"type": "swap"}, {"defaultType": "swap"}, {}):
            try:
                balance = ex.fetch_balance(params)
                break
            except Exception as e:
                last_error = e
                balance = None

        if balance is None:
            raise last_error or RuntimeError("fetch_balance returned no data")

        def coin_amount(section: str, coin: str = "USDT") -> float:
            data = balance.get(section, {})
            if isinstance(data, dict):
                return safe_float(data.get(coin, 0))
            return 0.0

        free = coin_amount("free")
        used = coin_amount("used")
        total = coin_amount("total")

        # Some ccxt futures responses keep account info in info.data instead of
        # normalized free/used/total. Try to extract a usable USDT value too.
        info = balance.get("info", {}) if isinstance(balance, dict) else {}
        extra_lines = []
        if total <= 0 and isinstance(info, dict):
            raw = info.get("data", info)
            candidates = raw if isinstance(raw, list) else [raw]
            for item in candidates:
                if not isinstance(item, dict):
                    continue
                currency = str(item.get("currency") or item.get("asset") or item.get("coin") or "").upper()
                if currency and currency != "USDT":
                    continue
                for key in ("availableBalance", "available", "cashBalance", "equity", "balance", "marginBalance"):
                    if key in item:
                        val = safe_float(item.get(key))
                        if val:
                            extra_lines.append(f"{key}: {val:.4f} USDT")

        # Lightweight extra read checks. They help tell if private futures API is
        # generally available, without submitting/canceling any order.
        positions_ok = "not checked"
        orders_ok = "not checked"
        try:
            ex.fetch_positions()
            positions_ok = "OK"
        except Exception as e:
            positions_ok = f"FAIL: {str(e)[:120]}"
        try:
            ex.fetch_open_orders()
            orders_ok = "OK"
        except Exception as e:
            orders_ok = f"FAIL: {str(e)[:120]}"

        lines = [
            f"💰 Futures Balance | {ex_name}",
            f"Free: {free:.4f} USDT",
            f"Used: {used:.4f} USDT",
            f"Total: {total:.4f} USDT",
        ]
        if extra_lines:
            lines.append("")
            lines.extend(extra_lines[:4])
        lines.extend([
            "",
            f"Positions API: {positions_ok}",
            f"Open orders API: {orders_ok}",
            "",
            "Read-only check. Orders are not sent.",
        ])
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text(
            "❌ Futures balance check failed\n"
            f"{compact_exchange_error(e, 800)}"
        )


async def ip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show direct public IP and, if configured, proxy public IP."""
    uid = user_id(update)
    services = [
        "https://api.ipify.org?format=json",
        "https://ifconfig.me/ip",
        "https://checkip.amazonaws.com",
    ]

    async def fetch_ip(label: str, proxy: bool = False) -> str:
        last = None
        for url in services:
            try:
                def _get():
                    kwargs = requests_proxy_kwargs(uid) if proxy else {}
                    r = requests.get(url, timeout=12, **kwargs)
                    r.raise_for_status()
                    return r.text.strip()
                raw = await asyncio.to_thread(_get)
                ip = raw
                if raw.startswith("{"):
                    try:
                        ip = json.loads(raw).get("ip", raw)
                    except Exception:
                        pass
                return f"✅ {label}: {ip}"
            except Exception as e:
                last = compact_exchange_error(e, 180)
        return f"❌ {label}: {last or 'failed'}"

    proxy_url = get_user_proxy(uid)
    lines = ["🌐 Bot IP check", await fetch_ip("Direct IP", proxy=False)]
    if proxy_url:
        lines.append(f"Proxy: {mask_proxy_url(proxy_url)}")
        lines.append(await fetch_ip("Proxy IP", proxy=True))
    else:
        lines.append("Proxy: not set")
    await update.message.reply_text("\n".join(lines))

async def api_check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Read-only diagnostics for private futures API and time drift.

    This does not submit/cancel orders. It helps distinguish HMAC/timestamp/read
    permission problems from order-submit WAF/geo blocks.
    """
    uid = user_id(update)
    s = get_settings(uid)
    ex_name = str(s.get("exchange", "mexc")).lower()
    lines = [f"🧪 API Check | {ex_name.upper()} | v{BOT_VERSION}", "Read-only: orders are NOT sent.", ""]
    try:
        ex = get_private_exchange(uid)
        lines.append(f"Exchange id: {getattr(ex, 'id', ex_name)}")
        proxy_url = get_user_proxy(uid)
        lines.append(f"Proxy: {mask_proxy_url(proxy_url) if proxy_url else 'not set'}")
        try:
            urls = getattr(ex, 'urls', {}) or {}
            api_urls = urls.get('api', urls)
            lines.append(f"API url: {str(api_urls)[:220]}")
        except Exception:
            pass

        # Local/server time drift check. Signature/timestamp errors often come
        # from a large drift, while HTML 403 usually means WAF/region/IP block.
        try:
            server_ms = await asyncio.to_thread(ex.fetch_time)
            local_ms = int(time.time() * 1000)
            if server_ms:
                drift = int(local_ms - int(server_ms))
                status = "OK" if abs(drift) <= 3000 else "WARN"
                lines.append(f"Server time: {status} drift={drift} ms")
            else:
                lines.append("Server time: not returned")
        except Exception as e:
            lines.append(f"Server time: FAIL {compact_exchange_error(e, 180)}")

        # Show CCXT recvWindow/options that may matter for signed endpoints.
        try:
            opts = getattr(ex, 'options', {}) or {}
            recv = opts.get('recvWindow') or opts.get('recvwindow') or opts.get('defaultRecvWindow')
            lines.append(f"recvWindow option: {recv or 'default'}")
            lines.append(f"adjustForTimeDifference: {opts.get('adjustForTimeDifference', False)}")
        except Exception:
            pass

        async def check(label, func):
            try:
                await asyncio.to_thread(func)
                lines.append(f"✅ {label}: OK")
            except Exception as e:
                msg = str(e).replace("\n", " ")[:260]
                lines.append(f"❌ {label}: {msg}")

        await check("load_markets", lambda: ex.load_markets())
        await check("futures balance", lambda: ex.fetch_balance({"type": "swap"}))
        await check("positions", lambda: ex.fetch_positions())
        await check("open orders", lambda: ex.fetch_open_orders())

        # Best-effort external IP diagnostics. Direct IP shows Railway NAT;
        # proxy IP shows what MEXC should see for private ccxt calls.
        try:
            def _ip_direct():
                r = requests.get("https://api.ipify.org?format=json", timeout=8)
                r.raise_for_status()
                return r.json().get("ip", r.text.strip())
            ip = await asyncio.to_thread(_ip_direct)
            lines.append(f"🌐 Direct IP: {ip}")
        except Exception as e:
            lines.append(f"🌐 Direct IP: FAIL {str(e)[:120]}")
        if proxy_url:
            try:
                def _ip_proxy():
                    r = requests.get("https://api.ipify.org?format=json", timeout=12, **requests_proxy_kwargs(uid))
                    r.raise_for_status()
                    return r.json().get("ip", r.text.strip())
                ip = await asyncio.to_thread(_ip_proxy)
                lines.append(f"🌐 Proxy IP: {ip}")
            except Exception as e:
                lines.append(f"🌐 Proxy IP: FAIL {compact_exchange_error(e, 180)}")

        lines.extend([
            "",
            "Если balance/positions OK, но order/submit даёт HTML 403 — это обычно WAF/IP/region block, а не подпись.",
        ])
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text("❌ API Check failed\n" + compact_exchange_error(e, 900))

async def proxy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Save per-user proxy URL used by private exchange/API calls."""
    uid = user_id(update)
    if not context.args:
        cur = get_user_proxy(uid)
        await update.message.reply_text(
            "Пример:\n/proxy socks5://login:password@host:port\n\n"
            f"Current proxy: {mask_proxy_url(cur) if cur else 'not set'}"
        )
        return
    proxy_url = " ".join(context.args).strip()
    try:
        saved = set_user_proxy(uid, proxy_url)
        await update.message.reply_text(
            "✅ Proxy saved\n"
            f"{mask_proxy_url(saved)}\n\n"
            "Теперь private exchange/API calls будут идти через proxy.\n"
            "Проверь: /ip и /api_check"
        )
    except Exception as e:
        await update.message.reply_text(
            "❌ Proxy not saved\n"
            f"{compact_exchange_error(e, 500)}\n\n"
            "Пример: /proxy socks5://login:password@host:port"
        )

async def del_proxy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id(update)
    existed = delete_user_proxy(uid)
    await update.message.reply_text("✅ Proxy deleted" if existed else "ℹ️ Proxy was not set")

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
    persist_openai_key(uid, context.args[0])
    set_ai_provider(uid, "openai")
    await update.message.reply_text("✅ OpenAI key saved\n🤖 Provider: OPENAI", reply_markup=main_menu(get_settings(uid)))

async def clearopenai_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id(update)
    existed = delete_openai_key(uid)
    set_ai_provider(uid, "ollama")
    msg = "✅ OpenAI key deleted" if existed else "ℹ️ OpenAI key was not saved"
    await update.message.reply_text(
        msg + "\n🤖 Provider: OLLAMA",
        reply_markup=main_menu(get_settings(uid))
    )

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

async def chat_exit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id(update)
    set_setting(uid, "mode", "signal")
    await update.message.reply_text("✅ AI Chat OFF. Бот вернулся в обычный режим. Auto Scanner продолжает работать.", reply_markup=main_menu(get_settings(uid)))

async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id(update)
    set_setting(uid, "mode", "signal")
    await show_inline_menu_message(update, context, f"🤖 Trading Bot v{BOT_VERSION}\n\nAI Chat OFF. Inline menu активировано.")

def live_tm_close_side(side: str) -> str:
    return "sell" if str(side).upper() == "LONG" else "buy"

def live_tm_exchange_symbol(ex, raw_symbol: str) -> str:
    try:
        return exchange_symbol_for_order(ex, raw_symbol)
    except Exception:
        markets = get_cached_markets(str(getattr(ex, "id", DEFAULT_EXCHANGE) or DEFAULT_EXCHANGE))
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

def live_tm_reduce_only_params(extra: Optional[Dict[str, Any]] = None, leverage=None) -> Dict[str, Any]:
    params = isolated_order_params(leverage, {"reduceOnly": True})
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

    exch_pos = fetch_exchange_position_for_local(ex, pos)
    amount = extract_position_amount(exch_pos) if exch_pos else safe_float(pos.get("amount") or pos.get("contracts") or pos.get("size"), 0)
    if amount <= 0:
        misses = int(pos.setdefault("tm", {}).get("exchange_position_misses", 0)) + 1
        pos["tm"]["exchange_position_misses"] = misses
        if misses >= 2:
            pos["status"] = "closed_on_exchange"
            pos["remaining_percent"] = 0
            return {"skipped": "position_not_found_on_exchange_confirmed", "misses": misses}
        return {"warning": "position_not_confirmed_on_exchange", "misses": misses}

    close_amount = live_tm_amount_to_precision(ex, symbol, amount * (float(percent) / 100.0))
    if close_amount <= 0:
        return {"skipped": "close_amount_zero"}

    order = create_reduce_only_market_order_adapter(
        ex,
        symbol,
        side,
        close_amount,
        pos.get("leverage") or s.get("leverage")
    )
    remaining_amount, remaining_pos = await confirm_exchange_remaining_amount(ex, pos, attempts=2, delay=0.7)
    remaining_confirmed = remaining_amount > 0
    if remaining_confirmed:
        pos.setdefault("tm", {})["exchange_position_misses"] = 0
        pos["amount"] = remaining_amount
        pos["remaining_percent"] = min(100, max(0, (remaining_amount / amount) * 100)) if amount else pos.get("remaining_percent", 100)
    else:
        # Do not rebuild SL/TP on the old full amount after Partial TP.
        # If the exchange does not return the remaining position immediately, use a conservative
        # estimated remainder from the executed reduceOnly amount and mark it unconfirmed.
        estimated_remaining = max(0.0, amount - close_amount)
        if float(percent) >= 99 or estimated_remaining <= 0:
            pos["amount"] = 0
            pos["remaining_percent"] = 0
            pos["status"] = "closed_on_exchange"
        else:
            pos["amount"] = estimated_remaining
            pos["remaining_percent"] = min(100, max(0, (estimated_remaining / amount) * 100)) if amount else max(0, safe_float(pos.get("remaining_percent", 100), 100) - float(percent))
            pos.setdefault("tm", {}).setdefault("warnings", []).append("Partial TP executed, but remaining exchange position was not confirmed; using estimated remaining amount for protection rebuild.")
    return {"order": str(order)[:500], "close_amount": close_amount, "exchange_remaining_amount": remaining_amount, "remaining_confirmed": remaining_confirmed, "local_remaining_amount": pos.get("amount")}

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

    exch_pos = fetch_exchange_position_for_local(ex, pos)
    amount = extract_position_amount(exch_pos) if exch_pos else safe_float(pos.get("amount") or pos.get("contracts") or pos.get("size"), 0)
    if amount <= 0:
        misses = int(pos.setdefault("tm", {}).get("exchange_position_misses", 0)) + 1
        pos["tm"]["exchange_position_misses"] = misses
        if misses >= 2:
            pos["status"] = "closed_on_exchange"
            pos["remaining_percent"] = 0
            return {"skipped": "position_not_found_on_exchange_confirmed", "misses": misses}
        return {"warning": "position_not_confirmed_on_exchange", "misses": misses}
    pos.setdefault("tm", {})["exchange_position_misses"] = 0
    close_amount = live_tm_amount_to_precision(ex, symbol, amount)

    if close_amount <= 0:
        return {"skipped": "runner_amount_zero"}

    order = create_reduce_only_market_order_adapter(
        ex,
        symbol,
        side,
        close_amount,
        pos.get("leverage") or s.get("leverage")
    )
    remaining_amount, remaining_pos = await confirm_exchange_remaining_amount(ex, pos, attempts=2, delay=0.7)
    if remaining_amount > 0:
        return {"warning": "runner_close_not_confirmed", "order": str(order)[:500], "close_amount": close_amount, "remaining_amount": remaining_amount}
    return {"order": str(order)[:500], "close_amount": close_amount, "closed_confirmed": True}

async def live_tm_place_or_replace_sl(uid: str, pos: Dict[str, Any], new_sl: float) -> Dict[str, Any]:
    """
    Best-effort SL replacement.
    Places new reduceOnly stop-market first, then cancels old known SL id.
    This keeps the old stop alive if the new SL cannot be placed.
    """
    s = get_settings(uid)
    if not s.get("real_execution_enabled", False):
        return {"skipped": "real_execution_off"}

    ex = get_private_exchange(uid)
    symbol = live_tm_exchange_symbol(ex, pos.get("symbol") or pos.get("market_symbol"))
    side = str(pos.get("direction") or pos.get("side")).upper()
    close_side = live_tm_close_side(side)

    exch_pos = fetch_exchange_position_for_local(ex, pos)
    amount = extract_position_amount(exch_pos) if exch_pos else safe_float(pos.get("amount") or pos.get("contracts") or pos.get("size"), 0)
    if amount <= 0:
        misses = int(pos.setdefault("tm", {}).get("exchange_position_misses", 0)) + 1
        pos["tm"]["exchange_position_misses"] = misses
        if misses >= 2:
            pos["status"] = "closed_on_exchange"
            pos["remaining_percent"] = 0
            return {"skipped": "position_not_found_on_exchange_confirmed", "misses": misses}
        return {"warning": "position_not_confirmed_on_exchange", "misses": misses}
    pos.setdefault("tm", {})["exchange_position_misses"] = 0
    pos["amount"] = amount
    sl_amount = live_tm_amount_to_precision(ex, symbol, amount)

    if sl_amount <= 0:
        return {"skipped": "sl_amount_zero"}

    tm = pos.setdefault("tm", {})
    old_sl_id = tm.get("sl_order_id") or pos.get("sl_order_id")

    errors = []
    typ = "market" if "mexc" in exchange_id(ex) else "stop_market"
    params_variants = protective_param_variants(ex, side, new_sl, "sl", pos.get("leverage") or s.get("leverage"))

    for params in params_variants:
        try:
            order = create_order_with_param_variants(ex, symbol, typ, close_side, sl_amount, None, [params])
            new_id = extract_order_id(order)
            if new_id:
                tm["sl_order_id"] = new_id
                pos["sl_order_id"] = new_id
            cancel_warning = None
            if old_sl_id and new_id:
                try:
                    ex.cancel_order(old_sl_id, symbol)
                except Exception as e:
                    cancel_warning = f"new SL placed, old SL cancel failed: {str(e)[:120]}"
                    tm.setdefault("warnings", []).append(cancel_warning)
                    tm["possible_duplicate_sl"] = True
            return {"order": str(order)[:500], "new_sl": new_sl, "type": typ, "order_id": new_id, "old_sl_id": old_sl_id, "cancel_warning": cancel_warning}
        except Exception as e:
            errors.append(f"{typ}: {compact_exchange_error(e, 180)}")

    return {"warning": "SL replace failed", "errors": errors[-5:]}

async def live_tm_place_or_replace_tp(uid: str, pos: Dict[str, Any], new_tp: float) -> Dict[str, Any]:
    """Best-effort TP replacement for the remaining position amount."""
    s = get_settings(uid)
    if not s.get("real_execution_enabled", False):
        return {"skipped": "real_execution_off"}

    ex = get_private_exchange(uid)
    symbol = live_tm_exchange_symbol(ex, pos.get("symbol") or pos.get("market_symbol"))
    side = str(pos.get("direction") or pos.get("side")).upper()
    close_side = live_tm_close_side(side)

    exch_pos = fetch_exchange_position_for_local(ex, pos)
    amount = extract_position_amount(exch_pos) if exch_pos else safe_float(pos.get("amount") or pos.get("contracts") or pos.get("size"), 0)
    if amount <= 0:
        misses = int(pos.setdefault("tm", {}).get("exchange_position_misses", 0)) + 1
        pos["tm"]["exchange_position_misses"] = misses
        if misses >= 2:
            pos["status"] = "closed_on_exchange"
            pos["remaining_percent"] = 0
            return {"skipped": "position_not_found_on_exchange_confirmed", "misses": misses}
        return {"warning": "position_not_confirmed_on_exchange", "misses": misses}
    pos.setdefault("tm", {})["exchange_position_misses"] = 0
    pos["amount"] = amount
    tp_amount = live_tm_amount_to_precision(ex, symbol, amount)
    if tp_amount <= 0:
        return {"skipped": "tp_amount_zero"}

    tm = pos.setdefault("tm", {})
    old_tp_id = tm.get("tp_order_id") or pos.get("tp_order_id")

    errors = []
    typ = "market" if "mexc" in exchange_id(ex) else "take_profit_market"
    params_variants = protective_param_variants(ex, side, new_tp, "tp", pos.get("leverage") or s.get("leverage"))
    for params in params_variants:
        try:
            order = create_order_with_param_variants(ex, symbol, typ, close_side, tp_amount, None, [params])
            new_id = extract_order_id(order)
            if new_id:
                tm["tp_order_id"] = new_id
                pos["tp_order_id"] = new_id
            cancel_warning = None
            if old_tp_id and new_id:
                try:
                    ex.cancel_order(old_tp_id, symbol)
                except Exception as e:
                    cancel_warning = f"new TP placed, old TP cancel failed: {str(e)[:120]}"
                    tm.setdefault("warnings", []).append(cancel_warning)
                    tm["possible_duplicate_tp"] = True
            return {"order": str(order)[:500], "new_tp": new_tp, "type": typ, "order_id": new_id, "old_tp_id": old_tp_id, "cancel_warning": cancel_warning}
        except Exception as e:
            errors.append(f"{typ}: {compact_exchange_error(e, 180)}")
    return {"warning": "TP replace failed", "errors": errors[-5:]}


async def live_tm_rebuild_protection_for_remaining(uid: str, pos: Dict[str, Any]) -> Dict[str, Any]:
    """After partial close, resize both SL and TP to remaining amount to avoid over-close conflicts."""
    results = {}
    current_sl = safe_float(pos.get("stop_loss") or pos.get("sl"), 0)
    current_tp = safe_float(pos.get("take_profit") or pos.get("tp1"), 0)
    if current_sl > 0:
        results["sl"] = await live_tm_place_or_replace_sl(uid, pos, current_sl)
    if current_tp > 0 and safe_float(pos.get("remaining_percent", 100), 100) > 0:
        results["tp"] = await live_tm_place_or_replace_tp(uid, pos, current_tp)
    return results


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

async def confirm_exchange_remaining_amount(ex, pos: Dict[str, Any], attempts: int = 2, delay: float = 0.7) -> Tuple[float, Optional[Dict[str, Any]]]:
    """Confirm remaining exchange position with a short retry to avoid false closes on transient empty responses."""
    last_pos = None
    for i in range(max(1, attempts)):
        try:
            last_pos = fetch_exchange_position_for_local(ex, pos)
            amount = extract_position_amount(last_pos) if last_pos else 0.0
            if amount > 0:
                return amount, last_pos
        except Exception as e:
            pos.setdefault("tm", {}).setdefault("warnings", []).append(f"confirm remaining failed: {compact_exchange_error(e, 160)}")
        if i < attempts - 1:
            await asyncio.sleep(delay)
    return 0.0, last_pos

def local_close_amount_from_position(pos: Dict[str, Any]) -> float:
    """Return best local remaining amount without double-applying remaining_percent after partial TP."""
    amount = safe_float(pos.get("amount") or pos.get("contracts") or pos.get("size"), 0)
    if amount <= 0:
        return 0.0
    initial = safe_float(pos.get("initial_amount"), 0)
    remaining_percent = safe_float(pos.get("remaining_percent", 100), 100)
    # Legacy compatibility: if local amount still equals the initial amount, apply remaining_percent.
    # In current versions, pos["amount"] is updated to the remaining exchange amount after partial TP.
    if initial > 0 and abs(amount - initial) <= max(1e-12, initial * 0.000001) and 0 < remaining_percent < 99.999:
        return amount * (remaining_percent / 100.0)
    return amount

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

    exch_pos = fetch_exchange_position_for_local(ex, pos)
    exch_amount = extract_position_amount(exch_pos) if exch_pos else 0.0
    amount = exch_amount if exch_amount > 0 else local_close_amount_from_position(pos)
    close_amount = live_tm_amount_to_precision(ex, symbol, amount)

    if close_amount <= 0:
        return {"skipped": "close_amount_zero"}

    order = create_reduce_only_market_order_adapter(
        ex,
        symbol,
        side,
        close_amount,
        pos.get("leverage") or s.get("leverage")
    )
    remaining_amount, remaining_pos = await confirm_exchange_remaining_amount(ex, pos, attempts=2, delay=0.7)
    if remaining_amount > 0:
        return {"warning": "close_not_confirmed", "order": str(order)[:500], "close_amount": close_amount, "remaining_amount": remaining_amount}
    return {"order": str(order)[:500], "close_amount": close_amount, "closed_confirmed": True}

async def stop_all_pro(uid: str, app=None) -> str:
    """
    Emergency STOP ALL:
    - turns off auto scanner
    - turns off trading
    - turns off real execution
    - turns off live trade manager
    - attempts to close tracked open positions reduceOnly BEFORE disabling real execution
    """
    scan_cancelled = cancel_user_scan(uid)
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
                if "order" in result and not result.get("warning"):
                    pos["status"] = "closed_by_stop_all"
                    pos["remaining_percent"] = 0
                results.append(f"{pos.get('symbol')}: {result}")
            except Exception as e:
                pos.setdefault("tm", {}).setdefault("errors", []).append(f"STOP ALL close error: {compact_exchange_error(e, 220)}")
                results.append(f"{pos.get('symbol')}: ERROR {compact_exchange_error(e, 180)}")
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
        f"Scan Task: {'CANCELLED' if scan_cancelled else 'OFF'}\n"
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


def live_tm_should_notify_trailing(pos: Dict[str, Any], new_sl: float, risk: float, now_ts: Optional[float] = None) -> bool:
    """Throttle trailing notifications: only significant moves or periodic heartbeat."""
    if risk <= 0 or new_sl <= 0:
        return False
    tm = pos.setdefault("tm", {})
    now_ts = now_ts or time.time()
    last_sl = safe_float(tm.get("last_trailing_notify_sl"), 0)
    last_ts = safe_float(tm.get("last_trailing_notify_ts"), 0)
    # Notify first real trailing move, any move >= 0.5R, or once per 15 minutes while trailing is active.
    if last_sl <= 0:
        return True
    if abs(new_sl - last_sl) >= risk * 0.5:
        return True
    if last_ts <= 0 or (now_ts - last_ts) >= 900:
        return True
    return False

async def manage_live_trades_for_user(uid: str, app=None):
    """
    Live Trade Manager.
    v0085:
    - sends Telegram notifications only for critical/key events
    - no spam for polling/retries/minor trailing updates
    - real actions only when real_execution_enabled=True
    """
    s = get_settings(uid)
    if not s.get("live_trade_manager_enabled", False):
        return
    # Live TM is an advanced layer over Trade Mgmt.
    # If Trade Mgmt is OFF, SL/TP are intentionally not managed.
    if not s.get("trade_mgmt_enabled", True):
        return

    positions = _positions(uid)
    if not positions:
        return

    changed = False

    for pos in positions:
        try:
            status = str(pos.get("status", "real_opened")).lower()
            if bool(pos.get("closed")) or bool(pos.get("closed_ts")) or status in {"closed", "done", "cancelled", "canceled", "closed_on_exchange", "closed_by_live_tm", "closed_by_stop_all"}:
                continue
            symbol = pos.get("symbol") or pos.get("market_symbol")
            side = str(pos.get("direction") or pos.get("side") or "").upper()
            entry = safe_float(pos.get("entry"), 0)
            sl = safe_float(pos.get("initial_stop_loss") or pos.get("sl") or pos.get("stop_loss"), 0)
            tp1 = safe_float(pos.get("tp1") or pos.get("take_profit"), 0)
            # Reversal/Hybrid may provide a three-target ladder. Prefer runner_target/TP3 for final close,
            # otherwise fall back to TP2, then take_profit for older positions.
            tp2 = safe_float(pos.get("runner_target") or pos.get("tp3") or pos.get("tp2") or pos.get("take_profit"), 0)

            # Live TM only manages positions that were opened with Trade Mgmt ON.
            # This prevents a later global toggle from managing intentionally unprotected entries.
            if pos.get("trade_mgmt_enabled") is False:
                continue
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

            # One clear notification that Live TM is active for this tracked real position.
            if not tm.get("live_tm_activated_notified"):
                tm["live_tm_activated_notified"] = True
                await notify_user(
                    app,
                    uid,
                    f"✅ Live TM activated\n{symbol} {side}\n"
                    f"BE={'ON' if breakeven_enabled else 'OFF'} | "
                    f"Partial TP={'ON' if partial_tp_enabled else 'OFF'} | "
                    f"Trailing={'ON' if trailing_enabled else 'OFF'}"
                )
                changed = True

            def hit_level(level: float) -> bool:
                if not level:
                    return False
                return (side == "LONG" and price >= level) or (side == "SHORT" and price <= level)

            # 1) BE move at configured R
            if breakeven_enabled and not tm.get("be_done") and hit_level(be_trigger):
                tm["new_sl"] = entry
                tm.setdefault("events", []).append(f"BE trigger hit. Move SL to entry {entry}.")

                result = {"mode": "local_only", "order": "paper"}
                if s.get("real_execution_enabled", False):
                    result = await live_tm_place_or_replace_sl(uid, pos, entry)
                    tm["be_order_result"] = result

                if "order" in result:
                    tm["be_done"] = True
                    tm["be_triggered_ts"] = time.time()
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
                else:
                    await notify_user(app, uid, f"❌ Breakeven SL update failed\n{symbol} {side}\nResult: {str(result)[:500]}")
                changed = True
            # 2) Partial close at configured R
            if partial_tp_enabled and not tm.get("partial_done") and hit_level(partial_trigger):
                tm["partial_close_percent"] = partial_tp_percent
                tm["partial_trigger"] = round(partial_trigger, 8)
                tm.setdefault("events", []).append(f"Partial TP hit at {partial_tp_r}R. Close {partial_tp_percent}%.")

                result = {"mode": "local_only", "order": "paper"}
                if s.get("real_execution_enabled", False):
                    result = await live_tm_partial_close(uid, pos, partial_tp_percent)
                    tm["partial_order_result"] = result

                if "order" in result:
                    tm["partial_done"] = True
                    tm["partial_triggered_ts"] = time.time()
                    if s.get("real_execution_enabled", False) and safe_float(pos.get("amount"), 0) > 0 and safe_float(pos.get("remaining_percent", 0), 0) > 0:
                        # live_tm_partial_close refreshes or estimates pos["amount"] and remaining_percent.
                        rebuild_result = await live_tm_rebuild_protection_for_remaining(uid, pos)
                        tm["rebuild_after_partial"] = rebuild_result
                    await notify_user(
                        app,
                        uid,
                        f"🎯 Partial TP executed\n"
                        f"{symbol} {side}\n"
                        f"✅ {partial_tp_percent}% closed\n"
                        f"Trigger: {round(partial_trigger, 8)} ({partial_tp_r}R)\n"
                        f"Price: {price}\n"
                        f"Result: {str(result)[:300]}"
                    )
                else:
                    await notify_user(app, uid, f"❌ Partial TP failed\n{symbol} {side}\nResult: {str(result)[:500]}")
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
                        if "order" in result:
                            new_sl_for_notice = safe_float(pos.get('stop_loss') or pos.get('sl'), 0)
                            if live_tm_should_notify_trailing(pos, new_sl_for_notice, risk):
                                tm["last_trailing_notify_sl"] = new_sl_for_notice
                                tm["last_trailing_notify_ts"] = time.time()
                                await notify_user(
                                    app,
                                    uid,
                                    f"🔄 Trailing updated\n"
                                    f"{symbol} {side}\n"
                                    f"Price: {price}\n"
                                    f"New SL: {pos.get('stop_loss') or pos.get('sl')}"
                                )
                        elif result.get("warning"):
                            await notify_user(app, uid, f"❌ Trailing SL update failed\n{symbol} {side}\nResult: {str(result)[:500]}")
                        changed = True

            # 5) Runner close at TP2
            if tp2 and not tm.get("runner_done") and hit_level(tp2):
                tm.setdefault("events", []).append("TP2 / runner target hit.")

                result = {"mode": "local_only", "order": "paper"}
                if s.get("real_execution_enabled", False):
                    result = await live_tm_close_runner(uid, pos)
                    tm["runner_order_result"] = result

                if "order" in result and not result.get("warning"):
                    tm["runner_done"] = True
                    tm["runner_done_ts"] = time.time()
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
                else:
                    await notify_user(app, uid, f"❌ Runner TP close failed\n{symbol} {side}\nResult: {str(result)[:500]}")
                changed = True

        except Exception as e:
            try:
                pos.setdefault("tm", {}).setdefault("errors", []).append(compact_exchange_error(e, 220))
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
            all_settings = _load_settings_cache_locked()
            for uid, s in all_settings.items():
                if s.get("live_trade_manager_enabled", False) and s.get("trade_mgmt_enabled", True):
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
        "scanner_mode_menu",
    ]

    missing = [x for x in required if x not in globals()]
    if missing:
        print("WARNING: missing menu functions:", missing)
    else:
        print("Menu validation OK")


async def cmd_scan_mode(update, context):
    try:
        uid = user_id(update)
        mode = (context.args[0] if context.args else "").lower()
        if mode not in ["momentum", "reversal", "hybrid"]:
            await update.message.reply_text("Usage: /scan_mode momentum|reversal|hybrid\nHybrid variant: /hybrid_light or /hybrid_full", reply_markup=scanner_mode_menu(get_settings(uid)))
            return
        set_scan_mode(mode, uid)
        await update.message.reply_text(f"✅ Scan mode set: {mode.upper()}", reply_markup=scanner_mode_menu(get_settings(uid)))
    except Exception as e:
        await update.message.reply_text(f"Mode error: {e}")

async def cmd_charts_on(update, context):
    uid = user_id(update)
    set_reversal_charts(True, uid)
    await update.message.reply_text("📊 Reversal charts: ON", reply_markup=scanner_mode_menu(get_settings(uid)))

async def cmd_charts_off(update, context):
    uid = user_id(update)
    set_reversal_charts(False, uid)
    await update.message.reply_text("📊 Reversal charts: OFF", reply_markup=scanner_mode_menu(get_settings(uid)))

async def cmd_hybrid_light(update, context):
    uid = user_id(update)
    set_hybrid_variant("light", uid)
    await update.message.reply_text("✅ Hybrid Variant: LIGHT\nReversal Top-N + Momentum confirm только по найденным Reversal монетам.", reply_markup=scanner_mode_menu(get_settings(uid)))

async def cmd_hybrid_full(update, context):
    uid = user_id(update)
    set_hybrid_variant("full", uid)
    await update.message.reply_text("✅ Hybrid Variant: FULL\nReversal Top-N + полный Momentum Top-N.", reply_markup=scanner_mode_menu(get_settings(uid)))

def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).concurrent_updates(False).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler(["exit", "chat_off"], chat_exit_cmd))
    app.add_handler(CommandHandler("callback_test", callback_test_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("ping", ping_cmd))
    app.add_handler(CommandHandler("ping_ai", ping_ai_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("api_check", api_check_cmd))
    app.add_handler(CommandHandler("ip", ip_cmd))
    app.add_handler(CommandHandler("proxy", proxy_cmd))
    app.add_handler(CommandHandler("del_proxy", del_proxy_cmd))
    app.add_handler(CommandHandler("scan_mode", cmd_scan_mode))
    app.add_handler(CommandHandler("charts_on", cmd_charts_on))
    app.add_handler(CommandHandler("charts_off", cmd_charts_off))
    app.add_handler(CommandHandler("hybrid_light", cmd_hybrid_light))
    app.add_handler(CommandHandler("hybrid_full", cmd_hybrid_full))
    app.add_handler(CommandHandler("signal", signal_cmd))
    app.add_handler(CommandHandler("positions", positions_cmd))
    app.add_handler(CommandHandler("structural", structural_cmd))
    app.add_handler(CommandHandler("autoscanner", autoscanner_cmd))
    app.add_handler(CommandHandler("autoscanner_off", autoscanner_off_cmd))
    app.add_handler(CommandHandler("setapi", setapi_cmd))
    app.add_handler(CommandHandler("setopenai", setopenai_cmd))
    app.add_handler(CommandHandler(["delopenai"], clearopenai_cmd))
    app.add_handler(CommandHandler("testai", testai_cmd))
    app.add_handler(CommandHandler("state_debug", state_debug_cmd))
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
    app.run_polling(drop_pending_updates=True)

# ===== Institutional Filters =====

def institutional_menu(settings):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📈 BTC Trend: {'ON' if settings.get('btc_trend_filter') else 'OFF'}", callback_data="toggle:btc_trend_filter")],
        [InlineKeyboardButton(f"💸 Funding: {'ON' if settings.get('funding_filter') else 'OFF'}", callback_data="toggle:funding_filter")],
        [InlineKeyboardButton(f"📊 Open Interest: {'ON' if settings.get('open_interest_filter') else 'OFF'}", callback_data="toggle:open_interest_filter")],
        [InlineKeyboardButton(f"🔥 Liquidity Filter: {'ON' if settings.get('liquidity_filter') else 'OFF'}", callback_data="toggle:liquidity_filter")],
        [InlineKeyboardButton(f"🧲 Heatmap Strength: {'ON' if settings.get('heatmap_strength') else 'OFF'}", callback_data="toggle:heatmap_strength")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back:main")]
    ])


INSTITUTIONAL_CACHE: Dict[str, Dict[str, Any]] = {}
INSTITUTIONAL_CACHE_TTL = int(os.getenv("INSTITUTIONAL_CACHE_TTL", "120"))
INSTITUTIONAL_TIMEOUT = float(os.getenv("INSTITUTIONAL_TIMEOUT", "5"))

def _inst_cache_get(key: str):
    item = INSTITUTIONAL_CACHE.get(key)
    if item and time.time() - item.get("ts", 0) < INSTITUTIONAL_CACHE_TTL:
        return item.get("data")
    return None

def _inst_cache_set(key: str, data):
    INSTITUTIONAL_CACHE[key] = {"ts": time.time(), "data": data}
    return data

def _inst_exchange(settings):
    try:
        return create_exchange(str(settings.get("exchange") or DEFAULT_EXCHANGE))
    except Exception:
        return create_exchange(DEFAULT_EXCHANGE)

def _inst_market_symbol(ex, symbol: str):
    try:
        return exchange_symbol_for_order(ex, symbol)
    except Exception:
        try:
            markets = get_cached_markets(str(getattr(ex, "id", DEFAULT_EXCHANGE) or DEFAULT_EXCHANGE))
            s = normalize_symbol(symbol)
            candidates = [
                s.replace("USDT", "/USDT:USDT"),
                s.replace("USDT", "/USDT"),
                s,
            ]
            for c in candidates:
                if c in markets:
                    return c
        except Exception:
            pass
    return symbol

def get_btc_trend_context(settings: Dict[str, Any]) -> str:
    cached = _inst_cache_get("btc_trend")
    if cached:
        return cached
    try:
        tf = "15m"
        df = add_indicators(fetch_ohlcv_for_symbol(str(settings.get("exchange") or DEFAULT_EXCHANGE), "BTCUSDT", tf, 120))
        last = df.iloc[-1]
        prev = df.iloc[-12]
        price = safe_float(last.get("close"), 0)
        change = (safe_float(last.get("close"), 0) / safe_float(prev.get("close"), 1) - 1) * 100
        ema20 = safe_float(last.get("ema20"), 0)
        ema50 = safe_float(last.get("ema50"), 0)

        if ema20 > ema50 and change > 0.15:
            state = "BULLISH"
        elif ema20 < ema50 and change < -0.15:
            state = "BEARISH"
        else:
            state = "NEUTRAL"

        return _inst_cache_set("btc_trend", f"BTC Trend: {state} | BTC {price:.2f} | 15m change {change:.2f}%")
    except Exception:
        return _inst_cache_set("btc_trend", "BTC Trend: unavailable")

def get_funding_context(symbol: str, settings: Dict[str, Any]) -> str:
    base = normalize_symbol(symbol)
    key = f"funding:{settings.get('exchange')}:{base}"
    cached = _inst_cache_get(key)
    if cached:
        return cached
    try:
        ex = _inst_exchange(settings)
        ms = _inst_market_symbol(ex, base)
        funding = None

        if hasattr(ex, "fetch_funding_rate"):
            data = ex.fetch_funding_rate(ms)
            if isinstance(data, dict):
                funding = data.get("fundingRate") or data.get("rate") or data.get("info", {}).get("fundingRate")

        if funding is None and hasattr(ex, "fetch_funding_rates"):
            data = ex.fetch_funding_rates([ms])
            if isinstance(data, dict):
                row = data.get(ms) or data.get(base)
                if isinstance(row, dict):
                    funding = row.get("fundingRate") or row.get("rate")

        rate = safe_float(funding, None)
        if rate is None:
            return _inst_cache_set(key, "Funding: unavailable")

        pct = rate * 100
        if pct > 0.08:
            status = "HIGH_POSITIVE"
        elif pct < -0.08:
            status = "HIGH_NEGATIVE"
        else:
            status = "NORMAL"

        return _inst_cache_set(key, f"Funding: {status} | {pct:.4f}%")
    except Exception:
        return _inst_cache_set(key, "Funding: unavailable")

def get_open_interest_context(symbol: str, settings: Dict[str, Any]) -> str:
    base = normalize_symbol(symbol)
    key = f"oi:{settings.get('exchange')}:{base}"
    cached = _inst_cache_get(key)
    if cached:
        return cached
    try:
        ex = _inst_exchange(settings)
        ms = _inst_market_symbol(ex, base)

        oi_now = None
        oi_prev = None

        if hasattr(ex, "fetch_open_interest_history"):
            try:
                hist = ex.fetch_open_interest_history(ms, timeframe="5m", limit=12)
                if hist and len(hist) >= 2:
                    oi_now = safe_float(hist[-1].get("openInterestValue") or hist[-1].get("openInterestAmount") or hist[-1].get("openInterest"), 0)
                    oi_prev = safe_float(hist[0].get("openInterestValue") or hist[0].get("openInterestAmount") or hist[0].get("openInterest"), 0)
            except Exception:
                pass

        if oi_now is None and hasattr(ex, "fetch_open_interest"):
            data = ex.fetch_open_interest(ms)
            if isinstance(data, dict):
                oi_now = safe_float(data.get("openInterestValue") or data.get("openInterestAmount") or data.get("openInterest"), 0)

        if not oi_now:
            return _inst_cache_set(key, "Open Interest: unavailable")

        if oi_prev and oi_prev > 0:
            change = (oi_now / oi_prev - 1) * 100
            if change > 2:
                status = "RISING"
            elif change < -2:
                status = "FALLING"
            else:
                status = "FLAT"
            return _inst_cache_set(key, f"Open Interest: {status} | change {change:.2f}%")

        return _inst_cache_set(key, f"Open Interest: received | value {oi_now:.4f}")
    except Exception:
        return _inst_cache_set(key, "Open Interest: unavailable")

def institutional_prompt_block(settings, candidate: Optional[Dict[str, Any]] = None):
    parts = []
    symbol = (candidate or {}).get("symbol", "BTCUSDT")

    if settings.get("btc_trend_filter"):
        parts.append(get_btc_trend_context(settings))
    if settings.get("funding_filter"):
        parts.append(get_funding_context(symbol, settings))
    if settings.get("open_interest_filter"):
        parts.append(get_open_interest_context(symbol, settings))

    return "\n".join([p for p in parts if p])

# ===== End Institutional Filters =====



# ===== Liquidity Filter =====

LIQUIDITY_CACHE: Dict[str, Dict[str, Any]] = {}
LIQUIDITY_CACHE_TTL = int(os.getenv("LIQUIDITY_CACHE_TTL", "180"))
LIQUIDITY_TIMEOUT = float(os.getenv("LIQUIDITY_TIMEOUT", "5"))

def _liq_symbol_base(symbol: str) -> str:
    s = normalize_symbol(str(symbol or "")).upper()
    return s.replace("USDT", "").replace("/", "").replace(":USDT", "")

def _liq_collect_numbers(obj, out=None):
    if out is None:
        out = []
    if isinstance(obj, dict):
        for v in obj.values():
            _liq_collect_numbers(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _liq_collect_numbers(v, out)
    else:
        try:
            f = float(obj)
            if f > 0:
                out.append(f)
        except Exception:
            pass
    return out

def _liq_collect_points(obj, points=None):
    if points is None:
        points = []
    if isinstance(obj, dict):
        price_keys = ["price", "liqPrice", "liquidationPrice", "priceLevel", "level", "y"]
        value_keys = ["value", "amount", "volume", "liquidity", "size", "long", "short", "sum", "qty"]
        price = None
        value = None
        for k in price_keys:
            if k in obj:
                price = safe_float(obj.get(k), 0)
                break
        for k in value_keys:
            if k in obj:
                value = safe_float(obj.get(k), 0)
                break
        if price and value:
            points.append((price, value))
        for v in obj.values():
            _liq_collect_points(v, points)
    elif isinstance(obj, (list, tuple)):
        # Many heatmap APIs return rows like [timestamp, price, value] or [price, value].
        nums = []
        for v in obj:
            if isinstance(v, (int, float, str)):
                try:
                    nums.append(float(v))
                except Exception:
                    pass
        if len(nums) >= 2:
            # Prefer the last two positive numeric values as price/value candidates.
            candidates = [n for n in nums if n > 0]
            if len(candidates) >= 2:
                p, val = candidates[-2], candidates[-1]
                if p > 0 and val > 0:
                    points.append((p, val))
        for v in obj:
            _liq_collect_points(v, points)
    return points

def _liq_fetch_json(url: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    headers = {
        "accept": "application/json,text/plain,*/*",
        "user-agent": "Mozilla/5.0",
        "origin": "https://www.coinglass.com",
        "referer": "https://www.coinglass.com/pro/futures/LiquidationHeatMap",
    }
    try:
        r = requests.get(url, params=params, headers=headers, timeout=LIQUIDITY_TIMEOUT)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None

def _liq_fetch_public_heatmap(symbol: str) -> Optional[Dict[str, Any]]:
    base = _liq_symbol_base(symbol)
    pair = f"{base}USDT"

    endpoints = [
        (
            "https://open-api.coinglass.com/public/v2/liqHeatmap",
            {"exchange": "Binance", "symbol": pair, "type": "24h"},
        ),
        (
            "https://open-api.coinglass.com/public/v2/aggregate/liqHeatmap",
            {"symbol": base, "type": "24h"},
        ),
    ]

    for url, params in endpoints:
        data = _liq_fetch_json(url, params)
        if isinstance(data, dict):
            # Accept common CoinGlass success wrappers.
            if data.get("success") is False or str(data.get("code", "")).startswith("4"):
                continue
            return data
    return None

def build_liquidity_summary(symbol: str, current_price: Optional[float] = None, direction: Optional[str] = None):
    """
    Free/public liquidation context layer.
    Uses legacy public CoinGlass endpoints when available.
    If data is blocked/unavailable, returns ok=False and the bot continues normally.
    """
    try:
        base = _liq_symbol_base(symbol)
        if not base:
            return {"ok": False, "summary": "", "strength": ""}

        cache_key = f"{base}:{round(safe_float(current_price, 0), 8)}"
        cached = LIQUIDITY_CACHE.get(cache_key)
        now = time.time()
        if cached and now - cached.get("ts", 0) < LIQUIDITY_CACHE_TTL:
            return dict(cached.get("data", {"ok": False, "summary": "", "strength": ""}))

        raw = _liq_fetch_public_heatmap(f"{base}USDT")
        if not raw:
            result = {"ok": False, "summary": "", "strength": ""}
            LIQUIDITY_CACHE[cache_key] = {"ts": now, "data": result}
            return result

        price = safe_float(current_price, 0)
        points = _liq_collect_points(raw)
        nums = _liq_collect_numbers(raw)

        # Fallback: if current price wasn't passed, estimate from median-like numeric range.
        if price <= 0:
            plausible = [n for n in nums if n > 0]
            if plausible:
                plausible = sorted(plausible)
                price = plausible[len(plausible)//2]

        if price <= 0:
            result = {"ok": True, "summary": "Liquidity data received. Direction unavailable.", "strength": "UNKNOWN"}
            LIQUIDITY_CACHE[cache_key] = {"ts": now, "data": result}
            return result

        above_value = 0.0
        below_value = 0.0
        for p, v in points:
            # Keep only price-like points near current price to avoid timestamps/ids.
            if price * 0.5 <= p <= price * 1.5:
                if p > price:
                    above_value += abs(v)
                elif p < price:
                    below_value += abs(v)

        if above_value <= 0 and below_value <= 0:
            # If parser cannot map exact points, still mark data received but neutral.
            result = {"ok": True, "summary": "Liquidity data received. Heatmap direction neutral.", "strength": "UNKNOWN"}
            LIQUIDITY_CACHE[cache_key] = {"ts": now, "data": result}
            return result

        total = above_value + below_value
        ratio = max(above_value, below_value) / total if total else 0

        if ratio >= 0.75:
            strength = "EXTREME"
        elif ratio >= 0.62:
            strength = "HIGH"
        elif ratio >= 0.55:
            strength = "MEDIUM"
        else:
            strength = "LOW"

        if above_value > below_value:
            summary = "Liquidity above current price. Short squeeze probability elevated. LONG bias."
            bias = "LONG"
        elif below_value > above_value:
            summary = "Liquidity below current price. Long liquidation risk elevated. SHORT bias."
            bias = "SHORT"
        else:
            summary = "Liquidity balanced around current price. Neutral bias."
            bias = "NEUTRAL"

        result = {
            "ok": True,
            "summary": summary,
            "strength": strength,
            "bias": bias,
            "above_value": round(above_value, 4),
            "below_value": round(below_value, 4),
        }
        LIQUIDITY_CACHE[cache_key] = {"ts": now, "data": result}
        return result
    except Exception:
        return {"ok": False, "summary": "", "strength": ""}

def liquidity_prompt_block(x, settings):
    if not settings.get("liquidity_filter"):
        return ""

    data = build_liquidity_summary(
        x.get("symbol", ""),
        current_price=safe_float(x.get("price"), 0),
        direction=x.get("direction"),
    )
    if not data.get("ok"):
        return ""

    parts = [f"Liquidity: {data.get('summary', '')}"]
    if settings.get("heatmap_strength") and data.get("strength"):
        parts.append(f"Heatmap Strength: {data.get('strength')}")
    return "\n".join([p for p in parts if p.strip()])

# ===== End Liquidity Filter =====

if __name__ == "__main__":
    main()
