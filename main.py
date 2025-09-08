# main.py

from flask import Flask, request
import requests
import hmac
import hashlib
import time
import json
import threading

app = Flask(__name__)

# ==== Bybit API (Demo) ====
API_KEY = "iiX4VE3pKKwIzN7MW7"
API_SECRET = "7eauCqefE7EPSr5p8esMAyWEJczL5i9uhsLL"
BASE_URL = "https://api-demo.bybit.com"

# ==== Telegram Bot ====
TELEGRAM_TOKEN = "8432592746:AAEg3uaH-Xa6tf-pqZl_RjiKaRZAQfsHbXM"
TELEGRAM_CHAT_ID = "1623720732"

# ==== Популярные монеты ====
POPULAR_COINS = [
    "USDT", "BTC", "ETH", "BNB", "SOL", "XRP", "ADA",
    "DOGE", "DOT", "TRX", "MATIC", "SHIB", "LTC", "AVAX"
]


# ==================== Telegram ====================
def send_telegram_message(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    try:
        requests.post(url, data=payload)
    except Exception as e:
        print("Ошибка отправки в Telegram:", e)


# ==================== Bybit ====================
def sign_params(params: dict) -> dict:
    """Создание подписи запроса"""
    param_str = "&".join([f"{k}={v}" for k, v in sorted(params.items())])
    sign = hmac.new(API_SECRET.encode("utf-8"), param_str.encode("utf-8"), hashlib.sha256).hexdigest()
    params["sign"] = sign
    return params


def get_balance_all():
    """Баланс по всем монетам"""
    endpoint = "/v5/account/wallet-balance"
    url = BASE_URL + endpoint
    timestamp = str(int(time.time() * 1000))

    params = {"api_key": API_KEY, "timestamp": timestamp}
    params = sign_params(params)

    r = requests.get(url, params=params).json()
    result = {}

    try:
        coins = r["result"]["list"][0]["coin"]
        for c in coins:
            if c["coin"] in POPULAR_COINS:
                result[c["coin"]] = c["walletBalance"]
    except Exception as e:
        result["error"] = str(e)

    return result


def place_order(symbol="BTCUSDT", side="Buy", qty=0.001, orderType="Market"):
    """Создание ордера"""
    endpoint = "/v5/order/create"
    url = BASE_URL + endpoint
    timestamp = str(int(time.time() * 1000))

    params = {
        "api_key": API_KEY,
        "symbol": symbol,
        "side": side,
        "orderType": orderType,
        "qty": qty,
        "timestamp": timestamp
    }
    params = sign_params(params)

    headers = {"Content-Type": "application/json"}
    r = requests.post(url, headers=headers, data=json.dumps(params))
    return r.json()


def get_positions():
    """Получение открытых позиций"""
    endpoint = "/v5/position/list"
    url = BASE_URL + endpoint
    timestamp = str(int(time.time() * 1000))

    params = {
        "api_key": API_KEY,
        "timestamp": timestamp
    }
    params = sign_params(params)

    r = requests.get(url, params=params).json()
    positions = []

    try:
        for p in r["result"]["list"]:
            if float(p["size"]) > 0:
                positions.append(f"{p['symbol']} {p['side']} {p['size']} @ {p['entryPrice']}")
    except Exception as e:
        positions.append(f"Ошибка: {e}")

    return positions if positions else ["Нет открытых позиций"]


# ==================== Автоматическая проверка баланса ====================
def auto_balance_checker():
    """Каждые 5 минут проверка баланса и отправка в Telegram"""
    while True:
        balances = get_balance_all()
        text = "⏰ Авто-проверка балансов:\n" + "\n".join([f"{k}: {v}" for k, v in balances.items()])
        send_telegram_message(text)
        time.sleep(300)  # 5 минут


# ==================== Flask Routes ====================
@app.route("/")
def home():
    return "🚀 Бот запущен!"


@app.route("/send_test")
def send_test():
    send_telegram_message("✅ Бот работает и ключи подключены!")
    return "Сообщение отправлено в Telegram"


@app.route("/balance")
def balance():
    balances = get_balance_all()
    text = "💰 Балансы:\n" + "\n".join([f"{k}: {v}" for k, v in balances.items()])
    send_telegram_message(text)
    return balances


@app.route("/buy")
def buy():
    symbol = request.args.get("symbol", "BTCUSDT")
    qty = float(request.args.get("qty", 0.001))
    result = place_order(symbol=symbol, side="Buy", qty=qty)
    send_telegram_message(f"🟢 Buy ордер {symbol}: {result}")
    return result


@app.route("/sell")
def sell():
    symbol = request.args.get("symbol", "BTCUSDT")
    qty = float(request.args.get("qty", 0.001))
    result = place_order(symbol=symbol, side="Sell", qty=qty)
    send_telegram_message(f"🔴 Sell ордер {symbol}: {result}")
    return result


@app.route("/positions")
def positions():
    pos = get_positions()
    text = "📊 Открытые позиции:\n" + "\n".join(pos)
    send_telegram_message(text)
    return {"positions": pos}


# ==================== Запуск ====================
if __name__ == "__main__":
    # Запускаем фоновый поток с авто-проверкой
    threading.Thread(target=auto_balance_checker, daemon=True).start()

    app.run(host="0.0.0.0", port=10000)
