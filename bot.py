import os
import re
import json
import time
import asyncio
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

import ccxt
import psutil
import requests
import pandas as pd
from dotenv import load_dotenv
from openai import OpenAI
from ta.trend import EMAIndicator, MACD, ADXIndicator
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange, BollingerBands
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

load_dotenv()

BOT_VERSION = os.getenv("BOT_VERSION", "0010")
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
if not DATA_DIR.exists():
    DATA_DIR = Path("./data")
DATA_DIR.mkdir(exist_ok=True)

SETTINGS_FILE = DATA_DIR / "settings.json"
API_KEYS_FILE = DATA_DIR / "api_keys.json"
OPENAI_KEYS_FILE = DATA_DIR / "openai_keys.json"

START_TIME = time.time()

DEFAULTS = {
    "ai_provider": os.getenv("DEFAULT_AI_PROVIDER", "ollama"),
    "ollama_model": os.getenv("DEFAULT_OLLAMA_MODEL", "llama3.1:8b"),
    "openai_model": os.getenv("DEFAULT_OPENAI_MODEL", "gpt-4.1-mini"),
    "reasoning_level": os.getenv("DEFAULT_REASONING_LEVEL", "medium"),
    "exchange": os.getenv("DEFAULT_EXCHANGE", "mexc"),
    "bot_mode": "signal",
    "trading_mode": "confirm",
    "trading_enabled": False,
    "ai_auto": False,
    "market_universe": os.getenv("DEFAULT_MARKET_UNIVERSE", "all"),  # all | btc_eth
    "timeframe_mode": os.getenv("DEFAULT_TIMEFRAME_MODE", "15m"),  # 15m | 15m_1h | 1h_4h | multi
    "session_filter": os.getenv("DEFAULT_SESSION_FILTER", "off").lower() == "on",
    "scanner_size": int(os.getenv("DEFAULT_SCANNER_SIZE", "100")),
    "min_score": float(os.getenv("DEFAULT_MIN_SCORE", "80")),
    "top_limit": os.getenv("DEFAULT_TOP_LIMIT", "10"),
    "risk_percent": float(os.getenv("DEFAULT_RISK_PERCENT", "1")),
    "max_risk_percent": float(os.getenv("DEFAULT_MAX_RISK_PERCENT", "3")),
    "max_trades": int(os.getenv("DEFAULT_MAX_TRADES", "3")),
    "leverage": int(os.getenv("DEFAULT_LEVERAGE", "5")),
    "strict_ai_mode": os.getenv("DEFAULT_STRICT_AI_MODE", "on").lower() == "on",
}

MODEL_IDLE_UNLOAD_SECONDS = int(os.getenv("MODEL_IDLE_UNLOAD_SECONDS", "1200"))
LAST_MODEL_USE: Dict[str, float] = {}
LAST_SCAN_RESULTS: Dict[int, List[Dict[str, Any]]] = {}
LAST_AI_CONFIRMED: Dict[int, List[Dict[str, Any]]] = {}

OLLAMA_MODELS = ["llama3.1:8b", "deepseek-r1:8b"]
OPENAI_MODELS = ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano", "gpt-4.1", "gpt-4.1-mini"]
REASONING_LEVELS = ["low", "medium", "high", "xhigh"]
EXCHANGES = ["mexc", "bingx", "binance"]


def load_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return default


def save_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def user_id(update: Update) -> str:
    if update.effective_user:
        return str(update.effective_user.id)
    return "default"


def get_settings(uid: str) -> Dict[str, Any]:
    all_settings = load_json(SETTINGS_FILE, {})
    if uid not in all_settings:
        all_settings[uid] = DEFAULTS.copy()
        save_json(SETTINGS_FILE, all_settings)
    merged = DEFAULTS.copy()
    merged.update(all_settings.get(uid, {}))
    return merged


def set_setting(uid: str, key: str, value):
    all_settings = load_json(SETTINGS_FILE, {})
    s = DEFAULTS.copy()
    s.update(all_settings.get(uid, {}))
    s[key] = value
    all_settings[uid] = s
    save_json(SETTINGS_FILE, all_settings)


def get_active_model(settings: Dict[str, Any]) -> str:
    return settings["ollama_model"] if settings["ai_provider"] == "ollama" else settings["openai_model"]


def main_menu(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    universe_label = "🟠 Only BTC/ETH" if settings["market_universe"] == "btc_eth" else "🌐 All Futures Market"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📈 Signal Mode", callback_data="mode:signal"),
            InlineKeyboardButton("💬 AI Chat Mode", callback_data="mode:chat"),
        ],
        [
            InlineKeyboardButton("🤖 AI Provider", callback_data="menu:provider"),
            InlineKeyboardButton("🧠 Model", callback_data="menu:model"),
        ],
        [
            InlineKeyboardButton("🧠 Reasoning", callback_data="menu:reasoning"),
            InlineKeyboardButton("🏦 Exchange", callback_data="menu:exchange"),
        ],
        [
            InlineKeyboardButton("🤖 Trading Mode", callback_data="menu:tradingmode"),
            InlineKeyboardButton("🕒 Таймфрейм", callback_data="menu:timeframe"),
        ],
        [
            InlineKeyboardButton("🌏 Азия/Америка", callback_data="toggle:sessions"),
            InlineKeyboardButton(universe_label, callback_data="toggle:btceth"),
        ],
        [
            InlineKeyboardButton("📋 Статус", callback_data="status"),
        ],
        [
            InlineKeyboardButton("🔥 Top-50 Signal", callback_data="scan:50"),
            InlineKeyboardButton("🔥 Top-100 Signal", callback_data="scan:100"),
        ],
        [
            InlineKeyboardButton("🔥 Top-200 Signal", callback_data="scan:200"),
            InlineKeyboardButton("📡 Ping", callback_data="ping"),
        ],
        [
            InlineKeyboardButton("❓ Help", callback_data="help"),
        ],
    ])


def provider_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🦙 Ollama Local (бесплатно)", callback_data="provider:ollama")],
        [InlineKeyboardButton("🌐 OpenAI ChatGPT (платно)", callback_data="provider:openai")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back:main")],
    ])


def model_menu(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    rows = []
    if settings["ai_provider"] == "ollama":
        for m in OLLAMA_MODELS:
            title = ("✅ " if settings["ollama_model"] == m else "") + m
            rows.append([InlineKeyboardButton(title, callback_data=f"model:ollama:{m}")])
    else:
        for m in OPENAI_MODELS:
            title = ("✅ " if settings["openai_model"] == m else "") + m
            rows.append([InlineKeyboardButton(title, callback_data=f"model:openai:{m}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="back:main")])
    return InlineKeyboardMarkup(rows)


def reasoning_menu(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    rows = []
    for lvl in REASONING_LEVELS:
        rows.append([InlineKeyboardButton(("✅ " if settings["reasoning_level"] == lvl else "") + lvl.upper(), callback_data=f"reasoning:{lvl}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="back:main")])
    return InlineKeyboardMarkup(rows)


def exchange_menu(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(("✅ " if settings["exchange"] == ex else "") + ex.upper(), callback_data=f"exchange:{ex}")] for ex in EXCHANGES] + [[InlineKeyboardButton("⬅️ Назад", callback_data="back:main")]])


def trading_mode_menu(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    modes = [("manual", "🟢 Manual"), ("confirm", "🟡 Confirm"), ("auto", "🔴 Auto")]
    return InlineKeyboardMarkup([[InlineKeyboardButton(("✅ " if settings["trading_mode"] == m else "") + label, callback_data=f"tradingmode:{m}")] for m, label in modes] + [[InlineKeyboardButton("⬅️ Назад", callback_data="back:main")]])


def timeframe_menu(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    modes = [
        ("15m", "15 мин"),
        ("15m_1h", "15 мин / 1 час"),
        ("1h_4h", "1 час / 4 часа"),
        ("multi", "Мульти"),
    ]
    rows = []
    for key, label in modes:
        rows.append([InlineKeyboardButton(("✅ " if settings.get("timeframe_mode") == key else "") + label, callback_data=f"timeframe:{key}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="back:main")])
    return InlineKeyboardMarkup(rows)


def timeframe_label(mode: str) -> str:
    return {
        "15m": "15 мин",
        "15m_1h": "15 мин / 1 час",
        "1h_4h": "1 час / 4 часа",
        "multi": "Мульти",
    }.get(mode, "15 мин")


def timeframe_pair(settings: Dict[str, Any], override: Optional[str] = None) -> Tuple[str, Optional[str]]:
    if override:
        return override, None
    mode = settings.get("timeframe_mode", "15m")
    if mode == "15m":
        return "15m", None
    if mode == "15m_1h":
        return "15m", "1h"
    if mode == "1h_4h":
        return "1h", "4h"
    if mode == "multi":
        return "15m", "1h"
    return "15m", None


def timeframe_list_for_analysis(settings: Dict[str, Any], override: Optional[str] = None) -> List[str]:
    if override:
        return [override]
    mode = settings.get("timeframe_mode", "15m")
    if mode == "15m":
        return ["15m"]
    if mode == "15m_1h":
        return ["15m", "1h"]
    if mode == "1h_4h":
        return ["1h", "4h"]
    if mode == "multi":
        return ["5m", "15m", "1h", "4h"]
    return ["15m"]




def moscow_now() -> datetime:
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=3)))


def session_context(settings: Dict[str, Any]) -> Dict[str, Any]:
    enabled = bool(settings.get("session_filter", False))
    now = moscow_now()
    minutes = now.hour * 60 + now.minute
    asia_open = 3 * 60
    america_open = 16 * 60 + 30

    if not enabled:
        return {
            "enabled": False,
            "label": "OFF",
            "active": "not_used",
            "note": "Asia/America session filter is disabled."
        }

    if asia_open <= minutes < america_open:
        active = "asia"
        label = "Азия активна"
        note = "Сейчас учитывается азиатская торговая сессия. Часто выше риск резких движений в первые часы после 03:00 МСК."
    elif minutes >= america_open:
        active = "america"
        label = "Америка активна"
        note = "Сейчас учитывается американская торговая сессия. Часто выше объем и волатильность после 16:30 МСК."
    else:
        active = "pre_asia"
        label = "До открытия Азии"
        note = "До 03:00 МСК основные Asia/America session triggers еще не активны."

    return {
        "enabled": True,
        "active": active,
        "label": label,
        "moscow_time": now.strftime("%H:%M"),
        "note": note,
        "asia_open": "03:00 МСК",
        "america_open": "16:30 МСК",
    }


def session_line(settings: Dict[str, Any]) -> str:
    ctx = session_context(settings)
    if not ctx["enabled"]:
        return "OFF"
    return f"{ctx['label']} ({ctx['moscow_time']} МСК)"

def score_market_multi(exchange_name: str, symbol: str, settings: Dict[str, Any], override: Optional[str] = None) -> Dict[str, Any]:
    frames = timeframe_list_for_analysis(settings, override)
    scores = []
    details = []
    for tf in frames:
        df = add_indicators(fetch_ohlcv_for_symbol(exchange_name, symbol, tf, 180))
        sc = score_market(df)
        sc["timeframe"] = tf
        scores.append(sc)
        details.append(f"{tf}: {sc['direction']} {sc['score']}% RSI {sc['rsi']:.1f} ADX {sc['adx']:.1f}")

    primary = scores[0]
    if len(scores) == 1:
        primary["mtf_details"] = details
        primary["mtf_confirmed"] = True
        return primary

    # Confirmation logic:
    # primary timeframe decides entry, higher timeframe confirms direction.
    confirmed = True
    for sc in scores[1:]:
        if primary["direction"] == "WAIT" or sc["direction"] == "WAIT" or sc["direction"] != primary["direction"]:
            confirmed = False

    avg_strength = sum(x["score"] for x in scores) / len(scores)
    primary["score"] = round(avg_strength if confirmed else min(primary["score"], avg_strength), 1)
    primary["mtf_details"] = details
    primary["mtf_confirmed"] = confirmed
    if not confirmed:
        primary["reasons"] = primary.get("reasons", []) + ["MTF confirmation failed / старший ТФ не подтвердил"]
        if settings.get("timeframe_mode") in ["15m_1h", "1h_4h", "multi"]:
            primary["direction"] = "WAIT"
    return primary


def help_text() -> str:
    return f"""
🤖 AI Futures Trading Bot — Help

📊 Анализ:
`/signal BTCUSDT`
`/signal ETH 5m`

🔥 Top Scanner:
`/top50`
`/top100`
`/top200`
`/minscore 80`
`/toplimit 10`
`/toplimit all`

🕒 Таймфрейм:
`/timeframe` — кнопки выбора таймфрейма
Режимы:
- 15 мин
- 15 мин / 1 час
- 1 час / 4 часа
- мульти

🌏 Азия/Америка:
`/sessions` — показать статус
`/sessions_on` — учитывать сессии
`/sessions_off` — не учитывать сессии

Время по МСК:
- Азия: 03:00
- Америка: 16:30

🟠 Only BTC/ETH:
`/onlybtceth_on` — анализ и торговля только BTC/ETH
`/onlybtceth_off` — вернуть весь futures market
`/market` — показать/переключить режим рынка

📋 Статус:
`/status` — показать все текущие настройки

🤖 AI:
`/provider` — Ollama Local (бесплатно) / OpenAI ChatGPT (платно)
`/model` — выбор модели
`/reasoning` — Low / Medium / High / XHigh

🏦 Биржа:
`/exchange` — MEXC / BingX / Binance

🤖 Торговля:
`/risk 1`
`/leverage 5`
`/maxtrades 3`
`/maxrisk 3`
`/trading_on`
`/trading_off`
`/aiauto_on`
`/aiauto_off`

🔐 API:
`/setapi mexc API_KEY API_SECRET`
`/testapi`
`/delapi mexc`

🌐 OpenAI:
`/setopenai sk-...`
`/testopenai`
`/delopenai`
`/openai_on`
`/openai_off`

📡 Система:
`/ping`

Version: {BOT_VERSION}
"""


def get_status_text(uid: str) -> str:
    s = get_settings(uid)
    api_keys = load_json(API_KEYS_FILE, {})
    openai_keys = load_json(OPENAI_KEYS_FILE, {})
    active_model = get_active_model(s)
    provider_label = "Ollama Local (бесплатно)" if s["ai_provider"] == "ollama" else "OpenAI ChatGPT (платно)"
    exchange_api = "✅ set" if uid in api_keys and s["exchange"] in api_keys.get(uid, {}) else "❌ not set"
    openai_api = "✅ set" if uid in openai_keys else "❌ not set"
    universe = "Only BTC/ETH" if s["market_universe"] == "btc_eth" else "All Futures Market"
    top_status = "OFF, because Only BTC/ETH is enabled" if s["market_universe"] == "btc_eth" else f"Top-{s['scanner_size']}"
    return f"""
📋 СТАТУС БОТА

📦 Version: {BOT_VERSION}

🤖 AI Provider: {provider_label}
🧠 Active Model: {active_model}
🧩 Reasoning Level: {s['reasoning_level'].upper()}

🏦 Exchange: {s['exchange'].upper()}
🌐 Market Universe: {universe}
🕒 Timeframe: {timeframe_label(s.get('timeframe_mode', '15m'))}
🌏 Asia/America: {session_line(s)}

📈 Bot Mode: {s['bot_mode'].upper()}
🤖 Trading Mode: {s['trading_mode'].upper()}
🚀 Trading Enabled: {'ON' if s['trading_enabled'] else 'OFF'}
🧠 AI Auto Confirm: {'ON' if s['ai_auto'] else 'OFF'}

🔥 Scanner: {top_status}
🎯 Min Score: {s['min_score']}%
📋 Top Limit: {s['top_limit']}

⚖️ Risk per trade: {s['risk_percent']}%
🛡 Max total risk: {s['max_risk_percent']}%
📌 Max trades: {s['max_trades']}
📈 Leverage: x{s['leverage']}

⏳ Model idle unload: {MODEL_IDLE_UNLOAD_SECONDS}s

🔐 Exchange API: {exchange_api}
🌐 OpenAI API: {openai_api}
"""


def uptime() -> str:
    sec = int(time.time() - START_TIME)
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h}h {m}m {s}s"


def ping_text(uid: str, latency_ms: Optional[int] = None) -> str:
    s = get_settings(uid)
    process = psutil.Process(os.getpid())
    mem_mb = process.memory_info().rss / 1024 / 1024
    vm = psutil.virtual_memory()
    provider_label = "Ollama Local (бесплатно)" if s["ai_provider"] == "ollama" else "OpenAI ChatGPT (платно)"
    return f"""
📡 Ping: {latency_ms if latency_ms is not None else '-'} ms
⏱ Uptime: {uptime()}
🧠 Process RAM: {mem_mb:.1f} MB
💾 Server RAM: {vm.used/1024/1024/1024:.2f} / {vm.total/1024/1024/1024:.2f} GB
🤖 Provider: {provider_label}
🧠 Model: {get_active_model(s)}
🧩 Reasoning: {s['reasoning_level'].upper()}
🏦 Exchange: {s['exchange'].upper()}
🌐 Market: {"Only BTC/ETH" if s["market_universe"] == "btc_eth" else "All Futures"}
🕒 Timeframe: {timeframe_label(s.get("timeframe_mode", "15m"))}
🌏 Asia/America: {session_line(s)}
📦 Version: {BOT_VERSION}
"""


def normalize_symbol(text: str) -> str:
    sym = text.strip().upper().replace("/", "").replace("-", "")
    if not sym.endswith("USDT"):
        sym += "USDT"
    return sym


def allowed_by_market_universe(symbol: str, settings: Dict[str, Any]) -> bool:
    if settings["market_universe"] != "btc_eth":
        return True
    return normalize_symbol(symbol) in ["BTCUSDT", "ETHUSDT"]


def create_exchange(name: str, uid: Optional[str] = None):
    cls = getattr(ccxt, name)
    config = {"enableRateLimit": True, "options": {"defaultType": "swap"}}
    if name == "binance":
        config["options"] = {"defaultType": "future"}
    keys = load_json(API_KEYS_FILE, {})
    if uid and uid in keys and name in keys[uid]:
        config["apiKey"] = keys[uid][name]["key"]
        config["secret"] = keys[uid][name]["secret"]
    return cls(config)


def fetch_ohlcv_for_symbol(exchange_name: str, symbol: str, timeframe: str = "15m", limit: int = 150) -> pd.DataFrame:
    ex = create_exchange(exchange_name)
    markets = ex.load_markets()
    normalized = normalize_symbol(symbol)
    possible = [
        normalized.replace("USDT", "/USDT:USDT"),
        normalized.replace("USDT", "/USDT"),
    ]
    market_symbol = next((p for p in possible if p in markets), None)
    if not market_symbol:
        raise ValueError(f"Пара {normalized} не найдена на {exchange_name.upper()} futures")
    ohlcv = ex.fetch_ohlcv(market_symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["timestamp","open","high","low","close","volume"])
    for c in ["open","high","low","close","volume"]:
        df[c] = df[c].astype(float)
    df.attrs["market_symbol"] = market_symbol
    return df


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df["ema20"] = EMAIndicator(df["close"], window=20).ema_indicator()
    df["ema50"] = EMAIndicator(df["close"], window=50).ema_indicator()
    df["ema200"] = EMAIndicator(df["close"], window=200 if len(df) >= 200 else 100).ema_indicator()
    df["rsi"] = RSIIndicator(df["close"], window=14).rsi()
    macd = MACD(df["close"])
    df["macd"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    adx = ADXIndicator(df["high"], df["low"], df["close"], window=14)
    df["adx"] = adx.adx()
    atr = AverageTrueRange(df["high"], df["low"], df["close"], window=14)
    df["atr"] = atr.average_true_range()
    bb = BollingerBands(df["close"], window=20)
    df["bb_high"] = bb.bollinger_hband()
    df["bb_low"] = bb.bollinger_lband()
    df["vol_ma"] = df["volume"].rolling(20).mean()
    return df.dropna()


def score_market(df: pd.DataFrame) -> Dict[str, Any]:
    last = df.iloc[-1]
    prev = df.iloc[-2]
    score = 50
    direction = "WAIT"
    reasons = []

    if last["ema20"] > last["ema50"]:
        score += 12; reasons.append("EMA20 > EMA50")
    else:
        score -= 12; reasons.append("EMA20 < EMA50")

    if last["close"] > last["ema200"]:
        score += 8; reasons.append("price > EMA200")
    else:
        score -= 8; reasons.append("price < EMA200")

    if last["rsi"] > 58:
        score += 10; reasons.append("RSI bullish")
    elif last["rsi"] < 42:
        score -= 10; reasons.append("RSI bearish")

    if last["macd"] > last["macd_signal"]:
        score += 10; reasons.append("MACD bullish")
    else:
        score -= 10; reasons.append("MACD bearish")

    if last["adx"] > 22:
        score += 8; reasons.append("ADX trend confirmed")

    if last["volume"] > last["vol_ma"]:
        score += 5; reasons.append("volume above average")

    if score >= 70:
        direction = "LONG"
        strength = min(score, 99)
    elif score <= 30:
        direction = "SHORT"
        strength = min(100 - score, 99)
    else:
        strength = max(score, 100 - score)

    return {
        "direction": direction,
        "score": round(float(strength), 1),
        "raw_score": round(float(score), 1),
        "price": float(last["close"]),
        "atr": float(last["atr"]),
        "rsi": float(last["rsi"]),
        "adx": float(last["adx"]),
        "reasons": reasons,
    }


async def ensure_ollama_model(model: str):
    try:
        res = requests.get("http://localhost:11434/api/tags", timeout=10).json()
        names = [m.get("name") for m in res.get("models", [])]
        if model not in names:
            subprocess.run(["ollama", "pull", model], check=False, timeout=1800)
    except Exception:
        subprocess.run(["ollama", "pull", model], check=False, timeout=1800)


async def unload_idle_models():
    while True:
        await asyncio.sleep(60)
        now = time.time()
        for model, last in list(LAST_MODEL_USE.items()):
            if now - last > MODEL_IDLE_UNLOAD_SECONDS:
                try:
                    requests.post("http://localhost:11434/api/generate", json={"model": model, "prompt": "", "keep_alive": 0, "stream": False}, timeout=20)
                    LAST_MODEL_USE.pop(model, None)
                except Exception:
                    pass


def reasoning_instruction(level: str) -> str:
    mapping = {
        "low": "Дай краткий быстрый анализ. Не углубляйся.",
        "medium": "Дай сбалансированный анализ: тренд, momentum, объем, риск.",
        "high": "Проведи глубокий multi-factor анализ: тренд, momentum, volume, ADX, volatility, ложные пробои, риск.",
        "xhigh": "Проведи максимально строгий профессиональный анализ. Отфильтруй слабые сетапы, оцени конфликтующие факторы, риск ложного пробоя, условия отмены, приоритет безопасности.",
    }
    return mapping.get(level, mapping["medium"])


async def call_ai(uid: str, prompt: str) -> str:
    s = get_settings(uid)
    level = s["reasoning_level"]
    system = f"Ты AI crypto futures analyst. {reasoning_instruction(level)} Не обещай прибыль. Отвечай структурировано."
    if s["ai_provider"] == "openai":
        keys = load_json(OPENAI_KEYS_FILE, {})
        api_key = keys.get(uid) or os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OpenAI API key не задан. Используй /setopenai sk-...")
        client = OpenAI(api_key=api_key)
        model = s["openai_model"]
        response = client.responses.create(
            model=model,
            input=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        )
        return response.output_text
    else:
        model = s["ollama_model"]
        await ensure_ollama_model(model)
        LAST_MODEL_USE[model] = time.time()
        full_prompt = system + "\n\n" + prompt
        r = requests.post("http://localhost:11434/api/generate", json={"model": model, "prompt": full_prompt, "stream": False}, timeout=180)
        r.raise_for_status()
        return r.json().get("response", "")



def validate_ai_response_or_raise(settings: Dict[str, Any], ai_text: str):
    """STRICT AI MODE: empty/error-like AI output blocks signal and execution."""
    if not settings.get("strict_ai_mode", True):
        return
    if not ai_text or not str(ai_text).strip():
        raise ValueError("STRICT AI MODE: AI response is empty. Signal/execution blocked.")
    lowered = str(ai_text).lower()
    hard_error_markers = [
        "openai api key не задан",
        "connection refused",
        "model not found",
        "traceback",
        "strict ai mode",
    ]
    if any(x in lowered for x in hard_error_markers):
        raise ValueError("STRICT AI MODE: AI error detected. Signal/execution blocked.")

def build_signal_prompt(symbol: str, timeframe: str, market: Dict[str, Any], settings: Dict[str, Any]) -> str:
    return f"""
Монета: {symbol}
Биржа: {settings['exchange']}
Таймфрейм: {timeframe}
Цена: {market['price']}
Системный сигнал: {market['direction']}
Сила: {market['score']}%
RSI: {market['rsi']:.2f}
ADX: {market['adx']:.2f}
ATR: {market['atr']:.6f}
Причины: {", ".join(market['reasons'])}

Asia/America session:
{session_context(settings)}

Если Asia/America включен, учитывай активную сессию, время открытия, возможную волатильность и риск ложных пробоев.

Ответь форматом:
Сигнал: LONG/SHORT/WAIT
Уверенность: %
Краткое рассуждение:
Условия входа:
Стоп-лосс:
Тейк-профит:
Риск:
"""


async def signal_for_symbol(uid: str, symbol: str, timeframe: Optional[str] = None) -> str:
    s = get_settings(uid)
    if not allowed_by_market_universe(symbol, s):
        return "🟠 Включен режим Only BTC/ETH. Доступны только BTCUSDT и ETHUSDT."
    market = score_market_multi(s["exchange"], symbol, s, timeframe)
    tf_display = timeframe if timeframe else timeframe_label(s.get("timeframe_mode", "15m"))
    prompt = build_signal_prompt(normalize_symbol(symbol), tf_display, market, s)
    prompt += "\n\nMulti-timeframe details:\n" + "\n".join(market.get("mtf_details", []))
    prompt += f"\nMTF confirmed: {market.get('mtf_confirmed', True)}"
    ai = await call_ai(uid, prompt)
    return f"📊 {normalize_symbol(symbol)} | {s['exchange'].upper()} | {tf_display}\n🤖 {s['ai_provider']} / {get_active_model(s)}\n🕒 MTF: {'✅ confirmed' if market.get('mtf_confirmed', True) else '❌ not confirmed'}\n\n{ai}"


def get_top_symbols(exchange_name: str, n: int) -> List[str]:
    ex = create_exchange(exchange_name)
    markets = ex.load_markets()
    syms = []
    for sym, m in markets.items():
        if not m.get("active", True):
            continue
        text = sym.upper()
        if ("USDT" in text) and (m.get("swap") or m.get("future") or ":USDT" in text):
            base = m.get("base") or text.split("/")[0]
            if base not in ["USDC", "BUSD", "FDUSD", "TUSD"]:
                syms.append(base + "USDT")
    return list(dict.fromkeys(syms))[:n]


async def run_top_scan(uid: str, n: int) -> str:
    s = get_settings(uid)
    set_setting(uid, "scanner_size", n)
    if s["market_universe"] == "btc_eth":
        symbols = ["BTCUSDT", "ETHUSDT"]
        header = f"🟠 Only BTC/ETH включен — Top Scanner отключен, сканирую только BTC/ETH. TF: {timeframe_label(s.get('timeframe_mode', '15m'))} | Session: {session_line(s)}"
    else:
        symbols = get_top_symbols(s["exchange"], n)
        header = f"🔥 Top-{n} Scanner | {s['exchange'].upper()} | TF: {timeframe_label(s.get('timeframe_mode', '15m'))} | Session: {session_line(s)}"

    results = []
    for sym in symbols:
        try:
            m = score_market_multi(s["exchange"], sym, s)
            if m["direction"] != "WAIT" and m["score"] >= float(s["min_score"]):
                results.append({"symbol": sym, **m})
        except Exception:
            continue

    results.sort(key=lambda x: x["score"], reverse=True)

    top_limit = s["top_limit"]
    if str(top_limit).lower() != "all":
        results = results[:int(top_limit)]

    LAST_SCAN_RESULTS[int(uid)] = results

    longs = [r for r in results if r["direction"] == "LONG"]
    shorts = [r for r in results if r["direction"] == "SHORT"]

    def fmt(items):
        if not items:
            return "Нет сильных сигналов"
        return "\n".join([f"{i+1}. {x['symbol']} — {x['direction']} {x['score']}%" for i, x in enumerate(items)])

    text = f"""{header}

🎯 MinScore: {s['min_score']}%
📋 TopLimit: {s['top_limit']}

🔥 TOP LONG
{fmt(longs)}

🔻 TOP SHORT
{fmt(shorts)}
"""
    return text


async def ai_confirm(uid: str) -> str:
    candidates = LAST_SCAN_RESULTS.get(int(uid), [])
    if not candidates:
        return "Нет кандидатов. Сначала запусти /top100 или кнопку Top Scanner."

    s = get_settings(uid)
    limited = candidates[:max(1, int(s["max_trades"]) * 3)]
    prompt = f"""
Из списка кандидатов выбери лучшие сделки для futures.
Максимум сделок: {s['max_trades']}
Максимальный общий риск: {s['max_risk_percent']}%
Риск на сделку: {s['risk_percent']}%
Плечо: x{s['leverage']}
Биржа: {s['exchange']}
Market universe: {s['market_universe']}
Asia/America session: {session_context(s)}

Если session filter включен, учитывай активную торговую сессию и риск волатильности около открытия.

Кандидаты:
{json.dumps(limited, ensure_ascii=False, indent=2)}

Верни JSON массив:
[
  {{"symbol":"BTCUSDT","direction":"LONG","confidence":85,"reason":"..."}}
]
Выбирай только если сетап качественный.
"""
    raw = await call_ai(uid, prompt)
    validate_ai_response_or_raise(s, raw)
    try:
        match = re.search(r"\[.*\]", raw, re.S)
        confirmed = json.loads(match.group(0)) if match else []
    except Exception:
        confirmed = []
    if s["market_universe"] == "btc_eth":
        confirmed = [x for x in confirmed if normalize_symbol(x.get("symbol","")) in ["BTCUSDT", "ETHUSDT"]]
    confirmed = confirmed[:int(s["max_trades"])]
    LAST_AI_CONFIRMED[int(uid)] = confirmed
    if not confirmed:
        LAST_AI_CONFIRMED[int(uid)] = []
        return "🧠 AI не подтвердил сделки из списка.\n\nОтвет AI:\n" + raw[:2500]

    lines = ["🧠 AI подтвердил сделки:"]
    for i, x in enumerate(confirmed, 1):
        lines.append(f"{i}. {normalize_symbol(x.get('symbol',''))} — {x.get('direction')} | {x.get('confidence','-')}%\n   {x.get('reason','')}")
    return "\n".join(lines)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id(update)
    s = get_settings(uid)
    await update.message.reply_text(
        f"🤖 Trading Bot v{BOT_VERSION}\n\nВыбери режим кнопками или напиши BTC/ETH.",
        reply_markup=main_menu(s)
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(help_text(), parse_mode="Markdown")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(get_status_text(user_id(update)))


async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id(update)
    t = time.time()
    msg = await update.message.reply_text("ping...")
    latency = int((time.time() - t) * 1000)
    await msg.edit_text(ping_text(uid, latency))


async def signal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id(update)
    if not context.args:
        await update.message.reply_text("Напиши: /signal BTCUSDT или /signal ETH 5m")
        return
    symbol = context.args[0]
    timeframe = context.args[1] if len(context.args) > 1 else None
    await update.message.reply_text("⏳ Анализирую...")
    try:
        text = await signal_for_symbol(uid, symbol, timeframe)
    except Exception as e:
        text = f"Ошибка: {e}"
    await update.message.reply_text(text[:3900])


async def top_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, n: int):
    uid = user_id(update)
    msg = await update.message.reply_text(f"⏳ Сканирую {n} монет...")
    text = await run_top_scan(uid, n)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🧠 Отправить в ИИ на подтверждение", callback_data="aiconfirm")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
    ])
    await msg.edit_text(text[:3900], reply_markup=kb)


async def minscore_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id(update)
    if not context.args:
        await update.message.reply_text("Пример: /minscore 80")
        return
    set_setting(uid, "min_score", float(context.args[0]))
    await update.message.reply_text(f"✅ MinScore установлен: {context.args[0]}%")


async def toplimit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id(update)
    if not context.args:
        await update.message.reply_text("Пример: /toplimit 10 или /toplimit all")
        return
    val = context.args[0].lower()
    if val != "all":
        val = str(int(val))
    set_setting(uid, "top_limit", val)
    await update.message.reply_text(f"✅ TopLimit установлен: {val}")


async def simple_set_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str, parser, label: str):
    uid = user_id(update)
    if not context.args:
        await update.message.reply_text(f"Укажи значение. Например: /{key} 3")
        return
    val = parser(context.args[0])
    set_setting(uid, key, val)
    await update.message.reply_text(f"✅ {label}: {val}")


async def sessions_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id(update)
    s = get_settings(uid)
    ctx = session_context(s)
    if not ctx["enabled"]:
        await update.message.reply_text(
            "🌏 Азия/Америка: OFF\n\n"
            "Время по МСК:\n"
            "Азия: 03:00\n"
            "Америка: 16:30\n\n"
            "Включить: /sessions_on"
        )
    else:
        await update.message.reply_text(
            f"🌏 Азия/Америка: ON\n"
            f"Статус: {ctx['label']}\n"
            f"Сейчас МСК: {ctx['moscow_time']}\n\n"
            f"Азия: {ctx['asia_open']}\n"
            f"Америка: {ctx['america_open']}\n\n"
            f"{ctx['note']}\n\n"
            f"Выключить: /sessions_off"
        )


async def timeframe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id(update)
    s = get_settings(uid)
    await update.message.reply_text("Выбери таймфрейм:", reply_markup=timeframe_menu(s))


async def market_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id(update)
    s = get_settings(uid)
    await update.message.reply_text(
        f"Текущий рынок: {'Only BTC/ETH' if s['market_universe']=='btc_eth' else 'All Futures Market'}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🟠 Only BTC/ETH", callback_data="market:btc_eth")],
            [InlineKeyboardButton("🌐 All Futures Market", callback_data="market:all")]
        ])
    )


async def setapi_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id(update)
    if len(context.args) < 3:
        await update.message.reply_text("Пример: /setapi mexc API_KEY API_SECRET")
        return
    ex, key, secret = context.args[0].lower(), context.args[1], context.args[2]
    data = load_json(API_KEYS_FILE, {})
    data.setdefault(uid, {})[ex] = {"key": key, "secret": secret}
    save_json(API_KEYS_FILE, data)
    await update.message.reply_text(f"✅ API для {ex.upper()} сохранен. Withdraw permission не включай.")


async def delapi_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id(update)
    ex = context.args[0].lower() if context.args else get_settings(uid)["exchange"]
    data = load_json(API_KEYS_FILE, {})
    if uid in data and ex in data[uid]:
        del data[uid][ex]
        save_json(API_KEYS_FILE, data)
    await update.message.reply_text(f"✅ API для {ex.upper()} удален.")


async def testapi_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id(update)
    s = get_settings(uid)
    try:
        ex = create_exchange(s["exchange"], uid)
        bal = ex.fetch_balance()
        await update.message.reply_text(f"✅ API OK: {s['exchange'].upper()}\nBalance keys: {list(bal.keys())[:8]}")
    except Exception as e:
        await update.message.reply_text(f"❌ API error: {e}")


async def setopenai_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id(update)
    if not context.args:
        await update.message.reply_text("Пример: /setopenai sk-...")
        return
    data = load_json(OPENAI_KEYS_FILE, {})
    data[uid] = context.args[0]
    save_json(OPENAI_KEYS_FILE, data)
    await update.message.reply_text("✅ OpenAI API key сохранен.")


async def delopenai_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id(update)
    data = load_json(OPENAI_KEYS_FILE, {})
    data.pop(uid, None)
    save_json(OPENAI_KEYS_FILE, data)
    await update.message.reply_text("✅ OpenAI API key удален.")


async def testopenai_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id(update)
    try:
        res = await call_ai(uid, "Ответь одним словом: OK")
        await update.message.reply_text(f"✅ OpenAI/AI test: {res[:200]}")
    except Exception as e:
        await update.message.reply_text(f"❌ AI test error: {e}")


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = str(q.from_user.id)
    s = get_settings(uid)
    data = q.data

    if data == "back:main":
        await q.edit_message_text("Главное меню", reply_markup=main_menu(get_settings(uid)))
    elif data == "menu:provider":
        await q.edit_message_text("Выбери AI Provider:", reply_markup=provider_menu())
    elif data == "menu:model":
        await q.edit_message_text("Выбери модель:", reply_markup=model_menu(s))
    elif data == "menu:reasoning":
        await q.edit_message_text("Выбери Reasoning Level:", reply_markup=reasoning_menu(s))
    elif data == "menu:exchange":
        await q.edit_message_text("Выбери биржу:", reply_markup=exchange_menu(s))
    elif data == "menu:timeframe":
        await q.edit_message_text("Выбери таймфрейм:", reply_markup=timeframe_menu(s))
    elif data == "menu:tradingmode":
        await q.edit_message_text("Выбери торговый режим:", reply_markup=trading_mode_menu(s))
    elif data.startswith("provider:"):
        set_setting(uid, "ai_provider", data.split(":")[1])
        await q.edit_message_text("✅ AI Provider изменен", reply_markup=main_menu(get_settings(uid)))
    elif data.startswith("model:"):
        _, provider, model = data.split(":", 2)
        if provider == "ollama":
            set_setting(uid, "ollama_model", model)
            await q.edit_message_text(f"⏳ Проверяю/загружаю модель {model}...")
            await ensure_ollama_model(model)
        else:
            set_setting(uid, "openai_model", model)
        await q.edit_message_text("✅ Модель изменена", reply_markup=main_menu(get_settings(uid)))
    elif data.startswith("reasoning:"):
        set_setting(uid, "reasoning_level", data.split(":")[1])
        await q.edit_message_text("✅ Reasoning Level изменен", reply_markup=main_menu(get_settings(uid)))
    elif data.startswith("exchange:"):
        set_setting(uid, "exchange", data.split(":")[1])
        await q.edit_message_text("✅ Биржа изменена", reply_markup=main_menu(get_settings(uid)))
    elif data.startswith("timeframe:"):
        set_setting(uid, "timeframe_mode", data.split(":")[1])
        await q.edit_message_text("✅ Таймфрейм изменен", reply_markup=main_menu(get_settings(uid)))
    elif data.startswith("mode:"):
        set_setting(uid, "bot_mode", data.split(":")[1])
        await q.edit_message_text("✅ Режим бота изменен", reply_markup=main_menu(get_settings(uid)))
    elif data.startswith("tradingmode:"):
        set_setting(uid, "trading_mode", data.split(":")[1])
        await q.edit_message_text("✅ Trading Mode изменен", reply_markup=main_menu(get_settings(uid)))
    elif data == "toggle:btceth":
        new = "btc_eth" if s["market_universe"] != "btc_eth" else "all"
        set_setting(uid, "market_universe", new)
        await q.edit_message_text("✅ Режим рынка изменен", reply_markup=main_menu(get_settings(uid)))
    elif data == "toggle:sessions":
        new = not bool(s.get("session_filter", False))
        set_setting(uid, "session_filter", new)
        await q.edit_message_text(f"✅ Азия/Америка: {'ON' if new else 'OFF'}", reply_markup=main_menu(get_settings(uid)))
    elif data.startswith("market:"):
        set_setting(uid, "market_universe", data.split(":")[1])
        await q.edit_message_text("✅ Режим рынка изменен", reply_markup=main_menu(get_settings(uid)))
    elif data.startswith("scan:"):
        n = int(data.split(":")[1])
        await q.edit_message_text(f"⏳ Сканирую Top-{n}...")
        text = await run_top_scan(uid, n)
        await q.edit_message_text(text[:3900], reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🧠 Отправить в ИИ на подтверждение", callback_data="aiconfirm")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
        ]))
    elif data == "aiconfirm":
        await q.edit_message_text("⏳ Отправляю кандидатов в ИИ...")
        text = await ai_confirm(uid)
        await q.edit_message_text(text[:3900], reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🚀 Открыть подтвержденные", callback_data="open_confirmed")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
        ]))
    elif data == "open_confirmed":
        # Safe placeholder: actual order execution should be implemented with exact exchange rules.
        await q.edit_message_text("🛡 Risk Manager проверил заявки.\n\n⚠️ В этой сборке открытие ордеров подключается через exchange adapter. Проверь API и режим Confirm/Auto перед реальным запуском.")
    elif data == "status":
        await q.edit_message_text(get_status_text(uid))
    elif data == "ping":
        await q.edit_message_text(ping_text(uid))
    elif data == "help":
        await q.edit_message_text(help_text())
    elif data == "cancel":
        await q.edit_message_text("Отменено.")


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id(update)
    s = get_settings(uid)
    text = update.message.text.strip()
    parts = text.split()
    symbol = parts[0]
    timeframe = parts[1] if len(parts) > 1 and re.match(r"^\d+[mhd]$", parts[1].lower()) else None

    if re.match(r"^[A-Za-z]{2,10}(USDT)?$", symbol):
        await update.message.reply_text("⏳ Анализирую...")
        try:
            result = await signal_for_symbol(uid, symbol, timeframe)
        except Exception as e:
            result = f"Ошибка: {e}"
        await update.message.reply_text(result[:3900])
    elif s["bot_mode"] == "chat":
        try:
            result = await call_ai(uid, text)
        except Exception as e:
            result = f"Ошибка AI: {e}"
        await update.message.reply_text(result[:3900])
    else:
        await update.message.reply_text("Напиши монету, например BTC или ETH, либо используй /help.")


async def post_init(app: Application):
    app.create_task(unload_idle_models())


def main():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")

    app = Application.builder().token(TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("ping", ping_cmd))
    app.add_handler(CommandHandler("signal", signal_cmd))

    app.add_handler(CommandHandler("top50", lambda u,c: top_cmd(u,c,50)))
    app.add_handler(CommandHandler("top100", lambda u,c: top_cmd(u,c,100)))
    app.add_handler(CommandHandler("top200", lambda u,c: top_cmd(u,c,200)))
    app.add_handler(CommandHandler("minscore", minscore_cmd))
    app.add_handler(CommandHandler("toplimit", toplimit_cmd))

    app.add_handler(CommandHandler("sessions", sessions_cmd))
    app.add_handler(CommandHandler("sessions_on", lambda u,c: (set_setting(user_id(u), "session_filter", True), u.message.reply_text("✅ Азия/Америка включено. Учитываю 03:00 МСК и 16:30 МСК."))[1]))
    app.add_handler(CommandHandler("sessions_off", lambda u,c: (set_setting(user_id(u), "session_filter", False), u.message.reply_text("✅ Азия/Америка выключено."))[1]))
    app.add_handler(CommandHandler("timeframe", timeframe_cmd))
    app.add_handler(CommandHandler("market", market_cmd))
    app.add_handler(CommandHandler("onlybtceth_on", lambda u,c: (set_setting(user_id(u), "market_universe", "btc_eth"), u.message.reply_text("✅ Only BTC/ETH включен. Top Scanner отключен."))[1]))
    app.add_handler(CommandHandler("onlybtceth_off", lambda u,c: (set_setting(user_id(u), "market_universe", "all"), u.message.reply_text("✅ All Futures Market включен."))[1]))

    app.add_handler(CommandHandler("risk", lambda u,c: simple_set_cmd(u,c,"risk_percent",float,"Risk %")))
    app.add_handler(CommandHandler("leverage", lambda u,c: simple_set_cmd(u,c,"leverage",int,"Leverage")))
    app.add_handler(CommandHandler("maxtrades", lambda u,c: simple_set_cmd(u,c,"max_trades",int,"Max trades")))
    app.add_handler(CommandHandler("maxrisk", lambda u,c: simple_set_cmd(u,c,"max_risk_percent",float,"Max risk %")))

    app.add_handler(CommandHandler("trading_on", lambda u,c: (set_setting(user_id(u), "trading_enabled", True), u.message.reply_text("✅ Trading ON"))[1]))
    app.add_handler(CommandHandler("trading_off", lambda u,c: (set_setting(user_id(u), "trading_enabled", False), u.message.reply_text("✅ Trading OFF"))[1]))
    app.add_handler(CommandHandler("aiauto_on", lambda u,c: (set_setting(user_id(u), "ai_auto", True), u.message.reply_text("✅ AI Auto Confirm ON"))[1]))
    app.add_handler(CommandHandler("aiauto_off", lambda u,c: (set_setting(user_id(u), "ai_auto", False), u.message.reply_text("✅ AI Auto Confirm OFF"))[1]))

    app.add_handler(CommandHandler("setapi", setapi_cmd))
    app.add_handler(CommandHandler("delapi", delapi_cmd))
    app.add_handler(CommandHandler("testapi", testapi_cmd))
    app.add_handler(CommandHandler("setopenai", setopenai_cmd))
    app.add_handler(CommandHandler("delopenai", delopenai_cmd))
    app.add_handler(CommandHandler("testopenai", testopenai_cmd))
    app.add_handler(CommandHandler("openai_on", lambda u,c: (set_setting(user_id(u), "ai_provider", "openai"), u.message.reply_text("✅ OpenAI ChatGPT включен"))[1]))
    app.add_handler(CommandHandler("openai_off", lambda u,c: (set_setting(user_id(u), "ai_provider", "ollama"), u.message.reply_text("✅ Ollama Local включен"))[1]))

    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    app.run_polling()


if __name__ == "__main__":
    main()
