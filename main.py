from pybit.unified_trading import HTTP

# 🔑 API-ключи от Bybit Testnet
API_KEY = "твоя_api_key"
API_SECRET = "твой_api_secret"

# Инициализация с правильными параметрами
session = HTTP(
    testnet=True,           # используем тестовую биржу
    api_key=API_KEY,
    api_secre
