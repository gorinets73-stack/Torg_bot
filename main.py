import os
import time
import pandas as pd
import numpy as np
from flask import Flask
from pybit.unified_trading import HTTP
import ta  # для индикаторов

# ================== Flask (для Render) ==================
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Trading Bot работает (RSI + EMA + Levels + TP/SL)"

# ================== Торговый бот ==================
def get_klines(session, symbol="BTCUSDT", interval="15"):
    """Загрузка свечей"""
    try:
        data = session.get_kline(
            category="linear",
            symbol=symbol,
            interval=interval,
            limit=200
        )
        df = pd.DataFrame(data["result"]["list"], columns=[
            "timestamp", "open", "high", "low", "close", "volume", "_1", "_2", "_3"
        ])
        df = df[["timestamp", "open", "high", "low", "close", "volume"]]
        df = df.astype(float)
        return df
    except Exception as e:
        print("❌ Ошибка загрузки свечей:", e)
        return None

def add_indicators(df):
    """Добавляем RSI и EMA"""
    df["ema50"] = ta.trend.EMAIndicator(df["close"], window=50).ema_indicator()
    df["ema200"] = ta.trend.EMAIndicator(df["close"], window=200).ema_indicator()
    df["rsi"] = ta.momentum.RSIIndicator(df["close"], window=14).rsi()
    return df

def detect_levels(df, lookback=20, percent=0.02):
    """Определяем уровни поддержки/сопротивления"""
    levels = []
    for i in range(lookback, len(df)-lookback):
        high = df["high"].iloc[i]
        low = df["low"].iloc[i]
        if high == max(df["high"].iloc[i-lookback:i+lookback]):
            levels.append((i, high))
        if low == min(df["low"].iloc[i-lookback:i+lookback]):
            levels.append((i, low))
    return levels

def trading_strategy(session, symbol="BTCUSDT", order_value_usdt=10, leverage=10):
    df = get_klines(session, symbol)
    if df is None or df.empty:
        return

    df = add_indicators(df)
    last = df.iloc[-1]

    trend_up = last["ema50"] > last["ema200"]
    trend_down = last["ema50"] < last["ema200"]

    # Текущая цена
    price = last["close"]

    # Кол-во монет для сделки
    qty = round(order_value_usdt / price, 4)

    # ===== Логика входа =====
    signal = None
    if trend_up and last["rsi"] < 30:
        signal = "Buy"
    elif trend_down and last["rsi"] > 70:
        signal = "Sell"

    # ===== Уровни =====
    levels = detect_levels(df)
    if levels:
        _, lvl_price = levels[-1]
        if price > lvl_price * 1.01:
            signal = "Buy"
        elif price < lvl_price * 0.99:
            signal = "Sell"

    if signal:
        print(f"📊 Сигнал: {signal} {symbol} по цене {price}")

        # Закрываем старые позиции
        try:
            pos = session.get_positions(category="linear", symbol=symbol)
            position_data = pos.get("result", {}).get("list", [])
            for p in position_data:
                if float(p.get("size", 0)) > 0:
                    print(f"❌ Закрываю старую позицию: {p}")
                    session.place_order(
                        category="linear",
                        symbol=symbol,
                        side="Sell" if p["side"] == "Buy" else "Buy",
                        orderType="Market",
                        qty=p["size"],
                        reduceOnly=True
                    )
        except Exception as e:
            print("Ошибка закрытия позиции:", e)

        # Открываем сделку с TP/SL
        try:
            tp_price = price * (1.05 if signal == "Buy" else 0.95)
            sl_price = price * (0.98 if signal == "Buy" else 1.02)

            order = session.place_order(
                category="linear",
                symbol=symbol,
                side=signal,
                orderType="Market",
                qty=qty,
                timeInForce="GoodTillCancel",
                reduceOnly=False,
                takeProfit=round(tp_price, 2),
                stopLoss=round(sl_price, 2)
            )
            print("✅ Сделка открыта:", order)
        except Exception as e:
            print("❌ Ошибка при открытии сделки:", e)
    else:
        print(f"⏳ Сигнала по {symbol} нет")

# ================== Главный цикл ==================
def main():
    api_key = os.getenv("BYBIT_API_KEY")
    api_secret = os.getenv("BYBIT_API_SECRET")

    if not api_key or not api_secret:
        print("❌ Нет API ключей")
        return

    session = HTTP(testnet=True, api_key=api_key, api_secret=api_secret)

    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]

    while True:
        for s in symbols:
            trading_strategy(session, symbol=s)
        time.sleep(60)

# ================== Запуск ==================
if __name__ == "__main__":
    import threading
    threading.Thread(target=main, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
