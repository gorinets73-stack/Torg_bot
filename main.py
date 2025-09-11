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

# === Flask ===
app = Flask(__name__)

# === Telegram ===
BOT_TOKEN = "8432592746:AAEg3uaH-Xa6tf-pqZI_RjiKaRZAQfsHbXM"
CHAT_ID = "1623720732"
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

# === Логирование ===
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# === Биржа ===
exchange = ccxt.bitget({"enableRateLimit": True})
exchange.load_markets()

# === Файлы ===
OPEN_TRADES_FILE = "open_trades.json"
CLOSED_TRADES_FILE = "closed_trades.json"
SETTINGS_FILE = "settings.json"

# === Символы ===
SYMBOLS = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT",
           "XRP/USDT", "ADA/USDT", "DOGE/USDT", "TRX/USDT",
           "DOT/USDT", "POL/USDT", "AVAX/USDT", "LINK/USDT",
           "LTC/USDT", "BCH/USDT"]

# === Параметры стратегии ===
SL_PCT = 0.02   # стоп 2%
TP_PCT = 0.04   # тейк 4%
LEVERAGE = 10

# === Настройки по умолчанию ===
ALL_TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h"]
ACTIVE_TF = ["1m", "5m", "15m"]
INVEST_AMOUNT = 20
TRADE_MODE = "virtual"  # или "real"

# === Память ===
open_trades = []
closed_trades = []

# === Настройки ===
def save_settings():
    try:
        data = {
            "ACTIVE_TF": ACTIVE_TF,
            "INVEST_AMOUNT": INVEST_AMOUNT,
            "TRADE_MODE": TRADE_MODE
        }
        with open(SETTINGS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logging.error(f"Ошибка сохранения настроек: {e}")

def load_settings():
    global ACTIVE_TF, INVEST_AMOUNT, TRADE_MODE
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, "r") as f:
                data = json.load(f)
                ACTIVE_TF[:] = data.get("ACTIVE_TF", ACTIVE_TF)
                INVEST_AMOUNT = data.get("INVEST_AMOUNT", INVEST_AMOUNT)
                TRADE_MODE = data.get("TRADE_MODE", TRADE_MODE)
    except Exception as e:
        logging.error(f"Ошибка загрузки настроек: {e}")

load_settings()

# === Работа с файлами сделок ===
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
def send_message(chat_id, text):
    url = f"{API_URL}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
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
    return df["low"].tail(lookback).min(), df["high"].tail(lookback).max()

def pnl_percent(entry_price, current_price, direction):
    return (current_price - entry_price) / entry_price if direction == "LONG" else (entry_price - current_price) / entry_price

def cash_pnl(invest_amount, leverage, pnl_percent):
    return invest_amount * leverage * pnl_percent

def mode_status():
    return "Виртуальный" if TRADE_MODE == "virtual" else "Реальный"

# === Формат сигналов ===
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

# === Виртуальные сделки ===
def open_virtual_trade(symbol, direction, entry_price, strategy_source, timeframe, invest=INVEST_AMOUNT):
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
        "opened_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
        "strategy": strategy_source,
        "timeframe": timeframe,
        "status": "OPEN"
    }
    open_trades.append(trade)
    save_state()
    send_message(CHAT_ID, f"💼 ОТКРЫТА СДЕЛКА:\n{symbol} {direction} {timeframe}\nentry={entry_price:.2f}, SL={sl_price:.2f}, TP={tp_price:.2f}")
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
    open_trades[:] = [t for t in open_trades if t["id"] != trade["id"]]
    save_state()
    send_message(CHAT_ID, f"✅ ЗАКРЫТА СДЕЛКА: {trade['symbol']} {trade['direction']}\nPnL={trade['pnl_percent']}% ({trade['pnl_cash']}$)\nПричина: {reason}")

# === Проверка сигналов ===
def check_signals():
    for symbol in SYMBOLS:
        for tf in ACTIVE_TF:
            try:
                df = fetch_ohlcv(symbol, tf, 300)
                df = compute_indicators(df)
                if len(df) < 200: continue

                price = df["close"].iloc[-1]
                rsi = df["rsi"].iloc[-1]
                sma200 = df["sma200"].iloc[-1]

                direction = None
                reason = ""

                if rsi < 30 and price > sma200:
                    direction, reason = "LONG", "RSI перепродан + выше SMA200"
                elif rsi > 70 and price < sma200:
                    direction, reason = "SHORT", "RSI перекуплен + ниже SMA200"

                if direction:
                    trade_id = f"{symbol}-{tf}"
                    if any(t["id"].startswith(trade_id) for t in open_trades):
                        continue

                    signal_text = (f"⚡ Сигнал: {symbol} {tf} {direction}\n"
                                   f"{format_signal_text(symbol, df)}\n"
                                   f"Причина: {reason}\n"
                                   f"Сумма: {INVEST_AMOUNT}$\n"
                                   f"Режим: {mode_status()}")
                    send_message(CHAT_ID, signal_text)

                    if TRADE_MODE == "virtual":
                        open_virtual_trade(symbol, direction, price, "signal", tf)
                    else:
                        send_message(CHAT_ID, f"🚀 (Реал) {symbol} {direction}, {INVEST_AMOUNT}$ {tf}")
                        # здесь код открытия реальной сделки на бирже

            except Exception as e:
                logging.error(f"Ошибка анализа {symbol} {tf}: {e}")

# === Автотрейдинг (каждые 60 сек) ===
def auto_trading_loop():
    while True:
        try:
            check_signals()
        except Exception as e:
            logging.error(f"Ошибка авто-трейдинга: {e}")
        time.sleep(60)

# === Формат настроек ===
def format_settings():
    return (f"⚙️ Текущие настройки:\n"
            f"Активные ТФ: {', '.join(ACTIVE_TF)}\n"
            f"Размер сделки: {INVEST_AMOUNT}$\n"
            f"Режим: {mode_status()}")

# === Полное описание стратегии ===
def format_strategy():
    return ("📖 Стратегия:\n\n"
            "1. Используем RSI(14), SMA50, SMA200.\n"
            "2. Вход в LONG:\n   - RSI < 30 (перепродан)\n   - Цена выше SMA200 (тренд вверх)\n"
            "3. Вход в SHORT:\n   - RSI > 70 (перекуплен)\n   - Цена ниже SMA200 (тренд вниз)\n"
            "4. Stop Loss = 2% от входа\n"
            "5. Take Profit = 4% от входа\n"
            "6. Плечо x10\n"
            "7. Работаем на таймфреймах: 1m, 5m, 15m, 30m, 1h\n"
            "8. Можно переключать виртуал/реал через /mode")

# === Команды ===
def handle_command(command):
    global ACTIVE_TF, INVEST_AMOUNT, TRADE_MODE
    cmd = command.strip().split()
    if not cmd:
        return "⚠️ Пустая команда."

    base = cmd[0].lower()

    if base == "/start":
        return "✅ Бот запущен!\n" + format_settings()

    if base == "/help":
        return ("📌 Команды:\n"
                "/start - показать настройки\n"
                "/settings - текущие параметры\n"
                "/strategy - описание стратегии\n"
                "/mode virtual|real - переключение режима\n"
                "/tfs - список таймфреймов\n"
                "/tf on X - включить ТФ\n"
                "/tf off X - выключить ТФ\n"
                "/amount N - изменить размер сделки\n"
                "/open - список открытых сделок\n"
                "/closed - последние закрытые сделки")

    if base == "/settings":
        return format_settings()

    if base == "/strategy":
        return format_strategy()

    if base == "/mode":
        if len(cmd) < 2:
            return "⚠️ Используй: /mode virtual или /mode real"
        if cmd[1] in ["virtual", "real"]:
            TRADE_MODE = cmd[1]
            save_settings()
            return f"🔄 Режим изменён: {mode_status()}"
        return "⚠️ Используй: /mode virtual или /mode real"

    if base == "/tfs":
        return f"📊 Доступные ТФ: {', '.join(ALL_TIMEFRAMES)}\nАктивные: {', '.join(ACTIVE_TF)}"

    if base == "/tf":
        if len(cmd) < 3:
            return "⚠️ Используй: /tf on X или /tf off X"
        action, tf = cmd[1], cmd[2]
        if tf not in ALL_TIMEFRAMES:
            return f"⚠️ Неверный ТФ. Доступные: {', '.join(ALL_TIMEFRAMES)}"
        if action == "on":
            if tf not in ACTIVE_TF:
                ACTIVE_TF.append(tf)
                save_settings()
            return f"✅ Таймфрейм {tf} включён\nАктивные: {', '.join(ACTIVE_TF)}"
        elif action == "off":
            if tf in ACTIVE_TF:
                ACTIVE_TF.remove(tf)
                save_settings()
            return f"❌ Таймфрейм {tf} выключен\nАктивные: {', '.join(ACTIVE_TF)}"
        return "⚠️ Используй: /tf on X или /tf off X"

    if base == "/amount":
        if len(cmd) < 2:
            return "⚠️ Укажи число, пример: /amount 50"
        try:
            INVEST_AMOUNT = float(cmd[1])
            save_settings()
            return f"💰 Размер сделки изменён: {INVEST_AMOUNT}$"
        except ValueError:
            return "⚠️ Укажи число, пример: /amount 50"

    if base == "/open":
        if not open_trades:
            return "📂 Нет открытых сделок"
        msg = "📂 Открытые сделки:\n"
        for t in open_trades:
            msg += f"- {t['symbol']} {t['direction']} {t['timeframe']} entry={t['entry_price']}$\n"
        return msg

    if base == "/closed":
        if not closed_trades:
            return "📂 Нет закрытых сделок"
        msg = "📂 Последние сделки:\n"
        for t in closed_trades[-10:]:
            msg += f"- {t['symbol']} {t['direction']} PnL={t['pnl_percent']}% ({t['pnl_cash']}$)\n"
        return msg

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
    t = Thread(target=auto_trading_loop, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
