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
# CONFIG
# -------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "8432592746:AAEg3uaH-Xa6tf-pqZI_RjiKaRZAQfsHbXM")
CHAT_ID = os.environ.get("TG_CHAT_ID", "1623720732")
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

BITGET_API_KEY = os.environ.get("BITGET_API_KEY", "")
BITGET_API_SECRET = os.environ.get("BITGET_API_SECRET", "")
BITGET_API_PASSPHRASE = os.environ.get("BITGET_API_PASSPHRASE", "")

SETTINGS_FILE = "settings.json"
OPEN_TRADES_FILE = "open_trades.json"
CLOSED_TRADES_FILE = "closed_trades.json"
VIRTUAL_BALANCE_FILE = "virtual_balance.json"

SYMBOLS = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT"]

SL_PCT = 0.02
TP_PCT = 0.04
LEVERAGE = 10
RSI_WINDOW = 14
SMA50 = 50
SMA200 = 200
LEVEL_LOOKBACK = 50
LEVEL_THRESHOLD_PCT = 0.005

ALL_TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h"]
ACTIVE_TF = ["5m", "15m"]

INVEST_AMOUNT = 20.0
TRADE_MODE = "virtual"
CHECK_INTERVAL = 60

DEFAULT_VIRTUAL_BALANCE = {"currency": "USDT", "total": 1000.0, "available": 1000.0}

# -------------------------
# STORAGE
# -------------------------
def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str, ensure_ascii=False)

def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default

open_trades: List[Dict] = load_json(OPEN_TRADES_FILE, [])
closed_trades: List[Dict] = load_json(CLOSED_TRADES_FILE, [])
settings = load_json(SETTINGS_FILE, {})
virtual_balance = load_json(VIRTUAL_BALANCE_FILE, DEFAULT_VIRTUAL_BALANCE)

ACTIVE_TF = settings.get("ACTIVE_TF", ACTIVE_TF)
INVEST_AMOUNT = settings.get("INVEST_AMOUNT", INVEST_AMOUNT)
TRADE_MODE = settings.get("TRADE_MODE", TRADE_MODE)

# -------------------------
# EXCHANGE
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
    else:
        exchange = ccxt.bitget({"enableRateLimit": True})
    exchange.load_markets()
    logging.info("Bitget initialized")
except Exception as e:
    logging.error(f"Ошибка инициализации биржи: {e}")
    exchange = None

# -------------------------
# VIRTUAL BALANCE
# -------------------------
def save_virtual_balance():
    save_json(VIRTUAL_BALANCE_FILE, virtual_balance)

def virtual_reserve(amount_usd):
    if virtual_balance.get("available", 0.0) >= amount_usd:
        virtual_balance["available"] -= amount_usd
        save_virtual_balance()
        return True
    return False

def virtual_release(amount_usd):
    virtual_balance["available"] += amount_usd
    virtual_balance["total"] += amount_usd
    save_virtual_balance()

def save_state():
    save_json(OPEN_TRADES_FILE, open_trades)
    save_json(CLOSED_TRADES_FILE, closed_trades)
    save_json(SETTINGS_FILE, {"ACTIVE_TF": ACTIVE_TF, "INVEST_AMOUNT": INVEST_AMOUNT, "TRADE_MODE": TRADE_MODE})

# -------------------------
# TELEGRAM
# -------------------------
def send_message(chat_id, text):
    url = f"{API_URL}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        logging.error(f"Ошибка Telegram: {e}")

# -------------------------
# MARKET HELPERS
# -------------------------
def fetch_ohlcv(symbol, timeframe="5m", limit=300):
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

# -------------------------
# TRADES
# -------------------------
def pnl_percent(entry_price, current_price, direction):
    return (current_price - entry_price) / entry_price if direction == "LONG" else (entry_price - current_price) / entry_price

def open_trade(symbol, direction, entry_price, timeframe, reason="signal"):
    sl_price = entry_price * (1 - SL_PCT) if direction == "LONG" else entry_price * (1 + SL_PCT)
    tp_price = entry_price * (1 + TP_PCT) if direction == "LONG" else entry_price * (1 - TP_PCT)
    trade = {
        "id": f"{symbol}-{timeframe}-{int(time.time())}",
        "symbol": symbol,
        "direction": direction,
        "entry_price": entry_price,
        "sl_price": sl_price,
        "tp_price": tp_price,
        "invest": INVEST_AMOUNT,
        "leverage": LEVERAGE,
        "opened_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "timeframe": timeframe,
        "status": "OPEN",
        "reason": reason,
    }
    open_trades.append(trade)
    save_state()
    send_message(CHAT_ID, f"📈 Открыта сделка {symbol} {direction} @ {entry_price}")
    return trade

def close_trade(trade, exit_price, reason):
    pnl_p = pnl_percent(trade["entry_price"], exit_price, trade["direction"])
    pnl_cash = round(trade["invest"] * trade["leverage"] * pnl_p, 2)
    trade["status"] = "CLOSED"
    trade["exit_price"] = exit_price
    trade["pnl_percent"] = round(pnl_p * 100, 2)
    trade["pnl_cash"] = pnl_cash
    trade["closed_at"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    trade["close_reason"] = reason
    closed_trades.append(trade)
    open_trades.remove(trade)
    save_state()
    virtual_release(trade["invest"] + pnl_cash)
    send_message(CHAT_ID, f"✅ Закрыта сделка {trade['symbol']} {trade['direction']} PnL={trade['pnl_percent']}% ({pnl_cash}$)")

# -------------------------
# SIGNALS
# -------------------------
def check_signals_once():
    for symbol in SYMBOLS:
        for tf in ACTIVE_TF:
            try:
                df = fetch_ohlcv(symbol, timeframe=tf, limit=300)
                df = compute_indicators(df)
                if len(df) < SMA200:
                    continue
                price = float(df["close"].iloc[-1])
                rsi = float(df["rsi"].iloc[-1])
                sma50 = float(df["sma50"].iloc[-1])
                sma200 = float(df["sma200"].iloc[-1])

                direction = None
                reason = ""
                if rsi < 30 and price > sma200:
                    direction, reason = "LONG", "RSI<30 и цена>SMA200"
                elif rsi > 70 and price < sma200:
                    direction, reason = "SHORT", "RSI>70 и цена<SMA200"

                if direction:
                    existing = next((t for t in open_trades if t["symbol"] == symbol and t["timeframe"] == tf), None)
                    if existing:
                        continue
                    if not virtual_reserve(INVEST_AMOUNT):
                        send_message(CHAT_ID, f"⚠ Недостаточно виртуального баланса для {symbol}")
                        continue
                    open_trade(symbol, direction, price, tf, reason)
            except Exception as e:
                logging.error(f"Ошибка сигнала {symbol} {tf}: {e}")

# -------------------------
# BACKGROUND WORKER
# -------------------------
def run_bot():
    while True:
        check_signals_once()
        time.sleep(CHECK_INTERVAL)

threading.Thread(target=run_bot, daemon=True).start()

# -------------------------
# FLASK SERVER
# -------------------------
app = Flask(__name__)

@app.route("/", methods=["GET"])
def home():
    return "Trading bot is running!"

@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    data = request.get_json()
    logging.info(f"Update: {data}")
    return "ok"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
