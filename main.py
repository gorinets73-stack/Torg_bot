import os
import logging
from flask import Flask, request
import requests
import time
import hmac
import hashlib
import base64

# === Режим работы ===
TEST_MODE = True  # 🔴 Оставь True для теста, поставь False для реальной торговли

# === Flask + Логи ===
logging.basicConfig(level=logging.INFO)
app = Flask(__name__)

# === Конфиг ===
BOT_TOKEN = "8432592746:AAEg3uaH-Xa6tf-pqZI_RjiKaRZAQfsHbXM"
CHAT_ID = "1623720732"
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

BITGET_API_KEY = "iiX4VE3pKkwIzN7MW7"
BITGET_API_SECRET = "7eauCqefE7EPSr5p8esMAyWEJczL5i9uhsLL"
BITGET_API_PASSPHRASE = "20220103"
BITGET_BASE_URL = "https://api.bitget.com"

# === Telegram ===
def send_message(chat_id, text):
    url = f"{API_URL}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        logging.error(f"Ошибка отправки сообщения: {e}")

# === Подпись Bitget ===
def sign_request(timestamp, method, request_path, body=""):
    message = str(timestamp) + method + request_path + body
    mac = hmac.new(
        BITGET_API_SECRET.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256
    )
    return base64.b64encode(mac.digest()).decode()

def bitget_headers(method, path, body=""):
    ts = str(int(time.time() * 1000))
    sign = sign_request(ts, method, path, body)
    return {
        "ACCESS-KEY": BITGET_API_KEY,
        "ACCESS-SIGN": sign,
        "ACCESS-TIMESTAMP": ts,
        "ACCESS-PASSPHRASE": BITGET_API_PASSPHRASE,
        "Content-Type": "application/json"
    }

# === Bitget API ===
def get_balance():
    if TEST_MODE:
        return "💰 Тестовый баланс: 100 USDT"
    try:
        url = f"{BITGET_BASE_URL}/api/v2/account/assets?coin=USDT"
        headers = bitget_headers("GET", "/api/v2/account/assets?coin=USDT")
        r = requests.get(url, headers=headers).json()
        if "data" in r and len(r["data"]) > 0:
            balance = r["data"][0]["available"]
            return f"💰 Баланс: {balance} USDT"
        return f"Ошибка получения баланса: {r}"
    except Exception as e:
        return f"Ошибка: {e}"

def get_positions():
    if TEST_MODE:
        return "📊 Тестовые позиции:\nBTCUSDT: +0.01\nETHUSDT: -0.02"
    try:
        url = f"{BITGET_BASE_URL}/api/v2/mix/position/list?productType=umcbl"
        headers = bitget_headers("GET", "/api/v2/mix/position/list?productType=umcbl")
        r = requests.get(url, headers=headers).json()
        if "data" in r:
            positions = []
            for p in r["data"]:
                positions.append(f"{p['symbol']} {p['holdSide']} {p['total']}")
            return "📊 Позиции:\n" + "\n".join(positions) if positions else "Нет открытых позиций"
        return f"Ошибка получения позиций: {r}"
    except Exception as e:
        return f"Ошибка: {e}"

# === Обработка команд ===
def handle_command(command, args):
    if command == "/start":
        return "✅ Бот запущен!\nНапиши /help чтобы увидеть все команды."
    elif command == "/help":
        return (
            "🤖 Доступные команды:\n"
            "/balance - Баланс\n"
            "/positions - Позиции\n"
            "/buy SYMBOL QTY - Купить (тест)\n"
            "/sell SYMBOL QTY - Продать (тест)\n"
            "/close_all - Закрыть все позиции (тест)"
        )
    elif command == "/balance":
        return get_balance()
    elif command == "/positions":
        return get_positions()
    elif command == "/buy":
        if len(args) < 2:
            return "⚠️ Использование: /buy SYMBOL QTY"
        symbol, qty = args[0], args[1]
        return f"🟢 {'Тестовая' if TEST_MODE else 'Реальная'} покупка {qty} {symbol}"
    elif command == "/sell":
        if len(args) < 2:
            return "⚠️ Использование: /sell SYMBOL QTY"
        symbol, qty = args[0], args[1]
        return f"🔴 {'Тестовая' if TEST_MODE else 'Реальная'} продажа {qty} {symbol}"
    elif command == "/close_all":
        return f"❌ {'Тестовые' if TEST_MODE else 'Реальные'} позиции закрыты."
    else:
        return "⚠️ Неизвестная команда. Напиши /help."

# === Webhook ===
@app.route("/", methods=["POST", "GET"])
def webhook():
    if request.method == "POST":
        update = request.json
        logging.info(f"Получено сообщение: {update}")

        if "message" in update and "text" in update["message"]:
            chat_id = update["message"]["chat"]["id"]
            text = update["message"]["text"].strip()
            parts = text.split()
            command = parts[0]
            args = parts[1:]

            reply = handle_command(command, args)
            send_message(chat_id, reply)

        return {"ok": True}
    else:
        return "Бот работает 🚀"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    send_message(CHAT_ID, "🚀 Бот запущен!\nИспользуй /help для списка команд.")
    app.run(host="0.0.0.0", port=port)
