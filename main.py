import time
import math
import requests
import pandas as pd
import numpy as np
from pybit.unified_trading import HTTP

# ========= НАСТРОЙКИ / КЛЮЧИ =========
BYBIT_API_KEY = "iiX4VE3pKkwIzN7MW7"
BYBIT_API_SECRET = "7eauCqefE7EPSr5p8esMAyWEJczL5i9uhsLL"
TESTNET = True  # DEMO-счёт

TELEGRAM_BOT_TOKEN = "8432592746:AAEg3uaH-Xa6tf-pqZI_RjiKaRZAQfsHbXM"
TELEGRAM_CHAT_ID = "1623720732"

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]   # какие фьючи мониторим
INTERVAL = "5"                                 # 5m
LIMIT = 300                                    # свечей для индикаторов/уровней
EMA_PERIOD = 200
RSI_PERIOD = 14
LEVEL_WINDOW = 20
NEAR_LEVEL_PCT = 0.02                          # близость к уровню ±2%
RISK_SHARE = 0.10                               # 10% депозита на вход
LEVERAGE = 10
STOP_LOSS_PCT = 0.10                            # -10% от входа
TRAIL_STEP = 0.05                               # шаг трейлинга 5%

CHECK_INTERVAL_SEC = 30                         # цикл каждые 30 сек

# Храним состояние трейлинга по открытой позиции
state = {}  # { symbol: {"entry": float, "side": "Buy"/"Sell", "step": int} }

# ========= ВСПОМОГАТЕЛЬНОЕ =========
def tg(text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=10
        )
    except Exception as e:
        print("TG error:", e)

session = HTTP(api_key=BYBIT_API_KEY, api_secret=BYBIT_API_SECRET, testnet=TESTNET)

def get_wallet_usdt():
    res = session.get_wallet_balance(accountType="UNIFIED")
    coins = res["result"]["list"][0]["coin"]
    usdt = next((c for c in coins if c["coin"] == "USDT"), None)
    return float(usdt["walletBalance"]) if usdt else 0.0

def get_last_price(symbol: str) -> float:
    t = session.get_tickers(category="linear", symbol=symbol)
    return float(t["result"]["list"][0]["lastPrice"])

def get_klines(symbol: str, interval=INTERVAL, limit=LIMIT) -> pd.DataFrame:
    data = session.get_kline(category="linear", symbol=symbol, interval=interval, limit=limit)
    cols = ["start","open","high","low","close","volume","turnover"]
    df = pd.DataFrame(data["result"]["list"], columns=cols)
    for c in ["open","high","low","close","volume","turnover"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.iloc[::-1].reset_index(drop=True)
    return df

def ema(series: pd.Series, period=EMA_PERIOD):
    return series.ewm(span=period, adjust=False).mean()

def rsi(series: pd.Series, period=RSI_PERIOD):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.bfill().fillna(50)

def support_resistance(df: pd.DataFrame, window=LEVEL_WINDOW):
    sup = df["low"].rolling(window).min()
    res = df["high"].rolling(window).max()
    return sup.iloc[-1], res.iloc[-1]

def set_leverage(symbol: str, leverage=LEVERAGE):
    try:
        session.set_leverage(category="linear", symbol=symbol,
                             buyLeverage=str(leverage), sellLeverage=str(leverage))
    except Exception as e:
        tg(f"⚠️ set_leverage {symbol}: {e}")

def qty_from_usdt(usdt: float, price: float, step=0.001):
    if price <= 0:
        return 0.0
    raw = usdt / price
    return max(step, round(raw / step) * step)

def close_position_market(symbol: str, side: str, qty: float):
    # side — текущее направление позиции; для закрытия отправляем обратную сторону
    exit_side = "Sell" if side == "Buy" else "Buy"
    try:
        session.place_order(category="linear", symbol=symbol,
                            side=exit_side, orderType="Market", qty=str(qty))
        tg(f"✅ Закрытие {symbol} {exit_side} {qty}")
    except Exception as e:
        tg(f"❌ Ошибка закрытия {symbol}: {e}")

def position_info(symbol: str):
    """Возвращает (side, size, avgPrice) или (None, 0, 0.0) если нет позиции"""
    try:
        res = session.get_positions(category="linear", symbol=symbol)
        lst = res["result"]["list"]
        if not lst:
            return None, 0.0, 0.0
        p = lst[0]
        side = "Buy" if float(p["size"]) > 0 and p["side"] == "Buy" else ("Sell" if float(p["size"]) > 0 and p["side"] == "Sell" else None)
        size = float(p["size"])
        entry = float(p["avgPrice"]) if p["avgPrice"] not in (None, "", "0") else 0.0
        return side, size, entry
    except Exception as e:
        tg(f"⚠️ position_info {symbol}: {e}")
        return None, 0.0, 0.0

def update_trailing(symbol: str, last_price: float):
    """Ступенчатый трейлинг по 5%: когда цена >= entry*(1+0.05*n), стоп = entry*(1+0.05*(n-1))"""
    if symbol not in state:
        return
    entry = state[symbol]["entry"]
    side = state[symbol]["side"]
    step = state[symbol]["step"]  # сколько «ступеней» уже пройдено

    if entry <= 0:
        return

    # текущая доходность от входа
    change = (last_price / entry - 1.0) if side == "Buy" else (entry / last_price - 1.0)

    # Сколько 5%-ступеней пройдено сейчас
    current_steps = int(math.floor(change / TRAIL_STEP))
    if current_steps <= step:
        return  # ещё не дошли до следующей ступени

    # Нужно повысить стоп
    new_step = current_steps
    # Новый стоп устанавливаем на уровень предыдущей ступени (на одну ниже текущей)
    protect_gain = max(0, (new_step - 1) * TRAIL_STEP)  # 0.00, 0.05, 0.10, ...
    if side == "Buy":
        new_sl = entry * (1 - STOP_LOSS_PCT)  # базовый
        # подтягиваем вверх
        new_sl = max(new_sl, entry * (1 + protect_gain))
    else:
        new_sl = entry * (1 + STOP_LOSS_PCT)
        new_sl = min(new_sl, entry * (1 - protect_gain))

    try:
        if side == "Buy":
            session.set_trading_stop(category="linear", symbol=symbol,
                                     stopLoss=str(round(new_sl, 4)), slTriggerBy="LastPrice")
        else:
            session.set_trading_stop(category="linear", symbol=symbol,
                                     stopLoss=str(round(new_sl, 4)), slTriggerBy="LastPrice")
        state[symbol]["step"] = new_step
        tg(f"🔄 Трейлинг {symbol}: шаг {new_step}, новый SL = {round(new_sl, 4)}")
    except Exception as e:
        tg(f"⚠️ set_trading_stop {symbol}: {e}")

def try_enter(symbol: str):
    # Уже в позиции?
    cur_side, cur_size, _ = position_info(symbol)
    if cur_size > 0:
        return  # уже в сделке — только управление

    df = get_klines(symbol)
    df["EMA200"] = ema(df["close"], EMA_PERIOD)
    df["RSI"] = rsi(df["close"], RSI_PERIOD)
    support, resistance = support_resistance(df, LEVEL_WINDOW)
    price = df["close"].iloc[-1]

    near_support = abs(price - support) / price <= NEAR_LEVEL_PCT
    near_resist = abs(price - resistance) / price <= NEAR_LEVEL_PCT

    # Сигналы
    long_signal = (df["RSI"].iloc[-1] < 30) and (price > df["EMA200"].iloc[-1]) and near_support
    short_signal = (df["RSI"].iloc[-1] > 70) and (price < df["EMA200"].iloc[-1]) and near_resist

    if not (long_signal or short_signal):
        return

    # Размер позиции ≈ 10% депозита (в USDT), конверсия в количество контрактов
    balance = get_wallet_usdt()
    exposure_usdt = balance * RISK_SHARE
    qty = qty_from_usdt(exposure_usdt, price)

    if qty <= 0:
        return

    set_leverage(symbol, LEVERAGE)
    side = "Buy" if long_signal else "Sell"

    try:
        order = session.place_order(category="linear", symbol=symbol,
                                    side=side, orderType="Market", qty=str(qty))
        # Получаем актуальную позицию/вход
        _, size, entry = position_info(symbol)
        if size <= 0 or entry <= 0:
            tg(f"⚠️ Не смогли получить позицию после входа {symbol}")
            return

        # Базовый SL -10%
        if side == "Buy":
            sl = round(entry * (1 - STOP_LOSS_PCT), 4)
            tp = round(min(resistance, entry * (1 + TRAIL_STEP)), 4)  # первичный TP: уровень или +5%
        else:
            sl = round(entry * (1 + STOP_LOSS_PCT), 4)
            tp = round(max(support, entry * (1 - TRAIL_STEP)), 4)

        session.set_trading_stop(category="linear", symbol=symbol,
                                 stopLoss=str(sl), takeProfit=str(tp),
                                 slTriggerBy="LastPrice", tpTriggerBy="LastPrice")

        state[symbol] = {"entry": entry, "side": side, "step": 0}
        tg(f"✅ Вход {symbol} {side} @ {entry}\nSL: {sl}\nTP: {tp}\nБаланс: {balance:.2f} USDT, QTY: {qty}")
    except Exception as e:
        tg(f"❌ Ошибка входа {symbol}: {e}")

def manage_position(symbol: str):
    side, size, entry = position_info(symbol)
    if size <= 0:
        if symbol in state:
            del state[symbol]
        return

    price = get_last_price(symbol)

    # Ступенчатый трейлинг
    update_trailing(symbol, price)

    # Закрытие на уровне
    df = get_klines(symbol, limit=LEVEL_WINDOW + 5)
    support, resistance = support_resistance(df, LEVEL_WINDOW)

    # допуск уровня 0.1%
    tol = 0.001

    if side == "Buy" and price >= resistance * (1 - tol):
        close_position_market(symbol, side, size)
        tg(f"🎯 TP по уровню {symbol}: цена {price} ≈ resistance {resistance}")
        if symbol in state: del state[symbol]
        return

    if side == "Sell" and price <= support * (1 + tol):
        close_position_market(symbol, side, size)
        tg(f"🎯 TP по уровню {symbol}: цена {price} ≈ support {support}")
        if symbol in state: del state[symbol]
        return

def main_loop():
    tg("🤖 Старт бота Bybit DEMO: RSI+EMA200+Уровни, 5m, вход 10% депо, SL -10%, трейлинг +5% шагами.")
    while True:
        try:
            for sym in SYMBOLS:
                try_enter(sym)      # вход, если есть сигнал
                manage_position(sym)  # управление открытой позицией
            time.sleep(CHECK_INTERVAL_SEC)
        except Exception as e:
            tg(f"⚠️ Ошибка цикла: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main_loop()
