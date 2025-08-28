import time
import requests
import pandas as pd
import ta
from pybit.unified_trading import HTTP

# ====== ТВОИ КЛЮЧИ (реальные/демо) ======
API_KEY = "iiX4VE3pKkwIzN7MW7"
API_SECRET = "7eauCqefE7EPSr5p8esMAyWEJczL5i9uhsLL"

# Telegram
TELEGRAM_BOT_TOKEN = "8432592746:AAEg3uaH-Xa6tf-pqZI_RjiKaRZAQfsHbXM"
TELEGRAM_CHAT_ID = "1623720732"

# ====== НАСТРОЙКИ ======
SYMBOLS = ["BTCUSDT", "ETHUSDT"]   # список монет
TIMEFRAME = "5"                    # 5-минутные свечи
LEVERAGE = 10
RISK_PER_TRADE = 0.1               # 10% от депозита
STOP_LOSS_PCT = 0.1                 # стоп 10%
TRAIL_STEP = 0.05                   # каждые 5% двигаем TP
TRAILING_STOP = 0.02                # трейлинг-стоп 2%

# ====== КЛИЕНТ BYBIT ======
session = HTTP(
    testnet=True,  # ставь False для реального рынка
    api_key=API_KEY,
    api_secret=API_SECRET,
)

# ====== ФУНКЦИИ ======
def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg})

def get_balance():
    data = session.get_wallet_balance(accountType="UNIFIED")
    return float(data["result"]["list"][0]["coin"][0]["walletBalance"])

def get_klines(symbol):
    data = session.get_kline(category="linear", symbol=symbol, interval=TIMEFRAME, limit=200)
    df = pd.DataFrame(data["result"]["list"], columns=[
        "timestamp","open","high","low","close","volume","turnover"])
    df = df.iloc[::-1]  # разворачиваем в норм порядок
    df[["open","high","low","close","volume"]] = df[["open","high","low","close","volume"]].astype(float)
    return df

def calc_indicators(df):
    df["EMA20"] = ta.trend.EMAIndicator(df["close"], 20).ema_indicator()
    df["EMA50"] = ta.trend.EMAIndicator(df["close"], 50).ema_indicator()
    df["RSI"] = ta.momentum.RSIIndicator(df["close"], 14).rsi()
    return df

def find_support_resistance(df):
    support = df["low"].rolling(20).min().iloc[-1]
    resistance = df["high"].rolling(20).max().iloc[-1]
    return support, resistance

def get_position(symbol):
    data = session.get_positions(category="linear", symbol=symbol)
    if len(data["result"]["list"]) > 0:
        pos = data["result"]["list"][0]
        if float(pos["size"]) > 0:
            return pos
    return None

def place_order(symbol, side, qty, sl, tp):
    return session.place_order(
        category="linear",
        symbol=symbol,
        side=side,
        orderType="Market",
        qty=str(qty),
        timeInForce="GoodTillCancel",
        reduceOnly=False,
        stopLoss=str(sl),
        takeProfit=str(tp),
        tpTriggerBy="LastPrice",
        slTriggerBy="LastPrice",
    )

def update_stop_loss(symbol, sl):
    return session.set_trading_stop(
        category="linear",
        symbol=symbol,
        stopLoss=str(sl),
    )

# ====== СТРАТЕГИЯ ======
def strategy(symbol):
    balance = get_balance()
    df = get_klines(symbol)
    df = calc_indicators(df)
    last = df.iloc[-1]
    support, resistance = find_support_resistance(df)

    qty = round((balance * RISK_PER_TRADE * LEVERAGE) / last["close"], 3)

    pos = get_position(symbol)

    # === Если позиция уже есть → подтягиваем SL ===
    if pos:
        entry_price = float(pos["avgPrice"])
        current_price = last["close"]
        side = pos["side"]

        if side == "Buy":  # Лонг
            new_sl = current_price * (1 - TRAILING_STOP)
            update_stop_loss(symbol, new_sl)
            send_telegram(f"📈 [{symbol}] Лонг держим. Новый SL={new_sl}")

        elif side == "Sell":  # Шорт
            new_sl = current_price * (1 + TRAILING_STOP)
            update_stop_loss(symbol, new_sl)
            send_telegram(f"📉 [{symbol}] Шорт держим. Новый SL={new_sl}")

        return

    # === Новый вход ===
    if last["EMA20"] > last["EMA50"] and last["RSI"] < 70:
        sl = last["close"] * (1 - STOP_LOSS_PCT)
        tp = min(resistance, last["close"] * (1 + TRAIL_STEP))
        res = place_order(symbol, "Buy", qty, sl, tp)
        send_telegram(f"🚀 [{symbol}] Лонг {qty}\nЦена={last['close']}\nSL={sl}\nTP={tp}\n{res}")

    elif last["EMA20"] < last["EMA50"] and last["RSI"] > 30:
        sl = last["close"] * (1 + STOP_LOSS_PCT)
        tp = max(support, last["close"] * (1 - TRAIL_STEP))
        res = place_order(symbol, "Sell", qty, sl, tp)
        send_telegram(f"🔻 [{symbol}] Шорт {qty}\nЦена={last['close']}\nSL={sl}\nTP={tp}\n{res}")

# ====== ОСНОВНОЙ ЦИКЛ ======
def main():
    while True:
        try:
            for symbol in SYMBOLS:
                strategy(symbol)
            time.sleep(300)  # ждем 5 минут
        except Exception as e:
            send_telegram(f"❌ Ошибка: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()
