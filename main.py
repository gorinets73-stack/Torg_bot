import os
import time
import warnings
from pybit.unified_trading import HTTP

# Убираем предупреждения библиотеки
warnings.filterwarnings("ignore", category=SyntaxWarning)


def main():
    # Загружаем ключи из окружения
    api_key = os.getenv("BYBIT_API_KEY")
    api_secret = os.getenv("BYBIT_API_SECRET")

    if not api_key or not api_secret:
        print("❌ Ошибка: API ключи не найдены. Установите BYBIT_API_KEY и BYBIT_API_SECRET")
        return

    # Создаём сессию (тестовая биржа)
    session = HTTP(
        testnet=True,
        api_key=api_key,
        api_secret=api_secret
    )

    symbol = "BTCUSDT"
    leverage = 10
    order_value_usdt = 10  # размер сделки в долларах

    # ===== 1. Проверим, есть ли открытая позиция =====
    try:
        positions = session.get_positions(
            category="linear",
            symbol=symbol
        )
        position_data = positions.get("result", {}).get("list", [])
        has_position = any(float(p.get("size", 0)) > 0 for p in position_data)

        if has_position:
            print(f"ℹ️ Уже есть открытая позиция по {symbol}, новую сделку не открываю.")
            return
        else:
            print(f"✅ Позиции по {symbol} нет, можно открыть сделку.")
    except Exception as e:
        print("❌ Ошибка при получении позиций:", e)
        return

    # ===== 2. Установим плечо =====
    try:
        lev = session.set_leverage(
            category="linear",
            symbol=symbol,
            buyLeverage=leverage,
            sellLeverage=leverage
        )
        print(f"✅ Плечо установлено: {lev}")
    except Exception as e:
        print("❌ Ошибка при установке плеча:", e)
        return

    # ===== 3. Рассчитаем количество =====
    current_price = 20000  # можно заменить запросом к API для реальной цены
    qty = round(order_value_usdt / current_price, 4)

    # ===== 4. Откроем сделку (рыночный ордер) =====
    try:
        order = session.place_order(
            category="linear",
            symbol=symbol,
            side="Buy",          # long
            orderType="Market",
            qty=qty,
            timeInForce="GoodTillCancel",
            reduceOnly=False
        )
        print("✅ Сделка открыта:", order)
    except Exception as e:
        print("❌ Ошибка при открытии сделки:", e)


if __name__ == "__main__":
    # Цикл для Render
    while True:
        main()
        time.sleep(60)  # проверять раз в минуту
