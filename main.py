import time
import math
import random
import requests
import pandas as pd
from pybit.unified_trading import HTTP

# ========= НАСТРОЙКИ =========
TESTNET = True  # демо-счёт. Для реала поставь False
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]  # торгуемые пары
INTERVAL = "5"                  # 5-минутные свечи
LEVERAGE = 10                   # плечо
RISK_PER_TRADE = 0.10           # 10% от депозита в сделку (с плечом)
STOP_LOSS_DROP = 0.10           # SL 10% от входа
TRAIL_STEP_PCT = 0.05           # шаг трейлинга: каждые +5% от входа
TRAIL_BUFFER = 0.02             # отставание стопа на 2% от ближайшей "ступени"
SR_LOOKBACK = 50                # окно для уровней S/R (кол-во свечей)
SLEEP_BETWEEN_SYMBOLS = 3       # сек между монетами
LOOP_PAUSE = 300                # сек между итерациями (5 минут)

# ========= КЛЮЧИ (Bybit) =========
API_KEY = "iiX4VE3pKkwIzN7MW7"
API_SECRET = "7eauCqefE7EPSr5p8esMAyWEJczL5i9uhsLL"

# ========= Telegram (опционально) =========
TELEGRAM_BOT_TOKEN = "8432592746:AAEg3uaH-Xa6tf-pqZI_RjiKaRZAQfsHbXM"
TELEGRAM_CHAT_ID = "1623720732"

def tg(msg: str):
    try:
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            return
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=10
        )
    except Exception:
        pass

# ========= ПРОКСИ (ротация) =========
PROXIES_POOL = [
    {"http": "socks5://207.154.230.54:1080", "https": "socks5://207.154.230.54:1080"},
    {"http": "socks5://185.199.229.156:7492", "https": "socks5://185.199.229.156:7492"},
    {"http": "socks5://51.79.50.22:9300", "https": "socks5://51.79.50.22:9300"},
    {"http": "socks5://146.190.66.23:1080", "https": "socks5://146.190.66.23:1080"},
    {"http": "socks5://165.22.204.32:7492", "https": "socks5://165.22.204.32:7492"},
    {"http": "socks5://68.183.219.54:7497", "https": "socks5://68.183.219.54:7497"},
    {"http": "socks5://134.122.22.242:7492", "https": "socks5://134.122.22.242:7492"},
    {"http": "socks5://64.227.8.166:7492", "https": "socks5://64.227.8.166:7492"},
    {"http": "socks5://159.223.212.204:7497", "https": "socks5://159.223.212.204:7497"},
    {"http": "socks5://206.189.117.108:9300", "https": "socks5://206.189.117.108:9300"},
]

def make_session():
    proxy = random.choice(PROXIES_POOL)
    print(f"[INFO] proxy -> {proxy}")
    return HTTP(
        testnet=TESTNET,
        api_key=API_KEY,
        api_secret=API_SECRET,
        request_timeout=15,
        proxies=proxy
    )

# ========= УТИЛИТЫ =========
def ema(series: pd.Series, period: int):
    return series.ewm(span=period, adjust=False).mean()

def rsi(series: pd.Series, period: int = 14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / (avg_loss.replace(0, pd.NA))
    rsi_val = 100 - (100 / (1 + rs))
    return rsi_val.bfill().fillna(50)

def nearest_qty(qty: float, step: float = 0.001):
    return max(step, round(qty / step) * step)

def get_tick_step(session: HTTP, symbol: str):
    # упрощённо: шаг количества 0.001
    # (для точности можно вытянуть через get_instruments_info и распарсить lotSizeFilter)
    return 0.001

# ========= ДАННЫЕ =========
def get_balance(session: HTTP) -> float:
    data = session.get_wallet_balance(accountType="UNIFIED")
    return float(data["result"]["list"][0]["totalEquity"])

def get_klines(session: HTTP, symbol: str, interval=INTERVAL, limit=200) -> pd.DataFrame:
    data = session.get_kline(category="linear", symbol=symbol, interval=interval, limit=limit)
    rows = data["result"]["list"]
    df = pd.DataFrame(rows, columns=["ts","open","high","low","close","volume","turnover"])
    df = df.iloc[::-1].reset_index(drop=True)
    for c in ["open","high","low","close","volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df

def find_sr(df: pd.DataFrame, lookback=SR_LOOKBACK):
    recent = df.tail(lookback)
    return float(recent["low"].min()), float(recent["high"].max())

# ========= ПОЗИЦИИ / ОРДЕРА =========
def set_leverage(session: HTTP, symbol: str, lev=LEVERAGE):
    session.set_leverage(category="linear", symbol=symbol, buyLeverage=str(lev), sellLeverage=str(lev))

def get_position(session: HTTP, symbol: str):
    data = session.get_positions(category="linear", symbol=symbol)
    arr = data["result"]["list"]
    if not arr:
        return None
    pos = arr[0]
    if float(pos.get("size", 0)) > 0:
        return pos
    return None

def place_market_order(session: HTTP, symbol: str, side: str, qty: float, sl: float, tp: float):
    return session.place_order(
        category="linear",
        symbol=symbol,
        side=side,                 # "Buy" или "Sell"
        orderType="Market",
        qty=str(qty),
        timeInForce="GoodTillCancel",
        reduceOnly=False,
        takeProfit=str(tp),
        stopLoss=str(sl),
        tpTriggerBy="LastPrice",
        slTriggerBy="LastPrice",
        positionIdx=0
    )

def set_trading_stop(session: HTTP, symbol: str, stop_loss: float | None = None, take_profit: float | None = None):
    return session.set_trading_stop(
        category="linear",
        symbol=symbol,
        stopLoss=str(stop_loss) if stop_loss else None,
        takeProfit=str(take_profit) if take_profit else None
    )

# ========= ЛОГИКА =========
def manage_open_position(session: HTTP, symbol: str, last_price: float, support: float, resistance: float):
    """
    Управляем открытой позицией:
    - ступенчатый трейлинг каждые +5% (лонг) / -5% (шорт) от входа
    - закрытие на уровнях S/R (через перенос TP)
    """
    pos = get_position(session, symbol)
    if not pos:
        return

    side = pos["side"]           # "Buy" / "Sell"
    entry = float(pos["avgPrice"])
    size = float(pos["size"])

    # уровень S/R как TP-цели
    if side == "Buy":
        target = resistance
        # ступени: каждые +5% от входа
        gain = (last_price / entry) - 1.0
        steps = math.floor(gain / TRAIL_STEP_PCT) if gain > 0 else 0
        # новый SL = entry * (1 + steps*5% - buffer)
        new_sl = entry * (1 + steps * TRAIL_STEP_PCT - TRAIL_BUFFER) if steps > 0 else entry * (1 - STOP_LOSS_DROP)
        # не опускаем SL ниже текущего (если биржа уже держит выше)
        try:
            current_sl = float(pos.get("stopLoss", 0) or 0)
        except:
            current_sl = 0.0
        if new_sl > current_sl:
            set_trading_stop(session, symbol, stop_loss=new_sl, take_profit=target)
            tg(f"📈 [{symbol}] Лонг: подтянул SL → {new_sl:.4f}, TP → {target:.4f}")

    else:  # Sell (шорт)
        target = support
        drop = 1.0 - (last_price / entry)
        steps = math.floor(drop / TRAIL_STEP_PCT) if drop > 0 else 0
        new_sl = entry * (1 - steps * TRAIL_STEP_PCT + TRAIL_BUFFER) if steps > 0 else entry * (1 + STOP_LOSS_DROP)
        try:
            current_sl = float(pos.get("stopLoss", 0) or 1e18)
        except:
            current_sl = 1e18
        if new_sl < current_sl:
            set_trading_stop(session, symbol, stop_loss=new_sl, take_profit=target)
            tg(f"📉 [{symbol}] Шорт: подтянул SL → {new_sl:.4f}, TP → {target:.4f}")

def try_open_new_position(session: HTTP, symbol: str, balance: float, df: pd.DataFrame, support: float, resistance: float):
    """
    Вход по сигналу:
    - LONG: EMA20 > EMA50 и RSI < 70, SL 10% ниже входа, TP = сопротивление
    - SHORT: EMA20 < EMA50 и RSI > 30, SL 10% выше входа, TP = поддержка
    """
    last = df.iloc[-1]
    price = float(last["close"])
    ema20, ema50, rsi_val = float(last["EMA20"]), float(last["EMA50"]), float(last["RSI"])

    # размер позиции: 10% депо * плечо / цена
    lot_step = get_tick_step(session, symbol)
    exposure_usdt = balance * RISK_PER_TRADE * LEVERAGE
    qty = nearest_qty(exposure_usdt / price, lot_step)

    if qty <= 0:
        return

    # Лонг
    if ema20 > ema50 and rsi_val < 70:
        sl = price * (1 - STOP_LOSS_DROP)
        tp = min(resistance, price * (1 + 3 * TRAIL_STEP_PCT))  # "первый ориентир": до 15% или уровень
        set_leverage(session, symbol, LEVERAGE)
        res = place_market_order(session, symbol, "Buy", qty, sl, tp)
        tg(f"🚀 [{symbol}] LONG qty={qty}  Px={price:.4f}\nSL={sl:.4f}\nTP={tp:.4f}\n{res}")

    # Шорт
    elif ema20 < ema50 and rsi_val > 30:
        sl = price * (1 + STOP_LOSS_DROP)
        tp = max(support, price * (1 - 3 * TRAIL_STEP_PCT))
        set_leverage(session, symbol, LEVERAGE)
        res = place_market_order(session, symbol, "Sell", qty, sl, tp)
        tg(f"🔻 [{symbol}] SHORT qty={qty}  Px={price:.4f}\nSL={sl:.4f}\nTP={tp:.4f}\n{res}")

def run_symbol(session: HTTP, symbol: str, balance_cache: dict):
    # котировки
    df = get_klines(session, symbol, INTERVAL, limit=max(200, SR_LOOKBACK + 20))
    df["EMA20"] = ema(df["close"], 20)
    df["EMA50"] = ema(df["close"], 50)
    df["RSI"] = rsi(df["close"], 14)

    support, resistance = find_sr(df, SR_LOOKBACK)
    last_price = float(df["close"].iloc[-1])

    # если позиция уже есть — менеджим (трейлинг + TP на S/R)
    manage_open_position(session, symbol, last_price, support, resistance)

    # если позиции нет — пробуем войти
    pos = get_position(session, symbol)
    if not pos:
        # баланс читаем не чаще, чем раз в 60 сек (кэш)
        now = time.time()
        bal_val, ts = balance_cache.get("val"), balance_cache.get("ts", 0)
        if bal_val is None or (now - ts) > 60:
            try:
                bal_val = get_balance(session)
                balance_cache["val"] = bal_val
                balance_cache["ts"] = now
            except Exception as e:
                tg(f"⚠️ Баланс недоступен: {e}")
                return
        try_open_new_position(session, symbol, bal_val, df, support, resistance)

# ========= MAIN LOOP =========
def main():
    tg("🤖 Бот запущен (Bybit Testnet). Стратегия: EMA+RSI+S/R, SL 10%, трейлинг +5% шаги.")
    while True:
        try:
            session = make_session()
            balance_cache = {"val": None, "ts": 0}
            for symbol in SYMBOLS:
                try:
                    run_symbol(session, symbol, balance_cache)
                except Exception as se:
                    print(f"[ERR:{symbol}] {se}")
                time.sleep(SLEEP_BETWEEN_SYMBOLS)
        except Exception as e:
            tg(f"❌ Сеанс оборвался: {e}")
        time.sleep(LOOP_PAUSE)

if __name__ == "__main__":
    main()
