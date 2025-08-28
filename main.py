import telebot

# === Твой токен ===
BOT_TOKEN = "8432592746:AAEg3uaH-Xa6tf-pqZI_RjiKaRZAQfsHbXM"

# === Создаем бота ===
bot = telebot.TeleBot(BOT_TOKEN)

# === Команда /start ===
@bot.message_handler(commands=["start"])
def start(message):
    bot.send_message(message.chat.id, "Бот запущен 🚀")

# === Ответ на любое сообщение ===
@bot.message_handler(func=lambda message: True)
def echo(message):
    bot.send_message(message.chat.id, f"Ты написал: {message.text}")

print("✅ Бот запущен и работает...")
bot.infinity_polling()
