import os
import time
import hmac
import hashlib
import requests
import json
from flask import Flask, request
import threading
from config import config

# ==== НАСТРОЙКИ ====
API_KEY = config["API_KEY"]
API_SECRET = config["API_SECRET"]
BASE_URL = config["BASE_URL"]

TELEGRAM_TOKEN = config["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = config["TELEGRAM_CHAT_ID"]

app = Flask(__name__)

# ==== Подпись для Bybit ====
def sign_params(params):
    sorted_params = sorted(params.items())
    query = "&".join([f"{k}={v}" for k, v in sorted_params])
    signature = hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    params["sign"] = signature
    return params

# ==== Отправка в Телеграм ====
def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text})
    except Exception as e:
        print(f"Ошибка отправки в Telegram: {e}")

# ==== Баланс ====
def get_balance():
    endpoint = "/v5/account/wallet-balance"
    url = BASE_URL + endpoint
    timestamp = str(int(time.time() * 1000))

    params = {"api_key": API_KEY, "accountType": "UNIFIED", "timestamp": timestamp}
    params = sign_params(params)

    r = requests.get(url, params=params)
    try:
        data = r.json()
    except:
        data = {"error": "Неверный ответ", "raw": r.text}

    send_telegram_message(f"📩 RAW balance response:\n{json.dumps(data, indent=2)}")

    balances = []
    try:
        for coin in data["result"]["list"][0]["coin"]:
            balances.append(f"{coin['coin']}: {coin['walletBalance']}")
    except Exception as e:
        balances.append(f"Ошибка: {e}")
    return balances

# ==== Позиции ====
def get_positions():
    endpoint = "/v5/position/list"
    url = BASE_URL + endpoint
    timestamp = str(int(time.time() * 1000))

    params = {"api_key": API_KEY, "accountType": "UNIFIED", "timestamp": timestamp}
    params = sign_params(params)

    r = requests.get(url, params=params)
    try:
        data = r.json()
    except:
        data = {"error": "Неверный ответ", "raw": r.text}

    send_telegram_message(f"📩 RAW positions response:\n{json.dumps(data, indent=2)}")

    positions = []
    try:
        for p in data["result"]["list"]:
            if float(p["size"]) > 0:
                positions.append(f"{p['symbol']} {p['side']} {p['size']} @ {p['entryPrice']}")
    except Exception as e:
        positions.append(f"Ошибка: {e}")
    return positions if positions else ["Нет открытых позиций"]

# ==== Ордера ====
def place_order(symbol, side, qty):
    endpoint = "/v5/order/create"
    url = BASE_URL + endpoint
    timestamp = str(int(time.time() * 1000))

    params = {
        "api_key": API_KEY,
        "symbol": symbol,
        "side": side,
        "orderType": "Market",
        "qty": qty,
        "timeInForce": "GoodTillCancel",
        "accountType": "UNIFIED",
        "timestamp": timestamp,
    }
    params = sign_params(params)

    r = requests.post(url, data=params)
    try:
        data = r.json()
    except:
        data = {"error": "Неверный ответ", "raw": r.text}

    send_telegram_message(f"📩 RAW order response:\n{json.dumps(data, indent=2)}")
    return data

# ==== Закрыть все позиции ====
def close_all_positions():
    pos = get_positions()
    closed = []
    for p in pos:
        if "@" in p:  # есть позиция
            parts = p.split()
            symbol, side, qty = parts[0], parts[1], parts[2]
            close_side = "Sell" if side == "Buy" else "Buy"
            resp = place_order(symbol, close_side, qty)
            closed.append(f"Закрыл {symbol} {qty}")
    return closed if closed else ["Нет позиций для закрытия"]

# ==== Telegram Webhook ====
@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def telegram_webhook():
    update = request.get_json()
    if "message" in update and "text" in update["message"]:
        chat_id = str(update["message"]["chat"]["id"])
        text = update["message"]["text"]

        if chat_id != TELEGRAM_CHAT_ID:
            return "Not allowed", 200

        if text == "/start":
            commands = (
                "🤖 Доступные команды:\n"
                "/balance - Баланс\n"
                "/positions - Позиции\n"
                "/buy SYMBOL QTY - Купить\n"
                "/sell SYMBOL QTY - Продать\n"
                "/close_all - Закрыть все позиции"
            )
            send_telegram_message(commands)

        elif text == "/balance":
            balances = get_balance()
            send_telegram_message("\n".join(balances))

        elif text == "/positions":
            positions = get_positions()
            send_telegram_message("\n".join(positions))

        elif text.startswith("/buy"):
            parts = text.split()
            if len(parts) == 3:
                _, symbol, qty = parts
                resp = place_order(symbol, "Buy", qty)
                send_telegram_message(json.dumps(resp, indent=2))
            else:
                send_telegram_message("Формат: /buy SYMBOL QTY")

        elif text.startswith("/sell"):
            parts = text.split()
            if len(parts) == 3:
                _, symbol, qty = parts
                resp = place_order(symbol, "Sell", qty)
                send_telegram_message(json.dumps(resp, indent=2))
            else:
                send_telegram_message("Формат: /sell SYMBOL QTY")

        elif text == "/close_all":
            result = close_all_positions()
            send_telegram_message("\n".join(result))

    return "OK", 200

# ==== Flask главная ====
@app.route("/")
def home():
    return "Бот работает!"

# ==== При запуске сразу сообщение в Telegram ====
def startup_message():
    time.sleep(2)
    send_telegram_message(
        "✅ Бот запущен!\n\n"
        "🤖 Доступные команды:\n"
        "/balance - Баланс\n"
        "/positions - Позиции\n"
        "/buy SYMBOL QTY - Купить\n"
        "/sell SYMBOL QTY - Продать\n"
        "/close_all - Закрыть все позиции"
    )

if __name__ == "__main__":
    threading.Thread(target=startup_message).start()
    app.run(host="0.0.0.0", port=10000)
