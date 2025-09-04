from pybit.unified_trading import HTTP

# 🔑 API-ключи от Bybit Testnet
API_KEY = "твоя_api_key"
API_SECRET = "твой_api_secret"

# Инициализация с правильными параметрами
session = HTTP(
    testnet=True,
    api_key="ВАШ_API_KEY",
    api_secret="ВАШ_API_SECRET"
)
