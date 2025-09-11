# trading_bot_with_balance.py
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

# -------------------------
# ====== CONFIG ===========
# -------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Telegram
BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "8432592746:AAEg3uaH-Xa6tf-pqZI_RjiKaRZAQfsHbXM")
CHAT_ID = os.environ.get("TG_CHAT_ID", "1623720732")
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Bitget API (для реальной торговли)
BITGET_API_KEY = os.environ.get("BITGET_API_KEY", "")
BITGET_API_SECRET = os.environ.get("BITGET_API_SECRET", "")
BITGET_API_PASSPHRASE = os.environ.get("BITGET_API_PASSPHRASE", "")

# Files
SETTINGS_FILE = "settings.json"
OPEN_TRADES_FILE = "open_trades.json"
CLOSED_TRADES_FILE = "closed_trades.json"
VIRTUAL_BALANCE_FILE = "virtual_balance.json"

# Symbols (подправь если Bitget требует другой нотации для фьючерсов)
SYMBOLS = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT"]

# Strategy params
SL_PCT = 0.02           # стоп 2%
TP_PCT = 0.04           # тейк 4%
LEVERAGE = 10
RSI_WINDOW = 14
SMA50 = 50
SMA200 = 200
LEVEL_LOOKBACK = 50
LEVEL_THRESHOLD_PCT = 0.005  # 0.5% - близость к уровню

# Timeframes
ALL_TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h"]
ACTIVE_TF = ["1m", "5m", "15m"]

# Defaults
INVEST_AMOUNT = 20.0
TRADE_MODE = "virtual"  # "virtual" or "real"
CHECK_INTERVAL = 60     # seconds

# Memory
open_trades = []
closed_trades = []

# -------------------------
# ====== EXCHANGE =========
# -------------------------
exchange = None
if BITGET_API_KEY and BITGET_API_SECRET:
    try:
        exchange = ccxt.bitget({
            "apiKey": BITGET_API_KEY,
            "secret": BITGET_API_SECRET,
            "password": BITGET_API_PASSPHRASE,
            "enableRateLimit": True,
        })
        exchange.load_markets()
        logging.info("Bitget initialized (ccxt).")
    except Exception as e:
        logging.error(f"Ошибка инициализации Bitget: {e}")
        exchange = None
else:
    logging.info("Bitget API keys не заданы — реальный режим недоступен (работа в виртуале).")

# -------------------------
# ==== STORAGE HELPERS ====
# -------------------------
def save_json(path, data):
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
    except Exception as e:
        logging.error(f"Ошибка записи {path}: {e}")

def load_json(path, default):
    try:
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
    except Exception as e:
        logging.error(f"Ошибка чтения {path}: {e}")
    return default

def save_state():
    save_json(OPEN_TRADES_FILE, open_trades)
    save_json(CLOSED_TRADES_FILE, closed_trades)

def load_state():
    global open_trades, closed_trades
    open_trades = load_json(OPEN_TRADES_FILE, [])
    closed_trades = load_json(CLOSED_TRADES_FILE, [])

def save_settings():
    try:
        data = {
            "ACTIVE_TF": ACTIVE_TF,
            "INVEST_AMOUNT": INVEST_AMOUNT,
            "TRADE_MODE": TRADE_MODE
        }
        save_json(SETTINGS_FILE, data)
    except Exception as e:
        logging.error(f"Ошибка сохранения настроек: {e}")

def load_settings():
    global ACTIVE_TF, INVEST_AMOUNT, TRADE_MODE
    data = load_json(SETTINGS_FILE, {})
    ACTIVE_TF = data.get("ACTIVE_TF", ACTIVE_TF)
    INVEST_AMOUNT = data.get("INVEST_AMOUNT", INVEST_AMOUNT)
    TRADE_MODE = data.get("TRADE_MODE", TRADE_MODE)

# -------------------------
# === VIRTUAL BALANCE =====
# -------------------------
DEFAULT_VIRTUAL_BALANCE = {"currency": "USDT", "total": 1000.0, "available": 1000.0}

def load_virtual_balance():
    data = load_json(VIRTUAL_BALANCE_FILE, DEFAULT_VIRTUAL_BALANCE)
    # ensure keys
    if "currency" not in data: data["currency"] = "USDT"
    if "total" not in data: data["total"] = float(DEFAULT_VIRTUAL_BALANCE["total"])
    if "available" not in data: data["available"] = float(data["total"])
    return data

def save_virtual_balance(bal):
    save_json(VIRTUAL_BALANCE_FILE, bal)

virtual_balance = load_virtual_balance()

def virtual_reserve(amount_usd):
    """
    Резервируем сумму при открытии виртуальной сделки (уменьшаем available).
    Возвращает True если успешно, False если недостаточно средств.
    """
    global virtual_balance
    if virtual_balance["available"] >= amount_usd:
        virtual_balance["available"] = round(virtual_balance["available"] - amount_usd, 8)
        save_virtual_balance(virtual_balance)
        return True
    return False

def virtual_release(amount_usd):
    """
    Освобождаем средства (например при закрытии возвращаем PnL)
    """
    global virtual_balance
    virtual_balance["available"] = round(virtual_balance["available"] + amount_usd, 8)
    virtual_balance["total"] = round(virtual_balance["total"] + amount_usd, 8)
    save_virtual_balance(virtual_balance)

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
def fetch_ohlcv(symbol, timeframe="1h", limit=300):
    if not exchange:
        raise RuntimeError("Exchange not initialized. Set Bitget API keys to enable fetch.")
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

# -------------------------
# ====== PNL & SIZING =====
# -------------------------
def pnl_percent(entry_price, current_price, direction):
    if direction == "LONG":
        return (current_price - entry_price) / entry_price
    else:
        return (entry_price - current_price) / entry_price

def cash_pnl(invest_amount, leverage, pnl_p):
    return invest_amount * leverage * pnl_p

def size_from_usd(symbol, price, amount_usd, leverage):
    amount = (amount_usd * leverage) / price
    return float(round(amount, 6))

# -------------------------
# ==== ORDERS (REAL) ======
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
# ====== TRADES =============
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
        "invest": invest,
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

    # if virtual, update virtual balance: return invest + pnl (we reserved invest on open)
    if not trade.get("real"):
        invest = trade["invest"]
        pnl_cash = trade.get("pnl_cash", 0.0)
        virtual_release(invest + pnl_cash)

    send_message(CHAT_ID, f"✅ ЗАКРЫТА СДЕЛКА: {trade['symbol']} {trade['direction']}\nPnL={trade['pnl_percent']}% ({trade['pnl_cash']}$)\nПричина: {reason}")

# -------------------------
# === SIGNALS & LOGIC ====
# -------------------------
def format_signal_text(symbol, df):
    price = df["close"].iloc[-1]
    rsi = df["rsi"].iloc[-1]
    sma50 = df["sma50"].iloc[-1]
    sma200 = df["sma200"].iloc[-1]
    support, resistance = levels_from_df(df)

    trend = "восходящий" if price > sma200 else "нисходящий"
    if rsi < 30:
        rsi_status = "перепродан (LONG-сигнал возможен)"
    elif rsi > 70:
        rsi_status = "перекуплен (SHORT-сигнал возможен)"
    else:
        rsi_status = "нейтральный"

    return (f"📊 {symbol}\n"
            f"Цена: {price:.2f}$\n"
            f"RSI: {round(rsi,2)} ({rsi_status})\n"
            f"SMA50: {round(sma50,2)}, SMA200: {round(sma200,2)}\n"
            f"Тренд: {trend}\n"
            f"Уровни: поддержка {round(support,2)}, сопротивление {round(resistance,2)}")

def is_price_near_level(price, level):
    return abs(price - level) / level <= LEVEL_THRESHOLD_PCT

def check_signals_once():
    for symbol in SYMBOLS:
        for tf in ACTIVE_TF:
            try:
                if exchange is None:
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
                    direction, reason = "LONG", "RSI перепродан + выше SMA200"
                elif rsi > 70 and price < sma200:
                    direction, reason = "SHORT", "RSI перекуплен + ниже SMA200"

                if not direction:
                    if is_price_near_level(price, support) and rsi < 40 and price > sma200:
                        direction, reason = "LONG", "Цена у поддержки + RSI<40 + тренд вверх"
                    elif is_price_near_level(price, resistance) and rsi > 60 and price < sma200:
                        direction, reason = "SHORT", "Цена у сопротивления + RSI>60 + тренд вниз"

                if direction:
                    trade_id_prefix = f"{symbol}-{tf}"
                    if any(t["id"].startswith(trade_id_prefix) for t in open_trades):
                        continue

                    signal_text = (f"⚡ Сигнал: {symbol} {tf} {direction}\n"
                                   f"{format_signal_text(symbol, df)}\n"
                                   f"Причина: {reason}\n"
                                   f"Сумма: {INVEST_AMOUNT}$\n"
                                   f"Режим: {mode_status()}")
                    send_message(CHAT_ID, signal_text)

                    if TRADE_MODE == "virtual" or not exchange:
                        if not virtual_reserve(INVEST_AMOUNT):
                            send_message(CHAT_ID, f"⚠️ Недостаточно виртуального баланса для открытия сделки {symbol}.")
                            continue
                        open_trade(symbol, direction, price, tf, strategy_source=reason, invest=INVEST_AMOUNT, real_order=None)
                    else:
                        amount = size_from_usd(symbol, price, INVEST_AMOUNT, LEVERAGE)
                        side = "buy" if direction == "LONG" else "sell"
                        try:
                            if hasattr(exchange, "set_leverage"):
                                try:
                                    exchange.set_leverage(LEVERAGE, symbol)
                                except Exception:
                                    pass
                        except Exception:
                            pass

                        order = place_real_market_order(symbol, side, amount)
                        if order:
                            real_order_info = {"order": order}
                            open_trade(symbol, direction, price, tf, strategy_source=reason, invest=INVEST_AMOUNT, real_order=real_order_info)
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
            if exchange:
                df = fetch_ohlcv(symbol, timeframe="1m", limit=5)
                price = float(df["close"].iloc[-1])
            else:
                continue

            if direction == "LONG":
                if price <= trade["sl_price"]:
                    if trade.get("real") and exchange:
                        amount = size_from_usd(symbol, trade["entry_price"], trade["invest"], trade["leverage"])
                        close_real_position_by_market(symbol, "sell", amount)
                    close_trade(trade, price, "Hit SL")
                elif price >= trade["tp_price"]:
                    if trade.get("real") and exchange:
                        amount = size_from_usd(symbol, trade["entry_price"], trade["invest"], trade["leverage"])
                        close_real_position_by_market(symbol, "sell", amount)
                    close_trade(trade, price, "Hit TP")
            else:  # SHORT
                if price >= trade["sl_price"]:
                    if trade.get("real") and exchange:
                        amount = size_from_usd(symbol, trade["entry_price"], trade["invest"], trade["leverage"])
                        close_real_position_by_market(symbol, "buy", amount)
                    close_trade(trade, price, "Hit SL")
                elif price <= trade["tp_price"]:
                    if trade.get("real") and exchange:
                        amount = size_from_usd(symbol, trade["entry_price"], trade["invest"], trade["leverage"])
                        close_real_position_by_market(symbol, "buy", amount)
                    close_trade(trade, price, "Hit TP")
        except Exception as e:
            logging.error(f"Ошибка мониторинга {trade.get('id')}: {e}")

# -------------------------
# ====== HELPERS ==========
# -------------------------
def mode_status():
    return "Виртуальный" if TRADE_MODE == "virtual" else "Реальный"

def format_settings_text():
    return (f"⚙️ Текущие настройки:\n"
            f"Активные ТФ: {', '.join(ACTIVE_TF)}\n"
            f"Размер сделки: {INVEST_AMOUNT}$\n"
            f"Режим: {mode_status()}")

# -------------------------
# ==== WEBHOOK & UI =======
# -------------------------
app = Flask(__name__)
load_settings()
load_state()

@app.route("/", methods=["POST", "GET"])
def webhook():
    global ACTIVE_TF, INVEST_AMOUNT, TRADE_MODE
    if request.method == "POST":
        data = request.json

        # MESSAGE
        if "message" in data:
            chat_id = data["message"]["chat"]["id"]
            text = data["message"].get("text", "")

            if "@" in text:
                # убираем упоминание бота, если есть
                text = text.split()[0]

            if text == "/start":
                send_message(chat_id, "✅ Бот запущен!\n" + format_settings_text())

            elif text == "/help":
                send_message(chat_id,
                             "📌 Команды:\n"
                             "/start\n/help\n/settings\n/strategy\n/panel\n/mode\n"
                             "/tfs\n/amount N\n/open\n/closed\n/balance")

            elif text == "/settings":
                send_message(chat_id, format_settings_text())

            elif text == "/strategy":
                send_message(chat_id,
                             "📖 Стратегия:\n"
                             "1) RSI(14) + SMA50/200\n"
                             "2) LONG: RSI<30 + цена > SMA200\n"
                             "3) SHORT: RSI>70 + цена < SMA200\n"
                             "4) SL=2% TP=4% + уровни")

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

                kb["inline_keyboard"].append([
                    {"text": "Set Virtual", "callback_data": "mode_virtual"},
                    {"text": "Set Real", "callback_data": "mode_real"}
                ])
                send_message(chat_id, "Панель управления:", kb)

            elif text == "/mode":
                kb = {
                    "inline_keyboard": [[
                        {"text": "Виртуальный", "callback_data": "mode_virtual"},
                        {"text": "Реальный", "callback_data": "mode_real"}
                    ]]
                }
                send_message(chat_id, "Выбери режим торговли:", kb)

            elif text == "/tfs":
                kb = {"inline_keyboard": []}
                for tf in ALL_TIMEFRAMES:
                    if tf in ACTIVE_TF:
                        kb["inline_keyboard"].append([
                            {"text": f"❌ {tf}", "callback_data": f"tf_off_{tf}"}
                        ])
                    else:
                        kb["inline_keyboard"].append([
                            {"text": f"✅ {tf}", "callback_data": f"tf_on_{tf}"}
                        ])
                send_message(chat_id, "Выбор таймфреймов:", kb)

        return "ok"
