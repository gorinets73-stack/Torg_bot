#!/usr/bin/env python3
# main.py - Telegram bot + trading threads (Bitget swap via ccxt)
import os
import time
import json
import logging
import threading
from datetime import datetime
import traceback

import pandas as pd
import ccxt
import requests
from ta.momentum import RSIIndicator
from ta.trend import SMAIndicator

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------- CONFIG ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "YOUR_BOT_TOKEN")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID")  # optional default chat id for messages
API_URL = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

BITGET_API_KEY = os.environ.get("BITGET_API_KEY", "")
BITGET_API_SECRET = os.environ.get("BITGET_API_SECRET", "")
BITGET_API_PASSPHRASE = os.environ.get("BITGET_API_PASSPHRASE", "")

SETTINGS_FILE = "settings.json"
OPEN_TRADES_FILE = "open_trades.json"
CLOSED_TRADES_FILE = "closed_trades.json"
VIRTUAL_BALANCE_FILE = "virtual_balance.json"

# Popular symbols (defaults)
SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
    "ADA/USDT", "DOGE/USDT", "AVAX/USDT", "MATIC/USDT", "LTC/USDT"
]

# Strategy params
SL_PCT = 0.02
TP_PCT = 0.04
RSI_WINDOW = 14
SMA50 = 50
SMA200 = 200
LEVEL_LOOKBACK = 50
LEVEL_THRESHOLD_PCT = 0.005

# Timeframes & defaults
ALL_TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h"]
ACTIVE_TF = ["1m", "5m", "15m"]

INVEST_AMOUNT = 20.0
TRADE_MODE = "virtual"  # "virtual" or "real"
LEVERAGE = 10
CHECK_INTERVAL = 60  # seconds between signal checks

# in-memory state
open_trades = []
closed_trades = []
state_lock = threading.Lock()

# last chat id if not provided
_last_chat_id = None

# ---------------- Exchange (Bitget swap) ----------------
exchange = None


def init_exchange():
    global exchange
    if BITGET_API_KEY and BITGET_API_SECRET:
        try:
            exchange = ccxt.bitget({
                "apiKey": BITGET_API_KEY,
                "secret": BITGET_API_SECRET,
                "password": BITGET_API_PASSPHRASE,
                "enableRateLimit": True,
                "options": {"defaultType": "swap"},
            })
            exchange.load_markets()
            logging.info("Bitget initialized.")
        except Exception as e:
            logging.error(f"Exchange init error: {e}")
            exchange = None
    else:
        logging.info("Bitget keys not provided - running in virtual mode.")


init_exchange()

# ---------------- Storage helpers ----------------
def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str, ensure_ascii=False)
    except Exception as e:
        logging.error(f"Error saving {path}: {e}")


def load_json(path, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logging.error(f"Error reading {path}: {e}")
    return default


def save_state():
    with state_lock:
        save_json(OPEN_TRADES_FILE, open_trades)
        save_json(CLOSED_TRADES_FILE, closed_trades)


def load_state():
    global open_trades, closed_trades
    with state_lock:
        open_trades = load_json(OPEN_TRADES_FILE, [])
        closed_trades = load_json(CLOSED_TRADES_FILE, [])


def save_settings():
    try:
        data = {
            "ACTIVE_TF": ACTIVE_TF,
            "INVEST_AMOUNT": INVEST_AMOUNT,
            "TRADE_MODE": TRADE_MODE,
            "LEVERAGE": LEVERAGE,
            "SYMBOLS": SYMBOLS
        }
        save_json(SETTINGS_FILE, data)
    except Exception as e:
        logging.error(f"Error saving settings: {e}")


def load_settings():
    global ACTIVE_TF, INVEST_AMOUNT, TRADE_MODE, LEVERAGE, SYMBOLS
    data = load_json(SETTINGS_FILE, {})
    ACTIVE_TF[:] = data.get("ACTIVE_TF", ACTIVE_TF)
    INVEST_AMOUNT = data.get("INVEST_AMOUNT", INVEST_AMOUNT)
    TRADE_MODE = data.get("TRADE_MODE", TRADE_MODE)
    LEVERAGE = data.get("LEVERAGE", LEVERAGE)
    SYMBOLS[:] = data.get("SYMBOLS", SYMBOLS)


load_settings()
load_state()

# ---------------- Virtual balance ----------------
DEFAULT_VIRTUAL_BALANCE = {"currency": "USDT", "total": 1000.0, "available": 1000.0}


def load_virtual_balance():
    data = load_json(VIRTUAL_BALANCE_FILE, DEFAULT_VIRTUAL_BALANCE.copy())
    if "currency" not in data:
        data["currency"] = "USDT"
    if "total" not in data:
        data["total"] = float(DEFAULT_VIRTUAL_BALANCE["total"])
    if "available" not in data:
        data["available"] = float(data["total"])
    return data


def save_virtual_balance(bal):
    save_json(VIRTUAL_BALANCE_FILE, bal)


virtual_balance = load_virtual_balance()


def virtual_reserve(amount_usd):
    global virtual_balance
    if virtual_balance["available"] >= amount_usd:
        virtual_balance["available"] = round(virtual_balance["available"] - amount_usd, 8)
        save_virtual_balance(virtual_balance)
        return True
    return False


def virtual_release(amount_usd):
    global virtual_balance
    virtual_balance["available"] = round(virtual_balance["available"] + amount_usd, 8)
    virtual_balance["total"] = round(virtual_balance["total"] + amount_usd, 8)
    save_virtual_balance(virtual_balance)

# ---------------- Telegram helpers ----------------
def tg_send(chat_id, text, reply_markup=None):
    """
    Send a message synchronously using Telegram Bot HTTP API.
    Useful for background threads that are not async.
    """
    try:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if reply_markup:
            payload["reply_markup"] = json.loads(json.dumps(reply_markup))  # ensure serializable
        resp = requests.post(f"{API_URL}/sendMessage", json=payload, timeout=10)
        if resp.status_code != 200:
            logging.error(f"Tg send error {resp.status_code}: {resp.text}")
    except Exception as e:
        logging.error(f"Tg send error: {e}")


# ---------------- Market helpers ----------------
def fetch_ohlcv(symbol, timeframe="1h", limit=300):
    if not exchange:
        raise RuntimeError("Exchange not initialized.")
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["time", "open", "high", "low", "close", "volume"])
    df["time"] = pd.to_datetime(df["time"], unit="ms")
    return df


def compute_indicators(df):
    df = df.copy().reset_index(drop=True)
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["rsi"] = RSIIndicator(df["close"], window=RSI_WINDOW).rsi()
    df["sma50"] = SMAIndicator(df["close"], window=SMA50).sma_indicator()
    df["sma200"] = SMAIndicator(df["close"], window=SMA200).sma_indicator()
    return df


def levels_from_df(df, lookback=LEVEL_LOOKBACK):
    low = df["low"].tail(lookback).min()
    high = df["high"].tail(lookback).max()
    return float(low), float(high)


def pnl_percent(entry_price, current_price, direction):
    if direction == "LONG":
        return (current_price - entry_price) / entry_price
    else:
        return (entry_price - current_price) / entry_price


def cash_pnl(invest_amount, leverage, pnl_p):
    return invest_amount * leverage * pnl_p


def size_from_usd(symbol, price, amount_usd, leverage):
    amount = (amount_usd * leverage) / price
    return float(round(amount, 6))

# ---------------- Orders ----------------
def place_real_market_order(symbol, side, amount):
    try:
        order = exchange.create_order(symbol, "market", side, amount, None, {})
        logging.info(f"Real market order placed: {order}")
        return order
    except Exception as e:
        logging.error(f"Error placing real order: {e}")
        return None


def close_real_position_by_market(symbol, side_opposite, amount):
    return place_real_market_order(symbol, side_opposite, amount)

# ---------------- Trades ----------------
def chat_from_last_update():
    return _last_chat_id


def open_trade(symbol, direction, entry_price, timeframe, strategy_source="signal", invest=INVEST_AMOUNT, real_order=None, amount_base=None):
    sl_price = entry_price * (1 - SL_PCT) if direction == "LONG" else entry_price * (1 + SL_PCT)
    tp_price = entry_price * (1 + TP_PCT) if direction == "LONG" else entry_price * (1 - TP_PCT)
    trade = {
        "id": f"{symbol}-{timeframe}-{int(time.time())}",
        "symbol": symbol,
        "direction": direction,
        "entry_price": float(entry_price),
        "sl_price": float(sl_price),
        "tp_price": float(tp_price),
        "invest": invest,
        "leverage": LEVERAGE,
        "opened_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "strategy": strategy_source,
        "timeframe": timeframe,
        "status": "OPEN",
        "real": bool(real_order),
        "real_order": real_order,
        "amount_base": amount_base
    }
    with state_lock:
        open_trades.append(trade)
        save_state()
    chat = TG_CHAT_ID or chat_from_last_update()
    if chat:
        tg_send(chat, f"💼 OPEN: {symbol} {direction} {timeframe}\nentry={entry_price:.2f}, SL={sl_price:.2f}, TP={tp_price:.2f}\nMode: {mode_status()}")
    logging.info(f"Opened trade: {trade['id']}")
    return trade


def close_trade(trade, exit_price, reason):
    with state_lock:
        trade["closed_at"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        trade["exit_price"] = float(exit_price)
        trade["status"] = "CLOSED"
        pnl_p = pnl_percent(trade["entry_price"], exit_price, trade["direction"])
        trade["pnl_percent"] = round(pnl_p * 100, 4)
        trade["pnl_cash"] = round(cash_pnl(trade["invest"], trade["leverage"], pnl_p), 8)
        trade["close_reason"] = reason
        closed_trades.append(trade)
        open_trades[:] = [t for t in open_trades if t["id"] != trade["id"]]
        save_state()

    if not trade.get("real"):
        invest = trade["invest"]
        pnl_cash = trade.get("pnl_cash", 0.0)
        virtual_release(invest + pnl_cash)

    chat = TG_CHAT_ID or chat_from_last_update()
    if chat:
        tg_send(chat, f"✅ CLOSED: {trade['symbol']} {trade['direction']}\nPnL={trade['pnl_percent']}% ({trade['pnl_cash']}$)\nReason: {reason}")
    logging.info(f"Closed trade: {trade['id']} reason={reason}")

# ---------------- Signals & Logic ----------------
def format_signal_text(symbol, df):
    price = df["close"].iloc[-1]
    rsi = df["rsi"].iloc[-1]
    sma50 = df["sma50"].iloc[-1]
    sma200 = df["sma200"].iloc[-1]
    support, resistance = levels_from_df(df)
    trend = "up" if price > sma200 else "down"
    if rsi < 30:
        rsi_status = "oversold (LONG possible)"
    elif rsi > 70:
        rsi_status = "overbought (SHORT possible)"
    else:
        rsi_status = "neutral"
    return (f"📊 {symbol}\nPrice: {price:.2f}$\nRSI: {round(rsi,2)} ({rsi_status})\nSMA50: {round(sma50,2)}, SMA200: {round(sma200,2)}\nTrend: {trend}\nLvls: S {round(support,2)}, R {round(resistance,2)}")


def is_price_near_level(price, level):
    return abs(price - level) / level <= LEVEL_THRESHOLD_PCT


def check_signals_once():
    if not SYMBOLS:
        return
    for symbol in list(SYMBOLS):
        for tf in list(ACTIVE_TF):
            try:
                if TRADE_MODE == "real" and exchange is None:
                    continue
                if exchange is None:
                    continue
                df = fetch_ohlcv(symbol, timeframe=tf, limit=300)
                df = compute_indicators(df)
                if len(df) < SMA200:
                    continue
                price = float(df["close"].iloc[-1])
                rsi = float(df["rsi"].iloc[-1])
                sma200 = float(df["sma200"].iloc[-1])
                support, resistance = levels_from_df(df)

                direction = None
                reason = ""

                if rsi < 30 and price > sma200:
                    direction, reason = "LONG", "RSI < 30 & price > SMA200"
                elif rsi > 70 and price < sma200:
                    direction, reason = "SHORT", "RSI > 70 & price < SMA200"

                if not direction:
                    if is_price_near_level(price, support) and rsi < 40 and price > sma200:
                        direction, reason = "LONG", "near support + RSI<40 + uptrend"
                    elif is_price_near_level(price, resistance) and rsi > 60 and price < sma200:
                        direction, reason = "SHORT", "near resistance + RSI>60 + downtrend"

                if direction:
                    trade_id_prefix = f"{symbol}-{tf}"
                    with state_lock:
                        if any(t["id"].startswith(trade_id_prefix) for t in open_trades):
                            continue

                    chat = TG_CHAT_ID or chat_from_last_update()
                    if chat:
                        tg_send(chat, f"⚡ SIGNAL: {symbol} {tf} {direction}\n{format_signal_text(symbol, df)}\nReason: {reason}\nSize: {INVEST_AMOUNT}$\nMode: {mode_status()}")

                    # open
                    if TRADE_MODE == "virtual" or exchange is None:
                        if not virtual_reserve(INVEST_AMOUNT):
                            if chat:
                                tg_send(chat, f"⚠️ Not enough virtual balance for {symbol}")
                            continue
                        open_trade(symbol, direction, price, tf, strategy_source=reason, invest=INVEST_AMOUNT, real_order=None, amount_base=size_from_usd(symbol, price, INVEST_AMOUNT, LEVERAGE))
                    else:
                        amount_base = size_from_usd(symbol, price, INVEST_AMOUNT, LEVERAGE)
                        side = "buy" if direction == "LONG" else "sell"
                        try:
                            if hasattr(exchange, "set_leverage"):
                                try:
                                    exchange.set_leverage(LEVERAGE, symbol)
                                except Exception:
                                    pass
                        except Exception:
                            pass
                        order = place_real_market_order(symbol, side, amount_base)
                        if order:
                            open_trade(symbol, direction, price, tf, strategy_source=reason, invest=INVEST_AMOUNT, real_order={"order": order}, amount_base=amount_base)
                        else:
                            if chat:
                                tg_send(chat, f"⚠️ Failed to open real trade for {symbol}")
            except Exception as e:
                logging.error(f"Signal error {symbol} {tf}: {e}\n{traceback.format_exc()}")

# ---------------- Monitor open trades ----------------
def monitor_open_trades_loop():
    while True:
        try:
            with state_lock:
                snapshot = list(open_trades)
            for trade in snapshot:
                try:
                    symbol = trade["symbol"]
                    direction = trade["direction"]
                    if exchange:
                        ticker = exchange.fetch_ticker(symbol)
                        price = float(ticker.get("last") or ticker.get("close") or 0)
                    else:
                        continue
                    if direction == "LONG":
                        if price <= trade["sl_price"]:
                            if trade.get("real") and exchange:
                                amount = trade.get("amount_base") or size_from_usd(symbol, trade["entry_price"], trade["invest"], trade["leverage"])
                                close_real_position_by_market(symbol, "sell", amount)
                            close_trade(trade, price, "Hit SL")
                        elif price >= trade["tp_price"]:
                            if trade.get("real") and exchange:
                                amount = trade.get("amount_base") or size_from_usd(symbol, trade["entry_price"], trade["invest"], trade["leverage"])
                                close_real_position_by_market(symbol, "sell", amount)
                            close_trade(trade, price, "Hit TP")
                    else:  # SHORT
                        if price >= trade["sl_price"]:
                            if trade.get("real") and exchange:
                                amount = trade.get("amount_base") or size_from_usd(symbol, trade["entry_price"], trade["invest"], trade["leverage"])
                                close_real_position_by_market(symbol, "buy", amount)
                            close_trade(trade, price, "Hit SL")
                        elif price <= trade["tp_price"]:
                            if trade.get("real") and exchange:
                                amount = trade.get("amount_base") or size_from_usd(symbol, trade["entry_price"], trade["invest"], trade["leverage"])
                                close_real_position_by_market(symbol, "buy", amount)
                            close_trade(trade, price, "Hit TP")
                except Exception as e:
                    logging.error(f"Monitoring error for {trade.get('id')}: {e}\n{traceback.format_exc()}")
            time.sleep(5)
        except Exception as e:
            logging.error(f"monitor loop error: {e}\n{traceback.format_exc()}")
            time.sleep(5)

def check_signals_loop():
    while True:
        try:
            check_signals_once()
            time.sleep(CHECK_INTERVAL)
        except Exception as e:
            logging.error(f"check_signals_loop error: {e}\n{traceback.format_exc()}")
            time.sleep(5)

# ---------------- Mode & helpers ----------------
def mode_status():
    return "Virtual" if TRADE_MODE == "virtual" else "Real"


def format_settings_text():
    return (f"⚙️ Settings:\nSymbols: {', '.join(SYMBOLS)}\nTFs: {', '.join(ACTIVE_TF)}\nSize: {INVEST_AMOUNT}$\nLeverage: {LEVERAGE}x\nMode: {mode_status()}")

# ---------------- Telegram handlers (async) ----------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _last_chat_id
    _last_chat_id = update.effective_chat.id
    await update.message.reply_text("Bot started. " + format_settings_text())


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Commands:\n"
        "/start\n/help\n/settings\n/strategy\n/panel\n/mode\n"
        "/tfs\n/amount N\n/leverage N\n/add_symbol SYMBOL\n/remove_symbol SYMBOL\n/open\n/closed\n/balance\n/force_check"
    )


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(format_settings_text())


async def strategy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"RSI({RSI_WINDOW}) + SMA{SMA50}/{SMA200}\nLONG: RSI<30 & price>SMA200\nSHORT: RSI>70 & price<SMA200\nSL={int(SL_PCT*100)}% TP={int(TP_PCT*100)}")


async def panel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = []
    row = []
    for tf in ALL_TIMEFRAMES:
        text = f"{'❌' if tf in ACTIVE_TF else '✅'} {tf}"
        cb = f"tf_toggle:{tf}"
        row.append(InlineKeyboardButton(text, callback_data=cb))
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    kb.append([InlineKeyboardButton("Set Virtual", callback_data="mode_virtual"), InlineKeyboardButton("Set Real", callback_data="mode_real")])
    await update.message.reply_text("Panel:", reply_markup=InlineKeyboardMarkup(kb))


async def mode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Virtual", callback_data="mode_virtual"), InlineKeyboardButton("Real", callback_data="mode_real")]])
    await update.message.reply_text("Choose mode:", reply_markup=kb)


async def tfs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Active TFs: " + ", ".join(ACTIVE_TF))


async def amount_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Usage: /amount 50
    Set INVEST_AMOUNT if positive.
    """
    global INVEST_AMOUNT
    try:
        parts = context.args
        if not parts:
            await update.message.reply_text("⚠️ Usage: /amount 50")
            return

        new = float(parts[0])
        if new > 0:
            INVEST_AMOUNT = new
            save_settings()
            await update.message.reply_text(f"✔️ Size set: {INVEST_AMOUNT}$")
        else:
            await update.message.reply_text("⚠️ Amount must be greater than 0")
    except ValueError:
        await update.message.reply_text("⚠️ Invalid number. Example: /amount 50")


async def leverage_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global LEVERAGE
    try:
        parts = context.args
        if not parts:
            await update.message.reply_text("⚠️ Usage: /leverage 10")
            return
        new = int(parts[0])
        if new > 0:
            LEVERAGE = new
            save_settings()
            await update.message.reply_text(f"✔️ Leverage set: {LEVERAGE}x")
        else:
            await update.message.reply_text("⚠️ Leverage must be > 0")
    except Exception:
        await update.message.reply_text("⚠️ Invalid number. Example: /leverage 10")


async def add_symbol_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        parts = context.args
        if not parts:
            await update.message.reply_text("⚠️ Usage: /add_symbol BTC/USDT")
            return
        sym = parts[0].upper()
        if sym not in SYMBOLS:
            SYMBOLS.append(sym)
            save_settings()
            await update.message.reply_text(f"✔️ Added symbol: {sym}")
        else:
            await update.message.reply_text("⚠️ Symbol already present")
    except Exception as e:
        logging.error(f"add_symbol_cmd error: {e}")
        await update.message.reply_text("⚠️ Error adding symbol")


async def remove_symbol_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        parts = context.args
        if not parts:
            await update.message.reply_text("⚠️ Usage: /remove_symbol BTC/USDT")
            return
        sym = parts[0].upper()
        if sym in SYMBOLS:
            SYMBOLS.remove(sym)
            save_settings()
            await update.message.reply_text(f"✔️ Removed symbol: {sym}")
        else:
            await update.message.reply_text("⚠️ Symbol not found")
    except Exception as e:
        logging.error(f"remove_symbol_cmd error: {e}")
        await update.message.reply_text("⚠️ Error removing symbol")


async def open_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with state_lock:
        if not open_trades:
            await update.message.reply_text("No open trades.")
            return
        lines = []
        for t in open_trades:
            lines.append(f"{t['id']}: {t['symbol']} {t['direction']} entry={t['entry_price']}$ SL={t['sl_price']}$ TP={t['tp_price']}$")
        await update.message.reply_text("\n".join(lines))


async def closed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with state_lock:
        if not closed_trades:
            await update.message.reply_text("No closed trades.")
            return
        lines = []
        for t in closed_trades[-20:]:
            lines.append(f"{t['id']}: {t['symbol']} {t['direction']} PnL={t.get('pnl_percent','?')}%")
        await update.message.reply_text("\n".join(lines))


async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Virtual balance: {virtual_balance['available']}$ available (total {virtual_balance['total']}$)")


async def force_check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Running signal check...")
    # run check synchronously (it will call tg_send for notifications)
    try:
        check_signals_once()
        await update.message.reply_text("Check finished.")
    except Exception as e:
        logging.error(f"force_check error: {e}\n{traceback.format_exc()}")
        await update.message.reply_text("Error during check.")


# CallbackQuery handler (for panel buttons)
async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    try:
        if data.startswith("tf_toggle:"):
            tf = data.split(":", 1)[1]
            if tf in ACTIVE_TF:
                ACTIVE_TF.remove(tf)
            else:
                ACTIVE_TF.append(tf)
            save_settings()
            await query.edit_message_text("Panel updated.")
        elif data == "mode_virtual":
            global TRADE_MODE
            TRADE_MODE = "virtual"
            save_settings()
            await query.edit_message_text("Mode set to Virtual.")
        elif data == "mode_real":
            TRADE_MODE = "real"
            save_settings()
            await query.edit_message_text("Mode set to Real.")
        else:
            await query.edit_message_text("Unknown action.")
    except Exception as e:
        logging.error(f"callback_query_handler error: {e}\n{traceback.format_exc()}")
        await query.edit_message_text("Error processing action.")


# ---------------- Main & startup ----------------
def start_background_threads():
    logging.info("Starting background threads (monitoring + signal checker)...")
    t1 = threading.Thread(target=monitor_open_trades_loop, daemon=True)
    t1.start()
    t2 = threading.Thread(target=check_signals_loop, daemon=True)
    t2.start()


def build_application():
    app = Application.builder().token(TG_BOT_TOKEN).build()

    # commands
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("strategy", strategy_cmd))
    app.add_handler(CommandHandler("panel", panel_cmd))
    app.add_handler(CommandHandler("mode", mode_cmd))
    app.add_handler(CommandHandler("tfs", tfs_cmd))
    app.add_handler(CommandHandler("amount", amount_cmd))
    app.add_handler(CommandHandler("leverage", leverage_cmd))
    app.add_handler(CommandHandler("add_symbol", add_symbol_cmd))
    app.add_handler(CommandHandler("remove_symbol", remove_symbol_cmd))
    app.add_handler(CommandHandler("open", open_cmd))
    app.add_handler(CommandHandler("closed", closed_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("force_check", force_check_cmd))

    # callback queries
    app.add_handler(CallbackQueryHandler(callback_query_handler))

    # simple echo for non-command text (optional)
    # app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    return app


def main():
    # Load state/settings already done above
    start_background_threads()
    app = build_application()
    logging.info("Starting Telegram bot (polling)...")
    # This will block; the background threads run independently
    app.run_polling()


if __name__ == "__main__":
    main()
