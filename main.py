import os
import time
import logging
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

BOT_TOKEN = "8432592746:AAEg3uaH-Xa6tf-pqZI_RjiKaRZAQfsHbXM"
CHAT_ID = "1623720732"
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

# === Логирование ===
logging.basicConfig(level=logging.INFO)

# === Flask ===
app = Flask(__name__)

# === Биржа ===
exchange = ccxt.bitget({
    "enableRateLimit": True,
})
exchange.load_markets()

# === Словарь замен тикеров ===
SYMBOL_ALIASES = {
    "MATIC/USDT": "POL/USDT",       # Polygon → POL
    "XBT/USDT": "BTC/USDT",         # Иногда используют XBT
    "ETH2/USDT": "ETH/USDT",        # ETH2 → ETH
    "BCHABC/USDT": "BCH/USDT",      # BCHABC → BCH
    "BCHSV/USDT": "BSV/USDT",       # BCHSV → BSV
    "DOGECOIN/USDT": "DOGE/USDT",   # DOGECOIN → DOGE
    "SHIB/USDT": "SHIB1000/USDT",   # SHIB → SHIB1000
}

# === Хранилище виртуальных сделок ===
positions = []

# === Отладка: вывод всех USDT пар ===
def debug_symbols():
    usdt_symbols = [s for s in exchange.symbols if "USDT" in s]
    logging.info(f"🔎 Всего найдено {len(usdt_symbols)} рынков с USDT")
    for i, s in enumerate(usdt_symbols[:50], 1):
        logging.info(f"{i}. {s}")
    if len(usdt_symbols) > 50:
        logging.info("... список обрезан ...")

# === Автоподбор символа ===
def get_symbol(symbol: str):
    if symbol in SYMBOL_ALIASES:
        return SYMBOL_ALIASES[symbol]
    if symbol in exchange.symbols:
        return symbol
    candidates = [s for s in exchange.symbols if symbol.replace("/", "").lower() in s.replace("/", "").lower()]
    if candidates:
        return candidates[0]
    raise ValueError(f"❌ Символ {symbol} не найден на Bitget.")

# === Telegram ===
def send_message(chat_id, text):
    url = f"{API_URL}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        logging.error(f"Ошибка отправки в Telegram: {e}")

# === Анализ стратегии ===
def analyze_symbol(symbol):
    try:
        market_symbol = get_symbol(symbol)
        ohlcv = exchange.fetch_ohlcv(market_symbol, timeframe="5m", limit=100)
        df = pd.DataFrame(ohlcv, columns=["time", "open", "high", "low", "close", "volume"])
        df["rsi"] = RSIIndicator(df["close"], window=14).rsi()
        df["sma50"] = SMAIndicator(df["close"], window=50).sma_indicator()

        rsi = df["rsi"].iloc[-1]
        price = df["close"].iloc[-1]
        sma50 = df["sma50"].iloc[-1]

        if rsi < 30 and price > sma50:
            return f"🟢 LONG {market_symbol} {INVEST_AMOUNT}$ (RSI={round(rsi,2)}, цена выше тренда)"
        elif rsi > 70 and price < sma50:
            return f"🔴 SHORT {market_symbol} {INVEST_AMOUNT}$ (RSI={round(rsi,2)}, цена ниже тренда)"
        else:
            return None
    except Exception as e:
        logging.error(f"Ошибка анализа {symbol}: {e}")
        return None

# === Цикл торговли (виртуальный счёт) ===
def trading_loop():
    send_message(CHAT_ID, "🤖 Тестовый трейдинг-бот запущен! (виртуальный счёт)")
    while True:
        for symbol in SYMBOLS:
            signal = analyze_symbol(symbol)
            if signal:
                trade = {
                    "symbol": symbol,
                    "signal": signal,
                    "time": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")
                }
                positions.append(trade)
                send_message(CHAT_ID, f"💼 Виртуальная сделка добавлена:\n{signal}")
        time.sleep(60)

# === Telegram команды ===
def handle_command(command):
    if command == "/start":
        return "✅ Бот работает в ТЕСТОВОМ (виртуальном) режиме!\nКоманды:\n/help - список команд\n/positions - показать виртуальные сделки"
    elif command == "/help":
        return "📌 Доступные команды:\n/start - запуск\n/help - помощь\n/positions - показать виртуальные сделки"
    elif command == "/positions":
        if not positions:
            return "📝 Сделок пока нет."
        text = "📊 Виртуальные сделки:\n"
        for pos in positions[-10:]:  # последние 10 сделок
            text += f"{pos['time']} | {pos['signal']}\n"
        return text
    else:
        return "⚠️ Неизвестная команда. Напиши /help."

@app.route("/", methods=["POST", "GET"])
def webhook():
    if request.method == "POST":
        update = request.json
        if "message" in update and "text" in update["message"]:
            chat_id = update["message"]["chat"]["id"]
            text = update["message"]["text"].strip()
            reply = handle_command(text)
            send_message(chat_id, reply)
        return {"ok": True}
    return "Бот работает 🚀"

# === Запуск ===
if __name__ == "__main__":
    debug_symbols()
    Thread(target=trading_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
