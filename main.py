# trading_bot_smart_fixed.py
import os
import time
import json
import logging
import threading
from datetime import datetime
from typing import List, Dict

import pandas as pd
import ccxt
import requests
from ta.momentum import RSIIndicator
from ta.trend import SMAIndicator
from flask import Flask, request

# -------------------------
# ====== CONFIG ===========
# -------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Telegram (prefer environment variables)
BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "8432592746:AAEg3uaH-Xa6tf-pqZI_RjiKaRZAQfsHbXM")
CHAT_ID = os.environ.get("TG_CHAT_ID", "1623720732")
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Bitget API (for real trading)
BITGET_API_KEY = os.environ.get("BITGET_API_KEY", "")
BITGET_API_SECRET = os.environ.get("BITGET_API_SECRET", "")
BITGET_API_PASSPHRASE = os.environ.get("BITGET_API_PASSPHRASE", "")

# Files (state persist)
SETTINGS_FILE = "settings.json"
OPEN_TRADES_FILE = "open_trades.json"
CLOSED_TRADES_FILE = "closed_trades.json"
VIRTUAL_BALANCE_FILE = "virtual_balance.json"

# Symbols (initial list; will be validated on startup)
SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT",
    "XRP/USDT", "ADA/USDT", "DOGE/USDT", "TRX/USDT",
    "DOT/USDT", "MATIC/USDT", "AVAX/USDT", "LINK/USDT",
    "LTC/USDT", "BCH/USDT"
]

# Aliases for renamed tickers
SYMBOL_ALIASES = {
    "MATIC/USDT": "POL/USDT",
    "XBT/USDT": "BTC/USDT",
    "ETH2/USDT": "ETH/USDT",
    "BCHABC/USDT": "BCH/USDT",
    "BCHSV/USDT": "BSV/USDT",
    "DOGECOIN/USDT": "DOGE/USDT",
    "SHIB/USDT": "SHIB1000/USDT",
}

# Strategy / sizing params
SL_PCT = 0.02           # stop 2%
TP_PCT = 0.04           # take 4%
LEVERAGE = 10
RSI_WINDOW = 14
SMA50 = 50
SMA200 = 200
LEVEL_LOOKBACK = 50
LEVEL_THRESHOLD_PCT = 0.005  # 0.5% - proximity to level

# Timeframes
ALL_TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h"]
ACTIVE_TF = ["5m", "15m"]  # default active

# Mode, amounts
INVEST_AMOUNT = 20.0
TRADE_MODE = "virtual"  # "virtual" or "real"
CHECK_INTERVAL = 60     # seconds between signal checks

# Virtual balance defaults
DEFAULT_VIRTUAL_BALANCE = {"currency": "USDT", "total": 1000.0, "available": 1000.0}

# Strategy description (edit as you like)
STRATEGY_TEXT = (
    "Стратегия:\n"
    "- Тренд: SMA50 и SMA200 (если SMA50 > SMA200 — восходящий тренд, иначе нисходящий).\n"
    "- Индикатор: RSI(14).\n"
    "- LONG: RSI < 30 + цена выше SMA200 (и/или цена у поддержки).\n"
    "- SHORT: RSI > 70 + цена ниже SMA200 (и/или цена у сопротивления).\n"
    "- Уровни: анализ последних N свечей (локальные мини/макс). SL=2% TP=4%.\n"
    "Бот закрывает открытую позицию при противоположном сигнале и учитывает PnL в виртуале."
)

# -------------------------
# ====== STORAGE ==========
# -------------------------
def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str, ensure_ascii=False)
    except Exception as e:
        logging.error(f"Ошибка записи {path}: {e}")

def load_json(path, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logging.error(f"Ошибка чтения {path}: {e}")
    return default

# -------------------------
# ====== STATE ============
# -------------------------
open_trades: List[Dict] = load_json(OPEN_TRADES_FILE, [])
closed_trades: List[Dict] = load_json(CLOSED_TRADES_FILE, [])
settings = load_json(SETTINGS_FILE, {})
virtual_balance = load_json(VIRTUAL_BALANCE_FILE, DEFAULT_VIRTUAL_BALANCE)

# apply persisted settings if present
ACTIVE_TF = settings.get("ACTIVE_TF", ACTIVE_TF)
INVEST_AMOUNT = settings.get("INVEST_AMOUNT", INVEST_AMOUNT)
TRADE_MODE = settings.get("TRADE_MODE", TRADE_MODE)

# -------------------------
# ==== EXCHANGE CLIENT ====
# -------------------------
exchange = None
try:
    if BITGET_API_KEY and BITGET_API_SECRET:
        exchange = ccxt.bitget({
            "apiKey": BITGET_API_KEY,
            "secret": BITGET_API_SECRET,
            "password": BITGET_API_PASSPHRASE,
            "enableRateLimit": True,
        })
        exchange.load_markets()
        logging.info("Bitget initialized (authenticated).")
    else:
        exchange = ccxt.bitget({"enableRateLimit": True})
        exchange.load_markets()
        logging.info("Bitget initialized (public).")
except Exception as e:
    logging.error(f"Ошибка инициализации биржи: {e}")
    exchange = None

# -------------------------
# ==== VIRTUAL BALANCE ====
# -------------------------
def save_virtual_balance():
    save_json(VIRTUAL_BALANCE_FILE, virtual_balance)

def virtual_reserve(amount_usd):
    """Reserve funds for opening virtual trade."""
    if virtual_balance.get("available", 0.0) >= amount_usd:
        virtual_balance["available"] = round(virtual_balance["available"] - amount_usd, 8)
        save_virtual_balance()
        return True
    return False

def virtual_release(amount_usd):
    """Release funds on close (return invest + pnl)."""
    virtual_balance["available"] = round(virtual_balance.get("available", 0.0) + amount_usd, 8)
    virtual_balance["total"] = round(virtual_balance.get("total", 0.0) + amount_usd, 8)
    save_virtual_balance()

def save_state():
    save_json(OPEN_TRADES_FILE, open_trades)
    save_json(CLOSED_TRADES_FILE, closed_trades)
    save_json(SETTINGS_FILE, {"ACTIVE_TF": ACTIVE_TF, "INVEST_AMOUNT": INVEST_AMOUNT, "TRADE_MODE": TRADE_MODE})

# -------------------------
# ==== TELEGRAM HELPERS ===
# -------------------------
def send_message(chat_id, text, reply_markup=None, parse_mode="HTML"):
    url = f"{API_URL}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logging.error(f"Ошибка отправки в Telegram: {e}")

# -------------------------
# ==== MARKET HELPERS =====
# -------------------------
def fetch_ohlcv(symbol, timeframe="5m", limit=300):
    if not exchange:
        raise RuntimeError("Exchange not initialized.")
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["time", "open", "high", "low", "close", "volume"])
    df["time"] = pd.to_datetime(df["time"], unit="ms")
    return df

def compute_indicators(df):
    df = df.copy().reset_index(drop=True)
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["rsi"] = RSIIndicator(df["close"], window=RSI_WINDOW).rsi()
    df["sma50"] = SMAIndicator(df["close"], window=SMA50).sma_indicator()
    df["sma200"] = SMAIndicator(df["close"], window=SMA200).sma_indicator()
    return df

def levels_from_df(df, lookback=LEVEL_LOOKBACK):
    sub = df.tail(lookback)
    low = float(sub["low"].min())
    high = float(sub["high"].max())
    return low, high

# -------------------------
# ===== PNL & SIZING ======
# -------------------------
def pnl_percent(entry_price, current_price, direction):
    if direction == "LONG":
        return (current_price - entry_price) / entry_price
    else:
        return (entry_price - current_price) / entry_price

def cash_pnl(invest_amount, leverage, pnl_p):
    return invest_amount * leverage * pnl_p

def size_from_usd(price, amount_usd, leverage):
    """Return base asset amount to trade for given USD and leverage."""
    amount = (amount_usd * leverage) / price
    return float(round(amount, 6))

# -------------------------
# ===== REAL ORDERS =======
# -------------------------
def place_real_market_order(symbol, side, amount):
    try:
        order = exchange.create_order(symbol, "market", side, amount, None, {})
        logging.info(f"Real market order placed: {order}")
        return order
    except Exception as e:
        logging.error(f"Ошибка отправки реального ордера: {e}")
        return None

def close_real_position_by_market(symbol, side_opposite, amount):
    return place_real_market_order(symbol, side_opposite, amount)

# -------------------------
# ====== TRADES ===========
# -------------------------
def open_trade(symbol, direction, entry_price, timeframe, strategy_source="signal", invest=INVEST_AMOUNT, real_order=None):
    sl_price = entry_price * (1 - SL_PCT) if direction == "LONG" else entry_price * (1 + SL_PCT)
    tp_price = entry_price * (1 + TP_PCT) if direction == "LONG" else entry_price * (1 - TP_PCT)
    trade = {
        "id": f"{symbol}-{timeframe}-{int(time.time())}",
        "symbol": symbol,
        "direction": direction,
        "entry_price": float(entry_price),
        "sl_price": float(sl_price),
        "tp_price": float(tp_price),
        "invest": float(invest),
        "leverage": LEVERAGE,
        "opened_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "strategy": strategy_source,
        "timeframe": timeframe,
        "status": "OPEN",
        "real": bool(real_order),
        "real_order": real_order
    }
    open_trades.append(trade)
    save_state()
    send_message(CHAT_ID, f"💼 ОТКРЫТА СДЕЛКА:\n{symbol} {direction} {timeframe}\nentry={entry_price:.2f}, SL={sl_price:.2f}, TP={tp_price:.2f}\nРежим: {mode_status()}")
    return trade

def close_trade(trade, exit_price, reason):
    try:
        trade["closed_at"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        trade["exit_price"] = float(exit_price)
        trade["status"] = "CLOSED"
        pnl_p = pnl_percent(trade["entry_price"], exit_price, trade["direction"])
        trade["pnl_percent"] = round(pnl_p * 100, 4)
        trade["pnl_cash"] = round(cash_pnl(trade["invest"], trade["leverage"], pnl_p), 8)
        trade["close_reason"] = reason
        closed_trades.append(trade)
        # remove from open
        open_trades[:] = [t for t in open_trades if t["id"] != trade["id"]]
        save_state()

        # virtual balance update if not real
        if not trade.get("real"):
            invest = trade["invest"]
            pnl_cash = trade.get("pnl_cash", 0.0)
            virtual_release(invest + pnl_cash)
        send_message(CHAT_ID, f"✅ ЗАКРЫТА СДЕЛКА: {trade['symbol']} {trade['direction']}\nPnL={trade['pnl_percent']}% ({trade['pnl_cash']}$)\nПричина: {reason}")
    except Exception as e:
        logging.error(f"Ошибка при закрытии сделки {trade.get('id')}: {e}")

# -------------------------
# ===== SIGNALS & LOGIC ===
# -------------------------
def format_signal_text(symbol, df):
    price = df["close"].iloc[-1]
    rsi = df["rsi"].iloc[-1]
    sma50 = df["sma50"].iloc[-1]
    sma200 = df["sma200"].iloc[-1]
    support, resistance = levels_from_df(df)
    trend = "восходящий" if sma50 > sma200 else "нисходящий"
    rsi_status = "нейтральный"
    if rsi < 30:
        rsi_status = "перепродан (LONG возможен)"
    elif rsi > 70:
        rsi_status = "перекуплен (SHORT возможен)"
    return (f"📊 {symbol}\nЦена: {price:.4f}$\nRSI: {round(rsi,2)} ({rsi_status})\n"
            f"SMA50: {round(sma50,4)}, SMA200: {round(sma200,4)}\nТренд: {trend}\n"
            f"Уровни: поддержка {round(support,4)}, сопротивление {round(resistance,4)}")

def is_price_near_level(price, level):
    return abs(price - level) / level <= LEVEL_THRESHOLD_PCT

def find_existing_trade_for(symbol, tf):
    # returns open trade for same symbol and timeframe (if any)
    for t in open_trades:
        if t["symbol"] == symbol and t["timeframe"] == tf:
            return t
    return None

def check_signals_once():
    for symbol in SYMBOLS:
        for tf in list(ACTIVE_TF):
            try:
                # fetch
                df = fetch_ohlcv(symbol, timeframe=tf, limit=300)
                df = compute_indicators(df)
                if len(df) < SMA200:
                    continue
                price = float(df["close"].iloc[-1])
                rsi = float(df["rsi"].iloc[-1])
                sma50 = float(df["sma50"].iloc[-1])
                sma200 = float(df["sma200"].iloc[-1])
                support, resistance = levels_from_df(df)

                direction = None
                reason = ""

                # trend filter
                trend_up = sma50 > sma200

                # basic signals
                if rsi < 30 and price > sma200:
                    direction, reason = "LONG", "RSI < 30 и цена > SMA200"
                elif rsi > 70 and price < sma200:
                    direction, reason = "SHORT", "RSI > 70 и цена < SMA200"

                # levels signals (weaker)
                if not direction:
                    if is_price_near_level(price, support) and rsi < 40 and price > sma200:
                        direction, reason = "LONG", "Цена у поддержки + RSI < 40 + тренд вверх"
                    elif is_price_near_level(price, resistance) and rsi > 60 and price < sma200:
                        direction, reason = "SHORT", "Цена у сопротивления + RSI > 60 + тренд вниз"

                # respect trend: if trend_up only allow LONG
                if direction == "LONG" and not trend_up:
                    continue
                if direction == "SHORT" and trend_up:
                    continue

                if direction:
                    existing = find_existing_trade_for(symbol, tf)

                    if existing:
                        if existing["direction"] != direction:
                            # close existing at market price
                            try:
                                if existing.get("real") and exchange:
                                    amount = size_from_usd(existing["entry_price"], existing["invest"], existing["leverage"])
                                    side_opposite = "sell" if existing["direction"] == "LONG" else "buy"
                                    close_real_position_by_market(symbol, side_opposite, amount)
                            except Exception:
                                pass
                            # close and open new
                            close_trade(existing, price, f"Closed by opposite signal: {reason}")
                            # open new
                            if TRADE_MODE == "virtual" or not (BITGET_API_KEY and BITGET_API_SECRET):
                                if not virtual_reserve(INVEST_AMOUNT):
                                    send_message(CHAT_ID, f"⚠️ Недостаточно виртуального баланса для открытия сделки {symbol}.")
                                    continue
                                open_trade(symbol, direction, price, tf, strategy_source=reason, invest=INVEST_AMOUNT, real_order=None)
                            else:
                                try:
                                    amount = size_from_usd(price, INVEST_AMOUNT, LEVERAGE)
                                    side = "buy" if direction == "LONG" else "sell"
                                    if hasattr(exchange, "set_leverage"):
                                        exchange.set_leverage(LEVERAGE, symbol)
                                except Exception:
                                    pass
                                order = place_real_market_order(symbol, side, amount)
                                if order:
                                    open_trade(symbol, direction, price, tf, strategy_source=reason, invest=INVEST_AMOUNT, real_order=order)
                                else:
                                    send_message(CHAT_ID, f"⚠️ Ошибка открытия реальной сделки для {symbol}")
                        else:
                            continue
                    else:
                        # open new
                        signal_text = (f"⚡ Сигнал: {symbol} {tf} {direction}\n"
                                       f"{format_signal_text(symbol, df)}\nПричина: {reason}\nСумма: {INVEST_AMOUNT}$\nРежим: {mode_status()}")
                        send_message(CHAT_ID, signal_text)

                        if TRADE_MODE == "virtual" or not (BITGET_API_KEY and BITGET_API_SECRET):
                            if not virtual_reserve(INVEST_AMOUNT):
                                send_message(CHAT_ID, f"⚠️ Недостаточно виртуального баланса для открытия сделки {symbol}.")
                                continue
                            open_trade(symbol, direction, price, tf, strategy_source=reason, invest=INVEST_AMOUNT, real_order=None)
                        else:
                            try:
                                amount = size_from_usd(price, INVEST_AMOUNT, LEVERAGE)
                                side = "buy" if direction == "LONG" else "sell"
                                if hasattr(exchange, "set_leverage"):
                                    exchange.set_leverage(LEVERAGE, symbol)
                            except Exception:
                                pass
                            order = place_real_market_order(symbol, side, amount)
                            if order:
                                open_trade(symbol, direction, price, tf, strategy_source=reason, invest=INVEST_AMOUNT, real_order=order)
                            else:
                                send_message(CHAT_ID, f"⚠️ Ошибка открытия реальной сделки для {symbol}")

            except Exception as e:
                logging.error(f"Ошибка сигнала {symbol} {tf}: {e}")

# -------------------------
# === MONITOR OPEN TRADES ==
# -------------------------
def monitor_open_trades():
    for trade in list(open_trades):
        try:
            symbol = trade["symbol"]
            direction = trade["direction"]
            df = fetch_ohlcv(symbol, timeframe="1m", limit=3)
            price = float(df["close"].iloc[-1])

            # check SL/TP
            if direction == "LONG":
                if price <= trade["sl_price"]:
                    if trade.get("real") and exchange:
                        amount = size_from_usd(trade["entry_price"], trade["invest"], trade["leverage"])
                        close_real_position_by_market(symbol, "sell", amount)
                    close_trade(trade, price, "Hit SL")
                elif price >= trade["tp_price"]:
                    if trade.get("real") and exchange:
                        amount = size_from_usd(trade["entry_price"], trade["invest"], trade["leverage"])
                        close_real_position_by_market(symbol, "sell", amount)
                    close_trade(trade, price, "Hit TP")
            else:
                if price >= trade["sl_price"]:
                    if trade.get("real") and exchange:
                        amount = size_from_usd(trade["entry_price"], trade["invest"], trade["leverage"])
                        close_real_position_by_market(symbol, "buy", amount)
                    close_trade(trade, price, "Hit SL")
                elif price <= trade["tp_price"]:
                    if trade.get("real") and exchange:
                        amount = size_from_usd(trade["entry_price"], trade["invest"], trade["leverage"])
                        close_real_position_by_market(symbol, "buy", amount)
                    close_trade(trade, price, "Hit TP")
        except Exception as e:
            logging.error(f"Ошибка мониторинга {trade.get('id')}: {e}")

# -------------------------
# ===== HELPERS ==========
# -------------------------
def mode_status():
    return "Виртуальный" if TRADE_MODE == "virtual" else "Реальный"

def format_settings_text():
    return (f"⚙️ Текущие настройки:\n"
            f"Активные ТФ: {', '.join(ACTIVE_TF)}\n"
            f"Размер сделки: {INVEST_AMOUNT}$\n"
            f"Режим: {mode_status()}")

def validate_symbols():
    global SYMBOLS
    valid = []
    if not exchange:
        logging.warning("exchange не инициализирован — пропускаю валидацию символов")
        return
    for s in SYMBOLS:
        try:
            sym = s
           
