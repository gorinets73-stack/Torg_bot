import os
import time
import threading
from flask import Flask
from pybit.unified_trading import HTTP

app = Flask(__name__)


def trading_bot():
    """Функция твоего бота"""
    while True:
        try:
            api_key = os.getenv("BYBIT_API_KEY")
            api_secret = os.getenv("BYBIT_API_SECRET")

            if not api_key or not api_secret:
                print("❌ Ошибка: API ключи не найдены. Установите BYBIT_API_KEY и BYBIT_API_SECRET")
                time.sleep(60)
                continue

            session = HTTP(testnet=True, api_key=api_key, api_secret=api_secret)
            symbol = "BTCUSDT"
            leverage = 10
            order_value_usdt = 10

            # Проверяем позиции
            positions = session.get_positions(category="linear", symbol=symbol)
            position_data = positions.get("result", {}).get("list", [])
            has_position = any(float(p.get("size", 0)) > 0 for p in position_data)

            if has_position:
                print(f"ℹ️ Уже есть открытая позиция по {symbol}, новую сделку не открываю.")
            else:
                print(f"✅ Позиции по {symbol} нет, можно открыть сделку.")

                # Установим плечо
                session.set_leverage(category="linear", symbol=symbol,
                                     buyLeverage=leverage, sellLeverage=leverage)

                # Откроем сделку
                current_price = 20000
                qty = round(order_value_usdt / current_price, 4)

                order = session.place_order(
                    category="linear",
                    symbol=symbol,
                    side="Buy",
                    orderType="Market",
                    qty=qty,
                    timeInForce="GoodTillCancel",
                    reduceOnly=False
                )
                print("✅ Сделка открыта:", order)

        except Exception as e:
            print("❌ Ошибка в боте:", e)

        time.sleep(60)  # проверка каждую минуту


@app.route("/")
def home():
    return "✅ Бот работает и слушает порт!"


if __name__ == "__main__":
    # Запускаем бота в отдельном потоке
    threading.Thread(target=trading_bot, daemon=True).start()

    # Flask слушает порт (Render требует PORT из окружения)
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
