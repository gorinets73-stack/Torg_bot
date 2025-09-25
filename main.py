#!/usr/bin/env python3
# main.py — Bitget Futures trading bot (Flask webhook + background trading threads)
import os
import time
import json
import logging
import threading
from datetime import datetime
from flask import Flask, request, jsonify

import pandas as pd
import ccxt
import requests
from ta.momentum import RSIIndicator
from ta.trend import SMAIndicator

# ---------------- CONFIG ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "YOUR_BOT_TOKEN")
CHAT_ID = os.environ.get("TG_CHAT_ID", "YOUR_CHAT_ID")
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

BITGET_API_KEY = os.environ.get("BITGET_API_KEY", "")
BITGET_API_SECRET = os.environ.get("BITGET_API_SECRET", "")
BITGET_API_PASSPHRASE = os.environ.get("BITGET_API_PASSPHRASE", "")

SETTINGS_FILE = "settings.json"
OPEN_TRADES_FILE = "open_trades.json"
CLOSED_TRADES_FILE = "closed_trades.json"
VIRTUAL_BALANCE_FILE = "virtual_balance.json"

# Popular futures symbols (USDT-m)
SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
    "ADA/USDT", "DOGE/USDT", "AVAX/USDT", "MATIC/USDT", "LTC/USDT",
    "DOT/USDT", "LINK/USDT", "TRX/USDT", "ATOM/USDT", "BCH/USDT"
]

# Strategy params
SL_PCT = 0.02
TP_PCT = 0.04
RSI_WINDOW = 14
SMA50 = 50
SMA200 = 200
LEVEL_LOOKBACK = 50
LEVEL_THRESHOLD_PCT = 0.005

# Timeframes / defaults
ALL_TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h"]
ACTIVE_TF = ["1m", "5m", "15m"]

INVEST_AMOUNT = 20.0
TRADE_MODE = "virtual"  # "virtual" or "real"
LEVERAGE = 10
CHECK_INTERVAL = 60  # seconds

# in-memory state
open_trades = []
closed_trades = []
state_lock = threading.Lock()

# ---------------- EXCHANGE (Bitget Futures / swap) ----------------
exchange = None
if BITGET_API_KEY and BITGET_API_SECRET:
    try:
        exchange = ccxt.bitget({
            "apiKey": BITGET_API_KEY,
            "secret": BITGET_API_SECRET,
            "password": BITGET_API_PASSPHRASE,
            "enableRateLimit": True,
            "options": {
                "defaultType": "swap",   # IMPORTANT: use swap (futures)
            }
        })
        exchange.load_markets()
        logging.info("Bitget (swap) initialized via ccxt.")
    except Exception as e:
        logging.error(f"Error initializing Bitget: {e}")
        exchange = None
else:
    logging.info("Bitget keys not provided — running in virtual/demo mode.")

# ---------------- STORAGE HELPERS ----------------
def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str, ensure_ascii=False)
    except Exception as e:
        logging.error(f"Error saving {path}: {e}")

def load_json(path, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logging.error(f"Error reading {path}: {e}")
    return default

def save_state():
    with state_lock:
        save_json(OPEN_TRADES_FILE, open_trades)
        save_json(CLOSED_TRADES_FILE, closed_trades)

def load_state():
    global open_trades, closed_trades
    with state_lock:
        open_trades = load_json(OPEN_TRADES_FILE, [])
        closed_trades = load_json(CLOSED_TRADES_FILE, [])

def save_settings():
    try:
        data = {
            "ACTIVE_TF": ACTIVE_TF,
            "INVEST_AMOUNT": INVEST_AMOUNT,
            "TRADE_MODE": TRADE_MODE,
            "LEVERAGE": LEVERAGE,
            "SYMBOLS": SYMBOLS
        }
        save_json(SETTINGS_FILE, data)
    except Exception as e:
        logging.error(f"Error saving settings: {e}")

def load_settings():
    global ACTIVE_TF, INVEST_AMOUNT, TRADE_MODE, LEVERAGE, SYMBOLS
    data = load_json(SETTINGS_FILE, {})
    ACTIVE_TF = data.get("ACTIVE_TF", ACTIVE_TF)
    INVEST_AMOUNT = data.get("INVEST_AMOUNT", INVEST_AMOUNT)
    TRADE_MODE = data.get("TRADE_MODE", TRADE_MODE)
    LEVERAGE = data.get("LEVERAGE", LEVERAGE)
    SYMBOLS = data.get("SYMBOLS", SYMBOLS)

# ---------------- VIRTUAL BALANCE ----------------
DEFAULT_VIRTUAL_BALANCE = {"currency": "USDT", "total": 1000.0, "available": 1000.0}

def load_virtual_balance():
    data = load_json(VIRTUAL_BALANCE_FILE, DEFAULT_VIRTUAL_BALANCE)
    if "currency" not in data:
        data["currency"] = "USDT"
    if "total" not in data:
        data["total"] = float(DEFAULT_VIRTUAL_BALANCE["total"])
    if "available" not in data:
        data["available"] = float(data["total"])
    return data

def save_virtual_balance(bal):
    save_json(VIRTUAL_BALANCE_FILE, bal)

virtual_balance = load_virtual_balance()

def virtual_reserve(amount_usd):
    global virtual_balance
    if virtual_balance["available"] >= amount_usd:
        virtual_balance["available"] = round(virtual_balance["available"] - amount_usd, 8)
        save_virtual_balance(virtual_balance)
        return True
    return False

def virtual_release(amount_usd):
    global virtual_balance
    virtual_balance["available"] = round(virtual_balance["available"] + amount_usd, 8)
    virtual_balance["total"] = round(virtual_balance["total"] + amount_usd, 8)
    save_virtual_balance(virtual_balance)

# ---------------- TELEGRAM HELPERS ----------------
def send_message(chat_id, text, reply_markup=None, parse_mode="HTML"):
    url = f"{API_URL}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logging.error(f"Error sending Telegram message: {e}")

# ---------------- MARKET HELPERS ----------------
def fetch_ohlcv(symbol, timeframe="1h", limit=300):
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
    low = df["low"].tail(lookback).min()
    high = df["high"].tail(lookback).max()
    return float(low), float(high)

def pnl_percent(entry_price, current_price, direction):
    if direction == "LONG":
        return (current_price - entry_price) / entry_price
    else:
        return (entry_price - current_price) / entry_price

def cash_pnl(invest_amount, leverage, pnl_p):
    return invest_amount * leverage * pnl_p

def size_from_usd(symbol, price, amount_usd, leverage):
    # approximate base quantity (needs rounding for exchange precision)
    amount = (amount_usd * leverage) / price
    return float(round(amount, 6))

# ---------------- ORDERS (REAL) ----------------
def place_real_market_order(symbol, side, amount):
    try:
        # NOTE: for some futures markets extra params may be required (reduceOnly, positionSide, marginMode).
        order = exchange.create_order(symbol, "market", side, amount, None, {})
        logging.info(f"Real market order placed: {order}")
        return order
    except Exception as e:
        logging.error(f"Error placing real order: {e}")
        return None

def close_real_position_by_market(symbol, side_opposite, amount):
    return place_real_market_order(symbol, side_opposite, amount)

# ---------------- TRADES ----------------
def open_trade(symbol, direction, entry_price, timeframe, strategy_source="signal", invest=INVEST_AMOUNT, real_order=None, amount_base=None):
    sl_price = entry_price * (1 - SL_PCT) if direction == "LONG" else entry_price * (1 + SL_PCT)
    tp_price = entry_price * (1 + TP_PCT) if direction == "LONG" else entry_price * (1 - TP_PCT)
    trade = {
        "id": f"{symbol}-{timeframe}-{int(time.time())}",
        "symbol": symbol,
        "direction": direction,  # "LONG" or "SHORT"
        "entry_price": float(entry_price),
        "sl_price": float(sl_price),
        "tp_price": float(tp_price),
        "invest": invest,
        "leverage": LEVERAGE,
        "opened_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "strategy": strategy_source,
        "timeframe": timeframe,
        "status": "OPEN",
        "real": bool(real_order),
        "real_order": real_order,
        "amount_base": amount_base
    }
    with state_lock:
        open_trades.append(trade)
        save_state()
    send_message(CHAT_ID, f"💼 OPEN: {symbol} {direction} {timeframe}\nentry={entry_price:.2f}, SL={sl_price:.2f}, TP={tp_price:.2f}\nMode: {mode_status()}")
    return trade

def close_trade(trade, exit_price, reason):
    with state_lock:
        trade["closed_at"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        trade["exit_price"] = float(exit_price)
        trade["status"] = "CLOSED"
        pnl_p = pnl_percent(trade["entry_price"], exit_price, trade["direction"])
        trade["pnl_percent"] = round(pnl_p * 100, 4)
        trade["pnl_cash"] = round(cash_pnl(trade["invest"], trade["leverage"], pnl_p), 8)
        trade["close_reason"] = reason
        closed_trades.append(trade)
        open_trades[:] = [t for t in open_trades if t["id"] != trade["id"]]
        save_state()

    if not trade.get("real"):
        invest = trade["invest"]
        pnl_cash = trade.get("pnl_cash", 0.0)
        virtual_release(invest + pnl_cash)

    send_message(CHAT_ID, f"✅ CLOSED: {trade['symbol']} {trade['direction']}\nPnL={trade['pnl_percent']}% ({trade['pnl_cash']}$)\nReason: {reason}")

# ---------------- SIGNALS & LOGIC ----------------
def format_signal_text(symbol, df):
    price = df["close"].iloc[-1]
    rsi = df["rsi"].iloc[-1]
    sma50 = df["sma50"].iloc[-1]
    sma200 = df["sma200"].iloc[-1]
    support, resistance = levels_from_df(df)
    trend = "up" if price > sma200 else "down"
    if rsi < 30:
        rsi_status = "oversold (LONG possible)"
    elif rsi > 70:
        rsi_status = "overbought (SHORT possible)"
    else:
        rsi_status = "neutral"
    return (f"📊 {symbol}\nPrice: {price:.2f}$\nRSI: {round(rsi,2)} ({rsi_status})\nSMA50: {round(sma50,2)}, SMA200: {round(sma200,2)}\nTrend: {trend}\nLvls: S {round(support,2)}, R {round(resistance,2)}")

def is_price_near_level(price, level):
    return abs(price - level) / level <= LEVEL_THRESHOLD_PCT

def check_signals_once():
    if not SYMBOLS:
        return
    for symbol in list(SYMBOLS):
        for tf in list(ACTIVE_TF):
            try:
                if TRADE_MODE == "real" and exchange is None:
                    continue
                if exchange is None:
                    # in pure-virtual mode we cannot fetch real OHLCV via ccxt
                    continue
                df = fetch_ohlcv(symbol, timeframe=tf, limit=300)
                df = compute_indicators(df)
                if len(df) < SMA200:
                    continue
                price = float(df["close"].iloc[-1])
                rsi = float(df["rsi"].iloc[-1])
                sma200 = float(df["sma200"].iloc[-1])
                support, resistance = levels_from_df(df)

                direction = None
                reason = ""

                if rsi < 30 and price > sma200:
                    direction, reason = "LONG", "RSI < 30 & price > SMA200"
                elif rsi > 70 and price < sma200:
                    direction, reason = "SHORT", "RSI > 70 & price < SMA200"

                if not direction:
                    if is_price_near_level(price, support) and rsi < 40 and price > sma200:
                        direction, reason = "LONG", "near support + RSI<40 + uptrend"
                    elif is_price_near_level(price, resistance) and rsi > 60 and price < sma200:
                        direction, reason = "SHORT", "near resistance + RSI>60 + downtrend"

                if direction:
                    trade_id_prefix = f"{symbol}-{tf}"
                    with state_lock:
                        if any(t["id"].startswith(trade_id_prefix) for t in open_trades):
                            continue

                    signal_text = (f"⚡ SIGNAL: {symbol} {tf} {direction}\n{format_signal_text(symbol, df)}\nReason: {reason}\nSize: {INVEST_AMOUNT}$\nMode: {mode_status()}")
                    send_message(CHAT_ID, signal_text)

                    # open trade
                    if TRADE_MODE == "virtual" or exchange is None:
                        if not virtual_reserve(INVEST_AMOUNT):
                            send_message(CHAT_ID, f"⚠️ Not enough virtual balance for {symbol}")
                            continue
                        open_trade(symbol, direction, price, tf, strategy_source=reason, invest=INVEST_AMOUNT, real_order=None, amount_base=size_from_usd(symbol, price, INVEST_AMOUNT, LEVERAGE))
                    else:
                        amount_base = size_from_usd(symbol, price, INVEST_AMOUNT, LEVERAGE)
                        side = "buy" if direction == "LONG" else "sell"
                        # try best-effort set leverage (actual API may differ)
                        try:
                            if hasattr(exchange, "set_leverage"):
                                try:
                                    exchange.set_leverage(LEVERAGE, symbol)
                                except Exception:
                                    pass
                        except Exception:
                            pass
                        order = place_real_market_order(symbol, side, amount_base)
                        if order:
                            open_trade(symbol, direction, price, tf, strategy_source=reason, invest=INVEST_AMOUNT, real_order={"order": order}, amount_base=amount_base)
                        else:
                            send_message(CHAT_ID, f"⚠️ Failed to open real trade for {symbol}")
            except Exception as e:
                logging.error(f"Signal error {symbol} {tf}: {e}")

# ---------------- MONITOR OPEN TRADES ----------------
def monitor_open_trades():
    while True:
        try:
            with state_lock:
                trades_snapshot = list(open_trades)
            for trade in trades_snapshot:
                try:
                    symbol = trade["symbol"]
                    direction = trade["direction"]
                    if exchange:
                        ticker = exchange.fetch_ticker(symbol)
                        price = float(ticker.get("last") or ticker.get("close") or 0)
                    else:
                        # in pure virtual mode we skip active price monitoring
                        continue

                    if direction == "LONG":
                        if price <= trade["sl_price"]:
                            if trade.get("real") and exchange:
                                amount = trade.get("amount_base") or size_from_usd(symbol, trade["entry_price"], trade["invest"], trade["leverage"])
                                close_real_position_by_market(symbol, "sell", amount)
                            close_trade(trade, price, "Hit SL")
                        elif price >= trade["tp_price"]:
                            if trade.get("real") and exchange:
                                amount = trade.get("amount_base") or size_from_usd(symbol, trade["entry_price"], trade["invest"], trade["leverage"])
                                close_real_position_by_market(symbol, "sell", amount)
                            close_trade(trade, price, "Hit TP")
                    else:  # SHORT
                        if price >= trade["sl_price"]:
                            if trade.get("real") and exchange:
                                amount = trade.get("amount_base") or size_from_usd(symbol, trade["entry_price"], trade["invest"], trade["leverage"])
                                close_real_position_by_market(symbol, "buy", amount)
                            close_trade(trade, price, "Hit SL")
                        elif price <= trade["tp_price"]:
                            if trade.get("real") and exchange:
                                amount = trade.get("amount_base") or size_from_usd(symbol, trade["entry_price"], trade["invest"], trade["leverage"])
                                close_real_position_by_market(symbol, "buy", amount)
                            close_trade(trade, price, "Hit TP")

                except Exception as e:
                    logging.error(f"Monitoring error for {trade.get('id')}: {e}")
            time.sleep(5)
        except Exception as e:
            logging.error(f"monitor_open_trades loop error: {e}")
            time.sleep(5)

# ---------------- MODE & HELPERS ----------------
def mode_status():
    return "Virtual" if TRADE_MODE == "virtual" else "Real"

def format_settings_text():
    return (f"⚙️ Settings:\nSymbols: {', '.join(SYMBOLS)}\nTFs: {', '.join(ACTIVE_TF)}\nSize: {INVEST_AMOUNT}$\nLeverage: {LEVERAGE}x\nMode: {mode_status()}")

# ---------------- FLASK WEBHOOK (commands + callback_query) ----------------
app = Flask(__name__)
load_settings()
load_state()

@app.route("/", methods=["POST", "GET"])
def webhook():
    global ACTIVE_TF, INVEST_AMOUNT, TRADE_MODE, LEVERAGE, SYMBOLS
    if request.method == "GET":
        return "OK", 200

    data = request.json or {}
    logging.info(f"Received update: {data}")

    # callback_query
    if "callback_query" in data:
        try:
            cq = data["callback_query"]
            chat_id = cq.get("message", {}).get("chat", {}).get("id", CHAT_ID)
            data_cb = cq.get("data", "")

            if data_cb.startswith("tf_on_"):
                tf = data_cb.replace("tf_on_", "")
                if tf not in ACTIVE_TF:
                    ACTIVE_TF.append(tf)
                    save_settings()
                send_message(chat_id, f"✅ TF on: {tf}")

            elif data_cb.startswith("tf_off_"):
                tf = data_cb.replace("tf_off_", "")
                if tf in ACTIVE_TF:
                    ACTIVE_TF.remove(tf)
                    save_settings()
                send_message(chat_id, f"❌ TF off: {tf}")

            elif data_cb == "mode_virtual":
                TRADE_MODE = "virtual"
                save_settings()
                send_message(chat_id, "Mode: Virtual")

            elif data_cb == "mode_real":
                TRADE_MODE = "real"
                save_settings()
                send_message(chat_id, "Mode: Real")

            return jsonify({"ok": True})
        except Exception as e:
            logging.error(f"callback_query error: {e}")
            return jsonify({"ok": False}), 500

    # message handling
    if "message" in data:
        msg = data["message"]
        chat_id = msg.get("chat", {}).get("id", CHAT_ID)
        text = (msg.get("text") or "").strip()

        if "@" in text:
            text = text.split()[0]

        if text == "/start":
            send_message(chat_id, "✅ Bot started!\n" + format_settings_text())

        elif text == "/help":
            send_message(chat_id,
                         "Commands:\n"
                         "/start\n/help\n/settings\n/strategy\n/panel\n/mode\n"
                         "/tfs\n/amount N\n/leverage N\n/add_symbol SYMBOL\n/remove_symbol SYMBOL\n/open\n/closed\n/balance\n/force_check")

        elif text == "/settings":
            send_message(chat_id, format_settings_text())

        elif text == "/strategy":
            send_message(chat_id, f"RSI({RSI_WINDOW}) + SMA{SMA50}/{SMA200}\nLONG: RSI<{30} & price>SMA200\nSHORT: RSI>{70} & price<SMA200\nSL={int(SL_PCT*100)}% TP={int(TP_PCT*100)}")

        elif text == "/panel":
            kb = {"inline_keyboard": []}
            row = []
            for tf in ALL_TIMEFRAMES:
                if tf in ACTIVE_TF:
                    row.append({"text": f"❌ {tf}", "callback_data": f"tf_off_{tf}"})
                else:
                    row.append({"text": f"✅ {tf}", "callback_data": f"tf_on_{tf}"})
                if len(row) == 2:
                    kb["inline_keyboard"].append(row)
                    row = []
            if row:
                kb["inline_keyboard"].append(row)
            kb["inline_keyboard"].append([{"text": "Virtual", "callback_data": "mode_virtual"}, {"text": "Real", "callback_data": "mode_real"}])
            send_message(chat_id, "Panel:", kb)

        elif text == "/mode":
            kb = {"inline_keyboard": [[{"text": "Virtual", "callback_data": "mode_virtual"}, {"text": "Real", "callback_data": "mode_real"}]]}
            send_message(chat_id, "Choose mode:", kb)

        elif text == "/tfs":
            send_message(chat_id, "Active TFs: " + ", ".join(ACTIVE_TF))

        elif text.startswith("/amount"):
            try:
                parts = text.split()
                if len(parts) > 1:
                    new_amount = float(parts[1])
                    if new_amount > 0:
                        INVEST_AMOUNT = new_amount
                        save_settings()
                        send_message(chat_id, f"Size set: {INVEST_AMOUNT}$")
                    else:
                        send_message(chat_id, "Amount must be > 0")
                else:
                    send_message(chat_id, f"Current size: {INVEST_AMOUNT}$")
            except Exception as e:
                logging.error(f"/amount error: {e}")
                send_message(chat_id, "Usage: /amount 50")

        elif text.startswith("/leverage"):
            try:
                parts = text.split()
                if len(parts) > 1:
                    new_leverage = int(parts[1])
                    if 1 <= new_leverage <= 125:
                        LEVERAGE = new_leverage
                        save_settings()
                        send_message(chat_id, f"Leverage set: {LEVERAGE}x")
                    else:
                        send_message(chat_id, "Leverage must be 1..125")
                else:
                    send_message(chat_id, f"Current leverage: {LEVERAGE}x")
            except Exception as e:
                logging.error(f"/leverage error: {e}")
                send_message(chat_id, "Usage: /leverage 10")

        elif text.startswith("/add_symbol"):
            try:
                parts = text.split()
                if len(parts) > 1:
                    sym = parts[1].upper()
                    with state_lock:
                        if sym not in SYMBOLS:
                            SYMBOLS.append(sym)
                            save_settings()
                            send_message(chat_id, f"Added symbol: {sym}")
                        else:
                            send_message(chat_id, f"{sym} already in list")
                else:
                    send_message(chat_id, "Usage: /add_symbol BTC/USDT")
            except Exception as e:
                logging.error(f"/add_symbol error: {e}")
                send_message(chat_id, "Error adding symbol")

        elif text.startswith("/remove_symbol"):
            try:
                parts = text.split()
                if len(parts) > 1:
                    sym = parts[1].upper()
                    with state_lock:
                        if sym in SYMBOLS:
                            SYMBOLS.remove(sym)
                            save_settings()
                            send_message(chat_id, f"Removed symbol: {sym}")
                        else:
                            send_message(chat_id, f"{sym} not found")
                else:
                    send_message(chat_id, "Usage: /remove_symbol BTC/USDT")
            except Exception as e:
                logging.error(f"/remove_symbol error: {e}")
                send_message(chat_id, "Error removing symbol")

        elif text == "/open":
            with state_lock:
                if not open_trades:
                    send_message(chat_id, "No open trades")
                else:
                    for t in open_trades:
                        send_message(chat_id, f"{t['id']}: {t['symbol']} {t['direction']} {t['timeframe']} @ {t['entry_price']}")

        elif text == "/closed":
            with state_lock:
                if not closed_trades:
                    send_message(chat_id, "No closed trades")
                else:
                    last = closed_trades[-10:]
                    for t in last:
                        send_message(chat_id, f"{t['id']}: {t['symbol']} {t['direction']} PnL={t.get('pnl_percent','?')}% ({t.get('pnl_cash','?')}$)")

        elif text == "/balance":
            if TRADE_MODE == "virtual" or exchange is None:
                vb = load_virtual_balance()
                send_message(chat_id, f"Virtual balance: {vb['available']}/{vb['total']} {vb['currency']}")
            else:
                try:
                    bal = exchange.fetch_balance()
                    # best-effort show USDT total / equity
                    usdt_total = None
                    if isinstance(bal, dict):
                        usdt_total = bal.get("total", {}).get("USDT") or bal.get("USDT", {}).get("total")
                    send_message(chat_id, f"Exchange balance (best-effort): {usdt_total or bal}")
                except Exception as e:
                    logging.error(f"fetch_balance error: {e}")
                    send_message(chat_id, "Failed to fetch exchange balance")

        elif text == "/force_check":
            try:
                check_signals_once()
                send_message(chat_id, "Signals checked.")
            except Exception as e:
                logging.error(f"/force_check error: {e}")
                send_message(chat_id, "Error checking signals")

        else:
            send_message(chat_id, "Unknown command. /help")

    return jsonify({"ok": True})

# ---------------- BACKGROUND THREADS ----------------
def trading_thread():
    while True:
        try:
            check_signals_once()
        except Exception as e:
            logging.error(f"trading loop error: {e}")
        time.sleep(CHECK_INTERVAL)

monitor_thread = threading.Thread(target=monitor_open_trades, daemon=True)
monitor_thread.start()
trader_thread = threading.Thread(target=trading_thread, daemon=True)
trader_thread.start()

# ---------------- RUN ----------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logging.info("Starting Flask app...")
    app.run(host="0.0.0.0", port=port)
