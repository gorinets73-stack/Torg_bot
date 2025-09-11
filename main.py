import os
import time
import logging
import json
import requests
import pandas as pd
import ccxt
from ta.momentum import RSIIndicator
from ta.trend import SMAIndicator
from flask import Flask, request
from threading import Thread

# === Настройки ===
TEST_MODE = True
SYMBOLS = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT",
           "XRP/USDT", "ADA/USDT", "DOGE/USDT", "TRX/USDT",
           "DOT/USDT", "POL/USDT", "AVAX/USDT", "LINK/USDT",
           "LTC/USDT", "BCH/USDT"]

INVEST_AMOUNT = 10
LEVERAGE = 10
SL_PCT = 0.02
TP_PCT = 0.04

BOT_TOKEN = "8432592746:AAEg3uaH-Xa6tf-pqZI_RjiKaRZAQfsHbXM"
CHAT_ID = "1623720732"
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

OPEN_TRADES_FILE = "open_trades.json"
CLOSED_TRADES_FILE = "closed_trades.json"

# === Виртуальный баланс ===
VIRTUAL_BALANCE = 1000  # стартовый депозит

# === Логирование ===
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# === Flask ===
app = Flask(__name__)

# === Биржа ===
exchange = ccxt.bitget({"enableRateLimit": True})
exchange.load_markets()

# === Память ===
open_trades = []
closed_trades = []

def save_state():
    try:
        with open(OPEN_TRADES_FILE, "w") as f:
            json.dump(open_trades, f, indent=2)
        with open(CLOSED_TRADES_FILE, "w") as f:
            json.dump(closed_trades, f, indent=2)
    except Exception as e:
        logging.error(f"Ошибка сохранения состояния: {e}")

def load_state():
    global open_trades, closed_trades
    try:
        if os.path.exists(OPEN_TRADES_FILE):
            with open(OPEN_TRADES_FILE, "r") as f:
                open_trades = json.load(f)
        if os.path.exists(CLOSED_TRADES_FILE):
            with open(CLOSED_TRADES_FILE, "r") as f:
                closed_trades = json.load(f)
    except Exception as e:
        logging.error(f"Ошибка загрузки состояния: {e}")

load_state()

# === Утилиты ===
def get_free_balance():
    used = sum([t["invest"] for t in open_trades])
    return VIRTUAL_BALANCE - used

def send_message(chat_id, text):
    url = f"{API_URL}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logging.error(f"Ошибка отправки в Telegram: {e}")

def fetch_ohlcv(symbol, timeframe="1h", limit=300):
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["time", "open", "high", "low", "close", "volume"])
    return df

def compute_indicators(df):
    df = df.copy()
    df["rsi"] = RSIIndicator(df["close"], window=14).rsi()
    df["sma50"] = SMAIndicator(df["close"], window=50).sma_indicator()
    df["sma200"] = SMAIndicator(df["close"], window=200).sma_indicator()
    return df

def levels_from_df(df, lookback=50):
    support = df["low"].tail(lookback).min()
    resistance = df["high"].tail(lookback).max()
    return support, resistance

def pnl_percent(entry_price, current_price, direction):
    if direction == "LONG":
        return (current_price - entry_price) / entry_price
    else:
        return (entry_price - current_price) / entry_price

def cash_pnl(invest_amount, leverage, pnl_percent):
    return invest_amount * leverage * pnl_percent

# === Формат сигналов (русский) ===
def format_signal_text(symbol, df):
    price = df["close"].iloc[-1]
    rsi = df["rsi"].iloc[-1]
    sma50 = df["sma50"].iloc[-1]
    sma200 = df["sma200"].iloc[-1]
    support, resistance = levels_from_df(df, lookback=50)

    trend = "восходящий" if price > sma200 else "нисходящий"
    if rsi < 30:
        rsi_status = "перепродан (LONG-сигнал возможен)"
    elif rsi > 70:
        rsi_status = "перекуплен (SHORT-сигнал возможен)"
    else:
        rsi_status = "нейтральный"

    return (f"📊 {symbol} [1h]\n"
            f"Цена: {price:.2f}$\n"
            f"RSI: {round(rsi,2)} ({rsi_status})\n"
            f"SMA50: {round(sma50,2)}, SMA200: {round(sma200,2)}\n"
            f"Тренд: {trend}\n"
            f"Уровни: поддержка {round(support,2)}, сопротивление {round(resistance,2)}")

# === Виртуальные сделки ===
def open_virtual_trade(symbol, direction, entry_price, strategy_source, timeframe, invest=INVEST_AMOUNT):
    sl_price = entry_price * (1 - SL_PCT) if direction == "LONG" else entry_price * (1 + SL_PCT)
    tp_price = entry_price * (1 + TP_PCT) if direction == "LONG" else entry_price * (1 - TP_PCT)
    trade = {
        "id": f"{symbol}-{int(time.time())}",
        "symbol": symbol,
        "direction": direction,
        "entry_price": float(entry_price),
        "sl_price": float(sl_price),
        "tp_price": float(tp_price),
        "invest": invest,
        "leverage": LEVERAGE,
        "opened_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
        "strategy": strategy_source,
        "timeframe": timeframe,
        "status": "OPEN"
    }
    open_trades.append(trade)
    save_state()
    send_message(CHAT_ID, f"💼 ОТКРЫТА СДЕЛКА:\n{symbol} | {direction}\nentry={entry_price:.2f}, SL={sl_price:.2f}, TP={tp_price:.2f}\nstrategy={strategy_source}")
    return trade

def close_virtual_trade(trade, exit_price, reason):
    trade["closed_at"] = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")
    trade["exit_price"] = float(exit_price)
    trade["status"] = "CLOSED"
    pnl_p = pnl_percent(trade["entry_price"], exit_price, trade["direction"])
    trade["pnl_percent"] = round(pnl_p * 100, 4)
    trade["pnl_cash"] = round(cash_pnl(trade["invest"], trade["leverage"], pnl_p), 8)
    trade["close_reason"] = reason
    closed_trades.append(trade)
    global open_trades
    open_trades = [t for t in open_trades if t["id"] != trade["id"]]
    save_state()
    send_message(CHAT_ID, f"✅ ЗАКРЫТА СДЕЛКА: {trade['symbol']} | {trade['direction']}\nexit={exit_price:.2f}\nP&L={trade['pnl_percent']}% ({trade['pnl_cash']}$)\nПричина: {reason}")

# === Ручные команды ===
def manual_open(symbol, direction, invest):
    if invest > get_free_balance():
        return "❌ Недостаточно средств."
    df = fetch_ohlcv(symbol, "1h", 2)
    entry_price = df["close"].iloc[-1]
    open_virtual_trade(symbol, direction, entry_price, "manual", "manual", invest=invest)
    return f"✅ Сделка открыта вручную: {symbol} {direction}, {invest}$ по цене {entry_price:.2f}"

def manual_close(symbol):
    for t in open_trades:
        if t["symbol"] == symbol:
            df = fetch_ohlcv(symbol, "1h", 2)
            exit_price = df["close"].iloc[-1]
            close_virtual_trade(t, exit_price, "Manual close")
            return f"✅ Сделка по {symbol} закрыта вручную."
    return "⚠️ Сделка не найдена."

def calc_profit(entry, target, direction, invest):
    pnl_p = pnl_percent(entry, target, direction)
    pnl_cash = cash_pnl(invest, LEVERAGE, pnl_p)
    return f"📈 Потенциальный результат:\nВход {entry}, выход {target}, {direction}\nPnL={round(pnl_p*100,2)}% ({round(pnl_cash,2)}$)"

# === Telegram вывод ===
def format_open_positions_text():
    if not open_trades:
        return "📝 Открытых сделок пока нет."
    text = "📊 Открытые сделки:\n"
    for t in open_trades:
        df = fetch_ohlcv(t["symbol"], "1h", 2)
        cur_price = df["close"].iloc[-1]
        pnl_p = pnl_percent(t["entry_price"], cur_price, t["direction"])
        pnl_cash = cash_pnl(t["invest"], t["leverage"], pnl_p)
        text += (f"{t['symbol']} | {t['direction']} | entry {t['entry_price']:.2f} | cur {cur_price:.2f} "
                 f"| PnL {round(pnl_p*100,2)}% ({round(pnl_cash,2)}$)\n")
    return text

def format_history_text(limit=20):
    if not closed_trades:
        return "📝 История сделок пуста."
    text = "📚 Закрытые сделки:\n"
    for t in closed_trades[-limit:]:
        text += (f"{t['closed_at']} | {t['symbol']} | {t['direction']} | entry {t['entry_price']:.2f} -> exit {t['exit_price']:.2f} "
                 f"| PnL {t['pnl_percent']}% ({t['pnl_cash']}$)\n")
    return text

def format_account_text():
    return (f"💰 Счёт (виртуал):\n"
            f"Депозит: {VIRTUAL_BALANCE}$\n"
            f"Свободно: {get_free_balance()}$\n"
            f"В сделках: {sum([t['invest'] for t in open_trades])}$\n"
            f"Закрыто сделок: {len(closed_trades)}")

# === Команды ===
def handle_command(command):
    cmd = command.strip().split()
    if not cmd: return "⚠️ Пустая команда."
    if cmd[0] == "/start": return "✅ Бот работает (виртуал). Команды: /help"
    if cmd[0] == "/help":
        return ("📌 Команды:\n"
                "/account - баланс\n"
                "/positions - открытые сделки\n"
                "/history - история\n"
                "/buy SYMBOL SUM - LONG\n"
                "/sell SYMBOL SUM - SHORT\n"
                "/close SYMBOL - закрыть\n"
                "/profit entry exit dir invest - расчёт прибыли")
    if cmd[0] == "/account": return format_account_text()
    if cmd[0] == "/positions": return format_open_positions_text()
    if cmd[0] == "/history": return format_history_text()
    if cmd[0] == "/buy" and len(cmd) == 3: return manual_open(cmd[1].upper(), "LONG", float(cmd[2]))
    if cmd[0] == "/sell" and len(cmd) == 3: return manual_open(cmd[1].upper(), "SHORT", float(cmd[2]))
    if cmd[0] == "/close" and len(cmd) == 2: return manual_close(cmd[1].upper())
    if cmd[0] == "/profit" and len(cmd) == 5: return calc_profit(float(cmd[1]), float(cmd[2]), cmd[3].upper(), float(cmd[4]))
    return "⚠️ Неизвестная команда. /help"

@app.route("/", methods=["POST", "GET"])
def webhook():
    if request.method == "POST":
        update = request.json
        if "message" in update and "text" in update["message"]:
            chat_id = update["message"]["chat"]["id"]
            reply = handle_command(update["message"]["text"].strip())
            send_message(chat_id, reply)
        return {"ok": True}
    return "Бот работает 🚀"

# === Запуск ===
if __name__ == "__main__":
    logging.info("Запуск бота...")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
