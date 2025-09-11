import os
import time
import logging
import json
import requests
import pandas as pd
import ccxt
from ta.momentum import RSIIndicator
from ta.trend import SMAIndicator
from flask import Flask, request
from threading import Thread

# === Настройки ===
TEST_MODE = True  # виртуальный режим
SYMBOLS = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT",
           "XRP/USDT", "ADA/USDT", "DOGE/USDT", "TRX/USDT",
           "DOT/USDT", "POL/USDT", "AVAX/USDT", "LINK/USDT",
           "LTC/USDT", "BCH/USDT"]

INVEST_AMOUNT = 10          # $ на одну сделку (но учитываем LEVERAGE при расчётах PnL)
LEVERAGE = 10               # условное плечо для расчёта PnL
SL_PCT = 0.02               # стоп-лосс 2%
TP_PCT = 0.04               # тейк-профит 4%

BOT_TOKEN = "8432592746:AAEg3uaH-Xa6tf-pqZI_RjiKaRZAQfsHbXM"
CHAT_ID = "1623720732"
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

OPEN_TRADES_FILE = "open_trades.json"
CLOSED_TRADES_FILE = "closed_trades.json"

# === Логирование ===
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# === Flask ===
app = Flask(__name__)

# === Биржа ===
exchange = ccxt.bitget({"enableRateLimit": True})
exchange.load_markets()

# === Словарь замен тикеров ===
SYMBOL_ALIASES = {
    "MATIC/USDT": "POL/USDT",
    "XBT/USDT": "BTC/USDT",
    "ETH2/USDT": "ETH/USDT",
    "BCHABC/USDT": "BCH/USDT",
    "BCHSV/USDT": "BSV/USDT",
    "DOGECOIN/USDT": "DOGE/USDT",
    "SHIB/USDT": "SHIB1000/USDT",
}

# === Память: открытые/закрытые сделки ===
open_trades = []
closed_trades = []

def save_state():
    try:
        with open(OPEN_TRADES_FILE, "w") as f:
            json.dump(open_trades, f, indent=2)
        with open(CLOSED_TRADES_FILE, "w") as f:
            json.dump(closed_trades, f, indent=2)
    except Exception as e:
        logging.error(f"Ошибка сохранения состояния: {e}")

def load_state():
    global open_trades, closed_trades
    try:
        if os.path.exists(OPEN_TRADES_FILE):
            with open(OPEN_TRADES_FILE, "r") as f:
                open_trades = json.load(f)
        if os.path.exists(CLOSED_TRADES_FILE):
            with open(CLOSED_TRADES_FILE, "r") as f:
                closed_trades = json.load(f)
    except Exception as e:
        logging.error(f"Ошибка загрузки состояния: {e}")

load_state()

# === Утилиты ===
def get_symbol(symbol: str):
    if symbol in SYMBOL_ALIASES:
        return SYMBOL_ALIASES[symbol]
    if symbol in exchange.symbols:
        return symbol
    candidates = [s for s in exchange.symbols if symbol.replace("/", "").lower() in s.replace("/", "").lower()]
    if candidates:
        return candidates[0]
    raise ValueError(f"Символ {symbol} не найден на бирже")

def send_message(chat_id, text):
    url = f"{API_URL}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logging.error(f"Ошибка отправки в Telegram: {e}")

# === Анализ данных ===
def fetch_ohlcv(symbol, timeframe="1h", limit=300):
    market = get_symbol(symbol)
    ohlcv = exchange.fetch_ohlcv(market, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["time", "open", "high", "low", "close", "volume"])
    return df

def compute_indicators(df):
    df = df.copy()
    df["rsi"] = RSIIndicator(df["close"], window=14).rsi()
    df["sma50"] = SMAIndicator(df["close"], window=50).sma_indicator()
    df["sma200"] = SMAIndicator(df["close"], window=200).sma_indicator()
    return df

# === Логика уровней ===
def levels_from_df(df, lookback=50):
    support = df["low"].tail(lookback).min()
    resistance = df["high"].tail(lookback).max()
    return support, resistance

# === PnL и размер позиции ===
def pnl_percent(entry_price, current_price, direction):
    if direction == "LONG":
        return (current_price - entry_price) / entry_price
    else:  # SHORT
        return (entry_price - current_price) / entry_price

def cash_pnl(invest_amount, leverage, pnl_percent):
    # простая аппроксимация: экспозиция = invest_amount * leverage
    return invest_amount * leverage * pnl_percent

# === Открыть виртуальную позицию ===
def open_virtual_trade(symbol, direction, entry_price, strategy_source, timeframe):
    sl_price = entry_price * (1 - SL_PCT) if direction == "LONG" else entry_price * (1 + SL_PCT)
    tp_price = entry_price * (1 + TP_PCT) if direction == "LONG" else entry_price * (1 - TP_PCT)

    trade = {
        "id": f"{symbol}-{int(time.time())}",
        "symbol": symbol,
        "direction": direction,
        "entry_price": float(entry_price),
        "sl_price": float(sl_price),
        "tp_price": float(tp_price),
        "invest": INVEST_AMOUNT,
        "leverage": LEVERAGE,
        "opened_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
        "strategy": strategy_source,
        "timeframe": timeframe,
        "status": "OPEN"
    }
    open_trades.append(trade)
    save_state()
    send_message(CHAT_ID, f"💼 ОТКРЫТА ВИРТУАЛЬНАЯ ПОЗИЦИЯ:\n{trade['symbol']} | {trade['direction']}\nentry={entry_price:.2f}, SL={sl_price:.2f}, TP={tp_price:.2f}\nstrategy={strategy_source}")
    logging.info(f"Opened virtual trade: {trade}")
    return trade

# === Закрыть виртуальную позицию ===
def close_virtual_trade(trade, exit_price, reason):
    trade["closed_at"] = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")
    trade["exit_price"] = float(exit_price)
    trade["status"] = "CLOSED"
    pnl_p = pnl_percent(trade["entry_price"], exit_price, trade["direction"])
    trade["pnl_percent"] = round(pnl_p * 100, 4)
    trade["pnl_cash"] = round(cash_pnl(trade["invest"], trade["leverage"], pnl_p), 8)
    trade["close_reason"] = reason

    # перемещение в closed_trades
    closed_trades.append(trade)
    # удалить из open_trades
    global open_trades
    open_trades = [t for t in open_trades if t["id"] != trade["id"]]
    save_state()

    send_message(CHAT_ID, f"✅ ЗАКРЫТА ПОЗИЦИЯ: {trade['symbol']} | {trade['direction']}\nexit={exit_price:.2f}\nP&L={trade['pnl_percent']}% ({trade['pnl_cash']}$)\nПричина: {reason}")
    logging.info(f"Closed virtual trade: {trade}")

# === Проверка условий открытия (включая тренд и объём) ===
def check_and_maybe_open(symbol, df_hourly, timeframe="1h"):
    """
    df_hourly: DataFrame уже с индикаторами для timeframe (обычно 1h)
    Возвращает: None или открытый trade
    """
    market_symbol = get_symbol(symbol)
    price = df_hourly["close"].iloc[-1]
    rsi = df_hourly["rsi"].iloc[-1]
    sma50 = df_hourly["sma50"].iloc[-1]
    sma200 = df_hourly["sma200"].iloc[-1]
    avg_vol_20 = df_hourly["volume"].tail(20).mean()
    vol_now = df_hourly["volume"].iloc[-1]

    support, resistance = levels_from_df(df_hourly, lookback=50)

    # тренд по SMA200
    trend = "UP" if price > sma200 else "DOWN"

    # сигналы индикаторные
    rsi_signal = None
    if rsi < 30 and price > sma50:
        rsi_signal = "LONG"
    elif rsi > 70 and price < sma50:
        rsi_signal = "SHORT"

    # сигналы по уровням (±0.5%)
    tolerance = 0.005 * price
    level_signal = None
    if abs(price - support) <= tolerance:
        level_signal = "LONG"
    elif abs(price - resistance) <= tolerance:
        level_signal = "SHORT"

    # volume filter
    vol_ok = vol_now >= avg_vol_20 if not pd.isna(avg_vol_20) else True

    # Decide final direction: require volume and trend agreement
    chosen = None
    reason = []
    if rsi_signal:
        reason.append(f"RSI+SMA:{rsi_signal}")
        chosen = rsi_signal
    if level_signal:
        reason.append(f"LEVELS:{level_signal}")
        # if both present and agree, keep. if conflict, prefer level_signal (configurable)
        if chosen is None:
            chosen = level_signal
        elif chosen != level_signal:
            # conflict: require extra condition — skip opening to avoid mixed signals
            chosen = None
            reason.append("CONFLICT_RSI_LEVEL -> SKIP")

    # Enforce trend filter: only open if direction equals trend (UP → LONG only; DOWN → SHORT only)
    if chosen:
        if (trend == "UP" and chosen != "LONG") or (trend == "DOWN" and chosen != "SHORT"):
            reason.append(f"Trend mismatch ({trend}) -> skip")
            chosen = None

    # Enforce volume
    if chosen and not vol_ok:
        reason.append("Low volume -> skip")
        chosen = None

    # Already have an open trade on this symbol in same direction? skip (prevent duplicates)
    if chosen:
        for t in open_trades:
            if t["symbol"] == symbol and t["direction"] == chosen:
                reason.append("Already open same-direction trade -> skip")
                chosen = None
                break

    # If chosen, open
    if chosen:
        strategy_source = ",".join(reason) if reason else "auto"
        return open_virtual_trade(symbol, chosen, entry_price=price, strategy_source=strategy_source, timeframe=timeframe)
    else:
        # if there were candidate infos, send compact info to telegram (but avoid spamming — send only hourly summary)
        info_text = (f"ℹ️ {market_symbol} [{timeframe}] price={price:.2f}, RSI={round(rsi,2)}, SMA50={round(sma50,2)}, "
                     f"SMA200={round(sma200,2)}, vol={vol_now:.2f}")
        # We'll send as info (but to avoid spam, not for every symbol every loop — main loop throttles)
        return None

# === Проверка открытых позиций: TP/SL/по обратному сигналу ===
def check_open_trades_and_close_if_needed(latest_prices):
    """
    latest_prices: dict symbol -> current_price
    """
    to_close = []
    for trade in list(open_trades):  # clone to allow modification
        symbol = trade["symbol"]
        if symbol not in latest_prices:
            continue
        price = latest_prices[symbol]
        # check TP/SL
        if trade["direction"] == "LONG":
            if price <= trade["sl_price"]:
                close_virtual_trade(trade, price, "Stop-Loss")
                continue
            if price >= trade["tp_price"]:
                close_virtual_trade(trade, price, "Take-Profit")
                continue
        else:  # SHORT
            if price >= trade["sl_price"]:
                close_virtual_trade(trade, price, "Stop-Loss")
                continue
            if price <= trade["tp_price"]:
                close_virtual_trade(trade, price, "Take-Profit")
                continue

        # optionally: close if opposite strong signal appears (simple rule)
        # We'll compute quick indicator on 1h for symbol to detect opposite signal
        try:
            df = fetch_ohlcv(symbol, timeframe="1h", limit=300)
            df = compute_indicators(df)
            price_now = df["close"].iloc[-1]
            rsi = df["rsi"].iloc[-1]
            sma50 = df["sma50"].iloc[-1]
            opposite = False
            if trade["direction"] == "LONG" and (rsi > 70 and price_now < sma50):
                opposite = True
                reason = "Opposite signal (RSI>70 & price<SMA50)"
            if trade["direction"] == "SHORT" and (rsi < 30 and price_now > sma50):
                opposite = True
                reason = "Opposite signal (RSI<30 & price>SMA50)"
            if opposite:
                close_virtual_trade(trade, price, reason)
        except Exception as e:
            logging.error(f"Ошибка проверки противоположного сигнала для {symbol}: {e}")

# === Цикл торговли ===
def trading_loop():
    send_message(CHAT_ID, "🤖 Бот запущен (виртуальный счёт). Стратегия: RSI+SMA50 + уровни + SMA200(trend) + volume filter. SL/TP включены.")
    last_summary_sent = 0
    summary_interval = 60 * 60  # присылать сводку по каждому символу не чаще чем раз в час (предотвращаем спам)
    while True:
        loop_start = time.time()
        latest_prices = {}
        for symbol in SYMBOLS:
            try:
                # используем 1h как основной timeframe для сигналов и тренда
                df1h = fetch_ohlcv(symbol, timeframe="1h", limit=300)
                df1h = compute_indicators(df1h)

                # Проверка и возможно открытие (возвращает trade или None)
                maybe_trade = check_and_maybe_open(symbol, df1h, timeframe="1h")
                # Отправляем периодическую информацию  (не чаще summary_interval)
                if time.time() - last_summary_sent > summary_interval:
                    price = df1h["close"].iloc[-1]
                    rsi = df1h["rsi"].iloc[-1]
                    sma50 = df1h["sma50"].iloc[-1]
                    sma200 = df1h["sma200"].iloc[-1]
                    support, resistance = levels_from_df(df1h, lookback=50)
                    summary = (f"Σ {get_symbol(symbol)} [1h]\nprice={price:.2f}, RSI={round(rsi,2)}, SMA50={round(sma50,2)}, SMA200={round(sma200,2)}\n"
                               f"Support={round(support,2)}, Resistance={round(resistance,2)}")
                    send_message(CHAT_ID, summary)

                latest_prices[symbol] = df1h["close"].iloc[-1]
            except Exception as e:
                logging.error(f"Ошибка в торговом цикле для {symbol}: {e}")

        # Check open trades for TP/SL/opposite signals
        if latest_prices:
            check_open_trades_and_close_if_needed(latest_prices)

        # update summary timestamp after full loop
        if time.time() - last_summary_sent > summary_interval:
            last_summary_sent = time.time()

        # main loop sleep: запуск примерно раз в 5 минут (1h timeframe достаточно редко)
        # но оставляем 60s для более оперативной проверки TP/SL
        sleep_time = 60 - ((time.time() - loop_start) % 60)
        time.sleep(max(1, sleep_time))

# === Telegram команды ===
def format_open_positions_text():
    if not open_trades:
        return "📝 Открытых позиций пока нет."
    text = "📊 Открытые виртуальные позиции:\n"
    for t in open_trades:
        # попытка получить актуальную цену
        try:
            df = fetch_ohlcv(t["symbol"], timeframe=t.get("timeframe", "1h"), limit=2)
            cur_price = df["close"].iloc[-1]
            pnl_p = pnl_percent(t["entry_price"], cur_price, t["direction"])
            pnl_cash = cash_pnl(t["invest"], t["leverage"], pnl_p)
            text += (f"{t['symbol']} | {t['direction']} | entry {t['entry_price']:.2f} | "
                     f"cur {cur_price:.2f} | PnL {round(pnl_p*100,4)}% ({round(pnl_cash,6)}$) | SL {t['sl_price']:.2f} TP {t['tp_price']:.2f}\n")
        except Exception:
            text += f"{t['symbol']} | {t['direction']} | entry {t['entry_price']:.2f} | (цена недоступна)\n"
    return text

def format_history_text(limit=20):
    if not closed_trades:
        return "📝 История закрытых сделок пуста."
    text = f"📚 Закрытые сделки (последние {min(limit,len(closed_trades))}):\n"
    for t in closed_trades[-limit:]:
        text += (f"{t['closed_at']} | {t['symbol']} | {t['direction']} | entry {t['entry_price']:.2f} -> exit {t['exit_price']:.2f} | "
                 f"Pnl {t.get('pnl_percent','?')}% ({t.get('pnl_cash','?')}$) | reason: {t.get('close_reason','-')}\n")
    return text

def handle_command(command):
    cmd = command.strip().lower()
    if cmd == "/start":
        return "✅ Бот работает в ТЕСТОВОМ (виртуальном) режиме.\nКоманды: /help /positions /history"
    if cmd == "/help":
        return ("📌 Команды:\n"
                "/start - статус бота\n"
                "/help - помощь\n"
                "/positions - показать открытые позиции с PnL\n"
                "/history - показать закрытые сделки")
    if cmd == "/positions":
        return format_open_positions_text()
    if cmd == "/history":
        return format_history_text()
    return "⚠️ Неизвестная команда. Напиши /help."

@app.route("/", methods=["POST", "GET"])
def webhook():
    if request.method == "POST":
        update = request.json
        if not update:
            return {"ok": True}
        if "message" in update and "text" in update["message"]:
            chat_id = update["message"]["chat"]["id"]
            text = update["message"]["text"].strip()
            reply = handle_command(text)
            send_message(chat_id, reply)
        return {"ok": True}
    return "Бот работает 🚀"

# === Запуск ===
if __name__ == "__main__":
    logging.info("Запуск бота...")
    # старт мониторинга в фоне
    Thread(target=trading_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
