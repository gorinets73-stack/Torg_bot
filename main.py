# main.py

from flask import Flask
import requests
import hmac
import hashlib
import time

app = Flask(__name__)

# ==== Bybit API (Demo) ====
API_KEY = "iiX4VE3pKKwIzN7MW7"
API_SECRET = "7eauCqefE7EPSr5p8esMAyWEJczL5i9uhsLL"
BASE_URL = "https://api-demo.bybit.com"

# ==== Telegram Bot ====
TELEGRAM_TOKEN = "8432592746:AAEg3uaH-Xa6tf-pqZl_RjiKaRZAQfsHbXM"
TELEGRAM_CHAT_ID = "1623720732"


@app.route("/")
def home():
    return "Бот запущен!"


@app.route("/send_test")
def send_test():
    """Отправка тестового сообщения в Telegram"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": "✅ Бот работает и ключи подключены!"}
    r = requests.post(url, data=payload)
    return f"Статус: {r.status_code}, ответ: {r.text}"


@app.route("/balance")
def balance():
    """Запрос баланса на Bybit"""
    endpoint = "/v5/account/wallet-balance"
    url = BASE_URL + endpoint
    timestamp = str(int(time.time() * 1000))

    params = f"api_key={API_KEY}&timestamp={timestamp}"
    sign = hmac.new(API_SECRET.encode("utf-8"), params.encode("utf-8"), hashlib.sha256).hexdigest()

    headers = {
        "Content-Type": "application/json"
    }

    r = requests.get(url, params={
        "api_key": API_KEY,
        "timestamp": timestamp,
        "sign": sign
    }, headers=headers)

    return r.text


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
