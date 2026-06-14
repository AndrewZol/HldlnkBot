import asyncio
import json
import os
import re
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.client.default import DefaultBotProperties
from openai import AsyncOpenAI
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

# Проверка, что токены загружены
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден в переменных окружения")
if not DEEPSEEK_API_KEY:
    raise ValueError("DEEPSEEK_API_KEY не найден в переменных окружения")

# Инициализация бота и DeepSeek
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties())
dp = Dispatcher()

# Подключение к DeepSeek через OpenRouter
deepseek = AsyncOpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://openrouter.ai/api/v1"
)

HISTORY_FILE = "history.json"

# Клавиатура
main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📋 Составить рацион")],
        [KeyboardButton(text="🗑 Очистить историю"), KeyboardButton(text="❓ Помощь")]
    ],
    resize_keyboard=True
)

# Работа с историей
def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_history(history):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=4, ensure_ascii=False)

def get_user_context(user_id):
    history = load_history()
    user_id_str = str(user_id)
    if user_id_str not in history:
        return {"recent_meals": [], "dislikes": []}
    user_data = history[user_id_str]
    recent_meals = user_data.get("history", [])[-3:] if user_data.get("history") else []
    return {
        "recent_meals": recent_meals,
        "dislikes": user_data.get("dislikes", [])
    }

def save_meal_to_history(user_id, meals, products):
    history = load_history()
    user_id_str = str(user_id)
    if user_id_str not in history:
        history[user_id_str] = {"history": [], "dislikes": []}
    today = datetime.now().strftime("%Y-%m-%d")
    history[user_id_str]["history"].append({
        "date": today,
        "meals": meals,
        "products_used": products
    })
    if len(history[user_id_str]["history"]) > 10:
        history[user_id_str]["history"] = history[user_id_str]["history"][-10:]
    save_history(history)

def clear_user_history(user_id):
    history = load_history()
    user_id_str = str(user_id)
    if user_id_str in history:
        history[user_id_str] = {"history": [], "dislikes": []}
        save_history(history)
        return True
    return False

# Обработчики команд
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    welcome_text = (
        "🍳 *Привет! Я твой персональный кулинарный помощник!*\n\n"
        "Я помогу тебе:\n"
        "• Составить рацион из продуктов, которые есть дома\n"
        "• Рассчитать КБЖУ каждого приёма пищи\n"
        "• Предложить разнообразные блюда (не повторяюсь день ото дня)\n"
        "• Посоветовать, что можно докупить\n\n"
        "📌 *Как пользоваться:*\n"
        "Просто напиши список продуктов через запятую\n"
        "Или нажми кнопку «Составить рацион»\n\n"
        "Пример: *курица, гречка, яйца, помидор, огурец*"
    )
    await message.answer(welcome_text, parse_mode="Markdown", reply_markup=main_keyboard)

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    help_text = (
        "❓ *Помощь*\n\n"
        "📝 *Как составить рацион:*\n"
        "Напиши список продуктов через запятую или нажми кнопку «Составить рацион»\n\n"
        "🧠 *Как бот запоминает:*\n"
        "• Я помню, что ты ел вчера и позавчера\n"
        "• Стараюсь не повторять блюда\n"
        "• Меняю типы кухни и основные ингредиенты\n\n"
        "🗑 *Очистить историю:*\n"
        "Нажми кнопку «Очистить историю» — это сбросит мою память о твоих предыдущих рационах\n\n"
        "📊 *Формат ответа:*\n"
        "• Время приёма пищи\n"
        "• Название блюда\n"
        "• КБЖУ\n"
        "• Рекомендации, что докупить"
    )
    await message.answer(help_text, parse_mode="Markdown")

@dp.message(Command("clear_memory"))
async def cmd_clear_memory(message: types.Message):
    if clear_user_history(message.from_user.id):
        await message.answer("🗑 История твоих рационов очищена! Теперь я буду составлять меню с чистого листа.")
    else:
        await message.answer("📭 У тебя пока нет сохранённой истории.")

@dp.message(F.text == "📋 Составить рацион")
async def button_generate(message: types.Message):
    await message.answer("📝 Напиши список продуктов, которые есть дома, через запятую\n\nПример: *курица, рис, помидоры, яйца, лук*", parse_mode="Markdown")

@dp.message(F.text == "🗑 Очистить историю")
async def button_clear(message: types.Message):
    if clear_user_history(message.from_user.id):
        await message.answer("🗑 История твоих рационов очищена!")
    else:
        await message.answer("📭 У тебя пока нет сохранённой истории.")

@dp.message(F.text == "❓ Помощь")
async def button_help(message: types.Message):
    await cmd_help(message)

# Генерация рациона
@dp.message(F.text)
async def generate_meal_plan(message: types.Message):
    user_id = message.from_user.id
    products = message.text.strip()
    
    processing_msg = await message.answer("🤔 Анализирую продукты и вспоминаю, что ты ел вчера...")
    
    context = get_user_context(user_id)
    recent_meals = context["recent_meals"]
    dislikes = context["dislikes"]
    
    prompt = f"""Ты — профессиональный нутрициолог и шеф-повар. Помоги пользователю составить разнообразный рацион на сегодня.

ПРОДУКТЫ В НАЛИЧИИ: {products}

"""

    if recent_meals:
        prompt += "ИСТОРИЯ ПРЕДЫДУЩИХ РАЦИОНОВ (НЕ ПОВТОРЯТЬ ЭТИ БЛЮДА!):\n"
        for i, meal_record in enumerate(recent_meals[-2:], 1):
            prompt += f"День {i}: {meal_record.get('meals', 'неизвестно')}\n"
        prompt += "\nВАЖНО: Предложи совершенно другие блюда! Поменяй сочетания, типы кухни, способы приготовления.\n"

    if dislikes:
        prompt += f"Пользователь НЕ ЛЮБИТ: {', '.join(dislikes)}\n"

    prompt += """

СОСТАВЬ РАЦИОН НА СЕГОДНЯ по следующей структуре:

🕗 ЗАВТРАК (08:00-09:00)
Название блюда:
КБЖУ: ___ ккал | Б___ / Ж___ / У___

🕛 ОБЕД (13:00-14:00)
Название блюда:
КБЖУ: ___ ккал | Б___ / Ж___ / У___

🕔 ПЕРЕКУС (17:00)
Название блюда:
КБЖУ: ___ ккал | Б___ / Ж___ / У___

🕘 УЖИН (19:30-20:30)
Название блюда:
КБЖУ: ___ ккал | Б___ / Ж___ / У___

📊 ИТОГО ЗА ДЕНЬ: ___ ккал | Б___ / Ж___ / У___

🛒 ЧТО МОЖНО ДОКУПИТЬ (максимум 3 позиции, опционально):

ПРАВИЛА:
1. Используй преимущественно указанные продукты
2. Каждый приём пищи должен отличаться от предыдущих дней
3. Добавляй эмодзи для наглядности, но НЕ используй символы * _ ` ~ для форматирования
4. Будь дружелюбным и мотивирующим
5. Если продуктов недостаточно для полноценного рациона, предложи конкретные добавки

ОТВЕЧАЙ ТОЛЬКО В УКАЗАННОМ ФОРМАТЕ. НЕ ИСПОЛЬЗУЙ ЗВЁЗДОЧКИ ИЛИ ПОДЧЁРКИВАНИЯ."""
    
    try:
        response = await deepseek.chat.completions.create(
            model="openai/gpt-oss-120b:free",
            messages=[
                {"role": "system", "content": "Ты — эксперт по здоровому питанию. Отвечай без использования символов форматирования типа * _ ` ~. Используй только эмодзи."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.8,
            timeout=60.0
        )
        
        meal_plan = response.choices[0].message.content
        meal_plan = re.sub(r'[*_`~]', '', meal_plan)
        
        save_meal_to_history(user_id, meal_plan, products.split(","))
        
        await processing_msg.edit_text(meal_plan)
        
        await message.answer(
            "💡 Совет: Я запомнил этот рацион. Завтра предложу что-то новое, чтобы тебе не надоедало!\n"
            "Напиши /clear_memory если хочешь сбросить историю."
        )
        
    except asyncio.TimeoutError:
        await processing_msg.edit_text("⏰ Превышено время ожидания ответа. Попробуй ещё раз.")
    except Exception as e:
        await processing_msg.edit_text(f"❌ Ошибка: {str(e)}\nПопробуй ещё раз.")

# Запуск бота
async def main():
    print("🤖 Бот запущен и готов к работе!")
    print(f"📁 Файл истории: {HISTORY_FILE}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())