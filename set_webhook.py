import requests

# 🔑 Твой токен от BotFather
TOKEN = "8432592746:AAEg3uaH-Xa6tf-pqZI_RjiKaRZAQfsHbXM"

# 🌍 Адрес твоего бота на Render (замени на свой реальный!)
WEBHOOK_URL = "https://mybot.onrender.com/"

url = f"https://api.telegram.org/bot{TOKEN}/setWebhook?url={WEBHOOK_URL}"

response = requests.get(url)

print(response.json())
