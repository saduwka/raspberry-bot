from telegram import ReplyKeyboardMarkup


def reply_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["💼 Вакансии", "📈 Трейдинг"],
            ["🔍 Новости", "⚙️ Система"],
        ],
        resize_keyboard=True,
    )
