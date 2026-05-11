
import os
import re
import json
import time
import asyncio
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

import ccxt
import pandas as pd
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters

BOT_VERSION = os.getenv("BOT_VERSION", "0020")
START_TIME = time.time()

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DATA_DIR.mkdir(exist_ok=True)
SETTINGS_FILE = DATA_DIR / "settings.json"
API_KEYS_FILE = DATA_DIR / "api_keys.json"
OPENAI_KEYS_FILE = DATA_DIR / "openai_keys.json"
POSITIONS_FILE = DATA_DIR / "positions.json"
COOLDOWN_FILE = DATA_DIR / "cooldown.json"
TRADE_EVENTS_FILE = DATA_DIR / "trade_events.json"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
OLLAMA_MODELS = [x.strip() for x in os.getenv("OLLAMA_MODELS", "llama3.1:8b,deepseek-r1:8b").split(",") if x.strip()]
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "llama3.1:8b")
DEFAULT_EXCHANGE = os.getenv("DEFAULT_EXCHANGE", "mexc").lower()

LAST_SCAN_RESULTS: Dict[int, List[Dict[str, Any]]] = {}
LAST_AI_CONFIRMED: Dict[int, List[Dict[str, Any]]] = {}

DEFAULT_SETTINGS: Dict[str, Any] = {
    "mode": "signal",
    "ai_provider": "ollama",
    "ollama_model": DEFAULT_MODEL,
    "openai_model": os.getenv("DEFAULT_OPENAI_MODEL", "gpt-4.1-mini"),
    "reasoning_level": os.getenv("DEFAULT_REASONING_LEVEL", "medium"),
    "exchange": DEFAULT_EXCHANGE,
    "trading_mode": "manual",
    "trading_enabled": False,
    "ai_auto": False,
    "risk_percent": float(os.getenv("DEFAULT_RISK_PERCENT", "1")),
    "max_trades": int(os.getenv("DEFAULT_MAX_TRADES", "3")),
    "max_total_risk": float(os.getenv("DEFAULT_MAX_TOTAL_RISK", "3")),
    "leverage": int(os.getenv("DEFAULT_LEVERAGE", "5")),
    "min_score": float(os.getenv("DEFAULT_MIN_SCORE", "80")),
    "top_limit": os.getenv("DEFAULT_TOP_LIMIT", "10"),
    "scanner_size": int(os.getenv("DEFAULT_SCANNER_SIZE", "100")),
    "market_universe": os.getenv("DEFAULT_MARKET_UNIVERSE", "all"),
    "timeframe_mode": os.getenv("DEFAULT_TIMEFRAME_MODE", "15m"),
    "session_filter": os.getenv("DEFAULT_SESSION_FILTER", "off").lower() == "on",
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
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def user_id(update: Update) -> str:
    return str(update.effective_user.id)

def get_settings(uid: str) -> Dict[str, Any]:
    data = load_json(SETTINGS_FILE, {})
    if uid not in data:
        data[uid] = dict(DEFAULT_SETTINGS)
        save_json(SETTINGS_FILE, data)
    merged = dict(DEFAULT_SETTINGS)
    merged.update(data.get(uid, {}))
    return merged

def set_setting(uid: str, key: str, value):
    data = load_json(SETTINGS_FILE, {})
    s = dict(DEFAULT_SETTINGS)
    s.update(data.get(uid, {}))
    s[key] = value
    data[uid] = s
    save_json(SETTINGS_FILE, data)

def normalize_symbol(symbol: str) -> str:
    s = symbol.upper().replace("/", "").replace(":USDT", "")
    if not s.endswith("USDT"):
        s += "USDT"
    return s

def get_active_model(settings: Dict[str, Any]) -> str:
    return settings.get("openai_model") if settings.get("ai_provider") == "openai" else settings.get("ollama_model", DEFAULT_MODEL)

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

def timeframe_label(mode: str) -> str:
    return {
        "15m": "15 мин",
        "15m_1h": "15 мин / 1 час",
        "1h_4h": "1 час / 4 часа",
        "multi": "мульти",
    }.get(mode, mode)

def auto_scanner_label(value: str) -> str:
    return {"15m": "15 мин", "60m": "60 мин", "4h": "4 часа", "12h": "12 часов", "24h": "24 часа", "off": "Выкл"}.get(value, "Выкл")

def auto_scanner_seconds(value: str) -> int:
    return {"15m": 900, "60m": 3600, "4h": 14400, "12h": 43200, "24h": 86400}.get(value, 0)

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
    cls = getattr(ccxt, exchange_name)
    params = {"enableRateLimit": True, "options": {"defaultType": "swap"}}
    if uid:
        keys = load_json(API_KEYS_FILE, {})
        if uid in keys and exchange_name in keys[uid]:
            params["apiKey"] = keys[uid][exchange_name].get("apiKey", "")
            params["secret"] = keys[uid][exchange_name].get("secret", "")
    return cls(params)

def fetch_ohlcv_for_symbol(exchange_name: str, symbol: str, timeframe: str = "15m", limit: int = 200) -> pd.DataFrame:
    ex = create_exchange(exchange_name)
    markets = ex.load_markets()
    norm = normalize_symbol(symbol)
    candidates = [norm.replace("USDT", "/USDT:USDT"), norm.replace("USDT", "/USDT")]
    market_symbol = next((c for c in candidates if c in markets), candidates[0])
    data = ex.fetch_ohlcv(market_symbol, timeframe=timeframe, limit=limit)
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

def score_market_multi(exchange_name: str, symbol: str, settings: Dict[str, Any], override: Optional[str] = None) -> Dict[str, Any]:
    primary, higher = timeframe_pair(settings, override)
    m = score_market(exchange_name, symbol, primary)
    details = [f"{primary}: {m['direction']} score {m['score']}"]
    m["mtf_confirmed"] = True
    if higher:
        h = score_market(exchange_name, symbol, higher)
        details.append(f"{higher}: {h['direction']} score {h['score']}")
        if m["direction"] != "WAIT" and h["direction"] not in [m["direction"], "WAIT"]:
            m["mtf_confirmed"] = False
            m["score"] = max(0, m["score"] - 15)
            m["reasons"].append("higher timeframe conflict")
    m["mtf_details"] = details
    return m

def get_top_symbols(exchange_name: str, limit: int) -> List[str]:
    try:
        ex = create_exchange(exchange_name)
        tickers = ex.fetch_tickers()
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
    return {"passed": bool(passed), "score_bonus": bonus, "low_touches": low_touches, "high_touches": high_touches, "compression": bool(compression), "direction_hint": hint, "summary": f"touches {low_touches}/{high_touches}, compression={compression}, pressure={hint}"}

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
        if uid is not None:
            await update_work_message(context, chat_id, str(uid), f"⬇️ Загрузка модели {model}: {percent}%")
        else:
            await context.bot.send_message(chat_id=chat_id, text=f"⬇️ Загрузка модели {model}: {percent}%")
    except Exception:
        pass


async def ensure_ollama_model(model: str, context: Optional[ContextTypes.DEFAULT_TYPE] = None, chat_id: Optional[int] = None, uid: Optional[str] = None) -> bool:
    """
    Ensures Ollama model is present. Sends simple 10/50/100 Telegram notifications.
    Real ollama pull progress is not stable enough to parse reliably across versions,
    so we report key phases.
    """
    if ollama_model_installed(model):
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


async def call_ollama_async(model: str, prompt: str, context: Optional[ContextTypes.DEFAULT_TYPE] = None, chat_id: Optional[int] = None, uid: Optional[str] = None) -> str:
    def post_api_chat():
        return requests.post(
            f"{OLLAMA_HOST}/api/chat",
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False
            },
            timeout=300
        )

    def post_api_generate():
        return requests.post(
            f"{OLLAMA_HOST}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False
            },
            timeout=300
        )

    def post_openai_compatible():
        return requests.post(
            f"{OLLAMA_HOST}/v1/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False
            },
            timeout=300
        )

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
            if not ollama_model_installed(model):
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
                r = requests.post(f"{OLLAMA_HOST}{endpoint}", json=payload, timeout=60)
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
        ex.fetch_ticker("BTC/USDT:USDT")
        return f"✅ OK ({round((time.perf_counter()-started)*1000)} ms)"
    except Exception:
        try:
            ex.fetch_ticker("BTC/USDT")
            return f"✅ OK ({round((time.perf_counter()-started)*1000)} ms)"
        except Exception as e:
            return f"❌ {str(e)[:160]}"


def call_ollama(model: str, prompt: str) -> str:
    r = requests.post(
        f"{OLLAMA_HOST}/api/chat",
        json={
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "stream": False
        },
        timeout=300
    )
    r.raise_for_status()
    data = r.json()
    return data.get("message", {}).get("content", "")

def call_openai(uid: str, model: str, prompt: str, reasoning: str) -> str:
    keys = load_json(OPENAI_KEYS_FILE, {})
    api_key = keys.get(uid) or os.getenv("OPENAI_API_KEY")
    if not api_key:
        return "OpenAI API key не задан. Используй /setopenai OPENAI_API_KEY"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.2}
    r = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=body, timeout=300)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]

async def call_ai(uid: str, prompt: str, context: Optional[ContextTypes.DEFAULT_TYPE] = None, chat_id: Optional[int] = None) -> str:
    s = get_settings(uid)
    if s.get("ai_provider") == "openai":
        return await asyncio.to_thread(call_openai, uid, s.get("openai_model"), prompt, s.get("reasoning_level"))
    return await call_ollama_async(s.get("ollama_model", DEFAULT_MODEL), prompt, context, chat_id)

def build_signal_prompt(symbol: str, timeframe: str, market: Dict[str, Any], settings: Dict[str, Any]) -> str:
    return f"""
Ты AI trading analyst. Дай краткий анализ futures setup.

Symbol: {symbol}
Timeframe: {timeframe}
Exchange: {settings['exchange']}
Direction: {market.get('direction')}
Score: {market.get('score')}
Price: {market.get('price')}
Reasons: {market.get('reasons')}
MTF: {market.get('mtf_details')}
Structural: {market.get('structural')}
Strict AI: {settings.get('strict_ai_mode')}
Ответь с полями:
Direction, Confidence %, Reasoning, Entry, SL, TP, Risk.
"""

WORK_MESSAGE_IDS_FILE = DATA_DIR / "work_message_ids.json"

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


async def update_work_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, uid: str, text: str, reply_markup=None):
    """
    One active work message for buttons/menu/status/ping/scan/model loading.
    AI Chat and trading signals are intentionally NOT routed here.
    """
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
    # Backward-compatible wrapper: now updates one active work message.
    await update_work_message(context, chat_id, uid, text, reply_markup)


def build_main_menu(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    universe_label = "🌐 Only BTC/ETH" if settings.get("market_universe") == "btc_eth" else "🌐 All Futures Market"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📈 Signal Mode", callback_data="mode:signal"), InlineKeyboardButton("💬 AI Chat Mode", callback_data="mode:chat")],
        [InlineKeyboardButton("🤖 AI Provider", callback_data="menu:provider"), InlineKeyboardButton("🧠 Model", callback_data="menu:model")],
        [InlineKeyboardButton("🧠 Reasoning", callback_data="menu:reasoning"), InlineKeyboardButton("🏦 Exchange", callback_data="menu:exchange")],
        [InlineKeyboardButton("🤖 Trading Mode", callback_data="menu:tradingmode"), InlineKeyboardButton("🕘 Таймфрейм", callback_data="menu:timeframe")],
        [InlineKeyboardButton("🌏 Азия/Америка", callback_data="toggle:sessions"), InlineKeyboardButton(universe_label, callback_data="toggle:btceth")],
        [InlineKeyboardButton("🔄 Auto Scanner Top", callback_data="menu:autoscanner"), InlineKeyboardButton("🧠 Structural Layers", callback_data="menu:structural")],
        [InlineKeyboardButton("📊 Positions", callback_data="positions"), InlineKeyboardButton("🛡 Trade Mgmt", callback_data="menu:trademgmt")],
        [InlineKeyboardButton("🚨 STOP ALL", callback_data="toggle:stopall"), InlineKeyboardButton("🔁 Position Sync", callback_data="toggle:positionsync")],
        [InlineKeyboardButton("📋 Статус", callback_data="status")],
        [InlineKeyboardButton("🔥 Top-50 Signal", callback_data="scan:50"), InlineKeyboardButton("🔥 Top-100 Signal", callback_data="scan:100")],
        [InlineKeyboardButton("🔥 Top-200 Signal", callback_data="scan:200"), InlineKeyboardButton("📡 Ping", callback_data="ping")],
        [InlineKeyboardButton("❓ Help", callback_data="help")],
    ])

main_menu = build_main_menu

def auto_scanner_menu(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    modes = [("15m", "15 мин"), ("60m", "60 мин"), ("4h", "4 часа"), ("12h", "12 часов"), ("24h", "24 часа"), ("off", "Выкл")]
    cur = settings.get("auto_scanner_interval", "off")
    rows = [[InlineKeyboardButton(("✅ " if cur == k else "") + label, callback_data=f"autoscanner:{k}")] for k, label in modes]
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="back:main")])
    return InlineKeyboardMarkup(rows)

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

def timeframe_menu(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    modes = [("15m", "15 мин"), ("15m_1h", "15 мин/1час"), ("1h_4h", "1 час/4 часа"), ("multi", "мульти")]
    return InlineKeyboardMarkup([[InlineKeyboardButton(("✅ " if settings.get("timeframe_mode") == k else "") + label, callback_data=f"timeframe:{k}")] for k, label in modes] + [[InlineKeyboardButton("⬅️ Назад", callback_data="back:main")]])

def model_menu(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    if settings.get("ai_provider") == "openai":
        models = ["gpt-5.5", "gpt-5.5-thinking", "gpt-5.5-mini", "gpt-4.1-mini", "gpt-4.1", "gpt-4o-mini", "gpt-4o"]
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

async def execute_real_trade(uid: str, symbol: str, direction: str, stop_loss=None, take_profit=None) -> Dict[str, Any]:
    if stop_all_active(uid):
        raise ValueError("🚨 STOP ALL is ON. Execution blocked.")
    s = get_settings(uid)
    if not s.get("real_execution_enabled"):
        raise ValueError("Real execution OFF. Use /real_on.")
    if not s.get("trading_enabled"):
        raise ValueError("Trading OFF. Use /trading_on.")
    if s["exchange"] not in ["mexc", "bingx"]:
        raise ValueError("Real execution supports MEXC/BingX.")
    active, msg = is_cooldown_active(uid)
    if active:
        raise ValueError(msg)
    ex = get_private_exchange(uid)
    ms = exchange_symbol_for_order(ex, symbol)
    ticker = ex.fetch_ticker(ms)
    entry = safe_float(ticker.get("last") or ticker.get("close"))
    if not stop_loss:
        stop_loss = entry * (0.99 if direction.upper() == "LONG" else 1.01)
    if not take_profit:
        dist = abs(entry - stop_loss)
        take_profit = entry + dist*2 if direction.upper() == "LONG" else entry - dist*2
    balance = get_usdt_free_balance(ex)
    lev = int(s["leverage"])
    amount = calc_amount_from_risk(entry, stop_loss, balance, float(s["risk_percent"]), lev)
    amount = float(ex.amount_to_precision(ms, amount))
    warnings = set_isolated_and_leverage(ex, ms, lev)
    entry_order = ex.create_order(ms, "market", side_for(direction), amount, None, {"marginMode": "isolated"})
    sl_order = place_protective_order(ex, ms, direction, amount, stop_loss, "sl")
    tp_order = place_protective_order(ex, ms, direction, amount, take_profit, "tp")
    pos = {"symbol": normalize_symbol(symbol), "market_symbol": ms, "exchange": s["exchange"], "direction": direction.upper(), "entry": round(entry,8), "amount": amount, "stop_loss": round(stop_loss,8), "take_profit": round(take_profit,8), "leverage": lev, "margin_mode": "isolated", "status": "real_opened", "remaining_percent": 100, "opened_ts": time.time(), "warnings": warnings, "entry_order": str(entry_order)[:500], "sl_order": str(sl_order)[:500], "tp_order": str(tp_order)[:500]}
    ps = _positions(uid); ps.append(pos); _save_positions(uid, ps)
    return pos

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
📋 TopLimit: {s.get('top_limit')}
🕒 Timeframe: {timeframe_label(s.get('timeframe_mode'))}
🌏 Asia/America: {'ON' if s.get('session_filter') else 'OFF'}

🤖 Trading Mode: {s.get('trading_mode').upper()}
🚀 Trading Enabled: {'ON' if s.get('trading_enabled') else 'OFF'}
💸 Real Execution: {'ON' if s.get('real_execution_enabled') else 'OFF'}
🚨 STOP ALL: {'ON' if s.get('stop_all_enabled') else 'OFF'}
🔁 Position Sync: {'ON' if s.get('position_sync_enabled') else 'OFF'}
🧠 Strict AI Mode: {'ON' if s.get('strict_ai_mode') else 'OFF'}
🏦 Margin Mode: ISOLATED only

📉 Risk: {s.get('risk_percent')}%
📈 Leverage: x{s.get('leverage')}
"""

def help_text() -> str:
    return f"""🤖 Trading Bot v{BOT_VERSION}

/start
/help
/status
/ping
/signal BTC

AI:
/provider_ollama
/provider_openai
/openai_on
/openai_off
/setopenai OPENAI_API_KEY
/testai
/strictai_on
/strictai_off

Scanner:
/top50
/top100
/top200
/minscore 80
/toplimit 10
/toplimit all

Structural:
/structural
OFF / Trendline / Trendline + RS/BTC / Trendline + RS/BTC + Super Volume / Structural Only

Auto Scanner:
/autoscanner
/autoscanner_off

Trading:
/trading_on
/trading_off
/real_on
/real_off
/risk 1
/leverage 5
/aiauto_on
/aiauto_off

Trade Management:
/positions
/breakeven_on
/breakeven_off
/trailing_on
/trailing_off
/partialtp_on
/partialtp_off
/partialtp 50 1

Safety:
/stopall_on
/stopall_off
/positionsync_on
/positionsync_off
/positionsync_now

Version: {BOT_VERSION}
"""

async def signal_for_symbol(uid: str, symbol: str, timeframe: Optional[str] = None, context: Optional[ContextTypes.DEFAULT_TYPE] = None, chat_id: Optional[int] = None) -> str:
    s = get_settings(uid)
    if not allowed_by_market_universe(symbol, s):
        return "🟠 Включен режим Only BTC/ETH. Доступны только BTCUSDT и ETHUSDT."
    primary_tf, _ = timeframe_pair(s, timeframe)
    df = add_indicators(fetch_ohlcv_for_symbol(s["exchange"], symbol, primary_tf, 180))
    market = score_market_multi(s["exchange"], symbol, s, timeframe)
    market = apply_structural_layers(s["exchange"], symbol, df, market, s)
    tf_display = timeframe if timeframe else timeframe_label(s.get("timeframe_mode"))
    prompt = build_signal_prompt(normalize_symbol(symbol), tf_display, market, s)
    prompt += "\n\nStructural Layers:\n" + "\n".join(structural_summary_lines(market))
    ai = await call_ai(uid, prompt, context, chat_id)
    validate_ai_response_or_raise(s, ai)
    ext_on, conf = should_enable_extended_tp(s, market, ai)
    if ext_on:
        ai += f"\n\n🚀 Extended TP Mode: ON\nReason: Trendline + RS/BTC + Super Volume + AI confidence {conf}%\nTarget logic: wider TP ~1:{s.get('extended_tp_rr')} RR."
    return f"📊 {normalize_symbol(symbol)} | {s['exchange'].upper()} | {tf_display}\n🤖 {s['ai_provider']} / {get_active_model(s)}\n🕒 MTF: {'✅ confirmed' if market.get('mtf_confirmed', True) else '❌ not confirmed'}\n🧠 Structural: {structural_mode_label(s.get('structural_mode'))}{' | ✅ passed' if market.get('structural_passed') else ''}\n🚀 Extended TP: {'ON' if ext_on else 'OFF'}\n\n{ai}"

async def run_top_scan(uid: str, n: int, context: Optional[ContextTypes.DEFAULT_TYPE] = None, chat_id: Optional[int] = None) -> str:
    s = get_settings(uid)
    set_setting(uid, "scanner_size", n)
    symbols = ["BTCUSDT", "ETHUSDT"] if s.get("market_universe") == "btc_eth" else get_top_symbols(s["exchange"], n)
    results = []

    total = max(len(symbols), 1)
    progress_sent = set()

    async def send_scan_progress(percent: int):
        if context is not None and chat_id is not None and percent not in progress_sent:
            progress_sent.add(percent)
            try:
                await update_work_message(
                    context,
                    chat_id,
                    uid,
                    f"🔎 Top-{n} scan: {percent}% просканировано"
                )
            except Exception:
                pass

    await send_scan_progress(10)

    for idx, sym in enumerate(symbols, 1):
        try:
            primary_tf, _ = timeframe_pair(s)
            df = add_indicators(fetch_ohlcv_for_symbol(s["exchange"], sym, primary_tf, 180))
            m = score_market_multi(s["exchange"], sym, s)
            m = apply_structural_layers(s["exchange"], sym, df, m, s)
            if m["direction"] != "WAIT" and (s.get("structural_mode") == "structural_only" or m["score"] >= float(s["min_score"])):
                results.append({"symbol": sym, **m})
        except Exception:
            continue

        current_percent = int((idx / total) * 100)
        if current_percent >= 50:
            await send_scan_progress(50)

    await send_scan_progress(100)

    results.sort(key=lambda x: x.get("score",0), reverse=True)
    limit = s.get("top_limit", "10")
    if str(limit).lower() != "all":
        results = results[:int(limit)]
    LAST_SCAN_RESULTS[int(uid)] = results
    if not results:
        return f"🔎 Top-{n} scan: нет монет, прошедших фильтры."
    lines = [f"🔥 Top-{n} Signal | {s['exchange'].upper()}", f"🎯 MinScore: {s['min_score']}%", f"📋 TopLimit: {s['top_limit']}", f"🧠 Structural: {structural_mode_label(s.get('structural_mode'))}", ""]
    for i,r in enumerate(results,1):
        lines.append(f"{i}. {r['symbol']} — {r['direction']} | score {r['score']}% | {'MTF ✅' if r.get('mtf_confirmed', True) else 'MTF ❌'}")
    lines.append("\nНажми AI Confirm, чтобы отправить кандидатов выбранному AI.")
    return "\n".join(lines)[:3900]
async def ai_confirm(uid: str) -> str:
    s = get_settings(uid)
    active, msg = is_cooldown_active(uid)
    if active:
        LAST_AI_CONFIRMED[int(uid)] = []
        return msg + "\nAI Confirm заблокирован."
    candidates = LAST_SCAN_RESULTS.get(int(uid), [])
    if not candidates:
        LAST_AI_CONFIRMED[int(uid)] = []
        return "Нет кандидатов. Сначала запусти Top Signal."
    prompt = f"""Ты AI risk/confirmation engine. Подтверди только лучшие сделки.
Верни JSON list:
[{{"symbol":"BTCUSDT","direction":"LONG","confidence":85,"reason":"..."}}]
Candidates:
{json.dumps(candidates[:20], ensure_ascii=False, default=str)}
"""
    raw = await call_ai(uid, prompt)
    validate_ai_response_or_raise(s, raw)
    try:
        m = re.search(r"\[.*\]", raw, re.S)
        confirmed = json.loads(m.group(0)) if m else []
    except Exception:
        confirmed = []
    confirmed = confirmed[:int(s.get("max_trades", 3))]
    for x in confirmed:
        conf = float(x.get("confidence", 0) or 0)
        x["extended_tp_mode"] = bool(s.get("extended_tp_enabled") and s.get("structural_mode") == "trendline_rs_volume" and conf >= float(s.get("extended_tp_min_confidence", 80)))
        if x["extended_tp_mode"]:
            x["tp_profile"] = "extended"
            x["extended_tp_rr"] = float(s.get("extended_tp_rr", 4))
    LAST_AI_CONFIRMED[int(uid)] = confirmed
    if not confirmed:
        return "🧠 AI не подтвердил сделки из списка.\n\nОтвет AI:\n" + raw[:2500]
    return "\n".join(["🧠 AI подтвердил сделки:"] + [f"{i}. {normalize_symbol(x.get('symbol',''))} — {x.get('direction')} | {x.get('confidence','-')}%" + (" | 🚀 Extended TP" if x.get("extended_tp_mode") else "") + f"\n   {x.get('reason','')}" for i,x in enumerate(confirmed,1)])

async def execute_confirmed_from_auto(uid: str) -> str:
    if stop_all_active(uid):
        return "🚨 STOP ALL is ON. Auto execution blocked."
    confirmed = LAST_AI_CONFIRMED.get(int(uid), [])
    if not confirmed:
        return "STRICT AI MODE: нет AI-approved сделок. Auto execution blocked."
    s = get_settings(uid)
    if s.get("trading_mode") != "auto" or not s.get("trading_enabled") or not s.get("ai_auto"):
        return "Auto Scanner нашел сделки, но Auto/Trading/AI Auto не включены."
    opened, errors = [], []
    for x in confirmed[:int(s.get("max_trades",3))]:
        sym = normalize_symbol(x.get("symbol",""))
        direction = x.get("direction","LONG").upper()
        try:
            if s.get("real_execution_enabled"):
                pos = await execute_real_trade(uid, sym, direction, x.get("stop_loss"), x.get("take_profit"))
                opened.append(f"REAL {sym} {direction} @ {pos['entry']} | isolated x{pos['leverage']}" + (" | Extended TP" if x.get("extended_tp_mode") else ""))
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
    confirm = await ai_confirm(uid)
    exec_txt = await execute_confirmed_from_auto(uid)
    await app.bot.send_message(chat_id=int(uid), text=f"🔄 Auto Scanner Top completed\n{scan[:1200]}\n\n{confirm[:1200]}\n\n{exec_txt[:900]}"[:3900])

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
                await run_auto_scanner_for_user(app, uid)

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

async def unload_idle_models():
    while True:
        await asyncio.sleep(1200)
        # Lightweight placeholder: Ollama keeps model management internally.
        pass

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id(update)
    s = get_settings(uid)
    msg = await update.message.reply_text(f"🤖 Trading Bot v{BOT_VERSION}\n\nВыбери режим кнопками или напиши BTC/ETH.", reply_markup=main_menu(s))
    set_work_message_id(uid, msg.message_id)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(help_text())

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id(update)
    await update.message.reply_text(get_status_text(uid), reply_markup=main_menu(get_settings(uid)))

async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    started = time.perf_counter()
    uid = user_id(update)
    s = get_settings(uid)
    ai_health = await check_ai_health(uid, context, update.effective_chat.id)
    exchange_health = await check_exchange_api(uid)
    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    await update.message.reply_text(
        f"📡 Ping: {latency_ms} ms\n"
        f"⏱ Время работы: {format_uptime(int(time.time() - START_TIME))}\n"
        f"🧠 Память: {memory_usage_text()}\n"
        f"🤖 Provider: {s.get('ai_provider')}\n"
        f"🧠 Модель ИИ: {get_active_model(s)}\n"
        f"🧪 Отклик/работа модели ИИ: {ai_health}\n"
        f"🏦 API биржи {s.get('exchange', '').upper()}: {exchange_health}\n"
        f"🔄 Auto Scanner: {auto_scanner_label(s.get('auto_scanner_interval'))}\n"
        f"🧠 Structural: {structural_mode_label(s.get('structural_mode'))}\n"
        f"🚀 ExtTP: {'ON' if s.get('extended_tp_enabled') else 'OFF'}\n"
        f"🚨 STOP ALL: {'ON' if s.get('stop_all_enabled') else 'OFF'}\n"
        f"🔁 Position Sync: {'ON' if s.get('position_sync_enabled') else 'OFF'}\n"
        f"🧠 Strict AI: {'ON' if s.get('strict_ai_mode') else 'OFF'}\n"
        f"📦 Version: {BOT_VERSION}",
        reply_markup=main_menu(get_settings(uid))
    )

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

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    uid = user_id(update)
    s = get_settings(uid)
    if s.get("mode") == "chat":
        try:
            ai = await call_ai(uid, txt, context, update.effective_chat.id)
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
        opened, errors = [], []
        for x in confirmed[:int(s.get("max_trades",3))]:
            sym = normalize_symbol(x.get("symbol",""))
            direction = x.get("direction","LONG").upper()
            try:
                if s.get("real_execution_enabled"):
                    pos = await execute_real_trade(uid, sym, direction, x.get("stop_loss"), x.get("take_profit"))
                    opened.append(f"REAL {sym} {direction} @ {pos['entry']} | isolated x{pos['leverage']}")
                else:
                    opened.append(f"PAPER {sym} {direction} — real execution OFF")
            except Exception as e:
                errors.append(f"{sym}: {str(e)[:220]}")
        await send_below_buttons(context, chat_id, ("🛡 Risk Manager PASSED\n\n" + "\n".join(opened) + ("\n\nОшибки:\n" + "\n".join(errors) if errors else "")), uid)
    elif data == "menu:provider":
        await send_below_buttons(context, chat_id, "AI Provider:", uid, reply_markup=provider_menu(s))
    elif data.startswith("provider:"):
        set_setting(uid, "ai_provider", data.split(":")[1])
        await send_below_buttons(context, chat_id, f"✅ Provider: {data.split(':')[1]}", uid)
    elif data == "menu:model":
        await send_below_buttons(context, chat_id, "Model:", uid, reply_markup=model_menu(s))
    elif data.startswith("ollama_model:"):
        model = data.split(":",1)[1]
        set_setting(uid, "ollama_model", model)
        await send_below_buttons(context, chat_id, f"✅ Ollama model selected: {model}\nПроверяю/загружаю модель...", uid)
        try:
            await ensure_ollama_model(model, context, chat_id, uid)
            await send_below_buttons(context, chat_id, f"✅ Модель {model} готова к работе.", uid)
        except Exception as e:
            await send_below_buttons(context, chat_id, f"❌ Ошибка загрузки модели {model}: {str(e)[:1000]}", uid)
    elif data.startswith("openai_model:"):
        model = data.split(":",1)[1]
        set_setting(uid, "openai_model", model)
        await send_below_buttons(context, chat_id, f"✅ OpenAI model: {model}", uid)
    elif data == "menu:reasoning":
        await send_below_buttons(context, chat_id, "Reasoning:", uid, reply_markup=reasoning_menu(s))
    elif data.startswith("reasoning:"):
        set_setting(uid, "reasoning_level", data.split(":")[1])
        await send_below_buttons(context, chat_id, f"✅ Reasoning: {data.split(':')[1]}", uid)
    elif data == "menu:exchange":
        await send_below_buttons(context, chat_id, "Exchange:", uid, reply_markup=exchange_menu(s))
    elif data.startswith("exchange:"):
        set_setting(uid, "exchange", data.split(":")[1])
        await send_below_buttons(context, chat_id, f"✅ Exchange: {data.split(':')[1].upper()}", uid)
    elif data == "menu:tradingmode":
        await send_below_buttons(context, chat_id, "Trading mode:", uid, reply_markup=trading_mode_menu(s))
    elif data.startswith("tradingmode:"):
        set_setting(uid, "trading_mode", data.split(":")[1])
        await send_below_buttons(context, chat_id, f"✅ Trading Mode: {data.split(':')[1]}", uid)
    elif data == "menu:timeframe":
        await send_below_buttons(context, chat_id, "Timeframe:", uid, reply_markup=timeframe_menu(s))
    elif data.startswith("timeframe:"):
        set_setting(uid, "timeframe_mode", data.split(":")[1])
        await send_below_buttons(context, chat_id, f"✅ Timeframe: {timeframe_label(data.split(':')[1])}", uid)
    elif data == "menu:autoscanner":
        await send_below_buttons(context, chat_id, "Auto Scanner Top:", uid, reply_markup=auto_scanner_menu(s))
    elif data.startswith("autoscanner:"):
        set_setting(uid, "auto_scanner_interval", data.split(":")[1])
        set_setting(uid, "auto_scanner_last_run", 0)
        await send_below_buttons(context, chat_id, f"✅ Auto Scanner: {auto_scanner_label(data.split(':')[1])}", uid)
    elif data == "menu:structural":
        await send_below_buttons(context, chat_id, "Structural Layers:", uid, reply_markup=structural_layers_menu(s))
    elif data.startswith("structural:"):
        mode = data.split(":")[1]
        set_setting(uid, "structural_mode", mode)
        await send_below_buttons(context, chat_id, f"✅ Structural: {structural_mode_label(mode)}", uid)
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
        new = not bool(s.get("stop_all_enabled"))
        set_setting(uid, "stop_all_enabled", new)
        if new:
            set_setting(uid, "auto_scanner_interval", "off")
            set_setting(uid, "trading_enabled", False)
            set_setting(uid, "real_execution_enabled", False)
            await send_below_buttons(context, chat_id, "🚨 STOP ALL ACTIVATED\nAuto Scanner OFF\nTrading OFF\nReal Execution OFF", uid)
        else:
            await send_below_buttons(context, chat_id, "✅ STOP ALL DISABLED\n/trading_on и /real_on включаются вручную.", uid)
    elif data == "toggle:positionsync":
        s_now = get_settings(uid)
        new = not bool(s_now.get("position_sync_enabled", False))
        set_setting(uid, "position_sync_enabled", new)
        await send_below_buttons(context, chat_id, f"✅ Position Sync: {'ON' if new else 'OFF'}", uid)
    elif data in ["toggle:breakeven", "toggle:trailing", "toggle:partialtp"]:
        key = {"toggle:breakeven": "breakeven_enabled", "toggle:trailing": "trailing_enabled", "toggle:partialtp": "partial_tp_enabled"}[data]
        new = not bool(s.get(key))
        set_setting(uid, key, new)
        await send_below_buttons(context, chat_id, f"✅ {key}: {'ON' if new else 'OFF'}", uid, reply_markup=trade_mgmt_menu(get_settings(uid)))
    elif data == "mode:signal":
        set_setting(uid, "mode", "signal")
        await send_below_buttons(context, chat_id, "✅ Signal Mode", uid)
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
async def autoscanner_off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE): set_setting(user_id(update), "auto_scanner_interval", "off"); await update.message.reply_text("✅ Auto Scanner OFF")
async def stopall_on_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=user_id(update); set_setting(uid,"stop_all_enabled",True); set_setting(uid,"auto_scanner_interval","off"); set_setting(uid,"trading_enabled",False); set_setting(uid,"real_execution_enabled",False); await update.message.reply_text("🚨 STOP ALL ACTIVATED")
async def stopall_off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE): set_setting(user_id(update),"stop_all_enabled",False); await update.message.reply_text("✅ STOP ALL DISABLED")
async def positionsync_on_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE): set_setting(user_id(update),"position_sync_enabled",True); await update.message.reply_text("✅ Position Sync ON")
async def positionsync_off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE): set_setting(user_id(update),"position_sync_enabled",False); await update.message.reply_text("✅ Position Sync OFF")
async def positionsync_now_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.message.reply_text(await sync_positions_for_user(None, user_id(update)))
async def strictai_on_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE): set_setting(user_id(update),"strict_ai_mode",True); await update.message.reply_text("🧠 STRICT AI MODE: ON")
async def strictai_off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE): set_setting(user_id(update),"strict_ai_mode",False); await update.message.reply_text("🧠 STRICT AI MODE: OFF")

async def setapi_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        await update.message.reply_text("Пример: /setapi mexc API_KEY API_SECRET")
        return
    uid = user_id(update); ex, key, sec = context.args[0].lower(), context.args[1], context.args[2]
    data = load_json(API_KEYS_FILE, {})
    data.setdefault(uid, {})[ex] = {"apiKey": key, "secret": sec}
    save_json(API_KEYS_FILE, data)
    await update.message.reply_text(f"✅ API saved for {ex.upper()}")

async def setopenai_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Пример: /setopenai OPENAI_API_KEY")
        return
    data = load_json(OPENAI_KEYS_FILE, {})
    data[user_id(update)] = context.args[0]
    save_json(OPENAI_KEYS_FILE, data)
    await update.message.reply_text("✅ OpenAI key saved")

def simple_setter(key, value, msg):
    async def f(update: Update, context: ContextTypes.DEFAULT_TYPE):
        set_setting(user_id(update), key, value)
        await update.message.reply_text(msg)
    return f

async def numeric_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str, cast, example: str):
    if not context.args:
        await update.message.reply_text(example); return
    set_setting(user_id(update), key, cast(context.args[0]))
    await update.message.reply_text(f"✅ {key}: {context.args[0]}")

async def post_init(app: Application):
    app.create_task(unload_idle_models())
    app.create_task(auto_scanner_loop(app))
    app.create_task(position_sync_loop(app))

def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN is required")
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("ping", ping_cmd))
    app.add_handler(CommandHandler("signal", signal_cmd))
    app.add_handler(CommandHandler("positions", positions_cmd))
    app.add_handler(CommandHandler("structural", structural_cmd))
    app.add_handler(CommandHandler("autoscanner", autoscanner_cmd))
    app.add_handler(CommandHandler("autoscanner_off", autoscanner_off_cmd))
    app.add_handler(CommandHandler("setapi", setapi_cmd))
    app.add_handler(CommandHandler("setopenai", setopenai_cmd))
    app.add_handler(CommandHandler("strictai_on", strictai_on_cmd))
    app.add_handler(CommandHandler("strictai_off", strictai_off_cmd))
    app.add_handler(CommandHandler("stopall_on", stopall_on_cmd))
    app.add_handler(CommandHandler("stopall_off", stopall_off_cmd))
    app.add_handler(CommandHandler("positionsync_on", positionsync_on_cmd))
    app.add_handler(CommandHandler("positionsync_off", positionsync_off_cmd))
    app.add_handler(CommandHandler("positionsync_now", positionsync_now_cmd))
    app.add_handler(CommandHandler("trading_on", simple_setter("trading_enabled", True, "✅ Trading ON")))
    app.add_handler(CommandHandler("trading_off", simple_setter("trading_enabled", False, "✅ Trading OFF")))
    app.add_handler(CommandHandler("real_on", simple_setter("real_execution_enabled", True, "✅ REAL EXECUTION ON")))
    app.add_handler(CommandHandler("real_off", simple_setter("real_execution_enabled", False, "✅ REAL EXECUTION OFF")))
    app.add_handler(CommandHandler("aiauto_on", simple_setter("ai_auto", True, "✅ AI Auto ON")))
    app.add_handler(CommandHandler("aiauto_off", simple_setter("ai_auto", False, "✅ AI Auto OFF")))
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
    app.add_handler(CommandHandler("toplimit", lambda u,c: numeric_cmd(u,c,"top_limit",str,"Пример: /toplimit 10 или /toplimit all")))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.run_polling()

if __name__ == "__main__":
    main()
