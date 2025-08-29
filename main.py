import time
import math
import itertools
import random
import pandas as pd
from pybit.unified_trading import HTTP
from config import config

# =========================
# НАСТРОЙКИ
# =========================
TESTNET = True                          # демо-счёт Bybit
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]  # можно расширять
INTERVAL = "5"                          # 5m свечи
LEVERAGE = 10                           # плечо
TRADE_SHARE = 0.10                      # 10% от депозита в одну сделку
STOP_LOSS_PCT = 0.10                    # SL 10% от входа
TRAIL_STEP_PCT = 0.05                   # ступени трейлинга: каждые +5% (лонг) / -5% (шорт)
TRAIL_BUFFER = 0.02                     # отступ для SL от ступени (2%)
SR_LOOKBACK = 60                        # окно для уровней поддержки/сопротивления (по свечам)
LOOP_DELAY = 60                         # пауза между проходами (сек)
PAUSE_BETWEEN_SYMBOLS = 2               # пауза между монетами (сек)

# =========================
# КЛЮЧИ BYBIT (берём из config.py)
# =========================
API_KEY = config["API_KEY"]
API_SECRET = config["API_SECRET"]

# =========================
# ПРОКСИ (авторотация при любой сетевой ошибке)
# =========================
PROXIES = [
    "http://103.172.70.121:8080",
    "http://47.243.177.210:8080",
    "http://185.105.237.92:8080",
    "http://51.159.66.158:3128",
    "http://8.219.97.248:80",
]
_proxy_cycle = itertools.cycle(PROXIES)

def _make_session(proxy: str) -> HTTP:
    return HTTP(
        testnet=TESTNET,
        api_key=API_KEY,
        api_secret=API_SECRET,
        timeout=15,  # ВАЖНО: правильный параметр (не request_timeout)
        proxies={"http": proxy, "https": proxy}
    )

def _get_session() -> HTTP:
    """Подбираем рабочий прокси и возвращаем сессию (тестовый вызов — баланс)."""
    while True:
        proxy = next(_proxy_cycle)
        try:
            s = _make_session(proxy)
            ping = s.get_wallet_balance(accountType="UNIFIED")
            if "result" in ping:
                print(f"✅ Сессия установлена через {proxy}")
                return s
        except Exception as e:
            print(f"❌ Прокси {proxy} не подошёл: {e}")
            time.sleep(2)

# глобальная сессия
session = _get_session()

def safe_call(func, *args, **kwargs):
    """Вызывает метод API; при ошибке — меняет прокси и пробует снова."""
    global session
    while True:
        try:
            return func(*args, **kwargs)
        except Exception as e:
            print(f"⚠️ Ошибка запроса: {e} → ротация прокси")
            session = _get_session()
            time.sleep(2)

# =========================
# ИНДИКАТОРЫ
# =========================
def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    out = 100 - (100 / (1 + rs))
    return out.bfill().fillna(50)

# =========================
# МАРКЕТ ДАННЫЕ
# =========================
def get_klines(symbol: str, interval=INTERVAL, limit=max(200, SR_LOOKBACK + 20)) -> pd.DataFrame:
    """Тянем 5m свечи линейных фьючерсов (USDT-Perp)."""
    k = safe_call(session.get_kline, category="linear", symbol=symbol, interval=interval, limit=limit)
    rows = k["result"]["list"]  # список списков: [ts, open, high, low, close, volume, turnover]
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume", "turnover"])
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.iloc[::-1].reset_index(drop=True)  # в хронологию
    return df

def get_balance() -> float:
    """Total Equity аккаунта (UNIFIED)."""
    b = safe_call(session.get_wallet_balance, accountType="UNIFIED")
    return float(b["result"]["list"][0]["totalEquity"])

def get_instrument_filters(symbol: str):
    """Размер шага/минималки лота из спецификаций инструмента."""
    info = safe_call(session.get_instruments_info, category="linear", symbol=symbol)
    item = info["result"]["list"][0]
    lot = item["lotSizeFilter"]
    min_qty = float(lot["minOrderQty"])
    qty_step = float(lot["qtyStep"])
    return min_qty, qty_step

def round_qty(qty: float, step: float, min_qty: float) -> float:
    rounded = round(qty / step) * step
    # форматируем, чтобы не потерять точность при очень мелких шагах
    rounded = float(f"{rounded:.10f}")
    return max(min_qty, rounded)

def calc_sr(df: pd.DataFrame, lookback=SR_LOOKBACK):
    """Уровни поддержки/сопротивления по минимуму/максимуму окна."""
    recent = df.tail(lookback)
    support = float(recent["low"].min())
    resistance = float(recent["high"].max())
    return support, resistance

# =========================
# ПОЗИЦИИ / ОРДЕРА
# =========================
def set_leverage(symbol: str, lev=LEVERAGE):
    safe_call(session.set_leverage, category="linear", symbol=symbol, buyLeverage=str(lev), sellLeverage=str(lev))

def get_position(symbol: str):
    data = safe_call(session.get_positions, category="linear", symbol=symbol)
    arr = data["result"]["list"]
    if not arr:
        return None
    pos = arr[0]
    if float(pos.get("size", 0)) > 0:
        return pos
    return None

def place_market(symbol: str, side: str, qty: float, sl: float, tp: float):
    """Открытие Market с одновременным TP/SL."""
    return safe_call(
        session.place_order,
        category="linear",
        symbol=symbol,
        side=side,                 # "Buy" / "Sell"
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

def update_trading_stop(symbol: str, stop_loss: float | None, take_profit: float | None):
    """Обновление стопа/тейка у открытой позиции."""
    return safe_call(
        session.set_trading_stop,
        category="linear",
        symbol=symbol,
        stopLoss=str(stop_loss) if stop_loss else None,
        takeProfit=str(take_profit) if take_profit else None,
        tpTriggerBy="LastPrice",
        slTriggerBy="LastPrice",
        positionIdx=0
    )

# =========================
# СИГНАЛЫ / ЛОГИКА
# =========================
def signal(df: pd.DataFrame):
    """'Buy' / 'Sell' / None по EMA20/EMA50 + фильтр RSI."""
    df["EMA20"] = ema(df["close"], 20)
    df["EMA50"] = ema(df["close"], 50)
    df["RSI"] = rsi(df["close"], 14)

    last = df.iloc[-1]
    if pd.isna(last["EMA20"]) or pd.isna(last["EMA50"]):
        return None

    # LONG: EMA20 > EMA50 и RSI < 70 (без перекупленности)
    if last["EMA20"] > last["EMA50"] and last["RSI"] < 70:
        return "Buy"

    # SHORT: EMA20 < EMA50 и RSI > 30 (без перепроданности)
    if last["EMA20"] < last["EMA50"] and last["RSI"] > 30:
        return "Sell"

    return None

def try_open(symbol: str, balance: float, df: pd.DataFrame, support: float, resistance: float):
    """Открытие позиции: 10% депозита * 10x, SL 10%, TP по S/R (или ориентир ±15%)."""
    price = float(df["close"].iloc[-1])
    min_qty, step = get_instrument_filters(symbol)

    exposure = balance * TRADE_SHARE * LEVERAGE          # USDT под позицию с учётом плеча
    qty = round_qty(exposure / price, step, min_qty)
    if qty <= 0:
        return

    sig = signal(df)
    if not sig:
        return

    set_leverage(symbol, LEVERAGE)

    if sig == "Buy":
        sl = price * (1 - STOP_LOSS_PCT)
        tp = min(resistance, price * (1 + 3 * TRAIL_STEP_PCT))  # первичный ориентир (до +15%)
        res = place_market(symbol, "Buy", qty, sl, tp)
        print(f"🚀 {symbol} LONG qty={qty} @~{price:.4f} SL={sl:.4f} TP={tp:.4f} → {res.get('retMsg')}")
    else:
        sl = price * (1 + STOP_LOSS_PCT)
        tp = max(support, price * (1 - 3 * TRAIL_STEP_PCT))
        res = place_market(symbol, "Sell", qty, sl, tp)
        print(f"🔻 {symbol} SHORT qty={qty} @~{price:.4f} SL={sl:.4f} TP={tp:.4f} → {res.get('retMsg')}")

def manage_trailing(symbol: str, last_price: float, support: float, resistance: float):
    """Ступенчатый трейлинг и TP на актуальном уровне."""
    pos = get_position(symbol)
    if not pos:
        return

    side = pos["side"]             # "Buy" / "Sell"
    entry = float(pos["avgPrice"])

    # текущие SL/TP (могут быть пустыми)
    try:
        if side == "Buy":
            current_sl = float(pos.get("stopLoss") or 0.0)
        else:
            current_sl = float(pos.get("stopLoss") or 1e18)
    except:
        current_sl = 0.0 if side == "Buy" else 1e18

    if side == "Buy":
        gain = (last_price / entry) - 1.0
        steps = math.floor(gain / TRAIL_STEP_PCT) if gain > 0 else 0
        # новый SL к ступени (с отступом BUFFER) или базовый SL -10%
        new_sl = entry * (1 + steps * TRAIL_STEP_PCT - TRAIL_BUFFER) if steps > 0 else entry * (1 - STOP_LOSS_PCT)
        target = resistance
        if new_sl > current_sl:  # подтягиваем только вверх
            update_trading_stop(symbol, stop_loss=new_sl, take_profit=target)
            print(f"📈 {symbol} LONG: подтянул SL → {new_sl:.4f}, TP → {target:.4f}")

    else:  # Sell
        drop = 1.0 - (last_price / entry)
        steps = math.floor(drop / TRAIL_STEP_PCT) if drop > 0 else 0
        new_sl = entry * (1 - steps * TRAIL_STEP_PCT + TRAIL_BUFFER) if steps > 0 else entry * (1 + STOP_LOSS_PCT)
        target = support
        if new_sl < current_sl:  # подтягиваем только вниз
            update_trading_stop(symbol, stop_loss=new_sl, take_profit=target)
            print(f"📉 {symbol} SHORT: подтянул SL → {new_sl:.4f}, TP → {target:.4f}")

# =========================
# ОСНОВНОЙ ЦИКЛ
# =========================
def run_symbol(symbol: str):
    df = get_klines(symbol, INTERVAL)
    support, resistance = calc_sr(df, SR_LOOKBACK)
    last_price = float(df["close"].iloc[-1])

    pos = get_position(symbol)
    if pos:
        # есть позиция — менеджим трейлинг и актуализируем TP к уровню
        manage_trailing(symbol, last_price, support, resistance)
    else:
        # нет позиции — пробуем открыть по сигналу
        balance = get_balance()
        try_open(symbol, balance, df, support, resistance)

def main():
    print("🤖 Бот запущен (Bybit Testnet): 5m, EMA+RSI+S/R, 10% депо, x10, SL 10%, трейлинг +5%")
    while True:
        for sym in SYMBOLS:
            try:
                run_symbol(sym)
            except Exception as e:
                print(f"❗ Ошибка на {sym}: {e}")
            time.sleep(PAUSE_BETWEEN_SYMBOLS)
        time.sleep(LOOP_DELAY)

if __name__ == "__main__":
    main()
