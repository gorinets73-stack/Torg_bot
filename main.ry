from pybit.unified_trading import HTTP

# 🔑 API-ключи от Bybit Testnet
API_KEY = "твоя_api_key"
API_SECRET = "твой_api_secret"

# Инициализация с правильными параметрами
session = HTTP(
    testnet=True,           # используем тестовую биржу
    api_key=API_KEY,
    api_secret=API_SECRET,
    timeout=10,             # вместо request_timeout
    proxy=None              # если нужен прокси: "http://127.0.0.1:8080"
)

print("✅ Подключение к Bybit Testnet успешно!")

# 🔹 Пример: получаем баланс
balance = session.get_wallet_balance(accountType="UNIFIED")
print(balance)

# Здесь можешь подключить свою стратегию:
# EMA + RSI + S/R
# SL 10%, трейлинг 5%
