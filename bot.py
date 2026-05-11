
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

BOT_VERSION = os.getenv("BOT_VERSION", "0029")
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


def calculate_trade_management_plan(levels: Dict[str, Any]) -> Dict[str, Any]:
    """
    Trade Management Engine:
    - Move SL to breakeven at +1R
    - Partial TP at TP1
    - Trailing after TP1
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

        if side == "LONG":
            be_trigger = round(entry + risk, 8)
        else:
            be_trigger = round(entry - risk, 8)

        return {
            "be_trigger": be_trigger,
            "partial_close_percent": 50,
            "trailing_enabled": True,
            "runner_target": tp2,
        }
    except Exception:
        return {}


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

    structural_mode = settings.get("structural_mode", "off")
    structural_passed = bool(market.get("structural_passed", False))

    structural = market.get("structural", {}) or {}

    trendline_passed = bool(structural.get("trendline", {}).get("passed"))
    rs_passed = bool(structural.get("relative_strength_btc", {}).get("passed"))
    volume_passed = bool(structural.get("super_volume", {}).get("passed"))

    # RR logic:
    # Normal signal -> 1:2
    # Trendline -> 1:2.5
    # Trendline + RS/BTC -> 1:3
    # Trendline + RS/BTC + Super Volume -> 1:4
    # Structural Only -> 1:4 only if all 3 layers passed

    rr = 2.0
    profile = "standard_1_2"

    if trendline_passed:
        rr = 2.5
        profile = "trendline_1_2.5"

    if trendline_passed and rs_passed:
        rr = 3.0
        profile = "trendline_rs_1_3"

    if trendline_passed and rs_passed and volume_passed:
        rr = 4.0
        profile = "trendline_rs_volume_1_4"

    if structural_mode == "structural_only":
        if trendline_passed and rs_passed and volume_passed and structural_passed:
            rr = 4.0
            profile = "structural_only_full_confirm_1_4"
        else:
            rr = 2.0
            profile = "structural_only_not_full_confirm_1_2"


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
        if tr.get("passed"):
            reasons.append("Trendline breakout / structure confirmed")
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

    if "REJECTED" in up or "REJECT" in up or "WAIT" in up or "NO TRADE" in up:
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
- REJECTED if weak volume, unclear direction, no breakout, poor RS/BTC, MTF conflict, or low confidence.
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



def inline_main_menu(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    """Main inline control panel."""
    universe_label = "Only BTC/ETH" if settings.get("market_universe") == "btc_eth" else "All Market"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📈 Signal", callback_data="mode:signal"),
            InlineKeyboardButton("💬 AI Chat", callback_data="mode:chat"),
        ],
        [
            InlineKeyboardButton("🤖 Provider", callback_data="menu:provider"),
            InlineKeyboardButton("🧠 Model", callback_data="menu:model"),
        ],
        [
            InlineKeyboardButton("🧠 Reasoning", callback_data="menu:reasoning"),
            InlineKeyboardButton("🏦 Exchange", callback_data="menu:exchange"),
        ],
        [
            InlineKeyboardButton("🤖 Trading", callback_data="menu:tradingmode"),
            InlineKeyboardButton("🕘 TF", callback_data="menu:timeframe"),
        ],
        [
            InlineKeyboardButton("🔄 Auto Scanner", callback_data="menu:autoscanner"),
            InlineKeyboardButton("🧠 Structural", callback_data="menu:structural"),
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
            InlineKeyboardButton("🚨 STOP ALL", callback_data="toggle:stopall"),
            InlineKeyboardButton("🔁 Position Sync", callback_data="toggle:positionsync"),
            InlineKeyboardButton("📈 Live TM", callback_data="toggle:livetrademanager"),
        ],
        [
            InlineKeyboardButton(f"🌐 {universe_label}", callback_data="toggle:btceth"),
            InlineKeyboardButton("🌏 Sessions", callback_data="toggle:sessions"),
        ],
        [
            InlineKeyboardButton("❓ Help", callback_data="help"),
        ],
    ])


def main_menu(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    return inline_main_menu(settings)

async def show_inline_menu_message(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str = None):
    uid = user_id(update)
    msg_text = text or f"🤖 Trading Bot v{BOT_VERSION}\n\nInline menu активировано."
    msg = await update.message.reply_text(msg_text, reply_markup=main_menu(get_settings(uid)))
    try:
        set_work_message_id(uid, msg.message_id)
    except Exception:
        pass


def build_main_menu(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    return inline_main_menu(settings)

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
    modes = [("15m", "15 мин"), ("15m_1h", "15 мин/1час"), ("1h_4h", "1 час/4 часа"), ("multi", "мульти 15m+1h+4h+1d")]
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
📈 Live Trade Manager: {'ON' if s.get('live_trade_manager_enabled') else 'OFF'}
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
/livetrademanager_on
/livetrademanager_off
/livetrademanager_status
/livetrademanager_on
/livetrademanager_off

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
    levels = calculate_trade_levels(normalize_symbol(symbol), market, df, s)

    prompt = build_signal_prompt(normalize_symbol(symbol), tf_display, market, s)
    ai = await call_ai(uid, prompt, context, chat_id)
    validate_ai_response_or_raise(s, ai)

    ai_verdict = extract_ai_verdict(ai, market)

    # Strict AI: if rejected, no trade levels are actionable.
    return format_strict_signal(normalize_symbol(symbol), tf_display, s, market, levels, ai_verdict)


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
async def livetrademanager_on_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id(update)
    set_setting(uid, "live_trade_manager_enabled", True)
    await update.message.reply_text("✅ Live Trade Manager: ON")

async def livetrademanager_off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id(update)
    set_setting(uid, "live_trade_manager_enabled", False)
    await update.message.reply_text("✅ Live Trade Manager: OFF")

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


async def live_tm_update_trailing_sl(uid: str, pos: Dict[str, Any], current_price: float) -> Dict[str, Any]:
    """
    Simple trailing:
    after TP1, trail SL by 1R behind price.
    """
    side = str(pos.get("direction") or pos.get("side")).upper()
    entry = safe_float(pos.get("entry"), 0)
    original_sl = safe_float(pos.get("initial_stop_loss") or pos.get("sl") or pos.get("stop_loss"), 0)
    if not entry or not original_sl:
        return {"skipped": "missing_entry_sl"}

    risk = abs(entry - original_sl)
    if risk <= 0:
        return {"skipped": "bad_risk"}

    current_sl = safe_float(pos.get("stop_loss") or pos.get("sl") or original_sl, original_sl)

    if side == "LONG":
        proposed_sl = round(current_price - risk, 8)
        if proposed_sl <= current_sl or proposed_sl <= entry:
            return {"skipped": "no_trailing_improvement"}
    else:
        proposed_sl = round(current_price + risk, 8)
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

            be_trigger = entry + risk if side == "LONG" else entry - risk

            pos.setdefault("tm", {})
            tm = pos["tm"]
            tm["last_price"] = price
            tm["last_check_ts"] = time.time()
            tm["real_execution"] = bool(s.get("real_execution_enabled", False))

            def hit_level(level: float) -> bool:
                if not level:
                    return False
                return (side == "LONG" and price >= level) or (side == "SHORT" and price <= level)

            # 1) BE move at +1R
            if not tm.get("be_done") and hit_level(be_trigger):
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
                    f"🛡 SL moved to BE\n"
                    f"{symbol} {side}\n"
                    f"Entry/BE: {entry}\n"
                    f"Price: {price}\n"
                    f"Result: {str(result)[:300]}"
                )
                changed = True

            # 2) Partial close at TP1
            if tp1 and not tm.get("partial_done") and hit_level(tp1):
                tm["partial_done"] = True
                tm["partial_close_percent"] = 50
                tm["partial_triggered_ts"] = time.time()
                tm.setdefault("events", []).append("TP1 hit. Partial close 50%.")

                result = {"mode": "local_only"}
                if s.get("real_execution_enabled", False):
                    result = await live_tm_partial_close(uid, pos, 50)
                    tm["partial_order_result"] = result
                    if "order" in result:
                        pos["remaining_percent"] = max(0, safe_float(pos.get("remaining_percent", 100), 100) - 50)

                await notify_user(
                    app,
                    uid,
                    f"🎯 TP1 reached\n"
                    f"{symbol} {side}\n"
                    f"✅ 50% closed/planned\n"
                    f"TP1: {tp1}\n"
                    f"Price: {price}\n"
                    f"Result: {str(result)[:300]}"
                )
                changed = True

            # 3) Activate trailing after partial
            if tm.get("partial_done") and not tm.get("trailing_active"):
                tm["trailing_active"] = True
                tm["trailing_started_ts"] = time.time()
                tm.setdefault("events", []).append("Trailing activated after TP1.")

                await notify_user(
                    app,
                    uid,
                    f"🔄 Trailing Stop activated\n"
                    f"{symbol} {side}\n"
                    f"After TP1 partial close"
                )
                changed = True

            # 4) Trailing update
            if tm.get("trailing_active") and not tm.get("runner_done"):
                if s.get("real_execution_enabled", False):
                    result = await live_tm_update_trailing_sl(uid, pos, price)
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
                    f"🏁 TP2 reached / Runner closed\n"
                    f"{symbol} {side}\n"
                    f"TP2: {tp2}\n"
                    f"Price: {price}\n"
                    f"Result: {str(result)[:300]}"
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



def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN is required")
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_cmd))
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

    app.add_handler(CommandHandler("livetrademanager_on", livetrademanager_on_cmd))
    app.add_handler(CommandHandler("livetrademanager_off", livetrademanager_off_cmd))
    app.add_handler(CommandHandler("livetrademanager_status", livetrademanager_status_cmd))
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
